# Frequently Asked Questions

## Abstract

This page collects the questions a reader most often arrives with and points
each one at the page that answers it in full. The entries are short by
design: enough context to confirm that the question matches the situation,
plus a link into the subsystem documentation. Detailed contracts, pseudocode,
and confidence claims live on the linked pages.

The page is organized into five clusters: what tileiras is, how to use it,
how to read this wiki, suggested reading paths for common goals, and meta
questions about the project itself.

## About tileiras

### What is tileiras?

Tileiras is NVIDIA's CUDA TileIR optimizing assembler, shipped in CUDA 13.1
as a separate compiler binary that sits alongside `cicc`, `ptxas`, and
`cudafe++`. It consumes MLIR bytecode describing a tile-level GPU program,
runs that program through a cascade of nine dialect layers, and emits a host
ELF relocatable that carries compiled SASS for one Blackwell-family target.

The narrow framing is the useful one. Tileiras does not parse CUDA C++, does
not handle host-side template expansion, and does not generate launch stubs.
Those responsibilities belong elsewhere in the toolchain. Tileiras begins
after a frontend has already produced Tile IR and ends after `ptxas` has
finished. The high-level shape is documented in
[Tileiras Internals](../index.md) and in
[Position in nvcc 13.1](../boundaries/nvcc-13-1-position.md).

### How is tileiras different from cicc?

The two binaries are sibling device compilers that target different input
languages. `cicc` is the legacy LLVM-based path: CUDA C++ enters through
`cudafe++`, lowers through a device LLVM IR cascade, and emits PTX.
Tileiras is the MLIR-based path: a tile frontend emits MLIR bytecode,
tileiras runs the dialect cascade, and emits PTX. They share `ptxas` as a
downstream and they share several IR concepts in the lower layers, but they
do not share a frontend, a pass pipeline, or a scheduler.

The contrast is unpacked in [cicc Comparison](../boundaries/cicc-comparison.md).

### Is tileiras open source?

Mostly no. A small portion of the `cuda_tile` dialect appears in NVIDIA's
public CUTLASS repository and that portion is what the
[OSS Comparison Overview](../oss/overview.md) maps. The rest of the program,
including every other dialect, the scheduler, the NVPTX customisations, and
the driver, is closed and ships only as a binary in the CUDA toolkit. The
[.td Files Delta](../oss/td-files-delta.md) page enumerates which TableGen
files have public counterparts.

### What architectures does tileiras support?

The driver accepts the SM levels listed in the
[CLI Options](../driver/cli-options.md) page as valid `--gpu-name` values.
The supported set includes `sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121`,
their `a` (architecture-specific) variants, and a backward-compatibility
range that covers earlier Hopper and Ampere targets used by older CUTLASS
atoms. The exact mapping from SM level to feature set is in
[PTX Version and Target Selection](ptx-version-and-target-selection.md).

## Using tileiras

### How do I invoke tileiras?

Normally, you do not invoke it directly. `nvcc` invokes tileiras when the
input is Tile IR bytecode produced by a tile frontend. Direct invocation is
also supported and matches the form
`tileiras --gpu-name=<target> --opt-level=<n> -o <output> <input>`. The
full option matrix is in [CLI Options](../driver/cli-options.md) and the
runtime gates that change behavior without appearing on the command line are
in [Env Vars and Runtime Gates](../driver/env-vars-and-runtime-gates.md).

### How do I produce Tile IR bytecode?

A tile frontend produces it. NVIDIA's Triton-style frontend and the CUTLASS
DSL frontend are the two known producers. Either one emits MLIR bytecode
that uses the dialect tags tileiras expects. Hand-writing Tile IR is
possible through `mlir-translate` with a tileiras-aware bytecode writer,
but it is not the supported path. The frontend contract, including the
dialect schema that a producer must obey, is in
[Frontend Contract and Tile IR Emission](frontend-contract-and-tile-ir-emission.md).

### Why doesn't `mlir-translate --serialize-bytecode` produce tileiras-readable files?

Tileiras's bytecode uses a wire format that diverges from upstream MLIR
bytecode in the attribute and type tag tables. Upstream `mlir-translate`
writes upstream tags; the tileiras reader expects its own tag space and
rejects files that probe as upstream bytecode. The reader probes for the
upstream magic explicitly so that this case produces a specific diagnostic,
`failed to parse IR bytecode (it looks like MLIR bytecode instead)`. The
wire-format contract is in
[MLIR Bytecode Format](../bytecode/mlir-bc-format.md) and the dialect-by-dialect
status is in [Dialect Reader/Writer Status](../bytecode/dialect-readers-status.md).

### My compile fails with `--device-debug --opt-level=3`. Why?

The combination is rejected at driver level with `optimized debugging is
not supported`. `--device-debug` implies `--opt-level=0`; raising the
optimization level past that is an error rather than a warning. Use
`--lineinfo` for source mapping at higher optimization levels. The full
debugging story is in
[Debugging and Introspection](debugging-and-introspection.md).

### What does `--gpu-name=sm_90a` mean versus `--gpu-name=sm_90`?

The `a` suffix marks an architecture-specific target. `sm_90a` unlocks
Hopper-only instructions, most importantly WGMMA, that are not part of the
forward-compatible `sm_90` baseline. Code compiled for `sm_90a` does not
run on later architectures without recompilation; code compiled for `sm_90`
does. The same pattern repeats for `sm_100a`, `sm_103a`, `sm_120a`, and
`sm_121a`. The selection logic is in
[PTX Version and Target Selection](ptx-version-and-target-selection.md).

## Understanding the wiki

### How accurate is this wiki?

For verbatim artifacts, very accurate. Diagnostic strings, opcode mnemonics,
attribute schemas, and bit-field layouts are extracted from the binary
byte-by-byte and carry HIGH confidence. For named functions like
`sub_ABCDEF`, the addresses are exact but the names are auto-generated by
IDA Pro because the binary is stripped; the algorithm descriptions on those
pages are derived from disassembly rather than from a source-level symbol.
The confidence taxonomy that every page uses is in
[String Evidence and Confidence Policy](../reference/string-evidence-and-confidence-policy.md).

### Why are there `sub_XXX` references throughout the wiki?

Tileiras ships as a stripped binary, so the original function names are not
recoverable. IDA Pro names unknown functions `sub_<hex_address>` and the
wiki keeps that convention as the canonical reference for a function whose
real name is unknown. The address is stable across analyses and useful for
cross-referencing the binary; the prose around it describes what the
function does. The reverse-engineering methodology is in
[Binary Anatomy and RE Methodology](binary-anatomy-and-re-methodology.md).

### What is the difference between cute, cute_nvgpu, and cutlass?

Three layers of the CUTLASS programming model, each a separate dialect.
`cute` is the layout algebra and tile-decomposition primitive set. The
contract is in [cute Overview](../dialects/cute/overview.md). `cute_nvgpu`
is the NVIDIA-specific atom layer that binds layouts to actual hardware
copy and MMA instructions; its roster is in
[cute_nvgpu Overview](../dialects/cute_nvgpu/overview.md). `cutlass` is the
high-level pipeline and tile-scheduler dialect that orchestrates kernels
built from the lower two; its overview is in
[cutlass Overview](../dialects/cutlass/overview.md).

### What does "wave-specialized" mean?

A scheduling pattern, also called producer-consumer specialization, where
one warp-group performs asynchronous loads and another warp-group performs
the matrix-multiply. The division is explicit in the IR: a producer
warp-group issues TMA copies and signals an `mbarrier`, the consumer
warp-group waits on that mbarrier and consumes the data. The op roster is
in [nv_tileas Op Roster and Builders](../dialects/nv_tileas/op-roster-and-builders.md)
and the synchronization protocol is in
[mbarrier State Machine](mbarrier-state-machine.md).

### What is mbarrier?

A transactional barrier living in shared memory, introduced on Hopper as
the synchronisation primitive for asynchronous copies. An mbarrier carries
an arrival count and a transaction-byte count; producers update the
transaction count when their copy commits, consumers wait until both
counters reach a threshold. The state machine is documented in
[mbarrier State Machine](mbarrier-state-machine.md).

### What is TMA?

The Tensor Memory Accelerator, a Hopper-and-later hardware engine for
asynchronous bulk tensor copies between global and shared memory. The TMA
descriptor (`CUtensorMap`) encodes the multi-dimensional shape and
swizzling; the copy itself is initiated by `cp.async.bulk.tensor` and its
completion is ordered through an mbarrier. The codegen contract is in
[TMA, Tensormap and cp.async.bulk](../codegen/tma-tensormap-and-cp-async-bulk.md).

### What is WGMMA?

Warp-Group Matrix Multiply-Accumulate, the Hopper `sm_90a` instruction in
which four warps cooperate to issue one matrix-multiply against shared-memory
operands described by a 64-bit descriptor. The descriptor layout, the
synchronisation fence sequence, and the way the scheduler treats WGMMA
issue groups are in [WGMMA Emission Protocol](wgmma-emission-protocol.md).

### What is tcgen05?

The Blackwell `sm_100a` matrix-multiply family that replaces WGMMA. Unlike
WGMMA, tcgen05 keeps operands and accumulators in a dedicated tensor memory
(TMEM) bank rather than in registers, and supports 2-CTA and 4-CTA modes
where the multiply spans multiple thread-blocks within a cluster. The
tensor-memory programming model is in
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) and the
multi-CTA variants are in
[Blackwell 2-CTA/4-CTA MMA](blackwell-2cta-and-4cta-mma.md).

## Reading paths

### I want to reimplement tileiras

Read [Position in nvcc 13.1](../boundaries/nvcc-13-1-position.md) first to
fix the binary's role, then [Program Layout](../binary-layout.md) for the
executable shape, then [Pipeline Overview](../pipeline/overview.md) for the
top-to-bottom cascade. Drill into whichever subsystem you are implementing
next. Verify any single claim against the binary using the recipes in
[Binary Anatomy and RE Methodology](binary-anatomy-and-re-methodology.md).

### I want to write a Tile IR frontend

Read [Frontend Contract and Tile IR Emission](frontend-contract-and-tile-ir-emission.md)
for the dialect schema your emitter must satisfy, then
[cuda_tile Overview](../dialects/cuda_tile/overview.md) for the public-input
dialect, then [DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) to follow a
worked example from frontend output to PTX.

### I want to understand WGMMA emission

Read [WGMMA Emission Protocol](wgmma-emission-protocol.md) for the issue
contract, then [cute_nvgpu MMA Atoms SM70-120](../dialects/cute_nvgpu/mma-atoms-sm70-120.md)
for the per-SM atom registry, then
[Per-SM Emission Templates](../codegen/per-sm-emission-templates.md) for the
PTX templates that the backend prints.

### I want to debug a slow kernel

Read [Performance and Cost Model](performance-and-cost-model.md) for the
scheduling cost function, then
[Debugging and Introspection](debugging-and-introspection.md) for the
diagnostic surfaces tileiras exposes, then
[Modulo Scheduler and Rau](../scheduler/modulo-scheduler-and-rau.md) when
the bottleneck reaches the scheduler itself.

### I want to verify a claim made on this wiki

Read [Binary Anatomy and RE Methodology](binary-anatomy-and-re-methodology.md)
for the verification recipes, then the
[String Evidence and Confidence Policy](../reference/string-evidence-and-confidence-policy.md)
for how each page tags its claims.

## Meta

### Who wrote this wiki?

Reverse engineering and writing by Grigory Evko. The project is not
endorsed by NVIDIA. Every claim derives from static analysis of the
publicly-distributed CUDA 13.1 tileiras binary.

### How can I contribute?

The wiki source lives at github.com/GrigoryEvko/crucible-notes under
`tileiras/wiki/`. Issues and pull requests are welcome. Corrections that
challenge a specific claim are most useful when they cite either a
reproducible behavior of the binary or a binary offset.

### Can I trust this wiki for production decisions?

For documentation and reimplementation reference, yes, within the
confidence labels each page declares. For correctness in production, treat
the wiki as a derived description and confirm any safety-critical behavior
against the actual binary. The wiki is a reverse-engineered model;
authoritative behavior lives only in the tileiras binary itself.

## Cross-references

The questions above point into the rest of the wiki; the converse direction
is [Reading Map](../bibliography.md), which organizes reading sequences by
subsystem rather than by question. The [Glossary](../glossary.md) defines
the terms used here without unpacking them. The
[Subsystem Map](../function-map.md) is the cross-reference for any
`sub_<hex>` name encountered while answering a follow-up question.
