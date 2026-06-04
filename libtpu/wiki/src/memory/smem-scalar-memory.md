# SMEM Scalar Memory

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. Other versions will differ.*

## Abstract

**SMEM** (scalar memory) is the on-chip SRAM tier private to each TensorCore's **Scalar Processing Unit (SPU)**. Where VMEM is the wide operand staging pool the MXU and VPU read from, SMEM is the SPU's narrow scratch: a flat, word-granular array that backs scalar-register spills, holds the function-argument (parameter-pointer) table, materializes loop counters and address-calculation results under register pressure, and carries the per-step completion descriptors the SPU writes. It is the rough equivalent of a CPU's stack-plus-spill-slots region — except it is explicitly addressed by dedicated scalar load/store opcodes, never implicitly via a stack pointer the hardware manages.

The reimplementer should hold one analogy and immediately complicate it. SMEM looks like a register-spill area, but it is **not** managed by the HBM↔VMEM cost-balancer. The [memory-space taxonomy](overview.md#1-the-six-region-taxonomy) labels SMEM `kSmem = 5` in the `MemorySpace` enum, and the [overview](overview.md) already established the two-stack story shared by all tiers: a compile-time placer (`xla::jellyfish::ProgramMemoryAllocator`) freezes offsets into a proto, and one `tpu::BestFitAllocator` per tier replays them at runtime. SMEM differs from VMEM in one decisive way: **MSA never colors SMEM**. `FastMemorySpace()` never returns `kSmem` on any Target (it is `kVmem` on Viperfish/Ghostlite, `kCmem` on Pufferfish, `kHbm` on Jellyfish), so SMEM is a *reserved tier* that XLA writes into by emitting scalar load/store opcodes whose operand `memory_space()` is declared `kSmem`, not by the `kAlternate`/`kDefault` tug-of-war. Placement is opcode-semantics-driven, not cost-driven.

This page owns the SMEM **scalar model**: the address space and its per-generation sizing, the byte-flat-vs-word-flat duality, the `Sld`/`Sst` scalar load/store opcode families and their addressing modes, and the `SmemWordImmPtr` immediate-pointer constructor that converts a word index into a kSmem-tagged byte pointer. It does **not** reproduce the (absent) register-window mechanism — that negative result is owned by [smem-register-window.md](smem-register-window.md); nor the SFLAG atomic protocol ([sflag-protocol.md](sflag-protocol.md)); nor the SPU bundle slot encoding ([../isa/slot-spu-scalar.md](../isa/slot-spu-scalar.md)).

For reimplementation, the contract is:

- **The address space** — `kSmem = 5`; byte-flat at the allocator, word-flat at the SPU; the per-`Target` field layout (`+0x470` size, `+0x508` word size, `+0x4CC` log2) and the in-range predicate.
- **The per-gen sizing** — where the literal byte counts live (`chip_parts.binarypb`), the confirmed field offsets, the banking (2 banks on JF, 8 on PF/VF/GL), and the `kSharedMemWord > kSmemWord` finer-granularity invariant.
- **The scalar load/store addressing** — the `Sld`/`Sst` opcode families per generation (`ScalarLoadSmem` / `…Offset` / `…XY` / `ScalarStore…`), their three addressing modes (absolute-imm, base-SREG+imm, base-SREG+disp-imm), and the v7x fetch-and-add extension.
- **`SmemWordImmPtr`** — the word-index → kSmem-byte-pointer constructor, its `word * SmemWordSizeBytes` conversion, and the `SmemWordSizeBytes == 4` assertion it carries.

| | |
|---|---|
| **MemorySpace value** | `kSmem = 5` (name table @ `0x21ce6b08`; `barna_core_smem = 9`, `sparse_core_sequencer_smem = 14`) |
| **`SmemSizeBytes()`** | `0x1d615e40` — `return *(uint32*)(Target + 0x470)` |
| **`SmemWordSizeBytes()`** | `0x1d617360` — `return *(uint32*)(Target + 0x508)` |
| **`SmemWordSizeLog2()`** | `0x1d617540` — `return *(uint32*)(Target + 0x4CC)` |
| **`IsSmemByteAddressInRange(b)`** | `0x1d6179a0` — `0 <= b && b < SmemSizeBytes()` |
| **`SmemWordImmPtr(word, name)`** | `0x1d516880` — word→byte ptr, `MemorySpace = kSmem(5)` |
| **`AllocateScopedSmem(shape, name)`** | `0x1d5182a0` — trampoline → `AllocateScopedMemory(…, 5u, …)` |
| **Compile-time placer** | `ProgramMemoryAllocator::AllocateBytes` @ `0x1c629e40` (branches on `MemorySpace`) |
| **Runtime allocator** | `tpu::BestFitAllocator`, `Config{base=0, end=SmemSizeBytes, align=granule=SmemWordSizeBytes}` |
| **MSA-managed?** | **No** — `FastMemorySpace()` is never `kSmem` on any Target (`kVmem` on VF/GL, `kCmem` on PF, `kHbm` on JF); placed by opcode semantics |
| **Owner engine** | Scalar Processing Unit (SPU); SREG file is the register source/sink |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## 1. The Scalar Address Space

### Purpose

SMEM is `xla::jellyfish::MemorySpace::kSmem = 5`. It is one of the six addressable regions enumerated in [overview §2](overview.md#2-the-memoryspace-enum); the SMEM-relevant slice of the `MemorySpaceToString` rodata table (`0x21ce6b08`) is:

```text
5   smem                        ◀── SMEM scalar memory (this page)
9   barna_core_smem             ◀── BarnaCore SMEM sibling tier (PXC family only)
14  sparse_core_sequencer_smem  ◀── SC sequencer well-known-constant tier
```

There is **no class named `SmemAllocator`**. SMEM uses the same two-stack architecture as every other tier — `ProgramMemoryAllocator` at compile time, one `tpu::BestFitAllocator` at runtime — distinguished only by the `Config` triple it is constructed with. See [overview §1 Considerations](overview.md#considerations) for why one allocator class services all tiers.

### The byte-flat / word-flat duality

SMEM is addressed at **two granularities** that a reimplementer must keep distinct:

1. **Byte-flat at the allocator level.** `tpu::BestFitAllocator` hands out **byte offsets** relative to `base_offset = 0` (SMEM starts at sub-tile address 0). The runtime `Config` is `{base_offset = 0, end = SmemSizeBytes(), alignment = SmemWordSizeBytes(), granule = SmemWordSizeBytes()}`. Alignment equals granule by construction, so every SMEM allocation is word-aligned and the `BestFitAllocator` ctor's "alignment is a power of two and `alignment % granule == 0`" precondition holds trivially.

2. **Word-flat at the SPU level.** The scalar load/store opcodes encode **word indices**, not byte addresses. The conversion `byte = word * SmemWordSizeBytes` is applied at LLO lowering time (see [§4](#4-smemwordimmptr--the-immediate-pointer-constructor)). Inside the LLO IR the value carries a `StrongInt<SmemOffset>` — a *word* index — so the allocator never observes raw byte addresses in the IR; it sees word offsets converted through `SmemWordSizeBytes()`. The strong-int arc-state type appears verbatim in `LloCodeStepper::ArcState<SmemOffset, SregValue>::SetValue` (`0x10bc7ac0`).

> **GOTCHA —** a naive reimplementation that treats the opcode immediate as a byte address will index SMEM at `word_index` bytes instead of `word_index * SmemWordSizeBytes` bytes — off by a factor of the word size. The opcode immediate is *always* a word index; the byte conversion is the allocator/lowering's job, applied exactly once, in `SmemWordImmPtr`.

### `Target` field layout

The SMEM geometry lives in the per-codename `Target` object, filled at boot from `chip_parts.binarypb`. The direct accessors and their decompiled bodies:

```c
// 0x1d615e40  Target::SmemSizeBytes() const
//   return *(uint32_t*)(this + 0x470);          // total SMEM bytes, signed-compared
// 0x1d617360  Target::SmemWordSizeBytes() const
//   return *(uint32_t*)(this + 0x508);          // per-bank word width, bytes (= granule)
// 0x1d617540  Target::SmemWordSizeLog2() const
//   return *(uint32_t*)(this + 0x4CC);          // == log2(SmemWordSizeBytes)
// 0x1d6179a0  Target::IsSmemByteAddressInRange(int b) const
//   return b >= 0 && b < *(int*)(this + 0x470); // 0 <= b < SmemSizeBytes()
// 0x1d618140  Target::SmemUserSpaceWordOffset() const
//   return *(uint64_t*)(this + 0x7F8);          // first user-allocatable word index
```

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `SmemSizeBytes` | `+0x470` | int32 | Total SMEM bytes (compared signed; the in-range check uses `>= 0`) |
| `BarnaCoreSmemSizeBytes` | `+0x47C` | int32 | BarnaCore SMEM sibling-tier size |
| `BarnaCoreSmemBaseBytes` | `+0x480` | int32 | Byte offset inside SMEM where the BarnaCore window begins |
| `SmemWordSizeLog2` | `+0x4CC` | uint32 | `log2(SmemWordSizeBytes)` — the shift used by the opcode encoder |
| `SmemWordSizeBytes` | `+0x508` | uint32 | Per-bank word width; alignment and granule both equal this |
| `BarnaCoreSmemWordSizeBytes` | `+0x51C` | uint32 | BarnaCore SMEM word width |
| `SmemUserSpaceWordOffset` | `+0x7F8` | uint64 | First word index a user allocation may use (above the bottom reservations) |
| `BarnaCoreFregSmemWordOffset` | `+0x818` | uint64 | BarnaCore freg SMEM word offset |

> **NOTE —** `SmemSizeBytes` is read as an `int32` and the range check is `b >= 0 && b < SmemSizeBytes()` (`IsSmemByteAddressInRange`, `0x1d6179a0`). A reimplementation must keep the lower-bound `>= 0` guard: the field is signed and a negative byte address is a distinct rejection from "too large". The matching compile-time diagnostic is `"byte_address < target().SmemSizeBytes()"`.

### Derived offsets

Two accessors compute reserved-region boundaries from the param-pointer table rather than from stored fields:

```c
// 0x1d618180  Target::ReservedSmemInBytes(int i) const
//   return SmemSizeBytes()
//        - (ParamPtrLocationWordOffset(i) - 1) * SmemWordSizeBytes();
// 0x1d618040  Target::StartReservedSmemWordOffset(int i) const
//   tail-call → ParamPtrLocationWordOffset(i)   (0x1d617fa0)
```

`StartReservedSmemWordOffset(i)` is a literal tail call to `ParamPtrLocationWordOffset(i)`, so the parameter-pointer table and the top-of-SMEM reserved-block table **share the same offset arithmetic**: the reserved blocks live at the top of SMEM, immediately below where the parameter pointers are placed. `ReservedSmemInBytes(i)` then turns that word boundary into a byte count of the reserved span at the top of the image.

> **NOTE —** the literal contents of `ParamPtrLocationWordOffset(int)` (`0x1d617fa0`) — the per-`i` constant table that drives both derived accessors — were not individually traced. The two accessors above are confirmed; the table they index is **LOW confidence** until decompiled.

---

## 2. Per-Generation Sizing

### Purpose

The accessors in [§1](#1-the-scalar-address-space) read four `Target` fields; the literal values are placed per-codename by `TpuChipParts::FromProto` / `TpuMemoryParts::FromProto` from the embedded `chip_parts.binarypb`. The C++ accessors, banking, and capability flags are fully decoded; the numeric byte sizes still live in the un-decoded protobuf.

### Sizing facts that are byte-anchored

| Generation | Target class | SMEM banks | `ScalarLoadLatency` (cy) | 4-byte SMEM-write DMA opcode | SCS SMEM (bytes) |
|---|---|---:|---:|---|---|
| v2 / Jellyfish (JF) | `JellyfishTarget` | **2** | **2** | `false` (`0x1d48fee0`) | n/a (no SCS) |
| v4 / Pufferfish (PF) | `PufferfishTarget` | **8** | **4** | `false` (`0x1d4946a0`) | 0 (no SCS in SMEM; `0x1fbac3e0`) |
| v5 / Viperfish (VF) | `ViperfishTarget` | **8** | **6** | `true` (`0x1d49a8e0`) | **64 KiB** (vfc) / 0 (vlc) |
| v6e / Ghostlite (GL) | `GhostliteTarget` | **8** | **6** | `true` (`0x1d4976c0`) | **64 KiB** (`0x1fe6dd60`) |
| v7x / `6acc60406` (gfc) | `GhostliteTarget` | **8** | **6** | `true` | **64 KiB** (`0x1fda8aa0`) |

- **Banks** come from the per-Target `MemBanks(MemorySpace)` override on the `kSmem(5)` branch: `JellyfishTarget::MemBanks` (`0x1d48fc80`) returns 2 for kSmem; PF/VF/GL return 8. The bank index for a byte offset `B` is `(B / SmemWordSizeBytes) mod MemBanks(kSmem)`; the row-within-bank is `(B / SmemWordSizeBytes) / MemBanks(kSmem)` (assumed by symmetry with VMEM — **MEDIUM confidence** on the exact formula; the bank *count* is CERTAIN).
- **`ScalarLoadLatency`** (load-to-use cycles, SREG ← SMEM) is the hard latency the LLO bundle packer honours: JF=2, PF=4, VF=6, GL=6.
- **`Supports4BSmemWriteDmaDestinationOpcode()`** is a hard capability flag. VF/GL expose a coalesced 4-byte SMEM-write DMA termination opcode (enabling the `"AllToAllDynamic with smem4b mode enabled."` codepath); JF/PF must use the wider 32-byte DMA wires (`"Pipelined AllToAllDynamic is supported for only non-smem4b mode"`). The `Target::` base is a pure-virtual `LogMessageFatal` (`0x1d61d500`).
- **SCS SMEM** is the SparseCore Scalar-Smem subset, returned directly as a compile-time constant by `asic_sw::deepsea::<family>::HardwareAttributes::GetSparseCoreScsSmemSizeBytes` — decompiled body for gfc/glc is literally `return 0x10000;` (64 KiB). The viperfish-`lite` shim (`vxc::vlc`, `0x1ff4b7a0`) and Pufferfish (`0x1fbac3e0`) return 0.

> **QUIRK —** the SCS SMEM size is a *hard-coded immediate in the binary* (`mov $0x10000, %eax`), not a `chip_parts.binarypb` field — unlike generic `SmemSizeBytes`. So the SparseCore scalar-memory budget is identical across Viperfish/Ghostlite/Ghostfish (64 KiB) and cannot be retargeted by swapping the protobuf; it is compiled in per `HardwareAttributes` subclass.

### The two finer-granularity invariants

SMEM is the **finer-grained** of the two on-chip scalar tiers. Two assertions enforce this, each a hard `LogMessageFatal` if violated:

```text
"kSharedMemWordSizeBytes > kSmemWordSizeBytes"
"kSharedMemWordSizeBytes % kSmemWordSizeBytes == 0"
"hbm_word_size_bytes > smem_word_size_bytes"
"hbm_word_size_bytes % smem_word_size_bytes == 0"
```

HBM and shared-memory words are strict multiples of the SMEM word, and strictly larger. A reimplementation that picks an SMEM word ≥ the HBM word will trip `LogMessageFatal` at lowering time.

### What is not yet decoded

> **NOTE —** the literal per-codename `SmemSizeBytes` and `SmemWordSizeBytes` numbers live in `chip_parts.binarypb` and are **not yet extracted** (LOW confidence on the numeric values). The field offsets and accessor bodies above are CERTAIN. One concrete number *is* pinned: `SmemWordImmPtr` (§4) asserts `SmemWordSizeBytes() == sizeof(uint32_t)` for the codename it is compiled against, so for that path the word is **4 bytes**. Public TPU SPU-scratch budgets suggest total SMEM in `{16, 64, 128, 256} KiB`, but the literal numbers are unconfirmed.

### BarnaCore SMEM sibling tier

`BarnaCoreSmem` (`barna_core_smem = 9`) is a **second SMEM tier** with its own size/base/word-size fields (`+0x47C`, `+0x480`, `+0x51C`) and its own accessors gated by `SupportsBarnaCore()` (`vtable[+0x258]`; `BarnaCoreSmemSizeBytes` `LogMessageFatal`s `"BarnaCore is not supported by this target"` otherwise). Only the Pufferfish family carries a non-empty BarnaCore window in this binary; its scalar load/store opcodes use the `BarnaCoreSequencerScalar1_` prefix instead of `TensorCoreScalar1_`. The matching scoped-frame trampoline is `AllocateScopedBarnaCoreSmem` (`0x1d518500`, `MemorySpace = kBarnaCoreSmem`).

---

## 3. Scalar Load / Store Addressing (`Sld` / `Sst`)

### Purpose

The SPU reaches SMEM through dedicated scalar load/store opcodes — the `Sld`/`Sst` families. These are *not* DMAs and are not modelled as DMAs by the cost model; they exit through the dedicated SREG-read port, and the entire round trip is covered by `ScalarLoadLatency` cycles ([§2](#2-per-generation-sizing)). The bundle-slot encoding of these opcodes is owned by [../isa/slot-spu-scalar.md](../isa/slot-spu-scalar.md); this section documents the *addressing model* — what each opcode computes as its SMEM address.

### The three addressing modes

Across generations the opcode names differ but the addressing modes collapse to three. The opcode-name decode is confirmed against the `*_functions.json` symbol table (the `ScalarLoadSmem`, `ScalarLoadSmemOffset`, `ScalarLoadSmemXY` families, each with `…AddressField` / `…OffsetField` / `…DestField` encoder accessors).

| Mode | Address computed | PXC opcode (TPU v4) | VXC/GXC opcode (v5/v5+) |
|---|---|---|---|
| Absolute immediate | `SMEM[imm_word]` | `ScalarLoadSmem` | `ScalarLoadSmemY` |
| Base-SREG + imm | `SMEM[SREG_base + imm]` | `ScalarLoadSmemOffset` | — |
| Base-SREG + disp-imm | `SMEM[SREG_x + imm_disp]` | — | `ScalarLoadSmemXY` |
| Store, absolute | `SMEM[imm_word] := SREG` | `ScalarStoreSmemAbsolute` | `ScalarStoreXToSmemY` |

- **PXC / Scalar1 slot** (Pufferfish, also Jellyfish shape):
  - `TensorCoreScalar1_ScalarLoadSmem` — loads one SMEM word at an immediate-encoded **absolute** word address into an SREG. The immediate is placed via `Place16BitImmediate` / `Place32BitScalarImmediate`; the encoder converts `word_offset = byte_offset / SmemWordSizeBytes`.
  - `TensorCoreScalar1_ScalarLoadSmemOffset` — same load, address = `base_SREG + immediate`. This is the parameter-table / stack-frame access mode where the base lives in an SREG and the displacement is constant.
  - `TensorCoreScalar1_ScalarStoreSmemAbsolute` — `SREG → SMEM` at an immediate absolute address. Used for sync-flag writes, completion-descriptor writes, return-value writes.
- **VXC/GXC / ScalarAlu slot** (Viperfish, Ghostlite):
  - `TensorCoreScalarAlu_ScalarLoadSmemY` — `SREG_dest ← SMEM[Y]`, `Y` a 16-bit immediate word index.
  - `TensorCoreScalarAlu_ScalarLoadSmemXY` — `SREG_dest ← SMEM[X + Y]`, `X` an SREG base, `Y` a 16-bit immediate displacement. This is the canonical "load with displacement" used for parameter-table reads.
  - `TensorCoreScalarAlu_ScalarStoreXToSmemY` — `SMEM[Y] ← SREG_X`.
  - SparseCore SPU variants exist with the `SparseCoreScalarAlu_` prefix, targeting the SCS SMEM subspace.

> **QUIRK —** the PXC family and the VXC/GXC family are *not* renamings of each other. PXC has a single `…Offset` form (`base + imm`); VXC/GXC split into `…Y` (absolute imm) and `…XY` (base + disp). A reimplementation that maps `ScalarLoadSmemOffset` directly onto `ScalarLoadSmemXY` will get the operand roles wrong on one of the two generations — on PXC the SREG is the base, on VXC the SREG (`X`) is the base and the immediate (`Y`) is the displacement, but the absolute form on VXC is the separate `…Y` opcode with no SREG operand at all.

### The v7x fetch-and-add extension

Ghostfish (GXC/GFC) adds two atomic-reduce-into-SMEM opcodes absent on JXC/PXC:

- `SparseCoreScalarAlu_ScalarStoreXToSmemSumDestAndY` — atomic `SMEM[Y] := SMEM[Y] + SREG_X`. A single-instruction fetch-add at the SMEM level, used where multiple SPUs reduce into a shared SMEM word.
- `SparseCoreScalarMisc_SmemFetchAndAdd` — same operation in the orthogonal `ScalarMisc` pipe (so it can issue alongside a regular scalar load); returns the pre-add SMEM value into a destination SREG. Emitted by `isa_emitter::EmitFetchAndAddOp<glc::SparseCoreTecBundle, SmemFetchAndAdd>` (`0x13a3a300`). Present on VFC and GLC/GFC; absent on JXC/PXC.

### What lands in SMEM

SMEM holds the SPU's scalar working set:

- **Scalar register spills** — when LSRA-v2 cannot keep a SREG live across a region, it emits `ScalarStore…ToSmem…` to the reserved spill region followed by `ScalarLoadSmem…` at the next use. SMEM is the **spill backing store for the SREG file**, not a window onto it (see [§5](#5-no-register-window)).
- **The parameter-pointer table** — `ParamPtrLocationWordOffset(int)` (`0x1d617fa0`) gives the SMEM word where the parameter-pointer table lives. The LLO `ScalarLoadSmem` lowering reads it to materialize function arguments into SREGs at the top of each kernel.
- **SC-sequencer well-known constants** — `chip_id`, `replica_id`, `partition_id`, `subslice_origin`, `hbm_offset`. `LloAddress::MakeSparseCoreSequencerSmemConstant(long)` (`0x1d60bc60`) builds an `LloAddress` at a hard-coded per-codename SMEM offset; the `GetIntegerFromSmemOpLowering` MLIR pattern lowers each to a `ScalarLoadSmem` of that fixed address — **bypassing the allocator entirely**.
- **Reserved top words** — last-set P/T-state (`"Reserved Smem offset for storing last set P/T-state."`), trace context (`TpuChipProfilerVxcImpl::ReadTraceContextFromSmem`, `0x1d1a22a0`), completion descriptors.
- **Reserved bottom words** — SCS overlay trampoline, ProgramContinuator stack frames (`GetTensorCoreStackSizeInWordForSparseCoreSmem` `0x1d17cb60`, `GetSparseCoreStackSizeInWordForSparseCoreSmem` `0x1d17cc80`).

> **GOTCHA —** SMEM is **not implicitly zero-initialized**. The buffer-assignment pre-pass explicitly refuses to zero an SMEM allocation: `"Cannot zero out AllocateBuffer output memory space is smem"`. Whoever allocates an SMEM region must write every word it relies on before reading it; a reimplementation that assumes zero-filled scratch will read stale data.

### MLIR / LLVM dialect ops

The compiler-side equivalents of the `Sld`/`Sst` opcodes:

```text
llo.alloca_smem        (mlir::llo::AllocaSmemOp)        — SMEM stack-frame allocation; getNumWords()
llo.saddr.smem         (mlir::llo::ScalarAddressSmemOp) — materialize an SMEM address into an SREG
                          (read effect on mlir::sparse_core::resource_effects::Smem)
llvm.tpu.alloca.smem / llvm.tpu.allocate.smem / .allocate.smem.any   — LLVM intrinsic equivalents
llvm.tpu.addrspacecast.smem                              — bridge LLO SMEM addr-space ↔ generic LLVM ptr
llvm.tpu.dma.hbm.to.smem.sc.{simple,general,single.strided}  — SparseCore HBM→SMEM DMA variants
```

> **NOTE —** the numeric `LlvmTpuDialect::SmemAddressSpace()` value is asserted by the sentinel `"address_space == LlvmTpuDialect::SmemAddressSpace()"`. The generic SMEM LLVM address space is **0** — `MemorySpace 1` (`smem`) maps to LLVM `addrspace 0` in the `MemorySpaceToAddressSpace` reverse table (`dword_AF36CE8[0] == 0`), confirmed on [Address-Space ID Table](../targets/address-space-ids.md). The LLO `kSmem` *MemorySpace* enum value (5) is a separate number space from this LLVM address space and must not be confused with it.

---

## 4. `SmemWordImmPtr` — the Immediate-Pointer Constructor

### Purpose

`LloRegionBuilder::SmemWordImmPtr(long word_index, std::string_view name)` (`0x1d516880`) is the single chokepoint that turns a **word index** into a **kSmem-tagged byte pointer** in the LLO IR. Every scalar load/store that addresses SMEM by a constant word offset routes through it. This is where the byte-flat/word-flat duality of [§1](#1-the-scalar-address-space) is resolved: exactly once, here.

### Algorithm

The decompiled body (cleaned of the `absl::StatusOr` machinery) is:

```c
// 0x1d516880  LloRegionBuilder::SmemWordImmPtr(long word_index, string_view name)
LloValue* SmemWordImmPtr(LloRegionBuilder* rb, long word_index, string_view name):
    Target* tgt = rb->module->target;                 // *(*(rb)+56)+16

    // Invariant: the SMEM word must be exactly a uint32 for this path.
    word_bytes = tgt->SmemWordSizeBytes();             // 0x1d617360
    CHECK_EQ(word_bytes, sizeof(uint32_t)):            // "target().SmemWordSizeBytes() == sizeof(uint32_t)"
        // on failure → LloModule::UpdateStatus(... CheckFailer ...)
        //   site: platforms/xla/service/jellyfish/llo_region_builder.cc

    word_bytes = tgt->SmemWordSizeBytes();             // re-read after the check
    Shape shape = ShapeUtil::MakeValidatedShape(U32 /*=8*/, /*rank*/0);  // a 4-byte scalar

    // Convert word → byte and build the immediate pointer, tagged kSmem.
    return rb->ImmPtr(/*byte_offset=*/ word_bytes * word_index,   // ◀── the word→byte conversion
                      shape,
                      /*MemorySpace=*/ 5 /*kSmem*/,               // ◀── tier tag
                      name, ...);
```

Three things a reimplementer must reproduce exactly:

1. **The word→byte conversion is `SmemWordSizeBytes() * word_index`** — multiplication, applied once, inside `SmemWordImmPtr`. Callers pass word indices; the IR pointer carries the byte offset.
2. **The element type is a 4-byte unsigned scalar.** `MakeValidatedShape(8, …)` builds a `U32` rank-0 shape — the immediate pointer points at a single 32-bit SMEM word.
3. **The `MemorySpace` argument to `ImmPtr` is the literal `5` (kSmem).** This is how the resulting pointer is declared to belong to the SMEM tier, which is in turn what makes `ProgramMemoryAllocator::AllocateBytes` commit the value into the SMEM image rather than VMEM/HBM. Placement is opcode/pointer-semantics-driven, exactly as [§ Abstract](#abstract) and the [overview](overview.md#1-the-six-region-taxonomy) state.

> **QUIRK —** `SmemWordImmPtr` hard-asserts `SmemWordSizeBytes() == sizeof(uint32_t)`. The `Target` field is a `uint32` capable of holding any power of two, but **this immediate-pointer path only supports a 4-byte SMEM word**. A codename whose `chip_parts.binarypb` set a non-4-byte SMEM word would `LogMessageFatal` the moment any constant-offset SMEM pointer is constructed. For 0.0.40's production codenames the word is therefore effectively pinned at 4 bytes on this path.

### Scoped-frame allocation

For per-region SMEM scratch (not a fixed word constant) the entry is the trampoline `AllocateScopedSmem`:

```c
// 0x1d5182a0  LloRegionBuilder::AllocateScopedSmem(Shape const&, string_view name)
LloValue* AllocateScopedSmem(rb, shape, name):
    return rb->AllocateScopedMemory(shape, /*MemorySpace=*/ 5u /*kSmem*/, name);  // 0x1d517c20
```

The decompile shows it is a single tail call passing the literal `5u` — the SMEM analogue of scoped-VMEM, differing only in the MemorySpace argument (5 instead of 3). `AllocateScopedMemory` delegates to `LloRegion::AllocateScopedFixedMemory` (`0x1d5137c0`). The BarnaCore sibling `AllocateScopedBarnaCoreSmem` (`0x1d518500`) passes the BarnaCore MemorySpace instead.

### LSRA-v2 spill-window arithmetic

The LSRA-v2 register allocator computes the SMEM bytes available for spilling using these same accessors. `lsrav2::SmemBytesAvailable(LloRegion*)` (`0x12786120`) decompiles to:

```c
// 0x12786120  lsrav2::SmemBytesAvailable(const LloRegion*)
long SmemBytesAvailable(self, region):
    Target* tgt = ...;
    int reservation_id = ...;                          // region field +52
    word_bytes  = tgt->SmemWordSizeBytes();
    ceiling     = tgt->ParamPtrLocationWordOffset(0);  // top user word (param-ptr table)
    floor       = tgt->SmemUserSpaceWordOffset();       // first user word (above bottom blocks)
    span_bytes  = word_bytes * (ceiling - floor);       // raw user window in bytes
    hbm_reserve = round_up(tgt->HbmWordSizeBytes(), 1024);     // HBM-aligned head reserve
    return span_bytes - (hbm_reserve + tgt->ReservedSmemInBytes(reservation_id));
```

The spill window is the user span (`SmemUserSpaceWordOffset` floor to `ParamPtrLocationWordOffset(0)` ceiling) minus an HBM-word-aligned reserve and the top-of-SMEM reserved span. This confirms the role of `SmemUserSpaceWordOffset` (`+0x7F8`) as the **bottom** of the user-allocatable region and `ParamPtrLocationWordOffset(0)` as its **top**.

> **NOTE —** the spill-region cap is further tunable by `FLAGS_xla_jf_lsra_v2_reserved_smem` (`0x223afaa8`), which reserves a fixed top-N words for scratch and tags loads above the cap as rematerializable (`"Considers all smem loads above the spill limit to be const and read-only and really trivially rematerializable."`). The exact translation of that flag into the `first_smem_scratch_word_` field referenced by the assertion `"current_local_sync_flag_ <= first_smem_scratch_word_"` was not traced (**LOW confidence** on that specific wiring).

---

## 5. No Register Window

SMEM has **no register-window machinery**. A search of the binary for `SmemRegisterWindow` / `SmemRegisterFile` / `SregWindow` / `SmemSpillRegister` returns zero hits. SMEM is a flat byte/word array, not a window onto a register file; there is no SPARC-style register-window overflow story for it. Scalar register windowing lives entirely on the **SREG file** (the `xla::jellyfish::SregNumber`-typed pool driven by LSRA-v2), and SMEM is merely that file's spill backing store. The full negative result and the SREG-file detail are owned by [smem-register-window.md](smem-register-window.md); this page records only that the SMEM scalar model is window-free by design.

---

## 6. Exhaustion and Invariant Handling

SMEM overflow is almost entirely a compile-time concern, because MSA does not rebalance SMEM and the image is fully laid out before execution (the runtime allocator only replays). The compile-time paths:

| Mode | Trigger | Diagnostic |
|---|---|---|
| Out-of-range byte offset | `IsSmemByteAddressInRange(b)` false (`0x1d6179a0`) | `"byte_address < target().SmemSizeBytes()"` |
| Allocator cannot place | shared `BestFitAllocator::Allocate` OOM | `absl::ResourceExhaustedError(...)` (see [hbm-allocator.md](hbm-allocator.md)) |
| Bad `smem_end`/`smem_start` clamp | control-plane config reject | `"Can't set smem_end using too large of a value."` |
| Geometry invariant | word-size / granule violations | `"kSharedMemWordSizeBytes > kSmemWordSizeBytes"`, `"hbm_word_size_bytes % smem_word_size_bytes == 0"`, `"available_smem_size >= granule_size"` |
| SC table overflow | ragged-pointer table exceeds SCS budget | `"Row pointers would exceed available SCS Smem ("` |
| Trampoline overflow | SCS overlay trampoline reservation too large | `"Reserve extra smem spill area for SCS overlays trampoline."` (`FLAGS_xla_sc_reserve_scs_trampoline_smem`, `0x22335e88`) |
| Debug poison | use-after-free detector | `"Poisoned Smem value use detected"` (`xla::jellyfish::llo_analysis::RaceAnalyzerStepper::PoisonSmemBuffer`, `0x10bc15c0`) |

Each geometry-invariant message is a hard `LogMessageFatal`. The SCS-budget check (`GetUserAllocatableScsSmemSize`, `0x13db6d80`) reports a compile-time error rather than silently spilling, so a SparseCore lowering that overflows SCS SMEM fails loudly.

---

## 7. Compile-Time → Runtime Hand-Off

SMEM follows the shared hand-off pipeline ([overview §3](overview.md), [hbm-allocator.md](hbm-allocator.md)) and diverges only at the MSA stage:

```text
1. HeapSimulator::Run(GlobalDecreasingSizeBestFitHeap, budget = SmemSizeBytes())
2. MSA pass — DOES NOT relocate SMEM (SMEM is not kAlternate; FastMemorySpace() is never kSmem — kVmem on VF/GL, kCmem on PF, kHbm on JF)
3. ProgramMemoryAllocator::AllocateHloBuffer (0x1c62a5a0)
     emits ProgramMemoryMetadata_Allocation{ memory_space = kSmem, offset, size, block_type, name }
4. proto travels inside the compiled XDB / LLO program
5. ProgramMemoryAllocator::CreateFromProto (0x1c631f20) rehydrates runtime state
6. TpuHal binds one tpu::BestFitAllocator for the SMEM tier:
     Config{ base_offset = 0,
             end        = Target::SmemSizeBytes(),
             alignment  = Target::SmemWordSizeBytes(),
             granule    = Target::SmemWordSizeBytes() }
```

The decisive divergence is **stage 2**: SMEM is never colored by MSA. A value lands in SMEM because its lowering emitted a scalar load/store opcode declaring `memory_space() == kSmem` (asserted by `"address->memory_space() == MemorySpace::kSmem"` and `"dest_address->memory_space() == MemorySpace::kSmem"` at every emission site), and `AllocateBytes(MemorySpace = kSmem, …)` then commits it into the SMEM image. There is no cost-balancing tug-of-war for SMEM.

---

## Related Components

| Name | Relationship |
|---|---|
| [Memory Hierarchy Overview](overview.md) | Owns the six-region taxonomy and the `MemorySpace` enum; SMEM is `kSmem = 5` |
| [SMEM Register Window](smem-register-window.md) | The negative result: SMEM has no register window; SREG-file windowing detail |
| [SFLAG Protocol](sflag-protocol.md) | Sibling `kSflag = 6` tier; its size/word fields neighbour the SMEM fields (`+0x468`, `+0x504`) |
| [CMEM Pool](cmem-pool.md) | Sibling on-chip operand pool (`kCmem`, Pufferfish-only); MSA-managed unlike SMEM |
| [HBM Allocator](hbm-allocator.md) | The `BestFitAllocator` algorithm SMEM replays at runtime; the shared OOM path |
| [VMEM Allocator](vmem-allocator.md) | The `kAlternate` MSA-managed tier SMEM is contrasted against |

## Cross-References

- [overview.md](overview.md) — memory-space taxonomy, enum, two-stack allocator story; SMEM tier row
- [smem-register-window.md](smem-register-window.md) — why "register window" does not apply to SMEM; SREG-file spill model
- [sflag-protocol.md](sflag-protocol.md) — the separate `kSflag` atomic tier the SPU also reaches
- [cmem-pool.md](cmem-pool.md) — Pufferfish CMEM operand pool, MSA-managed sibling
- [hbm-allocator.md](hbm-allocator.md) — `tpu::BestFitAllocator` algorithm, `Config` triple, `ResourceExhaustedError`
- [../isa/slot-spu-scalar.md](../isa/slot-spu-scalar.md) — SPU bundle-slot encoding of the `Sld`/`Sst` opcodes
- [../isa/memory-space-enum.md](../isa/memory-space-enum.md) — the `MemorySpace` enum values used as the SMEM tier tag
