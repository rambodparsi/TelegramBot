# Milestone 0 — Benchmark results

Run 2026-11-22 against `tg-assistant-staging` (Workers **Free** plan), 20 sequential
requests per probe, CPU time from Cloudflare's Workers analytics
(`workersInvocationsAdaptive`). Reproduce with `benchmarks/run_bench.py`.

| probe | ok | wall p50 | status | CPU p50 | p75 | p90 | p99 | max-ish (p99.9) |
|---|---|---|---|---|---|---|---|---|
| noop | 20/20 | 0.17s | success (20) | 8.6 ms | 16.8 ms | 25.8 ms | 39.7 ms | 39.7 ms |
| graph-int | 20/20 | 0.36s | success (20) | 67.9 ms | 91.8 ms | 134.3 ms | 182.9 ms | 182.9 ms |
| graph-messages-growing | 20/20 | 0.51s | success (19) | 112.0 ms | 201.6 ms | 405.4 ms | 408.5 ms | 408.5 ms |
| graph-messages-fresh | 20/20 | 0.40s | success (20) | 58.0 ms | 84.5 ms | 108.0 ms | 176.3 ms | 176.3 ms |

Probes:
- **noop** — Python Worker, no work (baseline per-request runtime cost).
- **graph-int** — Cloudflare's official example graph (int state) with our D1 saver.
- **graph-messages-fresh** — agent → tool → agent loop with LangChain messages, new thread each call.
- **graph-messages-growing** — same, one thread; history grows 4 messages per call (4 → 80).
- **gemini** — not yet measured: the Gemini key secret on staging is empty (see below).

## Findings

1. **It works.** 80/80 requests succeeded, including multi-step graphs with tool calls,
   checkpointing to D1, and resuming a conversation across requests.
2. **CPU is far above the documented 10 ms Free-plan limit**, yet requests were not
   killed: graph runs take ~60–110 ms p50 and up to ~400 ms. Cloudflare evidently
   tolerates this today, but we are relying on undocumented slack. One earlier request
   *was* killed (`exceededResources`) at ~105 ms while importing SQLAlchemy lazily.
3. **CPU grows with conversation length** (fresh 58 ms p50 → growing 112 ms p50,
   405 ms p90 at ~80 messages): every super-step deserializes/serializes the whole
   message list. Trimming state is mandatory, not optional.
4. **Imports must happen at module top level.** Top-level imports are captured in the
   deploy-time memory snapshot; a lazy import inside a request is billed to that request
   and is what triggered the CPU kill.
5. **Upstream saver issues fixed in this repo:** (a) Cloudflare's `ormsgpack` stub cannot
   serialize LangChain messages — replaced by `shims/ormsgpack`; (b)
   `WorkerCloudflareD1Saver` lazy-imports SQLAlchemy and silently swallows D1 errors —
   replaced by `src/memory/checkpointer.py`.

## Decision

**Conditional go.** Proceed with Python + LangGraph on Workers Free, with these guardrails:
- Keep the graph to the minimum super-steps; cap stored state at ~40 messages
  (full history lives in our own `messages` table).
- Track `exceededResources` / `scriptThrewException` counts from analytics in `/status`.
- **Escape hatch:** if CPU-limit errors appear in real use, move to Workers Paid
  ($5/month, 30 s CPU per request) — no code changes needed.
