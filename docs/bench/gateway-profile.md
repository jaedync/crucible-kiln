# Gateway profile: the 597 KB request path

This page shows where the gateway spends its time before the first token on a 597 KB body. It also shows what the fix changed. The tables give milliseconds per request.

## Method

- Sampling profile: `py-spy record --subprocesses --rate 1000 --format raw` on the full bench process tree (`tools/bench_gateway.py --cases large --conc-cases "" --stall-iterations 0 --n 100`). `tools/pyspy_summary.py` and a per-line tree of `Gateway.proxy` split the samples of the gateway process. py-spy stops the process for each sample, so its times are about 30 % high. Use them for proportions.
- Wall-clock split: a temporary copy of `app.py` with `time.perf_counter()` marks between the steps of `Gateway.proxy`, written to a file outside the repo. The marks were not committed. N=60 requests after 10 warm-up requests, one at a time.
- The machine was shared. The one-minute load average was 7 to 12 during these runs. The same steps take 1.5 to 3 times longer here than in a quiet single-thread micro benchmark, because the gateway process competes for cores.

## Before: where the 11 ms went

Wall-clock split, old code (commit `b1ea5de`), median of 60 requests. The bench measured 10.9 ms added time to first token in the same run.

| Step | Code | ms |
|---|---|---|
| Read the body | `request.read()` | 0.32 |
| Parse | `json.loads(raw)` | 1.44 |
| Record fields, eval lock, hold check | `proxy` up to `_prefix_start` | 0.12 |
| Prefix digests and conversation match | `_prefix_start` | 5.49 |
| Rewrite and serialize again | `rewrite_body`, `json.dumps(out).encode()` | 2.00 |
| Request headers, hub event | `_forward` up to the upstream call | 0.06 |
| Send to the engine until its response headers | `session.request(...)` | 1.62 |
| Sum | | 11.04 |

The py-spy profile splits the prefix step (per request, inflated by py-spy):

| Part of `_prefix_start` | ms |
|---|---|
| `json.dumps(sort_keys=True)` of each message (`prefix.py:61`) | 4.21 |
| HMAC of each message (`prefix.py:62`) | 1.97 |
| `PrefixTracker.start`, mostly `_common_prefix` | 0.64 |

### The 6 ms that the micro benchmark did not explain

The earlier estimate summed 5.6 ms for parse, digest and serialize, timed alone on a quiet thread. The wall-clock split accounts for the rest:

- In the running gateway the same three steps took 8.9 ms, not 5.6 ms: about 3.3 ms more. The process shares the CPU with the client, the mock engine and other jobs.
- `PrefixTracker.start` took about 0.6 ms. The bench sends each turn as a branch of the last one, so a client holds 16 conversations that share 399 messages. `_common_prefix` compared each digest of each conversation in a Python loop.
- The second transfer of the body (gateway to engine) and the read took about 1.9 ms. A direct request pays for one transfer only.
- Record fields, file reads for the eval lock and the hold check, and the hub event took about 0.2 ms.

## The changes

| Change | Code | Effect at 597 KB |
|---|---|---|
| Parse once and keep the position of each top-level value and each message in the client's bytes | `gateway/body.py` `parse` | Parse costs 1.3 times `json.loads` (0.78 ms against 0.59 ms alone) |
| Send the client's bytes for every value that the rewrite does not change; serialize only the changed values; send the body byte for byte when nothing changes | `gateway/body.py` `encode` | 2.0 ms of `json.dumps` becomes a 0.02 ms join |
| Look up the digest of a known message by a keyed hash of its bytes; compute `json.dumps(sort_keys=True)` and the HMAC only for new messages | `gateway/prefix.py` `_digest_known` | 2.4 ms becomes 0.3 ms (SipHash) |
| Binary search with list compares in `_common_prefix` | `gateway/prefix.py` | 0.6 ms becomes 0.1 ms |
| Bodies above `PREFIX_THREAD_BYTES` (1 MiB): parse, digests and rewrite run in a worker thread | `gateway/app.py` `_prepare` | The loop stays free for large bodies |

The digest values do not change: the lookup returns the same HMAC that `_digest` gives. The same bytes always parse to the same value, so a lookup cannot give the digest of another value.

## After: where the time goes

Wall-clock split of the new code, median of 60 requests. The bench measured 3.9 ms added time to first token in the same run (load average 7).

| Step | ms |
|---|---|
| Read the body | 0.42 |
| Parse with the layout | 1.26 |
| Record fields, eval lock, hold check | 0.08 |
| Digests, rewrite, encode | 0.93 |
| `PrefixTracker.start` | 0.08 |
| Request headers, hub event | 0.03 |
| Send to the engine until its response headers | 1.03 |
| Sum | 3.85 |

The SipHash lookup came after this split. It cut the digest step by about 0.3 ms in a micro benchmark.

## What limits the rest

- Forwarding the raw bytes with no parse at all (a temporary test build) added 2.3 ms at p50 at load average 10. The gateway reads the whole body before it sends it, so the engine gets it one transfer later. A p50 under 2 ms needs the body to stream to the engine while it arrives. That conflicts with the model check (HTTP 409) and the rewrite, which need the parsed body first.
- The parse is the largest step left. The standard library has no way to skip a JSON value without building it. The gateway could find known messages by their length and keyed hash from the last turn and not parse them. Then the dialect rewrite and the conversation match get no value for those messages, so that is a larger change. The gain would be about 1 ms at this load.
