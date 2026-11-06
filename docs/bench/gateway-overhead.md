# Kiln gateway overhead

Measured with `tools/bench_gateway.py` on a shared x86-64 test machine (Linux), Python 3.13.16, aiohttp 3.14.5, Kiln commit `161b689`.
The full run was repeated 3 times. Each cell shows the median of the repeats, then the lowest and highest repeat in brackets. All times are in milliseconds, measured by the client.

## What the bench does

- A mock engine on 127.0.0.1 streams SSE chat chunks. The first token comes 50 ms after the request body arrives. After that, one token comes every 15 ms. The last chunk carries usage.
- The gateway is a real `kiln gateway` process with a temp config, a temp state file and a temp record directory. The mock engine and the gateway run in their own processes. Neither shares a process with the client.
- Each pair of requests sends the same body direct to the mock and through the gateway, in random order. The gateway adds its own work: it reads and parses the body, computes the prefix digests, rewrites the body, forces `include_usage`, observes the stream and writes one record per request.
- Added time is the through value minus the direct value, taken at the same percentile.
- N=200 measured requests per case (100 for the xlarge case), after 20 warm-up requests that the tables leave out. Latency streams carry 30 tokens. Concurrency streams carry 100 tokens.

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
| Added time to first byte | 1.10 (0.85 to 1.42) | 2.99 (1.49 to 3.32) | 3.85 (1.99 to 5.03) | 3.86 (3.20 to 4.34) |
| Added time to first token | 1.13 (0.88 to 1.42) | 2.52 (1.48 to 3.13) | 2.61 (1.86 to 3.89) | 1.73 (1.39 to 4.16) |
| Added delay between tokens | -0.02 (-0.03 to -0.02) | 0.09 (0.06 to 0.17) | 0.19 (0.16 to 0.53) | 0.64 (-2.26 to 1.40) |
| Added total request time | 1.26 (0.95 to 1.51) | 2.96 (1.21 to 4.08) | 3.09 (1.24 to 3.37) | 2.96 (0.12 to 3.49) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 51.04 (50.86 to 51.32) | 53.41 (51.54 to 55.06) | 54.60 (51.90 to 56.76) |
| Time to first token, through | 52.17 (51.74 to 52.74) | 57.30 (53.40 to 57.68) | 58.15 (53.63 to 58.76) |
| Total request time, direct | 486.71 (486.40 to 486.99) | 490.17 (487.28 to 492.31) | 490.42 (487.37 to 496.71) |
| Total request time, through | 487.97 (487.36 to 488.50) | 493.54 (488.52 to 495.40) | 493.91 (490.33 to 496.83) |
| Delay between tokens, direct | 15.22 (15.20 to 15.24) | 15.85 (15.52 to 17.05) | 22.24 (16.89 to 23.96) |
| Delay between tokens, through | 15.21 (15.18 to 15.21) | 16.01 (15.71 to 17.58) | 19.99 (18.29 to 24.60) |

Gateway CPU time per request (user plus system, idle load removed): 11.40 (7.65 to 13.70) ms.

### large body (597 KB)

| Metric | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Added time to first byte | 11.51 (10.25 to 11.57) | 18.19 (17.46 to 20.20) | 24.31 (20.00 to 31.74) | 23.65 (19.66 to 36.55) |
| Added time to first token | 11.42 (10.28 to 11.57) | 17.55 (17.30 to 19.30) | 23.50 (19.67 to 31.79) | 25.02 (18.98 to 38.00) |
| Added delay between tokens | -0.02 (-0.03 to -0.02) | 0.09 (0.08 to 0.11) | 0.26 (0.03 to 0.43) | 0.05 (-1.29 to 9.40) |
| Added total request time | 11.50 (10.16 to 11.53) | 18.24 (17.50 to 19.19) | 26.99 (20.19 to 31.52) | 24.71 (21.70 to 30.02) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 51.71 (51.68 to 51.74) | 57.70 (54.45 to 59.93) | 60.23 (55.93 to 61.60) |
| Time to first token, through | 63.14 (62.02 to 63.25) | 81.20 (74.11 to 91.72) | 86.61 (74.91 to 98.23) |
| Total request time, direct | 487.38 (487.27 to 487.38) | 492.84 (489.97 to 496.36) | 498.21 (491.69 to 502.94) |
| Total request time, through | 498.78 (497.54 to 498.91) | 519.83 (510.15 to 527.88) | 522.92 (513.39 to 532.96) |
| Delay between tokens, direct | 15.22 (15.20 to 15.22) | 16.14 (15.94 to 16.18) | 22.07 (19.36 to 22.32) |
| Delay between tokens, through | 15.20 (15.18 to 15.20) | 16.20 (16.17 to 16.61) | 21.03 (19.41 to 31.47) |

Gateway CPU time per request (user plus system, idle load removed): 21.85 (21.00 to 22.85) ms.

### xlarge body (2049 KB)

| Metric | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Added time to first byte | 37.49 (29.36 to 55.61) | 56.12 (49.37 to 64.54) | 68.02 (61.37 to 90.60) | 72.64 (63.57 to 222.92) |
| Added time to first token | 37.34 (29.28 to 55.88) | 53.78 (49.23 to 64.46) | 67.38 (61.42 to 91.63) | 72.82 (63.85 to 222.24) |
| Added delay between tokens | -0.03 (-0.06 to -0.02) | 0.11 (0.06 to 0.36) | 0.38 (0.13 to 0.99) | 0.39 (-3.58 to 4.92) |
| Added total request time | 37.02 (29.46 to 55.82) | 54.05 (50.04 to 66.49) | 73.07 (61.28 to 90.89) | 79.56 (62.40 to 220.65) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 53.72 (52.86 to 56.90) | 60.90 (57.95 to 68.99) | 66.69 (58.55 to 99.31) |
| Time to first token, through | 91.06 (82.13 to 112.77) | 136.37 (119.37 to 152.53) | 172.13 (122.39 to 288.93) |
| Total request time, direct | 489.27 (488.40 to 492.64) | 496.92 (494.02 to 504.06) | 502.78 (494.53 to 531.56) |
| Total request time, through | 526.28 (517.85 to 548.47) | 577.13 (555.30 to 587.81) | 611.12 (556.93 to 723.43) |
| Delay between tokens, direct | 15.20 (15.03 to 15.24) | 15.92 (15.73 to 17.28) | 20.78 (18.45 to 21.79) |
| Delay between tokens, through | 15.18 (14.97 to 15.21) | 16.30 (15.85 to 18.27) | 18.84 (17.20 to 26.71) |

Gateway CPU time per request (user plus system, idle load removed): 51.30 (40.50 to 72.10) ms.

## 4 concurrent streams

Each round starts 4 streams at the same moment and waits for all of them. Direct rounds and through rounds alternate. A stream carries 100 tokens. The ideal rate is 267 tokens per second.

| Case | Direct, tokens/s | Through, tokens/s | Through / direct | Steady rate through / direct | Added time to first token p50 | p99 | max | Gateway CPU per request |
|---|---|---|---|---|---|---|---|---|
| small | 260.1 (259.9 to 260.2) | 259.4 (259.1 to 259.9) | 0.9976 (0.9969 to 0.9987) | 0.9996 (0.9994 to 0.9997) | 1.92 (1.27 to 2.03) | 2.47 (0.74 to 9.04) | 3.67 (0.58 to 16.73) | 19.43 (11.51 to 26.42) ms |
| large | 259.6 (259.4 to 259.7) | 251.2 (250.2 to 251.6) | 0.9674 (0.9645 to 0.9688) | 1.0007 (1.0004 to 1.0008) | 30.49 (29.27 to 33.14) | 89.77 (84.54 to 97.28) | 110.58 (94.85 to 128.14) | 35.24 (34.95 to 43.77) ms |

Aggregate rate counts all tokens of a round over the wall time of the round, so it includes the first-token wait. Steady rate counts only the time from the first to the last token of each stream.

## Does a large request block the event loop?

One probe stream of 300 tokens runs through the gateway. While it runs, 3 more requests arrive, 0.4 s apart. The extra requests are either large (the large body) or small. The probe is a small request. A blocked event loop shows as a long gap between probe tokens. The expected gap is 15 ms. 20 probes per kind.

| Extra requests | Probe gap p50 | p99 | max | Worst gap per probe, median | Gaps over 30 ms |
|---|---|---|---|---|---|
| small | 15.19 (15.18 to 15.20) | 16.68 (16.39 to 16.75) | 24.44 (18.69 to 25.39) | 16.82 (16.70 to 16.86) | 0 (0 to 0) |
| large | 15.18 (15.18 to 15.19) | 17.86 (17.17 to 17.97) | 42.53 (34.17 to 65.53) | 24.87 (24.38 to 25.19) | 5 (4 to 5) |

## Checks

- Repeat 1: the gateway wrote 1152 records for 1152 requests that went through it. Idle gateway CPU was 0.00 ms per second. One-minute load average was 5.7 at the start and 13.0 at the end.
- Repeat 2: the gateway wrote 1152 records for 1152 requests that went through it. Idle gateway CPU was 0.00 ms per second. One-minute load average was 14.5 at the start and 13.0 at the end.
- Repeat 3: the gateway wrote 1152 records for 1152 requests that went through it. Idle gateway CPU was 0.00 ms per second. One-minute load average was 12.2 at the start and 10.5 at the end.

Every stream arrived whole: the client checks the token count and the final `[DONE]` of each request, and stops the run on a mismatch.

