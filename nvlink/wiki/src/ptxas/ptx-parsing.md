# PTX Parsing

> **Note**: This page documents the PTX frontend embedded inside nvlink v13.0.88's ptxas copy -- a flex lexer + bison parser + 588-entry instruction-handler registry + 608-entry CUDA builtin database. The standalone ptxas binary has its own flex/bison frontend with a different LALR grammar (513 productions, 1,099 states) and ~552 lexer rules at different addresses; for that frontend see [ptxas PTX Parser](../../ptxas/pipeline/ptx-parser.html) and [PTX-to-Ori](../../ptxas/pipeline/ptx-to-ori.html).

The embedded ptxas compiler in nvlink v13.0.88 contains a complete PTX assembler frontend: a flex-generated lexer, a bison/yacc-generated parser, a module initialization system that defines every PTX special register and builtin type, an instruction handler registry covering the full PTX ISA 9.0, a CUDA builtin function database of 608 entries, and a recursive expression printer. This page documents each subsystem at the function level, reconstructed from decompiled binary analysis across the `0x12AF000`--`0x12C0000`, `0x1430000`--`0x15C0000`, and `0x16E0000`--`0x16F6000` address ranges.

## Architecture Overview

PTX parsing is organized into seven subsystems that form a pipeline from raw text to internal IR:

```text
PTX source text
    |
    v
[1] Flex Lexer (sub_16EAF60, 65KB)
    |  DFA state table at off_22788C0
    |  550+ token actions
    |  Macro preprocessor (sub_16E8310)
    v
[2] Bison/Yacc Parser (sub_16F0E60, 31KB)
    |  1172 lines of parser actions
    |  Operand list builders (sub_16E4D60..sub_16E7D00)
    v
[3] Module Initializer (sub_12AF950, 60KB)
    |  Special registers, builtin types, hash tables
    |  PTX version 9.0, ~20 symbol caches
    v
[4] Instruction Handler Registry (sub_158D130, 41KB)
    |  115 named mnemonic handlers + 473 hash-encoded handlers (588 total)
    |  Two-level dispatch: name table *(ctx+808), hash table *(ctx+816)
    v
[5] Semantic Validators (sub_147EF50, 288KB + sub_146BEC0, 206KB + ...)
    |  SM version gates, PTX version gates, operand type checks
    |  Relaxed mode bypass (sub_12B3090)
    v
[6] Code Template Generators (~250 functions, 3--50KB each)
    |  50KB temp buffer, sprintf-based PTX emission
    v
[7] Expression Printer (sub_12B33A0, 9KB)
       17 expression kinds, recursive descent
```

## Confidence Tags for Core Claims

| Claim | Confidence | Evidence |
|---|---|---|
| `sub_16EAF60` is flex-generated PTX lexer (64,758 bytes) | HIGH | DFA state table at `off_22788C0`; 550+ token actions; error strings `"fatal error - scanner input buffer overflow"`, `"out of dynamic memory in ptx_create_buffer()"` |
| `sub_16F0E60` is bison/yacc parser (31 KB, 1,172 lines) | HIGH | Companion to flex lexer; standard yacc parser table dispatch pattern in decompiled output |
| `sub_12AF950` is `ptx_init_compilation_state` (59,874 bytes) | HIGH | Calls `sub_448E70` (hash insert) for each of 85 PTX special-register names listed in declaration order; sets PTX_MAJOR_VERSION=9, PTX_MINOR_VERSION=0 |
| Builtin type fields for `.texref`/`.samplerref`/`.surfref` | HIGH | Field names emitted as string literals; created via `sub_12AD9E0` |
| 588 instruction handlers (115 named + 473 hashed) | MEDIUM | Counted from registry build; two-level dispatch at `ctx+808` / `ctx+816` visible in handler-install loop |
| Expression printer kinds = 17 | MEDIUM | Switch-case count in `sub_12B33A0` recursive printer |
| Macro nesting limit message `"macro nesting too deep!"` in `sub_16E8310` | HIGH | Direct string xref to the function |

## Module Initialization

### sub_12AF950 -- ptx_init_compilation_state (59,874 bytes, 1,440 lines)

The master initialization function. Creates the entire PTX compilation state in a single call, including the symbol table infrastructure, all PTX special registers, all builtin aggregate types, and the version/target configuration. Takes 11 parameters including a function pointer callback.

**Allocation.** Creates two major structures: a 1,128-byte compilation context and a 2,528-byte instruction state block. Both are arena-allocated via `sub_4307C0`.

**PTX version.** Sets `PTX_MAJOR_VERSION = 9`, `PTX_MINOR_VERSION = 0` via `sub_448E70` hash table inserts. This corresponds to CUDA Toolkit 13.0 / PTX ISA 9.0.

**Special register initialization.** Registers every PTX special register in declaration order, using `sub_448E70` to insert each name into the symbol table:

| Register Group | Names | Count |
|---|---|---|
| Thread/block IDs | `%tid`, `%ntid`, `%laneid`, `%warpid`, `%nwarpid` | 5 |
| SM identity | `%smid`, `%nsmid` | 2 |
| CTA/grid IDs | `%ctaid`, `%nctaid`, `%gridid` | 3 |
| Clocks | `%clock`, `%clock_hi`, `%clock64` | 3 |
| Performance counters | `%pm0`--`%pm7` (loop 0..7) | 8 |
| 64-bit perf counters | `%pm0_64`--`%pm7_64` (loop 0..7) | 8 |
| Lane masks | `%lanemask_eq`, `%lanemask_le`, `%lanemask_lt`, `%lanemask_ge`, `%lanemask_gt` | 5 |
| Environment | `%envreg0`--`%envreg31` (loop 0..31) | 32 |
| Timers | `%globaltimer_lo`, `%globaltimer_hi`, `%globaltimer` | 3 |
| Shared memory | `%total_smem_size`, `%dynamic_smem_size`, `%aggr_smem_size` | 3 |
| Reserved SMEM | `%reserved_smem_offset_begin`, `%reserved_smem_offset_end`, `%reserved_smem_offset_cap`, `%reserved_smem_offset_0`, `%reserved_smem_offset_1` | 5 |
| Cluster | `%is_explicit_cluster`, `%clusterid`, `%nclusterid`, `%cluster_ctaid`, `%cluster_ctarank`, `%cluster_nctaid`, `%cluster_nctarank` | 7 |
| CUDA graphs | `%current_graph_exec` | 1 |
| **Total** | — | **85** |

**Builtin type initialization.** Creates three aggregate type descriptors via `sub_12AD9E0`:

`.texref` fields: `width`, `height`, `depth`, `channel_data_type`, `channel_order`, `normalized_coords`, `filter_mode`, `addr_mode_0`, `addr_mode_1`, `addr_mode_2`, `array_size`, `num_mipmap_levels`, `num_samples` (13 fields).

`.samplerref` fields: `force_unnormalized_coords`, `filter_mode`, `addr_mode_0`, `addr_mode_1`, `addr_mode_2` (5 fields).

`.surfref` fields: `width`, `height`, `depth`, `channel_data_type`, `channel_order`, `array_size`, `memory_layout` (7 fields).

**Hash table allocation.** Initializes approximately 20 hash tables (via `sub_4489C0`) for symbol caches, type caches, instruction lookup, and builtin registration. Each table uses the standard pattern: `sub_4489C0(hash_fn, eq_fn, bucket_count)`.

**Target configuration.** Calls `sub_1426EE0` to initialize target-specific configuration and `sub_16E3AA0` to set SM version info (field 30 = SM major, field 31 = SM minor).

### sub_12AF200 -- ptx_init_builtin_macros (4,556 bytes, 144 lines)

Initializes Fermi-era backward-compatibility macro definitions. Referenced as `"<fermi macros>"` in calling context.

### sub_12AF550 -- ptx_init_target_features (4,625 bytes, 144 lines)

Initializes the target feature set based on the SM version. Companion to the main initializer.

## Lexer (Flex-Generated)

### sub_16EAF60 -- ptx_flex_scanner_main_loop (64,758 bytes, 2,530 lines)

The main scanner loop, generated by flex. This is the lexical analysis engine that tokenizes PTX source text.

**DFA structure.** The scanner uses a DFA state table at `off_22788C0`, indexed by `(current_state, input_character)`. The action dispatch table at `dword_2278020` maps accepted token types to action categories. The action switch has 550+ cases covering the entire PTX token vocabulary: keywords, directives, register names, literals, operators, and punctuation.

**EOF handling.** Case 550 handles end-of-file. The scanner tracks line numbers by counting newline characters (ASCII 10).

**Buffer management.** Three helper functions manage scanner input:

| Function | Size | Description |
|---|---|---|
| `sub_16EA470` (ptx_create_buffer) | 3,359 B | Allocates new scan buffer. Error: `"out of dynamic memory in ptx_create_buffer()"` |
| `sub_16EA690` (ptx_scan_buffer_handler) | 9,723 B | Manages buffer switching/refilling. Error: `"fatal error - scanner input buffer overflow"` |
| `sub_16EED20` (ptx_scan_bytes) | 5,001 B | Creates scan buffer from byte array. Errors: `"out of dynamic memory in ptx_scan_bytes()"`, `"bad buffer in ptx_scan_bytes()"` |
| `sub_16EF8E0` (ptx_scan_buffer_create) | 3,161 B | Companion buffer creator. Error: `"out of dynamic memory in ptx_scan_buffer()"` |
| `sub_16EAC00` (ptx_string_concat) | 4,329 B | String concatenation for token accumulation, called with `"\n"` separator |

### sub_16E8310 -- ptx_macro_preprocessor (33,290 bytes, 1,068 lines)

The PTX macro preprocessor. Runs before lexical analysis to expand `.MACRO`, `.ELSE`, `.ELIF` directives.

**Input handling.** Processes input character by character, checking for whitespace (ASCII 9 = tab, 32 = space). Manages macro nesting with a depth limit -- exceeding it triggers `"macro nesting too deep!"`. Uses `sub_44FB20` for character classification.

**State tracking.** Key offsets in the preprocessor context:
- `+48`: current line number
- `+52`: current column number
- `+2144`: input buffer pointer
- `+2441`: lookahead character
- `+8`: stdin fallback (when no input buffer is active)

### sub_16EF680 -- ptx_macro_expansion (4,010 bytes, 142 lines)

Handles individual macro expansion with nesting depth checking. Emits `"macro nesting too deep!"` on overflow.

## Parser (Bison/Yacc-Generated)

### sub_16F0E60 -- ptx_parser_yacc_main (30,684 bytes, 1,172 lines)

The main parser driver, generated by bison/yacc. Works in conjunction with the flex scanner (`sub_16EAF60`). At 1,172 lines, this encodes the full PTX grammar reduction rules and semantic actions.

### sub_16E9690 -- ptx_directive_parser (14,777 bytes, 525 lines)

Parses PTX directives including `.surfref`, `.samplerref`, `.texref`, and section directives. The 525-line function implements a complex state machine that handles multiple directive types and their parameter lists.

### sub_1442040 -- section_directive_validator (3,300 bytes, 82 lines)

Validates `.section` and `@@DWARF` directives. The `@@DWARF` handler processes inline DWARF debug data embedded in PTX source, which is the mechanism by which `nvcc` passes debug information through PTX to the assembler.

## Instruction Construction

The parser builds instruction IR nodes through a family of builder functions in the `0x16E4D60`--`0x16E7D00` range. All work with dynamic arrays of 48-byte records (one per operand), using a growth factor of 1.5x with a minimum of 16 elements. The block allocation unit is 768 bytes (16 elements times 48 bytes). Memory allocation uses vtable callbacks at `**obj+24`.

| Function | Size | Description |
|---|---|---|
| `sub_16E4D60` (ptx_instruction_builder) | 17,306 B | Main instruction node constructor. Manages operand count fields at offsets `+144`, `+148`, `+152`, `+156`, `+176`, `+184`, `+188` |
| `sub_16E5A60` (ptx_operand_insert_helper) | 7,165 B | Operand insertion into existing instruction |
| `sub_16E6370` (ptx_operand_setter) | 8,516 B | Sets operand properties within instruction node |
| `sub_16E6970` (ptx_complex_builder) | 13,307 B | Multi-operand instruction construction (505 lines) |
| `sub_16E7210` (ptx_operand_append) | 6,875 B | Appends operand to instruction's operand array |
| `sub_16E7770` (ptx_operand_modifier_handler) | 7,563 B | Applies modifiers during instruction construction |
| `sub_16E7D00` (ptx_instruction_fixup) | 8,513 B | Post-processing: adjusts operand counts and internal state |

## Instruction Handler Registration

### sub_158D130 -- ptx_instruction_handler_table_init (40,900 bytes, 595 lines)

The master instruction handler registration function. First calls `sub_158A600` to initialize the CUDA builtin name table, then populates two dispatch hash tables with a combined 588 unique handler functions spanning 988.7 KB of code.

### Named Mnemonic Table (115 entries)

Stored at `*(context+808)`. Maps PTX instruction mnemonic strings to handler function pointers via `sub_448E70`. Every entry below is extracted verbatim from the decompiled registration sequence -- no entries are omitted.

#### Arithmetic and Math (17 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `div` | `sub_1570670` | 17,760 | Integer and FP division, largest arithmetic handler |
| `div.full` | `sub_152C800` | 2,032 | Full-range division (no fast approximation) |
| `rem` | `sub_15427B0` | 3,872 | Integer remainder |
| `rcp` | `sub_1569C70` | 13,168 | Reciprocal approximation |
| `rsqrt` | `sub_1534F60` | 2,704 | Reciprocal square root |
| `sqrt` | `sub_156CFE0` | 13,968 | Square root |
| `ex2` | `sub_153C130` | 3,520 | Base-2 exponential |
| `lg2` | `sub_14E3560` | 1,360 | Base-2 logarithm |
| `tanh` | `sub_14BEAA0` | 1,072 | Hyperbolic tangent (sm_75+) |
| `cvt` | `sub_15585D0` | 6,256 | Type conversion |
| `testp` | `sub_153A9B0` | 2,960 | Floating-point property test (NaN, Inf, etc.) |
| `copysign` | `sub_14C4120` | 1,056 | Copy sign bit between FP values |
| `dp2a.lo` | `sub_1524A00` | 1,840 | 2-element dot product accumulate (low half) |
| `dp2a.hi` | `sub_1525870` | 1,840 | 2-element dot product accumulate (high half) |
| `dp4a` | `sub_1530B40` | 2,256 | 4-element dot product accumulate |
| `membar` | `sub_14943B0` | 496 | Memory barrier |
| `prefetch` | `sub_14C0F50` | 1,024 | Cache prefetch |

#### Bit Manipulation (6 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `bfind` | `sub_1549BC0` | 4,832 | Find most significant bit |
| `brev` | `sub_14C4540` | 1,136 | Bit reversal |
| `bfe` | `sub_1531410` | 2,368 | Bit field extract |
| `bfi` | `sub_14E70A0` | 1,504 | Bit field insert |
| `clz` | `sub_1494C60` | 608 | Count leading zeros |
| `popc` | `sub_14941B0` | 512 | Population count |

#### Warp-Level Operations (5 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `vote` | `sub_1539DF0` | 3,008 | Warp vote (all/any/uni/ballot) |
| `shfl` | `sub_1539170` | 3,200 | Warp shuffle (up/down/bfly/idx) |
| `match` | `sub_15436D0` | 3,952 | Warp match (sm_70+) |
| `redux` | `sub_1520620` | 1,792 | Warp-level reduction (sm_80+) |
| `bar.warp` | `sub_1523540` | 1,760 | Intra-warp barrier |

#### Barrier and Synchronization (12 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `barrier` | `sub_1529230` | 1,712 | Named barrier sync |
| `barrier.arrive` | `sub_15298E0` | 1,808 | Named barrier arrive (non-blocking) |
| `barrier.red` | `sub_1541950` | 3,680 | Named barrier with reduction |
| `barrier.cta` | `sub_1493B00` | 416 | CTA-scope barrier |
| `barrier.cta.arrive` | `sub_1493CA0` | 416 | CTA-scope barrier arrive |
| `barrier.cta.red` | `sub_1494A00` | 608 | CTA-scope barrier with reduction |
| `bar` | `sub_14DDF50` | 1,216 | Legacy barrier (PTX < 7.0 form) |
| `bar.arrive` | `sub_14B9B90` | 944 | Legacy barrier arrive |
| `bar.red` | `sub_14E6530` | 1,424 | Legacy barrier with reduction |
| `bar.cta` | `sub_1493820` | 368 | Legacy CTA-scope barrier |
| `bar.cta.arrive` | `sub_1493990` | 368 | Legacy CTA-scope barrier arrive |
| `bar.cta.red` | `sub_14945A0` | 576 | Legacy CTA-scope barrier with reduction |

#### Texture and Surface (6 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `tex` | `sub_153DCB0` | 3,984 | Texture fetch |
| `tex.base` | `sub_1540950` | 4,096 | Texture fetch (base LOD) |
| `tex.level` | `sub_1544640` | 4,432 | Texture fetch (explicit LOD) |
| `tex.grad` | `sub_1566D60` | 12,048 | Texture fetch (explicit gradients) |
| `tld4` | `sub_15266A0` | 2,000 | Texture load 4 (gather) |
| `sured.b` | `sub_14E5A60` | 1,408 | Surface reduction (byte) |

#### Matrix (Tensor Core) Operations (16 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `mma` | `sub_157A040` | 27,056 | Generic MMA (sm_80+) |
| `wmma.mma` | `sub_15809F0` | 39,952 | WMMA matrix multiply-accumulate (sm_70+) |
| `wmma.load.a` | `sub_155BCB0` | 7,984 | WMMA load matrix A fragment |
| `wmma.load.b` | `sub_1559E40` | 7,792 | WMMA load matrix B fragment |
| `wmma.load.c` | `sub_1561DE0` | 9,760 | WMMA load matrix C/accumulator fragment |
| `wmma.store.d` | `sub_155FB70` | 8,816 | WMMA store matrix D result fragment |
| `wgmma.mma_async` | `sub_14C3C10` | 1,296 | Warpgroup MMA async (sm_90+) |
| `wgmma.fence` | `sub_1493320` | 304 | Warpgroup MMA fence |
| `wgmma.commit_group` | `sub_1493450` | 304 | Warpgroup MMA commit group |
| `wgmma.wait_group` | `sub_1493580` | 320 | Warpgroup MMA wait group |
| `ldmatrix` | `sub_14C6450` | 1,200 | Load matrix from shared memory |
| `stmatrix` | `sub_14A9570` | 912 | Store matrix to shared memory |
| `movmatrix` | `sub_1493E40` | 432 | Move matrix between registers |
| `tcgen05.mma` | `sub_1574BD0` | 21,616 | 5th-gen tensor core MMA (sm_100+) |
| `tcgen05.mma.ws` | `sub_15489C0` | 4,608 | 5th-gen tensor core MMA with warpgroup specialization |
| `tensormap.replace` | `sub_1538680` | 2,800 | Replace fields in tensor map descriptor |

#### tcgen05 (5th-Gen Tensor Core) Management (9 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `tcgen05.alloc` | `sub_1522120` | 1,712 | Allocate tensor core accumulator |
| `tcgen05.dealloc` | `sub_1545790` | 4,288 | Deallocate tensor core accumulator |
| `tcgen05.relinquish_alloc_permit` | `sub_14DF310` | 1,296 | Relinquish allocation permit |
| `tcgen05.ld` | `sub_152CFF0` | 2,144 | Load into tensor core accumulator |
| `tcgen05.ld.red` | `sub_1531D50` | 2,480 | Load-reduce into tensor core accumulator |
| `tcgen05.st` | `sub_152AF80` | 2,080 | Store from tensor core accumulator |
| `tcgen05.commit` | `sub_1525130` | 1,856 | Commit tensor core operations |
| `tcgen05.cp` | `sub_14FB790` | 1,584 | Copy tensor core data |
| `tcgen05.shift` | `sub_14AAA30` | 912 | Shift tensor core accumulator rows |

#### tcgen05 Guardrail Intrinsics (8 entries)

These are internal pseudo-instructions (prefixed with `_`) used for runtime validation of tensor core operations. They emit guard traps expanded by `sub_15B86A0`.

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `_tcgen05.guardrails.is_phase_valid` | `sub_14936C0` | 352 | Validate mbarrier phase |
| `_tcgen05.guardrails.are_columns_allocated` | `sub_1496E10` | 784 | Validate column allocation |
| `_tcgen05.guardrails.is_current_warp_valid_owner` | `sub_1494EC0` | 608 | Validate warp ownership |
| `_tcgen05.guardrails.in_physical_bounds` | `sub_1497120` | 800 | Validate physical address bounds |
| `_tcgen05.guardrails.allocation_granularity` | `sub_1493FF0` | 448 | Validate allocation granularity |
| `_tcgen05.guardrails.datapath_alignment` | `sub_14A9900` | 848 | Validate datapath alignment |
| `_tcgen05.guardrails.sp_consistency_across_idesc_mod` | `sub_1496520` | 736 | Validate stack pointer consistency across idesc modifiers |
| `_tcgen05.guardrails.check_sparse_usage` | `sub_14B9F40` | 976 | Validate sparse matrix usage |

#### Async Copy and Bulk Operations (6 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `cp.async.bulk` | `sub_154C1B0` | 5,072 | Asynchronous bulk copy |
| `cp.async.bulk.tensor` | `sub_1564400` | 10,592 | Asynchronous bulk tensor copy (sm_90+) |
| `cp.async.mbarrier.arrive` | `sub_1495120` | 704 | Async copy mbarrier arrive notification |
| `st.async` | `sub_1547950` | 4,208 | Asynchronous store |
| `red.async` | `sub_153B540` | 3,056 | Asynchronous reduction |
| `st.bulk` | `sub_15023D0` | 1,552 | Bulk store |

#### Multi-Memory (CTA-level Multicast) (3 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `multimem.ld_reduce` | `sub_1546850` | 4,352 | Multicast memory load-reduce (sm_90+) |
| `multimem.st` | `sub_1534460` | 2,816 | Multicast memory store |
| `multimem.red` | `sub_14C37F0` | 1,056 | Multicast memory reduction |

#### Cache Policy (3 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `createpolicy.range` | `sub_15364D0` | 2,816 | Create L2 cache policy by address range |
| `createpolicy.fractional` | `sub_15302A0` | 2,208 | Create L2 cache policy by fractional hit rate |
| `createpolicy.cvt` | `sub_14947E0` | 544 | Convert between cache policy representations |

#### SIMD Video Instructions (23 entries)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `vadd` | `sub_1555780` | 5,648 | Scalar video add |
| `vsub` | `sub_1554170` | 5,648 | Scalar video subtract |
| `vmin` | `sub_15515D0` | 5,568 | Scalar video minimum |
| `vmax` | `sub_1552B90` | 5,600 | Scalar video maximum |
| `vabsdiff` | `sub_1556D90` | 6,208 | Scalar video absolute difference |
| `vshl` | `sub_154D580` | 5,280 | Scalar video shift left |
| `vshr` | `sub_154EA20` | 5,296 | Scalar video shift right |
| `vset` | `sub_154AEA0` | 4,880 | Scalar video compare and set |
| `vmad` | `sub_154FED0` | 5,888 | Scalar video multiply-add |
| `vadd2` | `sub_152E8F0` | 2,208 | SIMD-2 video add |
| `vsub2` | `sub_152F190` | 2,192 | SIMD-2 video subtract |
| `vmin2` | `sub_1536FD0` | 2,880 | SIMD-2 video minimum |
| `vmax2` | `sub_1537B10` | 2,928 | SIMD-2 video maximum |
| `vabsdiff2` | `sub_1509200` | 1,632 | SIMD-2 video absolute difference |
| `vset2` | `sub_14E6AC0` | 1,504 | SIMD-2 video compare and set |
| `vavrg2` | `sub_1532700` | 2,512 | SIMD-2 video average |
| `vadd4` | `sub_153FAB0` | 3,744 | SIMD-4 video add |
| `vsub4` | `sub_153EC40` | 3,696 | SIMD-4 video subtract |
| `vmin4` | `sub_152BFE0` | 2,080 | SIMD-4 video minimum |
| `vmax4` | `sub_152A780` | 2,048 | SIMD-4 video maximum |
| `vabsdiff4` | `sub_152B7A0` | 2,112 | SIMD-4 video absolute difference |
| `vset4` | `sub_1526E70` | 1,824 | SIMD-4 video compare and set |
| `vavrg4` | `sub_1533A80` | 2,528 | SIMD-4 video average |

#### Internal / Special (1 entry)

| Mnemonic | Handler | Size (bytes) | Notes |
|---|---|---|---|
| `_ldldu` | `sub_1496800` | 800 | Internal: unaligned load (emitted by lowering) |

**Named handler statistics.** 115 total entries, 115 unique function addresses. Total code size: 426,096 bytes (416.1 KB). Handler sizes range from 304 bytes (`wgmma.fence`) to 39,952 bytes (`wmma.mma`). The five largest handlers account for 99,632 bytes (23.4% of all named handler code): `wmma.mma` (39.9 KB), `mma` (27.1 KB), `tcgen05.mma` (21.6 KB), `div` (17.8 KB), `sqrt` (14.0 KB).

### Hash-Encoded Opcode Table (473 entries)

Stored at `*(context+816)`. Maps 32-bit hash keys (stored as decimal string keys like `"2644314910"`) to handler function pointers. These 473 entries cover instruction variants that are dispatched by opcode encoding hash rather than mnemonic string. Each handler follows the same code template pattern as the named handlers: allocate 50 KB temp buffer, build PTX text via `sprintf` from string table offsets, shrink-allocate, return.

**Structure.** The hash keys are 32-bit unsigned integers representing pre-computed hashes of instruction encoding descriptors. The hash function is not applied at lookup time to a mnemonic string; instead, the parser pre-computes the hash from the instruction's opcode + type + modifier combination and looks it up directly. This covers complex variant combinations (e.g., `add.s32`, `add.f32`, `mad.lo.s32`, `setp.eq.f64`) where encoding a separate named mnemonic for each variant would be impractical.

**Statistics.** 473 entries, 473 unique function addresses, zero overlap with the named mnemonic table. Total code size: 586,352 bytes (572.6 KB). Handler sizes range from 736 to 8,080 bytes, with median 1,136 bytes and mean 1,239 bytes. Size distribution:

| Size Range | Count | Pct |
|---|---|---|
| 500--999 bytes | 170 | 35.9% |
| 1,000--1,999 bytes | 296 | 62.6% |
| 2,000--4,999 bytes | 6 | 1.3% |
| 5,000--9,999 bytes | 1 | 0.2% |

**Address distribution.** Hash handlers cluster in the `0x1490000`--`0x1520000` range (92% of entries), while named handlers dominate `0x1520000`--`0x1580000`. This reflects a compilation unit boundary: the hash handlers are generated from table-driven templates, while the named handlers are handwritten for semantically complex instructions.

**Complete hash-encoded dispatch table.** All 473 entries in registration order:

| # | Hash Key | Handler | Size (B) | # | Hash Key | Handler | Size (B) |
|---|---|---|---|---|---|---|---|
| 1 | `2644314910` | `sub_1496B20` | 736 | 2 | `605425506` | `sub_1497440` | 800 |
| 3 | `359337725` | `sub_14D1F80` | 1,024 | 4 | `4134604268` | `sub_14AA6A0` | 912 |
| 5 | `3457617063` | `sub_14BBE60` | 1,024 | 6 | `3458731190` | `sub_14B84C0` | 1,024 |
| 7 | `3461614778` | `sub_14C1350` | 1,024 | 8 | `273026588` | `sub_14A9FC0` | 912 |
| 9 | `273550881` | `sub_14AA330` | 912 | 10 | `766250731` | `sub_14BC240` | 1,040 |
| 11 | `767430384` | `sub_14BC630` | 1,024 | 12 | `278072875` | `sub_1497770` | 832 |
| 13 | `278597168` | `sub_1497AB0` | 832 | 14 | `3013151698` | `sub_1527CA0` | 1,792 |
| 15 | `1034227483` | `sub_14CAB90` | 1,136 | 16 | `2998735345` | `sub_14CB000` | 1,136 |
| 17 | `2995851755` | `sub_14CB470` | 1,136 | 18 | `1030557441` | `sub_14C8C80` | 1,136 |
| 19 | `2983137751` | `sub_14C90F0` | 2,272 | 20 | `2980254161` | `sub_14C99D0` | 1,136 |
| 21 | `4210694260` | `sub_14987B0` | 832 | 22 | `4203485294` | `sub_1498AF0` | 832 |
| 23 | `1497109918` | `sub_1498E30` | 832 | 24 | `2394822284` | `sub_1499170` | 832 |
| 25 | `2387613318` | `sub_14994B0` | 832 | 26 | `4199093174` | `sub_14997F0` | 832 |
| 27 | `88478685` | `sub_1499B30` | 832 | 28 | `85595095` | `sub_1499E70` | 832 |
| 29 | `1848645077` | `sub_14B3EC0` | 912 | 30 | `2192249095` | `sub_149A1B0` | 832 |
| 31 | `3719369477` | `sub_14B3B20` | 928 | 32 | `2918388213` | `sub_149A4F0` | 832 |
| 33 | `2915504623` | `sub_149A830` | 832 | 34 | `224662509` | `sub_14B33E0` | 928 |
| 35 | `952045343` | `sub_149AB70` | 832 | 36 | `2319257885` | `sub_14B3780` | 928 |
| 37 | `1094325528` | `sub_14BBA70` | 1,024 | 38 | `3069252162` | `sub_149AEB0` | 832 |
| 39 | `1087116562` | `sub_149B1F0` | 832 | 40 | `2848461329` | `sub_149B530` | 832 |
| 41 | `843846881` | `sub_149B870` | 832 | 42 | `851055847` | `sub_14BB680` | 1,024 |
| 43 | `90377643` | `sub_14FB180` | 1,584 | 44 | `1335693225` | `sub_15138C0` | 1,648 |
| 45 | `1883181179` | `sub_14F13E0` | 1,520 | 46 | `3364426361` | `sub_150EB80` | 1,584 |
| 47 | `1886064769` | `sub_1520D20` | 1,792 | 48 | `1678118992` | `sub_1527590` | 1,808 |
| 49 | `1325928250` | `sub_14CB8E0` | 1,136 | 50 | `2738688312` | `sub_14D57E0` | 1,136 |
| 51 | `3355120138` | `sub_14CBD50` | 1,136 | 52 | `709825544` | `sub_14D4970` | 1,136 |
| 53 | `3359969808` | `sub_14D3FE0` | 1,136 | 54 | `1675235402` | `sub_14F2610` | 1,520 |
| 55 | `3140424264` | `sub_150CB50` | 1,584 | 56 | `4198894970` | `sub_14F2C20` | 1,520 |
| 57 | `1134169976` | `sub_1511890` | 1,584 | 58 | `1059984638` | `sub_14B8C50` | 944 |
| 59 | `1052775672` | `sub_149BBB0` | 832 | 60 | `3046838824` | `sub_149BEF0` | 832 |
| 61 | `816714957` | `sub_14B9020` | 976 | 62 | `2826047991` | `sub_149C230` | 832 |
| 63 | `809505991` | `sub_149C570` | 832 | 64 | `1870467175` | `sub_15283C0` | 1,856 |
| 65 | `86707601` | `sub_14E8260` | 1,552 | 66 | `1323503503` | `sub_150D830` | 1,648 |
| 67 | `1867583585` | `sub_14E7C50` | 1,552 | 68 | `3340309087` | `sub_150D1C0` | 1,648 |
| 69 | `1662521398` | `sub_1528B00` | 1,840 | 70 | `1313738528` | `sub_14CC1C0` | 1,136 |
| 71 | `2717978910` | `sub_14D9670` | 1,136 | 72 | `3331002864` | `sub_14CC630` | 1,136 |
| 73 | `677188590` | `sub_14D8CD0` | 1,136 | 74 | `3335852534` | `sub_14E3AB0` | 1,360 |
| 75 | `1659637808` | `sub_14FA560` | 1,584 | 76 | `3116306990` | `sub_15165D0` | 1,648 |
| 77 | `4195224928` | `sub_14FAB70` | 1,584 | 78 | `1121980254` | `sub_15172B0` | 1,648 |
| 79 | `2889945594` | `sub_14E1640` | 1,376 | 80 | `2867532256` | `sub_14E4A70` | 1,008 |
| 81 | `2673873353` | `sub_14E0C20` | 1,376 | 82 | `2651460015` | `sub_14E20A0` | 1,360 |
| 83 | `2678854089` | `sub_14DB370` | 1,136 | 84 | `2656440751` | `sub_14DA9B0` | 1,136 |
| 85 | `4003207167` | `sub_14BA6F0` | 1,024 | 86 | `1343296809` | `sub_149C8B0` | 832 |
| 87 | `3995998201` | `sub_149CBF0` | 832 | 88 | `3711244238` | `sub_14BAEB0` | 1,024 |
| 89 | `1073812728` | `sub_149DF70` | 832 | 90 | `3704035272` | `sub_149E2B0` | 832 |
| 91 | `3716224974` | `sub_14BAAD0` | 1,024 | 92 | `1078793464` | `sub_149E5F0` | 832 |
| 93 | `3709016008` | `sub_149E930` | 832 | 94 | `4259320680` | `sub_14BA310` | 1,024 |
| 95 | `2122780818` | `sub_14B1E20` | 960 | 96 | `3611562640` | `sub_14BE280` | 1,040 |
| 97 | `4256437090` | `sub_14B21C0` | 960 | 98 | `1687164256` | `sub_14BE690` | 1,024 |
| 99 | `4002681655` | `sub_14E3FF0` | 1,360 | 100 | `3999798065` | `sub_14C5700` | 1,136 |
| 101 | `1414468911` | `sub_14D6FF0` | 1,136 | 102 | `1888620641` | `sub_14C5290` | 1,136 |
| 103 | `3361346143` | `sub_14D7E60` | 1,136 | 104 | `4007662391` | `sub_14A9C50` | 912 |
| 105 | `1893601377` | `sub_149EC70` | 832 | 106 | `3366326879` | `sub_14B3040` | 928 |
| 107 | `4004778801` | `sub_149EFB0` | 832 | 108 | `1419449647` | `sub_14B1340` | 832 |
| 109 | `3968866277` | `sub_149F2F0` | 832 | 110 | `1320883471` | `sub_149F630` | 832 |
| 111 | `3961657311` | `sub_149F970` | 832 | 112 | `3676903348` | `sub_149FCB0` | 832 |
| 113 | `1051399390` | `sub_149FFF0` | 832 | 114 | `3669694382` | `sub_14A0330` | 832 |
| 115 | `3681884084` | `sub_14A0670` | 832 | 116 | `1056380126` | `sub_14A09B0` | 832 |
| 117 | `3674675118` | `sub_14A0CF0` | 832 | 118 | `4243723086` | `sub_14B0860` | 928 |
| 119 | `2119110776` | `sub_14B0C00` | 928 | 120 | `3599372918` | `sub_14BDE70` | 1,040 |
| 121 | `4240839496` | `sub_14B0FA0` | 928 | 122 | `1663046982` | `sub_14BEED0` | 1,024 |
| 123 | `3987084061` | `sub_14CEE20` | 1,136 | 124 | `3984200471` | `sub_14CCAA0` | 1,136 |
| 125 | `1390351637` | `sub_14D91A0` | 1,136 | 126 | `1884950599` | `sub_14CCF10` | 1,136 |
| 127 | `3349156421` | `sub_14D8800` | 1,232 | 128 | `3992064797` | `sub_149D8F0` | 832 |
| 129 | `1889931335` | `sub_149DC30` | 832 | 130 | `3354137157` | `sub_14AFD80` | 928 |
| 131 | `3989181207` | `sub_149D270` | 832 | 132 | `1395332373` | `sub_14B1A80` | 960 |
| 133 | `1537545665` | `sub_149D5B0` | 832 | 134 | `1515132327` | `sub_149CF30` | 832 |
| 135 | `453121116` | `sub_14953E0` | 736 | 136 | `437523522` | `sub_14956C0` | 736 |
| 137 | `456004706` | `sub_14959A0` | 736 | 138 | `440407112` | `sub_1495C80` | 736 |
| 139 | `2498761100` | `sub_1495F60` | 736 | 140 | `2495091058` | `sub_1496240` | 736 |
| 141 | `1034555164` | `sub_14C71E0` | 1,136 | 142 | `2999521778` | `sub_14C7650` | 1,136 |
| 143 | `2996638188` | `sub_14C7AC0` | 1,136 | 144 | `1030885122` | `sub_14C9E40` | 1,136 |
| 145 | `2983924184` | `sub_14CA2B0` | 1,136 | 146 | `2981040594` | `sub_14CA720` | 1,136 |
| 147 | `1095832857` | `sub_14B8880` | 944 | 148 | `1088623891` | `sub_1497DF0` | 832 |
| 149 | `3070300739` | `sub_1498130` | 832 | 150 | `852563176` | `sub_14B93F0` | 944 |
| 151 | `2849509906` | `sub_1498470` | 832 | 152 | `845354210` | `sub_14A1030` | 832 |
| 153 | `1886851202` | `sub_1523C20` | 1,792 | 154 | `90705324` | `sub_14F8710` | 1,520 |
| 155 | `1336348586` | `sub_1518600` | 1,648 | 156 | `1883967612` | `sub_14F8D20` | 1,520 |
| 157 | `3365540474` | `sub_150E510` | 1,648 | 158 | `3361083921` | `sub_14D3B20` | 1,184 |
| 159 | `1326583611` | `sub_14C6900` | 1,136 | 160 | `2739671353` | `sub_14D6650` | 1,232 |
| 161 | `3356234251` | `sub_14C6D70` | 1,136 | 162 | `711267337` | `sub_14D6B20` | 1,136 |
| 163 | `1678905425` | `sub_1522E70` | 1,792 | 164 | `1676021835` | `sub_14F19F0` | 1,520 |
| 165 | `3141538377` | `sub_1510BB0` | 1,648 | 166 | `4199222651` | `sub_14EF590` | 1,536 |
| 167 | `1134825337` | `sub_1510540` | 1,648 | 168 | `3137016288` | `sub_14B16E0` | 960 |
| 169 | `3132166618` | `sub_14B0120` | 960 | 170 | `471143384` | `sub_14BD650` | 1,040 |
| 171 | `1124994826` | `sub_14B04C0` | 960 | 172 | `2522026248` | `sub_14BDA60` | 1,040 |
| 173 | `1061491967` | `sub_14BB290` | 1,008 | 174 | `1054283001` | `sub_14A1370` | 832 |
| 175 | `3047887401` | `sub_14A16B0` | 832 | 176 | `818222286` | `sub_14B97C0` | 976 |
| 177 | `2827096568` | `sub_14A19F0` | 832 | 178 | `811013320` | `sub_14A1D30` | 832 |
| 179 | `1871253608` | `sub_1529FF0` | 1,936 | 180 | `87035282` | `sub_14EFBA0` | 1,552 |
| 181 | `1324158864` | `sub_1515F60` | 1,648 | 182 | `1868370018` | `sub_14F01B0` | 1,520 |
| 183 | `3341423200` | `sub_1515280` | 1,648 | 184 | `3336966647` | `sub_14E4530` | 1,344 |
| 185 | `1314393889` | `sub_14C5B70` | 1,136 | 186 | `2718961951` | `sub_14D4E40` | 1,136 |
| 187 | `3332116977` | `sub_14C5FE0` | 1,136 | 188 | `678630383` | `sub_14D5310` | 1,136 |
| 189 | `1663307831` | `sub_1524310` | 1,776 | 190 | `1660424241` | `sub_14F9330` | 1,552 |
| 191 | `3117421103` | `sub_1518C70` | 1,648 | 192 | `4195552609` | `sub_14F9940` | 1,552 |
| 193 | `1122635615` | `sub_1513F30` | 1,648 | 194 | `3112899014` | `sub_14B2900` | 960 |
| 195 | `3108049344` | `sub_14B2CA0` | 960 | 196 | `438506430` | `sub_14BCA20` | 1,040 |
| 197 | `1112805104` | `sub_14B2560` | 960 | 198 | `2501316846` | `sub_14BD240` | 1,040 |
| 199 | `4177860699` | `sub_14A2070` | 832 | 200 | `4170651733` | `sub_14A23B0` | 832 |
| 201 | `1475745157` | `sub_14A26F0` | 832 | 202 | `73667524` | `sub_14A2A30` | 832 |
| 203 | `70783934` | `sub_14A2D70` | 832 | 204 | `1825641916` | `sub_14AB500` | 912 |
| 205 | `2188906734` | `sub_14A30B0` | 832 | 206 | `3707835116` | `sub_14AB160` | 912 |
| 207 | `2361988723` | `sub_14A33F0` | 832 | 208 | `2354779757` | `sub_14A3730` | 832 |
| 209 | `4177728413` | `sub_14A3A70` | 832 | 210 | `2903577052` | `sub_14A3DB0` | 832 |
| 211 | `201659348` | `sub_14AADC0` | 912 | 212 | `2900693462` | `sub_14A40F0` | 832 |
| 213 | `2307723524` | `sub_14AE420` | 928 | 214 | `948702982` | `sub_14A4430` | 832 |
| 215 | `2868580833` | `sub_14E1B70` | 1,360 | 216 | `2890994171` | `sub_14E5FE0` | 1,360 |
| 217 | `2652508592` | `sub_14DAE90` | 1,248 | 218 | `2674921930` | `sub_14DE410` | 1,280 |
| 219 | `2657489328` | `sub_14E1130` | 1,296 | 220 | `2679902666` | `sub_14E2B00` | 1,296 |
| 221 | `1516180904` | `sub_14A4DF0` | 832 | 222 | `1538594242` | `sub_14A5130` | 832 |
| 223 | `3061191149` | `sub_14E3030` | 1,360 | 224 | `3243250227` | `sub_14A5470` | 832 |
| 225 | `3020952069` | `sub_14A57B0` | 832 | 226 | `3019248130` | `sub_14A5AF0` | 832 |
| 227 | `434050149` | `sub_14A5E30` | 832 | 228 | `3231978109` | `sub_14A6170` | 832 |
| 229 | `303240176` | `sub_14A64B0` | 832 | 230 | `43389887` | `sub_14A67F0` | 832 |
| 231 | `48370623` | `sub_14A6B30` | 832 | 232 | `275189148` | `sub_14F9F50` | 1,552 |
| 233 | `1515589530` | `sub_1516C40` | 1,648 | 234 | `2061111404` | `sub_14F2000` | 1,520 |
| 235 | `3537441386` | `sub_150FED0` | 1,584 | 236 | `2063994994` | `sub_14EDD50` | 1,552 |
| 237 | `86052206` | `sub_14EE360` | 1,552 | 238 | `1850871870` | `sub_14EE970` | 1,552 |
| 239 | `1853755460` | `sub_14EEF80` | 1,552 | 240 | `1308364633` | `sub_14EC510` | 1,552 |
| 241 | `2694582615` | `sub_150F1F0` | 1,648 | 242 | `3298431529` | `sub_14ECB20` | 1,520 |
| 243 | `626594855` | `sub_150F860` | 1,648 | 244 | `3301315119` | `sub_14ED130` | 1,552 |
| 245 | `86510955` | `sub_14ED740` | 1,552 | 246 | `1310855017` | `sub_150DEA0` | 1,648 |
| 247 | `1849954363` | `sub_14F3230` | 1,552 | 248 | `3310228025` | `sub_150C4E0` | 1,584 |
| 249 | `1852837953` | `sub_14F3840` | 1,552 | 250 | `1083838248` | `sub_14F3E50` | 1,552 |
| 251 | `2453999910` | `sub_150BE70` | 1,648 | 252 | `3051426296` | `sub_14F4460` | 1,552 |
| 253 | `363533302` | `sub_150AB20` | 1,584 | 254 | `3054309886` | `sub_14F4A70` | 1,552 |
| 255 | `1088818984` | `sub_14F5080` | 1,552 | 256 | `2458980646` | `sub_15158F0` | 1,648 |
| 257 | `3059290622` | `sub_14F07C0` | 1,552 | 258 | `3056407032` | `sub_14F0DD0` | 1,552 |
| 259 | `368514038` | `sub_15145A0` | 1,648 | 260 | `1354829774` | `sub_14AC380` | 912 |
| 261 | `2779386316` | `sub_14C0730` | 1,056 | 262 | `3398570654` | `sub_14AC720` | 912 |
| 263 | `765072540` | `sub_14C0B40` | 1,024 | 264 | `3401454244` | `sub_14AB8A0` | 912 |
| 265 | `210570726` | `sub_14ABC40` | 912 | 266 | `1475219428` | `sub_14C0320` | 1,024 |
| 267 | `2030440630` | `sub_14ABFE0` | 736 | 268 | `3531018932` | `sub_14BCE30` | 1,040 |
| 269 | `2033324220` | `sub_14ACAC0` | 912 | 270 | `411636811` | `sub_14A6E70` | 832 |
| 271 | `3209564771` | `sub_14A71B0` | 832 | 272 | `2998538731` | `sub_14A74F0` | 832 |
| 273 | `3220836889` | `sub_14A7830` | 832 | 274 | `2996834792` | `sub_14A7B70` | 832 |
| 275 | `280826838` | `sub_14A7EB0` | 832 | 276 | `20976549` | `sub_14A81F0` | 832 |
| 277 | `25957285` | `sub_14A8530` | 832 | 278 | `82382164` | `sub_14F6ED0` | 1,552 |
| 279 | `1835274276` | `sub_14F74E0` | 1,520 | 280 | `1838157866` | `sub_14E8870` | 1,552 |
| 281 | `271519106` | `sub_14E8E80` | 1,552 | 282 | `1503399808` | `sub_1514C10` | 1,648 |
| 283 | `2045513810` | `sub_14E9490` | 1,552 | 284 | `3513324112` | `sub_1511F00` | 1,648 |
| 285 | `2048397400` | `sub_14E9AA0` | 1,552 | 286 | `1304694591` | `sub_14EA0B0` | 1,552 |
| 287 | `2682392893` | `sub_1512570` | 1,648 | 288 | `3282833935` | `sub_14EA6C0` | 1,552 |
| 289 | `602477581` | `sub_1512BE0` | 1,648 | 290 | `3285717525` | `sub_14EACD0` | 1,552 |
| 291 | `1085148942` | `sub_14EB2E0` | 1,552 | 292 | `2446790924` | `sub_1513250` | 1,648 |
| 293 | `3043693028` | `sub_14EB8F0` | 1,552 | 294 | `3040809438` | `sub_14EBF00` | 1,552 |
| 295 | `344396764` | `sub_1511220` | 1,648 | 296 | `82840913` | `sub_14F5690` | 1,552 |
| 297 | `1298665295` | `sub_150B190` | 1,648 | 298 | `1834356769` | `sub_14F5CA0` | 1,552 |
| 299 | `3286110751` | `sub_150B800` | 1,648 | 300 | `1837240359` | `sub_14F62B0` | 1,552 |
| 301 | `1080168206` | `sub_14F68C0` | 1,552 | 302 | `2441810188` | `sub_1517920` | 1,648 |
| 303 | `3038712292` | `sub_14F7AF0` | 1,552 | 304 | `3035828702` | `sub_14F8100` | 1,552 |
| 305 | `339416028` | `sub_1517F90` | 1,648 | 306 | `1351159732` | `sub_14AD5A0` | 928 |
| 307 | `2767196594` | `sub_14BF6F0` | 1,040 | 308 | `3382973060` | `sub_14AD940` | 928 |
| 309 | `740955266` | `sub_14BF2E0` | 1,040 | 310 | `3385856650` | `sub_14ADCE0` | 928 |
| 311 | `206900684` | `sub_14AE080` | 928 | 312 | `1463029706` | `sub_14BFF10` | 1,040 |
| 313 | `2014843036` | `sub_14ACE60` | 912 | 314 | `3506901658` | `sub_14BFB00` | 1,040 |
| 315 | `2017726626` | `sub_14AD200` | 928 | 316 | `3038777811` | `sub_14E25D0` | 1,296 |
| 317 | `3245216309` | `sub_14A8870` | 832 | 318 | `3021214212` | `sub_14A8BB0` | 832 |
| 319 | `275713438` | `sub_14CD380` | 1,136 | 320 | `1516769180` | `sub_14D8330` | 1,136 |
| 321 | `2065436788` | `sub_14CD7F0` | 1,136 | 322 | `2062553198` | `sub_14CDC60` | 1,136 |
| 323 | `3539538540` | `sub_14DA010` | 1,216 | 324 | `1854279747` | `sub_14CE0D0` | 1,136 |
| 325 | `1851396157` | `sub_14CE540` | 1,136 | 326 | `3312325179` | `sub_14D9B40` | 1,136 |
| 327 | `87035245` | `sub_14CE9B0` | 1,136 | 328 | `1312034667` | `sub_14DA4E0` | 1,280 |
| 329 | `3222802971` | `sub_14A8EF0` | 832 | 330 | `2998800874` | `sub_14A9230` | 832 |
| 331 | `272043396` | `sub_14C9560` | 1,136 | 332 | `1504579458` | `sub_14D74C0` | 1,136 |
| 333 | `2049839194` | `sub_14C49B0` | 1,136 | 334 | `2046955604` | `sub_14C4E20` | 1,136 |
| 335 | `3515421266` | `sub_14D7990` | 1,136 | 336 | `83365203` | `sub_14C7F30` | 1,136 |
| 337 | `1299844945` | `sub_14D6180` | 1,136 | 338 | `1838682153` | `sub_14C83A0` | 1,136 |
| 339 | `1835798563` | `sub_14C8810` | 1,136 | 340 | `3288207905` | `sub_14D5CB0` | 1,136 |
| 341 | `2841907644` | `sub_14D31D0` | 1,184 | 342 | `2819494306` | `sub_14D3670` | 2,416 |
| 343 | `2846888380` | `sub_14DB850` | 1,248 | 344 | `2824475042` | `sub_14DBD30` | 1,248 |
| 345 | `3059225067` | `sub_14E4FC0` | 1,360 | 346 | `3036811729` | `sub_14E5510` | 1,360 |
| 347 | `2839941562` | `sub_14E0710` | 1,296 | 348 | `2817528224` | `sub_14E0200` | 1,280 |
| 349 | `2844922298` | `sub_14DE910` | 1,280 | 350 | `2822508960` | `sub_14DEE10` | 1,280 |
| 351 | `3422950081` | `sub_14AF9E0` | 960 | 352 | `3407352487` | `sub_14AEB60` | 928 |
| 353 | `1363152881` | `sub_14AEF00` | 928 | 354 | `1359482839` | `sub_14AF2A0` | 928 |
| 355 | `3425833671` | `sub_14AF640` | 960 | 356 | `3410236077` | `sub_14AE7C0` | 928 |
| 357 | `467604616` | `sub_14A4770` | 832 | 358 | `445191278` | `sub_14A4AB0` | 832 |
| 359 | `1158287159` | `sub_1504EA0` | 1,568 | 360 | `3132756487` | `sub_15054C0` | 1,568 |
| 361 | `3135640077` | `sub_1505AE0` | 1,568 | 362 | `3637908057` | `sub_1506100` | 1,568 |
| 363 | `981800023` | `sub_15192E0` | 1,648 | 364 | `1686312233` | `sub_1504880` | 1,568 |
| 365 | `3560118055` | `sub_151CC40` | 1,536 | 366 | `1689195823` | `sub_14FC3D0` | 1,536 |
| 367 | `3387691560` | `sub_1503C40` | 1,568 | 368 | `715527206` | `sub_151D2A0` | 1,552 |
| 369 | `1413616888` | `sub_1504260` | 1,568 | 370 | `3271366390` | `sub_151C5E0` | 1,632 |
| 371 | `1416500478` | `sub_1503620` | 1,568 | 372 | `1147801373` | `sub_14FC9D0` | 1,536 |
| 373 | `3110343149` | `sub_14FCFD0` | 1,536 | 374 | `3113226739` | `sub_14FD5D0` | 1,536 |
| 375 | `1666782485` | `sub_14FDBD0` | 1,536 | 376 | `1663898895` | `sub_14FE1D0` | 1,536 |
| 377 | `3529185037` | `sub_1519940` | 1,536 | 378 | `3627422271` | `sub_14FE7D0` | 1,536 |
| 379 | `962794557` | `sub_1519FA0` | 1,632 | 380 | `3377205774` | `sub_1506720` | 1,568 |
| 381 | `696521740` | `sub_151F920` | 1,568 | 382 | `1391203550` | `sub_1506D40` | 1,568 |
| 383 | `3240433372` | `sub_151FFA0` | 1,568 | 384 | `1394087140` | `sub_15029E0` | 1,568 |
| 385 | `3392672296` | `sub_14FEDD0` | 1,536 | 386 | `720507942` | `sub_151BF80` | 1,536 |
| 387 | `1421481214` | `sub_14FF3D0` | 1,536 | 388 | `1418597624` | `sub_14FF9D0` | 1,536 |
| 389 | `3276347126` | `sub_151B920` | 1,536 | 390 | `3382186510` | `sub_1507360` | 1,568 |
| 391 | `701502476` | `sub_151F2A0` | 1,568 | 392 | `1396184286` | `sub_1507980` | 1,568 |
| 393 | `3245414108` | `sub_151EC20` | 1,792 | 394 | `1399067876` | `sub_1507FA0` | 1,568 |
| 395 | `1359482725` | `sub_15085C0` | 1,568 | 396 | `2749632867` | `sub_151A600` | 1,648 |
| 397 | `3355054645` | `sub_1508BE0` | 1,568 | 398 | `687150131` | `sub_151D900` | 1,568 |
| 399 | `3357938235` | `sub_1503000` | 1,568 | 400 | `1157959476` | `sub_1509860` | 1,568 |
| 401 | `2532053298` | `sub_15227D0` | 1,712 | 402 | `3131052548` | `sub_1509EA0` | 1,568 |
| 403 | `447091714` | `sub_1521A80` | 1,648 | 404 | `3133936138` | `sub_150A4E0` | 1,568 |
| 405 | `3745714894` | `sub_14B4260` | 928 | 406 | `1127945420` | `sub_14C33E0` | 1,040 |
| 407 | `1847793054` | `sub_14B5C30` | 960 | 408 | `3759937436` | `sub_14C23A0` | 1,040 |
| 409 | `1850676644` | `sub_14B5FE0` | 928 | 410 | `2345603302` | `sub_14B6390` | 944 |
| 411 | `3861910244` | `sub_14C1F90` | 1,040 | 412 | `223810486` | `sub_14B6740` | 944 |
| 413 | `1976047028` | `sub_14C1770` | 1,040 | 414 | `226694076` | `sub_14B6AF0` | 944 |
| 415 | `3735229108` | `sub_14B6EA0` | 928 | 416 | `1108939954` | `sub_14C1B80` | 1,040 |
| 417 | `1825379716` | `sub_14B7250` | 960 | 418 | `3729004418` | `sub_14C2FD0` | 1,040 |
| 419 | `1828263306` | `sub_14B7600` | 960 | 420 | `2335117516` | `sub_14B79B0` | 944 |
| 421 | `3842904778` | `sub_14C2BC0` | 1,040 | 422 | `201397148` | `sub_14B7D60` | 944 |
| 423 | `1945114010` | `sub_14C27B0` | 1,040 | 424 | `204280738` | `sub_14B8110` | 1,008 |
| 425 | `1348996939` | `sub_1500BD0` | 1,536 | 426 | `2730627401` | `sub_151DF60` | 1,632 |
| 427 | `3332641307` | `sub_15011D0` | 1,536 | 428 | `656217113` | `sub_151E5C0` | 1,552 |
| 429 | `3335524897` | `sub_15017D0` | 1,536 | 430 | `1147473690` | `sub_1501DD0` | 1,568 |
| 431 | `2513047832` | `sub_151AC60` | 1,584 | 432 | `3108639210` | `sub_14FFFD0` | 1,536 |
| 433 | `416158696` | `sub_151B2C0` | 1,632 | 434 | `3111522800` | `sub_15005D0` | 1,536 |
| 435 | `1359482727` | `sub_14CF700` | 1,152 | 436 | `2750288229` | `sub_14DDA70` | 1,248 |
| 437 | `3358855741` | `sub_14CFB80` | 1,152 | 438 | `3355972151` | `sub_14D0000` | 1,152 |
| 439 | `688722997` | `sub_14DC210` | 1,248 | 440 | `1348996941` | `sub_14D0480` | 1,152 |
| 441 | `2731282763` | `sub_14DC6F0` | 1,248 | 442 | `3333558813` | `sub_14D0900` | 1,152 |
| 443 | `657789979` | `sub_14DCBD0` | 1,248 | 444 | `3336442403` | `sub_14D0D80` | 1,152 |
| 445 | `1157959478` | `sub_14D28B0` | 1,168 | 446 | `2532708660` | `sub_14DF820` | 1,264 |
| 447 | `3131970054` | `sub_14D2D40` | 1,152 | 448 | `448664580` | `sub_14DFD10` | 1,264 |
| 449 | `3134853644` | `sub_14D2420` | 1,184 | 450 | `1147473692` | `sub_14D1200` | 1,152 |
| 451 | `2513703194` | `sub_14DD0B0` | 1,280 | 452 | `3109556716` | `sub_14D1680` | 1,152 |
| 453 | `417731562` | `sub_14DD590` | 1,248 | 454 | `3112440306` | `sub_14D1B00` | 1,152 |
| 455 | `1884820921` | `sub_14B4610` | 928 | 456 | `1862407583` | `sub_14B49C0` | 928 |
| 457 | `3770356457` | `sub_14B4D70` | 928 | 458 | `3759870671` | `sub_14B5120` | 944 |
| 459 | `1887704511` | `sub_14B54D0` | 944 | 460 | `1865291173` | `sub_14B5880` | 944 |
| 461 | `1944652170` | `sub_14FBDC0` | 1,552 | 462 | `597299025` | `sub_152D850` | 2,128 |
| 463 | `2637630251` | `sub_152E0A0` | 2,128 | 464 | `2351893152` | `sub_152FA20` | 2,176 |
| 465 | `2267286071` | `sub_14D44A0` | 1,232 | 466 | `3536260243` | `sub_153CEF0` | 3,520 |
| 467 | `3543796907` | `sub_155DBE0` | 8,080 | 468 | `263656552` | `sub_14E7680` | 1,488 |
| 469 | `1337398693` | `sub_15330D0` | 2,480 | 470 | `1319900574` | `sub_15213D0` | 1,712 |
| 471 | `626988249` | `sub_1525FA0` | 1,808 | 472 | `3246921673` | `sub_14CF290` | 1,136 |
| 473 | `1681331703` | `sub_15359F0` | 2,784 | — | — | — | — |

### Two-Level Dispatch Protocol

When the parser encounters an instruction:

1. Look up the mnemonic in the named table at `*(context+808)` via `sub_449A80`.
2. If not found, compute the instruction encoding hash and look up in `*(context+816)`.
3. The resolved handler receives `(context, instruction_node)` and generates PTX code template text.

**Combined scale.** The two tables register 588 unique handler functions covering 988.7 KB of code (416.1 KB named + 572.6 KB hash-encoded). This is the complete PTX ISA 9.0 instruction set as implemented by nvlink's embedded assembler. The handler address range spans `0x1493320`--`0x15809F0` (949.7 KB of `.text`).

## Code Template Generators

Approximately 250 functions in the `0x14A0000`--`0x15C0000` range follow an identical code generation pattern. Each handles one specific instruction variant (a particular MMA shape, a barrier variant, an arithmetic operation with specific type combination).

**Common pattern** (verified across `sub_14A4770`, `sub_14AAA30`, `sub_14C3C10`):

```text
1. arena = sub_44F410(a1, a2) + offset[3]     // get memory arena
2. buf   = sub_4307C0(arena, 50000)            // allocate 50KB temp buffer
3. // Build PTX text via successive sprintf() calls:
   //   - Base template from read-only string table (a2 + constant_offset)
   //   - Conditional sections based on sub_16DF3B0 (has descriptor),
   //     sub_16DF3D0 (has scale), sub_16E4530 (matrix dimension)
   //   - Register names, immediates, matrix shapes substituted in
4. len   = strlen(buf)
5. out   = sub_4307C0(arena, len + 1)          // shrink-allocate final string
6. strcpy(out, buf)
7. sub_431000(buf)                             // free temp buffer
8. return out
```

The larger handlers deviate from this pattern by size but not structure. The six largest handlers each exceed 100KB:

| Function | Size | Instruction |
|---|---|---|
| `sub_15B86A0` | 345.2 KB | CUDA builtin prototype generator (608-case switch) |
| `sub_147EF50` | 287.9 KB | Master instruction semantic validator |
| `sub_1487650` | 240.3 KB | Top-level PTX statement processor |
| `sub_146BEC0` | 206.1 KB | Load/store memory operation validator |
| `sub_15809F0` | 170.5 KB | wmma.mma code template generator |
| `sub_157A040` | 118.6 KB | mma (tensor core) code template generator |

### Instruction Property Query Functions

Code template generators query instruction properties through a family of accessor functions in the `0x16DF000`--`0x16E4000` range:

| Function | Description |
|---|---|
| `sub_16DF3B0` | Check if instruction has descriptor field |
| `sub_16DF3D0` | Check if instruction has scale field |
| `sub_16DF450` | Get scale descriptor value |
| `sub_16DF5E0` | Get load descriptor value |
| `sub_16DF5F0` | Get store descriptor value |
| `sub_16E4530` | Get matrix dimension (a/b selector, variant) |
| `sub_16E1030` | Get matrix operation mode |
| `sub_16E2410` | Get immediate value from instruction node |
| `sub_16E32D0` | Get register range start |
| `sub_16E3320` | Get register range end |
| `sub_16E3960` | Get matrix layout/shape |
| `sub_16E36D0` | Check for specific instruction modifier |
| `sub_16DBA40` | Get instruction mnemonic string |
| `sub_16DBB00` | Get instruction type string |
| `sub_16DBDD0` | Get operand type string |
| `sub_16DBE80` | Get scope/modifier string |
| `sub_16DDD30` | Get data type string from enum |

## CUDA Builtin Registration

### sub_158A600 -- cuda_builtin_name_table_init (45,700 bytes, 630 lines)

Registers 608 CUDA runtime builtin function names into a hash table for the instruction lowering pipeline. These builtins are PTX pseudo-functions that the assembler expands into instruction sequences during compilation.

**Data structure.** Creates a hash table via `sub_4489C0` with capacity `0x80`. Allocates a 9,728-byte builtin data array from `unk_1F8E0C0`. Stores: `*(context+1056)` = data array, `*(context+1064)` = hash table, `*(context+1072)` = 608 (entry count).

Each builtin is registered with a sequential index (1--608) mapping the name to an index used by `sub_15B86A0` (the 345KB prototype generator) to produce the corresponding `.weak .func (...) __cuda_smXX_foo (...) ;` declaration.

**Builtin families** (608 total, organized by SM generation and functional category):

| Type | Prefix Pattern | Approx. Count | SM Range |
|---|---|---|---|
| Redux/sync | `__cuda_reduxsync_b32_*` | ~20 | sm_70+ |
| Sanitizer | `__cuda_sanitizer_memcheck_*` | ~10 | all |
| Video emulation | `__cuda_scalar_video_emulation_*` | ~30 | sm_30+ |
| Guardrail traps | `__cuda_sm10x_tcgen05_guardrail_trap_*` | ~8 | sm_100+ |
| Bulk copy | `__cuda_sm1xx_bulk_copy_*`, `__cuda_cp_async_bulk_tensor_*` | ~20 | sm_100+ |
| SM20 math | `__cuda_sm20_div_*`, `__cuda_sm20_rem_*`, `__cuda_sm20_rcp_*`, `__cuda_sm20_sqrt_*`, `__cuda_sm20_dsqrt_*`, `__cuda_sm20_drsqrt_*`, `__cuda_sm20_bfe_*`, `__cuda_sm20_bfi_*` | ~80 | sm_20+ |
| SM3x math | `__cuda_sm3x_div_rn_ftz_*`, `__cuda_sm3x_div_rn_noftz_*` | ~20 | sm_30+ |
| SM62 emulation | `__cuda_sm62_dp2a_*`, `__cuda_sm62_dp4a_*` | ~8 | sm_62 |
| SM70 barriers | `__cuda_sm70_barrier_arrive_*`, `__cuda_sm70_barrier_red_*`, `__cuda_sm70_barrier_sync_*` | ~30 | sm_70+ |
| SM70 warp | `__cuda_sm70_matchsync_*`, `__cuda_sm70_shflsync_*`, `__cuda_sm70_votesync_*`, `__cuda_sm70_warpsync` | ~40 | sm_70+ |
| SM70 WMMA | `__cuda_sm70_wmma_m16n16k16_*`, `__cuda_sm70_wmma_m32n8k16_*`, etc. | ~100+ | sm_70+ |
| SM80+ MMA | `__cuda_sm80_mma_*` and later | ~100+ | sm_80+ |

### sub_15B86A0 -- cuda_builtin_prototype_generator (345,200 bytes, 9,666 lines)

A giant switch on builtin index (608 cases). For each case, allocates a string via `sub_14932E0(length, arena)` and writes the PTX prototype. Returns a string of the form:

```ptx
.weak .func (.param .b32 retval) __cuda_sm20_div_s16 (.param .b32 a, .param .b32 b) ;
```

Covers all SM generations (sm20 through sm10x) and all function families: div, rem, rcp, sqrt, dsqrt, drsqrt, barrier, wmma, shfl, vote, matchsync, warpsync, reduxsync, sanitizer_memcheck, scalar_video_emulation, tcgen05, bulk_copy, cp_async_bulk_tensor.

## Semantic Validation

Instruction validation follows a consistent protocol across all validators:

```text
1. sub_12B3090(context)       // Check "relaxed mode" -- if true, skip all checks
2. sub_12B3CA0(major, minor)  // Check PTX version requirement
3. sub_1441FB0(context, ...)  // Emit error if PTX version too low
4. sub_12A8530(sm_version)    // Check SM target capability
5. sub_14422F0(context, ...)  // Emit error if SM version too low
6. (operand type/count checks)
7. sub_467A70(error_code)     // Emit diagnostic with specific error dword
```

**Error codes** are global `dword_` variables:

| Variable | Meaning |
|---|---|
| `dword_2A5C530` | PTX version requirement not met |
| `dword_2A5C500` | SM version requirement not met |
| `dword_2A5C560` | Target not supported |
| `dword_2A5CFD0` | Unsupported modifier |
| `dword_2A5D680` | Syntax error |
| `dword_2A5CAB0` | Type error |
| `dword_2A5D380` | Duplicate symbol definition |
| `dword_2A5D290` | Parameter error |

### sub_147EF50 -- ptx_instruction_semantic_analyzer (287,900 bytes, 7,803 lines)

The master instruction validator. Takes `(context, char* name, char mode, error_sink)`. Validates every aspect of a PTX instruction against the current SM target and PTX version:

- Dimensions, texture modes (`texmode_independent`, `texmode_raw`, `texmode_unified`)
- Cache policies, state spaces, vector types
- Scoping (`.cta`, `.cluster`, `.sys`), ordering (`.acquire`, `.release`)
- Proxy fences, comparison operators, eviction priorities
- Directives: `.branchtargets`, `.calltargets`, `.callprototype`, `.weak`, `.common`, `.noreturn`, `.abi_preserve`, `.abi_preserve_control`, `.unified`, `.allocno`, `.scratch`, `.retaddr_allocno`, `.ptr`

SM version gates referenced: sm_20, sm_30, sm_32, sm_50, sm_60, sm_61, sm_70, sm_72, sm_75, sm_80, sm_89, sm_90.

### sub_146BEC0 -- ptx_load_store_validator (206,100 bytes, 5,546 lines)

Validates all memory operations: `ld`/`st`, atomics, reductions, `fence`, `membar`, `cp.async`, `st.async`/`red.async`. Checks:

- Vector types (`.v2`, `.v4`, `.v8`), data widths (`.b32`, `.b64`, `.b128`)
- Memory ordering (`.acquire`/`.release`), scope (`.sys`)
- Cache eviction priority for L2
- Address space modifiers (`.shared::cluster`, `.shared::cta`)
- `.sync_restrict`, `.op_restrict`, `.cluster` scope on atomics
- SM gates: sm_11, sm_12, sm_20, sm_30, sm_50, sm_60, sm_80, sm_90
- PTX version gates: 1.1, 1.2, 2.0, 6.0, 7.8, 8.0, 8.1, 8.4, 8.6

References the string `"igonre-src on cp.async"` -- a genuine typo (`"ignore"`) in NVIDIA's source code at approximately offset +1100.

### sub_1487650 -- ptx_statement_processor (240,300 bytes, 6,428 lines)

Top-level PTX statement handler. Processes kernel/function declarations including:

- Performance directives: `.maxnctapersm`, `.minnctapersm`, `.maxntid`, `.reqntid`, `.reqnctapercluster`, `.blocksareclusters`
- Kernel parameter size validation (4,352 byte limit)
- Function attributes: `.noreturn`, `.extern`, `.FORCE_INLINE`, `.unique`
- Start/end label creation: `"__$startLabel$__%s"`, `"__$endLabel$__%s"`
- Parameter registers, scratch registers, return address registers
- PTX version gates: 2.0, 2.1, 2.2, 2.3, 3.0, 3.1, 4.3, 5.0, 6.0, 6.3, 6.4, 7.8, 8.0, 8.1, 8.8, 9.0

### Instruction Modifier Groups

`sub_14871D0` (3,400 bytes, 54 lines) initializes the modifier group enum. Registers bidirectional group mappings via `sub_465720` and `sub_448E70`:

```text
GUARD  <-> PRED     (bidirectional)
TYPES, POSTOP, COMPARE, APRX, FTZ, SAT, SHAMT, ORDER, NC, ROUND,
TESTP, FLOW, TEXTURE, QUERY, CLAMP, SHR, VMAD, PRMT, SHFL,
ENDIS, UNIFORM, VECTOR, VOTE
```

These 25 modifier groups classify the possible instruction suffixes. The `GUARD <-> PRED` bidirectional mapping reflects that guard predicates and predicate operands share the same register file.

## Expression Printer

### sub_12B33A0 -- ptx_print_expression (8,692 bytes, 291 lines)

A recursive pretty-printer that serializes expression AST nodes to text output. Dispatches on `(*node & 0x3F)`, a 6-bit expression kind discriminant:

| Type | Name | Output Format |
|---|---|---|
| 0 | Binary operator | `<left> <op> <right>` (recursive) |
| 1 | Unary operator | `<op> <child>` (recursive) |
| 2 | Integer literal | `%lld` |
| 3 | Float literal | `0D%016llx` (f64) or `0F%08x` (f32) -- PTX hex float format |
| 4 | Variable reference | variable name string |
| 5 | Array index | `<base>[<index>]` |
| 6 | Vector swizzle | `.x`, `.y`, `.z`, `.w` component select |
| 7 | Half-register select | `.h0`--`.h3`, `.b0`--`.b7` component select |
| 8 | Byte-register select | `.b0`--`.b3` component select |
| 9 | Predicate negate | `!` prefix if bit 0 of byte 1 is set |
| 10 | Nop/empty | (nothing) |
| 11 | Parenthesized | `( <child> )` (recursive) |
| 12 | Memory dereference | `[ <child> ]` (recursive) |
| 13 | Label reference | label name |
| 14 | Vector init | `{ <elem>, <elem>, ... }` |
| 15 | Tuple | `( <elem>, <elem>, ... )` |
| 16 | Wildcard | `_` |

The binary operator table at `off_1F24200` has 30 entries (indices 0 through 0x1D), covering arithmetic, bitwise, shift, logical, and comparison operators.

## Symbol Management

### sub_12ADB80 -- ptx_register_function_symbol (5,093 bytes, 171 lines)

Registers a function in the PTX module's symbol table. Allocates an 88-byte function descriptor (type = 4). Checks for `__cudart_` prefix to classify CUDA runtime functions, which are stored in a separate linked list at module offset `+168` (versus offset `+56` for user functions). Parameter lists are linked at offsets `+32` and `+40`.

### sub_12ADFE0 -- ptx_register_global_symbol (5,025 bytes, 153 lines)

Registers a global/shared/const variable. Takes 15 parameters covering all variable attributes. Allocates three structures: 88-byte descriptor (type = 5), 248-byte extended info (at offset `+80`), and 80-byte type descriptor (at extended info offset `+136`).

### sub_12AEA80 -- ptx_resolve_struct_member (7,282 bytes, 278 lines)

Resolves member access on struct types. Walks member name strings character by character, uses `ctype_b_loc()` for digit detection to extract numeric array index suffixes. Temporarily replaces characters with `'<'` during lookup. Recursive: calls `sub_12AE6F0` for base lookup, then recurses on the result for chained member access.

### sub_12B8790 -- ptx_define_global_symbol (6,937 bytes, 264 lines)

Defines a new global symbol with allocation. Takes 10 parameters: `(module, name, storage_class, alignment, size, ...)`. Storage class dispatch:

| Storage class | Address space | Behavior |
|---|---|---|
| 2 | `.shared` | Wraps name as `"$__internal_%d_$%s"` for module version <= 2. Aligns to boundary, updates module offset `+120`. |
| 3 | `.const` | Aligns to boundary, updates module offset `+80`. |
| 5 | Special | Creates `"$ADDRESS$%s"` wrapper symbol with alignment 4 (32-bit) or 8 (64-bit). Triggered by `module->byte[11]` or SM capability above threshold. |

Magic relocation base: `0x70000064` (= `R_CUDA` relocation type constant).

## Bindless Texture Lowering

### sub_12B9660 -- ptx_process_bindless_textures (7,505 bytes, 220 lines)

Processes bindless texture/surface operand pairs from the module. Iterates over three lists: samplers (offset `+144`), textures (offset `+152`), surfaces (offset `+160`). For each (texture, sampler) pair:

1. Query maximum resource limits via vtable calls at offsets `+24+56`, `+24+40`, `+24+48`.
2. Compare actual counts against limits; warn via `sub_467460` if exceeded.
3. Create a merged, sorted list of (texture, sampler) combinations.
4. For each pair within the address space limit, create a `"$BINDLESS$%s$%s$%s"` symbol (module name, texture name, sampler name). Allocate a 112-byte descriptor with type = 4, alignment = 4.
5. Set relocation offset = `vtable[304]() - 0x70000064`.
6. Register relocation entries via `sub_12B8650`.

Unpaired textures get `"$BINDLESS$%s$%s"` (two-part, no sampler) via `sub_12B9320`. Surfaces are processed in a final pass via `sub_465390`.

## DWARF Debug Data Processing

Several functions handle DWARF debug information embedded in PTX via `@@DWARF` directives:

| Function | Size | Description |
|---|---|---|
| `sub_1442040` | 3.3 KB | `@@DWARF` directive handler (also validates `.section`) |
| `sub_1444AB0` | 3.7 KB | `"dwarf data"` processing |
| `sub_14492F0` | 8.3 KB | DWARF data with SM validation (sm_80 gate) |
| `sub_14498C0` | 4.1 KB | DWARF data section handler (`"%s+%llu"`, `"labels + imm expression in .section"`) |
| `sub_1449BA0` | 4.3 KB | DWARF label validator |

## SM100 tcgen05 Intrinsic Codegen

A cluster of functions in the `0x16E0A70`--`0x16E3AB0` range generates PTX inline code for Blackwell (SM100+) tensor core gen05 operations. These are not parser functions per se but are invoked during instruction lowering to produce PTX helper sequences.

### sub_16E0A70 -- tcgen05_instruction_type_classifier (17,302 bytes, 322 lines)

Chains approximately 50 type-check predicates (`sub_12B5670`--`sub_12B5950`) against an instruction object at `*(a1+8)`. Returns an integer type ID (1--54) corresponding to specific tcgen05 MMA variants. Type IDs 1--8 map to base types, 18--29 to extended variants. Modifiers include `_expand16bit`, `_pack16bit`, `_maxabs`, `_minabs`, `_fused`, `_blockscale`, `_ashift`.

### sub_16E1DB0 -- tcgen05_guardrails_codegen (10,365 bytes, 325 lines)

Generates boundary-checking PTX code for tcgen05 tensor memory operations. Emits `"mov.u32 %s, %s;"` and `"mov.u32 %s, %d;"` sequences. References: `"__cuda__sm10x_tcgen05_guardrails_in_physical_bounds_"`, `"__cuda__sm10x_tcgen05_guardrails_are_columns_allocated_"`. Checks SM100/SM10x variants.

### sub_16E2410 -- tcgen05_tmem_addr_compute (3,411 bytes, 111 lines)

Emits `"add.u32 %s, %s, %s;"` for tensor memory address computation. References `"__cuda_sm_100_tcgen05_tmem_addr"`.

### sub_16E3AB0 -- tcgen05_instruction_args_type_mapper (18,623 bytes, 337 lines)

Maps arrays of instruction argument pointers to arrays of numeric type IDs, using the same predicate chain as `sub_16E0A70`. Stores results at `a1+944` per argument, with argument count at base struct offset `+796`.

## PTX Type System

### sub_12A7B90 -- ptx_init_version_table (17,596 bytes, 430 lines)

Initializes the PTX ISA version-to-feature mapping table. The entire body is SIMD divmod-by-10 computation on constant arrays at `xmmword_1F24440`--`1F244E0`. The pattern `q = (x * 0xCCCCCCCD) >> 34; r = x - 10*q` decomposes version numbers into major/minor pairs. Writes 30+ `(quotient, remainder)` pairs. String reference `"%d.%d\n"` confirms version formatting.

### sub_12AA190 -- ptx_get_cvt_opcode (6,332 bytes, 171 lines)

Resolves conversion instruction opcodes from source/destination type pairs. Takes `(src_type, dst_type, rounding_mode, context)`. Returns PTX opcode enum values (25--59). Examples:

- `src=8, dst=7` -> opcode 53 or 55 (f16 to f32 or similar)
- `src=2, dst=3` -> opcode 29 (s32 to s64)
- `src=3, dst=2` -> opcode 28 (s64 to s32)

Calls `sub_12A72D0` to validate that the conversion is legal. Calls `sub_16E08F0`/`sub_16E60D0` for illegal conversion warnings.

### sub_12AA9C0 / sub_12AAD20 -- ptx_canonicalize_opcode (5,110--6,944 bytes)

Opcode renumbering for stable serialization or cross-version compatibility. Maps old opcode IDs to canonical form via a giant if-else chain. Most values map to themselves, but some are reordered: `33->31`, `32->30`, `49->50`, `50->49`, `53->58`, `54->59`, `55->57`, `56->54`, `57->55`, `58->53`, `59->56`.

## Shared Utility Functions

The entire PTX frontend is built on a consistent set of utility primitives:

| Function | Description |
|---|---|
| `sub_467A70(dword)` | Diagnostic/error/warning emitter (takes error code dword) |
| `sub_44F410()` | Memory arena accessor |
| `sub_4307C0(arena, size)` | Arena memory allocator |
| `sub_431000(ptr)` | Arena memory deallocator |
| `sub_45CAC0()` | Out-of-memory handler (abort) |
| `sub_448E70(table, key, val)` | Hash table insert |
| `sub_449A80(table, key)` | Hash table lookup |
| `sub_449BE0(table, key)` | Hash table contains check |
| `sub_4489C0(hash, eq, cap)` | Hash table constructor |
| `sub_44E3A0()` | String comparison/matching |
| `sub_464460(node, next)` | Linked list append |
| `sub_464700(list, cb, ctx)` | Linked list foreach |
| `sub_464BB0(list)` | Linked list count |
| `sub_450280()` | sprintf to output buffer |
| `sub_12B3090()` | Check if in "relaxed mode" (skip validation) |
| `sub_12B3CA0(major, minor)` | PTX version check |
| `sub_12A8530(sm)` | SM version comparison (>= threshold) |
| `sub_16EF370()` | Parser error emitter |

## Context Structure (Partial Reconstruction)

The main compiler context is accessed via `*(a1 + 1096)` throughout the frontend. Key fields in the instruction structure pointed to by this offset:

```text
Offset  Size  Field
  +88    8B   scope/function pointer
 +104    8B   modifier enum table
 +112    8B   modifier group table
 +220    4B   operand count
 +224    4B   primary operand type enum
 +228    4B   secondary operand type enum
 +244    4B   instruction opcode / shape identifier
 +256   40B   operand type descriptors (8 bytes each, 5 operands max)
 +560    4B   parameter stride
 +596    4B   operand count (second field)
 +601    1B   flags (bit 1: has_scale, bits 2-5: type code)
 +602    1B   flags (bits 7-8: block_scale_kind)
 +603    1B   flags (bits 1-2: scale variant)
 +620    1B   instruction subtype byte
 +627    1B   flags (bits 4-5: layout variant)
 +648+   8B   per-operand tree pointers (8 bytes each)
 +776    4B   instruction class (11/12 = MMA class)
 +808    8B   named instruction handler hash table
 +816    8B   hash-encoded instruction handler hash table
+1056    8B   CUDA builtin data array
+1064    8B   CUDA builtin hash table
+1072    4B   CUDA builtin count (608)
+2488    8B   pragma definition table
```

## SM Version Gate Reference

The validators enforce feature availability across GPU generations. This table summarizes when major PTX features became available, as encoded in the validation logic:

| SM Version | Key Features Gated |
|---|---|
| sm_11 | Early shared memory loads |
| sm_12 | Shared memory banking |
| sm_20 | Generic addressing, `membar.sys`, `clock64`, `%pm0`--`%pm7`, math builtins |
| sm_30 | Shuffle, `barrier_sync`, `.branchtargets`, `.callprototype`, `%envreg` |
| sm_50 | `%total_smem_size`, `%dynamic_smem_size` |
| sm_53 | `.f16x2` packed type |
| sm_60 | Atomic scopes |
| sm_62 | `dp2a`/`dp4a` emulation via builtins |
| sm_70 | WMMA, `barrier_sync*`, `matchsync*`, `shflsync*`, `votesync*` |
| sm_72 | Additional MMA shapes |
| sm_75 | Format conversion, `ldmatrix`, cache prefetching |
| sm_80 | `.bf16`, `.tf32`, extended cache, `st.async`, eviction priorities |
| sm_89 | FP8 MMA |
| sm_90 | `wgmma`, `cp.async.bulk.tensor`, cluster scope, `tensormap` |
| sm_100+ | `tcgen05.*`, SM1xx builtins, guardrail traps |

## Cross-References

### nvlink Internal
- [Embedded ptxas Overview](overview.md) -- PTX frontend address map at `0x11EA000`--`0x15C0000`
- [IR Nodes](ir-nodes.md) -- the IR node structures produced by PTX parsing
- [Architecture Dispatch](arch-dispatch.md) -- SM version dispatch tables consulted during parsing
- [LTO Overview](../lto/overview.md) -- how LTO pipeline feeds PTX to the embedded parser

### Sibling Wikis
- [ptxas: PTX Parser](../../ptxas/pipeline/ptx-parser.html) -- standalone ptxas PTX parsing infrastructure
- [ptxas: PTX-to-Ori](../../ptxas/pipeline/ptx-to-ori.html) -- standalone ptxas PTX-to-Ori IR lowering
- [ptxas: PTX Directives](../../ptxas/pipeline/ptx-directives.html) -- directive handling in standalone ptxas
