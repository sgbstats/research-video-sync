"""ffmpeg / ffprobe wrappers.  Every call is an argument list (never shell=True) with the
return code checked and stderr captured into the raised error."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from fractions import Fraction
from pathlib import Path

import numpy as np

from .config import Config


class MediaError(RuntimeError):
    pass


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise MediaError(f"{what}: executable not found: {cmd[0]}") from exc
    except OSError as exc:
        raise MediaError(f"{what}: could not start {cmd[0]}: {exc}") from exc
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-6:]
        raise MediaError(f"{what} failed (exit {r.returncode}): " + " | ".join(tail))
    return r


def check_tools(cfg: Config) -> dict[str, str]:
    """Verify FFmpeg tools; use managed binaries for default names missing from PATH."""
    paths = {"ffmpeg": shutil.which(cfg.ffmpeg), "ffprobe": shutil.which(cfg.ffprobe)}
    missing = [name for name, path in paths.items() if path is None]
    custom_missing = [name for name in missing if getattr(cfg, name) != name]
    if custom_missing:
        name = custom_missing[0]
        exe = getattr(cfg, name)
        raise MediaError(f"'{exe}' was not found in PATH (install ffmpeg and add it to PATH, "
                         f"or set an absolute path in the config)")
    if missing:
        try:
            from static_ffmpeg import run

            bundled_ffmpeg, bundled_ffprobe = run.get_or_fetch_platform_executables_else_raise()
        except (ImportError, OSError, RuntimeError) as exc:
            names = " and ".join(missing)
            raise MediaError(f"'{names}' were not found in PATH and managed FFmpeg binaries "
                             f"could not be installed: {exc}") from exc
        bundled = {"ffmpeg": bundled_ffmpeg, "ffprobe": bundled_ffprobe}
        for name in missing:
            paths[name] = str(bundled[name])
            setattr(cfg, name, str(paths[name]))

    out = {}
    for name, path in paths.items():
        if path is None:
            raise MediaError(f"'{getattr(cfg, name)}' was not found in PATH")
        r = _run([path, "-version"], f"{name} -version")
        out[getattr(cfg, name)] = r.stdout.splitlines()[0] if r.stdout else path
    return out


@dataclass
class MediaInfo:
    size_bytes: int
    duration: float | None
    format_name: str | None
    format_start: float
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None
    audio_rate: int | None = None
    audio_channels: int | None = None
    audio_start: float = 0.0     # audio stream start_time relative to the container start

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    def to_dict(self) -> dict:
        return asdict(self)


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _fps(rate: str | None) -> float | None:
    try:
        v = float(Fraction(rate))          # ffprobe reports e.g. "30000/1001"
        return v if v > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe(cfg: Config, path: Path) -> MediaInfo:
    r = _run([cfg.ffprobe, "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", str(path)], f"ffprobe {path.name}")
    try:
        j = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned unreadable output for {path.name}") from exc
    fmt = j.get("format") or {}
    if not j.get("streams"):
        raise MediaError(f"{path.name}: no streams found (not a readable media container)")
    fstart = _f(fmt.get("start_time")) or 0.0
    info = MediaInfo(size_bytes=path.stat().st_size, duration=_f(fmt.get("duration")),
                     format_name=fmt.get("format_name"), format_start=fstart)
    for s in j["streams"]:
        kind = s.get("codec_type")
        if kind == "video" and not info.has_video and not (s.get("disposition") or {}).get("attached_pic"):
            info.video_codec = s.get("codec_name")
            info.width, info.height = s.get("width"), s.get("height")
            info.fps = _fps(s.get("avg_frame_rate") or s.get("r_frame_rate"))
            if info.duration is None:
                info.duration = _f(s.get("duration"))
        elif kind == "audio" and not info.has_audio:
            info.audio_codec = s.get("codec_name")
            info.audio_rate = int(s["sample_rate"]) if s.get("sample_rate") else None
            info.audio_channels = s.get("channels")
            info.audio_start = (_f(s.get("start_time")) or 0.0) - fstart
    return info


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def extract_audio(cfg: Config, src: Path, dst_raw: Path, rate: int) -> np.ndarray:
    """Decode the first audio stream to mono signed-16-bit PCM at `rate` Hz on disk and return
    it as a read-only memory map (so long recordings do not have to fit in RAM)."""
    _run([cfg.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn",
          "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", str(dst_raw)],
         f"audio extraction {src.name}")
    if dst_raw.stat().st_size < 2:
        raise MediaError(f"audio extraction produced no samples for {src.name}")
    return np.memmap(dst_raw, dtype="<i2", mode="r")


def _encode_cmd(cfg: Config, src: Path, dst: Path, trim: float, copy: bool) -> list[str]:
    e = cfg.encode
    cmd = [cfg.ffmpeg, "-nostdin", "-y", "-v", "error"]
    if trim > 0:
        # -ss BEFORE -i together with re-encoding is frame-accurate (ffmpeg decodes from the
        # preceding keyframe and discards frames/samples up to the requested time).
        cmd += ["-ss", f"{trim:.6f}"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0"]
    if copy:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", e.video_codec, "-preset", e.preset, "-crf", str(e.crf), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", e.audio_bitrate]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(dst)]
    return cmd


COPY_VIDEO = {"h264", "hevc"}
COPY_AUDIO = {"aac", "mp3"}


def can_copy(info: MediaInfo) -> bool:
    return info.video_codec in COPY_VIDEO and info.audio_codec in COPY_AUDIO


def encode(cfg: Config, src: Path, dst_partial: Path, trim: float, info: MediaInfo) -> str:
    """Write the synced video for one camera to `dst_partial`.  Returns "copy" or "reencode".

    Decision: the camera that needs no trimming is stream-copied (no quality loss, no time)
    when its codecs are MP4-compatible; if copying fails we fall back to re-encoding.  The
    trimmed camera is always re-encoded because trimming accuracy matters more than speed.
    """
    if trim <= 0 and cfg.encode.copy_untrimmed_when_possible and can_copy(info):
        try:
            _run(_encode_cmd(cfg, src, dst_partial, 0.0, True), f"stream copy {src.name}")
            return "copy"
        except MediaError:
            dst_partial.unlink(missing_ok=True)
    _run(_encode_cmd(cfg, src, dst_partial, trim, False), f"encode {src.name}")
    return "reencode"


def side_by_side(cfg: Config, mom: Path, child: Path, dst_partial: Path) -> None:
    e = cfg.encode
    fc = ("[0:v]scale=-2:720,setpts=PTS-STARTPTS[l];[1:v]scale=-2:720,setpts=PTS-STARTPTS[r];"
          "[l][r]hstack=inputs=2:shortest=1[v];[0:a][1:a]amix=inputs=2:duration=shortest[a]")
    _run([cfg.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(mom), "-i", str(child),
          "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
          "-c:v", e.video_codec, "-preset", e.preset, "-crf", str(e.crf), "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", e.audio_bitrate, "-movflags", "+faststart", "-f", "mp4",
          str(dst_partial)], "side-by-side encode")
