# Kiln gateway overhead

Measured with `tools/bench_gateway.py` on a shared x86-64 test machine (Linux), Python 3.13.16, aiohttp 3.14.5, Kiln commit `None`.
The full run was repeated 3 times. Each cell shows the median of the repeats, then the lowest and highest repeat in brackets. All times are in milliseconds, measured by the client.

## What the bench does

- A mock engine on 127.0.0.1 streams SSE chat chunks. The first token comes 50 ms after the request body arrives. After that, one token comes every 15 ms. The last chunk carries usage.
- The gateway is a real `kiln gateway` process with a temp config, a temp state file and a temp record directory. The mock engine and the gateway run in their own processes. Neither shares a process with the client.
- Each pair of requests sends the same body direct to the mock and through the gateway, in random order. The gateway adds its own work: it reads and parses the body, computes the prefix digests, rewrites the body, forces `include_usage`, observes the stream and writes one record per request.
- Added time is the through value minus the direct value, taken at the same percentile.
- N=80 measured requests per case (40 for the xlarge case), after 10 warm-up requests that the tables leave out. Latency streams carry 30 tokens. Concurrency streams carry 100 tokens.

| Case | Body size | Messages |
|---|---|---|
| small | 1 KB | 3 |
| large | 597 KB | 399 |
| xlarge | 2049 KB | 1389 |

The xlarge case is above `PREFIX_THREAD_BYTES` (1 MiB), so the gateway hashes it in a worker thread. The other cases hash on the event loop.

## Added time, one request at a time

### small body (1 KB)

| Metric | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Added time to first byte | 1.11 (0.85 to 1.21) | 1.81 (1.38 to 1.85) | 2.36 (1.46 to 2.42) | 2.22 (0.74 to 2.83) |
| Added time to first token | 1.07 (0.91 to 1.31) | 1.74 (1.26 to 1.83) | 2.18 (1.45 to 2.43) | 2.22 (0.93 to 2.41) |
| Added delay between tokens | -0.02 (-0.04 to -0.01) | 0.08 (0.06 to 0.15) | 0.10 (-0.06 to 0.16) | -0.41 (-0.71 to 0.10) |
| Added total request time | 1.28 (0.98 to 1.30) | 1.58 (0.97 to 1.92) | 1.93 (1.26 to 2.18) | 2.04 (0.43 to 2.36) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 51.05 (50.88 to 51.13) | 51.58 (51.53 to 51.81) | 51.93 (51.67 to 52.14) |
| Time to first token, through | 52.12 (51.79 to 52.45) | 53.71 (53.03 to 54.23) | 53.88 (53.07 to 54.34) |
| Total request time, direct | 486.69 (486.40 to 486.83) | 487.47 (487.35 to 487.83) | 488.17 (487.48 to 488.26) |
| Total request time, through | 487.96 (487.38 to 488.13) | 489.40 (488.61 to 490.01) | 489.52 (488.68 to 490.53) |
| Delay between tokens, direct | 15.25 (15.20 to 15.25) | 15.57 (15.53 to 16.45) | 17.03 (15.84 to 19.91) |
| Delay between tokens, through | 15.22 (15.19 to 15.23) | 15.73 (15.63 to 16.39) | 16.32 (15.94 to 19.50) |

Gateway CPU time per request (user plus system, idle load removed): 8.30 (7.38 to 11.50) ms.

### large body (597 KB)

| Metric | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Added time to first byte | 10.92 (7.92 to 12.53) | 13.92 (12.15 to 16.16) | 14.85 (12.96 to 17.37) | 15.68 (13.21 to 17.77) |
| Added time to first token | 10.81 (7.77 to 12.75) | 13.66 (11.90 to 15.96) | 14.70 (12.67 to 17.23) | 15.93 (13.29 to 17.33) |
| Added delay between tokens | -0.02 (-0.03 to -0.02) | 0.08 (0.06 to 0.16) | 0.14 (0.10 to 0.41) | -0.09 (-0.20 to 0.21) |
| Added total request time | 11.13 (8.05 to 12.66) | 14.07 (12.01 to 16.11) | 15.16 (13.32 to 16.39) | 15.09 (13.68 to 15.97) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 51.53 (51.22 to 51.65) | 52.48 (52.28 to 52.82) | 52.58 (52.29 to 52.89) |
| Time to first token, through | 62.34 (58.99 to 64.39) | 67.18 (64.95 to 70.06) | 68.51 (65.58 to 70.22) |
| Total request time, direct | 487.13 (486.79 to 487.26) | 488.26 (487.73 to 489.20) | 488.55 (487.91 to 490.22) |
| Total request time, through | 498.26 (494.84 to 499.91) | 503.42 (501.05 to 505.60) | 503.64 (501.59 to 506.19) |
| Delay between tokens, direct | 15.24 (15.20 to 15.24) | 15.56 (15.53 to 15.91) | 17.36 (16.07 to 18.29) |
| Delay between tokens, through | 15.21 (15.18 to 15.22) | 15.67 (15.67 to 16.32) | 17.16 (15.98 to 18.50) |

Gateway CPU time per request (user plus system, idle load removed): 17.41 (14.75 to 22.50) ms.

### xlarge body (2049 KB)

| Metric | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Added time to first byte | 35.00 (19.16 to 35.25) | 41.66 (26.62 to 42.85) | 40.48 (27.85 to 45.25) | 39.88 (27.83 to 46.56) |
| Added time to first token | 34.90 (19.58 to 35.17) | 41.57 (26.56 to 43.08) | 40.37 (27.67 to 45.53) | 39.76 (27.51 to 46.77) |
| Added delay between tokens | -0.03 (-0.03 to -0.01) | 0.08 (0.06 to 0.12) | 0.14 (-0.07 to 0.18) | 0.21 (-0.27 to 0.28) |
| Added total request time | 34.68 (19.46 to 35.12) | 41.65 (26.29 to 42.89) | 40.86 (27.72 to 44.78) | 40.39 (27.72 to 46.18) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 52.57 (52.52 to 52.76) | 55.91 (54.22 to 56.47) | 56.53 (54.32 to 56.67) |
| Time to first token, through | 87.66 (72.15 to 87.69) | 96.28 (84.14 to 99.74) | 96.29 (84.17 to 101.09) |
| Total request time, direct | 488.22 (488.11 to 488.45) | 491.25 (490.31 to 491.89) | 491.81 (490.48 to 492.02) |
| Total request time, through | 523.13 (507.57 to 523.34) | 532.11 (519.61 to 535.09) | 532.20 (519.74 to 536.66) |
| Delay between tokens, direct | 15.23 (15.17 to 15.25) | 15.53 (15.44 to 15.96) | 15.78 (15.74 to 17.23) |
| Delay between tokens, through | 15.20 (15.16 to 15.22) | 15.67 (15.62 to 15.90) | 16.06 (15.95 to 16.96) |

Gateway CPU time per request (user plus system, idle load removed): 41.73 (28.50 to 44.25) ms.

## 4 concurrent streams

Each round starts 4 streams at the same moment and waits for all of them. Direct rounds and through rounds alternate. A stream carries 100 tokens. The ideal rate is 267 tokens per second.

| Case | Direct, tokens/s | Through, tokens/s | Through / direct | Steady rate through / direct | Added time to first token p50 | p99 | max | Gateway CPU per request |
|---|---|---|---|---|---|---|---|---|
| small | 260.2 (260.2 to 260.3) | 259.8 (259.8 to 259.8) | 0.9984 (0.9983 to 0.9984) | 0.9997 (0.9997 to 0.9997) | 1.49 (1.49 to 1.54) | 2.51 (1.62 to 2.58) | 2.61 (1.50 to 3.00) | 13.15 (12.72 to 14.02) ms |
| large | 260.0 (260.0 to 260.1) | 254.9 (254.6 to 255.7) | 0.9802 (0.9794 to 0.9833) | 1.0001 (1.0001 to 1.0002) | 20.51 (17.07 to 21.94) | 42.78 (37.83 to 42.85) | 45.70 (44.33 to 50.29) | 23.26 (19.46 to 23.91) ms |

Aggregate rate counts all tokens of a round over the wall time of the round, so it includes the first-token wait. Steady rate counts only the time from the first to the last token of each stream.

## Does a large request block the event loop?

One probe stream of 300 tokens runs through the gateway. While it runs, 3 more requests arrive, 0.4 s apart. The extra requests are either large (the large body) or small. The probe is a small request. A blocked event loop shows as a long gap between probe tokens. The expected gap is 15 ms. 20 probes per kind.

| Extra requests | Probe gap p50 | p99 | max | Worst gap per probe, median | Gaps over 30 ms |
|---|---|---|---|---|---|
| small | 15.20 (15.19 to 15.22) | 16.22 (15.73 to 16.35) | 17.77 (16.72 to 18.49) | 16.61 (16.06 to 16.90) | 0 (0 to 0) |
| large | 15.20 (15.19 to 15.22) | 16.35 (16.02 to 16.52) | 24.13 (24.07 to 27.17) | 21.21 (20.32 to 22.12) | 0 (0 to 0) |

## Checks

- Repeat 1: the gateway wrote 582 records for 582 requests that went through it. Idle gateway CPU was 0.00 ms per second. One-minute load average was 5.2 at the start and 6.0 at the end.
- Repeat 2: the gateway wrote 582 records for 582 requests that went through it. Idle gateway CPU was 2.00 ms per second. One-minute load average was 5.9 at the start and 4.7 at the end.
- Repeat 3: the gateway wrote 582 records for 582 requests that went through it. Idle gateway CPU was 0.00 ms per second. One-minute load average was 5.3 at the start and 8.0 at the end.

Every stream arrived whole: the client checks the token count and the final `[DONE]` of each request, and stops the run on a mismatch.

