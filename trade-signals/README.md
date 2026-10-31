# trade-signals

The [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) pack, rewired
for **manual trading in Wealthsimple**. The original skills still do all the work
(scan, plan the exit, size, risk-check). Instead of sending an order to a broker, the
result arrives on your phone as a ticket you place yourself, and every ticket is logged
with its outcome.

**Nothing here can place an order.** Not financial advice. Log-only first: run it for
4-6 weeks and read the results before you trade real money.

## What happens on a trading day (Pacific time)

| Time (PT) | Mode | What runs |
|---|---|---|
| 6:40 AM | day | Starts. Opening-range scans at 6:50, 7:05, 7:20, 7:35 (9:50-10:35 ET) |
| on a breakout | day | Push: buy limit, sell limit (target), stop-loss, shares, fees, Time Machine odds |
| until 12:57 PM | day | The original price alerts: stop hit / target hit / **flat by 12:55 PM PT** |
| 1:35 PM | swing | Daily scan after the close. Push: tickets to place **before the next open** |
| 1:35 PM | both | Outcome tracker updates every open ticket; Notion sync |

Each mode sends at most `max_trades_per_day` (3) tickets a day and stops when the
tickets' combined max loss reaches `max_daily_loss_usd` ($50). Each mode is treated
as its own $1,000 log-only account.

### A ticket

```
DAY CI: buy signal 07:15
CI  orb
BUY  2 sh  limit $301.70  (~$603.40)
SELL limit (target)  $305.24
STOP-LOSS            $299.93
Max loss at stop $3.54 + fees $18.05
After 1.5% FX each way: win $-11.13 / lose $21.59 (net R:R -0.52)
WARNING: fees eat the whole target. A 'win' loses money.
Now (opening-range breakout). Out by 15:55 ET (12:55 PT) win or lose.
```

That is a real ticket from the original pack's test data (Oct 19 2026). Note the
warning: with a CAD account's ~3% round-trip conversion fee, most opening-range day
trades lose money even when they hit the target. The log tracks gross and net-of-fee
results side by side so you can see this for yourself.

### Which stocks you get

The originals' own order, unchanged:
- **day:** the earliest opening-range breakout first. Breakouts older than 20 minutes when the scan sees them are logged but not sent (too late to enter by hand).
- **swing:** the most dollars traded first (price × volume). The buy limit is the last close, the stop is 5% below it, and the target is 2R.

Tickets the original risk check BLOCKS (a stop too far for your $15 max loss, a share
costing more than the 75% position cap, ...) are logged with the reason and not sent.
Every raw scanner hit is kept in `data/scan_hits.csv`.

## Setup on your Mac (once, about 20 minutes)

1. **Get the code**
   ```bash
   git clone https://github.com/sharath957/Projects.git
   ```
   ```bash
   cd Projects/trade-signals
   ```
2. **Phone notifications (ntfy)**
   - Install the **ntfy** app (iOS / Android).
   - Make a long random topic name: `python3 -c "import secrets; print('sharath-' + secrets.token_hex(12))"`
   - In the app: Subscribe to topic, using that name.
3. **Settings**
   ```bash
   bash mac/install.sh
   ```
   The first run creates `config.json` (your numbers) and `.env` (secrets). Put the
   topic in `.env` as `NTFY_TOPIC=...`, then test:
   ```bash
   python3 run.py test-notify
   ```
4. **Notion (optional)**
   - Create an internal integration at https://www.notion.so/my-integrations and copy its secret into `.env` as `NOTION_TOKEN=...`.
   - Make an empty Notion page, then connect it to the integration (page menu, Connections).
   - Copy the page ID (the 32 characters at the end of its URL) and run:
     ```bash
     python3 run.py notion-init PAGE_ID
     ```
   - Put the printed ID in `config.json` under `notion.database_id`, and set `notion.enabled` to `true`.
5. **Keep the Mac awake on trading mornings** (needs your password):
   ```bash
   sudo pmset repeat wakeorpoweron MTWRF 06:30:00
   ```
   The jobs run under `caffeinate`, so the Mac stays awake while they run. If the lid is
   closed and it sleeps anyway, launchd runs the job when it wakes, and scans that are
   more than 10 minutes late are skipped.

To remove the schedule: `bash mac/uninstall.sh`. Your log stays.

## The log

`data/signals.csv` is the source of truth (on your Mac only; `data/` is git-ignored).
Notion is a copy of it.

| Group | Columns |
|---|---|
| Ticket | `signal_id, mode, generated_at_pt/et, session_date, symbol, setup, entry, stop, target, shares, position_usd, max_loss_usd` |
| Fees | `fx_fee_pct_per_side, fees_at_target_usd, fees_at_stop_usd, net_reward_usd, net_risk_usd, net_reward_risk` |
| Original checks | `risk_check (PASS/BLOCK), risk_reasons, tm_target_pct, tm_stop_pct, tm_neither_pct, tm_verdict, history_summary` |
| Outcome (tracker) | `status (open / target / stop / flat / expired / not_filled / blocked / capped), status_at, exit_price, gross_pnl_usd, fees_usd, net_pnl_usd, r_gross, r_net` |

The outcome is **what the ticket would have done** if placed exactly as written. It's
conservative: a bar that touches both the stop and the target counts as the stop, and
only bars after the push count for day trades. **Your real trades** go in Notion's
"My action / My fill $ / My exit $ / My exit at / My notes" columns. The script never
overwrites them. `python3 run.py notion-pull` copies them to `data/my_trades.csv`, which
lets you compare the plan against what you actually did. The two files are a ready-made
source for a dbt project.

`python3 run.py summary` prints results by mode and setup.

## Commands

```bash
python3 run.py day           # what the 6:40 AM job runs
```
```bash
python3 run.py after-close   # what the 1:35 PM job runs (swing + track + Notion)
```
```bash
python3 run.py summary
```
```bash
python3 -m unittest discover -s tests -v   # offline tests on the original pack's real fixtures
```

## Layout

```
run.py                 the command line
signals/core.py        config, clocks, runs the original scripts (--json)
signals/pipeline.py    day + swing runners, caps, tickets
signals/ticket.py      fee math, the CSV row, the phone message
signals/track.py       outcome tracker (Yahoo daily / 5-minute bars)
signals/notify.py      ntfy push
signals/notion.py      Notion database: init / sync / pull
signals/store.py       the CSV log
mac/install.sh         launchd schedule (no sudo)
vendor/day-trader-skill/   the original pack, unchanged (see VENDORED.md)
tests/                 offline tests
```

## Limits

- **Free, unofficial data.** Yahoo can lag, change or rate-limit you. A failed scan sends a "scan failed" push so a quiet day is never silent by accident.
- **Two parts haven't been tested live yet.** The offline tests cover the scans, planning, sizing, risk, tickets, caps and tracker. The ntfy push, the Notion API and live Yahoo calls couldn't be tested from the build environment. Run `test-notify`, `notion-init` and one live day before relying on them.
- **Day trades and the clock.** A push arrives a few seconds to minutes after a 5-minute bar closes, and your manual fill will differ from the ticket. Record your real fill in Notion.
- **Calendar.** The original's NYSE holiday list covers 2026-2027.
- **Fees.** `fx_fee_pct_per_side` is your conversion fee. If you open a USD account, set it to 0.
- **Taxes.** Frequent trading, especially in a TFSA, can be treated as business income by the CRA. Ask an accountant.
