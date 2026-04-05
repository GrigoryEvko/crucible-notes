# PTX Parser (Flex + Bison)

The ptxas front-end parses PTX assembly text into internal IR using a classic two-stage architecture: a Flex-generated DFA scanner (lexer) and a Bison-generated LALR(1) shift-reduce parser. Unlike most compiler front-ends, the parser does **not** construct an AST. Instead, Bison reduction actions directly build IR nodes, populate the instruction table, and emit validation calls -- the parse tree is consumed inline and never materialized as a data structure. A separate macro preprocessor handles `.MACRO`, `.ELSE`/`.ELIF`/`.ENDIF`, and `.INCLUDE` directives at the character level before tokens reach the Flex DFA. The instruction table builder (`sub_46E000`, 93 KB) registers all PTX opcodes with their legal type combinations during parser initialization, and an instruction lookup subsystem classifies operands into 12 categories at parse time.

| | |
|---|---|
| **Flex scanner** | `sub_720F00` (15.8 KB, 64 KB with inlined helpers) |
| **DFA table** | `off_203C020` (transition/accept array) |
| **Scanner rules** | ~552 Flex rules, 162 token types (codes 258--422) |
| **Scanner prefix** | `ptx` (all Flex symbols: `ptxlex`, `ptxensure_buffer_stack`, etc.) |
| **Bison parser** | `sub_4CE6B0` (48 KB, spans `0x4CE6B0`--`0x4DA337`) |
| **Grammar size** | ~512 productions, 443 reduction cases |
| **LALR tables** | `word_1D146A0` (yydefact), `word_1D121A0` (yycheck), `word_1D13360` (yypact), `word_1D150C0` (yypgoto), `byte_1D15960` (yyr2) |
| **Instruction table builder** | `sub_46E000` (93 KB, 1,141 calls to `sub_46BED0`) |
| **Instruction lookup** | `sub_46C690` (entry), `sub_46C6E0` (6.4 KB descriptor matcher) |
| **Macro preprocessor** | `sub_71F630` (14 KB dispatcher), `sub_71E2B0` (32 KB conditional handler) |
| **Parser state object** | 1,128 bytes (+ 2,528-byte lexer state via pointer at +1096) |
| **Error handler** | `sub_42FBA0` (2,350 callers, central diagnostics) |
| **Parser init** | `sub_451730` (14 KB, symbol table + special registers + opcode table) |

## Architecture

```
PTX source text
     │
     ▼
┌─────────────────────────────────────────────────────────┐
│  MACRO PREPROCESSOR (character-level, 0x71B000-0x720000)│
│  sub_71F630  dispatch: .MACRO / .ELSE / .INCLUDE        │
│  sub_71E2B0  conditional: .ELSE / .ELIF / .ENDIF (32KB) │
│  sub_71DCA0  macro definition handler                   │
│  sub_71C310  .INCLUDE file handler                      │
└────────────────────┬────────────────────────────────────┘
                     │ preprocessed character stream
                     ▼
┌─────────────────────────────────────────────────────────┐
│  FLEX DFA SCANNER  sub_720F00 (15.8KB, 552 rules)       │
│  off_203C020       DFA transition table                  │
│  Token codes:      258-422 (162 types)                   │
│  Helper:           sub_720410 (yy_get_next_buffer)       │
│                    sub_720630 (yy_get_previous_state)     │
│                    sub_720BA0 (yy_scan_string)            │
└────────────────────┬────────────────────────────────────┘
                     │ token stream (code + attribute)
                     ▼
┌─────────────────────────────────────────────────────────┐
│  BISON LALR(1) PARSER  sub_4CE6B0 (48KB, 512 prods)     │
│  5 LALR tables at 0x1D12xxx-0x1D15xxx                    │
│  443 reduction actions → direct IR construction           │
│  NO AST: reductions emit IR nodes inline                  │
└────────────────────┬────────────────────────────────────┘
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
  INSTRUCTION TABLE         SEMANTIC VALIDATORS
  sub_46E000 (93KB)         sub_4B2F20 (52KB, general)
  sub_46BED0 (per-opcode)   sub_4C5FB0 (28KB, operands)
  sub_46C690 (lookup)       sub_4C2FD0 (12KB, WMMA/MMA)
  sub_46C6E0 (6.4KB match)  sub_4ABFD0 (11KB, async copy)
                            sub_4A73C0 (10KB, tensormap)
                            + 20 more validators
```

## Flex DFA Scanner -- `sub_720F00`

The scanner is a standard Flex-generated DFA with the `ptx` prefix (all exported symbols use `ptx` instead of `yy`: `ptxlex`, `ptxensure_buffer_stack`, `ptx_create_buffer`, etc.). At 15.8 KB of core logic (64 KB including inlined buffer management), it is the largest single function in the lexer region. The DFA transition table lives at `off_203C020` and is indexed by `*(DWORD*)(state + 76)` (the current start condition). The main loop structure follows the textbook Flex pattern:

```c
// DFA transition core (reconstructed from sub_720F00)
while (1) {
    v10 = (DWORD*)(table_base + 8 * state);   // table[state]
    if (current_char == *v10) {                 // character match
        state = table_base + 8 * v10[1];       // goto next state
        action = *(unsigned int*)(state - 4);   // accept action (or 0)
    }
    if (action != 0) break;                     // matched a rule
}
// Giant switch on action number (0..~550)
switch (action) { ... }
```

The scanner returns integer token codes to the Bison parser. The value 550 is `YY_NULL` (end-of-input sentinel). Token attributes are communicated through the lexer state object, which the parser state carries as a pointer at offset `+1096`. The scanner receives this pointer as its `a3` argument and dereferences it (e.g., `*(_QWORD *)(a3 + 1096)`) to reach the 2,528-byte lexer state.

### Token Categories

The 552 Flex rules map PTX lexemes to 162 distinct token types. Bison terminal codes range from 258 to 422. The scanner switch cases reveal the following category structure:

| Switch cases | Token code | Category | Examples / attributes |
|---|---|---|---|
| 2 | 364 | Semicolons / newlines | Statement terminator |
| 5--7 | 340, 341, 344 | Keywords | PTX keywords |
| 63--65 | 302 | Register names | Attribute: -1, `chr-48`, `chr-38` (register numbering) |
| 74--91 | 320 | Data types | Values 1--18: `.b8` through `.f64` (18 type qualifiers) |
| 92--94 | 322 | Comparison types | Values 9, 7, 11 |
| 95--99 | 323 | Rounding modes | Values 24--29: `.rn`, `.rz`, `.rm`, `.rp`, etc. |
| 1 | (internal) | `#include` | Strips whitespace, copies filename |
| 3 | (dispatch) | Preprocessor directive | Calls `sub_71F630` |
| 4 | 339 | `#pragma` | Strips whitespace |

Line and column tracking uses fields at `*(state+48)` (line number) and `*(state+52)` (column), incremented on each newline character.

### Buffer Management

The scanner uses the standard Flex buffer stack for nested input sources (includes, macros, inline strings). Key buffer management functions:

| Address | Size | Identity | Purpose |
|---|---|---|---|
| `sub_720190` | 2.0 KB | `ptxensure_buffer_stack` | Grows buffer stack via realloc |
| `sub_7202E0` | 1.3 KB | `ptx_create_buffer` | Creates `YY_BUFFER_STATE` from FILE* |
| `sub_720410` | 3.3 KB | `yy_get_next_buffer` | Refills character buffer, handles EOF |
| `sub_720630` | 9.7 KB | `yy_get_previous_state` | Restores DFA state, SIMD-optimized memmove |
| `sub_720BA0` | 4.3 KB | `ptx_scan_string` | Scans inline string into buffer |
| `sub_724CC0` | 4.9 KB | `ptx_scan_bytes` | Macro expansion buffer allocation |
| `sub_725070` | 2.7 KB | `ptx_scan_buffer` | Buffer creation with error recovery |

Notable: `sub_720630` contains SSE2-optimized `memmove` using `__m128i` aligned 16-byte copies for buffer compaction -- a Flex optimization for large input buffers. The `ptx_scan_bytes` function (`sub_724CC0`) is called from the Bison parser actions (3 call sites in `sub_4CCF30`) to handle inline macro expansion during parsing.

Error strings in the buffer system:

- `"out of dynamic memory in ptxensure_buffer_stack()"`
- `"out of dynamic memory in ptx_create_buffer()"`
- `"out of dynamic memory in yy_get_next_buffer()"`
- `"out of dynamic memory in ptx_scan_bytes()"`
- `"bad buffer in ptx_scan_bytes()"`
- `"out of dynamic memory in ptx_scan_buffer()"`
- `"fatal flex scanner internal error--no action found"`
- `"fatal flex scanner internal error--end of buffer missed"`
- `"unexpected EOF while scanning"`

## Macro Preprocessor

Before tokens reach the Flex DFA, a character-level macro preprocessor handles `.MACRO`/`.ENDM`, `.ELSE`/`.ELIF`/`.ENDIF`, and `.INCLUDE` directives. The preprocessor lives at `0x71B000`--`0x720000` (~20 KB) and operates on raw character streams, not tokens. This design is identical to C's preprocessor running before the lexer.

### Preprocessor Dispatch -- `sub_71F630`

The top-level dispatcher (14 KB) is called from the Flex scanner's case 3 (directive detection). It examines the directive name and routes to the appropriate handler:

| Directive | Handler | Size | Description |
|---|---|---|---|
| `.MACRO` | `sub_71DCA0` | 8.4 KB | Macro definition: records body text, handles nesting |
| `.ELSE` / `.ELIF` | `sub_71E2B0` | 32 KB | Conditional code: skips blocks, handles nested conditionals |
| `.ENDIF` | `sub_71E2B0` | (shared) | End of conditional block |
| `.INCLUDE` | `sub_71C310` | 8.3 KB | File inclusion: pushes new input source onto lexer stack |

The dispatcher uses `strstr` for substring matching on directive names and returns token codes (e.g., 364 for end-of-directive).

### Conditional Handler -- `sub_71E2B0`

At 32 KB, this is the largest preprocessor function. It handles `.ELSE`, `.ELIF`, and `.ENDIF` by scanning ahead through the input character stream, counting nesting levels, and skipping entire blocks of PTX text when conditions are false. It calls `sub_4287D0` (the token reader) to evaluate conditional expressions and `sub_428C40` (string compare) for keyword matching. Two nearly-duplicate code blocks handle `.ELSE` and `.ELIF` paths with identical scanning logic but different branch conditions.

### Macro Definition -- `sub_71DCA0`

Handles `.MACRO` directives by recording the macro body text. The function is recursive to support nested `.MACRO` definitions. It delegates to `sub_71D710` (macro body scanner, 7.5 KB) and `sub_71D1B0` (macro argument scanner, 6.8 KB). The argument scanner uses `strlen` + `strncmp` for keyword matching against a delimiter string parameter.

### Include Handler -- `sub_71C310`

Processes `.INCLUDE` by pushing a new file onto the lexer's input stack. The function is recursive (calls itself 4 times) for nested includes. It manages the include-stack pointers at offsets +2128, +2136, +2160, and +2168 of the **lexer state object** (the 2,528-byte struct pointed to by `parser+1096`), and uses the "pushback character" register at offset +2441 of the same lexer state. String reference: `"ptxset_lineno called with no buffer"`.

### Error Handling

Macro errors are reported through `sub_71BF60` (fatal macro abort) which calls `sub_71BF30` to print `"out of dynamic memory..."` messages, and `sub_71C140` (format error) which calls `sub_42CA60` (error output). Nesting depth is checked by `sub_724CC0` which prints `"macro nesting too deep!"` on overflow.

## Bison LALR(1) Parser -- `sub_4CE6B0`

The parser is a standard Bison-generated LALR(1) shift-reduce parser spanning 48 KB (addresses `0x4CE6B0`--`0x4DA337`). It contains ~512 grammar productions with 443 reduction cases. The function calls `ptxlex` (`sub_720F00`) to obtain tokens and uses five LALR tables for state transitions:

| Table | Address | Bison name | Purpose |
|---|---|---|---|
| `word_1D146A0` | `0x1D146A0` | `yydefact` | Default reduction rule for each state |
| `word_1D121A0` | `0x1D121A0` | `yycheck` | Valid lookahead verification |
| `word_1D13360` | `0x1D13360` | `yypact` | Parser action table (shift/reduce) |
| `word_1D150C0` | `0x1D150C0` | `yypgoto` | Goto table for nonterminals |
| `byte_1D15960` | `0x1D15960` | `yyr2` | Right-hand-side length for each rule |

### Direct IR Construction (No AST)

The critical architectural decision: Bison reduction actions directly construct IR nodes rather than building an intermediate AST. When a grammar rule is reduced, the semantic action immediately:

1. Allocates IR nodes via the pool allocator (`sub_424070`)
2. Populates instruction fields from token attributes
3. Calls instruction validators for semantic checking
4. Links nodes into the instruction stream
5. Registers symbols in the symbol table (via `sub_426150`, the hash map)

This means the parser is a single-pass translator from PTX text to IR. The trade-off is clear: no AST means no multi-pass source-level analysis, but it eliminates an entire allocation and traversal phase. For an assembler (as opposed to a high-level language compiler), this is the right choice -- PTX is already a linearized instruction stream with no complex scoping or overload resolution that would benefit from an AST.

### Reduction Actions -- Semantic Processing

The 443 reduction cases in the parser body handle PTX constructs from simple register declarations to complex matrix instruction specifications. Diagnostic strings found in the parser tail (`0x4D5000`--`0x4DA337`) reveal the kinds of semantic checks performed during reduction:

**Directive validation:**
- `"Defining labels in .section"`
- `"dwarf data"` -- DWARF section processing
- `"reqntid"` / `".reqntid directive"` -- required thread count
- `".minnctapersm directive"` -- min CTAs per SM
- `".maxnctapersm"` / `".maxnctapersm directive"` -- max CTAs per SM (deprecated)
- `".maxntid and .reqntid cannot both be specified"`
- `".maxnctapersm directive deprecated..."`
- `".minnctapersm is ignored..."`

**Type and operand validation:**
- `"Vector Type not specified properly"`
- `".f16x2 packed data-type"` -- half-precision packed type
- `"matrix shape"` -- matrix instruction dimensions
- `".scale_vectorsize"` -- vector scaling modifier
- `"too many layout specifiers"`

**Resource limits:**
- `"Kernel parameter size larger than 4352 bytes"`

**Architecture gating:**
- `"sm_50"`, `"sm_20"`, `"sm_53"` -- target architecture checks via `sub_485520(ctx, sm_number)`
- PTX version checks via `sub_485570(ctx, major, minor)`

**Expression handling:**
- `"%s+%llu"` / `"%s-%s"` -- label arithmetic in address expressions
- `"Negative numbers in dwarf section"` -- DWARF data validation

**Symbol resolution:**
- `"unrecognized symbol"` -- lexer/symbol table failure
- `"syntax error"` -- generic parse error
- `".extern"` -- external declarations
- `".noreturn directive"` -- function attributes
- `"texmode_unified"` / `"texmode_raw"` -- texture mode selection
- `"cache eviction priority"` / `".level::eviction_priority"` -- cache policy

### Error Recovery

Parse errors trigger `sub_42FBA0` with `"syntax error"` as the message. The central diagnostic emitter (`sub_42FBA0`, 2,388 bytes, 2,350 callers) handles all severity levels:

| Severity | Prefix | Tag | Behavior |
|---|---|---|---|
| 0 | (suppressed) | -- | Silently ignored |
| 1--2 | `"info    "` | `@I@` | Informational message |
| 3 | `"warning "` or `"error   "` | `@W@` or `@E@` | Context-dependent; promoted to error by `--Werror` |
| 4 | `"error*  "` | `@E@` | Non-fatal error |
| 5 | `"error   "` | `@E@` | Error |
| 6+ | `"fatal   "` | (none) | Calls `longjmp` to abort compilation |

The diagnostic system reads the source file to display context lines (prefixed with `"# "`), caching file offsets every 10 lines in a hash map for fast random-access seeking.

## Parser Initialization -- `sub_451730`

Parser initialization (14 KB) builds the lexer's symbol table with all built-in PTX names before parsing begins. This function is called from the compilation driver (`sub_446240`) and performs three major tasks:

### 1. Special Register Registration

All PTX special registers are pre-registered in the symbol table with their internal identifiers:

| Category | Registers |
|---|---|
| Thread/block ID | `%ntid`, `%laneid`, `%warpid`, `%nwarpid`, `%smid`, `%nsmid`, `%ctaid`, `%nctaid`, `%gridid` |
| Clocks | `%clock`, `%clock_hi`, `%clock64` |
| Performance counters | `%%pm0`--`%%pm7`, `%%pm0_64`--`%%pm7_64` |
| Lane masks | `%lanemask_eq`, `%lanemask_le`, `%lanemask_lt`, `%lanemask_ge`, `%lanemask_gt` |
| Environment | `%%envreg0`--`%%envreg31` |
| Timers | `%globaltimer_lo`, `%globaltimer_hi` |
| Shared memory | `%total_smem_size`, `%dynamic_smem_size` |
| Texture types | `.texref`, `.samplerref`, `.surfref` |
| Predefined macros | `GPU_ARCH`, `PTX_MAJOR_VERSION`, `PTX_MINOR_VERSION` |

### 2. Opcode Table Construction

Calls `sub_46E000` -- the 93 KB instruction table builder -- to register all PTX opcodes with their legal type combinations. See the dedicated section below.

### 3. Context State Initialization

Allocates and initializes two objects: the **parser state** (1,128 bytes, `sub_424070(pool, 1128)`) and the **lexer state** (2,528 bytes, `sub_424070(pool, 2528)`). The parser state stores a pointer to the lexer state at offset +1096. The string `"PTX parsing state"` identifies the parser state allocation in memory dumps. The string `"<builtin>"` serves as the filename for built-in declarations. Both objects are zeroed via `memset` before field initialization.

## Instruction Table Builder -- `sub_46E000`

This is the largest single function in the front-end region at 93 KB. It is not a normal function body but a massive initialization sequence that calls `sub_46BED0` exactly 1,141 times -- once per legal PTX instruction variant. Each call registers an opcode name together with its accepted type combinations using compact encoding strings.

### Operand Encoding Strings

Each instruction variant is registered with a string that encodes its operand signature. The encoding uses single-character codes for operand categories:

| Code | Meaning |
|---|---|
| `F` | Float operand (`.f16`, `.f32`, `.f64`) |
| `H` | Half-precision (`.f16`, `.f16x2`) |
| `I` | Integer operand (`.s8`--`.s64`, `.u8`--`.u64`) |
| `B` | Bitwise operand (`.b8`--`.b128`) |
| `N` | Immediate / numeric literal |
| `P` | Predicate operand |

String references found in the function include composite type signatures:

- `"F32F32"` -- binary float32 operation
- `"F16F16F16F16"` -- quad half-precision
- `"I32I8I8I32"` -- integer MMA (int32 accumulator, int8 operands)
- `"F64F64F64F64"` -- quad float64 (double-precision MMA)
- `"_mma.warpgroup"` -- warp-group MMA marker

### Hash Tables

The instruction table builder populates two hash tables at offsets +2472 and +2480 within the **lexer state object** (the 2,528-byte struct passed as the first argument to `sub_46E000`). These hash tables provide O(1) lookup from opcode name to the registered type combination list.

### Registration Function -- `sub_46BED0`

Called 1,141 times from `sub_46E000`. Each call takes an opcode name string and an operand encoding string, creates a descriptor node, and inserts it into the hash table. The descriptor captures the opcode, its legal operand types, and the semantic validation function to call during parsing.

## Instruction Lookup -- `sub_46C690` and `sub_46C6E0`

At parse time, when the parser reduces an instruction production, it calls `sub_46C690` to look up the instruction name in the hash table built by `sub_46E000`. The lookup returns a descriptor list, and `sub_46C6E0` (6.4 KB, the descriptor matcher) walks the list to find the variant matching the actual operands present in the source.

### Operand Classification -- 12 Categories

The descriptor matcher (`sub_46C6E0`) classifies each operand into one of 12 categories based on its syntactic form, then matches the category sequence against the registered encoding strings. The 12 categories cover:

1. General-purpose register (R)
2. Predicate register (P)
3. Uniform register (UR)
4. Uniform predicate (UP)
5. Integer immediate
6. Float immediate
7. Address expression (register + offset)
8. Label / symbol reference
9. Special register
10. Vector operand
11. Texture / surface / sampler reference
12. Bitfield / compound modifier

The classification examines token attributes set by the lexer -- register type bits at `(field >> 28) & 7`, immediate flag (`0x1000000`), uniform flag (`0x6000000`), and operand descriptor fields at instruction offset 84+.

## Parser State Object (1,128 bytes)

The parser passes a state object through all phases. This 1,128-byte structure (`sub_424070(pool, 1128)`) carries compilation context and pointers to sub-systems. It is indexed as `_QWORD*` (8-byte slots), so QWORD index `[N]` = byte offset `N*8`. The highest accessed byte is +1120 (index `[140]`), fitting exactly within the 1,128-byte allocation.

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `pool_context` | Pool allocator handle (from `sub_4258D0`) |
| +8 | 8 | `compilation_unit` | Pointer to compilation unit (parameter a2) |
| +16 | 8 | `macro_symbol_table` | Hash table for macros (`sub_425CA0`, 64 buckets) |
| +24 | 8 | `module_ptr` | Pointer to module object (parameter a3) |
| +32 | 8 | `container_a` | Sorted set container (8,192 buckets) |
| +56 | 8 | `scope_chain[0]` | Scope chain entry (`sub_44F7C0`), used for symbol resolution |
| +64 | 8 | `scope_chain[1]` | Second scope chain entry |
| +72 | 8 | `scope_chain[2]` | Third scope chain entry |
| +80 | 8 | `type_map` | Type descriptor hash map (`sub_42D150`, 8 buckets) |
| +96 | 8 | `symbol_tables[0..5]` | Six hash tables for symbol lookup (at +96, +104, +112, +120, +128, +136) |
| +152 | 8 | `current_function` | Pointer to current function being parsed |
| +160 | 4 | `ptx_major_version` | PTX ISA major version (set by Bison reduction) |
| +164 | 4 | `ptx_minor_version` | PTX ISA minor version |
| +168 | 4 | `sm_version_check` | SM target version for feature gating |
| +177 | 1 | `flag_a` | Initialization flag |
| +192 | 2 | `word_96` | Zero-initialized word at WORD index 96 |
| +196 | 4 | `address_size` | 32 or 64 (address width) |
| +208 | 8 | `hash_ref_a` | Hash table reference (64-bucket) |
| +236 | 1 | `default_flag` | Initialized to 1 |
| +264 | 16 | `list_a` | Linked list (head at +264, tail ptr at +272 points to head) |
| +280 | 8 | `sorted_set_b` | Sorted set (8,192 buckets) |
| +288 | 8 | `sorted_set_c` | Sorted set (1,024 buckets) |
| +296 | 16 | `sorted_maps[0..1]` | Two sorted maps (`sub_42A300`) |
| +320 | 8 | `hash_e` | Hash table (1,024 buckets) |
| +328 | 16 | `list_b` | Linked list (head/tail pair) |
| +344 | 16 | `list_c` | Linked list (head/tail pair) |
| +360 | 256 | `offset_table[16]` | SSE-initialized offset table (16 entries of 16 bytes each, computed from base address + constants at `xmmword_1CFDA00`--`1CFDA70`) |
| +616 | 16 | `list_d` | Linked list (head/tail pair) |
| +632 | 16 | `list_e` | Linked list (head/tail pair); low bits of first word used as `address_space_flags` |
| +648 | 8 | `local_symbol_table` | Per-scope local symbol table pointer |
| +824 | 8 | `symbol_lookup_ref` | Hash table for symbol name lookup |
| +832 | 1 | `dwarf_section_flag` | Nonzero when inside `.section` DWARF data |
| +834 | 1 | `directive_flag_a` | Checked as pair with +835 |
| +836 | 1 | `directive_flag_b` | Set to 1 by multiple Bison reductions |
| +840 | 8 | `builtin_filename` | Interned string `"<builtin>"` |
| +848 | 8 | `empty_string` | Interned empty string `""` |
| +856 | 4 | `sm_arch_number` | SM architecture number (parameter a6, e.g. 90 for sm_90) |
| +860 | 1 | `feature_a` | Feature flags set during parsing |
| +861 | 1 | `feature_b` | |
| +862 | 1 | `feature_c` | |
| +864 | 1 | `feature_d` | |
| +865 | 1 | `feature_e` | ORed with 1 by Bison reductions |
| +869 | 1 | `flag_h` | Initialized to 0 |
| +960 | 4 | `sm_target_code` | SM target code used in `sub_454E70` checks |
| +968 | 8 | `insn_stream_a` | Instruction stream pointer A (set in Bison) |
| +976 | 8 | `insn_stream_b` | Instruction stream pointer B |
| +984 | 8 | `insn_stream_c` | Instruction stream pointer C |
| +1000 | 1 | `insn_state_flag` | Instruction state flag (= 0) |
| +1008 | 8 | `string_pool` | String pool pointer |
| +1016 | 8 | `context_ref` | Compilation context reference (parameter a4) |
| +1048 | 4 | `dword_262` | Zero-initialized |
| +1053 | 1 | `parsing_active` | Toggled 1/0 during active parsing |
| +1080 | 16 | `list_f` | Linked list (head/tail pair) |
| +1096 | 8 | **`lexer_state_ptr`** | **Pointer** to 2,528-byte lexer state object (see below) |
| +1104 | 16 | `list_g` | Linked list (head/tail pair) |
| +1120 | 1 | `param_flag` | From parameter a10 |

### Lexer State Object (2,528 bytes)

The lexer state is a **separate** heap-allocated object (`sub_424070(pool, 2528)`) pointed to by `parser_state+1096`. It is the primary state carrier for the Flex DFA scanner and the instruction table subsystem. All functions that need scanner state (the Bison parser, the Flex scanner, the include handler, and the instruction table builder) access this object through the pointer at +1096.

| Offset | Size | Field | Description |
|---|---|---|---|
| +48 | 4 | `line_number` | Current source line (incremented on newline) |
| +52 | 4 | `column_number` | Current source column |
| +64 | 8 | `buffer_limit` | Pointer to end of current scan buffer |
| +76 | 4 | `start_condition` | Flex DFA start condition (`*(state+76)`, indexes `off_203C020`) |
| +152 | 1 | `flag_a` | Scanner state flag |
| +156 | 8 | `sentinel_a` | Initialized to -1 (0xFFFFFFFFFFFFFFFF) |
| +164 | 8 | `sentinel_b` | Initialized to -1 |
| +172 | 4 | `address_size_proxy` | Written by Bison via `sub_4563E0`; -1 on init |
| +180 | 8 | `zero_pair` | Zero-initialized |
| +188 | 8 | `sentinel_c` | Initialized to 0xFFFFFFFF00000000 |
| +196 | 8 | `sentinel_d` | Initialized to -1 |
| +204 | 4 | `sentinel_e` | DWORD[51], initialized to -1 |
| +208 | 2 | `word_104` | WORD[104], zero-initialized |
| +540 | 1 | `flag_b` | Scanner flag |
| +541 | 1 | `include_active` | Checked by Flex (`lexer+541`) and Bison to gate `.INCLUDE` behavior |
| +784 | 8 | `current_filename` | Pointer to current filename string (set during include handling) |
| +1984 | 128 | `version_array[32]` | DWORD array of version fields; written by `sub_70FDD0(lexer, index, value)` as `*(lexer + 4*index + 1984) = value` |
| +2104 | 4 | `ptx_major_ver` | `version_array[30]` = PTX major version (initialized to 9) |
| +2108 | 4 | `ptx_minor_ver` | `version_array[31]` = PTX minor version (initialized to 0) |
| +2128 | 8 | `include_stack_a` | Include nesting pointer 1 (linked list for file stack) |
| +2136 | 8 | `include_stack_b` | Include nesting pointer 2 |
| +2160 | 8 | `include_stack_head` | Head of include stack (walked by `sub_71C310`) |
| +2168 | 8 | `include_stack_file` | Include stack filename pointer |
| +2441 | 1 | `pushback_char` | Character pushed back into input stream by scanner |
| +2464 | 2 | `word_1232` | Zero-initialized |
| +2466 | 1 | `flag_c` | Flag |
| +2472 | 8 | `opcode_hash_a` | Opcode lookup hash table (populated by `sub_46E000`) |
| +2480 | 8 | `opcode_hash_b` | Second opcode lookup hash table (populated by `sub_46E000`) |
| +2488 | 8 | `context_sub_ref` | Compilation context sub-reference (parameter a9); accessed by Bison for `sub_457CB0`/`sub_70A5B0` calls |
| +2496 | 1 | `flag_d` | Flag |
| +2504 | 24 | `tail_fields` | Three zero-initialized QWORD slots (indices [313],[314],[315]) |

Version checks use `sub_485520(ctx, sm_number)` (SM architecture >= N) and `sub_485570(ctx, major, minor)` (PTX version >= major.minor). For example, the address-space attribute setter (`sub_4035D3`) checks `sm_90` and PTX `7.8`:

```c
if (!sub_485520(ctx, 90))
    sub_42FBA0(&err, loc, "sm_90", ...);   // Error: requires sm_90
if (!sub_485570(ctx, 7, 8))
    sub_42FBA0(&err, loc, "7.8", ...);     // Error: requires PTX 7.8
*(byte*)(v15 + 632) = (old & 0xFC) | (a2 & 3);   // Set address space bits
```

## Semantic Validators

The parser's reduction actions dispatch to specialized validator functions for each instruction category. These functions live in `0x460000`--`0x4D5000` and check SM architecture requirements, type compatibility, operand constraints, and instruction-specific invariants.

| Address | Size | Identity | Coverage |
|---|---|---|---|
| `sub_4B2F20` | 52.6 KB | General instruction validator | Textures, surfaces, loads, stores, cvt, calls |
| `sub_4CE6B0` tail | 48 KB | Directive/declaration validator | `.local_maxnreg`, `.alias`, `.unified`, `.pragma`, `.noreturn` |
| `sub_4C5FB0` | 28.5 KB | Operand validator | State spaces, rounding, barriers, cache levels |
| `sub_4C2FD0` | 12.2 KB | WMMA/MMA validator | Matrix dimensions, FP8 types, layout specifiers |
| `sub_49BBA0` | 11.4 KB | MMA scale/block validator | `.scale_vec_size`, `.block_scale`, sparse GMMA |
| `sub_4ABFD0` | 11.1 KB | Async copy validator | `cp.async`, bulk copy, `cvt.tf32.f32.rna` |
| `sub_4A73C0` | 10.9 KB | Tensormap validator | `.tile`, field ranges, `.tensormap::generic` |
| `sub_4BFED0` | 10.3 KB | WMMA shape/type validator | `.m%dn%dk%d` shapes, `.aligned` modifier |
| `sub_4AF9F0` | 5.8 KB | CVT validator | `cvt.f16x2.f32`, type combinations, rounding |
| `sub_4AEB60` | 3.7 KB | LDSM validator | `_ldsm.s8.s4`/`_ldsm.u8.u4` format conversion |
| `sub_4B1630` | 4.6 KB | Function address validator | `cudaDeviceSynchronize`, kernel/device addresses |
| `sub_498AF0` | 3.9 KB | MMA layout validator | Row/col layout, floating-point type constraints |
| `sub_497C00` | 3.0 KB | Prototype validator | `.FORCE_INLINE`, `.noreturn`, `.unique`, register counts |
| `sub_496690` | 3.6 KB | Scope/barrier validator | Scope modifiers, barrier constraints |
| `sub_494210` | 2.3 KB | Sparse GMMA validator | Sparse GMMA with specific types |
| `sub_492C80` | 4.0 KB | Cache eviction validator | L2 eviction priority, `.v8.b32`/`.v4.b64` |
| `sub_49A5A0` | 3.5 KB | Special register validator | `%laneid`, `%clock64`, `%lanemask_*`, arch gating |
| `sub_4A0CD0` | 4.9 KB | Variable declaration validator | `.texref`, `.managed`, `.reserved`, `.common` |
| `sub_4A02A0` | 2.6 KB | Initializer validator | `generic()` operator, function addresses |
| `sub_4036D9` | 437 B | Parameter list validator | Count, types, alignment, state space |

Validators follow a uniform pattern: they receive the parser context and instruction data, check constraints against the current SM architecture and PTX version, and call `sub_42FBA0` with descriptive error messages when violations are found. The general validator (`sub_4B2F20`, 52.6 KB) is the second-largest function in the front-end and covers the broadest range of PTX instructions.

## ROT13 Opcode Name Obfuscation

PTX opcode names stored in the binary are ROT13-encoded as an obfuscation measure. The static constructor `ctor_003` at `0x4095D0` (17 KB, ~1,700 lines) decodes and populates the opcode name table at `0x29FE300` during program startup. Each entry is a `(string_ptr, length)` pair. Decoded examples:

| ROT13 | Decoded | PTX instruction |
|---|---|---|
| `NPDOHYX` | `ACQBULK` | `acqbulk` |
| `OFLAP` | `BSYNC` | `bsync` |
| `PPGY.P` | `CCTL.C` | `cctl.c` |
| `SZN` | `FMA` | `fma` |
| `FRGC` | `SETP` | `setp` |
| `ERGHEA` | `RETURN` | `return` |
| `RKVG` | `EXIT` | `exit` |

The table covers the entire PTX ISA vocabulary -- hundreds of opcodes. A separate ROT13 table in `ctor_005` (`0x40D860`, 80 KB) encodes 2,000+ internal Mercury/OCG tuning knob names (see [Knobs System](../config/knobs.md)).

## Compilation Pipeline Integration

The parser is invoked from the top-level compilation driver `sub_446240` (11 KB), which orchestrates the full pipeline:

```
Parse  →  CompileUnitSetup  →  DAGgen  →  OCG  →  ELF  →  DebugInfo
```

The driver reports timing for each phase:

- `"Parse-time            : %.3f ms (%.2f%%)"`
- `"CompileUnitSetup-time : %.3f ms (%.2f%%)"`
- `"DAGgen-time           : %.3f ms (%.2f%%)"`
- `"OCG-time              : %.3f ms (%.2f%%)"`
- `"ELF-time              : %.3f ms (%.2f%%)"`
- `"DebugInfo-time        : %.3f ms (%.2f%%)"`

The parse phase encompasses the Flex scanner, macro preprocessor, Bison parser, instruction table lookup, and all semantic validation. Since the parser directly builds IR, the output of the parse phase is a populated instruction stream ready for the DAG generation phase.

## PTX Text Generation (Reverse Direction)

The inverse of parsing -- converting IR back to PTX text -- lives in `0x4DA340`--`0x5A8E40` (580 formatter functions). Each handles one PTX opcode. A dispatcher at `sub_5D4190` (12.9 KB) routes by opcode name using 81 direct string comparisons plus a 473-entry hash switch. Every formatter follows an identical allocation pattern:

```c
pool = sub_4280C0(ctx)[3];              // Get allocator pool
buf = sub_424070(pool, 50000);          // 50KB temp buffer
// ... sprintf() operands into buf ...
len = strlen(buf);
result = sub_424070(pool, len + 1);     // Exact-size allocation
strcpy(result, buf);
sub_4248B0(buf);                        // Free temp buffer
return result;
```

A monolithic format string table (~1.8 MB) at the `a2` parameter contains pre-assembled PTX text templates with `%s`/`%llu`/`%d` placeholders. This trades memory for speed: instead of building instruction text dynamically, ptxas simply fills in operand names at runtime.

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_720F00` | 15.8 KB | `ptxlex` -- Flex DFA scanner main | 98% |
| `sub_4CE6B0` | 48 KB | `ptxparse` -- Bison LALR(1) parser | HIGH |
| `sub_46E000` | 93 KB | Instruction table builder (1,141 opcode registrations) | HIGH |
| `sub_46BED0` | -- | Per-opcode registration function (called 1,141x) | HIGH |
| `sub_46C690` | -- | Instruction lookup entry | HIGH |
| `sub_46C6E0` | 6.4 KB | Descriptor matcher (12-category operand classifier) | HIGH |
| `sub_451730` | 14 KB | Parser initialization (allocs 1,128B parser state + 2,528B lexer state) | HIGH |
| `sub_70FDD0` | 14 B | Lexer version array writer: `*(a1 + 4*a2 + 1984) = a3` | HIGH |
| `sub_71F630` | 14 KB | Preprocessor directive dispatcher | 93% |
| `sub_71E2B0` | 32 KB | Conditional handler (`.ELSE`/`.ELIF`/`.ENDIF`) | 92% |
| `sub_71DCA0` | 8.4 KB | Macro definition handler (`.MACRO`) | 90% |
| `sub_71C910` | 13 KB | Directive scanner | 91% |
| `sub_71C310` | 8.3 KB | Include handler (`.INCLUDE`) | 90% |
| `sub_71D1B0` | 6.8 KB | Macro argument scanner | 89% |
| `sub_71D710` | 7.5 KB | Macro body scanner | 89% |
| `sub_71BA10` | 2.3 KB | Macro character peek | 88% |
| `sub_71BB80` | 2.6 KB | Macro buffer reader | 88% |
| `sub_71BE20` | 1.1 KB | Macro expansion entry | 85% |
| `sub_71BF60` | 1.8 KB | Macro fatal abort | 90% |
| `sub_71C140` | 2.5 KB | Macro format error | 88% |
| `sub_720190` | 2.0 KB | `ptxensure_buffer_stack` | 95% |
| `sub_7202E0` | 1.3 KB | `ptx_create_buffer` | 96% |
| `sub_720410` | 3.3 KB | `yy_get_next_buffer` | 95% |
| `sub_720630` | 9.7 KB | `yy_get_previous_state` (SSE2 optimized) | 94% |
| `sub_720BA0` | 4.3 KB | `ptx_scan_string` | 93% |
| `sub_724CC0` | 4.9 KB | `ptx_scan_bytes` / macro nesting check | 91% |
| `sub_725070` | 2.7 KB | `ptx_scan_buffer` | 93% |
| `sub_42FBA0` | 2.4 KB | Central diagnostic emitter (2,350 callers) | HIGH |
| `sub_4280C0` | 597 B | Thread-local context accessor (3,928 callers) | HIGH |
| `sub_424070` | 2.1 KB | Pool allocator (3,809 callers) | HIGH |
| `sub_4248B0` | 923 B | Pool deallocator (1,215 callers) | HIGH |
| `sub_42BDB0` | 14 B | Fatal OOM handler (3,825 callers) | HIGH |
| `sub_446240` | 11 KB | Top-level compilation driver | HIGH |
| `sub_4095D0` | 17 KB | ROT13 opcode name table initializer | HIGH |
| `sub_5D4190` | 12.9 KB | PTX text format dispatcher | HIGH |
| `sub_4B2F20` | 52.6 KB | General instruction validator | HIGH |
| `sub_4C5FB0` | 28.5 KB | Instruction operand validator | HIGH |
| `sub_4C2FD0` | 12.2 KB | WMMA/MMA validator | HIGH |
| `sub_485520` | -- | SM architecture check (`sm >= N`) | HIGH |
| `sub_485570` | -- | PTX version check (`version >= M.N`) | HIGH |

## Cross-References

- [Pipeline Overview](./overview.md) -- where the parser fits in the compilation flow
- [PTX Directive Handling](./ptx-directives.md) -- detailed directive processing after parsing
- [PTX-to-Ori Lowering](./ptx-to-ori.md) -- what happens to the IR the parser builds
- [Knobs System](../config/knobs.md) -- ROT13-encoded knob names from `ctor_005`
- [Memory Pool Allocator](../infra/memory-pools.md) -- `sub_424070`/`sub_4248B0` pool system
- [Hash Tables & Bitvectors](../infra/hash-bitvector.md) -- `sub_426150`/`sub_426D60` hash map
- [PTX Instruction Table](../reference/ptx-instructions.md) -- full opcode catalog
- [CLI Options](../config/cli-options.md) -- `sub_432A00`/`sub_434320` option handling
