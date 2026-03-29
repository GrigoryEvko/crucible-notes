# Binary Layout

This page is a visual guide to navigating the cicc v13.0 binary in IDA Pro. It covers the ELF structure, section layout, subsystem address ranges, embedded data payloads, and the statically linked jemalloc allocator. If you are opening this binary for the first time, start here to orient yourself before diving into individual subsystems.

## ELF Overview

CICC is a statically linked, stripped x86-64 ELF binary. There are no dynamic symbol tables, no `.dynsym`, no DWARF debug info, and no export table. Every function name was removed at build time. IDA Pro recovers 80,562 functions; Hex-Rays successfully decompiles 80,281 of them (99.65%).

| Property | Value |
|---|---|
| File size | 60,108,328 bytes (57.3 MB) |
| Architecture | x86-64, little-endian |
| Linking | Fully static (no `.interp`, no PLT/GOT) |
| Stripped | Yes, all symbol tables removed |
| Build ID | `cuda_13.0.r13.0/compiler.36424714_0` |
| Compiler | Built with GCC (inferred from CRT stubs and `.init_array` layout) |
| Allocator | jemalloc 5.3.x, statically linked (~400 functions) |

Because the binary is statically linked, libc, libpthread, and libm are all embedded. This inflates the raw function count but also means every call target resolves to a concrete address within the binary itself -- there are no external dependencies at runtime beyond the kernel syscall interface.

## Address Space Map

The binary's `.text` section spans roughly `0x400000` to `0x3C00000`. Within that 56 MB range, subsystems occupy contiguous, non-overlapping regions. The map below is the primary orientation tool for IDA Pro navigation.

```
0x400000 ┌─────────────────────────────────────────┐
         │  CRT startup + libc stubs               │  ~52 KB
0x40D000 ├─────────────────────────────────────────┤
         │  jemalloc stats / vsnprintf              │  ~80 KB
0x420000 ├─────────────────────────────────────────┤
         │  (gap: misc libc, math, string ops)      │  ~64 KB
0x430000 ├─────────────────────────────────────────┤
         │  Global constructors (cl::opt reg)        │  ~1.6 MB
         │  ~1,689 LLVM command-line option objects  │
0x5D0000 ├─────────────────────────────────────────┤
         │  EDG 6.6 C++ Frontend                    │  3.2 MB
         │  Parser, constexpr evaluator, IL walker   │
0x8F0000 ├─────────────────────────────────────────┤
         │  CLI / Real Main / NVVM Bridge            │  520 KB
         │  sub_8F9C90 (real main), dual-path dispatch│
0x960000 ├─────────────────────────────────────────┤
         │  Architecture detection, NVVM options     │  576 KB
0x9F0000 ├─────────────────────────────────────────┤
         │  Bitcode reader (parseFunctionBody)       │  ~1 MB
0xAF0000 ├─────────────────────────────────────────┤
         │  X86 AutoUpgrade (legacy, 457KB fn)       │  ~1 MB
0xBF0000 ├─────────────────────────────────────────┤
         │  LLVM IR Verifier                        │  500 KB
0xC00000 ├─────────────────────────────────────────┤
         │  LLVM optimization passes                │  ~7 MB
         │  InstCombine, GVN, DSE, LICM, etc.       │
0x12D0000├─────────────────────────────────────────┤
         │  PassManager / NVVM bridge                │  4.2 MB
         │  Pipeline assembly (sub_12E54A0)          │
0x12FC000├ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┤
         │  jemalloc core (~400 functions)           │  ~256 KB
0x1700000├─────────────────────────────────────────┤
         │  Backend / machine passes                 │  8 MB
         │  RegAlloc, Block Remat, Mem2Reg           │
0x1F00000├─────────────────────────────────────────┤
         │  SelectionDAG                            │  2 MB
         │  LegalizeTypes (348KB), LegalizeOp        │
0x2100000├─────────────────────────────────────────┤
         │  NVPTX PTX emission                      │  1 MB
0x2340000├─────────────────────────────────────────┤
         │  New PM / pass registration               │  768 KB
         │  2,816-line registrar at sub_2342890      │
0x2A00000├─────────────────────────────────────────┤
         │  Loop passes                             │  4 MB
         │  LoopVectorize, SLP, Unroll               │
0x3000000├─────────────────────────────────────────┤
         │  NVPTX ISel + lowering                    │  7 MB
         │  343KB intrinsic switch (sub_33B0210)     │
0x3700000├─────────────────────────────────────────┤
         │  Machine-level passes (tail)              │  ~3 MB
         │  BlockPlacement, Outliner, StructurizeCFG │
0x3A00000├─────────────────────────────────────────┤
         │  (trailing code, CRT finalization)        │
         └─────────────────────────────────────────┘

DATA SECTIONS:
0x3EA0080   Embedded libdevice bitcode (Path A)    456 KB
0x420FD80   Embedded libdevice bitcode (Path B)    456 KB
0x4F00000+  Global BSS (cl::opt storage, hash tables, state)
```

## Embedded Data Payloads

### Libdevice Bitcode

Two identical copies of NVIDIA's libdevice are embedded directly in the `.rodata` section as raw LLVM bitcode. Each copy is approximately 456 KB and contains around 400 math intrinsic implementations (`__nv_sinf`, `__nv_expf`, `__nv_sqrtf`, etc.). The duplication supports the dual-path architecture: Path A (LibNVVM API mode) references one copy at `0x3EA0080`; Path B (standalone mode) references the other at `0x420FD80`. The bitcode is linked into the user's module during the LNK phase via the bitcode linker at `sub_12C06E0`.

### String Tables

IDA Pro extracts 188,141 strings from the binary. These fall into several categories:

| Category | Approximate count | Example |
|---|---|---|
| LLVM `cl::opt` descriptions | ~1,689 | `"Enable aggressive reassociation"` |
| LLVM error/diagnostic messages | ~5,000 | `"Invalid bitcode signature"` |
| EDG error messages | ~2,500 | `"expected a declaration"` |
| LLVM pass names | ~440 | `"instcombine"`, `"gvn"`, `"nvvm-memspace-opt"` |
| PTX instruction templates | ~800 | `"mov.b32 %0, %1;"` |
| NVVM builtin names | ~770 | `"__nvvm_atom_cas_gen_i"` |
| jemalloc config strings | ~200 | `"background_thread"`, `"dirty_decay_ms"` |
| NVVM container field names | ~144 | `"SmMajor"`, `"FastMath.Ftz"` |
| Miscellaneous (format strings, assertions) | ~170,000+ | `"%s:%d: assertion failed"` |

String cross-referencing is the single most productive technique for identifying functions in a stripped binary. The LLVM pass registration pattern is especially reliable: a string like `"nvvm-memspace-opt"` appears exactly once, in the constructor of that pass, which IDA locates via xref.

### NVVM Container Format

The binary includes a proprietary container format for wrapping LLVM bitcode with compilation metadata. The container uses a 24-byte binary header with magic `0x7F4E5C7D`, followed by delta-encoded tag/value pairs (only fields that differ from defaults are serialized). There are 144 distinct tag IDs spanning core options (tags 1-39), compression metadata (tag 99), extended target options (tags 101-173), blob data (tags 201-218), and structured hardware descriptors (tags 401-402 for TMA/TCGen05 configurations). Serialization and deserialization are handled by `sub_CDD2D0` and `sub_CD1D80` respectively.

## jemalloc Integration

NVIDIA statically links jemalloc 5.3.x as the process-wide memory allocator. The jemalloc functions cluster around `0x12FC000` (approximately 400 functions). The configuration initialization function `sub_12FCDB0` (129 KB, one of the largest functions in the binary) parses 199 configuration strings from the `MALLOC_CONF` environment variable.

Key jemalloc entry points visible in the binary:

| Address | Identity |
|---|---|
| `0x12FCDB0` | `malloc_conf_init` (199 config strings) |
| `0x40D5CA` | `vsnprintf` (jemalloc stats formatting) |
| `0x12FC000` range | Core arena management, tcache, extent allocator |

The jemalloc integration is significant for reverse engineering because it means `malloc`/`free` calls throughout the binary resolve to jemalloc's arena-based allocator rather than glibc's `ptmalloc2`. When tracing memory allocation patterns in IDA, look for calls into the `0x12FC000` range.

## Global Constructors

The region from `0x430000` to `0x5CFFFF` (~1.6 MB) is dominated by global constructors that execute before `main()`. The primary purpose of these constructors is LLVM `cl::opt` registration: approximately 1,689 command-line option objects are initialized, each registering a string name, description, default value, and storage location into LLVM's global option registry.

The `.init_array` section contains function pointers to these constructors. They execute in linker-determined order and populate a global hash table that `sub_8F9C90` (the real main) later queries during CLI parsing. In IDA Pro, navigating to any `cl::opt` constructor reveals the option name string and its associated global variable, which is invaluable for understanding what flag controls what behavior.

Additional global constructors handle:

- LLVM pass registration (`RegisterPass<T>` and `PassInfo` objects)
- LLVM target initialization (NVPTX target machine factory)
- jemalloc allocator bootstrapping
- EDG frontend static initialization tables

## Dual-Path Code Duplication

A distinctive structural feature of the binary is the presence of two near-complete copies of the NVVM bridge and backend entry points. Path A (LibNVVM API mode) lives around `0x90xxxx`; Path B (standalone/nvcc mode) lives around `0x126xxxx`. Each path has its own:

| Component | Path A | Path B |
|---|---|---|
| Simple compile entry | `sub_902D10` | `sub_1262860` |
| Multi-stage pipeline | `sub_905EE0` (43 KB) | `sub_1265970` (48 KB) |
| CLI parsing | `sub_900130` | `sub_125FB30` |
| Builtin resolution table | `sub_90AEE0` (109 KB) | `sub_126A910` (123 KB) |
| Embedded libdevice ref | `unk_3EA0080` | `unk_420FD80` |
| Version string | `nvvm-latest` | `nvvm70` |

In IDA, if you have identified a function in one path, search for a structurally similar function at the corresponding offset in the other path. The code is not byte-identical -- Path B is generally slightly larger due to additional standalone-mode logic -- but the control flow graphs are nearly congruent.

## IDA Pro Navigation Tips

When opening cicc in IDA Pro for the first time, the auto-analysis will take several minutes due to the 60 MB size. The following workflow accelerates orientation:

1. **Start with strings.** Open the Strings window (Shift+F12), filter for known LLVM pass names (`"instcombine"`, `"gvn"`, `"nvvm-"`). Each xref leads directly to a pass constructor or registration site.

2. **Use the address map above.** If you are looking at an address in the `0xC00000`-`0x12CFFFF` range, you are in LLVM optimization passes. The `0x3000000`-`0x36FFFFF` range is NVPTX instruction selection. The `0x5D0000`-`0x8EFFFF` range is EDG. Context narrows the search space immediately.

3. **Watch for vtable patterns.** LLVM passes are C++ classes with virtual methods. IDA's vtable reconstruction reveals inheritance hierarchies. Every `FunctionPass`, `ModulePass`, and `LoopPass` subclass has a vtable with `runOnFunction`/`runOnModule` at a consistent slot offset.

4. **Anchor on mega-functions.** The largest functions are the easiest to locate and serve as landmarks: `sub_A939D0` (457 KB, X86 AutoUpgrade), `sub_10EE7A0` (396 KB, InstCombine), `sub_20019C0` (341 KB, LegalizeTypes). These anchors partition the address space.

5. **Follow the pipeline.** Entry at `sub_8F9C90` calls into EDG at `sub_5D2A80`, pipeline assembly at `sub_12E54A0`, and PTX emission starting at `0x2100000`. Tracing callgraph edges from these known entry points maps out the entire compilation flow.

6. **Mark jemalloc early.** Identifying and labeling the jemalloc cluster at `0x12FC000` prevents wasted time reverse-engineering well-known allocator internals. The 199-string `malloc_conf_init` function is an unmistakable fingerprint.
