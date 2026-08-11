# SASS Legality & Hardware Capabilities

> *Recovered from the decoded per-architecture SASS instruction tables in
> nvdisasm V13.1.115 (CUDA 13.1). Counts are per representative non-alias arch.*

Every instruction class in the SASS tables carries a `CONDITIONS` block — a list of
guarded rules of the form `(predicate-expr) [→ alignment-expr] : "message"` — plus
`PROPERTIES` (`VALID_IN_SHADERS`, …) and `PREDICATES`. Together these define what the
hardware will accept: which register indices, which alignments, which shader stages,
and which operand/modifier combinations are legal. This page recovers that constraint
model and the per-arch capability surface.

## Hardware metadata (static descriptors)

Constant across all 13 tables: `WORD_SIZE 64` (a header artifact — the true word is
128 bits), an `ENCODING WIDTH 128`, `ELF_ID 190`, `ELF_ABI 0x33`,
`ELF_ABI_VERSION 7`, `ELF_VERSION 131`, and the identical 116-entry `R_CUDA_*`
relocator table. The `ARCHITECTURE "Volta"` / `PROCESSOR_ID Volta` strings are the
same literal in every file — a decoder-template artifact, **not** the true arch; all
per-arch differentiation lives in the `CLASS`/`OPCODE`/`CONDITIONS` bodies.
`%MAX_REG_COUNT` is a symbolic placeholder (register-file size resolved at use), with
`R254`/`RZ(R255)` as reserved sentinels.

## Legality constraint taxonomy

Nine constraint kinds, with representative occurrence counts:

| Kind | Enforces | SM75 | SM90 | SM100 | SM120 |
|---|---|---:|---:|---:|---:|
| `OOR_REG_ERROR` | register index in `[0, MAX_REG_COUNT−1]`, ≠ `R254` | 3823 | 7176 | 4688 | 7246 |
| `ILLEGAL_INSTR_ENCODING_ERROR` | shader-type / modifier / operand-combo illegal | 3205 | 4239 | 4450 | 5893 |
| `…_SASS_ONLY_ERROR` | legal binary, rejected by the assembler (e.g. disallowed `RZ`, opex table) | 2224 | 3079 | 2284 | 2215 |
| `MISALIGNED_REG_ERROR` | register-pair/quad alignment | 1826 | 2347 | 1936 | **4626** |
| `MISALIGNED_ADDR_ERROR` | memory-operand alignment | 140 | **201** | **0** | **0** |
| `INVALID_CONST_ADDR_ERROR` | const-bank/offset legality | 2 | **210** | 14 | 16 |
| `…_SASS_ONLY_ERROR` (const) | assembler-only const-addr | 144 | 202 | **0** | **0** |
| `UNPREDICTABLE_BEHAVIOR_WARNING` | non-fatal hazard | 6 | 6 | 6 | 6 |
| `ILLEGAL_INSTR_PARAM_ERROR` | parameter value out of range | 0 | 1 | 1 | 1 |

### Register-alignment model

The `MISALIGNED_REG_ERROR` rule is **size-driven**, in implication form: an operand
of width *W* forces its base register to a multiple:

- `sz == 64` → `Rd % 2 == 0` (even pair)
- `sz == 96` → `Rd % 4 == 0` (aligned quad, 3-of-4 used)
- `sz == 128` → `Rd % 4 == 0` (aligned quad)

`RZ` is exempt (`(Rd) == RZ` term). The **SM120 spike to 4626** comes from the new
uniform-float datapath plus wide-vector ops, all of which carry pair/quad alignment
rules. This is the hardware constraint ptxas's register allocator must satisfy for
every 64/96/128-bit (and tensor) operand.

### Blackwell relaxed address validation

`MISALIGNED_ADDR_ERROR` **collapses from 201 (Hopper) to 0** on SM100+ — every
standalone memory-alignment rule disappears — and the const-address checks were
*restructured*, not removed. SM90 replicated its RTV-bank rule across 202 classes
(plus 210 `INVALID_CONST_ADDR_ERROR` and 202 `…SASS_ONLY` const rules); SM100
consolidates to **14 `INVALID_CONST_ADDR_ERROR` checks and zero `…SASS_ONLY` const
rules** under a new **bank-partition model**: explicit allowed-bank lists, **banks
18–23 reserved**, and a shader-gated rule **"banks 8–31 illegal in CS"** (compute
shaders see only const banks 0–7). This couples with the Blackwell `CCTL`
cache-control expansion (16 → 35
classes: new `cctl_c_ldc*`/`ldcu*` const-cache forms plus `_pf2`/`_rml2`
prefetch/remote-L2 variants) — a fundamental const-bank/cache model change.

## Shader-type capability model

Legality by pipeline stage is stored as a `VALID_IN_SHADERS` bitmask plus
`%SHADER_TYPE ==` guards. The `$ST_*` / `ISHADER_*` enum has 8 stages:

| Stage | Meaning |
|---|---|
| `CS` | compute |
| `TRAP` | trap handler |
| `VSA`/`VSB` | vertex phases A/B (one `ISHADER_VS` bit) |
| `GS` | geometry |
| `TS`/`TI` | tess shader / tess-init (hull / domain) |
| `PS` | pixel |
| `$ST_UNKNOWN` | sentinel → always illegal |

Capability tiers on SM90 (202 primary mnemonics): **156 universal (`ISHADER_ALL`)**,
**34 compute/trap-only**, **9 graphics-gated**, 3 mixed.

- **Compute/TRAP-only** (illegal in any graphics shader) — the Hopper async/tensor/cluster core: `HGMMA`/`IGMMA`/`QGMMA`/`BGMMA`, `WARPGROUP[SET]`, `UTMALDG`/`UTMASTG`/`UTMAREDG`/`UTMAPF`/`UTMACCTL`/`UTMACMDFLUSH`, `UBLKCP`/`UBLKRED`/`UBLKPF`, `LDGSTS`/`LDSM`/`STSM`, `UCGABAR*`/`SETCTAID`, `LDS`/`STS`/`STAS`/`REDAS`, `BAR`/`SYNCS`/`ARRIVES`, `USETMAXREG`/`USETSHMSZ`, `ATOMS`.
- **Graphics-gated** (require a pipeline stage) — `IPA`, `ISBERD`, `AL2P`, `ALD`, `AST`, `OUT`, `KILL`, `LDTRAM`, `CSMTEST` (attribute interpolation / fragment kill / SBE read).
- **Mixed** — `CS2R` and `VOTE` carry both an `ISHADER_ALL` class and a graphics-restricted class, so they are legal in compute; a compute kernel reading `%clock64`/`%globaltimer` emits `CS2R` and one using `vote.sync` emits `VOTE` on SM90 and SM120 alike.

The compute-only surface grows monotonically: **55 (Turing) → 143 (Hopper) → 224
(Blackwell-DC SM100)** classes — nearly a quarter of SM100's ISA is illegal outside
compute.

## Predication model

Two parallel predicate register files with two guard paths:

- **General predicates** `P0–P6` + `PT`(=7, always-true): guard syntax
  `@[!]Predicate(PT):Pg` on **1435 of 1594 formats** (~90% of all instructions are
  predicable; default `PT`, optional `!` negation).
- **Uniform predicates** `UP0–UP6` + `UPT`: guard `@[!]UniformPredicate(UPT):UPg` on
  154 formats (SM90) — the scalar/uniform datapath ops (`UMOV`, `ULDC`, `LDCU`,
  `LDTM`, …) are guarded by the uniform predicate file.
- **Predicate destinations** (setters): `Pp` (primary, 336 classes), `Pu` (secondary,
  435), `Pq` (tertiary, 14) — e.g. `ISETP` can write **two** predicates. `VOTE`,
  `P2R`/`R2P`, and `PLOP3` (predicate LUT) move values between predicate and register
  space.

## A constraint checker built from the rules

The `CONDITIONS` grammar is small enough to execute. The decoded-table tooling
ships a checker (`decoded/sass-tools/sass_legality.py`) that loads one `CLASS`'s
rules and evaluates them against concrete operand values — reporting legal /
illegal and the offending rule. The rule language is a C-like boolean/arith
expression over named fields, where each rule is `(guard) -> (requirement) :
"message"` (with no `->`, the whole expression is the requirement). The four
families above map onto:

- **OOR** — `((Rd)==RZ) || (((Rd)<=MAX_REG_COUNT-1) && ((Rd)!=R254))`
- **alignment** — `mask-list -> ((((Rd)+((Rd)==RZ)) % N) == 0)`, `N ∈ {2,4,8,16}`
- **const-bank** — an OR'd allowed-bank list, optionally guarded by `%SHADER_TYPE == $ST_CS`
- **shader gate** — `(%SHADER_TYPE == $ST_CS) -> !(Sb_bank >= 8 && Sb_bank <= 31)`

Table-lookup rules (`DEFINED TABLES_x(…)`) are reported *indeterminate* rather
than silently passed. Feeding the SM100 `LDC` const-bound class through it
reproduces the bank partition exactly:

| operand | `CS` (compute) | `PS` (pixel) |
|---|:---:|:---:|
| `c[0x7][…]` (bank 7) | legal | legal |
| `c[0x8][…]` (bank 8) | **illegal** — *banks 8–31 not allowed in CS* | legal |
| `c[0x12][…]` (bank 18) | **illegal** — *banks 18–23 not allowed* | **illegal** — *banks 18–23 not allowed* |
| `c[0x18][…]` (bank 24) | **illegal** — *banks 8–31 not allowed in CS* | legal |

The reserved 18–23 window is unconditional; the 8–31 cutoff is shader-gated.

## Validation against real SASS

Every rule above was cross-checked against SASS that `ptxas` (V13.1.115)
actually emits, assembled from hand-written PTX and disassembled with
`nvdisasm -c` / `cuobjdump -sass`.

**Register alignment holds on every wide operand.** A kernel mixing 64-bit
`double` and 128-bit `int4` traffic lowers (SM90) to:

```text
LDG.E.64  R2,  desc[UR4][R2.64]    ; 64-bit load   -> data R2  (R2 % 2 == 0)
DFMA      R4,  R4, UR6, R2         ; 64-bit FP     -> dest R4  (R4 % 2 == 0)
STG.E.64  desc[UR4][R6.64], R4     ; 64-bit store  -> data R4  (R4 % 2 == 0)
LDG.E.128 R12, desc[UR4][R8.64]    ; 128-bit load  -> data R12 (R12 % 4 == 0)
STG.E.128 desc[UR4][R10.64], R12   ; 128-bit store -> data R12 (R12 % 4 == 0)
```

The data register of every `.64` op is even and every `.128` op is a multiple of
four; the address register inside `desc[…][Rn.64]` is itself a 64-bit pointer
pair and is always even. Sweeping all `wide_*.sass` (SM75 → SM120) found **zero**
alignment violations — exactly what the `MISALIGNED_REG_ERROR` rule demands, and
exactly what the register allocator must guarantee.

**Const banks stay in 0–7 for compute.** Across every compute kernel generated,
the only constant banks `ptxas` references are `c[0x0]` (the parameter/ABI bank)
and `c[0x3]` (a driver-reserved block) — never banks 8–31. This is the same
"compute sees only the low banks" rule the checker enforces above.

**Compute-only ops are shader-gated.** `LDGSTS`, `LDSM`, `STSM` and the whole
async/tensor core carry `VALID_IN_SHADERS = (1<<ISHADER_TRAP)+(1<<ISHADER_CS)`
(not `ISHADER_ALL`) on both SM90 and SM100. A `cp.async` kernel lowers to a real
`LDGSTS.E [R7], desc[UR6][R2.64]` in compute; the class is structurally illegal
in any graphics stage.

**The `LOP3` 8-bit LUT is a literal truth table.** `LOP3.LUT Rd, Ra, Rb, Rc,
imm8` computes, per bit, `imm8` bit `(Ca<<2)|(Cb<<1)|Cc` where `Ca/Cb/Cc` are the
corresponding bits of `Ra/Rb/Rc`. The functional model reproduces every canonical
table — and `ptxas` itself emitted `0xC0`, `0xFC` and `0x96` for the source
`a&b`, `a|b`, `a^b^c`:

```text
LOP3.LUT R8, R0, R7, R6, 0xc0, !PT   ; a & b
LOP3.LUT R9, R0, R7, R6, 0xfc, !PT   ; a | b
LOP3.LUT R10, R0, R7, R6, 0x96, !PT  ; a ^ b ^ c
```

| `imm8` | function | | `imm8` | function |
|:---:|---|---|:---:|---|
| `0xF0` | `a` | | `0x80` | `a & b & c` |
| `0xCC` | `b` | | `0xFE` | `a \| b \| c` |
| `0xAA` | `c` | | `0xC0` | `a & b` |
| `0x3C` | `a ^ b` | | `0xFC` | `a \| b` |
| `0x96` | `a ^ b ^ c` | | `0xF8` | `a \| (b & c)` |

(Worked: `0xF8` is *not* `(a&b)|c` — that is `0xEA`; building the LUT from the
truth table corrects the intuition. This is the kind of slip the functional
model catches.) See `decoded/sass-tools/sass_validate.py` for the full
hypothesis sweep.

## Cross-References

- [Architecture Evolution](architecture-evolution.md) — which instructions each capability tier contains.
- [SASS Instruction Encoding](instruction-encoding.md) — the operand fields these rules constrain.
- [Register Allocation — ABI](../regalloc/abi.md) — the alignment rules the allocator must satisfy.
- [Instruction Legality Matrix](../reference/instruction-legality.md) — the ptxas-side legality gate.
