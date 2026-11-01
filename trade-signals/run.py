#!/usr/bin/env python3
"""trade-signals: the original day-trader-skill, delivered as phone tickets you
place by hand in Wealthsimple, with every ticket logged. Nothing here can place
an order.

    python3 run.py day            # start 6:40 AM local: waits for the scans, then tickets and alerts until flat-by
    python3 run.py swing          # after the close: daily scan, tickets for the next session
    python3 run.py track          # outcomes for open tickets (after the close)
    python3 run.py after-close    # swing + track + notion sync (what the 2:15 PM local schedule runs)
    python3 run.py test-notify    # send one test push to your phone
    python3 run.py notion-init PAGE_ID   # create the Notion database under a page
    python3 run.py notion-sync    # copy data/signals.csv to Notion
    python3 run.py notion-pull    # your "My ..." columns -> data/my_trades.csv
    python3 run.py summary        # quick results table from the log
"""

import sys

from signals import core, notify, pipeline, store, track


def summary():
    rows = [r for r in store.read_rows() if r["notified"] == "yes"]
    if not rows:
        print("No tickets sent yet.")
        return 0
    by = {}
    for r in rows:
        k = (r["mode"], r["setup"])
        by.setdefault(k, []).append(r)
    print("%-6s %-16s %5s %6s %6s %6s %8s %10s" % ("mode", "setup", "sent", "target", "stop", "other", "avg Rnet", "net P&L"))
    for (mode, setup), rs in sorted(by.items()):
        done = [r for r in rs if r["r_net"] not in ("", None)]
        avg = sum(float(r["r_net"]) for r in done) / len(done) if done else 0
        pnl = sum(float(r["net_pnl_usd"] or 0) for r in done)
        print("%-6s %-16s %5d %6d %6d %6d %8.2f %10.2f" % (
            mode, setup, len(rs), sum(r["status"] == "target" for r in rs), sum(r["status"] == "stop" for r in rs),
            sum(r["status"] not in ("target", "stop") for r in rs), avg, pnl))
    print("Log-only results with %s%% FX fees each way. A few weeks of data is still a small sample."
          % rows[0]["fx_fee_pct_per_side"])
    return 0


def main(argv):
    core.load_env()
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    cfg = core.load_config()
    if cmd == "day":
        code = pipeline.run_day(cfg)
        track.run_track(cfg)
        _sync(cfg)
        return code
    if cmd == "swing":
        return pipeline.run_swing(cfg)
    if cmd == "track":
        track.run_track(cfg)
        return 0
    if cmd == "after-close":
        code = pipeline.run_swing(cfg)
        track.run_track(cfg)
        _sync(cfg)
        return code
    if cmd == "test-notify":
        ok = notify.send(cfg, "trade-signals test", "If you can read this on your phone, ntfy works.")
        print("sent" if ok else "not sent: set NTFY_TOPIC in .env")
        return 0 if ok else 1
    if cmd == "notion-init":
        from signals import notion
        if len(argv) < 2:
            print("usage: run.py notion-init PARENT_PAGE_ID")
            return 1
        print("Created. Put this in config.json under notion.database_id:\n  %s" % notion.init(argv[1], cfg))
        return 0
    if cmd == "notion-sync":
        return 0 if _sync(cfg, loud=True) is not None else 1
    if cmd == "notion-pull":
        from signals import notion
        notion.pull(cfg)
        return 0
    if cmd == "summary":
        return summary()
    print("unknown command %r\n%s" % (cmd, __doc__))
    return 1


def _sync(cfg, loud=False):
    if not cfg["notion"].get("enabled") and not loud:
        return 0
    from signals import notion
    try:
        return notion.sync(cfg)
    except Exception as e:
        core.log("Notion sync failed (CSV is safe; retries next run): %s" % e)
        return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
