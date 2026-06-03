# EUP Latency Overview

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The EUP — libtpu's transcendental approximator, the "XLU" of the VPU — is a deep, FIFO-buffered pipeline that a *push* enters in one bundle and a *pop* drains some bundles later. Because a TensorCore bundle **is** the issue packet and there is no runtime hazard interlock, the compiler must know exactly how many bundles separate a push from its drainable result, and it must place a `kVectorEupResultValue` pop no earlier than that. That separation is the EUP **push→pop data latency**, and it is the single number the bundle scheduler raises the push→pop dependency edge to. This page is the cost-model framing of that number: where it is stored, how it is read on each generation, how it composes with the orthogonal EUP issue reservation, and what a reimplementer must reproduce to schedule a transcendental correctly.

The latency model is the cost-side dual of the ISA-side push/pop encoding. The [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) page owns *how* a transcendental is encoded (the `Alu3` slot-3 push, the function selector, the `0x14e` pop, the fused-`AndPop`-vs-split duality); this page owns *when* the pop may be scheduled. The mechanism is a per-generation cost table read by the dependency graph: `LatencyTable::LatencyBetween(push, pop)` calls a per-gen `LatencyBetweenInternal`, which on the v4+ generations classifies the LLO push opcode to a per-gen `Performance::Instruction` ordinal and reads `Performance::GetLatency(Instruction) = latencies[Instruction]` from a heap `int32` array. The legacy Jellyfish/Dragonfish path does not have that array; it clamps the edge to a fixed `Performance[+0x30]` field instead. Either way, the value returned is the minimum bundle gap from the push to its pop — exactly the software-pipeline depth the interleaved VALU correction arithmetic must fill.

The number a reimplementer is tempted to confuse it with is `VectorEupReservationCycles`, the EUP unit's *issue rate* (how many bundles the unit stays reserved after a push, bounding back-to-back pushes). The two are read from different places, bound different gaps, and do **not** multiply: the latency is the push→pop deadline, the reservation is the push→push throughput, and the scheduler composes them as `max(latency-deadline, resource-availability)`. Pufferfish is the one generation where they diverge — a 7-cycle latency with a half-rate (2-cycle) reservation — which is precisely the case that breaks a naive `latency × reservation` model. The per-gen latency integers, the correction polynomials, the Payne-Hanek reduction, and the lane-width fan-out each get their own page; this one explains the edge they all plug into.

For reimplementation, the contract is:

- **The two-array model.** Each v4+ `Performance` object holds a flat `latencies[Instruction]` heap array (the data-latency edge) *and* a `resourceUsage[Instruction][Resource]` 2D grid (the issue reservation). `GetLatency` reads the first; the reservation comes from the second plus the `VectorEupReservationCycles` accessor. They are physically distinct arrays.
- **The read path.** `LatencyBetween` → per-gen `LatencyBetweenInternal` → `Get<Gen>Instruction(push)` → `GetLatency(Instruction)`, with the EUP push **not** in the transpose/MXU floor set, so the raw value passes through unmodified.
- **The per-gen latency integers** (PF 7, VF 6, GL 13 F32 / 14 BF16, JF/DF 4-clamp) and the uniform-across-functions, split-by-datatype structure — owned in full by [EUP Per-Gen Integers](eup-per-gen-integers.md).
- **The reservation orthogonality.** `VectorEupReservationCycles` (PF 2, VF/GL/JF 1) bounds push→push spacing, not the push→pop window; the composition is a max, not a product.

| | |
|---|---|
| **Edge dispatcher** | `LatencyTable::LatencyBetween` `@0x1c89f820` — virtual `LatencyBetweenInternal` at vtable `+0x18` |
| **v4+ read path** | `Get<Gen>Instruction(push)` → `<Gen>Performance::GetLatency(Instr)` = `latencies[Instr]` |
| **`GetLatency`** | VF `@0x1c8cbc20`, PF `@0x1c8c3860`, GL `@0x1c8d36e0` — identical bounds-checked `latencies[idx]` |
| **Push→pop latency** | JF/DF 4 (clamp); PF 7; VF 6; GL 13 (F32) / 14 (BF16); pop drains at latency 1 |
| **JF/DF clamp** | `LatencyTableJellyfish::LatencyBetweenInternal` `@0x1c8a0d60` — `from∈[0x128,0x13a] ∧ to==0x14e` → field `+0x1c` |
| **Issue reservation** | `VectorEupReservationCycles` (Target vtable `+0x480`): JF/VF/GL = 1, PF = 2 (half-rate) |
| **Composition** | `max(push_bundle + latency, prev_push + reservation)` — latency ⊥ reservation, never a product |
| **Software-pipeline depth** | the **latency** (PF 7), independent of the reservation |

---

## The Push→Pop Latency Edge

### Purpose

A transcendental is a two-instruction sequence: a push that reserves the EUP and a pop that retrieves the result `N` bundles later. The scheduler must not place the pop before `push_bundle + N`, or it would read an in-flight (not-yet-drained) result. `N` is the **push→pop data latency** — the EUP pipeline depth — and it is the dependency-graph edge weight from push to pop. The intervening `N` bundles are where the late decomposer interleaves VALU correction arithmetic (Newton steps, range reconstruction) or unrelated work, exactly as a CPU back end hides a long-latency divide. Because the bundle is the issue packet, this latency is not a runtime hazard the hardware stalls on; it is a compile-time scheduling constraint the cost model encodes.

### The Two Mechanisms

There are two ways the edge is computed, split at the Jellyfish/Pufferfish silicon boundary. The legacy path **clamps**; the modern path reads a **per-instruction array**.

```c
function LatencyBetweenInternal_jellyfish(from, to):   // @0x1c8a0d60 (JF/DF)
    edge = 1
    // ... RPU / IndexedStore / matres floors elided ...
    is_eup_push = (from.opcode - 0x128) <= 0x12          // from ∈ [0x128, 0x13a]
    if (is_eup_push && to.opcode == 0x14e)               // 0x14e = kVectorEupResultValue (pop)
       || LloOpcodeIsPseudoEupInstruction(from.opcode):  // or a fused AndPop op
        if edge <= this[+0x1c]:                          // clamp UP to the EUP-latency field
            edge = this[+0x1c]                            // = Performance[+0x30] = 4
    return edge
```

The clamp field `this+0x1c` is copied by the `LatencyTableJellyfish` ctor from `Performance[+0x30] = 4` — the flat-POD generations have no per-function transcendental latency, just one constant. (`Performance[+0x30]` lives in the JF/DF inline-POD model of the [Performance Family Overview](performance-overview.md); on Jellyfish it is one of the `.rodata`-block-copied cells, not a scalar store.)

The v4+ generations route through a per-instruction heap array instead. The structure is uniform across Pufferfish, Viperfish, and Ghostlite:

```c
function LatencyBetweenInternal_modern(from, to):   // PF @0x1c8a2aa0 / VF @0x1c8a4ac0 / GL @0x1c8b22e0
    instr = Get<Gen>Instruction(from)                // LLO opcode → per-gen Instruction ordinal
    base  = <Gen>Performance::GetLatency(instr)      // latencies[instr] from the heap array
    // a transpose-latch HALVING (base/2) applies ONLY to the matmul-latch region
    //   (GL/VF opcodes 0x8d..0x96 / 0x73..0x7c) — the EUP push (0x128..0x13a) is excluded
    // XLU / MXU max-combines only RAISE base on a conflict — the EUP push has none
    return base                                      // EUP push: passes through unmodified
```

`GetLatency` is the same four-instruction lookup on every modern gen — a bounds check against the element count at `Performance[+0x8]`, then `latencies[instr]` from the pointer at `Performance[+0x0]`:

```c
function <Gen>Performance::GetLatency(perf, instr):   // VF @0x1c8cbc20, PF @0x1c8c3860, GL @0x1c8d36e0
    if perf.latency_count <= instr:    // [perf+0x8]
        trap()                          // ud2 / BUG
    return perf.latency_ptr[instr]      // *(int32*)([perf+0x0] + instr*4)
```

> **GOTCHA —** the EUP push is the latency-carrying instruction; the pop is a 1-cycle drain. The push→pop dependency edge is computed as `LatencyBetweenInternal(first = push, second = pop)`, and the per-gen path evaluates `GetLatency` on the **first** operand — the push. So the edge weight equals the *push* latency (6/7/13/14). The `0x14e` pop's own `latencies[pop] = 1` is not the push→pop gap; it is the latency the *drained value* carries to whatever instruction consumes it downstream. A reimplementer who reads `GetLatency(pop)` for the push→pop edge gets 1 and schedules the pop one bundle after the push — reading garbage.

### Per-Gen Latency Values

The push value is uniform across every classified EUP function on a given generation — `tanh = pow2 = recip = log2 = rsqrt = sigshft = sinq = cosq = erf` — so the EUP unit has a single transcendental latency per datatype, not a per-function table. Ghostlite is the only generation that splits F32 from BF16 (it keeps the 16-bit BF16 lane, so the BF16 transcendental costs one extra cycle); Viperfish and Pufferfish classify only the F32 pushes and widen BF16 EUP to the F32 push, so they have one latency.

| Gen | TpuVersion | EUP push latency | Pop latency | Mechanism | Byte anchor | Confidence |
|---|---|---|---|---|---|---|
| Jellyfish | v2 | 4 | (clamp) | `Performance[+0x30]` = 4, clamped in `LatencyBetweenInternal` | clamp field `+0x1c` | CERTAIN |
| Dragonfish | v3 | 4 | (clamp) | inherits JF `+0x30` = 4 | (= JF) | CERTAIN |
| Pufferfish | v4 | 7 | 1 | `PufferfishPerformance` `latencies[]` | ctor `[ptr+0x19c..0x1b0]=7`, `[+0x1d8]=1` | CERTAIN |
| Viperfish | v5p | 6 | 1 | `ViperfishPerformance` `latencies[]` | ctor `[ptr+0x330..0x348]=6`, `[+0x5a0]=1` | CERTAIN |
| Ghostlite | v6e | 13 (F32) / 14 (BF16) | 1 | `GhostlitePerformance` `latencies[]` | ctor `[ptr+0x418..0x43c]=0xd`, `[+0x440..0x460]=0xe`, `[+0x710]=1` | CERTAIN |

The byte anchors above were confirmed cell-by-cell against the constructors: Pufferfish's `PufferfishPerformance::PufferfishPerformance @0x1c8be080` stores `latencies[0x67..0x6c] = 7` (six EUP-push ordinals) and `latencies[0x76] = 1` (the pop ordinal); Ghostlite's ctor stores `latencies[0x106..0x10f] = 13` (ten F32 pushes), `latencies[0x110..0x118] = 14` (nine BF16 pushes), `latencies[0x1c4] = 1`; Viperfish's stores `latencies[0xcc..0xd2] = 6` and `latencies[0x168] = 1`. The per-gen opcode→Instruction classifier that turns an LLO push opcode into the array index, and the full nine-function block, are owned by [EUP Per-Gen Integers](eup-per-gen-integers.md).

> **QUIRK —** Pufferfish's latency table is a `std::variant<PufferfishPerformance, PufferfishBarnaCorePerformance>` dispatched by a 2-arm `__fmatrix` visitor (`LatencyFromInstruction` `@0x21c203d0`); the high 16 bits of `GetPufferfishInstruction`'s return select the variant (`shr r14d,0x10` then `call [fmatrix + idx*8]`). **Every** EUP opcode emits variant 0 (TensorCore), so the EUP edge is the TensorCore array = 7 — not the BarnaCore variant, which carries its own lower-latency EUP block (= 6 at ordinals `0x77..0x7c`, byte-confirmed in the `PufferfishBarnaCorePerformance` ctor `@0x1c8c38c0`) for the legacy embedding engine. Pufferfish ships both because BarnaCore retires only after Pufferfish; the TensorCore transcendental never reaches the 6-cycle BarnaCore block.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LatencyTable::LatencyBetween` | `0x1c89f820` | edge dispatcher; calls virtual `LatencyBetweenInternal`, floors only `0x82`/`0x84` | CERTAIN |
| `LatencyTableJellyfish::LatencyBetweenInternal` | `0x1c8a0d60` | JF/DF EUP clamp to field `+0x1c` (= `Performance[+0x30]` = 4) | CERTAIN |
| `LatencyTablePufferfish::LatencyBetweenInternal` | `0x1c8a2aa0` | PF path; `__fmatrix` variant dispatch (EUP = variant 0) | CERTAIN |
| `LatencyTableViperfish::LatencyBetweenInternal` | `0x1c8a4ac0` | VF path; `GetViperfishInstruction` → `GetLatency` | CERTAIN |
| `LatencyTableGhostlite::LatencyBetweenInternal` | `0x1c8b22e0` | GL path; `GetGhostliteInstruction` → `GetLatency(this+0x1d0)` | CERTAIN |
| `<Gen>Performance::GetLatency` | `0x1c8c3860` (PF), `0x1c8cbc20` (VF), `0x1c8d36e0` (GL) | bounds-checked `latencies[Instruction]` | CERTAIN |
| `PufferfishPerformance` ctor | `0x1c8be080` | fills EUP=7 (TensorCore variant 0), pop=1 | CERTAIN |
| `ViperfishPerformance` ctor | `0x1c8c4840` | fills EUP=6, pop=1 | CERTAIN |
| `GhostlitePerformance` ctor | `0x1c8cbc80` | fills F32 EUP=13, BF16=14, pop=1 | CERTAIN |

---

## Latency vs Reservation: Why They Do Not Multiply

### Purpose

The push→pop pair is bounded by two independent quantities read from two different arrays. Conflating them is the single most common way to mis-schedule a transcendental, so the cost model keeps them physically separate: the **data latency** (push→pop deadline) lives in `latencies[Instruction]`; the **issue reservation** (push→push throughput) comes from `VectorEupReservationCycles` and the per-instruction `resourceUsage` grid. A reimplementer who multiplies them over-counts every EUP-bound schedule on Pufferfish.

### The Edge Passes Through Unmodified

`LatencyBetween` does not scale the edge by any reservation field. It calls the per-gen `LatencyBetweenInternal`, optionally adds uniform-random jitter (a noise model gated on a flag at `[this+0x10]`), then special-cases **only** the matres/transpose opcodes — `0x82` (matres) clamps an edge below 3 up to 2, and `0x84` (transpose) takes an MXU `AutoProto` floor — and otherwise returns the internal value verbatim. The EUP push is neither `0x82` nor `0x84`, so its edge passes through untouched:

```c
function LatencyTable::LatencyBetween(this, from, to):   // @0x1c89f820
    edge = this->LatencyBetweenInternal(from, to)        // virtual, vtable +0x18
    if noise_model_enabled([this+0x10]):
        edge += UniformRandom(0, 101)                    // optional scheduling jitter
    if from.opcode == 0x84 && to.opcode == 0x84:         // transpose-only MXU floor
        edge = max(edge, MxuProtoFloor(from))
    else if from.opcode == 0x82 && (to.opcode - 0x82) <= 2 && edge < 3:
        edge = 2                                          // matres-only floor
    return edge                                          // EUP push: returned unchanged
    // NO multiply by VectorEupReservationCycles anywhere on this path
```

### The Composition

The reservation is the EUP unit's *issue occupancy* — how many bundles the unit stays reserved after a push, applied by the bundle packer's per-instruction resource model (the `resourceUsage` grid plus the `SlotTracker`), not by the latency edge. The two constraints compose as a `max`:

| Quantity | Bounds | Source | PF | VF | GL (F32/BF16) | JF |
|---|---|---|---|---|---|---|
| push→pop data latency | min bundles push → its drain | `latencies[Get<Gen>Instr(push)]` | 7 | 6 | 13 / 14 | 4 (clamp) |
| pop drain latency | latency the drained value carries | `latencies[pop Instr]` | 1 | 1 | 1 | — |
| `VectorEupReservationCycles` | min bundles push → next push | Target accessor (`+0x480`) | 2 | 1 | 1 | 1 |

```text
bundle(pop)      >= bundle(push)   + latency                  // deadline  (e.g. push+7 on PF)
bundle(push_i+1) >= bundle(push_i) + VectorEupReservationCycles // throughput (e.g. +2 on PF)
                                                              // → effective gap = max(...), not product
```

> **QUIRK —** Pufferfish's half-rate EUP (reservation = 2) does **not** double the 7-cycle push→pop window. It halves the *issue rate*: one new push may enter the EUP every 2 bundles. The VALU-correction software-pipeline depth the late decomposer must fill is the **latency** (7), independent of the reservation. For a chain of `N` independent transcendentals on Pufferfish the EUP-bound schedule length is `≈ 2·(N−1) + 7` bundles (throughput-bound spacing between pushes, plus one final 7-cycle drain), not `7·2`. On Viperfish and Ghostlite, where reservation = 1, the two constraints coincide for back-to-back pushes and only the latency tail (6 / 13 / 14) matters.

### The `VectorEupReservationCycles` Accessor

`VectorEupReservationCycles` is a pure-virtual on `Target` at vtable slot `+0x480`. The four concrete generations return a single constant each — confirmed by reading the accessor bodies directly:

| Gen | Accessor | Returns | Confidence |
|---|---|---|---|
| Jellyfish | `JellyfishTarget::VectorEupReservationCycles` `@0x1d490660` | 1 | CERTAIN |
| Pufferfish | `PufferfishTarget::VectorEupReservationCycles` `@0x1d494cc0` | 2 | CERTAIN |
| Viperfish | `ViperfishTarget::VectorEupReservationCycles` `@0x1d49b060` | 1 | CERTAIN |
| Ghostlite | `GhostliteTarget::VectorEupReservationCycles` `@0x1d497ee0` | 1 | CERTAIN |

> **NOTE —** the `+0x480` vtable slot collides with ~160 unrelated vtables (LLVM TTI cost functions, proto facades), so the single bundle-resource call site that consumes the reservation was not isolated to one instruction (HIGH that it is the resource-model caller, not the latency edge). The orthogonality itself — that the latency edge is provably *not* scaled by the reservation — is byte-confirmed from the `LatencyBetween`/`LatencyBetweenInternal` disassembly. Which of the per-gen `Performance::Resource` columns is the EUP unit, and whether its `resourceUsage[push][EUP]` cell equals `VectorEupReservationCycles`, was read structurally only; the EUP resource row was not isolated from the ctor's template-fill (LOW), so the reservation→grid binding is the one unverified link in the model.

---

## The Result FIFO

### Purpose

The latency edge bounds where a single pop may be placed; the result FIFO bounds how many pushed-but-not-yet-popped results may be in flight at once. The two together determine the maximum software-pipeline width the scheduler can open.

### The Model

The EUP result FIFO is **not** a fixed compile-time depth. The hardware-state simulator proto holds it as a `repeated EupResultFifoEntry eup_result_fifo_entries` field — a runtime snapshot list of in-flight pushed results, each entry the per-lane result vector — so the depth is whatever the schedule has placed, not a libtpu literal. At schedule time the number of in-flight pushes is bounded by the latency edge plus the FIFO push/pop ordering (`BaseFifoTracker<LloValue*>::FindBlockingPushesAndPops` `@0x14442f60`, which calls `LatencyTable::LatencyBetween` for the FIFO-ordering edges), and at the v5+ bundle level by `HasEupRestrictions` forcing each push and its pop into separate bundles. The silicon FIFO's physical depth is a chip parameter and is not recoverable from this binary.

> **NOTE —** the push and pop are FIFO-ordered: `LloInstructionsPopAndThenPushSameFifo @0x1d4f3c80` head-checks that the pop drains the matching push. On the v5+ generations `HasEupRestrictions` is TRUE (Viperfish, Ghostlite), so the push and pop **cannot co-issue** — the late decomposer must place them in separate bundles with the latency gap between them filled by VALU correction or unrelated work. On Jellyfish and Pufferfish the restriction is FALSE, so a matching push+pop can re-fuse into a single `AndPop` bundle when the schedule allows; see [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md#fused-vs-split-the-andpop-duality).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `BaseFifoTracker<LloValue*>::FindBlockingPushesAndPops` | `0x14442f60` | FIFO push/pop ordering edges (calls `LatencyBetween`) | HIGH |
| `LloInstructionsPopAndThenPushSameFifo` | `0x1d4f3c80` | FIFO-ordering head-check pairing a pop to its push | HIGH |
| `EupResultFifoEntry` (proto ctor) | `0x0e7a6cc0` | runtime `repeated`-message FIFO list (not a fixed depth) | HIGH |

---

## How the Cost Model Consumes the Edge

The EUP latency feeds two consumers. The **dependency-graph scheduler** uses `LatencyBetween(push, pop)` as the true-dependency edge weight, placing the pop no earlier than `push_bundle + latency` and opening exactly that many bundles for correction arithmetic. The **bundle-cost transcendental estimate** (the per-op cost the higher-level cost model attributes to a `sin`/`cos`/`tanh`/etc.) is sized to the same latency: a generation's transcendental estimate scales with its EUP push latency, so the cost model and the scheduler agree on the software-pipeline depth a transcendental opens. The two numerics a reimplementer needs for that estimate — the per-gen latency integer and the correction-window contents — are owned by the per-gen and coefficient pages.

The latency value is the depth the correction window fills, and the correction window is where every polynomial on the transcendental path lives. A `tanh` on a generation with a hardware EUP pushes raw and the result is taken directly; a `recip`/`rsqrt` may wrap a Newton refinement around the push; a `sin`/`cos` first runs Payne-Hanek range reduction to produce a pushable argument; an `exp`/`ln` on a generation or datatype lacking the hardware push falls back to a full VALU polynomial. Each of those occupies some of the `N`-bundle latency window, and the per-gen latency is the budget that window has. The numerics:

- The per-gen push→pop integers, the per-opcode → `Performance::Instruction` classifier, and the full nine-function block — [EUP Per-Gen Integers](eup-per-gen-integers.md).
- The Newton/rational/`*NoEupF32` correction polynomials that fill the latency window — [EUP Correction Coefficients](eup-correction-coeffs.md).
- The trig argument reduction that precedes a `sin`/`cos` push — [EUP Payne-Hanek Range Reduction](eup-paynehanek.md).
- The packed-sub-lane unpack that determines the 1:N EUP fan-out — [EUP Lane-Width Unpack](eup-lane-width-unpack.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `Performance` family | the per-gen object holding both `latencies[]` (this edge) and the `resourceUsage[]` grid (the reservation) |
| `LatencyTable` | the per-gen dispatcher that reads `GetLatency` for the push→pop edge |
| `Target` accessors | `VectorEupReservationCycles` (the orthogonal issue rate), `HasEupRestrictions` (forces push/pop apart) |
| Bundle packer / `SlotTracker` | applies the reservation as an EUP-resource occupancy, distinct from the latency edge |

## Cross-References

- [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) — the ISA-side push/pop encoding, the fused-vs-split duality, and the latency table from the encoding view
- [EUP Per-Gen Integers](eup-per-gen-integers.md) — the per-gen push→pop latency integers, the opcode→Instruction classifier, and the full nine-function block
- [EUP Correction Coefficients](eup-correction-coeffs.md) — the Newton/rational/`*NoEupF32` polynomials that fill the latency window
- [EUP Payne-Hanek Range Reduction](eup-paynehanek.md) — the 1/(2π) trig reduction that precedes a `sin`/`cos` push
- [EUP Lane-Width Unpack](eup-lane-width-unpack.md) — the packed-sub-lane 1:N fan-out that multiplies the number of pushes
- [Performance Family Overview](performance-overview.md) — the `Performance` object layout, `GetLatency`/`GetResourceUsage` read paths, and the JF/DF inline-POD vs heap-grid split
- [VPU Slot](../isa/slot-vpu.md) — the VALU slot family the EUP push is restricted to (slot 3)
