# HLO → Native / NKI Kernel Lowering

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310). Two binaries are cited: `hlo-opt` (the three pattern passes and their workers) and `hlo2penguin` (the native-kernel cast-type policy and `SetNativeKernelCastType`). Where an address is binary-specific the binary is named inline; cp311/cp312 share the same offsets.*

## Abstract

Three mid-pipeline passes form the bridge between high-level HLO op idioms and AWS Neuron's native / NKI kernels. **`LowerToCustomNativeKernel`** (`hlo-opt` `::Run`@`0x1f3eeb0`) recognises the **matmul → softmax → matmul** attention idiom and *raises* it to a `CustomCall` whose target is an attention-variant string (`AttentionMMSoftmaxMM`, `…WithoutSwap`, `CausalAttentionMMSoftmaxMMWithoutSwap`) and whose `backend_config` is a serialized `{psum_shape, sb_shape, auto_cast, auto_cast_type, kernel_name}` blob. **`LowerToNKIKernelCC`** (`::Run`@`0x1f48300`) recognises an **MLP dot-chain** (`dot2(act(dot1(x)))`) and raises it to an `AwsNeuronMLPNKI` `CustomCall` with an *empty* config. **`DecomposeAttention`** (`::Run`@`0x1e9d160`) is the **inverse** of the attention raise: given a fused / native attention root it expands the canonical `dot(Q,Kᵀ) → softmax → dot(P,V)` subgraph back into explicit math ops, where softmax is the numerically stable `exp(x − rowmax) / Σ exp(x − rowmax)`.

Governing the attention raise is a separate, NKI-only **cast-type policy** living in `hlo2penguin`: `SetNativeKernelCastType`@`0x1f93060` writes a `"<what>-to-<type>"` string (default `"matmult-to-bf16"`) that splits into the globals `xla::whatToCast` (∈ {`matmult`, `all`}) and `xla::castType` (∈ {`bf16`, `fp16`}). The flag `enable-native-kernel-attention-bf16` decides whether the MM-Softmax-MM matcher fires on *any* input dtype (flag on) or only when the tensors are *already* BF16 (flag off). The pivotal nuance — easy to misread from the name `matmult-to-bf16` — is that this knob sets only the **storage / I/O dtype** of the fused kernel's operands and intermediates; the PE-array dot product still **accumulates in fp32 PSUM** (hardware-fixed). That distinction is made a QUIRK below and cross-referenced to [Part 9 — fp32 accumulation](#).

For reimplementation, the contract is:

- The three pass bodies and their match drivers, named to real symbols.
- The attention `CustomCall` construction: target selection, the 5-key `backend_config`, `api_version=1`.
- The MLP `CustomCall` construction: the byte-built `AwsNeuronMLPNKI` target, empty config.
- The `DecomposeAttention` softmax expansion with its HloOpcode immediates.
- The cast-type grammar, its two backing `cl::opt`s, and the storage-vs-accumulate split.

| | |
|---|---|
| **#37 attention raise** | `xla::LowerToCustomNativeKernel::Run` @`0x1f3eeb0` (`hlo-opt`), 2007 B |
| **#37 worker** | `xla::lowerMMSoftmaxMMToAttentionKernel` @`0x1f3e830` → `…WithConfig` @`0x1f35a80` |
| **#38 attention decompose** | `xla::DecomposeAttention::Run` @`0x1e9d160` (`hlo-opt`), 6143 B |
| **#56 MLP raise** | `xla::LowerToNKIKernelCC::Run` @`0x1f48300` (`hlo-opt`), 157 B → `lowerMLPToNKIKernel` @`0x1f47030` |
| **Cast setter** | `xla::hilo::SetNativeKernelCastType` @`0x1f93060` (`hlo2penguin`) |
| **Cast globals** | `xla::whatToCast` @`0x9c6d540`, `xla::castType` @`0x9c6d520` (`hlo2penguin`) |
| **Config builder** | `xla::serializeConfig` @`0x1f2a280` (`hlo-opt`) / `0x1fe9b20` (`hlo2penguin`) |
| **IR level** | HLO (post-StableHLO ingestion), pre-`hlo2penguin` lowering |
| **Opcode anchor** | `xla::HloOpcodeString` @`0x96bb550` (switch, case value == enum value) |

> **NOTE —** the cast-type policy symbols (`SetNativeKernelCastType`, `whatToCast`, `castType`, `enableNativeKernelAttention*`) live **only in `hlo2penguin`** — the `hlo-opt` ELF contains none of them. The pass *bodies* (`LowerToCustomNativeKernel::Run` etc.) exist in **both** binaries (different addresses); the cast directive reaches the `hlo-opt` copy as already-populated `whatToCast`/`castType` strings, set at compile-config build time before `Run` reads them.

---

## 1. Pipeline Placement & the Inverse Pair

All three passes are registered in `RegisterHiloHloPasses` and run post-order over the **entry computation only**. The two attention passes are **inverses registered one slot apart**: `#37 LowerToCustomNativeKernel` *raises* math → native-attention `CustomCall`; `#38 DecomposeAttention` *lowers* native / fused attention → math. Because `#38` sits immediately after `#37`, it can re-expand a native attention back to primitives when the kernel is not ultimately selected (canonicalization fallback). `#56 LowerToNKIKernelCC` is independent and structural — no flag, no dtype gate.

The custom-call target catalogue (all CONFIRMED as literal strings in the `hlo-opt` ELF except `AwsNeuronMLPNKI`, which is assembled byte-by-byte and so does not appear as a contiguous string):

| Target string | Emitter | Role | Evidence |
|---|---|---|---|
| `AttentionMMSoftmaxMM` | #37 | non-causal SD attention | string in ELF — CONFIRMED |
| `AttentionMMSoftmaxMMWithoutSwap` | #37 | operands not swapped | string in ELF — CONFIRMED |
| `CausalAttentionMMSoftmaxMMWithoutSwap` | #37 | causal / LLM attention | string in ELF — CONFIRMED |
| `AwsNeuronMLPNKI` | #56 | MLP dot-chain kernel | byte-decode @`0x1f47f88` — CONFIRMED (built inline, 15 B) |
| `AwsNeuronCustomNativeKernel` | (class string) | native-kernel CC class id | string in ELF — CONFIRMED |
| `AwsNeuronCustomNativeKernel_Sim` | (golden compare) | sim variant | string in ELF — CONFIRMED |

> **CORRECTION —** the `#37` `CustomCall` target is **variant-selected**, *not* a fixed `AwsNeuronCustomNativeKernel` literal. `AwsNeuronCustomNativeKernel` is the *class* string; the emitted target is `xla::target` resolved to one of the three `…MMSoftmaxMM…` variants. (CreateCustomCall target arg taken from `cs:_ZN3xlaL6targetE` @`0x1f3d17f`.) The `kernel_name` `backend_config` key carries the same variant string. Cross-ref [§3.13 — custom-call targets](#).

---

## 2. #37 `LowerToCustomNativeKernel` — the attention raise

### Purpose

Recognise the QKᵀ → softmax → PV attention block and replace it with a single `CustomCall` backed by a native attention kernel, with the operands' / intermediates' storage dtype governed by the cast-type policy (§4).

### Entry Point

```text
RegisterHiloHloPasses
  └─ LowerToCustomNativeKernel::Run @0x1f3eeb0   (per-inst post-order)
       └─ lowerMMSoftmaxMMToAttentionKernel @0x1f3e830   (shape+dtype gate)
            └─ lowerMMSoftmaxMMToAttentionKernelWithConfig @0x1f35a80  (full match + rewrite, 36 KB)
                 ├─ computeMMSoftmaxMMToAttentionKernelSbShape @0x1f0cc00   (SBUF tile shape)
                 ├─ serializeConfig @0x1f2a280                              (backend_config blob)
                 └─ HloInstruction::CreateCustomCall @0x964eac0
```

### Algorithm — `Run` dispatch loop (`0x1f3eeb0`)

```c
// xla::LowerToCustomNativeKernel::Run(HloModule*, const flat_hash_set<string_view>&)
StatusOr<bool> Run(module, threads) {
    bool enabled = *(byte*)&qword_9A39A58;          // 0x1f3eee1 — "Enable hlo pattern match
                                                    //   for native kernel attention" gate (default FALSE)
    if (!enabled) {                                 // 0x1f3eef0
        LOG(WARNING) << "LowerToCustomNativeKernel pass is enabled but none of "
                        "the kernels is enabled.";  // str confirmed in ELF; the no-op path
        return false;                               // Changed=false
    }
    dynamicDmaBytesPerPart = module.config[+0x48];  // 0x1f3f075 → xla::dynamicDmaBytesPerPart
                                                    //   (cl::opt "dynamic-dma-sbuf-bytes")
    HloComputation* entry = module.entry_computation();   // CHECK nullptr != entry_computation_
    bool changed = false;
    for (HloInstruction* inst : entry->MakeInstructionPostOrder()) {   // 0x1f3f0af
        PrimitiveType et = inst->shape().element_type();              // 0x1f3f192

        // --- cast-config consume: whatToCast / castType already populated (see §4) ---
        bool cast_ok = (whatToCast == "matmult" || whatToCast == "all")
                    && (castType   == "bf16"    || castType   == "fp16");   // 0x1f3f460

        bool castEligible = (et == BF16   /*0x10*/)                          // 0x1f3f1a1
                         || (et == F16    /*0x0A*/)                          // 0x1f3f1aa
                         || (et == F32    /*0x0B*/ && cast_ok);              // F32 only with an opt-in cast

        // internal target-instance discriminator (HIGH): only fire for "sunda"
        if (enabled
            && module.opts.string[+8].compare("sunda") == 0                 // 0x1f3f1c1, "sunda" in ELF
            && castEligible) {                                              // 0x1f3f1da
            changed |= lowerMMSoftmaxMMToAttentionKernel(entry, inst);      // 0x1f3f51e
        }
    }
    return changed;
}
```

Key facts:

- The **only** kernel wired into `#37` is the matmul-softmax-matmul attention kernel — a single call edge to `lowerMMSoftmaxMMToAttentionKernel`. The *"none of the kernels is enabled"* warning is the no-op branch taken when the global gate `qword_9A39A58` is 0. **CONFIRMED** (warning string in ELF; single callee).
- BF16 (`0x10`) and F16 (`0x0A`) inputs are eligible unconditionally; **F32 (`0x0B`) is eligible only under an opt-in numeric cast** (`whatToCast` ∈ {matmult, all} ∧ `castType` ∈ {bf16, fp16}). **CONFIRMED** (compare chain @`0x1f3f460`).
- `"sunda"` gates the lowering as a target-instance discriminator compared against `module.opts[+8]`. **STRONG** (`compare("sunda")` precedes the call; `sunda` string in ELF).

> **GOTCHA —** the pass name suggests a broad "native kernel router". It is not: it routes exactly one idiom (attention). MLP goes through `#56`; everything else falls out of `castEligible` / the `"sunda"` gate. Do not model a wide dispatch table here.

### Algorithm — the rewriter (`lowerMMSoftmaxMMToAttentionKernelWithConfig` @`0x1f35a80`)

Signature (nm-confirmed): `(HloComputation*, HloInstruction*, m, m, m, m, m, PrimitiveType)` — **five `unsigned long` dimension params + the cast/element type**. The 36 KB body holds the full `xla::match::` DSL over a `matmul → (scale) → softmax → matmul` chain. On a match it emits exactly **1 `CustomCall` + 5 `Reshape` + 3 `Transpose`** (callee count). Variant descriptors carried as log strings:

| Descriptor (verbatim) | Meaning |
|---|---|
| `SD without custom softmax, without LHS/RHS swap` | Stable-Diffusion attn, vanilla softmax, no swap |
| `SD without custom softmax, with LHS/RHS swap` | operands swapped |
| `SD with custom softmax, without LHS/RHS swap` | custom (masked) softmax |
| `SD with custom softmax, with LHS/RHS swap` | both |
| `LLM without custom softmax, without LHS/RHS swap` | causal / LLM-shaped attn |

Match / cast trace strings: `"Found match ("`, `" with params N: "`, `") for native kernel "`, `" Casting "`, `" Type: "`. The swap / no-swap discriminator is the lambda `…WithConfig::{lambda(bool)#2}::operator()` @`0x1f14440`. Replacement is `computation->ReplaceInstruction(inst, newInst)`; a reduce-scatter / transpose-MM2 variant uses `ReplaceInstruction(inst, newInstRsTpMM2)` @`0x3c2540`.

### CustomCall construction (`0x1f3d134`–`0x1f3d1a5`)

```c
serializeConfig(&backend_config_str,                 // out blob
                &psum_dims,    /* tuple<m,m>   */     // PE-array PSUM tile shape
                &sb_dims,      /* tuple<m,m,m> */     // SBUF on-chip tile shape (3 dims)
                &whatToCast, &castType);              // xla::whatToCast / xla::castType globals

target_name = xla::target;                            // cs:_ZN3xlaL6targetE @0x1f3d17f
                                                      //   = AttentionMMSoftmaxMM[WithoutSwap] / Causal…

HloInstruction* cc = HloInstruction::CreateCustomCall(
        output_shape,                                 // from computeMMSoftmaxMMToAttentionKernelSbShape
        operands_span,                                // inlined_vector<HloInstruction*,2>
        /*target=*/        target_name,
        /*backend_config=*/ backend_config_str,
        /*api_version=*/   1);                         // push 1 @0x1f3d168
```

### `backend_config` schema (`serializeConfig` @`0x1f2a280`)

The serialized native-attention config carries exactly these keys (verbatim tokens — the only meaningful strings in `serializeConfig`):

| Key | Value source | Meaning |
|---|---|---|
| `psum_shape` | `tuple<m,m>` | PE-array PSUM tile shape |
| `sb_shape` | `tuple<m,m,m>` | SBUF on-chip tile shape (3 dims) |
| `auto_cast` | `whatToCast` | which tensors to up-cast (`matmult` / `all`) |
| `auto_cast_type` | `castType` | `bf16` / `fp16` |
| `kernel_name` | `xla::target` | which attention variant to dispatch |

**CONFIRMED** — these are the five tokens in `serializeConfig`; together they tie the `CustomCall` to a native kernel and its tile geometry. In `hlo2penguin` the same builder (`0x1fe9b20`) emits an `nlohmann::json` object (key `"kernel_name"`); the JSON wire syntax (delimiters / ordering) is **INFERRED** — only the key names are CONFIRMED.

### Dispatch table (HLO pattern → kernel → config)

| HLO pattern matched | dtype gate | target (`kernel_name`) | Conf |
|---|---|---|---|
| MM→softmax→MM (SD, no custom softmax) | BF16/F16, or F32 w/ cast | `AttentionMMSoftmaxMM` | HIGH |
| …operands swapped | same | `AttentionMMSoftmaxMMWithoutSwap` | HIGH |
| …causal / LLM, custom softmax, no swap | same | `CausalAttentionMMSoftmaxMMWithoutSwap` | HIGH |

Master gate: global `qword_9A39A58` (in `hlo2penguin`, `xla::enableNativeKernelAttention` @`0x9c6d680`, default FALSE) **AND** target == `"sunda"`. The bf16-only sub-variant adds the `enableNativeKernelAttentionBF16` @`0x9c6d5c0` gate (§4). **CONFIRMED** gate; **HIGH** variant map.

> **NOTE —** the SBUF shape formula in `computeMMSoftmaxMMToAttentionKernelSbShape` (signature `(m, m, bool, PrimitiveType, bool)` — note **two** `m` dims + a causal bool + dtype + a trailing bool, *not* five dims) is not transcribed here; the `whatToCast=="matmult"` compare inside it toggles a `+2 / +0` term in the partition-byte computation, i.e. casting matmul intermediates to bf16 changes the state-buffer footprint reserved. **STRONG** (compare site @`0x1fce4b8` in `hlo2penguin`). See [arch/sbuf-psum-geometry](#) for the SBUF/PSUM tiling model the `psum_shape`/`sb_shape` tuples feed.

---

## 3. #56 `LowerToNKIKernelCC` — the MLP raise

### Purpose

Fold an MLP dot-chain (`dot2(activation(dot1(x)))`) into a single `AwsNeuronMLPNKI` `CustomCall`. Pure structural match — no dtype gate, no env flag, no cast config.

### Algorithm — `Run` (`0x1f48300`, 157 B)

```c
// xla::LowerToNKIKernelCC::Run
StatusOr<bool> Run(module, threads) {
    HloComputation* comp = module.entry_computation();          // 0x1f48317
    bool changed = false;
    for (HloInstruction* inst : comp->MakeInstructionPostOrder())   // 0x1f48326
        changed |= lowerMLPToNKIKernel(comp, inst);             // 0x1f48346 (StatusOr<bool>, OR-accum)
    return changed;
}
```

**CONFIRMED** — four callees only: `entry_computation`, `MakeInstructionPostOrder`, `lowerMLPToNKIKernel`, `operator delete`.

### Algorithm — `lowerMLPToNKIKernel` (`0x1f47030`, 4790 B)

Builds a nested `xla::match::AllOf<…>` rooted at a **second dot `dot2` (kDot = `0x2E`)** via `WithBinaryOperandsAnyOrder` / `WithOperand` chains. Opcode immediates loaded into the pattern (anchored to `HloOpcodeString` @`0x96bb550`): `0x2E dot`, `0x6E slice`, `0x5B reshape`, `0x22 concatenate`, `0x31 divide`, `0x3A get-tuple-element`, `0x76 multiply`, `0x2F dynamic-reshape`, among others — i.e. the MLP idiom `dot1 → activation/normalize (mul/div/reshape/concat) → dot2`.

```c
bool lowerMLPToNKIKernel(comp, root) {
    if (!pattern.Match(/*dot2=*/root)) return false;            // 0x1f47dca
    HloInstruction* op0 = root->mutable_operand(0);             // 0x1f47e76  operands[0]
    HloInstruction* op1 = root->mutable_operand(1);             // 0x1f47eca  operands[1]
    HloInstruction* op2 = innerInst->mutable_operand(1);        // 0x1f47f1e  operands[2] (activation input)
    Shape shape = innerInst->shape();                           // 0x1f47f6d

    // target name assembled byte-by-byte (0x1f47f88..0x1f47ffc), len 0x0F = 15:
    //   "AwsNeuro" (0x6F7275654E737741) + "NK" (0x4B4E) + "nMLP" (0x504C4D6E) + "I" (0x49)
    //   => "AwsNeuronMLPNKI"
    HloInstruction* cc = HloInstruction::CreateCustomCall(
            shape,
            {op0, op1, op2},
            /*target=*/        "AwsNeuronMLPNKI",
            /*backend_config=*/ "",                              // empty std::string (var_1B0=0)
            /*api_version=*/   1);                               // push 1 @0x1f47f86

    comp->AddInstruction(cc);                                   // 0x1f48077
    comp->ReplaceInstruction(root, cc);                        // 0x1f480cb
    root->ReplaceAllUsesWith(cc);                              // 0x1f48119
    return true;
}
```

> **NOTE —** unlike `#37`, `#56` carries **no cast config and no dispatch flag** — it is a pure structural MLP fusion. Routing criterion is the structural match alone; the dtype is whatever the dot-chain already had. The native-kernel cast-type policy of §4 does **not** touch the MLP path. **CONFIRMED** (byte-decode + CreateCustomCall args; empty config).

---

## 4. The Native-Kernel Cast-Type Policy (`hlo2penguin`)

### Purpose

Decide the **storage / I/O dtype** of a native attention kernel's matmul operands and intermediates, independently of the graph-wide `--fp32-cast` autocast pass. The default casts matmul intermediates to BF16.

### `SetNativeKernelCastType` (`0x1f93060`)

The programmatic setter (called from the Python driver / `CompileCommand`):

```c
// xla::hilo::SetNativeKernelCastType(const std::string& s)
void SetNativeKernelCastType(s) {
    HloPassOptions* opt = xla::hilo::options;                // ManagedStatic @0x9c6d060
    if (opt == 0) { RegisterManagedStatic(...); reload; }
    opt[+0x390].assign(s);                                   // store the cast-type string
    if (opt[+0x3f8] != 0)                                    // a std::function callback is registered
        return ((UniqueFunction)opt[+0x400])(opt[+0x3e8]);  // tailcall: propagate the new value
    else
        __throw_bad_function_call();
}
```

The cast-type is also a `cl::opt<string>` registered in `HloPassOptions::HloPassOptions()` — arg `native-kernel-cast-type`, help `"Cast which kernel intermediates and to what type"`, valdesc `"type"`, **default literal `"matmult-to-bf16"`** (assembled inline @`0x1f93d5e`, len 15). The `cl::opt`'s own change-callback is a no-op (`_M_invoke = ret`); the value just sits in the option and is split/consumed later. **CONFIRMED** (string + nm address; `matmult-to-bf16` in ELF).

### Grammar — `"<what>-to-<type>"`

The string splits on `-to-` into two pieces compared against enum literals in `LowerToCustomNativeKernel::Run` (`hlo2penguin` @`0x1ffed00`..):

| Half → global | Accepted values | Compare site |
|---|---|---|
| `<what>` → `xla::whatToCast` @`0x9c6d540` | `matmult`, `all` | `0x1ffed00` / `0x1ffed13` |
| `<type>` → `xla::castType` @`0x9c6d520` | `bf16`, `fp16` | `0x1ffed2a` / `0x1ffed49` |

So the grammar is `{matmult, all}` × `{bf16, fp16}`, default `matmult-to-bf16` (⇒ `whatToCast="matmult"`, `castType="bf16"`). **CONFIRMED**. There is **no** `fp32`/`fp8` branch on this path — distinct from the general `--fp32-cast` autocast which carries `{bf16, fp16, fp32r, fp8e4}`. The two globals default to empty and are populated at compile-config build time (the hidden mirror `native-kernel-auto-cast` @`0x9c67fa0`, same `matmult-to-bf16` default, feeds them via `GetHiloCompileConfig`); the exact `substr("-to-")` split was not pinned to one block — **INFERRED** but the four tokens and the default literal are CONFIRMED.

### `enable-native-kernel-attention-bf16` — the dtype-match flag

Two `cl::opt<bool>`, both default FALSE (registered in `_GLOBAL__sub_I__ZN3xla27enableNativeKernelAttentionE` @`0x1febd30`):

- `enable-native-kernel-attention` → `xla::enableNativeKernelAttention` @`0x9c6d680` — help *"Enable hlo pattern match for native kernel attention"*. This is the master `#37` gate (`Run` reads it first; 0 ⇒ skip).
- `enable-native-kernel-attention-bf16` → `xla::enableNativeKernelAttentionBF16` @`0x9c6d5c0` — help *"Enable hlo pattern match for native kernel attention when tensors are in bf16"*.

The decision point inside `lowerMMSoftmaxMMToAttentionKernel` (`hlo2penguin` @`0x1ffe414`):

```c
if (enableNativeKernelAttentionBF16.value == 1)   // flag ON
    goto match;                                   //   -> match REGARDLESS of input dtype, force bf16 intermediates
if (elem_type == 0x10 /* BF16 */)                 // flag OFF
    goto match;                                   //   -> match only when tensors are ALREADY bf16
else
    goto bail;                                     //   -> skip
```

Additional shape gates at the same site (**CONFIRMED**): sequence dim `> 0x3ef` (>1007, long-context only); head dim `<= 0x80` (≤128); computed SB bytes `<= 0x30000` (196608, must fit the state buffer). The companion F32 check `cmp r15d, 0xb` (=11=F32) in `…WithConfig` @`0x1ffc8cb` gates the F32 path. (XLA `PrimitiveType`: F16=10, F32=11, BF16=16 — consistent with both immediates.) **STRONG**.

### The storage-vs-accumulate split

> **QUIRK — `matmult-to-bf16` sets storage dtype, not accumulation dtype.** The cast-type policy governs only the **I/O / intermediate storage** dtype (bf16/fp16) of the fused attention kernel: QKᵀ inputs, the softmax statistics, and PV inputs as held in SBUF. It does **not** and **cannot** change the matmul *accumulation* dtype — the Trainium/Inferentia PE-array matmul always accumulates into the **fp32 PSUM** bank (hardware-fixed). So `matmult-to-bf16` means "feed/store the matmul operands in bf16"; the dot product still accumulates in fp32 PSUM and the bf16/fp16 result is read back out of PSUM. The binary keeps these on **separate axes**: the kernel-config vocabulary carries an independent `dot_accumulate_type` / `accumulation_mode` / `allow_imprecise_accumulation` notion (all three strings present in `hlo2penguin`), distinct from the cast-type knob. Cross-ref [Part 9 — fp32 accumulation](#) and [arch/sbuf-psum-geometry](#). **STRONG** (cast path CONFIRMED; the fp32-PSUM hardware fact is established in Part 9; the separate accumulation strings are CONFIRMED in the ELF).

### Where the cast directive reaches the kernel

The compiler does **not** run the generic autocast pass for native kernels. Instead `whatToCast`/`castType` are baked into the `CustomCall`'s serialized `backend_config` (`auto_cast` / `auto_cast_type` keys, §2), and `HloInstruction::CreateConvert` ops to the target dtype are inserted around the fused region; the kernel itself realises the intermediate dtype. The host-side log proves the intent: `" Casting " <whatToCast> " to " <castType> " for native kernel "`. **CONFIRMED** (string fragments in ELF, streamed @`0x1ffd3a7`..).

---

## 5. #38 `DecomposeAttention` — the inverse lowering

### Purpose

The inverse of `#37`: given a fused / native attention root, expand it back into the explicit `dot(Q,Kᵀ) → softmax → dot(P,V)` subgraph (math ops), where softmax is the numerically stable `exp(x − rowmax) / Σ exp(x − rowmax)`. Used to canonicalize / undo a native attention when the kernel is not selected.

### Algorithm — match (`0x1e9d160`)

Post-order over the entry computation. Per inst, builds a match pattern rooted at **kDot (`0x2E`)** (`r9d=0x2E` @`0x1e9d24d`; `MatchOption` seed `r8d=0x63`) with two operand sub-patterns, collecting matched attention roots into a worklist. The matched root is the **second matmul (P·V)** of an attention block whose first operand chain is a masked-softmax over a first matmul (Q·Kᵀ). The mask explorer is `xla::(anon)::Mask::Mask(HloInst*, HloInst*, HloInst*, bool causal)` @`0x1e9bcb0`. Diagnostics confirm the explored structure:

- `Expected to find masking select while exploring attention softmax`
- `Expected to find divide while exploring attention softmax!`
- `Failed to match on native softmax!`
- `Expected masked dot while exporing attention softmax!` *(sic — "exporing")*

### Algorithm — decomposition rewrite

All `Make*Hlo` calls disasm-anchored; HloOpcode immediates resolved via `HloOpcodeString` (case value == enum value):

```c
// --- recover Q, K, V, mask from the matched root ---
qk      = MakeDotHlo(Q, K, dnums, prec, …);                       // 0x1e9d47d  (Q·Kᵀ, dot=0x2E)
qk2     = MakeDotHlo(…);                                           // 0x1e9d545  (batched/second construction)
mask    = Mask(maskOp0, maskOp1, maskOp2, isCausal);              // 0x1e9d5da / 0x1e9d609  (@0x1e9bcb0)
scaleC  = MakeR0ConstantHlo<float>(comp, scale);                  // 0x1e9d64e
scale   = MakeConvertToHlo(scaleC, qk.element_type());           // 0x1e9d665

// --- softmax numerator: stable max-subtract-exp ---
rowmax1 = MakeReduceHlo(masked_qk, scale, dims, MAXIMUM /*0x43*/);// 0x1e9d6a2
rowmax2 = MakeReduceHlo(rowmax1,  …,    dims, MAXIMUM /*0x43*/);  // 0x1e9d6f1
rowmax  = MakeBinaryHlo(MAXIMUM /*0x43*/, rowmax2, …);           // 0x1e9d726
bcmax   = MakeBroadcastHlo(rowmax, out_dims, bcast_dims);         // 0x1e9d825
shifted = MakeBinaryHlo(SUBTRACT /*0x72*/, x, bcmax);            // 0x1e9d840
expv    = MakeUnaryHlo(EXPONENTIAL /*0x33*/, shifted);           // 0x1e9d866

// --- softmax denominator: sum-reduce ---
zeroC   = MakeR0ConstantHlo<float>(comp, 0.0);                    // 0x1e9d892
zero    = MakeConvertToHlo(zeroC, expv.element_type());          // 0x1e9d8a9
sum1    = MakeReduceHlo(expv, zero, dims, ADD /*0x01*/);         // 0x1e9d8dc
sum2    = MakeReduceHlo(…,           dims, ADD /*0x01*/);        // 0x1e9d9d3
// … MakeBinaryHlo / MakeBroadcastHlo (sum combine + broadcast)  // 0x1e9d966..0x1e9db01

// --- normalize + second matmul (P·V) ---
probs   = MakeBinaryHlo(DIVIDE /*0x2C*/, expv, bcast_sum);
pv      = MakeDotHlo(probs, V, dnums, …);                        // 0x1e9dbc5 / 0x1e9dc81  (dot=0x2E)
out     = MakeBinaryHlo(…);                                       // 0x1e9dd03  (final scale/add)

attention.root->parent()->ReplaceInstruction(attention.root, decomposedSMV.fusion);  // 0x1e9dd31
```

Opcode anchors (`HloOpcodeString` @`0x96bb550`, switch, ncases=123 — case value == enum value): `0x01 add`, `0x2C divide`, `0x2E dot`, `0x33 exponential`, `0x43 maximum`, `0x72 subtract`, `0x76 multiply`. **CONFIRMED**.

| Input (matched) | Output subgraph | Builders | Conf |
|---|---|---|---|
| root = kDot over masked-softmax over kDot | `dot(Q,Kᵀ)` → `−max` → `exp` → `÷ Σ` → `dot(P,V)` | `MakeDotHlo`×N, `MakeReduceHlo`(max,max,add,add), `MakeBinaryHlo`(max,subtract,divide,…), `MakeUnaryHlo`(exp), `MakeBroadcastHlo`, `MakeConvertToHlo`, `MakeR0ConstantHlo<float>`, `Mask`×2 | CERTAIN |

> **CORRECTION —** `DecomposeAttention` decomposes to the matmul-softmax-matmul **subgraph** (math ops), **not** to a fused custom-call. The output is a single fused result node `decomposedSMV.fusion` built from the `Make*` primitives; softmax is the stable `exp(x − rowmax) / Σ exp(x − rowmax)`. Cross-ref [Part 6.7.6 — attention kernels](#) for the kernel-side counterpart.

---

## 6. Reconstructed Signatures (binary-derived)

```cpp
// #37  (hlo-opt addresses; nm-confirmed)
StatusOr<bool> xla::LowerToCustomNativeKernel::Run(HloModule*,
        const absl::flat_hash_set<std::string_view>&);                        // 0x1f3eeb0
bool xla::lowerMMSoftmaxMMToAttentionKernel(HloComputation*, HloInstruction*); // 0x1f3e830
bool xla::lowerMMSoftmaxMMToAttentionKernelWithConfig(HloComputation*, HloInstruction*,
        unsigned long, unsigned long, unsigned long, unsigned long, unsigned long,
        PrimitiveType);                                                       // 0x1f35a80
Shape xla::computeMMSoftmaxMMToAttentionKernelSbShape(unsigned long, unsigned long,
        bool, PrimitiveType, bool);                                          // 0x1f0cc00
void  xla::serializeConfig(std::string&, std::tuple<m,m>&, std::tuple<m,m,m>&,
        std::string& whatToCast, std::string& castType);                     // 0x1f2a280 (hlo-opt)
// globals: xla::dynamicDmaBytesPerPart, xla::whatToCast, xla::castType, xla::target, qword_9A39A58

// #56
StatusOr<bool> xla::LowerToNKIKernelCC::Run(HloModule*,
        const absl::flat_hash_set<std::string_view>&);                        // 0x1f48300
bool xla::lowerMLPToNKIKernel(HloComputation*, HloInstruction*);              // 0x1f47030

// #38
StatusOr<bool> xla::DecomposeAttention::Run(HloModule*,
        const absl::flat_hash_set<std::string_view>&);                        // 0x1e9d160
xla::(anonymous)::Mask::Mask(HloInstruction*, HloInstruction*, HloInstruction*, bool causal); // 0x1e9bcb0

// cast policy  (hlo2penguin addresses; nm-confirmed)
void xla::hilo::SetNativeKernelCastType(const std::string&);                  // 0x1f93060
// globals: xla::enableNativeKernelAttention @0x9c6d680, ...BF16 @0x9c6d5c0,
//          xla::whatToCast @0x9c6d540, xla::castType @0x9c6d520

// custom-call factory:
HloInstruction* HloInstruction::CreateCustomCall(Shape, Span<HloInstruction* const>,
        std::string_view target, std::string backend_config, CustomCallApiVersion); // 0x964eac0
```

---

## 7. Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

1. **The three pass `Run` bodies and workers exist at the cited `hlo-opt` addresses.** Re-checked via `nm hlo-opt`: `DecomposeAttention::Run` @`0x1e9d160`, `LowerToCustomNativeKernel::Run` @`0x1f3eeb0`, `LowerToNKIKernelCC::Run` @`0x1f48300`, `lowerMLPToNKIKernel` @`0x1f47030`, `lowerMMSoftmaxMMToAttentionKernel` @`0x1f3e830`, `…WithConfig` @`0x1f35a80`, `computeMMSoftmaxMMToAttentionKernelSbShape` @`0x1f0cc00`, `serializeConfig` @`0x1f2a280`. **All eight CONFIRMED** byte-exact. (Correction applied: `SbShape` signature is `(m, m, bool, PrimitiveType, bool)` — two dims, not "five params"; the five-param form belongs to `…WithConfig`.)

2. **The `#37` target is a variant string, not `AwsNeuronCustomNativeKernel`.** `AttentionMMSoftmaxMM`, `AttentionMMSoftmaxMMWithoutSwap`, `CausalAttentionMMSoftmaxMMWithoutSwap`, `AwsNeuronCustomNativeKernel`, `AwsNeuronCustomNativeKernel_Sim` all appear as literal strings in the `hlo-opt` ELF (`rg -a` confirmed); the target is taken from `xla::target` @`0x1f3d17f`. **CONFIRMED.** `AwsNeuronCustomNativeKernel` is the class id, not the emitted target.

3. **`AwsNeuronMLPNKI` is the `#56` target.** It does **not** appear as a contiguous ELF string (re-checked: absent from `rg -a`), which corroborates the byte-build at `0x1f47f88` (`"AwsNeuro"+"NK"+"nMLP"+"I"`, len 15). **CONFIRMED** — and the absence is itself evidence of the inline assembly, not a failure.

4. **`matmult-to-bf16` sets storage, not accumulation.** The default literal `matmult-to-bf16`, the grammar tokens (`matmult`/`all`/`bf16`/`fp16`), and the three independent accumulation strings (`dot_accumulate_type`, `accumulation_mode`, `allow_imprecise_accumulation`) all confirmed present in `hlo2penguin` via `rg -a`. The fp32-PSUM accumulation fact is hardware-fixed (Part 9). The cast knob has **no** fp32 branch, so it cannot express the accumulation dtype — these are structurally separate axes. **STRONG.**

5. **The cast-policy symbols live only in `hlo2penguin`.** `SetNativeKernelCastType` @`0x1f93060`, `enableNativeKernelAttention` @`0x9c6d680`, `…BF16` @`0x9c6d5c0` all nm-confirmed in `hlo2penguin`; absent from `hlo-opt`'s symbol table. The pass bodies exist in both binaries (`LowerToCustomNativeKernel::Run` @`0x1ffe750` in `hlo2penguin`, @`0x1f3eeb0` in `hlo-opt`). **CONFIRMED.**

Tagged-INFERRED items (not fabricated): the exact `nlohmann::json` wire syntax of `backend_config`; the `substr("-to-")` split routine; the `xla::target` → variant decision tree inside the 36 KB rewriter; the exact `computeMMSoftmaxMMToAttentionKernelSbShape` tile formula. All are flagged in-place above.
