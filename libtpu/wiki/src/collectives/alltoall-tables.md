# AllToAll Tables

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset; every address below is a VMA.*

## Abstract

`xla::jellyfish::GenerateAllToAllTables` (`0x133ed620`) is the compile-time builder of the **barrier participant tables** for an ICI `all-to-all` collective. It does *not* build the per-step partner schedule — that is the Type-5 route literal produced by the routing subsystem ([Create Routing Schedule](../routing/create-routing-schedule.md)). Instead it answers the orthogonal question the within-replica-group barrier needs: *who is in my group, and where do I sit in it?* The function returns a `tuple<vector<int> A, vector<int> B, optional<vector<int>> C>` that `AllToAllEmitterBase::GenerateConstants` (`0x10f089a0`) registers into the per-instruction `ConstantMapper` as **ConstantMapper Types 8, 9, and 0xa**, and that `GetConstantTables` (`0x10f07860`) later reads back and hands to `BarrierWithinReplicaGroupStartNoReturn` as `(InfoTable A, InfoTable B, optional<InfoTable> C)`.

The reader who knows MPI owns the frame: in a ring/recursive collective each rank must know its *communicator membership* — the set of peers it synchronizes with and its ordinal in that set — independently of the per-iteration send/recv schedule. Tables A and B are exactly that membership map and its transpose: **A** is `device_id → (replica_group, within-group ordinal)` (2 ints per device), **B** is the transposed enumeration `(within-group position, group) → device_id`. Table **C** is an optional 3-D / partition-aware core table, present only on the channel-id-odd path that also has a static device assignment. The same three tables drive the **`ragged-all-to-all`** variant, which shares the membership builder and the all-to-all cost helper and differs only in its runtime offset arithmetic.

This page documents three reimplementable artifacts: (1) the **table-generation loop** in `GenerateAllToAllTables` (group-size selection, the replica-group fill and the no-replica-group identity fill, table-C strided linearization, the return tuple layout); (2) the **registration / read-back path** through `GenerateConstants` → `ConstantMapper` → `GetConstantTables` and the static-vs-dynamic carrier choice; and (3) the per-XY **route-buffer `Allocator` value struct** — the `FlatHashMap<XY, Allocator>` scratch-buffer scoreboard the routing-schedule callback maintains, whose value layout (`int32 size` + `std::deque<pair<int,int>> available`) is decoded here byte-exact. The mesh geometry the membership tables index (`MeshNDInfo`) and the ND-ring AllGather replica tables that share that struct are documented inline where they touch all-to-all, and owned in full by [AllGather ND-Ring](allgather-nd-ring.md).

For reimplementation, the contract is:

- **The membership encoding.** Tables A (`Type 8`, 2 ints/device), B (`Type 9`, 1 int/slot), and optional C (`Type 0xa`); their index arithmetic, their stored values, and the two fill paths (replica-groups vs. identity).
- **The group-size and table-C gates.** `group_size = channel_id & 1 ? mesh_dim1 : mesh_dim0`; the `static_device_assignment` precondition; the `-0x68` "table C present" flag set only on the channel-id-odd + replica-count path.
- **The carrier choice.** `CollectiveShouldUseStaticInfoTable` (`0x138194c0`) picks a static R1 `Literal` vs. a runtime `vector<int>` per table, against the `TpuCompEnv[+0x15d0] >= mesh_dim0·mesh_dim1` threshold.
- **The allocator value struct.** The `FlatHashMap<XY, Allocator>` 0x40-byte slot (8-byte XY key + 0x38-byte value), the four release-time `RET_CHECK` invariants, and the single `available` deque (no separate "latest-DMA-out" container).

| | |
|---|---|
| **Table builder** | `xla::jellyfish::GenerateAllToAllTables(const HloInstruction&, long, long)` @ `0x133ed620` |
| **SparseCore twin** | `GenerateAllToAllTablesForSparseCore(const HloInstruction&, const DeviceAssignment&)` @ `0x133ee200` |
| **Registrar** | `AllToAllEmitterBase::GenerateConstants` @ `0x10f089a0` (Types 8 / 9 / 0xa + Type 5) |
| **Carrier gate** | `CollectiveShouldUseStaticInfoTable` @ `0x138194c0` (`TpuCompEnv[+0x15d0] >= mesh_dim0·mesh_dim1`, `setge`) |
| **Read-back** | `(anon)::GetConstantTables` @ `0x10f07860` → `(InfoTable, InfoTable, optional<InfoTable>)` |
| **Barrier consumer** | `AllToAllEmitterBase::EmitBarrierStartImpl` @ `0x10f07240` → `BarrierWithinReplicaGroupStartNoReturn` @ `0x1c6983e0` |
| **Cost branch** | `ComputeAllToAllCycles` @ `0x130ae8e0` / `ComputeRaggedAllToAllCycles` @ `0x130aea80` (shared `ComputeAllToAllCyclesHelper` @ `0x130d02a0`) |
| **Route-buffer scoreboard** | `FlatHashMap<XY, Allocator>` via `find_or_prepare_insert` @ `0x138270a0` (slot stride `0x40`); `$_1` release callback @ `0x13826dc0` |
| **Source TU** | `platforms/xla/service/jellyfish/lowering/all_to_all_emitter_*.cc` (str @ `0x878b4af`); allocator in `net_router_emitter.cc` (str @ `0x8760f44`) |
| **Confidence** | HIGH — decompile-verified bodies for the builder, SparseCore twin, read-back, and allocator callback; LOW rows flagged inline |

---

## Where This Sits

`GenerateAllToAllTables` is one of two orthogonal constant families an all-to-all collective compiles into. The split is the central structural fact of this page:

```text
HLO all-to-all (opcode 12) / ragged-all-to-all (opcode 86)
        │
        ├── PARTNER SCHEDULE  (the per-(core, step, direction) DMA program)
        │     CreateAllToAllTransfers → CreateRoutingScheduleLiteral
        │       → ConstantMapper Type 5
        │       → CalculateWithLimitedIciRouting / EmitForLimitedIciRouting
        │     (owned by:  ../routing/create-routing-schedule.md)
        │
        └── BARRIER MEMBERSHIP  (who is in my replica group, and my ordinal)
              GenerateAllToAllTables  →  Types 8 / 9 / 0xa
                → GetConstantTables
                → BarrierWithinReplicaGroupStartNoReturn
              (THIS PAGE)
```

The two are produced by separate builders and consumed by separate code. The Type-5 route literal answers *which cores DMA to whom, per step*; Types 8/9/0xa answer *who barriers with whom* — the membership sets `GetReplicaGroupCoreInfo` reads to recover each core's replica-group peer set and master (master = peer 0). The all-to-all **cost** (`ComputeAllToAllCycles` @ `0x130ae8e0`, divide by `EstimatePhysicalLinksUsed`) is a third orthogonal axis — see [SPMD Link-Count Cost](spmd-link-count-cost.md).

This page owns the **membership table builder, its registration/read-back, and the route-buffer `Allocator` scoreboard struct**. The AllGather ND-ring replica tables (`CreateStaticNDRingReplicaInfoTable`, ConstantMapper Types 0/1/2) share the `MeshNDInfo` geometry and the same `ConstantMapper` mechanism but are a different collective; they are owned by [AllGather ND-Ring](allgather-nd-ring.md). The strategy decision that even reaches this builder is on [SelectNDStrategy](strategy-nd-picker.md).

---

## 1. `GenerateAllToAllTables` — the table-generation loop

### Purpose

Build the three membership tables for one all-to-all `HloInstruction`. The function takes the instruction plus the two mesh extents (`mesh_dim0`, `mesh_dim1`) and returns the `(A, B, optional C)` tuple by `sret`. It is the all-to-all analog of `CreateStaticReplicaInfoTable`: it precomputes, at compile time, the device-to-group mapping the runtime barrier would otherwise have to derive.

### Entry Point

```text
AllToAllEmitterBase::GenerateConstants @0x10f089a0
  └─ GenerateAllToAllTables @0x133ed620                    ── builds (A, B, opt C)
       ├─ HloInstruction::channel_id                       ── parity selects group_size
       ├─ HloInstruction::has_replica_groups / replica_groups
       ├─ GetModule()->config[+0x20][+0x660]               ── static_device_assignment has-bit
       └─ proto2::LogIndexOutOfBoundsAndAbort @0x21063300  ── bounds trap on table writes
```

### Algorithm

The two mesh extents arrive through `AllToAllEmitterBase::GenerateConstants`, which reads them from the `LogicalTopologyInfo` (`mesh_dim0 = movslq (r15)`, `mesh_dim1 = movslq 0x4(r15)`) and calls `GenerateAllToAllTables(hlo, mesh_dim1, mesh_dim0)`. Inside the builder, `group_size` is the **channel-id parity** select:

```c
function GenerateAllToAllTables(hlo, arg_dim1, arg_dim0):     // 0x133ed620, sret -> (A, B, opt C)
    // group_size = odd channel -> mesh_dim1, even -> mesh_dim0   (cmovne @0x133ed669)
    group_size = (channel_id(hlo) & 1) ? arg_dim1 : arg_dim0;  // line 94 / 121-125

    // The 3-D / partition-aware path requires a static device assignment.
    has_static_da = module_config[+0x20][+0x660];              // movzbl @0x133ed67d
    if ((channel_id(hlo) & 1) && replica_count_present && !has_static_da)
        LogMessageFatal("static_device_assignment_.has_value()");  // hlo_module_config.h:285 @0x133ee1c0

    table_C_present = (channel_id(hlo) & 1) && replica_count_present;  // -0x68 set @0x133edc42

    // ---- Table A : Type 8 ----  vector<int>, 2 ints per device id
    table_A.reserve(2 * group_size);                           // __append @line 168

    if (!has_replica_groups(hlo)) {
        // identity / single-group form
        for (pos in [0, group_size)):
            table_A[2*pos]   = 0;                              // group  = 0   @0x133edbd8
            table_A[2*pos+1] = pos;                            // ordinal = pos @0x133edbf3
            table_B[pos]     = pos;                            // identity      @0x133edc0b
    } else {
        // replica-group form
        for (g, group) in enumerate(replica_groups(hlo)):     // members inline@0x18 / heap@0x20, count@0x1c
            for (pos, dev) in enumerate(group.replica_ids):
                table_A[2*dev]   = g;                          // @0x133edd36
                table_A[2*dev+1] = pos;                        // @0x133edda1
                table_B[group_size*pos + g] = dev;             // imul %r8d,%eax; +pos @0x133edde5
    }

    // ---- Table C : Type 0xa (optional) ----  built only when table_C_present
    if (table_C_present):
        for (pos in within-group positions):
            coord     = (...);                                 // within-group position
            device_id = dot(coord, DA_per_dim_strides);        // 8-wide imul/add @0x133eda50/daf0/df50
            table_C[pos] = device_id;                          // copied from -0xb8 snapshot @0x133ee011

    return { tag=1, A, B, C, has_value = table_C_present };    // sret tuple @0x133ee03a
```

Three details a reimplementer must get right:

- **Table A is 2-wide.** Every device id consumes two consecutive `int` slots: `table_A[2·dev]` = replica group, `table_A[2·dev+1]` = within-group ordinal. The store sites `0x133edd36` / `0x133edda1` and the `__append(2 * group_size)` reservation at line 168 confirm the stride; the bounds check `*(_QWORD*)&v83[8] <= 2*v27` (line 263) is the `vector::size()` guard before each pair of writes.
- **Table B is the transpose.** It is indexed by `group_size·position + group` (the `imul %r8d(group_size),%eax; add pos` at `0x133edde5`) and stores the device id. Reading A gives "where does device *d* sit"; reading B gives "which device sits at (position, group)". The barrier uses B to enumerate peers in order, A to find its own ordinal.
- **Table C is conditional and 3-D.** It exists only when the channel id is odd *and* a replica count is present (`-0x68` set at `0x133edc42`), which is exactly the path that asserts `static_device_assignment_.has_value()`. Its values are flat device ids produced by the 8-wide strided dot-product against the `DeviceAssignment` per-dimension stride array — the same coordinate-linearization the ND-ring builder uses (§4).

> **GOTCHA —** these are *membership* tables, not a partner schedule. A reimplementer who treats Table B as "the device I send to at step *k*" will build a broken collective. B is the static enumeration `(position, group) → device`; the per-step send/recv partners come from the Type-5 route literal in [Create Routing Schedule](../routing/create-routing-schedule.md). The two tables are produced by different functions and feed different runtime entry points (barrier vs. limited-ICI routing).

### Return tuple layout

The `sret` buffer at `-0xd0` is a `tuple<vector<int>, vector<int>, optional<vector<int>>>`:

| Field | Offset | Type | Meaning |
|---|---|---|---|
| tag | `+0x00` | int | active-member tag (always `1` here) |
| A | `+0x08` / `+0x10` / `+0x18` | `vector<int>` `{data, size, cap}` | Table A (device → group, ordinal) |
| B | `+0x20` / `+0x28` / `+0x30` | `vector<int>` `{data, size, cap}` | Table B (position, group → device) |
| C | `+0x38` / `+0x40` / `+0x48` | `vector<int>` `{data, size, cap}` | Table C (3-D core table) |
| C.has_value | `+0x50` | byte | set `1` @ `0x133ee0ec` only when `-0x68` |

### Function Map

| Function | Address | Role |
|---|---|---|
| `GenerateAllToAllTables` | `0x133ed620` | builds `(A, B, opt C)`; 622 decompiled lines |
| `GenerateAllToAllTablesForSparseCore` | `0x133ee200` | SparseCore twin (no parity, no C); confirms A/B encoding |
| `proto2::LogIndexOutOfBoundsAndAbort` | `0x21063300` | bounds trap on table writes (`0x133edd71`, `0x133ee01e`) |
| `LogMessageFatal` (static-DA assert) | — | `hlo_module_config.h:285` @ `0x133ee1c0`, str `0x86508f3` |

### Considerations

The "which axis is replica vs. which is partition" labeling of A and B is **attributed, not self-describing** (LOW): the index arithmetic and stored values are byte-confirmed, but the symbolic meaning (A = replica-axis ordinal table, B = partition-axis enumeration) is recovered from the way `GetReplicaGroupCoreInfo` reads them at the barrier, not from a named field in the builder. Treat the *encoding* as HIGH and the *axis label* as the structural reading.

---

## 2. The SparseCore twin — independent A/B confirmation

### Purpose

`GenerateAllToAllTablesForSparseCore` (`0x133ee200`) is the simpler SparseCore variant of the builder. It has **no channel-id parity** and **no table C** — it produces only A and B — and it derives its participants from the collective op group mode rather than from the raw `replica_groups()`. Its value here is corroboration: it reproduces the exact same A/B index arithmetic from a completely different control path, which fixes the encoding beyond the single builder.

### Algorithm

```c
function GenerateAllToAllTablesForSparseCore(hlo, da):        // 0x133ee200
    total = da[+0] * da[+8];                                  // mesh extents product @0x133ee234
    mode  = GetCollectiveOpGroupMode(hlo);                    // 0x1e46bac0, line 67
    groups = GetParticipatingDevicesGroups(da, ..., mode);    // 0x1e46bc20, line 77
    for (g, group) in enumerate(groups):
        group_size = group.size();
        for (pos, dev) in enumerate(group):
            table_A[2*dev]   = g;                             // @0x133ee35f, line  (2*v21)
            table_A[2*dev+1] = pos;                           // @0x133ee37d
            table_B[group_size*g + pos] = dev;                // @0x133ee39d
```

The store sites (`0x133ee35f` / `0x133ee37d` / `0x133ee39d`) and the same `*(_QWORD*)&v37[8] <= 2*v21` bounds guard (line 100) match the main builder's encoding exactly. The only structural difference from §1 is that the SparseCore twin indexes B as `group_size·group + pos` whereas the dense builder uses `group_size·pos + group` — the transpose convention flips because the SparseCore loop nests group-outer rather than position-outer; both store the device id at the slot the corresponding reader expects.

> **NOTE —** the SparseCore twin is what makes the A/B encoding HIGH-confidence rather than single-witness. Two independent builders, two different participant-derivation paths, identical `2·dev` / `2·dev+1` stores.

---

## 3. Registration and read-back — Types 8 / 9 / 0xa

### Purpose

`AllToAllEmitterBase::GenerateConstants` registers the three vectors into the per-instruction `ConstantMapper` under fixed type tags, choosing per table whether to store a *static* R1 `Literal` (a baked constant the runtime reads directly) or a *dynamic* `vector<int>` (materialized at runtime). `GetConstantTables` reads them back as `InfoTable`s and `EmitBarrierStartImpl` feeds them to the within-replica-group barrier.

### Algorithm

```c
function AllToAllEmitterBase::GenerateConstants(hlo, target, topo, region):  // 0x10f089a0
    mesh_dim0 = movslq (topo);  mesh_dim1 = movslq 0x4(topo);   // 0x10f08a02 / a0a
    (A, B, C) = GenerateAllToAllTables(hlo, mesh_dim1, mesh_dim0);  // 0x10f08a10

    // static carrier iff TpuCompEnv[+0x15d0] >= mesh_dim0 * mesh_dim1
    use_static = CollectiveShouldUseStaticInfoTable(topo);      // 0x138194c0, setge

    AddConstant(Type=8, use_static ? Literal(A) : vector(A));   // 0x10f08cc8 / 0x10f08c2c
    AddConstant(Type=9, use_static ? Literal(B) : vector(B));   // 0x10f08d5b / 0x10f08e27
    if (C.has_value)
        AddConstant(Type=0xa, use_static ? Literal(C) : vector(C));  // 0x10f08ee4 / 0x10f08fa2

    // the orthogonal partner schedule:
    AddConstant(Type=5, CreateAllToAllRoutingScheduleTable(...));   // AddConstant @0x10f09161; call @0x10f0906f -> 0x10f061c0
```

`CollectiveShouldUseStaticInfoTable` (`0x138194c0`) computes `total = movslq(rsi) · movslq 0x4(rsi)` (= `mesh_dim0·mesh_dim1`), reads `GetTpuCompEnv`, and returns `TpuCompEnv[+0x15d0] >= total` via `cmp %rbx,0x15d0(%rax); setge`. Below the threshold the tables are small enough to bake as constants; above it they are materialized dynamically.

The read-back closes the loop:

```c
function GetConstantTables(hlo, mapper):                       // 0x10f07860
    A = mapper.GetConstant(Type=8);                            // 0x10f07889
    B = mapper.GetConstant(Type=9);                            // 0x10f078d2
    optional C;
    if (mapper.HasConstant(Type=0xa))                          // 0x10f07907
        C = mapper.GetConstant(Type=0xa);                      // 0x10f07920
    return StatusOr<tuple<InfoTable, InfoTable, optional<InfoTable>>>(A, B, C);  // 0x10f079f1..a34

function EmitBarrierStartImpl(...):                            // 0x10f07240
    (A, B, C) = GetConstantTables(hlo, mapper);                // 0x10f074e5
    BarrierWithinReplicaGroupStartNoReturn(                    // 0x1c6983e0, call @0x10f07635
        ..., /*rcx*/ A, /*r8*/ B, /*r9*/ C);                   // A=-0xd0, B=-0xb8, C=-0x60
```

`HasConstant(Type=0xa)` is byte-confirmed at the decompiled `GetConstantTables` (`HasConstant(a2, 10)`), so the read side gates table C on presence exactly as the builder gates its construction. The three `InfoTable`s land directly in the barrier-start argument registers `rcx`/`r8`/`r9`.

### Function Map

| Function | Address | Role |
|---|---|---|
| `AllToAllEmitterBase::GenerateConstants` | `0x10f089a0` | registers Types 8/9/0xa + Type 5 |
| `CollectiveShouldUseStaticInfoTable` | `0x138194c0` | static-vs-dynamic carrier gate |
| `(anon)::GetConstantTables` | `0x10f07860` | reads 8/9/0xa back as `InfoTable` triple |
| `AllToAllEmitterBase::EmitBarrierStartImpl` | `0x10f07240` | feeds triple to barrier-start |
| `BarrierWithinReplicaGroupStartNoReturn` | `0x1c6983e0` | the within-group barrier consumer |
| `CreateAllToAllRoutingScheduleTable` | `0x10f061c0` | the orthogonal Type-5 route literal (call site `0x10f0906f`) |

> **QUIRK —** the carrier choice is per-instruction, not per-table-kind. All three of A/B/C take the same `use_static` decision from one `CollectiveShouldUseStaticInfoTable` call, so a single instruction never mixes a static A with a dynamic B. The threshold `TpuCompEnv[+0x15d0]` is a compile-environment knob, so the same HLO can lower to static constants on one configuration and runtime vectors on another.

---

## 4. The `MeshNDInfo` geometry the tables index

Table C's 3-D linearization and the ND-ring AllGather tables share one mesh-geometry descriptor. `MeshNDInfo` (copy ctor `0x127b5100`) is **0x40 bytes**, describing one per-axis ring embedded in the N-D device mesh:

| Field | Offset | Type | Meaning |
|---|---|---|---|
| axis ids | `+0x00` | `vector<MeshDim>` (int32×) | the mesh-axis id list; `memcpy 4·n` @ `0x127b516b` |
| per-dim sizes | `+0x18` | `vector<long>` (8-byte×) | the ring lengths; divisor for the modular neighbor; `memcpy 8·n` @ `0x127b51a9` |
| ring order | `+0x28` | `vector<MeshDim>` (int32×) | traversal order (device ids along the ring); `memcpy 4·n` @ `0x127b51e9` |
| dim bitmask | `+0x38` | long | `popcount(low 3 bits)` ⇒ `Is2D` (2) / `Is3D` (3) |

The coordinate-to-device linearization both Table C (§1) and the ND-ring builder use is an 8-wide `imul`/`add` dot-product of the mesh coordinate against the `DeviceAssignment` per-dimension stride array. In `CreateStaticNDRingReplicaInfoTable` (`0x1c69e900`) the dispatch is gated by a `RET_CHECK` that the descriptor is 2-D or 3-D: `__popcnt(a2[7] & 7) != 2` then `... || mesh_info.Is3D()`, reporting `net_util.cc:2440` (str `0xa17c039`) on failure. The DA-flatten bound is `indexes.size() == num_dimensions()` (str `0xa1567ac`, line 413 of the decompile).

> **NOTE —** `MeshNDInfo` and the ND-ring builders (`CreateStaticNDRingReplicaInfoTable` @ `0x1c69e900`, `CreateNDRingReplicaInfoTable` @ `0x1c69e7e0`, the latter wrapping the former in `LiteralUtil::CreateR1<int>` at `net_util.cc:2412`) belong to the **AllGather** ND-ring path (ConstantMapper Types 0/1/2). They appear here only because Table C reuses the identical `DeviceAssignment` strided linearization. Their full per-axis ring-neighbor encoding and the `AllGatherEmitter::InitDim` axis walk are owned by [AllGather ND-Ring](allgather-nd-ring.md).

---

## 5. The route-buffer `Allocator` value struct

### Purpose

The routing-schedule solver maintains a per-destination scoreboard of scratch buffers as it walks the transfer schedule. The container is a `FlatHashMap<net_util::XY, net_router::(anon)::Allocator>`; its value struct tracks how many buffers a destination XY may use and which are currently in flight. This page decodes the **value layout** byte-exact from the buffer-release callback that reads it, resolving the earlier open question of whether "available" and "latest-DMA-out" were two containers (they are one).

### Slot and value layout

`find_or_prepare_insert` (`0x138270a0`) uses a `0x40`-byte slot (stride `shl $0x6` @ `0x13827163` / `0x13827217`). The slot is `pair<const XY, Allocator>`: an 8-byte `XY` key (2×int32) at `slot+0x0`, the `Allocator` value at `slot+0x8`.

| Value off | Slot off | Field | Type | Role |
|---|---|---|---|---|
| `+0x00` | `+0x08` | `size` | int32 | per-XY scratch-buffer count bound (`RET_CHECK *ptr.index < size`) |
| `+0x08` | `+0x10` | `available` | `std::deque<pair<int32,int32>>` | in-flight `(buffer_index, available_at_step)` list |

The deque (libc++ split-buffer, `0x30` bytes) lays out within the value as `__map_.__first_` @ `value+0x8`, `__begin_` @ `+0x10`, `__end_` @ `+0x18`, `__end_cap_` @ `+0x20`, `__start_` @ `+0x28`, `__size_` @ `+0x30`. The 512-element block indexing (`shr $0x9`, `and $0x1ff`) is the `std::deque` map signature. Total value = `0x38` bytes (`int32 size` + `0x30` deque), exactly filling the `0x40` slot after the 8-byte XY key.

### Release-callback invariants

The `$_1` buffer-release callback (`0x13826dc0`) is the definitive reader. On each release it finds-or-inserts the destination XY (zero-initializing the value on insert via `vmovups ymm0`), computes `available_at = step + 1`, then enforces four `RET_CHECK`s before pushing the freed buffer onto `available`:

```c
function release_buffer(map, ptr, dest_xy, step):              // $_1 @0x13826dc0
    alloc = map.find_or_insert(dest_xy);                       // fopi @0x13826de1
    available_at = step + 1;                                   // inc %r15d @0x13826e14/29

    RET_CHECK(alloc.available.empty() ||                       // net_router_emitter.cc:389
              alloc.available.back().second <= available_at);  //   sorted by release step @0x13826e50
    RET_CHECK(ptr.type == PointerType::kAlloc);                // :390  cmp $2 @0x13826e8d
    RET_CHECK(ptr.index.has_value());                          // :391  test $1 @0x13826e97
    RET_CHECK(c_none_of(alloc.available,                       // :395  deque scan @0x13826ea4..f1c
                        [&](pair p){ return p.first == *ptr.index; }));  // no double release
    RET_CHECK(*ptr.index < alloc.size);                        // :396  cmp %r14d,(%rax) @0x13826f1e

    alloc.available.push_back( *ptr.index | (step << 32) );    // __add_back_capacity @0x13826f4d
                                                               //   64-bit store @0x13826f76
```

All five strings are byte-confirmed in the decompile: `net_router_emitter.cc:389` `"available.empty() || available.back().second <= available_at"`, `:390` `"ptr.type == PointerType::kAlloc"`, `:391` `"ptr.index.has_value()"`, `:395` the `c_none_of` lambda, `:396` `"*ptr.index < size"`. The push packs the freed buffer as `(index | step<<32)` — `.first` = buffer index, `.second` = release step — preserving the back()-sorted-by-step invariant.

The route-buffer allocator keeps a single `std::deque<pair<int,int>> available` at `value+0x8` — there is no second container; the deque's `.second` field carries the release step.

### Considerations

The `size` field's **writer** is not isolated (LOW): it is byte-confirmed *read* as the per-XY bound by the `*ptr.index < size` check and zero-initialized on first insert, but the routing-schedule main-loop site that sets it to the topology-derived scratch-buffer count was not traced here. The `$_1` capture is a `0x28`-byte POD (built @ `0x13820aa1`): `{ &FlatHashMap @+0x00; ptr{type,index} 16B @+0x08; dest XY @+0x18; step int32 @+0x20 }`. The full routing-schedule driver that owns this scoreboard is on [Create Routing Schedule](../routing/create-routing-schedule.md).

---

## 6. The ragged-all-to-all variant

`ragged-all-to-all` (HLO opcode 86) is a data-dependent all-to-all whose per-rank send/receive counts are runtime values rather than compile-time uniform shards. For the **membership** tables it is identical to plain all-to-all: it is an all-to-all-family op, so the barrier participant sets come from the same `GenerateAllToAllTables` builder (Types 8/9/0xa) and the same `GetConstantTables` read-back. What differs is the *runtime offset arithmetic* on the data path (the ragged offsets that index into each peer's buffer), which the route schedule and DMA emission handle, not the membership tables on this page.

The cost model treats the two as one family: `ComputeRaggedAllToAllCycles` (`0x130aea80`) and `ComputeAllToAllCycles` (`0x130ae8e0`) both call the shared `ComputeAllToAllCyclesHelper` (`0x130d02a0`), and both divide by `EstimatePhysicalLinksUsed` (`0x1c8939c0`) — the all-link-saturating cost shape. The per-kind cost formulas are on [SPMD Link-Count Cost](spmd-link-count-cost.md).

> **NOTE —** the membership-table builder makes no special case for ragged; the divergence is entirely in the runtime offset path. A reimplementer can use one builder for both opcodes and only branch in the route-schedule / DMA-offset emission.

---

## Verification notes

> The table builder, SparseCore twin, registration/read-back, and allocator value struct were cross-checked against the IDA decompile of `libtpu.so` v0.0.40:
> - `GenerateAllToAllTables` @ `0x133ed620` (622 lines): `channel_id` parity (lines 94/121-125), `static_device_assignment_.has_value()` assert (`hlo_module_config.h:285`), `has_replica_groups`/`replica_groups()`, `__append(2*group_size)` (line 168), table-A `2·dev` stores (lines 263-266, `0x133edd36`/`da1`), table-B `imul group_size` (`0x133edde5`), table-C copy (`0x133ee011`), `LogIndexOutOfBoundsAndAbort` (`0x21063300`) — all present.
> - `GenerateAllToAllTablesForSparseCore` @ `0x133ee200`: `GetCollectiveOpGroupMode` (line 67), `GetParticipatingDevicesGroups` (line 77), `2·dev`/`2·dev+1` stores (lines 100-103) — exact.
> - `GetConstantTables` @ `0x10f07860`: two `GetConstant` reads + `HasConstant(a2, 10)` gate for Type 0xa — exact.
> - `MeshNDInfo` copy ctor @ `0x127b5100`: three vectors (`4·n`, `8·n`, `4·n` memcpy) + dim long; `CreateStaticNDRingReplicaInfoTable` @ `0x1c69e900` `Is2D/Is3D` popcount (`net_util.cc:2440`), `indexes.size() == num_dimensions()`; `CreateNDRingReplicaInfoTable` @ `0x1c69e7e0` `CreateR1<int>` at `net_util.cc:2412` — exact.
> - `$_1` release callback @ `0x13826dc0`: `RET_CHECK` lines 389/390/391/395/396 with their full strings (`net_router_emitter.cc`) — exact; the single `available` deque proven (no second container).
> - Cost: `ComputeAllToAllCycles` @ `0x130ae8e0` and `ComputeRaggedAllToAllCycles` @ `0x130aea80` both call `ComputeAllToAllCyclesHelper` @ `0x130d02a0` — exact.
>
> **[LOW]** (1) The A/B axis labels (A = replica axis, B = partition axis) are attributed from the barrier's `GetReplicaGroupCoreInfo` read pattern, not a named field. (2) The `Allocator.size` writer was not isolated; only its read semantics and zero-init are confirmed. (3) The `MeshNDInfo +0x38` bit→axis mapping is the structural reading; only the popcount→dimension-count is byte-confirmed.

---

## Related Components

| Name | Relationship |
|---|---|
| `CreateAllToAllRoutingScheduleTable` @ `0x10f061c0` | the orthogonal Type-5 partner schedule (route, not membership) |
| `BarrierWithinReplicaGroupStartNoReturn` @ `0x1c6983e0` | consumer of the `(A, B, opt C)` `InfoTable` triple |
| `CreateStaticNDRingReplicaInfoTable` @ `0x1c69e900` | AllGather ND-ring tables sharing the `MeshNDInfo` / DA-strided linearization |
| `ComputeAllToAllCyclesHelper` @ `0x130d02a0` | shared cost helper for both all-to-all opcodes |

## Cross-References

- [On-Pod Collectives — Section Map](overview.md) — the collective stack map; all-to-all (12) / ragged (86) cost branches
- [AllGather ND-Ring](allgather-nd-ring.md) — `MeshNDInfo`, the ND-ring replica tables (Types 0/1/2), `InitDim` axis walk
- [SelectNDStrategy](strategy-nd-picker.md) — the picker that decides the ICI ring algorithm upstream of this builder
- [SPMD Link-Count Cost](spmd-link-count-cost.md) — `ComputeAllToAllCycles` / `ComputeRaggedAllToAllCycles`, `EstimatePhysicalLinksUsed` divisor
- [Constant Mapper](constant-mapper.md) — the per-instruction `ConstantMapper` that stores Types 5/8/9/0xa
- [Create Routing Schedule](../routing/create-routing-schedule.md) — the Type-5 route literal and the route-buffer `Allocator` driver
- [Route-Table Generation](../routing/route-table-generation.md) — the per-color `RingLocation` neighbor schedule
- [Barriers](../barrier/overview.md) — `BarrierWithinReplicaGroup`, `GetReplicaGroupCoreInfo`, the membership-table consumer
- [Twisted Torus](../twist/overview.md) — twisted-torus geometry the ND strategies target
- [back to index](../index.md)
