# EUP Per-Gen Latency Integers

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). `.text`/`.rodata` VMA == file offset. Other versions will differ.*

## Abstract

The Extended Unary Pipeline (EUP) — libtpu's transcendental approximator — is a deep, FIFO-buffered unit driven by a *push* in one bundle and drained by a *pop* one or more bundles later. The scheduler prices the push→pop dependency edge with a single integer: the **push latency**, the minimum number of bundles the pop may trail the push. That integer is the depth the compiler's VALU-correction software pipeline must hide, so it is the highest-entropy number in the whole transcendental cost model. This page is the byte-level transcription of that integer for every generation that carries a heap-grid `Performance` object: the exact `latencies[Instruction]` store the constructor emits, the `Instruction`-ordinal index it writes, and the resulting push→pop edge weight.

The mechanism is uniform across the newer gens and is the libtpu analog of reading one cell out of an LLVM `SchedMachineModel`. `LatencyTable<Gen>::LatencyBetweenInternal` resolves the push opcode to a per-gen `Performance::Instruction` ordinal (`Get<Gen>Instruction`), then reads `<Gen>Performance::GetLatency(ordinal) = latencies[ordinal]` — a flat `int32` heap array whose pointer is at `Performance[0]` and whose element count is at `Performance[+0x8]`. The constructor fills that array element-wise with `mov dword ptr [array + Instruction*4], value` stores, after a leading `memset(_, 0xff, _)` that sentinels every unwritten slot. Recovering the per-gen EUP latency is therefore exactly recovering the value at the EUP-push ordinal in each constructor's fill — which this page does, store by store. The legacy Jellyfish/Dragonfish path does *not* use this array; it clamps the edge to a flat constant (`Performance[+0x30]` = 4) and is documented only as a baseline here.

The page is organized one `##` unit per `Performance` class — Pufferfish TensorCore (variant 0), Pufferfish BarnaCore (variant 1), Viperfish, Ghostlite, and the `6acc60406` GhPerf twin — each giving the constructor address, the `new` size that fixes the array cardinality, the EUP push/pop store offsets, and the integer. The grids these same constructors fill (the `GetResourceUsage` occupancy matrix) and the opcode→ordinal classifier tables live on the [`performance-*`](performance-pf.md) siblings and the [slot page](../isa/slot-eup-transcendental.md); this page is only the latency rows.

For reimplementation, the contract is:

- The per-gen EUP push→pop edge integer: **PF 7, VF 6, GL 13 (F32) / 14 (BF16), pop 1** — and the legacy clamp (Jf/Df 4) it replaces.
- The exact `latencies[]` store: constructor address, `new` size → array cardinality, `Instruction` ordinal, byte offset (`ordinal*4`), and value.
- That the EUP push value is **uniform across all classified EUP functions** on each gen (one transcendental latency per datatype), so the cost model uses a single constant, not a per-function table.
- The Pufferfish `variant<TensorCore, BarnaCore>` split: the EUP edge is the TensorCore array (7); BarnaCore is a separate 134-entry array with its own 6-cycle EUP block.
- That this latency edge is **orthogonal** to `VectorEupReservationCycles` (the EUP issue rate) — it is never multiplied by it.

| | |
|---|---|
| **Read path** | `<Gen>Performance::GetLatency(instr)` = `latencies[instr]` (`int32`, ptr@`Perf[0]`, count@`Perf[+0x8]`) |
| **Edge selector** | `LatencyTable<Gen>::LatencyBetweenInternal` → `Get<Gen>Instruction(push)` → `GetLatency` |
| **PF push / pop** | `7` (Instr 0x67..0x6c) / `1` (Instr 0x76) — `PufferfishPerformance` ctor `@0x1c8be080` |
| **VF push / pop** | `6` (Instr 0xcc..0xd2) / `1` (Instr 0x168) — `ViperfishPerformance` ctor `@0x1c8c4840` |
| **GL push / pop** | `13` F32 (Instr 0x106..0x10f) · `14` BF16 (Instr 0x110..0x118) / `1` (Instr 0x1c4) — ctor `@0x1c8cbc80` |
| **Legacy clamp** | Jf/Df push→pop = `4` (`Performance[+0x30]`, not a per-instruction array) |
| **Reservation** | `VectorEupReservationCycles` JF/VF/GL = 1, PF = 2 — orthogonal to the latency (never multiplied) |

---

## The Lookup and Its Two Anchors

### Purpose

Every per-gen latency integer on this page is read by one tiny accessor and written by one constructor. Pinning both is what makes the integer reimplementation-grade: the accessor proves *how* the ordinal indexes the array, and the constructor store proves the *value* at that ordinal.

### The Accessor

`<Gen>Performance::GetLatency` is byte-identical across the three newer gens — a bounds-checked `int32` array load:

```c
int32 GetLatency(Performance* p, u32 instr):   // VF @0x1c8cbc20, PF-TC @0x1c8c3860,
    if (p->count <= instr)                      //   GL @0x1c8d36e0, PF-BC @0x1c8c47e0
        BUG()                                   // p->count is Performance[+0x8]
    return p->latency[instr]                    // p->latency is Performance[0]; int32 stride
```

So the EUP push edge is `latency[Get<Gen>Instruction(push_opcode)]`. The classifier `Get<Gen>Instruction` maps the LLO push opcode (`0x128`..`0x13a`) and the `0x14e` pop to a per-gen `Instruction` ordinal; those ordinals are the indices this page reads. The classifier tables (VF/PF dense jump tables, GL's 258-entry sorted `(u16,u16)` pair table) are transcribed on the [slot page](../isa/slot-eup-transcendental.md); here only the resulting ordinals matter.

### The Constructor Store

Each `Performance` constructor allocates the latency array with `operator new(size)` (so `size/4` is the cardinality), `memset`s it to `0xff`, then overwrites slots with `mov dword ptr [array + ordinal*4], value`. The EUP rows are a contiguous run of identical-value stores. Reading the EUP latency is reading the value immediate at the EUP-push offset:

```text
ctor:
  rax = operator new(SIZE)        ; SIZE/4 = Instruction cardinality
  Perf[0]   = rax                 ; latency array pointer
  Perf[+8]  = Perf[+0x10] = COUNT ; element count (== SIZE/4)
  memset(rax, 0xff, SIZE)         ; sentinel = 0xffffffff
  ...
  cmp [Perf+8], ORDINAL ; jbe BUG ; rax = [Perf] ; mov [rax + ORDINAL*4], VALUE
```

> **NOTE —** the byte offset in a store (`[rax+0x418]`, or `[rax+412LL]` in the decompiler's decimal) is always `ordinal*4`. Throughout this page the offset is given in hex and the ordinal in the same row, so a reimplementer can cross-check either way: `0x418 = 0x106*4`, `0x19c = 0x67*4`, `0x330 = 0xcc*4`.

> **QUIRK —** on PF the `memset` sentinel (`0xff` → `0xffffffff`) never reaches a consumer because all 336 slots are overwritten. On GL/GF the `0xff` survives on unpriced rows. Either way the EUP rows are explicit stores, never the sentinel — a reimplementation must write them.

---

## Pufferfish — TensorCore Variant (push 7)

### Purpose

`PufferfishPerformance` is the TensorCore arm (variant 0) of the `variant<PufferfishPerformance, PufferfishBarnaCorePerformance>` that `LatencyTablePufferfish` prices. Every EUP opcode classifies to variant 0, so the Pufferfish EUP push→pop edge is the TensorCore array value.

### Edge Integer

| Role | LLO opcode | `Instruction` | Byte offset | Value | Confidence |
|---|---|---|---|---|---|
| EUP push (rsqrt) | `0x12c` | `0x67` | `0x19c` | 7 | CERTAIN |
| EUP push (pow2) | `0x129` | `0x68` | `0x1a0` | 7 | CERTAIN |
| EUP push (log2) | `0x12b` | `0x69` | `0x1a4` | 7 | CERTAIN |
| EUP push (tanh) | `0x128` | `0x6a` | `0x1a8` | 7 | CERTAIN |
| EUP push (recip) | `0x12a` | `0x6b` | `0x1ac` | 7 | CERTAIN |
| EUP push (pushErf) | `0x131` | `0x6c` | `0x1b0` | 7 | CERTAIN |
| EUP pop | `0x14e` | `0x76` | `0x1d8` | 1 | CERTAIN |

`PufferfishPerformance` ctor `@0x1c8be080`: `operator new(0x540)` = 1344 B = **336** `int32`; `Perf[+8] = Perf[+0x10] = 336`; `memset(array, 0xff, 1344)`. The six EUP-push stores are contiguous (`mov [rax+412LL]=7` … `[rax+432LL]=7`, decimal `412 = 0x19c` … `432 = 0x1b0`), and the pop store is `mov [rax+472LL]=1` (`472 = 0x1d8`). The value is **uniform 7** across all six classified F32 EUP functions.

> **NOTE —** the Pufferfish `kVectorSigShftF32` (`0x12d`) push falls to the classifier default — `GetPufferfishInstruction` emits no variant-0 ordinal for it, unlike the other six F32 pushes. Its effective edge is most plausibly 7 by the uniform-EUP pattern, but the ordinal was not pinned (INFERRED). All six *classified* PF EUP functions are 7.

---

## Pufferfish — BarnaCore Variant (push 6)

### Purpose

Pufferfish is the last generation that ships a BarnaCore embedding engine. `LatencyTablePufferfish` holds two grids in a `std::variant`; the 2-arm `__fmatrix LatencyFromInstruction` visitor (`@0x21c203d0`) selects variant 1 — `PufferfishBarnaCorePerformance` — for the legacy embedding-engine ops. Variant 1 is a **separate 134-entry array** with its own, lower, EUP block.

### Variant Dispatch

The variant index is the high 16 bits of `GetPufferfishInstruction`'s return; `LatencyTablePufferfish::LatencyBetweenInternal` (`@0x1c8a2aa0`) shifts it (`shr r14d,0x10`) and calls `fmatrix[variant]`. The two dispatch arms differ in how they read the `Instruction`:

```c
dispatch<0>(holder):   // @0x1c8a3140 — TensorCore
    return PufferfishPerformance::GetLatency(holder[0], (u16)*instr)   // 16-bit ordinal

dispatch<1>(holder):   // @0x1c8a3160 — BarnaCore
    return PufferfishBarnaCorePerformance::GetLatency(holder[8], (u8)*instr)  // 8-bit ordinal
```

The BarnaCore arm reads the `Instruction` as a **byte** (`unsigned __int8 *`) and loads the BarnaCore holder from `[holder+8]` (vs `[holder+0]` for TensorCore) — byte-confirmed in the `<1ul>` dispatcher body. Every EUP push opcode emits variant 0, so the Pufferfish EUP *edge* is 7; the BarnaCore block below is reached through the `PufferfishBarnaCoreChannelEmitter`, not through the LLO-level EUP push, and prices the legacy embedding-engine transcendentals.

### Edge Integer

| Role | `Instruction` | Byte offset | Value | Confidence |
|---|---|---|---|---|
| BarnaCore EUP/transcendental block | `0x77`..`0x7c` (6 entries) | `0x1dc`..`0x1f0` | 6 | CERTAIN |
| `kBarnaCoreScalarSyncDoneRead` | `0x3d` | `0xf4` | 3 | CERTAIN |
| `kBarnaCoreVectorStore` (memory op) | `0x85` | `0x214` | 12 | CERTAIN |
| no-op / null slot | `0x01` | `0x04` | 0 | CERTAIN |

`PufferfishBarnaCorePerformance` ctor `@0x1c8c38c0`: `operator new(0x218)` = 536 B = **134** `int32`; `memset(array, 0xff, 536)`; `Perf[+8] = 134`. The EUP block is six contiguous `mov [rax+476LL]=6` … `[rax+496LL]=6` stores (decimal `476 = 0x1dc` … `496 = 0x1f0`); the VectorStore store is `[rax+532LL]=12` (`532 = 0x214`). The 134-entry array is dominated by 1-cycle scalar/sync ops; the recovered non-default values are six `6` (the EUP block), one `12` (VectorStore), two `3`, two `4`, and one `0` — the legacy embedding-engine latency model.

> **QUIRK —** the BarnaCore EUP block is **6 cycles, one less than the TensorCore EUP (7)**. The 6-entry width matches the 6-function TensorCore EUP block shape, and a cheaper EUP is structurally consistent with the smaller 134-entry BarnaCore ISA. The exact BarnaCore-`Instruction`→function name for `0x77..0x7c` was not pinned — those ordinals are reached via the channel emitter, not `GetPufferfishInstruction`, and there is no `BarnaCoreInstructionToString` (INFERRED that the block is the embedding-engine transcendentals).

---

## Viperfish (push 6)

### Purpose

`ViperfishPerformance` is the first single-grid newer gen — no variant, no BarnaCore. `LatencyTableViperfish::LatencyBetweenInternal` (`@0x1c8a4ac0`) runs `GetViperfishInstruction(push)` → `GetLatency` directly.

### Edge Integer

| Role | LLO opcode | `Instruction` | Byte offset | Value | Confidence |
|---|---|---|---|---|---|
| EUP push (rsqrt) | `0x12c` | `0xcc` | `0x330` | 6 | CERTAIN |
| EUP push (pow2) | `0x129` | `0xcd` | `0x334` | 6 | CERTAIN |
| EUP push (log2) | `0x12b` | `0xce` | `0x338` | 6 | CERTAIN |
| EUP push (tanh) | `0x128` | `0xcf` | `0x33c` | 6 | CERTAIN |
| EUP push (sigshft) | `0x12d` | `0xd0` | `0x340` | 6 | CERTAIN |
| EUP push (recip) | `0x12a` | `0xd1` | `0x344` | 6 | CERTAIN |
| EUP push (pushErf) | `0x131` | `0xd2` | `0x348` | 6 | CERTAIN |
| EUP pop | `0x14e` | `0x168` | `0x5a0` | 1 | CERTAIN |

`ViperfishPerformance` ctor `@0x1c8c4840`: `operator new(0x600)` = 1536 B = **384** `int32`; `Perf[+8] = Perf[+0x10] = 0x180` (384); `memset(array, 0xff, 1536)`. The seven EUP-push stores are contiguous `mov dword ptr [rax+0x330],6` … `[rax+0x348],6` (`@0x1c8c5d4a`..`@0x1c8c5dec`), and the pop store is `mov dword ptr [rax+0x5a0],1` (`@0x1c8cadff`). Viperfish classifies **all seven** F32 EUP pushes (including `sigshft`, unlike PF) and the value is uniform 6.

> **NOTE —** Viperfish has `SupportsBf16AluInstructions` FALSE, so the late decomposer widens a BF16 transcendental to the F32 push; the BF16 EUP opcodes (`0x132`..`0x13a`) fall to the classifier default and there is no distinct BF16 latency slot. VF therefore has a *single* EUP latency (6), where Ghostlite splits it.

---

## Ghostlite (push 13 F32 / 14 BF16)

### Purpose

`GhostlitePerformance` is the production V5e/V6e-class table. Ghostlite has a native BF16 ALU (`SupportsBf16AluInstructions` TRUE), so its classifier maps **both** the F32 pushes and the BF16 pushes to distinct latency slots — the only gen that splits the EUP latency by datatype. `LatencyTableGhostlite::LatencyBetweenInternal` (`@0x1c8b22e0`) → `GetGhostliteInstruction` (sorted `(u16,u16)` pair table `@0x4067dc8`) → `GetLatency`.

### The Full 9-Transcendental Block

The constructor fills a contiguous F32 run (`Instruction 0x106..0x10f`, all 13) and a contiguous BF16 run (`0x110..0x118`, all 14). There is **no per-function deviation**: `tanh = pow2 = log2 = rsqrt = sigshft = recip = pushErf = sinq = cosq = erf`, identical per datatype. The cost model can size the VALU-correction window to a single GL EUP constant (13/14), not a per-function table.

| Function | F32 push opc | F32 `Instr` | F32 lat | BF16 push opc | BF16 `Instr` | BF16 lat |
|---|---|---|---|---|---|---|
| rsqrt | `0x12c` | `0x106` | 13 | `0x136` | `0x110` | 14 |
| pow2 (2^x) | `0x129` | `0x107` | 13 | `0x133` | `0x111` | 14 |
| log2 | `0x12b` | `0x108` | 13 | `0x135` | `0x112` | 14 |
| tanh | `0x128` | `0x109` | 13 | `0x132` | `0x113` | 14 |
| sigshft | `0x12d` | `0x10a` | 13 | `0x137` | `0x114` | 14 |
| recip | `0x12a` | `0x10b` | 13 | `0x134` | `0x115` | 14 |
| pushErf | `0x131` | `0x10c` | 13 | — | — | — |
| sinq | `0x12e` | `0x10d` | 13 | `0x138` | `0x116` | 14 |
| cosq | `0x12f` | `0x10e` | 13 | `0x139` | `0x117` | 14 |
| erf | `0x130` | `0x10f` | 13 | `0x13a` | `0x118` | 14 |
| **EUP pop** | `0x14e` | `0x1c4` | **1** | (same) | (same) | **1** |

All rows CERTAIN. F32 has 10 entries (`0x130 kVectorErfF32` *and* `0x131 kVectorPushErf` are both classified); BF16 has 9 (no separate push-form erf).

### Edge Integer

`GhostlitePerformance` ctor `@0x1c8cbc80` (clean symbol `_ZN3xla9ghostlite20GhostlitePerformanceC1Ev`): `operator new(0x770)` = 1904 B = **476** `int32`; `Perf[+8] = Perf[+0x10] = 0x1dc` (476); `memset(array, 0xff, 1904)`. Three contiguous store runs:

| Block | Ordinals | Byte offsets | Value | Store window |
|---|---|---|---|---|
| F32 EUP push (10) | `0x106`..`0x10f` | `0x418`..`0x43c` | `0xd` (13) | `@0x1c8cd80f`..`@0x1c8cd902` |
| BF16 EUP push (9) | `0x110`..`0x118` | `0x440`..`0x460` | `0xe` (14) | `@0x1c8cd91d`..`@0x1c8cd9f5` |
| EUP pop | `0x1c4` | `0x710` | `1` | `@0x1c8d2864` |

> **NOTE —** The GL F32 EUP fill is **ten** contiguous slots `0x106..0x10f`, all 13, including `sinq`/`cosq`/`erf` F32 (`0x10d..0x10f`), byte-confirmed at offsets `0x418..0x43c`; the 9 BF16 slots `0x110..0x118` are all 14. Every EUP function shares one per-precision latency — there is no per-function outlier (no `erf`/`sigshft` deviation), so GL needs no per-function EUP latency table.

> **NOTE —** the BF16 push costs one extra cycle (14 vs 13). This is the latency-level reflection of GL keeping the 16-bit lane (`SupportsBf16AluInstructions` TRUE): GL pays a distinct BF16 EUP latency where VF/PF unpack to F32 and pay the single F32 latency. The 192/182 and 212/204 figures on the [`performance-gl`](performance-gl-ghperf.md) / [`performance-gf`](performance-gf-ghperf.md) pages are the EUP-*prep* grid-band cost magnitudes — a different quantity from this push→pop latency edge.

---

## 6acc60406 GhPerf Twin (push ~10 F32 / ~11 BF16 — LOW)

### Purpose

The `6acc60406`-line (GF) GhPerf object is built by a distinct constructor `sub_1C8D3740`, structurally the same `GhostlitePerformance` layout but with a 465-row instruction set (vs GL's 476). Its cycle-table twin `GfcCycleTable` is registered immediately after `GlcCycleTable` (ghostlite) in the `cycle_table.cc` `FunctionRegistry`, keyed to the post-ghostlite `TpuVersion`. It fills its **own** latency array — it does not share GL's instance — and the EUP-shaped block carries different integers from GL.

### Edge Integer

| Block | Byte offsets (same as GL) | Value | Confidence |
|---|---|---|---|
| F32 EUP-shaped run (head, 3 slots) | `0x410`..`0x418` | 2 | LOW |
| F32 EUP-shaped run (rest, 9 slots) | `0x41c`..`0x43c` | `0xa` (10) | LOW |
| BF16 EUP-shaped run (9 slots) | `0x440`..`0x460` | `0xb` (11) | LOW |
| post-BF16 tail | `0x464`..`0x46c` | 1 | LOW |
| pop-position slot | `0x710` | 2 | LOW |

`sub_1C8D3740`: `operator new(0x744)` = 1860 B = **465** `int32` (one row short of GL's 476); `Perf[+8] = Perf[+0x10] = 0x1d1` (465); `memset(_, 0xff, 0x744)`. The latency fill differs from GL's. The stores at `0x410..0x460` are byte-exact and contiguous: three `mov [rax+off],2` at `0x410`/`0x414`/`0x418` (`@0x1c8d5367`..`@0x1c8d539d`), nine `mov [rax+off],0xa` at `0x41c..0x43c` (`@0x1c8d53b8`..`@0x1c8d5490`), nine `mov [rax+off],0xb` at `0x440..0x460` (`@0x1c8d54ab`..`@0x1c8d5583`), then `1`s from `0x464`; the pop-position slot `0x710` is `2` (`@0x1c8d97d1`).

> **GOTCHA —** the GF values are byte-exact *at the offsets*, but the GF opcode→`Instruction` classifier was **not traced** in this analysis, and the F32 EUP-shaped run is not uniform: the head is **three** slots (`0x410`/`0x414`/`0x418`) of value 2, then nine slots of 10 (`0x41c..0x43c`). That split breaks the single-value-per-datatype shape GL has, which is evidence the GF `Instruction` enum is *not* 1:1 with GL's (465 vs 476 rows shifts the mapping), so reading `0x418` as "GF rsqrt" is unsound. Do not assume the GF EUP push edge is 10/11 by offset analogy. Until the GF classifier is decoded, the binding of these offsets to the EUP transcendentals is LOW; only `LatencyTableGhostlite` (one instance, `@0x1c8b22e0`) is confirmed to route the *Ghostlite* push→pop edge through `GhostlitePerformance::GetLatency`.

---

## Legacy Baseline — Jellyfish / Dragonfish (clamp 4)

The pre-Pufferfish family does not use a per-instruction latency array for the EUP edge. `LatencyTableJellyfish::LatencyBetweenInternal` (`@0x1c8a0d60`), when the first opcode is a bare push (`∈ [0x128, 0x13a]`) and the second is the `0x14e` pop, clamps the edge to `this+0x1c`, which the constructor copies from `Performance[+0x30]` = 4. Dragonfish inherits the same clamp. So the EUP push→pop edge is a flat **4** on Jf/Df — the same notion (minimum push→pop bundle gap) expressed through a single per-table field instead of a per-`Instruction` array. This is the only EUP latency on these gens; the array mechanism is a Pufferfish-and-later construct.

---

## Per-Gen Consolidated Table

| Gen | EUP push latency | Pop latency | Mechanism | Constructor | Push byte anchor |
|---|---|---|---|---|---|
| Jellyfish | 4 | (clamp) | `Performance[+0x30]` = 4 clamp | `LatencyTableJellyfish` | n/a (flat field) |
| Dragonfish | 4 (inherits Jf) | (clamp) | `PerformanceDf` keeps `+0x30` = 4 | (inherits) | n/a |
| Pufferfish (TensorCore) | 7 | 1 | `latency[0x67..0x6c]` | `@0x1c8be080` | `[rax+0x19c..0x1b0]=7` |
| Pufferfish (BarnaCore) | 6 | (via channel emitter) | `latency[0x77..0x7c]` | `@0x1c8c38c0` | `[rax+0x1dc..0x1f0]=6` |
| Viperfish | 6 | 1 | `latency[0xcc..0xd2]` | `@0x1c8c4840` | `[rax+0x330..0x348]=6` |
| Ghostlite | 13 (F32) / 14 (BF16) | 1 | `latency[0x106..0x118]` | `@0x1c8cbc80` | `[rax+0x418..0x43c]=0xd` / `[0x440..0x460]=0xe` |
| `6acc60406` (GF, LOW) | ~10 / ~11 (offset-only) | ~2 | `latency[0x410..0x460]` | `sub_1C8D3740` | `[rax+0x41c..0x43c]=0xa` / `[0x440..0x460]=0xb` |

Every value above is the *push* latency — the push→pop dependency-graph edge weight. The pop's own latency (1 on PF/VF/GL) is what the drained EUP result carries downstream once popped.

---

## Latency vs Reservation — Not a Product

The EUP push→pop edge is bounded by **two independent quantities read from two different arrays**, and a reimplementer who multiplies them gets the wrong schedule:

| Quantity | Bounds | Source | PF | VF | GL (F32/BF16) | JF |
|---|---|---|---|---|---|---|
| push→pop data latency | min bundles push → its drain (pop) | `latency[Get<Gen>Instr(push)]` | 7 | 6 | 13 / 14 | 4 (clamp) |
| pop latency | latency the drained value carries | `latency[pop Instr]` | 1 | 1 | 1 | — |
| `VectorEupReservationCycles` | min bundles push → *next* push | Target accessor (vtable `+0x480`) | 2 | 1 | 1 | 1 |

`LatencyTable::LatencyBetween` (`@0x1c89f820`) calls the per-gen `LatencyBetweenInternal` (`call [rax+0x18]`), optionally adds a uniform-random jitter, special-cases **only** the matres/transpose opcodes (`0x82`/`0x84`) with an MXU floor, and returns the edge **unchanged** for the EUP push. There is no multiply by any reservation field anywhere on the path. The per-gen `LatencyBetweenInternal` main paths (GL general arm, VF wrapper `@0x1c8a4480`) apply a transpose-latch halving (`latency/2`, with a `(lat & 0x80000001)==1` round-up correction) gated on `LatchModeIsTranspose(operand)` — `if (!LatchModeIsTranspose(...)) latency = latency/2 + ...`. The EUP push is not a transpose/latch-mode op, so the halving never applies to it and `latency[Instruction]` passes through verbatim.

`VectorEupReservationCycles` is the orthogonal EUP-unit **issue occupancy**: how many bundles the EUP resource stays reserved after a push, applied by the per-instruction resource model (`GetResourceUsage` matrix + the `SlotTracker` reservation), not by the latency edge. The composition is `max(latency-deadline, resource-availability)`, never a product: a pop is placed no earlier than `push_bundle + latency`; consecutive pushes are no closer than `reservation` bundles.

> **GOTCHA —** The latency edge is **not** scaled by the reservation. Pufferfish's half-rate EUP (reservation 2) bounds the *issue rate* (one push per 2 bundles), not the push→pop window — the push returns `latency[Instruction]` unmodified. The VALU-correction software-pipeline depth the decomposer must fill equals the **latency** (PF 7), independent of the reservation. A chain of N independent transcendentals on PF is EUP-bound at ≈ 2·(N−1) + 7 bundles (issue-rate-limited spacing plus the final drain), not 7·2.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LatencyTable::LatencyBetween` | `0x1c89f820` | dispatcher; returns EUP edge unmodified | CERTAIN |
| `LatencyTableJellyfish::LatencyBetweenInternal` | `0x1c8a0d60` | Jf/Df EUP clamp to `Performance[+0x30]` = 4 | CERTAIN |
| `LatencyTablePufferfish::LatencyBetweenInternal` | `0x1c8a2aa0` | variant select via `shr r14d,0x10` → `fmatrix` | CERTAIN |
| `LatencyTableViperfish::LatencyBetweenInternal` | `0x1c8a4ac0` | VF EUP edge via `GetViperfishInstruction` → `GetLatency` | CERTAIN |
| `LatencyTableGhostlite::LatencyBetweenInternal` | `0x1c8b22e0` | GL/GF EUP edge via `GetGhostliteInstruction` → `GetLatency` | CERTAIN |
| `__fmatrix LatencyFromInstruction` visitor | `0x21c203d0` | 2-arm `variant<TensorCore,BarnaCore>` dispatch | CERTAIN |
| `dispatch<0ul>` / `dispatch<1ul>` | `0x1c8a3140` / `0x1c8a3160` | TensorCore (u16 ordinal) / BarnaCore (u8 ordinal) | CERTAIN |
| `PufferfishPerformance` ctor | `0x1c8be080` | fills TensorCore EUP push = 7, pop = 1 | CERTAIN |
| `PufferfishBarnaCorePerformance` ctor | `0x1c8c38c0` | fills BarnaCore EUP block = 6, VectorStore = 12 | CERTAIN |
| `ViperfishPerformance` ctor | `0x1c8c4840` | fills EUP push = 6, pop = 1 | CERTAIN |
| `GhostlitePerformance` ctor | `0x1c8cbc80` | fills F32 EUP = 13, BF16 = 14, pop = 1 | CERTAIN |
| `sub_1C8D3740` (GF GhPerf ctor) | `0x1c8d3740` | fills 465-row array; EUP-shaped block 10/11 (binding LOW) | LOW |
| `<Gen>Performance::GetLatency` | `0x1c8cbc20` (VF), `0x1c8c3860` (PF-TC), `0x1c8d36e0` (GL), `0x1c8c47e0` (PF-BC) | `latency[Instruction]` heap lookup | CERTAIN |
| `GetGhostliteInstruction` | `0x1c8b1740` | sorted `(u16,u16)` pair table `@0x4067dc8` (258 entries) | CERTAIN |

---

## Cross-References

- [EUP Latency Overview](eup-latency-overview.md) — the push→pop software-pipelining model and where this integer sits in the cost model
- [Slot — EUP & Transcendentals](../isa/slot-eup-transcendental.md) — the push/pop encoding, the classifier tables, and the latency-vs-reservation orthogonality in full
- [Performance: PF](performance-pf.md) — the 336-row PF grid, the variant dispatch, and the EUP push grid occupancy
- [Performance: VF](performance-vf.md) — the 384-row VF grid and why VF prices the EUP push via the latency array alone
- [Performance: GL (GhPerf 476×31)](performance-gl-ghperf.md) — the 476-row GL grid and the EUP-prep band cost magnitudes (192/182)
- [Performance: GF (GhPerf 465×31)](performance-gf-ghperf.md) — the distinct 465-row `6acc60406` GhPerf instance built by `sub_1C8D3740`
- [EUP Correction Coefficients](eup-correction-coeffs.md) — the Newton/rational refinement coefficients the latency window hides
- [Payne-Hanek Range Reduction](eup-paynehanek.md) — the trig 1/(2π) reduction that pairs with the sinq/cosq EUP push
