# OneSlot Scalar Router

> *Every address, opcode value, slot-flag bit, and jump-table bound on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`) — from the decompiled `ConsumeOneSlotInstruction` body, its callee tails, and the `.rodata` jump table. Other versions differ.*

## Abstract

`ConsumeOneSlotInstruction` is the SparseCore SC-MLO emitter's **scalar-slot router**: given one decoded `MCInst` whose opcode names a scalar (non-vector) SC operation, it decides *which physical issue slot* of the bundle the op occupies — `Stream`, `ScalarMisc`, `ScalarAlu` (and its dual sub-slots `S0`/`S1`), or `DMA` — and then tail-calls the matching `Consume<Slot>Instruction` leaf to lower the op into that slot's proto field. It is **not** a per-op encoder; it is the dispatch seam one level above the per-slot consumers, the SparseCore analog of an LLVM `MCInst` → functional-unit binding pass. Where a TableGen-driven backend would carry the slot as an itinerary class on the instruction, libtpu carries it two ways at once: a 4019-entry jump table maps the opcode to a slot *class*, and for the ops that can issue on more than one scalar slot, a per-`MCInst` flag word (`getSlotFlagsFromMCInst`, `MCInst+0x4`) picks the concrete sub-slot at emit time.

The router is **bundle-invariant**. The three SC bundle types that have a scalar region — [SCS](scs-engine.md), [TAC](tac-engine.md), and the scalar half of the [TEC](tec-engine.md) — instantiate `ConsumeOneSlotInstruction<Bundle>` from one template, and all three share an identical opcode→arm distribution: same jump-table base (`opcode − 0x1f3`), same bound (`0xfb2` = 4019), same ten arms with the same op-counts. Only the bundle template argument on the slot accessors differs (`GetStreamSlot<…,SparseCoreScsBundle>` vs `…TacBundle` vs `…TecBundle`). A reimplementer writes the router once and parameterizes it on the bundle.

This page documents the router's classify-by-opcode logic (the ten arms and their slot classes), the per-`MCInst` slot-flag sub-routing for the multi-slot ops, the three special arms (DMA materialization, optional-skip, no-op), the default-error path, and — because the page's job is to show how a single bundle slot reaches its sub-encoder — how the *vector* slot dispatch is a **separate** mechanism: the TEC bundle's vector slots are routed by `ConsumeOneTecBundleInstruction`, which reaches the 142-op `VectorAlu` table through `ConsumeVectorAluInstruction`. The vector opcode roster itself is owned by the [TEC Vector Opcode Enumeration](vector-opcode-enum.md) page and is linked, not duplicated, here.

For reimplementation, the contract is:

- **The classify table: `opcode − 0x1f3`, bound `0xfb2`, ten arms.** Read `MCInst+0x4` into the slot-flags word and the opcode `DWORD[MCInst]`; index the jump table at `0xae8dce4` (TAC); dispatch to one of ten arms. Each non-special arm selects a slot class, calls `Get<Slot>Slot`, `EmitPredicationToSlot<…>`, and tail-calls `Consume<Slot>Instruction`.
- **The slot-flag sub-routing for multi-slot ops.** The 54-opcode multi-scalar arm has *no* fixed slot; it tests the slot-flag bits (`flags & 1 → S0`, `& 2 → S1`, `& 4 → ScalarMisc`) and builds a `std::variant` value-visitor over `{SparseCoreScalarAlu*, SparseCoreScalarMisc*}`. No bit set is a hard error.
- **The DMA, optional-skip, and no-op arms.** The 70-opcode DMA arm materializes a `SparseCoreDma` into the bundle's `scalar_instruction` oneof under a `LogFatal`-guarded precondition, then visits a `{SparseCoreDma*, SparseCoreTecDma*}` variant. Four opcodes are silently skipped when the consumer's `bool` argument is set; one opcode (`0x264`) returns OK with no emission.
- **The vector path is separate.** `ConsumeOneSlotInstruction` handles only scalar slots; the TEC vector slots (`VectorAlu`/`VectorLoad`/`VectorStore`/`VectorExtended`) are dispatched by `ConsumeOneTecBundleInstruction`, and `VectorAlu` reaches its 142-op table via `ConsumeVectorAluInstruction` (jt base `0xb26`).

| | |
|---|---|
| **Router** | `ConsumeOneSlotInstruction<Bundle>` (per-bundle scalar-slot dispatcher) |
| **TAC entry** | `0x139f1360`; jt `0xae8dce4`, base `0x1f3`, bound `0xfb2` (4019 entries) |
| **SCS / TEC entries** | `0x13a50540` (jt `0xaea9fb4`) · `0x13a15500` (jt `0xaea4ba0`) — identical arm map |
| **Classify input** | `DWORD[MCInst]` (opcode) + `getSlotFlagsFromMCInst` (`MCInst+0x4`, slot-flag bits) |
| **Arms** | 10: Stream 888 · ScalarMisc 92 · ScalarAlu 49 · …S1 27 · …S0 17 · DMA 70 · multi-scalar 54 · skip 4 · no-op 1 · default 2817 |
| **Slot-flag bits** | `SLOT_S0 = 1` · `SLOT_S1 = 2` · `SLOT_SM = 4` (`llvm::TPU::SparseCoreMCSlot`) |
| **Vector path** | `ConsumeOneTecBundleInstruction` `0x13a08e00` → `ConsumeVectorAluInstruction` `0x13a0b580` (separate) |
| **Source** | `platforms/xla/sparse_core/ghostlite/isa_emitter.cc` |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — "OneSlot" means "one scalar issue slot," not "one instruction."** The name is the router's job: take one `MCInst`, place it into exactly one of the bundle's scalar slots. It is the scalar peer of the vector dispatcher. The 64-byte bundle layout, the scalar slot byte/bit bases (`Misc @111`, `Alu1 @138`, `Alu0 @165`), and the dual-issue S0/S1 geometry live on the [TEC Engine](tec-engine.md) and [SCS Engine](scs-engine.md) pages and are not repeated here.

---

## The Classify Logic

### Purpose

The router answers one question per `MCInst`: *which scalar slot does this op issue on?* The SC scalar opcode space is partitioned into contiguous-ish runs by slot class — a long Stream block, a ScalarMisc block, a ScalarAlu block with interleaved S0/S1 dual-issue runs — and the router is the table that decodes that partition. Slot assignment is the second half of SC instruction placement: the *engine* (which sequencer: SCS/TAC/TEC) is chosen upstream by the section-classifier; the *slot within the bundle* is chosen here.

### Entry Point

```text
ConsumeOneTecBundleInstruction (0x13a08e00)        ── per-MCInst TEC bundle dispatcher
  ├─ ConsumeOneSlotInstruction<Bundle> (0x139f1360 TAC)   ── THIS router: scalar slots
  │    ├─ GetStreamSlot       → ConsumeStreamInstruction        (0x139fa940)
  │    ├─ GetScalarMiscSlot   → ConsumeScalarMiscInstruction    (0x139eeca0)
  │    ├─ GetScalarAluSlot    → ConsumeScalarAluInstruction     (0x139f09c0)
  │    ├─ GetScalarAluSlotS0  → ConsumeScalarAluSlotS0Instruction (0x139f9480)
  │    ├─ GetScalarAluSlotS1  → ConsumeScalarAluSlotS1Instruction (0x139f9be0)
  │    └─ DefaultConstruct<SparseCoreDma> → variant{Dma,TecDma}  (0x13a04820)
  └─ ConsumeVectorAluInstruction (0x13a0b580)        ── separate: VectorAlu (142 ops)
```

### Algorithm

```c
// ConsumeOneSlotInstruction<SparseCoreTacBundle>   // glc 0x139f1360
//   args: (printer, mcinst, &bundle, bool tolerate_skip)
function ConsumeOneSlotInstruction(printer, mcinst, bundle, tolerate_skip):
    flags  = getSlotFlagsFromMCInst(mcinst)          // 0x13c798e0 → *(u32*)(mcinst+0x4)
    opcode = mcinst.opcode                            // DWORD[mcinst]
    idx    = opcode - 0x1f3                           // jt base 0x1f3
    if (unsigned)idx > 0xfb2:                          // bound check (4019 entries)
        goto DEFAULT
    switch jt[idx]:                                   // jt @0xae8dce4 (TAC), 4019×int32 rel

      MULTI_SCALAR:                                   // 54 opcodes, slot chosen by flags
        if flags & 1:   slot = GetScalarAluSlotS0(flags, bundle); vidx = 0   // SparseCoreScalarAlu
        elif flags & 2: slot = GetScalarAluSlotS1(flags, bundle); vidx = 0
        elif flags & 4: slot = GetScalarMiscSlot(flags, bundle);  vidx = 1   // SparseCoreScalarMisc
        else:           return Error("Invalid slot. Expected Scalar Slot. "  // line 5882
                                     "MCInst Flags: $0", flags)
        return variant_visit[vidx](slot, mcinst)      // {ScalarAlu*, ScalarMisc*} value-visitor

      SCALAR_ALU_S0:                                  // 17 opcodes, fixed S0
        slot = GetScalarAluSlotS0(flags, bundle)
        if EmitPredicationToSlot<…ScalarAlu>(mcinst, slot) != OK: return log(5969)
        return ConsumeScalarAluSlotS0Instruction(printer, …)

      SCALAR_ALU_S1:                                  // 27 opcodes, fixed S1
        slot = GetScalarAluSlotS1(flags, bundle)
        if EmitPredicationToSlot<…ScalarAlu>(mcinst, slot) != OK: return log(6003)
        return ConsumeScalarAluSlotS1Instruction(printer, …)

      SCALAR_ALU:                                     // 49 opcodes, generic ScalarAlu
        slot = GetScalarAluSlot(flags, bundle)        // returns StatusOr
        if EmitPredicationToSlot<…ScalarAlu>(mcinst, slot) != OK: return log(5945)
        return ConsumeScalarAluInstruction(printer, …, bundle)

      STREAM:                                         // 888 opcodes (DMA-stream descriptors)
        slot = GetStreamSlot(flags, bundle)
        if EmitPredicationToSlot<…Stream>(mcinst, slot) != OK: return log(7288)
        return ConsumeStreamInstruction(printer)

      SCALAR_MISC:                                    // 92 opcodes (sync/atomic/barrier/watch)
        slot = GetScalarMiscSlot(flags, bundle)
        if EmitPredicationToSlot<…ScalarMisc>(mcinst, slot) != OK: return log(6102)
        return ConsumeScalarMiscInstruction(printer, …, slot, bundle)

      DMA:                                            // 70 opcodes 0xfa1..0x1024 — see below
        ...materialize SparseCoreDma into scalar_instruction oneof...

      OPTIONAL_SKIP:                                  // 4 opcodes 0x100d/0x100e/0x1015/0x10f2
        if !tolerate_skip: goto DEFAULT
        return OK                                      // silently drop

      NO_OP:    return OK                              // opcode 0x264 only

      DEFAULT:                                         // 2817 opcodes (and OOB)
        return Error("Unsupported opcode while consuming slot instruction: "
                     "$0 : $1", opcode, getOpcodeName(opcode))   // line 7307
```

The decompile renders the jump table as a C `switch`, but the prologue is a true indirect jump — `lea ecx,[r12-0x1f3]; cmp ecx,0xfb2; ja default; movsxd rcx,[rdx+rcx*4]; add rcx,rdx; jmp rcx` — so the 4019 entries are signed 32-bit relative offsets into ten arm targets, exactly the dimension-table shape below. The `EmitPredicationToSlot<…>` call on every non-special arm stamps the op's predicate guard into the slot's predication header before the leaf consumer fills the slot body; a non-OK status from it converts to a logged status at the per-arm `isa_emitter.cc` line number.

### Arm Map

The ten arms, byte-confirmed against the TAC body and its jump table; the SCS and TEC routers have the identical distribution.

| Arm | Opcodes | Slot class → action | Confidence |
|---|---:|---|---|
| Stream | 888 | `GetStreamSlot` + `EmitPredicationToSlot<…Stream>` + `ConsumeStreamInstruction` | CONFIRMED |
| ScalarMisc | 92 | `GetScalarMiscSlot` + `…<…ScalarMisc>` + `ConsumeScalarMiscInstruction` | CONFIRMED |
| ScalarAlu | 49 | `GetScalarAluSlot` (StatusOr) + `…<…ScalarAlu>` + `ConsumeScalarAluInstruction` | CONFIRMED |
| ScalarAlu-S1 | 27 | `GetScalarAluSlotS1` + `ConsumeScalarAluSlotS1Instruction` (fixed S1) | CONFIRMED |
| ScalarAlu-S0 | 17 | `GetScalarAluSlotS0` + `ConsumeScalarAluSlotS0Instruction` (fixed S0) | CONFIRMED |
| DMA | 70 | guard oneof → `clear_scalar_instruction` → `DefaultConstruct<SparseCoreDma>` → variant | CONFIRMED |
| Multi-scalar (flag) | 54 | `flags & 1 → S0` / `& 2 → S1` / `& 4 → Misc`; none → error | CONFIRMED |
| Optional-skip | 4 | `if tolerate_skip return OK; else DEFAULT` (`0x100d/0x100e/0x1015/0x10f2`) | CONFIRMED |
| No-op | 1 | `return OK` (opcode `0x264`) | CONFIRMED |
| Default / OOB | 2817 | `MakeErrorImpl` "Unsupported opcode while consuming slot instruction: $0 : $1" | CONFIRMED |

> **GOTCHA — the slot class is in the jump table, but for 54 ops the *sub-slot* is in the `MCInst` flags, not the opcode.** The five fixed-slot arms (Stream, ScalarMisc, ScalarAlu, S0, S1) decide the slot from the opcode alone. The multi-scalar arm does not: it carries no fixed slot and reads the per-`MCInst` flag word to pick S0/S1/Misc. A reimplementer who maps opcode→slot statically will mis-route every one of those 54 ops, because the same opcode can land on a different scalar sub-slot in two different bundles depending on the scheduler's flag stamp.

---

## The Slot-Flag Sub-Routing

### The flag word and its bits

The router's second input is the slot-flags word, read by `getSlotFlagsFromMCInst` (`0x13c798e0`), whose entire body is `return *((u32*)mcinst + 1)` — i.e. the flags live at `MCInst+0x4`. The low three bits are the `llvm::TPU::SparseCoreMCSlot` enumeration, stamped upstream by the scheduler (`setSlotFlagInMCInst`, see [SCS scalar opcode page](scalar-opcode-enum.md)):

| Bit | Mask | `SparseCoreMCSlot` | Meaning |
|---:|---:|---|---|
| 0 | `0x1` | `SLOT_S0` | issue on scalar-ALU sub-slot 0 |
| 1 | `0x2` | `SLOT_S1` | issue on scalar-ALU sub-slot 1 |
| 2 | `0x4` | `SLOT_SM` | issue on the ScalarMisc slot |

### The multi-scalar dispatch

For the 54 multi-slot opcodes, the arm is a priority test on those bits, building a two-element `std::variant` value-visitor whose index selects the slot accessor's proto type:

```c
// multi-scalar arm  (glc 0x139f14f8)
if (flags & 1):                                   // SLOT_S0 — highest priority
    slot = GetScalarAluSlotS0<SparseCoreScalarAlu,Bundle>(flags, bundle)
    visitor_index = 0                              // → SparseCoreScalarAlu*
else if (flags & 2):                               // SLOT_S1
    slot = GetScalarAluSlotS1<SparseCoreScalarAlu,Bundle>(flags, bundle)
    visitor_index = 0                              // → SparseCoreScalarAlu*
else if (flags & 4):                               // SLOT_SM
    slot = GetScalarMiscSlot<SparseCoreScalarMisc,Bundle>(flags, bundle)
    visitor_index = 1                              // → SparseCoreScalarMisc*
else:                                              // no slot bit set
    return MakeError("Invalid slot. Expected Scalar Slot. MCInst Flags: $0", flags)
return __variant_dispatch[visitor_index](visitor, slot)   // {ScalarAlu*, ScalarMisc*}
```

The `else` branch — no `SLOT_S0/S1/SM` bit set on an op the router believes is scalar — formats the flags through `FastIntToBuffer` and `SubstituteAndAppendArray` into the second of the router's two error strings and returns it (`MakeErrorImpl<9>` at `isa_emitter.cc:151`, source-location 5882). This is the router's internal-consistency check: the jump table claims the op is scalar, but the scheduler stamped no scalar slot, so emission cannot proceed.

> **QUIRK — S0 wins over S1 wins over Misc; the test order is the policy.** The flag bits are checked in fixed priority `S0 → S1 → SM`, not as a one-hot. An `MCInst` with both `SLOT_S0` and `SLOT_S1` set routes to S0. Whether the scheduler ever sets more than one bit on a multi-scalar op was not traced (LOW), but the router's behavior if it does is deterministic and is the test order above, not an error.

---

## The DMA, Optional-Skip, and No-Op Arms

### DMA materialization (70 opcodes, `0xfa1..0x1024`)

The DMA arm does not call a `Get<Slot>Slot` accessor. A DMA op is not a slot fill; it is a descriptor that the router materializes into the bundle's `scalar_instruction` oneof, then dispatches through a DMA-type variant visitor.

```c
// DMA arm  (glc 0x139f1467)
oneof = *(u32*)(bundle + 0x38)                 // scalar_instruction oneof tag
switch oneof:                                   // precondition: must be empty
    case 2: LogFatal("!bundle.has_dma()",        isa_emitter.cc:157)
    case 6: LogFatal("!bundle.has_stream()",     isa_emitter.cc:158)
    case 1: LogFatal("!bundle.has_scalar_alu()", isa_emitter.cc:159)
if !(flags & 1): LogFatal("flags & SLOT_S0", isa_emitter.cc:161)   // requires S0
if !(flags & 2): LogFatal("flags & SLOT_S1", isa_emitter.cc:162)   // and S1
clear_scalar_instruction(bundle)               // 0x1fb59220
*(u32*)(bundle + 0x38) = 2                       // set oneof = dma
dma = Arena::DefaultConstruct<SparseCoreDma>(bundle.arena)   // 0x1fb5a480
*(u64*)(bundle + 0x30) = dma
return __variant_dispatch[0](visitor, dma)     // {SparseCoreDma*, SparseCoreTecDma*}
```

> **NOTE — the DMA arm requires *both* `SLOT_S0` and `SLOT_S1` set.** Beyond the oneof-empty precondition, the decompile shows two further `LogFatal` asserts: a DMA op must carry both scalar-ALU slot-flag bits (`SLOT_S0` and `SLOT_S1`). The DMA descriptor occupies the width of both dual-issue scalar lanes, so the scheduler must reserve both; a DMA `MCInst` missing either bit is a fatal compiler invariant violation, not a recoverable status. The variant over `{SparseCoreDma*, SparseCoreTecDma*}` then routes the simple/general/strided/iova DMA sub-consumers downstream.

### Optional-skip (4 opcodes) and no-op (1 opcode)

```c
// optional-skip arm  (glc 0x139f17bf)
case 0x100d: case 0x100e: case 0x1015: case 0x10f2:
    if (!tolerate_skip) goto DEFAULT             // bool = consumer's 4th argument
    return OK                                     // silently drop the op

// no-op arm  (glc 0x139f176a)
case 0x264:
    return OK                                     // no slot fill, no descriptor
```

The optional-skip arm gates four opcodes on the consumer's `bool` argument (`char a4` in the decompile, the 4th parameter): when set, the four ops are silently accepted and produce no emission; when clear, they fall into the default-error arm. The single no-op opcode `0x264` always returns OK with no emission — it is the placement of a bundle slot that occupies no encoding (the all-zero NOP described on the [TEC Engine](tec-engine.md) page).

> **QUIRK — the optional-skip bool flips four opcodes between "silently dropped" and "hard error."** The meaning of the flag (tolerate-padding vs. speculative-decode vs. a per-gen feature gate) was not traced to its caller (LOW). What is certain: with the flag clear, opcodes `0x100d/0x100e/0x1015/0x10f2` are unsupported; with it set, they vanish from the bundle without error. A reimplementer must thread this bool from the bundle-consume loop, or a stream containing those four ops either errors or round-trips inconsistently.

---

## Bundle Invariance

`ConsumeOneSlotInstruction` is a template on the bundle type, and the SC emitter instantiates it three times — for the SCS, TAC, and TEC scalar regions. All three instances are byte-identical in classify structure.

| Bundle | Router entry | Jump table | Base | Bound | Arm distribution |
|---|---|---|---:|---:|---|
| `SparseCoreTacBundle` | `0x139f1360` | `0xae8dce4` | `0x1f3` | `0xfb2` | 2817/888/92/70/54/49/27/17/4/1 |
| `SparseCoreScsBundle` | `0x13a50540` | `0xaea9fb4` | `0x1f3` | `0xfb2` | identical |
| `SparseCoreTecBundle` | `0x13a15500` | `0xaea4ba0` | `0x1f3` | `0xfb2` | identical |

The opcode→arm map is the same across all three; only the bundle template argument on `GetStreamSlot<…,Bundle>`, `GetScalarAluSlot<…,Bundle>`, etc. differs. All three bundles carry the dual scalar-ALU sub-slots (`GetScalarAluSlotS0`/`S1` are present and reached in each), so the S0/S1 dual-issue scalar geometry is a property of the SC scalar slot, not of any one engine.

> **NOTE — the TEC bundle's *vector* slots are routed elsewhere.** `ConsumeOneSlotInstruction<SparseCoreTecBundle>` handles only the TEC bundle's *scalar* region (the same Stream/Misc/Alu/DMA slots SCS and TAC have). The TEC's vector slots are dispatched by the separate `ConsumeOneTecBundleInstruction` (`0x13a08e00`), described next. A reimplementer must not look for `VectorAlu` in the OneSlot router; it is not there.

---

## Reaching VectorAlu — the Separate Vector Path

### Why the vector dispatch is a different function

The TEC bundle is the only SC bundle with a vector compute region, and its vector slots (`VectorAlu0/1/2`, `VectorLoad`, `VectorStore`, `VectorExtended`, `VectorResult`) are routed by `ConsumeOneTecBundleInstruction` (`0x13a08e00`), which sits *beside* `ConsumeOneSlotInstruction` under the per-`MCInst` TEC dispatcher. The scalar router and the vector router share the same classify *idiom* — read `DWORD[MCInst]`, subtract a base, bound-check, indirect-jump through a `.rodata` table — but they are distinct functions with distinct tables, because the scalar opcode space (`0x1f3`-based) and the vector opcode space (`0xb26`-based for `VectorAlu`) are disjoint.

### `ConsumeVectorAluInstruction` — the 142-op reach

The `VectorAlu` slot's consumer is `ConsumeVectorAluInstruction<glc::SparseCoreTecBundle>` (`0x13a0b580`), reached from `ConsumeOneTecBundleInstruction` for any opcode in the `VectorAlu` block. Its dispatch is the same shape as the scalar router, with the vector base and bound:

```c
// ConsumeVectorAluInstruction   // glc 0x13a0b580
//   args: (printer, mcinst, &vregports /*btree_set<SparsecoreVregReadPort>*/, &proto, &bundle)
function ConsumeVectorAluInstruction(printer, mcinst, vregports, proto, bundle):
    idx = mcinst.opcode - 0xb26                   // jt base 0xb26
    if (unsigned)idx > 0x5cf:                       // bound 0x5cf (1488 entries)
        return Error("Unsupported opcode for Vector Alu slot: $0 : $1", …)
    switch jt[idx]:                                // jt @0xae9d3dc, 143 targets
        case 0xb26: proto.mutable_vector_add_bf16();
                    return EmitVectorBinop<…VectorAddBf16,SparsecoreVregReadPort>(mcinst)
        case 0xc9f: proto.mutable_cosq_f32();
                    return EmitExtendedVectorVxUnop<…CosqF32>(mcinst)
        case 0xe87: GetOperandAndVsEncoding(mcinst, 1);
                    proto.mutable_pack_compressed_b16_to_b8();
                    return EmitPackVectorBinop<…PackCompressedB16ToB8>(mcinst)
        // 7 f32 compares + VectorMove share one oneof-dispatch chain [proto+0x50]
        default:    return Error(…)                 // 1213 opcodes
```

Each arm calls `SparseCoreTecVectorAlu::_internal_mutable_<op>()` to select the proto oneof field, then tail-jumps one of nine `Emit*` templates. The fifth template parameter `SparsecoreVregReadPort` (carried as a `btree_set` argument) is the per-bundle read-port reservation the bundle scheduler must satisfy across the three concurrent lanes. The 142 reachable ops — 135 single-op arms plus seven reached through one shared f32-compare/move oneof chain — their opcode values, emission templates, and per-generation deltas are the subject of the [TEC Vector Opcode Enumeration](vector-opcode-enum.md) page and are not duplicated here.

> **NOTE — the scalar and vector default-error strings are distinct `.rodata` literals.** The `VectorAlu` default-error string is "`Unsupported opcode for Vector Alu slot: $0 : $1`". The scalar OneSlot router uses the "while consuming slot instruction" phrasing; the vector consumer uses "for Vector Alu slot." Both are byte-confirmed against the decompiled bodies.

> **GOTCHA — scalar and vector dispatch differ in signature, not just table.** `ConsumeOneSlotInstruction` takes `(printer, mcinst, &bundle, bool)` and returns after filling one scalar slot. `ConsumeVectorAluInstruction` takes `(printer, mcinst, &vregports, &proto, &bundle)` — it additionally threads the `SparsecoreVregReadPort` btree and a pre-selected `SparseCoreTecVectorAlu` proto, because a vector op binds read ports the scheduler tracks per bundle. A reimplementer cannot reuse the scalar router's calling convention for the vector slots.

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `ConsumeOneSlotInstruction<…TacBundle>` | `0x139f1360` | the scalar-slot router (this page); jt base `0x1f3`, bound `0xfb2` | CONFIRMED |
| `ConsumeOneSlotInstruction<…ScsBundle>` | `0x13a50540` | SCS instance; jt `0xaea9fb4`, identical arm map | CONFIRMED |
| `ConsumeOneSlotInstruction<…TecBundle>` | `0x13a15500` | TEC scalar instance; jt `0xaea4ba0`, identical arm map | CONFIRMED |
| `getSlotFlagsFromMCInst` | `0x13c798e0` | `return *(u32*)(mcinst+0x4)` — the slot-flag word source | CONFIRMED |
| OneSlot jump table (TAC) | `0xae8dce4` | 4019×int32 rel offsets; 10 arm targets | CONFIRMED |
| `GetStreamSlot<…,TacBundle>` | `0x139fa760` | Stream slot accessor | CONFIRMED |
| `GetScalarMiscSlot<…,TacBundle>` | `0x139eeac0` | ScalarMisc slot accessor | CONFIRMED |
| `GetScalarAluSlot<…,TacBundle>` | `0x139f0800` | generic ScalarAlu slot accessor (StatusOr) | CONFIRMED |
| `GetScalarAluSlotS0` / `…S1` | `0x139f7300` / `0x139f74a0` | dual-issue sub-slot accessors | CONFIRMED |
| `ConsumeStreamInstruction` | `0x139fa940` | Stream slot leaf consumer | CONFIRMED |
| `ConsumeScalarMiscInstruction` | `0x139eeca0` | ScalarMisc slot leaf consumer | CONFIRMED |
| `ConsumeScalarAluInstruction` | `0x139f09c0` | generic ScalarAlu leaf consumer | CONFIRMED |
| `ConsumeScalarAluSlotS0Instruction` / `…S1` | `0x139f9480` / `0x139f9be0` | dual-issue leaf consumers | CONFIRMED |
| `clear_scalar_instruction` | `0x1fb59220` | DMA arm: clears the `scalar_instruction` oneof | CONFIRMED |
| `Arena::DefaultConstruct<SparseCoreDma>` | `0x1fb5a480` | DMA arm: materializes the DMA descriptor | CONFIRMED |
| DMA variant dispatcher | `0x13a04820` | `{SparseCoreDma*, SparseCoreTecDma*}` value-visitor | CONFIRMED |
| `MakeErrorImpl<9>` | `0x2111e900` | both router error paths | CONFIRMED |
| `ConsumeOneTecBundleInstruction` | `0x13a08e00` | the separate TEC *vector*-slot dispatcher | CONFIRMED |
| `ConsumeVectorAluInstruction<…TecBundle>` | `0x13a0b580` | reaches the 142-op `VectorAlu` table; jt `0xae9d3dc`, base `0xb26` | CONFIRMED |

Error strings (`.rodata`): "`Unsupported opcode while consuming slot instruction: $0 : $1`" (`0x9e6fbec`, default arm) and "`Invalid slot. Expected Scalar Slot. MCInst Flags: $0`" (`0x9fbf02c`, multi-scalar no-flag arm). Source file `platforms/xla/sparse_core/ghostlite/isa_emitter.cc` (`0x8762dbb`).

---

## Considerations

- **Slot class is opcode-driven; sub-slot is flag-driven.** The five fixed-slot arms decode from the opcode alone; the 54-op multi-scalar arm and the DMA arm read the `SparseCoreMCSlot` flag word (`MCInst+0x4`). A correct reimplementation needs both the 4019-entry table and the upstream flag-stamping discipline, or multi-slot ops mis-route.
- **The router is the placement seam, not the encoder.** It chooses the slot and stamps predication (`EmitPredicationToSlot`); the per-slot `Consume<Slot>Instruction` leaf fills the slot body, and the `<Slot>Encoder::Encode` (`BitCopy`) writes the absolute bundle bits below that. The byte-level encoding lives on the per-slot pages, not here.
- **DMA preconditions are `LogFatal`, not status.** The DMA arm's oneof-empty check and its `SLOT_S0`/`SLOT_S1` requirement are compiler invariants (`LogMessageFatal`), so a violation aborts rather than returning an error status. A reimplementer must guarantee these upstream; they are not recoverable at the router.
- **Vector dispatch is a parallel mechanism, not a sub-arm.** `ConsumeOneSlotInstruction` never reaches `VectorAlu`; the vector slots are routed by `ConsumeOneTecBundleInstruction` → `ConsumeVectorAluInstruction`, with a different signature (threading the `SparsecoreVregReadPort` btree and the `SparseCoreTecVectorAlu` proto). Treat the two routers as siblings under the TEC bundle dispatcher.
- **The optional-skip bool is an untraced policy input (LOW).** Four opcodes flip between drop and error on the consumer's `bool`. The bit is byte-confirmed; its caller-side meaning is not. Thread it from the bundle-consume loop and treat the four opcodes as conditionally supported.

---

## Related Components

| Name | Relationship |
|---|---|
| `ConsumeOneTecBundleInstruction` (`0x13a08e00`) | the per-`MCInst` TEC dispatcher above both this router and the vector consumer |
| `ConsumeVectorAluInstruction` (`0x13a0b580`) | the sibling vector-slot consumer reaching the 142-op `VectorAlu` table |
| `getSlotFlagsFromMCInst` (`0x13c798e0`) | the `MCInst+0x4` slot-flag word the multi-scalar and DMA arms sub-route on |
| `Get<Slot>Slot` / `Consume<Slot>Instruction` family | the slot accessors and leaf consumers each arm tail-calls |
| `EmitPredicationToSlot<…>` | stamps the op's predicate guard into the slot before the leaf consumer fills it |

## Cross-References

- [TEC (Vector) Engine](tec-engine.md) — the 64-byte bundle, the scalar slot byte/bit bases, and the dual-issue S0/S1 geometry the router places ops into.
- [SCS Engine](scs-engine.md) — the scalar control sequencer; `ConsumeOneSlotInstruction<SparseCoreScsBundle>` is the same router for the SCS scalar region.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the 142-op `VectorAlu` roster and its emission templates, reached through `ConsumeVectorAluInstruction` (linked, not duplicated, here).
- [SCS Scalar Opcode Enumeration](scalar-opcode-enum.md) — the scalar opcode roster and the `setSlotFlagInMCInst` discipline that stamps the `SparseCoreMCSlot` bits this router reads.
- [VectorLoad Slot](vectorload-slot.md) — a TEC vector slot routed by `ConsumeOneTecBundleInstruction`, not this scalar router.
- [VectorStore Slot](vectorstore-slot.md) — the tile vector-store + scatter-add slot, likewise a vector-path slot.
- [VectorExtended (VEX)](vectorextended-vex.md) — the scan/sort/dedup vector slot, also reached through the vector dispatcher.
- [SparseCore Overview](overview.md) — the three engine classes, per-generation presence, and the codec-template sequencer enum.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore ISA — [back to index](../index.md)
