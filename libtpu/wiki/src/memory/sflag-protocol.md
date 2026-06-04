# The SFLAG Sync-Flag Memory Protocol

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim and `.text` VMA == file offset. Other versions will differ.*

## Abstract

SFLAG is the TPU's atomic synchronisation tier: a small, word-granular register file (`MemorySpace::kSflag` = 6, distinct from HBM/VMEM/CMEM/SMEM — see the [memory hierarchy overview](overview.md)) that every engine spins on to coordinate DMAs, cross-engine handoffs, collectives, and barriers. A flag is addressed by a flat **sync-flag number** within a per-core table; the compiler never touches SFLAG bytes directly — it builds an `LloValue` pointer with `LloRegionBuilder::SflagImmPtr(number, name)` and then actuates it through a fixed family of `Vsync*` / `Vwait*` region-builder primitives that lower to the `mlir::llo` `VSync*Op` / `VWaitGeOp` instruction classes.

This page owns four things and only those. **(1) The memory model:** SFLAG is a flat array of 32-bit words; `SflagImmPtr` turns a flag number `n` into a `kSflag`-tagged pointer at `byte_offset = 4·n` over a 1-element S32 shape, so flag-number arithmetic is exact and word-granular. **(2) The flag-value semantics:** a flag carries a saturating counter *or* a one-shot done-bit, selected per write by the `0x100` done bit and gated per generation by `Target::HasExtraDoneBitInSyncFlags()`; whether the hardware *interprets* the word as a counter or a boolean is the `SyncFlagCountModeBitOffset` (bit 272 on Viperfish-non-lite/Ghostlite, 0 elsewhere). **(3) The actuation primitives:** `VsyncSet` (write), `VsyncAdd` / `VsyncAddDone` (atomic increment, with/without done), `VwaitGeSV` (block until ≥ threshold) and its `Eq`/`Ne`/`Lt`/`Done` siblings, `VsyncRead` (pop the counter into a scalar), plus the remote and public-access variants. **(4) `SflagImmPtr`** itself and the per-gen dummy/count constants it consumes.

Out of scope and owned elsewhere: *which* flag number a barrier binds to is [Barrier → SFLAG Number Binding](../barrier/barrier-to-sflag-binding.md); the cross-core barrier datapath that drives these primitives is the [barrier subsystem](../barrier/overview.md) and specifically the [tree-barrier Vsync emitter](../barrier/tree-barrier-vsync.md); the remote-DMA sflag-address encoders are [remote-sflag encoders](../barrier/remote-sflag-encoders.md). This page is the substrate those pages stand on.

| | |
|---|---|
| **Memory space** | `xla::jellyfish::MemorySpace::kSflag` = 6 (operand-space tag, baked into the pointer by `SflagImmPtr`) |
| **Word** | 32-bit (S32); `byte_offset = 4 · sync_flag_number` |
| **Pointer ctor** | `LloRegionBuilder::SflagImmPtr(int number, string_view name)` @ `0x1d5185a0` |
| **BarnaCore pointer ctor** | `LloRegionBuilder::BarnaCoreSflagImmPtr(int, string_view)` @ `0x1d538400` |
| **Set** | `LloRegionBuilder::VsyncSet(LloValue* sf, LloValue* v, optional<bool> done)` @ `0x1d54cce0` → `CreateVectorSyncFlagSet` |
| **Add** | `LloRegionBuilder::VsyncAdd(sf, v)` @ `0x1d523200` → `CreateVectorSyncFlagAdd(flag=0)` |
| **Add+done** | `LloRegionBuilder::VsyncAddDone(sf, v)` @ `0x1d54e380` → `CreateVectorSyncFlagAdd(flag=257)` |
| **Wait ≥** | `LloRegionBuilder::VwaitGeSV(sf, thr, bool yieldable)` @ `0x1d522f80` → `CreateVectorWait(391 + 8·yieldable)` |
| **Read** | `LloRegionBuilder::VsyncRead(sf)` @ `0x1d524220` → `PushSyncFlagToFifo` + (`PopSfrFifo` \| `CreateScalarV2SPop`) |
| **Dummy number** | `Target::GetDummySyncFlagNumber()` (vtable `+0x4F8`/idx 159) — JF = 7, PF/VF/GL = 0 |
| **Count-mode bit** | `Target::SyncFlagCountModeBitOffset()` — 272 (VF-non-lite/GL), 0 (JF/PF/VF-lite) |
| **Done-bit support** | `Target::HasExtraDoneBitInSyncFlags()` (per-gen predicate) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## 1. The Memory Model — a flat array of 32-bit words

### Purpose

A reimplementer needs exactly one fact to address SFLAG: a sync-flag number is an index into an array of 32-bit words, and the byte offset of flag `n` is `4·n`. There is no per-flag descriptor, no alignment slack, no sub-word packing at the pointer level — the packing that exists (§4) lives in the *operand encoding* of the instructions, not in the address. The number space itself (which numbers are reserved, which are allocatable) is fixed by a partition on the `Target` object and is documented for the barrier slots in [Barrier → SFLAG Number Binding](../barrier/barrier-to-sflag-binding.md); here we only need that a number resolves to a word address.

### `SflagImmPtr` — number → pointer

`LloRegionBuilder::SflagImmPtr` @ `0x1d5185a0` is the sole constructor of an SFLAG `LloValue` pointer. Decompiled, it builds a validated 1-element S32 shape and calls the generic `ImmPtr` with the byte offset and the SFLAG operand-space id:

```c
function SflagImmPtr(builder, int number, string_view name):    // 0x1d5185a0
    shape = ShapeUtil::MakeValidatedShape(/*element_type=*/4,    //   4 == S32 (xla::PrimitiveType)
                                          /*dims=*/0, /*minor_to_major=*/0)
    //                  byte_offset       shape  space  name
    return builder.ImmPtr(4 * number,     shape, 6,     name)    //   space 6 == MemorySpace::kSflag
```

Two numbers are central and both are confirmed byte-exact:

- **Element type `4` = S32.** Every sync flag is a 32-bit integer word. The shape is rank-0 (a scalar), so an SFLAG pointer names exactly one word.
- **`byte_offset = 4 · number`** (the `4LL * a2` argument). The factor of four *is* the word stride; it is why `SflagWordSizeBytes()` (`Target+0x504`, see the [overview's allocator matrix](overview.md#3-the-per-space-allocator--alignment-matrix)) is the SFLAG granule and alignment, and why `SflagWordSizeLog2()` (`Target+0x4c8`) is cached so the address arithmetic can be a shift rather than a multiply.

The space argument `6` **is** `MemorySpace::kSflag` itself — `SflagImmPtr` stamps the operand-space tag directly into the pointer. This is the same `6` every primitive in §3 checks for (`(*((_BYTE*)sf+11)>>2)&0x1F == 6`), and it is byte-confirmed against the structurally identical `BarnaCoreSflagImmPtr` @ `0x1d538400`, which passes space `0xA` (= `kBarnaCoreSflag` = 10). There is no separate "render id" baked here: the *DMA driver-resource* id that the descriptor path renders for sflag is a different number entirely (`sflag(6) → 0`), produced only at the descriptor boundary by `MemorySpaceToDriverResource`, never by this constructor (see the [enum correction](overview.md#2-the-memoryspace-enum)).

> **NOTE —** `BarnaCoreSflagImmPtr` @ `0x1d538400` is the structurally identical constructor for the **BarnaCore** sync-flag tier (`MemorySpace::kBarnaCoreSflag` = 10; the constructor passes space `0xA` to `ImmPtr`). It is a physically separate register file with its own size accessor (`Target::BarnaCoreSflagSizeBytes()`, `Target+0x478`, guarded by the `HasBarnaCore` vtable predicate). Code that targets BarnaCore flags must use this constructor; the two pointer families are never interchangeable.

### Why a pointer, not a number

Every actuation primitive in §3 takes an `LloValue*` sync-flag operand, never a raw integer. The reason is the memory-space CHECK: each primitive asserts `sync_flag->memory_space() == MemorySpace::kSflag` (decoded below), so the flag must arrive already typed as an SFLAG pointer. The check reads the operand's space tag out of the `LloValue` header (`(*((_BYTE*)sf + 11) >> 2) & 0x1F`) and compares it against the constant `kSflag` via `LloCheckForFailure<MemorySpace, MemorySpace, LloCheckOp::Eq>`. A pointer built by `SflagImmPtr` passes; any other operand fails the compile with `"sync_flag->memory_space() == MemorySpace::kSflag"` and a mnemonic dump of the offending value.

---

## 2. The Flag-Value Semantics — counter vs. done-bit

### Purpose

An SFLAG word is not a plain integer to the rest of the system: it is interpreted either as a **saturating counter** (the producer adds, the consumer waits for a threshold) or as a **one-shot done-bit** (the producer marks done, the consumer waits-done). Which interpretation applies is chosen *twice* — once per write (the instruction-level done bit) and once per generation (the hardware count-mode bit). Getting this wrong silently deadlocks: a consumer waiting on a counter threshold against a producer that set only the done-bit will spin forever.

### The two value modes

| Mode | Producer | Consumer | Use |
|---|---|---|---|
| **Counter** | `VsyncAdd` (atomic +) / `VsyncSet` to N | `VwaitGeSV(thr)` — block until value ≥ thr | N-way fan-in: N producers each `VsyncAdd 1`, one consumer `VwaitGe N` |
| **Done** | `VsyncSet(v, done=true)` / `VsyncAddDone` | `VwaitDone` | 1:1 producer/consumer completion handshake |

### The instruction-level done bit

The done-bit is carried as bit `0x100` (= 256) of the instruction flag word. The decompile pins both producers exactly:

- `VsyncAdd` @ `0x1d523200` emits `CreateVectorSyncFlagAdd(sf, v, /*flag=*/0, …)` — flag word `0`, a pure counter increment.
- `VsyncAddDone` @ `0x1d54e380` emits `CreateVectorSyncFlagAdd(sf, v, /*flag=*/257, …)` — flag word `0x101` = `0x100` (done) | `0x1` (a co-set low bit). The single bit `0x100` is the done selector; the same `0x101` mask appears as the done predicate inside `VsyncSet`.
- `VsyncSet` @ `0x1d54cce0` takes an `optional<bool> update_done_to`. When the optional is present (its `0x100` discriminant set), the builder asserts the target supports it before emitting:

```c
// VsyncSet @ 0x1d54cce0 — done-bit guard (lines ~47-71)
if (update_done_to.has_value() && !target().HasExtraDoneBitInSyncFlags()):
    CHECK_FAIL("!update_done_to.has_value() || target().HasExtraDoneBitInSyncFlags()")
              << "Done bit not supported for this target."        // llo_region_builder.cc:8061
emit CreateVectorSyncFlagSet(sf, value, flag_word, …)              // line 73
```

> **GOTCHA —** the done-bit is a *per-target capability*, not universal. `Target::HasExtraDoneBitInSyncFlags()` is a virtual predicate (reached as `target().…` through the module's target pointer). On a generation that returns false, *any* `VsyncSet` with a done value, or any `VwaitDone`, is a hard compile failure (`"Done bit not supported for this target."`, `llo_region_builder.cc:8061`). A reimplementation that always emits done-style completions will not compile on targets without the extra done bit; counter-mode (`VsyncAdd` + `VwaitGe`) is the portable lowering.

### The hardware count-mode bit (272)

Whether the *hardware* register treats a flag word as a counter or a done-bit is selected by `Target::SyncFlagCountModeBitOffset()` — a **bit position** within the multi-word SFLAG register encoding. Decoded per generation:

| Generation | `SyncFlagCountModeBitOffset()` | Addr | Interpretation |
|---|---:|---|---|
| Jellyfish (v2) | **0** | `0x1d491380` | count-mode bit not used; mode implicit |
| Pufferfish (v4) | **0** | `0x1d495860` | mode implicit |
| Viperfish (v5) | **272** non-lite / **0** lite | `0x1d49bca0` | `codename=="lite" ? 0 : 272` |
| Ghostlite (v6e) | **272** (`0x110`) | `0x1d4988c0` | count-mode bit at register bit 272 |

The Viperfish accessor (`0x1d49bca0`) decodes as: return `272` unless the codename string is the 4-character `"lite"` — it first tests the string *length* against `4` (`v2 != 4 → return 272`), then, for a 4-char string, compares the leading dword against `1702127980` (= `0x6574696c`, `"lite"` little-endian) and returns `0` only on a match. So viperfish-lite disables the count-mode bit and viperfish-std keeps it — the same lite-codename string-compare fork the VMEM and barrier pages observe. (The `4` is the codename string length, **not** a `TpuVersion` ordinal.) The value `272` is a bit index, not a byte offset: it selects "count mode vs. done mode" within the wider hardware register that backs each flag.

> **[LOW]** The full bit-field map of the multi-word SFLAG register where bit 272 sits — the counter width, the done bit, the public-access bit — is **not** decoded here. Bit 272 is CONFIRMED as the count-mode selector returned by the per-gen accessor; the surrounding fields are not enumerated and would require decoding the LLO→ISA lowering of the `VSync*Op` register encoders.

---

## 3. The Actuation Primitives — `Vsync*` / `Vwait*`

### Purpose

All SFLAG mutation and observation flows through one family of `LloRegionBuilder` methods. They share a fixed shape: take an `LloValue*` sync-flag pointer (and, for writes/waits, an `LloValue*` value or an `int` immediate), assert it is `kSflag`, then construct and append the matching `mlir::llo` instruction. A reimplementer reproduces the protocol by reproducing this table — the verbs are closed.

### The primitive table

| Builder method | Addr | LLO instruction | Semantics |
|---|---|---|---|
| `VsyncSet(sf, v, opt<bool> done)` | `0x1d54cce0` | `CreateVectorSyncFlagSet` | write `v` (optionally set done) |
| `VsyncAdd(sf, v)` | `0x1d523200` | `CreateVectorSyncFlagAdd(flag=0)` | atomic `*sf += v` |
| `VsyncAddDone(sf, v)` | `0x1d54e380` | `CreateVectorSyncFlagAdd(flag=257)` | atomic add **and** set done |
| `VsyncAddInGranules(sf, MemUnit)` | `0x1d54e160` | `CreateVectorSyncFlagAdd` | add a granule-count (byte→word) |
| `VsyncRead(sf)` | `0x1d524220` | `PushSyncFlagToFifo` → pop | read counter into a scalar register |
| `VwaitGeSV(sf, thr, yield)` | `0x1d522f80` | `CreateVectorWait(391 + 8·yield)` | block until `*sf ≥ thr` |
| `VwaitEqSV` / `VwaitNeSV` / `VwaitLtSV` | — | `CreateVectorWait` (cond ≠ Ge) | block until `==` / `!=` / `<` |
| `VwaitDone(sf, yield)` | — | `CreateVectorWaitDone` | block until done-bit set |
| `VsyncSetRemote(sf, CoreLoc, v)` | `0x1d54e120` | `CreateVectorSyncFlagSetRemote` | set a flag on a *remote* core |
| `VsyncAddRemote(sf, CoreLoc, v, b)` | `0x1d522f40` | `CreateVectorSyncFlagAddRemote` | add to a remote-core flag |
| `VsyncAddRemoteInGranules` | `0x1d54e4e0` | `CreateVectorSyncFlagAddRemote` | remote granule-count add |
| `VsyncPublicAccessSet(sf, v)` | — | `CreateVectorSyncFlagPublicAccessSet` | set + publish cross-engine |
| `VsyncMarkAsPublic` / `MarkAsPrivate(sf)` | — | (public-access bit) | toggle cross-engine visibility |

### `VsyncAdd` — the canonical producer

```c
function VsyncAdd(builder, LloValue* sync_flag, LloValue* value):   // 0x1d523200
    CHECK(sync_flag->memory_space() == MemorySpace::kSflag,         //   llo_region_builder.cc:8280
          "sync-flag argument was not in the sync-flag memory space: " + mnemonic(sync_flag))
    inst = LloInstruction::CreateVectorSyncFlagAdd(sync_flag, value, /*flag=*/0, module)
    return region.AppendInstruction(inst)
```

`VsyncAddDone` is byte-identical except `flag = 257` (`0x101`, the done bit) — confirming that "add" and "add-and-mark-done" are the *same* instruction with a different flag word, not two instructions.

### `VwaitGeSV` — the canonical consumer

`VwaitGeSV` @ `0x1d522f80` is the richest primitive and the one barrier waits lower to. It does five things in order:

```c
function VwaitGeSV(builder, LloValue* sync_flag, LloValue* threshold, bool yieldable):  // 0x1d522f80
    if (yieldable):
        CHECK(target().SupportsYieldableOps(), "target().SupportsYieldableOps()")   //   :8006
    CHECK(sync_flag->memory_space() == MemorySpace::kSflag,                          //   :8008
          "sync_flag->memory_space() == MemorySpace::kSflag")
    if (autoflag tracing-vwait enabled):                                            //   AutoOr<bool> probe
        emit CreateTracingVectorWait(sync_flag)                                     //   pre-wait trace hook
    inst = LloInstruction::CreateVectorWait(/*opcode=*/391 + 8*yieldable,           //   Ge condition
                                            sync_flag, threshold, /*neg=*/…, module)
    appended = region.AppendInstruction(inst)
    dummy = SflagImmPtr(target().GetDummySyncFlagNumber(), "dummy sync flag", …)     //   vtable +1272
    if (autoflag tracing-vwait enabled):
        emit CreateTracingVectorWait(dummy)                                          //   post-wait trace hook
    return appended
```

Three details a reimplementer must carry:

- **The wait opcode is `391 + 8·yieldable`.** A non-yieldable Ge-wait is opcode `391`; the yieldable form (which allows the sequencer to yield while spinning) is `399`. `yieldable` is only legal when `Target::SupportsYieldableOps()` (a per-gen predicate present on JF/PF/VF/GL) returns true, else the compile fails at `llo_region_builder.cc:8006`.
- **It threads a *dummy* sync flag.** After the wait, the builder fabricates an SFLAG pointer at `GetDummySyncFlagNumber()` (reached as the vtable call at `+1272`/idx 159, named `"dummy sync flag"`). On Jellyfish this number is **7**; on PF/VF/GL it is **0**. The dummy slot exists so the post-wait machinery (and tracing) always has a valid flag to name; it is the same `DummySyncFlagNumber` the per-gen config exposes (§5).
- **Tracing is conditional.** Both `CreateTracingVectorWait` emissions are gated on an `AutoOr<bool>` proto flag (`xla::jellyfish::AutoProto`); when disabled they are skipped, so the steady-state lowering is a single `CreateVectorWait`. The `Eq`/`Ne`/`Lt` siblings differ only in the condition encoded into the opcode; `VwaitDone` uses `CreateVectorWaitDone` and reads the done-bit instead of comparing the counter.

### `VsyncRead` — observing the counter

`VsyncRead` @ `0x1d524220` reads the current flag value into a scalar register. It does not compare — it pops:

```c
function VsyncRead(builder, LloValue* sync_flag):                  // 0x1d524220
    fifo = PushSyncFlagToFifo(builder, sync_flag)                  //   stage the flag into the read FIFO
    if (target()->vtable[+640]()):                                 //   target supports SFR FIFO pop
        return PopSfrFifo(builder, fifo)
    return AppendInstruction(CreateScalarV2SPop(fifo))             //   else vector→scalar pop
```

The read is two-staged: the flag is pushed into a sync-flag FIFO, then either popped via the dedicated SFR-FIFO path (when the target's `+0x280`/idx 80 vtable predicate holds) or via a generic `ScalarV2SPop` vector-to-scalar move. Either way the consumer ends with the counter value in an SREG.

### Public access — cross-engine visibility

`VsyncPublicAccessSet` / `VsyncMarkAsPublic` / `VsyncMarkAsPrivate` toggle a flag's *public-access* state, which controls whether a sub-engine's flag is observable outside its owning engine. They lower to `CreateVectorSyncFlagPublicAccessSet` and, at the ISA level, to `TensorCoreVectorMisc_SetPublicAccess` / `_ReadSyncPublicAccess` (each carrying a `SyncFlagNumberField`) — the per-engine ISA realization confirmed across the `gxc.gfc`, `gxc.glc`, and `vxc.vfc` namespaces. The detailed cross-mode address-space casting that lets one sub-engine name another's flag is the [remote-sflag encoder](../barrier/remote-sflag-encoders.md) story; here it suffices that the public bit is the visibility gate.

> **NOTE —** the `*Remote` variants (`VsyncSetRemote` @ `0x1d54e120`, `VsyncAddRemote` @ `0x1d522f40`) take a `CoreLocationBase` argument and target a flag on a *different* core, lowering to the `…Remote` instruction classes. They are the building block of ICI collective acks (a remote DMA write auto-increments the destination's flag). The address-encoding of the remote flag is owned by [remote-sflag encoders](../barrier/remote-sflag-encoders.md).

---

## 4. Operand Packing

### Purpose

The pointer-level model (§1) is one word per flag. The *operand* encoding is denser, because flag values are tiny and the ISA packs them. A reimplementer who reads only §1 will mis-size the scalar operands feeding the primitives.

### Two flags per scalar register

The SC scalar register that carries sync-flag operands packs **two** flag numbers into one 32-bit SREG (low/high halves), confirmed by the assertion `num_sync_flags_encoded_per_sreg == 2`. So a primitive that names a flag by number consumes half an SREG; address arithmetic at the byte level (`4·n`) is unaffected, but operand-register accounting must use the 2-per-sreg packing.

### Word-granular tracking

The SC allocation high-water-mark attribute records SFLAG occupancy in **words**, not bytes: `AllocationHighWaterMarkAttr::getSflagWords()` @ `0x1458fec0`. This is consistent with `SflagImmPtr`'s `4·n` stride — the allocator counts flags (words), the addresser multiplies by four.

### The done bit in the value

As §2 established, the producer-side done selector is bit `0x100` of the instruction flag word (`VsyncAddDone` flag `0x101`, `VsyncSet` `update_done_to` discriminant). This is operand-level packing, not address-level: the same flag word, the same `4·n` address, different control bits.

---

## 5. Per-Generation SFLAG Constants

### Purpose

The actuation primitives consume a handful of per-generation constants — the dummy flag number, the count-mode bit, and the done-bit / yieldable capabilities. These are the only generation-dependent inputs to the protocol; everything else (the `4·n` stride, the kSflag CHECK, the verb set) is gen-independent.

### The constants

| Constant / capability | Accessor | JF (v2) | PF (v4) | VF (v5) | GL (v6e) |
|---|---|---:|---:|---:|---:|
| Dummy flag number | `GetDummySyncFlagNumber()` | **7** | 0 | 0 | 0 |
| Count-mode bit offset | `SyncFlagCountModeBitOffset()` | 0 | 0 | **272** (lite: 0) | **272** |
| Done-bit in flags | `HasExtraDoneBitInSyncFlags()` | per-gen predicate | per-gen | per-gen | per-gen |
| Yieldable waits | `SupportsYieldableOps()` | per-gen predicate | per-gen | per-gen | per-gen |
| Remote-DMA flag ceiling | `GetSyncFlagNumberLimitForRemoteDma()` | `jxc::Max(=59)+1` / `+0x530` | `Target+0x530` | `Target+0x530` | `Target+0x530` |

Per-gen override addresses (decoded immediates): `JellyfishTarget::GetDummySyncFlagNumber` @ `0x1d4908c0` (= 7); `Pufferfish/Viperfish/Ghostlite::GetDummySyncFlagNumber` @ `0x1d494ea0` / `0x1d49b280` / `0x1d4980c0` (all = 0). `SyncFlagCountModeBitOffset`: `0x1d491380` (JF = 0), `0x1d495860` (PF = 0), `0x1d49bca0` (VF: `0x110` non-lite / 0 lite), `0x1d4988c0` (GL = `0x110`). `SupportsYieldableOps` is present on all four (`JellyfishTarget` … `GhostliteTarget`).

> **NOTE —** the Jellyfish dummy number being **7** while every later generation uses **0** is the kind of off-by-design quirk that breaks naïve reimplementations: a `VwaitGe` on Jellyfish threads dummy flag 7, so flag 7 must be reserved-as-dummy on JF and is free on PF/VF/GL. The `GetSyncFlagNumberLimitForRemoteDma()` ceiling of 59 on Jellyfish (`asic_sw::deepsea::jxc::MaxSyncFlagNumberForRemoteDma` @ `0x1d62da80` = `0x3b`) means only flags 0..59 are valid remote-DMA completion targets on JF; the encoded remote-sflag field is limited to that range.

> **[LOW]** The literal per-codename SFLAG byte size / word size (`SflagSizeBytes()` @ `Target+0x468`, `SflagWordSizeBytes()` @ `Target+0x504`) are boot-filled from the embedded `chip_parts.binarypb` and are **not** statically extractable from `.text`. The accessor *offsets* and the `4·n` stride are CONFIRMED; the numeric sizes are a memfile dependency shared with the [VMEM allocator](vmem-allocator.md) and the [overview](overview.md).

---

## 6. Verification Notes

> **[CONFIRMED]** Re-derived byte-exact from the IDA decompile of `libtpu.so` v0.0.40 for this page:
> - `SflagImmPtr` @ `0x1d5185a0`: `MakeValidatedShape(element_type=4 == S32)`; `ImmPtr(byte_offset = 4·number, shape, space=6, name)` — the word stride and render space id are exact.
> - `VsyncAdd` @ `0x1d523200`: CHECK `sync_flag->memory_space() == MemorySpace::kSflag` (`llo_region_builder.cc:8280`); `CreateVectorSyncFlagAdd(…, flag=0, …)` — pure counter add.
> - `VsyncAddDone` @ `0x1d54e380`: same CHECK (`:8289`); `CreateVectorSyncFlagAdd(…, flag=257=0x101, …)` — done bit `0x100`.
> - `VsyncSet` @ `0x1d54cce0`: CHECK kSflag (`:8057`); done-bit guard `!update_done_to.has_value() || target().HasExtraDoneBitInSyncFlags()` (`:8061`, `"Done bit not supported for this target."`); `CreateVectorSyncFlagSet`.
> - `VwaitGeSV` @ `0x1d522f80`: yieldable guard `target().SupportsYieldableOps()` (`:8006`); CHECK kSflag (`:8008`); `CreateVectorWait(391 + 8·yieldable)`; post-wait dummy `SflagImmPtr(GetDummySyncFlagNumber(), "dummy sync flag")` via vtable `+1272`; conditional `CreateTracingVectorWait` gated on an `AutoProto` flag.
> - `VsyncRead` @ `0x1d524220`: `PushSyncFlagToFifo` then `PopSfrFifo` (target vtable `+640`) or `CreateScalarV2SPop`.
> - Per-gen constants: `GetDummySyncFlagNumber` JF=7 (`0x1d4908c0`) / PF,VF,GL=0; `SyncFlagCountModeBitOffset` = 272 on VF-non-lite (`0x1d49bca0`) and GL (`0x1d4988c0`), 0 elsewhere — exact immediates.
> - `mlir::llo` op classes `VSyncSetOp` / `VSyncAddOp` / `VSyncAddDoneOp` / `VSyncAddRemoteOp` / `VSyncReadOp` / `VWaitGeOp` present in the registered-operation tables; `num_sync_flags_encoded_per_sreg == 2` and `AllocationHighWaterMarkAttr::getSflagWords` @ `0x1458fec0` confirm word-granular packing.
>
> **[LOW]** (1) The full bit-field layout of the multi-word SFLAG register where count-mode bit 272 sits (counter width, done bit, public bit) is not enumerated. (2) Literal per-codename SFLAG byte/word sizes live in `chip_parts.binarypb` (memfile dependency). (3) The exact `Target` vtable indices for `HasExtraDoneBitInSyncFlags` / `SupportsYieldableOps` are not individually pinned (the predicates are confirmed present per-gen; the dummy-number vtable slot at `+1272` is decoded).

---

## Cross-References

- [Memory Hierarchy Overview](overview.md) — SFLAG as `MemorySpace::kSflag` = 6; the six-region taxonomy, the render-id vs operand-tag enum split, and the `BestFitAllocator` matrix this page's `4·n` stride feeds
- [SMEM — Scalar Memory](smem-scalar-memory.md) — the adjacent scalar tier; SFLAG is snapshotted/transferred together with SMEM in the sequencer-local SRAM image
- [Barrier → SFLAG Number Binding](../barrier/barrier-to-sflag-binding.md) — *which* number a barrier binds to (the reserved five-slot window above `[base, base+count)`); this page is the substrate those numbers index
- [Barriers and Sync-Flags — Section Map](../barrier/overview.md) — the cross-core barrier subsystem that drives these `Vsync`/`Vwait` primitives
- [Tree-Barrier Vsync Emitter](../barrier/tree-barrier-vsync.md) — the canonical consumer of `VwaitGeSV` / `VsyncAdd` in the global-barrier datapath
- [Remote-SFLAG Encoders](../barrier/remote-sflag-encoders.md) — how `VsyncAddRemote` / `VsyncSetRemote` encode a destination flag on a different core; the cross-mode address-space casts
- [vmem-allocator.md](vmem-allocator.md) — the shared `chip_parts.binarypb` boot-fill that supplies the numeric SFLAG word/byte sizes
- [back to index](../index.md) — Part X — On-Chip Memory & DMA
