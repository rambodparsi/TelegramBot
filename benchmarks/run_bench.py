"""Milestone 0 benchmark runner.

Runs each probe N times in its own time window, then reads CPU-time quantiles for that
window from Cloudflare's Workers analytics (GraphQL). Prints a Markdown table.

Env: BENCH_URL, BENCH_TOKEN, CF_ACCOUNT_ID; Cloudflare auth header via CF_API_TOKEN
(in this repo's cloud environment the proxy injects it, so any placeholder works).

Usage: python benchmarks/run_bench.py [N] [probe ...]
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.request
import uuid

URL = os.environ["BENCH_URL"].rstrip("/")
TOKEN = os.environ["BENCH_TOKEN"]
ACCOUNT = os.environ["CF_ACCOUNT_ID"]
SCRIPT = os.environ.get("BENCH_SCRIPT", "tg-assistant-staging")
CF_TOKEN = os.environ.get("CF_API_TOKEN", "proxy-injected")

PROBES = {
    "noop": lambda i, run: "/bench/noop",
    "graph-int": lambda i, run: f"/bench/graph-int?thread=int-{run}-{i}",
    # same thread every call: history grows by 4 messages per call
    "graph-messages-growing": lambda i, run: f"/bench/graph-messages?thread=grow-{run}",
    # fresh thread every call
    "graph-messages-fresh": lambda i, run: f"/bench/graph-messages?thread=fresh-{run}-{i}",
    "gemini": lambda i, run: f"/bench/gemini?thread=gem-{run}&text=Say+hi+in+5+words",
}


def call(path: str) -> tuple[int, float, str]:
    req = urllib.request.Request(URL + path, headers={"x-bench-token": TOKEN, "user-agent": "tg-assistant-bench/1.0"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read().decode()
            return r.status, time.time() - t0, body
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0, e.read().decode()[:300]


def analytics(start: dt.datetime, end: dt.datetime) -> list[dict]:
    query = """query($a:String!,$s:String!,$from:Time!,$to:Time!){viewer{accounts(filter:{accountTag:$a}){
      workersInvocationsAdaptive(limit:20,filter:{scriptName:$s,datetime_geq:$from,datetime_leq:$to}){
        sum{requests errors} dimensions{status}
        quantiles{cpuTimeP25 cpuTimeP50 cpuTimeP75 cpuTimeP90 cpuTimeP99 cpuTimeP999}}}}}"""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    body = json.dumps({"query": query, "variables": {
        "a": ACCOUNT, "s": SCRIPT, "from": start.strftime(fmt), "to": end.strftime(fmt)}}).encode()
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/graphql", data=body,
        headers={"Authorization": f"Bearer {CF_TOKEN}", "content-type": "application/json", "user-agent": "tg-assistant-bench/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    return data["data"]["viewer"]["accounts"][0]["workersInvocationsAdaptive"]


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    names = sys.argv[2:] or [p for p in PROBES if p != "gemini"]
    run = uuid.uuid4().hex[:6]
    windows = []
    for name in names:
        start = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        results = [call(PROBES[name](i, run)) for i in range(n)]
        end = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
        ok = sum(1 for s, _, _ in results if s == 200)
        wall = sorted(t for _, t, _ in results)
        errors = {b[:160] for s, _, b in results if s != 200}
        windows.append((name, start, end, ok, wall, errors))
        print(f"{name}: {ok}/{n} ok, wall p50={wall[len(wall)//2]:.2f}s", file=sys.stderr)
        time.sleep(61)  # keep probe windows in separate minutes

    print("waiting for analytics to settle...", file=sys.stderr)
    time.sleep(90)
    print("| probe | ok | wall p50 | status | CPU p50 | p75 | p90 | p99 | max-ish (p99.9) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for name, start, end, ok, wall, errors in windows:
        for row in analytics(start, end):
            q = row["quantiles"]
            ms = lambda k: f"{q[k] / 1000:.1f} ms"  # noqa: E731
            print(f"| {name} | {ok}/{len(wall)} | {wall[len(wall)//2]:.2f}s | "
                  f"{row['dimensions']['status']} ({row['sum']['requests']}) | {ms('cpuTimeP50')} | "
                  f"{ms('cpuTimeP75')} | {ms('cpuTimeP90')} | {ms('cpuTimeP99')} | "
                  f"{ms('cpuTimeP999')} |")
        for e in errors:
            print(f"|  ↳ error sample | {e!r} |||||||| ")


if __name__ == "__main__":
    main()
