# Host Launch ABI + ptxas Knobs

## Abstract

`tileiras` is assembler-side. It never calls `cuLaunchKernel`,
`cuLaunchKernelEx`, or `cuKernelSetAttribute` directly — instead it emits
kernel launch metadata into IR attributes and PTX directives, and `ptxas`
lifts that information into the cubin metadata consumed by the CUDA runtime
or driver.

The host-visible launch ABI splits across three channels:

1. PTX directives in each kernel `.entry` header.
2. MLIR `nvvm.*` attributes on lowered LLVM functions.
3. `gpu.launch_func` and `nv_tileaa.launch_func` properties that carry dynamic
   launch operands through the lowering pipeline.

The ptxas `--knobs-file=<path>` path is separate. `tileiras` forwards the
argument only when the environment gate is enabled; ptxas owns the file
grammar and every diagnostic.

## Host-side launch ABI

Since the driver never synthesizes CUDA-driver launch calls, the compiled
cubin carries static launch metadata and leaves dynamic launch assembly to
the consumer. Static metadata flows from `nvvm.*` attributes and PTX
directives; dynamic metadata rides on launch-operation properties and
segment-size arrays during MLIR lowering.

The split that matters:

| Channel | Carrier | Purpose |
| --- | --- | --- |
| Static thread shape | `nvvm.maxntid`, `nvvm.reqntid`, `.maxntid`, `.reqntid` | Communicates block shape constraints. |
| Static cluster shape | `nvvm.cluster_dim`, `.reqnctapercluster`, `.maxclusterrank` | Communicates SM90+ cluster launch constraints. |
| Static register budget | `nvvm.maxnreg`, `.maxnreg` | Communicates register budget to ptxas. |
| Static CTA residency hint | `nvvm.minctasm`, `.minnctapersm` | Communicates minimum CTAs per SM. |
| Dynamic operands | `operandSegmentSizes` | Preserves launch operand partitioning through lowering. |
| Dynamic shared memory | launch operand segment | Eventually drives `%dynamic_smem_size` in PTX/SASS. |

Cluster directives are gated to SM90 and newer. On older targets the
compiler suppresses `.blocksareclusters`, `.explicitcluster`,
`.reqnctapercluster`, and `.maxclusterrank` even when cluster-shaped
metadata is present upstream.

`gpu.launch_func` carries `kernelFunc`, `kernelModule`, and
`operandSegmentSizes`. The setter also accepts the older
`operand_segment_sizes` spelling for compatibility with MLIR v17-era IR.
By the `nv_tileaa.launch_func` stage the kernel reference flattens into a
single `kernel` property alongside the same operand segment sizing.

## nvvm.* Annotations and PTX Directives

The `nvvm.*` attribute family is the canonical in-IR carrier of launch
metadata. Legacy `!nvvm.annotations` tuples still parse and can be
transplanted into attribute form; an internal marker prevents repeated
legacy scans after the transplant.

The verifier enforces the shape rules that matter:

1. Dimensional attributes contain one to three `i32` values, except cluster
   dimensions, which require three values.
2. Scalar resource attributes are integer attributes.
3. `nvvm.blocksareclusters` requires both `nvvm.reqntid` and
   `nvvm.cluster_dim` on the same function.

| Kind | Attribute name | Shape | PTX projection | Target gate |
| --- | --- | --- | --- | --- |
| kernel | `nvvm.kernel` | UnitAttr | `.entry` instead of `.func` | all SMs |
| maxntid | `nvvm.maxntid` | 1..3 `i32` values | `.maxntid` | all SMs |
| reqntid | `nvvm.reqntid` | 1..3 `i32` values | `.reqntid` | all SMs |
| cluster_dim | `nvvm.cluster_dim` | exactly 3 `i32` values | `.explicitcluster`, `.reqnctapercluster` | SM90+ |
| minctasm | `nvvm.minctasm` | integer | `.minnctapersm` | all SMs |
| maxnreg | `nvvm.maxnreg` | integer | `.maxnreg` | all SMs |
| maxclusterrank | `nvvm.maxclusterrank` | integer | `.maxclusterrank` | SM90+ |
| blocksareclusters | `nvvm.blocksareclusters` | UnitAttr | `.blocksareclusters` | SM90+ |
| grid_constant | `nvvm.grid_constant` | 1-based argument index list | Drives constant-argument layout | all SMs |
| annotations_transplanted | `nvvm.annotations_transplanted` | UnitAttr | Internal marker only | all SMs |

Several invariants are core for reimplementers. `nvvm.maxclusterrank` is
stored as an integer-valued function attribute, unlike the string-shaped
legacy forms used by some older launch metadata. `local_maxnreg` has no
new `nvvm.*` mirror — it stays legacy-only and is never printed as a PTX
directive by this stage. When updating dimensional attributes, write
every axis back together so the new attribute form stays coherent even
when the legacy source used split per-axis tuples.

PTX emission walks the verified attribute set in a fixed order so that
related directives stay adjacent in the kernel header. The thread-shape
group (`.maxntid`, `.reqntid`) emits first, followed by the residency
hints (`.minnctapersm`, `.maxnreg`), and finally the cluster group
(`.blocksareclusters`, `.explicitcluster`, `.reqnctapercluster`,
`.maxclusterrank`) when the target supports clusters. Both `.maxntid`
and `.reqntid` may appear on the same kernel — the PTX semantics make
them complementary: `.reqntid` declares an exact block shape the kernel
relies on, `.maxntid` declares an upper bound for register-pressure
budgeting. The verifier checks shape consistency but does not collapse
or override either directive, and the emitter prints both as written
when both are set.

```c
void emit_launch_directives(LLVMFuncOp fn, Target target, PTXWriter &out) {
    if (auto dims = get_dim_attr(fn, "nvvm.maxntid"))
        out.directive(".maxntid", *dims);
    if (auto dims = get_dim_attr(fn, "nvvm.reqntid"))
        out.directive(".reqntid", *dims);

    if (auto n = get_int_attr(fn, "nvvm.minctasm"))
        out.directive(".minnctapersm", *n);
    if (auto n = get_int_attr(fn, "nvvm.maxnreg"))
        out.directive(".maxnreg", *n);

    if (!target_supports_clusters(target))
        return;                              // suppress all cluster directives pre-SM90

    if (fn->hasAttr("nvvm.blocksareclusters"))
        out.directive(".blocksareclusters");
    if (auto dims = get_dim_attr(fn, "nvvm.cluster_dim")) {
        out.directive(".explicitcluster");
        out.directive(".reqnctapercluster", *dims);
    }
    if (auto n = get_int_attr(fn, "nvvm.maxclusterrank"))
        out.directive(".maxclusterrank", *n);
}
```

Two structural invariants keep this loop from being more complex.
`nvvm.blocksareclusters` is verified to require both `nvvm.reqntid` and
`nvvm.cluster_dim` on the same function, so by the time emission runs
the three directives are guaranteed to form a coherent triple. Cluster
directives are suppressed wholesale on pre-SM90 targets; the verifier
permits the attributes upstream so a single IR module can lower for
multiple targets, but the per-target emitter refuses to print them when
ptxas would reject the result.

## How tileiras chooses each directive

Verifying an attribute is well-formed is not the same as choosing its
value. The well-formedness rules above guard against malformed PTX;
the choice of value is what determines whether the kernel runs at all
and how fast it runs when it does. Each directive has its own input
channel — the kernel-spec attribute the upstream lowering attaches, a
user-supplied annotation that survives the front-end, or a constraint
imposed by an instruction the compiler emitted later. The table below
walks the policy for each directive.

| Directive | Primary input | Policy |
| --- | --- | --- |
| `.entry kernel_name` | `nvvm.kernel` marker on the LLVM function | always emit when the marker is present; the function name is the symbol the cubin exposes |
| `.maxntid X, Y, Z` | upper-bound hint from kernel-spec or DSL annotation | emitted when the bound is not also a hard contract; lets ptxas size the register fragment without pinning the launch shape |
| `.reqntid X, Y, Z` | hard contract from kernel-spec — warp-specialized split, warp-group requirement, or named-warp partition | emitted when the lowering depends on an exact thread count (every WGMMA or tcgen05 user, every kernel with named producer/consumer warps) |
| `.minnctapersm N` | occupancy floor from kernel-spec | emitted when the user requested a minimum residency, usually for kernels whose throughput is sensitive to warp-scheduler latency hiding |
| `.maxnreg N` | per-thread register budget from kernel-spec | emitted to let ptxas trade registers for occupancy — typical values come from a kernel-specific computation of `accumulator_regs + working_regs + slack` |
| `.explicitcluster` | implied by `nvvm.cluster_dim` presence | always emitted with `.reqnctapercluster` when the kernel is cluster-shaped on SM90+ |
| `.reqnctapercluster X, Y, Z` | cluster shape from kernel-spec | emitted on SM90+ when `nvvm.cluster_dim` is present; suppressed wholesale on older targets |
| `.maxclusterrank N` | portability cap from kernel-spec | emitted on SM90+ when the user wants a portable launch shape, capping cluster size below the device-specific maximum |
| `.blocksareclusters` | only legal when `.reqntid` and `.cluster_dim` are also present | emitted on SM90+ for kernels that opt into the single-CTA-cluster convention; lets cluster-aware code paths execute on a degenerate cluster shape |

The `.maxntid` versus `.reqntid` distinction is the policy decision
that affects the most kernels. `.maxntid` is an upper bound the launch
must respect but does not have to saturate; the driver accepts any
launch shape with X, Y, Z components no larger than the declared
maxima. `.reqntid` is a hard contract — the driver rejects any launch
whose block shape does not match the declared values exactly. Tileiras
emits `.reqntid` whenever the lowering has already baked in a specific
thread count: any kernel that emits WGMMA needs 128 threads per CTA
(four warps form one warp group), any kernel with warp-specialized
producer/consumer splits needs the exact named warp count, and any
kernel with named-warp NamedBarrier slots needs the exact thread count
the slot binding assumed. For kernels that adapt to launch shape —
elementwise kernels, kernels that use only synchronous `mma.sync`
forms, kernels with no warp specialization — tileiras emits only
`.maxntid` so the same cubin works for a range of launch shapes.

The `.maxnreg` choice is similarly central to performance. A
WGMMA-using kernel must leave room for the accumulator fragment: an
`m64n256k16` FP32 WGMMA needs 32 FP32 registers per thread just for
the accumulator, plus the working set for descriptors, loop indices,
and any other live values. Setting `.maxnreg` too low forces ptxas to
spill the accumulator to local memory, which silently regresses
throughput by an order of magnitude. Setting it too high reduces
occupancy and hurts latency hiding. The kernel-spec carries the
result of a per-kernel computation that balances both — usually
`accumulator_regs + descriptor_regs + slack`, with `slack` calibrated
to the SM's register file size and the desired CTAs per SM.

Cluster-shape directives are an all-or-nothing group. When the
kernel-spec carries `nvvm.cluster_dim`, the lowering emits
`.explicitcluster`, `.reqnctapercluster`, and any `nvvm.maxclusterrank`
or `nvvm.blocksareclusters` markers; when the spec is silent, no
cluster directive is emitted. The verifier rule that
`nvvm.blocksareclusters` requires both `nvvm.reqntid` and
`nvvm.cluster_dim` means the three-directive triple is always coherent
by the time the emitter sees it.

[GPU Execution Model](../topics/gpu-execution-model.md) is the
canonical reference for how the five tiers (thread, warp, CTA,
cluster, grid) consume these directives at runtime, with a worked
example that traces the directive emission from kernel-spec to PTX
header for a Hopper GEMM.

## ptxas Knobs File Format

When both `MLIR_ENABLE_EVO` and `PTX_KNOBS_PATH` are set, `tileiras`
forwards `--knobs-file=<path>` to ptxas. It does not parse or validate the
file — the grammar belongs to ptxas.

The file format is:

```text
arbitrary preamble
[knobs]
command command command
```

The `[knobs]` sentinel is case-sensitive; text before it is ignored.
After the sentinel, whitespace, `~`, and `;;` separate commands. The
command stream has no quoting, no escaping, no comment syntax.

Commands have three forms:

| Form | Meaning |
| --- | --- |
| `identifier=value` | Assign a knob value. The `=` is accepted but not always required by ptxas. |
| `WHEN ...` | Parse a conditional knob clause. |
| `INJECTSTRING ... ;;` | Parse an internal SASS-splice string terminated by `;;`. |

Values parse per the knob descriptor type. The recovered parser accepts
signed and unsigned integers, integer ranges, integer lists, 32-bit and
64-bit floats, strings, pointers, opcode lists, opcode-pair lists, and
`WHEN` clauses. Integer parsing is decimal — a string like `0x10` parses
as zero, with the trailing text ignored by the numeric conversion path.

Malformed knob files are fatal to the ptxas child process. Duplicate
assignments follow a last-wins policy: the later command overwrites the
earlier runtime value. Identifier matching is case-insensitive from the
user's point of view.

`tileiras` runs no preflight check that the path exists, contains
`[knobs]`, or uses valid identifiers. Every knob-file diagnostic comes
from ptxas and surfaces through the normal subprocess diagnostic buffer.

## Related pages

[Driver Overview](overview.md#driver-flow) covers how the produced kernel
directives travel into the relocatable object;
[Driver CLI Options](cli-options.md#pipeline-options) catalogues the
user-visible flags that map into pipeline options;
[ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md#knob-file-format)
documents the ptxas-side knob-file grammar in detail;
[Attribute System and Lowering](../topics/attribute-system-and-lowering.md)
documents the full lifecycle of each launch-shape attribute from frontend
hint through `nvvm.*` directive carrier, including which transitions
silently drop the fact and produce a degraded kernel.
