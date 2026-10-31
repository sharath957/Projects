"""The local log: data/signals.csv (every ticket) and data/scan_hits.csv (every raw
scanner hit, so the ones that were not sent are still on record). The CSV is the
source of truth; Notion is a copy."""

import contextlib
import csv
import fcntl
import os

from .core import DATA
from .ticket import FIELDS

SIGNALS = os.path.join(DATA, "signals.csv")
HITS = os.path.join(DATA, "scan_hits.csv")
HIT_FIELDS = ["scanned_at_pt", "mode", "session_date", "rank", "symbol", "setups", "price",
              "or_high", "or_low", "trigger_at_et", "rsi", "volume", "ticketed"]


@contextlib.contextmanager
def locked(path=None):
    path = path or SIGNALS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def read_rows(path=None):
    path = path or SIGNALS
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_rows(rows, path=None, fields=FIELDS):
    path = path or SIGNALS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    os.replace(tmp, path)


def append_rows(rows, path=None, fields=FIELDS):
    path = path or SIGNALS
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def stringify(row):
    return {k: ("" if row.get(k) is None else str(row.get(k))) for k in FIELDS}
