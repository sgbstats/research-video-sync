"""Lightweight tests for the independent TeenCam entry points."""
from pathlib import Path
from types import SimpleNamespace

from sync_inbox import discover
from sync_three_cameras_v2 import candidates


def touch(folder, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"synthetic fixture")


def test_inbox_accepts_two_and_three_camera_sets(tmp_path):
    inbox = tmp_path / "00_INBOX"
    touch(inbox, "#2181 snack task mum view point.AVI",
          "#2181 snack task teen view point.AVI",
          "#2341 Snack task parent view point.AVI",
          "#2341 Snack task teen view point.AVI",
          "#2341 Snack task third camera.AVI")
    groups = discover(inbox)
    assert len(groups["#2181"]["mom"]) == 1
    assert len(groups["#2181"]["child"]) == 1
    assert "third" not in groups["#2181"]
    assert len(groups["#2341"]["third"]) == 1


def test_duplicates_not_silently_accepted(tmp_path):
    inbox = tmp_path / "00_INBOX"
    touch(inbox, "#2181 snack mum view point.AVI",
          "#2181 snack parent view point.AVI",
          "#2181 snack teen view point.AVI")
    cfg = SimpleNamespace(raw_dir=tmp_path/"01_RAW", inbox_dir=inbox,
                          video_extensions=[".avi"], mom_pattern="parent view point",
                          child_pattern="teen view point")
    import pytest
    with pytest.raises(ValueError, match="found 0"):
        candidates(cfg, "#2181")


def test_candidate_selection_works_for_two_and_three_cameras(tmp_path):
    inbox = tmp_path / "00_INBOX"
    touch(inbox, "#2181 snack mum view point.AVI",
          "#2181 snack teen view point.AVI",
          "#2341 snack parent view point.AVI",
          "#2341 snack teen view point.AVI",
          "#2341 snack third camera.AVI")
    cfg = SimpleNamespace(raw_dir=tmp_path/"01_RAW", inbox_dir=inbox,
                          video_extensions=[".avi"], mom_pattern="parent view point",
                          child_pattern="teen view point")
    _, _, two = candidates(cfg, "#2181")
    _, _, three = candidates(cfg, "#2341")
    assert set(two) == {"mom", "child"}
    assert set(three) == {"mom", "child", "third"}
