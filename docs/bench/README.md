# Gateway bench: what the numbers say

Run: `.venv/bin/python tools/bench_gateway.py` (3 repeats, about 60 minutes).
Results: `gateway-overhead.md` (tables) and `gateway-overhead.json` (all values and raw samples).
Every figure below is the median of 3 repeats. The range of the repeats is in the tables.

## Summary

| Body | Added time to first token, p50 | p99 | Added delay between tokens, p50 / p99 |
|---|---|---|---|
| 1 KB (small prompt) | 1.1 ms | 2.6 ms | -0.02 ms / 0.2 ms |
| 597 KB (long agent conversation, 399 messages) | 11.4 ms | 23.5 ms | -0.02 ms / 0.3 ms |
| 2049 KB (above `PREFIX_THREAD_BYTES`) | 37 ms | 67 ms | -0.03 ms / 0.4 ms |

- The gateway adds no measurable delay between tokens. Once a stream runs, the relay is transparent.
- A small prompt costs about 1 ms before the first token.
- A large body costs a real amount: more than 10 ms at p50 and more than 20 ms at p99.
- 4 parallel small streams keep 99.8 % of the direct token rate. Large streams keep 96.7 %, because the 4 bodies queue behind each other before their first token. The steady rate of the streams is unchanged (1.000).
- Gateway CPU per request: 2.3 ms for a 2-token reply, 6.7 to 11 ms for a 30-token reply (small body), 22 ms (597 KB), 51 ms (2 MiB). The run was not quiet (load average 5 to 14 from other jobs), so CPU figures vary between repeats.

## Cost above 2 ms: where it comes from

The gateway reads the whole body before it sends the first byte to the engine, and it does this work on the event loop (`src/kiln/gateway/app.py`):

| Step | Code | 597 KB | 2 MiB |
|---|---|---|---|
| Parse the body | `json.loads(raw)`, `app.py:191` | 0.8 ms | 2.8 ms |
| Prefix digests | `PrefixTracker._digest`, `prefix.py:61` (`json.dumps(sort_keys=True)` and HMAC for each message), called from `_prefix_start`, `app.py:282` | 3.2 ms | 10.9 ms |
| Rewrite and serialize again | `json.dumps(out).encode()`, `app.py:235` | 1.6 ms | 5.8 ms |

I timed these three calls alone, on one thread, on the bench machine. They sum to 5.6 ms for 597 KB.

- The digest step is the largest. The comment at `app.py:37` says about 2.5 ms per MiB. On this machine it takes about 5.5 ms per MiB.
- A 597 KB body is below `PREFIX_THREAD_BYTES` (1 MiB), so the digests run on the event loop.
- The 2 MiB body goes to a worker thread for the digests. This does not help much: `json.dumps` and `hmac` hold the GIL while they work, and `json.loads` and `json.dumps(out)` still run on the loop. The added time is 37 ms, not less.
- The rest of the 11.4 ms is not measured separately. My estimate is the second transfer of the body (client to gateway, then gateway to engine) and the extra loop wake-ups. The gateway stores the whole body first, then forwards it.

## Event loop blocking

The stall probe runs a small 300-token stream through the gateway while 3 more requests arrive.

| Extra requests | Worst gap per probe, median | Largest gap | Gaps over 30 ms |
|---|---|---|---|
| small | 16.8 ms | 24 ms | 0 |
| large (597 KB) | 24.9 ms | 43 ms (34 to 66 over the repeats) | 4 to 5 |

The last column counts gaps over 30 ms in the 20 probes of one repeat.

A large request blocks the loop for about 8 ms at the median. In 4 to 5 of 60 large arrivals per repeat, the probe stream stalled for more than 30 ms. This is the same work as above, so the fix is to move parse, digests and serialization for large bodies off the loop, or to send the original bytes when no field changes. This task does not change the gateway.

## Limits of this measurement

- One machine, loopback only, one mock engine at a fixed cadence. A real engine shares CPU with the gateway.
- The machine had other jobs running (load average 5 to 14). The p99 and max columns of the added time carry noise. The range of the 3 repeats shows it.
- Latency streams have 30 tokens and concurrency streams have 100 tokens, not 300. This keeps one repeat near 20 minutes. Time to first byte and to first token do not depend on stream length.
- "Added" is the difference of two percentiles. It is not the percentile of a paired difference.
- The xlarge case has N=100, not 200.
- Gateway CPU comes from `/proc` with a 10 ms tick, so one repeat of 200 requests resolves about 0.05 ms.

## Update: the gateway fast path

The large-body fix and its before/after bench are in `gateway-fast.md`. The profile that explains the 11 ms is in `gateway-profile.md`.
