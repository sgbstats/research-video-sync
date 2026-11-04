#!/usr/bin/env python3
"""Discover complete two- or three-camera TeenCam sets; preview by default."""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROLES = {'mom': ('parent view point', 'mum view point'),
         'child': ('teen view point',), 'third': ('third camera',)}
VIDEO_SUFFIXES = {'.avi', '.mp4', '.mov', '.mkv', '.m4v'}
ID = re.compile(r'^(#?\d{2,8})(?!\d)', re.I)

def discover(inbox):
    grouped = {}
    for path in sorted(inbox.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        match = ID.match(path.stem)
        if not match:
            print('IGNORED (no leading study ID):', path.name)
            continue
        pid = '#' + match.group(1).lstrip('#')
        stem = path.stem.casefold()
        hits = [role for role, names in ROLES.items() if any(name in stem for name in names)]
        if len(hits) != 1:
            print('IGNORED (ambiguous camera label):', path.name)
            continue
        grouped.setdefault(pid, {}).setdefault(hits[0], []).append(path)
    return grouped

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path, help='Folder containing 00_INBOX')
    parser.add_argument('--config', required=True, type=Path, help='Existing rg2019 config.json')
    parser.add_argument('--apply', action='store_true', help='Generate videos (default: preview only)')
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    inbox = root / '00_INBOX'
    if not inbox.is_dir(): parser.error('Missing 00_INBOX: ' + str(inbox))
    if not args.config.is_file(): parser.error('Missing config: ' + str(args.config))
    groups = discover(inbox)
    if not groups:
        print('No recognised videos in 00_INBOX.'); return 1
    ready, incomplete = [], []
    for pid, roles in sorted(groups.items()):
        if (len(roles.get('mom', [])) == 1 and len(roles.get('child', [])) == 1
                and len(roles.get('third', [])) <= 1):
            ready.append(pid)
        else:
            details = ', '.join(f'{role}={len(roles.get(role, []))}' for role in ROLES)
            incomplete.append((pid, details))
    print(f'Found {len(groups)} participant IDs; {len(ready)} valid 2- or 3-camera sets; {len(incomplete)} incomplete/duplicate sets.')
    for pid, details in incomplete: print('SKIP:', pid, details)
    if not ready: return 1
    print('Mode:', 'APPLY' if args.apply else 'PREVIEW')
    for pid in ready:
        print(f'  {pid}: {2 + (len(groups[pid].get("third", [])) == 1)} cameras')
    failed = []
    script = Path(__file__).with_name('sync_three_cameras_v2.py')
    for pid in ready:
        print('\n' + '='*60 + '\nParticipant ' + pid, flush=True)
        command = [sys.executable, str(script), '--config', str(args.config), '--root', str(root), '--id', pid]
        if args.apply: command.append('--apply')
        result = subprocess.run(command, check=False)
        if result.returncode: failed.append(pid)
    print(f'\nCompleted attempts: {len(ready)-len(failed)}/{len(ready)}; failed: {len(failed)}')
    if failed: print('Failed IDs:', ', '.join(failed))
    print('Original recordings were not moved. Review video alignment manually.')
    return 1 if failed else 0

if __name__ == '__main__':
    sys.exit(main())
