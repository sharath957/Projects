"""Copy data/signals.csv into a Notion database (one page per ticket).

The script owns the ticket columns and rewrites them when the tracker updates a
row. The "My ..." columns are yours: the script never writes them, so what you
type in Notion (bought or skipped, your real fill, your exit, notes) is safe.
`pull` copies those columns back to data/my_trades.csv for analysis.

Needs a Notion internal integration token in NOTION_TOKEN (.env) and the
database shared with that integration. Uses Notion API version 2022-06-28.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from . import store
from .core import DATA, load_config, log, write_json

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
STATE = os.path.join(DATA, "notion_state.json")
MY_TRADES = os.path.join(DATA, "my_trades.csv")

COLS = {  # csv field -> (Notion property, type)
    "signal_id": ("Signal ID", "rich_text"), "mode": ("Mode", "select"), "session_date": ("Session date", "date"),
    "generated_at_pt": ("Sent (PT)", "rich_text"), "symbol": ("Symbol", "rich_text"), "setup": ("Setup", "rich_text"),
    "entry": ("Buy limit", "number"), "stop": ("Stop-loss", "number"), "target": ("Sell limit (target)", "number"),
    "shares": ("Shares", "number"), "position_usd": ("Position $", "number"), "max_loss_usd": ("Max loss $", "number"),
    "fees_at_target_usd": ("FX fees at target $", "number"), "fees_at_stop_usd": ("FX fees at stop $", "number"),
    "net_reward_usd": ("Net win $", "number"), "net_risk_usd": ("Net loss $", "number"),
    "net_reward_risk": ("Net R:R", "number"), "risk_check": ("Risk check", "select"),
    "risk_reasons": ("Risk reasons", "rich_text"), "notified": ("Notified", "select"),
    "tm_target_pct": ("TM target %", "number"), "tm_stop_pct": ("TM stop %", "number"),
    "flat_by_et": ("Flat by (ET)", "rich_text"), "status": ("Outcome", "select"),
    "status_at": ("Outcome at", "rich_text"), "exit_price": ("Exit price", "number"),
    "gross_pnl_usd": ("Gross P&L $", "number"), "net_pnl_usd": ("Net P&L $", "number"), "r_net": ("R (net)", "number"),
    "notes": ("Notes", "rich_text"),
}
MINE = {"My action": {"select": {"options": [{"name": "bought"}, {"name": "skipped"}]}},
        "My fill $": {"number": {}}, "My exit $": {"number": {}}, "My exit at": {"rich_text": {}},
        "My notes": {"rich_text": {}}}


def _token(cfg):
    t = os.environ.get(cfg["notion"].get("token_env") or "NOTION_TOKEN", "").strip()
    if not t:
        raise RuntimeError("no Notion token: put NOTION_TOKEN=... in trade-signals/.env")
    return t


def _call(cfg, method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode("utf-8") if body is not None else None)
    req.add_header("Authorization", "Bearer " + _token(cfg))
    req.add_header("Notion-Version", VERSION)
    req.add_header("Content-Type", "application/json")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(float(e.headers.get("Retry-After") or 1))
                continue
            raise RuntimeError("Notion %s %s: %s %s" % (method, path, e.code, e.read().decode("utf-8", "replace")[:300]))
    raise RuntimeError("Notion rate limit")


def init(parent_page_id, cfg=None):
    """Create the database under a Notion page you shared with the integration."""
    cfg = cfg or load_config()
    props = {"Name": {"title": {}}}
    for field, (name, typ) in COLS.items():
        props[name] = {"number": {"format": "number"}} if typ == "number" else {typ: {}}
    props.update(MINE)
    doc = _call(cfg, "POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "Trade Signals"}}], "properties": props})
    return doc["id"]


def _props(row):
    out = {"Name": {"title": [{"text": {"content": "%s %s %s" % (row["symbol"], row["mode"], row["session_date"])}}]}}
    for field, (name, typ) in COLS.items():
        v = row.get(field, "")
        if typ == "number":
            try:
                out[name] = {"number": float(v)} if v not in ("", None) else {"number": None}
            except ValueError:
                out[name] = {"number": None}
        elif typ == "select":
            out[name] = {"select": {"name": str(v)[:100]}} if v else {"select": None}
        elif typ == "date":
            out[name] = {"date": {"start": v}} if v else {"date": None}
        else:
            out[name] = {"rich_text": [{"text": {"content": str(v)[:1900]}}] if v else []}
    return out


def sync(cfg=None):
    """Create or update one page per CSV row. Rows whose content did not change are skipped;
    a row that fails is retried on the next run (its state is not saved)."""
    cfg = cfg or load_config()
    db = cfg["notion"].get("database_id")
    if not cfg["notion"].get("enabled") or not db:
        log("Notion sync is off (set notion.enabled and notion.database_id in config.json).")
        return 0
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    n = 0
    for row in store.read_rows():
        h = hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()
        st = state.get(row["signal_id"]) or {}
        if st.get("hash") == h:
            continue
        try:
            if st.get("page_id"):
                _call(cfg, "PATCH", "/pages/%s" % st["page_id"], {"properties": _props(row)})
            else:
                page = _call(cfg, "POST", "/pages", {"parent": {"database_id": db}, "properties": _props(row)})
                st["page_id"] = page["id"]
            st["hash"] = h
            state[row["signal_id"]] = st
            n += 1
        except Exception as e:
            log("Notion sync %s failed (will retry next run): %s" % (row["signal_id"], e))
        write_json(STATE, state)
    log("Notion: %d page(s) created or updated." % n)
    return n


def pull(cfg=None):
    """Copy your own 'My ...' columns from Notion to data/my_trades.csv."""
    cfg = cfg or load_config()
    db = cfg["notion"]["database_id"]
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        doc = _call(cfg, "POST", "/databases/%s/query" % db, body)
        for p in doc.get("results", []):
            pr = p["properties"]
            txt = lambda k: "".join(x.get("plain_text", "") for x in (pr.get(k) or {}).get("rich_text", []))
            num = lambda k: (pr.get(k) or {}).get("number")
            sel = (pr.get("My action") or {}).get("select")
            rows.append({"signal_id": txt("Signal ID"), "my_action": sel["name"] if sel else "",
                         "my_fill_usd": num("My fill $"), "my_exit_usd": num("My exit $"),
                         "my_exit_at": txt("My exit at"), "my_notes": txt("My notes")})
        if not doc.get("has_more"):
            break
        cursor = doc.get("next_cursor")
    store.write_rows(rows, MY_TRADES, ["signal_id", "my_action", "my_fill_usd", "my_exit_usd", "my_exit_at", "my_notes"])
    log("Wrote %d row(s) to %s" % (len(rows), MY_TRADES))
    return len(rows)
