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

[Driver Overview](overview.md) covers how the produced kernel directives
travel into the relocatable object; [Driver CLI Options](cli-options.md)
catalogues the user-visible flags that map into pipeline options; the
PTX emission pages under the lowering section explain how each `.entry`
header is serialised once attribute walking finishes.
