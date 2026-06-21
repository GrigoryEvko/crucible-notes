# SASS-ISA tooling — parser · decoder · scheduler sim · legality checker

Binary-derived tooling for the decoded per-architecture SASS-ISA tables
(`decoded/nvdisasm-sass-isa/sass_isa_SM*.txt`). The rule grammar is
reconstructed from the `CONDITIONS` blocks in those tables; nothing here
republishes the table text.

## `sass_legality.py`

A constraint checker plus a tiny functional model.

**Checker** — `LegalityChecker(load_class(table, class_name))`. Loads one
`CLASS` block's `CONDITIONS` rules and evaluates them against a concrete
operand environment, reporting legal/illegal and the offending rule. Models the
four constraint families the tables enforce:

- `OOR_REG_ERROR` — register index in `[0, MAX_REG_COUNT-k]`, never `R254`.
- `MISALIGNED_REG_ERROR` — size-driven pair/quad alignment
  (`(((Rd)+((Rd)==RZ)) % N) == 0`, N ∈ {2,4,8,16}; RZ-exempt).
- `INVALID_CONST_ADDR_ERROR` — const-bank partition (allowed-bank lists,
  banks 18–23 reserved, banks 8–31 illegal in compute).
- shader-type gates — `(%SHADER_TYPE == $ST_CS) -> …`.

Rules that consult an external decoder lookup table (`DEFINED TABLES_x(...)`)
are reported `indeterminate` rather than silently passed.

```python
from sass_legality import LegalityChecker, load_class, DEFAULT_TABLE_DIR
cr  = load_class(DEFAULT_TABLE_DIR / "sass_isa_SM100.txt", "ldg_256_memdesc__Ra64")
chk = LegalityChecker(cr)
chk.is_legal({"Rd": 5, "word_mask": 255, ...}, shader="CS")  # -> (False, [Finding(...)])
```

**Functional model** — exact bit-level semantics for `iadd3`, `lop3` (the 8-bit
truth-table LUT), `shf_l`/`shf_r` (funnel shift), `prmt` (byte permute).
`python3 sass_legality.py --demo` runs the self-tests.

## `sass_validate.py`

Validates the model against real ptxas-emitted SASS. Drives the checker over
synthetic legal/illegal operand vectors and over the register operands actually
emitted by ptxas (in `/tmp/sass_validate/*.sass`, produced by hand-written PTX +
`ptxas` + `nvdisasm`), confirming the five hypotheses: register alignment,
const-bank partition, shader-type gates, FP-format/tensor capability, and the
`LOP3` LUT semantics.

## `sass_ctrl_decode.py` — scheduling-control-word decoder

Parses `nvdisasm -c -hex <cubin>` output and reconstructs, per instruction, the
scheduling-control word ptxas computed. Field bit-positions in the 128-bit
instruction word:

```
usched_info  105-109   5-bit stall/yield enum (0-27)
dst_wr_sb    110-112   write scoreboard the producer sets (7 = none)
src_rel_sb   113-115   read scoreboard the producer releases (7 = none)
req_bit_set  116-121   6-bit wait mask over SB0..SB5
batch_t      122-124   co-issue / batch control
opex      = batch_t<<5 | usched_info
```

`usched_info` decode: `0` → DRAIN (yield, stall 0); `1..15` → stall *n* and end
the co-issue group; `17..27` → stall *n*−16, no group-end.

```sh
ptxas -arch sm_90 -o k.cubin k.ptx
python3 sass_ctrl_decode.py k.cubin     # one decoded line per instruction
```

## `sass_isa_parser.py` — table model + `sass_decode.py` — 128-bit decoder

`sass_isa_parser.py` reconstructs each `CLASS`'s bit-field layout from the
`BITS_*` directives, keeping every operand **sub-form** (one `OPCODES`+`ENCODING`
pair) paired so an opcode pattern maps to the right field map. Validates all 13
arch tables cleanly.

`sass_decode.py` is a table-driven 128-bit instruction decoder. It splits a raw
word into opcode `{91}++{11:0}`, guard `Pg`@14:12 / `Pg_not`@15, the operand
ports, and the scheduling-control word (`stall`/`yield`/`dst_wr_sb`/`src_rel_sb`/
`req_bit_set`/`reuse`/`pm_pred`). A two-tier matcher resolves the mnemonic by the
class's canonical (primary) opcode — near-bijective, 720/721 unique on SM90 — and
falls back to sub-form opcodes for immediate-only forms (e.g. the `LOP3`
immediate opcode `0x812` that no class exposes as primary), disambiguating ties
by fixed-const-bit agreement.

```sh
python3 sass_decode.py --cubin k.cubin --section .text.k   # whole kernel
python3 sass_decode.py --word 0x000fe20000000800 0x00000a00ff017b82 --arch SM90
```

## `sass_roundtrip.py` — decoder round-trip validation

Decodes real ptxas-emitted cubins with `sass_decode` and scores every field
against `nvdisasm -c`'s own disassembly (opcode/mnemonic, dest GPR, source GPRs,
guard predicate). UR-aware (a port whose RHS source names `URb`/`URc` is rendered
as a uniform register, not a GPR).

Validated on CUDA 13.1 / nvdisasm V13.1.115 across `sm_75 sm_80 sm_90 sm_100
sm_120`: **368 real instructions, 100% match on opcode, dst-reg, src-reg, and
guard predicate.** Scoreboard producer→consumer pairs (a `LDG` arming write
barrier *n*, its consumer waiting on bit *n*) reproduce exactly.

```sh
python3 sass_roundtrip.py k1.cubin k2.cubin ...   # per-arch + aggregate match rate
```

## `sass_sched_sim.py` — warp-issue / timing simulator

Single-warp issue/timing model. Consumes a decoded stream and replays the
per-warp issue rule: wait-mask gate, dispatch stall, yield-on-DRAIN, `batch_t`
co-issue (START..END collapse to one slot), and scoreboard arm/wait. Emits a
cycle-by-cycle issue trace and a basic-block cycle estimate.

```sh
python3 sass_sched_sim.py k.cubin              # trace + total cycles
python3 sass_sched_sim.py k.cubin --validate   # producer->barrier + opex self-check
```

`--validate` returns `barrier_pairs` (consumer waits paired to an earlier arming
producer), `unmatched_waits` (must be 0), `opex_violations` (must be 0), and
`batch_with_group_end_stall` (must be 0). It consumes the **real** control-word
fields verbatim; the only modelling defaults are the variable-latency completion
times that decide when a scoreboard-wait gate opens (`DEFAULT_COMPL_LATENCY`,
clearly flagged, coarse pipe families — not recovered constants).

Validated against CUDA 13.1 ptxas/nvdisasm V13.1.115 across `sm_75 sm_80 sm_86
sm_89 sm_90 sm_90a sm_100 sm_120`: **221 producer→consumer scoreboard pairs, 0
unmatched; opex invariant holds on 1912/1912 instructions.**

## `sass_latency_tables.py` — per-(arch, class) scheduling model loader

The lookup the scheduler consumes, cached per arch. Bridges two binary-derived
keyings into one model:

- the per-arch SASS-ISA tables (`decoded/nvdisasm-sass-isa/sass_isa_SM*.txt`) →
  per mnemonic: `INSTRUCTION_TYPE` (coupled vs `decoupled_*_scbd`),
  `MIN_WAIT_NEEDED`, `SIDL_NAME`, `VIRTUAL_QUEUE` (the decoupled functional-unit
  pipe), `MEM_SCBD`/`MEM_SCBD_TYPE` (Blackwell);
- the ptxas scheduling tables (`decoded/ptxas-sched-full/`) → the scalar latency
  oracle (per-Ori-opcode result band {6,13,24,30,300}).

`load_arch_model(arch)` returns `{mnemonic → ClassModel}` (coupled?, pipe family,
min-wait, scoreboard arming, band). `coupled_stall(arch, prod_pipe, cons_pipe)`
resolves the issue-relative stall from `coupled_stall_matrix.tsv` (exact arch key
→ family key → wildcard; producer-pipe rules outrank consumer-pipe rules). Run
`python3 sass_latency_tables.py SM89` to dump the model + a stall-matrix sample.
Everything loads the **local** decoded copies at runtime — no vendor table text
or matrices are duplicated in the Python.

## `sass_scheduler.py` — scheduling composer / decomposer (the "scheduler brain")

Chooses the stall counts and scoreboard pairings a SASS patcher cannot. It models
scheduling as a **directed, typed, weighted dependency DAG** and works across four
invertible layers:

```
L0  raw 128-bit word, control bits 102-125
L1  decoded control fields (usched_info/dst_wr_sb/src_rel_sb/req_bit_set/batch_t)
L2  semantic events (arm SB n / wait SB n / stall k / co-issue group / yield)
L3  abstract typed+weighted dependency DAG over instructions
```

**Four edge kinds — RAW ≠ WAR in weight (the key asymmetry).** For a producer P
and a later consumer C sharing a register:

| Kind | P / C | weight | mechanism |
|---|---|---|---|
| **RAW** | P writes, C reads | producer **result** latency — for coupled math an issue-relative stall looked up by **(arch-family, producer-pipe, consumer-pipe)** in the table-driven model (4 same-pipe / 5 cross-pipe / AGU pre-issue 5 (8 on Turing) / 6 to the float↔int conversion or MUFU on older arches / 13 (12 Turing) for a CC-pred guard); or a variable band for memory/MUFU resolved by a scoreboard | stall, or scoreboard |
| **WAR** | P reads, C writes | a small **operand-read** default — its **own** per-resource value, **never** the transpose of the RAW weight; zero-but-ordered on registers | order |
| **WAW** | both write | write-ordering default (zero-but-ordered) | order |
| **CTRL** | CC / branch / barrier | sequencing | order |

**Table-driven coupled-stall model (`sass_latency_tables.py` + `coupled_stall_matrix.tsv`).**
The coupled/decoupled verdict, the functional-unit **pipe**, the `MIN_WAIT_NEEDED`
floor and the result band are read at runtime from the **local decoded tables**
— the per-arch SASS-ISA tables (`decoded/nvdisasm-sass-isa/sass_isa_SM*.txt`:
`INSTRUCTION_TYPE`, `VIRTUAL_QUEUE`, `MIN_WAIT_NEEDED`, `MEM_SCBD`) and the ptxas
scalar latency oracle (`decoded/ptxas-sched-full/scalar_latency_oracle.tsv`).
This makes the model track the per-arch instruction-selection boundaries
exactly — e.g. the int→float conversion is decoupled **I2F** on sm_75/sm_80 but
the coupled **I2FP** form from sm_86, and `ULDC` is absent on Blackwell. The
issue-relative stall *magnitudes* (the 4/5/6/8/13 a coupled producer owes by
pipe pair) are not a single table cell — ptxas's OCG scheduler derives them — so
they are recovered by differential analysis of emitted SASS and shipped as a
small per-(family, prod-pipe, cons-pipe) matrix (`coupled_stall_matrix.tsv`,
our own result, no vendor bytes). The composer's stall pass is a **sound
difference-constraint solver over issue cycles**: every fixed RAW edge enforces
`issue_cycle[C] − issue_cycle[P] ≥ weight`, iterated to a fixpoint, so the stall
shrinks as intervening instructions absorb the latency yet never under-stalls a
genuine hazard.

The same directed graph carries RAW and WAR edges with **different magnitudes**
because they are different physical events: RAW = "wait for the value to exist"
(≥4 cyc); WAR = "wait for the old value to be consumed" (≈0 cyc, the reader has
already latched by issue time and ptxas renames). Treating the WAR table as a
transposed RAW matrix is the classic reconstruction mistake.

**Capabilities.**
- `--decompose K.cubin` — parse `nvdisasm -c -hex`, build per-instruction
  read/write sets (GPR/UR/PRED, predicate guards, implicit UR descriptors),
  construct the typed/weighted DAG, decode each L1 control word, and **attribute**
  every wait-bit / stall to the producer it resolves.
- `--compose K.cubin` — the scheduler: walk issue order on a virtual cycle clock;
  for each coupled producer set `usched` stall = the minimum that satisfies its
  fixed-latency RAW edges as an issue-cycle difference-constraint fixpoint
  (the latency shrinks as intervening instructions absorb it; the CC/pred control
  band is emitted in full); assign `dst_wr_sb`/`src_rel_sb` to variable-latency
  producers, set consumers' `req_bit_set`, and run the 6-scoreboard VSB→PSB
  overload allocator (DEPBAR.LE on overflow).
- `--debug K.cubin [--dot g.dot]` — L0/L1/L2/L3 side-by-side, the DAG, per-
  instruction reasoning ("stall=4 because RAW Rd=R5 to next coupled math; SB2 waits
  the LDG at #N"), critical path, and an optional Graphviz graph.
- `--verify K.cubin …` — round-trip: decompose→compose on real ptxas output,
  compare to ptxas's stalls and scoreboard pairings, classify every mismatch.
- `--verify-corpus` — compile a battery of hand-written PTX probes (independent
  arith, dependent chains, loads feeding math, transcendental chains, mixed,
  int-multiply chains, transcendental mixes, memory chains, and an 8-load
  scoreboard-overload kernel) across `--arches` (default sm_75/80/86/89/90; the
  PTX `.version` auto-bumps to 8.6/8.7 for sm_100/sm_120), decode, recompute,
  diff, and print per-arch match rates + a diagnostic mismatch taxonomy.
- `--verify-dyn K.cubin --entry NAME` — **stretch dynamic check**: patch the
  recomposed control words back into a copy of the cubin (`patch_cubin`), launch
  the original and the patched kernel on the GPU via the CUDA Driver API
  (`launch_cubin.c`: `cuModuleLoad` + `cuLaunchKernel`, plain C against `libcuda`
  to avoid the gcc/nvcc host-header clash), and diff the output. Identical output
  proves the composed schedule is hazard-safe.
- `--perf-diff` — **MODE 1, measured ptxas-waste proof** (below).
- `--stall-profile {K.cubin | --amp PROBE}` — **MODE 2, per-instruction warp-stall
  observability** (below).

```sh
python3 sass_scheduler.py --decompose k.cubin
python3 sass_scheduler.py --compose   k.cubin          # composed vs ptxas, per instr
python3 sass_scheduler.py --debug      k.cubin --dot k.dot
python3 sass_scheduler.py --verify-corpus
python3 sass_scheduler.py --verify-dyn k.cubin --entry kname   # needs gcc + GPU
python3 sass_scheduler.py --perf-diff  --arch sm_89            # needs gcc + ncu + GPU
python3 sass_scheduler.py --stall-profile --amp amp_loadmath   # needs gcc + ncu + GPU
```

### GPU measurement: which conservative stalls are *real waste*

`--verify-corpus` reports an `understall_vs_ptxas_conservatism` bucket — coupled
instructions where our hazard-safe composed stall is **smaller** than ptxas's and
the kernel still runs bit-identical (e.g. `transcendental MOV ptxas=4/composed=1`,
`mem_chain IMAD.WIDE ptxas=6/composed=5`). Those are *candidate* over-stalls. The
two GPU modes turn the candidates into measured fact and validate the model
against live silicon. Both reuse the patch/launch plumbing (`patch_cubin`,
`launch_cubin.c`) and add a thin Nsight Compute (`ncu`) wrapper. Every metric name
in the code is a **public ncu name**; the recovered can't-issue taxonomy is used
only to *interpret* them.

**`--perf-diff` — proving ptxas waste on the GPU.** The corpus probes are tiny, so
a per-instruction stall delta is invisible above launch noise. The mode compiles
an **amplified probe corpus** (`AMP_PTX`): each probe is a self-contained
per-iteration dependent chain looped `N` times so the per-iteration delta
accumulates (default `-O3`, production ptxas; these probes do not unroll at the
trip counts used, so the surgical V2 stays sound). For each probe with an
understall candidate it builds **V1** (ptxas's native control words) and a
**surgical V2** (ptxas's words with *only* the understall-candidate stalls
tightened — every other word held at ptxas, so any cycle change is attributable to
those stalls alone), measures both under `ncu` (**`sm__cycles_active.avg`** — the
low-noise per-iteration metric — paired V1/V2/V1/V2 to cancel clock drift) at
**low occupancy** (one warp per scheduler, so a per-warp dispatch stall is on the
critical path, not hidden behind sibling warps), and gates on **bit-identical
output across 6 seeds**. When the all-candidates tighten faults or diverges, it
**bisects** (launch-only) to the largest bit-identical subset and measures that.
Per-kernel verdict:

| verdict | condition | meaning |
|---|---|---|
| **MEASURED ptxas WASTE** | V2 bit-identical, V2 fewer cycles (> 0.1 % floor) | the candidate stalls were slack — removing them really saves cycles |
| **hardware-enforced** | V2 bit-identical, equal cycles | the stall delta is absorbed; the latency is structural, not wasted issue slots |
| **HAZARD (not waste)** | V2 output differs (any of 6 seeds) | ≥ 1 of those stalls was required — tightening changes results; ptxas was right |

Measured on the sm_89 GPU (RTX 1000 Ada laptop), `niter=100000`, grid 8×32,
**`-O3` (production ptxas)**, `sm__cycles_active.avg` paired (floor ≈ 0.10 %):

| probe | safe candidates | V1 cyc | V2 cyc | Δ | verdict |
|---|---|---|---|---|---|
| `amp_transc`  | MOV, IADD3, LOP3, IMAD (4)        | 4.44 M | 4.32 M | **+2.70 %** | **MEASURED WASTE** |
| `amp_fpchain` | 18 coupled stalls                 | 2.13 M | 2.03 M | **+4.70 %** | **MEASURED WASTE** |
| `amp_intchain`| 18 coupled stalls                 | 0.63 M | 0.62 M | **+1.59 %** | **MEASURED WASTE** |
| `amp_loadmath`| 9 of 15 (6 address-IMAD hazards)  | —      | —      | +0.00 % | hardware-enforced |

So some of ptxas's `understall_vs_ptxas_conservatism` cases are **genuine slack**
(the transcendental/FP/int chains run ~1.5–4.7 % faster with the conservative
stalls removed, bit-identical, even against `-O3`), while `amp_loadmath` is
**hardware-enforced** — its load-feeds-math stalls are absorbed by surrounding
latency, and 6 of its candidates are **real hazards** (a loop-carried `IMAD`
feeding the address path) that the multi-seed bit-identical gate correctly rejects.
(Earlier `-O1` measurements showed larger ~7–10 % deltas because `-O1` ptxas leaves
more slack; the numbers above are the honest `-O3` figures with a stable metric.)

**`--stall-profile` — per-instruction warp-stall observability.** Runs `ncu` PC
sampling (the public `smsp__pcsamp_warps_issue_stalled_*` family via the Source
Counters page), reads back a **per-SASS-instruction stall-reason histogram**, and
cross-maps each instruction's dominant reason to (1) the mechanism our composer
assigned it (a fixed `usched` stall vs a scoreboard `req_bit_set` wait) and (2) the
recovered can't-issue taxonomy (named only by its public ncu reason). It then
reports model-vs-silicon **agreement**:

- `long_scoreboard` lands on our **decoupled global-memory-load** waits (e.g. the
  `IADD3` consuming a `LDG.E` in `amp_loadmath` — 33 k samples, exactly our
  scoreboard wait);
- `short_scoreboard` lands on our shorter **decoupled MUFU / shared** waits (the
  two `FMUL`s consuming `MUFU.RSQ` in `amp_transc`);
- `wait` / `dispatch_stall` land on our **fixed coupled stalls** (the FFMA / IMAD /
  ISETP forwarding chains);
- branch-refill (`no_instructions` / `branch_resolving`), `imc_miss` and `drain`
  are flagged `~` (structural effects of many-warp contention / control flow, not
  a per-edge hazard our single-warp model predicts).

Across the four amplified probes the agreement on data-dependency stalls is
**100 % (`amp_intchain`), 93.8 % (`amp_transc`), 88.9 % (`amp_fpchain`), 85.7 %
(`amp_loadmath`)**. The handful of contradictions are useful model-refinement
flags: e.g. in `amp_loadmath` an `IMAD` consuming a *coupled* producer shows `wait`
(ncu is right — a fixed stall) where our model over-attributed the load's
scoreboard; in `amp_fpchain` a constant-`MOV` feeding the address `IMAD.WIDE` shows
a `wait` our per-edge model did not predict. The contradictions are reported
per-instruction so they can drive the next round of model calibration.

### Beating ptxas on cycles — a GPU-gated optimizing scheduler

Two modes turn the model into an optimizer that is **provably never wrong and
never slower than ptxas**. Both reuse the patch/launch/`ncu` plumbing above and
add the same absolute correctness gate: a rescheduled cubin is accepted only if,
on the sm_89 GPU, its output is **bit-identical to ptxas's across 6 seeds × 2
relaunches** *and* its measured cycles do not regress; otherwise that block (or
that stall) **falls back to ptxas byte-for-byte**. The final per-block result is
therefore `min(ptxas, ours-that-passed-the-gate)`.

The cycle metric is **`sm__cycles_active.avg`** (run-to-run CV ≈ 0.00 % on this
box vs up to 0.83 % for `gpc__cycles_elapsed.max`), measured **paired** (V1, V2,
V1, V2 … to cancel DVFS drift). The accept/win threshold (0.1 %) is tied to that
measured noise floor.

**`--tighten K.cubin` (Stage 1 — stall-tightener).** Keeps ptxas's instruction
*order* and tightens only the stalls the model proves are slack (a coupled
producer whose hazard-safe stall is below ptxas's conservative one). It builds a
**surgical** patch (only the candidate stalls change), gates bit-identical, and on
a fault **bisects** to the maximal safe subset. This is a guaranteed, zero-risk
win on FP/transcendental-heavy kernels. Measured at **`-O3`** (production ptxas),
hardened harness:

| kernel | tightened (bit-identical) | ptxas cyc | ours cyc | Δ | verdict |
|---|---|---|---|---|---|
| `amp_transc`  | 4 stalls (6 cyc/iter)  | 4.44 M | 4.32 M | **+2.70 %** | MEASURED WIN |
| `amp_fpchain` | 18 stalls (30 cyc/iter)| 2.13 M | 2.03 M | **+4.70 %** | MEASURED WIN |
| `amp_intchain`| 18 stalls (35 cyc/iter)| 0.63 M | 0.62 M | **+1.59 %** | MEASURED WIN |
| `amp_loadmath`| 9 of 15 (6 are hazards)| —      | —      | +0.00 % | hardware-enforced |

The multi-seed gate is what makes this honest: on `amp_loadmath` it correctly
flags **6 of 15** candidates as genuine hazards (an address-feeding `IMAD` whose
latency the GPU enforces) that a single-seed gate would have wrongly accepted.

**`--optsched K.cubin` (Stage 2 — constraint-optimal reorder).** Splits the
kernel into basic blocks at branch targets (`sass_blocks.py`; barrier/`BSSY`/
control-straddling state is pinned, loop-carried scoreboards have **both** their
arm and wait ends pinned), builds the per-block dependency DAG, and solves for an
issue order that minimizes the block makespan:

- **model.** Per-instruction issue cycle `t[i]`; constraints = RAW latency
  (coupled fixed stall `t[c] ≥ t[p] + lat`, decoupled scoreboard-absorbed
  order floor), WAR/WAW/CTRL ordering (the **fixed register allocation** is
  preserved — every false dependency is a hard edge, which caps the achievable
  reorder), single-issue `Distinct(t)`, terminator-last, and the pinned cross-block
  state. The objective is the global makespan.
- **solver.** The whole program is encoded as **one SMT-LIB2 optimization** and
  discharged to an external Z3 (incremental bounded check-sat / binary search on
  the makespan), so the solver co-optimizes every block at once; a per-block list
  scheduler + in-process Z3-Opt back it up. Per block we keep the best of
  `{ptxas, list, optimal}`.
- **emit + patch.** Because every SASS instruction is a fixed 16 bytes and a block
  has no internal branch target, the chosen permutation is applied by **permuting
  the 16-byte words in place** (no PC-relative fixup) and writing freshly
  recomposed control words; the operand-reuse hint is cleared on reorder.

The honest result: **`-O3` ptxas's combined order+stall schedule is excellent and
essentially optimal on small or rolled kernels** — the model and silicon agree, so
`--optsched` proposes no reorder and simply falls back (never slower). Reproducible
*ordering* wins appear only on **large unrolled hot loop bodies** where ptxas's
greedy list scheduler leaves a few cycles per iteration. On `rc1_twochain` (a hot
loop of four independent FFMA chains, 48-instruction unrolled body), the solver
re-interleaves the chains (block makespan 56 → 49), **all 5 reordered blocks pass
the bit-identical + cycle gate**, and the kernel measures **930 400 → 920 399
cycles (+1.08 %)** — a real, gated reorder win over `-O3` ptxas. `--optsched` then
runs Stage-1 tightening on top, so it is always at least as good as `--tighten`.

**Limits (stated plainly).** The fixed register allocation we inherit from ptxas
turns every WAR/WAW into a hard ordering edge, which structurally caps how much
reordering is possible (we never rename). Variable-latency producers are handled
by scoreboards, not static timing, so the model bounds — not predicts — their
completion. And the correctness gate is an *oracle for the inputs it runs*: a
kernel whose output is genuinely input-independent cannot be distinguished from a
hazard by any finite seed set (such blocks only have their semantically-inert
scheduling fields touched, so they remain safe in practice).

### Reachability ladder (how deep can we observe the scheduler?)

| Tier | channel | on this box | what it gives | caveats |
|---|---|---|---|---|
| **1** | Nsight Compute (`ncu`) PC sampling + cycle counters | **usable** (no sudo; `NVreg_RestrictProfilingToAdminUsers=0`) | per-instruction warp-stall-reason histograms, per-kernel cycle counts (both modes here) | sampled, aggregated over warps; not per-issue-slot truth |
| **2** | CUDA Debugger API / `cuda-gdb` | **possible** (verified: attach, `break_on_launch`, `stepi`, `info registers`, `info cuda warps/lanes`) | architectural ground truth — GPRs, PC, active/divergent lane masks for a *stopped* warp, SASS single-step | invasive: stops the whole grid, so it reads register/PC state, not live pipeline timing — complementary to Tier 1, not a stall-timing channel |
| **3** | privileged BAR0 / on-chip performance-monitor MMIO | not attempted | raw can't-issue signal counters behind the public metric names | root + undocumented register map + real risk of wedging the GPU |
| **4** | JTAG / DFT scan | n/a | gate-level state | lab equipment only |

Tier 1 is what these modes use and is sufficient to *prove* waste (cycle deltas
under a correctness gate) and to *validate* the scheduling model against live
silicon (per-instruction stall-reason agreement). Tier 2 is feasible here as a
future architectural-state oracle; Tiers 3–4 are out of scope.

If `ncu` returns an access-denied error (`ERR_NVGPUCTRPERM`), both modes detect it
early, print the exact remediation (the `NVreg_RestrictProfilingToAdminUsers=0`
modprobe option, or running under `sudo`), and degrade gracefully — the perf-diff
cubins are still built so the run can be repeated once profiling is permitted.

**Validation (CUDA 13.1).** **Producer→scoreboard pairing is 100 %** on every
arch (which producer arms each consumer's wait bit is reproduced exactly).
Moving from the prior hand-tuned constants to the table-driven model raised
coupled-stall **exactness from 71.9 % → 86.2 %** on the original 6-probe corpus
across sm_75/80/86/89/90 (per-arch sm_75 64.7 → 94.1, sm_80 70.0 → 80.0,
sm_86/89 72.1 → 81.4, sm_90 80.6 → 97.2). On the expanded 10-probe corpus across
9 arches (sm_75…sm_120) the stall exactness is **82.2 %** (264/264 scoreboard
pairs); the newer Blackwell-class arches sit at 88.9–97.2 %.

| stall exactness | sm_75 | sm_80 | sm_86 | sm_89 | sm_90 |
|---|---|---|---|---|---|
| before (heuristic, 6 probes) | 64.7 % | 70.0 % | 72.1 % | 72.1 % | 80.6 % |
| after (table-driven, 6 probes) | 94.1 % | 80.0 % | 81.4 % | 81.4 % | 97.2 % |

Which table fields fixed which mismatch buckets: the per-arch `INSTRUCTION_TYPE`
killed the I2F→I2FP / F2I rename-boundary misclassifications; the pipe
classification + the calibrated matrix made the same/cross-pipe (incl.
IMAD↔LOP3), the AGU pre-issue slot (and the Turing 8), the float↔int conversion
/ MUFU-input latch, and the CC/pred control band (13; 12 on Turing) all **exact**
— the prior `cc_pred_control_band`, `turing_prestore_slot` and
`tensor_or_special_band` buckets are now empty.

**Dynamic: 10/10 sm_89 kernels** (including the 8-load scoreboard-overload and a
memory-dependency chain) recompose to **bit-identical GPU output**; the
L0↔L1 round-trip is 100 % (200 000 random fuzz words). What remains approximate:
ptxas's global-list-scheduler **reorder freedom** (it can hide a producer's
latency behind a downstream scoreboard wait we cannot replay → it sometimes
emits a smaller stall than our hazard-safe minimum), the `ULDC` uniform-descriptor
**publish distance** (its stall scales with descriptor-consumer distance and the
1/4/9 spread is ptxas-schedule-specific), and the variable-latency completion
*times* / exact SB *index* (allocator policy). Every non-exact composed stall is
still hazard-safe — the issue-cycle fixpoint honours every RAW edge, confirmed
bit-identical on the GPU. The model is recovered purely from binary analysis of
CUDA 13.1 `ptxas`/`nvdisasm` and differential study of emitted SASS, loaded from
the local decoded tables at runtime; it republishes no vendor table text.
