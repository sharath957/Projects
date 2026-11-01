"""Offline tests for the wrapper. They run the ORIGINAL scripts on the original
pack's own fixtures (real Yahoo data from Oct 2026), so no network is needed.

    cd trade-signals && python3 -m unittest discover -s tests -v
"""

import datetime
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# Vancouver is on UTC-7 all year since March 2026 (no fall-back), and the code reads the Mac's own clock.
# Pin this process to the same fixed offset so the tests mean the same on any machine.
os.environ["TZ"] = "Etc/GMT+7"
time.tzset()

from signals import core, notify, pipeline, store, track  # noqa: E402
from signals.ticket import caps_check, fee_math, message  # noqa: E402

FIX = os.path.join(core.VENDOR, "tests", "fixtures")
WALL = os.path.join(FIX, "trader-scanner", "wall5m-2026-10-19.json.gz")
DAILY = os.path.join(FIX, "trader-scanner", "daily-spark-2026-10-19.json")
TM = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")
ET = core.ET


class TempData(unittest.TestCase):
    def setUp(self):
        os.environ["TRADE_SIGNALS_DRY_RUN"] = "1"
        self.dir = tempfile.mkdtemp()
        self.saved = (core.DATA, store.SIGNALS, store.HITS)
        core.DATA = self.dir
        store.SIGNALS = os.path.join(self.dir, "signals.csv")
        store.HITS = os.path.join(self.dir, "scan_hits.csv")
        self.cfg = core.load_config("/nonexistent")
        notify.SENT.clear()

    def tearDown(self):
        core.DATA, store.SIGNALS, store.HITS = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)


class FeeMath(unittest.TestCase):
    def test_round_trip_fees_on_the_reel_trade(self):
        # TSLA reel plan, 2 shares, 1.5% each way
        f = fee_math(364.18, 358.32, 370.04, 2, 1.5)
        self.assertAlmostEqual(f["fees_at_target_usd"], 0.015 * 2 * (364.18 + 370.04), places=2)
        self.assertAlmostEqual(f["net_reward_usd"], 2 * 5.86 - 0.015 * 2 * (364.18 + 370.04), places=2)
        self.assertLess(f["net_reward_usd"], 0)  # a 1R day trade loses money after 3% of fees
        self.assertGreater(f["net_risk_usd"], 2 * 5.86)

    def test_no_fee_is_the_plain_r(self):
        f = fee_math(100, 95, 110, 3, 0)
        self.assertEqual(f["net_reward_usd"], 30)
        self.assertEqual(f["net_risk_usd"], 15)
        self.assertEqual(f["net_reward_risk"], 2.0)


class Caps(unittest.TestCase):
    cfg = {"max_trades_per_day": 3, "max_daily_loss_usd": 50}

    def row(self, loss, mode="day", notified="yes"):
        return {"mode": mode, "notified": notified, "max_loss_usd": str(loss)}

    def test_trade_cap(self):
        today = [self.row(5)] * 3
        self.assertIn("max_trades_per_day", caps_check(self.row(5), today, self.cfg))

    def test_daily_loss_cap_counts_only_sent_tickets_in_the_same_mode(self):
        today = [self.row(20), self.row(20), self.row(40, notified="no"), self.row(40, mode="swing")]
        self.assertEqual(caps_check(self.row(10), today, self.cfg), "")
        self.assertIn("daily loss", caps_check(self.row(11), today, self.cfg))


def bars(day, start, rows, minutes=5):
    t = datetime.datetime(day.year, day.month, day.day, *start, tzinfo=ET)
    out = []
    for o, h, l, c in rows:
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
        t += datetime.timedelta(minutes=minutes)
    return out


DAY = datetime.date(2026, 10, 19)
DAY_ROW = {"session_date": "2026-10-19", "generated_at_et": "2026-10-19 10:15 ET", "flat_by_et": "15:55",
           "entry": "100", "stop": "98", "target": "104", "shares": "2", "risk_per_share": "2",
           "fx_fee_pct_per_side": "1.5"}


class TrackDay(unittest.TestCase):
    def test_target(self):
        b = bars(DAY, (10, 15), [(100.5, 101, 99.8, 100.2), (100.2, 104.2, 100, 104)])
        u = track.eval_day(DAY_ROW, b)
        self.assertEqual((u["status"], u["exit_price"]), ("target", "104.00"))
        self.assertEqual(u["r_gross"], "2.00")
        self.assertAlmostEqual(float(u["net_pnl_usd"]), 8 - 0.015 * 2 * 204, places=2)

    def test_stop_counts_first_in_a_bar_that_touches_both(self):
        b = bars(DAY, (10, 15), [(100, 105, 97, 101)])
        self.assertEqual(track.eval_day(DAY_ROW, b)["status"], "stop")

    def test_bars_before_the_push_do_not_count(self):
        b = bars(DAY, (10, 5), [(100, 105, 99, 104), (101, 101.5, 100.5, 101)])  # 10:05 and 10:10 only
        self.assertIsNone(track.eval_day(DAY_ROW, b))

    def test_not_filled_when_it_runs_away(self):
        b = bars(DAY, (10, 15), [(101, 103, 100.5, 102), (102, 103.5, 101, 103)])
        self.assertEqual(track.eval_day(DAY_ROW, b)["status"], "not_filled")

    def test_flat_by_close(self):
        rows = [(100, 101, 99.5, 100.5)] + [(100.5, 101, 100, 100.6)] * 200
        u = track.eval_day(DAY_ROW, bars(DAY, (10, 15), rows))
        self.assertEqual(u["status"], "flat")
        self.assertEqual(u["status_at"], "2026-10-19 15:55 ET")


SW = {"session_date": "2026-10-20", "entry": "50", "stop": "47.5", "target": "55", "shares": "4",
      "risk_per_share": "2.5", "fx_fee_pct_per_side": "1.5"}


class TrackSwing(unittest.TestCase):
    def d(self, rows, start=datetime.date(2026, 10, 20)):
        return bars(start, (9, 30), rows, minutes=24 * 60)

    def test_gap_down_fills_at_the_open(self):
        u = track.eval_swing(SW, self.d([(50.5, 51, 49.8, 50.5), (46, 47, 45, 46)]), 20, datetime.date(2026, 10, 22))
        self.assertEqual((u["status"], u["exit_price"]), ("stop", "46.00"))

    def test_target_and_not_filled_and_expiry(self):
        u = track.eval_swing(SW, self.d([(50.5, 51, 49.9, 50.6), (52, 56, 51, 55.5)]), 20, datetime.date(2026, 10, 22))
        self.assertEqual(u["status"], "target")
        self.assertEqual(track.eval_swing(SW, self.d([(51, 52, 50.5, 51.5)]), 20, DAY)["status"], "not_filled")
        u = track.eval_swing(SW, self.d([(50, 51, 49, 50.2), (50.2, 51, 49, 50.4)]), 2, datetime.date(2026, 10, 22))
        self.assertEqual((u["status"], u["exit_price"]), ("expired", "50.40"))

    def test_still_open(self):
        self.assertIsNone(track.eval_swing(SW, self.d([(50, 51, 49, 50.2)]), 20, datetime.date(2026, 10, 21)))


class DayPipelineOffline(TempData):
    def test_reel_morning(self):
        now = datetime.datetime(2026, 10, 19, 10, 15, tzinfo=ET)
        rows = pipeline.day_scan(self.cfg, DAY, now=now, offline={"scan": WALL, "tm": TM})
        sent = [r for r in rows if r["notified"] == "yes"]
        self.assertEqual(len(sent), 3)  # max_trades_per_day
        self.assertEqual(len(notify.SENT), 3)
        for r in sent:
            self.assertEqual(r["risk_check"], "PASS")
            self.assertLessEqual(float(r["max_loss_usd"]), 15)
            self.assertLess(float(r["stop"]), float(r["entry"]))
            self.assertGreater(float(r["target"]), float(r["entry"]))
        blocked = [r for r in rows if r["risk_check"] == "BLOCK"]
        self.assertTrue(all(r["notified"] == "no" and r["risk_reasons"] for r in blocked))
        hits = store.read_rows(store.HITS)
        self.assertEqual(len(hits), 41)  # every raw hit is on record, sent or not
        self.assertTrue(any(h["ticketed"].startswith("no: trigger too old") for h in hits))
        # a second scan the same morning does not re-send anything
        again = pipeline.day_scan(self.cfg, DAY, now=now, offline={"scan": WALL, "tm": TM})
        self.assertEqual(again, [])
        self.assertEqual(len(store.read_rows()), len(rows))

    def test_message_is_phone_ready(self):
        now = datetime.datetime(2026, 10, 19, 10, 15, tzinfo=ET)
        pipeline.day_scan(self.cfg, DAY, now=now, offline={"scan": WALL, "tm": TM})
        m = notify.SENT[0]
        m["title"].encode("ascii")
        for word in ("BUY", "SELL limit", "STOP-LOSS", "Out by 15:55 ET (12:55 your time)"):
            self.assertIn(word, m["body"])


class SwingPipelineOffline(TempData):
    def test_daily_scan_tickets_for_the_next_session(self):
        now = datetime.datetime(2026, 10, 19, 8, 0, tzinfo=ET)
        rows = pipeline.swing_scan(self.cfg, DAY, now=now, offline={"scan": DAILY})
        sent = [r for r in rows if r["notified"] == "yes"]
        self.assertEqual(len(sent), 3)
        for r in sent:
            self.assertEqual(r["session_date"], "2026-10-20")
            self.assertAlmostEqual(float(r["stop"]), float(r["entry"]) * 0.95, delta=0.011)  # pct:5
            self.assertAlmostEqual(float(r["reward_risk"]), 2.0, delta=0.01)  # 2R
            self.assertNotEqual(r["setup"], "level")  # the scanner's setup names
        self.assertIn("Place before the open on 2026-10-20", notify.SENT[0]["body"])

    def test_tracker_updates_the_log(self):
        now = datetime.datetime(2026, 10, 19, 8, 0, tzinfo=ET)
        pipeline.swing_scan(self.cfg, DAY, now=now, offline={"scan": DAILY})

        def fake(sym, interval, start, end):
            r = next(x for x in store.read_rows() if x["symbol"] == sym)
            e, t = float(r["entry"]), float(r["target"])
            return bars(start, (9, 30), [(e, e * 1.01, e * 0.995, e), (e, t * 1.01, e, t)], minutes=24 * 60)

        later = datetime.datetime(2026, 10, 21, 17, 0, tzinfo=ET)
        n = track.run_track(self.cfg, fetch=fake, now=later)
        self.assertEqual(n, 3)
        done = [r for r in store.read_rows() if r["notified"] == "yes"]
        self.assertTrue(all(r["status"] == "target" and float(r["net_pnl_usd"]) < float(r["gross_pnl_usd"])
                            for r in done))


class LocalClock(unittest.TestCase):
    """New York changes its clocks and Vancouver (UTC-7 all year) does not."""

    def test_new_york_is_three_hours_ahead_in_summer_two_in_winter(self):
        self.assertEqual(core.local_hhmm(datetime.date(2026, 10, 19), "15:55"), "12:55")  # EDT, UTC-4
        self.assertEqual(core.local_hhmm(datetime.date(2026, 11, 2), "15:55"), "13:55")   # EST, UTC-5
        self.assertEqual(core.local_hhmm(datetime.date(2027, 3, 15), "09:50"), "06:50")   # EDT again
        self.assertEqual(core.local_hhmm(datetime.date(2026, 11, 2), "09:50"), "07:50")

    def test_labels_follow_the_clock(self):
        t = datetime.datetime(2026, 11, 2, 9, 50, tzinfo=ET)
        self.assertTrue(core.pt_label(t).startswith("2026-11-02 07:50"))

    def test_winter_ticket_says_one_fifty_five(self):
        row = {"symbol": "TSLA", "mode": "day", "setup": "orb", "shares": 3, "entry": 364.18, "position_usd": 1092.54,
               "target": 375.9, "stop": 358.32, "max_loss_usd": 17.58, "fees_at_stop_usd": 33.0,
               "fx_fee_pct_per_side": 1.5, "net_reward_usd": 1.2, "net_risk_usd": 33.4, "net_reward_risk": 0.04,
               "tm_target_pct": "", "signal_id": "D20261102-TSLA", "generated_at_pt": "2026-11-02 07:50 GMT-7",
               "flat_by_et": "15:55", "session_date": "2026-11-02", "setup": "orb"}
        _, body = message(row)
        self.assertIn("Out by 15:55 ET (13:55 your time)", body)


class Calendar(unittest.TestCase):
    def test_original_nyse_calendar(self):
        self.assertFalse(core.trading_day(datetime.date(2026, 11, 26))[0])  # Thanksgiving
        self.assertEqual(core.next_trading_day(datetime.date(2026, 10, 23)), datetime.date(2026, 10, 26))
        self.assertTrue(core.early_close(datetime.date(2026, 11, 27)))


class VendorUnchanged(unittest.TestCase):
    def test_original_files_carry_their_license(self):
        self.assertTrue(os.path.exists(os.path.join(core.VENDOR, "LICENSE")))
        for p in core.SCRIPTS.values():
            self.assertTrue(os.path.exists(p), p)


if __name__ == "__main__":
    unittest.main()
