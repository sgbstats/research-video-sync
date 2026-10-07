"""Daily pipeline: INBOX -> (validate) -> RAW (move) -> audio sync -> SYNCED.

Design rules
------------
* 01_RAW holds only original material.  No markers/state are ever written there and, once
  promoted, files in RAW are only ever opened for reading (ffprobe/ffmpeg -i).
* State and logs live in 99_LOGS_QC (state/<ID>.json + pipeline_status.csv).
* One broken participant never stops the batch: every participant runs inside try/except.
* Idempotent: a SUCCESS participant with intact outputs is skipped (cheap size checks only,
  RAW is never re-hashed); interrupted participants resume from recorded state.
* Outputs are written as *.partial and renamed atomically after ffmpeg + ffprobe succeed.
* --dry-run performs read-only decisions and mutates nothing (no moves, no state, no video).
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import checkpoints, media, syncest
from .config import Config
from .discovery import (Discovery, discover_jobs, find_sources, is_ignored_dir, is_junk_file, is_transit_file,
                        valid_participant_id)
from .media import MediaError, MediaInfo
from .state import StateStore, new_state
from .statuses import S, STICKY, bucket

log = logging.getLogger("rg2019")
ROLES = ("mom", "child")
WOULD_PROCESS = "WOULD_PROCESS"     # dry-run only
DRY_RUN_OBSERVATION_WARNING = (
    "dry-run cannot record the stability observation timer (it writes no state), and "
    "stability_requires_prior_observation=true with stability_minutes={minutes}>0 requires a PREVIOUS real run to "
    "have seen the files unchanged. New participants will therefore be reported as waiting on every dry-run. "
    "For a one-off full pilot dry-run you may TEMPORARILY set stability_requires_prior_observation=false in the "
    "config (dry-run itself stays non-mutating), then restore it before the real/scheduled runs.")


@dataclass
class Outcome:
    pid: str
    status: str
    message: str = ""
    kind: str = ""            # "new" | "skipped" | "" | "dry"
    plan: list[str] = field(default_factory=list)


@dataclass
class PreparedPair:
    pid: str
    state: dict
    sources: dict
    output_dir: Path


@dataclass
class Summary:
    outcomes: list[Outcome] = field(default_factory=list)
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)      # warnings repeated at the end of the summary

    def ids(self, pred: Callable[[Outcome], bool]) -> list[str]:
        return [o.pid for o in self.outcomes if pred(o)]

    @property
    def newly_completed(self):
        return self.ids(lambda o: o.status == S.SUCCESS and o.kind == "new")

    @property
    def skipped_completed(self):
        return self.ids(lambda o: o.status == S.SUCCESS and o.kind == "skipped")

    @property
    def waiting(self):
        return self.ids(lambda o: o.kind != "dry" and bucket(o.status) == "waiting")

    @property
    def manual_review(self):
        return self.ids(lambda o: o.kind != "dry" and bucket(o.status) == "manual_review")

    @property
    def failed(self):
        return self.ids(lambda o: o.kind != "dry" and o.status != S.SUCCESS
                        and bucket(o.status) == "failed")

    @property
    def would_process(self):
        return self.ids(lambda o: o.kind == "dry")

    def exit_code(self) -> int:
        return 2 if (self.manual_review or self.failed) else 0

    def render(self) -> str:
        def line(label, ids):
            return f"  {label:<20}{len(ids):>3}" + (f"  {', '.join(ids)}" if ids else "")
        by = {o.pid: o for o in self.outcomes}
        rows = ["", "=" * 64, "PIPELINE SUMMARY" + (" (DRY RUN - nothing was changed)" if self.dry_run else ""),
                "=" * 64]
        if self.dry_run:
            rows.append(line("Would process:", self.would_process))
        rows += [line("Newly completed:", self.newly_completed),
                 line("Skipped completed:", self.skipped_completed),
                 line("Waiting:", self.waiting),
                 line("Manual review:", self.manual_review),
                 line("Failed:", self.failed)]
        for label, ids in (("Waiting", self.waiting), ("Manual review", self.manual_review),
                           ("Failed", self.failed)):
            for pid in ids:
                rows.append(f"    [{label}] {pid}: {by[pid].status} - {by[pid].message}")
        for pid in self.would_process:
            rows.append(f"    [dry-run] {pid}:")
            rows += [f"        - {step}" for step in by[pid].plan]
        for note in self.notes:
            rows.append(f"NOTE: {note}")
        rows.append("=" * 64)
        return "\n".join(rows)


class Pipeline:
    def __init__(self, cfg: Config, dry_run: bool = False, only: list[str] | None = None,
                 reprocess: list[str] | None = None, manual_offset: float | None = None,
                 now_fn: Callable[[], datetime] | None = None,
                 sleep_fn: Callable[[float], None] = time.sleep):
        self.cfg, self.dry, self.only = cfg, dry_run, only
        self.reprocess = set(reprocess or [])
        self.manual_offset = manual_offset
        self.now = now_fn or (lambda: datetime.now().astimezone())
        self.sleep = sleep_fn
        self.store = StateStore(cfg)
        self._integrity: dict[str, str | None] = {}   # per-run cache of RAW integrity results
        self._preparing = False
        self._readiness: dict[tuple[str, Path, bool], tuple[str, str] | None] = {}

    # ------------------------------------------------------------------ helpers
    def _iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def _save(self, st: dict) -> None:
        if not self.dry:
            self.store.save(st)

    def _set(self, st: dict, status: str, msg: str = "") -> None:
        if st.get("status") != status or st.get("message") != msg:
            st["history"].append({"at": self._iso(), "event": f"{status}: {msg}" if msg else status})
        st["status"], st["message"] = status, msg

    def _fail(self, st: dict, status: str, msg: str, kind: str = "") -> Outcome:
        self._set(st, status, msg)
        self._save(st)
        log.warning("%s -> %s: %s", st["participant_id"], status, msg)
        return Outcome(st["participant_id"], status, msg, kind)

    @staticmethod
    def _payload(cfg: Config, folder: Path) -> list[Path]:
        """Everything in an INBOX participant folder that is original material (not the READY
        marker, not OS/Synology junk)."""
        return [p for p in sorted(folder.iterdir())
                if p.name != cfg.ready_marker_name and not is_junk_file(p.name)
                and not (p.is_dir() and is_ignored_dir(p.name))]

    @staticmethod
    def _snapshot(folder: Path, files: list[Path]) -> dict:
        snap = {}
        for p in files:
            try:
                stt = p.stat()
            except OSError:            # vanished between listing and stat: the next comparison will differ
                continue
            snap[str(p.relative_to(folder).as_posix())] = [stt.st_size, stt.st_mtime_ns]
        return snap

    def _files_under(self, folder: Path) -> list[Path]:
        return [p for p in sorted(folder.rglob("*"))
                if p.is_file() and p.name != self.cfg.ready_marker_name and not is_junk_file(p.name)]

    # ------------------------------------------------------------------ run
    def candidates(self) -> tuple[list[str], list[Outcome]]:
        cfg = self.cfg
        ids: set[str] = set()
        bad: list[Outcome] = []
        for root in (cfg.inbox_dir, cfg.raw_dir):
            if not root.is_dir():
                continue
            ids.update(discover_jobs(cfg, root))
            for p in sorted(root.iterdir()):
                if not p.is_dir() or (is_ignored_dir(p.name) and not valid_participant_id(cfg, p.name)):
                    continue
                if valid_participant_id(cfg, p.name):
                    ids.add(p.name)
                elif not any(o.pid == p.name for o in bad) and not any(
                        video.is_file() and video.suffix.lower() in cfg.video_extensions
                        for video in p.rglob("*")):
                    bad.append(Outcome(p.name, S.INVALID_ID,
                                       f"folder name does not match {cfg.participant_id_regex!r}"))
        ids.update(i for i in self.store.all_ids() if valid_participant_id(cfg, i))
        if self.only:
            ids = {i for i in ids if i in set(self.only)}
            bad = [b for b in bad if b.pid in set(self.only)]
        return sorted(ids), bad

    def run(self) -> Summary:
        if self.dry:
            return self._run_batch()
        with media.process_scope() as processes:
            try:
                return self._run_batch()
            except BaseException:
                processes.cancel()
                raise

    def _isolated(self, pid: str, action: Callable[[], Outcome | PreparedPair]) -> Outcome | PreparedPair:
        try:
            return action()
        except Exception as exc:
            log.exception("unexpected error for %s", pid)
            out = Outcome(pid, S.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            if not self.dry:
                try:
                    st = self.store.load(pid, self._iso())
                    self._set(st, S.INTERNAL_ERROR, out.message)
                    self.store.save(st)
                except Exception:
                    log.exception("could not record state for %s", pid)
            return out

    def _observe_inbox(self, ids: list[str]) -> None:
        """Observe all shared folders before any promotion changes their contents."""
        self._readiness.clear()
        jobs = discover_jobs(self.cfg, self.cfg.inbox_dir)
        for pid in ids:
            job = jobs.get(pid)
            if not job or job.problem:
                continue
            st = self._load(pid)
            if st.get("stage") == "PROMOTING":
                continue
            folder = job.mom[0].parent
            try:
                wait = self._check_ready(st, folder, self._files_under(folder))
            except OSError as exc:
                wait = (S.WAITING_FOR_STABILITY, f"could not observe source files: {exc}")
            self._readiness[(pid, folder, True)] = wait

    def _run_batch(self) -> Summary:
        summary = Summary(dry_run=self.dry)
        cfg = self.cfg
        if self.dry and cfg.stability_requires_prior_observation and cfg.stability_minutes > 0:
            msg = DRY_RUN_OBSERVATION_WARNING.format(minutes=f"{cfg.stability_minutes:g}")
            log.warning(msg)
            summary.notes.append(msg)
        ids, bad = self.candidates()
        for o in bad:
            log.warning("%s -> INVALID_ID: %s", o.pid, o.message)
            summary.outcomes.append(o)
        log.info("%d participant folder(s) to consider%s", len(ids), " [DRY RUN]" if self.dry else "")
        if not self.dry:
            self._observe_inbox(ids)
        self._preparing = not self.dry
        prepared = []
        try:
            for pid in ids:
                prepared.append(self._isolated(pid, lambda pid=pid: self.process(pid)))
        finally:
            self._preparing = False
            self._readiness.clear()
        jobs = [job for job in prepared if isinstance(job, PreparedPair)]
        results: dict[str, Outcome] = {}
        if jobs:
            log.info("Processing %d pair(s), maximum parallel pairs: %d", len(jobs), cfg.max_parallel_pairs)
            executor = ThreadPoolExecutor(max_workers=cfg.max_parallel_pairs, thread_name_prefix="rg2019-pair")
            futures = {}
            try:
                for job in jobs:
                    futures[job.pid] = executor.submit(
                        self._isolated, job.pid, lambda job=job: self._finish_raw(job))
                for pid, future in futures.items():
                    results[pid] = future.result()
            except BaseException:
                media.cancel_processes()
                for future in futures.values():
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        summary.outcomes.extend(results[item.pid] if isinstance(item, PreparedPair) else item
                                for item in prepared)
        if not self.dry:
            self.store.rewrite_csv()
        return summary

    # ------------------------------------------------------------------ per participant
    def process(self, pid: str) -> Outcome | PreparedPair:
        cfg = self.cfg
        discovered = {root: discover_jobs(cfg, root) for root in (cfg.inbox_dir, cfg.raw_dir)}
        legacy = any((root / pid).is_dir() for root in discovered)
        for root, jobs in discovered.items():
            if any(path.parent != root / pid for job in (jobs.get(pid),) if job
                   for path in job.mom + job.child + job.both):
                legacy = False
            if any(path.parent == root / pid for other_id, job in jobs.items() if other_id != pid
                   for path in job.mom + job.child + job.both):
                legacy = False
        if not legacy:
            return self._process_discovered(pid)
        inbox, raw, out = cfg.inbox_dir / pid, cfg.raw_dir / pid, cfg.synced_dir / pid
        self._integrity.pop(pid, None)
        st = self._load(pid)
        st["last_checked_at"] = self._iso()
        payload = self._payload(cfg, inbox) if inbox.is_dir() else []
        in_inbox, in_raw = bool(payload), raw.is_dir()
        resuming = st.get("stage") == "PROMOTING"

        if pid in self.reprocess or st.get("reprocess_pending"):
            if self.dry:
                log.info("[DRY-RUN] %s: would archive existing outputs to _superseded_<timestamp> and re-sync from RAW", pid)
                return Outcome(pid, WOULD_PROCESS, "", "dry",
                               ["--reprocess: archive existing outputs to 02_SYNCED/<ID>/_superseded_<timestamp>/",
                                "re-estimate the offset from 01_RAW and re-encode (nothing is deleted)"])
            self._prepare_reprocess(st, out)

        if in_inbox and in_raw and not resuming:
            return self._fail(st, S.RAW_CONFLICT,
                              f"01_RAW/{pid} already exists AND 00_INBOX/{pid} contains material. Nothing was "
                              f"moved or overwritten. Compare both folders and resolve manually.")
        if in_inbox:
            return self._from_inbox(pid, st, inbox, raw, out, resuming)
        if in_raw:
            return self._from_raw(pid, st, raw, out)
        if st.get("status") is None:
            return Outcome(pid, S.WAITING_FOR_STABILITY, "inbox folder is empty (nothing to process yet)")
        return self._fail(st, S.RAW_CONFLICT,
                          f"state says {st.get('status')} but neither 00_INBOX/{pid} nor 01_RAW/{pid} exists")

    def _process_discovered(self, pid: str) -> Outcome | PreparedPair:
        """Process a pair in an arbitrary directory, preserving its relative path."""
        cfg = self.cfg
        st = self._load(pid)
        st["last_checked_at"] = self._iso()
        incoming = discover_jobs(cfg, cfg.inbox_dir).get(pid)
        existing = discover_jobs(cfg, cfg.raw_dir).get(pid)
        if incoming and incoming.problem and st.get("stage") != "PROMOTING":
            status = S.MISSING_FILES if incoming.problem == "MISSING" else S.AMBIGUOUS_FILES
            return self._fail(st, status, incoming.describe())
        if existing and existing.problem and st.get("stage") != "PROMOTING":
            status = S.MISSING_FILES if existing.problem == "MISSING" else S.AMBIGUOUS_FILES
            return self._fail(st, status, existing.describe())
        if incoming and existing and st.get("stage") != "PROMOTING":
            return self._fail(st, S.RAW_CONFLICT, "source videos exist in both INBOX and RAW")
        pair = incoming if incoming and st.get("stage") != "PROMOTING" else existing or incoming
        if st.get("stage") == "PROMOTING" and st.get("source_folder") is not None:
            relative_dir = Path(st["source_folder"])
        elif pair:
            source_root = cfg.inbox_dir if pair is incoming else cfg.raw_dir
            relative_dir = pair.mom[0].parent.relative_to(source_root)
        else:
            relative_dir = Path(".")
        if not pair and st.get("stage") != "PROMOTING":
            return self._fail(st, S.RAW_CONFLICT, "no source videos found in INBOX or RAW")
        # Once recorded, a change of source location cannot redirect existing outputs.
        if st.get("source_folder") is not None and st["source_folder"] != relative_dir.as_posix():
            return self._fail(st, S.RAW_CONFLICT, "source folder changed after validation")
        st["source_folder"] = relative_dir.as_posix()
        out = cfg.synced_dir / relative_dir
        if pid in self.reprocess or st.get("reprocess_pending"):
            if self.dry:
                return Outcome(pid, WOULD_PROCESS, "", "dry", ["archive existing outputs and re-sync from RAW"])
            self._prepare_reprocess(st, out)
        if incoming and st.get("stage") != "PROMOTING":
            files = self._files_under(pair.mom[0].parent)
            wait = self._check_ready(st, pair.mom[0].parent, files)
            if wait:
                return self._fail(st, wait[0], wait[1])
            fail, found = self._discover_and_probe(st, pair.mom[0].parent, discovery=pair)
            if fail:
                return fail
            if self.dry:
                return Outcome(pid, WOULD_PROCESS, "", "dry", [
                    f"move pair to 01_RAW/{relative_dir}, then sync into 02_SYNCED/{relative_dir}/{pid}"])
            for role in ROLES:
                path, info = found[role]
                st["sources"][role] = {"name": path.relative_to(cfg.inbox_dir).as_posix(),
                                       "size": info.size_bytes, "mtime_ns": path.stat().st_mtime_ns,
                                       "sha256": media.sha256_file(path), "media": info.to_dict()}
            st["stage"] = "PROMOTING"
            self._set(st, S.RAW_PROMOTED, "promotion in progress")
            self._save(st)
        if st.get("stage") == "PROMOTING":
            try:
                for role in ROLES:
                    name = st["sources"][role]["name"]
                    src, dst = cfg.inbox_dir / name, cfg.raw_dir / name
                    if dst.exists():
                        if (src.exists() or dst.stat().st_size != st["sources"][role]["size"]
                                or media.sha256_file(dst) != st["sources"][role]["sha256"]):
                            return self._fail(st, S.RAW_CONFLICT, f"{name} already exists in RAW")
                    else:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        os.rename(src, dst)
                    st.setdefault("promotion", {}).setdefault("moved", []).append(name)
                    self._save(st)
            except OSError as exc:
                return self._fail(st, S.PROMOTE_FAILED, f"could not move pair to RAW: {exc}")
            st["stage"], st["raw_promoted_at"] = "RAW", self._iso()
            self._set(st, S.RAW_PROMOTED, "moved to RAW; sync pending")
            self._save(st)
            folder = cfg.inbox_dir / st["source_folder"]
            if folder != cfg.inbox_dir and not any(p.name != cfg.ready_marker_name for p in folder.iterdir()):
                self._tidy_inbox(folder)
        elif not st.get("sources"):
            files = self._files_under(pair.mom[0].parent)
            wait = self._check_ready(st, pair.mom[0].parent, files, marker=False)
            if wait:
                return self._fail(st, wait[0], wait[1])
            fail, found = self._discover_and_probe(st, pair.mom[0].parent, discovery=pair)
            if fail:
                return fail
            if self.dry:
                return Outcome(pid, WOULD_PROCESS, "", "dry", ["validate, hash and sync RAW pair"])
            for role in ROLES:
                path, info = found[role]
                st["sources"][role] = {"name": path.relative_to(cfg.raw_dir).as_posix(),
                                       "size": info.size_bytes, "mtime_ns": path.stat().st_mtime_ns,
                                       "sha256": media.sha256_file(path), "media": info.to_dict()}
            st["stage"] = "RAW"
            self._set(st, S.RAW_PROMOTED, "RAW verified")
            self._save(st)
        return self._from_raw(pid, st, cfg.raw_dir, out)

    def _load(self, pid: str) -> dict:
        if self.dry:      # read-only: never rename a corrupt state file in dry-run
            try:
                p = self.store.path(pid)
                return json.loads(p.read_text(encoding="utf-8")) if p.exists() else new_state(pid, self._iso())
            except (OSError, ValueError):
                return new_state(pid, self._iso())
        return self.store.load(pid, self._iso())

    # ------------------------------------------------------------------ readiness
    def _check_ready(self, st: dict, folder: Path, files: list[Path],
                     marker: bool = True) -> tuple[str, str] | None:
        """None if the folder may be processed, else (status, reason).  The READY marker only exists
        in INBOX, so RAW-only participants are checked with marker=False.

        Layers (a lower number never depends on a higher one):
          1. ALWAYS ON, independent of stability_minutes: transfer-in-progress files (.tmp, .partial,
             .part, .crdownload, .download, .filepart, Synology/editor temp names) block processing.
          2. stability_minutes > 0 only: file-age rule and prior-observation timer.
          3. ALWAYS ON while stability_recheck_seconds > 0 (even if stability_minutes == 0): re-stat
             every file after a short pause to catch a file that is still growing.
        stability_minutes = 0 therefore disables only layer 2."""
        key = (st["participant_id"], folder, marker)
        if key in self._readiness:
            return self._readiness[key]
        cfg = self.cfg
        if marker and cfg.require_ready_marker and not (folder / cfg.ready_marker_name).is_file():
            return S.WAITING_FOR_READY, f"{cfg.ready_marker_name} not present yet"
        transit = sorted(p.name for p in folder.rglob("*") if is_transit_file(p.name))
        if transit:
            return S.WAITING_FOR_STABILITY, ("transfer-in-progress file(s) present "
                                             f"({', '.join(transit[:3])}{', ...' if len(transit) > 3 else ''})")
        if not files:
            return S.WAITING_FOR_STABILITY, "no files yet"
        snap = self._snapshot(folder, files)
        if cfg.stability_minutes > 0:
            now_ts = self.now().timestamp()
            newest = max(p.stat().st_mtime for p in files)
            window = cfg.stability_minutes * 60
            if now_ts - newest < window:
                return S.WAITING_FOR_STABILITY, (f"a file was modified {(now_ts - newest) / 60:.0f} min ago "
                                                 f"(< {cfg.stability_minutes:g} min)")
            if cfg.stability_requires_prior_observation:
                prior = st.get("stability") or {}
                if prior.get("snapshot") != snap:
                    st["stability"] = {"snapshot": snap, "since": self._iso(), "since_ts": now_ts}
                    self._save(st)
                    return S.WAITING_FOR_STABILITY, ("first observation of these files (or they changed since "
                                                     "the last run); they will be eligible once unchanged for "
                                                     f"{cfg.stability_minutes:g} min")
                if now_ts - prior.get("since_ts", now_ts) < window:
                    return S.WAITING_FOR_STABILITY, (f"unchanged for only "
                                                     f"{(now_ts - prior.get('since_ts', now_ts)) / 60:.0f} min "
                                                     f"(< {cfg.stability_minutes:g} min)")
        if cfg.stability_recheck_seconds > 0:
            self.sleep(cfg.stability_recheck_seconds)
            snap2 = self._snapshot(folder, self._files_under(folder))   # ALL current files: additions and
            if snap2 != snap or any(is_transit_file(p.name) for p in folder.rglob("*")):   # removals count too
                return S.WAITING_FOR_STABILITY, "files changed during the stability re-check (still syncing?)"
        return None

    # ------------------------------------------------------------------ validation
    def _discover_and_probe(self, st: dict, folder: Path, kind: str = "",
                            discovery: Discovery | None = None) -> tuple[Outcome | None, dict]:
        cfg = self.cfg
        d: Discovery = discovery if discovery is not None else find_sources(cfg, folder)
        prob = d.problem
        if prob == "MISSING":
            return self._fail(st, S.MISSING_FILES, "mother and/or child video not found. " + d.describe(), kind), {}
        if prob == "AMBIGUOUS":
            return self._fail(st, S.AMBIGUOUS_FILES,
                              "more than one plausible video (no file was chosen automatically). "
                              + d.describe(), kind), {}
        paths = {"mom": d.mom[0], "child": d.child[0]}
        infos: dict[str, MediaInfo] = {}
        for role, p in paths.items():
            try:
                info = media.probe(cfg, p)
            except MediaError as exc:
                return self._fail(st, S.INVALID_VIDEO, f"{role}: {exc}", kind), {}
            if not info.has_video or not info.duration or info.duration <= 0:
                why = "no video stream" if not info.has_video else "duration is zero/unknown"
                return self._fail(st, S.INVALID_VIDEO, f"{role} ({p.name}): {why}", kind), {}
            infos[role] = info
        for role, p in paths.items():
            if not infos[role].has_audio:
                return self._fail(st, S.NO_AUDIO, f"{role} ({p.name}) has no audio stream; audio "
                                                  f"synchronisation is impossible", kind), {}
        return None, {r: (paths[r], infos[r]) for r in ROLES}

    # ------------------------------------------------------------------ INBOX branch
    def _from_inbox(self, pid, st, inbox, raw, out, resuming) -> Outcome | PreparedPair:
        cfg = self.cfg
        if resuming:
            log.info("%s: resuming interrupted promotion", pid)
        else:
            files = self._files_under(inbox)
            wait = self._check_ready(st, inbox, files)
            if wait:
                return self._fail(st, wait[0], wait[1])
            fail, found = self._discover_and_probe(st, inbox)
            if fail:
                return fail
            plan = [f"validate OK: mom={found['mom'][0].name} ({found['mom'][1].duration:.1f}s), "
                    f"child={found['child'][0].name} ({found['child'][1].duration:.1f}s)",
                    "compute SHA-256 of both source videos (once)",
                    f"MOVE 00_INBOX/{pid} -> 01_RAW/{pid} (files moved one by one, never overwritten)",
                    "then extract audio from RAW, estimate offset, and encode "
                    f"{pid}_mom_synced.mp4 / {pid}_child_synced.mp4"
                    + (f" / {pid}_side_by_side.mp4" if cfg.create_side_by_side else "")
                    + f" into 02_SYNCED/{pid}"]
            if self.dry:
                log.info("[DRY-RUN] %s: %s", pid, " | ".join(plan))
                return Outcome(pid, WOULD_PROCESS, "", "dry", plan)
            for role in ROLES:
                path, info = found[role]
                log.info("%s: hashing %s source %s (%.2f GB)", pid, role, path.name, info.size_bytes / 1e9)
                st["sources"][role] = {"name": path.name, "size": info.size_bytes,
                                       "mtime_ns": path.stat().st_mtime_ns,
                                       "sha256": media.sha256_file(path), "media": info.to_dict()}
            st["stage"] = "PROMOTING"
            self._set(st, S.RAW_PROMOTED, "promotion in progress")
            self._save(st)

        # ---- promote (move) ------------------------------------------------
        try:
            raw.mkdir(parents=True, exist_ok=resuming)
            moved = st.setdefault("promotion", {}).setdefault("moved", [])
            for item in self._payload(cfg, inbox):
                dst = raw / item.name
                if dst.exists():
                    return self._fail(st, S.RAW_CONFLICT,
                                      f"01_RAW/{pid}/{item.name} already exists; refusing to overwrite")
                os.rename(item, dst)          # same volume => atomic; never overwrites on Windows
                moved.append(item.name)
                self._save(st)
            self._save(st)
        except FileExistsError:
            return self._fail(st, S.RAW_CONFLICT, f"01_RAW/{pid} appeared while promoting; nothing overwritten")
        except OSError as exc:
            return self._fail(st, S.PROMOTE_FAILED,
                              f"could not move material into RAW ({exc}). INBOX and RAW must be on the same "
                              f"volume; files not yet moved remain in 00_INBOX (safe to re-run).")
        for role in ROLES:                     # verify what landed in RAW is what we recorded
            src = st["sources"][role]
            f = raw / src["name"]
            if not f.is_file() or f.stat().st_size != src["size"]:
                return self._fail(st, S.RAW_CONFLICT, f"{role} source {src['name']} in RAW does not match the "
                                                      f"size recorded at validation time")
        st["stage"], st["raw_promoted_at"] = "RAW", self._iso()
        self._set(st, S.RAW_PROMOTED, "moved to RAW; sync pending")
        self._save(st)
        self._tidy_inbox(inbox)
        log.info("%s: promoted to 01_RAW", pid)
        return self._from_raw(pid, st, raw, out, promoted_now=True)

    def _tidy_inbox(self, inbox: Path) -> None:
        """Remove the READY marker and the (now empty) inbox folder.  Never removes anything else."""
        try:
            (inbox / self.cfg.ready_marker_name).unlink(missing_ok=True)
            if not any(inbox.iterdir()):
                inbox.rmdir()
        except OSError as exc:
            log.warning("could not tidy %s: %s", inbox, exc)

    # ------------------------------------------------------------------ RAW branch
    def _sources_intact(self, st: dict, raw: Path) -> str | None:
        """None if the RAW sources still match what was recorded at ingest, else a reason.

        Cheap daily check: existence + size + mtime_ns (three stat() calls, no reads).
          * size differs                      -> conflict immediately
          * size same, mtime_ns differs       -> recompute SHA-256 of THAT file once and compare with the
                                                 stored hash: different => conflict; identical => content is
                                                 unchanged, the new mtime_ns is stored in STATE only (RAW is
                                                 never modified) so the hash is not repeated on later runs.
        Unchanged files are never re-hashed.  The result is cached per participant for the current
        run so the same mismatch is not hashed twice.  Limit: an edit that preserves BOTH size and mtime
        is not detectable without hashing every run."""
        pid = st["participant_id"]
        if pid in self._integrity:
            return self._integrity[pid]
        result = self._check_sources(st, raw)
        self._integrity[pid] = result
        return result

    def _check_sources(self, st: dict, raw: Path) -> str | None:
        for role in ROLES:
            src = st.get("sources", {}).get(role)
            if not src:
                return f"no recorded {role} source"
            f = raw / src["name"]
            if not f.is_file():
                return f"{role} source {src['name']} is missing from RAW"
            stat = f.stat()
            if stat.st_size != src["size"]:
                return f"{role} source {src['name']} size differs from the recorded value"
            recorded_mtime = src.get("mtime_ns")
            if recorded_mtime is not None and stat.st_mtime_ns != recorded_mtime:
                if not src.get("sha256"):
                    return f"{role} source {src['name']} was modified and has no recorded SHA-256 to verify against"
                log.info("%s: %s source mtime changed with identical size - verifying SHA-256 once",
                         st["participant_id"], role)
                if media.sha256_file(f) != src["sha256"]:
                    return (f"{role} source {src['name']} content differs from the recorded SHA-256 "
                            f"(size identical, modified time changed)")
                src["mtime_ns"] = stat.st_mtime_ns          # content verified: refresh STATE only
                self._save(st)
        return None

    def _outputs_valid(self, st: dict, out: Path) -> bool:
        files = st.get("outputs", {}).get("files", {})
        keys = list(ROLES)
        if self.cfg.create_side_by_side and "side_by_side" in files:
            keys.append("side_by_side")       # expected AND recorded: must still be intact
        for key in keys:
            rec = files.get(key)
            f = out / rec["name"] if rec else None
            suffix = "side_by_side" if key == "side_by_side" else f"{key}_synced"
            if (not rec or rec["name"] != self._output_name(st["participant_id"], suffix)
                    or not f.is_file() or f.stat().st_size != rec["size"]):
                return False
        return True

    def _from_raw(self, pid, st, raw, out, promoted_now: bool = False) -> Outcome | PreparedPair:
        cfg = self.cfg
        # 1. tear down completed work quickly
        if st.get("sync_completed_at") and st.get("sources") and st.get("stage") != "PROMOTING":
            bad = self._sources_intact(st, raw)
            if bad is None and self._outputs_valid(st, out):
                if st.get("status") != S.SUCCESS:
                    self._set(st, S.SUCCESS, "")
                self._save(st)
                if not self.dry:
                    checkpoints.cleanup(cfg, pid)
                log.info("%s: already complete - skipped", pid)
                return Outcome(pid, S.SUCCESS, "", "skipped")
        st["stage"] = "RAW" if st.get("stage") in (None, "NEW", "PROMOTING") else st["stage"]
        if st.get("status") in STICKY and pid not in self.reprocess and self.manual_offset is None:
            self._save(st)
            return Outcome(pid, st["status"], st.get("message", "") + "  [needs manual review; use --reprocess "
                           "after checking, or --manual-offset]")
        if (st.get("status") in (S.SYNC_FAILED, S.ENCODE_FAILED) and st.get("attempts", 0) >= cfg.max_attempts
                and pid not in self.reprocess):
            self._save(st)
            return Outcome(pid, st["status"], f"{st.get('message', '')}  [retry limit {cfg.max_attempts} reached; "
                                              f"use --reprocess {pid}]")

        # 2. sources: record (RAW-only participants, e.g. placed manually) or verify
        if not st.get("sources"):
            files = self._files_under(raw)
            wait = self._check_ready(st, raw, files, marker=False)
            if wait:
                return self._fail(st, wait[0], wait[1])
            fail, found = self._discover_and_probe(st, raw)
            if fail:
                return fail
            if self.dry:
                plan = [f"RAW-only participant without state: validate, hash and sync from 01_RAW/{pid}"]
                return Outcome(pid, WOULD_PROCESS, "", "dry", plan)
            for role in ROLES:
                path, info = found[role]
                st["sources"][role] = {"name": path.name, "size": info.size_bytes,
                                       "mtime_ns": path.stat().st_mtime_ns,
                                       "sha256": media.sha256_file(path), "media": info.to_dict()}
            st["stage"] = "RAW"
            self._set(st, S.RAW_PROMOTED, "RAW verified (no prior state)")
            self._save(st)
        else:
            bad = self._sources_intact(st, raw)
            if bad:
                return self._fail(st, S.RAW_CONFLICT, bad + ". RAW was changed after validation - manual review.")
            if st.get("stage") == "PROMOTING":
                st["stage"], st["raw_promoted_at"] = "RAW", st.get("raw_promoted_at") or self._iso()
                self._set(st, S.RAW_PROMOTED, "promotion completed")
        src = {r: (raw / st["sources"][r]["name"], MediaInfo(**st["sources"][r]["media"])) for r in ROLES}
        job = PreparedPair(pid, st, src, out)
        return job if self._preparing else self._finish_raw(job)

    def _finish_raw(self, job: PreparedPair) -> Outcome:
        pid, st, src, out = job.pid, job.state, job.sources, job.output_dir
        cfg = self.cfg
        # 3. offset
        offset = st.get("sync", {}).get("offset_seconds")
        if self.manual_offset is not None and self.only and len(self.only) == 1:
            offset = float(self.manual_offset)
            st["sync"] = {"offset_seconds": offset, "confidence": None, "offset_source": "manual",
                          "estimated_at": self._iso(), "reason": "offset supplied manually after review"}
            self._save(st)
        if offset is None:
            if self.dry:
                plan = ["extract mono audio from both RAW videos, estimate offset (windowed two-stage NCC)",
                        f"encode {pid}_mom_synced.mp4 / {pid}_child_synced.mp4 into 02_SYNCED/{pid}"]
                return Outcome(pid, WOULD_PROCESS, "", "dry", plan)
            try:
                est = self._estimate(pid, src, st)
            except (MediaError, OSError, ValueError) as exc:
                st["attempts"] = st.get("attempts", 0) + 1
                return self._fail(st, S.SYNC_FAILED, str(exc))
            st["sync"] = {"offset_seconds": est.offset_seconds, "confidence": est.confidence,
                          "offset_source": "auto", "estimated_at": self._iso(), "reason": est.reason,
                          "details": est.to_dict(with_windows=True), "convention":
                          "offset = t_mom - t_child; >0 trims mom start, <0 trims child start"}
            if not est.ok:
                return self._fail(st, S.LOW_CONFIDENCE,
                                  f"offset={est.offset_seconds}, confidence={est.confidence:.2f}: {est.reason}. "
                                  f"No videos were produced. Review, then --reprocess or --manual-offset.")
            offset = est.offset_seconds
            log.info("%s: offset %+.4f s (confidence %.2f; %s)", pid, offset, est.confidence, est.reason)
            self._save(st)
        elif st["sync"].get("offset_source") != "manual" and st["sync"].get("offset_seconds") is not None:
            log.info("%s: reusing recorded offset %+.4f s", pid, offset)

        # 4. encode
        if self.dry:
            mt, ct = syncest.trim_plan(offset, cfg.encode.min_trim_seconds)
            return Outcome(pid, WOULD_PROCESS, "", "dry",
                           [f"encode with offset {offset:+.4f}s (trim mom {mt:.3f}s, child {ct:.3f}s)"])
        return self._encode_all(pid, st, src, out, offset)

    def _estimate(self, pid: str, src: dict, st: dict) -> syncest.SyncEstimate:
        cfg = self.cfg
        prm = cfg.sync
        arrays = {}
        try:
            for role in ROLES:
                path, _ = src[role]
                arrays[role] = checkpoints.audio(cfg, st, role, path, self._save)
            return syncest.estimate_offset(arrays["mom"], arrays["child"], prm,
                                           src["mom"][1].audio_start, src["child"][1].audio_start)
        finally:
            for array in arrays.values():
                array._mmap.close()

    # ------------------------------------------------------------------ encoding
    def _output_name(self, pid: str, suffix: str) -> str:
        description = f"_{self.cfg.video_description}" if self.cfg.video_description else ""
        return f"{pid}{description}_{suffix}.mp4"

    def _encode_all(self, pid, st, src, out: Path, offset: float) -> Outcome:
        cfg = self.cfg
        mt, ct = syncest.trim_plan(offset, cfg.encode.min_trim_seconds)
        trims = {"mom": mt, "child": ct}
        out.mkdir(parents=True, exist_ok=True)
        conflict = checkpoints.recover_outputs(st, out, self._save)
        if conflict:
            return self._fail(st, S.OUTPUT_CONFLICT, conflict)
        for stale in out.glob(f"{pid}_*.mp4.partial"):
            log.info("%s: removing stale %s", pid, stale.name)
            stale.unlink(missing_ok=True)
        files = st.setdefault("outputs", {}).setdefault("files", {})
        try:
            for role in ROLES:
                if not self._make_output(pid, st, files, out, role, src[role][0], src[role][1], trims[role]):
                    return Outcome(pid, S.OUTPUT_CONFLICT, st["message"])
            if cfg.create_side_by_side:
                final = out / self._output_name(pid, "side_by_side")
                rec = files.get("side_by_side")
                if rec and rec["name"] != final.name:
                    return self._fail(st, S.OUTPUT_CONFLICT,
                                      "Configured video_description differs from recorded outputs; "
                                      "use --reprocess to change output names.")
                if not (rec and final.is_file() and final.stat().st_size == rec["size"]):
                    if final.exists():       # unrecorded or truncated/modified: never overwrite
                        return self._fail(st, S.OUTPUT_CONFLICT,
                                          f"{final.name} exists in 02_SYNCED but does not match the recorded "
                                          f"state; not overwriting. Move/rename it and re-run.")
                    part = final.with_name(final.name + ".partial")   # missing (or never made): (re)create
                    media.side_by_side(cfg, out / files["mom"]["name"], out / files["child"]["name"], part)
                    self._commit(st, part, final, "side_by_side", None, "reencode")
                    self._save(st)
        except MediaError as exc:
            st["attempts"] = st.get("attempts", 0) + 1
            return self._fail(st, S.ENCODE_FAILED, str(exc))
        except OSError as exc:
            st["attempts"] = st.get("attempts", 0) + 1
            return self._fail(st, S.ENCODE_FAILED, f"filesystem error while writing outputs: {exc}")
        st["sync_completed_at"] = self._iso()
        st["stage"], st["attempts"] = "SYNCED", 0
        self._set(st, S.SUCCESS, "")
        self._save(st)
        checkpoints.cleanup(cfg, pid)
        log.info("%s: SUCCESS (offset %+.4f s)", pid, offset)
        return Outcome(pid, S.SUCCESS, "", "new")

    def _make_output(self, pid, st, files, out, role, src_path, info, trim) -> bool:
        cfg = self.cfg
        final = out / self._output_name(pid, f"{role}_synced")
        rec = files.get(role)
        if rec and rec["name"] != final.name:
            self._fail(st, S.OUTPUT_CONFLICT,
                       "Configured video_description differs from recorded outputs; "
                       "use --reprocess to change output names.")
            return False
        if rec and final.is_file() and final.stat().st_size == rec["size"]:
            return True                                   # already produced by an earlier (interrupted) run
        if final.exists():
            self._fail(st, S.OUTPUT_CONFLICT, f"{final.name} exists in 02_SYNCED but does not match the "
                                              f"recorded state; not overwriting. Move/rename it and re-run.")
            return False
        part = final.with_name(final.name + ".partial")
        log.info("%s: writing %s (trim %.3f s)", pid, final.name, trim)
        method = media.encode(cfg, src_path, part, trim, info)
        expected = (info.duration or 0) - trim
        self._commit(st, part, final, role, expected, method)
        self._save(st)
        return True

    def _commit(self, st: dict, part: Path, final: Path, key: str,
                expected: float | None, method: str):
        """Validate the .partial with ffprobe, then rename atomically."""
        cfg = self.cfg
        pi = media.probe(cfg, part)
        if not (pi.has_video and pi.has_audio and pi.duration and pi.duration > 0):
            part.unlink(missing_ok=True)
            raise MediaError(f"{final.name}: encoded file failed validation (video={pi.has_video}, "
                             f"audio={pi.has_audio}, duration={pi.duration})")
        if expected is not None and abs(pi.duration - expected) > cfg.encode.duration_tolerance_seconds:
            part.unlink(missing_ok=True)
            raise MediaError(f"{final.name}: duration {pi.duration:.2f}s differs from expected {expected:.2f}s")
        checkpoints.publish_output(st, part, final, key, pi, method, self._save)

    # ------------------------------------------------------------------ reprocess
    def _prepare_reprocess(self, st: dict, out: Path) -> None:
        """Archive (never delete) previous outputs and clear the sync result so the participant is
        recomputed from RAW.  Recorded source hashes are kept."""
        pid = st["participant_id"]
        log.info("%s: --reprocess requested", pid)
        if not st.get("reprocess_pending"):
            st["reprocess_pending"] = {
                "archive": f"_superseded_{self.now().strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}",
                "files": [f.name for f in sorted(out.iterdir())
                          if f.is_file() and f.name.startswith(f"{pid}_")] if out.is_dir() else []}
            self._save(st)
        pending = st["reprocess_pending"]
        arch = out / pending["archive"]
        for name in pending["files"]:
            source, dest = out / name, arch / name
            if dest.exists():
                if source.exists():
                    raise OSError(f"reprocess archive conflict: {dest}")
                continue
            arch.mkdir(parents=True, exist_ok=True)
            os.rename(source, dest)
        st.update({"sync": {}, "outputs": {"files": {}}, "sync_completed_at": None, "attempts": 0})
        st.pop("audio_checkpoints", None)
        st.pop("reprocess_pending", None)
        self._set(st, S.RAW_PROMOTED, "reprocess requested")
        self._save(st)
        checkpoints.cleanup(self.cfg, pid)
