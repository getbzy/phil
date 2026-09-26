#!/usr/bin/env python3
"""Paper-only Grok/X signal scout for prediction-market candidates.

This tool asks xAI's Responses API to use the server-side X Search tool for
one already-selected market candidate. It records the answer and reported tool
usage in journal/x-signal-requests.jsonl so later retros can compare
Grok-assisted forecasts against ordinary forecasts.

It never places trades, never changes forecasts, and never runs unless both:

  * XAI_API_KEY is present in the environment; and
  * PHIL_X_SIGNAL=1 is present, unless --force is passed for a manual test.

Usage:
  PHIL_X_SIGNAL=1 XAI_API_KEY=... python3 core/x_signal.py scout \
    --market-id 123 --question "Will ..." --outcome Yes \
    --deadline 2026-09-26T23:59:00Z --rules "Resolves according to ..."
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import pmapi

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "journal" / "x-signal-requests.jsonl"
API_URL = "https://api.x.ai/v1/responses"
DEFAULT_MODEL = "grok-4.7"
DEFAULT_MAX_POSTS = 50
X_POST_COST_FLOOR_USD = 0.005
X_USER_COST_FLOOR_USD = 0.01
OFFICIAL_RELEASE_TERMS = (
    "cpi", "ppi", "pce", "gdp", "nonfarm", "payroll", "unemployment",
    "fomc", "fed decision", "interest rate", "rate decision", "ecb",
    "boe", "central bank", "official cash rate", "consumer sentiment",
)
X_NATIVE_TERMS = (
    "tweet", "post", "x.com", "twitter", "say ", "says ", "said ",
    "announce", "announcement", "launch", "rumor", "viral",
    "statement", "speech", "interview", "spaces", "livestream",
)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(value):
    return " ".join(str(value or "").split())


def json_dumps(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def load_json(text):
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def extract_text(response):
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks = []
    for item in response.get("output", []) or []:
        if isinstance(item, dict):
            for part in item.get("content", []) or []:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
    return "\n".join(chunks).strip()


def usage_details(response):
    usage = response.get("usage") or {}
    details = usage.get("server_side_tool_usage_details") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "x_search_calls": details.get("x_search_calls", 0) or 0,
        "x_posts_fetched": details.get("x_posts_fetched", 0) or 0,
        "x_users_fetched": details.get("x_users_fetched", 0) or 0,
        "web_search_calls": details.get("web_search_calls", 0) or 0,
    }


def usage_cost_floor(details):
    # xAI docs as of 2026-09-22: X Search bills $5/1k posts and $10/1k users,
    # in addition to model tokens. Token costs are not estimated here.
    return round((details["x_posts_fetched"] * X_POST_COST_FLOOR_USD)
                 + (details["x_users_fetched"] * X_USER_COST_FLOOR_USD), 6)


def append_log(row):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def gamma_context(market_id):
    market = pmapi.gamma_market(market_id)
    question = market.get("question") or market.get("title") or ""
    deadline = market.get("endDate") or market.get("end_date") or ""
    rules = (
        market.get("description")
        or market.get("resolutionSource")
        or market.get("rules")
        or market.get("subtitle")
        or ""
    )
    outcomes = load_json(market.get("outcomes", "[]")) or []
    return {
        "question": question,
        "deadline": deadline,
        "rules": rules,
        "outcomes": outcomes,
    }


def hydrate_args(args):
    if args.no_fetch_market:
        return
    missing = not args.question or not args.deadline or not args.rules
    if not missing:
        return
    ctx = gamma_context(args.market_id)
    if not args.question:
        args.question = ctx["question"]
    if not args.deadline:
        args.deadline = ctx["deadline"]
    if not args.rules:
        args.rules = ctx["rules"]
    if not args.outcome and ctx["outcomes"]:
        args.outcome = ctx["outcomes"][0]


def eligibility(question, rules="", category=""):
    text = f"{question} {rules} {category}".lower()
    release_hits = [term for term in OFFICIAL_RELEASE_TERMS if term in text]
    x_hits = [term for term in X_NATIVE_TERMS if term in text]
    if release_hits and not x_hits:
        return {
            "fit": "low",
            "reason": "official scheduled data release; primary calendar/source beats X chatter",
            "hits": {"official_release": release_hits, "x_native": x_hits},
        }
    if x_hits:
        return {
            "fit": "high",
            "reason": "market appears driven by live narrative, statements, announcements, or X-native evidence",
            "hits": {"official_release": release_hits, "x_native": x_hits},
        }
    return {
        "fit": "medium",
        "reason": "X may help only if primary actors or original witnesses post relevant evidence",
        "hits": {"official_release": release_hits, "x_native": x_hits},
    }


def x_search_tool(args):
    tool = {"type": "x_search"}
    if args.allowed_x_handles:
        tool["allowed_x_handles"] = [h.lstrip("@") for h in args.allowed_x_handles]
    if args.excluded_x_handles:
        tool["excluded_x_handles"] = [h.lstrip("@") for h in args.excluded_x_handles]
    if args.from_date:
        tool["from_date"] = args.from_date
    if args.to_date:
        tool["to_date"] = args.to_date
    if args.enable_image_understanding:
        tool["enable_image_understanding"] = True
    if args.enable_video_understanding:
        tool["enable_video_understanding"] = True
    return tool


def build_prompt(args):
    market_p = "unknown" if args.market_p is None else f"{args.market_p:.4f}"
    own_p = "not yet formed" if args.own_p is None else f"{args.own_p:.4f}"
    return f"""Market candidate:
- market_id: {args.market_id}
- question: {one_line(args.question)}
- outcome being researched: {one_line(args.outcome)}
- deadline/end time UTC: {one_line(args.deadline)}
- market probability/price for this outcome: {market_p}
- Phil's pre-X probability for this outcome: {own_p}
- resolution rules/source: {one_line(args.rules)}

Use X Search only to find time-stamped public X evidence that is directly
relevant to this market's actual resolution criteria. Prefer primary actors,
official accounts, direct witnesses, and original documents over quote-tweets,
engagement bait, or unsourced rumor.

Do not recommend a trade. Do not change Phil's probability. Give research
context only. If X is not useful for this market, say so plainly.

Return concise JSON with these fields:
- summary: string
- relevant_signals: array of strings
- counter_signals: array of strings
- timestamp_notes: string
- resolver_relevance: string
- source_quality: "high" | "medium" | "low"
- probability_pressure: "up" | "down" | "mixed" | "none"
- cautions: array of strings
"""


def call_xai(api_key, payload, timeout):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"xAI returned non-JSON HTTP 200 body: {text[:500]}"
                ) from exc
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"xAI HTTP {exc.code}: {body_text[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"xAI transport error: {exc}") from exc


def cmd_scout(args):
    if args.allowed_x_handles and args.excluded_x_handles:
        sys.exit("ERROR: allowed and excluded X handles cannot both be set")
    if args.max_posts < 1:
        sys.exit("ERROR: --max-posts must be positive")
    hydrate_args(args)
    if not args.question:
        sys.exit("ERROR: --question is required when market fetch cannot fill it")
    if not args.outcome:
        sys.exit("ERROR: --outcome is required when market fetch cannot fill it")

    enabled = os.environ.get("PHIL_X_SIGNAL") == "1" or args.force
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    fit = eligibility(args.question, args.rules, args.category)
    if fit["fit"] == "low" and not args.ignore_eligibility:
        sys.exit("ERROR: X signal fit is low for this market "
                 f"({fit['reason']}); pass --ignore-eligibility only for a "
                 "manual operator test")
    prompt = build_prompt(args)
    payload = {
        "model": args.model,
        "input": [
            {
                "role": "system",
                "content": ("You are a paper-trading research scout. You gather "
                            "X evidence for one prediction-market candidate, "
                            "but you cannot trade, size, rewrite policy, or "
                            "override resolver rules."),
            },
            {"role": "user", "content": prompt},
        ],
        "tools": [x_search_tool(args)],
    }

    if args.dry_run:
        print(json.dumps({"dry_run": True, "payload": payload,
                          "x_signal_eligibility": fit}, indent=2,
                         sort_keys=True))
        return
    if not enabled:
        sys.exit("ERROR: PHIL_X_SIGNAL=1 is required for paid X Search "
                 "(or pass --force for a manual operator test)")
    if not api_key:
        sys.exit("ERROR: XAI_API_KEY is missing")

    started = dt.datetime.now(dt.timezone.utc)
    error = ""
    response = None
    try:
        response = call_xai(api_key, payload, args.timeout)
    except RuntimeError as exc:
        error = str(exc)
    latency_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds()
                     * 1000)

    if response is None:
        row = {
            "ts": utc_now(),
            "market_id": args.market_id,
            "question": one_line(args.question),
            "outcome": one_line(args.outcome),
            "model": args.model,
            "latency_ms": latency_ms,
            "error": error,
        }
        append_log(row)
        print(json.dumps(row, sort_keys=True))
        sys.exit(1)

    text = extract_text(response)
    parsed = load_json(text)
    details = usage_details(response)
    over_cap = details["x_posts_fetched"] > args.max_posts
    row = {
        "ts": utc_now(),
        "market_id": args.market_id,
        "question": one_line(args.question),
        "outcome": one_line(args.outcome),
        "deadline": one_line(args.deadline),
        "market_p": args.market_p,
        "own_p": args.own_p,
        "category": args.category,
        "x_signal_eligibility": fit,
        "model": args.model,
        "latency_ms": latency_ms,
        "x_usage": details,
        "x_search_cost_floor_usd": usage_cost_floor(details),
        "max_posts": args.max_posts,
        "over_post_cap": over_cap,
        "raw_text": text,
        "parsed_json": parsed,
        "error": "",
    }
    append_log(row)
    print(json.dumps(row, indent=2, sort_keys=True))
    if over_cap:
        sys.exit("ERROR: xAI fetched more X posts than the configured cap; "
                 "logged result but do not use it without review")


def cmd_doctor(args):
    info = {
        "phil_x_signal_enabled": os.environ.get("PHIL_X_SIGNAL") == "1",
        "xai_api_key_present": bool(os.environ.get("XAI_API_KEY", "").strip()),
        "model": os.environ.get("PHIL_XAI_MODEL", DEFAULT_MODEL),
        "max_posts": int(os.environ.get("PHIL_X_SIGNAL_MAX_POSTS",
                                        DEFAULT_MAX_POSTS)),
        "log_path": str(LOG.relative_to(ROOT)),
        "would_make_paid_call": False,
    }
    print(json.dumps(info, indent=2, sort_keys=True))


def cmd_eligible(args):
    question = args.question
    rules = args.rules
    if args.market_id:
        ctx = gamma_context(args.market_id)
        question = question or ctx["question"]
        rules = rules or ctx["rules"]
    result = eligibility(question, rules, args.category)
    result.update({
        "market_id": args.market_id,
        "question": one_line(question),
        "category": args.category,
    })
    print(json.dumps(result, indent=2, sort_keys=True))


def iter_jsonl(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if line.strip():
            row = load_json(line)
            if isinstance(row, dict):
                yield row


def cmd_summary(args):
    rows = list(iter_jsonl(LOG) or [])
    total_posts = sum((r.get("x_usage") or {}).get("x_posts_fetched", 0)
                      for r in rows)
    total_users = sum((r.get("x_usage") or {}).get("x_users_fetched", 0)
                      for r in rows)
    over_cap = sum(1 for r in rows if r.get("over_post_cap"))
    errors = sum(1 for r in rows if r.get("error"))
    pressures = {}
    for row in rows:
        parsed = row.get("parsed_json") or {}
        p = parsed.get("probability_pressure")
        if p:
            pressures[p] = pressures.get(p, 0) + 1

    forecast_notes = 0
    forecasts_path = ROOT / "journal" / "forecasts.jsonl"
    for row in iter_jsonl(forecasts_path) or []:
        if "x_signal:" in str(row.get("note", "")):
            forecast_notes += 1

    print(json.dumps({
        "requests": len(rows),
        "errors": errors,
        "over_post_cap": over_cap,
        "x_posts_fetched": total_posts,
        "x_users_fetched": total_users,
        "x_search_cost_floor_usd": usage_cost_floor({
            "x_posts_fetched": total_posts,
            "x_users_fetched": total_users,
        }),
        "probability_pressure_counts": pressures,
        "forecasts_with_x_signal_note": forecast_notes,
        "log_path": str(LOG.relative_to(ROOT)),
    }, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor", help="show setup state without API calls")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("eligible", help="classify whether X Search fits a market")
    p.add_argument("--market-id", default="")
    p.add_argument("--question", default="")
    p.add_argument("--rules", default="")
    p.add_argument("--category", default="")
    p.set_defaults(fn=cmd_eligible)

    p = sub.add_parser("summary", help="summarize logged X signal requests")
    p.set_defaults(fn=cmd_summary)

    p = sub.add_parser("scout", help="run one paper-only Grok/X evidence scout")
    p.add_argument("--market-id", required=True)
    p.add_argument("--question", default="")
    p.add_argument("--outcome", default="")
    p.add_argument("--deadline", default="")
    p.add_argument("--rules", default="")
    p.add_argument("--category", default="")
    p.add_argument("--market-p", type=float, default=None)
    p.add_argument("--own-p", type=float, default=None)
    p.add_argument("--model", default=os.environ.get("PHIL_XAI_MODEL",
                                                     DEFAULT_MODEL))
    p.add_argument("--allowed-x-handles", nargs="*", default=[])
    p.add_argument("--excluded-x-handles", nargs="*", default=[])
    p.add_argument("--from-date", default="")
    p.add_argument("--to-date", default="")
    p.add_argument("--max-posts", type=int, default=int(os.environ.get(
        "PHIL_X_SIGNAL_MAX_POSTS", DEFAULT_MAX_POSTS)),
        help="post-hoc usage cap; xAI may fetch/bill more before reporting usage")
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--enable-image-understanding", action="store_true")
    p.add_argument("--enable-video-understanding", action="store_true")
    p.add_argument("--no-fetch-market", action="store_true",
                   help="do not auto-fill question/rules/deadline from gamma")
    p.add_argument("--ignore-eligibility", action="store_true",
                   help="allow a low-fit market for a manual operator test")
    p.add_argument("--force", action="store_true",
                   help="manual operator test without PHIL_X_SIGNAL=1")
    p.add_argument("--dry-run", action="store_true",
                   help="print the request payload; no API call")
    p.set_defaults(fn=cmd_scout)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
