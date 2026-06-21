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

```sh
python3 sass_scheduler.py --decompose k.cubin
python3 sass_scheduler.py --compose   k.cubin          # composed vs ptxas, per instr
python3 sass_scheduler.py --debug      k.cubin --dot k.dot
python3 sass_scheduler.py --verify-corpus
python3 sass_scheduler.py --verify-dyn k.cubin --entry kname   # needs gcc + GPU
```

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
