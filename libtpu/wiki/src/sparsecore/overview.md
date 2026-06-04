# SparseCore Overview

> *Every codename, engine name, and per-generation presence claim on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`) — from the demangled C++ symbol table and the embedded proto-descriptor strings. Other versions differ.*

## Abstract

**SparseCore (SC)** is the TPU's on-die embedding / sparse-gather coprocessor. It sits next to the matrix-heavy **TensorCore (TC)** on the same die, shares the chip's HBM physical interface, and exists to do the one thing the TensorCore's systolic MXU cannot: stream *indirect* (index-driven) reads and writes against gigabyte-scale tables in HBM, including atomic floating-point scatter-add. The canonical workload is the embedding lookup of a recommender / DLRM-class model — gather millions of variable-length rows from an embedding table, reduce each sample's rows to one dense vector, hand the dense result to the TensorCore for the matmul, and on the backward pass scatter the gradient back into HBM by atomic add. The TensorCore's HBM controller is tuned for long contiguous bursts; SparseCore's is tuned for many short random transactions. That access-pattern split is the entire architectural justification for two engines instead of one.

SparseCore is not a TensorCore extension. It is a separate ISA family with its own opcode sets, its own register files, its own VLIW bundle formats, and its own per-generation encoder/decoder codecs. Inside SparseCore the compute fabric is itself split into **three sub-engine classes**, each driven by an independent VLIW bundle stream and selected by a `tpu::TpuSequencerType` enum value:

- **SCS** — *SparseCore Scalar* sequencer. The control / addressing CPU. 32-byte bundle. `TpuSequencerType = 3`.
- **TAC** — *Tile Access Core*. A specialised address-handler + DMA issuer; no vector compute. 64-byte bundle. `TpuSequencerType = 4`.
- **TEC** — *Tile Execute Core*. The wide vector engine that runs the per-tile reductions. 64-byte bundle. `TpuSequencerType = 5`.

The most consequential structural fact this binary records is that the roster is *not constant across silicon*. SparseCore appears first on the Viperfish generation as a three-engine SCS+TAC+TEC split; Ghostlite keeps all three; **6acc60406 drops TAC entirely**, folding tile-fetch issuance into the SCS+TEC pair. There is also a legacy monolithic predecessor, **SCv0**, which survives in this build only as two profiler-label constants — no SCv0 codec ships.

This page is navigational. It fixes what SparseCore is, names the three engine classes and their binary-evidenced per-generation presence, sketches the host→HBM→SparseCore→TensorCore data path, and routes to the page that owns each piece. The deep mechanics — opcode rosters, slot bit-layouts, the embedding scan datapath, the back-end pass pipeline — live on the sibling pages cross-referenced below.

For reimplementation, the contract is:

- **Three sub-engine classes, three independent bundle streams.** SCS, TAC, and TEC are distinct codecs (`SparseCore{Scs,Tac,Tec}CodecBase` per gen), each selected by its `TpuSequencerType` non-type template parameter (3 / 4 / 5). A reimplementer must treat them as three separate VLIW machines that coordinate through sync flags and shared memory, not one machine with three modes.
- **Per-gen presence is part of the contract.** Viperfish and Ghostlite ship all three engines; 6acc60406 ships SCS + TEC only. Emitting a TAC program for a 6acc60406 target is a codec error — the `gfc` namespace contains zero `SparseCoreTac*` symbols.
- **The data path is HBM-mediated and indirect.** Embedding tables live in HBM; SC gathers tile rows into a per-tile SRAM (`TILE_SPMEM`), the TEC reduces them, and the dense result is DMA'd to the TensorCore's VMEM (or scattered back to HBM). SC's `STREAM_OPCODE_SCATTER_FLOAT_ADD` runs atomic FP-add directly in HBM with no TC round-trip — the primitive that justifies SC as a separate engine.
- **SCv0 is enum-only.** The legacy single-personality SparseCore is preserved only as `TpuSequencerTypeProto` values 7 / 8 (the proto enum) and two profiler labels; it has no C++ `TpuSequencerType` value (`TpuSequencerTypeFromProto` rejects it) and no encoder, decoder, codec, or descriptor ships for it.

| | |
|---|---|
| **What it is** | On-die embedding / sparse-gather coprocessor, co-located with the TensorCore, sharing HBM |
| **Engine classes** | SCS (scalar sequencer) · TAC (tile-access / DMA) · TEC (vector compute) |
| **Sequencer enum** | C++ `tpu::TpuSequencerType` — SCS=3, TAC=4, TEC=5 (same as the codec template). SCv0 has no C++ value — see [getSequencerType](getsequencertype.md) for the proto-enum +1 peer |
| **Codec class roots** | `SparseCore{Scs,Tac,Tec}CodecBase` (per-gen, in `asic_sw::deepsea::<fam>::isa`) |
| **Gens with SC** | Viperfish (all 3) · Ghostlite (all 3) · 6acc60406 (SCS+TEC, no TAC) |
| **Gens without SC** | Jellyfish / Dragonfish / Pufferfish (BarnaCore era — see [BarnaCore Overview](../barnacore/overview.md)) |
| **Memory** | HBM (chip-wide) · SPMEM (per-chip SC SRAM) · TILE_SPMEM (per-tile) · TIMEM (per-tile instr) |
| **Confidence** | CONFIRMED (symbol-table / descriptor-anchored) unless a row or callout says otherwise |

---

## What SparseCore Is — and Why It Is Separate

A TensorCore is a statically-scheduled VLIW machine built around a systolic matrix unit; it is at its best when data arrives as dense tiles streamed contiguously out of HBM. Embedding-heavy models break that assumption: the dominant cost is not matmul FLOPs but *pointer-chasing* — reading a handful of rows out of a table with millions of rows, where which rows are touched is data-dependent and changes every minibatch. Forcing that traffic through the TensorCore's contiguous-burst HBM controller wastes the burst, and the systolic array's accumulation path has no way to do an atomic floating-point add into an arbitrary HBM address (the operation embedding-gradient accumulation needs on every backward step).

SparseCore exists to absorb exactly that traffic. It is a peer engine on the same die, with read/write access to the same HBM, but reaching HBM through a *different MMU translation surface and prefetcher policy* tuned for many short random transactions rather than long bursts. Its instruction set is built around gather, scatter, scan, sort, uniquify, and pack/unpack — the operations that turn a stream of variable-length index lists into dense embedding vectors. The SparseCore and TensorCore run in parallel and hand off through shared HBM, through programmed VMEM↔SPMEM DMA, and through a shared sync-flag pool.

> **NOTE — SparseCore is its own ISA family, not a TensorCore mode.** The binary carries fully independent `SparseCore{Scs,Tac,Tec}CodecBase` template hierarchies per generation, scoped under `asic_sw::deepsea::{vxc.vfc, gxc.glc, gxc.gfc}::isa`. These have their own bundle widths, opcode enums, and operand-field protos, distinct from the TensorCore's `LloOpcodeProto` / per-gen bundle encoders documented in [ISA Overview](../isa/overview.md). A reimplementer who models SparseCore as extra TensorCore slots will not produce encodable SC programs.

---

## The Three Engine Classes

Inside SparseCore the compute fabric is partitioned into three sub-engine classes, each a separate VLIW machine with its own bundle stream, its own program type, and its own codec. They are distinguished in the binary by the `tpu::TpuSequencerType` enum that the encoder template carries as a non-type parameter, and by the engine-specific name strings.

The value columns split the two numberings: **C++** is the `tpu::TpuSequencerType` enum the codec templates and `TpuSequencerTypeToString` both use (the wiki-canonical numbering); **proto** is the `TpuSequencerTypeProto` peer, which reserves an `INVALID=0` slot and so runs one higher (see [getSequencerType](getsequencertype.md) for the `FromProto` bridge).

| C++ | proto | `TpuSequencerTypeProto` literal | Short | Bundle | Role |
|:---:|:---:|---|---|---|---|
| 3 | 4 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_SEQUENCER` | SCS | 32 B | Scalar control / addressing sequencer |
| 4 | 5 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_ACCESS_CORE_SEQUENCER` | TAC | 64 B | Address generation + tile-fetch DMA issuer |
| 5 | 6 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_EXECUTE_CORE_SEQUENCER` | TEC | 64 B | Wide vector compute over loaded tiles |
| — | 7 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_V0_SEQUENCER` | SCv0 | (legacy) | Monolithic predecessor — proto-only, no C++ value, no codec |
| — | 8 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_V0_ADDRESS_HANDLER` | SCv0-AH | (legacy) | SCv0 address handler — proto-only, no C++ value, no codec |

**SCS — the scalar sequencer.** SCS is the control plane: it runs the program counter, computes addresses, manages circular buffers, reads chip registers (GTC clocks, tile id, sparse-core id, DMA credits), and issues the sync-flag and atomic operations that coordinate the SC tiles with each other and with the TensorCore. Its 32-byte bundle is the narrowest of the three. Its instruction set is dominated by a scalar ALU slot (integer + F32 arithmetic, compares, shifts, branches) and a "scalar misc" slot that holds the atomic-and-sync family. See [SCS (Scalar) Engine](scs-engine.md) and the [Scalar Opcode Enum](scalar-opcode-enum.md).

**TAC — the tile-access core.** TAC is a specialised address-handler + DMA issuer that sits between SCS and TEC. It has its own sequencer thread (branches, halt, delay), integer address arithmetic, compares, SMEM load/store for address-buffer staging, and a stream slot — but **no FPU, no vector ALU, no vector load/store**. Its sole job is to take a stream of indices and emit the tile-fetch DMAs that pull embedding rows from HBM/SPMEM into TILE_SPMEM. Its bundle is 64 bytes despite carrying no vector compute, because the width goes to many scalar-style address ops. TAC exists only on Viperfish and Ghostlite. See [TAC Engine](tac-engine.md).

**TEC — the tile-execute core.** TEC is the wide compute engine. A single 64-byte TEC bundle can issue three vector ALU operations, a vector load, a vector store, a "vector extended" op (scan / sort / uniquify), a "vector result" pop, two scalar ALU slots, immediates, a DMA, and a stream slot — all in one cycle. This is where the per-row reductions, the precision pack/unpack (including FP8 E4m3/E5m2 and sub-byte formats on the newest gen), and the embedding-optimizer math live. The vector ALU slot is the widest opcode set in all of SparseCore. See [TEC (Vector) Engine](tec-engine.md), the [Vector Opcode Enum](vector-opcode-enum.md), and the [Stream Gather/Scatter](stream-gather-scatter.md) datapath.

> **GOTCHA — TAC reuses SCS's proto-enum opcode space but constrains it.** The TAC sub-bundle does not declare a separate `SparseCoreTacScalarAlu` proto-enum namespace; it reuses the generic `SparseCoreScalarAlu` / `SparseCoreScalarMisc` enums and gates legality in the `SparseCoreTacScalarAlu{0,1}Encoder` classes during emit. A reimplementer cannot enumerate "TAC's opcodes" from a dedicated enum — TAC legality is the SCS scalar set minus the FPU and vector ops, enforced at encode time.

---

## Per-Generation Presence

SparseCore is a v5+ feature. The generations that ship SC silicon are Viperfish, Ghostlite, and 6acc60406; the earlier BarnaCore-era generations (Jellyfish / Dragonfish / Pufferfish) carry no SparseCore. Which sub-engines are present is itself per-generation, and is the single most important fact a reimplementer must encode.

The discriminator is the codec class family. SparseCore codecs are scoped under the per-generation `asic_sw::deepsea` family namespace — `vxc.vfc` (Viperfish), `gxc.glc` (Ghostlite), `gxc.gfc` (6acc60406) — and the presence or absence of a `SparseCore<Engine>CodecBase` class in that namespace is a direct binary readout of whether the engine ships.

| Gen | Codename | Family ns | SCS | TAC | TEC | SCv0 | Notes |
|---|---|---|:---:|:---:|:---:|:---:|---|
| TPU v2 | Jellyfish | `jxc` | – | – | – | – | No SparseCore (BarnaCore era) |
| TPU v3 | Dragonfish | `jxc` | – | – | – | – | No SparseCore (shares Jellyfish codec) |
| TPU v4 | Pufferfish | `pxc.pfc` | – | – | – | – | No SparseCore (BarnaCore VLIW) |
| TPU v5p | Viperfish | `vxc.vfc` | **Y** | **Y** | **Y** | – | First gen with the three-engine split |
| TPU v6e | Ghostlite | `gxc.glc` | **Y** | **Y** | **Y** | – | Full SCS+TAC+TEC; widened vector set |
| TPU7x | 6acc60406 | `gxc.gfc` | **Y** | **–** | **Y** | – | **TAC removed**; TEC widened (FP8/FP4 pack/unpack) |

> **NOTE — codename-to-marketing-name mapping.** SparseCore appears in the binary under the silicon codenames Viperfish, Ghostlite, and `6acc60406` — never under any marketing string. The marketing-name column above follows the SparseCore-specific sibling [SparseCore Target Descriptor](../targets/sparsecore-target-descriptor.md) (`nSparseCoreTarget` = v5p; `GhostLiteSparseCoreTarget` = v6e Ghostlite + v7x `6acc60406`); the binary itself keys everything on the codename family namespace and the `TpuVersion` ordinal carried inside the SC codec templates, not on the marketing name. (Some sibling parts label Viperfish `v5`/`v5e`; the codename + family namespace is the only binary-authoritative discriminator. For the record, Google's *Trillium* marketing name is TPU v6e, i.e. codename Ghostlite — **not** `6acc60406`, which is TPU7x.) Treat the codename + family namespace as authoritative; the marketing column is MEDIUM confidence.

### Decompile cross-check — engine roster and TAC absence

The roster and `6acc60406`'s missing TAC were confirmed directly against the decompiled function set. Counting decompiled `SparseCore<Engine>*` functions per family namespace:

| Family ns | Gen | `SparseCoreScs*` fns | `SparseCoreTac*` fns | `SparseCoreTec*` fns |
|---|---|---:|---:|---:|
| `vxc.vfc` | Viperfish | 509 | 770 | 4218 |
| `gxc.glc` | Ghostlite | 498 | 762 | 5961 |
| `gxc.gfc` | 6acc60406 | 493 | **0** | 6526 |

The `gfc` (6acc60406) namespace has **zero** `SparseCoreTac*` decompiled functions, against ~760–770 for each of Viperfish and Ghostlite — TAC is entirely absent from 6acc60406 silicon. (The single `gfc`-tagged `SparseCoreTac` symbol that survives, `llvm::TPUGfcSubtarget::isSparseCoreTac`, is a subtarget *query* that returns false — not a codec leaf.) The corresponding `SparseCoreTacCodecBase` class exists only under `vxc.vfc` and `gxc.glc`, never `gxc.gfc`. Conversely the TEC function count *grows* gen over gen (4218 → 5961 → 6526), consistent with TAC's tile-fetch role being folded into the SCS+TEC pair on 6acc60406. SCS is present and roughly constant in all three.

> **CONFIRMED — 6acc60406 collapses the 3-engine pipeline to 2.** On Viperfish/Ghostlite the path is SCS → TAC (tile-fetch DMA) → TEC (compute). On 6acc60406 TAC is gone and TEC absorbs the address-generation + DMA-issue duties through its own stream slot, leaving a single SCS↔TEC boundary. This is the "2-sequencer (SCS+TEC) model." See [TAC Engine](tac-engine.md) for the absorbed-role detail.

### SCv0 — the legacy monolithic predecessor

The proto enum `TpuSequencerTypeProto` reserves two values (7, 8) for a single-personality SparseCore that predates the SCS/TAC/TEC split (the C++ `TpuSequencerType` enum has no SCv0 value — `TpuSequencerTypeFromProto` rejects proto 7/8), plus a `TPU_CORE_TYPE_SPARSE_CORE_V0` core-type. In this build SCv0 survives **only** as two profiler-label constants — `kHloSparseCoreV0Infeed` and `kHloSparseCoreV0Outfeed`, referenced from the XLA HLO profiler's `DisambiguateInfeedOutfeed`. No SCv0 encoder, decoder, codec metadata, scheduling table, or descriptor proto ships. A user proto naming SCv0 will fail at codec lookup. The enum values are retained for schema back-compat only.

> **GOTCHA — the `V0` suffix on `6acc60406` TEC field symbols is *not* SCv0.** Decompiled symbols such as `gfc::isa::SparseCoreTecVectorExtendedAddScanF32V0XField` carry a `V0` that is an operand-field version tag on a live `6acc60406` TEC vector-extended op — unrelated to the SCv0 core type. The only genuine SCv0 references are the two profiler labels above.

---

## The Data Path

SparseCore's reason to exist is the embedding lookup. The high-level flow moves host-resident embedding tables into HBM, gathers tile rows on demand, reduces them on TEC, and hands the dense result to the TensorCore — with the gradient flowing back on the reverse path.

```text
HOST                         HBM (shared TC/SC)            SPARSECORE                    TENSORCORE
────                         ─────────────────             ──────────                    ──────────
embedding tables  ──load──▶  embedding rows                                              matmul / MLP
                             (GB-scale, indirect)
                                   │
                  index stream ────┤  STREAM_OPCODE_GATHER          SCS schedules
                                   ▼  ───────────────────────▶      tile-fetch program
                             [HBM row r_i]                                │
                                                          TAC* / TEC stream slot
                                                          issues tile-fetch DMA
                                                                         ▼
                                                          TILE_SPMEM  ◀── row tiles
                                                                         │
                                                          TEC vector reduce
                                                          (sum / max / weighted)
                                                                         │
                                              DMA  VMEM ◀────────────────┘  dense vector
                                              (DMA_CORE_ID_TENSOR_CORE_0,
                                               DMA_MEMORY_ID_VMEM)       ──sync flag──▶  consume
                                                                                          │
        ── backward pass ──                                                               ▼
                                                          TEC loads grad slice ◀── DMA  SPMEM
                                                          applies optimizer math
                                                                         │
                             [HBM row r_i] ◀── STREAM_OPCODE_SCATTER_FLOAT_ADD (atomic, in-HBM)
```

The forward pass: the TensorCore requests the next minibatch's embeddings; SCS receives and schedules a tile-fetch program; on Viperfish/Ghostlite TAC issues the stream-gather DMA from the HBM table into TILE_SPMEM, while on `6acc60406` the TEC's own stream slot does it; TEC vector-loads the tiles, runs the per-sample reduction, and DMAs the dense result into the TensorCore's VMEM, then raises a sync flag the TC waits on. The backward pass runs in reverse: the TC writes gradient slices to SPMEM, TEC loads them and applies the embedding-optimizer math (the newest gen's TEC supports stochastic round-to-bf16 and packed FP8 formats specifically for optimizer-state quantisation), and SC's stream engine issues `STREAM_OPCODE_SCATTER_FLOAT_ADD` to the embedding-row addresses in HBM — an atomic floating-point add that lands directly in HBM with no TensorCore round-trip.

SparseCore owns four address spaces, of decreasing scope and increasing speed: **HBM** (chip-wide, GB-scale, embedding tables + gradient buffers), **SPMEM** (all SC cores on chip, MB-scale, cross-SC communication and large buffers), **TILE_SPMEM** (per-tile, KB-scale, the local working set the TEC computes over), and **TIMEM** (per-tile instruction memory). The handoff with the TensorCore uses three mechanisms — HBM (slow, global), programmed VMEM↔SPMEM DMA (fast), and a shared sync-flag pool (control plane). The detail of the gather/scatter descriptor format is on [Stream Gather/Scatter](stream-gather-scatter.md); the SC↔MXU integration handshake is on [SC ↔ MXU Handshake](sc-mxu-handshake.md).

> **NOTE — the in-HBM atomic add is the keystone primitive.** `STREAM_OPCODE_SCATTER_FLOAT_ADD` (paired with the DMA destination opcode `DMA_DEST_OPCODE_READ_AND_ADD`) accumulates floating-point gradients directly into embedding rows in HBM. The TensorCore's MXU cannot do this. Without it, embedding gradients would require either shrinking the table to fit VMEM or a multi-pass scatter-gather-add. In DLRM-class models this single op carries the gradient-accumulation traffic for every embedding table on every backward step — the architectural reason SparseCore is worth its silicon.

---

## How Part IX Is Organized

Part IX keeps SparseCore whole rather than slicing it across the ISA / cost / scheduling seam used by the TensorCore parts, because it is a self-contained engine a reader wants in one place. The pages group into bands:

- **Engines (this band)** — this overview, plus [Architecture](architecture.md) (engine roles + the embedding datapath in depth), [SCS (Scalar) Engine](scs-engine.md), [TAC Engine](tac-engine.md), [TEC (Vector) Engine](tec-engine.md), the per-engine [Bundle Slot-Base Map](bundle-slot-base-map.md), the [Region → Sequencer Outliner](region-to-sequencer-outliner.md) that partitions an SC computation into per-engine bundles, and [getSequencerType](getsequencertype.md) (the SCS/TAC/TEC selection function).
- **ISA** — the opcode enums and slot encodings: [Scalar Opcode Enum](scalar-opcode-enum.md), [Vector Opcode Enum](vector-opcode-enum.md), the [OneSlot Scalar Router](oneslot-router.md), the [VectorLoad](vectorload-slot.md) / [VectorStore](vectorstore-slot.md) / [VectorExtended / VEX](vectorextended-vex.md) slot pages, [VEX Operand-Port Binding](vex-operand-port.md), [VEX Mask / Dest-Port / Sub-Opcode](vex-mask-destport-subopcode.md), the [M-Register Predicate Word](m-register-predicate.md), and the [CBREG Circular-Buffer Register](cbreg.md).
- **Embedding datapath** — the sparse compute: [Scan Datapath](scan-datapath.md), [Segmented Scan](segmented-scan.md), [Segmented-Add-Scan](segmented-add-scan.md), [Embedding Minibatching Decomposition](embedding-minibatching.md), [SampleCombiner Emitter](sample-combiner-emitter.md), [EmitValencyLoop](emit-valency-loop.md), [RankAndPermute / RadixSort](rank-and-permute-radixsort.md), and [Dedup Multiplicity](dedup-multiplicity.md).
- **Pointers & DMA** — the addressing model: [Fat Pointers (AS7/8/9)](fat-pointers-as789.md), [addrspacecast ISel](addrspacecast-isel.md), [Tile-ID Cast](tile-id-cast.md), [Stream Gather/Scatter](stream-gather-scatter.md), and [IndirectVregStream](indirect-vreg-stream.md).
- **Back-end** — the compiler offload path: [SC Backend Pipeline](sc-backend-pipeline.md) (RunPasses and the MEGACORE barrier), [SC EmitX Dispatcher](sc-emitx-dispatcher.md) (the seq3/seq4/seq5 → EmitX jump tables), [SC Core Selection](sc-core-selection.md), [SC Queue Assignment & Reservation](sc-queue-assignment-reservation.md), and [GetSparseCoreConfig](getsparsecoreconfig.md).
- **Cross-cutting** — [SC ↔ MXU Handshake](sc-mxu-handshake.md) and the cross-vendor [SparseCore vs Neuron MatmultSparse](sparsecore-vs-neuron-matmultsparse.md).

The retired predecessor, **BarnaCore** (the embedding accelerator on Jellyfish / Dragonfish / Pufferfish, replaced by SparseCore from Viperfish onward), has its own band starting at [BarnaCore Overview](../barnacore/overview.md). The TensorCore ISA that SparseCore hands off to is [ISA Overview](../isa/overview.md); the collective-offload story lives in Part XIII.

---

## Confidence Summary

| Claim | Evidence |
|---|---|
| Three engine classes SCS/TAC/TEC, selected by `TpuSequencerType` 3/4/5 | `TpuSequencerType` enum (`TpuSequencerTypeToString` @ `0x20b362e0` jump table over `off_22010DE0`); `EncoderBase<…, TpuSequencerType=3/5>` template instantiations |
| Per-gen codec roots `SparseCore{Scs,Tac,Tec}CodecBase` scoped `vxc.vfc` / `gxc.glc` / `gxc.gfc` | demangled codec-base class symbols in the decompiled set |
| Viperfish + Ghostlite ship all three engines | `vxc.vfc` and `gxc.glc` each have Scs/Tac/Tec codec bases; fn counts Scs≈500, Tac≈760, Tec 4218/5961 |
| `6acc60406` drops TAC (SCS+TEC only) | zero `gfc::…SparseCoreTac*` symbols / functions; no `gxc.gfc` `SparseCoreTacCodecBase`; TEC fn count 6526 |
| Jellyfish / Dragonfish / Pufferfish carry no SparseCore | no `SparseCore*CodecBase` under `jxc` / `pxc.pfc`; these are BarnaCore-era gens |
| SCv0 is enum-only (no codec) | `TpuSequencerTypeProto` 7/8 (proto-only; no C++ value, `FromProto` rejects) + `kHloSparseCoreV0{Infeed,Outfeed}` profiler labels; no SCv0 encoder/decoder/descriptor ships |
| SCS=32 B, TAC=64 B, TEC=64 B bundles; no check byte | `BundleSizeBytes` reads codec-metadata vtable slot returning 32/64; SC bundles carry no `0x55` trailer |
| In-HBM atomic FP scatter-add is the keystone primitive | `STREAM_OPCODE_SCATTER_FLOAT_ADD` + `DMA_DEST_OPCODE_READ_AND_ADD` enum strings |
| `V0` suffix on `6acc60406` TEC field symbols is an operand-version tag, not SCv0 | `gfc::isa::SparseCoreTecVectorExtended*V0XField` are live `6acc60406` TEC ops |
| Marketing names v5p/v6e/v7x for codenames Viperfish/Ghostlite/`6acc60406` | follows sibling [SparseCore Target Descriptor](../targets/sparsecore-target-descriptor.md); binary keys on codename family ns, not marketing name |

---

## Cross-References

- [Architecture](architecture.md) — engine roles and the embedding datapath in full depth.
- [SCS (Scalar) Engine](scs-engine.md) — the scalar control / addressing sequencer.
- [TAC Engine](tac-engine.md) — the tile-access / DMA-issuer engine and its `6acc60406` removal.
- [TEC (Vector) Engine](tec-engine.md) — the wide vector compute engine.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-selection function.
- [Region → Sequencer Outliner](region-to-sequencer-outliner.md) — partitioning an SC computation into per-engine bundle streams.
- [Scalar Opcode Enum](scalar-opcode-enum.md) — the SCS / TAC scalar ALU and scalar-misc opcode roster.
- [Vector Opcode Enum](vector-opcode-enum.md) — the TEC vector ALU opcode roster (per-gen widths).
- [Stream Gather/Scatter](stream-gather-scatter.md) — the indirect-DMA descriptor format and the `STREAM_OPCODE_*` set.
- [SC Backend Pipeline](sc-backend-pipeline.md) — the SparseCore compiler offload pass pipeline.
- [SC ↔ MXU Handshake](sc-mxu-handshake.md) — the SparseCore ↔ TensorCore integration handshake.
- [GetSparseCoreConfig](getsparsecoreconfig.md) — the offload op-type configuration source.
- [SparseCore vs Neuron MatmultSparse](sparsecore-vs-neuron-matmultsparse.md) — cross-vendor comparison.
- [ISA Overview](../isa/overview.md) — the TensorCore VLIW ISA SparseCore hands off to.
- [BarnaCore Overview](../barnacore/overview.md) — the retired v2–v4 embedding accelerator SparseCore replaced.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore engines — [back to index](../index.md)
