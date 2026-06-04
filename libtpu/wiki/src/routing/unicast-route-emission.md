# Unicast Route Emission

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset). **Status:** Reimplementation-grade · **Evidence grade:** Confirmed (byte-anchored) — `ParallelRoutingTableGenerator::CreateUnicastRoutingTables` (`@0x1fbd5340`), its two capture lambdas (`@0x1fbd70c0` / `@0x1fbd7240`), `CreateSrcDestUnicastRoutingTable` (`@0x1fbd5640`), and the serial twins (`@0x1fbd94a0` / `@0x1fbd9580`) were each cross-checked against the IDA decompile. **Part XII — Interconnect & Routing** / Routing · [back to index](../index.md)

## Abstract

This page documents the **unicast route-table emission layer**: the driver that turns a slice's topology into the per-chip routing tables the silicon programs. It sits **one level above** the per-`(src,dst)` route-table-entry mapper (which returns a single next-hop index) and one level below `Generate()`. Its job is to sweep every `(src, dst)` chip pair in the slice, ask the path generator for the hop sequence from `src` to `dst`, and write each hop's `{output_link, vc_control}` action into the right `superpod::routing::RoutingTable` row — producing the dense forwarding state every chip needs to relay a DMA toward any destination.

The layer is a **two-dimensional sweep**: the source axis (outer) and the destination axis (inner). `ParallelRoutingTableGenerator::CreateUnicastRoutingTables` (`@0x1fbd5340`) parallelizes the source axis by spawning **one fiber per source chip**; within each fiber the destination loop runs serially, calling `CreateSrcDestUnicastRoutingTable` (`@0x1fbd5640`) once per `(src, dst)` pair. The base `RoutingTableGenerator` ships a byte-identical **serial** twin (`@0x1fbd94a0` driving `WalkCreateSrcDestUnicastRoutingTable @0x1fbd9580`) that drops only the fiber wrapper. The unit of error is per-source: each fiber's `absl::Status` slot captures the first failure on its destination loop, and the driver propagates the first non-OK slot after `Join`.

`CreateSrcDestUnicastRoutingTable` is the heart. For one `(src, dst)` it resolves both chip coordinates through the topology interface, fetches the source's two writable tables (egress and next-hop), obtains an `IciRoutePath` — either from the precomputed `RouteTargetCache` (when `use_cache_` is set) or computed live by `GetStaticPath` — and then either writes the source's first-hop egress link (multi-hop case) or marks the destination terminal (the `src == dst` case). The downstream per-hop fan-out (`PopulateRoutingTable` → `GetNextHopAction`) that walks the remaining hops and the table-format internals (`GetTableIndex`, the three stride-`0x48` `RoutingTable` arrays, the `RouteTargetCache` 2-D path array) are described here as the layout this driver consumes and writes.

The single-`(src,dst)` index mapper (`DmaDestinationRoutingTableEntryMapper::Map`) and the physical↔logical placement map (`GetPhysicalToLogicalMapping3D`) are **not** owned here — they are on **[Route-Table Generation](route-table-generation.md)**. The deterministic per-pair path generator (`GetStaticPath`) is on **[Static-Path Generation](get-static-path.md)**. This page picks up at the emission driver and the build loop above the entry mapper.

For reimplementation, the contract is:

- **The 2-D sweep shape** — source axis parallel (one fiber per source chip), destination axis serial inside each fiber; per-source first-error capture into a pre-OK `vector<absl::Status>`.
- **The per-`(src,dst)` entry build** — coordinate resolve → fetch egress + next-hop tables → path source dispatch (cached vs live) → first-hop egress write vs terminal write → remaining-hop fan-out.
- **The table layout it writes** — `GetTableIndex` chip→dense-row map, the three RoutingTable arrays (egress / egress-next-hop / link-next-hop, stride `0x48`), the `RouteTargetCache` `[src_row][dst]` path array, and the `0xfe`/`0xff` sentinels.

## At a glance

| Aspect | Value (byte-anchored) |
|--------|----------------------|
| Parallel driver | `ParallelRoutingTableGenerator::CreateUnicastRoutingTables` `@0x1fbd5340` |
| Serial driver (base) | `RoutingTableGenerator::CreateUnicastRoutingTables` `@0x1fbd94a0` |
| Outer fiber lambda | `$_0` RemoteInvoker `@0x1fbd70c0` (one task per source chip) |
| Inner dst lambda | RemoteInvoker `@0x1fbd7240` (dst loop + `SetChannelMerges`) |
| Per-`(src,dst)` build | `CreateSrcDestUnicastRoutingTable` `@0x1fbd5640` |
| Serial per-`(src,dst)` build | `WalkCreateSrcDestUnicastRoutingTable` `@0x1fbd9580` |
| Per-hop action | `PopulateRoutingTable` `@0x1fbdb5c0` → `GetNextHopAction` `@0x1fbda6a0` |
| Chip→row map | `GetTableIndex` `@0x1fbdd000` (crc32 swiss-table) |
| Source egress table | `GetEgressTable` `@0x1fbdc040` (`gen+0xa8`, stride `0x48`) |
| Next-hop tables | `GetNextHopTable` `@0x1fbdbb00` (`gen+0xc0` egress-next / `gen+0xd8` link-next) |
| Path cache | `RouteTargetCache::GetPath` `@0x1fbd42c0` (`[src_row][dst]`, `IciRoutePath` stride `0x50`) |
| `use_cache_` flag | `gen+0x129` (`*((_BYTE*)this+297)`) |
| numsrcs / chipcount | `gen+0x3c` (Span size) / `gen+0x40` (`topology->TotalSize()`) |
| RoutingTable setters | `SetUnicastTarget` `@0x1ffdfce0` · `SetUnicastTerminal` `@0x1ffe0040` · `SetUnicastVcControl` `@0x1ffe0320` |
| Source TU | `parallel_routing_table_generator.cc` (`@0x875bc20` = `n_hop_routing_table_generator.cc` family) |

---

## 1. Where this layer sits

### 1.1 The route-generation stack

`Generate()` orchestrates four stages; this page owns the last two:

```text
RoutingTableGeneratorFactory::CreateGenerator @0x1fbd3dc0   ── pick base / Parallel / NHop / multipod
  └─ Generate
       ├─ InitializeGenerator @0x1fbd7740                    ── topology@+0x20, LinkMap@+0x30,
       │                                                        TableIndex map@+0x108, 3 RoutingTable arrays
       ├─ [use_cache_] RouteTargetCache populate              ── PopulatePathCache + PopulateLinkNextCache
       ├─ CreateUnicastRoutingTables  ◄── THIS PAGE           ── the 2-D src×dst sweep
       │    └─ CreateSrcDestUnicastRoutingTable ◄── THIS PAGE ── one (src,dst) entry build
       │         └─ PopulateRoutingTable → GetNextHopAction   ── per-hop {output_link, next_chip, vc}
       └─ (table installed by SetRoutingTable on the runtime side)
```

The factory selects among four concrete generators that share the same `Generate()` skeleton and the same per-`(src,dst)` entry-build logic, differing only in **serial vs fibered** sweep and in pod scope:

| Generator | Vtable | `CreateUnicastRoutingTables` | Sweep |
|-----------|--------|------------------------------|-------|
| `RoutingTableGenerator` (base) | `0x21f56fb0` | `@0x1fbd94a0` | serial — plain DOR |
| `ParallelRoutingTableGenerator` | `0x21f56f28` | `@0x1fbd5340` | one fiber per source |
| `viperlite_pod::NHopRoutingTableGenerator` | `0x21f57cc8` | (n-hop) | n-hop port tables |
| `multipod::RoutingTableGenerator` | `0x21f57c40` | (multipod) | inter-pod |

> **NOTE —** the base and Parallel generators build **identical tables**; the Parallel one only adds the fiber layer over the source axis. A reimplementation can ship the serial driver first and bolt on parallelism without changing the entry-build logic. `CreateGenerator @0x1fbd3dc0` dispatches on the routing-strategy enum gated by `FLAGS_tpu_slice_builder_ici_route_force_n_hop` (`@0x22479fb8`).

### 1.2 The generator state this layer reads

The driver and the entry build read a fixed set of generator fields. These are the inputs a reimplementer must have populated by `InitializeGenerator` before emission runs:

| Field | Offset | Meaning |
|-------|--------|---------|
| topology | `gen+0x20` (`*((qword*)this+4)`) | abstract `ToroidalTopologyInterface*`; vtable-dispatched |
| LinkMap | `gen+0x30` (`*((qword*)this+6)`) | `(chip, Direction) → output_link` resolver |
| numsrcs | `gen+0x3c` (`*((int*)this+15)`) | source-Span size; status-vector length |
| chipcount | `gen+0x40` (`*((int*)this+16)`) | `topology->TotalSize()`; inner dst-loop bound |
| compacted | `gen+0x18` | next-hop-table compaction flag |
| nexthop-enable | `gen+0x1a` | next-hop tables present |
| egress array | `gen+0xa8` / count `gen+0xb0` | per-source first-hop `RoutingTable[]`, stride `0x48` |
| egress-next-hop | `gen+0xc0` / count `gen+0xc8` | per-chip forwarding `RoutingTable[]`, stride `0x48` |
| link-next-hop | `gen+0xd8` / count `gen+0xe0` | per-(chip,in-link) `RoutingTable[]`, stride `0x48` |
| TableIndex map | `gen+0x108`/`+0x118`/`+0x120` | crc32 swiss-table chip_id → dense row |
| `use_cache_` | `gen+0x129` | cached `GetPath` vs live `GetStaticPath` |
| RouteTargetCache | `gen+0x130` (`*((qword*)this+38)`) | the 2-D path + per-link byte caches |

> **[CONFIRMED]** Every offset above appears as a direct field access in `CreateUnicastRoutingTables`/`CreateSrcDestUnicastRoutingTable`: `gen+0x3c` as `*(int*)(a1+60)` (status-vector size, `@0x1fbd5343`), `gen+0x40` as `*((int*)v2+16)` (inner-loop bound, `@0x1fbd7240`), `use_cache_` as `*((_BYTE*)this+297)` (`cmpb $1,0x129(%r15)` `@0x1fbd573d`), and the `RouteTargetCache` at `*((qword*)this+38)` (`gen+0x130`). The `use_cache_` field name is confirmed by the `LogMessageFatal(..., 417, "use_cache_")` consistency assert.

---

## 2. The 2-D sweep — `CreateUnicastRoutingTables`

### 2.1 Purpose

Build every chip's routing table by iterating the full `src × dst` grid. The destination loop is the heart of correctness; the source loop is the parallelism axis. The Parallel variant spawns a fiber per source so that the (independent) per-source tables fill concurrently, then collapses the per-fiber statuses to one.

### 2.2 Entry point

```text
ParallelRoutingTableGenerator::CreateUnicastRoutingTables @0x1fbd5340
  ├─ alloc vector<absl::Status> result[gen+0x3c]   ── pre-filled OK (discriminant 1)
  ├─ thread::Fiber (352 B)                          ── runs the $_0 outer invoker
  │    └─ $_0 outer lambda @0x1fbd70c0              ── one Bundle task per source chip
  │         └─ inner dst lambda @0x1fbd7240         ── dst loop + SetChannelMerges
  │              └─ CreateSrcDestUnicastRoutingTable(src,dst)
  ├─ Fiber::Start / Fiber::Join
  └─ scan result[] → first non-OK, else OK
```

### 2.3 Algorithm

```c
function CreateUnicastRoutingTables(gen, Span<int> srcs):   // @0x1fbd5340
    n = gen[+0x3c]                                  // numsrcs (= Span size)
    result = new absl::Status[n]                    // operator new(8*n)
    fill(result, OK)                                // each slot = 1 (ok discriminant)
                                                    // vmovddup qword_A2DF228 + tail *q=1 loop
    fiber = new thread::Fiber(352)                  // operator new(352, 16)
    bind fiber -> RemoteInvoker<$_0>{ gen, &srcs, &result }   // @0x1fbd70c0
    fiber.Start()
    fiber.Join()
    for i in [0, n):                                // first-error scan
        if result[i] != OK: return result[i]
    return OK

// outer lambda — one Bundle task per source chip            // @0x1fbd70c0
function Outer(gen, srcs, result):
    bundle = thread::Bundle()
    for src in srcs:                                // Span step +4
        row = GetTableIndex(src).value              // dense row index
        bundle.AddImpl( Inner{ gen, src, &result[row] } )   // one fiber per source
    bundle.JoinAll()

// inner lambda — the destination loop                       // @0x1fbd7240
function Inner(gen, src, result_slot):
    chipcount = gen[+0x40]                          // *((int*)gen+16)
    for dst in [0, chipcount):
        st = CreateSrcDestUnicastRoutingTable(gen, src, dst)
        if *result_slot == OK and st != OK:         // first-error capture
            *result_slot = st
        else if st is error: Unref(st)
    st = SetChannelMerges(gen, src)                 // @0x1fbda1e0 — merge per-source VC/channel
    if *result_slot == OK and st != OK: *result_slot = st
```

> **[CONFIRMED]** The status vector is `operator new(8*v5)` with `v5 = *(int*)(a1+60)` = `gen+0x3c`, pre-filled to OK by the `vmovddup cs:qword_A2DF228` block plus the `*(_QWORD*)v9 = 1` tail loop (`@0x1fbd5400`). The `thread::Fiber` is `operator new(352, 16)`, bound to `RemoteInvoker<...CreateUnicastRoutingTables...$_0&&>` capturing `{gen, &srcs, &result}` (the 24-byte `operator new(0x18)` closure at `@0x1fbd7240`'s caller), then `Start`/`Join`. The post-Join scan walks `result[i]` for the first slot `!= OK` (`@0x1fbd5570`).

> **[CONFIRMED]** The inner lambda (`@0x1fbd7240`) loops `dst = 0 .. *((int*)v2+16)-1` (chipcount = `gen+0x40`), calls `CreateSrcDestUnicastRoutingTable(gen, src, dst)`, and applies the first-error rule `if (*result_slot == 1 && st != 1) *result_slot = st;` (lines 23-32 of its decompile). After the loop it calls `SetChannelMerges(gen, src)` (line 38) with the same first-error capture. The outer lambda (`@0x1fbd70c0`) opens a `thread::Bundle`, resolves each source through `GetTableIndex`, and submits one `Bundle::AddImpl` task per source before `JoinAll`.

> **QUIRK —** the error model is **per source, first-error-wins**. Each fiber owns exactly one `result[]` slot (keyed by `GetTableIndex(src)`, not by the raw chip id), and a later dst failure cannot overwrite an earlier one. A reimplementation that records the *last* error, or that shares one status across sources, diverges from the binary's diagnostics.

### 2.4 The serial twin

The base generator's `CreateUnicastRoutingTables @0x1fbd94a0` is the same sweep with no fiber:

```c
function CreateUnicastRoutingTables_serial(gen, Span<int> srcs):   // @0x1fbd94a0
    for src in srcs:
        for dst in [0, gen[+0x40]):
            WalkCreateSrcDestUnicastRoutingTable(gen, src, dst)     // @0x1fbd9580
        SetChannelMerges(gen, src)
```

> **[CONFIRMED]** `@0x1fbd94a0` contains the nested `for src / for dst<[gen+0x40]` loop calling `WalkCreateSrcDestUnicastRoutingTable` (`@0x1fbd94f9`) followed by `SetChannelMerges` (`@0x1fbd951a`). `WalkCreateSrcDestUnicastRoutingTable @0x1fbd9580` mirrors the parallel entry build (GetCoordinate, GetStaticPath, egress `gen+0xa8`, HopDirection, SetUnicastTerminal) but is hard-wired to the live `GetStaticPath` path source — it is the non-cached analogue of §3.

---

## 3. The per-`(src,dst)` entry build — `CreateSrcDestUnicastRoutingTable`

### 3.1 Purpose

Produce, for a single `(src, dst)` pair, the routing-table entries that let a DMA launched at `src` reach `dst`: the **source's first-hop egress** entry plus the chain of **intermediate forwarding** entries along the path (or a single **terminal** entry when `src == dst`). It owns the path-source dispatch and the first hop; it delegates the remaining hops to `PopulateRoutingTable`.

### 3.2 Signature

```cpp
// @0x1fbd5640  (parallel_routing_table_generator.cc)
absl::Status
ParallelRoutingTableGenerator::CreateSrcDestUnicastRoutingTable(int src /*a2*/, int dst /*a3*/);
```

The return is a bare `absl::Status` (not the `StatusOr<int>` of the entry mapper); all side effects land in the generator's `RoutingTable` arrays.

### 3.3 Algorithm

```c
function CreateSrcDestUnicastRoutingTable(gen, int src, int dst):    // @0x1fbd5640
    // 1. resolve both chip coordinates (topology vtable slot +0x88)
    src_coord = topology->GetCoordinate(src)     // @0x1fbd566d, line 371 src-loc
    dst_coord = topology->GetCoordinate(dst)     // @0x1fbd56a7, line 373
        // either StatusOr error short-circuits to the cleanup chain

    // 2. fetch the source's two writable tables
    egress  = GetEgressTable(src)                // @0x1fbd56db, line 375 -> &egress[TableIndex(src)]
    nexthop = GetNextHopTable(src, /*egress=*/true)   // @0x1fbd5701, line 377

    // 3. PATH SOURCE dispatch on use_cache_ (gen+0x129)
    if gen[+0x129] == 1:                         // @0x1fbd573d  (cmpb $1,0x129(%r15))
        path = RouteTargetCache::GetPath(src, dst)        // @0x1fbd42c0 — precomputed IciRoutePath
    else:
        path = GetStaticPath(src_coord, dst_coord)        // @0x1fbd57f6, line 384 — compute now

    // 4. emit
    if path.num_hops > 0:                        // *(int*)(path+0x20) > 0 ; @0x1fbd5742 / line 388
        dir0 = path.HopDirection(0)              // @0x20c01900, line 401
        link = LinkMap::GetLink(src, dir0)       // @0x1ffe3940, line 403 — source's SerDes egress
        if egress.SetUnicastTarget(dst, link, /*overwrite=*/false) == OK:   // @0x1ffdfce0, line 405
            PopulateRoutingTable(src_coord, dst_coord, src, dst, path, /*hop=*/0)  // @0x1fbd5ad0, line 410
            if gen[+0x1a] (nexthop-enable):
                // cached fast path mirrors the first link-next byte into the nexthop table
                assert use_cache_                // LogMessageFatal(..., 417, "use_cache_")
                b = cache.link_next_byte[ src*numdst + dst ]    // [gen+0x130]+0x48, line 428
                if b != 0xfe:                    // unset sentinel -> skip
                    if b == 0xff: nexthop.SetUnicastTerminal(dst, false)   // line 431
                    else:         nexthop.SetUnicastTarget(dst, b, false)   // line 434
                    nexthop.SetUnicastVcControl(dst, /*vc*/, true)         // line 443
    else:                                        // src == dst — local delivery, no hops
        egress.SetUnicastTerminal(dst, false)    // @0x1ffe0040, line 391
        nexthop.SetUnicastTerminal(dst, false)   //              line 394
        nexthop.SetUnicastVcControl(dst, 1, true) // @0x1ffe0320, line 396
    return OK
```

> **[CONFIRMED]** Both coordinate fetches use topology vtable slot `+136` (`+0x88`, `(**((qword**)this+4) + 136)`), at `@0x1fbd566d` (src, AddSourceLocation line 371) and the second at line 373. `GetEgressTable` (line 375) and `GetNextHopTable(src, …)` (line 377) follow. The path-source branch tests `*((_BYTE*)this + 297)` (`gen+0x129`, line 120 of the decompile): true → `RouteTargetCache::GetPath(cache, src, dst)`; false → `GetStaticPath(this, src_coord)`. The hop guard reads `*(_DWORD*)v51` (the path's `num_hops`); the multi-hop arm calls `HopDirection`, `LinkMap::GetLink`, `SetUnicastTarget`, then `PopulateRoutingTable`; the no-hop arm (`LABEL_17`) calls `SetUnicastTerminal` twice + `SetUnicastVcControl`.

> **GOTCHA —** the **first hop is the source's own egress**, written directly here, not by `PopulateRoutingTable`. `PopulateRoutingTable` is then called with `hop = 0` to walk the path and fill the *intermediate* chips' forwarding entries. A reimplementation that lets the per-hop fan-out also write the source egress will double-write the egress row (the `SetUnicastTarget` `overwrite=false` arg makes the second write a no-op, but the link-byte computation differs between the two paths).

> **QUIRK —** the empty-path case is exactly `src == dst`. `GetStaticPath`/`GetPath` returns a path with `num_hops == 0`, and the entry build writes a **terminal** marker into both the egress and the next-hop table so a packet that arrives at its own destination is delivered locally rather than forwarded. The `SetUnicastVcControl(dst, 1, true)` on the terminal entry assigns the default VC.

### 3.4 The path-source dispatch

The `use_cache_` flag (`gen+0x129`) chooses between two `IciRoutePath` providers that return the same shape:

| `use_cache_` | Provider | Cost | Source |
|--------------|----------|------|--------|
| `1` | `RouteTargetCache::GetPath(src, dst)` `@0x1fbd42c0` | O(1) array index | precomputed in `PopulatePathCache` |
| `0` | `GetStaticPath(src_coord, dst_coord)` `@0x1fbdbd00` | computed now | live DOR/twist construction |

`GetPath` indexes a 2-D array: `idx = GetTableIndex(src) · numdst + dst`, `IciRoutePath` stride `0x50` (`lea rax,[rax+rax*4]; shl 4`), base pointer at `cache+0x0`, `numdst` read indirectly via the `cache+0x48` field (`*(int*)([cache+0x48]+0x40)`). See **[Static-Path Generation](get-static-path.md)** for the live generator's internals.

> **[CONFIRMED]** `RouteTargetCache::GetPath @0x1fbd42c0` computes `idx = TableIndex(src)·numdst + dst` with `numdst = *(_DWORD*)([cache+0x48]+0x40)` (`mov 0x48(%rdi),%rsi; imul 0x40(%rsi),%edx` at `@0x1fbd42f4`) and returns `*(qword*)cache + 0x50·idx` (`lea (rax,rax,4); shl 4` at `@0x1fbd4304`). The destination count lives one indirection deep through the `cache+0x48` pointer, not at `cache+0x40` directly; the `cache+0x8` field is the path-array element count used for the bounds check. `GetTableIndex(src)` is applied only on the non-fast-path branch (when `*(_BYTE*)([cache+0x48]+24) == 0`).

---

## 4. The per-hop action — `PopulateRoutingTable` → `GetNextHopAction`

### 4.1 Purpose

Given the path and a hop index, compute that hop's `{next_chip, output_link, vc}` action and write it into the correct forwarding `RoutingTable` row. `PopulateRoutingTable` is the table-selecting wrapper; `GetNextHopAction` is the topology decoder.

### 4.2 `GetNextHopAction`

```c
function GetNextHopAction(src_coord, dst_coord, IciRoutePath& path, int hop):  // @0x1fbda6a0
    dir        = path.HopDirection(hop)              // @0x20c01900 — proto Direction of this hop
    next_coord = topology->Walk(src_coord, dir)      // vtable +0xa0, @0x1fbda702
    next_chip  = topology->GetId(next_coord)         // vtable +0x90, @0x1fbda74b
    if next_coord == dst_coord:                      // Coordinates::operator== @0x20c0bac0
        if hop == path.num_hops - 1:                 // [path+0x20]-1
            output_link = 0xff                       // TERMINAL — next chip IS the destination
        else: ...                                    // (defensive; reached-dst-but-more-hops)
    else:                                            // intermediate — next chip must forward on
        next_dir = path.RemainDirectionHops(hop)     // @0x20c01ba0 — its outgoing direction
        output_link = LinkMap::GetLink(next_chip, next_dir)   // @0x1ffe3940 — its egress link byte
    // VC selection — deadlock-free torus VC allocation (rule not fully reduced)
    vc = vc_select( CrossesDateline(src,dst),        // @0x1fbdb120
                    Direction::IsSame(dir,next_dir),  // @0x20c025e0
                    GetVcBalanceUsage() )             // @0x1fbdb4c0  -> vc in {0,1,2}
    return { next_chip @+8, output_link @+0xc, vc @+0x10 }
```

> **[CONFIRMED]** `GetNextHopAction @0x1fbda6a0`: `HopDirection` (`@0x1fbda6d4`), `Walk` via vtable `+0xa0` (`@0x1fbda702`), `GetId` via vtable `+0x90` (`@0x1fbda74b`), `Coordinates::operator==` vs `dst_coord` (`@0x1fbda773`), last-hop terminal `0xff` (`@0x1fbda79c`), `RemainDirectionHops(hop)` (the raw hop index, `@0x1fbda7bb`), `LinkMap::GetLink` (`@0x1fbda863`). The VC inputs `CrossesDateline` (`@0x1fbda8b5`), `Direction::IsSame` (`@0x1fbda8fd`), `GetVcBalanceUsage` (`@0x1fbda921`) feed `vc ∈ {0,1,2}`. The result struct packs `{next_chip(int32)@+8, output_link(int8)@+0xc, vc(int32)@+0x10}` (`@0x1fbdaa6a`).

### 4.3 `PopulateRoutingTable`

```c
function PopulateRoutingTable(src_coord, dst_coord, src_chip, via_chip, path, hop):  // @0x1fbdb5c0
    act = GetNextHopAction(src_coord, dst_coord, path, hop)
    if gen[+0x1a] (nexthop-enable):
        idx = GetTableIndex(via_chip)
        table = gen[+0xc0 egress-next | +0xd8 link-next][idx]   // by egress bool
    else:                                                       // egress branch
        in_link = LinkMap::GetLink(via_chip, Direction::Opposite(hopdir))  // @0x20c02600 / @0x1ffe3940
        table   = GetLinkHopTable(via_chip, in_link)            // @0x1fbdbbe0
    entry = table.GetRoutingEntry(dst)                          // @0x1ffdf740
    if act.output_link == 0xff: entry.SetUnicastTerminal(dst, false)
    elif act.output_link != 0xfe: entry.SetUnicastTarget(dst, act.output_link, false)
    entry.SetUnicastVcControl(dst, act.vc, true)
    return (act.output_link != 0xfe)                            // "wrote a target"
```

> **[CONFIRMED]** `PopulateRoutingTable @0x1fbdb5c0`: `GetNextHopAction` (`@0x1fbdb5f6`), next-hop table select `gen+0xc0`/`+0xd8` gated by `gen+0x1a` (`@0x1fbdb659`), the egress branch via `Direction::Opposite` (`@0x1fbdb6fb`) + `LinkMap::GetLink` (`@0x1fbdb773`) + `GetLinkHopTable` (`@0x1fbdb797`), `GetRoutingEntry` (`@0x1fbdb85c`), `SetUnicastTarget`/`SetUnicastTerminal`/`SetUnicastVcControl` (`@0x1fbdb889`/`@0x1fbdb8b4`/`@0x1fbdb8cd`), and the `bl = (link != 0xfe)` return (`@0x1fbdb876`).

> **NOTE —** the VC-assignment rule from `{CrossesDateline, Direction::IsSame, GetVcBalanceUsage}` is a 3-way priority cascade, byte-confirmed in `GetNextHopAction`'s `r12d` immediates (`mov $0x1` at the `$_2`/"Turned" site `0x1fbda9d1`, `mov $0x2` at the `$_3`/"Crossed a dateline" site `0x1fbda950` and the `$_4`/"VC load balancing" site `0x1fbda9fd`): a **turn** (`!IsSame`) forces **VC1**; a straight hop that **crosses a dateline** forces **VC2**; a straight hop where **balance** fires forces **VC2**; a plain straight hop stays on the default **VC0**. So VC2 (the *high* VC) is the deadlock-break / balance VC and VC1 is the turn VC — see [VC-Balance Allocation](../ici/vc-balance-allocation.md) for the full cascade and the `CreateVcBalanceThreshold @0x1fbd8320` threshold math.

---

## 5. The table layout this layer writes

### 5.1 `GetTableIndex` — chip → dense row

`GetTableIndex(chip_id) @0x1fbdd000` maps a (possibly sparse) physical chip id to a dense `0 .. numsrcs-1` row index used to address every per-source table. It is a crc32-seeded absl swiss-table (`gen+0x108` size mask, `gen+0x118` ctrl bytes, `gen+0x120` slot array), with a linear fast path for slices under `0x20000` chips. This compaction is why a reimplementation cannot index the tables by raw chip id.

> **[CONFIRMED]** `GetTableIndex @0x1fbdd000`: crc32 of chip (`@0x1fbdd05c`), swiss-table fields `gen+0x108`/`+0x118`/`+0x120`, the `< 0x20000` fast path (`@0x1fbdd012`).

### 5.2 The three RoutingTable arrays

Emission writes three parallel arrays of `superpod::routing::RoutingTable`, each row stride `0x48`:

| Array | Base / count | Role | Indexed by |
|-------|--------------|------|-----------|
| egress | `gen+0xa8` / `gen+0xb0` | source's **first-hop** output link | `TableIndex(src)` |
| egress-next-hop | `gen+0xc0` / `gen+0xc8` | per-chip **forwarding** (compacted) | `TableIndex(via_chip)` |
| link-next-hop | `gen+0xd8` / `gen+0xe0` | per-(chip, in-link) forwarding | `TableIndex(via_chip)` + in-link |

`GetEgressTable @0x1fbdc040` returns `&(gen+0xa8)[TableIndex(src)]`; `GetNextHopTable @0x1fbdbb00` picks egress-next (`gen+0xc0`, `egress=true`) or link-next (`gen+0xd8`, `egress=false`), gated by `gen+0x1a`; `GetLinkHopTable @0x1fbdbbe0` is the per-(chip, incoming-link) table for the non-compacted egress branch. Each row is a `RoutingTable` whose `RoutingEntry`s are indexed by destination and carry `{unicast_target (output_link), unicast_terminal, vc_control}`.

### 5.3 The RouteTargetCache

When `use_cache_`, `RouteTargetCache` (at `gen+0x130`) holds a **2-D path array** `[dense_src_row][dst]` (`IciRoutePath` stride `0x50`, destination count at `cache+0x40`) plus parallel **per-link next-hop byte tables** read in the cached fast path. `PopulatePathCache @0x1fbd4360` fills the path array (one fiber per source, each running `GetStaticPath`); `PopulateLinkNextCache @0x1fbd4680` fills the byte tables read at `CreateSrcDest` line 428 / `PopulateRoutingTable`.

### 5.4 Entry sentinels

| `output_link` byte | Setter | Meaning |
|--------------------|--------|---------|
| `0..N` | `SetUnicastTarget(dst, link, false)` `@0x1ffdfce0` | physical SerDes output link of this hop |
| `0xfe` | (skip) | entry **unset** sentinel — `SetUnicastTarget` no-ops if already set (`@0x1ffdfd1e`) |
| `0xff` | `SetUnicastTerminal(dst, false)` `@0x1ffe0040` | **terminal** — this chip is the destination |
| — | `SetUnicastVcControl(dst, vc, true)` `@0x1ffe0320` | `vc ∈ {0,1,2}` deadlock/balance control |

> **[CONFIRMED]** The `0xfe` skip-if-set is in `SetUnicastTarget @0x1ffdfce0` (`@0x1ffdfd1e`); `0xff` is the terminal marker emitted by `GetNextHopAction`'s last-hop arm and routed to `SetUnicastTerminal`. The setters are the `superpod::routing::RoutingTable` `PerLinksRoutingTable` writers whose `output_link` byte is the `0..3` SerDes link index of the runtime's `LinkNextHopRoutingTablesEntry`.

> **GOTCHA —** `0xfe` and `0xff` are **distinct sentinels in the same byte field**: `0xfe` means "not yet written, leave it" (idempotent fill), `0xff` means "deliver here". A reimplementation that uses a single out-of-band value, or that treats `0xff` as just another link index, will mis-route either the unset rows or the destination rows.

---

## 6. Function map

| Function | Address | Role | Confidence |
|----------|---------|------|------------|
| `ParallelRoutingTableGenerator::CreateUnicastRoutingTables` | `0x1fbd5340` | fibered 2-D sweep driver | CERTAIN |
| `RoutingTableGenerator::CreateUnicastRoutingTables` | `0x1fbd94a0` | serial sweep twin | CERTAIN |
| `$_0` outer fiber lambda | `0x1fbd70c0` | one Bundle task per source | HIGH |
| inner dst lambda | `0x1fbd7240` | dst loop + `SetChannelMerges` + first-error | CERTAIN |
| `ParallelRoutingTableGenerator::CreateSrcDestUnicastRoutingTable` | `0x1fbd5640` | per-`(src,dst)` entry build | CERTAIN |
| `RoutingTableGenerator::WalkCreateSrcDestUnicastRoutingTable` | `0x1fbd9580` | serial per-`(src,dst)` twin (live path only) | HIGH |
| `RoutingTableGenerator::PopulateRoutingTable` | `0x1fbdb5c0` | per-hop table-select + write | CERTAIN |
| `RoutingTableGenerator::GetNextHopAction` | `0x1fbda6a0` | hop → `{next_chip, output_link, vc}` | CERTAIN |
| `GetTableIndex` | `0x1fbdd000` | chip_id → dense row (crc32 swiss) | HIGH |
| `GetEgressTable` | `0x1fbdc040` | `gen+0xa8`[row], stride `0x48` | CERTAIN |
| `GetNextHopTable` | `0x1fbdbb00` | egress-next / link-next select | CERTAIN |
| `GetLinkHopTable` | `0x1fbdbbe0` | per-(chip, in-link) table | HIGH |
| `RouteTargetCache::GetPath` | `0x1fbd42c0` | `[src_row][dst]` path index | CERTAIN |
| `SetChannelMerges` | `0x1fbda1e0` | per-source VC/channel merge | MEDIUM |
| `GetVcBalanceUsage` | `0x1fbdb4c0` | VC balance counter (rule not reduced) | LOW |
| `RoutingTableGeneratorFactory::CreateGenerator` | `0x1fbd3dc0` | strategy → generator class | HIGH |

---

## 7. Diagnostic source-locations

All emitted via `AddSourceLocationImpl` / `CreateStatusAndConditionallyLog` against `parallel_routing_table_generator.cc`:

| Line | Site |
|------|------|
| 371 / 373 | `GetCoordinate(src)` / `GetCoordinate(dst)` error |
| 375 / 377 | `GetEgressTable` / `GetNextHopTable` error |
| 384 | `GetStaticPath` error (live path) |
| 388 | path `num_hops` test |
| 391 / 394 / 396 | terminal arm: egress / nexthop `SetUnicastTerminal`, `SetUnicastVcControl` |
| 401 / 403 / 405 | multi-hop arm: `HopDirection(0)`, `LinkMap::GetLink`, `SetUnicastTarget` |
| 410 | `PopulateRoutingTable` error |
| 417 | `use_cache_` consistency assert (`LogMessageFatal`) |
| 428 / 431 / 434 / 443 | cached nexthop mirror: table fetch, terminal, target, VcControl |

> **[CONFIRMED]** Each line above is the literal `AddSourceLocationImpl(..., N, "...parallel_routing_table_generator.cc")` / `CreateStatusAndConditionallyLog(N, ...)` argument read at its call site in the `@0x1fbd5640` decompile. Line 417's `LogMessageFatal(..., 417, "use_cache_")` names the field.

---

## Cross-References

- **[Routing Overview](overview.md)** — the route-generation → cache → emission pipeline this driver is the emission stage of.
- **[Route-Table Generation](route-table-generation.md)** — the per-`(src,dst)` entry mapper (`DmaDestinationRoutingTableEntryMapper::Map`) and the physical↔logical placement map (`GetPhysicalToLogicalMapping3D`) this layer sits above; the single-index primitive vs the full-table sweep.
- **[Static-Path Generation](get-static-path.md)** — the deterministic `GetStaticPath` provider the non-cached emission consumes per `(src,dst)`.
- **[Create-Routing-Schedule](create-routing-schedule.md)** — the explicit-schedule (CollectivePermute) path, orthogonal to this auto-routing emission.
- **[Net-Router Pipeline](net-router-pipeline.md)** — the downstream consumer of the `{next_chip, output_link, vc}` per-link rows this layer writes.
- **[Collectives Overview](../collectives/overview.md)** — how the replica groups (placed via `GetPhysicalToLogicalMapping3D`) drive the on-pod collectives whose DMAs traverse these tables.
