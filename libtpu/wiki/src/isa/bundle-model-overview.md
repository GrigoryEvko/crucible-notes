# Bundle Model

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

A TPU TensorCore is a VLIW machine: the sequencer fetches one fixed-width **bundle** per cycle and issues every slot in it simultaneously to a distinct execution unit. There is no runtime dependency tracking — the bundle word *is* the issue packet, and the compiler is responsible for proving that the slots in a bundle are independent. The bundle word is a flat byte buffer of a width fixed per silicon generation: **41 bytes** for Jellyfish (TPU v2), **51 bytes** for Pufferfish (TPU v4), and **64 bytes** for Viperfish (TPU v5), Ghostlite (TPU v6 lite), and 6acc60406 (TPU7x). These widths are not free parameters. Jellyfish is the extreme case — its 41-byte buffer is hard-pinned by the `operator new(0x29)` allocation inside `EncoderJf::EncodeBundleInternal` itself. Pufferfish returns 51 as an inline constant from `EncoderPfTensorCore::BundleSizeBytes`, and the 64-byte v5+ widths come back through a `(TpuVersion, TpuSequencerType)`-keyed codec-metadata table (with Viperfish/Ghostlite computing the size through a vtable call rather than an inline literal in the *encoder* `BundleSizeBytes`). The encode path then lays slots into the buffer at generation-fixed byte/bit offsets.

> **NOTE — bit-numbering convention.** Every absolute bit position on this page and the slot pages it links is **LSB-first**: bit 0 is the least-significant bit of byte 0, and the universal v5+ packer `BitCopy(dst, dst_bit, src, src_bit, nbits)` (`0x1fa0a900`) writes `nbits` starting at the LSB-numbered `dst_bit`. The Jellyfish direct-pack encoder uses the same convention via `shl`/`or` shift constants. There is no MSB-first / big-endian bit ordering anywhere in the encode path.

The bundle is the wire form, not the compiler IR. Above it sits the [LLO instruction stream](llo-opcode-enum.md): a sequence of typed `LloInstruction`s, each one opcode + operands + memory-space + predicate. The [bundle packer](../sched/llo-bundle-packing.md) groups independent LLO ops into a `Bundle` object whose typed sub-fields (`ScalarInstruction`, `VectorAluInstruction`, `VectorExtendedInstruction`, `VectorLoadInstruction`, `VectorStoreInstruction`, `VectorResultInstruction`, `MiscInstruction`, plus the bundle header) map one-to-one onto the hardware slots; the per-generation `Encoder<gen>::EncodeBundleInternal` then serializes that `Bundle` into the fixed-width byte buffer. This page documents the model: what a bundle is, how wide each generation's bundle is and how that width is determined in the binary, the slot taxonomy and which engine each slot drives, the empty-slot (`kNeverExecute`) convention, and the encode/decode round-trip at a high level. The per-slot bit layouts live on the [individual slot pages](slot-mxu.md); the per-generation full slot maps live on the [per-gen bundle pages](bundle-jf-41b.md).

For reimplementation, the contract is:

- The three bundle byte-widths (41 / 51 / 64) and that they are selected by a `(TpuVersion, TpuSequencerType)` codec-metadata lookup, not by byte-extending a narrower bundle.
- The slot taxonomy: which typed `Bundle` sub-instruction drives which engine (MXU / VPU / SPU / sequencer / memory / predicate / loop / immediate / EUP), and how the count grows across generations.
- The simultaneous-issue semantics: no intra-bundle write-to-read forwarding; the compiler must serialize true dependencies across bundles or via sync flags.
- The empty-slot convention: unused slots carry the `kNeverExecute` predicate written into the bundle header, not arbitrary garbage.
- The encode/decode pair: `Encoder<gen>::EncodeBundleInternal` writes the byte buffer; `Decoder<gen>::Decode*` reconstructs the `Bundle`.

| | |
|---|---|
| **Bundle object** | `platforms_deepsea::jellyfish::isa::Bundle` (typed slot sub-fields) |
| **Wire form** | flat byte buffer, width fixed per `TpuVersion` |
| **Jellyfish (v2)** | **41 B** — hard-pinned by `operator new(0x29)` in `EncoderJf::EncodeBundleInternal` @ `0x1e86c7c0`; `JellyfishCodecMetadata::BundleSizeBytes` @ `0x1ecf7460` also returns `41` |
| **Dragonfish (v3)** | **41 B** — shares the Jellyfish/`EncoderDf` codec path (`CreateEncoderJfDf` @ `0x1e835b80`) |
| **Pufferfish (v4)** | **51 B** — `EncoderPfTensorCore::BundleSizeBytes` @ `0x1d227740` returns `51`; `PufferfishCodecMetadata::BundleSizeBytes` @ `0x1ecf7ac0` returns `51` |
| **Viperfish (v5)** | **64 B** — `ViperfishCodecMetadata::BundleSizeBytes` @ `0x1ee71320` returns `64`; `EncoderVfTensorCore::BundleSizeBytes` @ `0x1d2f8520` reaches it via a vtable+48 call |
| **Ghostlite (v6 lite)** | **64 B** — `GhostliteCodecMetadata::BundleSizeBytes` @ `0x1eeb7640` returns `64`; `EncoderGlTensorCore::BundleSizeBytes` @ `0x1d332540` reaches it via a vtable+48 call |
| **6acc60406 (TPU7x)** | **64 B** — registered codec metadata; the `gfc` TensorCore codec built by `CreateEncoderGlGf` @ `0x1e831020` (case `version==5`) |
| **Width dispatch** | `codec_metadata::BundleSizeBytes(TpuVersion, TpuSequencerType)` @ `0x1ecf7180` → `GetMetadataOrDie` → vtable +16 |
| **Encoder factories** | `CreateEncoderJfDf` @ `0x1e835b80` (JF+DF), `CreateEncoderPf` @ `0x1e835cc0`, `CreateEncoderVf` @ `0x1e835dc0`, `CreateEncoderGlGf` @ `0x1e831020` (GL+GF) |
| **TpuVersion enum** | 6 values, `0`..`5` (`TpuVersionToString` @ `0x20b3a480` traps on `≥ 6`); external names from [Codename Matrix](../targets/tpu-version-codename-matrix.md) |
| **Bit numbering** | LSB-first; `BitCopy` packer @ `0x1fa0a900` |
| **Empty-slot mark** | `HardwareBundleBits::kNeverExecute` predicate, written to the bundle header |
| **Encode entry** | `Encoder<gen>::EncodeBundleInternal` (e.g. Jellyfish @ `0x1e86c7c0`) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## What a Bundle Is

A bundle is one VLIW issue word: a fixed-width sequence of bytes the sequencer fetches from IMEM, decodes into N parallel slots, and dispatches in a single cycle. Each slot drives one execution unit — the matrix unit, a vector-ALU lane, the scalar pipe, a memory port, the sequencer's control logic — and every slot in a bundle fires at once.

The model is the classic statically-scheduled VLIW, and the trap is the classic VLIW trap: there is no scoreboard. The raw findings frame it precisely — one sequencer issues N parallel operations per cycle, the compiler is responsible for legal bundle packing, and there is no runtime dependency tracking because the ISA assumes the compiler proved correctness. Two consequences a reimplementer must encode:

- **No intra-bundle forwarding.** If slot A writes register `X` and slot B in the *same* bundle reads `X`, B sees the *old* value (the ISA treats the result as undefined). A correct packer never places a true dependency inside one bundle.
- **Cross-bundle ordering is the compiler's job.** A real dependency is satisfied either by placing the consumer in a later bundle (sequential issue advances the program counter) or, for asynchronous engines (MXU completion, DMA), by a sync-flag / semaphore wait the sequencer slot issues. The MXU in particular is decoupled: a matrix push enters the systolic array and the result is read back many cycles later via the [result-FIFO slot](resultfifo-archregister.md), gated by latch bookkeeping the compiler tracks in `MxuAssigner` / `MxuSequence`.

> **GOTCHA — the bundle word is the issue packet, not a basic block.** A reimplementation that schedules LLO ops as if each instruction issues on its own cycle will under-fill bundles and miss the entire point of the architecture; one that packs dependent ops into the same bundle expecting register forwarding will produce silently wrong results. The bundle boundary is a hard cycle boundary for write→read visibility.

The compiler-side `Bundle` object models exactly one such word. It is not a generic instruction list — it is a record with one typed, optional sub-field per slot class. `EncoderJf::EncodeBundleInternal` (`0x1e86c7c0`) reads those sub-fields by their concrete C++ type — `platforms_deepsea::jellyfish::isa::ScalarInstruction`, `VectorAluInstruction`, `VectorExtendedInstruction`, `VectorLoadInstruction`, `VectorStoreInstruction`, `VectorResultInstruction`, `MiscInstruction` — and a present sub-field becomes a populated slot, an absent one becomes an empty slot.

---

## Per-Generation Bundle Widths

The bundle byte-width is fixed per `TpuVersion` and is the single most load-anchored fact in the model: each per-generation codec returns it as an inline constant. The TPU has exactly six versions — `tpu::TpuVersionToString` (`0x20b3a480`) indexes a 6-entry pointer table (`off_22011BF0`) and calls `LogMessageFatal` for any version `≥ 6` — and their codenames (`kJellyfish`, `kDragonfish`, `kPufferfish`, `kViperfish`, `kGhostlite`, `k6acc60406`) all appear verbatim in the binary's `.rodata`.

| `TpuVersion` | Codename | Public name | Bundle bytes | Width source | Confidence |
|---|---|---|---:|---|---|
| 0 | Jellyfish | TPU v2 | **41** | `JellyfishCodecMetadata::BundleSizeBytes` @ `0x1ecf7460` `return 41` | CONFIRMED |
| 1 | Dragonfish | TPU v3 | 41 | shares Jellyfish codec metadata (component 0) | HIGH |
| 2 | Pufferfish | TPU v4 | **51** | `EncoderPfTensorCore::BundleSizeBytes` @ `0x1d227740` `return 51`; `PufferfishCodecMetadata::BundleSizeBytes` @ `0x1ecf7ac0` `return 51` | CONFIRMED |
| 3 | Viperfish | TPU v5 | **64** | `ViperfishCodecMetadata::BundleSizeBytes` @ `0x1ee71320` `return 64` | CONFIRMED |
| 4 | Ghostlite | TPU v6 lite | **64** | `GhostliteCodecMetadata::BundleSizeBytes` @ `0x1eeb7640` `return 64` | CONFIRMED |
| 5 | 6acc60406 | TPU7x | 64 | registered codec metadata; same 64-B class as v5+ | MEDIUM |

The dispatch from a `TpuVersion` to its width is a two-step indirection, not a `switch`:

```c
// codec_metadata::BundleSizeBytes(TpuVersion v, TpuSequencerType t)  @ 0x1ecf7180
metadata = GetMetadataOrDie(v);            // 0x1ecf6f60 — abseil flat_hash_map lookup
return metadata->vtable[+16](t);           // virtual BundleSizeBytes(t) on the per-gen class
```

`GetMetadataOrDie` (`0x1ecf6f60`) keys a process-wide `CodecMetadataRegistry` — an `absl::flat_hash_map<tpu::TpuVersion, CodecMetadata const*>` built once via the `StaticMapBase` singleton — and dies with "Codec metadata not registered for TpuVersion" if the version was never registered. Each registered `CodecMetadata` is a per-generation class (`JellyfishCodecMetadata`, `PufferfishCodecMetadata`, `ViperfishCodecMetadata`, `GhostliteCodecMetadata`) whose `BundleSizeBytes` virtual returns the inline constant above. There is no separate `6acc60406`-named metadata symbol; the `k6acc60406` (TPU7x) version is served by a registered entry reusing the 64-byte v5+ class shape.

> **QUIRK — width is keyed by `(version, sequencer-type)`, and the second argument matters.** `BundleSizeBytes` takes a `TpuSequencerType`. For the TensorCore sequencer (component `0`) Jellyfish returns 41; for the BarnaCore sequencer (component `1`) the *same* `JellyfishCodecMetadata` returns **16** (`0x1ecf7460`: `if (t==1) return 16`), and any other component triggers a fatal "Unhandled component". The Pufferfish BarnaCore sub-cores have their own 32-byte bundles (`EncoderPfBarnaCoreSequencer::BundleSizeBytes` @ `0x1d229220` `return 32`, `EncoderPfBarnaCoreChannel` @ `0x1d22bb00` `return 32`). A reimplementation that treats "bundle width" as a per-chip scalar misses that one chip has several sequencer types with different bundle widths.

The widths are mutually incompatible wire formats. A 41-byte Jellyfish bundle does not survive being read as a 51-byte Pufferfish bundle even after byte-extension: each generation has its own `Encoder<gen>` / `Decoder<gen>` pair and its own slot-offset map. The width grows across generations because the slot count grows (more vector-ALU lanes, more vector-load ports, a dedicated cmem-load slot at Pufferfish, a predicate slot at 6acc60406), not because fields were padded.

---

## The HBM / DMA Chunk Wrapping

The bundle width is the *issue* width. The width at which bundles are stored in HBM and DMA'd to IMEM is slightly larger, and bundles are grouped into fixed chunks. Jellyfish makes both facts explicit.

`JellyfishCodecMetadata::BundleSizeBytesForHbm` (`0x1ecf74c0`) returns **42** for the TensorCore — one byte wider than the 41-byte issue bundle — i.e. each stored bundle carries a 1-byte chunk/metadata prefix the on-chip sequencer strips before issue. `EncoderJf::GetBundleByteOffset` (`0x1e86db80`) then computes a bundle's byte offset inside a DMA chunk as:

```c
// EncoderJf::GetBundleByteOffset(bundle_index n)  @ 0x1e86db80
if (n < 0 || n >= bundles_per_chunk)
    return error("Requested bundle number N exceeds limit of "
                 "bundles-per-chunk (...)");          // encoder_jf.cc:3202
offset = (n / 3) * 128                                // 3 bundles per 128-B chunk
       + (n % 3) * (BundleSizeBytes() + 2);           // 41 + 2 = 43-B stride within chunk
```

Two constants fall out, both matching the documented model: **3 bundles per DMA chunk** (the `n / 3` grouping) and a **128-byte chunk granularity**. The `BundleSizeBytes() + 2` stride (43 bytes for Jellyfish) is the per-bundle slot inside the chunk; three of them (129 bytes of payload) round up to the 128-byte aligned chunk with the chunk header. The "exceeds limit of bundles-per-chunk" assertion string is the binary's own name for the constant.

| Generation | Issue bundle (B) | HBM bundle (B) | Bundles / DMA chunk | Source | Confidence |
|---|---:|---:|---:|---|---|
| Jellyfish | 41 | 42 | 3 | `BundleSizeBytesForHbm` @ `0x1ecf74c0`; `GetBundleByteOffset` @ `0x1e86db80` | CONFIRMED |
| Pufferfish | 51 | (codec-metadata `ForHbm`) | 10 | `EncoderPfTensorCore::BundleSizeBytesForDma` @ `0x1d227760` | MEDIUM |
| Viperfish+ | 64 | 64 | 1 | per-gen `BundleSizeBytesForDma` (vtable) | MEDIUM |

> **NOTE —** the bundles-per-chunk count is generation-specific and shrinks as the bundle widens: a 41-byte bundle packs 3 to a 128-byte chunk, while a 64-byte v5+ bundle effectively fills a chunk on its own. A reimplementation that hard-codes "3 bundles per chunk" misaligns every v4+ HBM image.

---

## Slot Taxonomy

A bundle's slots partition across the TensorCore's execution units. The compiler-side `Bundle` object encodes the partition as one typed sub-instruction per slot class; the names below are the concrete C++ types `EncodeBundleInternal` dispatches on, anchored to their decompiled symbol use in the Jellyfish encoder.

| Slot class | `Bundle` sub-instruction | Drives | First gen | Slot page | Confidence |
|---|---|---|---|---|---|
| Sequencer / scalar | `ScalarInstruction` | SPU + control flow, loop counters, sync; one per bundle | Jellyfish | [SPU](slot-spu-scalar.md), [Sequencer](slot-sequencer.md) | CONFIRMED |
| Vector ALU (VPU) | `VectorAluInstruction` | VPU lanes; count grows per gen (quad on Viperfish) | Jellyfish | [VPU](slot-vpu.md) | CONFIRMED |
| EUP / transcendental | `VectorExtendedInstruction` | extended-unit transcendentals; also feeds the MXU (`DecodeVectorExtendedToMxuNum`) | Jellyfish | [EUP](slot-eup-transcendental.md), [MXU](slot-mxu.md) | CONFIRMED |
| Vector load | `VectorLoadInstruction` | memory-load port; triple on Viperfish | Jellyfish | [Memory-Load](slot-memory-load.md) | CONFIRMED |
| Vector store | `VectorStoreInstruction` | memory-store port | Jellyfish | [Memory-Store](slot-memory-store.md) | CONFIRMED |
| Vector result | `VectorResultInstruction` | MXU result-FIFO read (latch result → vreg) | Jellyfish | [ResultFifo](resultfifo-archregister.md) | CONFIRMED |
| Misc | `MiscInstruction` | mask/M-register, rotate-count, immediate set helpers | Jellyfish | [vcreate_mask](slot-vcreate-mask-mregister.md), [Immediate](slot-immediate.md) | CONFIRMED |
| Bundle header | `HardwareBundleBits` | per-bundle predicate + boundary bits | Jellyfish | [Predicate](slot-predicate.md) | CONFIRMED |
| cmem-load | `TensorCoreCmemLoad` | second-scratchpad load port | Pufferfish | [cmem_load](slot-cmem-load-pf.md) | CONFIRMED |
| Sparsity | SparseCore sub-bundle | SparseCore control | Viperfish | [Sparsity](slot-sparsity-v5plus.md) | HIGH |
| Predicate | dedicated predicate slot | up to 4 predicate-defining ops/bundle | 6acc60406 | [Predicate](slot-predicate.md) | MEDIUM |

The MXU is not a single "slot" in the same sense as the others. Matrix work enters the array through the EUP / vector-extended path and a set of latch/matpush operations (`MxuAssigner`, `MxuSequence`, `MatrixMultiplyAccumulateFunctor`, `DistributeMatpushesEvenly`), and the result is read back later through the vector-result slot. The [MXU slot page](slot-mxu.md) and [matprep/IAR/latch page](slot-matprep-iar-latch.md) cover that two-phase model.

### Slot-count growth across generations

The bundle widens because the slot population grows. The byte-widths are byte-anchored (above); the slot counts are reconstructed from the per-generation slot types and bundle width and are best treated as the *shape* of the growth rather than load-anchored scalars.

| Generation | Bundle B | Slots (approx) | What the wider word buys | Confidence |
|---|---:|---:|---|---|
| Jellyfish | 41 | ~9 | scalar + 1 VPU + EUP + load/store/result + misc + header | HIGH |
| Pufferfish | 51 | ~12 | adds the `cmem_load` slot; widens the vector-extended path | MEDIUM |
| Viperfish | 64 | ~16 | quad VPU, triple vector-load, `ScalarSubBundle`, SparseCore standard | MEDIUM |
| Ghostlite | 64 | ~16 | same capacity as Viperfish; opcode/encoding deltas, adds a vector-misc slot | MEDIUM |
| 6acc60406 | 64 | ~17 | adds a dedicated predicate slot (up to 4 predicate-defining ops) | LOW |

> **NOTE —** the slot counts (9 / 12 / 16 / 16 / 17) are *not* directly extracted constants. The three byte-widths (41 / 51 / 64) are inline-constant returns from `BundleSizeBytes`, but the slot counts are derived from the per-gen slot-type roster (which typed sub-instructions each `Bundle` exposes) plus the bundle width. They are graded HIGH for Jellyfish (where `EncodeBundleInternal` enumerates the seven typed slot classes plus three header carriers) and MEDIUM/LOW for later generations until each per-gen `EncodeBundleInternal` is individually traced. The per-gen bundle pages ([Jellyfish](bundle-jf-41b.md), [Pufferfish](bundle-pf-51b.md), [Viperfish](bundle-vf-64b.md)) carry the confirmed maps.

---

## Empty-Slot and NOP Convention

An unused slot is not zeroed-and-ignored garbage; it carries a defined "do nothing" encoding so the decoder can round-trip it. The mechanism is the **bundle predicate**. `EncoderJf::EncodeBundleInternal` opens by stamping the `kNeverExecute` predicate value across the bundle header fields before any slot is filled:

```c
// EncoderJf::EncodeBundleInternal prologue  @ 0x1e86c7c0
nx = HardwareBundleBits::kNeverExecute & 0x1F;        // 5-bit predicate field
// write kNeverExecute into the bundle's predicate sub-fields (header bits)
bundle[+45] = (nx << 53) | (nx << 26);
bundle[+29] = kNeverExecute << 11;
bundle[+22] = 32 * nx;
// ... then fill present slots, leaving absent slots at kNeverExecute
```

A populated slot then overwrites its predicate field with the real `PredicateAndPolarity` value; a slot left untouched stays at `kNeverExecute`, which the hardware reads as "this slot's op is predicated off this cycle". The polarity helpers `PredicateAndPolarity::IsAlwaysExecute` (`0x1d5a6ac0`) and `IsNeverExecute` (`0x1d5a6b00`) are the runtime queries for the two extreme predicate values. This is the canonical empty-slot encoding — see [NOP / Unused-Slot Canonical Encoding](nop-canonical.md) for the full per-slot picture, including why a deterministic empty encoding matters for bit-exact testing.

> **GOTCHA — empty does not mean all-zero.** Because the default predicate stamp is `kNeverExecute` (a nonzero 5-bit pattern replicated into several header fields), a freshly initialized bundle is *not* a zero buffer. A reimplementation that memsets the bundle to zero and then fills only the active slots leaves the inactive slots at predicate-0 (which may be "always execute"), turning empty slots into live garbage ops.

---

## Encode / Decode Round-Trip

The bundle has a symmetric pair: an encoder that serializes a `Bundle` into the byte buffer and a decoder that reconstructs the `Bundle` from the bytes. Both are per-generation.

```text
LLO stream ──(bundle packer)──▶ Bundle (typed slots) ──▶ Encoder<gen>::EncodeBundleInternal ──▶ N-byte word
                                                                                                   │
N-byte word ──▶ Decoder<gen>::Decode* ──▶ Bundle (typed slots) ──(disassembly / verification)─────┘
```

- **Encode.** `Encoder<gen>::EncodeBundle` (public, takes a `BundleFacade` proxy) calls `EncodeBundleInternal`, which writes each present slot at its generation-fixed byte offset and leaves absent slots at `kNeverExecute`. Jellyfish: `EncoderJf::EncodeBundleInternal` @ `0x1e86c7c0`. The newer generations route through `TpuCodec<gen>::EncodeBundle` (e.g. `TpuCodecViperfish::EncodeBundle` @ `0x1e8449a0`), which dispatches by `TpuSequencerType` to the right `Encoder<gen>` instance.
- **Decode.** `Decoder<gen>::Decode<Slot>Instruction` reads the byte window back into the typed sub-field — e.g. `DecoderJf::DecodeVectorLoadInstruction` (`0x1e84c4a0`), `DecoderJf::DecodeVectorStoreInstruction` (`0x1e84b600`), `DecoderDf::DecodeVectorExtendedToMxuNum` (`0x1e849680`). The top-level `tpu::DecodeBundleBytes<DecoderPfTensorCore>` (`0x1e843de0`) takes an `absl::Span<const uint8_t>` and returns a `BundleFacade` proxy. The decode side is documented in [Decode-Side: JF / PF](decode-side-jf-pf.md) and [Decode-Side: VF / GXC](decode-side-vf-gxc.md).

For the BarnaCore-Pxc (Pufferfish) path, the per-slot bits additionally flow through the LLVM-MC [239-bit record](record-format.md) and `insertBits`; for every TensorCore and V5+ opcode the MC record is built all-zero and the real bytes come entirely from this proto-bundle `Encoder<gen>` path. The relationship between the MC record and the bundle word — one is per-instruction, the other per-bundle — is detailed in [Record Format](record-format.md#relationship-to-the-bundle-byte-widths).

---

## Cross-References

- [Jellyfish 41-Byte Bundle](bundle-jf-41b.md) — the full confirmed slot map for the 41-byte bundle (`EncodeBundleInternal`).
- [Record Format](record-format.md) — the 239-bit MC `APInt` record; how the per-instruction record relates to the per-bundle word.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr`; the BarnaCore-Pxc path that flows bits through the record, and the zero-base default for V5+.
- [LLO Bundle Packing](../sched/llo-bundle-packing.md) — the scheduler that groups independent LLO ops into the typed `Bundle` slots this page serializes.
