#!/usr/bin/env python3
"""agent-ready: audit an ecommerce store for AI shopping-agent readiness.

Usage:
    python3 agent_ready.py <store-url> [--json] [--timeout SECONDS]
    python3 agent_ready.py --bulk stores.txt [--out report] [--workers 5]

Example:
    python3 agent_ready.py https://allbirds.com
    python3 agent_ready.py --bulk stores.txt --out audits/2026-09-12/results
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import median
from urllib.parse import urlparse

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

    # Root-level files (robots.txt, llms.txt, products.json) live at the origin
    # root. If the homepage redirected (e.g. loom.fr -> /fr-fr), probe them at
    # the root, not under the redirected path.
    parsed = urlparse(final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    results: list[CheckResult] = []
    results.append(check_robots(session, origin, timeout))
    results.append(check_llms_txt(session, origin, timeout))
    pj_result, products = check_products_json(session, origin, timeout)
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


CHECK_ORDER = ["robots.txt", "llms.txt", "products.json", "structured data",
               "checkout handoff", "page structure"]


def read_store_list(path: str) -> list[str]:
    """One store URL per line; blank lines and # comments ignored."""
    urls = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def audit_safe(url: str, timeout: int) -> dict:
    """audit() that never raises - one broken store must not kill a batch."""
    try:
        return audit(url, timeout=timeout)
    except requests.RequestException as exc:
        return {"url": url, "error": str(exc)}
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


def bulk_report(reports: list[dict]) -> dict:
    """Turn per-store audit dicts into one ranked, aggregate report."""
    rows = []
    for rep in reports:
        if "error" in rep:
            rows.append({"store": rep["url"], "error": rep["error"]})
            continue
        row = {
            "store": rep["url"],
            "platform": rep["platform"],
            "score": rep["score"],
            "grade": rep["grade"],
            "top_fix": rep["recommendations"][0] if rep["recommendations"] else "",
            "elapsed_seconds": rep["elapsed_seconds"],
        }
        for c in rep["checks"]:
            row[c["name"]] = c["score"]
        rows.append(row)

    ok = sorted((r for r in rows if "score" in r), key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(ok, 1):
        r["rank"] = i
    failed = [r for r in rows if "score" not in r]

    scores = [r["score"] for r in ok]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stores_requested": len(reports),
        "audited": len(ok),
        "failed": len(failed),
        "median_score": round(median(scores), 1) if scores else None,
        "mean_score": round(sum(scores) / len(scores), 1) if scores else None,
        "stores": ok + failed,
    }


def write_bulk_outputs(report: dict, out_prefix: str) -> tuple[str, str]:
    csv_path = out_prefix + ".csv"
    json_path = out_prefix + ".json"

    fieldnames = ["rank", "store", "platform", "score", "grade"] + CHECK_ORDER + [
        "top_fix", "error", "elapsed_seconds"]
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["stores"])

    with open(json_path, "w") as fh:
        json.dump(report, fh, indent=2)

    return csv_path, json_path


def print_bulk_summary(report: dict) -> None:
    print(f"\nagent-ready bulk report: {report['audited']}/{report['stores_requested']} "
          f"stores audited, {report['failed']} failed")
    if report["median_score"] is not None:
        print(f"median score: {report['median_score']}   mean: {report['mean_score']}")
    print("-" * 72)
    print(f"{'rank':>4}  {'score':>5}  {'grade':>5}  store")
    for row in report["stores"]:
        if "score" in row:
            print(f"{row['rank']:>4}  {row['score']:>5}  {row['grade']:>5}  {row['store']}")
        else:
            print(f"{'-':>4}  {'ERR':>5}  {'-':>5}  {row['store']}  ({row['error']})")
    print()


def run_bulk(path: str, out_prefix: str, workers: int, timeout: int) -> int:
    urls = read_store_list(path)
    if not urls:
        print(f"error: no store URLs found in {path}", file=sys.stderr)
        return 2
    print(f"auditing {len(urls)} stores with {workers} workers...")

    reports: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(audit_safe, u, timeout): u for u in urls}
        done = 0
        for fut in as_completed(futures):
            reports.append(fut.result())
            done += 1
            print(f"  [{done}/{len(urls)}] {futures[fut]}", file=sys.stderr)

    report = bulk_report(reports)
    csv_path, json_path = write_bulk_outputs(report, out_prefix)
    print_bulk_summary(report)
    print(f"wrote {csv_path} and {json_path}")
    return 0


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
    parser.add_argument("url", nargs="?", help="Store URL, e.g. https://allbirds.com")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--timeout", type=int, default=20,
                        help="Per-request timeout in seconds (default 20)")
    parser.add_argument("--bulk", metavar="FILE",
                        help="Audit every store listed in FILE (one URL per line) "
                             "and write a combined CSV + JSON report")
    parser.add_argument("--out", default="agent-ready-bulk",
                        help="Output prefix for --bulk reports "
                             "(default agent-ready-bulk -> .csv and .json)")
    parser.add_argument("--workers", type=int, default=5,
                        help="Parallel stores for --bulk (default 5)")
    args = parser.parse_args()

    if args.bulk:
        return run_bulk(args.bulk, args.out, args.workers, args.timeout)
    if not args.url:
        parser.error("give a store URL, or --bulk FILE")

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
