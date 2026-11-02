"""Turn the original skills' outputs (plan, sizing, risk verdict, Time Machine)
into one manual order ticket: the numbers you type into Wealthsimple, plus the
currency-fee math the originals do not know about."""

import datetime

from .core import local_hhmm

FIELDS = [
    "signal_id", "mode", "generated_at_pt", "generated_at_et", "session_date", "symbol", "setup",
    "order_type", "buy_limit", "sell_limit_target", "stop_loss", "risk_per_share", "reward_risk",
    "shares", "position_usd", "max_loss_usd", "bound_by",
    "fx_fee_pct_per_side", "fees_at_target_usd", "fees_at_stop_usd",
    "net_reward_usd", "net_risk_usd", "net_reward_risk",
    "risk_check", "risk_reasons", "tm_target_pct", "tm_stop_pct", "tm_neither_pct", "tm_verdict",
    "history_summary", "flat_by_et", "notified", "notes",
    "status", "status_at", "fill_price", "exit_price", "gross_pnl_usd", "fees_usd", "net_pnl_usd",
    "r_gross", "r_net",
]

OPEN = "open"

# Column names used before the rename (data/signals.csv written by earlier versions)
ALIASES = {"entry": "buy_limit", "target": "sell_limit_target", "stop": "stop_loss"}


def r2(x):
    return None if x is None else round(float(x) + 0.0, 2)


def fee_math(entry, stop, target, shares, fee_pct):
    """Round-trip currency fees: fee_pct of the money converted on the way in
    (shares x entry) and on the way out (shares x exit price)."""
    f = float(fee_pct) / 100.0
    q = float(shares)
    fees_t = f * q * (entry + target)
    fees_s = f * q * (entry + stop)
    net_reward = q * (target - entry) - fees_t
    net_risk = q * (entry - stop) + fees_s
    nrr = (net_reward / net_risk) if net_risk > 0 else None
    return {"fees_at_target_usd": r2(fees_t), "fees_at_stop_usd": r2(fees_s),
            "net_reward_usd": r2(net_reward), "net_risk_usd": r2(net_risk),
            "net_reward_risk": None if nrr is None else round(nrr, 2)}


def build_row(mode, signal_id, generated_utc, session_date, plan, sizing, order, risk, tm, cfg, pt, et):
    """One signals.csv row. plan/sizing/order/risk/tm are the originals' JSON (tm may be None)."""
    s = sizing or {}
    shares = int(s.get("qty") or 0)
    entry = float(s.get("entry") or plan["entry"])
    stop, target = float(plan["stop"]), float(plan["target"])
    row = {k: "" for k in FIELDS}
    row.update({
        "signal_id": signal_id, "mode": mode,
        "generated_at_pt": pt, "generated_at_et": et,
        "session_date": session_date.isoformat(), "symbol": plan["symbol"], "setup": plan.get("setup", ""),
        "order_type": s.get("order_type", "limit"),
        "buy_limit": r2(entry), "sell_limit_target": r2(target), "stop_loss": r2(stop),
        "risk_per_share": r2(plan.get("risk_per_share")), "reward_risk": plan.get("reward_risk"),
        "shares": shares, "position_usd": r2(shares * entry), "max_loss_usd": r2(s.get("max_loss")),
        "bound_by": s.get("bound_by", ""),
        "fx_fee_pct_per_side": cfg["fx_fee_pct_per_side"],
        "flat_by_et": plan.get("flat_by", "") if mode == "day" else "",
        "status": OPEN,
        "max_loss_cap": cfg["max_loss_per_trade_usd"],  # for the message only, not a CSV column
    })
    if shares > 0:
        row.update(fee_math(entry, stop, target, shares, cfg["fx_fee_pct_per_side"]))
    if order is None:
        row["risk_check"] = "BLOCK"
        row["risk_reasons"] = "sizing: " + (s.get("refusal") or "no order")
        row["status"] = "blocked"
    elif risk:
        row["risk_check"] = risk.get("verdict", "")
        row["risk_reasons"] = "; ".join(risk.get("reasons") or [])
        if row["risk_check"] != "PASS":
            row["status"] = "blocked"
    if tm and tm.get("mc"):
        mc = tm["mc"]
        row["tm_target_pct"] = round(100 * mc.get("p_target", 0), 1)
        row["tm_stop_pct"] = round(100 * mc.get("p_stop", 0), 1)
        row["tm_neither_pct"] = round(100 * mc.get("p_neither", 0), 1)
        row["tm_verdict"] = tm.get("verdict", "")
        h = tm.get("history") or {}
        if h.get("triggered") is not None:
            row["history_summary"] = "fired %s of %s sessions: %s target, %s stop, %s neither" % (
                h.get("triggered"), h.get("sessions"), h.get("target"), h.get("stop"), h.get("close"))
    return row


def caps_check(row, today_rows, cfg):
    """The originals count trades and daily risk from orders PLACED at the broker,
    and nothing is placed here, so the wrapper applies the same two limits to the
    signals it has already sent today in this mode. Returns '' or the reason."""
    sent = [r for r in today_rows if r["mode"] == row["mode"] and r["notified"] == "yes"]
    if len(sent) >= int(cfg["max_trades_per_day"]):
        return "max_trades_per_day (%s) already sent today" % cfg["max_trades_per_day"]
    used = sum(float(r["max_loss_usd"] or 0) for r in sent)
    if used + float(row["max_loss_usd"] or 0) > float(cfg["max_daily_loss_usd"]) + 1e-9:
        return "daily loss limit: $%.2f already at risk + $%.2f > $%.2f" % (
            used, float(row["max_loss_usd"] or 0), float(cfg["max_daily_loss_usd"]))
    return ""


def money(x):
    return "$%s" % format(float(x), ",.2f")


def message(row):
    """(title, body) for the phone. Title is ASCII (HTTP header); the body is UTF-8."""
    sym, mode = row["symbol"], row["mode"]
    if mode == "day":
        title = "DAY %s: buy signal %s" % (sym, row["generated_at_pt"][11:16])
        when = "Now (opening-range breakout). Out by %s ET (%s your time) win or lose." % (
            row["flat_by_et"], local_hhmm(datetime.date.fromisoformat(row["session_date"]), row["flat_by_et"]))
    else:
        title = "SWING %s: buy signal for %s" % (sym, row["session_date"])
        when = "Place before the open on %s (day order)." % row["session_date"]
    lines = [
        "%s  %s" % (sym, row["setup"]),
        "BUY  %s sh  limit %s  (~%s)" % (row["shares"], money(row["buy_limit"]), money(row["position_usd"])),
        "SELL limit (target)  %s" % money(row["sell_limit_target"]),
        "STOP-LOSS            %s" % money(row["stop_loss"]),
        "Max loss at stop %s + fees %s" % (money(row["max_loss_usd"]), money(row["fees_at_stop_usd"] or 0)),
        "After %s%% FX each way: win %s / lose %s (net R:R %s)" % (
            row["fx_fee_pct_per_side"], money(row["net_reward_usd"]), money(row["net_risk_usd"]),
            row["net_reward_risk"]),
    ]
    if row["net_reward_usd"] != "" and float(row["net_reward_usd"]) <= 0:
        lines.append("WARNING: fees eat the whole target. A 'win' loses money.")
    if row.get("max_loss_cap") and row["net_risk_usd"] != "" and float(row["net_risk_usd"]) > float(row["max_loss_cap"]):
        lines.append("Note: with fees the loss at the stop (%s) is over your %s max loss." % (
            money(row["net_risk_usd"]), money(row["max_loss_cap"])))
    if row["tm_target_pct"] != "":
        lines.append("Time Machine: target %s%% / stop %s%% / open %s%%" % (
            row["tm_target_pct"], row["tm_stop_pct"], row["tm_neither_pct"]))
    lines.append(when)
    lines.append("Log-only: your call. Not advice. id %s" % row["signal_id"])
    return title, "\n".join(lines)
