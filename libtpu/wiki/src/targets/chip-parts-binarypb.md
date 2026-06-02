# chip_parts.binarypb Decode

> *All addresses, offsets, and constant values on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped, ELF x86-64). Other versions differ.*

## Abstract

Every per-codename hardware constant that the TPU compiler needs — HBM/VMEM/SMEM/SFLAG capacities, MXU lane/sublane geometry, TensorCore and HBM clocks, register-file widths, DMA granules — is carried not in C++ literals but in a serialized protobuf blob, `<codename>_chip_parts.binarypb`, embedded directly in the `.lrodata` section of `libtpu.so`. At boot the runtime resolves the blob for the active `TpuVersion`, parses it into a `TpuChipPartsProto`, and copies the decoded fields into the `xla::jellyfish::Target` object that the cost model, ISA emitter, and topology layers read. This is a *data-driven HAL*: the same C++ `Target` class serves every generation, specialized only by the bytes it was loaded with.

The mechanism resembles LLVM's `TargetMachine` initialized from a `.td`-generated `SubtargetInfo` table, except the table is a runtime-loaded proto rather than a TableGen-baked struct. The proto schema lives in the binary's own `protodesc_cold` descriptor pool, so it can be recovered exactly; the blobs are 232–624 bytes each and decode byte-for-byte with no inference. The resolution function `TpuChipParts::DefaultsForVersion` builds an `embed://` resource path from the version string and reads it through `tsl::ReadBinaryProto`.

For reimplementation, the contract is:

- The **resource model**: nine `embed://tpu_chip_parts/<name>_chip_parts.binarypb` blobs registered through a 40-byte `FileWrapper` descriptor array, each with an on-disk size and md5 fingerprint and a relocated data pointer.
- The **resolution path**: `version -> TpuVersionToString -> AsciiStrToLower -> StrCat("embed://...") -> ReadBinaryProto -> FromProto`.
- The **proto schema**: `TpuChipPartsProto` plus its companion `TpuCorePartsProto`, `TpuSequencerPartsProto`, `TpuMemoryPartsProto`, `TpuSharedMemoryPartsProto`, and the `tpu_chip_enums.proto` enumerations, as a field-numbered table.

| | |
|---|---|
| **Resolver** | `tpu::TpuChipParts::DefaultsForVersion` @ `0x20b1b040` |
| **Source file (resolver)** | `learning/45eac/tpu/runtime/topology/tpu_chip_parts.cc:341` |
| **Parser** | `tpu::TpuChipParts::FromProto` @ `0x20b1b400`; `tpu::TpuMemoryParts::FromProto` @ `0x20b333a0` |
| **Descriptor array** | `.data.rel.ro` VA `0x22010ED0` (file off `0x21E10ED0`), 9 entries × `0x28` bytes |
| **Blob region** | `.lrodata` VA `0x0BDF29A0..0x0BDF3AB8` (VA == file offset) |
| **Proto schema** | `protodesc_cold`: `tpu_chip_parts.proto` @ `0xC18FD80` and companions |

---

## The Resource Model

### Purpose

`chip_parts` is the single source of truth for one TPU generation's hardware capability geometry. It is *not* the same thing as `chip_config` (see [TpuChipConfig](tpu-chip-config.md)): `chip_parts` describes what the silicon *is* (core counts, memory sizes, MXU dimensions, clocks), while `chip_config` describes a runtime *mode* (bounce buffers, sync-flag resources, on-device transfer windows). Both are `embed://` proto blobs, but they resolve through different functions and parse into different objects.

### Blob Layout in `.lrodata`

Nine blobs sit contiguously in `.lrodata`, where the section VA equals the file offset, so the bytes can be carved directly. Each is registered by a 40-byte `FileWrapper` descriptor in a 9-entry array at `.data.rel.ro` VA `0x22010ED0`. The descriptor layout is:

```text
struct FileWrapper {              // 0x28 bytes
    const char* name;   // +0x00  R_X86_64_RELATIVE reloc (0 on disk, filled at load)
    const void* data;   // +0x08  R_X86_64_RELATIVE reloc -> blob VA in .lrodata
    int64_t     size;   // +0x10  ON DISK (the serialized byte length)
    uint8_t     fp[16]; // +0x18  ON DISK (md5 fingerprint of the blob)
};
```

> **GOTCHA —** the `name` and `data` pointers read as **zero in the on-disk image**: they are `R_X86_64_RELATIVE` relocations the dynamic loader fills in at load time. Only `size` and the 16-byte md5 `fp` are literal on disk. A reader that trusts the on-disk pointer fields concludes the array is empty and that only the two blobs whose data happen to be examined directly are present. The correct read resolves the `data` reloc *addend* (which points at the real blob VA) and verifies it against the on-disk md5. Every one of the nine `fp` fields matches the md5 of the blob its reloc addend targets.

> **CORRECTION (CHIP-PARTS) —** an earlier analysis concluded that only the two `6acc60406` blobs were embedded and that the six older-codename resources existed solely as `.rodata` lookup-key strings ("the bytes are NOT embedded"). That was wrong, and the cause was exactly the reloc trap above. All nine blobs are embedded; all nine were md5-verified against their descriptor `fp` field and decoded byte-exactly. The per-codename pages now carry confirmed values, not "—(configs/C++)" placeholders.

The nine descriptors, in array order, with reloc-resolved data VAs:

| # | Resource key | desc VA | blob VA | size (B) | md5 (== `fp`) | first bytes |
|---|---|---|---|---:|---|---|
| 0 | `6acc60406_tensornode_chip_parts.binarypb` | `0x22010ED0` | `0x0BDF29A0` | 504 | `d1e4bea3…dec694a5` | `08 06` |
| 1 | `6acc60406_chip_parts.binarypb` | `0x22010EF8` | `0x0BDF2BA0` | 546 | `f5c490e6…02fd8029` | `08 06` |
| 2 | `ghostlite_chip_parts.binarypb` | `0x22010F20` | `0x0BDF2DD0` | 564 | `010c6352…13f5807f` | `08 05` |
| 3 | `viperfish_chip_parts.binarypb` | `0x22010F48` | `0x0BDF3010` | 601 | `fccc06a7…e84c9dcf` | `08 04` |
| 4 | `viperfish_lite_chip_parts.binarypb` | `0x22010F70` | `0x0BDF3270` | 232 | `a8e02254…064cb465` | `08 04` |
| 5 | `pufferfish_lite_chip_parts.binarypb` | `0x22010F98` | `0x0BDF3360` | 277 | `fb066c9a…d1ff501c` | `08 03` |
| 6 | `pufferfish_chip_parts.binarypb` | `0x22010FC0` | `0x0BDF3480` | 624 | `acdf3a9e…49af3fb2` | `08 03` |
| 7 | `jellyfish_chip_parts.binarypb` | `0x22010FE8` | `0x0BDF3700` | 435 | `f86192ba…c4adecda` | `08 01` |
| 8 | `dragonfish_chip_parts.binarypb` | `0x22011010` | `0x0BDF38C0` | 504 | `d3d51f67…a680f610` | `08 02` |

The `first bytes` column is `tag=0x08` (field 1, varint) followed by the `TpuVersionProto` value: jellyfish=1, dragonfish=2, pufferfish(+lite)=3, viperfish(+lite)=4, ghostlite=5, 6acc60406=6. The two `6acc60406` blobs differ only by package multiplicity — `tensornode` is one die (1 TensorCore, 2 SparseCores, 1 HBM stack); the bare `6acc60406` blob is the full two-die megachip (doubled counts). See [Per-Codename Constant Table](per-codename-hw-constants.md) for the full decode.

### Resolution Path

`DefaultsForVersion` constructs the resource name at runtime and reads it. The reconstructed logic:

```c
StatusOr<TpuChipParts> DefaultsForVersion(TpuVersion v, string_view variant):  // sub_20b1b040
    proto = TpuChipPartsProto()
    name  = AsciiStrToLower(TpuVersionToString(v))   // e.g. "jellyfish", "6acc60406"
    if variant.non_empty():                           // tensornode / lite selector
        StrAppend(&name, "_", variant)
    filename = StrCat("embed://tpu_chip_parts/", name, "_chip_parts.binarypb")
    status = tsl::ReadBinaryProto(Env::Default(), filename, &proto)   // tpu_chip_parts.cc:343
    CHECK(status == Ok) << "Failed to parse TpuChipPartsProto."
    return TpuChipParts::FromProto(proto)             // sub_20b1b400
```

So selecting `v=0` (jellyfish) yields the `jellyfish_chip_parts.binarypb` resource, `v=4` (ghostlite) the `ghostlite_…` blob, and so on. The `embed://` VFS scheme maps the resource name back to the `FileWrapper` whose `name` string matches; `ReadBinaryProto` then parses the blob's bytes. Because the name is computed from the version, every generation's blob is *live*, not dead — the resolver builds a valid key for each.

> **NOTE —** `DefaultsForVersion` `CHECK`-fails (fatal) on a parse error rather than returning a degraded default. There is no fallback geometry baked into C++; if the blob is absent or malformed, the runtime aborts. This is the architectural commitment behind the data-driven HAL: the proto *is* the hardware description.

---

## Recovered Proto Schema

The full schema was recovered from the `FileDescriptorProto`s in the binary's `protodesc_cold` pool (`tpu_chip_parts.proto` @ `0xC18FD80`, `tpu_core_parts.proto` @ `0xC190810`, `tpu_sequencer_parts.proto` @ `0xC191340`, `tpu_memory_parts.proto` @ `0xC191750`, `tpu_shared_memory_parts.proto` @ `0xC1919B0`, `tpu_chip_enums.proto` @ `0xC191B90`). Every field number below was confirmed by decoding the embedded blobs against it; the decode is byte-exact and self-consistent across all nine blobs.

### `TpuChipPartsProto` — top-level message

| Field | # | Type | Meaning |
|---|---:|---|---|
| `version` | 1 | `TpuVersionProto` | generation tag (1..6) |
| `cores` | 2 | repeated `Core` | one entry per core type (TensorCore + BarnaCore/SparseCore) |
| `shared_memories` | 3 | repeated `SharedMemory` | HBM (always) and CMEM (Pufferfish only) |
| `uhi_sync_flag_memory_parts` | 4 | `TpuMemoryPartsProto` | UHI sync-flag region (where present) |
| `local_shared_memory_mappings` | 5 | repeated `LocalSharedMemoryMapping` | core -> HBM stack topology |
| `dma_requirements` | 6 | `DmaRequirementsProto` | alignment / granule / max-single-DMA |
| `variant_name` | 7 | string | `"lite"` on lite blobs, empty otherwise |
| `misc` | 8 | `MiscPropertiesProto` | sync-flag feature flags |
| `driver_abi_version` | 9 | int64 | `1` on every gen |

### Nested and companion messages

```text
Core           { TpuCoreTypeProto type=1; TpuCorePartsProto parts=2; int32 count=3; }
SharedMemory   { TpuSharedMemoryTypeProto type=1; TpuSharedMemoryPartsProto parts=2; int32 count=3; }
DmaRequirementsProto { int64 host_alignment_bytes=1; int64 device_alignment_bytes=2;
                       int64 granule_bytes=3; int64 sync_flag_granule_bytes=4;
                       int64 max_single_host_dma_bytes=5; }
MiscPropertiesProto  { int32 max_slice_size_for_all_to_all_routing=1;
                       bool has_extra_done_bit_in_sync_flags=2;
                       bool is_host_sync_flag_access_async=3;
                       bool supports_sync_flag_mode_count_dones=4; }

TpuCorePartsProto    { TpuVersionProto version=1; TpuCoreTypeProto type=2;
                       repeated Sequencer sequencers=3; repeated Memory memories=4;
                       int32 frequency_mhz=5; int32 host_interrupt_count=6;
                       BarnaCore barna_core=7; SparseCore sparse_core=8; }
  Sequencer  { TpuSequencerTypeProto type=1; TpuSequencerPartsProto parts=2; int32 count=3; }
  Memory     { TpuMemoryTypeProto type=1; TpuMemoryPartsProto parts=2; int32 count=3; }
  SparseCore { int32 dreg_word_count=1; int32 dreg_bytes_per_word=2;
               int32 tile_hbm_bandwidth_bytes_per_cycle=3; int32 stream_granule_size=4; }

TpuSequencerPartsProto { TpuVersionProto version=1; TpuSequencerTypeProto type=2;
                         repeated Register registers=3; ScalarIsa scalar_isa=4;
                         VectorIsa vector_isa=5; BarnaCoreFsm barna_core_fsm=6; }
  Register  { TpuRegisterTypeProto type=1; int32 count=2; }
  VectorIsa { int32 lane_count=2; int32 sublane_count=3; int32 issue_latency_cycle_count=4;
              int32 mxu_count=5; int32 xlu_count=6; int32 iar_count=7; }

TpuMemoryPartsProto       { TpuVersionProto version=1; TpuMemoryTypeProto type=2;
                            bool holds_instructions=3; bool supports_dma=4;
                            int32 bytes_per_word=5; int64 word_base=6; int64 word_count=7;
                            int64 bundle_count=8; int64 bytes_per_instruction_dma_chunk=9;
                            int64 bundles_per_instruction_dma_chunk=10; }
TpuSharedMemoryPartsProto { TpuVersionProto version=1; TpuSharedMemoryTypeProto type=2;
                            int32 bytes_per_word=3; int64 word_count=4; int32 frequency_mhz=5;
                            int32 channel_count=6; int32 ports_per_channel=7;
                            int32 bytes_per_port=8; int64 bytes_per_second=9; }
```

> **QUIRK —** memory and shared-memory sizes are stored as `bytes_per_word × word_count`, never as a single byte count. HBM on `6acc60406` is `32 × 3,187,671,040 = 102,005,473,280 B` (95 GiB); reading `word_count` alone undercounts by the word factor. The "word" is the native access granule of that memory: 32 B for v5p/v6e/v7 HBM, 1024 B for v2/v3 HBM, 512 B for VMEM everywhere, 4 B for SMEM/SFLAG. `TpuMemoryParts::FromProto` validates each field (`bytes_per_word > 0`, `word_count > 0`, instruction memories must not set `word_base`/`word_count`, etc.) before packing the region into a 64-byte heap record (`operator new(0x40)`).

### Relevant enums (`tpu_chip_enums.proto`)

```text
TpuCoreTypeProto         : 1 TENSOR_CORE, 2 BARNA_CORE, 3 SPARSE_CORE
TpuSequencerTypeProto    : 1 TC_SEQ, 2 BC_SEQ, 3 BC_ADDR, 4 SC_SEQ, 5 SC_TAC, 6 SC_TEC
TpuRegisterTypeProto     : 1 SREG, 2 VREG, 3 PREG, 4 VMREG
TpuMemoryTypeProto       : 1 IMEM, 2 VIMEM, 3 TILEIMEM, 4 SMEM, 5 SFLAG, 6 TACSFLAG,
                           7 TECSFLAG, 8 VMEM, 9 TILESPMEM, 10 SPMEM, 11 TACSMEM, 12 TECSMEM
TpuSharedMemoryTypeProto : 1 HBM, 2 CMEM
TpuVersionProto          : 1=jellyfish 2=dragonfish 3=pufferfish 4=viperfish 5=ghostlite 6=6acc60406
```

---

## What chip_parts Carries (and What It Does Not)

`chip_parts` carries everything the `Target` capability surface needs: every memory size, every core/sequencer/register count, MXU `lane_count`/`sublane_count`/`mxu_count`/`xlu_count`/`iar_count`, TensorCore and HBM `frequency_mhz`, HBM `bytes_per_second`, and the DMA granule/alignment block. Two classes of constant are *not* in the proto:

- **VMEM/SMEM/CMEM bank counts.** These are C++ literals in the per-codename `*Target::MemBanks` overrides, not a proto field. See [Per-Codename Constant Table](per-codename-hw-constants.md).
- **`issue_latency_cycle_count`** (VectorIsa field 4) is absent (defaults to 0) in *every* blob, including all six older gens — the real MXU/VPU issue latency lives in the per-codename cost model, not chip_parts. The MXU systolic *depth* is likewise not an explicit proto field: the proto gives `lane_count=128` and `mxu_count`, but the 256×256 v6e/v7 systolic dimension is a separate `GhostliteTarget` C++ override.

> **NOTE —** there are no dtype/sparsity capability fields in this proto. Per-format peak FLOPS and supported precisions are encoded in the per-codename ISA and accuracy tables, not in `chip_parts`.

---

## Related Components

| Name | Relationship |
|---|---|
| `TpuChipConfig::Create` | parallel `embed://` resolver for `chip_config` (mode/resource) blobs, not capability geometry |
| `TpuChipParts::FromProto` | parses the blob into `TpuChipParts`; feeds `Target` field block `+0x438..+0x510` |
| `xla::jellyfish::Target` | the runtime object the decoded constants populate; read by cost model, ISA emitter, topology |

## Cross-References

- [Per-Codename Constant Table](per-codename-hw-constants.md) — the byte-exact decode of all nine blobs as a per-generation table
- [TpuChipConfig](tpu-chip-config.md) — the parallel `chip_config` resolver and the `Target` field block the decoded constants fill
- [Codename Matrix](tpu-version-codename-matrix.md) — TpuVersion ↔ codename ↔ marketing-name mapping
- [Per-Gen Comparison Matrix](../appendix/per-gen-comparison-matrix.md) — cross-page consolidated per-generation comparison
- [FileWrapper TOC Catalog](../appendix/filewrapper-toc-catalog.md) — the full embedded-resource catalog this descriptor array belongs to
- [Cost Model Overview](../cost/overview.md) — consumer of the decoded frequency and bandwidth constants
- [ISA Overview](../isa/overview.md) — consumer of the decoded MXU lane/sublane geometry
