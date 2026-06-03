# SC Backend Pipeline

> *Addresses, symbols, and pass ordering apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`); the binary is shipped with full C++ symbols (not stripped). Other versions differ.*

## Abstract

When XLA lowers a SparseCore offload computation, the dense `tpu`-dialect IR is run through a fixed back-end pass pipeline before it becomes encodable SC bundles. That pipeline is built and driven by `CustomKernelEmitter::RunPasses` (`0x13202780`). This page documents the pipeline as a *contract*: the **exact ordered list of twelve passes**, the per-pass MLIR factory entry point and namespace, how each is attached (top-level `addPass` vs func-nested `nest`), the device-id remap pass that runs second-to-last, and the **MEGACORE barrier** — the even/odd core-pair cross-core synchronization the pipeline's final lowering pass emits whenever the part is a two-core SparseCore megachip.

The single most important structural fact is that `RunPasses` does **not** build one `PassManager` holding twelve passes. It builds and runs **twelve separate single-pass `PassManager`s in sequence**, each carrying exactly one pass, each with its own IR-print gating closure, and each driven independently through `jellyfish::RunPass`. This matters for reimplementation: there is no shared pass-manager analysis cache across the twelve stages, IR dumps are gated per-stage, and the order is hard-wired in the call sequence, not assembled from a registry. The LLVM reader should hold the analogy loosely — this is a hand-rolled equivalent of running twelve `mlir::PassManager::run` calls back-to-back on the same `ModuleOp`, not a single populated pipeline.

The second structural fact is that the MEGACORE barrier is **not a barrier kind**. `EmitScsBarrier` (the SC-side barrier dispatcher reached from the last pass) only dispatches `GLOBAL` and `CUSTOM`; the `MEGACORE` enum value is illegal there (it `RetCheck`s). The megacore even/odd partner sync is an *inner* code path that both the `GLOBAL` and the `CUSTOM` lowerings run whenever `Target::LogicalDevicesPerChip(SparseCore) == 2`. "Megacore" names the **hardware topology** (a two-core megachip), not a barrier the producer selects.

For reimplementation, the pipeline contract is:

- **Twelve single-pass managers, fixed order.** `ConvertIntegerMemrefs → InferMemRefLayout → Canonicalizer → CanonicalizeOperations → CanonicalizeMemorySpace → TilingPropagation → mosaic_sc::InferVectorLayout → mosaic_sc::InsertRelayout → mosaic_sc::ApplyVectorLayout → CSE → LogicalToPhysicalDeviceId → LowerToMlo`. The order is a vector-layout-then-lower sequence; reordering the layout trio (7/8/9) or moving `LogicalToPhysicalDeviceId` after `LowerToMlo` breaks the build.
- **Passes 7/8/9 are `mlir::mosaic_sc`, not `mlir::tpu`.** The SC vector-layout trio (`InferVectorLayout`, `InsertRelayout`, `ApplyVectorLayout`) lives in the `mosaic_sc` namespace. Pass 3 is the *generic* `mlir::createCanonicalizerPass`; passes 4/5 are the `tpu`-specific canonicalizers.
- **The device-id remap must run immediately before `LowerToMlo`.** `LogicalToPhysicalDeviceIdPass` (pass 11) rewrites the `device_id` operand of `tpu::EnqueueDMAOp` / `tpu::SemaphoreSignalOp` from a *logical* id to a *physical* core id (`i32(CoreIndex * stride + TileId)` for SC). It must run before the barrier-lowering pass so the sync ops `LowerToMlo` emits target the correct physical cores.
- **MEGACORE is a topology gate, not a barrier type.** The even/odd partner sync (`partner = local core id XOR 1`) is gated by `LogicalDevicesPerChip(SC) == 2` inside both the `GLOBAL` and `CUSTOM` barrier arms. On a single-core part (`LDPC(SC) == 1`) the partner path is suppressed (it `RetCheck`s) and only the local sync remains.

| | |
|---|---|
| **Pipeline builder** | `xla::tpu::sparse_core::CustomKernelEmitter::RunPasses` @ `0x13202780` |
| **Per-pass driver** | `jellyfish::RunPass` @ `0x14514d60`; `mlir::PassManager` ctor @ `0x1cb700a0`; attach via `OpPassManager::addPass` @ `0x1cb6c000` / `::nest` @ `0x1cb6d3e0` |
| **Pass count / shape** | 12 passes, each in its own single-pass `PassManager` (not one 12-pass pipeline) |
| **Last pass** | `createLowerToMloPass` @ `0x1322adc0` — SC → MLO lowering; collective op → `EmitScsBarrier` |
| **Device-id remap** | pass 11 `createLogicalToPhysicalDeviceIdPass` @ `0x132d5720`; body `runOnOperation` @ `0x132d5ec0`; `GetPhysicalCoreID` @ `0x132d8de0` |
| **SC barrier dispatch** | `CollectiveEmitterBase::EmitScsBarrier` @ `0x13352500` — `GLOBAL(1)`/`CUSTOM(3)` only |
| **Megacore gate** | `Target::LogicalDevicesPerChip(SparseCore) == 2`; partner = `OffloadFactory::ToPartnerGlobalCoreId` @ `0x133e66e0` (`core XOR 1`) |
| **Confidence** | CONFIRMED (byte-anchored, decompile-verified) unless a row or callout says otherwise |

---

## The Twelve-Pass Pipeline

`RunPasses(ModuleOp module, bool, bool, BarrierConfig const* bc)` saves the `BarrierConfig*` (it will be threaded into the last pass), then runs twelve passes in order. Each pass goes through the same four-step idiom:

1. construct a fresh `mlir::PassManager` (ctor `0x1cb700a0`) with a name and a `Nesting`;
2. install the IR-print "should-print" closure (the per-stage VLOG/dump gate);
3. attach the one pass via `OpPassManager::addPass` (top-level, operates on the `ModuleOp`) **or** `OpPassManager::nest<func::FuncOp>(...).addPass` (func-nested, operates per-function);
4. run it via `jellyfish::RunPass(pm, module, ...)`.

Between passes `RunPasses` reads the HLO `BackendConfig` and the `SparseCoreConfig` defaults plus the per-element layout flags (`ShouldEnableLarge2ndMinorLayoutForX16/X8/X4`) to fill in pass options — most visibly the tiling span handed to `InferMemRefLayout` and the `InferVectorLayoutPassOptions` / `ApplyVectorLayoutPassOptions`.

```text
tpu-dialect SparseCore module (post region→sequencer outlining)
   │
   ▼  RunPasses @0x13202780  — TWELVE single-pass PassManagers, in order:
   │
   1  createConvertIntegerMemrefsPass        addPass   int-memref legalization
   2  createInferMemRefLayoutPass            addPass   tile/layout inference (X16/X8/X4 flags)
   3  createCanonicalizerPass                addPass   generic greedy canonicalization
   4  createCanonicalizeOperationsPass       nest      tpu op canonicalization      (per-func)
   5  createCanonicalizeMemorySpacePass      nest      memory-space canonicalization (per-func)
   6  createTilingPropagationPass            nest      propagate {N,2} tiling        (per-func)
   7  mosaic_sc::createInferVectorLayoutPass nest      SC vector layout inference    (per-func)
   8  mosaic_sc::createInsertRelayoutPass    nest      insert layout-conversion ops  (per-func)
   9  mosaic_sc::createApplyVectorLayoutPass nest      apply chosen vector layouts   (per-func)
   10 createCSEPass                          addPass   common-subexpression elim.
   11 createLogicalToPhysicalDeviceIdPass    nest      device_id → physical core id  (per-func)  ── §Device-Id Remap
   12 createLowerToMloPass                   addPass   SC → MLO lowering (LAST)                  ── reaches EmitScsBarrier
   │
   ▼
MLO IR  ─→  SC bundle emission (per-engine SCS/TAC/TEC codecs)
```

### Per-pass table

The factory addresses, namespaces, and attach mode below are read directly from the `RunPasses` call sites and re-verified against the decompiled body (the twelve `create*Pass` calls appear in exactly this order at decompile lines 231 → 1307; passes 7/8/9 are `mosaic_sc`-qualified; the `addPass`/`nest` split matches the disassembly call sites one-for-one).

| # | Factory (VMA) | Namespace | Attach | `mlir::Pass` subclass / role |
|---|---|---|---|---|
| 1 | `createConvertIntegerMemrefsPass` @ `0x132bca60` | `mlir::tpu` | `addPass` | `ConvertIntegerMemrefsPass` — integer-memref legalization |
| 2 | `createInferMemRefLayoutPass` @ `0x132c0f00` | `mlir::tpu` | `addPass` | `InferMemRefLayoutPass` — tile/layout inference; opts = `(int, Span<long const> tiling, TpuTilingFlags)` built from the X16/X8/X4 layout flags |
| 3 | `createCanonicalizerPass` @ `0x1c941920` | `mlir` (generic) | `addPass` | `Canonicalizer` — generic greedy canonicalization |
| 4 | `createCanonicalizeOperationsPass` @ `0x132bb260` | `mlir::tpu` | `nest` | `CanonicalizeOperationsPass` — `tpu` op canonicalization |
| 5 | `createCanonicalizeMemorySpacePass` @ `0x132a2280` | `mlir::tpu` | `nest` | `CanonicalizeMemorySpacePass` — memory-space canonicalization |
| 6 | `createTilingPropagationPass` @ `0x132e0900` | `mlir::tpu` | `nest` | `TilingPropagationPass` — propagate `{N,2}` tiling; opts = `(array<long,2>, bool)` |
| 7 | `createInferVectorLayoutPass` @ `0x132ecf60` | `mlir::mosaic_sc` | `nest` | `InferVectorLayoutPass` — SC vector-layout inference; opts = `InferVectorLayoutPassOptions` |
| 8 | `createInsertRelayoutPass` @ `0x132eff80` | `mlir::mosaic_sc` | `nest` | `InsertRelayoutPass` — insert layout-conversion ops |
| 9 | `createApplyVectorLayoutPass` @ `0x132e57e0` | `mlir::mosaic_sc` | `nest` | `ApplyVectorLayoutPass` — apply chosen layouts; opts = `ApplyVectorLayoutPassOptions` |
| 10 | `createCSEPass` @ `0x1c93e180` | `mlir` (generic) | `addPass` | `CSE` — common-subexpression elimination |
| 11 | `createLogicalToPhysicalDeviceIdPass` @ `0x132d5720` | `mlir::tpu` | `nest` | `LogicalToPhysicalDeviceIdPass` — remap DMA/semaphore `device_id` → physical core id (see [§Device-Id Remap](#device-id-remap-pass-11)) |
| 12 | `createLowerToMloPass` @ `0x1322adc0` (**LAST**) | `mlir::tpu` | `addPass` | `LowerToMloPass` — SC → MLO lowering; the collective op lowers into `EmitScsBarrier` |

### The per-pass construction idiom

Every one of the twelve stages is built with the same fixed sequence of calls — the body of `RunPasses` is, in effect, twelve copies of this template differing only in the factory and the attach mode:

```text
// for each pass p in the twelve:
pm = mlir::PassManager(ctx, name, Nesting)        // 0x1cb700a0 — a fresh single-pass manager
pm.enableIRPrinting( $_0 should-print closure )   // 0x132048e0 — the per-stage IR-dump gate
op = pm.<addPass | nest<func::FuncOp>().addPass>( create<P>Pass(opts) )   // 0x1cb6c000 / 0x1cb6d3e0
jellyfish::RunPass(pm, module, dump_prefix, ...)  // 0x14514d60 — run this one pass over the module
// pm destructed before the next stage
```

Three of the twelve carry a non-trivial options struct, and `RunPasses` materializes those options *between* passes by reading the surrounding configuration:

- **Pass 2 (`InferMemRefLayout`)** — the `Span<long const> tiling` and `TpuTilingFlags` are built from `Target::ShouldEnableLarge2ndMinorLayoutForX16` / `X8` / `X4` (`0x1d6b6920` / `0x1d6b6960` / `0x1d6b6940`); the layout flags differ per generation and per element width, so this option block is the per-gen seam in the pipeline.
- **Passes 7 and 9** — `InferVectorLayoutPassOptions` / `ApplyVectorLayoutPassOptions` are filled from the `SparseCoreConfig` defaults (`SparseCoreConfig_globals_` @ `0x223a99c8`) and the HLO `BackendConfig` (`0xf58e6c0`).
- **Pass 11 (`LogicalToPhysicalDeviceId`)** — its `optional<DeviceAssignment>` argument is sourced from `HloModuleConfig::static_device_assignment()` (`0x10fb7f60`); the `ChipTopology` and the trailing `bool` come from the emitter's target. See [§Device-Id Remap](#device-id-remap-pass-11).

> **NOTE — twelve managers, not one.** `RunPasses` constructs a new `mlir::PassManager` for each pass and tears it down before the next. There is no carried analysis state and no fused nesting: each `nest`-attached pass is wrapped in its own manager whose top level is the `ModuleOp` and whose single nested pass runs over every `func::FuncOp`. A reimplementation that builds one `PassManager` and `addPass`es all twelve will *behave equivalently for correctness* but will not reproduce the per-stage IR-dump gating or the per-stage `RunPass` timing the binary emits, and folds analysis caches the binary deliberately discards.

> **GOTCHA — the layout trio order is load-order-sensitive.** `InferVectorLayout` (7) annotates the chosen vector layouts, `InsertRelayout` (8) materializes conversion ops where neighbors disagree, and `ApplyVectorLayout` (9) rewrites ops to their chosen layout. Running `ApplyVectorLayout` before `InsertRelayout` leaves layout mismatches with no conversion op to bridge them; running either before `InferVectorLayout` rewrites against an unset layout. The three are a strict three-phase sequence, all `mosaic_sc`, all func-nested.

---

## Device-Id Remap (Pass 11)

Pass 11, `LogicalToPhysicalDeviceIdPass`, is the reason the pipeline can emit cross-core barriers that hit the right hardware. It runs immediately before the final `LowerToMlo` lowering and rewrites the `device_id` operand on the DMA/semaphore ops from a *logical* device index to a *physical* SparseCore core id. The pass name strings are read from `.rodata`: `kPassName = "LogicalToPhysicalDeviceIdPass"`, `kArgumentName = "logical-to-physical-device-id"`.

### Construction

The factory (`0x132d5720`, signature `(optional<DeviceAssignment>, ChipTopology, bool)`) heap-allocates the pass, `memcpy`s the `optional<DeviceAssignment>` (setting its present-bit) and the `ChipTopology` into the pass object, and zero-inits the per-`CoreType` topology maps. In `RunPasses` the `DeviceAssignment` source is `HloModuleConfig::static_device_assignment()` (`0x10fb7f60`) — confirmed in the decompiled body where `static_device_assignment` feeds the pass-11 construction.

### `runOnOperation` (`0x132d5ec0`)

The body reads `Target::CoresPerChip(TpuCoreType)`, `Target::LogicalDevicesPerChip(TpuCoreType)`, and `TpuChipConfig::Megachip()` to build a `FlatHashMap<CoreType, CoreTopology>` from the copied `ChipTopology` + `DeviceAssignment`, then walks each `func::FuncOp` (`mlir::detail::walk`, primary callback `0x132d71c0`). The callback matches ops carrying a `(target_core_type, device_id)` operand pair and rewrites the `device_id`:

```text
for each matched op in func:
    phys = GetPhysicalCoreID(builder, loc, op.getTargetCoreType())   // §below
    op.getDeviceIdMutable().assign(phys)                             // MutableOperandRange::assign
```

The decompiled callback (`0x132d71c0`) confirms it handles `tpu::EnqueueDMAOp` and `tpu::SemaphoreSignalOp` (both via `getTargetCoreType` / `getDeviceIdMutable`), calls `GetPhysicalCoreID` at four sites, and commits via `MutableOperandRange::assign`. The pass may also `insertArguments` to thread a physical-core-index function argument.

### `GetPhysicalCoreID` (`0x132d8de0`)

This is the logical→physical materializer. For `CoreType == SparseCore` it emits the MLIR op chain (decompile lines 177–183, in order):

```text
ci   = sparse_core::CoreIndexOp::create(b, loc)            // current SC core index
k    = arith::ConstantIndexOp::create(b, loc, stride)      // per-tile physical stride
mul  = arith::MulIOp::create(b, loc, ci, k)
tid  = sparse_core::TileIdOp::create(b, loc)               // tile id within the core
add  = arith::AddIOp::create(b, loc, mul, tid)
i32  = Builder::getI32Type()
phys = arith::IndexCastOp::create(b, loc, i32, add)        // → physical_core_id : i32
                                                           //   = i32(CoreIndex * stride + TileId)
```

For the non-SC (TensorCore) path it returns `llo::CoreIndexOp::create(b, loc)` directly. The `stride` is selected by walking the `ChipTopology` core-entry array (per-core role tag), and its literal value depends on the chip-config proto — see the confidence note below.

> **NOTE — why this must precede `LowerToMlo`.** The barrier lowering in pass 12 turns the collective op into hardware sync (`SemaphoreSignal` / `SyncAdd`) whose target is the `device_id` operand. If those operands still held logical ids, the emitted barrier would signal the wrong physical cores. Running the remap as pass 11 guarantees every DMA/semaphore op carries a physical core id by the time `LowerToMlo` consumes it.

---

## The MEGACORE Barrier

The last pass, `LowerToMloPass`, lowers the SC collective op; for a barrier it reaches `CollectiveEmitterBase::EmitScsBarrier` (`0x13352500`). The megacore even/odd core-pair sync lives *inside* the two barrier arms that emitter dispatches — it is not a fourth dispatch case.

### Dispatch: `EmitScsBarrier` (`0x13352500`)

The emitter reads the HLO `BackendConfig` → `BarrierConfig` (defaulting to `BarrierConfig_globals_` when absent), then switches on `barrier_type` at offset `+0x20` (decompiled as `*((_DWORD*)cfg + 8)`):

| `barrier_type` | Dispatch | SFLAG-number source |
|---|---|---|
| `1` GLOBAL | → `EmitGlobalBarrier` @ `0x13352820` | `GetSyncFlagForBarrierId(reserved id)` (see [Barrier → SFLAG Binding](../barrier/barrier-to-sflag-binding.md)) |
| `3` CUSTOM | → `EmitCustomBarrierFromConfig` @ `0x13352cc0` → `EmitCustomBarrierStart` @ `0x13352fc0` | `GetSyncFlagForBarrierId(colored id)` |
| `0` / `2` / `4` (incl. **MEGACORE=4**) | → `RetCheckFailSlowPath` (source line `0x5f`) | n/a — **illegal** at SC kernel emission |

The decompiled body confirms this exactly: `v11 == 1` calls `EmitGlobalBarrier`, `v11 == 3` calls `EmitCustomBarrierFromConfig`, and the `else` arm `RetCheckFail`s with the message `"backend_config.barrier_config().barrier_type() == jellyfish::BarrierType::CUSTOM"`.

> **CORRECTION — MEGACORE is not a barrier kind.** `BarrierType::MEGACORE(4)` reaching `EmitScsBarrier` is a hard error (`RetCheck`). The enum value is a *producer-side annotation*, not something the SC kernel emitter consumes. The actual megacore behaviour is a topology-gated inner split applied inside **both** the `GLOBAL` and `CUSTOM` arms whenever `LogicalDevicesPerChip(SC) == 2`. A reimplementer must not model "megacore barrier" as a distinct lowering branch.

### Megacore split: `EmitGlobalBarrier` (`0x13352820`)

When the part is a two-core megachip, `EmitGlobalBarrier` builds an even/odd leader/partner predicate and emits the barrier as two mutually-exclusive predicated regions. The decompiled body shows the arithmetic in order:

```text
ChipCount();  ldpc = LogicalDevicesPerChip(SC);          // total SC cores = ChipCount * ldpc
// even/odd LSB selector:
Shli(x, 1); And(...);  Compare(eq)  ── is_even predicate (LSB == 0 ⇒ leader)
lowering_util::Assert(...)
core_index    = GetCoreIndex()
chip          = DivU(core_index, CoresPerChip(SC))
gcid          = ToGlobalCoreId(chip, ...)
pred          = Compare(gcid, ...)                       ── leader/partner predicate
Predicated(pred,      $_0)   ── EVEN / LEADER  arm  @0x13355b60
Predicated(Not(pred), $_1)   ── ODD  / PARTNER arm  @0x13355f80
```

### The two arms

Each arm is a `__policy_func` thunk taking an `OpBuilder&` and returning a `Status`. Decompile op-create counts (verified per arm):

| Arm | `SyncWaitOp` | local `SyncAddOp` | partner `SyncAddOp` | `scf::ForOp` |
|---|:---:|:---:|:---:|:---:|
| `$_0` even / leader (`0x13355b60`) | 1 | 1 | 1 (inside the `ForOp`) | 1 |
| `$_1` odd / partner (`0x13355f80`) | 1 | 1 | 1 | 0 |

Each arm:

- emits `sparse_core::SyncWaitOp::create` (`0x14618fa0`) tagged via `setAttr` with a `getStringAttr` twine of value **`259` (= `0x103` = `IciDim::kCoresOnChip`)** — i.e. the wait is explicitly marked a cross-core (cores-on-chip) wait. The literal `259` is read directly from the decompiled thunk (`v46 = 259`);
- emits the simple local `sparse_core::SyncAddOp::create` (`0x14611f40`) — the local sync increment;
- emits the **megacore-partner** `sparse_core::SyncAddOp::create` variant (`0x146120c0`, the longer `(…CoreTypeAttr, bool)` signature) targeting the partner core, with the cross-core sflag offset computed via `SubsliceToFullSlice` (`0x133e79a0`). In the leader arm this partner add is wrapped in an `scf::ForOp` (`0x17866d60`) over `LogicalDevicesPerChip(SC)`.

The `0x103`/`kCoresOnChip` tag ties this barrier to the same megacore knob the SC tensor-split uses — see [Megacore Even/Odd Split](../twist/megacore-even-odd.md).

### The partner: `ToPartnerGlobalCoreId` (`0x133e66e0`)

The cross-core `SyncAdd` targets the *partner* core, computed by `OffloadFactory::ToPartnerGlobalCoreId`. The decompiled body is unambiguous:

```text
RetCheck( LogicalDevicesPerChip(SC) == 2 )               // megacore-only; else RetCheck line 0x50
local = GetCoreIndex()
chip  = DivUIOp(local, CoresPerChip(SC))
gcid  = ToGlobalCoreId(chip, ...)
return XOrIOp(gcid, ConstantIndexOp(1))                  // flip the low bit ⇒ even↔odd partner
```

The partner is the current core's id with its low bit flipped (`XOR 1`): core 0 ↔ core 1 within the megachip pair. On failure of the `LDPC(SC) == 2` `RetCheck`, the slow path logs `"GetPartnerGlobalCoreId() is only available for 2 logical devices per chip configurations."` — confirming there is no partner unless the part is a two-core megachip.

> **NOTE — XOR target.** The raw narration phrases the flip as `XOR(local, 1)` followed by a re-flatten through `ToGlobalCoreId`; the decompiled body flattens *first* (`ToGlobalCoreId(chip)`) and then `XOR`s the resulting global id with `1`. Both produce the same even↔odd partner core id for the two-core megachip case (`LDPC(SC) == 2`); the page documents the decompile-observed order. Confidence: CONFIRMED for the XOR-1 low-bit flip; the exact operand fed to `XOR` (global id vs local index) is HIGH.

### Worked rendezvous — what the two arms accomplish

Concretely, take a megachip with SC cores `{0, 1}` (`LDPC(SC) == 2`). Core `0` is even (leader, runs `$_0`); core `1` is odd (partner, runs `$_1`). Each core's emitted program does, in MLIR terms:

```text
core 0 ($_0 leader):                       core 1 ($_1 partner):
  SyncWait(local sflag, tag=kCoresOnChip)    SyncWait(local sflag, tag=kCoresOnChip)
  SyncAdd(local sflag)                       SyncAdd(local sflag)
  for d in 0..LDPC(SC):                       SyncAdd( partner = ToPartnerGlobalCoreId()
    SyncAdd( partner = ToPartnerGlobalCoreId()              = (1) XOR 1 = core 0 )
            = (0) XOR 1 = core 1 )
```

The partner of core `0` is `0 XOR 1 = 1`; the partner of core `1` is `1 XOR 1 = 0`. Each core increments the *other* core's sync flag (the cross-core `SyncAdd` via the `SubsliceToFullSlice` addressing into the partner's sflag window) and waits on its own (the `kCoresOnChip`-tagged `SyncWait`), so the pair rendezvous before either proceeds. This is the structure that is entirely absent on a single-core part — there the partner adds and the even/odd predication are suppressed and the barrier degenerates to a purely local `SyncAdd`/`SyncWait`.

### The CUSTOM arm runs the same split

`EmitCustomBarrierStart` (`0x13352fc0`, reached from the `CUSTOM(3)` dispatch) calls `ToPartnerGlobalCoreId`, `GetSyncFlagForBarrierId` (the colored barrier id → SFLAG number), the megacore-partner `SyncAddOp` variant, and `Predicated` — the *same* even↔odd partner cross-core sync as the `GLOBAL` arm. The only difference between `GLOBAL` and `CUSTOM` is the SFLAG-number source (reserved id vs colored id). The megacore split is shared by both.

> **GOTCHA — single-core parts have no partner path.** On a part where `LogicalDevicesPerChip(SC) == 1`, `ToPartnerGlobalCoreId` `RetCheck`s, so the partner-add and the even/odd predication never fire — only the local `SyncAdd` / `SyncWait` remain. A reimplementer must gate the entire partner machinery on `LDPC(SC) == 2`, not emit it unconditionally.

---

## How the SC Pipeline Contrasts with the TensorCore Stack

The [TensorCore scheduling stack](../sched/overview.md) is three stages over two IRs (HLO latency-hiding scheduler → MXU/MRB assignment → LLO bundle packer), priced by a bundle cost model. The SC back-end pipeline is a different animal: a flat twelve-pass MLIR pipeline over the `tpu`/`mosaic_sc` dialects ending in an MLO lowering, with **no separate resource-assignment stage** and **no cost-model-priced reordering**. The SC pipeline's only "scheduling-adjacent" decisions are vector layout (passes 7–9) and the per-engine outlining that *precedes* this pipeline (see [Region → Sequencer Outliner](region-to-sequencer-outliner.md)). The two pipelines meet only at the chip boundary: the SC barrier this pipeline emits synchronizes against the TensorCore through the shared sync-flag pool, and the device-id remap (pass 11) is what makes those cross-engine signals land on the right physical cores.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| `RunPasses` runs twelve single-pass `PassManager`s in sequence (not one 12-pass pipeline) | `RunPasses` @ `0x13202780`; per-pass `PassManager` ctor + `RunPass` idiom; decompile lines 229–253 | CONFIRMED |
| The twelve passes are in the exact order listed | twelve `create*Pass` call sites in order at decompile lines 231 → 1307 | CONFIRMED |
| Passes 7/8/9 are `mlir::mosaic_sc` (not `mlir::tpu`); pass 3 is generic `Canonicalizer` | `mosaic_sc` qualification at decompile lines 566/616/666 | CONFIRMED |
| `addPass` vs `nest` attach mode per pass matches the table | `OpPassManager::addPass`/`::nest` call sites one-for-one with the disassembly | CONFIRMED |
| Pass 11 `LogicalToPhysicalDeviceIdPass` remaps `device_id` of `EnqueueDMAOp` / `SemaphoreSignalOp` | callback `0x132d71c0` (`EnqueueDMA`/`SemaphoreSignal` + `getDeviceIdMutable` + `MutableOperandRange::assign`) | CONFIRMED |
| `GetPhysicalCoreID` (SC) = `i32(CoreIndex * stride + TileId)`; (TC) = `llo::CoreIndexOp` | `0x132d8de0` op-create chain decompile lines 177–183 | CONFIRMED |
| `kPassName` / `kArgumentName` strings | `.rodata` reads; decompile string matches | CONFIRMED |
| `EmitScsBarrier` dispatches `GLOBAL(1)` / `CUSTOM(3)` only; `0/2/4` (incl. MEGACORE) `RetCheck` | `0x13352500` decompile: `v11==1`/`==3`/`else RetCheck` line `0x5f` | CONFIRMED |
| Megacore even/odd split is an inner path inside `EmitGlobalBarrier` / `EmitCustomBarrierStart`, gated by `LDPC(SC)==2` | `0x13352820` predicate + arms; `0x13352fc0` shares `ToPartnerGlobalCoreId` | CONFIRMED |
| Partner core = current core id `XOR 1`; requires `LDPC(SC)==2` (`RetCheck` otherwise) | `ToPartnerGlobalCoreId` @ `0x133e66e0` decompile lines 35/57/58/60/62/69 | CONFIRMED |
| Each arm: 1 `SyncWaitOp` (tagged `0x103`=kCoresOnChip) + 1 local `SyncAddOp` + 1 partner `SyncAddOp` | arm thunks `0x13355b60` / `0x13355f80`; `v46 = 259` tag | CONFIRMED |
| The `$_0` (leader) partner add is wrapped in an `scf::ForOp` over `LDPC(SC)`; `$_1` has no `ForOp` | `ForOp::create` count = 1 in `$_0`, 0 in `$_1` | CONFIRMED |
| Raw-finding claim of "3× SyncAdd in `$_1`" | decompile shows 2 `SyncAddOp::create` in `$_1` (1 local + 1 partner) | CORRECTED to 2× |
| The literal `stride` in `GetPhysicalCoreID` | `MulIOp`/`AddIOp`/`TileIdOp` structure CONFIRMED; numeric stride selected by `ChipTopology` walk + chip-config proto, not pinned per codename | INFERRED (structure CONFIRMED, value not pinned) |
| The megacore-partner `SyncAddOp` `CoreTypeAttr` / `bool` and the `SyncWaitOp` `0x103`/`0x105` attr names | call-arg setup byte-traced; ODS field *names* attributed from `kCoresOnChip` value + context, not read from a named op definition | INFERRED |

---

## Cross-References

- [SparseCore Overview](overview.md) — the three engine classes, per-gen presence, and the embedding data path this pipeline lowers.
- [SparseCore Architecture](architecture.md) — engine roles and the embedding datapath in depth.
- [Region → Sequencer Outliner](region-to-sequencer-outliner.md) — the per-engine outlining that produces the `tpu`-dialect module this pipeline consumes.
- [SC EmitX Dispatcher](sc-emitx-dispatcher.md) — the seq3/seq4/seq5 → EmitX jump tables the per-engine codecs use downstream.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-selection function.
- [SCS (Scalar) Engine](scs-engine.md) — the scalar sequencer that runs the emitted sync ops.
- [TAC Engine](tac-engine.md) — the tile-access / DMA-issuer engine.
- [TEC (Vector) Engine](tec-engine.md) — the wide vector compute engine the layout passes (7–9) target.
- [SC Core Selection](sc-core-selection.md) — physical SparseCore core selection, the counterpart to this pipeline's device-id remap.
- [GetSparseCoreConfig](getsparsecoreconfig.md) — the offload op-type configuration source feeding the pipeline.
- [Barrier → SFLAG Number Binding](../barrier/barrier-to-sflag-binding.md) — the colored/reserved barrier id → hardware SFLAG number map the barrier arms call.
- [BarrierColoring](../barrier/barrier-coloring.md) — the graph-coloring engine that assigns the CUSTOM barrier colors.
- [Global-Barrier SFLAG Window](../barrier/global-barrier-window.md) — the reserved SFLAG window the GLOBAL arm draws from.
- [Megacore Even/Odd Split](../twist/megacore-even-odd.md) — the `LDPC(SC)==2` / `kCoresOnChip` topology knob this barrier's partner sync shares.
- [Physical-Core Placement](../collectives/physical-core-placement.md) — the per-color physical-core mapping for SC-offload collectives.
- [TPU Scheduling Pipeline](../sched/overview.md) — the contrasting TensorCore-side three-stage scheduling stack.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore back-end — [back to index](../index.md)
