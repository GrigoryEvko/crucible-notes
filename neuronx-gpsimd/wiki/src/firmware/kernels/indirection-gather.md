# The Indirection Engine (gather / scatter / indirect-copy / embedding-update)

The GPSIMD "indirection engine" is not a single kernel. It is a *family* of index-tensor
primitives — table gather, scatter-by-index DMA, indexed copy, and embedding scatter-add —
realised by **three distinct gather/scatter mechanisms** that coexist on the Vision-Q7
(`ncore2gp`) POOL core: a **native two-phase IVP hardware vector-gather**, a **software
per-index loop** that drives a vectorised bounds-checked address generator, and a **DGE
descriptor-generation path** that lowers a 5-D indexed access pattern into indirect-DMA
descriptors. This page documents the opcodes, their 64-byte operand structs, the index dtype
and out-of-bounds behaviour, all four datapaths as annotated C pseudocode, and the exact
**2:1 NKI name → opcode split** that joins the compiler frontend to the two SUNDA POOL
kernels.

Everything below is grounded in the shipped binaries and headers. The operand structs and
enums are read byte-for-byte from the customop-lib arch-isa headers
(`neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/*.h`); the kernel funcVAs from the
SUNDA `EXTISA_0` `kernel_info_table`; the IVP micro-ops from the native `ncore2gp` Xtensa
disassembly (`extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`,
Binutils 2.34.20200201 / Xtensa Tools 14.09, `XTENSA_CORE=ncore2gp`); the NKI lowering from
the shipped plain-python `nki-0.3.0` frontend and the `neuronx-cc` `.pyi` stubs. Confidence
tags are per-claim: `HIGH/OBSERVED` = direct byte/header read; `MED/INFERRED` = strong
inference across a FLIX/literal-pool desync; `CARRIED` = OBSERVED in a cited report, reused
here.

---

## 1. The five indirection opcodes

The `NEURON_ISA_TPB_OPCODE` enum (`aws_neuron_isa_tpb_common.h`; the six opcode values below are
byte-identical across all four arch generations — only line numbers shift) defines five
index-tensor opcodes plus two compiler-emitted pseudo forms. The struct→opcode binding is read
directly from the shipped `instruction_mapping.json` `struct2opcode` table. [HIGH/OBSERVED]

| Opcode | Num | NISA struct | Mechanism | Role |
| ------ | --- | ----------- | --------- | ---- |
| `GATHER` | `0x68` (104) | `S4D4_GT_STRUCT` | C (NKI `nc_n_gather`) / HW | `dst[k] = Pool_Buffer[index[k]]` table gather |
| `INDIRECT_COPY` | `0xe7` (231) | `S4D4_IC_STRUCT` | B (NKI `local_gather`) | indexed copy `src→dst`, per-index row |
| `DMA_INDIRECT` | `0xbb` (187) | `DMA_INDIRECT1D_STRUCT` | C / DMA | gather/scatter-by-index DMA (the canonical one) |
| `EMBEDDING_UPDATE` | `0x79` (121) | (SEQ handler `'y'`; no `struct2opcode` key) | C / DMA | embedding scatter-reduce (raw SEQ form) |
| `DMAMEMCPY` | `0xb8` (184) | reached via `DMA_DIRECT2D_STRUCT` | — | direct DMA memcopy (the non-indexed sibling) |

Compiler-emitted pseudo forms (NRT-translated at runtime to `WRITE32` + DMA descriptors):

| Pseudo opcode | Num | NISA struct |
| ------------- | --- | ----------- |
| `PSEUDO_EMBEDDING_UPDATE` | `0xca` (202) | `PSEUDO_EMBEDDING_UPDATE_STRUCT` |
| `PSEUDO_DMAMEMCPY_FULL_IND` | `0xc4` (196) | `PSEUDO_DMA_MEMCPY_FULL_IND_STRUCT` |

The byte-exact `struct2opcode` rows, read with `jq` from
`neuron_sunda_arch_isa/tpb/instruction_mapping.json` (the cayman/mariana/maverick copies give the
same struct→opcode-name rows): [HIGH/OBSERVED]

```text
NEURON_ISA_TPB_S4D4_GT_STRUCT                 -> NEURON_ISA_TPB_OPCODE_GATHER                  (0x68)
NEURON_ISA_TPB_S4D4_IC_STRUCT                 -> NEURON_ISA_TPB_OPCODE_INDIRECT_COPY           (0xe7)
NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT          -> NEURON_ISA_TPB_OPCODE_DMA_INDIRECT            (0xbb)
NEURON_ISA_TPB_PSEUDO_EMBEDDING_UPDATE_STRUCT -> NEURON_ISA_TPB_OPCODE_PSEUDO_EMBEDDING_UPDATE (0xca)
NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT            -> NEURON_ISA_TPB_OPCODE_DMA_MEMCPY              (0xb8)
```

> **CORRECTION.** The raw `EMBEDDING_UPDATE` opcode is `0x79`, but the customop/compiler-facing
> struct (`PSEUDO_EMBEDDING_UPDATE_STRUCT`) maps in `struct2opcode` to **`0xca`**, not `0x79`.
> The `0x79` form is the *SEQ-side* handler (`'y'` in the NX_POOL 178-entry sequence table) and
> has **no `struct2opcode` key**; the `0xca` pseudo is what the ISA emitter writes and NRT expands
> at runtime into a `LoadPoolArgument`/`EmbeddingUpdate`/`TensorStore` sequence. Both bytes
> describe the same embedding scatter-reduce; they live at different lowering stages. SX-FW-74 §0
> tagged the struct with `0x79`; this page corrects the struct→pseudo binding to `0xca` from
> `struct2opcode`, with `0x79` retained as the raw SEQ opcode. [HIGH/OBSERVED]

### The SUNDA POOL kernel map (byte-exact `kernel_info_table`)

On SUNDA (`Xm_ncore2gp`, NC-v2), the full POOL-kernel bodies ship in the `EXTISA_0`
`kernel_info_table` at VMA `0x02000760` (18 entries × 8 bytes, format
`{u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA}`). The indirection rows: [HIGH/OBSERVED]

| Opcode | Kernel name | funcVA | Mechanism / struct |
| ------ | ----------- | ------ | ------------------ |
| `0xe7` | `pool_indirect_copy` | `0x01000748` | B (SW per-index loop), `S4D4_IC` |
| `0x74` | `pool_tensor_scalar_addr` | `0x01001904` | A (HW gather, 21 sites) |
| `0x68` | `pool_gather` | `0x01002590` | gather (`S4D4_GT`) |
| `0xb8` | `dma_memcopy` | `0x01002f80` | direct DMA (sibling) |
| `0xbb` | `dma_memcopy_indirect` | `0x0100474c` | A (HW gather, 33 sites), `DMA_INDIRECT1D` |
| `0x79` | `pool_embedding_update` | `0x01008520` | embedding scatter-reduce |
| `0x7e` | `pool_iota` | `0x01005e80` | the index/iota *producer* |

The inner workers, named from the SUNDA `.xt.prop.<mangled>` func-start records (first 4 bytes
= LE VMA; demangled via `c++filt`): [HIGH/OBSERVED]

- `ic_util::send_gather_request(NEURON_ISA_TPB_ADDR4, unsigned short*, unsigned int, NEURON_ISA_TPB_DTYPE, unsigned int, bool)` @ `0x01000788`
- `embedding_update(embedding_update_lib::embed_update_info*, unsigned short)` @ `0x01008540`

> **GENERATIONAL FINDING.** The CAYMAN (NC-v3) `EXTISA_0` `kernel_info_table` (17 entries,
> @`0x02000380`) registers **none** of `0x68`/`0xe7`/`0xbb`/`0x79` — its kernels are
> iota/cross-lane-reduce/pool/tensor-tensor/dequant/nonzero only. The CAYMAN arch-isa *headers*
> still **define** all four indirection structs (the ISA supports them) and the CAYMAN SEQ
> NX_POOL table carries the `'h'`/`'y'`/`0xe7` handlers, but on CAYMAN the gather/indirect path is
> served by the **DGE descriptor-generation layer** (`do_indirection`/`gather_indices`, present in
> the Q7 POOL DEBUG + DKL builds) rather than a dedicated POOL kernel. **SUNDA is the only
> generation that ships the dedicated POOL gather/indirect kernel implementations** in its
> `kernel_info_table`. [HIGH/OBSERVED — SUNDA 18-entry table has them, CAYMAN 17-entry does not]
>
> **Per-gen header presence and divergence.** All four generations (sunda=NC-v2, cayman=NC-v3,
> mariana=NC-v4, maverick=NC-v5) ship the `s4d4_gt.h` / `s4d4_ic.h` / `dma_indirect1d.h` /
> `pseudo_embedding_update.h` headers and the same `struct2opcode` rows — but the **headers are
> not byte-identical across gens** (every gen's `s4d4_gt.h` has a distinct SHA256). The
> 64-byte struct *layouts* match; the *validity contracts* drift. The v5 (MAVERICK) `s4d4_gt.h`
> differs from v2 in two semantic ways: (i) the active-channel check becomes tile-aware
> (`has_valid_active_channel_range_with_tile(..., header.inst_flags)`), and (ii) **PSUM is removed
> for gather on v5** — both src and dst `tensor4d_valid` change `AllowedInPSUM::True →
> AllowedInPSUM::False`, so on NC-v5 a `GATHER` can no longer source or sink PSUM (SBUF-only),
> whereas v2 permits PSUM. The v2–v4 struct/enum/kernel facts are byte-grounded; the **MAVERICK
> (v5) interior is header-OBSERVED only** — no v5 POOL kernel body was decoded, so v5 kernel-side
> behaviour beyond the header contracts is **INFERRED** from the shared ISA. [HIGH/OBSERVED
> headers (incl. the v5 divergence); v5 kernels INFERRED]

---

## 2. The two NKI gather lowerings — the 2:1 name→opcode split

The NKI frontend exposes **two** gather spellings, and they lower to **two different opcodes**.
This is the single most important binding for a reimplementation: get it wrong and a
`local_gather` program assembles a `Gather 0x68` instruction whose `Pool_Buffer`-relative
semantics differ from the `IndirectCopy 0xe7` row-copy the program actually means.

The routing is read verbatim from `nki/backends/mlir_tracer/isa.py` (shipped plain python,
`nki-0.3.0`): [HIGH/OBSERVED]

```python
# nki/backends/mlir_tracer/isa.py:853
def local_gather(dst, src_buffer, index, num_elem_per_idx, num_valid_indices, name):
    """Emit MLIR local_gather operation (maps to IndirectCopy in NISA dialect)."""
    ...
        emit_indirect_copy(dst=dst, src=src_buffer, index=index, ...)   # -> INDIRECT_COPY 0xe7

# nki/backends/mlir_tracer/isa.py:1238
def nc_n_gather(dst, data, indices, name):
    """Emit MLIR gather operation."""
    ...
        emit_gather(dst=dst, src=data, indices=indices, ...)            # -> GATHER 0x68
```

So the two NKI names map onto the two gather opcodes thus: [HIGH/OBSERVED]

| NKI op | `emit_*` → irbuilder mnemonic | TPB opcode | NISA struct | SUNDA kernel | Index dtype |
| ------ | ---------------------------- | ---------- | ----------- | ------------ | ----------- |
| `local_gather` | `emit_indirect_copy` → `indirect_copy` | `INDIRECT_COPY 0xe7` | `S4D4_IC` | `pool_indirect_copy` @`0x748` | **uint16** |
| `nc_n_gather` | `emit_gather` → `gather` | `GATHER 0x68` | `S4D4_GT` | `pool_gather` @`0x2590` | **uint32** |

The verbatim docstring `"maps to IndirectCopy in NISA dialect"` on `local_gather` is the
decisive disambiguator — the op whose *name* says "gather" lowers to **IndirectCopy**, while the
op named `nc_n_gather` lowers to the literal **Gather**. The index dtypes are read from
`nki/isa/gather.py`: `local_gather` — *"The indices in `index` tile must be uint16 types"*
(line 56); `nc_n_gather` — *"The `indices` tile must be uint32"* (line 136). [HIGH/OBSERVED]

> **GOTCHA (logged to the [correction ledger](../../reference/correction-ledger.md)).** The
> NKI gather name and the opcode it lowers to are *crossed*: `local_gather → 0xe7 IndirectCopy`
> and `nc_n_gather → 0x68 Gather`. The intuitive reading — that the op spelled with "gather"
> maps to the opcode spelled `GATHER` — is **wrong** for `local_gather`. A reimplementation that
> binds `local_gather → 0x68` will emit a `Pool_Buffer`-subset gather where the program intends a
> per-index row copy from an arbitrary SBUF buffer. Bind by the `emit_*` callee, not the verb.

The architectural difference behind the split: `nc_n_gather`/`GATHER 0x68` is a **within-partition
flat gather** from a `Pool_Buffer` that a prior `POOL_BUFFER_LOAD 0x67` filled (≤512 elements per
channel, `POOL_BUFFER_MAX_ELEMENTS = 512`; the ISA-group count `n = ceil(elems_per_partition/512)`);
`local_gather`/`IndirectCopy 0xe7` is an **8-core / 16-partition** indexed row-copy where each of
the eight GpSimd cores gathers independently from its 16 contiguous SBUF partitions.
[HIGH/OBSERVED — `nki/isa/gather.py` and the headers]

---

## 3. The operand structs (byte-exact, 64 bytes each)

All four structs are 64 bytes (`ISA_STATIC_ASSERT(sizeof == 64)` in every header). Read from
`neuron_sunda_arch_isa/tpb/`; the struct *layouts* match across gens, the validity contracts
drift (see the v5 PSUM divergence in §1). [HIGH/OBSERVED]

### 3a. GATHER — `S4D4_GT_STRUCT` (opcode `0x68`)

| Off | Sz | Field | Type | Notes |
| --- | -- | ----- | ---- | ----- |
| 0 | 4 | `header` | `TPB_HEADER` | `opcode = 0x68` |
| 4 | 8 | `events` | `TPB_EVENTS` | sync |
| 12 | 20 | `src_mem_pattern` | `TENSOR4D` | **the gather source table** (the loaded `Pool_Buffer`) |
| 32 | 1 | `in_dtype` | `DTYPE` | **the index dtype: UINT8 / UINT16 / UINT32** |
| 33 | 1 | `out_dtype` | `DTYPE` | gathered element dtype |
| 34 | 1 | `num_active_channels` | `uint8` | ≤ `POOLING_NUM_CHANNELS` (128) |
| 36 | 1 | `index_miss_behavior` | `INDEX_MISS_BEHAVIOR` | **OOB handling** |
| 37 | 1 | `free_pool_buffer` | `uint8` | dealloc `Pool_Buffer` after this gather |
| 40 | 4 | `immediate` | `IMM_VAL_INST_FIELD` | fill value for `IMMEDIATE_WRITE` on miss |
| 44 | 20 | `dst_mem_pattern` | `TENSOR4D` | gather destination |

`TENSOR4D = { ADDR4 start_addr; int16 step_elem[4]; uint16 num_elem[4] }` — base plus four
`(stride, count)` pairs. The validity predicate `has_gather_index_dtype` allows
`in_dtype ∈ {UINT8=0x3, UINT16=0x5, UINT32=0x9}`. On v2 src/dst may be SBUF or PSUM; on v5 both
are SBUF-only (§1). [HIGH/OBSERVED]

> **NOTE on the index width.** The `S4D4_GT` *prose comment* says *"the `src_mem_pattern` must
> have dtype uint16 or uint32"*, but the machine-checkable validity function `has_gather_index_dtype`
> admits **UINT8 as well**. The NKI `nc_n_gather` surface narrows this further to **UINT32 only**.
> So the hardware/ISA accepts `{u8,u16,u32}`, the firmware-validator accepts `{u8,u16,u32}`, and the
> compiler emits `u32`. A reimplementation should accept all three at the ISA layer. [HIGH/OBSERVED]

### 3b. INDIRECT_COPY — `S4D4_IC_STRUCT` (opcode `0xe7`)

| Off | Sz | Field | Notes |
| --- | -- | ----- | ----- |
| 12 | 20 | `src_mem_pattern` | `MEM_PATTERN4D` — **indirect** pattern (`.i.p`), SBUF-only, no PSUM |
| 32 | 1 | `in_dtype` / 33 `out_dtype` | must match (`in == out`, no cast) |
| 34 | 1 | `num_active_channels` | **must be a multiple of 16**; start partition ∈ {0,32,64,96} |
| 35 | 1 | `src_num_elem_per_idx` | **contiguous elements read per index** ∈ {1,2,4,8,16,32} |
| 36 | 1 | `dst_num_elem_per_idx` | scatter case (unsupported) — **must be 0** for gather |
| 38 | 2 | `src_buffer_size` | **the source bounds** (> 1) |
| 40 | 2 | `dst_buffer_size` | dest bounds — **must be 0** for gather |
| 44 | 20 | `dst_mem_pattern` | `MEM_PATTERN4D` — tensor pattern (`.t`) |

`MEM_PATTERN4D` is a union of `TENSOR4D t` and `INDIRECT20B i`, where the indirect form carries:

```c
typedef struct NEURON_ISA_TPB_INDIRECT {   // the .i.p member, 12 B
    NEURON_ISA_TPB_ADDR4 index_addr;       // starting address of the index tensor (SBUF)
    NEURON_ISA_TPB_ADDR4 data_addr;        // src buffer starting address (SBUF)
    uint16_t             num_elem;         // total elements per partition to read
    uint8_t              reserved[2];
} NEURON_ISA_TPB_INDIRECT;
```

The header's own constraint chain (read verbatim from the `is_valid_indirect_copy` assertion
block): only the **Gather** sub-mode is implemented — `src` is an indirect pattern, `dst` is a
tensor pattern; `dst_num_elem_per_idx == 0`, `dst_buffer_size == 0`; the per-index row width
`src_num_elem_per_idx ∈ {1,2,4,8,16,32}`; the index count
`src.i.p.num_elem / src_num_elem_per_idx ≤ 4096` (a documented temporary restriction).
**Scatter and Gather+Scatter are explicitly "not implemented yet."** [HIGH/OBSERVED]

### 3c. DMA_INDIRECT — `DMA_INDIRECT1D_STRUCT` (opcode `0xbb`, `DGE_OPCODE DMA_INDIRECT1D = 0x1`)

The canonical gather/scatter-by-index DMA. Header doc: *"DmaIndirect performs DMAs for a vector
of dynamic indices (offsets) generated during execution … applied to the source to perform a
gather, or applied to the destination to perform a scatter, or two sets of independent indices
for both gather and scatter in one instruction."* [HIGH/OBSERVED]

| Off | Sz | Field | Notes |
| --- | -- | ----- | ----- |
| 14 | 1 | `idx_num_active_channels` | |
| 15 | 1 | `flags` | `DMA_INDIRECT_FLAGS` (below) |
| 16 | 8 | `src_start_addr` (`ADDR8`) / 24 `src_step_elem` (i32) / 28 `src_num_elem` (u16) / 30 `src_elem_size` (u16) | |
| 32 | 8 | `dst_start_addr` (`ADDR8`) / 40 `dst_step_elem` (i32) / 44 `dst_num_elem` (u16) / 46 `dst_elem_size` (u16) | |
| 48 | 4 | `src_idx_start_addr` (`ADDR4`) | **the gather index tensor** |
| 52 | 4 | `dst_idx_start_addr` (`ADDR4`) | **the scatter index tensor** |
| 56 | 1 | `in_dtype` / 57 `out_dtype` | |
| 58 | 1 | `src_idx_bound_reg` | `BOUND_CHECK_REG` — register-based bound on src index |
| 59 | 1 | `dst_idx_bound_reg` | `BOUND_CHECK_REG` — register-based bound on dst index |
| 60 | 1 | `compute_op` | `DGE_COMPUTE_OP` — **reduce mode (scatter-add)** |
| 61 | 1 | `dma_configs` (reserved=0) / 62–63 `reserved[2]` | |

`DMA_INDIRECT_FLAGS` (1-byte bitfield, byte-exact from the header): [HIGH/OBSERVED]

```c
typedef struct NEURON_ISA_TPB_DMA_INDIRECT_FLAGS {
    INDIRECT_DMA_ADDRESSING_MODE indirect_mode : 2;   // SRC_INDIRECTION=0 (gather),
                                                      // DST_INDIRECTION=1 (scatter),
                                                      // SRC_DST_INDIRECTION=2 (both)
    uint8_t                      idx_bound_is_err : 1; // OOB is a hard error vs soft
    uint8_t                      non_unique_dst_idx : 1; // allow duplicate scatter targets
                                                          //   — REQUIRED for scatter-add
    INDIRECT_DIM                 gather_dim : 2;       // X=0,Y=1,Z=2,W=3
    INDIRECT_DIM                 scatter_dim : 2;
} NEURON_ISA_PACKED;
```

`BOUND_CHECK_REG` (1 byte): `{ bc_reg:6 (register holding the index upper bound; bc_reg+1 = high
32 bits for wide offsets), bc_disable_oob_error_notif:1, bc_enabled:1 }`.
`DGE_COMPUTE_OP`: `NONE=0` (`B=A` copy), `ADD=1` (`B+=A`, **scatter-ADD**), `MULTIPLY=2`,
`MAX=3`, `MIN=4`. [HIGH/OBSERVED]

Two structural constraints, read from the validity block: the index count is `≤ 4096`
(`has_dma_indirect_valid_index_count`), and the index tensor **must live in SBUF partition 0**
(`has_dma_indirect_idx_addr_in_sbuf_partition0`) — the indices sit in the first SBUF partition
row. When no shape register is used, the `indirect_dim` must be `X` (the only available dim).
[HIGH/OBSERVED]

### 3d. EMBEDDING_UPDATE — `PSEUDO_EMBEDDING_UPDATE_STRUCT` (raw `0x79` / pseudo `0xca`)

| Off | Sz | Field | Notes |
| --- | -- | ----- | ----- |
| 12 | 1 | `embed_data_dtype` / 13 `cce_op` (`CCE_OP`) | the scatter-reduce op |
| 16 | 8 | `embed_index_in` | `TENSOR1D` — **the index tensor (which rows)** |
| 24 | 12 | `embed_data_in` | `TENSOR2D` — the gradient/data to scatter |
| 36 | 8 | `desc_mem_pattern` | `TENSOR1D` |
| 44 | 4 | `embedding_table_base_addr` | **the embedding table base** |
| 48 | 2 | `embed_entry_num_elements` | row width (elements per entry) |
| 50 | 2 | `embed_entry_stride_bytes` / 60 `_hi` | row stride: `table[index] = base + index·stride` |
| 52 | 2 | `sequence_length` | number of indices to process |
| 56 | 4 | `embedding_table_num_entries` (`ADDR4`) | **the bound (row count = index max)** |

Header doc: *"High level instruction that expands in runtime a sequence of
`LoadPoolArgument`/`EmbeddingUpdate`/`TensorStore` to configure DMA engines and execute the
embedding update operation"* — i.e. `embedding_update` is a **meta/pseudo op** driving the
DGE+DMA. `CCE_OP` (same encoding as the SDMA CCE op): `ADD=0x00`, `MAX=0x02`, `MIN=0x03`
(`Multiply=0x01` commented out) — the scatter-reduce for embedding-gradient accumulation.
[HIGH/OBSERVED]

---

## 4. Mechanism A — the IVP hardware vector-gather (SuperGather)

The Vision-Q7 (`ncore2gp`) IVP ISA has a **native two-phase vector gather**, the on-core front
door to the external **SuperGather** memory engine (the full vector ISA batch is documented in
[ISA Batch 19](../../isa/ref/b19-scatter-gather.md)). The native disassembly shows the gather
A-phase always issued in a FLIX bundle paired with a dual-select drain — e.g. at IRAM
`0x1435e`: `{ ivp_gatheran_2x32t gr4, a1, v0, vb4 ; nop ; nop ; ivp_dseln_2x32t v17,… }`, at
`0x14ebb`: `{ ivp_gatheranx16t gr7, a9, v24, vb3 ; … ; ivp_dselnx16t v1,… }`, at `0x1b1df`:
`{ ivp_gatheranx8ut gr5, a4, v1, vb0 ; … ; ivp_dselnx16t v8,… }`. The two phases: [HIGH/OBSERVED]

**Phase 1 (post addresses)** — SUNDA IRAM `0x010019f0`, bytes `0ff86008006f9f02`:

```text
{ ivp_gatheran_2x32t  gr0, a8, v31, vb0 ; nop ; nop }
    gr0  = the gather-staging register (the gather FIFO; one of gr0..gr7)
    a8   = the gather BASE address (the table base)
    v31  = a VECTOR of 16 × 32-bit element OFFSETS (index·stride per lane)
    vb0  = the PREDICATE MASK (per-lane in-bounds; the 't' = predicated form)
```

**Phase 2 (drain results)** — SUNDA IRAM `0x010019f8`, bytes `6f03e03c2a00a202`:

```text
{ beqz.w15 a3, ... ; ivp_gatherdnx16  v3, gr0 }
    v3   = the gathered nx16 (16-bit) elements drained from gr0
```

The `*_2x32t` / `*nx16` / `*nx8ut` names are the width/predication variants of the
[B19](../../isa/ref/b19-scatter-gather.md) `gathera*` (A-phase, posts per-lane addresses + a
validity predicate into a `gvr` staging register `gr0..gr7`) and `gatherd*` (D-phase, drains the
collected elements back to a `vec`). The defining property is the per-lane indirect address
**`addr[k] = base + offset[k]·elem_sz`** computed inside the core and handed, together with a
per-lane validity predicate, to a memory port the rest of the pipeline cannot see compute values
flow into. Out-of-bounds lanes (predicate bit 0) are simply **not gathered**. [HIGH/OBSERVED]

```c
/* Mechanism A: the native two-phase IVP hardware vector-gather.
 * Lowered from ivp_gatheran_2x32t / ivp_gatheranx16t / ivp_gatheranx8ut (A-phase)
 *           + ivp_gatherdnx16 (D-phase), drained via ivp_dselnx16t dual-select.
 * Used by dma_memcopy_indirect @0x474c (33 sites) and pool_tensor_scalar_addr @0x1904 (21).
 */
static inline vec16_t ivp_hw_gather(addr_t base, vec32_t offsets, vbool_t pred,
                                    unsigned elem_sz) {
    gvr_t gr;                                 /* 512-bit gather-staging register */
    /* PHASE 1 — ivp_gatheran*t gr, base, offsets, pred:
     *   for each lane k, if pred[k]: post addr[k] = base + offsets[k]*elem_sz to the
     *   memory port; else mark lane k dead (it will read as the drain's miss value). */
    for (int k = 0; k < VLEN; ++k)
        if (pred.bit[k]) gr.addr[k] = base + (uint64_t)offsets.u32[k] * elem_sz;
        else             gr.dead[k] = 1;      /* OOB lane: not gathered */
    /* PHASE 2 — ivp_gatherdnx16 v, gr: drain the collected elements into a vec. */
    vec16_t v;
    for (int k = 0; k < VLEN; ++k)
        v.u16[k] = gr.dead[k] ? 0 : load16(gr.addr[k]);   /* dead lanes -> 0 */
    return v;
}
```

The companion IVP vocabulary in `dma_memcopy_indirect` builds the predicate from the bound:
`ivp_leun_2x32` (9 sites, unsigned `≤` bounds), `ivp_andb` (14, mask AND), `ivp_eqn_2x32` (8),
`ivp_mov2nx8t` (15, predicated select for the OOB fill); the drain is a `ivp_dselnx16t`
dual-select (the dominant op in the gather-resident bodies). The ISS per-lane oracle for the same
op is `ivp_gatheranx8ut` (the nx8 unsigned variant) in `libfiss-base.so`. [HIGH/OBSERVED]

> **NOTE.** The exact 1:1 per-op IVP binding inside the `dma_memcopy_indirect` body sits in
> FLIX/VLIW bundles that partly desync under the linear sweep; the IVP **vocabulary** and the
> two-phase gather/predicate/drain **structure** are recovered with confidence, but the precise
> per-instruction selection arithmetic inside the desynced spans is reported structurally, not
> fabricated. The gather A-phase + `dselnx16t` drain pairing is OBSERVED in the disassembly on v2
> (SUNDA) and v4 (MARIANA) firmware alike. [MED/INFERRED for the per-instruction span; HIGH for
> the named gather ops and the pairing]

---

## 5. Mechanism B — the software per-index loop (`pool_indirect_copy`)

`local_gather`/`IndirectCopy 0xe7` is served on SUNDA by a **software per-index loop**. The
dispatcher stub `pool_indirect_copy` @ `0x01000748` partitions work across the eight GpSimd cores
by processor-ID and calls a worker; the worker loops over the index count computing
`base + index·stride` per element and issuing `send_gather_request`, which performs a
**vectorised bounds-checked address generation**. [HIGH/OBSERVED]

The dispatcher stub, byte-exact (SUNDA binary-mode decode):

```text
0748: entry a1,32
0752: l32i a3, a0, 124       ; read this build's CPU/core count from the glob struct
0763: slli a2, a2, 4         ; scale this core's index by 16 (the 16-partition slice)
0766: bgeu a2, a3, 0x772     ; if this core is out of range -> skip
076c: const16 a2, 0x428      ; the per-cpu-id partitioned worker
076f: callx8 a2              ; -> run the indirect-copy worker
0774: retw.n
; 0781: rsr.prid a3          ; the per-pool-core processor-ID read = get_cpu_id()
```

The index loop @ `0x180` (byte-exact): the worker selects the element byte-width from the dtype
(`beqi a7,2` / `bnei a7,3` → `const16 a2, 1/2/4`), then loops over the index count `a11`:

```text
0271: { mull a2, a2, a5 ; l32i a8, a1, 24 }   ; a2 = stride * index(a5)
027d: { addx2 a2, a2, a8 ; ... }              ; a2 = base(a8) + 2*(stride*idx)  == base + index*stride
0285: { ... ; mov.a a11, a2 ; mov.a a15, a4 } ; the computed addr -> arg
0295: const16 a8, 0x788                        ; ic_util::send_gather_request
0298: callx8 a8                                ; *** issue the gather request
029e: addi.a a5, a5, 1                         ; index++
; bgeu.w15 a5, a11 @0x266 is the loop-bound test
```

```c
/* Mechanism B: pool_indirect_copy software per-index loop (IndirectCopy 0xe7).
 * Dispatcher @0x748 partitions by core; worker @0x180 loops; send_gather_request @0x788
 * does the vectorised bounds-checked address-gen.  Index dtype = uint16 (NKI local_gather).
 */
void pool_indirect_copy(const S4D4_IC *ic) {
    unsigned core = rsr_prid() & CORE_MASK;             /* @0x781 get_cpu_id() */
    if (core * 16 >= glob.num_chans) return;            /* @0x763/0x766 out-of-range skip */
    const uint16_t *idx = sbuf_ptr(ic->src.i.p.index_addr);  /* indices in SBUF, part 0/32/64/96 */
    addr_t          base = sbuf_ptr(ic->src.i.p.data_addr);
    unsigned        esz  = dtype_bytes(ic->in_dtype);   /* 1/2/4 from the dtype enum */
    unsigned        n    = ic->src.i.p.num_elem / ic->src_num_elem_per_idx;  /* index count <= 4096 */
    for (unsigned i = 0; i < n; ++i) {                  /* worker loop @0x266..0x29e */
        addr_t a = base + (addr_t)idx[i] * (esz * ic->src_num_elem_per_idx); /* @0x271/0x27d */
        send_gather_request(a, /*idx_desc=*/&idx[i], /*count=*/ic->src_num_elem_per_idx,
                            ic->in_dtype, /*stride=*/esz, /*flag=*/false);    /* @0x295/0x298 */
    }
}
```

`ic_util::send_gather_request` @ `0x01000788` is the per-request **vector bounds-check core**.
Its body maps the DTYPE enum to a 1/2/4/8-byte width, walks the multi-dim index descriptor
(`l16si` lo/hi pairs, `mul16u` + `add` to accumulate `index_component·stride + base`), then runs
the vector bounds-check at `0x0bee..0x0ca0`: [HIGH/OBSERVED]

```c
/* ic_util::send_gather_request @0x788 — vectorised bounds-checked address-gen.
 * The bounds-check core (FLIX bundles @0x0bee..0x0ca0):
 */
void send_gather_request(addr_t base, uint16_t *idx_desc, unsigned count,
                         DTYPE dtype, unsigned stride, bool last) {
    /* per-dim descriptor accumulate @0x7d2: addr += idx_component*stride + base */
    /* vector bounds-check + predicated select per dimension: */
    vbool_t vb0 = ivp_ltun_2x32(v0, v1);   /* (index < bound) unsigned — IN-RANGE check */
    vec_t   off = ivp_subn_2x32(v3, v5);   /* the offset */
    v30 = ivp_mov2nx8t(v4, v2, vb0);       /* predicated select: in-range -> gathered, else clamp */
    vb1 = ivp_eqn_2x32(v0, v6);
    vb0 = ivp_andb(vb1, ivp_ltun_2x32(v1, v7));  /* combine per-dim masks */
    vb0 = ivp_orb(ivp_ltun_2x32(v0, v6), vb0);
    v6  = ivp_dselnx16t(v6, v28, v16, v2, vb13);  /* dual lane-select the gathered result */
    /* N-dim handling re-runs {ltun, mov2nx8t, subn, andb/orb} per dimension */
}
```

The recovered IVP vocabulary of `send_gather_request`: `ivp_ltun_2x32` (38, unsigned-LT bounds),
`ivp_subn_2x32` (30, offset), `ivp_eqn_2x32` (12), `ivp_mov2nx8t` (34, predicated select),
`ivp_movva32` (17, AR→vector broadcast of base), `ivp_dselnx16t` (4, lane select),
`ivp_dseln_2x32t` (2), `ivp_lv2nx8_i` (7, strided load). [HIGH/OBSERVED for the set; per-op byte
binding MED across the FLIX desync]

> **NKI cross-check (CONFIRM).** The `local_gather` numpy reference simulator
> (`nki/backends/simulator/gather.py`) flattens each core's indices in **Fortran order
> (column-major)** — `core_idx.flatten(order="F")` — i.e. **along the partition dimension first**,
> then `np.take(core_src, indices_1d, axis=1)` per core (8 cores × 16 partitions). The output is
> pre-zeroed, and a contiguous run that overruns the buffer edge is clipped
> (`end_idx = min(idx + num_elem_per_idx, core_src.shape[1])`) with the tail left zero. This is
> exactly the SUNDA POOL `get_cpu_id()`-partitioned, partition-first model decoded here. [CONFIRM
> — sim OBSERVED vs FW kernel OBSERVED]

---

## 6. Mechanism C — the DGE descriptor-generation path (`do_indirection` / `gather_indices`)

On CAYMAN (and as the descriptor-gen layer everywhere), the indirect path is served by the **DGE
(Descriptor Generation Engine)**, which lowers a 5-D indexed access pattern into indirect-DMA
descriptors. The function identities are read from the firmware's own `P%i:` DEBUG log strings
(the only build carrying them, DRAM-resident); the bodies are FLIX-VLIW. [HIGH/OBSERVED strings;
MED bodies]

The DGE has a 3-kind dispatch (`DGE DIRECT2D` / `DGE INDIRECT` / `DGE GATHER TRANSPOSE`,
`DGE_OPCODE { DMA_DIRECT2D=0, DMA_INDIRECT1D=1 }`). The indirect transform pipeline:

- `IndirectionInfo` constructor (`"IndirectionInfo constructor: indices=%p"`)
- `gen_spray_info` — generates the DMA "spray" descriptor spread
  (`"gen_spray_info: reshape_kind=%d, do_indirection=%d, is_our_tensor=%d, hbm_to_hbm=%d, ndma=%zu"`)
- `make_gather_pattern` — builds the gather access pattern + an in-bounds mask
  (`"make_gather_pattern START: reshape_kind=%d, step_port=%d, step_half=%d, eng_mask=0x%x"`)
- `do_indirection` — the core index→address transform
  (`"do_indirection START: dim=%d, last_indices=0x%x, indirection_step=0x%x"`)
- `gather_indices` — reads the index vector with the pattern/mask
  (`"gather_indices START: indices=%p, pattern=0x%x, mask=0x%x"`)

The index→address multiply is named unambiguously by a DEBUG string pair —
`"Before IVP_MULUSAN_2X32: address=0x%x_%x, last_indices=0x%x, indirection_step=0x%x"` /
`"After IVP_MULUSAN_2X32: address=0x%x_%x"`: [HIGH/OBSERVED string]

```c
/* Mechanism C: the DGE index->address transform (do_indirection / gather_indices).
 * Index dtype = uint32 ("replacing step with sizeof(uint32_t)=4"); 64-bit address ("0x%x_%x").
 */
void do_indirection(IndirectionInfo *info, int dim) {
    /* gather_indices: read the index vector at info->indices using the make_gather_pattern
     * pattern + in-bounds mask, returning a gathered uint32 index. */
    uint32_t last_indices  = gather_indices(info->indices, info->pattern, info->mask);
    /* the index*step MAC (the named vector op): base += last_indices * indirection_step,
     * accumulated as a 64-bit address (unsigned index x signed step). */
    info->address = ivp_mulusan_2x32(info->address, last_indices, info->indirection_step);
    /* bounds: srx_idx_max / dst_idx_max + our/other_indices_valid masks built from
     * ivp_ltun_2x32 / ivp_leun_2x32 compares feeding the predicated gather/select. */
}
```

The bounds are logged as `"Indirection masks: our_indices_valid = 0x%x, other_indices_valid =
0x%x, srx_idx_max = %u, dst_idx_max = %u"` — the per-index bound is the table size
(`srx_idx_max`/`dst_idx_max`); the valid masks mark which lanes are in-bounds, enforced by the
`ivp_ltun/leun_2x32` predicates feeding the gather. The DGE index dtype is **uint32**, proven by
`"shared_indirection_dim=%d, replacing step with sizeof(uint32_t)=%d"` (step replaced with
`sizeof(uint32_t) = 4`); all address-gen ops are `*_2x32` (32-bit lanes). The exact `ncore2gp`
mnemonic the string names `IVP_MULUSAN_2X32` (one of `ivp_mulus*`/`ivp_mulan_2x32c` in the FLIX
bundle) is MED; the **operation** — unsigned-index × signed-step MAC, base accumulate — is HIGH
from the string. [HIGH/OBSERVED string; exact encoding MED]

> **CORRECTION — "Batman" resolved.** `"Batman"` appears in exactly two DEBUG strings —
> `"Tensor shape before Batman: num_elem=[%u×5] step_elem=[%d×5]"` and
> `"shared_indirection_dim in Batman: %d"` — and **nowhere else** in either
> `libnrtucode_internal.so` or `libnrtucode_extisa.so`. It is **not a kernel name and not a
> separate engine**: it is the internal codename for the indirection descriptor-generation
> **reshape transform** inside `do_indirection`, logged immediately before the 5-D descriptor loop
> reshapes the tensor access pattern along the shared indirection dim. [HIGH/OBSERVED — the two
> strings + their single occurrence; the "reshape step" reading is INFERRED-HIGH from the string
> text and its IRAM position before the descriptor loop]

The gather+transpose descriptor kind (`DGE GATHER TRANSPOSE`) is built by
`tensor_reshape_indirect_transpose`, which tiles the source rows/cols and transposes during the
indirect gather — the compute-side counterpart to the collective `GATHER_TRANSPOSE`. The full
descriptor format and the `rdma_desc_gen` loop are documented in the
[gather/scatter descriptors](../../dma/gather-scatter-descriptors.md) page. [HIGH/OBSERVED strings]

---

## 7. The four datapaths

### 7a. Gather — `GATHER 0x68` / `nc_n_gather`

Gather reads an index stream and emits `dst[k] = Pool_Buffer[index[k]]`. A prior
`POOL_BUFFER_LOAD 0x67` fills up to 512 elements per channel into the `Pool_Buffer`; each index
is range-checked against the *currently loaded subset*. The header's own pseudocode: [HIGH/OBSERVED]

```c
/* GATHER 0x68 (S4D4_GT) — within-partition table gather against the loaded Pool_Buffer.
 * Index dtype uint8/uint16/uint32; element dtype = whatever PoolBufferLoad loaded (no cast).
 */
void pool_gather(const S4D4_GT *gt) {
    bool first_gather = ...;                  /* first of a grouped sequence? */
    for (size_t k = 0; k < element_count(gt->src_mem_pattern); ++k) {
        uint32_t index = read_index(gt->src_mem_pattern, k, gt->in_dtype);
        /* subset hit test against the loaded Pool_Buffer window: */
        bool     hit   = ((index & ~pool_buffer_mask) == pool_buffer_start_index);
        unsigned off   = index & pool_buffer_mask;
        if (hit) {
            write_dst(gt->dst_mem_pattern, k, Pool_Buffer[off]);
        } else if (gt->index_miss_behavior == IMMEDIATE_WRITE) {  /* OOB / not-loaded */
            write_dst(gt->dst_mem_pattern, k, gt->immediate);     /* default 0 on first gather */
        } else { /* SKIP_WRITE */
            /* leave the destination element unmodified (retain prior value) */
        }
    }
    if (gt->free_pool_buffer) pool_buffer_free();  /* dealloc after last gather of the group */
}
```

> **NKI cross-check (CONFIRM).** The `nc_n_gather` simulator clips indices to the valid range
> `np.clip(idx, 0, data.shape[1]-1)` and **zero-fills** the lanes that were out of bounds
> `np.where(out_of_bounds, 0, result)` — exactly the `index_miss_behavior == IMMEDIATE_WRITE(0)`
> semantics. The two OOB policies (`IMMEDIATE_WRITE` = write the immediate fill; `SKIP_WRITE` =
> leave the destination untouched) are the firmware realization of the same model. [CONFIRM]

### 7b. Indirect-copy — `INDIRECT_COPY 0xe7` / `local_gather`

Covered as Mechanism B above. The datapath is the per-index software loop: for each of `≤ 4096`
indices, copy `src_num_elem_per_idx` contiguous elements (∈ {1,2,4,8,16,32}) from
`data_addr + index·row_bytes` to the destination tensor, vector-bounds-checked against
`src_buffer_size`. Eight GpSimd cores each handle a 16-partition slice independently.
`in_dtype == out_dtype` (no cast). Scatter is **not implemented** (the `dst_*` fields must be 0).

> **GOTCHA — `local_gather` OOB is documented "undefined."** `nki/isa/gather.py` line 41 states
> the OOB behaviour for `local_gather` is **undefined** at the NKI surface, whereas the *simulator*
> pre-zeroes the output and clips the contiguous-run tail. The firmware `IndirectCopy` path
> bounds against `src_buffer_size` but the header does not specify the miss-value the way `GATHER`
> does. A reimplementation should treat OOB as undefined-but-bounded (no wild read) for
> `IndirectCopy`, matching the NKI contract, and not assume the simulator's zero-fill is
> guaranteed. [HIGH/OBSERVED]

### 7c. Scatter-by-index DMA — `DMA_INDIRECT 0xbb`

`DMA_INDIRECT1D` is the one instruction that does true scatter. With
`flags.indirect_mode == SRC_INDIRECTION` it gathers (indices applied to the source); with
`DST_INDIRECTION` it scatters (indices applied to the destination); with `SRC_DST_INDIRECTION`
both, using two independent index tensors. [HIGH/OBSERVED]

```c
/* DMA_INDIRECT 0xbb (DMA_INDIRECT1D) — gather/scatter-by-index DMA via the DGE.
 * Index tensor must be in SBUF partition 0; index count <= 4096.
 */
void dma_indirect1d(const DMA_INDIRECT1D *d) {
    bool gather  = d->flags.indirect_mode != DST_INDIRECTION;
    bool scatter = d->flags.indirect_mode != SRC_INDIRECTION;
    const uint32_t *sidx = sbuf_p0(d->src_idx_start_addr);   /* gather indices, partition 0 */
    const uint32_t *didx = sbuf_p0(d->dst_idx_start_addr);   /* scatter indices, partition 0 */
    unsigned n = gather ? d->src_num_elem : d->dst_num_elem; /* <= 4096 */
    for (unsigned k = 0; k < n; ++k) {
        /* bounds-check each index against the register-held upper bound (BOUND_CHECK_REG) */
        if (d->src_idx_bound_reg.bc_enabled && sidx[k] >= reg[d->src_idx_bound_reg.bc_reg]) {
            if (d->flags.idx_bound_is_err && !d->src_idx_bound_reg.bc_disable_oob_error_notif)
                raise_oob();                                 /* hard error vs soft skip */
            continue;
        }
        addr_t sa = gather  ? d->src_start_addr + (uint64_t)sidx[k]*d->src_elem_size
                            : d->src_start_addr + (uint64_t)k*d->src_step_elem*d->src_elem_size;
        addr_t da = scatter ? d->dst_start_addr + (uint64_t)didx[k]*d->dst_elem_size
                            : d->dst_start_addr + (uint64_t)k*d->dst_step_elem*d->dst_elem_size;
        dma_reduce(da, sa, d->dst_elem_size, d->compute_op); /* NONE=copy, ADD=scatter-add, ... */
    }
}
```

When `compute_op != NONE`, this is a **reduce-scatter** (`ADD` = scatter-add, `MAX`/`MIN`/`MUL`
the others). Duplicate scatter targets are only legal with `flags.non_unique_dst_idx` set —
which is **required for scatter-add** to accumulate multiple sources into one destination row.
[HIGH/OBSERVED]

### 7d. Embedding-update — scatter-ADD accumulate (`EMBEDDING_UPDATE 0x79` / `0xca`)

`embedding_update` is the embedding-lookup / embedding-gradient scatter-reduce primitive: for
each of `sequence_length` indices, compute the table row address and **scatter-reduce** the data
into that row via `cce_op` (`ADD` for gradient accumulation). The SUNDA worker
`embedding_update(embed_update_info*, uint16)` @ `0x01008540` (`entry a1,0x3c0`; reads
`sequence_length` at struct offset 52; loops with `ivp_sv2nx8_i` scatter-store + float MAC ops)
realizes this; the header confirms it expands at runtime into a DMA-driving sequence.
[HIGH struct+strings; MED that the device loop is exactly this — FLIX]

```c
/* EMBEDDING_UPDATE (PSEUDO_EMBEDDING_UPDATE) — scatter-ADD into embedding rows.
 * Worker embedding_update() @0x8540; reads sequence_length @off52; cce_op=ADD/MAX/MIN.
 */
void embedding_update(const PSEUDO_EMBEDDING_UPDATE *e) {
    for (unsigned i = 0; i < e->sequence_length; ++i) {       /* loop @0x85fd */
        uint32_t index = read_index(e->embed_index_in, i);    /* TENSOR1D: which row */
        if (index >= e->embedding_table_num_entries) continue; /* bound = row count */
        addr_t   row = e->embedding_table_base_addr
                     + (uint64_t)index * e->embed_entry_stride_bytes;   /* table[index] */
        for (unsigned j = 0; j < e->embed_entry_num_elements; ++j) {
            data_t a = read_data(e->embed_data_in, i, j);     /* TENSOR2D gradient/data */
            switch (e->cce_op) {                              /* the scatter-reduce */
              case CCE_OP_ADD: row[j] += a; break;            /* *** scatter-ADD accumulate */
              case CCE_OP_MAX: row[j]  = max(row[j], a); break;
              case CCE_OP_MIN: row[j]  = min(row[j], a); break;
            }
        }
    }
}
```

The distinguishing feature versus the other three datapaths: embedding-update is a **scatter
with reduction** (`+=`), where multiple indices may target the same row and their contributions
**accumulate**. The `non_unique_dst_idx`/`CCE_OP_ADD` combination on the DMA side and the
`cce_op = ADD` here are the two expressions of the same scatter-add. [HIGH/OBSERVED]

---

## 8. Index dtype and bounds-checking — consolidated

**Index dtype.** [HIGH/OBSERVED]

- `GATHER 0x68`: `in_dtype ∈ {UINT8=0x3, UINT16=0x5, UINT32=0x9}` (`has_gather_index_dtype`);
  NKI `nc_n_gather` narrows to `uint32`.
- `INDIRECT_COPY 0xe7`: indices expected `UINT16` (header); NKI `local_gather` mandates `uint16`.
- DGE path / `DMA_INDIRECT`: indices `uint32` (`"replacing step with sizeof(uint32_t)=4"`; all
  address-gen IVP ops `*_2x32`). The address arithmetic is **64-bit** (`ADDR8` src/dst;
  DGE `"address=0x%x_%x"` high/low).

**Bounds-checking — four enforcement points.** [HIGH/OBSERVED]

1. **Instruction-level:** `GATHER.index_miss_behavior` (`IMMEDIATE_WRITE` writes the fill /
   `SKIP_WRITE` leaves the element); `DMA_INDIRECT.src/dst_idx_bound_reg` (register-held upper
   bound, `bc_disable_oob_error_notif`, `idx_bound_is_err`); `INDIRECT_COPY.src_buffer_size`;
   `EMBEDDING_UPDATE.embedding_table_num_entries`.
2. **DGE-level:** `srx_idx_max` / `dst_idx_max` + `our_indices_valid` / `other_indices_valid`
   masks.
3. **HW-level:** the predicate masks (`vb`) built from `ivp_ltun_2x32` / `ivp_leun_2x32`
   (unsigned `<` / `≤` bound) gate the predicated gather (`ivp_gatheran_2x32t`) and the
   predicated select (`ivp_mov2nx8t` / `ivp_dselnx16t`). OOB lanes are not gathered and get the
   miss value or are skipped.
4. **Placement constraint:** `DMA_INDIRECT` requires the index tensor in **SBUF partition 0**
   (`has_dma_indirect_idx_addr_in_sbuf_partition0`); the index count is `≤ 4096` everywhere.

> **GOTCHA — there is no single "OOB policy."** Each datapath answers OOB differently:
> `GATHER` fills-or-skips per `index_miss_behavior`; `DMA_INDIRECT` errors-or-soft-skips per
> `idx_bound_is_err`/`bc_disable_oob_error_notif`; the NKI `nc_n_gather` simulator **clip-and-zero**s
> while `local_gather` is documented OOB-**undefined**; `INDIRECT_COPY` bounds against
> `src_buffer_size`; `EMBEDDING_UPDATE` skips indices `≥ num_entries`. The HW predicate guarantees
> an OOB lane is *never loaded* — but what the destination lane then receives is the
> instruction-level policy's choice. A reimplementation must implement all four, not one.
> [HIGH/OBSERVED]

---

## 9. Cost model

From the `neuronx-cc` `.pyi` stubs (`nki/isa/__init__.pyi`). The GpSimd ops pay a **fixed
`GPSIMD_START ≈ 150` engine-cycle startup** (the kernel_info dispatch + trampoline + windowed-ABI
entry), versus the Vector/Scalar `MIN_II ≈ 64`. [HIGH/OBSERVED]

| NKI op | Cost (GpSimd Engine cycles) |
| ------ | --------------------------- |
| `local_gather` | `150 + (num_valid_indices · num_elem_per_idx) / C`, where `C = ((28 + t·num_elem_per_idx)/(t·num_elem_per_idx)) / min(4/dtype_size, num_elem_per_idx)`, `t = 4` |
| `nc_n_gather` | `150 + N` (within-partition flat gather) |

The `local_gather` divisor `C` encodes the per-core 16-partition parallelism — throughput rises
with `num_elem_per_idx` because contiguous runs amortize the gather posting. The 150-cycle floor
is why the compiler routes small tiles to Vector and reserves GpSimd for the ops Vector cannot do
(cross-partition reduce, gather, iota, affine_select). [HIGH/OBSERVED cost; architectural reading
INFERRED-HIGH]

---

## 10. Engine boundary (what this is *not*)

- **vs DVE (the Vector NX-core).** The DVE engine (NKI's numeric `vector=5`) carries
  `DveReadIndices` / `FindIndex8` / `MatchReplace8` / `RangeSelect` / `TensorScalarSelect` —
  **data-dependent value search/match**, a separate concern. The gather/indirect/embedding family
  here lives on **POOL (Q7)** (NKI's `gpsimd=3`), not DVE. `DveReadIndices` reads the DVE index
  register; it is not a gather kernel. [HIGH — handler-set diff]
- **vs the collective gather (CCL).** The DGE `GATHER TRANSPOSE` / `INDIRECT` kinds here are the
  **compute-side on-core** indirect-DMA descriptor generation. The cross-NeuronCore collective
  all-gather / reduce-scatter legs are a separate path; they share the SDMA descriptor format and
  the `rdma_desc_gen` loop but operate across cores over the collective ring. [HIGH/OBSERVED]

---

## See also

- [ISA Batch 19 — SuperGather Scatter/Gather](../../isa/ref/b19-scatter-gather.md) — the
  underlying `gathera*`/`gatherd*`/`scatter*`/`scatterinc*` vector ISA the kernels lower to; the
  `gvr` staging file, the per-lane `addr[k] = base + offset[k]·elem_sz`, the predicate AND.
- [Gather/Scatter + Gather-Transpose Descriptors](../../dma/gather-scatter-descriptors.md) — the
  DGE descriptor format and the `rdma_desc_gen` loop Mechanism C emits.
- [cas/fiss SuperGather Semantics](../../iss/cas-supergather.md) — the ISS value-oracle
  (`ivp_gatheranx8ut`, `writeback__ivp_gatheranx16`) for the same HW gather op.
- [VAL — Gather / Scatter (SuperGather) Family](../../validation/gather-scatter.md) — the
  validation sweep for the indirection family.
- [The Do-Not-Repeat / Correction Ledger](../../reference/correction-ledger.md) — the
  `local_gather → 0xe7` / `nc_n_gather → 0x68` crossed-split correction and the `0x79` vs `0xca`
  embedding-opcode distinction.
