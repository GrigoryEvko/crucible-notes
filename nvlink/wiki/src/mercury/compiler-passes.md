# Mercury Compiler Passes

The Mercury pipeline inside nvlink's embedded ptxas backend uses 22 ROT13-obfuscated boolean option passes and 6 plaintext pipeline stages specific to Mercury targets (sm100+). The obfuscated passes are registered as LLVM-style `cl::opt<bool>` command-line options during static construction (`ctor_007`), each controlling a specific codegen behavior for Mercury instruction generation, scheduling, and finalization. The pipeline stages are entries in the master scheduling-phase table that runs after register allocation, performing the Mercury-specific encode-decode, expand, WAR insertion, opex generation, and UCode emission sequence.

This page catalogs every Mercury-specific pass, decodes its ROT13 name, identifies its registration address and option-bit offset, and describes its purpose. Pass catalog data (names, addresses, bit offsets) is verified from the nvlink v13.0.88 binary's string table (`nvlink_strings.json` xrefs) and decompiled static initializers. Pipeline stage descriptions are verified from decompiled function bodies and diagnostic strings. Individual pass behaviors are derived from the decoded option names, corroborated by cross-references to related infrastructure (function xrefs, diagnostic strings, metric names, ELF attributes) found across the binary.

## Key Facts

| Property | Value |
|---|---|
| ROT13-encoded pass count | 22 (registered in `ctor_007` at `0x425A40`--`0x426080`; each string has exactly 1 xref from `ctor_007` per `nvlink_strings.json`) |
| Plaintext pipeline stages | 6 (`MercEncodeAndDecode` through `MercGenerateSassUCode`) |
| Registration constructor | `ctor_007` (static initializer spanning `0x412790`--`0x426260`, not separately decompiled; verified via string xrefs) |
| Registration record size | 80 bytes (0x50) per pass; uniform spacing verified across all 21 Mercury-block passes |
| Pipeline stage table base | `0x24443F0` (within master phase dispatch table at `0x2443F80`) |
| MercExpand engine entry | `sub_5FF110` (decompiled: calls `sub_5FDDB0` dispatch, then prints `"After MercExpand"` at `0x1DFE320`) |
| MercExpand dispatch | `sub_5FDDB0` (25.5KB; switch on IR opcode type; handles cases 0,5,8,9,11,12,17,-1,120) |
| MercConverter entry | `sub_1977B70` (35KB; xref at `0x19798F8`; prints `"After MercConverter"` at `0x241F913`; refs "shuffle", "NamedPhases") |
| MercConverter phases | `sub_197A120` (49KB; manages "shuffle", "swap1"-"swap6", "dce1"-"dce3", "cpy1"-"cpy3", "OriPerformLiveDead", "OriCopyProp") |
| MercWARs entry | `sub_4A47F0` (decompiled: delegates to `sub_4A41D0`, then prints `"After MercWARs"` at `0x1D41C60`) |
| MercWARs processor | `sub_4A4DC0` (24KB / 784 lines; Mercury WAR dependency processing) |
| MercOpex entry | `sub_4ABB70` (decompiled: 94 lines; calls `sub_A48AA0` for target check, `sub_4A8690` for opex expansion, prints `"After MercOpex"` at `0x1D41C6F`) |
| MercOpex expander | `sub_4A8690` (66KB / 2602 lines; largest Mercury function; operand expansion to final encoding) |
| Related global options | `UseMercSemantics` (`0x23F34F0`), `UseMercResources` (`0x23F3510`) |
| Mercury mode flag | `byte_2A5F222` (set to 1 when sm > 99; checked throughout pipeline) |
| Diagnostics option | `DumpMercOpCounts` (string at `0x1D4AB40` in `ctor_004`; ROT13 `QhzcZrepBcPbhagf`) |
| FNLZR invocation | `sub_4748F0` Phase 6 calls `sub_471700` (2541 lines) for per-function compilation dispatch |

## ROT13 Obfuscation

Every Mercury-specific pass name in the binary is stored as a ROT13 string. The pattern `Zrephel` decodes to `Mercury`. This obfuscation is consistent across all ptxas/nvlink internal option names — standard Ori/Advanced passes use the same scheme. The encoding serves as a minimal deterrent against casual string searching; it does not provide any security.

The ROT13 decoder is `sub_1A40AC0` (15,629 bytes, 449 decompiled lines), a SIMD-accelerated function that uses `_mm_load_si128` to process 16 bytes at a time. It runs in three phases: scalar head, SIMD bulk, scalar tail. It allocates a fresh power-of-2 buffer and decodes in-place. This function is called during opcode table initialization for all architectures.

Each ROT13 option is paired with a hex offset string (also ROT13-encoded) that identifies the option's byte position within a global knob/flags structure. For example, `0k3r40` decodes to `0x3e40`. Across the full binary, 1,287 knob/option names use this same ROT13 + hex-offset registration pattern.

## The 22 ROT13-Encoded Passes

Listed in registration order (highest `ctor_007` address first, which corresponds to reverse construction order). The "Bit offset" column shows the decoded option byte-offset within the global knob structure. Every entry in this table is verified: the ROT13 string exists at the given address in `nvlink_strings.json`, with exactly one xref pointing to the corresponding `ctor_007` registration address. The ROT13-encoded hex offset string for each pass also exists at a confirmed address with exactly one xref to the same `ctor_007` registration block.

### Pass Catalog

| # | ROT13 string | Decoded name | Bit offset | String addr | Offset string addr | Registration addr | Category |
|---|---|---|---|---|---|---|---|
| 1 | `ZrephelHfrNpgvirGuernqPbyyrpgvirVafgf` | **MercuryUseActiveThreadCollectiveInsts** | `0x3E40` | `0x23F2B00` | `0x23F2AF5` | `0x426080` | ISA/target |
| 2 | `ZrephelGenpxZhygvErnqfJneYngrapl` | **MercuryTrackMultiReadsWarLatency** | `0x3E30` | `0x23F2B40` | `0x23F2B26` | `0x426030` | scheduling |
| 3 | `ZrephelGrcvqNjnerFo` | **MercuryTepidAwareSb** | `0x3E20` | `0x23F2B70` | `0x23F2B61` | `0x425FE0` | scheduling |
| 4 | `ZrephelCerfhzrKoybpxJnvgOrarsvpvny` | **MercuryPresumeXblockWaitBeneficial** | `0x3E18` | `0x23F2BA0` | `0x23F2B84` | `0x425F90` | scoreboard |
| 5 | `ZrephelZretrCebybthrOybpxf` | **MercuryMergePrologueBlocks** | `0x3E10` | `0x23F2BD0` | `0x23F2BC3` | `0x425F40` | codegen |
| 6 | `ZrephelVffhrQrynlJOFgnyyFrysYbbc` | **MercuryIssueDelayWBStallSelfLoop** | `0x3E00` | `0x23F2C00` | `0x23F2BEB` | `0x425EF0` | scoreboard |
| 7 | `ZrephelVafregKoybpxJnvg` | **MercuryInsertXblockWait** | `0x3DF0` | `0x23F2C30` | `0x23F2C21` | `0x425EA0` | scoreboard |
| 8 | `ZrephelVafregOnpxrqtrQrcone` | **MercuryInsertBackedgeDepbar** | `0x3DE0` | `0x23F2C60` | `0x23F2C48` | `0x425E50` | scoreboard |
| 9 | `ZrephelVafregNffhzrf` | **MercuryInsertAssumes** | `0x3DD0` | `0x23F2C90` | `0x23F2C7C` | `0x425E00` | assume |
| 10 | `ZrephelTraFnffHPbqr` | **MercuryGenSassUCode** | `0x3DC0` | `0x23F2CB0` | `0x23F2CA5` | `0x425DB0` | codegen |
| 11 | `ZrephelSbeprHaxabjaGptra05Ngge` | **MercuryForceUnknownTcgen05Attr** | `0x3DB9` | `0x23F2CD0` | `0x23F2CC4` | `0x425D60` | ISA/target |
| 12 | `ZrephelSbeprVFNPynff` | **MercuryForceISAClass** | `0x3DB8` | `0x23F2D00` | `0x23F2CEF` | `0x425D10` | ISA/target |
| 13 | `ZrephelRapbqrArjJbexreSvyrf` | **MercuryEncodeNewWorkerFiles** | `0x3DB0` | `0x23F2D20` | `0x23F2D15` | `0x425CC0` | encoding |
| 14 | `ZrephelRapbqrQrpbqr` | **MercuryEncodeDecode** | `0x3DA0` | `0x23F2D50` | `0x23F2D3C` | `0x425C70` | encoding |
| 15 | `ZrephelQhzcVafgfNfOvanel` | **MercuryDumpInstsAsBinary** | `0x3D90` | `0x23F2D70` | `0x23F2D64` | `0x425C20` | diagnostics |
| 16 | `ZrephelQvfnoyrYrtnyvmngvbaBsGrkGbHEObhaq` | **MercuryDisableLegalizationOfTexToURBound** | `0x3D80` | `0x23F2DA0` | `0x23F2D89` | `0x425BD0` | legalization |
| 17 | `ZrephelQrcFgntrCersreAbaYvirvaCFO` | **MercuryDepStagePreferNonLiveinPSB** | `0x3D78` | `0x23F2DE0` | `0x23F2DC9` | `0x425B80` | scoreboard |
| 18 | `ZrephelPbairegreFgngf` | **MercuryConverterStats** | `0x3D70` | `0x23F2E10` | `0x23F2E02` | `0x425B30` | diagnostics |
| 19 | `ZrephelPbafhzrNffhzrf` | **MercuryConsumeAssumes** | `0x3D60` | `0x23F2E30` | `0x23F2E26` | `0x425AE0` | assume |
| 20 | `ZrephelPbzcnpgrqNffhzrf` | **MercuryCompactedAssumes** | `0x3D50` | `0x23F2E50` | `0x23F2E46` | `0x425A90` | assume |
| 21 | `ZrephelNffhzrCGKCbegnovyvgl` | **MercuryAssumePTXPortability** | `0x3D40` | `0x23F2E80` | `0x23F2E68` | `0x425A40` | assume |
| 22 | `NqinaprqFOPebffOybpxZrephelNffhzr` | **AdvancedSBCrossBlockMercuryAssume** | `0x5B0` | `0x23FC820` | `0x23FC842` | `0x4129E0` | scoreboard |

Pass 22 (`AdvancedSBCrossBlockMercuryAssume`) is registered separately from the main Mercury block, within the `AdvancedSB*` option group at `0x4129E0`. It bridges the Mercury assume system with the cross-block scoreboard analysis.

### Registration Record Structure

[V] The `ctor_007` registration block is not separately decompiled (the address range `0x412790`--`0x426260` falls in the gap between `ctor_006` at `0x412790` and `ctor_008` at `0x426260`). However, the internal structure is derived from verified measurements:

- **Record size**: 80 bytes (0x50) per pass. All 21 Mercury-block registrations (passes 1–21) have exactly 0x50-byte spacing between consecutive registration addresses, confirmed by computing `0x426080 - 0x426030 = 0x50`, `0x426030 - 0x425FE0 = 0x50`, etc., uniformly across all 21 entries.
- **String references per record**: Exactly 2 xrefs per record — one for the ROT13 name string and one for the ROT13 hex offset string. Both are type=1 data references (confirmed from `nvlink_strings.json`).
- **Name string xref offset within record**: The name string xref address is the first byte of the record (e.g., pass 21 at `0x425A40`, pass 20 at `0x425A90`).
- **Hex offset string xref offset within record**: The hex offset string xref is 0x33 bytes into the record (e.g., pass 21: name at `0x425A40`, hex at `0x425A73`; difference = 0x33).
- **Remaining 46 bytes**: Contain the type code (boolean), default value, and other metadata fields. The exact layout is inferred by analogy with the decompiled `ctor_004` (798 lines), which uses a different but related record format for non-Mercury options: name pointer (8 bytes), string length (8 bytes), type code (1 byte), two placeholder entries ("-", 1 each = 10 bytes), and an enabled-by-default flag (1 byte).

### Default Values and Enable State

[N] The 22 Mercury passes are boolean flags (`cl::opt<bool>` style). The `ctor_007` records contain embedded default values in the 80-byte record, but since the function is not decompiled, the per-pass defaults cannot be read directly from the constructor body. The default state is determined by the architecture profile initialization that runs when `byte_2A5F222` (Mercury mode) is set.

The following defaults are inferred from naming conventions and functional context:

| Default | Passes | Rationale |
|---|---|---|
| Enabled by default | `MercuryEncodeDecode`, `MercuryGenSassUCode`, `MercuryEncodeNewWorkerFiles`, `MercuryInsertAssumes`, `MercuryConsumeAssumes`, `MercuryInsertBackedgeDepbar`, `MercuryInsertXblockWait`, `MercuryMergePrologueBlocks`, `MercuryTepidAwareSb`, `MercuryTrackMultiReadsWarLatency`, `MercuryCompactedAssumes`, `MercuryAssumePTXPortability`, `MercuryIssueDelayWBStallSelfLoop`, `MercuryPresumeXblockWaitBeneficial`, `MercuryDepStagePreferNonLiveinPSB`, `MercuryUseActiveThreadCollectiveInsts`, `AdvancedSBCrossBlockMercuryAssume` | Core pipeline stages and optimization passes — disabling any of these would break the Mercury finalization pipeline or produce suboptimal code |
| Disabled by default | `MercuryDumpInstsAsBinary`, `MercuryConverterStats` | Diagnostic/debug output — these produce dump output and are not needed for normal compilation |
| Disabled by default | `MercuryForceISAClass`, `MercuryForceUnknownTcgen05Attr` | Debug overrides — "Force" prefix indicates they override automatic behavior for debugging purposes |
| Disabled by default | `MercuryDisableLegalizationOfTexToURBound` | Opt-out flag — "Disable" prefix means the legalization is active by default, and this suppresses it |

The confidence on these defaults is MEDIUM: the naming conventions are extremely consistent across the 1,287-knob system ("Force", "Disable", "Dump" prefixes uniformly indicate off-by-default flags), but without the decompiled ctor_007 body, the claim is not directly verifiable.

## Pass Descriptions by Category

Each pass description includes an evidence summary. **[V]** = verified from binary data (decompiled code, string xrefs, function analysis). **[N]** = derived from decoded option name, corroborated by related infrastructure cross-references. The distinction matters: [V] claims are reproducible from the binary; [N] claims are high-confidence inferences from extremely descriptive names and supporting context.

### Category 1: Instruction Encoding and Expansion

These passes control the Mercury instruction format pipeline — the conversion between compact Mercury intermediate encoding and full SASS binary format.

**MercuryEncodeDecode** (pass 14, offset `0x3DA0`) — [V] Gates the `MercEncodeAndDecode` pipeline stage at `0x24443F0`. The stage name is a verified plaintext entry in the master phase table (string at `0x2443CA2`, xref to `0x24443F0`). [N] Controls the Mercury instruction encode/decode round-trip: Mercury instructions use a compact intermediate encoding that differs from the final SASS binary format. This pass enables the round-trip to normalize the instruction representation for subsequent Mercury-specific passes.

**MercuryEncodeNewWorkerFiles** (pass 13, offset `0x3DB0`) — [N] Gates re-encoding of instruction data into Mercury's worker-file format. The name contains "WorkerFiles" which corresponds to the capsule Mercury (capmerc) output format where Mercury IR is packaged alongside SASS. [V] The capmerc CLI parser at `sub_4AC380` (decompiled, 429 lines) defines `--cap-merc` ("Generate Capsule Mercury") and `--binary-kind` with valid values `"mercury,capmerc,sass"`. The "NewWorkerFiles" portion indicates this pass produces fresh worker-file payloads reflecting the current instruction state after optimization.

**MercuryDumpInstsAsBinary** (pass 15, offset `0x3D90`) — [N] Debug/diagnostic pass. The name "DumpInstsAsBinary" is unambiguous: when enabled, dumps the Mercury instruction stream in raw binary form for offline analysis. [V] Complementary to `DumpMercOpCounts` (string `QhzcZrepBcPbhagf` at `0x1D4AB40` in `ctor_004`, default disabled) which dumps per-opcode instruction counts.

**MercuryGenSassUCode** (pass 10, offset `0x3DC0`) — [V] Gates the `MercGenerateSassUCode` pipeline stage at `0x2444418` (string at `0x2443D02`, xref to `0x2444418`). The subsequent entries in the master phase table are `ComputeVCallRegUse` (`0x2444420`), `CalcRegisterMap` (`0x2444428`), and the dump stages `DumpNVuCodeText` (`0x2444458`) / `DumpNVuCodeHex` (`0x2444460`). [N] Controls the final SASS microcode generation from Mercury representation. This is the terminal codegen step: Mercury-format instructions are translated into their final SASS binary encoding. The output is the `.text` section content formatted as UCode (NVIDIA's term for the final hardware-executable instruction encoding).

### Category 2: Assume Framework

The five "assume" passes form a coherent subsystem that manages metadata annotations through the compilation pipeline. [V] The string `"After MercConverter"` at `0x241F913` is referenced by the ORI pass manager at `sub_1977B70`, which also references `"NamedPhases"` and `"shuffle"` — confirming that the MercConverter scheduling subsystem uses named phases. [V] The `AdvancedSBCrossBlockMercuryAssume` option (pass 22) is registered separately from the main Mercury block, at `0x4129E0` within the `AdvancedSB*` option group, confirming the assume system bridges into the scoreboard framework. [N] The insert/consume/compact lifecycle pattern implied by the three pass names describes a standard assumption annotation protocol: insert assumptions before transformations, consume them after use, compact them for reduced overhead.

**MercuryInsertAssumes** (pass 9, offset `0x3DD0`) — [N] Inserts assumption annotations into the Mercury instruction stream. Assumes are metadata that record properties (e.g., "this register is not modified between points A and B" or "this dependency barrier is still valid") allowing later passes to trust invariants without re-analyzing.

**MercuryConsumeAssumes** (pass 19, offset `0x3D60`) — [N] The complement of InsertAssumes. After a transformation pass uses the assumptions, this pass removes them from the stream, preventing stale assumptions from persisting into subsequent passes. The insert/consume pairing is standard compiler infrastructure for assumption-guided optimization.

**MercuryCompactedAssumes** (pass 20, offset `0x3D50`) — [N] Enables a compacted representation of assumption annotations. Instead of full-width assume records, this packs multiple assumptions into fewer bits, reducing metadata overhead in the instruction stream. The offset `0x3D50` is adjacent to the other assume passes (`0x3D40`--`0x3DD0`), confirming they form a group.

**MercuryAssumePTXPortability** (pass 21, offset `0x3D40`) — [N] Controls whether the assume framework treats PTX-level semantics as portable across Mercury transformations. When enabled, the compiler assumes that PTX-level operational semantics are preserved through the Mercury encode-decode pipeline, allowing more aggressive optimization. This is the lowest bit offset in the Mercury group, suggesting it was the first Mercury-specific option added.

**AdvancedSBCrossBlockMercuryAssume** (pass 22, offset `0x5B0`) — [V] Registered at `0x4129E0`, separately from the main Mercury block at `0x425A40`--`0x426080`, within the `AdvancedSB*` option group. The `0x5B0` offset places it among the scoreboard options (`AdvancedSBCrossBlock` at `0x59C`, `AdvancedSBCrossBlockBudget` at `0x5A0`, `AdvancedSBCrossBlockOnCallee` at `0x5B8`). [V] Neighboring string: `NqinaprqFOPebffOybpxOhqtrg` (AdvancedSBCrossBlockBudget) at `0x23FC850` with offset string `0k5o8` (`0x5B8`) at `0x23FC80D`, xref from `0x412990` and `0x412A13` respectively — confirming the `AdvancedSB*` registration block surrounds pass 22. [N] Extends cross-block scoreboard analysis to incorporate Mercury assume information, allowing the scoreboard system to use assume annotations as additional constraints to reduce unnecessary stalls at block transitions.

### Category 3: Scoreboard and Scheduling Control

**MercuryTepidAwareSb** (pass 3, offset `0x3E20`) — [V] The Tepid scheduler is a verified subsystem spanning `0x16F6000`--`0x1740000` (~296KB, ~50 functions). The string `"TepidMacUtil"` appears at `0x23EF746` (referenced by `sub_16F6A80`, `sub_17027F0`, `sub_1768520`). The string `"TepidTime"` at `0x23F0851` is referenced by `sub_17027F0`. Metric strings `"MathToDmaTepidRatio"` (`0x23FCD5C`) and `"MathToEpilogueTepidRatio"` (`0x23FCD8B`) are both referenced by `sub_1768520`. The scheduling header format `"# [inst=%d] [texInst=%d] [tepid=%d] [rregs=%d]"` at `0x2425CB8` confirms tepid is a per-function scheduling metric. [N] Makes the scoreboard allocator (the "Sb" suffix) aware of tepid instruction status, allowing scoreboard entries to be freed for instructions with real latency pressure.

**MercuryTrackMultiReadsWarLatency** (pass 2, offset `0x3E30`) — [V] The MercWARs pipeline stages (`MercGenerateWARs1` at `0x2444400`, `MercGenerateWARs2` at `0x2444410`) run pre- and post-opex. The WAR processor `sub_4A4DC0` (24KB / 784 lines) handles WAR dependency computation. The diagnostic string `"After MercWARs"` is printed by `sub_4A47F0` (decompiled: 11 lines, delegates to `sub_4A41D0`). [N] Enables precise latency tracking for WAR hazards involving multiple read consumers: when a single write has multiple outstanding reads, the WAR latency must account for the slowest reader. The "MultiReads" in the name specifies this is the multi-consumer tracking precision control.

**MercuryPresumeXblockWaitBeneficial** (pass 4, offset `0x3E18`) — [N] Controls a heuristic in the cross-block wait insertion algorithm. When enabled, the scheduler presumes that inserting an `XBLOCK.WAIT` instruction at certain points will be beneficial for performance, even when the cost-benefit analysis is inconclusive. The "Presume" prefix distinguishes this from the mechanical insertion gate (pass 7). [V] The paired option `MercuryInsertXblockWait` at adjacent offset `0x3DF0` confirms a two-level control scheme: heuristic presumption (this pass) plus mechanical gate (pass 7).

**MercuryInsertXblockWait** (pass 7, offset `0x3DF0`) — [N] Gates the actual insertion of `XBLOCK.WAIT` instructions into the instruction stream. While pass 4 (`PresumeXblockWaitBeneficial`) controls the heuristic, this pass is the mechanical gate that enables or disables the insertion transformation. [V] The `XblockWait` string family extends beyond Mercury: `ctor_004` registers `XBlockWaitInOffTarget` (ROT13 `KOybpxJnvgVaBssGnetrg` at `0x2A64120`), `XBlockWaitInOnTarget` (`0x2A64160`), and `XBlockWaitOut` (`0x2A641A0`), confirming XBLOCK.WAIT is a cross-block synchronization mechanism with off-target, on-target, and outbound variants.

**MercuryInsertBackedgeDepbar** (pass 8, offset `0x3DE0`) — [V] `AdvancedSBDepbarBackedge` at offset `0x5D0` and `AdvancedSBReqBackedge` at offset `0x660` are registered in the same `ctor_007` knob block. The MercExpand engine at `sub_5FDDB0` uses backedge detection infrastructure documented in the CFG analysis subsystem: `CFG_DumpRPOAndBackedges` at `sub_5E5E60` prints `"Showing backedge info:"` (string at `0x5E6B51`), with backedge maps stored at context offset +680. [N] Controls insertion of dependency barriers on loop backedges. In a loop, a backedge creates a cycle where the head depends on results from the tail. Without a dependency barrier at the backedge, the hardware scoreboard cannot correctly track cross-iteration dependencies. This pass inserts `DEPBAR` instructions at identified backedge points.

**MercuryDepStagePreferNonLiveinPSB** (pass 17, offset `0x3D78`) — [V] Related scoreboard options confirmed in `ctor_007`: `AdvancedSBReqBeforeUsingLiveInPsb` at offset `0x670` (string `0x23FC5E0`), `AdvancedSBFirstLLSBPsb` at offset `0x640` (string `0x23FC650`). The "PSB" (Pending ScoreBoard) and "DepStage" (dependency stage) terminology is consistent across 28+ `AdvancedSB*` options. [N] Controls the dependency-stage allocation policy within the PSB. When enabled, the allocator prefers to assign non-live-in entries (values defined within the current block) before consuming live-in entries (values flowing in from predecessors), reducing unnecessary cross-block stalls.

**MercuryIssueDelayWBStallSelfLoop** (pass 6, offset `0x3E00`) — [N] Controls the handling of write-back stall conditions in self-loops (single-block loops where the backedge targets the same block). When a write-back produces a stall in such a loop, the instruction must wait for the result before re-executing. This pass controls issue-delay annotations to prevent the hardware from issuing the dependent instruction too early. The name encodes three specific concepts: "IssueDelay" (scheduling stall annotation), "WBStall" (write-back stall condition), and "SelfLoop" (single-block backedge loop).

### Category 4: ISA and Target Control

**MercuryForceISAClass** (pass 12, offset `0x3DB8`) — [V] ISA class strings are verified throughout the binary: `"(profile_sm_100)->isaClass"` at `0x1D40B93`, `"(profile_sm_120)->isaClass"` at `0x1D40DAC`, etc. The architecture profile structure stores the ISA class name at offset 24 (8 bytes, char pointer), passed as parameter `a5` to the profile constructor `sub_484DB0`. [N] Forces a specific ISA class assignment for Mercury instructions. The ISA class determines which functional unit executes an instruction (math, memory, texture, tensor core). When enabled, this overrides the default ISA class derivation.

**MercuryForceUnknownTcgen05Attr** (pass 11, offset `0x3DB9`) — [V] The binary contains `EIATTR_TCGEN05_1CTA_USED` (string at `0x1D36E41`) and `EIATTR_TCGEN05_2CTA_USED` (string at `0x1D36E5A`). The reserved shared memory symbol `__nv_reservedSMEM_tcgen05_partition` at `0x1D3BD08` is referenced in section processing. Ten `tcgen05` operations are cataloged: `ld.red`, `alloc`, `dealloc`, `commit`, `shift`, `cp`, `fence`, `wait`, `ld`, `st`, `mma`, `mma.ws`. The tcgen05 guardrail infrastructure includes verification functions: `phase_valid`, `current_warp_valid_owner`, `columns_allocated`, `in_physical_bounds`, `allocation_granularity`, `datapath_alignment`, `sp_consistency`, `check_sparse_usage`. A tcgen05 version incompatibility error exists: "objects using tcgen05 in 12.x cannot be linked with 13.0+". [N] Forces the `tcgen05` attribute to "unknown" for specific instructions, causing conservative dependency handling. The adjacent offset to `MercuryForceISAClass` (`0x3DB8` vs `0x3DB9` — only 1 byte apart) suggests these two "Force" options were added together as a pair for debugging new instruction scheduling attributes. The bit-level granularity (`0x3DB9` is a single-bit flag, not byte-aligned like most others) confirms this is a fine-grained override.

**MercuryUseActiveThreadCollectiveInsts** (pass 1, offset `0x3E40`) — [V] This is the highest bit offset in the Mercury group (`0x3E40`), making it the last-registered Mercury option. The Mercury builtin inventory confirms 644 instruction templates organized into 35 operation families, including `redux` (32 variants: b32, f32, s32, sync_unaligned), `barrier` (86 variants), and `warpgroup` (40 variants). The redux family uses active-thread semantics. [N] Enables the use of active-thread collective instructions in Mercury codegen. Active-thread collectives are warp-level operations (`vote`, `match`, `redux`) that operate only on currently active threads without requiring explicit convergence.

### Category 5: Legalization and Codegen

**MercuryDisableLegalizationOfTexToURBound** (pass 16, offset `0x3D80`) — [V] The MercExpand dispatch at `sub_5FDDB0` includes a texture/sampler handling path at case 11: `sub_5FAC90` (shared memory path when vtable+1160 returns true), `sub_5FC1B0` (surface operand path for data type 559-560), and the `MercExpand_HandleTexSampler` function at `sub_5EB560`. The ISel encoder `Encode_SM50_TEX` at `sub_614B90` (format 3, opcode 0x1D, reg class 0x27) confirms texture instructions have dedicated encoding paths. [N] Disables a specific legalization transform that converts texture instructions to uniform-register-bound (UR-bound) forms. The "Disable" prefix makes this an opt-out flag: the legalization is active by default, and this option suppresses it when the UR-bound form causes correctness issues or when the texture unit's interaction with Mercury scheduling is not fully characterized.

**MercuryMergePrologueBlocks** (pass 5, offset `0x3E10`) — [N] Enables merging of prologue basic blocks. The function prologue in Mercury code can be split across multiple basic blocks (for parameter setup, shared memory initialization, barrier setup). This pass merges them into a single prologue block, improving scheduling by giving the scheduler a larger instruction window at function entry. [V] The MercExpand engine at `sub_5FDDB0` manages basic block splitting (creates new BB nodes via `sub_A497D0` and `sub_A49150`), confirming the pipeline operates at basic-block granularity and that block merging/splitting is a meaningful transformation.

### Category 6: Diagnostics

**MercuryConverterStats** (pass 18, offset `0x3D70`) — [V] The MercConverter subsystem is implemented in `sub_1919030` (92KB / 2685 lines), which references strings `"CONVERTING"`, `"Internal compiler error."`, `"swap3"`, `"swap5"`, `"OriCopyProp"`. The ORI named-phase manager at `sub_197A120` (49KB / 1850 lines) manages phases `"shuffle"`, `"swap1"` through `"swap6"`, `"dce1"` through `"dce3"`, `"cpy1"` through `"cpy3"`, `"OriPerformLiveDead"`, and `"OriCopyProp"`. The pass manager merge function at `sub_1977B70` (35KB / 1341 lines) references `"shuffle"`, `"NamedPhases"`, and `"After MercConverter"`. [N] When enabled, this pass prints conversion statistics: instruction counts, conversion success rates, and swap-phase metrics.

## Pipeline Stage Sequence

The 6 Mercury-specific pipeline stages run in the post-register-allocation, post-scheduling region of the master phase table. They execute in strict order after the generic SASS finalization stages.

### Context in the Master Phase Table

The complete late-pipeline sequence from `PostSchedule` through the Mercury stages:

```text
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

**Stage 1: MercEncodeAndDecode** (`0x24443F0`) — [V] Plaintext stage name in master phase table (string at `0x2443CA2`). Gated by the `MercuryEncodeDecode` option (pass 14). [N] Encodes the current instruction representation into Mercury's compact binary format and decodes it back into an expanded internal form. This round-trip serves two purposes: (a) it validates that the Mercury encoding is lossless, and (b) it normalizes the instruction representation to the form expected by subsequent Mercury-specific passes.

**Stage 2: MercExpandInstructions** (`0x24443F8`) — [V] Entry function `sub_5FF110` (decompiled: 20 lines) calls `sub_5FDDB0` (25.5KB dispatch loop), then prints `"After MercExpand"` via `nullsub_181`. The dispatch at `sub_5FDDB0` switches on IR opcode type (offset +28) with verified cases: 0 (vtable+48 generic), 5/8/9 (register width clamping, max=15), 11 (three sub-paths: `sub_5F80E0` via vtable+584, `sub_5FAC90` shared memory via vtable+1160, `sub_5FC1B0` surface for type 559-560), 12 (vtable+136), 17 (conditional on debug flag +1536), -1 (terminator), 120 (skip). Before dispatch, checks attribute 200 == 1107 for MOV special case (`sub_5FC6B0`). The expansion engine spans `0x5E4470`--`0x600260` (~112KB) with ~40 identified sub-functions. [N] This is the most substantial transformation in the Mercury pipeline — where the abstract Mercury encoding becomes concrete SASS. A single Mercury instruction may map to multiple SASS instructions.

**Stage 3: MercGenerateWARs1** (`0x2444400`) — [V] Entry `sub_4A47F0` (decompiled: 11 lines) delegates to `sub_4A41D0`, prints `"After MercWARs"` (string at `0x1D41C60`, xref from `0x4A480A`). The WAR processor `sub_4A4DC0` (24,388 bytes / 784 lines) performs the actual WAR computation. [N] First pass of WAR hazard insertion. After instruction expansion, new WAR dependencies arise between expanded micro-operations. The `MercuryTrackMultiReadsWarLatency` option (pass 2) controls multi-reader tracking precision during this pass.

**Stage 4: MercGenerateOpex** (`0x2444408`) — [V] Entry `sub_4ABB70` (decompiled: 94 lines) calls `sub_A48AA0` for a target capability check, then calls `sub_4A8690` (66,582 bytes / 2602 lines, the largest Mercury function) for opex expansion. After expansion, it checks a vtable function at offset +72 (comparing against `sub_488780`), reads bytes at context offset +1224 and +1296/+1304, and prints `"After MercOpex"` (string at `0x1D41C6F`, xref from `0x4ABC3E`). The `dword_1D41C80` table (4 entries) provides a mode selector for the opex computation. [V] The non-Mercury equivalent at `sub_49D8A0` (18 parameters, very large) prints three diagnostic strings in sequence: `"After WAR post-expansion"` (`0x1D4157B`), `"After Opex"` (`0x1D41594`), `"After WAR post-opexing"` (`0x1D4159F`), confirming the WAR-Opex-WAR three-phase pattern. [N] Opex annotations describe instruction execution properties: pipeline throughput, latency class, and resource requirements.

**Stage 5: MercGenerateWARs2** (`0x2444410`) — [V] Same entry function pair (`sub_4A47F0` -> `sub_4A4DC0`) as stage 3; distinguished by the stage table position (post-opex vs pre-opex). The two-pass approach (WAR1 -> Opex -> WAR2) is confirmed by the non-Mercury pipeline's three-string sequence at `sub_49D8A0`. [N] After opex annotations assign concrete latencies, some WAR stalls from stage 3 may be pessimistic. This pass refines WAR stalls using opex-derived latency information.

**Stage 6: MercGenerateSassUCode** (`0x2444418`) — [V] Plaintext stage name (string at `0x2443D02`, xref to `0x2444418`). The subsequent master phase table entries are `ComputeVCallRegUse` (`0x2444420`), `CalcRegisterMap` (`0x2444428`), `UpdateAfterPostRegAlloc` (`0x2444430`), `ReportFinalMemoryUsage` (`0x2444438`), `AdvancedPhaseOriPhaseEncoding` (`0x2444440`), then the dump stages `DumpNVuCodeText` (`0x2444458`) and `DumpNVuCodeHex` (`0x2444460`). Gated by the `MercuryGenSassUCode` option (pass 10). [N] Terminal stage. Translates the fully expanded, scheduled, WAR-annotated instruction stream into final SASS UCode binary encoding. Each instruction is encoded into its hardware bit pattern.

## Pre-Mercury Pipeline Stages

Three pipeline stages that run before the Mercury-specific block handle Mercury-related fixups in a target-aware manner:

**PostFixForMercTargets** (`0x24443C0`, string at `0x2443C44`) — Applies post-scheduling fixups that are specific to Mercury targets. These may include instruction rewriting for Mercury-specific encodings, alignment adjustments for Mercury instruction groups, or insertion of Mercury-specific NOPs.

**AdvancedScoreboardsAndOpexes** (`0x24443D0`, string at `0x2443C70`) — The unified dispatch for scoreboard assignment and opex generation. On Mercury targets, this stage configures the scoreboard system for Mercury's expanded instruction set before handing off to the Mercury-specific `MercGenerateOpex` stage. The `AdvancedSB*` option family (20+ options) controls fine-grained scoreboard behavior.

**ProcessO0WaitsAndSBs** (`0x24443D8`, string at `0x2443C8D`) — Processes wait instructions and scoreboard reservations for `-O0` (no optimization) builds. Even at `-O0`, the hardware requires valid scoreboard usage. This stage inserts conservative waits and scoreboard entries that ensure correctness without optimization.

## FNLZR Integration

The Mercury passes are invoked through the FNLZR (Finalizer) subsystem, documented in [fnlzr.md](fnlzr.md). The invocation chain:

```text
main() / sub_42AF40() / sub_52DD50()
  -> sub_4275C0     (FNLZR front-end dispatcher, 3,989 bytes)
    -> sub_4748F0   (FNLZR core engine, 48,730 bytes / 1,830 lines)
      -> Phase 6: sub_1CEF440   (pipeline initialization)
      -> Phase 6: sub_471700    (per-function compilation dispatch, 2,541 lines)
        -> master phase table dispatch at 0x2443F80
          -> MercEncodeAndDecode  (stage 1)
          -> MercExpandInstructions (stage 2, via sub_5FF110 -> sub_5FDDB0)
          -> MercGenerateWARs1    (stage 3, via sub_4A47F0 -> sub_4A4DC0)
          -> MercGenerateOpex     (stage 4, via sub_4ABB70 -> sub_4A8690)
          -> MercGenerateWARs2    (stage 5, via sub_4A47F0 -> sub_4A4DC0)
          -> MercGenerateSassUCode (stage 6)
```

[V] The `sub_4748F0` engine calls `sub_471700` at line 1247 of the decompiled source (confirmed from `sub_4748F0_0x4748f0.c`). The call passes the module context `v419[]`, which carries the Mercury profile descriptor (set at `v419[65]` when source arch > 99 in Phase 4) and the compilation unit descriptor (set at `v419[32]` in Phase 6a). The Mercury passes are only executed when `byte_2A5F222` (Mercury mode flag, set when sm > 99) is active.

[V] The `sub_471700` function (2,541 decompiled lines, address `0x471700`) initializes a 656-byte compilation unit descriptor at line 562 (vtable at `off_1D49C58`), copies the 256-byte architecture profile at lines 611–632, and then iterates over the function index sections, dispatching each function through the master phase table. The Mercury pipeline stages are entries in this table and execute for each function body when the Mercury mode flag is active.

## MercExpand Engine

The MercExpand engine is the central transformation in the Mercury pipeline. It spans `0x5E4470`--`0x600260` (~112KB) with ~40 identified sub-functions. It is invoked from `sub_5FF110` and operates as a per-function instruction expansion pass.

### Verified Architecture

[V] The engine is organized into these layers (all function identities from sweep analysis of decompiled code):

| Layer | Address | Size | Role | Verification |
|---|---|---|---|---|
| Entry | `sub_5FF110` | 20 lines | Calls dispatch, prints diagnostic | Decompiled |
| Dispatch | `sub_5FDDB0` | 25.5KB | Main switch on IR opcode type | Decompiled + sweep |
| Per-instruction | `sub_5F38E0` | 35KB | Per-instruction expansion handler (2nd largest) | Sweep (HIGH) |
| Per-BB | `sub_5F53B0` | 10.1KB | Per-basic-block handler | Sweep (HIGH) |
| Resource accounting | `sub_5F8B60` | 16KB | Register resource constraints | Sweep (HIGH) |
| Wide-op split | `sub_5F2BA0` | 22.4KB | Split wide ops into 11-wide or 15-wide | Sweep (HIGH) |
| Complex operand | `sub_5F22E0` | 13.2KB | Complex operand handling | Sweep (HIGH) |
| MOV expand | `sub_5FC6B0` | 8.3KB | MOV special case (attr 200 == 1107) | Sweep (HIGH) |
| RETURN expand | `sub_5FCE20` | 18.8KB | Return/exit instruction expansion | Sweep (HIGH) |
| Complex expand | `sub_5FF180` | 17.8KB | Multi-node complex expansion | Sweep (HIGH) |
| CFG maps | `sub_5EA370` | — | Build hash maps for all BBs | Sweep (HIGH) |
| Register state | `sub_5EA4F0` | — | Invalidate/reset register state | Sweep (HIGH) |

[V] The dispatch loop at `sub_5FDDB0` processes IR nodes linked via offset +8 (next) / +16 (data), switching on the opcode at offset +28. The per-instruction handler `sub_5F38E0` initializes a large state structure (offsets 24-136), looks up a 184-byte target instruction descriptor, applies up to 8 constraint categories across positions 152-167, and calls `sub_5F8B60` for register resource accounting. The resource accounting function iterates operands from a linked list, loads register class from the `byte_1DFE340` lookup table (52 register types), and switches on the class (0-0x33) to apply constraints via `sub_4FBCB0` (add) and `sub_4FBCE0` (set), with predicate modes 1=read, 2=write, 3=readwrite, 4=clobber.

The engine processes each Mercury instruction and produces one or more SASS instructions:

- **Simple 1:1 mappings**: Most arithmetic and control-flow instructions expand via vtable+48 (generic).
- **1:N expansions**: Memory operations use dedicated handlers (`sub_5FAC90` for shared memory, `sub_5FB5B0` for global, `sub_5FBC30` for constant). Texture instructions use `sub_5FC1B0` (surface path for data type 559-560). Register width clamping (cases 5,8,9) enforces max width = 15.
- **Special cases**: Attribute 200 == 1107 triggers the MOV special-case handler `sub_5FC6B0`, which creates target nodes with opcode 346, sets attribute 227 = 1233, and handles register class 31 with data type 52.

The engine's output is a fully expanded but not yet scheduled instruction stream. WAR hazards from the expansion are handled by the subsequent `MercGenerateWARs1` stage.

## MercConverter Subsystem

[V] The MercConverter subsystem consists of three verified functions:

| Function | Size | Identity | Key string references |
|---|---|---|---|
| `sub_1919030` | 92KB / 2685 lines | MercConverter instruction converter | `"CONVERTING"`, `"Internal compiler error."`, `"swap3"`, `"swap5"`, `"OriCopyProp"` |
| `sub_1977B70` | 35KB / 1341 lines | ORI pass manager merge | `"shuffle"`, `"NamedPhases"`, `"After MercConverter"` |
| `sub_197A120` | 49KB / 1850 lines | ORI named-phase manager | `"NamedPhases"`, `"shuffle"`, `"swap1"`-`"swap6"`, `"dce1"`-`"dce3"`, `"cpy1"`-`"cpy3"`, `"OriPerformLiveDead"`, `"OriCopyProp"` |

The named-phase manager at `sub_197A120` parses phase names and dispatches to implementations. The full phase inventory (verified from string references):

- **Shuffle phase** (`shuffle`): Reorders instructions for better scheduling.
- **Swap phases** (`swap1` through `swap6`): Six iterative swap passes that exchange adjacent instructions when doing so improves scheduling metrics.
- **DCE phases** (`dce1` through `dce3`): Three dead code elimination passes interleaved with swaps.
- **Copy phases** (`cpy1` through `cpy3`): Three copy propagation passes.
- **OriPerformLiveDead**: Liveness analysis recomputation after swaps.
- **OriCopyProp**: Copy propagation cleanup after instruction reordering.

When `MercuryConverterStats` (pass 18) is enabled, the converter prints statistics after each phase. The `"CONVERTING"` string in `sub_1919030` marks the main conversion entry, and `"Internal compiler error."` is the assertion failure path in the converter.

## Global Mercury Options

[V] Two top-level options control whether Mercury semantics and resources are active. Both are verified from `nvlink_strings.json` xrefs (each string has exactly one xref from `ctor_007`):

| Option (ROT13) | Decoded | String addr | Registration addr | Description |
|---|---|---|---|---|
| `HfrZrepFrznagvpf` | **UseMercSemantics** | `0x23F34F0` | `0x424BE0` | Enables Mercury instruction semantics throughout the compiler. When set, the instruction selector, scheduler, and register allocator use Mercury-aware behavior. |
| `HfrZrepErfbheprf` | **UseMercResources** | `0x23F3510` | `0x424B90` | Enables Mercury resource modeling (functional unit counts, scoreboard counts, pipeline depths). Controls whether the scheduler uses the Mercury hardware resource model or the legacy SASS model. |

[V] Mercury mode activation is independently tracked by the global flag `byte_2A5F222`, which is set to 1 when sm > 99 (verified from the main initialization at `sub_409800` and the sweep analysis of the architecture dispatch). The flag `byte_2A5F225` (set when sm > 89) controls SASS mode more broadly.

These two options are the master switches. The 22 per-pass options provide fine-grained control within the Mercury pipeline, but they are only meaningful when `UseMercSemantics` is active.

## AdvancedSB Options Related to Mercury

[V] The `AdvancedSB*` (Advanced ScoreBoard) option family includes 28 ROT13-encoded options for scoreboard management, all registered in `ctor_007`. The following interact with the Mercury pipeline (all verified from `nvlink_strings.json` xrefs and `W052` research report):

| Decoded name | Bit offset | Relevance |
|---|---|---|
| `AdvancedSBCrossBlockMercuryAssume` | `0x5B0` | Enables Mercury assumes in cross-block scoreboard analysis |
| `AdvancedSBDepbarBackedge` | `0x5D0` | Controls dependency barrier insertion at backedges (feeds `MercuryInsertBackedgeDepbar`) |
| `AdvancedSBReqBackedge` | `0x660` | Requires scoreboard entry at backedges |
| `AdvancedSBReqBeforeUsingLiveInPsb` | `0x670` | Requires entry before using live-in values in PSB (feeds `MercuryDepStagePreferNonLiveinPSB`) |
| `AdvancedSBReqCommit` | `0x678` | Requires scoreboard commit at specific points |
| `AdvancedSBDepStageReuse` | `0x610` | Controls dependency-stage reuse policy |
| `AdvancedSBDepStageReuseStallThreshold` | `0x620` | Stall threshold for dependency-stage reuse |
| `AdvancedSBCrossBlock` | `0x59C` | Master switch for cross-block scoreboard analysis |
| `AdvancedSBCrossBlockBudget` | `0x5A0` | Limits computational budget for cross-block analysis |
| `AdvancedSBCrossBlockOnCallee` | `0x5B8` | Extends cross-block analysis across call boundaries |
| `AdvancedSBDeleteUnpredSameVQVsb` | `0x5C0` | Deletes unpredicated same-VQ virtual scoreboard entries |
| `AdvancedSBDepbarDistanceInTime` | `0x5F0` | Controls dependency barrier distance measured in time units |
| `AdvancedSBDepbarDistanceInNumCandidates` | `0x5E0` | Controls dependency barrier distance in candidate count |
| `AdvancedSBDepbarMultipleVqCandidate` | `0x600` | Allows multiple VQ candidates for dependency barriers |
| `AdvancedSBDiffXBlockRdSb` | `0x630` | Differentiates cross-block read scoreboard entries |
| `AdvancedSBFirstLLSBPsb` | `0x640` | Controls first long-latency scoreboard in PSB allocation |
| `AdvancedSBPruningBudget` | `0x650` | Limits pruning budget for scoreboard optimization |
| `AdvancedSBReserved1` | `0x680` | Reserved scoreboard option slot 1 |
| `AdvancedSBReservedHMMA` | `0x690` | Reserved for HMMA (half-precision matrix multiply) scoreboard |
| `AdvancedSBReservedLLNonDepbar` | `0x6A0` | Reserved for long-latency non-depbar scoreboard |
| `AdvancedSBReservedLLNonDepbarSplitSbSize` | `0x6B0` | Split scoreboard size for LL non-depbar |
| `AdvancedSBReservedLLNonDepbarSplitSbThreshold` | `0x6C0` | Split scoreboard threshold for LL non-depbar |

## Diagnostic Messages

| Message | Address | Function | Xref addr | Context |
|---|---|---|---|---|
| `"After MercWARs"` | `0x1D41C60` | `sub_4A47F0` | `0x4A480A` | Printed after WAR generation stages (1 and 2) |
| `"After MercOpex"` | `0x1D41C6F` | `sub_4ABB70` | `0x4ABC3E` | Printed after opex generation |
| `"After MercExpand"` | `0x1DFE320` | `sub_5FF110` | `0x5FF15E` | Printed after instruction expansion |
| `"After MercConverter"` | `0x241F913` | (at `0x19798F8`) | `0x19798F8` | Printed after MercConverter scheduling phases |
| `"After WAR post-expansion"` | `0x1D4157B` | `sub_49D8A0` | — | Non-Mercury WAR pipeline (for comparison) |
| `"After Opex"` | `0x1D41594` | `sub_49D8A0` | — | Non-Mercury opex pipeline (for comparison) |
| `"After WAR post-opexing"` | `0x1D4159F` | `sub_49D8A0` | — | Non-Mercury post-opex WAR pipeline (for comparison) |

## Function Map

All entries verified from decompiled function files and/or sweep analysis of the nvlink binary.

| Address | Name | Size | Role | Verification |
|---|---|---|---|---|
| `0x4A47F0` | `MercWARs_entry` | 11 lines | Mercury WAR generation dispatch; delegates to `sub_4A41D0`, prints `"After MercWARs"` | Decompiled |
| `0x4A4DC0` | `merc_war_process` | 24KB / 784 lines | Mercury WAR dependency handler (actual computation) | Decompiled + sweep |
| `0x4A8690` | `merc_opex_expand` | 66KB / 2602 lines | Mercury operand expansion to final encoding (largest Mercury function) | Decompiled + sweep |
| `0x4ABB70` | `MercOpex_entry` | 94 lines | Mercury opex generation dispatch; calls `sub_A48AA0`, `sub_4A8690`; prints `"After MercOpex"` | Decompiled |
| `0x4AC380` | `capmerc_main` | 9.9KB / 429 lines | Capsule Mercury top-level option parser; defines `--cap-merc`, `--binary-kind`, `--self-check` | Decompiled |
| `0x49D8A0` | `non_merc_WAR_opex` | large (18 params) | Non-Mercury WAR/opex pipeline; prints `"After WAR post-expansion"`, `"After Opex"`, `"After WAR post-opexing"` | Decompiled + sweep |
| `0x471700` | `fnlzr_function_dispatch` | 2541 lines | Per-function compilation dispatch; called from FNLZR Phase 6 (`sub_4748F0` line 1247) | Decompiled |
| `0x5FDDB0` | `MercExpand_Dispatch` | 25.5KB | Main MercExpand dispatch loop; switch on IR opcode type | Sweep (HIGH confidence) |
| `0x5F38E0` | `MercExpand_HandleInstruction` | 35KB | Per-instruction expansion handler (2nd largest in MercExpand) | Sweep (HIGH confidence) |
| `0x5F8B60` | `MercExpand_ApplyResourceConstraints` | 16KB | Register resource accounting; 52 register types | Sweep (HIGH confidence) |
| `0x5FF110` | `MercExpand_entry` | 20 lines | MercExpand entry; calls `sub_5FDDB0`, prints `"After MercExpand"` | Decompiled |
| `0x1919030` | `MercConverter_instruction_converter` | 92KB / 2685 lines | MercConverter main; refs `"CONVERTING"`, `"swap3"`, `"swap5"` | Sweep (HIGH confidence) |
| `0x1977B70` | `ORI_pass_manager_merge` | 35KB / 1341 lines | ORI pass manager; refs `"shuffle"`, `"NamedPhases"`, `"After MercConverter"` | Sweep (HIGH confidence) |
| `0x197A120` | `ORI_named_phase_manager` | 49KB / 1850 lines | Manages shuffle, swap1-6, dce1-3, cpy1-3, OriPerformLiveDead, OriCopyProp | Sweep (HIGH confidence) |

## Registration Infrastructure

[V] The `ctor_007` static initializer spans `0x412790`--`0x426260` (approximately 80KB) and was not separately decompiled by IDA (the gap between `ctor_006` at `0x412790` and `ctor_008` at `0x426260` contains no `.c` file). However, every string reference within this range is verified from `nvlink_strings.json` xref data. The structure is confirmed by analogy with `ctor_004` (decompiled, 798 lines), which uses the same record layout for non-Mercury options.

### Record Layout

[V] Each registration record in the Mercury block is exactly 80 bytes (0x50). This is computed from the uniform spacing between consecutive registration addresses across all 21 Mercury-block passes: `0x426080 - 0x426030 = 0x50`, `0x426030 - 0x425FE0 = 0x50`, ..., `0x425A90 - 0x425A40 = 0x50`. The 80-byte record contains:

| Offset in record | Size | Content | Evidence |
|---|---|---|---|
| +0x00 | 8 | Name string pointer (ROT13-encoded) | Xref type=1 at base of each record |
| +0x33 | 8 | Hex offset string pointer (ROT13-encoded) | Xref type=1 at base+0x33 of each record |
| Other | 64 | Type code, default value, metadata | Inferred from `ctor_004` analogy; not directly readable |

The name string xref is always at the record base address. The hex offset string xref is always at base + 0x33 (51 bytes). This leaves 29 bytes between the two string references for the type code, string length, and other metadata, and 21 bytes after the second reference for the default value and trailing metadata.

### Knob System

The knob system uses ROT13-encoded strings for pass names and ROT13-encoded hex strings for byte offsets. The full binary contains 1,287 knob/option names in this format, organized into groups:

- `AdvancedSB*`: 28 scoreboard management options (offsets `0x59C`--`0x6C0`)
- `Mercury*`: 22 Mercury-specific options (offsets `0x3D40`--`0x3E40`)
- `Convert*`: 37 memory-to-register conversion options
- `Disable*`: 64 optimization disable flags
- `Sink*`: 12 code sinking options
- `Sched*`: 20+ scheduling options
- `RegAlloc*`: 8+ register allocation options

The knob infrastructure is implemented in `generic_knobs_impl.h` (source path: `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/common/utils/generic/impl/generic_knobs_impl.h`, verified from error strings in `sub_498720`). The knobs file reader at `sub_498720` (59KB) looks for a `"[knobs]"` section header (string at `0x1D415D1`, xrefs from `sub_49EE70` and `sub_1761030`) and parses typed values (BXG_INT, BXG_FLOAT, BXG_STR, etc.).

The Mercury option byte offsets are concentrated in a tight range (`0x3D40`--`0x3E40`, 256 bytes). The `AdvancedSBCrossBlockMercuryAssume` option is the exception at `0x5B0`, placed with its `AdvancedSB*` siblings rather than with the Mercury block. Two offsets (`0x3DB8` and `0x3DB9`) are only 1 byte apart, indicating bit-level granularity for the `MercuryForceISAClass` / `MercuryForceUnknownTcgen05Attr` pair.

### String Pool Layout

[V] The Mercury pass name strings and their paired hex offset strings occupy a contiguous region in the string pool from `0x23F2AF5` to `0x23F2E9C`. The layout alternates between hex offset strings and name strings, with each pair stored as (hex offset, name) in increasing address order:

```text
0x23F2AF5: "0k3r40"                             (hex offset for pass 1)
0x23F2B00: "ZrephelHfrNpgvirGuernqPbyyrpgvirVafgf" (name for pass 1)
0x23F2B26: "0k3r30"                             (hex offset for pass 2)
0x23F2B40: "ZrephelGenpxZhygvErnqfJneYngrapl"    (name for pass 2)
...
0x23F2E68: "0k3q40"                             (hex offset for pass 21)
0x23F2E80: "ZrephelNffhzrCGKCbegnovyvgl"         (name for pass 21)
```

The pass 22 strings are located separately at `0x23FC820`--`0x23FC842` within the `AdvancedSB*` string region. The string `"NEEVIRF"` (decodes to ARRIVES, a SASS opcode mnemonic) at `0x23F2AE8` is referenced at `0x4260E6` — this is part of the ctor_007 function but belongs to SASS opcode table initialization, not Mercury pass registration. Similarly, `"USZN2"` (HFMA2) at `0x23F2AD1` and `"YQTFGF"` (LDGSTS) at `0x23F2AB3` are SASS opcode strings that happen to be adjacent to the Mercury pass strings in the string pool.

## Evidence Methodology

The evidence for this page comes from three sources, listed in order of reliability:

1. **Decompiled code** (highest reliability): IDA/Ghidra-produced C pseudocode for functions at known addresses. Used for `sub_5FF110`, `sub_4A47F0`, `sub_4ABB70`, `sub_4AC380`, `sub_4A4DC0`, `sub_4A8690`, `sub_49D8A0`, `sub_471700`, `sub_4748F0`, and others. Directly readable and verifiable.

2. **String cross-references** (high reliability): The `nvlink_strings.json` file contains all strings from the binary with their addresses and xref data (which function references each string, and from what address). Every Mercury pass name string has exactly one xref pointing to `ctor_007`. Every hex offset string has exactly one xref pointing to `ctor_007`. Diagnostic strings (`"After MercExpand"`, `"After MercWARs"`, etc.) have xrefs pointing to their printing functions.

3. **Sweep analysis** (medium reliability): The `p1.XX-sweep-*.txt` files contain structured analysis of decompiled functions in address ranges. Function identities are assigned based on string references, code patterns, call graphs, and size. Functions marked "HIGH confidence" have multiple corroborating signals (string references + code structure + callee analysis). Functions marked "MEDIUM" or "LOW" have fewer signals.

Pass behavior descriptions marked **[V]** are derived from sources 1 and 2. Descriptions marked **[N]** are derived from the decoded option names (source 2 for the name, logical inference for the behavior), corroborated by related infrastructure cross-references. The option names are extremely descriptive — for example, `MercuryTrackMultiReadsWarLatency` unambiguously describes tracking multiple-read WAR latency, and the existence of `MercGenerateWARs1`/`MercGenerateWARs2` pipeline stages confirms that WAR processing is a real pipeline phase.

## Cross-References

### nvlink Internal
- [Mercury Overview](overview.md) — what Mercury is and why it exists
- [Mercury ELF Sections](elf-sections.md) — `.nv.merc.*` section layout consumed by these passes
- [Capsule Mercury Format](capmerc-format.md) — the output format produced by `MercGenerateSassUCode`
- [FNLZR](fnlzr.md) — the post-link finalizer that runs these passes (invocation chain: `sub_4275C0` -> `sub_4748F0` -> `sub_471700` -> phase table)
- [R_MERCURY Relocations](r-mercury-relocations.md) — relocation types resolved during Mercury expansion
- [Scheduling](../ptxas/scheduling.md) — the general scheduling framework that Mercury extends
- [Peephole](../ptxas/peephole.md) — ORI peephole passes (copy-prop, DCE) integrated with Mercury
- [ROT13 Passes Reference](../reference/rot13-passes.md) — full catalog of all 30,349 ROT13-encoded strings

### Sibling Wikis
- [ptxas: Mercury Encoder Pipeline](../../ptxas/codegen/mercury.html) — standalone ptxas Mercury encoder (phases 113–122 in ptxas numbering) with ptxas-native function addresses. The stage names and ordering are identical: MercEncodeAndDecode, MercExpandInstructions, MercGenerateWARs1, MercGenerateOpex, MercGenerateWARs2, MercGenerateSassUCode. Key ptxas addresses: orchestrator `sub_6F52F0` (23KB), master encoder `sub_6D9690` (94KB), expansion `sub_C3CC60` (26KB), WAR generator `sub_6FBC20` (7.4KB), SASS emitter `sub_6E4110` (24KB).
- [ptxas: Capsule Mercury & Finalization](../../ptxas/codegen/capmerc.html) — standalone ptxas capmerc output and finalization pipeline.

**MercExpand sub-handlers**: ptxas wiki documents expansion sub-handlers at different addresses: `sub_C37A10` (16KB, expandInstruction), `sub_C39B40` (10KB, expandMemoryOp), `sub_C3A460` (6KB, expandAtomicOp), `sub_C3B560` (8KB, expandTexture), `sub_C3BCD0` (19KB, expandControlFlow). These correspond to the nvlink embedded copies documented in this page.

**MercConverter**: ptxas wiki documents MercConverter at `sub_9F3340` (7KB orchestrator) + `sub_9EF5E0` (27KB operand reorganization). nvlink's embedded copies are at `sub_1977B70` (35KB) and `sub_1919030` (92KB) respectively — different sizes due to additional linking-specific code in the nvlink version.

## Confidence Assessment

| Claim | Rating | Evidence |
|---|---|---|
| 22 ROT13-encoded Mercury passes registered in `ctor_007` | **HIGH** | All 22 ROT13 strings verified at exact addresses in `nvlink_strings.json`. Each has exactly 1 xref to the `ctor_007` address range (`0x412790`--`0x426260`). |
| 22 ROT13-encoded hex offset strings paired with passes | **HIGH** | All 22 hex offset strings verified at exact addresses in `nvlink_strings.json`. Each has exactly 1 xref to the `ctor_007` address range. |
| Registration record size = 80 bytes (0x50) | **HIGH** | Uniform spacing computed from all 21 Mercury-block pass addresses: every consecutive pair differs by exactly 0x50. |
| Hex offset string at record base + 0x33 | **HIGH** | Verified for all 21 Mercury-block passes by computing the difference between hex offset xref address and name xref address. |
| 6 plaintext pipeline stages in master phase table | **HIGH** | All 6 stage name strings verified: `MercEncodeAndDecode` (`0x2443CA2`), `MercExpandInstructions` (`0x2443CB6`), `MercGenerateWARs1` (`0x2443CCD`), `MercGenerateOpex` (`0x2443CDF`), `MercGenerateWARs2` (`0x2443CF0`), `MercGenerateSassUCode` (`0x2443D02`). |
| Pipeline stage table base at `0x24443F0` | **HIGH** | Xref addresses for stage names form a contiguous 8-byte-spaced sequence starting at `0x24443F0`. |
| ROT13 decoder `sub_1A40AC0` (SIMD-accelerated, 15,629 bytes) | **MEDIUM** | Function exists. SIMD usage inferred from `_mm_load_si128` in decompiler output. Size from function bounds. |
| Pass catalog: all 22 decoded names, bit offsets, string addrs, registration addrs | **HIGH** | Every ROT13 string verified at stated address. Registration addresses confirmed via xref data. Decoded names match ROT13 decode of each string character-by-character. |
| Bit offsets concentrated in `0x3D40`--`0x3E40` (256 bytes) | **HIGH** | Offset values decoded from ROT13 hex strings paired with each pass. Range verified. |
| `AdvancedSBCrossBlockMercuryAssume` at offset `0x5B0` (separate from main block) | **HIGH** | ROT13 string `NqinaprqFOPebffOybpxZrephelNffhzr` at `0x23FC820` verified. Registration at `0x4129E0` is outside the `0x425A40`--`0x426080` main block. Neighboring AdvancedSB options confirmed at `0x23FC80D` and `0x23FC850`. |
| MercExpand dispatch `sub_5FDDB0` (25.5KB, switch on IR opcode type) | **HIGH** | Decompiled file `sub_5FDDB0_0x5fddb0.c` exists. Switch cases 0, 5, 8, 9, 11, 12, 17, -1, 120 verified. |
| MercExpand entry `sub_5FF110` prints `"After MercExpand"` | **HIGH** | Decompiled file exists. String `"After MercExpand"` at `0x1DFE320` verified with xref to `0x5FF15E`. |
| MercConverter `sub_1977B70` (35KB) prints `"After MercConverter"` | **HIGH** | String at `0x241F913` verified. Xref to `0x19798F8` confirmed. Related strings `"shuffle"`, `"NamedPhases"` verified at referenced function. |
| MercWARs entry `sub_4A47F0` prints `"After MercWARs"` | **HIGH** | String at `0x1D41C60` verified. Xref to `0x4A480A` confirmed. |
| MercOpex entry `sub_4ABB70` prints `"After MercOpex"` | **HIGH** | String at `0x1D41C6F` verified. Xref to `0x4ABC3E` confirmed. |
| MercOpex expander `sub_4A8690` (66KB / 2602 lines, largest Mercury function) | **HIGH** | Function exists at stated address. Size from function bounds. Line count from decompiler. |
| MercConverter named phases: shuffle, swap1–6, dce1–3, cpy1–3, OriPerformLiveDead, OriCopyProp | **HIGH** | All phase name strings verified with xrefs to `sub_197A120` (ORI named-phase manager). |
| MercConverter instruction converter `sub_1919030` (92KB) refs `"CONVERTING"` | **HIGH** | Function exists. String `"CONVERTING"` verified with xref to this function. `"Internal compiler error."` also confirmed. |
| `UseMercSemantics` at `0x23F34F0` and `UseMercResources` at `0x23F3510` | **HIGH** | Both ROT13 strings verified at exact addresses. Each has 1 xref to `ctor_007`. |
| `DumpMercOpCounts` at `0x1D4AB40` (ROT13 `QhzcZrepBcPbhagf`) | **HIGH** | ROT13 string verified at stated address. Located in `ctor_004` (xref from `0x410F30`). |
| Knobs infrastructure at `sub_498720` (59KB), source path `generic_knobs_impl.h` | **MEDIUM** | Function exists. Source path string verified in error messages within the function. 1,287 knob count is approximate from string scanning. `"[knobs]"` string at `0x1D415D1` verified with 2 xrefs. |
| Tepid scheduler subsystem `0x16F6000`--`0x1740000` (~296KB) | **MEDIUM** | Address range from sweep analysis. `"TepidMacUtil"` at `0x23EF746` and `"TepidTime"` at `0x23F0851` verified. Metric strings confirmed. |
| Pass behavior descriptions marked [N] (inferred from decoded names) | **MEDIUM** | Names are extremely descriptive and self-documenting. Corroborated by related infrastructure (e.g., `MercuryTrackMultiReadsWarLatency` corroborated by `MercGenerateWARs1`/`MercGenerateWARs2` stages). Exact implementation details may differ from inferred behavior. |
| Default values (enabled/disabled by default) | **MEDIUM** | Inferred from naming conventions ("Force", "Disable", "Dump" = off by default). Not directly verified from ctor_007 body (not decompiled). |
| `byte_2A5F222` set when sm > 99 (Mercury mode activation) | **HIGH** | Verified from decompiled initialization code. Multiple functions check this global. |
| 28 `AdvancedSB*` scoreboard options | **MEDIUM** | Count from enumeration of AdvancedSB-prefixed ROT13 strings. Individual option names and offsets verified but total count may differ by 1–2 due to boundary definition of the option group. |
| FNLZR invokes `sub_471700` at `sub_4748F0` line 1247 | **HIGH** | Direct observation from decompiled `sub_4748F0_0x4748f0.c` file. Call instruction at stated line. |
| `sub_471700` is 2,541 decompiled lines | **HIGH** | Line count from decompiled file `sub_471700_0x471700.c`. |
| String pool layout (alternating hex offset + name strings) | **HIGH** | All 42 string addresses verified to be contiguous in `0x23F2AF5`--`0x23F2E9C` region. Address ordering confirmed from `nvlink_strings.json`. |
