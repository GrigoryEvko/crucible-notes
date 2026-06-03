# LLVM/MLIR Manifest

> *All addresses, offsets, and version strings on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (release `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes on disk). Other wheels will differ.*

## Abstract

`libtpu.so` is not a thin runtime shim — it is a whole compiler statically linked into one shared object. Roughly **a third of its code-and-rodata bytes are upstream LLVM and MLIR**, vendored from a single Google-internal LLVM monorepo snapshot and dragged in along with an out-of-tree `TPU` LLVM backend, the upstream MLIR dialect zoo, and a stack of TPU-specific MLIR dialects (`tpu`, `llo`, `sparse_core`, `mosaic_sc`, `xtile`). This page establishes the authoritative *what's-compiled-in* manifest: the exact LLVM/MLIR version evidence, which LLVM core/CodeGen/MC components are present, which MLIR dialects and infrastructure are linked, and the headline versions of the third-party libraries that ride alongside the toolchain.

The version frame is the central complication. Google's build system rewrites the upstream `LLVM_VERSION_MAJOR` to a rolling sentinel, so the binary carries no `LLVM 23.0.0` banner. What it *does* carry — pinned to byte offsets in `.rodata` — are two monorepo commit SHAs (`clang` and `LLVM` heads), a `g3_____-trunk` revision tag, the `9999.0.0` sentinel, and a build epoch. From those the upstream major is bounded to **LLVM 23-dev (tip-of-trunk, ~April 2026)** by the release-branch calendar, not read directly. Every component claim below is anchored to a defined symbol or a `.rodata` literal recovered from the binary; the version *window* is the one inference, and it is marked as such.

> **NOTE —** the binary is **un-stripped** in this wheel: `.symtab` carries 1,233,710 entries (`sh_size 0x1c3cc50 ÷ 24`; the `1232970` `readelf` prints in the section's `Inf` column is the first-global index, not the entry count) with full Itanium mangling. FLIRT pattern-matching is therefore moot — every component below is confirmed by an exact demangled symbol or rodata string, not a fuzzy signature. A production strip removes `.symtab`/`.strtab` but leaves all the code bytes identified here.

For reimplementation, the contract is:

- **The version pin** — which LLVM/MLIR commit the toolchain tracks, and how to bound the upstream major from a sentinel-masked build.
- **The LLVM component set** — Core IR, both ISel paths, MachineCodeGen, the MC layer, the *seven* registered target backends, the analysis/transform pass pipeline, and the two AOT MLGO advisor models.
- **The MLIR component set** — Core IR, the three-tier dialect registry, pass infra, bytecode reader/writer, the conversion framework, and the LLVM-IR translation path (but **no** ExecutionEngine/JIT).
- **The embedded-library versions** — the key third-party pins (Abseil, protobuf, Eigen, DNNL, ICU, libc++, …) that the toolchain is compiled against, deferring the exhaustive tree to the [Embedded-Library Atlas](embedded-library-atlas.md).

| | |
|---|---|
| **LLVM/MLIR version** | LLVM 23-dev (trunk), monorepo commit `8918319853fbdf9e6f6cb69e96848f913a22bc31` |
| **clang version literal** | `g3 clang version 9999.0.0 (a70419505471bd8240ef3451dcdd541f8676477c)` @ `0xaf591d4` |
| **LLVM version literal** | `LLVM version g3_____-trunk 8918319853fbdf…` @ `0xb1fa070` |
| **llvm-mc literal** | `llvm-mc (based on LLVM g3_____-trunk 8918319853fbdf…)` @ `0xa12d48e` |
| **Build epoch** | `Built on Apr 13 2026 14:17:21 (1776115041)` @ `0x84a1d90` |
| **Code/data model** | Large code model (`.lrodata` 113.3 MB, `.lbss` present), x86-64, clang/lld |
| **MLIR provenance** | Same monorepo commit as LLVM — no separate MLIR version |
| **Registered target backends** | X86, AArch64, ARM, AMDGPU (+R600), PowerPC, NVPTX, **TPU** (seven, by `LLVMInitialize*TargetInfo`) |
| **LLVM + MLIR footprint** | ~84.1 MB LLVM + ~72.2 MB MLIR = ~156 MB of code+rodata symbol bytes (~37% of T+R) |

---

## LLVM/MLIR Version Pin

### Purpose

Fix the exact toolchain version so every `llvm::`/`mlir::` address on the forensics pages is unambiguous, and explain *why* the upstream major cannot be read directly from a string.

### Version Evidence

Three version literals sit in `.rodata`, each recoverable with `strings -t x`. They are the primary anchors for the whole toolchain:

```text
0xa12d48e  llvm-mc (based on LLVM g3_____-trunk 8918319853fbdf9e6f6cb69e96848f913a22bc31)
0xaf591d4  …PIC LevelCode ModelLarge Data Threshold…g3      clang version 9999.0.0 (a70419505471bd8240ef3451dcdd541f8676477c)
0xb1fa070  LLVM version g3_____-trunk 8918319853fbdf9e6f6cb69e96848f913a22bc31
```

The clang and LLVM SHAs are **two heads of the same monorepo** (`clang`'s last-touched commit vs LLVM core's last-touched commit at the snapshot point), not two repositories. MLIR ships from that same monorepo at that same commit — there is no independent MLIR version number to find. The `clang` literal is co-located (same string-pool run) with the `Code Model` / `Large Data Threshold` driver flags, which is itself the evidence that this binary was built with the **large code model** (see the `.lrodata`/`.lbss` sections in [ELF Anatomy](elf-anatomy.md)).

| Field | Value | Anchor |
|---|---|---|
| LLVM trunk SHA | `8918319853fbdf9e6f6cb69e96848f913a22bc31` | `.rodata` `0xb1fa070` |
| clang trunk SHA | `a70419505471bd8240ef3451dcdd541f8676477c` | `.rodata` `0xaf591d4` |
| Revision tag | `g3_____-trunk` (Google rolling-trunk sync) | both literals |
| clang version string | `9999.0.0` (Google sentinel for tip-of-trunk) | clang literal |
| Build toolchain | `Bazel, release r4rca-2026.04.04-1 (mainline @894239244)` @ `0x84a12a0` | `.rodata` |
| Build epoch (UTC) | `2026-04-13 21:17:21` (epoch 1776115041) | `.rodata` `0x84a1d90` |

### Bounding the Upstream Major

The `9999.0.0` sentinel and `g3_____-trunk` tag deliberately mask `LLVM_VERSION_MAJOR`; no `LLVM 23.0.0`-style literal survives. The major is pinned from the **release-branch calendar**, not a string:

```text
Build epoch          : 2026-04-13  (RC cut from piper @894239244; Bazel r4rca-2026.04.04-1)
google3 sync lag     : days-to-low-weeks behind upstream main
=> upstream main window: ~late-March to mid-April 2026

LLVM release cadence (6-month major; branch ~6-8 wk before .1.0):
  21.x branched ~Jul 2025      (main was 21.0.0git in 2025)
  22.x branched ~late-Jan/Feb 2026, 22.1.0 ~Mar 2026
  23.x branches  ~Jul/Aug 2026

Once 22.x branches (Jan/Feb 2026), main's LLVM_VERSION_MAJOR bumps to 23.
By April 2026 upstream main reports 23.0.0git.
=> embedded LLVM = LLVM 23-dev (trunk), post-22.x-branch, pre-23.x-branch.
```

> **GOTCHA —** do **not** treat `9999.0.0` as a real version and assume an exhaustively-stable release ABI. This is tip-of-trunk: it can contain post-22-branch IR/pass changes that no tagged release ships, and it predates the 23.0.0 release. A reimplementer targeting "LLVM 22" or "LLVM 23" tagged sources will see API drift; the only exact reference is the monorepo commit `8918319853fbdf…`.

> **NOTE —** the major-version *window* (23-dev) is **HIGH** confidence — bounded by the build epoch and the deterministic branch/version-bump mechanic. It is the single inferred datum on this page; everything else is a direct symbol/string hit. The remaining gap is the exact upstream commit *date* for `8918319853fbdf…`, which is not embedded (only the SHA), so converting "23-dev window" to "23-dev as of YYYY-MM-DD" requires an external monorepo lookup.

---

## LLVM Core

### Purpose

Enumerate the LLVM components statically linked in — this is a full code-generation toolchain, not a stub. The headline is that the *entire* SelectionDAG + MachineCodeGen + MC stack is present, several upstream target backends are linked alongside the out-of-tree `TPU` target, and two MLGO advisor models are AOT-baked into rodata.

### Component Manifest

Every row is a defined-symbol hit (`nm -C libtpu.so | rg …`). Confidence is `CERTAIN` where a concrete class symbol is present.

| LLVM component | Present | Primary evidence (defined symbol) | Confidence |
|---|---|---|---|
| Core IR | YES | `llvm::Module`, `llvm::Function`, `llvm::BasicBlock`, `llvm::Instruction`, `llvm::LLVMContext` | CERTAIN |
| Bitcode reader/writer | YES | `llvm::BitcodeReader`, `llvm::parseBitcodeFile`, `llvm::WriteBitcodeToFile` | CERTAIN |
| SelectionDAG ISel | YES | `llvm::SelectionDAG`, `llvm::SelectionDAGISel`, `llvm::TargetLowering` | CERTAIN |
| GlobalISel infra | YES (linked) | `llvm::InstructionSelect`, `llvm::LegalizerInfo`, `llvm::RegisterBankInfo` | CERTAIN |
| MachineCodeGen | YES | `llvm::MachineFunction`, `llvm::MachineInstr`, `llvm::LiveIntervals` | CERTAIN |
| MC layer | YES | `llvm::MCStreamer`, `llvm::MCInst`, `llvm::MCCodeEmitter` (+ `TPUMCCodeEmitter`) | CERTAIN |
| TPU target backend | YES | `llvm::TPUTargetMachine` + the `llvm::TPU*` family (below) | CERTAIN |
| Analysis passes | YES | `llvm::ScalarEvolution`, `llvm::PassBuilder` (NewPM) | CERTAIN |
| MLGO advisor models | YES (2) | `RegAllocEvictModel`, `InlinerSizeModel` (AOT, see below) | CERTAIN |
| Embedded LLVM bitcode | YES | `kEigenUnaryLlIr_constant_buffer_contents` @ `0xaf58000` | CERTAIN |
| MCJIT / ORC | YES | `llvm::MCJIT`, `llvm::orc::*` (LLVM ExecutionEngine — note: distinct from `mlir::ExecutionEngine`, which is absent); XLA CPU backend JITs `llvm::Module`s | CERTAIN |

> **QUIRK —** **both** ISel infrastructures are linked. GlobalISel (`InstructionSelect`/`LegalizerInfo`/`RegisterBankInfo`) is present, but the TPU path's MC-emitter (`TPUMCCodeEmitter::getBinaryCodeForInstr` @ `0x13c74da0`, a 5,667-case switch over `InstBits`) is downstream of `MachineInstr`, consistent with a SelectionDAG-primary backend. Whether the TPU target also has a GlobalISel path for some opcodes is not resolved from the symbol surface alone — it needs a disassembly of the pass-pipeline constructor.

### The Linked Target Backends

The inventory of `LLVMInitialize*TargetInfo` registrations proves this binary registers **seven** LLVM target backends, not just the custom TPU one:

```text
LLVMInitializeX86TargetInfo        ── host backend (the binary runs on x86-64)
LLVMInitializeAArch64TargetInfo
LLVMInitializeARMTargetInfo
LLVMInitializeAMDGPUTargetInfo     ── (+ R600MCCodeEmitter, the legacy AMDGPU sub-target)
LLVMInitializePowerPCTargetInfo
LLVMInitializeNVPTXTargetInfo
LLVMInitializeTPUTargetInfo        ── the out-of-tree Google backend (this page's headline)
```

The set of `LLVMInitialize*Target` (codegen) initializers is the same seven, and each carries an instantiated `*TargetMachine` class (`llvm::X86TargetMachine`, `llvm::AArch64TargetMachine`, `llvm::ARMBaseTargetMachine`, `llvm::AMDGPUTargetMachine`/`GCNTargetMachine`/`R600TargetMachine`, `llvm::PPCTargetMachine`, `llvm::NVPTXTargetMachine`, `llvm::TPUTargetMachine`). Each in-tree target carries its own TableGen `InstBits` encoder table — e.g. `AMDGPUMCCodeEmitter::…::InstBits` @ `0x29d8910`, `AArch64MCCodeEmitter::…::InstBits` @ `0x397e980`, `PPCMCCodeEmitter::…::InstBits` @ `0x3c0d770`. They are `registerAllTargets()` fallout (the same build-system over-linking that drags in the unused MLIR target dialects, below).

> **QUIRK —** Hexagon source TUs are *partially* linked (≈140 `_GLOBAL__sub_I_Hexagon*.cpp` static-init thunks and a handful of `llvm::HexagonSubtarget::*Mutation` symbols survive), but Hexagon is **not** a usable backend: there is **no** `LLVMInitializeHexagon*` initializer, **no** `llvm::HexagonTargetMachine` vtable or constructor, and **no** `HexagonMCCodeEmitter`. It is dead static-init residue from over-linking, not a registered target — do not count it among the seven.

> **CORRECTION (LLVM-MANIFEST-01) —** an earlier reading held that the out-of-tree `TPU` target was effectively the *only* LLVM backend compiled in ("upstream LLVM has no TPU target" → "so only TPU is here"). The symbol surface shows **X86, AArch64, ARM, AMDGPU+R600, PowerPC, and NVPTX are all statically linked and registered** alongside TPU — seven registered backends. The TPU target is the only *custom* backend, but it is one of seven. `X86` is present (the host backend); the question "is an X86 backend compiled in?" is answered **yes** by `llvm::X86TargetMachine`/`llvm::X86Subtarget`/`llvm::X86InstrInfo` and `createX86MCCodeEmitter`. (A separate Hexagon static-init residue is present but is **not** a registered backend; see the QUIRK above.)

### The TPU Target Backend

The out-of-tree `TPU` target is the single most distinctive LLVM component — a complete `llvm::Target` named "TPU" that does not exist upstream. Its TableGen tables are located and sized at fixed addresses:

| Table / function | Address | Role | Confidence |
|---|---|---|---|
| `TPUMCCodeEmitter::…::InstBits` | `0x3366d90` | Per-opcode instruction encoding bits (TensorCore) | CERTAIN |
| `…::InstBits_BarnaCorePxcHwMode` | `0x33931f0` | BarnaCore (Pufferfish PXC) HwMode encoding variant | CERTAIN |
| `llvm::TPUDescs` | `0x33bf650` | Per-instr `MCInstrDesc` table | CERTAIN |
| `llvm::TPUInstrNameData` | `0x33f2be0` | Instr mnemonic string pool | CERTAIN |
| `llvm::TPUFeatureKV` | `0x21934550` | `SubtargetFeatureKV` key/value (16 features; 1,152 B / 72 B-stride) | CERTAIN |
| `llvm::TPUSubTypeKV` | `0x21934ca0` | Subtype/CPU key/value (9 CPU variants; 1,008 B / 112 B-stride) | CERTAIN |
| `TPUMCCodeEmitter::getBinaryCodeForInstr` | `0x13c74da0` | 5,667-case encoder switch (selects `InstBits` per HwMode) | CERTAIN |

Five silicon subtarget classes (plus a generated `llvm::TPUGenMCSubtarget`) cover the TensorCore generations across the HAL families (the abbreviations map to TPU codenames; the SparseCore sequencer split is documented in the [tpu dialect](../compiler/tpu-dialect-and-ops.md) and lowering pages):

```text
llvm::TPUSubtarget      ── base (jellyfish/dragonfish/pufferfish TensorCore)
llvm::TPUBcSubtarget    ── BarnaCore PXC
llvm::TPUVfcSubtarget   ── vfc = viperfish (v5)
llvm::TPUGlcSubtarget   ── glc = ghostlite (gen 4; mktg "Trillium")
llvm::TPUGfcSubtarget   ── gfc = 6acc60406 / TPU7x (gen 5; mktg "Ironwood")
```

The per-generation scheduling models are present as nine `*SchedModelSchedClasses` tables, split by sequencer type (SCS / TAC / TEC) and generation (VF / GL / GF):

```text
BarnaCorePFSchedModelSchedClasses                       ── Pufferfish BarnaCore
SparseCoreScs{GF,GL,VF}SchedModelSchedClasses           ── SCS sequencer (3 gens)
SparseCoreTac{GL,VF}SchedModelSchedClasses              ── TAC sequencer (2 gens — NO GF)
SparseCoreTec{GF,GL,VF}SchedModelSchedClasses           ── TEC sequencer (3 gens)
```

> **QUIRK —** there is **no** `SparseCoreTacGF` table. The GF generation (`gfc` = 6acc60406 / TPU7x, mktg "Ironwood") ships an SCS and a TEC sequencer but **no TAC** sequencer, so its TAC scheduling model is absent by design — not missing by extraction error. A reimplementer iterating generations × sequencer-types and assuming a full 3×3 grid will allocate a table the hardware does not have.

### MLGO Advisor Models

Two ML-guided-optimization models are AOT-compiled (via `tfcompile`) directly into native code plus constant-buffer rodata — there is **no** TF runtime or interpreter for them. They are the LLVM "release-mode" MLGO models baked in through the upstream `LLVM_RAEVICT_MODEL_PATH` / `LLVM_INLINER_MODEL_PATH` build mechanism:

| Model | Consumer | Evidence | Confidence |
|---|---|---|---|
| `RegAllocEvictModel` | LLVM greedy RA `MLRegAllocEvictAdvisor` | `…RegAllocEvictModel…` symbol family | CERTAIN |
| `InlinerSizeModel` | LLVM inliner `MLInlineAdvisor` | `_llvm__InlinerSizeModel_*_fusion_` constant-buffer symbols | CERTAIN |

These are **LLVM-backend** MLGO advisors, not an XLA learned cost model. The `InlinerSizeModel` constant buffers are even named per fusion shape (`dot_add_fusion`, `iota_reduce_fusion`, `compare_convert_fusion`, …), confirming they are the trained inliner-for-size weights.

### Embedded LLVM Bitcode

The binary ships at least one precompiled LLVM bitcode module as a rodata blob, which is direct proof the bitcode reader is live:

```text
0x0af58000  kEigenUnaryLlIr_constant_buffer_contents   (LLVM bitcode Module; vectorised tanh + FMA/min/max intrinsics)
            llvm_ir::kEigenUnaryLlIr                    (guard/storage symbol @ .bss 0x224ee910)
```

It is linked into JIT'd CPU `llvm::Module`s at compile time via `llvm::parseBitcodeFile`, so Eigen-lowered `tanh` resolves to a vector loop rather than a `libm` call. This blob is **LLVM IR bitcode** (LLVM-23-dev format), distinct from the MLIR bytecode surface (below) — both readers are compiled in.

---

## MLIR Dialects

### Purpose

Enumerate the MLIR components. MLIR ships from the *same* monorepo commit as LLVM core, so it is the LLVM-23-dev in-tree MLIR, layered with TPU-specific out-of-tree dialects authored against that API.

### Core IR and Infrastructure

| MLIR component | Present | Primary evidence | Confidence |
|---|---|---|---|
| Core IR | YES | `mlir::MLIRContext`, `mlir::OpBuilder`, `mlir::Operation` (+ `Block`/`Region`/`Value`/`Type`) | CERTAIN |
| Dialect registry | YES | 67 concrete `*Dialect` classes (unique `vtable for …Dialect`, excl. base `mlir::Dialect`) | CERTAIN |
| Pass infrastructure | YES | `mlir::PassManager`, `mlir::Pass` | CERTAIN |
| Bytecode reader/writer | YES | `mlir::BytecodeReader`, `mlir::writeBytecodeToFile` | CERTAIN |
| Conversion framework | YES | `mlir::ConversionTarget`, `mlir::TypeConverter`, `mlir::RewritePatternSet` | CERTAIN |
| LLVM-IR translation | YES | `mlir::translateModuleToLLVMIR` | CERTAIN |
| ExecutionEngine / JIT | **NO** | no `mlir::ExecutionEngine` symbol | CERTAIN |

> **NOTE —** the bytecode reader/writer and the LLVM-IR translation path are both present, but `mlir::ExecutionEngine` is **absent**. MLIR is used purely as an AOT translate path: the SparseCore lowering chain runs `translateModuleToLLVMIR` and hands the resulting `llvm::Module` to the LLVM TPU backend ([LowerToSparseCoreLlvm](../compiler/lower-to-sparsecore-llvm.md)). There is no MLIR JIT, which resolves a standing question about whether an MLIR ExecutionEngine is linked: it is not.

> **NOTE —** the directly-verifiable count of *instantiated* dialect classes is **67** — one `vtable for …Dialect` symbol per concrete dialect, deduped, excluding the base `mlir::Dialect` (counting the base gives 68). Looser regexes over the raw symbol surface inflate this: matching every bare `…Dialect` token double-counts namespaced vs. unqualified spellings (e.g. `xla::xtile::XTileDialect` and `XTileDialect`) and pulls in constructors/typeinfo, yielding figures north of 200. The vtable count is the conservative, one-class-one-hit number. The [Embedded-Library Atlas](embedded-library-atlas.md) owns the full dialect tree.

### The Three-Tier Dialect Set

The dialects fall into three groups, and the distinction matters for a reimplementer: most of Group A'/B is **linked but unused** by the TPU lowering chain — it is `registerAllDialects()` over-linking, the MLIR analogue of the seven LLVM target backends.

| Group | Dialects (verified samples) | Status on TPU path |
|---|---|---|
| A — upstream MLIR core | `func`, `arith`, `scf`, `cf`, `vector`, `memref`, `tensor`, `linalg`, `affine`, `math`, `complex`, `index` | Registered; some used in early lowering |
| A' — LLVM/target dialects | `llvm`, `gpu`, `spirv` (and the X86/NVVM/ROCDL/AMDGPU family) | Linked, unused (over-linking fallout) |
| B — HLO / input | `stablehlo`, `chlo`, `mhlo`, `vhlo` | Input + early-lowering |
| C — TPU-specific (Google) | `tpu`, `llo`, `sparse_core` (`ScDialect`, `LlvmTpuDialect`), `mosaic_sc`, `xtile` | The TPU compilation chain |

The Group-C dialects are confirmed by their concrete `Dialect` classes:

```text
mlir::tpu::TPUDialect              ── the TPU dialect (tpu/Mosaic ops)
mlir::llo::LLODialect              ── low-level ops (LLO)
mlir::sparse_core::ScDialect       ── SparseCore ops
mlir::sparse_core::LlvmTpuDialect  ── SparseCore -> LLVM-TPU bridge dialect
mlir::mosaic_sc::MosaicSCDialect   ── Mosaic-SparseCore
xla::xtile::XTileDialect           ── XLA XTile tiling dialect
```

> **QUIRK —** the presence of `mlir::gpu::GPUDialect`, `mlir::spirv::SPIRVDialect`, and the X86/NVVM target dialects in a *TPU* plugin is not evidence of a GPU code path. It is `registerAllDialects()` pulling the whole upstream dialect set into the static link. The same over-linking explains the AArch64/ARM/AMDGPU/PowerPC LLVM backends. A reimplementer should treat Group A' as dead weight on the TPU path, not as a capability.

---

## Embedded Third-Party Libraries

### Purpose

Pin the headline versions of the third-party libraries the toolchain is compiled against. This page gives the LLVM/MLIR-adjacent manifest with the *determinable* versions; the [Embedded-Library Atlas](embedded-library-atlas.md) owns the exhaustive ~60-library tree with byte accounting.

### Version Manifest

Versions are pinned three ways: a versioned **path literal** (decisive), a **version-tagged namespace** (decisive), or a **feature-floor** (a symbol that first appeared in a known release — pins a floor, not the exact version).

| Library | Version | Version evidence | Confidence |
|---|---|---|---|
| LLVM | 23-dev (trunk) | `LLVM version g3_____-trunk 8918319853fbdf…` @ `0xb1fa070` | HIGH (window) |
| MLIR | = LLVM (same commit) | same monorepo commit; no separate version | HIGH |
| libc++ | = LLVM commit | `std::__u::` ABI tag (Google libc++ inline-namespace build) | CERTAIN (provenance), HIGH (point) |
| Intel oneDNN | **v3.3** | path literal `third_party/intel_dnnl/v3_3/` | CERTAIN |
| ICU | **icu_78** | versioned namespace `icu_78::` | CERTAIN |
| Protobuf | v32+ (EDITION_2024 production) | edition enums `EDITION_2023`, `EDITION_2024`, `EDITION_2026`, `EDITION_UNSTABLE` (no `EDITION_2025`) | HIGH |
| Abseil | >= LTS 20240722 (floor) | `absl::AnyInvocable`, `absl::log_internal::SetTimeZone`, `absl::CordBuilder`, `absl::StatusOr` | HIGH (floor) |
| Eigen | 3.4.x branch | `Eigen::bfloat16` (modern), `Eigen::half`; no `EIGEN_*_VERSION` literal survives | MEDIUM |
| gRPC | google3-tip (>= 1.66-dev) | `chaotic_good` transport, `filter_fusion` (trunk-only) | MEDIUM |
| tcmalloc | 2024+ (rseq per-CPU) | `google_malloc` ELF section, `__rseq_cs`, rseq cmpxchg family | HIGH (floor) |

> **NOTE —** the **path-literal** and **versioned-namespace** pins (oneDNN `v3_3`, ICU `icu_78`) are the only `CERTAIN` exact versions; they survive as literal strings the build embeds. Everything else is a **floor** — the binary exhibits features added in a known release but no `*_VERSION_MAJOR` macro literal survives, so the exact point release cannot be read from this binary alone. The Atlas page documents the per-library reproduction recipe for tightening each floor.

> **NOTE —** the `std::__u::` inline-namespace ABI tag on every libc++ symbol is a Google build customization (the same monorepo as LLVM/MLIR). Because the `llvm::`/`mlir::` template instantiations are templated on `std::__u::` types throughout, the libc++ build is inseparable from the LLVM/MLIR version pin — they are one toolchain.

### Google-Specific Customizations

No behavioural patch to LLVM/MLIR *core* algorithms is visible in the symbol surface. The Google customizations over upstream are all standard vendoring patterns, not core forks:

1. **Version sentinel** — `9999.0.0` / `g3_____-trunk` are build-identity overrides of the upstream version macros (masks `LLVM_VERSION_MAJOR`; not a behavioural change).
2. **Out-of-tree TPU target** — the entire `llvm::TPU*` backend (5 silicon subtargets, 6,166 MC opcodes per `InitMCInstrInfo`, the `InstBits` encoder) is Google-private; upstream has no TPU target.
3. **AOT MLGO models** — `RegAllocEvictModel` + `InlinerSizeModel` baked in through the upstream release-model mechanism (build-time, not a source patch).
4. **libc++ `__u` ABI tag** — a Google libc++ build customization.
5. **TPU MLIR dialects** — `tpu`/`llo`/`sparse_core`/`mosaic_sc`/`xtile` authored against the in-tree MLIR API; out-of-tree dialects layered on stock MLIR, not core modifications.

---

## Byte Footprint

LLVM and MLIR together are the largest single category of code in the binary. The figures below are symbol-bucketed (summed `nm -S` sizes by namespace); the underlying ELF section sizes that bound them are confirmed directly: `.text` = `0x12bdb484` (314,422,404 B), `.lrodata` = `0x6c0e7d0` (113,305,552 B), `.rodata` = `0x39eaf28` (60,731,176 B).

| Bucket | Combined bytes | % of code+rodata symbol bytes | Confidence |
|---|---|---|---|
| LLVM (`llvm::` + TableGen tables) | ~84.1 MB | ~19.8% | HIGH |
| MLIR (`mlir::` + dialect templates) | ~72.2 MB | ~17.0% | HIGH |
| MLGO models (RegAllocEvict + InlinerSize) | ~0.59 MB | ~0.14% | HIGH |
| `kEigenUnaryLlIr` bitcode blob | 16,384 B | ~0.004% | CERTAIN |
| **LLVM + MLIR + MLGO + bitcode** | **~156.9 MB** | **~36.9%** | HIGH |

> **NOTE —** MLIR's `.rodata` share is tiny relative to its `.text` because MLIR is template-heavy code with few large constant tables; LLVM's `.rodata` is large because the TPU TableGen tables (`InstBits` 181,344 B, `TPUDescs`, `TPUInstrNameData`) and the in-tree-target `InstBits` tables live in rodata/lrodata. These are *symbol-bucketed* sizes — they exclude anonymous-namespace TU-local residue, an estimated 70-80% of which belongs to LLVM/MLIR/XLA, so the true footprint is modestly higher than 156.9 MB.

---

## Cross-References

- [Embedded-Library Atlas](embedded-library-atlas.md) — owns the exhaustive ~60-library static-link tree and per-library byte accounting; this page is the LLVM/MLIR-centric slice.
- [Binary Forensics Overview](overview.md) — the top of the forensics tier; build identity and the analysis methodology.
- [ELF Anatomy](elf-anatomy.md) — section sizes, the large-code-model `.lrodata`/`.lbss`, and the build-id note backing every offset here.
- [Custom Sections](custom-sections.md) — `google_malloc`, `protodesc_cold`, `__rseq_cs` and the other compiler-emitted sections that bound the library buckets.
- [The libtpu.so / sdk.so Two-Binary Split](two-binary-split.md) — which of the two shipped objects carries the LLVM/MLIR toolchain.
- [The TPU Compiler](../compiler/overview.md) — the compilation pipeline that drives the LLVM/MLIR components catalogued here.
- [LlvmTpu Intrinsic Catalog](../compiler/llvmtpu-intrinsic-catalog.md) — the intrinsics the LLVM TPU backend lowers.
- [The tpu MLIR Dialect: Ops and the Op-Model Contract](../compiler/tpu-dialect-and-ops.md) — the Group-C `tpu` dialect in depth.
- [LowerToSparseCoreLlvm](../compiler/lower-to-sparsecore-llvm.md) — the `translateModuleToLLVMIR` AOT path that hands MLIR to the LLVM TPU backend.
