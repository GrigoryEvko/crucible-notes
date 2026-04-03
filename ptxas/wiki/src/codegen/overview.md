# Code Generation Overview

The SASS code generation subsystem converts optimized Ori IR into executable GPU machine code. It is the largest subsystem in ptxas by every metric: approximately 12,000 functions, 9 MB of binary code, and nine functions so large that Hex-Rays cannot decompile them. The pipeline spans phases 112--159 of the 159-phase PhaseManager and comprises seven interlinked subsystems -- instruction selection, SASS binary encoding, peephole optimization, the Mercury encoding pipeline, Newton-Raphson math templates, SASS text generation, and ELF output packaging. Every subsystem dispatches through per-SM-family tables, so the same high-level flow produces correct output for targets from Kepler (sm_30) through Blackwell Ultra (sm_121).

| | |
|---|---|
| **Pipeline phases** | 112--159 (code generation spans the final third of the pipeline) |
| **Total functions** | ~12,000 (ISel, encoding, peephole, Mercury, formatters, ELF) |
| **Total binary size** | ~9 MB of machine code |
| **Non-decompilable functions** | 9 (3 peephole + 6 encoding megadispatchers) |
| **Core primitive** | `sub_7B9B80` -- bitfield insert (216 bytes, 18,347 callers) |
| **Architecture selector** | `*(int*)(config+372) >> 12` -- SM generation ID |
| **Largest function** | `sub_169B190` -- generic peephole dispatcher (280 KB) |
| **Output modes** | `mercury` (SM 75--99), `capmerc` (SM 100+), `sass` (explicit) |
| **CLI option** | `--binary-kind mercury,capmerc,sass` |

## Pipeline

```
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

ISel converts abstract Ori IR operations into concrete SASS instruction forms using SelectionDAG-style pattern matching. Unlike upstream LLVM's TableGen-driven ISel, ptxas uses handwritten C++ matchers compiled into ~750 functions that are invoked from the ISel driver via per-opcode dispatch tables.

### ISel Driver -- `sub_B285D0` (9 KB)

The top-level ISel coordinator is a vtable entry point with 66 callees. It selects the appropriate instruction builder variant based on target architecture and instruction properties:

```c
// Simplified ISel driver
void ISel_LowerInstruction(context, instruction) {
    int sm = *(context + 184);          // SM version
    int opcode = instruction[18] & 0xFFFFCFFF;

    // Select architecture-variant builder
    if (sm == 14)
        Builder_VariantA(context, instruction);    // sub_B1FA20
    else
        Builder_VariantB(context, instruction);    // sub_B20E00

    // Apply post-ISel modifiers
    ApplyModifiers(context, instruction);           // sub_B1D670
    SetProperties(context, instruction);            // sub_B241A0
}
```

### ISel Mega-Selector -- `sub_C0EB10` (185 KB)

The largest function in the ISel range. It handles the full IR-to-SASS mapping through a giant switch/case over instruction opcodes, with per-opcode logic that reads operands, checks constraints, and emits corresponding SASS instructions. It handles special cases like shared memory alias resolution (`"__nv_reservedSMEM_offset_0_alias"`).

### DAG Pattern Matchers -- 750 functions at `0xB30000`--`0xB7D000`

Every matcher shares an identical prototype and follows a strict check-and-report protocol:

```c
// Prototype (all 750 matchers)
char match(int64_t ctx, int64_t instr, int32_t *template_id, int32_t *priority);

// Algorithm
bool match_pattern_XXX(ctx, instr, template_id, priority) {
    // 1. Check instruction properties via DAG node field reader
    if (sub_10AE5C0(ctx, instr, 7) != 21)   return false;   // field 7 must be 21
    if (sub_10AE5C0(ctx, instr, 163) != 705) return false;   // field 163 must be 705

    // 2. Check operand count and types
    if (sub_B28F50(instr) != 2)              return false;   // need 2 source operands
    void *op0 = sub_B28F30(instr, 0);                        // get operand 0
    if (!sub_B28E10(op0))                    return false;   // must be GPR

    // 3. Check register class (1023 = wildcard)
    int regclass = sub_B28E00(op0);
    if (regclass != 1023 && regclass != 3)   return false;

    // 4. Report match with priority
    *template_id = THIS_TEMPLATE_ID;
    *priority = THIS_PRIORITY;
    return true;
}
```

Helper functions used by all matchers:

| Address | Purpose |
|---|---|
| `sub_10AE5C0` | Read DAG node field by ID (field_id to value) |
| `sub_10AE590` | Write DAG node field (opcode_class, encoding) |
| `sub_10AE640` | Modify DAG node (multi-field update, 5 args) |
| `sub_B28F50` | Get source operand count |
| `sub_B28F30` | Get operand by index (returns 24-byte operand record) |
| `sub_B28F40` | Get result operand count |
| `sub_B28E00` | Decode register class from packed field |
| `sub_B28E10` | Validate operand is GPR |
| `sub_B28E20` | Validate operand is immediate/constant |
| `sub_B28E40` | Validate operand is valid register |
| `sub_B28E80` | Check operand is predicate register |
| `sub_B28E90` | Check operand is uniform register |

### Architecture Dispatch Tables -- 4 copies at `sub_B128E0`--`sub_B12920`

Four nearly identical functions (15,049 bytes each) provide architecture-variant opcode dispatch. Each contains a massive switch on `*(a3+12)` (the opcode word field) with all cases jumping to shared code at `0x1C39xxx`. The four copies serve different SM architecture families. Additionally, opcode variant selectors like `sub_B0BE00` (19 KB, opcode class 194) and `sub_B0AA70` (5 KB, opcode class 306) map sub-variant indices to specific SASS encoding slots through a `sub_10AE590(ctx, inst, class, base+K)` pattern.

## SASS Binary Encoding

The encoding subsystem translates ISel output into packed binary SASS machine code. Each instruction is encoded into a 1280-bit (160-byte, 20-QWORD) buffer via the universal bitfield packer `sub_7B9B80`. The full architecture is documented in [SASS Instruction Encoding](./encoding.md); the key facts for the overview:

- **~4,000 encoding handler functions** -- each follows an identical 10-phase template, differing only in constants and modifier helpers
- **6 megadispatchers** (750 KB total) route field-level queries by instruction category: `setField` (180 KB), `getFieldOffset` (197 KB), `hasField` (187 KB), `setFieldDefault` (142 KB), `getOperandFieldOffset` (68 KB), `setOperandField` (65 KB)
- **2,095 bitfield accessor functions** at `0x10B0000`--`0x10BF2C0` (1,661 under 200 bytes)
- **530 encoding table initializers** at `0xC66000`--`0xD27000`, each populating one instruction format row
- **3-level opcode hierarchy**: major (9 bits), minor (8 bits), sub-opcode (7 bits)
- **Instruction widths**: 64-bit (format code 1), 128-bit (format code 2), 256-bit (format code 8)

The four type-specific operand encoders handle the majority of encoding traffic:

| Encoder | Size | Callers | Operand type |
|---|---|---|---|
| `sub_7BC030` | 814 B | 6,147 | Register (R0-R255, UR0-UR63) |
| `sub_7BCF00` | 856 B | 1,657 | Immediate / constant-buffer |
| `sub_7BC5C0` | 416 B | 1,449 | Predicate (PT, P0-P6) |
| `sub_7B9D60` | 408 B | n/a | Reuse flags + guard predicate |

## Peephole Optimization

Three monolithic dispatch functions implement brute-force pattern-match-and-rewrite. The full architecture is documented in [Peephole Optimization](./peephole.md). The key positioning facts:

| Dispatcher | Size | Matchers | Entry trampoline | Runs when |
|---|---|---|---|---|
| `sub_169B190` | 280 KB | 762 | `sub_B12930` | Pre-scheduling (all SM) |
| `sub_143C440` | 233 KB | 1,087 | `sub_B12940` | Pre-scheduling (SM 120 only) |
| `sub_198BCD0` | 233 KB | 1,336 | `sub_B12960` | Post-scheduling (all SM) |

All three use identical architecture: a 373-case primary switch on the 16-bit opcode at `instruction+0x0C`, per-case pattern matcher invocations with priority tracking, and a secondary switch for rewrite actions. The SM 120 dispatcher (`sub_143C440`) is architecture-gated and runs only when compiling for consumer RTX 50-series or enterprise Pro GPUs.

## Mercury Pipeline

Mercury is NVIDIA's intermediate encoding layer between the optimizer's Ori IR and native SASS machine code. It occupies phases 113--122 and forms a six-stage sub-pipeline. The full architecture is documented in [Mercury Encoder Pipeline](./mercury.md). The positioning within the codegen flow:

```
Phase 113  PostFixForMercTargets          Late Ori fixups for Mercury targets
Phase 114  FixUpTexDepBarAndSync          Texture dependency bars + sync fixups
Phase 115  AdvancedScoreboardsAndOpexes   Arch hook point (noop by default)
Phase 116  ProcessO0WaitsAndSBs           -O0 scoreboard insertion
                                          ──────────────────────────────
Phase 117  MercEncodeAndDecode            ┐
Phase 118  MercExpandInstructions         │  Six-stage Mercury core
Phase 119  MercGenerateWARs1              │
Phase 120  MercGenerateOpex               │
Phase 121  MercGenerateWARs2              │
Phase 122  MercGenerateSassUCode          ┘
```

Key functions:

| Address | Size | Identity |
|---|---|---|
| `sub_6D9690` | 94 KB | Master encoder -- largest backend function, massive switch on instruction type |
| `sub_6FFDC0` | 66 KB | Opex body -- generates scoreboards, computes latency waits |
| `sub_6F2BF0` | 59 KB | Decode pipeline -- encode Ori to Mercury, decode back, verify roundtrip |
| `sub_C3CC60` | 26 KB | MercExpand::run -- expand pseudo-instructions to concrete SASS |
| `sub_6F52F0` | 23 KB | RunStages orchestrator (18 parameters) |
| `sub_6E4110` | 24 KB | MercGenerateSassUCode -- final SASS microcode emission |
| `sub_6FBC20` | 7.4 KB | WAR hazard generation (runs twice: before and after opex) |

Three output modes controlled by `--binary-kind`:

| Mode | Default for | Mercury mode flag |
|---|---|---|
| `mercury` | SM 75--99 | `*(DWORD*)(context+385) == 2` |
| `capmerc` | SM 100+ (Blackwell) | Same flag, plus embedded PTX source + relocation metadata |
| `sass` | Explicit only | Direct SASS binary output |

## Newton-Raphson Templates

Double-precision operations that lack dedicated hardware support (DDIV, DRCP, DSQRT, DRSQRT) are lowered into multi-instruction SASS sequences that implement Newton-Raphson iterative refinement. The template system lives at `0x1700000`--`0x1722D60` and is organized in a two-level hierarchy:

### Template Hierarchy

```
sub_170E8B0 (DDIV handler, 1.2 KB)
  └─ sub_170E260 (DDIV coordinator, 1.6 KB)
       │  Allocates 298 virtual registers from dword_23993E0 table
       │  Names templates: "__ori_template_DDIV1/2/3"
       │  Creates 240-byte SASS instruction buffer
       │
       ├─ sub_1704180 — DDIV sequence part 1
       ├─ sub_1705820 — DDIV sequence part 2 (7.5 KB, ~100 SASS instructions)
       ├─ sub_17075A0 — DDIV sequence part 3
       ├─ sub_1709130 — DDIV sequence part 4
       ├─ sub_170AE80 — DDIV sequence part 5
       └─ sub_170CBD0 — DDIV sequence part 6

sub_1718D60 (DRCP/DSQRT handler, 790 B)
  └─ sub_1718790 (DRCP/DSQRT coordinator, 1.5 KB)
       └─ 7 sub-expanders

sub_17276C0 (DRSQRT handler, 1 KB)
  └─ sub_1720D60 (DRSQRT coordinator, 1.4 KB)
       └─ sub-expanders

sub_1727130 (Multi-precision FP coordinator, 1.4 KB)
```

Sub-expanders emit dense SASS sequences including IMAD, FSETP, MOV, SHR, FADD, and MUFU instructions -- the characteristic pattern of Newton-Raphson iterative division/square-root. Each instruction is emitted via `sub_9314F0(buf, ctx, opcode, func, operand_count, operands)` and `sub_934630`.

### Register-Count Dispatch

The DDIV/DRCP dispatcher `sub_1704070` (263 bytes) selects between three expansion strategies based on the target register file size:

```c
int reg_count = *(*(context + 1584) + 372);
if (reg_count > 20479)
    LargeRegPath(args);      // sub_1702990: full inline expansion
else if (reg_count > 16383)
    MediumRegPath(args);     // sub_1701F10: partial inline
else
    SmallRegPath(args);      // sub_1701860: template-based (call to __ori_template_DDIV)
```

When register pressure is low (small-register path), the coordinator builds named template objects (`"__ori_template_DDIV1/2/3"`) that become named code sections in the output. The template is built lazily on first use and cached at `*(handler+12)`.

## SASS Text Generation

Phase 129 (`DumpNVuCodeText`) converts the internal instruction stream into human-readable SASS assembly text for `--verbose` output and `--out-sass` dumps. The subsystem is documented in detail in [SASS Text Generation](./sass-printing.md).

### Architecture

```
sub_5D4190 (12.9 KB, instruction text format dispatcher)
  ├─ 81 named opcodes (direct string comparison)
  ├─ 473 hash-dispatched opcodes (hash-based switch)
  └─ 580 formatter functions at 0x4DA340-0x5A8E40 (~850 KB)
       └─ Each: alloc 50 KB buffer → sprintf via format table → shrink-copy → free
```

All 580 formatter functions are template-generated (mechanically identical structure). They use a monolithic format string table (~1.8 MB) containing pre-assembled PTX/SASS text templates -- an unusual design that trades memory for formatting speed.

The WMMA (tensor core) formatters are the largest, accounting for 34 KB (4% of the range) due to the combinatorial explosion of matrix shapes, data types, and layouts.

## ELF/Cubin Output

The final stage packages the encoded SASS binary into NVIDIA's custom ELF format (.cubin/.o). The ELF emitter chain is:

```
sub_612DE0 (47 KB, kernel finalizer)
  └─ sub_1C9F280 (97 KB, master ELF emitter — largest post-codegen function)
       ├─ sub_713710 (14 KB) — emit symbol table entries
       ├─ sub_7163C0 (13 KB) — emit relocation entries
       ├─ sub_7122C0 (12 KB) — build string table
       └─ sub_716DC0 (10 KB) — finalize section layout
```

## Per-SM Architecture Dispatch

Every code generation subsystem dispatches through architecture-specific tables. The SM generation is determined by `*(int*)(config+372) >> 12`:

| `config+372 >> 12` | Generation | SM versions |
|---|---|---|
| 3 | Kepler | sm_30--sm_37 |
| 5 | Maxwell | sm_50--sm_53 |
| 6 | Pascal | sm_60--sm_62 |
| 7 | Volta / Turing | sm_70--sm_75 |
| 8 | Ampere | sm_80--sm_89 |
| 9 | Hopper | sm_90--sm_90a |
| 10+ | Blackwell | sm_100--sm_121 |

Architecture-specific dispatch points across the codegen pipeline:

| Subsystem | Dispatch mechanism | Evidence |
|---|---|---|
| ISel | 4 arch-variant dispatch tables at `sub_B128E0`--`sub_B12910` | All JUMPOUT to shared code at `0x1C39xxx` |
| Encoding | vtable at `*(context+416)` with ~200 virtual methods | Per-opcode encoding, latency, hazard rules |
| Peephole | 3 mega-dispatchers with per-SM case logic | SM 120 dispatcher (`sub_143C440`) is arch-gated |
| Mercury | `sub_6E8EB0` sets arch-specific flags in opcode descriptor table | SM 80: bits 1, 8; SM 84: bits 16, 64 |
| Statistics | 8 SM-variant printer clones at `sub_ABBA50`--`sub_ABEB50` | 7,603 bytes each, 0x700 spacing |
| NR templates | Register-count-based dispatch at `sub_1704070` | Thresholds: 20479 / 16383 |

## Intrinsic Lowering

The OCG (On-Chip Global) intrinsic system at `0x6C0000`--`0x6D0000` handles PTX builtin operations for SM 100+ targets. The master intrinsic table at `sub_6C9EB0` (13 KB) initializes a 10,664-byte dispatch table with prefix `"__nv_ptx_builtin_ocg_"`, covering operations from basic add/load/store through SM 100 tensor core (tcgen05) and bulk async copy:

| Handler | Size | Operations |
|---|---|---|
| `sub_6C0D90` | 19 KB | Atomic reduce (atom.add/min/max/cas -- 54 validation strings) |
| `sub_6C3470` | 20 KB | cp.async.bulk (bulk async copy) |
| `sub_6C1CF0` | 16 KB | mbarrier (arrive, wait, test, counted variants) |
| `sub_6C4DA0` | 15 KB | Load/store with scope, memory order, domain validation |
| `sub_6C8100` | -- | cp.async.tensor (TMA, SM 90+) |
| `sub_6C5A40` | -- | Cache control (CCTL: shallow/deep, ldc/ldcu, iv/ivall) |
| `sub_6C60B0` | -- | Distributed shared memory (selfcast/broadcast) |
| `sub_6D4350` | 30 KB | MMA intrinsics (HMMA, IMMA, DMMA variants) |
| `sub_6D7AF0` | 19 KB | TCGen05 MMA (SM 100, 5th generation tensor core) |

Intrinsic parameter validators at `sub_6BDB60`--`sub_6BF910` enforce type, sub-operation, and memory domain constraints. NVIDIA consistently misspells "intrinsic" as "instrinsic" in all validation error strings (e.g., `"Unexpected instrinsic param number (%d)"`).

## Post-Scheduling Statistics

Eight SM-variant statistics printers at `sub_ABBA50`--`sub_ABEB50` (7,603 bytes each, spaced 0x700 apart) generate DUMPIR output as `"# [...] "` comments. These print comprehensive post-codegen metrics:

- Instruction counts and register usage (R-regs, UR-regs)
- Spill metrics (`LSpillB`, `LRefillB`)
- Estimated latency, occupancy, divergent branch count
- Per-functional-unit instruction estimates: adu, alu, cbu, fma2x, fma, half, transcendental, ipa, lsu, redux, schedDisp, tex, ttu, udp
- MMA instruction counts: imma16816, imma16832, immaSp, dmma, fma64, hmma variants
- Issue/unit/warp/register throughput
- Texture binding stats (CB-Bound, UR-Bound, Bindless)

The per-unit instruction counter `sub_ABF590` (17 KB) uses SSE2 operations for batch counter updates.

## Operand Legalization

Post-register-allocation operand legalization rewrites instructions that cannot be directly encoded in SASS:

| Address | Size | Purpose |
|---|---|---|
| `sub_AB3C30` | 32 KB | Post-RA instruction legalization (opcodes 288, 167, 185, 241, 299, 300, 317) |
| `sub_AB2D50` | 18 KB | Per-class operand legalization (opcode 307 = ternary/FMA-like) |
| `sub_ACF4D0` | 14 KB | Constraint solver -- splits instructions when direct encoding fails |
| `sub_AB8940` | 19 KB | Register move coalescing / copy elimination |
| `sub_AC2750` | 36 KB | Operand-to-encoding converter (36-byte operand records) |

When legalization requires instruction splitting, `sub_ACF4D0` creates new instructions via `sub_934630` (instruction constructor). The constraint solver tries alternative encodings before resorting to splits.

## WGMMA Pipeline (SM 90+)

The WGMMA (Warp Group Matrix Multiply-Accumulate) pipeline optimizer at `0xACE000`--`0xAE6000` manages asynchronous tensor core execution for Hopper and later architectures. It automatically inserts `warpgroup.arrive` and `warpgroup.wait` fences to ensure correct register handoff between producer and consumer instructions:

```
sub_AE4F70 (coordinator, outside analyzed range)
  ├─ sub_ADDDF0 (21 KB) — pass entry point (vtable)
  │    └─ sub_ADCA60 (22 KB) — scheduling coordinator
  │         └─ sub_ADBD30 (24 KB) — register pressure estimator
  │              ├─ sub_ADAD60 (8 KB) — live range limiter
  │              └─ sub_AD9C20 (14 KB) — per-class register allocator
  ├─ sub_ADEB40 (43 KB) — warpgroup sync fence insertion
  │    strings: "warpgroup.arrive is injected in around line %d..."
  │             "warpgroup.wait is injected in around line %d..."
  ├─ sub_AE17C0 (38 KB) — pipeline stage builder
  └─ sub_ACE480 (23 KB) — serialization warning emitter (9 reasons)
```

The warning emitter (`sub_ACE480`) issues `"Potential Performance Loss"` advisories (codes 7509--7511) when WGMMA pipelining fails due to: extern calls, cross-function pipelines, insufficient registers, ill-formed pipeline stages, non-WGMMA instructions touching accumulator/input registers, or program dependence on compiler-inserted warpgroup fences.

## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_169B190` | 280 KB | Generic peephole dispatcher (all SM, 762 matchers) |
| `sub_143C440` | 233 KB | SM 120 peephole dispatcher (1,087 matchers) |
| `sub_198BCD0` | 233 KB | Post-scheduling peephole dispatcher (1,336 matchers) |
| `sub_C0EB10` | 185 KB | Main instruction selector (500+ locals, giant switch) |
| `sub_10C0B20` | 180 KB | Encoding `setField` megadispatcher (3,109 callers) |
| `sub_10D5E60` | 197 KB | Encoding `getFieldOffset` megadispatcher (961 callers) |
| `sub_10E32E0` | 187 KB | Encoding `hasField` megadispatcher (72 callers) |
| `sub_10CCD80` | 142 KB | Encoding `setFieldDefault` megadispatcher (4 callers) |
| `sub_1C9F280` | 97 KB | Master ELF emitter |
| `sub_6D9690` | 94 KB | Mercury master encoder (instruction type switch) |
| `sub_6FFDC0` | 66 KB | Mercury opex body (scoreboard generation) |
| `sub_6E8EB0` | 64 KB | BasicBlock::Initialize (encoder state, opcode descriptors) |
| `sub_6F2BF0` | 59 KB | DecodePipeline::DecodeAndExpand (roundtrip verification) |
| `sub_612DE0` | 47 KB | Kernel finalizer / ELF builder |
| `sub_ADEB40` | 43 KB | WGMMA sync fence insertion |
| `sub_AE17C0` | 38 KB | WGMMA pipeline stage builder |
| `sub_AC2750` | 36 KB | Operand-to-encoding converter |
| `sub_AB3C30` | 32 KB | Post-RA instruction legalization |
| `sub_C3CC60` | 26 KB | MercExpand::run (pseudo-instruction expansion) |
| `sub_6E4110` | 24 KB | MercGenerateSassUCode (final SASS emission) |
| `sub_6F52F0` | 23 KB | Mercury RunStages orchestrator (18 parameters) |
| `sub_ACE480` | 23 KB | WGMMA serialization warning emitter |
| `sub_AB2D50` | 18 KB | Operand legalization (per-class) |
| `sub_ABF590` | 17 KB | Per-unit instruction counter (SSE2 batch update) |
| `sub_5D4190` | 12.9 KB | SASS text format dispatcher (580 targets) |
| `sub_6C9EB0` | 13 KB | OCG intrinsic table initializer |
| `sub_B285D0` | 9 KB | ISel lowering driver (66 callees) |
| `sub_6FBC20` | 7.4 KB | WAR hazard generation pass |
| `sub_170E260` | 1.6 KB | DDIV template coordinator (298 virtual registers) |
| `sub_7B9B80` | 216 B | Bitfield insert primitive (18,347 callers) |

## Cross-References

- [Instruction Selection](./isel.md) -- DAG pattern matching, builder variants, operand validation
- [SASS Instruction Encoding](./encoding.md) -- bit-level encoding format, 10-phase template, opcode hierarchy
- [Peephole Optimization](./peephole.md) -- 3 mega-dispatchers, 3,185 matchers, priority-based rewrite
- [Mercury Encoder Pipeline](./mercury.md) -- 6-stage sub-pipeline, WAR resolution, opex
- [Capsule Mercury & Finalization](./capmerc.md) -- SM 100+ variant with embedded PTX + relocations
- [Newton-Raphson Templates](./templates.md) -- DDIV/DRCP/DSQRT/DRSQRT software sequences
- [SASS Text Generation](./sass-printing.md) -- 580 formatters, format string table
- [Pipeline Overview](../pipeline/overview.md) -- full PTX-to-SASS compilation flow
- [Phase Manager](../passes/phase-manager.md) -- 159-phase pipeline infrastructure
- [Scheduling Architecture](../scheduling/overview.md) -- 3-phase scheduler (pre-codegen)
- [Register Allocation](../regalloc/overview.md) -- Fatpoint algorithm (pre-codegen)
- [ELF/Cubin Output](../output/elf-emitter.md) -- custom ELF emitter, section catalog
- [Knobs System](../config/knobs.md) -- knobs controlling codegen behavior
