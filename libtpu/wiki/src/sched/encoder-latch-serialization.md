# Per-Gen Encoder Latch Serialization

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). Other versions differ.*

## Abstract

[Latch assignment](latch-assignment-overrun.md) decides *which index* each MXU weight-latch op carries inside its `MxuSequence`. This page is the *encode* side: how each TPU generation's `Encoder` lowers a finished latch op — already carrying its `GainLatchMode` (`BYTE[op+0x40]`), unit-id/GMR (`WORD[op+0x0b]` bits 8–9), and MSR (`BYTE[op+0x44]`) — into the absolute bit positions of the per-gen TensorCore VLIW bundle's MXU / VectorExtended slot. The latch never travels as raw fields; it goes through a two-stage lowering: a per-gen `*Emitter::EmitVectorLatch` writes the LLO fields into a protobuf submessage, and a per-gen encoder then bit-packs that submessage into the bundle bitstream.

The encode toolkit is `gloop`'s `bitcoding.cc` writer family: a byte-level `Encoder` growable buffer, a `BitEncoder` accumulator with the inlined `PutBits`/`PutVarInt`/`PutGamma` primitives, the `(1<<k)-1` `mask_` table shared with the reader, and — for the V5+ protobuf-backed slots — a standalone `BitCopy(dst, dst_bit, src, src_bit, nbits)` field-blitter that the V5+ codecs call once per field. The same toolkit serves the route-cache codec and the profiler trace codec; this page documents the half that serializes MXU latches. The reader half (`BitDecoder` `GetBits64`/`GetGamma`/`GetVarInt`/`SkipBits`) is on the [route-cache codec page](../routing/route-cache-codec.md).

There are two distinct encoder shapes across generations. **Jellyfish (v2)** is *monolithic*: `EncoderJf::EncodeVectorExtendedInstruction` is one function that reads the proto's `VectorExtendedOpcode` field and runs a 35-case switch that OR-shifts opcode-field constants directly into `QWORD[bundle+12]`, with the GLM mapped through a 6-entry `{7,10,9,12,8,11}` table first. **Pufferfish (v4), Viperfish (v5p), and Ghostlite/`6acc60406` (v6e/TPU7x)** are *oneof-typed*: `EmitVectorLatch` selects a typed `PushGains*`/`Pushmatrix*`/`PushMatrix*` submessage by GLM, and one generated `Encode…<Variant>` sub-encoder per variant blits its fields with `BitCopy`. Viperfish alone splits its 7-bit MXU opcode field so a latch and a matmul share it, and the matmul opcode's LSB is the **MSR-select bit (bundle bit 57)** — the bundle-level encoding of the MSR-A/MSR-B overrun handshake that only Viperfish has.

Finally, the page pins the `CreateVectorLatchLsf` entry guard `opcode_produced_register_type[gain_src.opcode] != 4`. Register-type 4 is the *vector* class; the guard requires the gain matrix the LSF latch consumes to come from a vector register, taking a slow `LloModule::UpdateStatus` diagnostic path otherwise. It is the gate that decides which latch ops are even built and is the reason a latch's `register_number` is a Vregno.

For reimplementation, the contract is:

- **The latch's `GainLatchMode` becomes the slot opcode (and, on V5+, the dtype/format).** On JF it indexes a 6-entry GLM→VEopcode table, then a 35-case VEopcode→opcode-field switch. On PF it picks a `PushGains*` oneof whose 7-bit opcode `@abs91` follows `0x20 + variant + 8·transposed + 0x10·masked`. On VF it picks a `Pushmatrix*` oneof writing opcode-high `14` `@abs59` (5b) + `MatmulDataFormat` `@abs51` (4b); the dtype rides the *format* field, not the opcode (the masked variants instead bump the opcode to 15..23).
- **The unit-id/GMR (MXU quadrant) becomes a slot field.** JF writes it via `AddMxuNumToVectorExtended` into proto `+0x70`, encoded `@abs27-28` (2b).
- **The MSR is a Viperfish-only bit.** PF has a single MSR and emits no MSR bit. VF encodes MSR-A/MSR-B as the LSB of the 7-bit matmul opcode `@abs57`; the latch writes a 1-bit control field `@abs57` from proto `+0x20` and a coupled control bit `@abs58` from proto `+0x24`.
- **`BitCopy(dst, abs_bit, &value, 0, width)` is the V5+ field writer.** `abs_bit` is the absolute bundle bit and `width` the field width in bits; the bundle is a flat byte buffer, LSB-first within each byte.
- **The type-4 (vector) gain-register guard gates the build.** `CreateVectorLatchLsf` reads `opcode_produced_register_type[gain_src.opcode]`; if it is not 4 it records a `chunk->ProducesVreg()` diagnostic via `UpdateStatus` before proceeding.

| | |
|---|---|
| **Bit-writer primitives** | `gloop` `coder.cc` `Encoder` + `bitcoding.cc` `BitEncoder` (`PutBits`/`PutVarInt`/`PutGamma`, inlined) |
| **V5+ field blitter** | `BitCopy(dst, dst_bit, src, src_bit, nbits)` `sub_1FA0A900` |
| **Shared mask table** | `BitEncoder::mask_` `@0xbe79440` — 65 qwords, `mask_[k]=(1<<k)-1` |
| **JF encoder (monolithic)** | `EncoderJf::EncodeVectorExtendedInstruction` `sub_1E869F00` |
| **JF GLM→VEopcode table** | `dword_AEF42AC` = `{7,10,9,12,8,11}` (6 entries, indexed by GLM 0..5) |
| **PF emitter / encoder** | `PufferfishTensorCoreEmitter::EmitVectorLatch` `sub_1410E1A0` → `PushGains*` sub-encoders `sub_1EDC1660`… |
| **VF latch encoder (bf16)** | `Encode…0PushmatrixBf16` `sub_1EFAF820`: op `14@abs59`, fmt `3@abs51`, MSR `@abs57`, ctl `@abs58` |
| **VF MSR-select bit** | bundle bit **57** = LSB of matmul 7-bit opcode (`…Msra`=2, `…Msrb`=3) |
| **Type-4 gain guard** | `CreateVectorLatchLsf` `sub_1D4D7AA0`: `opcode_produced_register_type[gain_src.opcode] != 4` → slow `UpdateStatus` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Bit-Writer Primitives

### Purpose

Every bundle byte is produced by the same `gloop` writer toolkit. The toolkit has three layers: a byte-level growable buffer (`Encoder`), a bit accumulator over it (`BitEncoder`), and — for the protobuf-backed V5+ slots — a standalone `BitCopy` that writes one field directly into the bundle's flat byte array by absolute bit position. JF's monolithic encoder bypasses `BitCopy` and shifts constants straight into a `uint64` bundle word; the V5+ encoders blit field by field with `BitCopy`. There are no exported `PutBits`/`WriteBits` symbols — they are inlined into every call site — so the primitives are reconstructed from `BitEncoder::Initialize` (a gamma-table self-test that exercises the full encode→decode round-trip) and from the real V5+ sub-encoders.

### The Encoder byte buffer

`Encoder` (`coder.cc`) is a four-pointer growable buffer. Its destructor (`sub_21073980`) carries a buffer-overrun guard that is the strongest evidence of its layout:

```c
struct Encoder {                 // 0x20 bytes
    char* cursor;     // +0x00  buf_  — next write position
    char* limit;      // +0x08  limit_ — one-past the writable region
    char* begin;      // +0x10  owned allocation base
    char* alloc_end;  // +0x18  alloc end | owns-heap flag (top bit); SSO marker 0x8000000000000028
};

function Encoder::~Encoder(this):                 // sub_21073980
    if (this.cursor > this.limit):                // CHECK buf_ <= limit_  (coder.cc:34)
        LogFatal("buf_ <= limit_")                // overrun guard — fires on any spill past limit_
    if (this.alloc_end == this.begin):            // a heap slot was taken
        free(this.begin)
```

> **GOTCHA — the overrun guard is a destructor CHECK, not a bounds check on each write.** `coder.cc:34` (`buf_ <= limit_`) fires only when the buffer is destroyed, so an encoder that overruns `limit_` during packing keeps writing past the allocation and only aborts at teardown. A reimplementation must size the buffer to the bundle width up front; the gloop writer relies on the inline SSO buffer (`0x8000000000000028` marker) being large enough for every shipped bundle, and the grow/realloc path is not on any latch-encode trace.

### PutBits / PutVarInt / PutGamma

`BitEncoder` wraps an `Encoder` plus a `uint64` accumulator and a bit count. `BitEncoder::Initialize` (`sub_21072D40`) builds the runtime gamma table by encoding every value 1..255 with `PutGamma` and re-decoding each with `GetGamma`, asserting equality — which exposes the inlined primitives byte-exactly:

```c
// PutBits(value, n): mask to n bits, OR into the accumulator at the current
// bit position, then spill whole bytes LSB-first to the Encoder.
function PutBits(value, n):
    acc |= (value & mask_[n]) << bitpos       // mask_ @0xbe79440 = (1<<k)-1
    bitpos += n
    while bitpos >= 8:                         // byte-spill loop (seen in Initialize)
        *cursor = (uint8)acc                   //   *(_BYTE*)v13 = v6
        cursor += 1
        acc >>= 8                              //   v12 >>= 8
        bitpos -= 8                            //   v5 -= 8

// PutGamma(value>=1): Elias-gamma — b zero-bits, a 1, then the low b bits.
function PutGamma(value):
    b = floor(log2(value))                     // _BitScanReverse64(value | 1)
    emit b zero-bits, then a 1, then (value & mask_[b])
    // Initialize stores gamma_[i] = i | (codeword_len << 24);
    //   CHECK (value & 0xffffff) == value   (bitcoding.cc:110)
    //   re-decodes via GetGamma; CHECK v == i (bitcoding.cc:128)

// PutVarInt(chunk, value): self-delimiting integer — exact inverse of GetVarInt.
function PutVarInt(chunk, value):
    num_groups = max(1, ceil(bitlen(value) / chunk))
    emit (num_groups-1) one-bits then a 0      // unary prefix
    emit num_groups little-endian chunk-bit groups of value
```

`mask_` (`@0xbe79440`, `.rodata`, 65 qwords) is shared with the reader; both clamp a value to `n` bits with `value & mask_[n]`. `gamma_` (`@0x22593d00`, `.bss`, 256 × `uint32`) is built at runtime, not baked.

> **NOTE — the MXU latch path uses none of `PutVarInt`/`PutGamma`.** Those self-delimiting forms appear only on the route-cache path (and the gamma self-test). The JF monolithic encoder uses raw shift/OR; the V5+ encoders use fixed-width `BitCopy`. Latch fields are all fixed-width. `PutVarInt`/`PutGamma` are documented here only because they share the `Encoder`/`mask_` substrate.

### BitCopy — the V5+ field blitter

The V5+ TensorCore codecs do not accumulate; they write each field into the bundle byte array directly:

```c
// BitCopy(dst, dst_bit, src, src_bit, nbits)        // sub_1FA0A900
//   dst      : the bundle's flat byte buffer
//   dst_bit  : absolute bit position in the bundle (LSB-first within each byte)
//   src      : pointer to the field value (the sub-encoders pass &local_qword)
//   src_bit  : 0 at every latch call site
//   nbits    : field width in bits
```

Every V5+ latch field is emitted as `local = value; BitCopy(bundle, abs_bit, &local, 0, width)`. The function masks, shifts, and OR-merges the source bits across the spanning destination bytes (an AVX2 batched body for long copies, a scalar tail otherwise), preserving the bits outside `[dst_bit, dst_bit+nbits)`. This is why field order in a sub-encoder is irrelevant to the wire result: each `BitCopy` is an independent masked merge into a fixed bit window.

---

## Jellyfish — Monolithic VectorExtended Encode

### Purpose

Jellyfish (TPU v2) has no per-variant generated encoders. A single `EmitVectorLatch` maps the GLM to a `VectorExtendedOpcode`, stamps it plus the MXU number into the bundle's `VectorExtendedInstruction` proto, and `EncoderJf::EncodeVectorExtendedInstruction` later bit-packs that proto into the bundle. The slot lives in the JF 41-byte bundle's VectorExtended region (`@abs27-39`); this section adds the latch-specific occupants. (Dragonfish, the v3 gen, shares the JF emitter family — though `AddMxuNumToVectorExtended` gates its mxu_num proto write on `TpuVersionToDeviceIdentifiers == kDragonfishIdentifiers`, so on plain Jellyfish the field is left default.)

### Entry Point

```text
JellyfishEmitter::EmitVectorLatch          sub_140B8C20  ── GLM→VEopcode + emit
  └─ EmitVectorExtendedInstruction         sub_140B4F80  ── VEopcode → proto+0x60, Vs → proto+0x6c
  └─ AddMxuNumToVectorExtended             sub_140B8DA0  ── mxu_num → proto+0x70
        … (bundle finalize) …
        EncoderJf::EncodeVectorExtendedInstruction  sub_1E869F00  ── proto → bundle bits
```

### Algorithm

```c
function JellyfishEmitter::EmitVectorLatch(glm, vs_reg, mxu_num):   // sub_140B8C20
    if (glm >= 6) LogFatal("Unexpected Software latch mode.")       // jellyfish_emitter.cc:1647
    veopcode = dword_AEF42AC[glm]                                   // {7,10,9,12,8,11}
    EmitVectorExtendedInstruction(veopcode, vs_reg, /*imm=*/0)      // sub_140B4F80
    AddMxuNumToVectorExtended(mxu_num)                              // sub_140B8DA0

function EmitVectorExtendedInstruction(veop, vs, imm):              // sub_140B4F80
    ve = bundle.mutable_vector_extended()                           // proto submessage @bundle+0x50
    *(DWORD*)(ve + 0x60) = veop                                     // VEopcode field
    SetPredication(ve)
    *(DWORD*)(ve + 0x6c) = vs                                       // rotate-Vs / source reg

function AddMxuNumToVectorExtended(mxu):                            // sub_140B8DA0
    CheckMxuNum(mxu)
    *(DWORD*)(ve + 0x70) = mxu                                      // mxu_num field

function EncoderJf::EncodeVectorExtendedInstruction(ve, bundle):    // sub_1E869F00
    word = QWORD[bundle + 12]                                       // the VectorExtended bundle qword
    word = (word & ~(0x1F << 35)) | ((pred & 0x1F) << 35)           // predication @abs35-39
    word = (word & ~(0x3  << 27)) | ((ve.mxu_num & 3) << 27)        // mxu-id/GMR @abs27-28
    switch (ve.VEopcode):                                           // 35-case opcode→field switch
        case 0:  word = (word & 0xFFFFFFF81FFFFFFF) | 0x20000000    // field 1 @abs29-34 (no-op opcode)
        case 7:  word = (word & 0xFFFFFFF81FFFFFFF) | (0x9 << 29)   // ← LATCH GLM0 (bf16 NO_XPOSE)
        case 8:  word = (word & 0xFFFFFFF81FFFFFFF) | (0xa << 29)   // ← LATCH GLM4 (int8/S8)
        case 9:  word = (word & 0xFFFFFFF81FFFFFFF) | (0xb << 29)   // ← LATCH GLM2 (packed-bf16)
        case 10: word = (word & 0xFFFFFFF81FFFFFFF) | (0xd << 29)   // ← LATCH GLM1 (bf16 alt)
        case 11: word = (word & 0xFFFFFFF81FFFFFFF) | (0xe << 29)   // ← LATCH GLM5 (fp8-conv)
        case 12: word = (word & 0xFFFFFFF81FFFFFFF) | (0xf << 29)   // ← LATCH GLM3 (fp8 / E5M2)
        … (matmul / matres / EUP VEopcodes 1..6, 13..34) …
        default: LogFatal("Unknown opcode: ")                       // encoder_jf.cc:2570
    QWORD[bundle + 12] = word
```

The constants `<< 29` are the 6-bit opcode-field at `@abs29-34`; every populated case first clears the field (`& 0xFFFFFFF81FFFFFFF`, bits 29–34) then ORs in the case value. The field is *not* uniformly odd — `0xa` (GLM4) and `0xe` (GLM5) have bit 29 clear — so it is a plain 6-bit opcode, not an opcode-plus-fixed-valid-bit.

### The latch GLM → bundle opcode chain

Running the two tables in series — GLM through `dword_AEF42AC`, then the VEopcode through the encoder switch — gives the six JF latch opcodes:

| GLM | meaning | VEopcode | opcode-field `@abs29` |
|---|---|---|---|
| 0 | bf16 NO_XPOSE | 7 | `0x9` |
| 1 | bf16 alt | 10 | `0xd` |
| 2 | packed-bf16 | 9 | `0xb` |
| 3 | fp8 / E5M2 | 12 | `0xf` |
| 4 | int8 / S8 | 8 | `0xa` |
| 5 | fp8-conv | 11 | `0xe` |

> **NOTE — the six GLM opcode-fields are all distinct; the map is a bijection.** In `EncoderJf::EncodeVectorExtendedInstruction` (`0x1e869f00`) the `LABEL_55` cases (VEopcode 8, 11) add `0x20000000` on top of the same base the `LABEL_53` cases use, so VEopcode 8 encodes field `0xa` (not `0x9`) and VEopcode 11 encodes `0xe` (not `0xd`). The six JF latch opcode-fields are therefore `{0x9, 0xd, 0xb, 0xf, 0xa, 0xe}` — all distinct, a bijection over the six latch modes. It is easy to misread GLM0/GLM4 and GLM1/GLM5 as colliding (`0x9`/`0xd`); the `0x20000000` term is what separates them.

> **NOTE — the JF VectorExtended slot carries no separate format field.** Although the opcode field distinguishes all six latch GLMs, it is a single 6-bit field with no companion `MatmulDataFormat` like VF's `@abs51`. The dtype is implicit in the opcode value, so a JF decoder reconstructs the data type from the opcode field alone (`0x9`→bf16 NO_XPOSE, `0xa`→int8, …), not from a side field.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `JellyfishEmitter::EmitVectorLatch` | `sub_140B8C20` | GLM bound `< 6`, GLM→VEopcode lookup, emit chain | CONFIRMED |
| `JellyfishEmitter::EmitVectorExtendedInstruction` | `sub_140B4F80` | VEopcode → proto `+0x60`, Vs → proto `+0x6c` | CONFIRMED |
| `JellyfishEmitter::AddMxuNumToVectorExtended` | `sub_140B8DA0` | mxu_num → proto `+0x70` | CONFIRMED |
| `EncoderJf::EncodeVectorExtendedInstruction` | `sub_1E869F00` | 35-case VEopcode → bundle bits; pred `@abs35`, mxu-id `@abs27`, opcode `@abs29` | CONFIRMED |
| `dword_AEF42AC` | `.rodata` | 6-entry GLM→VEopcode table `{7,10,9,12,8,11}` | CONFIRMED |

> **NOTE — encoder reads MXU id from proto `+0x64`; the emitter writes it to `+0x70`.** The encoder reads the MXU id from proto `+0x64` (`*((DWORD*)ve + 25)`) while `AddMxuNumToVectorExtended` *writes* `mxu_num` to proto `+0x70`. The two reconcile as the emitter holding the submessage through an indirection while the encoder receives it directly; the unit-id → `@abs27-28` binding is byte-exact either way, but which protobuf field number occupies `+0x64` vs `+0x70` was not cross-checked against the proto descriptor. **MEDIUM** on the exact field-number layout; CONFIRMED on the bundle-bit binding.

---

## Pufferfish — PushGains Oneof Encode

### Purpose

Pufferfish (TPU v4) is the first generation with generated, per-variant encoders. `EmitVectorLatch` dispatches the GLM into a typed `PushGains{Rounded,Low,Hi,Packed,Byte}{,Transposed}{,Masked}` oneof inside the `TensorCoreVectorExtended0` (MXU0) or `TensorCoreVectorExtended1` (MXU1) submessage; a generated `Encode…<Variant>` sub-encoder then `BitCopy`s the fields. PF has a single MSR (no overrun handshake), so it emits no MSR bit.

### Entry Point

```text
PufferfishTensorCoreEmitter::EmitVectorLatch     sub_1410E1A0  ── switch(glm) → PushGains oneof
  ├─ DefaultConstruct<…_PushGainsRounded>                       (MXU0 slot, bundle flag 0x80)
  │   or DefaultConstruct<…1_PushGainsRounded>                  (MXU1 slot, bundle flag 0x100)
  └─ … 20 PushGains variants …
        EncodeTensorCoreVectorExtended0PushGainsRounded sub_1EDC1660  ── BitCopy fields → bundle
```

### Algorithm

`EmitVectorLatch` is a `switch(glm)` that, per GLM, claims an MXU slot and constructs the matching oneof. The MXU0/MXU1 choice keys on the bundle's populated-slot flags (`0x80` = MXU0 taken, `0x100` = MXU1 taken; both set ⇒ `"All vector extended slots occupied."`):

```c
function PufferfishTensorCoreEmitter::EmitVectorLatch(glm, vs, msr, mxu):  // sub_1410E1A0
    bundle = CurrentBundle()
    switch (glm):
        case 0: variant = PushGainsRounded            // oneof tag 104
        case 1: variant = PushGainsRoundedTransposed  // oneof tag 109
        case 2: variant = PushGainsHi                 // oneof tag 106
        case 3: variant = PushGainsHiTransposed       // oneof tag 111
        case 4: variant = PushGainsLow                // oneof tag 105
        … (GLM 5..13; 6..9 share an error branch) …
    ve = (bundle.mxu0_free()) ? bundle.mutable_tcve0()    // proto @bundle+0x50
                              : bundle.mutable_tcve1()    // proto @bundle+0x58
    SetPredicate(ve)
    ve.set_oneof(variant); ve.set_vsreg(vs)

function EncodeTensorCoreVectorExtended0PushGainsRounded(bundle, ve):   // sub_1EDC1660
    op = 0x20;  BitCopy(bundle, 91, &op, 0, 7)            // opcode @abs91 (7b)
    if ve.has_mode():    BitCopy(bundle, 89, &ve.mode,  0, 2)   // mode   @abs89 (2b)
    if ve.has_subop():   BitCopy(bundle, 83, &ve.subop, 0, 3)   // sub-op @abs83 (3b)
    if ve.has_op0():     BitCopy(bundle, 225, &ve.op0, 0, 5)    // register operands,
    if ve.has_op1():     BitCopy(bundle, 182, &ve.op1, 0, 5)    //   5b each, shared
    if ve.has_op2():     BitCopy(bundle, 152, &ve.op2, 0, 5)    //   operand pool
    if ve.has_op3():     BitCopy(bundle, 203, &ve.op3, 0, 5)
    if ve.has_op4():     BitCopy(bundle, 172, &ve.op4, 0, 5)
    // NOTE: predication is @abs98 (5b), written by the slot's shared pred path
```

### The PushGains opcode table (`@abs91`, 7-bit)

The 20 variants follow a regular structure: `opcode = 0x20 + variant + 8·transposed + 0x10·masked`, with `variant ∈ {Rounded:0, Low:1, Hi:2, Packed:3, Byte:4}`.

| Variant | base | +Transposed | +Masked | +Transp+Masked |
|---|---|---|---|---|
| Rounded | `0x20` | `0x28` | `0x30` | `0x38` |
| Low | `0x21` | `0x29` | `0x31` | `0x39` |
| Hi | `0x22` | `0x2a` | `0x32` | `0x3a` |
| Packed | `0x23` | `0x2b` | `0x33` | `0x3b` |
| Byte | `0x24` | `0x2c` | `0x34` | `0x3c` |

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `PufferfishTensorCoreEmitter::EmitVectorLatch` | `sub_1410E1A0` | `switch(glm)` → `PushGains*` oneof; MXU0/MXU1 slot select | CONFIRMED |
| `Encode…0PushGainsRounded` | `sub_1EDC1660` | `BitCopy` opcode `0x20@abs91`, mode `@abs89`, sub-op `@abs83`, operands | CONFIRMED |
| 19 sibling `Encode…0PushGains*` | `sub_1EDC1920`..`sub_1EDC4E00` | per-variant opcode `0x21..0x3c` `@abs91` | CONFIRMED |

> **NOTE — no MSR bit on Pufferfish.** PF has a single matrix-staging register and `HasMsrOverrunChecks()` is `FALSE` (see [latch assignment](latch-assignment-overrun.md#hasmsroverrunchecks--the-gen-level-coupling)). The `PushGains` sub-encoders write no MSR-select field; there are zero MSR-suffixed oneof types in the PF codec. The MSR-A/MSR-B distinction is a Viperfish-only feature.

---

## Viperfish — Pushmatrix Oneof and the MSR-Select Bit

### Purpose

Viperfish (TPU v5p) packs its MXU0 opcode into a 7-bit field at `@abs57-63` that a latch and a matmul *share*. A latch writes only the high 5 bits (`@abs59`) plus a 4-bit `MatmulDataFormat` (`@abs51`), leaving the low two bits for control; a matmul writes the full 7-bit opcode at `@abs57`, and its **LSB (`@abs57`) is the MSR-select bit**. This is the bundle-level encoding of the MSR-A/MSR-B overrun handshake — the only generation that has one, consistent with `HasMsrOverrunChecks` being `TRUE` only on Viperfish. The Viperfish 64-byte bundle's MXU slot map is on the [VF bundle page](../isa/bundle-vf-64b.md).

### Algorithm

The latch sub-encoder (bf16 shown) writes opcode-high, format, the two control bits, sub-fields, and operands, each via one `BitCopy`:

```c
function EncodeTensorCoreVectorExtended0PushmatrixBf16(bundle, ve):   // sub_1EFAF820
    hi = 14;  BitCopy(bundle, 59, &hi,  0, 5)             // opcode-high @abs59 (5b) = 14 (bf16)
    fmt = 3;  BitCopy(bundle, 51, &fmt, 0, 4)             // MatmulDataFormat @abs51 (4b) = 3 (Bf16)
    if ve.has_sub0():  BitCopy(bundle, 48, &ve.sub0, 0, 3)        // sub @abs48 (3b)
    if ve.has_sub1():  BitCopy(bundle, 55, &ve.sub1, 0, 2)        // sub @abs55 (2b)
    if ve.has_msr():   BitCopy(bundle, 57, &ve.msr,  0, 1)        // MSR-select @abs57 (1b), proto+0x20
    if ve.has_ctl():   BitCopy(bundle, 58, &ve.ctl,  0, 1)        // control    @abs58 (1b), proto+0x24
    // register operands @abs157/282/293/248/259/214/225/180 (6b each)
```

The matmul side proves the MSR bit. The U8 matmul comes in two encoders that differ *only* in the opcode value written at `@abs57`:

```c
function Encode…0MatrixMultiplyU8LgmrMsra(bundle, ve):   // sub_1EFA4A20
    op = 2;  BitCopy(bundle, 57, &op, 0, 7)              // full 7-bit opcode = 2 ⇒ bit57 = 0 (MSR-A)

function Encode…0MatrixMultiplyU8LgmrMsrb(bundle, ve):   // sub_1EFA4E00
    op = 3;  BitCopy(bundle, 57, &op, 0, 7)              // full 7-bit opcode = 3 ⇒ bit57 = 1 (MSR-B)
```

> **QUIRK — on Viperfish the latch dtype rides the format field, not the opcode.** Every non-masked `Pushmatrix*` variant writes opcode-high `14` at `@abs59`; the data type lives entirely in the 4-bit `MatmulDataFormat` at `@abs51`. Only the *masked* variants bump the opcode-high to 15..23. So a decoder that reads `@abs59` to distinguish bf16 from int8 sees `14` for both and must consult `@abs51`. This is the inverse of the JF slot, which carries the dtype (partially) in the opcode field and has no format field.

### Pushmatrix opcode + format per dtype

| Oneof | op-high `@abs59` | format `@abs51` | Masked oneof | op-high `@abs59` |
|---|---|---|---|---|
| PushmatrixRounded | 14 | 0 | RoundedMasked | 15 |
| PushmatrixPackedIf8Conv | 14 | 2 | PackedIf8ConvMasked | 17 |
| PushmatrixBf16 | 14 | 3 | Bf16Masked | 18 |
| PushmatrixBf8 | 14 | 4 | Bf8Masked | 19 |
| PushmatrixU8 | 14 | 5 | U8Masked | 20 |
| PushmatrixS8 | 14 | 6 | S8Masked | 21 |
| PushmatrixU4 | 14 | 7 | U4Masked | 22 |
| PushmatrixS4 | 14 | 8 | S4Masked | 23 |

The MXU1 (`VectorExtended1`) slot is the MXU0 layout shifted down 20 bits: opcode-high `@abs39`, format `@abs31`, MSR `@abs37`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `Encode…0PushmatrixBf16` | `sub_1EFAF820` | latch: op `14@abs59`, fmt `3@abs51`, MSR `@abs57`, ctl `@abs58`, operands | CONFIRMED |
| 15 sibling `Encode…0Pushmatrix*` | `sub_1EFAE520`..`sub_1EFB2C40` | per-dtype op `14`/masked `15..23`, fmt `0..8` | CONFIRMED |
| `Encode…0MatrixMultiplyU8LgmrMsra` | `sub_1EFA4A20` | matmul opcode `2@abs57` (bit57=0, MSR-A) | CONFIRMED |
| `Encode…0MatrixMultiplyU8LgmrMsrb` | `sub_1EFA4E00` | matmul opcode `3@abs57` (bit57=1, MSR-B) | CONFIRMED |

> **NOTE — bit 57 ties the encode side to the [latch-assignment overrun gate](latch-assignment-overrun.md) and the VF cost model.** The MSR-A/MSR-B choice the matmul carries at `@abs57` is the same handshake whose extra reservation the Viperfish cost model charges as `{Msr:2/6}` (see [MatmulMode and Modifiers](../cost/matmul-mode-modifiers.md)). The latch's MSR field (`BYTE[op+0x44]`) determines which bank a consuming matmul reads, and so which `…Msra`/`…Msrb` matmul encoder fires — i.e. the value of bit 57. The first-latch index that `SetLatchIndices` assigns only on Viperfish wide formats is the scheduling-side decision; bit 57 is its bundle-level result.

> **GOTCHA — the `@abs58` control bit role is not pinned. (LOW)** The VF latch writes a second 1-bit field at `@abs58` from proto `+0x24`, adjacent to the MSR bit at `@abs57`. It is a distinct per-latch control flag, but whether it arms the overrun handshake, selects a second MSR, or confirms transpose was not isolated to a named field. The `@abs57` MSR-select is CONFIRMED (via the matmul Msra/Msrb LSB); `@abs58` is **LOW**.

---

## Ghostlite / `6acc60406` — PushMatrix Oneof

### Purpose

Ghostlite (v6e, `glc`) and `6acc60406` (TPU7x, `gfc`) use the same oneof-typed shape as Viperfish with `PushMatrix*{,Masked}` variants and, like VF, an Msra/Msrb matmul family. The dtype roster differs between the two: `glc` names its variants `{F32, Bf16, Bf8, If8, S4, S8, U4, U8}`, while `gfc` names them `{F32, Bf16, E4m3, E5m2, …}` — i.e. the fp8 modes are spelled by encoding (`E4m3`/`E5m2`) on `gfc` rather than by role (`Bf8`/`If8`). Only the bf16 latch opcode bit base and value were traced cell-by-cell.

### Encoding

| Gen | bf16 latch encoder | opcode bit / width | opcode value (bf16) |
|---|---|---|---|
| Ghostlite (v6e, `glc`) | `PushMatrixBf16` `sub_1F33FE00` | `@abs60` (6b) | `14` (`0xe`) |
| `6acc60406` (TPU7x, `gfc`) | `PushMatrixBf16` `sub_1F9A14E0` | `@abs64` (6b) | `14` (`0xe`) |

The dtype is the oneof type; the opcode value is the shared matpush constant `14` on both. The `glc`→`gfc` bit base drifts `+4` bits. The full latch-slot field roster (pred, format, MSR positions) beyond the opcode bit base was not traced for either gen. **MEDIUM** beyond the opcode bit/value.

---

## The Type-4 Gain-Register Guard

### Purpose

Before any latch op is built, `CreateVectorLatchLsf` checks that the *gain source* — the LloValue feeding the LSF latch — produces a register of type 4 (the vector class). The check is the gate that decides whether the latch takes the fast build path or records a diagnostic; it is the encode-pipeline analog of the assignment-side guards and the reason a latch's `register_number` (`BYTE[op+0x0a]`) is a Vregno.

### Algorithm

```c
function LloInstruction::CreateVectorLatchLsf(gain_src, glm, unit_id, region):  // sub_1D4D7AA0
    op_code = WORD[gain_src]                                  // the gain SOURCE opcode
    if (op_code >= 0x1CD) trap                                // bound 461 (ud1)
    while (opcode_produced_register_type[op_code] != 4):      // .data table @0x223a16c0
        cf = new CheckFailer()
        diag = make_unique<StatusWrapper>(cf, "chunk->ProducesVreg()",
                                          {line 1073, llo_instruction.cc})
        LloModule::UpdateStatus(region.module, diag)          // slow diagnostic path
        // loop re-checks after the status is recorded
    if (glm > 0x33 || !bittest(0xF0000003C0C03, glm)):
        LogFatal("LSF latch mode not expected.")              // llo_instruction.cc:1089
    op = LloInstruction::New(0x8d /*kVectorLatchLsf*/, {gain_src}, region)
    set_latch_mode(op, glm)                                   // BYTE[op+0x40] = glm
    set_matrix_staging_register(op, 1)                        // BYTE[op+0x44] = 1 (LSF staging slot)
    ValidateAndSetMxuAndSourceBus(unit_id, op)                // WORD[op+0x0b] unit-id
    return op
```

`opcode_produced_register_type` (`@0x223a16c0`, `.data`; 461 one-byte entries for indices 0..460 — the `>= 0x1CD` bound, within a 464-byte symbol span) is indexed by the *producer* opcode and uses the taxonomy `{0=none, 1=predicate, 2=scalar, 3=vector-mask, 4=vector}`. The latch opcodes `0x8d..0x96` themselves map to type 0 — they have no destination register; they push into the systolic array. The guard therefore reads the entry for the *gain source*, not the latch. The same table-read appears in the [Matprep/IAR/Latch ISA page](../isa/slot-matprep-iar-latch.md), which models the matprep source guard as `opcode_produced_register_type[source.opcode] == 4`.

### Why register-type 4

Type 4 is the bulk vector register class. The gain matrix a latch loads into the MXU must reside in a vector register, so the producer of the latch's operand has to be a vector-producing op — load, scalar-to-vector, IAR read, and the like. If the gain source is instead a scalar (type 2), mask (type 3), or predicate (type 1) producer, the guard records a `chunk->ProducesVreg()` diagnostic via `UpdateStatus` (the `StatusWrapper` slow path) rather than aborting outright.

> **QUIRK — the guard is a status-recording loop, not a hard FATAL.** Unlike the GLM-validity check below it (which `LogFatal`s on an unexpected latch mode), the type-4 mismatch path allocates a `StatusWrapper`, calls `LloModule::UpdateStatus`, and re-checks in a loop — recording a register-class diagnostic on the module while still proceeding to build the op. A reimplementation that turns the type-4 mismatch into a hard error diverges from the binary's softer, status-accumulating behavior.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LloInstruction::CreateVectorLatchLsf` | `sub_1D4D7AA0` | gain-source bound, type-4 guard, slow path, field stamp | CONFIRMED |
| `opcode_produced_register_type` | `@0x223a16c0` (`.data`) | 461-entry (1 byte each) producer→reg-type table; `4`=vector | CONFIRMED |
| `set_latch_mode` | `sub_1D4D7C20` | `BYTE[op+0x40] = glm` | CONFIRMED |
| `set_matrix_staging_register` | `sub_1D4D7D40` | MSR opcode-mux: latch→`+0x44`, matmul→`+0x46`, load-LMR→`+0x42`, dwg→`+0x41` | CONFIRMED |
| `set_latch_index_in_sequence` | `sub_1D4E7960` | `WORD[op+0x42] = index`, bound `≤ 0xFFFF` | CONFIRMED |

> **NOTE — the latch_index field is *not* a per-latch bundle bit.** `set_latch_index_in_sequence` (`sub_1D4E7960`) stores the assigned ordinal at `WORD[op+0x42]` (re-checking `LloOpcodeIsVectorLatch` at `llo_instruction.cc:3399` and bounding `index <= 65535` at `:3400`), but no encoder blits that ordinal into the bundle. The index is a *scheduling* artifact consumed before encode: on Viperfish the first-latch index gates whether the overrun handshake fires, which surfaces in the bundle only indirectly as the MSR-select bit `@abs57`. The wire bundle carries opcode, format, unit-id, and MSR — never the sequence index.

---

## The LLO → Proto → Bundle Field Correspondence

For one latch op, the three representations line up as:

| LLO field (assignment side) | Proto field | Bundle bit (per gen) |
|---|---|---|
| `GainLatchMode` `BYTE[op+0x40]` | `VEopcode` / oneof type | JF opcode `@abs29-34`; PF op `@abs91`; VF op `@abs59` + fmt `@abs51`; GXC op `@abs60`(glc)/`@abs64`(gfc) |
| unit-id/GMR `WORD[op+0x0b]` bits 8–9 | mxu-num (JF proto `+0x70`) | JF `@abs27-28` |
| MSR `BYTE[op+0x44]` | Msra/Msrb oneof (VF) | VF `@abs57` (MSR-select); PF/JF: none |
| `register_number` `BYTE[op+0x0a]` | gain-source Vregno | the type-4-guarded vector reg selector |
| `latch_index_in_sequence` `WORD[op+0x42]` | (assignment only) | not a per-latch bundle bit; gates VF `@abs57` indirectly |

---

## Related Components

| Component | Relationship |
|---|---|
| [Latch Assignment & Overrun](latch-assignment-overrun.md) | the assignment pass that fills `BYTE[op+0x40]`/`WORD[op+0x42]`; this page is its encode-side consumer |
| [MxuSequence / SequenceInfo](mxu-sequence-struct.md) | the sequence record whose latches are encoded here |
| [Matprep, IAR, and Latch Sub-Slots](../isa/slot-matprep-iar-latch.md) | the latch-op LLO field layout and the matprep type-4 source guard from the ISA side |
| [MXU Slot](../isa/slot-mxu.md) | the systolic-array matmul slot the latched gains feed |
| [Viperfish 64-Byte Bundle](../isa/bundle-vf-64b.md) | the VF bundle whose MXU0 7-bit opcode field `@abs57-63` this page splits |
| [Pufferfish 51-Byte Bundle](../isa/bundle-pf-51b.md) | the PF bundle whose MXU0 slot holds the `PushGains` opcode `@abs91` |
| [Jellyfish 41-Byte Bundle](../isa/bundle-jf-41b.md) | the JF bundle whose VectorExtended slot holds the latch opcode `@abs29-34` |

## Cross-References

- [Latch Assignment & Overrun](latch-assignment-overrun.md) — assigns the latch indices and GLMs this page serializes; the source of `BYTE[op+0x40]`, `WORD[op+0x42]`, `BYTE[op+0x44]`.
- [MxuSequence / SequenceInfo](mxu-sequence-struct.md) — the `MxuSequence` record and `LatchLhs` partition that produce the latch ops.
- [Matprep, IAR, and Latch Sub-Slots](../isa/slot-matprep-iar-latch.md) — the latch-op `LloInstruction` field offsets and the matprep `opcode_produced_register_type == 4` source guard, the sibling of the gain guard here.
- [MXU Slot](../isa/slot-mxu.md) — the matmul op family whose Msra/Msrb encoders set the VF `@abs57` MSR-select bit.
- [Viperfish 64-Byte Bundle](../isa/bundle-vf-64b.md) — the VF MXU0/MXU1 slot map this page's latch fields occupy.
- [Pufferfish 51-Byte Bundle](../isa/bundle-pf-51b.md) — the PF MXU0 slot map; the `PushGains` opcode `@abs91`.
- [Jellyfish 41-Byte Bundle](../isa/bundle-jf-41b.md) — the JF VectorExtended slot map; the latch opcode-field `@abs29-34`.
- [Route-Cache Codec](../routing/route-cache-codec.md) — the reader half of the `gloop` `BitDecoder`/`BitEncoder` bit-codec toolkit shared with this page's writer primitives.
- [MatmulMode and Modifiers](../cost/matmul-mode-modifiers.md) — the Viperfish `{Msr:2/6}` overrun-check reservation; the cost-side consumer of the MSR-select bit decoded here.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
