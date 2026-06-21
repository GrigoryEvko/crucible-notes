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
