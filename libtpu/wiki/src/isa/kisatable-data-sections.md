# kIsaTable Data Sections

> *Every offset, value, address, and size on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). All addresses are virtual addresses; `.rodata` / `.lrodata` are mapped 1:1 (VA == file offset); `.data.rel.ro` has VA − file offset = `0x200000`. Other wheel versions differ.*

## Abstract

"kIsaTable" is the conventional name for the per-generation ISA-encoding descriptor data baked into a compiler's `.rodata` — the opcode/encoding tables an encoder and decoder index by opcode. libtpu has no symbol literally named `kIsaTable`. Instead the per-gen ISA-encoding role is **split across five concrete static structures**, each located, sized, and partially decoded: the per-gen `CodecMetadata` classes (bundle byte-widths, check bytes, chunk geometry), the LLVM TPU backend tables (`InstBits`, `TPUDescs`, `TPUInstrName*`, per-gen SchedModel tables), the per-gen `kNoopBundleBytes` NOP templates, the Ghostlite `kLloOpcodeToGlcInstruction` translation table, and the per-gen `Encoder*`/`Decoder*` classes that wrap the codec metadata. This page is the map of those sections: what each blob is, where it lives, how big it is, and how the encode/decode path indexes it.

The architectural fact a reimplementer needs first is that **the encoding data is not one monolithic table indexed by `(gen, opcode)`**. The LLVM-MC `InstBits` array is shared across generations but populated only for the Pufferfish BarnaCore HwMode (every TensorCore and V5+ row is all-zero — see [InstBits Master DB](instbits-master-db.md)); the real V5+ bundle bits are produced by per-gen proto-bundle `Encoder<gen>` classes whose geometry comes from the `CodecMetadata` registry, not from `InstBits`. So "the kIsaTable" is really a small federation: a registry of per-gen codec-metadata vtables for bundle geometry, the LLVM TableGen `.lrodata` region for the MC-layer descriptor/name/sched tables, three static NOP templates, and one Ghostlite-specific opcode-translation array.

This page documents the federation by its members — the namespace/generation map, the `CodecMetadata` class and its constants per gen, the LLVM backend table inventory with addresses and sizes, the NOP templates, and the Ghostlite LLO→GLC table — and explains how the encoder reaches each. It is a catalog page: confirmed addresses and sizes, with the algorithm that indexes each blob, rather than a dump of the 181 KB of base bits.

For reimplementation, the contract is:

- The **generation → namespace → LLVM-target map** (6 codenames, 3 HAL families, `TpuVersion 0..5`) — every encoder/table symbol is named by this taxonomy.
- The **`CodecMetadata` per-gen class** (7 virtuals) is the bundle-geometry "kIsaTable": `BundleSizeBytes`, `…ForHbm`, `HasCheckByteForHbm`, `BundleCheckByte`, `…Mask`, `BundleChunkSize`, `BundlesPerChunk`, keyed by `(TpuVersion, TpuSequencerType)` through a process-wide registry.
- The **LLVM TPU backend `.lrodata` tables** (`InstBits` @ `0x3366d90`, `TPUDescs` @ `0x33bf650`, `TPUInstrNameData`/`Indices`, per-gen SchedModel `SchedClasses`/`ProcResources`) — the MC-layer descriptor federation, indexed by `opcode − 499`.
- The **per-gen `kNoopBundleBytes`** (64-byte V5+ NOP templates) and that pre-V5 gens build NOPs dynamically with `kNeverExecute = 31`.
- The **Ghostlite `kLloOpcodeToGlcInstruction`** (258-entry sorted `(u16, u16)` array @ `0x4067dc8`) — the only static per-gen LLO→ISA translation table; other gens use switch statements.

| | |
|---|---|
| **Literal `kIsaTable` symbol** | none — role split across 5 structures |
| **Generations** | Jellyfish / Dragonfish / Pufferfish / Viperfish / Ghostlite / 6acc60406 (TPU7x) (`TpuVersion 0..5`) |
| **Codec registry** | `CodecMetadataRegistry` (`absl::flat_hash_map<TpuVersion, pair<char*, CodecMetadata*>>`, `StaticMapBase` singleton) |
| **Codec lookup** | `GetMetadataOrDie(TpuVersion)` → dies *"Codec metadata not registered for TpuVersion"* |
| **MC base-bits** | `InstBits` @ `0x3366d90` (`0x2c460` B) / `InstBits_BarnaCorePxcHwMode` @ `0x33931f0` |
| **MC descriptors** | `TPUDescs` @ `0x33bf650` (`0x33590` B); `TPUInstrNameData` @ `0x33f2be0`; `TPUInstrNameIndices` @ `0x3435d30` |
| **NOP templates** | `kNoopBundleBytes` — vxc @ `0xb846d64`, gxc::glc @ `0xb862ff4`, gxc::gfc @ `0xb88580a` (64 B each) |
| **Ghostlite xlat** | `xla::ghostlite::kLloOpcodeToGlcInstruction` @ `0x4067dc8` (`0x408` B = 258 × 4) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Generation and Namespace Map

Every table and encoder symbol is named by a three-axis taxonomy: codename, HAL family, and internal C++ namespace. The map is recovered from symbol demangling and the embedded `codec_metadata_<gen>.cc` source-path anchors.

| Codename | `TpuVersion` | Family | Internal namespace | LLVM subtarget |
|---|---|---|---|---|
| Jellyfish | 0 | JXC | `platforms_deepsea::jellyfish::isa` | `TPUSubtarget` (base) |
| Dragonfish | 1 | JXC | (shares `JellyfishCodecMetadata`) | base |
| Pufferfish | 2 | PXC | `asic_sw::deepsea::pxc::isa` | base + `TPUBcSubtarget` |
| Viperfish | 3 | VXC | `asic_sw::deepsea::vxc::isa` | `TPUVfcSubtarget` |
| Ghostlite | 4 | VXC=GXC | `asic_sw::deepsea::gxc::glc::isa` | `TPUGlcSubtarget` |
| 6acc60406 (TPU7x) | 5 | VXC=GXC | `asic_sw::deepsea::gxc::gfc::isa` | `TPUGfcSubtarget` |

The SparseCore sub-namespaces (`vxc::vfc::isa`, `gxc::glc::isa::sparsecore`, `gxc::gfc::isa::sparsecore`) and the Pufferfish BarnaCore (`pxc::pfc::isa`) carry their own encoders. The `(TpuVersion, TpuSequencerType)` pair is the full key into the codec metadata — one chip has several sequencer types with different bundle widths (see [Bundle Model](bundle-model-overview.md)). The eight `TpuSequencerType` values are `TC=0, BCS=1, BCAH=2, SCS=3, TAC=4, TEC=5, SCv0=6, SCv0AH=7`; the presence matrix per gen is documented on [Bundle Model](bundle-model-overview.md#per-generation-bundle-widths).

> **NOTE —** The v5 generation appears in the binary only as the codename `6acc60406` (the `tpu::TpuVersion::k6acc60406` enumerator, `TPU_VERSION_6acc60406`, `xla_target_6acc60406`, and the `.../target/6acc60406/` source paths); the marketing names "Trillium" and "Ironwood" occur **0 times** in `libtpu.so`. This page uses the binary codename, glossed `(TPU7x)` once.
>
> Ghostlite (`glc`) and 6acc60406 (`gfc`) are both the GXC family, which is why their symbols share the `gxc::` prefix. The one structural delta visible in the tables: there is a `SparseCoreTacGL*` SchedModel but **no `SparseCoreTacGF*`** — 6acc60406 has no TAC sequencer (confirmed by symbol absence below).

---

## CodecMetadata — the Bundle-Geometry "kIsaTable"

The closest thing to a per-gen `kIsaTable` is the `CodecMetadata` class: one subclass per generation, derived from the abstract `platforms_deepsea::jellyfish::isa::codec_metadata::CodecMetadata` base, exposing seven virtuals keyed by `TpuSequencerType`. The vtable layout is fixed (confirmed by the `BundleSizeBytes` thunks dereferencing `*((vtable) + N)`):

| vtable slot | virtual | meaning |
|---|---|---|
| `+16` | `BundleSizeBytes(seq)` | bytes per bundle on the wire (DMA form) |
| `+24` | `BundleSizeBytesForHbm(seq)` | bytes per bundle stored in HBM (may add check byte) |
| `+32` | `HasCheckByteForHbm(seq)` | does the HBM form append a check byte? |
| `+40` | `BundleCheckByte(seq)` | expected check-byte value (`0x55` universally) |
| `+48` | `BundleCheckByteMask(seq)` | mask before compare (`0xFF` universally) |
| `+56` | `BundleChunkSize(seq)` | on-wire chunk size in bytes |
| `+64` | `BundlesPerChunk(seq)` | bundles per chunk |

### The Registry and Lookup

The per-gen instances are entered into a process-wide `CodecMetadataRegistry` — an `absl::flat_hash_map<tpu::TpuVersion, pair<char const*, CodecMetadata const*>>` built once via a `util_registration::StaticMapBase` singleton. Four global constructors register the four codec classes; **`GetMetadataOrDie(TpuVersion)`** (@ `0x1ecf6f60`) keys the map and dies with *"Codec metadata not registered for TpuVersion: "* on a miss. The free-function dispatch wrappers (`codec_metadata::BundleSizeBytes(TpuVersion, TpuSequencerType)` @ `0x1ecf7180`, and the six siblings at `0x1ecf71a0…0x1ecf7240`) call `GetMetadataOrDie`, then index the resolved instance's vtable:

```c
// codec_metadata::BundleSizeBytes(TpuVersion v, TpuSequencerType t)  @ 0x1ecf7180  (dispatch)
md = GetMetadataOrDie(v);          // flat_hash_map lookup; LogFatal on miss
return md->vtable[+16](t);          // virtual BundleSizeBytes(t) on the per-gen class
```

| registering ctor | registers |
|---|---|
| `_GLOBAL__sub_I_codec_metadata_jellyfish.cc` @ `0x213673e0` | JF=0 **and** DF=1 (same instance) |
| `_GLOBAL__sub_I_codec_metadata_pufferfish.cc` @ `0x21367470` | PF=2 |
| `_GLOBAL__sub_I_codec_metadata_viperfish.cc` @ `0x213674c0` | VF=3 |
| `_GLOBAL__sub_I_codec_metadata_ghostlite.cc` @ `0x21367510` | GL=4 |

Jellyfish registers **two** keys (0 and 1) at the *same* `JellyfishCodecMetadata` instance — Dragonfish (v1) reuses the Jellyfish (v0) codec. There is **no** `_GLOBAL__sub_I_codec_metadata_6acc60406` constructor: 6acc60406 has no registered codec metadata, so a `GetMetadataOrDie(5, …)` would fatally abort. 6acc60406 bundle geometry is reached only through the type-erased `EncoderBase<…>` template vtables in `gxc::gfc::isa` (which forward `BundleSizeBytes` through their own `vtable[+48]`), never via the registry.

### Per-Gen Constants

Decompiled directly from the `BundleSizeBytes` virtuals — the `(seq, return)` branches are exact:

```c
// JellyfishCodecMetadata::BundleSizeBytes  @ 0x1ecf7460
if (seq == 0) return 41;               // TensorCore
if (seq == 1) return 16;               // BarnaCoreAddressHandler
LogFatal("Unhandled component");       // codec_metadata_jellyfish.cc:19

// ViperfishCodecMetadata::BundleSizeBytes  @ 0x1ee71320
if (seq != 0) LogFatal("Unhandled component");  // codec_metadata_viperfish.cc:20
return 64;                             // TensorCore
```

| Gen (seq=TC) | `BundleSizeBytes` | check byte | chunk | bundles/chunk | metadata @ |
|---|---:|---|---:|---:|---|
| Jellyfish / Dragonfish | 41 | `0x55` | 128 | 3 | `0x1ecf7460…` |
| Pufferfish | 51 | `0x55` | 51 | 1 | `0x1ecf7ac0…` |
| Viperfish | 64 | `0x55` | 64 | 1 | `0x1ee71320…` |
| Ghostlite | 64 | `0x55` | — | — | `0x1eeb7640…` |
| 6acc60406 | 64 | (template; no registry) | — | — | (no class) |

The `…ForHbm` virtual fatally aborts for `seq=0` on Pufferfish/Ghostlite (their TensorCore HBM path goes through `EncoderPfTensorCore` / `EncoderGlTensorCore` directly, not this metadata); Jellyfish's `…ForHbm` returns 42 (41 + 1 check byte). The per-gen full geometry — including the HBM `+1` check byte and the `(n/3)*128 + (n%3)*43` Jellyfish chunk layout — is on [Bundle Model](bundle-model-overview.md#the-hbm--dma-chunk-wrapping).

---

## LLVM TPU Backend Tables

The compiler ships a full LLVM TPU back end embedded in libtpu, whose TableGen-emitted tables sit in one contiguous `.lrodata` region (and a few in `.data.rel.ro`). These are the MC-layer descriptor federation — the per-opcode encoding bits, descriptors, mnemonics, register encodings, and per-gen scheduling models. They are indexed by `opcode − 499` (the first 499 opcodes are MC pseudos).

| Symbol | Address | Size | Purpose |
|---|---|---|---|
| `…getBinaryCodeForInstr::InstBits` | `0x3366d90` | `0x2c460` (181344 B = 5667 × 32) | base bits, TensorCore mode (all-zero) |
| `…InstBits_BarnaCorePxcHwMode` | `0x33931f0` | `0x2c460` | BarnaCore variant (704 populated rows) |
| `llvm::TPUDescs` | `0x33bf650` | `0x33590` | per-opcode `MCInstrDesc` (operand types, flags) |
| `llvm::TPUStages` | `0x343bd90` | `0x7c8` | pipeline-stage table |
| `llvm::TPUInstrNameData` | `0x33f2be0` | `0x4314c` | mnemonic string pool (~270 KB) |
| `llvm::TPUInstrNameIndices` | `0x3435d30` | `0x6058` | opcode → byte offset into NameData |
| `llvm::TPURegDesc` | `0x343e7b0` | `0x5358` | register descriptors |
| `llvm::TPURegStrings` | `0x343cde0` | `0x19c9` | register name strings |
| `llvm::TPURegEncodingTable` | `0x34469b0` | `0x6f2` | reg# → HW encoding (`889 × u16`) |
| `llvm::TPURegClassInfos` | `0x334ea60` | `0x800` | register-class metadata |
| `llvm::TPUFeatureKV` | `0x21934550` | `0x480` | `SubtargetFeature` KV (~48 × 24 B) |
| `llvm::TPUSubTypeKV` | `0x21934ca0` | `0x3f0` | subtype/CPU KV (~42 × 24 B) |

`InstBits` is the LLVM-MC slice of the federation; it is documented in full on [InstBits Master DB](instbits-master-db.md), including its counter-intuitive all-zero default table (the TensorCore and V5+ rows carry no bits — those bundles are encoded by the proto-bundle `Encoder<gen>` path, not by `getBinaryCodeForInstr`). The indexing arithmetic (`(opcode − 499) × 32` bytes, 5667 rows, 239-bit `APInt`) is on [239-Bit Record Format](record-format.md). The names JSON confirms the TPU `InstBits` symbol sits alongside `AArch64`/`AMDGPU`/`ARM`/`R600`/`PPC` `InstBits` — the binary embeds several LLVM targets, and the TPU one is the relevant member.

### Per-Gen SchedModel Tables

Each non-TensorCore sequencer gets a `SchedClasses` table (`0x79a` = 1946 B each) and a `ProcResources` table (per-cycle resource bitmasks, `16 B` per `MCProcResourceDesc` entry). The set of symbols *is* the per-gen sequencer inventory:

| Sequencer × gen | SchedClasses @ | ProcResources @ / size |
|---|---|---|
| BarnaCore (PF) | `0x3447630` | `0x21935740` / `0xa0` (10 PR) |
| SCS — 6acc60406 (GF) | `0x3448350` | `0x219357e0` / `0x320` (50 PR) |
| SCS — Ghostlite (GL) | `0x3449070` | `0x21935b00` / `0x300` (48 PR) |
| SCS — Viperfish (VF) | `0x3449d90` | `0x21935e00` / `0x2a0` (42 PR) |
| TAC — Ghostlite (GL) | `0x344a530` | `0x219360a0` / `0x300` |
| TAC — Viperfish (VF) | `0x344acd0` | `0x219363a0` / `0x2a0` |
| TEC — 6acc60406 (GF) | `0x344b470` | `0x21936640` / `0x320` |
| TEC — Ghostlite (GL) | `0x344bc10` | `0x21936960` / `0x300` |
| TEC — Viperfish (VF) | `0x344c3b0` | `0x21936c60` / `0x2a0` |

The `ProcResources` byte-size grows monotonically — VF `0x2a0` (42 units), GL `0x300` (48), GF `0x320` (50) — so 6acc60406 adds 8 functional units over Viperfish. The **absence of a `SparseCoreTacGF*` symbol** in the names table is the byte-level proof that 6acc60406 has no TAC sequencer (its SparseCore is SCS + TEC only). Each `TPUVfcSubtarget` / `TPUGlcSubtarget` / `TPUGfcSubtarget` overrides `getFifoDepth`, `getVyEncodings`, and `getSyEncodings` per gen.

---

## NOP Bundle Templates

The V5+ generations carry a static 64-byte NOP-bundle template in `.rodata`; pre-V5 gens build NOPs dynamically. The templates are the empty-bundle ground truth — a NOP fills every slot's predicate field with `kNeverExecute = 31` (`0x1F`) so the decoder round-trips an absent slot.

| Symbol (`(anonymous namespace)::kNoopBundleBytes`) | Address | Size | byte 63 | encoding style |
|---|---|---|---|---|
| `asic_sw::deepsea::vxc::isa` | `0xb846d64` | 64 B | `0x55` | `0x1F << shift` per slot predicate |
| `asic_sw::deepsea::gxc::glc::isa` | `0xb862ff4` | 64 B | `0x53` | more compact shift base (`e0 01`, `c0 03`) |
| `asic_sw::deepsea::gxc::gfc::isa` | `0xb88580a` | 64 B | `0x50` | all-zero body (zero-default slots) |

The Viperfish template's nonzero pattern (`00 3c`, `00 0f`, `00 f0`, `00 78`) is `0x1F` shifted to each slot's predicate bit offset, with byte 63 = the `0x55` check byte. Ghostlite uses a different (more compact) shift base, so its slot bit layout differs (byte 63 = `0x53`, not the literal check byte — the high bits carry the last slot's predicate ORed with the check field). **6acc60406's template is all-zero except byte 63 = `0x50`** — 6acc60406 arranges slots so all-zero means "present but inactive", not "active with an always-false predicate". Jellyfish and Pufferfish have *no* `kNoopBundleBytes` static; their `EncodeBundleInternal` zero-inits the buffer and patches `kNeverExecute = 31` into each predicate field at run time. The `kNeverExecute = 31` / `kAlwaysExecute = 15` / `kPredicateRegisterCount = 15` constants (`0xb834cf4…0xb834cff`) confirm a 5-bit Jellyfish predicate field. See [Bundle Model §Empty-Slot](bundle-model-overview.md#empty-slot-and-nop-convention).

---

## The Ghostlite LLO → GLC Translation Table

`xla::ghostlite::kLloOpcodeToGlcInstruction` (`0x4067dc8`, `0x408` B) is the **only per-gen static LLO→ISA translation table** in the `xla::` namespace: a 258-entry sorted array of `(uint16_t llo_opcode, uint16_t glc_instruction)` pairs, binary-searched by `llo_opcode`.

```c
// xla::ghostlite::kLloOpcodeToGlcInstruction  @ 0x4067dc8  (258 × 4 B, sorted ascending)
struct { uint16_t llo_opcode; uint16_t glc_instruction; } entries[258];
// e.g. {9, 118}, {10, 474}, {11, 474}, {13, 70}, ... {424, 23}, {425, 26}, {426, 25}
```

- LLO opcode coverage is sparse: `9..426` (258 of the ~418 range used).
- GLC instruction range is `0..475`.
- The mapping is **many-to-one** — e.g. LLO `10` and `11` both map to GLC `474`; LLO `37` and `39` both map to GLC `0` — so GLC opcodes are coarser-grained than LLO IR opcodes.

Why only Ghostlite is a static array: Jellyfish, Pufferfish, Viperfish, and 6acc60406 implement the LLO→ISA mapping as **switch statements** (e.g. `xla::jellyfish::LloOpcodeToIsaScalarOpcode` @ `0x140bc1e0`, a 162-case switch). Ghostlite's GLC mapping is wide enough (>200 entries) that a sorted-array binary search is preferred over a switch. The inverse LLO direction is the unnamed `LloOpcodeToProto` table @ `0x344cb4c` (`462 × u32`); the LLO opcode numbering itself (proto `1..499` → internal `0..461`) is reconstructed in `ProtoToLloOpcode` (`0x14420040`).

---

## What These Sections Do Not Hold

The federation is honest about its bounds:

- **The full per-opcode TensorCore / V5+ encoding bits.** `InstBits` is all-zero for those opcodes (no `.rela.dyn`, proven); the real bits come from per-gen `Encoder<gen>::EncodeBundleInternal` + `<Slot>Encoder::Encode` `BitCopy` calls, whose positions are the per-gen slot ladders ([Immediate Slot](slot-immediate.md), per-gen bundle pages). *(The 5667-case MC encoder switch and the inner per-slot encoder methods were not exhaustively enumerated — too large to enumerate by hand; MEDIUM on the un-walked V5+ slot encoders.)*
- **The 6acc60406 codec constants from a registry.** 6acc60406 has no registered `CodecMetadata`; its 64-byte geometry is read here from `kNoopBundleBytes` size and the `EncoderBase` template, not from a registry entry. *(HIGH, not CONFIRMED, for the 6acc60406 check byte.)*
- **The GLC instruction mnemonics.** The `kLloOpcodeToGlcInstruction` IDs (`0..475`) are decoded, but their names require cross-decoding `TPUInstrNameData` under the Ghostlite HwMode — not done here.
- **Per-gen `TPUFeatureKV` / `TPUSubTypeKV` contents.** Located and sized (~48 features, ~42 subtargets), but the individual feature/CPU strings were not enumerated.

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the `(TpuVersion, TpuSequencerType)` codec-metadata dispatch, per-gen bundle widths, and the empty-slot `kNeverExecute` convention.
- [InstBits Master DB](instbits-master-db.md) — the `InstBits` base-bits database (the LLVM-MC member of this federation), its all-zero default table, and the BarnaCore class field maps.
- [239-Bit Record Format](record-format.md) — the MC `APInt` record the `InstBits`/`TPUDescs` tables feed, and the `opcode − 499` indexing.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr`, the consumer that indexes `InstBits` and `TPUDescs`.
- [Immediate Slot](slot-immediate.md) — the per-gen immediate-slot ladders the V5+ proto-bundle encoders write (the bits `InstBits` does *not* hold).
- [TPUMCImm / SyImm32](tpumcimm-syimm32.md) — the MC immediate operand carried through the same backend tables.
