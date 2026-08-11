# Gather/Scatter + Gather-Transpose Descriptors

The GPSIMD ISA exposes **four** index-driven 64-byte descriptors. Two are DGE/DMA "high-level"
words that the descriptor-generation engine expands into 16-byte SDMA buffer descriptors
([descriptor model](descriptor-model.md), [DGE emit](../firmware/dge/dge-emit.md)); two are
POOL/Q7 compute words that run a software gather kernel in the Q7's own dataram. They share a
single index→address relation but differ in index dtype, where the multiply happens, and how the
result reaches memory. This page decodes all four **byte-level**, reproduces the three
address-generation mechanisms as annotated C naming the recovered symbols, and works four
concrete examples.

Every struct offset, enum value, opcode byte, and validity predicate below is read byte-for-byte
from the shipped `aws-neuronx-gpsimd-customop-lib` `0.21.2.0` arch-isa headers
(`neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/*.h`, each `ISA_STATIC_ASSERT(...==64)`);
the `struct→opcode` binding from the shipped `instruction_mapping.json`; the firmware
address-gen strings byte-exact from `libnrtucode_internal.so`; the CSR transpose port from the
`tpb_sbuf_cluster.json`. Device-side disassembly (the `@0x180`/`@0x748`/`@0x788` SW loop, the IVP
SuperGather datapath) is `CARRIED` from the [indirection engine](../firmware/kernels/indirection-gather.md)
and [SuperGather ISA batch](../isa/ref/b19-scatter-gather.md) decodes. Confidence is per-claim:
`HIGH/OBSERVED` = direct byte/header/string read; `MED/INFERRED` = reasoned over OBSERVED bytes
across a FLIX/literal-pool desync; `CARRIED` = OBSERVED in a cited sibling page, reused here.

> **GOTCHA — four descriptors, do not conflate.** Three name-vs-opcode traps recur: (a)
> `nki.isa.local_gather` lowers to **`INDIRECT_COPY 0xe7`**, *not* `GATHER 0x68`; (b)
> `GATHER_XPOSE 0xf1` (index-driven xbar gather-transpose) is distinct from `DMA_TRANSPOSE 0xbd`
> (non-indexed xbar) and from the DVE `STREAM_TRANSPOSE 0x6b` (a 32×32 datapath lane-permute, not
> a DMA at all); (c) `INDIRECT1D`'s `+58`/`+59` bound regs gate the **index** arrays, whereas
> `DIRECT2D`'s `+38`/`+39` bound regs gate the **data** buffers.

---

## 0. The four forms and the unifying relation

| Form | Opcode | Engine | NISA struct | Index dtype | Mechanism |
| ---- | ------ | ------ | ----------- | ----------- | --------- |
| `INDIRECT1D` | `0xbb` | NX/DMA (DGE) | `DMA_INDIRECT1D_STRUCT` | `uint32` | C — `do_indirection` |
| `GATHER_XPOSE` | `0xf1` | Gpsimd (SW-DGE) | `DMA_GATHER_XPOSE_STRUCT` | `uint32` | C — `tensor_reshape_indirect_transpose` |
| `INDIRECT_COPY` | `0xe7` | POOL/Q7 | `S4D4_IC_STRUCT` | `uint16` | B — SW per-index loop |
| `GATHER` | `0x68` | POOL/Q7 | `S4D4_GT_STRUCT` | `uint8/16/32` | A — HW IVP SuperGather |

The opcode bytes are byte-identical across all four arch generations
(`NEURON_ISA_TPB_OPCODE` enum: `GATHER = 0x68`, `DMA_INDIRECT = 0xbb`, `DMA_TRANSPOSE = 0xbd`,
`INDIRECT_COPY = 0xe7`, `DMA_GATHER_TRANSPOSE = 0xf1`); the `struct2opcode` table binds
`DMA_INDIRECT1D_STRUCT → OPCODE_DMA_INDIRECT`, `DMA_GATHER_XPOSE_STRUCT → OPCODE_DMA_GATHER_TRANSPOSE`,
`S4D4_IC_STRUCT → OPCODE_INDIRECT_COPY`, `S4D4_GT_STRUCT → OPCODE_GATHER`. [HIGH/OBSERVED]

The single index→address relation underlying **all four**:

```
eff_addr[lane] = start_addr + ( index[lane] * step_elem ) * elem_size_bytes
```

Only the scaling sourcing and the multiply site differ by form (§3). The DMA forms (`0xbb`/`0xf1`)
are reached from NKI via `dma_copy_indirect` / `dma_gather_transpose`; the compute forms
(`0xe7`/`0x68`) from `nki.isa.local_gather` / `nki.isa.nc_n_gather` (§7).

---

## 1. `INDIRECT1D` — the gather/scatter DGE word (`0xbb`)

`NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT`, 64 B, `ISA_STATIC_ASSERT(...==64)`. The struct **body is
byte-identical** from sunda (NC-v2) through maverick (NC-v5) — a direct `diff` of the typedef
across the two extreme generations is empty. Header prose (verbatim): *"DmaIndirect
performs DMAs for a vector of dynamic indices (offsets) generated during execution. These dynamic
indices can be applied to the source to perform a gather, or applied to the destination to perform
a scatter, or two sets of independent indices for both gather and scatter in one instruction."*

| off | size | member | semantics |
| --- | ---- | ------ | --------- |
| 0 | 4 | `header` | `{opcode==0xbb, inst_word_len, debug_cmd, debug_hint}` |
| 4 | 8 | `events` | per-instr semaphore wait/update (`WAIT_MODE`/idx + `UPDATE_MODE`/idx) |
| 12 | 1 | `semaphore` | completion semaphore index |
| 13 | 1 | `sem_increment` | |
| 14 | 1 | `idx_num_active_channels` | **must == 128** (`check_dma_indirect_indices`, §1.5) |
| 15 | 1 | `flags` | `DMA_INDIRECT_FLAGS` (§1.1) |
| 16 | 8 | `src_start_addr` | `ADDR8` — gather TABLE base / source tensor |
| 24 | 4 | `src_step_elem` | `int32` **SIGNED** — source element stride |
| 28 | 2 | `src_num_elem` | `uint16` — # source elements (≤4096 unless shape-from-reg) |
| 30 | 2 | `src_elem_size` | `uint16` — source element byte size |
| 32 | 8 | `dst_start_addr` | `ADDR8` — scatter base / destination tensor |
| 40 | 4 | `dst_step_elem` | `int32` **SIGNED** |
| 44 | 2 | `dst_num_elem` | `uint16` (≤4096 unless shape-from-reg) |
| 46 | 2 | `dst_elem_size` | `uint16` |
| 48 | 4 | `src_idx_start_addr` | `ADDR4` — the GATHER index vector (SBUF partition 0) |
| 52 | 4 | `dst_idx_start_addr` | `ADDR4` — the SCATTER index vector (SBUF partition 0) |
| 56 | 1 | `in_dtype` | `DTYPE` (source) |
| 57 | 1 | `out_dtype` | `DTYPE` (destination) |
| 58 | 1 | `src_idx_bound_reg` | `BOUND_CHECK_REG` — gates the **source** index (§6) |
| 59 | 1 | `dst_idx_bound_reg` | `BOUND_CHECK_REG` — gates the **dest** index (§6) |
| 60 | 1 | `compute_op` | `DGE_COMPUTE_OP` — in-flight reduce mode (§1.4) |
| 61 | 1 | `dma_configs` | `DMA_CONFIGS` — `priority_class:3` (0..4) + `reserved:5` |
| 62 | 2 | `reserved[2]` | must be 0 |

> **CORRECTION (vs the field catalog).** The `+61 dma_configs` byte is **not** "must be zero
> reserved": `is_valid_dma_configs` accepts `priority_class <= 4` with only the upper 5
> `reserved_bitfield` bits forced to 0. The low 3 bits carry a real 0..4 QoS priority class
> (see [DGE builder QoS](dge-builder-qos.md)). [HIGH/OBSERVED — `is_valid_dma_configs`]

The instruction's top-level predicate `is_valid_dma_indirect` chains: `is_valid_addr8` on both
start addresses; `is_valid_dge_shape_reg` (start_addr ↔ step_elem); `zero_num_elem_shape_reg_mode`
(§1.2); `check_dma_indirect_indices` (§1.5); `has_dma_indirect_valid_idx_start_addr`;
`are_valid_memcpy_dtypes(compute_op, in, out)`; `is_valid_dma_indirect_operation` (the
memcopy-without-CCE vs CCE split, §1.4); `has_valid_idx_bound_check_reg` on **both** bound regs
(§6.2); `has_valid_indirect_dim_by_mode` (§1.1); `has_dma_indirect_valid_index_count` (the ≤4096
cap, §1.2); and `has_dma_indirect_idx_addr_in_sbuf_partition0` (§1.3). [HIGH/OBSERVED]

### 1.1 `DMA_INDIRECT_FLAGS` (1 byte, packed)

```c
struct NEURON_ISA_TPB_DMA_INDIRECT_FLAGS {            /* LSB → MSB */
    INDIRECT_DMA_ADDRESSING_MODE indirect_mode      : 2;
    uint8_t                      idx_bound_is_err    : 1;
    uint8_t                      non_unique_dst_idx  : 1;
    INDIRECT_DIM                 gather_dim          : 2;   /* axis the SRC index walks */
    INDIRECT_DIM                 scatter_dim         : 2;   /* axis the DST index walks */
};
```

* `indirect_mode` — `INDIRECT_DMA_ADDRESSING_MODE`: `SRC_INDIRECTION = 0` (gather: index applies
  to source), `DST_INDIRECTION = 1` (scatter: index applies to dest), `SRC_DST_INDIRECTION = 2`
  (gather **and** scatter, two independent index sets in one instruction). [byte-exact enum]
* `idx_bound_is_err` — if 1, an OOB index raises an error notification; if 0 it is handled per the
  bound-reg's `bc_disable_oob_error_notif` (§6).
* `non_unique_dst_idx` — permits duplicate scatter indices (the reduce / scatter-add case where
  two indices land in one dst slot, §1.4).
* `gather_dim` / `scatter_dim` — `INDIRECT_DIM` `X=0, Y=1, Z=2, W=3` (minor2major: X fastest);
  select which source/dest axis the dynamic index permutes.

**The indirect-dim rule** (`has_valid_indirect_dim_by_mode` → `indirect_dim_check`, verbatim
logic): when the relevant `start_addr` is *shape-from-register* (an `ADDR8` shape-reg marker,
§1.2), the indirect dim may be any `X/Y/Z/W`; when it is an immediate address, the indirect dim
**must be `X`** (the only dim available without a shape register). In `SRC_INDIRECTION`,
`scatter_dim` must be 0; in `DST_INDIRECTION`, `gather_dim` must be 0; `SRC_DST` checks both legs.
[HIGH/OBSERVED]

### 1.2 `ADDR8 src/dst_start_addr` — the shape-from-register mode

`ADDR8` is an 8-byte union discriminated by a marker byte (`& 0xFC`, `ADDR8_MARKER_MASK`). Two
independent bits drive it — bit 6 (`SHAPE_REG_BIT = 1<<6 = 0x40`) and bit 7
(`ADDR_REG_BIT = 1<<7 = 0x80`):

| marker | `(addr, shape)` source | union member |
| ------ | ---------------------- | ------------ |
| `0x00` `IMM` | (imm, imm) | `addr_immediate` |
| `0x40` `SHAPE_REG` | (imm, reg) | shape from register |
| `0x80` `ADDR_REG` | (reg, imm) | `addr_reg` |
| `0xC0` `ADDR_SHAPE_REG` | (reg, reg) | `addr_reg` + shape reg |
| `0x60` `ADDR_TBL_SHAPE_REG` | (tbl, reg) | `addr_tbl_offs` |

`addr8_shape_from_register(addr)` is TRUE iff the marker is `SHAPE_REG (0x40)`,
`ADDR_SHAPE_REG (0xC0)`, `ADDR_TBL_SHAPE_REG (0x60)`, or the `ADDR_TBL_SHAPE_REG | OFFSET_REG`
combo — i.e. **bit 6 governs shape-from-register**, not bit 7.

> **CORRECTION (vs the field catalog).** The shape-from-register predicate keys on the
> `SHAPE_REG_BIT (0x40)`, **not** on the address-from-register form `0x80/0xC0`. `0x80` (`ADDR_REG`)
> is *address*-from-register with an *immediate* shape — for it `addr8_shape_from_register` is
> FALSE. [HIGH/OBSERVED — `addr8_shape_from_register` + the marker constants]

When shape-from-register is TRUE, `zero_num_elem_shape_reg_mode` forces the corresponding
`src/dst_num_elem` to **0** (the count comes from the register pair, not the immediate field), and
`has_dma_indirect_valid_index_count` **waives** the `≤4096` cap (the register carries a dynamic
count). This is exactly how a runtime-sized gather — element count unknown at compile time — is
expressed: the base *and* the count are register-sourced. [HIGH/OBSERVED — the two predicates]

### 1.3 The index vectors — `src_idx_start_addr` / `dst_idx_start_addr`

Each is an `ADDR4` (4 B). `has_dma_indirect_idx_addr_in_sbuf_partition0` →
`has_addr4_in_sbuf_partition0`: the active index vector(s) **must be register-sourced
(`addr_from_register`) or live in SBUF partition 0** (`addr_in_sbuf_partition(0, …)`). The DGE
descriptor path treats the index tensor as `UINT32`: the firmware string
`"shared_indirection_dim=%d, replacing step with sizeof(uint32_t)=%d"` records that the
indirection-dim step is **replaced with `sizeof(uint32_t)=4`** at the index read (§3.3).
[HIGH/OBSERVED — header predicate + firmware string]

### 1.4 `compute_op` — the in-flight reduce

```c
enum NEURON_ISA_TPB_DGE_COMPUTE_OP {
    NONE = 0x00,   /* B = A     ; standard memcpy */
    ADD  = 0x01,   /* B += A    */
    MULTIPLY = 0x02, /* B *= A  */
    MAX  = 0x03,   /* B = MAX(A,B) */
    MIN  = 0x04,   /* B = MIN(A,B) */
};
```

`compute_op == NONE` is a pure gather/scatter — the `is_memcopy_without_cce` leg of
`is_valid_dma_indirect_operation`. A non-`NONE` op routes through the CDMA CCE compute engine
(`is_valid_dma_indirect_cce`), which adds start-address alignment (`is_aligned_start_addr8`) and
step-alignment (`is_aligned_step`) constraints. The canonical reduce case is
`DST_INDIRECTION + non_unique_dst_idx + compute_op == ADD` — a **scatter-add** (histogram /
embedding-gradient accumulation): the firmware drives the IVP `SCATTERINC` path (`operation_fld=6`,
the in-memory `mem[addr] += value` RMW; see [SuperGather §1.3](../isa/ref/b19-scatter-gather.md)).
[HIGH/OBSERVED — enum + predicates]

### 1.5 Active-channel constraint

`check_dma_indirect_indices(idx_num_active_channels)` requires both `check_active_channels(...)`
**and** `idx_num_active_channels == POOLING_NUM_CHANNELS`, where `POOLING_NUM_CHANNELS == 128`. So
the `+14` field is not free — for both `INDIRECT1D` and `GATHER_XPOSE` it must equal **128**
(the index drives all 128 partition channels). [HIGH/OBSERVED — `check_dma_indirect_indices`]

---

## 2. `GATHER_XPOSE` — the gather-transpose DGE word (`0xf1`)

`NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT`, 64 B, `ISA_STATIC_ASSERT(...==64)`. **NC-v3+ only** — the
header is **absent** from sunda (NC-v2: the file does not exist); mariana (NC-v4) and maverick
(NC-v5) bodies are byte-identical (only the `ISA header for NC-v4`→`NC-v5` comment differs).
Header prose (verbatim): *"DmaGatherTranspose performs a gather operation from HBM or SBUF using
dynamic indices, followed by xbar transpose, and writes the result to SBUF. This instruction uses
the SW-DGE backend with Q7 processors in the Gpsimd engine. Key use cases: MOE MLP and chunked
prefill attention."*

| off | size | member | semantics |
| --- | ---- | ------ | --------- |
| 0 | 4 | `header` | `{opcode==0xf1, …}` |
| 4 | 8 | `events` | semaphore wait/update |
| 12 | 1 | `semaphore` | completion semaphore |
| 13 | 1 | `dma_configs` | `DMA_CONFIGS` (priority class) |
| 14 | 1 | `idx_num_active_channels` | must == 128 (§1.5) |
| 15 | 1 | `src_idx_bound_reg` | `BOUND_CHECK_REG` — gates the index (no flags coupling, §6) |
| 16 | 8 | `src_start_addr` | `ADDR8` — HBM or SBUF gather source (2-B aligned) |
| 24 | 8 | `src_step_elem[2]` | `int32[2]` **SIGNED** — 2-D source stride pair |
| 32 | 4 | `src_num_elem[2]` | `uint16[2]` — 2-D source counts (X=dim0, Y=dim1) |
| 36 | 4 | `src_idx_start_addr` | `ADDR4` — the `UINT32` index vector (4-B aligned) |
| 40 | 8 | `dst_start_addr` | `ADDR8` — SBUF destination (**32-B aligned**) |
| 48 | 3 | `reserved[3]` | must be 0 |
| 51 | 1 | `dst_bound_reg` | `BOUND_CHECK_REG` — gates the dst (keyed on dst marker, §6) |
| 52 | 4 | `dst_step_elem_1` | `int32` **SIGNED** — dst dim-1 stride; **dim-0 step is implicitly `sizeof(dtype_hi)`** |
| 56 | 4 | `dst_num_elem[2]` | `uint16[2]` |
| 60 | 2 | `elem_size` | `uint16` — bytes per transpose row (`[2,256]`, even) |
| 62 | 1 | `dtype` | `DTYPE_PAIR {dtype_lo:4 = src, dtype_hi:4 = dst}` |
| 63 | 1 | `flags` | `DMA_GATHER_TRANSPOSE_FLAGS {gather_dim:2, reserved:6}` |

> **NOTE — implicit dst dim-0 step.** Only `dst_step_elem_1` is encoded; the dst dim-0 (fastest)
> step is *not* a field — the header comment fixes it to `sizeof(dtype_hi)`. A reimplementation
> must synthesize the dim-0 stride from the dst dtype width, not read it from the word. [HIGH/OBSERVED]

**Constraints** (header predicate comments, all HIGH/OBSERVED):

* `nc >= V3` (`has_valid_dma_gather_transpose_nc`).
* **2-B dtype only** (`has_valid_gather_transpose_dtype`): `type_size_check(dtype_lo,2)` and
  `type_size_check(dtype_hi,2)` and `dtype_lo == dtype_hi` — BF16/FP16 only, no cast in ucode yet.
* `elem_size ∈ [2,256]`, even (`has_valid_gather_transpose_elem_size`).
* `src_start` 2-B aligned; `dst_start` **32-B aligned**; `src_idx_start` 4-B aligned (UINT32)
  (`has_valid_gather_transpose_addresses`).
* `gather_dim == Y` (`INDIRECT_DIM::Y`, the slowest/dim[1] axis) "for now to simplify uCode"
  (`has_valid_gather_transpose_flags`).
* `src_num_elem[1] % 16 == 0` — the transpose-tile row alignment
  (`has_valid_gather_transpose_src_dimensions`).
* `dst_step_elem_1 % 32 == 0` **OR** `dst_num_elem[1] == 1` — the xbar 32-B-aligned dst row stride
  (`has_valid_gather_transpose_dst_steps`).
* **Cross-field:** `src_num_elem[1] == dst_num_elem[0]` (both equal the index count)
  (`has_valid_gather_transpose_cross_field_consistency`).

**Dimension ordering** (verbatim header): the ISA uses **minor2major** — `X=dim[0]` (fastest) → Y →
Z → `W=dim[3]` (slowest), the **reverse** of Neuron/numpy major2minor. So `gather_dim == Y` permutes
source array index `[1]` (the slow axis); the gathered rows land in dst `dim[0]` (the fast axis) —
the gather collapses the slow source axis into the fast dst axis **while** the xbar transposes the
16×128 tile (§4). [HIGH/OBSERVED]

---

## 3. The address-generation math — how an index vector drives the BD stream

Three coexisting mechanisms realize the index→address transform. All compute the same relation
(§0); they differ in **where** the per-lane multiply happens and **how** the result reaches memory.

### 3.1 Mechanism A — the HW IVP SuperGather (vector-gather datapath) [HIGH/CARRIED]

The fastest path: one vector instruction gathers 16 × 32-bit / 32 × 16-bit / 64 × 8-bit elements.
Two phases ([indirection engine §4](../firmware/kernels/indirection-gather.md), native `ncore2gp`
disasm):

```c
/* Mechanism A — IVP SuperGather (S4D4_GT 0x68 / the HW datapath).
 * PHASE 1 (post): { ivp_gatheran_2x32t  gr0, a8, v31, vb0 }   ; 32-bit elem
 *      a8  = VAddrBase  (table base)
 *      v31 = GSVAddrOffset[16] = index[lane]*step*elem_size  (PRECOMPUTED upstream)
 *      vb0 = GSEnable[lane]    = the in-bounds predicate mask
 *      gr0 = the gather FIFO the addresses post into
 * PHASE 2 (drain): { ivp_gatherdnx16 v3, gr0 }  ; drain gr0 into vector v3
 * The A-phase variant selects element width: _2x32t(32b) / nx16t(16b) / nx8ut(8b),
 * i.e. GSControl.elem_sz_fld {0,1,2} → byte scale {1,2,4} (matched at the host). */
addr[lane] = VAddrBase + GSVAddrOffset[lane];         /* base + per-lane offset    */
GSEnable[lane] = op_pred ? vbr_in[lane] : 1;          /* predicated forms gate; else all-on */
```

The 16-bit `GSControl` control word
`{8'd0, elem_sz_fld[1:0], offst_sz_fld[1:0], operation_fld[3:0]}` carries `operation_fld`:
`1=gathera(post)`, `2=gatherd(drain)`, `3=mgatherd`, `4=scatter`, `5=scatterw`,
`6=scatterinc` (the scatter-ADD / histogram RMW). The `index*step` product is computed **upstream**
into the offset vector `v31`; the HW does `base + offset` per lane gated by the bounds mask, so OOB
lanes (mask bit 0) are never fetched. [HIGH/CARRIED — [SuperGather §1.3](../isa/ref/b19-scatter-gather.md)]

### 3.2 Mechanism B — the software per-index loop (`pool_indirect_copy 0xe7`) [HIGH/CARRIED]

The `S4D4_IC (0xe7)` compute form. Dispatcher `pool_indirect_copy @0x01000748` reads the
core/channel count and computes this core's 16-partition slice (`slli a2,a2,4` = core·16, with
`rsr.prid` @0x781 giving the core id over the 8-core / 16-partition split), then runs the worker.

```c
/* Mechanism B — pool_indirect_copy SW per-index loop (INDIRECT_COPY 0xe7).
 * Dispatcher @0x748 partitions by core; worker @0x180 loops; @0x788 services each. */
unsigned core = rsr_prid() & CORE_MASK;               /* @0x781 */
unsigned width = (in_dtype==u8)?1:(in_dtype==u16)?2:4; /* const16 a2,{1|2|4} */
for (unsigned i = 0; i < index_count; ++i) {           /* loop over index count a11 */
    /* @0x271 mull a2,a2,a5 ; l32i a8,a1,24   -> a2 = stride * index(a5)            */
    /* @0x27d addx2 a2,a2,a8                  -> a2 = base(a8) + 2*(stride*idx)     */
    addr_t a = base + index[i] * stride;               /* == base + index*stride    */
    /* @0x295 const16 a8,0x788 ; callx8 a8  -> ic_util::send_gather_request(addr,..)*/
    send_gather_request(a, &idx_desc, src_num_elem_per_idx, in_dtype, ...);
    /* @0x29e addi.a a5,a5,1 -> index++ */
}
```

`ic_util::send_gather_request @0x01000788` is the per-request **vectorized bounds-check core**: for
a multi-dim index pattern it reads an `(lo,hi)` 16-bit pair per dim, multiplies the index component
into the running address (`mul16u`), advances the idx-descriptor by 4 B, and gates with
`ivp_ltun_2x32 vb0,v0,v1` (unsigned `index < bound`) feeding `ivp_mov2nx8t` / `ivp_dselnx16t`
predicated dual-lane-selects. One element copied per loop iteration; ≤4096 indices/core. [HIGH/CARRIED]

### 3.3 Mechanism C — the DGE descriptor-generation path (`do_indirection`) [HIGH str / MED body]

The `D2 INDIRECT1D (0xbb)` and `D3 GATHER_XPOSE (0xf1)` words run this. It does **not** gather
elements directly — it **builds indirect SDMA descriptors** whose `buf_ptr` is the index-computed
address; the SDMA engine then moves the data. The authoritative multiply is the firmware string
(byte-exact in `libnrtucode_internal.so`):

```
P%i: Before IVP_MULUSAN_2X32: address=0x%x_%x, last_indices=0x%x, indirection_step=0x%x
P%i: After  IVP_MULUSAN_2X32: address=0x%x_%x
```

i.e. `address (64-bit, hi_lo) += last_indices * indirection_step`, via the IVP
Multiply-Unsigned-Signed-Accumulate-N 2×32 op. The supporting pipeline (all byte-exact strings,
HIGH/OBSERVED):

* `do_indirection START: dim=%d, last_indices=0x%x, indirection_step=0x%x` — the per-dim entry.
* `indirection_steps created: indirection_dimension=%zu, num_dims=%zu` plus
  `increment_dimension indirection_steps: … our_forward_step=%d, other_forward_step=%d` and
  `rewind_dimension indirection_steps: … our_rewind_step=%d, other_rewind_step=%d` — the per-dim
  forward/rewind stride accounting that walks the N-D pattern (the "our"/"other" legs are the two
  index sets, i.e. the gather vs scatter tensors).
* `shared_indirection_dim=%d, replacing step with sizeof(uint32_t)=%d` — the index-as-UINT32
  substitution (§1.3).
* `gen_spray_info: reshape_kind=%d, do_indirection=%d, is_our_tensor=%d, hbm_to_hbm=%d, ndma=%zu` —
  spreads the transfer across `ndma` descriptors.
* `make_gather_pattern START: reshape_kind=%d, step_port=%d, step_half=%d, eng_mask=0x%x` — builds
  the gather access PATTERN + an in-bounds MASK; `gather_indices START: indices=%p, pattern=0x%x,
  mask=0x%x` reads the index vector with that pattern/mask.
* `Indirection masks: our_indices_valid = 0x%x, other_indices_valid = 0x%x, srx_idx_max = %u,
  dst_idx_max = %u` — each index bound = the table size (`srx_idx_max` for gather, `dst_idx_max` for
  scatter); the valid masks mark in-bounds lanes (built with `ivp_ltun/leun_2x32`).
* `Descriptor generation loop index: [%d, %d, %d, %d, %d]` — a **5-D** descriptor walk;
  `Tensor shape before Batman: num_elem=[%u,%u,%u,%u,%u] step_elem=[%d,%d,%d,%d,%d]` — the internal
  reshape transform codenamed **"Batman"** reshapes the 5-D pattern along the shared indirection
  dim before the loop emits descriptors.
* `tensor_reshape_indirect_transpose: tile_src_rows=%d tile_src_cols=%d dtype_size=%d` (with
  `BEFORE`/`AFTER: elem_size=%u num_elem=[…] step_elem=[…]` dumps) — the `0xf1` GATHER_TRANSPOSE
  descriptor builder (tiles src rows/cols and transposes during the indirect gather, §4).

So the DGE path: `address += index*indirection_step` (64-bit) per gathered row → reshape the access
pattern ("Batman") → emit one SDMA BD per descriptor (§5). [HIGH for the named transforms +
strings; MED for the exact `ncore2gp` FLIX-bundle encoding of `IVP_MULUSAN_2X32` — the string names
the op unambiguously; the precise mnemonic sits in a FLIX-desynced bundle.]

### 3.4 The unified stride/offset formula [HIGH — reconciled]

Across all three, per lane:

```
eff_addr[lane] = start_addr + ( index[lane] * step_elem ) * elem_size_bytes
```

| piece | source |
| ----- | ------ |
| `start_addr` | `src/dst_start_addr` `ADDR8` (D2/D3) or `ADDR4` mem-pattern start (`0xe7`/`0x68`) |
| `index[lane]` | `src/dst_idx_start_addr`; dtype `u8/16/32` (`0x68`), `u16` (`0xe7`), `u32` (DGE) |
| `step_elem` | `src/dst_step_elem` `int32` **SIGNED** — a **negative** step is a reverse-axis walk (the transpose primitive) |
| `elem_size` | `src/dst_elem_size` (D2) / `elem_size` (D3) / `GSControl.elem_sz_fld` byte-scale (A) |

In **A** the `index*step` product is precomputed into the offset vector; in **B** it is a scalar
`mull`+`addx2` per index; in **C** it is `IVP_MULUSAN_2X32` accumulated into a 64-bit address. The
SIGNED `step_elem` is exactly the field `DIMPUSH` pushes (§5) and the source of transpose
(negative/permuted strides). [HIGH — three legs reconciled to one formula]

---

## 4. The transpose tiling — the axi2sram xbar path

`GATHER_XPOSE (0xf1)` and its non-indexed sibling `DMA_TRANSPOSE (0xbd)` are realized by the SBUF
**axi2sram crossbar** transposing the tile **while** the DMA streams it — *not* a datapath
lane-permute (that is the orthogonal DVE `STREAM_TRANSPOSE 0x6b`, 32×32; see
[stream-transpose](../firmware/kernels/stream-transpose.md)).

**Tile geometry** (header predicate constants, HIGH/OBSERVED):

* `0xf1 GATHER_XPOSE` — xbar tile = **16 rows × 128 cols**, 2-B dtype only. `src_num_elem[Y]`
  (the gathered count) `% 16 == 0` is the row-tile alignment; dst 32-B aligned.
* `0xbd DMA_TRANSPOSE` (non-indexed) — tile rows == 16; cols `1..128` for 2-B out_dtype, `1..64`
  for 4-B out_dtype; dst start + step 32-B aligned. So 16 × (≤128 / ≤64) tiles.

**The HW port.** `tpb_sbuf_cluster.json` block `axi2sram_config` carries field
`axi2sram_transpose_en` / `transpose_en`, description *"Enables transpose mode in axi2sram block"*.
This is the SBUF systolic transpose port the DMA xbar drives. [HIGH/OBSERVED — the JSON field +
description]

**Transpose-by-stride mechanism** (reconciled). The DGE reshape engine ("Batman" for the indirect
case, `tensor_reshape_indirect_transpose` for `0xf1`) resolves the xbar tile permutation by
**swapping the src/dst `step_elem[]` axes**: for a transpose, the dst dim-0 step is the source's
slow-axis step and vice versa, and the SIGNED `step_elem` lets it walk an axis in reverse. The DGE
then emits `DIMPUSH` levels (§5) with the **transposed stride pattern**; with `transpose_en` set,
the axi2sram xbar lands the 16×N tile transposed in 32-B-aligned SBUF. [HIGH for the tile constants +
the axi2sram port; the stride-swap is CARRIED from the [DMA/transpose opcode cluster](../firmware/kernels/dma-transpose-opcode-cluster.md).]

---

## 5. The DGE emit micro-op sequence — GENERATE / DIMPUSH / REGWRITE

Once the reshape engine resolves a reshape-kind + #DMA + post-reshape num/step, and a backend is
selected, the DGE emits a descriptor **program** onto a target channel `DMA[d]` via three push
primitives. The byte-exact NX_POOL/SWDGE firmware format strings (`libnrtucode_internal.so`,
detail in [DGE emit §4](../firmware/dge/dge-emit.md)):

| micro-op | format string | emits |
| -------- | ------------- | ----- |
| `GENERATE` | `S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i` | ONE data-transfer BD — direction (`%s`=RD/WR), 64-bit SoC addr (→ the §3 index-computed `eff_addr` → `BD.buf_ptr`), per-element byte size, completion semaphore. For an indirect gather, ONE GENERATE per gathered row (the address is the `IVP_MULUSAN_2X32` result). |
| `DIMPUSH` | `S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]` | ONE loop/dimension level — a `[num,num]` (u16 counts) + `[step,step]` (i32 **SIGNED** strides) pair (src & dst legs of that dim). The SIGNED steps carry the transpose stride-permutation (§4). |
| `REGWRITE` | `S: push REGWRITE to DMA[%d]` | inline register-source field (e.g. a register-sourced base/count); **retired on NC-v4+** (folded into `dge_reshape_memcopy_transpose_fast`). |

**Per-backend dim count** (read directly from the backend dump lines): the **software** backend
dumps 4 dims (`src=…[0x%x,0x%x,0x%x,0x%x][%u,%u,%u,%u]`); the **RTL** backend dumps `5+2`
(`…[0x%x,0x%x,0x%x,0x%x,0x%x|0x%x,0x%x][…]` — a 5-D outer walk + a 2-D inner); the **Pool** backend
uses 2. Selection is logged as `S: DGE: Select backend Pool` / `Select backend RTL` (and
`S: DGE: NO BACKEND FOUND, doing nothing` on miss).

**Shape state.** `dge_shape = { num[4], step[4] }` held in `$S[0..3]` (`NUM_DGE_SHAPE_REGISTERS=4`;
dumped via `S: dge_shape[%i].num = [ 0x%04x, … ]` / `.step = [ … ]`). The software backend line
`S: DGE software backend: $S[%i]+=%i@dmacomplete src=[…] dst=[…] cast:0x%x->0x%x, indirection_dim=0x%x,
reshape_kind=0x%x` names the gather/scatter axis (`indirection_dim`) and the reshape transform
(`reshape_kind`, including the indirect-transpose kind for `0xf1`).

**TPB opcode → DGE kind** (`NEURON_ISA_TPB_DGE_OPCODE` enum, byte-exact):

```c
DGE_OPCODE { DMA_DIRECT2D = 0x0, DMA_INDIRECT1D = 0x1,
             DMA_TRANSPOSE = 0x2, DMA_GATHER_TRANSPOSE = 0x3 };
```

So an `INDIRECT1D` word lowers to DGE **kind 1**; a `GATHER_XPOSE` word to **kind 3**. The backend
switches on this 2-bit kind, runs `do_indirection` (kind 1/3) or the plain reshape (kind 0/2), then
emits the GENERATE+DIMPUSH stream.

**Stream assembly** (per channel `DMA[d]`): `[REGWRITE]* + GENERATE + DIMPUSH×#dims`, landing in the
DGE_MEMORY carveout, then the `TDRTP_inc` (read/M2S) or `RDRTP_inc` (write/S2M) tail-pointer
**doorbell** — confirmed at the firmware level by `Q7: rdma_desc_start [TX]/[RX] … tail pointer
increment`. For an indirect gather the loop emits one GENERATE per (in-bounds) index, so the BD
count = the number of in-bounds indices. [HIGH — emit model + dim counts OBSERVED; the per-index
GENERATE count INFERRED-HIGH from the `Descriptor generation loop` string + gather semantics.]

---

## 6. `BOUND_CHECK_REG` — the per-descriptor / per-index validity gate

```c
struct NEURON_ISA_TPB_BOUND_CHECK_REG {        /* 1 B, ISA_STATIC_ASSERT(...==1) */
    REG_NUM bc_reg                     : 6;   /* HW reg holding the buffer/table UPPER LIMIT;
                                                 in wide-offset mode bc_reg+1 holds the high 32 b */
    uint8_t bc_disable_oob_error_notif : 1;   /* 1 = OOB does NOT raise an error notif (silent) */
    uint8_t bc_enabled                 : 1;   /* 1 = bound check active */
};
```

**The validity predicate** `has_valid_bound_check_reg(bc, marker)` (verbatim):

```c
   ( bc.bc_enabled == 0 && bc.bc_reg == 0 && bc.bc_disable_oob_error_notif == 0 )   /* fully inert */
|| ( bc.bc_enabled == 1 && is_valid_register_read_with_marker(bc.bc_reg, marker) ) /* fully armed */
```

The bound reg is either fully inert (all three fields zero) or fully armed (enabled + a readable
register). A half-set bound reg is **invalid**. [HIGH/OBSERVED]

**The index-bound variant** (`INDIRECT1D`-specific) couples the reg to the flags:

```c
fn has_valid_idx_bound_check_reg(bc, flags) =
       has_valid_idx_bound_check_reg_no_flags(bc)        /* §6.1 with ADDR8_MARKER_ADDR_TBL_OFFSET_REG */
    && ( flags.idx_bound_is_err == 1 || bc.bc_disable_oob_error_notif == 0 );
```

i.e. if the descriptor declares OOB-is-an-error (`idx_bound_is_err == 1`) **or** the bound reg does
not disable the OOB notification, the index bound is honored. `INDIRECT1D` checks this for **both**
`src_idx_bound_reg (+58)` and `dst_idx_bound_reg (+59)`. `GATHER_XPOSE` checks `src_idx_bound_reg`
with `has_valid_idx_bound_check_reg_no_flags` (no flags coupling) and `dst_bound_reg` with
`has_valid_bound_check_reg` keyed on the **dst `ADDR8` marker** (`dst_start_addr.addr_tbl_offs.marker`).
[HIGH/OBSERVED — both predicates]

**Three enforcement levels** (reconciled):

1. **Descriptor** — the `bc_reg` / `bc_enabled` / `idx_bound_is_err` fields: the compile-time
   declaration of which register holds the limit and whether OOB is fatal.
2. **Index-compare** — at run, the firmware reads `srx_idx_max` / `dst_idx_max` (the table sizes,
   §3.3) and builds the per-lane in-bounds mask with `ivp_ltun_2x32` / `ivp_leun_2x32` (unsigned
   `index < / ≤ bound`) — the `our_indices_valid` / `other_indices_valid` masks.
3. **Gather/miss** — the mask feeds `GSEnable` (mechanism A) or the `ivp_mov2nx8t`/`dselnx16t`
   predicated select (mechanism B); OOB lanes are not fetched. The **miss-fill policy** (what lands
   in the dst for an OOB lane) is a **compute-form** field, not the DGE word: `S4D4_GT`'s
   `index_miss_behavior {ImmediateWrite=0 (write `immediate`), SkipWrite=1 (leave dst unmodified)}`
   (§7.2).

[HIGH for (1) header + (2) firmware compares + (3) the miss-behavior enum; the host miss-fill is
CARRIED from [SuperGather](../isa/ref/b19-scatter-gather.md).]

---

## 7. The two NKI gather lowerings tied to their descriptor forms

The 2:1 NKI split (`local_gather` vs `nc_n_gather`, [indirection engine §2](../firmware/kernels/indirection-gather.md))
maps to two distinct POOL compute structs.

### 7.1 `S4D4_IC` (`INDIRECT_COPY 0xe7`) — `nki.isa.local_gather`

POOL engine (struct `s4d4_ic.h`); the 8-core / 16-partition **software per-index loop**
(mechanism B, §3.2). The gather case (the only one implemented; scatter and gather-scatter are
documented-but-unsupported):

| off | size | member | detail |
| --- | ---- | ------ | ------ |
| 12 | 20 | `src_mem_pattern` | `MEM_PATTERN4D`; **must be `indirect_pattern`** (`INDIRECT20B` `.i` variant — `index_addr` + `data_addr` + `num_elem`; ADDR4 marker `& 0xe0 ∈ {0x20 INDIRECT_IMM, 0xA0 INDIRECT_REG}`) |
| 32 | 1 | `in_dtype` | must == `out_dtype` (`has_same_in_out_dtype_indirect_copy`; no cast) |
| 33 | 1 | `out_dtype` | |
| 34 | 1 | `num_active_channels` | `% 16 == 0`, `1..128`; starting partition (in the mem-pattern addr) ∈ `{0,32,64,96}` |
| 35 | 1 | `src_num_elem_per_idx` | `∈ {1,2,4,8,16,32}` — contiguous elements copied per index |
| 36 | 1 | `dst_num_elem_per_idx` | **must == 0** |
| 37 | 1 | `reserved0` | |
| 38 | 2 | `src_buffer_size` | `uint16`, `> 1` |
| 40 | 2 | `dst_buffer_size` | `uint16`, **must == 0** |
| 42 | 2 | `reserved1[2]` | |
| 44 | 20 | `dst_mem_pattern` | `MEM_PATTERN4D`; **must be `tensor_pattern`** (`.t`, a plain strided dst) |

**Gather math** (`has_valid_s4d4_ic_gather_*`): src is the indirect pattern (`index_addr → data_addr`),
dst a flat tensor. The gathered element count = `src.i.p.num_elem` (which must `==`
`t4d_element_count(dst)`); the **index count = `num_elem / src_num_elem_per_idx`** (must divide
evenly and be **≤ 4096**). SBUF only (`AllowedInPSUM::False` both sides). Additional limit
`has_valid_s4d4_ic_dst_elem_count`: `t4d_element_count(dst) <= 1024` — the Q7 stages gathered
elements in **local RAM** (1024 = the current data-scratch max, assuming 4-B elements) before
pushing to SBUF, to avoid a DVE-shared-port deadlock. So this form copies `src_num_elem_per_idx`-
element blocks from `data_addr[index[i]]` to the dst tensor, 16-partition tiled, ≤4096 indices.

> **GOTCHA — the name trap.** "local" in `local_gather` is the SBUF-local 8-core/16-partition
> loop — it does **not** route to `GATHER 0x68`. [HIGH/OBSERVED — struct + predicates]

### 7.2 `S4D4_GT` (`GATHER 0x68`) — `nki.isa.nc_n_gather`

POOL engine (struct `s4d4_gt.h`); the within-partition **PoolBuffer subset gather** (the HW
vector-gather, mechanism A).
Before this instruction runs, a `PoolBufferLoad` must fill up to 512 elements/channel into the
Pool_Buffer; the gather then indexes a **window** of that buffer:

```c
/* Per index (header semantics, verbatim): */
subset_hit    = ((index & ~pool_buffer_mask) == pool_buffer_start_index);
subset_offset =   index &  pool_buffer_mask;
if (subset_hit)            dst = Pool_Buffer[subset_offset];
else if (index_miss_behavior == ImmediateWrite) dst = immediate;  /* typ. first-of-group */
else /* SkipWrite */       /* leave dst unmodified */;            /* typ. mid/last-of-group */
```

| off | size | member | detail |
| --- | ---- | ------ | ------ |
| 12 | 20 | `src_mem_pattern` | `TENSOR4D` — the index stream (plain strided) |
| 32 | 1 | `in_dtype` | **must be `UINT8` / `UINT16` / `UINT32`** — this field IS the index width (`has_gather_index_dtype`) |
| 33 | 1 | `out_dtype` | the gathered-data dtype (`FP32R` allowed) |
| 34 | 1 | `num_active_channels` | `≤ POOLING_NUM_CHANNELS (128)` |
| 35 | 1 | `reserved0[1]` | == 0 |
| 36 | 1 | `index_miss_behavior` | `INDEX_MISS_BEHAVIOR {IMMEDIATE_WRITE=0, SKIP_WRITE=1}` |
| 37 | 1 | `free_pool_buffer` | 0 or 1 (free the Pool_Buffer after this gather) |
| 38 | 2 | `reserved1[2]` | == 0 |
| 40 | 4 | `immediate` | `IMM_VAL_INST_FIELD` (4 B) — the OOB fill; **must == 0 unless `index_miss_behavior == ImmediateWrite`** (`gather_valid_options`) |
| 44 | 20 | `dst_mem_pattern` | `TENSOR4D` — the gathered output |

**Gather math** (`has_gather_index_dtype` / `gather_valid_options` / `s4d4_gt_same_src_dst_count`):
`in_dtype` is the index width; src and dst have the same element count
(`same_element_count_t4d`); PSUM is allowed both sides (`tensor4d_valid(... AllowedInPSUM …)`); on an
index miss the policy writes `immediate` or skips. [HIGH/OBSERVED — struct + predicates]

> **NOTE — `0x68` is a Pool_Buffer *subset* gather, not an arbitrary table gather.** The index is
> resolved against a single loaded window (`pool_buffer_start_index` + `pool_buffer_mask`), so a
> "miss" means the index falls outside the currently-loaded buffer, *not* simply out of table
> bounds. Grouped gathers stream a buffer once and re-gather: first-of-group `ImmediateWrite`
> (init dst), middle/last `SkipWrite` (accumulate). [HIGH/OBSERVED — header prose + enum]

### 7.3 The distinction in one line

* **`0xe7 INDIRECT_COPY`** = INDIRECT mem-pattern (`index_addr`+`data_addr`) + blocks of
  `src_num_elem_per_idx` + ≤4096 indices + 16-partition SW loop + same-dtype copy + SBUF-only +
  ≤1024 dst elements (local-RAM staging).
* **`0x68 GATHER`** = TENSOR mem-pattern + `in_dtype` IS the `u8/16/32` index width +
  Pool_Buffer subset hit + `index_miss_behavior` + `immediate` fill + ≤128 channels + PSUM-OK.

The DGE words (`0xbb`/`0xf1`) are the **DMA-side** analogues (UINT32 indices, the
`IVP_MULUSAN_2X32` / `do_indirection` descriptor-gen path, mechanism C) — used when the gather is a
DMA from HBM/SBUF rather than an in-SBUF POOL-kernel gather. [HIGH/OBSERVED — the four structs +
`struct2opcode`]

---

## 8. Worked examples

Each worked from the §3.4 formula + the OBSERVED field semantics. The numeric values are
illustrative; the **formula and field placements are HIGH/OBSERVED**. DTYPE codes used:
`BFLOAT16=0x6`, `UINT8=0x3`, `UINT16=0x5`, `UINT32=0x9`, `FP32R=0xB` (byte-exact `DTYPE` enum).

### 8.1 EX-A — within-partition GATHER (`0x68 S4D4_GT`), HW vector-gather

Pool_Buffer loaded with an FP16 embedding window, 4096 valid rows × 64 cols, row stride 64 elem,
`elem_size = 2 B`. Index tensor `UINT32` `= [7, 0, 4095, 9001]`, `num_active_channels = 4`,
`index_miss_behavior = ImmediateWrite`, `immediate = 0x0000`.

```
lane bound check (subset_hit / idx < 4096):  7 OK | 0 OK | 4095 OK | 9001 MISS
offset vector  v_off = index*row_stride*elem_size = index*64*2 = index*128:
               = [ 0x380, 0x0, 0x7FF80, x ]
GSControl = {8'd0, elem_sz=1(16b), offst=0, operation=1(gathera)} = 0x0011 ; then drain op=2
GSEnable  = [1,1,1,0]   (lane3 masked)
ivp_gatheran_2x32t gr0, base, v_off, vb[1,1,1,0] ; ivp_gatherdnx16 v, gr0
result v = [ Pool_Buffer[7], Pool_Buffer[0], Pool_Buffer[4095], 0x0000 ]
```

Lane 3 gets the `immediate` fill `0x0` (miss = `ImmediateWrite`). [INFERRED-HIGH from §3.1/§7.2]

### 8.2 EX-B — local INDIRECT_COPY (`0xe7 S4D4_IC`), SW per-index loop

`data_addr = 0x2000_0008_0000` (SBUF), `src_num_elem_per_idx = 8`, in==out dtype `FP32` (4 B),
`src_buffer_size = 4096`, index dtype `u16`, indices `= [2, 0, 17]`, `num_active_channels = 16`.
With `src.i.p.num_elem = 24`, index count `= 24 / 8 = 3` (≤4096 OK); `t4d_element_count(dst) = 24 ≤
1024` OK.

```
block byte stride = src_num_elem_per_idx * elem_size = 8 * 4 = 32 B
worker @0x180 per index a5:
  idx=2  -> addr = base + 2*32 = base+0x40
  idx=0  -> addr = base + 0
  idx=17 -> addr = base + 17*32 = base+0x220
  send_gather_request copies the 8-elem (32-B) block at each addr to the dst.
this core (rsr.prid) handles its 16-partition slice (slli a2,a2,4).
dst = [ block@base+0x40, block@base+0x0, block@base+0x220 ]  concatenated.
```

[INFERRED-HIGH from §3.2/§7.1]

### 8.3 EX-C — scatter-add histogram (`0xbb INDIRECT1D`) via the DGE path

`indirect_mode = DST_INDIRECTION`, `flags.non_unique_dst_idx = 1`, `compute_op = ADD`.
`dst_start` = a 256-bin int32 histogram, `dst_step_elem = 1`, `dst_elem_size = 4`. dst index tensor
`UINT32 = [5, 5, 200, 5]`, data `= [1,1,1,1]`. `dst_idx_bound_reg`: `bc_enabled=1`, `bc_reg` holds
limit 256, `flags.idx_bound_is_err = 0`, `bc_disable_oob_error_notif = 1` (silent clamp). `bc` is
valid: enabled + readable reg, and `idx_bound_is_err==0 || bc_disable_oob_error_notif==... ` — wait,
the coupling requires `idx_bound_is_err==1 OR bc_disable_oob_error_notif==0`; with
`bc_disable_oob_error_notif==1` and `idx_bound_is_err==0` the predicate is FALSE, so this exact
combination is **invalid** — a real reimplementation must set `idx_bound_is_err=1` *or*
`bc_disable_oob_error_notif=0`. Choosing `idx_bound_is_err=1`:

```
per element: addr = dst_start + index*step_elem*elem_size = dst_start + index*1*4
  -> bins 5, 5, 200, 5    (all < 256, no OOB)
with non_unique_dst_idx + compute_op=ADD, the three writes to bin 5 ACCUMULATE:
  bin[5] += 1 (x3) -> bin[5]=3 ; bin[200]=1
firmware drives IVP SCATTERINC (operation_fld=6, the in-memory +value RMW).
```

[INFERRED-HIGH from §1.4/§3.1/§6 — and a CORRECTION on the prior catalog's bound-reg combo, which
violated `has_valid_idx_bound_check_reg`.]

### 8.4 EX-D — gather-transpose (`0xf1 GATHER_XPOSE`), MOE-MLP

`src` = BF16 (`0x6`) expert-weight table in HBM, `src_num_elem = [128(X), 64(Y)]` (Y = the
gather/slow dim), `src_step_elem = [1, 128]` (row stride 128 elem). `flags.gather_dim = Y`. Index
tensor `UINT32`, count `= src_num_elem[Y] = 64` (must be `%16==0` → 64 OK). Cross-field:
`dst_num_elem[0]` must `== src_num_elem[Y] = 64`. dst in SBUF, 32-B aligned, `dst_step_elem_1 %32==0`,
`elem_size = 2*128 = 256 B/row` (≤256, even OK), dst dim-0 step implicitly `sizeof(dtype_hi)=2`.

```
per index i in [0,64): row r = index[i].
DGE: address += index[i] * indirection_step  (IVP_MULUSAN_2X32, indirection_step
     replaced by sizeof(u32)=4 at the index read; the row addr via the gather pattern),
     gather the 128-element BF16 row, and the axi2sram xbar (transpose_en set) lands
     it transposed into a 16x128 dst tile.
64 indices -> 4 tiles of 16 rows.  tensor_reshape_indirect_transpose tiles tile_src_rows=16.
```

[INFERRED-HIGH from §2/§3.3/§4]

---

## 9. Confidence and corrections

**HIGH/OBSERVED** (read byte-exact): every field offset of `INDIRECT1D` (sunda≡maverick,
identical body), `GATHER_XPOSE` (mariana≡maverick, identical body; absent from sunda), `S4D4_IC`,
`S4D4_GT` — all four `ISA_STATIC_ASSERT(...==64)`; the five opcode bytes
(`0x68/0xbb/0xbd/0xe7/0xf1`) + `struct2opcode`; the `DMA_INDIRECT_FLAGS` bit layout +
`INDIRECT_DMA_ADDRESSING_MODE` `{0,1,2}` + `INDIRECT_DIM` `{X,Y,Z,W}={0,1,2,3}`; the `DGE_COMPUTE_OP`
enum `{NONE,ADD,MULTIPLY,MAX,MIN}={0..4}`; the `ADDR8` marker constants + `addr8_shape_from_register`
(bit 6); the `zero_num_elem_shape_reg_mode` / ≤4096 / SBUF-partition0 predicates;
`check_dma_indirect_indices == 128`; the `BOUND_CHECK_REG` layout + both validity predicates + the
`idx_bound_is_err` coupling; the `GATHER_XPOSE` transpose constraints; the `axi2sram transpose_en`
CSR; the `DGE_OPCODE` kind enum `{0,1,2,3}`; the `index_miss_behavior {IMMEDIATE_WRITE,SKIP_WRITE}`
+ `immediate`; the firmware emit strings (`GENERATE`/`DIMPUSH`/`REGWRITE`) + per-backend dim count
(sw=4, RTL=5+2) + `dge_shape[4]`; the `IVP_MULUSAN_2X32` / `do_indirection` / `make_gather_pattern` /
`gather_indices` / `gen_spray_info` / `tensor_reshape_indirect_transpose` / `srx_idx_max`/`dst_idx_max`
strings (all in `libnrtucode_internal.so`).

**MED**: the exact `ncore2gp` FLIX-bundle encoding of `IVP_MULUSAN_2X32` (string names the op
unambiguously; the precise mnemonic sits in a FLIX-desynced bundle); the per-index GENERATE count
for an indirect gather (INFERRED-HIGH from the descriptor-loop string + gather semantics); the
exact transpose stride-swap the reshape engine writes (CARRIED from the DMA/transpose cluster).

**LOW**: the absolute register values bound to `bc_reg` at run (compiler/runtime policy); the §8
numeric values (illustrative).

**Corrections vs the field catalog** (HIGH/OBSERVED): (a) `INDIRECT1D +61 dma_configs`
is `priority_class:3` (0..4) + `reserved:5`, **not** "must be zero" — only the upper 5 bits are
reserved (`is_valid_dma_configs`). (b) `addr8_shape_from_register` keys on the `SHAPE_REG_BIT (0x40)`,
**not** `0x80/0xC0`; the `0x80` `ADDR_REG` marker is address-from-register with immediate shape and
is FALSE for the predicate. (c) `idx_num_active_channels` must equal **128** for both DMA forms
(`check_dma_indirect_indices`), not an arbitrary channel count. (d) `S4D4_IC` carries the extra
`t4d_element_count(dst) <= 1024` local-RAM staging cap. (e) `GATHER 0x68` is a **Pool_Buffer subset**
gather (`subset_hit` against a loaded window), not an arbitrary table gather.

See also: the [descriptor model](descriptor-model.md) (the 4-layer pipeline + SDMA ring/doorbell),
the [firmware indirection engine](../firmware/kernels/indirection-gather.md) (the three mechanisms
device-side, with funcVAs), the [DGE emit path](../firmware/dge/dge-emit.md) and
[DGE micro-op encoding](dge-microop-encoding.md), and the
[SuperGather ISA batch](../isa/ref/b19-scatter-gather.md) (the bit-precise `GSControl` /
`GSVAddrOffset` / `GSEnable` engine ports).
