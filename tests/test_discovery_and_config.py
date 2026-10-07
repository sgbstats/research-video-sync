"""participant-id validation, source discovery, config, Windows path handling (tests 1,2,3,4,12)."""
from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

import pytest

from rg2019 import config as cfgmod
from rg2019.discovery import find_sources, is_transit_file, valid_participant_id


@pytest.fixture
def base_cfg(tmp_path):
    return cfgmod.from_dict({"followup_root": str(tmp_path)})


@pytest.mark.parametrize("name,ok", [
    ("ID100392", True), ("ID100534", True), ("ID1234", True),
    ("id100392", False), ("ID12", False), ("ID100392_old", False), ("100392", False),
    ("ID 100392", False), ("ID100392 ", False), ("@eaDir", False), ("ID10039x", False), ("", False),
    ("ID../..", False),
])
def test_participant_id_validation(base_cfg, name, ok):
    assert valid_participant_id(base_cfg, name) is ok


def touch(folder: Path, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"x")


@pytest.mark.parametrize("ext", ["mp4", "avi", "mov", "mkv", "MP4", "Mov"])
def test_source_discovery_all_formats_and_case(base_cfg, tmp_path, ext):
    touch(tmp_path, f"ID100392_mom.{ext}", f"ID100392_child.{ext}", "notes.txt", "Thumbs.db")
    d = find_sources(base_cfg, tmp_path)
    assert d.problem is None
    assert [p.name for p in d.mom] == [f"ID100392_mom.{ext}"]
    assert [p.name for p in d.child] == [f"ID100392_child.{ext}"]


def test_discovery_mixed_formats(base_cfg, tmp_path):
    touch(tmp_path, "ID100392_MOM_cam1.mov", "ID100392_child.avi")
    assert find_sources(base_cfg, tmp_path).problem is None


def test_discovery_ignores_transfer_temp_files_and_non_videos(base_cfg, tmp_path):
    touch(tmp_path, "ID1_mom.mp4", "ID1_child.mp4", "ID1_mom.mp4.partial", ".~ID1_child.mp4", "ID1_mom.txt")
    assert find_sources(base_cfg, tmp_path).problem is None
    assert is_transit_file("x.mp4.tmp") and is_transit_file(".~x.mp4") and not is_transit_file("x.mp4")


def test_multiple_mom_files_are_ambiguous_never_first_match(base_cfg, tmp_path):
    touch(tmp_path, "ID100392_mom.mp4", "ID100392_mom_copy.mp4", "ID100392_child.mp4")
    d = find_sources(base_cfg, tmp_path)
    assert d.problem == "AMBIGUOUS" and len(d.mom) == 2


def test_multiple_child_files_are_ambiguous(base_cfg, tmp_path):
    touch(tmp_path, "ID1_mom.mp4", "ID1_child.mp4", "ID1_child.avi")
    assert find_sources(base_cfg, tmp_path).problem == "AMBIGUOUS"


def test_file_matching_both_patterns_is_ambiguous(base_cfg, tmp_path):
    touch(tmp_path, "ID1_mom_child.mp4", "ID1_mom.mp4", "ID1_child.mp4")
    assert find_sources(base_cfg, tmp_path).problem == "AMBIGUOUS"


def test_missing_child_is_missing(base_cfg, tmp_path):
    touch(tmp_path, "ID100392_mom.mp4")
    assert find_sources(base_cfg, tmp_path).problem == "MISSING"


def test_missing_both_is_missing(base_cfg, tmp_path):
    touch(tmp_path, "readme.txt")
    assert find_sources(base_cfg, tmp_path).problem == "MISSING"


# ---------------------------------------------------------------- config / Windows paths
def test_windows_style_root_and_subfolders_resolve_with_pathlib():
    cfg = cfgmod.from_dict({"followup_root": "D:\\RG2019_CAMERAS\\FOLLOWUP_2026"}, path_cls=PureWindowsPath)
    assert cfg.inbox_dir == PureWindowsPath("D:/RG2019_CAMERAS/FOLLOWUP_2026/00_INBOX")
    assert str(cfg.raw_dir) == "D:\\RG2019_CAMERAS\\FOLLOWUP_2026\\01_RAW"
    assert str(cfg.state_dir) == "D:\\RG2019_CAMERAS\\FOLLOWUP_2026\\99_LOGS_QC\\state"
    assert str(cfg.status_csv) == "D:\\RG2019_CAMERAS\\FOLLOWUP_2026\\99_LOGS_QC\\pipeline_status.csv"
    assert str(cfg.synced_dir / "ID100392" / "ID100392_mom_synced.mp4").endswith(
        "02_SYNCED\\ID100392\\ID100392_mom_synced.mp4")


def test_config_rejects_unknown_keys_and_bad_values(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="Unknown key"):
        cfgmod.from_dict({"followup_root": str(tmp_path), "stabilty_minutes": 5})
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.from_dict({"followup_root": str(tmp_path), "mom_pattern": "_x", "child_pattern": "_X"})
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.from_dict({"followup_root": str(tmp_path), "participant_id_regex": "("})
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.from_dict({})


@pytest.mark.parametrize("description", ["", "../escape", "a/b", "a\\b", ".hidden", "a  b", 5, "x" * 81])
def test_config_rejects_unsafe_video_descriptions(tmp_path, description):
    with pytest.raises(cfgmod.ConfigError, match="video_description"):
        cfgmod.from_dict({"followup_root": str(tmp_path), "video_description": description})


def test_config_normalizes_description_spaces(tmp_path):
    cfg = cfgmod.from_dict({"followup_root": str(tmp_path), "video_description": "pilot visit"})
    assert cfg.video_description == "pilot_visit"


def test_config_rejects_non_boolean_approval(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="require_approval"):
        cfgmod.from_dict({"followup_root": str(tmp_path), "require_approval": "false"})


def test_example_config_loads_and_contains_no_real_paths():
    p = Path(__file__).resolve().parents[1] / "config.example.json"
    cfg = cfgmod.load(p)
    assert cfg.create_side_by_side is False and cfg.require_ready_marker is False
    assert cfg.stability_minutes == 0
    assert cfgmod.from_dict({"followup_root": p.parent}).stability_minutes == 0
    assert cfg.video_description is None and cfg.require_approval is True


def test_paths_with_spaces_and_unicode_work_end_to_end(project):
    assert " " in str(project) and "Â" in str(project)


def test_source_code_never_uses_shell_true_or_manual_path_joins():
    import ast
    root = Path(__file__).resolve().parents[1]
    for f in [root / "pipeline_rg2019.py", *sorted((root / "rg2019").glob("*.py"))]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                pytest.fail(f"{f.name}: subprocess called with shell=...")
            seps = ("/", "\\")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join"
                    and isinstance(node.func.value, ast.Constant) and node.func.value.value in seps):
                pytest.fail(f"{f.name}:{node.lineno}: manual path join with a separator")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and any(
                    isinstance(o, ast.Constant) and o.value in seps for o in (node.left, node.right)):
                pytest.fail(f"{f.name}:{node.lineno}: path built by string concatenation")
