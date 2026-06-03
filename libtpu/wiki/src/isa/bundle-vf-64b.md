# Viperfish 64-Byte Bundle

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). All addresses are virtual addresses; `.text` and `.rodata` are mapped 1:1 (VA == file offset). Other wheel versions differ.*

## Abstract

Viperfish (`kViperfish`, TpuVersion enum 3, external "TPU v5" / "TPU v5 lite") is the first TPU TensorCore generation whose VLIW issue word is **64 bytes** (512 bits) wide, up from Pufferfish's 51 bytes and Jellyfish's 41. Both cloud SKU names map to this one codename: the accelerator-type parser `AcceleratorTypeToTpuVersionEnum` (@ `0x204cf620`) routes `v5e` *and* `v5lite` to enum case 5 and `v5p` to case 6, but the codec/HAL family for both is the single `Viperfish` generation — there is no separate per-cloud-name codec. The 64-byte width is not derived by zero-extending a narrower bundle — it is returned (after an `a2 == 0` component check) from the codec-metadata virtual `ViperfishCodecMetadata::BundleSizeBytes(TpuSequencerType)` (@ `0x1ee71320`, `return 64`), one cell of the `(TpuVersion, TpuSequencerType)`-keyed codec-metadata table described on the [Bundle Model](bundle-model-overview.md) page. The 64-byte width recurs in the two later VXC-family generations Ghostlite (`kGhostlite`, enum 4, external "TPU v6 lite") and 6acc60406 (`k6acc60406`, enum 5, external "TPU7x"), but each has its *own* per-generation codec class and slot-encoder set; this page documents Viperfish specifically and notes where the family diverges. ("Trillium" appears nowhere in the binary — the internal name for the v6e generation is the obfuscated tag `6acc60406`.)

The structural break from the JF/PF model is the encode mechanism, not just the width. Jellyfish and Pufferfish each have a single monolithic encoder — `EncoderJf::EncodeBundleInternal` (@ `0x1e86c7c0`) and `EncoderPfTensorCore::EncodeBundleInternal` (@ `0x1e8c5c40`) — that walks one `Bundle` object and packs every slot inline. Viperfish has **no `EncodeBundleInternal`**. Its top-level entry is the codec dispatcher `TpuCodecViperfish::EncodeBundle` (@ `0x1e8449a0`), which switches on `TpuSequencerType` and, for the TensorCore case (sequencer 0), constructs the `asic_sw::deepsea::vxc::isa::TensorCoreCodecBase<…>` (whose template-argument list *is* the slot-encoder walk order) and calls `viperfish::isa::EncoderVfTensorCore::EncodeBundle` (@ `0x1d2f7ce0`). That function casts the bundle facade to `vxc::isa::TensorCoreBundle` (or `TensorCoreBundleCompact`), allocates a zeroed `BundleSizeBytes()`-long buffer, then invokes the codec's `TensorCoreBundle::Encode` worker (a vxc `TensorCoreCodecBase` member, in the `0x1d2f8…` range), which serializes each slot through its own `<Slot>Encoder::Encode` method. Every field write — opcode, operand, predicate — goes through one universal bit-granular packer, `BitCopy(dst, dst_bit, src, src_bit, nbits)` (@ `0x1fa0a900`, LSB-first), so the whole bundle layout is expressed as a flat list of *(absolute-bit, width)* triples rather than a struct cast.

> **CORRECTION (VF-1) —** an earlier draft attributed the orchestrator to `EncodeBundle @ 0x1e838cc0` walking a `TensorCoreCodecBase` worker at `0x1d371540` (ctor `0x1d371e80`). Those three addresses are the **6acc60406 / gfc (TPU7x)** codec — their namespaces are all `asic_sw::deepsea::gxc::gfc::isa` and the `default` arm names `tpu_codec_6acc60406.cc`. The genuine Viperfish path is `TpuCodecViperfish::EncodeBundle @ 0x1e8449a0` → `EncoderVfTensorCore::EncodeBundle @ 0x1d2f7ce0` → the vxc `TensorCoreCodecBase` worker. The per-slot encoder addresses below were independently re-confirmed as `asic_sw::deepsea::vxc::isa` and are correct.

The rest of the page maps those triples. It covers the slot taxonomy and which bytes each slot occupies, the two MXU slots (`VectorExtended0/1`) with their matmul / weight-latch / matpush / transpose / lane / cross-lane op families, the `VectorResult0/1` pop slots that drain the MXU and the EUP, the EUP/transcendental **push** that — unlike every other compute op — lives in VALU slot 3 rather than a dedicated slot, the immediate, sequencer, and predicate slot positions, and how V5+ structurally differs from the JF/PF `EncodeBundleInternal` model.

For reimplementation, the contract is:

- The 64-byte width and that it is a codec-metadata constant (`ViperfishCodecMetadata::BundleSizeBytes` → `64`), selected by `(TpuVersion, TpuSequencerType)` lookup — never by extending a 51-byte bundle.
- The V5+ two-stage encode chain: `isa_emitter::EmitX` populates a proto submessage; `<Slot>Encoder::Encode` then `BitCopy`s each proto field to a generation-fixed *absolute bit* in the 512-bit buffer. The entry is `TpuCodecViperfish::EncodeBundle` → `EncoderVfTensorCore::EncodeBundle`; there is no `EncodeBundleInternal`.
- The MXU slot model: two `VectorExtended` slots (one per physical MXU) sharing one 8×6-bit systolic-feed vreg region, with distinct opcode/control regions; the matmul opcode is 7-bit @ bit 57, the data-format sub-discriminator 4-bit @ bit 51, the MXU-id 4-bit @ bit 64.
- The latch/push/matmul opcode families (`LoadMatrixRegister{Gmr,Lmr}{Msra,Msrb}`, `Pushmatrix<fmt>`, `MatrixMultiply<fmt>[Lgmr{Msra,Msrb}][Masked]`) and the EUP push-pop protocol (VALU3 push → `VectorResult` `PopEupResult`).

| | |
|---|---|
| **Generation** | Viperfish — `kViperfish`, TpuVersion enum 3, external "TPU v5" / "TPU v5 lite" (per [Bundle Model](bundle-model-overview.md)) |
| **Cloud SKUs** | `v5e`/`v5lite` (case 5) and `v5p` (case 6) in `AcceleratorTypeToTpuVersionEnum` @ `0x204cf620` — both served by the one Viperfish codec |
| **Namespace** | `asic_sw::deepsea::vxc::isa` (TC), `asic_sw::deepsea::vxc::vfc::isa` (SparseCore TEC/SCS) |
| **Bundle width** | **64 B / 512 bit** — `ViperfishCodecMetadata::BundleSizeBytes` @ `0x1ee71320` `return 64` (after `a2==0` component check) |
| **Width dispatch** | `codec_metadata::BundleSizeBytes(TpuVersion, TpuSequencerType)` @ `0x1ecf7180` → `GetMetadataOrDie` @ `0x1ecf6f60` (`CodecMetadataRegistry` hash-map) → vtable +16 |
| **Encode entry** | `TpuCodecViperfish::EncodeBundle` @ `0x1e8449a0` → `EncoderVfTensorCore::EncodeBundle` @ `0x1d2f7ce0` → vxc `TensorCoreCodecBase` worker (`0x1d2f8…`), per-slot `Encoder::Encode` walk |
| **Universal packer** | `BitCopy(dst, dst_bit, src, src_bit, nbits)` @ `0x1fa0a900` — bit-granular, LSB-first (`byte = dst_bit>>3`, `bit = dst_bit&7`) |
| **MXU slots** | `TensorCoreVectorExtended0Encoder::Encode` @ `0x1efa0f60` (switch on proto opcode, max case `0x66`=102 = `CoreToCoreMove`), `…Extended1Encoder::Encode` @ `0x1efec020` |
| **Result slots** | `TensorCoreVectorResult0Encoder::Encode` @ `0x1f018f40`, `…Result1Encoder::Encode` @ `0x1f019d80` |
| **EUP push** | `EncodeTensorCoreVectorAlu3EupPush` @ `0x1ef6e400` (VALU slot 3) |
| **MXU op census** | 94 `VectorExtended0` + 94 `VectorExtended1` per-op helpers; 0 `Extended2/3Encoder` (exactly two MXU issue slots) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Slot Taxonomy and Byte Map

A Viperfish bundle is a 512-bit flat buffer. Every functional unit reads one contiguous bit window of it; the windows are fixed per generation and populated by the per-slot encoders. The reader should think of the 64 bytes as a packed bitfield indexed from bit 0 = LSB of byte 0, exactly as `BitCopy` indexes it.

The slot taxonomy is the same `Bundle` sub-instruction set as Pufferfish (see [Bundle Model](bundle-model-overview.md)), with the V5+ doubling of the MXU and the EUP moved into the VALU. The table below gives each slot, the encoder that serializes it, and the bit region it occupies (from the verified `BitCopy` offsets of the representative ops).

| Slot | Engine | Encoder (`asic_sw::deepsea::vxc::isa::…`) | Region (bits) | Confidence |
|---|---|---|---|---|
| `VectorExtended0` | MXU 0 (matmul / latch / push / transpose / lane / xlane) | `TensorCoreVectorExtended0Encoder::Encode` @ `0x1efa0f60` | opcode/ctl ~48–64; operand pool 157–293 | CERTAIN |
| `VectorExtended1` | MXU 1 | `TensorCoreVectorExtended1Encoder::Encode` @ `0x1efec020` | opcode/ctl ~28–44; shares the 157–293 pool | CERTAIN |
| `VectorResult0` | MXU/EUP/transpose result pop | `TensorCoreVectorResult0Encoder::Encode` @ `0x1f018f40` | 11–24 (dest @ 14) | CERTAIN |
| `VectorResult1` | second result pop | `TensorCoreVectorResult1Encoder::Encode` @ `0x1f019d80` | mirror of Result0 | HIGH |
| `VectorAlu0…3` | 4 VALU lanes (`Alu3` also issues the EUP push) | `TensorCoreVectorAlu{0..3}Encoder::Encode` | VALU0 opcode @ 299 w7; Alu3 EUP @ 186/197 | CERTAIN |
| `VectorLoad0/1` | vector memory load | `TensorCoreVectorLoad{0,1}Encoder` | per-gen repacked (see [Memory Load](slot-memory-load.md)) | HIGH |
| `VectorStore` | vector memory store | `TensorCoreVectorStoreEncoder` | data-vreg @ 170 w4; base @ 157 w6 | HIGH |
| `ScalarAlu0/1` | sequencer / scalar pipe (branch/call/halt/LCC) | `TensorCoreScalarAlu0Encoder::Encode` @ `0x1eecb900` | opcode-low @ 488 w5; pred @ 499/503 | CERTAIN |
| `Immediates` | 6 immediate slots (branch/call offset home) | `TensorCoreImmediatesEncoder::Encode` @ `0x1eebee40` | imm0 @ 430 … imm5 @ 330, each w20 | CERTAIN |
| `Predicates` | predicate pool (per-slot field on VF) | per-slot 4+1 field at slot top | TC ScalarAlu0 @ 499/503 | CERTAIN |

> **NOTE —** the byte regions are *not* contiguous per slot in the obvious way. The MXU operand pool (bits 157–293, the eight 6-bit source vregs) lives physically in the middle of the bundle and is *shared* between `VectorExtended0` and `VectorExtended1`; only the opcode/control words at the top of each MXU slot are distinct. A reimplementation that assumes each slot owns a private contiguous run of bytes will mis-pack the second MXU.

> **QUIRK —** there is no `Sparsity` slot in the Viperfish TensorCore bundle. The 94 `VectorExtended0` op families (matmul, latch, push, transpose, lane-broadcast/rotate, permute, cross-lane reduce, set-pattern-register, core-to-core move) contain no structured-sparsity op. Structured sparsity arrives in a later generation; see [Sparsity Slot](slot-sparsity-v5plus.md) (the backing finding is still open — do not invent a VF sparsity field).

---

## The V5+ Encode Model — Why There Is No EncodeBundleInternal

### Purpose

The single most important structural fact about the Viperfish bundle is *how* it is built, because the JF/PF mental model breaks. Jellyfish and Pufferfish carry a per-encoder `EncodeBundleInternal` that takes a `Bundle` and packs all slots in one function body, mixing `and`/`shl`/`or` (Jellyfish direct-pack) or `BitCopy` (Pufferfish). Viperfish replaces that with a templated codec whose slot list is fixed at compile time and a per-slot encoder per slot. A reimplementer who looks for `EncoderVf::EncodeBundleInternal` will not find it.

### Entry Point

```text
TpuCodecViperfish::EncodeBundle @0x1e8449a0    ── codec dispatch on TpuSequencerType (vxc)
  ├─ (TC, seq 0)  → vxc::isa::TensorCoreCodecBase<…> ctor (operator new 0xF0)
  │                  EncoderVfTensorCore::EncodeBundle @0x1d2f7ce0
  │                    └─ TensorCoreBundle::Encode worker (0x1d2f8… range)
  │                       ── walks each slot Encoder in template-arg order:
  │             ├─ TensorCoreImmediatesEncoder::Encode      @0x1eebee40
  │             ├─ TensorCoreScalarAlu0Encoder::Encode      @0x1eecb900
  │             ├─ TensorCoreVectorAlu{0..3}Encoder::Encode
  │             ├─ TensorCoreVectorExtended0Encoder::Encode @0x1efa0f60   ── MXU 0
  │             ├─ TensorCoreVectorExtended1Encoder::Encode @0x1efec020   ── MXU 1
  │             ├─ TensorCoreVectorResult0Encoder::Encode   @0x1f018f40
  │             ├─ TensorCoreVectorResult1Encoder::Encode   @0x1f019d80
  │             ├─ TensorCoreVectorStoreEncoder::Encode
  │             └─ TensorCoreVectorLoad{0,1,2}Encoder::Encode
  ├─ (SCS, seq 3) → vxc::vfc::isa::SparseCoreScsCodecBase<…> → EncoderVfSparseCoreScs::EncodeBundle
  ├─ (TAC, seq 4) → vxc::vfc::isa::SparseCoreTacCodecBase<…> → EncoderVfSparseCoreTac::EncodeBundle
  └─ (TEC, seq 5) → vxc::vfc::isa::SparseCoreTecCodecBase<…> → EncoderVfSparseCoreTec::EncodeBundle
```

> **NOTE —** the TC codec template lists *three* vector-load slots (`TensorCoreVectorLoad0/1/2`) and carries `Predication` as a template type parameter rather than as a dedicated `TensorCorePredicatesEncoder` slot — that dedicated predicate-slot encoder first appears in the 6acc60406 (gfc) TC codec, consistent with the [Bundle Model](bundle-model-overview.md)'s note that the predicate slot is a 6acc60406 addition.

### Algorithm

The two-stage chain is: upstream the `isa_emitter` EmitX template populates a proto submessage and sets its present bit; downstream the slot encoder reads that submessage and `BitCopy`s each field to its absolute bit. The encoder bodies inline the per-op packing as a `switch` on the proto opcode (proto + 0x50); each `switch` arm is one op family's field layout.

```c
function TpuCodecViperfish::EncodeBundle(out, bundle, seq):   // @0x1e8449a0
    switch (seq):                              // TpuSequencerType
        case 0: codec = vxc::TensorCoreCodecBase<…>()         // TC
                EncoderVfTensorCore::EncodeBundle(out, bundle) // @0x1d2f7ce0
        case 3: …SparseCoreScs…   case 4: …SparseCoreTac…   case 5: …SparseCoreTec…

function EncoderVfTensorCore::EncodeBundle(out, bundle):   // @0x1d2f7ce0
    tc = cast<vxc::isa::TensorCoreBundle>(bundle)           // or TensorCoreBundleCompact
    buf = zeroed(tc.BundleSizeBytes())                      // 64 bytes
    tc.Encode(buf)            // vxc TensorCoreCodecBase worker, walks slot encoders
    // worker, in template-arg order, calls each SlotEncoder.Encode(slot_proto, buf):

function TensorCoreVectorExtended0Encoder::Encode(proto, out):   // @0x1efa0f60
    out_field = *(int*)(proto + 0x1c)
    BitCopy(out, 64, &out_field, 0, 4)         // MXU-id (unit) written FIRST, always
    switch (*(int*)(proto + 0x50)):             // proto opcode — the op-family dispatch
        case 0:    return                       // empty slot — nothing packed
        case 0xC:  EncodeMatrixMultiplyBf16(out, proto)   // = decimal 12
        case 0x41: EncodePushmatrixBf16(out, proto)       // = decimal 65
        case …:    // 94 op families; max case 0x66 (=102) = CoreToCoreMove
```

> **GOTCHA —** the present-bit guard. Every field `BitCopy` is gated on a proto present bit (`(proto[2] & mask) != 0`), and when the field is absent the encoder reads a **default-instance global** (`…_globals_`) instead of skipping. The decompiled `MatrixMultiplyBf16` body (@ `0x1efa2e40`) interleaves the live-proto path and the `…MatrixMultiplyBf16_globals_` default path for *every* field. A reimplementation that omits the default-instance fallback will leave stale bits when a field is unset; the hardware default for an unset matmul source vreg is whatever the proto default carries, not zero.

---

## MXU Slots — VectorExtended0 / VectorExtended1

### Purpose

Each `VectorExtended` slot drives one MXU and carries the full matrix-unit op vocabulary: dense matmul (`MatrixMultiply<fmt>`), the weight-stationary latch (`LoadMatrixRegister{Gmr,Lmr}{Msra,Msrb}`), the moving-operand push (`Pushmatrix<fmt>`), the latch-via-LMR fused matmul (`MatrixMultiplyLmr` / `…Lgmr{Msra,Msrb}`), plus transpose, lane-broadcast/rotate, permute, cross-lane reduce, and pattern-register ops. There are exactly two slots — the binary has 94 `Extended0` and 94 `Extended1` per-op helpers and **zero** `Extended2/3`.

### Encoding — Absolute Bit Map

The MXU control region for Viperfish, verified from the decompiled `MatrixMultiplyBf16` (@ `0x1efa2e40`), `PushmatrixBf16` (@ `0x1efaf820`), and `LoadMatrixRegisterGmrMsra` helpers:

| Field | Abs bit | Width | Written by | Value (Bf16 case) | Confidence |
|---|---|---|---|---|---|
| MXU-id (unit) | 64 | 4 | dispatcher (proto + 0x1c) | 0 (MXU 0) | CERTAIN |
| opcode-HIGH (matmul) | 57 | 7 | `MatrixMultiply<fmt>` | `0x1` | CERTAIN |
| opcode-HIGH (latch) | 57 | 7 | `LoadMatrixRegister*` | `0x37` (55) | CERTAIN |
| opcode-HIGH (push) | 59 | 5 | `Pushmatrix<fmt>` | `0xe` (14) | CERTAIN |
| data-format sub-disc | 51 | 4 | per-op | matmul Bf16=1 / push Bf16=3 / latch=0 | CERTAIN |
| control (3-bit) | 48 | 3 | per-op | proto + 0x18 | CERTAIN |
| done-gains / latch flag | 55 | 2 | per-op | proto + 0x1c | CERTAIN |
| Transpose field (push/latch) | 57 | 1 | `Pushmatrix*` (proto + 0x20) | — | CERTAIN |
| Target field (push/latch) | 58 | 1 | `Pushmatrix*` (proto + 0x24) | — | CERTAIN |
| push-src vreg | 180 | 6 | `Pushmatrix*` (proto + 0x44) | — | CERTAIN |
| primary operand (matmul) | 180 | 6 | `MatrixMultiply*` | proto + 0x3c | CERTAIN |
| src vreg #1 (proto + 0x20) | 157 | 6 | matmul | systolic feed | CERTAIN |
| src vreg #2 (proto + 0x24) | 282 | 6 | matmul | systolic feed | CERTAIN |
| src vreg #3 (proto + 0x28) | 293 | 6 | matmul | systolic feed | CERTAIN |
| src vreg #4 (proto + 0x2c) | 248 | 6 | matmul | systolic feed | CERTAIN |
| src vreg #5 (proto + 0x30) | 259 | 6 | matmul | systolic feed | CERTAIN |
| src vreg #6 (proto + 0x34) | 214 | 6 | matmul | systolic feed | CERTAIN |
| src vreg #7 (proto + 0x38) | 225 | 6 | matmul | systolic feed | CERTAIN |

> **QUIRK —** the opcode field changes both position and width by op family at the *same* slot. `MatrixMultiply<fmt>` writes a 7-bit opcode at bit 57; `Pushmatrix<fmt>` writes a 5-bit opcode-HIGH at bit 59 (with the 4-bit data-format at bit 51 below it); `LoadMatrixRegister*` reuses the 7-bit @ 57 window but with value `0x37`. On the push path, bits 57 and 58 are *repurposed* as the 1-bit **Transpose** and **Target** fields — the same physical bits that on the matmul path are the two high bits of the 7-bit opcode. The decoder distinguishes them by the opcode-HIGH value (a push/latch has opcode-HIGH `0xe`, a matmul `0x1`), so the LSB region is free to carry latch control. On the decode side the `Pushmatrix*TransposeField` reads `[base+8]>>0x39&1` = abs57 and the `…TargetField` reads `>>0x3a&1` = abs58.

### The Shared Systolic-Feed Operand Pool

The eight source-vreg fields (bits 157/282/293/248/259/214/225 plus the operand @ 180) are the systolic operand stream into the MXU. The decisive structural fact: `VectorExtended0` and `VectorExtended1` read the same vregs from the **same absolute bits** — only the opcode/control region differs between the two slots. Both MXUs draw the same vector read ports; the two slots address two physical matrix units over one shared operand pool.

```c
// VectorExtended0 (MXU 0) and VectorExtended1 (MXU 1):
//   opcode/control region: distinct per slot (Extended1 shifted down ~20 bits)
//   operand pool 157..293: IDENTICAL absolute bits in both slots
function EncodeMatrixMultiplyBf16(out, proto):     // @0x1efa2e40, proto opcode 0xc
    BitCopy(out, 57, {0x1}, 0, 7)                  // opcode-HIGH = 1
    BitCopy(out, 51, {0x1}, 0, 4)                  // data-format = bf16
    BitCopy(out, 48, proto.control, 0, 3)
    BitCopy(out, 55, proto.done_gains, 0, 2)
    BitCopy(out, 157, proto.src0, 0, 6)            // present-gated; else read globals_
    BitCopy(out, 282, proto.src1, 0, 6)
    BitCopy(out, 293, proto.src2, 0, 6)
    BitCopy(out, 248, proto.src3, 0, 6)
    BitCopy(out, 259, proto.src4, 0, 6)
    BitCopy(out, 214, proto.src5, 0, 6)
    BitCopy(out, 225, proto.src6, 0, 6)
    BitCopy(out, 180, proto.operand, 0, 6)
```

### Op Inventory

The 94 helpers per slot are not 94 distinct layouts — they are one `{opcode, sub-format, MXU-id, operand}` template specialized by the opcode immediate and the operand-present mask. The op families:

| Family | Members | Role |
|---|---|---|
| `MatrixMultiply<fmt>` | Bf16, Bf8, If8Bf16, S4, S8, U4, U8, F32Rounded, Lmr | dense matmul step, one helper per data format |
| `MatrixMultiply<fmt>Lgmr{Msra,Msrb}[Masked]` | per fmt × {Msra, Msrb} × {plain, Masked} | latch-via-LMR fused matmul (multi-pass K-tiling) |
| `Pushmatrix<fmt>` | Bf16, Bf8, S4, S8, U4, U8, Rounded, PackedIf8Conv | moving-operand push (matprep) |
| `LoadMatrixRegister{Gmr,Lmr}{Msra,Msrb}` | 4 variants | weight-stationary latch (opcode-HIGH `0x37`) |
| `*TransposeStart[End]`, `*TransposeContinueAnyType`, `Segmented*`, `Packed*` | ~10 | systolic-array transpose |
| `LaneBroadcast[Packed]`, `LaneRotate`, `PackedLaneRotate`, `Permute[Packed]` | ~6 | intra-lane data movement |
| `Xlane{Add,Max,Min}[Index]` | 5 | cross-lane reduction |
| `SetPatternRegisterPcr[Bytes,Sublanes]`, `CoreToCoreMove`, `SupplementalPackedXlu` | 4 | pattern-register / inter-core / XLU supplement |

> **NOTE —** the `Masked` suffix and the `Lgmr{Msra,Msrb}` suffix are the V5+ realization of the Jellyfish 6-entry `GainLatchMode → VEOpcode` table: what was a small enum on v3 became a named opcode family on v5. `Msra`/`Msrb` select which of the two matrix-staging-register banks the fused matmul accumulates into. See [MXU Slot](slot-mxu.md) for the cross-generation opcode story and [MXU Latency (Viperfish)](../cost/mxu-latency-vf.md) for the latency of each.

---

## Result Slots — VectorResult0 / VectorResult1

### Purpose

The `VectorResult` slot is the *pop* side of the push-pop protocols. It drains a finished MXU matmul (`PopMxuResult`), an EUP transcendental result (`PopEupResult`), a transpose result (`TransposeResult`), or — uniquely on Viperfish — a scalar/CRF pop (`PopCcrfResult`). The slot is a single discriminator-plus-tail layout: a 4-bit result-type discriminator selects which sub-message is present, and a common tail `BitCopy`s the dest vreg.

### Encoding — Absolute Bit Map (Viperfish)

Verified from `TensorCoreVectorResult0Encoder::Encode` (@ `0x1f018f40`):

| Field | Abs bit | Width | Notes | Confidence |
|---|---|---|---|---|
| header field (proto + 0x1c) | 24 | 4 | written first, every result-type | CERTAIN |
| sub-type selector | 22 | 2 | constant 0/1/2/3 = PopEup / PopMxu / Transpose / PopCcrf | CERTAIN |
| result mode (proto + 0x18) | 20 | 2 | per result-type (Mxu/Transpose only) | CERTAIN |
| dest vreg | 14 | 6 | common tail | CERTAIN |

The four Viperfish sub-messages (proto types under `asic_sw::deepsea::vxc::isa`), dispatched by the `proto + 0x50` opcode and tagged by the 2-bit selector at bit 22:

| Sub-message | proto opcode | bit-22 selector | Role | Confidence |
|---|---|---|---|---|
| `TensorCoreVectorResult_PopEupResult` | 5 | 0 | drain an EUP/transcendental result | CERTAIN |
| `TensorCoreVectorResult_PopMxuResult` | 6 | 1 | drain a finished MXU matmul accumulator | CERTAIN |
| `TensorCoreVectorResult_TransposeResult` | 7 | 2 | drain a systolic transpose | CERTAIN |
| `TensorCoreVectorResult_PopCcrfResult` | 8 | 3 | scalar/cross-core-register-file pop (vxc-only) | CERTAIN |

> **GOTCHA —** the 2-bit selector at bit 22 (values 0/1/2/3) is the field a decoder keys on, and it does **not** equal the `proto + 0x50` opcode (5/6/7/8). The encoder reads the opcode to choose a `switch` arm, then writes the *selector* constant. PopEup is reachable from two arms — opcode 5 and opcode 8 (the `else` of the PopCcrf branch falls through to the PopEup default-instance), so a reimplementation that maps opcode→selector 1:1 will mis-tag those. The dest vreg lands at the common tail bit 14 (w6) regardless of sub-type.

> **QUIRK —** the result sub-message *set* is generation-specific, not constant: a cross-generation reimplementation must key it on the `TpuVersion`, not assume Viperfish's four carry over. On Viperfish the set is `{PopEup, PopMxu, Transpose, PopCcrf}` (the four `switch` arms above). The Ghostlite and 6acc60406 sets differ (e.g. Ghostlite is reported to add a fused matres+accumulate result for the K>128 multi-pass path) — those deltas are tracked on the per-generation bundle pages and are not re-derived here.

---

## EUP / Transcendental Push — VALU Slot 3

### Purpose

The Extended Unary Processor (the transcendental unit) has no dedicated bundle slot. Its **push** is a VALU slot-3 (`Alu3`) op, and its **pop** is the `VectorResult` `PopEupResult`. This is the bit-exact realization of the push-pop transcendental model: an `Alu3` op pushes a source vreg into the EUP pipeline tagged with a 5-bit function selector, and one or more bundles later a `VectorResult` op pops the result into a dest vreg. The single-issue EUP means only `Alu3` (not `Alu0/1/2`) sources the push — the transcendental helpers exist *only* in the `Alu3` set.

### Encoding — Absolute Bit Map (Viperfish)

Verified from `EncodeTensorCoreVectorAlu3EupPush` (@ `0x1ef6e400`):

| Field | Abs bit | Width | Value | Confidence |
|---|---|---|---|---|
| VALU opcode (EUP-push family) | 197 | 7 | `0x0` | CERTAIN |
| EUP-function selector | 186 | 5 | `0x16` (22) for the generic `EupPush` | CERTAIN |
| src vreg | 191 | 6 | proto + 0x18 (present-gated) | CERTAIN |

```c
function EncodeTensorCoreVectorAlu3EupPush(out, proto):   // @0x1ef6e400
    BitCopy(out, 197, {0x0}, 0, 7)        // VALU-opcode = EUP-push family
    BitCopy(out, 186, {0x16}, 0, 5)       // generic EUP-push function selector = 22
    if (proto[0x50] == 135 && present):   // proto opcode 135 = EupPush
        BitCopy(out, 191, proto.src, 0, 6)
```

### Push-Pop Protocol

```text
bundle N    :  VALU slot 3 (Alu3)  ── VALU-op=0x0 @197, fn-selector @186, src vreg @191
                 │
                 ▼  (EUP pipeline latency; single-issue XLU hazard)
bundle N+k  :  VectorResult slot   ── PopEupResult (result-opcode 7), dest vreg @14
```

> **NOTE —** Viperfish's named transcendentals (`Reciprocal`, `ReciprocalSqrt`, `Tanh`, `ShiftedSigmoid`, `LogTwo`, `PowTwo`) each have their own `Alu3` helper carrying its own 5-bit selector in the same bit-186 field; the `0x16` value above is the *generic* `EupPush` whose function is carried elsewhere. The per-function selector value table (Tanh, Reciprocal, Erf, Sin/Cos, …) is documented per-generation on [EUP Transcendental Slot](slot-eup-transcendental.md). The pop dest vreg lands at the common `VectorResult` tail (bit 14 w6), so the transcendental result occupies a normal vreg one+ bundles after the push.

---

## Sequencer, Immediate, and Predicate Positions

These slots are not MXU-specific but pin the rest of the 512-bit map. They come from the same `<Slot>Encoder::Encode` + `BitCopy` mechanism and are documented in full on [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md); the Viperfish positions are summarized here for completeness.

### Immediate slots — branch/call/sync offset home

`TensorCoreImmediatesEncoder::Encode` (@ `0x1eebee40`) writes six 20-bit immediate slots, each from `proto + 0x18 … 0x2c`. The branch/call/sync 20-bit signed offset lands in **immediate slot 0**:

| imm slot | proto field | VF abs bit | Width | Confidence |
|---|---|---|---|---|
| 0 (branch/call offset) | proto + 0x18 | 430 | 20 | CERTAIN |
| 1 | proto + 0x1c | 410 | 20 | CERTAIN |
| 2 | proto + 0x20 | 390 | 20 | CERTAIN |
| 3 | proto + 0x24 | 370 | 20 | CERTAIN |
| 4 | proto + 0x28 | 350 | 20 | CERTAIN |
| 5 | proto + 0x2c | 330 | 20 | CERTAIN |

### Sequencer slot

`TensorCoreScalarAlu0Encoder::Encode` (@ `0x1eecb900`) writes a common predication header then dispatches on the proto opcode. The control-op layout:

| Field | Abs bit | Width | Notes | Confidence |
|---|---|---|---|---|
| predication reg index | 499 | 4 | proto + 0x20 | CERTAIN |
| predication inversion | 503 | 1 | proto + 0x18 (byte) | CERTAIN |
| opcode-HIGH / family | 493 | 6 | 0 for branch/call | CERTAIN |
| opcode-LOW / discriminator | 488 | 5 | 4/5/6/7 | CERTAIN |
| x-target sreg (`BranchSreg`) | 488 | 5 | sreg `x()` | HIGH |
| dest (return-addr) sreg | 477 | 5 | `Call` dest | HIGH |

Discriminator: `BranchAbsolute=4`, `BranchRelative=5`, `CallAbsolute=6`, `CallRelative=7`. The branch offset is a signed 20-bit value in immediate slot 0 (bit 430); there is no dedicated return op — a return is a `BranchSreg` reading the link sreg.

> **GOTCHA —** there is no in-bundle delay-slot field on Viperfish. The branch/call helpers write only `{opcode-low, offset (imm0), dest}`; the delay-slot count is a bundle-packer pad-count (empty `kNeverExecute` bundles appended after the branch), not an encoded bit field. A reimplementation that looks for a 3-bit `delay_slots` field in the bundle will not find one.

### Predicate slot

Viperfish uses a **per-slot** 4+1 predicate field (4-bit reg index + 1-bit inversion) at the top of each scalar slot — for `TensorCoreScalarAlu0` the reg is @ bit 499 (w4) and inversion @ bit 503 (w1). `Predication` is a template type parameter of the vxc TC codec, not a standalone slot encoder. This is the encoding the later 6acc60406 (TPU7x) generation replaces with a dedicated `TensorCorePredicatesEncoder` slot; on Viperfish the predicate index is local to each populated slot. Empty slots carry the `kNeverExecute` predicate written into the bundle header (see [Bundle Model](bundle-model-overview.md)).

---

## How Viperfish Differs from the JF/PF Bundle Model

| Axis | Jellyfish (41 B) / Pufferfish (51 B) | Viperfish (64 B) | Confidence |
|---|---|---|---|
| Encode entry | one `Encoder<gen>::EncodeBundleInternal` packing all slots | `TpuCodecViperfish::EncodeBundle` → `EncoderVfTensorCore::EncodeBundle` → per-slot `<Slot>Encoder::Encode` | CERTAIN |
| Field write | JF direct `and`/`shl`/`or`; PF `BitCopy` | every field via `BitCopy`; no direct-pack | CERTAIN |
| Layout source | per-slot `Encode` methods on the `Bundle` | `isa_emitter::EmitX` proto templates + slot encoders | CERTAIN |
| InstBits table | (n/a) | InstBits is all-zero on disk; positions live in the encoders | CERTAIN |
| MXU slots | JF 1 `VectorExtended`; PF 2 (`VE0/1`) | 2 (`VectorExtended0/1`), 94 helpers each, 0 `Extended2/3` | CERTAIN |
| MXU operand model | JF single moving-operand vreg | 8×6-bit systolic-feed pool, *shared* between the two MXU slots | CERTAIN |
| Latch encoding | JF 6-entry `GainLatchMode→VEOpcode` table | named `LoadMatrixRegister{Gmr,Lmr}{Msra,Msrb}` opcode family (opcode-HIGH `0x37`) | CERTAIN |
| EUP transcendental | issued from the VALU slot, popped by result slot | pinned to VALU **slot 3** (`Alu3`); 5-bit fn-selector @ bit 186 | CERTAIN |
| Bundle width source | inline constant in the encoder | `ViperfishCodecMetadata::BundleSizeBytes` → `64` (codec-metadata cell) | CERTAIN |

The clean way to state the break: Pufferfish's `EncodeBundleInternal` *is* the kIsaTable for its slots, baked into one function. Viperfish has no such function — its kIsaTable is the *set* of per-slot `BitCopy` offsets, distributed across ~200 per-op helpers, orchestrated by `TpuCodecViperfish::EncodeBundle` → `EncoderVfTensorCore::EncodeBundle`. The InstBits table that LLVM-MC would normally hold the fixed instruction bits in is entirely zero on disk for all V5+ generations, which is the binary's confirmation that the bits come from the emitter path, not from a static table.

---

## Related Components

| Name | Relationship |
|---|---|
| Pufferfish 51B Bundle | The immediate predecessor: monolithic `EncodeBundleInternal`, 2 MXU slots, no shared-pool / VALU3-EUP model |
| Ghostlite / 6acc60406 | The other two 64-byte VXC-family generations (external "TPU v6 lite" / "TPU7x"); same slot grid, byte-shifted opcode/predicate regions, differing result sub-message sets |
| Bundle Model | The cross-generation width-dispatch and slot-taxonomy reference |
| MXU Slot | The cross-generation matmul/latch opcode story this page specializes for VF |

## Cross-References

- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the predecessor; the `EncodeBundleInternal` model Viperfish replaces
- [Ghostlite Bundle](bundle-gl.md) — sibling 64-byte gen; `PopAddMxu01Result`, byte-shifted opcode region
- [Bundle Model](bundle-model-overview.md) — bundle widths, codec-metadata dispatch, `kNeverExecute` empty-slot convention
- [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md) — the `isa_emitter::EmitX` → `BitCopy` chain and the full sequencer/immediate/predicate positions
- [Sparsity Slot](slot-sparsity-v5plus.md) — the structured-sparsity slot (open; absent from the Viperfish TC bundle)
- [MXU Slot](slot-mxu.md) — cross-generation matmul/latch/push opcode families
- [EUP Transcendental Slot](slot-eup-transcendental.md) — the per-function EUP selector value table and pop semantics
- [IsaEmitter Registry](isa-emitter-registry.md) — the `(TpuVersion, SequencerType)` codec cell census that selects the Viperfish encoders
- [MXU Latency (Viperfish)](../cost/mxu-latency-vf.md) — per-op MXU cost for the Viperfish matmul/latch families
