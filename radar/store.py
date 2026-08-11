"""
Small JSON-on-disk helpers. The radar keeps its memory in plain files under
radar/state/ so you can open, diff, and hand-edit anything it believes.
"""

import json
import os
import tempfile

from . import settings


def ensure_dirs():
    for path in (settings.STATE_DIR, settings.FOLLOW_SNAPSHOT_DIR):
        os.makedirs(path, exist_ok=True)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # A corrupted state file must mean "start clean", never a crash mid-sweep.
        return default


def write_json(path, data):
    """
    Write atomically: the radar can be interrupted at any moment, and a
    half-written follow snapshot would produce a wave of bogus diffs next run.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_smart_accounts():
    """
    Read the curated handle list. Blank lines and '#' comments are ignored.
    """
    path = settings.SMART_ACCOUNTS_FILE
    if not os.path.exists(path):
        return []
    handles = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                handles.append(line.lstrip("@").lower())
    # Preserve order but drop duplicates.
    return list(dict.fromkeys(handles))
