# ConstantMapper

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

`ConstantMapper` is the jellyfish collective lowering's **compile-time constant pool**: every collective HLO (`all-reduce`, `all-gather`, `all-to-all`, `collective-permute`) that lowers to an ICI program needs a handful of static tables — replica/ordinal tables, ND-ring neighbor tables, the per-transfer route schedule, the binomial butterfly table, the all-to-all barrier membership tables — and each of those is registered into, and later fetched from, one `ConstantMapper` keyed by a small integer `Type` enum. The opposite number of this constant pool is the **runtime collective id**: a TPU core does not know its own replica/partition rank at compile time, so each rank is read at run time as a single U32 scalar from a fixed SMEM word whose offset is itself reserved at `Target::Init` from the chip-config. This page owns the `Type` enum, the `GenerateConstants` → `GetConstantTables` materialization path, and that SMEM-id read.

The mechanism is **not** a global enum-indexed table. `ConstantMapper` is a per-lowered-instruction object holding an Abseil SwissTable (`FlatHashMapPolicy<ConstantMapper::Type, StatusOr<shared_ptr<const vector<int>>>>`, confirmed from the mangled instantiation) keyed by the `Type` int. The same `Type` value carries *different* table content in different emitters — `Type 3` is a flat replica table in AllReduce but an ND-ring table in AllGather — and that is collision-free precisely because each lowered collective owns its own mapper, handed out by a per-HLO factory (`GetConstantFnForCollective`). A second namespace, `MeshNDInfo::MeshDim`, shares the same integers: the `MeshDim` `AddConstant`/`GetConstant` overloads forward the value to the `Type` overloads unchanged, so mesh axes 0/1/2 *are* `Type` 0/1/2.

For a reimplementer, the contract has three parts: (1) the 12-value `Type` enum (0..0xb) and which table-builder feeds each tag in which emitter; (2) the registration/read-back path — `GenerateConstants` populates the mapper, the lowering body fetches by `GetConstant(Type)`, and the AllToAll barrier specifically pulls Types 8/9/0xa through `GetConstantTables` as an `(InfoTable, InfoTable, optional<InfoTable>)` triple — including the static-`Literal`-vs-dynamic-`vector<int>` carrier choice; (3) the runtime-id read: `GetReplicaId`/`GetPartitionId` emit a U32 `Sld` (scalar load) from `Target+0x6f8`/`+0x700`, word offsets reserved from the chip-config's user-reserved SMEM region. The *table content* of the all-to-all tables (Types 8/9/0xa) is owned by [AllToAll Tables](alltoall-tables.md); the ND-ring tables (Types 0/1/2, 3) by [AllGather ND-Ring](allgather-nd-ring.md). This page owns the enum, the carrier mechanism, and the id read.

For reimplementation, the contract is:

- **The `Type` enum** — 12 tags (0..0xb), the `MeshDim`≡`Type` aliasing, the per-emitter overload of Types 3/4, and the one cross-collective tag (Type 5 = route schedule, shared by AllToAll/CollectivePermute/AllGather).
- **The materialization path** — `GetConstantFnForCollective` (per-HLO factory) → `GenerateConstants` (`AddConstant(Type, …)`) → `GetConstant(Type)` / `GetConstantTables`; the SwissTable storage and the static-vs-dynamic carrier gate.
- **The runtime collective-id read** — `GetReplicaId`/`GetPartitionId` = U32 `Sld` at a Target-resident word offset; `Target::Init` reserving that offset from the chip-config user-reserved SMEM region; the `partition_count==1` fold-to-0.

| | |
|---|---|
| **Mapper type** | `xla::jellyfish::ConstantMapper` — per-lowered-instruction object |
| **Storage** | Abseil SwissTable `FlatHashMap<ConstantMapper::Type, StatusOr<shared_ptr<const vector<int>>>>` |
| **Key space** | `ConstantMapper::Type` int 0..0xb (12 tags); `MeshNDInfo::MeshDim` aliases the same ints |
| **Add (Literal)** | `ConstantMapper::AddConstant(Type, StatusOr<Literal>)` @ `0x1c885ce0` |
| **Add (vector)** | `ConstantMapper::AddConstant(Type, StatusOr<vector<int>>)` @ `0x1c886300` |
| **Get** | `ConstantMapper::GetConstant(Type)` @ `0x1c886b00`; `HasConstant(Type)` @ `0x1c886920` |
| **Factory** | `GetConstantFnForCollective` @ `0x10c46f60` (per-HLO closures `$_0..$_3`) |
| **Barrier read-back** | `GetConstantTables` @ `0x10f07860` → `(InfoTable, InfoTable, optional<InfoTable>)` |
| **Replica-id read** | `net_util::GetReplicaId` @ `0x1c69a440` → U32 `Sld` at `Target+0x6f8` |
| **Partition-id read** | `net_util::GetPartitionId` @ `0x1c69a4a0` → U32 `Sld` at `Target+0x700` |
| **Offset reservation** | `Target::Init` @ `0x1d60fc20` via `GetUserReservedSmemBlock` @ `0x1d613b20` |

---

## The Type Enum

### Purpose

`ConstantMapper::Type` is a flat integer enum used as a SwissTable key, not a global table index. It tags every static constant a collective emitter needs so the lowering body can fetch each one back by tag during emission. There are 12 tags in use, 0 through 0xb, established by reading the `Type` immediate at every `AddConstant`/`GetConstant`/`HasConstant` call site and pairing it with the table-builder that produces the value.

### Mechanism — per-instance SwissTable, `MeshDim`≡`Type`

Each lowered collective HLO owns a `ConstantMapper`. `GenerateConstants(hlo, target, topo, region)` builds the constants it needs — replica/partition/route/binomial tables, as `xla::Literal` R1 int constants or as `vector<int>` — and registers each under a `Type` via `AddConstant`. The lowering body later fetches by `GetConstant(Type)`. The two `AddConstant(Type, …)` overloads (`Literal` @ `0x1c885ce0`, `vector<int>` @ `0x1c886300`) and `GetConstant(Type)` @ `0x1c886b00` are the whole API surface; `HasConstant(Type)` @ `0x1c886920` is the presence probe for the one optional tag.

`MeshNDInfo::MeshDim` shares the integer namespace. The `MeshDim` overloads do not have their own storage — they forward the mesh-axis index, unchanged, to the `Type` overloads:

```c
// ConstantMapper::AddConstant(MeshDim, StatusOr<Literal>)  — 0x1c886260
function AddConstant_MeshDim(this, mesh_dim /*esi*/, statusor_literal):
    // unwrap the StatusOr<Literal> into a bare Literal, then forward
    // mesh_dim (esi) UNCHANGED into the Type overload:
    return AddConstant_Type(this, /*Type=*/mesh_dim, literal)   // tail-call 0x1c885ce0

// ConstantMapper::GetConstant(MeshDim)  — 0x1c887560   (// attributes: thunk)
function GetConstant_MeshDim(this, mesh_dim):
    return GetConstant_Type(this, mesh_dim)                     // jmp 0x1c886b00
```

`GetConstant(MeshDim)` is a one-instruction thunk that jumps to `GetConstant(Type)`; `AddConstant(MeshDim, vector<int>)` @ `0x1c886620` forwards to `AddConstant(Type, vector<int>)` the same way. Therefore the per-mesh-axis ND-ring tables register and read back as Types 0/1/2 — the `MeshDim` of each axis.

> **QUIRK —** `Type` and `MeshDim` are the same integers but distinct C++ types, so a reimplementation that gives them separate hash maps will silently fail to find a constant added under `MeshDim 1` when it later looks up `Type 1`. They must alias the same key space, exactly as the binary forwards `MeshDim` into the `Type` overloads with no remapping.

### The 12 tags (0..0xb)

Each row pins the table-builder that feeds the `AddConstant(Type=k)` call (so the table *content* is anchored) and the consumer that reads it back. The English role names for the overloaded/opaque tags are attributed from the producing builder, not read from an enumerator descriptor — flagged `HIGH` where the producer is the only evidence.

| Type | Carrier | Table builder (producer) | Content / role |
|---|---|---|---|
| 0 | `MeshDim 0` | `CreateStaticNDRingReplicaInfoTable` / `CreateNDRingReplicaInfoTable` | ND-ring replica table, mesh **axis 0** |
| 1 | `MeshDim 1` | (same, axis 1) | ND-ring replica table, mesh **axis 1** |
| 2 | `MeshDim 2` | (same, axis 2) | ND-ring replica table, mesh **axis 2** |
| 3 | `Type` (overloaded) | AllReduce: `CreateReplicaInfoTable[ForLimitedIciRouting]`; AllGather: `CreateStaticNDRingReplicaInfoTable` | flat within-group replica/ordinal table (AR) **or** ND-ring table (AG) |
| 4 | `Type` (overloaded) | `CreateReplicaInfoTableForLimitedIciRouting` / `CreateStaticReplicaInfoTableForLimitedIciRouting` | limited-ICI-routing replica table / routing-table index |
| 5 | `Type` (**cross-collective**) | `net_router::CreateRoutingScheduleLiteral` / `CreateAllToAllRoutingScheduleTable` | the per-transfer ICI **route schedule** literal |
| 6 | `Type` | `net_util::CreateNDRingReplicaInfoTable` (AllReduce) | ring AllReduce ND reorder table |
| 7 | `Type` (+`Status`) | `CreateBinomialReplicaInfoTable` | binomial recursive-doubling butterfly table |
| 8 | `Type` | `GenerateAllToAllTables` (table A) | AllToAll barrier membership table A |
| 9 | `Type` | `GenerateAllToAllTables` (table B) | AllToAll barrier membership table B |
| 0xa | `Type` (`HasConstant`-gated) | `GenerateAllToAllTables` (table C) | AllToAll **optional** membership table C |
| 0xb | `Type` | `CreateCollectivePermuteTransfers` | CollectivePermute transfer/source-target table |

Three structural facts drive the table:

- **Type 5 is the only cross-collective tag.** The three routing-driven emitters — AllToAll, CollectivePermute, and AllGather (explicit-routing path) — produce *and* consume it; it carries `net_router`'s route-schedule literal — the per-transfer ICI route program. (AllReduce does **not** touch Type 5: its ring/binomial algorithms use Types 3/4/6/7 instead — byte-confirmed, no `AddConstant(…, 5, …)` in `AllReduceEmitter::GenerateConstants`.) Its internal int layout is not decoded here (see [Create Routing Schedule](../routing/create-routing-schedule.md) / [Route Table Generation](../routing/route-table-generation.md)).
- **Types 3 and 4 are overloaded slot indices.** The same tag carries a flat-replica table in AllReduce and an ND-ring/static table in AllGather. There is no collision because the mapper is per-lowered-instruction (see [§ The Materialization Path](#the-materialization-path)).
- **Type 7 takes both a table and a `Status`.** On the not-binomial-viable path the AllReduce emitter does `AddConstant(Type=7, Status)`; the later `GetConstant(7)` then surfaces that error. This is the binomial-table tag — see [Binomial Recursive Doubling](binomial-recursive-doubling.md).

> **NOTE —** Types 8/9/0xa *are* the AllToAll within-replica-group barrier's `(InfoTable A, InfoTable B, optional<InfoTable> C)`, read back as a triple by `GetConstantTables` @ `0x10f07860`. The table content, the two fill paths, and the index arithmetic are owned by [AllToAll Tables](alltoall-tables.md); this page owns only the tag assignment and the read-back path.

### Producers and consumers

The enum was reconstructed by reading the `Type`/`MeshDim` immediate at every call site. The producer/consumer split (every `AddConstant` is a producer, every `GetConstant`/`HasConstant` a consumer):

| Emitter | Producer — `GenerateConstants` | Tags added | Consumers |
|---|---|---|---|
| AllToAll | `AllToAllEmitterBase::GenerateConstants` @ `0x10f089a0` | 8, 9, 0xa, 5 | `CalculateWithLimitedIciRouting` (5), `GetConstantTables` (8/9/0xa) |
| CollectivePermute | `CollectivePermuteEmitter::GenerateConstants` @ `0x1346ff60` | 0xb, 5 | `EmitForLimitedIciRouting` (5), `Emit` (0xb) |
| AllReduce | `AllReduceEmitter::GenerateConstants` @ `0x1373cb60` | 3, 4, 6, 7(+Status); `MeshDim 0/1` (separate cross-module-ARS mapper) | `GetRingLocation` (3), `GetRingLocationWithReordering` (4), `EmitAllReduceFusion`/`ConstructAsyncFusionEmitter` (6), `BuildStrategyForCrossModuleARS` (0,1), `BinomialGroupData $_1` (7) |
| AllGather | `AllGatherEmitter::GenerateConstants` @ `0x13801be0` | `MeshDim 0/1/2`, 3, 4, 5 | `InitDim` (3/4/`MeshDim`), `EmitAllGatherWithExplicitRouting` (5) |

> **GOTCHA —** a reimplementation that drives off "all 12 tags exist for every collective" is wrong. Each emitter populates only the tags its algorithm needs; AllToAll never adds Type 3, AllReduce never adds Type 8. The `Type` space is the *union* over emitters, not a per-instance schema. Add only what the chosen algorithm produces, fetch only what it consumes.

---

## The Materialization Path

### Purpose

This is the dispatch and lifecycle that turns an HLO collective into a populated `ConstantMapper` and then reads tables back out during emission: one factory selects the right `GenerateConstants` by opcode, that function fills a fresh mapper, and the lowering body (or the barrier read-back) fetches by tag.

### Entry Point

```text
GetConstantFnForCollective (0x10c46f60)        ── HLO-opcode → factory closure
  └─ $_0 / $_1 / $_2 / $_3                      ── forward into one GenerateConstants
       └─ AllReduce/AllGather/AllToAll/CollectivePermute::GenerateConstants
            └─ AddConstant(Type, …)              ── populate the per-instance SwissTable
                 ⋮
       (lowering body)  GetConstant(Type)        ── 0x1c886b00, fetch by tag
       (AllToAll barrier) GetConstantTables      ── 0x10f07860, read 8/9/0xa as a triple
```

### Algorithm — factory, fill, fetch

`GetConstantFnForCollective` returns a `std::function<StatusOr<unique_ptr<ConstantMapper>>(HloInstruction*)>` whose target is one of four `$_0..$_3` closures (confirmed in the decompiled body: the function installs a `__policy_func::__call_func<…::$_0>` and so on). Each closure forwards into the matching emitter's `GenerateConstants`. So there is **one mapper per collective instruction**, built lazily by the opcode's `GenerateConstants`:

```c
function GetConstantFnForCollective(hlo, target, topo, region):   // 0x10c46f60
    switch opcode_class_of(hlo):
        all-reduce:        bind closure $_? → AllReduceEmitter::GenerateConstants
        all-gather:        bind closure $_? → AllGatherEmitter::GenerateConstants
        all-to-all:        bind closure $_? → AllToAllEmitterBase::GenerateConstants
        collective-permute:bind closure $_? → CollectivePermuteEmitter::GenerateConstants
    return std::function<StatusOr<unique_ptr<ConstantMapper>>(HloInstruction*)>

function GenerateConstants(hlo, target, topo, region):   // per-emitter
    mapper = new ConstantMapper
    for each table the algorithm needs:
        value = <table builder>(...)              // CreateReplicaInfoTable / GenerateAllToAllTables / …
        AddConstant(mapper, /*Type=*/tag, value)  // 0x1c885ce0 (Literal) or 0x1c886300 (vector)
    return mapper
```

`AddConstant(Type, Literal)` @ `0x1c885ce0` reads the `Type` int (`esi`→`r15d`) and stores a 448-byte (`0x1c0`) record keyed by the int at `record[+0]` — a linear scan/insert (`cmp record[+0]==Type`, stride `imul $0x1c0`) over the slot vector backing the SwissTable. `GetConstant(Type)` @ `0x1c886b00` is the hash lookup: it reads the `Type` (`edx`→`r15d`), CRC32-hashes it, group-probes the control bytes, and compares the candidate key against the requested `Type`.

### The static-vs-dynamic carrier gate

`AddConstant` has two overloads and the producer chooses per table whether to bake a **static** R1 `Literal` (`AddConstant(Type, StatusOr<Literal>)` @ `0x1c885ce0`) or to carry a **dynamic** `vector<int>` (`AddConstant(Type, StatusOr<vector<int>>)` @ `0x1c886300`). A static `Literal` is a constant the lowered program reads directly from a materialized R1 buffer; a dynamic `vector<int>` is materialized at runtime. The producer call sites for the same tag often appear twice — once with a `vec` argument, once with a `lit` argument — which is the carrier branch: the emitter picks the static `Literal` form when the table is fully known at compile time and the dynamic `vector` form otherwise. The all-to-all carrier choice is decoded in detail on [AllToAll Tables](alltoall-tables.md).

> **NOTE —** the SwissTable value type is `StatusOr<shared_ptr<const vector<int>>>` (from the mangled `FlatHashMapPolicy` instantiation). The `Literal` overload still routes through the same map; the static-vs-dynamic distinction is in *what the lowering does with the fetched constant* (read a baked R1 buffer vs. materialize), not in two separate maps.

### The barrier read-back — `GetConstantTables`

The AllToAll within-replica-group barrier does not call `GetConstant` directly; it goes through `GetConstantTables`, which reads the three membership tags and returns them as the barrier's `InfoTable` triple:

```c
function GetConstantTables(hlo, mapper):           // 0x10f07860
    A = GetConstant(mapper, /*Type=*/8)            // 0x1c886b00 — barrier InfoTable A
    if A is error: return A
    B = GetConstant(mapper, /*Type=*/9)            //            — barrier InfoTable B
    if B is error: return B                        // AddSourceLocation: all_to_all_emitter_base.cc:323
    C = optional{}
    if HasConstant(mapper, /*Type=*/0xa):          // 0x1c886920 — HasConstant(a2, 10)
        C = GetConstant(mapper, /*Type=*/0xa)      //            — optional InfoTable C
    return tuple<InfoTable, InfoTable, optional<InfoTable>>(A, B, C)
```

The decompiled body shows the three `GetConstant` calls and the `HasConstant(a2, 10)` gate on the third (10 = 0xa), confirming the read side gates table C on presence exactly as `GenerateAllToAllTables` gates its construction. The returned triple lands directly in the barrier-start argument registers.

### Function Map

| Function | Address | Role |
|---|---|---|
| `ConstantMapper::AddConstant(Type, StatusOr<Literal>)` | `0x1c885ce0` | static-`Literal` registrar; 448-byte record keyed by `record[+0]==Type` |
| `ConstantMapper::AddConstant(Type, StatusOr<vector<int>>)` | `0x1c886300` | dynamic-`vector<int>` registrar |
| `ConstantMapper::AddConstant(Type, Status)` | `0x1c8866c0` | error-carrier registrar (Type 7 not-viable path) |
| `ConstantMapper::GetConstant(Type)` | `0x1c886b00` | SwissTable lookup (CRC32 hash of `Type`) |
| `ConstantMapper::HasConstant(Type)` | `0x1c886920` | presence probe (gates Type 0xa) |
| `ConstantMapper::AddConstant(MeshDim, …)` | `0x1c886260` / `0x1c886620` | `MeshDim`→`Type` forwarders (esi unchanged) |
| `ConstantMapper::GetConstant(MeshDim)` | `0x1c887560` | thunk `jmp 0x1c886b00` |
| `GetConstantFnForCollective` | `0x10c46f60` | per-HLO factory; closures `$_0..$_3` |
| `GetConstantTables` | `0x10f07860` | reads Types 8/9/0xa → `InfoTable` triple |

> **QUIRK —** the Type-3/Type-4 overload is collision-free *only* because the mapper is per-lowered-instruction. That `GetConstantFnForCollective` returns a fresh closure per HLO is byte-confirmed; that this yields exactly one mapper per instruction is the structural basis and is `HIGH`, not `CERTAIN` — a reimplementer who shares one mapper across instructions would alias AllReduce's flat Type 3 onto AllGather's ND-ring Type 3.

---

## The Runtime Collective-Id Read

### Purpose

A collective emitter needs to know "which rank am I" — its `replica_id` and `partition_id` — to index the static tables above (the binomial table index is `replica_id*8 + col`; the flat barrier reader keys on `(replica_id, partition_id)`). That id is **not** a compile-time constant: each core reads its own id at run time as a single U32 scalar from a fixed SMEM word. The compiler only emits the load; the host runtime / TPU firmware deposits each core's id into the reserved word before the program runs.

### Algorithm — one U32 `Sld` from a Target-resident word offset

```c
function net_util::GetReplicaId(b /*LloRegionBuilder*/):       // 0x1c69a440
    word_off = b.target()->ReplicaIdLocationWordOffset()       // Target+0x6f8
    ptr      = b.SmemWordImmPtr(word_off, "replica id location")// ImmPtr(off, U32, MS=kSmem)
    return     b.Sld(ptr, /*pred=*/nullptr)                    // CreateScalarLoad → LloValue

function net_util::GetPartitionId(b):                          // 0x1c69a4a0  (identical shape)
    word_off = b.target()->PartitionIdLocationWordOffset()     // Target+0x700
    ptr      = b.SmemWordImmPtr(word_off, "partition id location")
    return     b.Sld(ptr, nullptr)
```

Both decompile to exactly the three-call body above. The annotation strings `"replica id location"` (len 0x13) and `"partition id location"` (len 0x15) are the literal arguments.

`SmemWordImmPtr` @ `0x1d516880` builds the pointer: it asserts `target().SmemWordSizeBytes() == sizeof(uint32_t)` (a `LloCheckForFailure` guard with the file string `llo_region_builder.cc`), then `MakeValidatedShape(8, …)` — PrimitiveType **8 = U32**, dims `[]` — and `ImmPtr(offset, shape, MemorySpace=5=kSmem, annotation)`. So the id is a scalar U32 in SMEM.

`Sld` @ `0x1d516a20` validates the operand's memory space before emitting the load:

```c
function Sld(b, ptr, pred):                                    // 0x1d516a20
    ms = (ptr->flags_byte[0xb] >> 2) & 0x1F                    // memory-space field
    if (ms - 9) >= 2 && ms != 5:                               // accept kSmem(5) or sflag tiers 9..10
        fail("...llo_region_builder.cc:5365")
    inst = LloInstruction::CreateScalarLoad(ptr, pred, region) // 0x1d516a54
    return b.AppendInstruction(inst)                           // 0x1d516a61
```

The accepted set is MemorySpace 5 (`kSmem`) or the SFLAG-class tiers 9..10 — see [SMEM Scalar Memory](../memory/smem-scalar-memory.md) for the memory-space taxonomy. The id read always uses `kSmem`.

### The HLO ops and the `partition_count==1` fold

The HLO `replica-id` / `partition-id` ops lower straight to these reads:

- `LoweringEmitter::HandleReplicaId` @ `0x10c34260` → `GetReplicaId` (@ `0x10c34306`).
- `LoweringEmitter::HandlePartitionId` @ `0x10c33940` → `GetPartitionId` (@ `0x10c339e6`).

Collective emitters call the read through a helper that folds the single-partition case:

```c
function collective_lowering_utils::GetPartitionId(b, hlo):    // 0x13819500
    if hlo->GetModule()->config().partition_count() == 1:      // module-config[+0x178] == 1
        return b.SimmS32(0)                                     // compile-time constant 0
    else:
        return net_util::GetPartitionId(b)                     // the SMEM read (tail-jump)
```

When there is no model parallelism (`partition_count == 1`) the partition id is a compile-time `0` and no SMEM load is emitted; otherwise it tail-jumps to the SMEM read. (Confirmed: `module->config[+376] == 1` → `SimmS32(0)`, else `net_util::GetPartitionId`.)

> **GOTCHA —** there is no `replica-id` analog of this fold. `HandleReplicaId` always emits the SMEM load; only `partition_id` is folded to 0 in the single-partition case. A reimplementation that mirrors the fold onto replica-id would constant-fold the wrong id.

### Who reads the runtime id

`GetReplicaId` @ `0x1c69a440` has 12 call sites; `GetPartitionId` @ `0x1c69a4a0` has 8. Every collective emitter reads `(replica_id, partition_id)` from these. Notable consumers: `HandleReplicaId`/`HandlePartitionId` (the HLO ops), `AllToAllEmitter::Init` / `RaggedAllToAllEmitter::Init`, the `CollectivePermuteEmitter` constructor and barrier, the `AllReduceEmitter`/`AllGatherEmitter` constructors, and `LoadBinomialReplicaInfoTable` @ `0x1375fca0` — whose table index is exactly this `replica_id` (see [Binomial Recursive Doubling](binomial-recursive-doubling.md)).

### Function Map

| Function | Address | Role |
|---|---|---|
| `net_util::GetReplicaId` | `0x1c69a440` | U32 `Sld` at `Target+0x6f8`, annotation "replica id location" |
| `net_util::GetPartitionId` | `0x1c69a4a0` | U32 `Sld` at `Target+0x700`, annotation "partition id location" |
| `LloRegionBuilder::SmemWordImmPtr` | `0x1d516880` | builds `ImmPtr(off, U32, MS=kSmem)`; asserts word size == 4 |
| `LloRegionBuilder::Sld` | `0x1d516a20` | MS-validates then `CreateScalarLoad` + `AppendInstruction` |
| `LoweringEmitter::HandleReplicaId` | `0x10c34260` | HLO `replica-id` op → `GetReplicaId` |
| `LoweringEmitter::HandlePartitionId` | `0x10c33940` | HLO `partition-id` op → `GetPartitionId` |
| `collective_lowering_utils::GetPartitionId` | `0x13819500` | `partition_count==1` → `SimmS32(0)`, else `net_util::GetPartitionId` |

---

## The SMEM Word-Offset Reservation

### Purpose

The word offsets the id reads use (`Target+0x6f8`, `+0x700`, …) are not hardcoded constants. They are plain field reads from the `Target` object, and the fields are filled at `Target::Init` from the chip-config's user-reserved SMEM region. The relative layout (which word in the region holds which id) is fixed; the absolute offset is region-base-relative and varies by codename/topology.

### The Target SMEM-id field cluster (`Target+0x6c0 .. +0x710`)

Each accessor is a one-line field read (`mov <off>(%rdi),%rax; ret`); the fields are `long` (8-byte) SMEM word indices.

| Target off | Accessor (@VMA) | Datum |
|---|---|---|
| `0x6c0` | `OutfeedBasePtrWordOffset` @ `0x1d617bc0` | outfeed base ptr |
| `0x6c8` | `OutfeedProducerHostSyncFlagNumberWordOffset` @ `0x1d617be0` | outfeed producer host sflag # |
| `0x6d0` | `CachedOutfeedProducerOffsetWordOffset` @ `0x1d617c00` | cached outfeed producer offset |
| `0x6d8` | `InfeedPtrLocationWordOffset` @ `0x1d617ba0` | infeed ptr |
| `0x6e0` | `ChipIdLocationWordOffset` @ `0x1d617c20` | chip id |
| `0x6e8` | `CoreIndexLocationWordOffset` @ `0x1d617c40` | core index |
| `0x6f0` | `PhysicalChipBoundsLocationWordOffset` @ `0x1d617c60` | physical chip bounds |
| `0x6f8` | `ReplicaIdLocationWordOffset` @ `0x1d617c80` | **replica id** |
| `0x700` | `PartitionIdLocationWordOffset` @ `0x1d617ca0` | **partition id** |
| `0x708` | `SliceIdLocationWordOffset` @ `0x1d617cc0` | slice id |
| `0x710` | `SubsliceOriginLocationWordOffset` @ `0x1d617ce0` | subslice origin |

### Algorithm — `Target::Init` reserves from the chip-config

```c
function Target::Init(topology, …, Target* out, …):            // 0x1d60fc20
    reservation = chip_config.GetMemoryReservation()           // 0x20afcf00
    // identity ids come from the TOP user-reserved SMEM region:
    out[+0x6f8] = GetUserReservedSmemBlock(reservation, /*type=*/1, /*top=*/1).word_offset // replica_id
    out[+0x700] = GetUserReservedSmemBlock(reservation, /*type=*/2, /*top=*/1).word_offset // partition_id
    out[+0x708] = GetUserReservedSmemBlock(reservation, /*type=*/3, /*top=*/1).word_offset // slice_id
    out[+0x710] = GetUserReservedSmemBlock(reservation, /*type=*/7, …).word_offset         // subslice_origin
    // +0x6c0..+0x6f0 from TpuMemoryReservation::GetRegionForType / TpuChipConfig::GetUserStack

function Target::GetUserReservedSmemBlock(res, type, top):      // 0x1d613b20
    region = TpuMemoryReservation::GetUserRegion(res)
    blocks = top ? kBlocksTop /*0xb53c180*/ : kBlocksBottom /*0xb53c230*/
    entry  = blocks[ index matching entry.type[+0] == type ]   // 24-byte entries
    if top:    abs_word = region.base + res.base - (entry.offset[+8] + entry.num_words[+0x10])  // counts down
    else:      abs_word = entry.offset[+8] + res.base                                            // counts up
    return { word_offset=abs_word, num_words=entry.num_words, type=entry.type }
```

`GetUserReservedSmemBlock` matches the requested `type` against a static 24-byte-entry table (`entry = {int type[+0], int pad[+4], long word_offset[+8], long num_words[+0x10]}`), selects the TOP or BOTTOM `kBlocks` table by the `top` bool, then computes the absolute SMEM word offset.

The two reserved-block layout tables:

```text
GetUserReservedTopSmemBlocks::kBlocks @ 0xb53c180   (7 entries, each 1 word)
  type 0 @ w0 | type 1 (replica_id) @ w1 | type 2 (partition_id) @ w2 | type 3 (slice_id) @ w3
  type 4 @ w4 | type 6 @ w5 | type 7 (subslice_origin) @ w6

GetUserReservedBottomSmemBlocks::kBlocks @ 0xb53c230 (3 entries)
  type 5 @ w0 (0x25=37 words) | type 8 @ w0x25 (1 word) | type 9 @ w0x26 (1 word)
```

So `replica_id`/`partition_id`/`slice_id` occupy three consecutive 1-word slots at relative words 1/2/3 of the TOP region. Their absolute offset is region-base-relative and depends on the per-core `TpuMemoryReservation` (so it varies by codename/topology, but the relative layout is fixed).

> **NOTE —** the `UserReservedSmemType` enumerator names (1=replica_id, 2=partition_id, 3=slice_id, 7=subslice_origin) are attributed from the `Target::Init` store target (each `GetUserReservedSmemBlock(type=k)` result is stored into the matching `…LocationWordOffset` field), not read from an enum descriptor — `HIGH` confidence. The `type` integers and the `kBlocks` word offsets are byte-confirmed.

### The compile/runtime split

> **GOTCHA —** libtpu only *reserves the slot* (`Target::Init`) and *emits the read* (`GetReplicaId`). It never writes a value into the reserved SMEM word. The host runtime / TPU firmware deposits each core's `(replica_id, partition_id, slice_id)` into these words before kernel launch; that writer is outside `libtpu.so` and is inferred from the read-only use, not observed here. A reimplementation that expects the compiler to emit the id store will find no such store.

### The datapath, end to end

| Stage | Function (VMA) | Output |
|---|---|---|
| reserve id SMEM word (compile) | `Target::Init` @ `0x1d60fc20` | `Target+0x6f8`/`+0x700`/`+0x708` word offsets |
| ↳ slot lookup | `GetUserReservedSmemBlock` @ `0x1d613b20` (`kBlocks`) | type → region-relative word offset |
| emit id read (lowering) | `GetReplicaId` @ `0x1c69a440` / `GetPartitionId` @ `0x1c69a4a0` | `LloValue` (U32 id) |
| ↳ build SMEM ptr | `SmemWordImmPtr` @ `0x1d516880` | `ImmPtr(U32, MS=kSmem)` to the reserved word |
| ↳ scalar load | `Sld` @ `0x1d516a20` (`CreateScalarLoad`) | the per-core `replica_id`/`partition_id` |
| HLO replica-id / partition-id op | `HandleReplicaId` @ `0x10c34260` / `HandlePartitionId` @ `0x10c33940` | the id `LloValue` |

---

## What Is Not Decoded Here

- **Type 5 internal layout.** Confirmed as the cross-collective route-schedule carrier; its int encoding (per-transfer route program) is owned by the routing subsystem ([Create Routing Schedule](../routing/create-routing-schedule.md), [Route Table Generation](../routing/route-table-generation.md)).
- **Type 8/9/0xa and 0xb table content.** Pinned to their builders by the `AddConstant` call site; the per-element semantics are owned by [AllToAll Tables](alltoall-tables.md) (8/9/0xa) and the CollectivePermute emitter (0xb).
- **Symbolic enumerator names** for `ConstantMapper::Type` (`Type::k…`) and `UserReservedSmemType`: the tag *integers*, their *producers*, and the type→word-offset `kBlocks` tables are byte-confirmed; the enum's own descriptor/symbol table was not extracted, so the English role names are attributed from producers and store targets.
- **One-mapper-per-instruction** (the basis for the Type 3/4 overload being collision-free): supported by `GetConstantFnForCollective` returning a per-HLO factory, not separately proven to be exactly one mapper per lowered instruction.
- **The runtime/firmware id writer** that deposits the per-core ids into the reserved words — outside `libtpu.so`.

---

## Cross-References

- [AllToAll Tables](alltoall-tables.md) — owns the content of Types 8/9/0xa (the barrier membership tables) and the static-vs-dynamic carrier decode for the all-to-all case
- [AllGather ND-Ring](allgather-nd-ring.md) — owns the ND-ring replica tables (Types 0/1/2, 3) and the `InitDim` axis walk that consumes them via `GetConstant(MeshDim)`
- [Binomial Recursive Doubling](binomial-recursive-doubling.md) — the Type 7 binomial butterfly table; its index is the `GetReplicaId` SMEM read documented here
- [SelectNDStrategy](strategy-nd-picker.md) — the strategy decision that determines which `GenerateConstants` runs and which tags it populates
- [On-Pod Collectives — Section Map](overview.md) — the collective-lowering pipeline this constant pool serves
- [Create Routing Schedule](../routing/create-routing-schedule.md) — the cross-collective Type 5 route-schedule literal producer
- [Route Table Generation](../routing/route-table-generation.md) — the route-table generation behind Type 5
- [SMEM Scalar Memory](../memory/smem-scalar-memory.md) — the memory-space taxonomy (`kSmem`=5) the `Sld` validates and the scalar-load model the id read uses
