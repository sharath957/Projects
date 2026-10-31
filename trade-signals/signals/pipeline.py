"""The two runners. Each runs the ORIGINAL skills in their original order:

  day:   scanner (orb) -> exit -> timemachine -> budget -> risk -> [notify instead of broker] -> alerts
  swing: scanner (daily) -> exit -> budget -> risk -> [notify instead of broker]

and keeps the originals' order of hits (orb: earliest breakout first; daily:
most dollars traded first). The only new steps are the fee math, the
per-day caps (the originals count orders placed at a broker; nothing is placed
here), the phone push and the log.
"""

import datetime
import os
import time

from . import notify, store
from .core import (DATA, early_close, et_label, load_config, log, next_trading_day, now_utc, original_config,
                   parse_et, pt_label, run_tool, to_et, trading_day, workdir, write_json, WrapperError)
from .ticket import build_row, caps_check, message

UNIVERSE = os.path.join(DATA, "universe.txt")
UNIVERSE_MAX_AGE_DAYS = 7


def _now_args(now):
    return ["--now", et_label(now)] if now is not None else []


def ensure_universe(offline=False):
    """Refresh the original scanner's symbol list once a week."""
    if offline:
        return None
    fresh = os.path.exists(UNIVERSE) and (time.time() - os.path.getmtime(UNIVERSE)) < UNIVERSE_MAX_AGE_DAYS * 86400
    if not fresh:
        log("Refreshing the stock universe (Nasdaq Trader lists)...")
        code, doc, err = run_tool("scan", ["universe", "--out", UNIVERSE, "--json"], cwd=DATA, timeout=300)
        if not os.path.exists(UNIVERSE):
            raise WrapperError("could not build the universe: %s" % (err or code))
    return UNIVERSE


def _prepare(mode, day, symbol, cfg):
    wd = workdir(day, mode, symbol)
    write_json(os.path.join(wd, "trader", "config.json"), original_config(cfg, mode))
    return wd


def process_candidate(mode, hit, session_day, cfg, now=None, offline_tm=None):
    """Plan, size and check one scanner hit with the original tools. Returns a signals row."""
    sym = hit["symbol"]
    wd = _prepare(mode, session_day, sym, cfg)
    c = ["--config", "trader/config.json"]
    if mode == "day":
        d = cfg["day"]
        args = ["plan", "--symbol", sym, "--entry", str(hit["price"]), "--or-high", str(hit["or_high"]),
                "--or-low", str(hit["or_low"]), "--setup", "orb", "--date", session_day.isoformat(),
                "--time", hit["at"], "--entry-bar", str(hit["bar_index"]),
                "--stop", d["stop"], "--target", d["target"], "--write", "--json"] + c + _now_args(now)
        pin = now
    else:
        s = cfg["swing"]
        args = ["plan", "--symbol", sym, "--entry", str(hit["price"]), "--setup", "level",
                "--date", session_day.isoformat(), "--stop", s["stop"], "--target", s["target"],
                "--write", "--json"] + c + _now_args(now)
        # Sized and risk-checked as of the morning you would place it.
        pin = parse_et(session_day, s["entry_time_et"])
    code, plan, err = run_tool("exit", args, cwd=wd)
    if not plan or code != 0:
        return None, "exit planner refused: %s" % (err or "no plan")

    tm = None
    if mode == "day" and cfg["day"].get("timemachine", True):
        targs = ["run", "--json", "--write", "--until", plan["flat_by"]] + _now_args(now)
        if offline_tm:
            targs += ["--offline", offline_tm]
        code, tm, err = run_tool("timemachine", targs, cwd=wd)
        if not tm:
            log("Time Machine skipped for %s: %s" % (sym, err))

    code, sz, err = run_tool("size", ["--limit", "--write", "--json"] + c + _now_args(pin), cwd=wd)
    if not sz:
        return None, "budget sizing failed: %s" % err
    order = (sz.get("orders") or [None])[0]
    risk = None
    if order:
        code, rk, err = run_tool("risk", ["--json"] + c + _now_args(pin), cwd=wd)
        if not rk:
            return None, "risk check failed: %s" % err
        risk = (rk.get("results") or [None])[0]

    gen = now or now_utc()
    sid = "%s%s-%s" % (mode[0].upper(), session_day.strftime("%Y%m%d"), sym)
    row = build_row(mode, sid, gen, session_day, plan, sz.get("sizing"), order, risk, tm, cfg,
                    pt_label(gen), et_label(gen) + " ET")
    row["setup"] = " ".join(hit.get("setups") or []) or row["setup"]  # the scanner's own setup names
    return row, ""


def _hit_row(mode, session_day, rank, hit, ticketed, now):
    return {"scanned_at_pt": pt_label(now or now_utc()), "mode": mode, "session_date": session_day.isoformat(),
            "rank": rank, "symbol": hit["symbol"], "setups": " ".join(hit.get("setups") or []),
            "price": hit.get("price"), "or_high": hit.get("or_high", ""), "or_low": hit.get("or_low", ""),
            "trigger_at_et": hit.get("at", ""), "rsi": hit.get("rsi", ""), "volume": hit.get("volume", ""),
            "ticketed": ticketed}


def handle_hits(mode, hits, session_day, cfg, now=None, offline_tm=None, max_age_min=None):
    """Walk the hits in the originals' order. Ticket, notify and log until the day's cap."""
    seen = {(r["mode"], r["session_date"], r["symbol"]) for r in store.read_rows(store.HITS)}
    new = [h for h in hits if (mode, session_day.isoformat(), h["symbol"]) not in seen]
    processed, hit_rows, out = 0, [], []
    with store.locked():
        rows = store.read_rows()
        today_rows = [r for r in rows if r["session_date"] == session_day.isoformat()]
        for rank, hit in enumerate(new, 1):
            done = sum(1 for r in today_rows if r["mode"] == mode and r["notified"] == "yes")
            if done >= int(cfg["max_trades_per_day"]) or processed >= int(cfg["max_candidates_per_run"]):
                hit_rows.append(_hit_row(mode, session_day, rank, hit, "no: daily cap reached", now))
                continue
            if max_age_min is not None and _age_min(hit, session_day, now) > max_age_min:
                hit_rows.append(_hit_row(mode, session_day, rank, hit, "no: trigger too old for a manual entry", now))
                continue
            processed += 1
            row, why = process_candidate(mode, hit, session_day, cfg, now, offline_tm)
            if row is None:
                log("%s %s: %s" % (mode, hit["symbol"], why))
                hit_rows.append(_hit_row(mode, session_day, rank, hit, "no: " + why[:120], now))
                continue
            if row["risk_check"] == "PASS":
                cap = caps_check(store.stringify(row), today_rows, cfg)
                if cap:
                    row["notified"], row["notes"], row["status"] = "no", cap, "capped"
                else:
                    title, body = message(row)
                    notify.send(cfg, title, body, priority="high" if mode == "day" else "default",
                                tags="chart_with_upwards_trend")
                    row["notified"] = "yes"
                    if mode == "day":
                        run_tool("alerts", ["add", "--from-plan"], cwd=workdir(session_day, mode, row["symbol"]))
            else:
                row["notified"] = "no"
            srow = store.stringify(row)
            today_rows.append(srow)
            out.append(srow)
            hit_rows.append(_hit_row(mode, session_day, rank, hit, "yes" if row["notified"] == "yes" else
                                     "no: " + (row["risk_reasons"] or row["notes"] or row["status"])[:120], now))
            log("%s %s: %s %s" % (mode, row["symbol"], row["risk_check"], row["notified"]))
        store.append_rows(out)
    store.append_rows(hit_rows, store.HITS, store.HIT_FIELDS)
    return out


def _age_min(hit, day, now):
    if not hit.get("at"):
        return 0
    t = datetime.datetime.strptime(hit["at"].strip().upper(), "%I:%M %p")
    trig = parse_et(day, "%02d:%02d" % (t.hour, t.minute))
    return ((now or now_utc()) - trig).total_seconds() / 60.0


# ---------------------------------------------------------------- day

def day_scan(cfg, day, now=None, offline=None):
    """One opening-range scan with the original scanner, then tickets for new hits."""
    d = cfg["day"]
    wd = _prepare("day", day, None, cfg)
    args = ["orb", "--json", "--no-write", "--config", "trader/config.json", "--min-price", str(d["min_price"])]
    if offline:
        args += ["--offline", offline["scan"]] + _now_args(now)
    else:
        args += ["--universe", ensure_universe()]
    code, doc, err = run_tool("scan", args, cwd=wd, timeout=900)
    if doc is None:
        raise WrapperError("orb scan failed: %s" % err)
    log("ORB scan %s: %d hits of %s scanned" % (doc.get("as_of"), len(doc.get("hits") or []), doc.get("scanned")))
    return handle_hits("day", doc.get("hits") or [], day, cfg, now,
                       offline_tm=(offline or {}).get("tm"), max_age_min=d.get("max_signal_age_min", 20))


def run_day(cfg=None, sleep=time.sleep):
    cfg = cfg or load_config()
    if not cfg["day"]["enabled"]:
        log("Day mode is off in config.json.")
        return 0
    today = to_et(now_utc()).date()
    ok, why = trading_day(today)
    if not ok:
        log("No day run: %s." % why)
        return 0
    times = list(cfg["day"]["scan_times_et"])
    if early_close(today):
        log("Early close today (1:00 PM ET): scans run as usual, flat-by moves up.")
    for hhmm in times:
        at = parse_et(today, hhmm)
        wait = (at - now_utc()).total_seconds()
        if wait < -10 * 60:
            continue  # started late: skip scans that are long past
        if wait > 0:
            sleep(wait)
        try:
            day_scan(cfg, today)
        except Exception as e:
            log("Day scan at %s failed: %s" % (hhmm, e))
            notify.send(cfg, "trade-signals: day scan failed %s ET" % hhmm, str(e)[:500], tags="warning")
    monitor_alerts(cfg, today, sleep)
    return 0


def monitor_alerts(cfg, day, sleep=time.sleep):
    """Run the original alerts.py check for every ticket sent today, until flat-by."""
    rows = [r for r in store.read_rows() if r["mode"] == "day" and r["session_date"] == day.isoformat()
            and r["notified"] == "yes"]
    if not rows:
        log("No day tickets today: nothing to watch.")
        return
    flat = max(r["flat_by_et"] for r in rows) or cfg["day"]["flat_by"]
    end = parse_et(day, flat) + datetime.timedelta(minutes=2)
    every = max(1, int(cfg["day"]["alert_every_min"])) * 60
    while now_utc() < end:
        for r in rows:
            code, doc, err = run_tool("alerts", ["check", "--json"], cwd=workdir(day, "day", r["symbol"]), timeout=120)
            for ring in (doc or {}).get("rings") or []:
                notify.send(cfg, "ALERT %s" % r["symbol"], ring.get("message", ""), priority="high", tags="bell")
        sleep(every)
    log("Flat-by passed: alert watch done.")


# ---------------------------------------------------------------- swing

def swing_scan(cfg, today, now=None, offline=None):
    s = cfg["swing"]
    wd = _prepare("swing", today, None, cfg)
    args = ["daily", "--json", "--no-write", "--min-price", str(s["min_price"]), "--min-volume", str(s["min_volume"])]
    if offline:
        args += ["--offline", offline["scan"]] + _now_args(now)
    else:
        args += ["--universe", ensure_universe()]
    code, doc, err = run_tool("scan", args, cwd=wd, timeout=900)
    if doc is None:
        raise WrapperError("daily scan failed: %s" % err)
    log("Daily scan %s: %d hits of %s scanned" % (doc.get("as_of"), len(doc.get("hits") or []), doc.get("scanned")))
    return handle_hits("swing", doc.get("hits") or [], next_trading_day(today), cfg, now)


def run_swing(cfg=None):
    cfg = cfg or load_config()
    if not cfg["swing"]["enabled"]:
        log("Swing mode is off in config.json.")
        return 0
    now = now_utc()
    today = to_et(now).date()
    ok, why = trading_day(today)
    if not ok:
        log("No swing run: %s." % why)
        return 0
    close = parse_et(today, "13:00" if early_close(today) else "16:00")
    if now < close + datetime.timedelta(minutes=10):
        log("Swing scan runs after the close (today's daily bar is final then). Now is %s." % pt_label(now))
        return 1
    try:
        swing_scan(cfg, today)
    except Exception as e:
        log("Swing scan failed: %s" % e)
        notify.send(cfg, "trade-signals: swing scan failed", str(e)[:500], tags="warning")
        return 1
    return 0
