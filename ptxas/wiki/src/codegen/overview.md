# Code Generation Overview

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The SASS code generation subsystem converts optimized Ori IR into executable GPU machine code. It is the largest subsystem in ptxas by every metric: approximately 12,000 functions, 9 MB of binary code, and nine functions so large that Hex-Rays cannot decompile them. The pipeline spans phases 112–158 of the 159-phase PhaseManager and comprises seven interlinked subsystems — instruction selection, SASS binary encoding, peephole optimization, the Mercury encoding pipeline, Newton-Raphson math templates, SASS text generation, and ELF output packaging. Every subsystem dispatches through per-SM-family tables, so the same high-level flow produces correct output for targets from Kepler (sm_30) through Blackwell Ultra (sm_121).

| | |
|---|---|
| **Pipeline phases** | 112–158 (code generation spans the final third of the pipeline) |
| **Total functions** | ~12,000 (ISel, encoding, peephole, Mercury, formatters, ELF) |
| **Total binary size** | ~9 MB of machine code |
| **Non-decompilable functions** | 9 (3 peephole + 6 encoding megadispatchers) |
| **Core primitive** | `sub_7B9B80` — bitfield insert (216 bytes, 18,347 callers) |
| **Architecture selector** | `*(int*)(config+372) >> 12` — SM generation ID |
| **Largest function** | `sub_169B190` — generic peephole dispatcher (280 KB) |
| **Output modes** | `mercury` (SM 75–99), `capmerc` (SM 100+), `sass` (explicit) |
| **CLI option** | `--binary-kind mercury,capmerc,sass` |

## Pipeline

```text
 Optimized Ori IR (register-allocated, scheduled)
      |
      v
 ┌─────────────────────────────────────────────────────────────┐
 │ SASS CODE GENERATION                                        │
 │                                                             │
 │  1. Instruction Selection (ISel) ───────> [isel.md]         │
 │     │  DAG pattern matching: ~750 matchers                  │
 │     │  Mega-selector: sub_C0EB10 (185 KB)                   │
 │     │  4 arch-variant dispatch tables                       │
 │     v                                                       │
 │  2. SASS Binary Encoding ───────────────> [encoding.md]     │
 │     │  ~4,000 template-generated handlers                   │
 │     │  6 megadispatchers (750 KB total)                     │
 │     │  sub_7B9B80 bitfield packer (18,347 callers)          │
 │     v                                                       │
 │  3. Peephole Optimization ──────────────> [peephole.md]     │
 │     │  3 mega-dispatchers: 280+233+233 KB = 746 KB          │
 │     │  ~3,185 pattern matchers                              │
 │     v                                                       │
 │  4. Mercury Pipeline (phases 117-122) ──> [mercury.md]      │
 │     │  Encode/Decode → Expand → WAR → Opex → WAR → SASS    │
 │     │  sub_6D9690 master encoder (94 KB)                    │
 │     v                                                       │
 │  5. Newton-Raphson Templates ───────────> [templates.md]    │
 │     │  DDIV/DRCP/DSQRT/DRSQRT software sequences           │
 │     │  36 functions, up to 298 virtual registers each       │
 │     v                                                       │
 │  6. SASS Text Generation (phase 129) ──> [sass-printing.md] │
 │     │  580 formatter functions + 12.9 KB dispatcher         │
 │     v                                                       │
 │  7. ELF/Cubin Output ──────────────────> [../output/…]      │
 │        sub_612DE0 finalizer → sub_1C9F280 ELF emitter       │
 └─────────────────────────────────────────────────────────────┘
      |
      v
 .cubin / .o (NVIDIA custom ELF)
```

## Phase-to-Subsystem Map

The code generation pipeline occupies phases 112–158. This table maps each phase to its subsystem and documents the six-stage Mercury core that is the dominant path for SM 75+ targets.

| Phase | Name | Subsystem | Detail page |
|---|---|---|---|
| 112 | `PlaceBlocksInSourceOrder` | Block layout | [cfg.md](../ir/cfg.md) |
| 113 | `PostFixForMercTargets` | Mercury pre-fixup | [mercury.md](./mercury.md) |
| 114 | `FixUpTexDepBarAndSync` | Scoreboard / sync | [scoreboards.md](../scheduling/scoreboards.md) |
| 115 | `AdvancedScoreboardsAndOpexes` | Scoreboard hook | [scoreboards.md](../scheduling/scoreboards.md) |
| 116 | `ProcessO0WaitsAndSBs` | Scoreboard (-O0) | [scoreboards.md](../scheduling/scoreboards.md) |
| 117 | `MercEncodeAndDecode` | Mercury core | [mercury.md](./mercury.md) |
| 118 | `MercExpandInstructions` | Mercury core | [mercury.md](./mercury.md) |
| 119 | `MercGenerateWARs1` | Mercury core | [mercury.md](./mercury.md) |
| 120 | `MercGenerateOpex` | Mercury core | [mercury.md](./mercury.md) |
| 121 | `MercGenerateWARs2` | Mercury core | [mercury.md](./mercury.md) |
| 122 | `MercGenerateSassUCode` | Mercury core | [mercury.md](./mercury.md) |
| 123 | `ComputeVCallRegUse` | Post-Mercury bookkeeping | — |
| 124 | `CalcRegisterMap` | Post-Mercury bookkeeping | — |
| 125 | `UpdateAfterPostRegAlloc` | Post-Mercury bookkeeping | — |
| 126 | `ReportFinalMemoryUsage` | Reporting | [dumpir.md](../config/dumpir.md) |
| 127 | `AdvancedPhaseOriPhaseEncoding` | Encoding hook | — |
| 128 | `UpdateAfterFormatCodeList` | Post-Mercury bookkeeping | — |
| 129 | `DumpNVuCodeText` | SASS text output | [sass-printing.md](./sass-printing.md) |
| 130 | `DumpNVuCodeHex` | SASS hex output | [dumpir.md](../config/dumpir.md) |
| 131 | `DebuggerBreak` | Debug | — |
| 132 | `UpdateAfterConvertUnsupportedOps` | Late cleanup | — |
| 133 | `MergeEquivalentConditionalFlow` | Late cleanup | — |
| 134 | `AdvancedPhaseAfterMidExpansion` | Late cleanup hook | — |
| 135 | `AdvancedPhaseLateExpandSyncInstructions` | Late cleanup hook | — |
| 136 | `LateMergeEquivalentConditionalFlow` | Late cleanup | — |
| 137 | `LateExpansionUnsupportedOpsMid` | Late lowering | — |
| 138 | `OriSplitHighPressureLiveRanges` | Late regalloc fixup | — |
| 139–158 | *(architecture-specific)* | Arch backends | [phase-manager.md](../passes/phase-manager.md) |

Subsystem grouping summary:

| Subsystem | Phases | Key property |
|---|---|---|
| Block layout | 112 | Restores source-order block placement |
| Scoreboard / sync | 113–116 | Pre-Mercury texture and dependency bar fixups |
| Mercury core | 117–122 | Six-stage encode-expand-WAR-opex-WAR-emit pipeline |
| Post-Mercury bookkeeping | 123–128 | Register maps, data structure refresh |
| SASS output + debug | 129–131 | Text/hex dumps and debugger hook |
| Late cleanup | 132–138 | Conditional merging, late lowering, live-range splits |
| Arch-specific | 139–158 | 20 backend-overridable phases (no-op by default) |

## Scale

| Subsystem | Functions | Binary size | Key entry point |
|---|---|---|---|
| ISel pattern matchers | ~750 | ~1.3 MB | `sub_B285D0` (ISel driver, 9 KB) |
| ISel mega-selector | 1 | 185 KB | `sub_C0EB10` |
| SASS encoding handlers | ~4,000 | ~2.5 MB | `sub_7B9B80` (bitfield packer) |
| Encoding megadispatchers | 6 | ~750 KB | `sub_10C0B20` (setField, 180 KB) |
| Peephole mega-dispatchers | 3 | ~746 KB | `sub_169B190` (generic, 280 KB) |
| Peephole pattern matchers | ~3,185 | ~1.5 MB | (individual matchers) |
| Mercury pipeline | ~50 | ~400 KB | `sub_6F52F0` (orchestrator, 23 KB) |
| Mercury encode tables | 530 | ~500 KB | format initializers at `0xC66000` |
| Encoding vtable methods | ~2,735 | ~450 KB | tiny dispatchers at `0xAF0000` |
| Newton-Raphson templates | 36 | ~180 KB | `sub_170E260` (DDIV coordinator) |
| SASS text formatters | 580 | ~850 KB | `sub_5D4190` (dispatcher, 12.9 KB) |
| ELF emitter | ~60 | ~300 KB | `sub_1C9F280` (master, 97 KB) |
| **Total** | **~12,000** | **~9 MB** | |

Nine functions exceed the decompilation threshold: the three peephole mega-dispatchers (280 + 233 + 233 KB) and the six encoding megadispatchers (180 + 197 + 187 + 142 + 68 + 65 KB). All analysis of these functions derives from disassembly, call graphs, and the smaller functions they invoke.

## Instruction Selection

ISel converts abstract Ori IR operations into concrete SASS instruction forms using SelectionDAG-style pattern matching. Unlike upstream LLVM's TableGen-driven ISel, ptxas uses handwritten C++ matchers compiled into ~750 functions invoked from the ISel driver via per-opcode dispatch tables. The ISel driver (`sub_B285D0`, 9 KB, 66 callees) selects architecture-variant builders based on the SM version. The mega-selector (`sub_C0EB10`, 185 KB) handles the full IR-to-SASS mapping through a giant switch over instruction opcodes. Four nearly identical dispatch functions (15,049 bytes each) at `sub_B128E0`--`sub_B12920` provide architecture-variant opcode routing, all jumping to shared handler code at `0x1C39xxx`.

See [Instruction Selection](./isel.md) for the full DAG matcher protocol, helper function table, architecture dispatch tables, and operand variant selectors.

## SASS Binary Encoding

The encoding subsystem translates ISel output into packed binary SASS machine code. Each instruction is encoded into a 1280-bit (160-byte, 20-QWORD) buffer via the universal bitfield packer `sub_7B9B80`. The full architecture is documented in [SASS Instruction Encoding](./encoding.md); the key facts for the overview:

- **~4,000 encoding handler functions** — each follows an identical 10-phase template, differing only in constants and modifier helpers
- **6 megadispatchers** (750 KB total) route field-level queries by instruction category: `setField` (180 KB), `getFieldOffset` (197 KB), `hasField` (187 KB), `setFieldDefault` (142 KB), `getOperandFieldOffset` (68 KB), `setOperandField` (65 KB)
- **2,095 bitfield accessor functions** at `0x10B0000`--`0x10BF2C0` (1,661 under 200 bytes)
- **530 encoding table initializers** at `0xC66000`--`0xD27000`, each populating one instruction format row
- **3-level opcode hierarchy**: major (9 bits), minor (8 bits), sub-opcode (7 bits)
- **Instruction widths**: 64-bit (format code 1), 128-bit (format code 2), 256-bit (format code 8)

## Peephole Optimization

Three monolithic dispatch functions implement brute-force pattern-match-and-rewrite. The full architecture is documented in [Peephole Optimization](./peephole.md). The key positioning facts:

| Dispatcher | Size | Matchers | Entry trampoline | Runs when |
|---|---|---|---|---|
| `sub_169B190` | 280 KB | 762 | `sub_B12930` | Pre-scheduling (all SM) |
| `sub_143C440` | 233 KB | 1,087 | `sub_B12940` | Pre-scheduling (SM 120 only) |
| `sub_198BCD0` | 233 KB | 1,336 | `sub_B12960` | Post-scheduling (all SM) |

All three use identical architecture: a 373-case primary switch on the 16-bit opcode at `instruction+0x0C`, per-case pattern matcher invocations with priority tracking, and a secondary switch for rewrite actions. The SM 120 dispatcher (`sub_143C440`) is architecture-gated and runs only when compiling for consumer RTX 50-series or enterprise Pro GPUs.

## Mercury Pipeline

Mercury is NVIDIA's intermediate encoding layer between the optimizer's Ori IR and native SASS machine code. It occupies phases 113–122 and forms a six-stage sub-pipeline. Three output modes are controlled by `--binary-kind`: `mercury` (SM 75–99), `capmerc` (SM 100+, with embedded PTX source and relocation metadata), and `sass` (explicit direct SASS output). The master encoder `sub_6D9690` (94 KB) is the largest backend function, with the orchestrator `sub_6F52F0` (23 KB, 18 parameters) driving the full stage sequence.

See [Mercury Encoder Pipeline](./mercury.md) for the six-stage architecture, key function table, and output mode details. See [Capsule Mercury & Finalization](./capmerc.md) for the SM 100+ variant.

## Newton-Raphson Templates

Double-precision operations lacking dedicated hardware (DDIV, DRCP, DSQRT, DRSQRT) are lowered into multi-instruction SASS sequences implementing Newton-Raphson iterative refinement. The template system at `0x1700000`--`0x1722D60` comprises 36 functions organized in a two-level hierarchy: a top-level handler per operation delegates to a coordinator that allocates up to 298 virtual registers and chains 5–7 sub-expander functions. The register-count dispatcher `sub_1704070` selects between full inline, partial inline, and template-based expansion paths based on register file pressure (thresholds: 20,479 / 16,383).

See [Newton-Raphson Templates](./templates.md) for the complete template hierarchy, register-count dispatch logic, and sub-expander details.

## SASS Text Generation

Phase 129 (`DumpNVuCodeText`) converts the internal instruction stream into human-readable SASS assembly text for `--verbose` output and `--out-sass` dumps. The dispatcher `sub_5D4190` (12.9 KB) routes 81 named opcodes via direct string comparison and 473 via hash-based switch to 580 template-generated formatter functions at `0x4DA340`--`0x5A8E40` (~850 KB). All formatters use a monolithic 1.8 MB format string table — an unusual design that trades memory for formatting speed.

See [SASS Text Generation](./sass-printing.md) for the full formatter architecture and opcode routing details.

## ELF/Cubin Output

The final stage packages the encoded SASS binary into NVIDIA's custom ELF format (.cubin/.o). The kernel finalizer `sub_612DE0` (47 KB) feeds the master ELF emitter `sub_1C9F280` (97 KB), which delegates to symbol table emission (`sub_713710`), relocation generation (`sub_7163C0`), string table construction (`sub_7122C0`), and section layout finalization (`sub_716DC0`).

See [ELF/Cubin Output](../output/elf-emitter.md) for section catalog, relocation format, and EIATTR attribute encoding.

## Intrinsic Lowering

The OCG (On-Chip Global) intrinsic system at `0x6C0000`--`0x6D0000` handles PTX builtin operations for SM 100+ targets. The master intrinsic table at `sub_6C9EB0` (13 KB) initializes a 10,664-byte dispatch structure: vtable at +0, prefix `"__nv_ptx_builtin_ocg_"` at +120, then 43 operation slots of 248 bytes each from +128 through +10,544. Each slot stores the operation name at +0 followed by up to 30 sub-operation/modifier string pointers. The 30 numeric literals IDA left unresolved are string pointers into the rodata segment (e.g., `30471598` = `0x1D0F5AE` = `"u32"`).

Complete dispatch table (43 slots, all sub-operations resolved from binary):

| Slot | Offset | Operation | Sub-operations / modifiers | SASS target |
|---:|---:|---|---|---|
| 0 | 128 | `add` | s32, f32, s64, f64, sat | IADD3 / FADD |
| 1 | 376 | `cp_async_commit` | mem, bulk, shared, global | LDGDEPBAR |
| 2 | 624 | `cp_async_wait` | mem, bulk, shared, global, read, write | DEPBAR |
| 3 | 872 | `cache` | tensor, pf, iv, ivall | CCTL / PREFETCH |
| 4 | 1120 | `ld_mc` | add, min, max, f32add, and, or, xor; f16x2..f16x8, bf16x2..bf16x8, f32..f32x4, f64, u32, s32, s64, u64 | LDG.MC |
| 5 | 1368 | `ldc` | u32, u64 | LDC |
| 6 | 1616 | `s2r` | *(none)* | S2R |
| 7 | 1864 | `acqblk` | *(none)* | barrier acquire |
| 8 | 2112 | `preexit` | *(none)* | EXIT.KEEPREFCOUNT |
| 9 | 2360 | `red_async` | release; shared, global; gpu, sys, mmio; v2, v4; u32, s32, u64; add, min, max, inc, dec, and, or, xor | RED.ASYNC |
| 10 | 2608 | `cp_async_bulk` | mbarrier, counted, shared, global, multicast, sequenced, bytemask | UBLKCP |
| 11 | 2856 | `cp_red_async_bulk` | mbarrier, counted, shared, global; u32, s32, u64, s64, f16, f32, f32ftz, f64, bf16; add..xor, inc, dec | UBLKCP.RED |
| 12 | 3104 | `cp_async_tensor` | mbarrier, shared, global; 1d–5d, im2col, multicast | UTMAKCP |
| 13 | 3352 | `cp_async_prefetch_tensor` | global; 1d–5d, im2col | UTMAPF |
| 14 | 3600 | `fence_view_async` | all, global, shared, dshared, tensor | FENCE.VIEW.ASYNC |
| 15 | 3848 | `viadd` | 32, f16x2 | VIADD |
| 16 | 4096 | `viaddmax` | s32, u32, s16x2, u16x2, relu | VIADDMNMX |
| 17 | 4344 | `viaddmin` | s32, u32, s16x2, u16x2, relu | VIADDMNMX |
| 18 | 4592 | `vimax` | s32, u32, s16x2, u16x2, relu | VIMNMX |
| 19 | 4840 | `vimin` | s32, u32, s16x2, u16x2, relu | VIMNMX |
| 20 | 5088 | `vimax3` | s32, u32, s16x2, u16x2, relu | VIMNMX3 |
| 21 | 5336 | `vimin3` | s32, u32, s16x2, u16x2, relu | VIMNMX3 |
| 22 | 5584 | `write_async` | release; shared, global; gpu, sys, mmio; v2, v4; u8, s8, u16, s16, b32, b64, u32, f64 | STG.ASYNC |
| 23 | 5832 | `cctl_c` | ldc, ldcu, shallow, deep, iv, ivall | CCTL |
| 24 | 6080 | `getnextworkid` | selfcast, broadcast | S2R (special) |
| 25 | 6328 | `fadd2` | ftz; rn, rm, rp, rz | HADD2 |
| 26 | 6576 | `ffma2` | ftz; rn, rm, rp, rz | HFMA2 |
| 27 | 6824 | `fmul2` | ftz; rn, rm, rp, rz | HMUL2 |
| 28 | 7072 | `mnmx` | s32, u32, s64, u64 | IMNMX / FMNMX |
| 29 | 7320 | `fmax3` | ftz, nan | FMNMX3 |
| 30 | 7568 | `fmin3` | ftz, nan | FMNMX3 |
| 31 | 7816 | `tcbar` | cta1, cta2, a1t0, a0tx, flush, multicast, b32, mmareadshma | TCBAR |
| 32 | 8064 | `tccp` | 128dp256bit, 4dp256bit, 128dp128bit, 2x64dp128bitlw02lw13, 2x64dp128bitlw01lw23, 4x32dp128bit, u4x16p64, u6x16p32; cta1, cta2; b32, b64 | TCCP |
| 33 | 8312 | `tcmma` | gdesc, tmem; h, i, q, o, mxq; cta1, cta2; ashift, scale, lutb; areuse, akeep, breuse, bkeep; ws; buffer0–buffer3; 2x, 4x, blockscale, impl; b32, b64, u32 | TCMMA |
| 34 | 8560 | `tcshift` | cta1, cta2, b32 | TCSHIFT |
| 35 | 8808 | `virtcount` | u32 | scoreboard |
| 36 | 9056 | `tcatomsws` | and, or, findandset, align, cas; cta1, cta2; b32, b64 | TCATOM.SWS |
| 37 | 9304 | `tcldsws` | cta1, cta2 | TCLD.SWS |
| 38 | 9552 | `tcstsws` | cta1, cta2; b32, b64 | TCST.SWS |
| 39 | 9800 | `memclear` | b32, b64 | MEMCLEAR |
| 40 | 10048 | `acqshminit` | *(none)* | shmem init barrier |
| 41 | 10296 | `ldtm` | 16dp128bit..32dp32bit; x1–x128; pack16bit, fused, stat; nan, max, maxabs, min, minabs; u32, s32, f32, b32; sparsify, u2, spfactor2to4 | LDTM |
| 42 | 10544 | `sttm` | 16dp128bit..32dp32bit; x1–x128; expand16bit, fused; b32 | STTM |

Handler functions that consume this table:

| Handler | Size | Operations |
|---|---|---|
| `sub_6C0D90` | 19 KB | Atomic reduce (atom.add/min/max/cas — 54 validation strings) |
| `sub_6C3470` | 20 KB | cp.async.bulk (bulk async copy) |
| `sub_6C1CF0` | 16 KB | mbarrier (arrive, wait, test, counted variants) |
| `sub_6C4DA0` | 15 KB | Load/store with scope, memory order, domain validation |
| `sub_6D4350` | 30 KB | MMA intrinsics (HMMA, IMMA, DMMA variants) |
| `sub_6D7AF0` | 19 KB | TCGen05 MMA (SM 100, 5th generation tensor core) |

Intrinsic parameter validators at `sub_6BDB60`--`sub_6BF910` enforce type, sub-operation, and memory domain constraints. NVIDIA consistently misspells "intrinsic" as "instrinsic" in all validation error strings.

## Post-Scheduling Statistics

Eight SM-variant statistics printers at `sub_ABBA50`--`sub_ABEB50` (7,603 bytes each, spaced 0x700 apart) generate `"# [...] "` comments with comprehensive post-codegen metrics: instruction counts, register usage, spill/refill bytes, estimated latency and occupancy, per-functional-unit instruction estimates, MMA counts, and throughput figures. The per-unit instruction counter `sub_ABF590` (17 KB) inlines large struct copies as `_mm_loadu_si128` runs (55 16-byte loads in the decompilation) when bulk-copying its per-functional-unit count tables.

## Operand Legalization

Post-register-allocation operand legalization rewrites instructions that cannot be directly encoded in SASS:

| Address | Size | Purpose |
|---|---|---|
| `sub_AB3C30` | 32 KB | Post-RA instruction legalization (opcodes 288, 167, 185, 241, 299, 300, 317) |
| `sub_AB2D50` | 18 KB | Per-class operand legalization (opcode 307 = ternary/FMA-like) |
| `sub_ACF4D0` | 14 KB | Constraint solver — splits instructions when direct encoding fails |
| `sub_AB8940` | 19 KB | Register move coalescing / copy elimination |
| `sub_AC2750` | 36 KB | Operand-to-encoding converter (36-byte operand records) |

When legalization requires instruction splitting, `sub_ACF4D0` creates new instructions via `sub_934630` (instruction constructor). The constraint solver tries alternative encodings before resorting to splits.

## WGMMA Pipeline (SM 90+)

The WGMMA (Warp Group Matrix Multiply-Accumulate) pipeline optimizer at `0xACE000`--`0xAE6000` manages asynchronous tensor core execution for Hopper and later. It automatically inserts `warpgroup.arrive` and `warpgroup.wait` fences to ensure correct register handoff. The warning emitter (`sub_ACE480`) issues `"Potential Performance Loss"` advisories (codes 7509–7511) when pipelining fails due to extern calls, insufficient registers, or ill-formed pipeline stages.

See [WGMMA Pipeline Optimizer](../passes/gmma-pipeline.md) for the full call tree, register pressure estimator, and serialization warning details.

## Per-SM Architecture Dispatch

Every code generation subsystem dispatches through architecture-specific tables. The SM generation is determined by `*(int*)(config+372) >> 12`:

| `config+372 >> 12` | Generation | SM versions |
|---|---|---|
| 3 | Kepler | sm_30–sm_37 |
| 5 | Maxwell | sm_50–sm_53 |
| 6 | Pascal | sm_60–sm_62 |
| 7 | Volta / Turing | sm_70–sm_75 |
| 8 | Ampere | sm_80–sm_89 |
| 9 | Hopper | sm_90–sm_90a |
| 10+ | Blackwell | sm_100–sm_121 |

Architecture-specific dispatch points across the codegen pipeline:

| Subsystem | Dispatch mechanism | Evidence |
|---|---|---|
| ISel | 4 arch-variant dispatch tables at `sub_B128E0`--`sub_B12910` | All JUMPOUT to shared code at `0x1C39xxx` |
| Encoding | vtable at `*(context+416)` with ~200 virtual methods | Per-opcode encoding, latency, hazard rules |
| Peephole | 3 mega-dispatchers with per-SM case logic | SM 120 dispatcher (`sub_143C440`) is arch-gated |
| Mercury | `sub_6E8EB0` sets arch-specific flags in opcode descriptor table | SM 80: bits 1, 8; SM 84: bits 16, 64 |
| Statistics | 8 SM-variant printer clones at `sub_ABBA50`--`sub_ABEB50` | 7,603 bytes each, 0x700 spacing |
| NR templates | Register-count-based dispatch at `sub_1704070` | Thresholds: 20479 / 16383 |

## Function Map (Top 10)

| Address | Size | Identity |
|---|---|---|
| `sub_169B190` | 280 KB | Generic peephole dispatcher (all SM, 762 matchers) |
| `sub_10D5E60` | 197 KB | Encoding `getFieldOffset` megadispatcher (961 callers) |
| `sub_10E32E0` | 187 KB | Encoding `hasField` megadispatcher (72 callers) |
| `sub_C0EB10` | 185 KB | Main instruction selector (500+ locals, giant switch) |
| `sub_10C0B20` | 180 KB | Encoding `setField` megadispatcher (3,109 callers) |
| `sub_10CCD80` | 142 KB | Encoding `setFieldDefault` megadispatcher (4 callers) |
| `sub_1C9F280` | 97 KB | Master ELF emitter |
| `sub_6D9690` | 94 KB | Mercury master encoder (instruction type switch) |
| `sub_6FFDC0` | 66 KB | Mercury opex body (scoreboard generation) |
| `sub_6E8EB0` | 64 KB | BasicBlock::Initialize (encoder state, opcode descriptors) |

See [function-map.md](../function-map.md) for the complete table (~30 entries with all codegen functions).

## Cross-References

- [Instruction Selection](./isel.md) — DAG pattern matching, builder variants, operand validation
- [SASS Instruction Encoding](./encoding.md) — bit-level encoding format, 10-phase template, opcode hierarchy
- [Peephole Optimization](./peephole.md) — 3 mega-dispatchers, 3,185 matchers, priority-based rewrite
- [Mercury Encoder Pipeline](./mercury.md) — 6-stage sub-pipeline, WAR resolution, opex
- [Capsule Mercury & Finalization](./capmerc.md) — SM 100+ variant with embedded PTX + relocations
- [Newton-Raphson Templates](./templates.md) — DDIV/DRCP/DSQRT/DRSQRT software sequences
- [SASS Text Generation](./sass-printing.md) — 580 formatters, format string table
- [Pipeline Overview](../pipeline/overview.md) — full PTX-to-SASS compilation flow
- [Phase Manager](../passes/phase-manager.md) — 159-phase pipeline infrastructure
- [Scheduling Architecture](../scheduling/overview.md) — 3-phase scheduler (pre-codegen)
- [Register Allocation](../regalloc/overview.md) — Fatpoint algorithm (pre-codegen)
- [ELF/Cubin Output](../output/elf-emitter.md) — custom ELF emitter, section catalog
- [Knobs System](../config/knobs.md) — knobs controlling codegen behavior
