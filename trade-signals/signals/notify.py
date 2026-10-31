"""Phone push through ntfy (https://ntfy.sh): one HTTP POST, no account needed.

The topic name works like a password: anyone who knows it can read your
notifications. Use a long random one and keep it in .env, not in the repo."""

import os
import urllib.request

from .core import log

SENT = []  # tests read this when ntfy is off


def send(cfg, title, body, priority="default", tags=""):
    n = cfg.get("ntfy") or {}
    topic = os.environ.get(n.get("topic_env") or "NTFY_TOPIC", "").strip()
    SENT.append({"title": title, "body": body, "priority": priority})
    if not topic or os.environ.get("TRADE_SIGNALS_DRY_RUN"):
        log("NOTIFY (dry run, no ntfy topic) %s | %s" % (title, body.replace("\n", " | ")))
        return False
    url = "%s/%s" % ((n.get("server") or "https://ntfy.sh").rstrip("/"), topic)
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title.encode("ascii", "replace").decode("ascii"))
    req.add_header("Priority", priority)
    if tags:
        req.add_header("Tags", tags)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except Exception as e:  # a failed push must not stop the run: the CSV still has it
        log("NOTIFY FAILED (%s): %s" % (e, title))
        return False
