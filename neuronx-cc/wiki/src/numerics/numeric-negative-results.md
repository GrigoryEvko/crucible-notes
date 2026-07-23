# Numeric Negative Results — the Features That Do Not Exist

> *All symbols, addresses, and string sweeps on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share this C++ core — names, offsets, and constants are stable, only virtual addresses are cp310-specific). The five binaries swept live under `neuronxcc/starfish/`: device path `lib/libBIR.so` (build-id `a9b1ea38…f206`, 9.5 MB), `lib/libBIRSimulator.so` (build-id `f1c6885f…4ea6`, 36 MB), `lib/libwalrus.so` (build-id `92b4d331…c346`, 65 MB); front-end path `bin/hlo-opt` (build-id `93dd8bd9…`, 229 MB) and `bin/hlo2penguin` (build-id `4ea91b0c…`, 231 MB).*

## Abstract

This page is the **negative space** of Neuron's numeric path: it catalogs the rounding, denormal, and stochastic features that a reimplementer must **not** add, because they do not exist in the shipped compiler — and it proves each absence by an exhaustive search that returned nothing, not by silence. A good reimplementation of `bir::CastToNewDType`, the BIR simulator, or the walrus backend matches not only what these binaries *do* but what they *refuse to do*: there is exactly one floating-point rounding mode, no device flush-to-zero knob, and no path from the device random-number generator to any cast.

The shape of every absence here is the same trap. A token like `kStochasticConvert` or `denormal-fp-math` **is present in the binary** — because `hlo-opt` and `hlo2penguin` statically link the whole of upstream XLA and LLVM, which carry that machinery for the CPU/GPU reference paths. A naive `strings | grep` therefore "finds" stochastic rounding and denormal-flush attributes and concludes the device supports them. It does not. The discriminator is *which binary* and *which namespace*: every stochastic and denormal symbol is in the `xla::`, `mlir::mhlo::`, or `llvm::` namespace inside the two front-end mega-binaries, and **zero** such symbols appear in the three Neuron device libraries (`libBIR` / `libBIRSimulator` / `libwalrus`) that actually produce and simulate silicon code. The front-end *vocabulary* recognizes these ops; the device *codegen* has no handler for them.

The page is organized as four absences and one positive fence. [§1](#1-stochastic-rounding-is-absent) proves stochastic rounding is absent from the device path. [§2](#2-denormal-fp-math-is-llvm-host-only) shows `denormal-fp-math` is an LLVM-host attribute, not a device knob. [§3](#3-fp8fp4-subnormals-are-handled-not-flushed) shows FP8/FP4 subnormals are handled, not flushed. [§4](#4-the-rounding-that-does-exist-rne-and-one-ceil) fences the positive: RNE is the sole FP mode, and the only directed rounding anywhere is a `ceil` for counting DMA descriptors. [§5](#5-the-negative-results-ledger) is the consolidated ledger.

For reimplementation, the contract is the inverse of a normal page — these are the things you must leave **out**:

- **No stochastic rounding.** No RNG/seed argument on any cast; no `StochasticConvert` device visitor; no random tie-break.
- **No device denormal-flush mode.** No FTZ/DAZ/MXCSR knob on the device path; `denormal-fp-math` governs only the host x86 backend's own float math.
- **No FP8/FP4 subnormal flush.** Subnormals are decoded and encoded with explicit branches; the only zero-collapse is IEEE underflow below the smallest subnormal.
- **No directed FP rounding on tensor data.** One `fesetround(FE_TONEAREST)`, RNE everywhere; the sole `ceil` is integer descriptor-count arithmetic.

| | |
|---|---|
| **Device libraries swept** | `libBIR.so`, `libBIRSimulator.so`, `libwalrus.so` |
| **Stochastic string hits, device libs** | **0 / 0 / 0** (`strings -a \| rg -i stochastic`) |
| **`denormal-fp-math` hits, device libs** | **0 / 0 / 0** (`strings \| rg -iw 'denormal-fp-math'`) |
| **`ftz`/`daz`/`mxcsr` hits, device libs** | **0 / 0 / 0** |
| **FP rounding modes on tensor data** | exactly one — RNE (`fesetround(0)`, single call site) |
| **Directed-rounding opcodes in `libBIR`** | 8 × `roundss`: 6 × RNE (`$0xc`) + 2 × `ceil` (`$0xa`, both in `numDescs`) |
| **Configurable FP8 numeric knobs** | exactly two — `disable-fp8-saturation`, `enable-fp8-nan-suppression` |

> **GOTCHA —** the single most common way to get this wrong is to grep one of the two 230 MB front-end binaries, find `kStochasticConvert` / `denormal-fp-math` / `ftz`, and conclude the device supports them. It does not. Those binaries statically link all of XLA and LLVM (with NVPTX and AMDGPU targets compiled in) for the host/GPU reference paths. The device truth is in `libBIR` / `libBIRSimulator` / `libwalrus`, and there the count is zero. Always sweep the device libraries, never the front-end mega-binaries, to characterize device numerics.

---

## 1. Stochastic Rounding Is Absent

### Purpose

Establish, by exhaustive search, that stochastic rounding (a random tie-break injected into a narrowing cast) exists nowhere on the Neuron device path. This is the central negative result: no device artifact mentions, lowers, or simulates a stochastic conversion.

### The exhaustive string sweep

`strings -a | rg -i stochastic` across the five targets:

```text
  libBIR.so          : 0 hits.   <- device IR + the CastToNewDType cast primitive
  libBIRSimulator.so : 0 hits.   <- the numeric reference simulator
  libwalrus.so       : 0 hits.   <- BIR -> machine lowering (walrus backend)
  hlo-opt            : ~20 hits.  ALL xla:: / mlir::mhlo:: (stock XLA front-end)
  hlo2penguin        : same set + one dead oneDNN host symbol (see below)
```

The three Neuron-specific libraries that produce and simulate device code contain **zero** references to stochastic conversion of any kind. Cross-checked against the decompiled symbol corpus: a name search for `stochastic` across all decompiled sidecars returns exactly three files, and **none** are in a device library — two are `xla::cpu::ThunkEmitter::EmitStochasticConvertThunk` (`hlo2penguin` / `snapshot-unpack`, the XLA *CPU* reference path) and one is `xla::…ShapeVerifier…HandleStochasticConvert` (`hlo2penguin`). Per-binary `strings` counts and decompiled-symbol enumeration both return zero for `libBIR`, `libBIRSimulator`, and `libwalrus`.

### Where `kStochasticConvert` actually lives — stock-XLA scaffolding

`hlo-opt` and `hlo2penguin` statically link the full upstream XLA HLO library, so the `kStochasticConvert` opcode and its machinery **are** in the binary. Every symbol is in the `xla::` (or `mlir::mhlo::`) namespace — stock XLA, not Neuron:

```text
xla::HloEvaluator::HandleStochasticConvert(HloInstruction const*)
    — the host (interpreter) literal-level evaluator; consumes a RANDOM operand
      tensor. This is XLA's CPU/reference evaluator, NOT device codegen.
xla::ShapeVerifier::HandleStochasticConvert(HloInstruction*)
xla::ShapeInference::InferStochasticConvertShape(Shape, Shape, PrimitiveType)
xla::HloInstruction::CreateStochasticConvert(Shape, HloInstruction*, HloInstruction*)
xla::DecomposeStochasticConvert(HloComputation*, HloInstruction*)
xla::StochasticConvertDecomposer::Run(HloModule*, ...)        + vtable/typeinfo
xla::primitive_util::{Floating/Integral/Complex}TypeSwitch<... StochasticConvertOp<...>>
source tag: xla/hlo/transforms/expanders/stochastic_convert_decomposer.cc
MLIR side : mhlo.stochastic_convert / mlir::mhlo::StochasticConvertOp
```

The error strings are verbatim upstream XLA (`"Stochastically converting from type %s to type %s is not implemented."`, `"Random numbers for stochastic convert must be unsigned integers…"`, `"StochasticConvert should be decomposed for CPU."`). The opcode is therefore **recognized** by the front-end's HLO parser — `strings hlo-opt | rg -x 'stochastic-convert'` returns one hit (the `HloOpcodeString` table entry), so the HLO text parser will *accept* a `stochastic-convert` op. **Recognition is not lowering.**

### Why recognition never reaches the device

Three independent facts close the gap between "the front-end knows the opcode" and "the device emits a stochastic round":

```c
// (a) NO Neuron-namespace handler.
//     nm -C hlo2penguin | rg stochastic, filtered to drop xla::/mhlo/mlir,
//     leaves exactly TWO non-XLA symbols:
//       _GLOBAL__sub_I__ZN3xla26DecomposeStochasticConvert...   // static-init ctor
//       dnnl::impl::math::stochastic_round_fwd(...)             // oneDNN host, see below
//     There is NO penguin::/hlo2penguin::/neuron:: visitor case for StochasticConvert.
//     Confirmed downstream too: penguin IR has no stochastic opcode
//       (nm -C hlo2penguin | rg penguin | rg -ci stochastic == 0),
//     and BIR has no InstStochastic (its cast ops are
//       InstActivation / InstTensorCopy / CastToNewDType).

// (b) The lowering's DEFAULT action for an unhandled opcode is to ERROR:
//     the string "<op> cannot be lowered." is present in hlo2penguin,
//     alongside "unhandled opcode: " / "Unsupported opcode: ".
//     A kStochasticConvert reaching the lowering visitor aborts; it does
//     NOT silently emit a device stochastic-round.

// (c) The stock expander StochasticConvertDecomposer is linked in
//     (AddPass<StochasticConvertDecomposer> instantiated; pass-name string
//     "stochastic_convert_decomposer"). Upstream this pass REWRITES the op
//     into deterministic arithmetic + a compare against a supplied random
//     tensor BEFORE backend lowering — so if the op ever appears, XLA's own
//     decomposer removes it before it can reach the Neuron emitter.
```

> **QUIRK —** the opcode is in the front-end's HLO *vocabulary* (linked stock XLA) yet has *zero* Neuron device codegen. The truthful shape of the absence is: a `stochastic-convert` op is either decomposed away by the stock expander, or — if it survives — rejected as un-lowerable. A reimplementer who sees the opcode in the HLO enum and adds a device stochastic-round handler is reproducing a feature the original deliberately never built.

### The only stochastic-round code body is dead host code

```text
W dnnl::impl::math::stochastic_round_fwd(float, unsigned, unsigned, dnnl_data_type_t)
                                                      @ 0x49110a0 (hlo2penguin)
```

This is a **weak** symbol from the bundled oneDNN (MKL-DNN) host-CPU math library that `hlo2penguin` links for XLA:CPU reference kernels. It is absent from `libBIRSimulator` (`nm -DC sim | rg dnnl|stochastic` → 0), absent from `libBIR`/`libwalrus`, and is a host x86 float routine — not a NeuronCore/PE/Act-engine kernel. It is a useful **negative control**: if a stochastic-round routine *were* wired into the device path, a corresponding `birsim::`, `bir::`, or `walrus` symbol would exist — and none does.

### The device RNG is real — and disjoint from every cast

The absence of stochastic rounding is *not* the absence of an RNG. BIR and the simulator do contain a hardware random-number facility — but it is a **standalone random-tensor generator** (dropout / weight-init), never a rounding tie-break bolted onto a cast:

```text
libBIR : bir::InstRand, bir::InstRng, bir::isRNGInstruction,
         bir::InstGetRandState::getRngState, bir::InstRandSetState::getSrcSeeds,
         bir::RandomAlgorithmKind2string ("Lcg"),
         bir::RandomDistributionKind2string ({Raw, Normal, Binomial}).
sim    : birsim::InstVisitor::visitInstRand, ::generateXorwowRandom(XorwowState&),
         ::getRandomInt<u8/u16/u32>, ::getRandomFloat, ::checkRandomSeedStateSize.
walrus : birverifier::visitInstRand / visitInstRng; klr::SetRngSeed_(de)s;
         EnableRandomInit (global flag).
```

The disjointness is proven by disassembly, not asserted:

```c
// visitInstRand @0x1d6290 (sim) was disassembled in full. Its call set:
//   MemoryObject ctor, AccessPattern shape queries, the RandomAlgorithmKind
//   red-black-tree lookup, stream/file ops — but NOT _ZN3bir14CastToNewDType...
// There are EXACTLY TWO call sites to CastToNewDType in the entire simulator
//   (objdump xref): one in birsim::MemoryObject::cast_to (0x28db97), one in an
//   internal dtype-conversion helper (0x2ae6e0). NEITHER touches the RNG.
// And the cast's signature carries no random input:
//   bir::CastToNewDType(shared_ptr<vector<uint8>> data,
//                       Dtype src, Dtype dst, bool,
//                       optional<FP8ConversionConfig>)   // D-X06, confirmed in
//                                                        // the libBIR sidecar symbol
```

The RNG produces tensors via a dedicated `InstRand` op; `CastToNewDType` takes no seed/random argument; the two facilities never compose. The `RandomDistributionKind` set is `{Raw, Normal, Binomial}` and the algorithm is `Lcg`/`Xorwow` — dropout/init distributions, not rounding tie-breakers. The `visitInstRand` disassembly contains no cast call, and the cast signature has no seed parameter.

### Verdict (§1)

Stochastic rounding is **absent** from `neuronx-cc`'s device path. `kStochasticConvert` exists only as stock-XLA front-end scaffolding, is decomposed or rejected before device lowering, and has no Neuron handler; the three device libraries carry zero stochastic references; the device RNG is a separate `InstRand` op that feeds no cast; the only `stochastic_round_fwd` body is dead oneDNN host code. Grounding is per-binary `strings`/`nm`/`objdump` sweeps that returned the above and nothing more.

---

## 2. denormal-fp-math Is LLVM-Host-Only

### Purpose

Show that `denormal-fp-math` / `denormal-fp-math-f32` configure the **compiler's own host (x86) float math**, not the Neuron device's denormal handling — and that the device path has no denormal-flush knob at all.

### Where the attributes live

`strings | rg -iw 'denormal-fp-math|denormal-fp-math-f32|denormal_fp_math'`:

```text
  libBIR.so          : 0 hits.
  libBIRSimulator.so : 0 hits.
  libwalrus.so       : 0 hits.
  hlo-opt            : present.
  hlo2penguin        : present (same set).
```

The attribute is confined to the two front-end mega-binaries — exactly where LLVM and XLA are statically linked. None of the three Neuron device libraries reference it. The decompiled-symbol corpus agrees: a `denormal` name search returns zero files in any device library.

### What they are — stock LLVM attributes plus their attributor

The surrounding symbols are the standard LLVM IR function attributes and LLVM's own inter-procedural attributor that propagates them:

```text
llvm::DenormalFPMathState                        (N4llvm19DenormalFPMathStateE)
llvm::AADenormalFPMath                           (the abstract attribute)
(anonymous)::AADenormalFPMathImpl                (the Attributor implementation)
llvm::StateWrapper<DenormalFPMathState, ...>
"invalid value for 'denormal-fp-math' attribute: "       (LLVM attr parser)
"invalid value for 'denormal-fp-math-f32' attribute: "
amdgpu.ignore.denormal.mode / rocdl.ignore_denormal_mode (AMDGPU backend)
Tag_ABI_FP_denormal / ABI_FP_denormal                    (ARM ELF ABI attribute)
DenormalMode vocabulary: "ieee" / "preserve-sign" / "positive-zero" / "dynamic"
```

These attributes control how the LLVM **code generator** treats subnormals in the machine code it emits — whether the *host x86* backend (or, in unused XLA:GPU builds, NVPTX/AMDGPU) sets FTZ/DAZ in `MXCSR` for the compiler's own floating-point work. They are a property of LLVM's backend, not a description of the NeuronCore's arithmetic units.

### The FTZ tokens are LLVM:NVPTX/AMDGPU + XLA:GPU — not Neuron

`strings hlo-opt | rg -i ftz` returns **only** LLVM GPU-backend intrinsic mnemonics and the XLA:GPU flag:

```text
ftz.f, ftz.bf16*, ftz.nan.H*, ftz.relu*, ftz.sat.*      (NVPTX ISA mnemonics)
llvm.amdgcn.fmad.ftz                                     (AMDGPU intrinsic)
llvm.nvvm.add.rm.ftz.f, nvvm.rcp.approx.ftz.f            (NVVM intrinsics)
xla_gpu_ftz  "If true, flush-to-zero semantics are enabled in the code
              generated for GPUs."                       (XLA:GPU debug flag)
nvptx64-nvidia-cuda                                      (the NVPTX triple)
```

These exist because `hlo-opt` statically links the whole of LLVM (with NVPTX and AMDGPU targets compiled in) and the XLA:GPU compiler. They are dead with respect to Neuron — the device target is not NVPTX/AMDGPU, and no Neuron pass reads `xla_gpu_ftz`. `strings | rg -i 'ftz|daz|mxcsr'` over the three device libraries returns **0 hits**.

### The device has no denormal-flush knob — its FP knobs are FP8-only

The only float-behavior global flags on the Neuron device path (in `libwalrus` BSS) concern FP8 saturation and NaN — never denormal flushing:

```c
// libwalrus cl::opt globals (the complete device FP-mode knob inventory):
bool disableFp8Saturation;       // cl::opt "disable-fp8-saturation"
                                 // help   "Disable saturation mode for FP8 types"
bool enableFp8NanSuppression;    // cl::opt "enable-fp8-nan-suppression"
                                 // help   "Suppress NaN values in FP8 types"
// (Plus debugDveNan / register_generator_debug_dve_nan__ — a debug NaN injector,
//  not a numeric mode.)
//
// These two drive the FP8ConversionConfig byte that CastToNewDType consumes
//   (cfg.lo = saturate bit; see §4.4 / the cast-to-new-dtype page).
// There is NO "disable-fp8-subnormal", "flush-fp8-denormal", "ftz", or "daz"
//   cl::opt anywhere in libwalrus / libBIR / libBIRSimulator.
```

> **GOTCHA — the `"+Subnormal"` / `"-Subnormal"` / `"Underflow"` strings in `libwalrus` are not a denormal knob.** They belong to the bundled IBM decNumber 3.61 / ICU decimal-formatting library (`uprv_decNumberIsSubnormal_67` and friends) and have nothing to do with Neuron float behavior. A string sweep will surface them; do not read them as a device flush-to-zero option.

### Verdict (§2)

`denormal-fp-math` / `denormal-fp-math-f32` are LLVM-generic function attributes that configure the **host** (and, in the unused XLA:GPU path, NVPTX/AMDGPU) code generator's own FTZ/DAZ treatment of subnormals — they describe the *compiler's* float math, not the *device's*. The Neuron device path has no denormal-flush attribute or flag; its only float-mode knobs are FP8 saturation and FP8 NaN-suppression. This rests on the where-they-live sweep, the LLVM-attributor symbols, and the FP8-only device-flag inventory.

---

## 3. FP8/FP4 Subnormals Are Handled, Not Flushed

### Purpose

Pin the positive numeric fact that anchors §2's negative one: FP8 and FP4 subnormals are decoded and encoded with explicit branches on **both** the compiler cast primitive and the simulator's own kernels. The only zero-collapse is the IEEE-required underflow of a value below the smallest representable subnormal — that is not a flush-to-zero mode.

### Two FP8 narrowers, one shared source

`neuronx-cc` has two byte-level FP8 narrowers reached at compile/sim time:

```text
(1) libBIR  bir::CastToNewDType — the canonical OCP narrow (D-X06):
      e4m3fn helper @0x4b1f40 (max 448, NO Inf, code 0x7E saturate),
      e5m2   helper @0x4b2160 (max 57344, HAS Inf, code 0x7B saturate).
    Imported (undefined `U`) by BOTH libBIRSimulator and libwalrus — they call
    the SAME primitive (nm -DC sim/walrus | rg CastToNewDType => `U`).
(2) libBIRSimulator's OWN inlined micro-kernels (NX-124), after birsim::rsqrt_on_pool:
      E3M4 enc @0x2a36e0, E4M3-legacy(240-max, with Inf) enc @0x2a38d0, E5M2 enc @0x2a3cf0;
      decoders @0x2a3f80 / 0x2a4050 / 0x2a41e0.
    These cover the legacy float8 variants and the e8m0/MX scale path; the
    canonical OCP e4m3fn/e5m2 narrow in the MX path is DELEGATED back to (1).
```

Both share the same upstream `__FILE__` string baked into all three binaries — `…/neuronxcc/support/dtype_impl/float_converter.cpp` — i.e. the compiler and the simulator narrow FP8 with the *same* code logic.

### e4m3fn / e5m2 subnormals reach 2⁻⁹ / 2⁻¹⁶

`CastToNewDType`'s narrow encoders take an **explicit subnormal branch** — they do not flush small magnitudes to zero at the min-normal boundary:

```c
// e4m3fn (helper 0x4b1f40): min-normal exponent = -6.
//   For unbiased exp in [-9,-7] the SUBNORMAL path @0x4b20e0 shifts the
//   mantissa right by (-6 - e), applies RNE, sets exp_field = 0.
//   => subnormals representable down to 2^-9 (smallest positive subnormal,
//      2^-9 ~= 0.00195). Only for unbiased exp < -9 does the value underflow
//      to signed zero (sign << 7) — the IEEE-correct round-to-zero of a value
//      below the smallest subnormal, NOT a flush of a representable subnormal.

// e5m2 (helper 0x4b2160): min-normal exponent = -14.
//   Subnormal path @0x4b2310 (exp in [-16,-15]) shift + RNE, exp_field = 0
//   => subnormals down to 2^-16. Below that, underflow to signed zero.
```

The widen side is symmetric: the fp8/fp16 → fp32 decode renormalizes subnormals explicitly (fp16 subnormal renorm via `subss` with `2^-14 = 0x38800000`), so subnormals survive the round-trip rather than being zeroed on decode.

### The simulator's own FP8 kernels handle subnormals too

Each of the sim's three inlined decoders (E3M4 / E4M3 / E5M2) has a dedicated **DENORMAL** branch:

```c
// FP8 decode (fp8 -> fp32 widen), per variant:
//   exp==0, mant!=0 -> DENORMAL: normalize loop — shift mant left, decrement
//   the start exponent until the hidden bit is set, then assemble.
//   The hidden-bit test is at the format-specific position:
//     E3M4 bit 0x10  (decode @0x2a3f80)
//     E4M3 bit 0x8   (decode @0x2a4050)
//     E5M2 bit 0x4   (decode @0x2a41e0)
//   i.e. exp==0 is RECONSTRUCTED into fp32, never treated as a flat zero.
```

The three inlined **encoders** share the `float_converter.cpp` narrow whose NORMAL/SUBNORMAL arm applies the RNE round-bias to *both* normal and subnormal magnitudes — subnormals are rounded into the subnormal grid, not flushed.

### FP4 e2m1 handles its single subnormal grid point

```c
// The sim's LOCAL FP4 e2m1 encoder (sub_2A3530) has an explicit DENORMAL
//   branch @0x2a35e0: exp<0 normalizes the subnormal mantissa with the SAME RNE.
//   Grid = {0, +-0.5, +-1, +-1.5, +-2, +-3, +-4, +-6}.
//   The +-0.5 subnormal (0x3F000000) is a first-class output; only values that
//   round below 0.25 collapse to +-0 — IEEE round-to-zero below the smallest
//   grid step, NOT an FTZ of the 0.5 subnormal.
```


### No FTZ/DAZ on the device FP8/FP4 path

There is no flush-to-zero / denormals-are-zero knob anywhere on the FP8/FP4 device path:

- No `ftz`/`daz`/`mxcsr` string in `libBIR`/`libBIRSimulator`/`libwalrus` ([§2](#the-ftz-tokens-are-llvmnvptxamdgpu--xlagpu--not-neuron)).
- The only device FP8 `cl::opt`s are `disable-fp8-saturation` and `enable-fp8-nan-suppression` — neither flushes subnormals.
- The `FP8ConversionConfig` is a 2-byte struct `{byte0 = saturate, byte1 = nan-suppress}` — there is **no** third byte/bit for denormal flushing.
- The hardware MX-quantize bundle has only a saturate bit (`bundle+0x01` bit0) and **no** round-mode or FTZ field.

The only "flush"-like behavior is the mathematically-required underflow of a value smaller than the format's smallest subnormal to a signed zero — present in every IEEE-correct narrowing, and *not* a denormal-flush mode.

> **NOTE —** there is one *simulator-fidelity* divergence worth flagging, which is a saturation matter, not a subnormal one: the sim's FP8 *reduce* stubs (the scatter-add path in `cast_copy_or_accumulate`) hardcode `xor ecx,ecx` → `FP8Config == 0`, so an overflowing reduce result emits Inf rather than the saturate-max-finite the device default (`fp8_sis = 1`) would produce. This makes the sim *more pessimistic* than the device on FP8 reduce overflow; it does not touch subnormal handling, and it is a simulator gap rather than a device behavior.

### Verdict (§3)

FP8 (and FP4) subnormals are **handled, not flushed**, on both the compiler cast primitive (`CastToNewDType`: e4m3fn → 2⁻⁹, e5m2 → 2⁻¹⁶) and the `libBIRSimulator` FP8/FP4 paths (explicit DENORMAL decode/encode branches; the sim delegates the OCP fp8 narrow back to the same `CastToNewDType`). No FTZ/DAZ exists on the device FP8 path; the only zero-collapse is IEEE underflow below the smallest subnormal. This rests on the cast disassembly, the byte-exact sim kernels, and the shared `float_converter.cpp` source string.

---

## 4. The Rounding That Does Exist: RNE and One ceil

### Purpose

A negative-results page must also fence the positive, so the absence of stochastic and directed FP rounding is stated against a *fully enumerated* alternative. Here is the complete set of rounding behaviors that **are** present.

### FP rounding mode = RNE, and only RNE

```c
// CastToNewDType (0x264e70-0x265fb0, ~4.4 KB) pins the C rounding mode once:
//   fesetround(0) == FE_TONEAREST, at 0x2650e2:  xor edi,edi ; call fesetround@plt
// libBIR contains EXACTLY ONE fesetround call site (objdump grep == 1) — that one.
//   There is NO fesetround(<other>) anywhere in libBIR; the mode is set to RNE
//   once, between the two cast stages, and never changed.
//
// Every FP-narrowing encoder (e4m3fn, e5m2, E3M4, E4M3-legacy, e2m1 FP4, bf16,
//   fp16) does explicit guard + round + sticky round-to-nearest-EVEN in integer
//   arithmetic: round-bias = 1 << (MANT_SHIFT - 1); ties broken toward even via
//   the odd-bit test. (Confirmed bit-exact in the sim's bf16/fp16/fp8 kernels,
//   D-F04 §2.1-2.3.)
//
// The hardware MX-quantize / activation bundles emit NO round-mode operand
//   (D-J20): rounding is HARD RNE in silicon for both the matmul MX-quantize
//   and the activation narrow. No round-mode byte is encoded.
```

No round-toward-zero, round-toward-+∞, round-toward-−∞, or stochastic FP-narrowing mode exists. RNE is the single, hard-wired mode. This page's claim is independently corroborated by the [`bir::CastToNewDType`](cast-to-new-dtype.md) page, which pins the same `fesetround(0)` at `0x2650e2` with no other call site.

### The one float→int conversion — IEEE-standard, not a directed mode

```c
// float -> integer (CastToNewDType float->int leg, D-X06 §4.2):
x = roundss(f, 0xc)      // imm 0xc = round-to-nearest-EVEN, suppress-exc
i = cvttss2si(x)         // cvtt = truncate-toward-zero the now-INTEGRAL x
// + a saturating cmov clamp to the target int range.
//
// This is the standard "round-to-nearest, then the value is already integral so
// the truncating convert is exact" idiom: the round step is RNE; cvttss2si's
// toward-zero behavior only matters for the already-rounded integral value.
// Net float->int rounding is round-to-nearest-EVEN — NOT a directed mode.
// (0xc roundss imm = _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC.)
// No floor/ceil/trunc rounding mode is exposed for float->int.
```


### The only directed-rounding opcode in libBIR — ceil for descriptor counting

A full scan of `libBIR`'s `.text` for `roundss $imm` finds exactly **8 sites**, with only two distinct immediates:

```c
// 6 x roundss $0xc  (round-to-nearest-even, suppress-exc)  -- the FP narrows above
// 2 x roundss $0xa  (round-toward-+infinity = CEIL, suppress-exc)
//
// BOTH ceil sites are inside bir::numDescs(PhysicalAccessPattern const&)
//   (callsites 0x3171b1, 0x317227). The idiom:
//     cvtsi2ss ; cvtsi2ss ; divss ; roundss $0xa(ceil) ; mulss ; cvttss2si -> int
//   i.e. an integer CEILING DIVISION counting how many DMA descriptors a tensor
//   AccessPattern needs. This is GEOMETRY/SIZING arithmetic on integer counts,
//   NOT a rounding mode applied to tensor element values.
```

> **QUIRK —** directed rounding *does* exist in the device libraries — but only as a `ceil` for counting DMA descriptors, never on a data-narrowing path. The same pattern holds across all three device libs: `libBIRSimulator` has 1× and `libwalrus` has 4× `roundss $0xa` (ceil), and **zero** `roundss $0xc` — because the sim/walrus FP narrows implement RNE via explicit integer guard+round+sticky manipulation (not the hardware `roundss`), while their only hardware `roundss` uses are ceil for descriptor/tiling geometry. `libwalrus` has no `fesetround` at all (objdump grep == 0). Net: across all three device libraries the **only** directed rounding is ceil-for-integer-counts; every tensor-data narrow is RNE. A reimplementer must not read the `ceil` as evidence of a directed FP-narrowing mode — it is integer tiling math.

> **NOTE on the `numDescs` two-VA-frame artifact —** the exported symbol `bir::numDescs` resolves to a 6-byte PLT thunk (`0x1791c0: jmp cs:off_9130C8`); the real body and its two `ceil` sites live at the implementation VA (the `0x316eb0` frame, callsites `0x3171b1` / `0x317227`). Reading the export-stub VA shows only the jump, not the arithmetic — the per-function disasm of the body is where the `divss`/`roundss $0xa`/`mulss` idiom appears.

### Saturation and NaN — the only other configurable numeric behaviors

The FP8 narrow has exactly two configurable behaviors, both via the 2-byte `FP8ConversionConfig` (default `0x0001` at `libBIR .data 0x917848` → saturate **ON**):

| Byte | Field | Effect |
|---|---|---|
| `byte0` | `fp8_sis` (saturate) | on → overflow clamps to max-finite (e4m3fn `0x7E`=448, e5m2 `0x7B`=57344); off → overflow emits NaN (e4m3) / Inf (e5m2) |
| `byte1` | `fp8_sns` (nan-suppress) | on → NaN input → signed zero; off → canonical NaN |

These select **overflow/NaN encoding**, not the rounding direction of in-range values, which is always RNE.

> **GOTCHA —** the two FP8 knobs are the *entire* configurable numeric surface of the device cast. There is no rounding-mode knob, no denormal knob, and no stochastic knob to discover. A reimplementer who exposes a "rounding mode" parameter on the cast is adding a degree of freedom the original does not have — every in-range narrow is RNE, unconditionally.

---

## 5. The Negative-Results Ledger

Each row is an absence (or a tightly-bounded positive) with the search that grounds it. A single counter-hit — one stochastic device symbol, one device denormal knob, one second `fesetround` — would falsify the tagged claim; the searches below returned none.

| Claim | Grounding | Tag |
|---|---|---|
| Stochastic rounding ABSENT from Neuron device path | §1 — `strings`/`nm`/decompiled sweep, all 3 device libs = 0 | CERTAIN |
| `kStochasticConvert` present only as stock-XLA front-end scaffold | §1 — `nm -C`, all `xla::`/`mhlo::` namespace | CERTAIN |
| `kStochasticConvert` NOT wired to a Neuron lowering (no handler) | §1 — `nm` filter leaves no `penguin::`/`neuron::` visitor | CERTAIN |
| `kStochasticConvert` decomposed / rejected before device lowering | §1 — `StochasticConvertDecomposer` AddPass + `"<op> cannot be lowered."` | HIGH |
| No RNG/seed feeds any cast; RNG is a separate `InstRand` op | §1 — `visitInstRand` disasm has no cast call; cast sig has no seed | CERTAIN |
| Only `stochastic_round_fwd` body = dead oneDNN host CPU symbol | §1 — weak `W`; absent from sim/`libBIR`/`libwalrus` | CERTAIN |
| `denormal-fp-math`/`-f32` = LLVM-generic host attrs, not device | §2 — where-they-live sweep + LLVM attributor symbols | CERTAIN |
| `ftz`/`daz` tokens = LLVM NVPTX/AMDGPU + XLA:GPU, dead for Neuron | §2 — only NVVM/AMDGCN intrinsics + `xla_gpu_ftz` | CERTAIN |
| Neuron device has no denormal-flush knob; FP8 knobs only | §2 — `cl::opt` flag inventory = {disable-fp8-saturation, enable-fp8-nan-suppression} | CERTAIN |
| FP8 e4m3fn subnormals handled down to 2⁻⁹ | §3 — subnormal path `0x4b20e0` shift+RNE | CERTAIN |
| FP8 e5m2 subnormals handled down to 2⁻¹⁶ | §3 — subnormal path `0x4b2310` shift+RNE | CERTAIN |
| Sim FP8 kernels have explicit DENORMAL decode/encode branches | §3 — sim hidden-bit tests `0x10`/`0x8`/`0x4` | CERTAIN |
| FP4 e2m1 subnormal ±0.5 reachable; denormal branch w/ RNE | §3 — `sub_2A3530` denorm branch `0x2a35e0` | CERTAIN |
| No FTZ/DAZ on device FP8/FP4 path; only IEEE underflow-to-0 | §3 — `ftz`/`daz`/`mxcsr` = 0; config 2-byte, no denorm bit | CERTAIN |
| FP rounding = RNE only (`fesetround(0)`; guard+round+sticky) | §4 — single `fesetround` call site `0x2650e2` | CERTAIN |
| float→int = RNE (`roundss $0xc`) + truncating convert + sat clamp | §4 — cast disassembly | CERTAIN |
| HW MX/activation bundle has no round-mode field — HARD RNE | §4 — the MX/activation bundle layout | CERTAIN |
| Only directed rounding = `ceil` for DMA-descriptor counts (`numDescs`) | §4 — `roundss $0xa` scan, both sites in `numDescs` | CERTAIN |
| No round-toward-zero/+∞/−∞ on any tensor-data narrow | §4 — `roundss` immediate scan, all 3 device libs | CERTAIN |
| Only configurable FP8 numeric knobs = saturate + nan-suppress | §4 — `FP8ConversionConfig` 2-byte struct | CERTAIN |
| Stochastic-string count = 0 in all 3 device libs × cp310/11/12 | §1 — `grep -c` = 0 (cp311/312 size-identical) | CERTAIN |

> **NOTE on cross-wheel stability —** all binaries above are cp310; the cp311 and cp312 images are size-identical, so names, offsets, and constants are stable across the three Python builds — only virtual addresses are cp310-specific. The negative results are grounded in exhaustive-search-found-nothing statements (the explicit per-binary hit counts), not silence.

---

## Related Components

| Component | Relationship |
|---|---|
| `bir::CastToNewDType` (`libBIR`) | the single cast/saturate engine; the `fesetround(0)` RNE pin and the FP8 subnormal branches live here |
| `birsim::cast_copy_or_accumulate` (`libBIRSimulator`) | the per-dtype reduce/cast kernel; its FP8 reduce stubs are the source of the sim-only saturation-fidelity gap |
| `bir::numDescs` (`libBIR`) | the *only* directed-rounding (`ceil`) site — DMA-descriptor counting, not numerics |
| `bir::InstRand` / `birsim::generateXorwowRandom` | the device RNG — a standalone dropout/init op, architecturally disjoint from every cast |
| `xla::StochasticConvertDecomposer` (front-end) | stock-XLA expander that removes `kStochasticConvert` before device lowering |

## Cross-References

- [bir::CastToNewDType — Cast / Saturate Engine](cast-to-new-dtype.md) — the cast primitive; independently pins the same `fesetround(0)` RNE-only behavior and the FP8 saturate default
- [Neuron dtype Catalog & x4-Packing](dtype-catalog.md) — the OCP-MXFP formats whose subnormals §3 proves are handled
- [BIR Simulator — MX & Matmul](../bir/sim-matmul-mx.md) — the `QuantizeMx` kernel and E8M0 block-scale path referenced for the hard-RNE silicon claim
- [MX Microscaling](mx-microscaling.md) — §9.8, the E8M0 block-scale and MX-quantize numerics
