# Lowering On-Chip Collectives — `lower_local_collectives` / `insert_ptcom_flat`

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core with a fixed per-build address delta — re-confirm any raw address against the target wheel). The two passes live in `neuronxcc/starfish/lib/libwalrus.so` (build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`, stripped; all names resolve from `.dynsym`, there is no `.symtab`). For `.text` (`0x62d660+`) and `.rodata` (`0x1c72000+`) the virtual address equals the file offset; `.data` carries a `+0x400000` delta. The IR-side handles (`InstGPSIMDSB2SB::isCompatible`, `getEffectiveBytesPerPartition`, the `MaxBytesPerPartition` ceiling) live in `libBIR.so` (build-id `a9b1ea38…`). The pre-dumped `.c` sidecars for these two passes are PLT re-export thunks (`_0x5e9xxx`); the real bodies disassemble from `.text` at `0x1615xxx`/`0x16axxx` — the two-VA-frame artifact. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

A backend collective — an `AllReduce`, a `SendRecv`, a `ReduceScatter` — arrives at the
libwalrus backend as a single opaque opcode: `bir::InstCollectiveCompute`, InstructionType
**48**, with a `CollectiveKind` sub-field that multiplexes all eleven collective variants
through one instruction. By the time the backend pipeline reaches order 80, two earlier
multi-core passes have already run: `lnc_splitter` (order 8) cloned the symbolic module into
one `bir::Module` **per physical core** (`NeuronCoreId` 0..`lnc`-1), and `ExpandReplication`
(order 7) made the SPMD replication explicit. `lower_local_collectives` is the pass that takes
that vector of per-core modules and decomposes the *on-chip* collectives — the ones whose data
movement stays inside the LNC group of cores on a single die (tensor-parallel / FSDP traffic
within a node) — into concrete engine ops: GPSIMD cross-core SBUF→SBUF copies, core-to-core
DMA, `CoreBarrier` fences. The *cross-node* collectives (ICI / EFA fabric) it leaves untouched.

The headline is a single unsigned compare. `lower_local_collectives` dispatches on
`CollectiveKind`: kinds **0/1/2** (`SendRecv` / `SendRecvCCE` / `AllReduce`) get a real local
lowering; kind **≥ 3** (the `ReduceScatter` / `AllGather` / `AllToAll` / `Permute` family) is
fence-only — the op is left intact for a downstream / runtime path. Inside the kind-0
`SendRecv` lowering sits a second, finer choice: each swap is carried either by the GPSIMD
engine's SB2SB copy or by DMA, selected by a byte-budget gate. Its sibling pass
`insert_ptcom_flat` then drops point-to-point completion tokens (`InstTensorCompletion`,
"PTCOM") so a consumer core can wait on a single producer write rather than on a whole barrier.

See also [GPSIMD Engine — the Pool-Alias Cross-Core SB2SB Mover](../arch/gpsimd-engine.md)
for the `InstGPSIMDSB2SB` (IT 33) mechanism and the libBIR `isCompatible` predicate this pass
gates on, and [Collective / GPSIMD / CustomOp Encoding](../isa/collective-customop-encoding.md)
for the `bir::CollectiveKind` ordinals and the IT-48 wire encoding. The cross-*node* collective
lowering (the kind-≥3 remote path) is documented in the planned Part 13 distribution chapter.

---

## The symbol roster

Both passes follow the dual-overload shape shared by every multi-core backend pass
(`LncSplitter`, `LncVerifier`): a per-`Module&` overload that is a no-op / abort shim, and a
`vector<unique_ptr<Module>>&` overload that is the real worker. The `BackendPass` dispatcher
drives the vector overload because the work is inherently cross-module / cross-core — the
per-`Module&` shim is never run for these passes. [CONFIRMED — `nm -DC libwalrus.so`; the
shim/worker split matches `LncSplitter` D-H03 and `LncVerifier` D-G11.]

`LowerLocalCollectives` (factory `register_generator_lower_local_collectives__` @`0x162a6a0`):

| Symbol | Address | Role |
|---|---|---|
| `run(bir::Module&)` | `0x1615680` | per-module shim (stores `0x2`, no-op) |
| `run(vector<unique_ptr<Module>>&)` | `0x1624850` | **real worker** (~`0x5e50` bytes) |
| `getRemoteCores(unsigned long self)` | `0x1617940` | `{0..lnc-1}\{self}` |
| `getScatterDimension(PhysicalAccessPattern const&)` | `0x1615d40` | tail-jmp alias → `getFirstSplittableNonPartitionDimension` `0x1615c70` |
| `extractAndGroupCCOps(...)` | `0x1618440` | clusters IT-48 ops into SPMD groups |
| `lowerSendRecv(...)` | `0x161cc70` | **CollectiveKind 0** |
| `lowerSendRecvCCE(...)` | `0x161f130` | **CollectiveKind 1** |
| `lowerAllReduce(...)` | `0x1621a80` | **CollectiveKind 2** |
| `createRemoteAP(...)` | `0x161cad0` | clone AP onto a peer core |
| `createPartialAP(...)` | `0x1620fb0` | per-core sub-tensor AP (the scatter slice) |
| `getMemoryLocation(...)` | `0x161ab30` | remote-memloc minter (`vnc_remote_addr_map`) |
| `insertCoreBarrier(...)` | `0x161a150` | emit `InstCoreBarrier` (IT 87) |
| `updateDeps(...)` | `0x1619620` | dependency fix-up before deletion |
| `sendRecvToGPSIMDMaxBytesPerPartition` | `.bss 0x3e050a0` | `cl::opt` byte-budget value |

`InsertPTCOMFlat` (factory `register_generator_insert_ptcom_flat__` @`0x16ae030`):

| Symbol | Address | Role |
|---|---|---|
| `run(bir::Module&)` | `0x16a5710` | per-module shim (stores `0x2`, no-op) |
| `run(vector<unique_ptr<Module>>&)` | `0x16ad1c0` | **real worker** |
| `identifyPTCOMBlocks(...)` | `0x16aa040` | per-memloc convergence block (postdom) |
| `identifyLastWrites(...)` | `0x16a9280` | per-(ML,BB) last writer |
| `extendLastWrites(...)` | `0x16abd80` | propagate last-write across cores |
| `insertPTCOMs(...)` | `0x16acc50` | emit `InstTensorCompletion` (IT 94) |
| `findMemLocOnCore0(...)` | `0x16a7540` | flat core-0-name lookup |
| `findCBOnCore0(...)` | `0x16a8010` | match a `CoreBarrier` on core 0 |
| `meetsPTCOMRequirements(...)` | `0x16a5840` | free fn — non-loop-SCC gate |
| `enablePTCOM` | `.bss 0x3df6a40` | pass-enable `cl::opt` |
| `enableRemoteSemaphoreDMA` | `.bss 0x3e05160` | extra-barrier gate (kind 0) |

---

## What "local" means — the on-chip vs remote split

`InstCollectiveCompute` carries its `CollectiveKind` at `Inst+0xf8`. The enum (D-D07) is:

```
0 SendRecv   1 SendRecvCCE  2 AllReduce  3 ReduceScatter  4 AllGather
5 AllToAll   6 AllToAllV    7 Permute    8 PermuteReduce   9 PermuteImplicit
10 PermuteReduceImplicit
```

A single IT-48 opcode multiplexes all of them; the kind field is the discriminator. The worker
prologue first gates on the LNC width and then dispatches on the kind.

**The LNC gate** [CONFIRMED — run prologue `0x1624876`]: `cmp DWORD[Module->PassOptions+0x1A4],
1; je return`. `PassOptions+0x1A4` is `lnc_size` / number of cores (confirmed S2-08, D-H03). When
`lnc == 1` (a single core), there is no cross-core exchange to lower, so the worker returns
cleanly — the pass is a no-op on single-core configs.

**The kind dispatch** [CONFIRMED — worker dispatch `0x1629337`; STRONG that only 0/1/2 reach a
`lowerXXX` helper]:

```c
// LowerLocalCollectives::run(vector<Module>&)  @0x1624850 (worker dispatch @0x1629337)
// 'cc0' is group[0]; kind = cc0[+0xf8] loaded into eax.
unsigned kind = *(uint32_t *)(cc0 + 0xf8);

cmp(kind, 1);
ja  >= 3 -> default_remote;        // 0x162999f  (unsigned 'ja' past 1 => kind >= 3)
je  == 1 -> lowerSendRecvCCE;      // 0x1629949
cmp(kind, 2);
je  == 2 -> lowerAllReduce;        // 0x1629859
test_eq_zero -> lowerSendRecv;     // 0x1629676  (kind == 0)
```

The control flow is an unsigned `ja` past `1` (which folds *every* kind ≥ 3 into one branch
because the comparison is unsigned), plus explicit `== 1` / `== 2` / `== 0` compares. The two
groups behave very differently:

- **Kinds 0 / 1 / 2** (`SendRecv`, `SendRecvCCE`, `AllReduce`) — the **on-chip / local** path.
  Each is decomposed *here* into SBUF↔SBUF data exchange plus `CoreBarrier` synchronization.
  These are the collectives whose traffic stays inside the LNC group of on-die cores.

- **Kind ≥ 3** (`ReduceScatter` / `AllGather` / `AllToAll` / `AllToAllV` / `Permute` family) —
  the **remote / ICI** path. The collective op is **not** decomposed here. The run loop still
  calls `insertCoreBarrier` per remote core (`0x16299f7`) to *fence* it, but the IT-48 op is
  left intact for a different cross-node lowering downstream (the runtime collective library /
  ICI fabric). [STRONG — only kinds 0/1/2 reach a `lowerXXX` helper; kind ≥ 3 reaches only
  `insertCoreBarrier`. The precise boundary between "fence-only here" and the true downstream
  remote lowering is INFERRED to be the runtime collective lib / ICI; this is not pinned in the
  binary.]

The decomposition target is on-die SBUF↔SBUF transport: either the GPSIMD cross-core SB2SB copy
engine (`InstGPSIMDSB2SB`, IT 33) or core-to-core DMA (`InstDMACopy`), synchronized by
`InstCoreBarrier` (IT 87) and finalized by the PTCOM op `InstTensorCompletion` (IT 94).

---

## The worker loop

```c
// LowerLocalCollectives::run(vector<Module>&)  @0x1624850
// reconstructed from disasm 0x1624850..0x162a6a0
void run(vector<unique_ptr<Module>> &modules) {
    int lnc = modules[0]->passOptions[+0x1A4];        // 0x1624876
    if (lnc == 1) return;                             // nothing to lower

    auto precMap = getPrecedingCoreBarrierOrLocalCCMap(modules);  // 0x16248ab
    auto groups  = extractAndGroupCCOps(modules);     // 0x16248c4 -> vector<vector<CC*>>
    vector<uint> allCores = {0 .. lnc-1};             // built via _M_realloc_insert loop

    for (auto &group : groups) {                      // one group = the SPMD-symmetric copies
        CC *cc0   = group[0];                         //   of ONE collective across all cores
        uint core = cc0->getModule()->getNeuronCoreId();          // 0x1624a28
        vector<DenseMap<MemoryLocation*,MemoryLocation*>> denseMaps[lnc]; // per-core remote map
        vector<vector<Instruction*>>                      newInsts[lnc];  // ops per core
        unsigned long semCounter = 0;

        unsigned kind = *(uint32_t *)(cc0 + 0xf8);
        switch (kind) {                                                  // dispatch @0x1629337
          case 0: lowerSendRecv   (group, newInsts, allCores, denseMaps, &semCounter); break; // 0x16296c2
          case 1: lowerSendRecvCCE(group, newInsts, allCores, denseMaps, &semCounter); break; // 0x1629995
          case 2: lowerAllReduce  (group, newInsts,           denseMaps, &semCounter); break; // 0x16298a2
          default: /* kind >= 3, remote/ICI: fence only, op left intact */              // 0x162999f
            for (size_t i = 0; i < group.size(); ++i)
                insertCoreBarrier(group[i], allCores, newInsts, i, name, &semCounter);   // 0x16299f7
        }
        // kind==0 special case (0x16296e2): unless enableRemoteSemaphoreDMA (.bss 0x3e05160)
        //   is set, add a CoreBarrier per remote core for the SendRecv swap.

        for (size_t i = 0; i < group.size(); ++i)
            updateDeps(group[i], newInsts[i]);         // 0x162971d (dependency fix-up)
        for (CC *cc : group)
            cc->getBasicBlock()->removeInstruction(cc);// 0x1629742  <- original IT48 deleted
    }
    renumberCoreBarriers(modules, getSubgraphId(modules[0]));  // 0x162979f — global CB ids
}
```

Two helpers do the work that makes the lowering *joint* across cores rather than independent
per-core:

- **`extractAndGroupCCOps`** (`0x1618440`) [CONFIRMED]: walks every basic block of every core
  module (`getFirstWithin` / `getNextInstFlat`), selects insts with `InstructionType == 48`
  (the `cmp [I+0x58],0x30` test), and clusters them into a `vector<vector<InstCollectiveCompute*>>`
  — one inner vector per *logical* collective (the matching SPMD copies that `lnc_splitter`
  cloned across cores). The grouping is exactly what turns a lowering into a joint cross-core
  operation: the swap on core A and the swap on core B are members of one group.

- **`getRemoteCores(self)`** (`0x1617940`) [CONFIRMED]: loops `core = 0..lnc-1`, pushes `core`
  into the result iff `core != self` (`cmp eax,ebx; je skip`); re-reads `lnc` each iteration
  from `PassOptions+0x1A4`. Returns `{0..lnc-1}\{self}` — the set of peers each core must
  exchange with. This is the same `getRemoteCores` D-H03 names as the feed for the
  `vnc_remote_addr_map` `RemoteLocalTarget` minting.

`getPrecedingCoreBarrierOrLocalCCMap` (`0x1615e30`) and `precededByLocalCC` (`0x1615d50`) record,
per core, the most recent preceding `InstCoreBarrier` or already-lowered local CC (a `local`
flag at `Inst+0x1b0`), so a redundant barrier is not inserted when a region is already
fenced. [STRONG — `MapVector<CC*,Inst*>` destroyed at `0x162984f`.]

---

## Kind 0 — `lowerSendRecv`: the GPSIMD-or-DMA SBUF swap

A `SendRecv` is a paired send+recv "swap" between two cores' SBUF tiles — the TP/FSDP token
swap. This is the only kind where the data-mover engine is chosen per instruction; the other
two are fixed.

**The engine-selection gate** [CONFIRMED — `0x161dfc3..0x161e8c5`]:

```c
// lowerSendRecv  @0x161cc70  — engine selection
bool compatible =
    InstGPSIMDSB2SB::isCompatible(lnc, module, srcAP, dstAP, inst);   // 0x161dfc3
    // libBIR-native SB2SB shape/context predicate (libBIR @0x2931d0).
    // FIRST check inside isCompatible is `cmp rdi,0x2` (ctx==2): the
    // cross-core context must be exactly 2 cores-per-LNC. [D-M11/D-E22]

if (!compatible) {
    // -> DMA fallback
    insertElement<InstDMACopy>(bb, ip, name);                        // 0x161eed6 / 0x161e3f5
} else {
    int bpp = AccessPattern::getNumElementsPerPartition()
              * DtypeSize[dtype];                                     // 0x161e892
              // DtypeSize LUT @.rodata 0x1e01ce0, indexed by AP+0x30, bound <= 0x13 = 19.
    int ebpp = InstGPSIMDSB2SB::getEffectiveBytesPerPartition(bpp, dtype); // 0x161e8b3
               // dtype byte-pad (x4 / x2 / x1) applied via `shl eax,2` etc. (libBIR @0x291a50)

    if (ebpp > sendRecvToGPSIMDMaxBytesPerPartition) {               // 0x161e8c5
        insertElement<InstDMACopy>(bb, ip, name);                    // -> DMA fallback
    } else {
        // -> GPSIMD path
        BasicBlock::checkInsertionPointValid(...);                   // 0x161d721
        Inst *g = insertElement<InstGPSIMDSB2SB>(bb, ip, name);      // 0x161d72f
        *(uint8_t *)(g + 0x90) = 1;                                  // 0x161d772 (lowered/local marker)
    }
}
// Both paths also emit an InstTensorCopy when an AP repack is needed
// (insertElement<InstTensorCopy>).
```

So the GPSIMD-vs-DMA choice is a two-stage filter:

1. **Shape/context predicate.** `InstGPSIMDSB2SB::isCompatible` (libBIR `@0x2931d0`) must hold —
   the source/dest access patterns must form a legal SB2SB shape and the cross-core context must
   be 2 cores (`cmp rdi,0x2` is the first predicate). If not compatible at all → DMA. [CONFIRMED
   — D-M11 pins `isCompatible@0x2931d0`, ctx==2 first predicate.]

2. **Byte budget.** Even when compatible, the *effective* bytes-per-partition
   (`getEffectiveBytesPerPartition`, libBIR `@0x291a50`, applying the dtype byte-pad factor)
   must not exceed the pass-side `cl::opt` value `sendRecvToGPSIMDMaxBytesPerPartition`. Above
   it → DMA; at or below it → GPSIMD. The `cl::opt` is named `sendrecv-to-gpsimd-max-bpp`
   (`.rodata 0x1c88377`) with help text *"Bytes/partition under which local swapping SendRecvs
   will be mapped to GPSIMDSB2SB"* (`.rodata 0x1d7ff48`). [CONFIRMED — string byte-evidence.]

This pass-side knob sits **under** the hard libBIR ceiling
`InstGPSIMDSB2SB::MaxBytesPerPartition = 0x400 = 1024` (libBIR `.rodata @0x784a00`, byte-read
`00 04 00 00`; assert `GPSIMDSB2SB.cpp:52`, D-E22/D-M11). The libBIR `isCompatible` enforces the
1024 ceiling unconditionally; the pass knob is a *softer* threshold the deployment can set lower
to bias more swaps toward DMA. [CONFIRMED — the 1024 ceiling and the pass knob are two distinct
gates, cross-confirmed in D-M11.] **GAP:** the numeric *default* of `sendrecv-to-gpsimd-max-bpp`
is the `cl::opt` initializer, which is not read out of the binary here. [SPECULATIVE — the
default is unverified; only the existence and semantics of the knob are CONFIRMED.]

**Addressing & sync.** `createRemoteAP(srcAP, allAPs, coreIdx, …, isRemote, denseMaps)`
(`0x161cad0`) clones `srcAP` and calls `getMemoryLocation` to bind it to the *peer* core's
`MemoryLocation`; `setLocation` rebinds it. `insertCoreBarrier(group, allCores, …)` fences the
swap. Unless `enableRemoteSemaphoreDMA` (`.bss 0x3e05160`) is set, the run loop adds an extra
per-remote-core `CoreBarrier` for the SendRecv swap (the kind-0-only branch at `0x16296f6`).
[STRONG.]

---

## Kind 1 — `lowerSendRecvCCE`: the DMA-via-CCE swap

`SendRecvCCE` is a `SendRecv` routed through the Collective-Communication-Engine DMA path with
no GPSIMD option. The call set is the DMA-engine sibling of `lowerSendRecv`: `createRemoteAP`,
`insertCoreBarrier`, `cloneToOutput`, a `PhysicalAccessPattern` ctor, and
`insertElement<InstDMACopy>` **only** — there is no `isCompatible` probe and no `GPSIMDSB2SB`
symbol anywhere in the body. The same remote-AP minting and `CoreBarrier` sync as kind 0, but
the data mover is unconditionally `InstDMACopy` (the CCE descriptor flavour is selected later by
`lower_dma` / `LegalizeCCEDMA`, D-H06). [STRONG — the call-set diff vs `lowerSendRecv` is the
absence of every GPSIMD symbol.]

---

## Kind 2 — `lowerAllReduce`: reduce-scatter + all-gather

The on-chip `AllReduce` is decomposed into a DMA-based **reduce-scatter** followed by an
**all-gather** across the LNC cores' SBUF, sharded along a free axis so each core reduces
`1/lnc` of the tensor.

```c
// lowerAllReduce  @0x1621a80  — reconstructed; call order from 0x1621a80..0x1624850
void lowerAllReduce(group, newInsts, denseMaps, unsigned long *semCounter) {
    // Phase A — seed / initial copy
    insertElement<InstDMACopy>(...);                                   // 0x16221ab
    auto ap = srcAP.clone();
    auto ml = getMemoryLocation(...); ap.setLocation(ml);              // 0x16222ab / 0x1622355 / 0x162236b
    auto arg = ap.cloneToArgument(inst);
    auto out = ap.cloneToOutput(inst);                                 // 0x16223c6 / 0x16223fa
    legalizeAPs(lnc, srcInst, newInst);                               // 0x1622417

    // Phase B — REDUCE (scatter): each core owns one slice
    auto remotes   = getRemoteCores(self);                            // 0x1622dd8
    auto scatterDim = getScatterDimension(ap);  // x4 over candidate APs // 0x1622f8c..0x16230aa
    insertElement<InstDMACopy>(/* reduce copy */);                    // 0x16234b6
    // cloneToArgument / clone / getMemoryLocation / setLocation / cloneToOutput / legalizeAPs

    // Phase C — BARRIER between reduce and gather
    remotes = getRemoteCores(self);                                   // 0x16236a8
    insertElement<InstCoreBarrier>(...);                              // 0x1623899

    // Phase D — ALL-GATHER: broadcast each core's reduced slice to all peers
    remotes = getRemoteCores(self);                                   // 0x162392a
    insertElement<InstDMACopy>(/* gather copy */);                    // 0x1623fe2
    createPartialAP(ap, allAPs, totalSize, partSize, coreIdx, scatterDim, ..., denseMaps); // x2
    //                                                                 // 0x1624134 / 0x1624209
    legalizeAPs(...);                                                  // 0x1624244
    remotes = getRemoteCores(self);  // x2                             // 0x162426e / 0x1624443
}
```

- **`getScatterDimension`** (`0x1615d40`, tail-jmp to `getFirstSplittableNonPartitionDimension`
  `0x1615c70`) [CONFIRMED]: skips the partition dim (`MemoryLocation::getPartitionDim @0x5f2c00`)
  and walks the free dims (`cmp [ML+0x110],0x4` numDims check), returning the first dim with
  extent ≥ 4 that divides evenly across `lnc` cores — the axis the reduction is sharded on. It
  has exactly one caller (`lowerAllReduce`, 4 calls / 2 AP arms; NX-156): one arm is the
  *scatterDim*, the other the *gatherDim*. [CONFIRMED — NX-156.]

- **`createPartialAP`** (`0x1620fb0`) [CONFIRMED]: builds a per-core **sub-tensor** AP —
  `setOffset(coreIdx * partStride)` / `setPattern` / `setType` / `setLocation` — so each core's
  DMA touches only its `1/lnc` slice; `getMemoryLocation` then binds it to the owning core's
  memloc. This is what makes the reduce a *scatter* (each core owns one slice) rather than an
  all-to-one. Its splittable-axis argument `a5` is exactly the `getScatterDimension` result,
  which downstream becomes the 2-bit `gather_dim`/`scatter_dim` DGE descriptor field (NX-163).
  [STRONG; the a5→DGE-axis bridge is CONFIRMED in NX-163.]

The `AllReduce` therefore **never** uses GPSIMD — it is always `InstDMACopy` + `InstCoreBarrier`
(NX-156: "one `InstDMACopy` per replica-core, + `InstCoreBarrier` (`_cb_end`) fences"). The
body contains no `isCompatible` probe and no `GPSIMDSB2SB`: the reduced payload is generally
larger than the GPSIMD per-partition budget and the reduce-scatter shape is not an SB2SB swap.
[INFERRED from the call set — no GPSIMD symbols in `lowerAllReduce`; corroborated by NX-156.]

---

## Remote addressing — the `vnc_remote_addr_map`

`getMemoryLocation(allAPs, idx, core, denseMaps&)` (`0x161ab30`) is the cross-core address
minter D-H03 names as the source of the `vnc_remote_addr_map` `RemoteLocalTargets`:

- `denseMaps` is a `vector<DenseMap<MemoryLocation*,MemoryLocation*>>` — **one remote map per
  core**; `DenseMapBase::operator[]` caches src-memloc → remote-memloc so the same remote target
  is reused across the group. [CONFIRMED — `DenseMap<ML*,ML*>` grow + `operator[]` calls.]
- It resolves the target by tensor id via `MemoryLocationSet::getMemlocByTensorId` within the
  *remote* core's `MemoryLocationSet`. [CONFIRMED.]
- It mints/labels remote targets with names carrying `_remote_<core>` and `_set` suffixes
  (`.rodata "remote" @0x1c86837`, `"_remote_" @0x1c88352`, `"_set" @0x1c867ac`). [CONFIRMED.]
- It marks the target with `MemoryLocation::setIsWrittenByAnotherCore(true)` so downstream
  allocators (`coloring_allocator_dram_shared`, `sync_shared_allocations`) and `vnc_link`
  keep the cross-core writer alive and resolve the physical VNC address. The diagnostic strings
  *" has a tensor id of "* (`0x1c88328`) and *" and isAllocated is "* (`0x1c8833d`) confirm the
  tensor-id + allocation reasoning. [CONFIRMED.]

`legalizeAPs` (`0x161b9a0`) [STRONG] repacks the AP pattern
(`PhysicalAccessPattern::setPattern`) against the partition dim after retargeting so the cloned
remote/partial APs are HW-legal SBUF/DRAM patterns. `insertCoreBarrier` (`0x161a150`)
[CONFIRMED] inserts `InstCoreBarrier` (IT 87) over the given core-id vector — the
semaphore/barrier between exchange phases — and `renumberCoreBarriers` (`0x5e9080`) re-assigns
globally-unique barrier ids across all core modules after lowering.

---

## `insert_ptcom_flat` — the point-to-point completion tokens

PTCOM = **point-to-point COMpletion**. The pass inserts an `InstTensorCompletion` (IT 94, S2-04
enum) per shared `MemoryLocation` at a convergence block, named `<…>-PTCOM` (`.rodata "-PTCOM"
@0x1c87e87`). It is the wire-level p2p **sync token** — a completion / wait marker, *not* a data
mover: a consumer core blocks on it until a producer core has finished writing the shared tensor.
It complements the `CoreBarrier` (the collective fence) with finer point-to-point
producer→consumer completion, so a reader need not wait on a full barrier. [CONFIRMED —
`insertPTCOMs` inserts `InstTensorCompletion` + `attachWholeTensorInputAP`, no copy emitted.]

```c
// InsertPTCOMFlat::run(vector<Module>&)  @0x16ad1c0  (linear, CONFIRMED)
void run(vector<unique_ptr<Module>> &modules) {
    int subgraphs = modules.size() / lnc;             // div @0x16ad208
    // if (subgraphs == 1 && ...) { log; proceed; }   // 0x16ad210
    auto blocks   = identifyPTCOMBlocks(modules);     // 0x16ad66b -> vector<pair<ML*,BB*>>
    auto lastW    = identifyLastWrites(blocks);       // 0x16ad679 -> per-(ML,BB) last writer
    auto extended = extendLastWrites(lastW, modules); // 0x16ad69c -> propagate across cores
    insertPTCOMs(extended);                           // 0x16ad6a7 -> emit InstTensorCompletion
}
// Pass-enable gate: cl::opt enablePTCOM (.bss 0x3df6a40, string @0x1600b1) checked by the
//   pipeline/PassAdder (not the lambda); off => pass omitted. [CONFIRMED]
```

The token-placement mechanism, in order:

- **`identifyPTCOMBlocks`** (`0x16aa040`) [CONFIRMED]: per shared `MemoryLocation`, collects the
  set of BBs that write it (`DenseMap<ML*, DenseSet<BB*>>`), then uses CFG dominance to pick the
  insertion block = the **first common postdominator** of all writer BBs
  (`CFG::getImmediatePostdominator`, `CFG::getFirstCommonPostdominator`). It filters via
  `meetsPTCOMRequirements` and `CFG::getSCCForBB`, and asserts `"loc2index.count(loc) > 0"`
  (`.rodata 0x1c8931e`) and `"blocks.size() == 1"` (`0x1c89337`) — exactly **one** convergence
  block per memloc.

- **`meetsPTCOMRequirements`** (free fn `0x16a5840`) [STRONG]: a candidate BB qualifies only if
  it is **not** inside a non-trivial loop SCC (`CFG::getSCCForBB`). Completions are hoisted out
  of loops so the p2p token fires once after the producing region, not every iteration.

- **`findMemLocOnCore0`** (`0x16a7540`) / **`findCBOnCore0`** (`0x16a8010`) [CONFIRMED]: the
  "**FLAT**" addressing scheme. All per-core modules reference the *canonical core-0* names:
  `findMemLocOnCore0` looks a shared memloc up by string name in core-0's module;
  `findCBOnCore0` matches a `CoreBarrier` on core 0 by `Module::getName` + name-hash
  (`_Hash_bytes`, `memcmp`). "Flat" means a single flat namespace keyed on core 0, so cross-core
  producer/consumer pairs are correlated by **shared name** rather than by per-core pointer
  identity — which differs after the `lnc_splitter` JSON clone (D-H03: the clone is a
  `saveJson`→`loadJson` round-trip, so pointers are not preserved but names are).

- **`identifyLastWrites`** (`0x16a9280`) / **`extendLastWrites`** (`0x16abd80`) [STRONG]:
  `identifyLastWrites` finds, per (ML, BB), the last writer instruction; `extendLastWrites`
  propagates that `LastWriteInfo` across the per-core modules (so a completion on core B
  references the producing write on core A), asserting `"extendedWrite != nullptr"`
  (`.rodata 0x1c89378`). `findNewLastWrite` (`0x16a8df0`) re-locates the writer after
  re-lowering, gated by `meetsPTCOMRequirements`. **GAP:** the exact `LastWriteInfo` struct
  layout is not pinned. [SPECULATIVE on the field offsets.]

- **`insertPTCOMs`** (`0x16acc50`) [CONFIRMED]: for each (ML, insertInst) pair, insert
  `InstTensorCompletion` before `BasicBlock::getFirstTerminator` (`0x16acd1f` / `0x16acd3c`),
  then `attachWholeTensorInputAP(newInst, ML)` (`0x16acd4b`) so the completion waits on the
  **whole** shared tensor; `bir::isDataPathEngine` routes it to the proper sync engine. The
  `"_sb2sb"` string (`0x1c88425`) confirms the completions target the on-chip SB2SB transport
  produced by the kind-0 lowering.

---

## Why each pass runs twice — dual placement

Both passes are registered at two static pipeline slots (S2-06 order table):

- **Default (post-schedule) phase**, contiguous: `80 lower_local_collectives → 81
  extend_shared_lifetimes → 83 sync_shared_allocations → … → 95 insert_ptcom_flat`.
- **Alternate (pre-`post_sched`) phase**, contiguous: `111 lower_local_collectives → 112
  insert_ptcom_flat`.

The selector is the `cl::opt` `--run-shared-allocation-before-post-sched`, help text verbatim:
*"Run lower_local_collectives and coloring_allocator_dram_shared before post_sched. Only
relevant for trn2."* (D-A11). The same pass body is registered at two static slots; the
pipeline builder positions the collective lowering + its PTCOM completions either **after**
`post_sched` (default) or **before** `post_sched` (when the flag is set — Trn2 / CoreV3 only).
[CONFIRMED — flag text and arch restriction are verbatim.]

The two are **mutually exclusive at runtime**, not both-run:

- `lower_local_collectives` is idempotent across placements because its `run()` deletes the
  original IT-48 CC op (`removeInstruction @0x1629742`) once lowered. Whichever slot fires first
  consumes the work; the other finds `extractAndGroupCCOps` returns no IT-48 ops → no-op. The
  `lnc == 1` early-return likewise makes single-core a no-op at either slot. [CONFIRMED — the
  deletion is in `run`.]
- `insert_ptcom_flat` is paired with whichever `lower_local_collectives` slot is active (80→95
  vs 111→112) so the PTCOM completions are inserted in the **same** scheduling regime as the
  collective DMAs they fence (pre- vs post-RA / post-sched addresses). [STRONG.]

On Trn2 the shared-DRAM allocation (`coloring_allocator_dram_shared`) and the collective
lowering must run **before** `post_sched` so the post-RA scheduler sees the real cross-core
DMA / GPSIMD traffic and the shared allocations are colored coherently; on other arches the
default keeps them after `post_sched`. The dual registration is the mechanism that lets one
static pipeline serve both orderings.

---

## Adversarial re-verification

The five highest-value claims, re-checked against the binary evidence and the sibling
cross-references:

1. **Kind dispatch is unsigned `ja` past 1 plus explicit 0/1/2 compares** — CONFIRMED at the
   worker dispatch `0x1629337`; the field is `CC+0xf8` (D-D07), and only kinds 0/1/2 reach a
   `lowerXXX` helper while ≥3 reaches only `insertCoreBarrier`. The "fence-only" nature of ≥3 is
   STRONG (call-graph evidence); the *destination* of the remote lowering is INFERRED (runtime
   collective lib / ICI), not pinned.

2. **GPSIMD-vs-DMA two-stage gate** (`isCompatible` shape/ctx predicate, then
   `getEffectiveBytesPerPartition` vs `sendrecv-to-gpsimd-max-bpp`) — CONFIRMED. D-M11
   independently pins `isCompatible@0x2931d0` (ctx==2 first predicate),
   `getEffectiveBytesPerPartition@0x291a50` (dtype byte-pad), and the `MaxBytesPerPartition=1024`
   ceiling in libBIR. The `cl::opt` name + help strings are byte-confirmed. The numeric
   *default* of the knob is the genuine GAP (SPECULATIVE).

3. **`lowerAllReduce` is DMA-only (reduce-scatter + all-gather)** — CONFIRMED structure;
   NX-156 corroborates "one `InstDMACopy` per replica-core + `InstCoreBarrier` fences",
   `getScatterDimension` (scatter arm vs gather arm), and `createPartialAP`. The *reason* GPSIMD
   is never chosen (payload > budget, non-SB2SB shape) is INFERRED from the call set.

4. **`getMemoryLocation` mints the `vnc_remote_addr_map` with `setIsWrittenByAnotherCore`** —
   CONFIRMED via the `_remote_<core>` / `_set` rodata strings and the diagnostic strings;
   D-H03 independently names this as the `RemoteLocalTarget` source feeding `vnc_link` and the
   shared-DRAM allocators.

5. **`insert_ptcom_flat` places `InstTensorCompletion` at the writers' first common
   postdominator, hoisted out of loops, keyed on flat core-0 names** — CONFIRMED for the IT-94
   insert + `attachWholeTensorInputAP` + the `-PTCOM` name; STRONG for the postdominator +
   non-loop-SCC placement and the flat-naming correlation. The `LastWriteInfo` struct layout is
   the GAP (SPECULATIVE field offsets).

**Re-verify ceiling.** The libwalrus binary is present in the corpus as IDA DBs and
decompiled/disasm sidecars, and the addresses above were resolved by `nm -DC` and disassembly
captured in the D-H23 analysis; the five claims are each independently corroborated by a second
sibling note (D-D07, D-M11/D-E22, NX-156/NX-163, D-H03, S2-04). The remaining unverifiable
items are the `cl::opt` numeric defaults (initializers not read out), the `LastWriteInfo` struct
layout, and the precise downstream lowering of the kind-≥3 remote collectives (fenced here, but
the cross-node decomposition is in a different translation unit).
