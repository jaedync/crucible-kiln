# Kiln gateway overhead

Measured with `tools/bench_gateway.py` on a shared x86-64 test machine (Linux), Python 3.13.16, aiohttp 3.14.5, Kiln commit `61ee093`.
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
| Added time to first byte | 1.07 (1.03 to 1.09) | 1.64 (1.41 to 2.09) | 1.53 (1.43 to 2.72) | 2.93 (1.77 to 5.47) |
| Added time to first token | 1.15 (1.09 to 1.17) | 1.76 (1.70 to 2.08) | 2.13 (1.83 to 2.45) | 2.59 (1.87 to 6.08) |
| Added delay between tokens | -0.03 (-0.03 to -0.02) | 0.11 (0.10 to 0.12) | 0.21 (0.17 to 0.23) | -0.57 (-2.06 to 1.34) |
| Added total request time | 1.05 (1.00 to 1.10) | 1.70 (1.20 to 2.12) | 2.14 (1.85 to 3.06) | 2.12 (1.96 to 5.95) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 51.02 (51.00 to 51.04) | 51.61 (51.52 to 53.35) | 51.67 (51.63 to 54.24) |
| Time to first token, through | 52.16 (52.10 to 52.19) | 54.05 (53.35 to 55.49) | 54.21 (53.54 to 60.31) |
| Total request time, direct | 486.69 (486.61 to 486.77) | 487.68 (487.31 to 489.17) | 487.96 (487.37 to 489.24) |
| Total request time, through | 487.71 (487.70 to 487.82) | 489.82 (489.16 to 492.23) | 490.08 (489.33 to 495.19) |
| Delay between tokens, direct | 15.23 (15.23 to 15.23) | 15.92 (15.67 to 16.00) | 19.13 (18.56 to 20.34) |
| Delay between tokens, through | 15.20 (15.19 to 15.21) | 16.13 (15.91 to 16.17) | 18.28 (17.99 to 20.47) |

Gateway CPU time per request (user plus system, idle load removed): 10.12 (9.50 to 10.25) ms.

### large body (597 KB)

| Metric | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Added time to first byte | 4.91 (4.38 to 4.94) | 6.77 (6.53 to 23.61) | 6.99 (6.65 to 25.26) | 7.10 (6.90 to 26.90) |
| Added time to first token | 4.76 (4.40 to 4.84) | 6.37 (6.20 to 23.97) | 6.40 (6.36 to 27.14) | 6.65 (6.20 to 27.81) |
| Added delay between tokens | -0.03 (-0.03 to -0.02) | 0.15 (0.15 to 0.50) | 0.49 (0.36 to 0.84) | -0.89 (-1.22 to 2.03) |
| Added total request time | 4.92 (4.44 to 4.93) | 6.50 (6.00 to 23.51) | 6.81 (6.54 to 27.14) | 7.14 (7.00 to 33.02) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 51.51 (51.42 to 51.57) | 52.74 (52.69 to 61.45) | 52.96 (52.74 to 61.50) |
| Time to first token, through | 56.32 (55.82 to 56.36) | 59.09 (59.09 to 88.59) | 59.39 (59.16 to 89.30) |
| Total request time, direct | 487.07 (487.02 to 487.27) | 488.42 (488.24 to 495.46) | 488.63 (488.51 to 495.77) |
| Total request time, through | 492.00 (491.46 to 492.19) | 495.04 (494.97 to 522.60) | 495.77 (495.51 to 528.79) |
| Delay between tokens, direct | 15.23 (15.21 to 15.23) | 15.75 (15.71 to 18.16) | 19.12 (16.98 to 22.66) |
| Delay between tokens, through | 15.20 (15.18 to 15.21) | 16.20 (16.11 to 19.00) | 19.01 (17.90 to 21.77) |

Gateway CPU time per request (user plus system, idle load removed): 13.88 (12.88 to 21.25) ms.

### xlarge body (2049 KB)

| Metric | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Added time to first byte | 16.07 (12.97 to 27.67) | 20.17 (17.95 to 120.16) | 34.64 (20.37 to 169.70) | 41.12 (21.27 to 200.50) |
| Added time to first token | 15.86 (13.00 to 27.19) | 19.59 (17.34 to 121.42) | 33.68 (20.20 to 173.42) | 39.97 (21.19 to 207.29) |
| Added delay between tokens | -0.03 (-0.05 to -0.02) | 0.15 (0.13 to 0.21) | 0.15 (-1.18 to 0.48) | -0.44 (-8.09 to 1.18) |
| Added total request time | 15.54 (12.42 to 27.10) | 20.73 (17.24 to 121.86) | 34.78 (19.88 to 172.08) | 41.48 (20.58 to 202.39) |

| Absolute (direct, then through) | p50 | p99 | max |
|---|---|---|---|
| Time to first token, direct | 53.14 (52.68 to 55.89) | 57.98 (55.82 to 90.87) | 58.46 (55.98 to 91.28) |
| Time to first token, through | 69.01 (65.68 to 83.08) | 91.66 (76.01 to 264.28) | 98.43 (77.17 to 298.57) |
| Total request time, direct | 488.78 (488.51 to 491.82) | 492.70 (491.68 to 530.77) | 492.80 (491.97 to 532.61) |
| Total request time, through | 504.32 (500.93 to 518.92) | 527.49 (511.56 to 702.85) | 534.28 (512.55 to 735.00) |
| Delay between tokens, direct | 15.22 (15.22 to 15.22) | 15.88 (15.74 to 21.31) | 19.73 (17.17 to 32.06) |
| Delay between tokens, through | 15.19 (15.17 to 15.20) | 16.22 (16.03 to 20.13) | 19.29 (18.35 to 23.97) |

Gateway CPU time per request (user plus system, idle load removed): 25.75 (24.25 to 52.00) ms.

## 4 concurrent streams

Each round starts 4 streams at the same moment and waits for all of them. Direct rounds and through rounds alternate. A stream carries 100 tokens. The ideal rate is 267 tokens per second.

| Case | Direct, tokens/s | Through, tokens/s | Through / direct | Steady rate through / direct | Added time to first token p50 | p99 | max | Gateway CPU per request |
|---|---|---|---|---|---|---|---|---|
| small | 259.9 (259.8 to 260.2) | 259.2 (258.6 to 259.8) | 0.9975 (0.9954 to 0.9985) | 0.9998 (0.9997 to 1.0001) | 1.76 (1.51 to 1.90) | 14.20 (1.76 to 17.74) | 16.05 (1.53 to 17.44) | 18.80 (14.13 to 30.98) ms |
| large | 259.6 (259.5 to 259.8) | 256.7 (256.6 to 256.7) | 0.9887 (0.9882 to 0.9888) | 1.0002 (1.0000 to 1.0003) | 9.18 (7.77 to 11.02) | 51.79 (14.61 to 67.63) | 63.09 (17.61 to 77.12) | 27.83 (25.33 to 28.59) ms |

Aggregate rate counts all tokens of a round over the wall time of the round, so it includes the first-token wait. Steady rate counts only the time from the first to the last token of each stream.

## Does a large request block the event loop?

One probe stream of 300 tokens runs through the gateway. While it runs, 3 more requests arrive, 0.4 s apart. The extra requests are either large (the large body) or small. The probe is a small request. A blocked event loop shows as a long gap between probe tokens. The expected gap is 15 ms. 20 probes per kind.

| Extra requests | Probe gap p50 | p99 | max | Worst gap per probe, median | Gaps over 30 ms |
|---|---|---|---|---|---|
| small | 15.19 (15.18 to 15.20) | 16.36 (16.36 to 16.38) | 18.51 (17.79 to 20.48) | 16.87 (16.77 to 17.39) | 0 (0 to 0) |
| large | 15.20 (15.18 to 15.20) | 16.45 (16.33 to 16.58) | 18.64 (18.52 to 19.07) | 17.32 (17.12 to 17.62) | 0 (0 to 0) |

## Checks

- Repeat 1: the gateway wrote 582 records for 582 requests that went through it. Idle gateway CPU was 0.00 ms per second. One-minute load average was 7.6 at the start and 10.1 at the end.
- Repeat 2: the gateway wrote 582 records for 582 requests that went through it. Idle gateway CPU was 0.00 ms per second. One-minute load average was 9.6 at the start and 7.5 at the end.
- Repeat 3: the gateway wrote 582 records for 582 requests that went through it. Idle gateway CPU was 0.00 ms per second. One-minute load average was 7.3 at the start and 4.2 at the end.

Every stream arrived whole: the client checks the token count and the final `[DONE]` of each request, and stops the run on a mismatch.

