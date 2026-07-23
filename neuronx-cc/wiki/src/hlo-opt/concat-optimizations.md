# Concatenation Optimizations

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (`neuronxcc/starfish/bin/hlo-opt`, cp310). Other builds will differ. The binary ships with no Hex-Rays C for this module (`NVOPEN_IDA_SKIP_DECOMPILE`), so every pseudocode block below is reconstructed from disassembly, demangled symbol names, vtable-slot calls, and verbatim `.rodata` strings — anchored to addresses throughout.*

## Abstract

Three HLO passes in `hlo-opt` canonicalize the `concat`/`slice`/`dynamic-update-slice`
idioms that the Neuron front-end emits when it lowers tensor-tiling, loop-unrolling, and
sharded-buffer patterns. All three operate on the stock XLA `HloInstruction` graph, before
the IR drops to Penguin. They are registered by `RegisterHiloHloPasses` (`0x1e72270`) as
`--passes` entries #27, #107, and #108:

| # | `name()` key | Class | `Run` | Namespace | Role |
|---|---|---|---|---|---|
| 107 | `slice-of-concat-optimizer` | `xla::hilo::NeuronSliceOfConcatOptimizer` | `0x1fdae30` | `xla::hilo::` | `slice(concat)` / `dynamic-slice(concat)` → the single concat operand the window covers |
| 108 | `neuron_repeated_dus_to_concat` | `xla::hilo::NeuronRepeatedDusToConcat` | `0x1fd6b10` | `xla::hilo::` | `broadcast → DUS → … → DUS` chain → one `concat` over the updates |
| 27 | `simplify-concat` | `xla::SimplifyConcat` | `0x1f68690` | `xla::` (stock) | five concat peepholes on the entry computation |

The unifying mechanism is a **prefix-sum boundary algebra** over the concat axis. A
concatenation along dimension `cdim` partitions its output extent into half-open intervals
`[B[k], B[k+1])` where `B[k] = Σ_{j<k} operand(j).dims[cdim]`. #107 reads a slice window and
asks *which interval does this window exactly cover*; if the window snaps to one `[B[k],
B[k+1])` boundary and is full-extent on every other axis, the slice **is** `operand(k)` and is
replaced wholesale. #108 runs the inverse: it discovers a buffer that was written
slab-by-slab by a chain of dynamic-update-slices, recovers the per-slab order from an
`iterationidx=` tag, and rebuilds the buffer as a single `concat` over the update tensors.
The `iterationidx=` tag and the boundary algebra are the same machinery the DUS/DS mover of
[Part 4.7](allreduce-dynslice-rewrites.md) uses to track loop-unrolled write positions —
this page shares that coupling.

For reimplementation, the contract is:

- The boundary prefix-sum and the exact match predicate (`starts[cdim]==B[k]`,
  `limits[cdim]==B[k]+operand(k).dims[cdim]`, full-extent off-axis, unit strides) that gates
  the `slice→operand` rewrite.
- The `dynamic-slice` variant: how compile-time and loop-`iterationidx=`-derived start indices
  feed the *same* boundary match.
- The DUS-chain shape (`broadcast → DUS → … → DUS`, optionally through `tuple → gte → call`),
  the `ParseIterationIdx` ordering key, and the `CreateConcatFromGroup` shape check.
- The five-way `SimplifyConcat` dispatcher and what each sub-transform rewrites.

| | |
|---|---|
| **Registry** | `RegisterHiloHloPasses` `0x1e72270`, rows #27/#107/#108 |
| **Opcode byte** | `HloInstruction+0x14`; `kConcatenate=0x22`, `kDynamicSlice=0x30`, `kDynamicUpdateSlice=0x31`, `kSlice=0x6E` |
| **concat axis field** | `concatenate_dimension()` = vtable slot `+0x50` → reads `[concat+0x208]` |
| **slice vectors** | `slice_starts` `+0x208`, `slice_limits` `+0x220`, `slice_strides` `+0x238` (element units) |
| **iteration tag** | `"iterationidx="` `.rodata` `0x26e876` (13 bytes); boundary marker `"NeuronBoundaryMarker-End"` `0x27a6fd` |
| **IR level** | Stock `xla::HloInstruction` graph, pre-Penguin |

> **NOTE —** all boundary offsets and slice starts/limits are in **element** units, not bytes.
> The match arithmetic never multiplies by element size; it compares `int64` element counts
> read directly out of `slice_starts`/`slice_limits` against accumulated `dims[cdim]` values.

---

## NeuronSliceOfConcatOptimizer (#107)

### Purpose

Eliminate a `slice` or `dynamic-slice` whose window happens to coincide exactly with one
operand of the concatenation it slices. After loop tiling and collective rewrites the graph is
full of `slice(concat(a, b, c), …)` where the slice simply re-extracts `b`; this pass cuts the
concat-plus-slice round trip down to a direct reference to `b`, after which `HloDCE` removes
the now-dead concat and slice.

### Entry Point

```text
NeuronSliceOfConcatOptimizer::Run            0x1fdae30 (1740 B)
  ├─ HloModule::computations(exec_threads)   0x1fdae75
  ├─ per kDynamicSlice → OptimizeDynamicSliceOfConcat   0x1fd99c0 (4975 B)
  ├─ per kSlice        → OptimizeSliceOfConcat          0x1fd8fa0 (1633 B)
  └─ HloDCE::Run                              0x1fdb224
```

### Algorithm — dispatcher

The driver is a collect-then-process loop: it snapshots every instruction of a computation
into a worklist *before* mutating, so that `ReplaceInstruction` does not invalidate the
iterator mid-walk.

```c
StatusOr<bool> NeuronSliceOfConcatOptimizer::Run(module, exec_threads):   // 0x1fdae30
    changed = false
    for comp in module->computations(exec_threads):        // 0x1fdae75
        worklist = []                                       // vector<HloInstruction*>
        for inst in comp->instructions():                   // walk [comp+0x40] list, stride 0x10
            worklist.push_back(inst)                         // snapshot EVERY inst; no opcode filter here
        for inst in worklist:                               // loop @0x1fdb088
            switch inst->opcode():                          // byte [inst+0x14]
              case 0x30 /*kDynamicSlice*/:                  // 0x1fdb0a0 cmp al,'0'
                  changed |= OptimizeDynamicSliceOfConcat(inst)   // -> 0x1fd99c0, call @0x1fdb0b2
              case 0x6E /*kSlice*/:                          // 0x1fdb088 cmp al,'n'
                  if OptimizeSliceOfConcat(inst) != nullptr: // -> 0x1fd8fa0, call @0x1fdb26e
                      changed = true                         // (replacement happens inside the helper)
    HloDCE::Run(module, exec_threads)                       // 0x1fdb224 — erase dead concat/slices
    return changed
```

(CERTAIN — both helper-call targets and the trailing `HloDCE::Run` are demangled-symbol
calls; the opcode `cmp al,'0'`/`cmp al,'n'` tests are verbatim.)

### Algorithm — the static-slice boundary algebra (`OptimizeSliceOfConcat`)

This is the core of the page. `OptimizeSliceOfConcat(slice) → StatusOr<HloInstruction*>` returns
non-null (the chosen concat operand) when it rewrites, null+OK when it declines. The prefix-sum
`running` accumulator lives in `r14`, zeroed at `0x1fd904e` (`xor r14d,r14d`) and advanced by
`add r14,[rbp+var_1D8]` at `0x1fd9078` — `var_1D8` being the current operand's `dims[cdim]`.

```c
HloInstruction* OptimizeSliceOfConcat(slice):                    // 0x1fd8fa0
    concat = slice->mutable_operand(0)                           // 0x1fd8fd6
    if concat->opcode()/*[+0x14]*/ != 0x22 /*kConcatenate*/:     // 0x1fd8fdb cmp byte[rax+0x14],'"'
        VLOG("Skipping optimization as slice instruction=", slice,
             " doesn't have concat operand as input")
        return nullptr

    cdim    = concat->concatenate_dimension()                    // 0x1fd8fee call [vtbl+0x50] -> reads [concat+0x208]
    starts  = slice->slice_starts()    // &slice[0x208], vector<int64>   0x1fd9002
    limits  = slice->slice_limits()    // &slice[0x220]                  0x1fd900a
    strides = slice->slice_strides()   // &slice[0x238]                  0x1fd901c

    // GUARD 1 — unit stride on every axis (a strided slice can never equal a contiguous operand):
    for s in strides: if s != 1: return nullptr                  // 0x1fd91d0..0x1fd91dd cmp [rax],1

    // GUARD 2 — boundary prefix-sum + exact-cover match:
    running = 0                                                  // r14, 0x1fd904e
    for k in 0 .. concat->operand_count()-1:                     // bound = [concat+0x18]>>1
        op_k   = concat->mutable_operand(k)                      // 0x1fd9091
        ext_k  = op_k->shape().dimensions[cdim]                  // array_state[8 + cdim*8] -> var_1D8
        // Does the slice window exactly span interval [running, running+ext_k) on cdim?
        if starts[cdim] == running                               // 0x1fd90d2 cmp [starts+cdim*8], r14
           and (limits[cdim] - running) == ext_k:                // 0x1fd90e6 sub rax,r14 ; cmp var_1D8
            // candidate k found — now require FULL extent on every OTHER axis:
            for d in 0 .. slice.rank-1:                          // inner loop 0x1fd9120
                if d == cdim: continue
                if starts[d] != 0: goto next_or_fail             // 0x1fd912c cmp [starts+d*8],0
                if limits[d] != op_k->shape().dimensions[d]:     // 0x1fd9162 cmp r14',[rcx+r13*8]
                    goto next_or_fail
            // window == operand k exactly:
            ReplaceInstruction(slice->parent()/*[slice+0x48]*/, slice, op_k)   // 0x1fd91a2
            return op_k                                          // non-null
        running += ext_k                                         // 0x1fd9078 add r14,var_1D8
  next_or_fail:
    VLOG("instruction=", slice,
         " operand doesnt match input of concat instruction exactly")
    return nullptr
```

**Algebra summary (CERTAIN — disasm-anchored).** Let `B[k] = Σ_{j<k} operand(j).dims[cdim]`
be the boundary prefix sum (the running offset along the concat axis, walked left-to-right). The
slice fuses to `concat.operand(k)` iff:

1. every stride is `1` (`strides[d]==1 ∀d`);
2. on the concat axis, the window snaps to interval `k`: `slice_starts[cdim] == B[k]` **and**
   `slice_limits[cdim] == B[k] + operand(k).dims[cdim]`;
3. on every other axis `d≠cdim`, the window is full extent: `slice_starts[d] == 0` **and**
   `slice_limits[d] == operand(k).dims[d]`.

There is no cost model and no partial-cover handling: the window must coincide with exactly one
operand. A window that straddles `B[k]` (covers part of operand `k` and part of `k+1`) never
matches; that case is handed off to the slice/concat splitter in
[Part 4.21](instcombine-peephole.md).

> **GOTCHA —** the off-axis predicate compares against `operand(k).dims[d]`, *not*
> `slice->shape().dims[d]`. They are equal only when the slice is full on axis `d`; the test is
> precisely there to reject a window that is full on `cdim` but cropped on another axis. A
> reimplementation that checks `limits[d]==starts[d]+slice.dims[d]` (always true) silently
> accepts cropped slices and produces a wrong-shape replacement.

> **QUIRK —** `concatenate_dimension()` is read through vtable slot `+0x50`
> (`call qword ptr [rax+0x50]` at `0x1fd8fee`), not a fixed struct field, because the concat-dim
> getter is virtual on `HloInstruction`. The backing data still lands at `[concat+0x208]`, but
> drive it off the vtable slot — sibling instruction classes reuse that offset for unrelated
> fields.

VLOG strings verbatim (`.rodata`): `"Skipping optimization as slice instruction="`,
`"doesn't have concat operand as input"`, `"instruction="`,
`"operand doesnt match input of concat instruction exactly"`,
`"Skipping optimization for slice instruction="`.

### Algorithm — the dynamic-slice variant (`OptimizeDynamicSliceOfConcat`)

`dynamic-slice(concat(…), i0, i1, …)` runs the **same** boundary match, but the window origin
comes from the index operands and the extent from `dynamic_slice_sizes()` rather than from
`slice_starts`/`slice_limits`. Two index sources are handled: a compile-time constant index
(read with `LiteralBase::GetFirstElement<long>`), and a loop-derived index carried as an
`"iterationidx="` tag in the backend config — the identical needle (`.rodata 0x26e876`) that
`ParseIterationIdx` uses, decoded here through `absl::numbers_internal::safe_strto64_base`.

```c
HloInstruction* OptimizeDynamicSliceOfConcat(ds):                // 0x1fd99c0 (4975 B, 253 bb)
    concat = ds->mutable_operand(0)
    if concat->opcode() != 0x22:                                 // kConcatenate
        VLOG("Skipping optimization as dynamic-slice instruction=", ds,
             " doesn't have concat operand as input")
        return nullptr
    rank  = ShapeUtil::TrueNumDimensions(ds->shape())            // 0x1fd9aa0
    sizes = ds->dynamic_slice_sizes()                            // 0x1fd9aec — extent per axis
    for d in 0 .. rank-1:
        idx = ds->operand(1 + d)
        if idx is constant:
            start[d] = LiteralBase::GetFirstElement<long>(idx->literal())   // 0x1fd9d2b
        elif backend_config contains "iterationidx=":           // 0x1fda0fc needle 0x26e876
            start[d] = safe_strto64_base(substr_after_tag, /*base=*/10)     // 0x1fda8d2
        else:
            VLOG("Failed to obtain start indices for instruction=", ds)
            return nullptr
    // same prefix-sum match: find operand k with B[k]==start[cdim] AND sizes[cdim]==operand(k).dims[cdim],
    // and start[d]==0 & sizes[d]==operand(k).dims[d] for d!=cdim
    if matched(k):                                               // 0x1fd9c98 cmp rax,[rdx+r14]
        ReplaceInstruction(ds->parent(), ds, concat->operand(k))   // 0x1fd9cd8
        return concat->operand(k)
    VLOG("instruction=", ds, " operand doesnt match input of concat instruction exactly")
    return nullptr
```

The index-dtype is validated against the integer-type dispatch tables `CSWTCH_428`/`CSWTCH_432`
(S32/S64/U32 families) before `GetFirstElement<long>` reads it. (HIGH on the matcher topology
— callee targets and the boundary `cmp` at `0x1fd9c98` are verbatim; the exact `iterationidx=`
substr→`safe_strto64` arithmetic is MED because there is no decompiled C to confirm the
post-parse index composition.)

VLOG strings verbatim: `"Skipping optimization as dynamic-slice instruction="`,
`"Skipping optimization for dynamic-slice instruction="`,
`"Failed to obtain start indices for instruction="`, `"iterationidx="`.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `…::Run` | `0x1fdae30` | 1740 B | worklist dispatcher + final `HloDCE` | CERTAIN |
| `OptimizeSliceOfConcat` | `0x1fd8fa0` | 1633 B | static-`kSlice` boundary match + replace | HIGH |
| `OptimizeDynamicSliceOfConcat` | `0x1fd99c0` | 4975 B | `kDynamicSlice` with const / `iterationidx=` start | HIGH (match) / MED (idx parse) |

---

## NeuronRepeatedDusToConcat (#108)

### Purpose

Recover a `concat` from a *write-merge* idiom. When a loop is unrolled to fill a buffer, the
front-end emits `broadcast(init)` and then a chain of `dynamic-update-slice` instructions, each
overwriting one slab of the broadcast-initialised tensor at a position encoded by the loop
iteration. This pass detects the chain, orders the slabs by iteration index, and replaces the
whole `broadcast → DUS → … → DUS` cascade with one `concat` over the DUS *update* tensors —
exposing the buffer's true block structure to the downstream tiler.

### Entry Point

```text
NeuronRepeatedDusToConcat::Run               0x1fd6b10 (1333 B)
  ├─ HloDCE::Run                             0x1fd6b70  (pre-cleanup)
  ├─ MoveDusOutOfCall                        0x1fd6bf3 -> 0x1fd13e0 (22234 B)  — detect + hoist + group
  ├─ GroupedDusToConcat                      0x1fd6db6 -> 0x1fce130 (5127 B)   — rebuild as concat
  ├─ TupleSimplifier(false).Run              0x1fd6dd1/0x1fd6e0b               — tidy dus→tuple→gte
  └─ HloDCE::Run                             0x1fd6e72  (post-cleanup)
```

### Algorithm — orchestrator

```c
StatusOr<bool> NeuronRepeatedDusToConcat::Run(module, exec_threads):   // 0x1fd6b10
    HloDCE::Run(module, exec_threads)                          // 0x1fd6b70
    map = MoveDusOutOfCall(module)                             // 0x1fd6bf3
          // flat_hash_map<HloInstruction* base, vector<HloInstruction*> dus_group>
    if map.empty():
        VLOG("Leaving DUS operations unchanged.")
        return false                                          // 0x1fd6c67
    GroupedDusToConcat(map)                                    // 0x1fd6db6 — the rebuild
    TupleSimplifier(/*exclude_entry_root=*/false).Run(module) // 0x1fd6dd1/0x1fd6e0b
    HloDCE::Run(module, exec_threads)                          // 0x1fd6e72
    return changed
```

(CERTAIN — all five call targets demangle to `HloDCE::Run`, `MoveDusOutOfCall`,
`GroupedDusToConcat`, `TupleSimplifier::TupleSimplifier(bool)` + `TupleSimplifier::Run`,
`HloDCE::Run`. The map type is the `flat_hash_map<HloInstruction*, vector<HloInstruction*>>`
named in both `MoveDusOutOfCall`'s return and `GroupedDusToConcat`'s parameter symbol.)

### Algorithm — chain detection (`MoveDusOutOfCall`)

This 22 KB function (1019 basic blocks) walks each computation in post-order, recognises the DUS
chain rooted at a `broadcast`, hoists chains that live inside a called loop-body computation up
into the caller (so the chain is flat), and populates the `base → [DUS…]` map. Two chain shapes
are recognised, named verbatim by the function's own `RET_CHECK` strings:

```text
canonical : broadcast(init) → DUS → DUS → … → DUS
call-wrapped: broadcast(init) → DUS → DUS → Tuple → GTE → DUS → … → DUS   (loop body)
```

Per-DUS, the iteration index is parsed with `ParseIterationIdx` (`0x1fd2c0c`) and the boundary
slab count with `ExtractBoundaryCountFromBackendConfig` (`0x1fd3cf6`), which keys off the
custom-call target `"NeuronBoundaryMarker-End"` (`.rodata 0x27a6fd`) — the same boundary-marker
family documented under [Part 4.12](boundary-markers-layer-cut.md). A `RET_CHECK` enforces the
arity invariant:

> `iter_idx.size() == instr->operand_count() - 2` (`.rodata`, ref @`0x1fd5479`)

i.e. a DUS has `operand + update + rank` index scalars, so `operand_count - 2 == rank ==` the
number of iteration-index components.

(HIGH on chain shape and map construction — the `RET_CHECK`/VLOG strings and the
`ParseIterationIdx`/`ExtractBoundaryCountFromBackendConfig` callees are verbatim. The full
call-hoisting rewrite — `CreateCall`/`CreateTuple`/`CreateGetTupleElement`/
`ReplaceOperandWithDifferentShape`/`RemoveInputOperandFromCallByGteIndex` re-plumbing — was
not transcribed line-by-line; it is out of scope here and marked MED.)

Other verbatim diagnostics: `"Found DUS in computation "`, `" | First DUS in chain: "`,
`"    - DUS instruction: "`, `"dus_start_indices: "`, `"move_out_indices includes "`,
`"Handling call input shape mismatch: call - "`, `"Finished printing DUS groups."`,
and the two `"Should not be here. The DUS chain is not …"` `RET_CHECK`s.

### Algorithm — the ordering key (`ParseIterationIdx`)

`ParseIterationIdx(string) → vector<int>` is the link between the two passes on this page and the
DUS/DS mover of 4.7: it decodes the `"iterationidx="` tag that loop unrolling stamps into each
DUS, and that integer vector is the slab's position in the chain.

```c
vector<int> ParseIterationIdx(s):                              // 0x1fcd530 (1199 B)
    pos = s.find("iterationidx=")                             // 0x1fcd568, needle 0x26e876
    if pos == npos: return {}                                  // un-tagged DUS sort first
    tail = s.substr(pos + 13)                                  // 0x1fcd590 add rbx,0Dh (13-char tag)
    iss  = istringstream(tail)                                 // ios_base / basic_ios::init setup
    out  = []
    while getline-parsed int n from iss:                      // 0x1fcd7c7 getline + strtol
        out.push_back(n)
    return out
```

(HIGH — needle, `add 0Dh` tag skip, and the `istringstream`+`getline` parse structure are
verbatim; the exact whitespace/multi-int layout within the tail is MED.)

> **NOTE —** a DUS whose backend config carries no `iterationidx=` returns an empty key and
> sorts *first* — the comparator treats un-indexed DUS as iteration 0. If a reimplementation
> emits the tag inconsistently across a chain, the merge order silently corrupts; the tag is the
> single source of slab ordering.

### Algorithm — the rebuild (`GroupedDusToConcat` + `CreateConcatFromGroup`)

For each `(base, dus_group)` the rebuilder sorts the group by its `ParseIterationIdx` key
(introsort with a `vector<int>` comparator), picks the concat `axis` as the DUS start-index
component that varies across the chain, and calls `CreateConcatFromGroup` to assemble and
shape-check the concat over the update tensors.

```c
void GroupedDusToConcat(map):                                 // 0x1fce130 (5127 B, 272 bb)
    for (base, dus_group) in map:
        keyed = [(ParseIterationIdx(dus), dus) for dus in dus_group]
        introsort keyed by vector<int> key                    // __introsort_loop 0x1fca2f0
        axis = component of the DUS start-index that varies across the chain   // [INFERRED]
        concat = CreateConcatFromGroup([dus for _,dus in keyed], axis, comp, base)  // 0x1fcdaf0
        if concat == nullptr:                                  // shape mismatch
            VLOG("Skipping group due to shape mismatch returned by CreateConcatFromGroup. ")
            continue
        // rewire  dus -> tuple -> gte -> user   ==>   concat -> user
        last_dus->ReplaceAllUsesWith(concat)  /  ReplaceOperandWith(idx, concat)

HloInstruction* CreateConcatFromGroup(group, axis, comp, base):   // 0x1fcdaf0 (1033 B)
    out_shape = group[0]->shape()                             // 0x1fcdb56 copy first update shape
    total = 0
    for g in group:
        if g->shape().rank != group[0]->shape().rank: return nullptr       // 0x1fcdbca
        if !ShapeUtil::Compatible(g->shape(), group[0]->shape()): return nullptr  // 0x1fcdbde
        total += g->shape().dimensions[axis]                  // 0x1fcdc13 array_state[8+axis*8]
    out_shape.set_dimensions(axis, total)                     // 0x1fcdc34 -> Shape::set_dimensions
    if !ShapeUtil::Compatible(base->shape(), out_shape):      // 0x1fcdc4f
        VLOG("Cannot create concat: concatenated shape ", out_shape,
             " does not match first DUS shape ", …)
        return nullptr
    concat = HloInstruction::CreateConcatenate(out_shape, Span<group>, axis)   // 0x1fcdcfa
    return comp->AddInstruction(concat, name="")             // 0x1fcdd14
```

**The concat is built over the group's UPDATE tensors** (the DUS second operands), in iteration
order, at `axis`, with output shape equal to the base buffer's shape. The sum of the per-update
extents along `axis` must equal the base extent (`ShapeUtil::Compatible(base->shape(),
out_shape)`), else the group is skipped — this is the dual of #107's boundary match: where #107
checks one window snaps to one operand interval, #108 checks the union of all update intervals
exactly tiles the base buffer. (HIGH — `set_dimensions`/`CreateConcatenate`/`Compatible`/
`AddInstruction` callees and the shape-mismatch string are verbatim. The `axis` *derivation*
inside `GroupedDusToConcat` — which start-index component is chosen — is MED; the consumer side,
`CreateConcatFromGroup` receiving `axis` as arg 2, is CERTAIN.)

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `…::Run` | `0x1fd6b10` | 1333 B | DCE → move → group → TupleSimplifier → DCE | CERTAIN |
| `MoveDusOutOfCall` | `0x1fd13e0` | 22234 B | chain detect + call-hoist + map build | HIGH (pattern) / MED (hoist) |
| `GroupedDusToConcat` | `0x1fce130` | 5127 B | sort by iter-idx + rewire | HIGH |
| `ParseIterationIdx` | `0x1fcd530` | 1199 B | `iterationidx=` → `vector<int>` order key | HIGH |
| `CreateConcatFromGroup` | `0x1fcdaf0` | 1033 B | concat builder + base-shape check | HIGH |

---

## SimplifyConcat (#27)

### Purpose

Despite the generic name, `SimplifyConcat` is **not** a single nested-flatten pass: its `Run` is
a 103-byte dispatcher that runs five distinct concat peepholes over **only the entry
computation** and ORs their changed flags. It belongs to the stock `xla::` namespace (orders
1–43), not `xla::hilo::`, so unlike #107/#108 it touches just `entry_computation()`.

### Entry Point

```text
SimplifyConcat::Run                          0x1f68690 (103 B)
  comp = module->entry_computation()         0x1f686a3
  ├─ selectionRemoval(comp)                  0x1f686ae -> 0x1f66370
  ├─ additionToConcatenation(comp)           0x1f686b8 -> 0x1f66bb0
  ├─ fuseConcatenations(comp)                0x1f686c2 -> 0x1f67270
  ├─ simplifyConcatenatedSum(comp)           0x1f686cc -> 0x1f67800
  └─ simplifyConcatenatedZeros(comp)         0x1f686d6 -> 0x1f67f80
```

### Algorithm

```c
StatusOr<bool> SimplifyConcat::Run(module, exec_threads):     // 0x1f68690
    comp = module->entry_computation()                        // 0x1f686a3
    c  = selectionRemoval(comp)                               // 0x1f686ae
    c |= additionToConcatenation(comp)                        // 0x1f686b8 ; or ebx,eax @0x1f686c0
    c |= fuseConcatenations(comp)                             // 0x1f686c2 ; or ebx,eax @0x1f686ca
    c |= simplifyConcatenatedSum(comp)                        // 0x1f686cc ; or ebx,eax @0x1f686d4
    c |= simplifyConcatenatedZeros(comp)                      // 0x1f686d6 ; or ebx,eax @0x1f686e3
    return c
```

(CERTAIN — all five call targets and the four `or ebx,eax` accumulators are verbatim; only
`entry_computation()` is fetched.)

### The five sub-transforms

Each is `xla::<fn>(HloComputation*) → bool`, iterates `MakeInstructionPostOrder()`, matches with
the XLA `xla::match::HloInstructionPattern` DSL, rebuilds with the XLA helper builders
(`MakeConcatHlo`/`MakeBinaryHlo`/`MakeBroadcastHlo`/`PadVectorWithZeros`), and replaces via
`computation->ReplaceInstruction`.

| fn | addr | rebuild callees | rewrite (inferred) | confirm string |
|---|---|---|---|---|
| `selectionRemoval` | `0x1f66370` (1950 B) | `LiteralBase::Slice`, `padding_config`, `dynamic_cast` | constant-predicate `select` / concat-of-`select` → the taken branch | `"computation->ReplaceInstruction(inst, onTrue)"` |
| `additionToConcatenation` | `0x1f66bb0` (1573 B) | `MakeConcatHlo`, `PadVectorWithZeros` | sum of zero-padded disjoint vectors → `concat` + pad | `"computation->ReplaceInstruction(inst, pad)"` |
| `fuseConcatenations` | `0x1f67270` (1263 B) | `MakeConcatHlo`, match-DSL | nested same-axis concats → one flat concat over all leaves | `"computation->ReplaceInstruction( inst, fused)"` |
| `simplifyConcatenatedSum` | `0x1f67800` (1779 B) | `MakeConcatHlo`, `MakeBinaryHlo` | `add(pad(a), pad(b))` with disjoint pads → `concat(a, b)` | `"computation->ReplaceInstruction(inst, concat)"` |
| `simplifyConcatenatedZeros` | `0x1f67f80` (1803 B) | `MakeConcatHlo`, `MakeBroadcastHlo` | concat with zero-broadcast operands → broadcast/pad form | `"computation->ReplaceInstruction(inst, concat)"` |

> **NOTE —** `fuseConcatenations` is the genuine "merge nested concats" canonicalizer the name
> implies; the other four *introduce* concats from `add`/`pad`/`zero`/`select` idioms or peel a
> constant-predicate `select`. There is no generic "adjacent identical-source slice merge" here —
> that lives in `NeuronHloInstCombine` (#62) and in `NeuronSliceOfConcatOptimizer` (#107) above.

(HIGH on builder callees and `ReplaceInstruction` debug strings; the exact `match::` predicates —
operand arity, pad/zero constraints — are MED, characterised by their builder targets rather than
by transcribing each anonymous matcher lambda.)

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `SimplifyConcat::Run` | `0x1f68690` | 103 B | 5-way dispatcher over entry comp | CERTAIN |
| `selectionRemoval` | `0x1f66370` | 1950 B | const-pred select peel | HIGH / MEDIUM (predicate) |
| `additionToConcatenation` | `0x1f66bb0` | 1573 B | padded-sum → concat | HIGH / MEDIUM (predicate) |
| `fuseConcatenations` | `0x1f67270` | 1263 B | nested concat flatten | HIGH / MEDIUM (predicate) |
| `simplifyConcatenatedSum` | `0x1f67800` | 1779 B | `add(pad,pad)` → concat | HIGH / MEDIUM (predicate) |
| `simplifyConcatenatedZeros` | `0x1f67f80` | 1803 B | zero-broadcast concat collapse | HIGH / MEDIUM (predicate) |

---

## Adversarial Self-Verification

The five strongest claims on this page, re-challenged against the binary:

1. **Prefix-sum accumulator in `r14`, advanced by `dims[cdim]`.** Re-checked
   `OptimizeSliceOfConcat` disasm: `xor r14d,r14d` @`0x1fd904e`, `add r14,[rbp+var_1D8]`
   @`0x1fd9078`, `cmp [rax+rcx],r14` @`0x1fd90d2`. The accumulator is real and the match compares
   `starts[cdim]` to it.
2. **`concatenate_dimension()` is virtual at vtable slot `+0x50`.** `call qword ptr [rax+0x50]`
   @`0x1fd8fee` — a virtual call, not a direct field read.
3. **`SimplifyConcat::Run` calls exactly five sub-transforms and ORs their bools, entry comp
   only.** Disasm shows `entry_computation` @`0x1f686a3` then five demangled calls with
   `or ebx,eax` between each.
4. **#108 pipeline = DCE → MoveDusOutOfCall → GroupedDusToConcat → TupleSimplifier → DCE.** All
   five calls demangle exactly in that order (`0x1fd6b70`/`0x1fd6bf3`/`0x1fd6db6`/
   `0x1fd6dd1`+`0x1fd6e0b`/`0x1fd6e72`).
5. **The `dynamic-slice` variant shares the `"iterationidx="` needle with `ParseIterationIdx`.**
   Both reference `.rodata 0x26e876` (`OptimizeDynamicSliceOfConcat` @`0x1fda0fc`,
   `ParseIterationIdx` @`0x1fcd544`) and decode via `safe_strto64_base`/`istringstream`.
   The needle is shared; the post-parse index composition in the DS variant
   remains **[INFERRED]** (no decompiled C).

Items left **[INFERRED]** and not upgraded: the concat-`axis` *selection* inside
`GroupedDusToConcat`; the exact `match::` predicates of the five `SimplifyConcat` sub-transforms;
the full call-hoisting rewrite inside `MoveDusOutOfCall`. None are asserted as fact above.

---

## Related Components

| Name | Relationship |
|---|---|
| `NeuronHloInstCombine` (#62) | owns the adjacent-slice merge and the partial-cover slice/concat split this page defers to |
| `HloDCE` | bookends both #107 and #108; erases the concat/slice/DUS left dead after each rewrite |
| `TupleSimplifier` | invoked by #108 to collapse the `dus → tuple → gte` plumbing after the concat rebuild |
| `NeuronAddBoundaryMarker` family | `"NeuronBoundaryMarker-End"` couples #108's slab-count extraction to the boundary-marker pass |

## Cross-References

- [AllReduce/DynamicSlice Rewrites](allreduce-dynslice-rewrites.md) — Part 4.7, the DUS/DS mover and the shared `iterationidx=` loop-position tag
- [Boundary Markers & Layer-Cut Analysis](boundary-markers-layer-cut.md) — Part 4.12, the `NeuronBoundaryMarker-End` custom call #108 keys off
- [The hlo-opt Pass Registry](pass-registry.md) — `RegisterHiloHloPasses` and the `--passes` numbering (#27/#107/#108)
