"""Create pseudonym-labelled copies of synced headcam recordings.

Requires: python -m pip install openpyxl
Preview: python pseudonymise_synced_videos.py --mapping "C:\\path\\ids.xlsx"
Apply:   python pseudonymise_synced_videos.py --mapping "C:\\path\\ids.xlsx" --apply
"""
import argparse
import re
import shutil
import sys
from pathlib import Path
from openpyxl import load_workbook

DEFAULT_ROOT = Path.home() / 'The University of Liverpool' / 'WCHADS Teencam-O365-Team - Videos to sync'
PATTERN = re.compile(r'^(#?\d{2,8})_(mom|child)_synced\.mp4$', re.IGNORECASE)
SAFE_PSEUDO = re.compile(r'^[A-Za-z0-9_-]+$')


def study_id(value):
    if value is None:
        return None
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError('Non-integer studyid found')
        value = int(value)
    text = str(value).strip().lstrip('#').strip()
    if not re.fullmatch(r'\d{2,8}', text):
        raise ValueError('studyid must have 4–8 digits, optionally prefixed with #')
    return text


def load_mapping(path):
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            raise ValueError('The Excel worksheet is empty')
        names = [str(v).strip().lower() if v is not None else '' for v in header]
        if 'studyid' not in names or 'pseudo_id' not in names:
            raise ValueError('First worksheet must have columns studyid and pseudo_id in row 1')
        si, pi = names.index('studyid'), names.index('pseudo_id')
        result, reverse = {}, {}
        for row_number, row in enumerate(rows, start=2):
            raw_id = row[si] if si < len(row) else None
            raw_pseudo = row[pi] if pi < len(row) else None
        if (raw_id is None or str(raw_id).strip() == ''
        or raw_pseudo is None or str(raw_pseudo).strip() == ''):
    		continue
            sid = study_id(raw_id)
            pseudo = str(raw_pseudo).strip()
            if not SAFE_PSEUDO.fullmatch(pseudo):
                raise ValueError(f'Unsafe pseudo_id in Excel row {row_number}; use letters, numbers, - or _')
            if sid in result and result[sid] != pseudo:
                raise ValueError(f'Conflicting pseudo IDs for studyid in Excel row {row_number}')
            if pseudo.casefold() in reverse and reverse[pseudo.casefold()] != sid:
                raise ValueError(f'Same pseudo_id assigned to different participants in Excel row {row_number}')
            result[sid] = pseudo
            reverse[pseudo.casefold()] = sid
        if not result:
            raise ValueError('No mapping entries found')
        return result
    finally:
        wb.close()


def main():
    parser = argparse.ArgumentParser(description='Preview or copy synchronised videos with pseudonymised filenames.')
    parser.add_argument('--mapping', type=Path, required=True, help='Path to Excel .xlsx mapping file')
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT, help='Research-video-sync followup root')
    parser.add_argument('--apply', action='store_true', help='Copy files (otherwise preview only)')
    args = parser.parse_args()
    source = args.root / '02_SYNCED'
    destination = args.root / '03_PSEUDO_SYNCED'
    if not source.is_dir():
        parser.error(f'Cannot find source directory: {source}')
    try:
        mapping = load_mapping(args.mapping)
    except (OSError, ValueError, StopIteration) as exc:
        parser.error(str(exc))
    if source.resolve() == destination.resolve():
        parser.error('Source and destination must be different')

    plans = []
    unmatched = 0
    for video in sorted(source.rglob('*.mp4')):
        match = PATTERN.fullmatch(video.name)
        if not match:
            continue
        sid = study_id(match.group(1))
        if sid not in mapping:
            unmatched += 1
            continue
        visible = 'Teen' if match.group(2).lower() == 'mom' else 'Mum'
        # Flat output: one pair per pseudonym, preventing identifiers in folder paths.
        target = destination / f'{mapping[sid]}_{visible}_synced.mp4'
        plans.append((video, target))

    duplicate_names = [str(t.name) for _, t in plans if sum(1 for _, other in plans if other.name.casefold() == t.name.casefold()) > 1]
    if duplicate_names:
        parser.error('Multiple input videos would produce the same output name; no files copied')

    print('Mode:', 'COPY' if args.apply else 'PREVIEW (no changes)')
    print('Recognised matched recordings:', len(plans))
    print('Recordings without a mapping entry:', unmatched)
    print('Destination:', destination)
    copied = skipped = 0
    for original, target in plans:
        if target.exists():
            print('SKIP (destination exists):', target.name)
            skipped += 1
            continue
        print(('COPY: ' if args.apply else 'WOULD COPY: ') + target.name)
        if args.apply:
            destination.mkdir(parents=True, exist_ok=True)
            # Exclusive destination creation prevents accidental overwrites.
            try:
                with original.open('rb') as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                copied += 1
            except Exception:
                if target.exists():
                    target.unlink()
                raise
    print(f'Finished. Copied: {copied}; existing outputs skipped: {skipped}.')
    if not args.apply:
        print('Preview only. Add --apply after checking the filenames.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
