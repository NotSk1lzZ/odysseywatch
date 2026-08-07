#!/usr/bin/env python3
"""
odyssey-watch — Cinema City Praha Flora / IMAX 70mm watcher

Pings you when a NEW 70mm screening of The Odyssey appears at Flora, or when
tickets are (re-)released for a screening that was sold out.

How it works:
  * Uses Cinema City's public JSON "quickbook" API — the same API the website
    calls. No login, no key, no HTML scraping.
  * Keeps a small state file of screenings it has already seen (state/seen.json)
    so it only alerts on *changes*.
  * Zero third-party dependencies — Python 3.9+ standard library only.

Modes:
  python watch.py --once                     # one scan (simple cron)
  python watch.py --loop 60                   # poll every 60s forever (always-on machine)
  python watch.py --loop 60 --max-runtime 3300  # poll 60s for ~55min then exit (CI loop)
  python watch.py --seed                      # record current state WITHOUT alerting (first run)
  python watch.py --force                      # alert on everything currently listed (test)

In --loop mode each tick makes ONE cheap request (the dates list). A full
per-screening sweep only runs when a date appears/disappears, or every
--full-every seconds (default 300), so 60s polling stays light on the API.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ----------------------------------------------------------------------------
# Config — everything can be overridden with environment variables.
# ----------------------------------------------------------------------------
BASE = "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101"
CINEMA_ID  = os.environ.get("CINEMA_ID", "1052")            # 1052 = Praha Flora
ATTR       = os.environ.get("ATTR", "70-mm")               # server-side filter + local recheck
FILM_QUERY = os.environ.get("FILM_QUERY", "odysse").lower() # substring; matches "Odyssea"/"Odyssey"
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "90"))
LANG       = os.environ.get("LANG_CC", "cs_CZ")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))
REQ_DELAY  = float(os.environ.get("REQ_DELAY", "0.7"))      # politeness between requests
TIMEOUT    = float(os.environ.get("HTTP_TIMEOUT", "20"))
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "odyssey-watch/1.0 (personal showtime alert; https://github.com/)",
)

# Notifiers — set at least one. Telegram is the default; ntfy is a fallback.
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT    = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL   = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
def _get_json(url: str, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}\n  {last_err}")


def api_dates() -> list[str]:
    until = (datetime.now() + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")
    q = urllib.parse.urlencode({"attr": ATTR, "lang": LANG})
    url = f"{BASE}/dates/in-cinema/{CINEMA_ID}/until/{until}?{q}"
    return _get_json(url).get("body", {}).get("dates", [])


def api_events(date: str):
    q = urllib.parse.urlencode({"attr": ATTR, "lang": LANG})
    url = f"{BASE}/film-events/in-cinema/{CINEMA_ID}/at-date/{date}?{q}"
    body = _get_json(url).get("body", {})
    return body.get("films", []), body.get("events", [])


# ----------------------------------------------------------------------------
# Core scan: return {event_id: {...}} for screenings that match Odyssey + 70mm
# ----------------------------------------------------------------------------
def scan(dates: list | None = None) -> dict:
    matches: dict = {}
    if dates is None:
        dates = api_dates()
    for date in dates:
        films, events = api_events(date)
        names = {f.get("id"): (f.get("name") or "") for f in films}
        for ev in events:
            name = names.get(ev.get("filmId"), "")
            attrs = ev.get("attributeIds") or []
            if FILM_QUERY and FILM_QUERY not in name.lower():
                continue
            if ATTR and ATTR not in attrs:
                continue
            eid = str(ev.get("id"))
            link = ev.get("bookingRouterLaunchLink") or \
                f"https://www.cinemacity.cz/cz/booking-router/launch/{eid}?lang=cs"
            matches[eid] = {
                "film": name,
                "when": ev.get("eventDateTime", date),
                "auditorium": ev.get("auditorium", ""),
                "soldOut": bool(ev.get("soldOut", False)),
                "ratio": ev.get("availabilityRatio"),
                "attrs": attrs,
                "link": link,
            }
        time.sleep(REQ_DELAY)
    return matches


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ----------------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------------
def _pretty_when(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", ""))
        return dt.strftime("%a %d %b, %H:%M")
    except Exception:
        return iso


def send_ntfy(title: str, body: str, link: str) -> None:
    if not NTFY_TOPIC:
        return
    req = urllib.request.Request(
        f"{NTFY_URL}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("ascii", "replace").decode(),
            "Priority": "high",
            "Tags": "clapper",
            "Click": link,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def send_telegram(title: str, body: str, link: str) -> None:
    if not (TG_TOKEN and TG_CHAT):
        return
    text = f"*{title}*\n{body}\n{link}"
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": "false",
    }).encode()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=TIMEOUT).read()


DRY_RUN = False  # set by --dry-run: find matches but don't actually send


def notify(title: str, body: str, link: str) -> None:
    if DRY_RUN:
        print(f"[dry-run] WOULD ALERT: {title} | {body} | {link}")
        return
    sent = False
    for fn in (send_telegram, send_ntfy):
        try:
            fn(title, body, link)
            sent = sent or True
        except Exception as e:
            print(f"  [notify warning] {fn.__name__}: {e}", file=sys.stderr)
    # Always log to stdout too (shows up in CI logs / terminal).
    print(f"ALERT: {title} | {body} | {link}")
    if not sent and not ((TG_TOKEN and TG_CHAT) or NTFY_TOPIC):
        print("  (no notifier configured — set TELEGRAM_TOKEN/CHAT_ID or NTFY_TOPIC)",
              file=sys.stderr)


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------
def run_once(seed: bool = False, force: bool = False, dates: list | None = None) -> None:
    old = load_state()
    current = scan(dates=dates)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {len(current)} matching screening(s) found.")

    if seed:
        save_state(current)
        print("Seeded state; no alerts sent.")
        return

    for eid, ev in current.items():
        prev = old.get(eid)
        is_new = prev is None
        released = (not is_new) and prev.get("soldOut") and not ev["soldOut"]

        if force or is_new or released:
            when = _pretty_when(ev["when"])
            seats = "SOLD OUT" if ev["soldOut"] else (
                f"~{round((ev['ratio'] or 0) * 100)}% seats free" if ev["ratio"] is not None
                else "available")
            if is_new:
                head = "🎬 New Odyssey 70mm date at Flora"
            elif released:
                head = "🎟️ Tickets released — Odyssey 70mm at Flora"
            else:
                head = "Odyssey 70mm at Flora"
            body = f"{when} · {ev['auditorium']} · {seats}"
            notify(head, body, ev["link"])

    # Detect screenings that vanished (cancelled / date pulled) — optional signal.
    for eid, prev in old.items():
        if eid not in current:
            notify("⚠️ Odyssey 70mm screening removed",
                   f"{_pretty_when(prev.get('when',''))} is no longer listed", "")

    if not DRY_RUN:
        save_state(current)


def run_loop(interval: int, full_every: int, max_runtime: int,
             seed: bool = False, force: bool = False) -> None:
    """
    Poll every `interval` seconds. Each tick fetches only the cheap dates list
    (1 request). A full per-screening sweep runs when the set of dates changes
    (a NEW date — your main target, caught within one interval) or every
    `full_every` seconds (to catch sold-out -> available on existing dates).
    Exits after `max_runtime` seconds (0 = run forever).
    """
    start = time.time()
    last_full = 0.0
    known_dates: set | None = None
    print(f"Looping every {interval}s (full sweep on change or every {full_every}s"
          + (f", for {max_runtime}s" if max_runtime else "") + "). Ctrl-C to stop.")
    while True:
        tick = time.time()
        try:
            dates = api_dates()                       # cheap: one request
            dateset = set(dates)
            date_changed = known_dates is not None and dateset != known_dates
            due_full = (tick - last_full) >= full_every
            if known_dates is None or date_changed or due_full or seed or force:
                run_once(seed=seed, force=force, dates=dates)
                last_full = tick
                seed = force = False                  # only on the first pass
            else:
                print(f"[{datetime.now():%H:%M:%S}] no date change "
                      f"({len(dateset)} dates); skipping full sweep.")
            known_dates = dateset
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
        if max_runtime and (time.time() - start) >= max_runtime:
            print("Reached max runtime; exiting cleanly.")
            return
        # keep a steady cadence even if a sweep took a while
        time.sleep(max(1.0, interval - (time.time() - tick)))


def main() -> None:
    global DRY_RUN
    p = argparse.ArgumentParser(description="Cinema City Flora IMAX 70mm Odyssey watcher")
    p.add_argument("--once", action="store_true", help="single scan (default)")
    p.add_argument("--loop", type=int, metavar="SECONDS",
                   help="poll every N seconds (light dates check each tick)")
    p.add_argument("--full-every", type=int, default=int(os.environ.get("FULL_EVERY", "300")),
                   metavar="SECONDS", help="force a full sweep at least this often (default 300)")
    p.add_argument("--max-runtime", type=int, default=int(os.environ.get("MAX_RUNTIME", "0")),
                   metavar="SECONDS", help="exit after this long (0 = forever; use in CI)")
    p.add_argument("--seed", action="store_true", help="record current state, send no alerts")
    p.add_argument("--force", action="store_true", help="alert on everything currently listed")
    p.add_argument("--dry-run", action="store_true",
                   help="find matches and print them, but send NO notifications")
    p.add_argument("--test", action="store_true",
                   help="send one test notification to confirm delivery, then exit")
    args = p.parse_args()
    DRY_RUN = args.dry_run

    if args.test:
        print("Sending a test notification...")
        notify("✅ odyssey-watch works",
               "This is a test. Your Odyssey 70mm alerts will look like this.",
               "https://www.cinemacity.cz/cz/movies/the-odyssey")
        print("Done. If nothing arrived, check your TELEGRAM_TOKEN / TELEGRAM_CHAT_ID.")
        return

    if args.loop:
        run_loop(args.loop, args.full_every, args.max_runtime,
                 seed=args.seed, force=args.force)
    else:
        run_once(seed=args.seed, force=args.force)


if __name__ == "__main__":
    main()
