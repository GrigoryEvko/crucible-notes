# Layout Assignment

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

Layout assignment is the XLA pass that fixes the *physical* minor-to-major dimension order and HBM tiling of every tensor in the module, before the graph is fused and lowered. On TPU the pass is `xla::jellyfish::TpuLayoutAssignment` (`RunImpl` @ `0x110ace00`), a subclass of the open-source `xla::LayoutAssignment`. The base class owns the bidirectional constraint-propagation worklist; the TPU subclass overrides only the *backend extension points* — which seed constraints to plant (`AddBackendConstraints` @ `0x110b19a0`), how each op picks a layout for an operand/result, which ops may change layout, and a per-tensor fallback chooser for buffers that propagation never reaches (`FindMemoryMinimizingLayout` @ `0x1109dfe0`). It runs inside Phase 4 (`HloOptimizeThroughLayoutAssignment`), after sharding/SPMD and before fusion; see [compile-phases.md](compile-phases.md).

The reader who knows the upstream XLA `LayoutAssignment` will recognise the skeleton: seed mandatory constraints, run a forward+backward fixpoint over a worklist of `BufferLayoutConstraint`/`OperandLayoutConstraint` records, then commit layouts and insert `kCopy` where a producer's layout cannot feed a consumer's required layout. What is TPU-specific is the *content* of the constraints — the MXU's 128-lane / 8-sublane tile drives every dot, convolution, reduce-window, scatter and collective op toward a layout whose two minor-most dimensions tile cleanly — and a re-run loop wrapped around the OSS pass: `RunImpl` runs OSS layout assignment to a fixpoint, validates with `ModuleLayoutIsValid` (`0x110a6b80`), and if any TPU validator (Gather/Scatter/SelectAndScatter/ReduceWindow) rejects, it re-seeds and runs again.

This page documents three things a reimplementer must reproduce: the per-tensor layout chooser `FindMemoryMinimizingLayout` and its actual cost model (byte-size first, minor-dimension fill-efficiency ratio as tie-break — **not** a weighted sum of discrete penalties), the seed constraints planted by `AddBackendConstraints` per opcode, and how those choices reach memory-space assignment (MSA) and the scheduler through the `memory_space` integer and the tile geometry stamped on each `xla::Layout`.

For reimplementation, the contract is:

- **The driver shape.** The OSS pass under a TPU re-run loop, with a TPU validator gating each iteration.
- **The chooser.** `FindMemoryMinimizingLayout` — its element-type gate, its rank>3 search gate, and the exact two-key cost: compact bytes, then minor-dim fill ratio.
- **The seed constraints.** The opcode→handler dispatch in `AddBackendConstraints` and the mandatory MXU / packed-collective / window-stride==1 rules each handler enforces.
- **The downstream contract.** What MSA and scheduling read out of the assigned `xla::Layout` (memory space, tile).

| | |
|---|---|
| **Pass class** | `xla::jellyfish::TpuLayoutAssignment : xla::LayoutAssignment : HloPassInterface` |
| **Driver** | `TpuLayoutAssignment::RunImpl` @ `0x110ace00` (0x37c4 ≈ 14 KB) |
| **Seed constraints** | `TpuLayoutAssignment::AddBackendConstraints` @ `0x110b19a0` (0x546d ≈ 21 KB; `ModuleLayoutIsValid` @ 0x585a ≈ 22 KB is marginally larger) |
| **Per-tensor chooser** | `(anon)::FindMemoryMinimizingLayout` @ `0x1109dfe0` |
| **Compact-layout chooser** | `Target::ChooseCompactLayoutForShape` @ `0x1d61bd00` |
| **OSS base RunImpl / AssignLayouts** | `0x169bf440` / `0x169bb0e0` |
| **Pipeline slot** | Phase 4 tail (`HloOptimizeThroughLayoutAssignment`), after SPMD, before fusion |
| **IR level** | XLA HLO (`HloModule` / `HloInstruction`), pre-MLIR |
| **Source file** | `platforms/xla/service/jellyfish/tpu_layout_assignment.cc` (rodata) |
| **MXU tile** | `(SublaneCount, LaneCount)` = `(8, 128)` this build (v5+/ghostlite v6); `(16, 128)` on pufferfish v4 |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## Driver — RunImpl and the re-run loop

### Purpose

`RunImpl` is the TPU override of the pass entry point. It does not reimplement layout assignment; it wraps the open-source `xla::LayoutAssignment::RunImpl` (`0x169bf440`) in setup, a parse step for Mosaic kernels, and an outer validate-and-re-run loop. The wrap exists because the OSS algorithm assigns layouts that are *legal for arbitrary backends*; some of them are illegal for the TPU's gather/scatter/reduce-window restrictions, and the only way to discover that is to run OSS to a fixpoint, validate, and re-seed the offenders.

### Entry Point

```text
TpuLayoutAssignment::RunImpl  (0x110ace00)                  ── driver, timed "XLA::JF Layout Assignment"
  ├─ GetTpuCompEnv / PartitionReplicaMapper setup           ── multi-slice (megascale) topology
  ├─ PreprocessModule (0x1109ff20)                          ── insert kCopy boundary nodes
  ├─ ParseCustomCallKernelsInParallel                       ── pin Mosaic kernel I/O layouts
  └─ loop:
       ├─ xla::LayoutAssignment::RunImpl (0x169bf440)        ── OSS: seed → propagate → assign
       │     └─ TpuLayoutAssignment::AddBackendConstraints   ── (TPU vtable hook, fires inside OSS)
       └─ ModuleLayoutIsValid (0x110a6b80)                   ── TPU validator; re-seed + repeat if false
```

### Algorithm

```c
function RunImpl(module, exec_threads):              // 0x110ace00
    this->tpu_comp_env = GetTpuCompEnv(module)        // member +1472 bytes (offset 184)

    if this->target->GetMultiSliceTopology():         // megascale / ICI mesh
        CHECK(module.config().static_device_assignment().has_value())   // src:3618
        this->partition_replica_mapper =
            PartitionReplicaMapper::Create(this->target, module.device_assignment())

    ScopedLoggingTimer timer(name(), kXlaTimerTraceStats0)  // guard 0x224cdec0
    TraceMe("XLA::JF Layout Assignment")

    // Insert kCopy boundary nodes so propagation has free choices at the seams.
    PreprocessModule(module, exec_threads)            // 0x1109ff20

    // Mosaic (Pallas) kernels arrive with layouts pinned in their kernel proto.
    // Parse them and stash per-operand / per-result layouts in the proposed maps.
    ParseCustomCallKernelsInParallel(module)          // populates members +0x420 / +0x440
    for (instr, kernel) in tpu_custom_call instructions:
        operand_layouts_map[instr] = CreateTwoMinorLayout(kernel.input_dims)   // 0x110c1fa0
        result_layouts_map[instr]  = CreateTwoMinorLayout(kernel.output_dims)

    passes_run = 0
    while true:
        // VLOG(2) "Before TpuLayoutAssignment: " ...                src:3625
        LogNeighborhoodFingerprints(module, "layout", 6, 6)          // src:3626

        // Reject ops that must have been removed by earlier expanders.
        for c in module.MakeNonfusionComputationsSorted(exec_threads):
            for inst in c.MakeInstructionPostOrder():
                if inst.opcode in {batch-norm-grad/inf/training} or
                   (inst.opcode == fusion and inst.fusion_kind != kCustom):
                    return InvalidArgument(
                      "Instruction %s is not expected to be seen during layout assignment")

        // The OSS engine: AddBackendConstraints (our hook) → PropagateConstraints
        //                  → AssignLayouts → PostProcess.
        xla::LayoutAssignment::RunImpl(module, exec_threads)         // 0x169bf440 ; src:3737
        ++this->first_pass_count                                     // member +183
        if passes_run == 0: VLOG(2) "Ran first pass of layout assignment."

        if not this->ModuleLayoutIsValid(module, exec_threads):      // 0x110a6b80
            VLOG(2) "Running " passes_run " additional passes of layout assignment ..."
            // VLOG(3) the four offender lists (at-risk / scatter / scatter-updates /
            //         incompatible / expensive-collective), src:3679-3723
            ++passes_run
            continue          // re-seed: the recorded incompatibilities steer the next pass
        break

    // VLOG(2) "After TpuLayoutAssignment: " ...                     src post-loop
    return /*module_changed=*/ true
```

> **NOTE —** the re-run is driven by *validation failure*, not by a fixed iteration count. Each OSS run is itself a complete forward/backward fixpoint; the outer loop only fires again when a TPU-specific validator rejects an op the OSS engine considered done. In practice the second pass is rare — it triggers when a gather/scatter/reduce-window op was handed a layout its TPU lowering cannot realise, and the recorded "incompatible layouts" lists feed the re-seed.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuLayoutAssignment::RunImpl` | `0x110ace00` | Driver + re-run loop | CONFIRMED |
| `TpuLayoutAssignment::PreprocessModule` | `0x1109ff20` | Insert `kCopy` boundary nodes pre-assignment | HIGH |
| `TpuLayoutAssignment::ModuleLayoutIsValid` | `0x110a6b80` | TPU module-level validator (loop gate) | CONFIRMED |
| `xla::LayoutAssignment::RunImpl` (base) | `0x169bf440` | OSS seed→propagate→assign engine | CONFIRMED |
| `xla::LayoutAssignment::AssignLayouts` (base) | `0x169bb0e0` | Commit chosen layouts to module | CONFIRMED |
| `xla::LayoutAssignment::PropagateConstraints` (base) | `0x169b8120` | Forward+backward worklist | CONFIRMED |
| `(anon)::CreateTwoMinorLayout` | `0x110c1fa0` | Build a `Layout` from pinned 2-minor dims | HIGH |
| `GetReduceLayoutFromOperand` (anon) | `0x110ac4e0` | Forward reduce layout | HIGH |
| `MemorySpaceColorMap::UpdateFromLayout` | `0x110411a0` | Read `memory_space` into per-buffer color map (feeds MSA) | HIGH |

> **QUIRK —** the `TpuLayoutAssignment` members are accessed as `*((qword*)this + N)` in the decompile. The two that matter most: `target_` is at qword `+181` (1448 bytes in) — every handler reaches the chip descriptor through it — and the `allow_relayout` flag is at qword `+152` (offset `0x4C0`), tested as `*((_QWORD*)this + 152) != 0` before any handler is permitted to insert a relayout copy.

---

## Per-Tensor Chooser — FindMemoryMinimizingLayout

### Purpose

When constraint propagation never reaches a buffer — typically a floating-point intermediate deep in a long elementwise chain — something must still choose its layout. For rank>3 tensors that choice is `FindMemoryMinimizingLayout` (`0x1109dfe0`): it searches permutations of the major dimensions and keeps the one with the smallest compact byte size, breaking ties by how completely the two minor-most dimensions fill their lane/sublane tile. It is reached two ways: from `GetUnconstrainedLayout` (`0x110b7520`, the OSS "no constraint reached this buffer" fallback) and directly from `AddBackendConstraints` on the copy-into-SparseCore path.

### Algorithm

```c
function FindMemoryMinimizingLayout(target, shape /*in/out*/, out_bytes, cap):  // 0x1109dfe0
    // ---- Guard 1: element-type must be in the searchable set ----
    // mask = 0x2FFF91FFE ; only proceed if primitive-type bit is set.   movabs @0x1109e014
    et = shape.element_type()
    if et > 0x21 or not bittest64(0x2FFF91FFE, et):
        return                                  // type not layout-searchable; keep incoming

    CHECK(shape.IsArray()); CHECK(LayoutUtil::HasLayout(shape))   // src:528-529

    // ---- Baseline: compact byte size of the incoming layout ----
    best_bytes = target->ShapeSizeCompact(shape)        // 0x1d61a620
    if out_bytes != null: *out_bytes = best_bytes

    // ---- Guard 2: only search when rank > 3 ----
    if shape.dimensions_size() <= 3:                    // @0x1109e0d8 (cmpq $3 ; ja)
        return                                          // low rank: keep OSS default layout

    // ---- Minor-dimension tile fill factors (used only for the tie-break) ----
    lane_chunks    = shape.dimensions_minor(0) / target->LaneCount()      // 0x1d60f400
    sublane_chunks = shape.dimensions_minor(1) / target->SublaneCount()   // 0x1d60f300

    // ---- Search candidate permutations of the (rank>>1) major dims ----
    for perm in candidate_major_permutations(shape):
        apply perm to shape.layout().minor_to_major()
        cand_bytes = target->ShapeSizeCompact(shape)    // @0x1109e504
        if cap.has_value() and cand_bytes > cap: continue

        // KEY 1 (primary): strictly fewer bytes wins.       jl @0x1109e77c (accept)
        if cand_bytes < best_bytes:
            best = perm; best_bytes = cand_bytes; continue

        // KEY 2 (tie-break, equal bytes): lower fill-waste wins.
        //   cost = (per-dim utilization fractions, built via vcvtsi2sd/vdivsd/vmulsd)
        //   @0x1109e262 and @0x1109e73a, compared @0x1109e78c (vucomisd ; jbe @0x1109e790 reject)
        if cand_bytes == best_bytes and cand_cost < best_cost:
            best = perm; best_cost = cand_cost

    commit best to shape.layout()
```

### The cost model — what it is and is not

The cost is **two keys in priority order**:

| Key | Quantity | Source | Rule |
|---|---|---|---|
| 1 (primary) | Compact byte size `ShapeSizeCompact(shape)` | `0x1d61a620` (target-aware: tile + dtype packing) | strictly fewer bytes ⇒ accept |
| 2 (tie-break) | Minor-dimension fill-efficiency ratio (product of per-dim utilization fractions) | built from `dimensions_minor(0)/LaneCount` and `dimensions_minor(1)/SublaneCount` | equal bytes, lower fill-waste ⇒ accept |

The only numeric constants in the function are the element-type validity bitmask `0x2FFF91FFE` (bit *i* set ⇒ primitive type *i* is searchable) and the lane/sublane divisors (read from the chip descriptor, not literals). The decompile confirms both directly: `if ( v10 > 0x21 || (v11 = 0x2FFF91FFELL, !_bittest64(&v11, v10)) )` at the entry, and the `vcvtsi2sd xmm0,…,r12 / vcvtsi2sd xmm1,…,r15 / vdivsd` sequence building the ratio from the two minor-dim chunk counts.

> **CORRECTION (LAYOUT-1) —** an earlier reconstruction described this cost as a weighted combination of *bytes* and a *predicted-copy-count* term sourced from `adaptive_layout_map_`, gated by a float constant `dword_84A28E8` ("factor = 1.0"). Decompilation shows no such term and no such constant in this function. The cost is **min compact bytes, tie-broken by minor-dim fill ratio**; there is no discrete transpose/copy/bitcast penalty and no copy-count feedback inside `FindMemoryMinimizingLayout`. (The copy-count adaptivity that does exist lives elsewhere — `AdaptiveHloLayoutMap::RemoveOnCopyOverhead` @ `0x110accc0` — and acts on profile-loaded overrides between passes, not on this chooser.)

> **GOTCHA —** the rank gate is `> 3`, not `>= 3`. Rank-1/2/3 tensors are never searched; they keep the OSS `GetDefaultLayoutForRank` layout. A reimplementation that searches at rank 3 will produce different (and slower-to-converge) layouts than libtpu for the very common 3-D activation shapes.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `(anon)::FindMemoryMinimizingLayout` | `0x1109dfe0` | Rank>3 unconstrained chooser | CONFIRMED |
| `TpuLayoutAssignment::GetUnconstrainedLayout` | `0x110b7520` | OSS fallback hook; calls the chooser then `ChooseCompactLayoutForShape` | HIGH |
| `Target::ShapeSizeCompact` | `0x1d61a620` | Compact byte size (cost key 1) | CONFIRMED |
| `Target::LaneCount` / `SublaneCount` | `0x1d60f400` / `0x1d60f300` | Tile divisors (cost key 2) | CONFIRMED |
| `Target::ChooseCompactLayoutForShape` | `0x1d61bd00` | Catch-all compact/SparseCore chooser | CONFIRMED |

---

## Seed Constraints — AddBackendConstraints

### Purpose

`AddBackendConstraints` (`0x110b19a0`) is the TPU vtable hook the OSS engine calls before propagation. It plants the *seed* constraints that the worklist then fans out. The body is a sequence of independent `for inst in computation->MakeInstructionPostOrder()` loops, each gated by an inline opcode-byte test on `HloInstruction+0xc` (verified: `movzbl 0xc(%r12),%eax`). Each loop seeds layouts via the OSS `SetInstructionLayout` / `SetArrayOperandLayout` / `SetOperandLayout` (`0x169b0740` / `0x169b00e0` / `0x169af600`).

### Algorithm — opcode dispatch

```c
function AddBackendConstraints(constraints):              // 0x110b19a0
    target = this->target_                                 // member qword +181
    allow_relayout = (this->[qword 152] != 0)              // offset 0x4C0

    // LOOP 1 — HBM-transfer / RNG boundary ops -> compact tiled layout, tiles cleared.
    for inst where opcode in {recv(89), rng-bit-generator(100), send(110)}:
        L = target->ChooseCompactLayoutForShape(inst.operand(0).shape())   // 0x1d61bd00
        ClearTiles(L); SetInstructionLayout(inst, L); SetArrayOperandLayout(inst.operand(0), L)

    // LOOP 2 — adaptive / autofdo-proposed overrides (seeded before per-op loops).
    for inst where this->adaptive_layout_map.HasLayout(inst):              // 0x110b6e20
        SetInstructionLayout(inst, adaptive_layout_map.GetLayout(inst), pin=false, set_default=false)

    // LOOP 3 — Mosaic custom-call operand/result pinning (from RunImpl parse step).
    for inst in operand_layouts_map (members +0x420 / +0x440):
        if single-output: pin operand-0 layout = front()
        elif tuple kCall(27)/kFusion(61): pin each tuple element's layout

    // Collective ops -> packed-lane layout, replicas must agree.
    for inst where CollectiveOpsNeedPackedLayout(inst):                    // 0x1109ef40
        // switch on opcode: all-gather(6), all-gather-start(8), all-reduce(9),
        //   all-reduce-start(11), all-to-all(12), collective-broadcast(33),
        //   collective-permute(34), ragged-all-to-all(86), reduce-scatter(93)
        SetInstructionLayout(inst, compact, pin=1, allow=1, default=1)

    // LOOP 4 — the bulk: per-op layout choosers.
    GetLayoutConfig(span, &conv_decisions /*this+0x500*/, &op_decisions /*this+0x560*/)  // 0x110a5940
    for inst in computation->MakeInstructionPostOrder():
        switch inst.opcode:
          case dot(52):
          case convolution(43):
            AssignConvolutionLayout(inst, this, target, &conv_decisions, allow_relayout)  // 0x11096160
          case reshape(97):
            AssignReshapeLayout(target, inst, this, &op_decisions, allow_relayout)        // 0x1109be80
          case select-and-scatter(109):
            AssignSelectAndScatterLayout(target, inst, this)                              // 0x1109b400
          case ragged-dot(87):
            AssignRaggedDotLayout(inst, this, target)                                     // 0x1109a060
          case reduce-window(94):
            // max window dim over operand; if 2nd-minor extent >= SublaneCount() (runtime call)
            // allocate new optimal layout; window bound/stride must be 1 in 2 minor dims
          case concatenate(39):
            CreateShapeWithOptimalLayoutForConcat(target, inst); pin concat layout        // 0x110b1320
          case gather(62):
            GatherLayoutIsValid(inst, false)                                              // 0x110a2d80
            AddIndicesLayoutConstraintForScatterGather(inst, idx=dnums+0x90, this)        // 0x110b7080
          case scatter(107):
            ScatterLayoutIsValid(inst)                                                    // 0x110a12a0
            AddIndicesLayoutConstraintForScatterGather(inst, idx=dnums+0x90, this)
            pin each update operand via GetScatterUpdatesLayout                           // 0x110a2600
          case copy(44) feeding SparseCore:
            if TransferSizeUtil::HasSparseCoreLayout(target.Topology(), op.shape()):      // 0x110b7440
                FindMemoryMinimizingLayout(target, op.shape(), ...); pin                  // 0x1109dfe0
```

### Mandatory constraints by category

The handlers above enforce a small set of hard rules. These are what a reimplementer must reproduce; the per-op handler is just the mechanism.

| Category | Ops | Mandatory layout rule | Enforced by |
|---|---|---|---|
| MXU feed | dot(52), convolution(43) | Contracting (input-feature) dim reaches the MXU as the inner reduction; output-feature/batch tiled to the lane/sublane MXU tile via `TwoMinorSize` (`0x110bd3a0`) | `AssignConvolutionLayout` (`0x11096160`) |
| HBM transfer | recv(89), send(110), rng-bit-generator(100) | Compact tiled layout with tiles cleared (linear at the transfer boundary) | LOOP 1 inline |
| Collective | all-gather(6/8), all-reduce(9/11), all-to-all(12), collective-broadcast(33), collective-permute(34), ragged-all-to-all(86), reduce-scatter(93) | Packed-lane layout; all replicas agree | `CollectiveOpsNeedPackedLayout` (`0x1109ef40`) |
| Gather/Scatter | gather(62), scatter(107) | Indices operand pinned at `index_vector_dim` (packed); scatter updates pinned via `GetScatterUpdatesLayout` | `Add…ForScatterGather` (`0x110b7080`) |
| Window | reduce-window(94), select-and-scatter(109) | Window bound & stride must be 1 in the two minor-most dims; pass tries to make the window dim outer | `AssignSelectAndScatterLayout` (`0x1109b400`) + inline |
| Concat | concatenate(39) | Single optimal layout chosen across all operands | `CreateShapeWithOptimalLayoutForConcat` (`0x110b1320`) |
| Copy→SparseCore | copy(44) | Memory-minimising layout, then pinned | `FindMemoryMinimizingLayout` (`0x1109dfe0`) |

> **GOTCHA —** the window-op rule ("bound and stride must be 1 in the two minor-most dims") is the source of a runtime fatal if the pass cannot satisfy it: *"Encountered select-and-scatter with a window bound or window stride in one of the two minor-most dimensions that are not 1, which is not implemented for TPU. XLA tries to choose a layout such that this is not the case, but that does not always succeed."* A reimplementation that does not steer the window dim outer will hit this on legal HLO.

> **QUIRK —** there is **no separate `kDot` loop**. Dot (opcode 52) and convolution (opcode 43) share the single `AssignConvolutionLayout` call site; `AssignConvolutionLayout` self-dispatches on `0x34`(dot)/`0x2b`(conv) at its entry. A reimplementer expecting a distinct matmul layout routine will not find one — the MXU tiling math is unified.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuLayoutAssignment::AddBackendConstraints` | `0x110b19a0` | Seed-constraint dispatch (per-opcode loops) | CONFIRMED |
| `TpuLayoutAssignment::AssignConvolutionLayout` | `0x11096160` | dot+conv MXU layout (self-dispatch 0x34/0x2b) | CONFIRMED |
| `TpuLayoutAssignment::AssignReshapeLayout` | `0x1109be80` | Reshape layout via `ImproveReshapeLayout` | HIGH |
| `TpuLayoutAssignment::AssignSelectAndScatterLayout` | `0x1109b400` | Window-op joint layout | HIGH |
| `TpuLayoutAssignment::AssignRaggedDotLayout` | `0x1109a060` | RaggedDot (variable-batch matmul) layout | HIGH |
| `TpuLayoutAssignment::GatherLayoutIsValid` | `0x110a2d80` | Gather validator | HIGH |
| `TpuLayoutAssignment::ScatterLayoutIsValid` | `0x110a12a0` | Scatter validator | HIGH |
| `TpuLayoutAssignment::CollectiveOpsNeedPackedLayout` | `0x1109ef40` | Collective packed-lane test (opcodes 6/8/9/11/12/33/34/86/93) | CONFIRMED |
| `TpuLayoutAssignment::GetLayoutConfig` | `0x110a5940` | Load autofdo proposed layouts into decision caches | HIGH |
| `(anon)::AddIndicesLayoutConstraintForScatterGather` | `0x110b7080` | Pin indices operand layout | HIGH |
| `(anon)::CreateShapeWithOptimalLayoutForConcat` | `0x110b1320` | Concat optimal layout | HIGH |
| `(anon)::TwoMinorSize` | `0x110bd3a0` | Two-minor MXU tile sizing (8 call sites in conv) | CONFIRMED |

---

## Propagation Hooks — per-op layout choice

### Purpose

Inside each OSS propagation step, the engine asks the subclass "given this operand layout, what output layout?" (forward) and "given this output layout, what operand layout?" (backward). The TPU override answers for a handful of opcodes and delegates the rest to the OSS base. It also overrides two policy predicates: which ops may change layout at all, and whether an output layout is *always* pushed to operands.

### Algorithm — forward / backward routing

```c
function ChooseOutputLayoutFromOperandLayout(layout, inst, opnd_idx):   // 0x110ba2c0 (forward)
    switch inst.opcode:
      case reduce(91):   return GetReduceLayoutFromOperand(layout, inst)      // 0x110ac4e0
      case gather(62):   return GetGatherOutputLayout(inst, layout, idx)      // 0x110a4be0
      case reshape(97):  return ImproveReshapeLayout(layout, shape, …, target)// 0x110b8f00
      default:           return OSS::ChooseOutputLayoutFromOperandLayout(...) // 0x169b76a0

function ChooseOperandLayoutFromOutputLayout(layout, inst, opnd_idx):   // 0x110b7de0 (backward)
    switch inst.opcode:
      case scatter(107): if opnd is the updates operand:
                           return GetScatterUpdatesLayout(target, inst, layout,
                                    contiguous_update_window_dim)             // 0x110a2600
      case reduce(91):   return <reduce-operand layout>
      case reshape(97):  return ImproveReshapeLayout(...)                     // 0x110b8f00
      case fusion(61):   return FindOperandPilots(...)   // aggressive-loop-fusion path; flag-gated
      default:           return OSS::ChooseOperandLayoutFromOutputLayout(...) // 0x169b64c0

function OutputLayoutAlwaysPropagateToOperands(inst):                   // 0x11094ae0
    if inst.opcode == 127 /*transpose*/: return true
    return OSS::OutputLayoutAlwaysPropagateToOperands(inst)                   // 0x169b7660

function InstructionCanChangeLayoutDeepsea(inst):                       // 0x110b0640
    switch inst.opcode:
      case fusion(61):       can-change iff fusion_kind == kCustom (else cmps 40/120/49)
      case custom-call(49):  look up CompilationProperties in the global custom-call
                             registry (0x10a87cc0); use its instruction_can_change_layout
      case batch-norm-grad/inf/training 21/22/23: FATAL "!IsInvalidInstructionDuringLayoutAssignment" (src:3811)
      default:               return OSS::InstructionCanChangeLayout(inst)     // 0x169c19c0
```

> **QUIRK —** `OutputLayoutAlwaysPropagateToOperands` returning true for **transpose (opcode 127)** is what lets a transpose be realised as a *pure layout change* (free) rather than a data copy: the desired output layout is forced onto the transpose's operand, so the transpose becomes a relabel of `minor_to_major`. The decompile is unambiguous — `if ( *((_BYTE *)a2 + 12) == 127 ) return 1;`. An earlier note guessed opcode 127 was `kCopy`; the authoritative HloOpcode map pins 127 = `transpose`.

> **NOTE —** the custom-call branch of `InstructionCanChangeLayoutDeepsea` is the pass's extensibility hook. Every TPU custom kernel (`tpu_custom_call`, `XlaMosaic`, `TopK`, `Sharding`, …) registers a `CompilationProperties` struct whose `instruction_can_change_layout` boolean is consulted here. A reimplementer adding a custom op must register it or layout assignment will treat it with the OSS default.

### Authoritative opcode integers used on this page

The per-op dispatches above are byte tests against the upstream `HloOpcode` enum as compiled into this libtpu (decoded from `HloOpcodeString` @ `0x1e5ef000`). The integers a reimplementer needs:

| Opcode | Int | Opcode | Int | Opcode | Int |
|---|---|---|---|---|---|
| convolution | 43 (`0x2b`) | reduce | 91 (`0x5b`) | reduce-scatter | 93 (`0x5d`) |
| copy | 44 (`0x2c`) | reduce-window | 94 (`0x5e`) | gather | 62 (`0x3e`) |
| dot | 52 (`0x34`) | reshape | 97 (`0x61`) | scatter | 107 (`0x6b`) |
| custom-call | 49 (`0x31`) | fusion | 61 (`0x3d`) | select-and-scatter | 109 (`0x6d`) |
| collective-permute | 34 (`0x22`) | ragged-all-to-all | 86 (`0x56`) | ragged-dot | 87 (`0x57`) |
| concatenate | 39 (`0x27`) | transpose | 127 (`0x7f`) | sort | 120 (`0x78`) |

> **CORRECTION (LAYOUT-2) —** the opcode integers above supersede an earlier table that read several wrong: it had convolution=49 (49 is custom-call), broadcast=0x78 (0x78 is sort), and listed a nonexistent opcode 130 (the enum ends at 127). Treat any opcode integer not in the table above as re-derive-before-use.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuLayoutAssignment::ChooseOutputLayoutFromOperandLayout` | `0x110ba2c0` | Forward per-op routing | CONFIRMED |
| `TpuLayoutAssignment::ChooseOperandLayoutFromOutputLayout` | `0x110b7de0` | Backward per-op routing | CONFIRMED |
| `TpuLayoutAssignment::OutputLayoutAlwaysPropagateToOperands` | `0x11094ae0` | transpose-always-propagate rule | CONFIRMED |
| `TpuLayoutAssignment::InstructionCanChangeLayoutDeepsea` | `0x110b0640` | Per-op can-change bitmap | CONFIRMED |
| `(anon)::ImproveReshapeLayout` | `0x110b8f00` | Reshape source↔target dim mapping (both directions) | HIGH |
| `(anon)::GetScatterUpdatesLayout` | `0x110a2600` | Scatter-updates backward layout | HIGH |
| `(anon)::GetGatherOutputLayout` | `0x110a4be0` | Gather forward output layout | HIGH |
| `xla::LayoutAssignment::InstructionCanChangeLayout` (base) | `0x169c19c0` | OSS fallback | CONFIRMED |

---

## Depth-Aware Layout Cost and the Tile

### The tile geometry

Every assigned `xla::Layout` carries a physical HBM tile whose dims are `(SublaneCount, LaneCount)`. The values are read from the runtime chip descriptor at `target->[0x3b8]`, not hard-coded:

- `Target::LaneCount()` (`0x1d60f400`) = `target->[+0x3b8]->[+0x198]` — `128` across all generations.
- `Target::SublaneCount()` (`0x1d60f300`) = `target->[+0x3b8]->[+0x1a0]` — `8` on this v5+/v6 build, `16` on jellyfish v4.
- `Target::ChunksPerTile()` (`0x1d60f2c0`) = `[+0x198] / [+0x1a0]` (lane_count / sublane_count, off the same descriptor).

So the default tile is `(8, 128)` for this build and `(16, 128)` for v4. The tile and `memory_space` are stamped onto leaf subshapes by `HardwareLayout::PopulateDefaultLayout` (`0x1d6da120`) / `HardwareLayout::PopulateShape` (`0x1d6da360`). A separate per-shape fixup, `(anon)::UpdateLayout(target, Layout, Shape&)` (`0x110f66a0`), copies a chosen `Layout` onto a shape and then delegates to `Target::UpdateLayout` (`0x1d618aa0`), which runs `TransferSizeUtil::UpdateLayout` (`0x1d6b05a0`) to recompute the tile/packing after a dtype change. Several `jellyfish` passes expose their own `UpdateLayout(Shape*)` shim (`TpuSpmdPartitioner` `0x127a4100`, `TpuBFloat16Propagation` `0x110128e0`, `TpuBFloat16Normalization` `0x11012980`, `TpuReduceWindowRewriter` `0x109589c0`, others) so that layout metadata is refreshed after a pass mutates a shape; these are independent shims that each call into the `Target`/`TransferSizeUtil` layout path rather than one shared helper.

### Where "depth" enters

The cost in `FindMemoryMinimizingLayout` is per-tensor and tile-aware, not graph-depth-aware: it sizes the tensor's compact bytes under each candidate `minor_to_major` and prefers the layout whose two minor dims fill the `(SublaneCount, LaneCount)` tile most completely. The "depth-aware" behaviour of the *pass as a whole* comes from the iterative re-run loop (deeper chains surface more incompatibilities, which re-seed the next pass) and from the `Compact2ndMinorRatio` (`0x1d61a4e0`) packing check used by `ScatterLayoutIsValid` to confirm the 2nd-minor dim packs cleanly into the sublane tile.

> **QUIRK —** `ShapeSizeCompact` already folds the tile and dtype packing into the byte count it returns, so "minimise bytes" and "fill the tile" are not independent goals — a layout that wastes tile lanes pads to more bytes. The fill-ratio tie-break only matters when two permutations pad to exactly the same byte total but distribute the waste differently across the lane vs sublane dim.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `Target::LaneCount` / `SublaneCount` / `ChunksPerTile` | `0x1d60f400` / `0x1d60f300` / `0x1d60f2c0` | Tile dims from chip descriptor | CONFIRMED |
| `Target::Compact2ndMinorRatio` | `0x1d61a4e0` | 2nd-minor packing ratio (called by `ScatterLayoutIsValid`) | CONFIRMED |
| `(anon)::UpdateLayout(target, Layout, Shape&)` | `0x110f66a0` | Per-shape fixup: set Layout, then `Target::UpdateLayout` | CONFIRMED |
| `Target::UpdateLayout` | `0x1d618aa0` | Recompute tile/packing via `TransferSizeUtil::UpdateLayout` | CONFIRMED |
| `HardwareLayout::PopulateDefaultLayout` | `0x1d6da120` | Stamp tile + memory_space on leaf subshapes | CONFIRMED |
| `HardwareLayout::PopulateShape` | `0x1d6da360` | Tile-kind selection (Default/X64/X128) | MEDIUM |

---

## How Layout Feeds MSA and Scheduling

### Purpose

Layout assignment is the precondition for the entire back end. Two pieces of metadata it writes are consumed downstream: the `memory_space` integer embedded in each `xla::Layout`, and the tile geometry. Memory-space assignment (MSA) reads the former to know which physical tier a buffer lives in; the scheduler prices spill/refill against those tiers.

### The handoff

The `memory_space` integer routes physical placement. `MemorySpaceColorMap::UpdateFromLayout` (`0x110411a0`) walks every `Shape` in the module after layout assignment and reads `Layout::memory_space()` into a per-buffer color map; `BuildFromLayoutAndBackendConfig` (`0x11040d80`) additionally folds in `frontend_attribute`/`backend_config` memory-space strings. The color map is what the buffer allocator (MSA) consumes — see [msa-overview.md](msa-overview.md). The integer codes are produced by `MemorySpaceToColor`; the named spaces include `kHbm` (off-chip), `kVmem` (on-chip vector SRAM), `kCmem`/`kSmem` (scalar/sequencer SRAM), `kAlternate` (the secondary HBM pool MSA reserves), and the SparseCore/BarnaCore spaces.

Fusion (Phase 5) runs *after* layout assignment because fusion legality depends on physical tile layouts — see [fusion-patterns.md](fusion-patterns.md). The scheduler runs after MSA, in the [scheduling pipeline](../sched/overview.md), and prices each op in cycles against the assigned memory spaces; a layout that forces an HBM round-trip is more expensive than one that keeps a buffer in VMEM, which is precisely why minimising bytes (and thus tile padding / HBM footprint) at this stage matters to the eventual schedule.

> **NOTE —** the SparseCore has its own downstream layout pass, `xla::tpu::sparse_core::RunLayoutAssignment` (`0x12e02140`, src `sparse_core_layout_assignment.cc`), that runs in the post-layout pipeline over the SparseCore custom-call instructions (driven by `sparse_core_compiler_util`, stamping per-leaf `HardwareLayout::TileKind`). The TensorCore-side `TpuLayoutAssignment` documented here is what seeds the SparseCore-side `memory_space` tags (LOOP 1 / the copy→SparseCore path via `HasSparseCoreLayout`), so the two passes are coupled through the layout's memory space. (The named SparseCore memory spaces in this build are `kSparseCorePrivateStackHbm`, `kSparseCoreSequencerSmem`, `kSparseCoreSequencerSflag` — there is no flat `kSparseCoreMem` enumerator.)

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::LayoutAssignment` (OSS base, `0x169bf440`) | Owns the propagation worklist; TPU class overrides only backend hooks |
| `TpuLayoutAssignment::PreprocessModule` (`0x1109ff20`) | Pre-pass: inserts `kCopy` boundary nodes |
| `MemorySpaceColorMap` (`0x110411a0`) | Reads assigned `memory_space` for MSA |
| `xla::tpu::sparse_core::RunLayoutAssignment` (`0x12e02140`) | SparseCore-side layout, downstream, coupled via memory space |
| `TpuTilingRewriter::RewriteHostComputeWithLinearLayout` (`0x1112efc0`) | Post-layout: bridges tiled TensorCore layouts to host flat memory |

## Cross-References

- [compile-phases.md](compile-phases.md) — the ordered phase spine; layout assignment is the Phase 4 tail, after SPMD, before fusion
- [overview.md](overview.md) — Part V orientation; where layout assignment sits in the XLA-then-MLIR descent
- [hlo-pre-passes.md](hlo-pre-passes.md) — the pre-passes (incl. BatchNormExpander) that must run first; layout assignment FATALs on un-expanded batch-norm
- [fusion-patterns.md](fusion-patterns.md) — main fusion (Phase 5), which runs after layout because fusion legality depends on physical tile layouts
- [msa-overview.md](msa-overview.md) — memory-space assignment, which consumes the `memory_space` color map written here
- [mosaic-layout-inference.md](mosaic-layout-inference.md) — Mosaic/Pallas kernel layouts, parsed in `RunImpl` and pinned as seed constraints
- [../sched/overview.md](../sched/overview.md) — the scheduler that prices ops in cycles against the memory spaces this pass assigns
