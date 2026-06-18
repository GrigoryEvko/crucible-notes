# The Three-Sink Kernel-Node Model

> *All symbols, addresses, strings, and enum values on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The codegen sinks live in `BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (`neuronxcc/starfish/penguin/targets/codegen/`, Cython `-O3 -fwrapv -fPIC -g`, UNSTRIPPED with DWARF). The three BIR node classes (`bir::InstBIRKernel` / `bir::InstNKIKernel` / `bir::InstNKIKLIRKernel`) and their `InstructionType` enum values live in the native `libwalrus` / BIR runtime; the downstream resolvers are in `libwalrus`. cp311/cp312 share the `__pyx` method roster; addresses are the cp310 frame. Provenance: report D-P13.*

## Abstract

When the Penguin tensoriser lowers a macro-kernel — a flash-attention, a fused MLP, an RMSNorm+QKV projection — into Backend IR, it does **not** re-emit the inner GEMM/softmax/normalize math. It emits one BIR *kernel node*. But there is not one kind of kernel node; there are **three**, each a distinct C++ class deriving from `bir::Instruction`, each carrying a distinct `InstructionType` ordinal, and each built by a *different* codegen method inside `BirCodeGenLoop`:

- **`InstBIRKernel`** (`InstructionType = 54`) — a **library** kernel node, keyed by a string `kernel_name`. It carries the name, an attribute bag, and operand shapes — but **no binary, no version, no trace**. The compiled `_private_kernels.<leaf>.so` BIR is spliced in **by name** much later, by `InlineBIRKernel` in `libwalrus`. This is the sink for every production model-fusion macro: MLP, Attention, QKV, RMSNormQuant, BackwardsAttention, and every generic library kernel.
- **`InstNKIKernel`** (`InstructionType = 55`) — the **user** `@nki.jit` external-kernel node. It carries `func`/`func_args`/`func_outs` for a kernel the user wrote and the front end already AOT-compiled. Out of scope for the model-fusion macros, but it is the second of the three sinks and is documented here for completeness.
- **`InstNKIKLIRKernel`** (`InstructionType = 56`) — the **registry-traced carrier**. It is the *only* node that embeds a freshly-traced kernel binary (`klir_binary` + `kernel_format` + `nki_binary_version_identifier`). It is built only for the kernels in `_INTERNAL_KERNEL_REGISTRY` — convolution, resize, blockwise-MM, transpose, select-and-scatter — whose BIR must be **regenerated** from NKI source at codegen time.

This page documents the BIR-node side of the macro lowering: which `codegen<Name>` method emits which node, why the production model macros land in the cheap name-inlined `InstBIRKernel` sink rather than the expensive registry-traced `InstNKIKLIRKernel` sink, and how the bespoke-vs-generic codegen split (already documented on the emit side in [NeuronCodegen Macro-Kernel Emitters](neuroncodegen-macro.md)) is the *same* boundary viewed from the node side. The registry's build/resolve mechanism itself — `_build_internal_kernel_registry`, `InternalKernelConfig`, `_resolve_kernel_config`, the cache, the beta2/beta3 trace leaves — is the subject of the next page, [The Internal Kernel Registry & New-NKI-Frontend Re-Trace](internal-kernel-registry.md); here we only show *which* sink it feeds.

For reimplementation, the contract is:

- **The three node classes and their ordinals** — the `InstructionType` enum values 54/55/56, the `Opcode.{BIRKernel,NKIKernel,NKIKLIRKernel}` getattr each codegen performs, and what each node carries.
- **The sink-selection rule** — which `codegen<Name>` method runs for which macro family, and the single fact (`Opcode.BIRKernel` vs `Opcode.NKIKLIRKernel`) that separates the name-inlined library path from the registry-traced path.
- **The bespoke-vs-generic split** — why MLP/Attn/QKV/RMSNorm have their own `codegen<Name>` methods while RouterTopK/ExpertMLPs/RowTiledMM lower through the generic `codegenBIRKernel`, and why *both* still emit `InstBIRKernel` (IT54).
- **The operand-access marshalling** — the per-operand access-pattern dispatcher (`codegenBIRKernelAccess`) and the tile/sub-tensor AP builders that are the *only* per-macro work the lowerers do.

| | |
|---|---|
| **Three node classes** | `bir::InstBIRKernel` (IT54), `bir::InstNKIKernel` (IT55), `bir::InstNKIKLIRKernel` (IT56) — all direct `bir::Instruction` subclasses, "Custom/Extension" category |
| **Enum proof** | BIR `InstructionType`: `…CustomOp=53, BIRKernel=54, NKIKernel=55, NKIKLIRKernel=56, DevicePrint=57…`; `InstBIRKernel` ctor literally calls `Instruction(name, bb, /*InstructionType=*/54)` |
| **IT54 sinks** | `codegenBIRKernel` @ `0xa8360` (#221, generic); `_commonNativeKernelCodegen` @ `0x171a70` (#225, MLP/QKV/RMSNorm); `_commonAttentionKernelCodegen` @ `0x1140a0` (#217, attention); `codegenBackwardsAttention` @ `0x1e02c0` (#223, inline) |
| **IT55 sink** | `codegenExternalNativeNkiKernel` @ `0x1a4e60` (#231) |
| **IT56 sink** | `codegenInternalNativeNkiKernel` @ `0x8d630` (#243) — the only registry-traced path |
| **Sink discriminator** | the `Opcode.<X>` getattr in each body: `Opcode.BIRKernel` (IT54) / `Opcode.NKIKernel` (IT55) / `Opcode.NKIKLIRKernel` (IT56) |
| **IT54 downstream** | `InlineBIRKernel::run` @ `0xd86510` (libwalrus) scans IT54 nodes by `name@+0xF0`, expands via `BIRKernelWrapper::createInstance` @ `0xd855d0` — no JSON round-trip, no KLR walk |
| **IT56 downstream** | `TranslateNKIASTToBIR::lowerKernelInst` @ `0xf0b610` (libwalrus) reads `kernel_format@+0x110`; FORM A = KLR-AST file, FORM B (`=="bir"`) = embedded BIR-JSON via `lowerFromBirJson` @ `0xf0a160` |

---

## 1. Why three nodes, not one

The natural model — and the one an earlier pass of this analysis assumed — is that *every* macro kernel funnels through a single trace driver: `codegen<Name>` → `codegenInternalNativeNkiKernel` → `_resolve_kernel_config` → trace the leaf. That model is **wrong**, and getting it wrong is expensive: it implies the compiler re-traces and re-compiles a flash-attention kernel on *every* lowering, when in fact the production model macros are emitted as a bare string name and resolved by a downstream splice.

> **CORRECTION (D-P13) —** an earlier reading (P06 §0) held that every bespoke `codegen<Name>` *and* the generic `codegenBIRKernel` "ALL funnel into `codegenInternalNativeNkiKernel` → `_resolve_kernel_config` → trace." With the UNSTRIPPED `BirCodeGenLoop` body in hand this is refuted. There are **three** distinct BIR kernel nodes built by **three** distinct codegen sinks, and only one of them (`codegenInternalNativeNkiKernel`) touches the registry or re-traces anything. `[CONFIRMED — see §2/§3]`

The three nodes are real, separate C++ classes in the `bir` namespace. The mangled latency-model symbols in `libwalrus` name all three explicitly:

```text
_ZNK3bir3Hwm10getLatencyERKNS_13InstNKIKernelE          // bir::InstNKIKernel
_ZNK3bir3Hwm10getLatencyERKNS_17InstNKIKLIRKernelE      // bir::InstNKIKLIRKernel
_ZNK11TrainiumHwm10getLatencyERKN3bir13InstBIRKernelE   // bir::InstBIRKernel
```

### The InstructionType ordinals (the keystone proof)

The three nodes occupy contiguous slots in the BIR `InstructionType` enum, in the "Custom / Extension" band right after `CustomOp`:

```c
// bir::InstructionType — the 110-member opcode enum (extract, slots 50-58)
CollectiveRecv  = 50,
Select          = 51,
CopyPredicated  = 52,
CustomOp        = 53,
BIRKernel       = 54,    // → bir::InstBIRKernel
NKIKernel       = 55,    // → bir::InstNKIKernel
NKIKLIRKernel   = 56,    // → bir::InstNKIKLIRKernel
DevicePrint     = 57,
GetRandState    = 58,
```

The numbering is not inferred from string order — it is pinned by the node constructor itself. `bir::InstBIRKernel(std::string const&, BasicBlock*)` chains to its base with the ordinal as a literal argument:

```c
// bir::InstBIRKernel::InstBIRKernel(name, bb)  (libwalrus, ~704-byte object)
: Instruction(name, bb, /*InstructionType=*/54)   // ← ordinal is a ctor literal
```

So "IT54 / IT55 / IT56" on this page are the literal enum values `BIRKernel=54`, `NKIKernel=55`, `NKIKLIRKernel=56`. All three are direct `bir::Instruction` subclasses (not `InstCollective`/`InstMatmultBase` subclasses), and all three are tagged category `Cust` in the BIR opcode table. `[CONFIRMED — enum + ctor literal, cross-ref [BIR Kernel-Inst Nodes](../bir/kernel-inst-nodes.md) (Part 7, planned)]`

The enum itself is defined in `libBIR.so` (`neuronxcc/starfish/lib/libBIR.so`, imported as `UND` by every native driver via `DT_NEEDED`). Two mutually-confirming functions pin the ordinals byte-exact:

- **Forward** — `bir::InstructionType::string(InstructionType)` @ VMA `0x2d5bf0` dispatches through a 110-entry jump table @ `0x7853a0` (one self-relative `int32` per opcode, `cmp $0x6d,%esi` bounding the 0..109 index). Index 54 loads the name string `"BIRKernel"` @ `0x709947`, index 55 `"NKIKernel"` @ `0x709951`, index 56 `"NKIKLIRKernel"` @ `0x70995b`.
- **Inverse** — `bir::string2InstructionType(string const&)` @ VMA `0x2da0b0` returns the raw ordinal as an immediate: `mov $0x36,%eax` (54) / `$0x37` (55) / `$0x38` (56) at `0x2dae6c` / `0x2dae62` / `0x2dae58`.

The numbering is **0-based** with no off-by-one: the IT-number *is* the enum integer (IT54 ⇔ `0x36`). `[CONFIRMED — forward + inverse name↔ordinal tables in the defining binary `libBIR.so`]`

> **QUIRK — "Custom/Extension" is the catch-all band, not a coincidence.** Slots 53–57 (`CustomOp`, `BIRKernel`, `NKIKernel`, `NKIKLIRKernel`, `DevicePrint`) are the opcodes the scheduler/simulator treat as opaque, latency-modelled black boxes rather than ISA instructions it can reason about. A macro kernel is, to the backend, an opaque cost — which is exactly why the latency model carries a per-node `getLatency` overload for each of `InstBIRKernel` / `InstNKIKernel` / `InstNKIKLIRKernel`.

### The discriminator: one getattr per sink

The only thing that distinguishes the three sinks at emit time is the `Opcode.<X>` they pass to `addInstruction`. Each codegen body performs a `Opcode` module-global lookup followed by a `.BIRKernel` / `.NKIKernel` / `.NKIKLIRKernel` attribute fetch, then `addInstruction(that_opcode)`. In `codegenBIRKernel` @ `0xa8360` the sequence is visible verbatim in the decompiled body:

```c
// codegenBIRKernel (#221) @ 0xa8360 — the Opcode.BIRKernel getattr chain
Attr   = v62(op1, __pyx_n_s_addInstruction, ...);          // line 1102: bind .addInstruction
kw_argsf = _Pyx__GetModuleGlobalName(__pyx_n_s_Opcode, ...);  // line 1129: Opcode
pyx_int_0 = v63(kw_argsf, __pyx_n_s_BIRKernel);            // line 1146: Opcode.BIRKernel
// → addInstruction(Opcode.BIRKernel)  ⇒ emits an InstBIRKernel (IT54)
```

The same shape appears in every sink, differing only in the final attribute name. This is the single fact that sorts a macro into one of three nodes. `[CONFIRMED — decompiled + disasm]`

---

## 2. Sink A — `InstBIRKernel` (IT54): the name-inlined library node

This is the sink for **every production model-fusion macro**. It is built by four distinct codegen methods, all of which perform `addInstruction(Opcode.BIRKernel)` and none of which re-trace anything or touch the registry.

### Purpose

An `InstBIRKernel` node says: *"here is a kernel called `<kernel_name>`, with these attributes and these operand shapes; the actual BIR for it already exists in a precompiled leaf — splice it in by name, later."* It carries no binary and no version. The macro NAME is the entire payload that selects the compute.

### What the node carries

| Field family | Setters called | Source |
|---|---|---|
| Identity | `set_kernel_name(kernel_name)` | the macro-op NAME = the leaf name |
| Attribute bag | `set_kernel_attrs(inst.kernel_attrs)` (generic) **or** named setters (bespoke) | the kwarg config bag the emitter packed |
| Shapes | `set_srcs_shape`, `set_dsts_shape`, `set_sb_buf_shape`, `set_psum_buf_shape` | operand tensor shapes |
| Cast policy | `set_auto_cast`, `set_auto_cast_type` | mixed-precision policy |
| Operand access | per-operand `addSeqAccess` / `addAP` / `addOpaqueAP` (§5) | the access-pattern marshalling |

No `klir_binary`, no `kernel_format`, no `nki_binary_version_identifier` — those are IT56-only fields (§4). `[CONFIRMED — setter strings present in every IT54 body; cross-ref [BIR Kernel-Inst Nodes](../bir/kernel-inst-nodes.md) (Part 7, planned)]`

### The four IT54 sinks

```text
codegenBIRKernel (#221, generic)            ──┐
_commonNativeKernelCodegen (#225)            ─┤
   ← codegenMLPKernel / codegenNormQKV /     │   all four perform
     codegenRMSNormQuantKernel + tiled twins │   addInstruction(Opcode.BIRKernel)
_commonAttentionKernelCodegen (#217)         ─┤   ⇒ bir::InstBIRKernel (IT54)
   ← codegenAttentionMMSoftmaxMM + tiled twin │
codegenBackwardsAttention (#223, inline)     ──┘
```

Each was confirmed to reference `Opcode` + `BIRKernel` + `addInstruction` in its decompiled body, with the family-specific setters:

| Sink (mdef) | VA | Emits IT54 via | Family-specific setters in body |
|---|---|---|---|
| `_commonNativeKernelCodegen` (#225) | `0x171a70` | `Opcode.BIRKernel` | `set_kernel_name`, `set_auto_cast`/`_type` — the family-agnostic node builder |
| `_commonAttentionKernelCodegen` (#217) | `0x1140a0` | `Opcode.BIRKernel` | + `cache_softmax`, `use_flash`, `use_dma_transpose` |
| `codegenBackwardsAttention` (#223) | `0x1e02c0` | `Opcode.BIRKernel` (inline) | + `set_is_causal` |
| `codegenBIRKernel` (#221) | `0xa8360` | `Opcode.BIRKernel` | generic: `set_kernel_attrs` + metrics |

`[CONFIRMED — each body greps to exactly one of {Opcode, BIRKernel, addInstruction} plus its named setters]`

> **NOTE — two shared emitters, one node.** `_commonNativeKernelCodegen` (#225) and `_commonAttentionKernelCodegen` (#217) are *shared* IT54 builders. The bespoke per-family methods (`codegenMLPKernel`, `codegenNormQKV`, `codegenRMSNormQuantKernel`, `codegenAttentionMMSoftmaxMM`, and the four `codegenTiledNativeKernel*` twins) pin the family attrs and marshal operands, then hand off to one of these two for the actual `addInstruction(Opcode.BIRKernel)`. `codegenBackwardsAttention` is the one exception — it inlines its own `addInstruction` because it carries `is_causal` as a first-class `set_is_causal` field that the common builder doesn't name. `[CONFIRMED]`

### Why the model macros are IT54 and not registry-traced

The decisive fact: the compiled BIR for attention/MLP/QKV/RMSNorm **already exists** in `neuronxcc/nki/_private_kernels/{attention,mlp,qkv,rmsnorm}.cpython-310…so`. Lowering does not need to regenerate it — it only needs to *reference* it by name and let the downstream inliner splice it. So the lowerer emits a cheap `InstBIRKernel` (IT54) carrying just the name, and the heavy lifting is deferred.

Downstream, in `libwalrus`, `InlineBIRKernel::run` @ `0xd86510` scans IT54 nodes (reading the name at `node+0xF0`) and expands each via `BIRKernelWrapper::createInstance(Logger, InstBIRKernel*, BasicBlock*, bool)` @ `0xd855d0` — directly, with **no JSON round-trip and no KLR walk**. The macro's compute is pattern-rewritten into the function in place. `[CONFIRMED — D-P13 / D-I28 §5 / D-H16]`

> **GOTCHA — `codegenBIRKernel` does NOT touch the registry.** A reimplementer who wires the generic library-kernel path through `_INTERNAL_KERNEL_REGISTRY` will be wrong. The `codegenBIRKernel` body (#221 @ `0xa8360`) contains **zero** references to `get_internal_kernel_registry`, `_resolve_kernel_config`, or `InternalKernelConfig` — verified in both the decompiled C and the raw disassembly, and corroborated by the xref table (those strings are referenced only by their own `pw` functions, never by `0xa8360`). The registry is for IT56 (§4) only.
>
> **CORRECTION (D-P13) —** an earlier reading (P06 §2) had "RouterTopK / ExpertMLPs / RowTiledMM / ColumnTiledMM / Cayman route through generic `codegenBIRKernel` **+ `_INTERNAL_KERNEL_REGISTRY`**." The "+ registry" half is refuted: `codegenBIRKernel` emits an IT54 library node keyed by name; those macros are name-inlined downstream like every other IT54. `[CONFIRMED by registry-call absence]`

### `codegenBIRKernel` — the generic expander

`codegenBIRKernel` (#221) is the deliverable-#1 generic library-kernel emitter. Every macro that has no bespoke `codegen<Name>` twin — `RouterTopK`, `ExpertMLPs`, `RowTiledMM`, `ColumnTiledMM`, `CaymanPackedPETranspose`, `AttentionTkgFwd` — lowers through it.

```c
// codegenBIRKernel (#221) @ 0xa8360 — the generic IT54 expander
function codegenBIRKernel(self, inst):
    kernel_name = inst.kernel_name                       // the macro-op NAME = the leaf
    // 1. telemetry (NEW: a per-kernel usage sidecar)
    KernelMetricsCollector.get_instance().record_kernel(  // __pyx_n_s_record_kernel
        kernel_name, category=KernelCategory.BIR_KERNEL,  // __pyx_n_s_KernelCategory
        ...)
    // 2. emit the node
    node = self.addInstruction(Opcode.BIRKernel)         // ⇒ InstBIRKernel (IT54)
    node.set_kernel_name(kernel_name)
    node.set_kernel_attrs(inst.kernel_attrs)             // op-config bag, passed THROUGH verbatim
    node.set_srcs_shape(src_shapes); node.set_dsts_shape(dst_shapes)
    node.set_sb_buf_shape(sb); node.set_psum_buf_shape(psum)
    node.set_auto_cast(ac);    node.set_auto_cast_type(act)
    // 3. per-operand access (the only per-op work) — see §5
    for src in inst.srcs: codegenBIRKernelAccess(node, src, isOutput=False)
    for dst in inst.dsts: codegenBIRKernelAccess(node, dst, isOutput=True)
    return node     // NO trace, NO registry, NO inner GEMM/softmax math
```

The generic path is therefore: **name the leaf + pass `kernel_attrs` through verbatim + bind operand access patterns.** The inner math is spliced by name downstream. `[CONFIRMED — KernelMetricsCollector ×2, record_kernel, KernelCategory ×2, and all six setters confirmed at disasm level]`

> **NOTE — the telemetry sidecar.** `codegenBIRKernel` records every library-kernel emission into `KernelMetricsCollector` (`record_kernel`, keyed by `KernelCategory.BIR_KERNEL` and the kernel name). This is the metrics half of the `NEW_NKI_FE` instrumentation; the IT56 path has its own cache-hit/miss counters (next page). The IT54 path has no *trace* cache because there is no trace to memoize — the leaf BIR is inlined by name. `[CONFIRMED]`

---

## 3. The bespoke families — same node, named attrs

The five bespoke families (`codegenMLPKernel`, `codegenNormQKV`, `codegenRMSNormQuantKernel`, `codegenAttentionMMSoftmaxMM`, `codegenBackwardsAttention`) and their four `codegenTiledNativeKernel*` twins all emit the same `InstBIRKernel` (IT54) node as the generic path. They are *bespoke* — they have their own `codegen<Name>` method instead of routing through the generic expander — for exactly two reasons:

1. They read a **fixed, named** attribute set off the macro inst rather than passing a generic `kernel_attrs` bag through verbatim.
2. Some compute a **routing flag** (token-generation vs prefill) from a sequence-length threshold that the lowerer must read from the leaf module.

This is the *same* bespoke-vs-generic boundary documented on the emit side in [NeuronCodegen Macro-Kernel Emitters §1](neuroncodegen-macro.md) — viewed from the node side, "bespoke" means "has a named-attr `codegen<Name>` method," and "generic" means "routes through `codegenBIRKernel` with a `kernel_attrs` bag." Both sides emit IT54.

### The named-attr rosters

| Bespoke sink (mdef) | VA | Named attrs read (binary-confirmed) | Notes |
|---|---|---|---|
| `codegenMLPKernel` (#245) | `0xf6a70` | `fused_rmsnorm`, `norm_type`, `store_add`, `quant_kernel`, `lower_bound`, `skip_gate`, `act_fn`, `up_bias`, `down_bias` | imports `TKG_BS_SEQLEN_THRESHOLD`, sets `is_tkg` |
| `codegenNormQKV` (#227) | `0xfb050` | `fused_rmsnorm`, `norm_type`, `output_layout`, `lnc_size`, `useTkgQKVKernel` | RMSNorm fused into QKV proj |
| `codegenRMSNormQuantKernel` (#247) | `0x9a9c0` | `lower_bound` (minimal) | quant config rides in `kernel_attrs` |
| `codegenAttentionMMSoftmaxMM` (#219) | `0xb8c10` | (via `_commonAttentionKernelCodegen`) `cache_softmax`, `use_flash`, `use_dma_transpose` | name-dispatches untiled vs tiled |
| `codegenBackwardsAttention` (#223) | `0x1e02c0` | `is_causal` (`set_is_causal`, inline) | scale=1.0, dropout=0 contracts |

`[CONFIRMED — all attr strings present; `skip_gate`/`act_fn`/`useTkgQKVKernel` provable only from disasm (see GOTCHA below)]`

### `codegenMLPKernel` — the token-gen routing decision

```c
// codegenMLPKernel (#245) @ 0xf6a70 — dense / fused-add / quant MLP
function codegenMLPKernel(self, inst):
    // 1. read the NAMED MLP attrs (not a generic bag)
    fused_rmsnorm = inst.fused_rmsnorm;  norm_type = inst.norm_type
    store_add  = inst.store_add          // fused-add residual flag
    quant_kernel = inst.quant_kernel     // quant fold
    lower_bound = inst.lower_bound;  skip_gate = inst.skip_gate
    act_fn = inst.act_fn;  up_bias = inst.up_bias;  down_bias = inst.down_bias
    // 2. the seqlen routing decision — the threshold const lives in the LEAF .so
    from neuronxcc.nki._private_kernels.mlp import TKG_BS_SEQLEN_THRESHOLD
    is_tkg = (src_shapes[...] < TKG_BS_SEQLEN_THRESHOLD)  // token-gen vs CTE/prefill
    // 3. pin attrs + marshal operands, then hand to the shared IT54 builder
    node.set_kernel_attrs({fused_rmsnorm, store_add, quant_kernel, is_tkg, ...})
    for op in operands: node.addSeqAccess(op, isOutput)   // untiled (whole-tensor)
    return _commonNativeKernelCodegen(self, inst, ...)    // ⇒ addInstruction(Opcode.BIRKernel)
```

Why bespoke: MLP must (i) read the gate/up/down + norm + quant + store-add config explicitly (the generic bag can't name them), and (ii) make the `is_tkg` token-generation-vs-prefill decision *in the lowerer* — which requires pulling `TKG_BS_SEQLEN_THRESHOLD` from the `mlp` leaf module. `codegenNormQKV` makes the analogous decision via `useTkgQKVKernel` and the same threshold. `[CONFIRMED]`

> **GOTCHA — the `mlp` module import is NOT a registry registration.** `neuronxcc.nki._private_kernels.mlp` appears as a module-path string in `BirCodeGenLoop`, but it is imported *only* by `codegenMLPKernel`/`codegenNormQKV` to read the `TKG_BS_SEQLEN_THRESHOLD` constant. There is **no** `PyDict_SetItem(registry, 'MLP', …)` and **no** `fused_mlp_isa_kernel` string anywhere in the binary (grep returns 0). MLP is an IT54 library node, not an IT56 registry carrier.
>
> **CORRECTION (D-P13) —** an earlier reading (O30 §3) said "the registry maps `mlp → fused_mlp_isa_kernel`." Refuted: the registry holds no `MLP` key and the binary holds no `fused_mlp_isa_kernel` string. The O30 entry conflated the module-string *presence* with a registry *registration*. `[CONFIRMED by PyDict key + string absence]`

### The tiled-native twins — same attrs, `addAP` instead of `addSeqAccess`

The four `codegenTiledNativeKernel*` twins read the **same** family attrs as their untiled sibling but marshal operands with `addAP(...)` (a pre-tiled `MemrefTile` access pattern) instead of `addSeqAccess(...)` (whole-tensor sequential), then call the same shared common helper. The tiled/untiled split at the lower level is *purely* the operand-access flavor:

| Twin (mdef) | VA | Same attrs as | Access flavor |
|---|---|---|---|
| `codegenTiledNativeKernelMLP` (#251) | `0x19b0d0` | `codegenMLPKernel` | `addAP` |
| `codegenTiledNativeKernelQKV` (#229) | `0xf8fe0` | `codegenNormQKV` | `addAP` |
| `codegenTiledNativeKernelRMSNormQuant` (#249) | `0x94240` | `codegenRMSNormQuantKernel` (+`set_lnc_size`) | `addAP` |
| `codegenTiledNativeKernelAttention` (#253) | `0x5aaf0` | (via `_commonAttentionKernelCodegen`) | `addAP` |

The upstream IO-type test (`_is_all_io_type_memref_tile`, in `KernelBuilder.so` — [NeuronCodegen Macro-Kernel Emitters §3](neuroncodegen-macro.md)) picks *which* codegen runs; the codegen just honors the operand access flavor it was handed. `[CONFIRMED — twin attr rosters identical modulo addAP↔addSeqAccess]`

---

## 4. Sink C — `InstNKIKLIRKernel` (IT56): the registry-traced carrier

`codegenInternalNativeNkiKernel` (#243) @ `0x8d630` is the **only** codegen that re-traces a compiled kernel and embeds the result. Its docstring is verbatim: *"Codegen for internal NKI kernels using new NKI frontend path."* (the rest: *"Traces the kernel to new NKI frontend at codegen time and creates BIR NKIKLIRKernel instruction. Uses caching to avoid redundant tracing of identical kernels."*).

### Purpose

For a small set of kernels — convolution, resize, blockwise-MM, transpose, select-and-scatter — there is **no** precompiled-leaf BIR to inline by name. Their BIR must be **regenerated** from NKI source through the "new NKI frontend." So instead of an IT54 name-reference, the lowerer traces the kernel *now*, serializes the result, and embeds it in an `InstNKIKLIRKernel` (IT56) carrier.

### What the node carries (the IT56-only fields)

```c
// codegenInternalNativeNkiKernel (#243) @ 0x8d630 — registry trace driver
function codegenInternalNativeNkiKernel(self, inst):
    cfg = get_internal_kernel_registry().get(inst.func_name)   // ← REGISTRY lookup
    nki_frontend = os.environ.get('NKI_FRONTEND', 'beta2')     // default = beta2 (KLIR)
    binary, version = trace_or_cache(inst, cfg, nki_frontend)  // see next page (cache + beta2/beta3)
    node = self.addInstruction(Opcode.NKIKLIRKernel)           // ⇒ InstNKIKLIRKernel (IT56)
    node.set_klir_binary(binary)                  // the traced binary (carrier @+0xF0)
    node.set_kernel_format(fmt)                   // "bir" or KLIR form  (@+0x110)
    node.set_nki_binary_version_identifier(version)              // (@+0x130)
    node.set_func_args(...);  node.set_func_outs(...)
    node.set_sb_buf_shape(...); node.set_psum_buf_shape(...)
    for op in operands: node.addOpaqueAP(op, isOutput)  // kernel owns its internal tiling
    return node
```

| IT56-only field | Setter | Carrier offset (D-I28) |
|---|---|---|
| Traced binary (file path or embedded blob) | `set_klir_binary` | `klir_binary` @ `+0xF0` |
| Carrier discriminator (`"bir"` ⇒ FORM B) | `set_kernel_format` | `kernel_format` @ `+0x110` |
| Kernel version | `set_nki_binary_version_identifier` | `nki_binary_version_identifier` @ `+0x130` (optional) |

These three fields are what make IT56 distinct from IT54 — an `InstBIRKernel` has *none* of them. `[CONFIRMED — all 8 setters present in #243 body; offsets from D-I28]`

### Downstream resolution — two carrier forms

In `libwalrus`, `TranslateNKIASTToBIR::lowerKernelInst` @ `0xf0b610` reads `kernel_format@+0x110` and branches:

- **FORM A** (`kernel_format != "bir"`) — `klir_binary@+0xF0` is a path to a serialized `klr::` KLR-AST binary; `fopen` + `KLRFile_des`/`KLRMetaData_des`/`Contents_des` deserialize it (this is the **beta2 / KLIR** default path).
- **FORM B** (`kernel_format == "bir"`) — `klir_binary@+0xF0` is an embedded/on-disk BIR-JSON blob; `lowerFromBirJson` @ `0xf0a160` nlohmann-parses it and picks `j["functions"][NeuronCoreId]` (this is the **beta3 / BIR** path).

`[CONFIRMED — D-I28 §3/§4; cross-ref [BIR Kernel-Inst Nodes](../bir/kernel-inst-nodes.md) (Part 7, planned)]`

### The registry's contents (proof of the IT54/IT56 boundary)

`_INTERNAL_KERNEL_REGISTRY` registers **exactly** these keys — and *not* MLP/QKV/Attention/RMSNorm:

```text
ResizeNearest                            ← neuronxcc.private_nkl.resize
SelectAndScatter                         ← neuronxcc.private_nkl.select_and_scatter
Conv1d_depthwise_bf01_oi01_bf01          ← neuronxcc.private_nkl.conv
conv2d_depthwise_f01b_o01i_bf01          ← neuronxcc.private_nkl.conv
Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh  ← neuronxcc.private_nkl.conv
conv2d_column_packing[_io10][_1]         ← neuronxcc.private_nkl.conv  (3 variants)
Conv2d_pbp_0f1b_0i1o_01fb_experimental_1 ← neuronxcc.nki._private_kernels.conv
Conv2d_pbp_fb01_io01_01bf_experimental_1 ← neuronxcc.nki._private_kernels.conv
blockwise_mm                             ← neuronxcc.nki._private_kernels.blockwise_mm
tiled_pf_transpose                       ← neuronxcc.private_nkl.transpose
tiled_dve_transpose_10                   ← neuronxcc.private_nkl.transpose
```

This is the *same* boundary as §2/§3, ground-truthed: the registry holds the kernels that need **re-tracing** (no precompiled-leaf BIR), and the IT54 path holds the kernels whose BIR **already exists** (model-fusion macros). `[CONFIRMED — all 13 keys present; `MLP`/`fused_mlp_isa_kernel` absent]`

> **NOTE — two registry namespaces.** The production resize/select-and-scatter/conv2d/transpose leaves come from `neuronxcc.private_nkl.{resize,select_and_scatter,conv,transpose}` — a *sibling* of `nki._private_kernels`. Only the two `pbp_*experimental` conv2d variants and `blockwise_mm` come from `neuronxcc.nki._private_kernels.{conv,blockwise_mm}`. Both module-path namespaces are confirmed as strings in the binary. The build/resolve mechanics are the next page's subject. `[CONFIRMED]`

---

## 5. Sink B — `InstNKIKernel` (IT55): the user external node

`codegenExternalNativeNkiKernel` (#231) @ `0x1a4e60` is the third sink. It handles **user** `@nki.jit` kernels — code the user wrote, traced through the front end, and AOT-compiled. The body performs `addInstruction(Opcode.NKIKernel)` and binds `set_func` / `func_args` / `func_outs`:

```c
// codegenExternalNativeNkiKernel (#231) @ 0x1a4e60 — user @nki.jit
function codegenExternalNativeNkiKernel(self, inst):
    node = self.addInstruction(Opcode.NKIKernel)    // ⇒ InstNKIKernel (IT55)
    node.set_func(user_func)                         // the user kernel callable/handle
    node.set_func_args(...); node.set_func_outs(...)
    ...
    return node
```

IT55 is the *legacy* trace-and-AOT-compile NKI form; IT56 (`InstNKIKLIRKernel`) is the new-frontend re-trace variant. The companion `codegenExternalNativeNkiKlirKernel` (#233) @ `0x137c40` is the external KLIR-path twin. These are out of scope for the model-fusion macros (which are all IT54), but they complete the three-sink picture: IT54 = library, IT55 = user external, IT56 = registry-traced internal. `[CONFIRMED — Opcode.NKIKernel + set_func* in #231 body]`

---

## 6. Operand-access marshalling — the per-macro work

The *only* per-macro work the IT54 lowerers do (beyond pinning attrs) is binding each operand's access pattern onto the node. This is the shared `codegenBIRKernelAccess` dispatcher (nested in `codegenBIRKernel`, and the structurally-identical version nested in `codegenAttentionMMSoftmaxMM`).

### The access-type dispatch

`codegenBIRKernelAccess` reads each operand's access *type* and routes to one of four builders, then restores any temporary AP reshape:

```c
// codegenBIRKernelAccess (nested @ 0xc4100) — per-operand access dispatcher
function codegenBIRKernelAccess(node, operand, isOutput):
    switch operand.access_type:
      case FullTensorAccess:                          // whole-tensor, contiguous
          node.addSeqAccess(isOutput)
      case NDimSubTensorAccess:                       // strided N-D sub-tensor
          addBIRKernelNDimSubTensorAccess(node, operand, isOutput)   // #275 @ 0xc5c60
      case TileAccess:                                // MemrefTile / NDTile view
          addBIRKernelTileAccess(node, operand, isOutput)            // #277 @ 0x1170e0
      case OpaqueAccess:                              // kernel owns its tiling
          node.addOpaqueAP(isOutput)
    restore_original_ap(operand)                      // un-do any temporary AP reshape
```

This is the same access-type taxonomy the untiled (`addSeqAccess`) vs tiled-native (`addAP`) emitters specialize on. `[CONFIRMED — FullTensorAccess/NDimSubTensorAccess/TileAccess/OpaqueAccess + addSeqAccess/addBIRKernelNDimSubTensorAccess/addBIRKernelTileAccess/addOpaqueAP/restore_original_ap all present in the nested body]`

### The tile / sub-tensor AP builders

`addBIRKernelTileAccess` (#277) and `addBIRKernelNDimSubTensorAccess` (#275) are twin bodies — identical name roster modulo `access_elts_per_dim` vs `access_elts_per_dim_2`. Each builds the address list and creates the access pattern:

```c
// addBIRKernelTileAccess (#277) @ 0x1170e0  (≈ addBIRKernelNDimSubTensorAccess #275)
function addBIRKernelTileAccess(node, operand, isOutput):
    access, access_shape, src_shape = operand.access, operand.access_shape, operand.src_shape
    partition_dim = operand.partition_dim;  lnc = LncSize(operand)
    addrs = build_addrs(access_elts_per_dim, ...)   // mul over dims, on a NeuronSBTensor / memref
    ap = node.createAP(addrs, access_shape, partition_dim)
    node.addArgumentOrOutput(ap, isOutput)          // reduce flag = may reduce along partition dim
```

The tile-vs-sub-tensor split is *which* dim-iterator builds `addrs`; the `createAP → addArgumentOrOutput` tail is identical. `[CONFIRMED — createAP, addArgumentOrOutput, access_shape, partition_dim, reduce, access_elts_per_dim all present in #277]`

---

## 7. Reimplementation checklist

A reimplementer of the BIR-node side of macro lowering needs:

1. **Three node classes, three ordinals.** `InstBIRKernel = 54` (library, name-keyed), `InstNKIKernel = 55` (user external), `InstNKIKLIRKernel = 56` (registry-traced carrier). All three derive directly from `Instruction`; the ordinal is a constructor literal.
2. **One discriminator per sink.** The codegen body's `addInstruction(Opcode.<X>)` choice (`BIRKernel` / `NKIKernel` / `NKIKLIRKernel`) is the entire sink-selection. Get this getattr right and the rest follows.
3. **The IT54 payload.** `kernel_name` (the macro NAME), `kernel_attrs` (or named setters), `srcs_shape`/`dsts_shape`/`sb_buf_shape`/`psum_buf_shape`, `auto_cast`/`auto_cast_type`, and per-operand access. **No** binary, **no** version, **no** trace — the compute is spliced by name downstream (`InlineBIRKernel` / `BIRKernelWrapper`).
4. **The registry boundary.** Route a kernel to IT56 (registry trace) **iff** it is one of {ResizeNearest, SelectAndScatter, the 8 Conv*, blockwise_mm, tiled_pf/dve_transpose}. Everything else — MLP, Attention, QKV, RMSNorm, RouterTopK, ExpertMLPs, RowTiledMM, ColumnTiledMM, Cayman — is IT54. Do **not** route the model-fusion macros through the registry.
5. **The bespoke-vs-generic split (both emit IT54).** A family gets a bespoke `codegen<Name>` when it reads a *named* attr set (and possibly a token-gen routing flag via `TKG_BS_SEQLEN_THRESHOLD`); everything else passes a generic `kernel_attrs` bag through `codegenBIRKernel`. The tiled twins differ only in `addAP` (pre-tiled) vs `addSeqAccess` (whole-tensor).
6. **The IT56 carrier fields and two forms.** `klir_binary@+0xF0`, `kernel_format@+0x110` (`"bir"` ⇒ FORM B embedded BIR-JSON, else FORM A KLR-AST file), `nki_binary_version_identifier@+0x130`. Operands use `addOpaqueAP` (the kernel owns its internal tiling).

> **What is not pinned here.** The Cython macro-op → `codegen<Name>` *dispatch* (which Inst type routes to which codegen) is a dynamic PyObject dispatch (vtable/dict), not a static call edge — the codegen identity is grounded by the `__pyx_n_s` name rosters + the mdef table, not a static jump (`STRONG`, standard for Cython). The registry build/resolve mechanics, the trace cache, and the beta2/beta3 trace leaves are the next page's subject. The downstream `InlineBIRKernel` / `lowerKernelInst` resolvers live in `libwalrus` and are covered by the Part-7 BIR pages.

> **NOTE — verification caveats (carried from binary re-grep).** Two precision notes from re-disassembly: (1) `beta3` is *not* a standalone interned string in `BirCodeGenLoop` — only `beta2` is (at `0x1f8668`), consistent with `beta2` being the hardcoded `NKI_FRONTEND` default; `beta3` appears only inside the selector docstring. (2) The MLP attrs `skip_gate`/`act_fn` and the QKV `useTkgQKVKernel` are provable only from the `.asm` (the IDA decompiler's "local variable allocation has failed" on the large kernel functions drops some Cython interned-string loads from the C text); each was confirmed as an `__pyx_n_[su]_NAME` load inside the claimed function's disassembly.

---

## Related Components

| Name | Relationship |
|---|---|
| `bir::InstBIRKernel` (IT54) | the library kernel node; built by `codegenBIRKernel` / `_commonNativeKernelCodegen` / `_commonAttentionKernelCodegen` / `codegenBackwardsAttention` |
| `bir::InstNKIKernel` (IT55) | the user `@nki.jit` external node; built by `codegenExternalNativeNkiKernel` |
| `bir::InstNKIKLIRKernel` (IT56) | the registry-traced carrier; built by `codegenInternalNativeNkiKernel` |
| `InlineBIRKernel::run` (libwalrus) | expands IT54 nodes by name — `BIRKernelWrapper::createInstance`, no JSON round-trip |
| `TranslateNKIASTToBIR::lowerKernelInst` (libwalrus) | resolves IT56 carriers — FORM A KLR-AST / FORM B BIR-JSON |

## Cross-References

- [NeuronCodegen Macro-Kernel Emitters](neuroncodegen-macro.md) — the EMIT side (6.5.7): the ~25 `GeneratedNeuronCodegen` macro emitters, the macro NAME pool, and the bespoke-vs-generic split this page views from the node side
- [The Internal Kernel Registry & New-NKI-Frontend Re-Trace](internal-kernel-registry.md) — the next page (6.6.2): `_build_internal_kernel_registry`, `InternalKernelConfig`, `_resolve_kernel_config`, the trace cache, and `_trace_kernel_beta2`/`beta3`
- [BirCodeGenLoop: the beta3 Penguin→BIR Driver](bircodegenloop.md) — the codegen driver that hosts all `codegen<Name>` methods
- [BIR Kernel-Inst Nodes](../bir/kernel-inst-nodes.md) — the Part-7 node serializers (I28): the `InstBIRKernel`/`InstNKIKernel`/`InstNKIKLIRKernel` field layouts and the `InstructionType` enum
- [NKI Architecture Overview & the 3-Layer Lowering Stack](architecture-overview.md) — where BIR-node emission sits in the Python-trace → penguin.ir → BIR pipeline
