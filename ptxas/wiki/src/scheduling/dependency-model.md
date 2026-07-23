# Dependency & Hazard Model

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

Before ptxas can place stall counts and dependency barriers on SASS, it builds a
dependency graph over the instruction stream and asks, for every ordered pair of
instructions that touch a common resource, *"how many cycles must separate them?"*
The answer is governed by a small, fixed taxonomy of **data hazards**. This page
documents that taxonomy, how the scheduler tracks each hazard per operand, and
which binary tables supply the cycle counts. It is the model that the latency
tables ([Latency Model](latency-model.md)) and the scoreboard machinery
([Scoreboards](scoreboards.md)) plug into.

## The four dependency kinds

For a producer instruction **P** and a later consumer **C** that share a resource,
the hazard is classified by *which side reads and which side writes*:

| Kind | Hazard | P does | C does | Meaning |
|---|---|---|---|---|
| **True** | RAW (read-after-write) | writes reg | reads reg | C must wait for P's *result* to exist |
| **Anti** | WAR (write-after-read) | reads reg | writes reg | P must have *collected its operand* before C overwrites it |
| **Output** | WAW (write-after-write) | writes reg | writes reg | the two writes must retire in program order |
| **Control** | flow ordering | sets flow state | depends on flow | branch / condition-code / branch-stack ordering |

The scheduler's dependency classification recognizes exactly these four kinds and
no others. The first three are *data* hazards on register-like resources
(general registers, predicates); the fourth covers the condition-code, the
branch divergence stack, and control-flow resources.

### True (RAW) — the result-latency table

RAW is the dominant hazard: a consumer reading a register must wait the full
**result latency** of the producer. ptxas stores these as the **scalar latency
bands** built by `sub_738E20` — a small set of distinct values
`{6, 13, 24, 30, 300}` keyed by the producer's pipeline class:

- band **6** — the fixed-latency math pipes (integer / FXU, the lighter FMA path)
- band **13** — a *mixed* class: tensor / fp64 ops (DMMA, GMMA, HFMA2_MMA) **and**
  control / sync ops (BRA, BSYNC, BAR_INDEXED, RTT). The numeric Ori opcode id is
  authoritative here; the oracle's best-effort mnemonics are not (they include
  duplicates and boundary markers)
- bands **24 / 30** — slower fixed-latency ops
- band **300** — the "very long" sentinel for ops whose latency is not a compile-time
  constant (see *Variable latency* below)

These are producer→consumer cycle counts: an FXU result is readable 6 cycles after
issue, an HMMA result after 13. A consumer scheduled sooner gets an explicit stall.

### Anti (WAR) — and why it is *not* the transpose of RAW

WAR asks the opposite question: how long must a *writer* wait so that an earlier
*reader* has already latched the old value? This is only the operand-collection
window — a couple of cycles — **not** the result latency. That is the crucial
asymmetry: RAW is "wait for the value to be produced" (6–13+ cycles); WAR is "wait
for the old value to be consumed" (≈1–2 cycles). They are different physical events
with different magnitudes, so **the anti table is never the transpose of the true
table.** The asymmetry is structural, not incidental: the anti latency is carried by
its own per-resource default (a small fixed value — ~1 cycle for fault-class writers,
zero-but-ordered otherwise), entirely independent of the producer's result latency.
The true (RAW) table is consulted only to decide whether a given WAR edge *collapses*
to zero-but-ordered or to no-latency — never to copy the RAW magnitude across. Treating
the anti table as a transposed RAW matrix is the classic mistake when reconstructing
these tables.

On register resources the WAR (and WAW) default is **zero-but-ordered**: a packed
sentinel meaning *0 cycles, ordering preserved*. In-order issue plus the register
file already guarantee a reader sees the old value before a same-or-later-issued
writer overwrites it, so most WAR pairs cost nothing — the table only carries the
**exceptions** (e.g. a long-latency reader followed by a fast writer). A sparse
anti/output table is therefore correct, not incomplete.

### Output (WAW) — write ordering

Two instructions writing the same register must finish in program order, or the
register would end up holding the *earlier* (late-completing) value. The output
table gives the minimum separation that guarantees correct final state. It is
mostly zero-but-ordered, with nonzero entries exactly where a long-latency producer is
followed by a short-latency producer to the same destination (the short one would
otherwise retire first).

### Control

The condition-code, the branch-divergence stack, and control-flow resources carry
a hard 1-cycle default ordering for their output/anti edges, plus explicit
true-edges for branch operations. These keep `BRA`/`BSSY`/`BMOV`/`EXIT`-class flow
mutations correctly serialized independently of any register dependency.

## Per-operand tracking and the register range

Hazards are tracked **per operand slot**, not per instruction: a dependency exists
only between the producer's *destination* write port and the consumer's *source*
read port that name the same register. The scheduler distinguishes the source read
ports (the `Ra`/`Rb`/`Rc`-class operands) from the destination write ports (`Rd`,
and `Rd2` for a second/wide destination).

Wide operands extend the dependency across a **register range**: an operand whose
declared size is *N* bits spans

```text
range = ((max(N, 1) - 1) >> 5) + 1      // number of 32-bit registers
```

consecutive registers (a 64-bit `Rd2` covers 2, a 128-bit load covers 4). A
dependency on any register in a producer's destination range against any register
in a consumer's source range raises the hazard. This is why a single `(producer,
consumer)` latency cell must be applied across the overlapping register span, not
just to the base register — the answer to *"how do the rows and columns combine"*:
pick the kind from `(P-writes, C-reads ⇒ RAW)` / `(P-reads, C-writes ⇒ WAR)` /
`(P-writes, C-writes ⇒ WAW)`, look up the `(P-class row, C-class column)` cell, and
apply it across the register-range overlap.

## Fixed vs variable latency — the scoreboard path

Fixed-latency ops (math) use the scalar bands above: the latency is known at
compile time and emitted as a stall count. **Variable-latency ops** — global/shared
loads, texture, MIO, anything whose completion time the compiler cannot know —
cannot use a fixed RAW number. Instead the producer **sets a scoreboard** and the
consumer **waits on it** via a dependency barrier (`DEPBAR`/the `.wait` annotation).
RAW on the scoreboard resource is therefore *zero-but-ordered with a barrier*, not a
cycle count. This is the `barrier_latency` / `barrier_throughput` pair in the
per-class dependency rule, and the per-SM scoreboard configuration described in
[Scoreboards & Dependency Barriers](scoreboards.md).

## How the binary tables realize the model

The model is not stored as four big 2-D matrices in ptxas; it is **compiled into a
per-scheduling-class reduced form** plus the scalar bands. Three `.rodata` table
families carry the numbers (full layouts in [Latency Model](latency-model.md)):

| Model concept | Binary carrier |
|---|---|
| RAW result latency | scalar latency bands (`sub_738E20`); `latency` / `read_latency` in the 40-B rule |
| WAW write ordering | `write_latency` in the 40-B dependency rule (`-1` ⇒ use `latency`) |
| WAR (anti) | zero-but-ordered default; nonzero exceptions in the per-class stall fields |
| Variable-latency / scoreboard RAW | `barrier_latency` / `barrier_throughput` (40-B rule) + 88-B scoreboard config |
| Throughput / occupancy | `throughput_inv` (issue interval) in the 40-B rule; the pipe-eligibility vectors in the 72-B sched-class descriptor |
| Unit absent on this arch | `rule_type == 4` paired with `latency == 255` |

The 40-byte dependency rule is indexed by scheduling-class id and is **per-SM**; the
72-byte sched-class descriptor (pipe masks, throughput class, max-stall) is shared
per SM *family*; the 88-byte scoreboard config is per-SM. Together they are the
shipped projection of the RAW/WAR/WAW/control model onto each target.

## Cross-validation across SMs (ptxas v13.0.88)

The flattened tables were re-derived byte-exact and confirm the four-kind model on
every shipped target. The dense producer×consumer matrices ship in **no** CUDA 13
binary — they are a build-time artifact; ptxas carries the per-class projection
below, and the per-arch SASS instruction tables carry only the grammar/encoding
(no latency matrices).

| Model concept | Binary carrier | sm_70 | sm_90 / sm_90a | sm_100 / sm_103 |
|---|---|---|---|---|
| RAW, fast path | scalar band `sub_738E20` → `oracle[744+4·op]`, read by `sub_8BF3A0` | bands {6,13,24,30,300} | same | same |
| RAW, per-class | rule `latency` + `read_latency` (−1 ⇒ use `latency`) | overrides on 190 classes | 22 | 150 / 14 |
| WAW | rule `write_latency` (−1 ⇒ use `latency`) | 34 classes, {4,5,6,8,11}, never 0 | 15 | 27 / 30 |
| WAR | *no field* — zero-but-ordered | implicit | implicit | implicit |
| Control | band-13 on `BRA`/`BSYNC`/`BAR`/`RTT` + pred/CC in scoreboard | 13 cyc | same | same |
| Scoreboard config (rows/config; `mask` = pipe bitset) | 88-B cfg via selector `sub_ABF590`, `count` @ `+0x54` | 1 | 1 | sm_100 ≤6 / sm_103 1 |
| Decoupled barrier | rule `barrier_latency` / `barrier_throughput` (−1 ⇒ none) | 473 active | 250 / 256 | 420 / 301 |
| Long-latency gate | `sub_8CCF80` ⇒ `latency > 19` | 19 | same | same |

- **`rule_type == 4 ⇔ latency == 255`** (unit absent on this arch) — zero violations across all 11 per-SM tables.
- **sm_90 vs sm_90a** differ in exactly 6 rows: units `{41, 561, 562, 563, 566, 567}` (WGMMA / async-MMA) disabled on sm_90, active on sm_90a — the sole difference (sm_86 is byte-identical to sm_90).
- **Scoreboard configs.** A config groups *N* `{scoreboard_id, threshold, mask}` rows, where `mask` is a **bitset over pipes/scoreboards** (`-1` = all). The grouped-row count is **not** a scoreboard budget. The per-target table is selected by `sub_ABF590` (stores the table pointer at `cfg+0x60`, count at `cfg+0x68`): sm_100 → table `0x2266A60`, 75 configs, up to 6 rows each, masks to bit 6; sm_103 → table `0x2261740`, 46 configs, all single-row, but masks remapped to a **wider** Blackwell-Ultra pipe space (up to bit 10, popcount ≤3). sm_103 is the same Blackwell scoreboard schema as sm_100 restricted to its single-row configs — *not* a smaller scoreboard model. Pre-Blackwell SMs use a single config.

## Per-instruction scheduling class & the SASS control word (CUDA 13.1)

The newest decode point — the per-arch SASS instruction tables from nvdisasm
V13.1.115 — names the model *per instruction*. Each instruction form carries an
`INSTRUCTION_TYPE` that is exactly the fixed-vs-variable split above, plus a
`MIN_WAIT_NEEDED` and the control-word field layout. For sm_100 (1380 forms):

| `INSTRUCTION_TYPE` | count | hazard handling |
|---|---:|---|
| `COUPLED_MATH` | 673 | fixed-latency — RAW via stall count (the scalar bands) |
| `DECOUPLED_RD_WR_SCBD` | 344 | variable — sets a write scoreboard, releases a read scoreboard |
| `DECOUPLED_RD_SCBD` | 220 | variable — read-side scoreboard only |
| `DECOUPLED_BRU_DEPBAR_RD_SCBD` | 92 | branch-unit / `DEPBAR` decoupled |
| `COUPLED_EMULATABLE` (+2 variants) | 31 | fixed-latency, emulatable |
| `DECOUPLED_WR_SCBD` | 20 | variable — write-side scoreboard only |

`COUPLED_*` ⇒ the producer's RAW latency is a compile-time constant emitted as a
**stall count**; `DECOUPLED_*_SCBD` ⇒ variable latency handled by **scoreboards** —
the exact distinction the model draws between fixed and variable RAW.
`MIN_WAIT_NEEDED` is the minimum stall the scheduler must insert (sm_100: 0 for 688
forms, 1 for 571, then 4/5/6/8/11), and `MEM_SCBD` / `MEM_SCBD_TYPE` give the
scoreboard role (SOURCE / SINK) and kind (`BARRIER_INST` vs `MEM_INST`).

The resolved hazard is then **encoded into each SASS instruction's control word**
(bits 105–124), whose fields the same tables define:

| Field | Bits | Role |
|---|---|---|
| `opex` (stall / yield / batch) | 105–109, 122–124 | fixed stall count + yield, via `TABLES_opex_1(batch_t, usched_info)` |
| `dst_wr_sb` | 110–112 | write-barrier scoreboard the producer **sets** (`7` = none) |
| `src_rel_sb` | 113–115 | read-barrier scoreboard the producer **releases** (`7` = none) |
| `req_bit_set` | 116–121 | 6-bit **wait mask** — which barriers the consumer waits on before issue |

So a RAW on a fixed-latency producer becomes a `stall` in `opex`; a RAW on a
variable-latency producer becomes a `dst_wr_sb`/`src_rel_sb` on the producer and a
`req_bit_set` wait bit on the consumer. The control kind rides the same fields on
branch/`DEPBAR` ops. This is the shipped, current-silicon realization of the
four-kind model — and the up-to-date counterpart to the older per-class numbers
above (sm_100/103/110/120/121 are present here but absent from older tables).

> **Provenance.** The SASS instruction tables are NVIDIA's compiled data and are not
> redistributed; only recovered facts — type names, counts, field bit-positions —
> are cited.

## Validation on emitted SASS (CUDA 13.1)

The resolved hazards are observable directly: assemble PTX with `ptxas`, read each
instruction's 128-bit encoding with `nvdisasm -c -hex`, and decode the control
word (`usched`/`dst_wr_sb`/`src_rel_sb`/`req_bit_set`/`batch_t`). A battery of
probes — fixed-latency math chains, global/shared loads, transcendentals,
conversions, tensor MMA, and `cp.async` — was compiled for `sm_75 sm_80 sm_86
sm_89 sm_90 sm_90a sm_100 sm_120` (1912 decoded instructions). Each predicted
hazard mechanism appears exactly where the model places it.

**True (RAW), fixed path — the stall.** A dependent fixed-latency math pair
carries a small stall on the producer, stable across every generation:

| Pair | Stall (SM75→SM120) |
|---|---|
| `IADD3`→`IADD3`, `IMAD`→`IMAD`, `FFMA`→`FFMA`, `FADD`→`FADD` | **4** |
| `IADD3`→`IMAD`, `LOP3`/`FMUL`→consumer | **5** |
| `ISETP`→predicated op (CC/predicate) | **13** (the control band) |
| `HMMA.16816`→consumer (tensor) | **11** + follow-on (the tensor band) |

The emitted stall is the **producer→consumer dependency latency** for the coupled
math pipe, not the full result band: a same-pipe pair (`IADD3`→`IADD3`,
`FFMA`→`FFMA`) carries the coupled base latency of **4**, and a cross-pipe pair
(`IADD3`→`IMAD`, `LOP3`/`FMUL`→consumer) adds one cycle of inter-pipe hazard
penalty for **5**. Both sit *below* the 6-cycle scalar band because that band also
folds in the slower CC/predicate-write path — a coupled op that writes a
condition-code or predicate exposes `6 + 7 = 13` (the control band), which is why
the `ISETP`→predicated-op edge shows **13** unmodified. The `HMMA.16816` result
carries its own pipe penalty (**11** + follow-on, the tensor band). In every case
the stall encodes the cycle distance *between the two issue slots* — the value the
consumer must wait after the producer issues — which is already issue-relative, so
it is the dependency edge weight directly rather than a fixed band with a constant
subtracted.

**True (RAW), variable path — the scoreboard pairing.** Every variable-latency
producer that sets a write scoreboard is matched by a consumer carrying that
exact `req_bit_set` bit — **221 producer→consumer pairs, 0 unmatched**, on every
arch:

| arch | producers arming a write SB | matched consumer waits | unmatched |
|---|--:|--:|--:|
| sm_75 / sm_80 / sm_86 / sm_89 | 17 / 18 / 18 / 18 | 17 / 18 / 18 / 18 | 0 |
| sm_90 / sm_90a | 32 / 32 | 32 / 32 | 0 |
| sm_100 / sm_120 | 35 / 35 | 35 / 35 | 0 |

Loads (`LDG`/`LDS`/`LDGSTS`), special-register reads (`S2R`/`S2UR`), constant
loads (`LDC.64`), the MUFU pipe (`MUFU.RSQ`/`SIN`, `F2I`/`I2F`/`F2F`, `POPC`) all
arm `dst_wr_sb` and gate their consumer with `req_bit_set`. Note the MUFU pipe
carries the band-13 *latency* but delivers it through a **scoreboard**, not a
fixed stall — the scalar band gives the magnitude, the `INSTRUCTION_TYPE` gives
the mechanism. When loads outnumber the free scoreboards, ptxas packs several onto
one scoreboard (e.g. two `LDG`s both arming `SB2`) and the consumer's single wait
bit gates on the scoreboard *count* reaching its target.

**Partial-wait — `DEPBAR`.** A batch of `cp.async` copies confirms the
`DEPBAR.LE` mechanism. `cp.async.commit_group` lowers to `LDGDEPBAR` (commits the
outstanding `LDGSTS` into a group counter on `SB0`); `cp.async.wait_group k`
lowers to **`DEPBAR.LE SB0, k`** — block until ≤ *k* groups remain outstanding.
`wait_group 1` → `DEPBAR.LE SB0, 0x1`; `wait_group 0` → `DEPBAR.LE SB0, 0x0`.
Byte-identical SM80 through SM120. This is the "wait on partial completion of a
batch of outstanding ops" path the variable-latency model describes.

**Anti (WAR) — zero-but-ordered, confirmed.** Read-then-overwrite pairs carry
**no extra stall**: ptxas renames the destination register so the
anti-dependency never reaches the schedule, exactly as the *zero-but-ordered*
default predicts. The `opex` invariant (`opex = batch_t<<5 | usched_info`) holds
on all 1912 instructions, and no co-issue batch carries a group-ending stall.

Tooling: `decoded/sass-tools/sass_ctrl_decode.py` decodes the control words and
`sass_sched_sim.py --validate` runs the producer→barrier and `opex` self-check.

## Cross-References

- [Latency Model & Functional Units](latency-model.md) — the 72-B/40-B table layouts and per-FU latencies.
- [Scoreboards & Dependency Barriers](scoreboards.md) — the variable-latency barrier mechanism.
- [Scheduling Algorithm](algorithm.md) — how the resolved hazards drive priority-list scheduling.
- [Per-SM Scheduling Sample](../reference/data/per-sm-scheduling-sample.md) — extracted rule/scoreboard rows.
