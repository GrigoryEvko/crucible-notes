# SASS Text Generation

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

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

```asm
/*ADDR*/  {CTRL} OPCODE{.MODIFIERS}  DST, SRC0{, SRC1{, SRC2}} ;  /* LINE */
```

Concrete examples of the format ptxas produces:

```asm
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

For architectures with explicit scheduling control (SM 50–SM 70), the control word is printed in a dedicated line before each group of three instructions:

```asm
      /* 0x001c4400fe2007f6 */
/*0008*/                   MOV R1, c[0x0][0x20] ;
/*0010*/                   S2R R0, SR_TID.X ;
/*0018*/                   S2R R2, SR_CTAID.X ;
```

The 64-bit control word encodes scheduling data for three instructions:

| Field | Bits | Description |
|-------|------|-------------|
| Stall count | 4 bits per instruction | Minimum cycles to wait before issue (0–15) |
| Yield hint | 1 bit per instruction | Suggest warp scheduler switch |
| Write barrier | 3 bits per instruction | Dependency barrier index (0–5, 7 = none) |
| Read barrier | 3 bits per instruction | Read dependency barrier mask |
| Wait barrier mask | 6 bits per instruction | Which barriers to wait on before issue |

For SM 75+ architectures (Turing and later), scheduling information is embedded per-instruction rather than in grouped control words, so the text output places it differently or omits the separate control line.

### Hex Dump Format (Phase 130)

Phase 130 (`DumpNVuCodeHex`) emits the raw encoding bytes as hex values:

```text
/*0000*/  0x00000a0004017802
/*0008*/  0x0000000000007919
/*0010*/  0x000000ff0aff7824
```

Each line contains the instruction address and its encoded QWORD(s). For 128-bit instructions, two QWORDs are printed.

## Architecture

The text generation subsystem has two levels: a PTX-level pretty-printer that formats instructions from the Ori IR representation, and a SASS-level disassembly renderer that decodes binary-encoded SASS instructions back to text.

### Level 1: PTX Instruction Text Formatters

This is the primary text generation system. The 580 formatter functions convert internal instruction representations (accessed via the instruction object at `*(a1+1096)`) into PTX assembly text strings.

```text
sub_5D4190 (12.9 KB, dispatcher)
  ├─ First: calls sub_5D1660 to initialize intrinsic ID table (608 entries)
  ├─ Registers 121 named opcodes at a1+808 via sub_426150()
  ├─ Registers 473 variant-keyed opcodes at a1+816 via sub_426150()
  └─ Dispatches to one of 580 formatters at 0x4DA340-0x5A8E40
       └─ Each: alloc 50 KB → sprintf via format table → shrink-copy → free
```

The dispatcher uses a two-level dispatch strategy:

1. **Named dispatch** (121 opcodes): Direct string-to-function registration for recent or complex instructions. The opcode name string (e.g., `"wmma.load.a"`, `"tcgen05.mma"`, `"barrier.cta"`) is looked up in a hash map at `a1+808`.

2. **Variant-ID dispatch** (473 entries): Composite 32-bit variant identifiers are used as keys in a second hash map at `a1+816`. The caller computes a variant ID from the instruction's internal representation (Ori opcode, PTX type, modifiers), converts it to a decimal string via `sprintf("%u", variant_id)`, and looks up the corresponding formatter. Keys like `"2644314910"` and `"605425506"` are the decimal encodings of these IDs. This covers the stable ISA core — arithmetic, logic, loads, stores, branches, conversions — plus MMA/WMMA shape+type+layout variants.

Both maps use the same MurmurHash3-based hash map infrastructure (`sub_427630` for bucket hashing, `sub_426150` for insert, `sub_426D60` for lookup). The variant-ID keys are structured — bits 8-15 encode a PTX type discriminator (16 unique values spanning 9–24, corresponding to type codes like `.s32`, `.f32`, `.f64`), and related variants (e.g., same opcode with different types) cluster with small numeric deltas. Pairs differing only in layout produce a fixed hi16 delta of 76, confirming the ID encodes multiple orthogonal fields rather than being a hash of the opcode name string.

```text
Variant ID structure (32-bit, approximate field boundaries):
  bits 0-7:    sub-variant / modifier discriminator (203 unique values)
  bits 8-15:   PTX type code (16 values: 9=.pred .. 24=.bf16x2)
  bits 16-31:  opcode + layout composite (467 unique hi16 values)
```

### Phase Ordering — Text vs Encoding

The late Mercury phase-label table at `0x22bd400` orders the relevant tail phases as `MercGenerateSassUCode → ReportFinalMemoryUsage → FormatCodeList → DumpNVuCodeText → DumpNVuCodeHex`. The two SASS-named phases are easily confused: `MercGenerateSassUCode` is the SASS **encoding-generation** phase (it produces the binary instruction words) and is **not** the text printer. `DumpNVuCodeText` (phase 129) is the SASS-**text** phase, and `DumpNVuCodeHex` (phase 130) is the raw-hex phase. Only the latter two consume the disassembly renderer described below.

### Level 2: SASS Disassembly Renderer

The SASS printer at `0x17F8000`--`0x181FFFF` operates on binary-encoded SASS instructions and produces text through a builder/visitor pattern. This is used for the `--self-check` roundtrip verification and `--out-sass` output. The text builder `sub_719D00` (50 KB) carries **zero inline format strings** — it is entirely table+vtable driven: the opcode at `instruction+72` indexes a ROT13-obfuscated mnemonic table, which selects modifier templates, then operands flow through the operand encoder and predicate/control flags through the builder vtable methods. A Flex lexer (`sub_720F00`) re-parses the rendered SASS text for `--self-check`.

The mnemonic and modifier names are resolved by `sub_7CB560` and `sub_896D50` (1,026 table references each) against the ROT13 opcode-name region at `0x21C1336` (322 primary names plus 385 Mercury-extended names) and the modifier-format-string region at `0x21C5E00`. Modifier formatting proper runs through `sub_7A5D10`, `sub_7C5410`, and `sub_BE7390` (412 references each, into `0x21C5E00`–`0x21CEE00`). Because the opcode names are stored ROT13-encoded — e.g. ROT13 `VZNQ` decodes to `IMAD`, `SSZN` to `FFMA` — the resolver applies the rotation at print time rather than storing plaintext mnemonics in the binary.

```text
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
| `sub_70B3F0` | — | — | `get_ftz_flag()` |
| `sub_707530` | — | — | `get_precision_string()` |
| `sub_707C80` | — | — | `get_scope_string()` |
| `sub_7075E0` | — | — | `get_layout_string()` |
| `sub_707BE0` | — | — | `get_shape_string()` |
| `sub_70A810` | — | — | `get_scale_string()` |

All accessors read from the instruction object at `*(a1+1096)`. The tiny sizes (7–151 bytes for most) indicate these are simple field extractions from the instruction record.

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

For the SASS disassembly renderer, the register class discriminator `sub_91C840` (347 bytes, 232 callers) maps internal type codes 1–0x17 to output class IDs 0–18, covering integer registers, float registers, double registers, predicate registers, condition registers, texture/surface references, and uniform registers.

The operand encoder `sub_9D12F0` (1,423 bytes, 289 callers) is the core serializer for SASS-level printing. It takes an instruction and operand index, resolves whether the operand is a register, immediate, or memory reference, handles constant buffer lookups, and fills a 64-byte (4x `__m128i`) encoding structure that the builder/visitor consumes.

## Address and Offset Formatting

Memory operands are formatted with address space qualifiers and offset expressions:

```text
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

## SASS Disassembly Renderer

The SASS-level renderer at `0x17F8000`--`0x181FFFF` (~160 KB, ~123 virtual entry points) converts binary-encoded SASS instructions into textual SASS assembly. Unlike the PTX formatters (Level 1) which work from the high-level Ori IR via `sprintf` chains, the SASS renderer decodes the binary instruction encoding and drives a builder/visitor object through a structured sequence of `emit_*` calls. The builder's concrete implementation determines the output format — text for `--out-sass`, comparison data for `--self-check`, or binary encoding verification.

### Internal Layers

The subsystem splits into five layers by address range and function role:

| Layer | Range | Count | Role |
|-------|-------|-------|------|
| A: Encoding templates | `0x17F8000`--`0x180FFFF` | ~75 | Build per-opcode operand layout descriptors |
| B: Accessor vtable methods | `0x1810700`--`0x1810BFF` | ~15 | ISA version/class discriminator predicates |
| C: Format-class printers | `0x1810D20`--`0x18167FF` | ~50 | Workhorses: decode operands + emit through builder |
| D: Complex multi-format printers | `0x1817000`--`0x181CFFF` | ~15 | Texture, multi-operand, predicated printers |
| E: Post-processing hooks | `0x181E000`--`0x181FFFF` | ~8 | ISA-override detection, fixup dispatch |

All ~123 entry points have zero static callers, confirming they are virtual method overrides dispatched through vtables. The printer dispatch layer at `0xAA8000`--`0xACA000` (`sub_AA9330`, `sub_AA9860`, `sub_AAB9C0`, `sub_AC99D0`) invokes them.

### Rendering Protocol

Every SASS printer receives `(a1, a2)` where `a1` is the printer context (builder pointer at `a1+24`) and `a2` is the binary-encoded instruction. The rendering follows a fixed protocol:

```text
1. vtable[0](builder, instruction_kind_id)     // begin_instruction
2. vtable[3760](builder, sync_mode)            // set_sync_type (if applicable)
3. vtable[3768](builder)                       // begin_operand_list
4. sub_9DB7E0(a1, a2, 1)                       // emit predicate guard (@Px)
5. For each operand:
   a. sub_9D12F0(&buf, a1, a2, idx, stride, mode, flag)  // encode -> 64B struct
   b. vtable[16](builder, kind_id, buf...)                // emit_operand
6. vtable[3952](builder)                       // emit_saturation (.SAT)
7. vtable[3960](builder)                       // emit_ftz (.FTZ)
8. vtable[3968](builder)                       // emit_negate (.NEG)
9. vtable[4072](builder)                       // emit_cache_operation
10. vtable[4080](builder)                      // emit_eviction_hint
11. vtable[4160](builder)                      // end_instruction
```

The protocol is directly visible in decompiled code. In `sub_1812F60` (16-DWORD immediate printer), the function begins with `vtable[0](builder, 89)` (begin instruction kind 89), calls `vtable[3760]` for sync type, `vtable[3768]` for begin operand list, `sub_9DB7E0` for predicate guard, then loops 16 times calling `vtable[272]` (create integer operand) followed by `vtable[16]` (emit operand) with kind IDs 55 through 70 — one per DWORD.

In `sub_1810D20` (comparison-mode printer), the function first reads the modifier word from the operand array at `instruction+84`, switches on `(modifier >> 4) & 0xF`, calls `vtable[3528]`/`vtable[3536]` to configure comparison mode and variant, then emits 2–3 operands via the standard `sub_9D12F0` + `vtable[16]` sequence.

### Builder/Visitor Vtable

The builder object at `*(a1 + 24)` exposes a vtable spanning 4,160+ bytes (~520 method slots at 8 bytes each). The complete set of identified methods:

| Offset | Method | Category |
|--------|--------|----------|
| +0 | `begin_instruction(kind_id)` | Framing |
| +8 | `get_current_instruction()` | Accessor |
| +16 | `emit_operand(kind_id, operand_buf...)` | Core emission |
| +24 | `post_process_operand()` | After-emit hook |
| +112 | `get_register_size_32()` | Register geometry |
| +120 | `get_register_size_64()` | Register geometry |
| +128 | `create_register_operand()` | Operand factory |
| +152 | `create_memory_operand()` | Operand factory |
| +192 | `create_special_operand()` | Operand factory |
| +208 | `create_literal_operand()` | Operand factory |
| +272 | `create_integer_operand(value)` | Operand factory |
| +304 | `create_register_ref_operand()` | Operand factory |
| +368 | `set_address_space()` | Memory qualifier |
| +936 | `begin_predicate_guard()` | Predicate block |
| +944 | `end_predicate_guard()` | Predicate block |
| +984 | `set_predicate_mode()` | Predicate negate/true |
| +1000 | `emit_modifier()` | Generic modifier |
| +1056 | `set_offset_mode()` | Address offset |
| +1128 | `emit_width_qualifier()` | `.B32`, `.B64` |
| +1392 | `set_comparison_flag()` | Comparison type |
| +1760 | `set_rounding_mode()` | `.RN`, `.RZ`, `.RM`, `.RP` |
| +1936 | `begin_sync_block()` | Sync scope |
| +1944 | `end_sync_block()` | Sync scope |
| +2016 | `set_sync_width()` | Sync width |
| +2024 | `set_sync_depth()` | Sync depth |
| +2584 | `set_uniform_flag()` | `.U` modifier |
| +2960 | `set_address_mode()` | Address mode |
| +2992 | `set_cache_level_a()` | Cache hint (L1) |
| +3000 | `set_cache_level_b()` | Cache hint (L2) |
| +3096 | `set_comparison_type()` | Second comparison slot |
| +3128 | `set_source_type_a()` | Source type |
| +3136 | `set_source_type_b()` | Source type |
| +3144 | `set_interlock_mode()` | Memory ordering |
| +3152 | `begin_comparison_block()` | Comparison section |
| +3160 | `set_comparison_width()` | Comparison width |
| +3520 | `set_data_width()` | Operand width |
| +3528 | `set_comparison_mode()` | Comparison config |
| +3536 | `set_comparison_variant()` | Comparison variant |
| +3560 | `set_conversion_type()` | Conversion modifier |
| +3576 | `begin_conversion()` | Conversion block |
| +3760 | `set_sync_type()` | Synchronization type |
| +3768 | `begin_operand_list()` | Operand section |
| +3776 | `emit_rounding_decoration()` | Rounding modifier |
| +3824 | `emit_texture_header()` | Texture header index |
| +3952 | `emit_saturation_flag()` | `.SAT` |
| +3960 | `emit_ftz_flag()` | `.FTZ` |
| +3968 | `emit_negate_flag()` | `.NEG` |
| +4072 | `emit_cache_operation()` | Cache operation hint |
| +4080 | `emit_eviction_hint()` | Eviction priority |
| +4160 | `end_instruction()` | Framing |

Different concrete visitor implementations produce different output formats. The vtable design means adding a new output format (e.g., JSON, binary verification) requires only a new visitor class with no changes to any of the ~123 printer functions.

### Encoding Template Builders (Layer A)

~75 functions at `0x17F8000`--`0x180FFFF` build per-opcode instruction format descriptors that define the expected operand signature. Each function:

1. Sets the SASS opcode ID: `*(a2+12) = opcode_number`
2. Loads a 128-bit format descriptor: `*(a1+8) = xmmword_23Fxxxx` (from rodata)
3. Fills up to 10 operand slots at `a1+24`..`a1+120` with type codes, register class IDs, and modifier flags
4. Writes expected-value constraints at `a1+64`..`a1+160` (-1 = any)
5. Writes type constraint modifiers at `a1+104`..`a1+200`

From `sub_17F8210` (opcode 274):

```c
*(a2+12) = 274;                                          // SASS opcode ID
*(a1+8)  = _mm_loadu_si128(&xmmword_23F21B0);           // 128-bit descriptor
*(a1+24) = 10;   // operand 0: predicate register type
*(a1+64) = -1;   // operand 0: any value accepted
*(a1+104) = 0;   // operand 0: no modifier constraint
*(a1+28) = 17;   // operand 1: specific register class
*(a1+68) = -1;   // operand 1: any value
*(a1+108) = 3;   // operand 1: modifier constraint 3
// ... remaining operands bulk-copied via SSE from xmmword_23F1C60 table
```

The 128-bit descriptors at `xmmword_23F1xxx`--`23F2xxx` encode canonical operand layouts. The bulk SSE copies (`_mm_load_si128`/`_mm_loadu_si128`) fill 4 operand slots per iteration, making the template builders compact despite handling up to 10 operand positions.

### Format-Class Printers (Layer C)

The instruction's format class at `instruction+76` determines which printer handles it. The dispatch computes `index = format_class - 11`, then looks up `dword_23B39E0[index]` for the encoding strategy. Full table contents (read from VA `0x23B39E0`, 10 DWORDs):

| Index | Format class | `dword_23B39E0[i]` | Strategy |
|------:|:------------:|:-------------------:|----------|
| 0 | 11 | 1 | Wide (9-bit register fields, 8 sequential operands) |
| 1 | 12 | 3 | Extended (extra modifier bits) |
| 2 | 13 | 0 | Default (standard register fields) |
| 3 | 14 | 0 | Default |
| 4 | 15 | 0 | Default |
| 5 | 16 | 0 | Default |
| 6 | 17 | 0 | Default |
| 7 | 18 | 0 | Default |
| 8 | 19 | 0 | Default |
| 9 | 20 | 2 | Pair (2x register fields per operand) |

Only three non-zero strategies exist: Wide (1) for format class 11, Pair (2) for class 20, and Extended (3) for class 12. The remaining seven classes use strategy 0 (Default).

The companion `word_23B3A58[4]` table maps the strategy value to a builder `kind_id` used by the texture/surface printer (VA `0x23B3A58`, 4 WORDs):

| Index (strategy) | `word_23B3A58[i]` | Builder kind ID |
|------------------:|:-----------------:|-----------------|
| 0 | 0x00C2 (194) | Default builder |
| 1 | 0x00D5 (213) | Wide builder |
| 2 | 0x0083 (131) | Pair builder |
| 3 | 0x0087 (135) | Extended builder |

Printer functions for each format class:

| Function | Size | Format class | Evidence |
|----------|------|-------------|----------|
| `sub_1810D20` | 8.8 KB | Comparison-mode | Switches on `(modifier >> 4) & 0xF`: case 4 emits comparison with two variants, case 6 emits single-variant. Calls `vtable[3528]`/`vtable[3536]` for comparison config |
| `sub_18111F0` | 11.6 KB | Wide-operand | 8 sequential `sub_9D12F0` calls with indices 0–7 |
| `sub_1811E20` | 11.6 KB | Wide + special | Both `sub_9D12F0` and `sub_9CF740` calls |
| `sub_1812890` | 10.5 KB | Register + constant | `sub_9CF8A0` for constant folding |
| `sub_1812F60` | 15.3 KB | 16-DWORD immediate | `sub_7E4CF0` iterator, 16x `vtable[272]` + `vtable[16]` with kind IDs 55–70 |
| `sub_18141C0` | 6.5 KB | Per-operand comparison | Dispatch entry from `sub_1820000` |
| `sub_1814660` | 7.1 KB | Load/store | `sub_C49400` + `sub_9CEB50` for address space |
| `sub_1814B10` | 17.6 KB | Load/store + predicated | `sub_C49400`, `sub_91E860`, `sub_91C840`, `sub_9CEB50` |
| `sub_1815810` | 12.7 KB | Wide variant | Similar to `sub_1811E20` |
| `sub_1816000` | 13.1 KB | Data-type qualified | `sub_9E9910` for data type emission |
| `sub_18167F0` | 11.8 KB | Memory-access | `sub_9E7B00` for address space qualifier, `sub_A3B930` for operand modifier |
| `sub_1816FC0` | 6.4 KB | Modifier-heavy | Checks bits 6, 7, 14 of operand word for negate/absolute modifiers |

### Texture/Surface Printer (Layer D)

The texture/surface printer `sub_18189C0` is the largest at 45.2 KB. It handles the complete texture and surface instruction families:

```text
sub_18189C0 (45.2 KB, 1361 lines)
  ├─ Read opcode at +72, mask to canonical form
  ├─ Giant switch on opcodes:
  │    18 (FADD/FMUL?), 119 (MUFU?), 186 (TEX),
  │    211 (TLD), 283 (SULD), 315 (SUST)
  ├─ Check operand modifier bits for predication/negation
  ├─ dword_23B39E0[format_class-11] → subtype (values 0-4)
  ├─ word_23B3A58[subtype] → builder kind ID
  ├─ Emit predicate: vtable[89], begin_operand_list
  ├─ sub_9DB7E0 → predicate guard
  ├─ For each operand: sub_9D12F0 → builder->emit_operand
  ├─ If format > 9: sub_1817C50 (12.8KB) → texture header index
  │    └─ Linearizes from 2D bit fields: bits[0:8] x bits[9:17] → index 0-52
  ├─ Emit cache/eviction: vtable[4080], vtable[4072]
  ├─ Emit saturation/ftz: vtable[3952], vtable[3960], vtable[3968]
  └─ Return 1 on success
```

The multi-operand printer `sub_181B370` (27.8 KB) handles instructions with many operand variants (VOTE at opcode `0x7A`, multi-op at `0x138`), emitting up to 12 sequential operands through `sub_9CEF90` (extended operand encoder) and `sub_9CF740` (immediate encoder).

### ISA-Override Detection (Layer E)

`sub_181E1D0` (7.3 KB) is a post-processing fixup dispatcher called from `sub_AA9330`. It performs ISA-target-aware fixups by comparing vtable method pointers against known discriminator functions:

```c
// If the current ISA target has the DEFAULT implementation:
if (vtable[111] == sub_1810B90)   // default comparison handler
    apply_default_fixup();
// Otherwise the target has OVERRIDDEN the method:
else
    apply_specialized_fixup();    // e.g., sub_1BCBB90 for arch-specific
```

This mechanism supports 45 opcodes (`0x12`, `0x16`, `0x24`, ..., `0x141`) and dispatches to architecture-specific post-processors (`sub_1BCBB90`, `sub_1BCC2D0`, `sub_BCCF80`, `sub_1BCF120`) or re-emits modifiers via `sub_9E9910`/`sub_9E9A70`.

The discriminator functions at `0x1810700`--`0x1810BFF` (~15 tiny functions) serve as sentinel values: `sub_1810720`, `sub_1810750`, `sub_18108A0`, `sub_18108D0`, `sub_1810B90`. Their identity (which function pointer is stored) determines which specialization path the fixup dispatcher takes.

### Instruction Object Layout

The binary instruction object (`a2`) used by all SASS printers:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 8 | Context/vtable pointer |
| +8 | 8 | ISA context pointer (register file, instruction info table) |
| +24 | 8 | Builder/visitor object pointer |
| +32 | 8 | Operand metadata pointer |
| +40 | 1 | Half-precision flag |
| +48 | 8 | Operand modifier context |
| +72 | 4 | Opcode (bits 12–13 are variant flags, masked via `&0xCFFF`) |
| +76 | 4 | Format class (subtract 11 for `dword_23B39E0[]` indexing) |
| +80 | 4 | Operand count |
| +84+ | 8*N | Operand array (N operands, 8 bytes each) |

Each 8-byte operand slot encodes:

| Bits | Word | Field |
|------|------|-------|
| 28–30 | 0 | Operand type tag: 1=register, 4=address, 5=constant buffer, 7=special |
| 0–23 | 0 | Register/constant index |
| 24–27 | 0 | Modifier flags |
| 0 | 1 | Negate flag (mirrored from word 1 bit 31) |
| 1 | 1 | Absolute value flag (mirrored from word 1 bit 30) |
| 20 | 1 | Constant pool flag (`0x100000`) |
| 29 | 1 | Sign extension / signed-folding flag (`0x20000000`) |
| 30 | 1 | `.ABS` modifier (`0x40000000`) — absolute value of source |
| 31 | 1 | `.NEG` modifier (`0x80000000`) — arithmetic negation of source |

The polarity is "set means modifier applied":

- **Source-operand modifiers** (in `sub_9D12F0`):
  - word-1 bit-31 test is `v17 < 0` — sign-bit set means negate.
  - bit-30 is `v17 & 0x40000000` — set means absolute value.
- **Branch predicate guard negation** is independent of the two source-operand modifiers above. It lives at **bit 24 (`0x1000000`)** of word1 of the predicate-register operand in the guard pair (verified in `sub_137E3A0` line 50, which reads `instr[2*last_idx + 22] & 0x1000000`).
- **Textual printer `sub_70B780`** does **not** consult this binary bit — it reads the `'!'` prefix from the operand-descriptor name string at `+2120`.

> The two surfaces stay in sync because the passes that negate a guard set both, but downstream code that mutates only one must rebuild the other (see `passes/predication.md`).

### Global Lookup Tables

| Table | Size | Index | Purpose |
|-------|------|-------|---------|
| `dword_23B39E0[10]` | 40 B | `format_class - 11` | Format class to encoding strategy: `{1,3,0,0,0,0,0,0,0,2}` |
| `word_23B3A58[4]` | 8 B | Strategy from above | Strategy to builder kind ID: `{194,213,131,135}` |
| `dword_23B3A20[14]` | 56 B | `register_class - 3` | Register class to comparison type ID |
| `dword_23B3980[7]` | 28 B | `width_field - 1` | Encoded width to builder width value |
| `xmmword_23F1xxx`--`23F2xxx` | ~16 B each | Per-opcode | 128-bit operand layout descriptor templates |

### SASS Renderer Function Map

| Address | Size | Callers | Identity | Confidence |
|---------|------|---------|----------|------------|
| `sub_17F8210` | ~1.3 KB | 0 (vtable) | Encoding template builder (opcode 274) | 95% |
| `sub_1810D20` | 8.8 KB | 0 (vtable) | Comparison-mode format-class printer | 90% |
| `sub_18111F0` | 11.6 KB | 0 (vtable) | Wide-operand format-class printer | 85% |
| `sub_1811E20` | 11.6 KB | 0 (vtable) | Wide-operand + special encoding printer | 85% |
| `sub_1812890` | 10.5 KB | 0 (vtable) | Register + constant operand printer | 85% |
| `sub_1812F60` | 15.3 KB | 0 (vtable) | 16-DWORD immediate printer | 90% |
| `sub_18141C0` | 6.5 KB | 0 (vtable) | Per-operand comparison printer | 85% |
| `sub_1814660` | 7.1 KB | 0 (vtable) | Load/store with address space printer | 85% |
| `sub_1814B10` | 17.6 KB | 0 (vtable) | Load/store + predication printer | 85% |
| `sub_1815810` | 12.7 KB | 0 (vtable) | Wide-operand variant printer | 80% |
| `sub_1816000` | 13.1 KB | 0 (vtable) | Data-type qualified printer | 85% |
| `sub_18167F0` | 11.8 KB | 0 (vtable) | Memory-access instruction printer | 85% |
| `sub_1816FC0` | 6.4 KB | 0 (vtable) | Modifier-heavy instruction printer | 85% |
| `sub_1817C50` | 12.8 KB | ~1 | Texture header index encoder | 90% |
| `sub_18189C0` | 45.2 KB | 0 (vtable) | Texture/surface instruction printer | 92% |
| `sub_181B370` | 27.8 KB | 0 (vtable) | Multi-operand instruction printer | 88% |
| `sub_181CF60` | 14.0 KB | 0 (vtable) | Predicated instruction printer | 85% |
| `sub_181D9B0` | 12.6 KB | 0 (vtable) | Load/store variant printer | 80% |
| `sub_181E1D0` | 7.3 KB | ~1 | ISA-override fixup dispatcher | 90% |
| `sub_181E630` | 14.7 KB | ~1 | Comparison instruction post-processor | 88% |
| `sub_181F000` | 7.6 KB | ~1 | Data-type specialized printer | 75% |
| `sub_181F4F0` | 17.3 KB | ~1 | Multi-variant data-type printer | 80% |

## Compile-Time FP Folding

The literal floating-point immediates that appear in the rendered SASS — and any `.f16`/`.bf16`/`.tf32`/`.f32`/`.f64` constant the optimizer folds during code generation — are computed without any software-float library. ptxas links **no** Berkeley-SoftFloat layer, **no** FP128/extF80, and **no** 128-bit-integer software routines:

- **No SoftFloat reciprocal tables** — the canonical `approxRecip_1k0s` / `approxRecipSqrt_1k0s` are absent from the entire 37.74 MB image.
- **No `roundPackToF64`/`F32` leaves.**
- **Dynamic math imports are exactly the glibc libm set** — `sqrt`, `pow`, `floor`, `ceil`, `cos`, `sin`, `log` `@GLIBC_2.2.5`, notably with no `fma` and no `fesetround`/`fegetround` import.
- **The lone `__float128` string** in the binary lives in the C++ Itanium demangler's type table, not on any folding path.

Instead, ptxas folds FP immediates with the **host x86 SSE2 FPU** (`_mm_*_pd`/`_ss`) plus glibc libm, applying manual round-bit twiddling for the directed-rounding and narrowing cases. The fold cluster:

| Address | Role |
|---|---|
| `sub_926a30` | constant-fold master dispatch (multi-thousand-line opcode switch) |
| `sub_9203a0` | binary-op folder — native `__m128d` add/sub/mul/div/min/max `.f32`/`.f64` |
| `sub_921820` | unary-op folder via libm — `0xC0`→`sqrt()`, `0x3B`→`pow(2.0,x)` (ex2), `0x6A`→`log()`/ln2 (lg2), rcp/round/cvt |
| `sub_91b730` | typed-immediate decoder → host `double` — f16 via `pow(2.0, e−15)`, bf16 `<< 16`, flushing subnormals to a signed zero |
| `sub_91ba60` / `sub_91cdd0` | folded-result writer / f32 const-pool interner |

The libm kernels are reached through PLT thunks `sub_2a12cb0` (`sqrt`) and `sub_2a12d50` (`pow`). Because the engine narrows through `double` and re-rounds explicitly, half- and bfloat-precision immediates round-trip exactly while `.f32`/`.f64` directed-rounding modes are honoured by the post-fold bit fix-up rather than by the host rounding-mode register.

## CLI Integration

### `--verbose` / `-v`

Enables printing of code generation statistics after compilation. The statistics printers at `sub_ABBA50`--`sub_ABEB50` (8 SM-variant clones, 7,603 bytes each) emit post-scheduling metrics in `"# [...] "` comment format.

#### Resource-Usage Report

The familiar `ptxas info :` resource report is built by the single unified formatter `sub_463710`.

- **Line assembly** — each line is assembled into a stringstream buffer (`sub_4287D0` create, `sub_428F30` sprintf-append, `sub_4289F0` finalize), then flushed through the message-emit core `sub_42F590`, which prepends the `ptxas info :` prefix.
- **Report-line descriptors** — live in a `.rodata` array at `0x29FC000` with a 16-byte stride `[severity dword][pad][format-ptr]`.
  - Severities: `1=plain, 2=info, 3=warning, 5=error, 6=fatal`.
  - `sub_42F590` writes the matching `info`/`warning`/`error`/`fatal` word plus the `@I@`/`@O@`/`@W@`/`@E@` channel tags.
- **Gating** — the report is gated at the call site (`sub_446240`, near `0x447b8a`) by `verboseMode && !forceText`.

The report has two-block structure: a **global** line (`gmem`, then `cmem[N]` over the const-bank tags) followed by a **per-entry** line (`Compiling entry function`, `Function properties`, register/barrier/stack/smem/cmem/lmem and texture/surface/sampler counts), plus a separate `Compile time = %.3f ms` line. Field order:

| Field | Format string |
|---|---|
| gmem | `%lld bytes gmem` |
| cmem (global) | `, %lld bytes cmem[%d]` |
| compiling entry | `Compiling entry function '%s' for '%s'` |
| function properties | `Function properties for %s\n    %d bytes stack frame, %d bytes spill stores, %d bytes spill loads` |
| registers | `Used %d registers` |
| barriers | `, used %d barriers` |
| stack | `, %d bytes cumulative stack size` |
| smem | `, %lld bytes smem` |
| cmem | `, %lld bytes cmem[%d]` |
| lmem | `, %lld bytes lmem` |
| textures | `, %d textures` |
| surfaces | `, %d surfaces` |
| samplers | `, %d samplers` |
| compile time | `Compile time = %.3f ms` |

The `, used %d barriers` and `Compile time = %.3f ms` lines are newer than the field set older ptxas builds emitted — treat both as standard modern fields. The spill, lmem, and stack-limit diagnostics are **separate warning descriptors** (`dword_29FD320`/`dword_29FD330`, severity 3) gated by the `warn-on-spills` knob, not part of the info report itself.

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
| Small | 500–1,000 B | 191 | Standard 3–4 operand (copysign: 794 B) |
| Medium | 1,000–2,000 B | 319 | Instructions with modifiers (bfind: 1,130 B) |
| Large | 2,000–4,000 B | 36 | Arch-conditional paths (membar: 2,788 B) |
| Very large | 4,000–6,000 B | 20 | Complex multi-form (tex.grad: 5,636 B) |
| Monster | 6,000–10,000 B | 2 | WMMA matrix loads (wmma.load.b: 9,757 B) |

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

The remaining 473 opcode variants (arithmetic, logic, load/store, control flow, MMA shape+type+layout combinations) are dispatched through composite variant IDs at `a1+816`.

## SASS Printer Key Functions

| Address | Size | Callers | Identity |
|---------|------|---------|----------|
| `sub_5D4190` | 12.9 KB | 1 | PTX instruction text dispatch + intrinsic registration |
| `sub_5D1660` | 46 KB | 1 | Intrinsic library registration (608 entries) |
| `sub_5FF700` | 354 KB | — | Builtin function declaration emitter (prototype generator) |
| `sub_4DA340` | 61 B | 1,080 | Builtin declaration lookup helper |
| `sub_719D00` | 50 KB | — | SASS text formatter (self-check output builder) |
| `sub_720F00` | 64 KB | — | Flex lexer for SASS text parsing (self-check input) |
| `sub_9D12F0` | 1.4 KB | 289 | Operand encoder (64-byte struct per operand) |
| `sub_9DB7E0` | 662 B | 19 | Predicate guard printer |
| `sub_91C840` | 347 B | 232 | Register class discriminator |
| `sub_9CEB50` | 185 B | 57 | Address space qualifier resolver |
| `sub_91E860` | 31 B | 214 | Data size accessor |
| `sub_18189C0` | 45.2 KB | — | Texture/surface instruction printer (largest SASS printer) |
| `sub_181B370` | 27.8 KB | — | Multi-operand instruction printer |
| `sub_1817C50` | 12.8 KB | — | Texture header index encoder |

## Instruction Data Flow

```text
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

- [Code Generation Overview](./overview.md) — pipeline context and subsystem map
- [SASS Instruction Encoding](./encoding.md) — binary encoding format that this subsystem renders
- [Mercury Encoder Pipeline](./mercury.md) — source of instructions for text generation
- [Capsule Mercury & Finalization](./capmerc.md) — `--self-check` and `--out-sass` integration
- [CLI Options](../config/cli-options.md) — `--verbose`, `--forcetext`, `--out-sass` flags
- [Knobs System](../config/knobs.md) — DUMPIR knob triggering phase 129/130
- [Phase Manager](../passes/phase-manager.md) — phase 129/130 registration and execution
- [SASS printer & `-v` report function map](../reference/data/sass-printer-functions.md) — full printer/report/DWARF function table
