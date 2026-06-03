# M-Register Predicate (M0–M31)

> *Every register band, bit offset, shift constant, field-offset immediate, opcode number, struct offset, and assert string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`) — from the `GetVectorMask`/`GetVMDestregno` band guards, the `LloRegionBuilder::Vcmask` shift-pack core, the per-target `GetVcmaskFieldOffsets`/`HasVcmaskInstruction` vtable bodies, the `CreateVmask`/`CreateLaneVmask`/`CreateSublaneVmask`/`CreateVmaskHelper` builders, and the `mlir::tpu::ScanOp::verify` contract. Addresses are VMA; in `.text`/`.rodata`, VMA == file offset. Other versions differ.*

## Abstract

The SparseCore vector predicate is a file of architectural **mask registers** `M0..M31`. A masked [VEX](vectorextended-vex.md) scan/sort/dedup op names one of them with a 5-bit selector field (`bit0x104`, decoded by [VEX Mask/Dest-Port/Sub-Opcode](vex-mask-destport-subopcode.md)); the named M-register supplies the per-`(lane, sublane)` execution predicate the [scan datapath](scan-datapath.md) consumes. This page owns what the encode side hands off: the M-register *file* (its 32-deep read band vs 16-deep write subset), the *predicate word* the register holds, and the *masked-scan inactive-lane output model* — what a masked-off lane reads after the scan.

The decisive structural finding is that **the M-register predicate is range-based, not bitmask-stored**. An M-register always represents a 2D rectangle `{sublane ∈ [s_lo, s_hi)} ∧ {lane ∈ [l_lo, l_hi)}`. The hardware has two physical realizations of that rectangle, chosen by a target vtable predicate `Target::HasVcmaskInstruction()` (vtable slot `+0x410`):

- **NATIVE** (`GhostliteTarget` = v6e SC, `ViperfishTarget` SC; `HasVcmaskInstruction()` = `1`): the four rectangle bounds are packed into a single 32-bit `vcmask` scalar immediate at fixed bit-offsets `{0, 3, 10, 13}` returned by `Target::GetVcmaskFieldOffsets()` (vtable slot `+0x420`). A 3-bit sublane field (⇒ 8 sublanes) and a 7-bit lane field (⇒ up to 128 lanes).
- **SYNTHESIZED** (`JellyfishTarget`, `PufferfishTarget`; `HasVcmaskInstruction()` = `0`): the rectangle is computed from iota comparisons — compare the lane-index iota (`Vxlaneid`) and sublane-index iota (`Vslaneid`) against the bounds, then `AND` the two 1-D predicates (`VectorMaskAnd` opcode `0x195`, with `VectorMaskNegate` opcode `0x198` for a complement half).

The masked-scan **inactive-lane behavior is not a VEX bundle micro-field.** Inactive *input* lanes contribute the reduction identity (in silicon, below the binary); the masked-off *output* lanes are disposed by a *separate, post-scan* `VectorSelect(mask, scan_result, else)` in the MLIR lowering, where the `else` operand decides zero/identity vs preserve-old.

For reimplementation, the contract is:

- **An M-register is a 2D `{sublane-range, lane-range}` rectangle, not a per-lane bitmask.** Build it with the four bounds `(s_lo, s_hi, l_lo, l_hi)`, each half-open `[lo, hi)` at the builder API; the native packer converts the high bounds to inclusive (`hi − 1`) before packing.
- **The native packed word is `WORD = (s_start<<0) | (l_start<<3) | (s_end<<10) | (l_end<<13)`** — sublane fields 3 bits wide (pinning `SublaneCount = 8`), lane fields 7 bits wide (`LaneCount ≤ 128`). Offsets `{0,3,10,13}` come from `GetVcmaskFieldOffsets()`; both native gens return the identical pair.
- **The read file is 32-deep (`M0..M31`), the op-write subset is 16-deep (`M0..M15`).** A scan input mask may live anywhere in `M0..M31`; an op that *produces* a mask result (index-scan / uniquify) and a post-scan `VectorSelect` may only target `M0..M15`.
- **Masked-off output lanes are select-driven, not bundle-driven.** Inactive input lanes feed the reduction identity (`add→0`, `min→+INF`, `max→−INF`); the output disposition is the `else` operand of a downstream `VectorSelect`.

| | |
|---|---|
| **Register file** | `M0..M31` — SparseCore VPU mask/predicate registers |
| **Read band** | `[0x5f, 0x7e]` = `M0..M31` (32-deep); `GetVectorMask` `@0x13a33320`, value `regno − 0x5f` |
| **Write subset** | `[0x5f, 0x6e]` = `M0..M15` (16-deep); `GetVMDestregno` `@0x13a65b20`, value `regno − 0x5f` |
| **Geometry** | `SublaneCount = 8` (3-bit packed field + `VectorMaskConstantPacked(uint8)`); `LaneCount ≤ 128` (7-bit field) |
| **Native packer** | `LloRegionBuilder::Vcmask(s_start, s_end_incl, l_start, l_end_incl)` `@0x1d53f9c0` |
| **Native gate** | `Target::HasVcmaskInstruction()` vtable `+0x410` — Ghostlite `@0x1d497d20` = `1`, Viperfish `@0x1d49ae60` = `1`; Jellyfish `@0x1d4904c0` = `0`, Pufferfish `@0x1d494b60` = `0`; abstract base `@0x1d61dcc0` is a `LogFatal` "Unimplemented" stub |
| **Field offsets** | `Target::GetVcmaskFieldOffsets()` vtable `+0x420` → `{0, 3, 10, 13}` |
| **Native word** | `(s_start<<0) \| (l_start<<3) \| (s_end<<10) \| (l_end<<13)` |
| **Synth ops** | `VectorMaskAnd` `0x195`, `VectorMaskNegate` `0x198`, `VcmpHelper` `@0x1d55ce40` |
| **Output model** | post-scan `VectorSelect(mask, scan, else)`; inactive input = reduction identity |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the M-register *file*, the *predicate word*, and the *inactive-lane output model*.** The 5-bit selector field that *names* the M-register (`bit0x104`), the dest read-port (`bit0x10c`), and the `bit0x109` second-source port live in [VEX Mask/Dest-Port/Sub-Opcode](vex-mask-destport-subopcode.md). How the scan op *consumes* the selected mask (the lowering, the two-part predication datapath) lives in [Scan Datapath](scan-datapath.md). They are linked, not repeated.

---

## 1. The M-Register File — read band, write subset

### 1.1 Two band guards, two widths

The mask register file is asymmetric: a scan or sort op may *read* a predicate from any of `M0..M31`, but an op that *produces* a mask result (and the post-scan `VectorSelect`) may only *write* the lower half `M0..M15`. The two band guards prove it byte-exactly. The read getter `GetVectorMask<SparsecoreVectorMask>` `@0x13a33320`:

```c
// xla::tpu::sparse_core::isa_emitter::GetVectorMask<...SparsecoreVectorMask>  @0x13a33320
__int64 GetVectorMask(__int64 a1) {              // a1 = &MCOperand
  if ( *(_BYTE *)a1 != 1 )       { /* "operand.isReg()"          */ LogFatal(...); }
  unsigned int v1 = *(_DWORD *)(a1 + 8);         // the register id
  if ( v1 <= 0x5E )              { /* "regno >= llvm::TPU::M0"   */ LogFatal(...); }
  if ( v1 >= 0x7F )              { /* "regno <= llvm::TPU::M31"  */ LogFatal(...); }
  return v1 - 95;                                // 95 == 0x5f ⇒ index ∈ [0, 0x1f]
}
```

The write getter `GetVMDestregno` `@0x13a65b20` is structurally identical but its upper guard is `>= 0x6F`, not `>= 0x7F`:

```c
// xla::tpu::sparse_core::isa_emitter::GetVMDestregno  @0x13a65b20
__int64 GetVMDestregno(__int64 this) {
  if ( *(_BYTE *)this != 1 )     { /* "operand.isReg()"          */ LogFatal(...); }
  unsigned int v2 = *(_DWORD *)(this + 8);
  if ( v2 <= 0x5E )              { /* "regno >= llvm::TPU::M0"   */ LogFatal(...); }
  if ( v2 >= 0x6F )              { /* "regno <= llvm::TPU::M15"  */ LogFatal(...); }
  return v2 - 95;                                // band [0x5f,0x6e] = M0..M15
}
```

Both subtract `95` (`0x5f`) to dense-index the file from zero. The read band `[0x5f, 0x7e]` spans exactly 32 ids; the write band `[0x5f, 0x6e]` spans exactly 16. The architectural id of mask register `Mk` is therefore `k + 0x5f`, and the 5-bit `bit0x104` selector encodes `k = id − 0x5f`.

> **NOTE — read-32 / write-16 is the file partition model.** A reimplementer allocating mask registers must obey both ceilings: a scan *input* predicate can sit in `M16..M31`, but a `VectorSelect` mask and an op-produced mask result must sit in `M0..M15`. Whether `M16..M31` have *any* op-produced write path beyond the compiler materializing them with `Vcmask`/`CreateVmask` (i.e. whether the upper half is read-only predicate input) is **INFERRED** from the band split alone — no write-path-absence search was run against the upper half.

### 1.2 The VPU geometry that bounds the file's content

A mask register predicate spans the SparseCore VPU lane/sublane grid. The two dimension getters read the per-target config blob:

| Getter | Computation | Source | Confidence |
|---|---|---|---|
| `SublaneCount()` `@0x1d60f300` | `QWORD[[Target+0x3b8]+0x1a0]` | per-target config | CONFIRMED |
| `LaneCount()` `@0x1d60f400` | `QWORD[[Target+0x3b8]+0x198]` | per-target config | CONFIRMED |
| `AllSublanesMask()` `@0x1d61c3e0` | `(1 << SublaneCount) − 1` (`0xffffffff >> (32 − count)`, `cmov→0` when count == 0) | derived | CONFIRMED |
| `ChunkLanesMask()` `@0x1d61c3a0` | `0xffffffff >> (clz(LaneCount) + 1)` (`_BitScanReverse` on `LaneCount`; for power-of-two `LaneCount` ⇒ `LaneCount − 1`) | derived | CONFIRMED |
| `Vxlaneid()` `@0x1d51d540` | `VectorLaneSequence AND ChunkLanesMask` | iota | CONFIRMED |

`SublaneCount = 8` is pinned two independent ways: the native packed word reserves a **3-bit** field for each sublane bound (`0..7`), and a literal mask can be set with `LloModule::VectorMaskConstantPacked(uint8)` `@0x1d506a80` — an **8-bit** packed sublane mask. `LaneCount` is read from the runtime config and is only bounded (`≤ 128`) by the 7-bit lane field; the exact per-gen value lives in the config blob, not the code path **[INFERRED for the exact value]**.

---

## 2. The Predicate Word — native packed format (Ghostlite / Viperfish)

### 2.1 The packer wires four bounds at four offsets

On a native gen, `LloRegionBuilder::Vcmask(s_start, s_end_incl, l_start, l_end_incl)` `@0x1d53f9c0` builds the M-register by OR-ing the four bounds at offsets supplied by the target vtable, materializing one 32-bit `ScalarU32Constant`, then issuing `CreateVectorCreateMask`. The packing core is a single expression:

```c
// LloRegionBuilder::Vcmask(this, s_start a2, s_end_incl a3, l_start a4, l_end_incl a5)  @0x1d53f9c0
v11 = (*(...vtable+0x420...))(target);            // GetVcmaskFieldOffsets() → rax:v11, rdx:v12
// v11 = 0x300000000  ⇒ low32 = 0 (s_start shift), byte4 = 3 (l_start shift)
// v12 = 0xd0000000a  ⇒ low32 = 0x0a = 10 (s_end shift), byte4 = 0x0d = 13 (l_end shift)
word = ((_QWORD)a4 << SBYTE4(v11))   // l_start << 3
     | ((_QWORD)a2 << v11)           // s_start << 0
     | ((_QWORD)a3 << v12)           // s_end   << 10
     | ((_QWORD)a5 << SBYTE4(v12));  // l_end   << 13
v13 = LloModule::ScalarU32ConstantImpl(word, ...);            // @0x1d506020
mask = LloInstruction::CreateVectorCreateMask(v13, ...);      // @0x1d4db820
```

The offsets are a constant pair returned identically by both native gens. `GhostliteTarget::GetVcmaskFieldOffsets()` `@0x1d497d60` and `ViperfishTarget::GetVcmaskFieldOffsets()` `@0x1d49aea0` both load `rax = 0x300000000`, `rdx = 0xd0000000a` (raw bytes `48 b8 00 00 00 00 03 00 00 00` / `48 ba 0a 00 00 00 0d 00 00 00`), which decode as the four int offsets `{0, 3, 10, 13}`. The abstract base `Target::GetVcmaskFieldOffsets()` `@0x1d490500` is a `LogFatal` stub.

> **NOTE — argument order vs field order.** The packing assigns shifts by *register pair half*, so the lane-start bound (`a4`, the third argument) lands at offset 3 and the sublane-end bound (`a3`, the second argument) lands at offset 10. The resulting field layout interleaves sublane and lane bounds; do not assume the four arguments pack in argument order.

### 2.2 The packed word layout

```text
                   31                13 12  10 9              3 2   0
                  ┌────────────────────┬──────┬────────────────┬─────┐
   native WORD =  │  lane_end (incl)    │ subl │   lane_start    │ subl│
                  │       7 bits        │ _end │     7 bits       │_st  │
                  │       << 13         │ 3b   │     << 3         │3b<<0│
                  └────────────────────┴──────┴────────────────┴─────┘
   WORD = (s_start << 0) | (l_start << 3) | (s_end << 10) | (l_end << 13)
```

| Field | Vcmask arg | shift | bits | width | meaning | Confidence |
|---|---|---|---|---|---|---|
| `sublane_start` | `a2` (`s_start`) | `<< 0` | `[2:0]` | 3 | first active sublane (`0..7`) | CONFIRMED |
| `lane_start` | `a4` (`l_start`) | `<< 3` | `[9:3]` | 7 | first active lane (`0..127`) | CONFIRMED |
| `sublane_end` | `a3` (`s_end_incl`) | `<< 10` | `[12:10]` | 3 | last active sublane (inclusive) | CONFIRMED |
| `lane_end` | `a5` (`l_end_incl`) | `<< 13` | `[≥13]` | 7 | last active lane (inclusive) | CONFIRMED |

The two 3-bit sublane fields at offsets `0` and `10` (with the 7-bit lane field between them) directly pin `SublaneCount = 8`. The word is a `{sublane_range, lane_range}` rectangle descriptor, **not** a per-lane bitmask — the hardware's `vcmask` decoder expands the four bounds into the active-lane set.

> **GOTCHA — half-open at the API, inclusive in the word.** The builder API takes half-open ranges `[lo, hi)`; the native call sites pass `hi − 1` for the high bounds. `CreateLaneVmask` calls `Vcmask(0, SublaneCount−1, l_lo, l_hi−1)`; `CreateSublaneVmask` calls `Vcmask(s_lo, s_hi−1, 0, LaneCount−1)`. So the emitter's convention for the packed `sublane_end`/`lane_end` is **inclusive**. Whether the HW `vcmask` *decoder* reads the end fields as inclusive vs exclusive (and whether the 7-bit lane field is truncated at `LaneCount` or full 128) was not cross-checked against a decoder arm — **INFERRED** at the HW decode side, CONFIRMED at the emit side.

### 2.3 Literal mask shortcuts

Two builders set an M-register directly without the four-bound packer:

| Builder | Argument | Produces | Confidence |
|---|---|---|---|
| `LloModule::VectorMaskConstantPacked(uint8)` `@0x1d506a80` | 8-bit packed sublane bitmask | per-sublane literal (one bit per sublane ⇒ 8 sublanes) | CONFIRMED |
| `LloModule::VectorMaskConstant(bool)` `@0x1d506940` | `true` / `false` | all-ones / all-zeros predicate | CONFIRMED |

The `uint8` width of `VectorMaskConstantPacked` is the second independent confirmation of the 8-sublane geometry.

---

## 3. The Predicate Word — synthesized format (Jellyfish / Pufferfish)

When `HasVcmaskInstruction()` returns `0` (`JellyfishTarget` `@0x1d4904c0` and `PufferfishTarget` `@0x1d494b60` both return `0`), there is no `vcmask` instruction, so the *same logical rectangle* is synthesized as a chain of LLO predicate ops: compare the lane and sublane iotas against the bounds and `AND` the two 1-D results. The entry point `CreateVmask(s_lo, s_hi, l_lo, l_hi)` `@0x1d53fc40` first validates every bound against the geometry, with byte-confirmed assert strings:

```c
// CreateVmask(this, s_lo, s_hi, l_lo, l_hi)  @0x1d53fc40 — validation, then AND of two sub-masks
//   "start_sublane < target().SublaneCount()" / "end_sublane < target().SublaneCount()"
//   "start_lane    < target().LaneCount()"    / "end_lane    < target().LaneCount()"
if ( sublane_full && lane_full )
    return LloModule::VectorMaskConstant(true);                 // both ranges full ⇒ all-ones
sublane_sub = CreateSublaneVmask(this, s_lo, s_hi);            // optionally SimplifyPredicateNegate'd
lane_sub    = CreateLaneVmask(this, l_lo, l_hi);
result      = /* VectorMaskAnd(sublane_sub, lane_sub) */ ...;  // op 0x195
```

The per-axis builders short-circuit empty/full ranges and otherwise call `CreateVmaskHelper`:

| Builder | Iota source | Predicate built | Empty/full shortcut | Confidence |
|---|---|---|---|---|
| `CreateLaneVmask(l_lo, l_hi)` `@0x1d53f740` | `Vxlaneid()` `@0x1d51d540` (`VectorLaneSequence & ChunkLanesMask`) | `l_lo ≤ xlaneid < l_hi` | `l_hi==l_lo` → `false`; `l_lo==0 && l_hi==LaneCount` → `true` | CONFIRMED |
| `CreateSublaneVmask(s_lo, s_hi)` `@0x1d53d7c0` | `Vslaneid()` `@0x1d51d380` (single); `VectorLaneSequence×LaneCount` (multi) | `s_lo ≤ slaneid < s_hi` (single → `Vslaneid == const` EQ) | `s_hi==s_lo` → `false`; full → `true` | CONFIRMED |
| `CreateVmaskHelper(iota, lo, hi, range_lo, range_hi)` `@0x1d53f380` | the passed iota | `iota ∈ [lo, hi)` | `hi==range_hi` → one-sided (`iota ≥ lo`); `lo==range_lo` → one-sided (`iota < hi`) | CONFIRMED |

The general two-sided case in `CreateVmaskHelper` issues two comparisons against `VcmpHelper` `@0x1d55ce40` — direction `5` *against `hi`* (yielding `iota < hi`) then direction `2` *against `lo`* (yielding `iota ≥ lo`) — and combines them with `SimplifyPredicateAnd` `@0x1d58e4e0`, which lowers to `CreateVectorMaskBinop` opcode `0x195` (`VectorMaskAnd`). The two one-sided shortcut arms pin the direction-code meaning: the `lo==range_lo` arm emits `VcmpHelper(iota, U32Constant(hi), 4, /*dir*/ 5)` for the upper bound, and the `hi==range_hi` arm emits `VcmpHelper(iota, U32Constant(lo), 4, /*dir*/ 2)` for the lower bound:

```c
// CreateVmaskHelper general (two-sided) arm  @0x1d53f380
v18 = VcmpHelper(this, iota, U32Constant(hi), /*op4*/ 4, /*dir*/ 5, 0);   // iota <  hi
v21 = VcmpHelper(this, iota, U32Constant(lo), /*op4*/ 4, /*dir*/ 2, 0);   // iota >= lo
result = SimplifyPredicateAnd(v18, v21);   // → CreateVectorMaskBinop op 0x195 = VectorMaskAnd
```

The complement half (when a sub-mask must be negated) goes through `SimplifyPredicateNegate` `@0x1d58eb20` → `CreateVectorMaskUnop` opcode `0x198` (`VectorMaskNegate`). The synthesized result is the **same logical M-register** — the 2D rectangle `{sublane ∈ [s_lo, s_hi)} ∧ {lane ∈ [l_lo, l_hi)}` — materialized as an LLO predicate-instruction chain instead of one packed `vcmask` immediate.

| LLO opcode | Number | Builder | Confidence |
|---|---|---|---|
| `VectorMaskAnd` | `0x195` | `CreateVectorMaskBinop` `@0x1d4d2b00` | CONFIRMED |
| `VectorMaskNegate` | `0x198` | `CreateVectorMaskUnop` `@0x1d4d2e80` | CONFIRMED |

---

## 4. The Masked-Scan Inactive-Lane Output Model

A masked scan asks two questions, and the binary answers them in two different places. The M-mask the bundle carries (`bit0x104` → an M-register selector) gates **which input lanes participate**; the disposition of **masked-off output lanes** is *not* a VEX bundle micro-field — it is realized one level up, in the MLIR `sc_tpu.scan` lowering.

```text
SparseCore masked scan — inactive-lane handling is TWO mechanisms
  ┌──────────────────────────────────────────────────────────┐
  │ VEX scan slot (e.g. AddScanS32)                            │
  │   M-mask selector (bit0x104) → names M0..M31               │  ── gates INPUT lanes (HW)
  │   inactive INPUT lanes contribute the REDUCTION IDENTITY   │     add→0  min→+INF  max→−INF
  │   output shape == input shape (full-width result)          │
  └──────────────────────────────────────────────────────────┘
                          │ scan_result (all lanes occupied)
                          ▼
  ┌──────────────────────────────────────────────────────────┐
  │ post-scan VectorSelect (SEPARATE LLO op, M0..M15 file)     │
  │   select(mask, then = scan_result, else)                   │  ── disposes INACTIVE OUTPUT lanes
  │   else = zero/identity (fresh) OR prior value (preserve)   │
  └──────────────────────────────────────────────────────────┘
```

### 4.1 The scan op carries an explicit mask vector operand

The `sc_tpu.scan` lowering builds an explicit mask operand by broadcasting a boolean across the chunk width and feeds it to the scan op (`AtLeastNOperands<1>`, `OneResult`, rank 1 or 2 — matching the 2D lane×sublane geometry):

```text
scan_mask = lowering_util::BroadcastBoolToVector(b, loc, chunk_size_, /*value=*/true);
sparse_core::ScanOp::create(..., data, scan_mask, reduction_attr);
```

The full op-name chain is `lower_scan` / `max_ell_row_size_scan` / `masked_scan` / `sc_tpu.scan` / `vector.scan`.

### 4.2 The verify contract

`mlir::tpu::ScanOp::verify` `@0x14af7460` asserts (rodata error strings):

| Error string | Constraint | Confidence |
|---|---|---|
| "Scan is supported only on the SC vector subcore" | SC vector subcore only | CONFIRMED |
| "Input must be a rank 1 or 2 vector" | rank 1 (lane vector) or 2 (packed sublanes) | CONFIRMED |
| "Input and output shape mismatch" | output shape == input shape (scan produces a **full-width** result; masked-off lanes still occupy their slot) | CONFIRMED |
| "Output element type must be i32 vector for i1 vector inputs" | `i1` input ⇒ `i32` output | CONFIRMED |
| "Only sum reduction is supported for i1 vector inputs" | `i1` input ⇒ `sum` only | CONFIRMED |

The `i1 → i32` sum-only scan is the **count-active-lanes** primitive (`DuplicateCount`): a prefix-count of set predicate bits. A boolean input forbids a separate mask and forbids non-`sum` reductions.

### 4.3 Inactive input vs masked-off output

The two halves resolve independently:

- **Inactive INPUT lanes contribute the reduction identity.** Because the scan output is full-width (verify enforces `output shape == input shape`), a masked-off input lane cannot simply vanish; it feeds the identity element so the running prefix is unperturbed — `0` for `add`, `+INF` for `min`, `−INF` for `max`. This is **CONFIRMED** by the `masked_scan` classification and the `i1→i32` sum semantics; the exact identity per family is an **INFERRED** detail.
- **Masked-off OUTPUT lanes are select-driven.** The LLO op roster exposes `VectorSelectOp` and the `vnsel` (vector negate-mask select) emitter `EmitVectorSelectNegateMask(Vregno dest, Vmregno mask, variant<VregnoOrImm, Sregno> src)`: it selects `dest`/`src` per the named mask Vmreg. The per-target bodies are `GhostliteTensorCoreEmitter::EmitVectorSelectNegateMask` `@0x1424c8a0` and `ViperfishTensorCoreEmitter::EmitVectorSelectNegateMask` `@0x141cbca0`; the abstract `IsaEmitter::EmitVectorSelectNegateMask` `@0x140c1920` is a `LogFatal` stub ("Instruction vnsel not supported on this platform."). The masked-off output lanes take the select's `else`/`src` operand — the broadcast identity/zero for a fresh result, or the prior register value for a preserve-old select. The select's mask is drawn from the narrower `M0..M15` write/select file (§1.1), distinct from the scan's `M0..M31` read file.

| Layer | What | Evidence | Confidence |
|---|---|---|---|
| MLIR `sc_tpu.scan` | explicit mask vector operand | `scan_mask = BroadcastBoolToVector(..., true)`; `ScanOp::create` | CONFIRMED |
| op shape | `AtLeastNOperands<1>`, `OneResult`, rank 1/2 | `sparse_core::ScanOp` traits | CONFIRMED |
| verify | `i1→i32` sum-only (count-active); full-width output | `ScanOp::verify` `@0x14af7460` strings | CONFIRMED |
| inactive INPUT lanes | contribute the reduction identity (no perturbation) | `masked_scan` classification; sum/min/max identity | CONFIRMED (identity detail INFERRED) |
| masked-off OUTPUT lanes | `select(mask, scan, else)` — `else` = zero/identity or preserve-old | `VectorSelectOp`; `vnsel` emitter `EmitVectorSelectNegateMask` (Ghostlite `@0x1424c8a0`, Viperfish `@0x141cbca0`; base `@0x140c1920` is a `LogFatal` stub) | CONFIRMED |
| result commit | inline (VresMove/Sort dest-port) OR out-of-line (`PopXrf`) | see [VEX Mask/Dest-Port](vex-mask-destport-subopcode.md) | CONFIRMED |

> **GOTCHA — the bundle carries only the M-mask + the scan op; the zero-vs-preserve choice is the lowering's select wiring.** A reimplementation that looks for a "masked-off output" micro-field in the VEX bundle will not find one. The hardware bundle gates the input lanes by the named M-register; the output disposition is a downstream `VectorSelect` whose `else` operand the lowering chooses per scan family.

> **INFERRED — the lowest per-lane write-enable micro-datapath.** Whether the VPU *physically suppresses* the write on masked-off output lanes, or the lowering *always materializes* the select, is one layer below the binary. The mask SELECTS the predicate (CONFIRMED), inactive INPUT lanes feed the reduction identity (CONFIRMED via the `masked_scan` classification + `i1→i32` sum semantics), and the OUTPUT is select-driven at the MLIR layer (CONFIRMED via `VectorSelectOp`); the precise zero-vs-preserve choice is per-call and the hardware write-enable was not simulator-checked.

---

## 5. Building an M-Register — the two paths side by side

```text
                    CreateVmask(s_lo, s_hi, l_lo, l_hi)            @0x1d53fc40
                      │  validate vs SublaneCount() / LaneCount()
                      │  both ranges full ──────────────► VectorMaskConstant(true)  @0x1d506940
                      ▼
        HasVcmaskInstruction()  (vtable +0x410)
            │                              │
   = 1 (NATIVE)                     = 0 (SYNTHESIZED)
   Ghostlite / Viperfish            Jellyfish / Pufferfish
            │                              │
   Vcmask(s_lo, s_hi−1,              CreateSublaneVmask(s_lo,s_hi)  AND  CreateLaneVmask(l_lo,l_hi)
          l_lo, l_hi−1)               │ Vslaneid / VectorLaneSequence    │ Vxlaneid
   @0x1d53f9c0                        ▼                                  ▼
   word = (s_start<<0)            CreateVmaskHelper: VcmpHelper dir5 vs hi (<hi) & dir2 vs lo (≥lo)
        | (l_start<<3)                              → SimplifyPredicateAnd
        | (s_end<<10)                               → VectorMaskAnd op 0x195
        | (l_end<<13)              (negate half: SimplifyPredicateNegate → VectorMaskNegate op 0x198)
            │                              │
   ScalarU32ConstantImpl              an LLO predicate-instruction chain
   → CreateVectorCreateMask
   @0x1d4db820
            └──────────────┬───────────────┘
                           ▼
              one M-register holding the 2D rectangle
              {sublane ∈ [s_lo,s_hi)} ∧ {lane ∈ [l_lo,l_hi)}
```

Both paths produce the same *logical* predicate; only the realization differs (one packed `vcmask` immediate vs an iota-compare instruction chain). A reimplementation that targets a native gen emits the packed word; one targeting Jellyfish/Pufferfish emits the compare/AND chain. `Vsmask` `@0x1d53fc00` (sublane-only mask) and the `CreateVectorCreateSublaneMask` `@0x1d4db640` arm follow the same native/synth split for the sublane-only case.

---

## 6. What is not yet pinned

- **The HW `vcmask` decoder inclusivity.** The emitter packs inclusive end bounds (`hi − 1`); whether the VPU decoder reads `sublane_end`/`lane_end` as inclusive vs exclusive, and whether the 7-bit lane field is truncated at `LaneCount` or full 128, was not cross-checked against a decoder arm. **INFERRED** at the HW decode side.
- **The per-lane write-enable micro-datapath of the masked scan** (physical suppress-write vs always-materialized select). The `masked_scan` classification and `VectorSelect` lowering are CONFIRMED; the lowest hardware write gate is below the binary. **INFERRED.**
- **The exact per-gen `LaneCount`.** Read from the runtime config blob `[Target+0x3b8]+0x198`. `SublaneCount = 8` is CONFIRMED by the 3-bit packed field + `VectorMaskConstantPacked(uint8)`; `LaneCount ≤ 128` by the 7-bit field, but the exact per-gen value lives in config, not code. **INFERRED for the value.**
- **Whether `M16..M31` have any op-produced write path.** The read band is 32-deep, the op-write band (`GetVMDestregno`) is 16-deep; the upper half being read-only compiler-materialized predicate inputs is **INFERRED** from the band split, not from a write-path-absence proof. The native writers (`Vcmask`/`CreateVmask`) target the full logical `M0..M31` space.

---

## Cross-References

- [VEX Mask/Dest-Port/Sub-Opcode](vex-mask-destport-subopcode.md) — the 5-bit `bit0x104` M-register *selector*, the `bit0x10c` dest read-port, and the `bit0x109` second-source port (the encode side that names the register this page decodes).
- [Scan Datapath](scan-datapath.md) — how the scan op *consumes* the selected mask: the two-part predication datapath, the `ScanOp` lowering, and the scan-mode roster.
- [VectorExtended / VEX](vectorextended-vex.md) — the VEX op family, opcode dispatch, and full op roster.
- [Segmented Scan](segmented-scan.md) — the segment-boundary operand frame (the `(data, segment)` binding, distinct from the M-register mask).
- [TEC (Vector) Engine](tec-engine.md) — the 64-byte SparseCore vector bundle that hosts the VEX slot.
- [SparseCore Overview](overview.md) — where the TEC/VEX datapath and the mask register file sit in the SparseCore architecture.
