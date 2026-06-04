# ConvolutionCostState

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. All addresses are VMA.*

## Abstract

`ConvCostState` is the **running state the cost model builds once per convolution** before it prices the op. It is the conv-shaped analogue of an LLVM `MachineFunction`'s analysis cache: extract the conv-like HLO once, decode its `ConvolutionDimensionNumbers`, kernel `Shape`, and `Window`, map each logical dimension to its physical (layout-mapped) position, run the `Target`'s per-axis chunk grid over the three operand shapes, and stash the result so the per-kernel pricing leaf can read it directly. The struct is built by `CostModel::GetConvolutionCostState` `@0x130a4b20` and is the `const ConvCostState&` argument carried through the whole conv-cost emitter chain.

There are in fact **two** distinct structs, and conflating them is the central trap. `ConvCostState` is the large persistent state (1180+ bytes — protos at `+0x158`/`+0x1e8`/`+0x328`, six layout-mapped dim sizes at `+0x350..+0x378`, a set of per-dim vector masks at `+0x380..+0x458`, the 21-byte `ConvolutionLoweringStrategy` dtype/pack flags at `+0x470..+0x484`, and three `inlined_vector<long,6>` chunk-count grids at `+0x8`/`+0x78`/`+0xe8`). `ConvState` is the small per-call dim-product struct (`+0x0..+0x78`) that `CostModel::RecordConvolutionCycles` `@0x130b6ce0` builds **inside** the deposit, on the stack, from `ConvCostState`'s chunk-count vectors divided by the `Target` geometry. The pricing leaf `CostModel::RecordConvKernelCycles` `@0x130caf20` reads dtype/pack flags from `ConvCostState` and dim products from `ConvState`, multiplies them into an op count, and deposits cycles into the `Matmul` / `Matpush` / `Xlu` resource slots.

This page documents the field offsets of both structs and the **throughput bridge** that turns the conv's op count into cycles. The bridge is the central detail: the matmul-rate multiplier the conv deposit needs is `VfCycleTable::GetCyclesForThroughput(CT 0)`, which is *not* a constant — it is a call into `MxuLatencyTable::GetResourceUsage(instr 212, res 3)`, and the `res` argument is a **resource-index remap** (res 3 → array index 15 = `MatmulAccA`; res 11 → array index 0 = `MatpushPushPort`), not a cycle seed. So the `VfCycleTable` throughput integer and the `MxuLatencyTable` reservation cycle of [`mxu-latency-overview`](mxu-latency-overview.md) are *the same number viewed two ways*.

For reimplementation, the contract is:

- The `ConvCostState` field map: the three copied protos, the six logical→physical dim sizes, the per-dim vector masks, the `ConvolutionLoweringStrategy` flag bytes (read only for opcode `0x2b`=convolution), and the three chunk-count vectors.
- The `ConvState` field map: the dim products at `+0x8..+0x78`, the four shape-case flag bytes at `+0x0..+0x6`, and the `Product(chunk_vector)/SublaneCount/ChunksPerTile` formula that fills each.
- The `Matmul`/`Matpush`/`Xlu` deposit: op count × `thru(CT)` × format-rate (`0.5`/`0.25`) ÷ `Target+0x4ac` (matmul rate) or `Target+0x4b0` (Xlu rate) ÷ `ElementPackingFactor`.
- The `VfCycleTable → MxuLatencyTable` bridge and its `res→index` remap.
- That `Target+0x4ac`/`+0x4b0` are **`Target` vector-ISA rate fields**, not `ConvCostState`-local derate fields.

| | |
|---|---|
| **Structs** | `xla::jellyfish::CostModel::ConvCostState` (persistent) · `xla::jellyfish::CostModel::ConvState` (per-call) |
| **Builder** | `CostModel::GetConvolutionCostState` `@0x130a4b20` (3520 B) |
| **ConvState build** | `CostModel::RecordConvolutionCycles` (4-RV) `@0x130b6ce0` (built at `rbp-0x1d0`) |
| **Pricing leaf** | `CostModel::RecordConvKernelCycles` `@0x130caf20` |
| **Throughput bridge** | `VfCycleTable::GetCyclesForThroughput` `@0x1c89e2c0` → `MxuLatencyTable::GetResourceUsage` `@0x1c8ae5c0` |
| **`thru(CT 0)`** | `GetResourceUsage(instr 212, res 3, false)` = `array[15]` = `MatmulAccA` = 8 (bf16) |
| **`thru(CT matpush)`** | `GetResourceUsage(instr 267, res 11, false)` = `array[0]` = `MatpushPushPort` = 2 (bf16) |
| **Geometry divisors** | `Target::SublaneCount()`, `LaneCount()`, `ChunksPerTile()` |
| **Rate divisors** | `Target+0x4ac` (matmul), `Target+0x4b0` (Xlu) — vector-ISA fields |

---

## ConvCostState — The Persistent State

### Purpose

`GetConvolutionCostState` `@0x130a4b20` has the signature `bool GetConvolutionCostState(const HloInstruction* inst, ConvCostState* out)`. It extracts the conv-like HLO (`fusion_util::ExtractConvLikeHlo` `@0x1d6aa140`; a null result is a `LogMessageFatal` on `conv_like_hlo != nullptr` at `cost_model.cc:3374`), then fills the struct top-to-bottom: copy the three shape/window protos, run `ChunkCountsWithTmp` over the three operand shapes, map each logical conv dimension to its physical position via `LayoutUtil::MakeLogicalToPhysical`, initialise the per-dim vector masks, classify each dim (batch / outer / inner) into the three mask vectors, and finally — only for a real convolution opcode — copy the `ConvolutionLoweringStrategy` flag block.

### Field Map

`a3` is the `ConvCostState*`. Offsets are byte offsets from the struct base; the decompiler renders them as `(char*)a3 + N` (proto copies) or `*((_QWORD*)a3 + N/8)` (scalar/pointer stores).

| Offset | Content | Store site / source |
|---|---|---|
| `+0x0` | `const HloInstruction* conv` | `*(_QWORD*)a3 = ExtractConvLikeHlo(inst)` `@0x130a4b4f` |
| `+0x8` | `inlined_vector<long,6>` — **operand-shape** chunk counts | `ChunkCountsWithTmp(operand_shape)` → `Assign(a3+1)` `@0x130a4c25` |
| `+0x78` | `inlined_vector<long,6>` — **output-shape** chunk counts | `ChunkCountsWithTmp(output_shape)` → `Assign(a3+15)` `@0x130a4c38` |
| `+0xe8` | `inlined_vector<long,6>` — **kernel-shape** chunk counts | `ChunkCountsWithTmp(kernel_shape)` → `Assign(a3+29)` `@0x130a4c58` |
| `+0x158` | `ConvolutionDimensionNumbers` | `CopyFrom((char*)a3 + 344, …)` `@0x130a4b79` |
| `+0x1e8` | `Shape` (operand/kernel shape) | `Shape::operator=((char*)a3 + 488, …)` `@0x130a4b90` |
| `+0x328` | `Window` | `Window::CopyFrom((char*)a3 + 808, …)` `@0x130a4baa` |
| `+0x350` | output-feature dim SIZE (physical) | logical→physical `[+106]` `@0x130a51f7` |
| `+0x358` | input-feature dim SIZE (physical) | logical→physical `[+107]` `@0x130a5212` |
| `+0x360` | output-spatial dim SIZE (physical) | logical→physical `[+108]` `@0x130a5235` |
| `+0x368` | input-spatial dim SIZE (physical) | logical→physical `[+109]` `@0x130a5250` |
| `+0x370` | kernel-feature dim SIZE (physical) | logical→physical `[+110]` `@0x130a5279` |
| `+0x378` | kernel-spatial dim SIZE (physical) | logical→physical `[+111]` `@0x130a529b` |
| `+0x380` | `vector<bool>` — per-dim mask A (batch-dim) | `assign(N, 0)` `@0x130a52bb` |
| `+0x398` | `vector<bool>` — per-dim mask B (outer-dim) | `assign(N, 0)` `@0x130a52dd` |
| `+0x3b0` | `vector<bool>` — per-dim mask C (inner-dim) | `assign(N, 0)` `@0x130a52ff` |
| `+0x3c8` / `+0x3e0` / `+0x3f8` | `vector<long>` (init = 1) | `assign(N, 1)` `@0x130a531e/533d/535c` |
| `+0x410` | `vector<bool>` | `assign(N, 0)` `@0x130a537e` |
| `+0x428` | `vector<long>` (init = 1) | `assign(N, 1)` `@0x130a539d` |
| `+0x440` / `+0x458` | `vector<long>` (init = 0) | `assign(N, 0)` `@0x130a53bc/53e2` |
| `+0x470..+0x484` | `ConvolutionLoweringStrategy` (21 B) | `GetConvolutionLoweringStrategy`, copied **only when opcode == 0x2b** `@0x130a5705..577a` |

> **NOTE —** The three chunk-count `inlined_vector<long,6>` members sit at `+0x8`/`+0x78`/`+0xe8` on a 0x70 stride (112 B per member, including its header): the three `Storage<long,6>::Assign` calls target `(__int64*)a3 + 1`, `+15`, `+29` immediately after the three `ChunkCountsWithTmp` calls. The first vector is fed from the **operand** shape (`v128`), the second from the output shape, the third from the kernel shape — easy to mis-bind if the operand vector is overlooked.

### The ConvolutionLoweringStrategy Flag Block

The `+0x470..+0x484` region is the 21-byte `ConvolutionLoweringStrategy` of [`mxu-latency-overview`](mxu-latency-overview.md)'s sibling. It is copied as one `vmovups [r15+0x470]` of the 16-byte head plus a `qword` tail at `+0x47d`, **only** when the conv-like HLO's opcode byte (`[conv+12]`) is `0x2b` (convolution). `RecordConvKernelCycles` reads it byte-by-byte as the dtype / packing decision. The byte offsets within the struct (confirmed by the byte reads in `@0x130caf20`):

| CCS offset | Decimal | Flag (inferred name) | Read in `RecordConvKernelCycles` |
|---|---|---|---|
| `+0x470` | 1136 | `perform_batch_packed_activation_reorg` | `== 1` gate `@0x130cb040` |
| `+0x476` | 1142 | `perform_activation_if_bf16_packing` | `!= 0` / `== 1` gate `@0x130cb1b8/cb2bb` |
| `+0x477` | 1143 | `generate_combined_x8_packed_matmuls_and_latches` | OR-combine `@0x130caf38` |
| `+0x478` | 1144 | `generate_combined_x4_packed_matmuls_and_latches` | OR-combine `@0x130caf38` |
| `+0x479` | 1145 | `generate_post_xpose_x8_packed_matmuls` | OR-combine `@0x130caf38` |
| `+0x47a` | 1146 | `generate_bf16_packed_vmatmuls` | `^1` index-select `@0x130cb37e` |
| `+0x47e` | 1150 | `perform_activation_bf16_packing` | OR-combine `@0x130caf38` |
| `+0x480` | 1152 | `generate_x8_packed_vlatches` | `== 1` (0.25 quarter-rate gate) `@0x130cb468` |
| `+0x481` | 1153 | `generate_x8_packed_vlatches_for_activations` | OR-combine `@0x130caf38` |
| `+0x482` | 1154 | `generate_x8_packed_vmatmuls` | OR-combine `@0x130caf38` |

These bytes OR-combine (`@0x130caf38..af86`) to decide whether `TransferSizeUtil::ElementPackingFactor` `@0x1d6b03e0` is consulted (called only if any flag is set) versus the `rbx = 1 / 2` default. The flag *names* are inferred from the `ConvolutionLoweringStrategy` proto field order and the gate semantics; the *offsets* and read sites are byte-confirmed.

---

## ConvState — The Per-Call Dim-Product Struct

### Purpose

`ConvState` is built on the stack (at `rbp-0x1d0`) inside `RecordConvolutionCycles` (4-RV) `@0x130b6ce0` and passed by `const&` to `RecordConvKernelCycles`. It is **not** persisted. Each dim field is a `Product(chunk_dim_sizes)` over one of `ConvCostState`'s chunk-count vectors, divided by a `Target` geometry constant (`SublaneCount()` = 8, `LaneCount()` = 128, `ChunksPerTile()` = 16), with paired remainder / divisibility flags. The three `xla::Product` calls (`@0x130b6f1d/6f43/6f69`) reduce the operand / kernel / output chunk vectors; the four leading bytes are the conv-shape-case flags the deposit branches on.

### Field Map

`a3` (in `RecordConvKernelCycles`) is the `ConvState*`. The decompiler renders fields as `a3[k]` (8-byte stride) or `a3[byte]`.

| Offset | `a3[k]` | Content (inferred role) |
|---|---|---|
| `+0x0` | `a3[0]` byte | shape-case flag ("input == kernel" class) |
| `+0x2` | `a3[2]` byte | shape-case flag ("dim ≥ 2" class) |
| `+0x3` | `a3[3]` byte | feature-dim selector (used as `[+0x3]^1` index pick) |
| `+0x5` | `a3[5]` byte | `HasNonBatchDotBaseDilation` result `@0x130b79fc` |
| `+0x6` | `a3[6]` byte | grouped-conv gate (`== 1`) |
| `+0x8` | `a3[1]` | spatial / output dim product |
| `+0x10` | `a3[2]` | spatial / output dim product (minor gate) |
| `+0x18` | `a3[3]` | spatial dim product |
| `+0x20` | `a3[4]` | output dim product |
| `+0x40` | `a3[8]` | feature-group / kernel-feature product (read in `ShouldPackInputFeature`) |
| `+0x48` | `a3[9]` | window-volume / spatial product |
| `+0x50` | `a3[10]` | the M dim |
| `+0x60` | `a3[12]` | feature dim product (`[+0x3]^1`-indexed pair with `+0x68`) |
| `+0x68` | `a3[13]` | feature dim product (sibling of `+0x60`) |
| `+0x70` | `a3[14]` | output-spatial iteration count |
| `+0x78` | `a3[15]` | window-iteration count |

The pair `(+0x60, +0x68)` plus the `[+0x3]^1` XOR-select (`@0x130cafc3`) is the "which-feature-dim-first" choice: the deposit reads `a3[16*a3[3] + 104]` (=`[+0x68]`-region) and `a3[16*(a3[3]^1) + 96]` (=`[+0x60]`-region). The op count is
`a3[3] · a3[2] · a3[4] · a3[1]` (the `[+0x18]·[+0x10]·[+0x20]·[+0x8]` block) `× [+0x50] × feat0 × feat1`, then a branch on the flag bytes `[+0x0]`, `[+0x2]`, `[+0x3]` folds in `[+0x48]` / `[+0x70]` / `[+0x78]` (the four conv-shape cases at `@0x130cb011/0c0/0ee/113`), finally divided by `ElementPackingFactor` and `ChunksPerTile`.

> **NOTE —** the *semantic* role of each `ConvState` dim offset (which is output-feature vs input-feature vs spatial vs batch vs kernel-window) follows the `imul` chain and the `ChunkCountsWithTmp` source order; this build carries no DWARF, so the dimension *names* are an interpretation while the offsets and the formula shape are taken straight from the disassembly. The authoritative source is `GetConvIterationCounts` `@0x130b3c80`.

---

## The Throughput Bridge — VfCycleTable → MxuLatencyTable

### Why a Bridge Exists

`RecordConvKernelCycles` needs a *throughput* (cycles-per-op) for the matmul and the matpush, not the per-op *latency* of the base `Performance` grid. That throughput is the per-resource occupancy from [`mxu-latency-overview`](mxu-latency-overview.md). `VfCycleTable::GetCyclesForThroughput(CycleTable::Instruction)` `@0x1c89e2c0` is the adapter: it maps each 32-entry `CycleTable::Instruction` (CT) ordinal to the right call into the MXU occupancy table. The conv deposit reaches it through the `Target`'s `CycleTable` vtable — the `(*(…+16LL))(this, CT, …)` indirect calls in `@0x130caf20` (e.g. `CT 0` at `@0x130cb...`, `CT 1` at `@0x130cb375`).

### The CT → (instr, res, transpose) Table

The decompile of `@0x1c89e2c0` (a `switch(a2)` over CT, `default → return 1`) confirms three call shapes: matmul/matpush CTs call `MxuLatencyTable::GetResourceUsage`; mxres/Xlu CTs call `ViperfishPerformance::GetResourceUsage(…, res 14) + 1`; CT 10/16 are a `LogMessageFatal("Unsupported PushGainsS4.")` at `cycle_table.cc:682`.

| CT | Call | Role |
|---:|---|---|
| 0 | `MxuLat.GetResourceUsage(212 matmul', res 3, false)` | **the conv matmul-rate** `thru(CT 0)` |
| 1 | `MxuLat.GetResourceUsage(218 matmul'', res 3, false)` | matmul-rate (alt) |
| 4 | `MxuLat.GetResourceUsage(230 matmul, res 3, false)` | matmul-rate (base) |
| 5 | `MxuLat.GetResourceUsage(267 matpush, res 11, false)` | matpush-rate |
| 6 | `MxuLat.GetResourceUsage(271 matpush', res 11, false)` | matpush'-rate |
| 9 | `MxuLat.GetResourceUsage(277 matpush'', res 11, false)` | matpush''-rate |
| 10, 16 | `LogMessageFatal("Unsupported PushGainsS4.")` | abort |
| 11 | `MxuLat.GetResourceUsage(267, res 11, **true**)` | matpush-rate (transposed) |
| 12 | `MxuLat.GetResourceUsage(271, res 11, **true**)` | matpush'-rate (transposed) |
| 15 | `MxuLat.GetResourceUsage(277, res 11, **true**)` | matpush''-rate (transposed) |
| 23 | `VfPerf.GetResourceUsage(296, res 14) + 1` | mxres / Xlu (matres TC) |
| 27 | `VfPerf.GetResourceUsage(302, res 14) + 1` | mxres |
| 28 | `VfPerf.GetResourceUsage(287, res 14) + 1` | **the conv MXRES (Xlu)** |
| 29 | `VfPerf.GetResourceUsage(291, res 14) + 1` | mxres |
| 31 | `VfPerf.GetResourceUsage(298, res 14) + 1` | mxres |
| all others | `return 1` | vector / matprep-bucket default |

The transpose flag is the 4th `GetResourceUsage` arg; the matpush key pre-transforms (`267` direct, `271 → latch_mode ^ 0xB`, `277 → latch_mode | 0x14`) are applied inside `GetResourceUsage` (see [`mxu-latency-overview`](mxu-latency-overview.md)).

### The Throughput Bridge — `res` Is a Resource-Index Remap

`MxuLatencyTable::GetResourceUsage(instr, res, transpose)` `@0x1c8ae5c0` does **not** index its `array<int,19>` reservation vector with `res` directly. It first remaps:

```c
function GetResourceUsage(instr, res, transpose):       // VF @0x1c8ae5c0
    if   res == 3:  idx = 15        // → MatmulAccA
    elif res == 11: idx = 0         //  → MatpushPushPort
    else: return InvalidArgument("Unsupported kind of resource")   // mxu_latency_table_vf.cc:547

    // build the modifier key from the opcode, then find:
    switch (instr):
        case 212: key = MatmulModifier{format=1}; map = this+0x20
        case 218: key = MatmulModifier{format=2}; map = this+0x20
        case 230: key = MatmulModifier{format=6}; map = this+0x20
        case 267: key = MatpushKey(latch_mode);          map = this+0x00
        case 271: key = MatpushKey(latch_mode ^ 0xB);    map = this+0x00
        case 277: key = MatpushKey(latch_mode | 0x14);   map = this+0x00
        default:  LogFatal("Unsupported opcode")          // vf.cc:578

    entry = map.find(key)                 // miss → out_of_range
    array<int,19> v = entry.value         // vmovups: matmul reads [rdx+8], matpush reads [rdx+4]
    CHECK(idx < 0x13)                      // == 19
    return (float) v[idx]                  // the hold-cycle count
```

So the `res` argument 3/11 is a **named-sub-resource selector**, remapped to the concrete array index (`3 → 15 = MatmulAccA`, `11 → 0 = MatpushPushPort`), and *that* indexes the per-modifier reservation array. The matmul CTs (0, 1, 4) therefore read `array[15]`; the matpush CTs (5, 6, 9, 11, 12, 15) read `array[0]`. The `VfCycleTable` throughput integer **is** the `MxuLatencyTable` reservation cycle — the two cost tables are the same numbers.

| `thru(CT 0)` — matmul rate (`array[15]` = `MatmulAccA`, per `MatmulModifier` format) | value |
|---|---:|
| bf16 (format group 1) | 8 |
| format group 2 | 16 |
| int8 / wide (format group 4) | 32 |

| `thru(CT 5)` — matpush rate (`array[0]` = `MatpushPushPort`, per `MatpushModifier`) | value |
|---|---:|
| bf16 narrow | 2 |
| bf16 mid | 4 |
| int8 / x8 wide | 8 |

> **NOTE —** the non-bf16 entries follow the per-format reservation groups; the live `MatmulModifier`/`MatpushModifier` maps per `MatmulDataFormat` give the exact values. The `{2,1,1}` / `{4,3,2}` / `{8,7,6}` reservation triplet's **first** element (the `MatpushPushPort` value) is exactly `thru(CT matpush)`; the other two (matrix-staging-register holds) are read by separate `GetResourceUsage` calls in the MXU scheduler, not by this bridge.

### Per-Generation Bridge

The bridge is per-generation. The Ghostlite helper `GlcCycleTable::GetCyclesForThroughputHelper` `@0x1c89ed20` is structurally identical but uses the Ghostlite instruction set — matmul CTs (0, 1, 4) use instrs `292`/`298`/`310` (`0x124`/`0x12a`/`0x136`); matpush CTs (5, 6, 9, 10) use `347`/`349`/`356`/`358` (`0x15b`/`0x15d`/`0x164`/`0x166`); the GL `MxuLatencyTable` `array<int,11>` remaps `res 4 → idx 3` and `res 9 → idx 9`. `GfcCycleTable::GetCyclesForThroughputHelper` `@0x1c89f400` is the TPU7x variant with the same shape (`array<int,11>`). A reimplementation must read the (instr, res) pair from the per-gen helper.

---

## The Deposit — Putting It Together

`RecordConvKernelCycles` `@0x130caf20` computes one op count from `ConvState` and deposits cycles into three slots (`ResourceVector::Acc(slot, cycles)`):

| Slot | `Acc` index | Deposit | Site |
|---|---:|---|---|
| `Matmul` | `R[1]` | `op_count × thru(CT 1) × 0.5 ÷ Target+0x4ac ÷ ElementPackingFactor` | `@0x130cb...` (Acc `@line 560`) |
| `Matpush` | `R[0]` | `matpush_count × thru(CT matpush)` | Acc `@0x130cb...` (`@line 609`) |
| `Xlu` | `R[2]` | `thru(CT mxres) × ChunksPerTile × remainder ÷ Target+0x4b0` | Acc `@line 259/428` |

The `0.5` (`.rodata 0xa2df5c8`) is the half-rate the matmul multiplies by when the format flag `v166` is clear; the `0.25` (`.rodata 0xa2df6d0`) replaces it only when `ConvCostState+0x480` (`generate_x8_packed_vlatches`) `== 1` **and** `Target+0x398` `== 2` (the topology gate at `@0x130cb468/cb471`). The matmul-rate divisor is read as `vcvtsi2sd xmm1, [rax+0x4ac]` (`Target+0x4ac`); the Xlu divisor as `[rax+0x4b0]` reached through the `CycleTable`'s `Target` pointer.

> **GOTCHA —** `Target+0x4ac` (matmul rate) and `Target+0x4b0` (Xlu rate) are **`Target` fields**, not `ConvCostState` fields. They are chip-wide vector-ISA throughput-rate parameters populated in `Target::Init` from `TpuSequencerParts::vector_isa()` `@0x20b31840`: `vector_isa[+0xc]` (8 bytes) → `Target+0x4ac` & `+0x4b0`; `vector_isa[+0x14]` → `Target+0x4a8`. The conv deposit reads them through `*(_QWORD*)a1` (the `CostModel`'s `Target*`) and through the `CycleTable`'s `Target*`, never out of `ConvCostState` — they are not a conv-local derate.

Because `Matmul`, `Matpush`, and `Xlu` (`R[0]`/`R[1]`/`R[2]`) are in the plain-MAX group of `MaxResourceCycles` (the MXU pipes overlap — see [`resource-enum`](resource-enum.md)), a compute-bound conv's bundle cost is `≈ max(Matmul, Matpush, Xlu)`, not their sum.

---

## Worked Example — bf16 Conv on Viperfish

```text
thru(CT 0)  = VfCycleTable::GetCyclesForThroughput(0)
            = MxuLatencyTable::GetResourceUsage(instr=212, res=3, false)
            = MatmulModifier{format=1 bf16}, array[remap(res=3)=15]
            = array[15] = MatmulAccA = 8

R[1] Matmul  = op_count × 8 × 0.5 ÷ Target[+0x4ac] ÷ ElementPackingFactor[bf16=1]

thru(CT 5)   = GetResourceUsage(instr=267, res=11, false) = array[0] = MatpushPushPort = 2
R[0] Matpush = matpush_tile_count × 2

thru(CT 28)  = VfPerf.GetResourceUsage(287, res=14) + 1            (the Xlu mxres-read rate)
R[2] Xlu     = thru(CT 28) × ChunksPerTile × remainder ÷ Target[+0x4b0]

bundle cost ≈ max(R[0], R[1], R[2])                                (MXU pipes overlap)
```

For an int8-x8 conv the matmul format selects group 4 → `array[15] = 32` (matmul), `array[0] = 8` (matpush) — 4× the bf16 rate, the four-byte-plane x8 latch sequence.

---

## Sibling — Reduce-Window Cost Reuses ConvState

`CostModel::RecordReduceWindowCycles` `@0x130b5ec0` is the pooling sibling. It takes three `Window`s directly (not a `ConvCostState`) and builds the same stack `ConvState` at `rbp-0x1d0` by the same `Product / SublaneCount / ChunksPerTile` division. A trivial path: when `CostModel+0x14` is **false** it deposits zero into `R[7]` `VectorLoad`, `R[3]` `VectorAlu0`, `R[4]` `VectorAlu1`, `R[2]` `Xlu` and returns (`Acc(a9, 7/3/4/2)` `@line 782..788`). Otherwise it dispatches on `GetReduceWindowType` `@0x1454d4a0` (0 → `RecordLaneReduceWindowCycles`, 1 → `RecordSublaneReduceWindowCycles`, 2 → `RecordMajorReduceWindowCycles`), the Lane and Sublane paths gated on `!HasBaseDilation`. The lane leaf (`@0x130c97e0`) deposits `R[7] VectorLoad` (`op_volume = ConvState[+0x20]·[+0x18]·[+0x70]`), an `R[5] VectorAluAny` per-element combine (`thru(CT 22)`), the `UpdateCostBasedOnReductionFunction` combiner cost (count = `op_volume × (ConvState[+0x68] − 1)`), and an `R[2] Xlu` cross-lane drain (`thru(CT 27)` ÷ `Target+0x4b0`). The full pooling cost is on [`reduce-window-pooling-cost`](reduce-window-pooling-cost.md).

---

## Function Map

| Function | Address | Role |
|---|---|---|
| `CostModel::GetConvolutionCostState` | `0x130a4b20` | builds `ConvCostState` (3520 B) |
| `CostModel::RecordConvolutionCycles` (4-RV) | `0x130b6ce0` | builds `ConvState`, calls the kernel leaf |
| `CostModel::RecordConvKernelCycles` | `0x130caf20` | dim → MXU deposit (`R[0]`/`R[1]`/`R[2]`) |
| `VfCycleTable::GetCyclesForThroughput` | `0x1c89e2c0` | CT → MXU-occupancy adapter |
| `viperfish::MxuLatencyTable::GetResourceUsage` | `0x1c8ae5c0` | `res→index` remap + `array[idx]` |
| `GlcCycleTable::GetCyclesForThroughputHelper` | `0x1c89ed20` | Ghostlite bridge (`array<int,11>`) |
| `GfcCycleTable::GetCyclesForThroughputHelper` | `0x1c89f400` | TPU7x bridge (`array<int,11>`) |
| `fusion_util::ExtractConvLikeHlo` | `0x1d6aa140` | conv-like HLO extractor |
| `convolution_util::GetConvolutionLoweringStrategy` | `0x13192820` | the 21-byte flag block |
| `Target::ChunkCountsWithTmp` | `0x10c8b6e0` | per-axis chunk grid → `+0x8`/`+0x78`/`+0xe8` |
| `TransferSizeUtil::ElementPackingFactor` | `0x1d6b03e0` | per-dtype packing divisor |
| `ResourceVector::Acc` | `0x1c89adc0` | the slot deposit |

---

## Related Components

| Name | Relationship |
|---|---|
| `mxu-latency-overview` | the `MxuLatencyTable` model the bridge reads — the reservation array `thru(CT)` returns |
| `vf-cycletable` | the 32-entry `CycleTable::Instruction` dump and the full throughput bridge per gen |
| `window-description-cost` | the conv/DMA byte+throughput primitive feeding `ChunkCountsWithTmp` |
| `reduce-window-pooling-cost` | the pooling sibling reusing the same `ConvState` build |
| `resource-enum` | the 23-slot `ResourceVector` the deposit writes; the `MaxResourceCycles` overlap model |

---

## Cross-References

- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuLatencyTable` reservation model, the `MatmulModifier`/`MatpushModifier` keys, and the `array[idx]` read this bridge surfaces
- [VfCycleTable](vf-cycletable.md) — the 32-entry CT → (instr, res) dump and the full `GetCyclesForThroughput` per-gen bridge
- [WindowDescription Byte-Cost](window-description-cost.md) — the conv/DMA byte + throughput primitive
- [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md) — `RecordReduceWindowCycles`, the pooling sibling that reuses `ConvState`
- [Resource Enum (23-slot)](resource-enum.md) — the `ResourceVector` slots (`Matmul`/`Matpush`/`Xlu`) the conv deposit writes and the `MaxResourceCycles` overlap rule
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instructions (matmul / matpush / matres) the conv deposit prices
