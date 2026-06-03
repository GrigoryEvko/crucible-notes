# BarnaCore Overview

> *Every codename, engine name, sequencer-type value, and per-generation presence claim on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`) — from the demangled C++ symbol table (`…_functions.json`), the `TpuSequencerTypeToString` enum jump table, and the per-family `isa` namespace symbols. Other versions differ.*

## Abstract

**BarnaCore (BC)** is the TPU's *pre-SparseCore* embedding / sparse-lookup coprocessor. It does the same architectural job SparseCore later does — hardware-accelerated embedding gather, lookup, and gradient scatter-add against gigabyte-scale tables in HBM, the access pattern the TensorCore's dense systolic MXU cannot serve efficiently — but it does it with an earlier, narrower ISA. BarnaCore ships on exactly three generations: **Jellyfish (TPU v3), Dragonfish, and Pufferfish (TPU v4)**. From the **Viperfish** generation onward it is retired and replaced wholesale by [SparseCore](../sparsecore/overview.md). The cut is razor-sharp and falls once, at the v4→v5 silicon boundary: no generation in this binary ships both a live BarnaCore and a live SparseCore.

Within the BarnaCore era the engine has *two distinct personalities*, and which one a generation carries is itself per-generation. Jellyfish and Dragonfish (`jxc` family) expose only a **BarnaCore Address Handler (BCAH)** — a 16-byte address-generation bundle stream driven inline by the TensorCore's own encoder, with no standalone embedding sequencer. Pufferfish (`pxc`/`pfc` family) is the high-water mark: it gains a full **BarnaCore Scalar sequencer (BCS)** — a fully independent 32-byte VLIW machine with its own dual scalar pipes (`Scalar0`/`Scalar1`), a separate **BarnaCore Channel** vector unit, a private four-tier memory hierarchy (`barna_core_{bmem,smem,sflag,imem}`), a hardwired sync FSM, and its own 181 KB LLVM instruction-encoding table. Pufferfish is the last BarnaCore chip and the one whose ISA most closely converges on what SparseCore would standardise.

This page is navigational. It fixes what BarnaCore is, names the BCS/BCAH personality split and its binary-evidenced per-generation presence, sketches the host→HBM→BarnaCore→TensorCore embedding data path at the level a reader needs to orient, and routes to the page that owns each piece. The deep mechanics — the end-to-end retirement evidence, the BCS 32-byte bundle byte-map, the scalar ISA roster, the merged-ALU bit layout, the per-gen performance grids, and the JF/DF 16-byte address-handler bundle — live on the sibling pages cross-referenced below.

For reimplementation, the contract is:

- **BarnaCore is its own ISA family, not a TensorCore mode.** Pufferfish carries fully independent `pxc::pfc::isa::BarnaCore{Sequencer,Channel}CodecBase` class hierarchies and a dedicated LLVM subtarget (`TPUBcSubtarget`); Jellyfish/Dragonfish carry an `isa::EncoderBcsDf` leaf. A reimplementer must treat BC as a separate VLIW machine that coordinates with the TensorCore through DMA and sync flags, not as extra TensorCore slots.
- **Two personalities, split by generation.** JF/DF ship BCAH only (16-byte bundle, `TpuSequencerType` = 2); Pufferfish ships BCS (32-byte bundle, `TpuSequencerType` = 1) plus the Channel vector unit. Emitting a BCS program for a Jellyfish target, or a BCAH bundle for Pufferfish, is a codec error.
- **Per-gen presence is part of the contract.** BarnaCore is JF/DF/PF only. There are **zero** `BarnaCore*` ISA symbols under any of the v5+ family namespaces (`vfc` / `glc` / `gfc`) in this binary — Viperfish, Ghostlite, and Trillium have no live BarnaCore. Targeting BarnaCore on a v5+ codec has no encoder leaf to build.
- **The retirement leaves a vestige, not a clean delete.** `TpuSequencerType` keeps BCS=1 / BCAH=2 reserved forever (proto back-compat), the `TpuCore` driver vtable keeps the fourteen `Set*BarnaCore*` / `Enable*BarnaCore*` control-plane vfuncs on every family, and the SparseCore DMA fabric still names `DMA_CORE_ID_BARNA_CORE_0..3` / `DMA_MEMORY_ID_BMEM`. These are enum identity + ABI + DMA-routing back-compat; the `HasBarnaCore` Target predicate gates the live tiers off on v5+ chips. See [Retirement Evidence](retirement.md).

| | |
|---|---|
| **What it is** | Pre-SparseCore on-die embedding / sparse-gather coprocessor, co-located with the TensorCore, sharing HBM |
| **Personalities** | BCS — *BarnaCore Scalar* sequencer (Pufferfish) · BCAH — *BarnaCore Address Handler* (Jellyfish/Dragonfish) |
| **Sequencer enum** | `tpu::TpuSequencerType` — TC=0, **BCS=1**, **BCAH=2**, then SCS=3 / TAC=4 / TEC=5 (SparseCore) |
| **Codec / encoder roots** | `pxc::pfc::isa::BarnaCore{Sequencer,Channel}CodecBase` (Pufferfish) · `jellyfish::isa::EncoderBcsDf` (JF/DF) |
| **Gens with BC** | Jellyfish (BCAH) · Dragonfish (BCAH) · Pufferfish (BCS + Channel) |
| **Gens without BC** | Viperfish / Ghostlite / Trillium — SparseCore era (see [SparseCore Overview](../sparsecore/overview.md)) |
| **Memory** | `barna_core_bmem` (working buffer) · `barna_core_smem` (scalar) · `barna_core_sflag` (sync) · `barna_core_imem` (instr) |
| **Confidence** | CONFIRMED (symbol-table-anchored) unless a row or callout says otherwise |

---

## What BarnaCore Is — and Why It Is Separate

A TensorCore is a statically-scheduled VLIW machine built around a systolic matrix unit; it is at its best when data arrives as dense tiles streamed contiguously out of HBM. Embedding-heavy models break that assumption: the dominant cost is not matmul FLOPs but *pointer-chasing* — reading a handful of rows out of a table with millions of rows, where which rows are touched is data-dependent and changes every minibatch, and accumulating gradients back into arbitrary HBM rows on the backward pass. BarnaCore exists to absorb exactly that traffic on the v3/v4-class chips, the same way SparseCore does on v5+.

The binary records the handoff path directly. The Pufferfish-side embedding pipeline runs through `barna_core::BcsLloEmitter`: `IssueDmaInfeedToVmem` gathers the assembled embedding tiles and DMA-infeeds them into the TensorCore's VMEM, `WaitForInfeedToVmemDma` is the TC-side completion wait, and `IssueDmaScatter` / `IssueDmaScatterOne` are the gradient write-back path. That is the same shape as the SparseCore embedding pipeline (gather → DMA to VMEM → sync flag → backward scatter) — only the ISA, bundle width, and sync model differ.

> **NOTE — BarnaCore is its own ISA family, not a TensorCore mode.** Pufferfish carries fully independent `pxc::pfc::isa::BarnaCoreSequencerCodecBase` and `BarnaCoreChannelCodecBase` class roots and a dedicated LLVM subtarget `TPUBcSubtarget` ("BarnaCore, Pufferfish HBM-side"). Jellyfish/Dragonfish carry an `isa::EncoderBcsDf` leaf and a BarnaCore-specific slot on the legacy TensorCore encoder vtable (`EncodeBarnaCoreAddressHandlerScalarSlot`). A reimplementer who models BarnaCore as extra TensorCore slots will not produce encodable BC programs.

---

## The Two Personalities — BCS and BCAH

BarnaCore is structured as a control/compute **sequencer** paired (on the gens that have it) with a dedicated **address handler**. The two are distinguished in the binary by the `tpu::TpuSequencerType` enum, which the encoder template carries as a non-type parameter and `TpuSequencerTypeToString` renders. BarnaCore occupies the two lowest non-TensorCore sequencer-type slots — a chronological retirement fingerprint, since it predates SparseCore in the enum.

| Enum | `TpuSequencerType` literal | Short | Bundle | Role |
|---|---|---|---|---|
| 0 | `TPU_SEQUENCER_TYPE_TENSOR_CORE_SEQUENCER` | TC | (per-gen) | The dense matrix sequencer — all gens |
| 1 | `TPU_SEQUENCER_TYPE_BARNA_CORE_SEQUENCER` | BCS | 32 B | BarnaCore Scalar control/compute sequencer (Pufferfish) |
| 2 | `TPU_SEQUENCER_TYPE_BARNA_CORE_ADDRESS_HANDLER` | BCAH | 16 B | Address generation for embedding lookups (JF/DF) |
| 3 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_SEQUENCER` | SCS | 32 B | SparseCore scalar sequencer (v5+) |
| 4 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_ACCESS_CORE_…` | TAC | 64 B | SparseCore tile-access / DMA issuer (v5e/v5p) |
| 5 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_EXECUTE_CORE_…` | TEC | 64 B | SparseCore vector compute (v5+) |

**BCS — the BarnaCore Scalar sequencer (Pufferfish).** BCS is a full independent VLIW machine. The decompiled `pfc::isa` symbols expose a **dual scalar pipe** — `BarnaCoreSequencerScalar0_*` and `BarnaCoreSequencerScalar1_*` — each carrying the same op set (integer add/sub, scalar and/or/xor/move, the sync family `SyncAdd` / `SyncEqualTo` / `SyncLessThan`, `IssueFsm`, `ReadDone` / `WriteDone`). Alongside it a separate **BarnaCore Channel** vector unit exposes `BarnaCoreChannelVectorAlu0_*` / `VectorAlu1_*` (`VectorMove` / `VectorAnd` / `VectorOr` / `VectorXor`). The two-scalar-pipe-each-with-a-SyncAdd structure is the direct ancestor of SparseCore's `SparseCoreScalarAlu0` / `Alu1`. The BCS bundle is 32 bytes; the scalar ISA and the merged-ALU bit layout are on their own pages. See [BCS 32-Byte Bundle](bcs-32byte-bundle.md), [BCS Scalar0/Scalar1 ISA](bcs-scalar-isa.md), and [Merged-ALU Bit Layout](merged-alu.md).

**BCAH — the BarnaCore Address Handler (Jellyfish/Dragonfish).** JF/DF have no standalone embedding sequencer. The BarnaCore work is driven through a 16-byte address-handler bundle stream, and the TensorCore sequencer itself issues the lookups; BCAH only handles address generation. The evidence is the legacy 20-slot TensorCore encoder vtable on `jellyfish::isa::EncoderBcsDf`, whose extra slot 19 is literally named `EncodeBarnaCoreAddressHandlerScalarSlot` — the BarnaCore address-handler scalar-slot encoder built inline into the TensorCore encoder rather than into a wholly separate engine. See [JF/DF 16-Byte Address-Handler Bundle](jf-df-address-handler-bundle.md).

> **GOTCHA — the `EncoderBcsDf` symbol name does not match its personality.** The JF/DF encoder symbol is `EncoderBcsDf`, where "Bcs" abbreviates "BarnaCore Sequencer" (seq=1) — yet the `TpuSequencerType` presence enumeration places Jellyfish/Dragonfish firmly under **BCAH** (seq=2, 16-byte bundle), and the only BarnaCore method on its vtable is the *address-handler* slot encoder. The presence matrix is authoritative: JF/DF BarnaCore is the 16-byte address-handler personality regardless of the abbreviation the symbol implies. The full-sequencer label `Bcs` is genuine only on Pufferfish (`EncoderPfBarnaCoreSequencer`). [Confidence: HIGH — symbol-name vs presence-matrix tension is recorded but the functional fact is solid.]

---

## Per-Generation Presence

BarnaCore is a v3/v4-only feature. Which personality is present is itself per-generation, and is the single most important fact a reimplementer must encode. The discriminator is the family namespace: BarnaCore codec / encoder classes are scoped under the per-generation `asic_sw::deepsea` family namespace — `jxc` (Jellyfish/Dragonfish) and `pxc`/`pfc` (Pufferfish) — and their absence under `vxc`/`vfc`, `gxc`/`glc`, `gxc`/`gfc` is a direct binary readout that v5+ has no BarnaCore.

| Gen | Codename | Family ns | BCS | BCAH | BC bundle | Embedding engine | Notes |
|---|---|---|:---:|:---:|---|---|---|
| TPU v3 | Jellyfish | `jxc` | – | **Y** | 16 B | **BarnaCore** | BCAH only; TC sequencer issues lookups |
| TPU v3 | Dragonfish | `jxc` | – | **Y** | 16 B | **BarnaCore** | Reuses the Jellyfish codec verbatim |
| TPU v4 | Pufferfish | `pxc`/`pfc` | **Y** | – | 32 B | **BarnaCore** | Full BCS sequencer + Channel; last BC gen |
| TPU v5e | Viperfish | `vxc`/`vfc` | – | – | — | SparseCore | No BarnaCore (vestige only) |
| TPU v5p | Ghostlite | `gxc`/`glc` | – | – | — | SparseCore | No BarnaCore (vestige only) |
| TPU v6e | Trillium | `gxc`/`gfc` | – | – | — | SparseCore | No BarnaCore (vestige only) |

> **NOTE — codename-to-marketing-name mapping.** BarnaCore appears in the binary under the silicon codenames Jellyfish, Dragonfish, and Pufferfish; the marketing-name column (`v3`/`v4`) follows the convention used by the sibling [ISA Overview](../isa/overview.md). The binary keys everything on the codename family namespace and the `TpuVersion` ordinal, not the marketing name — treat the codename + family namespace as the authoritative discriminator. [Confidence: MEDIUM for the marketing-name mapping; CONFIRMED for the codename / family-namespace presence.]

### Decompile cross-check — BarnaCore ISA symbols by family

The presence matrix was confirmed directly against the demangled function set (`…_functions.json`). The Pufferfish (`pfc`) namespace carries a rich BarnaCore ISA; the v5+ family namespaces carry none.

| Symbol pattern | Where | Count (this build) | Reading |
|---|---|---:|---|
| `BarnaCore`/`barna_core` (all symbols) | binary-wide | 172,148 | BarnaCore is a large, live subsystem |
| `pfc::isa::…BarnaCore*` (Pufferfish ISA ops) | `pxc`/`pfc` | 44,770 | Full BCS sequencer + Channel ISA on Pufferfish |
| `BarnaCoreSequencerScalar0_SyncAdd` / `Scalar1_SyncAdd` | `pfc::isa` | 168 / 168 | Dual scalar pipes confirmed (BCS) |
| `BarnaCoreSequencerCodecBase` / `BarnaCoreChannelCodecBase` | `pfc::isa` | 142 / 200 | Pufferfish full-VLIW codec roots present |
| `EncoderBcsDf` | `jellyfish::isa` | 140 | JF/DF address-handler encoder leaf |
| `EncoderPfBarnaCoreSequencer` / `EncoderPfBarnaCoreChannel` | `pufferfish::isa` | 77 / 77 | Pufferfish BCS + Channel encoder leaves |
| `(vfc\|glc\|gfc)::isa::…BarnaCore*` | v5+ families | **0** | **No live BarnaCore on Viperfish/Ghostlite/Trillium** |

The **zero** `BarnaCore*` ISA symbols under any of `vfc` / `glc` / `gfc` is the cleanest single datum that BarnaCore is JF/DF/PF-only. Conversely the ~45 K Pufferfish BarnaCore ISA symbols (a full sequencer + channel roster), against an address-handler-only encoder leaf on Jellyfish/Dragonfish, pins the BCS/BCAH personality split. The supporting LLVM-backend artefacts — the `TPUBcSubtarget` subtarget (149 symbol hits), the `BarnaCoreSyncFsmInstructionBitfieldsRefImpl` sync-FSM encoder, and the `BcsLloEmitter` embedding-DMA emitter — are all present and Pufferfish-keyed.

> **CONFIRMED — BarnaCore and SparseCore are mutually exclusive, one per generation.** No generation in this binary ships both a live BarnaCore and a live SparseCore. Generations 0/1/2 (Jellyfish/Dragonfish/Pufferfish) ship BarnaCore and no SparseCore; generations 3/4/5 (Viperfish/Ghostlite/Trillium) ship SparseCore and no BarnaCore. The swap happens exactly once, at the v4→v5 boundary. The full vestigial-vs-absent breakdown is on [Retirement Evidence](retirement.md).

---

## The Embedding Data Path

BarnaCore's reason to exist is the embedding lookup, and the high-level flow is the same one SparseCore inherited: move host-resident embedding tables into HBM, gather rows on demand into BarnaCore's private working buffer, DMA-infeed the assembled tiles into the TensorCore's VMEM, and on the backward pass scatter gradients back into HBM.

```text
HOST                      HBM (shared TC/BC)        BARNACORE                       TENSORCORE
────                      ──────────────────        ─────────                       ──────────
embedding tables ─load─▶  embedding rows                                            matmul / MLP
                          (GB-scale, indirect)
                                │
              index stream ─────┤  BCAH (JF/DF) /        address generation
                                ▼  BCS (PF) sequence      + lookup DMA
                          [HBM row r_i] ──gather──▶  barna_core_bmem  ◀── row tiles
                                                          (working buffer, MS tier)
                                                               │
                                          BcsLloEmitter::IssueDmaInfeedToVmem
                                          ───────────────────────────────────▶  VMEM
                                                          │   WaitForInfeedToVmemDma  consume
        ── backward pass ──                               ▼                            │
                          [HBM row r_i] ◀── BcsLloEmitter::IssueDmaScatter[One]  ◀──── gradients
```

BarnaCore owns its own four-tier private memory hierarchy, mirrored in the global `MemorySpace` enum and recovered from the `MemorySpaceToString` rodata table: **`barna_core_bmem`** (the embedding tile / working buffer — the analogue of SparseCore's `TILE_SPMEM`), **`barna_core_smem`** (scalar memory), **`barna_core_sflag`** (its own sync-flag / atomic register file), and **`barna_core_imem`** (instruction memory). The TensorCore handoff uses DMA into VMEM plus the BarnaCore sync surface.

Synchronisation is where BarnaCore differs most sharply from its successor. BarnaCore runs a **hardwired sync FSM** — `isa::BarnaCoreSyncFsmInstructionBitfieldsRefImpl` plus the program-patch fixup `barna_core::fsm_program_patch_functions::UpdateSyncFlagWaitAndClear` (which fuses a wait-then-clear into one FSM instruction) — and a dual `Scalar0`/`Scalar1` `SyncAdd`, a BarnaCore-side store fence `LloRegionBuilder::BcSfence`, and dedicated bundle fence slots (`BundleRequirement::add_bc_sfence_slots`). SparseCore replaced this fixed FSM with a software-visible sync model. That FSM-vs-software-sync delta is one of the architectural reasons for retirement; the full set lives on [Retirement Evidence](retirement.md).

> **NOTE — the BarnaCore↔SparseCore correspondence is functional, not binary-compatible.** Both engines do the same job — embedding gather / lookup / scatter-add against HBM that the dense MXU cannot serve. The structural lineage is one-to-one (BCS↔SCS, BCAH↔TAC, `barna_core_bmem`↔`TILE_SPMEM`, `IssueDmaScatter`↔`STREAM_OPCODE_SCATTER_FLOAT_ADD`), but a BarnaCore program and a SparseCore program are not interchangeable: different ISA, different bundle width, different sync model. SparseCore is a clean-sheet redesign of the same functional role, not an extension of BarnaCore.

---

## How the BarnaCore Sub-Part Is Organized

The BarnaCore sub-part of Part IX keeps the engine whole. This overview fixes the orientation, the personality split, and the per-gen presence; the deep mechanics fan out to the sibling pages:

- **[Retirement Evidence](retirement.md)** — the end-to-end BarnaCore → SparseCore transition: the merged presence matrix (two independent enumerations that agree), the vestigial-vs-absent breakdown (what survives on v5+: `TpuSequencerType` 1/2, the `TpuCore` driver vtable slots 25..38, `DMA_CORE_ID_BARNA_CORE_0..3`, `DMA_MEMORY_ID_BMEM`, the `HasBarnaCore`-guarded SFLAG tier), and the architectural reasons inferable from the ISA deltas.
- **[BCS 32-Byte Bundle](bcs-32byte-bundle.md)** — the Pufferfish BCS VLIW bundle layout, the `InstBits_BarnaCorePxcHwMode` instruction-encoding table, and the BCS metadata accessor.
- **[BCS Scalar0/Scalar1 ISA](bcs-scalar-isa.md)** — the dual-scalar control + memory opcode roster (the `BarnaCoreSequencerScalar0/1_*` op set).
- **[Merged-ALU Bit Layout](merged-alu.md)** — the per-slot field encoding (vector-result destination, base-address encoding) of the BarnaCore merged ALU.
- **[Per-Gen BarnaCore Perf Grids](per-gen-perf-grids.md)** — the per-generation BarnaCore performance / cost grids (the `PufferfishBarnaCorePerformance` variants).
- **[JF/DF 16-Byte Address-Handler Bundle](jf-df-address-handler-bundle.md)** — the Jellyfish/Dragonfish BCAH 16-byte bundle and the `EncodeBarnaCoreAddressHandler` slot encoder.

The successor engine that replaced BarnaCore from Viperfish onward is documented at [SparseCore Overview](../sparsecore/overview.md). The TensorCore ISA that BarnaCore hands off to is [ISA Overview](../isa/overview.md).

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| BarnaCore ships on Jellyfish / Dragonfish / Pufferfish only | `EncoderBcsDf` under `jellyfish::isa`; `pfc::isa::BarnaCore*` (~45 K syms); **zero** BarnaCore ISA syms under `vfc`/`glc`/`gfc` | CONFIRMED |
| Two personalities: BCS (seq=1, 32 B, Pufferfish) and BCAH (seq=2, 16 B, JF/DF) | `TpuSequencerTypeToString` enum; `EncoderPfBarnaCoreSequencer` (PF) vs `EncodeBarnaCoreAddressHandlerScalarSlot` (JF/DF vtable slot 19) | CONFIRMED |
| Pufferfish BCS is a full independent VLIW machine | `pfc::isa::BarnaCoreSequencerCodecBase` + `BarnaCoreChannelCodecBase`; dual `Scalar0/1_SyncAdd`; `TPUBcSubtarget` | CONFIRMED |
| `EncoderBcsDf` symbol-name vs BCAH presence-matrix tension on JF/DF | symbol abbreviates "BarnaCore Sequencer"; only vtable method is `EncodeBarnaCoreAddressHandlerScalarSlot`; presence matrix says BCAH | HIGH |
| BarnaCore embedding path: gather → DMA-infeed to VMEM → backward scatter | `BcsLloEmitter::{IssueDmaInfeedToVmem,WaitForInfeedToVmemDma,IssueDmaScatter,IssueDmaScatterOne}` | CONFIRMED |
| Four BarnaCore memory tiers `barna_core_{bmem,smem,sflag,imem}` | global `MemorySpace` enum / `MemorySpaceToString` rodata table (relative order bmem→smem→sflag→imem certain; absolute base ±1 — see [Retirement Evidence](retirement.md)) | HIGH |
| Hardwired sync FSM (vs SparseCore software sync) | `BarnaCoreSyncFsmInstructionBitfieldsRefImpl`; `fsm_program_patch_functions::UpdateSyncFlagWaitAndClear`; `BcSfence` + `add_bc_sfence_slots` | CONFIRMED |
| BC and SC are mutually exclusive — one per generation, swap at v4→v5 | presence matrix; no gen carries both a live BC and live SC codec | CONFIRMED |
| Pufferfish BC has its own 181 KB LLVM encoding table `InstBits_BarnaCorePxcHwMode` | LLVM `getBinaryCodeForInstr` HwMode-gated rodata table (not a function symbol; from binary-analysis report) | HIGH |
| Codename→marketing names v3/v4 for Jellyfish/Dragonfish/Pufferfish | follows sibling [ISA Overview](../isa/overview.md); binary keys on codename family ns | MEDIUM |

---

## Cross-References

- [Retirement Evidence](retirement.md) — the BarnaCore → SparseCore transition, the vestigial-vs-absent breakdown, and the retirement rationale.
- [BCS 32-Byte Bundle](bcs-32byte-bundle.md) — the Pufferfish BCS VLIW bundle layout and `InstBits_BarnaCorePxcHwMode`.
- [BCS Scalar0/Scalar1 ISA](bcs-scalar-isa.md) — the dual-scalar control + memory opcode roster.
- [Merged-ALU Bit Layout](merged-alu.md) — the per-slot field encoding of the BarnaCore merged ALU.
- [Per-Gen BarnaCore Perf Grids](per-gen-perf-grids.md) — the per-generation BarnaCore performance / cost grids.
- [JF/DF 16-Byte Address-Handler Bundle](jf-df-address-handler-bundle.md) — the Jellyfish/Dragonfish BCAH bundle and its slot encoder.
- [SparseCore Overview](../sparsecore/overview.md) — the successor engine that replaced BarnaCore from Viperfish onward.
- [ISA Overview](../isa/overview.md) — the TensorCore VLIW ISA BarnaCore hands off to.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / BarnaCore (legacy v2–v4) — [back to index](../index.md)
