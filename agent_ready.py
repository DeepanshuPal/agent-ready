#!/usr/bin/env python3
"""agent-ready: audit an ecommerce store for AI shopping-agent readiness.

Usage:
    python3 agent_ready.py <store-url> [--json] [--timeout SECONDS]

Example:
    python3 agent_ready.py https://allbirds.com
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import requests

from checks import (
    AI_USER_AGENTS,
    DEFAULT_UA,
    CheckResult,
    check_checkout,
    check_llms_txt,
    check_page_structure,
    check_products_json,
    check_robots,
    check_structured_data,
    detect_platform,
    normalize_url,
)

WEIGHTS = {
    "products.json": 0.25,
    "structured data": 0.20,
    "robots.txt": 0.15,
    "checkout handoff": 0.15,
    "page structure": 0.15,
    "llms.txt": 0.10,
}

ICON = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO", "skip": "SKIP"}


def grade(score: float) -> str:
    for cutoff, letter in ((85, "A"), (70, "B"), (55, "C"), (40, "D")):
        if score >= cutoff:
            return letter
    return "F"


def recommendations(results: list[CheckResult]) -> list[str]:
    recs = []
    by_name = {r.name: r for r in results}
    if by_name["products.json"].status == "fail":
        recs.append("Expose a machine-readable catalog (Shopify products.json, "
                    "a Google Merchant feed, or a /products API).")
    if by_name["structured data"].status != "pass":
        recs.append("Add schema.org Product JSON-LD (name, price, currency, "
                    "availability) to every product page.")
    if by_name["robots.txt"].status != "pass":
        recs.append("Review robots.txt: explicitly allow the AI agents you want "
                    "shopping from you instead of leaving it ambiguous.")
    if by_name["llms.txt"].status == "fail":
        recs.append("Publish /llms.txt describing the store, catalog endpoints "
                    "and policies in plain markdown.")
    if by_name["checkout handoff"].status != "pass":
        recs.append("Make checkout handoff deterministic: a deep link or API that "
                    "drops a chosen variant into a cart an agent can hand to a human.")
    if by_name["page structure"].status != "pass":
        recs.append("Clean up homepage semantics: one <h1>, alt text on images, "
                    "landmark tags, less markup per byte of content.")
    return recs


def audit(url: str, timeout: int = 20) -> dict:
    base = normalize_url(url)
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA,
                            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})

    started = time.time()
    home = session.get(base, timeout=timeout, allow_redirects=True)
    final_url = home.url.rstrip("/")
    home_html = home.text
    platform = detect_platform(home_html, home.headers)

    results: list[CheckResult] = []
    results.append(check_robots(session, final_url, timeout))
    results.append(check_llms_txt(session, final_url, timeout))
    pj_result, products = check_products_json(session, final_url, timeout)
    results.append(pj_result)
    results.append(check_structured_data(session, final_url, home_html, timeout,
                                         products=products))
    results.append(check_checkout(session, final_url, timeout, products=products))
    results.append(check_page_structure(home_html))

    total = sum(WEIGHTS[r.name] * r.score for r in results)
    elapsed = round(time.time() - started, 1)

    return {
        "url": final_url,
        "platform": platform,
        "score": round(total),
        "grade": grade(total),
        "elapsed_seconds": elapsed,
        "checks": [
            {"name": r.name, "status": r.status, "score": round(r.score),
             "summary": r.summary, "details": r.details}
            for r in results
        ],
        "recommendations": recommendations(results),
    }


def print_text(report: dict) -> None:
    print(f"\nagent-ready report: {report['url']}")
    print(f"platform: {report['platform']}   "
          f"score: {report['score']}/100 ({report['grade']})   "
          f"audited in {report['elapsed_seconds']}s")
    print("-" * 72)
    for c in report["checks"]:
        print(f"[{ICON[c['status']]}] {c['name']:<18} {c['score']:>3}/100  {c['summary']}")
        for d in c["details"]:
            print(f"       - {d}")
    if report["recommendations"]:
        print("-" * 72)
        print("top fixes, in order of leverage:")
        for i, rec in enumerate(report["recommendations"], 1):
            print(f"  {i}. {rec}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit an ecommerce store for AI shopping-agent readiness.")
    parser.add_argument("url", help="Store URL, e.g. https://allbirds.com")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--timeout", type=int, default=20,
                        help="Per-request timeout in seconds (default 20)")
    args = parser.parse_args()

    try:
        report = audit(args.url, timeout=args.timeout)
    except requests.RequestException as exc:
        print(f"error: could not fetch {args.url}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
