"""Outcome tracker: what would have happened to each ticket if you had placed it
exactly as written. Runs after the close. Uses the same conservative rule as the
original Time Machine: in a bar that touches both, the stop counts first.

  day    filled if a 5-minute bar after the trigger trades at or below the limit
         before flat-by; then stop / target / flat (close of the bar ending at flat-by)
  swing  a DAY limit order on the session date: filled if that day's low <= limit;
         then each day: stop (at the stop, or the open if it gaps below), target
         (at the target, or the open if it gaps above), else expired after
         max_hold_days at that day's close
"""

import datetime
import json
import urllib.request

from . import store
from .core import ET, load_config, log, now_utc, parse_et, to_et
from .ticket import OPEN

UA = "Mozilla/5.0 (compatible; trade-signals/1.0)"


def fetch_bars(symbol, interval, start_day, end_day):
    """Yahoo v8 chart bars [{t (ET datetime), o,h,l,c}] between two dates (free, unofficial)."""
    p1 = int(datetime.datetime(start_day.year, start_day.month, start_day.day, tzinfo=ET).timestamp())
    p2 = int(datetime.datetime(end_day.year, end_day.month, end_day.day, tzinfo=ET).timestamp()) + 86400
    last = None
    for host in ("query2", "query1"):
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=%s"
               % (host, symbol, p1, p2, interval))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return parse_chart(json.loads(r.read().decode("utf-8")))
        except Exception as e:
            last = e
    raise RuntimeError("Yahoo chart for %s failed: %s" % (symbol, last))


def parse_chart(doc):
    res = ((doc or {}).get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    q = (r.get("indicators") or {}).get("quote") or [{}]
    q = q[0]
    out = []
    for i, ts in enumerate(r.get("timestamp") or []):
        o, h, l, c = (q.get(k, [None] * (i + 1))[i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        out.append({"t": datetime.datetime.fromtimestamp(ts, ET), "o": o, "h": h, "l": l, "c": c})
    return out


def eval_day(row, bars):
    """bars: the session's 5-minute bars. Returns the update dict, or None if undecided."""
    day = datetime.date.fromisoformat(row["session_date"])
    trig = datetime.datetime.strptime(row["generated_at_et"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    flat = parse_et(day, row["flat_by_et"] or "15:55")
    entry, stop, target = float(row["buy_limit"]), float(row["stop_loss"]), float(row["sell_limit_target"])
    # Conservative: you see the push at generated_at, so only bars that START after it count.
    after = [b for b in bars if b["t"].date() == day and b["t"] >= trig
             and b["t"] + datetime.timedelta(minutes=5) <= flat]
    if not after:
        return None
    filled = next((i for i, b in enumerate(after) if b["l"] <= entry), None)
    if filled is None:
        return {"status": "not_filled", "status_at": flat.strftime("%Y-%m-%d %H:%M ET")}
    for b in after[filled:]:
        if b["l"] <= stop:  # stop first when a bar touches both
            return _closed("stop", stop, b["t"], row)
        if b["h"] >= target:
            return _closed("target", target, b["t"], row)
    last = after[-1]
    return _closed("flat", last["c"], last["t"] + datetime.timedelta(minutes=5), row)


def eval_swing(row, bars, max_hold_days, today):
    """bars: daily bars from the session date on. Returns the update dict, or None while still open."""
    day = datetime.date.fromisoformat(row["session_date"])
    entry, stop, target = float(row["buy_limit"]), float(row["stop_loss"]), float(row["sell_limit_target"])
    days = [b for b in bars if b["t"].date() >= day]
    if not days or days[0]["t"].date() != day:
        return None if today <= day else {"status": "no_data", "status_at": today.isoformat()}
    first = days[0]
    if first["l"] > entry:
        return {"status": "not_filled", "status_at": day.isoformat()}
    for n, b in enumerate(days):
        o = b["o"] if n > 0 else entry  # the fill day starts at the fill
        if b["l"] <= stop:
            return _closed("stop", min(stop, o), b["t"], row)
        if b["h"] >= target:
            return _closed("target", max(target, o), b["t"], row)
        if n + 1 >= int(max_hold_days):
            return _closed("expired", b["c"], b["t"], row)
    return None


def _closed(status, exit_price, t, row):
    q = int(float(row["shares"] or 0))
    entry = float(row["buy_limit"])
    f = float(row["fx_fee_pct_per_side"] or 0) / 100.0
    gross = q * (exit_price - entry)
    fees = f * q * (entry + exit_price)
    risk = float(row["risk_per_share"] or 0)
    return {"status": status, "status_at": t.strftime("%Y-%m-%d %H:%M ET"), "fill_price": "%.2f" % entry,
            "exit_price": "%.2f" % exit_price, "gross_pnl_usd": "%.2f" % gross, "fees_usd": "%.2f" % fees,
            "net_pnl_usd": "%.2f" % (gross - fees),
            "r_gross": "%.2f" % ((exit_price - entry) / risk) if risk else "",
            "r_net": "%.2f" % ((gross - fees) / (q * risk)) if risk and q else ""}


def run_track(cfg=None, fetch=fetch_bars, now=None):
    cfg = cfg or load_config()
    now = now or now_utc()
    today = to_et(now).date()
    with store.locked():
        rows = store.read_rows()
        changed = 0
        for r in rows:
            if r["status"] != OPEN or r["risk_check"] != "PASS":
                continue
            day = datetime.date.fromisoformat(r["session_date"])
            try:
                if r["mode"] == "day":
                    if to_et(now) < parse_et(day, "16:05"):
                        continue
                    upd = eval_day(r, fetch(r["symbol"], "5m", day, day))
                else:
                    if day > today or (day == today and to_et(now) < parse_et(day, "16:05")):
                        continue
                    upd = eval_swing(r, fetch(r["symbol"], "1d", day, today),
                                     cfg["swing"]["max_hold_days"], today)
            except Exception as e:
                log("track %s: %s" % (r["signal_id"], e))
                continue
            if upd is None and r["mode"] == "day" and day < today:
                upd = {"status": "no_data", "status_at": today.isoformat()}
            if upd:
                r.update(upd)
                changed += 1
        if changed:
            store.write_rows(rows)
    log("Tracker: %d ticket(s) updated." % changed)
    return changed
