"""End-to-end pipeline tests on SYNTHETIC videos only (tests 3,4,5,6,11 + dry-run, stability, ...)."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

import pipeline_rg2019 as cli
from helpers import make_video, recording, wall_signal
from rg2019 import config as cfgmod, media, pipeline as pl, syncest
from rg2019.config import SyncParams
from rg2019.pipeline import Outcome, Pipeline, Summary
from rg2019.state import StateStore
from rg2019.statuses import S


def run(cfg, **kw):
    return Pipeline(cfg, **kw).run()


def state(cfg, pid):
    return json.loads(StateStore(cfg).path(pid).read_text(encoding="utf-8"))


def tree(root: Path):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def audio_of(cfg, path, tmp_path):
    return media.extract_audio(cfg, path, tmp_path / (path.stem + ".pcm"), 8000)


# ====================================================================== happy path & sign
@pytest.mark.parametrize("ms,cs,trimmed", [(0, 5, "mom"), (0, 20, "mom"), (5, 0, "child"),
                                           (20, 0, "child"), (0, 0, "none")])
def test_end_to_end_known_offsets_trim_the_correct_video(cfg, make_participant, tmp_path, ms, cs, trimmed):
    pid = "ID100392"
    make_participant(pid, ms, cs)
    orig = {n: sha(cfg.inbox_dir / pid / n) for n in (f"{pid}_mom.mp4", f"{pid}_child.mp4")}
    s = run(cfg)
    assert s.newly_completed == [pid], s.render()

    out = cfg.synced_dir / pid
    mom, child = out / f"{pid}_mom_synced.mp4", out / f"{pid}_child_synced.mp4"
    assert mom.is_file() and child.is_file()
    assert not (out / f"{pid}_side_by_side.mp4").exists()          # default: no side-by-side
    assert sorted(p.name for p in out.iterdir()) == sorted([mom.name, child.name])   # no partials left

    st = state(cfg, pid)
    assert st["sync"]["offset_seconds"] == pytest.approx(cs - ms, abs=0.02)
    dm, dc = media.probe(cfg, mom).duration, media.probe(cfg, child).duration
    if trimmed == "mom":
        assert dm == pytest.approx(60 - (cs - ms), abs=0.3) and dc == pytest.approx(60, abs=0.3)
    elif trimmed == "child":
        assert dc == pytest.approx(60 - (ms - cs), abs=0.3) and dm == pytest.approx(60, abs=0.3)
    else:
        assert dm == pytest.approx(60, abs=0.3) and dc == pytest.approx(60, abs=0.3)
    # the camera that needed no trim was stream-copied (no re-encode), the other one re-encoded
    methods = {r: st["outputs"]["files"][r]["method"] for r in ("mom", "child")}
    assert methods == {"mom": "reencode" if trimmed == "mom" else "copy",
                       "child": "reencode" if trimmed == "child" else "copy"}

    # the two OUTPUTS begin at the same real-world event: re-estimate on the outputs => ~0 offset
    prm = SyncParams(max_lag_seconds=5, window_seconds=8, min_overlap_seconds=5)
    est = syncest.estimate_offset(audio_of(cfg, mom, tmp_path), audio_of(cfg, child, tmp_path), prm)
    assert est.ok and abs(est.offset_seconds) < 0.05, est.reason

    # RAW is the untouched original, holds nothing but the two source files, INBOX folder is gone
    raw = cfg.raw_dir / pid
    assert {p.name: sha(p) for p in raw.iterdir()} == orig
    assert not (cfg.inbox_dir / pid).exists()
    assert not list(cfg.raw_dir.rglob(".sync_done")) and not list(cfg.raw_dir.rglob("*.json"))


def test_state_lives_in_logs_qc_and_csv_has_one_row(cfg, make_participant):
    make_participant("ID100392")
    run(cfg)
    st = state(cfg, "ID100392")
    assert st["status"] == S.SUCCESS and st["raw_promoted_at"] and st["sync_completed_at"]
    assert st["sources"]["mom"]["sha256"] == sha(cfg.raw_dir / "ID100392" / "ID100392_mom.mp4")
    rows = list(csv.DictReader(cfg.status_csv.open(encoding="utf-8", newline="")))
    assert len(rows) == 1
    r = rows[0]
    assert list(r) == ["participant_id", "status", "mom_source", "child_source", "mom_sha256", "child_sha256",
                       "mom_duration", "child_duration", "offset_seconds", "sync_confidence", "raw_promoted_at",
                       "sync_completed_at", "last_checked_at", "error_message"]
    assert r["status"] == "SUCCESS" and float(r["offset_seconds"]) == pytest.approx(5.0, abs=0.02)
    assert float(r["mom_duration"]) == pytest.approx(60, abs=0.5) and len(r["mom_sha256"]) == 64


def test_side_by_side_is_optional(cfg, make_participant):
    cfg.create_side_by_side = True
    make_participant("ID100392")
    assert run(cfg).newly_completed == ["ID100392"]
    out = cfg.synced_dir / "ID100392"
    sbs = out / "ID100392_side_by_side.mp4"
    assert sbs.is_file()
    mom_info, sbs_info = media.probe(cfg, out / "ID100392_mom_synced.mp4"), media.probe(cfg, sbs)
    assert sbs_info.has_video and sbs_info.has_audio and sbs_info.height == 720
    assert sbs_info.width > 1.5 * mom_info.width * 720 / mom_info.height / 1.0 - 4     # two panels wide
    assert state(cfg, "ID100392")["outputs"]["files"]["side_by_side"]["size"] == sbs.stat().st_size


def test_video_description_names_pair_and_side_by_side(cfg, make_participant):
    cfg.video_description = "pilot_run"
    cfg.create_side_by_side = True
    make_participant("ID100392")
    assert run(cfg).newly_completed == ["ID100392"]
    names = {p.name for p in (cfg.synced_dir / "ID100392").iterdir()}
    assert names == {
        "ID100392_pilot_run_mom_synced.mp4",
        "ID100392_pilot_run_child_synced.mp4",
        "ID100392_pilot_run_side_by_side.mp4",
    }
    assert {record["name"] for record in state(cfg, "ID100392")["outputs"]["files"].values()} == names


def test_description_change_requires_reprocess(cfg, make_participant):
    make_participant("ID100392")
    assert run(cfg).newly_completed == ["ID100392"]
    cfg.video_description = "pilot"
    assert run(cfg).manual_review == ["ID100392"]
    assert state(cfg, "ID100392")["status"] == S.OUTPUT_CONFLICT
    assert run(cfg, reprocess=["ID100392"]).newly_completed == ["ID100392"]
    assert (cfg.synced_dir / "ID100392" / "ID100392_pilot_mom_synced.mp4").is_file()


# ====================================================================== discovery outcomes
def test_multiple_mom_files_stop_participant_ambiguous(cfg, make_participant):
    d = make_participant("ID100392")
    shutil.copy2(d / "ID100392_mom.mp4", d / "ID100392_mom_backup.mp4")
    s = run(cfg)
    assert s.manual_review == ["ID100392"]
    assert state(cfg, "ID100392")["status"] == S.AMBIGUOUS_FILES
    assert "ID100392_mom.mp4" in state(cfg, "ID100392")["message"] and "backup" in state(cfg, "ID100392")["message"]
    assert (cfg.inbox_dir / "ID100392").is_dir() and not (cfg.raw_dir / "ID100392").exists()
    assert not (cfg.synced_dir / "ID100392").exists()


def test_missing_child_stops_participant(cfg, make_participant):
    d = make_participant("ID100392")
    (d / "ID100392_child.mp4").unlink()
    s = run(cfg)
    assert s.manual_review == ["ID100392"] and state(cfg, "ID100392")["status"] == S.MISSING_FILES
    assert not (cfg.raw_dir / "ID100392").exists()


def test_invalid_participant_folder_name_is_reported_and_gets_no_state(cfg, make_participant):
    (cfg.inbox_dir / "not-an-id").mkdir()
    (cfg.inbox_dir / "@eaDir").mkdir()
    s = run(cfg)
    assert s.manual_review == ["not-an-id"] and not StateStore(cfg).all_ids()


def test_unreadable_video_is_invalid_video(cfg, make_participant):
    d = make_participant("ID100392")
    (d / "ID100392_child.mp4").write_bytes(b"this is not a video" * 100)
    s = run(cfg)
    assert state(cfg, "ID100392")["status"] == S.INVALID_VIDEO and s.manual_review == ["ID100392"]
    assert not (cfg.raw_dir / "ID100392").exists()          # never promoted


def test_video_without_audio_is_no_audio(cfg, make_participant):
    make_participant("ID100392", child_audio=False)
    run(cfg)
    st = state(cfg, "ID100392")
    assert st["status"] == S.NO_AUDIO and "child" in st["message"]
    assert not (cfg.raw_dir / "ID100392").exists()


# ====================================================================== RAW protection
def test_existing_conflicting_raw_stops_and_touches_nothing(cfg, make_participant):
    d = make_participant("ID100392")
    raw = cfg.raw_dir / "ID100392"
    raw.mkdir()
    (raw / "ID100392_mom.mp4").write_bytes(b"older original")
    before_raw, before_in = tree(cfg.raw_dir), tree(cfg.inbox_dir)
    s = run(cfg)
    assert s.manual_review == ["ID100392"]
    st = state(cfg, "ID100392")
    assert st["status"] == S.RAW_CONFLICT and "RAW" in st["message"]
    assert tree(cfg.raw_dir) == before_raw and tree(cfg.inbox_dir) == before_in
    assert not (cfg.synced_dir / "ID100392").exists()
    # and it keeps being reported (not silently forgotten) on later runs, still untouched
    assert run(cfg).manual_review == ["ID100392"] and tree(cfg.raw_dir) == before_raw


def test_raw_tampering_after_success_is_detected(cfg, make_participant):
    make_participant("ID100392")
    run(cfg)
    f = cfg.raw_dir / "ID100392" / "ID100392_mom.mp4"
    f.write_bytes(f.read_bytes() + b"tamper")
    s = run(cfg)
    assert s.manual_review == ["ID100392"] and state(cfg, "ID100392")["status"] == S.RAW_CONFLICT


# ====================================================================== idempotency / resume
def test_success_is_skipped_100_times_without_touching_anything(cfg, make_participant, monkeypatch):
    make_participant("ID100392")
    assert run(cfg).newly_completed == ["ID100392"]
    snap_out, snap_raw = tree(cfg.synced_dir), tree(cfg.raw_dir)
    monkeypatch.setattr(media, "encode", lambda *a, **k: pytest.fail("must not re-encode"))
    monkeypatch.setattr(media, "sha256_file", lambda *a, **k: pytest.fail("must not re-hash RAW"))
    monkeypatch.setattr(media, "extract_audio", lambda *a, **k: pytest.fail("must not re-extract audio"))
    for _ in range(100):
        s = run(cfg)
        assert s.skipped_completed == ["ID100392"] and not s.newly_completed
    assert tree(cfg.synced_dir) == snap_out and tree(cfg.raw_dir) == snap_raw
    rows = list(csv.DictReader(cfg.status_csv.open(encoding="utf-8", newline="")))
    assert len(rows) == 1
    assert len(state(cfg, "ID100392")["history"]) <= 5          # no endless SUCCESS records


def test_interrupted_encode_recovers_and_reuses_finished_output(cfg, make_participant, monkeypatch):
    make_participant("ID100392", 0, 5)                          # mom trimmed (re-encoded), child copied
    real = media.encode
    calls = []

    def flaky(c, src, dst, trim, info):
        calls.append(dst.name)
        if "child" in dst.name:
            dst.write_bytes(b"half written")                    # leaves a corrupt .partial behind
            raise media.MediaError("simulated crash while writing child")
        return real(c, src, dst, trim, info)
    monkeypatch.setattr(media, "encode", flaky)
    s = run(cfg)
    assert s.failed == ["ID100392"] and state(cfg, "ID100392")["status"] == S.ENCODE_FAILED
    out = cfg.synced_dir / "ID100392"
    assert (out / "ID100392_mom_synced.mp4").is_file()
    assert not (out / "ID100392_child_synced.mp4").exists()     # a failed partial never became a final file
    mom_mtime = (out / "ID100392_mom_synced.mp4").stat().st_mtime_ns

    monkeypatch.setattr(media, "encode", real)                  # "fixed" -> next daily run resumes
    monkeypatch.setattr(media, "extract_audio", lambda *a, **k: pytest.fail("offset must be reused"))
    s = run(cfg)
    assert s.newly_completed == ["ID100392"]
    assert (out / "ID100392_mom_synced.mp4").stat().st_mtime_ns == mom_mtime      # not re-encoded
    assert sorted(p.name for p in out.iterdir()) == ["ID100392_child_synced.mp4", "ID100392_mom_synced.mp4"]
    assert media.probe(cfg, out / "ID100392_child_synced.mp4").duration > 50


def test_stale_partial_file_from_killed_run_is_replaced(cfg, make_participant):
    make_participant("ID100392")
    out = cfg.synced_dir / "ID100392"
    out.mkdir(parents=True)
    (out / "ID100392_mom_synced.mp4.partial").write_bytes(b"killed mid-write")
    assert run(cfg).newly_completed == ["ID100392"]
    assert not list(out.glob("*.partial"))


def test_unrecorded_existing_output_is_never_overwritten(cfg, make_participant):
    make_participant("ID100392")
    out = cfg.synced_dir / "ID100392"
    out.mkdir(parents=True)
    (out / "ID100392_mom_synced.mp4").write_bytes(b"someone else's file")
    s = run(cfg)
    assert s.manual_review == ["ID100392"] and state(cfg, "ID100392")["status"] == S.OUTPUT_CONFLICT
    assert (out / "ID100392_mom_synced.mp4").read_bytes() == b"someone else's file"


def test_interrupted_promotion_resumes_without_loss(cfg, make_participant, monkeypatch):
    d = make_participant("ID100392")
    (d / "notes.txt").write_text("field notes")
    real_rename, n = os.rename, {"c": 0}

    def flaky_rename(a, b):
        n["c"] += 1
        if n["c"] == 2:
            raise OSError("simulated power cut / locked file")
        return real_rename(a, b)
    monkeypatch.setattr(pl.os, "rename", flaky_rename)
    s = run(cfg)
    assert s.failed == ["ID100392"] and state(cfg, "ID100392")["status"] == S.PROMOTE_FAILED
    assert (cfg.raw_dir / "ID100392").is_dir() and any((cfg.inbox_dir / "ID100392").iterdir())   # split, nothing lost

    monkeypatch.setattr(pl.os, "rename", real_rename)
    s = run(cfg)
    assert s.newly_completed == ["ID100392"], s.render()
    assert sorted(p.name for p in (cfg.raw_dir / "ID100392").iterdir()) == [
        "ID100392_child.mp4", "ID100392_mom.mp4", "notes.txt"]
    assert not (cfg.inbox_dir / "ID100392").exists()


def test_corrupt_state_file_is_quarantined_not_fatal(cfg, make_participant):
    make_participant("ID100392")
    run(cfg)
    StateStore(cfg).path("ID100392").write_text("{ not json", encoding="utf-8")
    s = run(cfg)
    # state was lost: RAW is re-verified/hashed, but the existing (now unrecorded) outputs are NOT overwritten
    assert s.manual_review == ["ID100392"] and not s.newly_completed
    assert state(cfg, "ID100392")["status"] == S.OUTPUT_CONFLICT
    assert list(cfg.state_dir.glob("*.corrupt-*"))
    assert len(list((cfg.synced_dir / "ID100392").glob("*.mp4"))) == 2


# ====================================================================== dry run
def test_dry_run_changes_nothing_and_says_what_would_happen(cfg, make_participant, capsys):
    make_participant("ID100392")
    make_participant("ID100534", 0, 20)
    (cfg.inbox_dir / "bad name").mkdir()
    before = {r: tree(getattr(cfg, r + "_dir")) for r in ("inbox", "raw", "synced", "logs")}
    dirs_before = sorted(str(p.relative_to(cfg.followup_root)) for p in cfg.followup_root.rglob("*"))
    s = run(cfg, dry_run=True)
    assert s.dry_run and s.would_process == ["ID100392", "ID100534"]
    text = s.render()
    assert "DRY RUN" in text and "MOVE 00_INBOX/ID100392 -> 01_RAW/ID100392" in text and "SHA-256" in text
    assert {r: tree(getattr(cfg, r + "_dir")) for r in ("inbox", "raw", "synced", "logs")} == before
    assert sorted(str(p.relative_to(cfg.followup_root)) for p in cfg.followup_root.rglob("*")) == dirs_before
    assert not cfg.status_csv.exists() and not cfg.state_dir.exists()


def test_dry_run_after_completion_reports_skips_and_changes_nothing(cfg, make_participant):
    make_participant("ID100392")
    run(cfg)
    before = tree(cfg.followup_root)
    s = run(cfg, dry_run=True)
    assert s.skipped_completed == ["ID100392"] and tree(cfg.followup_root) == before


def test_cli_dry_run(cfg, make_participant, project, capsys, tmp_path):
    make_participant("ID100392")
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"followup_root": str(project), "stability_minutes": 0}), encoding="utf-8")
    before = tree(project)
    code = cli.main(["--config", str(conf), "--dry-run"])
    output = capsys.readouterr().out
    assert code == 0
    assert tree(project) == before
    assert "Would process" in output
    assert "Videos matching participant_id_regex" in output
    assert "00_INBOX/ID100392/ID100392_mom.mp4" in output
    assert "00_INBOX/ID100392/ID100392_child.mp4" in output
    assert not cfg.state_dir.exists() and not cfg.status_csv.exists()


def test_cli_dry_run_does_not_create_missing_folders(cfg, project, capsys, tmp_path):
    shutil.rmtree(cfg.inbox_dir)
    shutil.rmtree(cfg.raw_dir)
    shutil.rmtree(cfg.synced_dir)
    shutil.rmtree(cfg.logs_dir)
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"followup_root": str(project)}), encoding="utf-8")
    assert cli.main(["--config", str(conf), "--dry-run"]) == 3
    assert not any(project.iterdir())


def test_cli_dry_run_lists_videos_waiting_for_stability(cfg, make_participant, project, capsys, tmp_path):
    make_participant("ID100392")
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"followup_root": str(project), "stability_minutes": 10,
                                "stability_recheck_seconds": 0}), encoding="utf-8")

    assert cli.main(["--config", str(conf), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Unstable videos (1 participant folder(s))" in output
    assert "ID100392: a file was modified" in output
    assert "00_INBOX/ID100392/ID100392_mom.mp4" in output
    assert "00_INBOX/ID100392/ID100392_child.mp4" in output


def test_cli_real_run_cancellation_does_not_start_pipeline(cfg, make_participant, project,
                                                           capsys, tmp_path, monkeypatch):
    make_participant("ID100392")
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"followup_root": str(project), "stability_minutes": 0,
                                "stability_recheck_seconds": 0}), encoding="utf-8")
    run_modes = []
    original_run = Pipeline.run

    def track_run(self):
        run_modes.append(self.dry)
        return original_run(self)

    monkeypatch.setattr(Pipeline, "run", track_run)
    monkeypatch.setattr("builtins.input", lambda _: "no")

    assert cli.main(["--config", str(conf)]) == 1
    output = capsys.readouterr().out
    assert "Will sync: 2 source video(s) across 1 participant(s)" in output
    assert "PRE-RUN APPROVAL REPORT" in output
    assert "Workflow cancelled" in output
    assert run_modes == [True]
    assert not cfg.state_dir.exists() and not cfg.status_csv.exists() and not cfg.lock_file.exists()


def test_preflight_report_counts_matches_extras_and_unsupported_suffix(cfg, make_participant):
    folder = make_participant("ID100392")
    (folder / "extra_video.mp4").write_bytes(b"extra")
    (folder / "ID100392_mom.wmv").write_bytes(b"unsupported suffix")

    report = cli.preflight_report(cfg)

    assert "Will sync: 2 source video(s) across 1 participant(s)" in report
    assert "Won't sync this run: 2 video/file(s)" in report
    assert "No match: 1 video(s) across 1 participant(s)" in report
    assert "extra_video.mp4" in report
    assert "No configured video suffix: 1 video/file(s) across 1 participant(s)" in report
    assert "ID100392_mom.wmv" in report


def test_cli_real_run_starts_only_after_approval(cfg, make_participant, project,
                                                capsys, tmp_path, monkeypatch):
    make_participant("ID100392")
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"followup_root": str(project), "stability_minutes": 0,
                                "stability_recheck_seconds": 0}), encoding="utf-8")
    run_modes = []
    original_run = Pipeline.run

    def track_run(self):
        run_modes.append(self.dry)
        return original_run(self) if self.dry else Summary()

    monkeypatch.setattr(Pipeline, "run", track_run)
    monkeypatch.setattr("builtins.input", lambda _: "yes")

    assert cli.main(["--config", str(conf)]) == 0
    assert run_modes == [True, False]
    assert not cfg.lock_file.exists()


def test_no_eligible_run_records_first_observation_without_approval(
        cfg, project, tmp_path, monkeypatch, capsys):
    folder = cfg.inbox_dir / "ID100392"
    folder.mkdir()
    videos = [folder / f"ID100392_{role}.mp4" for role in ("mom", "child")]
    for path in videos:
        path.write_bytes(b"test")
        old = datetime.now().timestamp() - 300
        os.utime(path, (old, old))
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"followup_root": str(project), "stability_minutes": 1,
                                "stability_recheck_seconds": 0}), encoding="utf-8")
    monkeypatch.setattr(cli.media, "check_tools", lambda _: {})
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("approval was requested"))

    assert cli.main(["--config", str(conf)]) == 0
    assert "Will sync: 0 source video(s)" in capsys.readouterr().out
    recorded = state(cfg, "ID100392")
    assert recorded["stability"]["snapshot"]
    assert recorded["stability"]["since_ts"]
    assert all(path.is_file() for path in videos)
    assert not cfg.lock_file.exists()

    # On a later run, the saved observation makes the pair eligible again.
    saved = recorded["stability"]
    saved["since_ts"] -= 120
    StateStore(cfg).save(recorded)
    observer = Pipeline(cfgmod.load(conf), dry_run=True)
    assert observer._check_ready(recorded, folder, videos) is None


@pytest.mark.parametrize("mode", ["yolo", "config"])
def test_cli_skips_approval_when_requested(project, tmp_path, monkeypatch, mode):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"followup_root": str(project), "stability_minutes": 120,
                                "require_approval": mode != "config"}), encoding="utf-8")
    observed = []
    monkeypatch.setattr(cli.media, "check_tools", lambda cfg: {})
    monkeypatch.setattr(cli, "preflight_inventory", lambda *args: ("preview", True, Summary(dry_run=True)))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("approval was requested"))

    def fake_run(self):
        observed.append((self.dry, self.cfg.stability_minutes))
        return Summary()

    monkeypatch.setattr(Pipeline, "run", fake_run)
    assert cli.main(["--config", str(conf)] + (["--yolo"] if mode == "yolo" else [])) == 0
    assert observed == [(False, 0 if mode == "yolo" else 120)]


def test_yolo_dry_run_keeps_read_only_semantics(project, tmp_path, monkeypatch):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({"followup_root": str(project)}), encoding="utf-8")
    monkeypatch.setattr(cli.media, "check_tools", lambda cfg: {})
    observed = []

    def fake_run(self):
        observed.append((self.dry, self.cfg.stability_minutes))
        return Summary()

    monkeypatch.setattr(Pipeline, "run", fake_run)
    assert cli.main(["--config", str(conf), "--yolo", "--dry-run"]) == 0
    assert observed == [(True, 0)]


@pytest.mark.parametrize("sources", ["empty", "incomplete", "already_synced", "unstable"])
def test_cli_no_eligible_videos_reports_without_approval(
        cfg, project, tmp_path, monkeypatch, capsys, sources):
    conf = tmp_path / "config.json"
    settings = {"followup_root": str(project), "stability_minutes": 120,
                "stability_recheck_seconds": 0}
    if sources == "unstable":
        (cfg.inbox_dir / "ID100392").mkdir()
        (cfg.inbox_dir / "ID100392" / "ID100392_mom.mp4").write_bytes(b"test")
        (cfg.inbox_dir / "ID100392" / "ID100392_child.mp4").write_bytes(b"test")
    elif sources == "incomplete":
        settings["stability_minutes"] = 0
        (cfg.inbox_dir / "ID100392").mkdir()
        (cfg.inbox_dir / "ID100392" / "ID100392_mom.mp4").write_bytes(b"test")
    elif sources == "already_synced":
        settings["stability_minutes"] = 0
        (cfg.raw_dir / "ID100392").mkdir()
        (cfg.raw_dir / "ID100392" / "ID100392_mom.mp4").write_bytes(b"test")
        (cfg.raw_dir / "ID100392" / "ID100392_child.mp4").write_bytes(b"test")
    conf.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(cli.media, "check_tools", lambda _: {})
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("approval was requested"))
    original_run = Pipeline.run

    def preview_only(self):
        if not self.dry:
            pytest.fail("real run was started")
        if sources == "already_synced":
            return Summary(outcomes=[Outcome("ID100392", S.SUCCESS, kind="skipped")], dry_run=True)
        return original_run(self)

    monkeypatch.setattr(Pipeline, "run", preview_only)
    assert cli.main(["--config", str(conf)]) == (2 if sources == "incomplete" else 0)
    output = capsys.readouterr().out
    assert "PRE-RUN APPROVAL REPORT" in output
    assert "Will sync: 0 source video(s)" in output
    assert "No videos eligible for processing" in output
    assert not cfg.lock_file.exists()


def test_cli_setup_creates_config_and_followup_folders(tmp_path, capsys):
    followup_root = tmp_path / "new followup"
    config_path = tmp_path / "config.json"

    assert cli.main(["--setup", str(followup_root), "--config", str(config_path)]) == 0

    created = cfgmod.load(config_path)
    assert created.followup_root == followup_root.resolve()
    assert all(path.is_dir() for path in (
        followup_root / "00_INBOX",
        followup_root / "01_RAW",
        followup_root / "02_SYNCED",
        followup_root / "99_LOGS_QC",
        followup_root / "99_LOGS_QC" / "logs",
        followup_root / "99_LOGS_QC" / "state",
    ))
    assert "Created" in capsys.readouterr().out


def test_cli_setup_creates_config_in_followup_root_by_default(tmp_path, monkeypatch):
    followup_root = tmp_path / "default config followup"
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--setup", str(followup_root)]) == 0

    config_path = followup_root / "config.json"
    assert config_path.is_file()
    assert not (tmp_path / "config.json").exists()
    assert cfgmod.load(config_path).followup_root == followup_root.resolve()


def test_cli_reads_default_config_from_current_followup_directory(tmp_path, monkeypatch):
    followup_root = tmp_path / "followup"
    assert cli.main(["--setup", str(followup_root)]) == 0
    monkeypatch.chdir(followup_root)
    monkeypatch.setattr(cli.media, "check_tools", lambda _: {})
    monkeypatch.setattr(cli, "setup_logging", lambda *args: None)
    monkeypatch.setattr(cli, "matching_video_files", lambda _: [])

    assert cli.main(["--dry-run"]) == 0


def test_cli_config_path_overrides_default_config_location(tmp_path, monkeypatch):
    followup_root = tmp_path / "followup"
    config_path = tmp_path / "settings" / "override.json"
    assert cli.main(["--setup", str(followup_root), "--config-path", str(config_path)]) == 0
    assert config_path.is_file()
    assert not (followup_root / "config.json").exists()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.media, "check_tools", lambda _: {})
    monkeypatch.setattr(cli, "setup_logging", lambda *args: None)
    monkeypatch.setattr(cli, "matching_video_files", lambda _: [])

    assert cli.main(["--config-path", str(config_path), "--dry-run"]) == 0


def test_cli_setup_declines_overwrite_without_creating_folders(tmp_path, capsys, monkeypatch):
    followup_root = tmp_path / "new followup"
    config_path = tmp_path / "config.json"
    config_path.write_text('{"followup_root": "preserve this"}', encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "no")

    assert cli.main(["--setup", str(followup_root), "--config", str(config_path)]) == 1

    assert config_path.read_text(encoding="utf-8") == before
    assert not followup_root.exists()
    assert "Setup cancelled" in capsys.readouterr().out


def test_cli_setup_confirms_overwrite_then_creates_folders(tmp_path, capsys, monkeypatch):
    followup_root = tmp_path / "new followup"
    config_path = tmp_path / "config.json"
    config_path.write_text('{"followup_root": "old path"}', encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "yes")

    assert cli.main(["--setup", str(followup_root), "--config", str(config_path)]) == 0

    created = cfgmod.load(config_path)
    assert created.followup_root == followup_root.resolve()
    assert all(path.is_dir() for path in (
        followup_root / "00_INBOX",
        followup_root / "01_RAW",
        followup_root / "02_SYNCED",
        followup_root / "99_LOGS_QC",
    ))
    assert "Created" in capsys.readouterr().out


# ====================================================================== readiness / stability
def test_ready_marker_required(cfg, make_participant):
    cfg.require_ready_marker = True
    d = make_participant("ID100392")
    s = run(cfg)
    assert s.waiting == ["ID100392"] and state(cfg, "ID100392")["status"] == S.WAITING_FOR_READY
    assert not (cfg.raw_dir / "ID100392").exists()
    (d / "READY.txt").write_text("ready")
    s = run(cfg)
    assert s.newly_completed == ["ID100392"]
    assert not (cfg.inbox_dir / "ID100392").exists()                     # marker + empty folder tidied
    assert not list(cfg.raw_dir.rglob("READY.txt"))                      # the marker is never put in RAW


def test_recently_modified_files_wait(cfg, make_participant):
    cfg.stability_minutes = 10
    make_participant("ID100392")                                         # mtimes = "just now"
    s = run(cfg)
    assert s.waiting == ["ID100392"] and state(cfg, "ID100392")["status"] == S.WAITING_FOR_STABILITY
    assert not (cfg.raw_dir / "ID100392").exists()


def test_files_must_be_observed_unchanged_across_runs(cfg, make_participant):
    cfg.stability_minutes = 10
    d = make_participant("ID100392")
    old = datetime.now().timestamp() - 86400
    for f in d.iterdir():
        os.utime(f, (old, old))
    t0 = datetime.now().astimezone()
    clock = {"t": t0}
    def new_pipe():
        return Pipeline(cfg, now_fn=lambda: clock["t"], sleep_fn=lambda s: None)
    assert new_pipe().run().waiting == ["ID100392"]                      # first sighting
    clock["t"] = t0 + timedelta(minutes=5)
    assert new_pipe().run().waiting == ["ID100392"]                      # unchanged only 5 min
    (d / "ID100392_child.mp4").write_bytes((d / "ID100392_child.mp4").read_bytes())   # rewrite => mtime moves
    os.utime(d / "ID100392_child.mp4", (old, old + 1))                                # size same, mtime differs
    clock["t"] = t0 + timedelta(minutes=12)
    assert new_pipe().run().waiting == ["ID100392"]                      # snapshot changed => timer restarts
    clock["t"] = t0 + timedelta(minutes=30)
    assert new_pipe().run().newly_completed == ["ID100392"]


def test_growing_file_detected_by_recheck(cfg, make_participant):
    cfg.stability_minutes = 1
    cfg.stability_requires_prior_observation = False
    cfg.stability_recheck_seconds = 1
    d = make_participant("ID100392")
    old = datetime.now().timestamp() - 3600
    for f in d.iterdir():
        os.utime(f, (old, old))

    def sleeper(_):                     # a file is still being written while we wait
        with open(d / "ID100392_mom.mp4", "ab") as fh:
            fh.write(b"\0")
        os.utime(d / "ID100392_mom.mp4", (old, old))
    s = Pipeline(cfg, sleep_fn=sleeper).run()
    assert s.waiting == ["ID100392"] and "changed during" in state(cfg, "ID100392")["message"]


def test_transfer_temp_file_blocks_processing(cfg, make_participant):
    cfg.stability_minutes = 1
    d = make_participant("ID100392")
    (d / "ID100392_child.mp4.tmp").write_bytes(b"x")
    assert run(cfg).waiting == ["ID100392"]


# ====================================================================== batch isolation / errors
def test_one_broken_participant_does_not_stop_the_batch(cfg, make_participant, monkeypatch):
    make_participant("ID100001")
    d = make_participant("ID100002")
    (d / "ID100002_mom.mp4").write_bytes(b"garbage")
    make_participant("ID100003", 0, 20)
    real = Pipeline._from_inbox

    def boom(self, pid, *a, **k):
        if pid == "ID100003":
            raise RuntimeError("unexpected bug")
        return real(self, pid, *a, **k)
    monkeypatch.setattr(Pipeline, "_from_inbox", boom)
    s = run(cfg)
    assert s.newly_completed == ["ID100001"]
    assert s.manual_review == ["ID100002"] and s.failed == ["ID100003"]
    assert state(cfg, "ID100003")["status"] == S.INTERNAL_ERROR
    txt = s.render()
    for label in ("Newly completed:", "Skipped completed:", "Waiting:", "Manual review:", "Failed:"):
        assert label in txt
    assert s.exit_code() == 2


def test_missing_ffmpeg_is_reported_before_processing(tmp_path, project, capsys):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"followup_root": str(project), "ffmpeg": "no-such-ffmpeg-binary"}))
    assert cli.main(["--config", str(conf)]) == 3
    assert "not found in PATH" in capsys.readouterr().out


def test_missing_root_and_bad_config_exit_3(tmp_path, capsys):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"followup_root": str(tmp_path / "nope")}))
    assert cli.main(["--config", str(conf)]) == 3
    assert cli.main(["--config", str(tmp_path / "missing.json")]) == 3


def test_second_run_while_locked_is_refused(cfg, project, tmp_path):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"followup_root": str(project), "stability_minutes": 0}))
    cfg.lock_file.write_text("pid=1")
    assert cli.main(["--config", str(conf)]) == 3
    assert cfg.lock_file.exists()                                        # a foreign lock is not deleted


# ====================================================================== LOW_CONFIDENCE / manual review flow
@pytest.fixture
def unrelated_participant(cfg):
    pid = "ID100777"
    d = cfg.inbox_dir / pid
    a, b = wall_signal(70, 16000, seed=1), wall_signal(70, 16000, seed=2)
    make_video(d / f"{pid}_mom.mp4", a[:60 * 16000], 16000, 60)
    make_video(d / f"{pid}_child.mp4", b[:60 * 16000], 16000, 60)
    return pid


def test_low_confidence_is_not_success_and_makes_no_videos(cfg, unrelated_participant):
    pid = unrelated_participant
    s = run(cfg)
    assert s.manual_review == [pid] and not s.newly_completed
    st = state(cfg, pid)
    assert st["status"] == S.LOW_CONFIDENCE and st["sync_completed_at"] is None
    assert not (cfg.synced_dir / pid).exists() or not list((cfg.synced_dir / pid).glob("*.mp4"))
    assert (cfg.raw_dir / pid / f"{pid}_mom.mp4").is_file()             # promoted safely; only the sync is on hold


def test_low_confidence_is_not_recomputed_every_day_and_can_be_resolved(cfg, unrelated_participant, monkeypatch):
    pid = unrelated_participant
    run(cfg)
    monkeypatch.setattr(media, "extract_audio", lambda *a, **k: pytest.fail("sticky: must not recompute daily"))
    for _ in range(3):
        assert run(cfg).manual_review == [pid]
    monkeypatch.undo()
    s = run(cfg, only=[pid], reprocess=[pid], manual_offset=1.5)         # human reviewed and supplies offset
    assert s.newly_completed == [pid], s.render()
    st = state(cfg, pid)
    assert st["sync"]["offset_source"] == "manual" and st["sync"]["offset_seconds"] == 1.5
    dm = media.probe(cfg, cfg.synced_dir / pid / f"{pid}_mom_synced.mp4").duration
    assert dm == pytest.approx(60 - 1.5, abs=0.3)                        # positive offset => mom trimmed


def test_reprocess_archives_old_outputs_instead_of_deleting(cfg, make_participant):
    make_participant("ID100392")
    run(cfg)
    out = cfg.synced_dir / "ID100392"
    old = {p.name: p.read_bytes() for p in out.glob("*.mp4")}
    s = run(cfg, only=["ID100392"], reprocess=["ID100392"], sleep_fn=lambda s: None)
    assert s.newly_completed == ["ID100392"]
    archived = list(out.glob("_superseded_*/*.mp4"))
    assert {p.name: p.read_bytes() for p in archived} == old
    assert len(list(out.glob("*.mp4"))) == 2


def test_sha256_is_computed_once_and_only_from_inbox_before_promotion(cfg, make_participant, monkeypatch):
    make_participant("ID100392")
    seen = []
    real = media.sha256_file
    monkeypatch.setattr(media, "sha256_file", lambda p, *a, **k: seen.append(Path(p).parent.parent.name) or real(p))
    run(cfg)
    run(cfg)
    assert seen == ["00_INBOX", "00_INBOX"]                              # 2 files, hashed once each, in INBOX


def test_raw_only_participant_without_state_is_processed_from_raw(cfg, make_participant):
    d = make_participant("ID100392")
    shutil.move(str(d), str(cfg.raw_dir / "ID100392"))                    # placed manually into RAW
    s = run(cfg)
    assert s.newly_completed == ["ID100392"]
    assert state(cfg, "ID100392")["raw_promoted_at"] is None


def test_ready_marker_requirement_does_not_block_raw_only_participants(cfg, make_participant):
    cfg.require_ready_marker = True
    d = make_participant("ID100392")
    shutil.move(str(d), str(cfg.raw_dir / "ID100392"))
    assert run(cfg).newly_completed == ["ID100392"]


def test_stray_json_in_state_dir_is_ignored(cfg, make_participant):
    make_participant("ID100392")
    cfg.state_dir.mkdir(parents=True)
    (cfg.state_dir / "notes.json").write_text("{}")
    s = run(cfg)
    assert s.newly_completed == ["ID100392"] and (cfg.state_dir / "notes.json").exists()
    assert not list(cfg.state_dir.glob("*.corrupt-*"))
    # ... and the stray file must not create a row in the master CSV
    rows = list(csv.DictReader(cfg.status_csv.open(encoding="utf-8", newline="")))
    assert [r["participant_id"] for r in rows] == ["ID100392"]
    assert "notes" not in cfg.status_csv.read_text(encoding="utf-8")
