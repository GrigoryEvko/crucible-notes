# SASS ISA — Architecture Evolution (SM75 → SM121)

> *Recovered from the decoded per-architecture SASS instruction tables shipped in
> nvdisasm V13.1.115 (CUDA 13.1). Facts only — counts, mnemonic sets, and the
> per-generation diff; the verbatim tables are not redistributed.*

nvdisasm carries one encrypted SASS instruction table per target. Decoding all
thirteen (Turing SM75 through Blackwell SM120/121) and diffing them recovers the
machine-level ISA evolution directly from the hardware decoder — what each GPU
generation physically added, removed, or gated, independent of any PTX-level
documentation.

## Nine decoders cover thirteen targets

Three groups of targets decode to **byte-identical** SASS tables (MD5 + `cmp`
verified):

| Group | Members | Status |
|---|---|---|
| Ampere consumer | `SM86 ≡ SM87 ≡ SM88` | byte-identical |
| Hopper | `SM90 ≡ SM90a` | byte-identical |
| Blackwell consumer | `SM120 ≡ SM121` | byte-identical |
| Distinct | `SM75, SM80, SM89, SM100, SM103, SM110` | all unique |

So **nine distinct decoders** cover all thirteen arch targets. The most important
consequence: **`SM90` and `SM90a` are the same hardware decoder.** The `a` suffix
does not change which instructions the silicon can decode — it is a *ptxas
PTX-acceptance gate* that controls whether ptxas will **emit** the accelerated
families (`wgmma`, `setmaxnreg`, async-tensor). This reconciles exactly with the
ptxas scheduling tables, where `sm_90` marks 6 WGMMA/async dependency-rule units
disabled and `sm_90a` enables them (see [Dependency & Hazard Model](../scheduling/dependency-model.md)).
The hardware decodes both identically; ptxas just refuses to schedule wgmma without `a`.

## The mnemonic surface

Across all arches there are **280 distinct uppercase SASS mnemonics**: **168
universal** (present on every target) and **112 architecture-variable**. The
universal core is the classic scalar machine — integer/FP ALU (`IADD3`, `IMAD`,
`LOP3`, `FFMA`, `FADD`, `FMUL`, `MUFU`), memory (`LDG/STG/LDS/STS/LD/ST`), control
(`BRA`, `BSSY`, `BSYNC`, `EXIT`, `CALL`, `RET`), and the move/convert family. The
112 variable mnemonics are where generations differ — overwhelmingly **tensor,
async-copy, cluster, and uniform-datapath** families.

## Generation-by-generation diff

Each step lists the net mnemonic delta and the families that drive it:

| Step | Δ | Added families | Removed |
|---|---|---|---|
| **SM75 → SM80** (Turing → Ampere) | +7 | `DMMA` (FP64 tensor); `LDGSTS`/`ARRIVES`/`LDGDEPBAR` (cp.async); `REDUX`; `SPMETADATA` (2:4 sparse) | — |
| **SM80 → SM86** | +4 | `F2IP`/`I2FP`/`UF2FP` (packed convert); `SUQUERY` | — |
| **SM86 → SM89** (→ Ada) | +1 | `QMMA` (FP8 tensor, transient) | — |
| **SM89 → SM90** (→ **Hopper**) | **+41** | **`HGMMA`/`IGMMA`/`QGMMA`/`BGMMA` + `WARPGROUP[SET]`** (wgmma); **`UTMALDG`/`UTMASTG`/`UTMAREDG`/`UTMAPF`/… + `UBLKCP`/`UBLKRED`/`UBLKPF`** (TMA / bulk-async); **`UCGABAR*`/`ELECT`/`SETCTAID`/`ENDCOLLECTIVE`** (CGA clusters); `STSM`/`SYNCS`/`FENCE`/`PREEXIT`; `REDAS`/`REDG`/`STAS` | `QMMA` |
| **SM90 → SM100** (→ **Blackwell-DC**) | +30 / −10 | **`UTCHMMA`/`UTCIMMA`/`UTCQMMA`/`UTCOMMA`/`UTCCP`/`UTCBAR`/`UTCSHIFT`** (tcgen05); **`LDTM`/`STTM`/`LDT`/`STT`** (tensor memory, TMEM); `FADD2`/`FMUL2`/`FFMA2`/`FHADD`/`FHFMA` (paired-FP); `QADD4`/`QFMA4`/`QMUL4`; `REDS`/`CREDUX`/`UREDGR`; `UGETNEXTWORKID` | **all `*GMMA` + `WARPGROUP*`**, `BMMA`, `RED`, `ULDC`, `SPMETADATA` |
| **SM100 → SM103** (→ **Bk-Ultra / GB300**) | +1 / −2 | `mufu_fp16_simd` (packed-FP16 special-fn); `ldtm_stat` | **`UTCIMMA`** (int tensor); **`utcmxqmma`** (MX-dedicated tensor) |
| **SM103 → SM110** (→ **Thor**) | +1 / −12 | restores `UTCIMMA` | `FADD2`/`FMUL2`/`FFMA2`; `VI*`; `QADD4`/`QFMA4`/`QMUL4`; `CREDUX`; `VHMNMX` |
| **SM110 → SM120** (→ **Bk-consumer**) | +27 / −17 | **`UFADD`/`UFMUL`/`UFFMA`/`UFSET[P]`/`UF2F`/`UF2I`/`UI2F`/`UI2I`/`UFMNMX`/`UFRND`/`UFSEL`/`UIABS`** (full uniform-FP ALU); `OMMA`; `MOV64IUR`; `CS2UR` | **all `UTC*` + TMEM (`LDT`/`STT`/`LDTM`/`STTM`)**; `UTCBAR`/`UTCSHIFT`/`UREDGR`; `VABSDIFF[4]` |

## The two decisive capability splits

### A. Tensor cores bifurcate at Blackwell

Hopper's **wgmma** (`*GMMA`, warpgroup-issued) is **entirely removed** at Blackwell
and replaced by **tcgen05** (`UTC*MMA`): async-issued tensor ops with **1-CTA / 2-CTA
variants** (two CTAs cooperatively feed one tensor core — 243 references on SM100),
`_tmem` vs `_gdesc` operand sources (a tensor-memory operand vs a global
descriptor), and a dedicated **tensor memory (TMEM)** addressed by `LDTM`/`STTM`/
`LDT`/`STT`. This is a complete redesign of the matrix datapath at the ISA level,
not an incremental extension.

### B. Datacenter and consumer Blackwell are not the same chip

tcgen05 + TMEM + 2-CTA paired issue exist **only on SM100/103/110** (datacenter +
Thor) and are **absent on SM120/121** (consumer RTX 50-series). Consumer Blackwell
does FP4/FP6 tensor through the legacy **per-warp** `OMMA`/`omma_scale`/`mxqmma_scale`
path (microscaling + 2:4 sparsity baked in via `SCALEFMT`/`SCALEVECTORSZ`) and has
**no tensor-memory engine** — but it uniquely gains a **full uniform-float ALU**
(12+ `UF*`/`UI*` ops), making SM120 the most uniform-datapath-rich arch (196 uniform
classes vs 148 on SM100). The compute capability gap between datacenter and consumer
Blackwell is an ISA-level gate, not a clock/SKU difference.

### C. Within datacenter Blackwell, SM103 (GB300/Ultra) is FP-biased

SM103 **drops** integer tensor (`UTCIMMA`) and the MX-dedicated tensor op
(`utcmxqmma`), and **adds** packed-FP16 special-function SIMD (`mufu_fp16_simd`) —
trading INT8 tensor throughput for FP16 special-function throughput. SM110 (Thor)
keeps `UTCIMMA` but drops the paired-FP scalar ops.

## FP-format capability map

| Arch | FP8 (`E4M3`/`E5M2`) | FP6 (`E3M2`/`E2M3`) | FP4 (`E2M1`) | Microscaling (MX) |
|---|:---:|:---:|:---:|---|
| Ada / Hopper (SM89/90) | ✅ | — | — | — |
| Blackwell DC (SM100/103/110) | ✅ | ✅ | ✅ | via tcgen05 (`utcmxqmma`, `utcqmma_scale`) |
| Blackwell consumer (SM120/121) | ✅ | ✅ | ✅ | via per-warp (`omma_scale`, `mxqmma_scale`) |

The microscaling `scale` MMA classes appear **first** on Blackwell (0 on Ada/Hopper,
~24–28 per Blackwell arch).

## Compute-only surface growth

Counting instruction classes legal **only** in compute/trap shaders (illegal in any
graphics stage):

| Turing SM75 | Hopper SM90 | Blackwell-DC SM100 |
|:---:|:---:|:---:|
| 55 | 143 | **224** |

Nearly a quarter of SM100's ISA is illegal outside compute — a quantitative measure
of the datacenter GPU's pivot to a compute-first device. The growth is driven
entirely by the async / tensor / cluster families above.

## Headline findings

1. **`SM90a` is not a hardware variant** — same decoder as `SM90`; the distinction is purely a ptxas PTX-acceptance flag.
2. **Consumer Blackwell (RTX 50xx) lacks the datacenter tensor core** (tcgen05/TMEM/2-CTA) — a hard, ISA-level gate, not a binning difference.
3. **Blackwell-Ultra (SM103/GB300) sacrifices INT8 tensor** (`UTCIMMA`) for FP16 special-function throughput.
4. **Hopper is the single largest expansion** (+41 mnemonics): TMA, CGA clusters, warpgroup MMA, and async barriers all arrive together.
5. **The matrix datapath was redesigned twice** in three generations — Ampere/Ada `HMMA`/`QMMA` → Hopper warpgroup `*GMMA` → Blackwell async `UTC*MMA` + tensor memory.

## Validated against real SASS

The capability map was cross-checked by generating tensor kernels per arch and
seeing what `ptxas` (V13.1.115) accepts, rejects, and emits. Two evidence
sources agree: the decoded-table presence of each family, and the SASS opcode
`ptxas` actually produces.

**Tensor-op presence (decoded tables).** `UTC*` (tcgen05) and TMEM
(`LDTM`/`STTM`/`LDT`/`STT`) exist only on the datacenter line; consumer Blackwell
substitutes `OMMA`:

| Arch | `UTC*` (tcgen05) | TMEM (`LDTM`/…) | `OMMA` | `*GMMA` |
|---|:---:|:---:|:---:|:---:|
| SM90 (Hopper) | — | — | — | ✅ (21 classes) |
| SM100 (Bk-DC) | ✅ (60) | ✅ (4) | — | — |
| SM103 (Bk-Ultra) | ✅ (52) | ✅ (5) | — | — |
| SM110 (Thor) | ✅ (60) | ✅ (5) | — | — |
| SM120 (Bk-consumer) | **—** | **—** | ✅ (2) | — |

`UTCIMMA` (integer tensor) is present on SM100 and SM110 but **absent on SM103** —
the GB300/Ultra INT8-tensor drop, confirmed at the class level.

**FP-format gates (ptxas accept/reject).** Generating `mma` of each width and
reading `ptxas`'s own diagnostics:

| Test | sm_75 | sm_80 | sm_89 | sm_90 | sm_100 | sm_120 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| FP16 `wmma` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| FP8 `mma.e4m3` | reject¹ | reject¹ | ✅ | ✅ | ✅ | ✅ |
| FP4 `mma.block_scale` | reject | reject | reject | reject² | reject³ | ✅ |

¹ ptxas: *"Feature 'mma with FP8 floating point type' requires .target sm_89 or
higher."* ² Hopper rejects block-scale outright. ³ The datacenter `mma.sync`
block-scale form is rejected on `sm_100a` because datacenter Blackwell routes FP4
through **tcgen05** (`UTC*`/`tcgen05.mma`), not the warp-level `mma.sync` path —
which is itself the bifurcation this page describes; consumer `sm_120a` accepts
it via the per-warp **`OMMA`** path.

**Emitted SASS opcode confirms the datapath split.** What each tensor kernel
actually lowers to:

| Kernel / arch | emitted SASS | reading |
|---|---|---|
| FP8 `mma` / sm_89 (Ada) | `QMMA` | the transient Ada FP8 tensor op |
| FP8 `mma` / sm_100 | `HMMA` | warp-level FP8 via the HMMA pipe |
| FP16 `wmma` / sm_90a, sm_100, sm_120a | `HMMA` | warp-`wmma` always lowers to `HMMA` |
| FP4 `mma.block_scale` / sm_120a | `OMMA` | the consumer-Blackwell per-warp FP4 op |

The warp-level `wmma.*` API lowers to `HMMA` on every arch; Hopper `HGMMA` and
Blackwell `UTC*MMA` are reached only through their dedicated async PTX paths
(`wgmma`, `tcgen05.mma`), never through `wmma`. `QMMA` on Ada and `OMMA` on
consumer Blackwell appear exactly where the generation diff predicts. Tooling and
the full sweep: `decoded/sass-tools/sass_validate.py`.

## Cross-References

- [SASS Instruction Encoding](instruction-encoding.md) — the 128-bit word these mnemonics encode into.
- [Legality & Capabilities](legality-and-capabilities.md) — the shader-type and constraint model.
- [Dependency & Hazard Model](../scheduling/dependency-model.md) — how the scheduler treats these ops.
- [SM Version Codes](../targets/version-codes.md) — how ptxas selects a target; the `a`-suffix gate.
