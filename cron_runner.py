#!/usr/bin/env python3
"""
cron_runner.py — Keeps running inside Railway (free tier) and fires
market_notifier.main() every weekday at 09:00 AM Pakistan time (PKT = UTC+5).

Railway's built-in cron requires a paid plan. This script is the FREE alternative:
deploy it as a long-running service, it sleeps between runs and wakes up on schedule.

Schedule (edit RUN_TIMES to add more):
  - 09:00 PKT  weekdays  →  market open summary
  - 16:30 PKT  weekdays  →  market close summary  (optional, see below)
"""

import time
import logging
from datetime import datetime, timezone, timedelta

# ── Import the notifier (same directory) ──────────────────────────────────────
import market_notifier

# ── Config ────────────────────────────────────────────────────────────────────
PKT = timezone(timedelta(hours=5))          # Pakistan Standard Time (UTC+5)

# (hour, minute) pairs in PKT when the report should be sent
# Weekdays only (Mon=0 … Fri=4). Remove or add as you like.
RUN_TIMES = [
    (11, 0),    # 09:00 PKT — morning briefing
     (18, 30),  # 16:30 PKT — close of market  ← uncomment to enable
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cron_runner")

# Track which (date, hour, minute) slots have already fired today
fired: set = set()


def should_fire(now: datetime) -> bool:
    """Return True if now matches a scheduled slot that hasn't fired yet."""
    if now.weekday() > 4:          # skip Saturday (5) and Sunday (6)
        return False
    slot = (now.date(), now.hour, now.minute)
    for h, m in RUN_TIMES:
        if now.hour == h and now.minute == m and slot not in fired:
            fired.add(slot)
            # Clean up old dates from the set to avoid unbounded growth
            today = now.date()
            stale = {s for s in fired if s[0] < today}
            fired.difference_update(stale)
            return True
    return False


def run():
    log.info("=== Market Notifier cron_runner started ===")
    log.info(f"Schedule (PKT): {RUN_TIMES}  |  weekdays only")

    while True:
        now_pkt = datetime.now(tz=PKT)

        if should_fire(now_pkt):
            log.info(f"--- Firing at {now_pkt:%Y-%m-%d %H:%M} PKT ---")
            try:
                market_notifier.main()
                log.info("--- Done ---")
            except Exception as e:
                log.error(f"market_notifier.main() raised: {e}", exc_info=True)
        else:
            # Calculate seconds until next scheduled slot so we can log a
            # helpful "next run in X min" message every 30 minutes.
            if now_pkt.minute % 30 == 0:
                next_runs = []
                for h, m in RUN_TIMES:
                    candidate = now_pkt.replace(hour=h, minute=m, second=0, microsecond=0)
                    if candidate <= now_pkt:
                        candidate = candidate + timedelta(days=1)
                    # Skip to next weekday if needed
                    while candidate.weekday() > 4:
                        candidate += timedelta(days=1)
                    next_runs.append(candidate)
                if next_runs:
                    nxt = min(next_runs)
                    diff = nxt - now_pkt
                    hours, rem = divmod(int(diff.total_seconds()), 3600)
                    mins = rem // 60
                    log.info(f"Next run: {nxt:%Y-%m-%d %H:%M} PKT  (in {hours}h {mins}m)")

        time.sleep(60)   # check every minute


if __name__ == "__main__":
    run()
