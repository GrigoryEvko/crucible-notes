# SASS Text Generation

Phases 129 (`DumpNVuCodeText`) and 130 (`DumpNVuCodeHex`) convert the internal instruction stream into human-readable SASS assembly text and raw hex dumps respectively. The text output is the same format produced by `cuobjdump --dump-sass` and is used for `--verbose` output, `DUMPIR` diagnostics, `--forcetext` mode, `--out-sass` dumps, and the `--self-check` roundtrip verification pipeline. The subsystem spans two distinct address ranges: a PTX-level text generation system (580 formatter functions at `0x4DA340`--`0x5A8E40`) and a SASS-level disassembly renderer (~123 virtual printer methods at `0x17F8000`--`0x181FFFF`).

| | |
|---|---|
| **Pipeline phases** | 129 (`DumpNVuCodeText`), 130 (`DumpNVuCodeHex`) |
| **Phase category** | Debug (conditionally executed) |
| **PTX formatter count** | 580 functions at `0x4DA340`--`0x5A8E40` (~850 KB) |
| **PTX dispatcher** | `sub_5D4190` (12.9 KB, two-level opcode dispatch) |
| **SASS printer count** | ~123 vtable methods at `0x17F8000`--`0x181FFFF` |
| **Builder/visitor vtable** | ~520 method slots (4,160+ byte vtable) |
| **Format string table** | ~1.8 MB monolithic NUL-terminated string block |
| **Temp buffer size** | 50,000 bytes per formatter invocation |
| **Largest formatter** | `sub_5A8E40` (wmma.load.b, 9,757 bytes) |
| **Key helpers** | `sub_9D12F0` (operand encoder), `sub_9DB7E0` (predicate printer) |

## Output Format

SASS text generation produces output compatible with `cuobjdump --dump-sass`. The format includes control information (scheduling metadata), predicate guards, opcode mnemonics, operands with modifiers, and optional annotations.

### Instruction Line Format

```
/*ADDR*/  {CTRL} OPCODE{.MODIFIERS}  DST, SRC0{, SRC1{, SRC2}} ;  /* LINE */
```

Concrete examples of the format ptxas produces:

```
/*0000*/                   MOV R1, c[0x0][0x28] ;                /* 0x00000a0004017802 */
/*0010*/                   S2R R0, SR_CTAID.X ;                  /* 0x0000000000007919 */
/*0020*/              @P0  IMAD.MOV.U32 R4, RZ, RZ, c[0x0][0x168] ;
/*0030*/                   IMAD.MOV.U32 R5, RZ, RZ, c[0x0][0x16c] ;
/*0040*/                   ISETP.GE.AND P0, PT, R0, R2, PT ;
/*0050*/              @P0  EXIT ;
/*0060*/                   STG.E [R4.64], R0 ;
/*0070*/                   EXIT ;
/*0080*/                   BRA 0x80 ;
```

### Control Word Format

For architectures with explicit scheduling control (SM 50--SM 70), the control word is printed in a dedicated line before each group of three instructions:

```
      /* 0x001c4400fe2007f6 */
/*0008*/                   MOV R1, c[0x0][0x20] ;
/*0010*/                   S2R R0, SR_TID.X ;
/*0018*/                   S2R R2, SR_CTAID.X ;
```

The 64-bit control word encodes scheduling data for three instructions:

| Field | Bits | Description |
|-------|------|-------------|
| Stall count | 4 bits per instruction | Minimum cycles to wait before issue (0--15) |
| Yield hint | 1 bit per instruction | Suggest warp scheduler switch |
| Write barrier | 3 bits per instruction | Dependency barrier index (0--5, 7 = none) |
| Read barrier | 3 bits per instruction | Read dependency barrier mask |
| Wait barrier mask | 6 bits per instruction | Which barriers to wait on before issue |

For SM 75+ architectures (Turing and later), scheduling information is embedded per-instruction rather than in grouped control words, so the text output places it differently or omits the separate control line.

### Hex Dump Format (Phase 130)

Phase 130 (`DumpNVuCodeHex`) emits the raw encoding bytes as hex values:

```
/*0000*/  0x00000a0004017802
/*0008*/  0x0000000000007919
/*0010*/  0x000000ff0aff7824
```

Each line contains the instruction address and its encoded QWORD(s). For 128-bit instructions, two QWORDs are printed.

## Architecture

The text generation subsystem has two levels: a PTX-level pretty-printer that formats instructions from the Ori IR representation, and a SASS-level disassembly renderer that decodes binary-encoded SASS instructions back to text.

### Level 1: PTX Instruction Text Formatters

This is the primary text generation system. The 580 formatter functions convert internal instruction representations (accessed via the instruction object at `*(a1+1096)`) into PTX assembly text strings.

```
sub_5D4190 (12.9 KB, dispatcher)
  ├─ First: calls sub_5D1660 to initialize intrinsic ID table (608 entries)
  ├─ Registers 121 named opcodes at a1+808 via sub_426150()
  ├─ Registers ~400 hash-keyed opcodes at a1+816 via sub_426150()
  └─ Dispatches to one of 580 formatters at 0x4DA340-0x5A8E40
       └─ Each: alloc 50 KB → sprintf via format table → shrink-copy → free
```

The dispatcher uses a two-level dispatch strategy:

1. **Named dispatch** (121 opcodes): Direct string-to-function registration for recent or complex instructions. The opcode name string (e.g., `"wmma.load.a"`, `"tcgen05.mma"`, `"barrier.cta"`) is looked up in a hash map at `a1+808`.

2. **Hash dispatch** (~400 opcodes): Numeric hash values of opcode names are used as keys in a second hash map at `a1+816`. The hash values are stored as decimal string representations (e.g., `"2644314910"`, `"605425506"`). This covers the stable ISA core -- arithmetic, logic, loads, stores, branches, conversions.

### Level 2: SASS Disassembly Renderer

The SASS printer at `0x17F8000`--`0x181FFFF` operates on binary-encoded SASS instructions and produces text through a builder/visitor pattern. This is used for the `--self-check` roundtrip verification and `--out-sass` output.

```
SASS instruction (binary-encoded)
  │
  ├─ Read opcode at instruction+72, mask BYTE1 &= 0xCF
  ├─ Switch on canonical opcode ID
  │
  ├─ For each operand:
  │    └─ sub_9D12F0(output_128, ctx, instr, operand_idx, stride, mode, flag)
  │         → 64-byte operand encoding structure
  │
  ├─ Emit via builder/visitor vtable at *(a1 + 24):
  │    ├─ vtable+936:  begin_predicate_guard()
  │    ├─ vtable+3768: begin_operands()
  │    ├─ vtable+16:   emit_operand(kind_id, ...)
  │    ├─ vtable+272:  emit_integer(value)
  │    ├─ vtable+1760: set_rounding_mode(mode)
  │    ├─ vtable+3952: emit_saturation_flag()
  │    ├─ vtable+3960: emit_ftz_flag()
  │    ├─ vtable+3968: emit_negate_flag()
  │    ├─ vtable+4072: emit_cache_operation()
  │    ├─ vtable+4080: emit_eviction_hint()
  │    ├─ vtable+944:  end_predicate_guard()
  │    └─ vtable+4160: end_statement()
  │
  └─ Predicate guard: sub_9DB7E0 (662 bytes, 19 callers)
```

The builder/visitor vtable has approximately 520 method slots (vtable spans 4,160+ bytes), making it one of the largest virtual dispatch interfaces in the binary. Different concrete visitor implementations produce different output formats (text, hex, self-check comparison).

## Formatter Template

Every PTX formatter function is mechanically generated from instruction definition tables. All 580 follow an identical structure:

```c
char* format_OPCODE(int64_t a1, int64_t a2) {
    // a1 = instruction context (instruction data at a1+1096)
    // a2 = format string table base pointer (~1.8 MB)

    // Phase 1: Allocate temp buffer
    int64_t pool = ((int64_t*)sub_4280C0(a1, a2))[3];   // arena_get_pool
    char* buf = (char*)sub_424070(pool, 50000);           // pool_alloc(50KB)
    if (!buf) sub_42BDB0(pool, 50000, ...);               // alloc_fail_abort

    // Phase 2: Build instruction text via sprintf chain
    int pos = sprintf(buf, "%s", (char*)(a2 + OFFSET_A)); // opcode prefix
    if (sub_70B6E0(*(a1+1096)))                           // has_predicate?
        pos += sprintf(buf+pos, fmt, sub_70B780(*(a1+1096))); // predicate name
    pos += sprintf(buf+pos, "%s", (char*)(a2 + OFFSET_B)); // operand template
    // ... more operands via sub_70B8E0, sub_70B910, sub_70B920 ...
    strcpy(buf+pos, (char*)(a2 + OFFSET_N));              // trailing text

    // Phase 3: Shrink-copy to exact size
    size_t len = strlen(buf);
    int64_t pool2 = ((int64_t*)sub_4280C0(buf, ...))[3];
    char* result = (char*)sub_424070(pool2, len + 1);
    strcpy(result, buf);

    // Phase 4: Free temp buffer
    sub_4248B0(buf);                                       // pool_free
    return result;
}
```

The format string table (`a2`) is a single monolithic ~1.8 MB block of NUL-terminated strings containing pre-assembled text templates with `%s`, `%llu`, `%d` placeholders. Different formatters access it at different offsets:

| Formatter | Offset into `a2` | Approximate position |
|-----------|-------------------|---------------------|
| wgmma.mma_async | 1,731,609 | ~1.7 MB |
| wmma.mma | 1,731,130 | ~1.7 MB |
| rsqrt | 67,573 | ~67 KB |
| copysign | 110,152 | ~110 KB |
| vavrg4 | 286,309 | ~286 KB |
| guardrails.alloc | ~1,843,620 | ~1.8 MB |

This design trades memory for speed: instead of building instruction text dynamically, ptxas stores the complete format template and fills in operand names at runtime.

## Instruction Operand Accessors

All formatters query the instruction object through a uniform set of tiny accessor functions:

| Address | Size | Callers | Identity |
|---------|------|---------|----------|
| `sub_70B700` | 14 B | 946 | `has_predicate()` |
| `sub_70B6E0` | 14 B | 42 | `has_predicate_v2()` |
| `sub_70B710` | 111 B | 348 | `get_opcode_string()` |
| `sub_70B780` | 151 B | 514 | `get_predicate_name()` |
| `sub_70B8E0` | 12 B | 1,449 | `get_reg_operand(idx)` |
| `sub_70B910` | 12 B | 1,656 | `get_src_part0(idx)` |
| `sub_70B920` | 12 B | 1,296 | `get_src_part1(idx)` |
| `sub_70B930` | 7 B | 68 | `get_operand_count()` |
| `sub_70B4C0` | 22 B | 46 | `get_base_address()` |
| `sub_70CA60` | 11 B | 480 | `get_operand_type(idx)` |
| `sub_70CA70` | 427 B | 191 | `get_type_suffix()` |
| `sub_70CD20` | 122 B | 158 | `get_operand_offset(idx)` |
| `sub_710860` | 39 B | 2,953 | `get_data_type(idx, part)` |
| `sub_70FA00` | 10 B | 286 | `get_target_sm(idx)` |
| `sub_70FA10` | 66 B | 7 | `check_target_sm(idx, str)` |
| `sub_709910` | 14 B | 13 | `get_variant_count()` |
| `sub_709A10` | 73 B | 46 | `get_variant_string()` |
| `sub_707CE0` | 22 B | 93 | `get_address_operand(idx)` |
| `sub_709760` | 127 B | 21 | `get_comparison_op()` |
| `sub_709FE0` | 11 B | 17 | `get_rounding_mode()` |
| `sub_70A500` | 13 B | 15 | `get_saturation_mode()` |
| `sub_70B3F0` | -- | -- | `get_ftz_flag()` |
| `sub_707530` | -- | -- | `get_precision_string()` |
| `sub_707C80` | -- | -- | `get_scope_string()` |
| `sub_7075E0` | -- | -- | `get_layout_string()` |
| `sub_707BE0` | -- | -- | `get_shape_string()` |
| `sub_70A810` | -- | -- | `get_scale_string()` |

All accessors read from the instruction object at `*(a1+1096)`. The tiny sizes (7--151 bytes for most) indicate these are simple field extractions from the instruction record.

## Memory Allocation

The formatter memory lifecycle uses a pool allocator:

| Function | Size | Callers | Identity |
|----------|------|---------|----------|
| `sub_4280C0` | 597 B | 3,928 | `arena_get_pool(ctx, table)` |
| `sub_424070` | 2,098 B | 3,809 | `pool_alloc(pool, size)` |
| `sub_42BDB0` | 14 B | 3,825 | `alloc_fail_abort()` |
| `sub_4248B0` | 923 B | 1,215 | `pool_free(ptr)` |

Every formatter allocates a 50,000-byte temporary buffer, builds the instruction string via `sprintf` chains, measures the result with `strlen`, allocates an exact-size copy, and frees the temporary. The 50 KB buffer provides headroom for the largest instructions (WMMA loads produce multi-KB strings) but is wasteful for simple 2-operand instructions that generate ~50-byte strings.

## Predicate Guard Printing

Predicate guards (`@P0`, `@!P1`, etc.) are printed by checking `has_predicate()` on the instruction, then formatting the guard via `get_predicate_name()`:

```c
// PTX-level predicate printing (in every formatter)
int pos = sprintf(buf, "%s", opcode_prefix);
if (sub_70B6E0(*(a1+1096))) {                     // has_predicate?
    int64_t pred = sub_70B780(*(a1+1096));         // get_predicate_name
    pos += sprintf(buf+pos, guard_fmt, pred);      // e.g., "@P0 " or "@!P1 "
}

// SASS-level predicate printing (in disassembly renderer)
// sub_9DB7E0 (662 bytes, 19 callers) — emits guard through builder vtable
//   calls builder->begin_predicate_guard() at vtable+936
//   emits predicate register name
//   calls builder->end_predicate_guard() at vtable+944
```

## Register and Operand Formatting

Register operands are resolved from the instruction's operand array. The formatter accesses operands by index through `get_reg_operand(idx)`, `get_src_part0(idx)`, and `get_src_part1(idx)`. The standard register naming follows NVIDIA conventions:

| Register class | Naming | Examples |
|----------------|--------|----------|
| General-purpose | `R0`--`R255` | `R0`, `R4`, `R255` |
| Zero register | `RZ` | `RZ` |
| Predicate | `P0`--`P6`, `PT` | `@P0`, `PT` |
| Uniform | `UR0`--`UR63` | `UR4`, `UR16` |
| Uniform predicate | `UP0`--`UP6`, `UPT` | `UP0` |
| Constant buffer | `c[bank][offset]` | `c[0x0][0x168]` |
| Special | `SR_*` | `SR_CTAID.X`, `SR_TID.X` |

For the SASS disassembly renderer, the register class discriminator `sub_91C840` (347 bytes, 232 callers) maps internal type codes 1--0x17 to output class IDs 0--18, covering integer registers, float registers, double registers, predicate registers, condition registers, texture/surface references, and uniform registers.

The operand encoder `sub_9D12F0` (1,423 bytes, 289 callers) is the core serializer for SASS-level printing. It takes an instruction and operand index, resolves whether the operand is a register, immediate, or memory reference, handles constant buffer lookups, and fills a 64-byte (4x `__m128i`) encoding structure that the builder/visitor consumes.

## Address and Offset Formatting

Memory operands are formatted with address space qualifiers and offset expressions:

```
[R4.64]              — register indirect, 64-bit
[R4+0x10]            — register + offset
c[0x0][0x168]        — constant buffer bank 0, offset 0x168
[UR4]                — uniform register indirect
```

The address space qualifier resolver `sub_9CEB50` (185 bytes, 57 callers) combines address space information from the operand descriptor with the instruction context. For SASS-level output, the address space emitter `sub_9E7B00` and related functions (`sub_9E9910`, `sub_9E9A70`) handle data type and memory space qualifiers.

## Architecture-Conditional Formatting

86 of the 580 formatters contain architecture-conditional paths that check the target SM version via `sub_70FA00` (numeric comparison) or `sub_70FA10` (string comparison). Architecture-specific formatting reflects hardware evolution:

| SM | Era | Formatting impact |
|----|-----|-------------------|
| sm_20, sm_21 | Fermi (2010) | `copysign` has different operand layout (7 vs 5 fields) |
| sm_62 | Pascal mobile (2016) | `vavrg4` gets per-component register formatting |
| sm_103 | Blackwell Ultra (2025) | `rsqrt` gains new operand layout for extended precision |

Five formatters additionally use string-based SM comparison via `sub_70FA10`:

- `sub_4DD860` (`copysign`): checks `"sm_20"`, `"sm_21"`
- `sub_56BA60` (`vavrg4`): checks `"sm_62"`
- `sub_56C8D0` (`dp2a.lo`): SM string comparison
- `sub_577BA0` (`dp2a.hi`): SM string comparison
- `sub_583190` (`rsqrt`): checks `"sm_103"`

## SASS Disassembly Printer Subsystem

The SASS-level printer at `0x17F8000`--`0x181FFFF` handles disassembly of binary-encoded SASS instructions. Unlike the PTX formatters which work from the high-level IR, these printers decode the binary instruction representation and emit text through a virtual builder/visitor interface.

### Builder/Visitor Vtable

The builder object at `*(a1 + 24)` exposes a massive vtable with ~520 method slots:

| Vtable offset | Method | Purpose |
|---------------|--------|---------|
| +16 | `emit_operand` | Emit a decoded operand |
| +208 | `emit_literal` | Emit a literal string |
| +272 | `emit_integer` | Emit an integer value |
| +368 | `set_address_space` | Set memory address space qualifier |
| +936 | `begin_predicate` | Open predicate guard block |
| +944 | `end_predicate` | Close predicate guard block |
| +1760 | `set_rounding_mode` | Emit rounding mode modifier |
| +3520 | `set_width` | Set operand width |
| +3560 | `set_conversion` | Set conversion modifier |
| +3760 | `set_sync_type` | Set synchronization type |
| +3768 | `begin_operands` | Open operand section |
| +3824 | `emit_tex_header` | Emit texture header index |
| +3952 | `emit_saturation` | Emit `.SAT` flag |
| +3960 | `emit_ftz` | Emit `.FTZ` flag |
| +3968 | `emit_negate` | Emit negate modifier |
| +4072 | `emit_cache_op` | Emit cache operation hint |
| +4080 | `emit_eviction` | Emit cache eviction priority |
| +4160 | `end_statement` | Close instruction statement |

### Instruction Format Class Printers

The printer functions at `0x1810D20`--`0x1816FC0` handle specific instruction format classes. Each reads the format class from `instruction+76`, subtracts 11 for table indexing via `dword_23B39E0[]`, and dispatches to the appropriate rendering logic:

| Function | Size | Purpose |
|----------|------|---------|
| `sub_1810D20` | 8.8 KB | Comparison-mode instructions (SETP, SET) |
| `sub_18111F0` | 11.6 KB | Wide-operand instructions (8 sequential operand slots) |
| `sub_1811E20` | 11.6 KB | Wide-operand with special encodings |
| `sub_1812890` | 10.5 KB | Combined register + constant operand instructions |
| `sub_1812F60` | 15.3 KB | 16-DWORD immediate instructions (bulk constant loads) |
| `sub_18141C0` | 6.5 KB | Per-operand comparison mode |
| `sub_1814660` | 7.1 KB | Load/store with address space encoding |
| `sub_1814B10` | 17.6 KB | Load/store with predication and constant buffer |
| `sub_18189C0` | 45.2 KB | Texture/surface instruction printer (largest) |
| `sub_181B370` | 27.8 KB | Multi-operand instructions (VOTE, etc.) |
| `sub_181CF60` | 14.0 KB | Predicated instruction printer |
| `sub_181D9B0` | 12.6 KB | Load/store variant printer |

The texture/surface printer `sub_18189C0` is the largest at 45.2 KB. It handles the full TEX, TLD, TXQ, SULD, SUST, and SURED instruction families through a giant switch on opcodes 18, 119, 186, 211, 283, and 315. It uses lookup tables `dword_23B39E0[10]` for format class mapping and `word_23B3A58[4]` for subtype resolution, and calls `sub_1817C50` (12.8 KB) for texture header index computation.

### Encoding Template Builders

Functions at `0x17F8000`--`0x180FFFF` (~75 functions) build instruction format descriptors. Each sets:

```c
*(a2+12) = SASS_OPCODE_ID;           // e.g., 274, 285, 172
*(a1+8)  = xmmword_23Fxxxx;          // 128-bit descriptor from rodata
// a1+24..32 = operand type/register/immediate slots (up to 10)
```

These are vtable entry points with zero static callers, confirming virtual dispatch.

## CLI Integration

### `--verbose` / `-v`

Enables printing of code generation statistics after compilation. The statistics printers at `sub_ABBA50`--`sub_ABEB50` (8 SM-variant clones, 7,603 bytes each) emit post-scheduling metrics in `"# [...] "` comment format.

### `--forcetext`

Forces text-mode SASS output regardless of the default binary output mode. Internal flag: `"forcetext=%d"`.

### `--out-sass`

Generates reconstituted SASS text from the Capsule Mercury representation. Used for debugging the capmerc encode/decode roundtrip. Triggers the SASS text Flex lexer `sub_720F00` (64 KB) for parsing in `--self-check` mode.

### `--self-check`

Roundtrip verification for Capsule Mercury: encodes the instruction stream to capmerc format, decodes it back, renders both original and reconstituted as SASS text, and compares. The Flex lexer at `sub_720F00` parses the text output for comparison. The SASS text formatter `sub_719D00` (50 KB) builds the output for self-check.

### DUMPIR

The `DUMPIR` environment variable (and related knobs) triggers intermediate representation dumps at named phases. Phase 129 (`DumpNVuCodeText`) is one of the dump targets, emitting the full instruction stream as formatted text when DUMPIR includes that phase name.

## Formatter Size Distribution

Function size directly correlates with PTX instruction complexity:

| Tier | Size range | Count | Description |
|------|-----------|-------|-------------|
| Tiny | < 500 B | 13 | Simple 2-operand (wgmma.fence: 295 B) |
| Small | 500--1,000 B | 191 | Standard 3--4 operand (copysign: 794 B) |
| Medium | 1,000--2,000 B | 319 | Instructions with modifiers (bfind: 1,130 B) |
| Large | 2,000--4,000 B | 36 | Arch-conditional paths (membar: 2,788 B) |
| Very large | 4,000--6,000 B | 20 | Complex multi-form (tex.grad: 5,636 B) |
| Monster | 6,000--10,000 B | 2 | WMMA matrix loads (wmma.load.b: 9,757 B) |

The WMMA load/store formatters account for 34,423 bytes (4% of the total range), reflecting the combinatorial explosion of matrix shapes, data types, layouts, and architectures.

## Named Opcode Dispatch Table

The 121 named opcodes registered at `a1+808` by `sub_5D4190`:

| Category | Opcodes |
|----------|---------|
| Memory fence | `membar` |
| Conversion | `cvt`, `tensormap.replace` |
| Math | `div`, `div.full`, `rem`, `rcp`, `rsqrt`, `ex2`, `lg2`, `sqrt`, `tanh`, `copysign` |
| Bit manipulation | `bfind`, `brev`, `bfe`, `bfi`, `clz`, `popc`, `testp` |
| Load/store | `_ldldu`, `ldmatrix`, `movmatrix`, `stmatrix`, `st.async`, `red.async`, `st.bulk`, `prefetch` |
| Texture | `tex`, `tex.base`, `tex.level`, `tex.grad`, `tld4`, `sured.b` |
| Video SIMD | `vadd`--`vmad`, `vadd2`--`vavrg2`, `vadd4`--`vavrg4` |
| Dot product | `dp2a.lo`, `dp2a.hi`, `dp4a` |
| Barriers | `bar`, `barrier`, `bar.arrive`, `barrier.arrive`, `bar.red`, `barrier.red`, `bar.cta`, `barrier.cta`, + `.arrive`/`.red` variants, `bar.warp` |
| Warp ops | `vote`, `shfl`, `match`, `redux` |
| Async copy | `cp.async.mbarrier.arrive`, `cp.async.bulk`, `cp.async.bulk.tensor` |
| Cache policy | `createpolicy.range`, `createpolicy.fractional`, `createpolicy.cvt` |
| Multi-memory | `multimem.ld_reduce`, `multimem.st`, `multimem.red` |
| WMMA | `wmma.load.a`, `wmma.load.b`, `wmma.load.c`, `wmma.store.d`, `wmma.mma`, `mma` |
| WGMMA | `wgmma.mma_async`, `wgmma.fence`, `wgmma.commit_group`, `wgmma.wait_group` |
| TCGen05 | `tcgen05.alloc`, `tcgen05.relinquish_alloc_permit`, `tcgen05.dealloc`, `tcgen05.ld`, `tcgen05.ld.red`, `tcgen05.st`, `tcgen05.commit`, `tcgen05.cp`, `tcgen05.shift`, `tcgen05.mma`, `tcgen05.mma.ws` |
| Guardrails | `_tcgen05.guardrails.is_phase_valid`, `.are_columns_allocated`, `.is_current_warp_valid_owner`, `.in_physical_bounds`, `.allocation_granularity`, `.datapath_alignment`, `.sp_consistency_across_idesc_mod`, `.check_sparse_usage` |

The remaining ~400 opcodes (arithmetic, logic, load/store, control flow, etc.) are dispatched through hash values at `a1+816`.

## SASS Printer Key Functions

| Address | Size | Callers | Identity |
|---------|------|---------|----------|
| `sub_5D4190` | 12.9 KB | 1 | PTX instruction text dispatch + intrinsic registration |
| `sub_5D1660` | 46 KB | 1 | Intrinsic library registration (608 entries) |
| `sub_5FF700` | 354 KB | -- | Builtin function declaration emitter (prototype generator) |
| `sub_4DA340` | 61 B | 1,080 | Builtin declaration lookup helper |
| `sub_719D00` | 50 KB | -- | SASS text formatter (self-check output builder) |
| `sub_720F00` | 64 KB | -- | Flex lexer for SASS text parsing (self-check input) |
| `sub_9D12F0` | 1.4 KB | 289 | Operand encoder (64-byte struct per operand) |
| `sub_9DB7E0` | 662 B | 19 | Predicate guard printer |
| `sub_91C840` | 347 B | 232 | Register class discriminator |
| `sub_9CEB50` | 185 B | 57 | Address space qualifier resolver |
| `sub_91E860` | 31 B | 214 | Data size accessor |
| `sub_18189C0` | 45.2 KB | -- | Texture/surface instruction printer (largest SASS printer) |
| `sub_181B370` | 27.8 KB | -- | Multi-operand instruction printer |
| `sub_1817C50` | 12.8 KB | -- | Texture header index encoder |

## Instruction Data Flow

```
                    ┌──────────────────────────────────┐
                    │  Ori IR Instruction Object        │
                    │  (instruction data at *(a1+1096)) │
                    └────────────────┬─────────────────┘
                                     │
               ┌─────────────────────┼──────────────────────┐
               │                     │                      │
               v                     v                      v
   sub_70B6E0/B700          sub_70B8E0/B910/B920     sub_70CA60/CA70
   has_predicate()          get_reg_operand(idx)      get_operand_type()
   get_predicate_name()     get_src_part0/1(idx)      get_type_suffix()
               │                     │                      │
               └─────────────────────┼──────────────────────┘
                                     │
                                     v
                          ┌─────────────────────┐
                          │  sprintf() chain     │
                          │  into 50 KB buffer   │
                          │  using format table  │
                          │  at a2+offset        │
                          └──────────┬──────────┘
                                     │
                                     v
                          ┌─────────────────────┐
                          │  strlen → alloc →    │
                          │  strcpy → free temp  │
                          └──────────┬──────────┘
                                     │
                                     v
                          ┌─────────────────────┐
                          │  Formatted PTX text  │
                          │  string (exact size) │
                          └─────────────────────┘
```

## Cross-References

- [Code Generation Overview](./overview.md) -- pipeline context and subsystem map
- [SASS Instruction Encoding](./encoding.md) -- binary encoding format that this subsystem renders
- [Mercury Encoder Pipeline](./mercury.md) -- source of instructions for text generation
- [Capsule Mercury & Finalization](./capmerc.md) -- `--self-check` and `--out-sass` integration
- [CLI Options](../config/cli-options.md) -- `--verbose`, `--forcetext`, `--out-sass` flags
- [Knobs System](../config/knobs.md) -- DUMPIR knob triggering phase 129/130
- [Phase Manager](../passes/phase-manager.md) -- phase 129/130 registration and execution
