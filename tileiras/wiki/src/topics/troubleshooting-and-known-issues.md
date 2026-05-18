# Troubleshooting and Known Issues

## Abstract

A practical map from symptom to root cause. Each entry pairs the user-visible
text — the literal stderr line, the exit code, or the silent behavior — with
the layer that produced it and the change that resolves it. The catalog also
covers diagnostic typos that are part of the public contract (and therefore
preserved), wire-format incompatibilities between tileiras's bytecode and
upstream MLIR tooling, and a set of gotchas that are not bugs but routinely
surprise first-time users.

The page is organized by where in the pipeline the error originates rather
than by what the user typed. Driver-level rejections fire before any pass
runs and produce exit codes 2 or 3 from
[Driver Program Handle](../driver/program-handle.md#public-error-codes).
Verifier failures fire inside the pass manager and produce exit code 5.
Codegen failures surface as either an LLVM `report_fatal_error` (abort) or
an exit code 5 carrying a ptxas-shaped tail. The verifier-ladder positioning
of each layer is documented in
[Correctness Layers](correctness-layers.md), and the exit-code contract is
documented in [Error Handling and Diagnostics](error-handling-and-diagnostics.md).

## Symptom-driven index

| If you see | Read |
|---|---|
| `failed to parse IR bytecode (it looks like MLIR bytecode instead)` | [Bytecode parse failures](#bytecode-parse-failures) |
| `unknown attribute tag` from the bytecode reader | [Bytecode parse failures](#bytecode-parse-failures) |
| `unsupported GPU target`, `invalid optimization level` | [Driver-level rejections](#driver-level-rejections) |
| `optimized debugging is not supported` | [Driver-level rejections](#driver-level-rejections) |
| `could not find libdevice` | [Driver-level rejections](#driver-level-rejections) |
| `op expects ... arguement types to match ...` (note the typo) | [Verifier failures](#verifier-failures) |
| `Formal parameter space overflowed (X bytes required, max Y bytes allowed)` | [Verifier failures](#verifier-failures) |
| `Not a canonical UMMA_MN Layout: No flat offset mode` | [Verifier failures](#verifier-failures) |
| `colletor::a` (note the typo) in tcgen05 diagnostics | [Verifier failures](#verifier-failures), [Known typos](#known-typos-in-diagnostic-strings) |
| Cannot find WGMMA in selection, sm_90 target | [Codegen failures](#codegen-and-backend-failures) |
| `Function uses too much shared data`, ptxas stderr | [Runtime and ptxas failures](#runtime-and-ptxas-failures) |
| Cluster launch silently fails or aborts at runtime | [Gotchas](#gotchas) |
| TMA descriptor produces garbage output | [Gotchas](#gotchas) |

## Bytecode parse failures

The bytecode reader is the first stage every invocation passes through.
Three failure modes surface here, all returned as driver exit code 3.

**Symptom.** `input does not correspond to Tile IR bytecode`. Exit code 3.
**Cause.** The magic word at the head of the file is not the Tile IR magic.
The driver checked `looks_like_mlir_bytecode` first and that probe also
failed; the file is neither Tile IR bytecode nor upstream MLIR bytecode.
**Fix.** Verify the producer. Tile IR bytecode comes from the frontend's
serializer; a randomly named file with a `.bc` extension is almost
certainly LLVM bitcode and belongs on ptxas's plate, not tileiras's.

**Symptom.** `failed to parse IR bytecode (it looks like MLIR bytecode instead)`.
Exit code 3.
**Cause.** The magic-word probe matched upstream MLIR bytecode. This is
almost always a wire-format incompatibility (see
[Wire-format incompatibilities](#wire-format-incompatibilities) below): the
caller used `mlir-translate --serialize-bytecode` or
`mlir-opt --emit-bytecode` on a module that happens to import Tile IR
dialects, and the resulting file uses upstream AttrTag numbering rather
than tileiras's.
**Fix.** Re-serialize through the tileiras-aware bytecode writer. The
frontend's emission path is described in
[Frontend Contract and Tile IR Emission](frontend-contract-and-tile-ir-emission.md);
the bytecode envelope itself is in
[MLIR Bytecode Format](../bytecode/mlir-bc-format.md).

**Symptom.** `unknown attribute tag N` from the bytecode reader, with `N` an
integer. Exit code 5 (parser failures surface after the magic check
succeeded, so the driver classes them as compile failures rather than
configuration failures).
**Cause.** The bytecode envelope passed the magic check but a per-dialect
reader hit an AttrTag value it does not recognize. Tileiras's
[Dialect Reader/Writer Status](../bytecode/dialect-readers-status.md)
table shows which dialect-specific readers know which tag ranges; a tag
outside the recorded ranges typically means the producer is from a
different tileiras snapshot.
**Fix.** Pin the producer and the consumer to the same tileiras revision.
The AttrTag numbering is not stable across snapshots.

## Driver-level rejections

The driver validator runs before any pass begins and rejects ill-formed
inputs with exit code 2 (configuration) or 3 (bytecode shape). The
verbatim text appears in [Driver CLI Options — Validation Algorithm](../driver/cli-options.md#validation-algorithm).

**Symptom.** `unsupported GPU target`. Exit code 2.
**Cause.** `--gpu-name` was set to a string the driver's accept table does
not list. The accept table covers `sm_100`, `sm_103`, `sm_110`, `sm_120`,
`sm_121`. Note in particular that `sm_90` is not in the driver's accept
table — Hopper is reachable only through a frontend that writes the
`nv_tileaa.compute_capability` module attribute directly.
**Fix.** Pick a supported spelling. The full list is in
[Driver CLI Options — Enum-valued Options](../driver/cli-options.md#enum-valued-options-as-int32-codes);
the subtarget mechanism that turns the spelling into `.target sm_NNa`
is documented in
[PTX Version and Target Selection](ptx-version-and-target-selection.md).

**Symptom.** `invalid optimization level`. Exit code 2.
**Cause.** `--opt-level` was outside `0..3`. The driver checks `(uint32_t)opt > 3`
so negative values appear as huge unsigned values and also fail.
**Fix.** Use `0`, `1`, `2`, or `3`. The driver and pipeline use different
defaults (3 vs 2); both are valid on the CLI.

**Symptom.** `optimized debugging is not supported, change optimization level to 0 or disable full debug info`.
Exit code 2.
**Cause.** `--device-debug` (or `-g`) was combined with `--opt-level != 0`.
Full device debug injects NVVM debug options that disable several
code-motion and block-merge transforms; the driver rejects the
combination rather than silently degrading the build.
**Fix.** Either drop `-g` or set `-O0`. For lighter source-line context
without disabling optimization, use `--lineinfo` instead.

**Symptom.** `could not find libdevice.bc`, or a missing-symbol error from
the math pipeline after the NVVM-Reflect pass.
**Cause.** Tileiras resolves the libdevice path from the environment
variables `CUDA_HOME`, `CUDA_PATH`, `CUDA_ROOT`, and the install-relative
fallback; when none of them point at a directory containing
`libdevice.10.bc`, the math pipeline cannot link the device math
intrinsics and ends up with unresolved symbols at the NVVM verifier
layer.
**Fix.** Export one of `CUDA_HOME`, `CUDA_PATH`, or `CUDA_ROOT` to the
CUDA install root that ships `nvvm/libdevice/libdevice.10.bc`. The full
env-var contract is in
[Env Vars and Runtime Gates](../driver/env-vars-and-runtime-gates.md);
the link order is covered in [Libdevice Overview](../libdevice/overview.md).

**Symptom.** `unsupported host operating system`, `unsupported host architecture`. Exit code 2.
**Cause.** `--host-os` or `--host-arch` was a string outside the accept
table (`linux`, `windows` and `x86_64`, `aarch64`, `arm64ec` respectively).
**Fix.** Pick a supported spelling. There is no autodetection on the CLI
surface; the defaults are platform-derived but the CLI parser rejects
arbitrary strings.

## Verifier failures

Verifier diagnostics fire inside the pass manager and propagate to the
driver as exit code 5. The text is part of the public contract — frontends
and tests key off the exact spelling. The verifier ladder (per-op, between-pass,
named, NVVM IR) is described in [Correctness Layers](correctness-layers.md).

**Symptom.** `op expects ... arguement types to match with producer types ...`.
Note the verbatim typo `arguement`.
**Cause.** A region-bearing op (typically `nv_tileas.async.pipeline.consume_one`)
was left with a region-argument list that does not match its paired producer's
result list. This is almost always a partial-rewrite leftover from a pass that
aborted between the producer and consumer rewrites.
**Fix.** Identify the pass that touched the op last; the between-pass
verifier names it. The verbatim diagnostic is preserved with the typo;
see [Known typos](#known-typos-in-diagnostic-strings) and
[nv_tileas Verifiers — Region-Op Verifier Template](../dialects/nv_tileas/verifiers.md).

**Symptom.** `Formal parameter space overflowed (X bytes required, max Y bytes allowed) in function Z`.
**Cause.** The kernel's by-value parameter struct, summed with target
alignment rules, exceeds the SM's parameter-space ceiling. For sm_75 the
ceiling is 1024 bytes; for sm_80 it is 32760 bytes; the per-SM table is
in [NVVM IR Verifier](../nvptx-passes/nvvm-ir-verifier.md).
**Fix.** Restructure the kernel signature. Move the bulk of the data
behind a global-memory pointer, split into two kernels, or pack with
explicit alignment. The diagnostic is the actionable form; the ptxas
analogue is far less specific.

**Symptom.** `Not a canonical UMMA_MN Layout: No flat offset mode`.
**Cause.** A `cute_nvgpu` layout passed to a UMMA verifier does not match
the verifier's expected canonical shape. The verifier walks the layout's
mode tree and requires a flat offset mode at a specific position.
**Fix.** Have the frontend emit the canonical layout shape, or run a
layout-canonicalization pass before the consumer. The mode-pattern
contract is in
[cute_nvgpu Mode-Pattern Verifiers](../dialects/cute_nvgpu/mode-pattern-verifiers.md).

**Symptom.** `expects #C element type to be f32, but got <type>`.
**Cause.** A WGMMA or `tcgen05.mma` op was built with a non-f32
accumulator type. The verifier rejects accumulator types its emission
template does not have a code path for.
**Fix.** Use `f32`. The accumulator-type matrix is in
[WGMMA Emission Protocol](wgmma-emission-protocol.md) and
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md).

**Symptom.** `expected TileType for block arguments but got types: ...`.
**Cause.** The frontend built a block whose entry argument types do not
match the surrounding `tile` op's contract. This typically means the
emitter passed an LLVM-tier type into a Tile-tier region.
**Fix.** Run the frontend's tile-type fixup before the tileiras entry
point; the tile-type discipline is described in
[Frontend Contract and Tile IR Emission](frontend-contract-and-tile-ir-emission.md).

**Symptom.** `'tcgen05.alloc' op expects colletor::a layout` (note the
typo `colletor::a`).
**Cause.** A `tcgen05` allocation was passed a layout that does not match
the expected collector shape.
**Fix.** Search source by the literal typo, not by the corrected
spelling. The full diagnostic catalog for the dialect is in
[tcgen05 Ops](../dialects/nvvm/tcgen05-ops.md).

## Codegen and backend failures

These fire during MLIR-to-LLVM conversion or NVPTX instruction selection.

**Symptom.** `WGMMA requires sm_90a`, or an unhelpful "Cannot select"
message from the NVPTX selector.
**Cause.** WGMMA is arch-conditional. Plain `sm_90` does not enable the
WGMMA instruction set; the `a` suffix on the target string is required.
Tileiras's driver does not accept `sm_90a` as a `--gpu-name` value — the
suffix is selected by the frontend through the
`nv_tileaa.compute_capability` module attribute.
**Fix.** Have the frontend write the arch-conditional attribute. The
mechanism is in [PTX Version and Target Selection](ptx-version-and-target-selection.md).

**Symptom.** Cannot select `tcgen05.*` intrinsic, or a similar selector
error.
**Cause.** tcgen05 instructions are introduced at sm_100. A target below
sm_100 cannot legalize them.
**Fix.** Use `--gpu-name=sm_100` (and ensure the frontend selects
`sm_100a` if the program uses arch-conditional variants). The per-SM
intrinsic matrix is in
[NVPTX Subtarget and Feature Matrix](../codegen/nvptx-subtarget-and-feature-matrix.md).

**Symptom.** `unsupported tma load mode '<mode>'`, where the mode name
contains `im2col`.
**Cause.** The frontend emitted an im2col TMA descriptor against a target
that does not implement that descriptor variant. Im2col was added later
than the basic tiled mode and is not available on every SM that has TMA.
**Fix.** Pick the basic tiled mode or pick a target that supports im2col.
The atom registry is in [TMA Atoms](../dialects/cute_nvgpu/tma-atoms.md).

**Symptom.** Compilation succeeds but `dsmem` operations produce zero or
garbage at runtime on sm_80.
**Cause.** Distributed shared memory (`dsmem`) requires sm_90 or higher.
Lowering does not reject the op on sm_80 at the dialect level; the lowered
PTX exists but the hardware path is absent.
**Fix.** Target sm_90 or higher. The DSMEM handshake is documented in
[Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md).

## Runtime and ptxas failures

Tileiras invokes ptxas as a subprocess. ptxas diagnostics surface through
the harness's stderr capture; the driver returns exit code 5 carrying the
ptxas text.

**Symptom.** `ptxas: error: Function '<name>' uses too much shared data`.
**Cause.** The kernel's static + dynamic shared-memory footprint exceeds
the per-SM shared-memory ceiling. Tileiras's smem-accounting does not
re-check the ceiling after pipeline buffer assignment, so the limit
surfaces only at ptxas.
**Fix.** Reduce SMEM pressure — fewer pipeline stages, smaller tile
sizes, or split into two kernels. See
[Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md).

**Symptom.** `ptxas: error: Multiple kernel definitions` for the same
function name.
**Cause.** The frontend emitted the same `nvvm.kernel` function twice,
typically because two upstream modules with the same kernel name were
merged before tileiras saw them.
**Fix.** Deduplicate at the frontend. Tileiras does not rename to break
collisions.

**Symptom.** `ptxas: error: Address out of range`, or any internal ptxas
assertion.
**Cause.** Almost always a tileiras codegen bug. ptxas does not normally
diagnose the producer; an Address-out-of-range from ptxas usually means
the AsmPrinter emitted an out-of-range immediate.
**Fix.** Report. Capture the full invocation (see
[Reporting a bug](#reporting-a-bug)).

**Symptom.** ptxas exits non-zero with no stderr text.
**Cause.** ptxas crashed (signal exit) rather than failing cleanly. The
subprocess harness reports the non-zero status but cannot reconstruct a
diagnostic from a signal-killed child.
**Fix.** Re-run ptxas directly on the PTX text tileiras emitted; the
harness logs the argv on debug builds. The handoff protocol is
documented in [ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md).

## Known typos in diagnostic strings

The following typos are present in the binary's diagnostic strings and
are preserved across snapshots. Downstream tooling (log scrapers,
test-failure classifiers, frontend translation tables) keys on the
verbatim text, so a corrected spelling would be a wire-format-style
break. When grepping the binary, the build directory, or production
logs, use the typo'd form; the corrected form will not match.

| Verbatim string in binary | Corrected English | Where it fires |
|---|---|---|
| `colletor::a` | `collector::a` | `tcgen05` layout verifier in [tcgen05 Ops](../dialects/nvvm/tcgen05-ops.md) |
| `arguement` (e.g. `region arguement types to match`) | `argument` | region-op verifiers in [nv_tileas Verifiers](../dialects/nv_tileas/verifiers.md) |
| `types to be match` | `types to match` | several pattern-side rewrite diagnostics |
| `paramater` (occasional) | `parameter` | a small number of parser callouts |
| `succeded` | `succeeded` | a single info-class message from the pass instrumentation |

A reimplementer who silently corrects the spelling produces a binary
whose diagnostics no longer line up with the recovered binary's
downstream consumers. The corrections must be a coordinated change at
the consumer side first.

## Wire-format incompatibilities

Tileiras's bytecode envelope reuses upstream MLIR's container format but
the per-dialect AttrTag numbering is tileiras-specific. Mixing upstream
MLIR tooling with tileiras bytecode produces files that pass the magic
check but fail at the per-dialect reader.

**Pitfall.** `mlir-translate --serialize-bytecode` on a module that
imports `cuda_tile`, `nv_tileaa`, `nv_tileas`, `cute`, or `cute_nvgpu`
emits the upstream AttrTag numbering. When tileiras reads it, the
per-dialect reader sees a tag value outside its accept range and emits
`unknown attribute tag N`. Use the tileiras-aware serializer that
ships with the frontend.

**Pitfall.** `mlir-opt --emit-bytecode` produces the same shape and
fails the same way. The `--emit-bytecode` flag is in upstream
`mlir-opt`; the tileiras driver does not expose an `--emit-bytecode`
mode because it consumes bytecode rather than producing it.

**Pitfall.** Bytecode produced by an older tileiras snapshot may fail
the version word check at the envelope level (`unsupported version`),
even if every dialect tag would otherwise be readable. The envelope
version is independent of the dialect AttrTag versioning, and both must
match.

**Pitfall.** Mixing two tileiras snapshots' bytecode in a single
multi-module compile (for example, by linking a prebuilt library
bytecode with a newly emitted module) is not supported. The dialect
tag numbering can shift between snapshots without an envelope-level
version bump.

The full bytecode envelope is documented in
[MLIR Bytecode Format](../bytecode/mlir-bc-format.md); per-dialect tag
range support is in
[Dialect Reader/Writer Status](../bytecode/dialect-readers-status.md).

## Gotchas

These are not bugs. They are mechanism details that routinely trip up
first-time users.

**Cluster dim must be a power of 2.** The cluster verifier accepts only
`1`, `2`, `4`, or `8` for each cluster-dim component, with the product
bounded by 16. A frontend that emits `cluster_dim = [3, 1, 1]` fails
the verifier. The hardware does not implement non-power-of-2 cluster
shapes; the verifier is enforcing a hardware constraint, not a
tileiras convention. See
[Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md).

**TMA descriptor alignment is 128 bytes.** The TMA descriptor passed to
`cp.async.bulk.tensor` must be aligned to 128 bytes in host memory.
Tileiras's host-side descriptor mutator assumes the alignment; a
misaligned descriptor produces incorrect copies at runtime rather than
a clean error. The frontend's descriptor builder is responsible for the
alignment; if the descriptor lives in a host allocator that does not
honor the alignment, the descriptor builder must over-allocate and
align manually.

**WGMMA accumulator reads inside the body are silent UB.** The WGMMA
accumulator can only be read after `wgmma.wait_group N` completes for
the relevant group. Reading the accumulator earlier — inside the
warp-group body before the matching wait — produces no diagnostic and
no compile-time rejection; the read silently returns stale data. The
read-after-wait discipline is documented in
[WGMMA Emission Protocol](wgmma-emission-protocol.md).

> ⚡ **QUIRK — accumulator-before-`fence_async` is silent UB, not a verifier error**
> The natural assumption is that any verifier that knows about WGMMA also knows that the accumulator is asynchronously written and would diagnose a too-early read. It doesn't. The mbarrier/fence_async ordering is enforced only at runtime by the hardware's async-proxy ordering rules; the MLIR verifier accepts a use of the accumulator SSA value at any point after the WGMMA op, including between `wgmma.commit_group` and `wgmma.wait_group N`. The read compiles, runs, and returns whatever bits the accumulator register held before the WGMMA retired — usually the previous iteration's result, occasionally garbage from a sibling warp-group's register reuse. There is no `--Werror` flag that catches it; the discipline is a frontend obligation.

**`--use-fast-math` enables FTZ even when no op carries fast-math
flags.** The driver's fast-math flag is a global on/off; it does not
key off per-op MLIR fast-math metadata. A program that writes
fast-math-free MLIR but compiles with `--use-fast-math` (or the
pipeline option `ftz=true`) still emits FTZ-mode arithmetic. To get
non-FTZ arithmetic, leave the global flag off.

> ⚡ **QUIRK — `--use-fast-math` is a module-wide FTZ master switch with no per-function escape hatch**
> Upstream LLVM exposes FTZ as a function attribute (`denormal-fp-math`) plus per-op `FastMathFlags`, so a single hot kernel can opt in while the rest of the module stays IEEE. Tileiras's driver flag short-circuits that: enabling `--use-fast-math` writes the `unsafe-fp-math` Function attribute onto *every* function it lowers, and the case-`0x66` FMA selector reads that attribute and picks the FTZ opcode unconditionally. There is no `__attribute__((noflush))` or per-function override that reverses the global flag — a function that needs IEEE-denormal arithmetic must be compiled in a *separate* invocation with the flag off, then linked in. The same applies to the pipeline option `ftz=true`.

**Libdevice link order matters.** `NVVMReflect` must run before
always-inline. If a pipeline rearrangement moves always-inline above
NVVMReflect, libdevice's `__nvvm_reflect` calls get inlined with
unresolved arguments and the math intrinsics emit fallback bodies
rather than fast paths. The pass order is in
[NVVMReflect Mechanism](../libdevice/nvvm-reflect-mechanism.md).

**`--gpu-name` does not accept the `a` or `f` suffix.** The driver's
parser only matches the bare `sm_NN` form. Architecture-conditional
selection (`sm_90a`, `sm_100a`, etc.) is decided by the frontend's
module attribute and combined with `--gpu-name` to pick the final
`.target` line. A user trying to "force" `sm_100a` from the CLI cannot
do so; the frontend must write the attribute.

> ⚡ **QUIRK — `--gpu-name=sm_90a` is silently rejected, not diagnosed**
> The driver's `--gpu-name` accept table contains only the bare numeric forms (`sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121`); the family- and arch-conditional suffixes (`a`, `f`) that downstream tools like `ptxas` accept are *not* in this table. A user who writes `--gpu-name=sm_90a` does not get an "unknown target" diagnostic — the parser either ignores the unrecognised string and falls back to the default or rejects it with a generic "unrecognised --gpu-name" message that does not mention the suffix. The architecture-conditional `.target sm_90a` line is written by the frontend's module attribute, not the CLI, so the only way to compile for `sm_90a` is to set that attribute and pass `--gpu-name=sm_90` — but `sm_90` is *also* missing from the accept table on the current driver (Blackwell-first), so Hopper builds bypass `--gpu-name` entirely.

**The driver `--opt-level` default differs from the pipeline
`opt-level` default.** Driver default is `3`; pipeline default is `2`.
A user invoking the pipeline directly through `--pass-pipeline`
without specifying `opt-level` gets `2`; a user invoking the driver
without `--opt-level` gets `3`. The mismatch is documented but a
common source of "I changed nothing and the output changed" reports.

> ⚡ **QUIRK — driver and pipeline disagree on the default `opt-level`**
> Two entry points to the same compiler read different defaults from different option tables: `--opt-level` on the driver defaults to `3`, while `opt-level=` inside `--pass-pipeline` defaults to `2`. Identical IR therefore produces different PTX depending on whether the user invoked the driver or hand-rolled the pipeline, with no warning either way. This is the canonical "I changed nothing and the output changed" failure mode — pin `opt-level=` explicitly in both invocations to make builds reproducible across entry points.

**`sm_90` is not in the driver's accept table.** The driver targets
Blackwell as its primary deployment surface. Hopper builds go through
a frontend that writes `compute_capability` directly and bypass the
`--gpu-name` table.

## Reporting a bug

A useful bug report contains five artifacts. Capture them at the time
of the failure; reconstructing them after the fact is significantly
harder.

1. The exact tileiras invocation. The full argv vector and the
   environment variables `CUDA_HOME`, `CUDA_PATH`, `CUDA_ROOT`,
   `LD_LIBRARY_PATH`, and any `TILE*` variable. The env-var catalog
   is in
   [Env Var and Runtime Gate Catalog](../reference/env-var-and-runtime-gate-catalog.md).
2. The input Tile IR. Either the original bytecode buffer or the
   textual form produced by `--mlir-print-ir-before-all` (which dumps
   the IR before every pass; the last entry before the crash is the
   one that mattered). See
   [Debugging and Introspection](debugging-and-introspection.md).
3. The full stderr output. Tileiras's diagnostics arrive in emission
   order; the first diagnostic is usually the actionable one even when
   later diagnostics look more dramatic.
4. The scheduler trace, if the failure is scheduling-related. Pass
   `schedule-trace-file=<path>` through `--pass-pipeline` to emit the
   Chrome-timeline JSON. The format is described in
   [Modulo Driver and 4-Arm OR-Chain](../scheduler/modulo-driver-or-chain.md).
5. The SM target, CUDA version, and host platform. SM target is in the
   argv; CUDA version comes from `${CUDA_HOME}/version.txt` or
   `nvcc --version`; host platform is `uname -srm` on Linux,
   `cmd /c ver` on Windows.

A report that omits any of these forces the maintainer to ask for it
before any diagnosis can begin; one with all five usually permits an
immediate root-cause hypothesis.

## Cross-references

[Error Handling and Diagnostics](error-handling-and-diagnostics.md) is
the canonical reference for the diagnostic engine, the pass-failure
handshake bit, and the five driver-level exit codes; the symptoms in
this page are organized by the layer that produces them in that page's
architecture.
[Correctness Layers](correctness-layers.md) places each verifier-level
failure mode in its layer (per-op, between-pass, named, NVVM IR,
ptxas), explaining why the same overflow can appear with two different
diagnostic texts at two different layers.
[Driver CLI Options](../driver/cli-options.md) is the source for every
driver-level rejection string; the validation algorithm there matches
the [Driver-level rejections](#driver-level-rejections) section
line-for-line.
[Debugging and Introspection](debugging-and-introspection.md) is the
primary reference for the introspection flags
(`--mlir-print-ir-before-all`, `--mlir-print-ir-after-failure`,
`schedule-trace-file`, `dump-host`) used to assemble a bug report.
[MLIR Bytecode Format](../bytecode/mlir-bc-format.md) and
[Dialect Reader/Writer Status](../bytecode/dialect-readers-status.md)
document the envelope and AttrTag contracts that the
[Wire-format incompatibilities](#wire-format-incompatibilities) section
describes from the user's side.
[Frontend Contract and Tile IR Emission](frontend-contract-and-tile-ir-emission.md)
documents the producer side of the same wire-format contract and the
tile-type discipline a frontend must observe to avoid the verifier
failures in this page's catalog.
[Env Vars and Runtime Gates](../driver/env-vars-and-runtime-gates.md)
covers the `CUDA_HOME` / `CUDA_PATH` / `CUDA_ROOT` resolution that the
libdevice gotcha turns on.
[Testing and Observability](testing-and-observability.md) takes the
verbatim diagnostic strings catalogued here — including the preserved
typos in [Known typos](#known-typos-in-diagnostic-strings) — and shows
how to pin them as golden-test assertions so a downstream regression
suite detects diagnostic-catalog drift between snapshots.
