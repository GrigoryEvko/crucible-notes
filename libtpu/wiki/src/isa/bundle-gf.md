# 6acc60406 Bundle

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`6acc60406` (`TpuVersion::k6acc60406` = 5, external name `TPU7x`) is the newest TensorCore generation in this build. Its bundle is a **64-byte (512-bit) VLIW issue word** — the same width as [Viperfish](bundle-vf-64b.md) and [Ghostlite](bundle-gl.md), confirmed by `gxc::gfc::isa::...GetBytesPerBundle` @ `0x1d375a40` returning `0x40`. What makes it a distinct wire format is not its width but its slot bit layout: the generation's ISA lives in the `asic_sw::deepsea::gxc::gfc` (general fetch-core) sub-core namespace, and every slot encoder packs its fields at byte/bit offsets shifted relative to Ghostlite (`gxc::glc`). This page documents that layout to reimplementation grade: the complete slot map with absolute bit offsets, how it diffs from Ghostlite (the MXU latency/format remap including the FP8 `e4m3`/`e5m2` dtype set, the result-slot remap, and the shared `GhostliteTensorCoreEmitter` EmitX leaf), and the codename's defining quirk — **its codec is anonymous**: `TpuCodec::Create` case 5 dispatches to an unnamed factory `sub_1E838380`, with no `CreateTpuCodec6acc60406` symbol anywhere in the binary.

Like every V5+ generation, the 6acc60406 bundle bytes are produced entirely by the proto-bundle `<Slot>Encoder::Encode` codecs — the default LLVM-MC `InstBits` table is all-zero for this gen. Each encoder reads its typed proto sub-message and calls the universal bit-packer `BitCopy(dst, dst_bit, src, src_bit, nbits)` @ `0x1fa0a900` to place each field at a fixed absolute bit in the 64-byte buffer. The orchestrator `EncodeBundle` @ `0x1e838cc0` walks the per-slot encoders in template-argument order; the `gfc`-namespace encoder set IS the generation's effective `kIsaTable`. For the model context (what a bundle is, the no-scoreboard VLIW contract, the empty-slot `kNeverExecute` convention), see [Bundle Model](bundle-model-overview.md).

For reimplementation, the contract is:

- The 64-byte width and that it is selected through the **anonymous** codec `sub_1E838380` (case 5 of `TpuCodec::Create`), not a named `CreateTpuCodec6acc60406`.
- The `gfc` slot offsets: each functional slot is a contiguous bit window in the 512-bit buffer, populated by `BitCopy` from a typed proto sub-message, with the per-slot predicate selector at the slot's high end and the opcode discriminator below it.
- The two GF-specific deltas vs Ghostlite: (1) the **dedicated dual-predicate slot** (`TensorCorePredicates`, a 2×(4+1)-bit pool at bits 496..505) plus the per-slot 2-bit predicate *selector*, replacing Ghostlite's per-slot 4+1-bit predicate field; (2) the **MXU format remap** — a float-only dtype set `{F32, E4m3, Bf16, E5m2}` (no integer matmul), the matmul opcode at bit 62, and the result slot remapped to bits 11/20/323.
- The decode-side inverse: `gfc::isa::...Decoder::Decode` reads the bytes back via per-field `GetConcatenatedValue` accessors and per-dtype `Opcode::Matches` mask predicates.

| | |
|---|---|
| **TpuVersion** | `k6acc60406` = **5** (external `TPU7x`); see [Codename Matrix](../targets/tpu-version-codename-matrix.md) |
| **Sub-core ISA** | `asic_sw::deepsea::gxc::gfc::isa` (general fetch-core); see [GXC Family](../targets/gxc-family.md) |
| **Bundle width** | **64 B / 512 bit** — `gfc::...GetBytesPerBundle` @ `0x1d375a40` `return 0x40` |
| **Codec** | **anonymous** — `TpuCodec::Create` @ `0x1e835fa0` case 5 → `sub_1E838380`; NO `CreateTpuCodec6acc60406` symbol |
| **Encode orchestrator** | `EncodeBundle` @ `0x1e838cc0` → `gfc::TensorCoreCodecBase<…>` worker @ `0x1d371540` |
| **Bit packer** | `BitCopy(dst, dst_bit, src, src_bit, nbits)` @ `0x1fa0a900` (LSB-first, bit-granular) |
| **Emitter leaf** | `GhostliteTensorCoreEmitter` @ `0x14221840` (shared; cell 12 reuses Ghostlite's) |
| **MXU dtype set** | `{F32, E4m3, Bf16, E5m2}` — float-only, FP8 named explicitly (vs Ghostlite's 8) |
| **Predicate model** | dedicated dual-predicate slot + per-slot 2-bit selector |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Anonymous Codec (`sub_1E838380`)

Every other TensorCore generation is reified as a named C++ codec factory: `TpuCodec::Create` (`0x1e835fa0`) is a `switch(TpuVersion)` whose first five arms call demangled `CreateTpuCodec<Codename>` constructors. The sixth arm — case 5, the 6acc60406 codec — calls an **anonymous** factory. This is not a symbolizer failure; it is a structural property of the build, and it is the single most distinctive fact about this generation.

The disassembly is unambiguous:

```text
TpuCodec::Create(TpuVersion)  @ 0x1e835fa0  (jump table on version):
  case 0 -> call 0x1e840ac0  CreateTpuCodecJellyfish
  case 1 -> call 0x1e8360e0  CreateTpuCodecDragonfish
  case 2 -> call 0x1e841fa0  CreateTpuCodecPufferfish
  case 3 -> call 0x1e843f00  CreateTpuCodecViperfish
  case 4 -> call 0x1e83bce0  CreateTpuCodecGhostlite      // named
  case 5 -> call 0x1e838380  (anonymous; no CreateTpuCodec6acc60406)
```

A symbol-table sweep returns exactly five `CreateTpuCodec*` factories — `Jellyfish`, `Dragonfish`, `Pufferfish`, `Viperfish`, `Ghostlite` — and **no** `CreateTpuCodec6acc60406`. The case-5 target `0x1e838380` carries no `CreateTpuCodec` symbol at all; it is a three-line leaf that `operator new(8)`s an 8-byte object and installs the unnamed vtable `off_21D358A8`:

```text
sub_1E838380  @ 0x1e838380:
  result = operator new(8);
  *result = &off_21D358A8;     ; vtable, no named _ZTV / _ZTI
  return result;
```

So the 6acc60406 codec is constructed inline by an unnamed factory whose vtable has no demangled `_ZTV` / `_ZTI` symbol — the structural opposite of Ghostlite's named `TpuCodecGhostlite` (vtable `0x21d35c00`).

The jump-table anchor at `0x1e835fab` is the tell that the case-5 path is genuinely the `gfc` codec and not a stray branch: the `lea` immediately preceding the dispatch references the type string for

```text
jellyfish::isa::EncoderBase<
  asic_sw::deepsea::gxc::gfc::isa::SparseCoreScsCodecBase<
    SparseCoreScsBundle, ScsScalarSubBundle, SparseCoreScalarAlu0Decoder, …Encoder,
    SparseCoreScalarAlu1…, SparseCoreScalarImmediates…, SparseCoreVectorScalar…,
    SparseCoreDma…, SparseCoreScsScalarMisc…, SparseCoreStream…, SparseCoreDmaFields>,
  …, SparseCoreScsProgram, TpuSequencerType=3>
```

— the `gxc::gfc::isa` SCS codec base, keyed at `TpuSequencerType 3`. So the anonymous codec installs `gfc`-namespace encoders.

This asymmetry is corroborated from three independent angles, all of which name the generation only by its obfuscated tag and never as a C++ class:

- **String-only registrations.** The generation appears as `6acc60406BundleRestrictions`, `6acc60406TensorcoreEmitter`, `6acc60406HardwareScanner` — string literals, not demangled classes. By contrast Ghostlite has a fully-named `TpuCodecGhostlite` (vtable `0x21d35c00`).
- **Shared emitter leaf.** The pair-keyed `IsaEmitter` registry cell for key `(5, TensorCoreSequencer)` reuses `GhostliteTensorCoreEmitter` @ `0x14221840` — there is no `Trillium`/`6acc60406` emitter class. The `gfc`-vs-`glc` split happens *inside* the codec (the `gfc::TensorCoreCodecBase` 4-VALU template), not at the leaf.
- **No Target subclass.** The LLVM `Target` family stops at `GhostliteTarget` (`0x21cc85f8`); the 6acc60406 path reuses it. Same gen-merge pattern.

> **GOTCHA — do not invent a `TpuCodec6acc60406` or a `Trillium` class.** The strings `Trillium`, `Ironwood`, and `Ghostfish` occur zero times in this binary. A reimplementation that emits a named codec/factory/target for generation 5 will diverge from the binary's structure; the correct shape is *reuse the Ghostlite leaf classes, dispatch through an anonymous codec, and key the `gfc` encoder set at version 5*. The canonical name is `6acc60406`; the only public-facing name is `TPU7x`.

> **NOTE — `gfc` is the 6acc60406 sub-core, not Ghostlite's.** A naming hazard: `gfc` looks adjacent to `glc`, but [GXC Family](../targets/gxc-family.md) pins **Ghostlite (v4) = `glc`** (general load-core) and **6acc60406 (v5) = `gfc`** (general fetch-core). Throughout this page, every `gfc::*` symbol is the 6acc60406 generation and every `glc::*` symbol is Ghostlite.

---

## Bundle Width and the Encode Path

The width is byte-anchored. `asic_sw::deepsea::gxc::gfc::isa::...GetBytesPerBundle` @ `0x1d375a40` is a two-instruction leaf:

```text
0x1d375a40:  mov eax, 0x40      ; 64
0x1d375a45:  ret
```

So the 6acc60406 TensorCore bundle is 64 bytes — confirming the [Bundle Model](bundle-model-overview.md) claim that Viperfish (v3), Ghostlite (v4), and 6acc60406 (v5) all share the 64-byte issue word and differ only in slot layout. The width is reached through the codec-metadata registry (`codec_metadata::BundleSizeBytes` @ `0x1ecf7180` → `GetMetadataOrDie` → vtable), not a fixed `switch` — there is no `Trillium`/`6acc60406CodecMetadata` symbol; the registered entry reuses the 64-byte v5+ class shape.

The TensorCore encoder itself is built by the **shared** factory `tpu::internal::CreateEncoderGlGf` @ `0x1e831020` — the same factory Ghostlite uses. It branches on `TpuVersionFromProtoOrDie`: `v3 == 4` constructs a `gxc::glc::isa::TensorCoreCodecBase` (object size `0xF0`, vtable `off_21CBFCD0`), while `v3 == 5` constructs a `gxc::gfc::isa::TensorCoreCodecBase` (object size `0xF8`, vtable `off_21CC22E0`); any sequencer type other than the handled TC / SCS / TAC cases hits a `LogMessageFatal("Unsupported sequencer type")`. That single factory housing both generations is the concrete realization of the GL/GF encoder sharing — the `gfc`-vs-`glc` split is a `TpuVersion` branch *inside* `CreateEncoderGlGf`, not two separate factories. See [Ghostlite Bundle](bundle-gl.md) for the `glc` arm.

The encode path is the universal two-stage V5+ chain:

```text
LLO op ──(EmitX, proto population)──▶ proto sub-message (present-bit + fields)
proto sub-message ──(<Slot>Encoder::Encode, BitCopy)──▶ absolute bit window in 64-B buffer
```

`EncodeBundle` @ `0x1e838cc0` is the orchestrator: it dispatches by `TpuSequencerType` (TC / SCS / TEC), constructs the `gfc::TensorCoreCodecBase<…>` for the TC path, and calls the worker @ `0x1d371540`, which invokes each slot's `Encoder::Encode` against the shared 64-byte buffer in template-argument order. Every field write is a `BitCopy(buffer, dst_bit, &field, 0, width)` call to `0x1fa0a900` — bit-granular, LSB-first, where `dst_bit` is the absolute bit in the bundle (byte = `dst_bit >> 3`, bit-in-byte = `dst_bit & 7`). The tables below give the `(dst_bit, width)` triple for every field, each verified from the literal `mov esi,<dst_bit>` / `mov r8d,<width>` immediates preceding the `BitCopy` call in the named encoder.

---

## Slot Map — Absolute Bit Offsets (64-byte / 512-bit buffer)

The bundle partitions into the standard V5+ slot classes. The table below is the consolidated 6acc60406 (`gfc`) slot map; bit 0 is the LSB of byte 0. Each entry is anchored to the named `gfc::isa::...Encoder::Encode` that writes it and the `BitCopy` immediate that fixes the offset.

| Slot / field | dst_bit (dec) | hex | width | Encoder (`gfc::isa`) @ | Confidence |
|---|---:|---|---:|---|---|
| **Predicate pool** `pred_0` reg | 501 | 0x1f5 | 4 | `TensorCorePredicatesEncoder::Encode` @ `0x1f86e500` | CONFIRMED |
| `pred_0` invert | 505 | 0x1f9 | 1 | (same) | CONFIRMED |
| `pred_1` reg | 496 | 0x1f0 | 4 | (same) | CONFIRMED |
| `pred_1` invert | 500 | 0x1f4 | 1 | (same) | CONFIRMED |
| **Sequencer** per-slot 2-bit pred selector | 489 | 0x1e9 | 2 | `TensorCoreScalarAlu0Encoder::Encode` @ `0x1f87b420` | CONFIRMED |
| seq opcode-HIGH / family | 483 | 0x1e3 | 6 | (same) | CONFIRMED |
| seq opcode-LOW / discriminator | 478 | 0x1de | 5 | (same; branch helpers @ `0x1f87f5c0`+) | CONFIRMED |
| seq x-target / 2nd operand | 472 | 0x1d8 | 6 | (same) | CONFIRMED |
| seq call dest (return-addr) sreg | 467 | 0x1d3 | 5 | (same) | CONFIRMED |
| **Immediate** slot 0 (branch/call/sync offset) | 423 | 0x1a7 | 20 | `TensorCoreImmediatesEncoder::Encode` @ `0x1f86de20` | CONFIRMED |
| imm slot 1 | 403 | 0x193 | 20 | (same) | CONFIRMED |
| imm slot 2 | 383 | 0x17f | 20 | (same) | CONFIRMED |
| imm slot 3 | 363 | 0x16b | 20 | (same) | CONFIRMED |
| imm slot 4 | 343 | 0x157 | 20 | (same) | CONFIRMED |
| imm slot 5 | 323 | 0x143 | 20 | (same) | CONFIRMED |
| **VALU slot 0** opcode | 293 | 0x125 | 8 | `TensorCoreVectorAlu0Encoder` (P-3-204 family) | HIGH |
| VALU0 dst vreg | 276 | 0x114 | 6 | (same) | HIGH |
| VALU0 src0 | 270 | 0x10e | 6 | (same) | HIGH |
| VALU0 src1 | 287 | 0x11f | 6 | (same) | HIGH |
| VALU0 Y-enc | 282 | 0x11a | 5 | (same) | HIGH |
| VALU0 2-bit pred selector | 301 | 0x12d | 2 | (same) | HIGH |
| **MXU VEx0** opcode-HIGH | 62 | 0x3e | 8 | `TensorCoreVectorExtended0Encoder::Encode` @ `0x1f996940` | CONFIRMED |
| VEx0 data-format sub-disc | 57 | 0x39 | 4 | (same) | CONFIRMED |
| VEx0 MXU-id (unit) | 70 | 0x46 | 2 | (same) | CONFIRMED |
| VEx0 control (matpush target) | 54 | 0x36 | 3 | (matmul/push helpers) | CONFIRMED |
| VEx0 done-gains / latch flag | 61 | 0x3d | 1 | (same) | CONFIRMED |
| VEx0 primary operand (matmul) | 47 | 0x2f | 7 | `…MatrixMultiplyBf16` @ `0x1f99a920` | CONFIRMED |
| **MXU VEx1** opcode-HIGH | 37 | 0x25 | 8 | `TensorCoreVectorExtended1Encoder::Encode` @ `0x1f9d3800` | CONFIRMED |
| VEx1 data-format sub-disc | 32 | 0x20 | 4 | (same) | CONFIRMED |
| VEx1 MXU-id (unit) | 45 | 0x2d | 2 | (same) | CONFIRMED |
| **EUP push** (VALU slot 3) VALU-opcode | 194 | 0xc2 | 8 | `…VectorAlu3F32Tanh` @ `0x1f96ae40` (family) | CONFIRMED |
| EUP function selector | 183 | 0xb7 | 5 | (same) | CONFIRMED |
| EUP push src vreg | 188 | 0xbc | 6 | (same) | CONFIRMED |
| **Result slot** result-type discriminator | 20 | 0x14 | 2 | `TensorCoreVectorResult0Encoder::Encode` @ `0x1fa01820` | CONFIRMED |
| result dest vreg | 11 | 0x0b | 6 | (same) | CONFIRMED |
| result mode/format | 17–19 | 0x11–0x13 | 2/1 | (same) | CONFIRMED |
| `PopMxuResult` accum-mode/format | 323 | 0x143 | 8 | (same; result-opcode 7 path) | CONFIRMED |

The eight MXU source-vreg fields are **shared** between VEx0 and VEx1 (both MXUs draw the same vector read ports) and are byte-identical across the two slots:

| MXU systolic source-vreg (`gfc` VEx0 = VEx1) | dst_bit (dec) | hex | width | Confidence |
|---|---:|---|---:|---|
| src #1 (proto +0x20) | 156 | 0x9c | 6 | CONFIRMED |
| src #2 (proto +0x24) | 276 | 0x114 | 6 | CONFIRMED |
| src #3 (proto +0x28) | 287 | 0x11f | 6 | CONFIRMED |
| src #4 (proto +0x2c) | 243 | 0xf3 | 6 | CONFIRMED |
| src #5 (proto +0x30) | 254 | 0xfe | 6 | CONFIRMED |
| src #6 (proto +0x34) | 210 | 0xd2 | 6 | CONFIRMED |
| src #7 (proto +0x38) | 221 | 0xdd | 6 | CONFIRMED |
| src #8 (proto +0x3c) | 177 | 0xb1 | 6 | CONFIRMED |

> **NOTE — the two MXU control regions are a fixed −25-bit twin.** VEx0's opcode/format/MXU-id/control region (bits 62/57/70/54) and VEx1's (bits 37/32/45/29) differ by exactly **−25 bits**; only the eight shared source-vreg fields are delta-0. A reimplementation packs both MXUs into the same 64-byte word by writing one control region at the VEx0 offsets and a second at VEx0 − 25, over a single 8×6-bit operand pool. (The VALU slot rows are marked HIGH rather than CONFIRMED here because their offsets are carried from the cross-confirmed VALU-slot trace rather than re-walked on this page.)

---

## Sequencer Slot and the 20-bit Branch Offset

The 6acc60406 sequencer slot (`TensorCoreScalarAlu0Encoder::Encode` @ `0x1f87b420`) follows the V5+ shape: a `{6-bit opcode-HIGH, 5-bit opcode-LOW}` pair plus operand and dest fields, with the branch/call target landing in **immediate slot 0**, not in the sequencer slot itself.

```text
gfc TensorCoreScalarAlu0 sequencer slot:
  per-slot pred selector  @ bit 489 (0x1e9) w2   ; 2-bit selector into the predicate pool
  opcode-HIGH / family    @ bit 483 (0x1e3) w6   ; = 0 for branch/call, op for ALU compute
  opcode-LOW discriminator @ bit 478 (0x1de) w5  ; 4/5/6/.. (see map)
  x-target / 2nd operand  @ bit 472 (0x1d8) w6   ; BranchSreg / Call aux
  call dest sreg          @ bit 467 (0x1d3) w5   ; return-address link register
  branch/call offset      @ imm slot 0 = bit 423 (0x1a7) w20  ; signed −0x80000..+0x7FFFF
```

The opcode-LOW discriminator value map (shared with all V5+ gens):

| Value | Op | Confidence |
|---:|---|---|
| 4 | `BranchAbsolute` | CONFIRMED |
| 5 | `BranchRelative` | CONFIRMED |
| 6 | `CallAbsolute` | CONFIRMED |
| 7 | `CallRelative` | CONFIRMED |

Two GF specifics relative to Ghostlite (`glc`), where the sequencer slot is the inverse of the predicate change below: the GF sequencer slot is **wider** — it adds a 6-bit operand field at bit 472 that Ghostlite's slot lacks — and its predicate field shrinks from Ghostlite's 4-bit reg + 1-bit inversion to a **2-bit selector** at bit 489. There is no in-bundle delay-slot field on any V5+ gen; the branch delay-slot count is a bundle-packer pad-count (empty bundles appended after the branch), not an encoded slot bit. The hardware loop is likewise not an encoded field — it is an LCC-register read at the sequencer opcode feeding a conditional `BranchRelative`.

---

## The Dedicated Dual-Predicate Slot (GF vs Ghostlite)

This is the structural predicate change that defines the generation. Viperfish and Ghostlite carry a per-slot 4-bit predicate register index + 1-bit inversion at the top of *each* functional slot. 6acc60406 splits this into two pieces:

1. A **dedicated dual-predicate slot** (`TensorCorePredicatesEncoder::Encode` @ `0x1f86e500`) holding a two-entry register pool at the very top of the 64-byte bundle.
2. A per-slot **2-bit selector** (e.g. bit 489 on the sequencer slot, bit 301 on VALU0) that picks one of `{pred_0, pred_1, always, never}`.

The pool layout, byte-exact from the four `BitCopy` calls in the encoder:

```text
gfc TensorCorePredicatesEncoder::Encode @ 0x1f86e500:
  pred_0 reg     -> bit 501 (0x1f5) w4   ; mov esi,0x1f5 ; mov r8d,0x4 ; call BitCopy
  pred_0 invert  -> bit 505 (0x1f9) w1   ; mov esi,0x1f9 ; mov r8d,0x1
  pred_1 reg     -> bit 496 (0x1f0) w4   ; mov esi,0x1f0 ; mov r8d,0x4
  pred_1 invert  -> bit 500 (0x1f4) w1   ; mov esi,0x1f4 ; mov r8d,0x1
```

Each 4-bit `reg` field indexes the 16-entry `PredicationSlot` pool (0..15). Every functional slot then carries only the 2-bit selector. This is the bit-exact realization of the "two predicate slots are already taken" overflow rule: the two `(reg, invert)` entries at bits 496..505 are the entire per-bundle predicate budget.

| Predicate model | Viperfish / Ghostlite | 6acc60406 | Confidence |
|---|---|---|---|
| Per-slot field | 4-bit reg + 1-bit invert (TC: reg @ 499, inv @ 503 on VXC) | 2-bit selector (TC seq @ 489) | CONFIRMED |
| Pool | none (each slot self-contained) | dedicated `TensorCorePredicates` slot @ bits 496..505 | CONFIRMED |
| Per-bundle predicate budget | per-slot (unbounded) | exactly two pool entries (`pred_0`, `pred_1`) | CONFIRMED |

> **GOTCHA — a slot's predicate is an indirection on GF, a direct index on Ghostlite.** A Ghostlite decoder reads a slot's 4-bit register index directly from the slot. A 6acc60406 decoder must read the slot's 2-bit selector, then resolve it against the `pred_0`/`pred_1` pool in the dedicated predicate slot. Treating the GF 2-bit field as a register index (rather than a pool selector) decodes wrong predicates.

---

## The MXU Format Remap and FP8 (GF vs Ghostlite)

The MXU slot is where the GF "latency/format remap" lives. The slot mechanism is identical to the other V5+ gens — two `VectorExtended` slots (VEx0, VEx1) for the two MXUs, each a `BitCopy`-packed per-op helper dispatched through the slot encoder's jump table — but the dtype set, the opcode bit, and the result-slot offsets all shift.

### Dtype set: float-only, FP8 named explicitly

6acc60406 supports **four** matmul/push dtypes, all float: `{F32, E4m3, Bf16, E5m2}`. It drops the integer matmul group entirely. The symbol-table census is definitive:

| Generation | PushMatrix / MatrixMultiply dtype set | Confidence |
|---|---|---|
| **6acc60406 (`gfc`)** | `F32`, `E4m3`, `Bf16`, `E5m2` (4, float-only; FP8 = e4m3 / e5m2) | CONFIRMED |
| Ghostlite (`glc`) | `F32`, `If8`, `Bf16`, `Bf8` (float) + `U8`, `S8`, `U4`, `S4` (int) (8) | CONFIRMED |

The rename is the heart of the remap: where Ghostlite names its two FP8 formats `If8` / `Bf8`, 6acc60406 names them explicitly as **`E4m3`** and **`E5m2`** — and registers no integer matmul dtypes at all. (The `e4m3`/`e5m2` here are the IEEE-style FP8 mantissa/exponent splits; the GF MXU latency/format mapping for these — including the FP8 `fnuz` handling and the result-format remap — is detailed in the [GF MXU latency cost page](../cost/mxu-latency-gf.md).)

### MXU slot bit map

```text
gfc MatrixMultiplyBf16 @ 0x1f99a920 (VEx0):
  opcode-HIGH (literal 0x1)  -> bit 62 (0x3e) w8   ; mov esi,0x3e ; mov r8d,0x8
  data-format (literal 0x1)  -> bit 57 (0x39) w4   ; bf16 = 1 (push-format enum)
  control (3-bit)            -> bit 54 (0x36) w3
  done-gains / latch flag    -> bit 61 (0x3d) w1
  primary operand            -> bit 47 (0x2f) w7
  MXU-id (unit)              -> bit 70 (0x46) w2   ; addresses up to 4 (2 used)
  8 systolic source vregs    -> bits 156/276/287/243/254/210/221/177 (w6 each)
```

The opcode bound for the VEx0 encoder is `cmp 0x54` = 85 ops/slot. The weight latch is the `LoadMatrixRegister{Gmr,Lmr}{Msra,Msrb}[Bf16Conversion]` opcode family (opcode-HIGH `0x37` @ bit 62 w8, 4-bit sub-discriminator @ bit 57); the moving-operand push is `PushMatrix<fmt>[Masked]` (opcode-HIGH `0xe` @ bit 64 w6, 3-bit `MatpushTarget` MSRA/MSRB control @ bit 54). The fused latch-via-LMR matmul `MatrixMultiplyLmr` (sub-format `0x2`) implements the K>128 multi-pass accumulate (vs Ghostlite's dedicated `PopAddMxu01Result`).

### Result-slot remap

The result slot (`TensorCoreVectorResult0Encoder::Encode` @ `0x1fa01820`) is where the "res-remap" delta sits — the discriminator and dest-vreg fields move down relative to Ghostlite:

| Result-slot field | Ghostlite (`glc`) | 6acc60406 (`gfc`) | Confidence |
|---|---:|---:|---|
| result-type discriminator | bit 24 w4 | **bit 20 w2** | CONFIRMED |
| dest vreg | bit 14 w6 | **bit 11 w6** | CONFIRMED |
| `PopMxuResult` accum-mode/format | (per +0x1c) | **bit 323 w8** | CONFIRMED |
| result-opcode bound | 0x8 (9 ops) | **0x7 (8 ops)** | CONFIRMED |

The result-opcode map (the proto sub-message tag, read from the `switch` in `TensorCoreVectorResult0Encoder::Encode` @ `0x1fa01820`): `5` = `PopEupResult` (EUP transcendental pop), `6` = `TransposeResult`, `7` = `PopMxuResult` (matres pop; the `case 7` arm is the only one that writes the 8-bit accum-mode/format at bit 323). 6acc60406 has no `PopAddMxu01Result` (Ghostlite's fused accumulate) and no `PopCcrfResult` (Viperfish's scalar pop) — its result sub-message set is `{PopEupResult, TransposeResult, PopMxuResult}`.

---

## EUP / Transcendental Push (VALU Slot 3)

The transcendental push is a VALU **slot-3** op (`Alu3`), not an MXU op — the single-issue XLU is sourced exclusively from VALU slot 3. The push writes a 5-bit function selector and pops one+ bundles later through the result slot's `PopEupResult` opcode.

```text
gfc EncodeTensorCoreVectorAlu3F32Tanh @ 0x1f96ae40:
  VALU opcode (EUP-push family, 0x0) -> bit 194 (0xc2) w8   ; mov esi,0xc2 ; mov r8d,0x8
  EUP function selector              -> bit 183 (0xb7) w5   ; mov esi,0xb7 ; mov r8d,0x5
  EUP push src vreg                  -> bit 188 (0xbc) w6   ; mov esi,0xbc ; mov r8d,0x6
```

The 5-bit function selector value map (verified from the per-function `Alu3` helper literals):

| Function | F32 selector | Bf16 selector | Confidence |
|---|---:|---:|---|
| `Erf` | 0x0e (14) | 0x0f (15) | CONFIRMED |
| `ReciprocalSqrt` | 0x10 (16) | 0x0c (12) | CONFIRMED |
| `PowTwo` (2^x) | 0x11 (17) | 0x19 (25) | HIGH |
| `LogTwo` (log2) | 0x12 (18) | 0x1a (26) | HIGH |
| `Tanh` | 0x13 (19) | 0x1b (27) | CONFIRMED |
| `ShiftedSigmoid` | 0x14 (20) | 0x1c (28) | HIGH |
| `Reciprocal` | 0x15 (21) | 0x1d (29) | CONFIRMED |
| `Sinq` (sin) | 0x17 (23) | 0x1e (30) | HIGH |
| `Cosq` (cos) | 0x18 (24) | 0x1f (31) | HIGH |

The push-pop protocol: bundle N issues the VALU3 op with the function selector; bundle N+k issues a result-slot `PopEupResult` (result-opcode 5) with dest vreg at bit 11. (`Tanh` and `Reciprocal`/`ReciprocalSqrt` are CONFIRMED from disassembled helper immediates; the remaining selectors are HIGH from the helper roster and the contiguous selector ordering.)

---

## Decode Side — the Inverse Twin

The codec ships a symmetric decoder; `gfc::isa::TensorCoreVectorExtended0Decoder::Decode` @ `0x1f96d020` (and `…Extended1Decoder::Decode` @ `0x1f9aa7a0`) reads the 64-byte span back into the typed proto. The mechanism is the structural inverse of `BitCopy`: the span is staged into a 16-byte struct (size-tag at base, bundle bytes at base+8), then each field is read by a tiny `Field::GetConcatenatedValue` accessor (`mov rax,[base+off]; shr rax,shift; and rax,mask`, where absolute bit = `(off−8)*8 + shift`), and the opcode is resolved by trying per-dtype `Opcode::Matches` mask predicates in sequence (`Noop`, then `MatrixMultiply*`, then `PushMatrix*`).

The decode side independently confirms the GF MXU layout and pins two GF-specific facts:

- **Latch opcode @ bit 64.** The `gfc` `PushMatrix*Opcode::Matches` reads the latch opcode at bit 64 (width 6 = 14, all four dtypes float) and the dtype-class at bit 59 (width 2): `{F32=0, E4m3=1, Bf16=2, E5m2=3}`. The matmul opcode is a unified 8-bit field at bit 62 (`MatrixMultiply*Lgmr{Msra,Msrb}` @ `0x1f98f740`, value `0x2`=Msra / `0x3`=Msrb, MSR-select = opcode LSB); the latch valid-guard is a `bt bit 62` test (the GF analog of Ghostlite's 2-bit `58,59 == 3` guard).
- **The inter-MXU twin is −25 on GF (−21 on Ghostlite).** Because GF's MXU0 control region is +4 bits higher than Ghostlite's (latch opcode 60→64) while MXU1 anchors at bit 39 in both, the GF MXU0↔MXU1 delta widens to −25 (latch op 64→39, dtype-class 59→34, matmul op 62→37, matmul fmt 57→32).

| MXU-twin geometry | MXU0 latch op bit | MXU1 latch op bit | inter-MXU twin | matmul MSR-LSB (MXU0/MXU1) | Confidence |
|---|---:|---:|---:|---|---|
| Ghostlite (`glc`) | 60 | 39 | −21 | 58 / 37 | CONFIRMED |
| **6acc60406 (`gfc`)** | **64** | **39** | **−25** | **62 / 37** | CONFIRMED |

> **CORRECTION (GF-TWIN) —** an earlier reading carried the 6acc60406 inter-MXU twin as −21 (the Ghostlite value). The decode-side disassembly resolves it to **−25**: the +4 `glc`→`gfc` MXU0 drift (latch opcode 60→64) compounds the inter-MXU offset because MXU1 stays anchored at bit 39 in both generations. The −21 figure applies only to Ghostlite.

For the full decode-side mechanism (the staged-copy accessor model and the linear `Opcode::Matches` dispatch) shared with Viperfish, see [Decode-Side: VF / GXC](decode-side-vf-gxc.md).

---

## Worked Example — a bf16 `vmatmul` + `vtanh.f32` in a 6acc60406 Bundle

Encoding a bf16 matmul on MXU 0 (`VectorExtended` slot 0, unpredicated) and a `tanh.f32` transcendental into a fresh 512-bit buffer:

```text
EncodeBundle @ 0x1e838cc0 -> gfc TC worker @ 0x1d371540 walks each slot encoder.

VectorExtended0Encoder::Encode (the MatrixMultiplyBf16 helper):
  MXU-id (0)            -> bit 70 (w2)
  opcode-HIGH (0x1)     -> bit 62 (w8)
  data-format (bf16=1)  -> bit 57 (w4)
  control               -> bit 54 (w3) ; done-gains -> bit 61 (w1)
  primary operand       -> bit 47 (w7)
  8 systolic src vregs  -> bits 156/276/287/243/254/210/221/177 (w6 each)

VectorResult0Encoder::Encode (PopMxuResult, result-opcode 7):
  result-type disc      -> bit 20 (w2)
  accum-mode/format     -> bit 323 (w8)
  dest vreg             -> bit 11 (w6)

VectorAlu3 F32Tanh (EUP push):
  VALU-opcode (EUP, 0x0) -> bit 194 (w8)
  function selector (0x13)-> bit 183 (w5)
  push src vreg          -> bit 188 (w6)
  ... one+ bundles later: PopEupResult (result-opcode 5), dest vreg -> bit 11 (w6)
```

Empty slots stay at the `kNeverExecute` predicate stamp written into the bundle header before any slot is filled (see [Bundle Model](bundle-model-overview.md#empty-slot-and-nop-convention)); a populated slot's 2-bit selector overwrites the default with a pool index.

---

## Cross-References

- [Ghostlite Bundle](bundle-gl.md) — the `glc` (v4) sibling this page diffs against: 8-dtype MXU set, per-slot 4+1 predicate, result slot at bits 14/24, −21 inter-MXU twin.
- [Bundle Model](bundle-model-overview.md) — the VLIW issue-word model, the 64-byte width family, the empty-slot `kNeverExecute` convention.
- [Decode-Side: VF / GXC](decode-side-vf-gxc.md) — the staged-copy `GetConcatenatedValue` / `Opcode::Matches` decode mechanism and the per-gen MXU twin geometry.
- [GXC Family](../targets/gxc-family.md) — why 6acc60406 = `gfc` (general fetch-core) and shares the VXC HAL but has its own ISA.
- [Codename Matrix](../targets/tpu-version-codename-matrix.md) — the `TpuVersion` enum, the anonymous case-5 codec, and the `6acc60406` ↔ `TPU7x` mapping.
- [MXU Latency: GF (6acc60406)](../cost/mxu-latency-gf.md) — the GF-specific per-format MXU latency / reservation matrices, the FP8 `e4m3`/`e5m2` (`fnuz`) handling and the result-format remap.
