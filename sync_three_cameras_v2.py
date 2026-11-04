#!/usr/bin/env python3
"""Automatic two- or three-camera TeenCam synchronisation, separate from the existing two-camera pipeline.

Requires the installed rg2019 package and FFmpeg. Never moves or modifies input files.
Run with --id '#2096' to select a single participant. Preview is the default; --apply encodes.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from rg2019 import config, media, syncest

ROLES = ('mom', 'child', 'third')


def candidates(cfg, pid):
    found = []
    # Prefer the originals already promoted to RAW; otherwise read from INBOX.
    for base in (cfg.raw_dir, cfg.inbox_dir):
        if not base.is_dir():
            continue
        by_dir = {}
        for path in base.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in cfg.video_extensions:
                continue
            if path.name.lower().endswith(('.partial', '.tmp')):
                continue
            stem = path.stem.lower()
            # Match the complete participant ID at the beginning of the filename.
            if not re.match(re.escape(pid.lower()) + r'(?!\d)', stem):
                continue
            tags = [any(label in stem for label in (cfg.mom_pattern.lower(), 'mum view point')),
                    cfg.child_pattern.lower() in stem, 'third camera' in stem]
            if sum(tags) != 1:
                continue
            role = ROLES[tags.index(True)]
            by_dir.setdefault(path.parent, {}).setdefault(role, []).append(path)
        for directory, role_files in by_dir.items():
            if (len(role_files.get('mom', [])) == 1 and len(role_files.get('child', [])) == 1
                    and len(role_files.get('third', [])) <= 1):
                found.append((base, directory, {r: files[0] for r, files in role_files.items()}))
        if found:
            break
    if len(found) != 1:
        raise ValueError(f'Expected exactly one folder containing parent and teen recordings (optional third camera) for {pid}; found {len(found)}. '
                         'Check filenames and ensure there are no duplicate sets.')
    return found[0]


def audio_offset(cfg, mom_path, other_path, mom_info, other_info, temp, label):
    m = media.extract_audio(cfg, mom_path, temp / f'mom_{label}.raw', cfg.sync.sample_rate)
    o = media.extract_audio(cfg, other_path, temp / f'{label}.raw', cfg.sync.sample_rate)
    try:
        result = syncest.estimate_offset(m, o, cfg.sync, mom_info.audio_start, other_info.audio_start)
    finally:
        del m, o
    print(f'  {label}: offset={result.offset_seconds} seconds, confidence={result.confidence:.3f}; {result.reason}')
    if not result.ok or result.offset_seconds is None:
        raise ValueError(f'Audio synchronisation with {label} did not meet quality checks. '
                         'No videos will be produced; inspect the recordings and audio.')
    return float(result.offset_seconds)


def encode(cfg, source, target, trim, duration):
    partial = target.with_name(target.name + '.partial')
    if target.exists() or partial.exists():
        raise FileExistsError(f'Output already exists; will not overwrite: {target.name} (or .partial)')
    cmd = [cfg.ffmpeg, '-nostdin', '-n', '-v', 'error', '-ss', f'{trim:.6f}', '-i', str(source),
           '-t', f'{duration:.6f}', '-map', '0:v:0', '-map', '0:a:0',
           '-c:v', cfg.encode.video_codec, '-preset', cfg.encode.preset,
           '-crf', str(cfg.encode.crf), '-pix_fmt', 'yuv420p',
           '-c:a', 'aac', '-b:a', cfg.encode.audio_bitrate,
           '-movflags', '+faststart', '-f', 'mp4', str(partial)]
    try:
        media._run(cmd, f'encode {source.name}')
        info = media.probe(cfg, partial)
        if not info.has_video or not info.has_audio or info.duration is None or abs(info.duration - duration) > cfg.encode.duration_tolerance_seconds:
            raise ValueError(f'Encoded video failed duration/audio/video validation: {target.name}')
        if target.exists():
            raise FileExistsError(f'Output appeared while encoding: {target}')
        partial.rename(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def main():
    ap = argparse.ArgumentParser(description='Synchronise two or three cameras to a shared timeline (preview unless --apply).')
    ap.add_argument('--config', required=True, type=Path, help='Existing rg2019 config.json')
    ap.add_argument('--id', required=True, help='Participant ID, e.g. #2096')
    ap.add_argument('--root', type=Path, help='Override the video folder containing 00_INBOX and 01_RAW')
    ap.add_argument('--apply', action='store_true', help='Encode videos after estimating offsets')
    args = ap.parse_args()
    cfg = config.load(args.config)
    if args.root is not None:
        if not args.root.is_dir():
            ap.error(f'Video root folder does not exist: {args.root}')
        cfg.followup_root = args.root.resolve()
    pid = args.id.strip()
    if re.fullmatch(cfg.participant_id_regex, pid) is None:
        ap.error('ID does not match participant_id_regex in config.json')
    media.check_tools(cfg)
    _, directory, sources = candidates(cfg, pid)
    print('Input folder:', directory)
    active_roles = tuple(role for role in ROLES if role in sources)
    print(f'Camera set: {len(active_roles)} cameras')
    for role in active_roles:
        print(f'  {role}: {sources[role].name}')
    info = {r: media.probe(cfg, p) for r, p in sources.items()}
    for role, meta in info.items():
        if not meta.has_video or not meta.has_audio or meta.duration is None:
            raise ValueError(f'{role} video needs usable video and audio streams')
    with tempfile.TemporaryDirectory(prefix='teencam_three_audio_') as tmp:
        temp = Path(tmp)
        a = audio_offset(cfg, sources['mom'], sources['child'], info['mom'], info['child'], temp, 'child')
        b = (audio_offset(cfg, sources['mom'], sources['third'], info['mom'], info['third'], temp, 'third')
             if 'third' in sources else None)
    # Offsets are t_mom - t_other; all trims reference the SAME real-world instant.
    offsets = {'mom': 0.0, 'child': a}
    if b is not None:
        offsets['third'] = b
    common_start = max(0.0, *offsets.values())
    trims = {r: max(0.0, common_start - offsets[r]) for r in active_roles}
    duration = min(info[r].duration - trims[r] for r in active_roles)
    if duration < cfg.sync.min_overlap_seconds:
        raise ValueError(f'Insufficient camera overlap: {duration:.2f} seconds')
    desc = f'_{cfg.video_description}' if cfg.video_description else ''
    out = cfg.followup_root / '02_SYNCED_3CAM' / pid
    names = {r: f'{pid}{desc}_{r}_synced.mp4' for r in active_roles}
    names['side_by_side'] = f'{pid}{desc}_side_by_side.mp4'
    print(f'Common duration: {duration:.3f} s')
    for r in active_roles:
        print(f'  {r}: trim {trims[r]:.3f} s -> {names[r]}')
    if cfg.create_side_by_side:
        print('  side-by-side: mom + child only ->', names['side_by_side'])
    else:
        print('  side-by-side disabled by config')
    print('Output folder:', out)
    if not args.apply:
        print('PREVIEW ONLY. Check offsets and camera labels before using --apply.')
        return
    outputs = [out / names[r] for r in active_roles]
    if cfg.create_side_by_side:
        outputs.append(out / names['side_by_side'])
    if any(p.exists() or p.with_name(p.name + '.partial').exists() for p in outputs):
        raise FileExistsError('One or more output files already exist. Nothing will be overwritten.')
    out.mkdir(parents=True, exist_ok=True)
    for r in active_roles:
        print('Encoding:', names[r], flush=True)
        encode(cfg, sources[r], out / names[r], trims[r], duration)
    if cfg.create_side_by_side:
        target = out / names['side_by_side']
        part = target.with_name(target.name + '.partial')
        try:
            media.side_by_side(cfg, out / names['mom'], out / names['child'], part)
            check = media.probe(cfg, part)
            if not check.has_video or not check.has_audio or check.duration is None or abs(check.duration-duration)>cfg.encode.duration_tolerance_seconds:
                raise ValueError('Side-by-side validation failed')
            part.rename(target)
        except Exception:
            part.unlink(missing_ok=True)
            raise
    print(f'SUCCESS. All {len(active_roles)} camera outputs share the same start and duration; side-by-side uses two POV cameras only.')
    print('IMPORTANT: Visually check sync, especially when audio correlation is weak.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, media.MediaError, config.ConfigError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
