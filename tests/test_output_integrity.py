"""Committed-output integrity and atomic no-clobber publication regressions."""
import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from rg2019 import checkpoints, media
from rg2019.pipeline import Pipeline
from rg2019.state import new_state
from rg2019.statuses import S

PID = "ID100392"


def output_state(cfg):
    folder = cfg.synced_dir / PID
    folder.mkdir()
    st = new_state(PID, "2026-10-07")
    st.update({"status": S.SUCCESS, "stage": "SYNCED", "sync_completed_at": "2026-10-07",
               "sources": {"mom": {}, "child": {}}, "sync": {"offset_seconds": 5}})
    for key in ("mom", "child", "side_by_side"):
        suffix = key if key == "side_by_side" else f"{key}_synced"
        name = f"{PID}_{suffix}.mp4"
        data = key.encode() * 20
        (folder / name).write_bytes(data)
        st["outputs"]["files"][key] = {"name": name, "size": len(data), "duration": 60,
                                      "method": "reencode", "sha256": hashlib.sha256(data).hexdigest()}
    return folder, st


@pytest.mark.parametrize("key", ["mom", "child", "side_by_side"])
@pytest.mark.parametrize("dry", [False, True])
def test_same_size_edits_are_conflicts_on_completed_reuse(cfg, monkeypatch, key, dry):
    cfg.create_side_by_side = True
    folder, st = output_state(cfg)
    pipe = Pipeline(cfg, dry_run=dry)
    assert pipe._outputs_valid(st, folder)
    changed = folder / st["outputs"]["files"][key]["name"]
    original = changed.stat()
    data = bytearray(changed.read_bytes())
    data[-1] ^= 1
    changed.write_bytes(data)
    os.utime(changed, ns=(original.st_atime_ns, original.st_mtime_ns))
    monkeypatch.setattr(pipe, "_sources_intact", lambda *a: None)
    assert not pipe._outputs_valid(st, folder)
    result = pipe._from_raw(PID, st, cfg.raw_dir / PID, folder)
    assert result.status == S.OUTPUT_CONFLICT
    assert changed.read_bytes() == data


@pytest.mark.parametrize("key", ["mom", "child", "side_by_side"])
def test_same_size_edits_are_conflicts_on_incomplete_output_reuse(cfg, monkeypatch, key):
    cfg.create_side_by_side = True
    folder, st = output_state(cfg)
    st["sync_completed_at"] = None
    changed = folder / st["outputs"]["files"][key]["name"]
    changed.write_bytes(b"\0" * changed.stat().st_size)
    monkeypatch.setattr(media, "encode", lambda *a: pytest.fail("existing output must not be overwritten"))
    monkeypatch.setattr(media, "side_by_side", lambda *a: pytest.fail("existing output must not be overwritten"))
    sources = {role: (Path("unused"), None) for role in ("mom", "child")}
    assert Pipeline(cfg)._encode_all(PID, st, sources, folder, 5).status == S.OUTPUT_CONFLICT
    assert changed.read_bytes() == b"\0" * changed.stat().st_size


def test_legacy_output_records_keep_size_only_validation(cfg, monkeypatch):
    folder, st = output_state(cfg)
    for record in st["outputs"]["files"].values():
        record.pop("sha256")
    monkeypatch.setattr(checkpoints, "sha256_file", lambda *a: pytest.fail("legacy states have no digest"))
    assert Pipeline(cfg)._outputs_valid(st, folder)


@pytest.mark.parametrize("recover", [False, True])
def test_committed_record_retains_digest(tmp_path, recover):
    partial = tmp_path / "output.mp4.partial"
    final = tmp_path / "output.mp4"
    data = b"validated synthetic output"
    partial.write_bytes(data)
    st = new_state(PID, "2026-10-07")
    digest = hashlib.sha256(data).hexdigest()
    if recover:
        st["outputs"]["pending"] = {
            "key": "mom", "record": {"name": final.name, "size": len(data), "duration": 60},
            "sha256": digest}
        assert checkpoints.recover_outputs(st, tmp_path, lambda st: None) is None
    else:
        info = media.MediaInfo(len(data), 60, "mp4", 0)
        checkpoints.publish_output(st, partial, final, "mom", info, "reencode", lambda st: None)
    assert st["outputs"]["files"]["mom"]["sha256"] == digest
    assert checkpoints.matches_output(final, st["outputs"]["files"]["mom"])
    assert not partial.exists()


@pytest.mark.parametrize("recover", [False, True])
def test_posix_destination_race_never_clobbers_and_reports_conflict(tmp_path, monkeypatch, recover):
    partial = tmp_path / "output.mp4.partial"
    final = tmp_path / "output.mp4"
    partial.write_bytes(b"validated")
    st = new_state(PID, "2026-10-07")
    link = os.link

    def competing_link(src, dest):
        final.write_bytes(b"unrelated file")
        link(src, dest)

    monkeypatch.setattr(checkpoints, "os", SimpleNamespace(name="posix", link=competing_link))
    if recover:
        st["outputs"]["pending"] = {
            "key": "mom", "record": {"name": final.name, "size": 9, "duration": 60},
            "sha256": hashlib.sha256(b"validated").hexdigest()}
        assert "refusing to overwrite" in checkpoints.recover_outputs(st, tmp_path, lambda st: None)
    else:
        info = media.MediaInfo(9, 60, "mp4", 0)
        with pytest.raises(checkpoints.OutputConflict, match="refusing to overwrite"):
            checkpoints.publish_output(st, partial, final, "mom", info, "reencode", lambda st: None)
    assert final.read_bytes() == b"unrelated file"
    assert partial.read_bytes() == b"validated"
    assert "pending" in st["outputs"] and not st["outputs"]["files"]


def test_publication_conflict_uses_manual_review_without_consuming_retry(cfg, monkeypatch):
    folder = cfg.synced_dir / PID
    folder.mkdir()
    st = new_state(PID, "2026-10-07")
    info = media.MediaInfo(9, 60, "mp4", 0, video_codec="h264", audio_codec="aac")
    def encode(c, src, dest, *a):
        dest.write_bytes(b"validated")
        return "reencode"

    monkeypatch.setattr(media, "encode", encode)
    monkeypatch.setattr(media, "probe", lambda *a: info)

    def appeared(partial, final):
        final.write_bytes(b"unrelated file")
        raise checkpoints.OutputConflict("output appeared; refusing to overwrite")

    monkeypatch.setattr(checkpoints, "publish_noreplace", appeared)
    sources = {role: (Path("unused"), info) for role in ("mom", "child")}
    result = Pipeline(cfg)._encode_all(PID, st, sources, folder, 0)
    assert result.status == S.OUTPUT_CONFLICT
    assert st["attempts"] == 0


@pytest.mark.parametrize("boundary", ["before-unlink", "before-state-save"])
def test_posix_link_publication_is_recoverable_after_interruption(tmp_path, monkeypatch, boundary):
    partial = tmp_path / "output.mp4.partial"
    final = tmp_path / "output.mp4"
    partial.write_bytes(b"validated")
    st = new_state(PID, "2026-10-07")
    persisted = None
    link = os.link

    class Killed(BaseException):
        pass

    def interrupted_link(src, dest):
        link(src, dest)
        if boundary == "before-unlink":
            raise Killed()

    def save(current):
        nonlocal persisted
        if boundary == "before-state-save" and current["outputs"]["files"]:
            raise Killed()
        persisted = copy.deepcopy(current)

    monkeypatch.setattr(checkpoints, "os", SimpleNamespace(name="posix", link=interrupted_link))
    with pytest.raises(Killed):
        checkpoints.publish_output(st, partial, final, "mom", media.MediaInfo(9, 60, "mp4", 0),
                                   "reencode", save)
    assert checkpoints.recover_outputs(persisted, tmp_path, lambda st: None) is None
    assert final.read_bytes() == b"validated" and not partial.exists()
    assert persisted["outputs"]["files"]["mom"]["sha256"] == hashlib.sha256(b"validated").hexdigest()


def test_native_publication_does_not_replace_existing_destination(tmp_path):
    partial = tmp_path / "output.mp4.partial"
    final = tmp_path / "output.mp4"
    partial.write_bytes(b"validated")
    final.write_bytes(b"unrelated")
    with pytest.raises(checkpoints.OutputConflict, match="refusing to overwrite"):
        checkpoints.publish_noreplace(partial, final)
    assert partial.read_bytes() == b"validated"
    assert final.read_bytes() == b"unrelated"


def test_unsupported_posix_links_fail_explicitly_without_rename_fallback(tmp_path, monkeypatch):
    partial = tmp_path / "output.mp4.partial"
    final = tmp_path / "output.mp4"
    partial.write_bytes(b"validated")

    def unsupported(*a):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(checkpoints, "os", SimpleNamespace(name="posix", link=unsupported))
    with pytest.raises(OSError, match="hard links unsupported"):
        checkpoints.publish_noreplace(partial, final)
    assert not final.exists()
    assert partial.read_bytes() == b"validated"
