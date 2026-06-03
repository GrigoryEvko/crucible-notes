# MRB FIFO / MSR Placement

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, **not** stripped — every symbol below is a demangled C++ name). Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. Other versions differ.*

## Abstract

This is the **physical-placement layer** of Stage 2's matrix-resource assignment. The [MRB chain allocator](mrb-chain-allocator.md) decides, on a program-order reservation timeline, *which* matrix-result-buffer (MRB) chunk and FIFO index each matmul accumulation chain occupies — it hands each chain a `MrbEntry = {chunk_id, fifo_index, format}`. Two `MxuAssigner` methods then turn those abstract reservations into the two concrete attributes the per-generation encoder serializes into the bundle word: `AllocateMrbEntriesAsFifo` (`0x10f3ef80`) assigns each matmul and each matres a physical **result-FIFO slot address**, and `BounceBetweenMsrs` (`0x10f3fae0`) assigns each `MxuSequence` a physical **matrix-staging-register (MSR) bank** — `msra` (0) or `msrb` (1). Both run once **per MXU quadrant**, dispatched from `MxuAssigner::VisitRegion` over an `array<Span<MxuSequence>,4>` (the four MXU instances of a region).

The familiar reference frame is a hardware ring buffer paired with a double-buffered staging latch. The result FIFO is a per-MXU circular buffer: `AllocateMrbEntriesAsFifo` maintains two running cursors per MXU — a **write cursor** (advanced on the matmul/push side) and a **read cursor** (advanced on the matres/pop side) — each advanced by the per-op entry count, rounded up to a write-block granule, and wrapped modulo the FIFO depth. The MSR bounce is the classic ping-pong of a weight-stationary array: while `msra` feeds one sequence's matmuls, the next sequence's gain matrix stages into `msrb`, then the banks swap; `BounceBetweenMsrs` realizes that by toggling a one-bit bank index at the top of every sequence and stamping it onto each op via `set_matrix_staging_register`. The one wrinkle is the LMR/XMR path: a `vector-matmul-lmr` (opcode `0xa5`) reads its gain matrix directly from a load-matrix register rather than staging through an MSR, so the bounce is meaningless for it and the whole pass returns early.

This page documents the two placement functions byte-for-byte, the two struct layouts the allocator carries them on (`MrbChunkState` — actually two distinct per-chunk arrays — and the `0x70`-byte `AccumulationChainAfterSplit` record), and the downstream consumer (`LloInstruction::set_mrb_address` → `set_mrb_address_unrestricted`, the matmul-family field at `+0x42`, read back by `mrb_address`). It does **not** re-derive the reservation timeline (that is the [chain allocator](mrb-chain-allocator.md)) or the bundle bit positions of the MSR/MRB fields (those are the [MXU slot](../isa/slot-mxu.md) and per-gen bundle pages).

For reimplementation, the contract is:

- **The FIFO discipline** — two `int32` cursor arrays sized by `num_mxus`, per-MXU select `unit_id & 3`, advance by `MxuResultEntriesPushed`/`Popped`, round up to `MinMrbWriteBlockSize`, wrap modulo `ResultFifoEntryCount(kMrf0, version)`, committed by `set_mrb_address`.
- **The MSR bounce** — the `0xa5` early-out pre-scan, the per-sequence `msr ^= 1` toggle, and `set_matrix_staging_register`'s per-op-family destination byte (`+0x46`/`+0x44`/`+0x42`/`+0x41`).
- **The two struct layouts** — the `0x70`-byte per-MXU timeline element vs the `0x40`-byte per-chunk free-pool record, and the `0x70`-byte `AccumulationChainAfterSplit`, all keyed by `MrbEntry.chunk_id`.
- **The per-gen accessor surface** — six `Target` vtable slots that price the placement, and *why* the path is gen-gated off in v0.0.40 (every shipped TensorCore target returns `MatmulResultBufferEntries() == 0`).

| | |
|---|---|
| **FIFO placement** | `MxuAssigner::AllocateMrbEntriesAsFifo(Target const&, Span<unique_ptr<MxuSequence>>)` `@0x10f3ef80` |
| **MSR bounce** | `MxuAssigner::BounceBetweenMsrs(Target const&, Span<unique_ptr<MxuSequence>>)` `@0x10f3fae0` |
| **Per-quadrant dispatch** | `MxuAssigner::VisitRegion` `@0x10f3a640` → `array<Span,4>` lambdas `$_5` `@0x10f40a80`, `$_6` `@0x10f40b60` |
| **Physical FIFO write** | `LloInstruction::set_mrb_address(int)` `@0x1d4d93c0` → `set_mrb_address_unrestricted(int)` `@0x1d4e9780` (field `+0x42`) |
| **MSR write** | `LloInstruction::set_matrix_staging_register(Msr)` `@0x1d4d7d40` (fields `+0x46`/`+0x44`/`+0x42`/`+0x41`) |
| **FIFO depth** | `ResultFifoEntryCount(kMrf0=8, TpuVersion)` `@0x1d631520`; table `@0xb53e240` = `{16,16,16,48,224,256}` |
| **Msr enum** | `MsrToString` `@0x1d629720`: `0 → "msra"`, `1 → "msrb"` |
| **Carried on** | `MrbChainAllocator` per-MXU timeline `@alloc+0xf0` (stride `0x70`), per-chunk free pool `@alloc+0x7a0` (stride `0x40`) |
| **Source** | `platforms/xla/service/jellyfish/mxu_assigner.cc`, `mxu_accumulation.cc`, `llo_instruction.cc` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Per-Quadrant Dispatch

### Purpose

Both placement functions are *region-local* and *quadrant-local*. `MxuAssigner::VisitRegion` (`0x10f3a640`) groups the region's `MxuSequence`s by physical MXU into an `array<Span<unique_ptr<MxuSequence>>,4>` — one span per MXU instance — and runs that array through two `InvokeObject` lambdas: `$_5` (`0x10f40a80`) calls `AllocateMrbEntriesAsFifo` four times (once per quadrant span, paired `[rcx+0/0x10/0x20/0x30]` pointer with `[rcx+8/0x18/0x28/0x38]` length); `$_6` (`0x10f40b60`) calls `BounceBetweenMsrs` the same four times. Each lambda early-outs if a call returns a non-OK `Status`. The captured first word of each lambda is the `Target&` proxy.

### Structure

```text
MxuAssigner::VisitRegion                          0x10f3a640
  build array<Span<unique_ptr<MxuSequence>>, 4>   ── one span per MXU instance (quadrant)
  ├─ $_5  InvokeObject(AllocateMrbEntriesAsFifo)   0x10f40a80
  │     AllocateMrbEntriesAsFifo(target, quadrant[0])     0x10f40abe
  │     AllocateMrbEntriesAsFifo(target, quadrant[1])     0x10f40ad2
  │     AllocateMrbEntriesAsFifo(target, quadrant[2])     0x10f40ae6
  │     AllocateMrbEntriesAsFifo(target, quadrant[3])     0x10f40afc
  │       └─ early-out if Status != OK (1)
  └─ $_6  InvokeObject(BounceBetweenMsrs)          0x10f40b60
        BounceBetweenMsrs(target, quadrant[q]) × 4        0x10f40f91…
```

> **QUIRK — the FIFO cursors are per-MXU *inside one quadrant call*, not global.** `AllocateMrbEntriesAsFifo` allocates its two cursor arrays at function entry and frees them at function return, so each of the four quadrant calls starts both cursors at zero. The arrays are sized `num_mxus`, and the per-element index comes from `unit_id & 3` (the per-op MXU instance, `HIBYTE(WORD[+0xb]) & 3`), so within one call the cursors are independent per MXU. A reimplementation that hoists the cursors to region scope (carrying the FIFO write position across quadrants) will diverge — the binary resets them per call. Whether the hardware actually expects a fresh FIFO per quadrant region or a cross-region carry is **not traced** (the cursor lifetime is strictly intra-call here).

---

## FIFO Placement — `AllocateMrbEntriesAsFifo`

### Purpose

Walk one quadrant's `MxuSequence`s in program order and assign every matmul and every matres a concrete result-FIFO slot address via `set_mrb_address`. The result FIFO is modelled as a per-MXU circular buffer of `ResultFifoEntryCount(kMrf0, version)` entries; matmuls push into it (advancing a write cursor) and matreses pop from it (advancing a separate read cursor).

### Entry Point

```text
AllocateMrbEntriesAsFifo(Target const& [rdi], Span<unique_ptr<MxuSequence>> [rsi=ptr, rdx=count])
  num_mxus      = Target[+0x4ac]                         ── 0x10f3ef9b  (movsxd; same field as LatchLhs)
  write_ptr[]   = operator new(4 * num_mxus); memset 0   ── 0x10f3efc4  ([rbp-0x48], matmul/push side)
  read_ptr[]    = operator new(4 * num_mxus); memset 0   ── 0x10f3efe0  ([rbp-0x68], matres/pop side)
  fifo_depth    = ResultFifoEntryCount(8, Target[+0x398])── 0x10f3f019
  for each MxuSequence in span: …
```

### Algorithm

```c
// MxuAssigner::AllocateMrbEntriesAsFifo @0x10f3ef80
//   a1 = Target const&  (the MxuAssigner `this` is dead; rdi carries the Target&)
//   span = Span<unique_ptr<MxuSequence>>  (one MXU quadrant)
function AllocateMrbEntriesAsFifo(target, span):
    num_mxus = target[+0x4ac]                       // CONFIRMED Target field (== LatchLhs num_mxus read)
    if num_mxus > 0:
        write_ptr = new int32[num_mxus]; memset(write_ptr, 0)   // matmul/push cursor, [rbp-0x48]
        read_ptr  = new int32[num_mxus]; memset(read_ptr,  0)   // matres/pop cursor,  [rbp-0x68]
    fifo_depth = ResultFifoEntryCount(kMrf0 /*=8*/, target[+0x398] /*DeepseaVersion*/)  // @0x1d631520

    for seq in span:
        RET_CHECK(!seq->matmuls.empty())            // mxu_assigner.cc:881  ([seq+0x50] count)
        // per-MXU quadrant select: bits 8..9 of LloValue WORD[+0xb], gated by the 0x400 "has mxu" bit
        seq_unit = (seq->matmuls[0].word[0xb] & 0x400) ? ((HIBYTE(word) & 3) | HAS_VALUE) : 0
        RET_CHECK(seq->matmuls[0]->unit_id().has_value())       // mxu_assigner.cc:882

        matres_index = 0
        for matmul in seq->matmuls:                 // [seq+0x48] ptr, [seq+0x50] count
            fmt  = matmul.matmul_data_format()      // @0x1d4e8440
            push = target.MxuResultEntriesPushed(matmul_op, fmt)    // vtbl[+0x5f0]
            unit = matmul->unit_id()
            RET_CHECK(unit.has_value())             // mxu_assigner.cc:890
            RET_CHECK(unit == seq_unit)             // mxu_assigner.cc:893 "same unit id"
            mxu  = unit & 3                          // per-MXU index into the cursor arrays

            // ---- THE PHYSICAL WRITE (matmul push side) ----
            set_mrb_address(matmul, write_ptr[mxu])             // @0x1d4d93c0  ← FIFO slot
            write_ptr[mxu] += push                              // advance by entries this matmul pushes
            g = target.MinMrbWriteBlockSize()                   // vtbl[+0x5e0], a byte (idiv divisor)
            write_ptr[mxu] = ceil_div(write_ptr[mxu], g) * g    // round UP to the write-block granule
            write_ptr[mxu] %= fifo_depth                        // wrap into the FIFO

            // ---- THE MATRES POP SIDE (only when this matmul pushes entries) ----
            if push > 0:
                base = read_ptr[mxu]
                acc  = 0
                while acc < push:                   // drain `push` worth of result entries
                    matres = seq->matreses[matres_index]        // [seq+0x60] ptr, [seq+0x68] count
                    RET_CHECK(matres_index < seq->matreses.size())   // mxu_assigner.cc:902 / :913
                    RET_CHECK(matres->unit_id().has_value())         // mxu_assigner.cc:924
                    RET_CHECK(*matres->unit_id() == unit)            // mxu_assigner.cc:925
                    rel = target.MrbRelativeAddressFromOffset(matres_op, acc)   // vtbl[+0x5e8]
                    set_mrb_address(matres, (base + rel) % fifo_depth)          // @0x1d4d93c0
                    acc += target.MxuResultEntriesPopped(matres_op, fmt)        // vtbl[+0x5f8]
                    matres_index += 1
            read_ptr[mxu] = ceil_div((read_ptr[mxu] + push), g_pop) * g_pop % fifo_depth   // symmetric

        RET_CHECK(matres_index == seq->matreses.size())          // mxu_assigner.cc:949 "too many matreses"
    return OK
```

The round-up arithmetic is emitted as a signed `idiv` plus the standard "did we truncate toward zero" fixup, exactly as the decompiler renders it at `0x10f3f218`:

```c
// write_ptr[mxu] = ceil(write_ptr[mxu] / g) * g, for a signed dividend (0x10f3f1f8..0x10f3f234)
q = write_ptr[mxu] / g;                                  // idiv
write_ptr[mxu] = g * (q + ((write_ptr[mxu] > g*q) & (q >= 0)));   // bump quotient if there was a remainder
write_ptr[mxu] %= fifo_depth;                            // final wrap
```

> **GOTCHA — the matmul cursor and the matres cursor are independent.** `write_ptr` (matmul push) and `read_ptr` (matres pop) advance separately; the read cursor is *not* derived from the write cursor. The matres address is `(read_base + MrbRelativeAddressFromOffset(op, acc)) % fifo_depth`, where `acc` accumulates `MxuResultEntriesPopped` until it reaches the matmul's `push` count. A reimplementation that places matres results at the same slot as the producing matmul (assuming a single shared cursor) will mis-address every result-pop; the read/write skew *is* the result-FIFO occupancy model the [chain allocator's reservation timeline](mrb-chain-allocator.md) prices.

### The Six `Target` Accessors

`AllocateMrbEntriesAsFifo` reaches the per-generation geometry entirely through `Target` virtuals. These six slots resolve the formerly-inferred `Target[+0x390]`/`[+0x5e0]` slots that the [chain allocator](mrb-chain-allocator.md) and the cost model left unnamed.

| `Target` vtable slot | vptr offset | Base impl | Role on this path | Confidence |
|---|---|---|---|---|
| `MatmulResultBufferEntries` | `+0x390` | `0x1d61da40` | MRB-support gate: `set_mrb_address` aborts unless `> 0` | CONFIRMED |
| `SupportsMatmulResultPack` | `+0x398` | `0x1d61da80` | sibling; not on this hot path | HIGH |
| `MinMrbWriteBlockSize` | `+0x5e0` | `0x1d490d80` (`LogFatal`) | the FIFO push round-up granule (`idiv` divisor) | CONFIRMED |
| `MrbRelativeAddressFromOffset` | `+0x5e8` | `0x1d490dc0` (`LogFatal`) | `(op, offset) → relative MRB address` on the pop side | HIGH |
| `MxuResultEntriesPushed` | `+0x5f0` | `0x1d61e9c0` | entries a matmul pushes; advances `write_ptr` | CONFIRMED |
| `MxuResultEntriesPopped` | `+0x5f8` | `0x1d61ea00` | entries a matres pops; advances `matres_index` | CONFIRMED |

> **NOTE — `Target[+0x4ac]` is `num_mxus`, `Target[+0x398]` is the version.** `num_mxus` (`movsxd rcx,[rdi+0x4ac]`) is read identically by `MxuAssigner::LatchLhs` (`0x10f3b791`, `imul rcx, Target::ChunksPerTile()`); `Target[+0x398]` is the `tpu::TpuVersion` (an unsigned in `[0,6)`) the FIFO-depth table is indexed by. Both are CONFIRMED cross-function.

### FIFO Depth Table

`ResultFifoEntryCount(ResultFifo fifo, TpuVersion ver)` (`0x1d631520`) is a switch over the `fifo` argument; the matmul-result FIFO `kMrf0 = 8` lands in the arm that returns `table[ver]` from `.rodata` at `0xb53e240`. Read directly from the ELF (`struct.unpack`):

| TpuVersion ordinal | Generation | `ResultFifoEntryCount(kMrf0, ver)` | Confidence |
|---|---|---|---|
| 0 | Jellyfish | 16 | CONFIRMED |
| 1 | Dragonfish | 16 | CONFIRMED |
| 2 | Pufferfish | 16 | CONFIRMED |
| 3 | Viperfish | 48 | CONFIRMED |
| 4 | Ghostlite | 224 | CONFIRMED |
| 5 | 6acc60406 (TPU7x) | 256 | CONFIRMED |

The version ordinals `≥ 6` take the `LogMessageFatal("invalid platform type:")` arm; the table has exactly six live rows. This is the modulus every cursor wraps at.

### `MxuResultEntriesPushed` / `Popped` — the Per-Op Entry Counts

The two count accessors are overridden per generation even on the gens that gate MRB off (below). The Viperfish overrides are the byte-exact reference:

```c
// ViperfishTarget::MxuResultEntriesPushed @0x1d499ae0  (a2 = LloOpcode, a3 = MatmulDataFormat)
if a2 != 0xa5 /*vector-matmul-lmr*/:
    CHECK(a3-1 < 8)                                      // valid MatmulDataFormat
    return push_table[a3-1]   // @0xa2e6180 = {2,4,8,8,4,4,4,4}
else:
    CHECK((a3-2) in {0,3,4,5,6})                         // 0xa5 valid-format mask 0x79 (bits 0,3,4,5,6)
    return lmr_push_table[a3-2] // @0xb5307c0 = {2,0,0,1,1,1,1,0}

// ViperfishTarget::MxuResultEntriesPopped @0x1d499ba0
return 2 - MatmulDataFormatIsIntegral(fmt)              // @0x1d629240 : integral fmt → pop 1, float → pop 2
```

Both push tables were read directly from `.rodata`: `0xa2e6180 = {2,4,8,8,4,4,4,4}` (indexed `fmt-1`) and `0xb5307c0 = {2,0,0,1,1,1,1,0}` (the `0xa5` path, indexed `fmt-2`). The Jellyfish base (`MxuResultEntriesPushed` `@0x1d492360`) is the small `fmt1→1, fmt2→2` rule; popped (`@0x1d492420`) is `fmt1/2→1`.

### Result Checks

`AllocateMrbEntriesAsFifo` is dense with `RET_CHECK`s (`mxu_assigner.cc` line numbers are the literal third arguments to `RetCheckFailSlowPath`):

| Line | Condition | Failure string |
|---|---|---|
| 881 | `!sequence->matmuls.empty()` | (the empty-matmuls guard at the top of each sequence) |
| 882 | `sequence->matmuls[0]->unit_id().has_value()` | sequence unit id present |
| 890 | `matmul->unit_id().has_value()` | per-matmul unit id present |
| 893 | `unit_id == sequence_unit_id` | "All matmuls in a sequence must have the same unit id." |
| 902 / 913 | `matres_index < sequence->matreses.size()` | matres index in range |
| 924 / 925 | `matres->unit_id().has_value()` / `*matres->unit_id() == unit_id` | matres belongs to the same MXU |
| 949 | `matres_index == sequence->matreses.size()` | "Sequence had too many matreses for the number of matmuls" |
| — | (matres-drain under-run) | "Sequence had too few matreses for the number of matmuls" (`@0x8547285`/`@0x854724c`) |

---

## MSR Bounce — `BounceBetweenMsrs`

### Purpose

Assign each `MxuSequence` in the quadrant a matrix-staging-register (MSR) bank — `msra` (0) or `msrb` (1) — and stamp it onto every staging op. Consecutive sequences alternate banks so that the gain matrix for sequence *n+1* can stage into one MSR while sequence *n*'s matmuls drain from the other — the double-buffered weight-stationary ping-pong. The MSR each op carries is exactly the attribute the [cost model's `GetResourceUsage`](../cost/mxu-latency-overview.md) reads to charge the per-bank latch reservation.

### Algorithm

```c
// MxuAssigner::BounceBetweenMsrs @0x10f3fae0  (a1 = Target const&; span = quadrant)
function BounceBetweenMsrs(target, span):
    // ---- PRE-SCAN: abort if any sequence uses the LMR/XMR matmul path ----
    for seq in span:
        RET_CHECK(!seq->matmuls.empty())                // mxu_assigner.cc:1063  ([seq+0x50])
        for op in seq->matmuls:                         // [seq+0x48] ptr, [seq+0x50] count
            if op.opcode == 0xa5 /*vector-matmul-lmr*/:
                return OK                                // 0xa5 reads gains from LMR, bypasses the MSRs

    // ---- BOUNCE: toggle the bank at the top of every sequence ----
    msr = 1
    for seq in span:
        msr ^= 1                                         // alternate: msra(0) ↔ msrb(1)
        for latch in seq->latches:                       // [seq+0x18] ptr, [seq+0x20] count (0x8d..0x96)
            set_matrix_staging_register(latch, msr)      // @0x1d4d7d40
        // first matmul of the sequence also carries the bank
        set_matrix_staging_register(seq->matmuls[0], msr)   // [seq+0x48][0]
    return OK
```

> **CORRECTION (MRB-BOUNCE-OFFSETS) —** the bounce stamps the bank onto the sequence's **latch** list (`[seq+0x18]`/`[seq+0x20]`, the stationary-operand staging ops `0x8d..0x96`) and onto **`matmuls[0]`** (`[seq+0x48][0]`), not onto a matres. The `[seq+0x48]`/`[seq+0x50]` array is the *matmuls* vector — pinned by the `RET_CHECK(!sequence->matmuls.empty())` string at `mxu_assigner.cc:1063` guarding `[seq+0x50]`. The full `MxuSequence` sub-vector map (cross-checked against the [MxuSequence struct](mxu-sequence-struct.md) deleter and `SetLatchIndices`) is: latches `+0x18`/`+0x20`, matmuls `+0x48`/`+0x50`, matreses `+0x60`/`+0x68`. (Earlier raw notes labeled `+0x48`/`+0x50` as matreses; the check string corrects it.)

### Why `0xa5` Aborts the Whole Pass

A `vector-matmul-lmr` (`0xa5`) reads its stationary operand from a **load-matrix register** (the `Xmr` space), not from one of the two MSR staging banks. The `Xmr` enum is `{0 → "gmra", 1 → "lmr"}` (`XmrToString` `@0x1d629740`), and `XmrToMsr` (`@0x1d629780`) returns OK only for `gmra` (0) — converting `lmr` (1) to an MSR is the error path `"Cannot convert Xmr %s to Msr."` (`matrix_register.cc:100`). So an `0xa5` op has no MSR to bounce, and the whole quadrant's bounce is skipped rather than partially applied.

> **QUIRK — one `0xa5` anywhere in the quadrant suppresses the bounce for *all* sequences.** The pre-scan walks every matmul of every sequence first; a single `0xa5` makes `BounceBetweenMsrs` return before assigning any bank. A reimplementation that bounces sequence-by-sequence and only skips the `0xa5` sequences will assign MSR banks to the non-LMR sequences that the binary leaves unassigned.

### `set_matrix_staging_register` — the Destination Bytes

The bank byte is written into a different per-op-family field depending on opcode. The dispatch is a cascade of range tests (`LloOpcode` is the 16-bit `*a1`):

```c
// LloInstruction::set_matrix_staging_register(Msr) @0x1d4d7d40  (a2 = msr byte 0/1)
op = *a1;                                       // LloOpcode (uint16)
if      ((uint16)(op - 0x9b) <= 0xA):  a1[+0x46] = msr;   // 0x9b..0xa5  vector-matmul / -lmr
else if ((uint16)(op - 0x8d) <= 0x9):  a1[+0x44] = msr;   // 0x8d..0x96  vector-latch family (kVectorLatch*)
else if ((op & 0xFFFE) == 0xAA):       a1[+0x42] = msr;   // 0xaa/0xab   vector-load-lmr / -bf16
else if (op == 0xA8):                  a1[+0x41] = msr;   // 0xa8        vector-done-with-gains
else:  FATAL("msr unsupported for opcode: ");             // llo_instruction.cc:3414
```

| `LloOpcode` range | Op family | Destination field | Confidence |
|---|---|---|---|
| `0x9b..0xa5` | vector-matmul / vector-matmul-lmr | `+0x46` | CONFIRMED |
| `0x8d..0x96` | vector-latch family (`kVectorLatch*`) | `+0x44` | CONFIRMED |
| `0xaa`, `0xab` | vector-load-lmr / -lmr-bf16 | `+0x42` | CONFIRMED |
| `0xa8` | vector-done-with-gains | `+0x41` | CONFIRMED |
| else | — | FATAL | CONFIRMED |

### The `Msr` Enum

`MsrToString(Msr)` (`0x1d629720`) writes a 4-byte string by integer arithmetic, confirming the enum is exactly two values:

```c
// MsrToString @0x1d629720  (a2 = Msr ordinal)
*(uint32*)out = ((a2 != 0) << 24) + 0x6172736D;   // 0x6172736D == "msra" (LE: m,s,r,a)
// a2 != 0 adds 0x01000000 → last byte 'a'(0x61) → 'b'(0x62) ⇒ "msrb"
out[4] = 0; out.len = 4;
```

So `Msr{0} = "msra"`, `Msr{1} = "msrb"`. This is the bank index `BounceBetweenMsrs` toggles, and the bank the cost model's `Target`-level `MsrA`/`MsrB` resources (the latch ports) are charged against.

---

## Struct Layouts

The two placement functions read their `MrbEntry` inputs off the `MrbChainAllocator`'s per-chunk state, and the split logic stores its tail records in a btree. Three byte-exact layouts, decoded from `ReleaseMrbReservation` (`0x10f5f9e0`) and `SplitAccumulationChain` (`0x10f598e0`):

### `MrbChunkState` — Two Distinct Per-Chunk Arrays

The allocator carries *two* flat arrays keyed by `MrbEntry.chunk_id`, not one. They serve different purposes and have different strides:

**(a) Per-MXU reservation-timeline array** — `MrbChainAllocator[+0xf0]`, allocated `clamp(num_mxus, ≥8 when ver≥5) * 0x70` via `__size_returning_new`, **stride `0x70`**, indexed `chunk_id × 0x70` (`SplitAccumulationChain` `0x10f59941`: `v13 = base + 112 * chunk_id`). Each element is its **own** `boost::multi_index` bimap (the `chains_unevictable_until_` timeline of the [chain allocator](mrb-chain-allocator.md)) — i.e. the reservation timeline is **per-MXU**, not one global structure. The per-element ctor (`r14 += 0x70` per iteration):

| Field | Offset | Value / role | Confidence |
|---|---|---|---|
| multi_index self/sentinel | `+0x00` | `= &[+0x18]` | CONFIRMED |
| node-header ptr | `+0x10` | `operator new(0xa8)` | CONFIRMED |
| ordered-index head sentinel | `+0x18` | `= self` | CONFIRMED |
| size | `+0x28` | `0` | CONFIRMED |
| bucket count | `+0x38` | `0x36 = 54` | CONFIRMED |
| bucket array | `+0x40` | `operator new(0x1b0) = 54 * 8` | CONFIRMED |
| max load factor | `+0x48` | `(int)(54 * 1.0f)` (`0x3f800000`) | CONFIRMED |

(The boost prime-bucket sizes `{53,97,193,389,769,…}` are at `0xac092a0`; max load factor `1.0f`.)

**(b) Per-chunk free-entry pool** — `MrbChainAllocator[+0x7a0]` with an SOO bit at `MrbChainAllocator[+0x798]`, a flat array of **`0x40`-byte** records, indexed `chunk_id × 0x40` (`ReleaseMrbReservation` `0x10f5fa18`: `v7 = (int16)chunk_id << 6`). Each record is an `absl::container_internal::raw_hash_set` whose element is a `linked_hash_set<MrbEntry>` (policy `@0x2181c730`):

| Field | Offset | Role | Confidence |
|---|---|---|---|
| capacity mask | `+0x00` | hash-set capacity | CONFIRMED |
| size / SOO-state | `+0x08` | element count + SOO flag | CONFIRMED |
| ctrl ptr | `+0x10` | control-byte array | CONFIRMED |
| slots ptr | `+0x18` | slot array | CONFIRMED |
| SOO inline slot | `+0x20` | single-element fast path | CONFIRMED |
| free-list head | `+0x28` | doubly-linked recycled-entry list | CONFIRMED |
| list count | `+0x38` | recycled-entry count | CONFIRMED |

`ReleaseMrbReservation` hashes the `MrbEntry` (`MixingHashState::kSeed` CRC32 over `{uint16 chunk_id, int fifo_index, byte format}`), finds/inserts it in the chunk's set, then `operator new(0x20)`s a node `{+0x10 = MrbEntry key, +0x18 = format byte}` and pushes it onto the free-list (`[record+0x28]` head, `++[record+0x38]`). This recycles a freed FIFO entry back into the per-chunk pool.

### `AccumulationChainAfterSplit` (`0x70` bytes)

When `SplitAccumulationChain` cuts a chain at the current program order, the *tail* is deferred as a `unique_ptr<AccumulationChainAfterSplit>` keyed by program order into a `btree_set_container<btree<map_params_impl<long, unique_ptr<…>>>>` (root at `MrbChainAllocator[+0x70…]`, insert `@0x10f5d2e0`). The record:

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| span base | `+0x00` | `AccumulationWithOriginalChain*` | tail span begin | CONFIRMED |
| split program order | `+0x08` | `s64` | compared vs `alloc[+0x70]` current_time (`0x10f598ff`) | CONFIRMED |
| span ptr | `+0x10` | `AccumulationWithOriginalChain*` | cut cursor; `+= consumed * 0x60` (AWOC stride `0x60`) | CONFIRMED |
| span len | `+0x18` | `s64` | `-= consumed = current_order − begin` | CONFIRMED |
| `MrbEntry.chunk_id` | `+0x20` | `s16` | read at `0x10f599e2` / `AdvanceTimeTo` | CONFIRMED |
| `MrbEntry.fifo_index` | `+0x28` | `s32` | (also at `+0x24`) | CONFIRMED |
| has-MRB-entry flag | `+0x2c` | `byte` | gates `ReleaseMrbReservation` (`0x10f5990d`/`0x10f599d6`) | CONFIRMED |
| matres program order | `+0x68` | `s64` | the bimap LEFT key (unevictable-until) | CONFIRMED |

The span cut is `lea r14,[rax+rax*2]; shl r14,5` (`0x10f59ab6`) = `consumed * 0x60`, so the `AccumulationWithOriginalChain` element stride is `0x60`. The btree node built at `operator new(0x30)` writes `{+0x00 span_base, +0x08 latest_matmul/program_order, +0x10 span_ptr, +0x18 len}`. (Source: `mxu_accumulation.cc:1017` "Cannot split an AccumulationChain in the past", `:1033` "Freeing MRB entry …".)

---

## The Downstream Consumer — `set_mrb_address`

The FIFO slot `AllocateMrbEntriesAsFifo` computes is committed by `LloInstruction::set_mrb_address(int)` (`0x1d4d93c0`), which gates on MRB support, bounds-checks, then delegates to `set_mrb_address_unrestricted`:

```c
// LloInstruction::set_mrb_address(this, addr) @0x1d4d93c0
target = this->parent()->target();
if (target.MatmulResultBufferEntries() <= 0)            // vtbl[+0x390] gate
    UpdateStatus("parent()->target() supports …");      // llo_instruction.cc:3660
RET_CHECK(addr < ResultFifoEntryCount(kMrf0, target.DeepseaVersion()))   // :3663
        // "Expected MRB address in the range 0-512, found: <addr>"
return set_mrb_address_unrestricted(this, addr);        // @0x1d4e9780

// set_mrb_address_unrestricted(this, addr) @0x1d4e9780
RET_CHECK(addr >= 0);                                   // :3642 "mrb_address >= 0"
if ((uint16)(op - 0x9b) < 0xB || (op & 0xFFFE) == 0x152) // matmul 0x9b..0xa5, or matres 0x152/0x153
    *(uint16*)(this + 0x42) = addr;                     // ← the physical MRB-address field
else
    FATAL("Unsupported opcode: <op>");                  // :3655
```

So the placed slot lands in a 16-bit field at `LloInstruction+0x42`, valid for the matmul family (`0x9b..0xa5`) and the matres family (`0x152`/`0x153`); `mrb_address()` (`@0x1d4e8860`) reads it back (`lea ecx,[op-0x9b]; cmp cx,0xb`). The per-generation `Encoder*::EncodeBundle` then serializes that field — and the MSR byte from `set_matrix_staging_register` — into the bundle word (see the [MXU slot](../isa/slot-mxu.md) and [encoder latch serialization](encoder-latch-serialization.md) pages for the bit positions).

> **GOTCHA — the error string says "range 0-512" but the actual bound is per-version.** The hard bound is `ResultFifoEntryCount(kMrf0, version)` — `{16,16,16,48,224,256}` — *not* a fixed 512. The "0-512" literal is a stale message; the runtime check compares against the per-gen table value. A reimplementation that hardcodes 512 will accept out-of-range FIFO addresses that the binary rejects (and reject valid ones on the larger gens only if it clamps low).

---

## Why the Path Is Gen-Gated Off in v0.0.40

> **NOTE — every TensorCore `Target` in this build returns `MatmulResultBufferEntries() == 0`.** `JellyfishTarget` (`0x1d4902c0`), `PufferfishTarget` (`0x1d493b…`), `ViperfishTarget` (`0x1d49ac60`), and `GhostliteTarget` all return `0` (`xor eax,eax; ret`), and the base `Target::MinMrbWriteBlockSize` / `MrbRelativeAddressFromOffset` are `LogFatal` stubs. No subclass compiled into v0.0.40 overrides them to a live value. Consequently `set_mrb_address`'s `MatmulResultBufferEntries() > 0` gate fails and the chain allocator's "Cannot use MRB" guard fires — the FIFO-address path runs with MRB disabled. The **arithmetic, the cursor recurrence, the accessor surface, and both struct layouts are byte-exact CONFIRMED**; only the *concrete numeric geometry* (the live `MinMrbWriteBlockSize` granule and `MrbRelativeAddressFromOffset` map) for the gen that enables MRB is absent from this wheel — it awaits a newer-gen `Target`. Notably `MxuResultEntriesPushed`/`Popped` *are* overridden per-gen (the Viperfish `{2,4,8,8,4,4,4,4}` push table) even with `MatmulResultBufferEntries == 0`.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Two per-MXU `int32` cursors (write/read), sized `num_mxus = Target[+0x4ac]`, memset 0 | `0x10f3efc4`/`0x10f3efe0`; `num_mxus` cross-checked vs `LatchLhs` `0x10f3b791` | CONFIRMED |
| Per-MXU select `unit_id & 3`, gated by the `0x400` "has mxu" bit on WORD[+0xb] | `0x10f3f069` | CONFIRMED |
| Advance by `MxuResultEntriesPushed`/`Popped`, round up to `MinMrbWriteBlockSize`, wrap mod `ResultFifoEntryCount` | `idiv` `@0x10f3f218`, mod `@0x10f3f234`; vtbl `+0x5f0`/`+0x5f8`/`+0x5e0` | CONFIRMED |
| FIFO depth `{16,16,16,48,224,256}` per version | table `@0xb53e240` (`struct.unpack` direct ELF read) | CONFIRMED |
| Physical write via `set_mrb_address` → `set_mrb_address_unrestricted` into `+0x42` | `0x1d4d93c0` / `0x1d4e9780` decompiled | CONFIRMED |
| `0xa5` pre-scan aborts the whole quadrant bounce | `0x10f3fb34` (`cmp …,165`) | CONFIRMED |
| Per-sequence `msr ^= 1` toggle from initial `msr = 1` | `0x10f3fb60` | CONFIRMED |
| MSR byte written to `+0x46`/`+0x44`/`+0x42`/`+0x41` by op family | `set_matrix_staging_register` `0x1d4d7d40` decompiled | CONFIRMED |
| Bounce stamps latches (`+0x18`) and `matmuls[0]` (`+0x48`), not matres | `[seq+0x50]` guarded by "!sequence->matmuls.empty()" `:1063` | CONFIRMED |
| `Msr{0}="msra"`, `Msr{1}="msrb"` | `MsrToString` `0x1d629720` arithmetic | CONFIRMED |
| Per-MXU timeline element stride `0x70`; per-chunk free-pool record stride `0x40` | `0x10f59941` (`×0x70`), `0x10f5fa18` (`<<6`) | CONFIRMED |
| `AccumulationChainAfterSplit` `0x70`-byte field map; AWOC stride `0x60` | `SplitAccumulationChain` `0x10f598e0`; `0x10f59ab6` | CONFIRMED |
| Six `Target` accessors at `+0x390`/`+0x398`/`+0x5e0`/`+0x5e8`/`+0x5f0`/`+0x5f8` | vtable `@0x21cce6a0`, `.rela.dyn`-resolved | CONFIRMED |
| MRB path gen-gated off (all shipped Targets return `MatmulResultBufferEntries==0`) | `JellyfishTarget` `0x1d4902c0` etc. (`xor eax,eax; ret`) | CONFIRMED |
| `MrbRelativeAddressFromOffset` is the matres-side relative-address map | vtbl `+0x5e8` used at `0x10f3f313`; base is `LogFatal` | HIGH |
| Per-MXU cursors reset per quadrant call (no cross-region carry) | cursor `new`/`free` bracket the function body | HIGH |

---

## Related Components

| Component | Relationship |
|---|---|
| `MrbChainAllocator::SplitAccumulationChain` `0x10f598e0` | source of the `0x70`-byte split record + per-MXU timeline index |
| `MrbChainAllocator::ReleaseMrbReservation` `0x10f5f9e0` | the `0x40`-byte free-pool record and MrbEntry recycle |
| `MxuAssigner::VisitRegion` `0x10f3a640` | the per-quadrant `array<Span,4>` dispatch of both functions |
| `MxuAssigner::SetLatchIndices` `0x10f3b4c0` / `LatchLhs` `0x10f3b5e0` (`num_mxus` read `@0x10f3b791`) | the LHS gain-matrix latch side (runs before this) |
| `LloInstruction::mrb_address` `0x1d4e8860` | reads back the `+0x42` field this writes |
| `ViperfishTarget::MxuResultEntriesPushed`/`Popped` `0x1d499ae0`/`0x1d499ba0` | the per-gen push/pop counts the cursors advance by |

## Cross-References

- [MRB Chain Allocator](mrb-chain-allocator.md) — the program-order reservation timeline that hands each chain the `MrbEntry` this page turns into a physical FIFO slot + MSR bank; owns the per-MXU `boost::multi_index` carried in the `0x70`-byte timeline element.
- [MxuSequence / SequenceInfo](mxu-sequence-struct.md) — the per-sequence record whose latches (`+0x18`), matmuls (`+0x48`), and matreses (`+0x60`) these two functions index.
- [MXU Assignment Bin-Packer](mxu-assignment-binpacker.md) — `AssignMxusForSequenceGroup`, which builds the `MxuSequence`s placed here.
- [Latch Assignment & Overrun](latch-assignment-overrun.md) — the `SetLatchIndices`/`LatchLhs` gain-latch side that runs before the FIFO/MSR placement in `VisitRegion`.
- [TPU Scheduling Pipeline](overview.md) — Stage 2's place in the four-stage scheduler stack; this page is the result-FIFO/MSR row of Stage 2.
- [MXU Slot](../isa/slot-mxu.md) — the bundle slot whose `mrb_address` (`+0x42`) and matrix-staging-register fields are serialized from the values placed here; the `GainLatchMode` MSR-A/MSR-B bank model.
- [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md) — the latch ops (`0x8d..0x96`) the MSR bounce stamps.
- [LLO Opcode Enum](../isa/llo-opcode-enum.md) — the `0x9b..0xa5` matmul, `0x8d..0x96` latch (`kVectorLatch*`), `0xaa`/`0xab` load-lmr, `0xa8` done-with-gains, `0x152` matres numeric space.
- [MXU Latency Overview](../cost/mxu-latency-overview.md) — the cost model that consumes the placed MSR bank (the `MatpushModifier` staging-register key) to charge per-bank reservation cycles.
- [MatmulMode & Modifiers](../cost/matmul-mode-modifiers.md) — the `MatmulDataFormat`/`GainLatchMode` ordinals keying `MxuResultEntriesPushed`/`Popped`.
- [MXU Latency: VF](../cost/mxu-latency-vf.md) — the Viperfish reservation matrix indexed by the bank/format the bounce assigns.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
