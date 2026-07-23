# SUNDA v2 Baseline Topology

This page is the **definitive SUNDA reference**: SUNDA (NC-v2, **arch_id 5 /
coretype 6**) as the *floor* of the unified-NeuronCore GPSIMD generation line.
It consolidates the byte-level SUNDA image carves, the SUNDA-specific kernels,
and the SUNDA-touching cross-gen surfaces into one v2-floor model — the
generation every later gen (CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK) is a
*superset* over.

The thesis in one sentence: **SUNDA is not an engine-count-reduced baseline; it
is the same five-NX-engine + POOL-Q7 structure as CAYMAN on the same Cairo /
NX1.1.4 Vision-Q7-NX core, reduced *per subsystem* — packaging, compute,
collective, dispatch, observability — while carrying a handful of genuine
v2-distinct features (a BF16 fast-path cluster, a dual-ALU `TensorScalarPtr`, a
divergent activation header, a standalone-EXTISA delivery model, the only
non-zero EXTRAM) that the later, more general ISA retired.**

Everything here is read from the shipped binaries and headers of the
`aws-neuronx-gpsimd-customop-lib_0.21.2.0` and
`aws-neuronx-runtime-lib_2.31.24.0` packages. Recovered symbols, strings,
shipped C headers (`neuron_sunda_arch_isa/`), and `.json` manifests are
binary-derived artifacts and are cited as such; no external source tree is
referenced.

```
INT  = …/custom_op/c10/lib/libnrtucode_internal.so   (5-gen symbol twin, NOT stripped)
       sha256 b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b ; 10,276,288 B
A    = …/custom_op/c10/lib/libnrtucode.a             (static archive, 435 members; 48 SUNDA)
EXT  = …/aws/neuron/lib/libnrtucode_extisa.so        (standalone EXTISA container, stripped)
       sha256 dc00763dbdb27cb49d90cad55da676b9e01c5c0dfa3151919bc4e8ea1c39159f
INC  = …/custom_op/c10/include/neuron_sunda_arch_isa (the NC-v2 ISA + arch headers)
OBJD = …/gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump   (XTENSA_CORE=ncore2gp; the device decoder)
```

> **NOTE — VMA == file offset only in `.text`/`.rodata`.** In `INT` the
> `get_ext_isa` jump table @ `0x556c` and the `*_libs` tables @ `0x9b8f80+` are
> in identity-mapped sections; the `EXT` container's `.data`/`.data.rel.ro`
> carry a `+0x1000` VA↔file delta (`sunda_libs @VA 0x934b00` is read *by VA*),
> and the ncore2gp config DLLs carry the `+0x200000` `.data` delta. Carved
> *device* images (IRAM/DRAM) map file-offset == device IRAM VA, with
> **DRAM string-offset == device DRAM VA − `0x80000`**. `extracted/`/`ida/`
> are gitignored — every path is absolute / `--no-ignore`.

**Confidence tags** follow
[the Confidence & Walls Model](../reference/confidence-model.md): `OBSERVED` =
a byte/string/header read; `INFERRED` = reasoned over OBSERVED
facts; `CARRIED` = consolidated from a cited sibling at its stated confidence.
Crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but
real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive
reading), **NOTE** (orientation).

---

## 0. Headline

1. **SUNDA is the v2 floor, not a missing-engine baseline.** It ships the *same*
   six getter classes as CAYMAN — `NX_{ACT,DVE,PE,POOL,SP}` + `Q7_POOL` — on the
   *same* gen-invariant Cairo / NX1.1.4 / Xtensa24 Vision-Q7-NX core
   (`XtensaTools-14.09 clang 10.0.1`, e_flags `0x300`). The reduction is
   per-subsystem. `[HIGH/OBSERVED]`
2. **The v2 absence floor** (§2): no RNG *body* (the `RAND_ALGORITHM` enum lacks
   `XORWOW`, which appends at CAYMAN); no `0xe4 CONV_LUT_LOAD`; no `0xbf` on-chip
   SB2SB collective; no `0xf0` ExtendedInst NX→Q7 *bridge*; no PROF CAM/table; no
   DKL variant; no in-library EXTISA; the monolithic NCFW; and **zero** `S:`/`P%i:`
   self-naming logs — the observability floor. `[HIGH/OBSERVED]`
3. **SUNDA is genuinely distinct, not just minus-CAYMAN** (§3): a five-opcode
   2×-throughput BF16 cluster (`0x8a/0x8b/0x8c/0x8d/0x8f`), the dual-ALU
   `TensorScalarPtr` pair (`0x87/0x88`), a divergent activation PWP header
   (`dbdca26b…` vs cayman+ `8f6f5f49…`: CAM mask byte-swap + 2 fewer config
   bits), the retained static `CUSTOM_OP_HEADER/PAYLOAD` structs (the *only*
   removals in the whole chain), the arch5 single-data-band device ELF, the
   external-lib loader shell Q7, the sole non-zero EXTRAM, and the
   standalone-only EXTISA delivery model. `[HIGH/OBSERVED]`
4. **The boot signature is a relocated CAYMAN shape** (§4): NX reset `j 0x1dc`
   (== CAYMAN), but `enter_run @0x94` (CAYMAN `@0x90`, **+0x4**), Q7 reset
   `j 0x220` (CAYMAN `j 0x200`, **+0x20**), window base `0x00100000`
   (CAYMAN-family `0x04000000`). `[HIGH/OBSERVED]`
5. **"Sunda-mode" ≠ SUNDA** (§4f): the CAYMAN-and-later runtime software-fetch
   *fallback* is *named after* the legacy SUNDA-style fetch but is **not** the
   SUNDA generation. The SUNDA NX image is the pre-dual-mode monolithic baseline
   that has *neither* the HW-Decode FSM *nor* the "Sunda-mode" construct. `[HIGH/OBSERVED]`

---

## 1. The SUNDA identity (NC-v2 / arch_id 5 / coretype 6 / the Cairo core)

### 1a. The generation identity

| Field | Value | Anchor | Conf |
|---|---|---|---|
| codename | **SUNDA** | — | — |
| NC-version | **NC-v2** | `aws_neuron_isa_tpb_common.h:3` "ISA header for NC-v2" | `[HIGH/OBSERVED]` |
| **arch_id** | **5 (0x05)** = coretype − 1 | `<GEN>_NX_TOPSP` enum ordinal + `libncfw_get_image` selector `0x05` | `[HIGH/OBSERVED]` |
| **coretype** | **6** = `NRTUCODE_CORE_SUNDA_NX_POOL` | `get_ext_isa` case index `coretype−6 == 0` → `sunda_libs @0x9b8f80` | `[HIGH/OBSERVED]` |
| NCFW selector byte | `0x05` → `v2_ncfw_{iram,dram}_bin` | `libncfw_get_image` arch_id ladder | `[HIGH/OBSERVED]` |
| core kind enum | `NRTUCODE_CORE_SUNDA_NX_POOL` (first of 5) | `INT` strings | `[HIGH/OBSERVED]` |
| ucode/opset version | **1.21.1.0** | `INT` strings (27 hits) + EXTISA JSON `ulib_to_ucode_version` | `[HIGH/OBSERVED]` |
| shipped? | **YES** (all 4 libs) | `.a` has 48 SUNDA members; `NRTUCODE_CORE_*` names 5 gens in `INT`, 4 in `SO` | `[HIGH/OBSERVED]` |
| silicon | gen-2 (trn1/inf2-era) | not part-name-bound in this corpus | `[LOW/INFERRED]` |

The `coretype = arch_id + 1` relation is not a coincidence: both are positions in
one dense C enum `nrtucode_coretype_t`, where `<GEN>_NX_TOPSP` is the `arch_id`
ordinal and `<GEN>_Q7_POOL` is the `coretype` ordinal — see
[Codename ↔ Generation Map](codename-generation-map.md) for the row-by-row enum
proof and the full 32-entry `get_ext_isa` jump table. SUNDA sits unambiguously at
the bottom of all six independent monotone anchors (coretype 6 / arch_id 5 /
NCFW sel-byte 0x05 / NC-banner v2 / dtype-superset / toolchain), and the
SUNDA→CAYMAN step is the line's only `+7` coretype stride (every subsequent is
`+8`). `[HIGH/OBSERVED]`

> **GOTCHA — the silicon part is *not* pinned by these binaries.** The
> `coretype → silicon-part` map is not inside any GPSIMD binary in this corpus.
> "gen-2 / trn1/inf2-era" is an INFERRED placement, never a byte-observed
> binding; do **not** fabricate a Trn-N or Inf-N number. `[LOW/INFERRED]`

### 1b. The core is gen-invariant — the reduction is above the Xtensa IP

SUNDA's GPSIMD Xtensa core is the *same* Cairo (`uarchName`) / NX1.1.4 /
`Xm_ncore2gp` / Xtensa24 Vision-Q7-NX datapath as CAYMAN/MARIANA/MARIANA_PLUS:
`XCHAL_HAVE_VISION = 1`, `XCHAL_VISION_TYPE = 7`, `IsaMaxInstructionSize = 32`
(the FLIX/VLIW layer), 512-bit SIMD. All four gens' EXTISA Q7 blobs carry the
same `.comment` (`XtensaTools-14.09 clang version 10.0.1`) and e_flags `0x300`.
**The SUNDA "reduction" is at the SoC / firmware / TPB-engine-ISA layers, not in
the Xtensa core IP** — the Cairo datapath underneath is constant across the line.
`[HIGH/OBSERVED — CARRIED from the lineage survey]` See
[Codename ↔ Generation Map](codename-generation-map.md) for the cross-gen
toolchain identity.

### 1c. The NCFW has no private SUNDA version

Both NCFW host selectors (`libncfw_get_image` *and* `libncfw_ctx_log`) key on the
`arch_id` integer (= coretype − 1), not a private NCFW axis. The `arch_id == 5`
leg returns the `v2_ncfw_{iram,dram}_bin` blob pair; the codename `ctx_log`
function `sunda_ncfw_ctx_log` is present (alongside cayman/mariana/mariana_plus —
no maverick, since `libncfw` tops out at `arch_id 0x1c`). The "v2" tag *is* the
SUNDA codename, not a separate firmware version number. `[HIGH/OBSERVED — CARRIED
from the NCFW survey + the get_image ladder]`

### 1d. RELEASE-only, ucode 1.21.1.0

`INT` surfaces exactly **24 SUNDA getters** = six engine-classes × four regions,
**RELEASE-only** (no DEBUG/PERF/TEST getters). The static archive `A` also holds
the SUNDA *DEBUG* members (48 SUNDA members total = 6 engine-classes × DEBUG+RELEASE ×
4 regions, of which 16 are the POOL pair) for module enumeration, but the host
runtime selects RELEASE. RELEASE is SUNDA-specific naming (CAYMAN+ use
DEBUG/PERF/TEST); in observability it sits at CAYMAN's PERF level (no logs). The
EXTISA JSON manifest declares `ulib_to_ucode_version "1.21.1.0"`. `[HIGH/OBSERVED]`

### 1e. The standalone-EXTISA dependency — the structural v2-floor identity bit

SUNDA is the **only** gen whose EXTISA Q7 kernels are resolved *exclusively* from
the separate `EXT` container. The in-library getters are weak-undef:

```
$ readelf -sW INT | rg -i 'SUNDA.*EXTISA'
  21: 0  0  NOTYPE  WEAK  UND  SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get
  22: 0  0  NOTYPE  WEAK  UND  SUNDA_Q7_POOL_RELEASE_EXTISA_0_JSON_get
1347: 0  0  NOTYPE  WEAK  UND  SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get
1348: 0  0  NOTYPE  WEAK  UND  SUNDA_Q7_POOL_RELEASE_EXTISA_0_JSON_get
```

Both getters are `NOTYPE WEAK UND` (value 0, no body), and the SUNDA device-ELF
blob + its JSON manifest are **not present anywhere** in `INT` — the standalone
manifest markers `all.stripped.so` / `ulib_to_ucode_version` return **0 hits** in
`INT`. They live only in `EXT`, where the same two getters are defined at `0xaf10`
/ `0xaef0`. By contrast CAYMAN/MARIANA/MARIANA_PLUS live in *both* the container
*and* as in-library getters (the internal-twin), and MAVERICK lives
internal-only. **SUNDA = standalone-container-only.** This is the
[arch5 EXTISA container](../images/sunda-arch5-extisa.md) model. `[HIGH/OBSERVED]`

---

## 2. The v2 floor — per-subsystem absences (what SUNDA lacks vs CAYMAN)

Each cell is a *reduction* relative to the CAYMAN(v3) reference, anchored to the
absent opcode/struct/string/getter. (The `S:` handler-name logs are themselves
absent — the observability floor, §2e — so absences are proven by 0-hit byte/
header sweeps, with the search scope stated.)

> **CORRECTION — "absent" means *image/kernel_info-level*, not
> *ISA-header-declaration-level*.** The SUNDA `neuron_sunda_arch_isa` headers are
> the *shared opcode-space declaration* and DO declare several opcodes whose
> *bodies* are not implemented at the v2 floor: `TENSOR_DEQUANTIZE = 0x7b`
> (`common.h:211`), `EXTENDED_INST = 0xf0` (`common.h:297`), and the
> `DEQUANT_FMT` enum (incl. FP4_E2M1/NF4 schemes, `common.h:788`). The v2 absence
> is therefore: **no kernel body, no `kernel_info_table` row, no `0xf0` escape on
> the SUNDA images** — *not* a missing header declaration. Where a feature is
> genuinely absent *from the header too* (e.g. `XORWOW`, `CONV_LUT_LOAD 0xe4`,
> `SB2SB_COLLECTIVE 0xbf`), that is called out explicitly below. `[HIGH/OBSERVED]`

### 2a. The per-subsystem absence floor

| Subsystem | Present at SUNDA? | First gen that adds it | Evidence (search scope) | Conf |
|---|---|---|---|---|
| RNG body / `XORWOW` algo | **NO** | CAYMAN (SW Xorwow; TIE+LFSR at MARIANA) | `RAND_ALGORITHM` enum = `{LFSR 0, PCG32 1, PHILOX 2}` in `sunda/common.h:874`; CAYMAN appends `XORWOW = 3` `cayman/common.h:898`; `rg -i xorwow neuron_sunda_arch_isa` = **0** vs CAYMAN **5** | `[HIGH/OBSERVED]` |
| `TENSOR_DEQUANTIZE 0x7b` *body* / MX / cptc | **NO** (declared, no body) | CAYMAN (POOL-Q7 MX-dequant) | `0x7b` declared `sunda/common.h:211` but no dequant kernel body, no `kernel_info_table` row; device-ELF `xtensa-elf-strings` = 0 dequant/cptc/MX | `[HIGH/OBSERVED]` |
| `CONV_LUT_LOAD 0xe4` | **NO** | CAYMAN | `rg CONV_LUT sunda/common.h` = **0**; declared CAYMAN-first `cayman/common.h:294` | `[HIGH/OBSERVED]` |
| `SB2SB_COLLECTIVE 0xbf` (on-chip SBUF→SBUF reduce-copy) | **NO** | CAYMAN (nc≥V3) | `rg SB2SB_COLLECTIVE sunda/common.h` = **0** vs CAYMAN **1**; no `aws_neuron_isa_tpb_s3d3_collective.h` (SUNDA 0, CAYMAN 1) | `[HIGH/OBSERVED]` |
| `gather_xpose 0xf1` / `DmaTranspose 0xbd` DGE kinds | **NO** | CAYMAN (nc≥V3) | no `aws_neuron_isa_tpb_dma_gather_xpose.h` on SUNDA (0 vs CAYMAN 1) | `[HIGH/OBSERVED]` |
| `GetSequenceBounds` / `NonzeroWithCount` (0xf2 family) | **NO** | post-SUNDA | no `S3D3_SEQ_BOUNDS` / `S3D3_NONZERO_WITH_COUNT` structs | `[HIGH/OBSERVED — CARRIED]` |
| `Sort` (decode_sort) | **NO** | post-SUNDA | no `Sort`/`sort` string in the device ELF | `[HIGH/OBSERVED — CARRIED]` |
| `0xf0 ExtendedInst` NX→Q7 *bridge* | **NO** (opcode declared, no escape row) | CAYMAN | `0xf0` declared `sunda/common.h:297` but **no `0xf0` row** in the 18-entry `kernel_info_table`, all specs = 0; "ExtendedInst" string absent in both SUNDA POOL cores | `[HIGH/OBSERVED]` |
| PROF (CAM / TABLE) | **NO** | CAYMAN (47-record CAM + 8 KiB table) | no `PROF_CAM`/`PROF_TABLE` getters, no `hwdecode_` `.a` members; PROF_CAM `8fd7e422` absent | `[HIGH/OBSERVED]` |
| DKL variant | **NO** separate image | — (function *is* the SUNDA Q7 base, §3e) | no separate SUNDA DKL image | `[HIGH/OBSERVED]` |
| In-library EXTISA | **NO** | CAYMAN (internal-twin) | weak-undef getters (§1e); blob absent from `INT` | `[HIGH/OBSERVED]` |
| Multi-flavor images (DEBUG/PERF/TEST) | **NO** (RELEASE only) | CAYMAN | 24 getters = 6×4 RELEASE only | `[HIGH/OBSERVED]` |
| Self-naming logs (`S:` / `P%i:`) | **NO** (zero, all engines) | CAYMAN | §2e | `[HIGH/OBSERVED]` |
| NCFW `DRAM+0xB0` algo table | **NO** (monolithic body) | CAYMAN | §4d; no `const16 0xB0` dispatch index | `[HIGH/OBSERVED — CARRIED]` |
| The dual fetch front-end (HW-Decode + "Sunda-mode") | **NO** (monolithic `fast_fetch`) | CAYMAN | §4f, [HW-Decode vs Sunda Dual Fetch](../firmware/seq/dual-fetch.md) §7 | `[HIGH/OBSERVED]` |

### 2b. The compute floor in one line

SUNDA POOL = the floor compute set — the 18 flat pool/dma kernels of §3a, with
**no** RNG, **no** dequant/MX, **no** sort, **no** conv-LUT, **no** seq-bounds,
**no** `0xf0` escape. `[HIGH/OBSERVED]`

### 2c. The collective floor — what SUNDA *keeps*

The single decisive collective delta is the on-chip `0xbf` SB2SB leg + the
transpose DGE kinds (both nc≥V3). SUNDA is **not** collective-incapable: it still
ships all nine collective pseudo-ops (`0xC7/C8/D9/DA/CB/D8/D5/C3/DB`), the
cross-die RDMA `ExtendedInst` legs (`op6/8/9` + `DMA_DIRECT2D/INDIRECT1D`
structs), P2P sendrecv (`0xCB`), the `EVT_SEM` 256-array, and the
ring/mesh/hier `algo_configs` regions. The collective-*reducible* dtype set is
the gen-invariant 6 (`{BF16, FP16, FP32R, FP8_E3/E4/E5}`). **SUNDA's missing
piece is the on-chip `0xbf` leg, not the RDMA primitives.** `[HIGH/OBSERVED for
the absence; the retained legs CARRIED from the collective survey]` See
[Cross-Gen Collective Support](master-capability-matrix.md) for the cross-gen
table.

### 2d. The `0xf0` bridge boundary

The `0xf0 = ExtendedInst` SEQ→Q7 escape is the POOL-exclusive dual-dispatch
bridge that makes POOL dual-core on CAYMAN+. It **arrives at CAYMAN**. SUNDA is
*still* dual-core (NX_POOL SEQ + Q7_POOL compute), but the cores couple through
the external-lib loader / direct-opcode `kernel_info_table` path (§3e), **not**
the `0xf0` escape — there is no `0xf0` row in SUNDA's table. `[HIGH/OBSERVED]`

### 2e. The observability floor — total, every engine

The SUNDA DEBUG DRAMs carry **zero** `S:` handler-name log strings on *every*
engine (ACT/DVE/PE/POOL/SP all 0; vs CAYMAN's ~41 ACT / 53 DVE / 24 PE / 142 SP),
**zero** `P%i:` kernel logs (Q7), **zero** `"Dispatch opcode=0x%x"`, and zero
compute-name strings (`Matmul`/`Activate`/`relu`/`gelu`/`dequant`/`xorwow` = 0
word-boundary hits). The classic `S:`-log handler set-diff method is *inapplicable*
on SUNDA for all engines. Set-diffing the four DEBUG DRAMs yields **62 strings
common to all four**; the *sole* engine-distinguishing string is DVE's
`sunda/seq/src/uarch.hpp:161` `0 && "not a supported tscr op"` (the tensor-scalar
arm). The SUNDA handler set is recoverable only from (i) the source-path
assertions, (ii) the EXTISA `kernel_info_table` (§3a), and (iii) the byte-distinct
IRAM/IVP datapaths. `[HIGH/OBSERVED]`

---

## 3. SUNDA-distinct / unique features (not just absences)

SUNDA carries genuine v2-distinct features later gens do *not* — features the
later, more general ISA either *absorbed* (the BF16 cluster) or *replaced* (the
static custom-op structs).

### 3a. The 18-entry `kernel_info_table` — the authoritative SUNDA kernel roster

Decoded byte-exact from the container device ELF (`kernel_info_table @VA
0x02000760`, file off `0xb260`, size `0x90` = 18 × 8 B, no header/terminator;
record `{u8 0; u8 0; u8 spec@+2; u8 opcode@+3; u32_le funcVA@+4}`, **all spec =
0, no `0xf0`**):

| idx | opcode | funcVA | kernel | idx | opcode | funcVA | kernel |
|---|---|---|---|---|---|---|---|
| 0 | `0xe7` | `0x01000748` | `pool_indirect_copy` | 9 | `0x41` | `0x01006398` | `pool_tensor_tensor_arith_op` |
| 1 | `0x74` | `0x01001904` | `pool_tensor_scalar_addr` | 10 | `0x7c` | `0x01007d30` | `pool_cross_lane_reduce_arith` |
| 2 | `0x67` | `0x010023c4` | `pool_pool_buffer_load` | 11 | `0x7d` | `0x01007d48` | `pool_cross_lane_reduce_bitvec` |
| 3 | `0x68` | `0x01002590` | `pool_gather` (`send_gather_request`) | 12 | `0x49` | `0x01007fc4` | `pool_memset` |
| 4 | `0x46` | `0x01002700` | `pool_copy` | 13 | `0x7a` | `0x010084dc` | `pool_load_pool_argument` |
| 5 | `0x47` | `0x01002844` | `pool_cast` | 14 | `0x79` | `0x01008520` | `pool_embedding_update` |
| 6 | `0xb8` | `0x01002f80` | `dma_memcopy` | 15 | `0x43` | `0x0100a0d8` | `pool_tensor_scalar_arith_op` |
| 7 | `0xbb` | `0x0100474c` | `dma_memcopy_indirect` | 16 | `0x44` | `0x0100a0d8` | `pool_tensor_scalar_arith` (op68 **alias** of op67) |
| 8 | `0x7e` | `0x01005e80` | `pool_iota` (`iota_kernel`) | 17 | `0x92` | `0x0100a118` | `pool_tensor_scalar_affine_select` |

One flat 18-entry table, all spec = 0, opcodes `0x41..0xe7`, **no `0xf0`** — vs
CAYMAN's multi-lib split with a 5-row `0xf0` spec sub-dispatch. The real JSON
manifest names 17 functions (it omits the op68 alias of op67); the JSON op-set is
a strict subset of the SO op-set (the lone SO-only opcode is the `0x44` alias).
The SUNDA base compute lives at the *v2 opcode numbers* (`0x41/0x43/0x44/0x49…`),
which the SUNDA→CAYMAN renumbering later changes. `[HIGH/OBSERVED]` Full carve in
[SUNDA arch5 EXTISA](../images/sunda-arch5-extisa.md).

### 3b. The SUNDA-only EXTISA kernels (the arch5 additions over the CAYMAN roster)

Three kernels in the SUNDA device ELF's `.xt.prop` (demangled C++ symbols) are
the arch5-new kernels not in CAYMAN's roster — they are the only kernels with
their own `.xt.prop` property sections (the others being `tensor_tensor_arith`
`0x41`):

| opcode | demangled symbol | kernel |
|---|---|---|
| `0x7e` (126) | `iota_kernel<true>()` / `iota_kernel<false>()` | `pool_iota` |
| `0x79` (121) | `embedding_update(embed_update_info*, ushort)` | `pool_embedding_update` |
| `0x68` (104) | `ic_util::send_gather_request(TPB_ADDR4, ushort*, uint, TPB_DTYPE, uint, bool)` | `pool_gather` |

These funcVAs land on real FLIX code; their per-step operand/dtype semantics are
*not* exhaustively decoded here (see §7). `decode_embedding_update` /
`embedding_update.hpp` are present in `INT` (22 string hits) — the SUNDA-new
kernel name is byte-confirmed in the host lib. `[HIGH/OBSERVED for presence +
opcode + symbol; per-kernel body decode LOW/OPEN]`

### 3c. The SUNDA BF16 cluster — the *old end* of the gen bracket

SUNDA is the **only** gen carrying a five-opcode, dtype-specialised,
**2×-throughput packed-BF16** fast-path cluster, *hard-removed at CAYMAN* once the
generic dtype dispatch could run BF16 (`0x6`) directly. The cluster is five ops
(`0x8e` is a non-BF16 hole), each flagged `// Y` (maintained) in SUNDA only, zero
hits in any later gen:

| opcode | enum identifier | struct | `sunda/common.h` |
|---|---|---|---|
| `0x8a` | `TENSOR_TENSOR_ADD_BF16` | `S3S3D3_TT` | `:223` |
| `0x8b` | `TENSOR_TENSOR_MULT_BF16` | `S3S3D3_TT` | `:224` |
| `0x8c` | `TENSOR_REDUCE_ADD_BF16` | `S4D4_TR` | `:225` |
| `0x8d` | `TENSOR_REDUCE_MAX_BF16` | `S4D4_TR` | `:226` |
| `0x8f` | `TENSOR_TENSOR_SUB_BF16` | `S3S3D3_TT` | `:228` |

> **GOTCHA — `0x8e` is *not* a BF16 op.** Byte `0x8e` = `BATCH_NORM_PARAM_LOAD2`
> (`sunda/common.h:227`), maintained in *all four* gens. The BF16 cluster is the
> five *non-contiguous* bytes `{0x8a,0x8b,0x8c,0x8d,0x8f}`. After SUNDA those five
> bytes go *unassigned* (not recycled) in every later enum. `[HIGH/OBSERVED]`

The mechanism: the "2×" packing reads source patterns as `Dtype::UINT32` (a packed
BF16 pair per 4-byte lane), requires the inner dim even + unit-stride
(`num_elem[0] % 2 == 0`, `step_elem[0] == 1`), and pins both inputs to
`Dtype::BFLOAT16` (`0x6`):

```c
/* SUNDA BF16 TT trio (0x8a/0x8b/0x8f) on S3S3D3_TT. [HIGH/OBSERVED — header semantics]
 * src patterns read as UINT32 = packed BF16 pair per lane -> 2x lanes.
 * The AluOp field is PINNED to match the opcode (has_valid_tensor_tensor_bf16_op). */
for (e = 0; e < element_count; ++e) {          /* element_count counts BF16, 2 per 32b lane */
    bf16 a = src0_bf16[e], b = src1_bf16[e];
    switch (opcode) {
        case TensorTensorAddBf16:  dst[e] = a + b; break;  /* op == AluOp::Add  (0x04) */
        case TensorTensorMultBf16: dst[e] = a * b; break;  /* op == AluOp::Mult (0x06) */
        case TensorTensorSubBf16:  dst[e] = a - b; break;  /* op == AluOp::Sub  (0x05) */
    }
}

/* SUNDA BF16 reduce pair (0x8c/0x8d) on S4D4_TR. op_dim (TENSOR_SUBDIM) selects X/XY/XYZ/XYZW;
 * 'negated' (S4D4_TR+35) optional negate; output may be FP32R (DtypeAllowFP32R::True). */
if (opcode == TensorReduceAddBf16)  dst[p] = sum_over(op_dim, src_bf16[...]);
if (opcode == TensorReduceMaxBf16)  dst[p] = max_over(op_dim, src_bf16[...]);
```

The retirement is *measurable in the struct member counts*: `S3S3D3_TT` shrinks
SUNDA **8 → CAYMAN 5** (the three dropped members are exactly the TT BF16 trio
`{0x8a,0x8b,0x8f}`); `S4D4_TR` shrinks SUNDA **13 → CAYMAN 11** (the two dropped
are `0x8c`/`0x8d`). All four structs compile-verify to 64 B. This is
*specialisation → generalisation*: SUNDA solved a throughput problem with
per-opcode BF16 ops; the ISA then generalised dtype handling and retired them
cleanly. `[HIGH/OBSERVED for the opcode set + struct shrink; the 2× micro-op
sequence is header-semantics, the SUNDA RELEASE image is string-stripped so the
device body is not self-named]` Full decode in
[INT_WIDE & BF16 — the opcode extremes](../firmware/kernels/intwide-bf16-extremes.md).

### 3d. The SUNDA-only dual-ALU `TensorScalarPtr` (`0x87`/`0x88`)

SUNDA is the *upper* extreme of the `TensorScalar*` deprecation spectrum: it is
the **only** gen defining the dual-ALU `TensorScalarPtr` pair, dropped at CAYMAN:

| opcode | name | `sunda/common.h` | maint | struct |
|---|---|---|---|---|
| `0x87` (135) | `TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH` | `:221` | `// n, ucode/kaenadve exists, not maintained/used` | `S4D4_TSM` |
| `0x88` (136) | `TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC` | `:222` | `// n` (header-**disabled**) | `S4D4_TSM` |

> **GOTCHA — "dual" = dual *ALU op* per instruction, not dual pointer.** It
> merges up to four regular `TensorScalarPtr` ops into one 4-D kernel.
> `W = src_mem_pattern.num_elem[3]` is the number of scalar **pairs**, `1 ≤ W ≤ 4`.
> The struct (`S4D4_TSM`, 64 B) has a *single* `op` field @36 and *zero* immediate
> fields — it is shared byte-for-byte with the singular `PtrMulti` `0x4F`/`0x5F`.
> `[HIGH/OBSERVED]`

```c
/* SUNDA-only dual TensorScalarPtr (0x87). is_valid_tensor_scalar_ptr_multi_dual().
 * [HIGH/OBSERVED — header semantics; no enabled firmware worker (header-only ceiling)] */
void tensor_scalar_ptr_multi_dual(const S4D4_TSM *i,
                                  const scalar_pair_t pairs[/*W*/],  /* from ImmLd flops */
                                  AluOp op1_fixed /* == Multiply, from DVE_config */) {
    const uint8_t W   = i->src_mem_pattern.num_elem[3];   /* 1..4 (has_valid_src_slices_tsm_dual) */
    const AluOp   op0 = i->op;                             /* @36, programmable */
    for (uint8_t w = 0; w < W; ++w)                        /* pair swaps per XYZ round */
        for_each_zyx(i->src_mem_pattern, w) {
            T src = load(i->in_dtype, &SRC[w][z][y][x]);
            T t0  = alu_apply(op0,       src, pairs[w].s0, i->reverse_operands);  /* TensorScalar #1 */
            T out = alu_apply(op1_fixed, t0,  pairs[w].s1, i->reverse_operands);  /* * pairs[w][1] */
            store(i->out_dtype, &DST[w][z][y][x], out);
        }
}
```

> **QUIRK — the second ALU op is *not* in the instruction.** `op1` is **hard-coded
> to `AluOp::Multiply` (`0x06`)** in `DVE_config`, not encoded in the struct
> (`sunda/s4d4_tsm.h:52`). The single-`op` 64-B struct serves *both* the singular
> (`op1 = bypass 0x00`) and the dual (`op1 = config-multiply 0x06`) forms with no
> byte-layout change — the "second op" lives at engine-config level. The BITVEC
> form `0x88` is *disabled even on SUNDA* ("no sensible default 2nd op for bitvec
> to hard-code yet … very tight on uop table space in DVE"). `[HIGH/OBSERVED]`

> **CORRECTION — `0x87` is *not* recycled as a semaphore opcode.** SUNDA carries
> `0x87` in *two distinct enums simultaneously*: `OPCODE_*_DUAL_ARITH = 0x87`
> (`common.h:221`) and `UPDATE_MODE_SEM_SUB_REG_READ = 0x87` (`common.h:350`, and
> stable through maverick). On NC-v3+ only the *opcode* name drops; the
> gen-stable `UPDATE_MODE` constant is untouched. Separate decode contexts.
> `[HIGH/OBSERVED]`

Firmware reality: `"Dual"` strings = **0** hits in `INT` — `0x87`/`0x88` are
header-only (no enabled worker even on SUNDA), and they leave a *stale* `S4D4_TSM`
`struct2opcode` JSON binding on cayman+ (listed, but the enum names are gone).
Full decode in
[SUNDA dual TensorScalarPtr](../firmware/kernels/sunda-dual-tensorscalarptr.md).

### 3e. The static `CUSTOM_OP_HEADER/PAYLOAD` structs — the *only* removals in the chain

SUNDA's `struct2opcode` (`instruction_mapping.json`) carries
`NEURON_ISA_TPB_CUSTOM_OP_HEADER_STRUCT` + `…_CUSTOM_OP_PAYLOAD_STRUCT` — the
static custom-op header/payload pair (the shipped headers
`aws_neuron_isa_tpb_custom_op_{header,payload}.h` are present in the SUNDA tree).
**CAYMAN dropped both.** These are the *only* struct removals in the entire
SUNDA→…→MAVERICK chain (every other transition is purely additive); CAYMAN
replaced the static pair with the `kernel_info_table` / EXTISA mechanism. SUNDA
`struct2opcode` length = 89, CAYMAN = 99. A genuine SUNDA-unique *survival*, not
an absence. `[HIGH/OBSERVED — CARRIED from the arch-isa header diff]`

### 3f. The arch5 single-data-band device ELF

The SUNDA EXTISA device ELF (ELF32/LE, `EM_XTENSA`, ET_EXEC, **entry
`0x010000c8`**, e_flags `0x300`, 17 section headers, image size **`0xd308`**
exactly — `e_shoff 0xd060 + 17·0x28 = 0xd308 = file size`) has **no `.rodata`**
and **no `.globstruct`** — everything collapses into a single `.data` band
@`0x02000080` (size `0x6dc`). This is *unlike* CAYMAN (`.rodata @0x02000000` +
`.globstruct @0x02000408`). Layout highlights:

| section | VA | off | size | note |
|---|---|---|---|---|
| `.text` | `0x01000000` | `0x100` | `0xa9be` | FLIX/VLIW Q7 code (2428 bundles, 81 `entry`, 98 `retw`) |
| `.data` | `0x02000080` | `0xab80` | `0x6dc` | **single data band** |
| `kernel_info_table` | `0x02000760` | `0xb260` | `0x90` | the 18 entries (§3a) |
| `.rela.got` | `0x030000c8` | `0xb3c8` | **`0x14dc`** | **445 R_XTENSA relocs** (CAYMAN: 240 / `0xb40`) |

SUNDA's `.rela.got` is the *largest* in the catalog (445 relocs) because SUNDA_0
is the largest single EXTISA image (`0xd308` vs CAYMAN `0xa260`) — the monolithic
`all.stripped.so`. The 445-reloc table is the const16-VA fixup table the host
prelink applies at device-staging time. Three VA windows: code `0x010xxxxx` (RE),
data `0x020xxxxx` (**RWE** — executable data, no W^X), dyn `0x030xxxxx` (WE).
`[HIGH/OBSERVED]` Full anatomy in
[SUNDA arch5 EXTISA](../images/sunda-arch5-extisa.md).

### 3g. The external-lib loader shell Q7 + the sole non-zero EXTRAM

SUNDA's `Q7_POOL` flat IRAM is just **`0x42d0`** (17 KB) — dramatically smaller
than CAYMAN's Q7 PERF (`0x16360`, 90 KB) — because SUNDA's Q7 firmware is the
*dispatch + external-lib-loader shell*; the kernels are loaded at runtime from the
`EXT` container. The Q7 DEBUG DRAM carries the loader string set
(`load_external_libraries_impl` / `external_lib_loader.hpp` / `call_start_symbol`
/ `xtensa_unload` / `load_from_nx_addr` / `.kernel_info_table` /
`dispatch_wrapper.hpp`), source tree `sunda/pool/src/`. So SUNDA's *base* Q7 build
**is** the external-lib-loading kind — what CAYMAN factored into a separate DKL
variant is the SUNDA default; there is no separate SUNDA DKL image. The full load
chain:

```
shell -> nrtucode_get_ext_isa(coretype=6, lib=0)
      -> EXT's SUNDA handler -> sunda_libs[0]
      -> SO_get   (VA 0x921660, size 0xd308)     /* the device ELF blob */
      -> JSON_get (VA 0x920fa0, size 0x6c0)       /* the manifest */
      -> host prelink applies the 445 .rela.got relocs -> device-stage
      -> on-device dispatch_wrapper linear-scans the 18-entry kernel_info_table
         (spec always 0; no 0xf0 escape).
```

> **QUIRK — the sole non-zero EXTRAM in the whole catalog.**
> `SUNDA_Q7_POOL_RELEASE_EXTRAM` is **`0x1b40`** bytes — the *only* non-zero EXTRAM
> in the entire 386-getter catalog. Its head bytes `36 61 00` decode as
> `entry a1, 48` (a *function prologue*, not a reset vector); the band is real
> windowed-ABI FLIX code (19 `entry` / 55 `retw`, exit 0), consistent with an
> EXTRAM-resident loader-stub / aux kernel staged alongside the loader string set.
> No other gen's POOL ships a non-zero EXTRAM. `[HIGH/OBSERVED for bytes+decode;
> loader-stub role INFERRED-HIGH]`

### 3h. The divergent activation PWP header (`dbdca26b…` vs cayman+ `8f6f5f49…`)

SUNDA is the *only* gen whose activation PWP header block is **not** byte-identical
to the cayman+ block (which is stable v3→v5). SUNDA sha256 prefix `dbdca26b…` vs
cayman+ `8f6f5f49…`. The four shipped header structs are
`aws_hal_stpb_act_{cam,profile,control,bucket}_entry_t` (CAM 32 B / PROFILE 128 B
/ CONTROL 32 B / BUCKET 32 B); the divergence is **3 fields, all within the same
sizes** (compile-verified equal-size):

| # | field | SUNDA (v2) | cayman+ |
|---|---|---|---|
| (i) | CAM mask byte-order | `{func_id_mask@39:32, opcode_mask@47:40}` | **swapped** → `{opcode_mask@39:32, func_id_mask@47:40}` |
| (ii) | PROFILE FMA-bypass | `rsvd33:5 @439:435` (no bit) | **adds** `bias_scale_fma_bypass:1 @435` |
| (iii) | PROFILE batch-norm | `unused1:14 @767:754` (no bit) | **adds** `batchnorm_accumulator_rd:1 @754` |

So the divergence is exactly **the CAM mask byte-swap + 2 fewer PROFILE config
bits** (no FMA-bypass, no bn-accum-read). The core PWL machine (CONTROL
index-extract `act_tbl_base:11/extract_lsb:5/extract_size:4`; BUCKET cubic
`d0..d3,x0`) is byte-stable v2→v5.

> **GOTCHA — a reimplementer keying off cayman+ `8f6f5f49` mis-reads SUNDA's CAM
> masks.** The SUNDA CAM puts `func_id_mask` *first* (low byte); a cayman+-shaped
> reader will read the opcode/func_id masks swapped. Also note: the activation CAM
> (32 B `aws_hal_stpb_act_cam_entry_t`) is *not* the PROF_CAM (16 B HW-decode
> profiler, `8fd7e422`), which SUNDA omits entirely. `[HIGH/OBSERVED — CARRIED
> from the SP-remaining image survey]`

---

## 4. The SUNDA topology (SoC / engine layout)

### 4a. The full five-NX + POOL-Q7 engine set — not engine-count-reduced

`nm INT` lists exactly **24 SUNDA getters** = six engine-classes × four regions,
RELEASE-only:

```
NX_ACT  NX_DVE  NX_PE  NX_POOL  NX_SP        (the 5 NX sequencer engines)
Q7_POOL                                       (the POOL-paired Q7 compute core)
```

The ISA engine enum (gen-stable, `common.h:139-146`): `PE=0, ACT=1, POOL=2,
DVE=3, TPB_SP=4, TOP_SP=5`. POOL is the *sole* dual-core engine (NX_POOL SEQ +
Q7_POOL compute) — the reason only POOL ships a second core. SUNDA SP = `TPB_SP`
(engine_idx 4), distinct from the standalone `TOP_SP` (5). `engine_idx` is
runtime-computed from base address (why all five NX engines share the identical
reset + boot trampoline). The per-engine compute role (ACT=activation-LUT,
DVE=bn-scan-tscr, PE=matmul-array, POOL=pool-dma, SP=sync-control) is present as
un-named IRAM code (distinct-IVP op counts: ACT **212**, DVE **232** (highest),
PE **222**, SP **209**), inferred from the CAYMAN baselines + the IVP datapaths +
the module spine — *not* re-verifiable by handler name (the observability floor).
`[HIGH/OBSERVED for the engine set + IVP counts; per-engine role INFERRED-HIGH]`

### 4b. Per-engine image sizes + the engine order

| engine | IRAM | DRAM | DEBUG DRAM | EXTRAM |
|---|---|---|---|---|
| NX_ACT | `0x8f20` | `0x2120` | `0x3300` | 0 |
| NX_DVE | `0xbab0` | `0x2660` | `0x3800` | 0 |
| NX_PE | `0xb3d0` | `0x2300` | `0x3360` | 0 |
| NX_POOL | `0xd040` | `0x2730` | — | 0 |
| NX_SP | `0xb450` | `0x2220` | `0x3390` | 0 |
| Q7_POOL | `0x42d0` (loader shell) | `0xa540` | — | **`0x1b40`** (sole non-zero) |

The engines are laid out **byte-contiguous** (each blob's end == the next blob's
start), order `ACT → DVE → PE → NX_POOL → NX_SP → Q7_POOL`. Adjacency closes
exactly (`0x055f0 + 0x8f20 = 0x0e510`; `0x469d0 + 0x2220 = 0x48bf0`). **NX_SP is
interleaved between the two POOL cores.** `[HIGH/OBSERVED]` The eight non-POOL
engine carves are byte-identical to their `.a` RELEASE members (full shas in
[SUNDA SP + remaining engines](../images/sunda-sp-remaining.md)); the POOL/Q7
carves likewise (see [SUNDA POOL](../images/sunda-pool.md)).

### 4c. The boot / reset signature

The carved `img_SUNDA_NX_POOL_RELEASE_IRAM` from `A` (sha256 `0ef85167…f06b68`,
matching the page anchor) decodes its reset/enter_run trampoline under
`OBJD` (`XTENSA_CORE=ncore2gp`):

```
0x000:  06 76 00   j 0x1dc        ; primary reset -> boot        (== CAYMAN)
0x006:  86 77 00   j 0x1e8        ; secondary    -> halt
0x1dc:  04 00 00   const16 a0,0
0x1df:  04 94 00   const16 a0,148 ; *** 0x94  (CAYMAN here: 0x90, +0x4)
0x1e2:  a0 00 00   jx a0          ; -> enter_run @0x94
0x1e8:  00 52 00   halt 0
```

| core | SUNDA reset | CAYMAN reset | SUNDA enter_run | CAYMAN enter_run | Δ |
|---|---|---|---|---|---|
| NX (all 5) | `j 0x1dc` | `j 0x1dc` (==) | `@0x94` | `@0x90` | entry **+0x4** |
| Q7_POOL | `j 0x220` | `j 0x200` | `@0x94` | `@0x90` | reset **+0x20**, entry +0x4 |

Other gen-invariant boot bytes: DRAM `.globstruct` magic `0x6099cb34` (==
CAYMAN), DRAM `@0x18` = `4 × 0x00001000` dispatcher-state init block (==).

> **QUIRK — the v2 window base is `0x00100000`, the CAYMAN-family is
> `0x04000000`.** SUNDA POOL's control window sits at base `0x00100000`
> (`start_ctrl 0x00100010`, `run_state 0x00100014`) — the v2 floor. The
> CAYMAN-family window base is `0x04000000`. The v2 boot is a *relocated variant*
> of the same trampoline shape, not a new boot path: same `06 76` reset value,
> `enter_run @0x94` (vs `0x90`), Q7 reset `+0x20`, window `0x00100000`.
> `[HIGH/OBSERVED for reset/enter_run; window base
> CARRIED from the POOL image survey]`

### 4d. The NCFW management-core topology (the SUNDA-distinct fabric)

SUNDA's NCFW (the separate Xtensa-LX management core) is the smaller, earlier
fabric: 43 KB IRAM (≈2.2× the v3 19 KB but a *single monolithic body* of 23
functions, biggest @`0x5f60`/`0x1760` B), DRAM `0x36c0`, **4 soc_addr CSRs**
(v3 per-die 20), **11 descriptor records** (v3 16), a **50-event mesh tape** (v3
108), and **no `DRAM+0xB0` algo dispatch table** (the `+0xB0` region holds
descriptor *data*, not IRAM code addresses). The `(A)` command vector @`DRAM+0x00`
(3 trampolines + 1 default, `{0x1bb3,0x1bcf,0x1be3,0x1bb3}`) is *common* to all
gens; only the algorithm-dispatch realization differs (SUNDA monolithic;
CAYMAN+ table-at-`DRAM+0xB0`). SUNDA's `soc_addr` range is `0x0fff_xxxx` (4 CSRs)
vs the v3/v4 per-die `0x00xx_0270_0000` block — the reduced counts read as a
smaller, earlier SoC fabric (fewer dies, a shorter mesh-reduction tape).
`[HIGH/OBSERVED for the counts — CARRIED from the NCFW survey; "smaller fabric"
INFERRED-HIGH]`

> **NOTE — the v2 die/HBM geometry is LOW/OPEN.** The exact SUNDA die count + HBM
> stack geometry + the coretype→silicon-part binding are *not* in this corpus.
> SUNDA is in the Cayman-class DMA/transport/HBM family (SDMA, SerDes-d2d, the
> 57-bit SoC address); its SDMA trigger count is 239 (CAYMAN 254). The reduced
> 4-CSR fabric *infers* a smaller die, but the precise numbers are not observable
> here. Do not fabricate them. `[LOW/OPEN]`

### 4e. The base-subtraction "Sunda-mode" NX dispatch — the origin

Every SUNDA NX engine (PE/ACT/POOL/DVE/SP) runs *exactly one* `sub a2,a2,a3`
(register-base subtraction) feeding const16-base `addx4`-indexed jumps with
per-segment raw-compare leaves; **zero** `addi a2,a2,-65` (the `'A'`−0x41 ASCII
normalization CAYMAN DVE/POOL use) and **zero** `movi a3,177` (the 178-entry table
bound):

| engine | `sub a2,a2,a3` | `addi a2,a2,-65` | `movi a3,177` | `addx4` | `jx` |
|---|---|---|---|---|---|
| ACT | 1 | 0 | 0 | 41 | 12 |
| DVE | 1 | 0 | 0 | 28 | 13 |
| PE | 1 | 0 | 0 | 29 | 12 |
| SP | 1 | 0 | 0 | 28 | 12 |

This is the *origin* of the base-subtraction segmented dispatch that
MARIANA/MAVERICK SP inherit byte-exact and that the later runtime "Sunda-mode"
fallback is *named after*. There is no `0xf0` ExtendedInst NX→Q7 escape on either
SUNDA core — the Q7 is reached by the external-lib loader / direct-opcode
`kernel_info_table` path. `[HIGH/OBSERVED for the sub/addi/movi counts; the
unified base-subtraction reading INFERRED-HIGH; the full per-opcode hub is
FLIX-desynced under the linear sweep, MED]`

### 4f. "Sunda-mode" (runtime) ≠ SUNDA (generation)

The CAYMAN-and-later NX firmware ships *two* co-resident fetch/decode FSMs — a
HW-Decode FIFO path and a legacy software-fetch **"Sunda-mode"** fallback —
selected by a boot flag. **SUNDA(v2) has neither.** The SUNDA SEQ image is
*monolithic*: no direct-indexed dispatch table, no dual mode, and it names its
fetch/surprises functions plainly (`fast_fetch` / `handle_surprises`, **1** each)
rather than `sunda_fetch` / `sunda_handle_surprises`. The CAYMAN+ build *renamed*
the legacy path `sunda_*` precisely when it added the HW-decode path, to mark the
retained legacy fetch. The SUNDA negative control (carved `SUNDA_NX_POOL_DEBUG`,
59,600 B):

| probe | SUNDA result |
|---|---|
| `"NX in Sunda mode"` / `"NX in HW Decode mode"` | 0 / 0 |
| `"Seq Loop, iter"` / `"RTL_PC_check"` | 0 / 0 |
| `sunda_fetch` / `sunda_handle_surprises` | 0 / 0 (uses `fast_fetch`/`handle_surprises`, 1 each) |
| `addi a2,a2,-65` / `addi a2,a2,-48` dispatch sites | 0 / 0 |
| `const16 *,0xadc` (HW-decode table base) | 0 |

> **CORRECTION — `"Sunda-mode"` is a CAYMAN+ runtime fallback *name*, not the v2
> silicon.** In `INT` the runtime-mode strings `"NX in Sunda mode"` (16) and
> `sunda_fetch` (48) are present — but they belong to the *CAYMAN+* runtime
> software-fetch fallback, **not** the SUNDA generation, whose own image has
> neither construct. Conflating the two inverts every per-gen claim. `[HIGH/OBSERVED]`
> Full FSM contrast + the per-gen presence proof in
> [HW-Decode vs Sunda Dual Fetch](../firmware/seq/dual-fetch.md).

---

## 5. The baseline → v3 role (how SUNDA establishes the floor CAYMAN extends)

SUNDA opens the bottom of the GPSIMD matrix. The SUNDA→CAYMAN(v3) edge is the
first *and largest* batch of adds in the whole line. `[HIGH/OBSERVED for each delta]`

**The SUNDA(v2) → CAYMAN(v3) transition (the lattice edge):**

- **+ collective:** `SB2SB_COLLECTIVE 0xbf` (on-chip S3D3 reduce-copy) +
  `gather_xpose 0xf1` / `DmaTranspose 0xbd` DGE kinds.
- **+ bridge:** the `0xf0 ExtendedInst` NX→Q7 dual-dispatch escape (POOL goes
  truly dual-core via the escape).
- **+ ISA:** the `+27` INT/UINT ALU_OP family (ALU_OP 33→60); the
  transpose/sparse-matmul struct family (`struct2opcode` 89→99; OPCODE 145→150);
  `CONV_LUT_LOAD 0xe4` (`cayman/common.h:294`).
- **+ RNG:** SW Xorwow (`RAND_ALGORITHM` gains `XORWOW = 3`, `cayman/common.h:898`).
- **+ PROF:** the shared 47-record CAM + 8 KiB table.
- **+ NCFW:** the `DRAM+0xB0` 12-entry table dispatch (vs SUNDA's monolithic body)
  + 108-event mesh (vs 50) + 20 per-die soc_addr CSRs (vs 4) + 16 descriptors
  (vs 11).
- **+ packaging:** the multi-flavor DEBUG/PERF/TEST image model + the per-handler
  `S:`/`P%i:` logs (the observability the SUNDA floor lacks) + the 4-lib EXTISA
  split + the in-library EXTISA getters (vs SUNDA's standalone-only).
- **+ front-end:** the normalized indexed SEQ dispatch table (the `addi-0x41`
  form) *beside* the retained base-subtraction "Sunda-mode" fallback — the dual
  front-end arrives at CAYMAN; SUNDA is the pre-dual-mode monolithic baseline.
- **+ reset family:** the `06 76 → 06 7d` `+0x1c` NX-only reset-shift family
  (SUNDA is the prior point: same `06 76` reset, `enter_run @0x94` instead of
  `0x90`) and the `0x04000000` window base (vs SUNDA `0x00100000`).
- **− (the *only* removals in the whole chain):** `CUSTOM_OP_HEADER/PAYLOAD` (the
  static custom-op pair, replaced by `kernel_info_table`/EXTISA) **and** the
  SUNDA-only fast paths — the BF16 cluster (`0x8a/0x8b/0x8c/0x8d/0x8f`, absorbed
  into generic dtype dispatch) and the dual `TensorScalarPtr` (`0x87/0x88`).
- **DTYPE unchanged** (16 == 16; SUNDA and CAYMAN share the 16-code base floor —
  `INVALID/INT*/UINT*/FP16/BFLOAT16/FP32/FP32R/FP8_E3/E4/E5`; FP4/CPTC/MX dtypes
  arrive at MARIANA).

**The v2-floor thesis** `[synthesis INFERRED-HIGH; every constituent fact
OBSERVED]`: SUNDA is the *same* five-NX + POOL-Q7 structure as CAYMAN, on the
*same* Cairo / NX1.1.4 core, with the *same* base pool/tensor/dma kernel set and
the *same* reset/globstruct shape — but it is the *floor* on every additive axis:
the packaging floor (single RELEASE flavor, no PROF/DKL/in-lib-EXTISA), the
compute floor (no RNG/dequant/MX/sort/conv-LUT/seq-bounds), the collective floor
(no `0xbf`/gather_xpose), the dispatch floor (monolithic NCFW + base-subtraction
NX dispatch, no `0xf0` bridge), the observability floor (no self-naming logs) —
plus a thin shell of v2-distinct survivals the later, more general ISA pruned.
Everything CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK add is a superset over this floor,
with the per-subsystem partial-order exceptions the
[Master Capability Matrix](master-capability-matrix.md) catalogues. **SUNDA is the
partial-order bottom.**

---

## 6. Adversarial self-verification

Five strongest claims, each re-challenged against the shipped binaries/headers:

| # | Claim | Challenge | Verdict |
|---|---|---|---|
| 1 | **arch_id 5 / coretype 6** | `NRTUCODE_CORE_SUNDA` enum in `INT`; `get_ext_isa` case index; NC-v2 banner | **HOLDS.** `NRTUCODE_CORE_SUNDA_NX_POOL` first of 5 in `INT`; `get_ext_isa` index `coretype−6 == 0` → `sunda_libs @0x9b8f80`; `common.h:3` "ISA header for NC-v2". OBSERVED. |
| 2 | **RELEASE-only 8-carve = `.a` identity** | `ar t A` SUNDA members; carve a RELEASE engine + sha | **HOLDS.** 48 SUNDA `.a` members, 24 RELEASE (6×4); carved `NX_POOL_RELEASE_IRAM` sha `0ef85167…f06b68` = page prefix, byte-identical. OBSERVED. |
| 3 | **Absence floor (0-hit)** | `rg` xorwow/SB2SB/CONV_LUT across SUNDA arch_isa | **HOLDS, refined.** `xorwow` 0 (CAYMAN 5); `SB2SB_COLLECTIVE` 0 (CAYMAN 1); `CONV_LUT` 0 (CAYMAN-first `:294`); no `s3d3_collective.h`/`gather_xpose.h`. **Refinement:** `0x7b`/`0xf0` *declared* in the shared header (`:211`/`:297`) but image/`kernel_info`-absent — absence is body-level (§2 CORRECTION). OBSERVED. |
| 4 | **enter_run @0x94 / window 0x00100000** | native objdump of carved IRAM trampoline | **HOLDS.** `0x1df: const16 a0,148` = `0x94` (CAYMAN `0x90`); reset `j 0x1dc`; window base `0x00100000` per the POOL survey. OBSERVED. |
| 5 | **Activation-header divergence** | the two shas + the 3-field diff sizes | **HOLDS.** SUNDA `dbdca26b…` vs cayman+ `8f6f5f49…`; CAM mask byte-swap + 2 fewer PROFILE bits (FMA-bypass, bn-accum-read), all same-size structs. OBSERVED (CARRIED from the SP survey). |

---

## 7. Open items

- **O-1 — the SUNDA-unique kernels are not deep-decoded.** `iota_kernel` /
  `embedding_update` / `send_gather_request` are confirmed by opcode + funcVA +
  `.xt.prop` demangled symbol, and the funcVAs land on real FLIX code, but the
  per-kernel operand/dtype semantics + FLIX body decode are not exhaustively
  traced. `[LOW/OPEN]`
- **O-2 — the FLIX-desync dispatch frontier.** The exact per-opcode SUNDA NX
  dispatch chain (the `sub a2,a2,a3` base-subtraction hub) is FLIX-desynced under
  the ncore2gp linear sweep; the `sub/addi/movi` counts are OBSERVED, the full
  per-opcode hub is not linearly recovered. A small hidden indexed sub-table
  cannot be 100% excluded. `[MED]`
- **O-3 — the v2 die/HBM geometry + silicon-part binding.** Not in this corpus
  (§4d NOTE). The reduced 4-soc_addr-CSR fabric *infers* a smaller die; the
  precise numbers + the coretype→part binding are LOW/OPEN — do not fabricate
  trn/inf bindings. `[LOW/OPEN]`
- **O-4 — the NCFW LX-core schedule.** The SUNDA NCFW management core is
  Xtensa-LX with no shipped disassembler config; the per-(world-size,topology)
  leg schedule + the monolithic case bodies are FLIX-corrupted. The dispatch
  *spine* (get_image selector, the `(A)` vector, the absence of `DRAM+0xB0`, the
  50-event mesh) is HIGH/OBSERVED; the case bodies are MED. `[MED]`
- **O-5 — the RELEASE-vs-DEBUG runtime path.** Which silicon part / runtime path
  loads RELEASE vs the (existing-in-`.a`) SUNDA DEBUG build is not determinable
  from these binaries. `[LOW]`

---

## See also

- [SUNDA POOL image](../images/sunda-pool.md) — the v2-baseline POOL diff (NX_POOL
  SEQ + Q7_POOL shell, the boot bytes, the EXTRAM).
- [SUNDA SP + remaining engines](../images/sunda-sp-remaining.md) — the full
  five-engine carve set (eight byte-identical `.a` members), the dispatch counts,
  the activation-header divergence.
- [SUNDA arch5 EXTISA](../images/sunda-arch5-extisa.md) — the standalone
  `libnrtucode_extisa.so` container + the arch5 single-data-band device ELF + the
  18-entry `kernel_info_table`.
- [INT_WIDE & BF16 opcode extremes](../firmware/kernels/intwide-bf16-extremes.md)
  — the SUNDA BF16 cluster (the old end of the gen bracket).
- [SUNDA dual TensorScalarPtr](../firmware/kernels/sunda-dual-tensorscalarptr.md)
  — the SUNDA-only dual-ALU `0x87`/`0x88` (and the all-gen-deprecated `0x44`/`0x54`).
- [HW-Decode vs Sunda Dual Fetch](../firmware/seq/dual-fetch.md) — the proof that
  "Sunda-mode" is a CAYMAN+ runtime fallback, not the SUNDA generation.
- [Codename ↔ Generation Map](codename-generation-map.md) — the `(arch_id,
  coretype)` enum proof + the 32-entry `get_ext_isa` jump table.
- [Master Capability Matrix](master-capability-matrix.md) — the cross-gen
  per-subsystem feature lattice this page sits at the bottom of.
- [The Confidence & Walls Model](../reference/confidence-model.md) — the
  OBSERVED/INFERRED/CARRIED × HIGH/MED/LOW tagging used throughout.
