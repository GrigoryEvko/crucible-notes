# LocalDmaBandwidth

> *Every value, offset, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. The per-gen bandwidth cells were recovered by decoding the IEEE-754 immediate each accessor returns; the consumer formulas were recovered from the decompiled bodies plus `.rodata` immediate decode. Other versions differ. All addresses are virtual addresses; for this binary `.text` VMA == file offset (`0xe63c000`) and `.rodata` VMA == file offset (`0x84a0000`). Itanium-ABI note: an object's vptr is `"vtable for X" + 0x10`, so a virtual call `*(vptr+N)` lands at slot `N`.*

## Abstract

`Target::LocalDmaBandwidth(MemorySpace src, MemorySpace dst)` is the cost model's **on-chip DMA bandwidth matrix**: a per-generation table of GB/s figures, one cell per `(source memory space, destination memory space)` pair across HBM, VMEM, CMEM, SMEM (and a single SPMEM→HBM entry). It is *not* the per-byte cycle rate the conv/fusion operand DMA uses — that path goes through `GetBytesPerCycle` and the chip-geometry HBM bytes-per-second (see [`memory-bandwidth-latency-model`](memory-bandwidth-latency-model.md)). Instead, `LocalDmaBandwidth` is the **async-vs-synchronous copy comparator**: the only consumers are the three copy-strategy deciders, which ask "is a local DMA (e.g. a VMEM→VMEM rotate) faster than pushing the same bytes over ICI?" and choose the async local copy iff the ICI ceiling beats the local-DMA estimate.

A reader who knows LLVM should map this to a `TargetTransformInfo` cost hook that returns a *relative* throughput figure for a memory-space-to-memory-space copy, consulted by a transform to decide between two lowerings — not a cycle count fed into the schedule. The matrix lives in the `Target` vtable immediately after the two ICI-rate accessors (`+0x188 ICIPerLinkDataRate`, `+0x190 ICIIngressEgressDataRate`, then `+0x198…+0x208` the LocalDmaBandwidth cells, `+0x210 SpmemToHbm`), and `LocalDmaBandwidth` itself is a pure `(src,dst)→slot` router that tail-calls the matching accessor or returns the empty optional on no match.

This page documents the matrix end to end: the dispatch router and the `(src,dst)→slot` map, the full per-gen GB/s values transcribed from each accessor (Dragonfish, Pufferfish, Viperfish std/lite, Ghostlite — base `Target` returns `0`), the async-copy consumers (`UseAsyncDataCopy`, `ShouldUseAsyncLocalCopy`, the SparseCore AllGather strategy) and their `min(ICIIngressEgress, 2·ICIPerLink)` ICI ceiling, and — for contrast — the **separate** per-byte cycle pricer (`GetBytesPerCycle`/`WindowCycles`/`DefaultHbmInitLatency`) that the operand-DMA scheduling entry actually consumes, with its per-gen `InitialDmaLatencyInNs` startup constants.

For reimplementation, the contract is:

- The dispatch router `LocalDmaBandwidth(src,dst)` — its `(src,dst)→vtable-slot` map, the `MemorySpace` enum `{HBM=1, VMEM=3, CMEM=4, SMEM=5}`, and the empty-optional miss return.
- The full per-gen GB/s matrix (transcribed below) and the Viperfish `variant_name == "lite"` (`0x6574696c`) std/lite split.
- The async-copy consumer: `local_cost = LocalDmaBandwidth(src,dst) · (n−1)` vs `ici_ceiling = min(ICIIngressEgress, 2·ICIPerLink)`; async iff `ici_ceiling ≤ local_cost`.
- The contrast with the per-byte MemXfer pricer: `bytes_per_cycle = HbmFullChipBytesPerSecond / (TC_freq_MHz·1e6) / CoresPerChip`, `WindowCycles = transfer_bytes / bytes_per_cycle + init`, `DefaultHbmInitLatency = InitialDmaLatencyInNs · (TC_freq_MHz/1000)` cycles — and the per-gen `InitialDmaLatencyInNs` (240 / {555,50} / 1200,0 ns).

| | |
|---|---|
| **Dispatch router** | `Target::LocalDmaBandwidth(MemorySpace,MemorySpace)` `@0x1d6168e0` |
| **MemorySpace enum** | `HBM=1`, `VMEM=3`, `CMEM=4`, `SMEM=5` (SPMEM via its own slot) |
| **Matrix vtable range** | `+0x198` (HbmToHbm) … `+0x208` (SmemToSmem); `+0x210` SpmemToHbm tail |
| **Base default** | `Target::LocalDmaBandwidth*` accessors `@0x1d48fa00…` all return `0` |
| **Consumer (real)** | `(anon)::UseAsyncDataCopy` `@0x1380a480`; `ShouldUseAsyncLocalCopy` `@0x133eff40` |
| **ICI ceiling** | `min(ICIIngressEgress[+0x190], 2·ICIPerLink[+0x188])` |
| **Per-byte pricer (separate)** | `fusion_util::GetBytesPerCycle` `@0x1454dd00` → `WindowCycles` `@0x14552660` |
| **Scheduling entry** | `cost_model_util::RecordMemXferCyclesImpl` `@0x13844e80` (R9/R10, R11/R12) |

---

## The Dispatch Router — `LocalDmaBandwidth`

### Purpose

`LocalDmaBandwidth(src, dst)` `@0x1d6168e0` is a pure router: given two `MemorySpace` enum values it computes a vtable byte-slot and tail-calls the per-pair accessor (`LocalDmaBandwidthHbmToHbm`, …). On no recognised pair it returns the empty optional `{value=0, has_value=0}`. The router holds no values itself; the GB/s figures live in the per-gen accessor leaves the router dispatches into.

### Algorithm

```c
function LocalDmaBandwidth(Target* this, uint8 src, uint8 dst):   // @0x1d6168e0
    // The decompiler renders the slot as a guarded cascade of XOR tests
    // (src^1 == HBM, src^3 == VMEM, src^4 == CMEM, src^5 == SMEM); the first
    // matching (src,dst) pair leaves `slot` at the table value below.
    slot = (src,dst) -> {
        HBM ->HBM  : 0x198,  HBM ->VMEM : 0x1a0,  HBM ->SMEM : 0x1a8,
        VMEM->HBM  : 0x1b0,  VMEM->VMEM : 0x1b8,  VMEM->CMEM : 0x1c0,  VMEM->SMEM : 0x1c8,
        CMEM->HBM  : 0x1d0,  CMEM->VMEM : 0x1d8,  CMEM->CMEM : 0x1e0,  CMEM->SMEM : 0x1e8,
        SMEM->HBM  : 0x1f0,  SMEM->VMEM : 0x1f8,  SMEM->CMEM : 0x200,  SMEM->SMEM : 0x208,
        else       : MISS
    }
    if slot == MISS: return optional{ value=0, has_value=false }
    return (*(vtable + slot))(this)                                // tail-call the accessor
```

The `MemorySpace` enum is recovered from the XOR comparands: `HBM=1`, `VMEM=3`, `CMEM=4`, `SMEM=5`. SPMEM is not in this router; the single `LocalDmaBandwidthSpmemToHbm` accessor sits at vtable `+0x210` (immediately after the `SMEM->SMEM` cell at `+0x208`) and is reached directly (the SparseCore path), not through this `(src,dst)` dispatch.

> **NOTE —** the two ICI-rate accessors are the immediate neighbours of the matrix in the `Target` vtable: `+0x188 = ICIPerLinkDataRate`, `+0x190 = ICIIngressEgressDataRate`, then the LocalDmaBandwidth cells from `+0x198` (`HBM->HBM`) through `+0x208` (`SMEM->SMEM`), with `SpmemToHbm` at `+0x210`. This adjacency is not cosmetic — the same consumer (`UseAsyncDataCopy`) reads both the LocalDmaBandwidth cell and the two ICI rates to make one decision, so the compiler laid them out contiguously.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `Target::LocalDmaBandwidth(MemorySpace,MemorySpace)` | `0x1d6168e0` | `(src,dst)→slot` router; miss → `{0,false}` | CERTAIN |
| `Target::LocalDmaBandwidthHbmToHbm` (base) | `0x1d48fa00` | base default — returns `0` (each gen overrides) | CERTAIN |
| `Target::ICIPerLinkDataRate` (vtable `+0x188`) | per-gen | per-SerDes-link ICI rate (consumer ceiling term) | CERTAIN |
| `Target::ICIIngressEgressDataRate` (vtable `+0x190`) | per-gen | full bidirectional chip ICI aggregate | CERTAIN |

---

## The Per-Gen Bandwidth Matrix

Each accessor returns a single IEEE-754 `double` (GB/s). The base `Target` accessors (`@0x1d48fa00…`) all `return 0` — the matrix is meaningless until a derived per-gen `Target` overrides the vtable slots. The table below transcribes the decoded immediate from every per-gen accessor. Every cell was confirmed by decoding the accessor's returned bit-pattern.

### Ghostlite (v6e) — `@0x1d4973c0 …`

| (src→dst) | Accessor `@addr` | bit-pattern | GB/s | Confidence |
|---|---|---|---|---|
| HBM→HBM   | `0x1d4973c0` | `0x4050000000000000` | 64   | CERTAIN |
| HBM→VMEM  | `0x1d4973e0` | `0x4094140000000000` | 1285 | CERTAIN |
| HBM→SMEM  | `0x1d497400` | `0x404B800000000000` | 55   | CERTAIN |
| VMEM→HBM  | `0x1d497420` | `0x4096600000000000` | 1432 | CERTAIN |
| VMEM→VMEM | `0x1d497440` | `0x4050000000000000` | 64   | CERTAIN |
| VMEM→SMEM | `0x1d497460` | `0x404B800000000000` | 55   | CERTAIN |
| SMEM→HBM  | `0x1d497480` | `0x404B800000000000` | 55   | CERTAIN |
| SMEM→VMEM | `0x1d4974a0` | `0x404B800000000000` | 55   | CERTAIN |
| SMEM→SMEM | `0x1d4974c0` | `0x403C000000000000` | 28   | CERTAIN |
| SPMEM→HBM | `0x1d4974e0` | `0x4082600000000000` | 588  | CERTAIN |

### Viperfish (v5p std / v5e lite) — `@0x1d49a320 …`

Every Viperfish accessor branches on the variant string: it reads the libc++ `std::string variant_name` SSO flag byte at `this+951` (`+0x3b7`), its length (SSO inline or `this+0x3a8` heap length), and compares the 4-byte payload at `this+0x3a0` against `0x6574696c` (`"lite"`, little-endian). A 4-char `"lite"` match returns the **lite (v5e)** value; any other string falls through to the **std (v5p)** value.

| (src→dst) | Accessor `@addr` | std (v5p) | lite (v5e) | Confidence |
|---|---|---:|---:|---|
| HBM→HBM   | `0x1d49a320` | 72   | 308 | CERTAIN |
| HBM→VMEM  | `0x1d49a380` | 1198 | 822 | CERTAIN |
| HBM→SMEM  | `0x1d49a3e0` | 55   | 56  | CERTAIN |
| VMEM→HBM  | `0x1d49a440` | 1224 | 828 | CERTAIN |
| VMEM→VMEM | `0x1d49a4a0` | 72   | 827 | CERTAIN |
| VMEM→SMEM | `0x1d49a500` | 55   | 56  | CERTAIN |
| SMEM→HBM  | `0x1d49a560` | 55   | 56  | CERTAIN |
| SMEM→VMEM | `0x1d49a5c0` | 55   | 56  | CERTAIN |
| SMEM→SMEM | `0x1d49a620` | 28 (both) | 28 (both) | CERTAIN |
| SPMEM→HBM | `0x1d49a640` | 587.4 (both) | 587.4 (both) | CERTAIN |

> **GOTCHA —** the variant split is computed *inline in every accessor*, not via a numeric variant index. The single-TC v5e die uniformly reports *higher* intra-die loopback bandwidth (VMEM→VMEM 827 vs the std-die 72) because a lite part has no second core to contend for the local DMA fabric; conversely it reports *lower* HBM-bound bandwidth (HBM→VMEM 822 vs 1198) reflecting its halved HBM stack. A reimplementation that reads one variant's cells for both will misprice the async-copy decision on the other.

### Pufferfish (v4) — `@0x1d494340 …`

Pufferfish is the only gen with first-class CMEM, so its matrix is the widest (15 cells).

| (src→dst) | Accessor `@addr` | bit-pattern | GB/s | Confidence |
|---|---|---|---:|---|
| HBM→HBM   | `0x1d494340` | `0x407E000000000000` | 480  | CERTAIN |
| HBM→VMEM  | `0x1d494360` | `0x407E100000000000` | 481  | CERTAIN |
| HBM→SMEM  | `0x1d494380` | `0x4041000000000000` | 34   | CERTAIN |
| VMEM→HBM  | `0x1d4943a0` | `0x40915C0000000000` | 1111 | CERTAIN |
| VMEM→VMEM | `0x1d4943c0` | `0x4081000000000000` | 544  | CERTAIN |
| VMEM→CMEM | `0x1d4943e0` | `0x4091840000000000` | 1121 | CERTAIN |
| VMEM→SMEM | `0x1d494400` | `0x4041000000000000` | 34   | CERTAIN |
| CMEM→HBM  | `0x1d494420` | `0x4090E00000000000` | 1080 | CERTAIN |
| CMEM→VMEM | `0x1d494440` | `0x40A2460000000000` | 2339 | CERTAIN |
| CMEM→CMEM | `0x1d494460` | `0x4092A40000000000` | 1193 | CERTAIN |
| CMEM→SMEM | `0x1d494480` | `0x4041000000000000` | 34   | CERTAIN |
| SMEM→HBM  | `0x1d4944a0` | `0x4041000000000000` | 34   | CERTAIN |
| SMEM→VMEM | `0x1d4944c0` | `0x4041000000000000` | 34   | CERTAIN |
| SMEM→CMEM | `0x1d4944e0` | `0x4041000000000000` | 34   | CERTAIN |
| SMEM→SMEM | `0x1d494500` | `0x4031000000000000` | 17   | CERTAIN |

### Dragonfish (v3) and base Target

Dragonfish overrides only two cells; every other slot falls through to the base `Target` accessor (which returns `0`). Jellyfish (v2) supplies no `LocalDmaBandwidth*` overrides at all in this build — its async-copy path never queries the matrix.

| Gen | (src→dst) | Accessor `@addr` | GB/s | Confidence |
|---|---|---|---:|---|
| Dragonfish (v3) | HBM→VMEM | `0x1d48fa20` | 423 | CERTAIN |
| Dragonfish (v3) | VMEM→HBM | `0x1d48fa60` | 423 | CERTAIN |
| base `Target` | all cells | `0x1d48fa00…0x1d48fbe0` | 0 | CERTAIN |

> **QUIRK —** the SMEM cells cluster on a small set of repeated constants (Pufferfish `34` for every SMEM-touching pair except SMEM→SMEM = `17`; Ghostlite `55` for every SMEM pair except SMEM→SMEM = `28`; Viperfish std `55` / lite `56`). SMEM (the scalar register window) is a tiny, slow memory; the cost model treats every transfer that touches it as a single low fixed rate rather than modelling the source/destination pairing. Only the HBM/VMEM/CMEM cells carry per-pair-distinct figures.

---

## The Real Consumer — Async-vs-ICI Copy Decision

### Purpose

The matrix has exactly three callers, all copy-strategy deciders: `(anon)::UseAsyncDataCopy` `@0x1380a480`, `ShouldUseAsyncLocalCopy` `@0x133eff40`, and the SparseCore AllGather strategy `TcStyleSinglePhaseAGTransferStrategy::ComputeUseAsyncLocalCopy` `@0x1335f2e0`. None of them feeds a cycle count into the schedule; each compares the local-DMA bandwidth against an ICI ceiling and returns a boolean — "use an async local copy" vs "go over ICI".

### Algorithm

```c
function UseAsyncDataCopy(n_elems, …, Target& t, fn src_of, fn dst_of):   // @0x1380a480
    if early_flags: return 0
    if n_elems <= 0: return 1                            // trivial copy is always async-OK
    src = src_of(0); dst = dst_of(0)
    if not t.SupportsDmaMode(src, dst, …)[vtable+0x2a0]: return 0   // gen must allow the DMA
    if not (t.ICIPerLinkDataRate[+0x188] && t.ICIIngressEgressDataRate[+0x190]):
        return 0
    local_bw   = t.LocalDmaBandwidth(src, dst)           // GB/s for this (src,dst) pair
    local_cost = local_bw · n_elems                      // (var_38 = n_elems as double)
    ici_ceiling = min( t.ICIIngressEgressDataRate,       // r13
                       2 · t.ICIPerLinkDataRate )        // 2·rbx
    // async iff the ICI ceiling does NOT beat the local-DMA estimate
    return (ici_ceiling <= local_cost)                   // vucomisd @0x1380a5ad
```

The comparison is a single `vucomisd ici_ceiling, local_cost`: the function chooses the async local copy when the ICI ceiling is at or below the local-DMA bandwidth (scaled by element count), i.e. when a local DMA is competitive. `SupportsDmaMode` (vtable `+0x2a0`) gates the whole decision — a gen that does not support the requested `(src,dst)` DMA mode falls back to the synchronous/ICI path immediately.

> **NOTE —** `ICIPerLinkDataRate` is the bandwidth of one SerDes link; `ICIIngressEgressDataRate` is the full bidirectional chip aggregate. The ceiling `min(IngressEgress, 2·PerLink)` models the two regimes: a transfer over a single pair of links is bounded by `2·PerLink` (one in, one out), and a transfer that saturates all links is bounded by the chip-wide IngressEgress aggregate. The decision is therefore "is a local on-chip DMA faster than the better of single-link-pair and all-links ICI?"

### The Two Bandwidth Sources — do not conflate

This is the single most important structural fact about `LocalDmaBandwidth`: it is **not** the bandwidth the operand-DMA scheduler prices against. The cost model has two distinct on-chip bandwidth sources with different consumers and different units:

| Source | Stored at | Consumer | Units | Used for |
|---|---|---|---|---|
| `LocalDmaBandwidth(src,dst)` matrix | per-gen vtable `+0x198…` | `UseAsyncDataCopy` / `ShouldUseAsyncLocalCopy` / SparseCore AG | GB/s (relative) | async-vs-ICI **copy-strategy** decision |
| `HbmFullChipBytesPerSecond` (chip_parts) | `Target+0x4f0` | `GetBytesPerCycle` → `WindowCycles` | bytes/sec → bytes/cycle | per-byte operand-DMA **cycle deposit** (R10/R12) |

A reimplementation that routes operand-DMA cycle pricing through the `LocalDmaBandwidth` matrix will be wrong: the matrix is a comparator heuristic, the per-byte cost uses the chip-geometry HBM bytes-per-second. The next section documents that second source for contrast and to anchor the scheduling entry.

---

## The Per-Byte Pricer (separate path) — `GetBytesPerCycle` / `WindowCycles`

### Purpose

The operand-DMA *cycle* cost — the value that lands in the scheduler's `ResourceVector` — is computed by the MemXfer pricer, which never touches the `LocalDmaBandwidth` matrix. `GetBytesPerCycle` derives a per-core, per-cycle byte budget from chip geometry; `WindowCycles` divides the transferred bytes by it; `DefaultHbmInitLatency` adds a once-paid DMA startup. The byte count itself comes from the [`WindowDescription`](window-description-cost.md) byte model.

### Algorithm

```c
function GetBytesPerCycle(HloInstruction* inst, Target& t, MemorySpace ms):  // @0x1454dd00
    CHECK(ms == HBM || ms == CMEM)                       // else Fatal (fusion_util.cc:2354)
    freq_hz = t.TensorCoreFrequencyInMegaHertz() · 1e6   // const @0xa2e0208 = 1,000,000.0
    bw_Bps  = (ms == HBM) ? t.HbmFullChipBytesPerSecond()   // Target+0x4f0  (= *(this+158))
                          : t.CmemFullChipBytesPerSecond()  // Target+0x4f8  (= *(this+159))
    bpc = bw_Bps / freq_hz / t.CoresPerChip(coreType)    // BYTES PER TENSORCORE CYCLE (per core)
    // HBM-only override: a positive TpuCompEnv[+0x1040] or BackendConfig field-14
    //   value REPLACES bpc (flag-driven; default uses the computed value).
    return bpc

function WindowCycles(MemUnit, wd, Target& t, bytes_per_cycle, …, c):       // @0x14552660
    if MemUnit == t.MemUnitFromKiB(0): return 0.0        // sentinel — no transfer
    init = (c != -1) ? (t.TensorCoreFrequencyInMegaHertz()/1000.0)·c
                     : DefaultHbmInitLatency(MemUnit, wd, t)
    count_desc = (t.vtable[+0x590]() == 1)               // per-gen count-descriptors predicate
    base = WindowCyclesGenericTargetAgnostic(MemUnit, wd, count_desc, bytes_per_cycle) + init
    if t[+0x398] >= 5:                                   // v5p+ (Target+0x398 = TpuVersion)
        // two-direction {HBM, VMEM} blend: divide the {dir0,dir1} byte pair by 1000.0
        //   (xmmword @0xa2ce650 = [1000.0, 1000.0]), multiply by TC freq, take per-dir MAX,
        //   gated by a percentage > 0.01 test (xmmword @0xa2d8020 = [0.01, 0.01])
        return max(dir0_cycles, dir1_cycles) + init      // @0x1455282e
    else:
        return max(dir0_cycles, dir1_cycles) + init      // simple add path (@0x14552888)

function DefaultHbmInitLatency(MemUnit, wd, Target& t):                       // @0x14552ca0
    elem = (BYTE[*(wd+0x20) + 0xb] >> 2) & 0x1f          // operand-shape element type
    init_ns = t.vtable[+0x20].InitialDmaLatencyInNs(MemUnit, window_bytes)
    return init_ns · (t.TensorCoreFrequencyInMegaHertz() / 1000.0)   // ns · GHz = cycles
                                                          // const @0xa2e0430 = 1000.0
```

`GetBytesPerCycle` is the units anchor: `LocalDma`/HBM bandwidth *in the cycle pricer* is **bytes per TensorCore cycle, per core**, derived as `(bytes/sec) ÷ (TC_freq_MHz·1e6 cycles/sec) ÷ cores_per_chip`. The clock is the TensorCore clock (`Target+0x90c`), not a separate DMA clock. `HbmFullChipBytesPerSecond` is `*(this+158) == this+0x4f0`; `CmemFullChipBytesPerSecond` is `*(this+159) == this+0x4f8` — both folded from the chip_parts `bytes_per_second × stack_count` at `Target::Init`.

`WindowCycles`' v5p+ branch (`Target+0x398 >= 5`) blends two transfer directions: it loads the `{HBM, VMEM}` fragment-byte pair, divides each by `1000.0`, multiplies by TC frequency, gates each on a `> 0.01` percentage test, and takes the per-direction maximum. The VLOG-6 trace at `fusion_util.cc:3474` names the lanes explicitly — `hbm_percentage`, `vmem_percentage`, `hbm_bytes`, `vmem_bytes`, `hbmbw`, `vmembw`, `hbmlatency`, `vmemlatency`, `next_gen` — confirming the v5p+ model is a two-lane (HBM vs VMEM) max, not a single transfer.

### Per-Gen `InitialDmaLatencyInNs` (vtable `+0x20`)

The once-paid DMA startup latency, in nanoseconds, that `DefaultHbmInitLatency` scales to cycles. Each per-gen accessor was decoded byte-exact; the `.rodata` immediates were decoded directly from the `.so`.

| Gen | Accessor `@addr` | rule | non-special | special | Confidence |
|---|---|---|---:|---:|---|
| v2 Jellyfish | `0x1d48f3a0` | constant for all spaces (`@0xa2de6a8`) | 240 ns | — | CERTAIN |
| v4 Pufferfish | `0x1d493d00` | `table[ms==CMEM]` (`@0xa2dcd40`) | 555 ns (HBM) | 50 ns (CMEM) | CERTAIN |
| v5p/v5e Viperfish | `0x1d499ca0` | `(ms==VMEM) ? 0 : 1200` (`@0xa2df9f8`) | 1200 ns | 0 (VMEM) | CERTAIN |
| v6e Ghostlite | `0x1d496dc0` | `(ms==VMEM) ? 0 : 1200` (`@0xa2df9f8`) | 1200 ns | 0 (VMEM) | CERTAIN |
| base `Target` | `0x1d61c880` | `LogMessageFatal("Unimplemented")` — every gen overrides | — | — | CERTAIN |

> **GOTCHA —** v5p+/v6e charge **zero** DMA startup latency for VMEM transfers (`ms == 3` returns `0.0`) and the full 1200 ns only for HBM/SMEM/CMEM. The decompiled test is literally `if (ms != 3) return 1200ns;` — VMEM (MemorySpace `3`) is the cheap, latency-free path. Dragonfish (v3) inherits Jellyfish's flat 240 ns; v3-specific per-space values were not separately overridden in this build.

---

## The Scheduling Entry — `RecordMemXferCyclesImpl`

### Purpose

`RecordMemXferCyclesImpl` `@0x13844e80` is where the per-byte pricer runs and the two cycle values land in the `ResourceVector`. It is the *only* scheduling consumer of `WindowCycles`/`DefaultHbmInitLatency`; the `LocalDmaBandwidth` matrix is never reached from here. It is called once per priced operand: the startup latency goes into one resource lane, the bandwidth into another.

### Algorithm

```c
function RecordMemXferCyclesImpl(label, latency_res, bw_res, hlo, wd, CycleTable, rv, …):
    if not is_priced_memory_space(wd.shape):  return     // element-type gate (bittest 0x2fff91ffe)
    raw_bytes      = windowing_util::Size(wd.strides, MemUnit, ChunkGranules)   // @0x1c86f320
    transfer_bytes = raw_bytes / (Compact2ndMinorRatio · ElementPackingFactor)
    if rv[latency_res] == 0.0:                            // startup billed once per lane
        rv.Acc(latency_res, DefaultHbmInitLatency(transfer_bytes, MemUnit, wd, Target))
    bpc = GetBytesPerCycle(hlo, Target, MemUnit)          // @0x1454dd00
    rv.Acc(bw_res, WindowCycles(transfer_bytes, MemUnit, wd, Target, bpc, 0, 0, -1))
```

`RecordInputMemXferCycles` `@0x13845580` passes `(latency=R9, bw=R10)`; `RecordOutputMemXferCycles` `@0x13845860` passes `(latency=R11, bw=R12)` — the input/output split documented in [`memory-bandwidth-latency-model`](memory-bandwidth-latency-model.md) and routed by [`resource-enum`](resource-enum.md). The latency deposit is guarded by `rv[latency_res] == 0.0` so a sequence of transfers into the same lane pays one DMA startup, not N; the bandwidth deposit always accumulates.

### Worked Example — an HBM→VMEM operand input-DMA on v6e Ghostlite

```text
TC_freq           = 1750 MHz            (Target+0x90c; v6e CONFIRMED, v7x = 1900)
HbmFullChipBytesPerSecond = 1.638e12 B/s (Target+0x4f0, from chip_parts × stack count)
CoresPerChip      = 1
bytes_per_cycle   = 1.638e12 / (1750·1e6) / 1  ≈  936 B/cycle
R10 bandwidth     = window_bytes / 936          cycles
R9  latency       = 1200 ns · (1750/1000) GHz = 2100 cycles   (paid once)
```

So an HBM operand read costs a ~2100-cycle startup plus `bytes/936` bandwidth cycles, deposited into the MemXfer R9/R10 lanes and then bundle-reduced (`MaxResourceCycles`). Note this example exercises the *chip-geometry* HBM bandwidth (936 B/cycle), **not** the Ghostlite `LocalDmaBandwidth` HBM→VMEM cell (1285 GB/s) — that cell is consulted only when deciding whether to issue this copy asynchronously.

---

## Related Components

| Name | Relationship |
|---|---|
| `window-description-cost` | the `WindowDescription` byte model `Size`/`WindowCycles` feed; the byte→cycle core in detail |
| `memory-bandwidth-latency-model` | the R9/R10/R11/R12 lane routing and `bytes_per_cycle` geometry |
| `resource-enum` | the `ResourceVector` lanes the latency/bandwidth deposits accumulate into |
| `convolution-cost-state` | `RecordConvKernelCycles` — consumes the operand-DMA MemXfer deposit |
| `reduce-window-pooling-cost` | reuses the operand-window MemXfer pricing for pooling tiles |
| `gethloresources-routing` | the element-type gate and per-op resource dispatch above this leaf |
| `../ici/overview` | the ICI rate model the async-copy ceiling compares against |
| `../memory/vmem-allocator` | the VMEM space whose local DMA bandwidth the matrix prices |

---

## Cross-References

- [WindowDescription Byte-Cost](window-description-cost.md) — the byte model and the `WindowCycles` byte→cycle core the per-byte pricer shares
- [Memory Bandwidth / Latency Model](memory-bandwidth-latency-model.md) — the R9/R10/R11/R12 lane split and `bytes_per_cycle = HBM B/s / (TC_MHz·1e6) / cores`
- [Resource Enum (23-slot)](resource-enum.md) — the `ResourceVector` the MemXfer latency and bandwidth deposits accumulate into
- [ConvolutionCostState](convolution-cost-state.md) — `RecordConvKernelCycles`: the operand window DMA consumer
- [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md) — pooling reuses this operand-window MemXfer pricing
- [GetHloResources Routing](gethloresources-routing.md) — the element-type gate and per-op resource dispatch
- [Cost Model Overview](overview.md) — the three-family per-gen cost-model architecture and the TC clock wiring
- [ICI Overview](../ici/overview.md) — the inter-chip interconnect the async-copy ceiling `min(IngressEgress, 2·PerLink)` models
- [VMEM Allocator](../memory/vmem-allocator.md) — the on-chip VMEM space whose local DMA bandwidth this matrix prices
