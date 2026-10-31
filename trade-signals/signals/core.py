"""Shared plumbing: paths, config, clocks, and calling the ORIGINAL day-trader-skill
scripts unchanged (as subprocesses, reading their --json output).

Nothing in this package edits or re-implements the original skills' logic. The
original scripts decide setups, entries, stops, targets, sizing and risk; this
wrapper only runs them, adds the currency-fee math, notifies and logs.
"""

import datetime
import importlib.util
import json
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "vendor", "day-trader-skill")
DATA = os.path.join(ROOT, "data")
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Vancouver")

SCRIPTS = {
    "scan": os.path.join(VENDOR, "trader-scanner", "scan.py"),
    "entry": os.path.join(VENDOR, "trader-entry", "entry.py"),
    "exit": os.path.join(VENDOR, "trader-exit", "exit.py"),
    "timemachine": os.path.join(VENDOR, "trader-timemachine", "timemachine.py"),
    "size": os.path.join(VENDOR, "trader-budget", "size.py"),
    "risk": os.path.join(VENDOR, "trader-risk", "check.py"),
    "alerts": os.path.join(VENDOR, "trader-alerts", "alerts.py"),
}

DEFAULT_CONFIG = {
    "account_size_usd": 1000,
    "max_loss_per_trade_usd": 15,
    "max_daily_loss_usd": 50,
    "max_trades_per_day": 3,
    "fx_fee_pct_per_side": 1.5,
    "max_candidates_per_run": 10,
    "day": {
        "enabled": True,
        "scan_times_et": ["09:50", "10:05", "10:20", "10:35"],
        "max_signal_age_min": 20,
        "stop": "or-mid",
        "target": "2R",
        "timemachine": True,
        "alert_every_min": 2,
        "flat_by": "15:55",
        "min_price": 5,
    },
    "swing": {
        "enabled": True,
        "stop": "pct:5",
        "target": "2R",
        "entry_time_et": "09:50",
        "max_hold_days": 20,
        "min_price": 5,
        "min_volume": 1000000,
    },
    "ntfy": {"server": "https://ntfy.sh", "topic_env": "NTFY_TOPIC"},
    "notion": {"enabled": False, "token_env": "NOTION_TOKEN", "database_id": ""},
    # Passed through to the original skills' trader/config.json (their own keys).
    "original": {
        "cash_buffer_pct": 10,
        "max_position_pct": 75,
        "max_order_usd": 4000,
        "min_order_usd": 5,
        "max_orders_per_day": 10,
        "min_reward_risk": 1.0,
        "require_stop": True,
        "no_entries_before": "09:45",
        "no_entries_after": "15:30",
        "fractional": False,
        "paper": True,
        "blocked": [],
        "allow_options": False,
        "allow_margin": False,
        "allow_short": False,
        "pdt_guard": False,
    },
}


class WrapperError(Exception):
    pass


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    path = path or os.path.join(ROOT, "config.json")
    user = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
    return _merge(DEFAULT_CONFIG, user)


def load_env(path=None):
    """Read KEY=VALUE lines from .env (launchd does not see your shell's exports)."""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def original_config(cfg, mode):
    """The original skills' trader/config.json, built from this wrapper's config."""
    o = dict(cfg["original"])
    o.update({
        "budget": cfg["account_size_usd"],
        "max_loss_per_trade_usd": cfg["max_loss_per_trade_usd"],
        "max_daily_loss_usd": cfg["max_daily_loss_usd"],
        "max_trades_per_day": cfg["max_trades_per_day"],
        "flat_by": cfg["day"]["flat_by"],
        "broker": "manual",
    })
    return o


# ---------------------------------------------------------------- clocks

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def to_et(t):
    return t.astimezone(ET)


def et_label(t):
    """'2026-10-19 10:15' New York wall clock, the format the original --now flags take."""
    return to_et(t).strftime("%Y-%m-%d %H:%M")


def parse_et(day, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


def pt_label(t):
    return t.astimezone(PT).strftime("%Y-%m-%d %H:%M PT")


_scan_mod = None


def _scan_module():
    """Import the original scanner as a module, only to reuse its NYSE calendar."""
    global _scan_mod
    if _scan_mod is None:
        spec = importlib.util.spec_from_file_location("dts_scan", SCRIPTS["scan"])
        _scan_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_scan_mod)
    return _scan_mod


def trading_day(d):
    """(is_trading_day, reason) from the original scanner's NYSE calendar (2026-2027)."""
    m = _scan_module()
    if d.year not in m.CALENDAR_YEARS:
        raise WrapperError("the original NYSE calendar covers %s only: update vendor/ for %d"
                           % ("-".join(str(y) for y in m.CALENDAR_YEARS), d.year))
    return m.trading_day(d)


def early_close(d):
    return d.isoformat() in _scan_module().NYSE_EARLY_CLOSE


def next_trading_day(d):
    n = d + datetime.timedelta(days=1)
    while not trading_day(n)[0]:
        n += datetime.timedelta(days=1)
    return n


# ---------------------------------------------------------------- running the original scripts

def run_tool(name, args, cwd, timeout=600):
    """Run an original script with --json. Returns (exit_code, parsed_json_or_None, stderr).

    The originals use exit code 1 for 'nothing found / refused', so a JSON answer
    with code 1 is normal; only a missing or unparsable answer is an error.
    """
    os.makedirs(cwd, exist_ok=True)
    cmd = [sys.executable, SCRIPTS[name]] + list(args)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    doc = None
    out = (p.stdout or "").strip()
    if out:
        start = out.find("{")
        if start >= 0:
            try:
                doc = json.loads(out[start:])
            except ValueError:
                doc = None
    return p.returncode, doc, (p.stderr or "").strip() or (out if doc is None else "")


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def workdir(day, mode, symbol=None):
    """data/work/<date>/<mode>[-<SYM>]: each candidate gets its own trader/ folder,
    because the originals read and write trader/*.json in the current directory."""
    name = mode if symbol is None else "%s-%s" % (mode, symbol)
    return os.path.join(DATA, "work", day.isoformat(), name)


def log(msg):
    stamp = now_utc().astimezone(PT).strftime("%Y-%m-%d %H:%M:%S PT")
    line = "[%s] %s" % (stamp, msg)
    print(line, flush=True)
    d = os.path.join(DATA, "logs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "run-%s.log" % stamp[:10]), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
