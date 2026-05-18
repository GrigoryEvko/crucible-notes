# Frontend Contract and Tile IR Emission

## Abstract

Tileiras consumes Tile IR bytecode; producing that bytecode is a frontend's
responsibility. A conformant frontend follows three conventions: emit operations
in the `cuda_tile` dialect with the documented operand and attribute structure,
attach module-level kernel-launch metadata that survives every subsequent
lowering, and serialize using MLIR bytecode under the tileiras-flavored
attribute-tag wire format. This page is the producer-facing contract: kernel
signature rules, op-construction conventions, attribute requirements,
bytecode-format constraints, and the common emission mistakes that produce
modules tileiras rejects.

The contract is documented from the consumer's perspective. Every rule below
corresponds to a check that fires somewhere in the bytecode reader, the
`cuda_tile` verifier, the `ConvertCudaTileToTileAA` conversion target, or the
downstream kernel-spec lookup. A frontend that satisfies all four boundaries
produces bytecode that flows through the entire 53-pass pipeline without
producer-visible diagnostics.

## Who Produces Tile IR

Tile IR is an open input format. Any compiler that wants to target the tile
pipeline can build a frontend; tileiras only inspects the bytecode buffer it
receives, not the producer that wrote it.

| Producer | Surface | Status |
|---|---|---|
| NVIDIA's Triton-style frontend | High-level Python DSL with `tt.*` kernel attributes | Primary producer; sets the de facto attribute conventions |
| CUTLASS DSL frontend | Python DSL that emits `cuda_tile` directly through MLIR Python bindings | Targets the same bytecode container with the same attribute names |
| `mlir-translate` with a tileiras-aware bytecode writer | Textual `cuda_tile` IR plus the tileiras `AttrTag` numbering | Practical for hermetic tests and small reproducers; requires the tileiras-flavored writer rather than stock upstream |
| Hand-rolled bytecode emitters | Direct LEB128 record construction against the wire format documented in [MLIR Bytecode Format](../bytecode/mlir-bc-format.md) | Used for differential testing and bug reduction; only viable when the producer freezes the tileiras tag table |

The producer set is open in both directions: tileiras has no allowlist of
signing frontends, and the public bytecode contract has no producer-identity
field. The only invariants it checks are the bytecode envelope, the dialect
list, and the per-op verifier rules.

## The Kernel Signature Contract

A frontend's first job is producing a `cuda_tile.entry` operation whose
signature, attributes, and body region match the public dialect contract. The
verifier checks the structure; the kernel-attribute attachment step in
`ConvertTileAAToTileAS` reads the attributes; the function-boundary lowering in
`ConvertTileFuncToLLVM` projects the attributes onto `nvvm.*` directives.

### Required Module Shell

Every conformant module looks like this at the top level:

```mlir
module attributes {
    nv_tileaa.compute_capability = 90 : i32,    // sm_90
    tt.num_warps = 4 : i32,                     // four warps per CTA
    tt.num_ctas = 1 : i32                       // single-CTA cluster
} {
  cuda_tile.module {
    cuda_tile.entry @gemm(%A: !cuda_tile.ptr<f16>, ...) {
      ...
      cuda_tile.return
    }
  }
}
```

The outer `builtin.module` carries the kernel-launch attributes; the inner
`cuda_tile.module` holds one or more `cuda_tile.entry` operations whose bodies
contain the kernel logic.

### Kernel-Function Requirements

An entry function must satisfy four structural rules. The verifier in
[cuda_tile Verifiers](../dialects/cuda_tile/verifiers.md) catches all four
before the conversion target rejects the operation.

| Rule | Verifier check | Producer responsibility |
|---|---|---|
| Operation is `cuda_tile.entry` (not `func.func`) | The dialect declares `entry` as the kernel constructor; arbitrary `func.func` ops are not recognized as kernels by the downstream lowering | Frontend must construct `cuda_tile.entry`, not lift to `func.func` |
| Body terminates with `cuda_tile.return` | Region terminator must be the matching return op for the entry | No raw `func.return` allowed in the entry body |
| Arguments use `cuda_tile.ptr`, `cuda_tile.tensor_view`, `cuda_tile.partition_view`, or scalar types | The type converter only knows how to lift these | A frontend that passes a raw `!llvm.ptr` argument fails at type conversion |
| No view-typed return values | The verifier rejects view results across structured-control-flow boundaries | Views are produced from arguments inside the kernel, not returned out |

The `cuda_tile.entry` op is distinct from `func.func` by design. It carries the
region in which the kernel-private structured control flow lives, and the
downstream lowering can identify the entry without a separate annotation walk.
A frontend that tries to lift the body into `func.func` and tag it with a
custom unit attribute will not produce a kernel; the resulting function emits
`.func` rather than `.entry` and is invisible to the CUDA driver at launch
time.

### Compute-Capability Attribute

`nv_tileaa.compute_capability` is the single attribute the frontend must attach
to choose a target. Its absence is fatal at `ConvertTileFuncToLLVM`: the pass
emits `"Failed to get ComputeCapability"` through severity 259/0x103 and aborts
the module. The same encoding rule applies everywhere — the integer is the
two-digit form `major * 10 + minor`.

| Compute capability | Encoded integer | SM string |
|---|---:|---|
| sm_70 | 70 | `"sm_70"` |
| sm_80 | 80 | `"sm_80"` |
| sm_89 | 89 | `"sm_89"` |
| sm_90 (Hopper) | 90 | `"sm_90"` |
| sm_100 (Blackwell datacenter) | 100 | `"sm_100"` |
| sm_103 (Blackwell Ultra) | 103 | `"sm_103"` |
| sm_120 (consumer Blackwell) | 120 | `"sm_120"` |

The driver passes `--gpu-name=sm_<NN>` on the command line; the conversion pass
reads the `--compute-capability` option (`major * 10 + minor` integer) and
writes the attribute onto the module. A frontend that emits bytecode without
this attribute must rely on the driver to inject it from `--gpu-name`; a
frontend that emits the attribute itself short-circuits the option lookup.

The fallback `nv_tileaa.target_spec` (a `StringAttr` of the form `"sm_XX"`) is
read when the integer attribute is absent. The two spellings convert to one
logical concept; new IR should prefer the canonical underscore form
`compute_capability`.

### Kernel-Launch Attributes

The `tt.*` namespace is the de facto convention for kernel-launch attributes
attached at the module level. They flow through `ConvertCudaTileToTileAA`
verbatim (no rename) and are folded into `nv_tileaa.kernel_spec` during
`attachKernelSpecAttributes`. The kernel-spec record is then read by the
scheduler, the agent-switch builder, and the function-boundary lowering, which
projects it onto `nvvm.*` function attributes the AsmPrinter emits as PTX
directives.

> ⚡ **QUIRK — Triton's `tt.*` prefix is the project-neutral compiler's de-facto schema**
> Tileiras is a project-neutral CUDA tile compiler, but its kernel-launch contract reads attributes under the `tt.*` namespace — the prefix Triton uses for its own frontend. `tt.num_warps`, `tt.num_ctas`, `tt.cluster_dim`, and `tt.num_stages` flow through `ConvertCudaTileToTileAA` with no rename and land in `nv_tileaa.kernel_spec` unchanged. A frontend that uses a clean per-project namespace (say `myfrontend.num_warps`) gets the silent defaults instead — 4 warps, 1 CTA, cluster `[1,1,1]` — and the scheduler emits a kernel sized for a single warp group with no warning that the producer's intent was ignored.

| Attribute | Default if absent | Projected PTX directive |
|---|---|---|
| `tt.num_warps = N : i32` | 4 | `.reqntid (32*N), 1, 1` |
| `tt.num_ctas = N : i32` | 1 | (drives cluster directive emission) |
| `tt.cluster_dim` (3-element i32 array) | `[1, 1, 1]` | `.reqnctapercluster X, Y, Z` on SM90+ |
| `tt.num_stages = N : i32` | scheduler default | (consumed by modulo scheduler, no direct PTX) |

A frontend may attach additional implementation-specific attributes under its
own namespace; they survive every lowering stage that does not actively rewrite
them and are dropped if no consumer reads them. The recommended practice is to
keep producer-internal attributes prefix-namespaced so they cannot collide with
the consumer-visible ones.

### Optional NVVM Directive Hints

A frontend can ask for a tighter register cap or a per-SM occupancy floor by
attaching `nvvm.*` attributes directly to the kernel function. These bypass the
kernel-spec mirror and reach the AsmPrinter unchanged.

| Attribute | Type | PTX directive | Use case |
|---|---|---|---|
| `nvvm.maxnreg = N : i32` | `IntegerAttr<i32>` | `.maxnreg N` | Bound per-thread register usage so ptxas can trade registers for occupancy |
| `nvvm.minctasm = N : i32` | `IntegerAttr<i32>` | `.minnctapersm N` | Request a minimum occupancy floor |
| `nvvm.maxclusterrank = N : i32` | `IntegerAttr<i32>` | `.maxclusterrank N` | Portability cap on cluster size |
| `nvvm.blocksareclusters` | `UnitAttr` | `.blocksareclusters` | Treat every CTA as its own cluster (legal only with `cluster_dim = (1, 1, 1)`) |

These attributes are optional. The kernel-spec path is the primary mechanism;
direct `nvvm.*` attributes are for cases where the frontend already knows the
exact directive value and wants to skip the mirror step. Mixing the two is
legal — the function-boundary lowering writes `nvvm.*` attributes derived from
the kernel-spec only when they are not already present.

## Op-Construction Conventions

The 92-op `cuda_tile` roster (see [Operation Roster](../dialects/cuda_tile/op-roster.md))
divides into families with consistent operand-order and attribute conventions.
A frontend that follows the family conventions produces IR that satisfies the
verifier on first construction; a frontend that improvises operand orders
triggers verbatim diagnostics keyed off the `operandSegmentSizes` arrays the
verifier consults.

### Token-Ordered Memory Operations

Every memory-side-effect op carries a token chain. The convention for
constructing a load is:

```mlir
%value, %tok_out = cuda_tile.load_view_tko %view[%i, %j], %mask, %fallback, %tok_in
    { mem_semantic = #cuda_tile<mem_semantic relaxed>,
      mem_scope    = #cuda_tile<mem_scope gpu>,
      operandSegmentSizes = array<i32: 1, 2, 1, 1, 1> }
    : !cuda_tile.partition_view<128x64xf32>, index, index,
      tile<128x64xi1>, tile<128x64xf32>, !cuda_tile.token
    -> tile<128x64xf32>, !cuda_tile.token
```

The operand order is fixed by the family: `(view, indices..., mask, fallback,
token_in)` for views; `(ptr, indices..., mask, fallback, token_in)` for raw
pointers. The `operandSegmentSizes` array partitions the operand list into the
five slots `{view_or_ptr, indices, mask, fallback, token}` and is the
verifier's primary structural check.

Three structural rules govern token construction:

- **Every memory op consumes one input token and produces one output token.**
  A frontend that leaves the token unthreaded breaks the dataflow representation
  of memory ordering. Stores produce a token but no data; loads and atomics
  produce both.
- **A `make_token` op at function entry seeds the chain.** Use it once per
  independent ordering chain; multiple independent loads that can reorder use
  separate chains, multiple ordered loads thread the same chain.
- **`join_tokens` merges two chains.** When two independent chains both need
  to feed into a later store, join them and pass the result through.

The `_tko` suffix marks the family, but it is also the verifier's keyword:
omitting the suffix produces an unknown-op diagnostic during bytecode read.

### MMA Operations

Matrix multiply-accumulate operations follow the `(A, B, C) -> D` convention
where C is the accumulator-in and D is the accumulator-out. The same SSA value
is permitted to flow through both — that is the common pattern when an MMA
runs inside a K-loop. The shape contract is enforced at construction:

| Op | A shape | B shape | C/D shape | Required attributes |
|---|---|---|---|---|
| `mmaf` (floating) | `tile<[B ×] M × K × elem_a>` | `tile<[B ×] K × N × elem_b>` | `tile<[B ×] M × N × elem_c>` | optional `rounding` |
| `mmai` (integer) | same | same | same | required `signedness_a`, `signedness_b` |

The K dimension is contracted (must agree between A and B); the M and N
dimensions are the output extents (must agree between A/B and C/D). The
batched form takes rank-3 tile types with a shared leading batch dimension; the
verifier rejects any rank disagreement, K-dimension mismatch, or accumulator/
result type divergence.

The MMA atom (WGMMA / `tcgen05.mma` / `mma.sync`) is *not* selected by the
frontend. It is the lowering pipeline's job to pick the right atom for the
target. A frontend that tries to pick a specific atom must do so through an
optimization hint (op-level attribute under `optimization_hints`), not by
constructing a different op.

### Reductions and Scans

The `reduce` and `scan` ops carry a region with a pure combiner body. The
convention is `(input, identity) -> result` with the combiner taking two
block arguments of the input element type.

```mlir
%sum = cuda_tile.reduce %a, %identity { axis = 1 : i32 } : tile<8x16xf32>
    -> tile<8xf32> {
  ^bb0(%lhs: f32, %rhs: f32):
    %r = cuda_tile.addf %lhs, %rhs : f32
    cuda_tile.yield %r : f32
}
```

The body must be a pure region — no side-effecting ops, no token-ordered
memory ops, no view operations. Element-type identity in the combiner must
match the input element type; rank-zero block arguments are mandatory. The
verifier rejects each violation with a verbatim diagnostic that names the rule
that fired.

### Async Pipeline Ops

Async-pipeline ops are emitted by the scheduler in `nv_tileas`, not by the
frontend. A frontend that wants explicit pipeline staging communicates the
intent through the module-level `tt.num_stages` hint and lets the modulo
scheduler in [Modulo Scheduler and Rau](../scheduler/modulo-scheduler-and-rau.md)
turn it into producer/consumer agents during lowering. A frontend that emits
`nv_tileas.async.pipeline.*` ops directly bypasses the verifier — `nv_tileas`
is not legal at the bytecode boundary.

## Required vs Optional Attributes

A single table summarises every attribute the frontend must, may, or must not
attach to a conformant `cuda_tile` module. "Required" means the lowering fails
without it; "optional" means a sensible default applies; "advisory" means the
attribute is read if present but has no failure path when absent.

| Attribute | Carrier | Status | Default if absent |
|---|---|---|---|
| `nv_tileaa.compute_capability` | Module | Required | `"Failed to get ComputeCapability"` (`O3`); driver-supplied from `--gpu-name` |
| `nv_tileaa.target_spec` | Module | Fallback | Used when `compute_capability` is absent |
| `tt.num_warps` | Module | Optional | `4` (1 warp group) |
| `tt.num_ctas` | Module | Optional | `1` (single-CTA cluster) |
| `tt.cluster_dim` | Module | Optional | `[1, 1, 1]` |
| `tt.num_stages` | Module | Advisory | Scheduler default |
| `nvvm.maxnreg` | Function | Optional | No cap; ptxas chooses |
| `nvvm.minctasm` | Function | Optional | No occupancy floor |
| `nvvm.maxclusterrank` | Function | Optional | No portability cap |
| `nvvm.blocksareclusters` | Function | Optional | Off |
| `nvvm.kernel` | Function | Synthesized | Attached by downstream rewrite; frontend should not emit |
| `nv_tileaa.kernel_spec` | Function | Synthesized | Attached by `attachKernelSpecAttributes` from `tt.*` hints |
| `nv_tileaa.occupancy` | Function | Optional | No `nvvm.maxnreg` synthesized |
| Per-op `mem_semantic`, `mem_scope` | Op | Conditional | `weak`/CTA when absent; required for non-weak orderings |
| Per-op `fastmath` | Op | Optional | No fast-math flags |
| Per-op `optimization_hints` | Op | Advisory | No hint applied |
| Per-op `operandSegmentSizes` | Op | Required | Verifier emits structural error when absent on multi-operand-family ops |

The cross-cutting policy for every attribute family — which stages drop,
preserve, synthesize, or read each one — is documented in [Attribute System and
Lowering](attribute-system-and-lowering.md).

## Bytecode-Format Constraints

The wire format is not stock MLIR bytecode. A frontend that constructs a valid
`cuda_tile` module in memory still has to clear the bytecode envelope and the
attribute-tag numbering before tileiras can read the buffer.

### Magic and Version

Every conformant container opens with the eight-byte magic and a three-VarInt
Tile-version triple. The magic and version constants are documented at
[MLIR Bytecode Format — Header Parser](../bytecode/mlir-bc-format.md#header-parser-sub_5838a0).
The accepted version range is `13.1.x` only; the parser emits an
`"unsupported Tile version ..."` diagnostic for everything else.

```text
7f 54 69 6c 65 49 52 00    // "\x7fTileIR\0"
0d 01 00                   // VarInt 13, VarInt 1, VarInt 0 (Tile 13.1.0)
```

Upstream MLIR fills the eighth magic byte with the start of `"\nMLIR"`. A
producer that uses an unmodified upstream writer emits `0x0A` in that slot, and
the tileiras reader rejects the buffer with `"invalid magic number at position
7"`. The driver also surfaces this case with the diagnostic `"input does not
correspond to Tile IR bytecode (it looks like MLIR bytecode instead)"`.

### Dialect List

The envelope's dialect list must include `cuda_tile`. A `builtin` entry is
synthesized automatically by the MLIR infrastructure. Other dialects are legal
only if they appear in the registered set and the frontend actually uses them:
`arith` for constants whose representation is not a `cuda_tile.constant`,
`func` for symbol references, and the optional debug-info dialects.

A module that lists `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, `cutlass`,
`nvgpu`, `nvvm`, or `llvm` in its dialect list is rejected by the
conversion-target legality check: those are internal lowering dialects, not
public input. The diagnostic spelling is `"unregistered dialect: <name>"` from
the dialect-list walker.

### AttrTag Wire Format

The 13-entry attribute-tag table inside the bytecode reader is the
single-largest wire-format-breaking divergence from upstream. The shipped
numbering is documented in [Self-Contained Attribute Dispatch](../bytecode/mlir-bc-format.md#self-contained-attribute-dispatch);
the key differences are:

| Tag | tileiras meaning | Upstream MLIR meaning |
|---|---|---|
| 1 | `StringAttr` | `IntegerAttr` |
| 4 | `DenseElementsAttr` | `TypeAttr` |
| 5 | `DenseElementsAttr<string>` | `StringAttr` |
| 13 | `AssumePredicateAttr` | (undefined) |

A producer that writes attributes through stock `mlir-translate
--serialize-bytecode` emits the upstream numbering and the tileiras reader
decodes every attribute incorrectly — usually surfacing as garbled type
mismatches mid-IR rather than envelope errors. The practical implication is
that **a frontend cannot use upstream `mlir-translate` directly**; it must
either link the tileiras-aware writer or fork the upstream AttrTag table.

### Canonical VarInt Encoding

Every multi-byte integer in the container uses the LEB128 variant documented in
[VarInt Encoding](../bytecode/mlir-bc-format.md#varint-encoding). Producers
must emit the canonical (shortest) encoding for every integer; an overlong
encoding decodes to the same value but is rejected with `"non-canonical
VarInt"` and the section fails. The writer-side rule is straightforward: count
leading zero bytes in the integer's two's-complement form, pick the shortest
encoding that fits, never zero-pad for alignment.

### Section Ordering

Sections must be present in dependency order. The reader's walker assumes that
later sections can index into earlier ones, so a producer that writes the
sections out-of-order fails the cross-section index validation, not the
section-walker. The required order is documented in [Section Walker
Algorithm](../bytecode/mlir-bc-format.md#section-walker-algorithm). The minimum
set for a `cuda_tile` module is string, type, attribute/constant, IR (func and
global), and the end marker. Resource and debug sections are optional.

## Common Pitfalls

Most frontend bugs are well-formed bytecode that tileiras refuses for one of a
small set of repeatable reasons. The diagnostics are verbatim from the reader
and the verifier; the root causes are producer-side.

### Missing Kernel Marker

**Symptom.** The kernel compiles, ptxas accepts the PTX, but the resulting
cubin exposes no entry symbol for the CUDA driver to launch.

**Cause.** The frontend wrote a `func.func` instead of `cuda_tile.entry`, or
the downstream `cute.kernel`-to-`nvvm.kernel` rewrite did not fire because the
function never picked up the `cute.kernel` marker. The directive emitter wrote
`.func` rather than `.entry` because no kernel-spec attached and no
`nvvm.kernel` was present.

**Fix.** Emit `cuda_tile.entry` for every kernel. Do not lift to `func.func`
before the bytecode boundary; the dialect's structured-control-flow surface
covers everything a kernel body needs.

### Wrong AttrTag Numbering

**Symptom.** The bytecode parser emits `"unknown attribute tag <N>"` mid-IR,
or — more confusingly — successfully decodes the file but produces a module
whose attribute types are systematically off by one slot.

**Cause.** The writer used stock upstream `AttrTag` numbering. Tag 1 wrote an
`IntegerAttr` (upstream) where the tileiras reader expected a `StringAttr`;
tag 5 wrote a `StringAttr` where the reader expected a
`DenseElementsAttr<string>`.

**Fix.** Use a tileiras-aware writer. The producer-side AttrTag table is frozen
to the values the reader uses; encoding through any other table produces an
unreadable buffer regardless of in-memory correctness.

### Missing Compute Capability

**Symptom.** `"Failed to get ComputeCapability"` (`O3`) or
`"failed to get compute capability."` (`O2`) at lowering time, depending on which pass first observes the missing attribute.

**Cause.** Neither `nv_tileaa.compute_capability` nor `nv_tileaa.target_spec`
attached to the module. The driver's `--gpu-name=sm_<NN>` option is the
intended injection point; the frontend may skip the attribute and rely on the
driver, but a module produced without the attribute is not portable across
drivers that do not inject one.

**Fix.** Attach `nv_tileaa.compute_capability = N : i32` at the module level
when the frontend knows the target, or document the requirement that the
caller pass `--gpu-name`.

### Wrong Operand Order on Token-Ordered Ops

**Symptom.** The verifier emits `"expected token operand"` or a structural
error keyed on `operandSegmentSizes`.

**Cause.** The frontend placed the token operand at a non-canonical position,
or omitted `operandSegmentSizes`. The verifier reconstructs the operand
partition from the array; without it, the multi-operand families fail
structural validation.

**Fix.** Follow the operand-order convention in [Operation Roster](../dialects/cuda_tile/op-roster.md):
view/ptr first, indices, optional mask, optional fallback, token last. Always
emit `operandSegmentSizes` as a five-element `array<i32>` for the
load/store/atomic families.

### `tile<...>` vs `tensor<...>` Type Confusion

**Symptom.** The first-stage type converter fails with an unexpected type
diagnostic, or a downstream pattern fails to match.

**Cause.** A frontend that ported from a tensor-typed IR may have lifted shape
operations to `tensor<>` rather than `cuda_tile.tile<>`. The two types have
different verifier contracts: `cuda_tile.tile` enforces power-of-two dimensions
and a 16-million-element ceiling, while `tensor<>` has neither check.

**Fix.** Construct `cuda_tile.tile<...>` for every shaped value in the kernel
body. Tensor types appear in the IR only after `ConvertCudaTileToTileAA` lifts
tiles to tensors during the alias-aware stage.

### Returning a View

**Symptom.** Verifier emits `"view-typed result rejected"` from `cuda_tile.if`
or `cuda_tile.for`.

**Cause.** View types are not first-class results of structured-control-flow
operations. The intended pattern is to construct the view inside the region
and consume it directly, not to return it across the region boundary.

**Fix.** Construct views close to where they are consumed; if conditional view
construction is necessary, branch around the consuming load rather than
yielding a view from `cuda_tile.if`.

### Power-of-Two Tile Dimensions

**Symptom.** `"tile dimensions must be powers of two"` at type construction.

**Cause.** A tile shape includes a non-power-of-two dimension. The shape
verifier walks each tile type and rejects any dimension that is not `2^k` for
some non-negative `k`. The element-count ceiling fires later: products above
16 million elements are rejected with `"tile would exceed the maximum element
count"`.

**Fix.** Round tile shapes up to the next power of two and use masking for the
ragged region. Frontends that target non-power-of-two problem sizes typically
tile around an oversized power-of-two block and predicate the tail.

### Unregistered Dialect

**Symptom.** `"unregistered dialect: <name>"` from the dialect-list walker.

**Cause.** The frontend declared an internal lowering dialect (`nv_tileaa`,
`nv_tileas`, `cute`, etc.) in its bytecode envelope. These dialects are not
public input — they are produced inside tileiras and are illegal at the
bytecode boundary.

**Fix.** Restrict the dialect list to `cuda_tile`, `builtin`, `arith`, `func`,
and the debug-info dialects. Construct everything else through `cuda_tile`'s
own operation surface.

## Minimal Hand-Rolled Kernel

For testing, hermetic builds, or differential reduction, a frontend can
hand-construct a tiny kernel as textual MLIR and run it through a
tileiras-aware writer. The minimum is one entry function with one return:

```mlir
module attributes {
    nv_tileaa.compute_capability = 90 : i32,
    tt.num_warps = 4 : i32
} {
  cuda_tile.module {
    cuda_tile.entry @noop() {
      cuda_tile.return
    }
  }
}
```

Serialized through a tileiras-aware writer, this produces a 256-byte buffer
that flows through the entire pipeline and emits a `.entry noop` PTX function
with the four expected directive lines (`.entry`, `.reqntid 128, 1, 1`,
`.maxnreg` if set, and the parameter block). It is the smallest input that
exercises every stage of the cascade and is the canonical reduction target for
producer-side bugs.

## Triton-Frontend Extensions

NVIDIA's Triton-style frontend extends the contract with a handful of
domain-specific module attributes. They follow the same convention as the
documented `tt.*` attributes — module-level, integer-or-array values, read
once by `attachKernelSpecAttributes` and folded into the `nv_tileaa.kernel_spec`
record on each entry function.

| Triton attribute | Effect | Lowering site |
|---|---|---|
| `tt.num_stages = N : i32` | Hint to the modulo scheduler about pipeline depth | [Async/Pipeline Family](../passes/tileas/async-pipeline-family.md) |
| `tt.cluster_size = [X, Y, Z]` | Shorthand for `tt.cluster_dim` plus `tt.num_ctas` | [CTA Cluster Family](../passes/tileas/cta-cluster-family.md) |
| `tt.is_persistent` | Mark the kernel as persistent for the StaticPersistent scheduler | [Pipeline and Tile Scheduler](../dialects/cutlass/pipeline-and-tile-scheduler.md) |
| `tt.dump_intermediate` | Producer-side debugging hint (informational only) | (no consumer) |

These are not part of the canonical contract — a non-Triton frontend can ignore
them entirely — but they are stable enough that downstream consumers can rely
on them when they are present. See [cuda_tile Overview](../dialects/cuda_tile/overview.md)
for the public dialect surface they map onto and [Attribute System and
Lowering](attribute-system-and-lowering.md) for the lifecycle that each one
follows from the module dictionary to the PTX directive emitter.

## Cross-References

[cuda_tile Overview](../dialects/cuda_tile/overview.md) documents the public
dialect surface this contract targets. [Operation Roster](../dialects/cuda_tile/op-roster.md)
catalogues every legal mnemonic and operand-order convention. [Types and
Attributes](../dialects/cuda_tile/types-and-attrs.md) covers the type-storage
parameters and attribute parse contract. [Verifiers](../dialects/cuda_tile/verifiers.md)
documents the verifier diagnostics this page references by spelling.
[MLIR Bytecode Format](../bytecode/mlir-bc-format.md) is the wire-format
reference; [Dialect Bytecode Reader/Writer Status](../bytecode/dialect-readers-status.md)
explains why only `cuda_tile` has a linked reader. [Position in nvcc 13.1
Toolchain](../boundaries/nvcc-13-1-position.md) shows where the frontend's
bytecode artifact lands in the larger build. [Attribute System and
Lowering](attribute-system-and-lowering.md) is the cross-stage policy reference
for every attribute discussed above; [GPU Execution Model](gpu-execution-model.md)
walks the same kernel-attribute story from the perspective of PTX directive
emission. [DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) traces a single
kernel through every stage of the pipeline from this contract down to PTX
text.
