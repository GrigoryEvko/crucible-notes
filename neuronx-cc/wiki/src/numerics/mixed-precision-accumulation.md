# Mixed-Precision Accumulation and Accumulator-dtype Rules

> *All symbols, addresses, and string offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/`, plus the native `libwalrus.so` / `libBIR.so`). cp310/11/12 are structurally identical but relayout the Cython string pool, shifting every per-module offset. For `.text`/`.rodata` in the native libs the virtual address equals the file offset; treat every address as version-pinned.*

## Abstract

`--enable-mixed-precision-accumulation` is the public, default-`true` flag whose name suggests it picks the accumulator dtype for every reduction in the program. It does not. The flag is a **pure pipeline toggle**: the Python frontend reads it, and when true it appends a *different* token — the tensorizer flag `accumulate-on-alu-dtype` — to the codegen argument list. That token sets one target property, `target.accumulate_on_alu_dtype`, and that property gates exactly one transform pass, `EnforceAluDTAcc`, in the Sunda (Trainium2-class) codegen flow. There is no native-side predicate anywhere that reads a string literal `mixed_precision_accumulation`; the flag's entire effect is the scheduling of that one pass.

What `EnforceAluDTAcc` does is narrow and specific: for each **ALU-engine running accumulation** — vector/pool reduce-sum and the three-pass BatchNorm — it walks *up* to the instruction that produces the accumulated operand and forces that producer's destination dtype to fp32 (the native ALU accumulator width), unless the fp32 promotion would overflow an SBUF partition. It touches reduce-sum and BatchNorm and **nothing else**; there is no matmul, TensorContract, or PE string anywhere in the module. The flag therefore protects the *summation* precision of ACT/POOL accumulators, not the output dtype and not the matmul.

The headline truth a reimplementer must internalize is that the **matmul accumulator is hardware-fixed fp32 and is entirely independent of this flag**. PSUM banks are physically a grid of 4-byte fp32 accumulator cells; `PSUMLegalization::widen_psum` rewrites any sub-4-byte matmul destination to a 4-byte fp32 dtype before the PE array ever writes it. The flag never reaches the PE array — the PE array does not need it, because PSUM cannot hold bf16 by construction, whereas SBUF can, so only the SBUF/ALU accumulators have a downcast to suppress. A third, separate layer sits above all of this: the XLA/StableHLO `DotAlgorithmAttr` (`getAccumulationType`, `getAllowImpreciseAccumulation`), converted by `xla::ConvertDotAlgorithm`, is the framework-level "accumulate in f32" control and is a distinct code path from the neuron flag. This page reverses all three and proves their independence.

For reimplementation, the contract is:

- The flag→token→property→pass binding chain, with the three Cython hops named at their decompiled call sites.
- `EnforceAluDTAcc`'s op-family scope (six transform methods, the min/max skip, the SBUF-budget guard) and its producer-chase algorithm.
- The proof that the matmul PSUM accumulator is fp32 *by legalization*, flag-independent, via `widen_psum`.
- The distinction between this flag, the `--fp32-cast` fp32r operand knob, and the upstream `DotAlgorithmAttr` accumulation_type — three separate mechanisms that all touch "accumulation precision."

| | |
|---|---|
| **CLI flag** | `--enable-mixed-precision-accumulation` (PUBLIC, default `true`) — owned by `driver/jobs/Frontend.*.so` |
| **Tensorizer token** | `accumulate-on-alu-dtype` — emitted by `Penguin.setup_tensorizer_args` (`pw @0x251b0`) |
| **Gating property** | `target.accumulate_on_alu_dtype` — read by `sunda/CodeGenFlow.codegen_optimization` (`generator5 @0x31520`) |
| **The pass** | `EnforceAluDTAcc(TargetLowering)` — `transforms/EnforceAluDTAcc.*.so` (cp310 sidecar `_ba63736b235d4fe7`) |
| **ALU accumulator dtype** | fp32 = `target.get_native_alu_dtype` (Tonga `pw @0x54f80`) |
| **Matmul accumulator dtype** | fp32, hardware-fixed — `PSUMLegalization::widen_psum @0x15592e0` (`libwalrus.so`) |
| **XLA separate layer** | `xla::ConvertDotAlgorithm(stablehlo::DotAlgorithmAttr) @0x2be2680` (`hlo-opt`) — *not* this flag |
| **Op-family scope** | reduce-sum + 3-pass BatchNorm; **NO matmul** |

---

## The Flag Is a Pipeline Toggle, Not a Codegen Predicate

### Purpose

`--enable-mixed-precision-accumulation` is the public, default-true CLI bool whose help text reads *"Always use full precision of the accelerator ALU when we do accumulation, never downcast the input of an accumulation. (Default: true)"* (defined in `driver/jobs/Frontend.*.so`; default sourced from `_Py_TrueStruct @0x1956d). The critical reverse-engineering finding is that the verbatim string `enable_mixed_precision_accumulation` / `--enable-mixed-precision-accumulation` appears in **exactly two** of the 768 package `.so`: the argparse definition in `Frontend.*.so`, and one consumer — `Penguin.*.so`. It is **absent** from `libwalrus.so`, `libBIR.so`, the tonga/sunda targets, and the tensorizer codegen. No native codegen branch tests this flag by name.

> **QUIRK —** the flag's name and the pass's name share no token. A reimplementer grepping the codegen for `mixed_precision` finds nothing; the flag is rewritten in the Python frontend into the unrelated token `accumulate-on-alu-dtype`, and only *that* token survives into the codegen. Treat the flag and the pass as two ends of a marshalling chain, not one symbol.

### Algorithm — the CLI bool to tensorizer flag

`Penguin.setup_tensorizer_args` (public wrapper `pw @0x251b0`; the generator bodies are `generator..5 @0x11690..0x181d0`) is the arg-list builder feeding the walrus/tensorizer invocation. Its decompiled body reads the bool and, only when true, appends the token as a presence flag (no paired value):

```c
function setup_tensorizer_args(self, options):          // Penguin pw @0x251b0
    args = PyList_New(...)                               // the accumulating arg list
    // ... other flags ...
    v = GetAttrStr(options, "enable_mixed_precision_accumulat")   // L6319 — read the bool
    if PyObject_IsTrue(v):                               // L6328 — flag ON (the default)
        // mstate.kp_u_accumulate_on_alu_dtype is the interned literal (L6410)
        self.append_args(args, "accumulate-on-alu-dtype")          // L6414/L6418
    // when --disable-... → IsTrue is false → token never appended
    return args
```

The literal `accumulate-on-alu-dtype` is interned in `Penguin.*.so`. It is emitted dash-less here and dashed by the `append_args` formatter's `--%s` path — only three `--`-prefixed literals survive verbatim in `Penguin.*.so` (`--no-run-pg-layout-and-tiling`, `--fp32-cast`, `--%s`), and this token is not one of them. The effect is one extra presence flag on the tensorizer command line. This pairs with the documented escape hatch in the overflow diagnostic (see [SBUF-budget guard](#the-sbuf-budget-guard)): *"Remove the --accumulate-on-alu-dtype flag to disable this optimization."*

### Function Map

| Function | Module | Role | Confidence |
|---|---|---|---|
| `setup_tensorizer_args` `pw @0x251b0` | `Penguin.*.so` | Reads `enable_mixed_precision_accumulation`, appends `accumulate-on-alu-dtype` | CERTAIN |
| `append_args` `@0x16670` | `Penguin.*.so` | Formats and appends the `--%s` presence flag | HIGH |

---

## Pass Gating — `accumulate_on_alu_dtype` to `EnforceAluDTAcc`

### Purpose

The tensorizer token sets `target.accumulate_on_alu_dtype` (a target property; the property also exists on Tonga, since Tonga supplies the `alu_dtype`). The property is consumed by the **Sunda** codegen-optimization pipeline, which yields passes; when the property is true it yields one extra pass, `EnforceAluDTAcc`.

### Entry Point

```text
CLI --enable-mixed-precision-accumulation (default True)         driver/jobs/Frontend.*.so
  └─ Penguin.setup_tensorizer_args  (pw @0x251b0)                appends "accumulate-on-alu-dtype"
       └─ target.accumulate_on_alu_dtype = True
            └─ sunda/CodeGenFlow.codegen_optimization            generator5 @0x31520
                 └─ yield build_pass_constructor(EnforceAluDTAcc)   transforms/EnforceAluDTAcc.*.so
```

### Algorithm — the gate

`sunda/CodeGenFlow.codegen_optimization` is a generator whose body is `generator5 @0x31520`. The gate is a single property test:

```c
function codegen_optimization(self):                    // sunda/CodeGenFlow generator5 @0x31520
    // ... yields the legalization / autocast passes first ...
    if self.target.accumulate_on_alu_dtype:             // L1535 getattro, L1546/L1566 IsTrue
        yield self.build_pass_constructor(EnforceAluDTAcc)   // L1571/L1591/L1601
    // ... yields the remaining codegen passes ...
```

The decompiled `generator5_0x31520.c` references `accumulate_on_alu_dtype` once (the predicate), `EnforceAluDTAcc` four times (the symbol load + the yielded constructor), and `build_pass_constructor` (the shared pass-yielding helper used for ~32 passes in this generator).

> **NOTE —** `EnforceAluDTAcc` runs *after* the autocast legalization in this same `codegen_optimization` generator. That ordering is the whole point: autocast (`AutoCastFP32`/`AutoCastInputs`) is free to insert fp32→bf16 downcasts on producers; `EnforceAluDTAcc` then fixes up the ones that feed an accumulate op. The pass is a counter-pass, scheduled to undo a narrowing that an earlier pass may have applied.

### Arch Scope

`EnforceAluDTAcc` is referenced **only** in `sunda/CodeGenFlow.*.so` (count 4); `tonga/CodeGenFlow.*.so` has **zero** references (verified: the tonga sidecar string table contains the token `accumulate_on_alu_dtype` nowhere relevant and no `EnforceAluDTAcc`).

> **GOTCHA —** the *property* `accumulate_on_alu_dtype` lives on Tonga (it is read by `LowerIntrinsics` and `LegalizeTongaMacro` to keep the promoted dtype intact downstream), but the *pass* that consumes it to do the actual promotion is scheduled only in the Sunda (gen2/gen3, Trainium2-class) flow. A reimplementer who schedules the pass on the gen1 Tonga path is wrong; one who omits the property from Tonga's target model breaks the downstream `keep_alu_dtype` bookkeeping. The two are split across arches by design.

### Function Map

| Function | Module | Role | Confidence |
|---|---|---|---|
| `codegen_optimization` `generator5 @0x31520` | `sunda/CodeGenFlow.*.so` | Yields `EnforceAluDTAcc` iff `target.accumulate_on_alu_dtype` | CERTAIN |
| `get_native_alu_dtype` `pw @0x54f80` | `tonga/Tonga.*.so` | Returns the ALU accumulator dtype (fp32) | HIGH |

---

## `EnforceAluDTAcc` — Suppressing the Producer Downcast

### Purpose

The module docstring, verbatim from `.rodata`, is the codegen realization of the CLI help line:

```text
EnforceAluDTAcc - Make sure we dont downcast the producer of accumulate
 instructions, such that we can accumulate on full ALU precision.
```

The class is `EnforceAluDTAcc(TargetLowering)`, a statement-transform walker. `__init__ @0x12290` sets `self.mutator = DTypeMutator(target)` (with the `matchOrMutateDstTy` helper) and `self.alu_dtype = target.get_native_alu_dtype` (= np.float32). For each accumulate op family it visits, it promotes the *producer* of the accumulated operand to `alu_dtype`, chasing back through copy/transpose nodes and giving up (with a diagnostic) only when no producer can be widened — and bailing safely if the promotion would blow the SBUF partition budget.

### Op-Family Scope — six transforms, no matmul

The module exposes exactly six `transform*` methods, every one an ACT/POOL-engine *running accumulation*:

| Transform method | Address | Op family | Confidence |
|---|---|---|---|
| `transformTensorReduceOp` | `@0xe240` | vector reduce (reduce-sum) | CERTAIN |
| `transformPartitionReduceOp` | `@0xd6e0` | partition-axis reduce | CERTAIN |
| `transformNeuronReduceMacro` | (qualname) | reduce macro | HIGH |
| `transformSundaBNStats` | `@0xcb80` | BatchNorm pass 1 (stats) | CERTAIN |
| `transformSundaBNAggr` | `@0xc020` | BatchNorm pass 2 (aggregate) | CERTAIN |
| `transformSundaBNGradient` | `@0xb4c0` | BatchNorm pass 3 (gradient) | CERTAIN |

Every inspected body reduces to a single `self.mutateProducerType(inst.src)` call (verified: `transformSundaBNStats`/`Aggr`/`Gradient` and `transformPartitionReduceOp` each reference `mutateProducerType` once). There is **no** `Matmul`, `TensorContract`, `PSUM`, or `PE` string anywhere in the module (census returns empty).

> **QUIRK —** `transformTensorReduceOp @0xe240` first compares `inst.op` against `np.maximum`/`np.minimum` and skips promotion for min/max reductions. Min and max are exact in *any* dtype — there is no accumulation rounding error to protect — so only SUM-class reductions get the fp32 producer promotion. A reimplementer who promotes every reduce wastes SBUF on min/max ops that gain nothing.

This op-family scope is the deliverable distinction: **the flag touches ALU-engine accumulators (reduce-sum + BatchNorm), never the PE-array matmul.** The matmul accumulator is a separate, hardware-fixed fp32 path covered below.

### Algorithm — the producer chase

```c
function mutateProducerType(self, operand):             // EnforceAluDTAcc @0x10680
    // "don't let the producer of this accumulate operand be narrower than fp32"
    if isinstance(operand.dtype, numbers.Integral_class):   // dtype guard — skip integer
        return
    if operand.dtype == self.alu_dtype:                 // already fp32 → nothing to do
        return
    for def_inst in producers(operand):                 // TensorUtils.store_insts def-chase
        self.castInstructionDst(def_inst)               // promote each producer's dst
    rebuild operand's NeuronAP at the widened dtype     // reinterpret + NeuronAP

function castInstructionDst(self, inst):                // EnforceAluDTAcc @0x15150
    if inst.canMutateDstTy and not self._would_overflow_sb(inst):
        inst.mutateDstTy(self.alu_dtype)                // raise this dst to fp32
        // record keep_alu_dtype bookkeeping (honoured by LowerIntrinsics later)
    elif isinstance(inst, (TensorCopyOp, TransposeOp)):
        self.mutateProducerType(inst.src)              // can't widen here → recurse upstream
    else:
        neuron_internal_assert(...)                     // give up with a diagnostic
```

The recursion through `TensorCopyOp`/`TransposeOp` is the mechanism that lets the pass reach the *real* producer when the immediate def is a no-op data movement that cannot itself be re-typed. `mutateProducerType`'s body references `alu_dtype`, `numbers.Integral`, `castInstructionDst` (twice), `store_insts`, `NeuronAP` (twice), and `reinterpret` (twice); `castInstructionDst`'s body references `canMutateDstTy`, `mutateDstTy`, `_would_overflow_sb`, `keep_alu_dtype`, `TensorCopyOp`, `TransposeOp`, `mutateProducerType`, and `neuron_internal_assert` — all attribute names confirmed in the decompiled sidecars.

### The SBUF-budget guard

`_would_overflow_sb @0x13ff0` is the safety valve. Promoting a producer's dst to fp32 doubles its per-partition byte footprint (bf16→fp32). If that exceeds the SBUF partition size, the promotion is refused and the op is left narrow:

```c
function _would_overflow_sb(self, inst):                // EnforceAluDTAcc @0x13ff0
    // "Check if promoting inst's dst dtype to alu_dtype would overflow SB."
    promoted_bytes = np.dtype(self.alu_dtype).itemsize * inst.dst.partition_size
    limit_bytes    = self.target.statebuf_par_size_in_bytes
    return promoted_bytes > limit_bytes
```

On the overflow path the compiler raises the catalogued error, which lives in `starfish/lib/liblogging.so` (not the Python `ErrorMessages` catalog): *"EnforceAluDTAcc: promoting tensor {tensor_name} from {src_dtype} to {dst_dtype} would overflow SB partition ({promoted_bytes} > {limit_bytes} bytes)"*, with the guidance *"Remove the --accumulate-on-alu-dtype flag to disable this optimization."* The message prefix is literally `EnforceAluDTAcc:`. The module's own `.rodata` carries only the short docstring `"...would overflow SB."`; the parameterized message is in `liblogging.so`. `_would_overflow_sb`'s body references `statebuf_par_size_in_bytes`, `itemsize`, and `partition_size` (all confirmed) — see [SBUF/PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) for `statebuf_par_size_in_bytes` and the partition model.

### Net Codegen Effect

With the flag **on** (default): for every ALU accumulate op, `EnforceAluDTAcc` forces the producer's dst dtype to fp32, so the value entering the ALU accumulator is not pre-rounded to bf16. The downcast is suppressed *at the producer's dst*, not at the accumulate input — and if the fp32 promotion would overflow the SBUF partition, the op is left narrow. With the flag **off** (`--disable-...`): the property is false, the pass is never scheduled, and autocast downcasts those producers to bf16 freely, so accumulation happens on bf16-rounded inputs.

> **NOTE —** the flag protects the *accumulation* precision (the intermediate adds in fp32), not the *output* precision. The accumulated fp32 value is still cast down to the tensor's declared dtype at read-out — see [Cast to New dtype](cast-to-new-dtype.md) for the final fp32→bf16 RNE cast (`bir::CastToNewDType @0x264e70`).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `__init__` | `@0x12290` | Builds `DTypeMutator`; sets `alu_dtype = get_native_alu_dtype` | CERTAIN |
| `mutateProducerType` | `@0x10680` | Core: chase producers of an accumulate operand, promote to fp32 | CERTAIN |
| `castInstructionDst` | `@0x15150` | Per-inst dst promote; recurse through copy/transpose | CERTAIN |
| `_would_overflow_sb` | `@0x13ff0` | SBUF-budget guard; gates the promotion | CERTAIN |
| `transformTensorReduceOp` | `@0xe240` | reduce-sum; skips min/max | CERTAIN |
| `transformPartition/SundaBN*` | `@0xd6e0`/`@0xcb80`/`@0xc020`/`@0xb4c0` | partition-reduce + 3-pass BatchNorm | CERTAIN |

---

## The Matmul PSUM Accumulator Is Hardware-Fixed fp32

### Purpose

This is a **different axis** from `EnforceAluDTAcc`, and it is the headline truth of the page: the PE-array matmul does not go through `EnforceAluDTAcc`, its accumulator (PSUM) is a hardware-fixed fp32 grid, and `--enable-mixed-precision-accumulation` has **zero** effect on it. The proof is that a dedicated legalizer, not a flag, widens every sub-4-byte matmul destination to fp32 before the PE array can write it.

### PSUM is physically fp32 — `widen_psum`

PSUM banks are a grid of 4-byte fp32 accumulator cells. `neuronxcc::backend::PSUMLegalization::run @0x155b110` (`libwalrus.so`, pass order 27) calls `PSUMLegalization::widen_psum @0x15592e0`: any matmul whose declared output dtype is < 4 bytes (e.g. bf16) cannot be the direct PSUM accumulator dst, so `widen_psum` rewrites the PSUM location, its access patterns, and the owning set to a 4-byte (fp32) dtype (`bir::widenDtype`), scaling the free extent by `4/srcBytes`. `birverifier::checkPSUMLegality @0xfefa30` enforces it, and the column size of every PSUM tensor is rounded up to a 4-byte multiple so it occupies a whole number of fp32 cells per bank. All three retain their demangled C++ symbols in `libwalrus.so` (`_ZN9neuronxcc7backend16PSUMLegalization10widen_psumERN3bir6ModuleE` etc.) — these are not stripped.

```c
function widen_psum(M):                                  // libwalrus PSUMLegalization @0x15592e0
    for each PSUM tensor t in M:                         // (op 87 = InstCoreBarrier excluded)
        if dtype_bytes(t.dtype) < 4:                     // sub-4-byte → illegal accumulator dst
            w = bir::widenDtype(t.dtype)                 // widen to next 4-byte type (→ fp32)
            rewrite t.loc, t.access_patterns, owning_set to w
            t.free_extent *= 4 / dtype_bytes(t.dtype)    // preserve byte footprint
```

> **GOTCHA —** there is no flag, no perf-mode, and no user knob that makes a matmul accumulate in bf16 in PSUM. `widen_psum` runs unconditionally in `PSUMLegalization`. A reimplementer modeling "mixed-precision accumulation" as "the matmul accumulator dtype follows the operand dtype" is wrong: the matmul *operand* dtype can be bf16/fp16/fp8, but the *accumulator* is always a 4-byte fp32 cell.

### The matmul dst-dtype byte is set from perf-mode, not from the flag

In the CoreV2 PE bundle (`NEURON_ISA_TPB_S3D3_MM_STRUCT`, 64 B), the control word carries two dtype bytes:

| Field | Offset | Source | Meaning |
|---|---|---|---|
| `IN_DTYPE` | `+0x20` | `inDtypeTranslate(srcAP.dtype)` via BIR→ISA LUT `@0x1df5760` | PE-array input (ifmap/weight) dtype |
| `DST_DTYPE` | `+0x23` | `perfModeToDstDtype(perfMode) @0x1203630` | PSUM/output accumulator dtype byte |

The dst (PSUM) dtype byte is chosen by the matmul **perf mode** (`MatmultPerfMode`: 0 None / 1 DoubleRow / 2 DoubleColumn / 3 DoublePixel / 4 DoubleRowSwInterleave) — `perfModeToDstDtype` returns the perf-mode-derived byte for `perfMode <= 3`. No mixed-precision toggle participates. See [PE Engine](../arch/pe-engine.md) for the systolic array and the perf-mode encoding.

> **NOTE —** `perfModeToDstDtype @0x1203630` is IDA-stripped to `sub_1203630` in `libwalrus.so` — the human-readable name is attested only via the cross-grounding PE-encode analyses, not by a symbol in the binary. Likewise the in-dtype source `@0x1df5760` is a data LUT (20 bytes, BIR dtype `0x00..0x13` → ISA dtype), not a decompiled function. Both addresses are firm; the names are reconstructed.

### `fp32r` is a separate knob

"Reduced-precision accumulation for matmul" is a real thing, but it is **not** this flag. `float32r` (Dtype idx 17, wire-tag 11, align 4) is the TF32-like round/reduced-precision fp32 PE format, gated by `NEURON_ISA_TPB_DTYPE_ALLOW_FP32R` (`core_v3::is_valid_dtype`). It is a legal matmul operand/result dtype, selected via the `--fp32-cast {matmult-fp32r, all-fp32r}` family — a completely separate CLI knob.

> **QUIRK —** three distinct mechanisms all sound like "accumulation precision": (1) `--enable-mixed-precision-accumulation` → fp32 ALU accumulators (reduce/BN); (2) `--fp32-cast …-fp32r` → TF32-like *matmul operand* format; (3) the always-fp32 PSUM cell that no flag touches. Conflating any two is the easiest way to misreport this subsystem.

### START / ACCUMULATE / STOP across the K dimension

A multi-tile matmul accumulates partial sums into one PSUM bank across the contraction (K) dimension via the PE "Calc" flags on `InstMatmultBase` (libBIR externs: `setCalcStart @0x600990`, `setCalcAccu @0x5f24c0`, `setCalcStop @0x5f41e0`):

```text
START      first matmul of the group ZEROES the bank then writes (zero-on-first-write)
ACCUMULATE interior matmuls ADD into the bank
STOP       last matmul DRAINS / reads out the bank
CONTINUE   clears start+stop (middle of a chain)
```

`OverlappedMatmulAccGrp::set_psum_accumulate_flag @0x16e1410` assigns head→`setCalcStart`, tail→`setCalcStop`, interior→`setCalcAccu`; `set_first_matmults @0x157d340` marks the first writer of a PSUM dst as `CalcStart` and the rest as `CalcContinue`. The codegen byte (`generateMatMulAccumulateFlag @0x1428630`) packs bit0=START, bit1=STOP, bit2=ACCUMULATE at the matmul-struct's accu byte. The dtype choice is fixed fp32 at **every** phase: START zeroes an fp32-addressed bank, ACCUMULATE adds in fp32, and only STOP/read-out casts the accumulated fp32 down to the consumer dtype — the matmul-side analogue of the read-out cast. None of these phases is moved by the flag. See [SBUF/PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) for the bank model and [PE Engine](../arch/pe-engine.md) for the Calc-flag encoding.

---

## The XLA `DotAlgorithmAttr` Layer Is Separate

### Purpose

Above the neuron compiler entirely, the StableHLO/MHLO front-door carries its own "accumulate in f32" mechanism — the `DotAlgorithmAttr`. This is the framework-level control a model author sees; it is **not** `--enable-mixed-precision-accumulation`, and the two are independent code paths. Distinguishing them is a deliverable: a reimplementer who maps `accumulation_type=f32` directly onto `accumulate-on-alu-dtype` conflates an upstream attribute with a downstream pass.

### The attribute and its converter

`mlir::mhlo::DotAlgorithmAttr` / `mlir::stablehlo::DotAlgorithmAttr` expose the fields `getLhsPrecisionType`, `getRhsPrecisionType`, `getAccumulationType`, `getLhsComponentCount`, `getRhsComponentCount`, `getNumPrimitiveOperations`, and `getAllowImpreciseAccumulation`. The converter is a 14-case dispatch (the 13 known algorithm presets + `ALG_UNSET`):

```text
xla::ConvertDotAlgorithm(mlir::stablehlo::DotAlgorithmAttr)   @0x2be2680   (hlo-opt)
xla::ConvertDotAlgorithm(mlir::mhlo::DotAlgorithmAttr)        @0x2be2350   (hlo-opt)
    14-way switch (values 2..13 + default ALG_UNSET) → PrecisionConfig_Algorithm
```

> **GOTCHA —** the two `ConvertDotAlgorithm` bodies are at `0x2be2680` (stablehlo) and `0x2be2350` (mhlo) in the cp310 `hlo-opt`, both `nm`-attested `T` symbols. Nearby VAs in the `0x2f0c…` range belong to LLVM's `(anonymous namespace)::X86FastISel::fastEmit_*` blocks — landing there means you have the wrong function.

The known presets (`mlir::hlo::getKnownDotAlgorithm`) are `ALG_DOT_BF16_BF16_F32`, `_BF16_BF16_F32_X3/X6/X9`, `ALG_DOT_TF32_TF32_F32(_X3)`, `ALG_DOT_F32_F32_F32`, `ALG_DOT_F16_F16_F32`, `ALG_DOT_ANY_F8_ANY_F8_F32(_FAST_ACCUM)`, `ALG_DOT_F64_F64_F64`, and `ALG_UNSET`. The raw string tokens `accumulation_type` (`@0x26c6ba`, len 17) and `allow_imprecise_accumulation` (`@0x260dfc`, len 28) appear only in the not-stripped MLIR tools (`hlo-opt`/`hlo2penguin`); `allow_imprecise_accumulation` appears in **no** neuron `.so`.

> **NOTE —** at the StableHLO front-door a dot already carries `accumulationType=f32` + `allowImpreciseAccumulation`. Inside the neuron compiler the equivalent decision is *re-expressed* as the `accumulate-on-alu-dtype` pass (ALU) plus PSUM-fp32 legalization plus the fp32r operand choice (PE). The framework attr and the neuron flag are independent layers that happen to express related intent; neither reads the other.

### Function Map

| Symbol | Address | Module | Confidence |
|---|---|---|---|
| `xla::ConvertDotAlgorithm(stablehlo::DotAlgorithmAttr)` | `@0x2be2680` | `hlo-opt` | CERTAIN (nm) |
| `xla::ConvertDotAlgorithm(mhlo::DotAlgorithmAttr)` | `@0x2be2350` | `hlo-opt` | CERTAIN (nm) |
| `getAccumulationType` / `getAllowImpreciseAccumulation` | (vtable accessors) | `hlo-opt`/`hlo2penguin` | HIGH |

---

## Interactions and the Three Precision Layers

The subsystem is best understood as three independent layers that all touch "accumulation precision," each with its own knob and its own code path:

```text
LAYER 1  XLA / StableHLO     DotAlgorithmAttr.accumulationType / allowImpreciseAccumulation
  (framework)                xla::ConvertDotAlgorithm @0x2be2680 — upstream, hlo-opt only
                             ↓ (lowered to neuron ops; the attr is NOT carried as the neuron flag)
LAYER 2  ALU accumulators    --enable-mixed-precision-accumulation → accumulate-on-alu-dtype
  (reduce / BatchNorm)       → EnforceAluDTAcc promotes producers to fp32   ← THIS flag's domain
LAYER 3  PE / PSUM matmul     PSUM cells hardware-fixed fp32 (widen_psum @0x15592e0); operand
  (matmul)                   precision via --fp32-cast …-fp32r — NOT this flag
```

The two opposing forces inside the neuron compiler are autocast and `EnforceAluDTAcc`. `AutoCastFP32`/`AutoCastInputs` is the fp32→bf16 downcast inserter; its `castTCOperand` downcasts *matmul operands* via `target.getMatmultOperandType` (the PE-array dtype), and `transformAffineLoad`/`transformOffloadedMemCpy` downcast loads/memcpys at the I/O boundary. `EnforceAluDTAcc` is the counter-pass for the ALU side: where an accumulate op's producer would have been narrowed, it re-promotes (or chases the chain through copy/transpose) back to fp32. The domain split is clean — autocast's `castTCOperand` owns the matmul operand dtype, `EnforceAluDTAcc` owns the ALU producer dtype, and the matmul *accumulator* never needs `EnforceAluDTAcc` because PSUM is fp32 by construction. See [AutoCast to fp32](autocast-fp32.md) for the downcast inserter.

> **GOTCHA —** `--accumulate-on-alu-dtype` selects nothing on the matmul path. The matmul PSUM accumulator is fp32 by legalization (`widen_psum`) regardless of the flag, and the only matmul-side reduced-precision choice is the orthogonal `--fp32-cast` fp32r operand knob. The flag's sole domain is the ALU (reduce/BatchNorm) producer dtype.

---

## Evidence Summary

| Claim | Confidence | Evidence |
|---|---|---|
| Flag appears in exactly two `.so` (Frontend def + Penguin consumer), absent from native libs | CERTAIN | string census of 768 `.so` |
| `setup_tensorizer_args` reads the bool, appends `accumulate-on-alu-dtype` | CERTAIN | `pw @0x251b0`; both literals present in `Penguin.*.so` |
| `generator5 @0x31520` gates `EnforceAluDTAcc` on `accumulate_on_alu_dtype` | CERTAIN | gate body: 1× property, 4× symbol, `build_pass_constructor` |
| `EnforceAluDTAcc` scope = 6 reduce/BN transforms, no matmul | CERTAIN | 6 transform bodies present; matmul/PSUM census empty |
| `mutateProducerType`/`castInstructionDst`/`_would_overflow_sb` internals | CERTAIN | attr sequences confirmed in each sidecar |
| min/max reductions skip promotion | CERTAIN | `transformTensorReduceOp` references `maximum`/`minimum` |
| tonga/CodeGenFlow has zero `EnforceAluDTAcc` | CERTAIN | tonga sidecar string table |
| PSUM accumulator hardware-fixed fp32 via `widen_psum @0x15592e0` | CERTAIN | run @0x155b110, widen @0x15592e0, checkPSUMLegality @0xfefa30 |
| matmul dst-dtype byte from `perfModeToDstDtype @0x1203630`, not the flag | CERTAIN | CoreV2 bundle +0x23; LUT @0x1df5760 |
| XLA `ConvertDotAlgorithm @0x2be2680` is a separate layer | CERTAIN | nm-attested symbol + 14-case switch |
| `alu_dtype` == fp32 exactly | HIGH | Tonga `get_native_alu_dtype @0x54f80` references `float32`; exact BSS slot not byte-resolved |
| MX-FP8 ScaledMatmul accumulates in PSUM fp32 like any matmul | MEDIUM | no `EnforceAluDTAcc`/MX cross-reference; PSUM legality applies generally |

Every Cython hop (Penguin → property → sunda gate → pass internals) is grounded in its own decompiled sidecar attribute sequence, so the chain holds end to end. The native PSUM widen and perf-mode addresses come from the walrus and PE strands rather than being re-disassembled here, with matching addresses on both sides. The one link short of certain is the exact `float32` *value* of `alu_dtype`: the method and its `float32` reference are read directly, but the dtype-global BSS slot was not byte-resolved.

---

## Related Components

| Name | Relationship |
|---|---|
| `AutoCastFP32` / `AutoCastInputs` | The opposing fp32→bf16 downcast inserter; `EnforceAluDTAcc` is its counter-pass for ALU producers |
| `PSUMLegalization` (`libwalrus.so`) | Hardware-fixes the matmul accumulator to fp32 via `widen_psum`, independent of this flag |
| `LowerIntrinsics` / `LegalizeTongaMacro` | Read `accumulate_on_alu_dtype` / `keep_alu_dtype` to preserve the promoted fp32 dtype downstream |
| `bir::CastToNewDType @0x264e70` | The read-out fp32→bf16 RNE cast applied after accumulation completes |

## Cross-References

- [Frontend Precision-Flag Marshalling](../frontend/precision-flag-marshalling.md) — where `--enable-mixed-precision-accumulation` is defined and parsed
- [PE Engine](../arch/pe-engine.md) — the systolic matmul array, perf modes, and the Calc start/stop encoding
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — PSUM fp32 cell grid and `statebuf_par_size_in_bytes`
- [AutoCast to fp32](autocast-fp32.md) — the fp32↔bf16 cast policy that `EnforceAluDTAcc` counters
- [Cast to New dtype](cast-to-new-dtype.md) — `bir::CastToNewDType`, the accumulator→output read-out cast
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where the Sunda codegen-optimization flow sits in the pipeline
