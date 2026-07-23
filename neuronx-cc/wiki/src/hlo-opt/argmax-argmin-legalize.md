# ArgMax / ArgMin Legalization

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (the `neuronxcc/starfish/bin/hlo-opt` ELF, cp310 build). Other versions and the cp311/cp312 builds will differ.*

## Abstract

Neuron lowers an HLO `arg-min`/`arg-max` not as a single primitive but as a **two-pass bracket** around the rest of the `hlo-opt` pipeline. A frontend emits the reduction as an `AwsNeuronArgMax` or `AwsNeuronArgMin` custom-call carrying the reduction axis in its `backend_config`. Pass #6 `legalize-aws-neuron-arg-max` (`xla::LegalizeAwsNeuronArgMax::Run` @ `0x1eecd30`) **normalizes** that custom-call — parses the axis, attaches an int64 iota constant operand, checks the element dtype against a fixed seven-type set, and re-emits a canonical custom-call. Pass #19 `lower-argminmax-custom-call` (`xla::LowerArgMinMaxCustomCall::Run` @ `0x1f06c60`) later **expands** that canonical custom-call into a pure-HLO subgraph of `iota` + `reduce` + `compare` + arithmetic index selection, then replaces all uses. The expansion engine `handleArgMinMaxCustomCall` @ `0x1f05590` does the real work; the `Run` is a matcher/dispatcher.

The interesting part is the expansion. There is **no `select` op** in the lowered graph. Instead, Neuron uses the *weighted-iota argmax idiom*: a value-reduce finds the extremum, an equality `compare` builds an "is-extremum" PRED mask, an `iota` along the axis is shifted by a large sentinel, the mask multiplies it so matching lanes dominate, and a **second** min/max-reduce selects the matching index — which a final `subtract` un-shifts. Both reduces share a trivial scalar `maximum`/`minimum` comparator computation named `reduction_subcomp`. This arithmetic shape is exactly what maps cleanly onto the DVE search primitives (`Max8`/`FindIndex8`) in [Part 2.16](../isa/dve-search-encoding.md) and the codegen max-index path in [Part 7](../bir/codegen-dve-rng-control.md): a reduce that finds the value and a second reduce that finds the index, with no data-dependent control flow.

For reimplementation, the contract is:

- **The two-stage split:** which pass parses the axis, which attaches the iota constant, which builds the iota+reduce+compare graph, and the registry order (#6 forward, #19 inverse).
- **The supported dtype set** `{F16, BF16, F32, S32, S64, U32, U64}` and the two function-static `DenseMap<PrimitiveType,string>` that encode it, plus the `NCC_EUDT001` / `NCC_EUOC001` diagnostics.
- **The exact expansion arithmetic** — opcode immediates, sentinel direction (`GetMin`/`GetMaxNonInfValue` keyed on ArgMax vs ArgMin), and why the "select" is done by multiply + subtract + second reduce rather than a `kSelect`.

| | |
|---|---|
| **Forward Run** | `xla::LegalizeAwsNeuronArgMax::Run` @ `0x1eecd30` (5556 B, registry #6, key `legalize-aws-neuron-arg-max`) |
| **Inverse Run** | `xla::LowerArgMinMaxCustomCall::Run` @ `0x1f06c60` (~460 B, registry #19, key `lower-argminmax-custom-call`) |
| **Expansion engine** | `(anon)::handleArgMinMaxCustomCall` @ `0x1f05590` (5784 B, 281 bb, 40 callees) |
| **Comparator builder** | `(anon)::addReduceMinMaxComputation` @ `0x1f04c10` (1662 B) → computation `reduction_subcomp` |
| **IR level** | XLA HLO (pre-`hlo2penguin`) |
| **Supported dtypes** | `{F16, BF16, F32, S32, S64, U32, U64}` (`retNames` DenseMap) |
| **Error codes** | `NCC_EUDT001` (fwd, unsupported dtype), `NCC_EUOC001` (inv, op/operand-config) |

> **NOTE —** despite the name "Legalize", pass #6 does **not** lower anything to primitive HLO. It is a normalizer for an already-present `AwsNeuron*` custom-call. The actual iota/reduce/compare lowering is pass #19. A reimplementer who collapses the two will attach the dtype check to the wrong stage and lose the canonical-CC handoff.

---

## Forward Pass — `LegalizeAwsNeuronArgMax::Run`

### Purpose

Canonicalize the incoming `AwsNeuronArgMax`/`AwsNeuronArgMin` custom-call: validate that its element type is one of the seven supported dtypes, parse the reduction axis from `backend_config`, materialize a `[0,1,…,n-1]` int64 iota **constant** as an extra operand (where `n` is the axis dimension), and re-emit the custom-call canonically via `ReplaceInstruction`. The numeric arg-graph is produced later by pass #19.

### Algorithm

```c
// xla::LegalizeAwsNeuronArgMax::Run  @ 0x1eecd30
StatusOr<bool> Run(HloModule* module, ExecThreads exec_threads):
    // lazy-init two function-static DenseMap<PrimitiveType, std::string>
    // (guard words @ 0x9a39250.. ; both built by the init-list ctor @ 0x1eec220)
    fnames   = { U64:"4", U32:"2", S64:"4", S32:"2" }      // index-width code; int types only
    retNames = { F32:"_f32", F16:"_f16", BF16:"_bf16",     // dtype suffix → CC target / error tag
                 S64:"_s64", S32:"_s32", U64:"_u64", U32:"_u32" }

    changed = false
    for inst in computation.MakeInstructionPostOrder():     // 0x9634ab0
        cc = dynamic_cast<HloCustomCallInstruction*>(inst)  // 0x1eecdfc
        if !cc: continue
        if cc.custom_call_target == "AwsNeuronArgMax":      // str 0x256f0f; cmp 0x1eece17
            kind = ARGMAX
        elif cc.custom_call_target == "AwsNeuronArgMin":    // str 0x23bf74; cmp 0x1eed900
            kind = ARGMIN
        else: continue

        // --- dtype gate on operand(0): range bound + 17-bit reject mask ---
        et = inst.operand(0).shape().element_type()          // operands[0] @ 0x1eece24..0x1eece32
                                                             //   (inlined-vector: test [inst+0x18],1 / cmovne)
                                                             // Shape::element_type = [Shape+0] @ 0x1eece3a
        if et > 0x10:                              continue  // cmp eax,0x10 ; ja  @ 0x1eece42/45
        if (0xfffffffffffef0cf >> et) & 1:         continue  // bt rcx,rax ; jb    @ 0x1eece52/56
        // bits CLEAR in the mask over 0..0x10 ⇒ accepted:
        //   {4,5,8,9,10,11,16} = {S32, S64, U32, U64, F16, F32, BF16}

        // --- build the int64 iota index constant ---
        n   = inst.operand(0).shape().dimensions()[axis]    // array_state @ 0x1eece75
        shp = ShapeUtil::MakeValidatedShape(S64/*5*/, {n})  // esi=5 @ 0x1eecf3a
        lit = Literal(shp)
        lit.PopulateR1<long>({0,1,…,n-1})                   // 0x1eec6d0, call @ 0x1eecfae

        // --- parse axis from backend_config (StatusOr) ---
        raw  = cc.backend_config().GetRawStringWithoutMutex()   // 0x1eecfcc, under Mutex
        axis = hilo::parseIntConfig<long>(raw)              // 0x1eecfed  (see Axis Parsing)
        if !axis.ok():
            return InvalidArgumentError(StrFormat(
                "LegalizeAwsNeuronArgMax: invalid backend_config: %s", raw))  // str 0x2fba90

        // --- diagnostic arm: NCC_EUDT001, reached from the cold blocks at 0x1eed75e.. ---
        //     (formatErrorMessage 0x1eed7da; banner 0x27a6d3; code str 0x214b51 @ 0x1eedc6d)
        msg = hilo::formatErrorMessage<PrimitiveType,PrimitiveType>(
                  ErrorCode::EUDT001, elem_type, elem_type)
        emit "[ERROR] [" "NCC_EUDT001" "] " msg

        iota    = HloInstruction::CreateConstant(lit)        // 0x1eed175 / 0x9668610
        newInst = HloInstruction::CreateCustomCall(          // 0x1eed4bf / 0x964eac0
                      result_shape, {operands…, iota},
                      target /*AwsNeuronArgMax|Min*/, /*opaque*/"",  // opaque = byte_234779 = ""
                      CustomCallApiVersion::API_VERSION_ORIGINAL/*=1*/)  // push 1 @ 0x1eed49e
        CHECK(computation->ReplaceInstruction(inst, newInst))   // 0x1eed564 / 0x963fe50
        changed = true
    return changed
```

The gate is on **`operand(0)`** — the *value* tensor — not on the custom-call's own (tuple) result shape. Both arms reach it: the `AwsNeuronArgMin` string compare at `0x1eed900` jumps back to `0x1eece24`, the head of the same operand-fetch/gate block, so ArgMax and ArgMin share one dtype filter.

The accepted set is decoded from the two-instruction idiom the compiler emitted for a seven-way `PrimitiveType` membership test:

| Step | Instruction | Address | Effect |
|---|---|---|---|
| range bound | `cmp eax,0x10` / `ja` | `0x1eece42` | reject every code above `0x10` (BF16, the numerically largest accepted type) |
| reject mask | `mov rcx,0xfffffffffffef0cf` / `bt rcx,rax` / `jb` | `0x1eece4b`–`0x1eece56` | reject every code whose mask bit is **set** |

Bits `{4,5,8,9,10,11,16}` are the only ones clear in `0xfffffffffffef0cf` at or below `0x10`, giving exactly `{S32, S64, U32, U64, F16, F32, BF16}` — the same seven codes the `retNames` map is keyed on. A rejected dtype takes the `continue` edge to `0x1eecdc0` (the post-order loop's increment): the custom-call is left untouched rather than diagnosed at this site.

> **GOTCHA — `cmp eax,0x10` at `0x1eece42` is a range bound, not a `TUPLE` compare.** `0x10` is 16 = `BF16`, the largest accepted `PrimitiveType`, and the instruction only decides "is this code inside the table's span" before the `bt` mask does the real membership test. `TUPLE` is `0x0D` (13) throughout XLA and throughout this binary — see the direct `cmp DWORD PTR [rax],0xd` tuple tests at `0x1e9f81e` (`DecomposeCCOps`) and `0x1f852e3` (`EnsureDescendingLayoutInRoot`). Reading `0x10` as a `TUPLE` sentinel both invents a 16-valued `TUPLE` and hides the dtype whitelist that is actually being applied.

### The Two dtype DenseMaps

Both are function-static `llvm::DenseMap<xla::PrimitiveType, std::string>` built once by the init-list ctor at `0x1eec220`. Keys are `PrimitiveType` enum integers (XLA `xla_data.proto`: `S32=4`, `S64=5`, `U32=8`, `U64=9`, `F16=10`, `F32=11`, `BF16=16`). The values are decoded from the little-endian immediate stores in the `Run` body; every store address is cited in the tables below.

`fnames` — 4 entries, single-char index-width code, **integer types only**:

| Key | PrimitiveType | Value | Store |
|---|---|---|---|
| 9 | U64 | `"4"` | `mov byte[…],0x34` @ `0x1eed9c8` |
| 8 | U32 | `"2"` | `0x32` @ `0x1eed9eb` |
| 5 | S64 | `"4"` | `0x34` @ `0x1eeda0e` |
| 4 | S32 | `"2"` | `0x32` @ `0x1eeda40` |

`retNames` — 7 entries, dtype suffix (feeds the canonical CC target and the error tag):

| Key | PrimitiveType | Value | dword (LE) | Store |
|---|---|---|---|---|
| 11 | F32 | `"_f32"` | `0x3233665f` | `0x1eeddbf` |
| 10 | F16 | `"_f16"` | `0x3631665f` | `0x1eedde5` |
| 16 | BF16 | `"_bf16"` | `0x3166625f`+`'6'` | `0x1eede0b` |
| 5 | S64 | `"_s64"` | `0x3436735f` | `0x1eede38` |
| 4 | S32 | `"_s32"` | `0x3233735f` | `0x1eede5e` |
| 9 | U64 | `"_u64"` | `0x3436755f` | `0x1eede8b` |
| 8 | U32 | `"_u32"` | `0x3233755f` | `0x1eedebf` |

The supported element dtypes for arg-min/max are therefore exactly **{F16, BF16, F32, S32, S64, U32, U64}** — the same seven codes the `0x1eece42` accept mask lets through, which is the independent confirmation that the mask and the map encode one dtype contract. The `NCC_EUDT001` "unsupported dtype" diagnostic is emitted from the cold blocks around `0x1eed75e`–`0x1eedc94`; the mask gate itself is a silent `continue`, so which of the two arms fires for a given rejected type is not pinned by this trace. The `fnames` width code (`"2"`/`"4"`) exists only for the four integer types — it selects the index-arithmetic width and is irrelevant for the three float value types (which still index in S32, see below).

> **GOTCHA —** `fnames` and `retNames` share the same ctor (`0x1eec220`) but are **two distinct maps with different key sets**. `fnames` has 4 keys (int only); `retNames` has 7 (int + float). Treating them as one map — or assuming the float types have a width code — is wrong: floats are absent from `fnames` by design.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `xla::LegalizeAwsNeuronArgMax::Run` | `0x1eecd30` | 5556 B | forward normalizer | CERTAIN |
| `…ArgMax::Run` cold | `0x1eecab6` | — | EH landing pad | CERTAIN |
| `hilo::parseIntConfig<long>` | `0x1eeb770` | 344 B | axis = strtol(backend_config), isInteger-guarded | CERTAIN |
| `hilo::isInteger(const string&)` | `0x1eeb820` | — | base-10 integer validator | HIGH |
| `hilo::formatErrorMessage<PrimitiveType,PrimitiveType>` | `0x1eeb8d0` | 816 B | NCC structured error (lookup_cause/_resolution) | CERTAIN |
| `DenseMap<PrimitiveType,string>::DenseMap(init_list)` | `0x1eec220` | 863 B | dtype→suffix map ctor (used twice) | CERTAIN |
| `LiteralUtil::CreateR0<long>` | `0x1ed73c0` | 459 B | scalar int64 literal | CERTAIN |
| `MutableLiteralBase::PopulateR1<long>` | `0x1eec6d0` | 998 B | fill R1 iota literal 0..n-1 | CERTAIN |

---

## Inverse Pass — `LowerArgMinMaxCustomCall::Run`

### Purpose

Match every canonical `AwsNeuronArgMax`/`AwsNeuronArgMin` custom-call and dispatch it to the expansion engine. The `Run` body is a pure matcher/dispatcher; it carries no expansion logic of its own. Its only state is the per-match `(HloInstruction*, HloOpcode)` pair, where the opcode is the comparator direction — `kMaximum` for ArgMax, `kMinimum` for ArgMin.

### Algorithm

```c
// xla::LowerArgMinMaxCustomCall::Run  @ 0x1f06c60
StatusOr<bool> Run(HloModule* module, ExecThreads exec_threads):
    CHECK(module->entry_computation() != nullptr)            // str 0x2108c9; fatal @ 0x1f06e48
    worklist = vector<pair<HloInstruction*, HloOpcode>>
    for inst in computation.MakeInstructionPostOrder():      // 0x1f06cb2
        cc = dynamic_cast<HloCustomCallInstruction*>(inst)   // 0x1f06cee
        if cc && cc.target == "AwsNeuronArgMax":             // str ref @ 0x1f06cf3
            worklist.emplace_back(cc, kMaximum/*0x43*/)      // mov [rbp+var_79],0x43 @ 0x1f06d26
        elif cc && cc.target == "AwsNeuronArgMin":           // str ref @ 0x1f06df0
            worklist.emplace_back(cc, kMinimum/*0x44*/)      // mov [rbp+var_79],0x44 @ 0x1f06e0a
    for (inst, opcode) in worklist:
        handleArgMinMaxCustomCall(module, computation, inst, opcode)  // 0x1f06da6
    return changed
```

The `pair.second` is the comparator-direction selector handed to the engine: **ArgMax → `kMaximum` (0x43)**, **ArgMin → `kMinimum` (0x44)**. Opcode integers are proven from the `HloOpcodeString` switch @ `0x96bb550` (case 67 = `maximum`, 68 = `minimum`, 69 = `multiply`, 114 = `subtract`). The pass collects all matches before expanding so that the post-order walk is not invalidated by the in-place rewrites. On a failure path the inverse pass tags diagnostics with `NCC_EUOC001` (str `0x27a77c`).

> **QUIRK —** the match is gathered into a worklist first, then expanded in a second loop. A reimplementer who expands inside the post-order walk will mutate the instruction list mid-iteration. The two-loop structure (collect-then-rewrite) is the safe pattern and is visible in the disasm as a `vector<pair>` built at `0x1f06d26`/`0x1f06e0a` and drained at `0x1f06da6`.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `xla::LowerArgMinMaxCustomCall::Run` | `0x1f06c60` | ~460 B | inverse matcher/dispatcher | CERTAIN |
| `…LowerArgMinMax::Run` cold | `0x1f06c28` | — | EH landing pad | CERTAIN |
| `(anon)::handleArgMinMaxCustomCall` | `0x1f05590` | 5784 B | expansion engine | CERTAIN |

---

## The HLO Expansion — `handleArgMinMaxCustomCall`

### Purpose

Consume the canonical custom-call and emit the iota + reduce + compare + arithmetic-index subgraph that replaces it. This is the only function that builds `Create*` HLO; every other function on this page either normalizes (forward), dispatches (inverse), or is a helper called from here. The sequence below follows the `Create*` call order in the disassembly, with each call address cited inline.

### Algorithm

```c
// xla::(anon)::handleArgMinMaxCustomCall(module, comp, inst, opcode)  @ 0x1f05590
//   opcode = kMaximum (ArgMax) | kMinimum (ArgMin)
handleArgMinMaxCustomCall(...):
    in        = inst.mutable_operand(0)                      // 0x1f05610  (the value tensor)
    inShape   = in.shape()
    elemT     = inShape.element_type()                       // r15d, field@+0
    axisShape = MakeValidatedShape(S64/*5*/, …)              // esi=5 @ 0x1f0565b

    // --- axis re-parse (engine does its own strtol + int32 range guard) ---
    raw = inst.backend_config().GetRawStringWithoutMutex()   // 0x1f056bc, under Mutex
    errno = 0; axis = strtol(raw, &endptr, 10)               // 0x1f056f4
    if endptr == raw:                  goto error            // no digits   → NCC_EUOC001
    if (0x80000000 + axis) >> 32 != 0: goto error            // out of int32 range
    if *endptr == '"':                 goto error
    redShape = getReducedShape(inShape, axis)                // 0x1f057a0  (drops axis dim)
    cmpComp  = addReduceMinMaxComputation(module, opcode, elemT)  // 0x1f05807  → "reduction_subcomp"

    // ===== Stage A: find the extremum value =====
    // sentinel inverts vs direction: ArgMax reduces from -inf-ish, ArgMin from +inf-ish
    init   = constant( opcode==kMaximum ? GetMinNonInfValue(elemT)   // 0x1f06820 (ArgMax)
                                        : GetMaxNonInfValue(elemT) )  // 0x1f0582d (ArgMin)
                                                                      // CreateConstant 0x1f0583c
    redVal = reduce(in, init, dims={axis}, cmpComp)          // CreateReduce 0x1f0595c (r9d=1 dim)
    bVal   = broadcast(redVal, inShape)                      // CreateBroadcast 0x1f059e8
    mask   = compare(in, bVal, kEq/*0*/)                     // CreateCompare 0x1f05b9f (r8d=0)
                                                             //   PRED[inShape], true where in==extremum
    maskI  = convert(mask -> S32/*4*/)                       // CreateConvert 0x1f05c0a

    // ===== Stage B: select the matching index, arithmetically =====
    iota   = iota(MakeValidatedShape(S32, inShape.dims()), iota_dim=axis)  // CreateIota 0x1f05d9e (esi=4)
    iotaC  = convert(iota -> working_dtype)                  // CreateConvert 0x1f05e09
    big    = BroadcastScalar(inShape, large_sentinel, addFn) // BroadcastScalar 0x1f05f46
    t1     = subtract(iotaC, big)            // kSubtract 0x72  // CreateBinary 0x1f06051 (edx=0x72 @ 0x1f0603e)
    t2     = multiply(maskI, t1)             // kMultiply 0x45  // CreateBinary 0x1f06127 (edx=0x45 @ 0x1f06114)
                                             //   = (iota-BIG) on matching lanes, 0 elsewhere
    idxCmp = addReduceMinMaxComputation(module, opcode, S32working)  // 2nd comparator (esi=0x43 @ 0x1f061f0)
    big2   = BroadcastScalar(idxShape, large_sentinel, addFn)        // BroadcastScalar 0x1f06471
    idxRed = reduce(t2, idxInit, dims={axis}, idxCmp)        // CreateReduce 0x1f06357 (r9d=1)
                                                             //   max/min over the masked iota → index
    t3     = subtract(idxRed, big2)          // kSubtract 0x72  // CreateBinary 0x1f06568 (edx=0x72 @ 0x1f06559)
                                                             //   undo the sentinel shift → true index
    out    = convert(t3 -> result_index_dtype)              // CreateConvert 0x1f06649
    inst.ReplaceAllUsesWith(out, /*name*/"")               // 0x1f0672b (name = byte_234779 = "")
    // every new instruction inherits cc.metadata():
    //   OpMetadata::CopyFrom (0x9861880) @ 0x1f05a84, 0x1f05c97, 0x1f05e96, 0x1f05fd2, …
    //   added via HloComputation::AddInstruction(uptr, "")  (0x96370d0)
```

### Why it has no `select`

The "select the index whose value equals the extremum" step is implemented **arithmetically**, not with a `kSelect` op — there is no `kSelect` immediate anywhere in the engine. The idiom (Stage B) is:

1. Build an S32 `iota` along the reduction axis: lane `i` holds `i`.
2. Shift it down by a large sentinel: `iota - BIG`. Now every lane is a large negative number, ordered by index.
3. Multiply by the PRED-derived S32 mask: matching lanes keep `(iota-BIG)`, non-matching lanes become `0`. Because `iota-BIG` is far below `0`, **every matching lane dominates every non-matching lane under a `max`-reduce** (and the mirror holds under `min`).
4. A **second** min/max-reduce (its own `reduction_subcomp`, built from the same `opcode`) picks the extremal masked value — i.e. the matching index, shifted.
5. A final `subtract` removes the sentinel offset, recovering the true index.

The `GetMin`/`GetMaxNonInfValue` sentinels keep the value-reduce identity off ±Inf so the comparator never has to reason about infinities. The whole shape is data-flow only: two reduces, a compare, an iota, three converts, two broadcast-scalars, and three binary ops — **no branch on tensor data**. This is precisely why it lowers onto the DVE `Max8`/`FindIndex8` search primitives (Part 2.16): the value-reduce is the `Max8` tree and the masked-iota index-reduce is the `FindIndex8` companion.

### Op Tally

| Op | HloOpcode | Immediate | Site | Role |
|---|---|---|---|---|
| value comparator | `kMaximum`/`kMinimum` | 0x43 / 0x44 | dispatcher 0x1f06d26 / 0x1f06e0a | Stage A reduce |
| index comparator | `kMaximum`/`kMinimum` | esi=0x43 @ 0x1f061f0 | 2nd `addReduceMinMaxComputation` | Stage B reduce |
| `t1 = iotaC - BIG` | `kSubtract` | edx=0x72 @ 0x1f0603e | CreateBinary 0x1f06051 | sentinel shift |
| `t2 = maskI * t1` | `kMultiply` | edx=0x45 @ 0x1f06114 | CreateBinary 0x1f06127 | mask · masked iota |
| `t3 = idxRed - BIG2` | `kSubtract` | edx=0x72 @ 0x1f06559 | CreateBinary 0x1f06568 | un-shift |
| `mask = in == bVal` | — | r8d=0 (kEq) | CreateCompare 0x1f05b9f | is-extremum mask |

Plus: 2× `CreateReduce` (0x1f0595c, 0x1f06357), 1× `CreateBroadcast` (0x1f059e8), 3× `CreateConvert` (0x1f05c0a mask→S32, 0x1f05e09 iota→working, 0x1f06649 result), 1× `CreateIota` (0x1f05d9e, S32), 1× `CreateConstant` (reduce init), 2× `BroadcastScalar` sentinels (0x1f05f46, 0x1f06471).

> **NOTE —** the index comparator at `0x1f061f0` is loaded with `esi = 0x43`, the **same** direction as the value comparator (this trace is on the ArgMax path; ArgMin loads `0x44`). The second reduce uses the same min/max sense as the first; it is not fixed to `max`.

> **GOTCHA —** the "iota" in the forward pass is only an iota *literal*. `LegalizeAwsNeuronArgMax` emits exactly two builders — `CreateConstant` (the int64 index literal) and `CreateCustomCall` — and its disassembly contains no `CreateReduce`, `CreateIota`, or `CreateCompare` call site at all. The whole iota + reduce + compare graph is built in `handleArgMinMaxCustomCall` (pass #19). Attributing the expansion to the forward pass puts the dtype gate and the graph construction in the same stage, which the binary does not do.

---

## Anonymous-Namespace Helpers

### `addReduceMinMaxComputation` — the comparator builder

```c
// xla::(anon)::addReduceMinMaxComputation(HloModule*, HloOpcode op, PrimitiveType elemT)  @ 0x1f04c10
addReduceMinMaxComputation(module, op, elemT):
    name = "reduction_subcomp"                              // SSE "reduction_subcom" (xmmword_404910)
                                                            //   + 'p' byte (mov [rax+0x10],0x70 @ 0x1f04cb7)
                                                            //   declared length 0x11=17 @ 0x1f04c74
    scalarShape = MakeValidatedShape(elemT, {})            // 0x1f04d1c (rank-0)
    p0 = CreateParameter(0, scalarShape, "param0")          // 0x1f04d79 (str 0x214b7d @ 0x1f04d63)
    p1 = CreateParameter(1, scalarShape, "param1")          // 0x1f04e2a (str 0x286605 @ 0x1f04e11)
    root = CreateBinary(scalarShape, op, p0, p1)            // 0x1f04ed8; op = movzx edx,[rbp-339h] @ 0x1f04ebd
    comp = Builder.Build(root)                              // 0x1f04f26
    return module->AddEmbeddedComputation(comp)             // 0x1f04f39
```

The reduce's apply-computation is the trivial scalar `maximum(param0, param1)` (ArgMax) or `minimum(param0, param1)` (ArgMin) — the opcode is the `op` argument, so a single builder serves both directions and both stages. The computation is named `reduction_subcomp` (17 chars), built from a 16-byte SSE literal `reduction_subcom` plus a trailing `'p'`. This is the comparator that drives the DVE `Max8` tree at codegen.

### `getReducedShape` — drop the reduction axis

```c
// xla::(anon)::getReducedShape(Shape in, long axis)  @ 0x1f04490
getReducedShape(in, axis):
    dims = SmallVector<uint>{}
    for i in 0 .. in.rank()-1:
        if i == axis: continue                              // cmp rbx,r13; jz @ 0x1f044e0
        dims.push_back(in.dimensions()[i])                  // array_state 0x97d18e0; grow_pod 0x9521440
    return ShapeUtil::MakeValidatedShape(in.element_type(), dims)
```

Returns the post-reduction shape, reused as both the `reduce` result shape and (via its element type) the comparator scalar type.

### `BroadcastScalar` — materialize a broadcast sentinel constant

```c
// xla::(anon)::BroadcastScalar(Shape out, long value, std::function<HloInstruction*(uptr)>& addFn)  @ 0x1f048e0
BroadcastScalar(out, value, addFn):
    lit    = LiteralUtil::CreateR0<long>(value)             // 0x1f0491c
    c      = CreateConstant(lit)                            // 0x1f0492b
    cAdded = addFn(c)        // AddInstruction closure; std::bad_function_call if empty  // 0x1f04943
    return CreateBroadcast(out, cAdded, /*bcast_dims*/{})   // 0x1f0498f
```

`addFn` is the captured `{lambda(uptr)#1}::_M_invoke` (`0x1f04a00`) that forwards to `HloComputation::AddInstruction`. `BroadcastScalar` materializes the large-negative index sentinel (`BIG`/`BIG2`) broadcast to `inShape`/`idxShape` for the Stage-B arithmetic. (`0x208` = 520 = `sizeof(HloInstruction)`, seen as the `operator delete` size.)

### `GetMaxNonInfValue` / `GetMinNonInfValue`

`xla::hlo_utils::GetMaxNonInfValue(PrimitiveType)` @ `0x1ed3b20` and `GetMinNonInfValue` @ `0x1ed3d00` (401 B each) return the dtype-specific **finite** (non-±Inf) extremum used as the value-reduce identity. The selection inverts the direction: **ArgMax uses `GetMinNonInfValue`** (so any real value wins the `max`-reduce), **ArgMin uses `GetMaxNonInfValue`**. The branch selector is keyed on `opcode == kMaximum (0x43)` at `0x1f0582d` / `0x1f06820`.

---

## Axis Parsing — Two Independent Implementations

The two passes each parse the reduction axis from `backend_config`, with **different** parsers:

```c
// hilo::parseIntConfig<long>(const std::string& s)  @ 0x1eeb770  (forward only)
StatusOr<long> parseIntConfig<long>(s):
    if hilo::isInteger(s):                                  // 0x1eeb795
        return (long) strtol(s.c_str(), nullptr, 10)        // 0x1eeb83a (base 10, cdqe sign-ext)
    return WithLogBacktrace(InvalidArgumentError(
               StrFormat("backend_config is invalid")))      // str 0x2865eb @ 0x1eeb7a2
```

- **Forward** (`LegalizeAwsNeuronArgMax`) wraps `parseIntConfig<long>` and, on the not-ok branch, emits the outer `"LegalizeAwsNeuronArgMax: invalid backend_config: %s"` (`0x2fba90`).
- **Inverse** (`handleArgMinMaxCustomCall`) does **not** call `parseIntConfig`. It inlines its own `strtol` + **int32-range guard** `((0x80000000 + axis) >> 32 != 0)` + a `*endptr == '"'` trailing-quote check, and on failure tags `NCC_EUOC001`.

> **GOTCHA —** the two axis parsers can disagree. `parseIntConfig` (forward) accepts any 64-bit integer that `isInteger` validates; the inverse engine additionally rejects anything outside int32 range. A `backend_config` axis that passes #6 but exceeds int32 will be rejected at #19 with `NCC_EUOC001`, not at #6. Keep both guards if you reimplement.

---

## Error / String Catalog

All strings verified against `hlo-opt` `strings.json` / `rodata.bin` (`.rodata` VMA base `0x20c940`; note VA ≠ raw file offset — `.rodata` file_off = VA − 0x200000, so the VMA base maps to file offset `0xc940`).

| Addr | String | Used by |
|---|---|---|
| `0x256f0f` | `AwsNeuronArgMax` (custom_call_target) | both stages |
| `0x23bf74` | `AwsNeuronArgMin` (custom_call_target) | both stages |
| `0x25af80` | `legalize-aws-neuron-arg-max` (pass key) | fwd registry |
| `0x27a7a7` | `lower-argminmax-custom-call` (pass key) | inv registry |
| `0x2fba90` | `LegalizeAwsNeuronArgMax: invalid backend_config: %s` | fwd axis fail |
| `0x2865eb` | `backend_config is invalid` | `parseIntConfig` fail |
| `0x214b51` | `NCC_EUDT001` (unsupported data type) | fwd dtype gate |
| `0x27a77c` | `NCC_EUOC001` (op/operand-config) | inv failure path |
| `0x27a6d3` / `0x25f073` | `[ERROR] [` / `] ` (NCC banner) | both |
| `0x20e407` | `%s dtype is not supported.` (likely EUDT cause) | fwd (via `lookup_cause`) |
| `0x404910`(+`'p'`) | `reduction_subcomp` (comparator name) | `addReduceMinMaxComputation` |
| `0x214b7d` / `0x286605` | `param0` / `param1` | comparator params |
| `0x234779` | `""` (empty name-hint / opaque) | both |
| `0x2108c9` | `nullptr != entry_computation_` (CHECK) | inv |
| `0x2b7ff8` | `computation->ReplaceInstruction(inst, newInst)` (CHECK) | fwd |
| `0x2abed0` | `hilo/hlo_passes/LegalizeAwsNeuronArgMax.cc` (source path) | fwd |

The structured NCC diagnostics route through `hilo::formatErrorMessage<…>(ErrorCode, …)` @ `0x1eeb8d0`, which calls `hilo::lookup_resolution(ErrorCode)` (@ rel `0x1eeb907`) and `hilo::lookup_cause(ErrorCode)` (@ rel `0x1eeb916`) — a code → {cause, resolution} table keyed by the `hilo::ErrorCode` enum. `EUDT001` and `EUOC001` are members of that enum.

> **NOTE —** `NCC_EUOC001` is cited from `strings.json` (`0x27a77c`) as the inverse-pass diagnostic tag; its exact xref inside the engine disasm was not isolated in this trace (the failure-path block is reached via the `goto error` chains at `0x1f069ef`/`0x1f06a1c`). The string and its association with the inverse pass are direct readings; the precise emit site is not [UNRESOLVED].

---

## Evidence summary

| Claim | Anchor |
|---|---|
| Forward and inverse `Run` addresses and roles | demangled symbols `_ZN3xla23LegalizeAwsNeuronArgMax3RunE…_0x1eecd30` and `_ZN3xla24LowerArgMinMaxCustomCall3RunE…_0x1f06c60`; the role split follows from the `Create*` inventory — forward has only `CreateConstant`/`CreateCustomCall`, inverse `Run` only matches and calls the engine |
| The expansion is subtract/multiply/subtract with no `kSelect` | `mov edx,0x72` @ `0x1f0603e` and `0x1f06559` (kSubtract), `mov edx,0x45` @ `0x1f06114` (kMultiply); no `kSelect` immediate occurs anywhere in the engine |
| Two reduces, two comparators, same direction | `CreateReduce` @ `0x1f0595c` and `0x1f06357` (both `r9d=1`, single dim); `addReduceMinMaxComputation` called twice, the second loading `esi=0x43` @ `0x1f061f0` — the same `kMaximum` the dispatcher used for the value comparator on the ArgMax path |
| Supported dtype set `{F16,BF16,F32,S32,S64,U32,U64}` | all seven `retNames` dword stores at `0x1eeddbf`..`0x1eedebf`; the four `fnames` byte stores (`4`/`2`) at `0x1eed9c8`..`0x1eeda40` |
| The forward gate is a dtype whitelist on `operand(0)`, not a `TUPLE` assert | operand fetch `test BYTE PTR [r15+0x18],0x1` / `cmovne` @ `0x1eece28`, `HloInstruction::shape` @ `0x1eece35`, `element_type = [Shape+0]` @ `0x1eece3a`; bound `cmp eax,0x10` @ `0x1eece42`; mask `mov rcx,0xfffffffffffef0cf` / `bt rcx,rax` @ `0x1eece4b`; clear bits ≤ `0x10` are `{4,5,8,9,10,11,16}` |
| `TUPLE` is `0x0D`, not `0x10` | `cmp DWORD PTR [rax],0xd` @ `0x1e9f81e` (`DecomposeCCOps`) and `0x1f852e3` (`EnsureDescendingLayoutInRoot`), both immediately after `xla::HloInstruction::shape` @ `0x9650370`; `BF16 = 0x10` from the `retNames` key set above |
| Sentinel direction: ArgMax → `GetMinNonInfValue`, ArgMin → `GetMaxNonInfValue` | `GetMaxNonInfValue` call @ `0x1f0582d` (ArgMin branch), `GetMinNonInfValue` @ `0x1f06820` (ArgMax branch), branch keyed on `opcode == 0x43` |

## Limits of this reading

- The exact `NCC_EUOC001` emit site inside the engine was not isolated; the failure path is reached through the `goto error` chains at `0x1f069ef` / `0x1f06a1c`.
- Whether the inverse engine consumes the forward pass's int64 iota *constant* operand, or ignores it and rebuilds an S32 `iota` fresh, is unresolved. The engine reads `operand(0)` (the data tensor) and does build its own S32 iota at `0x1f05d9e`, which leaves the constant's fate open.
- The precise broadcast-dimension spans are reconstructed; the set of ops emitted is read directly.
- No decompiler output exists for either `Run` or the engine (`NVOPEN_IDA_SKIP_DECOMPILE`), so the statement order above follows the `Create*` call order in the disassembly rather than recovered source structure.

---

## Related Passes

| Order | Pass | Relationship |
|---|---|---|
| 6 | `legalize-aws-neuron-arg-max` | forward normalizer (this page) |
| 19 | `lower-argminmax-custom-call` | inverse expander (this page) |
| — | other `ccops-decompose-legalize` passes | sibling custom-call legalizers in the same family |

## Cross-References

- [DVE Search Primitives — Max8 / FindIndex8](../isa/dve-search-encoding.md) — the hardware search engine this two-reduce idiom lowers onto (value-reduce → `Max8`, masked-iota index-reduce → `FindIndex8`)
- [Codegen Max-Index Path](../bir/codegen-dve-rng-control.md) — Part 7 codegen of the lowered reduce/iota graph onto the DVE
- [CC-Op Decompose & Legalize Family](ccops-decompose-legalize.md) — sibling `AwsNeuron*` custom-call legalizers and the shared `hilo::formatErrorMessage` / `ErrorCode` diagnostic machinery
- [The hlo-opt Pass Registry](pass-registry.md) — registry positions #6 and #19, `RegisterHiloHloPasses`
