"""Participant status vocabulary and the buckets used in the end-of-run summary."""
from __future__ import annotations


class S:
    """Status strings (kept as plain strings so they serialise trivially to JSON/CSV)."""
    SUCCESS = "SUCCESS"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    MISSING_FILES = "MISSING_FILES"
    AMBIGUOUS_FILES = "AMBIGUOUS_FILES"
    INVALID_VIDEO = "INVALID_VIDEO"
    NO_AUDIO = "NO_AUDIO"
    RAW_CONFLICT = "RAW_CONFLICT"
    SYNC_FAILED = "SYNC_FAILED"
    ENCODE_FAILED = "ENCODE_FAILED"
    WAITING_FOR_STABILITY = "WAITING_FOR_STABILITY"
    # additional statuses (not in the original list, needed to be explicit)
    WAITING_FOR_READY = "WAITING_FOR_READY"      # READY marker required but absent
    INVALID_ID = "INVALID_ID"                    # folder name is not a valid participant id
    OUTPUT_CONFLICT = "OUTPUT_CONFLICT"          # unrecorded / mismatching file already in 02_SYNCED
    PROMOTE_FAILED = "PROMOTE_FAILED"            # move INBOX -> RAW raised an OS error
    INTERNAL_ERROR = "INTERNAL_ERROR"            # unexpected exception (bug / environment)
    RAW_PROMOTED = "RAW_PROMOTED"                # in RAW, sync not finished yet (resumable)


WAITING = {S.WAITING_FOR_STABILITY, S.WAITING_FOR_READY}
MANUAL_REVIEW = {S.LOW_CONFIDENCE, S.MISSING_FILES, S.AMBIGUOUS_FILES, S.INVALID_VIDEO,
                 S.NO_AUDIO, S.RAW_CONFLICT, S.OUTPUT_CONFLICT, S.INVALID_ID}
FAILED = {S.SYNC_FAILED, S.ENCODE_FAILED, S.PROMOTE_FAILED, S.INTERNAL_ERROR}

# Statuses that are NOT retried automatically on the next daily run: a human must look
# (LOW_CONFIDENCE never becomes "completed" by simply re-running the same maths).
STICKY = {S.LOW_CONFIDENCE}


def bucket(status: str) -> str:
    if status == S.SUCCESS:
        return "completed"
    if status in WAITING:
        return "waiting"
    if status in MANUAL_REVIEW:
        return "manual_review"
    return "failed"
