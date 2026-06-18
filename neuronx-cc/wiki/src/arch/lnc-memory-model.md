# The multi-core (LNC) memory model

> *All symbols and addresses on this page are read from the **cp310** wheel of `neuronx_cc` 2.24.5133.0+58f8de22 (cp310/cp311/cp312 share this logic; the `libwalrus.so` C++ core is rebuilt per wheel — cp310 and cp311 share a size but not a SHA, cp312 differs in size — so re-confirm any raw address against cp311/cp312, see [Version Provenance](../reference/versions.md)). The backend constants and passes live in `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331…`); the IR-side handles in `libBIR.so` (BuildID `a9b1ea38…`). For `.text`/`.rodata`, VMA equals file offset. Treat every address as version-pinned.*

## Abstract

A *Logical NeuronCore* (LNC) is the compiler's name for a group of `N` physical NeuronCores fused into one SPMD device. On Trainium-2 (`sunda`/gen2 and later, the parts that support it) `N = 2`; everywhere else `N = 1` and this whole model collapses to the single-core case. The number `N` is read from one field — `PassOptions+0x1A4` — and it drives the entire backend split. The question this page answers is purely conceptual: **after the LNC split, what does each core own privately, and what do the cores share?** The mechanisms — the splitter that clones the program, the allocators that color each core, the linker that re-assembles them — are [Part 8](../walrus/) territory and are only pointed at here.

The answer is asymmetric across the three memory classes, and the asymmetry is the whole point. **SBUF and PSUM are *replicated*: each physical core owns the full 128-partition State Buffer and the full 8-bank PSUM, addressed from the same per-core geometry every other core reads — there is no `1/N` slicing of on-chip memory.** **DRAM is *partitioned*: each core gets its own per-core HBM window, plus exactly one shared cross-core DRAM region that every core addresses at the same physical address.** Cross-core data movement therefore has exactly one channel: **shared DRAM.** A core can never name another core's SBUF or PSUM; everything inter-core goes out to DRAM and back (the one fixed-function on-chip exception, the GPSIMD SBUF↔SBUF swap, is discussed at the end and does not change the model — it is a peer-to-peer copy, not a shared address space).

The "replicated, not partitioned" claim for on-chip memory rests on two independent supports, both verified on the binary for this page:

- A **positive**: the SBUF allocator stores the *full* `SB_SIZE` for the target arch into its per-core budget, with **no division by `N`** anywhere in the read chain (`SB_Allocator` ctor `@0xa97b82`–`0xa97bc3`).
- A **negative**: there is **no `sharedSBUF` / `sharedPSUM` symbol or string** anywhere in `libwalrus.so` or `libBIR.so`. The entire "shared/cross-core" machinery is keyed to DRAM alone.

A reimplementer who internalizes this page knows where each tensor can legally live, which memory is the synchronization surface, and why a multi-core program is structurally `N` copies of one single-core program that meet only in HBM.

| | |
|---|---|
| **The one number `N`** | `lnc_size = *(uint*)(PassOptions+0x1A4)`, default 1; 2 on Trn2 (`sunda`) |
| **SBUF** | REPLICATED — full `128 × SB_SIZE` per core; no `1/N` slice |
| **PSUM** | REPLICATED — full `8 banks × 2048 B × 128 parts` per core (gen1: 4 banks × 64 parts) |
| **DRAM** | PARTITIONED — per-core HBM window (`Core::dramSizeGb` GiB) + one shared region |
| **Cross-core channel** | shared DRAM only (`isSharedPostDRAMAlloc`); never SBUF/PSUM |
| **SBUF budget read** | `SB_Allocator` ctor `@0xa97b82` → full `Statebuf+0`, **no `/N`** |
| **Per-core HBM gate** | `HBMUsage` `@0x16ba31f` → `Core+0x30 (dramSizeGb) << 30` |
| **`N`=2 hard-coding** | `cmpl $0x2,0x1a4(%rax)` at GPSIMD encoder `@0x1359a67`/`@0x1359da1` |
| **Negative (verified)** | no `sharedSBUF`/`sharedPSUM`/`getSharedSBUFforRank` symbol exists |

---

## At a glance: owns vs shares

```text
   ┌──────────────────── LOGICAL NeuronCore (LNC), N cores ─────────────────────┐
   │                                                                            │
   │   core 0 (private)                         core 1 (private)                │
   │   ┌──────────────────────────┐             ┌──────────────────────────┐    │
   │   │ SBUF  [REPLICATED]       │             │ SBUF  [REPLICATED]       │    │
   │   │   128 parts × SB_SIZE    │             │   128 parts × SB_SIZE    │    │
   │   │   full, private, never   │             │   full, private, never   │    │
   │   │   reachable by core 1    │             │   reachable by core 0    │    │
   │   ├──────────────────────────┤             ├──────────────────────────┤    │
   │   │ PSUM  [REPLICATED]       │             │ PSUM  [REPLICATED]       │    │
   │   │   8 banks × 2KB × 128p   │             │   8 banks × 2KB × 128p   │    │
   │   │   full, private          │             │   full, private          │    │
   │   ├──────────────────────────┤             ├──────────────────────────┤    │
   │   │ DRAM  [PARTITIONED]      │             │ DRAM  [PARTITIONED]      │    │
   │   │   per-core HBM window    │             │   per-core HBM window     │    │
   │   │   = Core::dramSizeGb GiB │             │   = Core::dramSizeGb GiB │    │
   │   │   private spills/interm. │             │   private spills/interm. │    │
   │   └────────────┬─────────────┘             └─────────────┬────────────┘    │
   │                │      the ONLY cross-core channel        │                 │
   │                └───────────────────┬─────────────────────┘                 │
   │                                    ▼                                        │
   │            ┌─────────── SHARED DRAM region (one physical copy) ──────────┐  │
   │            │  isSharedPostDRAMAlloc == true. Same physical address on     │  │
   │            │  every core. Ordering via anti-dep + CoreBarrier/semaphore.  │  │
   │            └──────────────────────────────────────────────────────────────┘  │
   └────────────────────────────────────────────────────────────────────────────┘

   replicated  = each core sees the FULL geometry; no 1/N slicing; never shared.
   partitioned = each core fills its OWN window; one named-unified shared region.
```

The diagram is drawn for `N = 2` (the only non-trivial value this build supports — see *The one number `N`*), but the model generalizes to any `N`: `N` private SBUF/PSUM, `N` private DRAM windows, one shared DRAM region.

---

## The one number `N` (`lnc_size`)

### Purpose

One integer — call it `N`, the NeuronCores-per-Logical-NeuronCore fan-out — drives the entire multi-core model. It decides how many copies of the program exist, how many private address spaces are allocated, and whether any cross-core machinery runs at all. Getting this number and its single storage location right is the prerequisite for everything else on the page.

### Where `N` lives

`N` is plumbed end-to-end from the front-end to the backend under three names that all denote the same value:

- **Front-end** (`CompileCommand`): `--logical-nc-config` / `--lnc` sets `self.logical_nc_config`; default **2 on Trn2 (`sunda`)**, forced **1** elsewhere. This seeds the SPMD device mesh. *(STRONG — front-end default established in the driver; the CLI→`PassOptions` plumbing is not traced through one symbol on this page, see [Part 8](../walrus/).)*
- **Backend** (`PassOptions`): `lnc_size = *(uint*)(PassOptions+0x1A4)`, default 1. This is the single field every backend pass reads. *(CONFIRMED — the `+0x1A4` field is read at every site below.)*
- **Splitter / fork passes**: `LncSplitter`, `CoreForkPass`, `getRemoteCores`/`getAllCores` all re-read `PassOptions+0x1A4` to learn the clone count and the peer set. *(CONFIRMED at the `CoreForkPass` site below.)*

The field offset `+0x1A4` is **CONFIRMED** at multiple independent read sites this page disassembled:

```asm
; CoreForkPass::run — read N, branch on the single-core fast path
173e2dd:  8b b0 a4 01 00 00     mov    0x1a4(%rax),%esi      ; esi = N (lnc_size)
173e2e8:  83 fe 01              cmp    $0x1,%esi             ; N == 1 ? (single-core)
;            then groupModulesByCore(N, vector<Module>&) regroups the program by core.
```

```asm
; CoreV3GenImpl::visitInstGPSIMDSB2SB — the cross-core SB-swap encoder gate
1359a67:  83 b8 a4 01 00 00 02  cmpl   $0x2,0x1a4(%rax)      ; arch ctx (== cores-per-LNC) must be 2
1359a74:  41 c6 45 20 01        movb   $0x1,0x20(%r13)       ; cross-core enable byte = 1
; ... and the verify path at 0x1359da1 repeats cmpl $0x2,0x1a4(%rax).
```

### The `cores-per-LNC = 2` hard-coding

The `arch+0x1A4 == 2` immediate is not incidental: the **only** on-chip cross-core machine instruction in the ISA, the GPSIMD State-Buffer→State-Buffer copy (`InstGPSIMDSB2SB`), is gated on it. The encoder asserts `arch+0x1A4 == 2` at two sites (`@0x1359a67` GENERATE, `@0x1359da1` CHECK), the libBIR legality check `isCompatible` opens with `cmp rdi,0x2` (ctx must be 2), and the simulator asserts `getNumCoresPerLNC() == 2` with peer arithmetic `partner = (coreId + 1) & 1`. There is **no core-index field and no peer-count field anywhere** in that encoder, verifier, or simulator: the two-core topology is *structural*. A `4`-core LNC would fail the encoder assert outright — "the other core" is only unambiguous when there are exactly two. *(CONFIRMED — three `+0x1A4`/`==2` sites disassembled; the GPSIMD instruction itself is [1.12](gpsimd-engine.md).)*

> **Re-challenge of the `== 2` immediate (adversarial).** The `cmpl $0x2,0x1a4(%rax)` byte at `@0x1359a67` is read directly from the GPSIMD encoder — it is the *consumer's* assertion that the arch context equals 2, not the *definition* of `N`. The definition is `PassOptions+0x1A4`, and the GPSIMD `arch+0x1A4` is the per-arch mirror of the same cores-per-LNC value (both fields are at offset `+0x1A4` of their respective context objects, and the strings/symbol evidence ties them to one notion: `lnc_size` == `cores-per-LNC` == this GPSIMD context). So the immediate `2` is **CONFIRMED** as "this cross-core op is legal only in a 2-core LNC", and the *general* claim "`N` can be other values" is true for the framework (`lnc_size` is a variable, default 1) but the *only shipped non-trivial value with a legal cross-core op is 2*. A reimplementer should treat `N` as a parameter but note that this build's cross-core instruction encoding only expresses `N = 2`.

### Two "core" notions

Do not conflate them — both matter for memory:

- **Logical NeuronCore (LNC) index** `0..N-1` — the SPMD shard index the splitter stamps as the `NeuronCoreId` module attribute. This is the **compile-time** partition unit.
- **Physical core** — the runtime rank. `birsim::NeuronCoresManager` exposes both `getNumCoresPerLNC()` and `getNumPhysicalCores()` / `getRankIdforCore(core)`. At runtime the `N` logical cores map onto physical cores. SBUF and PSUM are **per-physical-core hardware** — each physical core has its own State Buffer and PSUM banks — and the compiler's per-LNC-core private SBUF/PSUM address space lands 1:1 on that hardware. *(CONFIRMED — the `NeuronCoresManager` method set is present: `getNumCoresPerLNC`, `getNumPhysicalCores`, `getRankIdforCore`, `getSharedDRAMforRank`, `doSyncPhysicalCores`.)*

"Each LNC core owns the full SBUF" is true *precisely because* each LNC core is realized on its own physical core whose SBUF is physically private.

---

## SBUF and PSUM are replicated

### The claim

After the split, each core owns the **full** State Buffer (`128 partitions × SB_SIZE`, where `SB_SIZE` is `96/192/224/256 KiB` for gen1/2/3/4 — [1.05](sbuf-psum-geometry.md)) and the **full** PSUM (`8 banks × 2048 B × 128 partitions`; gen1 is `4 banks × 64 partitions`). There is no `1/N` slicing. This rests on a positive (full-`SB_SIZE` read) and a negative (no shared-on-chip symbol), and both are verified on this build.

### Support 1 (positive): the allocator reads the full `SB_SIZE`, never `/N`

The SBUF allocator reads `SB_SIZE` through the standard `getArchModel → Board+0x8 → Device+0x10 → Core+0x28 (Statebuf) → +0` walk ([1.05](sbuf-psum-geometry.md)) and stores it verbatim into its per-core budget slot. There is **no division by `lnc_size`** anywhere in that chain:

```asm
; ColoringAllocatorWithLoop::Rep::SB_Allocator::SB_Allocator @0xa97b82
a97b82:  e8 ..                 call   getArchModel@plt      ; rbx = Board*
a97baa:  48 8b 43 08           mov    0x8(%rbx),%rax        ; Board+0x8  = Device*
a97bb3:  48 8b 40 10           mov    0x10(%rax),%rax       ; Device+0x10 = Core*
a97bb7:  48 8b 40 28           mov    0x28(%rax),%rax       ; Core+0x28  = Statebuf*
a97bbb:  8b 00                 mov    (%rax),%eax           ; SB_SIZE = Statebuf+0  (FULL)
a97bbd:  89 81 b8 02 00 00     mov    %eax,0x2b8(%rcx)      ; allocator+0x2b8 = SB_SIZE  (no /N)
a97bc3:  41 83 fe 0a           cmp    $0xa,%r14d            ; ArchLevel 10 (gen1) fork — NOT an N fork
```

The value stored at `allocator+0x2b8` is the **whole-core** `SB_SIZE`. The `cmp $0xa` immediately after is the Inferentia (gen1) byte-budget fork ([1.05](sbuf-psum-geometry.md)), **not** a per-core division. Because the allocator is constructed **per per-core Module/Function** (the post-split pre-codegen pipeline runs per core — [Part 8](../walrus/)), every core's allocator receives the full `SB_SIZE` budget. **No per-core fraction exists.** *(CONFIRMED — chain re-disassembled for this page; matches [1.05](sbuf-psum-geometry.md)'s read-chain byte-for-byte.)*

The same conclusion is reinforced two further ways:

- The SBUF tile-legality assertion in `.rodata` caps partition accesses at `ArchModel.device.core.statebuf.numPartitions` — the constant **128**, the same on every core — not at `128/N`. A per-core program may therefore legally touch **all 128** partitions, which is only possible if each core owns the full partition array. *(CONFIRMED — the assert string references `statebuf.numPartitions`; the constant is 128 per [1.05](sbuf-psum-geometry.md).)*
- `KlirToBirCodegen::getSbufMaxBytes()` `@0xf14420` and `getPsumMaxBankId()` `@0xf14430` are per-codegen-instance getters (return `codegen+0x118` and a PSUM ceiling). Codegen runs per core (per `Function`), so each core's codegen carries its **own** full SBUF byte budget and PSUM bank ceiling — no `N`-division. *(CONFIRMED — both symbols present as 2-instruction getters.)*

> **Disambiguation — the other partition asserts are intra-core alignment, not slicing.** Rules like *"Sunda PSUM accesses must start at a %32 partition"*, *"CoreV1 PSUM accesses must start at partition 0"*, *"Matmult inputs must start at SB Partition 0"* pin where an **engine** may begin inside **one core's full** SBUF/PSUM. They constrain access *within* a core and are unchanged by `N`. None of them slice the memory across cores. *(CONFIRMED — strings are intra-core engine alignment rules.)*

### Support 2 (negative): there is no shared on-chip memory

The "replicated, not shared" claim depends critically on a negative: **no core can name another core's SBUF or PSUM.** This page verified the negative directly. The entire shared/cross-core machinery is keyed to DRAM only — `bir::MemoryLocation::isSharedPostDRAMAlloc()` (note: *Post**DRAM**Alloc*), `coloring_allocator_dram_shared`, `getSharedDRAMforRank`, `getSharedMemLoc2MemObjMapforRank` — and an exhaustive symbol/string sweep of `libwalrus.so` **and** `libBIR.so` finds **no** symbol or string of the form `sharedSBUF`, `SBUFshared`, `sharedPSUM`, `PSUMshared`, `shared_sbuf`, `getSharedSBUFforRank`, or any `shared`-token co-located with `sbuf`/`statebuf`/`psum`:

```
$ nm -DC libwalrus.so | rg -i 'sharedSBUF|sharedPSUM|getSharedSBUF|getSharedPSUM|shared_sbuf'
  (no output)
$ nm -DC {libwalrus,libBIR}.so | rg -i shared | rg -i 'sbuf|statebuf|psum'
  ...AntiDependencyAnalyzer::populatePsumUseMaps(... std::shared_ptr<bir::PhysicalAccessPattern> ...)
```

The single hit is the C++ STL token `std::shared_ptr<bir::PhysicalAccessPattern>` inside an anti-dependency *PSUM use-map* builder — a smart-pointer type, **not** a shared-PSUM concept. *(CONFIRMED — verified on both binaries this build; the lone hit is `std::shared_ptr`.)*

> **Why a negative is sound here.** A negative ("symbol X does not exist") is weaker than a positive in general, but it is sound when (a) the *positive* counterpart for the other memory class **does** exist and is found (every `getSharedDRAMforRank` / `isSharedPostDRAMAlloc` symbol is present), and (b) the search is exhaustive over the naming space. Both hold: shared **DRAM** has a full, discoverable symbol family; shared **SBUF/PSUM** has none, across two independently-compiled binaries. The asymmetry is the evidence — the developers built cross-core sharing for exactly one memory class and named it after that class.

### Why "replicated" is the right word

Each core reads the **same** full `Core` geometry, allocates **independently** into its own full address space, and cannot reach any other core's on-chip memory. The replication is in the **data**, not the hardware: the logical tensor is grown into per-replica copies *before* the split (on the one symbolic module), so after the split each core's full-SBUF program operates on its **own slice** of that replicated data within its **own full** State Buffer. The SBUF hardware is never sub-partitioned. *(STRONG — the "binding selects this core's shard within its full SBUF" reading is a join of the per-core full-partition ceiling (CONFIRMED above) with the splitter's shard-id binding ([Part 8](../walrus/)); the exact grown-buffer byte layout is an `expand_replication` detail and is INFERRED — see [Part 8](../walrus/).)*

---

## DRAM is partitioned (and the one shared region)

### Three tiers

DRAM is the only memory with cross-core structure, and it has three tiers:

- **(i) Per-core private DRAM** — each core's own spills and intermediates, allocated bottom-up into that core's own HBM window. Not visible to other cores. PARTITIONED. *(CONFIRMED.)*
- **(ii) Shared cross-core DRAM** — one region every core addresses at the **same** physical address, marked by `bir::MemoryLocation::setIsSharedPostDRAMAlloc(true)`. SHARED. *(CONFIRMED — the marker symbol is present; it is the single source of truth for "this loc is one physical buffer for all `N` cores".)*
- **(iii) Input/output/weight DRAM** — pre-addressed host-tensor bindings, kept in place (whole-program, not per-core). *(CONFIRMED — separate from the per-core split.)*

### The per-core HBM window is the budget ceiling

Each core's total DRAM footprint is gated against its **per-core** HBM window = `Core::dramSizeGb << 30`, read through the same arch walk ([1.06](dram-hbm-geometry.md)):

```asm
; HBMUsage::run @0x16ba31f
16ba31f:  48 8b 43 08           mov    0x8(%rbx),%rax        ; Board+0x8  = Device*
16ba323:  48 8b 40 10           mov    0x10(%rax),%rax       ; Device+0x10 = Core*
16ba327:  8b 40 30              mov    0x30(%rax),%eax       ; Core+0x30  = dramSizeGb (GiB)
16ba32a:  48 c1 e0 1e           shl    $0x1e,%rax            ; << 30 → HBM limit in BYTES (per core)
```

The window is `16/16/24/36 GiB` for gen1/2/3/4 ([1.06](dram-hbm-geometry.md)). There is **no** `×N` or `/N`: the gate is **per-core**, against `Core::dramSizeGb`, not the whole-device HBM total. The per-core DRAM allocator's slab cap and CC-buffer limit are likewise per-core fields (`this+0x1e8 << 20` slab MB, `this+0x204 << 20` collective-compute buffer MB, default 500 MB), read directly off the allocator instance:

```asm
; DRAM select_impl @0xa7a860
a7a860:  49 63 87 e8 01 00 00  movslq 0x1e8(%r15),%rax       ; per-core slab cap (MB)
a7a86e:  48 c1 e0 14           shl    $0x14,%rax             ; << 20 → bytes
a7a867:  49 63 97 04 02 00 00  movslq 0x204(%r15),%rdx       ; CC/allreduce buffer (MB)
a7a87c:  48 c1 e2 14           shl    $0x14,%rdx             ; << 20 → bytes
```

*(CONFIRMED — `HBMUsage` and `select_impl` re-disassembled for this page; both match [1.06](dram-hbm-geometry.md).)*

### Address equalization (the shared region)

The shared region is the one place where two cores must agree on a physical address. That agreement is reached at **compile time**, before the linker runs: each core records, for a buffer it shares, a `RemoteLocalTarget {remote_name, coreIdx}`, and a sync pass copies the owning core's physical `{address, basePartition, bankId}` into every peer core's view of the buffer — so every core's copy of a shared DRAM tensor resolves to **one identical physical address**. The dual path places a shared tensor **once** (on the owning core) and gives other cores a remote pointer instead of a second allocation — the recovered log line *"Skipping shared tensor allocations on core 1, marking as remoteLocalTarget instead"* and *"…based on its remote local target"* are present in the binary. *(CONFIRMED — both strings verified this page; the `RemoteLocalTarget` and `setIsSharedPostDRAMAlloc` symbols are present.)*

The linker (Part 8) then unifies the per-core **names** for the shared region into one module-level `MemoryLocation` and adds producer↔consumer alias edges; it never re-allocates or merges SBUF/PSUM. **This page does not re-document those mechanisms** — the splitter, the per-core allocators, `sync_shared_allocations`, `bir_linker`, and the VNC cross-core link all live in [Part 8](../walrus/). The model fact is: **`N` private HBM windows + one address-equalized shared region.**

---

## Cross-core access: the single channel

When core *J* needs core *K*'s data, the bridge is **always** DRAM. SBUF and PSUM are never remotely addressed — a core physically cannot reach another core's on-chip memory (the negative above). The flow, model-level:

1. **Compile-time address agreement** — `RemoteLocalTarget` → address-equalization (same physical DRAM address on every core) → linker name-unification + alias edges. ([Part 8](../walrus/).)
2. **Ordering / race-freedom** — anti-dependency analysis over the freshly-placed shared region orders a use on one core against a def on another (they alias one physical buffer); shared-buffer lifetimes are extended so the region is not reused while a peer may still read it; `CoreBarrier` / semaphore objects emit the actual happens-before fences. ([Part 8](../walrus/).)
3. **Runtime realization** — only when `isLNC()`, `BirSim::runPhysicalCore` fetches `getSharedDRAMforRank(rank)` + `getSharedMemLoc2MemObjMapforRank(rank)` so every physical core maps its shared `MemoryLocation`s onto the **same** `MemoryObject`s; a sync provides the barrier. Critically, **only shared DRAM is mapped per-rank** — there is no per-rank shared SBUF/PSUM fetch, confirming on-chip memory is private hardware per physical core:

```asm
; BirSim::runPhysicalCore — cross-core mapping is DRAM-only
110b82c:  e8 ..  call  birsim::NeuronCoresManager::isLNC()
110b84d:  e8 ..  call  birsim::NeuronCoresManager::getSharedDRAMforRank(unsigned long)
110b860:  e8 ..  call  birsim::NeuronCoresManager::getSharedMemLoc2MemObjMapforRank(unsigned long)
```

*(CONFIRMED — the `isLNC → getSharedDRAMforRank → getSharedMemLoc2MemObjMapforRank` sequence re-disassembled this page; there is no analogous `getSharedSBUFforRank`.)*

### The one on-chip exception that is not a counterexample

There is exactly one machine instruction that copies on-chip memory **between** cores without going through DRAM: the GPSIMD State-Buffer→State-Buffer copy, `InstGPSIMDSB2SB` ([1.12](gpsimd-engine.md)). It is **not** a shared address space — it is a **peer-to-peer point copy**: one bundle reads a tile from this core's SBUF and writes the peer core's SBUF, addressing the peer by a *single scalar SB partition address* plus a one-bit "go cross-core" enable, with **no shared mapping and no core index**. The instruction is gated on `arch+0x1A4 == 2` (the cores-per-LNC == 2 hard-coding above) and is a narrow small-transfer fast path; anything larger or shape-incompatible falls back to DMA, and reductions/all-reduce are built from DMA + barriers. It moves data; it does not make any SBUF address visible to another core's allocator or program. The model — "on-chip memory is private; cross-core data exchange is through DRAM" — stands; `InstGPSIMDSB2SB` is a copy *between* two private SBUFs, not a window *into* a peer's SBUF. *(CONFIRMED — the instruction's cross-core addressing has no shared mapping; see [1.12](gpsimd-engine.md).)*

---

## The per-core memory map

Consolidated, for an `N`-way LNC (e.g. `N = 2` on Trn2/`sunda`), per logical core `c ∈ {0..N-1}` realized on its own physical core:

| Memory | Model | Geometry each core sees | Cross-core? |
|---|---|---|---|
| **SBUF** | REPLICATED (private full) | full `128 parts × SB_SIZE` (`96/192/224/256 KiB`, gen1/2/3/4) | NO — never shared |
| **PSUM** | REPLICATED (private full) | full `8 banks × 2048 B × 128 parts` (gen1: `4 banks × 64 parts`) | NO — never shared |
| **DRAM** | PARTITIONED + one shared | per-core HBM window = `Core::dramSizeGb` GiB (`16/16/24/36`) | YES — one address-equalized shared region |

The whole-program tiers (I/O/weight DRAM, the collective-compute buffer) sit outside the per-core split and are documented with the linker in [Part 8](../walrus/).

---

## Confidence ledger

| Claim | Tag | Basis (this page) |
|---|---|---|
| SBUF allocator reads full `SB_SIZE`, no `/N` | CONFIRMED | `SB_Allocator` ctor `@0xa97b82`–`@0xa97bc3` re-disassembled |
| No `sharedSBUF`/`sharedPSUM` symbol or string | CONFIRMED | exhaustive `nm`/`strings` sweep of `libwalrus.so` + `libBIR.so` (lone hit = `std::shared_ptr`) |
| Per-core HBM window = `Core+0x30 << 30`, no `×N` | CONFIRMED | `HBMUsage::run` `@0x16ba31f` re-disassembled |
| Per-core DRAM slab/CC caps are per-instance | CONFIRMED | `select_impl` `@0xa7a860` (`this+0x1e8`/`+0x204 << 20`) |
| `N` read from `PassOptions+0x1A4` | CONFIRMED | `CoreForkPass` `@0x173e2dd` (`mov 0x1a4(%rax),%esi`) |
| GPSIMD cross-core op gated on `arch+0x1A4 == 2` | CONFIRMED | encoder `@0x1359a67`/`@0x1359da1` (`cmpl $0x2,0x1a4(%rax)`) |
| Runtime cross-core mapping is DRAM-only | CONFIRMED | `runPhysicalCore` `@0x110b82c`–`@0x110b860` (no per-rank SBUF/PSUM fetch) |
| Shared-DRAM machinery present (`isSharedPostDRAMAlloc`, `getSharedDRAMforRank`, `RemoteLocalTarget`) | CONFIRMED | symbols + `mark_remote` strings verified |
| `NeuronCoresManager` method set (logical/physical core mapping) | CONFIRMED | `getNumCoresPerLNC`/`getNumPhysicalCores`/`getRankIdforCore` present |
| Front-end `logical_nc_config == backend lnc_size` is one `N` | STRONG | three-layer plumbing; CLI→`+0x1A4` not traced through one symbol here |
| Each core's bound access selects its shard within its full SBUF | STRONG | join of full-partition ceiling (CONFIRMED) + shard-id binding ([Part 8](../walrus/)) |
| Exact grown replicated-buffer byte layout | INFERRED | `expand_replication` detail; [Part 8](../walrus/) |

---

## Cross-References

- [1.05 SBUF / PSUM Bank Geometry](sbuf-psum-geometry.md) — the per-core SBUF/PSUM geometry every core reads in full: `SB_SIZE`, the 128-partition layout, the 8-bank PSUM, and the `getArchModel → Core+0x28/0x20` read chain this page shows is *not* divided by `N`.
- [1.06 DRAM / HBM Geometry](dram-hbm-geometry.md) — the off-chip side: `Core::dramSizeGb` (the per-core HBM window this model partitions across cores) and the `HBMUsage` budget gate.
- [1.12 GPSIMD Engine](gpsimd-engine.md) — `InstGPSIMDSB2SB`, the one on-chip cross-core copy, its `arch+0x1A4 == 2` gate, and why it is a peer-to-peer move, not a shared address space.
- [1.01 The arch Object Model](arch-object-model.md) — the `getArchModel → Board/Device/Core` walk that every geometry read on this page traverses, and the `Core+0x30 = dramSizeGb` field.
- [Part 8 — The libwalrus Backend](../walrus/) — the **mechanisms** this page only points at: `lnc_splitter` (clone ×`N` + concretize), the per-core SBUF/PSUM/DRAM allocators, `sync_shared_allocations` (address-equalize the shared region), `bir_linker` (merge `N` per-core Modules → one), and the VNC cross-core link.
