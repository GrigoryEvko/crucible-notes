# DGE Reshape Engine

The DGE **reshape engine** (`dge_reshape.cpp` + `analyze_tensor_reshape` +
`tensor_reshape_transpose`) is the descriptor-generation machinery that turns a
logical *reshape* or *transpose* of an N-D tensor into a sequence of SDMA
descriptors. It is the analysis stage that runs *before* the
[3-backend selector](./dge-backend-selector.md): it fills the DGE-internal
4-dim shape (`num_elem[4]`/`step_elem[4]`), classifies the request into a
*Reshape Kind*, decides a partition-split and a DMA fan-out, and — for transpose
requests — builds a `DmaGatherTranspose` descriptor whose hardware crossbar does
the actual transpose in 16×128 tiles.

This page also documents **the one functional addition in MARIANA_PLUS (v4+)**:
the reshape fast-path (`dge_reshape_memcopy_transpose_fast` /
`tensor_reshape_transpose_sb2sb` / `wait_for_credit`), which is the byte-grounded
delta between MARIANA (v4) and MARIANA_PLUS (v4+).

> **Audience.** Senior C++/LLVM engineer reconstructing a Vision-Q7 GPSIMD DGE.
> Every address, symbol, opcode and string below is tagged with a confidence
> level (HIGH / MED / LOW) and an evidence kind (OBSERVED — read directly from
> bytes/disasm/strings/header; INFERRED — derived; CARRIED — relied on from a
> sibling page). See the [confidence model](../../reference/confidence-model.md).

---

## 1. Provenance — carved objects and hashes

All reshape code lives in the **POOL firmware images** (the `NX_POOL` and
`Q7_POOL` members of `libnrtucode.a`), not in the host x86
`libnrtucode_internal.so` (the host only exposes
`nrtucode_core_dge_{set,get}_priority_class_map` and the DGE-mailbox config — see
[DGE setup](./dge-setup.md)). Each `img_<GEN>_<CLS>_POOL_DEBUG_<SEG>_contents.c.o`
member is an x86 wrapper whose `.rodata` section *is* the raw device IRAM/DRAM
image. Carve and confirm:

```text
AR = .../custom_op/c10/lib/libnrtucode.a            (10 235 636 B)
ar p $AR <member> > t.o
objcopy -O binary --only-section=.rodata t.o out.bin
```

| Carved object (NX/Q7 POOL DEBUG) | Segment | Size (B) | sha256[:12] | Confidence |
|---|---|---:|---|---|
| `img_CAYMAN_NX_POOL_DEBUG` | IRAM | 116 768 | `8e4412b99201` | HIGH/OBSERVED |
| `img_CAYMAN_NX_POOL_DEBUG` | DRAM | 28 448 | `7bdf6ed7ccd2` | HIGH/OBSERVED |
| `img_MARIANA_NX_POOL_DEBUG` | IRAM | 114 816 | `41b6c798bff3` | HIGH/OBSERVED |
| `img_MARIANA_NX_POOL_DEBUG` | DRAM | 28 672 | `ec067304e6cf` | HIGH/OBSERVED |
| `img_MARIANA_PLUS_NX_POOL_DEBUG` | IRAM | **119 616** | `9b514bb6d45a` | HIGH/OBSERVED |
| `img_MARIANA_PLUS_NX_POOL_DEBUG` | DRAM | 29 024 | `d2e1552a13f1` | HIGH/OBSERVED |
| `img_CAYMAN_Q7_POOL_DEBUG` | IRAM | 125 504 | `513a8a22d94b` | HIGH/OBSERVED |
| `img_CAYMAN_Q7_POOL_DEBUG` | DRAM | 89 344 | `226f4254d475` | HIGH/OBSERVED |
| `img_MARIANA_PLUS_Q7_POOL_DEBUG` | IRAM | 126 720 | `1c9c15bcc91b` | HIGH/OBSERVED |
| `img_MARIANA_PLUS_Q7_POOL_DEBUG` | DRAM | 89 472 | `295fae9c1cdb` | HIGH/OBSERVED |

The IRAM sizes track the per-gen totals carried from prior pages (SUNDA 59 600 /
CAYMAN 116 768 / MARIANA 114 816 / MARIANA_PLUS 119 616). The MARIANA_PLUS NX
IRAM is **`119616 − 114816 = 4800 = 0x12C0` bytes larger** than MARIANA — that
growth is the headline gen-delta dissected in §6.

> **DRAM addressing rule.** The DRAM image loads at device VA `0x80000`, so a
> string at file-offset `f` has device VA `f + 0x80000`. IRAM file-offset ==
> device IRAM VA. (HIGH/OBSERVED, CARRIED from the DGE-cluster setup page.)

> **GOTCHA — FLIX desync.** These are linked, densely-scheduled FLIX/VLIW Vision-Q7
> IRAM images (bundles up to 32 B) with literal pools interleaved into `.text`
> and **no `.symtab`**. A linear `objdump` sweep loses bundle sync across literal
> boundaries and prints bogus `.byte`/`l32r` spans. The decisive consequence for
> this page: the reshape *format-string corpus* (§4) and the *descriptor structs*
> (§3) are ground truth (bytes / compiled headers, not desync-affected), but the
> reshape *function bodies* sit in FLIX-desynced spans and are **not**
> byte-recoverable per-instruction. Body-level algorithm claims are therefore
> tagged MED/INFERRED and grounded on strings + structs, never on a desynced
> opcode read. Two clean anchors that *do* decode (the backend-select branch and
> the Q7 transpose assert site) are quoted in §7 and §3.4.

Disassembly throughout uses the native Vision-Q7 toolchain:

```text
XTENSA_SYSTEM=.../gpsimd_tools_tgz/tools/XtensaTools/config  XTENSA_CORE=ncore2gp
xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0 \
    --start-address=0x... --stop-address=0x... <image>.bin
# GNU objdump 2.34.20200201, Xtensa Tools 14.09 (HAVE_VISION=1 / VISION_TYPE=7)
```

---

## 2. Where the reshape runs — device-side, on the Q7/POOL cores

The shipped ISA header `aws_neuron_isa_tpb_dma_gather_xpose.h` states it verbatim
(lines 16–17, HIGH/OBSERVED):

> *"DmaGatherTranspose performs a gather operation from HBM or SBUF using dynamic
> indices, followed by xbar transpose, and writes the result to SBUF. This
> instruction uses the **SW-DGE backend with Q7 processors in the Gpsimd
> engine**."*

So the DGE that lowers and executes the reshape/transpose runs on the **Q7 cores
of the GPSIMD POOL engine**. The reshape symbols (`dge_reshape.cpp`,
`analyze_tensor_reshape`, `tensor_reshape_transpose`) are present *only* in the
`*_POOL_*` members and absent from the per-opcode EXTISA kernels and the non-POOL
sequencers (HIGH/OBSERVED grep). The host configures (priority / mailbox); the
device generates descriptors.

---

## 3. The reshape output — three 64-byte ISA descriptor structs

The reshape engine's output is one of three ISA descriptor structs, each
`ISA_STATIC_ASSERT`ed to 64 bytes in the shipped clean headers and
**compile-verified** with `gcc -I .../neuron_mariana_arch_isa/tpb`
(`sizeof()` returned `64`, `64`, `64`):

```text
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT)     == 64, ...);  // line 45
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT)   == 64, ...);  // line 49
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT) == 64, ...);  // line 61
```

### 3.1 `NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT` — DIRECT2D kind (2-D strided memcopy)

The plain reshape/copy descriptor: a 2-D `(x,y)` strided memcopy. (HIGH/OBSERVED,
compile-verified.)

| Off | Field | Notes |
|---:|---|---|
| +0 | `header` (4) | |
| +4 | `events` (8) | |
| +12 | `dma_configs` (1) | |
| +13 | `semaphore` (1) | |
| +14 | `sem_increment` (1) | |
| +15 | `compute_op` (1) | `DGE_COMPUTE_OP`; `NONE` = pure copy |
| +16 | `src_start_addr` (ADDR8, 8) | |
| +24 | `src_step_elem[2]` (i32 x,y) | signed strides |
| +32 | `src_num_elem[2]` (u16 x,y) | loop bounds |
| +36 | `src_elem_size` (u16) | |
| +38 | `src_bound_reg` / +39 `dst_bound_reg` | bounds-check regs |
| +40 | `dst_start_addr` (8) | |
| +48 | `dst_step_elem[2]` / +56 `dst_num_elem[2]` | |
| +60 | `dst_elem_size` (u16) | |
| +62 | `in_dtype` / +63 `out_dtype` | cast pair |

This struct backs the `DGE $S[]...[2dim] cast:0x%x->0x%x compute_op:0x%x` POOL
log of the [backend selector](./dge-backend-selector.md).

### 3.2 `NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT` — INDIRECT kind (1-D + gather/scatter)

A 1-D access with dynamic gather/scatter indices. (HIGH/OBSERVED.) Key fields:
`idx_num_active_channels` (+14), `flags` (+15: `indirect_mode:2`, `gather_dim`,
`scatter_dim`, `non_unique_dst_idx`), `src_idx_start_addr` (ADDR4, +48),
`dst_idx_start_addr` (ADDR4, +52), `compute_op` (+60). Its validator references
`is_valid_dge_shape_reg(src_start_addr, src_step_elem)` — confirming the DGE
`num/step` shape **is** the ISA shape-register representation.

### 3.3 `NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT` — GATHER TRANSPOSE kind

The reshape-transpose descriptor. Field offsets match the header comments
exactly (HIGH/OBSERVED, compile-verified):

| Off | Field | Notes |
|---:|---|---|
| +0 | `header` (4) | |
| +4 | `events` (8) | |
| +12 | `semaphore` (1) | |
| +13 | `dma_configs` (1) | |
| +14 | `idx_num_active_channels` (1) | |
| +15 | `src_idx_bound_reg` (1) | |
| +16 | `src_start_addr` (ADDR8, 8) | |
| +24 | `src_step_elem[2]` (i32) | |
| +32 | `src_num_elem[2]` (u16) | |
| +36 | `src_idx_start_addr` (ADDR4, 4) | |
| +40 | `dst_start_addr` (ADDR8, 8) | |
| +48 | `reserved[3]` (3) | |
| +51 | `dst_bound_reg` (1) | |
| +52 | `dst_step_elem_1` (i32) | `dst_step_elem_0 == sizeof(dtype_hi)` (implicit) |
| +56 | `dst_num_elem[2]` (u16) | |
| +60 | `elem_size` (u16) | |
| +62 | `dtype` (DTYPE_PAIR) | src in `dtype_lo`, dst in `dtype_hi` |
| +63 | `flags` (GATHER_TRANSPOSE_FLAGS: `gather_dim:2`, `reserved:6`) | |

**Header-documented transpose constraints** (the xbar semantics, all OBSERVED):

- *"Xbar transpose tiles are 16×128 for 2B dtypes"* (line 28) — matches the
  `tile_src_rows` / `tile_src_cols` debug log of §4.4.
- 2-byte dtypes only initially, and `dtype_lo == dtype_hi` (no cast yet):
  `has_valid_gather_transpose_dtype` requires `type_size_check(...,2)` on both
  halves and equality.
- `elem_size` in `[2, 256]`, even.
- `dst_start_addr` must be 32 B aligned, and `dst_step_elem[1] % 32 == 0`
  (xbar HW constraint, lines 146/149).
- `gather_dim` must be **Y** (the slowest dim) "for now to simplify uCode"
  (line 148); `src_num_elem[gather_dim] % 16 == 0` (tile alignment, line 151);
  `src_num_elem[gather_dim] == dst_num_elem[0]` (line 155).
- *"DmaGatherTranspose is only available in Neuron+ (V3+)"* (line 141), gated by
  `has_valid_dma_gather_transpose_nc` (`nc >= V3`).
- Header **key use cases**: *"MOE MLP and chunked prefill attention"* (line 19).

### 3.4 Supporting enums (compile-verified)

- **`DGE_COMPUTE_OP`** `{NONE=0 (B=A memcpy), ADD=1, MULTIPLY=2, MAX=3, MIN=4}` —
  the `compute_op:0x%x` of the backend log; `NONE` = pure reshape/copy. (The
  enum constants appear in `aws_neuron_isa_tpb_enums.h` as `_n_NONE / _n_ADD /
  _n_MULTIPLY / _n_MAX / _n_MIN` with `n_MAX_PLUS_ONE = 5`.)
- **`INDIRECT_DIM`** `{X=0, Y=1, Z=2, W=3}` (`n_MAX_PLUS_ONE = 4`) — the
  `gather_dim`/`scatter_dim` axis selector; the transpose pins `gather_dim = Y`.

> **NOTE — per-gen ISA stability.** The CAYMAN and MARIANA `gather_xpose.h` differ
> only by the header banner comment (`NC-v3` vs `NC-v4`); the struct bytes are
> identical. **MARIANA_PLUS has no own `arch_isa` directory** (only
> cayman/mariana/maverick/sunda exist) — it *shares* the mariana ISA; its
> `arch-headers/mariana_plus/` tree is register-map-only (a2i/a2j APB dumps).
> Therefore the v4+ delta (§6) is a **firmware** add, not an ISA add. SUNDA ships
> `dma_indirect1d.h` but **not** `gather_xpose.h` — v3 lacks the
> transpose-reshape descriptor entirely (HIGH/OBSERVED, `ls` of the arch dirs).

---

## 4. The reshape-kind analysis & loop-nest compilation

The lowering is a 3-stage *analyze-then-emit* pipeline. All format strings below
read byte-exact from the MARIANA_PLUS NX POOL DEBUG DRAM (VA = file-off + 0x80000);
the identical strings (same field set, shifted offsets) appear on CAYMAN and
MARIANA. (HIGH/OBSERVED strings; the algorithm wiring INFERRED-HIGH from the
field set + the structs.)

### 4.1 `analyze_tensor_reshape` — per-tensor classification

Log at VA `0x8321e` (`dge_reshape.cpp`):

```text
S: DGE Reshape: Analyzed tensor (nelem_target = %u, step_target = 0x%X (%dP),
   base = 0x%X (%dP)): Reshape Strategy = %d, Requested Reshape Kind = %d,
   Partition Range = %d, Num Partitions = %d
```

For one tensor it computes:

- `nelem_target` — total element count of the reshaped *view*;
- `step_target` — the target stride, printed with a partition-count `(%dP)`
  suffix;
- `base` — the base address, also `(%dP)`;

then **decides** a `Reshape Strategy` (an integer enum) and a `Requested Reshape
Kind`, plus a `Partition Range` and `Num Partitions`. The `%dP` suffix means *the
value expressed in SBUF partitions* — the reshape reasons in `(partition, free)`
coordinates, not flat byte offsets. Annotated reconstruction:

```c
// dge_reshape.cpp — analyze_tensor_reshape  (VA 0x8321e log)
// Per-tensor: derive the reshaped view geometry and pick a strategy/kind.
struct reshape_view {
    uint32_t nelem_target;   // total elements of the reshaped view
    int32_t  step_target;    // target stride (in partitions: "%dP")
    uint32_t base;           // base addr (in partitions: "%dP")
    int      strategy;       // Reshape Strategy enum  (values not byte-recoverable)
    int      requested_kind; // Requested Reshape Kind (DIRECT2D / INDIRECT / GATHER_XPOSE / sb2sb)
    int      part_range;     // Partition Range spanned
    uint32_t num_partitions; // # SBUF partitions touched
};

static reshape_view analyze_tensor_reshape(const dge_shape *t, dtype_t dt) {
    reshape_view v;
    // Inputs to the decision: the tensor's {num_elem[4], step_elem[4]}, its dtype,
    // and whether the view is contiguous / mergeable / requires an axis swap.
    v.nelem_target = product_of(t->num_elem);            // collapse the N-D view
    v.step_target  = compute_target_stride(t);           // in partition units
    v.base         = t->base >> PARTITION_SHIFT;          // "%dP"
    // Classify: identity (no remap) | dim-merge | dim-split | transpose (axis swap)
    //   -> a Reshape Strategy, and a Requested Reshape Kind tied to a descriptor type.
    v.strategy        = classify_reshape(t);              // MED — enum values not recoverable
    v.requested_kind  = kind_for_strategy(v.strategy, dt);
    v.part_range      = partition_span(t);
    v.num_partitions  = count_partitions(t);
    return v;  // logged via the VA 0x8321e format string
}
```

> **NOTE.** The four classes — *identity* (no remap), *dim-merge*, *dim-split*,
> *transpose* (axis swap) — are the natural reshape taxonomy implied by the
> `Reshape Strategy` / `Requested Reshape Kind` split; the exact integer enum
> values are **not** byte-recoverable (the body is FLIX-desynced). Tagged
> MED/INFERRED. The *inputs* (shape `num_elem[4]`, strides `step_elem[4]`, dtype)
> are OBSERVED from the descriptor structs and the BEFORE/AFTER log (§4.5).

### 4.2 Pair assessment — resolve final Kind + DMA fan-out

Log at VA `0x832df`:

```text
S: DGE Reshape: Assessed tensor pair params (Requested Reshape Kind = %d,
   Min Num Partitions = %u, #DMA Avail = %u): Reshape Kind = %d, #DMA = %u
```

Given the src+dst pair's *Requested Kind*, the *Min Num Partitions* needed, and
the *#DMA channels available*, it **resolves** the final `Reshape Kind` and the
`#DMA` to use. This is the descriptor-count / DMA-fan-out decision: a reshape
spanning *P* partitions across *D* DMAs. The `#DMA Avail` input is the
`MEMCOPY_DMA_CFG` local reg (see §8).

### 4.3 Simple pair assessment — the HBM-only fast case

Log at VA `0x83372`:

```text
S: DGE Reshape: Assessed simple tensor pair params (Num Partitions A = %u,
   Num Partitions B = %u, #DMA Avail = %u, HBM Only = %d): Reshape Kind = %d,
   #DMA = %u
```

The simple case (e.g. HBM-to-HBM, no SBUF-partition remap) picks `Kind` + `#DMA`
directly from the two operands' partition counts. `HBM Only = %d` gates a simpler
stride math (no partition axis to reconcile).

### 4.4 `tensor_reshape_transpose` — the xbar/gather-transpose lowering

Name string at VA `0x836a2`; the apply lives in `dge_reshape_apply_impl.hpp`
(string at VA `0x836bb`) — so the *analysis* is in the `.cpp`, the *apply* in the
`.hpp`. Log at VA `0x836f3`:

```text
S: tensor_reshape_transpose: tile_src_rows=%d tile_src_cols=%d dtype_size=%d
```

The transpose is **tile-based**: it walks the src in `(tile_src_rows ×
tile_src_cols)` tiles — the 16×128 xbar tiles for 2-byte dtypes from §3.3 — and
emits a `GATHER_XPOSE` descriptor per tile-group. `dtype_size` selects the tile
geometry. (HIGH/OBSERVED string; per-tile descriptor emit INFERRED-HIGH from the
xbar header constraints.)

A separate **Q7-side** assert site for this symbol decodes cleanly (one of the
two clean anchors), CAYMAN Q7 IRAM `@0x15640`:

```text
1565a:  a4003e   const16  a10, 0x3e00   ; -> "tensor_reshape_transpose" (Q7 DRAM VA 0x83e00)
1565d:  b4193e   const16  a11, 0x3e19   ; next string (__func__/condition)
15660:  24e860   const16  a2,  0x60e8   ; __assert_func entry
15663:  e00200   callx8   a2            ; the assert idiom
```

(The bytes at Q7-DRAM file-offset `0x3e00` read `tensor_reshape_transpose`,
confirming the `const16 a10, 0x3e00` target. HIGH/OBSERVED.)

### 4.5 The working state — `{elem_size, num_elem[4], step_elem[4], is_src}`

`tensor_reshape_indirect_transpose` (name at VA `0x83724`; present 3× on each of
CAYMAN/MARIANA/MARIANA_PLUS) exposes the reshape's internal working state
**byte-exact** through its BEFORE/AFTER logs:

```text
BEFORE @VA 0x837c2:
  ...BEFORE: elem_size=%u num_elem=[%u,%u,%u,%u] step_elem=[%d,%d,%d,%d] is_src=%d
AFTER  @VA 0x83836:
  ...AFTER:  elem_size=%u num_elem=[%u,%u,%u,%u] step_elem=[%d,%d,%d,%d]
```

So the internal tensor representation is:

```c
struct dge_reshape_state {
    uint16_t elem_size;       // bytes per element
    uint16_t num_elem[4];     // per-dim loop bounds   (== dge_shape.num)
    int32_t  step_elem[4];    // per-dim signed strides (== dge_shape.step); negative = reverse-axis
    bool     is_src;          // src vs dst tag
};
```

This is exactly the DGE 4-dim `dge_shape = {num[4], step[4]}` carried from
[DGE setup](./dge-setup.md), plus `elem_size` and a `is_src` tag.

> **The transpose is a stride permutation.** The `_indirect_transpose` log prints
> `num_elem[]`/`step_elem[]` BEFORE and AFTER. Since the axis-permutation
> manifests as a permutation of those two arrays (`AFTER.step_elem[] !=
> BEFORE.step_elem[]`), the transpose is realized as the canonical *"transpose =
> permute the strides"* lowering — no data is moved by the analysis; only the
> descriptor's stride/count arrays are reordered. The signed `step_elem` (i32,
> printed `%d`) admits negative strides (reverse-axis walks). (HIGH/OBSERVED
> field set; the permutation reading INFERRED-HIGH from BEFORE != AFTER.)

### 4.6 Loop-nest → descriptor compilation (the DIMPUSH nest)

Once `num_elem[4]`/`step_elem[4]` are set (post-transpose), the reshape lowers
the 4-dim walk onto a target `DMA[d]` via the three push primitives carried from
[DGE setup](./dge-setup.md) / [DGE emit](./dge-emit.md):

```text
push GENERATE to DMA[d]: %s : addr, elem_size, sem_num     ; %s = RD/WR direction tag
push DIMPUSH  to DMA[d]: [num,num][step,step]              ; ONE per reshape dim
push REGWRITE to DMA[d]                                    ; CAYMAN/MARIANA only — RETIRED on MARIANA_PLUS (§6)
```

Each **`DIMPUSH` carries one nesting level** of the loop-nest: `[num, num]` is the
loop bound and `[step, step]` the stride of that dimension. The reshape struct's
4 `num_elem`/`step_elem` entries therefore become up to four `DIMPUSH` ops — the
nested BD-generation loop-nest:

```text
reshape state  ->  GENERATE (base+dir+sem)  ->  DIMPUSH(dim0) ... DIMPUSH(dim3)  ->  BD walk
{num[4],step[4]}        per-DMA-port                 one per dimension              nested SDMA descriptors
```

> **NOTE.** The push primitives and `dge_shape` are OBSERVED (carried from DGE
> setup); the *one-DIMPUSH-per-reshape-dim* mapping is the consistent reading of
> the `[num,num][step,step]` payload against the 4-dim shape (INFERRED-HIGH, the
> exact body is FLIX-desynced). The `GENERATE` direction `%s` is `RD`/`WR` —
> string region in MP NX DRAM is adjacent to `wait_for_credit` (§6).

### 4.7 The Q7-side spray machinery — `gen_spray_info` / `make_gather_pattern`

On the Q7 POOL image the reshape→descriptor lowering is driven by two helpers
(present CAYMAN..MARIANA_PLUS; absent on SUNDA). Their byte-exact logs (Q7 DRAM,
VA = off + 0x80000) reveal the control flow:

**`gen_spray_info`** — per-operand reshape-apply decision:

```text
START  @VA 0x83c63: gen_spray_info: reshape_kind=%d, do_indirection=%d,
                    is_our_tensor=%d, hbm_to_hbm=%d, ndma=%zu
  -> @0x83cc7: gen_spray_info: applying indirection reshape
       -> indirection result step_port=%d, step_half=%d
  -> @0x83d3e: gen_spray_info: applying tensor reshape for our tensor
       -> tensor result step_port=%d, step_half=%d
  -> @0x83dba: gen_spray_info: skipping reshape (not our tensor and no indirection)
```

For each operand it applies the indirection-reshape (if `do_indirection`), the
tensor-reshape (if `is_our_tensor`), or skips. The reshape produces a
`(step_port, step_half)` pair: `step_port` = the per-DMA-port stride (which
physical DMA port the descriptor sprays to), `step_half` = a half/sub-stride
within a port. `hbm_to_hbm` + `ndma` steer the fan-out. (HIGH/OBSERVED strings;
`step_port`/`step_half` semantics INFERRED-HIGH.)

**`make_gather_pattern`** — turns the spray strides into a gather pattern:

```text
START @VA 0x83fe5: make_gather_pattern START: reshape_kind=%d, step_port=%d,
                   step_half=%d, eng_mask=0x%x
END   @VA 0x84041: make_gather_pattern END: pattern=0x%x, mask=0x%x
```

It consumes `(reshape_kind, step_port, step_half, eng_mask)` and emits a bit
`pattern` + `mask` — the descriptor-spray distribution across the engine's DMA
channels (`eng_mask` = which engines/DMAs participate; `pattern`/`mask` = the
interleave). This is the bridge from the reshape's logical stride to the physical
multi-DMA descriptor placement, which then feeds the 3-KIND `dge_decode_fast`
dispatch and `rdma_desc_gen`/`rdma_desc_start`. (HIGH/OBSERVED strings.)

---

## 5. The transpose descriptor end-to-end

Putting §3 and §4 together, a transpose request flows:

```text
analyze_tensor_reshape  ->  pair-assess (Kind, #DMA)  ->  tensor_reshape_transpose
   (per-tensor view)         (resolve fan-out)            (tile 16x128, permute strides)
        |                                                       |
        v                                                       v
  Reshape Strategy/Kind                            GATHER_XPOSE descriptor (64 B)
        |                                          gather_dim=Y, dtype_lo==dtype_hi (2B)
        v                                                       |
  gen_spray_info -> make_gather_pattern  -->  per-DMA descriptors  -->  RDM doorbell
   (step_port, step_half, eng_mask)            (the xbar HW transposes during transfer)
```

The transpose is performed **at the descriptor level**: the DGE *builds* the
`DmaGatherTranspose` descriptor; the **DMA/xbar hardware** performs the actual
16×128-tile transpose during the transfer (§6.5 contrasts this with the DVE
StreamTranspose opcode, which transposes in-compute).

---

## 6. The MARIANA_PLUS (v4+) FAST-PATH — the gen-delta

This is the page's headline: the **one functional add** that distinguishes
MARIANA_PLUS (v4+) from MARIANA (v4). It is a *firmware* add (the ISA is shared —
§3.4 NOTE), comprising a new fast reshape-transpose function, a new SB-to-SB
transpose kind, and a DMA flow-control primitive.

### 6.1 Per-gen presence map (byte-grounded)

Grep over the carved POOL DEBUG DRAMs (`rg -a -c '<string>' <image>.bin`):

**NX POOL DEBUG DRAM:**

| String | SUNDA (v3) | CAYMAN | MARIANA (v4) | MARIANA_PLUS (v4+) |
|---|:---:|:---:|:---:|:---:|
| `dge_reshape.cpp` | 1 | 1 | 1 | 1 |
| `analyze_tensor_reshape` | 1 | 1 | 1 | 1 |
| `DGE Reshape:` (strategy logs) | **0** | 3 | 3 | 3 |
| `tensor_reshape_transpose` | **0** | 1 | 1 | **2** |
| `Setting up DGE` | **0** | 1 | 1 | 1 |
| `push REGWRITE` | 0 | 1 | 1 | **0 (retired)** |
| `memcopy_transpose_fast` | 0 | 0 | 0 | **1** |
| `tensor_reshape_transpose_sb2sb` | 0 | 0 | 0 | **1** |
| `wait_for_credit` | 0 | 0 | 0 | **1** |
| `dge_decode_fast.cpp` | 0 | 0 | 0 | **1** |

**Q7 POOL DEBUG DRAM:**

| String | CAYMAN Q7 | MARIANA_PLUS Q7 |
|---|:---:|:---:|
| `make_gather_pattern` | 2 | 2 |
| `gen_spray_info` | 6 | 6 |
| `tensor_reshape_transpose_sb2sb` | **0** | **1** |
| `tensor_reshape_transpose` | 1 | **2** |

Readings (HIGH/OBSERVED unless noted):

- **The reshape analysis code exists from SUNDA(v3)** — but SUNDA carries *only*
  the `analyze_tensor_reshape` name + the `dge_shape` working state. It has no
  `DGE Reshape:` strategy logs, no `tensor_reshape_transpose`, no `tile_src_rows`,
  no `Setting up DGE`: SUNDA is a **stub/minimal reshape**. The full
  transpose-reshape engine + the DGE setup/backend/emit arrive with **CAYMAN**.
- The fast-path subsystem (`memcopy_transpose_fast` / `dge_decode_fast.cpp` /
  `wait_for_credit`) is **exclusive to MARIANA_PLUS NX POOL** (`0/0/0/1`).
- `tensor_reshape_transpose_sb2sb` (a new reshape *kind*) is on **both** MP NX and
  MP Q7 (`0→1` on each) — the SB-to-SB transpose spans NX + Q7.
- `tensor_reshape_transpose` count goes `1 → 2` on MARIANA_PLUS NX (and Q7); the
  extra hit is the `_sb2sb` variant string.

### 6.2 The fast-path strings are real compiled symbols

> **QUIRK.** The four fast-path names are not stray text — they also appear in the
> *symbol-bearing* MARIANA_PLUS NX POOL **TEST** DRAM (`dge_decode_fast`,
> `dge_reshape_memcopy_transpose_fast`, `tensor_reshape_transpose_sb2sb`,
> `wait_for_credit`), i.e. they are real compiled symbols, not debug-log
> artifacts. (HIGH/OBSERVED.)

### 6.3 Where the fast-path lives — `dge_decode_fast.cpp`

In the MARIANA_PLUS NX DRAM the strings are **byte-adjacent** (file-offsets
`0x34b0` / `0x34d3`; device VA `0x834b0` / `0x834d3`):

```text
0x34b0: "dge_reshape_memcopy_transpose_fast\0dge_decode_fast.cpp\0"
```

So `dge_reshape_memcopy_transpose_fast` is the `__func__` and
`dge_decode_fast.cpp` is its `__FILE__` — the fast reshape path is implemented in
**`dge_decode_fast.cpp`**, the *same* translation unit as the DGE
context-init/decode setup (see [DGE setup](./dge-setup.md)). The fast path is
integrated into the decode/setup TU, not a separate reshape file. (HIGH/OBSERVED
adjacency; `wait_for_credit` sits at VA `0x83914`, in the same emit-tag region as
the `RD`/`WR` GENERATE direction tags.)

### 6.4 What makes it "fast"

- **`dge_reshape_memcopy_transpose_fast`** — a specialized reshape-transpose that
  *fuses the memcopy + transpose into one descriptor-gen pass*, versus the
  general `analyze → pair-assess → per-tile-emit` path of §4. The header's
  call-out (MOE MLP, chunked-prefill attention — §3.3) is the contiguous/tiled
  case this fast path targets. (Names OBSERVED; fusion reading INFERRED-HIGH.)
- **`tensor_reshape_transpose_sb2sb`** — an **SBUF-to-SBUF** transpose kind with
  *no HBM round trip*. It feeds the SB2SB transport directly (the device leg of a
  collective; the symbol also surfaces in `remote_copy` /
  `libnrtucode_extisa.so`). See [gather-scatter descriptors](../../dma/gather-scatter-descriptors.md)
  for the downstream descriptor ring.
- **`wait_for_credit`** — an explicit DMA flow-control wait (block until the
  descriptor-ring has *credit* before pushing more BDs). In DRAM it sits adjacent
  to the `RD`/`WR` direction tags and the `Setting up DGE` string — i.e. part of
  the setup/emit flow, throttling the fast-path's higher descriptor-issue rate
  against ring depth. (String + position OBSERVED; credit-counter mechanics
  LOW/flagged.)

### 6.5 `push REGWRITE` retired — the decisive correlate

`push REGWRITE to DMA` is present on CAYMAN and MARIANA but **absent on
MARIANA_PLUS** (`0/1/1/0` in §6.1). The DMA trigger/register programming the
separate `REGWRITE` did (in the generic emit) is folded into the streamlined
`dge_reshape_memcopy_transpose_fast` emit on v4+. This retirement landed *exactly*
when the fast-path arrived, making it the decisive gen-delta correlate.
(Presence HIGH/OBSERVED; the "REGWRITE folded into fast path" reading
INFERRED-HIGH.)

### 6.6 Structural corroboration — IRAM grew, entries fell

Disassembled with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`),
counting `entry` prologues (`rg -c '\tentry\t'`):

| Image | NX IRAM size (B) | `entry` prologues |
|---|---:|---:|
| MARIANA (v4) | 114 816 | **697** |
| MARIANA_PLUS (v4+) | 119 616 (`+0x12C0`) | **675** |

MARIANA_PLUS has a **larger IRAM** yet **fewer function entries** — bigger,
more-inlined functions. The fast path is a *consolidation* (inline the spray +
gather-pattern + emit into one big function), consistent with the `REGWRITE`
retirement and the +0x12C0 growth. (HIGH/OBSERVED counts; the
inline-consolidation reading INFERRED-HIGH. Exact per-frame sizes are
FLIX-desync-unreliable and not claimed.)

### 6.7 Why this is the one v4+ functional add

Across the per-gen map, MARIANA_PLUS adds **no new ISA** (it shares the mariana
`gather_xpose` ISA — §3.4) and **no new descriptor struct**. Its only new
*firmware behaviour* in the reshape engine is this fast-path triple
(`memcopy_transpose_fast` + `sb2sb` + `wait_for_credit`) in `dge_decode_fast.cpp`,
plus the `REGWRITE` retirement. That makes it the single v4+ functional addition
this page characterizes. For the broader generation diff see
[mariana-plus delta](../../generations/mariana-plus-delta.md).

---

## 6.5. Contrast — reshape-DGE transpose vs DVE StreamTranspose

There are **two unrelated transpose mechanisms** in the device, at two different
layers. The DGE reshape transpose documented here is *not* the DVE
StreamTranspose opcode:

| Aspect | DGE reshape transpose (this page) | DVE `StreamTranspose` ([kernel page](../kernels/stream-transpose.md)) |
|---|---|---|
| Layer | Descriptor-generation (builds a 64-B descriptor) | Compute *opcode* (executed by the engine) |
| Engine | POOL/Q7 GPSIMD — "SW-DGE backend" | DVE vector engine |
| Mechanism | DMA/xbar HW transposes 16×128 tiles *during transfer* | Datapath/streaming transpose on SBUF-resident data |
| Symbol | `tensor_reshape_transpose` → `DmaGatherTranspose` | a DVE scan/transpose/shuffle op |
| Data movement | gather from HBM/SBUF → xbar → SBUF | in-place over data already in SBUF |
| Cast | `dtype_lo == dtype_hi` (no cast yet) | n/a (compute op) |

They differ in layer (descriptor-gen vs compute opcode), engine (POOL/Q7 DGE vs
DVE), and mechanism (xbar-on-DMA vs DVE-datapath). The `sb2sb` fast-path reshape
(§6.4) is **still** a *descriptor* transpose — it generates the SB2SB descriptors;
it is not a DVE `StreamTranspose`. (HIGH/OBSERVED — header layer statement +
distinct symbol sets.)

---

## 7. Boundaries — selector, emit, transport, errors

After analyze/reshape resolves `Kind` + `#DMA`, the DGE selects a backend. The
selector anchor re-decodes cleanly in the MARIANA_PLUS NX IRAM (`@0xf9b0`, the
second clean anchor, HIGH/OBSERVED):

```text
f9b0:  44e05f   const16  a4,  0x5fe0   ; a4 = &backend-availability table (BSS)
f9b3:  a49731   const16  a10, 0x3197   ; -> "DGE: Select backend Pool" (MP DRAM VA 0x83197)
f9b6:  0c05     movi.n   a5,  0
f9b8:  3804     l32i.n   a3,  a4, 0     ; backend[Pool].available
f9ba:  16f307   beqz     a3,  0xfa3d    ; if !available -> next / NO-BACKEND arm
```

(The bytes at MP NX DRAM file-offset `0x3197` read `S: DGE: Select backend Pool`,
confirming the `const16 a10, 0x3197` target.) The resolved `Reshape Kind` + the
chosen descriptor struct feed the per-backend descriptor *builders* owned by the
[backend selector](./dge-backend-selector.md) (`Pool | RTL | software |
NO-BACKEND`).

- **Emit** — the post-transpose `num_elem[4]`/`step_elem[4]` drive the three push
  primitives (§4.6); the exact `SDMA_CME_BD_DESC` word packing is owned by
  [DGE emit](./dge-emit.md).
- **SB2SB transport** — `tensor_reshape_transpose_sb2sb` targets the SBUF-to-SBUF
  collective transport; the reshape generates the descriptors, then the RDMA ring
  builder/drainer rings the doorbell. See
  [gather-scatter descriptors](../../dma/gather-scatter-descriptors.md) (Part 9).
- **DMA-avail / carveout** — the `#DMA Avail` the reshape consults (§4.2/§4.3) is
  the `MEMCOPY_DMA_CFG` local reg (`AWS_NEURON_ISA_XT_MEMCOPY_DMA_CFG = 40`:
  lower-16 = DMA bitfield, upper-16 = queue bitfield). The descriptor RAM the BDs
  go into is the DGE Pool carveout (`MEMCOPY_CARVEOUT_CFG = 39`: lower-16 =
  per-partition offset in #descriptors (64-B aligned), upper-16 = max #descriptors
  per memcopy), backed by `DGE_MEMORY`. (HIGH/OBSERVED header.)
- **Errors / completion** — every descriptor is guarded by `S: DGE: Failed bounds
  check. $S[%i]+=%i.` (the per-desc src/dst `BOUND_CHECK_REG` fields, §3); the
  DGE-packet-notification feature flag gates completion notifications. Owned by
  [DGE errors](./dge-errors.md).

---

## 8. Per-generation feature matrix

| Feature | SUNDA (v3) | CAYMAN | MARIANA (v4) | MARIANA_PLUS (v4+) |
|---|:---:|:---:|:---:|:---:|
| `dge_shape` (num/step working state) | yes | yes | yes | yes |
| `analyze_tensor_reshape` (name / stub) | yes | yes | yes | yes |
| `DGE Reshape:` strategy / partition logs | **no** | yes | yes | yes |
| `tensor_reshape_transpose` (xbar tile) | **no** | yes | yes | yes |
| `tensor_reshape_indirect_transpose` | **no** | yes(3) | yes(3) | yes(3) |
| DGE setup + backend select + push ops | **no** | yes | yes | yes |
| `push REGWRITE` | no | yes | yes | **no (retired)** |
| `gather_xpose` ISA descriptor (V3+) | **no\*** | yes | yes | yes (shared ISA) |
| `make_gather_pattern` / `gen_spray_info` (Q7) | no | yes | yes | yes |
| `dge_reshape_memcopy_transpose_fast` | no | no | no | **yes** |
| `tensor_reshape_transpose_sb2sb` | no | no | no | **yes (NX+Q7)** |
| `wait_for_credit` | no | no | no | **yes** |
| `dge_decode_fast.cpp` (fast-path TU) | no | no | no | **yes** |

`*` SUNDA ships `dma_indirect1d.h` but **not** `gather_xpose.h` — no
transpose-reshape ISA (§3.4).

> **LOW / flagged — MAVERICK (v5).** MAVERICK ships `gather_xpose.h` (own
> `arch_isa` dir) but no MAVERICK NX_POOL DGE image was carved —
> v5 reshape behaviour is header-OBSERVED only, INFERRED, not claimed here.

---

## 9. Confidence summary

- **HIGH / OBSERVED** — where the DGE runs (`gather_xpose.h` "SW-DGE backend with
  Q7 processors"); the reshape format-string corpus (§4, byte-exact); the
  `gen_spray_info` / `make_gather_pattern` Q7 flow (§4.7, byte-exact); the three
  64-B descriptor structs + offsets + the `DGE_COMPUTE_OP` / `INDIRECT_DIM` enums
  (§3, gcc compile-verified `sizeof == 64`); the xbar 16×128-tile constraints
  (§3.3 header); the per-gen presence map + `REGWRITE` retirement +
  fast-path-symbols-in-TEST (§6); the fast-path TU localization
  (`dge_decode_fast.cpp`, byte-adjacent, §6.3); the backend-select and Q7 assert
  anchors (§7, §4.4); IRAM-size / entry-count delta (§6.6). All carve hashes match.
- **HIGH / INFERRED** — reshape = analyze → pair-assess → per-tile-emit pipeline
  (§4 from the field set); transpose = stride permutation (BEFORE != AFTER, §4.5);
  `step_port` / `step_half` = per-DMA-port spray strides (§4.7); fast-path =
  inline consolidation (IRAM-grew-but-fewer-entries, §6.6); `REGWRITE` folded into
  fast path (§6.5).
- **MED** — reshape function *bodies* are FLIX-desynced (no per-instruction
  decode); the exact `Reshape Strategy`/`Kind` integer enum values are not
  byte-recoverable; the one-`DIMPUSH`-per-reshape-dim mapping (§4.6).
- **LOW / flagged** — MAVERICK (v5) NX_POOL DGE not carved; the precise
  `wait_for_credit` credit-counter mechanics; whether the `sb2sb` kind ever uses a
  non-Y `gather_dim` (the header pins Y for now).

## See also

- [DGE Setup + Context Init](./dge-setup.md) — `dge_shape`, the decode setup TU.
- [DGE 3-Backend Selector](./dge-backend-selector.md) — the `Pool | RTL |
  software | NO-BACKEND` selection the resolved Kind feeds.
- [DGE Descriptor-Emit Path](./dge-emit.md) — the `GENERATE`/`DIMPUSH`/`REGWRITE`
  BD word packing.
- [DGE Error Notifications](./dge-errors.md) — bounds-check + completion notify.
- [StreamTranspose (DVE datapath transpose)](../kernels/stream-transpose.md) —
  the distinct DVE-compute transpose.
- [Gather/Scatter Descriptors](../../dma/gather-scatter-descriptors.md) — the
  SB2SB descriptor ring the `sb2sb` reshape feeds.
- [MARIANA_PLUS Delta](../../generations/mariana-plus-delta.md) — the broader v4+
  generation diff.
- [The Confidence & Walls Model](../../reference/confidence-model.md).
