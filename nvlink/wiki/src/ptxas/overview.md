# Embedded ptxas: Architecture Overview

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
