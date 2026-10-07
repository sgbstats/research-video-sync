#!/usr/bin/env python3
"""RG2019 follow-up video synchronisation pipeline (v2) - command-line entry point.

    research-video-sync --config config.json --dry-run
    research-video-sync --config config.json

Exit codes: 0 = finished, nothing needs a human;  2 = finished, but some participants need
manual review or failed;  3 = configuration / tooling / lock problem (nothing was processed).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from rg2019 import __version__, config as cfgmod, media
from rg2019.discovery import (
    discover_jobs,
    find_sources,
    is_junk_file,
    is_transit_file,
    valid_participant_id,
)
from rg2019.pipeline import Pipeline, Summary
from rg2019.state import RunLock
from rg2019.statuses import S

log = logging.getLogger("rg2019")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="RG2019 INBOX -> RAW -> SYNCED pipeline")
    ap.add_argument("directory", nargs="?", type=Path, metavar="DIRECTORY",
                    help="run in this data directory (default: current directory)")
    ap.add_argument("--config", "--config-path", "--config_path", dest="config", type=Path,
                    help="source config for setup or config to load when running; overrides DIRECTORY (default: DIRECTORY/config.json)")
    ap.add_argument("--setup", type=Path, metavar="FOLLOWUP_ROOT",
                    help="create FOLLOWUP_ROOT/config.json and make its configured folders")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what WOULD happen; moves nothing, writes no state, encodes nothing")
    ap.add_argument("--yolo", action="store_true",
                    help="run without approval and with stability_minutes=0 (other safety checks remain)")
    ap.add_argument("--participant", action="append", metavar="ID",
                    help="only handle this participant (repeatable)")
    ap.add_argument("--reprocess", action="append", metavar="ID",
                    help="recompute this participant from RAW; previous outputs are archived, not deleted")
    ap.add_argument("--manual-offset", type=float, metavar="SECONDS",
                    help="with exactly one --participant: use this reviewed offset (>0 trims mom, <0 trims child)")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO"])
    ap.add_argument("--version", action="version", version=f"pipeline_rg2019 {__version__}")
    args = ap.parse_args(argv)
    if args.setup is not None and args.directory is not None:
        ap.error("DIRECTORY cannot be combined with --setup; use --setup DIRECTORY")
    return args


def setup_project(followup_root: Path, config_path: Path, overwrite: bool = False,
                  source_config: Path | None = None) -> Path:
    """Copy config settings into the follow-up root and initialize its folder layout."""
    if config_path.exists() and not overwrite:
        raise FileExistsError(f"config already exists: {config_path}; refusing to overwrite it")

    template_path = (Path(__file__).resolve().parent / "config.example.json"
                     if source_config is None else source_config)
    data = json.loads(template_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise cfgmod.ConfigError(f"Config must be a JSON object ({template_path})")
    data["followup_root"] = str(followup_root.expanduser().resolve())
    cfg = cfgmod.from_dict(data)

    cfg.followup_root.mkdir(parents=True, exist_ok=True)
    for folder in (cfg.inbox_dir, cfg.raw_dir, cfg.synced_dir, cfg.logs_dir,
                   cfg.log_file_dir, cfg.state_dir):
        folder.mkdir(parents=True, exist_ok=True)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    with config_path.open(mode, encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return config_path


def confirm_config_overwrite(config_path: Path) -> bool:
    """Confirm replacement of an existing config before setup makes any changes."""
    try:
        response = input(f"{config_path} already exists. Overwrite it and continue setup? Type 'yes': ")
    except (EOFError, OSError):
        return False
    return response.strip().lower() == "yes"


def setup_logging(level: str, log_dir: Path | None) -> None:
    root = logging.getLogger("rg2019")
    root.handlers.clear()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    if sys.stdout is not None:                       # None under pythonw.exe / some schedulers
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"pipeline_{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def matching_video_files(cfg: cfgmod.Config) -> list[Path]:
    """Return ID-bearing video files under INBOX and RAW."""
    matches = []
    for root in (cfg.inbox_dir, cfg.raw_dir):
        for job in discover_jobs(cfg, root).values():
            matches.extend(job.mom + job.child + job.both)
    return matches


def preflight_inventory(cfg: cfgmod.Config, only: list[str] | None = None,
                        reprocess: list[str] | None = None,
                        manual_offset: float | None = None) -> tuple[str, bool, Summary]:
    """Build a read-only report and return eligibility and dry-run outcomes."""
    preview = Pipeline(cfg, dry_run=True, only=only, reprocess=reprocess,
                       manual_offset=manual_offset)
    pipeline_logger = logging.getLogger("rg2019")
    previous_level = pipeline_logger.level
    pipeline_logger.setLevel(logging.ERROR)
    try:
        summary = preview.run()
    finally:
        pipeline_logger.setLevel(previous_level)

    videos_by_pid: dict[str, list[Path]] = {}
    jobs_by_pid = {}
    for root in (cfg.inbox_dir, cfg.raw_dir):
        if not root.is_dir():
            continue
        for pid, job in discover_jobs(cfg, root).items():
            jobs_by_pid.setdefault(pid, []).append(job)
            videos_by_pid.setdefault(pid, []).extend(job.mom + job.child + job.both)
        for participant_dir in root.iterdir():
            if participant_dir.is_dir():
                if valid_participant_id(cfg, participant_dir.name):
                    existing = videos_by_pid.setdefault(participant_dir.name, [])
                    existing.extend(path for path in participant_dir.iterdir()
                                    if path.is_file() and path.suffix.lower() in cfg.video_extensions
                                    and path not in existing)
    for paths in videos_by_pid.values():
        paths.sort(key=lambda path: str(path).lower())
    outcomes = {outcome.pid: outcome for outcome in summary.outcomes}
    selected = set(only or [])
    participant_dirs: dict[str, list[Path]] = {}
    for root in (cfg.inbox_dir, cfg.raw_dir):
        if not root.is_dir():
            continue
        for folder in root.iterdir():
            if folder.is_dir() and (not selected or folder.name in selected):
                participant_dirs.setdefault(folder.name, []).append(folder)

    will_sync: dict[str, list[Path]] = {}
    already_synced: dict[str, list[Path]] = {}
    no_match: dict[str, tuple[str, list[Path]]] = {}
    no_suffix: dict[str, list[Path]] = {}
    blocked: dict[str, tuple[str, list[Path]]] = {}

    for pid, outcome in outcomes.items():
        if selected and pid not in selected:
            continue
        folders = participant_dirs.get(pid, [])
        files = videos_by_pid.get(pid, [])
        if not valid_participant_id(cfg, pid):
            no_match[pid] = (f"participant ID does not match {cfg.participant_id_regex!r}", files)
            continue
        candidate_folder = next((folder for folder in folders if folder.parent == cfg.inbox_dir), None)
        if candidate_folder is None:
            candidate_folder = next((folder for folder in folders if folder.parent == cfg.raw_dir), None)
        discovery = jobs_by_pid.get(pid, [None])[0]
        if discovery is None and candidate_folder:
            discovery = find_sources(cfg, candidate_folder)
        if discovery is None and outcome.status != S.SUCCESS:
            no_match[pid] = (outcome.message or "participant source folder is missing", files)
        if discovery is not None and discovery.problem is None:
            matched = sorted(discovery.mom + discovery.child, key=lambda p: p.name.lower())
            if outcome.kind == "skipped" and outcome.status == S.SUCCESS:
                already_synced[pid] = matched
            elif outcome.kind == "dry":
                will_sync[pid] = matched
            unmatched = [path for path in videos_by_pid.get(pid, []) if path not in matched]
            if unmatched:
                no_match[pid] = ("video does not match the configured mother or child pattern", unmatched)
        elif discovery is not None:
            no_match[pid] = (outcome.message or "no unique mother/child video pair", videos_by_pid.get(pid, []))

        unsupported = []
        source_dirs = {p.parent for p in files} | set(folders)
        for folder in source_dirs:
            for path in folder.iterdir():
                if (not path.is_file() or is_junk_file(path.name) or is_transit_file(path.name)):
                    continue
                stem = path.stem.lower()
                if ((cfg.mom_pattern.lower() in stem or cfg.child_pattern.lower() in stem)
                        and path.suffix.lower() not in cfg.video_extensions):
                    unsupported.append(path)
        if unsupported:
            no_suffix[pid] = sorted(set(unsupported), key=lambda p: str(p).lower())

        if outcome.status != "WOULD_PROCESS" and outcome.status not in (
                S.SUCCESS, S.MISSING_FILES, S.AMBIGUOUS_FILES, S.INVALID_ID):
            blocked_files = (sorted(discovery.mom + discovery.child, key=lambda p: p.name.lower())
                             if discovery is not None and discovery.problem is None else files)
            blocked[pid] = (f"{outcome.status}: {outcome.message}".rstrip(": "),
                            blocked_files)

    for pid, folders in participant_dirs.items():
        if pid in outcomes:
            continue
        videos = videos_by_pid.get(pid, [])
        if videos:
            no_match[pid] = ("participant ID does not match the configured regex", videos)
    for pid in selected - outcomes.keys():
        no_match[pid] = ("requested participant folder does not exist", [])

    lines = ["", "=" * 64, "PRE-RUN APPROVAL REPORT", "=" * 64,
             f"Will sync: {sum(len(paths) for paths in will_sync.values())} source video(s) "
             f"across {len(will_sync)} participant(s)"]

    def add_group(title: str, groups: dict[str, list[Path]]) -> None:
        count = sum(len(paths) for paths in groups.values())
        lines.append(f"{title}: {count} video/file(s) across {len(groups)} participant(s)")
        for pid, paths in sorted(groups.items()):
            if not paths:
                lines.append(f"  {pid}: no matching source videos")
            for path in paths:
                display_path = (path.relative_to(cfg.followup_root)
                                if path.is_relative_to(cfg.followup_root) else path)
                lines.append(f"  {display_path} ({pid})")

    add_group("Already synced (will be skipped)", already_synced)
    lines.append(f"No match: {sum(len(paths) for _, paths in no_match.values())} video(s) "
                 f"across {len(no_match)} participant(s)")
    for pid, (reason, paths) in sorted(no_match.items()):
        lines.append(f"  {pid}: {reason}")
        for path in paths:
            display_path = (path.relative_to(cfg.followup_root)
                            if path.is_relative_to(cfg.followup_root) else path)
            lines.append(f"    {display_path}")
    add_group("No configured video suffix", no_suffix)
    lines.append(f"Blocked for another reason: {sum(len(paths) for _, paths in blocked.values())} video(s) "
                 f"across {len(blocked)} participant(s)")
    for pid, (reason, paths) in sorted(blocked.items()):
        lines.append(f"  {pid}: {reason or 'not eligible for processing'}")
        for path in paths:
            display_path = (path.relative_to(cfg.followup_root)
                            if path.is_relative_to(cfg.followup_root) else path)
            lines.append(f"    {display_path}")
    excluded_paths = {
        path
        for groups in (already_synced, no_suffix)
        for paths in groups.values()
        for path in paths
    }
    excluded_paths.update(path for _, paths in no_match.values() for path in paths)
    excluded_paths.update(path for _, paths in blocked.values() for path in paths)
    lines.insert(5, f"Won't sync this run: {len(excluded_paths)} video/file(s) "
                     f"(see exclusion reasons below)")
    lines.append("=" * 64)
    return "\n".join(lines), bool(will_sync), summary


def preflight_report(cfg: cfgmod.Config, only: list[str] | None = None,
                     reprocess: list[str] | None = None,
                     manual_offset: float | None = None) -> str:
    """Build a read-only run inventory using the pipeline's dry-run decisions."""
    report, _, _ = preflight_inventory(cfg, only, reprocess, manual_offset)
    return report


def record_first_observations(cfg: cfgmod.Config, summary: Summary) -> None:
    """Persist first stable-file sightings without promoting or encoding videos."""
    if cfg.stability_minutes <= 0 or not cfg.stability_requires_prior_observation:
        return
    observer = Pipeline(cfg)
    jobs = {root: discover_jobs(cfg, root) for root in (cfg.inbox_dir, cfg.raw_dir)}
    for outcome in summary.outcomes:
        if (outcome.status != S.WAITING_FOR_STABILITY
                or not outcome.message.startswith("first observation of these files")):
            continue
        source = next(((root, found[outcome.pid]) for root, found in jobs.items()
                       if outcome.pid in found and not found[outcome.pid].problem
                       and found[outcome.pid].mom), None)
        if source is None:
            continue
        root, job = source
        folder = job.mom[0].parent
        files = observer._files_under(folder)
        if not files:
            continue
        try:
            st = observer.store.load(outcome.pid, observer._iso())
            # Repeat readiness checks under the lock: a transfer may have started since preflight.
            wait = observer._check_ready(st, folder, files, marker=root == cfg.inbox_dir)
        except OSError as exc:
            log.warning("%s: could not record stability observation: %s", outcome.pid, exc)
            continue
        if wait and wait[1].startswith("first observation of these files"):
            log.info("%s: recorded first stability observation for a later run", outcome.pid)


def request_approval() -> bool:
    """Require an explicit interactive confirmation before a real run."""
    try:
        response = input("Approve and start the workflow? Type 'yes' to continue: ")
    except (EOFError, OSError):
        return False
    return response.strip().lower() == "yes"


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.setup is not None:
        followup_root = args.setup.expanduser().resolve()
        config_path = followup_root / "config.json"
        overwrite = config_path.exists()
        if overwrite and not confirm_config_overwrite(config_path):
            print("Setup cancelled; config and folders were not changed.")
            return 1
        try:
            config_path = setup_project(
                followup_root, config_path, overwrite=overwrite, source_config=args.config,
            )
        except (OSError, ValueError, cfgmod.ConfigError) as exc:
            print(f"SETUP ERROR: {exc}", file=sys.stderr)
            return 3
        print(f"Created {config_path} and initialized followup folders under {followup_root}")
        print("Review the generated config and edit tool paths if needed before running the pipeline.")
        return 0
    if args.config is not None:
        config_path = args.config.expanduser()
    else:
        run_directory = (args.directory or Path.cwd()).expanduser().resolve()
        if not run_directory.is_dir():
            print(f"CONFIG ERROR: Run directory does not exist or is not a directory: {run_directory}",
                  file=sys.stderr)
            return 3
        config_path = run_directory / "config.json"
    try:
        cfg = cfgmod.load(config_path)
    except cfgmod.ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 3
    if args.config is None:
        cfg.followup_root = run_directory
    if args.manual_offset is not None and not (args.participant and len(args.participant) == 1):
        print("CONFIG ERROR: --manual-offset requires exactly one --participant", file=sys.stderr)
        return 3
    if args.yolo:
        cfg.stability_minutes = 0
    setup_logging(args.log_level, None)

    if not cfg.followup_root.is_dir():
        log.error("followup_root does not exist: %s", cfg.followup_root)
        return 3
    if not cfg.inbox_dir.is_dir():
        log.error("inbox folder does not exist: %s", cfg.inbox_dir)
        return 3
    try:
        versions = media.check_tools(cfg)
    except media.MediaError as exc:
        log.error("%s", exc)
        return 3
    log.info("pipeline_rg2019 %s | root=%s | dry_run=%s", __version__, cfg.followup_root, args.dry_run)
    for exe, v in versions.items():
        log.info("%s: %s", exe, v)
    if args.dry_run:
        videos = matching_video_files(cfg)
        log.info("Videos matching participant_id_regex=%r and configured suffixes %s (%d):",
                 cfg.participant_id_regex, ", ".join(cfg.video_extensions), len(videos))
        for video in videos:
            display_path = video.relative_to(cfg.followup_root) if video.is_relative_to(cfg.followup_root) else video
            log.info("  %s", display_path.as_posix())
        if not videos:
            log.info("  (none)")
    else:
        report, has_work, preview = preflight_inventory(cfg, args.participant, args.reprocess,
                                                       args.manual_offset)
        for line in report.splitlines():
            log.info("%s", line)
        if cfg.lock_file.exists():
            try:
                with RunLock(cfg.lock_file, cfg.lock_stale_hours):
                    pass
            except RuntimeError as exc:
                log.error("%s", exc)
                return 3
        if not has_work:
            if cfg.stability_minutes > 0 and cfg.stability_requires_prior_observation:
                try:
                    with RunLock(cfg.lock_file, cfg.lock_stale_hours):
                        record_first_observations(cfg, preview)
                except RuntimeError as exc:
                    log.error("%s", exc)
                    return 3
            log.info("No videos eligible for processing; no approval needed.")
            return preview.exit_code()
        if cfg.require_approval and not args.yolo and not request_approval():
            log.info("Workflow cancelled; no processing was started.")
            return 1
        setup_logging(args.log_level, cfg.log_file_dir)

    pipe = Pipeline(cfg, dry_run=args.dry_run, only=args.participant, reprocess=args.reprocess,
                    manual_offset=args.manual_offset)
    if args.dry_run:
        summary = pipe.run()
    else:
        for folder in (cfg.raw_dir, cfg.synced_dir, cfg.logs_dir):
            folder.mkdir(parents=True, exist_ok=True)
        try:
            with RunLock(cfg.lock_file, cfg.lock_stale_hours):
                summary = pipe.run()
        except RuntimeError as exc:
            log.error("%s", exc)
            return 3
    if args.dry_run:
        videos_by_participant: dict[str, list[Path]] = {}
        for root in (cfg.inbox_dir, cfg.raw_dir):
            for pid, job in discover_jobs(cfg, root).items():
                videos_by_participant.setdefault(pid, []).extend(job.mom + job.child + job.both)
        unstable = [outcome for outcome in summary.outcomes if outcome.status == S.WAITING_FOR_STABILITY]
        log.info("Unstable videos (%d participant folder(s)):", len(unstable))
        for outcome in unstable:
            log.info("  %s: %s", outcome.pid, outcome.message)
            for video in videos_by_participant.get(outcome.pid, []):
                display_path = (video.relative_to(cfg.followup_root)
                                if video.is_relative_to(cfg.followup_root) else video)
                log.info("    %s", display_path.as_posix())
        if not unstable:
            log.info("  (none)")
    text = summary.render()
    for line in text.splitlines():
        log.info("%s", line)
    return summary.exit_code()


if __name__ == "__main__":
    sys.exit(main())
