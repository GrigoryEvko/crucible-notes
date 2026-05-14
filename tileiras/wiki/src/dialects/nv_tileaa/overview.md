# nv_tileaa Dialect Overview

`nv_tileaa` exists only inside the `tileiras` binary. There is no
open-source counterpart: no header, no TableGen file, no entry in any
public NVIDIA component ships under this name. In the lowering cascade
it sits one step below the user-facing `cuda_tile` dialect and one step
above the assembler-near `nv_tileas` dialect, and its job is to expose
the alias and memory-space information that the lower tiers need before
they commit to layouts and TMA descriptors. The "aa" stands for
*alias-aware*: this dialect reifies buffer lifetime, pointer arithmetic,
and tile provenance as first-class IR ops, so downstream passes can run
ordinary dataflow analyses over them.

## Purpose

The dialect bridges two very different worlds. Above it, `cuda_tile`
speaks in user-level terms — tiles, dot products, reductions, control
flow. Below it, `nv_tileas` already commits to async pipelines, TMA
descriptors, and per-agent register budgets. `nv_tileaa` is what fits
between them. It keeps the high-level operation set largely intact
(`addf`, `dot`, `reduce`, `scan`, `broadcast`, `extract_slice`) and
layers on three orthogonal kinds of structural information the upper
dialect lacks: explicit pointer arithmetic (`addptr`, `int_to_ptr`,
`ptr_to_int`, `make_memref`), explicit memory-token lifetimes
(`create_mem_token`, `join_mem_token`, `mark_for_reuse`), and a
launch/queue skeleton (`launch_func`, `execute`, `plugin`, `func`,
`queue.get`, `queue.put`, `queue.yield`). The result is an IR tier
where alias relationships, buffer reuse, and structural decomposition
all show up as plain SSA edges — ready for the layout assignment, async
materialization, and pipeline-region passes that run later in
`nv_tileas`.

## In-memory only

`nv_tileaa` is strictly an internal pass-to-pass IR. With no
`BytecodeDialectInterface`, the binary contains no bytecode reader and
no writer — the cascade consumes `cuda_tile` bytecode, lowers in
memory, and never serializes an `nv_tileaa` module. The dialect also
installs no `OpAsmDialectInterface`: no custom textual printer, no
custom parser, no type or attribute aliases, nothing that would let it
round-trip through generic MLIR text. A handful of ops (`func`, `load`,
`atomic_cas`, `atomic_rmw`, `tiled_load`, `tiled_atomic_rmw`) install
the per-op `OpAsmOpInterface` purely for SSA-name pretty-printing and a
`getDefaultDialect` shortcut; everything else falls through to the
ODS-emitted generic form. The takeaway: any textual dump of an
`nv_tileaa` module is a lossy debugging artifact, never a stable wire
format.

## Semantic Surface

The dialect has a small named type surface, a target and memory attribute
surface, and a compact operation set arranged around alias-aware tile
computation. The operation roster is catalogued in
[op-roster.md](op-roster.md); the useful overview is by semantic family:

| Family | Examples | Purpose |
|---|---|---|
| Pointer and memref construction | `addptr`, `bitcast`, `int_to_ptr`, `ptr_to_int`, `make_memref` | Turn public view/pointer concepts into explicit addressable objects. |
| Memory operations | `load`, `store`, `tiled_load`, `tiled_store`, `gather_load`, `scatter_store`, `atomic_cas`, `atomic_rmw` | Express memory access with visible provenance, reuse, and token dependencies. |
| Tile compute | `addf`, `subf`, `mulf`, `divf`, `fma`, `dot`, `conv_dot`, `reduce`, `scan`, `histogram` | Preserve tile math while making alias and layout preconditions explicit. |
| Shape and view transforms | `broadcast`, `extract`, `extract_slice`, `expand_dims`, `permute`, `view`, `cat`, `make_range` | Carry shape manipulation into the internal pipeline before TileAS layout assignment. |
| Program-grid queries | `get_program_id`, `get_num_programs`, `get_dim_size`, `is_valid_program_id` | Represent kernel-grid structure without committing to NVVM builtins yet. |
| Memory-token protocol | `create_mem_token`, `join_mem_token`, `mark_for_reuse` | Encode ordering and reuse information as SSA dataflow. |
| Structural operations | `func`, `call`, `return`, `yield`, `execute`, `plugin`, `launch_func`, queues, globals, diagnostics | Provide the internal function, queue, launch, and extension shell used by later passes. |

The dialect installs only the interfaces needed for internal inlining and
generic IR handling. There is no public bytecode or text format — by
design.

## Alias-Aware Contract

`nv_tileaa` is the first stage where the compiler reasons about memory
provenance as IR rather than as implicit frontend intent. Three contracts
matter:

```c
MemRef make_memref(Pointer base, Shape shape, Stride stride,
                   MemorySpace space, AliasScope scope);

Token create_mem_token();
Token join_mem_token(ArrayRef<Token> inputs);

Value load(MemRef ref, Indices indices, Token token);
Token store(MemRef ref, Indices indices, Value value, Token token);
```

Op signatures differ by operation family, but the discipline is uniform:

- memory references carry element type, address space, shape, stride, and alias
  provenance;
- memory effects are ordered through token SSA edges;
- reuse intent is explicit through `mark_for_reuse`;
- queues and plugin hooks remain structural until TileAS and companion lowering
  decide how to materialize them;
- math and shape operations keep tile semantics stable while alias information
  becomes available to downstream analyses.

That is why the dialect sits between `cuda_tile` and `nv_tileas`: it has
enough information to refine memory and alias behavior, but has not yet
committed to the final schedule, layout, async pipeline, or TMA descriptor
form.

## Position in the cascade

`nv_tileaa` is the central waypoint of the three-dialect tile
lowering. Conceptually:

```
cuda_tile
    -> nv_tileaa
    -> nv_tileas
    -> llvm/nvvm
```

The conversion into TileAA is pattern driven: public `cuda_tile` arithmetic,
shape, view, token, and memory operations rewrite into internal TileAA forms.
The conversion out of TileAA lowers those forms into TileAS, where layouts,
schedules, async pipelines, TMA descriptors, and CTA/cluster behavior become
explicit.

`nv_tileaa` never serializes, so it is purely transient. The conversion in
produces it, the conversion out consumes it, and users must not depend on its
textual spelling or on seeing it on disk.

## Lowering Invariants

- A verified `nv_tileaa` module has no remaining `cuda_tile` operations.
- Memory references carry enough provenance for alias and reuse analysis.
- Token-producing and token-consuming memory operations preserve ordering
  dependencies as SSA dataflow.
- Tile compute still describes mathematical intent; target-specific layout and
  scheduling are deferred to TileAS.
- Queue and plugin operations are structural bridges, not final backend ABI.
- The dialect may appear in debug dumps, but those dumps are not a stable file
  format.

## AbstractOperation Record

Every registered op in `nv_tileaa` carries a single 0x70-byte `AbstractOperation` record — eight bytes wider
than the `cuda_tile` record. The dialect ctor allocates each slab with `sub_44A8C20(0x70)` and uses the extra
slot at `+0x68` for the alias-token concept pointer that gives the dialect its alias-aware identity. The
shape is otherwise the same descriptor that an `Operation*` resolves through its `OperationName` slot to
reach the dialect's interface tables and fold callback.

```c
typedef struct AbstractOperation {
    /*+0x00*/ void           **vtable;                       // dispatch for the op
    /*+0x08*/ StringRef        mnemonic;                     // e.g. "nv_tileaa.addptr"
    /*+0x18*/ ConceptModel    *interface_inliner;
    /*+0x20*/ ConceptModel    *interface_opasm;
    /*+0x28*/ ConceptModel    *interface_fold;
    /*+0x30*/ ConceptModel    *interface_typeinfer;
    /*+0x38*/ ConceptModel    *interface_bytecode;
    /*+0x40*/ ConceptModel    *interface_memeffects;
    /*+0x48*/ ConceptModel    *interface_destinationstyle;
    /*+0x50*/ ConceptModel    *interface_alias;              // alias-aware concept (nv_tileaa-only)
    /*+0x58*/ ConceptModel    *interface_extra1;
    /*+0x60*/ FoldCallback     fold_canon;                   // op-fold and canonicalize hook
    /*+0x68*/ ConceptModel    *interface_alias_token;        // extra slot for the alias-token concept
} AbstractOperation;
```

The allocator zero-initializes the slab, so unused interface slots stay null and the dispatcher probes them
without a presence flag. The `mnemonic` field is an embedded `StringRef` pointing at a `.rodata` literal owned
by the binary, not a heap-interned copy. The interface-concept pointers at `+0x18..+0x58` are the MLIR
concept-model singletons that wire inlining, asm printing, folding, type inference, bytecode, memory effects,
destination-style, and — at `+0x50` — the alias-aware concept that `nv_tileaa` uses to track buffer
provenance through ordinary dataflow. The fold callback at `+0x60` is the op's per-op rewriter; the extra
concept pointer at `+0x68` is the alias-token model backing `create_mem_token`, `join_mem_token`, and
`mark_for_reuse`. `nv_tileaa.addptr`, for instance, is registered with vtable `&unk_59E0238` and a fold/canon
record at `&unk_5B46F60`, both populated by its reg thunk in `sub_1543B70`.

The records sit consecutively in a statically-allocated array in `.data.rel.ro` that mirrors the layout of
`cuda_tile`'s bank: one slab per op, walked from the dialect base by mnemonic hash. The exact range for this
build is the bank that holds the 61 registered ops; the surrounding fold-record cluster sits at
`0x5B46D28..0x5B46F68`, which is the secondary index that the registrar threads through the slab's `+0x28`
fold-interface pointer. The end-of-registered-ops boundary is marked by the same null sentinel as
`cuda_tile`, `0x5BE6138`; lookup helpers stop walking the bank when they hit it.

This is the static-sentinel idiom described in
[mlir-infra/typeid-sentinels-and-anchors.md](../../mlir-infra/typeid-sentinels-and-anchors.md): the bank is
allocated once, lives for the entire process, and is indexed by mnemonic hash from the dialect base. Live
`Operation*` instances reach this record through their `OperationName` slot — the resolution path documented
in [mlir-infra/operation-layout.md](../../mlir-infra/operation-layout.md). The per-op `vtable` and
fold-callback pairs for the rest of the roster are catalogued in [op-roster.md](op-roster.md).

## Cross-references

- [`op-roster.md`](op-roster.md) — operation families and behavioral
  contracts.
- [`types-attrs-verifiers.md`](types-attrs-verifiers.md) — the type surface,
  the target and memory attribute surface, the `compute-capability` /
  `compute_capability` spelling pair, the parametric
  `div_by` / `same_elements` / `bounded` trio, and the dialect's verifier
  contracts.
- [`folds-canonicalizers-tokens.md`](folds-canonicalizers-tokens.md) —
  per-op fold and canonicalization records, plus the
  `create_mem_token` / `join_mem_token` / `mark_for_reuse` linear-token
  protocol that gives the dialect its alias-awareness.
