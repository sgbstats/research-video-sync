"""Configuration loading and validation (pathlib only, Windows-first)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePath
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass
class SyncParams:
    """Parameters of the audio synchronisation estimator (see syncest.py)."""
    sample_rate: int = 8000            # Hz, mono analysis audio (ffmpeg downsampled)
    max_lag_seconds: float = 120.0     # largest |offset| searched in the coarse stage
    window_seconds: float = 60.0       # length of each analysis window
    coarse_windows: int = 7            # stage 1: windows spread over the recording
    coarse_tolerance_seconds: float = 0.5
    fine_windows: int = 5              # stage 2: windows spread over the overlap
    fine_search_seconds: float = 2.0   # stage 2: search +-this around the coarse offset
    fine_tolerance_seconds: float = 0.25   # windows agreeing within this of the median
    min_ncc: float = 0.05              # min normalised correlation peak of a valid window
    min_peak_ratio: float = 1.5        # peak / best competing peak (>=0.05 s away)
    min_agreeing_windows: int = 3
    min_agree_fraction: float = 0.6
    min_overlap_seconds: float = 10.0
    silence_rms: float = 1e-4          # windows quieter than this (full scale=1) are skipped


@dataclass
class EncodeParams:
    video_codec: str = "libx264"
    preset: str = "fast"
    crf: int = 20
    audio_bitrate: str = "192k"
    copy_untrimmed_when_possible: bool = True   # stream-copy the camera that needs no trim
    min_trim_seconds: float = 0.02              # |offset| below this is treated as zero
    duration_tolerance_seconds: float = 1.5     # accepted deviation of output duration


@dataclass
class Config:
    followup_root: Path
    inbox: str = "00_INBOX"
    raw: str = "01_RAW"
    synced: str = "02_SYNCED"
    logs_qc: str = "99_LOGS_QC"
    participant_id_regex: str = r"^ID\d{4,8}$"
    mom_pattern: str = "_mom"
    child_pattern: str = "_child"
    video_description: str | None = None
    video_extensions: list[str] = field(default_factory=lambda: [".mp4", ".avi", ".mov", ".mkv"])
    require_ready_marker: bool = False
    ready_marker_name: str = "READY.txt"
    stability_minutes: float = 0.0
    stability_recheck_seconds: float = 5.0
    stability_requires_prior_observation: bool = True
    create_side_by_side: bool = True
    require_approval: bool = True
    max_attempts: int = 3               # automatic retries for SYNC_FAILED / ENCODE_FAILED
    lock_stale_hours: float = 24.0
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    sync: SyncParams = field(default_factory=SyncParams)
    encode: EncodeParams = field(default_factory=EncodeParams)

    # ---- resolved directories -------------------------------------------------
    def _dir(self, name: str) -> Path:
        p = Path(name)
        return p if p.is_absolute() else self.followup_root / p

    @property
    def inbox_dir(self) -> Path:
        return self._dir(self.inbox)

    @property
    def raw_dir(self) -> Path:
        return self._dir(self.raw)

    @property
    def synced_dir(self) -> Path:
        return self._dir(self.synced)

    @property
    def logs_dir(self) -> Path:
        return self._dir(self.logs_qc)

    @property
    def state_dir(self) -> Path:
        return self.logs_dir / "state"

    @property
    def status_csv(self) -> Path:
        return self.logs_dir / "pipeline_status.csv"

    @property
    def lock_file(self) -> Path:
        return self.logs_dir / "pipeline.lock"

    @property
    def log_file_dir(self) -> Path:
        return self.logs_dir / "logs"


def _build(cls, data: dict[str, Any], where: str):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known - {k for k in data if k.startswith("_")}
    if unknown:
        raise ConfigError(f"Unknown key(s) in {where}: {sorted(unknown)} (typo?)")
    return {k: v for k, v in data.items() if k in known}


def from_dict(data: dict[str, Any], path_cls: type[PurePath] = Path) -> Config:
    """Build a Config from a parsed JSON dict.

    `path_cls` exists so tests can exercise Windows path semantics (PureWindowsPath) on any OS.
    """
    if not isinstance(data, dict):
        raise ConfigError("Config must be a JSON object")
    try:
        return _from_dict(data, path_cls)
    except ConfigError:
        raise
    except (TypeError, ValueError, AttributeError) as exc:
        raise ConfigError(f"Invalid config settings: {exc}") from exc


def _from_dict(data: dict[str, Any], path_cls: type[PurePath]) -> Config:
    data = dict(data)
    if "followup_root" not in data:
        raise ConfigError("'followup_root' is required")
    root = path_cls(data.pop("followup_root"))
    sync = SyncParams(**_build(SyncParams, data.pop("sync", {}), "'sync'"))
    enc = EncodeParams(**_build(EncodeParams, data.pop("encode", {}), "'encode'"))
    cfg = Config(followup_root=root, sync=sync, encode=enc,
                 **_build(Config, {k: v for k, v in data.items() if k != "followup_root"}, "config"))
    try:
        re.compile(cfg.participant_id_regex)
    except re.error as exc:
        raise ConfigError(f"participant_id_regex is not a valid regex: {exc}") from exc
    if cfg.sync.min_agreeing_windows > cfg.sync.fine_windows:
        raise ConfigError(f"sync.min_agreeing_windows ({cfg.sync.min_agreeing_windows}) cannot exceed "
                          f"sync.fine_windows ({cfg.sync.fine_windows})")
    if not cfg.mom_pattern or not cfg.child_pattern:
        raise ConfigError("mom_pattern and child_pattern must be non-empty")
    if cfg.mom_pattern.lower() == cfg.child_pattern.lower():
        raise ConfigError("mom_pattern and child_pattern must differ")
    if cfg.video_description is not None:
        if (not isinstance(cfg.video_description, str)
                or len(cfg.video_description) > 80
                or not re.fullmatch(r"[A-Za-z0-9]+(?:[ _-][A-Za-z0-9]+)*", cfg.video_description)):
            raise ConfigError("video_description must be null or a filename-safe description "
                              "(letters, digits, spaces, underscores or hyphens; max 80 characters)")
        cfg.video_description = cfg.video_description.replace(" ", "_")
    if not isinstance(cfg.require_approval, bool):
        raise ConfigError("require_approval must be true or false")
    cfg.video_extensions = [e.lower() if e.startswith(".") else "." + e.lower()
                            for e in cfg.video_extensions]
    dirs = {cfg.inbox, cfg.raw, cfg.synced, cfg.logs_qc}
    if len(dirs) != 4:
        raise ConfigError("inbox/raw/synced/logs_qc must be four different folders")
    return cfg


def load(path: Path) -> Config:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config file ({path}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON ({path}): {exc}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"Config file is not valid UTF-8 ({path}): {exc}") from exc
    try:
        return from_dict(data)
    except ConfigError as exc:
        raise ConfigError(f"Invalid config settings ({path}): {exc}") from exc
