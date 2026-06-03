# LlvmTpu Intrinsic Catalog

> *Every address, symbol, offset, and string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, **not stripped**, `.text`/`.rodata` VMA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

The TPU backend has two distinct op surfaces. The `tpu` dialect that [tpu → LLO ODS Lowering](tpu-to-llo-ods.md) covers is the *TensorCore* target dialect — ~322 ops that descend to LLO bundles. This page documents the *other* surface: the **`LlvmTpuDialect`**, a separate MLIR dialect of **1356 `tpu_*` intrinsic ops** that is the bottom-of-stack instruction surface for the **SparseCore** — the embedding-processing co-processor. Each op is registered as `mlir::sparse_core::tpu_X_Y_Z` and prints as the LLVM-intrinsic name `llvm.tpu.X.Y.Z` (underscore→dot). These are not lowered to LLO; they descend through [Lower to SparseCore LLVM](lower-to-sparsecore-llvm.md) into honest LLVM-IR intrinsic calls and reach the SparseCore LLVM backend, where [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) and the per-engine ISel match them.

The reader who knows LLVM should hold one analogy: this is a TableGen `IntrinsicsTPU.td` surface materialised as an MLIR dialect. Every `llvm.tpu.*` name is an LLVM intrinsic; every name is *also* a registered MLIR op carrying the standard 23-slot `RegisteredOperationName::Model<Op>` ABI. The two views are the same set — recovered two independent ways (1356 `Model<tpu_*>` vtables and 1356 `llvm.tpu.*` printed-name strings, set-identical under the underscore→dot map). The dominant structural fact is that **op identity, not an attribute, encodes the hardware variant**: 834 of the 1356 (62 %) are the `tpu_stream_*` embedding gather/scatter family, a `{pattern × verb × dtype × memspace}` cross-product where each cell is its own registered op. There is no `dtype` attribute on a stream op; the dtype is in its name.

This page owns the **family taxonomy, the representative per-family signatures, and the ID-table organisation** (the 10-batch registrar). It does not re-derive the per-cast address-space ISel (that is [addrspacecast ISel](../sparsecore/addrspacecast-isel.md)), the `tpu`-dialect ODS (that is [tpu → LLO ODS Lowering](tpu-to-llo-ods.md)), or the SparseCore back-end engine encodings (those are the [SparseCore ISA slot pages](#cross-references)). For reimplementation, the contract is:

- **The Model↔printed-name correspondence** — the mechanical `tpu_X_Y` ↔ `llvm.tpu.X.Y` map and why it is 1:1 with zero mismatch.
- **The 20-class functional taxonomy** — every one of the 1356 assigned to exactly one class; the classes sum to 1356; each class mapped to its SparseCore engine or LLO/EmitX target.
- **The ID-table organisation** — the 10-batch `registerLlvmTpuDialectOperations0..9` registrar, why it is split, and how it differs from the 115-op ScDialect path.
- **The ODS shape recovery rule** — how the typed `tpu_*::create` arg list *is* the operand/result declaration, with the verified per-family arities.

| | |
|---|---|
| **Dialect class** | `mlir::sparse_core::LlvmTpuDialect` (ctor `LlvmTpuDialectC1EPNS_11MLIRContextE`) |
| **Intrinsic count** | **1356** (`Model<tpu_*>` vtables == `llvm.tpu.*` strings, set-identical) |
| **Registrar** | `registerLlvmTpuDialectOperations` @ `0x146d0560` → tail-calls `…Operations0..9` |
| **Printed-name rule** | C++ `tpu_X_Y_Z` ↔ intrinsic `llvm.tpu.X.Y.Z` (`_`→`.`); `comm -23` == 0 |
| **Address spaces** | `LlvmTpuDialect::AddressSpaceDescription(int)` @ `0x135462c0` (base 0xc9=201, span 0x18) |
| **Consuming pass** | `LowerToSparseCoreLlvmPass::runOnOperation` @ `0x13566d00`; `lowerFunc` @ `0x13568280` |
| **Dominant class** | `tpu_stream_*` — **834 / 1356** (SparseCore embedding gather/scatter engine) |
| **Typed-create coverage** | 466 / 1356 carry a typed `create` (ODS shape read off the symbol); 890 default-builder |
| **Confidence** | CONFIRMED (byte-anchored) for enumeration/taxonomy/registration; per-class HW target as marked |

---

## The Two-Surface Model

### Purpose

Before the catalog, the reader must hold why a *second* TPU op dialect exists at all, and why the page that owns the `tpu`-dialect→LLO descent does not own this one. The split is structural, not incidental: TensorCore and SparseCore are different machines with different ISAs, and the compiler models them as different MLIR dialects.

### The split

`tpu` dialect (322 ops, [tpu → LLO ODS Lowering](tpu-to-llo-ods.md)) is the TensorCore target: matmul/MXU, VPU vector ops, sequencer, descended to `llo.*` and packed into VLIW bundles. `LlvmTpuDialect` (1356 ops, this page) is the SparseCore target: an embedding-table stream processor whose primitives are indirect gather/scatter, sflag atomics, circular-buffer registers (CBREG), and address-space casts between the SparseCore memory pools. The SparseCore ops are *not* packed into LLO bundles — they become LLVM-IR intrinsic calls and are code-generated by the SparseCore LLVM backend.

> **NOTE —** the prior dialect-inventory work recorded `LlvmTpuDialect` as having "none distinct" registered ops because it scanned the 115-op ScDialect `addOperations` path (`0x14594f60`). The 1356 intrinsics never go through that path; they register through the separate 10-batch `registerLlvmTpuDialectOperations` path documented in [ID-Table Organisation](#id-table-organisation). The intrinsics are real, registered, and verifiable — they were simply on a different registrar.

### The Model↔printed-name correspondence

Every intrinsic exists in the binary twice, and the two forms are mechanically interconvertible:

```c
// (a) the registered MLIR op — a RegisteredOperationName::Model<Op> vtable:
//        _ZTVN4mlir23RegisteredOperationName5ModelINS_11sparse_core11tpu_syncaddEEE
//     1356 such vtables for mlir::sparse_core::tpu_* classes.
// (b) the printed LLVM-intrinsic name string, in .rodata:
//        "llvm.tpu.syncadd"
//     1356 such strings.
//
// The map is purely lexical:
//     class  tpu_X_Y_Z   ->   intrinsic  llvm.tpu.X.Y.Z      // s/^tpu_/llvm.tpu./ ; s/_/./g
// Applying it to the class list and diffing against the string list:
//     comm -23 (mapped-classes) (string-list)  ==  0         // zero mismatch, both ways
```

So each registered op has exactly one printed `llvm.tpu.*` name and vice-versa. This is the canonical `IntrinsicsTPU.td`-derived surface: the C++ class is the ODS-registered MLIR op; the `llvm.tpu.*` string is the LLVM-intrinsic name it carries into the backend.

> **GOTCHA —** the dot/underscore boundary is *only* the prefix and field separators, not a tokenizer. `tpu_dma_hbm_to_tilespmem_sc_simple` ↔ `llvm.tpu.dma.hbm.to.tilespmem.sc.simple` — every `_` becomes `.`, including the ones inside `hbm_to_tilespmem`. A reimplementer that special-cases `_to_` will desync the two name spaces.

---

## Functional-Class Taxonomy

### Purpose

The 1356 intrinsics partition into 20 functional classes; each op belongs to exactly one class and the classes sum to 1356. This is the page's central reimplementation artifact: it tells a reader *which SparseCore hardware unit* each named intrinsic drives, without dumping 1356 rows. The taxonomy is recovered by name-family grouping cross-checked against the printed-name string set; the per-class hardware target is joined to the SparseCore engine pages and the V5+ EmitX bit-position work.

### The 20 classes

`#ops` = the class size; `HW target` = the SparseCore engine / LLO slot it lowers to; `st` = C (encoding byte-confirmed in a sibling page) or I (engine confirmed, per-leaf opcode inferred).

| #ops | functional class (`tpu_*` prefix) | HW target / lowering | st |
|-----:|---|---|---|
| 834 | sparsecore-stream (`stream_*`) | SparseCore stream/scatter engine descriptor (linear/strided/indirect × gather/scatter/vreg, CBREG-windowed); → LLVM call via LowerToSparseCoreLlvm | I |
| 87 | pack / unpack (`pack*`, `unpack*`) | VPU pack/unpack slot → `llo.vpack*`/`llo.vunpack*`; sub-byte staging (b32→b16→b8→b4→b2→b1, e4m3/e5m2/s8/u8) for MXU/quant | C |
| 74 | vector load/store (`vld*`, `vst*`) | VPU mem slot → `llo.vector_{load,store}[_masked]`; `_idx` indexed, `_cb` CBREG-windowed, `_strided`, `_add` scatter, `_np` no-predicate | C |
| 47 | semaphore wait/watch (`wait*`, `watch*`) | sflag VWait slot → `llo.vwait.{eq,ne,lt,le,gt,ge}[.done]`; `_yieldable`=seq-yield, `_imem`=instr-mem, `ordone`=or-done | C |
| 40 | DMA descriptor (`dma_*_sc_*`) | SparseCore DMA engine cmd (simple/single_strided/general tiers); 8/10/16-operand descriptor; cf ScDialect DmaSimpleStart | C |
| 32 | scan / segment-scan / reduce (`{add,min,max}_*scan*`) | SparseCore scan unit (add/max/min × full/half/1xN/2xN × seg × index) — embedding-aggregation primitives | I |
| 28 | vector convert (`vcvt*`, `cvt*`) | VPU convert slot → `llo.vcvt.*`; f32↔bf16/bf8/hf16/if8/s4/s8/u4/u8; `_sr`=stochastic-round, `_pr`=probabilistic | C |
| 26 | semaphore set/add (`syncadd*`, `syncset*`, `sfence`) | sflag VSync slot → `llo.vsync.{add,set}[.done,.remote]`; `_remote`=ICI peer, `_tile`/`_pa`=tile/public bank, `_doneinv`=invert done | C |
| 24 | transcendental / EUP (`rcp`,`rsqrt`,`tanh`,`sin`,`cos`,`erf`,`log2`,`pow2`,`sigshft`) | EUP VALU3 push (Alu3 op0 + 5-bit selector) + PopEupResult; each bare + `_macro` push+pop pair | C |
| 23 | alloca / allocate (`alloca*`, `allocate*`) | SparseCore allocator (smem/spmem/vmem/sflag/hbm/iova/timem/tilespmem/dreg/cbreg + `_dyn` + `_any`); cf ScDialect Alloca | C |
| 21 | control register rd/set (`rdreg_*`, `setreg_*`) | scalar RdReg/SetReg → SCS scalar slot; cycle counters, tid/scid/tag id regs, fsr/ddr/dmacrdt/sflagrange | C |
| 19 | pointer / addressing / loop-bc (`inttoptr`,`ptrtoint`,`bc_*`,…) | LLVM inttoptr/ptrtoint/addrspace + loop bytecode (`bc_loop_*`, `bc_load/store_aliaddr`, `bc_select_predicate`, `make_restrict_ptr`) | C |
| 16 | addrspacecast (`addrspacecast*`) | LLVM addrspacecast → SparseCore address-space ID (base 201; AddressSpaceDescription @ `0x135462c0`) | C |
| 14 | scalar ALU / scalar mem (`shll`,`shra`,`sadd_ov`,`addcarry`,…) | SCS scalar slot: shll/shra/shrl/sshllo shifts, sadd_ov/ssub_ov/sshla_ov overflow-add, addcarry, add_{high,low}_f32 | C |
| 14 | lane / sublane shuffle / permute (`vrot_sublane`,`vperm`,`sc_permute`) | VPU cross-lane slot → `llo.vrot.slane`/`llo.vperm.sublane`/`llo.vslaneseq`/`vshift_insert`/`sc_mask_permute` | C |
| 12 | CBREG / circular-buffer (`rdcbreg`,`wrcbreg`,`cbreg_add`,…) | scalar CBREG ops: rd/wr/add/copy CBREG {base,offset,size}; 16 CBREGs/bank, dual smem/tilespmem base, allocate_cbreg | C |
| 12 | trace / telemetry / sc-control (`sc_*`,`strace`,`event`,`read_*cycle`) | SparseCore control/trace: strace/event/spill_debug/log/mprefix, read_{global,local}_cycle_count, ssetpstate/ssettm | C |
| 11 | sort / unique / dupcount (`sort*`,`uniquef`,`dupcntf`,`vmpcnt`) | SparseCore sort/unique unit: sort_{asc,dsc}d{f,i}, uniquef/i, dupcntf/i, vmctz/vmpcnt_ones (embedding dedup) | I |
| 10 | task / control / structural (`task_*`,`loop_*`,`barrier`,`nop`,…) | SparseCore tile-task + structural: task_dispatch, loop_{name,parallel}, barrier, nop, delay, clear_ibuf, halt_trap, tileid | C |
| 5 | i1-mask width conversion (`*i1_to_*i1`) | VPU mask slot: `tpu_{8,16,32}i1_to_{8,16,32}i1` — vector-mask width re-pack | C |

```text
834+87+74+47+40+32+28+26+24+23+21+19+16+14+14+12+12+11+10+5  =  1356
```

> **QUIRK —** the seven largest classes (stream, pack/unpack, vld/vst, wait, dma) are 1082 of 1356 — 80 % of the surface is SparseCore *data movement*, not compute. The transcendental/EUP class is 24 ops and the scalar-ALU class 14; SparseCore is a gather/scatter/sflag machine that does very little arithmetic of its own. A reimplementer budgeting effort should expect the stream-engine descriptor to dominate the work, not the math.

---

## The Stream Family (834 ops)

### Purpose

The `tpu_stream_*` family is 62 % of the intrinsic surface and is the single most important SparseCore primitive: the embedding-table gather/scatter engine. Its 834-way explosion is not 834 unrelated ops — it is a four-axis cross-product where each cell is a distinct registered intrinsic so the per-`(pattern,verb,dtype,memspace)` stream-engine command is selected by *op identity at the IR level*, not by an attribute decoded at runtime.

### The cross-product axes

```text
llvm.tpu.stream.<pattern>.[vreg.]{gather|scatter}.[cb.][add.]<dtype>.<src>.to.<dst>

  axis            values                                          how it splits the 834
  --------------------------------------------------------------------------------------
  pattern         linear (180) · strided (114) · indirect (540)   address-generation mode
  verb            vreg (360) · gather (246) · scatter (228)        direction / lane-spread
  dtype           bf16 · e4m3 · e5m2 · f32 · s16 · s32 (6)         ~111 ops each (6×111=666 dtyped)
  transfer        _to_tilespmem 399 · _to_spmem 210 · _to_smem 27  src→dst memspace pair
                  · _to_hbm4b 15 · _to_hbm 15
  modifier        _cb_ (CBREG-windowed, 556) · _add (scatter-add) · _np (no-predicate)
```

The `indirect` pattern dominates (540) because that is the embedding-lookup mode: an index stream selects rows. The `_cb_` (CBREG-windowed) modifier appears on 556 of the 834 — the windowed forms use the `INDIRECT_OFFSET_SOURCE_CBREG` source for embedding-table windowing (see [CBREG](../sparsecore/cbreg.md)). `gather` pulls rows *into* tile-local SPMEM (hence `_to_tilespmem` is the largest transfer bucket); `scatter`/`scatter-add` pushes accumulated gradients back out.

### The pipeline this encodes

```text
        HBM embedding table                         tile-local SPMEM
        ┌───────────────┐    indirect gather        ┌──────────────┐
        │ row 0         │ ── (CBREG-windowed) ─────▶ │ gathered rows│
        │ row 1         │    indexed by offset       │              │
        │ …             │    stream                  └──────┬───────┘
        └───────────────┘                                   │ compute (forward)
                ▲                                            ▼
                │  scatter-add (_add)                 ┌──────────────┐
                └──────── accumulate gradients ────── │ updated rows │
                                                      └──────────────┘
```

### Representative signature

```c
// tpu_stream_linear_gather_add_f32_hbm_to_tilespmem
//   ::create(OpBuilder&, Location, Value, Value, Value, Value, Value, Value)   // 6 operands
// tpu_stream_indirect_gather_add_bf16_hbm_to_tilespmem
//   ::create(OpBuilder&, Location, Value ×8)                                    // 8 operands
//
// The operand count tracks the pattern: linear/strided forms carry 6 SSA operands
// (src/dst handle + base/offset/size + sflag); the indirect forms carry 8
// (the extra two are the index/offset-stream + its CBREG window).
```

> **NOTE —** the per-leaf intrinsic→numeric-stream-engine-command for all 834 is **INFERRED** at the class→engine level only. The class maps to the SparseCore stream engine and the descriptor operand shape is recovered byte-exactly; the per-`(pattern,verb,dtype,memspace)` HW command *value* is the SparseCore stream ISA — sibling to the DMA descriptor — and is not individually byte-dumped for each of the 834. See [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md).

---

## ODS Shape Recovery

### Purpose

The operand/result declaration of each intrinsic is recoverable from the binary without TableGen source, the same way [tpu → LLO ODS Lowering](tpu-to-llo-ods.md#ods-signature-model) recovers LLO signatures: the typed `create` symbol's demangled argument list *is* the ODS declaration in source order.

### The recovery rule

```c
// create(OpBuilder&, Location, <ODS args in declaration order>)
//   mlir::Type   -> 1 explicit result type (inference-free op)
//   mlir::Value  -> 1 SSA operand
// Cross-checked against the op's NOperands<N> trait, which pins the operand count
// independently of the create arg list (e.g. tpu_syncadd carries NOperands<2>).
```

466 of the 1356 carry a typed `create`; the other 890 use the generic `(TypeRange, ValueRange, ArrayRef<NamedAttribute>)` default builder (result type inferred via `SameOperandsAndResultType`/`InferType`, operand count by name-family arity). The typed forms verified against the binary:

| intrinsic (class) | `create` args (after Location) | operands | Conf |
|---|---|---|---|
| `tpu_addrspacecast` (addrspacecast) | `Type, Value` | 1 res, 1 opnd | CERTAIN |
| `tpu_addrspacecast_smem` / `_spmem` / `_*_tec` | `Type, Value, Value` | + tile-window/base | CERTAIN |
| `tpu_dma_*_sc_simple` (DMA) | `Value ×8` | 8-field descriptor | CERTAIN |
| `tpu_dma_*_sc_single_strided` (DMA) | `Value ×10` | + stride | CERTAIN |
| `tpu_dma_*_sc_general` (DMA) | `Value ×16` | multi-dim | CERTAIN |
| `tpu_stream_linear_*` (stream) | `Value ×6` | stream descriptor | HIGH |
| `tpu_stream_indirect_*` (stream) | `Value ×8` | + index/offset stream | HIGH |
| `tpu_syncadd` (sync) | `Value, Value` | sflag, delta | CERTAIN |
| `tpu_fetch_and_add` (sync) | `Value, Value, Value` | sflag, addr, value | CERTAIN |
| `tpu_*_macro` (EUP, e.g. `tpu_sin_macro`) | `Type, Value` | 1 res, 1 opnd | HIGH |
| `tpu_rdcbreg_offset` / `_size` (CBREG) | `Type, Value` | result, cbreg | HIGH |
| `tpu_wrcbreg_offset` (CBREG) | `Type, Value, Value` | cbreg, value | HIGH |
| `tpu_inttoptr` / `tpu_ptrtoint` (ptr) | `Type, Value` | result, val | HIGH |

> **CORRECTION (LLVMTPU-1) —** the DMA `_single_strided` tier is **10** SSA operands, not 11. The demangled `tpu_dma_hbm_to_hbm_sc_single_strided::create` arg list is `Value` + 9×`S5_` (a back-reference to the prior `Value` type), i.e. ten `Value` operands, between the `_simple` 8 and the `_general` 16. The stride tier adds two operands over `_simple`, not three.

> **CORRECTION (LLVMTPU-2) —** the stream-family typed `create` is **not uniformly 6 operands**. The `linear`/`strided` patterns carry 6, but the `indirect` patterns carry **8** (the two extra operands are the index/offset stream and its CBREG window). The "V×6" figure is the linear representative, not the family-wide arity.

---

## ID-Table Organisation

### Purpose

A reimplementer must reproduce *how* 1356 ops register into one dialect. The answer is the 10-batch registrar — a TableGen pattern that splits one logical `addOperations<…>` into ten functions to bound per-function instantiation size. This is the page's "ID-table" structure: the intrinsic IDs are assigned in registration order across the ten batches.

### The registrar

```text
LlvmTpuDialect::initialize
  └─ registerLlvmTpuDialectOperations   @0x146d0560   ── tail-calls 10 sub-registrars:
       ├─ registerLlvmTpuDialectOperations0   @0x146d05c0
       ├─ registerLlvmTpuDialectOperations1   @0x1472bea0
       ├─ registerLlvmTpuDialectOperations2   @0x1478b500
       ├─ registerLlvmTpuDialectOperations3   @0x147e1c40
       ├─ registerLlvmTpuDialectOperations4   @0x14835b80
       ├─ registerLlvmTpuDialectOperations5   @0x148891c0
       ├─ registerLlvmTpuDialectOperations6   @0x148dc3c0
       ├─ registerLlvmTpuDialectOperations7   @0x1492d5c0
       ├─ registerLlvmTpuDialectOperations8   @0x14982d00
       └─ registerLlvmTpuDialectOperations9   (last batch)
                                                ── 10 × ~135 ops  =  1356
```

Each batch is a variadic `addOperations<tpu_X, tpu_Y, …>` that materialises one `RegisteredOperationName::Model<Op>` per template argument — the standard MLIR op-registration ABI, just split across ten functions. There is no separate numeric "intrinsic ID enum" table in this dialect: the op's identity *is* its `TypeID`/`RegisteredOperationName`, and the printed `llvm.tpu.*` name is the ID the LLVM translator emits. The Model vtables span a contiguous `.data.rel.ro` region (`tpu_16i1_to_32i1` … `tpu_wrcbreg_tilespmem_base`).

> **QUIRK —** the ≤256-op batch split is a TableGen artifact, not a semantic grouping. Batch *N* does not correspond to functional class *N*; ops from the same family (e.g. the 834 streams) are scattered across all ten batches in alphabetical-class order. A reimplementer must not assume batch membership carries meaning beyond "register these ~135 ops here."

### Registration binding

```c
function registerLlvmTpuDialectOperations(LlvmTpuDialect *d):   // 0x146d0560
    registerLlvmTpuDialectOperations0(d);                       // each batch:
    …                                                           //   addOperations<tpu_A, tpu_B, …>(d)
    return registerLlvmTpuDialectOperations9(d);                //   -> RegisteredOperationName::insert ×135
```

The dialect class is `mlir::sparse_core::LlvmTpuDialect` (ctor `LlvmTpuDialectC1EPNS_11MLIRContextE`). Its companion helpers — `AddressSpaceDescription(int)` @ `0x135462c0`, `GetAnyTypeFromAddressSpace(int)`, the attr parse/print pair, and the target-CPU attribute predicates — are the dialect-level machinery the intrinsics rely on for memory-space and target resolution.

---

## Bridging into the LLVM Backend

### Purpose

The intrinsics do not stop at MLIR. The `LowerToSparseCoreLlvmPass` rewrites each `tpu_*` op into its LLVM-dialect form so the SparseCore LLVM backend can code-generate it. This section names the bridge and points to the pages that own each lowering arm.

### The consuming pass

```text
xla::tpu::sparse_core::CreateLowerToSparseCoreLlvmPass     @0x135667c0  ── factory
  └─ LowerToSparseCoreLlvmPass::runOnOperation             @0x13566d00  ── driver
       └─ lowerFunc                                        @0x13568280  ── per-op rewrite
```

Per class, the lowering arm differs:

| class | lowers to | owned by |
|---|---|---|
| addrspacecast | LLVM `addrspacecast` / `INTRINSIC_WO_CHAIN` keyed by intrinsic ID | [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) |
| transcendental / EUP | EUP VALU3 push (`Alu3` op0 + 5-bit selector) + `PopEupResult` | [EUP Transcendental Slot](../isa/slot-eup-transcendental.md) |
| CBREG | scalar CBREG ops (ReadCbreg 0x36 / WriteCbreg 0x35 / AddCbreg 0x33) | [CBREG](../sparsecore/cbreg.md) |
| sparsecore-stream | per-(pattern,verb,dtype,memspace) stream-engine descriptor | [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md) |
| sync / wait | sflag VSync/VWait LLO ops | [VPU Slot](../isa/slot-vpu.md) |
| pack/unpack, vcvt, vld/vst, permute | VPU slot ops (`llo.v*`) | [VPU Slot](../isa/slot-vpu.md) |
| scan / sort / unique | SparseCore scan/sort/dedup units | [Scan Datapath](../sparsecore/scan-datapath.md), [Rank & Permute / Radixsort](../sparsecore/rank-and-permute-radixsort.md) |

> **NOTE —** the addrspacecast arm is *not* an `ISD::ADDRSPACECAST(0xf4)` lowering. The SC cast intrinsics survive into LLVM-IR as intrinsic *calls* (`INTRINSIC_WO_CHAIN`), distinct from the TensorCore front-end's real `addrspacecast` *instructions*. See [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) for the full per-cast from→to map and why wiring the SC casts into `LowerADDRSPACECAST` produces a backend that traps with `CannotYetSelect`.

---

## What Is Not Recovered

Honest gaps, for the reimplementer:

- **Per-leaf stream-engine command (834 ops).** Class→engine confirmed and the descriptor operand shape recovered; the numeric per-`(pattern,verb,dtype,memspace)` HW command value is not individually byte-dumped. This is the SparseCore stream ISA, sibling to the DMA descriptor.
- **Per-intrinsic LLVM `IntrProperties` bitset.** Which `OpInterface`s each op carries is observable from the interface-Model symbols (AliasAnalysis 546, MemoryEffect 285, Bytecode 188, AccessGroup 180); the exact per-op `IntrNoMem`/`IntrArgMemOnly`/`IntrWillReturn` bitset that drives backend scheduling/aliasing is not transcribed.
- **The 890 default-builder ops' exact arity + result `TypeConstraint`.** Recovered by name family and the `NOperands<N>` trait where present; the per-op `verifyInvariantsImpl` byte-decode (1-vs-2 operands, Vreg/Mask/Scalar/Ptr result predicate) is not exhaustively walked.
- **The complete numeric address-space ID table.** The `AddressSpaceDescription` switch base (201) and sampled case strings (`HBMAny`, `SflagAny`, `SflagTile`, `TileSmem`, plus Smem/Sflag/HBM/Dreg) are decoded; the full ID↔space map and the 16 addrspacecast from→to ID pairs are completed on [addrspacecast ISel](../sparsecore/addrspacecast-isel.md).
- **The scan/sort/unique-engine opcode encodings (32+11 ops).** Mapped to the SparseCore scan/sort/dedup units by name; the per-op HW command bit layout is not decoded (these are SparseCore-specific compute units, not TensorCore LLO slots).

---

## Cross-References

- [tpu → LLO ODS Lowering](tpu-to-llo-ods.md) — the *other* surface: the 322-op `tpu` dialect → LLO; the ODS-from-`create` recovery rule this page reuses
- [The TPU Compiler](overview.md) — Part V orientation; where the SparseCore lowering sits in the five-phase descent
- [Lower to SparseCore LLVM](lower-to-sparsecore-llvm.md) — the consuming `LowerToSparseCoreLlvmPass` that turns these intrinsics into LLVM-IR
- [Dot/Conv MXU Lowering](dot-conv-mxu-lowering.md) — the TensorCore matmul lowering, contrast to the SparseCore stream engine
- [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) — the 16 addrspacecast intrinsics at instruction selection; full from→to address-space map
- [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md) — the 834-op stream family's engine-side detail
- [CBREG](../sparsecore/cbreg.md) — the 16-CBREG-per-bank circular buffer the CBREG and `_cb_` stream intrinsics drive
- [Scan Datapath](../sparsecore/scan-datapath.md) — the scan/segment-scan engine the 32 scan intrinsics target
- [Rank & Permute / Radixsort](../sparsecore/rank-and-permute-radixsort.md) — the sort/unique/dedup unit the 11 sort intrinsics target
- [EUP Transcendental Slot](../isa/slot-eup-transcendental.md) — the EUP VALU3 push/pop the 24 transcendental intrinsics lower to
- [VPU Slot](../isa/slot-vpu.md) — the VPU slot ops the pack/unpack, convert, load/store, permute, and sync/wait intrinsics target
- [MXU Slot](../isa/slot-mxu.md) — the MXU bundle slot the pack/unpack staging feeds
