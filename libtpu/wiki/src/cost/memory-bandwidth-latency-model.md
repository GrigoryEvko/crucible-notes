# Memory Bandwidth & Latency Model

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. Other versions differ. All addresses are virtual addresses; for this binary `.rodata` VMA == file offset (`0x84a0000`), and the rodata doubles quoted here were decoded from `.rodata` at those VAs.*

## Abstract

This page documents the **memory-side cost model**: the half of the TPU cost model that prices a DMA by how many TensorCore cycles it costs to move *N* bytes through a particular memory tier. Where [WindowDescription Byte-Cost](window-description-cost.md) turns a windowed access into a byte count, this page turns that byte count into cycles. The model has exactly two terms, summed:

```text
dma_cycles = init_latency_cycles  +  transferred_bytes / bytes_per_cycle
             └─ fixed startup ─┘     └────── bandwidth-proportional ──────┘
```

The startup term is a flat per-(destination-tier) constant — the `InitialDmaLatencyInNs` virtual, scaled to cycles by `DefaultHbmInitLatency`. The bandwidth term is `bytes / bytes_per_cycle`, where `bytes_per_cycle` is derived once from the full-chip HBM (or CMEM) byte rate divided by the TensorCore clock and core count. On v5p and newer, the bandwidth term is computed **twice** — once for the HBM lane and once for the VMEM lane — and the slower lane wins. That per-lane `max`, plus the `max` against the startup term, is the model's **roofline**: a transfer is *latency-bound* when the fixed startup dominates and *bandwidth-bound* when `bytes / bytes_per_cycle` dominates.

The familiar reference frame is a textbook roofline: `time = max(latency, size / bandwidth)`. The divergences a reimplementer must respect are (1) the unit is *TensorCore cycles*, not seconds — both terms are pre-multiplied by TC frequency so they compose with the [CycleTable](cycletable-family.md) throughput integers; (2) the startup latency is charged **once per resource lane**, not once per transfer, so a chain of DMAs into the same lane pays one startup; and (3) the per-tier rate constants are *per-generation virtual overrides* returning `std::optional<double>`, where an absent value means "this tier pair is not modelled" rather than "zero bandwidth."

For reimplementation, the contract is:

- The per-(dst tier) `InitialDmaLatencyInNs` constants (HBM/VMEM/SMEM/CMEM), per generation, and their three distinct dispatch shapes (constant / 2-entry table / VMEM-vs-other branch / `LogFatal` base).
- `DefaultHbmInitLatency = InitialDmaLatencyInNs(elem, bytes) · (TC_MHz / 1000.0)` — the ns→cycle conversion, and the `MemUnitFromKiB(0)` sentinel that short-circuits a no-transfer `MemUnit`.
- The byte→cycle conversion: `MemUnitFromKiB` (KiB→granule-unit shift) and `ChunkGranules` (`(ChunkCellCount·4) / ChunkGranuleBytes`), the granule that `windowing_util::Size` rounds up to.
- `bytes_per_cycle = FullChipBytesPerSecond / (TC_MHz · 1e6) / CoresPerChip`, from `GetBytesPerCycle`, including the `kHbm`/`kCmem`-only CHECK and the two env/backend-config overrides.
- The roofline assembly in `WindowCycles`: the once-per-lane startup add, the per-direction HBM/VMEM `max`, and the v5p+ gate.
- The per-tier `LocalDmaBandwidth<Src>To<Dst>` GB/s matrix that feeds the *copy-mode* decision (`ShouldUseAsyncLocalCopy`), distinct from the cycle-cost `bytes_per_cycle`.

| | |
|---|---|
| **Startup latency (cycles)** | `fusion_util::DefaultHbmInitLatency(MemUnit, WindowDescription, Target)` `@0x14552ca0` |
| **Per-tier startup (ns)** | `Target::InitialDmaLatencyInNs(MemorySpace, double)` `@0x1d61c880` (base `LogFatal`) |
| **Roofline assembly** | `fusion_util::WindowCycles(MemUnit, WindowDescription, Target, double, long, long, long)` `@0x14552660` |
| **Bytes/cycle budget** | `fusion_util::GetBytesPerCycle(HloInstruction, Target, MemorySpace)` `@0x1454dd00` |
| **KiB→granule conversion** | `Target::MemUnitFromKiB(long)` `@0x1d61bf20` |
| **Granule** | `Target::ChunkGranules()` `@0x1d61a440` = `(ChunkCellCount·4) / ChunkGranuleBytes` |
| **Full-chip HBM rate** | `Target::HbmFullChipBytesPerSecond()` `@0x1d6172a0` (= `Target+0x4f0`) |
| **Cores per chip** | `Target::CoresPerChip(TpuCoreType)` `@0x1d615b40` |
| **Per-tier GB/s matrix** | `Target::LocalDmaBandwidth(MemorySpace, MemorySpace)` `@0x1d6168e0` (dispatcher) |
| **Copy-mode consumer** | `ShouldUseAsyncLocalCopy(HloInstruction, MemorySpace, MemorySpace, Target)` `@0x133eff40` |

---

## The Two-Term Cost

### Purpose

Every priced operand DMA — a conv kernel transfer, a reduce-window tile, any `kCopy`/`kCopyStart` priced by the cost model — deposits two cycle values into the [`ResourceVector`](resource-enum.md): a **latency** value into the startup lane and a **bandwidth** value into the transfer lane. The two are deposited separately because the scheduler maxes lanes independently; the latency lane is the analogue of an LLVM `WriteLatency`'s fixed component, the bandwidth lane the analogue of `getMemoryOpCost`'s size-proportional term.

### The arithmetic

```c
// Per priced operand (input or output), inside RecordMemXferCyclesImpl:
granule        = Target.ChunkGranules()                            // @0x1d61a440
raw_bytes      = windowing_util::Size(wd.strides, MemUnit, granule)// align_up to granule
transfer_bytes = raw_bytes / (Compact2ndMinorRatio · ElementPackingFactor)

if rv[latency_lane] == 0.0:                                        // charged ONCE per lane
    rv.Acc(latency_lane, DefaultHbmInitLatency(MemUnit, wd, Target))   // @0x14552ca0
bytes_per_cycle = GetBytesPerCycle(hlo, Target, MemSpace)          // @0x1454dd00
rv.Acc(bw_lane, WindowCycles(MemUnit, wd, Target, bytes_per_cycle, 0, 0, -1))  // @0x14552660
```

The `rv[latency_lane] == 0.0` guard is the **once-per-lane** rule: the DMA startup is billed only the first time a lane is touched in this op's cost, so N transfers into the same lane pay one startup, not N. The bandwidth deposit always accumulates. The input/output lane split (`R9`/`R10` for input, `R11`/`R12` for output) belongs to [WindowDescription Byte-Cost](window-description-cost.md), which prices the byte count this page consumes.

> **NOTE —** the two terms are deposited into *different* lanes and never added together in a single number until the bundle-level `MaxResourceCycles` reduction. A transfer whose startup latency (e.g. 2280 cycles on v6e HBM) exceeds its bandwidth cycles is *latency-bound*: the latency lane dominates the bundle max. A large contiguous transfer is *bandwidth-bound*: the `bytes / bytes_per_cycle` lane dominates. This per-lane separation is the roofline.

---

## Per-Tier Startup Latency — InitialDmaLatencyInNs

### Purpose

`Target::InitialDmaLatencyInNs(MemorySpace dst, double burst_size)` returns the fixed nanosecond startup cost of beginning a DMA whose *destination* is the given tier. The `burst_size` argument is accepted but **ignored by every override** — the latency is purely a per-(dst tier) constant, never a function of transfer size. This is the value `DefaultHbmInitLatency` scales to cycles.

### The per-tier constants

Decoded from the `vmovsd xmm0, [rodata]` immediates inside each per-gen override (all `.rodata` doubles verified byte-exact):

| dst MemorySpace | Jellyfish (v2/v3) | Pufferfish (v4) | Viperfish (v5p) | Ghostlite (v6e) | base | Confidence |
|---|---:|---:|---:|---:|---|---|
| VMEM (`MS=3`) | 240 | 555 | 0 | 0 | `LogFatal` | CERTAIN |
| CMEM (`MS=4`) | 240 | **50** | 1200 | 1200 | `LogFatal` | CERTAIN |
| SMEM (`MS=5`) | 240 | 555 | 1200 | 1200 | `LogFatal` | CERTAIN |
| HBM / any other | 240 | 555 | 1200 | 1200 | `LogFatal` | CERTAIN |

The three dispatch shapes, each confirmed byte-exact in the decompile:

```c
// JellyfishTarget @0x1d48f3a0 — unconditional constant, no MemorySpace branch
return 240.0;                              // vmovsd xmm0, [0xa2de6a8] = 240.0

// PufferfishTarget @0x1d493d00 — 2-entry table, index = (dst == kCmem)
idx = (dst == 4);                          // sete al
return ((double*)0xa2dcd40)[idx];          // [555.0, 50.0]  → CMEM=50, else=555

// ViperfishTarget @0x1d499ca0 / GhostliteTarget @0x1d496dc0 — VMEM-vs-other branch
return (dst == 3) ? 0.0 : 1200.0;          // vxorps→0; if dst!=VMEM, [0xa2df9f8] = 1200.0

// base Target @0x1d61c880 — LogMessageFatal("Unimplemented") at target.h:1235
LogFatal();                                // never reached for a configured target
```

Latency rodata constants (decoded from `.rodata`):

| VA | double | role | Confidence |
|---|---:|---|---|
| `0xa2de6a8` | `240.0` | Jellyfish, all tiers | CERTAIN |
| `0xa2dcd40` | `555.0` | Pufferfish default (table index 0) | CERTAIN |
| `0xa2dcd48` | `50.0` | Pufferfish CMEM (table index 1) | CERTAIN |
| `0xa2df9f8` | `1200.0` | Viperfish / Ghostlite non-VMEM | CERTAIN |

> **GOTCHA —** Pufferfish's CMEM destination is **cheaper** (50 ns) than every other tier (555 ns), the only tier where a non-HBM destination lowers startup cost. The 2-entry table at `0xa2dcd40` is indexed by the boolean `dst == kCmem(4)`; a reimplementation that hardcodes a single PF latency will misprice every CMEM-targeted DMA by 11×. Conversely, on Viperfish/Ghostlite the *only* cheap tier is VMEM (0 ns) — every non-VMEM destination, including CMEM and SMEM, pays the full 1200 ns. The polarity of the cheap tier flips between v4 and v5p+.

> **QUIRK —** the base `Target::InitialDmaLatencyInNs` is a `LogMessageFatal("Unimplemented")` at `target.h:1235`, not a default of zero. A `MemorySpace`/generation pair that reaches the base virtual aborts the compile. Every shipping generation overrides it, so this fatal is a guard against an unconfigured target, not a live code path.

---

## ns → Cycles — DefaultHbmInitLatency

### Purpose

`DefaultHbmInitLatency` is the cost model's adapter that turns the per-tier nanosecond constant into TensorCore cycles, so the startup latency composes with the cycle-denominated bandwidth and [CycleTable](cycletable-family.md) terms. It is the value deposited into the latency lane.

### Algorithm

```c
function DefaultHbmInitLatency(MemUnit mu, WindowDescription wd, Target t):  // @0x14552ca0
    sentinel = t.MemUnitFromKiB(0)                  // @0x1d61bf20 — the "no transfer" MemUnit
    if mu == sentinel: return 0.0                   // vxorpd — short-circuit, no DMA
    CHECK(mu.amount_ != INT64_MAX)                  // target.h:211 guard
    elem = (wd.shape != null) ? (WORD[wd.shape+0xb] >> 2) & 0x1f : 0   // operand element type
    init_ns = t.InitialDmaLatencyInNs(elem, window_bytes)             // vtable[+0x20]
    tc_ghz  = t.TensorCoreFrequencyInMegaHertz() / 1000.0             // /0xa2e0430 = /1000.0
    return init_ns * tc_ghz                                           // ns · GHz = cycles
```

The conversion is exactly `ns · GHz`: dividing `TC_MHz` by `1000.0` yields GHz, and `nanoseconds · gigahertz` is dimensionless cycles. The `/1000.0` divisor was confirmed at `.rodata 0xa2e0430 = 1000.0`. For a non-VMEM destination on v6e (`InitialDmaLatencyInNs = 1200 ns`, `TC = 1900 MHz`): `1200 · 1.9 = 2280` cycles deposited once into the latency lane.

> **GOTCHA —** the `MemorySpace` argument to `InitialDmaLatencyInNs` here is **not** the literal tier enum — it is the operand's *element-type* field re-extracted from the shape pointer (`(WORD[shape+0xb] >> 2) & 0x1f`). For the per-tier latency this is benign because every override either ignores the argument (JF) or branches on a small set of values, but a reimplementation that passes the tier enum where the binary passes the element type will diverge on any future override that actually reads it.

### The MemUnitFromKiB(0) sentinel

`WindowCycles` and `DefaultHbmInitLatency` both begin by comparing the incoming `MemUnit` against `Target::MemUnitFromKiB(0)`. A `MemUnit` is a `{amount_, granule_tag}` pair; `MemUnitFromKiB(0)` is the canonical "zero bytes" `MemUnit` for the target's granule. When the priced access is this sentinel there is no transfer, and both functions return `0.0` immediately — the cost model's way of saying "this operand lives in a memory space that does not incur a DMA."

---

## Byte → Cycle — MemUnitFromKiB and ChunkGranules

### Purpose

The cost model counts bytes in *granule units*, not raw bytes: a transfer is always rounded up to a whole memory granule before it is divided by bandwidth. Two accessors define the granule and the KiB→granule conversion.

### MemUnitFromKiB — `@0x1d61bf20`

```c
function MemUnitFromKiB(this, long amount_kib):                 // @0x1d61bf20
    granule_bytes = this.vtable[+0x5c0]()                       // ChunkGranuleBytes (per gen)
    CHECK(granule_bytes <= 1024)                                // target.cc:3244
    CHECK(1024 % granule_bytes == 0)                            // target.cc:3245 — power-of-two divisor
    shift = log2(1024 / granule_bytes)                          // _BitScanReverse
    return amount_kib << shift                                  // KiB → granule-units
```

`MemUnitFromKiB(kib)` converts a kibibyte count into the target's native granule unit: `kib · (1024 / granule_bytes)`, computed as a left shift because `1024 / granule_bytes` is required to be a power of two (the two CHECKs enforce `granule_bytes | 1024` and `granule_bytes ≤ 1024`). For `amount_kib == 0` the result is `0` — the sentinel that `DefaultHbmInitLatency`/`WindowCycles` test against.

### ChunkGranules — `@0x1d61a440`

```c
function ChunkGranules(this):                                  // @0x1d61a440
    chunk_cell_bytes = 4 * topology[+0x1a8]                     // ChunkCellCount · 4 bytes/cell
    granule_bytes    = this.vtable[+0x5c0]()                    // ChunkGranuleBytes (per gen)
    return chunk_cell_bytes / granule_bytes                     // granules per chunk
```

`ChunkGranules` is the granule that `windowing_util::Size` rounds the transfer up to: the chunk-cell byte size (`ChunkCellCount · 4`) divided by the per-gen `ChunkGranuleBytes`. `Size` products the window's per-axis stride counts, ceil-divides by this granule, and multiplies back — billing any partial granule as a whole one. Both `MemUnitFromKiB` and `ChunkGranules` read the same per-gen `ChunkGranuleBytes` virtual at `vtable[+0x5c0]`, so the byte-accounting unit is gen-consistent across the two accessors.

> **NOTE —** the rounding granule is a *chunk* concept, not the DMA descriptor granule. `Size`'s `align_up(Product(strides), ChunkGranules)` rounds the byte volume up to whole chunks; the DMA-fragment count (the `{1.0, 1.6, 1.3, 1.1, 1.05}` efficiency ratio) is a separate, orthogonal correction in [WindowDescription Byte-Cost](window-description-cost.md). A reimplementation that conflates the chunk granule with the descriptor count will double-correct.

---

## Bytes/Cycle — GetBytesPerCycle

### Purpose

`bytes_per_cycle` is the per-core, per-cycle byte budget that `WindowCycles` divides the transferred bytes by. It is the bandwidth denominator of the roofline, derived from the full-chip byte rate and the TC clock.

### Algorithm

```c
function GetBytesPerCycle(HloInstruction* hlo, Target t, MemorySpace ms):  // @0x1454dd00
    CHECK(ms == kHbm(1) || ms == kCmem(4))               // fusion_util.cc:2354
    cycles_per_sec = t.TensorCoreFrequencyInMegaHertz() * 1e6     // *0xa2e0208 = 1e6
    full_chip_bps  = (ms == kHbm) ? t.HbmFullChipBytesPerSecond() // @0x1d6172a0 (Target+0x4f0)
                                  : t.CmemFullChipBytesPerSecond()// @0x1d6172c0
    bpc = full_chip_bps / cycles_per_sec / t.CoresPerChip(0)      // @0x1d615b40
    if ms == kHbm:
        env = GetTpuCompEnv(hlo.GetModule())[+0x1040]             // env HBM bw override
        if env > 0: bpc = env                                    // vblendvpd select
        // per-instruction backend_config FlagConfig override (opcode-14 custom-call path):
        if hlo has FlagConfig_Value of type kDouble: bpc = that.double_value  // [+0x10]
    return bpc
```

The geometry is `bytes_per_cycle = FullChipBytesPerSecond / (TC_MHz · 1e6) / CoresPerChip`: the chip's aggregate memory rate, converted to bytes-per-second-per-cycle by dividing by the clock in Hz, then split across the chip's TensorCores. The `1e6` multiplier was confirmed at `.rodata 0xa2e0208 = 1000000.0`; `HbmFullChipBytesPerSecond` is a plain field read at `Target+0x4f0` (populated from chip parts); `CoresPerChip` reads a per-`TpuCoreType` count from the topology.

| Source | Address | Meaning | Confidence |
|---|---|---|---|
| `TensorCoreFrequencyInMegaHertz` | `0x1d615b60` | TC clock (MHz); v7 chip parts = 1900 | HIGH |
| `HbmFullChipBytesPerSecond` | `0x1d6172a0` | `Target+0x4f0` field (full-chip HBM B/s) | CERTAIN |
| `CmemFullChipBytesPerSecond` | `0x1d6172c0` | full-chip CMEM B/s (kCmem path) | CERTAIN |
| `CoresPerChip` | `0x1d615b40` | per-`TpuCoreType` core count from topology | CERTAIN |
| `1e6` Hz multiplier | `0xa2e0208` | MHz → Hz | CERTAIN |

> **GOTCHA —** `GetBytesPerCycle` is only valid for `kHbm` and `kCmem`; any other `MemorySpace` triggers `LogFatal("m_space == MemorySpace::kHbm || m_space == MemorySpace::kCmem")` at `fusion_util.cc:2354`. VMEM/SMEM transfers are *not* priced through this function — their cycle cost rides the HBM `bytes_per_cycle` denominator and the per-direction VMEM lane inside `WindowCycles` (below), not a VMEM-specific `bytes_per_cycle`. The two **override hatches** — the `GetTpuCompEnv[+0x1040]` chip-wide env value and the per-op `FlagConfig` double — let a caller pin `bytes_per_cycle` for benchmarking; a reimplementation that ignores them will diverge whenever the env or a custom-call backend config sets a non-zero HBM bandwidth.

---

## The Roofline — WindowCycles

### Purpose

`WindowCycles` is the public entry that assembles the full cost: it adds the startup latency, divides bytes by `bytes_per_cycle`, and — on v5p and newer — prices the HBM and VMEM directions separately and takes the slower one. Its return is the bandwidth-lane cycle value. The combination of the per-direction `max` and the startup add **is** the roofline.

### Algorithm

```c
function WindowCycles(MemUnit mu, wd, Target t, double bytes_per_cycle, a, b, c):  // @0x14552660
    if mu == t.MemUnitFromKiB(0): return 0.0           // sentinel — no transfer

    // startup-latency term (cycles)
    init = (c == -1) ? DefaultHbmInitLatency(mu, wd, t)             // @0x14552ca0
                     : (t.TensorCoreFrequencyInMegaHertz() / 1000.0) * c   // freq-scaled override

    count_desc = (t.vtable[+0x590]() == 1)             // per-gen "count descriptors" predicate
    base = WindowCyclesGenericTargetAgnostic(mu, wd, count_desc, bytes_per_cycle)  // @0x14552180
    cycles = base + init

    if t[+0x398] < 5:                                  // < v5p (TpuVersion at Target+0x398)
        // single-lane: per-direction max already folded into `base`
        return max(dir0, dir1) + init
    else:                                              // v5p+ two-lane HBM/VMEM roofline
        hbm_lat  = t.InitialDmaLatencyInNs(kHbm,  ...) // vtable[+0x20], arg = 1
        vmem_lat = t.InitialDmaLatencyInNs(kVmem, ...) // vtable[+0x20], arg = 3
        // {hbm_bytes, vmem_bytes} / 1000.0 (xmmword_A2CE650), * TC_MHz, gated by >0.01
        hbm_cyc  = (hbm_bytes  / 1000.0) * TC_MHz
        vmem_cyc = (vmem_bytes / 1000.0) * TC_MHz
        return max(hbm_cyc, vmem_cyc) + (hbm_lat + vmem_lat-blend) + init
```

The core divide `transferred_bytes / bytes_per_cycle` happens inside `WindowCyclesGenericTargetAgnostic` (the `vdivsd` against the `bytes_per_cycle` passed in `xmm0`) — see [WindowDescription Byte-Cost](window-description-cost.md) for the fragment-ratio detail. `WindowCycles` adds the startup `init` on top and, on v5p+, replaces the single bandwidth term with a two-lane computation.

The v5p+ two-lane block (confirmed at `@0x1455282e`) is the explicit roofline:

- It loads an `{hbm_bytes, vmem_bytes}` pair and divides each by `1000.0` (the `xmmword_A2CE650` pair, confirmed `[1000.0, 1000.0]`), then multiplies by TC frequency — converting each direction's byte count to its own cycle estimate.
- A `vcmpnlepd` against `xmmword_A2D8020` (`[0.01, 0.01]`) gates each direction by a 1% percentage floor, `vandpd`-selecting whether that direction's latency contributes.
- A `vmaxsd` takes the **per-direction maximum** — the slower of HBM and VMEM. This is the "two-lane roofline": a transfer that is HBM-bound and a transfer that is VMEM-bound are priced independently, and the op pays the worse.

The VLOG-6 trace at `fusion_util.cc:3474` names every intermediate, confirming the model is a two-lane max: `"hbm_percentage"`, `"vmem_percentage"`, `"hbm_bytes"`, `"vmem_bytes"`, `"mem_bw"`, `"vmem_byte_rate"`, `"hbmbw"`, `"vmembw"`, `"hbmlatency"`, `"vmemlatency"`, and `"next_gen"` (the v5p+ marker, `fusion_util.cc:3481`).

| rodata / vtable | value / offset | role | Confidence |
|---|---|---|---|
| `0xa2ce650` | `[1000.0, 1000.0]` | per-direction byte→rate divisor (v5p+) | CERTAIN |
| `0xa2d8020` | `[0.01, 0.01]` | per-direction 1% percentage gate (`vcmpnlepd`) | CERTAIN |
| `vtable[+0x590]` | `== 1` | per-gen count-descriptors predicate | HIGH |
| `Target+0x398` | `>= 5` | v5p+ two-lane gate (`TpuVersion`) | CERTAIN |

> **NOTE —** the `c` (7th) argument selects the startup source: `c == -1` (the value `RecordMemXferCyclesImpl` always passes) uses `DefaultHbmInitLatency`; a non-`-1` `c` uses `TC_GHz · c` as an explicit per-call startup count. The shipping cost path always passes `-1`, so the live startup is always the per-tier `InitialDmaLatencyInNs`; the `c` hatch exists for callers that supply a precomputed startup.

---

## Per-Tier Bandwidth Matrix — LocalDmaBandwidth

### Purpose

Distinct from the `bytes_per_cycle` *cycle-cost* denominator, the per-tier `LocalDmaBandwidth<Src>To<Dst>` virtuals return a raw **GB/s** for each ordered (source tier, destination tier) pair. These feed the *copy-mode* decision — whether a copy is issued as an async local DMA or kept synchronous — not the per-byte cycle cost. The dispatcher `Target::LocalDmaBandwidth(src, dst)` `@0x1d6168e0` maps a `(src, dst)` pair to one of the 15 reachable per-tier virtuals; each returns `std::optional<double>` (the IEEE-754 bits in `rax`, the present flag in `dl`).

### The matrix (GB/s)

Decoded from the `return <ieee754>` immediates in each per-gen override; `—` = no override (base returns `nullopt` = not modelled). Viperfish columns show `default / lite` where the `variant_name == "lite"` gate applies (all immediates verified byte-exact):

| Pair (Src→Dst) | Dragonfish | Pufferfish | Viperfish (def/lite) | Ghostlite | Confidence |
|---|---:|---:|---:|---:|---|
| HBM → HBM | — | 480 | 72 / 308 | 64 | CERTAIN |
| HBM → VMEM | 423 | 481 | 1198 / 822 | 1285 | CERTAIN |
| HBM → SMEM | — | 34 | 55 / 56 | 55 | CERTAIN |
| HBM → CMEM | *(via VmemToVmem slot)* | *(via VmemToVmem)* | *(via VmemToVmem)* | *(via VmemToVmem)* | CERTAIN |
| VMEM → HBM | 423 | 1111 | 1224 / 828 | 1432 | CERTAIN |
| VMEM → VMEM | — | 544 | 72 / 827 | 64 | CERTAIN |
| VMEM → CMEM | — | 1121 | — | — | CERTAIN |
| VMEM → SMEM | — | 34 | 55 / 56 | 55 | CERTAIN |
| CMEM → HBM | — | 1080 | — | — | CERTAIN |
| CMEM → VMEM | — | 2339 | — | — | CERTAIN |
| CMEM → CMEM | — | 1193 | — | — | CERTAIN |
| CMEM → SMEM | — | 34 | — | — | CERTAIN |
| SMEM → HBM | — | 34 | 55 / 56 | 55 | CERTAIN |
| SMEM → VMEM | — | 34 | 55 / 56 | 55 | CERTAIN |
| SMEM → CMEM | — | 34 | — | — | CERTAIN |
| SMEM → SMEM | — | 17 | 28 | 28 | CERTAIN |
| SPMEM → HBM | — | — | 587.4 | 588 | CERTAIN |

Structural facts a reimplementer must respect:

- **No `LocalDmaBandwidthHbmToCmem` virtual exists.** The dispatcher routes `(Hbm, Cmem)` into the **VmemToVmem** slot (`vtable+0x1b8`): HBM↔CMEM transfers are cost-modelled as VMEM-staged, so the HBM→CMEM leg is priced at the VmemToVmem rate.
- **The base `Target` returns `0` (nullopt)** for every pair — confirmed at `Target::LocalDmaBandwidthHbmToVmem @0x1d492580` returning `0`. An absent value means "this tier pair is not modelled," and `ShouldUseAsyncLocalCopy` treats it as "do NOT route this as a local DMA," keeping the copy synchronous.
- **Dragonfish overrides only HBM↔VMEM** (both 423, `0x407A700000000000`); Jellyfish defines no `LocalDmaBandwidth` override of its own and inherits.
- **Viperfish gates on `"lite"`** via an SSO-aware `variant_name.size() == 4 && variant_name == "lite"` compare (the 4-byte literal `0x6574696C`, `'l','i','t','e'`), confirmed at `LocalDmaBandwidthHbmToVmem @0x1d49a380`: default → 1198, lite → 822. Single-valued VF pairs (`SmemToSmem = 28`, `SpmemToHbm = 587.4`) do not gate.

Sample byte-exact confirmations:

```c
DragonfishTarget::LocalDmaBandwidthHbmToVmem @0x1d48fa20 → 0x407A700000000000 = 423.0
GhostliteTarget::LocalDmaBandwidthHbmToVmem  @0x1d4973e0 → 0x4094140000000000 = 1285.0
ViperfishTarget::LocalDmaBandwidthHbmToVmem  @0x1d49a380 → "lite"? 822.0 : 1198.0
Target::LocalDmaBandwidthHbmToVmem (base)    @0x1d492580 → 0   (nullopt)
```

> **QUIRK —** the GB/s matrix and the `bytes_per_cycle` budget are two *different* bandwidth numbers serving two *different* decisions. `bytes_per_cycle` (from `GetBytesPerCycle`, HBM/CMEM only) prices the *cycle cost* of a transfer once the cost model has decided to emit it. The `LocalDmaBandwidth<Src>To<Dst>` matrix decides *whether* a copy is emitted async at all, and feeds the collective-vs-local bisection-bandwidth threshold. A reimplementation that uses the GB/s matrix as the cycle-cost denominator (or vice versa) will produce a self-consistent but wrong cost model. The full dispatcher map and the copy-mode/bisection consumers are documented in [Local DMA Bandwidth](local-dma-bandwidth.md).

---

## How the Scheduler Consumes the Latency/Bandwidth

The latency and bandwidth cycle values land in the [`ResourceVector`](resource-enum.md) and are reduced into the op's cost by the bundle-level `MaxResourceCycles`. The [`CostModelLatencyEstimator`](overview.md) (the `LatencyHidingScheduler` client) then converts cycles to wall-clock seconds via `cycles · trip_count / (TC_MHz · 1e6)` — the same TC-frequency geometry `GetBytesPerCycle` uses, so the bandwidth term is dimensionally consistent end to end.

The copy-mode decision sits *before* this. `ShouldUseAsyncLocalCopy` `@0x133eff40` reads the `LocalDmaBandwidth(src, dst)` GB/s matrix: if the optional is absent the copy stays synchronous; if present, for a multi-device collective it compares the local rate (scaled by `replicas²` and a ×4 factor) against the cross-chip bisection bandwidth to choose local-async vs collective-routed. That gate decides *which* DMAs exist; this page's latency/bandwidth cycle model prices the DMAs that survive it. The generic XLA `memory_space_assignment::CostAnalysis::GetAsyncCopyElapsed` uses a single flat config bandwidth (`DefaultMemBandwidthBytesPerSecond @0x1dceb320`), **not** this per-tier matrix — the per-tier model is TPU-specific and runs ahead of MSA's kAlternate/kDefault choice.

---

## Related Components

| Name | Relationship |
|---|---|
| [WindowDescription Byte-Cost](window-description-cost.md) | builds the byte count this model divides by `bytes_per_cycle`; owns the R9–R12 lane routing |
| [Local DMA Bandwidth](local-dma-bandwidth.md) | the full `LocalDmaBandwidth(src,dst)` dispatcher map + copy-mode/bisection consumers |
| [Cost Model Overview](overview.md) | the three-family cost model and the `cycles → seconds` TC-frequency conversion |
| [CycleTable Family](cycletable-family.md) | the throughput integers the bandwidth/latency cycles compose with in the bundle max |
| [Resource Enum (23-slot)](resource-enum.md) | the `ResourceVector` lanes the latency and bandwidth values deposit into |

## Cross-References

- [WindowDescription Byte-Cost](window-description-cost.md) — `windowing_util::Size`, the fragment ratio, and the R9/R10/R11/R12 lane split that consumes this `bytes_per_cycle` and `DefaultHbmInitLatency`
- [Local DMA Bandwidth](local-dma-bandwidth.md) — the per-(src,dst) GB/s dispatcher `@0x1d6168e0`, the HBM→CMEM-via-VmemToVmem routing, and `ShouldUseAsyncLocalCopy`
- [Cost Model Overview](overview.md) — the `LatencyHidingScheduler` cost client and the `TensorCoreFrequencyInMegaHertz` wiring shared with `GetBytesPerCycle`
- [CycleTable Family](cycletable-family.md) — the per-gen throughput backend that the memory-transfer cycles are maxed against
- [Resource Enum (23-slot)](resource-enum.md) — the `MaxResourceCycles` reduction over the latency and bandwidth lanes
- [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md) — a consumer that prices operand-window DMAs through this latency/bandwidth model
- [ConvolutionCostState](convolution-cost-state.md) — `RecordConvKernelCycles`, which deposits the operand DMA latency/bandwidth alongside the conv compute
