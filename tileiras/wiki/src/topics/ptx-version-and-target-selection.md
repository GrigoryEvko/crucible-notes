# PTX Version and Target Selection

## Abstract

Every PTX module tileiras emits begins with the same three-directive header:
a `.version`, a `.target`, and an `.address_size`. The three values are not
independent. They are the projection of one decision — pick a subtarget —
made by stitching together the user's `--gpu-name` flag, the
`nv_tileaa.compute_capability` module attribute, the NVPTX subtarget feature
bitset, and the `TargetMachine` debug toggle. Picking a target also picks
an instruction surface: `wgmma`, `tcgen05`, and the block-scaled MMA family
are gated by the `a` / `f` suffix on the `.target` directive, and a kernel
that requires any of them cannot run on a vanilla `sm_NN` variant.

This page is the cross-cutting story. It explains which knobs choose the
PTX version, which choose the `.target` line, what the `a` / `f` suffixes
mean architecturally, and where the resulting subtarget object is consumed
during codegen.

## The Three-Directive Header

The AsmPrinter emits the header exactly once per PTX module, drawing
every field from the active `NvptxSubtarget` plus the `TargetMachine`
debug flag. A representative `sm_90a` build with debug info enabled
produces:

```ptx
.version 8.4
.target sm_90a, debug
.address_size 64
```

| Directive | Source | Choice |
|---|---|---|
| `.version` | Subtarget `+ptxNN` feature bit | Highest PTX ISA the chosen subtarget supports for the requested features. |
| `.target` | Subtarget CPU plus optional debug flag | `sm_NN[a\|f][, debug]`. |
| `.address_size` | Subtarget pointer width | Always `64` in this build. |

The header is one of the few PTX surfaces where the AsmPrinter does
zero independent thinking. Every value already exists on the
`NvptxSubtarget` by the time the printer runs; the header step is a
projection, not a decision. See [Module Header Directives](../codegen/asm-printer-monster-and-windows.md#module-header-directives)
for the exact printing routine.

## The `.version` Directive

The PTX ISA version is the version of the PTX grammar the emitted
module conforms to. PTX is a forward-compatible ISA: a `ptxas` shipped
with CUDA 13.1 can ingest any earlier PTX version, but it can only
ingest later versions up to the maximum its build understands.

Tileiras picks the PTX version through a subtarget feature bit, not
through a free-form integer. The thirty bits `ptx32`..`ptx88` in the
[NVPTX feature index table](../codegen/nvptx-subtarget-and-feature-matrix.md#the-81-feature-indices)
each act as a discrete version selector. The driver layer (cicc or the
hosting tool) sets exactly one of them through `-mattr=+ptxNN`; the
NVPTX subtarget parses the numeric tail of the feature name into
`PTXVersionTimesTen` and the AsmPrinter divides by ten to print
`.version major.minor`.

| PTX Version | Minimum for |
|---|---|
| 6.0 | sm_70 (Volta WMMA, basic mbarrier) |
| 7.0 | sm_80 (Ampere baseline, cp.async) |
| 7.5 | sm_86 / sm_87 |
| 7.8 | sm_89 (FP8 `mma.sync` on Ada) |
| 8.0 | sm_90 baseline (Hopper) |
| 8.2 | `wgmma.mma_async`, TMA bulk copies on sm_90a |
| 8.4 | Extended `cp.async.bulk`, mbarrier additions |
| 8.6 | `tcgen05.*` family on sm_100a / sm_103a |
| 8.7 | Consumer-Blackwell `mma.sync.aligned.*.block_scale` on sm_120a |
| 8.8 | The build cap for this drop |

The table is what the toolchain enforces, not what the language
mandates. NVIDIA's PTX manual states the minimum version per
instruction, and ptxas refuses any module that uses an instruction
without declaring at least the matching `.version`. Tileiras's job is
to declare a version *high enough* for every instruction it emits,
without picking a version higher than the downstream ptxas supports.

The CPU rows in the [40 CPU rows table](../codegen/nvptx-subtarget-and-feature-matrix.md#the-40-cpu-rows)
carry no implied PTX bit; the PTX-version selector is orthogonal to
the CPU selection. A reimplementation that bundles `+ptx84` into the
implication mask of `sm_90a` breaks the orthogonality and forces
downgrades. Pick the highest version compatible with the chosen
feature set, set the corresponding `+ptxNN` flag, and let the CPU row
contribute only its self-bit.

## The `.target` Directive

The `.target` directive identifies the streaming-multiprocessor
generation the module is being compiled for. It is the single most
consequential field in the entire PTX file — it selects the
instruction lattice, the warp model, the shared-memory and
register-file sizes, and the set of architecture-conditional
operations available.

The grammar accepted by `ptxas` is:

```
.target sm_<digits>[<suffix>][, debug][, map_f64_to_f32]
```

The suffix is one of three states:

- **No suffix** — `sm_90`, `sm_100`, `sm_120`. The vanilla
  architecture. Only baseline ISA instructions are available, but
  the module is forward-compatible: a binary built for `sm_90` runs
  on every `sm_>=90` device, including future ones.
- **`a` suffix** — `sm_90a`, `sm_100a`, `sm_120a`. Architecture-specific.
  The module unlocks the full instruction set of that exact
  architecture, including any architecture-conditional families
  documented per generation. It is *not* forward-compatible: a
  binary built for `sm_90a` runs only on Hopper, never on Blackwell.
- **`f` suffix** — `sm_100f`, `sm_103f`. Family-conditional. The
  module unlocks architecture-conditional instructions but promises
  forward compatibility within the family of variants that share the
  same major SM number. Builds for `sm_100f` run on every Blackwell
  datacenter variant (`sm_100`, `sm_101`, `sm_103` cores) but not on
  consumer Blackwell or future generations.

The complete grid of who-implies-what lives in [the 40 CPU rows table](../codegen/nvptx-subtarget-and-feature-matrix.md#the-40-cpu-rows).
Each `a` or `f` variant is a separate CPU row in the subtarget table,
with its own feature bit and its own implication mask. The `tmem`
feature (index 80) is the prime example: it is implied by every
datacenter `a` / `f` Blackwell row and by no base or consumer row.

## Architecture-Conditional Instructions

Several instruction families are reachable *only* through a target
suffix. The compiler's lowering is built around a feature predicate;
plain SM rows leave the predicate false, suffix rows toggle it true.

| Family | Required suffix | Predicate gate |
|---|---|---|
| `wgmma.mma_async.sync.aligned` (Hopper warp-group MMA) | `sm_90a` | `HasSM90a` |
| `wgmma.fence`, `wgmma.commit_group`, `wgmma.wait_group` | `sm_90a` | `HasSM90a` |
| TMA `cp.async.bulk.tensor` im2col modes | `sm_90a` | `HasSM90a` |
| `setmaxnreg.inc`, `setmaxnreg.dec` | `sm_90a` | `HasSM90a` |
| `tcgen05.alloc`, `tcgen05.dealloc`, `tcgen05.relinquish` | `sm_100a` / `sm_100f` / `sm_103a` / `sm_103f` | `HasTMem` (index 80) |
| `tcgen05.mma`, `tcgen05.mma.sp`, `tcgen05.mma.ws` | `sm_100a` / `sm_100f` / `sm_103a` / `sm_103f` | `HasTMem` |
| `tcgen05.ld`, `tcgen05.st`, `tcgen05.cp` | `sm_100a` / `sm_100f` / `sm_103a` / `sm_103f` | `HasTMem` |
| `mma.sync.aligned.*.block_scale` (MXFP8, MXFP4, NVFP4) | `sm_120a` / `sm_121a` | `HasSM120a` / `HasSM121a` |
| 2-CTA and 4-CTA `tcgen05.mma.cta_group::N` modes | `sm_100a` / `sm_103a` | `HasTMem` plus shape verifier |

When the user picks `--gpu-name=sm_90` (without the `a`), tileiras
cannot emit `wgmma`. There are two well-defined outcomes:

1. The frontend has already specialized its lowering to avoid
   producing `tt.dot` ops that would lower to `wgmma`. The pipeline
   completes and the emitted PTX uses `mma.sync` fallbacks.
2. The frontend has emitted a tensor-core op that *requires* `wgmma`.
   The selector finds no legal MachineInstr and fails with an
   "unsupported operation for target" diagnostic. The compile stops.

There is no third path. Tileiras does not silently degrade a `wgmma`
kernel into a `mma.sync` loop nest; that admission belongs upstream,
at the dialect-lowering or tile-scheduler level. The same rule
applies one tier up: a kernel that requires `tcgen05.mma` cannot run
on `sm_100` (base), only on `sm_100a` or `sm_100f`. Consumer
Blackwell (`sm_120`/`sm_121`) substitutes block-scaled `mma.sync`
instead and is described in [SM120 / SM121 emission](../codegen/per-sm-emission-templates.md#sm120-sm121).

## The Compute-Capability Attribute

Inside the compiler, the source of truth for the target choice is the
`nv_tileaa.compute_capability` module attribute. Each lowering and
codegen pass consults this attribute through the
[attribute-attached lifecycle](attribute-system-and-lowering.md#lifecycle-of-a-kernel-attribute):
the driver writes it from `--gpu-name`, the
[`ConvertTileFuncToLLVM`](attribute-system-and-lowering.md#per-stage-attribute-rules)
stage propagates it, and the `AttachNVVMTarget` stage folds it into a
single `#nvvm.target` attribute that the NVPTX backend reads when
constructing the `NvptxTargetMachine`.

The attribute carries the numeric SM major-times-ten value (90 for
sm_90, 90 for sm_90a — the variant suffix is recorded in a sibling
`target_spec` field, not in the integer). Downstream rewrites that
need to distinguish `sm_90` from `sm_90a` consult both fields, never
just the integer.

Dropping the attribute before `AttachNVVMTarget` runs is a known
source of silent miscompiles: the `#nvvm.target` attribute falls back
to a default chip, the NVVM IR verifier accepts it because the chip
string is well-formed, and the cubin compiles for the wrong SM. The
[intentional drops list](attribute-system-and-lowering.md#intentional-drops-and-silent-miscompiles)
documents this failure mode explicitly.

## Target Machine Construction

The NVPTX backend wraps the choices above into an
`NvptxTargetMachine` constructed from a triple, a CPU string, and a
feature string:

```c
NvptxTargetMachine *tm = NVPTXTarget::createTargetMachine(
    /*triple=*/   "nvptx64-nvidia-cuda",
    /*cpu=*/      "sm_90a",
    /*features=*/ "+ptx84",
    /*options=*/  TargetOptions{...},
    /*reloc=*/    Reloc::Default,
    /*code-model=*/ CodeModel::Small,
    /*opt-level=*/ CodeGenOpt::Aggressive);
```

The triple is fixed: `nvptx64-nvidia-cuda` for every supported target.
The 32-bit variant `nvptx-nvidia-cuda` is not produced by this build;
the `.address_size` directive is always 64.

The CPU string is the literal `sm_NN[a|f]` form, taken verbatim from
the `compute_capability` + `target_spec` pair. `std::lower_bound`
against the sorted CPU table resolves it to a row, and the row's
implication mask is ORed into the runtime feature bitset.

The feature string is a comma-separated list of `+feature_name`
tokens. The PTX-version bit (`+ptx84` in the example) is the most
common entry; other tokens like `+fma-level=2`, `+prec-divf32=3`,
`+prec-sqrtf32=1` appear when the driver propagates the corresponding
numerical-precision flags. The string is *additive over* the CPU
row's mask — the row contributes its self-bit and any implied bits,
the string adds whatever else the driver wants.

A worked example for the canonical CUDA 13.1 Hopper build:

```text
--gpu-name=sm_90a
  → compute_capability = 90, target_spec = "a"
  → CPU = "sm_90a"
  → CPU row 39 implication mask: {bit 60 = sm_90a}
  → driver propagates --ptx-version=8.4
  → feature string = "+ptx84"
  → runtime feature_bits[0] |= (1ULL << 60)
  → runtime feature_bits[0] |= (1ULL << 28)  // ptx84 = index 28
  → SMVersionTimesTen = 90
  → PTXVersionTimesTen = 84
```

The AsmPrinter divides `PTXVersionTimesTen` by ten and prints
`.version 8.4`. It reads the CPU string out of the subtarget and
prints `.target sm_90a`. The whole chain is two field reads and a
print.

## Address Size

`.address_size` is always 64 in this build. The full set of CPU rows
listed in the subtarget table starts at `sm_20` (Fermi), and Fermi
era cards were the last NVIDIA generation to ever use 32-bit
addressing. Even the legacy CPU rows in this build emit
`.address_size 64`; the 32-bit code path was removed when the build
was cut, and no flag re-enables it.

This is one of the rare PTX header fields with no decision logic at
all: the printer emits the literal `.address_size 64` after
`.target`, full stop.

## The Debug Suffix

A second comma-separated token on the `.target` line declares the
presence of DWARF debug information:

```ptx
.target sm_90a, debug
```

The token is added when the `TargetMachine` debug level is non-zero.
The driver sets that whenever the user passes `--device-debug` (or
its `-g` alias); the
[option validator rule](../driver/cli-options.md#validation-algorithm)
requires `-O0` in that case, because full device debug disables
several code-motion and block-merge transforms that an optimized
build relies on.

A non-debug build with only `--lineinfo` set does *not* add the
`debug` token. The line-info path emits source-location records as
PTX `.loc` directives inside function bodies; the `.target` header
remains the un-suffixed form. The two paths are independent axes,
not a single switch.

The fourth token on the `.target` line, `map_f64_to_f32`, exists in
the ptxas grammar but is never emitted by this compiler. It belongs
to a legacy fp64 emulation path the modern stack does not select.

## Cross-Architecture Builds

Tileiras compiles for one target at a time. A single invocation
produces one PTX file for one `(--gpu-name, --ptx-version)` pair,
with no `-arch=...` list, no `compute_NN` / `sm_NN` pairing, and no
fatbin section table.

Multi-architecture builds are managed entirely at the nvcc level.
nvcc invokes tileiras once per target architecture in the user's
`-gencode` list, collects the resulting PTX or cubin files, and
hands them to `fatbinary` and `nvlink` for packaging. Each tileiras
invocation is independent of the others. See
[the nvcc-tileiras handoff diagram](../boundaries/nvcc-13-1-position.md#path-b-tileiras-new-mlir-bytecode)
for how the driver-level orchestration assembles a fatbin from
multiple single-target tileiras runs.

The implication for an integrator: there is no API on the tileiras
side to ask "give me a JIT-able PTX for any device this fatbin
covers". The granularity is one-target-per-invocation, and the
fatbin-aware logic lives strictly above the tileiras boundary.

## Choosing Between `sm_NN`, `sm_NNa`, and `sm_NNf`

The choice is driven by three orthogonal questions, and the answers
combine into the suffix decision.

1. **Does the kernel need arch-conditional instructions on this
   generation?** If the lowered IR contains `wgmma`,
   `tcgen05.mma`, `mma.sync.aligned.*.block_scale`, TMA im2col, or
   `setmaxnreg`, the answer is yes; an `a` or `f` suffix is
   mandatory.
2. **Will the binary be deployed across multiple variants in the
   same SM family?** If yes, prefer the `f` suffix on the
   generations where it exists. `sm_100f` runs on every Blackwell
   datacenter chip; `sm_100a` runs only on the specific GB100 /
   B100 die. Consumer Blackwell (`sm_120`, `sm_121`) has no `f`
   variant because the family does not contain `tcgen05` —
   architectural specialization is purely the `a` form.
3. **Is forward compatibility with future generations required?**
   Only the bare `sm_NN` form is forward-compatible across major
   generations. Choose it when the kernel can do without arch-cond
   instructions and must run on hardware released after the build.

Practical guidance: choose the *narrowest* suffix that still admits
every instruction the kernel emits. If the kernel uses `wgmma`,
pick `sm_90a`. If it uses both `tcgen05.mma` and is deployed on a
mixed Blackwell datacenter fleet (GB100, GB200, B100), pick
`sm_100f`. If neither is needed, pick the bare `sm_NN` for maximum
forward compatibility. Fatbin construction at the nvcc level is the
correct mechanism for multi-architecture deployment, not a single
broad-target tileiras invocation.

The compute-capability attribute selection in the frontend is what
decides which branch tileiras takes. There is no fallback: a
kernel emitted by a frontend that expects `sm_90a` semantics will
not compile against `sm_90`, and vice versa.

## Cross-References

[NVPTX Subtarget and Feature Matrix — The 40 CPU Rows](../codegen/nvptx-subtarget-and-feature-matrix.md#the-40-cpu-rows)
catalogs every CPU row, including which `a`/`f` variants imply
`tmem`. [Per-SM Emission Templates — Capability Matrix](../codegen/per-sm-emission-templates.md#capability-matrix)
walks the actual instruction surfaces unlocked at each SM tier.
[Attribute System and Lowering — Lifecycle of a Kernel Attribute](attribute-system-and-lowering.md#lifecycle-of-a-kernel-attribute)
explains how `compute_capability` propagates from the driver through
to `AttachNVVMTarget`. [Driver CLI Options](../driver/cli-options.md#enum-valued-options-as-int32-codes)
documents the `--gpu-name` enum table. [Position in nvcc 13.1](../boundaries/nvcc-13-1-position.md#path-b-tileiras-new-mlir-bytecode)
covers the fatbin assembly that wraps multi-architecture builds.
[AsmPrinter — Module Header Directives](../codegen/asm-printer-monster-and-windows.md#module-header-directives)
shows the exact printing path for the three-directive header.
