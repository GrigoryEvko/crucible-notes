# MemorySpace Enum (17)

> *All addresses, enum integers, and string offsets on this page were decoded byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

Every LLO operand that names a memory region carries an `xla::jellyfish::MemorySpace` — a small integer enum that tells the allocator, the DMA emitter, and the bundle packer which physical (or virtual) pool a buffer lives in. It is the TensorCore-side analogue of the SparseCore address-space ID ([Address-Space IDs](../targets/address-space-ids.md)): one compact integer per pool, used as a dispatch key everywhere a buffer crosses a tier boundary. The enum has **17 named values** (`0`..`16`), and the canonical decoder is a one-instruction string-table lookup, `xla::jellyfish::MemorySpaceToString(MemorySpace)` at `0x1d6ffae0`, which reads `off_21CE6B08[ms]`.

The 17 values cover the five physical TensorCore tiers (`hbm`, `vmem`, `smem`, `sflag`, `imem`), the Pufferfish-only second scratchpad (`cmem`), the host-interface tier (`hib`), the four BarnaCore sub-spaces (`barna_core_{bmem,smem,sflag,imem}`), the two SparseCore-sequencer pools (`sparse_core_sequencer_{sflag,smem}`), the SparseCore private-stack region (`sparse_core_private_stack_hbm`), the host and pinned-host pools (`host`, `pinned_hbm`), plus the value-`0` "no memory space" sentinel. This is a *different* enumeration from both the LLVM-level SparseCore address-space IDs (the `addrspace(N)` integers in [Address-Space IDs](../targets/address-space-ids.md)) and — critically — from the wire-format `MemorySpaceProto` field numbers. The runtime C++ enum and the proto field numbers **disagree on the integer assignment** for several spaces; a serializer that conflates them mislabels every buffer. That divergence is the central trap this page documents.

For reimplementation, the contract is the 17-value runtime enum (the integers `MemorySpaceToString` indexes, which is what LLO operands and slot encodings carry), its 1:1 string table, the proto↔enum remap, the masked validity gates the DMA-id helpers use to reject non-addressable spaces, and the relationship to the SparseCore AS-ID space and the on-chip tier model.

| | |
|---|---|
| **Runtime enum** | `xla::jellyfish::MemorySpace` — 17 named values, `0`..`16` |
| **Decoder** | `xla::jellyfish::MemorySpaceToString(MemorySpace)` @ `0x1d6ffae0` (14 B) |
| **String table** | `off_21CE6B08` — 8-byte pointer array, `RELATIVE`-relocated, indexed by the enum int |
| **Wire format** | `xla.jellyfish.MemorySpaceProto` descriptor @ VA `0xbf8cc80` (641 B), 17 values |
| **Operand carrier** | `LloMemUnit` / `LloAddress` — a `(MemorySpace, offset)` pair on every memory LLO op |
| **Tag width** | 3 bits in the hardware bundle word (most slots address only a subset) |
| **Sibling AS-ID enum** | `mlir::sparse_core::MemorySpace` — 22 values, *unrelated numbering* |
| **Confidence** | CONFIRMED unless a cell is annotated otherwise |

---

## The 17-Value Enum

`MemorySpaceToString` is the ground truth for the integer→region mapping: it is a single `mov rax, [off_21CE6B08 + rax*8]`, so the enum value is a direct index into a pointer array, and the C-string at each slot is the canonical lowercase region name. The 17 slots (`0`..`16`) and the string each resolves to (after following its `R_X86_64_RELATIVE` reloc) are below; the "Region / tier" column maps the name to the physical memory it backs, drawn from the [Memory Hierarchy](../targets/memory-hierarchy.md) tier model and the BarnaCore/SparseCore sub-core taxonomy.

| Enum# | String (`off_21CE6B08[n]`) | Region / tier | Meaning | Confidence |
|------:|----------------------------|---------------|---------|------------|
| 0 | `<no memory space>` | — (sentinel) | unset / invalid; the default-constructed value | CONFIRMED |
| 1 | `hbm` | HBM (off-chip) | device-global backing store: program I/O, spill, embeddings | CONFIRMED |
| 2 | `hib` | Host-Interface Buffer | HBM↔host staging tier the HIB DMA engine drives | CONFIRMED |
| 3 | `vmem` | VMEM (per-TensorCore) | vector working set — MXU/VPU operands stage through it | CONFIRMED |
| 4 | `cmem` | CMEM (Pufferfish/v4 only) | second large chip-level scratchpad above VMEM | CONFIRMED |
| 5 | `smem` | SMEM (per-TensorCore) | scalar / sequencer scratchpad: addresses, loop bounds | CONFIRMED |
| 6 | `sflag` | SFLAG (per-TensorCore) | sync-flag words polled by DMA-completion and barriers | CONFIRMED |
| 7 | `imem` | IMEM (per-TensorCore) | instruction memory the sequencer fetches bundles from | CONFIRMED |
| 8 | `barna_core_bmem` | BarnaCore BMEM | BarnaCore (embedding-engine) bulk scratchpad | CONFIRMED |
| 9 | `barna_core_smem` | BarnaCore SMEM | BarnaCore scalar scratchpad | CONFIRMED |
| 10 | `barna_core_sflag` | BarnaCore SFLAG | BarnaCore sync-flag tier (distinct from TC SFLAG) | CONFIRMED |
| 11 | `barna_core_imem` | BarnaCore IMEM | BarnaCore instruction memory | CONFIRMED |
| 12 | `sparse_core_sequencer_sflag` | SparseCore sequencer SFLAG | SC-sequencer sync-flag bank | CONFIRMED |
| 13 | `host` | Host DRAM | host-resident buffer (transfer source/sink) | CONFIRMED |
| 14 | `sparse_core_sequencer_smem` | SparseCore sequencer SMEM | SC-sequencer scalar scratchpad | CONFIRMED |
| 15 | `sparse_core_private_stack_hbm` | SC private-stack HBM window | per-SC private stack carved from HBM | CONFIRMED |
| 16 | `pinned_hbm` | Pinned HBM | page-pinned HBM region for host-visible DMA | CONFIRMED |

> **GOTCHA —** the `MemorySpaceToString` table does **not** stop at index 16. Indices 17/18/19 resolve to `absolute` (VA `0x868144c`), `heap_relative` (`0x8678cad`), and `stack_relative` (`0x8678cbb`). These are pointer-*relativity* tags appended to the same string array, not members of `MemorySpaceProto` (which has exactly 17 values, `0`..`16` — see below). A reimplementation that sizes the `MemorySpace` enum by the string-table length, or that treats `absolute`/`heap_relative`/`stack_relative` as memory pools, is wrong: they belong to the `LloAddress` relocation model, not the region enum. The canonical region enum is 17 values; the string table is over-long because it shares storage with the relativity tags.

### Why a MemorySpace, not an address-space ID

The TensorCore LLO does not use the LLVM `addrspace(N)` integer for its memory operands the way the SparseCore lowering does. LLO runs on a `(MemorySpace, offset)` pair carried by `LloMemUnit` / `LloAddress`; the allocator assigns every buffer exactly one `MemorySpace` and the bundle packer encodes that space as a small tag in the slot. The two enums meet only at the SparseCore boundary, where `sparse_core_sequencer_{sflag,smem}` (enum 12/14) and `sparse_core_private_stack_hbm` (enum 15) name the SC-side pools that the [Address-Space IDs](../targets/address-space-ids.md) table also covers under its own `mlir::sparse_core::MemorySpace` numbering. The numberings are unrelated: SparseCore `smem` is AS-ID-derived MS `1`, whereas the LLO `smem` here is enum `5`. Keep the two enums in separate namespaces.

---

## The `MemorySpaceToString` Decoder

The decoder is the smallest non-trivial function in the subsystem and pins the table layout exactly:

```c
const char *MemorySpaceToString(MemorySpace ms):     // sub_1D6FFAE0, 14 bytes
    // mov eax, edi          ; zero-extend the enum int
    // lea rcx, off_21CE6B08 ; base of the 8-byte pointer array
    // mov rax, [rcx+rax*8]  ; load the C-string pointer at index ms
    return off_21CE6B08[ms]
```

There is no bounds check: the enum int is used as a raw array index. A caller passing an out-of-range `ms` reads adjacent rodata pointers (`absolute`, `heap_relative`, … and beyond), so callers that may hold an untrusted value mask first — `MemorySpaceToString((uint8_t)ms)` is the usual call shape (seen verbatim in the DMA-id helpers below). The 64 callers recovered for this function are the proof that `MemorySpace` is a first-class LLO operand attribute, not a backend implementation detail: they include `DmaEmitter::Emit`, `net_router::AddressToPointer`, `LloAddress::ToString`, `LloInstruction::ToString`, `LloAllocation::ToString`, every `<gen>Target::DmaAddressGranule(MemorySpace)`, and the `ProgramMemorySpaceSummary` reporters.

### String table provenance

`off_21CE6B08` lives in the writable image (second `LOAD`, `0x215f25e0`+) and each slot is filled at load by an `R_X86_64_RELATIVE` relocation pointing into `.rodata`; on the on-disk image the slots read as zero. The 17 region strings are not contiguous in `.rodata` — each is the null-terminated prefix of a larger concatenated string blob (e.g. `hbm` at `0x861833c` precedes `for opcode: kDmaHostToHbm…`), so they must be read up to the first `NUL`, not by fixed stride.

---

## Wire Format vs Runtime Enum — the Remap Trap

LLO instructions serialize through `MemorySpaceProto` (`platforms/xla/service/jellyfish/proto/memory_space.proto`, descriptor at VA `0xbf8cc80`). The proto enum and the runtime C++ enum name the **same 17 spaces** but assign them **different integers**. The proto field numbers come from declaration order in the `.proto`; the C++ enum is reordered so the contiguous TensorCore tiers (`hbm`, `hib`, `vmem`, `cmem`, `smem`, `sflag`, `imem`) sit at `1`..`7` ahead of the BarnaCore block, whereas the proto interleaves them differently.

| Region | C++ enum# (`MemorySpaceToString`) | Proto# (`MemorySpaceProto`) |
|--------|----------------------------------:|----------------------------:|
| NONE / `<no memory space>` | 0 | 0 (`MEMORY_SPACE_NONE`) |
| `hbm` | 1 | 1 (`…_HBM`) |
| `hib` | **2** | **10** (`…_HIB`) |
| `vmem` | **3** | **2** (`…_VMEM`) |
| `cmem` | **4** | **11** (`…_CMEM`) |
| `smem` | **5** | **3** (`…_SMEM`) |
| `sflag` | **6** | **4** (`…_SFLAG`) |
| `imem` | **7** | **5** (`…_IMEM`) |
| `barna_core_bmem` | **8** | **6** (`…_BARNA_CORE_BMEM`) |
| `barna_core_smem` | **9** | **7** (`…_BARNA_CORE_SMEM`) |
| `barna_core_sflag` | **10** | **8** (`…_BARNA_CORE_SFLAG`) |
| `barna_core_imem` | **11** | **9** (`…_BARNA_CORE_IMEM`) |
| `sparse_core_sequencer_sflag` | 12 | 12 (`…_SPARSE_CORE_SEQUENCER_SFLAG`) |
| `host` | 13 | 13 (`…_HOST`) |
| `sparse_core_sequencer_smem` | 14 | 14 (`…_SPARSE_CORE_SEQUENCER_SMEM`) |
| `sparse_core_private_stack_hbm` | 15 | 15 (`…_SPARSE_CORE_PRIVATE_STACK_HBM`) |
| `pinned_hbm` | 16 | 16 (`…_PINNED_HBM`) |

> **GOTCHA —** the two numberings agree at `0`, `1`, and `12`..`16`, but diverge across `2`..`11`. `hib` is `2` in C++ but `10` in the proto; `vmem` is `3` in C++ but `2` in the proto; `cmem` is `4` vs `11`. A reimplementation that round-trips an LLO buffer through `MemorySpaceProto` **must remap** between the two integers at the (de)serialization boundary — treating the protobuf field number as the runtime enum value silently relabels `vmem` buffers as `hib`, `cmem` as `vmem`, and so on. The boundary that must apply the remap is the `LloOpcodeProto` (de)serializer; the rest of the compiler operates on the C++ enum only.

> **CORRECTION (MS-1) —** the [Memory Hierarchy](../targets/memory-hierarchy.md) page states the `MemorySpaceToString` table has "18 entries". Decompilation shows the proper `MemorySpaceProto` region enum is **17 values** (`0`..`16`); the string table at `0x21CE6B08` is longer than 17 only because it shares storage with the `absolute`/`heap_relative`/`stack_relative` relativity tags at indices 17+. The region enum proper is 17, matching the proto descriptor exactly.

---

## How the Enum is Used in Operands and Slots

A memory LLO instruction (`kDmaHbmToVmem`, `kLoad`, `kStore`, the 36 DMA opcodes) carries its operand as an `LloMemUnit` / `LloAddress` = `(MemorySpace, byte-offset)`. The `MemorySpace` selects:

1. **The DMA opcode family.** `OpcodeFromDmaMemorySpace(Target, MemorySpace src, MemorySpace dst, …)` keys the DMA opcode (e.g. `kDmaHbmToVmem` vs `kDmaVmemToHbm`) off the `(src, dst)` pair — the same `(srcMS, dstMS)` dispatch shape the SparseCore lowering uses on AS-IDs.
2. **The address granule.** Each `<gen>Target::DmaAddressGranule(MemorySpace)` returns the per-space DMA alignment (the HBM word, the VMEM 512-B word, etc.) used to convert a byte offset to a hardware word address.
3. **The slot memory-space tag.** The bundle packer emits the `MemorySpace` as a 3-bit field inside the memory slot of the VLIW bundle (see [Memory-Load Slot](slot-memory-load.md) / [Memory-Store Slot](slot-memory-store.md)). Three bits cannot encode 17 values, so each slot type addresses only the subset of spaces its hardware port can reach — the validity gates below enforce that subset.

### The DMA-addressable subset (masked validity gates)

Two independent DMA-id helpers prove that only a *subset* of the 17 spaces is reachable as a DMA endpoint on a given target, and both gate on the **runtime enum** numbering:

```c
int DmaMemoryId(MemorySpace ms):                         // sub_1C4AC1C0, pufferfish::proto_utils
    if (uint8_t)(ms - 1) > 7 || ((0xDD >> (ms - 1)) & 1) == 0:
        LOG(FATAL) << "Unimplemented DMA." << ms          // MemorySpaceToString(ms)
    return dword_A2E7040[ms - 1]                          // local-mem-id lookup

int dma_utils::MemorySpaceToLocalMemId(MemorySpace ms):  // sub_1D5AE120, pufferfish::dma_utils
    if (uint8_t)(ms - 1) >= 0x0B || !bittest(0x5DD, ms - 1):
        LOG(FATAL) << "Unhandled memory space " << ms
    return byte_B53A8C8[ms - 1]
```

- `DmaMemoryId` accepts `ms-1 ∈ {0,2,3,4,6,7}` (mask `0xDD = 0b1101_1101`) → spaces `hbm(1)`, `vmem(3)`, `cmem(4)`, `smem(5)`, `imem(7)`, `barna_core_bmem(8)`.
- `MemorySpaceToLocalMemId` accepts `ms-1 ∈ {0,2,3,4,6,7,8,9,10}` (mask `0x5DD`) → the same six plus the three remaining BarnaCore spaces `barna_core_{smem(9),sflag(10),imem(11)}`.

A third helper, `ghostlite::GhostliteProtoUtils::MemorySpaceToLocalMemId` (`sub_1C5EF520`), collapses spaces to a 3-value local-mem id: `{hbm(1), vmem(3)} → 0`, `{hib(2), smem(5), sparse_core_sequencer_smem(14)} → 1`, `{imem(7)} → 2`, everything else `InvalidArgument`. The `hib`/`vmem`/`smem` cases here only line up with sensible local-mem ids under the **runtime** enum (`hib=2`, `vmem=3`, `smem=5`), which is the cleanest confirmation that the enum int — not the proto field number — is what flows through the emitter.

> **NOTE —** the masks are per-generation (Pufferfish `0xDD`/`0x5DD` above), so the reachable subset grows with silicon. The constant-array indices (`dword_A2E7040[ms-1]`, `byte_B53A8C8[ms-1]`) are `ms-1`-based because the `none` sentinel (`0`) is never a valid DMA endpoint, so the helpers subtract 1 before indexing the dense local-mem-id table.

---

## Relationship to the SparseCore AS-ID Space

The LLO `MemorySpace` enum and the SparseCore `mlir::sparse_core::MemorySpace` enum ([Address-Space IDs](../targets/address-space-ids.md)) are two distinct, separately-numbered enumerations that name overlapping physical memory. The TensorCore enum (this page) is what the `xla::jellyfish` LLO carries; the SparseCore enum is what the `LowerToSparseCoreLlvm` pass derives from the LLVM `addrspace(N)` integer. They intersect only at the SparseCore-sequencer and SC-private-stack pools:

| Physical pool | LLO `MemorySpace` (this page) | SparseCore AS-ID / MS (sibling page) |
|---------------|------------------------------:|--------------------------------------|
| SC-sequencer SFLAG | `sparse_core_sequencer_sflag` = 12 | AS-band sflag pools (`sflag`/`sflag_tile`/`sflag_scs`), MS 5/14/20 |
| SC-sequencer SMEM | `sparse_core_sequencer_smem` = 14 | `smem` family, MS 1/16/21 (AS 0/219/224) |
| SC private-stack HBM | `sparse_core_private_stack_hbm` = 15 | `hbm` family, MS 4/10 (AS 203/213) |

> **QUIRK —** the same word `smem` means different integers in the two enums: `5` in the LLO `MemorySpace` here, and MS `1` (AS `0`) in the SparseCore AS-ID table. A cross-subsystem analysis must qualify which enum a `MemorySpace` value comes from. The SparseCore enum is 1-based with a value-8 gap and 22 values; this LLO enum is 0-based, gapless, with 17 values. They are not convertible by arithmetic — only the named pools above correspond, and only by physical identity, not by integer.

---

## Related Components

| Name | Relationship |
|------|--------------|
| `MemorySpaceToString` @ `0x1d6ffae0` | the canonical enum-int → region-name decoder (`off_21CE6B08[ms]`) |
| `OpcodeFromDmaMemorySpace` (jellyfish anon-ns) | selects the DMA opcode from the `(srcMS, dstMS)` pair |
| `<gen>Target::DmaAddressGranule(MemorySpace)` | per-space DMA alignment / word geometry |
| `DmaMemoryId` @ `0x1c4ac1c0` / `MemorySpaceToLocalMemId` @ `0x1d5ae120` | masked validity gates → dense local-mem-id, per generation |
| `LloMemUnit` / `LloAddress` | the `(MemorySpace, offset)` operand pair on every memory LLO op |
| `MemorySpaceProto` @ VA `0xbf8cc80` | the serialized wire enum (17 values, *remapped* integers) |

## Cross-References

- [Address-Space IDs](../targets/address-space-ids.md) — the SparseCore `mlir::sparse_core::MemorySpace` (22 values) and its `addrspace(N)` ID space; the separately-numbered sibling enum
- [Memory Hierarchy](../targets/memory-hierarchy.md) — the HBM/VMEM/SMEM/SFLAG/CMEM physical-tier model these enum values name
- [Memory-Load Slot](slot-memory-load.md) — where the MemorySpace tag is encoded in the load slot of the bundle word
- [Memory-Store Slot](slot-memory-store.md) — the store-slot counterpart of the MemorySpace tag encoding
