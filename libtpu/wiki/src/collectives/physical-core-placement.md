# Physical-Core Placement

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`, `.rodata` VMA == file offset `0x84a0000`).
> **Status:** Reimplementation-grade · **Evidence grade:** Confirmed (byte-anchored) — the `physical_core_indices` write path (`AddCollectivePhysicalCoreIndicesHelper`), the read-back (`GetPhysicalCoreIndices`), and the `tensor_split_mode==2` classifier (`TensorSplitPerCoreClassifier`) were each cross-checked against the IDA decompile of all three functions; two residual sub-layouts marked **[LOW]** below · **Part XIII — On-Pod Collectives & Barriers** / SparseCore-offload collectives · [back to index](../index.md)

## Abstract

This page documents the **physical-core placement datapath** of a SparseCore-offloaded embedding collective: how a collective's logical colors land on a concrete set of physical SparseCore (SC) cores, and how the per-core *tensor-split* (`tensor_split_mode == 2`) emission is keyed. It owns three byte-exact mechanisms:

1. the **`physical_core_indices` fill** — `AddCollectivePhysicalCoreIndices` (`@0x1c868500`), a recursive async/fusion walker, and its writer `AddCollectivePhysicalCoreIndicesHelper` (`@0x1c868920`), which copies the chosen-core `absl::Span<long const>` *verbatim* (truncated `long`→`int32`) into the per-collective `*OffloadConfig` variant's `repeated int32 physical_core_indices` (proto field 4);
2. the **`physical_core_indices` index-array layout + read-back** — the proto2 `RepeatedField<int32>` at `[variant+0x18..+0x20]`, read back (widened to `long`) by `GetPhysicalCoreIndices` (`@0x1c8692e0`) into a `StatusOr<absl::InlinedVector<long,4>>`;
3. the **`tensor_split_mode==2` per-core emission key** — `TensorSplitPerCoreClassifier` (`@0x13379de0`), which maps a per-color `UniDirRingStrategy` to a classification key `(tensor-split bool) | (axis classcode 0/2/4 | D2D core count)`, and the `CanBeCombined` (`@0x13379d60`) predicate that merges per-color strategies into one per-SC-core partial-tensor op.

The **core-selection policy** — *which* cores are eligible and the order in which `SelectCores` builds the chosen list — is **not** owned here; it lives on **[SC Core-Selection (Offload)](sc-core-selection-offload.md)** and **[SC Core Selection](../sparsecore/sc-core-selection.md)**. The **ND-plane / tensor-split-factor** derivation lives on **[Tensor-Split / ND-Plane](tensor-split-ndplane.md)**. This page picks up at the moment the chosen-core `Span` exists and follows it into the proto, and picks up the split-mode-2 emission at the classifier that keys it.

Contract of the placement datapath as observed in the binary:

- `physical_core_indices` is the **sorted set of chosen physical SC core IDs** produced by `SparseCoreQueueAssignment::SelectCores`, numerically `__sort`ed by the caller `AssignQueueIDsToAsyncStart` (`@0x10fdf480`), then handed to `AddCollectivePhysicalCoreIndices` as an `absl::Span<long const>`.
- The fill is **purely a copy**: `AddCollectivePhysicalCoreIndicesHelper` truncates each `long` to `int32` and appends it to the variant's `RepeatedField<int32>` — there is no per-element transformation, scaling, or re-indexing.
- The **same `Span` drives two consumers**: it is copied into `physical_core_indices` *and* appended to the `MegaChipParallelismConfig` repeated-long field — the placement and the mega-chip parallelism share one selected-core list.
- The walker is **recursive over fusion bodies**: an async-wrapped fusion's `called_computations` are walked, and every sub-collective opcode in `{6, 9, 12, 86, 93}` receives the *same* `Span`.
- `TensorSplitPerCoreClassifier` returns a per-SC-core **grouping/sort discriminant**, not an instruction; for `IciDim::kCoresOnChip` the discriminant is the `D2DUniDirRingStrategy::core_classifier_` count — the megacore cross-core split (`LogicalDevicesPerChip(SparseCore) == 2`), the SC analog of the dense TensorCore megacore data-split.

## At a glance

| Aspect | Value (byte-anchored) |
|--------|----------------------|
| Walker (recursive) | `AddCollectivePhysicalCoreIndices` `@0x1c868500` → `bool` |
| Writer | `…Helper` `@0x1c868920` (anon-ns) → `bool` |
| Reader | `GetPhysicalCoreIndices` `@0x1c8692e0` → `StatusOr<InlinedVector<long,4>>` |
| Producer (Span) | `AssignQueueIDsToAsyncStart` `@0x10fdf480` (call `@0x10fdff30`) |
| Sibling producer | `OffloadCollective` `@0x10fc75e0` (`SetMegaChipParallelism` + `AddCollective…`) |
| Index array | proto2 `RepeatedField<int32>` at `[variant+0x18]` flags / `[+0x1c]` size / `[+0x20]` `Rep*` (data at `Rep+0x8`) |
| Oneof variant ptrs | AR `[CollectiveOffloadConfig+0x18]` · AG `[+0x20]` · RS `[+0x28]` (discriminant dword `[+0x10]`) |
| Walker opcode set | `{6, 9, 12, 86, 93}` (jump table `@0xb438098`, mask `0x1240` + explicit `cmp 0x56/0x5d`) |
| Read-back error lines | `523` / `527` / `565` (`backend_config_util.cc`), all `MakeErrorImpl<13>` (INTERNAL) |
| Split-2 classifier | `TensorSplitPerCoreClassifier` `@0x13379de0` → `(byte[s+0x43]) \| (axis 0/2/4 \| D2D count)` |
| `ici_dim()` | vtable `+0x68` = `byte[s+0x80] \| 0x100` (`AutoOr`-engaged `IciDim`) |
| Combine key | `core_count()` (vtable `+0x30` = `qword[s+0x88]`) via `CanBeCombined` `@0x13379d60` |
| Reader consumer | `CheckCoreAssignmentConsistency` `@0x1c869cc0` |

---

## 1. The `physical_core_indices` field — array layout

`physical_core_indices` is **proto field 4**, a `repeated int32`, carried inside each per-collective `*OffloadConfig` variant (`AllGatherOffloadConfig` / `AllReduceOffloadConfig` / `ReduceScatterOffloadConfig`), which are byte-identical (see **[SC-Offload Config Builder](sc-offload-config-builder.md)** §4). The field is a standard proto2 `RepeatedField<int32>` and occupies four offsets inside the variant:

| Offset (rel. variant) | Field | Meaning |
|-----------------------|-------|---------|
| `[variant+0x10]` | `int32` has-bits | bit0 = `physical_core_indices` present |
| `[variant+0x18]` | `RepeatedField` flags \| arena tag | low bit = "has heap `Rep`" |
| `[variant+0x1c]` | `int32` `current_size` | element count |
| `[variant+0x20]` | `Rep*` | element data starts at `Rep+0x8` (`int32[]`) |

> **[CONFIRMED]** Both the writer and the reader agree on this layout. In `…Helper @0x1c868920` the AR fill clears `*(_DWORD*)(v9+28)` (= `[+0x1c]` size) and `*(_BYTE*)(v9+16) &= ~1u` (= `[+0x10]` has-bit), then per element stores `v16[(int)v14 + 2] = v17` (= `Rep+0x8 + idx*4`), sets `*(_DWORD*)(v9+28) = size+1`, and `*(_BYTE*)(v9+16) |= 1u`. In `GetPhysicalCoreIndices @0x1c8692e0` the read picks `Rep*` from `*(_QWORD*)(v9+32)` (`[+0x20]`) when the heap-bit `*(_BYTE*)(v9+24) & 1` (`[+0x18]`) is set, count from `*(int*)(v9+28)` (`[+0x1c]`), and reads element data from `Rep+0x8`.

> **[CONFIRMED]** This **supersedes** an earlier working hypothesis that field 4 lived at `[variant+0x40]/[+0x44]`. The decompile shows `[+0x28]` is a separate cleared scalar (`*(_DWORD*)(v_variant+0x28)` zeroed by the ctor, not touched by the fill), and the repeated `int32` is unambiguously at `[+0x18..+0x20]`. The `[+0x40]/[+0x44]` reading does not survive the byte trace.

### 1.1 The oneof variant select

`physical_core_indices` is not a top-level `*OffloadConfig` field reached directly — it is reached through the `CollectiveOffloadConfig` oneof wrapper. Both the writer and the reader select the variant by the **discriminant dword** at `[CollectiveOffloadConfig+0x10]`:

| Discriminant bit | Variant | Variant ptr (rel. `CollectiveOffloadConfig`) |
|------------------|---------|----------------------------------------------|
| `& 0x1` | `AllReduceOffloadConfig` | `[+0x18]` (`*((_QWORD*)cfg+3)`) |
| `& 0x2` | `AllGatherOffloadConfig` | `[+0x20]` (`*((_QWORD*)cfg+4)`) |
| `& 0x4` | `ReduceScatterOffloadConfig` | `[+0x28]` (`*((_QWORD*)cfg+5)`) |
| `& 0x8` | `RaggedAllToAllOffloadConfig` | `mutable_ragged_all_to_all_offload_config` |
| `& 0x10` | `AllToAllOffloadConfig` | `mutable_all_to_all_offload_config` |

The two `*_to_all` variants are handled by a templated lambda `$_0<RaggedAllToAllOffloadConfig>` / `$_0<AllToAllOffloadConfig>` rather than the inline AR/AG/RS loop, but the field layout (proto2 `RepeatedField<int32>` at the same relative offsets) is identical.

> **[CONFIRMED]** In `…Helper`, the discriminant is `v8 = *((_DWORD*)v6 + 4)` (= `[cfg+0x10]`), tested `& 2` (AG, ptr `*((_QWORD*)v6+4)`), `& 1` (AR, `*((_QWORD*)v6+3)`), `& 4` (RS, `*((_QWORD*)v6+5)`), `& 8` (ragged → `mutable_ragged_all_to_all_offload_config(v6)` + `$_0`), `& 0x10` (a2a → `mutable_all_to_all_offload_config(v6)` + `$_0`). `GetPhysicalCoreIndices` selects the identical bits (`v60 & 2` gates the "no collective offload config" error; then the same `& 2 / & 1 / & 4 / & 8 / & 0x10`).

---

## 2. The fill path — `AddCollectivePhysicalCoreIndices` + `…Helper`

### 2.1 The recursive walker (`@0x1c868500`)

`AddCollectivePhysicalCoreIndices(HloInstruction*, absl::Span<long const>) → bool` is **not** the writer; it is a recursive collective-walker that forwards the *same* `Span` to every sub-collective:

```text
AddCollectivePhysicalCoreIndices(hlo, Span indices):
  if !hlo->IsAsynchronous()                       (@0x1e592520)            → return 1   // no-op
  wrapped = hlo->async_wrapped_instruction()       (@0x1e5aa300)
  if opcode_byte[wrapped+0xc] != 0x1b (fusion)                              → return 1
  for each instr in wrapped->called_computations() (@0x1e5885a0):
     switch opcode_byte[instr+0xc]  (jump table @0xb438098, opcode-6 index):
        6 / 9 / 12  → AddCollectivePhysicalCoreIndicesHelper(instr, indices)   // mask 0x1240
        0x56 (86)   → AddCollectivePhysicalCoreIndicesHelper(instr, indices)   // explicit cmp
        0x5d (93)   → AddCollectivePhysicalCoreIndicesHelper(instr, indices)   // explicit cmp
        0x28 (40)   → recurse into instr->called_computations()
        other       → skip
  return 1
```

The opcode set `{6, 9, 12, 86, 93}` is the SparseCore async-collective plus all-reduce / all-gather / reduce-scatter / all-to-all family. Opcode `0x28` (fusion) triggers a recursive descent, so a nested fusion body is fully walked. The walk is depth-first; the same `Span` reaches every matched leaf.

> **[CONFIRMED — symbol + structure]** The walker symbol `…AddCollectivePhysicalCoreIndicesEPNS_14HloInstructionEN4absl4SpanIKlEE @0x1c868500` is present in `*_functions.json`, with its two source call sites in `AssignQueueIDsToAsyncStart` (`@0x10fdff30`) and `OffloadCollective` (`@0x10fc871c`). The jump-table form (`@0xb438098`, `0x23` dwords, opcode−6 index; mask `0x1240` = bits 6/9/12; explicit `cmp eax, 0x56` / `0x5d`; recurse target for `0x28`) is byte-traced from the disassembly. The Helper call forwards the *same* `(rsi=data, rdx=count)` pair to each matched instruction.

### 2.2 The writer (`…Helper @0x1c868920`)

```text
AddCollectivePhysicalCoreIndicesHelper(hlo, Span data, count) → bool:
  bc = GetBackendConfig(hlo)            (@0x1c8664a0)         // absent → return 1
  cfg = bc.CollectiveOffloadConfig      ([BackendConfig+0x178], default-constructed if missing)
  select variant by discriminant [cfg+0x10]   (AR/AG/RS/ragged/a2a, §1.1)
  // FILL (byte-identical for AR/AG/RS):
  variant.physical_core_indices.current_size = 0      ([variant+0x1c] = 0)
  variant.has-bit &= ~1                                ([variant+0x10] &= 0xfe)
  for i in 0 .. count-1:
     v = (int32) data[i]                               // movsxd/trunc: long → int32
     RepeatedField<int32>::Add(v)                      // GrowNoAnnotate @0xe68d9e0 if full;
                                                        //   store [Rep+0x8 + size*4]
     variant.physical_core_indices.current_size = i+1  ([variant+0x1c] = i+1)
     variant.has-bit |= 1                              ([variant+0x10] |= 1)
  // FINALIZE:
  cloned = CloneBackendConfigProto(bc)                 (@0x1e60dac0)
  BackendConfigWrapper::operator=([hlo+0x68], cloned)  (@0x1e60de40)
  return 1
```

The fill is a **verbatim copy**: the only transformation is the `long`→`int32` truncation that proto field 4's `int32` element type forces. The selected-core IDs the producer chose are written unchanged, in the order the caller sorted them.

> **[CONFIRMED]** The AR fill (`v8 & 1` branch) in the decompile: `*(_DWORD*)(v9+28) = 0`, `*(_BYTE*)(v9+16) &= ~1u`, loop reads `v17 = *(_DWORD*)(v11 + 8*v14)` (truncating the 8-byte `Span` element to a 4-byte store), `GrowNoAnnotate<…>(v13, v9, …)` on full, stores `v16[(int)v14 + 2] = v17`, sets `*(_DWORD*)(v9+28) = v18` (size+1) and `*(_BYTE*)(v9+16) |= 1u`. AG (`v8 & 2`) and RS (`v8 & 4`) branches are structurally identical (only the variant ptr differs). Finalize: `CloneBackendConfigProto((xla*)v44, …)` then `BackendConfigWrapper::operator=((char*)v53 + 104, …)` = `[hlo+0x68]`. The success VLOG string `"backend_config_util::AddCollectivePhysicalCoreIndices( inst, sparse_core_ids) is OK"` is present in `.rodata`.

---

## 3. The read-back — `GetPhysicalCoreIndices` (`@0x1c8692e0`)

```text
GetPhysicalCoreIndices(const HloInstruction* hlo) → StatusOr<InlinedVector<long,4>>:
  bc = GetBackendConfig(hlo)
  if !bc                          → MakeErrorImpl<13>("No backend config found", line 523)
  if !(disc & 2 present)          → MakeErrorImpl<13>("No collective offload config found", line 527)
  select the SAME variant by the SAME discriminant bits (§1.1)
  if variant absent / field empty → MakeErrorImpl<13>("No physical core indices found", line 565)
  count = [variant+0x1c]
  Rep*  = [variant+0x20]  (or inline [variant+0x18] when heap-bit clear)
  for each int32 at Rep+0x8 + i*4:  widen int32 → long   (vpmovsxdq, 4-wide)
  build InlinedVector<long,4>:  count <= 4 → inline; >= 9 → exact heap; 5..8 → cap-8 heap
  return StatusOr OK { sret+0=1, sret+8=data, sret+0x10=size, sret+0x18=cap }
```

The reader is the exact inverse of the writer: same oneof variant select, same `[+0x1c]`/`[+0x20]` field offsets, widening each stored `int32` back to a `long`. The result type is an `absl::InlinedVector<long,4>` — counts ≤ 4 stay in the inline buffer; larger lists go to a heap allocation (`_size_returning_new`). All three error paths are `MakeErrorImpl<13>` (gRPC code 13 = INTERNAL) in `backend_config_util.cc`. The reader's consumer is `CheckCoreAssignmentConsistency` (`@0x1c869cc0`), which re-validates that the placed cores agree across the collective's instructions.

> **[CONFIRMED]** The three error strings + source line numbers read byte-exact from the decompile: `"No backend config found"` (line **523**), `"No collective offload config found"` (line **527**), `"No physical core indices found"` (line **565**), all via `absl::status_internal::MakeErrorImpl<13>(…, "platforms/xla/service/jellyfish/lowering/backend_config_util.cc")`. The `int32`→`long` widen is the `vpmovsxdq` 4-wide unrolled loop; the `cmp v12,5` (count<5 inline) / `v12 >= 9` (exact) split selects the `InlinedVector<long,4>` storage. The OK sret layout (`[+0]=1` tag, `[+8]` data, `[+0x10]` size, `[+0x18]` cap) is byte-confirmed.

---

## 4. The `Span` producer — where the chosen cores come from

The `Span` the fill copies is built one step upstream by `SparseCoreQueueAssignment::AssignQueueIDsToAsyncStart` (`@0x10fdf480`). The full **selection policy** — `GetAllowedCores`, the five-phase `SelectCores` greedy filter, the cost tie-break — is owned by **[SC Core-Selection (Offload)](sc-core-selection-offload.md)** and **[SC Core Selection](../sparsecore/sc-core-selection.md)**; here is only the hand-off into this page's fill:

```text
AssignQueueIDsToAsyncStart(hlo):
  mega = GetMegaChipParallelism(hlo)              (@0x1c867b00)  // StatusOr<InlinedVector<long,4>>
  split_axis0 = mega[0] >> 1                       // megacore: 2 cores per chip
  allowed = GetAllowedCores(hlo)                  (@0x10fda3c0)  // btree_set<long> candidate pool
  chosen  = SelectCores(hlo, allowed, …)          (@0x10fdc4e0)  // unsorted, {phase, cost} order
  __sort(chosen)                                  (@0x10fdfde7)  // ASCENDING numeric core ID
  AddCollectivePhysicalCoreIndices(hlo, Span{chosen.data, chosen.size})   (@0x10fdff30)
  // the SAME chosen array is ALSO appended to MegaChipParallelismConfig (repeated long, loop @0x10fdfe80)
```

The critical observation for this page: `SelectCores` returns the cores in **build order** ({same-ND-plane, data-dep, assignment-group, not-different-plane, fallback} × ascending cost), but the caller **numerically `__sort`s** the list *before* the fill. Therefore `physical_core_indices` is always stored in **ascending physical-core-ID order**, not in selection-priority order — the selection order is consumed internally and is not visible in the proto.

The sibling producer `SparseCoreCollectiveOffload::OffloadCollective` (`@0x10fc75e0`) pairs the same chosen `Span` between `SetMegaChipParallelism` (`@0x1c867680`, `@0x10fc86f1`) and `AddCollectivePhysicalCoreIndices` (`@0x10fc871c`) — confirming the placement list and the mega-chip parallelism list are one and the same array.

> **[CONFIRMED — symbol + cross-call]** `AssignQueueIDsToAsyncStart`, `GetMegaChipParallelism`, `SetMegaChipParallelism`, and `OffloadCollective` are present in `*_functions.json`; the `AddCollectivePhysicalCoreIndices` call site `@0x10fdff30` (and `@0x10fc871c`) and the `__sort @0x10fdfde7` are byte-traced. **[LOW]** The exact per-core numeric ID `SelectCores` assigns (the logical-color → physical-core bijection / tie-break arithmetic) is the selection policy's concern and is documented (as a 5-phase greedy filter, not a closed-form scorer) on the core-selection pages — this page only needs that the output is the sorted ID set.

---

## 5. The `tensor_split_mode==2` per-core emission

When the offload substrate adopts the split-tensor mode (`tensor_split_mode == 2`, the "Adopting split tensor mode." path gated upstream — see **[Tensor-Split / ND-Plane](tensor-split-ndplane.md)**), a collective's per-color rings are emitted as **per-SC-core partial-tensor ops**. The grouping that decides which colors collapse into one per-core op is keyed by `TensorSplitPerCoreClassifier`.

### 5.1 `TensorSplitPerCoreClassifier` (`@0x13379de0`)

```text
TensorSplitPerCoreClassifier(UniDirRingStrategy* s) → long:
  split_bit = (byte[s+0x43] != 0)                  // the tensor-split / per-core bool, bit0
  switch (s->ici_dim() & 0x1ff):                   // ici_dim() = vtable+0x68 = byte[s+0x80] | 0x100
     0x100  IciDim::kX           → class = 0
     0x101  IciDim::kY           → class = 2
     0x102  IciDim::kZ           → class = 4
     0x103  IciDim::kCoresOnChip → d2d = dynamic_cast<D2DUniDirRingStrategy*>(s)
                                    if !d2d  → FATAL "Expected D2DUniDirRingStrategy if the
                                               ici_dim is kCoresOnChip"   (.cc line 156)
                                    class = d2d->core_classifier_   ([d2d+0x90])
                                    CHECK class >= 0    "core_classifier_ >= 0"   (.h line 375)
     default → FATAL CHECK "strategy->ici_dim() == IciDim::kCoresOnChip"   (.cc line 153)
  return class | split_bit                          // `or rbx, rcx`
```

The classifier returns a per-SC-core **key**: the low bit is the tensor-split flag, and the upper part is either an **axis classcode** (`kX → 0`, `kY → 2`, `kZ → 4` — which torus axis the per-core partial-tensor ring iterates) or, for `kCoresOnChip` (the megacore cross-core split — the split-2 datapath proper), the `D2DUniDirRingStrategy::core_classifier_` count. The `kCoresOnChip` D2D strategy is the SC analog of the dense TensorCore megacore data-split.

> **[CONFIRMED]** The decompile of `@0x13379de0` matches byte-exact: `v3 = *((_BYTE*)this + 67) != 0` (`[+0x43]`); the `ici_dim()` virtual via `*(_QWORD*)this + 104LL` (vtable `+0x68`) masked `& 0x1FF`; comparisons `0x100 → v4=0`, `0x101 → v4=2`, `0x102 → v4=4`; `0x103` → `_dynamic_cast(this, typeinfo UniDirRingStrategy, typeinfo D2DUniDirRingStrategy, 0)` then `v9 = v11[18]` (= `[+0x90]`, 18×8) with `CHECK v9 >= 0`; the final `return v4 | v2`. The three diagnostic strings + source lines are byte-exact: `"strategy->ici_dim() == IciDim::kCoresOnChip"` (`offload_collective_strategies.cc:153`), `"Expected D2DUniDirRingStrategy if the ici_dim is kCoresOnChip"` (`.cc:156`), `"core_classifier_ >= 0"` (`offload_collective_strategies.h:375`).

### 5.2 The `UniDirRingStrategy` field map

The strategy fields the classifier reads (from the `ImplicitUniDirRingStrategy` ctor `@0x1339bca0` base and the `D2DUniDirRingStrategy` ctor `@0x1339ba60` override):

| Offset | Field | Notes |
|--------|-------|-------|
| `[s+0x42]` | `bool` | ctor bool #1 |
| `[s+0x43]` | `bool` | **the tensor-split / per-core flag** the classifiers read |
| `[s+0x44]` | `byte` `RingDir` | |
| `[s+0x80]` | `byte` `IciDim` | `0 kX / 1 kY / 2 kZ / 3 kCoresOnChip`; D2D ctor hardwires `3` |
| `[s+0x88]` | `long` `core_count` | the **combine key** (`CanBeCombined`); D2D hardwires `2` |
| `[s+0x90]` | base `bool` · **D2D `long core_classifier_`** | the per-core split count; D2D CHECKs `LogicalDevicesPerChip(SC) == 2` |
| `[s+0x91]` | `bool` | |

The D2D vtable (`@0x21908db0`, vptr at `+0x10`) resolves slot `+0x30` → `core_count` (`@0x13399000` = `qword[s+0x88]`) and slot `+0x68` → `ici_dim` (`@0x13399020` = `byte[s+0x80] | 0x100`). The `| 0x100` is the `AutoOr<IciDim>`-engaged bit (the same `AutoOr` packing the offload config builder uses for `HierarchicalKind` — see **[SC-Offload Config Builder](sc-offload-config-builder.md)** §3).

> **[CONFIRMED — vtable + ctor]** `D2DUniDirRingStrategy` is present in `*_functions.json`; the vtable slot relocations (`+0x30 → 0x13399000`, `+0x68 → 0x13399020`) and the D2D ctor hardwires (`[+0x80]=3`, `[+0x88]=2`, `[+0x90]=` trailing `long` arg, `LogicalDevicesPerChip(SparseCore) == 2` CHECK) are byte-traced. **[LOW]** *Which* caller computes the `core_classifier_` `long` (i.e. whether it equals `tensor_split_factor`, `NumScOffloadDevices/LDPC`, or the megacore 2-core count) was not traced to the `AllReduceUnidirNdStrategy::TryCreate` lambda that constructs the D2D strategy — the classifier returns the stored value directly; its provenance is a tensor-split-factor concern (**[Tensor-Split / ND-Plane](tensor-split-ndplane.md)**).

### 5.3 The combine consumer — `CanBeCombined` (`@0x13379d60`)

```text
CanBeCombined(a, b) → bool:
  return a->core_count() == b->core_count()                          // vtable +0x30 = [s+0x88]
      && !FLAGS_xla_tpu_impure_coff_never_combine_colors_test_only
```

The classifier key from §5.1 plus `CanBeCombined` are what the `DefaultStrategyCombiner<*>` family (e.g. `@0x133a4660` / `@0x133a3c60`) and `MoreRelaxedStrategyCombiner` use to merge per-color `UniDirRingStrategy` objects into one per-SC-core partial-tensor emission: colors with the same classifier key *and* equal `core_count` (and the never-combine flag off) collapse together. The simpler sibling `DimPerCoreClassifier` (`@0x13379dc0`) keys on the tensor-split bool alone (`byte[s+0x43] != 0`).

> **[CONFIRMED]** `CanBeCombined @0x13379d60`: `v3 = (*(this->vtable+48))(this, a2, a3)` compared to `(*(a2->vtable+48))(a2)` (the `core_count` virtual at `+0x30`), gated by `!FLAGS_xla_tpu_impure_coff_never_combine_colors_test_only` (flag-impl cached at `qword_2231E730`, read via `FlagImpl::ReadOneBool`). `DimPerCoreClassifier @0x13379dc0` is literally `return *((_BYTE*)this + 67) != 0;` (= `byte[+0x43]`). Both byte-exact. The `CanBeCombined` overloads for `SinglePhaseRSTransferStrategy` / `SinglePhaseAGTransferStrategy` are present as separate symbols. **[LOW]** The `DefaultStrategyCombiner<*>` merge-loop body (how the key actually groups colors and which combined `IciStrategyRingConfig` the merged per-SC-core op emits) was confirmed by call-edge to `CanBeCombined`/the classifier but not expanded to the per-field emission.

---

## 6. Placement + split-mode-2 — relationship table

| Quantity | Source | Role |
|----------|--------|------|
| chosen physical cores (sorted) | `SelectCores` `@0x10fdc4e0` → `__sort` (caller) | the array written to `physical_core_indices` |
| `physical_core_indices` write | `AddCollectivePhysicalCoreIndices` `@0x1c868500` + `…Helper` `@0x1c868920` | verbatim `Span`→`RepeatedField<int32>` copy |
| index-array layout | `[variant+0x1c]`=size · `[+0x20]`=`Rep*` (data `Rep+0x8`) | proto2 `RepeatedField<int32>` (field 4) |
| read-back | `GetPhysicalCoreIndices` `@0x1c8692e0` | `StatusOr<InlinedVector<long,4>>` (`int32`→`long`) |
| read-back consumer | `CheckCoreAssignmentConsistency` `@0x1c869cc0` | cross-instruction core agreement |
| shared list | `MegaChipParallelismConfig` repeated long (`@0x10fdfe80`) | same chosen array → mega-chip parallelism |
| per-core class key | `TensorSplitPerCoreClassifier` `@0x13379de0` | `(split bool) \| (axis 0/2/4 or D2D count)` |
| `ici_dim` | `byte[s+0x80] \| 0x100` (vtable `+0x68`) | `kX` / `kY` / `kZ` / `kCoresOnChip` |
| D2D per-core split | `D2DUniDirRingStrategy::core_classifier_` `[s+0x90]` (LDPC(SC)==2) | megacore cross-core split count |
| combine key | `core_count()` `[s+0x88]` (vtable `+0x30`) | `CanBeCombined` grouping |

---

## 7. Verification notes

> **[CONFIRMED]** Cross-checked against the IDA decompile of `libtpu.so` v0.0.40 (build-id `89edbbe8…`):
> - **Writer** (`…Helper @0x1c868920`) — `GetBackendConfig` → `v43 == 1` gate; the `CollectiveOffloadConfig` oneof discriminant `v8 = *((_DWORD*)v6+4)` (`[cfg+0x10]`) with bits `2/1/4/8/0x10` and variant ptrs `*((_QWORD*)v6+4)/+3/+5` (= `[+0x20]/[+0x18]/[+0x28]`); the clear (`[+0x1c]=0`, `[+0x10]&=~1`) + the verbatim `long`→`int32` copy loop (`GrowNoAnnotate @0xe68d9e0`, store `[Rep+0x8+i*4]`, `[+0x1c]++`, `[+0x10]|=1`); the `CloneBackendConfigProto` + `BackendConfigWrapper::operator=([hlo+0x68])` write-back — all byte-exact.
> - **Reader** (`GetPhysicalCoreIndices @0x1c8692e0`) — the same oneof bits; `Rep*`=`[+0x20]`, count=`[+0x1c]`, data `Rep+0x8`; `vpmovsxdq` `int32`→`long`; the `cmp 5` inline / `>= 9` exact heap `InlinedVector<long,4>` split; the three `MakeErrorImpl<13>` strings at lines `523/527/565`; the OK sret layout `+0/+8/+0x10/+0x18` — all byte-exact.
> - **Field-offset agreement** — writer `[+0x1c]/[+0x10]` and reader `[+0x1c]/[+0x18]/[+0x20]` independently confirm the `RepeatedField<int32>` at `[variant+0x18..+0x20]`; the `[+0x40]/[+0x44]` reading is disproven.
> - **Classifier** (`TensorSplitPerCoreClassifier @0x13379de0`) — `byte[+0x43]` read; `ici_dim()` vtable `+0x68` masked `& 0x1FF`; `0x100/0x101/0x102 → 0/2/4`; `0x103` → `dynamic_cast<D2DUniDirRingStrategy>` + `[+0x90]` `core_classifier_` (`CHECK >= 0`); default FATAL; `return class | bool`. Source lines `153/156` (`.cc`) and `375` (`.h`) byte-exact.
> - **Combine** (`CanBeCombined @0x13379d60`, `DimPerCoreClassifier @0x13379dc0`) — `core_count()` equality via vtable `+0x30`, gated by `FLAGS_xla_tpu_impure_coff_never_combine_colors_test_only`; `DimPerCoreClassifier` = `byte[+0x43] != 0` — both byte-exact.
> - **Symbols** — `AddCollectivePhysicalCoreIndices`, `…Helper` (+ `$_0<Ragged…>` / `$_0<AllToAll…>` lambdas), `GetPhysicalCoreIndices`, `AssignQueueIDsToAsyncStart`, `OffloadCollective`, `GetMegaChipParallelism`, `SetMegaChipParallelism`, `D2DUniDirRingStrategy`, `CheckCoreAssignmentConsistency`, and the `CanBeCombined` overloads all present in `*_functions.json`.
>
> **[LOW]** Confirmed by structure / call-edge but not fully numeric-decoded:
> - The `SelectCores` per-candidate placement arithmetic (the logical-color → physical-core bijection / tie-break) — owned by the core-selection pages; here only the *sorted output* matters.
> - The `D2DUniDirRingStrategy::core_classifier_` construction site (which caller computes the stored `long`, and whether it equals `tensor_split_factor` / `NumScOffloadDevices`-derived / the megacore 2-core count) — the classifier returns it directly.
> - The `DefaultStrategyCombiner<*>` merge-loop body (how the classifier key groups colors into the emitted per-SC-core `IciStrategyRingConfig`).

---

## Cross-References

**SparseCore-offload placement & selection**
- [SC Core-Selection (Offload)](sc-core-selection-offload.md) — `GetAllowedCores` candidate mask + the cost/resource model that feeds `SelectCores`
- [SC Core Selection](../sparsecore/sc-core-selection.md) — the five-phase `SelectCores` greedy filter that produces the chosen-core list this page copies
- [Tensor-Split / ND-Plane](tensor-split-ndplane.md) — `tensor_split_factor` / `NumScOffloadDevices` + `NDPlaneInfo`, the gate that selects `tensor_split_mode==2`

**Config + substrate**
- [SC-Offload Config Builder](sc-offload-config-builder.md) — the `*OffloadConfig` struct family carrying `physical_core_indices`, and the `AutoOr` packing the `IciDim` read mirrors
- [On-Pod Collectives — Section Map](overview.md) — the substrate split and the SC-offload gate

**Sibling subsystems**
- [HierarchicalKind](hierarchical-kind.md) — the `AutoOr<bool>` flat-vs-hierarchical split the offload builder dispatches on
- [back to index](../index.md)
