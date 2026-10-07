from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
    import static_ffmpeg

    static_ffmpeg.add_paths()

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import make_video, recording, wall_signal   # noqa: E402
from rg2019 import config as cfgmod                        # noqa: E402

VIDEO_RATE = 16000
REC_SECONDS = 60


@pytest.fixture(scope="session")
def video_cache(tmp_path_factory):
    """Synthetic (mom, child) mp4 pairs keyed by (mom_start, child_start) in wall-clock seconds.
    Generated once per session; both cameras record the same synthetic 'room sound'."""
    base = tmp_path_factory.mktemp("video_cache")
    wall = wall_signal(120, VIDEO_RATE, seed=11)
    made: dict = {}

    def get(mom_start: float, child_start: float, child_audio: bool = True, mom_audio: bool = True):
        key = (mom_start, child_start, child_audio, mom_audio)
        if key not in made:
            d = base / f"{len(made)}"
            m = recording(wall, VIDEO_RATE, mom_start, REC_SECONDS)
            c = recording(wall, VIDEO_RATE, child_start, REC_SECONDS)
            make_video(d / "mom.mp4", m if mom_audio else None, VIDEO_RATE, REC_SECONDS)
            make_video(d / "child.mp4", c if child_audio else None, VIDEO_RATE, REC_SECONDS)
            made[key] = d
        return made[key]
    return get


@pytest.fixture
def project(tmp_path):
    """Empty FOLLOWUP_ROOT with a space and a non-ASCII character in the path on purpose."""
    root = tmp_path / "RG2019 CÂMERAS" / "FOLLOWUP_TEST"
    for d in ("00_INBOX", "01_RAW", "02_SYNCED", "99_LOGS_QC"):
        (root / d).mkdir(parents=True)
    return root


@pytest.fixture
def cfg(project):
    return cfgmod.from_dict({
        "followup_root": str(project),
        "stability_minutes": 0, "stability_recheck_seconds": 0,
        "create_side_by_side": False,
        "sync": {"max_lag_seconds": 30, "window_seconds": 10, "coarse_windows": 7, "fine_windows": 5,
                 "fine_search_seconds": 1.0},
        "encode": {"preset": "ultrafast", "crf": 30},
    })


@pytest.fixture
def make_participant(cfg, video_cache):
    def _make(pid: str, mom_start=0.0, child_start=5.0, ready=False, **kw):
        src = video_cache(mom_start, child_start, **kw)
        d = cfg.inbox_dir / pid
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "mom.mp4", d / f"{pid}_mom.mp4")
        shutil.copy2(src / "child.mp4", d / f"{pid}_child.mp4")
        if ready:
            (d / cfg.ready_marker_name).write_text("ready")
        return d
    return _make
