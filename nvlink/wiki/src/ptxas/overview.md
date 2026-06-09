# Embedded ptxas: Architecture Overview

> **Note**: This page is the entry point to the nvlink-internal documentation of the ptxas backend statically linked into nvlink v13.0.88. The address map, mega-hub layout, ROT13 details, and `sub_1112F30` per-module compilation driver below are recovered from nvlink's binary — not from the standalone ptxas binary. For the standalone ptxas reverse-engineering reference (159-phase pipeline, full Ori IR architecture, target catalog), see the [ptxas wiki](../../ptxas/index.html) — in particular [Pipeline Overview](../../ptxas/pipeline/overview.html), [Codegen Overview](../../ptxas/codegen/overview.html), and [Targets](../../ptxas/targets/index.html).

The single most important structural fact about nvlink v13.0.88 is that approximately 95% of its 25.2 MB `.text` section is not linker code — it is a complete, statically embedded copy of the ptxas assembler/compiler backend. The actual device linker (ELF merge, symbol resolution, relocation, layout, output) occupies roughly 1.2 MB in the address range `0x400000`--`0x530000`. Everything from `0x530000` through the end of `.text` at `0x1D32172` (~24 MB, ~38,000 functions) is the ptxas compiler backend: IR primitives, instruction selection, register allocation, instruction scheduling, SASS binary encoding, PTX parsing, and ELF/cubin output generation.

This page documents the evidence for this claim, the complete address map of the embedded ptxas subsystems, the five mega-hub instruction selector dispatch functions, and the ROT13 obfuscation applied to SASS mnemonics.

## Evidence for Embedded ptxas

The embedded compiler is not a stripped-down stub — it is a full-featured PTX-to-SASS compilation pipeline identical in capability to the standalone `ptxas` binary shipped in the CUDA toolkit. Key evidence:

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

### IR Primitives (0x530000 — 0x620000, ~960 KB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x530E80`--`0x530FD0` | <1 KB | **IR node accessors** | 22 | `sub_530FB0` has 31,399 callers — universal `getOperand(idx)` |
| `0x530FE0`--`0x5B1AB0` | 523 KB | ISel pattern matchers (SM50-7x) | 1,293 | 152 target opcodes, 36 priority levels |
| `0x5B1D80`--`0x5E4470` | 204 KB | **MercExpand mega-hub** | 1 | MercExpand dispatch + CFG analysis (too large for Hex-Rays) |
| `0x5E4470`--`0x600260` | 114 KB | MercExpand engine | ~50 | Bitvector ops, FNV-1a hash maps, register constraint propagation |
| `0x603F60`--`0x61FA60` | 112 KB | SM50 instruction encoders | 79 | Per-instruction binary encoding functions |

The IR node structure is accessed through 22 leaf functions that constitute the most-called code in the entire binary. `sub_530FB0` (get operand by index) at 31,399 callers and `sub_A49150` (get instruction attribute) at 30,768 callers form the universal accessor layer. The IR node layout:

```text
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

### ISA Encoding Tables (0x620000 — 0xA70000, ~4.3 MB)

This is the largest contiguous subsystem — 4.3 MB of template-instantiated functions defining the complete NVIDIA GPU instruction set encoding and metadata.

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

### Instruction Codecs (0xA70000 — 0xCA0000, ~2.2 MB)

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

#### SM80 (Ampere) ISel Backend (0xCA0000 — 0xDA0000, ~1 MB)

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0xCA0000`--`0xCDC000` | 240 KB | Operand emission + packing | 137 |
| `0xCDD5F0`--`0xCDD690` | <1 KB | Operand predicates | 15 |
| `0xCE2000`--`0xD5FD70` | 510 KB | ISel pattern matchers | 259 |
| **`0xD5FD70`** | **239 KB** | **SM80 ISel mega-hub** | **1** |
| `0xD9A400`--`0xDA0000` | 23 KB | Binary encoding | 17 |

Three-phase pipeline: (1) pattern match on IR attributes/operand types, (2) operand emission into instruction descriptor, (3) binary encoding into 128-bit SASS word.

#### SM100+ (Blackwell) SASS Codec — Second Table (0xDA0000 — 0xF16000, ~1.5 MB)

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0xDA0310`--`0xE436D0` | 669 KB | Blackwell encoders | 438 |
| `0xE43C20` | 1 KB | Encoder dispatch | 1 |
| `0xE43DC0`--`0xF15A50` | 847 KB | Blackwell decoders | 648 |
| `0xEFE6C0` | 1 KB | Decoder dispatch | 1 |

Format 1 instructions: 147. Format 2 (extended with modifiers): 290. Format 3 (special wide): 1. Every encoder has a mirror decoder; the decoder count exceeds encoders because decoders also handle architecture-variant forms.

#### SM75 (Turing) ISel Backend (0xF16000 — 0x100C000, ~984 KB)

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0xF16030`--`0xF160F0` | <1 KB | Operand predicates | 15 |
| `0xF10080`--`0xF15A50` | 22 KB | Instruction emitters | 18 |
| `0xF16150`--`0xFBB780` | 678 KB | ISel pattern matchers | 276 |
| **`0xFBB810`** | **280 KB** | **SM75 ISel mega-hub** | **1** |
| `0xFFFDF0`--`0x100BBF0` | 48 KB | Post-ISel emit+encode | 38 |

This is the largest single-architecture ISel backend. `sub_FBB810` at 280 KB is the largest function in the binary.

#### SM89/90 (Ada/Hopper) Backend (0x100C000 — 0x11EA000, ~1.9 MB)

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

### PTX Frontend (0x11EA000 — 0x15C0000, ~3.5 MB)

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

### Compilation Pipeline (0x15C0000 — 0x1A00000, ~4.2 MB)

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

### SASS Emission (0x1A00000 — 0x1D32172, ~3.2 MB)

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

The ISel protocol is the standard ptxas linear-scan pattern matcher: every matcher is invoked with `(ctx, ir_node, &pattern_id, &priority)`, the highest-priority match wins, the emitter table dispatches by `pattern_id`. Matchers query IR through `sub_A49150` (attribute), `sub_530FD0`/`sub_530FC0` (operand count), and `sub_530FB0` (operand by index). For the algorithm in full detail see [ptxas: Instruction Selection](../../ptxas/codegen/isel.html); the table above lists the nvlink-binary addresses of the five mega-hub dispatch functions that implement it.

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
| `sub_1A40AC0` | 1.9 KB | `rot13_string_decoder` | SIMD-vectorized ROT13 decoder |
| `sub_4BD760` | varies | `ptxas_jit_compile` | Entry point for PTX JIT compilation |
| `sub_4BC6F0` | varies | `compile_linked_lto_ir` | Entry point for LTO compilation |
| `sub_15B86A0` | 345 KB | `cuda_builtin_prototype_generator` | 608-case builtin switch (second-largest function) |
| `sub_147EF50` | 288 KB | `ptx_instruction_semantic_analyzer` | Master instruction validator |

## Compilation Pipeline: `sub_1112F30`

`sub_1112F30` (65,018 bytes at `0x1112F30`, ~2,164 decompiled lines) is the top-level per-module compilation driver inside nvlink's embedded ptxas. It receives a module context `a1` and a PTX module descriptor `a2`, then orchestrates the full PTX-to-SASS compilation across 26 phases before returning. Confidence: **HIGH** — derived directly from Hex-Rays output of this function.

This driver corresponds structurally to the entry/dispatch path in standalone ptxas — see [ptxas: Pipeline Overview](../../ptxas/pipeline/overview.html) for the generic 159-phase pipeline narrative and [ptxas: Entry Point](../../ptxas/pipeline/entry.html) for option-parser behavior. The table and notes below preserve the binary-specific phase order, callback choices, and helper addresses that are unique to the nvlink-embedded copy.

### Phase Table

| # | Phase | Key calls / addresses | Effect |
|---|---|---|---|
| 1 | Option query & cache config | `option_get_bool` on `def-load-cache`, `force-load-cache`, `def-store-cache`, `force-store-cache` | Captures cache-mode booleans into stack locals |
| 2 | Cancellation check | reads `a1+288`; invokes `cancel_callback(a1+296)` | Longjmps to error handler if returns 1 |
| 3 | Timing gate | `sub_45CCD0` wall-clock, `sub_44EF30` high-res; flags at `a1+104..107`, `a1+402` | Starts timers if profiling enabled |
| 4 | Callback registration | `sub_1108860` instr CB, `sub_1101EB0` func CB, `sub_12B30E0`/`sub_12B31D0` PTX version tables | Installs per-IR-node callbacks |
| 5 | SM version validation | `sscanf` on `.target`; `sub_12A8360` PTX/SM compat | Fatal if module SM > max supported |
| 6 | Mode flag dispatch | selects `(init_fn, begin_fn)` — see [Compilation Mode Matrix](#compilation-mode-matrix) | Picks one of four codegen pathways |
| 7 | PTX header emission | `sub_12AF550` inline / `fopen` + `fprintf(.version/.target/.entry __cuda_dummy_entry__ { ret; })` + `sub_12AF200` | Emits dummy entry when none exist |
| 8 | Tools-patch warnings | conditional on `--compile-as-tools-patch`, `--assyscall` | Warns about allocating textures/surfaces/samplers/constants |
| 9 | Compilation flags setup | PIC processing; `--fast-compile`/`--extensible-whole-program` disables for ABI-less; `--legacy-bar-warp-wide-behavior` (SM70 only); `--g-tensor-memory-access-check` (SM100+ only) | Resolves flag conflicts |
| 10 | Hash maps + codegen context | 8x `sub_4489C0`/`sub_465020` (caps 0x100/0x400/0x40/0x20); per-func resource array via `sub_12AE300` (48 B/entry at `a1+336`); result array (112 B/entry at `a1+256`) | Allocates module-wide tables |
| 11 | Register callbacks on module IR | `sub_1102AC0` per-function, `sub_1101E90` per-symbol, `sub_1111DB0` per-func-IR, `sub_1101DE0` per-global (unless `--compile-only`), `sub_110F5E0` per-section, `sub_1101F60` per-symbol post-process | Installs IR walkers |
| 12 | Address width + register budget | SM≤13 → 32-bit (maxnreg=32); SM>13 → from module metadata; SM>90 + 32-bit → fatal | Sets address mode |
| 13 | Entry point collection | resolves `-e` / `-E` names through module reader; else uses `a2+88` | Builds ordered entry list |
| 14 | Transfer state into codegen context | copies maps/flags into `a1+1072..1296`; alias map (cap 0x100); callee usage map (cap 0x418) | Snapshots compilation state |
| 15 | `init_callback(ctx, entries)` | from Phase 6: `sub_110CD20` / `sub_110CBA0` / `sub_110D0B0` / `sub_110D110` | Builds per-function codegen descriptors via `sub_110BC90`, stores at `a1+1192` |
| 16 | Load/store cache mode | per-function: respects `force-load-cache` (mode=2), `def-load-cache` (mode=1), `force-store-cache`, callee analysis | Assigns memory-op cache mode |
| 17 | Indirect call + MMA validation | per-function: warns on indirect `mma.f64`; fatal on mutual recursion markers | Frontend correctness check |
| 18 | Scheduling class assignment | class 0 / 1 / 2 propagated through call graph; class 2 = aggressive (callee analysis) | Picks scheduling aggressiveness |
| 19 | Debug info setup | `sub_1672520` `dwarf_init` if `--device-debug` | Initializes DWARF context |
| 20 | Reserved register configuration | `--first-reserved-rreg` (min=4); total = first + count | Reserves R-regs from regalloc |
| 21 | Build per-function codegen config | packs ~50 flags (device_debug, lineinfo, fast_compile, maxrregcount, opt_level, compile_only, tools_patch, ewp, preserve_relocs, sm_version, address_width, default caches, PIC, ...) into struct; `sub_16257C0` creates `CodegenPipeline` | Builds pipeline-config object |
| 22 | Output file setup | `fopen_and_truncate` on `--output` path | Prepares dump file |
| 23a | **Sequential** per-function loop | `sub_110AA30` `codegen_init` → `sub_1655A60` 48-pass pipeline → `sub_1102B30` `codegen_compile` (setjmp-wrapped) → `sub_110D2A0` `codegen_finalize` | Compiles each function in main thread |
| 23b | **Parallel** per-function loop | `sub_43FDB0` thread-pool create → for each func build 48-B work item → `sub_43FF50` enqueue → `sub_43FFE0` barrier → `sub_43FE70` destroy. Worker = `sub_1107420` → `sub_1102B30` (setjmp + compile) + timing | Same as `--split-compile-extended` in standalone ptxas |
| 24 | Post-compilation cleanup | register-budget cross-check if `--compile-only` (caller-callee budget validation through `register_budget_map`) | Validates inter-function constraints |
| 25 | Pipeline config teardown | `sub_1626480` `pipeline_finalize` | Destroys `CodegenPipeline` |
| 26 | Final cleanup | `sub_4650A0` destroys hash maps; frees per-function arrays | Returns 0 |

### Per-Function Inner Pipeline (Phase 23)

For each function in the compile list, Phase 23 (either sequential or thread-pool worker) runs the following sub-stages:

| Sub-stage | Address | Role |
|---|---|---|
| `codegen_init` | `sub_110AA30` | Allocate 360-B per-function state; create OCG context; set producer="NVIDIA", tool="ptxocg.0.0"; configure ~30 SM-specific fields; invoke vtable->init to map symbol names |
| `codegen_per_func` | `sub_1655A60` | Drive the 48-pass codegen pipeline — see [The 48-Pass Codegen Pipeline](#the-48-pass-codegen-pipeline) |
| `codegen_compile` | `sub_1102B30` | `setjmp`-wrapped vtable->compile call; longjmp + record failure on error |
| Timing record | — | `timing_record` writes per-function start/end time |
| `codegen_finalize` | `sub_110D2A0` | Emit ELF section content (.text, .nv.info, .nv.constant); write EIATTR register usage records; write SASS binary; cleanup per-function OCG state |

In parallel mode each worker additionally allocates three local sorted maps (cap 8), copies a 15×16-byte snapshot of driver state into the per-function state, and allocates a 216-B per-function DWARF state via `dwarf_register`. After the barrier, the main thread merges per-thread maps, restores DWARF and pipeline snapshots, and runs `codegen_finalize` sequentially for each function so that register-budget propagation observes a deterministic order.

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

The per-function codegen entry point `sub_1655A60` runs a 48-pass pipeline (passes 0--47). Each pass is enable-gated by the SM dispatch vtable at `a1[3757]` (registered by `sub_15C0CE0`); enable flags occupy `a1[160..207]`. The pass numbering, vtable offsets, and binary slot allocation are the nvlink-embedded copy's own and do not match the standalone ptxas pass numbering (~159 phases) — for the corresponding standalone passes see [ptxas: Passes Index](../../ptxas/passes/index.html), [ptxas: Instruction Selection](../../ptxas/codegen/isel.html), and [ptxas: Scheduling Algorithm](../../ptxas/scheduling/algorithm.html).

| Pass(es) | Role | Gating |
|---|---|---|
| 0 | Zero placeholder | always off |
| 1 | Initial IR canonicalization | unconditional |
| 2 | Instruction count estimation | vtable+120 |
| 3--20 | SM-gated optimization passes (architecture-specific opt) | vtable+72 capability query per pass |
| 21 | Address-width-dependent setup | gated on `addr_width` |
| 22 | Register class initialization | unconditional for SM >= sm_50 |
| 23--38 | Core backend: ISel mega-hub dispatch, regalloc (graph coloring + spilling), `ScheduleInstructions`, peephole, SASS encoding | universally enabled for SM >= sm_50 |
| 39 | Initial ABI frame setup | unconditional |
| 40--42 | Final lowering passes | unconditional |
| 43 | Peephole cleanup | unconditional |
| 44--45 | Reserved | always off |
| 46 | Binary encoding query | vtable+488 |
| 47 | Final verification + pass-count teardown | unconditional |

After the pass loop, `sub_1655A60` registers additional IR lowering callbacks (`sub_161F1C0`, `sub_161F800`, `sub_1620460`) on the function's basic block list, sets up UDT/UFT relocations for Blackwell+ (SM ordinal > 26 = sm_100+), and processes the function's call graph for register pressure analysis.

### Sequential vs. Parallel Compilation

Selected by `ctx->thread_count` at `a1+668`. Sequential (count = 0) runs `codegen_init -> codegen_compile -> codegen_finalize` on the main thread with timing recorded between stages. Parallel (count > 0) creates a thread pool via `sub_43FDB0`, builds 48-byte work items containing the per-function state snapshot (360 B + 3 local sorted maps + 216-B DWARF state + pipeline snapshot), enqueues each via `sub_43FF50`, barriers on `sub_43FFE0`, then destroys via `sub_43FE70`. Workers run `sub_1107420`, which delegates to `sub_1102B30` (`setjmp`-wrapped compile) and records timing + peak memory. After the barrier the main thread merges per-thread maps back, restores DWARF and pipeline snapshots, and runs `codegen_finalize` sequentially so register-budget propagation is deterministic. If `qword_2A64430` is non-null, each worker error-checks via `sub_1D1E060`/`sub_1D1E300` after its work item. This is the same `--split-compile-extended` mechanism available in standalone ptxas.

## Cross-References

### nvlink Internal
- [IR Nodes](ir-nodes.md) — IR node structure and universal accessor functions
- [ISel Hubs](isel-hubs.md) — the five mega-hub instruction selector dispatch functions
- [Peephole](peephole.md) — peephole optimization passes (ORI, scheduling-phase, linker-level)
- [PTX Parsing](ptx-parsing.md) — the embedded PTX assembler frontend
- [Register Allocation](register-allocation.md) — graph-coloring register allocator with spilling
- [Scheduling](scheduling.md) — pre-RA and tepid (post-RA) instruction schedulers
- [Architecture Dispatch](arch-dispatch.md) — per-SM vtable dispatch system
- [Mercury Overview](../mercury/overview.md) — Mercury ISA encoding pipeline
- [FNLZR](../mercury/fnlzr.md) — post-link binary rewriter for Mercury targets
- [LTO Overview](../lto/overview.md) — how the LTO pipeline invokes the embedded compiler

### Sibling Wikis
- [ptxas: Pipeline Overview](../../ptxas/pipeline/overview.html) — standalone ptxas 159-phase compilation pipeline
- [ptxas: Entry Point](../../ptxas/pipeline/entry.html) — standalone ptxas main() and option parsing
- [ptxas: Optimizer](../../ptxas/pipeline/optimizer.html) — standalone ptxas optimization passes
- [ptxas: Codegen Overview](../../ptxas/codegen/overview.html) — standalone ptxas code generation
