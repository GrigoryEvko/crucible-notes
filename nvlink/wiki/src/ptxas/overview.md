# Embedded ptxas: Architecture Overview

> **Note**: This page is the entry point to the nvlink-internal documentation of the ptxas backend statically linked into nvlink v13.0.88. The address map, mega-hub layout, ROT13 details, and `sub_1112F30` per-module compilation driver below are recovered from nvlink's binary -- not from the standalone ptxas binary. For the standalone ptxas reverse-engineering reference (159-phase pipeline, full Ori IR architecture, target catalog), see the [ptxas wiki](../../ptxas/index.html) -- in particular [Pipeline Overview](../../ptxas/pipeline/overview.html), [Codegen Overview](../../ptxas/codegen/overview.html), and [Targets](../../ptxas/targets/index.html).

The single most important structural fact about nvlink v13.0.88 is that approximately 95% of its 25.2 MB `.text` section is not linker code -- it is a complete, statically embedded copy of the ptxas assembler/compiler backend. The actual device linker (ELF merge, symbol resolution, relocation, layout, output) occupies roughly 1.2 MB in the address range `0x400000`--`0x530000`. Everything from `0x530000` through the end of `.text` at `0x1D32172` (~24 MB, ~38,000 functions) is the ptxas compiler backend: IR primitives, instruction selection, register allocation, instruction scheduling, SASS binary encoding, PTX parsing, and ELF/cubin output generation.

This page documents the evidence for this claim, the complete address map of the embedded ptxas subsystems, the five mega-hub instruction selector dispatch functions, and the ROT13 obfuscation applied to SASS mnemonics.

## Evidence for Embedded ptxas

The embedded compiler is not a stripped-down stub -- it is a full-featured PTX-to-SASS compilation pipeline identical in capability to the standalone `ptxas` binary shipped in the CUDA toolkit. Key evidence:

1. **Named memory pools.** The linker creates `"nvlink option parser"` and `"nvlink memory space"` arenas at startup. The embedded compiler creates its own arenas with ptxas-specific names. Memory pool diagnostics at `0x1AEE070` report pool usage statistics (total, freeable, leaked) for the compiler's internal allocations.

2. **Full option parser.** `sub_1103030` (29,803 bytes) registers the complete ptxas command-line option set via `sub_42F130`: `--maxrregcount`, `--opt-level`, `--gpu-name`, `--device-debug`, `--fast-compile`, `--register-usage-level`, `--compile-only`, `--minnctapersm`, `--warn-spills`, `--lineinfo`, `--sp-bounds-check`, `--device-stack-protector`, `--sanitize`, `--position-independent-code`, and approximately 50 more. These are forwarded from nvlink's LTO pipeline into the embedded compiler.

3. **Full compilation pipeline.** `sub_1112F30` (65,018 bytes) at `0x1112F30` is the top-level per-module compilation driver. It writes PTX headers (`.version`, `.target`, `.entry __cuda_dummy_entry__ { ret; }`), selects codegen callbacks based on mode flags (`--compile-as-tools-patch`, `--extensible-whole-program`, `--compile-only`), validates SM version compatibility, and dispatches to per-function codegen initialization.

4. **Multi-architecture support.** `sub_15C0CE0` (14,517 bytes) initializes 7 dispatch hash maps covering sm_75, sm_80, sm_86, sm_87, sm_88, sm_89, sm_90/90a, sm_100/100a/100f, sm_103/103a/103f, sm_110/110a/110f, sm_120/120a/120f, and sm_121/121a/121f. Each architecture gets 7 registered callbacks (nv.info emitter, resource usage table, instruction encoding table, compute capability array, perf-stats handler, cpf_optx handler, codegen options).

5. **Register allocation and instruction scheduling.** The range `0x1850000`--`0x1A00000` contains the full backend compiler core: `ScheduleInstructions` (85 KB), `ScheduleInstructionsReduceReg`, `DynBatch`, `HoistInvariants`, `ConvertMemoryToRegister`, spilling regalloc, SMEM spilling, multi-class register allocation (R-regs, UR-regs, predicates), `setmaxnreg` CTA-reconfig for Blackwell+, and codegen verification passes.

6. **ISel mega-hubs.** Five functions exceed 160 KB each. These are the top-level instruction selector dispatch functions, too large for Hex-Rays to decompile. Each calls hundreds of pattern matchers, selects the highest-priority match, and dispatches to the corresponding emitter.

## Relationship to Standalone ptxas

The standalone `ptxas` binary in the CUDA toolkit and the compiler backend embedded in nvlink share the same codebase. They differ in how they are invoked:

- **Standalone ptxas**: Invoked as a separate process by `nvcc`. Reads `.ptx` files from disk, writes `.cubin` files.
- **Embedded ptxas in nvlink**: Invoked in-process during LTO (`-lto`) and PTX JIT compilation. The entry point is `sub_4BD760` (called from `main()` when a PTX input file is detected) or `sub_4BC6F0` (called for LTO IR compilation after libnvvm produces PTX output). Options are forwarded programmatically rather than via argc/argv.

The embedded copy supports thread-pool parallelism for split compilation (`sub_43FDB0` creates the pool, `sub_4264B0` dispatches per-function work items). This is the same `--split-compile-extended` feature available in standalone ptxas.

## Embedded ptxas Address Map

The following table maps the full address range of the embedded ptxas backend. All addresses are within the `.text` section of nvlink v13.0.88.

### IR Primitives (0x530000 -- 0x620000, ~960 KB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x530E80`--`0x530FD0` | <1 KB | **IR node accessors** | 22 | `sub_530FB0` has 31,399 callers -- universal `getOperand(idx)` |
| `0x530FE0`--`0x5B1AB0` | 523 KB | ISel pattern matchers (SM50-7x) | 1,293 | 152 target opcodes, 36 priority levels |
| `0x5B1D80`--`0x5E4470` | 204 KB | **MercExpand mega-hub** | 1 | MercExpand dispatch + CFG analysis (too large for Hex-Rays) |
| `0x5E4470`--`0x600260` | 114 KB | MercExpand engine | ~50 | Bitvector ops, FNV-1a hash maps, register constraint propagation |
| `0x603F60`--`0x61FA60` | 112 KB | SM50 instruction encoders | 79 | Per-instruction binary encoding functions |

The IR node structure is accessed through 22 leaf functions that constitute the most-called code in the entire binary. `sub_530FB0` (get operand by index) at 31,399 callers and `sub_A49150` (get instruction attribute) at 30,768 callers form the universal accessor layer. The IR node layout:

```
Offset  Size   Field
  0     1B     operand type tag (1=immediate, 2=register, 6=memref, ...)
  4     4B     register class / encoding field (1023 = wildcard "any")
 14     1B     flag A
 15     1B     flag B
 20     4B     data type / secondary encoding
 28     2B     IR opcode
 32     8B     pointer to operand array (each operand = 32 bytes)
 40     4B     total operand count
 92     4B     first source operand index
```

Number of source operands = `*(off+40) + 1 - *(off+92)`. Number of destination operands = `*(off+92)`.

### ISA Encoding Tables (0x620000 -- 0xA70000, ~4.3 MB)

This is the largest contiguous subsystem -- 4.3 MB of template-instantiated functions defining the complete NVIDIA GPU instruction set encoding and metadata.

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x620000`--`0x84DD70` | 2.2 MB | **SM100+ SASS encoders** | 1,537 | 128-bit instruction encoders for Blackwell ISA |
| `0x84DD70`--`0xA48290` | 1.7 MB | **InstrDesc init table** | 1,613 | Instruction descriptor initializers (operand types, latencies) |
| `0xA49010`--`0xA4AB10` | 4 KB | NVInst accessors | ~30 | IR instruction class hierarchy |
| `0xA4AB10` | 11 KB | NVInst constructor | 1 | Allocates and initializes instruction IR node |
| `0xA4B5E0`--`0xA4C7C0` | 5 KB | FNV-1a hash tables | 4 | Instruction lookup by hash |
| `0xA5B6B0` | 180 KB | `setOperandField` dispatch | 1 | Giant switch: sets operand fields by opcode class |
| `0xA62220` | 65 KB | `setOperandImm` dispatch | 1 | Giant switch: sets immediate operand values |
| `0xA65900` | 67 KB | `getOperandField` dispatch | 1 | Giant switch: reads operand fields |
| `0xA67910` | 141 KB | `getDefaultOperandValue` | 1 | Giant switch: returns default operand values per opcode |

The 1,537 SM100+ encoders each translate one instruction variant into a 128-bit SASS instruction word via the core primitive `sub_4C28B0(buf, bit_offset, width, value)`. Opcode breakdown: major=1 (ALU/Scalar) 37.2%, major=2 (Vector/Memory/Control) 62.7%, major=3 (Special) 0.1%, across 118 instruction families.

The 1,613 InstrDesc initializers populate per-instruction metadata: operand count, operand types/constraints, scheduling hints, latency estimates, and execution unit assignments. Combined, the encoder + descriptor tables define the complete NVIDIA GPU ISA from SM50 through SM121.

### Instruction Codecs (0xA70000 -- 0xCA0000, ~2.2 MB)

Multi-architecture instruction encoding and decoding, organized per-SM.

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0xA709F0` | 54 KB | Field offset query | 1 | 6,491-line switch: `(opcode_class, field_id) -> bit_offset` |
| `0xA7DE70` | 50 KB | Field presence query | 1 | Mirror: returns `hasField` boolean |
| `0xA87CE0`--`0xB25D50` | 630 KB | SM90/100 encoders | ~164 | Per-opcode binary instruction encoders |
| `0xACECF0`--`0xB77B60` | 700 KB | SM90/100 decoders | ~139 | Binary-to-IR instruction decoders |
| `0xB9FDE0`--`0xBC2CC0` | 142 KB | **SM7x (Volta/Turing) codecs** | ~60 | Encoders + decoders for SM70/SM75 |
| `0xBC3FC0`--`0xBFEC10` | 236 KB | SM75 extended codecs | ~80 | Turing-specific instruction variants |
| `0xC00070`--`0xC2FB60` | 193 KB | **SM80 (Ampere) codecs** | ~70 | Ampere instruction encoders |
| `0xC3D540`--`0xC50970` | 83 KB | SM80 decoders | ~15 | HMMA tensor core, SHF, memory decoders |
| `0xC7EC90`--`0xC9EE60` | 131 KB | **SM86/89 (Ada) codecs** | ~40 | GA10x / AD10x encoders + decoders |

Each encoder packs IR operands into a 128-bit SASS instruction word at `*(a1+40)`. Each decoder unpacks a 128-bit word back into IR form. The sentinel value 1023 (register field) maps to RZ (zero register), and 31 (predicate field) maps to PT (true predicate). Architecture-specific encoder variants are differentiated by the helper functions they call: `sub_A5A000` (SM70 Volta), `sub_A5AB30` (SM75 Turing), `sub_A59D80` (SM80 Ampere), etc.

### Per-Arch ISel Backends

Instruction selection is implemented as a linear-scan architecture: for each IR instruction, every pattern matcher is called in sequence, and the match with the highest priority wins. Each backend has its own set of pattern matchers, a mega-hub dispatch function (too large for Hex-Rays), and instruction emitters.

#### SM80 (Ampere) ISel Backend (0xCA0000 -- 0xDA0000, ~1 MB)

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0xCA0000`--`0xCDC000` | 240 KB | Operand emission + packing | 137 |
| `0xCDD5F0`--`0xCDD690` | <1 KB | Operand predicates | 15 |
| `0xCE2000`--`0xD5FD70` | 510 KB | ISel pattern matchers | 259 |
| **`0xD5FD70`** | **239 KB** | **SM80 ISel mega-hub** | **1** |
| `0xD9A400`--`0xDA0000` | 23 KB | Binary encoding | 17 |

Three-phase pipeline: (1) pattern match on IR attributes/operand types, (2) operand emission into instruction descriptor, (3) binary encoding into 128-bit SASS word.

#### SM100+ (Blackwell) SASS Codec -- Second Table (0xDA0000 -- 0xF16000, ~1.5 MB)

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0xDA0310`--`0xE436D0` | 669 KB | Blackwell encoders | 438 |
| `0xE43C20` | 1 KB | Encoder dispatch | 1 |
| `0xE43DC0`--`0xF15A50` | 847 KB | Blackwell decoders | 648 |
| `0xEFE6C0` | 1 KB | Decoder dispatch | 1 |

Format 1 instructions: 147. Format 2 (extended with modifiers): 290. Format 3 (special wide): 1. Every encoder has a mirror decoder; the decoder count exceeds encoders because decoders also handle architecture-variant forms.

#### SM75 (Turing) ISel Backend (0xF16000 -- 0x100C000, ~984 KB)

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0xF16030`--`0xF160F0` | <1 KB | Operand predicates | 15 |
| `0xF10080`--`0xF15A50` | 22 KB | Instruction emitters | 18 |
| `0xF16150`--`0xFBB780` | 678 KB | ISel pattern matchers | 276 |
| **`0xFBB810`** | **280 KB** | **SM75 ISel mega-hub** | **1** |
| `0xFFFDF0`--`0x100BBF0` | 48 KB | Post-ISel emit+encode | 38 |

This is the largest single-architecture ISel backend. `sub_FBB810` at 280 KB is the largest function in the binary.

#### SM89/90 (Ada/Hopper) Backend (0x100C000 -- 0x11EA000, ~1.9 MB)

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0x100C000`--`0x10FFFFF` | 1.0 MB | Shared instruction encoders | ~750 |
| `0x1100000`--`0x1120000` | 128 KB | **Backend driver** | ~30 |
| `0x1104950` | 38 KB | ptxas option parser | 1 |
| `0x1112F30` | 65 KB | Compilation driver main | 1 |
| `0x1116890` | 60 KB | ELF output + metadata gen | 1 |
| `0x1120000`--`0x119BF40` | 496 KB | ISel pattern matchers | ~160 |
| **`0x119BF40`** | **231 KB** | **SM89/90 ISel mega-hub** | **1** |
| `0x11D4680`--`0x11EA000` | 90 KB | Scheduler + emission | ~16 |

### PTX Frontend (0x11EA000 -- 0x15C0000, ~3.5 MB)

The PTX frontend parses PTX assembly text, validates instructions against SM version constraints, and lowers them to the internal IR consumed by the per-architecture ISel backends.

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x11EA000`--`0x126C000` | 520 KB | ISel pattern-match predicates | ~160 | Shared across all SM targets |
| **`0x126CA30`** | **239 KB** | **PTX ISel mega-hub** | **1** | Shared PTX-level instruction selector |
| `0x12A7000`--`0x12B0000` | 36 KB | PTX type system + operand builders | ~20 | Type constructors, operand IR building |
| `0x12B0000`--`0x12BA000` | 40 KB | Special register name table | ~20 | `%ntid`, `%laneid`, `%smid`, `%clock64`, `%ctaid`, ... |
| `0x12BA000`--`0x12D0000` | 88 KB | ISel lowering passes | ~30 | LTO-path instruction lowering |
| `0x12D0000`--`0x12D5000` | 20 KB | DWARF debug line info gen | ~5 | Line table emission for LTO-compiled code |
| `0x12D5000`--`0x1400000` | 1.2 MB | ISel pattern clones | ~500 | Parametric clones per SM (sm_5x through sm_10x) |
| `0x1400000`--`0x1430000` | 192 KB | LTO pipeline + ELF emit | ~20 | Top-level LTO pipeline, MMA lowering |
| `0x1430000`--`0x1442000` | 72 KB | PTX version/SM gates | ~30 | Version-gated instruction validators |
| `0x1442000`--`0x146BEC0` | 156 KB | Instruction emission handlers | ~80 | Per-instruction PTX code generators |
| `0x146BEC0` | 206 KB | `ptx_load_store_validator` | 1 | Memory operation validator with SM checks |
| `0x147EF50` | 288 KB | `ptx_instruction_semantic_analyzer` | 1 | Master validator: all SM version requirements |
| `0x1487650` | 240 KB | `ptx_statement_processor` | 1 | Top-level PTX statement handler |
| `0x14932E0`--`0x15B86A0` | 700 KB | Instruction handlers + builtins | ~250 | Code-template generators for CUDA builtins |
| `0x15B86A0` | 345 KB | `cuda_builtin_prototype_generator` | 1 | 608-case switch covering sm20 through sm10x builtins |

The `cuda_builtin_prototype_generator` is the second-largest function in the binary at 345 KB. It maps builtin index numbers to PTX prototype strings of the form `.weak .func (...) __cuda_smXX_foo (...)`. Function families include div, rem, rcp, sqrt, dsqrt, barrier, wmma, shfl, vote, matchsync, warpsync, reduxsync, sanitizer_memcheck, tcgen05, bulk_copy, and cp_async_bulk_tensor.

### Compilation Pipeline (0x15C0000 -- 0x1A00000, ~4.2 MB)

This region contains the per-function compilation pipeline from SM dispatch through code generation to backend verification.

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x15C0CE0` | 15 KB | **SM dispatch tables** | 1 | 7 callback maps for sm_75 through sm_121 |
| `0x15C44D0`--`0x15CA450` | 348 KB | nv.info attribute emitters | ~10 | Per-SM EIATTR record generation (largest: 78 KB) |
| `0x1610000`--`0x163FFFF` | 192 KB | PTX compilation frontend | ~40 | Operand handling, control flow, symbol management |
| `0x1640000`--`0x165FFFF` | 128 KB | Codegen operand lowering | ~30 | Atom formatting, offset calculation |
| `0x1660000`--`0x169FFFF` | 256 KB | ISel/scheduling + DWARF | ~40 | Instruction scheduling, peephole, debug emission |
| `0x16A0000`--`0x16DFFFF` | 256 KB | OCG intrinsic lowering | ~80 | `builtin_ocg_*` handlers, tcmma/tensor operations |
| `0x16E0000`--`0x16E3AB0` | 12 KB | tcgen05 intrinsic codegen | ~10 | SM100 tensor memory address setup, guardrails |
| `0x16E4D60`--`0x16F6000` | 70 KB | PTX instruction builder | ~20 | Instruction construction, operand insert |
| `0x16F6000`--`0x1740000` | 296 KB | **Tepid instruction scheduler** | ~50 | Full instruction scheduling pipeline |
| `0x175D000`--`0x1768000` | 44 KB | Knobs/config infrastructure | ~15 | Runtime tuning parameters |
| `0x1769000`--`0x1850000` | 924 KB | **SASS opcode tables** | ~150 | SM70-SM120 opcode encoding/emission with ROT13 mnemonics |
| `0x1850000`--`0x186F000` | 124 KB | **Instruction scheduling** | ~15 | `ScheduleInstructions` (85 KB), ReduceReg, DynBatch, Cutlass-aware |
| `0x1878000`--`0x189C000` | 144 KB | ConvertMemoryToRegister | ~20 | Shared-memory to register promotion |
| `0x189C000`--`0x18FC000` | 384 KB | **Register allocation** | ~40 | Spilling, SMEM spilling, multi-class regalloc |
| `0x18FC000`--`0x1920000` | 144 KB | setmaxnreg / CTA-reconfig | ~20 | Blackwell+ register budget negotiation |
| `0x1916000`--`0x1960000` | 296 KB | mbarrier + ORI passes | ~30 | Copy propagation, dead-code elimination |
| `0x1960000`--`0x19E0000` | 512 KB | **Codegen verification** | ~40 | Uninitialized register detection, remat verify |
| `0x19A0000`--`0x1A00000` | 384 KB | Metrics + scheduling guidance | ~35 | Occupancy estimation, loop analysis, regalloc guidance |

### SASS Emission (0x1A00000 -- 0x1D32172, ~3.2 MB)

The final segment of `.text` handles SASS instruction lowering, ABI enforcement, ELF/cubin output, name demangling, and DWARF debug info.

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x1A009C0`--`0x1A0B180` | 6 KB | Bug injection framework | ~5 | Testing hooks for intentional bug injection |
| `0x1A0B180`--`0x1A20000` | 84 KB | Instruction operand analysis | ~30 | Operand lowering, constant buffer encoding |
| `0x1A1A000`--`0x1A2A000` | 64 KB | Warp sync / mbarrier | ~15 | `%%mbarrier_%s_%s` instruction generation |
| `0x1A4B000`--`0x1A61090` | 88 KB | WGMMA pipeline analysis | ~20 | Warpgroup MMA live ranges, sync injection |
| `0x1A61090`--`0x1A6A480` | 38 KB | Scoreboard management | ~10 | Instruction scheduling scoreboard |
| `0x1A6A480`--`0x1AA2090` | 352 KB | ISel/lowering + encoding | ~80 | Instruction selection, SASS emission |
| `0x1AA2090`--`0x1ABF000` | 124 KB | Regalloc + ABI | ~30 | Register allocation, ABI handling |
| `0x1AEAA90`--`0x1AEE070` | 14 KB | Instruction vtable factory | ~10 | SASS instruction vtable construction |
| `0x1AEE070`--`0x1B00000` | 70 KB | Memory pool diagnostics | ~10 | Pool tracking, encoding passes |
| `0x1B00000`--`0x1B20000` | 128 KB | Register liveness | ~30 | Interference graph construction |
| `0x1B19750`--`0x1B40000` | 160 KB | Machine scheduling + CFG | ~40 | Basic block management |
| `0x1B40000`--`0x1B60000` | 128 KB | Dependency tracking | ~30 | Scoreboard / dependency graph |
| `0x1B60000`--`0x1B9FFFF` | 256 KB | ISel + lowering (tail) | ~200 | PTX-to-SASS ISel, tail-call optimization |
| `0x1BA0000`--`0x1BFFFFF` | 384 KB | **ABI / calling convention** | ~150 | Return address mgmt, convergent boundary, coroutine regs |
| `0x1C00000`--`0x1CDFFFF` | 896 KB | **ELF section builder** | ~120 | .nv.constant, .nv.shared, cubin/fatbin container |
| `0x1CE0000`--`0x1CEDFFF` | 56 KB | C++ name demangler | ~40 | Itanium ABI + MSVC demangler |
| `0x1CF0000`--`0x1D32172` | 265 KB | DWARF + LEB128 + KNOBS | ~140 | Debug info generation, SSE-accelerated LEB128, config system |

## The Five Mega-Hub Functions

Five functions exceed 160 KB each. They are the top-level instruction selector dispatch functions for different SM architecture generations. Each contains a massive jump table that calls hundreds of ISel pattern matchers in sequence, selects the highest-priority match, then dispatches to the corresponding emitter. All five are too large for Hex-Rays to decompile.

| Address | Size | Target | Description |
|---|---|---|---|
| `sub_FBB810` | 280 KB | SM75 (Turing) | Calls 276+ pattern matchers. Largest function in the binary |
| `sub_126CA30` | 239 KB | SM50-7x (shared) | Covers Maxwell/Pascal/Volta backends |
| `sub_D5FD70` | 239 KB | SM80 (Ampere) | Calls 259 pattern matchers for Ampere-class GPUs |
| `sub_119BF40` | 231 KB | SM89/90 (Ada/Hopper) | Calls ~160 pattern matchers |
| `sub_5B1D80` | 204 KB | SM50-7x (MercExpand) | MercExpand instruction expansion dispatch |

The ISel protocol is uniform across all backends:

```
for each pattern_matcher in pattern_table:
    matched = pattern_matcher(ctx, ir_node, &pattern_id, &priority)
    if matched && priority > best_priority:
        best_priority = priority
        best_id = pattern_id
emitter_table[best_id](ctx, ir_node)  // emit selected instruction
```

Each pattern matcher queries IR node attributes via `sub_A49150`, checks operand counts via `sub_530FD0`/`sub_530FC0`, retrieves operands via `sub_530FB0`, validates operand types and register classes, and writes `(pattern_id, priority)` if all constraints are satisfied.

## ROT13 Obfuscation of SASS Mnemonics

NVIDIA applies ROT13 encoding to SASS instruction mnemonic strings stored in the binary. The decoder function `sub_1A40AC0` uses SSE/SIMD vectorization for bulk ROT13 processing (loading 16 bytes at a time via `_mm_load_si128`). The SASS opcode table initializer at `0x1A85E40` stores all mnemonics in ROT13-encoded form; they are decoded at runtime before use.

Known decoded mnemonics:

| ROT13 | Decoded | Instruction |
|---|---|---|
| `VZNQ` | IMAD | Integer multiply-add |
| `SZHY` | FMUL | Float multiply |
| `SNQQ` | FADD | Float add |
| `SRAPR` | FENCE | Memory fence |
| `ZREPHEL` | MERCURY | Blackwell codename prefix |
| `CCGY` | CCTL | Cache control |
| `OFLAP` | BSYNC | Barrier synchronization |
| `ERZBAR` | REMOVE | Instruction removal tag |

The "MERCURY" prefix (`ZREPHEL` in ROT13) corresponds to sm_100+ (Blackwell) and appears throughout the compilation pipeline as a codename. ROT13 is also applied to some internal ELF section names: `.sync_restrict::shared::read::mma::a` is stored as its ROT13 equivalent, `.acc::f16` as `.npp::s16`, and `.sp::2to4` as `.fc::2gb4`.

## Size Summary

| Subsystem | Address Range | Size | Functions | % of .text |
|---|---|---|---|---|
| Linker core (not ptxas) | `0x400000`--`0x530000` | 1.2 MB | ~600 | 5% |
| IR primitives + SM50-7x ISel | `0x530000`--`0x620000` | 960 KB | ~1,450 | 4% |
| ISA encoding tables | `0x620000`--`0xA70000` | 4.3 MB | ~3,150 encoders + ~1,613 descriptors | 17% |
| Instruction codecs (multi-arch) | `0xA70000`--`0xCA0000` | 2.2 MB | ~700 | 9% |
| SM80 ISel backend | `0xCA0000`--`0xDA0000` | 1.0 MB | ~430 | 4% |
| SM100+ codec (second table) | `0xDA0000`--`0xF16000` | 1.5 MB | ~1,090 | 6% |
| SM75 ISel backend | `0xF16000`--`0x100C000` | 984 KB | ~350 | 4% |
| SM89/90 backend | `0x100C000`--`0x11EA000` | 1.9 MB | ~980 | 8% |
| PTX frontend | `0x11EA000`--`0x15C0000` | 3.5 MB | ~1,100 | 14% |
| Compilation pipeline | `0x15C0000`--`0x1A00000` | 4.2 MB | ~700 | 17% |
| SASS emission + ABI + ELF | `0x1A00000`--`0x1D32172` | 3.2 MB | ~1,300 | 13% |
| **Total embedded ptxas** | **`0x530000`--`0x1D32172`** | **~24 MB** | **~38,000** | **~95%** |

## Cross-Reference: Key Functions

| Function | Size | Identity | Role |
|---|---|---|---|
| `sub_530FB0` | <1 KB | `IRNode_GetOperand` | Universal operand accessor (31,399 callers) |
| `sub_A49150` | <1 KB | `IRInstr_GetAttribute` | Universal attribute accessor (30,768 callers) |
| `sub_4C28B0` | <1 KB | `setBitfield` | Core encoding primitive for all SASS encoders |
| `sub_1112F30` | 65 KB | `ptxas_main_compilation_driver` | Top-level per-module compilation entry |
| `sub_1103030` | 30 KB | `ptxas_option_definition_table_builder` | Full option parser (~60 options) |
| `sub_1104950` | 38 KB | `ptxas_command_option_parser` | Option processing and validation |
| `sub_15C0CE0` | 15 KB | `init_sm_dispatch_tables` | SM architecture callback registration |
| `sub_1A40AC0` | <1 KB | `rot13_string_decoder` | SIMD-vectorized ROT13 decoder |
| `sub_4BD760` | varies | `ptxas_jit_compile` | Entry point for PTX JIT compilation |
| `sub_4BC6F0` | varies | `compile_linked_lto_ir` | Entry point for LTO compilation |
| `sub_15B86A0` | 345 KB | `cuda_builtin_prototype_generator` | 608-case builtin switch (second-largest function) |
| `sub_147EF50` | 288 KB | `ptx_instruction_semantic_analyzer` | Master instruction validator |

## Compilation Pipeline: `sub_1112F30` Reconstructed

`sub_1112F30` (65,018 bytes at `0x1112F30`) is the top-level per-module compilation driver. It receives a module context `a1` and a PTX module descriptor `a2`, then orchestrates the full PTX-to-SASS compilation in a sequence of clearly delineated phases. The following ASCII pipeline diagram and reconstructed pseudocode are derived from the decompiled output of this function.

### Pipeline Diagram

```
 nvlink main / LTO pipeline
         |
         v
 +--------------------------+
 | sub_1112F30              |    per-module compilation driver (65 KB)
 | "ptxas_compile_module"   |
 +--------------------------+
         |
    Phase 1: Option Query & Cache Config
         |  query "def-load-cache", "force-load-cache",
         |  "def-store-cache", "force-store-cache"
         |  from option map at a1+904
         v
    Phase 2: Cancellation Check
         |  if a1+288 (cancel flag): call cancel_callback(a1+296)
         |  if returns 1 -> longjmp to error handler
         v
    Phase 3: Timing Gate
         |  check profiling flags a1+104..107, a1+402
         |  start wall-clock timer (sub_45CCD0)
         |  start high-res timer (sub_44EF30) if enabled
         v
    Phase 4: Callback Registration
         |  register per-instruction callback: sub_1108860 -> a1+408
         |  register per-function callback:    sub_1101EB0 -> a1+416
         |  initialize PTX version tables:     sub_12B30E0, sub_12B31D0
         v
    Phase 5: SM Version Validation
         |  sscanf .target string -> extract SM version number
         |  sscanf a1+576 (max supported SM) -> compare
         |  if module SM > max supported -> fatal error
         |  validate PTX version compatibility (sub_12A8360)
         v
    Phase 6: Mode Flag Dispatch (codegen callback selection)
         |  select (init_callback, begin_callback) pair:
         |
         |  if --compile-only OR --assyscall OR --compile-as-tools-patch:
         |      init = sub_110CD20   ("compile_only_init")
         |      begin = sub_11089E0  ("compile_only_begin")
         |
         |  elif --extensible-whole-program AND NOT --device-debug:
         |      init = sub_110D110   ("ewp_init")
         |      begin = sub_1107F10  ("ewp_begin")
         |
         |  elif --extensible-whole-program AND --device-debug:
         |      init = sub_110CD20
         |      begin = sub_11089E0
         |
         |  else (normal LTO / standard compilation):
         |      if --extensible-whole-program flag:
         |          init = sub_110CBA0  ("standard_init_ewp")
         |      else:
         |          init = sub_110D0B0  ("standard_init")
         |      begin = sub_1109180    ("standard_begin")
         v
    Phase 7: PTX Header Emission (dummy entry generation)
         |  if no explicit entry functions AND not tools-patch mode:
         |    if output-to-memory (sub_464740 returns true):
         |      sub_12AF550("__cuda_dummy_entry__", ptx_header, ...)
         |    else (output to file):
         |      fopen(output_path, "w")
         |      fprintf:  .version <ptx_version>
         |      fprintf:  .target  <sm_name>
         |      fprintf:  .entry __cuda_dummy_entry__ { ret; }
         |      fclose
         |      sub_12AF200(output_path, ...)
         v
    Phase 8: Tools-Patch Resource Allocation Warnings
         |  if --compile-as-tools-patch:
         |    warn if allocating: textures, surfaces, samplers, constants
         |  if --assyscall:
         |    warn if allocating: textures, surfaces, samplers
         v
    Phase 9: Compilation Flags Setup
         |  disable --fast-compile for ABI-less calls
         |  disable --extensible-whole-program for ABI-less compilation
         |  process --position-independent-code
         |  check texmode_independent vs texmode_unified
         |  check --preserve-relocs compatibility
         |  check --legacy-bar-warp-wide-behavior (SM70 only)
         |  check --g-tensor-memory-access-check (SM100+ only)
         v
    Phase 10: Hash Map + Codegen Context Allocation
         |  allocate 8 hash maps via sub_4489C0/sub_465020:
         |    [0] instruction map (cap 0x100)
         |    [1] function list (cap 0x400)
         |    [2] basic-block map (cap 0x100), conflict map (cap 0x40)
         |    [3] symbol map (cap 0x100), label map (cap 0x100)
         |    [4] directive map (cap 0x40), auxiliary map (cap 0x40)
         |    [5] register table (cap 0x20), operand table (arch-size)
         |  allocate per-function resource array:
         |    sub_12AE300(a2) entries x 48 bytes each -> a1+336
         |  allocate per-function compilation result array:
         |    count entries x 112 bytes each -> a1+256
         v
    Phase 11: Register Callbacks on Module IR
         |  sub_1102AC0 -> per-function callback on module functions list
         |  sub_1101E90 -> per-symbol callback on symbol table
         |  sub_1111DB0 -> per-function dispatch on func IR list
         |  sub_1101DE0 -> per-global callback (unless --compile-only)
         |  sub_110F5E0 -> per-section callback on section list
         |  sub_1101F60 -> per-symbol post-process callback
         v
    Phase 12: Address Width + Register Budget
         |  determine address width: 32-bit or 64-bit
         |    SM <= 13 -> 32-bit (maxnreg=32)
         |    SM > 13  -> read from module metadata
         |  PIC validation: if PIC address > threshold -> disable PIC
         |  maxrregcount validation: warn on mismatch
         |  32-bit register check: SM > 90 -> fatal
         v
    Phase 13: Entry Point Collection
         |  if explicit -e entries:
         |    resolve each name through module's symbol table
         |    build ordered entry list -> v322
         |  elif explicit -E entries:
         |    resolve through module, build list
         |  else:
         |    use module's own entry list (a2+88)
         v
    Phase 14: Transfer Compilation State to Context
         |  copy hash maps, flags, and tables into a1+1072..1296
         |  create alias tracking map (sub_4489C0, cap 0x100)
         |  create callee usage map (sub_4489C0, cap 0x418)
         v
    Phase 15: init_callback(a1, entry_list) -- Codegen Initialization
         |  calls the selected init callback (from Phase 6)
         |  sub_110CD20: builds per-function codegen descriptors
         |    for each function: sub_110BC90 -> allocate codegen record
         |    stores in a1+1192 (register usage map)
         |    returns ordered list of functions needing compilation
         v
    Phase 16: Load/Store Cache Mode Assignment
         |  for each function in compilation list:
         |    if force-load-cache -> set cache_mode = 2
         |    elif def-load-cache -> set cache_mode = 1
         |    else -> set cache_mode based on call graph analysis
         v
    Phase 17: Indirect Call + MMA Validation
         |  for each function:
         |    check for indirect calls with .f64 MMA -> warn
         |    check for mutual recursion markers -> flag
         v
    Phase 18: Scheduling Class Assignment
         |  for each function: assign scheduling class (0, 1, or 2)
         |    class 0 = no scheduling needed
         |    class 1 = standard scheduling
         |    class 2 = aggressive scheduling (callee analysis)
         |  propagate class upward through call graph if needed
         v
    Phase 19: Debug Info Setup
         |  if --device-debug: init DWARF context (sub_1672520)
         |  check .debug_abbrev / .debug_info availability
         v
    Phase 20: Reserved Register Configuration
         |  if --first-reserved-rreg: validate (min=4)
         |  compute total reserved = first_reserved + count
         v
    Phase 21: Build Per-Function Codegen Configuration
         |  pack ~50 compilation flags into struct at v334..v358:
         |    device_debug, lineinfo, fast_compile, maxrregcount,
         |    opt_level, compile_only, tools_patch, ewp, preserve_relocs,
         |    sm_version, address_width, default caches, PIC, ...
         |  call sub_16257C0 to create codegen pipeline config object
         v
    Phase 22: Output File Setup
         |  if --output specified: create/truncate output file
         v
  +------+------+
  |             |
  | thread_count == 0            thread_count > 0
  | (sequential)                 (parallel via thread pool)
  |             |
  v             v
    Phase 23a: Sequential          Phase 23b: Parallel
    Per-Function Loop              Per-Function Loop
         |                              |
    for each func:                 sub_43FDB0(thread_count)
      |                              = create thread pool
      |                              |
      +---> sub_110AA30            for each func:
      |     "codegen_init"           build work item (48 bytes):
      |     - create OCG context       [0..15] = timing state
      |     - set "NVIDIA"             [24] = per-func codegen ctx
      |     - set "ptxocg.0.0"         [32] = compilation driver ref
      |     - configure 30+ fields     [40] = optional sync state
      |     - set opt level            |
      |     - set SM-specific flags    sub_43FF50(pool, sub_1107420, item)
      |     - invoke vtable->init      = enqueue work item
      |       to map symbol names      |
      |                              sub_43FFE0(pool) = barrier wait
      +---> sub_1655A60             sub_43FE70(pool) = destroy pool
      |     "codegen_per_func"       |
      |     - initialize 48 pass     sub_1107420 (thread worker):
      |       enable/disable flags     sub_1102B30 -> setjmp + compile
      |       (passes 0..47)           record timing per function
      |     - register lowering        record peak memory
      |       callbacks on IR
      |     - set up UDT/UFT
      |       relocations
      |     - process entry
      |       function list
      |     - ISel dispatch
      |     - register allocation
      |
      +---> sub_1102B30
      |     "codegen_compile"
      |     - setjmp for error
      |       recovery
      |     - invoke compilation
      |       via vtable callback
      |       at a1+96
      |     - on error: longjmp,
      |       record failure
      |
      +---> timing measurement
      |     record compile time
      |     per-function stats
      |
      +---> sub_110D2A0
            "codegen_finalize"
            - emit ELF metadata
            - write nv.info records
            - output SASS binary
            - write register usage
            - cleanup per-func state
         |
         v
    Phase 24: Post-Compilation Cleanup
         |  clean up hash maps, free temp allocations
         |  validate register usage across functions
         |  if --compile-only: cross-check register budgets
         v
    Phase 25: Pipeline Config Teardown (sub_1626480)
         |
         v
    Phase 26: Final Cleanup
         |  destroy hash maps (sub_4650A0)
         |  free per-function resource array
         |  free codegen config
         |  return
```

### Reconstructed Pseudocode for `sub_1112F30`

```c
// sub_1112F30 -- per-module compilation driver
// Address: 0x1112F30, Size: 65,018 bytes
// Parameters:
//   a1 = compilation driver context (opaque struct, ~1300 bytes)
//   a2 = PTX module descriptor (parsed PTX representation)
// Returns: 0 on success

int ptxas_compile_module(CompilerCtx *ctx, PtxModule *mod) {

    // ---- Phase 1: Query cache configuration options ----
    bool def_load_cache   = option_get_bool(ctx->option_map, "def-load-cache");
    bool force_load_cache = option_get_bool(ctx->option_map, "force-load-cache");
    bool def_store_cache  = option_get_bool(ctx->option_map, "def-store-cache");
    bool force_store_cache= option_get_bool(ctx->option_map, "force-store-cache");

    // ---- Phase 2: Cancellation check ----
    if (ctx->cancel_flag) {
        if (ctx->cancel_callback(ctx->cancel_handle, ctx->cancel_arg) == 1)
            longjmp_to_error_handler();
    }

    // ---- Phase 3: Timing infrastructure ----
    timing_gate_start(ctx);   // sub_45CCD0 on a1+128/a1+144
    if (ctx->high_res_timing)
        ctx->wall_start = get_hires_time();  // sub_44EF30

    // ---- Phase 4: Register per-instruction and per-function callbacks ----
    list_foreach(ctx->instr_callbacks, per_instr_callback, ctx);   // sub_1108860
    list_foreach(ctx->func_callbacks, per_func_callback, ctx);     // sub_1101EB0
    ptx_version_table_init(mod);       // sub_12B30E0
    ptx_version_table_validate(mod);   // sub_12B31D0

    // ---- Phase 5: SM version validation ----
    unsigned sm_module, sm_max;
    sscanf(mod->target_string, "%*[^0-9]%d", &sm_module);
    sscanf(ctx->max_sm_string, "%*[^0-9]%d", &sm_max);
    if (sm_max < sm_module)
        fatal_error(ERR_SM_MISMATCH);

    if (mod->ptx_version_flag) {
        if (!ptx_version_compatible(sm_max, sm_module))  // sub_12A8360
            fatal_error(ERR_PTX_VERSION);
        if (!ctx->allow_unsupported_sm)
            fatal_error(ERR_UNSUPPORTED_SM, mod->target_string, ctx->max_sm_string);
    }

    // ---- Phase 6: Select codegen callback pair based on mode flags ----
    CodegenInitFn   init_fn;
    CodegenBeginFn  begin_fn;

    if (ctx->compile_only || ctx->assyscall || ctx->tools_patch) {
        init_fn  = compile_only_init;     // sub_110CD20
        begin_fn = compile_only_begin;    // sub_11089E0
    } else if (ctx->ewp_mode) {
        if (ctx->device_debug) {
            init_fn  = compile_only_init;     // sub_110CD20
            begin_fn = compile_only_begin;    // sub_11089E0
        } else {
            init_fn  = ewp_init;              // sub_110D110
            begin_fn = ewp_begin;             // sub_1107F10
        }
    } else {
        begin_fn = standard_begin;            // sub_1109180
        if (ctx->ewp_flag)
            init_fn = standard_init_ewp;      // sub_110CBA0
        else
            init_fn = standard_init;          // sub_110D0B0
    }

    // ---- Phase 7: Emit PTX dummy entry if no explicit entries ----
    if (!mod->entry_list && !ctx->assyscall && !ctx->tools_patch) {
        saved_flag = mod->flag_236;
        if (output_is_memory(ctx->func_callbacks)) {
            ptx_emit_entry_inline("__cuda_dummy_entry__", ptx_header_text, mod);
        } else {
            char *path = get_output_path(ctx->func_callbacks);
            FILE *fp = fopen(path, "w");
            if (mod->ptx_version_str)
                fprintf(fp, "\t.version %s\n", mod->ptx_version_str);
            if (mod->target_str)
                fprintf(fp, "\t.target  %s\n", mod->target_str);
            fprintf(fp, "\t.entry __cuda_dummy_entry__ { ret; }\n");
            fclose(fp);
            ptx_parse_file(path, mod);
        }
        mod->flag_236 = saved_flag;  // restore flag modified by parse
    }

    // ---- Phase 8-9: Compilation flags, warnings, compatibility ----
    if (ctx->tools_patch) {
        if (mod->alloc_textures)  warn("Allocating additional textures");
        if (mod->alloc_surfaces)  warn("Allocating additional surfaces");
        if (mod->alloc_samplers)  warn("Allocating additional samplers");
        if (mod->alloc_constants) warn("Allocating additional constants");
        ctx->fast_compile = 0;
    }

    ctx->abi_mode = 0;
    if (ctx->ewp_mode)  ctx->ewp_mode = 0;   // one-shot
    if (mod->has_entry_funcs || mod->has_extern_funcs) {
        if (ctx->fast_compile)
            warn("'--fast-compile' incompatible with calls without ABI");
        ctx->fast_compile = 0;
        if (ctx->ewp_flag)
            warn("'--extensible-whole-program' incompatible with compilation without ABI");
        ctx->ewp_flag = 0;
    }

    // PIC handling
    bool pic = option_get_bool(ctx->option_map, "position-independent-code");
    if (!ctx->compile_only && !ctx->device_debug && !ctx->tools_patch
        && !ctx->ewp_mode && !ctx->ewp_flag) {
        if (!mod->has_relo && !pic)
            ctx->enable_pic = 1;   // auto-enable PIC for normal compilation
    }
    if (ctx->ewp_flag && pic) {
        ctx->enable_pic = 0;
        warn("'--position-independent-code' incompatible with '--extensible-whole-program'");
    }

    if (ctx->early_exit)
        return 0;  // --dry-run equivalent

    // ---- Phase 10: Allocate codegen data structures ----
    HashMaps maps;
    maps.instr_map   = hashmap_create(cmp_fn, free_fn, 0x100);
    maps.func_list   = hashmap_create(cmp_fn, free_fn, 0x400);
    maps.bb_map      = hashmap_create(cmp_fn, free_fn, 0x100);
    maps.conflict    = hashmap_create(cmp_fn, free_fn, 0x40);
    maps.sym_map     = hashmap_create(cmp_fn, free_fn, 0x100);
    maps.label_map   = hashmap_create(cmp_fn, free_fn, 0x100);
    maps.dir_map     = hashmap_create(cmp_fn, free_fn, 0x40);
    maps.aux_map     = hashmap_create(cmp_fn, free_fn, 0x40);
    maps.reg_table   = sorted_map_create(int_cmp, free_fn, 0x20);
    maps.operand_tbl = sorted_map_create(int_cmp, free_fn, arch_operand_count(mod));

    unsigned func_count = get_function_count(mod);   // sub_12AE300
    ctx->per_func_resources = arena_alloc(func_count * 48);
    memset(ctx->per_func_resources, 0, func_count * 48);

    // ---- Phase 11: Traverse module IR, register callbacks ----
    list_foreach(mod->functions.list, register_function_cb, ctx);    // sub_1102AC0
    foreach_symbol(mod->symbol_table, register_symbol_cb, ctx);      // sub_1101E90
    list_foreach(mod->functions.list, dispatch_function_ir, &maps);  // sub_1111DB0
    if (!ctx->compile_only)
        list_foreach(mod->functions.globals, register_global_cb, &maps);
    list_foreach(mod->functions.sections, register_section_cb, &maps);
    foreach_symbol(mod->symbol_table, postprocess_symbol_cb, &maps);

    // ---- Phase 12: Address width + register budget ----
    int addr_width = determine_address_width(ctx, mod);
    ctx->addr_width = addr_width;  // 32 or 64
    validate_maxrregcount(ctx, mod);

    // ---- Phase 13: Collect entry points ----
    FuncList *entries;
    if (ctx->explicit_entries) {
        entries = resolve_entries(ctx->explicit_entries, ctx->module_reader);
    } else if (ctx->explicit_globals) {
        entries = resolve_entries(ctx->explicit_globals, ctx->module_reader);
    } else {
        entries = mod->entry_list;  // default: module's own entry list
    }

    // ---- Phase 14: Transfer state into codegen context ----
    ctx->maps         = maps;
    ctx->func_count   = list_count(entries);
    ctx->alias_map    = sorted_map_create(cmp, free, 0x100);
    ctx->callee_map   = sorted_map_create(cmp, free, 0x418);
    // ... copy ~30 more fields ...

    // ---- Phase 15: Call init_callback to prepare codegen descriptors ----
    FuncList *compile_list = begin_fn(ctx, entries, ctx->per_func_resources);

    // ---- Phase 16: Load/store cache mode per function ----
    for (FuncNode *f = compile_list; f; f = f->next) {
        FuncDesc *desc = f->data;
        int func_idx = desc->func_ir->header->index;
        bool needs_caching = per_func_resources[func_idx].has_cached_callees;

        if (sm_dispatch->supports_caching(sm_class)) {
            if (force_load_cache)
                desc->cache_mode = 2;  // force all loads cached
            else if (def_load_cache)
                desc->cache_mode = 1;  // default cached
            else if (needs_caching || force_store_cache)
                desc->cache_mode = 2;
            else
                desc->cache_mode = (def_store_cache != 0);
        } else {
            desc->cache_mode = 2 * (force_load_cache || def_load_cache);
        }
    }

    // ---- Phase 17: Validate indirect calls + MMA .f64 ----
    for (FuncNode *f = compile_list; f; f = f->next) {
        FuncDesc *desc = f->data;
        if (desc->mma_info && desc->mma_info->has_f64) {
            warn_once(ERR_MMA_F64, desc->name, "mma with .f64 type");
        }
        if (desc->has_mutual_recursion)
            fatal_error(ERR_MUTUAL_RECURSION, desc->entry_name);
    }

    // ---- Phase 18: Assign scheduling class ----
    for (FuncNode *f = compile_list; f; f = f->next) {
        FuncDesc *desc = f->data;
        if (!desc->needs_scheduling) {
            desc->sched_class = 0;
        } else {
            // analyze call graph for callee scheduling requirements
            int callee_sched_count = count_callees_with_sched_class_2(desc);
            if (ctx->force_aggressive_sched && callee_sched_count > 0) {
                desc->sched_class = 2;
            } else {
                desc->sched_class = sm_dispatch->determine_sched_class(desc);
            }
        }
    }

    // ---- Phase 19: Debug info + Phase 20: Reserved regs ----
    if (ctx->device_debug)
        dwarf_init(ctx->dwarf_ctx, mod);
    int reserved_rreg = -1;
    if (ctx->has_reserved_rreg) {
        if (!ctx->first_reserved_rreg)
            ctx->first_reserved_rreg = 4;  // minimum
        reserved_rreg = ctx->first_reserved_rreg + ctx->reserved_count;
    }

    // ---- Phase 21: Build codegen config struct ----
    CodegenConfig cfg;
    cfg.module         = mod;
    cfg.module_reader  = ctx->module_reader;
    cfg.device_debug   = ctx->device_debug;
    cfg.lineinfo       = ctx->lineinfo;
    cfg.opt_level      = ctx->opt_level;
    // ... pack ~50 flags from ctx into cfg ...
    CodegenPipeline *pipeline = create_codegen_pipeline(cfg);  // sub_16257C0

    // ---- Phase 22: Create output file if needed ----
    if (ctx->output_path && (ctx->dump_sass || ctx->dump_ptx))
        fopen_and_truncate(ctx->output_path, "wt");

    // ---- Phase 23: Per-function compilation ----
    unsigned total_funcs = list_count(compile_list);
    ctx->result_array = arena_alloc(total_funcs * 112);
    memset(ctx->result_array, 0, total_funcs * 112);

    if (ctx->thread_count == 0) {
        // ---- Phase 23a: SEQUENTIAL per-function compilation ----
        for (FuncNode *f = compile_list; f; f = f->next) {
            FuncDesc *desc = f->data;
            unsigned idx = desc->func_index;
            char *name = desc->func_ir->name;

            ctx->result_array[idx].name = name;
            ctx->result_array[idx].start_time = timer_read(ctx->timer);

            // Allocate 360-byte per-function codegen state
            PerFuncState *pfs = arena_alloc_zeroed(360);

            // Phase 23a-i: Initialize codegen context for this function
            codegen_init(ctx, mod, desc, pipeline, pfs);  // sub_110AA30:
            //   - create OCG (Optimizing Code Generator) context
            //   - set producer = "NVIDIA", tool = "ptxocg.0.0"
            //   - configure SM-specific codegen flags
            //   - set optimization level, maxrregcount, address width
            //   - initialize instruction vtable from SM dispatch table
            //   - set up DWARF state if debug enabled
            //   - call vtable->init to resolve symbol names

            // Phase 23a-ii: Invoke the compilation pipeline
            // sub_1655A60 (called from within codegen_init flow):
            //   - initialize 48 pass-enable flags (passes 0..47)
            //   - register IR lowering callbacks
            //   - set up UDT/UFT relocations for Blackwell+
            //   - for each pass in sequence:
            //       pass 1:  IR canonicalization
            //       pass 2:  instruction count estimation
            //       pass 3-22: SM-gated optimization passes
            //           (each enabled/disabled by SM dispatch vtable)
            //       pass 21: address width query
            //       pass 22: register class initialization
            //       pass 23-38: ISel, regalloc, scheduling (SM-gated)
            //       pass 39: ABI frame setup
            //       pass 40-42: final lowering
            //       pass 43: peephole cleanup
            //       pass 46: binary encoding query
            //       pass 47: final verification
            //   - emit SASS instructions via ISel mega-hub
            //   - perform register allocation (graph coloring)
            //   - schedule instructions (ScheduleInstructions, 85 KB)
            //   - run peephole optimizations
            //   - encode final SASS binary (128-bit words)

            // Phase 23a-iii: Error-wrapped compile invocation
            codegen_compile(ctx, desc, pfs);  // sub_1102B30:
            //   - setjmp for error recovery
            //   - call vtable->compile(ctx, func_ir, ctx->codegen_config)
            //   - on error: longjmp, free resources, report failure

            // Phase 23a-iv: Record timing
            timing_record(ctx, desc, pfs);

            // Phase 23a-v: Finalize and emit
            codegen_finalize(ctx, mod, desc, pfs);  // sub_110D2A0:
            //   - emit ELF section content (.text, .nv.info, .nv.constant)
            //   - write register usage records (EIATTR)
            //   - write SASS binary to output
            //   - handle PTX re-emission if --output-ptx
            //   - cleanup per-function OCG state

            arena_free(pfs);

            // cancellation check between functions
            if (ctx->cancel_flag && cancel_callback() == 1)
                longjmp_to_error_handler();
        }
    } else {
        // ---- Phase 23b: PARALLEL per-function compilation ----
        ThreadPool *pool = create_thread_pool(ctx->thread_count);  // sub_43FDB0
        IndexArray *sync = index_array_create(total_funcs);

        for (FuncNode *f = compile_list; f; f = f->next) {
            FuncDesc *desc = f->data;

            // Allocate extended per-function state (360 bytes + 3 maps)
            PerFuncState *pfs = arena_alloc_zeroed(360);
            pfs->local_map_a = sorted_map_create(cmp, free, 8);
            pfs->local_map_b = sorted_map_create(cmp, free, 8);
            pfs->local_map_c = sorted_map_create(cmp, free, 8);
            pfs->pipeline = pipeline;
            pfs->driver_ctx = ctx;
            pfs->module = mod;
            pfs->func_desc = desc;

            // Initialize codegen (same as sequential)
            codegen_init(ctx, mod, desc, pipeline, pfs);

            // Copy 15 x 16-byte blocks of driver state into pfs
            // (compiler flags, maps, timing state)
            memcpy(&pfs->snapshot, &ctx->compilation_state, 15 * 16);

            // Allocate per-function DWARF state (216 bytes)
            pfs->dwarf_state = arena_alloc_zeroed(216);
            dwarf_register(ctx->dwarf_ctx, pfs->dwarf_state);

            // Snapshot pipeline state
            pipeline_snapshot(pipeline, &pfs->pipeline_state);

            // Copy function-local maps from shared -> per-thread
            copy_maps(pfs->local_map_a, ctx->shared_maps);

            // Enqueue work item
            index_array_push(pfs, sync);
        }

        // Wait for all per-function compilations to complete
        // Each thread runs sub_1107420:
        //   1. sub_1102B30 -- setjmp + vtable->compile()
        //   2. record timing (wall-clock + per-function)
        //   3. record peak memory usage
        //   4. free per-function OCG state
        for_each(compile_list, sync, dispatch_work_item, pool);
        thread_pool_barrier(pool);   // sub_43FFE0
        thread_pool_destroy(pool);   // sub_43FE70

        // Single-threaded finalization pass
        report_function_index(ctx, -1);  // sub_1107720
        if (ctx->cancel_flag && cancel_callback() == 1)
            cleanup_and_longjmp();

        // ---- Sequential post-compilation merge ----
        bool first = true;
        for (FuncNode *f = compile_list; f; f = f->next) {
            PerFuncState *pfs = sync_get(sync, index);

            // Restore driver state from per-function snapshot
            memcpy(&ctx->compilation_state, &pfs->snapshot, 15 * 16);

            // Replay pipeline state
            pipeline_restore(pipeline, pfs->pipeline_state);

            // Merge per-thread maps back into shared maps
            merge_maps(ctx->shared_maps, pfs->local_map_a);
            merge_maps(ctx->shared_maps, pfs->local_map_b);
            merge_maps(ctx->shared_maps, pfs->local_map_c);

            // Restore DWARF state
            dwarf_merge(ctx->dwarf_ctx, pfs->dwarf_state);

            // Finalize this function
            codegen_finalize(ctx, mod, desc, pfs);

            // Track first-function state for register budget
            if (first) {
                shared_register_budget = ctx->register_budget;
                first = false;
            }
            ctx->register_budget = shared_register_budget;

            // Cleanup
            cleanup_per_func_maps(pfs);
            arena_free(pfs);
        }
        index_array_destroy(sync);
    }

    // ---- Phase 24: Post-compilation validation ----
    pipeline_finalize(pipeline);  // sub_1626480
    if (ctx->compile_only && ctx->register_budget_map) {
        // Cross-validate register usage: each callee must not exceed
        // the register budget of its caller
        for (int i = 0; i < func_count; i++) {
            ResourceEntry *re = &ctx->per_func_resources[i];
            if (re->entry_name && re->callee_list) {
                unsigned caller_budget = map_lookup(ctx->register_budget_map,
                                                    re->entry_name);
                for (FuncNode *c = re->callee_list; c; c = c->next) {
                    unsigned callee_regs = map_lookup(ctx->alias_map, c->name);
                    if (caller_budget > callee_regs)
                        warn("register budget exceeded: %s uses %d, caller %s allows %d",
                             c->name, callee_regs, re->entry_name, caller_budget);
                }
            }
        }
        map_destroy(ctx->register_budget_map);
    }

    // ---- Phase 25-26: Cleanup ----
    hashmap_destroy(maps.bb_map);
    hashmap_destroy(maps.sym_map);
    arena_free(ctx->per_func_resources);
    if (ctx->tensor_check_map)
        set_destroy(ctx->tensor_check_map);

    return 0;
}
```

### Key Subroutine Reference

| Address | Name (reconstructed) | Role in Pipeline |
|---|---|---|
| `sub_1112F30` | `ptxas_compile_module` | Top-level per-module driver (this function) |
| `sub_110AA30` | `codegen_init` | Per-function OCG context creation + field setup |
| `sub_1655A60` | `codegen_per_func` | 48-pass codegen pipeline (ISel, regalloc, sched, encode) |
| `sub_1102B30` | `codegen_compile` | Error-wrapped compilation (setjmp + vtable dispatch) |
| `sub_110D2A0` | `codegen_finalize` | ELF emission, nv.info, SASS output, cleanup |
| `sub_1107420` | `thread_worker` | Thread pool work item: compile + timing + cleanup |
| `sub_110CD20` | `compile_only_init` | Init for `--compile-only` / `--compile-as-tools-patch` |
| `sub_110D0B0` | `standard_init` | Init for normal LTO compilation |
| `sub_110D110` | `ewp_init` | Init for `--extensible-whole-program` mode |
| `sub_110CBA0` | `standard_init_ewp` | Init for standard mode with EWP flag |
| `sub_11089E0` | `compile_only_begin` | Begin callback for compile-only modes |
| `sub_1107F10` | `ewp_begin` | Begin callback for EWP mode |
| `sub_1109180` | `standard_begin` | Begin callback for standard compilation |
| `sub_110BC90` | `alloc_codegen_record` | Allocate per-function codegen descriptor |
| `sub_16257C0` | `create_codegen_pipeline` | Build the codegen pipeline configuration object |
| `sub_1626480` | `pipeline_finalize` | Tear down the codegen pipeline |
| `sub_43FDB0` | `create_thread_pool` | Create split-compilation thread pool |
| `sub_43FF50` | `enqueue_work_item` | Submit per-function work to thread pool |
| `sub_43FFE0` | `thread_pool_barrier` | Wait for all enqueued work to complete |
| `sub_43FE70` | `thread_pool_destroy` | Destroy thread pool |
| `sub_12AE300` | `get_function_count` | Return number of functions in module |
| `sub_12AF550` | `ptx_emit_entry_inline` | Emit PTX entry point to in-memory buffer |
| `sub_12AF200` | `ptx_parse_file` | Parse a PTX file into module representation |
| `sub_12B30E0` | `ptx_version_table_init` | Initialize PTX version compatibility tables |
| `sub_12A8360` | `ptx_version_compatible` | Check PTX/SM version compatibility |
| `sub_15C3DD0` | `sm_name_to_ordinal` | Convert SM target string to ordinal index |
| `sub_1672520` | `dwarf_init` | Initialize DWARF debug info context |

### Compilation Mode Matrix

The mode flag dispatch at Phase 6 selects one of four codegen pathways. The choice is determined by command-line flags forwarded into the embedded compiler:

| Mode | Condition | init_fn | begin_fn | Behavior |
|---|---|---|---|---|
| **Compile-only** | `--compile-only` or `--compile-as-tools-patch` or `--assyscall` | `sub_110CD20` | `sub_11089E0` | Compile all functions independently. No cross-function optimization. Used for tools patches (Nsight Compute, Nsight Systems) |
| **EWP (no debug)** | `--extensible-whole-program` and NOT `--device-debug` | `sub_110D110` | `sub_1107F10` | Whole-program optimization. Functions compiled with global visibility into callee register usage. Enables aggressive inlining decisions |
| **EWP + debug** | `--extensible-whole-program` AND `--device-debug` | `sub_110CD20` | `sub_11089E0` | Falls back to compile-only pathway because whole-program optimization conflicts with debug info fidelity |
| **Standard** | Normal LTO compilation (default) | `sub_110D0B0` | `sub_1109180` | Standard per-function compilation with cross-function register budget propagation. Used for typical nvlink LTO |
| **Standard + EWP flag** | Standard with `--extensible-whole-program` hint | `sub_110CBA0` | `sub_1109180` | Same as standard but with EWP-aware init (reserves additional register space for potential future extensibility) |

### The 48-Pass Codegen Pipeline (`sub_1655A60`)

The per-function codegen entry point `sub_1655A60` runs a 48-pass pipeline (passes numbered 0--47). Each pass is enabled or disabled by querying the SM dispatch vtable at `a1[3757]` (the architecture-specific callback table registered by `sub_15C0CE0`). The pipeline initializes 48 boolean flags in `a1[160..207]` and then iterates:

```
Pass  0:   Zero (placeholder)
Pass  1:   Initial IR canonicalization
Pass  2:   Instruction count estimation (query vtable+120)
Pass  3-20: SM-gated optimization passes
             Each pass queries vtable+72 for SM capability.
             If SM supports the pass, the pass enable flag is set to 1.
             If not supported, the flag remains 0.
             Pass 21: address-width-dependent setup
             Pass 22: register class initialization
Pass 23-38: Core backend passes (ISel, register allocation, scheduling)
             These are universally enabled for all SM >= sm_50.
             Passes 23-38 include:
               - Instruction selection (ISel mega-hub dispatch)
               - Register allocation (graph coloring + spilling)
               - Instruction scheduling (ScheduleInstructions)
               - Peephole optimization
               - SASS encoding
Pass 39:   Initial ABI frame setup
Pass 40-42: Final lowering passes
Pass 43:   Peephole cleanup
Pass 44-45: Reserved
Pass 46:   Binary encoding query (vtable+488)
Pass 47:   Final verification + pass-count teardown
```

After the pass loop, `sub_1655A60` registers additional IR lowering callbacks (`sub_161F1C0`, `sub_161F800`, `sub_1620460`) on the function's basic block list, sets up UDT/UFT relocations for Blackwell+ (SM > 26 in ordinal = sm_100+), and processes the function's call graph for register pressure analysis.

### Sequential vs. Parallel Compilation

The compilation driver supports two execution models, selected by `ctx->thread_count` (offset `a1+668`):

**Sequential mode** (`thread_count == 0`): Each function is compiled in the main thread with a simple loop: `codegen_init` -> `codegen_compile` (error-wrapped) -> `codegen_finalize`. Timing is recorded between stages. The cancel callback is checked between functions.

**Parallel mode** (`thread_count > 0`): A thread pool is created via `sub_43FDB0`. For each function, a work item containing the full per-function state snapshot (360 bytes + 3 local hash maps + pipeline snapshot) is allocated and enqueued. Each thread executes `sub_1107420`, which calls `sub_1102B30` (the error-wrapped compile) and records timing. After the barrier wait (`sub_43FFE0`), the main thread performs a sequential merge pass: it restores each function's snapshot, merges local maps back into shared maps, and calls `codegen_finalize`. This is the same `--split-compile-extended` mechanism available in standalone ptxas.

The thread pool implementation uses a producer-consumer work queue. `sub_43FF50` enqueues items, and worker threads dequeue and execute them. The barrier at `sub_43FFE0` blocks until all items complete. The pool is destroyed by `sub_43FE70`. If an optional synchronization state (`qword_2A64430`) is non-null, each worker checks for compilation errors after completing its work item via `sub_1D1E060` / `sub_1D1E300`.

## Cross-References

### nvlink Internal
- [IR Nodes](ir-nodes.md) -- IR node structure and universal accessor functions
- [ISel Hubs](isel-hubs.md) -- the five mega-hub instruction selector dispatch functions
- [Peephole](peephole.md) -- peephole optimization passes (ORI, scheduling-phase, linker-level)
- [PTX Parsing](ptx-parsing.md) -- the embedded PTX assembler frontend
- [Register Allocation](register-allocation.md) -- graph-coloring register allocator with spilling
- [Scheduling](scheduling.md) -- pre-RA and tepid (post-RA) instruction schedulers
- [Architecture Dispatch](arch-dispatch.md) -- per-SM vtable dispatch system
- [Mercury Overview](../mercury/overview.md) -- Mercury ISA encoding pipeline
- [FNLZR](../mercury/fnlzr.md) -- post-link binary rewriter for Mercury targets
- [LTO Overview](../lto/overview.md) -- how the LTO pipeline invokes the embedded compiler

### Sibling Wikis
- [ptxas: Pipeline Overview](../../ptxas/pipeline/overview.html) -- standalone ptxas 159-phase compilation pipeline
- [ptxas: Entry Point](../../ptxas/pipeline/entry.html) -- standalone ptxas main() and option parsing
- [ptxas: Optimizer](../../ptxas/pipeline/optimizer.html) -- standalone ptxas optimization passes
- [ptxas: Codegen Overview](../../ptxas/codegen/overview.html) -- standalone ptxas code generation
