# Binary Layout

This page is the primary orientation map for navigating the nvlink v13.0.88 binary in IDA Pro. It documents every ELF segment, the complete `.text` address-range breakdown across all 20 sweep regions, subsystem composition, the five mega-hub instruction selectors, and the import table. If you are opening the binary for the first time, start here.

## ELF Overview

nvlink is a dynamically linked, fully stripped x86-64 ELF binary. All C++ symbols have been removed. The binary imports only C-level symbols from glibc, pthreads, and libdl -- no `std::`, no `__cxa_`, no vtable symbols are visible in the import table. This confirms that the C++ runtime is statically linked (or that all C++ was compiled with hidden visibility and LTO).

| Property | Value |
|---|---|
| File size | ~37 MB |
| Architecture | x86-64, little-endian |
| Linking | Dynamic (glibc, pthreads, libdl, libm) |
| Stripped | Yes -- all internal symbols removed |
| Build ID | `cuda_13.0.r13.0/compiler.36424714_0` |
| Build date | Wed Aug 20, 2025 |
| Version | `Cuda compilation tools, release 13.0, V13.0.88` |
| Tool identity | `NVIDIA (R) Cuda linker` |
| Functions | 40,532 total |
| Imports | 156 dynamic symbols (C-only, no C++ mangled names) |

## ELF Segment Map

| Segment | Start | End | Size | Perms |
|---|---|---|---|---|
| ELF header + LOAD | `0x400000` | `0x402EE0` | 12.0 KB | R-X |
| `.init` | `0x402EE0` | `0x402EF8` | 24 B | R-X |
| `.plt` | `0x402F00` | `0x4038C0` | 2.4 KB | R-X |
| **`.text`** | **`0x4038C0`** | **`0x1D32172`** | **25.2 MB** | **R-X** |
| `.fini` | `0x1D32174` | `0x1D32182` | 14 B | R-X |
| **`.rodata`** | **`0x1D321A0`** | **`0x2463BB0`** | **7.2 MB** | **R--** |
| `.eh_frame_hdr` | `0x2463BB0` | `0x24BC244` | 354 KB | R-- |
| `.eh_frame` | `0x26BC968` | `0x2A59938` | 3.6 MB | RW- |
| `.gcc_except_table` | `0x2A59938` | `0x2A59CE4` | 940 B | RW- |
| `.ctors` | `0x2A59CE8` | `0x2A59D50` | 104 B | RW- |
| `.dtors` | `0x2A59D50` | `0x2A59D60` | 16 B | RW- |
| `.data.rel.ro` | `0x2A59D60` | `0x2A5AE00` | 4.2 KB | RW- |
| `.got` | `0x2A5AFE0` | `0x2A5AFF8` | 24 B | RW- |
| `.got.plt` | `0x2A5B000` | `0x2A5B4F0` | 1.2 KB | RW- |
| `.data` | `0x2A5B500` | `0x2A5F180` | 15.1 KB | RW- |
| `.bss` | `0x2A5F180` | `0x2A77DD8` | 99.1 KB | RW- |
| `.tls` | `0x2A77DE0` | `0x2A77DF8` | 24 B | --- |
| `extern` (imports) | `0x2A77DF8` | `0x2A782D8` | 1.2 KB | --- |

The `.text` section dominates the binary at 25.2 MB. The `.rodata` section (7.2 MB) holds instruction encoding constant tables (xmmword templates, operand descriptor arrays, opcode lookup tables), string literals, and read-only data structures.

## Binary Composition

The single most important structural fact about nvlink is its size distribution. The binary is a **hybrid linker-compiler** where the actual linker is a small minority of the code.

| Component | Approx. Size | % of .text | Function Count | Role |
|---|---|---|---|---|
| Embedded ptxas backend | ~15 MB | ~60% | ~20,000 | PTX-to-SASS compiler: ISel, regalloc, scheduling, encoding |
| Instruction encoding tables | ~7 MB | ~28% | ~8,000 | Per-SM SASS encoder/decoder/descriptor functions |
| Linker core | ~1.2 MB | ~5% | ~600 | ELF merge, symbol resolution, relocation, layout, output |
| LTO orchestration | ~1.0 MB | ~4% | ~500 | IR collection, libnvvm dlopen, PTX compilation dispatch |
| Infrastructure | ~0.5 MB | ~2% | ~400 | Memory arenas, option parsing, hash tables, compression |
| Mercury / FNLZR | ~0.3 MB | ~1% | ~200 | SM100+ capsule mercury post-link transformation |

Approximately **95% of the binary is compiler backend**. Only about **5% is linker logic**. Most functions you encounter during reverse engineering are instruction selection patterns, SASS encoders, or register allocator internals -- not linker code.

## Visual Memory Map

The following ASCII diagram shows the complete virtual address layout of nvlink v13.0.88. Each row is proportional to its byte size. The `.text` section is expanded into all 16 functional zones. The rightmost column shows the dominant subsystem and approximate function count.

### Full ELF Virtual Address Layout

```text
 Address         Section / Region                                       Size
 ──────────────────────────────────────────────────────────────────────────────
 0x400000       ┌──────────────────────────────────────────────────┐
                │  ELF Header (64 B) + Program Headers (392 B)    │   12.0 KB
                │  .interp / .hash / .dynsym / .dynstr / .rela    │
 0x402EE0       ├──────────────────────────────────────────────────┤
                │  .init (24 B)                                    │   24 B
 0x402F00       ├──────────────────────────────────────────────────┤
                │  .plt  (156 stubs)                               │   2.4 KB
 0x4038C0       ├══════════════════════════════════════════════════╡
                │                                                  │
                │              .text  (25.2 MB)                    │
                │       40,532 functions -- see breakdown below    │
                │                                                  │
 0x1D32172      ├══════════════════════════════════════════════════╡
                │  .fini (14 B)                                    │   14 B
 0x1D321A0      ├──────────────────────────────────────────────────┤
                │  .rodata  (string tables, encoding templates,    │   7.2 MB
                │   xmmword constants, operand descriptor arrays,  │
                │   ROT13 SASS mnemonics, lookup tables)           │
 0x2463BB0      ├──────────────────────────────────────────────────┤
                │  .eh_frame_hdr                                   │   354 KB
 0x24BC244      ├──────────────────────────────────────────────────┤
                │  (hole -- LOAD segment alignment padding)        │   2.0 MB
 0x26BC968      ├──────────────────────────────────────────────────┤
                │  .eh_frame  (unwinding data for 40K functions)   │   3.6 MB
 0x2A59938      ├──────────────────────────────────────────────────┤
                │  .gcc_except_table                               │   940 B
 0x2A59CE8      ├──────────────────────────────────────────────────┤
                │  .ctors (104 B) + .dtors (16 B)                  │   120 B
 0x2A59D60      ├──────────────────────────────────────────────────┤
                │  .data.rel.ro  (vtable pointers)                 │   4.2 KB
 0x2A5AFE0      ├──────────────────────────────────────────────────┤
                │  .got (24 B) + .got.plt (1.2 KB)                 │   1.2 KB
 0x2A5B500      ├──────────────────────────────────────────────────┤
                │  .data  (initialized globals: option defaults,   │   15.1 KB
                │   function pointers, dispatch tables)            │
 0x2A5F180      ├──────────────────────────────────────────────────┤
                │  .bss   (zero-init: knob tables, TLS keys,       │   99.1 KB
                │   option vars, SM dispatch hash maps, mutexes)   │
 0x2A77DD8      ├──────────────────────────────────────────────────┤
                │  .tls (24 B) + extern/imports (1.2 KB)           │   1.2 KB
 0x2A782D8      └──────────────────────────────────────────────────┘
                   Total mapped: ~43 MB virtual, ~37 MB on disk
```

### .text Section Expanded -- 16 Functional Zones

The 25.2 MB `.text` section decomposes into 16 zones. The diagram uses one line per major zone, with bar width proportional to byte size. The `[====]` bars indicate relative size (1 `=` per ~100 KB).

```text
  0x4038C0                                                             0x1D32172
  |                         .text  (25.2 MB, 40,532 functions)                 |
  |                                                                            |
  Zone  Start      End        Size   Bar                      Identity
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   1    0x4038C0   0x470000   430 KB [====]                   Linker core: main,
                                                              CLI, merge, layout
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   2    0x470000   0x530000   768 KB [=======]                Finalize, arch compat,
                                                              debug info, LTO entry,
                                                              encoding infra
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   3    0x530000   0x620000   960 KB [=========]              IR primitives +
                                                              SM50-7x ISel +
                                                              MercExpand engine
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   4    0x620000   0x7A0000   1.5 MB [===============]        SM100+ Blackwell
                                                              SASS encoders (1,068)
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   5    0x7A0000   0x920000   1.5 MB [===============]        SM100+ encoders tail
                                                              (469) + InstrDesc
                                                              init table (670)
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   6    0x920000   0xA70000   1.3 MB [=============]          InstrDesc table cont.
                                                              (943) + NVInst
                                                              accessors + operand
                                                              dispatch switches
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   7    0xA70000   0xB80000   1.1 MB [===========]            Multi-arch codec:
                                                              field queries,
                                                              SM90/100 encoders
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   8    0xB80000   0xCA0000   1.1 MB [===========]            SM7x + SM80 codecs,
                                                              Turing/Ampere
                                                              enc/dec tables
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
   9    0xCA0000   0xDA0000   1.0 MB [==========]             SM80 ISel backend:
                                                              pattern matchers +
                                                              mega-hub (239 KB)
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
  10    0xDA0000   0xF16000   1.5 MB [===============]        SM100+ Blackwell
                                                              codec 2nd table:
                                                              encoders + decoders
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
  11    0xF16000   0x100C000  984 KB [=========]              SM75 ISel backend:
                                                              patterns (276) +
                                                              mega-hub (280 KB)
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
  12    0x100C000  0x11EA000  1.9 MB [===================]    Shared encoders +
                                                              SM89/90 backend +
                                                              ISel mega-hub (231 KB)
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
  13    0x11EA000  0x1430000  2.3 MB [=======================]PTX frontend + ISel
                                                              helpers + LTO engine
                                                              + ISel pattern clones
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
  14    0x1430000  0x16E0000  2.8 MB [============================]
                                                              PTX assembler +
                                                              builtins + CUDA
                                                              compilation backend
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
  15    0x16E0000  0x1A00000  3.2 MB [================================]
                                                              Scheduler + regalloc
                                                              + SASS opcode tables
                                                              + codegen verify
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
  16    0x1A00000  0x1D32172  3.2 MB [================================]
                                                              SASS backend + ABI +
                                                              ELF builder + DWARF +
                                                              demangler + knobs
  ───── ────────── ────────── ────── ──────────────────────── ────────────────────
```

### Subsystem Heat Map

The following diagram groups the `.text` section by subsystem identity rather than address, showing where each logical component's code resides. Non-contiguous ranges for the same subsystem are combined.

```text
  .text subsystem view (25.2 MB)
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │ Linker Core        ░░░                                                │1.2MB│
  │ (main, merge,      ░░░                                                │  5% │
  │  layout, reloc)    ░░░                                                │~150 │
  ├────────────────────┬───────────────────────────────────────────────────┤     │
  │ LTO + Infra   ░░░░░│                                                  │1.5MB│
  │ (libnvvm, arena,   │                                                  │  6% │
  │  knobs, compress)  │                                                  │~100 │
  ├────────────────────┴──┬────────────────────────────────────────────────┤     │
  │ ISel Dispatch          ████████████████████████████                    │5.0MB│
  │ (5 mega-hubs +         ████████████████████████████                    │ 20% │
  │  2,000+ pattern        ████████████████████████████                    │~2500│
  │  matchers)             ████████████████████████████                    │     │
  ├────────────────────────┴──┬───────────────────────────────────────────┤     │
  │ SASS Encoding Tables       ████████████████████████████████████████   │7.0MB│
  │ (per-instruction encoders, ████████████████████████████████████████   │ 28% │
  │  decoders, descriptors)    ████████████████████████████████████████   │~8000│
  ├────────────────────────────┴──┬───────────────────────────────────────┤     │
  │ Operand Dispatch + IR         ███████████                             │1.5MB│
  │ (setOperandField,             ███████████                             │  6% │
  │  getOperandField,             ███████████                             │ ~40 │
  │  NVInst accessors)            ███████████                             │     │
  ├───────────────────────────────┴──┬────────────────────────────────────┤     │
  │ PTX Assembler Frontend           ████████████████                     │3.0MB│
  │ (parsers, validators,            ████████████████                     │ 12% │
  │  builtins, handlers)             ████████████████                     │~800 │
  ├──────────────────────────────────┴──┬─────────────────────────────────┤     │
  │ Backend Compiler Core               ██████████████████████████████   │6.0MB│
  │ (scheduler, regalloc, codegen,      ██████████████████████████████   │ 23% │
  │  ABI, ELF builder, DWARF,          ██████████████████████████████   │~800 │
  │  scoreboard, liveness)              ██████████████████████████████   │     │
  └─────────────────────────────────────┴─────────────────────────────────┘
                                   Legend: ░ = linker-specific
                                           █ = embedded ptxas compiler
```

> **Cross-reference**: The embedded ptxas backend in nvlink is a subset of the standalone ptxas v13.0.88 binary (37.7 MB, 40,185 functions). The standalone [ptxas wiki](../ptxas/binary-layout.html) documents the same three-subsystem decomposition (PTX Frontend / Ori Optimizer / SASS Backend) in full detail. The ISel mega-hubs, encoding tables, and register allocator within nvlink correspond to the SASS Backend subsystem of standalone ptxas, while the PTX assembler frontend in nvlink zones 13--14 maps to ptxas's PTX Frontend subsystem. The standalone ptxas includes additional optimization passes (the "Ori Optimizer" at 5.8 MB) that are absent from nvlink's embedded copy, since nvlink relies on libnvvm for IR optimization during LTO.

### Per-Zone Statistics

| Zone | Address Range | Size | Functions | Avg Size | Code Density | Dominant Pattern |
|---|---|---|---|---|---|---|
| 1 | `0x4038C0`--`0x470000` | 430 KB | ~160 | 2.7 KB | Medium | Large control-flow functions (main=58 KB) |
| 2 | `0x470000`--`0x530000` | 768 KB | ~411 | 1.9 KB | Medium | Config/init/debug infrastructure |
| 3 | `0x530000`--`0x620000` | 960 KB | ~1,544 | 636 B | High | ISel patterns (1,293 identical-template) |
| 4 | `0x620000`--`0x7A0000` | 1.5 MB | ~1,068 | 1.4 KB | Very high | 128-bit instruction encoders (template) |
| 5 | `0x7A0000`--`0x920000` | 1.5 MB | ~1,139 | 1.3 KB | Very high | Encoders + descriptor initializers |
| 6 | `0x920000`--`0xA70000` | 1.3 MB | ~954 | 1.4 KB | Very high | Descriptor init + operand dispatch |
| 7 | `0xA70000`--`0xB80000` | 1.1 MB | ~303 | 3.6 KB | Medium | Multi-arch codec functions |
| 8 | `0xB80000`--`0xCA0000` | 1.1 MB | ~332 | 3.3 KB | Medium | SM7x/SM80 encode/decode |
| 9 | `0xCA0000`--`0xDA0000` | 1.0 MB | ~413 | 2.5 KB | Medium | SM80 ISel + mega-hub |
| 10 | `0xDA0000`--`0xF16000` | 1.5 MB | ~1,088 | 1.4 KB | Very high | Blackwell codec table 2 |
| 11 | `0xF16000`--`0x100C000` | 984 KB | ~314 | 3.1 KB | Medium | SM75 ISel + mega-hub |
| 12 | `0x100C000`--`0x11EA000` | 1.9 MB | ~978 | 2.0 KB | High | Shared encoders + SM89/90 |
| 13 | `0x11EA000`--`0x1430000` | 2.3 MB | ~3,310 | 712 B | High | ISel clones + LTO pipeline |
| 14 | `0x1430000`--`0x16E0000` | 2.8 MB | ~1,034 | 2.8 KB | Medium | PTX frontend + builtins |
| 15 | `0x16E0000`--`0x1A00000` | 3.2 MB | ~1,126 | 2.9 KB | Medium | Scheduler + regalloc + SASS tables |
| 16 | `0x1A00000`--`0x1D32172` | 3.2 MB | ~1,239 | 2.6 KB | Medium | Backend + ABI + ELF + DWARF |

**Code density classification**: "Very high" = zones dominated by template-generated functions where every byte is encoding logic with minimal control flow. "High" = many small pattern-matcher or clone functions. "Medium" = mix of large complex functions and small helpers.

### Major Function Clusters

The binary contains several distinct function "clusters" -- groups of 50+ functions with identical structure, generated from the same template or macro expansion.

| Cluster | Zone(s) | Count | Template Size | Identity |
|---|---|---|---|---|
| SM100+ SASS encoders | 4, 5 | 1,537 | 4--9 KB each | `sub_4C28B0`-based bitfield insertion, 128-bit instructions |
| SM100+ SASS decoders | 10 | 648 | 3--8 KB each | Mirror of encoder set, binary-to-IR translation |
| InstrDesc initializers | 5, 6 | 1,613 | 1--3 KB each | Operand count/type/constraint/latency metadata |
| SM50-7x ISel patterns | 3 | 1,293 | 1--3 KB each | `(ctx, node, &pattern_id, &priority)` template |
| SM80 ISel patterns | 9 | 259 | 1--3 KB each | Same template, Ampere-specific opcodes |
| SM75 ISel patterns | 11 | 276 | 1--3 KB each | Same template, Turing-specific opcodes |
| SM89/90 ISel patterns | 12 | ~160 | 1--3 KB each | Same template, Ada/Hopper-specific opcodes |
| ISel parametric clones | 13 | ~500 | 1--2 KB each | Per-SM-variant copies of shared patterns |
| Shared encoders | 12 | ~750 | 1--2 KB each | Template-instantiated SASS encoding table entries |
| Builtin code-template gen | 14 | ~250 | 3--5 KB each | CUDA builtin lowering functions |

These 10 clusters account for approximately 7,286 functions (18% of the total) but consume roughly 15 MB of `.text` (60% of code bytes). Template-generated code dominates the binary.

## Function Size Distribution

| Size Bucket | Count | % | Notes |
|---|---|---|---|
| < 100 B | 11,677 | 28.8% | IR node accessors, type predicates, identity functions |
| 100 B -- 1 KB | 20,601 | 50.8% | ISel pattern matchers, encoding helpers, small utilities |
| 1 -- 5 KB | 7,957 | 19.6% | Instruction encoders/decoders, operand emitters |
| 5 -- 20 KB | 262 | 0.65% | Pipeline drivers, complex validators, pass entry points |
| 20 -- 100 KB | 27 | 0.07% | Main functions, scheduling drivers, mega-dispatchers |
| 100 -- 280 KB | 8 | 0.02% | ISel mega-hubs, builtin prototype DB (see below) |

## The Five ISel Mega-Hubs

Five functions exceed 160 KB each. They are the top-level instruction selector dispatch functions for different SM architecture generations. Each contains a massive jump table that calls hundreds of ISel pattern matchers in sequence, selects the highest-priority match, then dispatches to the corresponding emitter. These functions are **too large for Hex-Rays** to decompile.

| Address | Size | Target SM | Subsystem |
|---|---|---|---|
| `sub_FBB810` | 280 KB | SM75 (Turing) | ISel dispatch -- calls 276+ pattern matchers |
| `sub_126CA30` | 239 KB | SM50-7x (Maxwell/Pascal/Volta) shared | ISel dispatch for pre-Turing backends |
| `sub_D5FD70` | 239 KB | SM80 (Ampere) | ISel dispatch for Ampere-class GPUs |
| `sub_119BF40` | 231 KB | SM89/90 (Ada/Hopper) | ISel dispatch for Ada Lovelace / Hopper |
| `sub_5B1D80` | 204 KB | SM50-7x (MercExpand) | MercExpand instruction expansion dispatch |

Each mega-hub sits at the boundary between ISel pattern matchers (hundreds of small ~3 KB functions) and instruction emitters. The pattern matchers feed `(pattern_id, priority)` pairs to the hub, which selects the best match and dispatches to an emission function.

Other large functions outside ISel hubs:

| Address | Size | Identity |
|---|---|---|
| `sub_15903D0` | 165 KB | CUDA builtin code-template generator (SM variant) |
| `sub_1638800` | 118 KB | PTX intrinsic lowering dispatch |
| `sub_146BEC0` | 51 KB | `ptx_load_store_validator` -- memory operation validator |
| `sub_1487650` | 47 KB | `ptx_statement_processor` -- top-level PTX statement handler |
| `sub_15B86A0` | 34 KB | `cuda_builtin_prototype_generator` -- 608-case switch on builtin index |
| `sub_147EF50` | 28 KB | `ptx_instruction_semantic_analyzer` -- master instruction validator |

## Complete .text Address Map

The following table maps every region of the `.text` section to its identified subsystem, based on the 20 sweep reports covering `0x400000` through `0x1D32172`. Adjacent regions with identical function are merged where possible.

### Linker Core (0x400000 -- 0x530000, ~1.2 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x400000`--`0x4038C0` | 14 KB | ELF header, PLT stubs | ~156 | PLT entries for 156 dynamic imports |
| `0x4038C0`--`0x409800` | 24 KB | CRT init, startup | ~30 | `_start`, `__libc_start_main` trampoline |
| `0x409800`--`0x427AE0` | 122 KB | **nvlink main** | 1 | `main()`: 58 KB / 1936 lines, file-type dispatch loop |
| `0x427AE0`--`0x432000` | 41 KB | CLI option parsing | ~10 | `nvlink_parse_options()`: 30 KB, ~60 option registrations |
| `0x432000`--`0x445000` | 76 KB | Memory arena, ELF mgmt | ~40 | Arena allocator, ELF section/symbol management |
| `0x445000`--`0x469D60` | 150 KB | Merge/layout/relocate | ~50 | `merge_elf`, `layout`, relocation engine |
| `0x469D60`--`0x470000` | 25 KB | Finalize, output | ~20 | Callgraph, dead-code elimination, output write |

### Finalization + Architecture + JIT Pipeline (0x470000 -- 0x530000, ~768 KB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x470000`--`0x471700` | 6 KB | Architecture compat | ~8 | `can_finalize_architecture_check`, sm_100/103/110/120/121 maps |
| `0x471700`--`0x4748F0` | 12 KB | **Finalization orchestrator** | 1 | `nvlink_finalize_object`: 78 KB, 460+ locals, key-value config |
| `0x4748F0`--`0x476FB0` | 10 KB | Link+finalize entry | 1 | Top-level 25-param entry: mercury/capmerc/sass binary kinds |
| `0x476FB0`--`0x488530` | 93 KB | Debug info, line tables | ~30 | DWARF-like debug encoding/decoding, file tables |
| `0x488530`--`0x4BC6F0` | 209 KB | Knobs, archive, nvvm | ~60 | Knobs infrastructure, .a parsing, libNVVM integration layer |
| `0x4BC6F0`--`0x4C2A60` | 25 KB | LTO + PTX JIT entry | ~10 | `compile_linked_lto_ir`, `ptxas_jit_compile` entry points |
| `0x4C2A60`--`0x530000` | 843 KB | Encoding infrastructure | ~40 | `setBitfield`, `encodeOperand`, init helpers -- shared by all SM |

### IR Node Primitives + SM50-7x ISel (0x530000 -- 0x620000, ~960 KB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x530E80`--`0x530FD0` | <1 KB | **IR node accessors** | 22 | `sub_530FB0` has 31,399 callers -- universal operand accessor |
| `0x530FE0`--`0x5B1AB0` | 523 KB | ISel pattern matchers | 1,293 | SM50-7x backend: 152 target opcodes, 36 priority levels |
| `0x5B1D80`--`0x5E4470` | 204 KB | MercExpand mega-hub | 1 | MercExpand dispatch + CFG analysis + bitvector ops |
| `0x5E4470`--`0x600260` | 114 KB | MercExpand engine | ~50 | Node creation, tree walker, register constraint propagation |
| `0x603F60`--`0x61FA60` | 112 KB | SM50 instruction encoders | 79 | Per-instruction binary encoding functions |

### SM100+ (Blackwell) SASS Encoding (0x620000 -- 0x7A0000, ~1.5 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x6200A0`--`0x79FAF0` | 1.5 MB | **Blackwell SASS encoders** | 1,068 | 128-bit instruction encoders for SM100+ ISA |

Each function encodes one instruction variant into a 128-bit instruction word. Opcode breakdown: major=1 (ALU/Scalar) 37%, major=2 (Vector/Mem/Ctrl) 63%, across 118 instruction families.

### SM100+ Encoding Cont. + Descriptor Table (0x7A0000 -- 0x920000, ~1.5 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x7A0000`--`0x84DD70` | 700 KB | SM100+ SASS encoders (tail) | 469 | Continuation of Blackwell encoding table |
| `0x84DD70`--`0x920000` | 750 KB | **InstrDesc init table** | 670 | Instruction descriptor initializers -- operand types, latencies |

Combined encoding table: `0x620000`--`0x84DD70` (1,537 encoders). Combined descriptor table starts at `0x84DD70`.

### Descriptor Table + Operand Dispatch (0x920000 -- 0xA70000, ~1.3 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x920240`--`0xA48290` | 1.1 MB | InstrDesc init table (cont.) | 943 | Covers opcode IDs 15--365+; most common: 18 (IMAD), 56 (FFMA) |
| `0xA49010`--`0xA4AB10` | 4 KB | NVInst accessors | ~30 | IR instruction class hierarchy, `sub_A49150` (30,768 callers) |
| `0xA4AB10` | 11 KB | NVInst constructor | 1 | Allocates and initializes instruction IR node |
| `0xA4B5E0`--`0xA4C7C0` | 5 KB | FNV-1a hash tables | 4 | Instruction lookup by hash |
| `0xA5B6B0` | 180 KB | `setOperandField` dispatch | 1 | Giant switch: sets operand fields by opcode class |
| `0xA62220` | 65 KB | `setOperandImm` dispatch | 1 | Giant switch: sets immediate operand values |
| `0xA65900` | 67 KB | `getOperandField` dispatch | 1 | Giant switch: reads operand fields by opcode class |
| `0xA67910` | 141 KB | `getDefaultOperandValue` dispatch | 1 | Giant switch: returns default operand values per opcode |

### Instruction Codec -- Multi-Arch (0xA70000 -- 0xCA0000, ~2.2 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0xA709F0` | 54 KB | Field offset query | 1 | 6,491-line switch: `(opcode_class, field_id) -> bit_offset` |
| `0xA7DE70` | 50 KB | Field presence query | 1 | Mirror of A709F0: returns `hasField` boolean |
| `0xA87CE0`--`0xB25D50` | 630 KB | Per-opcode encoders | ~164 | SM90/100 instruction binary encoders |
| `0xACECF0`--`0xB77B60` | 700 KB | Per-opcode decoders | ~139 | Binary-to-IR instruction decoders |
| `0xB80000`--`0xB9FDE0` | 128 KB | Multi-arch utilities | ~20 | Register pair validators, operand class computation |
| `0xB9FDE0`--`0xBC2CC0` | 142 KB | **SM7x (Volta/Turing) codecs** | ~60 | Instruction encoders + decoders for SM70/SM75 |
| `0xBC3FC0`--`0xBFEC10` | 236 KB | SM75 extended codecs | ~80 | Turing-specific instruction encoding variants |
| `0xC00070`--`0xC2FB60` | 193 KB | **SM80 (Ampere) codecs** | ~70 | Ampere instruction encoders |
| `0xC3D540`--`0xC50970` | 83 KB | SM80 decoders | ~15 | HMMA tensor core, SHF, memory decoders |
| `0xC7EC90`--`0xC9EE60` | 131 KB | **SM86/89 (Ada) codecs** | ~40 | GA10x / AD10x encoders + decoders |

### SM80 (Ampere) ISel Backend (0xCA0000 -- 0xDA0000, ~1 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0xCA0000`--`0xCDC000` | 240 KB | Operand emission + packing | 137 | Phase 2: operand emitters for 19 SM80 opcodes |
| `0xCDD5F0`--`0xCDD690` | <1 KB | SM80 operand predicates | 15 | `isGPR`, `isPredicate`, `isUniform`, `isImmediate`, etc. |
| `0xCE2000`--`0xD5FD70` | 510 KB | ISel pattern matchers | 259 | SM80 pattern matching functions |
| `0xD5FD70` | 239 KB | **SM80 ISel mega-hub** | 1 | Too large for Hex-Rays |
| `0xD9A400`--`0xDA0000` | 23 KB | Binary instruction encoding | 17 | Phase 3: final SASS binary packing |

### SM100+ (Blackwell) SASS Codec -- Second Table (0xDA0000 -- 0xF16000, ~1.5 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0xDA0310`--`0xE436D0` | 669 KB | Blackwell encoders | 438 | Format 1: 147, Format 2: 290, Format 3: 1 |
| `0xE43C20` | 1 KB | Encoder dispatch | 1 | Routes opcode to specific encoder function |
| `0xE43DC0`--`0xF15A50` | 847 KB | Blackwell decoders | 648 | Mirrors encoder set; handles architecture variants |
| `0xEFE6C0` | 1 KB | Decoder dispatch | 1 | Routes opcode to specific decoder function |

### SM75 (Turing) ISel Backend (0xF16000 -- 0x100C000, ~984 KB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0xF16030`--`0xF160F0` | <1 KB | SM75 operand predicates | 15 | Operand type classifiers (register, immediate, etc.) |
| `0xF10080`--`0xF15A50` | 22 KB | SM75 instruction emitters | 18 | Phase 2: operand emission for Turing instructions |
| `0xF16150`--`0xFBB780` | 678 KB | ISel pattern matchers | 276 | SM75 pattern matching -- linear scan architecture |
| `0xFBB810` | 280 KB | **SM75 ISel mega-hub** | 1 | Largest function in binary. Too large for Hex-Rays |
| `0xFFFDF0`--`0x100BBF0` | 48 KB | Post-ISel emit+encode | 38 | Combined match+emit for complex instructions |

### SM89/90 Backend + Shared Encoders (0x100C000 -- 0x11EA000, ~1.9 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x100C000`--`0x10FFFFF` | 1.0 MB | Shared instruction encoders | ~750 | Template-instantiated SASS encoding table entries |
| `0x1100000`--`0x1120000` | 128 KB | **SM89/90 backend driver** | ~30 | `ptxas_main_compilation_driver` (65 KB), ELF output gen |
| `0x1104950` | 38 KB | ptxas option parser | 1 | ~60 option registrations (gpu-name, opt-level, etc.) |
| `0x1112F30` | 65 KB | Compilation driver main | 1 | Per-module entry: PTX header, codegen callback selection |
| `0x1116890` | 60 KB | ELF output + metadata | 1 | CUBIN generation, JSON metadata tree builder |
| `0x1120000`--`0x119BF40` | 496 KB | ISel pattern matchers | ~160 | SM89/90 instruction selection patterns |
| `0x119BF40` | 231 KB | **SM89/90 ISel mega-hub** | 1 | Too large for Hex-Rays |
| `0x11D4680`--`0x11EA000` | 90 KB | Scheduler + emission | ~16 | Instruction scheduling, register-pressure tracking |

### PTX Frontend + ISel Helpers (0x11EA000 -- 0x12B0000, ~800 KB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x11EA000`--`0x126C000` | 520 KB | ISel pattern-match predicates | ~160 | Shared PTX compiler ISel patterns (all SM targets) |
| `0x126CA30` | 239 KB | **PTX ISel mega-hub** | 1 | Too large for Hex-Rays |
| `0x12A7000`--`0x12AD000` | 24 KB | PTX type system / operand builders | ~15 | Type constructors, operand IR building |
| `0x12AF000`--`0x12B0000` | 4 KB | PTX module initializer | 5 | Creates module-level PTX compilation context |

### LTO Compilation Engine (0x12B0000 -- 0x1430000, ~1.5 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x12B0000`--`0x12BA000` | 40 KB | PTX operand/type system | ~20 | Special registers (%ntid, %laneid, etc.), symbol management |
| `0x12BA000`--`0x12D0000` | 88 KB | ISel lowering passes | ~30 | Instruction lowering for LTO path |
| `0x12D0000`--`0x12D5000` | 20 KB | DWARF debug line info gen | ~5 | Line table emission for LTO-compiled code |
| `0x12D5000`--`0x1400000` | 1.2 MB | ISel pattern clones | ~500 | Parametric clones per SM variant (sm_5x through sm_10x) |
| `0x1400000`--`0x1430000` | 192 KB | LTO pipeline + ELF emit | ~20 | Top-level LTO pipeline, MMA lowering, output |

### PTX Assembler Frontend (0x1430000 -- 0x15C0000, ~1.6 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x1430000`--`0x1442000` | 72 KB | PTX version/SM gates | ~30 | `ptx_version_gate`, `sm_version_gate` helpers |
| `0x1442000`--`0x146BEC0` | 156 KB | Instruction emission handlers | ~80 | Per-instruction PTX code generators |
| `0x146BEC0` | 206 KB | `ptx_load_store_validator` | 1 | Validates ld/st/atom/fence with SM-version checks |
| `0x147EF50` | 288 KB | `ptx_instruction_semantic_analyzer` | 1 | Master validator: checks all SM version requirements |
| `0x1487650` | 240 KB | `ptx_statement_processor` | 1 | Top-level PTX statement handler |
| `0x14932E0`--`0x15B86A0` | 700 KB | Instruction handlers + builtins | ~250 | Code-template generators for CUDA builtins |
| `0x15B86A0` | 345 KB | `cuda_builtin_prototype_generator` | 1 | 608-case switch: sm20 through sm10x builtin prototypes |

### CUDA Compilation Backend (0x15C0000 -- 0x16E0000, ~1.2 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x15C0CE0` | 15 KB | SM dispatch tables | 1 | Registers 7 callback pointers per arch (sm_75 through sm_121) |
| `0x15C44D0`--`0x15CA450` | 348 KB | nv.info attribute emitters | ~10 | Per-SM EIATTR record generation (largest: 78 KB) |
| `0x15CD6A0`--`0x15D5000` | 30 KB | Attribute merging/sorting | ~5 | Cross-function attribute merger |
| `0x1610000`--`0x163FFFF` | 192 KB | PTX compilation frontend | ~40 | Operand handling, control flow, symbol management |
| `0x1640000`--`0x165FFFF` | 128 KB | Codegen operand lowering | ~30 | Atom formatting, offset calculation |
| `0x1660000`--`0x169FFFF` | 256 KB | ISel/scheduling + DWARF | ~40 | Instruction scheduling, peephole, debug emission |
| `0x16A0000`--`0x16DFFFF` | 256 KB | OCG intrinsic lowering | ~80 | `builtin_ocg_*` handlers, tcmma/tensor operations |

### PTX Lexer/Parser + Instruction Scheduler + SASS Tables (0x16E0000 -- 0x1850000, ~1.5 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x16E0000`--`0x16E3AB0` | 12 KB | tcgen05 intrinsic codegen | ~10 | SM100 tensor memory address setup, guardrails |
| `0x16E4D60`--`0x16F6000` | 70 KB | PTX instruction builder | ~20 | Instruction construction, operand insert helpers |
| `0x16F6000`--`0x1740000` | 296 KB | **Tepid instruction scheduler** | ~50 | Full instruction scheduling pipeline |
| `0x175D000`--`0x1768000` | 44 KB | Knobs/config infrastructure | ~15 | Runtime tuning parameters |
| `0x1769000`--`0x1850000` | 924 KB | **SASS opcode tables** | ~150 | SM70-SM120 instruction encoding/emission tables |

ROT13-encoded SASS mnemonics: `SNQQ`=FADD, `SZHY`=FMUL, `VZNQ`=IMAD, `SRAPR`=FENCE, `ZREPHEL`=MERCURY.

### Backend Compiler Core (0x1850000 -- 0x1A00000, ~1.7 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x1850000`--`0x186F000` | 124 KB | **Instruction scheduling** | ~15 | `ScheduleInstructions` (85 KB), ReduceReg, DynBatch, Cutlass |
| `0x1878000`--`0x189C000` | 144 KB | ConvertMemoryToRegister | ~20 | Shared-memory to register promotion pass |
| `0x189C000`--`0x18FC000` | 384 KB | **Register allocation** | ~40 | Spilling, SMEM spilling, multi-class regalloc |
| `0x18FC000`--`0x1920000` | 144 KB | setmaxnreg / CTA-reconfig | ~20 | Blackwell+ register budget negotiation |
| `0x1916000`--`0x1960000` | 296 KB | mbarrier + ORI passes | ~30 | Instruction lowering, copy propagation, dead-code elim |
| `0x1960000`--`0x19E0000` | 512 KB | Codegen verification | ~40 | Uninitialized register detection, rematerialization verify |
| `0x19A0000`--`0x19C0000` | 128 KB | PTX metrics/statistics | ~15 | Occupancy estimation, instruction counts, loop analysis |
| `0x19C0000`--`0x1A00000` | 256 KB | Scheduling/regalloc guidance | ~20 | Guidance output, loop metrics, register validation |

### SASS Backend + ABI + ELF Builder (0x1A00000 -- 0x1B60000, ~1.4 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x1A009C0`--`0x1A0B180` | 6 KB | Bug injection framework | ~5 | Testing framework for intentional bug injection |
| `0x1A0B180`--`0x1A20000` | 84 KB | Instruction operand analysis | ~30 | Operand lowering, constant buffer encoding |
| `0x1A1A000`--`0x1A2A000` | 64 KB | Warp sync / mbarrier | ~15 | `%%mbarrier_%s_%s` instruction generation |
| `0x1A4B000`--`0x1A61090` | 88 KB | WGMMA pipeline analysis | ~20 | Warpgroup MMA live ranges, synchronization injection |
| `0x1A61090`--`0x1A6A480` | 38 KB | Scoreboard management | ~10 | Instruction scheduling scoreboard |
| `0x1A6A480`--`0x1AA2090` | 352 KB | ISel/lowering + encoding | ~80 | Instruction selection, SASS emission |
| `0x1AA2090`--`0x1ABF000` | 124 KB | Regalloc + ABI | ~30 | Register allocation, ABI handling |
| `0x1AEAA90`--`0x1AEE070` | 14 KB | Instruction vtable factory | ~10 | SASS instruction vtable construction |
| `0x1AEE070`--`0x1B00000` | 70 KB | Memory pool diagnostics | ~10 | Pool tracking, encoding passes |
| `0x1B00000`--`0x1B20000` | 128 KB | Register liveness | ~30 | Interference graph construction |
| `0x1B19750`--`0x1B40000` | 160 KB | Machine scheduling + CFG | ~40 | Basic block management, scheduling |
| `0x1B40000`--`0x1B60000` | 128 KB | Dependency tracking | ~30 | Scoreboard / dependency graph management |

### Final Segment: ISel + ABI + ELF + Demangler + DWARF (0x1B60000 -- 0x1D32172, ~1.8 MB)

| Range | Size | Subsystem | Functions | Key Finding |
|---|---|---|---|---|
| `0x1B60000`--`0x1B9FFFF` | 256 KB | ISel + lowering | ~200 | PTX-to-SASS ISel, tail-call optimization |
| `0x1BA0000`--`0x1BFFFFF` | 384 KB | ABI / calling convention | ~150 | Return address mgmt, convergent boundary, coroutine regs |
| `0x1C00000`--`0x1CDFFFF` | 896 KB | **ELF section builder** | ~120 | .nv.constant, .nv.shared, .strtab, cubin/fatbin container |
| `0x1CE0000`--`0x1CEDFFF` | 56 KB | C++ name demangler | ~40 | Itanium ABI + MSVC demangler, templates, RTTI, lambdas |
| `0x1CF0000`--`0x1D32172` | 265 KB | **DWARF + LEB128 + KNOBS** | ~140 | DW_FORM/TAG/AT generation, SSE-accelerated LEB128, ELF finalize |

## Import Table

nvlink dynamically imports exactly **156 symbols** from glibc, libpthread, and libdl. There are **zero C++ mangled symbols** in the import table -- the binary is fully stripped with all C++ internals resolved at static link time.

| Type | Count | Examples |
|---|---|---|
| libc (stdio/stdlib/string) | 112 | `printf`, `malloc`, `free`, `fopen`, `memcpy`, `strcmp`, `getenv` |
| pthread | 20 | `pthread_create`, `pthread_mutex_lock`, `pthread_cond_wait` |
| GCC unwinder | 12 | `_Unwind_Resume`, `_Unwind_GetIPInfo`, `_Unwind_SetGR` |
| math (libm) | 9 | `sqrt`, `sin`, `cos`, `pow`, `log`, `floor`, `ceil`, `expf` |
| dl (dynamic loading) | 3 | `dlopen`, `dlsym`, `dlclose` |

The `dlopen`/`dlsym`/`dlclose` imports are used for loading `libnvvm` at runtime during LTO compilation. nvlink does not statically link libnvvm -- it discovers and loads it dynamically via the `--nvvmpath` option.

## .rodata Composition

The 7.2 MB `.rodata` section (`0x1D321A0`--`0x2463BB0`) holds:

| Content | Approx. Size | Description |
|---|---|---|
| Instruction encoding templates | ~4 MB | `xmmword_*` constants: 128-bit opcode templates OR'd into instruction words |
| Operand descriptor tables | ~1.5 MB | Per-instruction operand type/constraint/modifier arrays (10 DWORDs x 3) |
| String literals | ~1 MB | Error messages, option names, ROT13 mnemonics, format strings |
| Lookup tables | ~0.5 MB | Architecture dispatch tables, opcode-to-handler mappings |
| Miscellaneous | ~0.2 MB | Alignment padding, small constant arrays |

## .bss Layout

The 99 KB `.bss` section (`0x2A5F180`--`0x2A77DD8`) stores runtime state:

- `0x2A5F2xx`--`0x2A5F3xx`: Global option variables (set by `nvlink_parse_options`)
- `0x2A5F330`: Input file linked list head
- `0x2A644B8`--`0x2A644C0`: SM dispatch hash map pointers (7 tables)
- Remaining: Thread-local storage, compilation state, hash table backing stores

## Key Observations for Navigation

1. **Address heuristic**: If you are at an address below `0x530000`, you are likely in linker-specific code. Above `0x530000`, you are almost certainly in the embedded ptxas compiler backend.

2. **Pattern recognition**: Functions in the range `0x530FE0`--`0x5B1AB0`, `0x11EA000`--`0x126C000`, and `0xCE2000`--`0xD5FD70` are ISel pattern matchers. They all follow the identical `(ctx, node, &pattern_id, &priority)` signature with nested attribute checks.

3. **Encoding tables**: The massive contiguous blocks at `0x620000`--`0x84DD70` and `0xDA0000`--`0xF15A50` are instruction encoder/decoder tables. Each function is 4--9 KB, follows a rigid template, and differs only in opcode constants and operand layouts. Analyzing one is sufficient to understand all of them.

4. **Four operand dispatch switches**: `sub_A5B6B0` (27 KB), `sub_A62220` (14 KB), `sub_A65900` (8 KB), `sub_A67910` (36 KB) are the operand read/write dispatch functions. They map `(opcode_class, field_id)` to specific field accessors. If you see a call to `sub_A49150`, it routes through these.

5. **The 31K-caller function**: `sub_530FB0` (get operand by index) is the most-called function in the binary with 31,399 callers. `sub_A49150` (get instruction attribute) is second with 30,768 callers. These are the IR accessor layer that every backend function uses.

6. **ROT13 strings**: NVIDIA obfuscates SASS instruction mnemonics via ROT13. When you see strings like `VZNQ`, `SZHY`, `SNQQ`, `ZREPHEL` in `.rodata`, apply ROT13 to decode: IMAD, FMUL, FADD, MERCURY.
