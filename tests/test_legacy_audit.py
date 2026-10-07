"""AUDIT of the LEGACY shell scripts (documentation-as-tests).

These tests extract the *actual* Python heredocs from the legacy scripts, run them on
synthetic audio with a known offset, and check which camera the legacy export logic
would trim.  They document findings; they do not endorse the legacy code.

Truth model (wall-clock): the mother camera started recording at s_m, the child camera
at s_c (seconds).  Both record the same real-world sound.  The camera that started
EARLIER holds extra material at the start of its file and is the one that must be
trimmed, by |s_c - s_m|, so both files begin at the same real-world event.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import recording, wall_signal, write_wav

ROOT = Path(__file__).resolve().parents[1]
RATE = 100  # low rate keeps the legacy pure-python loops fast; sign logic is rate-independent


def heredocs(script: str) -> list[str]:
    text = (ROOT / script).read_text()
    return re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", text, flags=re.S)


def run_py(code: str, args: list[str], block_numpy: bool = False) -> str:
    prelude = "import sys; sys.modules['numpy']=None\n" if block_numpy else ""
    r = subprocess.run([sys.executable, "-c", prelude + code, *args],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


CASES = [  # (label, mom_start, child_start) ; child_start - mom_start = signed start difference
    ("child starts 5s later", 0.0, 5.0),
    ("child starts 20s later", 0.0, 20.0),
    ("mom starts 5s later", 5.0, 0.0),
    ("mom starts 20s later", 20.0, 0.0),
    ("zero", 0.0, 0.0),
]


def correct_trim(mom_start: float, child_start: float) -> str:
    if child_start > mom_start:
        return "mom"    # mom started earlier -> mom has extra lead-in
    if mom_start > child_start:
        return "child"
    return "none"


def make_wavs(tmp_path, mom_start, child_start):
    wall = wall_signal(150, RATE, seed=7)
    write_wav(tmp_path / "mom.wav", recording(wall, RATE, mom_start, 120), RATE)
    write_wav(tmp_path / "child.wav", recording(wall, RATE, child_start, 120), RATE)
    return str(tmp_path / "mom.wav"), str(tmp_path / "child.wav")


@pytest.mark.parametrize("label,ms,cs", CASES)
def test_legacy_project_script_numpy_path_is_correct(tmp_path, label, ms, cs):
    """sync_research_project.sh (numpy FFT path): FINDING = sign handling is CORRECT."""
    a, b = make_wavs(tmp_path, ms, cs)
    est = float(run_py(heredocs("sync_research_project.sh")[0], [a, b]))
    trimmed = "mom" if est > 0 else "child" if est < 0 else "none"  # script: offset>0 -> trim mom else child
    assert est == pytest.approx(cs - ms, abs=0.02)
    assert trimmed == correct_trim(ms, cs)


def test_legacy_project_script_pure_python_fallback_is_unreliable(tmp_path):
    """sync_research_project.sh fallback (only used if `import numpy` fails): FINDING = it does
    NOT recover the true offset (un-normalised dot product + mismatched window bookkeeping);
    e.g. it reports a non-zero offset for identical start times.  Not usable for research."""
    wrong = 0
    for _label, ms, cs in CASES:
        d = tmp_path / f"{ms}_{cs}"
        d.mkdir()
        a, b = make_wavs(d, ms, cs)
        est = float(run_py(heredocs("sync_research_project.sh")[0], [a, b], block_numpy=True))
        if abs(est - (cs - ms)) > 0.1:
            wrong += 1
    assert wrong >= 3   # observed: 4 of 5 wrong (35, 45, 60, 40 s instead of 5, -5, -20, 0)


@pytest.mark.parametrize("label,ms,cs", [c for c in CASES if c[2] != c[1]])
def test_legacy_sync_videos_script_trims_wrong_camera(tmp_path, label, ms, cs):
    """sync_videos.sh (video1=mom, video2=child): FINDING = the estimator uses the same
    convention as the numpy path (est = child_start - mom_start; positive => video1 has the
    extra lead-in) but its export trims video2 for est >= 0 and video1 otherwise.
    => the WRONG camera is trimmed in every non-zero case."""
    a, b = make_wavs(tmp_path, ms, cs)
    est = float(run_py(heredocs("sync_videos.sh")[0], [a, b]))
    assert est == pytest.approx(cs - ms, abs=0.05)             # estimator itself is right
    trimmed = "child" if est >= 0 else "mom"                   # export logic in the script
    assert trimmed != correct_trim(ms, cs)


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                    reason="requires POSIX bash argument handling")
def test_legacy_bash_set_e_post_increment_from_zero_aborts():
    """Bash `set -e` + `((COUNTER++))`: the arithmetic command's exit status is 1 when the
    expression evaluates to 0, and post-increment evaluates to the OLD value (0 on first use).
    FINDING = the script would abort at the first skipped/failed/processed participant."""
    r = subprocess.run(["bash", "-c", 'set -e; N=0; ((N++)); echo REACHED'],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "REACHED" not in r.stdout
    ok = subprocess.run(["bash", "-c", 'set -e; N=0; ((N++)) || true; ((N++)); echo REACHED $N'],
                        capture_output=True, text=True)
    assert "REACHED 2" in ok.stdout          # from the second increment on it is harmless


def test_legacy_scripts_use_the_risky_counter_pattern():
    text = (ROOT / "sync_research_project.sh").read_text()
    assert "set -e" in text
    assert all(f"(({c}++))" in text for c in ("PROCESSED", "SKIPPED", "FAILED"))
