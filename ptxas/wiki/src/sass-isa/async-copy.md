# Asynchronous Copy, mbarrier & TMA

> *Recovered by disassembling ptxas v13.1.115 (CUDA 13.1) output with
> nvdisasm V13.1.115, cross-validated against the decoded per-architecture SASS
> instruction tables. Each PTX→SASS mapping below was produced by assembling a
> probe kernel for the relevant `sm_` target and reading the emitted machine
> code. Facts and models only — the verbatim NVIDIA tables are not redistributed.*

Three generations of GPU added a *decoupled memory engine* that moves data from
global memory into shared memory without occupying the math datapath, and a
*shared-memory completion object* (the **mbarrier**) that signals when the bytes
have landed. This page recovers the full lowering of that subsystem from the PTX
the programmer writes down to the SASS the GPU executes:

- **Ampere `cp.async`** — the `LDGSTS` instruction (global→shared), the
  commit/wait *group* mechanism (`LDGDEPBAR` + `DEPBAR.LE`), and the copy→barrier
  arrive (`ARRIVES.LDGSTSBAR`, which survives through Blackwell).
- **The mbarrier object** — its packed 64-bit shared-memory layout (phase, pending
  arrivals, expected arrivals, transaction-byte count) and the Hopper `SYNCS`
  family that operates on it.
- **Hopper TMA** (`cp.async.bulk` / `cp.async.bulk.tensor`) — the `UBLKCP` /
  `UTMALDG` / `UTMASTG` / `UTMAREDG` / `UTMAPF` engine, the 128-byte tensor-map
  descriptor, multicast, im2col, and the `UTMACMDFLUSH` drain.

The whole subsystem is **compute-only**: every instruction in it carries
`VALID_IN_SHADERS = (1<<ISHADER_TRAP) | (1<<ISHADER_CS)` and is structurally
illegal in any graphics shader (see
[Legality & Capabilities](legality-and-capabilities.md)).

---

## 1. Ampere `cp.async` — `LDGSTS`

`cp.async.<cache>.shared.global` copies bytes from global memory straight into
shared memory, never passing through a register. ptxas lowers it to a single
`LDGSTS` (Load-Global-Store-Shared) instruction. Assembling the cache/size
matrix for `sm_80` and disassembling gives the exact form:

| PTX | SASS (`sm_80`) |
|---|---|
| `cp.async.ca.shared.global [s],[g],4` | `LDGSTS.E [s], [g.64]` |
| `cp.async.ca.shared.global [s],[g],8` | `LDGSTS.E.64 [s], [g.64]` |
| `cp.async.ca.shared.global [s],[g],16` | `LDGSTS.E.128 [s], [g.64]` |
| `cp.async.cg.shared.global [s],[g],16` | `LDGSTS.E.BYPASS.128 [s], [g.64]` |
| `cp.async.ca.shared.global [s],[g],16,N` | `LDGSTS.E.128.ZFILL [s], [g.64]` |
| `cp.async.cg.shared.global [s],[g],16,N` | `LDGSTS.E.BYPASS.128.ZFILL [s], [g.64]` |
| `…global.L2::128B …` | `LDGSTS.E.LTC128B.128 …` |
| `…global.L2::256B …` | `LDGSTS.E.BYPASS.LTC256B.128 …` |

The mnemonic decoration order is fixed:

```
LDGSTS.E{.BYPASS}{.LTC64B|.LTC128B|.LTC256B}{.64|.128}{.ZFILL}
```

with the following semantics, each recovered from a one-variable probe:

- **`.E`** — extended (64-bit) global address. Always present; the global pointer
  occupies a register pair (`[Rn.64]`).
- **Width (`<empty>`/`.64`/`.128`)** — the per-thread byte count: 4, 8 or 16 bytes,
  taken directly from the PTX size operand. Sixteen-byte copies are the only width
  that accepts `.cg`.
- **`.BYPASS`** — the L1 cache-bypass form. `cp.async.cg` (cache-at-global-L2-only)
  maps to `.BYPASS`; `cp.async.ca` (cache-at-all-levels) leaves it off. The two are
  distinct cache-residency policies, not just a hint: `.ca` writes the line into L1
  on its way to shared, `.cg`/`.BYPASS` does not.
- **`.ZFILL`** — out-of-bounds zero-fill. When the PTX carries the optional
  *source-size* operand `N` (the number of valid bytes when fewer than the copy
  width are in bounds), ptxas emits `.ZFILL`; the hardware copies `N` valid bytes
  and writes zeros for the remaining `copy-size − N` bytes of the shared
  destination. This is how `cp.async` boundary tiles are handled without a branch.
- **`.LTC64B`/`.LTC128B`/`.LTC256B`** — an L2-residency *prefetch* hint
  (`L2::64B`/`128B`/`256B`), independent of the L1 policy. It pulls the surrounding
  L2 sector group toward the SM so neighbouring copies hit warm.

A predicated `cp.async` lowers to a guarded `@P LDGSTS …`; the predicate gates the
copy issue, and the `.ZFILL` semantics are independent of it.

`LDGSTS` carries the primary opcode byte `0xae`. The four decoration groups map
onto four independent encoding sub-fields, each pinned by an isolated probe:

| Sub-field | Width | Values | Driven by |
|---|---|---|---|
| `loc` | 1 | `BYPASS`=0 (`.cg`), `ACCESS`=1 (`.ca`) | the PTX cache hint |
| `zfill` | 1 | `NONE`=0, `ZFILL`=1 | presence of the source-size operand |
| `sp` (L2 promotion) | 2 | `NOSP`=0, `LTC64B`=1, `LTC128B`=2, `LTC256B`=3 | `L2::{64,128,256}B` |
| `sz` (width) | 3 | `SIZE32`=4 (bare), `SIZE64`=5 (`.64`), `SIZE128`=6 (`.128`) | the byte-count operand |

The eviction class shares the same `cop` field (`EF`/`EN`/`EL`/`LU`/`EU`/`NA`) as an
ordinary `LDG` (see [Memory Model](memory-model.md#eviction-priority-cop-volta)).

### The async-copy *group* — commit and wait

`cp.async` copies are tracked as ordered **groups** on a single dedicated async
scoreboard. There are two lowering rules, confirmed by assembling interleaved
commits and waits:

| PTX | SASS |
|---|---|
| `cp.async.commit_group` | `LDGDEPBAR` |
| `cp.async.wait_group N` | `DEPBAR.LE SB0, N` |
| `cp.async.wait_all` | `DEPBAR.LE SB0, 0x0` |

`LDGDEPBAR` (Load-Global-Store-Shared **Dep-Bar**) takes no operands; it closes
the current group and advances the group counter on async scoreboard **SB0**.
`DEPBAR.LE SB0, k` blocks the warp until the number of *still-outstanding* copy
groups on SB0 is **≤ k**. So `wait_group 0` / `wait_all` blocks until every
committed copy has retired; `wait_group 2` lets the two most-recent groups stay in
flight. A three-group probe makes the counting explicit:

```
LDGSTS.E.128 …        ; group 0 body
LDGDEPBAR             ; commit group 0
LDGSTS.E.128 …        ; group 1 body
LDGDEPBAR             ; commit group 1
LDGSTS.E.128 …        ; group 2 body
LDGDEPBAR             ; commit group 2
DEPBAR.LE SB0, 0x1    ; wait_group 1 — tolerate ≤1 outstanding
DEPBAR.LE SB0, 0x0    ; wait_group 0 — drain all
```

All `cp.async` traffic funnels through **SB0**; the `LDGDEPBAR`/`DEPBAR.LE` pair
is an explicit *partial-wait* that is byte-identical from SM80 through SM120 (see
the dispatch-stall table in
[Execution Model](execution-model.md#scoreboard--barrier-model)). The `DEPBAR`
encoding carries three fields: the scoreboard index, the count threshold `k`, and
a `.LE` (less-than-or-equal) test flag; ptxas always sets `.LE` and the dedicated
async-scoreboard index for the `cp.async` path.

### Coupling a copy group to a barrier — `ARRIVES.LDGSTSBAR`

Instead of (or in addition to) the group counter, an Ampere `cp.async` batch can
signal an mbarrier when it completes. `cp.async.mbarrier.arrive[.noinc]
.shared.b64 [bar]` lowers to a dedicated arrive form:

```
LDGSTS.E.128 …                         ; the async copy
ARRIVES.LDGSTSBAR.64 [URZ+0x1000]      ; arm: signal [bar] when this copy lands
```

`ARRIVES.LDGSTSBAR` ties the LDGSTS completion to a shared-memory barrier: the warp
arrives on the named mbarrier *after* the outstanding `cp.async` copies have written
shared memory, not when the `ARRIVES` itself issues. The `.noinc` variant arrives
without incrementing the arrival count (it only registers the copy-completion
dependency).

The instruction is **not** superseded by the Hopper `SYNCS` family — it is still the
opcode ptxas emits for `cp.async.mbarrier.arrive` on SM90 through SM121, and the
`arrives_` class is present in every decoded table from SM80 onward. What changes is
which barrier field the copy completion decrements, expressed as a new modifier:

| Target | `cp.async.mbarrier.arrive` | `cp.async.mbarrier.arrive.noinc` |
|---|---|---|
| `sm_80` / `sm_89` | `ATOMS.ADD.S32` + `ARRIVES.LDGSTSBAR.64` | `ARRIVES.LDGSTSBAR.64` |
| `sm_90a` / `sm_100a` / `sm_120a` | `SYNCS.ARRIVE.TRANS64.RED.A0T1` + `ARRIVES.LDGSTSBAR.64.TRANSCNT` | `ARRIVES.LDGSTSBAR.64.ARVCNT` |

On Ampere/Ada the arrival count is bumped by a separate shared-memory atomic
(`ATOMS.ADD.S32`) because the barrier word has no transaction channel. On
Hopper/Blackwell the plain form raises the transaction-byte expectation by one
(`SYNCS.ARRIVE.TRANS64.RED.A0T1`) and the copy retires against the **transaction**
counter (`.TRANSCNT`); the `.noinc` form retires against the **arrival** counter
(`.ARVCNT`) instead. So `ARRIVES` is the ancestor *and* the surviving mechanism, with
`SYNCS` supplying the transaction-byte accounting around it.

---

## 2. The mbarrier object

An mbarrier is a **64-bit object that lives in shared memory** and tracks two
things at once: a *thread-arrival* count (how many participants have arrived) and
a *transaction-byte* count (how many bytes a pending async copy still owes). When
both are satisfied the **phase bit** flips and any waiter on the old phase is
released. The PTX type is `.b64`; the SASS that manipulates it differs by
generation.

On **Ampere** the object is an opaque 64-bit shared word touched by ordinary
shared-memory atomics — no dedicated barrier engine exists yet. The three
operations lower to:

| PTX (Ampere) | SASS (`sm_80`) |
|---|---|
| `mbarrier.init.shared.b64 [b],C` | `STS.64 [b], R` + `MEMBAR.ALL.CTA` |
| `mbarrier.arrive.shared.b64 r,[b]` | `ATOMS.ARRIVE.64 RZ, [b]` |
| `mbarrier.test_wait.shared.b64 p,[b],s` | `LDS …` (read state, compare in ALU) |

`ATOMS.ARRIVE` is a shared-memory atomic with an `ARRIVE` reduction op (the
named-barrier atomic also used by `bar`-class arrivals); there is no
transaction-byte channel, so an Ampere mbarrier only counts arrivals. **Hopper**
replaces this with a purpose-built barrier engine, the `SYNCS` family, which adds
the transaction-byte counter that TMA copies decrement (§3). The rest of this
section describes that Hopper/Blackwell `SYNCS` form.

### Packed 64-bit layout — recovered from `mbarrier.init`

`mbarrier.init.shared.b64 [bar], count` lowers to a single 64-bit shared store.
Varying `count` and reading the init word out of the disassembly fixes the
encoding exactly:

```
; mbarrier.init.shared.b64 [bar], C
UIADD3 t,  -C, 0x100000, URZ     ; t = 2^20 - C
USHF.L  hi, t, 0xb,  URZ         ; hi-word field = t << 11   (overall bit 43)
USHF.L  lo, t, 0x1,  URZ         ; lo-word field = t << 1    (overall bit 1)
SYNCS.EXCH.64 URZ, [bar], {lo,hi}   ; atomically write the packed init word
```

Both arrival fields are stored as the **down-counting complement** `2^20 − count`,
so a 20-bit field counts *up* toward `2^20` (zero crossing = satisfied) rather than
down toward zero:

- The **pending-arrival** count occupies a 20-bit field starting at **bit 1** of
  the word (`t << 1`).
- The **expected-arrival** count — the reset value reloaded after each phase flip
  — occupies a 20-bit field starting at **bit 43** (`t << 11` within the high
  32-bit half).
- The remaining low bit (**bit 0**) is the **phase** bit; the high bits above the
  expected field hold the **transaction-byte** counter, which `mbarrier.init`
  leaves at zero.

`mbarrier.inval.shared.b64 [bar]` lowers to `SYNCS.CCTL.IV [bar]` — it invalidates
the object's cache state so the shared slot can be reused for non-barrier data.

### Arrive, expect-tx, and wait — the `SYNCS` family

Assembling each mbarrier op for `sm_90a` and `sm_100a` yields an identical mapping
on both:

| PTX | SASS (`sm_90a` / `sm_100a`) | op byte |
|---|---|---|
| `mbarrier.init.shared.b64 [b],C` | `SYNCS.EXCH.64 URZ, [b], packed` | `0xb2` |
| `mbarrier.inval.shared.b64 [b]` | `SYNCS.CCTL.IV [b]` | `0xb1` |
| `mbarrier.arrive.shared.b64 r,[b]` | `SYNCS.ARRIVE.TRANS64 RZ, [b], R` | `0xa7` |
| `mbarrier.arrive.expect_tx.shared.b64 r,[b],N` | `SYNCS.ARRIVE.TRANS64.A1T0 RZ, [b], N` | `0xa7` |
| `mbarrier.expect_tx.shared.b64 [b],N` (drop-form) | `SYNCS.ARRIVE.TRANS64.RED.A0TR RZ, [b], N` | `0xa7` |
| `mbarrier.try_wait.parity.shared.b64 p,[b],ph` | `SYNCS.PHASECHK.TRANS64.TRYWAIT P, [b], ph` | `0xa7` |
| `mbarrier.test_wait.parity.shared.b64 p,[b],ph` | `SYNCS.PHASECHK.TRANS64 P, [b], ph` | `0xa7` |

The `TRANS64` decoration marks the instruction as operating on the 64-bit
transaction-counting barrier object. The arrive modifiers encode *which* fields
the instruction touches:

- **`SYNCS.ARRIVE.TRANS64`** — a plain arrival; decrements the pending-arrival
  field by one (advancing the up-counter).
- **`.A1T0`** (*Arrive-1, Tx-0 set*) — `arrive.expect_tx`: register one arrival
  **and** raise the transaction-byte count by `N`, so the barrier will not flip
  until `N` more bytes have been delivered by async copies. This is the producer
  half of a TMA pipeline (§3).
- **`.RED.A0TR`** (*Reduce, Arrive-0, Tx-Raise*) — the `expect_tx`-only form:
  add to the transaction-byte expectation without registering a thread arrival.
- **`SYNCS.PHASECHK.TRANS64{.TRYWAIT}`** — read the phase bit and compare it to the
  expected parity `ph`, returning a predicate. `.TRYWAIT` is the non-blocking
  `try_wait` (returns immediately with the current truth value, for spin loops);
  the plain form is `test_wait`. A waiter loops `SYNCS.PHASECHK … @!P bra back`
  until the phase flips.

### Single-thread issue — `ELECT`

Operations that must run on exactly one thread of the warp (arming a barrier,
issuing a bulk copy) are guarded by **`ELECT`**, the warp leader-election
instruction:

```
@P0 ELECT P1, URZ, PT    ; P1 = (this lane is the elected leader)
@P1 …                    ; only the leader proceeds
```

`ELECT` returns a predicate true in exactly one active lane (and the elected
lane id in a uniform register). ptxas wraps the descriptor build, the
`expect_tx` arm, and the `UBLKCP`/`UTMALDG` issue in an `ELECT` guard so the
single-issue semantics of the async engine are respected without the programmer
writing the election by hand.

---

## 3. Hopper TMA — `cp.async.bulk` and `cp.async.bulk.tensor`

The Tensor Memory Accelerator (TMA) is a dedicated copy engine that moves *bulk*
regions — contiguous byte ranges or multi-dimensional tensor tiles — between
global and shared memory, addressed once per warp through a uniform-register
path and completed through an mbarrier's transaction-byte counter. ptxas exposes
two instruction sub-families: the **`UBLK*`** ops for untyped byte ranges and the
**`UTMA*`** ops for tensor tiles described by a tensor-map descriptor.

### Untyped bulk copies — `UBLKCP` / `UBLKRED` / `UBLKPF`

Assembling the `cp.async.bulk` matrix for `sm_90a`:

| PTX | SASS |
|---|---|
| `cp.async.bulk.shared::cluster.global.mbarrier… [s],[g],N,[b]` | `UBLKCP.S.G [s], [g], size` |
| `cp.async.bulk.global.shared::cta.bulk_group [g],[s],N` | `UBLKCP.G.S [g], [s], size` + `UTMACMDFLUSH` |
| `cp.async.bulk.prefetch.L2.global [g],N` | `UBLKPF.L2 [g], size` |
| `cp.reduce.async.bulk.global.shared::cta.add.u32 [g],[s],N` | `UBLKRED.G.S.ADD [g], [s], size` |

`UBLKCP.<dst>.<src>` names its direction by the **destination then source** memory
space: `.S.G` is global→shared (the load), `.G.S` is shared→global (the store).
The shared address fed to the engine is a *cluster-mapped DSMEM* address built from
the CTA rank and the shared offset:

```
S2UR UR, SR_CgaCtaId          ; this CTA's rank within the cluster
ULEA UR_addr, UR, UR_off, 0x18 ; DSMEM addr = (rank << 0x18) | shared_offset
```

The `<< 0x18` (24-bit) shift packs the CTA rank into the high bits of the shared
address, which is how a `shared::cluster` copy targets another CTA's shared memory
inside the cluster. A shared→global `UBLKCP.G.S` or a `UBLKRED` is followed by
**`UTMACMDFLUSH`** (op `0xb7`), which drains the TMA command queue so the subsequent
`DEPBAR.LE SB0, 0x0` (`cp.async.bulk.wait_group 0`) observes completion. The
`.read` wait variant (`cp.async.bulk.wait_group.read`) additionally emits
`CCTL.IVALL` to invalidate the source so the buffer can be reused.

The bulk-copy opcode bytes (low byte of the 128-bit word) are `UBLKCP` = `0xba`,
`UBLKPF` = `0xbc`; the issue is wrapped in an `ELECT P0, URZ, PT` (op `0x2f`)
leader guard so exactly one lane launches the engine.

### Tensor copies — `UTMALDG` / `UTMASTG` / `UTMAREDG` / `UTMAPF`

`cp.async.bulk.tensor.<rank>d` copies a tile of a multi-dimensional tensor using a
descriptor (the tensor-map) plus per-call tile coordinates. The matrix for
`sm_90a` (identical on `sm_100a`):

| PTX | SASS | op byte |
|---|---|---|
| `…bulk.tensor.2d.shared::cluster.global.tile.mbarrier…` | `UTMALDG.2D [s], [desc]` | `0xb4` |
| `… .multicast::cluster` | `UTMALDG.2D.MULTICAST [s], [desc], mask` | `0xb4` |
| `…bulk.tensor.5d.im2col.shared::cluster.global…` | `UTMALDG.5D.IM2COL [s], [desc], off` | `0xb4` |
| `…bulk.tensor.2d.global.shared::cta.tile…` | `UTMASTG.2D [desc], [s]` | `0xb5` |
| `cp.reduce.async.bulk.tensor.2d…add.tile…` | `UTMAREDG.2D.ADD [desc], [s]` | `0xb6` |
| `cp.async.bulk.prefetch.tensor.2d.L2.global.tile…` | `UTMAPF.L2.2D [desc]` | `0xb8` |

The descriptor is read once into a uniform-register pointer
(`ULDC.64 UR, c[0x0][0x210]` — the kernel-parameter bank on Hopper) and the
instruction is issued under an `ELECT` guard so exactly one thread launches it:

```
@P0 ELECT P1, URZ, PT
    UTMALDG.2D [smem], [desc]        ; one elected thread issues the tile load
```

The decoration encodes the geometry directly:

- **`.2D` / `.3D` / …`.5D`** — tensor rank, from the PTX `.<n>d` modifier.
- **`.TILE`** (implicit) vs **`.IM2COL`** — the access pattern. `IM2COL` is the
  convolution-lowering mode that gathers an implicit `im2col` patch from a higher-
  rank tensor; it takes extra per-call offset operands (the convolution corner
  offsets) beyond the box coordinates.
- **`.MULTICAST`** — the cluster-broadcast load. A `cp.async.bulk.tensor
  .multicast::cluster` reads the tile **once** from global memory and writes it
  into the shared memory of every CTA selected by the 16-bit CTA-mask operand,
  saving global bandwidth across the cluster.
- **`.ADD` / `.MIN` / …** on `UTMAREDG` — the reduction operator applied when
  storing shared→global, the SASS half of `cp.reduce.async.bulk.tensor`.

Completion is signalled through the mbarrier whose address is passed in the PTX
(`mbarrier::complete_tx::bytes`): the producer thread first arms the barrier with
`SYNCS.ARRIVE.TRANS64.A1T0 …, N` (expecting `N` transaction bytes), then issues the
`UTMALDG`; as the tile lands the engine decrements the barrier's transaction-byte
counter, and when it reaches zero (and arrivals are satisfied) the phase flips,
releasing the consumer's `SYNCS.PHASECHK.TRANS64.TRYWAIT` wait loop. A full
producer→consumer pipeline disassembles to:

```
SYNCS.EXCH.64 URZ, [bar], packed        ; mbarrier.init
SYNCS.ARRIVE.TRANS64 RZ, [bar], 0x4000  ; arrive + expect_tx = 16384 bytes
@P0 ELECT P1, URZ, PT
    UTMALDG.2D [smem], [desc]            ; issue tile load (one thread)
WAIT:
SYNCS.PHASECHK.TRANS64.TRYWAIT P0, [bar], phase
@!P0 BRA WAIT                           ; spin until the tile has landed
```

### The 128-byte tensor-map descriptor

The tensor-map is a **128-byte (1024-bit) opaque structure**. ptxas does not
build it on-device for static cases — it is normally produced host-side — but the
`tensormap.replace` PTX ops *do* lower to plain shared loads/stores with bitfield
masking, which exposes the byte offsets of each field. Assembling each
`tensormap.replace.tile.<field>` in isolation for `sm_90a` and reading the `STS`
offset recovers the layout:

| Field | PTX op | Byte offset | Encoding |
|---|---|---:|---|
| Global address | `global_address` (b64) | `0x00`–`0x07` | 64-bit base pointer (split lo at `0x00`, hi merged at `0x0c`) |
| Packed flags | (rank / elem-type / interleave / swizzle) | `0x08` | bitfields, masks `0x780` (elem-type), `0x2000`, `0x400000`, `0x8000` |
| Global stride[i] | `global_stride` (b64) | `0x0c`, `0x10`, … | per-dimension stride, stored **÷16** (the low 4 bits are dropped — strides are 16-byte-aligned) |
| Element stride / fill | `element_stride`, `fill_mode` | `0x1c`, `0x34` | packed bitfields |
| Global dim[i] | `global_dim` (b32) | `0x20`, `0x24`, `0x28`, … | one 32-bit extent per dimension |
| Box dim[i] | `box_dim` (b32→16) | `0x34`, `0x36`, `0x38`, … | one 16-bit tile extent per dimension, packed two per word |
| Swizzle / OOB-fill mode | `swizzle_mode`, `fill_mode` | `0x34` region | small bitfields, masks `0x3f000000`, `0x7` |

So the descriptor encodes everything the engine needs to walk a tile: the global
base and per-dimension **global strides** (in 16-byte units) and **global extents**
(`global_dim`), the **box dimensions** of the tile to fetch, the **element stride**,
the **swizzle mode** (the shared-memory bank-conflict-avoiding layout applied as
the tile lands), the **interleave** layout, and the **OOB-fill mode** (whether
out-of-bounds tile elements are zero-filled or take a constant — the tensor-tile
analogue of `cp.async`'s `.ZFILL`).

Publishing a descriptor that was modified on-device requires a proxy fence:
`tensormap.cp_fenceproxy …` lowers to an `ATOMG.E.EXCH.STRONG.GPU` (a strong-scope
exchange) that flushes the generic-proxy write of the descriptor so the TMA engine,
which reads it through a different memory proxy, observes the new bytes. The
acquire half — `fence.proxy.tensormap::generic.acquire …` — lowers instead to
**`UTMACCTL.IV`** (op `0xb9`), which invalidates the TMA's tensor-map proxy cache
so a subsequent tile op re-reads the freshly published descriptor.

---

## 4. Named barriers and cluster barriers (for contrast)

The async barrier object (§2) is distinct from the classic *named/CTA barrier* and
the *cluster barrier*, which share the `BAR` and `UCGABAR` opcode space:

| PTX | SASS (`sm_90a`) |
|---|---|
| `bar.sync 0` | `BAR.SYNC.DEFER_BLOCKING 0x0` |
| `bar.arrive 0, N` | `BAR.ARV 0x0, N` |
| `barrier.cluster.arrive` | `UCGABAR_ARV` |
| `barrier.cluster.wait` | `UCGABAR_WAIT` |

`BAR.SYNC` is the whole-CTA convergence barrier; the `.DEFER_BLOCKING` modifier is
the Hopper form that lets the warp defer the block so independent work can issue
before the barrier actually stalls. `BAR.ARV` is the split-barrier arrive (no
wait). `UCGABAR_ARV`/`UCGABAR_WAIT` are the cluster-wide (CGA) barrier arrive/wait,
paired with `CGAERRBAR` (the cluster error-barrier that drains the cluster memory
error pipe). These are *count-based* barriers with no transaction-byte channel —
they synchronise threads, not async byte deliveries.

---

## 5. How ptxas sequences async ops against the scoreboard

Two orthogonal completion mechanisms cover the whole subsystem:

1. **Group / scoreboard completion** (`cp.async`, `cp.async.bulk` without an
   mbarrier). The copies are tracked as ordered groups on async scoreboard **SB0**;
   `LDGDEPBAR` (or the implicit `bulk_group` commit) advances the group counter and
   `DEPBAR.LE SB0, k` waits the count down. `UTMACMDFLUSH` first drains the TMA
   command queue so the `DEPBAR` sees the right count. This is the low-overhead path
   when the consumer is in the same warp and a simple FIFO ordering is enough.

2. **Transaction-byte completion** (`cp.async.bulk[.tensor].mbarrier…` and
   `cp.async.mbarrier.arrive`). The copy carries the address of an mbarrier;
   the producer arms the expected byte count (`SYNCS.ARRIVE.TRANS64.A1T0`, or the
   `ARRIVES.LDGSTSBAR.64.TRANSCNT` form for a `cp.async` batch), the engine
   decrements the byte counter as data lands, and a separate consumer warp is
   released by the **phase flip** it observes through `SYNCS.PHASECHK`. This
   decouples the producer and consumer entirely — the consumer never touches the
   scoreboard, only the barrier object. On SM80/SM89 the same PTX takes the
   arrival-only path instead: there is no transaction channel in the barrier word,
   so `ARRIVES.LDGSTSBAR.64` pairs with an `ATOMS` arrival update.

ptxas chooses the mechanism from the PTX form (a `.mbarrier::complete_tx::bytes`
qualifier selects path 2, a `.bulk_group`/`.commit_group` selects path 1), wraps
single-issue ops in `ELECT`, inserts `UTMACMDFLUSH` before any group-wait that
follows a shared→global bulk op, and emits the proxy fence
(`ATOMG.E.EXCH.STRONG.GPU`) for any on-device descriptor it must publish. The async
scoreboard SB0 and the per-instruction wait-mask in the scheduling-control word are
the two hardware resources it allocates; everything else — the barrier object, the
transaction counter, the DSMEM cluster mapping — is built in shared memory by the
instruction stream itself.

---

## Architecture coverage

| Capability | Turing SM75 | Ampere SM80/86 | Ada SM89 | Hopper SM90 | Blackwell SM100/103/110 | Blackwell SM120/121 |
|---|---|---|---|---|---|---|
| `cp.async` / `LDGSTS` | — | **yes** | yes | yes | yes (`desc[...]` addr) | yes |
| `LDGDEPBAR` / `DEPBAR.LE SB0` groups | — | yes | yes | yes | yes | yes |
| `ARRIVES.LDGSTSBAR` (copy→barrier) | — | **yes** | yes | yes (`.TRANSCNT`/`.ARVCNT`) | yes | yes |
| mbarrier via `ATOMS.ARRIVE` (arrivals only) | — | yes | yes | (replaced by `SYNCS`) | (replaced by `SYNCS`) | (replaced by `SYNCS`) |
| mbarrier `SYNCS.ARRIVE/PHASECHK.TRANS64` (+tx) | — | — | — | **yes** | yes | yes |
| `UBLKCP`/`UBLKRED`/`UBLKPF` (bulk) | — | — | — | **yes** | yes | yes |
| `UTMALDG`/`UTMASTG`/`UTMAREDG`/`UTMAPF` (TMA) | — | — | — | **yes** | yes | yes |
| `.MULTICAST` / `.IM2COL` | — | — | — | **yes** | yes | yes |
| `.GATHER4` / `.SCATTER4` (`tile::gather4`/`scatter4`, `a`/`f` target) | — | — | — | — | **yes** | **—** |
| `UCGABAR` cluster barriers | — | — | — | **yes** | yes | yes |
| `USETMAXREG` (`setmaxnreg`, arch-conditional target) | — | — | — | **yes** (`sm_90a`) | yes | yes |

Class counts from the decoded per-architecture tables agree with the emission
probes: `utma*` 15, `ublk*` 6, `syncs*` 9, `cgabar*` 4, `elect*` 2, `usetmaxreg*`
4 and `usetshmsz*` 3 on **every** target from SM90 through SM121, and **zero** of
each on SM75/SM80/SM86/SM89. `ldgsts*` is 5 on SM80 through SM121 and 0 on SM75.
Ada (SM89) therefore has the Ampere `cp.async` path and none of the Hopper async
stack: ptxas rejects `cp.async.bulk[.tensor]`, `mbarrier.arrive.expect_tx`,
`elect`, `stmatrix`, `mapa`, `%cluster_ctarank` and `barrier.cluster.*` for
`sm_89` with *"requires .target sm_90 or higher"*, and `setmaxnreg` with *"not
supported on .target 'sm_89'"*.

Consumer Blackwell (SM120/121) carries the complete Hopper async stack — the same
TMA, bulk-copy, transaction-barrier, cluster and register-reconfiguration
instructions the datacenter parts use. The one async-family gap measured is
`tile::gather4`/`tile::scatter4`: ptxas accepts these on `sm_100a`/`sm_100f`,
`sm_103a`/`sm_103f` and `sm_110a`/`sm_110f`, and rejects them on
`sm_120a`/`sm_120f`/`sm_121a`/`sm_121f` with *"Feature '.tile::gather4 with
destination state space as .shared::cluster' not supported"*. Only the
tensor-core families (`tcgen05`, TMEM) are absent outright; see
[Architecture Evolution](architecture-evolution.md#b-datacenter-and-consumer-blackwell-are-not-the-same-chip).

Targeting note: most of this subsystem assembles for the plain
`sm_90`/`sm_100`/`sm_120`/`sm_121` targets — TMA tile/multicast/im2col copies,
bulk copies, mbarriers, `elect`, clusters and DSMEM all do. Two members require an
arch-conditional (`a`/`f`) target on every architecture that has them:
`setmaxnreg` and `tile::gather4`/`scatter4`; the base targets reject both.

On Blackwell (`sm_100a`) the mbarrier and TMA instruction families are encoded
identically to Hopper; the visible delta in the disassembly is that ordinary
`LDGSTS` switches to the **descriptor-addressed** form
`LDGSTS.E … [Rd], desc[UR][Rsrc.64]`, where the global access goes through a
uniform-register memory descriptor rather than a bare 64-bit pointer — the same
descriptor-addressing change that the rest of the Blackwell global-memory path
adopts.

---

## Cross-References

- [Execution Model](execution-model.md#scoreboard--barrier-model) — the async
  scoreboard SB0, the `DEPBAR.LE` wait-down, and the per-warp dispatch rule that
  sequences these ops.
- [Memory Hierarchy & Ordering](memory-model.md) — how the `cp.async`/TMA path
  relates to the `MEMBAR` scope ladder and the LSU/coalescer model.
- [Legality & Capabilities](legality-and-capabilities.md) — the
  `VALID_IN_SHADERS = TRAP|CS` gate that makes the whole subsystem compute-only.
- [Architecture Evolution](architecture-evolution.md) — the SM75→SM121 ISA diff
  that places `LDGSTS` at Ampere and the TMA/`SYNCS`/cluster families at Hopper.
- [GMMA/WGMMA Pipeline](../passes/gmma-pipeline.md) — the tensor-core consumer that
  these async copies feed.
