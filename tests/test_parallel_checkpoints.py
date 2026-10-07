"""Pair concurrency and durable restart boundaries, using synthetic recordings only."""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from rg2019 import checkpoints, media
from rg2019.cli import main, parse_args
from rg2019.config import ConfigError, from_dict
from rg2019.pipeline import Outcome, Pipeline
from rg2019.state import StateStore
from rg2019.statuses import S

PID = "ID100392"


class Killed(BaseException):
    pass


@pytest.fixture(autouse=True)
def preserve_logging(monkeypatch):
    logger = logging.getLogger("rg2019")
    original = list(logger.handlers)
    monkeypatch.setattr(logger, "handlers", list(original))
    monkeypatch.setattr(logger, "level", logger.level)
    yield
    for handler in logger.handlers:
        if handler not in original:
            handler.close()


def state(cfg, pid=PID):
    return json.loads(StateStore(cfg).path(pid).read_text(encoding="utf-8"))


@pytest.mark.parametrize("workers", [0, -1, True, 1.5, "2", None])
def test_invalid_worker_settings(tmp_path, workers):
    with pytest.raises(ConfigError, match="positive integer"):
        from_dict({"followup_root": str(tmp_path), "max_parallel_pairs": workers})


def test_worker_default_and_cli_override(tmp_path, monkeypatch, capsys):
    assert from_dict({"followup_root": str(tmp_path)}).max_parallel_pairs == 2
    assert parse_args(["--workers", "3"]).workers == 3
    with pytest.raises(SystemExit):
        parse_args(["--workers", "0"])
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"followup_root": str(tmp_path), "require_approval": False}))
    (tmp_path / "00_INBOX").mkdir()
    monkeypatch.setattr(media, "check_tools", lambda cfg: {})
    assert main(["--config", str(cfg), "--workers", "3"]) == 0
    assert "Maximum parallel pairs: 3" in capsys.readouterr().out


def test_workers_overlap_and_are_bounded_with_ordered_failure_isolation(cfg, make_participant, monkeypatch):
    ids = [f"ID10000{i}" for i in range(1, 5)]
    for pid in ids:
        make_participant(pid)
    barrier = threading.Barrier(2)
    mutex = threading.Lock()
    active = 0
    maximum = 0

    def finish(self, job):
        nonlocal active, maximum
        with mutex:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait(timeout=10)
            if job.pid == ids[1]:
                raise ValueError("one pair failed")
            return Outcome(job.pid, S.SUCCESS, kind="new")
        finally:
            with mutex:
                active -= 1

    monkeypatch.setattr(Pipeline, "_finish_raw", finish)
    result = Pipeline(cfg).run()
    assert maximum == cfg.max_parallel_pairs == 2
    assert [o.pid for o in result.outcomes] == ids
    assert result.newly_completed == [ids[0], ids[2], ids[3]]
    assert result.failed == [ids[1]]
    assert state(cfg, ids[1])["status"] == S.INTERNAL_ERROR


def test_one_worker_is_sequential(cfg, make_participant, monkeypatch):
    cfg.max_parallel_pairs = 1
    ids = ["ID100001", "ID100002"]
    for pid in ids:
        make_participant(pid)
    calls = []

    def finish(self, job):
        calls.append(job.pid)
        return Outcome(job.pid, S.SUCCESS, kind="new")

    monkeypatch.setattr(Pipeline, "_finish_raw", finish)
    assert Pipeline(cfg).run().newly_completed == ids
    assert calls == ids


def test_shared_folder_checks_happen_before_any_moves(cfg, video_cache):
    folder = cfg.inbox_dir / "batch"
    folder.mkdir()
    cfg.require_ready_marker = True
    cfg.stability_recheck_seconds = 1
    ids = ["ID100001", "ID100002"]
    source = video_cache(0, 5)
    for pid in ids:
        for role in ("mom", "child"):
            shutil.copy2(source / f"{role}.mp4", folder / f"{pid}_{role}.mp4")
    (folder / cfg.ready_marker_name).write_text("ready")
    observations = []

    def observe(_):
        observations.append(sorted(p.name for p in folder.glob("*.mp4")))

    result = Pipeline(cfg, sleep_fn=observe).run()
    assert result.newly_completed == ids
    assert len(observations) == 2 and all(len(files) == 4 for files in observations)
    assert not folder.exists()
    for pid in ids:
        assert (cfg.synced_dir / "batch" / f"{pid}_mom_synced.mp4").exists()


def test_audio_checkpoint_reused_after_failed_second_extraction(cfg, make_participant, monkeypatch):
    make_participant(PID)
    extract = media.extract_audio
    calls = []

    def interrupted(c, source, dest, rate):
        calls.append(dest.name)
        if dest.name.startswith("child"):
            raise media.MediaError("disk temporarily unavailable")
        return extract(c, source, dest, rate)

    monkeypatch.setattr(media, "extract_audio", interrupted)
    assert Pipeline(cfg).run().failed == [PID]
    assert set(state(cfg)["audio_checkpoints"]) == {"mom"}
    calls.clear()

    def resumed(c, source, dest, rate):
        calls.append(dest.name)
        assert dest.name.startswith("child"), "completed mother audio must be reused"
        return extract(c, source, dest, rate)

    monkeypatch.setattr(media, "extract_audio", resumed)
    assert Pipeline(cfg).run().newly_completed == [PID]
    assert calls == ["child.pcm.partial"]
    assert not checkpoints.workspace(cfg, PID).exists()


@pytest.mark.parametrize("after_rename", [False, True])
def test_audio_publication_kill_reuses_valid_extraction(cfg, monkeypatch, after_rename):
    st = {"participant_id": PID, "sources": {"mom": {"sha256": "synthetic-source"}}}
    persisted = None
    renamed = False
    replace = checkpoints.os.replace

    def extract(c, source, dest, rate):
        dest.write_bytes(np.arange(100, dtype="<i2").tobytes())
        return np.memmap(dest, dtype="<i2", mode="r")

    def save(current):
        nonlocal persisted
        if after_rename and renamed:
            raise Killed()
        persisted = json.loads(json.dumps(current))

    def move(source, dest):
        nonlocal renamed
        if not after_rename:
            raise Killed()
        replace(source, dest)
        renamed = True

    monkeypatch.setattr(media, "extract_audio", extract)
    monkeypatch.setattr(checkpoints.os, "replace", move)
    with pytest.raises(Killed):
        checkpoints.audio(cfg, st, "mom", Path("synthetic"), save)
    assert persisted["audio_checkpoints"]["mom"]["pending"]
    monkeypatch.setattr(checkpoints.os, "replace", replace)
    monkeypatch.setattr(media, "extract_audio", lambda *a: pytest.fail("completed PCM must be reused"))
    samples = checkpoints.audio(cfg, persisted, "mom", Path("synthetic"), lambda st: None)
    assert samples.size == 100
    samples._mmap.close()
    assert "pending" not in persisted["audio_checkpoints"]["mom"]


def test_audio_cache_tampering_and_setting_changes_are_rebuilt(cfg, monkeypatch):
    st = {"participant_id": PID, "sources": {"mom": {"sha256": "synthetic-source"}}}
    calls = []

    def extract(c, source, dest, rate):
        calls.append(rate)
        dest.write_bytes(np.arange(100, dtype="<i2").tobytes())
        return np.memmap(dest, dtype="<i2", mode="r")

    monkeypatch.setattr(media, "extract_audio", extract)
    for action in ("initial", "reuse", "tamper", "rate"):
        if action == "tamper":
            (checkpoints.workspace(cfg, PID) / "mom.pcm").write_bytes(b"\0\0" * 100)
        if action == "rate":
            cfg.sync.sample_rate *= 2
        samples = checkpoints.audio(cfg, st, "mom", Path("synthetic"), lambda st: None)
        samples._mmap.close()
    assert calls == [8000, 8000, 16000]


@pytest.mark.parametrize("boundary", ["pending-save", "final-rename", "side-by-side", "success"])
def test_kill_publication_boundaries_resume_without_reencoding(cfg, make_participant, monkeypatch, boundary):
    cfg.create_side_by_side = boundary == "side-by-side"
    make_participant(PID)
    save = Pipeline._save
    rename = checkpoints.os.rename
    killed = False

    def interrupted_save(self, st):
        nonlocal killed
        files = st["outputs"]["files"]
        trigger = ((boundary == "final-rename" and "mom" in files)
                   or (boundary == "side-by-side" and "side_by_side" in files)
                   or (boundary == "success" and st.get("sync_completed_at")))
        if not killed and trigger:
            killed = True
            raise Killed()
        return save(self, st)

    def interrupted_rename(src, dest):
        nonlocal killed
        if not killed and boundary == "pending-save" and str(src).endswith(".mp4.partial"):
            killed = True
            raise Killed()
        return rename(src, dest)

    monkeypatch.setattr(Pipeline, "_save", interrupted_save)
    monkeypatch.setattr(checkpoints.os, "rename", interrupted_rename)
    with pytest.raises(Killed):
        Pipeline(cfg).run()
    assert killed
    monkeypatch.setattr(Pipeline, "_save", save)
    monkeypatch.setattr(checkpoints.os, "rename", rename)
    encode = media.encode
    side = media.side_by_side

    def resumed_encode(c, source, dest, trim, info):
        assert "mom" not in dest.name, "mother output must be recovered/reused"
        assert boundary not in ("side-by-side", "success")
        return encode(c, source, dest, trim, info)

    monkeypatch.setattr(media, "encode", resumed_encode)
    monkeypatch.setattr(media, "extract_audio", lambda *a: pytest.fail("saved offset must be reused"))
    if boundary == "side-by-side":
        monkeypatch.setattr(media, "side_by_side", lambda *a: pytest.fail("side-by-side must be recovered"))
    result = Pipeline(cfg).run()
    assert result.newly_completed == [PID]
    assert "pending" not in state(cfg)["outputs"]
    assert state(cfg)["attempts"] == 0
    assert Pipeline(cfg).run().skipped_completed == [PID]
    monkeypatch.setattr(media, "side_by_side", side)


def test_tampered_pending_output_is_not_adopted(cfg, make_participant, monkeypatch):
    make_participant(PID)
    save = Pipeline._save

    def killed(self, st):
        if "mom" in st["outputs"]["files"]:
            raise Killed()
        return save(self, st)

    monkeypatch.setattr(Pipeline, "_save", killed)
    with pytest.raises(Killed):
        Pipeline(cfg).run()
    monkeypatch.setattr(Pipeline, "_save", save)
    final = cfg.synced_dir / PID / f"{PID}_mom_synced.mp4"
    data = bytearray(final.read_bytes())
    data[-1] ^= 1
    final.write_bytes(data)
    result = Pipeline(cfg).run()
    assert result.manual_review == [PID]
    assert state(cfg)["status"] == S.OUTPUT_CONFLICT
    assert final.read_bytes() == data


def test_reprocess_archive_interruption_resumes_without_reprocess_flag(cfg, make_participant, monkeypatch):
    make_participant(PID)
    assert Pipeline(cfg).run().newly_completed == [PID]
    rename = os.rename
    moves = 0

    def killed(src, dest):
        nonlocal moves
        if "_superseded_" in str(dest):
            moves += 1
            if moves == 2:
                raise Killed()
        return rename(src, dest)

    monkeypatch.setattr(os, "rename", killed)
    with pytest.raises(Killed):
        Pipeline(cfg, reprocess=[PID]).run()
    assert state(cfg)["reprocess_pending"]
    monkeypatch.setattr(os, "rename", rename)
    assert Pipeline(cfg).run().newly_completed == [PID]
    assert "reprocess_pending" not in state(cfg)
    assert len(list((cfg.synced_dir / PID).glob("_superseded_*/*.mp4"))) == 2


@pytest.mark.parametrize("shared", [False, True])
def test_kill_after_source_move_before_checkpoint_is_reconciled(cfg, make_participant, monkeypatch, shared):
    folder = make_participant(PID)
    if shared:
        target = cfg.inbox_dir / "batch"
        folder.rename(target)
        folder = target
    else:
        (folder / "notes.txt").write_text("original notes")
    originals = {p.name: p.read_bytes() for p in folder.iterdir()}
    rename = os.rename
    killed = False

    def interrupted(source, dest):
        nonlocal killed
        rename(source, dest)
        if not killed and str(dest).startswith(str(cfg.raw_dir)):
            killed = True
            raise Killed()

    monkeypatch.setattr(os, "rename", interrupted)
    with pytest.raises(Killed):
        Pipeline(cfg).run()
    assert state(cfg)["stage"] == "PROMOTING"
    assert state(cfg)["attempts"] == 0
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(Pipeline, "_finish_raw", lambda self, job: Outcome(job.pid, S.SUCCESS, kind="new"))
    assert Pipeline(cfg).run().newly_completed == [PID]
    raw = cfg.raw_dir / ("batch" if shared else PID)
    assert {p.name: p.read_bytes() for p in raw.iterdir()} == originals


def test_no_work_cli_rebuilds_csv_from_completed_checkpoints(cfg, make_participant, tmp_path, monkeypatch):
    make_participant(PID)
    assert Pipeline(cfg).run().newly_completed == [PID]
    cfg.status_csv.unlink()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"followup_root": str(cfg.followup_root),
                                 "create_side_by_side": False, "stability_recheck_seconds": 0}))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("completed run must not ask approval"))
    assert main(["--config", str(config)]) == 0
    assert cfg.status_csv.exists()
    assert PID in cfg.status_csv.read_text()


def test_real_process_kill_after_final_rename_and_normal_cli_restart(cfg, make_participant, tmp_path):
    make_participant(PID)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "followup_root": str(cfg.followup_root), "require_approval": False,
        "stability_recheck_seconds": 0, "create_side_by_side": False,
        "sync": {"max_lag_seconds": 30, "window_seconds": 10, "fine_search_seconds": 1},
        "encode": {"preset": "ultrafast", "crf": 30},
    }))
    ready = tmp_path / "ready"
    script = """
import sys, time
from pathlib import Path
from rg2019.pipeline import Pipeline
from rg2019.cli import main
save = Pipeline._save
def stop(self, st):
    if "mom" in st["outputs"]["files"]:
        Path(sys.argv[2]).write_text("ready")
        time.sleep(120)
    return save(self, st)
Pipeline._save = stop
sys.exit(main(["--config", sys.argv[1]]))
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(config), str(ready)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), process.stderr.read().decode() if process.poll() is not None else "timeout"
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()
    assert cfg.lock_file.exists()
    assert state(cfg)["outputs"]["pending"]["key"] == "mom"
    before = (cfg.synced_dir / PID / f"{PID}_mom_synced.mp4").stat().st_mtime_ns
    assert main(["--config", str(config)]) == 0
    assert not cfg.lock_file.exists()
    assert (cfg.synced_dir / PID / f"{PID}_mom_synced.mp4").stat().st_mtime_ns == before
