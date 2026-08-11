# CollectivePermute → AllGather Lowering

> *All addresses on this page are virtual addresses (VMA) for the `hlo-opt` binary inside `neuronx_cc-2.24.5133.0+58f8de22` (cp310 wheel, `neuronxcc/starfish/bin/hlo-opt`); resolve via `objdump --start-address` or the VMA-keyed IDA tables. VA ≠ raw file offset: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000 (section headers). `hlo-opt` is decompile-skipped, so claims are anchored to the IDA function/string/xref tables rather than a Hex-Rays listing. Other versions will differ.*

## Abstract

`NeuronCollectivePermuteToAllGather` is the dedicated HLO rewrite that turns every `kCollectivePermute` (opcode `29`) into a *global all-gather followed by a replica-indexed dynamic-slice*. This is a different and more specialized path than the forward [collectives→custom-call](collectives-to-customcall.md) conversion: that pass wraps stock XLA collectives in `AwsNeuronCustomCall` targets and leaves the *shape* of the collective alone, whereas this pass structurally **replaces** a collective-permute with an entirely different subgraph — a gather over all replicas, plus a local index-select that re-extracts exactly the shard the permute would have delivered. The collectives→custom-call pass does **not** handle collective-permute at all; this pass owns it.

The intuition is "gather everything, then locally pick the one shard the permute would have sent me." Stock XLA expresses a collective-permute as a set of `(source, target)` replica pairs: after the op, replica `target` holds the data that replica `source` started with. Neuron has no efficient hardware primitive for that scatter, but it does have an all-gather. So the pass gathers the operand across every replica into one big tensor, builds a constant **mapping array** `mapping[target] = source`, has each replica read `mapping[replica_id()]` at runtime to learn *its* source replica, multiplies that by the per-replica shard extent to form a byte/element offset, and `dynamic-slice`s its own shard back out of the gathered tensor. The `-1` sentinel plus an identity backfill make the mapping a total function even when `source_target_pairs` is only a partial permutation.

The rewrite body is `Run` (`0x1f931d0`, 6692 bytes, 1333 instructions, 343 basic blocks). It is gated by `xla::hilo::IsSourceTargetPairsInvalid` (`0x1f90da0`), a structural predicate over the pairs. The pass constructor (`0x1f927e0`) computes a `core_count` from the platform version and a flags-struct divisor, but — a real surprise documented below — the rewrite's replica math is driven by the `HloModuleConfig`, not by that `core_count`. This page reconstructs the full emission sequence instruction-by-instruction, the mapping-array semantics, the validity gate, and the constructor's core-count logic.

For reimplementation, the contract is:

- **The emitted subgraph**: the exact instruction chain `all-gather → constant(mapping) → replica-id → dynamic-slice(mapping) → reshape → constant(shard) → multiply → constant(0)×(rank−1) → dynamic-slice(ag)`, with the right shapes and the right `CreateAllGather` flags.
- **The mapping-array construction**: `S32[total]`, `memset(-1)`, `mapping[target]=source`, identity backfill — and what `total` is.
- **The validity gate** `IsSourceTargetPairsInvalid`: duplicate-target, cross-group, and completeness checks, and the polarity with which `Run` consumes its result.
- **The constructor's core-count derivation** from platform version / `nc_divisor` / `override_core_count`, and the fact that the rewrite does *not* consume it.

| | |
|---|---|
| **Pass key** | `collective-permute-to-all-gather` (`name()` @ `0x1f8ffa0`) |
| **Rewrite entry** | `xla::hilo::NeuronCollectivePermuteToAllGather::Run` — `0x1f931d0` (6692 B) |
| **Cold landing pad** | `Run [clone .cold]` — `0x1f92db2` (408 B) |
| **Constructor (C1)** | `…::NeuronCollectivePermuteToAllGather(string, int, int)` — `0x1f927e0` (610 B) |
| **Static-init / registrar** | `_GLOBAL__sub_I…C2…` `0x1f926e0`; factory lambda `0x1e70420` (117 B) |
| **Validity gate** | `xla::hilo::IsSourceTargetPairsInvalid(vector<pair<l,l>>, l, l)` — `0x1f90da0` (6449 B) |
| **Source file (LogMessage arg)** | `hilo/hlo_passes/neuron_collective_permute_to_all_gather.cc` (`@0x3cf820`) |
| **IR level** | hilo HLO (post-ingestion, pre-penguin), per-computation post-order |
| **Triggers on** | `HloOpcode::kCollectivePermute` = `29` (`0x1D`) |

---

## The Rewrite

### Purpose

Replace each collective-permute with an all-gather + replica-indexed dynamic-slice that reproduces the permute's data movement using only a gather primitive and local index arithmetic. The pass runs per computation, in post-order, and edits the graph in place: `ReplaceAllUsesWith` then `RemoveInstruction` on the original CP.

### Entry Point

```text
NeuronCollectivePermuteToAllGather::Run (0x1f931d0, 6692 B)
  └─ HloModule::computations(exec_threads)              ── iterate computations
       └─ HloComputation::MakeInstructionPostOrder()     ── post-order walk
            └─ [opcode == 29?]  Cast<HloCollectivePermuteInstruction>
                 ├─ IsSourceTargetPairsInvalid(...)       (0x1f90da0)  ── gate
                 ├─ HloInstruction::CreateAllGather(...)   (0x96680b0)  ── Span<ReplicaGroup> overload
                 │     └─ CreateAllGather(... CollectiveDeviceList ...) (0x964d3d0)
                 ├─ LiteralUtil::PopulateR1<int> + CreateConstant       ── mapping array
                 ├─ HloInstruction::CreateReplicaId(U32[])  (0x9665840)
                 ├─ HloInstruction::CreateDynamicSlice(...)             ── mapping[rid]
                 ├─ HloInstruction::CreateReshape(...)
                 ├─ HloInstruction::CreateBinary(.., kMultiply, ..)     ── idx * shard
                 ├─ HloInstruction::CreateDynamicSlice(...)             ── carve my shard
                 ├─ OpMetadata::CopyFrom + ReplaceAllUsesWith
                 └─ HloComputation::RemoveInstruction(cp)
```

The callee list of `Run` confirms this chain in exactly this order (verified against the IDA `callees` array for `0x1f931d0`): `IsSourceTargetPairsInvalid` → `mutable_operand` → `shape` → `ReplicaGroup` ctor + `RepeatedField<long>::Reserve` → `NextChannelId` → `Shape::array_state` → `MakeValidatedShape` → `CreateAllGather` (the `Span<ReplicaGroup>` overload, *not* the `CollectiveDeviceList` one — that one is reached internally) → `AddInstruction`; then `operator new` + `memset` + `Literal` ctor + `PopulateR1<int>` + `CreateConstant`; then `CreateReplicaId` → `CreateDynamicSlice` → `CreateReshape` → `LiteralUtil::CreateR0<int>` + `CreateConstant` → `CreateBinary` → (`vector::_M_realloc_insert` for the offset vector) → a second `CreateR0<int>`/`CreateConstant` (the zero offsets) → `CreateDynamicSlice` → `OpMetadata::CopyFrom` → `ReplaceAllUsesWith` → `RemoveInstruction`.

### Algorithm

```c
StatusOr<bool> NeuronCollectivePermuteToAllGather::Run(module, exec_threads):   // 0x1f931d0
    // --- config read (base = module + 0x28) ---
    replica_count  = config[+0x170];   // log "Initial replica_count from config = "  @0x1f9475e
    num_partitions = config[+0x178];   // log "Initial num_partitions from config = "  @0x1f946ce
    changed = false

    for comp in module->computations(exec_threads):
        for inst in comp->MakeInstructionPostOrder():
            if inst->opcode() != 29 /*kCollectivePermute, [rax+0x14]*/: continue   // @0x1f9340e
            VLOG("Found collective-permute: " + inst->ToString())                  // @0x1f94020
            cp = Cast<HloCollectivePermuteInstruction>(inst)

            // --- (b) effective replica count ---                                  // small-mesh fallback
            if replica_count <= 1 && num_partitions <= 1:
                c = (int)config[+0x0C];        // movsxd; secondary mesh count       @0x1f942af
                if c > 0: num_partitions = c   // cmovle: keep prior if c <= 0
            total = replica_count * num_partitions                                  // imul

            // --- gate: is this a convertible full permutation? ---
            StatusOr<bool> st = IsSourceTargetPairsInvalid(
                                    cp->source_target_pairs(),   // pairs @ cp+0x218..+0x220
                                    replica_count, total)        // (m, n)
            if !st.ok(): return st.status()    // propagate; teardown @0x1f94468/0x1f9449e
            if st.value() == 0: continue       // NOT rewritten on a 0 result (see GOTCHA)

            operand  = cp->mutable_operand(0)
            op_shape = operand->shape()

            // --- (d) one global replica group, one channel id ---
            ReplicaGroup rg; rg.replica_ids = [0, 1, ..., total-1]   // RepeatedField<long>::Reserve+fill
            channel_id = hlo_query::NextChannelId(*module)

            // --- all-gather output shape: one dim scaled by total ---
            ag_dims = op_shape.dims; ag_dims[AG_DIM] *= total        // imul on one dim entry
            ag_shape = ShapeUtil::MakeValidatedShape(op_shape.element_type, ag_dims)
            ag = CreateAllGather(ag_shape, {operand}, /*all_gather_dimension=*/1,   // edx = 1
                                 {rg}, /*constrain_layout=*/true, channel_id,
                                 /*use_global_device_ids=*/true)     // 0x96680b0
            ag = comp->AddInstruction(ag)

            // --- (a) mapping array constant: S32[total] ---
            int32* map = new int32[total]              // operator new (._Znwm)
            memset(map, 0xFF, total * 4)               // every entry = -1
            for (src, tgt) in cp->source_target_pairs():
                map[tgt] = src                         // mov [r14 + tgt*4], src
            for i in [0, total):
                if map[i] == -1: map[i] = i            // identity backfill
            Literal lit(S32[total]); lit.PopulateR1<int>(map)
            map_inst = comp->AddInstruction(CreateConstant(lit))
            VLOG("Created mapping array constant: " + map_inst->ToString()          // @0x1f943eb
                 + " with values: " + per_elem("  mapping_array[" i "] = " v))      // @0x1f93ad1

            // --- (c) gather-of-mapping[replica_id] ---
            rid = comp->AddInstruction(CreateReplicaId(U32[]))     // PrimitiveType 8 = U32
            ds0 = comp->AddInstruction(
                      CreateDynamicSlice(S32[1], map_inst, {rid}, /*slice_sizes=*/{1}))
            idx = comp->AddInstruction(CreateReshape(S32[], ds0, /*inferred_dim=*/-1))  // -> S32[]

            // --- offset = idx * shard; carve out my shard ---
            shard = comp->AddInstruction(
                       CreateConstant(LiteralUtil::CreateR0<int>(op_shape.dims[0])))
            off   = comp->AddInstruction(
                       CreateBinary(S32[], /*HloOpcode=*/69 /*kMultiply*/, idx, shard))
            offsets = { off }
            for d in [1, ag_shape.rank):
                offsets.push(comp->AddInstruction(
                                CreateConstant(LiteralUtil::CreateR0<int>(0))))
            result = comp->AddInstruction(
                        CreateDynamicSlice(op_shape, ag, offsets, /*slice_sizes=*/op_shape.dims))
            result->metadata().CopyFrom(cp->metadata())     // OpMetadata::CopyFrom

            cp->ReplaceAllUsesWith(result)
            comp->RemoveInstruction(cp)
            changed = true
    return changed                                          // StatusOr<bool>
```

> **NOTE —** every `LogMessage` ctor in `Run` is passed the file string `hilo/hlo_passes/neuron_collective_permute_to_all_gather.cc` (`@0x3cf820`, referenced nine times across `Run`). These are `VmoduleActivated`-gated VLOGs, so they cost nothing at the default verbosity but are the most reliable anchors for which step is which. `Found collective-permute:` is referenced at `0x1f94020`, `Created mapping array constant:` at `0x1f943eb`, the per-element `  mapping_array[` dump at `0x1f93ad1`, and the two config reads at `0x1f9475e`/`0x1f946ce`.

### Emitted-Graph Shape

The rewrite removes one CP and adds the following (shapes in XLA notation; `N = total`, `D` = the gathered dimension):

```text
%operand   = <op_shape>                              # cp operand 0
%ag        = all-gather(%operand),                   # shape = op_shape with dims[D] *= N
                 dimensions={D},
                 replica_groups={{0,1,...,N-1}},      # one global group
                 channel_id=NextChannelId(module),
                 constrain_layout=true,
                 use_global_device_ids=true
%map       = s32[N] constant { mapping[target]=source, -1 backfilled to identity }
%rid       = u32[]  replica-id()
%ds0       = s32[1] dynamic-slice(%map, %rid), dynamic_slice_sizes={1}
%idx       = s32[]  reshape(%ds0)                     # this replica's source id
%shard     = s32[]  constant(op_shape.dims[0])        # per-replica extent
%off       = s32[]  multiply(%idx, %shard)            # HloOpcode 69
%z         = s32[]  constant(0)                       # one per remaining output dim
%result    = <op_shape> dynamic-slice(%ag, %off, %z, ...),
                 dynamic_slice_sizes=op_shape.dims
# %result inherits cp's OpMetadata; replaces all uses of cp; cp removed.
```

PrimitiveType crosscheck (the first argument to `ShapeUtil::MakeValidatedShape`): `8 = U32` for the replica-id, `4 = S32` for the mapping/index/offset constants — both appear in `Run`'s constant pool. Opcode crosscheck via `HloOpcodeString` (`0x96bb550`): `29 = collective-permute`, `48 = dynamic-slice`, `69 = multiply`, `90 = replica-id`, `91 = reshape`. Both `4` and `8` and `29` and `69` are present in `Run`'s `constants_used` set, confirming the shape/opcode choices.

> **QUIRK —** the all-gather dimension argument is the **constant `1`** (`mov edx, 1` flowing into the `Span<ReplicaGroup>` overload at `0x96680b0`), yet the final dynamic-slice puts the computed offset at **offset-vector index 0** and zeros every other position, and the `shard` constant is `op_shape.dims[0]`. For a rank-1 / leading-dim-sharded operand these coincide; for a higher-rank operand the page treats "the AG concat dimension" and "the slice offset dimension" as *the operand's sharded leading dimension* and marks the exact axis pairing as not fully pinned from disasm alone (see [Gaps](#gaps)). A reimplementer targeting only leading-dim shards is safe; a general reimplementation must verify the axis.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `NeuronCollectivePermuteToAllGather::Run` | `0x1f931d0` | 6692 B | per-computation post-order rewrite body | CERTAIN |
| `Run [clone .cold]` | `0x1f92db2` | 408 B | exception-cleanup landing pad | CERTAIN |
| `…::NeuronCollectivePermuteToAllGather(string,int,int)` (C1) | `0x1f927e0` | 610 B | constructor; computes `core_count`/`override_core_count` | CERTAIN |
| `_GLOBAL__sub_I…C2…` | `0x1f926e0` | — | static-init wrapper (registration) | HIGH |
| registrar factory lambda | `0x1e70420` | 117 B | reads flags struct, calls C1 ctor | CERTAIN |
| `IsSourceTargetPairsInvalid(vec<pair>,l,l)` | `0x1f90da0` | 6449 B | duplicate/cross-group/completeness gate | HIGH |
| `…::name()` | `0x1f8ffa0` | — | returns `"collective-permute-to-all-gather"` | CERTAIN |
| `HloInstruction::CreateReplicaId(Shape)` | `0x9665840` | — | builds `u32[]` replica-id op | CERTAIN |
| `HloInstruction::CreateAllGather(…,Span<ReplicaGroup>,…)` | `0x96680b0` | 277 B | public overload → CollectiveDeviceList one | CERTAIN |
| `HloInstruction::CreateAllGather(…,CollectiveDeviceList,…)` | `0x964d3d0` | — | inner overload | CERTAIN |
| `HloOpcodeString(HloOpcode)` | `0x96bb550` | — | opcode→string (opcode crosscheck) | CERTAIN |

---

## The Mapping Array

### Purpose

The mapping array is a compile-time constant `S32[total]` that encodes the permutation as a lookup table: `mapping[r]` = "which replica's data ends up at replica `r`." Each replica reads `mapping[replica_id()]` at runtime to discover its own source replica, without any cross-replica communication beyond the single all-gather.

### Algorithm

```c
// build @ 0x1f9382c .. 0x1f938b4
int32* map = new int32[total]          // operator new ._Znwm; total = replica_count * num_partitions
memset(map, 0xFF, total * 4)           // every entry initialised to -1 (constant 255 in the pool)
for (source, target) in cp->source_target_pairs():   // 16-byte pairs at cp+0x218..+0x220
                                                      //   pair.first=source @+0, pair.second=target @+8
    map[target] = source               // mov [r14 + target*4], source
for i in [0, total):                   // identity backfill
    if map[i] == -1: map[i] = i        // replicas that are not a permute target keep their own data
```

The `-1` sentinel plus the identity backfill make the mapping **total** even when `source_target_pairs` is only a *partial* permutation: any replica that is never named as a target retains its own shard. Without the backfill, an unmapped replica would read `-1`, multiply by the shard extent, and dynamic-slice a wildly out-of-range (clamped) offset — so the backfill is a correctness requirement, not an optimization.

> **GOTCHA —** the byte order of each `(source, target)` pair is `first = source` (offset +0), `second = target` (offset +8), and the store is `map[target] = source` — *not* `map[source] = target`. A reimplementation that inverts this builds the transpose permutation and silently moves data the wrong direction. The store instruction indexes the destination array by the pair's *second* element.

### Considerations

`total` is the product of the `HloModuleConfig` `replica_count` and `num_partitions` (with the small-mesh fallback), so for a large mesh the mapping constant and the all-gather group both scale linearly with the device count. The allocation `new int32[total]` is guarded against `total > 0x1FFFFFFFFFFFFFFF` (= `2305843009213693951`, present in `Run`'s constant pool) with a jump to the `std::vector`/`length_error` path — a defensive bound that is unreachable for any real mesh. The constant `255` (the `memset` fill) and `2305843009213693951` are both in `Run`'s `constants_used`, confirming the `-1` initialization and the overflow guard.

---

## The Validity Gate — `IsSourceTargetPairsInvalid`

### Purpose

A pure structural predicate (`0x1f90da0`, 6449 B, 240 blocks) that decides whether a CP's `source_target_pairs` form a permutation the pass can lower. Its demangled signature is `xla::hilo::IsSourceTargetPairsInvalid(std::vector<std::pair<long,long>> const&, long, long)` — the two trailing `long`s are `replica_count` (`m`) and `total` (`n`).

### Algorithm

```c
StatusOr<bool> IsSourceTargetPairsInvalid(pairs, m /*replica_count*/, n /*total*/):  // 0x1f90da0
    // builds absl flat_hash structures over the pairs:
    //   source->? and target->? maps; a group->vector<pair> map (FlatHashMapPolicy)
    for (source, target) in pairs:
        if target already inserted: return INVALID    // duplicate-target collision path
        // cross-group check: a permute must stay inside a replica-group of width m
        if (source / m) != (target / m):              // two idiv by m
            mark cross_group_violation                //   flag set on mismatch
    // completeness sweep: every index in [0, n) must appear as a target
    for r in [0, n):
        if r is never a target: mark incomplete       // cmp [r10+rax], rbx loop
    // writeback: StatusOr<bool> { ok = byte@+8, value = qword@+0 }
    // clean full-permutation -> value = 0 ; duplicate/violation/incomplete -> value = 1
```

The structural checks are string-free. The validator allocates `absl::flat_hash` tables over the pairs (an OOM there is the only non-`ok` Status it can return, via the `_M_realloc_insert` path inside its vector growth), performs the two `idiv`-by-`m` cross-group test, and runs a completeness sweep that every index `[0, n)` is some pair's target. The constant `576460752303423487` (= `0x7FFFFFFFFFFFFFF`, the `vector::max_size` guard) appears in its constant pool, consistent with the hash-table growth path.

> **GOTCHA —** the predicate's *name* says "Invalid", but `Run` performs the rewrite only when the returned `value` is **non-zero** and **skips** (leaves the CP intact) when it is `0`. Control-flow is certain: after the call `Run` does `al = st.value(); if (al == 0) continue;`. So either the function returns "is a valid/convertible full permutation" (name stale/inverted), or `Run`'s branch sense is the opposite of the literal name. Trust the control flow — `value != 0` ⇒ rewrite — not the symbol name. The human-readable meaning of the boolean is the one **[INFERRED]** semantic on this page.

### Considerations

The cross-group check (`source/m == target/m`) is what makes this the "intra-group full permutation" path: a collective-permute that crosses replica-group boundaries is left untouched. The completeness sweep is why a *partial* permute can still pass the gate at the validator while the mapping-array backfill handles the unmapped replicas downstream — the two stages cooperate on partiality. A non-`ok` return aborts the **entire module** with that Status (teardown at `0x1f94468`/`0x1f9449e`), not just the one CP.

---

## The Constructor and Core-Count Logic

### Purpose

The pass object is constructed once at registration with `(platform_version : string, nc_divisor : int, override_core_count : int)`. The C1 ctor (`0x1f927e0`, 610 B, demangled `(…basic_string…, int, int)`) computes a `core_count` field and stores the override.

### Algorithm

```c
NeuronCollectivePermuteToAllGather(platform_version, nc_divisor, override_core_count):  // 0x1f927e0
    base ctor (registration)
    VLOG("Platform Version: " + platform_version)                      // @0x1f928c3
    if platform_version == "3.0":          // version-gated; literal [INFERRED], see Gaps
        this->core_count = (override_core_count > 0) ? override_core_count
                                                     : (128 / nc_divisor)
    else:
        this->core_count = 32              // VLOG ". Defaulting to 32 cores"  @0x1f928e5
    this->override_core_count = override_core_count   // stored at +0x0C
    VLOG("core count " + this->core_count)
    VLOG("override_core_count " + override_core_count)
```

The registrar lambda (`0x1e70420`, 117 B) `operator new`s the pass object and calls this C1 ctor, feeding the three arguments from the global flags struct (per the pass-registry row: version, an `nc_divisor`, and an `override_core_count`). The C1 ctor's constant pool contains `128`, `32`, and `400`, consistent with the `128 / nc_divisor` and "32 cores" arithmetic; the strings `"Platform Version: "`, `"core count "`, `"override_core_count "`, and `". Defaulting to 32 cores"` are all referenced from this function.

> **GOTCHA —** `core_count` does **not** set the all-gather width. The width comes from `total = replica_count * num_partitions`, which `Run` reads out of the `HloModuleConfig` (`config[+0x170]`/`[+0x178]`, with the `config[+0x0C]` small-mesh fallback). The ctor's `core_count` / `override_core_count` / `"3.0"` / `128`-core machinery is computed here but consumed elsewhere; treat the two quantities as independent.

### Considerations

Because the rewrite is config-driven, the small-mesh fallback matters: when both `replica_count` and `num_partitions` report `≤ 1`, `Run` re-reads a secondary mesh count from `config[+0x0C]` (used only if `> 0`, else the prior `num_partitions` is kept via a `cmov`). This prevents a degenerate `N = 1` all-gather — a no-op gather that would leave the CP semantically wrong — when the meta-config under-reports the mesh.

---

## Edge Cases / When the Pass Bails

| Condition | Behavior | Anchor |
|---|---|---|
| Instruction is not `kCollectivePermute` | skipped by the `opcode == 29` filter | `@0x1f9340e` |
| `IsSourceTargetPairsInvalid` returns `!ok` | **whole module** aborted with that Status | teardown `0x1f94468`/`0x1f9449e` |
| Gate `value == 0` (clean per `Run`'s polarity) | CP left untouched; loop continues; `changed` unchanged | `if (al==0) continue` |
| Both mesh counts `≤ 1` | `num_partitions` re-derived from `config[+0x0C]` (clamped `> 0`) | small-mesh fallback |
| `total > 0x1FFFFFFFFFFFFFFF` | jump to `length_error` path (unreachable in practice) | const `2305843009213693951` |
| `platform_version != "3.0"` | ctor falls back to 32 cores; **does not abort** (core_count unused by rewrite) | `". Defaulting to 32 cores"` |

---

## Gaps

Limits of this reconstruction — the binary is decompile-skipped, so the claims rest on the IDA function/string/xref/callee tables rather than a Hex-Rays listing:

- **AG-dimension vs slice-offset axis** — **[INFERRED]**. `CreateAllGather` receives `dim = 1`, the AG output scales one dim by `total`, and the final dynamic-slice places the offset at vector index 0 with `shard = dims[0]`. These coincide for a leading-dim-sharded operand; the exact axis pairing for rank > 1 operands is not proven from disasm. Treat both as "the operand's sharded leading dimension."
- **Predicate polarity** — **[INFERRED]**. `Run` rewrites on `value != 0` despite the name `IsSourceTargetPairsInvalid`. The control flow is read off the disassembly; only the boolean's human meaning is reconstructed. See the gate's GOTCHA.
- **`HloModuleConfig` field offsets** — **[INFERRED]**. `+0x170` (`replica_count`), `+0x178` (`num_partitions`), `+0x0C` (fallback) are derived from the two string-anchored VLOGs and the `cmov` fallback; the config struct was not independently reconstructed here. Cross-check against the replica-group infrastructure in [collectives→custom-call](collectives-to-customcall.md) (and Part 13, Distribution & Collectives).
- **`"3.0"` literal.** The version comparison is reconstructed as `== "3.0"`; no `"3.0"` string is present in the binary's string table, so the comparison likely uses an inline/immediate form. The branch itself is read off the disassembly — the "Defaulting to 32 cores" path is real — but the compared literal is **[INFERRED]**.
- **U32→S32 index convert** — **[INFERRED]**. `replica-id()` is `u32[]`; the dynamic-slice start index is consumed as `s32`. An implicit convert/bitcast is assumed (no visible `kConvert` in the callee chain); it may be folded into `CreateDynamicSlice`'s start-operand handling.
- **`core_count` consumer** — **[UNRESOLVED]**. Computed in the ctor but never consumed by the rewrite; the consuming pass or assertion was not located.

---

## Related Components

| Name | Relationship |
|---|---|
| [Collectives → Custom-Call Forward Conversion](collectives-to-customcall.md) | the general collective-lowering path; it does **not** handle collective-permute — this pass does |
| [AllReduce/ReduceScatter/AllGather Combiners](collective-combiners.md) | run later; can combine the all-gather this pass emits |
| [Collective Stream-ID & Channel-ID Family](collective-stream-channel-id.md) | `NextChannelId` source; the emitted all-gather gets a fresh channel id |
| [The hlo-opt Pass Registry](pass-registry.md) | registers this pass; supplies the ctor's three flag arguments |

## Cross-References

- [Collectives → Custom-Call Forward Conversion](collectives-to-customcall.md) — the sibling path that owns every collective *except* collective-permute (Part 4.3)
- [Collective Stream-ID & Channel-ID Family](collective-stream-channel-id.md) — `hlo_query::NextChannelId` and channel-id assignment
- [AllReduce/ReduceScatter/AllGather Combiners](collective-combiners.md) — downstream combiner that may fuse the emitted all-gather
- [AllReduce / ReduceScatter Dynamic-Slice Rewrites](allreduce-dynslice-rewrites.md) — the sibling replica-indexed-dynamic-slice rewrite family (Part 4.7)
