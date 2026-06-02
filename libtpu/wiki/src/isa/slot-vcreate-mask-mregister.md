# vcreate_mask and the M-Register (Vector-Mask) File

> *Every offset, address, bit position, constant, and immediate on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). `.text`/`.rodata` VMAs equal their file offsets. Other libtpu builds differ.*

## Abstract

The SparseCore VPU does per-lane predication through a 32-entry **vector-mask register file** (M0..M31, LLVM `MPRRegClass`). A masked vector op names one M-register by a 5-bit selector; the named M-register *is* the 2D `{sublane-range × lane-range}` active/inactive predicate. This page documents how an M-register is *built* — the `vcreate_mask` (LLVM-MIR `llo.vcmask`) instruction whose 32-bit scalar immediate packs the four range bounds at the field offsets returned by `Target::GetVcmaskFieldOffsets()` — and how that built mask is then *consumed*, with the masked-scan family as the worked example.

There are two architectural facts a reimplementer must reproduce exactly and one that is easy to get wrong. First, the predicate is **range-based, not a stored bitmask**: an M-register encodes a rectangle `{sublane ∈ [s_start, s_end]} ∧ {lane ∈ [l_start, l_end]}`, and the end bounds are **inclusive** (the last active index). Second, only two of the six target generations have the native `vcreate_mask` instruction — Viperfish (v5p SC) and Ghostlite (v6e/v7x SC); the other three (Jellyfish, Dragonfish, Pufferfish) **synthesize** the same rectangle from iota comparisons, never reading the field offsets. Third — the masked-scan trap — the M-mask is an **intrinsic operand of the scan op itself** (`sc_tpu.scan` operand[0]); it is *not* a compiler-inserted post-scan select. Inactive input lanes contribute the reduction identity.

For reimplementation, the contract is:

- The native packed-word layout: `WORD = (s_start<<0) | (l_start<<3) | (s_end<<10) | (l_end<<13)` — a 3-bit sublane field (⇒ 8 sublanes) and a 7-bit lane field (⇒ ≤128 lanes), gen-stable across both native gens.
- The per-gen `GetVcmaskFieldOffsets()` / `HasVcmaskInstruction()` sweep: which gens are native vs synthesized, and that the field offsets `{0,3,10,13}` do **not** drift with the per-gen lane count.
- The end-inclusivity convention and the `+1`/`−1` adjustments at the three builder-API boundaries (`CreateVmask` inclusive, `CreateLaneVmask`/`CreateSublaneVmask` half-open).
- The masked-scan inactive-lane model: M-mask = scan-op operand[0], inactive *input* lanes = reduction identity, no per-family compiler-chosen else operand.

| | |
|---|---|
| **Mask register file** | `Vmreg` / LLVM `TPU::MPRRegClass` @ `0x2192f0c0` — 32 entries M0..M31 |
| **Mask selector (read band)** | `GetVectorMask` @ `0x13a33320` — reg-id ∈ `[0x5f, 0x7e]`, value = `regid − 0x5f` ∈ `[0,31]` |
| **Mask write-destination band** | `GetVMDestregno` @ `0x13a65b20` — reg-id ∈ `[0x5f, 0x6e]` = M0..M15 (16-deep write subset) |
| **Native build op** | `LloRegionBuilder::Vcmask(s_start, s_end, l_start, l_end)` @ `0x1d53f9c0` → `llo.vcmask` |
| **Packed word** | `(s_start<<0) \| (l_start<<3) \| (s_end<<10) \| (l_end<<13)` (offsets `{0,3,10,13}`) |
| **Field offsets** | `Target::GetVcmaskFieldOffsets()` (vtable `+0x420`) → `{0,3,10,13}` on Viperfish + Ghostlite |
| **Native gate** | `Target::HasVcmaskInstruction()` (vtable `+0x410`) — `1` on VF/GL, `0` on JF/DF/PF |
| **MLIR consumer op** | `mlir::llo::VectorCreateMaskOp::create` @ `0x13fb3ba0` → mnemonic `llo.vcmask` |
| **Masked scan op** | `mlir::sparse_core::ScanOp::create(builder, loc, Type, MASK, DATA, reduction)` @ `0x145f93e0` → `sc_tpu.scan` |
| **Geometry** | SublaneCount = 8 (3-bit fields); LaneCount ≤ 128 (7-bit field) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The M-Register Is a Range Rectangle, Not a Bitmask

The first reimplementation trap is treating an M-register as a per-lane bit array. It is not stored that way. Each M-register holds a 2D rectangle descriptor: a `{sublane-range × lane-range}` pair of inclusive bounds. The full per-(lane × sublane) predicate is *materialized from* that rectangle at the consumer, not held bit-by-bit in the register.

The 5-bit field in the consuming op (the VEX bundle's `bit0x104`, see [VPU Slot §predicate and vector-mask files](slot-vpu.md#predicate-and-vector-mask-register-files)) is only a *selector*: it names which of the 32 M-registers supplies the predicate. `GetVectorMask` (`0x13a33320`) decodes it byte-exactly — assert `operand.isReg()`, then reg-id `∈ [0x5f, 0x7e]` (asserts `"regno >= llvm::TPU::M0"` / `"regno <= llvm::TPU::M31"`, both present in `.rodata`), returning `regid − 0x5f`:

```c
// GetVectorMask<SparsecoreVectorMask> @ 0x13a33320 (decompiled, exact)
if (op.kind != Reg)        LogFatal("operand.isReg()");          // isa_emitter_base.h:555
v = op.reg;                                                       // [a1 + 8]
if (v <= 0x5E)             LogFatal("regno >= llvm::TPU::M0");    // :557
if (v >= 0x7F)             LogFatal("regno <= llvm::TPU::M31");   // :558
return v - 95;             // regid - 0x5f  ∈ [0, 0x1f]  = M0..M31
```

> **GOTCHA — selector ≠ predicate.** The 5-bit field is an *index*, not a mask. Treating it as a lane bitmask, a sublane count, or a predicate value mis-encodes every masked vector op. It names one of 32 mask registers; the chosen register holds the rectangle. The selector is also orthogonal to whole-op scalar predication (the [Predicate File](slot-predicate.md)'s `Preg`), which gates the entire instruction — a masked vector op carries *both* a per-lane M-mask and a whole-op `Preg` guard.

The 32-deep read band has a 16-deep write subset. Ops that *produce* a mask result (index-scan, uniquify) write through `GetVMDestregno` (`0x13a65b20`), whose band is `[0x5f, 0x6e]` = M0..M15 (assert `"regno <= llvm::TPU::M15"`, present in `.rodata`). So M16..M31 are read-only predicate inputs (compiler-materialized `vcreate_mask` results); only M0..M15 are legal op-write destinations. The read-32 / write-16 split is the M-register-file partition model.

---

## Native vcreate_mask — the Packed-Word Layout

On the two native SC gens, an M-register is built by one `vcreate_mask` instruction: a single 32-bit scalar immediate packs the four rectangle bounds, which `LloInstruction::CreateVectorCreateMask` turns into an `llo.vcmask` op. The packing function is `LloRegionBuilder::Vcmask(s_start, s_end, l_start, l_end)` (`0x1d53f9c0`), whose body — confirmed byte-exact in the decompile — reads the field offsets from the target vtable and shifts each bound into place:

```c
// LloRegionBuilder::Vcmask(s_start, s_end, l_start, l_end) @ 0x1d53f9c0 (decompiled)
//   args: a2 = s_start, a3 = s_end, a4 = l_start, a5 = l_end
offs = (*vtable[1056])(target);            // vtable +0x420 = GetVcmaskFieldOffsets()
//   rax (offs.lo) = 0x300000000 → off0 = 0  (low dword), off1 = 3  (high dword)
//   rdx (offs.hi) = 0xd0000000a → off2 = 10 (low dword), off3 = 13 (high dword)
WORD = (s_start << off0)                   //  a2 << 0
     | (l_start << off1)                   //  a4 << 3
     | (s_end   << off2)                   //  a3 << 10
     | (l_end   << off3);                  //  a5 << 13
imm  = LloModule::ScalarU32ConstantImpl(WORD);             // @ 0x1d506020
m    = LloInstruction::CreateVectorCreateMask(imm);        // @ 0x1d4db820 → llo.vcmask
LloRegion::AppendInstruction(region, m);                   // @ 0x1d50f9a0
```

The decompiler renders the four shifts as `(a4 << SBYTE4(v11)) | (a2 << v11) | (a3 << v12) | (a5 << SBYTE4(v12))`, where `v11 = 0x300000000` (so `v11`-as-int32 = 0, `SBYTE4(v11)` = 3) and `v12 = 0xd0000000a` (so `v12`-as-int32 = 10, `SBYTE4(v12)` = 13). The instruction additionally stamps a human-readable annotation `[s_start:s_end,l_start:l_end]` (built by `CatPieces` from the literals `"["`, `":"`, `","`, `":"`, `"]"`), which independently confirms the `(s_start, s_end)` / `(l_start, l_end)` field assignment.

| Field | Vcmask arg | Shift | Bits | Width | Meaning | Confidence |
|---|---|---|---|---|---|---|
| `sublane_start` | `a2` (s_start) | `<< 0` | `[2:0]` | 3 | first active sublane (0..7) | CONFIRMED |
| `lane_start` | `a4` (l_start) | `<< 3` | `[9:3]` | 7 | first active lane (0..127) | CONFIRMED |
| `sublane_end` | `a3` (s_end) | `<< 10` | `[12:10]` | 3 | last active sublane (inclusive) | CONFIRMED |
| `lane_end` | `a5` (l_end) | `<< 13` | `[19:13]` | 7 | last active lane (inclusive) | CONFIRMED |

The 3-bit sublane fields (start at bit 0, end at bit 10, each 3 bits wide because `off1 − off0 = 3` and `off3 − off2 = 3`) pin **SublaneCount = 8**; the `LloModule::VectorMaskConstantPacked(uint8)` literal-mask builder (`0x1d506a80`) — an 8-bit packed sublane mask — confirms 8 sublanes independently. The 7-bit lane fields (`off2 − off1 = 7`, end at bit 13) bound **LaneCount ≤ 128**. The geometry leaves are `Target::SublaneCount() = QWORD[[Target+0x3b8]+0x1a0]` (`0x1d60f300`) and `Target::LaneCount() = QWORD[[Target+0x3b8]+0x198]` (`0x1d60f400`); the exact per-gen LaneCount lives in the runtime config blob, not the code path (LOW — bounded `≤ 128`, exact value not in the code).

Worked example — a mask covering sublanes 0..3 and lanes 16..63 (all bounds inclusive, the values handed to `Vcmask`):

```c
// s_start=0, s_end=3, l_start=16, l_end=63
WORD = (0  << 0)        //  0x00000000   sublane_start
     | (16 << 3)        //  0x00000080   lane_start
     | (3  << 10)       //  0x00000c00   sublane_end (inclusive)
     | (63 << 13);      //  0x0007e000   lane_end    (inclusive)
//   = 0x0007ec80
```

A reimplementer encoding from a half-open `CreateLaneVmask(16, 64)` would arrive at the same `lane_end = 63` because the native path subtracts 1 (`l_hi − 1`). The complement (e.g. lanes outside `[16,63]`) is *not* a `start > end` packed word at the `vcreate_mask` boundary on the native gens — it is built by negating this mask via `VectorMaskNegate` (op `0x198`).

---

## Per-Generation Field-Offset Sweep

The native packed-word path is gated per generation by `Target::HasVcmaskInstruction()` (vtable `+0x410`). Only two subclasses return true; the rest synthesize the rectangle and never reach `GetVcmaskFieldOffsets()`. All five concrete `HasVcmaskInstruction` bodies and both real `GetVcmaskFieldOffsets` bodies were read from the decompile.

```c
JellyfishTarget ::HasVcmaskInstruction()  @ 0x1d4904c0  ->  return 0;   // synthesized
PufferfishTarget::HasVcmaskInstruction()  @ 0x1d494b60  ->  return 0;   // synthesized
GhostliteTarget ::HasVcmaskInstruction()  @ 0x1d497d20  ->  return 1;   // NATIVE
ViperfishTarget ::HasVcmaskInstruction()  @ 0x1d49ae60  ->  return 1;   // NATIVE
Target          ::HasVcmaskInstruction()  @ 0x1d61dcc0  ->  LogFatal    // abstract base
```

Both native `GetVcmaskFieldOffsets` bodies first re-assert `HasVcmaskInstruction()` (vtable `+0x410`, the decompiler shows `vtable[1040]`) and then return the identical constant pair:

```c
// GhostliteTarget::GetVcmaskFieldOffsets() @ 0x1d497d60  (Viperfish @ 0x1d49aea0 byte-identical)
if (!HasVcmaskInstruction()) LogFatal("HasVcmaskInstruction()");  // target_ghostlite.h:646
return 0x300000000LL;          // rax = {off0=0, off1=3} ; rdx = 0xd0000000a = {off2=10, off3=13}

// Target::GetVcmaskFieldOffsets() @ 0x1d490500  (abstract base — never reached)
LogFatal("Unsupported instruction: vcmask");                      // target.h:2667  (__noreturn)
```

The abstract base body is `__noreturn` and emits the verbatim string `"Unsupported instruction: vcmask"` (present in `.rodata`), so a synthesized gen that ever dispatched to it would abort — which it cannot, because the synthesized path is gated behind `HasVcmaskInstruction() == 0`.

| Target subclass (gen) | `HasVcmaskInstruction` | `GetVcmaskFieldOffsets` | Offsets | Confidence |
|---|---|---|---|---|
| `Target` (abstract base) | `0x1d61dcc0` LogFatal | `0x1d490500` LogFatal (`__noreturn`) | — | CONFIRMED |
| `JellyfishTarget` (v2 / JXC) | `0x1d4904c0` → `0` | base (unreached) | synthesized | CONFIRMED |
| `DragonfishTarget` (v3) | inherits Jellyfish → `0` | base (unreached) | synthesized | CONFIRMED |
| `PufferfishTarget` (v4 / PXC) | `0x1d494b60` → `0` | base (unreached) | synthesized | CONFIRMED |
| `ViperfishTarget` (v5p / VXC) | `0x1d49ae60` → `1` | `0x1d49aea0` → `{0,3,10,13}` | `{0,3,10,13}` | CONFIRMED |
| `GhostliteTarget` (v6e/v7x / GXC) | `0x1d497d20` → `1` | `0x1d497d60` → `{0,3,10,13}` | `{0,3,10,13}` | CONFIRMED |

> **NOTE — the packed-word layout is gen-stable; it does not drift with lane count.** Both native gens return the *byte-identical* offset pair `{0,3,10,13}`, even though v6e/v7x have a wider compute fabric than v5p. The wider-lane gens do not widen or shift the packed fields — the 3-bit sublane / 7-bit lane layout is a single fixed SC predicate-register wire format across v5p and v6e/v7x. The Dragonfish (v3) target inherits `JellyfishTarget::HasVcmaskInstruction` via its vtable `+0x410` slot. There is no separate Ghostfish/Trillium/`gfc` `Target` subclass — the v7x `gfc` backend shares `GhostliteTarget`. The two SparseCore *cost-model* classes (`GhostLiteSparseCoreTarget` / `ViperfishSparseCoreTarget`) use a different vtable layout — their `+0x410`/`+0x420` slots map to `MxuNoncontractingSize`/`ShouldReverseTileForLatching`, not the Vcmask ABI — and do not participate in the `vcreate_mask` path.

> **DECODER CAVEAT (LOW).** The HW `vcreate_mask` *decoder* arm (the inverse field-extract in `SparseCoreTecVectorAlu*VectorCreateMask`) was not separately disassembled. The end-inclusive convention is CONFIRMED from the encode side (every `Vcmask` call site receives inclusive ends; the three builder APIs are mutually consistent — see below), but the inverse decode reading the end fields as inclusive (rather than one-past) is overwhelmingly implied, not byte-proven.

---

## End-Inclusivity and the Builder-API Boundary

The packed `s_end`/`l_end` fields hold the **last active index** (inclusive), not a one-past-the-end bound. This is provable two independent ways from the three public builder APIs, all of which funnel into `Vcmask` with inclusive ends.

`LloRegionBuilder::CreateLaneVmask(l_lo, l_hi)` (`0x1d53f740`) takes a **half-open** `[l_lo, l_hi)` range — its bound checks are `start >= 0`, `end <= target().LaneCount()` (so `l_hi` may equal the count), and `start <= end` — and on the native path **subtracts 1** to convert to the inclusive end:

```c
// CreateLaneVmask(l_lo, l_hi) @ 0x1d53f740 (decompiled, exact)
LloCheck(l_lo >= 0,                       "start >= 0");                 // Op2 = `>=`
LloCheck(l_hi <= target().LaneCount(),    "end <= target().LaneCount()");// Op3 = `<=`  (half-open)
LloCheck(l_lo <= l_hi,                    "start <= end");               // Op3 = `<=`
if (l_lo == 0 && l_hi == LaneCount())  return VectorMaskConstant(true);  // full
if (l_lo == l_hi)                      return VectorMaskConstant(false); // empty
if (HasVcmaskInstruction())                                              // vtable +0x410
    return Vcmask(0, SublaneCount() - 1, l_lo, l_hi - 1);  // NATIVE: l_hi-1 = inclusive lane END
else
    return CreateVmaskHelper(Vxlaneid(), l_lo, l_hi, 0, LaneCount());    // synthesized iota-cmp
```

The `Vcmask(0, SublaneCount()−1, l_lo, l_hi−1)` call passes `SublaneCount()−1` (all sublanes, inclusive) and `l_hi−1` (the last active lane, inclusive). `CreateSublaneVmask(s_lo, s_hi)` (`0x1d53d7c0`) is the symmetric case, calling `Vcmask(s_lo, s_hi−1, 0, LaneCount()−1)`.

`LloRegionBuilder::CreateVmask(s_start, s_end, l_start, l_end)` (`0x1d53fc40`) instead takes **inclusive** bounds. Its eight bound checks are all strict `<` against the count (`start_sublane < SublaneCount()`, `end_sublane < SublaneCount()`, `start_lane < LaneCount()`, `end_lane < LaneCount()`, each paired with a `>= 0`), so every bound — including the *end* — must be a valid index `∈ [0, Count−1]`. On the native path it passes the four args **raw** to `Vcmask`; when delegating to the half-open builders it **adds 1**:

```c
// CreateVmask(s_start, s_end, l_start, l_end) @ 0x1d53fc40 (decompiled, exact)
LloCheck(s_start >= 0,                "start_sublane >= 0");                       // Op2
LloCheck(s_start < SublaneCount(),    "start_sublane < target().SublaneCount()");  // Op1 = `<`  ⇒ inclusive arg
LloCheck(s_end   >= 0,                "end_sublane >= 0");                          // Op2
LloCheck(s_end   < SublaneCount(),    "end_sublane < target().SublaneCount()");     // Op1
LloCheck(l_start >= 0,                "start_lane >= 0");                           // Op2
LloCheck(l_start < LaneCount(),       "start_lane < target().LaneCount()");         // Op1
LloCheck(l_end   >= 0,                "end_lane >= 0");                             // Op2
LloCheck(l_end   < LaneCount(),       "end_lane < target().LaneCount()");           // Op1
if (both ranges full)              return VectorMaskConstant(true);
if (HasVcmaskInstruction())        return Vcmask(s_start, s_end, l_start, l_end);   // NATIVE: raw, inclusive
// delegate path adds 1 to convert inclusive -> half-open:
//   CreateSublaneVmask(s_start, s_end + 1)   AND   CreateLaneVmask(l_start, l_end + 1)
```

The `LloCheckForFailure` op semantics are byte-exact in the decompile: template parameter `(LloCheckOp)2` is `>=` (`cmp [rdi],rax; jl fail`), `(LloCheckOp)1` is strict `<` (`cmp rax,[rsi]; jge fail`), `(LloCheckOp)3` is `<=` (`cmp rax,[rsi]; jg fail`). The check strings (`"start_sublane < target().SublaneCount()"`, `"end <= target().LaneCount()"`) are present in `.rodata`, sourced from `platforms/xla/service/jellyfish/llo_region_builder.cc`.

| Builder API | Range convention | End handling at the `Vcmask` boundary | Confidence |
|---|---|---|---|
| `Vcmask` @ `0x1d53f9c0` | INCLUSIVE `[lo, hi]` | primitive — packs ends raw | CONFIRMED |
| `CreateVmask` @ `0x1d53fc40` | INCLUSIVE `[lo, hi]` | native: pass raw; delegate: `+1` (inclusive → half-open) | CONFIRMED |
| `CreateLaneVmask` @ `0x1d53f740` | HALF-OPEN `[lo, hi)` | native: `Vcmask(0, SublaneCount−1, l_lo, l_hi−1)` | CONFIRMED |
| `CreateSublaneVmask` @ `0x1d53d7c0` | HALF-OPEN `[lo, hi)` | native: `Vcmask(s_lo, s_hi−1, 0, LaneCount−1)` | CONFIRMED |

> **GOTCHA — the public APIs disagree on convention by design; the wire format does not.** `CreateVmask` is inclusive, `CreateLaneVmask`/`CreateSublaneVmask` are half-open — but all three converge on `Vcmask` receiving inclusive ends, so the *packed M-register word always stores an inclusive rectangle*. A `start > end` (inclusive) describes the complement / wrap-around, materialized via `SimplifyPredicateNegate` (`CreateVectorMaskUnop` op `0x198` = `VectorMaskNegate`, the decompile shows the literal `408`) on the synthesized path.

---

## Synthesized Path (Jellyfish / Dragonfish / Pufferfish)

The three non-native gens build the identical logical rectangle from iota comparisons instead of one packed immediate. `CreateLaneVmask` falls through to `CreateVmaskHelper(Vxlaneid(), l_lo, l_hi, 0, LaneCount())`; `CreateSublaneVmask` uses `Vslaneid()` (single sublane → `Vcmp`-EQ a constant; multi-sublane → `VectorLaneSequence` scaled by LaneCount). `CreateVmaskHelper` (`0x1d53f380`) builds `iota ∈ [lo, hi)` as one or two `Vcmp`s (`VcmpHelper` dir 5 then dir 2) AND'd via `CreateVectorMaskBinop` op `0x195` (`VectorMaskAnd`). `CreateVmask` then ANDs the sublane sub-mask and the lane sub-mask (each optionally `VectorMaskNegate`'d for the complement), again via op `0x195`. The result is the same 2D rectangle, materialized as an LLO predicate-instruction chain rather than one `vcreate_mask` immediate. See [SparseCore M-Register Predicate Word](../sparsecore/m-register-predicate.md) for the synthesized iota-compare path in detail.

---

## How a Masked Op Consumes an M-Register: the Scan Family

A masked SC vector op does not select-after-the-fact: the M-mask is an **operand of the op itself**. The MLIR `sc_tpu.scan` op carries the mask as operand[0] and the data as operand[1]. `mlir::sparse_core::ScanOp::create` (`0x145f93e0`) takes `(builder, loc, Type, MASK Value, DATA Value, reduction StringAttr)` and builds an op named `"sc_tpu.scan"`; `ScanOp::build` (`0x145f92e0`) adds the operands in that order:

```c
// ScanOp::build(builder, state, Type, mask, data, reduction) @ 0x145f92e0 (decompiled)
if (mask)  OperationState::addOperands(state, &mask, 1);   // operand[0] = mask  (OPTIONAL)
           OperationState::addOperands(state, &data, 1);   // operand[1] = data
getOrAddProperties<ScanOpGenericAdaptorBase::Properties>().reduction = reduction;  // sum/min/max
```

The mask is added **only if non-null** (`if (mask)`), which is exactly the hook for the unmasked path (see the i1 case below). When present, the lowering passes it straight through to the typed intrinsic — `ScanOpLowering::matchAndRewrite` re-emits `tpu_{add,min,max}_scan1xN{f,i}` (each a 2-operand op, `NOperands<2>` = `{mask, data}`), and `SegmentedScanOpLowering` (XOR-dispatching the reduction string `sum`/`max`/`min`) re-emits `tpu_{add,min,max}_seg_scan1xN{f,i}` with the segment-boundary reset intrinsic to the op. The intrinsic mnemonics `tpu_add_scan1xNf`, `tpu_max_scan1xNf`, `tpu_min_scan1xNi`, `tpu_add_seg_scan1xNf`, `tpu_dupcntf`, `tpu_unique`, `tpu_mprefix`, and the strings `sc_tpu.scan` / `masked_scan` are all present in `.rodata`.

> **GOTCHA — there is no post-scan `VectorSelect`.** The M-mask is the scan op's own per-lane participation predicate, carried as `sc_tpu.scan` operand[0] and threaded unchanged into the intrinsic. A reimplementation that lowers a masked scan as `select(mask, scan_result, else)` over an unmasked scan produces a different op graph and a different inactive-lane disposition. The mask gates lanes *inside* the hardware scan op.

The `ScanOp` verifier (whose assert strings are all present verbatim in `.rodata`) pins the op contract:

| Verify assertion (`.rodata`) | Meaning |
|---|---|
| `"Scan is supported only on the SC vector subcore"` | SC VPU only |
| `"Input must be a rank 1 or 2 vector."` | rank-1/2 (matches the 1D/2D lane×sublane geometry) |
| `"Mask must be a rank 1 vector."` | the mask is a rank-1 `i1` vector |
| `"Expected mask shape to match result shape:"` | mask shape == result shape (one mask bit per result lane) |
| `"Output element type must be i32 vector for i1 vector inputs."` | i1→i32 count form |
| `"Only sum reduction is supported for i1 vector inputs."` | i1 path is sum-only (count-active) |
| `"Mask is not supported for i1 vector inputs."` | i1 sum-count is **unmasked** (all lanes counted) |

### Inactive-Lane Semantics

The inactive-lane model follows directly from the mask being the op's intrinsic predicate. For the plain and segmented scans (`AddScan`/`MinScan`/`MaxScan`/`IndexScan`, 1xN and seg variants):

- **Inactive *input* lanes contribute the reduction identity** (`add → +0`, `min → +INF`, `max → −INF`), so they do not perturb the running prefix. This is the standard masked-scan reading, confirmed by the i1-no-mask classification and the masked-scan op contract.
- **There is no per-family compiler-chosen else operand.** The masked-off *output* lane carries whatever the HW scan datapath drives for an inactive lane (carry vs identity vs undriven) — a hardware micro-datapath detail one layer below the binary (LOW / INFERRED).

| Scan family | Op / intrinsic (operands) | Mask handling | Confidence |
|---|---|---|---|
| `AddScan`/`MinScan`/`MaxScan` (1xN typed) | `ScanOp(mask, data, reduction)` → `tpu_{add,min,max}_scan1xN{f,i}` `NOperands<2>` | intrinsic operand[0]; no select | CONFIRMED |
| `IndexScan` (Min/Max, 1xN) | same `ScanOp` path (dtype via `VpackFormat`) | intrinsic operand[0] | CONFIRMED |
| `SegmentedAddScan`/`Min`/`Max` | `SegmentedScanOp` → `tpu_{add,min,max}_seg_scan1xN{f,i}` `NOperands<2>` | intrinsic mask + intrinsic segment reset | CONFIRMED |
| `DuplicateCount` / `Uniquify` | `tpu_dupcnt{f,s}` / `tpu_unique` + `ReplaceOpWithExtracts` | no select, no sentinel; result produced directly | CONFIRMED |
| i1→i32 sum (count-active) | `ScanOp` i1 input, sum-only | **mask not supported** — all lanes counted | CONFIRMED |
| row-size consumer (`lower_scan`) | `ScanOp("sum")` + `tpu_mprefix` (1 operand) | `scan_mask = BroadcastBoolToVector(..., true)` = all-active | CONFIRMED |
| masked-off *output* lane value | — | HW datapath (carry/identity/undriven) | LOW (one layer below binary) |

`DuplicateCount` / `Uniquify` emit dedicated `tpu_dupcnt{f,s}` / `tpu_unique` ops plus `ReplaceOpWithExtracts` (struct-unpack only) — no select, no sentinel; the uniquify mask result is written to a `VMDest` (M0..M15) directly. The only explicit "else"-type wiring lives in the row-size consumer (`lower_scan` / `max_ell_row_size_scan` over `chunk_size_`), which builds an **all-active** `scan_mask = lowering_util::BroadcastBoolToVector(b, loc, chunk_size_, true)` and uses `tpu_mprefix` (one operand) for the masked-prefix positions. The i1→i32 sum-scan (count-active-lanes, the `DuplicateCount` prefix form) explicitly **disallows** a mask.

> **CORRECTION (MASK-1).** An earlier reading modeled the masked-scan inactive *output* lane as a compiler-inserted post-scan `VectorSelect(mask, scan_result, else)` whose else operand (zero/identity vs preserve-old) was chosen per call. The resolved `ScanOp::build` decompile shows the mask is the scan op's own operand[0] — there is *no* post-scan select for the plain/segmented/index scans. Inactive input lanes contribute the reduction identity; the masked-off output value is a HW datapath detail, not a per-call compiler-chosen else.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LloRegionBuilder::Vcmask(s_start,s_end,l_start,l_end)` | `0x1d53f9c0` | native packed-word builder (`llo.vcmask`) | CONFIRMED |
| `LloRegionBuilder::CreateVmask(s_start,s_end,l_start,l_end)` | `0x1d53fc40` | inclusive-bounds 2D mask builder (8 checks) | CONFIRMED |
| `LloRegionBuilder::CreateLaneVmask(l_lo,l_hi)` | `0x1d53f740` | half-open lane-range mask | CONFIRMED |
| `LloRegionBuilder::CreateSublaneVmask(s_lo,s_hi)` | `0x1d53d7c0` | half-open sublane-range mask | CONFIRMED |
| `LloRegionBuilder::CreateVmaskHelper` | `0x1d53f380` | synthesized iota-compare construction | CONFIRMED |
| `Target::GetVcmaskFieldOffsets()` (base) | `0x1d490500` | `__noreturn` LogFatal `"Unsupported instruction: vcmask"` | CONFIRMED |
| `GhostliteTarget::GetVcmaskFieldOffsets()` | `0x1d497d60` | returns `{0,3,10,13}` | CONFIRMED |
| `ViperfishTarget::GetVcmaskFieldOffsets()` | `0x1d49aea0` | returns `{0,3,10,13}` (byte-identical) | CONFIRMED |
| `Target::HasVcmaskInstruction()` (base) | `0x1d61dcc0` | LogFatal (abstract) | CONFIRMED |
| `JellyfishTarget::HasVcmaskInstruction()` | `0x1d4904c0` | `return 0` (synthesized) | CONFIRMED |
| `PufferfishTarget::HasVcmaskInstruction()` | `0x1d494b60` | `return 0` (synthesized) | CONFIRMED |
| `GhostliteTarget::HasVcmaskInstruction()` | `0x1d497d20` | `return 1` (native) | CONFIRMED |
| `ViperfishTarget::HasVcmaskInstruction()` | `0x1d49ae60` | `return 1` (native) | CONFIRMED |
| `mlir::llo::VectorCreateMaskOp::create` | `0x13fb3ba0` | MLIR `llo.vcmask` op create | CONFIRMED |
| `mlir::sparse_core::ScanOp::create` | `0x145f93e0` | `sc_tpu.scan` create (mask, data, reduction) | CONFIRMED |
| `mlir::sparse_core::ScanOp::build` | `0x145f92e0` | adds operand[0]=mask (optional), operand[1]=data | CONFIRMED |
| `ScanOpLowering<ScanOp,ScanOp>::matchAndRewrite` | `0x135f2580` | dtype Pack/Unpack splitter; mask passed through | CONFIRMED |
| `GetVectorMask<SparsecoreVectorMask>` | `0x13a33320` | 5-bit M-selector decode (`regid − 0x5f`) | CONFIRMED |
| `GetVMDestregno` | `0x13a65b20` | M0..M15 mask-write band `[0x5f,0x6e]` | CONFIRMED |

---

## What Is Not Decoded

- The HW `vcreate_mask` *decoder* field-extract arm (`SparseCoreTecVectorAlu*VectorCreateMask`). The end-inclusive convention is CONFIRMED from the encode side; the inverse decode reading the end fields as inclusive (vs one-past) and whether the 7-bit lane field is truncated at LaneCount or full 128 is not byte-proven (LOW).
- The masked-scan per-lane *write-enable* micro-datapath: whether the VPU physically suppresses the output write on a masked-off lane or always materializes a value (carry/identity/undriven). The mask is the op's intrinsic predicate (CONFIRMED) and inactive input lanes contribute the identity (CONFIRMED), but the masked-off output lane value is a hardware detail one layer below the binary (INFERRED).
- The exact per-gen runtime `LaneCount` value (from `[[Target+0x3b8]+0x198]`). SublaneCount = 8 is CONFIRMED (3-bit packed fields + 8-bit `VectorMaskConstantPacked`); LaneCount ≤ 128 is bounded by the 7-bit field; the exact value lives in the runtime config blob (LOW).
- Whether any future SC gen beyond Viperfish/Ghostlite forks a distinct native-vmask `Target` (the v7x `gfc` shares `GhostliteTarget` in v0.0.40; none exists today).

---

## Cross-References

- [VPU Slot](slot-vpu.md) — the `Vmsk` vector-mask register file from the consumer side, the `VectorSelectVmsk` ops, and the VEX `bit0x104` 5-bit M-selector field.
- [Predicate Register File](slot-predicate.md) — the *scalar* predicate file (`Preg`) that gates whole slots/branches, orthogonal to the per-lane M-mask documented here; the two-file split is the central predication fact.
- [SparseCore M-Register Predicate Word](../sparsecore/m-register-predicate.md) — the synthesized iota-compare path and the masked-scan output model in SparseCore-ISA context.
- [Bundle Model](bundle-model-overview.md) — the VLIW bundle the masked vector op and its M-selector field pack into, and the `kNeverExecute` empty-slot convention.
