"""Compact view of a scan pool for unscreened cycles.

Usage: python3 core/scan.py ... | python3 strategy/tools/pool.py [--grep RE] [--max N]
       [--min-liq L] [--hours-min H] [--hours-max H]

Reads scan's JSONL from stdin, drops markets already in journal/forecasts.jsonl
(open or settled) and placeholder 0.5 books, and prints one line per market:
hours-to-end, Yes price, liquidity, 24h volume, id, question. Sorted by end date.
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grep", default=None)
    ap.add_argument("--exclude", default=None)
    ap.add_argument("--max", type=int, default=120)
    ap.add_argument("--min-liq", type=float, default=2000)
    ap.add_argument("--hours-min", type=float, default=0)
    ap.add_argument("--hours-max", type=float, default=1e9)
    a = ap.parse_args()

    seen = set()
    fpath = ROOT / "journal" / "forecasts.jsonl"
    if fpath.exists():
        for line in fpath.read_text().splitlines():
            try:
                seen.add(str(json.loads(line).get("market_id")))
            except json.JSONDecodeError:
                pass

    now = dt.datetime.now(dt.timezone.utc)
    inc = re.compile(a.grep, re.I) if a.grep else None
    exc = re.compile(a.exclude, re.I) if a.exclude else None
    rows = []
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(m.get("market_id")) in seen:
            continue
        q = m.get("question") or ""
        if inc and not inc.search(q):
            continue
        if exc and exc.search(q):
            continue
        try:
            p = float(m["outcome_prices"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if p in (0.5,) or p < 0.03 or p > 0.97:
            continue
        if m.get("liquidity", 0) < a.min_liq:
            continue
        try:
            end = dt.datetime.fromisoformat(m["end_date"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError):
            continue
        h = (end - now).total_seconds() / 3600
        if not a.hours_min <= h <= a.hours_max:
            continue
        rows.append((h, p, m))
    rows.sort(key=lambda r: r[0])
    for h, p, m in rows[: a.max]:
        print(f"{h:6.1f}h {p:.3f} liq{m['liquidity']:>8.0f} v24{m['volume_24h']:>8.0f} "
              f"{m['market_id']} {m['question'][:110]}")
    print(f"pool: {len(rows)} rows shown-cap {a.max}", file=sys.stderr)


if __name__ == "__main__":
    main()
