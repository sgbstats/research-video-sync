# research-video-sync

[![CI](https://github.com/sgbstats/research-video-sync/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/sgbstats/research-video-sync/actions/workflows/ci.yml)
[![Combined coverage baseline (2026-10-08): 88.6%](https://img.shields.io/badge/coverage%20baseline-88.6%25-green)](#coverage-assessment-2026-10-08)

Audio-based synchronisation of the two camera recordings (mother / child) of the RG2019 follow-up
study, designed for a **Synology-Drive-synchronised project folder** on a private Windows workstation.

> **Production = the installable `research-video-sync` command (v2).** The `rg2019` Python module and `pipeline_rg2019.py` script remain available for compatibility.
> `sync_research_project.sh` and `sync_videos.sh` are **LEGACY**, kept only for history. Do **not** use them
> on the NAS workflow: they write markers inside participant folders, trim the wrong camera
> (`sync_videos.sh`), and abort after the first participant (`set -e`). See [docs/LEGACY_AUDIT.md](docs/LEGACY_AUDIT.md).
> The two HTML guides describe that legacy workflow.

## Quick start

First [install the command](#1-prerequisites-and-installation) with Python >= 3.10. Then, in PowerShell:

```powershell
# Initialize the target folder and review its configuration.
research-video-sync --setup "D:\RG2019_CAMERAS\FOLLOWUP_2026"

# Make changes if needed.
notepad "D:\RG2019_CAMERAS\FOLLOWUP_2026\config.json"
```

Place each participant's mother and child recordings in a folder such as
`D:\RG2019_CAMERAS\FOLLOWUP_2026\00_INBOX\ID12345`, with filenames containing
`_mom` and `_child` respectively (or adjust the patterns in the config). Folders are preserved (but not strictly necessary), and videos are only matched if they are in the same folder.

Let transfers finish, then:

```powershell
# Preview first: no files are moved or encoded.
research-video-sync "D:\RG2019_CAMERAS\FOLLOWUP_2026" --dry-run

# Process the recordings; review the report and type yes when prompted.
research-video-sync "D:\RG2019_CAMERAS\FOLLOWUP_2026"
```

Both commands use **`D:\RG2019_CAMERAS\FOLLOWUP_2026\config.json`**, regardless of your
current directory. Originals are moved into `01_RAW`, synced videos are written to
`02_SYNCED`, and status/logs are stored in `99_LOGS_QC`, under the target folder with
the default configuration.

## 1. Prerequisites and installation

For a step-by-step command-line walkthrough, see the [beginner's guide](docs/COMMAND_LINE_GUIDE.md).
For unattended daily runs on Windows, see the [Task Scheduler guide](docs/WINDOWS_TASK_SCHEDULER.md).

* Windows 10/11 or Linux, Python >= 3.10.
* `ffmpeg` and `ffprobe` with libx264 and AAC. When the default names `ffmpeg` and `ffprobe`
  are not found on `PATH`, the `static-ffmpeg` dependency downloads the missing tools'
  platform binaries on first use; this needs an internet connection but does not require
  administrator privileges. Explicitly configured executable names or paths must exist;
  a missing custom executable produces an error and is never silently replaced.

Install from a source checkout (recommended until a release is published to PyPI):

```powershell
git clone https://github.com/sgbstats/research-video-sync.git
cd research-video-sync
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install .
```

On Linux, create and activate the virtual environment with:

```bash
git clone https://github.com/sgbstats/research-video-sync.git
cd research-video-sync
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

After a release is published to PyPI, the module can also be installed without cloning the repository:

```bash
python -m pip install research-video-sync
```

Then initialize the machine-specific configuration and folder layout (PowerShell example):

```powershell
research-video-sync --setup "D:\RG2019_CAMERAS\FOLLOWUP_2026"
Set-Location "D:\RG2019_CAMERAS\FOLLOWUP_2026"
notepad config.json
```

The installed `research-video-sync` command and `python -m rg2019` are equivalent. The
`pipeline_rg2019.py` script remains available for existing scheduled tasks.

`--setup` creates `config.json` inside the target follow-up directory, sets `followup_root`, and creates the configured folder layout. By default, setup uses the packaged example config. If `--config-path SOURCE` is supplied, its settings are copied into the target's `config.json` (with `followup_root` set to the target); the source file is left unchanged. `--config` and `--config_path` are aliases. If a destination config already exists, setup asks before replacing it.

Run without arguments to process the current directory, or pass a directory path to process that directory:

```powershell
research-video-sync
research-video-sync "D:\RG2019_CAMERAS\FOLLOWUP_2026"
research-video-sync "D:\RG2019_CAMERAS\FOLLOWUP_2026" --dry-run
```

### Which config file is used?

| Command | Config file loaded | Data root used |
|---|---|---|
| `research-video-sync` | `config.json` in the current working directory | Current working directory |
| `research-video-sync "D:\RG2019_CAMERAS\FOLLOWUP_2026"` | `D:\RG2019_CAMERAS\FOLLOWUP_2026\config.json` | The supplied target directory |
| `research-video-sync --config-path PATH` | The explicitly specified file | Its configured `followup_root` |

**Passing a directory uses that target directory's config, not the current directory's config.**
Without an explicit config option, the selected directory overrides `followup_root` for that
run without rewriting the file. The directory and a readable, valid config must exist;
missing or invalid configs stop the run with `CONFIG ERROR` and exit code `3` before tool
checks or processing. There is **no fallback** to a config in the current directory.
Use `--setup DIRECTORY` to initialize a missing config.

`--config-path PATH` (also `--config` or `--config_path`) takes precedence over both the
current working directory and any directory argument: it loads the specified config and
uses its configured `followup_root`. Relative config paths are resolved from the current
working directory.

`config.json` is git-ignored (machine-specific). The root and packaged example configs contain only fake paths.

### v2 upgrade notes

The processing defaults intentionally changed at the user's request: `stability_minutes`
is now `0` rather than `120`, and `create_side_by_side` is now `true` rather than `false`.
This affects both new configs and existing configs that omit these keys; explicitly saved
values are retained. The zero-minute default disables the minimum-age and prior-observation
wait, but keeps transfer-file blocking and the growth re-check. Side-by-side output adds an
extra encode and requires additional processing time and disk space. To preserve the previous
behavior, explicitly set `"stability_minutes": 120` and `"create_side_by_side": false` before
upgrading.

Pair processing now defaults to **two concurrent pairs**, including existing configs that omit
`max_parallel_pairs`. Set `"max_parallel_pairs": 1` to retain sequential processing.

## 2. Architecture

```
FOLLOWUP_2026/                      <- followup_root (whole tree synced to the Synology NAS)
├── 00_INBOX/IDxxxx/                <- written by the shared university PC (may still be syncing!)
├── 01_RAW/IDxxxx/                  <- preserved originals: ONLY original material, never modified
├── 02_SYNCED/IDxxxx/               <- IDxxxx_mom_synced.mp4, IDxxxx_child_synced.mp4 (+ optional side_by_side)
├── 03_WORKSPACE/  04_FACEREADER_OUTPUT/  05_ANALYTIC_DATA/     (not touched by this pipeline)
└── 99_LOGS_QC/                     <- ALL pipeline state, logs and QC files
    ├── state/IDxxxx.json           <- per-participant state (source of truth)
    ├── pipeline_status.csv         <- human-readable master table (regenerated from state)
    ├── logs/pipeline_YYYY-MM-DD.log
    └── pipeline.lock               <- exists only while a run is active
```

Daily flow per participant:

```
00_INBOX/IDxxxx --ready? stable? one mom + one child? ffprobe OK? SHA-256--> MOVE --> 01_RAW/IDxxxx
                                                                                        │ (read-only from here)
                                     02_SYNCED/IDxxxx  <-- offset estimate + trim/encode ┘
```

### How RAW protection works

* Material is **moved** (not copied) from INBOX to RAW, file by file; an existing file/folder in RAW is **never overwritten**.
  If `01_RAW/IDxxxx` already exists while INBOX also holds material for it -> `RAW_CONFLICT`, nothing is touched.
* After promotion the code only *opens RAW files for reading* (`ffprobe`, `ffmpeg -i`). It never writes, renames or
  deletes anything in `01_RAW`, and never puts markers/state there (that is why `.sync_done` is gone).
* Each RAW source's size, modified time (`mtime_ns`) and SHA-256 are recorded at ingest. Every run does a cheap check
  (existence + size + mtime, no file reads):
  * size differs -> `RAW_CONFLICT` immediately;
  * size identical but mtime differs -> that one file's SHA-256 is recomputed **once** and compared with the stored hash:
    different -> `RAW_CONFLICT`; identical -> content is unchanged, the new mtime is recorded **in the state file only**
    (RAW itself is never modified) so it is not hashed again;
  * unchanged files are never re-hashed. *Limit:* an edit that preserves both the size **and** the modified time is not
    detectable by the daily check (it would need hashing every run); the stored SHA-256 is there for manual audit.
* SHA-256 is computed **once** (in INBOX, before the move) and stored in the state file; it is not recomputed on daily runs.
* This is protection *by program design*. For protection *by the operating system*, also make `01_RAW` read-only
  for everyone else (NAS/Synology permissions; the shared PC should only ever have read access to RAW).

## 3. Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `followup_root` | *(required)* | e.g. `D:\\RG2019_CAMERAS\\FOLLOWUP_2026` |
| `inbox`, `raw`, `synced`, `logs_qc` | `00_INBOX`, `01_RAW`, `02_SYNCED`, `99_LOGS_QC` | folder names under the root (INBOX and RAW must be on the same volume) |
| `participant_id_regex` | `^ID\d{4,8}$` | valid participant folder names (e.g. `ID100392`) |
| `mom_pattern`, `child_pattern` | `_mom`, `_child` | case-insensitive substring of the file name |
| `video_extensions` | mp4, avi, mov, mkv | |
| `require_ready_marker` | `false` | if `true`, `00_INBOX/IDxxxx/READY.txt` must exist |
| `stability_minutes` | `0` | age rule + prior-observation timer (see below). `0` disables **only those two**: transfer-file blocking and the growth re-check stay active |
| `stability_recheck_seconds` | `5` | re-stat files after this pause to catch files still growing; active **even when `stability_minutes` is `0`** (set this to `0` to switch it off) |
| `stability_requires_prior_observation` | `true` | files must also have been seen unchanged by a *previous run* (see below) |
| `create_side_by_side` | **`true`** | also create `IDxxxx_side_by_side.mp4` (large) |
| `video_description` | `null` | optional filename-safe label; `"pilot visit"` produces `IDxxxx_pilot_visit_mom_synced.mp4` (and labels child and side-by-side outputs) |
| `require_approval` | `true` | require typing `yes` after the pre-run report on real runs |
| `max_attempts` | `3` | automatic retries for `SYNC_FAILED`/`ENCODE_FAILED` |
| `max_parallel_pairs` | `2` | maximum simultaneous participant pairs; positive integer; `1` runs sequentially |
| `sync.*` | see file | sample rate (8000 Hz), `max_lag_seconds` (120), window length (60 s), thresholds; `min_agreeing_windows` must not exceed `fine_windows` |
| `encode.*` | `fast`, CRF 20, 192k | x264 preset/CRF, AAC bitrate, `copy_untrimmed_when_possible` |
| `ffmpeg`, `ffprobe` | `ffmpeg`, `ffprobe` | names on `PATH`, managed fallback, or absolute paths |

Unknown keys are rejected (typo protection).

## 4. Running

```bat
:: 1) ALWAYS start with a dry run: nothing is moved, encoded, written or deleted
research-video-sync --config config.json --dry-run

:: 2) real run (this is what the scheduled task does)
research-video-sync --config config.json
```

Other options: `--setup FOLLOWUP_ROOT` (create config and folder structure), `--participant IDxxxx` (repeatable; restrict to those IDs), `--reprocess IDxxxx`, `--manual-offset SECONDS`, `--yolo` (skip approval and set stability minutes to zero for this invocation),
`--workers N` (override the pair limit without editing config), `--log-level DEBUG`. Exit code `0` = fine, `1` = real run cancelled at approval, `2` = something needs manual review/failed, `3` = config/tool/lock problem, `130` = interrupted.
The run ends with a summary (Newly completed / Skipped completed / Waiting / Manual review / Failed, with IDs).
`--yolo` still prints the pre-run report and retains transfer-file detection, the configured growth re-check, and the run lock. Changing `video_description` for existing outputs requires `--reprocess`; prior files are archived.
The pre-run report lists videos under INBOX and RAW that have no unique participant ID matching `participant_id_regex` or match neither `mom_pattern` nor `child_pattern`, including files in nested folders. Each entry shows its path and the unmatched patterns; mother/child patterns remain case-insensitive substrings, not regular expressions. With `--participant`, this list is restricted to videos identified as belonging to the selected participants.
If the pre-run report finds no videos eligible to sync (including when all are already synced or waiting), the command prints the report and exits without asking for approval or starting a full run. It records any first stability observations so a later run can proceed. Review problems still return exit code `2`.
Daily scheduling: [docs/WINDOWS_TASK_SCHEDULER.md](docs/WINDOWS_TASK_SCHEDULER.md).

### Parallel pairs and restarting interrupted work

Readiness checks and INBOX promotion are coordinated before independent RAW-to-SYNCED jobs
run in a bounded worker pool. Two different participant pairs can synchronize/encode at once;
the camera outputs within each pair remain sequential. Shared inbox folders and READY markers
remain supported. The pre-run report shows the worker limit. Dry runs remain sequential and read-only.

```powershell
research-video-sync --config config.json --workers 2
# Use one pair at a time on a resource-constrained workstation:
research-video-sync --config config.json --workers 1
# After interruption, rerun the normal command:
research-video-sync --config config.json
```

Completed audio extractions are checkpointed in `99_LOGS_QC\work\<ID>`; offsets and output
publication records are saved atomically in the participant's state file. A restart reuses valid
completed audio, saved offsets, and committed videos, including a video published just before
the process was killed. An unfinished individual encode restarts from its beginning, not from
the last frame. An interrupted correlation calculation is recalculated unless its offset was saved.
Committed outputs retain their SHA-256 and are checked whenever reused, including same-size edits.
Unrelated or altered final outputs are conflicts and are never silently adopted or overwritten.
Legacy output records without a checksum retain size-only validation; deliberate reprocessing
creates checksum-protected outputs. Full output checks add disk reads on subsequent runs.
Publication never replaces an existing filename: Windows uses a no-replace rename, and POSIX
uses an atomic hard link followed by removal of the partial name. POSIX output storage must
support hard links; an unsupported filesystem produces an explicit failure, not an unsafe fallback.
Do **not** use `--reprocess` to resume: that deliberately archives outputs and discards prior sync work.

Allow scratch space for mono PCM: at 8000 Hz, 16-bit, each camera uses approximately 58 MB per
hour. Checkpoints for unfinished pairs remain until successful completion or deliberate reprocessing.
Keep state and work files private: the PCM files contain study audio. Consider excluding
`99_LOGS_QC\work` from Synology Drive synchronization, and do not edit/delete checkpoints during a run.
The CSV is a regenerated report, not the resume source of truth; after a hard kill it may lag the JSON state.

Owned FFmpeg processes are stopped on cancellation or pipeline termination. A verified dead
local lock owner can be recovered immediately; live or unverifiable owners remain protected.
Existing retry limits and manual-review rules still apply. Checkpoints protect process-interruption
boundaries, not arbitrary disk corruption or power-loss durability.

FFmpeg already uses multiple CPU threads, so more workers do not guarantee higher throughput.
Start with two, reduce to one if CPU, memory, or storage contention is excessive, and increase only
after measuring representative recordings. Encoding remains CPU-based; GPU support is not added here.

**Dry run** does read-only work only: it applies the readiness/stability rules, discovers and `ffprobe`s the videos and
prints the exact steps that *would* happen. It does not hash, move, extract audio, encode, or write state/CSV/log files.
(It also cannot know the sync offset before audio analysis, so it reports the encode step generically.)

> **Dry-run and the prior-observation timer.** When `stability_minutes>0` and
> `stability_requires_prior_observation=true`, a new participant must first be *seen unchanged by a previous real run*. A dry-run deliberately
> writes no state, so it can never record that first observation: repeated dry-runs will keep reporting new participants
> as waiting ("first observation ..."). The pipeline prints a warning (and repeats it in the summary) when this applies.
> For a **one-off full pilot dry-run**, temporarily set `"stability_requires_prior_observation": false` in your config -
> the dry-run itself stays non-mutating - and **restore it to `true` before real/scheduled runs**.

### Readiness: what "not still synchronising" means

For a participant in INBOX the pipeline waits (`WAITING_FOR_READY` / `WAITING_FOR_STABILITY`) unless **all** of these hold:

1. if `require_ready_marker`: `READY.txt` exists (the shared PC should create it **last**);
2. **always, regardless of `stability_minutes`:** no transfer-in-progress files are present anywhere in the participant
   folder (`.tmp`, `.partial`, `.part`, `.crdownload`, `.download`, `.filepart`, `.~*`, `~$*`, `.syno*`) - such a file blocks
   processing and nothing is moved into RAW;
3. only if `stability_minutes>0`: no file was modified in the last `stability_minutes`;
4. only if `stability_minutes>0` and `stability_requires_prior_observation`: the same file list/sizes/mtimes were already recorded
   by an earlier real run at least `stability_minutes` ago - Synology Drive preserves the original file mtime, so mtime alone
   cannot show that a download just finished. When this rule is enabled, a new participant is first *seen* on one run and
   processed on a later run (normally the next day). Set it to `false` to accept same-day processing using the other rules;
5. **always while `stability_recheck_seconds>0`** (default 5, also when `stability_minutes=0`): the list of files, their sizes and
   mtimes are identical after that pause (files that *appear or disappear* during the pause count as changes), so a file that is
   still growing or arriving is caught. This is the safer behaviour; set `stability_recheck_seconds`
   to `0` only if you accept that risk.

`stability_minutes=0` therefore disables only rules 3 and 4.

`READY.txt` can reach the workstation *before* large videos finish downloading, so the stability rules still apply when it is required.
`READY.txt` is removed from INBOX after promotion and is never copied to RAW.

### Which files are chosen

Pairs may live directly in `00_INBOX` or together in any subfolder. The participant ID is found in each video's
filename first, then in its containing path; each pair must have exactly one `_mom` and one `_child` video with the
same ID **in the same directory**. Multiple IDs may share a directory. A missing role, duplicate role, or a pair
split across folders is flagged for manual review; no first match is silently chosen. Unmatched extra videos are not
selected for processing.
The relative directory structure is preserved in `01_RAW` and `02_SYNCED`. For example, a pair in
`00_INBOX/batch/` produces outputs in `02_SYNCED/batch/`. Existing ID-only folders retain their legacy behavior,
including promotion of other original material (notes etc.); flexible layouts move the two selected videos only.

### Synchronisation

`offset_seconds = t_mom(event) - t_child(event)` (= child start - mom start).
`offset > 0`: the mother camera started **earlier**, so `offset` seconds are trimmed from the **start of the mother** video;
`offset < 0`: `|offset|` seconds are trimmed from the **start of the child** video. After trimming, both outputs start at
the same real-world moment. Outputs are not cut at the end (the two files may differ in length).

Estimator (details in `rg2019/syncest.py`): mono audio is decoded by ffmpeg at 8 kHz to a temp file and memory-mapped
(RAM use is one analysis window, not the whole recording). *Coarse stage*: 7 windows spread over the recording are
matched (normalised cross-correlation via FFT) against the other file within +-`max_lag_seconds`; a cluster of agreeing
windows gives the coarse offset. *Fine stage*: 5 windows spread over the whole overlap (inset by the +-2 s search range so every window lies inside both files), searched +-2 s,
sub-sample interpolation, median. Windows must be **distinct** (start >= half a window apart); if the aligned overlap is too short
to hold enough distinct windows the result is `LOW_CONFIDENCE`, never a success built from repeated copies of one window.
**SUCCESS requires** >= `min_agreeing_windows` (3) distinct fine windows (>= 60 %) agreeing within 0.25 s, each with a clear correlation
peak (NCC >= 0.05, peak >= 1.5x any competing peak), and a coarse consensus. Otherwise `LOW_CONFIDENCE` (no videos are made).
`sync_confidence` = median NCC of the agreeing windows x fraction agreeing (0-1). The per-window results and the
spread (max-min of the window offsets, a clock-drift indicator) are stored in the state file.
Assumption: a **constant** offset (no clock-drift correction).

### Encoding decisions

* The camera that needs **no trim** is stream-copied (no quality loss, fast) when it is H.264/HEVC + AAC/MP3; otherwise re-encoded.
* The **trimmed** camera is always re-encoded (frame-accurate cut): libx264, CRF 20, preset `fast`, yuv420p, AAC 192k, `+faststart`.
* Only the first video and first audio stream are kept in the outputs (RAW keeps everything).
* Each output is written as `*.mp4.partial`, validated with `ffprobe` (video+audio, duration within 1.5 s of expected), then renamed atomically.

## 5. Statuses

| Status | Meaning | What happens next |
|---|---|---|
| `SUCCESS` | in RAW, synced, outputs recorded | skipped on later runs |
| `WAITING_FOR_READY` / `WAITING_FOR_STABILITY` | not ready / possibly still syncing | re-checked every run |
| `MISSING_FILES` / `AMBIGUOUS_FILES` | no / several mom or child videos | stays in INBOX; fix the folder, re-checked every run |
| `INVALID_VIDEO` / `NO_AUDIO` | unreadable, no video stream, zero duration / no audio stream | stays in INBOX; re-checked every run |
| `INVALID_ID` | folder name not a valid participant id | reported only; nothing recorded |
| `RAW_CONFLICT` | RAW folder exists with different/incoming material, or RAW changed after validation | **manual intervention**; nothing touched |
| `LOW_CONFIDENCE` | offset not trustworthy | in RAW, **no videos made, not retried automatically**; review, then `--reprocess` / `--manual-offset` |
| `SYNC_FAILED` / `ENCODE_FAILED` | ffmpeg/ffprobe error, disk full ... | retried each run up to `max_attempts`, then needs `--reprocess` |
| `OUTPUT_CONFLICT` | a file exists in `02_SYNCED` that the state does not know | never overwritten; move/rename it, re-run |
| `PROMOTE_FAILED` | the move INBOX -> RAW hit an OS error (e.g. locked file) | safe; resumes on the next run |
| `INTERNAL_ERROR` | unexpected exception (see log) | other participants unaffected |
| `RAW_PROMOTED` | transient: in RAW, sync not finished | resumes on the next run |

## 6. Manual reprocessing (safely)

```bat
:: what would happen?
research-video-sync --config config.json --participant ID100392 --reprocess ID100392 --dry-run
:: recompute from RAW (previous outputs are MOVED to 02_SYNCED\ID100392\_superseded_<timestamp>\, never deleted)
research-video-sync --config config.json --participant ID100392 --reprocess ID100392
:: accept a reviewed offset for a LOW_CONFIDENCE participant (positive trims mom, negative trims child)
research-video-sync --config config.json --participant ID100392 --reprocess ID100392 --manual-offset 12.34
```

To re-run after fixing a problem that is not a sync problem (e.g. a moved conflicting file) no flag is needed.
Never edit `01_RAW`. Do not delete state files casually: without state a finished participant has un-recorded outputs
and becomes `OUTPUT_CONFLICT` (safe, but needs a manual step). Turning `create_side_by_side` on later does **not**
retro-generate files for completed participants; use `--reprocess` for those you want. Once a side-by-side file has been
created and `create_side_by_side` is on, it is part of the completion check: if it is later deleted it is regenerated from the
synced videos on the next run; if it exists but differs from the recorded size or SHA-256 it is flagged `OUTPUT_CONFLICT` and never overwritten.

## 7. Troubleshooting

* *`ffmpeg was not found`* - check the configured executable names/absolute paths and internet access
  for the first managed-binary download (scheduled tasks often have a limited `PATH`).
* *Participant stays in WAITING in every dry-run* - if `stability_minutes>0` and prior observation is required, dry-runs cannot record the observation; see the dry-run note above.
* *Participant stays in WAITING* - read the message in `pipeline_status.csv` (`error_message`) or the log; check for transfer-in-progress files and the growth re-check, which remain active when `stability_minutes=0`. If age/prior-observation checks are enabled, adjust `stability_minutes` or set `stability_requires_prior_observation=false`.
* *`another pipeline run appears to be active`* - a run is active or its owner cannot be verified.
  Verified dead local owners are recovered immediately; live local owners are never taken over just
  because their heartbeat is old. Structured foreign/unverifiable owners remain protected;
  legacy or malformed locks retain age-based stale recovery.
  Do not delete a lock until you have confirmed no pipeline or its encoders are still running.
  The persistent `pipeline.lock.guard` file is an OS-locking aid, not evidence of an active run;
  its initialization is serialized under the OS lock, including on Windows. Do not delete it
  while processes could be using it.
* *`PROMOTE_FAILED`* - a file was open/locked (Synology Drive, antivirus); just re-run. INBOX and RAW must be on one volume.
* *`LOW_CONFIDENCE`* - inspect `99_LOGS_QC\state\IDxxxx.json` (`sync.details`): quiet recordings, camera mics far apart,
  an offset larger than `max_lag_seconds`. Play both videos, decide, use `--manual-offset`.

## 8. Development and tests

```bash
python -m pip install -e ".[test]"
python -m pytest            # uses only synthetic media generated on the fly
python -m pytest --cov --cov-report=term-missing --cov-report=xml --cov-report=html
python -m build             # build source and wheel distributions
```

The test extra includes the build backend requirements (`setuptools>=68` and `wheel`)
because the wheel compatibility test builds without an isolated environment.

GitHub Actions runs tests and builds distributions on pushes and pull requests for Python 3.10, 3.12 and 3.13 on
Windows and Linux. Each matrix job measures line and branch coverage for `rg2019` and the compatibility
entry point, prints uncovered lines in its test log, and retains XML and HTML coverage reports as a
`coverage-<os>-python-<version>` artifact for 14 days. Download an artifact from the CI run and open
`htmlcov/index.html` for the annotated source report. The CI badge shows status on `main`; the
coverage baseline badge shows the dated combined coverage assessment below and is updated manually,
not by CI. Coverage does not currently enforce a minimum percentage or include the
legacy shell scripts; Python subprocess coverage is not collected.

Publishing is triggered by a published GitHub release; configure the `research-video-sync`
trusted publisher on PyPI for the repository and `pypi` environment before publishing.

Layout: `research-video-sync` (installed command), `rg2019/` (Python module), `pipeline_rg2019.py` (compatibility entry point), `tests/`, `docs/`.
**Never commit research data**: this repository is public and `.gitignore` excludes media, tables,
logs, participant folders, pipeline state and local configs. Tests never read real study files.

### Coverage assessment (2026-10-08)

The local Windows / Python 3.14.2 baseline measured **90.6% line coverage** (1,926/2,127
statements), **82.8% branch coverage** (608/734 branches), and **88.6% combined coverage**.
These are a dated assessment, not the live CI matrix results.

The suite exercises synthetic-media synchronization, RAW/output integrity, restart checkpoints,
parallel processing, configuration, CLI behavior, packaging, and process ownership. The main
reported gaps are `processes.py` (71% combined), `pipeline.py` (88%), and `cli.py` (89%).
Prioritize promotion/recovery error paths and CLI interruption handling when expanding tests.
The process coverage figure includes Linux-only paths that cannot execute on Windows; module
entry points and child-process execution also appear uncovered because subprocess coverage is
not collected. Read these figures as execution coverage, not proof of correctness.

The full run had 297 passing tests, one platform skip, and one timing-sensitive failure:
`test_cli_dry_run_lists_videos_waiting_for_stability` expects recently modified files, but its
session-cached video fixtures had aged beyond the configured 10-minute window during the run.
It passed independently with fresh fixtures. That fixture-age coupling remains unresolved.

## 9. Assumptions and known limits

* Constant offset assumed (drift is reported as `offset_spread_seconds`, not corrected).
* First audio stream of each file is used; both cameras must record the same room sound.
* Not tested on Windows itself (developed on Linux); path handling is `pathlib` throughout and a Windows-path unit test exists.
* Very large offsets (> `max_lag_seconds`) are reported `LOW_CONFIDENCE`, not guessed.
