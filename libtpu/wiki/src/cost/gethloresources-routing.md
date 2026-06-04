# GetHloResources Routing

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). Every symbol below is a demangled C++ name. `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. All addresses are VMA. Other versions differ.*

## Abstract

`CostModel::GetHloResourcesImpl` is the routing spine of the TPU bundle-occupancy cost model: given one HLO instruction, it decides *which* cost sub-emitter prices the op, and that sub-emitter deposits the op's throughput cycles into the 23-slot [`ResourceVector`](resource-enum.md). It is the resource-routing counterpart to the flop/byte side. The flop side ([TpuHloCostAnalysis](tpu-hlo-cost-analysis.md)) walks the same HLO and writes `float` properties (`+0x50` flops / `+0x54` transcendentals / `+0x58` bytes) consumed by the fusion-priority model; this page is the *other* surface — the per-op cycle deposit into a VLIW-bundle resource vector. The two share the HLO and agree on which ops are expensive, but they are computed by different functions, store into different structures, and never share a number.

Routing happens in three nested stages, and the page is organized around them. **Stage 1 — `GetHloResourcesImpl` (`@0x130aa580`)** is a five-way top-level dispatch: a collective goes to the network model; a conv-lowerable op (or a non-max-pool reduce-window) goes to the MXU output-fusion model; a collective-compute fusion goes to its own model; everything else falls to the loop/elementwise default. **Stage 2 — `RecordCyclesIfFused` (`@0x130cc720`)** is the fusion-peel router reached only from the default arm: for a fused region it peels convolution, reduce-window, loop-fusion and output-fusion roots to their dedicated emitters, dropping producer→consumer input-DMA edges as it goes, and falls through to the leaf emitter for every remaining op. **Stage 3 — `RecordHloCycles` (`@0x130bbfe0`)** is the leaf per-op deposit: a `switch` over the opcode, compiled to a `0x7f`-entry self-relative jump table at `.rodata 0xae0ebbc`, routes each opcode to the `CycleTable::Instruction` it issues and the named `Resource` slot it lands in.

The familiar reference frame is an LLVM `TargetTransformInfo` cost path where each opcode reaches a `getInstructionCost` arm, except that here the "cost" is a *resource occupancy* deposited into a structured bundle vector that a later `MaxResourceCycles` reduction folds with explicit overlap rules. This page documents the three dispatch layers, the predicates and masks that gate each arm, the leaf opcode→slot routing, and how each routed sub-model writes into `ResourceVector::Acc`. The flop-override table the [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) page owns is not duplicated here.

For reimplementation, the contract is:

- The five-way `GetHloResourcesImpl` dispatch: the collective predicate (with its opcode-61 collective-in-fusion peel), the numeric element-type bitmask gate, the conv/reduce-window selection with the max-pool sentinel, and the collective-compute-fusion vs loop/elementwise default.
- The `RecordCyclesIfFused` peel order — conv → reduce-window → loop-fusion → output-fusion → leaf — and the `IsProducerUse` edge-internalisation that runs before the per-leaf deposit.
- The `RecordHloCycles` opcode→slot jump table: the `idx = opcode − 3` index, the `ja > 0x7e → default` bound, and for every named block the `CycleTable::Instruction` issued and the `Resource` slot it lands in via `CycleTable::GetResource`.
- The leaf deposit mechanic: `slot = GetResource(CT)`, `thru = cycletable.vtable[+0x10](CT)`, `Acc(slot, element_count × thru × W)`.

| | |
|---|---|
| **Class** | `xla::jellyfish::CostModel` (`cost_model.cc`) |
| **Stage 1 — top dispatch** | `CostModel::GetHloResourcesImpl` `@0x130aa580` (5-way) |
| **Stage 2 — fusion peel** | `CostModel::RecordCyclesIfFused` `@0x130cc720` |
| **Stage 3 — leaf deposit** | `CostModel::RecordHloCycles` `@0x130bbfe0` |
| **Leaf jump table** | `.rodata 0xae0ebbc` (`0x7f` × i32 self-relative; `target = 0xae0ebbc + off`) |
| **Op→slot map** | `CycleTable::GetResource(Instruction)` `@0x1c89ce20` → `dword_B438AEC[CT]` |
| **Deposit primitive** | `ResourceVector::Acc(Resource, double)` `@0x1c89adc0` (bound `< 0x17`) |
| **Public entry** | `CostModel::GetHloResources` `@0x130aa560` (thin wrapper) |
| **Source file** | `platforms/xla/service/jellyfish/cost_model/cost_model.cc` |

---

## Stage 1 — `GetHloResourcesImpl`, the Five-Way Top Dispatch

### Purpose

`GetHloResourcesImpl` is the front door: it returns a `StatusOr<ResourceVector>` for one HLO op by selecting one of five pricing paths. It does **not** itself deposit cycles — each arm delegates to a sub-emitter that builds and fills the vector. The arms, in evaluation order: collective → `GetCollectiveCycles`; conv-lowerable / non-max-pool reduce-window → `GetOutputFusionOrConvolutionCycles`; collective-compute fusion → `GetCollectiveComputeFusionCycles`; default → `GetLoopFusionOrUnfusedHloCycles` (the only arm that reaches `RecordCyclesIfFused` / `RecordHloCycles`).

### Entry Point

```text
GetHloResources              @0x130aa560  ── public wrapper
  └─ GetHloResourcesImpl     @0x130aa580  ── StatusOr<ResourceVector>, 5-way
      ├─ IsSupportedCollectiveHlo  @0x130aeda0 ── arm 1 (+ opcode-61 fusion peel)
      │     → GetCollectiveCycles  @0x130abfc0     rv[+8] = scalar cycles
      ├─ IsFusionSupportedHlo      @0x130abee0 ── numeric-type + fusion-support gate
      ├─ IsConvLowerable           @0x14553620 ┐
      ├─ ExtractConvLikeHlo        @0x1d6aa140 ├ conv / reduce-window selection
      ├─ GetReduceWindowType       @0x1454d4a0 ┘  (type −1/2 max-pool → NOT conv-priced)
      │     → GetOutputFusionOrConvolutionCycles @0x130aede0   ── arm 2 (MXU)
      ├─ IsCollectiveComputeFusion @0x13e028c0
      │     → GetCollectiveComputeFusionCycles   @0x130b13a0   ── arm 3
      └─ (default)
            → GetLoopFusionOrUnfusedHloCycles    @0x130b2bc0   ── arm 4 → Stage 2
```

### Algorithm

```c
StatusOr<ResourceVector> GetHloResourcesImpl(inst, opts, fs, isFused):  // sub_130AA580
    op = *(byte*)(inst + 12);                       // HloInstruction::opcode

    // (1) COLLECTIVE — all-reduce / all-gather / … priced by the network path.
    if IsSupportedCollectiveHlo(inst):              // sub_130AEDA0
        return GetCollectiveCycles(inst, &rv);      // sub_130ABFC0 — rv[+8] = scalar cycles
    if op == 61 (fusion):                            // peel a collective wrapped in a fusion
        comp = inst.fused_instructions_computation();
        c    = first fused instr where IsSupportedCollectiveHlo(c) holds;
        if c found and !IsCollectiveComputeFusion(inst):
            return GetCollectiveCycles(c, &rv);     // price the inner collective

    // (2) NUMERIC ELEMENT-TYPE + FUSION-SUPPORT GATE
    et = inst.shape.element_type;                   // *(u32*)*(inst+88)
    if (et <= 0x21 and bit(0x2FFF91FFE, et))         // numeric/packed-type mask (bittest64)
       or (et & 0xFFFFFFFE) == 0x20                  // (the F8 pair 0x20/0x21)
       or (et <= 0x22 and bit(0x400048000, et)):     // the loop/output-fusion peel mask
        if IsFusionSupportedHlo(inst, opts.target):  // sub_130ABEE0
            // (3) CONV / REDUCE-WINDOW MAIN-OP SELECTION
            convlowerable = IsConvLowerable(inst);   // sub_14553620
            conv          = ExtractConvLikeHlo(inst);// sub_1D6AA140
            priced        = inst;
            if conv and conv.opcode == 94 (reduce-window):
                t = GetReduceWindowType(conv);       // sub_1454D4A0
                if t != -1 and t != 2:               // 0/1 (lane/sublane) → conv-priced
                    if convlowerable:
                        return GetOutputFusionOrConvolutionCycles(inst, opts, fs);  // sub_130AEDE0
                    priced = inst;                   // fall to collective-compute / default
            else if convlowerable:
                return GetOutputFusionOrConvolutionCycles(inst, opts, fs);

            // (4) COLLECTIVE-COMPUTE FUSION (collective fused with compute)
            if IsCollectiveComputeFusion(priced):    // sub_13E028C0
                return GetCollectiveComputeFusionCycles(priced, opts, fs);  // sub_130B13A0
            // (5) DEFAULT
            return GetLoopFusionOrUnfusedHloCycles(priced, opts, fs);       // sub_130B2BC0

    // non-numeric / unsupported types → empty ResourceVector (Ok, all-zero)
    return Ok(empty_rv);
```

### The Three Gates That Shape the Dispatch

**The numeric element-type gate (`@0x130aa9dc`).** Before any conv/fusion work, the dispatch tests `inst.shape.element_type` against `cmp et, 0x21` followed by `bt 0x2FFF91FFE` (the `movabs` at `@0x130aa9e2`). `0x2FFF91FFE` is a 34-bit mask over the `PrimitiveType` ordinal selecting the numeric/packed types the bundle model can price; a tuple/token/opaque type (outside the mask) falls straight through to an empty (all-zero) `ResourceVector`. Two secondary tests widen the gate: `(et & 0xFFFFFFFE) == 0x20` admits the F8 pair (ordinals `0x20`/`0x21`), and `bt 0x400048000` over `et ≤ 0x22` is the loop-fusion / output-fusion peel mask that lets a fusion root through even when its own element type is structural.

> **GOTCHA —** the same `0x2FFF91FFE` numeric mask and the same `0x400048000` peel mask reappear verbatim inside `RecordHloCycles` (`@0x130bc03d`). They are not the same decision: the copy in `GetHloResourcesImpl` gates *routing* (does this op reach the conv/fusion arms at all), while the copy in `RecordHloCycles` gates the *leaf deposit* (whether the op is recorded or short-circuited). A reimplementation must apply the mask at both layers; applying it only at the top lets structural ops slip into the leaf switch, and applying it only at the leaf mis-routes numeric ops at the top.

**The conv/reduce-window selection (`@0x130aaa79`).** `ExtractConvLikeHlo` pulls the conv-like root out of the (possibly fused) instruction. If that root is a `reduce-window` (opcode `94` = `0x5e`), `GetReduceWindowType` classifies its axis. The decompile branches on `t != -1 && t != 2`: a reduce-window whose type is `-1` (not conv-lowerable) or `2` (major-axis / max-pool) is **not** priced as a convolution — it falls through to the default loop path. Only a lane-axis (`0`) or sublane-axis (`1`) reduce-window that is also `IsConvLowerable` reaches `GetOutputFusionOrConvolutionCycles`.

> **NOTE —** the type `-1`/`2` sentinel here is the *dispatch* filter that skips MXU pricing; it is **not** the leaf reduce-window emitter. When a reduce-window *is* conv-priced it later reaches `RecordReduceWindowCycles`, where all three axis types (lane/sublane/major) deposit. The upstream skip-logic must not be carried into the leaf. The full pooling cost is on [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md); the conv state it shares is on [ConvolutionCostState](convolution-cost-state.md).

**The collective predicate and its fusion peel (`@0x130aa580`, lines following the first `IsSupportedCollectiveHlo`).** A bare collective is routed to `GetCollectiveCycles` immediately. When the op is a `fusion` (opcode `61` = `0x3d`), the dispatch walks the fused computation's instruction list looking for a collective HLO inside (`IsSupportedCollectiveHlo` over each fused instr) and, if found and the op is *not* a collective-compute fusion, prices that inner collective through `GetCollectiveCycles`. This is the one arm that re-points the priced op away from the top-level instruction.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `CostModel::GetHloResources` | `0x130aa560` | public wrapper | CERTAIN |
| `CostModel::GetHloResourcesImpl` | `0x130aa580` | 5-way routing dispatch | CERTAIN |
| `IsSupportedCollectiveHlo` | `0x130aeda0` | collective predicate (arm 1) | CERTAIN |
| `GetCollectiveCycles` | `0x130abfc0` | network-model deposit (`rv[+8]`) | CERTAIN |
| `IsFusionSupportedHlo` | `0x130abee0` | numeric + fusion-support gate | CERTAIN |
| `IsConvLowerable` | `0x14553620` | conv-lowerable predicate | CERTAIN |
| `ExtractConvLikeHlo` | `0x1d6aa140` | pull the conv/reduce-window root | CERTAIN |
| `GetReduceWindowType` | `0x1454d4a0` | `−1`/`2` max-pool sentinel (axis class) | CERTAIN |
| `GetOutputFusionOrConvolutionCycles` | `0x130aede0` | arm 2 — MXU output-fusion model | CERTAIN |
| `IsCollectiveComputeFusion` | `0x13e028c0` | arm-3 predicate | CERTAIN |
| `GetCollectiveComputeFusionCycles` | `0x130b13a0` | arm 3 | HIGH |
| `GetLoopFusionOrUnfusedHloCycles` | `0x130b2bc0` | default → Stage 2 | CERTAIN |

---

## Stage 2 — `RecordCyclesIfFused`, the Fusion-Peel Router

### Purpose

`GetLoopFusionOrUnfusedHloCycles` composes the software-pipelined prologue/steady/tail bundle cost (see [Bundle-Aware Cost](bundle-aware-cost.md)) and calls into the per-op deposit machinery. For a fused region that machinery is `RecordCyclesIfFused`: a router that recognizes the fusion root's *kind* and dispatches to the dedicated heavy emitter, peeling the structural fusions before any leaf op is priced. An unfused op reaches the leaf `RecordHloCycles` directly.

### Entry Point

```text
RecordCyclesIfFused (@0x130cc720)            ── fusion-root kind router
  ├─ IsLoopFusion?    → RecordLoopFusionCycles      @0x130b89a0  (recurse over fused leaves)
  ├─ IsConvLowerable? ── builds shared ConvCostState, then:
  │     ├─ IsOutputFusion? → RecordOutputFusionCycles  @0x130b86c0  (MXU output-fusion)
  │     ├─ opcode == 0x2b?  → RecordConvolutionCycles   @0x130ca6c0  (conv → Matmul/Matpush/Xlu)
  │     └─ (else, CHECK 0x5e) → RecordReduceWindowCycles @0x130c94e0 (pooling → VectorLoad/AluAny/Xlu)
  └─ (not conv-lowerable) → RecordHloCycles         @0x130bbfe0  (leaf per-op deposit)
```

### Algorithm

```c
Status RecordCyclesIfFused(inst, fs, window, rv, isFused, nesting):  // sub_130CC720
    if IsLoopFusion(inst):                              // line 188
        return RecordLoopFusionCycles(inst, window, rv, …, nesting+1);  // sub_130B89A0
    if IsConvLowerable(inst):                           // line 256 — gates the window arms
        convCostState = GetConvolutionCostState(inst);  // + CalculateNestedConvolutionWindows
        if IsOutputFusion(inst):                        // line 434
            return RecordOutputFusionCycles(inst, &convCostState, …, nesting+1);  // sub_130B86C0
        if inst.opcode == 0x2b (convolution):           // line 513
            return RecordConvolutionCycles(inst, &convCostState, rv, nesting+1);  // sub_130CA6C0
        // else: CHECK(opcode == kReduceWindow) @line 526 (cost_model.cc:6236)
        return RecordReduceWindowCycles(inst, …);                      // sub_130C94E0 — line 536
    // not conv-lowerable: drop internal producer edges, then the leaf deposit
    return RecordHloCycles(inst, window, rv, fs, nesting+1);           // sub_130BBFE0 — line 706
```

The peel order is fixed and confirmed at the five call sites. `IsLoopFusion` is tested first (it recurses over its constituent leaves). The remaining heavy arms — output-fusion, then conv (`0x2b`), then reduce-window (`0x5e`) — are nested inside a single `IsConvLowerable(inst)` gate (line 256): when that gate holds, the router materializes a `ConvCostState` (via `GetConvolutionCostState` / `CalculateNestedConvolutionWindows`) shared by all three window arms, dispatched in that order. Only a non-conv-lowerable, non-loop-fusion op reaches the leaf fallthrough. The reduce-window arm is the `else` of the conv test and guards itself with `CHECK(hlo_to_fuse->opcode() == kReduceWindow)` (`MakeCheckOpString` @line 526, `cost_model.cc:6236`), so a mis-classified peel aborts rather than mis-prices.

> **NOTE —** the leaf `RecordHloCycles` itself `CHECK`s `!hlo->IsLoopFusion() && !hlo->IsOutputFusion()` (`cost_model.cc:6609`, `@0x130bbfe0` line 128). A fusion root must never reach the leaf — it is `RecordCyclesIfFused`'s job to peel it first. A reimplementation that calls the leaf on a fusion root trips this assertion. The peel router is the only legal path from a fusion to a deposit.

### Edge Internalisation — `IsProducerUse`

Before the leaf deposit of a fused `parameter`, `RecordHloCycles` walks the fusion's operands and consults `IsProducerUse` (`@0x130ab0c0`) per operand edge. A producer→consumer edge that is internal to the fusion has its input-DMA *dropped* — the bytes never leave the fusion, so they cost no `MemXfer` cycles. This is the bundle-side mirror of the fusion-priority model's edge internalisation: only the fusion's *external* inputs are charged as input transfers. The walk runs in the `parameter` arm of the leaf switch (below), not in `RecordCyclesIfFused`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `CostModel::RecordCyclesIfFused` | `0x130cc720` | fusion-root kind router | CERTAIN |
| `CostModel::RecordLoopFusionCycles` | `0x130b89a0` | loop-fusion recursion | CERTAIN |
| `CostModel::RecordOutputFusionCycles` | `0x130b86c0` | output-fusion (MXU) | CERTAIN |
| `CostModel::RecordConvolutionCycles` | `0x130ca6c0` | conv peel → MXU deposit | CERTAIN |
| `CostModel::RecordReduceWindowCycles` | `0x130c94e0` | reduce-window peel → vector deposit | CERTAIN |
| `CostModel::RecordHloCycles` | `0x130bbfe0` | leaf per-op deposit | CERTAIN |
| `CostModel::IsProducerUse` | `0x130ab0c0` | producer→consumer edge-drop | HIGH |
| `CostModel::RecordFusionInputCycles` | `0x130ce940` | external-input DMA → `MemXfer` | HIGH |

---

## Stage 3 — `RecordHloCycles`, the Leaf Opcode→Slot Routing

### Purpose

`RecordHloCycles` is the leaf per-op deposit: given one HLO instruction, the bundle `Window`, the output `ResourceVector`, and a nesting depth, it deposits that op's throughput cycles into the named `Resource` slot(s) the op occupies. The op→block decision is a `switch` over the opcode byte (`*(byte*)(a2+12)`), compiled to a `0x7f`-entry self-relative jump table at `.rodata 0xae0ebbc`. The dispatch index is `idx = opcode − 3` (`add eax, 0xFFFFFFFD` at `@0x130bc0db`), and `ja > 0x7e` routes to the default block.

### Entry Point

```text
RecordHloCycles (@0x130bbfe0)
  ├─ CHECK(!IsLoopFusion && !IsOutputFusion)      cost_model.cc:6609
  ├─ numeric-mask + IsLoopFusion/IsOutputFusion peel pre-check   @0x130bc03d
  ├─ ElementIsFloating(shape)                     ── the add/subtract lane gate
  ├─ element_count = Product(output dims)         ── xla::Product → [rbp-0x30]
  ├─ RecordHloCyclesIfTopLevel  @0x130cdd80        ── == 1 guard, else skip the switch
  └─ switch(opcode) → jump table @ .rodata 0xae0ebbc
```

### The Deposit Mechanic

Every named block resolves a slot and a per-gen throughput, then accumulates:

```c
// per CT::Instruction k issued by the block:
slot = CycleTable::GetResource(k);                  // sub_1C89CE20 = dword_B438AEC[k]
thru = cycletable.vtable[+0x10](k);                 // GetCyclesForThroughput(k), per-gen
ResourceVector::Acc(slot, element_count × thru × W);// sub_1C89ADC0, W = fixed FP multiplier
```

`element_count` is `Product(output dims)`. `GetResource` is a single gen-invariant flat lookup — `dword_B438AEC[k]` (4 B/entry, `.rodata 0xb438aec`) — re-decoded below and identical to the table on [Resource Enum](resource-enum.md#opslot-mapping--cycletablegetresource). `GetCyclesForThroughput` is the per-gen integer ([Per-Opcode Cycle Constants](per-opcode-cycle-constants.md)). `Acc` (`@0x1c89adc0`) bounds-checks `slot < 0x17` (23) with a trapping `ud1` and does `vector[slot] += cycles`.

> **QUIRK —** for a *float* `add`/`subtract` the block resolves the slot via `GetResource(CT)` (→ `VectorAlu1`), but for an *integer* `add`/`subtract` the slot is hard-coded to `5` (`VectorAluAny`) while the throughput is still `thru(CT 0x12)` / `thru(CT 0x13)`. The slot and the throughput are decoupled: the float gate redirects only the *destination lane*, not the *cycle count*. A reimplementation that gates the throughput on the type (rather than the lane) mis-prices integer arithmetic.

### Algorithm

```c
RecordHloCycles(inst, window, rv, fs, nesting):       // sub_130BBFE0
    CHECK(!inst.IsLoopFusion() && !inst.IsOutputFusion());     // line 128
    isFloat = ShapeUtil::ElementIsFloating(inst.shape);        // line 139
    elems   = Product(output_dims);                            // line 145 → [rbp-0x30]
    if RecordHloCyclesIfTopLevel(...) != 1: return status;     // line 147 (fusion-root guard)

    switch (*(byte*)(inst + 12)):                     // jump table @ .rodata 0xae0ebbc
      case 0x03 add:        slot = isFloat ? GetResource(CT 0x12) : 5;   // R4 / R5
                            Acc(slot, elems × thru(CT 0x12));
      case 0x7b subtract:   slot = isFloat ? GetResource(CT 0x13) : 5;   // R4 / R5
                            Acc(slot, elems × thru(CT 0x13));
      case 0x4b multiply:   Acc(GetResource(CT 0x14) /*R3*/, elems × thru(CT 0x14));
      case 0x32 divide:     /* 4-deposit reciprocal+mul micro-sequence */
            Acc(6 /*R6 VectorEup*/,        elems × thru(CT 0x18));
            Acc(GetResource(CT 0x14) /*R3*/, (elems × thru(CT 0x14)) × 3.0);   // 0xa2df930
            Acc(GetResource(CT 0x12) /*R4*/, (elems×2) × thru(CT 0x12));
            Acc(5 /*R5*/,                  elems × 9.0);                       // 0xa2deb40
      case 0x47 logistic:   /* 5-deposit sigmoid micro-sequence */
            Acc(GetResource(CT 0x12) /*R4*/, elems × thru(CT 0x12));
            Acc(GetResource(CT 0x14) /*…*/, (elems×2) × thru(CT 0x14));
            Acc(5 /*R5*/, elems);
            Acc(6 /*R6 VectorEup*/, … via CT 0x1a);
      case 0x38 erf:        if (inst.vtable[+312](11)):          // ext-precision predicate
                                Acc(6 /*R6 VectorEup*/, elems × thru(CT 0x11));  // single deposit
                            else: /* 4-deposit polynomial */
                                Acc(6 /*R6*/,                  elems × thru(CT 0x18));
                                Acc(GetResource(CT 0x14) /*R3*/, (elems × thru(CT 0x14)) × 16.0); // 0xa2df040
                                Acc(GetResource(CT 0x12) /*R4*/, (elems×2) × thru(CT 0x12));
                                Acc(5 /*R5*/,                  elems × 4.0);     // 0xa2de830
      case 0x2a convert:    if ElementHasBitWidth(inst.shape, 1):  // 1-bit PRED
                                Acc(5, elems × 2.0);               // packed-bool repack
                            else: /* fall to ZERO — numeric width-conversion free */
      case 0x6c select:     Acc(5, elems × 2.0);                   // read both branches + pred
      case 0x52 parameter:  if !IsFused(inst): /* not deposited here */
                            else: walk operands, IsProducerUse edge-drop,
                                  RecordCyclesIfFused per producer, RecordFusionInputCycles → MemXfer;
      case 0x5b reduce:     if IsFused(inst): Acc(5, elems);
                            else: w = GetInputWindow(inst); Acc(5, Product(w));  // operand window
      case 0x18,0x1a,0x27,0x29,0x43,0x61,0x81: /* ZERO — no deposit */
      default:              Acc(5 /*R5 VectorAluAny*/, elems × 1.0);
```

### The Decoded Jump Table

Block addresses and the jump-table target offsets are byte-verified from `.rodata 0xae0ebbc` (`target = 0xae0ebbc + offset[opcode−3]`). The full µop-sequence detail for `divide`/`logistic`/`erf` (the FP-multiplier scaling, the slow-vs-fast erf gate) is documented on [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md#recordhlocycles--the-opcodeslot-jump-table); this table is the routing-spine view — which block, which CT, which slot.

| Opcode (name) | Block @ | CT issued → slot | Cycle quantity | Confidence |
|---|---|---|---|---|
| `0x03` add | `0x130bc0f7` | float: `CT 0x12`→`R4`; int: `R5` | `elems × thru(0x12)` | CERTAIN |
| `0x7b` subtract | `0x130bc4af` | float: `CT 0x13`→`R4`; int: `R5` | `elems × thru(0x13)` | CERTAIN |
| `0x4b` multiply | `0x130bc4dd` | `CT 0x14`→`R3` | `elems × thru(0x14)` | CERTAIN |
| `0x32` divide | `0x130bc3cf` | `R6` + `CT 0x14`→`R3` + `CT 0x12`→`R4` + `R5` | 4-deposit; `×3.0`(R3), `×2`(R4 elems), `×9.0`(R5) | CERTAIN |
| `0x47` logistic | `0x130bc17b` | `CT 0x12`→`R4`, `CT 0x14`, `R5`, `CT 0x1a`→`R6` | 5-deposit sigmoid; `×2`(R-elems) | HIGH |
| `0x38` erf | `0x130bc245` | fast: `CT 0x11`→`R6`; slow: `R6`+`R3`+`R4`+`R5` | gated; slow `×16.0`(R3), `×2`(R4), `×4.0`(R5) | HIGH |
| `0x2a` convert | `0x130bc293` | 1-bit: `R5`; wider: none | 1-bit: `elems × 2.0`; wider: 0 | CERTAIN |
| `0x6c` select | `0x130bc2a9` | `R5` (`×2` path) | `elems × 2.0` | CERTAIN |
| `0x52` parameter | `0x130bc2b8` | fused→`RecordFusionInputCycles`; else none | fused: input-DMA into `MemXfer` `R9..12` | MEDIUM |
| `0x5b` reduce | `0x130bc2f9` | `R5` over operand window | priced over the reduced-OVER input window | HIGH |
| DEFAULT (most ops) | `0x130bc3c0` | `R5` `VectorAluAny` | `elems × 1.0` | CERTAIN |
| ZERO-cost | `0x130bc8c2` | (no deposit) | `0` | CERTAIN |

The DEFAULT arm covers every numeric elementwise / structural op not named above (the decompiler annotates it `cases 4-23, 25, 27-38, 40, 43-49, 51-55, 57-66, 68-70, 72-74, 76-81, 83-90, 92-96, 98-107, 109-122, 124-128`). The ZERO arm covers the data-layout ops: `bitcast` (`0x18`), `broadcast` (`0x1a`), `concatenate` (`0x27`), `constant` (`0x29`), `iota` (`0x43`), `reshape` (`0x61`), `tuple` (`0x81`) — all at the single block `0x130bc8c2`.

> **NOTE —** The `erf` fast path carries no `Matpush` traffic: the ext-precision predicate `inst.vtable[+312](11)` (`@0x130bc245`) selects a single `VectorEup` deposit at `CT 0x11`, which `GetResource` maps to `R[6]` (`dword_B438AEC[0x11] == 6`, byte-verified). It stays entirely on the vector/EUP pipes — see [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md).

### The Op→Slot Table — `dword_B438AEC`

`CycleTable::GetResource(k)` (`@0x1c89ce20`) is literally `return dword_B438AEC[k]`. The entries the leaf blocks issue, re-decoded from the binary:

| `CT::Instruction` | `dword_B438AEC[CT]` → slot | Issued by | Confidence |
|---|---|---|---|
| `0x11` | `R[6]` `VectorEup` | erf fast path | CERTAIN |
| `0x12` | `R[4]` `VectorAlu1` | float add, divide, logistic | CERTAIN |
| `0x13` | `R[4]` `VectorAlu1` | float subtract | CERTAIN |
| `0x14` | `R[3]` `VectorAlu0` | multiply, divide, logistic, erf-slow | CERTAIN |
| `0x18` | `R[6]` `VectorEup` | divide / erf-slow transpose stage | CERTAIN |
| `0x1a` | `R[6]` `VectorEup` | logistic lane-compare | CERTAIN |

> **QUIRK —** `CT 0x12` and `CT 0x13` both map to the *same* slot `R[4]` `VectorAlu1`, even though they are distinct `CycleTable::Instruction` ordinals (input-rotate vs output-rotate) with distinct per-gen throughputs. So float add and float subtract share a dedicated lane but may not share a cycle count. Multiply pins the *other* dedicated lane (`R[3]` via `CT 0x14`). This is the deliberate port-balancing input to `MaxResourceCycles` — see Considerations.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `CostModel::RecordHloCycles` | `0x130bbfe0` | leaf per-op deposit + opcode switch | CERTAIN |
| `CostModel::RecordHloCyclesIfTopLevel` | `0x130cdd80` | `== 1` fusion-root guard | HIGH |
| `CycleTable::GetResource` | `0x1c89ce20` | `dword_B438AEC[CT]` (gen-invariant) | CERTAIN |
| `ResourceVector::Acc` | `0x1c89adc0` | `vector[slot] += cycles` (bound `< 23`) | CERTAIN |
| `ShapeUtil::ElementIsFloating` | (call @line 139) | add/subtract lane gate | CERTAIN |
| `ShapeUtil::ElementHasBitWidth` | `0x20ce1580` | 1-bit PRED convert gate | CERTAIN |
| `xla::Product` | `0x20cf5200` | `element_count` = Product(output dims) | CERTAIN |
| `GetInputWindow` | (anon-ns) | non-fused reduce operand window | HIGH |

### Fixed FP Multipliers (`.rodata`, byte-verified)

| Address | Value | Used by | Confidence |
|---|---|---|---|
| `0xa2df230` | `1.0` | default per-op multiplier | CERTAIN |
| `0xa2df930` | `3.0` | divide `VectorAlu0` (R3) scale | CERTAIN |
| `0xa2deb40` | `9.0` | divide `VectorAluAny` (R5) scale | CERTAIN |
| `0xa2df040` | `16.0` | erf slow-path `VectorAlu0` (R3) scale | CERTAIN |
| `0xa2de830` | `4.0` | erf slow-path `VectorAluAny` (R5) scale | CERTAIN |
| `0xa2df5c8` | `0.5` | `MaxResourceCycles` `VectorAlu` port-balance blend | CERTAIN |

---

## Considerations — How the Routing Feeds the Reduction

The leaf op→slot routing across the three `VectorAlu` slots is the port-balancing input to the `MaxResourceCycles` 0.5-blend group `{R3, R4, R5}` (see [Resource Enum](resource-enum.md#the-maxresourcecycles-reduction)). The routing is deliberate: multiply pins `VectorAlu0` (`R3`), floating add/subtract pin `VectorAlu1` (`R4`), and the catch-all DEFAULT plus the `×2` ops (`select`, 1-bit `convert`) plus integer add/subtract land on `VectorAluAny` (`R5`), the lane the blend redistributes. A fusion mixing multiplies, floating adds, and "any" ops therefore fills both dedicated lanes and the bundle cost is the balanced max, not the serial sum.

The heavy emitters reached by Stages 1 and 2 deposit into *other* slot groups. `RecordConvolutionCycles` (conv peel) deposits into `Matmul` / `Matpush` / `Xlu` (`R0`/`R1`/`R2`, the plain-MAX MXU group — see [ConvolutionCostState](convolution-cost-state.md)); `RecordReduceWindowCycles` (pooling) deposits into `VectorLoad` / `VectorAluAny` / `Xlu` (`R7`/`R5`/`R2` — see [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md)); `GetCollectiveCycles` writes a scalar cycle count into `rv[+8]` for the ICI links. Each routing arm therefore lands on the slot group whose reduction rule matches the op's hardware behavior — MXU pipes overlap (plain MAX), vector ALUs port-balance (0.5 blend), memory transfers serialize (sum).

`parameter` and `reduce` route *out* of the ALU lanes. A fused `parameter` is an external-input DMA priced by `RecordFusionInputCycles` (`@0x130ce940`) into the `MemXfer` slots `R9..12`, after `IsProducerUse` has dropped the internal producer edges; whether VMEM-resident params instead route to `R7` `VectorLoad` per memory space is not byte-confirmed (MEDIUM). A non-fused `reduce` is priced over its *operand* window via `GetInputWindow` — cost scales with the large reduced-over tensor, not the small output — the bundle-side mirror of `HandleReduce`'s `ExtentProduct(operand)` flop formula.

> **QUIRK —** the bundle model prices a `broadcast` (`0x1a`) at **zero** in the leaf (the ZERO-cost block), even though the fusion-priority model may charge a cross-lane-movement weight for the same op. The two surfaces disagree on layout ops by design: the bundle path treats a broadcast as a free register splat (no functional-unit occupancy), while the priority model accounts for the data movement. Do not unify them. See [NormalizedComputationCost](normalized-computation-cost.md).

---

## Worked Example — Routing One Fusion Through All Three Stages

A loop-fusion body `{ multiply [256,128] (f32), add [256,128] (f32), tanh [256,128] }` is priced top to bottom:

```text
GetHloResourcesImpl(fusion)                         Stage 1
  IsSupportedCollectiveHlo → no
  et = F32 (in 0x2FFF91FFE mask) → numeric gate OK
  IsFusionSupportedHlo → yes; ExtractConvLikeHlo → null (no conv)
  IsConvLowerable → no; IsCollectiveComputeFusion → no
  → GetLoopFusionOrUnfusedHloCycles                 (default arm)

GetLoopFusionOrUnfusedHloCycles → RecordCyclesIfFused(fusion)   Stage 2
  IsLoopFusion → yes → RecordLoopFusionCycles → recurse over leaves:
    each leaf → RecordCyclesIfFused → (not a fusion/conv/rw) → RecordHloCycles

RecordHloCycles per leaf, element_count = 256·128 = 32768       Stage 3
  multiply (0x4b)  → CT 0x14 → GetResource → R3 VectorAlu0:  Acc(R3, 32768 × thru(0x14))
  add (0x03), f32  → CT 0x12 → GetResource → R4 VectorAlu1:  Acc(R4, 32768 × thru(0x12))
  tanh (0x7d)      → DEFAULT block →           R5 VectorAluAny: Acc(R5, 32768 × 1.0)
```

`MaxResourceCycles`' 0.5-blend on `{R3, R4, R5}` runs the multiply (`VectorAlu0`) and add (`VectorAlu1`) lanes in parallel, load-balances the `tanh` "any" work into the less-busy lane, and overlaps the residual at 50% — so the bundle cost is `≈ max(R3, R4) + half the tanh residual`, not the serial sum. Had `add` been integer-typed, it would have joined `tanh` on `R5` (the slot hard-coded to `5` when `ElementIsFloating` is false) rather than pinning `R4`. The flop side (run by a separate pass) classifies `tanh` as transcendental (`Properties+0x54`) and `multiply`/`add` as flops (`Properties+0x50`); the two surfaces agree on which ops are expensive but never share a number.

---

## Related Components

| Component | Relationship |
|---|---|
| [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) | the flop/byte override surface that runs alongside this routing; owns the full `RecordHloCycles` µop-sequence detail and the flop-override table |
| [Resource Enum (23-slot)](resource-enum.md) | the `ResourceVector` slots, `Acc`, `GetResource`, and the `MaxResourceCycles` reduction the deposits feed |
| [ConvolutionCostState](convolution-cost-state.md) | the conv peel emitter (`RecordConvolutionCycles`) reached from Stages 1/2 |
| [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md) | the reduce-window peel emitter and the `GetReduceWindowType` axis classifier |
| [Bundle-Aware Cost](bundle-aware-cost.md) | the software-pipelined loop composition over the per-op deposits |
| [NormalizedComputationCost](normalized-computation-cost.md) | the fusion-priority surface that prices the same HLO differently |

## Cross-References

- [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) — the override surface that feeds into this routing spine; the flop/byte `Properties` model and the per-op µop-sequence breakdown
- [Resource Enum (23-slot)](resource-enum.md) — the `Resource` slot names, `Acc`, `GetResource` op→slot table, and the `MaxResourceCycles` overlap model
- [ConvolutionCostState](convolution-cost-state.md) — `RecordConvolutionCycles` / `RecordConvKernelCycles`, the MXU deposit reached from the conv arm
- [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md) — `RecordReduceWindowCycles` and the lane/sublane/major axis dispatch
- [WindowDescription Byte-Cost](window-description-cost.md) — the conv/DMA byte+throughput primitive behind the heavy emitters
- [NormalizedComputationCost](normalized-computation-cost.md) — the opcode→weight fusion-priority model that prices layout ops differently from the bundle path
- [Bundle-Aware Cost](bundle-aware-cost.md) — `GetLoopFusionOrUnfusedHloCycles` and the prologue/steady/tail loop cost over the per-op deposits
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the per-gen `GetCyclesForThroughput(CT::Instruction)` integers each deposit multiplies by
- [Cost Model Overview](overview.md) — where this routing sits among the cost-model class families
