# Program Layout

## Abstract

Tileiras is a single ELF executable with the entire MLIR-and-LLVM compiler statically linked inside it. Its segments are conventional - `.text`, `.rodata`, `.data`, `.bss`, plus the usual support sections - but the content of each segment is structured by subsystem boundary. The `.text` segment is partitioned into bands that correspond to the driver, the bytecode reader, dialect implementations, conversion patterns, the scheduler, codegen, and the LLVM and NVPTX libraries. The `.rodata` segment carries dialect string pools, pass and diagnostic strings, the bitcode writer's tag tables, and the XOR-3 obfuscated NVPTX mnemonic pool used by the asm printer. The `.data` segment carries `cl::opt` globals, dialect registration tables, and the encoded mnemonic pool that the printer walks at runtime. The `.bss` segment carries the singletons every subsystem accumulates: the `StorageUniquer` hash tables, the `TypeID` Meyers-cache slots, the registered dialect instances, and the per-thread caches reached through TLS. This page describes what lives in each band and why each band has the shape it does. Addresses are deliberately omitted; reimplementers care about the structure, not the offsets.

## Identity

| Property | Value |
| --- | --- |
| Tool role | CUDA TileIR optimizing assembler |
| CUDA release | 13.1 |
| Toolkit banner | `Cuda compilation tools, release 13.1, V13.1.80` |
| LLVM lineage | Internal LLVM mainline snapshot identifying as `LLVM21.0.0git` |
| Input format | TileIR MLIR bytecode |
| Primary output | Host relocatable object containing compiled GPU code |
| Default output name | `elf.o` |
| Default GPU family | Blackwell-family target, normally `sm_100` |

Tileiras is not a C++ frontend. It does not parse CUDA C++, instantiate templates, or generate host stubs. It consumes serialized MLIR, lowers it through the TileIR dialect stack, and produces PTX text that is then assembled by `ptxas`.

## `.text` Bands

Code is grouped by subsystem; each band is a contiguous region containing the functions of one cohesive responsibility. The order, top to bottom, roughly tracks the runtime path through the compiler.

| Band | Contents |
| --- | --- |
| Driver text | Command-line parsing, target validation, CUDA-toolkit discovery, subprocess harness, file emission. |
| Bytecode reader text | Container header parsing, section walkers, varint and string-table reconstruction, operation/region rebuilder, post-load verifier driver. |
| Dialect-registration text | The `register_operations`, `register_types`, `register_attributes`, and `register_interfaces` bodies for each of the nine dialects, plus the per-operation `verify`, `fold`, `print`, and `parse` hooks. |
| Lowering text | The `populate_*_patterns` functions and pattern bodies for every conversion edge in the TileIR stack; `ConversionTarget` configuration; type converters; full and partial conversion drivers. |
| Scheduler text | RRT construction and probing, MII-bound computation, group placement, modulo-schedule solve, pipe/mutex materialization. |
| Codegen text | MLIR-to-LLVM translation, libdevice link logic, LLVM IR pipeline driver, NVPTX selection-DAG driver, MachineIR verifier hooks, asm printer. |
| LLVM/NVPTX library text | The statically linked LLVM core, LLVM IR analysis and transform passes, the NVPTX backend, the SelectionDAG infrastructure, and the asm-printer support. |
| C++ runtime and libstdc++ text | The standard-library bodies pulled in by the static link (the `std::sort` introsort, hash-table primitives, allocator support, the exception runtime). |

Each band has a stable internal shape: a small number of public entry points called by neighbouring bands, surrounded by a much larger population of pattern bodies and helper functions called only within the band. The lowering band is the heaviest; the scheduler band is the densest in algorithmic content per byte.

## `.rodata` Bands

Read-only data is structured by purpose. The largest bands carry strings; the smaller bands carry the constant tables that drive the dispatch machinery.

| Band | Contents |
| --- | --- |
| Per-dialect mnemonic pools | The operation, type, and attribute mnemonics for each dialect, kept in registration order. The bytecode reader and the printer both index into these pools through `OperationName`. |
| Per-pass diagnostic strings | The text of every pass-emitted diagnostic, plus the verifier strings used by `verify` hooks. |
| Conversion-pattern descriptors | The static descriptor structures (`OpRewritePattern` / `OpConversionPattern` instances) for the lowering patterns, including their root mnemonics, benefits, and source-dialect tags. |
| NVPTX printer string table | The PTX mnemonic and operand-format strings consumed by the asm printer. These are stored XOR-3 encoded (each byte XORed with three) and decoded on first use; the encoded form keeps the readable PTX vocabulary out of `strings` output without changing runtime cost. |
| Bitcode writer string blob | The fixed blob of attribute, type, and metadata tag names that LLVM's bitcode writer would otherwise embed in every output; here it is interned into one rodata region and referenced by offset. |
| `cl::opt` description text | The help text and default-value descriptions for every command-line option, including the LLVM-inherited options and the Tileiras-specific knobs. |
| C++ typeinfo and vtables | The `__cxxabiv1` typeinfo nodes and the vtables for every polymorphic class - dialects, passes, patterns, conversion-target objects, the diagnostic engine, and so on. |
| libdevice bitcode blob | The bundled libdevice bitcode for the supported target families, embedded as a rodata blob and parsed at compile time. |

Each rodata band has a single dominant access pattern: mnemonic pools are read by hash lookup, descriptor arrays are walked sequentially during pass registration, vtables are indexed by slot, and the libdevice blob is read end-to-end exactly once per compilation.

## `.data` Bands

Writable but statically initialised data carries the global state that registration and option handling populate at startup.

| Band | Contents |
| --- | --- |
| `cl::opt` globals | The mutable storage for every command-line option (boolean flags, integers, enums, paths). Parsed values land here. |
| Dialect-registration tables | The static arrays the dialect registrar walks during `register_operations` / `register_types`. Their entries point into the `.rodata` mnemonic pools and into the `.text` hook functions. |
| Pass-registration tables | The list of static `PassRegistration` and `PassPipelineRegistration` entries that the pipeline builder consults to assemble each opt-level pipeline. |
| XOR-3 mnemonic walking-cipher pool | The runtime working copy of the NVPTX mnemonic table the printer decodes on first reference. The pool is initialised from rodata and walked entry-by-entry as PTX is emitted. |
| Global constant initialisers | Initialised static objects for the LLVM core, including the LLVM context manager, the target registry, and the intrinsic table. |

The data segment is small relative to text and rodata. Most truly mutable state lives in `.bss`; the data segment exists primarily to give registration tables and `cl::opt` storage a stable load-time address.

## `.bss` Bands

Zero-initialised state is where every subsystem keeps its singletons and lazy caches. These are the structures that grow during a compilation and are not meant to outlive the process.

| Band | Contents |
| --- | --- |
| `StorageUniquer` hash tables | The hash-set storage that uniques types and attributes. One bucket array per uniquer kind; populated lazily on first lookup. |
| `TypeID` Meyers-cache singletons | The `static` local slots used by `mlir::TypeID::get<T>()` to give each concrete type a stable identity. Each instantiation lands in its own zero-initialised slot guarded by a one-time-init flag. |
| Dialect-singleton storage | The single `Dialect` instance per dialect kind, owned by the `MLIRContext` once registration runs. |
| Operation-name registry | The mnemonic-to-`AbstractOperation` lookup table, populated by dialect registration and read on every operation construction. |
| Pass and pattern statistics | The counter slots that pass and pattern implementations bump for `Statistic` reporting. |
| Per-thread caches | The TLS slots used by the diagnostic engine, the pass timer, the threaded pass manager, and the pattern applicator's local rewrite buffers. |
| LLVM context state | The `LLVMContext` singletons and intrinsic caches the codegen path relies on. |

Every entry in the `.bss` bands is logically owned by exactly one subsystem and is reset (or simply discarded) between compilations of independent modules.

## Data Lifetimes

Three lifetime classes span the bands above. Knowing which lifetime a piece of data belongs to is what keeps the layout coherent across a long compilation.

| Lifetime | Data | Ends when |
| --- | --- | --- |
| Static-init lifetime | `.rodata` pools, `.data` registration tables, vtables, libdevice blob, `cl::opt` defaults | Never; constructed during process startup. |
| Compile-unit lifetime | Bytecode buffer, MLIR module, dialect operations, scheduler analyses, LLVM module, libdevice working copy, PTX text | The host relocatable for the unit has been written. |
| Per-pass lifetime | Pattern applicator state, rewrite buffers, conversion target snapshots, diagnostic temporaries | The pass manager finishes the pass. |

Keeping these lifetimes separate matters in practice. `cuda_tile` operations are not meaningful after their conversion edge has run; scheduler analyses are not meaningful after lowering reaches LLVM; LLVM MachineIR is not meaningful before instruction selection; the XOR-3 mnemonic working copy is not meaningful before the asm printer is invoked.

## Reimplementation Notes

A compatible implementation does not have to reproduce the executable's segment layout. It does have to reproduce the contracts that the layout exists to support: a stable static-init order for dialect, type, attribute, and pass registration; a uniquer that returns the same value for equal operands across the process; a `.rodata`-style discipline that keeps mnemonic pools, diagnostic strings, and printer tables immutable; and a `.bss`-style discipline that scopes all growth-only structures to the compilation that allocated them. The detailed pages under each subsystem describe these contracts as algorithms and invariants rather than as offsets.

For the reader-side view — file identity, the tools the wiki was produced with, and a recipe for verifying any individual wiki claim against the binary — see [Binary Anatomy and RE Methodology](topics/binary-anatomy-and-re-methodology.md).
