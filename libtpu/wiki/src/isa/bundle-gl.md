# Ghostlite Bundle

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

Ghostlite (`TpuVersion::kGhostlite` = 4, external name **"TPU v6 lite"** / v6e) is the second-newest TensorCore generation in this build and the **named** half of the [GXC family](../targets/gxc-family.md). Its ISA lives in the `asic_sw::deepsea::gxc::glc` (general **load**-core) sub-namespace, and — unlike its [6acc60406 / `gfc`](bundle-gf.md) sibling — every layer of it is fully symbol-named: a `TpuCodecGhostlite` codec (`CreateTpuCodecGhostlite` @ `0x1e83bce0`, vtable `0x21d35c00`), a `GhostliteCodecMetadata`, named `ghostlite::isa::EncoderGl{TensorCore,SparseCoreScs,SparseCoreTac,SparseCoreTec}` workers, a `GlcCycleTable` cost model, and a dedicated `GetGhostliteInstruction` opcode→cost-row remap.

The Ghostlite TensorCore bundle is a **64-byte (512-bit) VLIW issue word** — the same width as [Viperfish](bundle-vf-64b.md) and [6acc60406](bundle-gf.md), confirmed by `GhostliteCodecMetadata::BundleSizeBytes` @ `0x1eeb7640` returning `0x40` and by `EncoderGlTensorCore::BundleSizeBytes` @ `0x1d332540`. What makes it a distinct wire format is not its width but its slot bit layout: relative to Viperfish, Ghostlite *widens* every opcode field 7→8 bits and shifts the TensorCore scalar/sequencer/immediate region uniformly **+3 bits** higher in the buffer; relative to 6acc60406, it keeps the per-slot 4+1-bit predicate (no dedicated dual-predicate slot) and retains the full **8-dtype** MXU (four float + four integer) where `gfc` drops integers. This page documents the `glc` layout to reimplementation grade: the complete slot map with absolute byte/bit offsets, how it diffs from Viperfish (the `GetGhostliteInstruction` opcode→GhPerf-row remap, the GL-specific iar-class jump-table arms, the MXU contracting-depth feed), and the `EmitX`/`EncodeGl*` templates specific to GL.

Like every V5+ generation, the Ghostlite bundle bytes are produced entirely by the proto-bundle `<Slot>Encoder::Encode` codecs — the default LLVM-MC `InstBits` table is all-zero for this generation. Each encoder reads its typed `glc::isa` proto sub-message and calls the universal bit-packer `BitCopy(dst, dst_bit, src, src_bit, nbits)` @ `0x1fa0a900` to place each field at a fixed absolute bit in the 64-byte buffer. The orchestrator `EncoderGlTensorCore::EncodeBundle` @ `0x1d331d00` walks the per-slot encoders in template-argument order; the `glc`-namespace encoder set IS the generation's effective `kIsaTable`. For the model context (what a bundle is, the no-scoreboard VLIW contract, the empty-slot `kNeverExecute` convention), see [Bundle Model](bundle-model-overview.md).

For reimplementation, the contract is:

- The 64-byte width and that it is selected through the **named** `TpuCodecGhostlite` codec (case 4 of `TpuCodec::Create`), with a named `GhostliteCodecMetadata` and named `EncoderGl*` workers.
- The `glc` slot offsets: each functional slot is a contiguous bit window in the 512-bit buffer, populated by `BitCopy` from a typed proto sub-message, with the per-slot 4-bit predicate register index + 1-bit inversion at the slot's high end and the opcode discriminator below it.
- The two deltas vs Viperfish: (1) the opcode field **widens 7→8 bits** in the MXU and VALU slots; (2) the TensorCore scalar/sequencer/immediate region sits **+3 bits** higher (e.g. TC branch offset at bit 433, not Viperfish's 430).
- The two deltas vs 6acc60406: (1) Ghostlite keeps the **per-slot 4+1-bit predicate** (no `TensorCorePredicates` dual-predicate slot); (2) Ghostlite retains the **8-dtype** integer+float MXU and a dedicated `PopAddMxu01Result` fused-accumulate result op.
- The decode-side inverse: `glc::isa::...Decoder::Decode` reads the bytes back via per-field `GetConcatenatedValue` accessors and per-dtype `Opcode::Matches` mask predicates.

| | |
|---|---|
| **TpuVersion** | `kGhostlite` = **4** (external "TPU v6 lite" / v6e); see [Codename Matrix](../targets/tpu-version-codename-matrix.md) |
| **Sub-core ISA** | `asic_sw::deepsea::gxc::glc::isa` (general **load**-core); see [GXC Family](../targets/gxc-family.md) |
| **Bundle width** | **64 B / 512 bit** — `GhostliteCodecMetadata::BundleSizeBytes` @ `0x1eeb7640` `return 0x40` |
| **Codec** | **named** — `TpuCodecGhostlite` (vtable `0x21d35c00`); `CreateTpuCodecGhostlite` @ `0x1e83bce0` |
| **Encode orchestrator** | `EncoderGlTensorCore::EncodeBundle` @ `0x1d331d00` (`TpuCodecGhostlite::EncodeBundle` @ `0x1e83c780`) |
| **Bit packer** | `BitCopy(dst, dst_bit, src, src_bit, nbits)` @ `0x1fa0a900` (LSB-first, bit-granular) |
| **Cost remap** | `GetGhostliteInstruction` @ `0x1c8b1740` (LloOpcode → GhPerf::Instruction); `GlcCycleTable` @ `0x1c89e7e0` |
| **MXU dtype set** | `{F32, If8, Bf16, Bf8}` (float) + `{U8, S8, U4, S4}` (int) — 8 total |
| **Predicate model** | per-slot 4-bit reg index + 1-bit inversion (no dedicated predicate slot) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Named Codec and the `EncoderGl*` Workers

Ghostlite is the last generation in this build that is reified as a fully-named C++ codec. `TpuCodec::Create` (`0x1e835fa0`) is a `switch(TpuVersion)` whose case 4 calls the demangled `CreateTpuCodecGhostlite` (`0x1e83bce0`), installing the named vtable `tpu::TpuCodecGhostlite` @ `0x21d35c00`. (Its `gfc` sibling — case 5 — is the only *anonymous* codec; see [6acc60406 Bundle](bundle-gf.md#the-anonymous-codec-sub_1e838380).)

`TpuCodecGhostlite::EncodeBundle` (`0x1e83c780`) dispatches by `TpuSequencerType` to one of four named encoder workers, all in the `ghostlite::isa` namespace and all binding to the `gxc::glc::isa` proto types:

```text
ghostlite::isa::EncoderGlTensorCore      @ 0x1d331d00   (EncodeBundle)   ; TensorCore sequencer
ghostlite::isa::EncoderGlSparseCoreScs                                    ; SparseCore scalar sub-bundle
ghostlite::isa::EncoderGlSparseCoreTac                                    ; SparseCore tile-access core
ghostlite::isa::EncoderGlSparseCoreTec                                    ; SparseCore tensor-engine core
```

The `EncodeProgram<ghostlite::isa::EncoderGlTensorCore, gxc::glc::isa::TensorCoreProgram>` instantiation (`0x1e83cca0`) is the proof of binding: the `EncoderGl*` template names the codename (`Gl`), but its proto argument is `asic_sw::deepsea::gxc::glc::isa::TensorCoreProgram`, the *load*-core ISA. A single factory `CreateEncoderGlGf` (`0x1e831020`) constructs both the Ghostlite and 6acc60406 sequencer encoders from a shared `TpuSequencerProgramProto`; the `glc`-vs-`gfc` split is in the template arguments, not at the leaf.

> **NOTE — `glc` is the Ghostlite sub-core, not 6acc60406's.** A naming hazard: `glc` looks adjacent to `gfc`, but [GXC Family](../targets/gxc-family.md) pins **Ghostlite (v4) = `glc`** (general load-core) and **6acc60406 (v5) = `gfc`** (general fetch-core). Throughout this page, every `glc::*` symbol is the Ghostlite generation. The external name of this generation is **"TPU v6 lite"** (v6e); do **not** read `glc` as "TPU7x" — that is the `gfc` / 6acc60406 generation.

The width is byte-anchored twice over. `GhostliteCodecMetadata::BundleSizeBytes` @ `0x1eeb7640` for the TensorCore sequencer is a two-instruction leaf:

```text
0x1eeb7644:  mov eax, 0x40      ; 64
0x1eeb7649:  ret
```

and `EncoderGlTensorCore::BundleSizeBytes` @ `0x1d332540` returns the same 64 via its codec-metadata pointer. So the Ghostlite TensorCore bundle is 64 bytes — confirming the [Bundle Model](bundle-model-overview.md) claim that v5e / v6e / v7x all share the 64-byte issue word and differ only in slot layout.

---

## The Encode Path

The encode path is the universal two-stage V5+ chain:

```text
LLO op ──(EmitX, proto population)──▶ glc proto sub-message (present-bit + fields)
glc proto sub-message ──(<Slot>Encoder::Encode, BitCopy)──▶ absolute bit window in 64-B buffer
```

`EncoderGlTensorCore::EncodeBundle` @ `0x1d331d00` is the orchestrator: it invokes each slot's `glc::isa::...Encoder::Encode` against the shared 64-byte buffer in template-argument order. Every field write is a `BitCopy(buffer, dst_bit, &field, 0, width)` call to `0x1fa0a900` — bit-granular, **LSB-first** (bit 0 is the least-significant bit of byte 0, the universal v5+ convention stated on the [Bundle Model](bundle-model-overview.md#abstract); there is no MSB-first ordering anywhere in the encode path), where `dst_bit` is the absolute bit in the bundle (byte = `dst_bit >> 3`, bit-in-byte = `dst_bit & 7`). The tables below give the `(dst_bit, width)` triple for every field, each verified from the literal `mov esi,<dst_bit>` / `mov r8d,<width>` immediates preceding the `BitCopy` call in the named encoder.

---

## Slot Map — Absolute Bit Offsets (64-byte / 512-bit buffer)

The bundle partitions into the standard V5+ slot classes. The table below is the consolidated Ghostlite (`glc`) slot map; bit 0 is the LSB of byte 0. Each entry is anchored to the named `glc::isa::...Encoder::Encode` that writes it and the `BitCopy` immediate that fixes the offset.

| Slot / field | dst_bit (dec) | hex | width | Encoder (`glc::isa`) @ | Confidence |
|---|---:|---|---:|---|---|
| **Sequencer** per-slot pred reg index | 502 | 0x1f6 | 4 | `TensorCoreScalarAlu0Encoder::Encode` @ `0x1f219b40` | CONFIRMED |
| seq pred inversion | 506 | 0x1fa | 1 | (same) | CONFIRMED |
| seq opcode-HIGH / family | 496 | 0x1f0 | 6 | (same; branch helpers @ `0x1f21da40`+) | CONFIRMED |
| seq opcode-LOW / discriminator | 491 | 0x1eb | 5 | (same) | CONFIRMED |
| **Immediate** slot 0 (branch/call/sync offset) | 433 | 0x1b1 | 20 | `TensorCoreImmediatesEncoder::Encode` @ `0x1f20d520` | CONFIRMED |
| imm slot 1 | 413 | 0x19d | 20 | (same) | CONFIRMED |
| imm slot 2 | 393 | 0x189 | 20 | (same) | CONFIRMED |
| imm slot 3 | 373 | 0x175 | 20 | (same) | CONFIRMED |
| imm slot 4 | 353 | 0x161 | 20 | (same) | CONFIRMED |
| imm slot 5 | 333 | 0x14d | 20 | (same) | CONFIRMED |
| **VALU slot 0** opcode | 302 | 0x12e | 7 | `TensorCoreVectorAlu0Encoder` | HIGH |
| VALU0 predicate (4-bit reg) | 309 | 0x135 | 4 | (same) | HIGH |
| **MXU VEx0** opcode-HIGH | 58 | 0x3a | 8 | `TensorCoreVectorExtended0Encoder::Encode` @ `0x1f32fd00` | CONFIRMED |
| VEx0 data-format sub-disc | 52 | 0x34 | 4 | (same) | CONFIRMED |
| VEx0 MXU-id (unit) | 66 | 0x42 | 4 | (same) | CONFIRMED |
| VEx0 control (matpush target) | 49 | 0x31 | 3 | `…MatrixMultiplyBf16` @ `0x1f333ce0` | CONFIRMED |
| VEx0 done-gains / latch flag | 56 | 0x38 | 1 | (same) | CONFIRMED |
| VEx0 systolic src vreg #1 (proto +0x20) | 160 | 0xa0 | 6 | (same) | CONFIRMED |
| VEx0 systolic src vregs #2..#8 (proto +0x24..+0x3c) | 285/296/251/262/217/228/183 | — | 6 ea | (same) | CONFIRMED |
| **EUP push** (VALU slot 3) VALU-opcode | 200 | 0xc8 | 7 | `…VectorAlu3F32Tanh` @ `0x1f2f4f40` (family) | CONFIRMED |
| EUP function selector | 189 | 0xbd | 5 | (same) | CONFIRMED |
| EUP push src vreg | 194 | 0xc2 | 6 | (same) | CONFIRMED |
| **Result slot** result-type discriminator | 24 | 0x18 | 4 | `TensorCoreVectorResult0Encoder::Encode` @ `0x1f3bc160` | CONFIRMED |
| result dest vreg | 14 | 0x0e | 6 | (same) | CONFIRMED |

> **NOTE — the VALU slot rows are HIGH, not CONFIRMED.** The VALU0/VALU3 opcode and predicate offsets (302/309 for VALU0) are carried from the cross-confirmed VALU-slot trace (the `TensorCoreVectorAlu0Encoder` family) rather than re-walked field-by-field on this page; they are reported HIGH. The MXU, sequencer, immediate, EUP, and result offsets above were each re-walked from the named `glc::isa` encoder's `BitCopy` immediates and are CONFIRMED. All eight MXU systolic source-vreg fields (proto `+0x20..+0x3c`) were re-walked from `MatrixMultiplyBf16` @ `0x1f333ce0`: they land at bits **160 / 285 / 296 / 251 / 262 / 217 / 228 / 183** (w6 each), in proto-field order — note the offsets are *non-monotone* in the buffer (160 then jumps to 285, descends, and the last field +0x3c lands at 183). The corresponding `gfc` pool is documented on the [6acc60406 page](bundle-gf.md#slot-map--absolute-bit-offsets-64-byte--512-bit-buffer).

---

## Diff Against Viperfish — the `+3` Shift and the 7→8 Opcode Widening

Ghostlite is, field-for-field, a Viperfish bundle with two systematic transformations. Both are visible in the binary as constant offsets between the `vxc` and `glc` encoders.

### Transformation 1 — TensorCore scalar/sequencer/immediate region shifts +3 bits up

Every field in the TensorCore *scalar* region (sequencer opcode, predicate, immediate slots) sits exactly **+3 bits** higher on Ghostlite than on Viperfish. This is uniform and internally consistent:

| Field | Viperfish (`vxc`) | Ghostlite (`glc`) | Δ | Confidence |
|---|---:|---:|---:|---|
| TC imm slot 0 (branch offset) | bit 430 (0x1ae) | bit 433 (0x1b1) | +3 | CONFIRMED |
| TC imm slots 1..5 | 410/390/370/350/330 | 413/393/373/353/333 | +3 | CONFIRMED |
| TC seq opcode-HIGH / family | bit 493 (0x1ed) | bit 496 (0x1f0) | +3 | CONFIRMED |
| TC seq opcode-LOW discriminator | bit 488 (0x1e8) | bit 491 (0x1eb) | +3 | CONFIRMED |
| TC seq predicate reg index | bit 499 (0x1f3) | bit 502 (0x1f6) | +3 | CONFIRMED |
| TC seq predicate inversion | bit 503 (0x1f7) | bit 506 (0x1fa) | +3 | CONFIRMED |

These positions are read directly from `glc::isa::TensorCoreScalarAlu0Encoder::Encode` (`0x1f219b40`) and its branch helper `EncodeTensorCoreScalarAlu0BranchAbsolute` (`0x1f21da40`). The Ghostlite TC scalar region is uniformly **+3 bits** above Viperfish, matching the TC immediate-slot +3 (433 vs 430): the whole TC scalar/sequencer/immediate block translates as one rigid window. The SparseCore SCS sequencer is *not* shifted — there `glc` is byte-identical to `vxc` (see below).

The **SparseCore SCS** sequencer, by contrast, is byte-identical between the two generations — the +3 shift is a TensorCore-only phenomenon:

```text
glc SparseCoreScalarAlu0Encoder::Encode @ 0x1e9d2140:
  predication reg index  -> bit 187 (0xbb) w4   ; = vxc
  predication inversion  -> bit 191 (0xbf) w1   ; = vxc
  opcode-HIGH / family   -> bit 181 (0xb5) w6   ; = vxc
  opcode-LOW disc        -> bit 176 (0xb0) w5   ; = vxc
glc SCS imm slot 0 (branch offset) -> bit 67 (0x43) w20   ; = vxc (SparseCoreImmediatesEncoder @ 0x1eb563c0)
```

### Transformation 2 — opcode fields widen 7→8 bits

The MXU and VALU opcode fields gain one bit on Ghostlite. The Viperfish MXU `VectorExtended` opcode is 7-bit @ bit 57; the Ghostlite one is **8-bit @ bit 58**. This mirrors the VALU-slot opcode widening across the same generation pair (Viperfish VALU0 opcode 7-bit @ bit 299; Ghostlite 7-bit @ bit 302 — note the VALU0 opcode stays 7-bit on `glc` and only becomes 8-bit on `gfc`).

| MXU `VectorExtended` field | Viperfish (`vxc`) | Ghostlite (`glc`) | Confidence |
|---|---:|---:|---|
| opcode-HIGH | bit 57 w7 | **bit 58 w8** | CONFIRMED |
| data-format sub-disc | bit 51 w4 | bit 52 w4 | CONFIRMED |
| MXU-id (unit) | bit 64 w4 | bit 66 w4 | CONFIRMED |
| opcode bound (`cmp`) | `0x66` (103 ops) | **`0x70` (113 ops)** | CONFIRMED |

The wider opcode and the larger jump-table bound (113 vs 103) are how Ghostlite's MXU slot encodes its extra operations within the same 64-byte word.

---

## The `GetGhostliteInstruction` Opcode→Row Remap (Cost Model)

Ghostlite is the only generation in this build with a named, dedicated LloOpcode→cost-row translator: `xla::ghostlite::(anonymous namespace)::GetGhostliteInstruction` @ `0x1c8b1740`. It maps an `LloOpcode` (0..0x1cc) to a `GhPerf::Instruction` index (0..0x1db) — a **distinct, denser** enum that the `GlcCycleTable` cost grid is indexed by. This is the reason the LLO opcode is *not* directly the cost-grid row.

The mechanism, decompiled:

```text
GetGhostliteInstruction(LloValue*)  @ 0x1c8b1740:
  1. opcode = WORD[value]                                       ; movzx ecx,WORD[rbx]
  2. binary search a SORTED 258-entry table @ 0x4067dc8         ; mov edi,0x102 (=258)
     (key = LloOpcode WORD, value = GhPerf::Instruction WORD)   ; lower_bound; on hit, return value
  3. miss -> lea edx,[opcode-1]; cmp edx,0xa5; jump table @ 0xb43b34c (166 entries)
       data-format / mode-dependent arms compute the row from a secondary key:
         Latch* opcodes   -> LloInstruction::latch_mode()        @ 0x1d4e7500
         Matmul* opcodes  -> LloInstruction::matmul_data_format() @ 0x1d4e8440
         Transpose 0xa6   -> LloInstruction::vxpose_mode()        @ 0x1d4e7440 (2nd jt @ 0xb43b5e4)
         iar-class ops    -> LloInstruction::iar()                @ 0x1d4e7120   (FIVE arms)
  4. any opcode neither in the table nor a special arm -> absl::LogMessageFatal (latency_table_gl.cc:761)
```

The `kLloOpcodeToGlcInstruction` binary-search table @ `0x4067dc8` (258 entries, anchored in [GXC Family](../targets/gxc-family.md#compiler-target-and-cost-model-binding)) is the literal LloOpcode→GhPerf map; the remap is total over the valid Ghostlite opcode set and aborts otherwise.

### The GL-specific iar-class jump-table arms

The single most Ghostlite-specific aspect of the remap is the **iar-class** (index-address-register) handling. Five distinct arms of the `0xb43b34c` jump table — for the iar-class LLO opcodes — route through `LloInstruction::iar()` @ `0x1d4e7120` to compute the GhPerf row from the instruction's IAR operand rather than its opcode alone. The `glc::isa` operand-variant set confirms the IAR operand class is a first-class field of the Ghostlite scalar slots: the `FormatSlot<glc::isa::TensorCoreScalarAlu>` mnemonic variant carries an `IarNumber` strong-int alternative alongside `Sreg`/`Vreg`/`Preg`. The matmul/latch/transpose arms key on `matmul_data_format` / `latch_mode` / `vxpose_mode` — the same modifier keys the MXU latency table uses, so the cost-row selection and the MXU reservation both pivot on the data format.

> **NOTE — shared GhPerf rows.** The remap collapses many LloOpcodes to one GhPerf row: e.g. all 14 HBM-DMA opcodes map to one row and all 14 VMEM/SMEM-DMA opcodes to another, so the cost model prices an entire DMA-direction family with a single grid row. The `GhPerf::Instruction` enum is gen-invariant between Ghostlite and 6acc60406; the per-gen difference is only the grid row count (476 for Ghostlite). The default-latency sentinel is `0xffffffff` = −1 (signed), structurally dominated in the `LatencyBetween` signed-max so it never surfaces as a cost. Full cost-model detail is on [MXU Latency: GL](../cost/mxu-latency-gl.md).

---

## The MXU Slot — 8 Dtypes and the Contracting-Depth Feed

The Ghostlite MXU slot is two `VectorExtended` slots (VEx0, VEx1), one per physical MXU, each a `BitCopy`-packed per-op helper dispatched through the slot encoder's jump table. The slot encodes the dense matmul step, the moving-operand push (matpush), and the weight latch.

### Dtype set: float + integer (8 total)

Ghostlite supports **eight** matmul/push dtypes — the symbol census of the `PushMatrix*` helper roster is definitive:

| Generation | PushMatrix / MatrixMultiply dtype set | Confidence |
|---|---|---|
| **Ghostlite (`glc`)** | `F32`, `If8`, `Bf16`, `Bf8` (float) + `U8`, `S8`, `U4`, `S4` (int) — **8** | CONFIRMED |
| 6acc60406 (`gfc`) | `F32`, `E4m3`, `Bf16`, `E5m2` (4, float-only) | CONFIRMED |

Ghostlite names its two FP8 formats `If8` / `Bf8` (vs 6acc60406's explicit `E4m3` / `E5m2`) and — crucially — keeps the four integer matmul dtypes that `gfc` drops entirely. The decode side reads the dtype as a 2-bit sub-ordinal `@ bit 54` within a 4-element class selected by the latch opcode at bit 60 (14 = float class `{F32=0, If8=1, Bf16=2, Bf8=3}`, 15 = integer class `{U8=0, S8=1, U4=2, S4=3}`).

### MXU slot bit map

```text
glc MatrixMultiplyBf16 @ 0x1f333ce0 (VEx0):
  opcode-HIGH (literal 0x1)  -> bit 58 (0x3a) w8   ; BitCopy(buf, 58, &1, 0, 8)
  data-format (literal 0x1)  -> bit 52 (0x34) w4   ; bf16 = 1 (push-format enum)
  control (matpush target)   -> bit 49 (0x31) w3
  done-gains / latch flag    -> bit 56 (0x38) w1
  MXU-id (unit)              -> bit 66 (0x42) w4   ; written by VectorExtended0Encoder
                                                   ;   dispatcher @ 0x1f32fd00, not the leaf
  8 systolic src vregs       -> bits 160/285/296/251/262/217/228/183 (w6 each)
    (proto +0x20..+0x3c, in proto-field order; offsets non-monotone in the buffer)
```

The MXU draws an **8-vreg systolic operand feed** (the proto `+0x20..+0x3c` source-vreg fields, all 6-bit) shared between both MXUs — this is the contracting-depth feed: the 256×256 systolic array is fed one operand column per cycle from the shared vector read ports, and the eight source-vreg fields stream the contracting dimension into the array. The weight latch is the `LoadMatrixRegister{Gmr,Lmr}{Msra,Msrb}[Bf16Conversion]` opcode family (`LoadMatrixRegisterGmrMsra` @ `0x1f33f140`, opcode-HIGH @ bit 58 w8 — the same unified 8-bit opcode region as the matmul, 4-bit sub-discriminator @ bit 52); the moving-operand push is `PushMatrix<fmt>[Masked]` (`PushMatrixBf16` @ `0x1f33fe00`, opcode-HIGH `0xe`=14 @ bit 60 w6, sub @ bit 58 w2, dtype @ bit 54 w2, 3-bit `MatpushTarget` MSRA/MSRB control). The K>128 multi-pass accumulate has a dedicated result op on Ghostlite (`PopAddMxu01Result`, below) rather than a separate `VaddF32`.

---

## The Result Slot and `PopAddMxu01Result`

The result slot (`TensorCoreVectorResult0Encoder::Encode` @ `0x1f3bc160`) reads the MXU result FIFO and the EUP transcendental FIFO into a vreg. Ghostlite keeps the Viperfish-shaped result-slot offsets (no `gfc` downshift) and carries a result sub-message that 6acc60406 lacks:

```text
glc TensorCoreVectorResult0Encoder::Encode @ 0x1f3bc160:
  result-type discriminator -> bit 24 (0x18) w4   ; always written (proto +0x1c)
  dest vreg                 -> bit 14 (0x0e) w6
  switch on proto oneof tag (a2+0x50); cmp bound 0x8 (cases 0,5,6,7,8):
    tag 5 = PopEupResult        -> writes sub-disc 0 @ bit 20 w4
    tag 6 = PopMxuResult        -> writes sub-disc 2 @ bit 21 w3 (matres pop)
    tag 7 = PopAddMxu01Result   -> writes sub-disc 1 @ bit 20 w4 (GL-only fused accumulate)
    tag 8 = TransposeResult     -> writes sub-disc 4 @ bit 21 w3
```

| Result-slot axis | Viperfish (`vxc`) | Ghostlite (`glc`) | 6acc60406 (`gfc`) | Confidence |
|---|---:|---:|---:|---|
| result-type discriminator | bit 24 w4 | bit 24 w4 | bit 20 w2 | CONFIRMED |
| dest vreg | bit 14 w6 | bit 14 w6 | bit 11 w6 | CONFIRMED |
| result-opcode bound | 0x8 (9) | 0x8 (9) | 0x7 (8) | CONFIRMED |
| fused-accumulate op | `PopCcrfResult` (scalar) | **`PopAddMxu01Result`** | (none) | CONFIRMED |

The tag→op pairing above is read from `TensorCoreVectorResult0Encoder::Encode` (`0x1f3bc160`): the `switch` is over the proto oneof tag at `a2+0x50`, and the default-instance global each arm references pins the op (tag 5 → `PopEupResult`, tag 6 → `PopMxuResult`, tag 7 → `PopAddMxu01Result`, tag 8 → `TransposeResult`). The fused-accumulate `PopAddMxu01Result` (tag 7) is GL-only.

`PopAddMxu01Result` (referenced as `TensorCoreVectorResult_PopAddMxu01Result_globals_`, the proto default-instance the tag-7 arm falls back to, in the `glc::isa` namespace) is the in-result matres-add accumulate of the multi-pass (K>128) matmul path — a Ghostlite-specific fusion. Where Viperfish uses a scalar `PopCcrfResult` and 6acc60406 uses a separate VALU `VaddF32`, Ghostlite folds the accumulate into the result pop itself.

---

## EUP / Transcendental Push (VALU Slot 3)

The transcendental push is a VALU **slot-3** op (`Alu3`), not an MXU op — the single-issue XLU is sourced exclusively from VALU slot 3. Ghostlite carries the full transcendental roster: `glc::isa` has named `Alu3` helpers for `F32`/`Bf16` × `{Tanh, Erf, Sinq, Cosq, Reciprocal, ReciprocalSqrt, …}` plus a generic `EupPush` (`0x1f2f5180`).

```text
glc EncodeTensorCoreVectorAlu3F32Tanh @ 0x1f2f4f40:
  VALU opcode (EUP-push family) -> bit 200 (0xc8) w7   ; mov esi,0xc8 ; mov r8d,0x7
  EUP function selector         -> bit 189 (0xbd) w5   ; mov esi,0xbd ; mov r8d,0x5
  EUP push src vreg             -> bit 194 (0xc2) w6   ; mov esi,0xc2 ; mov r8d,0x6
```

> **NOTE —** Ghostlite's VALU opcode is **7-bit** (like Viperfish's); only 6acc60406 widens it to 8 bits. The Ghostlite EUP-push fields therefore sit +6 bits above the 6acc60406 offsets: VALU-opcode @ bit 200 w7 (vs gfc bit 194 w8), function selector @ bit 189 w5 (vs 183), src vreg @ bit 194 w6 (vs 188), per `glc::isa::…VectorAlu3F32Tanh` (`0x1f2f4f40`).

The 5-bit function selector value (`0x13` for `F32Tanh`) is sourced at encode time from the function helper's static proto default-instance (e.g. `TensorCoreVectorAlu_F32Tanh_globals_` @ `0x2243f290`), not a literal in the helper, so the per-function selector values carry the same enum as the gen-invariant `GhPerf` transcendental set:

| Function | F32 selector | Bf16 selector | Confidence |
|---|---:|---:|---|
| `Erf` | 0x0e (14) | 0x0f (15) | HIGH |
| `ReciprocalSqrt` | 0x10 (16) | 0x0c (12) | HIGH |
| `PowTwo` (2^x) | 0x11 (17) | 0x19 (25) | HIGH |
| `LogTwo` (log2) | 0x12 (18) | 0x1a (26) | HIGH |
| `Tanh` | 0x13 (19) | 0x1b (27) | HIGH |
| `ShiftedSigmoid` | 0x14 (20) | 0x1c (28) | HIGH |
| `Reciprocal` | 0x15 (21) | 0x1d (29) | HIGH |
| `Sinq` (sin) | 0x17 (23) | 0x1e (30) | HIGH |
| `Cosq` (cos) | 0x18 (24) | 0x1f (31) | HIGH |

The push-pop protocol: bundle N issues the VALU3 op with the function selector; bundle N+k issues a result-slot `PopEupResult` (opcode 7) with dest vreg at bit 14. (The selector *bit position* is CONFIRMED; the per-function *values* are HIGH — they are read from the `.data` globals, whose per-function ordinals follow the gen-invariant `GhPerf` transcendental enum rather than per-helper literals.)

---

## The `EmitX` Templates Specific to GL

Stage 1 of the encode chain — proto population — runs through the `EmitX` template family. The GL-specific instantiations bind to `gxc::glc::isa` proto types and set a present bit before the corresponding `EncodeGl*` slot encoder `BitCopy`s the field:

- **`EmitBranchOp` / `EmitCallOp`** — signed-20-bit range check (`lea 0x80000(%rsi); cmp 0x100000; jae fail`), set the scalar-ALU present bit, write the offset into immediate slot 0 via `EmitImmediate`. The `glc` TC branch helpers (`EncodeTensorCoreScalarAlu0BranchAbsolute` @ `0x1f21da40`, `BranchRelative` @ `0x1f21daa0`, `CallAbsolute` @ `0x1f21db80`, `BranchSreg` @ `0x1f21db00`) write the discriminator (4 = BranchAbsolute, 5 = BranchRelative, 6 = CallAbsolute, 7 = CallRelative) at opcode-LOW bit 491 w5 and opcode-HIGH 0 at bit 496 w6.
- **`EmitImmediate<TensorCoreImmediates>`** — jump-tables on the slot index (0..5) and writes the value to the per-slot proto field; the `glc` `TensorCoreImmediatesEncoder::Encode` @ `0x1f20d520` then `BitCopy`s each to its bundle bit (slot 0 @ 433, slots 1..5 @ 413/393/373/353/333, all w20).
- **`EmitPredicationToSlot<…glc::isa::TensorCoreScalarAlu>`** — writes the predicate sense + register number into the slot proto; the slot encoder `BitCopy`s the 4-bit reg index @ bit 502 and 1-bit inversion @ bit 506 on the TC sequencer (or @ 187 / 191 on the SCS sequencer).
- **`EncodeProgram<EncoderGlTensorCore, glc::isa::TensorCoreProgram>`** @ `0x1e83cca0` — the program-level driver that walks every bundle through `EncoderGlTensorCore::EncodeBundle`.

The discriminator value map (shared with all V5+ generations):

| Value | Op | Confidence |
|---:|---|---|
| 4 | `BranchAbsolute` | CONFIRMED |
| 5 | `BranchRelative` | CONFIRMED |
| 6 | `CallAbsolute` | CONFIRMED |
| 7 | `CallRelative` | CONFIRMED |

There is no in-bundle delay-slot field on any V5+ generation; the branch delay-slot count is a bundle-packer pad-count (empty bundles appended after the branch), not an encoded slot bit. The hardware loop is likewise not an encoded field — it is an LCC-register read at the sequencer opcode feeding a conditional `BranchRelative`.

---

## Worked Example — a bf16 `vmatmul` + `vtanh.f32` in a Ghostlite Bundle

Encoding a bf16 matmul on MXU 0 (`VectorExtended` slot 0, unpredicated) and a `tanh.f32` transcendental into a fresh 512-bit buffer:

```text
EncoderGlTensorCore::EncodeBundle @ 0x1d331d00 walks each slot encoder.

VectorExtended0Encoder::Encode (the MatrixMultiplyBf16 helper @ 0x1f333ce0):
  MXU-id (0)            -> bit 66 (w4)
  opcode-HIGH (0x1)     -> bit 58 (w8)
  data-format (bf16=1)  -> bit 52 (w4)
  control               -> bit 49 (w3) ; done-gains -> bit 56 (w1)
  8 systolic src vregs  -> bits 160/285/296/251/262/217/228/183 (w6 each)

VectorResult0Encoder::Encode (PopMxuResult, result-opcode 6):
  result-type disc      -> bit 24 (w4)
  dest vreg             -> bit 14 (w6)

VectorAlu3 F32Tanh (EUP push @ 0x1f2f4f40):
  VALU-opcode (EUP)      -> bit 200 (w7)
  function selector (0x13)-> bit 189 (w5)
  push src vreg          -> bit 194 (w6)
  ... one+ bundles later: PopEupResult (result-opcode 7), dest vreg -> bit 14 (w6)
```

Empty slots stay at the `kNeverExecute` predicate stamp written into the bundle header before any slot is filled (see [Bundle Model](bundle-model-overview.md#empty-slot-and-nop-convention)); a populated slot's 4-bit predicate register index overwrites the default with a real `PredicationSlot`.

---

## Cross-References

- [Viperfish 64B Bundle](bundle-vf-64b.md) — the `vxc` (v5e) sibling this page diffs against: 7-bit MXU opcode, TC scalar region 3 bits lower (branch offset at 430), `PopCcrfResult` instead of `PopAddMxu01Result`.
- [6acc60406 Bundle](bundle-gf.md) — the `gfc` (v5 / TPU7x) sibling: anonymous codec, 8-bit VALU opcode, dedicated dual-predicate slot, float-only 4-dtype MXU, result slot at bits 11/20.
- [Bundle Model](bundle-model-overview.md) — the VLIW issue-word model, the 64-byte width family, the empty-slot `kNeverExecute` convention.
- [GXC Family](../targets/gxc-family.md) — why Ghostlite = `glc` (general load-core), the named-codec/`EncoderGl*` roster, and the `GetGhostliteInstruction` / `GlcCycleTable` compiler binding.
- [MXU Latency: GL](../cost/mxu-latency-gl.md) — the `GlcCycleTable` per-format MXU latency / reservation matrices and the `GhPerf::Instruction` cost grid the `GetGhostliteInstruction` remap feeds.
