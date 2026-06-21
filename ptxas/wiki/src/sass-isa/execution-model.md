# SASS Execution Model — Pipelines, Scoreboards, Scheduling

> *Recovered from the decoded per-architecture SASS instruction tables in
> nvdisasm V13.1.115 (CUDA 13.1). The compiler-side hazard resolution (RAW/WAR/WAW)
> is the [Dependency & Hazard Model](../scheduling/dependency-model.md); this page is
> the hardware's dynamic execution and warp-issue model.*

The single most striking result of decoding the tables is that **the warp-scheduler
ISA contract has not changed since Volta.** The control-word bit layout, the six
scoreboards, the 6-bit wait mask, the `usched_info` (0–27) and `batch_t` enums are
**byte-identical from Turing (SM75) through Blackwell (SM121)** — every table even
self-identifies as `ARCHITECTURE "Volta"`. Six GPU generations have added pipes and
datapaths but left the scheduling model untouched. It is a **single-issue,
scoreboard-assisted, statically stall-scheduled** machine: the compiler pre-computes
every fixed stall and every variable-latency barrier; the hardware just evaluates the
wait mask, honours the stall counter, and picks among ready warps.

## The SM as a physical machine

The scheduling model runs on a fixed physical substrate, stable across the
Volta→Hopper generations:

| Resource | Value |
|---|---|
| Register file | **65,536 × 32-bit registers per SM** (256 KB), split **16,384 per sub-partition** |
| Register banks | **2 banks** (even/odd by register number) on Volta–Ampere; 4 banks on Kepler–Pascal |
| Sub-partitions (warp schedulers) | **4 per SM** |
| Warps per SM | **64** (Volta, sm_80) · **48** (sm_86) · **32** (Turing) |
| FP32 FMA lanes | **64 per SM** (4 sub-partitions × 16 lanes) |
| SMs per TPC | 2 |
| Constant bank size | **64 KB** per bank |
| Dependency scoreboards | **6 per warp** (SB0–SB5) |
| Max registers / thread | **255** (`RZ` = R255; allocation granularity 8) |

A warp is bound to one of the 4 sub-partitions, which owns its register-file slice,
its dispatch unit, and the 6 scoreboards; the single-issue rule is *per
sub-partition*. The **2-bank** (even/odd) register file is exactly why the operand
collector and the `.reuse` cache matter — two source operands drawn from the same
bank in one cycle would conflict, so the allocator biases an accumulator into the
opposite bank from its multiplicands and the reuse cache serves a repeated operand
without a second read. (Banking and the allocator's bank bias are in
[Register Allocation](../regalloc/overview.md).)

## Coupled vs decoupled — the `INSTRUCTION_TYPE` split

Each instruction's `INSTRUCTION_TYPE` classifies how its latency is tracked. The enum
grows from 9 codes (Turing–Hopper) to 12 (Blackwell adds scoreboard-elided emulatable variants):

| Code | Name | Hazard mechanism |
|---:|---|---|
| 0 | `COUPLED_MATH` | **fixed** latency → static **stall count** in `opex`; no scoreboard |
| 1 | `DECOUPLED_RD_SCBD` | variable → **read** scoreboard only |
| 2 | `DECOUPLED_RD_WR_SCBD` | variable → **read + write** scoreboards (the load pattern) |
| 3 | `COUPLED_EMULATABLE` | fixed, software-emulated multi-op sequence |
| 4 | `DECOUPLED_BRU_DEPBAR_RD_SCBD` | branch-unit / `DEPBAR` decoupled, read scoreboard |
| 5 | `DECOUPLED_WR_SCBD` | variable → **write** scoreboard only |
| 6 | `DECOUPLED_RD_NOREQ_SCBD` | read scoreboard, no wait-mask requirement |
| 7 | `DECOUPLED_WR_NOREQ_SCBD` | write scoreboard, no wait-mask |
| 8 | `DECOUPLED_BRU_DEPBAR_RD_NOREQ_SCBD` | branch decoupled, no wait-mask |
| 9–11 | `COUPLED_EMULATABLE_{NORD,NOWR,NORD_NOWR}_SCBD` | **SM100+ only** — emulated, scoreboard-elided (used by `cctl_c_ldc_*`) |

**Distribution by instruction form** (PROPERTIES blocks; ~55–60 % `COUPLED_MATH`, ~40–45 % `DECOUPLED` on every arch):

| type | SM75 | SM80 | SM90 | SM100 | SM120 |
|---|--:|--:|--:|--:|--:|
| `COUPLED_MATH` | 575 | 665 | 820 | 673 | 760 |
| `DECOUPLED_RD_WR_SCBD` | 380 | 333 | 461 | 344 | 380 |
| `DECOUPLED_RD_SCBD` | 115 | 94 | 145 | 220 | 163 |
| `DECOUPLED_BRU_DEPBAR_RD_SCBD` | 71 | 80 | 113 | 92 | 92 |
| `COUPLED_EMULATABLE` | 83 | 44 | 44 | 27 | **0** |
| `DECOUPLED_WR_SCBD` | 1 | 1 | 4 | 20 | 14 |
| emulatable-scbd-elided (9/10/11) | 0 | 0 | 0 | 4 | 0 |
| **total forms** | 1229 | 1217 | 1589 | 1380 | 1414 |

Verified correlation: `COUPLED_MATH` forms carry **no pipe** (~99 %) — they are
fixed-latency ALU ops dispatched into the back-to-back math pipe — while **every
`DECOUPLED` form has a `VIRTUAL_QUEUE` assignment** (variable latency must route to a
named functional-unit queue).

## Pipe topology — the `VIRTUAL_QUEUE` functional units

`VIRTUAL_QUEUE` names the FU pipe an instruction dispatches into (39 codes). The pipe
*set* is where generations diverge (distinct pipes: SM75=17, Ampere=20, SM90=26,
Blackwell-DC=34, consumer Blackwell=27). Population by form:

| pipe | meaning | SM75 | SM80 | SM90 | SM100 | SM120 |
|---|---|--:|--:|--:|--:|--:|
| `AGU` | address-gen (smem/local, `ALD`/`AST`, `ISBERD`) | 124 | 109 | 109 | 112 | 108 |
| `AGU_UNORDERED_WR` | global load/atomic, unordered writeback | 75 | 48 | 46 | 49 | 53 |
| `MUFU` | transcendentals, `F2F`/`F2I`/`I2F`, `POPC` | 74 | 74 | 119 | 73 | 73 |
| `FMA64` | 64-bit FMA path, wide convert | 66 | 66 | 111 | 67 | 67 |
| `UNORDERED` | `BAR`/`CCTL`/`LDC`/`BMOV` | 48 | 45 | 88 | 94 | 92 |
| `CBU` | convergence/branch (`BRA`/`BSSY`/`BSYNC`/`CALL`/`EXIT`) | 53 | 64 | 71 | 68 | 68 |
| `TEX` | texture/surface (`TEX`/`SULD`/`SUQUERY`) | 61 | 53 | 57 | 24 | 36 |
| `REDIRECTABLE` | FP64 **emulation** queue (`DADD`/`DFMA`/`DMUL`) | 29 | 36 | 36 | 20 | **0** |
| `REDIRECTABLE_FP16` | **Turing-only** FP16 pipe | 42 | 0 | 0 | 0 | 0 |
| `FP64` | **SM120-only** *native* double precision | 0 | 0 | 0 | 0 | **20** |
| `UMMA` | **Hopper-only** warpgroup MMA (`*GMMA`) | 0 | 0 | 24 | 0 | 0 |
| `TC_1CTA` / `TC_2CTA` | **Blackwell-DC** tcgen05 tensor cores | 0 | 0 | 0 | 30/30 | 0 |
| `TMEM` | **Blackwell-DC** tensor memory (`LDTM`/`STTM`) | 0 | 0 | 0 | 4 | 0 |
| `HMMA`/`IMMA`/`DMMA` | legacy half/int/double tensor | 8/3/0 | 4/3/1 | 4/3/1 | 4/2/1 | 0/0/1 |
| `TMA_UNORDERED_WR` | **Hopper+** TMA bulk async copy | 0 | 0 | 21 | 40 | 40 |
| `SYNCS_UNORDERED_WR` | **Hopper+** async smem barriers (mbarrier) | 0 | 0 | 9 | 10 | 10 |
| `ATOM_UNORDERED`/`SUATOM` | global/surface atomics | 0/16 | 0/8 | 22/8 | 22/4 | 22/6 |
| `FENCE_S`/`FENCE_G`/`FENCE_T` | memory fences | — | — | 1/1/0 | 1/1/1 | 1/1/0 |
| `IPA` | attribute interpolation (graphics) | 7 | 7 | 7 | 6 | 6 |

Separate queues mean a long-latency texture or tensor op does not block an
independent FMA — the scheduler tracks readiness per queue. The **emulation→native
migration** is visible directly: Turing's dedicated `REDIRECTABLE_FP16` folds away by
SM80; FP64 stays on the emulated `REDIRECTABLE` queue until **SM120 gains a native
`FP64` pipe**; tensor goes emulated `HMMA`/`IMMA` → Hopper `UMMA` → Blackwell-DC
`TC_1CTA`/`TC_2CTA` + `TMEM`. **`COUPLED_EMULATABLE` reaches zero on SM120/121** —
consumer Blackwell implements FP64 and its tensor variants natively.

## The scheduling-control word

Bits 102–125 of every instruction carry the schedule ptxas computed (only bits 127–126 are reserved; layout identical on all arches):

| Field | Bits | W | Role |
|---|---|--:|---|
| `pm_pred` | 102–103 | 2 | perf-monitor predicate (`PMN`/`PM1`/`PM2`/`PM3`) — SM90+ |
| `opex` | 105–109 + 122–125 | 9 | stall + yield + dual-issue batch + `.reuse` (below) |
| `dst_wr_sb` | 110–112 | 3 | write scoreboard the producer **sets** on writeback (`7` = none) |
| `src_rel_sb` | 113–115 | 3 | read scoreboard released when sources are consumed (`7` = none) |
| `req_bit_set` | 116–121 | 6 | **wait mask** — one bit per scoreboard SB0–SB5 |

### `opex = batch_t<<5 | usched_info`

Verified across 4500 table rows, **0 mismatches**: the 8-bit `opex` byte is the
high-3-bit `batch_t` (122–124) over the low-5-bit `usched_info` (105–109).

**`usched_info` (0–27) — the unified stall/yield field:**

| value | meaning |
|---|---|
| 0 (`DRAIN`, default) | **yield** — drain the warp from the issue deck; scheduler prefers another ready warp |
| 1–15 (`W1EG…W15EG`) | stall *n* cycles **and end** the co-issue group |
| 17–27 (`W1…W11`) | stall *n*−16 cycles (pipeline transition), no group-end |

This is the Volta-family successor to the Maxwell/Pascal "stall count + yield bit"
control word, packed into one field.

**`batch_t` — the dual-issue/batch control:** `NOP`(0) standalone · `BATCH_START`(1) ·
`BATCH_START_TILE`(2) · `BATCH_END`(4) · `BARRIER_EXEMPT`(5).

### Dual-issue is in the instruction, not the scheduler

**Every arch reports `ISSUE_SLOTS 1`** — one math issue per warp per cycle.
Superscalar co-issue is expressed entirely through `batch_t`: `BATCH_START … BATCH_END`
mark instructions that issue together into distinct pipes in one scheduler decision.
The `TABLES_opex_N(batch_t, usched_info)` legality tables encode the **co-issue rule**:
`TABLES_opex_0` permits a stall (usched 17–27) in any batch position (~40 % of forms),
but **all other tables force a multi-cycle stall to `batch_t=0`** — an instruction
inside a co-issue batch cannot also carry a stall. You may batch *or* stall, not both.
The **compiler**, not the hardware, decides co-issue groups.

### Operand-reuse cache

A separate 64-bit "OE" operand-encoding view exposes per-source **reuse-cache** flags
`OEReuseA`/`OEReuseB`/`OEReuseC` (bits 46/45/44). When set, a source operand is taken
from a small reuse cache instead of re-reading the register file — a power/bandwidth
optimization the compiler controls per source slot. Reuse is mutually exclusive with
`?DRAIN`/`?WAITn_END_GROUP` scheduler tokens (a `CONDITIONS` rule enforces it).

## Scoreboard / barrier model

**Exactly six dependency scoreboards, SB0–SB5, on every arch SM75→SM121** (values 6/7
are the "no barrier" sentinel in the 3-bit producer fields). Resolution:

1. A **variable-latency producer** (`DECOUPLED_*_SCBD`) is assigned `dst_wr_sb` (set
   when its result is written back) and/or `src_rel_sb` (set when its sources have been
   read). The format syntax `WR:wr = dst_wr_sb`, `RD:rd = src_rel_sb` makes this
   explicit; a load (`ldg`) sets both.
2. The producer issues and the warp continues with independent instructions.
3. A **consumer** sets the matching bit in its 6-bit `req_bit_set` **wait mask**. The
   warp may not issue until every scoreboard in the mask has reached its target count —
   up to **6 outstanding dependencies** waited on at once.

Fixed-latency (`COUPLED_MATH`) producers skip all of this: the RAW latency is a
compile-time constant emitted as a `usched` stall, and `MIN_WAIT_NEEDED` gives the
floor (0 for ~55 % of math, up to 6 for `rpcmov`/`depbar`).

**`DEPBAR.LE sb, cnt, {list}`** is an explicit scoreboard wait-down: block until the
indexed scoreboard's outstanding count drops to ≤ `cnt`. This waits on *partial*
completion of a batch of outstanding ops (e.g. "until ≤2 of 5 loads remain").

**`MEM_SCBD` — the orthogonal memory-ordering axis.** Separate from register
scoreboards, `MEM_SCBD` (`NONE`/`SOURCE_RD`/`SOURCE_WR`/`SINK`/`SOURCE_SINK_WR`/…) +
`MEM_SCBD_TYPE` (`BARRIER_INST`/`MEM_INST`/`BB_ENDING_INST`/`ALL`) sequence the memory
subsystem: `SINK/BARRIER_INST` = membar/TMA drain the memory pipe; `SOURCE_WR/ALL` =
`fence` produces global ordering; `SOURCE_SINK_WR` = async smem barriers. This is the
fence/membar model layered on top of register scoreboarding.

## The per-warp issue rule

Each cycle, for a warp's next decoded instruction *I*:

1. **Wait-mask gate** — if `req_bit_set ≠ 0` and not all its scoreboards have reached target → **WAIT** (a variable-latency RAW/WAR dependency).
2. **Dispatch-stall gate** — if `cycles_since_last_issue < MIN_WAIT_NEEDED(I)` → **STALL**; honour the `usched` stall before the next issue (0 → yield-candidate; 1–15 → stall+end-batch; 17–27 → stall *n*−16).
3. **Yield** — `usched == DRAIN(0)` voluntarily yields the issue deck (greedy-then-oldest), avoiding SM monopolization.
4. **Batch** — `BATCH_START … BATCH_END` co-issue into distinct pipes in one decision (the only "dual issue"; mutually exclusive with a stall on most pipes).
5. **Issue** — into the instruction's `VIRTUAL_QUEUE`. `COUPLED_MATH` → fixed pipe, no scoreboard; `DECOUPLED_*` → FU queue, arm `dst_wr_sb`/`src_rel_sb`, move on. `BRANCH_DELAY == 0` on all arches — no architected branch delay slot; convergence is the `CBU` pipe (`BSSY`/`BSYNC`) + control scoreboards.

## Validation against ptxas-emitted SASS

The model above is reproduced exactly by the schedule ptxas computes. The check
assembles PTX with `ptxas`, reads each instruction's 128-bit encoding with
`nvdisasm -c -hex`, and decodes the control word (bits 105–124) back into
`usched`/`dst_wr_sb`/`src_rel_sb`/`req_bit_set`/`batch_t`. A `saxpy` inner block
on SM90 decodes as:

| Op | `usched`→stall | `dst_wr` | `src_rel` | wait mask | role |
|---|---|---|---|---|---|
| `IMAD.WIDE` (addr of x) | 6 → stall 6 | — | — | SB0 | waits the index `S2R` chain |
| `LDG.E R2` (load x) | 17 → stall 1 | **SB2** | — | — | arms write scoreboard SB2 |
| `IMAD.WIDE` (addr of y) | 5 → stall 5 | — | — | SB1 | independent address math, fills the load shadow |
| `LDG.E R7` (load y) | 18 → stall 2 | **SB2** | — | — | second load **reuses SB2** (count-based) |
| `FFMA R7, R2, a, R7` | 5 → stall 5 | — | — | **SB2** | consumer waits SB2 until **both** loads complete |
| `STG.E` (store) | 17 → stall 1 | — | — | — | |

The two loads share one scoreboard (`SB2`); the consuming `FFMA` carries a single
`req_bit_set` bit for SB2 and blocks until that scoreboard's outstanding count
returns to its target — i.e. until *both* loads have written back. This is the
producer→consumer pairing the model predicts, realized verbatim.

Across `sm_75 sm_80 sm_86 sm_89 sm_90 sm_90a sm_100 sm_120`, over a battery of
math-chain, load, transcendental, tensor and `cp.async` probes (1912 decoded
instructions): **every consumer wait-bit pairs with an earlier producer that
armed exactly that scoreboard — 221 pairs, 0 unmatched — and `opex =
batch_t<<5 | usched_info` holds on every single instruction** (0 violations,
recomputed independently from the raw 128-bit words). A co-issue batch
(`batch_t ≠ 0`) never carries a group-ending stall (`usched` 1–15); the only
stall that rides a batch member is the 1–2-cycle no-group-end spacing of a
`.reuse` co-issue pair — confirming "batch *or* stall, not both".

### Observed dispatch stall by producer class

For fixed-latency math, the stall ptxas places between a producer and its
immediate dependent consumer is small and **stable across every generation**:

| Dependent pair | Mechanism | Stall (SM75→SM120) |
|---|---|---|
| `IADD3`→`IADD3`, `IMAD`→`IMAD`, `FFMA`→`FFMA`, `FADD`→`FADD` | fixed (coupled math) | **4** |
| `IADD3`→`IMAD`, `LOP3`/`FMUL`→consumer | fixed (coupled math) | **5** (SM75 emits 8 for the pre-store slot) |
| `ISETP`→predicated op (CC/predicate) | fixed (control class) | **13** |
| `HMMA.16816`→consumer (tensor) | fixed (tensor class) | **11** + follow-on |
| `LDG`/`LDS`→consumer | variable → scoreboard | `dst_wr_sb` + matching `req_bit_set` |
| `MUFU`, `F2I`/`I2F`/`F2F`, `POPC`→consumer | variable → scoreboard (MUFU pipe) | scoreboard, **not** a fixed stall |
| `cp.async` group wait | variable → `DEPBAR` | `LDGDEPBAR` + `DEPBAR.LE SBn, k` |

The dispatch *stall* is the producer→consumer dependency latency for the coupled
math pipe, which is *narrower* than the 6-cycle result band: a same-pipe pair emits
**4** (the coupled base latency), a cross-pipe pair emits **5** (one extra cycle of
inter-pipe hazard penalty). The 6-cycle band is the higher-level scalar that also
covers the slower CC/predicate-write path — a coupled op writing a condition-code or
predicate exposes `6 + 7 = 13`, the control band, which is why an `ISETP`→predicate
edge stalls 13. The stall always counts the cycles *between the two issue slots*
(the wait the consumer owes after the producer issues), so it is the dependency edge
weight directly, not a band with a constant subtracted. The MUFU pipe
(transcendentals, `F2*`/`I2F` conversions, `POPC`) carries the band-13 *latency* but
delivers it through a **scoreboard**, not a fixed stall — the band gives the
magnitude, the `INSTRUCTION_TYPE` gives the mechanism.

`cp.async` (`cp.async.commit_group` → `LDGDEPBAR`; `cp.async.wait_group k` →
`DEPBAR.LE SBn, k`) is the explicit partial-wait path: the count `k` is the
number of outstanding copy *groups* the warp will tolerate, byte-identical from
SM80 through SM120. WAR (read-then-overwrite) pairs carry **no extra stall** —
ptxas renames the destination so the anti-dependency never reaches the schedule.

### Reference simulator

`decoded/sass-tools/sass_sched_sim.py` is a single-warp issue/timing simulator
that replays exactly this rule over a decoded stream — wait-mask gate, dispatch
stall, yield-on-DRAIN, `batch_t` co-issue, scoreboard arm/wait — and emits a
cycle-by-cycle trace plus a basic-block cycle estimate. Its `--validate` mode is
the producer→barrier and `opex` self-check used above. `sass_ctrl_decode.py` is
the control-word decoder it consumes.

## Per-arch execution deltas

| Axis | SM75 Turing | SM80–89 | SM90 Hopper | SM100/103/110 Bk-DC | SM120/121 Bk-consumer |
|---|---|---|---|---|---|
| ISSUE_SLOTS / BRANCH_DELAY | 1 / 0 | 1 / 0 | 1 / 0 | 1 / 0 | 1 / 0 |
| Scoreboards | 6 | 6 | 6 | 6 | 6 |
| Control word / `usched`/`batch_t` | fixed | identical | identical | identical | identical |
| `INSTRUCTION_TYPE` codes | 0–8 | 0–8 | 0–8 | **0–11** | 0–11 |
| Distinct pipes | 17 | 20 | 26 | 34 | 27 |
| FP16 | dedicated pipe | folded | folded | folded | folded |
| FP64 | emulated | emulated | emulated | emulated | **native pipe** |
| Tensor | `HMMA`/`IMMA` (emul) | +`DMMA` | **+`UMMA` (wgmma)** | **+`TC_1CTA/2CTA`+`TMEM`** | none |
| Async copy / barriers | — | — | **+`TMA`/`SYNCS`** | TMA + TMEM | TMA |
| `COUPLED_EMULATABLE` forms | 83 | 44 | 44 | 27 (+3 elided) | **0** |

## Cross-References

- [SASS Instruction Encoding](instruction-encoding.md) — the control-word bit placement.
- [Dependency & Hazard Model](../scheduling/dependency-model.md) — the ptxas-side RAW/WAR/WAW decision.
- [Scoreboards & Dependency Barriers](../scheduling/scoreboards.md) — ptxas scoreboard allocation.
- [Architecture Evolution](architecture-evolution.md) — the pipe additions per generation.
- Tooling: `decoded/sass-tools/sass_ctrl_decode.py` (control-word decoder) and
  `sass_sched_sim.py` (warp-issue / timing simulator + `--validate` self-check).
