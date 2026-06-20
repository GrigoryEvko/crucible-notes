# RDMA/DMA Collective Pseudo-Ops (DIRECT2D / GATHER_XPOSE / INDIRECT1D)

This page decodes the three RDMA/DMA descriptor-generation pseudo-ops that the
Neuron compiler emits and the host runtime (NRT) lowers into the real GPSIMD DGE
(Descriptor-Generation Engine) instructions: **DIRECT2D** (2-D strided copy),
**INDIRECT1D** (indexed gather/scatter), and **GATHER_XPOSE** (fused gather +
xbar-transpose). It gives, for each, the compile-verified 64-byte struct, its DGE
descriptor *kind*, the host lowering dispatch (byte-exact), the SDMA descriptor-ring
path, the shared `dma_configs`/`compute_op`/`dtype` carriers, and — the boundary the
collective survey cares about — **how the compute-side gather here differs from the
collective-side and the Pool-engine gathers**.

> **Scope note.** These three are the *compute-side / custom-op* indirect-DMA
> descriptor kinds — on-core descriptor generation, not inter-core data movement.
> The *collective* gather (cross-NeuronCore) is a **different** instruction,
> [S3D3 Collective `0xBF`](s3d3-collective.md); the *Pool-engine* subset gather is yet
> another, [`GATHER 0x68`](../../dma/gather-scatter-descriptors.md). §7 nails all three
> boundaries. This page MUST match the committed
> [descriptor model](../../dma/descriptor-model.md) /
> [gather-scatter descriptors](../../dma/gather-scatter-descriptors.md) /
> [DGE micro-op encoding](../../dma/dge-microop-encoding.md) /
> [descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md) catalog
> exactly — it does, and the §1 opcode table cites the same six 64-B words.

**Provenance.** Layouts are compile-verified from the shipped clean C ISA headers
(`aws-neuronx-gpsimd-customop-lib 0.21.2.0`, the four per-arch copies
`neuron_{cayman,mariana,maverick,sunda}_arch_isa/tpb/*.h`); opcode bindings from each
arch's `instruction_mapping.json`; the lowering dispatch disassembled byte-exact from
`libnrt.so.2.31.24.0` (NRT host runtime, DWARF-named, `.text` VMA==fileoffset); the
device decode strings from `libnrtucode_internal.so`. Tags: HIGH/MED/LOW ×
OBSERVED/INFERRED/CARRIED, per claim.

---

## 0. The two-level model in one paragraph

`direct2d / indirect1d / gather_xpose` exist at **two** levels. (a) As **real HW
instructions** that the DGE/SDMA executes — four distinct 64-byte words with their own
opcodes: `DMAMemcpy 0xb8` (DIRECT2D), `DmaIndirect 0xbb` (INDIRECT1D),
`DmaTranspose 0xbd` (DIRECT2D_XPOSE), `DmaGatherTranspose 0xf1` (GATHER_XPOSE). (b) As a
**single compiler pseudo** — `PseudoDmaDirect2d 0xd4`, struct
`NEURON_ISA_TPB_PSEUDO_DMA_DIRECT2D_STRUCT` — that carries a `dge_op` selector byte
(`NEURON_ISA_TPB_DGE_OPCODE`) choosing which real kind NRT lowers it to. INDIRECT1D and
both transpose kinds additionally require a back-to-back `PseudoExtension 0xda`
(`PSEUDO_DMA_EXT_STRUCT`) carrying the index/tile spillover fields. All six structs are
64 bytes (`ISA_STATIC_ASSERT`, all offsets compile-confirmed in §2/§5/§8). The host
dispatch reads the pseudo's `dge_op` byte at **struct offset 15** and the `0xda` byte at
**offset 64**, then routes to the per-kind translator; the produced HW word is decoded on
the device by `dge_decode_fast` and driven onto the SDMA BD ring via
`rdma_desc_gen → rdma_desc_start`. [OBSERVED HIGH — headers + JSON + libnrt disasm + ucode
strings, all below.]

---

## 1. Opcode + struct binding [HIGH/OBSERVED]

`aws_neuron_isa_tpb_common.h` (cayman/NC-v3) `NEURON_ISA_TPB_OPCODE` enum — the `// Y`
comment marks a *real HW* (non-pseudo) opcode; the `0b110`-prefixed values are the
compiler-pseudo class:

| opcode | enumerator | role | struct | line |
|---|---|---|---|---|
| `0xb8` | `OPCODE_DMAMEMCPY` `// Y` | DIRECT2D (2-D copy) | `DMA_DIRECT2D_STRUCT` | `common.h:257` |
| `0xbb` | `OPCODE_DMA_INDIRECT` `// Y` | INDIRECT1D (indexed) | `DMA_INDIRECT1D_STRUCT` | `common.h:258` |
| `0xbd` | `OPCODE_DMA_TRANSPOSE` `// Y` | DIRECT2D_XPOSE (xbar) | `DMA_DIRECT2D_XPOSE_STRUCT` | `common.h:260` |
| `0xc4` | `OPCODE_PSEUDO_DMAMEMCPY_FULL_IND` | pseudo (full-indirect AP) | `PSEUDO_DMA_MEMCPY_FULL_IND_STRUCT` | `common.h:266` |
| `0xd4` | `OPCODE_PSEUDO_DMA_DIRECT2D` | **the one compiler pseudo** | `PSEUDO_DMA_DIRECT2D_STRUCT` | `common.h:282` |
| `0xda` | `OPCODE_PSEUDO_EXTENSION` | pseudo extension (shared) | `PSEUDO_DMA_EXT_STRUCT` | `common.h:288` |
| `0xf1` | `OPCODE_DMA_GATHER_TRANSPOSE` `// Y` | GATHER_XPOSE (gather+xbar) | `DMA_GATHER_XPOSE_STRUCT` | `common.h:302` |

`0xb8/0xbb/0xbd` are `< 0xC0` (outside the pseudo range); `0xf1` is in the `0xF0` EXTISA
range (a POOL extended-opcode). The `struct2opcode` map in `instruction_mapping.json`
(cayman) binds them:

```json
"NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT"        : ["NEURON_ISA_TPB_OPCODE_DMA_MEMCPY"]            // 0xb8
"NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT"      : ["NEURON_ISA_TPB_OPCODE_DMA_INDIRECT"]          // 0xbb
"NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT"  : ["NEURON_ISA_TPB_OPCODE_DMA_TRANSPOSE"]         // 0xbd
"NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT"    : ["NEURON_ISA_TPB_OPCODE_DMA_GATHER_TRANSPOSE"]  // 0xf1
"NEURON_ISA_TPB_PSEUDO_DMA_DIRECT2D_STRUCT" : ["NEURON_ISA_TPB_OPCODE_PSEUDO_DMA_DIRECT2D"]   // 0xd4
"NEURON_ISA_TPB_PSEUDO_DMA_EXT_STRUCT"      : ["NEURON_ISA_TPB_OPCODE_PSEUDO_EXTENSION"]      // 0xda
```

> **QUIRK (OBSERVED).** The JSON spells the memcpy opcode `OPCODE_DMA_MEMCPY` (with an
> underscore) while the `common.h` enumerator is `OPCODE_DMAMEMCPY` (no underscore). Same
> value `0xb8`; purely cosmetic. Both spellings read directly. [HIGH/OBSERVED]

These are the **same six** 64-B descriptor words the committed
[descriptor model §2](../../dma/descriptor-model.md) catalogs (`0xbd ≠ 0xf1` — two distinct
transpose words; see §4 here and the CORRECTION callout there: the catalog is **six**, not
five). [HIGH/OBSERVED — JSON + header + cross-page]

---

## 2. STRUCT #1 — DIRECT2D (`DMA_DIRECT2D_STRUCT`, opcode `0xb8`, 64 B) [HIGH/OBSERVED]

Header doc (`aws_neuron_isa_tpb_dma_direct2d.h`): *"DmaMemcpy Instruction. This instruction
will generate DMA descriptors to initiate a Memcpy. The descriptors will be supplied by the
DGE block and the TPB engine will update the DMA queue tail pointers when the descriptors are
ready."* `ISA_STATIC_ASSERT(sizeof == 64)`; gcc layout probe confirms `sizeof==64` and every
offset (§8).

| off | size | field | C type | meaning |
|---|---|---|---|---|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode==0xb8, inst_word_len, debug_cmd, debug_hint}` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore (`common.h:418`) |
| 12 | 1 | `dma_configs` | `DMA_CONFIGS` | `priority_class:3 + reserved:5` (§6.1) |
| 13 | 1 | `semaphore` | `uint8_t` | completion-sem index |
| 14 | 1 | `sem_increment` | `uint8_t` | sem increment; bounds # DMAs used |
| 15 | 1 | `compute_op` | `DGE_COMPUTE_OP` | reduce-on-copy op (§6.4) |
| 16 | 8 | `src_start_addr` | `ADDR8` | SRC base (imm/reg/table) |
| 24 | 8 | `src_step_elem[2]` | `int32_t[2]` | 2-D **signed** strides (elem) |
| 32 | 4 | `src_num_elem[2]` | `uint16_t[2]` | 2-D shape (X,Y element counts) |
| 36 | 2 | `src_elem_size` | `uint16_t` | bytes/elem (`0` ⇒ 64 KiB) |
| 38 | 1 | `src_bound_reg` | `BOUND_CHECK_REG` | src **address** bound (§6.3) |
| 39 | 1 | `dst_bound_reg` | `BOUND_CHECK_REG` | dst **address** bound |
| 40 | 8 | `dst_start_addr` | `ADDR8` | DST base |
| 48 | 8 | `dst_step_elem[2]` | `int32_t[2]` | 2-D signed strides |
| 56 | 4 | `dst_num_elem[2]` | `uint16_t[2]` | 2-D shape |
| 60 | 2 | `dst_elem_size` | `uint16_t` | bytes/elem |
| 62 | 1 | `in_dtype` | `DTYPE` | SRC dtype |
| 63 | 1 | `out_dtype` | `DTYPE` | DST dtype |

**Semantics.** A 2-D strided DMA copy `src → dst`. Both operands are a full `ADDR8` base +
a 2-D `(X,Y)` stride/shape access pattern with a per-element size. If `in_dtype != out_dtype`
the copy **dtype-casts** (the CCE compute path); if equal it is a bitwise memcpy. A non-`NONE`
`compute_op` turns it into a reduce-on-copy (`B += A`, etc.). There is **no** `num_active_channels`
field — the DGE backend decides the DMA spread at descriptor-gen time (§9). The header inline
validator `is_valid_dma_direct2d` = `_general` ∧ `_arith`:

```c
/* is_valid_dma_memcpy_direct_general (header pseudo-code, OBSERVED): */
//   has_valid_neuron_header(i) && has_valid_neuron_events(i)
//   && i.header.opcode == Opcode::DMAMemcpy
//   && is_valid_addr8(src_start_addr) && is_valid_addr8(dst_start_addr)
//   && is_valid_dge_shape_reg(src_start_addr, src_step_elem[0])     // shape-from-register mode
//   && is_valid_dge_shape_reg(dst_start_addr, dst_step_elem[0])
//   && zero_num_step_elem_shape_reg_mode(i)                         // num/step==0 when shape-reg
//   && are_valid_memcpy_dtypes(compute_op, in_dtype, out_dtype)
//   && has_valid_bound_check_reg(src_bound_reg, src_start_addr.addr_tbl_offs.marker)
//   && has_valid_bound_check_reg(dst_bound_reg, dst_start_addr.addr_tbl_offs.marker)
//   && is_valid_dge_compute_op(compute_op, total_src_elems, total_dst_elems, src_addr, dst_addr)
//   && is_valid_dma_configs(dma_configs)
/* _arith: is_valid_dma_memcpy_direct_without_cce(i)   // byte counts match, in==out, compute_op==NONE
        || is_valid_dma_cce(i)                          // aligned addrs+steps + CCE elem-size */
```

[HIGH/OBSERVED — full validator block verbatim in the header.]

---

## 3. STRUCT #2 — INDIRECT1D (`DMA_INDIRECT1D_STRUCT`, opcode `0xbb`, 64 B) [HIGH/OBSERVED]

Header doc (`aws_neuron_isa_tpb_dma_indirect1d.h`): *"DmaIndirect performs DMAs for a vector
of dynamic indices (offsets) generated during execution. These dynamic indices can be applied
to the source to perform a gather, or applied to the destination to perform a scatter, or two
sets of independent indices for both gather and scatter in one instruction."* `sizeof==64`,
probe-confirmed.

| off | size | field | C type | meaning |
|---|---|---|---|---|
| 0 | 4 | `header` | `HEADER` | `opcode==0xbb` |
| 4 | 8 | `events` | `EVENTS` | sem |
| 12 | 1 | `semaphore` | `uint8_t` | completion-sem index |
| 13 | 1 | `sem_increment` | `uint8_t` | increment |
| 14 | 1 | `idx_num_active_channels` | `uint8_t` | # idx-gen channels (== 128, §6.2) |
| 15 | 1 | `flags` | `DMA_INDIRECT_FLAGS` | gather/scatter mode + dims (§3.1) |
| 16 | 8 | `src_start_addr` | `ADDR8` | SRC base |
| 24 | 4 | `src_step_elem` | `int32_t` | 1-D src stride |
| 28 | 2 | `src_num_elem` | `uint16_t` | src element count (≤ 4096) |
| 30 | 2 | `src_elem_size` | `uint16_t` | bytes/elem |
| 32 | 8 | `dst_start_addr` | `ADDR8` | DST base |
| 40 | 4 | `dst_step_elem` | `int32_t` | 1-D dst stride |
| 44 | 2 | `dst_num_elem` | `uint16_t` | dst element count (≤ 4096) |
| 46 | 2 | `dst_elem_size` | `uint16_t` | bytes/elem |
| 48 | 4 | `src_idx_start_addr` | `ADDR4` | **gather index tensor** (SBUF part 0) |
| 52 | 4 | `dst_idx_start_addr` | `ADDR4` | **scatter index tensor** |
| 56 | 1 | `in_dtype` | `DTYPE` | src dtype |
| 57 | 1 | `out_dtype` | `DTYPE` | dst dtype |
| 58 | 1 | `src_idx_bound_reg` | `BOUND_CHECK_REG` | bound on the **src index** (§6.3) |
| 59 | 1 | `dst_idx_bound_reg` | `BOUND_CHECK_REG` | bound on the **dst index** |
| 60 | 1 | `compute_op` | `DGE_COMPUTE_OP` | scatter-add reduce mode (§6.4) |
| 61 | 1 | `dma_configs` | `DMA_CONFIGS` | `priority_class:3` (§6.1) |
| 62 | 2 | `reserved[2]` | `uint8_t[2]` | validator: `== 0` |

> **NOTE.** This is byte-identical to the compute-side `DMA_INDIRECT 0xbb` word the
> committed [gather-scatter descriptors §1](../../dma/gather-scatter-descriptors.md) and the
> [firmware indirection engine](../../firmware/kernels/indirection-gather.md) decode (same
> `idx_num_active_channels@14`, `flags@15`, `src@16`, `dst@32`, `src_idx@48`, `dst_idx@52`,
> `compute_op@60`, `dma_configs@61`, `reserved@62`). INDIRECT1D is *both* the collective-survey
> pseudo-op here *and* the compute-side indexed-DMA word there — one instruction, two views.
> [HIGH/OBSERVED — byte-exact reconciliation]

### 3.1 `DMA_INDIRECT_FLAGS` (1 byte) — `common.h:852` [HIGH/OBSERVED]

```c
struct NEURON_ISA_TPB_DMA_INDIRECT_FLAGS {           // packed, 1 byte
    INDIRECT_DMA_ADDRESSING_MODE indirect_mode : 2;  // SRC_INDIRECTION=0 (gather),
                                                     // DST_INDIRECTION=1 (scatter),
                                                     // SRC_DST_INDIRECTION=2 (both)
    uint8_t                      idx_bound_is_err : 1;   // OOB index is a hard error
    uint8_t                      non_unique_dst_idx : 1; // allow duplicate scatter targets (scatter-add)
    INDIRECT_DIM                 gather_dim : 2;          // X=0 Y=1 Z=2 W=3
    INDIRECT_DIM                 scatter_dim : 2;
};
```

The `indirect_mode` enumerators carry a redundant doubled prefix in the header
(`INDIRECT_DMA_ADDRESSING_MODE_INDIRECT_DMA_ADDRESSING_MODE_SRC_INDIRECTION = 0x0`, etc.);
the **values** are `0/1/2`. The validator `has_valid_indirect_dim_by_mode` requires: in
`SRC_INDIRECTION` mode `gather_dim` is dim-checked and `scatter_dim == 0` (vice-versa for
`DST`); in `SRC_DST` both are checked. `indirect_dim_check`: when shape-from-register, any
valid `IndirectDim`; otherwise the indirect dim **must be `X`** (the only dim available
without a shape reg). [HIGH/OBSERVED]

### 3.2 Validity constraints (header inline, OBSERVED HIGH)

- `has_dma_indirect_valid_index_count`: index count **≤ 4096** for the active leg(s) (or
  shape-from-register). Matches the committed catalog's `≤4096` per-leg cap.
- `has_dma_indirect_idx_addr_in_sbuf_partition0`: the `ADDR4` index tensor **must be in SBUF
  partition 0** (`has_addr4_in_sbuf_partition0`: from-register OR `addr_in_sbuf_partition(0, …)`).
- `check_dma_indirect_indices(idx_num_active_channels)` = `check_active_channels(n) ∧
  (n == POOLING_NUM_CHANNELS)`, with `POOLING_NUM_CHANNELS == 128U` (`common.h:35`) — the
  index generator runs **all 128 pooling channels** (§6.2).
- `reserved[2] == 0`; valid `dma_configs`; valid memcpy dtypes; valid `dge_compute_op`.

---

## 4. STRUCT #3 — GATHER_XPOSE (`DMA_GATHER_XPOSE_STRUCT`, opcode `0xf1`, 64 B) [HIGH/OBSERVED]

Header doc (`aws_neuron_isa_tpb_dma_gather_xpose.h`): *"DmaGatherTranspose performs a gather
operation from HBM or SBUF using dynamic indices, followed by xbar transpose, and writes the
result to SBUF. This instruction uses the SW-DGE backend with Q7 processors in the Gpsimd
engine. Key use cases: MOE MLP and chunked prefill attention. … Only 2B data types supported
initially (BF16/FP16); Xbar transpose tiles are 16x128 for 2B dtypes; Index tensor must be
UINT32, multiple of 16 indices; Destination must be 32B aligned (xbar transpose requirement);
Gather dimension must be slowest dimension (Y) initially."* `sizeof==64`, probe-confirmed.

| off | size | field | C type | meaning |
|---|---|---|---|---|
| 0 | 4 | `header` | `HEADER` | `opcode==0xf1` |
| 4 | 8 | `events` | `EVENTS` | sem |
| 12 | 1 | `semaphore` | `uint8_t` | completion-sem index |
| 13 | 1 | `dma_configs` | `DMA_CONFIGS` | `priority_class:3` (§6.1) |
| 14 | 1 | `idx_num_active_channels` | `uint8_t` | # idx-gen channels (== 128, §6.2) |
| 15 | 1 | `src_idx_bound_reg` | `BOUND_CHECK_REG` | bound on the src index |
| 16 | 8 | `src_start_addr` | `ADDR8` | SRC base (HBM **or** SBUF) |
| 24 | 8 | `src_step_elem[2]` | `int32_t[2]` | 2-D signed strides |
| 32 | 4 | `src_num_elem[2]` | `uint16_t[2]` | 2-D shape (`src_num_elem[Y] % 16 == 0`) |
| 36 | 4 | `src_idx_start_addr` | `ADDR4` | **gather index tensor** (UINT32, 4 B-aligned) |
| 40 | 8 | `dst_start_addr` | `ADDR8` | DST base (SBUF, 32 B-aligned) |
| 48 | 3 | `reserved[3]` | `uint8_t[3]` | pad |
| 51 | 1 | `dst_bound_reg` | `BOUND_CHECK_REG` | dst **address** bound |
| 52 | 4 | `dst_step_elem_1` | `int32_t` | dst step dim1; **dim0 step = `sizeof(dtype_hi)`** |
| 56 | 4 | `dst_num_elem[2]` | `uint16_t[2]` | 2-D dst shape |
| 60 | 2 | `elem_size` | `uint16_t` | bytes/elem (2..256, even) |
| 62 | 1 | `dtype` | `DTYPE_PAIR` | `dtype_lo:4` (src) + `dtype_hi:4` (dst) |
| 63 | 1 | `flags` | `DMA_GATHER_TRANSPOSE_FLAGS` | `gather_dim:2 + reserved:6` |

> **QUIRK (OBSERVED).** GATHER_XPOSE is the **only** member of the DMA family that packs
> src+dst dtype into a single byte (`DTYPE_PAIR`, `common.h:767`: `dtype_lo:4 / dtype_hi:4`)
> — because it spends its bytes on the `ADDR4` index (`@36`) plus the dst-tile geometry. It
> also is the only one whose dim0 dst step is **implicit** (`= sizeof(dtype_hi)`), not stored.
> [HIGH/OBSERVED]

> **GOTCHA — `0xbd` ≠ `0xf1`.** Do not conflate `DMA_GATHER_XPOSE` (`0xf1`, *index-array*
> driven, with an `ADDR4` index at `@36`) with the sibling `DMA_DIRECT2D_XPOSE` (`0xbd`,
> *plain* xbar transpose, no index — `DMA_DIRECT2D_XPOSE_STRUCT` carries its tile geometry
> **inline** as `tile_src_row_step@36`, `tile_src_rows@60`, `tile_src_cols@61`,
> `semaphore@62`, `dma_configs@63`). They are two distinct 64-B words with distinct opcodes.
> The compiler emits the plain transpose via `dge_op == DMA_TRANSPOSE = 2` (§5); GATHER_XPOSE
> via `dge_op == DMA_GATHER_TRANSPOSE = 3`. This is the "six descriptors, not five" CORRECTION
> in [descriptor model §2](../../dma/descriptor-model.md). [HIGH/OBSERVED — both headers + probe]

### 4.1 Validator `is_valid_dma_gather_transpose` (header inline, OBSERVED HIGH)

```c
//   has_valid_dma_gather_transpose_nc(nc):           nc >= NeuronCoreVersion::V3   // NOT sunda/V2 (§10)
//   has_valid_gather_transpose_dtype:                size(dtype_lo)==2 && size(dtype_hi)==2
//                                                    && dtype_lo == dtype_hi       // 2B only, NO cast
//   has_valid_gather_transpose_elem_size:            2 <= elem_size <= 256 && elem_size % 2 == 0
//   has_valid_gather_transpose_addresses:            src %2==0, dst %32==0, src_idx %4==0
//   has_valid_gather_transpose_flags:                flags.gather_dim == IndirectDim::Y    // dim[1], slowest
//   has_valid_gather_transpose_src_dimensions:       src_num_elem[1] % 16 == 0              // transpose-tile align
//   has_valid_gather_transpose_cross_field_consistency: src_num_elem[1] == dst_num_elem[0] // both = #indices
//   has_valid_gather_transpose_dst_steps:            dst_step_elem_1 % 32 == 0 || dst_num_elem[1]==1
//   check_dma_indirect_indices(idx_num_active_channels)                                    // == 128
//   has_valid_idx_bound_check_reg_no_flags(src_idx_bound_reg)
```

[HIGH/OBSERVED — full validator block verbatim in the header.]

### 4.2 Semantics

A **fused gather + xbar-transpose**. For each of `N` indices (`N = src_num_elem[Y] =
dst_num_elem[X]`, `N % 16 == 0`), read a row from `src[idx]` (HBM or SBUF) by a UINT32 index,
transpose it through the **16×128 xbar tile**, and write to the 32 B-aligned SBUF destination.
This is the COMPUTE-side counterpart of the device DGE "GATHER TRANSPOSE" kind; the on-device
builder is `tensor_reshape_indirect_transpose` (its `tile_src_rows` / `tile_src_cols` /
`dtype_size` / `num_indices` log strings read byte-exact from `libnrtucode_internal.so`). The
header's *"SW-DGE backend with Q7"* is the software descriptor-gen backend (§9). [HIGH struct +
validator; the 16×128-tile execution is from the header doc + the device strings, HIGH.]

---

## 5. The compiler pseudo + the lowering [HIGH/OBSERVED]

### 5.1 `PSEUDO_DMA_DIRECT2D` (opcode `0xd4`, 64 B) — the *one* compiler pseudo

Header doc (`aws_neuron_isa_tpb_pseudo_dma_direct2d.h`): *"This pseudo instruction will serve
as the compiler interface to communicate a DMAMemcpy request wrt compiler variables. Runtime
will translate this into a hardware DMAMemcpy instruction by using the variable ids to populate
the absolute mem pattern. … dge_op — the type of dge operation to lower to: dma_direct2d /
dma_indirect1d (requires extension instr) / dma_transpose (requires extension instr)."*

Layout is **identical** to DIRECT2D (§2) **except**:

- `@15` = `dge_op` (`NEURON_ISA_TPB_DGE_OPCODE`, *not* `compute_op`).
- `src_start_addr@16` / `dst_start_addr@40` are `PSEUDO_ADDR8` (8 B): immediate `NeuronAddr` /
  register / table / **`addr_var`** (a neff variable id + immediate offset, resolved at model
  load) / unknown. The `PSEUDO_ADDR8` marker top byte selects the kind (`common.h:586`:
  `MARKER_IMM=0x00`, `…OFFSET_WIDE_BIT (1<<2)`, `…OFFSET_REG_BIT (1<<3)`, `…VAR_ID_BIT (1<<4)`,
  `…ADDR_TBL_BIT (1<<5)`, `…SHAPE_REG_BIT (1<<6)`, `…ADDR_REG_BIT (1<<7)`).
- `dma_configs@12`, `semaphore@13`, `sem_increment@14`, `src_bound_reg@38`, `dst_bound_reg@39`,
  `in_dtype@62`, `out_dtype@63` — same offsets as DIRECT2D.

The `dge_op` enum (`common.h:829`):

```c
enum NEURON_ISA_TPB_DGE_OPCODE {
    DMA_DIRECT2D         = 0x0,   // lower to DMAMemcpy 0xb8     (no extension)
    DMA_INDIRECT1D       = 0x1,   // lower to DmaIndirect 0xbb   (requires 0xda extension)
    DMA_TRANSPOSE        = 0x2,   // lower to DmaTranspose 0xbd  (requires 0xda extension)
    DMA_GATHER_TRANSPOSE = 0x3,   // lower to DmaGatherTranspose 0xf1 (requires 0xda extension)
};
```

The pseudo's `dma_configs` doc: *"priority_class (0-4): the lower 3 bits … 0 indicates
unused/invalid priority — apply the default packet size; the max allowed priority class number
in ucode is 4."* So although the field is 3 bits (`0..7`), the ucode **caps usable values at
4** — i.e. the 5-value band `P0..P4`, matching the committed
[DGE micro-op encoding §6](../../dma/dge-microop-encoding.md). [HIGH/OBSERVED]

### 5.2 `PSEUDO_DMA_EXT` (opcode `0xda`, 64 B) — the extension instruction

Header doc: *"This instruction must follow a Pseudo DMA Direct instruction (using
pseudo_dma_direct2d struct) immediately in the instruction stream. This extension is currently
being used to hold spillover fields for indirect dma index info. … This instruction must have
PseudoExtension==0xda opcode in the first byte. There could be multiple pseudo instruction
extension structs that use this same opcode."* So `0xda` is a **generic** extension opcode;
disambiguation is **positional** — it is whatever extension the instruction it immediately
follows expects. (The same `0xda` value also carries `TRIGGER_COLLECTIVE2_EXT_STRUCT`; see
[trigger-collective2-ext](trigger-collective2-ext.md).) Layout (probe-confirmed):

| off | size | field | C type |
|---|---|---|---|
| 0 | 1 | `opcode` | `OPCODE` (`== 0xda`) |
| 1 | 1 | `flags` | `DMA_INDIRECT_FLAGS` (the gather/scatter/dim byte, §3.1) |
| 2 | 1 | `idx_num_active_channels` | `uint8_t` |
| 3 | 1 | `compute_op` | `DGE_COMPUTE_OP` |
| 4 | 4 | `src_idx_start_addr` | `ADDR4` |
| 8 | 4 | `dst_idx_start_addr` | `ADDR4` |
| 12 | 4 | `src_bound_size_bytes` | `uint32_t` |
| 16 | 4 | `dst_bound_size_bytes` | `uint32_t` |
| 20 | 4 | `tile_src_row_step` | `int32_t` |
| 24 | 1 | `tile_src_rows` | `uint8_t` |
| 25 | 1 | `tile_src_cols` | `uint8_t` |
| 26 | 30 | `reserved1[30]` | `uint8_t[30]` |
| 56 | 4 | `src_bound_size_bytes_upper` | `uint32_t` |
| 60 | 4 | `dst_bound_size_bytes_upper` | `uint32_t` |

The single `0xda` extension carries **both** the indirect-index info (flags / index `ADDR4`s /
byte-bounds for INDIRECT1D) **and** the transpose tile geometry (`tile_src_rows`/`tile_src_cols`/
`tile_src_row_step` for the transpose kinds). Validator `has_valid_xpose_tile_size`:
`tile_src_rows ∈ {16, 0}`, `tile_src_cols ∈ {128, 0}` — the same 16×128 tile as the GATHER_XPOSE
HW word, or all-zero when the extension is carrying index info rather than tile geometry.
`is_valid_pseudo_dma_ext_fields`: index fields are validated only when `idx_num_active_channels
!= 0`. [HIGH/OBSERVED]

### 5.3 The host NRT lowering dispatch (libnrt, disassembled byte-exact) [HIGH/OBSERVED]

DWARF subprogram names (`libnrt.so.2.31.24.0`, `.text` VMA==fileoffset):
`translate_one_pseudo_dge_instr_v2 / _v3` (the dispatcher),
`translate_one_pseudo_dma_memcpy_instr_v2` (the direct2d/indirect1d builder),
`translate_pseudo_dma_direct2d_xpose_instr_v3`, `translate_pseudo_dma_gather_xpose_instr_v3`,
`insert_dma_indirect1d_bound_check_instrs_v2`.

**`translate_one_pseudo_dge_instr_v3` @ `0x3220d0`** — routes the two transpose kinds:

```asm
3220d4:  mov    %edi,%eax              ; eax = dge_op  (1st arg = the off-15 field)
3220ea:  cmp    $0x2,%al               ; DGE_OPCODE_DMA_TRANSPOSE
3220ec:  je     322140  -> jmp 0x321ac0  translate_pseudo_dma_direct2d_xpose_instr_v3
3220ee:  cmp    $0x3,%al               ; DGE_OPCODE_DMA_GATHER_TRANSPOSE
3220f0:  jne    322100                 ; (dge_op 0/1 -> common memcpy/indirect path)
3220f6:  jmp    0x321dc0               translate_pseudo_dma_gather_xpose_instr_v3
322100..32213a:  nlog_write("Unsupported dynamic dma opcode %u"); mov $0x2,%eax; ret  ; bad dge_op
```

**`translate_one_pseudo_dge_instr_v2` @ `0x322c40`** — reads `dge_op@15` and the `0xda@64`
extension:

```asm
322ca9:  movzbl 0xf(%r14),%eax         ; *** dge_op at STRUCT OFFSET 15 ***
322cb5:  test   %al,%al
322cb7:  je     322e40                 ; dge_op==0 (DMA_DIRECT2D) arm  -> DMAMemcpy 0xb8, no ext
322cbd:  cmp    $0x1,%al               ; dge_op==1 (DMA_INDIRECT1D)
322cbf:  jne    3230f0                 ;   (2/3 fall through to v3 dispatcher)
322cd7:  cmpb   $0xda,0x40(%r14)       ; *** byte at OFFSET 64 == 0xda (the PseudoExtension) ***
322cdc:  jne    323180                 ; -> nlog "Pseudo dma indirect1d instruction must be followed by extension"
322d12:  movzbl 0x41(%r14),%eax        ; reads ext.flags at ext-offset 1 (= struct off 65)
322d17:  movb   $0xbb,0x30(%rsp)       ; *** writes opcode 0xbb (DmaIndirect) into the new word ***
```

This is **conclusive proof** of the lowering model: NRT reads `dge_op@15`; for `dge_op==0`
(DIRECT2D) it lowers directly to `DMAMemcpy 0xb8` with no extension; for `dge_op==1`
(INDIRECT1D) it **requires** the `0xda` `PseudoExtension` immediately following at offset 64,
reads its flags/index fields, and emits the `DmaIndirect 0xbb` word; for `dge_op==2/3`
(TRANSPOSE / GATHER_TRANSPOSE) the v3 dispatcher routes to the xpose / gather-xpose builders.
The v2 frame even allocates a stack local `dma_indirect1d_ins` of type
`NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT` (64 B) as the scratch word it builds; its constant pool
includes `184 (0xb8)`, `187 (0xbb)`, `218 (0xda)`, `128`. `v2` vs `v3` is a NEFF/ABI version
gate (`translate_one_pseudo_instr_v3` selects which based on a config check; the v3 path is the
one that adds the transpose / gather-transpose kinds). [HIGH/OBSERVED — disasm + DWARF +
stack-var types]

### 5.4 Per-kind `dma_configs` routing — `add_dma_configs_priority_class_v3` @ `0x321a80`

The priority-class copy is per-kind, proving the differing `dma_configs` offsets:

```asm
321a80:  movzbl 0xf(%rdi),%eax          ; dge_op@15 of the pseudo
321a86:  jne    321aa0                  ; dge_op != 0 -> indirect arm
                                        ; --- dge_op==0 (DIRECT2D): dma_configs @12 ---
321a88:  movzbl 0xc(%rdi),%edx          ; pseudo dma_configs @12
321a8c:  movzbl 0xc(%rsi),%eax          ; dst (DIRECT2D) dma_configs @12
321a90:  and    $0x7,%edx               ; priority_class : 3 (mask 0x7)
321a93:  and    $0xfffffff8,%eax        ; keep upper 5 reserved bits
321a96:  or     %edx,%eax ; mov %al,0xc(%rsi)   ; write merged dma_configs @12
321aa0:  cmp    $0x1,%al                ; dge_op==1 (INDIRECT1D)
321aac:  movzbl 0x3d(%rsi),%eax         ; *** dst (INDIRECT1D) dma_configs @61 (0x3d) ***
```

So the runtime copies the 3-bit `priority_class` from the pseudo's `dma_configs@12` into the
**per-kind** offset of the real word: `@12` for DIRECT2D, `@61` (`0x3d`) for INDIRECT1D
(`@13` for GATHER_XPOSE; §6.1). [HIGH/OBSERVED — disasm]

### 5.5 `PSEUDO_DMAMEMCPY_FULL_IND` (opcode `0xc4`) — the family sibling, for completeness

A *distinct* pseudo (not one of the three task ops). Header: an indirect AP that points to a
dynamically-generated AP in SBUF (pre-placed via `READ_VAR_ADDR`): an indirect leg
`{indirect_start_offset@12, indirect_step_elems[2]@16, indirect_num_elems[2]@24}` + a
direct/static leg `{direct_start_offset@32, direct_step_elems[2]@36, direct_num_elems[2]@44,
direct_elem_size@52, direct_var_id@56}` + `semaphore_wait_value@58` + `indirection_mode@60`
(SRC/DST). It is the "fully indirect" memcpy where the **whole** access pattern (not just an
index vector) is computed at runtime; it lowers via
`translate_one_pseudo_dma_memcpy_instr_v2` too. [HIGH/OBSERVED struct; role from doc.]

---

## 6. The shared carriers — `dma_configs`, `idx_num_active_channels`, bound regs, `compute_op`, `dtype`

### 6.1 `DMA_CONFIGS` (1 byte, `common.h:713`) [HIGH/OBSERVED]

```c
struct NEURON_ISA_TPB_DMA_CONFIGS { uint8_t priority_class : 3; uint8_t reserved_bitfield : 5; };
```

The 3-bit `priority_class` (host-gated valid range **`P0..P4`**, ucode-capped at 4) indexes the
DGE priority-class map (`nrtucode_core_dge_{set,get}_priority_class_map` @ `0x9b11e0`/`0x9b10f0`),
gated on `boot_state == BOOTED_LEGACY` and a `*_NX_POOL` core kind
(`{SUNDA,CAYMAN,MARIANA,MARIANA_PLUS,MAVERICK}_NX_POOL`) — a higher-priority custom-op DMA
stream is serviced first (cross-link [descriptor model "priority map"](../../dma/descriptor-model.md)).
Offsets: DIRECT2D `@12`, INDIRECT1D `@61`, GATHER_XPOSE `@13`, PSEUDO_DMA_DIRECT2D `@12`,
DIRECT2D_XPOSE `@63`.

> **CORRECTION-guard (vs compiler IR).** The host `priority_class` is the **5-value** `P0..P4`
> band (the runtime gate, `<= 4`). The compiler-IR `DMAQoSClass` is a wider `0..14`; the host
> `add_dma_configs_priority_class_v3` lowering narrows it into the 3-bit field. Do not cite
> `0..14` for the wire `dma_configs` — that is the IR-level QoS, not the descriptor byte
> (cross-link [DGE micro-op encoding](../../dma/dge-microop-encoding.md)). [HIGH/OBSERVED]

### 6.2 `idx_num_active_channels` (`common.h:2343`) [HIGH/OBSERVED]

INDIRECT1D (`@14`) and GATHER_XPOSE (`@14`) carry `idx_num_active_channels`. The validator
`check_dma_indirect_indices(n) = (n != 0) ∧ (n <= POOLING_NUM_CHANNELS) ∧ (n ==
POOLING_NUM_CHANNELS)` with `POOLING_NUM_CHANNELS == 128U` — i.e. the index generator must run
**all 128 pooling channels**. DIRECT2D has **no** channel field (the DGE backend decides the
DMA spread). [HIGH/OBSERVED — the predicate constant `common.h:2348`]

### 6.3 `BOUND_CHECK_REG` (1 byte, `common.h:707`) — and the role swap [HIGH/OBSERVED]

```c
struct NEURON_ISA_TPB_BOUND_CHECK_REG {
    REG_NUM bc_reg : 6;                     // register # of the bound; bc_reg+1 = high 32 bits for wide offsets
    uint8_t bc_disable_oob_error_notif : 1; // suppress the OOB notification
    uint8_t bc_enabled : 1;
};
```

`pseudo_dma_direct2d.h` doc, verbatim: *"src_bound_reg / dst_bound_reg … Used as address bound
check for DmaDirect2d and DmaTranspose. Used as index bound check for DmaIndirect1d."* So the
bound reg bounds the **address** for DIRECT2D / DIRECT2D_XPOSE / GATHER_XPOSE-dst, but bounds
the **index** for INDIRECT1D (`src_idx_bound_reg@58`, `dst_idx_bound_reg@59`) and GATHER_XPOSE
(`src_idx_bound_reg@15`). `flags.idx_bound_is_err` selects hard-error vs soft;
`bc_disable_oob_error_notif` suppresses the notification. NRT even ships
`insert_dma_indirect1d_bound_check_instrs_v2` — the lowering **inserts explicit bound-check
instructions** for INDIRECT1D when the HW reg path is unavailable (gated by
`tdrv_arch_instr_block_supports_dma_indirect1d_bound_check`). [HIGH/OBSERVED — doc + libnrt symbols]

### 6.4 `DGE_COMPUTE_OP` (1 byte, `common.h:837`) [HIGH/OBSERVED]

```c
enum NEURON_ISA_TPB_DGE_COMPUTE_OP {
    NONE=0,      // B = A   (plain memcpy)
    ADD=1,       // B += A   (reduce-/scatter-add)
    MULTIPLY=2,  // B *= A
    MAX=3, MIN=4,
};
```

The DGE's own copy-with-compute. Present in DIRECT2D `@15`, INDIRECT1D `@60` (scatter-add —
pairs with `flags.non_unique_dst_idx` for duplicate scatter targets), and PSEUDO_DMA_EXT `@3`.
`are_valid_memcpy_dtypes` gates: either a no-CCE memcpy (`compute_op==NONE`, `in==out`) or a
valid CCE/cast dtype pair. [HIGH/OBSERVED — enum + validator]

### 6.5 `DTYPE` / `DTYPE_PAIR` [HIGH/OBSERVED]

The 4-bit `NEURON_ISA_TPB_DTYPE` (`common.h:722`): `INVALID=0`, `UINT64=1`, `INT8=2`, `UINT8=3`,
`INT16=4`, `UINT16=5`, `BFLOAT16=6`, `FP16=7`, `INT32=8`, `UINT32=9`, `FP32=A`, `FP32R=B`,
`INT64=C`, `FP8_EXP3=D`, `FP8_EXP4=E`, `FP8_EXP5=F`. DIRECT2D/INDIRECT1D carry separate
`in_dtype`/`out_dtype` bytes (cast allowed via the CCE path); GATHER_XPOSE packs both into one
`DTYPE_PAIR` byte (`dtype_lo:4 / dtype_hi:4`) and **requires `dtype_lo == dtype_hi`, 2 B only**
(no cast yet). The pseudo doc adds: `in_dtype` may not be `FP32R`, `UINT64`/`INT64` are invalid
for both, and `NC_v4+` restricts to the `DtypeBasic` subset. [HIGH/OBSERVED]

---

## 7. Compute-vs-collective gather — the three distinct gathers

This is the boundary the survey asks for. There are **three** different "gather" instructions
on this chip; they share the SDMA descriptor format and the `rdma_desc_gen` loop but are
*distinct ops* at *distinct levels*:

| gather | opcode | struct | level | mechanism | where decoded |
|---|---|---|---|---|---|
| **INDIRECT1D** (this page) | `0xbb` | `DMA_INDIRECT1D_STRUCT` | compute, **DGE descriptor-gen** | index→address transform, then a strided DMA per index | DGE `dge_decode_fast` "DGE INDIRECT" |
| **GATHER_XPOSE** (this page) | `0xf1` | `DMA_GATHER_XPOSE_STRUCT` | compute, **SW-DGE / Q7** | gather row by UINT32 index + 16×128 xbar transpose | DGE "DGE GATHER TRANSPOSE" |
| **Pool subset gather** | `0x68` | `S4D4_GT_STRUCT` | compute, **POOL/Q7 vector datapath** | HW IVP SuperGather (vector-register gather, no descriptor ring) | POOL `pool` decode |
| **Collective gather** | `0xBF` | `S3D3_COLLECTIVE_STRUCT` | **inter-core** (cross-NeuronCore) | SB2SB Pool/Q7 iDMA over the LNC-grouped peer ring | "Decode: SB2SB_Collective" |

The three boundaries, precisely:

1. **The DGE descriptor-gen gather (INDIRECT1D `0xbb`)** is the compute-side *indexed-DMA*
   kind documented here. The index→address transform is
   [`do_indirection` / `gather_indices` / `make_gather_pattern`](../../firmware/kernels/indirection-gather.md)
   (`base += last_indices * indirection_step` via the `IVP_MULUSAN_2X32` MAC, OOB gated by the
   `idx_bound_reg` + predicate masks). Index in SBUF partition 0, UINT32, ≤ 4096, 128 channels.
   It builds an SDMA BD ring (§9).

2. **The Pool-engine subset gather (`GATHER 0x68`, `S4D4_GT_STRUCT`)** is a *different*
   compute-side gather — the HW IVP SuperGather **vector datapath**, fed from
   `nki.isa.nc_n_gather` / `nki.isa.local_gather`. It is a Pool-engine instruction that gathers
   into vector registers, **not** a descriptor-generating DMA: no `rdma_desc_gen`, no index
   `ADDR4` in the same layout — see the committed
   [gather-scatter descriptors §3.1](../../dma/gather-scatter-descriptors.md). Contrast: `0xbb`
   *generates DMA descriptors*; `0x68` *runs a vector gather in the Pool datapath*. (Note that
   `nki.isa.local_gather` lowers to `INDIRECT_COPY 0xe7` / `S4D4_IC_STRUCT`, the software
   per-index loop, not `0x68` — three Pool-side gather/copy forms, all distinct from the DGE
   DMA forms.)

3. **The collective gather (`S3D3_COLLECTIVE 0xBF`, `SB2SB_COLLECTIVE`)** is the
   *cross-NeuronCore* reduce-scatter / all-gather leg — a real HW Pool/Q7 iDMA op that moves a
   3-D SBUF tensor between **LNC-grouped peer NeuronCores** (see
   [S3D3 Collective](s3d3-collective.md), `OPCODE_SB2SB_COLLECTIVE = 0xbf` at `common.h:262`).
   The compiler-level `TriggerCollective[2]` pseudos lower into it. It is **not** a pseudo-op
   and **not** a mode of `TRIGGER_COLLECTIVE`.

> **GOTCHA — the firmware "GATHER TRANSPOSE" decode is COMPUTE, not collective.** The device
> `libnrtucode_internal.so` logs `"P%i: DGE GATHER TRANSPOSE"` (the compute-side GATHER_XPOSE
> `0xf1`) **separately** from `"Decode: SB2SB_Collective"` (the collective leg). The DGE
> 3-kind decode-fast log set (`"P%i: DGE DIRECT2D"`, `"P%i: DGE INDIRECT"`, `"P%i: DGE GATHER
> TRANSPOSE"`) belongs entirely to the *compute / custom-op* path of this page. The collective
> Decode-set is its own group (`SB2SB_Collective`, `Sbuf2Sbuf`, `ExtendedInst*`,
> `GetSequenceBounds`, …). Same SDMA descriptor format, same `rdma_desc_gen` doorbell loop —
> different instructions. [HIGH/OBSERVED — both string groups read byte-exact this session]

**Shared machinery.** Both the compute DGE kinds *and* the collective SB2SB leg converge on the
**same** SDMA BD ring and the **same** `rdma_desc_gen → rdma_desc_start` tail-pointer doorbell
(§9). The difference is *what generates the descriptors* (on-core index→address compute for the
DGE kinds; the inter-core SB2SB composer for the collective) and *whether the data crosses a
NeuronCore boundary* (no for the three compute gathers; yes for the collective). The
differential validator harness for these is
[VAL-15 gather/scatter](../../validation/gather-scatter.md) (planned). [HIGH/OBSERVED]

---

## 8. GCC layout probe — every offset compile-confirmed [HIGH/OBSERVED]

Compiling all six structs against the cayman headers (`gcc -I…/neuron_cayman_arch_isa/tpb`,
`offsetof`/`sizeof`) independently reproduces every offset above:

```
sizeof DIRECT2D=64  INDIRECT1D=64  GATHER_XPOSE=64  DIRECT2D_XPOSE=64
       PSEUDO_DIRECT2D=64  PSEUDO_EXT=64
DIRECT2D:       dma_configs@12 compute_op@15 src_start_addr@16 dst_start_addr@40 in_dtype@62 out_dtype@63
INDIRECT1D:     idx_num_active_channels@14 flags@15 src_idx_start_addr@48 dst_idx_start_addr@52
                compute_op@60 dma_configs@61 reserved@62
GATHER_XPOSE:   dma_configs@13 idx_num_active_channels@14 src_idx_bound_reg@15 src_idx_start_addr@36
                dst_start_addr@40 dst_step_elem_1@52 elem_size@60 dtype@62 flags@63
DIRECT2D_XPOSE: tile_src_row_step@36 tile_src_rows@60 tile_src_cols@61 semaphore@62 dma_configs@63
PSEUDO_DIRECT2D:dge_op@15 src_start_addr@16 dst_start_addr@40 in_dtype@62
PSEUDO_EXT:     flags@1 idx_num_active_channels@2 compute_op@3 src_idx_start_addr@4
                tile_src_rows@24 tile_src_cols@25 src_bound_size_bytes_upper@56
```

Supporting types (`common.h`): `HEADER` 4 B (`opcode/inst_word_len/debug_cmd/debug_hint`);
`EVENTS` 8 B (`wait_mode/wait_idx/update_mode/update_idx/semaphore_value`); `ADDR8` 8 B union
(imm `NeuronAddr` / `addr_reg{reg_lo,reg_hi,…,marker}` / `addr_table8` / `addr_tbl_offs`),
`ADDR4` 4 B union; `PSEUDO_ADDR8` 8 B adds an `addr_var` (neff variable id + imm offset, marker
`VAR_ID_BIT (1<<4)`). All match the header inline byte annotations exactly. [HIGH/OBSERVED]

---

## 9. The DGE-kind map + SDMA lowering [HIGH/OBSERVED + cross-ref]

### 9.1 `dge_op → DGE kind → real HW opcode → device decode`

| `dge_op` | `DGE_OPCODE` | real HW op (opcode) | device DGE log |
|---|---|---|---|
| `0x0` | `DMA_DIRECT2D` | `DMAMemcpy` (`0xb8`) | `"P%i: DGE DIRECT2D"` |
| `0x1` | `DMA_INDIRECT1D` | `DmaIndirect` (`0xbb`) | `"P%i: DGE INDIRECT"` |
| `0x2` | `DMA_TRANSPOSE` | `DmaTranspose` (`0xbd`) | (folded into the gather-transpose reshape arm on device) |
| `0x3` | `DMA_GATHER_TRANSPOSE` | `DmaGatherTranspose` (`0xf1`) | `"P%i: DGE GATHER TRANSPOSE"` |

The device `dge_decode_fast` logs **three** kinds; the ISA `DGE_OPCODE` enum has **four**
(`TRANSPOSE=2` separate). Reconciliation: the device's plain xbar TRANSPOSE shares the
gather-transpose reshape builder `tensor_reshape_indirect_transpose` (it handles both the gather
and the transpose tiling), so a transpose-without-gather rides the gather-transpose decode arm as
a gather-transpose with an identity index. [3 device log lines + the 4-value ISA enum both
OBSERVED; that device TRANSPOSE rides the gather-transpose arm is **INFERRED/MED** from the
shared builder + only-3 log lines.]

### 9.2 The SDMA path — `dge_decode_fast → rdma_desc_gen → rdma_desc_start`

All three kinds converge on the DGE descriptor-generation pipeline (cross-link
[DGE micro-op encoding](../../dma/dge-microop-encoding.md) and
[descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md)):

```text
dge_decode_fast(kind)
  -> dge_shape[i] = { num[4], step[4] }                       // the engine's 4-D rep
  -> gen_spray_info / make_gather_pattern / do_indirection     // reshape_kind + indirection + #DMA split
  -> SELECT BACKEND  (Pool 2-dim | RTL 5+2-dim | software 4-dim | NONE)
  -> push GENERATE / DIMPUSH / REGWRITE to DMA[d]              // the SDMA BD-descriptor ring program
        GENERATE reads {src/dst_start_addr, elem_size, semaphore->sem_num} + the RD/WR direction tag
        DIMPUSH  reads {src/dst_step_elem[k], src/dst_num_elem[k]} — one per dimension
        REGWRITE programs a control reg before the data BDs
  -> rdma_desc_gen -> rdma_desc_start                          // drain BDs, write the M2S/S2M
                                                               // tail-pointer INCREMENT doorbell
```

Per-kind specifics:

- **DIRECT2D** — a plain 2-D strided copy; the Pool/software "[2dim]" descriptor; no index
  gather. `compute_op != NONE` ⇒ reduce-on-copy via the SDMA CCE descriptor.
- **INDIRECT1D** — `do_indirection` / `gather_indices` build the index→address (`base +=
  index * indirection_step` via `IVP_MULUSAN_2X32`), then the **same** GENERATE/DIMPUSH ring;
  gather (src indirection) / scatter (dst) / both per `flags.indirect_mode`; OOB gated by the
  `idx_bound_reg` + predicate masks. Index in SBUF partition 0, UINT32, ≤ 4096, 128 channels.
- **GATHER_XPOSE** — `make_gather_pattern` + `tensor_reshape_indirect_transpose`: gather the
  `N = src_num_elem[Y]` rows by UINT32 index, transpose through the 16×128 xbar tile, write the
  32 B-aligned SBUF dst; the *"SW-DGE backend with Q7"* (header) is the **software** backend of
  the FW backend-selection table. 2 B dtype only (BF16/FP16), no cast.

[pipeline HIGH/OBSERVED via the firmware emit strings + the indirection-engine page; the exact
BD-word bitfield per push is the descriptor-ring-field-tables scope — MED/CARRIED there.]

---

## 10. Generational delta (NC-v2 … NC-v5) [HIGH/OBSERVED]

Cross-arch header presence (`fd` over the four arch trees) and `DGE_OPCODE` width:

| header / feature | cayman (v3) | mariana (v4) | maverick (v5) | sunda (v2) |
|---|---|---|---|---|
| `dma_direct2d` (`0xb8`) | YES | YES | YES | YES |
| `dma_indirect1d` (`0xbb`) | YES | YES | YES | YES |
| `pseudo_dma_direct2d` (`0xd4`) | YES | YES | YES | YES |
| `dma_direct2d_xpose` (`0xbd`) | YES | YES | YES | **NO** |
| `dma_gather_xpose` (`0xf1`) | YES | YES | YES | **NO** |
| `DGE_OPCODE` values | 0..3 | 0..3 | 0..3 | **0..1 only** |

- **sunda (NC-v2)** ships **neither** transpose header. Its `DGE_OPCODE` enum has only
  `DMA_DIRECT2D=0` and `DMA_INDIRECT1D=1` (no `TRANSPOSE`/`GATHER_TRANSPOSE`); its `OPCODE` enum
  has only `DMAMEMCPY 0xb8` + `DMA_INDIRECT 0xbb` (no `0xbd`/`0xf1`). This matches the validator
  `has_valid_dma_{gather_,}transpose_nc = (nc >= V3)`. (sunda also lacks `SB2SB_COLLECTIVE 0xBF`
  — the collective gather is v3+ too.)
- The struct **bodies** of `dma_direct2d` / `dma_indirect1d` / `pseudo_dma_direct2d` are
  byte-identical across **all four** arches; `dma_gather_xpose` / `dma_direct2d_xpose` are
  byte-identical across the three (v3/v4/v5) that ship them (md5 of struct regions matches).
  Only the *"ISA header for NC-vN"* comment differs.

> **NOTE — v5/MAVERICK is header-OBSERVED only.** The maverick (NC-v5) struct *layouts* are
> verified from the shipped header text (the body md5 matches v3/v4), but no MAVERICK binary was
> byte-disassembled this session; the v2/v3/v4 paths are byte-grounded (the cayman header + the
> libnrt disasm + the ucode strings). MAVERICK interiors beyond the header text are
> **INFERRED/CARRIED** from the byte-identical struct region. [v2–v4 HIGH/OBSERVED; v5 layout
> HIGH-from-header, MAVERICK-binary behavior INFERRED]

---

## 11. Confidence ledger

**HIGH / OBSERVED**

- All six structs' full 64 B layout, every field type/width/offset (gcc probe `sizeof==64`
  each, §8); byte-identical across the arches that ship them (§10).
- Opcodes (`DMAMemcpy 0xb8`, `DmaIndirect 0xbb`, `DmaTranspose 0xbd`, `DmaGatherTranspose 0xf1`,
  `PseudoDmaDirect2d 0xd4`, `PseudoExtension 0xda`, `DMAMEMCPY_FULL_IND 0xc4`) + the
  `instruction_mapping.json` bindings (incl. the `DMA_MEMCPY` vs `DMAMEMCPY` naming quirk).
- The `DGE_OPCODE` enum (`DIRECT2D=0 / INDIRECT1D=1 / TRANSPOSE=2 / GATHER_TRANSPOSE=3`), the
  pseudo's `dge_op@15` selector, and the `0xda` PseudoExtension positional rule.
- All supporting enums: `DMA_INDIRECT_FLAGS`, `INDIRECT_DMA_ADDRESSING_MODE`, `INDIRECT_DIM`,
  `BOUND_CHECK_REG`, `DMA_CONFIGS`, `DGE_COMPUTE_OP`, `DTYPE`, `DTYPE_PAIR`,
  `DMA_GATHER_TRANSPOSE_FLAGS`, `PSEUDO_ADDR8` markers.
- The full inline validator pseudo-code for each, incl. GATHER_XPOSE's `nc>=V3` / 2 B-dtype /
  `Y`-gather-dim / ×16-multiple / 32 B-align / `src[Y]==dst[X]`, and INDIRECT1D's ≤4096 /
  SBUF-partition-0 / `==128`-channels.
- The host NRT lowering dispatch byte-exact: `translate_one_pseudo_dge_instr_v3` (`cmp $0x2 ->
  direct2d_xpose`, `$0x3 -> gather_xpose`, else memcpy) and `_v2` (`movzbl 0xf` reads
  `dge_op@15`; `cmp $0x1` indirect; `cmpb $0xda,0x40` reads the off-64 extension; `movb $0xbb`
  emits DmaIndirect); the `add_dma_configs_priority_class_v3` per-kind priority copy; DWARF
  names for all translators + the bound-check inserter.
- The device DGE 3-kind decode strings + the `do_indirection`/`gather_indices`/
  `make_gather_pattern`/`tensor_reshape_indirect_transpose` ucode strings; the separate
  `SB2SB_Collective` collective decode (the compute-vs-collective boundary, §7).
- `dma_configs.priority_class:3` (host `P0..P4`, ucode-capped at 4) feeding the DGE priority
  map; `idx_num_active_channels == POOLING_NUM_CHANNELS == 128`; index in SBUF partition 0,
  UINT32, ≤ 4096; address-bound for direct/transpose vs index-bound for indirect.
- Generational delta: sunda(V2) lacks both transpose headers + the DGE_OPCODE transpose values
  + the `0xbd`/`0xf1`/`0xbf` opcodes; struct bodies byte-identical across arches.

**MED / INFERRED**

- The device collapsing of `DMA_TRANSPOSE` (`dge_op==2`) under the gather-transpose decode arm
  (3 device log lines vs 4 ISA enum values; shared `tensor_reshape_indirect_transpose` builder),
  §9.1.
- The exact BD-word bitfield each `GENERATE`/`DIMPUSH`/`REGWRITE` push produces per kind, and
  the precise per-kind backend (Pool/RTL/software) chosen at runtime — the
  [descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md) scope.
- `v2`-vs-`v3` translator selection being a NEFF/ABI version gate.
- MAVERICK (v5) interiors beyond the header text (struct region byte-identical to v3/v4; no v5
  binary disassembled this session).

**LOW / NOT CLAIMED**

- The concrete TDRTP/RDRTP tail-increment CSR numbers per DMA queue.
- The full byte-exact body of the per-kind translators
  (`translate_pseudo_dma_gather_xpose_instr_v3` etc.) beyond the dispatch + the buffer-zeroing
  prologue — DWARF names + dispatch + priority copy proven; descriptor-emit bodies not decoded
  line-by-line.

---

## See also

- [S3D3 Collective (`0xBF`, SB2SB)](s3d3-collective.md) — the *collective* (cross-core) gather.
- [Collective-type + cc_op enum reference](collective-enums.md).
- [Trigger Collective 2 extension (`0xDA`)](trigger-collective2-ext.md) — the other `0xda`-shared extension.
- [DMA descriptor model (the six 64-B words)](../../dma/descriptor-model.md).
- [Gather/scatter descriptors (`0xbb`/`0xf1`/`0x68`/`0xe7`)](../../dma/gather-scatter-descriptors.md) — the Pool subset gather.
- [DGE micro-op encoding (GENERATE/DIMPUSH/REGWRITE, priority band)](../../dma/dge-microop-encoding.md).
- [Descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md).
- [Firmware indirection engine (`do_indirection`/`gather_indices`)](../../firmware/kernels/indirection-gather.md).
- [VAL-15 gather/scatter differential](../../validation/gather-scatter.md) (planned).
