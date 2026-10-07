# Beginner's guide: running the video-sync tool from the command line

This guide is for Windows users who want to run the video-sync pipeline from a terminal after getting its code from GitHub. The production command is `research-video-sync`; `pipeline_rg2019.py` remains available for compatibility. **Do not run the `.sh` scripts**; they are legacy scripts and are not for the current workflow.

The pipeline moves original videos from `00_INBOX` into `01_RAW`, then creates synchronized outputs under `02_SYNCED`. Treat a real run as a file operation, not just a preview.

## 1. Prerequisites

Install these before running the pipeline:

| Requirement | Why it is needed |
|---|---|
| **Git for Windows** | Downloads (clones) the project from GitHub. |
| **Python 3.10 or newer** | Runs the pipeline. During installation, enable the option to add Python to `PATH` if offered. |
| **FFmpeg, including `ffprobe`** | Reads and encodes the video and audio. The FFmpeg build must support `libx264` and AAC. |

Install Git and Python from their official websites, and install FFmpeg using your organization's approved method. The commands below use Windows Command Prompt. Open a new Command Prompt after installing, then check that Git and Python are available:

```bat
git --version
python --version
```

Python should report version 3.10 or newer. If either command is not recognized, install the missing program or correct its `PATH` before continuing. FFmpeg and `ffprobe` must also be available on `PATH`, or their full paths must be set in the configuration file.

> If you encounter platform-specific problems, check the troubleshooting section below and the project README.

## 2. Get the project from GitHub

Choose a folder where you want to keep the program, open Command Prompt, and run:

```bat
git clone https://github.com/chmlz/research-video-sync.git
cd research-video-sync
```

or 

```bat
git clone https://github.com/sgbstats/research-video-sync.git
cd research-video-sync
```

If the repository is **already cloned**, do not clone it again; open Command Prompt and change to that existing `research-video-sync` folder instead.

## 3. Install the Python packages

From the repository folder, install the package and its dependencies:

```bat
python -m pip install .
```

This installs the `research-video-sync` command and required packages. If `ffmpeg` or
`ffprobe` is not on `PATH`, their platform binaries are downloaded on first use. If
`python` is not recognized but the Python launcher is installed, try using `py` in place
of `python` in the commands in this guide.

## 4. Set up your configuration

Create a local configuration and the default follow-up folder structure in one step:

```bat
research-video-sync --setup "D:\RG2019_CAMERAS\FOLLOWUP_2026"
notepad config.json
```

`--setup` copies the project defaults into `config.json`, sets `followup_root` to the supplied path, and creates the configured folders if missing. An absolute path is recommended. If the config already exists, setup asks before replacing it; answering `yes` continues folder creation, while any other response cancels without changes. You can choose a different config destination with `--config`, for example `research-video-sync --setup "D:\RG2019_CAMERAS\FOLLOWUP_2026" --config "D:\settings\rg2019.json"`.

If setting up manually instead, set `followup_root` in `config.json` to the full path of your actual project data folder. For example:

```json
"followup_root": "D:\\RG2019_CAMERAS\\FOLLOWUP_2026"
```

Use your own correct path; the example path is not a real location. The project folder should contain `00_INBOX`, where incoming participant folders arrive. By default, the pipeline also uses `01_RAW`, `02_SYNCED`, and `99_LOGS_QC` beneath that root. If FFmpeg is not on `PATH`, edit the `ffmpeg` and `ffprobe` settings to their full executable paths.

Keep `config.json` private to your machine. It is intentionally excluded from Git; only the example configuration should be committed.

Set `"video_description": null` (default) to keep names such as `ID100392_mom_synced.mp4`, or set a filename-safe label such as `"pilot visit"` to write `ID100392_pilot_visit_mom_synced.mp4` (also applied to child and side-by-side outputs). Changing it for completed pairs requires `--reprocess` to archive old outputs. `"require_approval": true` (default) prompts for `yes` before a real run; setting it to `false` skips the prompt but retains the report.

### Configuration reference

The keys below are the complete set accepted by the current Python pipeline. Defaults are used when a key is omitted; `followup_root` is required. `config.example.json` shows a shorter configuration because it omits some settings that already have defaults. Unknown keys (except keys beginning with `_`, used for comments) are rejected.

| Key | Default | Purpose |
|---|---|---|
| `followup_root` | Required | Folder containing the INBOX, RAW, SYNCED and log folders. Use an absolute path; escape backslashes in JSON. |
| `inbox` | `00_INBOX` | Incoming originals. |
| `raw` | `01_RAW` | Preserved originals; must be on the same volume as INBOX. Do not edit files here. |
| `synced` | `02_SYNCED` | Synchronized output videos. |
| `logs_qc` | `99_LOGS_QC` | State files, CSV, logs and the run lock. |
| `participant_id_regex` | `^ID\\d{4,8}$` | Full-match pattern for IDs. In JSON, escape `\` as `\\`; for example, use `"^#\\d{4,8}$"` for IDs such as `#0000`. |
| `mom_pattern` | `_mom` | Case-insensitive substring marking a mother video. |
| `child_pattern` | `_child` | Case-insensitive substring marking a child video; must differ from `mom_pattern`. |
| `video_description` | `null` | Optional filename label inserted after the ID for both synchronized videos and optional side-by-side output. A non-null label may contain letters, digits, single spaces, `_` or `-` between alphanumeric segments (maximum 80 characters); spaces become underscores. Changing it for completed pairs requires `--reprocess`. |
| `video_extensions` | `[".mp4", ".avi", ".mov", ".mkv"]` | Accepted source extensions; matching is case-insensitive. |
| `create_side_by_side` | `false` | Also create `<ID>[_description]_side_by_side.mp4`. Enable before processing or use `--reprocess` for completed pairs. |
| `require_approval` | `true` | Prompt for `yes` on real runs with eligible videos. A report with no eligible videos does not prompt. Set to `false` for unattended runs. |
| `require_ready_marker` | `false` | Require a `READY.txt` marker for incoming material when enabled. |
| `ready_marker_name` | `READY.txt` | Name of that marker file. |
| `stability_minutes` | `120` | Minimum source-file age and unchanged prior-observation interval; `0` disables those two checks only. |
| `stability_recheck_seconds` | `5` | Pause, then check again for additions, removals or file changes; remains active when `stability_minutes` is `0`. Use `0` to disable this recheck. |
| `stability_requires_prior_observation` | `true` | With positive `stability_minutes`, require a previous real-run observation of the unchanged files. Dry runs never save observations. |
| `max_attempts` | `3` | Maximum automatic retries for sync/encode failures before manual reprocessing. |
| `lock_stale_hours` | `24` | A run lock with no heartbeat for this long can be replaced. |
| `ffmpeg` | `ffmpeg` | Executable name on `PATH`, or full path to FFmpeg. |
| `ffprobe` | `ffprobe` | Executable name on `PATH`, or full path to ffprobe. |

Transfer-in-progress files block processing regardless of the stability-minute setting. The `sync` and `encode` keys are optional **JSON objects** with these subkeys (write them inside their corresponding object, as in `config.example.json`):

| Key | Default | Purpose |
|---|---|---|
| `sync.sample_rate` | `8000` | Audio analysis sample rate (Hz). |
| `sync.max_lag_seconds` | `120.0` | Largest offset searched in the coarse stage (seconds). |
| `sync.window_seconds` | `60.0` | Analysis window length (seconds). |
| `sync.coarse_windows` | `7` | Number of coarse analysis windows. |
| `sync.coarse_tolerance_seconds` | `0.5` | Coarse window offset agreement tolerance (seconds). |
| `sync.fine_windows` | `5` | Number of fine analysis windows. |
| `sync.fine_search_seconds` | `2.0` | Search range around the coarse offset (± seconds). |
| `sync.fine_tolerance_seconds` | `0.25` | Fine window agreement tolerance (seconds). |
| `sync.min_ncc` | `0.05` | Minimum normalized correlation peak. |
| `sync.min_peak_ratio` | `1.5` | Minimum ratio of the best peak to a competing peak. |
| `sync.min_agreeing_windows` | `3` | Minimum agreeing fine windows; cannot exceed `sync.fine_windows`. |
| `sync.min_agree_fraction` | `0.6` | Minimum fraction of fine windows agreeing. |
| `sync.min_overlap_seconds` | `10.0` | Minimum overlap to estimate an offset (seconds). |
| `sync.silence_rms` | `0.0001` | Audio level below which a window is treated as silent. |
| `encode.video_codec` | `libx264` | Video encoder for re-encoded output. |
| `encode.preset` | `fast` | Encoding speed/quality preset. |
| `encode.crf` | `20` | Constant-rate-factor quality setting. |
| `encode.audio_bitrate` | `192k` | Encoded audio bitrate. |
| `encode.copy_untrimmed_when_possible` | `true` | Stream-copy compatible, untrimmed cameras rather than re-encoding. |
| `encode.min_trim_seconds` | `0.02` | Treat smaller absolute offsets as zero for trimming. |
| `encode.duration_tolerance_seconds` | `1.5` | Allowed difference between expected and encoded duration (seconds). |

### Local conventions

For WCHADS data, change the config.json to:

```json
  "participant_id_regex": "^#\\d{4,8}$",
  "mom_pattern": "mum view point",
  "child_pattern": "teen view point",
  "create_side_by_side": true
```

## 5. Preview the run first

Always start with a dry run:

```bat
research-video-sync --config config.json --dry-run
```

Read the output and make sure the folders and participant videos it identifies are the ones you expect. A dry run does not create folders or write state; it lists matching videos and reports those waiting for stability. A real run prints a pre-run inventory of source videos to sync and those skipped or blocked (including missing/ambiguous matches, unsupported suffixes, and already-synced participants). When videos are eligible and `require_approval` is `true`, it requires you to type `yes` before processing. Any other response cancels and returns exit code `1`.
If no videos are eligible to sync, the real-run command prints the report and exits without requesting approval or starting full processing. This includes pairs waiting for stability; first observations are recorded so a later run can recheck them. Review problems still return exit code `2`.

With the default settings, a new participant can remain in a waiting status during a dry run. That is expected: the default stability check requires a prior **real** run to have observed the files unchanged. Repeating dry runs will not satisfy that check because dry runs do not save observations. The README explains how to do a one-off pilot dry run without changing files.

## 6. Run the pipeline

Only after reviewing the dry-run output, start a real run:

```bat
research-video-sync --config config.json
```

The pipeline waits until incoming files appear stable. With the default configuration, files must be at least 120 minutes old and must have been observed unchanged during an earlier real run; consequently, a new participant will normally wait until a later run. Transfer-in-progress files also block processing.

The pipeline accepts pairs directly in `00_INBOX` or in nested folders. Each participant ID needs exactly one `_mom` and one `_child` video in the same folder; the ID comes from the filenames or, if absent there, the folder path. Multiple ID pairs may share a folder. Source folders are preserved in `01_RAW` and `02_SYNCED`; status and logs are recorded under `99_LOGS_QC`. **Do not edit files in `01_RAW`.**

## 7. Useful command options

Replace `ID100392` with the participant ID you intend to process.

| What you want to do | Example |
|---|---|
| Process only one participant | `research-video-sync --config config.json --participant ID100392` |
| Preview one participant | `research-video-sync --config config.json --participant ID100392 --dry-run` |
| Reprocess a participant from `01_RAW` | `research-video-sync --config config.json --participant ID100392 --reprocess ID100392` |
| Show more diagnostic detail | `research-video-sync --config config.json --log-level DEBUG` |
| Skip approval and the stability-minute wait for this run | `research-video-sync --config config.json --yolo` |
| Use a manually reviewed offset | `research-video-sync --config config.json --participant ID100392 --reprocess ID100392 --manual-offset 12.34` |

Reprocessing archives known previous outputs under a `_superseded_...` folder rather than deleting them. A manual offset should only be used after reviewing the videos and deciding the correct offset. It requires exactly one `--participant`; a positive offset trims the mother video, and a negative offset trims the child video. See the README for the full command and details.
`--yolo` still prints the report and retains transfer-file blocking, the growth re-check, and the run lock; it does not change your saved config. With `--dry-run`, it remains read-only.

### CLI option reference

These are all command-line options supported by `research-video-sync`:

| Option | Default | Effect |
|---|---|---|
| `--config PATH` | `config.json` | Load this JSON configuration; with `--setup`, write the generated configuration here. |
| `--setup FOLLOWUP_ROOT` | Not set | Initialize a config and its folders, then exit. Existing configs require confirmation before overwrite. |
| `--dry-run` | Off | Preview decisions without moving files, writing state or encoding. It does not satisfy prior-observation stability. |
| `--yolo` | Off | For this invocation, set `stability_minutes=0` and skip approval. Transfer-file blocking, the configured recheck and locking remain. Combining with `--dry-run` stays read-only. |
| `--participant ID` | All IDs | Restrict processing to this ID; repeat to select several. |
| `--reprocess ID` | No IDs | Recompute from RAW for this ID and archive existing outputs; repeat for several IDs. |
| `--manual-offset SECONDS` | Automatic estimation | Use a reviewed offset with exactly one `--participant` (`+` trims mother, `-` trims child). Add `--reprocess ID` when changing a completed pair. |
| `--log-level {INFO,DEBUG}` | `INFO` | Set logging detail. |
| `--version` | — | Show the program version and exit. |
| `-h`, `--help` | — | Show command help and exit. |

## 8. Check results and troubleshoot

Look at `99_LOGS_QC\\pipeline_status.csv` for the participant statuses and messages. The dated log files are in `99_LOGS_QC\\logs`. The command exits with code `0` when no human action is needed, `2` when something needs review or failed, and `3` for a configuration, tool, or lock problem.

Common problems:

| Message or situation | What to check |
|---|---|
| `ffmpeg was not found` | Install FFmpeg or set full paths for `ffmpeg` and `ffprobe` in `config.json`. |
| Participant stays in a waiting status | Check `pipeline_status.csv` and the log. Waiting on a new participant is expected with the default prior-observation setting. |
| `MISSING_FILES` or `AMBIGUOUS_FILES` | Confirm there is exactly one `_mom` video and one `_child` video in the participant's inbox folder. |
| `LOW_CONFIDENCE` | Review the recordings and status details; do not assume the estimated offset is correct. See the README before reprocessing or using a manual offset. |
| `PROMOTE_FAILED` | A file may be locked by another program. Close it and run the pipeline again. `00_INBOX` and `01_RAW` must be on the same volume. |

## More information

The repository's [README](../README.md) describes readiness rules, synchronization behavior, statuses, and manual reprocessing in more detail. For automatic daily runs, see [Windows Task Scheduler setup](WINDOWS_TASK_SCHEDULER.md).
