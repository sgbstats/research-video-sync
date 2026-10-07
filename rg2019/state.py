"""Per-participant state files, the master CSV and the run lock.

State lives ONLY under 99_LOGS_QC (never in 01_RAW).  All writes are atomic
(write temp file in the same folder, then os.replace)."""
from __future__ import annotations

import csv
import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .discovery import valid_participant_id
from .processes import process_identity

SCHEMA = 1
HISTORY_LIMIT = 30
_log = logging.getLogger(__name__)

CSV_COLUMNS = ["participant_id", "status", "mom_source", "child_source", "mom_sha256", "child_sha256",
               "mom_duration", "child_duration", "offset_seconds", "sync_confidence",
               "raw_promoted_at", "sync_completed_at", "last_checked_at", "error_message"]


def atomic_write_text(path: Path, text: str) -> None:
    """Publish fsynced text atomically, briefly retrying Windows sharing/access violations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    retry_delays = (0.05, 0.1, 0.2, 0.4, 0.8)
    for attempt in range(len(retry_delays) + 1):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            if (os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33)
                    or attempt == len(retry_delays)):
                raise
            delay = retry_delays[attempt]
            _log.warning("Atomic publication of %s blocked; retry %d/%d in %.2fs: %s",
                         path, attempt + 1, len(retry_delays), delay, exc)
            time.sleep(delay)


def new_state(pid: str, now: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "participant_id": pid, "status": None, "message": "", "stage": "NEW",
            "first_seen_at": now, "last_checked_at": now, "raw_promoted_at": None,
            "sync_completed_at": None, "attempts": 0, "sources": {}, "promotion": {}, "sync": {},
            "outputs": {"files": {}}, "stability": {}, "history": []}


class StateStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.state_dir
        self.csv = cfg.status_csv

    def path(self, pid: str) -> Path:
        return self.dir / f"{pid}.json"

    def load(self, pid: str, now: str) -> dict[str, Any]:
        p = self.path(pid)
        if not p.exists():
            return new_state(pid, now)
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            if st.get("participant_id") != pid:
                raise ValueError("participant_id mismatch")
            return st
        except (OSError, ValueError) as exc:
            bad = p.with_name(p.name + f".corrupt-{now.replace(':', '')}")
            os.replace(p, bad)
            st = new_state(pid, now)
            st["history"].append({"at": now, "event": f"corrupt state file moved to {bad.name}: {exc}"})
            return st

    def save(self, st: dict[str, Any]) -> None:
        st["history"] = st.get("history", [])[-HISTORY_LIMIT:]
        atomic_write_text(self.path(st["participant_id"]),
                          json.dumps(st, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def all_ids(self) -> list[str]:
        """Participant ids that have a state file; file names that are not valid participant ids
        (e.g. a stray notes.json) are ignored."""
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json") if valid_participant_id(self.cfg, p.stem))

    def _is_pipeline_state(self, pid: str, st: object) -> bool:
        """A file only counts as a state record if it is a JSON object written by this pipeline
        (schema marker), names the participant, and that participant id equals the file name."""
        return (isinstance(st, dict) and st.get("schema") == SCHEMA
                and st.get("participant_id") == pid and valid_participant_id(self.cfg, pid))

    def rewrite_csv(self) -> None:
        """Regenerate the human-readable master CSV from the state files (one row per participant,
        so running the pipeline many times can never create duplicate rows)."""
        rows = []
        for pid in self.all_ids():
            try:
                st = json.loads(self.path(pid).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not self._is_pipeline_state(pid, st):
                continue
            src, sync = st.get("sources", {}), st.get("sync", {})
            mom, child = src.get("mom", {}), src.get("child", {})
            rows.append({
                "participant_id": pid, "status": st.get("status"),
                "mom_source": mom.get("name", ""), "child_source": child.get("name", ""),
                "mom_sha256": mom.get("sha256", ""), "child_sha256": child.get("sha256", ""),
                "mom_duration": _fmt(mom.get("media", {}).get("duration")),
                "child_duration": _fmt(child.get("media", {}).get("duration")),
                "offset_seconds": _fmt(sync.get("offset_seconds"), 4),
                "sync_confidence": _fmt(sync.get("confidence"), 3),
                "raw_promoted_at": st.get("raw_promoted_at") or "",
                "sync_completed_at": st.get("sync_completed_at") or "",
                "last_checked_at": st.get("last_checked_at") or "",
                "error_message": st.get("message") if st.get("status") != "SUCCESS" else ""})
        import io
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)
        atomic_write_text(self.csv, buf.getvalue())


def _fmt(v, nd=2):
    return "" if v is None else (f"{v:.{nd}f}" if isinstance(v, (int, float)) else str(v))


class RunLock:
    """Prevents two daily runs from overlapping (e.g. a long run + the next scheduled trigger).

    Structured local owners are recovered immediately only after verifying death or PID reuse.
    Live, foreign-host and unverifiable structured owners are never stolen. Legacy records retain
    the age-based recovery policy. A persistent OS-locked guard serializes reclaim, heartbeat and
    release; its existence does not indicate an active run and it must not be removed."""

    def __init__(self, path: Path, stale_hours: float):
        self.path, self.stale = path, timedelta(hours=stale_hours)
        self.interval = max(0.2, min(60.0, stale_hours * 3600 / 4))
        self.held = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._token = uuid.uuid4().hex
        self._host = socket.gethostname().casefold()

    @contextmanager
    def _guard(self):
        # Lock a separate, stable inode: locking the replaceable run lock itself would let a
        # contender lock an old inode and delete a new owner's record during reclaim.
        guard = self.path.with_name(self.path.name + ".guard")
        with open(guard, "a+b") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fh.seek(0)
                if os.name == "nt":
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _record(self) -> dict | None:
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if (isinstance(record, dict) and record.get("schema") == "rg2019-run-lock-1"
                    and isinstance(record.get("pid"), int) and not isinstance(record["pid"], bool)
                    and record["pid"] > 0 and isinstance(record.get("host"), str)
                    and isinstance(record.get("token"), str) and len(record["token"]) == 32
                    and (record.get("identity") is None or isinstance(record["identity"], str))):
                return record
        except (OSError, ValueError):
            pass
        return None

    def _owns_record(self) -> bool:
        record = self._record()
        return record is not None and record["token"] == self._token

    def _reclaimable(self, age: timedelta) -> bool:
        record = self._record()
        if record is None:
            return age >= self.stale
        if record["host"].casefold() != self._host:
            return False
        status, identity = process_identity(record["pid"])
        return status == "dead" or (status == "alive" and identity is not None
                                    and record["identity"] is not None
                                    and identity != record["identity"])

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                with self._guard():
                    if self._owns_record():
                        os.utime(self.path)
                    else:
                        return
            except OSError:
                pass

    def acquire(self) -> None:
        if self.held:
            raise RuntimeError(f"lock is already held: {self.path}")
        self._token = uuid.uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._guard():
            for _ in range(2):
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    try:
                        age = datetime.now() - datetime.fromtimestamp(self.path.stat().st_mtime)
                    except FileNotFoundError:
                        continue
                    if not self._reclaimable(age):
                        raise RuntimeError(f"another pipeline run appears to be active (lock {self.path}, "
                                           f"last heartbeat {age} ago); owner is not verified abandoned")
                    self.path.unlink()
                    continue
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        record = {"schema": "rg2019-run-lock-1", "host": self._host,
                                  "pid": os.getpid(), "owner": f"pid={os.getpid()}",
                                  "identity": process_identity(os.getpid())[1], "token": self._token,
                                  "started": datetime.now().isoformat(timespec="seconds")}
                        fh.write(json.dumps(record) + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                except BaseException:
                    self.path.unlink(missing_ok=True)
                    raise
                self.held = True
                break
            else:
                raise RuntimeError(f"could not acquire lock {self.path}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._heartbeat, name="rg2019-lock-heartbeat", daemon=True)
        self._thread.start()

    def release(self) -> None:
        if self.held:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=5)
                self._thread = None
            with self._guard():
                if self._owns_record():
                    self.path.unlink(missing_ok=True)
            self.held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
