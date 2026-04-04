# Mercury Compiler Passes

The Mercury pipeline inside nvlink's embedded ptxas backend uses 22 ROT13-obfuscated boolean option passes and 6 plaintext pipeline stages specific to Mercury targets (sm100+). The obfuscated passes are registered as LLVM-style `cl::opt<bool>` command-line options during static construction (`ctor_007`), each controlling a specific codegen behavior for Mercury instruction generation, scheduling, and finalization. The pipeline stages are entries in the master scheduling-phase table that runs after register allocation, performing the Mercury-specific encode-decode, expand, WAR insertion, opex generation, and UCode emission sequence.

This page catalogs every Mercury-specific pass, decodes its ROT13 name, identifies its registration address and option-bit offset, and describes its likely purpose based on name semantics and cross-references to related infrastructure.

## Key Facts

| Property | Value |
|---|---|
| ROT13-encoded pass count | 22 (registered in `ctor_007` at `0x425A40`--`0x426080`) |
| Plaintext pipeline stages | 6 (`MercEncodeAndDecode` through `MercGenerateSassUCode`) |
| Registration constructor | `ctor_007` (static initializer, runs before `main`) |
| Pipeline stage table base | `0x24443F0` (within master phase dispatch table at `0x2443F80`) |
| MercExpand engine entry | `sub_5FF110` (prints `"After MercExpand"` at `0x1DFE320`) |
| MercConverter entry | referenced at `0x19798F8` (prints `"After MercConverter"` at `0x241F913`) |
| MercWARs entry | `sub_4A47F0` (prints `"After MercWARs"` at `0x1D41C60`) |
| MercOpex entry | `sub_4ABB70` (prints `"After MercOpex"` at `0x1D41C6F`) |
| Related global options | `UseMercSemantics` (`0x23F34F0`), `UseMercResources` (`0x23F3510`) |
| Diagnostics option | `DumpMercOpCounts` (`0x1D4AB40`, registered in `ctor_004`) |

## ROT13 Obfuscation

Every Mercury-specific pass name in the binary is stored as a ROT13 string. The pattern `Zrephel` decodes to `Mercury`. This obfuscation is consistent across all ptxas/nvlink internal option names -- standard Ori/Advanced passes use the same scheme. The encoding serves as a minimal deterrent against casual string searching; it does not provide any security.

Each ROT13 option is paired with a hex offset string (also ROT13-encoded) that identifies the option's bit position within a global flags structure. For example, `0k3r40` decodes to `0x3e40`.

## The 22 ROT13-Encoded Passes

Listed in registration order (highest `ctor_007` address first, which corresponds to reverse construction order). The "Bit offset" column shows the decoded option-bit position.

### Pass Catalog

| # | ROT13 string | Decoded name | Bit offset | String addr | Registration addr |
|---|---|---|---|---|---|
| 1 | `ZrephelHfrNpgvirGuernqPbyyrpgvirVafgf` | **MercuryUseActiveThreadCollectiveInsts** | `0x3e40` | `0x23F2B00` | `0x426080` |
| 2 | `ZrephelGenpxZhygvErnqfJneYngrapl` | **MercuryTrackMultiReadsWarLatency** | `0x3e30` | `0x23F2B40` | `0x426030` |
| 3 | `ZrephelGrcvqNjnerFo` | **MercuryTepidAwareSb** | `0x3e20` | `0x23F2B70` | `0x425FE0` |
| 4 | `ZrephelCerfhzrKoybpxJnvgOrarsvpvny` | **MercuryPresumeXblockWaitBeneficial** | `0x3e18` | `0x23F2BA0` | `0x425F90` |
| 5 | `ZrephelZretrCebybthrOybpxf` | **MercuryMergePrologueBlocks** | `0x3e10` | `0x23F2BD0` | `0x425F40` |
| 6 | `ZrephelVffhrQrynlJOFgnyyFrysYbbc` | **MercuryIssueDelayWBStallSelfLoop** | `0x3e00` | `0x23F2C00` | `0x425EF0` |
| 7 | `ZrephelVafregKoybpxJnvg` | **MercuryInsertXblockWait** | `0x3df0` | `0x23F2C30` | `0x425EA0` |
| 8 | `ZrephelVafregOnpxrqtrQrcone` | **MercuryInsertBackedgeDepbar** | `0x3de0` | `0x23F2C60` | `0x425E50` |
| 9 | `ZrephelVafregNffhzrf` | **MercuryInsertAssumes** | `0x3dd0` | `0x23F2C90` | `0x425E00` |
| 10 | `ZrephelTraFnffHPbqr` | **MercuryGenSassUCode** | `0x3dc0` | `0x23F2CB0` | `0x425DB0` |
| 11 | `ZrephelSbeprHaxabjaGptra05Ngge` | **MercuryForceUnknownTcgen05Attr** | `0x3db9` | `0x23F2CD0` | `0x425D60` |
| 12 | `ZrephelSbeprVFNPynff` | **MercuryForceISAClass** | `0x3db8` | `0x23F2D00` | `0x425D10` |
| 13 | `ZrephelRapbqrArjJbexreSvyrf` | **MercuryEncodeNewWorkerFiles** | `0x3db0` | `0x23F2D20` | `0x425CC0` |
| 14 | `ZrephelRapbqrQrpbqr` | **MercuryEncodeDecode** | `0x3da0` | `0x23F2D50` | `0x425C70` |
| 15 | `ZrephelQhzcVafgfNfOvanel` | **MercuryDumpInstsAsBinary** | `0x3d90` | `0x23F2D70` | `0x425C20` |
| 16 | `ZrephelQvfnoyrYrtnyvmngvbaBsGrkGbHEObhaq` | **MercuryDisableLegalizationOfTexToURBound** | `0x3d80` | `0x23F2DA0` | `0x425BD0` |
| 17 | `ZrephelQrcFgntrCersreAbaYvirvaCFO` | **MercuryDepStagePreferNonLiveinPSB** | `0x3d78` | `0x23F2DE0` | `0x425B80` |
| 18 | `ZrephelPbairegreFgngf` | **MercuryConverterStats** | `0x3d70` | `0x23F2E10` | `0x425B30` |
| 19 | `ZrephelPbafhzrNffhzrf` | **MercuryConsumeAssumes** | `0x3d60` | `0x23F2E30` | `0x425AE0` |
| 20 | `ZrephelPbzcnpgrqNffhzrf` | **MercuryCompactedAssumes** | `0x3d50` | `0x23F2E50` | `0x425A90` |
| 21 | `ZrephelNffhzrCGKCbegnovyvgl` | **MercuryAssumePTXPortability** | `0x3d40` | `0x23F2E80` | `0x425A40` |
| 22 | `NqinaprqFOPebffOybpxZrephelNffhzr` | **AdvancedSBCrossBlockMercuryAssume** | `0x5b0` | `0x23FC820` | `0x4129E0` |

Pass 22 (`AdvancedSBCrossBlockMercuryAssume`) is registered separately from the main Mercury block, within the `AdvancedSB*` option group at `0x4129E0`. It bridges the Mercury assume system with the cross-block scoreboard analysis.

## Pass Descriptions

### Instruction Encoding and Expansion

**MercuryEncodeDecode** (pass 14) -- Controls the Mercury instruction encode/decode round-trip. Mercury instructions use a compact intermediate encoding that differs from the final SASS binary format. This pass enables the round-trip: encode to Mercury format, then decode back to an internal representation. It is the gate for the `MercEncodeAndDecode` pipeline stage (`0x24443F0`). The related pipeline stage function emits `"After MercExpand"` diagnostics from `sub_5FF110`.

**MercuryEncodeNewWorkerFiles** (pass 13) -- Gates re-encoding of instruction data into Mercury's worker-file format. Mercury splits kernel code into worker files for parallel processing by the FNLZR. When enabled, the encoder produces fresh worker-file payloads reflecting the current instruction state after optimization passes. This supports the capsule Mercury (capmerc) output format where Mercury IR travels alongside SASS.

**MercuryDumpInstsAsBinary** (pass 15) -- Debug/diagnostic pass. When enabled, dumps the Mercury instruction stream in raw binary form, enabling offline analysis of the encoded instruction payload. This is complementary to `DumpMercOpCounts` (registered separately in `ctor_004` at `0x410F30`) which dumps per-opcode instruction counts.

### Assume Framework

The "assume" passes form a coherent subsystem that manages dataflow assumptions across the Mercury pipeline. These assumptions let the scheduler operate without full data-flow recomputation after each transformation.

**MercuryInsertAssumes** (pass 9) -- Inserts initial assumption annotations into the Mercury instruction stream. Assumes are metadata that record properties like "this register is not modified between points A and B" or "this dependency barrier is still valid." They allow later passes to trust invariants without re-analyzing.

**MercuryConsumeAssumes** (pass 19) -- The complement of InsertAssumes. After a transformation pass uses the assumptions, this pass consumes (removes) them from the stream, preventing stale assumptions from persisting into subsequent passes.

**MercuryCompactedAssumes** (pass 20) -- Enables a compacted representation of assumption annotations. Instead of full-width assume records, this packs multiple assumptions into fewer bits, reducing metadata overhead in the instruction stream.

**MercuryAssumePTXPortability** (pass 21) -- Controls whether the assume framework treats PTX-level semantics as portable across Mercury transformations. When enabled, the compiler assumes that PTX-level operational semantics are preserved through the Mercury encode-decode pipeline, allowing more aggressive optimization under the assumption that PTX portability constraints hold.

**AdvancedSBCrossBlockMercuryAssume** (pass 22) -- Extends the scoreboard-based cross-block analysis to incorporate Mercury assume information. In the `AdvancedSB*` framework (which manages scoreboard allocation, dependency barrier placement, and stall-count computation across basic block boundaries), this option allows the cross-block propagation to use Mercury assume annotations as additional constraints, potentially reducing unnecessary stalls at block transitions.

### Scoreboard and Scheduling Control

**MercuryTepidAwareSb** (pass 3) -- Makes the scoreboard allocator aware of "tepid" instructions. In NVIDIA's scheduling model, a "tepid" instruction is one that does not require immediate scheduling -- it occupies a pipeline stage without time-critical latency constraints. The `TepidMacUtil` metric (`0x23EF746`) and `TepidTime` (`0x23F0851`) track the ratio of math-to-DMA tepid utilization. When this pass is enabled, scoreboard assignment considers tepid status, potentially freeing scoreboard entries for instructions with real latency pressure. Related metrics: `MathToDmaTepidRatio` (`0x23FCD5C`), `MathToEpilogueTepidRatio` (`0x23FCD8B`).

**MercuryTrackMultiReadsWarLatency** (pass 2) -- Enables precise latency tracking for write-after-read (WAR) hazards involving multiple read consumers. When a single write has multiple outstanding reads, the WAR latency must account for the slowest reader. This pass tracks all readers and computes the correct stall count for WAR insertion.

**MercuryPresumeXblockWaitBeneficial** (pass 4) -- Controls a heuristic in the cross-block wait insertion algorithm. When enabled, the scheduler presumes that inserting an `XBLOCK.WAIT` instruction at certain points will be beneficial for performance, even when the cost-benefit analysis is inconclusive. `XBLOCK.WAIT` synchronizes across execution blocks in the Mercury scheduling model.

**MercuryInsertXblockWait** (pass 7) -- Gates the actual insertion of `XBLOCK.WAIT` instructions into the instruction stream. While pass 4 controls the heuristic presumption, this pass is the mechanical gate that enables or disables the insertion transformation itself.

**MercuryInsertBackedgeDepbar** (pass 8) -- Controls insertion of dependency barriers on loop backedges. In a loop, a backedge creates a cycle where the head of the loop may depend on results from the tail. Without a dependency barrier at the backedge, the hardware scoreboard may not correctly track cross-iteration dependencies. This pass inserts `DEPBAR` instructions at identified backedge points. Related infrastructure: `AdvancedSBDepbarBackedge` (`0x23FC790`), `AdvancedSBReqBackedge` (`0x23FC610`).

**MercuryDepStagePreferNonLiveinPSB** (pass 17) -- Controls the dependency-stage allocation policy within the Pending Scoreboard (PSB). When enabled, the allocator prefers to assign non-live-in entries (values defined within the current block) to the PSB before consuming live-in entries (values flowing in from predecessors). This reduces unnecessary cross-block stalls. Related options: `AdvancedSBReqBeforeUsingLiveInPsb` (`0x23FC5E0`), `AdvancedSBFirstLLSBPsb` (`0x23FC650`).

**MercuryIssueDelayWBStallSelfLoop** (pass 6) -- Controls the handling of write-back stall conditions in self-loops (single-block loops where the backedge targets the same block). When a write-back produces a stall in such a loop, the instruction must wait for the result before re-executing. This pass inserts appropriate issue-delay annotations to prevent the hardware from issuing the dependent instruction too early.

### ISA and Target Control

**MercuryForceISAClass** (pass 12) -- Forces a specific ISA class assignment for Mercury instructions. The ISA class determines which functional unit executes an instruction (e.g., math, memory, texture, tensor core). When enabled, this overrides the default ISA class derivation, typically used for debugging or for instructions whose class cannot be determined from the opcode alone.

**MercuryForceUnknownTcgen05Attr** (pass 11) -- Forces the `tcgen05` tensor core generation 05 attribute to "unknown" for specific instructions. `tcgen05` is the Blackwell-generation tensor core instruction set (`tcgen05.mma`, `tcgen05.ld`, `tcgen05.st`, `tcgen05.cp`, etc.). When this pass is enabled, the compiler treats certain `tcgen05` operations as having unknown scheduling attributes, forcing conservative dependency handling. This is a safety mechanism for new `tcgen05` operations whose latency characteristics are not yet characterized. Related ELF attribute: `EIATTR_TCGEN05_1CTA_USED` (`0x1D36E41`), `EIATTR_TCGEN05_2CTA_USED` (`0x1D36E5A`).

**MercuryUseActiveThreadCollectiveInsts** (pass 1) -- Enables the use of active-thread collective instructions in Mercury codegen. Active-thread collectives are warp-level operations (like `vote`, `match`, `redux`) that operate only on threads that are currently active, without requiring explicit convergence. This pass enables the Mercury backend to emit these instructions rather than falling back to explicit synchronization patterns.

### Legalization and Codegen

**MercuryDisableLegalizationOfTexToURBound** (pass 16) -- Disables a specific legalization transform that converts texture instructions to uniform-register-bound forms. In SASS, texture operations can be bound to uniform registers for address computation; the legalization pass normally converts eligible `TEX` instructions to this form for better register utilization. Disabling this is useful when the uniform-register-bound form causes correctness issues or when the texture unit's interaction with Mercury scheduling is not well characterized.

**MercuryGenSassUCode** (pass 10) -- Controls the final SASS microcode generation from Mercury representation. This is the terminal codegen step: the Mercury-format instructions are translated into their final SASS binary encoding. The corresponding pipeline stage is `MercGenerateSassUCode` (`0x2444418`). The output is the `.text` section content, formatted as UCode -- NVIDIA's internal term for the final hardware-executable instruction encoding. Related dump stages: `DumpNVuCodeText` (`0x2444458`), `DumpNVuCodeHex` (`0x2444460`).

**MercuryMergePrologueBlocks** (pass 5) -- Enables merging of prologue basic blocks. The function prologue in Mercury code may be split across multiple basic blocks (e.g., for parameter setup, shared memory initialization, barrier setup). This pass merges them into a single prologue block, improving scheduling by giving the scheduler a larger instruction window at function entry.

### Diagnostics

**MercuryConverterStats** (pass 18) -- Enables statistical output from the MercConverter subsystem. MercConverter is the transformation engine at `0x19798F8` (prints `"After MercConverter"`) that converts instructions between representations during scheduling. When enabled, this pass prints conversion statistics: instruction counts, conversion success rates, and swap-phase metrics. The converter operates in named phases and includes sub-phases: `shuffle`, `swap1` through `swap6`, `OriPerformLiveDead`, and `OriCopyProp`.

## Pipeline Stage Sequence

The 6 Mercury-specific pipeline stages run in the post-register-allocation, post-scheduling region of the master phase table. They execute in strict order after the generic SASS finalization stages.

### Context in the Master Phase Table

The complete late-pipeline sequence from `PostSchedule` through the Mercury stages:

```
PostSchedule                    (0x24443A8)
AdvancedPhasePostFixUp          (0x24443B0)
PlaceBlocksInSourceOrder        (0x24443B8)
PostFixForMercTargets           (0x24443C0)   <-- Mercury-specific fixups
FixUpTexDepBarAndSync           (0x24443C8)
AdvancedScoreboardsAndOpexes    (0x24443D0)   <-- scoreboard/opex dispatch
ProcessO0WaitsAndSBs            (0x24443D8)
  [gap: 0x24443E0, 0x24443E8 -- no entries found]
MercEncodeAndDecode             (0x24443F0)   <-- Mercury stage 1
MercExpandInstructions          (0x24443F8)   <-- Mercury stage 2
MercGenerateWARs1               (0x2444400)   <-- Mercury stage 3
MercGenerateOpex                (0x2444408)   <-- Mercury stage 4
MercGenerateWARs2               (0x2444410)   <-- Mercury stage 5
MercGenerateSassUCode           (0x2444418)   <-- Mercury stage 6
ComputeVCallRegUse              (0x2444420)
CalcRegisterMap                 (0x2444428)
UpdateAfterPostRegAlloc         (0x2444430)
ReportFinalMemoryUsage          (0x2444438)
AdvancedPhaseOriPhaseEncoding   (0x2444440)
UpdateAfterFormatCodeList       (0x2444450)
DumpNVuCodeText                 (0x2444458)
DumpNVuCodeHex                  (0x2444460)
```

### Stage Descriptions

**Stage 1: MercEncodeAndDecode** (`0x24443F0`) -- Encodes the current instruction representation into Mercury's compact binary format and decodes it back into an expanded internal form. This round-trip serves two purposes: (a) it validates that the Mercury encoding is lossless, and (b) it normalizes the instruction representation to the form expected by subsequent Mercury-specific passes. Gated by the `MercuryEncodeDecode` option (pass 14).

**Stage 2: MercExpandInstructions** (`0x24443F8`) -- Expands Mercury-format macro instructions into their constituent SASS micro-operations. A single Mercury instruction may map to multiple SASS instructions (e.g., a fused memory operation expanding into address calculation + load + barrier). The MercExpand engine at `sub_5FF110` executes this expansion and emits the `"After MercExpand"` diagnostic. This is the most substantial transformation in the Mercury pipeline -- it is where the abstract Mercury encoding becomes concrete SASS.

**Stage 3: MercGenerateWARs1** (`0x2444400`) -- First pass of write-after-read hazard insertion. After instruction expansion, new WAR dependencies arise between expanded micro-operations. This pass analyzes the post-expansion instruction stream and inserts stall counts, yield hints, or `DEPBAR.WAIT` instructions to cover WAR hazards. The entry function `sub_4A47F0` prints `"After MercWARs"`. The `MercuryTrackMultiReadsWarLatency` option (pass 2) controls multi-reader tracking precision during this pass.

**Stage 4: MercGenerateOpex** (`0x2444408`) -- Generates "opex" (operation execution) annotations. Opex annotations describe instruction execution properties: pipeline throughput, latency class, and resource requirements. The scoreboard system (`AdvancedScoreboardsAndOpexes`) uses these annotations to assign scoreboard entries and compute stall counts. Entry function: `sub_4ABB70` (prints `"After MercOpex"`). The overall flow mirrors the non-Mercury opex generation (`"After Opex"`, `"After WAR post-opexing"` at `sub_49D8A0`) but adapted for Mercury-expanded instructions.

**Stage 5: MercGenerateWARs2** (`0x2444410`) -- Second pass of WAR hazard insertion, post-opex. After opex annotations assign concrete latencies, some WAR stalls computed in stage 3 may be pessimistic (the opex-derived latency is shorter than the conservative estimate). This pass refines the WAR stalls using opex information. The two-pass approach (WAR1 -> Opex -> WAR2) matches the non-Mercury pipeline's `"After WAR post-expansion"` -> `"After Opex"` -> `"After WAR post-opexing"` sequence visible at `sub_49D8A0`.

**Stage 6: MercGenerateSassUCode** (`0x2444418`) -- Terminal stage. Translates the fully expanded, scheduled, WAR-annotated instruction stream into final SASS UCode binary encoding. Each instruction is encoded into its hardware bit pattern. The output feeds directly into `CalcRegisterMap` and the UCode dump stages (`DumpNVuCodeText`, `DumpNVuCodeHex`). Gated by the `MercuryGenSassUCode` option (pass 10).

## Pre-Mercury Pipeline Stages

Three pipeline stages that run before the Mercury-specific block handle Mercury-related fixups in a target-aware manner:

**PostFixForMercTargets** (`0x24443C0`) -- Applies post-scheduling fixups that are specific to Mercury targets. These may include instruction rewriting for Mercury-specific encodings, alignment adjustments for Mercury instruction groups, or insertion of Mercury-specific NOPs.

**AdvancedScoreboardsAndOpexes** (`0x24443D0`) -- The unified dispatch for scoreboard assignment and opex generation. On Mercury targets, this stage configures the scoreboard system for Mercury's expanded instruction set before handing off to the Mercury-specific `MercGenerateOpex` stage. The `AdvancedSB*` option family (20+ options) controls fine-grained scoreboard behavior.

**ProcessO0WaitsAndSBs** (`0x24443D8`) -- Processes wait instructions and scoreboard reservations for `-O0` (no optimization) builds. Even at `-O0`, the hardware requires valid scoreboard usage. This stage inserts conservative waits and scoreboard entries that ensure correctness without optimization.

## MercExpand Engine

The MercExpand engine is the central transformation in the Mercury pipeline. It is invoked from `sub_5FF110` and operates as a per-function instruction expansion pass.

The engine processes each Mercury instruction and produces one or more SASS instructions:

- **Simple 1:1 mappings**: Most arithmetic and control-flow instructions expand trivially to their SASS equivalents.
- **1:N expansions**: Memory operations, texture instructions, and tensor core operations may expand to multiple SASS instructions (address calculation, prefetch, barrier, load/store, writeback notification).
- **Macro elimination**: Mercury macros (compound operations that have no single SASS equivalent) are split into instruction sequences.

The engine's output is a fully expanded but not yet scheduled instruction stream. WAR hazards from the expansion are handled by the subsequent `MercGenerateWARs1` stage.

## MercConverter Subsystem

MercConverter (referenced at `0x19798F8`, prints `"After MercConverter"` at `0x241F913`) is a scheduling-phase subsystem that converts instruction representations between different internal formats. It operates within named phases (`NamedPhases` at `0x23F26E0`) and includes:

- **Shuffle phase** (`shuffle`): Reorders instructions for better scheduling.
- **Swap phases** (`swap1` through `swap6`): Six iterative swap passes that exchange adjacent instructions when doing so improves scheduling metrics.
- **OriPerformLiveDead**: Liveness analysis recomputation after swaps.
- **OriCopyProp**: Copy propagation cleanup after instruction reordering.

When `MercuryConverterStats` (pass 18) is enabled, the converter prints statistics after each phase.

## Global Mercury Options

Two top-level options control whether Mercury semantics and resources are active:

| Option (ROT13) | Decoded | Bit offset | Purpose |
|---|---|---|---|
| `HfrZrepFrznagvpf` | **UseMercSemantics** | (registered at `0x424BE0`) | Enables Mercury instruction semantics throughout the compiler. When set, the instruction selector, scheduler, and register allocator use Mercury-aware behavior. |
| `HfrZrepErfbheprf` | **UseMercResources** | (registered at `0x424B90`) | Enables Mercury resource modeling (functional unit counts, scoreboard counts, pipeline depths). Controls whether the scheduler uses the Mercury hardware resource model or the legacy SASS model. |

These two options are the master switches. The 22 per-pass options provide fine-grained control within the Mercury pipeline, but they are only meaningful when `UseMercSemantics` is active.

## AdvancedSB Options Related to Mercury

The `AdvancedSB*` (Advanced ScoreBoard) option family includes 20+ ROT13-encoded options for scoreboard management. Several directly interact with the Mercury pipeline:

| Decoded name | Bit offset | Relevance |
|---|---|---|
| `AdvancedSBCrossBlockMercuryAssume` | `0x5b0` | Enables Mercury assumes in cross-block scoreboard analysis |
| `AdvancedSBDepbarBackedge` | `0x5d0` | Controls dependency barrier insertion at backedges (feeds `MercuryInsertBackedgeDepbar`) |
| `AdvancedSBReqBackedge` | `0x660` | Requires scoreboard entry at backedges |
| `AdvancedSBReqBeforeUsingLiveInPsb` | `0x670` | Requires entry before using live-in values in PSB (feeds `MercuryDepStagePreferNonLiveinPSB`) |
| `AdvancedSBDepStageReuse` | `0x610` | Controls dependency-stage reuse policy |
| `AdvancedSBDepStageReuseStallThreshold` | `0x620` | Stall threshold for dependency-stage reuse |
| `AdvancedSBCrossBlock` | `0x59c` | Master switch for cross-block scoreboard analysis |
| `AdvancedSBCrossBlockBudget` | `0x5a0` | Limits computational budget for cross-block analysis |
| `AdvancedSBCrossBlockOnCallee` | `0x5b8` | Extends cross-block analysis across call boundaries |
| `AdvancedSBDiffXBlockRdSb` | `0x630` | Differentiates cross-block read scoreboard entries |
| `AdvancedSBFirstLLSBPsb` | `0x640` | Controls first long-latency scoreboard in PSB allocation |
| `AdvancedSBPruningBudget` | `0x650` | Limits pruning budget for scoreboard optimization |

## Diagnostic Messages

| Message | Address | Function | Context |
|---|---|---|---|
| `"After MercWARs"` | `0x1D41C60` | `sub_4A47F0` | Printed after WAR generation stages (1 and 2) |
| `"After MercOpex"` | `0x1D41C6F` | `sub_4ABB70` | Printed after opex generation |
| `"After MercExpand"` | `0x1DFE320` | `sub_5FF110` | Printed after instruction expansion |
| `"After MercConverter"` | `0x241F913` | (at `0x19798F8`) | Printed after MercConverter scheduling phases |
| `"After WAR post-expansion"` | `0x1D4157B` | `sub_49D8A0` | Non-Mercury WAR pipeline (for comparison) |
| `"After Opex"` | `0x1D41594` | `sub_49D8A0` | Non-Mercury opex pipeline (for comparison) |
| `"After WAR post-opexing"` | `0x1D4159F` | `sub_49D8A0` | Non-Mercury post-opex WAR pipeline (for comparison) |

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x4A47F0` | `MercWARs_entry` | -- | Mercury WAR generation dispatch; prints `"After MercWARs"` |
| `0x4ABB70` | `MercOpex_entry` | -- | Mercury opex generation dispatch; prints `"After MercOpex"` |
| `0x4AC380` | `capmerc_main` | -- | Capsule Mercury top-level option parser and dispatch |
| `0x49D8A0` | `non_merc_WAR_opex` | -- | Non-Mercury WAR/opex pipeline (comparison reference) |
| `0x5FF110` | `MercExpand_entry` | -- | MercExpand instruction expansion entry; prints `"After MercExpand"` |
| `0x197A120` | `MercConverter_phases` | -- | MercConverter named-phase scheduling subsystem |

## Cross-References

- [Mercury Overview](overview.md) -- what Mercury is and why it exists
- [Mercury ELF Sections](elf-sections.md) -- `.nv.merc.*` section layout consumed by these passes
- [Capsule Mercury Format](capmerc-format.md) -- the output format produced by `MercGenerateSassUCode`
- [FNLZR](fnlzr.md) -- the post-link finalizer that runs these passes
- [R_MERCURY Relocations](r-mercury-relocations.md) -- relocation types resolved during Mercury expansion
- [Scheduling](../ptxas/scheduling.md) -- the general scheduling framework that Mercury extends
