# Overview

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

Part VI documents the TPU TensorCore instruction set and how the compiler encodes it. The TensorCore is a statically-scheduled VLIW machine: its on-chip sequencer fetches one fixed-width **bundle** per cycle and issues every slot in that bundle to a distinct execution unit — the matrix unit, the vector ALUs, the scalar pipe, the memory ports, the sequencer's own control logic — all in the same cycle, with no runtime dependency tracking. Above the hardware bundle sits **LLO** (the late, TPU-specific compiler IR): a stream of typed instructions, each one opcode + operands + memory-space + predicate, produced by the `HLO → MHLO → TLP → tpu → LLO` descent and consumed by the bundle packer and the per-generation encoders. The `LloOpcodeProto` enum carries **462** opcodes and `MemorySpaceProto` carries **17** memory spaces; together they are the vocabulary the rest of this Part decodes into bits.

The encoding problem has two faces, and this Part keeps them separate. There is the **LLVM-MC layer** — `TPUMCCodeEmitter::getBinaryCodeForInstr` (`0x13c74da0`) lowering one `MCInst` into a [239-bit `APInt` record](record-format.md) via a per-opcode base-bits table and `insertBits` holes — which actually carries bits only for the BarnaCore-Pxc (Pufferfish) HwMode. And there is the **proto-bundle layer** — the per-generation `Encoder<gen>::EncodeBundleInternal` and the [IsaEmitter](isa-emitter-registry.md) `EmitX` / `<Slot>Encoder::Encode` path — which encodes every TensorCore and V5+ opcode the MC layer returns all-zero for. A reimplementer needs to know which path owns which opcodes; the [MC-Emitter](mc-emitter.md) page draws that line precisely (4956 of 5667 MC opcodes route to the zero-base default).

This page is navigational. It orients the reader in the VLIW model, names the per-generation bundle widths (41 B Jellyfish → 51 B Pufferfish → 64 B Viperfish / Ghostlite / 6acc60406), sketches the slot taxonomy, and then routes to the page that owns each piece. The deep mechanics live elsewhere: the [bundle model](bundle-model-overview.md) for the slot/issue semantics, the per-slot pages for bit layouts, the per-gen bundle pages for full slot maps, and the [record format](record-format.md) / [MC-emitter](mc-emitter.md) / [InstBits DB](instbits-master-db.md) trio for the LLVM-MC wire path.

For reimplementation, the contract is:

- The two-level encoding split: LLO IR (`LloOpcodeProto`, 462 opcodes) above, and the fixed-width per-gen VLIW bundle below — and the two distinct encoders (MC `insertBits` record vs proto-bundle `EmitX`).
- The VLIW slot model: which engine each slot drives and the simultaneous-issue / no-forwarding rule the compiler must respect.
- The six-generation `TpuVersion` axis (`0`..`5`) and that the bundle width and slot set are selected per version, not byte-extended.

| | |
|---|---|
| **LLO IR enum** | `LloOpcodeProto` — 462 opcodes — [LloOpcode Enum](llo-opcode-enum.md) |
| **Memory spaces** | `MemorySpaceProto` — 17 values — [MemorySpace Enum](memory-space-enum.md) |
| **MC record** | `llvm::APInt`, 239 bits, 4 words — [Record Format](record-format.md) |
| **MC emitter** | `TPUMCCodeEmitter::getBinaryCodeForInstr` @ `0x13c74da0` — [MC-Emitter](mc-emitter.md) |
| **Bundle widths** | 41 B (Jellyfish) / 51 B (Pufferfish) / 64 B (Viperfish, Ghostlite, 6acc60406) — [Bundle Model](bundle-model-overview.md) |
| **`TpuVersion`** | 6 values (`TpuVersionToString` @ `0x20b3a480` traps on `≥ 6`) |
| **Proto-bundle encode** | `Encoder<gen>::EncodeBundleInternal` (Jellyfish @ `0x1e86c7c0`) |

---

## The Two Encoding Levels

LLO is the compiler IR; the bundle is the wire form. The split is real in the binary and a reimplementer must not conflate them.

- **Level 1 — LLO instruction stream.** A program is a sequence of `LloInstruction`s, each `(opcode, operand-list, memory-space, predicate)`. The opcode is one of the 462 `LloOpcodeProto` values; the memory space is one of the 17 `MemorySpaceProto` values. This is what the optimizer and scheduler manipulate. See [LloOpcode Enum](llo-opcode-enum.md) and [LloOpcode ↔ Proto](llo-opcode-to-proto.md).
- **Level 2 — VLIW bundle word.** The [bundle packer](../sched/llo-bundle-packing.md) groups mutually independent LLO ops into a `Bundle` whose typed sub-fields map onto the hardware slots, and the per-generation encoder serializes that into a fixed-width byte buffer. See [Bundle Model](bundle-model-overview.md).

Within Level 2 there are *two* encoders, and which one carries an opcode's bits depends on the generation:

| Encoder | Owns | Page |
|---|---|---|
| LLVM-MC `insertBits` record | BarnaCore-Pxc (Pufferfish) lanes + native ops only | [MC-Emitter](mc-emitter.md), [Record Format](record-format.md) |
| proto-bundle `EmitX` / `<Slot>Encoder::Encode` | every TensorCore + Viperfish / Ghostlite / 6acc60406 opcode | [IsaEmitter Registry](isa-emitter-registry.md), [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md) |

> **NOTE —** the MC `getBinaryCodeForInstr` returns an all-zero 239-bit record for the overwhelming majority of opcodes (4956 of 5667). That is not a bug or a stub — those opcodes are encoded by the proto-bundle path. A reimplementation that treats the MC layer as the sole encoder will emit all-zero bundles for every V5+ instruction. See [MC-Emitter](mc-emitter.md#per-opcode-dispatch).

---

## The VLIW Bundle and Its Slots

The bundle is a fixed-width VLIW word issued in one cycle; the compiler proves slot independence because the hardware does not. The width is fixed per generation and selected by a `(TpuVersion, TpuSequencerType)` codec-metadata lookup. The codename ↔ external-name mapping below is the one the [Codename Matrix](../targets/tpu-version-codename-matrix.md) pins from the `TpuVersionToString` / `TpuVersionToExternalName` pair; `6acc60406` (`TpuVersion` 5) is the binary's literal codename, not the marketing name (`Trillium`/`Ironwood` appear **zero** times in `libtpu.so`).

| `TpuVersion` | Codename | External name | Bundle bytes | Bundle bits |
|---:|---|---|---:|---:|
| 0 | Jellyfish | TPU v2 | 41 | 328 |
| 1 | Dragonfish | TPU v3 | 41 | 328 |
| 2 | Pufferfish | TPU v4 | 51 | 408 |
| 3 | Viperfish | TPU v5e | 64 | 512 |
| 4 | Ghostlite | TPU v6 lite | 64 | 512 |
| 5 | 6acc60406 | TPU7x | 64 | 512 |

The 41-byte Jellyfish width is the hardest-pinned: it is the literal `operator new(0x29)` (= 41) allocation inside `EncoderJf::EncodeBundleInternal` (`0x1e86c7c0`), not a metadata read. The 51-byte and 64-byte widths are computed at runtime — `EncoderPfTensorCore::BundleSizeBytes` returns 51 inline, while the v5+ codecs reach 64 through a `(TpuVersion, TpuSequencerType)` vtable call (and the SparseCore overlayer's `GetTileInstructionBundleSizeInBytes` derives a per-tile size as `field[32] / field[31]`). The full byte-source accounting per generation is on the [Bundle Model](bundle-model-overview.md#per-generation-bundle-widths) page.

The slots partition across the execution units. Each slot class is a typed sub-instruction in the compiler-side `Bundle` object and has its own page:

| Engine | Slot page(s) |
|---|---|
| Matrix unit (systolic MXU) | [MXU Slot](slot-mxu.md), [Matprep / IAR / Latch](slot-matprep-iar-latch.md), [ResultFifo & ArchRegister](resultfifo-archregister.md) |
| Vector ALU (VPU) | [VPU Slot](slot-vpu.md) |
| Scalar pipe (SPU) | [SPU / Scalar Slot](slot-spu-scalar.md) |
| Sequencer (control flow / sync) | [Sequencer Slot](slot-sequencer.md), [Sequencer Ops Per Gen](sequencer-ops-per-gen.md) |
| Memory ports | [Memory-Load](slot-memory-load.md), [Memory-Store](slot-memory-store.md), [cmem_load (Pufferfish)](slot-cmem-load-pf.md) |
| Predicate / loop / immediate | [Predicate](slot-predicate.md), [Hardware Loop-Counter](slot-loop.md), [Immediate](slot-immediate.md) |
| Extended unit (transcendentals) | [EUP / Transcendental Slot](slot-eup-transcendental.md) |
| Mask / M-register | [vcreate_mask / M-Register](slot-vcreate-mask-mregister.md) |
| SparseCore (v5+) | [Sparsity Slot](slot-sparsity-v5plus.md) |

The full per-generation slot maps — which slots exist and at what byte offsets — are on the per-gen pages: [Jellyfish 41-B](bundle-jf-41b.md), [Dragonfish](bundle-df.md), [Pufferfish 51-B](bundle-pf-51b.md), [Viperfish 64-B](bundle-vf-64b.md), [Ghostlite](bundle-gl.md), [6acc60406](bundle-gf.md). The simultaneous-issue and empty-slot (`kNeverExecute`) semantics that bind them are on the [Bundle Model](bundle-model-overview.md) page.

---

## How LLO Packs Into Bundles

The path from an LLO op to its bits has a fixed shape:

```text
LloInstruction  (opcode + operands + memory-space + predicate)
   │  bundle packer — group independent ops into typed Bundle slots
   ▼
Bundle  (ScalarInstruction, VectorAluInstruction, VectorExtendedInstruction,
         VectorLoadInstruction, VectorStoreInstruction, VectorResultInstruction,
         MiscInstruction, + HardwareBundleBits header)
   │  Encoder<gen>::EncodeBundleInternal — write each present slot at its byte offset
   ▼
N-byte bundle word   (41 / 51 / 64, per TpuVersion)
```

The packer is the scheduler's responsibility ([LLO Bundle Packing](../sched/llo-bundle-packing.md)); the serialization is the encoder's. For the BarnaCore-Pxc path the per-instruction bits additionally pass through the [239-bit MC record](record-format.md); for everything else the [IsaEmitter](isa-emitter-registry.md) writes the bytes directly. The MXU is a two-phase exception: matrix pushes enter the systolic array via the EUP / latch path and results are read back cycles later through the result-FIFO slot — see [MXU Slot](slot-mxu.md).

---

## How This Part Is Organized

The pages group into five bands:

- **Foundations** — this page, the [LloOpcode Enum](llo-opcode-enum.md), the [MemorySpace Enum](memory-space-enum.md), and the [Bundle Model](bundle-model-overview.md).
- **MC wire path** — [InstBits Master DB](instbits-master-db.md), [TPUInstrNameData / Descs / RegEncoding](instr-name-data.md), [LloOpcode ↔ Proto](llo-opcode-to-proto.md), [MC-Emitter](mc-emitter.md), [239-Bit Record Format](record-format.md), [TPUMCImm / SyImm32](tpumcimm-syimm32.md), [ArchRegno Numbering](archregno-numbering.md).
- **Per-generation bundles** — [Jellyfish](bundle-jf-41b.md), [Dragonfish](bundle-df.md), [Pufferfish](bundle-pf-51b.md), [Viperfish](bundle-vf-64b.md), [Ghostlite](bundle-gl.md), [6acc60406](bundle-gf.md).
- **Per-slot encodings** — the MXU / VPU / SPU / sequencer / memory / predicate / loop / immediate / EUP / matprep / mask / cmem / sparsity slot pages linked above, plus [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md).
- **Encode / decode support** — [IsaEmitter Registry](isa-emitter-registry.md), [Decode-Side JF/PF](decode-side-jf-pf.md), [Decode-Side VF/GXC](decode-side-vf-gxc.md), [NOP / Unused-Slot Canonical Encoding](nop-canonical.md), [kIsaTable Data Sections](kisatable-data-sections.md), [ResultFifo & ArchRegister](resultfifo-archregister.md), [Bias-Add & Quant/Dequant](bias-quantization-helpers.md), [XLU Op Roster](xlu-op-roster.md), [Pack/Unpack Precision](pack-unpack-precision.md).

The per-generation silicon families themselves (cost models, sub-core taxonomy, address-space IDs) live in the targets Part — start at [Targets Overview](../targets/overview.md).

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the VLIW bundle, slot taxonomy, and simultaneous-issue semantics this page summarizes.
- [LloOpcode Enum](llo-opcode-enum.md) — the 462-value `LloOpcodeProto` instruction vocabulary.
- [InstBits Master DB](instbits-master-db.md) — the base-bits, descriptor, name, and register-encoding tables the MC emitter reads.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr`, the per-opcode dispatch, and the MC-vs-proto-bundle ownership line.
- [Targets Overview](../targets/overview.md) — the per-generation silicon families, cost models, and sub-core taxonomy.
