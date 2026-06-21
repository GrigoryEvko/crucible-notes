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
| `MISALIGNED_ADDR_ERROR` | memory-operand alignment | 140 | **201** | **1** | **1** |
| `INVALID_CONST_ADDR_ERROR` | const-bank/offset legality | 2 | **210** | 15 | 17 |
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

`MISALIGNED_ADDR_ERROR` **collapses from ~201 (Hopper) to 1** on SM100+ — and the
const-address checks were *restructured*, not removed. SM90 replicated one RTV-bank
rule across ~210 classes; SM100 consolidates to ~15 checks under a new
**bank-partition model**: explicit allowed-bank lists, **banks 18–23 reserved**, and
a shader-gated rule **"banks 8–31 illegal in CS"** (compute shaders see only const
banks 0–7). This couples with the Blackwell `CCTL` cache-control expansion (16 → 35
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
**34 compute/trap-only**, **11 graphics-gated**, 1 mixed.

- **Compute/TRAP-only** (illegal in any graphics shader) — the Hopper async/tensor/cluster core: `HGMMA`/`IGMMA`/`QGMMA`/`BGMMA`, `WARPGROUP[SET]`, `UTMALDG`/`UTMASTG`/`UTMAREDG`/`UTMAPF`/`UTMACCTL`/`UTMACMDFLUSH`, `UBLKCP`/`UBLKRED`/`UBLKPF`, `LDGSTS`/`LDSM`/`STSM`, `UCGABAR*`/`SETCTAID`, `LDS`/`STS`/`STAS`/`REDAS`, `BAR`/`SYNCS`/`ARRIVES`, `USETMAXREG`/`USETSHMSZ`, `ATOMS`.
- **Graphics-gated** (require a pipeline stage) — `IPA`, `ISBERD`, `AL2P`, `ALD`, `AST`, `OUT`, `KILL`, `LDTRAM`, `CS2R`, `VOTE`, `CSMTEST` (attribute interpolation / fragment kill / SBE read).

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

## Cross-References

- [Architecture Evolution](architecture-evolution.md) — which instructions each capability tier contains.
- [SASS Instruction Encoding](instruction-encoding.md) — the operand fields these rules constrain.
- [Register Allocation — ABI](../regalloc/abi.md) — the alignment rules the allocator must satisfy.
- [Instruction Legality Matrix](../reference/instruction-legality.md) — the ptxas-side legality gate.
