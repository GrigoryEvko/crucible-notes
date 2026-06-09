# Binary Layout

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

PTXAS v13.0.88 is a 37,741,528-byte stripped x86-64 ELF executable. Its `.text` section spans 26.2 MB (`0x403520`--`0x1CE2DE2`) containing 40,185 functions (HIGH — direct ELF header read; function count from IDA auto-analysis). This page maps every byte of the binary to the subsystem that owns it, derived from all 40 sweep reports covering the complete address range.

## ELF Section Map

| Section | Address | Size | Notes |
|---|---|---|---|
| `.plt` | `0x402C00` | 2,336 B (146 stubs) | Procedure linkage table for libc/libpthread imports |
| `.text` | `0x403520` | 26,212,546 B (26.2 MB) | All executable code — 40,185 functions |
| `.rodata` | `0x1CE2E00` | 7,508,368 B (7.5 MB) | Read-only data: encoding tables, strings, DFA tables |
| `.eh_frame_hdr` | `0x240BF90` | 358,460 B (350 KB) | Exception handling frame index |
| `.eh_frame` | `0x2664A60` | 3,751,640 B (3.7 MB) | Unwinding data for 40K functions |
| `.gcc_except_table` | `0x29F8938` | 940 B | C++ exception filter tables |
| `.ctors` | `0x29F8CE8` | 104 B (12 entries) | Static constructor table |
| `.data.rel.ro` | `0x29F8D60` | 4,256 B | Vtable pointers, resolved at load time |
| `.got.plt` | `0x29FA000` | 1,184 B (148 entries) | Global offset table for PLT |
| `.data` | `0x29FA4A0` | 14,032 B (13.7 KB) | Initialized globals: function pointers, defaults |
| `.bss` | `0x29FDB80` | 85,864 B (83.9 KB) | Zero-init globals: knob tables, TLS keys, mutexes |

Total file composition:

| Component | Size | Percentage |
|---|---|---|
| `.text` | 26.2 MB | 69.4% |
| `.rodata` | 7.5 MB | 19.9% |
| `.eh_frame` + `.eh_frame_hdr` | 4.0 MB | 10.7% |
| `.data` + `.bss` + other | 0.1 MB | 0.3% |

## Program Headers

| Segment | VirtAddr | MemSiz | Flags | Contents |
|---|---|---|---|---|
| LOAD 0 | `0x400000` | 32.4 MB | R E | `.text` + `.rodata` + headers + `.eh_frame_hdr` |
| LOAD 1 | `0x2664A60` | 3.7 MB | RW | `.eh_frame` + `.data` + `.bss` + `.got` |
| GNU_RELRO | `0x2664A60` | 3.6 MB | R | Read-only after relocation (`.eh_frame` through `.data.rel.ro`) |
| GNU_EH_FRAME | `0x240BF90` | 350 KB | R | Exception handling index |
| GNU_STACK | `0x0` | 0 | RW | Non-executable stack |

Entry point: `0x42333C` (ELF `e_entry`), which is inside `.text` (the CRT startup stub `_start`). The actual `main` is at `0x409460`.

## Three Subsystems

The `.text` section decomposes into three subsystems with distinct coding styles, data structures, and origins:

```text
  .text linear address map (26.2 MB)
  0x403520                 0x67F000        0xC52000                          0x1CE2DE2
  |--- PTX Frontend 2.9 MB ---|-- Ori Optimizer 5.8 MB --|---- SASS Backend 17.6 MB ----|
  |          11%               |          22%             |              67%              |
  |  parsers, validators,      | passes, regalloc,        | encoding handlers, ISel,      |
  |  intrinsics, formatters    | scheduling, CFG analysis  | peephole, codecs, ABI, ELF    |
```

| Subsystem | Address Range | Size | Functions | Share | Avg Fn Size | Largest Function |
|---|---|---|---|---|---|---|
| PTX Frontend | `0x403520`--`0x67F000` | 2.9 MB | ~2,592 | 11% | ~1,170 B | `sub_46E000` (93 KB, opcode table builder) |
| Ori Optimizer | `0x67F000`--`0xC52000` | 5.8 MB | ~11,001 | 22% | ~550 B | `sub_926A30` (155 KB decomp, interference graph) |
| SASS Backend | `0xC52000`--`0x1CE2DE2` | 17.6 MB | ~26,592 | 67% | ~690 B | `sub_169B190` (280 KB, master ISel dispatch) |

The backend dominates the binary because SASS instruction encoding is template-generated code: each of the ~4,000 encoding handler functions is a standalone vtable entry, never called directly. The optimizer has the highest function density (many small pass helpers), while the frontend has the largest average function size (complex validators and parsers).

## Complete .text Address Map

The table below maps every address range in the `.text` section to its subsystem, function count, and key entry points. Data is aggregated from the 30 sweep partitions (p1.01 through p1.30).

### PTX Frontend (`0x403520`--`0x67F000`, 2.9 MB)

> **Note on the `0x400000`--`0x403520` gap.** The LOAD segment begins at `0x400000`, but the first 13.6 KB before `.text` contains the ELF header (64 B at `0x400000`), program headers (7 entries, 392 B), `.interp` (28 B, path to `ld-linux-x86-64.so.2`), `.hash` / `.gnu.hash` (symbol hash tables), `.dynsym` / `.dynstr` (dynamic symbol table, 146 entries), `.gnu.version` / `.gnu.version_r` (symbol versioning), `.rela.plt` (PLT relocations, 146 entries), and the `.plt` stub table (2,336 B, 146 stubs at `0x402C00`--`0x403520`). These are standard ELF infrastructure, not ptxas application code. The first ptxas function begins at `0x403520`.

| Address Range | Size | Functions | Subsystem | Key Functions |
|---|---|---|---|---|
| `0x403520`--`0x430000` | 178 KB | ~300 | Runtime infrastructure: pool allocator, hash maps, TLS, diagnostics, error reporting, string utilities | `sub_424070` (pool alloc, 3809 callers), `sub_4280C0` (TLS context, 3928 callers), `sub_426150` (hash insert, 2800 callers), `sub_42FBA0` (diagnostic emitter, 2350 callers), `sub_427630` (MurmurHash3) |
| `0x430000`--`0x460000` | 200 KB | ~120 | CLI parsing and compilation driver: option registration, argument parser, target configuration, register/resource constraints, Chrome trace JSON parser | `sub_446240` (real main, 11 KB), `sub_432A00` (option registration, 6 KB), `sub_434320` (option parser, 10 KB), `sub_43B660` (register constraint calc), `sub_439880` (trace JSON parser) |
| `0x460000`--`0x4D5000` | 470 KB | ~350 | PTX instruction validators: per-opcode semantic checkers for MMA, WMMA, load/store, cvt, atomics, barriers, tensormap, async copy | `sub_4B2F20` (general validator, 52 KB), `sub_4CE6B0` (Bison parser, 48 KB), `sub_4C5FB0` (operand validator, 28 KB), `sub_4C2FD0` (WMMA/MMA validator, 12 KB), `sub_4A73C0` (tensormap validator, 11 KB) |
| `0x4D5000`--`0x5AA000` | 872 KB | 581 | PTX instruction text generation: 580 per-opcode formatters that convert internal IR to PTX assembly text, plus a built-in function declaration emitter | `sub_5D4190` (formatter dispatch, 13 KB), `sub_5FF700` (builtin decl emitter, 34 KB), ~580 formatter functions (avg 1.5 KB each) |
| `0x5AA000`--`0x67F000` | 874 KB | 628 | Intrinsic infrastructure: 608 CUDA intrinsic handlers, MMA/WMMA/tcgen05 tensor core codegen, SM profile tables (sm\_75 through sm\_121), special register init, ELF/DWARF finalization, memory space management | `sub_5D1660` (608 intrinsics, 11 KB), `sub_607DB0` (SM profile hash maps, 4 KB), `sub_6765E0` (arch capability constructor, 7.6 KB), `sub_612DE0` (version string) |

### Ori Optimizer (`0x67F000`--`0xC52000`, 5.8 MB)

| Address Range | Size | Functions | Subsystem | Key Functions |
|---|---|---|---|---|
| `0x67F000`--`0x754000` | 869 KB | ~500 | Mercury SASS backend core: scheduling engine (ReduceReg/DynBatch, 9 reg pressure counters), WAR hazard management, Opex (operand expansion) pipeline, OCG intrinsic lowering, instruction encoding core, Flex DFA scanner, ELF section helpers | `sub_688DD0` (scheduler engine, 3.6 KB), `sub_6D9690` (encoding switch, 27 KB), `sub_6FC240` (WAR/scoreboard), `sub_720F00` (Flex scanner, 16 KB, 552 rules) |
| `0x754000`--`0x829000` | 872 KB | 1,545 | Knobs infrastructure (1,294 entries) and peephole optimizer class: knob lookup/read/file parsing, `PeepholeOptimizer` with 7 virtual methods (`Init`, `RunOnFunction`, `RunOnBB`, `RunPatterns`, `SpecialPatterns`, `ComplexPatterns`, `SchedulingAwarePatterns`), pipeline orchestrator, Mercury operand registration helpers | `sub_79B240` (GetKnobIndex), `sub_79D070` (ReadKnobsFile), `sub_7A5D10` (PeepholeOptimizer), `sub_7BD3C0`/`sub_7BD650`/`sub_7BE090` (operand registrars), `sub_7BD260` (encoding finalize) |
| `0x829000`--`0x8FE000` | 872 KB | 1,069 | Debug line tables, scheduler core, and HW profiles: `ScheduleInstructions` pipeline (context setup, priority computation, reverse scheduling, register budget with occupancy optimization), ROT13 SASS mnemonic table, architecture-specific latency/throughput profiles, constant bank naming, peephole/legalization passes, cutlass-aware scheduling heuristics | `sub_8BF000`--`0x8D1600` (ScheduleInstructions), `sub_896D50` (ROT13 SASS mnemonics), `sub_8F0D00` (HW latency profiles), `sub_8F4820` (cutlass heuristics) |
| `0x8FE000`--`0x9D3000` | 872 KB | 1,090 | Register allocator: fatpoint algorithm core, interference graph builder (155 KB decompiled — largest non-dispatch function), spill/refill mechanism, live range analysis, retry with reduced register count, memory-to-register promotion, `ConvertMemoryToRegisterOrUniform` pass | `sub_926A30` (interference graph, 155 KB decomp), `sub_957160` (fatpoint core), `sub_95DC10` (regalloc driver), `sub_9714E0` (failure handler + retry), `sub_910840` (ConvertMemoryToRegister) |
| `0x9D3000`--`0xAA8000` | 860 KB | 1,218 | Post-RA pipeline phases: `NamedPhases` registry (`OriPerformLiveDead`, `OriCopyProp`, `shuffle`, `swap1`--`swap6`), DAG/dependency analysis, IR statistics printer (instruction count, reg count, estimated latency, spill bytes, occupancy, throughput), hot/cold split, mbarrier intrinsics, regalloc verification, uninitialized register detection | `sub_9F4040` (NamedPhases registry), `sub_A3A7E0` (IR stats printer), `sub_A0B5E0` (uninitialized reg detector), `sub_A9EDB0` (mbarrier/scheduling, 85 KB decomp) |
| `0xAA8000`--`0xB7D000` | 862 KB | 4,493 | GMMA/WGMMA pipeline optimizer, ISel, and instruction emission: GMMA register allocation, warpgroup sync injection, instruction emission helpers (SASS encoder dispatch), post-scheduling IR statistics, operand legalization, 1,269 tiny vtable dispatchers (~160 bytes each), live range analysis, master lowering dispatcher | `sub_AED3C0` (master ISel/template lowering dispatcher, 28 KB native / ~140 KB decomp — not the scheduler; see [templates.md](codegen/templates.md)), `sub_AF7DF0`/`sub_AF7200` (register decode helpers), ~1,269 vtable dispatchers |
| `0xB7D000`--`0xC52000` | 870 KB | 1,086 | CFG analysis, bitvectors, and IR manipulation: ~390 instruction operand pattern matchers, bitvector dataflow framework (alloc, OR, AND, XOR, clear, iterate), CFG analysis (edge printing, reverse post-order, DOT graph dump), scoreboard and instruction classification, sync analysis | `sub_BDC000` (bitvector infra), `sub_BDE8B0` (CFG/RPO/DOT), `sub_BE2E40` (scoreboard classification), ~390 operand pattern matchers |

### SASS Backend (`0xC52000`--`0x1CE2DE2`, 17.6 MB)

| Address Range | Size | Functions | Subsystem | Key Functions |
|---|---|---|---|---|
| `0xC52000`--`0xD27000` | 853 KB | 1,053 | PhaseManager (159 phases): phase factory (159-case switch), phase vtable table at `off_22BD5C8`, default phase ordering table at `0x22BEEA0`, 530 encoding table initialization bodies, instruction handler vtable bodies | `sub_C60D30` (phase factory), `sub_C62720` (PhaseManager constructor), `sub_C60D20` (default table pointer), ~530 phase table body functions |
| `0xD27000`--`0xDFC000` | 853 KB | 592 | SASS encoder table (SM100 Blackwell, set 1): 592 uniform template-generated encoding handlers, each packing operands into a 1,280-bit instruction word at `a1+544`. Covers 60 opcode classes across 16 format groups. All vtable-dispatched (zero direct callers). | 592 per-variant handlers (avg 1,473 B), `sub_7B9B80` (bitfield insert helper) |
| `0xDFC000`--`0xED1000` | 877 KB | 591 | SASS encoder/decoder (SM100 Blackwell, set 2): 494 encoders translating IR to packed SASS bitfields, plus 97 decoders for the reverse direction (disassembly/validation). All vtable-dispatched. | 494 encoders (`0xDFC`--`0xEB2`), 97 decoders (`0xEB3`--`0xED0`), `sub_E0F370` (largest, 3.1 KB) |
| `0xED1000`--`0xFA6000` | 860 KB | 683 | SM100 SASS encoders (set 3): 683 per-variant encoding handlers for 59 SASS opcodes. Each sets opcode ID, loads 128-bit format descriptor via SSE, initializes 10-slot register class map, registers operands, finalizes, extracts bitfields. | 683 template-generated handlers, 128-bit xmmword format descriptors |
| `0xFA6000`--`0x107B000` | 851 KB | 678 | SM100 SASS encoders (set 4): 587 primary encoders (opcodes 16–372, predicate/comparison/memory/tensor/control flow), plus 91 alternate-form encoders for dual-width or SM-variant instruction encodings. Combined with sets 1–3: **2,544 SM100 encoding handlers total**. Six mega dispatch tables. | 587 primary + 91 alternate-form encoders, 6 dispatch tables |
| `0x107B000`--`0x1150000` | 853 KB | 3,396 | SM100 codec completion: 641 final encoding handlers, 78 object lifecycle and scheduling support functions (FNV-1a hash, instruction construction), 2,095 bitfield accessor functions (machine-generated read/write primitives for the packed encoding format). Seven core extractors handle 1-bit, 2-bit, and multi-bit fields across 192-bit words. | `sub_10AFF80` (instruction constructor, 2.7 KB, 32 params), 2,095 bitfield accessors, 7 core extractors |
| `0x1150000`--`0x1225000` | 852 KB | 733 | SASS codec (decoders + encoders): both directions of the instruction codec for an older SM target (likely sm\_89 Ada Lovelace or sm\_90 Hopper). Decoders read 128-bit words and extract fields; encoders pack fields back. Three mega-decoders (29–33 KB each) and two mega-dispatchers (78–104 KB, too large for Hex-Rays). | 3 mega decoders (29–33 KB), 2 mega dispatchers (78–104 KB), 728 of 733 vtable-dispatched |
| `0x1225000`--`0x12FA000` | 860 KB | 1,552 | Register-pressure scheduling + ISel + encoders: register-pressure-aware instruction scheduling (`0x1225`--`0x1240`), instruction selection and emission pipeline (`0x1240`--`0x1254`), 982 SASS binary encoders packing operand fields into 128-bit words (`0x1254`--`0x12FA`). All encoders vtable-dispatched. | Scheduling at `0x1225`--`0x1240`, ISel at `0x1240`--`0x1254`, 982 encoding handlers |
| `0x12FA000`--`0x13CF000` | 845 KB (native) | 1,282 | Operand legalization and peephole: 522 per-instruction bit-field encoders (366 KB native), 186 peephole pattern matchers (81 KB native), 11 operand legalization/materialization functions (40 KB native), 38 operand encoding emitters (31 KB native), 8 live-range analysis functions (14 KB native). | `sub_137B790` (operand legalization, 8,475 B native), 186 peephole matchers, 522 encoders |
| `0x13CF000`--`0x14A4000` | 844 KB (native) | 1,219 | SM120 (RTX 50-series) peephole pipeline: 1,087 instruction pattern matchers (429 KB native), one 233 KB native master opcode dispatch switch (`sub_143C440`, 373-case primary switch), 123 instruction encoders (180 KB native). Pattern matchers validate opcode, modifiers, and operand types; dispatch rewrites opcode byte and operand mapping. | `sub_143C440` (238,922 B native dispatch, 373-case switch), 1,087 pattern matchers, 123 encoders |
| `0x14A4000`--`0x1579000` | 852 KB | 606 | Blackwell ISA encode/decode: 332 encoder functions (`0x14A4`--`0x1520`) packing SASS bitstreams, 1 dispatcher (vtable router at `0x15209F0`), 273 decoder functions (`0x1520`--`0x1578`) unpacking bitstreams and validating fields. Encoder state struct is 600+ bytes with 128-bit format descriptor at `+8`, operand arrays at `+24`--`+143`. | 332 encoders, 273 decoders, 1 dispatcher |
| `0x1579000`--`0x164E000` | 852 KB | 1,324 | SASS encoding + peephole matchers: Zone A has 367 instruction encoders, Zone B has 78 utility/transition functions, Zone C has 469 peephole pattern matchers. All pattern matchers are called from a single 280 KB mega-dispatcher (`sub_169B190`). | 367 encoders, 469 peephole matchers, 78 utilities |
| `0x164E000`--`0x1723000` | 873 KB | 899 | ISel pattern matching core: 762 PTX opcode pattern matchers (Zone A), the master dispatch function `sub_169B190` at 280 KB / 66K instructions (Zone B — **the single largest function in the binary**), 100 encoding table entries, and 36 multi-instruction template expanders. The dispatch tries every matcher, selects the highest-scoring match, and records which SASS expansion template to use. | `sub_169B190` (280 KB, 66K insns, 15,870 callees), 762 matchers, 36 template expanders |
| `0x1723000`--`0x17F8000` | 852 KB | 631 | ISA description database: ~555 SASS instruction format descriptor classes (one per opcode variant), ~316 bitfield layout initializers, ~239 opcode handler vtable entries. Also contains instruction sequence generators (multi-instruction expansions for complex PTX operations), register allocation helpers, and Newton-Raphson approximation templates. 91.8% of functions have zero static callers (vtable-dispatched). | ~555 format descriptor classes, ~316 bitfield initializers, ~239 vtable entries |
| `0x17F8000`--`0x18CD000` | 852 KB (native) | 1,460 | SASS instruction printer + peephole: Subsystem A (`0x17F8`--`0x181F`) implements SASS disassembly rendering via virtual method overrides on a builder/visitor with a 4,080+ byte vtable. Subsystem B (`0x1820`--`0x18CC`) is a 225 KB native peephole dispatch function (`sub_18A2CA0`, 231,217 B native, 54K instructions, 1,330 unique callees). | `sub_18189C0` (SASS printer, 8,804 B native), `sub_181B370` (SASS printer, 3,808 B native), `sub_18A2CA0` (231,217 B native peephole dispatch) |
| `0x18CD000`--`0x19A2000` | 877 KB (native) | 1,598 | Scheduling + peephole dispatchers: Zone A (275 KB native) is the instruction scheduling core (list scheduler, dependency graph, ready queue, register pressure tracking). Zone B (130 KB native) contains 318 opcode property/classification tables. Zones C+D (460 KB native) contain 888 peephole pattern matchers called from `sub_198BCD0` (238,930 B native, 1,336 unique callees). | `sub_198BCD0` (238,930 B native peephole dispatch), 392 scheduling functions, 318 opcode property tables, 888 pattern matchers |
| `0x19A2000`--`0x1A77000` | 880 KB (native) | 1,393 | GPU ABI/calling convention + SM89/90 encoders: Zone A (250 KB native, 276 functions) implements the NVIDIA GPU calling convention — parameter register allocation, return address placement, scratch/preserved classification, convergent boundary enforcement, coroutine SUSPEND semantics, uniform register support, per-SM ABI lowering (sm\_35 through sm\_100+). Zone B (480 KB native) has ~1,117 supplementary SASS encoding vtable handlers. | `sub_19D1AF0` (master ABI setup, 5,608 B native), 276 ABI functions, ~1,117 encoding handlers |
| `0x1A77000`--`0x1B4C000` | 829 KB | 1,518 | SASS emission backend (4 SM families): Zone A has 1,083 bit-field packing encoders spanning sm\_50 through sm\_100+. Zone B has 339 instruction lowering/expansion functions (two SM families: sm\_8x and sm\_9x/10x). Zone C has 84 Ampere/Ada/Hopper-era encoders. Zone D has 92 Blackwell-era encoders. | `sub_1B6B250` (register-class-to-HW mapping, 254 callers), 1,083 emitters, 339 lowering functions |
| `0x1B4C000`--`0x1C21000` | 876 KB | 1,974 | SASS emission + format descriptors: register-class encoding tables (Zone A), per-SM instruction bit-field encoders (Zone B), instruction emission orchestrators (Zone C), multi-operand dispatch emitters (Zone D), mirrored SM-variant emitters (Zone E), instruction format descriptors (Zone F, `0x1C05`--`0x1C21`). | 487 functions exceed 2 KB decompiled |
| `0x1C21000`--`0x1CE2DE2` | 776 KB | 1,628 | Library layer: custom ELF emitter (CUBIN output), capsule Mercury ELF (`.nv.capmerc` debug metadata), section layout and memory allocation (shared/constant/local/global), relocation resolution (branch targets, UFT/UDT, YIELD-to-NOP), call graph analysis (recursion detection, dead function elimination), DWARF debug generation (`.debug_info`/`.debug_line`/`.debug_frame`), option parsing library, thread pool (pthread-based), JSON builder, GNU Make jobserver client, C++ name demangler (Itanium ABI), ELF file writer | `sub_1C9F280` (ELF emitter, 97 KB decomp), `sub_1CABD60` (section allocator, 11,856 B native / 66 KB decomp), `sub_1CC9800` (EIATTR builder, 14,764 B native / 86 KB decomp), `sub_1CDC780` (demangler, 15,988 B native / 90 KB decomp), `sub_1CB53A0` (ELF world init), `sub_1CD48C0` (relocation resolver, 4,184 B native / 20 KB decomp), `sub_1CBB920` (recursion detector), `sub_1CB18B0` (thread pool), `sub_1CD13A0` (file writer, 2,541 B native / 11 KB decomp) |

## .rodata Contents (7.5 MB)

The `.rodata` section at `0x1CE2E00`--`0x240BF8F` is 29% of the binary by size. Its dominant consumers:

| Content | Estimated Size | Notes |
|---|---|---|
| SASS encoding format descriptors | ~3.5 MB | 128-bit `xmmword` constants loaded via SSE by ~4,000 encoding handlers |
| Flex DFA transition tables | ~600 KB | `off_203C020`, the 552-rule PTX scanner's state machine |
| Bison parser tables | ~400 KB | LALR(1) action/goto tables for the PTX grammar |
| Error/diagnostic format strings | ~300 KB | 30,632 strings extracted from the binary |
| Phase ordering + vtable tables | ~100 KB | Default 159-entry phase table at `0x22BEEA0`, vtable table at `off_22BD5C8` |
| ROT13-encoded string tables | ~200 KB | PTX opcode names (~900 entries), knob names (~2,000 entries) |
| Architecture capability tables | ~150 KB | Per-SM feature maps (sm\_75 through sm\_121), HW latency profiles |
| DWARF name tables | ~50 KB | `DW_FORM_*`, `DW_AT_*`, `DW_OP_*` string tables |
| Hash constants + misc | ~2.2 MB | MurmurHash3 mixing constants, lookup tables, padding |

## .bss Contents (84 KB)

| Content | Notes |
|---|---|
| ROT13 PTX opcode name table | Populated by `ctor_003` (`0x4095D0`, 17 KB) at startup |
| General OCG knob table | Populated by `ctor_005` (`0x40D860`, 80 KB) — ~2,000 entries |
| Mercury scheduler knob table | Populated by `ctor_007` (`0x421290`, 8 KB) — 98 entries |
| Thread-local storage keys | `pthread_key_t` for per-thread context (280-byte struct) |
| Global pool allocator mutex | `pthread_mutex_t` at pool struct offset 7128 |
| Diagnostic suppression bitmaps | Per-warning-ID suppression flags |
| SM architecture profile objects | Constructed on demand per `sub_6765E0` |
| Global error/warning counters | Incremented by `sub_42FBA0` |
| Make jobserver state | Atomic state machine (0=init, 5=no MAKEFLAGS, 6=no auth, 7=failed) |

## .data Contents (14 KB)

| Content | Notes |
|---|---|
| Function pointer tables | Exit wrapper (`off_29FA4B0`), error handler dispatch |
| Default option values | Populated by `sub_432A00` (option registration) |
| Static string table pointers | Version strings, format strings |
| Diagnostic output tables | Severity prefix strings: `"error   "`, `"warning "`, `"info    "`, `"fatal   "` |

## Static Constructors

The `.ctors` section holds 12 entries executed before `main`. The four largest are:

| Constructor | Address | Binary Size | Purpose |
|---|---|---|---|
| `ctor_001` | `0x4094C0` | 204 B | Thread infrastructure: `pthread_key_create`, mutex init, thread priority range |
| `ctor_003` | `0x4095D0` | 17,007 B | PTX opcode name table: ~900 ROT13-encoded opcode mnemonics |
| `ctor_005` | `0x40D860` | 80,397 B | General OCG knob table: ~2,000 ROT13-encoded knob names + hex defaults |
| `ctor_007` | `0x421290` | 7,921 B | Mercury scheduler knob table: 98 ROT13-encoded scheduler knobs |

The remaining 8 constructors handle memory allocator pool initialization, hash map infrastructure setup, diagnostic system initialization, and architecture vtable factory registration (`sub_1CCD900`).

## Mega-Functions (>50 KB binary)

| Address | Binary Size | Decompiled | Function | Callees |
|---|---|---|---|---|
| `sub_169B190` | 280 KB | N/A | Master ISel pattern dispatch (66K instructions) | 15,870 |
| `sub_198BCD0` | 239 KB | N/A | Peephole dispatch, SM variant 2 | 1,336 |
| `sub_143C440` | 233 KB | N/A | SM120 peephole dispatch (373-case switch) | ~1,100 |
| `sub_18A2CA0` | 231 KB | N/A | Peephole dispatch, SM variant 1 | 1,330 |
| `sub_6D9690` | 94 KB | N/A | Instruction encoding switch | ~500 |
| `sub_46E000` | 93 KB | N/A | PTX opcode-to-handler table builder | 1,168 |
| `sub_40D860` | 80 KB | N/A | `ctor_005`: general knob registration | ~2,000 |
| `sub_720F00` | 64 KB | N/A | Flex DFA scanner (552 rules) | ~50 |

These eight functions account for 1.2 MB of code (4.8% of `.text`) but only 0.02% of the function count.

## Most-Called Functions

| Address | Callers | Identity |
|---|---|---|
| `sub_4280C0` | 3,928 | Thread-local context accessor (`pthread_getspecific`) |
| `sub_42BDB0` | 3,825 | Fatal OOM handler (called from every allocation site) |
| `sub_424070` | 3,809 | Pool memory allocator (alloc) |
| `sub_426150` | 2,800 | Hash map insert/update |
| `sub_42FBA0` | 2,350 | Central diagnostic message emitter |
| `sub_4248B0` | 1,215 | Pool memory deallocator (free) |
| `sub_42CA60` | 298 | Linked list prepend |
| `sub_42D850` | 282 | Hash set insert |
| `sub_1B6B250` | 254 | Register-class-to-hardware-number lookup (SASS emission) |
| `sub_4279D0` | 185 | String prefix match (`starts_with`) |

The top five functions are all in the runtime infrastructure region (`0x403520`--`0x42F000`). Together they represent the core allocation, error handling, and data structure layer that the rest of the binary depends on.

## Binary Composition by Purpose

Estimated from function classification across 30 sweep reports (p1.01–p1.30). Each function was assigned to a single purpose category based on its dominant behavior; functions straddling categories (e.g., a scheduling pass that also emits SASS) are attributed to the category consuming the larger share of their code.

| Purpose | Estimated Size | Share of .text |
|---|---|---|
| SASS instruction encoding/decoding | ~12 MB | 46% |
| Optimization passes + scheduling | ~5 MB | 19% |
| Peephole pattern matching + dispatch | ~3 MB | 12% |
| Frontend: parsing + validation | ~2 MB | 8% |
| ISel pattern matching + templates | ~1.5 MB | 6% |
| Infrastructure: allocator, hash, ELF, debug | ~1.5 MB | 6% |
| GPU ABI + calling convention | ~0.7 MB | 3% |

The single largest consumer of code space is SASS instruction encoding. Each SM architecture generation requires its own set of per-opcode encoding/decoding handler functions. With support for SM75 through SM121 (six major generations), this yields approximately 4,000 encoding handlers, each a standalone function averaging 1,400 bytes.
