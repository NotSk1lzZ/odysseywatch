# odyssey-watch

Alerts you when a **70mm screening of *Dune (Duna)*** appears (or tickets are
re-released) at **Cinema City Praha Flora** — by polling Cinema City's public
JSON API instead of scraping the website.

It fires on three events:

- 🎬 **New date** — a 70mm Dune screening that wasn't listed before
- 🎟️ **Tickets released** — a screening that flipped from *sold out* to *available*
- ⚠️ **Removed** — a screening that disappeared (cancelled / pulled)

No login, no API key, no dependencies (Python 3.9+ standard library only).

---

## 1. Pick a notifier

You need at least one. **Telegram is the default.**

### Option A — Telegram (default)
1. Message **@BotFather**, send `/newbot`, copy the token.
2. Message your new bot once (say "hi"), then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your `chat.id`.
3. Set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.

### Option B — ntfy (no account, fallback)
1. Install the **ntfy** app (iOS / Android) or open <https://ntfy.sh> in a browser.
2. Subscribe to a topic — pick something long and unguessable, e.g.
   `odyssey-flora-7hk28df`. Anyone who knows the topic can read it, so treat it
   like a password.
3. Set the env var `NTFY_TOPIC=odyssey-flora-7hk28df`.

---

## 2. Test it works

Two quick checks (need Python locally; no local Python? use the forced GitHub
run in step 4 of section 3 instead):

```bash
# a) Delivery test — sends ONE message to your phone
export TELEGRAM_TOKEN=123456:ABC... TELEGRAM_CHAT_ID=987654   # or NTFY_TOPIC=...
python watch.py --test

# b) Pipeline test — prints every current match, sends NOTHING
python watch.py --once --force --dry-run
```

Test (a) confirms your bot token + chat ID are right. Test (b) confirms the API,
the Dune/70mm filter, and message formatting — you'll see the currently listed
Flora dates printed as `[dry-run] WOULD ALERT: ...` without spamming your phone.

---

## 3. Run it

### Path A — GitHub Actions (recommended: free, always-on, zero maintenance)

1. Create a **public** repo (public repos get unlimited free Actions minutes)
   and push these files into it.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   add `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` (or an `NTFY_TOPIC` secret).
3. **Settings → Actions → General → Workflow permissions →** enable
   *Read and write permissions* (lets the job save its state file).
4. (Optional but nice) Seed the state once so your first run doesn't alert you
   about every date already listed:
   ```bash
   TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... python watch.py --seed
   git add state/seen.json && git commit -m "seed" && git push
   ```
5. Done. Trigger a manual test under **Actions → odyssey-watch → Run workflow**
   (tick *force* to see current dates).

> **How the frequency works.** GitHub's cron floor is 5 min and scheduled runs
> are only best-effort — they get delayed under load and are sometimes dropped,
> so a small cron number doesn't actually give reliable fast polling. Instead,
> this workflow fires **once an hour** and then the job **loops internally every
> 60 seconds for ~55 minutes**, committing its state on exit. The hourly trigger
> only has to land once per hour, so its lag is harmless, and you get true ~60s
> resolution the rest of the time. There's a ~1–3 min gap at each hourly handoff.
>
> To stay polite at 60s, each tick only fetches the cheap *dates* list (one
> request); a full per-screening sweep runs only when a date changes or every
> 5 min (`--full-every`). Public repos get unlimited free Actions minutes, so the
> near-continuous run costs nothing.
>
> For **sub-minute, zero-gap** polling, run Path B on an always-on machine.

### Path B — Always-on machine (your PC, a Raspberry Pi, or a small VPS)

```bash
export TELEGRAM_TOKEN=123456:ABC... TELEGRAM_CHAT_ID=987654   # or NTFY_TOPIC=...
python watch.py --seed                        # first time only: no spam
python watch.py --loop 30                      # poll every 30s, forever
```

`--loop N` checks the cheap dates list every N seconds and only does a full
sweep when a date changes or every `--full-every` seconds (default 300). So even
`--loop 20` stays light. This is the most reliable option — no scheduler, no
gaps. Leave it running in `tmux`, or install it as a `systemd` service.

---

## 4. Tuning

Everything is an environment variable:

| Var          | Default   | Meaning                                              |
|--------------|-----------|------------------------------------------------------|
| `CINEMA_ID`  | `1052`    | Praha Flora. Change to watch another Cinema City.    |
| `FILM_QUERY` | `dun`    | Case-insensitive substring; matches Dune/Duna.          |
| `ATTR`       | `70-mm`   | Screening attribute filter. Set empty to allow all.  |
| `DAYS_AHEAD` | `365`     | How far ahead to scan (presales can be months out). |
| `FULL_EVERY` | `300`     | Loop mode: force a full sweep at least this often (s). |
| `MAX_RUNTIME`| `0`       | Loop mode: exit after this many seconds (0 = forever). |
| `REQ_DELAY`  | `0.7`     | Politeness delay between API requests (s).           |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | — | Telegram (default notifier).        |
| `NTFY_TOPIC` | —         | ntfy topic to publish to (fallback).                 |

**Want alerts for *any* 70mm film at Flora, not just Dune?** Set
`FILM_QUERY=` (empty).

**Want to also catch a normal (non-70mm) IMAX Dune showing?** Set `ATTR=`
(empty) — you'll then get every Dune format at Flora.

---

## How it works (the API)

```
Base: https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101
  Dates : /dates/in-cinema/1052/until/{YYYY-MM-DD}?attr=70-mm&lang=cs_CZ
  Events: /film-events/in-cinema/1052/at-date/{YYYY-MM-DD}?attr=70-mm&lang=cs_CZ
```

The dates call (filtered to `70-mm`) returns just the handful of days that have
a 70mm show, so a full scan is only a few requests. Each event carries
`soldOut`, `availabilityRatio`, `auditorium`, `attributeIds`, and a direct
`bookingRouterLaunchLink`. The script keeps a `state/seen.json` of what it has
seen and only alerts on differences.

Please keep the polling gentle (the built-in `REQ_DELAY` and a 60s+ loop are
fine) — it's an unofficial endpoint and the goal is just to get your ticket.
