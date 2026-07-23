# Information-Leakage / Side-Channel Surface

This page is the **white-hat, defensive** consolidation of what a *co-located* or
*co-tenant* GPSIMD custom op can **observe** about another op's (or model's)
execution on the Cayman / Vision-Q7 GPSIMD, and how to **mitigate** it. The lens
is strictly: *given the runtime and silicon as shipped, what residue and what
timing/contention signals cross the boundary between two pieces of work, and what
can a defender do about it?* Every claim is anchored to an address / offset /
symbol recovered from static analysis of the shipped binaries, and tagged
`[confidence × provenance]` where confidence ∈ {HIGH, MED, LOW} and provenance ∈
{OBSERVED, INFERRED, CARRIED}. Nothing here is an exploit recipe; it is a surface
**inventory**, a residual-exposure **assessment**, and a concrete **mitigation**
checklist for an op author and a runtime integrator.

The four channel classes covered:

| # | Class | One-line surface | Verdict |
|---|-------|------------------|---------|
| **1** | **Residual-data** (SBUF / HBM scratch / per-core DataRAM) | is shared working memory zeroed between ops/models? | **Not scrubbed** `[HIGH·OBSERVED]` |
| **2** | **Timing / contention** (SBUF 2-stage arbiter, SDMA QoS, CCOUNT) | can one op time another through shared schedulers? | **Cycle clock present + contention shared** `[HIGH·OBSERVED]` |
| **3** | **PMU / profiler visibility** (qos_pmu, on-core Xtensa PMU) | what measurement is op-readable vs gated? | **USER telemetry readable; shaping/secure gated** `[HIGH·OBSERVED]` |
| **4** | **On-core shared state** (regfiles, `.globstruct`, CRT heap, TLS, semaphores) | does same-core back-to-back state persist? | **Persists; one ring, no scrub** `[HIGH·OBSERVED]` |

> **SCOPE — what this page is NOT.** It is not the reachability proof (which
> physical apertures a Q7 master can address) — that is
> [Custom-Op Reachability / Isolation Model](reachability-isolation.md); not the
> debug/trace gating policy — that is
> [Profiling / Trace / Debug + Access Gating](profiling-trace-debug-gating.md);
> and not the boot/attest/fault trust spine — that is
> [SEC-Lane Synthesis](security-synthesis.md). This page consumes those boundaries
> and asks the narrower question: *within* what the isolation model permits, what
> leaks. Cross-links point at the authoritative pages rather than re-deriving them.

---

## 0. The trust geometry this page sits inside

Three facts from the reachability and SPMD models bound the entire analysis; they
are **CARRIED** here and are the reason the surface is as small (and as sharp) as
it is:

1. **Per-core memory is private; SBUF is the one shared pool.** Each of the 8 Q7
   GPSIMD cores has a private 256 KiB DataRAM, a private 64 KiB SoC aperture
   (`index = 9 + 2·prid`, `0x10000` apart), and a private 2 MiB HBM-scratch
   sub-window (`0x84000000 + prid·0x200000`). The **only** cross-core on-chip
   memory is the **shared, banked SBUF**.
   `[HIGH·CARRIED]` — see
   [The 8-core SPMD model](../../abi/multicore-spmd.md#7-the-work-partition--spmd-model)
   and
   [SBUF 8-core × 16-partition wiring](../../dma/sbuf-psum-banks.md#75-the-8-core--16-partition-gpsimdsbuf-wiring).

2. **PSUM is unreachable from the Q7.** `PSUM_BUF` lives in a disjoint SoC block
   based at `0x2800000000` (32 GiB from the SBUF block at `0x2000000000`); there is
   **no pinned NX window and no `axi2sram`-equivalent PSUM bridge**, so a Q7 AXI
   master has no path to it. PSUM residue is therefore out of an op's observable
   reach. `[HIGH·CARRIED·OBSERVED]` —
   [Why GPSIMD cannot touch PSUM](../../dma/onchip-working-memory.md#3-psum--the-disjoint-block-q7-cannot-reach).

3. **The fabric perimeter gates on `{region, 10-bit AXI master-ID}`, not on a
   VMID.** A cross-tenant address-space breach is a *reachability* question handled
   at Layer-A (the `sprot` remapper CAM, amzn fail-closed / user fail-open). This
   page assumes that boundary holds and studies leakage *through legitimately
   shared resources*. `[HIGH·CARRIED]` —
   [The SoC-Fabric Perimeter](soc-fabric-perimeter.md).

> **NOTE — single-die, single-ring core.** The GPSIMD core is a Cadence Tensilica
> Vision-Q7 in config `ncore2gp`: `XCHAL_HAVE_XEA3 = 1` / `XCHAL_XEA_VERSION = 3`
> (NX), `XCHAL_HAVE_CCOUNT = 1`, `XCHAL_NUM_PERF_COUNTERS = 8`, `XCHAL_HAVE_MPU = 1`
> with `XCHAL_MPU_ENTRIES = 16`, and — decisively for this page —
> **`XCHAL_MMU_RINGS = 1` / `XCHAL_MMU_RING_BITS = 0`** and
> `XCHAL_HAVE_IDENTITY_MAP = 1` / `XCHAL_HAVE_PTP_MMU = 0`
> (`ncore2gp/.../config/core-isa.h`). One ring, identity-mapped, no page-table MMU.
> An op's code and the on-core runtime share the *same* privilege level. This is
> the root cause of §4.

---

## 1. Channel class 1 — Shared-memory residual data

**The central question:** is SBUF / HBM scratch / per-core DataRAM **zeroed or
scrubbed** between custom ops, or between models, so that op *B* cannot read what op
*A* left behind? The recovered runtime answer is **no — there is no scrub on
alloc, on op boundary, or on teardown.** This was verified two independent ways and
both agree.

### 1.1 The carveout life-cycle, with the scrub points it *doesn't* have

The custom-op runtime (`libnrtucode.a` device-orchestration objects;
`libnrtucode_internal.so` host loader) manages three working-memory families —
on-chip `STATE_BUF_SCRATCH_RAM` (32 MiB, SoC `0x2004000000`), `hbm_scratch`
(HBM-resident heap, NX `0x84000000`), and per-core Q7 DataRAM heaps
(`init_neuron_dataram_allocator`: `buffer = DSM + 0x3200`, `size = 0x4000`). The
**only** memset/zero operations anywhere in the non-image runtime objects are:

| Site | Symbol / offset | What it zeroes | Per-op? |
|------|-----------------|----------------|---------|
| Allocation-time struct init | `nrtucode_opset_create` `memset(buf,0,size)` @`0x44`; `calloc` @`0x1e4` | a *freshly allocated* opset struct | No — alloc-time |
| Loaded-image BSS fill | `prelink_load_lib` `memcpy(dst,src,filesz)` @`0x164` → `memset(dst+filesz,0,memsz−filesz)` @`0x17c` (and the split/PI pair @`0x1fe`/`0x217`) | the **BSS gap of the newly loaded code image** | No — one-time at load |

Everything else on the destroy path is **`free()` with no preceding memset**:
`nrtucode_context_destroy` (`free` @`0x150`, `@0x158`), `nrtucode_core_destroy`
(`free` @`0x1a7`), `nrtucode_ll_destroy` (`free` @`0x541`). The unload-sequence
generator `nrtucode_loadable_library.c.o` references **no** memset/bzero at all
(only `memcpy`).

The PI-scratch loader is the same: `xtlib_load_pi_scratch`
(`libnrtucode_internal.so` @`0x9b7830`; `xtlib_pi_scratch_size` @`0x9b75a0`; same
code in `pi_library_load.c.o` @`0x9f0`) only verifies the magic, parses header
words via `xtlib_host_word`/`xtlib_host_half`, and records the scratch placement.
It issues **no** memset/memcpy/bzero — `nm` shows the object has **no** undefined
`memset`/`bzero`/`calloc`. The op runs against **residual scratch**.

> **GOTCHA — the only "zeroing" in the loader is BSS, and it is scoped to
> `memsz − filesz`.** That is the classic crt-style zero-fill of an ELF segment's
> uninitialized tail for *the image being loaded* — it does not touch the shared
> SBUF data plane, the HBM scratch heap, or a previous op's leftover bytes. Do not
> mistake `prelink_load_lib`'s `memset` for a tenant scrub.

### 1.2 Cross-model teardown is a *destroy*, not an in-place scrub

Between **models** the runtime runs full teardown: `tdrv_destroy @0x269a70`
reverses bring-up, destroying all 13 cores (`ucode_core_destroy @0x2267d0` ×13 =
5 NX + 8 Q7), which stops each device image and **frees** its IRAM/DRAM, then
`ucode_free_lib_set @0x311060` frees `lib_dmem`/`scratch_space`/`ucode_table`/
`extram`. This is a *free-and-rebuild*, not a sanitizing wipe — the freed backing
store is not zeroed, and a subsequent model's bring-up does not zero it on the way
in either. `[HIGH·OBSERVED·CARRIED]` —
[the teardown path](../../runtime/spmd-teardown.md#6-the-teardown-path--tdrv_destroy--core_destroy-13--llstdiolib_set).

Between **ops** on the same model there is *no* state reset at all: the only per-op
"join" is the host-emitted `DRAIN (0x10A2)` + completion semaphore, and each core's
`customop_cleanup` ends with a single `memw` (`wrapper_api.o customop_cleanup`
@`0x5d4`, `memw` @`0x5f1`) — a **visibility/ordering fence, not a scrub**. `memw`
makes prior writes globally visible; it clears nothing. `[HIGH·OBSERVED·CARRIED]` —
[Per-core commit fence = `memw`](../../runtime/spmd-teardown.md#42-per-core-commit-fence--memw-in-customop_cleanup-high--observed).

> **CORRECTION — `memw` is not a sanitization step.** A reader skimming the
> teardown could mistake the closing `memw` for "the op cleans up." It is purely a
> memory-ordering fence so the host's post-`DRAIN` reads see the op's writes. No
> register file, `.bss`, heap, TLS, or SBUF byte is zeroed by it.

### 1.3 Residual-data exposure assessment

| Region | Reachable by next op? | Scrubbed? | Residual-leak verdict |
|--------|-----------------------|-----------|-----------------------|
| **SBUF** (shared, 32 MiB; v5/Maverick re-bases to 128 MiB header-only) | Yes — the one shared pool; per-core 16-partition slice via `cayman_is_address_in_my_sbuf` | **No** | **Exposed across ops/tenants that share a partition window** `[HIGH·OBSERVED]` |
| **`hbm_scratch`** (per-core 2 MiB window; HBM physically shared) | Window-private per core; shared only if host hands a shared HBM address | **No** | Residue persists in a core's own window; cross-core only via deliberate sharing `[HIGH·OBSERVED;MED·INFERRED on shared pool]` |
| **`STATE_BUF_SCRATCH_RAM`** (32 MiB on-chip) | Via dynamic 16 MiB NX window | **No** | Residue persists `[HIGH·OBSERVED]` |
| **per-core Q7 DataRAM heap** (`DSM+0x3200`, 16 KiB) | Private to the core | **No** | Same-core back-to-back residue (see §4) `[HIGH·OBSERVED]` |
| **PSUM** | **No** (§0.2) | n/a | Out of observable reach `[HIGH·CARRIED]` |

> **QUIRK — `neuron_translate` has no bounds guard.** The on-core translate
> unconditionally evicts a TLB slot and programs the HW window for whatever 64-bit
> pointer it is handed — no null check, no range check. Region-legality is enforced
> *host-side* by the `cayman_memory_bounds` table (8 × uint64). This is not itself a
> leak, but it means an op that *miscomputes* a scratch base can silently address an
> out-of-region SoC window; defensive ops must not assume the device validates.
> `[HIGH·OBSERVED device-no-guard; MED·INFERRED host-table-is-the-guard]` —
> [the SoC↔Q7 window map](../../dma/onchip-working-memory.md#4-the-soc--q7-nx-local-window-mapping).

> **CORRECTION — device-side per-op scrub is INFERRED-absent, not disproven.** The
> host orchestration drives no scrub, no `scrub`/`wipe`/`sanitize` opcode or string
> exists in the opset tables, and the only device-side zeroing consistent with the
> shipped images is a one-time crt0 `.bss` clear at boot. A per-op scrub loop hidden
> *inside* a device firmware image (the raw `SUNDA_*_IRAM` blobs are flat memory
> images, not ELFs, and were not blind-disassembled) is **very unlikely but cannot
> be formally ruled out**. Treat "no device scrub" as the strong default; do not
> assert it as a proof. `[device-side INFERRED]`

---

## 2. Channel class 2 — Timing / contention channels

Two co-located ops cannot read each other's *data* through timing, but they **can**
observe each other's *activity* through shared schedulers and a freely readable
cycle clock. There are three sub-surfaces: the on-core cycle counter (the
attacker's clock), the SBUF 2-stage arbiter (on-chip contention), and the SDMA QoS
stack (DMA-bandwidth contention).

### 2.1 The clock — CCOUNT is present and op-readable

`XCHAL_HAVE_CCOUNT = 1` and **`CCOUNT` is special-register 234** (`specreg.h:65`;
`CCOMPARE_0/1/2` = SR 240/241/242; `XCHAL_NUM_TIMERS = 3`). CCOUNT is a
free-running cycle counter read with `RSR.CCOUNT`. Because the core has
**one ring** (§0 NOTE: `XCHAL_MMU_RINGS = 1`), there is **no privilege level at
which an op runs that could be denied the `RSR.CCOUNT` read** — op code and runtime
share the same ring. A custom op therefore has a high-resolution, monotonically
increasing on-core timestamp available to it.

> **NOTE — the shipped custom-op wrapper does not itself read CCOUNT.** A sweep of
> `libneuroncustomop.a` (10 members) finds **no** `rsr.ccount` / `rsr a,234` and no
> perf-SR read; the SR usage is limited to context-save SRs and the two
> `rsr.prid`. So the platform neither uses the cycle counter for timing nor fences
> it away from op code — its availability is a *consequence of the single-ring ISA
> config*, not of any runtime policy.

> **GOTCHA — a defender cannot revoke CCOUNT without an ISA reconfig.** Unlike a
> ring-3 OS where `RDTSC` can be trapped via `CR4.TSD`, the one-ring Q7 has no
> "disable cycle counter in user mode" knob. CCOUNT-based timing is a structural
> property of this core. Mitigation must be at the *scheduling* layer (do not
> co-schedule mutually distrustful ops on the same core), not at the read site.

### 2.2 The SBUF 2-stage arbiter — on-chip bandwidth contention

SBUF is shared, banked SRAM (16 ECC banks = 8 clusters × 2 halves; 128 partitions
× 256 KiB stride). Access is mediated by a **2-stage arbiter** with a 3-bit strict
priority + 4-bit DWRR weight at each stage
(`tpb_sbuf_cluster.json`: `arb_stage1 @0x000`, `arb_stage2 @0x100`; at reset only
stage-2 `pe_rd_client` has priority `0x1`, biasing the systolic array; all weights
reset `0x1` = round-robin among equals). Above it sits a **TDM layer**
(`tdm_config @0x300`, 8 slots: 7 compute / 1 DMA at reset) and per-engine
rd/wr throttlers (OFF by default). `[HIGH·OBSERVED·CARRIED]` —
[the in-cluster arbiter](../../dma/sbuf-psum-banks.md#71-pe--act--pool--dve--the-in-cluster-arbiter-clients)
and [the TDM/throttler layers](../../dma/sbuf-psum-banks.md#72-the-tdm--throttler-layers).

The contention signal: **a same-bank, same-cycle conflict stalls the
lower-priority client one slot.** An op whose SBUF accesses collide with a peer's
will see its own access latency modulated by the peer's bank-access pattern — a
classic shared-cache-style contention channel. The arbiter reset values are
OBSERVED; the *stall-one-slot* reading is the page's INFERRED interpretation of the
priority+DWRR semantics. The DMA share is bounded to 1/8 by the TDM slot split.
`[arbiter HIGH·OBSERVED; stall reading MED·INFERRED·CARRIED]`

> **QUIRK — the channel is partition-local.** Two ops only contend if their SBUF
> footprints share an ECC bank. Per the 8-core × 16-partition map, each Q7 core
> owns partitions `[16c : 16c+16]`; SDMA rounds active partitions up to a multiple
> of 32 (a 2-core quadrant). So the SBUF timing channel is *strongest between cores
> in the same quadrant* and absent between non-overlapping partition windows.
> `[HIGH·OBSERVED partition map; MED·INFERRED contention coupling]`

### 2.3 The SDMA QoS stack — DMA-bandwidth contention

Custom-op, compute, and collective DMA are **peers on the same 16-queue
arbiter** — not special-cased. They share three arbiters (DWRR packet scheduler,
prefetch, completion) plus a two-level rate limiter, separated only by queue-bundle
routing and `DMAQoSClass` (3-bit field, 5 classes 0..4). At reset the SDMA is
biased toward *fairness* (prefetch RR, DWRR off, rate-limiters masked; only the
completion arbiter is QoS by default). `[HIGH·OBSERVED·CARRIED]` —
[how custom-op DMA is scheduled vs compute/collective](../../dma/dge-builder-qos.md#103-how-custom-op-dma-is-scheduled-vs-compute--collective)
and [the arbiter summary](../../dma/dge-builder-qos.md#86-the-arbiter-summary).

Crucially, the **arbiter's internal scheduling state is read-back-able from the
USER side**: DWRR per-queue `deficit_cnt[23:0]` via `sel_dwrr_status @0x244`
(indexed by `indirect_ctrl.q_num @0x234`), and rate-limiter `token_cnt[23:0]` via
`sel_rate_limit_status @0x240`. These expose the scheduler's view of *recent
traffic*, which is a function of all tenants sharing that engine. Combined with
`qos_host_visible`'s per-channel backpressure counters (§3), a USER observer can
infer a co-tenant's DMA intensity without reading its data.

---

## 3. Channel class 3 — PMU / profiler counter visibility

The question: **what measurement is readable by an unprivileged op, and what is
gated?** There are two distinct counter facilities and they have opposite exposure.

### 3.1 The fabric `qos_pmu` / `qos_host_visible` — USER-observable telemetry

The `sprot` profiling triad splits cleanly: **CONTROL = `qos_prot` (SECURE),
MONITOR = `qos_host_visible` (USER FIS), PROFILE = `qos_pmu` (USER DEBUG-FIS)** —
the privilege contract is **"observe in USER, shape in SECURE."** `[HIGH·OBSERVED·CARRIED]` —
[the trust split](../csr/qos-pmu-hostvisible.md#9a-trust-domain--what-the-untrusted-host-vs-the-secure-side-can-reach-high--observed).

`qos_pmu` (window `0x800`, USER DEBUG-FIS) ships **8 programmable event counters**
(IP provisions 16; Cayman wires 8), each at `0x100 + I·0x20`:
`pmu_counterI_event_select @+0x00` (32-bit one-hot source select),
`threshold_lo/hi @+0x04/0x08`, `cmp @+0x0c` (3-bit op per snapshot), and the
double-buffered 48-bit snapshots `snap0_lo/hi @+0x10/0x14`, `snap1_lo/hi
@+0x18/0x1c`; absolute `event_select` offsets `{0x100,0x120,…,0x1e0}`. Four AXI
transaction matchers (`axi_txn_matcherM_reg0..19`, base `0x200 + M·0x50`) feed the
counters. All fields reset 0 (counters boot disabled; SW must arm). I verified the
count directly: `jq` over `csrs/sprot/qos_pmu.json` returns exactly 8
`pmu_counterN_event_select` fields. See
[per-counter block](../csr/qos-pmu-hostvisible.md#3b-per-counter-block-i-in-07-base-0x100--i0x20).

What an **unprivileged USER op CAN read/do** (the side-channel-relevant surface):

- **Program `qos_pmu`** — arm `event_select`, set thresholds/`cmp`, program the 4
  matchers, read 48-bit snapshots, take counter interrupts. (gated by
  `fis_control.json apb_user_decode.user_debug_en[24]`, reset 1.)
- **Read `qos_host_visible`** (USER FIS, zero-config, always-on): 48-bit
  `total_reads/writes/bytes`, per-window mirrors, and — the sharpest signal —
  **per-AXI-channel backpressure/stall-cycle counters** ("cycles the channel was
  backpressured: valid and not ready", 5 channels × 2 windows, offsets
  `0x80..0xcc`) plus the signed 9-bit `read/write_channel_delta` outstanding
  monitors and `nts_user` live occupancy (`outstanding_reads[7:0]`,
  `outstanding_writes[5:0]`). This is direct stall/occupancy accounting readable
  from USER. See
  [host-visible vs debug split](../csr/qos-pmu-hostvisible.md#9-host-visible-vs-debug-split-and-what-each-observes-high--observed).

What an unprivileged op **CANNOT** do: change any shaping parameter (every
outstanding-cap, rate/byte limiter, fairness staller, NTS responder lives in
`qos_prot`, **secure-only**), nor read the window/snapshot trigger config (it lives
in privileged `csrs/fis/fis_control.json`, not in the `qos_*` files).

> **NOTE — the PMU event-source semantics are NOT shipped.** `event_select` is a
> 32-bit one-hot, but the *enum* (which bit = read txn / write txn / stall cycles /
> latency / occupancy) is not in any shipped artifact, and the 640-bit matcher
> field layout is opaque. A claim like "`qos_pmu` counter N measures SBUF bank
> stalls" is **not byte-grounded** — flag any specific event mapping as
> INFERRED/unverified. The *structural* fact (USER can program 8 counters + read
> backpressure cycles) is HIGH·OBSERVED; the *what-each-bit-means* is `[LOW]`.

### 3.2 The on-core Xtensa PMU — present in HW, ISA-gated in this config

The Q7 also has an architectural PMU: `XCHAL_NUM_PERF_COUNTERS = 8`
(`core-isa.h`). The memory-mapped debug aperture (`xtensa_q7.json`, 16 KiB) exposes
a PMU block at `0x1000` with `PMG @0x1000` (overall enable), 8× 32-bit free-running
counters `PM0..PM7 @0x1080..0x109c`, selectors `PMCTRL0..7 @0x1100`, status
`PMSTAT0..7 @0x1180`. Counting is **privilege-gated** via `PMCTRLn.KRNLCNT[3]` and
`TRACESCOPE[6:4]` against the core's `EXECLEVEL`. `[HIGH·OBSERVED·CARRIED]` —
[the PMU block](../csr/xtensa-q7.md#2-performance-monitor-pmu-block--0x1000-26-regs).

But two facts neutralize this as an *op-readable* channel:

1. **The ncore2gp build disables the perf-counter ISA path:**
   `IsaUsePerfCounters = 0` in `ncore2gp/.../config/default-params` (even though
   `perfCounterCount = 8`). The hardware counters exist; the standard `RSR`-based
   perf-counter *instructions* are not enabled in this config.
2. **The memory-mapped PMU block is an APB/JTAG debug aperture**, programmed by a
   debug host or privileged perf driver — not the unprivileged-op-facing register
   surface. Whether it is host/op-visible at all is decided at the **fabric
   perimeter**, not in the CSR file. `[HIGH·OBSERVED·CARRIED]`

So: the on-core Xtensa PMU is **present in silicon but not exposed to an op through
the ISA in this build**, and its debug-aperture exposure is gated upstream. The
op-readable measurement surface is the **fabric `qos_*` telemetry of §3.1**, not the
core PMU.

> **CORRECTION — do not conflate the two "8 perf counters."** The fabric `qos_pmu`
> (8 programmable AXI-event counters, USER DEBUG-FIS, **op-programmable**) and the
> on-core Xtensa PMU (`XCHAL_NUM_PERF_COUNTERS = 8`, debug-aperture/`EXECLEVEL`-
> gated, ISA path disabled by `IsaUsePerfCounters = 0`) are **different facilities
> at different privilege tiers**. The op-reachable one is the fabric PMU; the core
> PMU is profiler-gated. Mixing them up over-states what an op can measure on-core.
> Gating policy lives at
> [Profiling / Trace / Debug + Access Gating](profiling-trace-debug-gating.md).

---

## 4. Channel class 4 — Shared on-core state between same-core ops

This is the sharpest residue surface, and it is a direct consequence of the
single-ring core (§0 NOTE). When two custom ops run **back-to-back on the same Q7
core**, what on-core state persists from the first into the second?

### 4.1 The state that persists (no per-op reset)

| State | Where it lives | Reset between ops? | Evidence |
|-------|----------------|--------------------|----------|
| **`.bss` globals** (e.g. `cpu_id` `_ZL6cpu_id`) | per-core SRAM image `.bss` | **No** — set once by static ctor `_GLOBAL__sub_I_parallel.cpp` @`0x18` (`rsr.prid → s32i cpu_id`) at lib load | `[HIGH·OBSERVED·CARRIED]` |
| **CRT heap** (`xmem` DataRAM heap, `DSM+0x3200`, 16 KiB; libc pool `DSM+0xa00`) | per-core DataRAM | **No** — allocator carves once; freed blocks not zeroed (§1.1) | `[HIGH·OBSERVED·CARRIED]` |
| **TLS / THREADPTR** (`XCHAL_HAVE_THREADPTR = 1`) | per-core | **No** per-op reset observed | `[MED·INFERRED]` |
| **Vector / FS register files** | core regfiles | **No** — teardown saves only context SRs (`ps/lbeg/lend/lcount/sar/prefctl/isl/wb/epc`); no regfile clear | `[HIGH·OBSERVED — by absence in the SR sweep]` |
| **Semaphores / atomics** | — | n/a — **none ship** | `[HIGH·OBSERVED]` |

The semaphore row is itself a finding: a full sweep of `libneuroncustomop.a` finds
**zero** `l32ex`/`s32ex` (atomic-exclusive), zero barrier/semaphore/spinlock/mutex/
atomic device symbols; the only mutex symbols are libc++ **no-op weak shims**
(`__libcpp_mutex_lock` = `entry; movi.n a2,0; retw.n`). The 8 cores are fully
independent and single-threaded-per-core; there is no shared lock structure to leak
*through*, but equally no synchronization-driven scrub. `[HIGH·OBSERVED·CARRIED]` —
[inter-core synchronization — proof of independence](../../abi/multicore-spmd.md#8-inter-core-synchronization--proof-of-independence).

### 4.2 Why one ring makes this a real channel

On a multi-ring core, the OS clears a process's registers/heap on context switch and
the ring boundary prevents the next process from reading kernel/peer state. On the
one-ring Q7 (`XCHAL_MMU_RINGS = 1`, `XCHAL_HAVE_IDENTITY_MAP = 1`,
`XCHAL_HAVE_PTP_MMU = 0`) there is **no privilege boundary and no address-space
switch** between an op and the runtime, or between two ops, on the same core. The
**16-entry MPU** (`XCHAL_MPU_ENTRIES = 16`, 4 KiB granularity, `XCHAL_MPU_LOCK = 0`
— entries unlockable, 2 background entries) is the *only* on-core protection
mechanism, and it partitions a flat identity-mapped space — it is not a
privilege-tiered isolator and it does not scrub. So **op *B* on core *c* inherits op
*A*'s register file contents, `.bss`, heap, and TLS verbatim** unless op *A* or the
op author zeroed them.

> **QUIRK — even fault handling does not sanitize.** A compute-core fault is caught
> by the Q7-POOL firmware's own `exception.hpp` (`"GENERIC GPSIMD EXCEPTION"`,
> `"DIVIDE BY ZERO"`), classified, and **self-halts + self-reports** to the host
> (UR#`0x15` + notification ring). There is no state-clear on fault — the boot/fault
> page confirms there is **no `MPU_LOCK`-style latch and zero perimeter-wide
> lock/freeze fields**. A crashed op leaves its residue exactly where it died.
> `[HIGH·OBSERVED·CARRIED]` —
> [boot-arming / fault-recovery](boot-arming-fault-recovery.md).

---

## 5. Mitigations (defensive, white-hat)

Because the platform provides **no scrub guarantee** and **no per-op privilege
boundary on a core**, defenses are caller-side and scheduler-side. Ordered by
strength:

1. **Do not co-schedule mutually distrustful ops on the same Q7 core.** This is the
   only mitigation that closes §4 (same-core register/heap/TLS residue) and §2.1
   (CCOUNT timing) at once — neither has an on-core "off switch" in this config. A
   trust-domain-aware scheduler should pin one tenant per core for the duration of a
   sensitive op and **destroy-and-rebuild** the core (`ucode_core_destroy` →
   re-bring-up) before handing the core to a different domain. `[mitigates §2.1, §4]`

2. **Caller-side scratch zeroing — entry *and* exit.** Since the runtime zeroes
   scratch neither on load (§1.1) nor on teardown, a defensive op must (a) **not
   trust residual** scratch/SBUF contents on entry (zero or fully overwrite before
   first read), and (b) **zero its own secrets** in SBUF / `hbm_scratch` /
   DataRAM before it returns. The closing `memw` in `customop_cleanup` will make
   that zeroing globally visible, but only the op can *issue* it. `[mitigates §1]`

3. **Partition-window discipline for SBUF.** Keep a sensitive op's SBUF footprint
   inside its own per-core 16-partition window and avoid the 32-partition (2-core
   quadrant) SDMA rounding sharing a bank with a co-tenant — this shrinks the §2.2
   bank-contention coupling. `[mitigates §2.2]`

4. **Gate the fabric PMU / host-visible telemetry at the perimeter for sensitive
   workloads.** The §3.1 USER-readable backpressure/occupancy counters are a
   coarse cross-tenant DMA-intensity signal. Where the threat model includes a
   malicious co-tenant, the `user_debug_en` / `user_fis_en` region gates
   (`fis_control.json`, reset 1) can be cleared by privileged firmware to deny the
   debug-FIS PMU and/or the host-visible monitor — at the cost of losing benign
   profiling. This is a **policy** lever owned by the secure side; an op cannot set
   it. `[mitigates §3.1; policy — see Profiling/Trace gating]`

5. **Treat HBM as physically shared.** `hbm_scratch` is window-private per core but
   the HBM pool is physically shared; do not place cross-tenant secrets where a host
   could hand the same physical HBM address to two domains. `[mitigates §1, HBM]`

> **NOTE — v5 / Maverick caveat.** The above is grounded in the Sunda/Cayman/Mariana
> images. For **v5 (Maverick)** only the SBUF *header* geometry is OBSERVED
> (`STATE_BUF_SZ = 0x8000000` = 128 MiB, `PSUM_BUF_SZ = 0x0`); the **interior arbiter,
> scrub behavior, and per-op reset on v5 are INFERRED** to follow the prior
> generations and must be re-verified against a v5 image before relying on any v5
> claim. `[v5 interior INFERRED]`

---

## 6. Confidence ledger

| Claim | Anchor | Confidence × provenance |
|-------|--------|-------------------------|
| No scrub on alloc/op-boundary/teardown of SBUF/scratch | §1.1; `nrtucode_*_destroy` free-without-memset; `xtlib_load_pi_scratch` @`0x9b7830` | **HIGH × OBSERVED** |
| Only zeroing = alloc-time struct init + loaded-image BSS fill (`memsz−filesz`) | §1.1; `prelink_load_lib` memset @`0x17c`; `nrtucode_opset_create` memset @`0x44` | **HIGH × OBSERVED** |
| `memw`-only per-op fence (not a scrub) | §1.2; `customop_cleanup` @`0x5d4`, `memw` @`0x5f1` | **HIGH × OBSERVED (CARRIED)** |
| Device-side per-op scrub *absent* (raw IRAM not disassembled) | §1.3 CORRECTION | **device-side INFERRED** |
| CCOUNT present + op-readable (SR 234, one ring) | §2.1; `core-isa.h` `XCHAL_HAVE_CCOUNT=1`, `specreg.h:65`, `XCHAL_MMU_RINGS=1` | **HIGH × OBSERVED** |
| SBUF 2-stage arbiter bank-conflict contention | §2.2; `arb_stage1/2 @0x000/0x100` | arbiter **HIGH·OBSERVED**; stall reading **MED·INFERRED (CARRIED)** |
| SDMA arbiter peers + USER-readable deficit/token state | §2.3; `sel_dwrr_status @0x244`, `sel_rate_limit_status @0x240` | **HIGH × OBSERVED (CARRIED)** |
| `qos_pmu` = 8 USER-programmable counters; backpressure cycles USER-readable | §3.1; verified `jq` count = 8; `qos_host_visible` offsets `0x80..0xcc` | **HIGH × OBSERVED** |
| PMU event-source enum / matcher layout not shipped | §3.1 NOTE | **LOW** |
| On-core Xtensa PMU present but ISA-gated (`IsaUsePerfCounters=0`) + debug-aperture/`EXECLEVEL`-gated | §3.2; `core-isa.h` `XCHAL_NUM_PERF_COUNTERS=8`; `default-params` | **HIGH × OBSERVED (CARRIED)** |
| Same-core regfile/`.bss`/heap/TLS persists; one ring, 16-entry MPU only | §4; SR sweep absence; `XCHAL_MMU_RINGS=1`, `XCHAL_MPU_ENTRIES=16` | **HIGH × OBSERVED (CARRIED)** |
| No semaphores/atomics ship | §4.1; libc++ no-op shims only | **HIGH × OBSERVED (CARRIED)** |
| Fault path self-halts without sanitization | §4.2 QUIRK | **HIGH × OBSERVED (CARRIED)** |

---

## 7. See also

- [Custom-Op Reachability / Isolation Model](reachability-isolation.md) — which
  apertures a Q7 master can address (the boundary this page assumes).
- [Profiling / Trace / Debug + Access Gating](profiling-trace-debug-gating.md) —
  the policy that decides PMU/TRAX host-visibility (gates §3).
- [SEC-Lane Synthesis](security-synthesis.md) — the boot→attest→fault trust spine.
- [The SoC-Fabric Perimeter](soc-fabric-perimeter.md) — the `{region, AXI-ID}`
  fabric firewall (§0.3).
- [SBUF + PSUM Bank Model](../../dma/sbuf-psum-banks.md) — the 2-stage arbiter and
  bank geometry (§2.2).
- [DGE Descriptor-Builder + SDMA QoS](../../dma/dge-builder-qos.md) — the SDMA
  arbiter peer model (§2.3).
- [qos_pmu + qos_host_visible](../csr/qos-pmu-hostvisible.md) — the USER/SECURE
  telemetry split (§3.1).
- [CSR — Xtensa Q7 Debug/Trace/PMU/OCD](../csr/xtensa-q7.md) — the on-core PMU /
  debug aperture (§3.2).
- [The 8-Core SPMD Model](../../abi/multicore-spmd.md) and
  [SPMD Teardown](../../runtime/spmd-teardown.md) — per-core state and teardown (§1, §4).
- [On-Chip Working-Memory Regions](../../dma/onchip-working-memory.md) — scratch
  families and the no-bounds-guard translate (§1).
