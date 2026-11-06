# Gateway fast path: before and after

The fix cuts the added time to first token for a 597 KB body from 10.8 ms to 4.8 ms at p50, and from 14.7 ms to 6.4 ms at p99. A large body no longer stalls other streams: the largest probe gap went from 24.1 ms to 18.6 ms, against a token cadence of 15 ms.

The p50 target of 2 ms was not reached. Forwarding the raw bytes with no parse at all adds about 2.3 ms on this machine (see `gateway-profile.md`).

## Method

- Command, run for each code version, one after the other on the same machine:
  `.venv/bin/python tools/bench_gateway.py --n 80 --xlarge-n 40 --warmup 10 --repeats 3 --stall-iterations 20`
- Before: commit `b1ea5de` (the tree that the earlier bench measured). After: commit `61ee093`.
- N is 80 per case (40 for the 2049 KB case), not 200, so that both versions fit in one hour. Every other setting is the default of the bench.
- Each cell shows the median of the 3 repeats, then the lowest and highest repeat in brackets. All times are in milliseconds.
- Raw results: `gateway-fast-before.json`, `gateway-fast-after.json`, with the full tables in `gateway-fast-before.md` and `gateway-fast-after.md`. `tools/bench_compare.py` makes the table below from the two JSON files.
- The machine was shared with other jobs. The load average is under the table. The after run had a higher load (7 to 10) than the before run (5 to 8).

## Results

| Case | Metric | Before | After |
|---|---|---|---|
| 1 KB | Added time to first token, p50 | 1.07 (0.91 to 1.31) | 1.15 (1.09 to 1.17) |
| 1 KB | Added time to first token, p99 | 2.18 (1.45 to 2.43) | 2.13 (1.83 to 2.45) |
| 1 KB | Added time to first token, max | 2.22 (0.93 to 2.41) | 2.59 (1.87 to 6.08) |
| 1 KB | Added delay between tokens, p99 | 0.10 (-0.06 to 0.16) | 0.21 (0.17 to 0.23) |
| 1 KB | Added total request time, p50 | 1.28 (0.98 to 1.30) | 1.05 (1.00 to 1.10) |
| 1 KB | Gateway CPU per request | 8.3 (7.4 to 11.5) | 10.1 (9.5 to 10.2) |
| 597 KB | Added time to first token, p50 | 10.81 (7.77 to 12.75) | 4.76 (4.40 to 4.84) |
| 597 KB | Added time to first token, p99 | 14.70 (12.67 to 17.23) | 6.40 (6.36 to 27.14) |
| 597 KB | Added time to first token, max | 15.93 (13.29 to 17.33) | 6.65 (6.20 to 27.81) |
| 597 KB | Added delay between tokens, p99 | 0.14 (0.10 to 0.41) | 0.49 (0.36 to 0.84) |
| 597 KB | Added total request time, p50 | 11.13 (8.05 to 12.66) | 4.92 (4.44 to 4.93) |
| 597 KB | Gateway CPU per request | 17.4 (14.8 to 22.5) | 13.9 (12.9 to 21.2) |
| 2049 KB | Added time to first token, p50 | 34.90 (19.58 to 35.17) | 15.86 (13.00 to 27.19) |
| 2049 KB | Added time to first token, p99 | 40.37 (27.67 to 45.53) | 33.68 (20.20 to 173.42) |
| 2049 KB | Added time to first token, max | 39.76 (27.51 to 46.77) | 39.97 (21.19 to 207.29) |
| 2049 KB | Added delay between tokens, p99 | 0.14 (-0.07 to 0.18) | 0.15 (-1.18 to 0.48) |
| 2049 KB | Added total request time, p50 | 34.68 (19.46 to 35.12) | 15.54 (12.42 to 27.10) |
| 2049 KB | Gateway CPU per request | 41.7 (28.5 to 44.2) | 25.8 (24.2 to 52.0) |
| 4 streams, 1 KB | Added time to first token, p50 | 1.49 (1.49 to 1.54) | 1.76 (1.51 to 1.90) |
| 4 streams, 1 KB | Added time to first token, p99 | 2.51 (1.62 to 2.58) | 14.20 (1.76 to 17.74) |
| 4 streams, 1 KB | Through / direct token rate | 0.9984 (0.9983 to 0.9984) | 0.9975 (0.9954 to 0.9985) |
| 4 streams, 597 KB | Added time to first token, p50 | 20.51 (17.07 to 21.94) | 9.18 (7.77 to 11.02) |
| 4 streams, 597 KB | Added time to first token, p99 | 42.78 (37.83 to 42.85) | 51.79 (14.61 to 67.63) |
| 4 streams, 597 KB | Through / direct token rate | 0.9802 (0.9794 to 0.9833) | 0.9887 (0.9882 to 0.9888) |
| Stall probe, small arrivals | Largest probe gap | 17.77 (16.72 to 18.49) | 18.51 (17.79 to 20.48) |
| Stall probe, small arrivals | Worst gap per probe, median | 16.61 (16.06 to 16.90) | 16.87 (16.77 to 17.39) |
| Stall probe, small arrivals | Probe gap p99 | 16.22 (15.73 to 16.35) | 16.36 (16.36 to 16.38) |
| Stall probe, small arrivals | Gaps over 30 ms | 0 (0 to 0) | 0 (0 to 0) |
| Stall probe, 597 KB arrivals | Largest probe gap | 24.13 (24.07 to 27.17) | 18.64 (18.52 to 19.07) |
| Stall probe, 597 KB arrivals | Worst gap per probe, median | 21.21 (20.32 to 22.12) | 17.32 (17.12 to 17.62) |
| Stall probe, 597 KB arrivals | Probe gap p99 | 16.35 (16.02 to 16.52) | 16.45 (16.33 to 16.58) |
| Stall probe, 597 KB arrivals | Gaps over 30 ms | 0 (0 to 0) | 0 (0 to 0) |

before: load average 5.2 to 6.0, 5.9 to 4.7, 5.3 to 8.0; records written / requests 582/582, 582/582, 582/582

after: load average 7.6 to 10.1, 9.6 to 7.5, 7.3 to 4.2; records written / requests 582/582, 582/582, 582/582

## Notes

- Targets. The 597 KB p50 is 4.8 ms (target under 2 ms, not reached). The p99 is 6.4 ms (target under 4 ms, not reached). One after repeat had a p99 of 27 ms; the other two had 6.4 ms. The largest probe gap is 18.6 ms, which is 3.6 ms over the 15 ms cadence (target: no stall over 10 ms, reached). The 1 KB p50 is 1.15 ms against 1.07 ms; the ranges of the repeats overlap.
- 1 KB check, small cases only (`--cases small --conc-cases small`, 3 repeats, load average 1 to 2): added time to first token p50 was 0.90 to 1.13 ms after and 1.09 to 1.54 ms before. 4-stream p99 was 3.3 to 4.1 ms after and 2.6 to 3.4 ms before.
- Open item. In the full run, the 4-stream 1 KB case had a p99 of 14 to 18 ms in 2 of 3 after repeats (p50 unchanged). This case runs after the 2049 KB phase. The before run did not show it, and the small-only check did not show it. The load during those 2 repeats was 7.5 to 10. The cause is not known. Run the full bench again on a quiet machine to tell load from a real effect.
- The 2049 KB case uses a worker thread. Its spread is wide in both versions (one after repeat: p99 173 ms). The p50 went from 34.9 ms to 15.9 ms.
