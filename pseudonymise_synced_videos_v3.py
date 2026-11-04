#!/usr/bin/env python3
"""Create pseudonym-labelled COPIES of two- and three-camera synced recordings.

Requires: python -m pip install openpyxl
Dry run by default; --apply copies without overwriting existing outputs.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path
from openpyxl import load_workbook

# Accept 2 through 8 digits, optionally prefixed by #, with optional SNACK task label.
VIDEO_NAME = re.compile(
    r'^(#?\d{2,8})(?:_SNACK)?_(mom_synced|child_synced|third_synced|side_by_side)\.mp4$',
    re.IGNORECASE,
)
VALID_ID = re.compile(r'\d{2,8}')
SAFE_PSEUDO = re.compile(r'[A-Za-z0-9_-]+')
LABELS = {
    'mom_synced': 'Teen_synced',    # parent POV: teen is visible
    'child_synced': 'Mum_synced',   # teen POV: mum is visible
    'third_synced': 'Third_synced',
    'side_by_side': 'side_by_side',
}


def normalize_id(value, row):
    if isinstance(value, bool):
        raise ValueError(f'Excel row {row}: invalid studyid (must be 2–8 digits)')
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f'Excel row {row}: studyid is not a whole number')
        value = int(value)
    text = str(value).strip()
    if text.startswith('#'):
        text = text[1:].strip()
    if not VALID_ID.fullmatch(text):
        raise ValueError(f'Excel row {row}: studyid must have 2–8 digits')
    return text


def load_mapping(path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = workbook.active
        rows = worksheet.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            raise ValueError('Excel worksheet is empty')
        columns = [str(x).strip().lower() if x is not None else '' for x in header]
        if 'studyid' not in columns or 'pseudo_id' not in columns:
            raise ValueError('Excel first row needs studyid and pseudo_id columns')
        id_col, pseudo_col = columns.index('studyid'), columns.index('pseudo_id')
        mapping, reverse = {}, {}
        ignored = 0
        for row_num, row in enumerate(rows, 2):
            raw_id = row[id_col] if len(row) > id_col else None
            raw_pseudo = row[pseudo_col] if len(row) > pseudo_col else None
            if raw_id is None or str(raw_id).strip() == '' or raw_pseudo is None or str(raw_pseudo).strip() == '':
                ignored += 1
                continue
            study = normalize_id(raw_id, row_num)
            pseudo = str(raw_pseudo).strip()
            if not SAFE_PSEUDO.fullmatch(pseudo):
                raise ValueError(f'Excel row {row_num}: invalid pseudo_id characters')
            if study in mapping and mapping[study] != pseudo:
                raise ValueError(f'Excel row {row_num}: conflicting pseudo_id for studyid')
            if pseudo.casefold() in reverse and reverse[pseudo.casefold()] != study:
                raise ValueError(f'Excel row {row_num}: pseudo_id assigned to multiple study IDs')
            mapping[study] = pseudo
            reverse[pseudo.casefold()] = study
        if not mapping:
            raise ValueError('No valid mappings found')
        return mapping, ignored
    finally:
        workbook.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mapping', type=Path, required=True, help='Excel mapping file')
    parser.add_argument('--root', type=Path, required=True, help='Root folder containing 02_SYNCED and/or 02_SYNCED_3CAM')
    parser.add_argument('--apply', action='store_true', help='Copy videos; default is preview')
    args = parser.parse_args()
    root = args.root.expanduser()
    sources = [
        (root / '02_SYNCED', root / '03_PSEUDO_SYNCED', 'two-camera'),
        (root / '02_SYNCED_3CAM', root / '03_PSEUDO_SYNCED_3CAM', 'three-camera'),
    ]
    if not any(src.is_dir() for src, _, _ in sources):
        parser.error('Neither 02_SYNCED nor 02_SYNCED_3CAM exists under --root: ' + str(root))
    try:
        mapping, ignored = load_mapping(args.mapping)
    except (OSError, ValueError, StopIteration) as exc:
        parser.error(str(exc))

    plans, unmatched, unrecognised = [], 0, 0
    for source, dest, group in sources:
        if not source.is_dir():
            continue
        for video in sorted(source.rglob('*')):
            if not video.is_file() or video.suffix.lower() != '.mp4':
                continue
            match = VIDEO_NAME.fullmatch(video.name)
            if match is None:
                unrecognised += 1
                continue
            study = match.group(1).lstrip('#')
            if study not in mapping:
                unmatched += 1
                print(f'NO MAPPING ({group}): {video.name}')
                continue
            label = LABELS[match.group(2).lower()]
            target = dest / f'{mapping[study]}_{label}.mp4'
            plans.append((video, target, group))

    # Prevent any duplicate destinations before copying a single file.
    seen = {}
    for video, target, _ in plans:
        key = str(target).casefold()
        if key in seen:
            parser.error('Two source recordings map to the same destination filename: '
                         + target.name + '. Check duplicates and task labels; no files copied.')
        seen[key] = video

    print('Mode:', 'COPY' if args.apply else 'PREVIEW (no changes)')
    print('Root:', root)
    print('Excel rows skipped due to blanks:', ignored)
    print('Recognised recordings with mapping:', len(plans))
    print('Recognised recordings without mapping:', unmatched)
    print('Other MP4 files ignored:', unrecognised)
    copied = skipped = 0
    for source, target, group in plans:
        if target.exists():
            print(f'SKIP existing ({group}): {target.name}')
            skipped += 1
            continue
        print(f'{"COPY" if args.apply else "WOULD COPY"} ({group}): {source.name} -> {target.name}')
        if args.apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with source.open('rb') as input_file, target.open('xb') as output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                shutil.copystat(source, target)
                copied += 1
            except Exception:
                if target.exists():
                    target.unlink()
                raise
    print(f'Finished. Copied: {copied}; existing skipped: {skipped}.')
    if not args.apply:
        print('PREVIEW ONLY. Review mapping and labels; add --apply to create copies.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
