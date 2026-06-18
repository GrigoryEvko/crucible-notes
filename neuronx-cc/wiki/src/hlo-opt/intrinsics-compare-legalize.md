# Intrinsics & Compare Legalization

> *All addresses on this page apply to `neuronx-cc` 2.24.5133.0+58f8de22 (the `neuronxcc/starfish/bin/hlo-opt` binary, cp310 build). Other versions will differ. `hlo-opt` was built with decompilation suppressed, so every claim below is anchored to the disassembly, the string pool, the static-initializer, or a recovered symbol — control-flow shape is MEDIUM confidence, evidence rows are HIGH–CERTAIN.*

## Abstract

Two of the earliest rewrite passes in the `hlo-opt` HLO pipeline turn front-end abstractions into the concrete forms the Neuron back-end can lower. **LegalizeIntrinsics** (registry order 5 — the first genuine rewrite after the trivial tuple-simplifier / call-inliner / DCE / stub block) is the *activation-intrinsic lowering* pass: it scans the entry computation for a fixed set of abstract `AwsNeuron…` custom-calls and re-emits each as a **dtype-suffixed** `AwsNeuron<X>_{f16,f32,bf16}` custom-call carrying a flattened-element-count S32 constant as a second operand. It also strips a single see-through marker, `AwsNeuronTransferWithStaticRing`, replacing it with its input. **LegalizeCompare** (registry order 13) is the *comparison-Type rectifier*: it forces every `kCompare`'s stored comparison `Type` to the XLA canonical default for the operand's element type, while preserving the comparison Direction and both operands.

Neither pass touches embedded, while, or fusion computations — both walk `entry_computation()` only, in `MakeInstructionPostOrder()` order, guarded by the upstream `CHECK(nullptr != entry_computation_)`. Both are plain `HloPass` subclasses (their own `Run` at vtable slot +0x18), not `OpExpander`s. They sit early because everything downstream — layout assignment, the tensorizer, native-kernel selection — assumes activations already wear their dtype kernel selector and that no compare carries a Type the back-end cannot map.

The shape of each pass differs in an instructive way. LegalizeIntrinsics is a **collect-then-execute** pass: the post-order loop never mutates the graph inline; it pushes a `std::function<void()>` closure per match into a heap vector, then drains the vector after the traversal. This is the standard XLA iterator-safety idiom — rewriting an instruction inside the post-order walk would invalidate the iterator. LegalizeCompare is small enough (one rebuild per non-canonical compare) that it rewrites inline. The reader who has implemented an XLA `HloModulePass` will recognize both structures immediately; the Neuron-specific content is entirely in the **dispatch tables** (two `std::set<std::string>`) and the **target-name / operand encoding convention**.

For reimplementation, the contract is:

- The two LegalizeIntrinsics dispatch sets — `to_remove` (1 entry, bypass) and `target_names` (6 entries, activation lowering) — built once at static-init and probed by `std::_Rb_tree<string>::find`.
- The per-dtype target-name construction (`base + "_f16" | "_f32" | "_bf16"`, bare fallthrough otherwise) and the second-operand S32 flattened element count.
- The custom-call contract: empty `opaque`, `api_version = 1`, operands `{op0, S32 const{n}}` — a *different* encoding from the softmax family, which round-trips its reduce-axis through a decimal-string backend_config.
- The LegalizeCompare rule: `comparison_type_ := DefaultComparisonType(operand(1).element_type)`, direction preserved, idempotent skip-if-already-default.

| | |
|---|---|
| **LegalizeIntrinsics class / vtable** | `xla::LegalizeIntrinsics` / `_ZTVN3xla18LegalizeIntrinsicsE` @ `0x40d368` |
| **LegalizeIntrinsics `Run`** | `0x1ef4080` (2491 B, 117 bb, two-pass collect-then-execute) |
| **`Run` signature** | `Run(HloModule*, flat_hash_set<string_view> const& execution_threads)` |
| **LegalizeCompare class / vtable** | `xla::LegalizeCompare` / `_ZTVN3xla15LegalizeCompareE` @ `0x40d080` |
| **LegalizeCompare `Run`** | `0x1ef25f0` (822 B, 41 bb, inline rewrite) |
| **Dispatch sets** | `xla::target_names` @ `0x9a39400` (6), `xla::to_remove` @ `0x9a39440` (1) |
| **Set builder** | `__static_initialization_and_destruction_0` @ `0x1ef3d30` |
| **Set probe** | `std::_Rb_tree<string>::find` @ `0x1ef3680` |
| **Registry order** | LegalizeIntrinsics = 5, LegalizeCompare = 13 |
| **IR level** | HLO (XLA `HloInstruction` graph), pre-layout, pre-tensorizer |

---

## LegalizeIntrinsics (order 5)

### Purpose

Lower the six abstract Neuron activation custom-calls emitted by the front-end into the concrete, dtype-specialized custom-calls the tensorizer/native-kernel layer dispatches on, and erase the one transfer-staging marker (`AwsNeuronTransferWithStaticRing`) that exists only to be seen through at the HLO layer. Everything the back-end needs — which activation, which dtype kernel, how many elements — is conveyed *structurally* (target-name suffix + a second S32 operand), with an **empty** backend_config.

### Entry Point

```text
xla::LegalizeIntrinsics::Run                    0x1ef4080   ── entry-computation post-order scan, collect-then-execute
  ├─ HloComputation::MakeInstructionPostOrder    0x9634ab0  ── traversal order
  ├─ std::_Rb_tree<string>::find                 0x1ef3680  ── probe to_remove / target_names
  ├─ {lambda#1}::operator()                      0x1ef4aa0  ── to_remove handler: replace with operand(0)
  │    └─ HloComputation::ReplaceInstruction                ── (assert str 0x3b72e0)
  └─ {lambda#2}::operator()                      0x1ef4c40  ── target_names handler: emit AwsNeuron<X>_<dtype>
       ├─ Shape::set_element_type(S32=4)         0x80e3b70
       ├─ HloInstruction::CreateConstant         0x9663f10  ── scalar S32 literal = n
       ├─ HloInstruction::CreateCustomCall       0x964ebc0  ── {op0, const}, opaque="", api=1
       └─ HloComputation::ReplaceInstruction                ── (assert str 0x2e46c8)
```

### Algorithm

```c
// xla::LegalizeIntrinsics::Run  @ 0x1ef4080
StatusOr<bool> Run(HloModule* m, const flat_hash_set<string_view>& exec_threads):
    comp = m->entry_computation();                 // [rdx+0x38]; CHECK(nullptr != entry_computation_)
                                                   //   cold-path CHECK string @ 0x2108c9
    order = comp->MakeInstructionPostOrder();      // 0x9634ab0
    vector<function<void()>> work;                 // deferred-rewrite list (iterator safety)

    for (inst : order):
        if (inst->opcode() != kCustomCall)         // 0x1ef413f: cmp byte[r13+0x14], 0x2B
            continue;
        const string& tgt = inst->custom_call_target();

        // ---- table 1: bypass markers ----
        if (to_remove.find(tgt) != to_remove.end())            // 0x1ef415d find; cmp rax,0x9a39448 (=&to_remove+8)
            work.push_back(lambda1{ inst });                   // mgr 0x1ef30d0 / invoke 0x1ef4b50
            continue;

        // ---- table 2: activation intrinsics ----
        if (target_names.find(tgt) == target_names.end())      // 0x1ef4290 find; cmp rax,0x9a39408 (=&target_names+8)
            continue;                                          //   neither set → leave instruction untouched

        Shape sh = inst->shape();                              // copy
        string name = tgt;                                     // rebuild target name locally
        switch (sh.element_type()):                            // 0x1ef42ea..0x1ef42fc
            case F32 /*0x0B*/: name += "_f32";  // len 4, str 0x23fc8f, append @0x1ef4938
            case F16 /*0x0A*/: name += "_f16";  // len 4, str 0x27e6e7, append @0x1ef48f6
            case BF16/*0x10*/: name += "_bf16"; // len 5, str 0x210919, append @0x1ef48c0
            default          : /* bare name, no suffix */;      // → 0x1ef4305

        int n = 1;                                             // flattened element count
        for (d : sh.dimensions()):                             // 0x1ef4340: imul r15d,[rax]; add rax,8
            n *= d;

        work.push_back(lambda2{ inst, comp, name, n });        // mgr 0x1ef3540 / invoke 0x1ef5080

    for (f : work): f();                                       // 0x1ef41ef: call qword[r12+0x18]
    return changed;                                            // StatusOr<bool> bool-tag: mov byte[rax+8],1
```

> **QUIRK —** the pass is two-pass on purpose. The post-order loop *records* rewrites into `work` and only *applies* them after the traversal finishes. `ReplaceInstruction` inside the walk would invalidate the post-order iterator and the still-pending `find` probes. A reimplementation that rewrites inline will appear to work on small graphs and then crash or silently skip instructions on larger ones. Keep the closure-vector.

> **NOTE —** the two `find` sentinels are the set's `_M_header`, i.e. `&set + 8`: `to_remove.end()` compares against `0x9a39448` (= `0x9a39440 + 8`) and `target_names.end()` against `0x9a39408` (= `0x9a39400 + 8`). `find() != end()` therefore means *member*. The set globals themselves are `_ZN3xlaL9to_removeE` @ `0x9a39440` and `_ZN3xlaL12target_namesE` @ `0x9a39400` (local-linkage statics).

### The two dispatch sets

`__static_initialization_and_destruction_0` @ `0x1ef3d30` constructs both `std::set<std::string>`s from `initializer_list`s and registers their teardown via `__cxa_atexit`. The set ctor's element count is the `edx` argument; each member string is a `mov esi, offset aAwsneuron…` immediately above the per-element `std::string` constructor. All seven strings are verbatim in the string pool (`.rodata`), confirmed below.

| Set | Global @ | Count | Member(s) | String @ | Role |
|---|---|---|---|---|---|
| `xla::to_remove` | `0x9a39440` | 1 | `AwsNeuronTransferWithStaticRing` | `0x2d95e8` | bypass / erase (→ operand 0) |
| `xla::target_names` | `0x9a39400` | 6 | `AwsNeuronErf` | `0x234850` | activation → dtype-suffixed CC |
| | | | `AwsNeuronGelu` | `0x23fd1e` | |
| | | | `AwsNeuronGeluBackward` | `0x25f0ff` | |
| | | | `AwsNeuronGeluApprxTanh` | `0x28257d` | |
| | | | `AwsNeuronSilu` | `0x23fd2c` | |
| | | | `AwsNeuronSiluBackward` | `0x23485d` | |

> **GOTCHA —** the set is the *whole* dispatch oracle. `AwsNeuronSoftmax*` is **not** here (the softmax family is handled by its own legalize passes that use a decimal-string backend_config). `AwsNeuronDropout` (str `0x282523`) is **not** here either — dropout is lowered by `EmitOffloadedDropout`. `AwsNeuronTopK` / `AwsNeuronArgMax` / `AwsNeuronArgMin` are **not** here — they have their own passes. A reimplementation that lowers any custom-call *not* in one of these two sets is wrong: the correct behaviour for a non-member custom-call is to leave it untouched. (These are exhaustive string-pool negative findings — HIGH.)

### The intrinsic → custom-call mapping table

This is the centerpiece. For every `target_names` member, the emitted custom-call is `<base>_<dtype>`; operands are `{ inst->operand(0), S32 const{n} }`; `opaque = ""`; `api_version = 1` (`CustomCallApiVersion::API_VERSION_ORIGINAL`); the result shape is `inst->shape()`; the rewrite is `ReplaceInstruction(inst, new)`. `n` is the **flattened element count** of the activation tensor (the product of all dimensions), which the downstream kernel reads as the vector length.

| Intrinsic (match target) | Set | Emitted custom-call | Operands | `opaque` | `api` | Conf |
|---|---|---|---|---|---|---|
| `AwsNeuronErf` | `target_names` | `AwsNeuronErf_{f16/f32/bf16}` | `{op0, S32 n}` | `""` | 1 | CERTAIN |
| `AwsNeuronGelu` | `target_names` | `AwsNeuronGelu_{f16/f32/bf16}` | `{op0, S32 n}` | `""` | 1 | CERTAIN |
| `AwsNeuronGeluBackward` | `target_names` | `AwsNeuronGeluBackward_{f16/f32/bf16}` | `{op0, S32 n}` | `""` | 1 | CERTAIN |
| `AwsNeuronGeluApprxTanh` | `target_names` | `AwsNeuronGeluApprxTanh_{f16/f32/bf16}` | `{op0, S32 n}` | `""` | 1 | CERTAIN |
| `AwsNeuronSilu` | `target_names` | `AwsNeuronSilu_{f16/f32/bf16}` | `{op0, S32 n}` | `""` | 1 | CERTAIN |
| `AwsNeuronSiluBackward` | `target_names` | `AwsNeuronSiluBackward_{f16/f32/bf16}` | `{op0, S32 n}` | `""` | 1 | CERTAIN |
| `AwsNeuronTransferWithStaticRing` | `to_remove` | *(none — bypass)*: `ReplaceInstruction(inst, op0)` | n/a | n/a | n/a | CERTAIN |

The dtype suffix is a four-case switch on the result/operand element type (XLA `PrimitiveType`). Only the three float dtypes get a suffix; everything else emits the bare base name with no kernel selector.

| `element_type` | hex | Suffix | Length | Append site |
|---|---|---|---|---|
| `F16` | `0x0A` | `_f16` | 4 (str `0x27e6e7`) | `0x1ef48f6` |
| `F32` | `0x0B` | `_f32` | 4 (str `0x23fc8f`) | `0x1ef4938` |
| `BF16` | `0x10` | `_bf16` | 5 (str `0x210919`) | `0x1ef48c0` |
| anything else | — | *(none — bare base name)* | — | default `→ 0x1ef4305` |

> **NOTE —** `_f16` (`0x27e6e7`) and `_f32` (`0x23fc8f`) are **tail-substring** pointers — they point into the suffix of longer `…_f16` / `…_f32` host strings in the pool, so they have no standalone `strings.json` entry. They were verified from the `mov esi, offset` plus the explicit `mov edx, 4` length operand in the `Run` disassembly (`0x1ef48f1`, `0x1ef4933`). `_bf16` (`0x210919`, length 5, append at `mov edx, 5` @ `0x1ef48bb`) is a standalone pool entry. So the suffix encoding is *binary-confirmed by length and offset*, not by a clean named string for two of the three.

> **QUIRK —** the bare-name fallthrough (any dtype ∉ {F16, F32, BF16}) emits e.g. `AwsNeuronGelu(op0, n)` with *no dtype kernel selector*. In practice the Neuron front-end only emits these activations in the three float dtypes, so the fallthrough is a defensive default — likely dead. A reimplementation must still implement it (the switch has an explicit default arm); do not assert-fail there. (MEDIUM that it is ever reached.)

### The activation rewrite — lambda#2 @ 0x1ef4c40

The `target_names` handler builds the lowered custom-call in three steps: a scalar S32 count constant, the dtype-suffixed custom-call taking the original activation input plus that constant, and the replacement.

```c
// {lambda#2}::operator()  @ 0x1ef4c40   (captured: inst, comp, name, n)
void emit_activation():
    // 1. flattened-count constant (scalar S32 literal = n)
    Shape cnt_shape;
    cnt_shape.set_element_type(S32 /*4*/);                 // 0x1ef4c81: mov esi,4; 0x1ef4c8d call set_element_type
    Literal lit(cnt_shape);
    lit.mutable_data<int>()[0] = n;                        // n = captured count
    HloInstruction* cnt = CreateConstant(move(lit));       // 0x1ef4d2c
    comp->AddInstruction(cnt, /*name=*/"");                // 0x1ef4d67 (empty name)

    // 2. the dtype-suffixed custom-call
    HloInstruction* cc = CreateCustomCall(                 // 0x1ef4e19
        /*shape   =*/ inst->shape(),
        /*operands=*/ Span{ inst->mutable_operand(0), cnt },   // ecx=2 operands @0x1ef4df5
        /*target  =*/ name,                                    // "AwsNeuron<X>_<dtype>"
        /*opaque  =*/ "",                                      // empty backend_config
        /*api_ver =*/ API_VERSION_ORIGINAL /*1*/);             // push 1 @0x1ef4de4
    comp->AddInstruction(cc, /*name=*/"");                 // 0x1ef4e6a

    // 3. replace
    comp->ReplaceInstruction(inst, cc);                   // 0x1ef4ebe
        // CHECK str 0x2e46c8 "computation->ReplaceInstruction(inst, customCallInst)"
```

So an `AwsNeuronGelu` of F32 type becomes `AwsNeuronGelu_f32(operand0, S32 const{n})`, `opaque=""`, `api_version=1`. (HIGH — every step is a confirmed call: `set_element_type(4)`, `CreateConstant`, `CreateCustomCall` with `ecx=2` / `push 1`, `ReplaceInstruction`, plus the verbatim assert string.)

### The bypass rewrite — lambda#1 @ 0x1ef4aa0

The `to_remove` handler is a pure passthrough — it replaces the marker with its single operand:

```c
// {lambda#1}::operator()  @ 0x1ef4aa0   (captured: inst)
void bypass():
    comp->ReplaceInstruction(inst, inst->mutable_operand(0));    // 0x1ef4adf
        // CHECK str 0x3b72e0 "computation->ReplaceInstruction(inst, inst->mutable_operand(0))"
        // cold-path CHECK-fail site: LegalizeIntrinsics.cc:22  (str 0x3cf6a0, line 0x16)
```

`AwsNeuronTransferWithStaticRing` is therefore *erased* at the HLO layer — it is a transfer/ring-staging marker the HLO optimizer must see through, and the real ring transfer is materialised later in the collective/codegen stages. (HIGH.)

> **NOTE —** a separate diagnostic string exists in this binary — `found illegally nested AwsNeuronTransferWithStaticRing, replacing ` @ `0x34bf18` — but it belongs to a *different* pass that checks for illegal nesting of the marker, not to LegalizeIntrinsics. LegalizeIntrinsics does the unconditional single-level bypass shown above; it does not emit that diagnostic.

---

## LegalizeCompare (order 13)

### Purpose

Rectify every `kCompare` instruction's stored `Comparison::Type` to the XLA canonical default for its operand element type, leaving the comparison Direction (`LT/LE/GT/GE/EQ/NE`) and both operands untouched. A front-end that emits, e.g., a `kFloatTotalOrder` float compare, or a signed/unsigned-mismatched integer compare, produces a Type the Neuron back-end cannot lower; this pass resets it to `Comparison::DefaultComparisonType(operand(1).element_type)`. The pass is idempotent — if the stored type already equals the default, it skips.

### Entry Point

```text
xla::LegalizeCompare::Run                        0x1ef25f0   ── entry-computation post-order scan, inline rewrite
  ├─ HloComputation::MakeInstructionPostOrder     0x9634ab0
  ├─ ___dynamic_cast                                          ── inst → HloCompareInstruction* (RTTI 0xd543c0/0xd53d08)
  ├─ xla::Comparison::DefaultComparisonType        0x96ced10  ── canonical Type per PrimitiveType (+ clones 0x96cec08/0x96cec70)
  ├─ HloInstruction::CreateCompare                            ── rebuild with default type, same direction
  └─ HloComputation::ReplaceInstruction                       ── (assert str 0x2ce728)
```

### Algorithm

```c
// xla::LegalizeCompare::Run  @ 0x1ef25f0
StatusOr<bool> Run(HloModule* m, const flat_hash_set<string_view>& exec_threads):
    comp = m->entry_computation();                 // [rdx+0x38]; CHECK(nullptr != entry_computation_) str 0x2108c9
    for (inst : comp->MakeInstructionPostOrder()):  // 0x9634ab0
        if (inst->opcode() != kCompare)            // 0x1ef268c: cmp byte[r13+0x14], 0x20
            continue;
        auto* cmp = dynamic_cast<HloCompareInstruction*>(inst);   // 0x1ef26a2 ___dynamic_cast

        PrimitiveType et = inst->operand(1)->shape().element_type();
        Comparison::Type want = DefaultComparisonType(et);        // 0x1ef26c1 → al
        if (cmp->comparison_type() == want)                       // 0x1ef26c6: cmp [r12+0x211], al; jz skip
            continue;                                             // already canonical → no-op (idempotent)

        Comparison::Direction dir = cmp->comparison_direction();  // 0x1ef26e3: movzx eax, byte[r12+0x208]
        HloInstruction* nw = CreateCompare(                       // 0x1ef273a
            /*shape    =*/ inst->shape(),
            /*lhs      =*/ inst->mutable_operand(0),
            /*rhs      =*/ inst->mutable_operand(1),
            /*direction=*/ dir,                                   // preserved
            /*type     =*/ optional<Comparison::Type>{ want });   // r9 = {al=want, present-bit set}
        comp->AddInstruction(nw, /*name=*/"");
        comp->ReplaceInstruction(inst, nw);                       // CHECK str 0x2ce728
            // "computation->ReplaceInstruction(inst, rectifiedCompareInst)"
```

(HIGH — opcode-0x20 test, `___dynamic_cast` to `HloCompareInstruction`, `operand(1)->shape()->element_type` feeding `DefaultComparisonType`, the `cmp [r12+0x211], al` skip-if-equal guard, `CreateCompare` with preserved direction (`+0x208`) and the rebuilt optional type, and the verbatim assert string.)

### The rectification rule and DefaultComparisonType

LegalizeCompare has **no backend_config and no dispatch table**. Its single rule:

> For every `kCompare`, if `comparison_type ≠ DefaultComparisonType(operand(1).element_type)`, rebuild via `CreateCompare(shape, op0, op1, /*same direction*/, /*default type*/)` and replace. Otherwise leave unchanged.

`xla::Comparison::DefaultComparisonType(PrimitiveType)` @ `0x96ced10` is upstream XLA, reused verbatim (two outlined clones at `0x96cec08`/`0x96cec70`). It maps:

| Operand dtype | hex / test | DefaultComparisonType | Conf |
|---|---|---|---|
| `F16` / `F32` / `F64` | `0x0A–0x0C` (`et-0x0A ≤ 2`) | `kFloat` (0) | HIGH |
| `BF16` | `0x10` (`cmp edi,0x10; jz`) | `kFloat` (0) | HIGH |
| `F8` / other float / complex `C64`,`C128` | mask `0xFFFFFFFCFFFB7FFF` via `bt rax,rdi` | `kFloat` (0) | HIGH |
| signed integers | `_part_0` clone fall-through | `kSigned` | MEDIUM |
| unsigned integers / `PRED` | `_part_0` clone fall-through | `kUnsigned` | MEDIUM |

The float/complex → `kFloat` path is what these float-heavy Neuron graphs hit, and it is HIGH-confidence (the `lea eax,[rdi-0xA]; cmp eax,2; jbe →0`, the `cmp edi,0x10; jz →0`, and the float/complex bitmask). The integer split (kSigned vs kUnsigned, via the `_part_0` outlined clone) was not disassembled line-by-line and follows upstream XLA semantics — MEDIUM on the exact PRED/unsigned predicate.

### Struct slice — HloCompareInstruction

The two field offsets are binary-confirmed from the two byte loads in `Run`:

```c
// xla::HloCompareInstruction : HloInstruction   (offsets from LegalizeCompare::Run)
struct HloCompareInstruction : HloInstruction {
    // … HloInstruction base; opcode byte at +0x14, kCompare = 0x20 …
    /* +0x208 */ uint8_t comparison_direction_;  // LT/LE/GT/GE/EQ/NE  (preserved by the rewrite)
    /* +0x211 */ uint8_t comparison_type_;        // kFloat/kFloatTotalOrder/kSigned/kUnsigned (rectified)
};
```

(HIGH — `movzx eax, byte[r12+0x208]` reads direction at `0x1ef26e3`; `cmp [r12+0x211], al` tests type at `0x1ef26c6`. The 8-byte gap holds other `HloCompareInstruction` fields not exercised here.)

---

## Custom-Call / Backend-Config Schema

The two builders are `HloInstruction::CreateCustomCall(shape, operands, target, opaque, api_version)` (LegalizeIntrinsics lambda#2 @ `0x1ef4e19`) and `HloInstruction::CreateCompare(shape, lhs, rhs, direction, optional<type>)` (LegalizeCompare @ `0x1ef273a`).

| Field | LegalizeIntrinsics (activation) | LegalizeCompare |
|---|---|---|
| op kind | CustomCall `AwsNeuron<X>_<dtype>` | Compare (rectified type) |
| operands | `{ operand(0), S32 const{n} }` (2) | `{ mutable_operand(0), mutable_operand(1) }` (2) |
| result shape | `inst->shape()` | `inst->shape()` |
| opaque / backend_config | `""` (empty) | n/a |
| api_version | `1` (`API_VERSION_ORIGINAL`) | n/a |
| extra attr | dtype suffix = kernel selector; `n` = flattened element count | direction preserved; type = `DefaultComparisonType` |
| AddInstruction name | `""` | `""` |
| replace assert (verbatim) | `…ReplaceInstruction(inst, customCallInst)` (`0x2e46c8`) / `…inst->mutable_operand(0))` (`0x3b72e0`) | `…ReplaceInstruction(inst, rectifiedCompareInst)` (`0x2ce728`) |

> **CORRECTION (vs softmax family) —** the activation intrinsics carry **no backend_config**. Unlike the softmax legalize family, which round-trips its reduce-axis through a *decimal-string* `opaque` (itoa/strtol digit-table), LegalizeIntrinsics conveys everything structurally: the **dtype lives in the target-name suffix** and the **flattened element count is a second S32 operand**. So "Neuron activation intrinsics" and "Neuron softmax" do **not** share a backend_config schema. A reimplementer who copies the softmax decimal-string convention here will produce custom-calls the activation kernels cannot bind. (HIGH — empty `opaque` confirmed at the `CreateCustomCall` site; no digit-table / strtol anywhere in `Run` or its lambdas.)

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

1. **`target_names` has exactly 6 members; `to_remove` has exactly 1.** Re-checked: the six `AwsNeuron…` activation strings (`Erf`/`Gelu`/`GeluBackward`/`GeluApprxTanh`/`Silu`/`SiluBackward`) and `AwsNeuronTransferWithStaticRing` are present in `strings.json` at the exact offsets cited; the set ctor counts (`edx=6` / `edx=1`) match. **CONFIRMED.**
2. **The opcode tests are CustomCall=0x2B (Intrinsics) and Compare=0x20 (Compare).** Re-checked the disassembly: `cmp byte[r13+0x14], 2Bh` @ `0x1ef413f` and `cmp byte[r13+0x14], 20h` @ `0x1ef268c`. **CONFIRMED.**
3. **The dtype switch maps F32=0x0B/F16=0x0A/BF16=0x10 to `_f32`/`_f16`/`_bf16`.** Re-checked: `cmp eax,0Bh` / `cmp eax,0Ah` / `cmp eax,10h` @ `0x1ef42ea..0x1ef42fc`, with the `mov edx,4/4/5` length operands at the append sites. The two `_f16`/`_f32` pointers are tail-substrings (no standalone string entry) — tagged accordingly, length-confirmed. **CONFIRMED (with the substring caveat noted in-text).**
4. **The custom-call is built with opaque="" and api_version=1, two operands.** Re-checked lambda#2 @ `0x1ef4c40`: `push 1` (api), `mov ecx,2` (operand count), `CreateCustomCall`, and the verbatim assert `0x2e46c8`. No strtol/itoa table present → empty opaque. **CONFIRMED.**
5. **LegalizeCompare reads direction at +0x208 and type at +0x211, calls `DefaultComparisonType`, and is idempotent.** Re-checked: `DefaultComparisonType` call @ `0x1ef26c1`, `cmp [r12+0x211],al; jz` skip guard, `movzx [r12+0x208]` direction load, `CreateCompare` with `optional<Type>`, assert `0x2ce728`. **CONFIRMED.**

Items left **INFERRED / MEDIUM**: (a) the exact branch nesting of the vector-growth realloc paths in `Run` (no decompile, 117 bb) — the *logic* is HIGH, the *control-flow shape* is MEDIUM; (b) the kSigned/kUnsigned split inside `DefaultComparisonType`'s `_part_0` clone (integer dtypes) — float/complex → `kFloat` is HIGH, the integer predicate follows upstream XLA; (c) whether the bare-name dtype fallthrough is ever reached at runtime (LOW — likely defensive dead code). Nothing on this page was fabricated; every address, offset, string, and enum value above resolves in the cp310 `hlo-opt` artifacts.

---

## Related Passes

| Order | Name | Relationship |
|---|---|---|
| 5 | `legalize-intrinsics` | this page — activation lowering + transfer-marker bypass |
| 6 / 8 | TopK / ArgMax legalize | sibling intrinsic-lowering passes; **not** in either dispatch set here |
| 7 / 41 / 42 | Softmax family | sibling activation lowering with a *different* (decimal-string backend_config) convention |
| 13 | `legalize-compare` | this page — comparison-Type rectifier |
| 36 | `EmitOffloadedDropout` | lowers `AwsNeuronDropout` (deliberately absent from `to_remove`/`target_names`) |

## Cross-References

- [The hlo-opt Pass Registry (the --passes Table)](pass-registry.md) — where orders 5 and 13 sit in the 112-row registry, and how `Run` is dispatched.
- [Activation Engine — Datapath and the LUT-Load Mechanism](../arch/activation-engine.md) — the hardware/kernel side of `AwsNeuron<X>_{f16,f32,bf16}`; what the suffixed targets ultimately compute (Part 10 / activation family).
- [HLO → Native / NKI Kernel Lowering](hlo-to-native-kernel-lowering.md) — 4.33; the tensorizer/native-kernel binding that consumes the dtype-suffixed custom-call and the S32 element-count operand.
- [HLO/mhlo/stablehlo Ingestion & the Stock-vs-Neuron Boundary](hlo-ingestion-boundary.md) — how the abstract `AwsNeuron…` custom-calls enter the graph before this pass lowers them.
