# Per-SM Scheduling Model — Sample Tables

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

Representative slices of the three per-SM scheduling table families. The full per-SM TSVs
(11 dependency-rule sets, 3 latency families, 7 scoreboard configs) live in the repo at
`decoded/ptxas-sched-full/`. See [Latency Model](../../scheduling/latency-model.md) and
[Scoreboards](../../scheduling/scoreboards.md) for the field semantics.

## Dependency rule (40 B) — sm_90 vs sm_90a (first 12 classes + the WGMMA split)

`rule_type 4` = disabled/unit-absent (always paired with `latency=255`). The six classes
**41, 561, 562, 563, 566, 567** are disabled on `sm_90` but active on `sm_90a` — the WGMMA /
async-MMA tensor classes. This is the entire `sm_90` vs `sm_90a` divergence.

| sm | idx | unit | rule | lat | tput_inv | bar_lat | bar_tput | rd_lat | wr_lat | stall | slots |
|---|---|---|---|---|---|---|---|---|---|---|---|
| sm_90 | 0 | 2 | 1 | 17 | 0 | 56 | 8 | -1 | -1 | 0 | 1 |
| sm_90 | 1 | 3 | 1 | 17 | 0 | 56 | 8 | -1 | -1 | 33 | 2 |
| sm_90 | 2 | 4 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 1 | 1 |
| sm_90 | 3 | 5 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 1 | 1 |
| sm_90 | 4 | 6 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 1 | 1 |
| sm_90 | 5 | 7 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 2 | 3 |
| sm_90 | 6 | 8 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 2 | 3 |
| sm_90 | 7 | 9 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 1 | 1 |
| sm_90 | 8 | 10 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 4 | 1 |
| sm_90 | 9 | 11 | 1 | 17 | 0 | 56 | 8 | -1 | 6 | 1 | 1 |
| sm_90 | 10 | 12 | 1 | 17 | 0 | 56 | 8 | -1 | -1 | 1 | 1 |
| sm_90 | 11 | 15 | 0 | 22 | 2 | 56 | 2 | -1 | -1 | 39 | 4 |
| sm_90 | 33 | 41 | 4 | 255 | 35 | 56 | -1 | -1 | -1 | 39 | 4 |
| sm_90 | 171 | 561 | 4 | 255 | 35 | 56 | -1 | -1 | -1 | 39 | 4 |
| sm_90 | 172 | 562 | 4 | 255 | 35 | 56 | -1 | -1 | -1 | 39 | 4 |
| sm_90 | 173 | 563 | 4 | 255 | 35 | 56 | -1 | -1 | -1 | 39 | 4 |
| sm_90 | 174 | 566 | 4 | 255 | 35 | 56 | -1 | -1 | -1 | 39 | 4 |
| sm_90 | 175 | 567 | 4 | 255 | 35 | 56 | -1 | -1 | -1 | 39 | 4 |
| sm_90a | 0 | 2 | 1 | 17 | 0 | 56 | 8 | -1 | -1 | 0 | 1 |
| sm_90a | 1 | 3 | 1 | 17 | 0 | 56 | 8 | -1 | -1 | 33 | 2 |
| sm_90a | 2 | 4 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 1 | 1 |
| sm_90a | 3 | 5 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 1 | 1 |
| sm_90a | 4 | 6 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 1 | 1 |
| sm_90a | 5 | 7 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 2 | 3 |
| sm_90a | 6 | 8 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 2 | 3 |
| sm_90a | 7 | 9 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 1 | 1 |
| sm_90a | 8 | 10 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 4 | 1 |
| sm_90a | 9 | 11 | 1 | 17 | 0 | 56 | 8 | -1 | 6 | 1 | 1 |
| sm_90a | 10 | 12 | 1 | 17 | 0 | 56 | 8 | -1 | -1 | 1 | 1 |
| sm_90a | 11 | 15 | 0 | 22 | 2 | 56 | 2 | -1 | -1 | 39 | 4 |
| sm_90a | 33 | 41 | 1 | 42 | 15 | 56 | 4 | -1 | -1 | 33 | 2 |
| sm_90a | 171 | 561 | 0 | 15 | 19 | 17 | 8 | -1 | -1 | 39 | 4 |
| sm_90a | 172 | 562 | 0 | 15 | 19 | 17 | 8 | -1 | -1 | 39 | 4 |
| sm_90a | 173 | 563 | 0 | 14 | 19 | 18 | 16 | -1 | -1 | 39 | 4 |
| sm_90a | 174 | 566 | 0 | 15 | 19 | 17 | 8 | -1 | -1 | 39 | 4 |
| sm_90a | 175 | 567 | 0 | 14 | 19 | 18 | 16 | -1 | -1 | 39 | 4 |

## Latency / sched-class descriptor (72 B) — sm_8x family (first 16 classes)

`p7_self` equals `class_id` in every record (self-reference). `p1_tput` = throughput class,
`p5_maxstall` = max-stall cycles, `p11` always 0. `pipeA`/`pipeB` are per-pipe eligibility
byte vectors (`0xFF` byte = pipe N/A).

| idx | class_id | pipeA_hex | pipeB_hex | p0_flags | p1_tput | p2 | p3 | p4 | p5_maxstall | p6 | p7_self | p8 | p9 | p10 | p11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 2 | 0303ffffffffffff | 0000ffffffff0000 | 0 | 4 | 0 | 1 | 0 | 7 | 3 | 2 | 1 | 2 | 3 | 0 |
| 1 | 3 | 0303ffffffffffff | 0000ffffffff0000 | 0 | 4 | 33 | 2 | 0 | 7 | 3 | 3 | 1 | 2 | 3 | 0 |
| 2 | 4 | 04040202ffffffff | 0000ffffffff0000 | 0 | 4 | 1 | 1 | 3 | 7 | 3 | 4 | 1 | 2 | 3 | 0 |
| 3 | 5 | 04040202ffffffff | 0000ffffffff0000 | 268435456 | 4 | 1 | 1 | 3 | 7 | 3 | 5 | 1 | 2 | 3 | 0 |
| 4 | 6 | 04040202ffffffff | 0000ffffffff0000 | 268435456 | 4 | 1 | 1 | 3 | 7 | 3 | 6 | 1 | 2 | 3 | 0 |
| 5 | 7 | 04040202ffff0101 | 0000ffffffff0000 | 0 | 4 | 2 | 3 | 3 | 7 | 3 | 7 | 1 | 2 | 3 | 0 |
| 6 | 8 | 04040202ffff0101 | 0000ffffffff0000 | 0 | 4 | 2 | 3 | 3 | 7 | 3 | 8 | 1 | 2 | 3 | 0 |
| 7 | 9 | 04040202ffffffff | 0000ffffffff0000 | 0 | 4 | 1 | 1 | 3 | 7 | 3 | 9 | 1 | 2 | 3 | 0 |
| 8 | 10 | 0404ffffffff0101 | 0000ffffffff0000 | 0 | 4 | 4 | 1 | 3 | 7 | 3 | 10 | 1 | 2 | 1 | 0 |
| 9 | 11 | 0404ffffffff0101 | 0000ffffffff0000 | 276824064 | 4 | 1 | 1 | 3 | 7 | 3 | 11 | 1 | 2 | 1 | 0 |
| 10 | 12 | 0404ffffffff0101 | 0000ffffffff0000 | 268439552 | 4 | 1 | 1 | 3 | 7 | 3 | 12 | 1 | 2 | 1 | 0 |
| 11 | 15 | 0000ffffffffffff | 0000ffffffff0000 | 0 | 0 | 39 | 4 | 19 | 3 | 2 | 15 | 0 | 1 | 1 | 0 |
| 12 | 16 | 1212ffffffffffff | 00000303ffff0000 | 0 | 4 | 13 | 1 | 14 | 6 | 1 | 16 | 3 | 1 | 1 | 0 |
| 13 | 17 | 1212ffffffffffff | 00000303ffff0000 | 0 | 4 | 13 | 1 | 14 | 6 | 1 | 17 | 3 | 1 | 1 | 0 |
| 14 | 18 | 1313ffffffffffff | 00000303ffff0000 | 0 | 4 | 13 | 1 | 14 | 6 | 1 | 18 | 3 | 1 | 1 | 0 |
| 15 | 19 | 1111ffffffffffff | 00000303ffff0000 | 0 | 4 | 13 | 1 | 14 | 6 | 1 | 19 | 3 | 1 | 1 | 0 |

## Scoreboard config — sm_100 (≤6 triplets) vs sm_90 (single triplet)

Each config is up to 6 `{sb_id, threshold, mask}` triplets. `mask = -1` = unconditional
wait; small masks (2/4/8/32) = pipe-specific. `threshold 56` dominates. sm_100 (Blackwell)
uses up to 6 scoreboards per config (async dependency-barrier model); sm_90 uses one triplet
with a pipe mask.

### sm_100 (first 5 configs)

| idx | triplet_count | sb_id | threshold | mask |
|---|---|---|---|---|
| 0 | 2 | 0 | 56 | -1 |
| 0 | 2 | 2 | 56 | -1 |
| 1 | 3 | 0 | 56 | -1 |
| 1 | 3 | 2 | 56 | -1 |
| 1 | 3 | 5 | 56 | -1 |
| 2 | 4 | 0 | 56 | -1 |
| 2 | 4 | 2 | 56 | -1 |
| 2 | 4 | 5 | 56 | -1 |
| 2 | 4 | 15 | 56 | -1 |
| 3 | 6 | 0 | 56 | -1 |
| 3 | 6 | 2 | 56 | -1 |
| 3 | 6 | 12 | 56 | -1 |
| 3 | 6 | 15 | 56 | -1 |
| 3 | 6 | 23 | 56 | -1 |
| 3 | 6 | 34 | 56 | -1 |
| 4 | 3 | 0 | 56 | -1 |
| 4 | 3 | 2 | 56 | -1 |
| 4 | 3 | 15 | 56 | -1 |

### sm_90 (first 10 configs)

| idx | triplet_count | sb_id | threshold | mask |
|---|---|---|---|---|
| 0 | 1 | 0 | 56 | 8 |
| 1 | 1 | 2 | 56 | 2 |
| 2 | 1 | 5 | 56 | 4 |
| 3 | 1 | 6 | 56 | 8 |
| 4 | 1 | 12 | 8 | 2 |
| 5 | 1 | 12 | 9 | 1 |
| 6 | 1 | 12 | 12 | 4 |
| 7 | 1 | 12 | 13 | 2 |
| 8 | 1 | 13 | 56 | 8 |
| 9 | 1 | 15 | 56 | 4 |

