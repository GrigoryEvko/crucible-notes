# Binomial / Recursive-Doubling

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

The binomial single-phase AllReduce emitter is the **latency-optimal** in-axis collective on the ICI fabric: a **recursive-doubling butterfly** in which every core exchanges-and-reduces with the partner whose ring position differs in bit `k` at step `k`, so after `log₂(N)` symmetric steps every core holds the complete sum. There is no separate all-gather — the butterfly is self-completing. This is the algorithm `BinomialSinglePhaseRingSumEmitter::EmitAllReduceOnSpan` (`0x13769be0`) emits, and it stands opposite the **bandwidth-optimal** ring rotation (`2·(N−1)` steps, see [Hierarchical AllReduce / Pincer](allreduce-hierarchical-pincer.md)) that the [SelectNDStrategy picker](strategy-nd-picker.md) chooses for it.

The page documents three coupled things a reimplementer cannot guess from the op surface alone:

- **Both loop shapes side by side** — the binomial butterfly (`log₂(N)` steps, distance doubles `1,2,4,…`) against the ring's reduce-scatter + all-gather (`N−1` + `N−1` steps, fixed `+1` neighbour, chunk index rotates) — so the latency/bandwidth tradeoff is concrete.
- **The per-step partner math** — `partner = my_pos ± (1<<step)` with a branchless modular-wrap select, byte-identical at *build time* (the replica-table writer) and *emit time* (the per-step loop). Getting the sign-select wrong silently routes a step to the wrong core.
- **The binomial replica table** — `CreateStaticBinomialReplicaInfoTable` (`0x1375efa0`) precomputes the entire per-rank butterfly schedule into an `int32[N×8]` table indexed by `(rank×8 + step)`, materialised as an HLO `Literal` constant (ConstantMapper Type 7) and loaded per-core at emit time so the emitter never recomputes the partner — it walks a `CoreLocationBase[]` counterparts vector. This table is the **structural inverse** of the flat star-barrier table (device→ordinal, 1 column).

The shared reduce primitive (`RingSumEmitter::ReduceShardInPlace`, `0x1375d460`, the bf16-accumulate-in-f32 reducer) and the per-step wire DMA (`EnqueueDmaInGranules` → `DMA_TYPE_REMOTE_WRITE_UNICAST`) are documented at the family level by [the ICI AllReduce primitive](../ici/all-reduce-primitive.md); this page references them rather than re-deriving them, and concentrates on the *partner schedule* — the only thing that distinguishes binomial from ring.

| | |
|---|---|
| **Binomial emitter** | `BinomialSinglePhaseRingSumEmitter::EmitAllReduceOnSpan(bool phase, b)` — `0x13769be0` (414 decompiled lines) |
| **Init / counterparts install** | `BinomialSinglePhaseRingSumEmitter::EmitInitialization` — `0x137699c0` |
| **Viability gate** | `IsBinomialSinglePhaseAllReduceViable` — `0x1375ed80` (`popcnt(N)==1 && (bsr(N)^7)<8`) |
| **Replica-table build** | `CreateStaticBinomialReplicaInfoTable` — `0x1375efa0` (`int32[N×8]`) |
| **Per-row butterfly writer** | `$_2` lambda — `0x1375fb20` (`partner = my_pos ± (1<<step)`) |
| **Device-id resolver** | `$_5` lambda — `0x13761260` (ReplicaGroup ids / DeviceAssignment flatten) |
| **HLO-constant wrapper** | `CreateBinomialReplicaInfoTable` — `0x1375ee40` (`LiteralUtil::CreateR1<int>`) |
| **Per-core table read** | `LoadBinomialReplicaInfoTable` — `0x1375fca0` (`replica_id×8+col`, `LoadInfoTable count=8`) |
| **Recv sflags reserved** | `receive_sync_flags_needed()` — `0x1376a680` (returns `7`) |
| **Ring partner (1-D)** | `UniDirection1DRingStrategy::CwCore` — `0x137d7680` (fixed `+1` CW neighbour) |
| **Ring partner (N-D)** | `BaseStrategyND::TorusStrideNPhasekNeighbor` — `0x137c5960` (`(pos+stride·step) mod N`) |
| **Cap** | `kBinomialCounterpartsCount = 8` ⇒ `N ∈ {2,4,8,16,32,64,128}` |
| **Source TU** | `platforms/xla/service/jellyfish/lowering/ring_sum_emitter.cc` |

---

## The Two Loop Shapes, Side by Side

The five ICI AllReduce families collapse into three step-generation loop shapes. This page covers the two that differ only by their partner schedule; the bidirectional pincer is documented in [Hierarchical AllReduce / Pincer](allreduce-hierarchical-pincer.md). All shapes share `ShardAddress` (flat VMEM offset into the per-color scratch) and `ReduceShardInPlace` (the VPU merge); only the partner walk and the step count differ.

### Binomial — recursive-doubling butterfly (self-completing)

At step `k`, a core exchanges its current accumulator with the core whose ring position differs in bit `k`, then reduces the received shard in place. After `log₂(N)` steps the difference bits are exhausted, so every core has summed every other core's contribution — there is **no separate all-gather**.

```text
emit_binomial_all_reduce(span, b):                       # 0x13769be0
    N = ring_size                                        # power of 2, 2..128
    assert popcount(N) == 1 and (bsr(N) ^ 7) < 8         # IsBinomial...Viable gate
    L = bsr(N)                                           # = log2(N) = number of steps
    my_pos = position_in_ring                            # from the replica table row
    if barrier_scope == kAll:                            # *(this+0x110) == 3
        for cp in counterparts:                          # pre-loop tree barrier
            BarrierCoresStart(cp, GetBarrierSyncFlag(cfg, b), 1<<k, b)
    src = ShardAddress(self, span, b)                    # local accumulator (whole slice)
    for step in 0 .. L-1:
        pcore   = counterparts[step]                     # PRECOMPUTED — table[rank*8+1+step]
        EnqueueDmaInGranules(src, pcore, REMOTE_WRITE_UNICAST, ...)
        recv = DmaDoneInGranules(...).annotate("shard-recv-wait")
        # runtime butterfly (mirrors the build-side $_2 writer, see below)
        dist    = 1 << step                              # DISTANCE DOUBLES: 1,2,4,...
        lo      = my_pos & (0xFFFFFFFFFFFFFFFF >> (63 - step))   # low step+1 bits
        partner = (lo < dist) ? my_pos + dist : my_pos - dist
        ReduceShardInPlace(src, recv, useBf16=is_bf16, b)
    # after L steps every core holds the full reduction (no AG emitted)
```

### Ring — reduce-scatter + all-gather (bandwidth-optimal)

The ring families never compute a per-step partner index on the sender side. The physical link target is the **same `+1` (CW) neighbour every step**; what rotates is the *chunk index* each core forwards. AllReduce = reduce-scatter then all-gather, each `N−1` steps, threaded by a `bool phase` arg.

```text
emit_ring_all_reduce(span, b):
    for color in 0 .. num_colors-1:                      # concurrent rings, one per axis
        N   = ring_size[color]
        pos = ring_position[color]
        # ---- PHASE 0: reduce-scatter (N-1 steps, with reduce) ----
        for i in 0 .. N-2:
            cw = CwCore(step=i, ring=color)              # fixed +1 neighbour @ strategy+0xd8
            EnqueueDmaInGranules(ShardAddress(self, (pos-i)   mod N), cw, ...)
            recv = DmaDoneInGranules(...).annotate("shard-recv-wait")
            ReduceShardInPlace(ShardAddress(self, (pos-i-1) mod N), recv, is_bf16, b)
        # ---- PHASE 1: all-gather (N-1 steps, no reduce) ----
        for i in 0 .. N-2:
            cw = CwCore(step=i, ring=color)
            EnqueueDmaInGranules(ShardAddress(self, (pos+1-i) mod N), cw, ...)
            DmaDoneInGranules(...).annotate("shard-send-wait")
            SafeMemcopyN(recv -> ShardAddress(self, ...))   # plain copy, no merge
```

> **NOTE —** `EmitAllReduceOnSpan` takes a `bool phase` first argument (`esi`/`r12d`). The name "single-phase" refers to the *butterfly being one fused traversal*, not to a single phase argument; the same emitter also contains the ring-style all-gather `SafeMemcopyN` path (`0x13769be0` lines 313/393) for the non-butterfly span case. The decompile shows both `"shard-recv-wait"` (reduce side) and `"shard-send-wait"` (gather side) annotation strings emitted from the same function.

### The tradeoff

| | Binomial butterfly | Unidirectional ring |
|---|---|---|
| Steps per AllReduce | `log₂(N)` | `2·(N−1)` (RS `N−1` + AG `N−1`) |
| Bytes per link | redundant (full slice each step) | `(N−1)/N` of the data (optimal) |
| Latency | optimal (`log₂(N)` round-trips) | linear in `N` |
| Restriction | `N` power of 2, `N ≤ 128` | any `N` |
| Self-completing? | yes (no AG) | no (explicit AG phase) |

The binomial path is latency-bound — few steps, each carrying a redundant copy — and the ring is bandwidth-bound. The picker selects binomial only for small power-of-2 rings where round-trip latency dominates; large or non-power-of-2 axes fall back to the ring or pincer. See [SelectNDStrategy](strategy-nd-picker.md) and [SPMD link-count cost](spmd-link-count-cost.md) for the decision.

---

## The Viability Gate

`IsBinomialSinglePhaseAllReduceViable` (`0x1375ed80`) decides whether the binomial path is even legal for a given ring size and compilation environment. The same predicate is inlined into the table-build prologue (`0x1375efa0`), so the build and the dispatch agree by construction.

```c
// IsBinomialSinglePhaseAllReduceViable — 0x1375ed80 (decompiled, simplified)
bool viable(target, long N, bool use_global_device_ids, DeviceAssignment* da, env):
    // env-flag gate: both binomial-AR enables must be set (or a vtable override)
    if (env[0xf48] != 1 || env[0x10c8] != 1 || ((N >= 2 && (N & 1)) | target.vtable[0x18]()))
        return false;                                    // NB: the (N&1) term is a deopt path
    if (!use_global_device_ids) {
        if (N < 2) return false;
        goto check_pow2;
    }
    // cross-module / global-device-id path
    if (env[0x1006] != 1) return false;
    if (da && env[0xe1e] == 0 && N >= 2 && env[0xfa3])   // use_physical_core_ids must be 0
        goto check_pow2;
    return false;
check_pow2:
    if (popcnt(N) != 1) return false;                    // N is a power of 2
    return (bsr(N) ^ 7) < 8;                              // log2(N) <= 7  ⇒  N <= 128
```

`popcnt(N) == 1` forces `N` to be a power of two; `(bsr(N) ^ 7) < 8` is the clz-style cap — `bsr(N)` is `log₂(N)`, and the XOR-with-7 then `< 8` test rejects `log₂(N) > 7`, i.e. `N > 128`. So the binomial ring is restricted to **`N ∈ {2, 4, 8, 16, 32, 64, 128}`**. The cap is `kBinomialCounterpartsCount = 8` (the 8 columns of the replica table = self + up to 7 butterfly partners), which is why `receive_sync_flags_needed()` returns exactly `7` (`0x1376a680`).

| Env field (offset) | Attributed name (from use) | Role |
|---|---|---|
| `0xf48` | `xla_jf_*binomial_all_reduce` enable | must be `1` |
| `0x10c8` | second binomial-AR enable | must be `1` |
| `0x1006` | cross-module binomial enable | must be `1` if global ids |
| `0xe1e` | `use_physical_core_ids` | must be `0` for global-id path |
| `0xfa3` | cross-module gate | must be non-zero |

> **NOTE —** the offsets are byte-confirmed (identical in `IsBinomial...Viable` and the table-build prologue). The flag *names* (`xla_jf_*` / `xla_tpu_binomial_all_reduce_*`) are attributed from how the bytes are used, not from a recovered proto descriptor — treat them as LOW-confidence labels on Confirmed offsets.

---

## The Per-Step Partner Math

The butterfly partner is `partner = my_pos ± (1<<step)`, with the sign chosen so the partner stays in `[0, N)`. The select is **branchless**: test whether the low `step+1` bits of `my_pos` are below the step distance; if so add, else subtract. The same arithmetic appears twice — once at build time (the table writer) and once at emit time (the loop) — and they are byte-for-byte identical, which is the proof that the precomputed table and the runtime walk agree.

### Build-side: the `$_2` per-row writer (`0x1375fb20`)

```c
// CreateStaticBinomialReplicaInfoTable::$_2 — 0x1375fb20 (decompiled)
my_pos = use_partition ? (N * partition_count + rel) : rel;   // 1D: rel == member index
table[write_pos + 0] = my_pos;                                // COLUMN 0 = self
step = 0;
while (step < log2N) {                                        // *result == log2(N)
    v15 = 1 << step; step++;                                  // dist = 1<<step ; step now = k+1
    v16 = (int64)v15;
    v17 = -v16;
    if ((my_pos & ~(-1 << step)) < v16)                       // low k+1 bits (step pre-incremented) below dist?
        v17 = v16;                                            //   -> add
    partner = my_pos + v17;                                   // my_pos ± dist
    CHECK(partner >= 0);                                      // ring_sum_emitter.cc:1810
    CHECK(partner <  N);                                      // ring_sum_emitter.cc:1811
    table[write_pos + step] = $_5(partner);                  // resolve to global device-id
}
```

The two FATAL CHECKs carry the literal string `"counterpart_ordinal >= 0"` (line 1810) and `"counterpart_ordinal < num_participants_per_group"` (line 1811), confirming the `[0, N)` wrap bound.

### Emit-side: the loop body (`0x13769be0`)

```c
// EmitAllReduceOnSpan step loop — 0x13769be0 (decompiled, lines 351-360)
v27 = 1LL << v105++;                                          // dist = 1<<step ; step++
mask    = SimmU32(0xFFFFFFFFFFFFFFFF >> v47);                 // step-bit mask
lo      = SandU32(my_pos, mask);                              // low bits of my_pos
cond    = SltS32(lo, SimmS32(dist));                          // lo < dist ?
up      = SaddS32(my_pos, SimmS32(dist));                     // my_pos + dist
down    = SsubS32(my_pos, SimmS32(dist));                     // my_pos - dist
partner = Sselect(cond, up, down);                            // pick in-range counterpart
addr    = ShardAddress(self, span, partner, b);
ReduceShardInPlace(local, recv, useBf16, b);
```

> **NOTE —** the emit-side select is `SandU32`/`SltS32`/`SaddS32`/`SsubS32`/`Sselect` (scalar VPU ops emitted into the program); the build-side select is a host-side `cmovl`. They compute the same function — the emit-side form exists because the binomial path *can* also recompute the partner from the live `position_in_ring` register, while the table path precomputes it. In the binomial datapath the table wins: `EmitInitialization` installs the resolved `CoreLocationBase` vector and the loop reads `counterparts[step]` (a 24-byte-stride record) for the *physical* core, while this arithmetic resolves the *logical* position used for the shard address.

The N-D ring uses a different partner computation entirely — modular ring rotation, not a butterfly:

```c
// BaseStrategyND::TorusStrideNPhasekNeighbor — 0x137c5960
color   = dim_to_color[axis];                                // *(this+0xd0+axis*0x18)
ring_pos= ring_position[color];                              // *(this+0x160+color*8)
offset  = stride * step;
partner = ModuloRingSize(ring_pos + offset, ring_size[color]);   // 0x137c61a0
```

`ModuloRingSize` (`0x137c61a0`) is a branchless single-step wrap valid on `[−N, 2N)`: `if (x<0) x+=N; if (x>=N) x-=N`. The mesh variant (`MeshStrideNPhasekNeighbor`, `0x137c5cc0`) replaces the wrap with a clamp/reflect because a mesh has no wrap edge — the only structural difference between the torus and mesh ring families.

---

## The Binomial Replica Table

The butterfly schedule is not recomputed per emit; it is precomputed once per program into a constant. `CreateStaticBinomialReplicaInfoTable` (`0x1375efa0`) builds an `int32[N × 8]` array indexed by `(rank × 8 + step)`. Column 0 of each rank's row is the rank's own logical position; columns `1 .. log₂(N)` are that rank's butterfly partners' **global device-ids** at each step (the `$_2` writer above fills the row, the `$_5` resolver maps `partner_pos → device-id`).

```text
    column:   0        1            2            3        ...   7
            +--------+------------+------------+--------+   +--------+
 rank 0     | self=0 | partner@±1 | partner@±2 | ...    |   | unused |
 rank 1     | self=1 | partner@±1 | partner@±2 | ...    |   | unused |
   ...      |        |            |            |        |   |        |
 rank N-1   | self   | partner@±1 | partner@±2 | ...    |   | unused |
            +--------+------------+------------+--------+   +--------+
            8 columns per rank (kBinomialCounterpartsCount); rows 1..log2(N) used
```

The table is the **structural inverse** of the flat star-barrier table: the barrier table is `int32[device]` storing the within-group *ordinal* (one column, membership only); the binomial table is `int32[rank × 8 + step]` storing the *partner device-id* (8 columns, the precomputed butterfly schedule). They live in different translation units, are keyed in different constant registries, and feed different actuators.

| Aspect | Flat barrier table | Binomial table |
|---|---|---|
| Builder | `CreateStaticReplicaInfoTable` (`net_util.cc`) | `CreateStaticBinomialReplicaInfoTable` `0x1375efa0` (`ring_sum_emitter.cc`) |
| Width | `N` entries (1 col) | `N × 8` entries (8 cols) |
| Index | flattened device_id | `rank × 8 + step` |
| Stored value | within-group ordinal `k` | partner device-id at step (col0 = self) |
| Partner math | none (membership) | `my_pos ± (1<<step)`, branchless wrap → `$_5` |
| Viability gate | none | `popcnt(N)==1 && (bsr(N)^7)<8` |
| Constant carrier | ProgramSharedRegistry closures | ConstantMapper **Type 7** → `xla::Literal` R1 int |
| Read at runtime | `GetReplicaGroupCoreInfo` | `LoadBinomialReplicaInfoTable` |
| Actuator | flat star barrier | binomial butterfly emitter |

### Build → materialise → register

1. **Build** — `CreateStaticBinomialReplicaInfoTable` (`0x1375efa0`) allocates `N×8` `int32` (`_Znwm(N<<5)` = `N×32` bytes), memsets 0, runs the per-group/per-member `$_2` writer.
2. **Materialise** — `CreateBinomialReplicaInfoTable` (`0x1375ee40`) wraps the array into an `xla::Literal` R1 int via `LiteralUtil::CreateR1<int>` and frees the scratch.
3. **Register** — `AllReduceEmitter::GenerateConstants` (`0x1373cb60`) calls `ConstantMapper::AddConstant(Type=7, …)`. Type 7 of the jellyfish ConstantMapper enum *is* the binomial replica table.

### Per-core read (`LoadBinomialReplicaInfoTable`, `0x1375fca0`)

```c
// LoadBinomialReplicaInfoTable — 0x1375fca0 (decompiled, simplified)
replica_id = net_util::GetReplicaId(b);                      // current core's replica id (SMEM)
index = SaddS32(SmulU32(replica_id, SimmS32(stride=8)), col_offset);   // rank*8 + col
LoadInfoTable(b, index, /*count=*/8, table);                 // load this rank's 8-entry row
for (step = 1; step < 8; ++step)
    counterparts[step] = FromGlobalCoreId(loaded[step]);     // -> CoreLocationBase[] (0x18 stride)
return BinomialGroupData{ shard_span, counterparts };
```

Each core reads **only its own row** (offset `replica_id × 8`) and recovers its 8-column butterfly schedule as a `CoreLocationBase[]` vector. `EmitInitialization` (`0x137699c0`) stores that vector on the emitter (`{ptr, count, cap}` triple, 24-byte-stride records) and the step loop indexes `counterparts[step]` directly — no emit-time butterfly recomputation of the physical core. The decompile confirms `LoadInfoTable` is called with the literal count `8` (`0x1375fca0` line 128/242) and the resolver loop bounds at `step < 8`.

> **NOTE —** the cross-module path is gated: if `module()->[0xfa3]` (cross-module) is set and `module()->[0xe1e]` (`use_physical_core_ids`) is *not*, the loader FATALs with `"!is_cross_module || use_physical_ids"` (`ring_sum_emitter.cc:1875`) — cross-module binomial rings require physical core ids. The `$_5` device-id resolver (`0x13761260`) handles both the 1-D `ReplicaGroup.replica_ids[idx]` case and the 2-D `DeviceAssignment` multi-dimensional flatten (an `idiv`/`div` split by `partition_count` then a polynomial flatten); the exact 2-D dim ordering of that flatten is the one residual not pinned (LOW).

---

## Reduction and Wire — Shared With the Family

Every butterfly step ends in the same VPU merge and the same wire DMA as the ring and pincer families:

- **`RingSumEmitter::ShardAddress`** (`0x1375d3a0`) — `CHECK(span.memory_space == VMEM)` then `span_base + shard_index × ChunkBytes`. The shard index is the butterfly bit on the binomial path, the rotating chunk on the ring path.
- **`RingSumEmitter::ReduceShardInPlace`** (`0x1375d460`) — the per-element merge closure `fmerge_` (vtable+0xd0). When the dtype is BF16 *and* `NotUseBf16ArithInReduction` holds (TPU gen < 4 or the excess-precision env flag), it does `VunpackF32` → `fmerge_` ×2 → `VpackBf16` (bf16-accumulate-in-f32); otherwise it runs `fmerge_` natively.
- **`EnqueueDmaInGranules`** → `DMA_TYPE_REMOTE_WRITE_UNICAST`, followed by `DmaDoneInGranules` annotated `"shard-recv-wait"`; the binomial emitter reserves **7 receive sync flags** (`receive_sync_flags_needed()=7`, `0x1376a680`).

The remote-sflag write address each step targets is computed by `EncodeRemoteSyncFlagAddress` (`0x1d54da40`): it validates the operand is in `MemorySpace::kSflag`, maps the peer's logical chip-id to physical (`MapLogicalToPhysicalChipId`, `0x1d519f40`), then dispatches by `TpuVersion` (the `Target+0x398` gen field) through a `FunctionRegistry<TpuVersion,…>` to the per-codename encoder. The Jellyfish/Dragonfish encoder `JfDf` (`0x1d5aa620`) is byte-confirmed:

```text
addr =  sflag
      | (peer.chip_x << 0x14)               // chip X coord, bit 20
      | (phys_chip_id << 0x15)              // physical chip, bit 21
      | 0x40000                             // bit 18 fixed remote marker
      | (DefaultSyncFlagSegmentId() << 0xc) // 0x40 << 12 = segment, bits 12..17
      | (multicast ? 0x80000 : 0)           // bit 19 multicast/atomic target
```

The full handshake, descriptor format, and the per-gen encoder table are documented at [the ICI AllReduce primitive](../ici/all-reduce-primitive.md) and [routing overview](../routing/overview.md); this page only notes that the binomial path uses the identical primitives — the schedule is the only difference.

> **NOTE —** the pincer families consume a *separate* set of reserved AllReduce sflag slots (`GetAllReduceSyncFlagNumber`) and a flat `net_util::GetRingLocation` schedule; a full `.text` cross-reference shows the only callers of `GetAllReduceSyncFlagNumber` are `AsyncPincerInstance::InitSflags` and `RotatedPincer*::InitSyncFlags` — **none** in the binomial emitter. The binomial table is read *only* through the `BinomialGroupData` provider into the binomial emitter. This is the three-topology split: binomial butterfly (table + 7 recv sflags), ring rotation (`GetRingLocation`, no table), pincer (`GetRingLocation` + reserved slots).

---

## Where the Pincer Fusion Sits Relative to the Butterfly

The AllReduce pincer-fusion (the reduce-arm / broadcast-arm fusion) is a *different* emitter from the binomial butterfly, and the distinction matters for a reimplementer choosing where to fuse surrounding compute. The binomial butterfly is **self-completing**: there is no separable reduce-scatter arm to fuse a producer into, nor a separable all-gather arm to fuse a consumer into — every step both sends and reduces, and after `log₂(N)` steps the result is already broadcast to all cores. The pincer fusion, by contrast, exposes the reduce-scatter and all-gather halves as distinct fusable arms.

| | Binomial butterfly | Pincer fusion |
|---|---|---|
| Emitter | `BinomialSinglePhaseRingSumEmitter` `0x13769be0` | `RotatedPincerFusionEmitter` (`EmitAllReduceScatterFusion`, `EmitAllGatherFusion`) |
| Reduce arm / broadcast arm | fused into one traversal (not separable) | separable — RS arm and AG arm exposed for windowing |
| Schedule source | binomial replica table (`Type 7`) | `net_util::GetRingLocation` |
| Sflags | 7 general recv sflags | reserved AllReduce slots (`GetAllReduceSyncFlagNumber`) |
| Directions / step | one (butterfly partner) | two (rotated CW + counter-rotated CCW) |
| Fits when | small power-of-2 ring, latency-bound | large ring, bandwidth-bound, windowed-einsum overlap |

The pincer runs the ring rotation of the bandwidth-optimal family in *both* directions per step (each direction covering half the ring, `⌈N/2⌉` steps per direction) with overlapped send/receive windows, and its per-step bookkeeping is a 2-D `[dim][color]` sflag table — the smoking gun for bidirectionality, since it keeps separate flags for the rotated and counter-rotated shards. The reduce-arm/broadcast-arm split is what lets surrounding matmul tiles be windowed *into* the collective (windowed-einsum). The full pincer loop and its fusion arms are documented in [Hierarchical AllReduce / Pincer](allreduce-hierarchical-pincer.md); the megacore split that shares each arm across the two TC cores is in [Megacore Fusion](megacore-fusion.md).

> **NOTE —** the picker's choice between these is not a quality knob — it is a correctness-and-performance fork. Binomial is *only* legal for power-of-2 `N ≤ 128` (the viability gate); for every other shape, and for any case where bandwidth dominates latency, the picker emits ring or pincer. A reimplementer that fuses a producer into a "binomial reduce-scatter arm" has misread the topology: that arm does not exist — the butterfly has no separable scatter phase.

---

## Reimplementation Checklist

- Reject the binomial path unless `popcnt(N)==1 && bsr(N) ≤ 7` (`N ∈ {2..128}`) and the env-flag gate passes.
- Precompute the per-rank schedule into `int32[N×8]`, `table[rank×8+0]=rank`, `table[rank×8+1+step]=device_id(my_pos ± (1<<step))` with `0 ≤ partner < N` enforced.
- At runtime, read `table[replica_id×8 .. +8)`, resolve each entry through `FromGlobalCoreId` into a `CoreLocationBase[]` vector, walk `step ∈ [0, log₂(N))`.
- Per step: `EnqueueDma(REMOTE_WRITE_UNICAST)` to `counterparts[step]`, wait `shard-recv-wait`, then `ReduceShardInPlace` (bf16-in-f32 on gen < 4). No all-gather phase — the butterfly self-completes.
- Reserve 7 receive sync flags; emit the optional pre-loop tree barrier when the barrier scope is `kAll`.
- The runtime logical-position select (`SandU32`/`SltS32`/`Sselect`) must equal the build-time `cmovl` select bit-for-bit, or the shard address and the physical partner disagree.

---

## Cross-References

- [Collectives Overview](overview.md) — the strategy picker and the family taxonomy.
- [SelectNDStrategy](strategy-nd-picker.md) — the picker that chooses binomial vs. ring vs. pincer (and degraded-axis handling).
- [SPMD Link-Count Cost](spmd-link-count-cost.md) — the latency-vs-bandwidth cost the picker minimises.
- [Hierarchical AllReduce / Pincer](allreduce-hierarchical-pincer.md) — the bidirectional pincer loop shape (the third step-generation form).
- [Megacore Fusion](megacore-fusion.md) — how the reduce-arm / broadcast-arm fuse across the two TC cores.
- [ICI All-Reduce Primitive](../ici/all-reduce-primitive.md) — the shared step-generation primitive: `ReduceShardInPlace`, `EnqueueDmaInGranules`, the sflag handshake.
- [Routing Overview](../routing/overview.md) — how a peer `CoreLocationBase` becomes an ICI-routable remote address.
