# SC Core Selection

> *Every address, offset, string, and constant on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). `.text` VMA == file offset (`0xE63C000`); `.rodata` VMA == file offset (`0x84A0000`). Other versions differ.*

## Abstract

This page is the **physical-core-selection policy** the SparseCore back-end runs when it assigns an embedding/collective async op to a concrete set of physical SparseCore cores. It is owned by two member functions of `xla::jellyfish::SparseCoreQueueAssignment`: **`GetAllowedCores`** (`0x10FDA3C0`), which computes the *candidate-core mask* for one collective, and **`SelectCores`** (`0x10FDC4E0`), which turns that mask into an *ordered* core list. The product is the `absl::Span<long const>` that the caller numerically sorts and writes into the op's `physical_core_indices` backend-config field through `AddCollectivePhysicalCoreIndices` (`0x1C868500`).

The mental model a reimplementer needs is that **`SelectCores` is not a numeric scorer** — there is no closed-form `score(core)` and no core→queue bijection arithmetic anywhere in its `0x28C1`-byte body (`nm -S`). It is a **deterministic greedy priority filter**: it materializes the allowed btree-set into a flat vector, sorts that vector once into *ascending per-core cost* order (the only place the `double cost` argument is consumed), then runs **five ordered passes** over the cost-sorted candidates. Each pass appends a candidate the first time it satisfies that pass's predicate, where the predicate is evaluated against the running `vector<Info>` of *already-assigned* async collectives. A candidate's final rank is therefore `(pass index 1..5)` major, `(ascending cost)` minor. The five predicates, in order: (P1) the core already runs a collective on the **same ND plane**; (P2) an assigned op holding the core has a **data dependency** with this op (probed through an `HloReachabilityMap`); (P3) the op is a member of a **pre-determined assignment group** whose other members hold the core; (P4) the core is **not** running a collective on a **different** ND plane; (P5) **fallback** — append every remaining allowed core. P5 guarantees the selected set covers the whole allowed set; P1–P4 only fix the *order*, which determines membership after the caller truncates to the device/megachip count.

`GetAllowedCores` builds the candidate mask from two inputs intersected through two Swiss tables: the **megachip per-axis chip IDs** (`GetChipIDsFromParallelismConfig` → `GetMegaChipParallelism`) and the **per-collective scheduling resource-type IDs** `{0, 23, 24, 25, 26, 27, 28}` (`GetSparseCoreResources`: a 7-arm switch on the offload-config collective-type enum, where six arms insert a fixed constant and the enum-4 arm instead returns `AsyncTracker::GetResourceTypeForOp` on the unwrapped async op's root opcode). A per-resource **thread-local reservation budget** (read via `__tls_get_addr`, decremented per assignment, gated `>= 2`) excludes cores whose reservation for that resource is exhausted — this is the embedding-device / reserved-core effect. The output is a `btree_set<long>` of allowed SC core IDs.

The page is four units: the **end-to-end pipeline** (where selection sits between the candidate mask and the proto write), **`GetAllowedCores`** (the candidate-mask build and the reservation budget), **`SelectCores`** (the five-phase filter, the `Info` struct, the cost tie-break), and the **`tensor_split_factor` interaction** (how the *count* of selected cores is consumed downstream by the collective ring strategy). The 4:1 SC:TC ratio that bounds the per-chip core count is stated on [SparseCore Hardware Architecture](architecture.md).

For reimplementation, the contract is:

- **Selection is a two-stage producer of `physical_core_indices`, not a single scorer.** `GetAllowedCores` (mask) → `SelectCores` (order) → caller numeric `__sort` → `AddCollectivePhysicalCoreIndices`. `SelectCores` returns its vector **unsorted**; the numeric sort happens in the caller.
- **`SelectCores` is a five-phase greedy priority filter.** Pass order is `{same-plane, data-dep, assign-group, not-different-plane, fallback}`; within a pass the cost-ascending `stable_sort` is the tie-break. Membership is decided by the state of *already-assigned* collectives, not by any per-candidate number.
- **The only "cost" is a tie-break, not a placement metric.** The `double cost` argument feeds a per-core weight that the `stable_sort` comparator (`$_0`, `vucomisd`) reads to order equal-priority candidates. Lower cost is appended first.
- **`GetAllowedCores` is a resource-reservation intersection.** Allowed cores = megachip chip IDs grouped by per-collective resource-type ID `{0,23..28}`, minus the cores whose per-resource thread-local reservation budget is exhausted.
- **`tensor_split_factor` is a consumer, not a selection input.** It lives in the collective offload-config and governs how the *selected* cores are partitioned by the ring strategy; the strategy enforces `color_strategies_size() % tensor_split_factor == 0`. It does not branch inside `SelectCores` or `GetAllowedCores`.

| | |
|---|---|
| **Candidate mask** | `SparseCoreQueueAssignment::GetAllowedCores(HloInstruction*)` (`0x10FDA3C0`) → `btree_set<long,…,256>` |
| **Ordered selection** | `SparseCoreQueueAssignment::SelectCores(hlo, allowed, devcount, cost, assigned, reach, assign_groups)` (`0x10FDC4E0`) → `StatusOr<vector<long>>` |
| **Mask inputs** | `GetChipIDsFromParallelismConfig` (`0x10FDBF40`) ∩-by `GetSparseCoreResources` (`0x10FDC0A0`) |
| **Resource-type set** | `{0, 23, 24, 25, 26, 27, 28}` (`.rodata 0xAC0A8E0..0xAC0A910`, byte-read; jump table @`0xAC0A82C`) |
| **Reservation budget** | per-resource thread-local `long` (`__tls_get_addr(&qword_22048D78)`), decremented, gated `>= 2` |
| **Selection passes** | P1 same-plane · P2 data-dep (`HloReachabilityMap`) · P3 assign-group · P4 not-different-plane · P5 fallback |
| **Tie-break** | cost-ascending `stable_sort` comparator `$_0` (`vucomisd`, `0x10FE8AE0`) |
| **Caller / sink** | `AssignQueueIDsToAsyncStart` (`0x10FDF480`) → `__sort` → `AddCollectivePhysicalCoreIndices` (`0x1C868500`) → `physical_core_indices` |

---

## End-to-End Pipeline

### Purpose

Core selection is a sub-step of queue assignment. For each async-start collective, the `SparseCoreQueueAssignment` pass must decide which physical SC cores it occupies; that decision becomes a list of `long` core IDs (truncated to `int32`) stored in the op's `physical_core_indices` backend-config field, where the downstream emitter reads it. Selection itself never touches the proto — it produces an `absl::Span<long const>` that the caller commits.

### Entry Point

The chain is byte-confirmed in the caller `AssignQueueIDsToAsyncStart` (`0x10FDF480`):

```text
AssignQueueIDsToAsyncStart (0x10FDF480)               ── per-collective driver
  ├─ GetAllowedCores(hlo) (0x10FDA3C0)                ── candidate btree_set<long>          [call @ caller line ~236]
  ├─ SelectCores(hlo, allowed, devcount, cost,        ── ordered vector<long> (UNSORTED)    [call @ caller line ~406]
  │              assigned, reach, assign_groups) (0x10FDC4E0)
  ├─ std::__u::__sort<__less<long>>(begin, end)        ── numeric ascending sort of the result [caller line ~459]
  └─ AddCollectivePhysicalCoreIndices(hlo, span)       ── write physical_core_indices       [caller line ~521]
       (backend_config_util::AddCollectivePhysicalCoreIndices, 0x1C868500, absl::Span<long const>)
```

### Algorithm

```text
allowed   = GetAllowedCores(hlo)                       // btree_set<long> candidate mask
selected  = SelectCores(hlo, allowed, devcount, cost,  // 5-phase greedy filter; UNSORTED
                        assigned, reach, assign_groups)
sort(selected, ascending)                              // numeric — in the CALLER, not SelectCores
AddCollectivePhysicalCoreIndices(hlo, span(selected))  // long -> int32, write proto field
```

> **GOTCHA —** `SelectCores` returns the vector in `{phase, cost}` order, **not** numeric order. The numeric `__sort` that produces the final `physical_core_indices` lives in the caller (`0x10FDF480`), after `SelectCores` returns. A reimplementer who sorts inside the selection routine, or who treats the selection order as the proto order, will mis-rank cores when the list is later truncated to the device/megachip count — because truncation drops the *highest-numbered* cores after the caller's sort, but the *phase-and-cost* order is what decided which cores were appended at all.

### Related Functions

| Function | Address | Role |
|---|---|---|
| `SparseCoreQueueAssignment::GetAllowedCores` | `0x10FDA3C0` | candidate-core mask (btree_set) |
| `SparseCoreQueueAssignment::SelectCores` | `0x10FDC4E0` | five-phase ordered selection |
| `SparseCoreQueueAssignment::AssignQueueIDsToAsyncStart` | `0x10FDF480` | per-collective driver; calls both, then sorts |
| `backend_config_util::AddCollectivePhysicalCoreIndices` | `0x1C868500` | writes `physical_core_indices` from `Span<long const>` |

---

## `GetAllowedCores` — The Candidate-Core Mask

### Purpose

`GetAllowedCores` (`0x10FDA3C0`) is a member function (sret) with signature `GetAllowedCores(HloInstruction*) → btree_set<long,less<long>,allocator<long>,256>`. It returns the pool of physical SC core IDs `SelectCores` is allowed to choose from for one collective. The pool is the intersection of the chips the collective's megachip parallelism spans and the cores still available under the collective's scheduling-resource reservation.

### Entry Point

```text
GetAllowedCores (0x10FDA3C0)                           ── sret = btree_set<long>; rdx = hlo
  ├─ GetChipIDsFromParallelismConfig(hlo) (0x10FDBF40) ── vector<long> megachip per-axis chip IDs
  │    └─ GetMegaChipParallelism (0x1C867B00)          ── StatusOr<InlinedVector<long,4>>; per-axis split
  ├─ walk device-assignment member set                  ── Swiss-table SOO: control [hlo+0x90], slots [hlo+0x98], slot stride 0x68 (13 qwords)
  │    └─ GetSparseCoreResources(member-op) (0x10FDC0A0)── btree_set<long> resource-type IDs {0,23..28}
  ├─ flat_hash_map<resource_id, btree_set<chip_id>>     ── @ policy global 0x2181D940 (group chips by resource)
  ├─ flat_hash_map<chip_id, refcount>                   ── @ policy global 0x21639C10 (per-chip occupancy)
  ├─ per-resource thread-local budget                   ── __tls_get_addr(&qword_22048D78); dec; gate `>= 2`
  └─ second pass over [hlo+0xC0]                         ── re-apply budget; assemble output btree_set
```

### Algorithm — the two inputs

**`GetChipIDsFromParallelismConfig`** (`0x10FDBF40`) reads `GetMegaChipParallelism` (`0x1C867B00`) — the same proto reader the queue-assignment driver uses — and copies each per-axis `MegaChipParallelism` value into a `RepeatedField<long>`. An empty `StatusOr` is a hard error. So the "chip IDs" are the per-axis megachip split list, not a device list.

**`GetSparseCoreResources`** (`0x10FDC0A0`) returns the `btree_set<long>` of AsyncTracker scheduling resource-type IDs the collective occupies, byte-confirmed:

```c
function GetSparseCoreResources(op):                   // 0x10FDC0A0
    if op.opcode == 0x11 /* custom-call */:
        cfg = GetSparseCoreConfig(op)                  // 0x1C868D20 (backend_config_util)
        if (cfg.has_type() /* type hasbit */):         // byte-exact: `(cfg_byte[0x10] & 4) != 0`
            switch (collective_type_enum - 1) of {0..6}:  // `dec eax; cmp 6; ja default`, jump table @0xAC0A82C
                case 0 (enum 1): insert 28 (.rodata 0xAC0A910)
                case 1 (enum 2): insert 23 (.rodata 0xAC0A8E8)
                case 2 (enum 3): insert 24 (.rodata 0xAC0A8F0)
                case 3 (enum 4):                       // NOT a constant — derive from the wrapped op:
                    body = op.async_wrapped_instruction()  // 0x1E5AA300
                          .called_computations()           // 0x1E5885A0  (root instruction)
                    insert AsyncTracker::GetResourceTypeForOp(root.opcode)  // 0x13612240
                case 4 (enum 5): insert 25 (.rodata 0xAC0A8F8)
                case 5 (enum 6): insert 26 (.rodata 0xAC0A900)
                case 6 (enum 7): insert 27 (.rodata 0xAC0A908)
                default:         insert 0  (.rodata 0xAC0A920)
        // else (type hasbit clear): insert 0 (.rodata 0xAC0A920)
    else:  // not a custom-call
        insert 0 (.rodata 0xAC0A8E0)                   // single resource-type 0
    return result                                      // btree_set<long>, insert_hint_unique
```

The resource-type `long` constants were read directly out of `.rodata` (VMA == file offset): `0xAC0A8E0 = 0`, `0xAC0A8E8 = 23`, `0xAC0A8F0 = 24`, `0xAC0A8F8 = 25`, `0xAC0A900 = 26`, `0xAC0A908 = 27`, `0xAC0A910 = 28`, and the all-zero slots `0xAC0A918 = 0` / `0xAC0A920 = 0` (the `default` / hasbit-clear arm). Each non-zero value is a distinct AsyncTracker scheduling resource; a core can hold one collective per resource per budget unit, which is what restricts the candidate set. Note the structure: **six** switch arms (enum 1,2,3,5,6,7) insert a fixed constant `{28,23,24,26,27}` ∪ `{25}`; the **enum-4 arm** does *not* insert a constant — it unwraps the async op (`async_wrapped_instruction` → `called_computations` root) and inserts whatever `AsyncTracker::GetResourceTypeForOp(root.opcode)` returns. The **not-a-custom-call** path and the **default / hasbit-clear** paths insert the single resource-type `0`.

### Algorithm — the mask build and the reservation budget

The body walks the device-assignment / replica-group structure and accumulates the candidate pool through two Swiss tables:

```c
function GetAllowedCores(hlo):                         // 0x10FDA3C0
    chips     = GetChipIDsFromParallelismConfig(hlo)   // megachip per-axis chip IDs
    by_res    = flat_hash_map<long, btree_set<long>>{} // resource_id -> chip_set   (policy @0x2181D940)
    occupancy = flat_hash_map<long, long>{}            // chip_id     -> refcount   (policy @0x21639C10)
    budget    = __tls_get_addr(&qword_22048D78)        // &(per-resource thread-local reservation budget)

    for assignment in device-assignment (Swiss-table SOO @[hlo+0x90]/[hlo+0x98], slot stride 0x68):
        resources = GetSparseCoreResources(member-op)  // {0,23..28}
        for (res, chip) in (resources × chips):
            v = (*budget)--                            // post-decrement the in-place TLS long
            if v >= 2:                                 // gate (pre-decrement value): chip still reservable
                by_res[res].insert(chip)               // insert_hint_unique
                occupancy[chip] += 1                   // inc [slot+8]

    // second pass re-applies the budget over [hlo+0xC0]
    result = btree_set<long>{}
    for chip surviving the budget across by_res / [hlo+0x70] device count:
        result.insert(chip)                            // internal_emplace / insert_hint_unique
    return result
```

> **NOTE —** the budget gate is the **post-decrement-then-compare** pattern `v = (*budget)--; if (v >= 2)` at two sites (one per map-fill pass; the TLS `long` is loaded via `__tls_get_addr(&qword_22048D78)`, post-decremented, then tested `>= 2`). The **writer that seeds** the thread-local `long` — and therefore its initial value — is not visible in this function. The reservation is the embedding-device / reserved-core knob: it is the mechanism by which cores reserved for embedding devices (the `sc_dev − num_embedding_devices` split) are excluded from the candidate pool; whether the seed is `NumEmbeddingDevices`, a fixed cores-per-resource cap, or `LogicalDevicesPerChip(SC)` is not determined here. See [SC Queue Assignment & Reservation](sc-queue-assignment-reservation.md) for the resource→limit map this budget is part of.

> **GOTCHA —** the two map globals are *policy* singletons, not per-call state: `flat_hash_map<long, btree_set<long,…,256>>::GetPolicyFunctions()::value` at `0x2181D940` and `flat_hash_map<long, long>` at `0x21639C10`. The btree empty-node sentinel is `0x2181D930`. A reimplementer instantiating these maps must match the `btree_set<...,256>` node fan-out (256) for the resource→chip-set value type; the `256` is part of the type and the btree-node walk stride depends on it.

### Function Map

| Function | Address | Role |
|---|---|---|
| `SparseCoreQueueAssignment::GetAllowedCores` | `0x10FDA3C0` | candidate-mask build (sret btree_set) |
| `(anon)::GetChipIDsFromParallelismConfig` | `0x10FDBF40` | megachip per-axis chip IDs → `vector<long>` |
| `(anon)::GetSparseCoreResources` | `0x10FDC0A0` | per-collective resource-type IDs `{0,23..28}` |
| `GetMegaChipParallelism` | `0x1C867B00` | `StatusOr<InlinedVector<long,4>>` per-axis split |
| `backend_config_util::GetSparseCoreConfig` | `0x1C868D20` | offload-config read (type enum + hasbit) |
| `AsyncTracker::GetResourceTypeForOp` | `0x13612240` | enum-4 switch arm: resource-type from unwrapped async-op root opcode |
| reservation budget | `__tls_get_addr(&qword_22048D78)` | per-resource exclusion counter |

---

## `SelectCores` — The Five-Phase Greedy Filter

### Purpose

`SelectCores` (`0x10FDC4E0`) is a member function (sret) returning `StatusOr<vector<long>>`. It receives the allowed btree-set, the device count, a `double cost`, the running `vector<Info>` of already-assigned collectives, the `HloReachabilityMap`, and the assignment-group vector, and produces the cores in greedy-priority order. Demangled signature (from the symbol table):

```text
SelectCores(HloInstruction const* hlo,
            btree_set<long, less<long>, allocator<long>, 256> const& allowed,
            long devcount, double cost,
            vector<Info>& assigned,
            HloReachabilityMap& reach,
            vector<vector<HloInstruction*>>& assign_groups)
    -> StatusOr<vector<long>>
```

### The `Info` Struct (sizeof `0x60`)

Each element of the `assigned` vector describes one already-placed async collective. The struct is `0x60` bytes (the element stride is computed as `lea rax,[rax+rax*2]; shl rax,5`, i.e. `×3 ×32 = ×96`). Byte layout, confirmed by the per-phase field compares:

```text
[Info+0x00]  HloInstruction*   the already-assigned async collective op
[Info+0x08]  vector<long> tag  low bit (bit0) = is-heap flag; if heap, size = tag>>1
[Info+0x10]  vector<long> data the physical cores this assigned op holds (inline if <=4, else heap ptr)
[Info+0x30]  NDPlaneInfo       the SC ND-plane descriptor:
             +0x30 int32 plane#1 | +0x34 int32 #2 | +0x38 int32 #3
             +0x3C int32 size_x  | +0x40 bool has_x
             +0x44 int32 size_y  | +0x48 bool has_y
             +0x4C int32 size_z  | +0x50 bool has_z
             +0x54 bool          (NDPlaneStrideInfo trailing byte)
             +0x58 bool          across_cores_on_chip (megacore / both-cores flag)
```

The `NDPlaneInfo` sub-layout matches the descriptor used elsewhere in the SC collective stack; `NDPlaneInfo::ToString` (`0x10FDF2A0`) is the formatter the VLOGs call (or the literal `"None"` when absent).

### Algorithm — Phase 0 setup and the cost tie-break

```c
function SelectCores(hlo, allowed, devcount, cost, assigned, reach, assign_groups):  // 0x10FDC4E0
    // a) count allowed btree elements (leaf-node walk, 0x100 stride)
    // b) materialize allowed IDs into a heap vector<long>  (allowed_vec)
    allowed_vec = materialize(allowed)
    // c) the ONLY use of `cost`: order candidates by ascending per-core weight
    stable_sort(allowed_vec, $_0)        // $_0(a,b): vucomisd weight[a], weight[b]  (0x10FE8AE0 / cmp @0x10FE8B34)
    // d) get THIS collective's ND plane (the target plane the predicates test against)
    target_plane = TryGetNDPlaneInfoForSparseCoreCollectives(hlo, this->target_)  // 0x10FDEDC0
    if !target_plane.ok(): return Error(src line 269)   // AddSourceLocationImpl(..., 269, "…sparse_core_queue_assignment.cc")
    selected = []
    for phase in [P1, P2, P3, P4, P5]:
        for core in allowed_vec:          // ascending cost
            if core in selected: continue
            if phase.predicate(core, assigned, reach, assign_groups, target_plane):
                selected.append(core)     // grow via _Znwm + memcpy
                VLOG(phase.msg)
    return Ok(selected)                   // sret: [+0]=1 ok-tag, [+8]=data, [+0x10]=size, [+0x18]=cap
```

> **QUIRK —** the `double cost` argument is consumed at exactly one place: the `stable_sort` comparator `$_0` (`vucomisd` on a per-core `double` weight array). It is a **tie-break only**. Among candidates that equally satisfy a phase's predicate, the lower-cost core is appended first. There is no per-candidate score that decides *whether* a core is selected — only the phase predicate does that. The arithmetic that *populates* each core's weight (the LatencyEstimator / queue-occupancy feed in the caller) is not visible in this function; only the comparator's *use* (ascending sort) is determined here.

### Algorithm — the five predicates

Each pass scans `assigned`; the SIMD `vpcmpeqq` (4-wide) search tests whether a candidate core appears in an `Info`'s physical-core list `[Info+0x10..]`, then applies the per-phase predicate. The VLOG strings are byte-confirmed in source order in the decompile.

```text
P1  SAME ND PLANE          (lambda $_1, src line 279):
    exists Info holding `core` whose NDPlaneInfo[+0x30] == target_plane (all 3 ints, the
    per-axis has/size optionals, and across_cores[+0x58]) -> append.
    VLOG: "Adding core <c> to selected cores for <hlo> because it is running <NDPlaneInfo|None>
           on the same ND plane."

P2  DATA DEPENDENCY        (lambda $_2, src line 302):
    probe HloReachabilityMap `reach`: crc32 Swiss find keyed on (GetModule()->[module+0xAC4],
    HloInstruction::unique_id()) for BOTH the Info's op and the target hlo; on hit, test the
    reachability bit  (row = bitidx>>0xA, bound [reach+0x40]; col = bitidx&0x3FF scaled by
    [reach+0x28]; base [reach+0x38]; `bt` test). Reachable in either direction -> append.
    VLOG: "Adding core <c> ... because an assigned op <op> has data dependency with it."

P3  ASSIGNMENT-GROUP HINT  (lambda $_3, src line 329):
    for each group in assign_groups containing `hlo`: for each group member an Info holds and
    that appears in allowed_vec -> append that core (this is a PIN).
    VLOG: "Adding core <c> ... due to hint from pre-determined assignment groups."

P4  NOT A DIFFERENT PLANE  (lambda $_5 add, src line 362 / lambda $_4 skip, src line 352):
    same field compares as P1 but the success branch is the negation (`setne; and across_cores`).
    candidate NOT found running a collective on a DIFFERENT ND plane -> append.
    VLOG (add): "Adding core <c> ... because it is not running a collective on a different ND plane."
    VLOG (skip): "Core <op> is running <NDPlaneInfo|None> on a different ND plane."

P5  FALLBACK FILL          (no VLOG):
    every candidate in allowed_vec not already selected -> append unconditionally.
```

| Phase | Src line | Predicate (candidate core `c`, scanning `assigned`) | VLOG |
|---|---|---|---|
| P1 | 279 | ∃ `Info` holding `c` whose `NDPlaneInfo == target` (**same plane**) | "… because it is running … on the same ND plane." |
| P2 | 302 | ∃ `Info` whose op is reachable ↔ `hlo` (**data dependency**) | "… has data dependency with it." |
| P3 | 329 | `hlo` ∈ some assignment group ∧ a member's `Info` holds `c` | "… due to hint from pre-determined assignment groups." |
| P4 | 362 | **no** `Info` holding `c` is on a **different** ND plane | "… because it is not running a collective on a different ND plane." |
| P4 (skip) | 352 | (`c` **is** on a different plane → skip) | "Core … is running … on a different ND plane." |
| P5 | — | always (append every remaining allowed core) | (none) |

> **NOTE —** the assignment-group VLOG inside `SelectCores` is the **positive** pin ("Adding core … due to hint …", line 329). The complementary diagnostic "Not pinning … due to hint from pre-determined assignment groups." is **not** referenced inside `SelectCores` or `GetAllowedCores`; it lives in a higher-level assignment-group decision (the group construction / `AssignQueueIDsForComputation` layer). `SelectCores`'s own per-candidate "skip" diagnostic is P4's "Core … is running … on a different ND plane." (line 352).

### Return Shape

`SelectCores` writes `StatusOr<vector<long>>` into its sret: `[sret+0] = 1` (ok tag), `[sret+8] = data ptr`, `[sret+0x10] = size`, `[sret+0x18] = capacity`. The vector is **unsorted** — its order is exactly `{P1 same-plane} ++ {P2 data-dep} ++ {P3 assign-group} ++ {P4 not-different-plane} ++ {P5 all-remaining}`, each pass cost-ascending. The caller numerically sorts it before copying it (truncated `long → int32`) into `physical_core_indices`.

> **GOTCHA —** there is **no** closed-form per-candidate numeric score and **no** explicit core→queue bijection arithmetic in `SelectCores`. The placement is a deterministic greedy priority: a candidate's position is `(phase index 1..5, then ascending per-core cost)`, where phase membership is decided by whether an already-assigned async collective that holds the core is same-plane / data-dependent / in the same assignment group / on a different plane. A reimplementer must reproduce the *pass order* and the *cost tie-break*, not invent a scalar score.

### Function Map

| Function | Address | Role |
|---|---|---|
| `SparseCoreQueueAssignment::SelectCores` | `0x10FDC4E0` | five-phase ordered selection (sret StatusOr) |
| `…::SelectCores(...)::$_0` (in `__stable_sort`) | `0x10FE8AE0` | cost-ascending comparator (`vucomisd` @ `0x10FE8B34`) |
| `(anon)::TryGetNDPlaneInfoForSparseCoreCollectives` | `0x10FDEDC0` | target ND-plane for the predicates |
| `NDPlaneInfo::ToString` | `0x10FDF2A0` | VLOG plane formatter (or literal `"None"`) |
| `HloReachabilityMap` probe | — | P2 bit-matrix `bt` test (crc32 Swiss find) |
| `HloInstruction::async_wrapped_instruction` | `0x1E5AA300` | unwrap for resource derivation (shared with mask) |

---

## The `tensor_split_factor` Interaction

### Purpose

The relevant field is the **`tensor_split_factor`** of the collective offload-config (there is no `tensor_split_mode` field in this build — the "mode" is implied by whether `tensor_split_factor() > 1`). It does **not** appear inside `SelectCores` or `GetAllowedCores`. Instead it is consumed *downstream*, in the collective ring-strategy layer, where it governs how the *selected* cores partition a collective's tensor. Its relationship to core selection is therefore a count constraint, not a selection input.

### Where it lives

The strings `tensor_split` and `tensor_split_factor` resolve to the SC collective strategy code, not the queue-assignment pass. Two byte-confirmed sites:

```text
SinglePhaseAGStrategy::AdjustStrategiesForSingleCoreIfNeeded (0x1338C1E0):
  gate:  "offload_config().use_single_sparse_core()
          || (offload_config().has_tensor_split_factor()
              && offload_config().tensor_split_factor() > 1)"
  error: "Ring adjustment is only supported either for single core or tensor-split mode."

emitter_helpers::CreateRingStrategiesForNdFromExplicitTable (0x13390900):
  RET_CHECK: "ici_strategy_config.color_strategies_size() % tensor_split_factor.value_or(1) == 0"
           (offload_collective_strategies.cc:3966)
```

So the modes are: **single-core** (`use_single_sparse_core()` → the collective collapses to one SC core) versus **tensor-split** (`tensor_split_factor() > 1` → the tensor is split across that many cores). The ring strategy requires the number of color strategies to be divisible by `tensor_split_factor` (default 1).

### How it relates to selection

`SelectCores`/`GetAllowedCores` produce the *set* of physical cores a collective may use; `tensor_split_factor` determines how many of those cores a single collective's ring actually fans out across, and the divisibility `RET_CHECK` ensures the strategy table partitions evenly. The selection policy is agnostic to the split mode — it neither reads `tensor_split_factor` nor branches on `use_single_sparse_core`. A reimplementer should treat `tensor_split_factor` as a property of the *emitted strategy* that must be consistent with the count of selected cores, propagated through the offload-config the same `GetSparseCoreConfig` reader that `GetSparseCoreResources` uses for its resource-type enum.

> **NOTE —** the gate strings and the divisibility `RET_CHECK` for the `tensor_split_factor` ↔ selected-core-count consistency both sit in the collective strategy layer that consumes `physical_core_indices`. The exact arithmetic mapping a `tensor_split_factor` of N onto N specific entries of the selected-core list is part of the ring-strategy construction, which is **out of scope** for this selection page (it belongs to the offload-strategy emitters). `tensor_split_factor` is a consumer of the selected-core set, not a selection input.

### Function Map

| Function | Address | Role |
|---|---|---|
| `SinglePhaseAGStrategy::AdjustStrategiesForSingleCoreIfNeeded` | `0x1338C1E0` | single-core vs tensor-split gate |
| `SinglePhaseRSStrategy::AdjustStrategiesForSingleCoreIfNeeded` | `0x1338A7A0` | reduce-scatter analogue |
| `emitter_helpers::CreateRingStrategiesForNdFromExplicitTable` | `0x13390900` | `color_strategies_size() % tensor_split_factor == 0` |
| `backend_config_util::GetSparseCoreConfig` | `0x1C868D20` | offload-config reader (shared with the mask) |

---

## Per-Generation Notes

Nothing in `SelectCores` or `GetAllowedCores` is generation-branched in code. The candidate pool varies with the chip's megachip parallelism and SC count (data-driven through `GetMegaChipParallelism` and the device assignment), and the `across_cores_on_chip` flag in `Info[+0x58]` is the megacore / both-cores discriminator the plane predicates read — but the *policy* (five passes, cost tie-break, resource-reservation intersection) is identical across silicon. The 4:1 SparseCore:TensorCore ratio that bounds how many physical SC cores a chip exposes is established on [SparseCore Hardware Architecture](architecture.md) (`SparseCoreCountPerTensorCore`, `0x1C6CB760`); this page consumes that count, it does not compute it.

---

## Related Components

| Name | Relationship |
|---|---|
| `AssignQueueIDsToAsyncStart` (`0x10FDF480`) | the driver that calls `GetAllowedCores` + `SelectCores`, then sorts the result |
| `AddCollectivePhysicalCoreIndices` (`0x1C868500`) | the sink that writes the sorted core list into `physical_core_indices` |
| `GetSparseCoreConfig` (`0x1C868D20`) | the offload-config reader shared by the resource-type enum and `tensor_split_factor` |
| `AsyncTracker::GetResourceTypeForOp` (`0x13612240`) | the enum-4 switch-arm resource-type derivation (from the unwrapped async-op root opcode) feeding `GetSparseCoreResources` |

## Cross-References

- [SparseCore Overview](overview.md) — the navigational entry for Part IX; engine names, per-gen presence, the data path.
- [SparseCore Hardware Architecture](architecture.md) — the geometry source (`SparseCoreTarget`), the 4:1 SC:TC ratio, and the physical core count this policy selects from.
- [SC Backend Pipeline](sc-backend-pipeline.md) — the SC-MLO pass pipeline the queue-assignment pass runs inside (and the MEGACORE barrier).
- [SC Queue Assignment & Reservation](sc-queue-assignment-reservation.md) — the resource→limit reservation map the `GetAllowedCores` per-resource budget is part of.
- [Physical-Core Placement](../collectives/physical-core-placement.md) — the collective-side consumer of `physical_core_indices`, where the selected core list maps onto the ring/megachip topology.
- [GetSparseCoreConfig](getsparsecoreconfig.md) — the offload op-type configuration the resource-type enum and `tensor_split_factor` are read from.
- [OneSlot Router](oneslot-router.md) — the per-slot routing the selected cores feed into.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-selection function and the sequencer-type enum.
- [Region → Sequencer Outliner](region-to-sequencer-outliner.md) — partitioning the selected SC computation into per-engine bundle streams.
- [SCS (Scalar) Engine](scs-engine.md) · [TAC Engine](tac-engine.md) · [TEC (Vector) Engine](tec-engine.md) — the three sub-engine bundle surfaces the selected cores run.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore back-end — [back to index](../index.md)
