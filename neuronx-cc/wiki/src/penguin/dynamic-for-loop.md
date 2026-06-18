# Data-Dependent Control Flow: InstDynamicForLoop

> *All addresses on this page apply to neuronx-cc 2.24.5133.0+58f8de22 (starfish BIR, cp310 wheel). Symbol/offset evidence is drawn from `libBIR.so`, `libBIRSimulator.so`, `libwalrus.so` and the statically-linked driver binaries (`coloring_allocator_with_loop`, `nki_klr_sim`, `bir_roundtrip`) under `ida/`. Other versions and other wheels (cp311/cp312) relocate every address.*

## Abstract

`InstDynamicForLoop` is the BIR instruction for a **runtime-trip-count** loop — the data-dependent sibling of the static `InstLoop`. A static loop knows its iteration count at compile time (the trip count is a ceil-divide over constant `lo`/`hi`/`stride` integer fields baked into the axis). A dynamic loop does not: its upper bound is a hardware register whose value is only known when the kernel runs, materialized from a dynamic-shape / data-dependent quantity in the front end. Everything about the instruction — its verifier contract, its (absent) static cost model, and its simulator semantics — flows from that single difference.

Structurally the two are near-twins. `InstDynamicForLoop` is a 336-byte (`0x150`) object whose `bir::Instruction` base is embedded at `+0x88` (the opcode field lives in that base, value **106** / `IT106`). It owns exactly **one** induction axis, a `DynamicForLoopAxis*` at `+0x148`, carrying three `QuasiAffineExpr` slots — lower bound at axis `+0x68`, upper bound (the trip count) at axis `+0x88`, step at axis `+0xA8`. `DynamicForLoopAxis` derives from `BirLoopAxis` and overrides exactly two virtuals: `getTripcount()` returns `0` (there is no static count) and `hasRuntimeValue()` returns `1` (the count is always a runtime value). Those two overrides are the clean discriminator between a static and a dynamic axis.

This page documents the instruction end to end: the struct layout and the static-vs-dynamic axis split; the verifier's well-formedness / back-edge contract (`lb`/`stride` must be pure constants, `ub` must be exactly one affine term whose index is a non-null runtime register); the three cooperating back-edge mechanisms (the per-instruction verifier, the symbolic-CFG nesting collector, and the CFG topology validator that raises the `backEdgeAssertCondition` assertion at line 419); and the simulator, which reads the *concrete* register value (not a symbolic estimate, not a conservative max-bound) and executes the body exactly that many times. The runtime trip-count register itself is produced upstream by the codegen pass that fills `axisRegs : DenseMap<DynamicForLoopAxis*, Register*>` (see [Symbolic AP / Register ALU](symbolic-ap-register-alu.md)).

For reimplementation, the contract is:

- **The object & axis layout** — `InstDynamicForLoop` (`0x150`, base at `+0x88`, opcode 106, axis ptr at `+0x148`) and `DynamicForLoopAxis` (`0xC8`, three QAE at `+0x68/+0x88/+0xA8`, two override flags, parent back-pointer).
- **The verifier / back-edge contract** — the exact assertion chain that constrains a well-formed dynamic loop, and the three layers of back-edge recognition.
- **The simulator trip-count read** — `trip = const + coeff * RegState::read(register)`, then init-`lb` / iterate-while-`cur<ub` / `cur+=stride`, binding the induction variable each pass.
- **Why the static cost model is absent** — all four `Hwm::getLatency*` overrides are stubs; a dynamic loop is a pure control container costed by recursing its body.

| | |
|---|---|
| **Instruction** | `bir::InstDynamicForLoop` — opcode **106** (`IT106`) |
| **Object size** | `0x150` (336 bytes); `bir::Instruction` base embedded at `+0x88` |
| **Axis pointer** | `+0x148` → one `bir::DynamicForLoopAxis*` |
| **Axis object** | `bir::DynamicForLoopAxis : public BirLoopAxis`, `0xC8` (200 bytes) |
| **Axis QAE slots** | `lb` @ `+0x68`, `ub` @ `+0x88` (trip count), `stride` @ `+0xA8` |
| **Dynamic discriminator** | `getTripcount()==0` and `hasRuntimeValue()==1` (overrides) |
| **Trip-count register** | `BirIntRuntimeValue::regref` @ `+0x40` (the `axisRegs` value) |
| **Verifier** | `birverifier::InstVisitor::visitInstDynamicForLoop` (lib `walrus`) |
| **Back-edge assertion** | `backEdgeAssertCondition` NeuronAssertion, `CFG::validateTopology`, **line 419** |
| **Simulator** | `birsim::InstVisitor::visitDynamicForLoop` / `evaluateUpperBoundExpr` (lib `BIRSimulator`) |
| **Static cost model** | all four `bir::Hwm::getLatency*(InstDynamicForLoop)` = not-implemented stubs |

> **GROUNDING (provenance) — the standalone `.so` bodies ARE in the corpus.** `libBIR.so`,
> `libBIRSimulator.so` (and its libBIR-core sibling `libBIRParserDumper.so`), and `libwalrus.so`
> all ship as per-symbol decompiled/disassembled sidecars *and* as full IDA databases under
> `ida/`. Every body cited here — the `Hwm::getLatency*` stubs, `evaluateUpperBoundExpr`,
> `visitDynamicForLoop`, `validateTopology` — is disassembled, not merely declared via an
> import. Where an address is given in a statically-linked driver's frame (`nki_klr_sim`,
> `coloring_allocator_with_loop`), the same body is independently present in the originating
> `.so`. The addresses are CONFIRMED — the same stance as the sibling backend pages
> ([Symbolic-AP Register-ALU](symbolic-ap-register-alu.md), [Backend Dependence-Distance](backend-dependence-distance.md),
> [DGE Level Selection](dge-level-dynamic-dma.md), [Dynamic-Shape Synthesis](dynamic-shape-synthesis.md)).

---

## Object Layout & the Axis

### Purpose

`InstDynamicForLoop` is a control container: it holds one induction axis plus a body of basic blocks, and re-executes that body a runtime number of times. Unlike a normal arithmetic instruction it produces no value of its own; the loop structure *is* the instruction.

### The instruction object

The instruction is built through the `NamedObjectContainer<BasicBlock,Instruction>::insertElement<InstDynamicForLoop>` template (the standard BIR element-insertion path). The construction allocates `operator new(0x150)`, runs the `bir::Instruction` base constructor on the sub-object at `+0x88` with opcode literal **106**, installs the primary vtable at `+0x00` and the Instruction-base vtable at `+0x88`, and seeds a default field at `+0x38` with `0x3F800000` (float `1.0f`).

```c
// InstDynamicForLoop construction (insertElement<InstDynamicForLoop>)   [STRONG]
InstDynamicForLoop* make_dynamic_for_loop(Function* fn, ...):
    self = operator new(0x150)                  // 336 bytes
    *(float*)(self + 0x38) = 1.0f               // 0x3F800000 default field
    Instruction::Instruction(self + 0x88,       // base sub-object at +0x88
                             ..., fn, /*opcode*/ 106, ...)   // <-- IT106
    *(void**)(self + 0x00) = &vtable_InstDynamicForLoop
    *(void**)(self + 0x88) = &vtable_Instruction_base
    // axis pointer slot at self + 0x148 starts null; setAxis() fills it
    return self
```

> **QUIRK —** the opcode field is *not* at the top of the object. It lives inside the embedded `bir::Instruction` base at `+0x88`. Every opcode-keyed dispatch (the simulator's `IRVisitor::visit`, the verifier's `InstVisitor`) reads it from `inst + 0x88 + <Instruction opcode offset>`, not from `inst + 0`. The simulator dispatch reads it as `*(i32*)(inst + 0x58)` relative to the *base* pointer it already holds.

> **NOTE —** the *static* `InstLoop` (opcode 105) is a larger object — `operator new(0x190)` (400 bytes) with its own dedicated constructor and a static `LoopAxis`. The two loop opcodes 105/106 sit adjacent in the enum and are fast-pathed adjacently in the simulator (see [Simulator](#simulator-concrete-trip-count) below).

### The DynamicForLoopAxis

`InstDynamicForLoop::setAxis(name, QuasiAffineExpr lb, QuasiAffineExpr ub, QuasiAffineExpr stride)` allocates the single axis: `operator new(0xC8)`, runs the `BirLoopAxis` base constructor with axis-kind tag `6`, installs the `BirLoopAxis` then `DynamicForLoopAxis` vtables, sets the two override flags, stores the parent back-pointer, and copy-constructs the three `QuasiAffineExpr` into `+0x68/+0x88/+0xA8`. The finished axis is stored into the parent at `+0x148`.

> **QUIRK —** the *signature* of `setAxis` is itself the static-vs-dynamic tell. `bir::InstLoop::setAxis(string, long, long, long)` takes three **integer** bounds (CONFIRMED: the mangled thunk in `nki_klr_sim` is `...EElll`). `bir::InstDynamicForLoop::setAxis(string, QuasiAffineExpr, QuasiAffineExpr, QuasiAffineExpr)` takes three **affine expressions**. A static loop's bounds are compile-time integers; a dynamic loop's bounds are expression trees that may reference a runtime value.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| vptr | `+0x00` | `DynamicForLoopAxis` vtable | dispatch table (overrides `getTripcount`/`hasRuntimeValue`) |
| default reg/flags blob | `+0x20` | 16 B | default register / flag init blob |
| dynamic flag | `+0x30` | u64 = `1` | marks axis dynamic |
| parent | `+0x38` | `InstDynamicForLoop*` | back-pointer to owning instruction |
| has-runtime flag | `+0x40` | u32 = `1` | the axis carries a runtime value |
| name ptr / len / SSO | `+0x48`/`+0x50`/`+0x58` | string | axis (induction-variable) name |
| `lb` | `+0x68` | `QuasiAffineExpr` (32 B) | lower bound — induction init |
| `ub` | `+0x88` | `QuasiAffineExpr` (32 B) | **upper bound = trip count expr** |
| `stride` | `+0xA8` | `QuasiAffineExpr` (32 B) | step |

> **CORRECTION (D-Z03 §index) —** the backing report's 5-line index opens by calling `InstDynamicForLoop` "BIR opcode 6". That is the *axis-kind tag* passed to the `BirLoopAxis` base constructor (`setAxis` passes `6`), **not** the instruction opcode. The instruction opcode is **106** (`IT106`) — confirmed by the `Instruction::Instruction(..., 106, ...)` literal in the constructor and the simulator's `cmp 0x6A` dispatch. Sections A–F of the same report carry the correct value 106 throughout; only the one-line summary conflates the two. Do not drive a reimplementation off "6".

### findAxis

`InstDynamicForLoop::findAxis(name, Instruction*)` reads the single axis at `+0x148`, calls its `getNameStr()` (the `BirLoopAxis` virtual), and compares against the requested name. On a miss it falls through to `BasicBlock::findAxis(...)` on the loop body — so a dynamic loop owns exactly **one** local induction axis, and any outer axis is resolved through the enclosing basic block. Nesting is by block structure, not by multi-axis instructions.

---

## Static vs Dynamic Axis: the Discriminator

### Purpose

Both `LoopAxis` (static) and `DynamicForLoopAxis` share the `lo`/`hi`/`stride` QAE layout, but they answer the two trip-count virtuals differently. Those answers are the *only* reliable way to tell, at a vtable call site, whether a loop is statically or dynamically bounded.

### Algorithm

```c
// bir::LoopAxis::getTripcount()  — STATIC axis                        [STRONG]
long LoopAxis::getTripcount():
    stride = this->stride                       // integer field
    assert(stride != 0)                         // "Division by zero"
    lo = this->lo; hi = this->hi                // integer fields
    if stride > 0 && lo < hi:  return (hi - 1 - lo) / stride + 1   // ceil-div
    if stride < 0 && lo > hi:  return (lo - 1 - hi) / -stride + 1
    return 0
// bir::LoopAxis::hasRuntimeValue() : return 0

// bir::DynamicForLoopAxis::getTripcount()    : return 0     // NO static count   [STRONG]
// bir::DynamicForLoopAxis::hasRuntimeValue() : return 1     // count is runtime
```

The static axis computes a real compile-time ceil-divide over its integer `lo`/`hi`/`stride` (the same arithmetic as `pelican::calculateTripcount(lo, hi, stride) = divideCeil(hi - lo, stride)`, which asserts `stride != 0` "Division by zero"). The dynamic axis cannot: its `hi` is a `QuasiAffineExpr` that lowers to a register, so `getTripcount()` short-circuits to `0` and the loop is bounded at runtime by the materialized register.

> **GOTCHA —** never read a dynamic loop's iteration count from `getTripcount()`. It is hard-wired to `0`. A static-sizing or cost pass that wants a conservative bound must call the runtime-aware `getMaxTripcount()` virtual instead (vtable slot, `pelican::AffineIdx::getMaxTripcount`); the simulator, which has a live register, reads the *exact* value via `evaluateUpperBoundExpr` (next sections). Three different consumers, three different answers — confusing them silently mis-sizes a buffer or hangs a loop.

---

## Verifier & Back-Edge Contract

### Purpose

The verifier guarantees that a dynamic loop is *shaped* the only way the simulator and codegen can handle: a single induction axis from a constant start, by a constant step, to a runtime end held in a non-null register. It also participates in the function-wide back-edge topology check. Three cooperating mechanisms enforce this.

### (i) Per-instruction well-formedness — `visitInstDynamicForLoop`

`birverifier::InstVisitor::enterInstDynamicForLoop` simply forwards to `visitInstDynamicForLoop`, which runs the base `visitInstruction` checks on the embedded Instruction (`inst + 0x88`), then reads the axis at `+0x148` and walks an assertion chain over its three QAE.

```c
// birverifier::InstVisitor::visitInstDynamicForLoop(InstDynamicForLoop& inst)   [STRONG]
void visitInstDynamicForLoop(inst):
    visitInstruction(inst + 0x88)               // base Instruction checks
    axis = *(DynamicForLoopAxis**)(inst + 0x148)

    lb = axis->lb     (axis + 0x68)
    assert(lb.getKind() == AffExprKind)         // (2) lb is affine
    assert(lb.getNumTerms() == 0)               // (1) lb is a pure CONSTANT

    stride = axis->stride  (axis + 0xA8)
    assert(stride.getKind() == AffExprKind)     // (4) stride is affine
    assert(stride.getNumTerms() == 0)           // (3) stride is a pure CONSTANT

    ub = axis->ub     (axis + 0x88)
    assert(ub.getKind() == AffExprKind)         // (6) ub is affine
    assert(ub.getNumTerms() == 1)               // (5) ub has EXACTLY ONE term
    ubIdx = ub.term[0].idx
    assert(ubIdx->getKind() == IntRuntimeValueKind) // (7) the term is a runtime value
    ubReg = ubIdx->register   (BirIntRuntimeValue + 0x40)
    assert(ubReg != nullptr)                    // (8) the runtime register is NON-NULL
    // on any failure -> NeuronAssertion (RESOLUTION_CONTACT_SUPPORT), not a miscompile
```

The "back-edge assertion" at the instruction level is exactly `(5)+(7)+(8)`: the only legal dynamic upper bound is `coeff * <runtime-register> + const`, where the register is real and non-null. `lb` and `stride` being pure constants means a dynamic loop is a *single* induction variable with a known start and fixed step and a runtime *end*. This is the same invariant the simulator relies on (`getNumTerms()==1 && hasRuntimeValue()`), and the same `IntRuntimeValue->register` fetch — verifier and simulator agree by construction.

> **GOTCHA —** a multi-term or pure-constant `ub` is rejected, not silently coerced. If you emit a dynamic loop whose upper bound is `regA + regB` (two terms) or a plain constant (zero terms), the verifier fires the assertion. The runtime bound must reduce to one affine term over one register. The user-facing diagnostic for the simulator's mirror of this check is *"Only scalar values supported in Dynamic For Loop upper bound expressions currently"*.

### (ii) Symbolic-CFG nesting — `DynamicCFGBasicBlockCollectorSymbolic`

While walking the BIR to build the symbolic dynamic control-flow graph, this collector maintains a nesting-depth counter at `collector + 8`:

```c
// DynamicCFGBasicBlockCollectorSymbolic                              [STRONG]
enterInstDynamicForLoop(self):  ++*(i32*)(self + 8)   // entering a dynamic loop
leaveInstDynamicForLoop(self):  --*(i32*)(self + 8)   // leaving it
```

Body blocks collected while depth `> 0` are marked as living inside a dynamic loop — the back-edge region that re-executes a runtime number of times. The collector itself only tracks depth; block registration is in the base collector.

### (iii) CFG topology validation — `backEdgeAssertCondition`

The function-wide check lives in `neuronxcc::backend::CFG::validateTopology`. It walks the CFG's predecessor/successor structure and back-edge `MapVector`, and on a violated topology constraint raises a nested `NeuronAssertion<neuronxcc::backend::ErrorCode>` tagged with the literal string `"backEdgeAssertCondition"`.

```c
// CFG::validateTopology — back-edge branch  (verbatim shape)         [CONFIRMED]
if (back-edge topology constraint violated):
    msg  = lookup_cause(...) / lookup_resolution(...)   // RESOLUTION_CONTACT_SUPPORT
    tag  = "backEdgeAssertCondition"
    NeuronAssertion<ErrorCode>(record, /*line*/ 419, tag, formatted_msg, ...)
    throw_with_nested(record)                           // logs "Assertion failure: backEdgeAssertCondition"
```

> **NOTE —** the assertion-construction line number is **419**, confirmed directly in `CFG::validateTopology` (decompiled `sub_561960`, `coloring_allocator_with_loop`). The same function also constructs sibling NeuronAssertions at lines 417, 418, 420 for adjacent topology checks; `backEdgeAssertCondition` is specifically the one at 419. The resolution string is the standard aws-neuron support-ticket message — this is a hard compile error, not a warning.

`CFG::isBackEdge(from, to)` is the dominator/loop-header-based predicate the validator and the symbolic collector both rely on: it looks `to` up in the CFG's back-edge `MapVector<BasicBlock*,BasicBlock*>` and returns true iff the recorded back-edge of `to` is exactly `from`. That single recorded back-edge per loop header is what makes a dynamic-loop body re-execute.

---

## Simulator: Concrete Trip Count

### Purpose

The simulator (`libBIRSimulator.so`) is the one consumer that runs the loop for real. It reads the **actual** runtime register value at simulation time and executes the body exactly that many times — not a symbolic value, not a conservative max-bound.

### Dispatch

The simulator's instruction visitor fast-paths both loop opcodes at the very top of `IRVisitor<birsim::InstVisitor>::visit`, *before* the 110-entry jump table:

```c
// IRVisitor<birsim::InstVisitor>::visit(Instruction* inst)           [STRONG]
op = *(i32*)(inst + 0x58)                        // opcode in the Instruction base
if (op == 105) tailcall visitLoop(inst)          // 0x69 — STATIC loop
if (op == 106) tailcall visitDynamicForLoop(inst)// 0x6A — DYNAMIC loop   <--
if (op == 108) ...
else           switch (op) { /* 110-case jump table, base 0x6D */ }
```

`enterInstDynamicForLoop` is trivial — it sets the simulator's "current instruction" cursor to the embedded Instruction sub-object: `setInstPtr(this, inst + 0x88)` (the static-loop enter is byte-identical).

### The trip-count read — `evaluateUpperBoundExpr`

```c
// birsim::InstVisitor::evaluateUpperBoundExpr(InstDynamicForLoop& inst)   [STRONG]
long evaluateUpperBoundExpr(inst):
    ubExpr = *(Expr**)(*(void**)(inst + 0x148) + 0x88)   // axis->ub underlying expr
    affine = cast<pelican::AffineExpr>(ubExpr)           // require expr-kind == 17
    // mirror of the verifier contract:
    assert(affine->getNumTerms() == 1 && affine->hasRuntimeValue(),
           "Only scalar values supported in Dynamic For Loop upper bound expressions currently")
    term  = affine->terms.begin()[0]                     // single (idx, coeff) pair
    idx   = term.idx; coeff = term.coeff
    rtv   = cast<bir::BirIntRuntimeValue>(idx)           // require idx-kind in [6..13]
    reg   = *(Register**)(rtv + 0x40)                    // the trip-count register (axisRegs value)
    const_off = *(i32*)(affine + 0x38)                   // constant term of the affine expr
    return const_off + RegState::read(this + 0x150, reg, 0) * coeff
```

The returned upper bound is `const_offset + coeff * RegState::read(<runtime register>)`. `RegState::read` returns the *current* scalar value of a hardware register in the simulator's register file (`RegState` lives at `this + 0x150`). The register at `BirIntRuntimeValue + 0x40` is precisely the one the codegen `axisRegs : DenseMap<DynamicForLoopAxis*, Register*>` materialized — and the `coeff * reg` product matches the `InstRegisterAlu` MULT that codegen emits to compute it (see [Symbolic AP / Register ALU](symbolic-ap-register-alu.md)).

### The iteration driver — `visitDynamicForLoop`

```c
// birsim::InstVisitor::visitDynamicForLoop(InstDynamicForLoop& inst)   [STRONG]
void visitDynamicForLoop(inst):
    enterInstDynamicForLoop(inst)                  // set instruction cursor
    axis = *(DynamicForLoopAxis**)(inst + 0x148)   // DenseMap key for induction binding
    cur  = QuasiAffineExpr::eval(axis->lb)         // induction INIT (lb)
    ub   = evaluateUpperBoundExpr(inst)            // CONCRETE register read
    step = QuasiAffineExpr::eval(axis->stride)     // step
    if (cur < ub):                                 // entry guard
        do:
            inductionMap[axis] = cur               // DenseMap<AffineIdx*,long> @ this+0x1688
            for (BB : blocks(inst)):               // body basic blocks
                birsim::InstVisitor::visit(BB)     // recurse + execute each block
            writtenLocs.clear()                    // per-iter DenseSet<MemoryLocation*> @ this+0x1c88
            cur += step
        while (ub > cur)                           // iterate while current < upper
```

The simulator binds the induction variable into `inductionMap : DenseMap<pelican::AffineIdx*, long>` (`this + 0x1688`) each iteration so the body's access-pattern evaluations see the live counter, and clears a per-iteration touched-memory set `writtenLocs : DenseSet<MemoryLocation*>` (`this + 0x1c88`) between passes.

> **QUIRK —** the dynamic loop's iteration engine is structurally *identical* to the static `InstLoop`'s. `visitLoop` (opcode 105) does the same init / `while (cur < hi)` / `cur += step` / body-recursion / induction-binding — the *only* difference is where the bound comes from: the static loop reads `hi` straight from the axis' integer field (`*(u64*)(axis + 40)`), the dynamic loop substitutes `evaluateUpperBoundExpr` (a `RegState::read`). One more difference worth noting: the **static** axis is at `inst + 0x150` (decimal 336) while the **dynamic** axis is at `inst + 0x148` (decimal 328) — do not reuse the static offset to fetch the dynamic axis.

---

## Static Cost Model: Deliberately Absent

### Purpose

The hardware static cost model (`bir::Hwm`) assigns per-instruction latency for scheduling and performance estimation. For a dynamic loop it assigns nothing — the iteration count is unknown until runtime, so a per-instruction latency is undefined.

### Function map

All four `Hwm` latency overrides for `InstDynamicForLoop` are not-implemented stubs that `__assert_fail` with a "not implemented" message. The dynamic loop is a pure control container; its cost is obtained by the cost/perf model recursing the loop **body**.

| Function | Symbol | Behavior | Confidence |
|---|---|---|---|
| `getLatency` | `_ZNK3bir3Hwm10getLatencyERKNS_18InstDynamicForLoopE` | `__assert_fail "not implemented"` | STRONG |
| `getLatencyExec` | `_ZNK3bir3Hwm14getLatencyExecERKNS_18InstDynamicForLoopENS_10EngineTypeE` | `__assert_fail "not implemented"` | STRONG |
| `getLatencyReadInit` | `_ZNK3bir3Hwm18getLatencyReadInitERKNS_18InstDynamicForLoopENS_10EngineTypeE` | `__assert_fail "not implemented"` | STRONG |
| `getLatencyWriteDrain` | `_ZNK3bir3Hwm20getLatencyWriteDrainERKNS_18InstDynamicForLoopENS_10EngineTypeE` | `__assert_fail "not implemented"` | STRONG |

> **NOTE —** all four mangled symbols are CONFIRMED present and their bodies are in the
> corpus: each `Hwm::getLatency*(InstDynamicForLoop)` overload ships a decompiled/disassembled
> sidecar (e.g. `getLatency` and `getLatencyReadInit` in the libBIR-core `libBIRParserDumper.so`
> at `0x1e4fb0` / `0x1e4d78`), and the statically-linked `nki_klr_sim` carries the same bodies
> in its own VA frame (`getLatency @0x99e608`, …). The stub bodies (`__assert_fail
> "not implemented"`, `Hwm.cpp` line refs) are therefore CONFIRMED, not merely declared.
> A reimplementer should treat the dynamic loop as having no intrinsic latency and cost it by
> its body.

---

## Serialization (BIR-JSON)

`InstDynamicForLoop::toJson` / `createFromJson` round-trip the axis as a nested object keyed `"DynamicForLoopAxis"` with four members; the loop body is the standard `NamedObjectContainer<BasicBlockHolder,BasicBlock>` child list.

```json
{
  "DynamicForLoopAxis": {
    "name":   "<induction-axis name>",
    "lb":     "<QuasiAffineExpr>",   // verifier requires 0 terms (constant)
    "ub":     "<QuasiAffineExpr>",   // 1 term, IntRuntimeValue -> register (runtime trip)
    "stride": "<QuasiAffineExpr>"    // verifier requires 0 terms (constant)
  }
  /* ... loop body basic blocks ... */
}
```

`createFromJson` reads `obj.at("DynamicForLoopAxis")`, then `axis.at("lb")` / `axis.at("ub")` / `axis.at("stride")` (each parsed by `QuasiAffineExpr::createFromJson`), calls `setAxis(name, lb, ub, stride)`, and deserializes the body via the `BasicBlockHolder` container's `adl_serializer`. The human-readable debug dump (`writeDebugOutput`) renders the loop as `for(<axis>: range(<lb>, <ub>, <stride>)) { <body> }` — confirming the lb/ub/stride triple one more time.

---

## Related Components

| Name | Relationship |
|---|---|
| `bir::InstLoop` (opcode 105) | static-trip-count sibling; same iteration engine, compile-time `getTripcount()` ceil-div, full Hwm cost model |
| `bir::DynamicForLoopAxis` | the single induction axis; `BirLoopAxis`-derived, overrides `getTripcount`/`hasRuntimeValue` |
| `bir::BirIntRuntimeValue` | the `ub` term's runtime-value index; its register (`+0x40`) is the trip-count register |
| `axisRegs` DenseMap | `DenseMap<DynamicForLoopAxis*, Register*>` — codegen-populated map that materializes the trip-count register |
| `neuronxcc::backend::CFG` | `validateTopology` / `isBackEdge` — function-wide back-edge topology validation |

## Cross-References

- [Symbolic AP / Register ALU](symbolic-ap-register-alu.md) — `axisRegs` linkage; how the trip-count register is materialized and the `coeff * reg` MULT emitted
- [Dynamic Shape Synthesis](dynamic-shape-synthesis.md) — front-end origin of the runtime trip count (dynamic-shape / data-dependent quantity → `BirDynamicForAxis`)
- [Simulator Control Flow](../bir/sim-control-sync-customop.md) — Part 7, the `birsim` instruction-visitor dispatch and `RegState` register file that `evaluateUpperBoundExpr` reads
