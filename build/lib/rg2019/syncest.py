"""Audio synchronisation estimator (memory-safe, windowed, two-stage).

SIGN CONVENTION (single source of truth for the whole project)
--------------------------------------------------------------
Let a real-world sound event happen at container time ``t_mom`` in the mother file and
``t_child`` in the child file.  Then

    offset_seconds = t_mom - t_child          (== child_start - mom_start in wall-clock time)

* offset > 0 : the event is LATER in the mother file, i.e. the mother camera started
               recording EARLIER and its file has ``offset`` seconds of extra lead-in.
               => trim ``offset`` seconds from the START OF THE MOTHER video.
* offset < 0 : the child camera started earlier => trim ``|offset|`` s from the CHILD video.
* offset = 0 : nothing to trim.

After trimming, both files begin at the same real-world moment.  ``trim_plan`` is the only
place that turns an offset into trims.

The offset is the lag maximising  sum_i mom[p + i] * child[p - offset + i]  (a normalised
cross-correlation of a mother window against a child search region).

ALGORITHM
---------
Stage 1 (coarse): ``coarse_windows`` windows of the mother audio spread over the recording are
    each searched in the child audio within +-max_lag.  Windows are accepted only if their
    normalised correlation peak (NCC) and peak-to-competing-peak ratio pass thresholds; the
    largest cluster of agreeing windows gives the coarse offset.
Stage 2 (fine): up to ``fine_windows`` DISTINCT windows (start positions >= half a window apart) are spread over the whole *overlap* implied by the
    coarse offset and searched only +-fine_search_seconds around it, with parabolic sub-sample
    interpolation.  The final offset is the median of the agreeing windows.
Only one window (<= max_lag*2 + window seconds) is ever held in RAM as float64; the full
audio stays on disk as a memory map.  Fully deterministic (no randomness).

CONFIDENCE RULE (SUCCESS requires ALL of):
    * stage 1 found a coarse cluster of >= 2 valid windows,
    * >= min_agreeing_windows DISTINCT stage-2 windows agree within fine_tolerance_seconds of the median,
    * agreeing windows / stage-2 windows >= min_agree_fraction,
    * median NCC of the agreeing windows >= min_ncc.
Otherwise the result is LOW_CONFIDENCE and must be reviewed by a human.
``confidence`` (0..1) = median NCC of agreeing windows * (agreeing / stage-2 windows).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
from scipy.signal import fftconvolve

from .config import SyncParams

PEAK_EXCLUSION_SECONDS = 0.05


def trim_plan(offset_seconds: float, min_trim: float = 0.0) -> tuple[float, float]:
    """(mom_trim_seconds, child_trim_seconds) for an offset in the convention above."""
    if abs(offset_seconds) < max(min_trim, 1e-9):
        return 0.0, 0.0
    return (offset_seconds, 0.0) if offset_seconds > 0 else (0.0, -offset_seconds)


@dataclass
class WindowResult:
    stage: int
    position_seconds: float      # window start in mother time
    offset_seconds: float | None
    ncc: float
    peak_ratio: float
    valid: bool
    note: str = ""


@dataclass
class SyncEstimate:
    ok: bool                              # True => SUCCESS-worthy, False => LOW_CONFIDENCE
    offset_seconds: float | None
    confidence: float
    reason: str
    coarse_offset_seconds: float | None = None
    n_windows: int = 0
    n_agree: int = 0
    median_ncc: float = 0.0
    median_peak_ratio: float = 0.0
    offset_spread_seconds: float = 0.0    # max-min of agreeing window offsets (drift indicator)
    windows: list[WindowResult] = field(default_factory=list)

    def to_dict(self, with_windows: bool = True) -> dict:
        d = asdict(self)
        if not with_windows:
            d.pop("windows")
        return d


def _prep(x: np.ndarray) -> np.ndarray:
    """int16 -> float64, DC removed, first-order pre-emphasis (whitens hum / low-freq rumble)."""
    x = np.asarray(x, dtype=np.float64) / 32768.0
    if x.size == 0:
        return x
    x = x - x.mean()
    return np.append(x[0], x[1:] - 0.97 * x[:-1])


def _ncc_search(template: np.ndarray, segment: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation of `template` at every position k of `segment`
    (k = 0 .. len(segment)-len(template)); values in [-1, 1]."""
    m = template.size
    num = fftconvolve(segment, template[::-1], mode="valid")
    csum = np.concatenate(([0.0], np.cumsum(segment * segment)))
    energy = csum[m:] - csum[:-m]
    den = np.sqrt(np.maximum(energy, 1e-12) * max(float(template @ template), 1e-12))
    return num / den


def _peak(ncc: np.ndarray, rate: int) -> tuple[float, float, float]:
    """(index with sub-sample interpolation, peak value, peak/competitor ratio)."""
    k = int(np.argmax(ncc))
    peak = float(ncc[k])
    frac = 0.0
    if 0 < k < ncc.size - 1:
        y0, y1, y2 = ncc[k - 1], ncc[k], ncc[k + 1]
        d = y0 - 2 * y1 + y2
        if d < 0:
            frac = float(np.clip(0.5 * (y0 - y2) / d, -0.5, 0.5))
    ex = max(1, int(PEAK_EXCLUSION_SECONDS * rate))
    rest = np.concatenate((ncc[:max(0, k - ex)], ncc[k + ex + 1:]))
    ratio = peak / max(float(np.max(np.abs(rest))), 1e-9) if rest.size else 99.0
    return k + frac, peak, min(ratio, 99.0)


def _window(mom: np.ndarray, child: np.ndarray, rate: int, p: int, w: int,
            lo_off: float, hi_off: float, stage: int, prm: SyncParams) -> WindowResult:
    """One window: mother samples [p, p+w) vs child region for offsets in [lo_off, hi_off]."""
    pos = p / rate
    tpl_raw = mom[p:p + w]
    if float(np.sqrt(np.mean((np.asarray(tpl_raw, dtype=np.float64) / 32768.0) ** 2))) < prm.silence_rms:
        return WindowResult(stage, pos, None, 0.0, 0.0, False, "silent mother window")
    # child index of the partner of mother sample p is p - offset*rate
    c0 = max(0, int(np.floor(p - hi_off * rate)))
    c1 = min(child.size, int(np.ceil(p + w - lo_off * rate)))
    if c1 - c0 < w:
        return WindowResult(stage, pos, None, 0.0, 0.0, False, "child region too short")
    seg_raw = child[c0:c1]
    if float(np.sqrt(np.mean((np.asarray(seg_raw, dtype=np.float64) / 32768.0) ** 2))) < prm.silence_rms:
        return WindowResult(stage, pos, None, 0.0, 0.0, False, "silent child region")
    ncc = _ncc_search(_prep(tpl_raw), _prep(seg_raw))
    kf, peak, ratio = _peak(ncc, rate)
    offset = (p - (c0 + kf)) / rate
    valid = peak >= prm.min_ncc and ratio >= prm.min_peak_ratio
    note = "" if valid else f"weak peak (ncc={peak:.3f}, ratio={ratio:.2f})"
    return WindowResult(stage, pos, float(offset), peak, ratio, valid, note)


MIN_WINDOW_SPACING = 0.5      # distinct windows start at least half a window apart (<= 50 % overlap)


def _positions(a: int, b: int, n: int, w: int) -> list[int]:
    """Start positions (samples) of up to `n` analysis windows spread evenly over [a, b].

    Positions closer than MIN_WINDOW_SPACING * w to an already kept one are dropped, so identical or
    near-identical windows (e.g. when the region is no longer than one window and linspace would
    repeat the same start) are never counted as independent evidence."""
    if n <= 1 or b <= a:
        return [(a + b) // 2]
    kept: list[int] = []
    for p in np.linspace(a, b, n).astype(int):
        if not kept or p - kept[-1] >= MIN_WINDOW_SPACING * w:
            kept.append(int(p))
    return kept


def _largest_cluster(vals: list[float], tol: float) -> list[int]:
    best: list[int] = []
    for i, v in enumerate(vals):
        members = [j for j, u in enumerate(vals) if abs(u - v) <= tol]
        if len(members) > len(best):
            best = members
    return best


def estimate_offset(mom: np.ndarray, child: np.ndarray, prm: SyncParams,
                    mom_audio_start: float = 0.0, child_audio_start: float = 0.0) -> SyncEstimate:
    """Estimate ``offset_seconds`` (convention in the module docstring) between two mono int16
    signals sampled at ``prm.sample_rate``.  ``*_audio_start`` are the audio streams' start
    times relative to their container start (so the offset refers to container time, which
    is what ffmpeg -ss uses)."""
    rate = prm.sample_rate
    dur_m, dur_c = mom.size / rate, child.size / rate
    shift = mom_audio_start - child_audio_start
    if min(dur_m, dur_c) < 5.0:
        return SyncEstimate(False, None, 0.0, f"audio too short (mom {dur_m:.1f}s, child {dur_c:.1f}s)")

    # ---------------- stage 1: coarse -----------------
    win_s = min(prm.window_seconds, dur_m * 0.5, dur_c * 0.5)
    w = int(win_s * rate)
    L = int(prm.max_lag_seconds * rate)
    # A mother window starting at p can only have a child partner if p - offset lies inside the child
    # file for some |offset| <= max_lag, i.e. p <= len(child) - w + L.  Spread the windows evenly over
    # that range (whole file when durations are similar; no part is favoured for +/- offsets).
    hi = max(0, min(mom.size - w, child.size - w + L))
    pos1 = _positions(0, hi, max(1, prm.coarse_windows), w)
    coarse = [_window(mom, child, rate, int(p), w, -prm.max_lag_seconds, prm.max_lag_seconds, 1, prm)
              for p in pos1]
    valid1 = [r for r in coarse if r.valid]
    est = SyncEstimate(False, None, 0.0, "", windows=list(coarse), n_windows=len(coarse))
    cl = _largest_cluster([r.offset_seconds for r in valid1], prm.coarse_tolerance_seconds)
    if len(cl) < min(2, len(coarse)) or not cl:
        est.reason = (f"no coarse consensus: only {len(valid1)}/{len(coarse)} windows had a clear "
                      f"correlation peak within +-{prm.max_lag_seconds:.0f}s")
        est.median_ncc = float(np.median([r.ncc for r in coarse])) if coarse else 0.0
        return est
    coarse_off = float(np.median([valid1[i].offset_seconds for i in cl]))
    est.coarse_offset_seconds = coarse_off + shift

    # ---------------- stage 2: fine, over the whole overlap -----------------
    # Mother-time interval that has a child partner, inset by fine_search_seconds at both ends so every
    # fine window (template) lies fully inside BOTH files for any true offset within coarse +- search.
    # (Without the inset a window starting exactly at the overlap edge can begin a sample before the
    # child file starts and correlate badly.)
    ov_lo = max(0.0, coarse_off) + prm.fine_search_seconds
    ov_hi = min(dur_m, dur_c + coarse_off) - prm.fine_search_seconds
    if ov_hi - ov_lo < prm.min_overlap_seconds:
        est.reason = f"overlap after coarse alignment too short ({max(0.0, ov_hi - ov_lo):.1f}s)"
        return est
    w2 = int(min(prm.window_seconds, ov_hi - ov_lo) * rate)
    a, b = int(ov_lo * rate), max(int(ov_lo * rate), int(ov_hi * rate) - w2)
    pos2 = _positions(a, b, max(1, prm.fine_windows), w2)
    fine = [_window(mom, child, rate, int(p), w2, coarse_off - prm.fine_search_seconds,
                    coarse_off + prm.fine_search_seconds, 2, prm) for p in pos2]
    est.windows += fine
    est.n_windows = len(fine)
    valid2 = [r for r in fine if r.valid]
    est.median_ncc = float(np.median([r.ncc for r in fine]))
    if not valid2:
        est.reason = "fine stage: no window has a clear correlation peak"
        return est
    med0 = float(np.median([r.offset_seconds for r in valid2]))
    agree = [r for r in valid2 if abs(r.offset_seconds - med0) <= prm.fine_tolerance_seconds]
    offs = [r.offset_seconds for r in agree]
    est.n_agree = len(agree)
    est.offset_seconds = float(np.median(offs)) + shift
    est.median_ncc = float(np.median([r.ncc for r in agree]))
    est.median_peak_ratio = float(np.median([r.peak_ratio for r in agree]))
    est.offset_spread_seconds = float(max(offs) - min(offs))
    est.confidence = float(est.median_ncc * len(agree) / len(fine))

    need = prm.min_agreeing_windows            # counted over DISTINCT windows; never lowered for short overlaps
    problems = []
    if len(fine) < need:
        problems.append(f"overlap too short for {need} distinct analysis windows (only {len(fine)} fit)")
    if len(agree) < need:
        problems.append(f"only {len(agree)}/{len(fine)} windows agree (need >= {need})")
    if len(agree) / len(fine) < prm.min_agree_fraction:
        problems.append(f"agreement fraction {len(agree) / len(fine):.2f} < {prm.min_agree_fraction}")
    if est.median_ncc < prm.min_ncc:
        problems.append(f"median NCC {est.median_ncc:.3f} < {prm.min_ncc}")
    est.ok = not problems
    est.reason = "; ".join(problems) if problems else (
        f"{len(agree)}/{len(fine)} windows agree, median NCC {est.median_ncc:.3f}, "
        f"spread {est.offset_spread_seconds * 1000:.0f} ms")
    return est
