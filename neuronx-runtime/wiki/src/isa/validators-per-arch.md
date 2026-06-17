# Per-Arch Instruction Validators (SUNDA / CAYMAN / MARIANA Deltas)

> *All addresses, offsets, struct ordinals, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce` (runtime-lib **2.31.24.0-0b044f4ce**; `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). `.text` VMA equals file offset for the cited ranges; the per-arch validator bands are all in `.text`, so every band/validator address below is a direct file offset. Source-TU attribution is from DWARF `addr2line`; function addresses/sizes/caller-counts from the function table (`functions.json`); the dispatcher jump tables from `switches.json`; struct/enum layouts from DWARF (`structures.json`, `enums.json`). Other versions will differ.*

## Abstract

The [validator architecture](validator-architecture.md) page documents *one* validator forest — the silent `is_valid_<op>` gate, its `dbg_is_valid_<op>` twin, the shared `has_valid_neuron_header`/`has_valid_neuron_events` preamble, and the `NEURON_ISA_TPB_DEBUG_BOOL` return ABI. This page documents the fact that **that entire forest exists three times over**. `libnrt.so` carries a complete, independent copy of the validator library for each of the three NeuronCore silicon generations it supports — **SUNDA** (arch 2, ISA-v2), **CAYMAN** (arch 3, ISA-v3), and **MARIANA** (arch 4, ISA-v4) — each occupying its own contiguous `.text` band of hundreds of kilobytes, each emitted from its own per-arch copy of the ISA-spec header. The three copies are not a runtime polymorphism over one body; they are three separately-compiled bodies, selected statically per record by the arch-versioned entry `ib_is_valid_engine_instruction_v{2,3,4}` (the `tdrv_arch_ops` slot `final_inst_validity_check`, [runtime/tdrv-arch-ops §4.3](../runtime/tdrv-arch-ops.md)). This is the single most expensive structural decision in the validation layer, and the one a reimplementer most needs to understand before deciding whether to replicate it.

The triplication is **generated, not hand-maintained**. DWARF `addr2line` attributes every validator in the SUNDA band to `/opt/amazon/include/neuron_sunda_arch_isa/tpb/aws_neuron_isa_tpb_assert.h`, every validator in the CAYMAN band to the identical path under `neuron_cayman_arch_isa/`, and every validator in the MARIANA band to `neuron_mariana_arch_isa/`. The three headers are the *same generated file* parameterized by one arch token: for any opcode, the SUNDA `is_valid_<op>`, the CAYMAN `is_valid_<op>_0`, and the MARIANA `is_valid_<op>_1` are the same predicate tree compiled against three different sets of legality constants (address apertures, quadrant geometry, active-channel windows, the dtype set, and the small handful of per-generation opcodes). The IDA `_0`/`_1` suffixes and `.constprop.0`/`.part.0` decorations are GCC clone/constprop artifacts, not part of the C identifier; `nm` shows the clean symbol. The cost is paid in `.text`: the same `dbg_is_valid_dtype` body is compiled three times, the same `tensor4d_valid` is compiled three times, and the giant silent dispatcher exists as `is_valid_neuron_instruction @0x2f2350` (17 952 B, SUNDA), `is_valid_neuron_isa_instruction @0x3a60b0` (16 606 B, CAYMAN), and a third copy inlined into `ib_is_valid_engine_instruction_v4 @0x43f810` (16 087 B, MARIANA).

This page treats the triplicated space by its **two axes** — *opcode-class* × *arch* — and never as three per-arch dumps. The roster (§2) lists each validator *family* once, with an arch-coverage column, because the families are shared; the delta table (§3) is the centerpiece: opcode-class rows against `{SUNDA v2, CAYMAN v3, MARIANA v4}` columns, with each cell recording *present / absent / field-delta* rather than re-listing the (identical) check tree per arch. A reimplementer who internalizes the two axes can reconstruct any one arch's validator set from the shared family roster plus that arch's column, instead of memorizing three near-identical rosters. §1 fixes the generation scheme and pins the three band extents; §4 documents the `ib_hal_to_isa_engine_v{3,4}` HAL→ISA engine map that sits at the head of each arch's block driver and routes a record to its per-engine legality switch.

For reimplementation, the contract is:

- **The triplication invariant** — there is one validator *design* and three compiled *instances*. For every opcode, `{SUNDA, CAYMAN, MARIANA}` each carry an independent `is_valid_<op>` + `dbg_is_valid_<op>` pair in their own band, generated from `neuron_<arch>_arch_isa/tpb/aws_neuron_isa_tpb_assert.h`. Reproduce the *design* once; instantiate it per target with that target's constant table.
- **The static arch selection** — the arch is chosen at the entry, not inside the body. `tdrv_arch_ops.instr_block_caps->final_inst_validity_check` points at `ib_is_valid_engine_instruction_v2`/`_v3`/`_v4`, latched once per process by `al_hal_tpb_get_arch_type()` ∈ {2,3,4}. Each entry calls *only* its own arch's forest; the three forests never cross-call.
- **The shared family roster** — the set of validator *families* (header/events preamble, the `tensorNd_valid` descriptor ladder, `tensor_start_addr_valid`/`addr_aligned_dtype`, `is_valid_dtype`/`is_valid_enum`, the per-opcode validators) is the same across all three arches; it is the rows of §2 and §3.
- **The per-arch deltas are constants and a few opcodes, not logic** — the arches differ in address-aperture and quadrant geometry constants, the dtype set (FP4 / 64-bit gating), and a small per-generation opcode delta (e.g. CAYMAN gains `dma_transpose 0xBD`, `jpeg_decode 0x81`; MARIANA gains the `dma_gather_transpose`, `modify_pool_config`, FP4 quantize paths). The opcode→struct dispatch and the predicate trees are identical.

| | |
|---|---|
| **Three forests, one design** | SUNDA (arch 2) · CAYMAN (arch 3) · MARIANA (arch 4) — each a full `is_valid_*`/`dbg_is_valid_*` copy in its own `.text` band |
| **Generated origin** | `neuron_{sunda,cayman,mariana}_arch_isa/tpb/aws_neuron_isa_tpb_assert.h` (per-arch copies of one template; DWARF `addr2line`) |
| **SUNDA band (v2)** | validators `0x27xxxx`; documented slice `0x334250..0x33f761`; silent `is_valid_neuron_instruction @0x2f2350`; debug `debug_invalid_neuron_isa_instruction @0x2ecbb0`; entry `ib_is_valid_engine_instruction_v2 @0x2f6b80` |
| **CAYMAN band (v3)** | validators `0x387540..0x39c601` and `0x39c610..0x3ab42e`; silent `is_valid_neuron_isa_instruction.constprop.0 @0x3a60b0`; debug `debug_invalid_neuron_isa_instruction.constprop.0 @0x39fec0`; entry `ib_is_valid_engine_instruction_v3 @0x3aa1e0` |
| **MARIANA band (v4)** | validators `0x3b0960..0x3b84b7` and `0x3e0980..0x3eb380`; debug `debug_invalid_neuron_engine_instruction @0x439ab0`; entry `ib_is_valid_engine_instruction_v4 @0x43f810`; block driver `ib_final_inst_validity_check_v4 @0x4436f0` |
| **HAL→ISA engine map** | `ib_hal_to_isa_engine_v3 @0x3aa190` (72 B, `instruction_block_cayman.c:33`) · `_v4 @0x43f7c0` (MARIANA analog) |
| **Arch selector** | `al_hal_tpb_get_arch_type @0x44bca0` → `{INVALID 0/1, SUNDA 2, CAYMAN 3, MARIANA 4}` |
| **MARIANA tdrv hook** | vtable slot `0xbf3778` → `ib_final_inst_validity_check_v4_wrapper @0x30d2d0` (`tdrv_arch_mariana.c:62`) |
| **Generated-copy cost** | the silent dispatcher alone: 17 952 B (SUNDA) + 16 606 B (CAYMAN) + 16 087 B (MARIANA inlined) ≈ 50 KB for one logical function |

---

## 1. The Generation Scheme — One Template, Three Compiled Copies

### Purpose

Before reading any delta, a reimplementer needs the mechanical fact that makes the deltas small: the three validator forests are not three hand-written validators that happen to resemble each other — they are three *compilations of one generated source*. The source is a per-arch copy of `aws_neuron_isa_tpb_assert.h`; the compiler emits, for each opcode, a silent predicate and a debug twin into each arch's translation unit, and the linker leaves them as three disjoint `.text` bands. That is why the per-opcode check trees are structurally identical across arches and the only differences are constant values and a few generation-specific opcodes.

### The template → three TUs mechanism

```text
                          aws_neuron_isa_tpb_assert.h   (one generated template)
                                          │
              ┌───────────────────────────┼───────────────────────────┐
   neuron_sunda_arch_isa/tpb/   neuron_cayman_arch_isa/tpb/   neuron_mariana_arch_isa/tpb/
        (arch token = sunda)        (arch token = cayman)        (arch token = mariana)
              │                           │                           │
        compiled TU                 compiled TU                 compiled TU
              │                           │                           │
   ┌──────────┴──────────┐     ┌──────────┴──────────┐     ┌──────────┴──────────┐
   SUNDA .text band          CAYMAN .text bands          MARIANA .text bands
   is_valid_<op>             is_valid_<op>_0 (clone)     is_valid_<op>_1 (clone)
   dbg_is_valid_<op>         dbg_is_valid_<op>_0         dbg_is_valid_<op>_1
   tensorNd_valid            tensorNd_valid_0            tensorNd_valid_1
   (~0x27xxxx, 0x33xxxx)     (0x387540.., 0x39c610..)    (0x3b0960.., 0x3e0980..)
```

The `_0` / `_1` symbol suffixes on the CAYMAN and MARIANA copies are GCC clone disambiguators (the SUNDA copy, emitted first, keeps the undecorated name); `.constprop.0` and `.part.0` are constant-propagation and partial-inlining decorations. `nm` resolves all of them to the clean C identifier. The structural proof that the three are one design is DWARF: each band's functions `addr2line` to the same relative path `tpb/aws_neuron_isa_tpb_assert.h`, differing only in the `neuron_<arch>_arch_isa/` prefix, and the line numbers within that header match across arches (e.g. `tensor4d_valid` resolves to `assert.h:3089` in the MARIANA copy at `0x3e6af0`).

### Band extents (pinned)

The three forests do not interleave — each is a run of contiguous `.text`. The documented extents below are the slices verified function-by-function against `nm` (band boundaries confirmed by the first OUT-of-band symbol):

| Arch | ISA | Validator `.text` extent(s) | Boundary anchor | Conf |
|---|---|---|---|---|
| SUNDA | v2 | `~0x27bxxx` (shared primitives), documented slice `0x334250..0x33f761` (24 fns) | the `0x32xxxx`/`0x33xxxx` TU holds the v2 leaves | HIGH |
| CAYMAN | v3 | `0x387540..0x39c601` (24 fns) + `0x39c610..0x3ab42e` (19 v3 fns + 5 MARIANA primitives) | ends `0x3ab42e` (`is_valid_int_aluop`); next sym `is_valid_imm_reg @0x3ab430` OUT | HIGH |
| MARIANA | v4 | `0x3b0960..0x3b84b7` (24 fns) + `0x3e0980..0x3eb380` (24 fns) | ends `0x3b84b7` (`dbg_is_valid_dma_gather_transpose`); shared v4 primitives at `0x3aa..0x3df` | HIGH |

> **NOTE —** the bands are *adjacent and partly intermixed at the seams*, not perfectly disjoint. The tail of the CAYMAN band `0x3aad80..0x3ab42e` holds five **MARIANA** shared primitives (`neuron_isa_tpb_new/merge_dbg_bool_1`, `is_valid_enum_1`, `is_valid_dtype_1`, `is_valid_int_aluop_0`) physically abutting the CAYMAN block — they are high-degree boundary nodes fanned-in by 49–107 MARIANA `_1` validators living in other bands. DWARF attribution, not address proximity, is the ground truth for which arch a function belongs to. A reimplementer sweeping by address range must use `addr2line` to assign each function to its arch, or it will mis-file these seam functions.

> **QUIRK —** the three giant silent dispatchers are the clearest single artifact of the triplication cost. `is_valid_neuron_instruction @0x2f2350` (SUNDA, 17 952 B, 530 BB), `is_valid_neuron_isa_instruction @0x3a60b0` (CAYMAN, 16 606 B, 499 BB), and the third copy folded into `ib_is_valid_engine_instruction_v4 @0x43f810` (MARIANA, 16 087 B, 488 BB) decode the **same** 64-byte record with the **same** opcode→struct switch — the opcode dispatch is byte-for-byte identical across all three. Only the leaf legality constants the arms feed differ. Roughly 50 KB of `.text` implements one logical function three times.

---

## 2. The Validator-Family Roster

### Purpose

The triplicated space is best described by listing each validator *family* once and recording which arches carry it. Because the families are shared (they come from the one template), this single roster — not three per-arch lists — is the complete inventory. The arch-coverage column flags the handful of families that are generation-specific; everything unmarked exists, identically-structured, in all three bands.

### Roster (one row per family, all three arches)

| Validator family | What it checks | Arch coverage | Conf |
|---|---|---|---|
| `has_valid_neuron_header` / `dbg_` | opcode ∈ `OPCODE` enum; `inst_word_len` (`byte[1]`) `== 16` | all (SUNDA/CAYMAN/MARIANA) | HIGH |
| `has_valid_neuron_events` / `dbg_` | `WAIT_MODE`/`UPDATE_MODE` enums + semaphore-update consistency | all | HIGH |
| `is_valid_enum` / `dbg_` | membership in a `NEURON_ISA_TPB_ENUM_LIST_*` value set (packed bitmask) — most-called leaf | all | HIGH |
| `is_valid_dtype` / `dbg_` | dtype ≠ `INVALID`; `FP32R` only if allowed; ∈ `DTYPE` enum | all; **MARIANA adds** `is_valid_dtype_inc_fp4` (FP4_EXP2 gate) | HIGH |
| `is_valid_dtype_64` / `dbg_` | dtype variant permitting `INT64`/`UINT64` | all | HIGH |
| `addr_aligned_dtype` | address alignment vs dtype element size (1/2/4/8-byte) | all | HIGH |
| `tensor_start_addr_valid` | the central SBUF/PSUM aperture + register-mode (`regnum≤0x3F`) check | all | HIGH |
| `tensor{1,2,3,4}d_valid` / `dbg_` | per-rank extent + reg-mode marker + start-addr legality ladder | all (`tensor2d_valid` is the most-reused leaf: 17 callers in CAYMAN) | HIGH |
| `mem4d_valid` / `dbg_check_m{2,3,4}d_active_channels` | direct/indirect `MEM_PATTERN` descriptors + active-channel windows | all | HIGH |
| `tpb_addr_active_channels` / `start_addr_active_channels` | partition-mask active-channel range gate | all | HIGH |
| `is_general_arith_op` / `is_bitvec_op` / `is_valid_int_aluop` | ALU-op class predicates (bitmask whitelists over `ALU_OP`) | all | HIGH |
| `valid_mm_{sbuf,psum}_quadrant` / `addresses_in_same_sbuf_quadrant` | PE-array matmul SBUF/PSUM quadrant geometry | all (geometry constants differ per arch) | HIGH |
| matmul: `is_valid_s4d3_mm` / `dbg_` / `matmul_regular` | 4D-strided + regular matmul: transpose/perf-opt/fp32r/quadrant | all | HIGH(surface)/MED(branch) |
| tensor-tensor / reduce: `tensor_tensor_bitvec`, `tensor_reduce_*` | per-engine TT-bitvec; reduce ALU-op × dtype legality hub | all | HIGH |
| rand: `is_valid_rand[_set_state]` / `dbg_` | RAND algo/mode (single vs dual src), reserved-zero | all | HIGH |
| copy/cast: `copy`, `cast`, `reciprocal`, `copy_cast_predicated` | opcode gate + shared header/events/subdim check set | CAYMAN+ (the `0x46`/`0x47`/`0x48` family appears in v3 roster) | HIGH |
| DMA: `dma_transpose`, `dma_gather_transpose`, `dma_indirect` | nc-aware transpose tiling / gather-transpose / indirect ADDR8 | **CAYMAN** (`dma_transpose 0xBD`), **MARIANA** (`dma_gather_transpose`, `dma_indirect 0xBB`) | HIGH |
| `jpeg_decode`, `conv_lut_load`, `s1d2_sort`, `stream_shuffle` | image/LUT/sort/shuffle opcode validators | CAYMAN+ (in v3 roster; `conv_lut_load 0xE4` also in v3 `0x39cc40`) | HIGH |
| `modify_pool_config`, `ctrl_es`, `ctrl_poll_sem`, `match_value_load`, `pe_manage_seed` | MARIANA composite opcode validators | **MARIANA** | HIGH |
| `is_valid_ctrl_no`, `ctrl_no_valid_notification` | CTRL_NO no-op/notification control word | all (MARIANA `is_valid_ctrl_no @0x3b1350` is the v4-dispatch leaf template) | HIGH |
| `lpa_valid_imm_ptr`, `is_valid_dge_compute_op`, `get_type_size` | load-pool-arg immediate ptr; DGE compute-op; dtype byte-size | all | HIGH |
| ctor/merge: `neuron_isa_tpb_new_dbg_bool` / `merge_dbg_bool` | `DEBUG_BOOL` constructor / AND-merge — one clone per arch | all (3 clones: bare / `_0` / `_1`) | HIGH |

> **GOTCHA —** the per-opcode validator names drift slightly between the silent and debug twin and between arch clones (`is_valid_s2s2d3_roi` vs `dbg_is_valid_s2s2d3_roi_0`; `tensor2d_valid` vs `tensor2d_valid.constprop.0` vs `tensor2d_valid_1`). These are the *same* family — do not count them as distinct validators. A reimplementer who treats the silent leaf, its debug twin, and the three arch clones as five separate functions will over-count the design five-fold; there is one predicate per `(opcode-class)`, instantiated `2 (silent/debug) × 3 (arch)` ways.

---

## 3. The Per-Arch Delta Dimension Table

This is the centerpiece. The validator space has two axes: **opcode-class** (the rows) and **arch** (`SUNDA v2` / `CAYMAN v3` / `MARIANA v4`, the columns). Every cell records the *delta* — `present` (the family exists, structurally identical), `+field-Δ` (present but with an arch-specific constant or extra field check), or `absent` (the opcode/check does not exist on that generation). The table deliberately does **not** repeat the (identical) check tree per column; it records only how each opcode-class differs across the three silicon targets. A representative validator address pins each present cell to its band so the claim is verifiable.

### Axis values

| Axis | Values | Source |
|---|---|---|
| arch | SUNDA (2) · CAYMAN (3) · MARIANA (4) | `al_hal_tpb_get_arch_type @0x44bca0`; per-arch DWARF header prefix |
| opcode-class | the §2 families, grouped by shared / arch-specific | `aws_neuron_isa_tpb_assert.h` per-opcode emission |
| cell value | `present` / `+Δ<what differs>` / `absent` | function presence in band (nm) + decompiled constant compare |

### Dimension table (opcode-class × arch)

| Opcode-class | SUNDA v2 | CAYMAN v3 | MARIANA v4 | Conf |
|---|---|---|---|---|
| header/events preamble | present (`has_valid_neuron_header`, `_events`) | present (`_0`) | present (`dbg_has_valid_neuron_header_1 @0x3b5370`, `_events_1 @0x3b4630`) | HIGH |
| `is_valid_enum` oracle | present (`@0x27b650`, 210 callers) | present (`@0x3aaf90`, 101 callers) | present (`@0x3aaf90` shared / `is_valid_enum_1`) | HIGH |
| dtype legality | present (`is_valid_dtype @0x27b910`) | present (`_0`) | **+Δ FP4**: adds `is_valid_dtype_inc_fp4 @0x3b3b10` (FP4_EXP2=16 gate for quantize/MX) | HIGH |
| 64-bit dtype | present | present | present (`dbg_is_valid_dtype_64 @0x3b2010`) | HIGH |
| address aperture | present (`tensor_start_addr_valid_0`; base `0x2000000`, PSUM span `0x3FFFFF`, partition mask `0x1E000000`, PSUM bias `0x1800000`) | **+Δ const**: same masks, PSUM-bias band `0x2003FFF` (ROI gate) | **+Δ const**: partition cap `≤0x1FFFFFF`/`≤0xFFFFFF`; s4d3 PSUM guard `(addr&0x1FFFFFFF)>0x83FFFF` | HIGH |
| SBUF/PSUM quadrant geometry | present (`addresses_in_same_sbuf_quadrant_0`, `valid_mm_{sbuf,psum}_quadrant`) | present (same fns, v3) | **+Δ const**: `addresses_in_same_sbuf_quadrant_1 @0x3ab550`; `dbg_indirect_quadrant_check_src3d_dst3d @0x3b0960` | HIGH |
| tensor descriptor ladder | present (`tensor{1,2,3,4}d_valid`, `dbg_` twins) | present (`tensor2d_valid.constprop.0 @0x397d00`, 17 callers) | present (`tensor4d_valid_1 @0x3e6af0`, 13 callers) | HIGH |
| matmul (regular + s4d3_mm) | present (`dbg_is_valid_s4d3_mm @0x337810`, `matmul_regular @0x33aed0`) | present (`s3_lt` load-stat-transpose `@0x394650` uses `valid_mm_sbuf_quadrant`) | present (`is_valid_s4d3_mm_0 @0x3e7610`, `dbg_ @0x3e11b0` emits `s4d3_mm_perf_opt_dtype`) | HIGH(surface)/MED(branch) |
| tensor-tensor / reduce | present (`tensor_tensor_bitvec @0x337480`, `tensor_reduce_arith @0x33a670`) | present (`tensor_reduce_common @0x395850` hub; `cross_lane_reduce @0x387540`) | present (`is_valid_cross_lane_reduce_1 @0x3e7400`) | HIGH |
| batchnorm family | present (subset) | present (`is_valid_s4d2_bn_0 @0x39ab90`, 5 opcodes; `stats2_common @0x39aa40`) | **+Δ opcodes**: `s3s3d1_bn @0x3e0b60` (`0x63`), `s3s3d1_bn2 @0x3e0e50` (`0x94`), `s4d2_bn_1 @0x3e80c0` | HIGH |
| rand | present (`is_valid_rand_set_state @0x33e4a0`, engine `0x78`) | present (`dbg_is_valid_rand @0x38d0e0`, `0x76`, 5241 B) | present (`is_valid_d4_rand_1 @0x3e8d30`; `pe_manage_seed @0x3e9840`) | HIGH |
| copy / cast / reciprocal | absent from documented v2 slice | **present** (`copy 0x46`, `cast 0x47`, `reciprocal 0x48`, `copy_cast_predicated`) | present (folded into v4 forest) | HIGH |
| DMA transpose | absent | **+Δ present** `dma_transpose 0xBD` (nc-aware, `dbg_ @0x388810`, xpose tile geometry) | **+Δ present** `dma_gather_transpose @0x3b72f0` (nc, 8 merge sites) | HIGH |
| DMA indirect | present (`indirect_copy 0xE7`, `@0x33e690`) | present (indirect addressing markers) | **+Δ present** `dma_indirect 0xBB @0x3eac40` (DMA_INDIRECT_FLAGS + PSEUDO_ADDR8) | HIGH |
| jpeg / conv-lut / sort / shuffle | absent from documented v2 slice | **present** (`jpeg_decode 0x81 @0x397dc0`, `conv_lut_load 0xE4 @0x399850`, `s1d2_sort 0x96 @0x398a00`, `stream_shuffle 0x6A @0x38ab60`) | present (`conv_lut_load`, `match_replace8 0x6F @0x3e7060`, `gather @0x3e7250`) | HIGH |
| pool / ctrl composites | acttableld `0x23`, iflush `0xA3`, notify `0xA6` (`@0x335590/0x336460/0x334250`) | present | **+Δ present** `modify_pool_config @0x3b5550`, `ctrl_es @0x3b60e0`, `ctrl_poll_sem 0xB3 @0x3e34f0` | HIGH |
| ROI / scalar-tensor-tensor | present (`s2s2d3_roi 0x73 @0x33cef0`) | present (`is_valid_s2s2d3_roi_0 @0x39b670`, `s2s2d2_stt @0x39b8c0`) | present (`tensor_scalar_select 0x98 @0x3e0980`, `s2s2d2_stt` family) | HIGH |
| DEBUG_BOOL ctor/merge | present (`new/merge_dbg_bool @0x27b440/0x27b4c0`) | present (`_0 @0x323e40/0x323ec0`) | present (`_1 @0x3aad80/0x3aae00`) | HIGH |

> **QUIRK —** read the table by *column delta against SUNDA*, not by row. The SUNDA column is the baseline; CAYMAN's column adds the copy/cast/reciprocal, jpeg/sort/conv-lut, and `dma_transpose` opcode-classes (the `0x46`-band engine ops and the DMA-transpose engine), and MARIANA's column further adds the FP4 dtype gate, `dma_gather_transpose`, `dma_indirect 0xBB`, the `modify_pool_config`/`ctrl_es`/`ctrl_poll_sem` composites, and the `s3s3d1_bn`/`bn2` batchnorm opcodes. Every *unmarked* cell is the same predicate tree — the deltas are a small additive frontier of new opcodes per generation plus a handful of `+Δ const` aperture/quadrant geometry changes. A reimplementer reproduces the SUNDA forest, then adds CAYMAN's new opcode validators, then MARIANA's; the shared core is never re-derived.

> **NOTE —** the `+Δ const` cells (address aperture, quadrant geometry, active-channel windows) are where the per-silicon physical SBUF/PSUM layout leaks into the validator. The check *logic* is identical — `(addr & mask) within aperture` — but the mask and aperture constants are per-generation, because the partition stride, PSUM bias, and quadrant size are hardware geometry. These constants are the validator's encoding of the [device address map](../runtime/arch-geometry.md); they are not free to copy between arches.

---

## 4. The `ib_hal_to_isa_engine` HAL→ISA Engine Map

### Purpose

Each arch's block driver opens by mapping the runtime's HAL engine enumeration onto the ISA's `NEURON_ISA_TPB_NEURON_ENGINE` taxonomy, then runs a per-engine opcode-legality switch (a PE-array matmul issued on the SP engine is malformed regardless of field validity). The map is a tiny per-arch leaf — `ib_hal_to_isa_engine_v3 @0x3aa190` (72 B, `instruction_block_cayman.c:33`) for CAYMAN, and its MARIANA analog `_v4 @0x43f7c0` reached from `ib_is_valid_engine_instruction_v4 @0x43f810` (`instruction_block_mariana.c:33`). It is the one place the otherwise-generated validator forest meets hand-written tdrv glue, and it is the entry seam at which arch selection has already happened.

### Entry Point

```text
tdrv_arch_ops.instr_block_caps->final_inst_validity_check   ── slot, arch-latched
  └─ ib_final_inst_validity_check_v3   (0x3aaca0)  ── 64-byte-stride block driver
       └─ ib_is_valid_engine_instruction_v3 (0x3aa1e0)      ── per-record, engine-gated
            ├─ ib_hal_to_isa_engine_v3       (0x3aa190)     ── HAL eng → ISA eng  [THIS §]
            ├─ is_valid_neuron_isa_instruction (0x3a60b0)   ── structural dispatch (CAYMAN forest)
            └─ ib_is_valid_engine_instruction_v3_0 (0x3a4cf0) ── .part.0 logging tail
                 └─ debug_invalid_neuron_isa_instruction_0 (0x39fec0) ── DEBUG_BOOL reason
```

For MARIANA the same shape is `ib_final_inst_validity_check_v4 @0x4436f0` → `ib_is_valid_engine_instruction_v4 @0x43f810` → `ib_hal_to_isa_engine_v4 @0x43f7c0` + the v4 forest, with the diagnostic tail in `debug_invalid_neuron_engine_instruction @0x439ab0`. The MARIANA driver is reached through the `tdrv_arch_ops` vtable slot `0xbf3778` (`ib_final_inst_validity_check_v4_wrapper @0x30d2d0`, `tdrv_arch_mariana.c:62`).

### Algorithm

```c
// Models ib_hal_to_isa_engine_v3 @0x3aa190 (72 B, instruction_block_cayman.c:33).
// The MARIANA copy ib_hal_to_isa_engine_v4 @0x43f7c0 is the same body, v4-cloned.
// Maps al_hal_tpb_eng_type_t (HAL taxonomy) -> NEURON_ISA_TPB_NEURON_ENGINE (ISA taxonomy).
function ib_hal_to_isa_engine_v3(hal_eng) -> NEURON_ISA_TPB_NEURON_ENGINE:
    if hal_eng > AL_HAL_TPB_ENG_SP:                  // out-of-range HAL id
        nlog_write("Unknown hal->isa engine enum conversion (%u --> ?)", hal_eng)  // 0x812f58
        return NEURON_ISA_TPB_TOP_SP                 // fallback: the collective-trigger sequencer
    return isa_engine_of[hal_eng]                    // direct table map; {PE,ACT,POOL,DVE,SP}->{0,1,2,3,4}

// Models ib_is_valid_engine_instruction_v3 @0x3aa1e0 (2747 B). The per-record gate that
// sits above the generated forest; one body per arch (v2/v3/v4), differing only in which
// arch-clone of the forest and which per-engine opcode set it consults.
function ib_is_valid_engine_instruction_v3(hal_eng, inst /* 64-byte record */) -> bool:
    isa_eng = ib_hal_to_isa_engine_v3(hal_eng)        // 0x3aa190 — HAL->ISA engine id
    if !is_valid_neuron_isa_instruction(inst):        // 0x3a60b0 — STRUCTURAL gate (CAYMAN forest)
        return ib_is_valid_engine_instruction_v3_0(inst)  // 0x3a4cf0 — log reason, return false
    // per-engine opcode legality: is this opcode issuable on THIS engine?
    switch isa_eng:                                   // {PE, ACT, POOL, DVE, SP}
        case PE:   ... is_valid_int_aluop / tensor_tensor_bitvec_engine / tensor_start_addr_valid_0
        case ACT:  ...                                // per-engine opcode-set predicates
        case POOL: ...                                // (bittest64 masks over the opcode byte)
        case DVE:  ...
        case SP:   ...
    return engine_opcode_legal                        // AND of structural + per-engine legality

// Models ib_final_inst_validity_check_v3 @0x3aaca0 (207 B). The block driver: walk the
// instruction-block buffer in 64-byte strides, validate each record, log the first failure.
function ib_final_inst_validity_check_v3(buf /* {size_used, *buf} */, hal_eng) -> bool:
    assert(buf.size_used % TPB_INST_NBYTES == 0)      // 0x40 stride invariant; 0x813000 assert
    for off in range(0, buf.size_used, 0x40):         // one 64-byte record per step
        inst = buf.buf + off
        if !ib_is_valid_engine_instruction_v3(hal_eng, inst):
            nlog_write("Found invalid instruction! opcode=%u, offset=%u",  // 0x813030
                       inst.raw.bytes[0], off)
            return false                              // first invalid record stops the walk
    return true
```

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `ib_hal_to_isa_engine_v3` | `0x3aa190` | 72 B | HAL eng → ISA eng; `>AL_HAL_TPB_ENG_SP` → `TOP_SP` fallback + log | HIGH |
| `ib_hal_to_isa_engine_v4` | `0x43f7c0` | 72 B | MARIANA clone of the engine map | HIGH |
| `ib_hal_to_isa_engine_v2` | `0x2f6b30` | 72 B | SUNDA clone of the engine map | HIGH |
| `ib_is_valid_engine_instruction_v3` | `0x3aa1e0` | 2747 B | per-record gate: engine map + structural + per-engine legality | HIGH |
| `ib_is_valid_engine_instruction_v3_0` | `0x3a4cf0` | 159 B | `.part.0` logging tail; emits per-slot `DEBUG_BOOL` err rows | HIGH |
| `ib_final_inst_validity_check_v3` | `0x3aaca0` | 207 B | 64-byte-stride block driver; logs first invalid record | HIGH |
| `ib_final_inst_validity_check_v4` | `0x4436f0` | — | MARIANA block driver (`instruction_block_mariana.c:49`) | HIGH |
| `ib_final_inst_validity_check_v4_wrapper` | `0x30d2d0` | — | tdrv vtable slot `0xbf3778` → v4 driver (`tdrv_arch_mariana.c:62`) | HIGH |

### Considerations

The engine map and the per-engine legality switch are the *only* hand-written tdrv code in the per-arch validation path; everything below `is_valid_neuron_isa_instruction` is generated. The `nlog_write` strings (`"Unknown hal->isa engine enum conversion (%u --> ?)" @0x812f58`, `"Found invalid instruction! opcode=%u, offset=%u" @0x813030`, `"Error message number:%u,  message: %s" @0x812f30`) live only in these `ib_*` glue functions — the generated validators reference no strings, because they assemble their `DEBUG_BOOL` messages from the shared `.rodata` template pool (`xmmword_850FA0..`, [validator-architecture §2](validator-architecture.md#the-err-message-template-pool)). The provenance string `/opt/workspace/KaenaRuntime/tdrv/instruction_block_cayman.c` (`@0x81f090`) appears four times, once per `ib_*` call site in the CAYMAN driver — the cleanest confirmation that the driver layer is per-arch tdrv glue wrapping the per-arch generated forest.

> **QUIRK —** the triplication is *complete down to the block driver*, not just the leaves. There is a separate `ib_final_inst_validity_check_v{2,3,4}`, a separate `ib_is_valid_engine_instruction_v{2,3,4}`, and a separate `ib_hal_to_isa_engine_v{2,3,4}` per arch — even these tiny glue functions (72–207 B) are cloned three times rather than shared with a runtime arch parameter. The arch is resolved once, statically, at the `tdrv_arch_ops` slot, and from that point every function on the path is the arch-specific clone. A reimplementer can collapse the three glue clones into one parameterized function with no behavioral change — the cloning here is a compilation artifact of the per-arch TU split, not a design requirement, since the glue logic (unlike the leaf constants) does not actually vary by arch.

---

## Related Components

| Name | Relationship |
|---|---|
| `ib_is_valid_engine_instruction_v{2,3,4}` | the three arch-versioned entries; each calls only its own arch's forest |
| `is_valid_neuron_instruction` / `is_valid_neuron_isa_instruction.constprop.0` | the SUNDA / CAYMAN silent dispatchers (third copy inlined into `_v4`) |
| `aws_neuron_isa_tpb_assert.h` (per-arch) | the one generated template compiled into three TUs |
| `tdrv_arch_ops.instr_block_caps->final_inst_validity_check` | the vtable slot that selects the arch's block driver |
| `ib_hal_to_isa_engine_v{2,3,4}` | the per-arch HAL→ISA engine map at the head of each block driver |

## Cross-References

- [ISA Validator Architecture and Entry Tree](validator-architecture.md) — the validator *engine* this page deltas: the silent/debug two-family design, the `NEURON_ISA_TPB_DEBUG_BOOL` ABI, and the shared header/events preamble (do not re-derive it here)
- [The 64-Byte Instruction Record Format](instruction-record.md) — the `NEURON_ISA_TPB_INST_UNION` and per-opcode struct views every per-arch validator decodes
- [Overview: the TPB Engine Instruction Model](overview.md) — Part VI map; where per-arch validation sits in the lowering→validation→stage flow
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](../runtime/tdrv-arch-ops.md) — the `tdrv_arch_ops` vtable (`final_inst_validity_check` slot, `instr_block_caps`) and `al_hal_tpb_get_arch_type` that statically select the arch forest
