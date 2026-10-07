"""Durable per-participant work and recoverable output publication."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

import numpy as np

from . import media
from .config import Config
from .media import MediaInfo, sha256_file

log = logging.getLogger("rg2019")
Save = Callable[[dict], None]


def workspace(cfg: Config, pid: str) -> Path:
    return cfg.logs_dir / "work" / pid


def audio(cfg: Config, st: dict, role: str, source: Path, save: Save) -> np.memmap:
    pid = st["participant_id"]
    folder = workspace(cfg, pid)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{role}.pcm"
    partial = folder / f"{role}.pcm.partial"
    identity = {"source": st["sources"][role], "rate": cfg.sync.sample_rate,
                "format": "mono-s16le-v1"}
    records = st.setdefault("audio_checkpoints", {})
    record = records.get(role)
    if record and record["identity"] == identity:
        candidate = partial if record.get("pending") and not final.exists() else final
        if (candidate.is_file() and candidate.stat().st_size == record["size"]
                and sha256_file(candidate) == record["sha256"]):
            if candidate == partial:
                os.replace(partial, final)
            if record.pop("pending", None):
                save(st)
            log.info("%s: reusing %s audio checkpoint", pid, role)
            return np.memmap(final, dtype="<i2", mode="r")
    if record or final.exists() or partial.exists():
        log.info("%s: rebuilding incomplete or incompatible %s audio checkpoint", pid, role)
    records.pop(role, None)
    save(st)
    partial.unlink(missing_ok=True)
    log.info("%s: extracting %d Hz mono audio from %s", pid, cfg.sync.sample_rate, source.name)
    samples = media.extract_audio(cfg, source, partial, cfg.sync.sample_rate)
    samples._mmap.close()
    size = partial.stat().st_size
    if size < 2 or size % 2:
        raise media.MediaError(f"{role} audio checkpoint has invalid PCM size: {size}")
    digest = sha256_file(partial)
    records[role] = {"identity": identity, "size": size, "sha256": digest, "pending": True}
    save(st)
    os.replace(partial, final)
    records[role].pop("pending")
    save(st)
    return np.memmap(final, dtype="<i2", mode="r")


def publish_output(st: dict, partial: Path, final: Path, key: str, info: MediaInfo,
                   method: str, save: Save) -> None:
    record = {"name": final.name, "size": partial.stat().st_size,
              "duration": info.duration, "method": method}
    outputs = st.setdefault("outputs", {"files": {}})
    outputs["pending"] = {"key": key, "record": record, "sha256": sha256_file(partial)}
    save(st)
    if final.exists():
        raise media.MediaError(f"{final.name}: output appeared before publication; refusing to overwrite")
    os.rename(partial, final)
    outputs["files"][key] = record
    outputs.pop("pending")
    save(st)


def recover_outputs(st: dict, folder: Path, save: Save) -> str | None:
    outputs = st.setdefault("outputs", {"files": {}})
    pending = outputs.get("pending")
    if not pending:
        return None
    record = pending["record"]
    name = record["name"]
    if Path(name).name != name:
        return "invalid checkpoint output filename"
    final = folder / name
    partial = final.with_name(final.name + ".partial")
    path = final if final.exists() else partial
    if not path.exists():
        log.warning("%s: pending output %s is missing; restarting that output", st["participant_id"], name)
        outputs.pop("pending")
        save(st)
        return None
    if path.stat().st_size != record["size"] or sha256_file(path) != pending["sha256"]:
        return f"{path.name} differs from the validated publication checkpoint; refusing to overwrite"
    if path == partial:
        os.rename(partial, final)
    else:
        partial.unlink(missing_ok=True)
    outputs["files"][pending["key"]] = record
    outputs.pop("pending")
    save(st)
    log.info("%s: recovered committed output %s", st["participant_id"], name)
    return None


def cleanup(cfg: Config, pid: str) -> None:
    folder = workspace(cfg, pid)
    if not folder.exists():
        return
    try:
        for role in ("mom", "child"):
            for name in (f"{role}.pcm", f"{role}.pcm.partial"):
                (folder / name).unlink(missing_ok=True)
        if not any(folder.iterdir()):
            folder.rmdir()
    except OSError as exc:
        log.warning("%s: could not clean checkpoint workspace %s: %s", pid, folder, exc)
