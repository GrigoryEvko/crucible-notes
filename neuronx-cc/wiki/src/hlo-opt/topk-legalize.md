# TopK Legalization

> *All addresses on this page are virtual addresses (VMA) for neuronx-cc 2.24.5133.0+58f8de22 (`hlo-opt`, cp310); resolve via `objdump --start-address` or the VMA-keyed `disasm/` sidecars. VA ≠ raw file offset: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000 (section headers). Other builds will differ.*

## Abstract

`legalize-topk` is registration-order pass **8** in the `hlo-opt` `--passes` table, class `xla::LegalizeTopK`. Despite the name, it is *not* a sort+slice idiom recognizer. It is a narrow **dtype specializer**: it scans the entry computation for `kCustomCall` instructions whose `custom_call_target()` is exactly **`AwsNeuronTopK`**, gates on the element type (must be F16, BF16, or F32), parses the integer `k` out of the `backend_config` opaque string, builds an **S64 index/iota constant** over the operand's minor dimension, and rewrites the op into a per-dtype custom-call named **`TopK_f32`**, **`TopK_f16`**, or **`TopK_bf16`** with operands `{original_input, iota}`. The string `AwsNeuronTopK` (`0x25f115`) is referenced by exactly one function in the binary — `LegalizeTopK::Run` (`0x1f01260`) — so the *producer* of `AwsNeuronTopK` lives upstream (driver-side Frontend / HLOToTensorizer) and is out of this binary.

The familiar reference frame is upstream XLA's `TopkRewriter`/`HloTopKInstruction`/`TopkDecomposer`, all statically linked into this same ELF but on a *different* code path: `TopkRewriter::SortIsInTopK` (`0x29e24b0`) recognizes the sort+slice idiom and emits a custom-call named plain `"TopK"` via `CreateTopKCustomCall` (`0x29e4930`). `LegalizeTopK` consumes the Neuron-specific `AwsNeuronTopK` form, never the stock `"TopK"` one. Think of this pass as the last HLO-level hop before the legalized `TopK_*` op enters NKI/KLR codegen.

The legalized `TopK_*` op resolves downstream to an internal NKI kernel (the `router_topk` / `topk_reduce` family), which lowers through KlirToBirCodegen into the BIR **DVE reduce/index** instructions (`InstructionType` 88–91: `Max`, `MaxIndex`, `MatchReplace`, `MaxIndexAndMatchReplace`), realized by the DVE Max-8 / Find-Index-8 / Match-Value-Load microcode primitives. That crosswalk is documented in §4.

For reimplementation, the contract is:

- The match predicate: `kCustomCall` + target `== "AwsNeuronTopK"` + element type ∈ {F16, BF16, F32}, scanned over the **entry computation only**, in post-order.
- The rewrite: append an S64 iota constant, look up the dtype-specialized target name, emit `CreateCustomCall(shape, {input, iota}, name, /*opaque=*/"", /*api=*/API_VERSION_ORIGINAL)`, then `ReplaceInstruction`.
- The `backend_config` schema: a **bare base-10 integer** = `k` (no `dim`/`sorted` fields), validated to `INT32` range; consumed for validation only and *dropped* (the new op carries an empty config).

| | |
|---|---|
| **`name()` key** | `legalize-topk` (`0x247ac1`, len 13) → `name()` @ `0x1f00f80` |
| **`Run` / entry** | `xla::LegalizeTopK::Run` @ `0x1f01260` (2879 B, 41 callees) |
| **`Run` .cold** | `0x1f010a2` (186 B, exception cleanup) |
| **Vtable** | `0x40e350` (vptr = +0x10); factory `_M_invoke` `0x1e71150` |
| **Static-init** | `GLOBAL__sub_I…` @ `0x1f00fb0` (241 B) builds the `topkNames` map |
| **Match target** | `AwsNeuronTopK` (`0x25f115`, single-xref `0x1f01321`) |
| **Emitted targets** | `TopK_f32` / `TopK_f16` / `TopK_bf16` (immediates @ `0x1f01bcf`/`bee`/`c0d`) |
| **IR level** | XLA HLO, post `TopkRewriter`, pre-NKI codegen |
| **Iteration scope** | `entry_computation()` only, instruction post-order |

---

## LegalizeTopK::Run

### Purpose

Consume an already-emitted `AwsNeuronTopK` custom-call and specialize it by dtype into one of three concretely-named `TopK_<dtype>` custom-calls, attaching the S64 index iota that the downstream NKI kernel consumes as its lane-index source. The pass performs no comparator construction, no `CreateSort`, and no reduce-window synthesis — it is purely a recognize-and-relabel rewrite over the entry computation.

### Entry Point

```text
xla::LegalizeTopK::Run  (0x1f01260, 2879 B)          ── pass body (HloPassInterface::Run)
  ├─ HloComputation::MakeInstructionPostOrder  (0x1f012c2 → 0x9634ab0)  ── scan order
  ├─ __dynamic_cast                            (0x1f0131c)              ── → HloCustomCallInstruction*
  ├─ std::string::compare("AwsNeuronTopK")     (0x1f01334)              ── target gate
  ├─ Shape::array_state / LiteralUtil::CreateR0<long>  (0x1f01387/0x1f01399)  ── iota length
  ├─ ShapeUtil::MakeValidatedShape(S64, {n})   (0x1f014d7)              ── index shape
  ├─ MutableLiteralBase::PopulateR1<long>      (0x1f0154b)              ── fill iota
  ├─ strtol(raw, &end, 10)                     (0x1f0159c)              ── parse k
  ├─ HloInstruction::CreateConstant            (0x1f01738)              ── materialize iota
  ├─ HloInstruction::CreateCustomCall          (0x1f01901)             ── emit TopK_<dtype>
  └─ HloComputation::ReplaceInstruction        (0x1f019a2 → 0x963fe50) ── splice in
```

### Algorithm

Reconstructed instruction-by-instruction from the `0x1f01260` disasm. Callee/immediate facts are CERTAIN; loop/skip control flow is MEDIUM (no Hex-Rays — reconstructed from jump targets). Anchors in `«addr»`.

```c
StatusOr<bool> LegalizeTopK::Run(HloModule* m, const flat_hash_set<string_view>& threads) {
    // topkNames: a lazily-built, process-lifetime DenseMap<PrimitiveType,std::string>
    //   F16(0x0A)->"TopK_f16", F32(0x0B)->"TopK_f32", BF16(0x10)->"TopK_bf16"
    //   built under __cxa_guard in static-init region; see "topkNames table" below.

    HloComputation* entry = m->entry_computation();        // «[rdx+38h]»
    CHECK(entry != nullptr);                               // "nullptr != entry_computation_" hlo_module.h:164

    for (HloInstruction* inst : entry->MakeInstructionPostOrder()) {   // «0x1f012c2»
        if (inst->opcode() != kCustomCall) continue;       // opcode byte gate
        auto* cc = dynamic_cast<HloCustomCallInstruction*>(inst);      // «0x1f0131c»
        if (cc == nullptr) continue;
        if (cc->custom_call_target().compare("AwsNeuronTopK") != 0)    // «0x1f01321 / 0x1f01334»
            continue;

        // --- dtype gate: operand0 element type must be F16 / F32 / BF16 ---
        PrimitiveType et = cc->operand(0)->shape().element_type();     // ecx «0x1f01356»
        if ((uint32_t)(et - 0x0A) > 1 && et != 0x10)        // «lea eax,[rcx-0Ah]; jbe 0x1f01364; cmp ecx,10h 0x1f01366»
            continue;                                       //   accepts {0x0A=F16, 0x0B=F32, 0x10=BF16}

        // --- build S64 iota over the operand's minor (last) dimension ---
        const Shape& s = cc->operand(0)->shape();
        int64 n = s.array_state(); /* minor-dim element count */        // «array_state 0x1f01387/0x1f013ba»
        Shape idxShape = ShapeUtil::MakeValidatedShape(/*S64=*/5, {n}); // «mov esi,5 0x1f014c4; 0x1f014d7»
        Literal idxLit(idxShape);
        idxLit.PopulateR1<long>(/* 0..n-1 index vector */);            // «0x1f0154b»

        // --- parse k from backend_config (a bare base-10 integer) ---
        std::string raw = cc->backend_config().raw_string();
        errno = 0; char* end;
        long k = strtol(raw.c_str(), &end, 10);             // «0x1f0159c»
        if (end == raw.c_str())  throw invalid_argument("stoi");        // «0x1f01cdc»
        if (errno == ERANGE)     throw out_of_range("stoi");           // «[rbx]==0x22 0x1f01cd2»
        if (k < INT32_MIN || k > INT32_MAX) throw out_of_range("stoi"); // «0x1f015c5/0x1f015cf vs 0x7FFFFFFF»
        (void)k;   // validated, then DISCARDED — the new op carries NO backend_config

        // --- materialize iota and assemble {input, iota} ---
        HloInstruction* iota = entry->AddInstruction(
                                   HloInstruction::CreateConstant(std::move(idxLit)), "");  // «0x1f01738»
        HloInstruction* operands[2] = { cc->operand(0), iota };

        // --- look up dtype-specialized name and emit ---
        const std::string& newTarget = topkNames[et];       // "TopK_f32" / "TopK_f16" / "TopK_bf16"
        auto newCC = HloInstruction::CreateCustomCall(
                         cc->shape(),                        // output shape = original AwsNeuronTopK shape
                         absl::Span<HloInstruction* const>(operands, 2),
                         /*custom_call_target=*/ newTarget,
                         /*opaque(backend_config)=*/ "",     // EMPTY
                         /*api=*/ API_VERSION_ORIGINAL /*1*/);          // «push 1 0x1f018e3; 0x1f01901»
        HloInstruction* nn = entry->AddInstruction(std::move(newCC), "");
        CHECK_OK(entry->ReplaceInstruction(inst, nn));      // «0x1f019a2»
        // changed = true;
    }
    return /*StatusOr<bool>*/ changed;
}
```

> **GOTCHA —** `k` is parsed and range-checked but then thrown away. The emitted `TopK_<dtype>` op has an **empty** `backend_config` (`opaque=""`). Downstream identity of the op is `(target name, operand shapes, output shape)` only. A reimplementation that tries to propagate `k` onto the new op's config will diverge — the value of `k` is recovered downstream from the *output shape's* minor dimension, not from a config field.

> **QUIRK —** the second operand is an S64 iota of length = the **input's** minor dimension `n` (`0..n-1`), not the output's `k`. The NKI kernel uses this full-length index vector as the lane-index source for its argmax/`find_index8` passes; it is sliced to `k` internally, not here.

> **NOTE —** only `entry_computation()` is scanned (single `[rdx+38h]` read + null `CHECK`), in instruction post-order. This contrasts with passes such as `NeuronHloInstCombine` that iterate `module.computations()`. A `TopK` buried inside a fusion/while body is invisible to this pass.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `LegalizeTopK::Run` | `0x1f01260` | 2879 B | Pass body — match + rewrite | CERTAIN |
| `LegalizeTopK::Run.cold` | `0x1f010a2` | 186 B | Exception-cleanup landing pad | CERTAIN |
| `LegalizeTopK::name` | `0x1f00f80` | 11 B | Returns StringRef `legalize-topk` (len 13) | CERTAIN |
| `GLOBAL__sub_I…` | `0x1f00fb0` | 241 B | Static-init: builds `topkNames` under `__cxa_guard` | CERTAIN |
| `LiteralUtil::CreateR0<long>` | `0x1f01399` | — | Per-element iota literal helper | HIGH |
| `ShapeUtil::MakeValidatedShape` | `0x1f014d7` | — | Builds the S64 R1 index shape | CERTAIN |
| `MutableLiteralBase::PopulateR1<long>` | `0x1f0154b` | — | Fills the iota vector | CERTAIN |
| `HloInstruction::CreateCustomCall` | `0x1f01901` | — | Emits `TopK_<dtype>` (5-arg overload) | CERTAIN |
| `HloComputation::ReplaceInstruction` | `0x1f019a2` | — | Splices new op for old | CERTAIN |

### The `topkNames` table

Built once, lazily, under `__cxa_guard` in the static-init region; destroyed via `__cxa_atexit`. The three string payloads are stored as 8-byte little-endian inline immediates (SSO short strings) — decoded directly from the `mov rax, imm64` instructions:

| PrimitiveType | Gate | Immediate (`0x1f01b**`) | Decoded | Value |
|---|---|---|---|---|
| F32 = 11 (0x0B) | `lea/jbe` | `0x3233665F4B706F54` @ `0x1f01bcf` | `TopK_f32` | `"TopK_f32"` (len 8) |
| F16 = 10 (0x0A) | `lea/jbe` | `0x3631665F4B706F54` @ `0x1f01bee` | `TopK_f16` | `"TopK_f16"` (len 8) |
| BF16 = 16 (0x10) | `cmp ecx,10h` | `0x3166625F4B706F54` @ `0x1f01c0d` + byte `'6'` | `TopK_bf16` | `"TopK_bf16"` (len 9) |

The same table is the single source of truth for both the dtype gate and the emitted name, so they cannot disagree: the `jbe` accepts `et-0x0A ≤ 1` (F16/F32), the `cmp` accepts `0x10` (BF16), and the map is keyed on those exact ordinals.

> **NOTE —** XLA `PrimitiveType` ordinals here are F16=10, F32=11, BF16=16 — these match the DenseMap keys exactly. The gate is two comparisons (`lea eax,[rcx-0Ah]` then `jbe`, plus `cmp ecx,10h`), not a switch; a fourth float type (e.g. FP8) is silently skipped, not legalized.

---

## backend_config Schema

On the incoming `AwsNeuronTopK`, `backend_config` is a **bare base-10 integer string** equal to `k`. There is **no structured proto, no `dim` field, and no `sorted` field**. The reduction axis is implicit — the operand's minor (last) dimension, over which the iota is built. "Sorted" is implicit-true (top-k via the DVE Max/MaxIndex family is inherently ordered).

| Check | Disasm anchor | Diagnostic (catalog addr) |
|---|---|---|
| non-empty / parseable | `end == start` → `0x1f01cdc` | "A TopK configuration parsing error has occurred. Failed to parse backend_config parameter. … 'k' parameter for top-k selection." (`0x3bc650`) |
| `errno != ERANGE` (0x22) | `[rbx]==0x22` → `0x1f01cd2` | `std::out_of_range("stoi")` |
| `INT32_MIN ≤ k ≤ INT32_MAX` | `0x1f015c5` / `0x1f015cf` vs ±`0x7FFFFFFF` | "TopK custom operation does not support k values larger than %d. Found k=%d." (`0x369368`) |

The wider Neuron-TopK constraint set surfaces through the `hilo` diagnostic catalog (`lookup_cause` `0x759d7d0` / `lookup_resolution` `0x759fc20`), even though the *enforcing* checks for operand count and integer-type rejection are upstream of this pass:

| ErrorCode | Cause string | Addr |
|---|---|---|
| 67 | "Custom call operation mismatch: expected AwsNeuronTopK but found different operation in TopK print function. …" | `0x3eba68` |
| 68 | "TopK operation validation error: expected exactly 1 operand but found %d operands. …" | `0x375568` |
| 129 | "A TopK configuration parsing error has occurred. … 'k' parameter …" | `0x3bc650` |
| 194 | "TopK custom operation does not support 32-bit and 64-bit integer types." | `0x300578` |
| 195 | "TopK custom operation does not support k values larger than %d. Found k=%d." | `0x369368` |

Net contract: **exactly 1 operand; element type F16/BF16/F32 (no 32/64-bit int); k ≤ INT32_MAX.**

> **CORRECTION (D-B03) —** the upstream framed `xla::HloTopKInstruction` *does* carry explicit `k=` and `largest=` attributes (printed by `PrintExtraAttributesImpl` `0x9686460`). That is the *stock XLA-TopK* representation, **not** the `AwsNeuronTopK` opaque string consumed here. Do not conflate the two: the Neuron form is the bare integer, the XLA form is the structured instruction. Likewise `formatErrorMessage<PrimitiveType,PrimitiveType>` (`0x1eeb8d0`) is ArgMax-only — its sole caller is `LegalizeAwsNeuronArgMax::Run`, not TopK.

---

## HLO-TopK → BIR-DVE Crosswalk

The legalized `TopK_<dtype>` custom-call is realized by an internal NKI kernel (registered in `_INTERNAL_KERNEL_REGISTRY`), which lowers through KLR/KlirToBirCodegen into the BIR DVE reduce/index family. Each hop is anchored to a prior report (D-A03 / S2-04 / S2-06 / S2-08 / S2-09).

```text
 upstream XLA (same ELF, other path)        hlo-opt (this pass)            NKI ISA (nisa.*)          BIR InstructionType (88-91)        DVE microcode op
 ───────────────────────────────────        ────────────────────           ────────────────         ────────────────────────────       ─────────────────
 sort+slice idiom
   │ TopkRewriter::SortIsInTopK (0x29e24b0)
   │   TransformPatternToCustomCall (0x29e5930)
   ▼   → CreateTopKCustomCall (0x29e4930)  target="TopK"  (NOT AwsNeuronTopK)
 HloTopKInstruction (k=, largest=)

 Neuron Frontend (out-of-binary) ─emits─► AwsNeuronTopK ─[LegalizeTopK::Run 0x1f01260]─► TopK_f32/f16/bf16 (+S64 iota)
                                                                                            │ NKI kernel: router_topk / topk_reduce /
                                                                                            │   cascaded_max / rotational_topk
                                                                                            ▼
                                                                                          nisa.max8                  → codegenMax8                → 88 Max                      → "Max-8"
                                                                                          nisa.nc_find_index8        → codegenFindIndex8          → 89 MaxIndex                 → "Find-Index-8"
                                                                                          nisa.nc_match_replace8     → codegenMatchReplace8       → 90 MatchReplace             → "Match-Value-Load"
                                                                                          nc_match_replace_indices8  → codegenMaxIndexAndMatchReplace → 91 MaxIndexAndMatchReplace
```

**BIR `InstructionType` enum** (`InstructionType2string` @ `0x2d5bf0`, 110 values, CERTAIN per S2-04 §3.1):

| Inst | BIR opcode | NKI ISA | DVE microcode | Role |
|---|---|---|---|---|
| 88 | `Max` | `nisa.max8` | `Max-8` | 8-lane running maximum |
| 89 | `MaxIndex` | `nisa.nc_find_index8` | `Find-Index-8` | 8-lane argmax / find-index |
| 90 | `MatchReplace` | `nisa.nc_match_replace8` | `Match-Value-Load` | match-value-load / replace |
| 91 | `MaxIndexAndMatchReplace` | `nc_match_replace_indices8` | (fused) | fused argmax + match-replace |

KLR/Walrus codegen hooks (CERTAIN names, S2-06/S2-09): `BirCodeGenLoop` exposes `codegenMax8`, `codegenFindIndex8`, `codegenMatchReplace8`, `codegenMaxIndexAndMatchReplace`; `neuronxcc::backend::KlirToBirCodegen::codegenMatchReplace8(Ptr<klr::MatchReplace8>)` returns a `bir::InstMaxIndexAndMatchReplace`. NKI kernels using the family: `cascaded_max.py`, `router_topk.py`, `topk_reduce.py`, `rotational_topk_utils.py`, plus compiled `_private_kernels/router_topk.*.so`.

> **NOTE —** the exact DVE-engine integer opcode for each microcode name (`Max-8` / `Find-Index-8` / `Match-Value-Load`) is not byte-pinned (S2-08 §8 open gap). The BIR-Inst(88–91) → DVE-op mapping above is by **name correspondence** (MEDIUM on the precise microcode integers); the BIR enum ordinals and NKI ISA names are CERTAIN. The internal `TopK_*` kernel body (its per-`k`, per-dtype tiling of `max8`/`find_index8`/`match_replace8`) is a compiled `.so` not decoded here.

End-to-end: **`AwsNeuronTopK` → (legalize-topk) → `TopK_f32/f16/bf16` (+ S64 iota) → (NKI `router_topk`/`topk_reduce`) → `nisa.max8` / `nc_find_index8` / `nc_match_replace8` → (KlirToBirCodegen) → BIR Inst 88 `Max` / 89 `MaxIndex` / 90 `MatchReplace` / 91 `MaxIndexAndMatchReplace` → DVE Max-8 / Find-Index-8 / Match-Value-Load.**

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

1. **`AwsNeuronTopK` is consumer-only, single-xref.** `strings.json` `0x25f115` `referenced_by_functions` = `[LegalizeTopK::Run]`, single xref `from:0x1f01321`. Re-checked: no other function references it. **CONFIRMED.**
2. **Dtype gate = {F16=0x0A, F32=0x0B, BF16=0x10}.** Disasm `0x1f01358 lea eax,[rcx-0Ah]`, `0x1f01364 jbe`, `0x1f01366 cmp ecx,10h`. The `lea`/`jbe` admits `et∈{0x0A,0x0B}`; the `cmp` admits `0x10`. Matches the three `topkNames` keys. **CONFIRMED.**
3. **Emitted names are `TopK_f32/f16/bf16` from inline immediates.** `0x1f01bcf mov rax,3233665F4B706F54h` = `TopK_f32`; `0x1f01bee` = `TopK_f16`; `0x1f01c0d` = `TopK_bf1`(+`'6'`) = `TopK_bf16`. Little-endian decode verified. **CONFIRMED.** (Immediate addresses pinned to `0x1f01bcf/bee/c0d`; D-B03's `0x1f01c17`-region figures were the key-store sites, not the payload movs.)
4. **`k` is `strtol(...,10)`-parsed, INT32-checked, then discarded; new op carries empty config.** `strtol` @ `0x1f0159c`; range check vs `0x7FFFFFFF` @ `0x1f015c5/cf`; `CreateCustomCall` @ `0x1f01901` preceded by `push 1` (API_VERSION_ORIGINAL) @ `0x1f018e3` — the opaque arg slot is the empty string. **CONFIRMED.**
5. **S64 iota of length = input minor dim is the second operand.** `mov esi,5` (S64) @ `0x1f014c4` → `MakeValidatedShape` @ `0x1f014d7`; `PopulateR1<long>` @ `0x1f0154b`; `array_state` minor-dim read @ `0x1f01387`; `CreateConstant` @ `0x1f01738`. **CONFIRMED.** The 0-to-(n-1) value population is INFERRED from `PopulateR1<long>` + `LiteralUtil::CreateR0<long>` semantics (the per-element fill body was not opened) — tagged **INFERRED** for the precise index values, CONFIRMED for the S64 R1 shape and operand position.

---

## Related Components

| Name | Address | Relationship |
|---|---|---|
| `xla::TopkRewriter::SortIsInTopK` | `0x29e24b0` | Upstream idiom matcher; emits `"TopK"`, not `AwsNeuronTopK` |
| `xla::CreateTopKCustomCall` | `0x29e4930` | Builds the stock framed-TopK custom-call (different path) |
| `xla::HloTopKInstruction::PrintExtraAttributesImpl` | `0x9686460` | Prints `k=`/`largest=` for the stock XLA-TopK form |
| `xla::TopkDecomposerVisitor` | — | Inverse: expands framed TopK back to sort+slice (`CreateVariadicComparator`) |
| `xla::LegalizeAwsNeuronArgMax::Run` | — | Sibling legalizer; sole caller of `formatErrorMessage<PrimitiveType,PrimitiveType>` (`0x1eeb8d0`) |

## Cross-References

- [The hlo-opt Pass Registry (the --passes Table)](pass-registry.md) — resolves `legalize-topk` to order 8, vtable `0x40e350`, Run `0x1f01260`
- [CC-Op Decompose & Legalize Family](ccops-decompose-legalize.md) — sibling custom-call legalizers (ArgMax and the broader family)
- [NKI Top-K Primitives (6.7.12)](../nki/topk-primitives.md) — `router_topk` / `topk_reduce` kernels and the `max8`/`find_index8`/`match_replace8` ISA surface
- [GPSIMD Bitonic Top-K (Part 11)](../gpsimd/bitonic-topk.md) — the alternative GPSIMD-engine top-k path
- [DVE Max8 & Reduce/Index Family (2.16)](../isa/dve-max8.md) — the DVE microcode primitives BIR Inst 88–91 select
