from __future__ import annotations

import importlib.metadata
import importlib.resources
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline_rg2019
from rg2019 import config as cfgmod
from rg2019 import media
from rg2019 import cli


def test_distribution_exposes_console_script_and_package_template():
    distribution = importlib.metadata.distribution("research-video-sync")
    scripts = {entry.name: entry.value for entry in distribution.entry_points}
    assert scripts["research-video-sync"] == "rg2019.cli:main"
    assert "rg2019" not in scripts
    assert importlib.resources.files("rg2019").joinpath("config.example.json").is_file()


def test_script_and_module_share_the_same_cli():
    assert pipeline_rg2019 is cli
    assert pipeline_rg2019.media is media


def test_python_module_entry_point_displays_version():
    result = subprocess.run(
        [sys.executable, "-m", "rg2019", "--version"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "pipeline_rg2019 2.0.0"


def test_built_wheel_includes_working_compatibility_module(tmp_path):
    project = Path(__file__).resolve().parents[1]
    wheels = tmp_path / "wheels"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheels)],
        cwd=project, capture_output=True, text=True, check=True,
    )
    installed = tmp_path / "installed"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
         "--target", str(installed), str(next(wheels.glob("*.whl")))],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    code = (
        "import sys, runpy, importlib.util; from pathlib import Path; "
        f"sys.path.insert(0, {str(installed)!r}); "
        f"assert Path(importlib.util.find_spec('pipeline_rg2019').origin) == "
        f"Path({str(installed)!r}) / 'pipeline_rg2019.py'; "
        "sys.argv = ['pipeline_rg2019', '--version']; "
        "runpy.run_module('pipeline_rg2019', run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "pipeline_rg2019 2.0.0"


def test_check_tools_uses_path_tools_without_managed_download(tmp_path, monkeypatch):
    cfg = cfgmod.from_dict({"followup_root": tmp_path})
    found = {"ffmpeg": "C:\\tools\\ffmpeg.exe", "ffprobe": "C:\\tools\\ffprobe.exe"}
    calls = []
    monkeypatch.setattr(media.shutil, "which", lambda name: found.get(name))
    monkeypatch.setattr(media, "_run", lambda cmd, what: (
        calls.append(cmd) or SimpleNamespace(stdout=f"{cmd[0]} version\n")
    ))

    versions = media.check_tools(cfg)

    assert cfg.ffmpeg == "ffmpeg" and cfg.ffprobe == "ffprobe"
    assert list(versions) == ["ffmpeg", "ffprobe"]
    assert calls == [[path, "-version"] for path in found.values()]


def test_check_tools_falls_back_only_for_default_tool_missing_from_path(tmp_path, monkeypatch):
    cfg = cfgmod.from_dict({"followup_root": tmp_path})
    binaries = ("C:\\managed\\ffmpeg.exe", "C:\\managed\\ffprobe.exe")
    monkeypatch.setattr(media.shutil, "which", lambda name: (
        "C:\\system\\ffprobe.exe" if name == "ffprobe" else None
    ))
    monkeypatch.setitem(sys.modules, "static_ffmpeg", SimpleNamespace(
        run=SimpleNamespace(get_or_fetch_platform_executables_else_raise=lambda: binaries)
    ))
    commands = []
    monkeypatch.setattr(media, "_run", lambda cmd, what: (
        commands.append(cmd) or SimpleNamespace(stdout="tool version\n")
    ))

    media.check_tools(cfg)

    assert cfg.ffmpeg == binaries[0]
    assert cfg.ffprobe == "ffprobe"
    assert commands == [[binaries[0], "-version"], ["C:\\system\\ffprobe.exe", "-version"]]


def test_check_tools_does_not_replace_an_explicit_missing_executable(tmp_path, monkeypatch):
    cfg = cfgmod.from_dict({"followup_root": tmp_path, "ffmpeg": "custom-ffmpeg"})
    monkeypatch.setattr(media.shutil, "which", lambda _: None)
    monkeypatch.setitem(sys.modules, "static_ffmpeg", SimpleNamespace(
        run=SimpleNamespace(get_or_fetch_platform_executables_else_raise=lambda: pytest.fail(
            "explicit executable must not be replaced"
        ))
    ))

    with pytest.raises(media.MediaError, match="custom-ffmpeg.*not found in PATH"):
        media.check_tools(cfg)


def test_check_tools_reports_managed_binary_install_failure(tmp_path, monkeypatch):
    cfg = cfgmod.from_dict({"followup_root": tmp_path})
    monkeypatch.setattr(media.shutil, "which", lambda _: None)
    monkeypatch.setitem(sys.modules, "static_ffmpeg", SimpleNamespace(
        run=SimpleNamespace(get_or_fetch_platform_executables_else_raise=lambda: (
            (_ for _ in ()).throw(RuntimeError("download failed"))
        ))
    ))

    with pytest.raises(media.MediaError, match="managed FFmpeg binaries could not be installed: download failed"):
        media.check_tools(cfg)
