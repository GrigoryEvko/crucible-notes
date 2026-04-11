# PTX Parser (Flex + Bison)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

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

`sub_46C690` (lines 4--16 of `sub_46C690_0x46c690.c`) is a trivial wrapper: it probes the two opcode hash tables at lexer-state offsets `+2472` and `+2480` with `sub_426D60` and returns the first nonzero bucket's `*(_DWORD*)(entry+8+8)` (the descriptor head pointer). The real work happens in `sub_46C6E0`, which is called directly from the Bison reduction actions with the raw token list.

### Operand Classification -- 12 Categories

The descriptor matcher classifies every operand into one of twelve category codes **before** walking the candidate descriptor list. The classifier is the leading loop in `sub_46C6E0` (lines 142--249 of `sub_46C6E0_0x46c6e0.c`): it iterates `a8` times (`a8` = parsed operand count), reads each 8-byte operand-token pointer `v14 = *(_DWORD **)(a6 + 8*i)` (note: the source uses `2 * v13` with `v13` stepped by 4, which is a byte stride of 8), and dispatches on `*v14` (the first DWORD, a **lexer token-kind enum**, distinct from the AST-node 6-bit tag of IR-08). The switch writes two parallel slots of the stack array `v133`:

- `v133[i]` -- the **category code** (0--11), occupying `[0..15]`
- `v133[i + 16]` -- the operand's **bit width** obtained from `sub_44B390(v14)` (which walks the type token and folds `*= 2/4/8/16/32/64/128` or `*= arraylen` for aggregates)

Category 0 is the implicit default (any token that hits the `default: break;` at line 244 leaves `v133[i]` **unwritten**, so it is effectively the sentinel "unclassified"). That produces twelve distinct states numbered 0--11. Every classification is a pure table lookup on `*v14`; no flag bits, no uniform-register `0x6000000` mask, no `(field>>28)&7` test -- those checks live in the *lexer* (where the token kind itself was assigned), not in the matcher. By the time `sub_46C6E0` sees the operand, the distinction between `R5` vs `UR5` vs `%r5` is already baked into the numeric value of `*v14`.

#### The 12 Category Codes

| Code | Name | Token-kind values (`*v14`) from the switch | Category meaning | Encoding-string role |
|---|---|---|---|---|
| **0** | *(unclassified)* | `0x3D..0x3F`, `0x41..0x44`, any kind hitting `default:` (line 244) | Token shapes with no direct classifier entry (aggregate/wildcard wrappers resolved elsewhere) | Matches the "missing slot" sentinel; descriptor slots marked `0` in `v50[9+i]` compare equal to an uninitialized `v133[i]` |
| **1** | Label / branch target | `0x34`, `0x3A`, `0x3B` (line 219) | Identifier reference that will resolve to a code or data label (`bra L1`, `call foo`) | Paired with AST kind 14 (3) in the inner check at line 1416 (`case 0xE`) |
| **2** | Integer-data register | `0x38`, `0x39` (line 232) | Signed/unsigned integer register class (`%r1`, `.s32`/`.u32` typed) | Width-compared in `case 0`/`case 2` (lines 1061--1073), integer-only guards via `sub_457AE0`, `sub_457B40`, `sub_457B80` |
| **3** | Large-width float/packed vector register | `0x0C`, `0x0E`, `0x14`, `0x16` (line 180) | 32-bit+ float register or vector-packed form (`%f1`, `%fd1`, `%r1v4`) | Width 32 or pair used by `case 5: case 3`; triggers `v122 = v74` comparison path |
| **4** | Small/medium integer or byte register | `0x09..0x0B`, `0x0D`, `0x0F..0x13`, `0x15`, `0x17`, `0x18` (line 173) | Byte/half/word register with signed/unsigned/bit flavor | General register slot; width pulled into `v122`, compared in the `case 0` giant switch |
| **5** | Type-width / qualifier token | `0x01..0x08` (line 158) | Bare type qualifier (`.s8`..`.b128`, `.pred`) used as a free-standing operand (uncommon; appears in state-space prefixes) | Consumed by `case 0x11` (line 1436) which rejects unless the descriptor bit at `v61+17 & 0x40` permits it |
| **6** | Predicate register | `0x3C` (line 237) | `%p0`..`%p7` and vote/select predicate operands | Handled by `case 0x14` (line 1453): rejects unless AST kind 15; also the only kind permitted in the `0x170` bit-check fast path of predicate-only instructions (line 411) |
| **7** | Aggregate / structured constant | `0x40` (line 241) | Composite constant -- initializer list, sub-struct aggregate used by `.param` and texture-array instructions | Width read via `sub_44B390` which recurses through `case 0x42`/`case 0x44` (lines 99--109 of `sub_44B390`) to expand the aggregate |
| **8** | Constant address-space reference | `0x35`, `0x36` (line 224) | `.const` or `.param` address-space name (`c[0x0]`, `param[0]`) | Appears in memory-op encoding variants; matcher uses descriptor bit `v12+617 & 0x20` (line 324) to pre-filter descriptors that require address-space operands |
| **9** | Global address-space reference | `0x37` (line 228) | `.global` address operand | Distinct from 8 so that ld/st matchers can accept `.global` without also accepting `.const` |
| **10** | Typed immediate (integer/bit literal) | `0x19..0x1F`, `0x22`, `0x25..0x30`, `0x33` (line 204) | Integer / hex / binary literal with explicit type suffix | Drives the "immediate allowed" check at `v12+600 & 2` (line 523): if the descriptor forbids immediates, any category-10 operand kills that descriptor via `v13 & 1` at offset 13 |
| **11** | Typed immediate (float/double literal) | `0x20`, `0x21`, `0x23`, `0x24`, `0x31`, `0x32` (line 213) | Float / double literal (`0f3F800000`, `0d...`) | Same immediate-gate as cat 10, but also participates in the float-type check `v12+611 & 0x30` at line 857 |

The distinction is that the classifier runs *once* per operand in a tight switch, while the matcher then walks a list of candidate descriptors and rejects each one using a series of **descriptor-bit-against-category-code** filters before finally doing a full per-operand check. The 11 explicit categories plus category 0 (default/unclassified) give 12 states, which fits in 4 bits -- but the binary stores them as full `_DWORD`s (`v133` is `_DWORD v133[32]`, line 135) because the compare at line 950 (`if ( v50[9] != v133[0] )`) is a direct int compare against the descriptor's pre-serialized category sequence.

#### Classifier Pseudocode

```c
// Extracted from sub_46C6E0 lines 142-249. Pure function over token array.
void classify_operands(
    const OperandToken *tokens[],  // a6: array of 8-byte token pointers
    size_t count,                  // a8: number of operands
    uint32_t cat[32],              // v133[0..15]  -- output category codes
    uint32_t width[32])            // v133[16..31] -- output bit widths
{
    // v133 is zero-initialized only implicitly; default: leaves cat[i] at
    // its prior (unset) value, which effectively acts as "category 0".
    for (size_t i = 0; i < count; i++) {
        const uint32_t *tok = (const uint32_t *)tokens[i];
        switch (tok[0]) {                       // first DWORD = token-kind enum
            case 0x01: case 0x02: case 0x03: case 0x04:
            case 0x05: case 0x06: case 0x07: case 0x08:
                cat[i] = 5; break;              // type-width / qualifier
            case 0x09: case 0x0A: case 0x0B: case 0x0D:
            case 0x0F: case 0x10: case 0x11: case 0x12:
            case 0x13: case 0x15: case 0x17: case 0x18:
                cat[i] = 4; break;              // small/medium integer register
            case 0x0C: case 0x0E: case 0x14: case 0x16:
                cat[i] = 3; break;              // 32b+ float / packed vector reg
            case 0x19: case 0x1A: case 0x1B: case 0x1C:
            case 0x1D: case 0x1E: case 0x1F: case 0x22:
            case 0x25: case 0x26: case 0x27: case 0x28:
            case 0x29: case 0x2A: case 0x2B: case 0x2C:
            case 0x2D: case 0x2E: case 0x2F: case 0x30:
            case 0x33:
                cat[i] = 10; break;             // typed integer immediate
            case 0x20: case 0x21: case 0x23: case 0x24:
            case 0x31: case 0x32:
                cat[i] = 11; break;             // typed float immediate
            case 0x34: case 0x3A: case 0x3B:
                cat[i] = 1;  break;             // label
            case 0x35: case 0x36:
                cat[i] = 8;  break;             // .const / .param addr
            case 0x37:
                cat[i] = 9;  break;             // .global addr
            case 0x38: case 0x39:
                cat[i] = 2;  break;             // integer data reg
            case 0x3C:
                cat[i] = 6;  break;             // predicate reg
            case 0x40:
                cat[i] = 7;  break;             // aggregate constant
            default:
                /* cat[i] left unset -> effective category 0 */
                break;
        }
        width[i] = bit_width_of(tok);           // sub_44B390
    }
}
```

#### Matcher Pseudocode

Once the category array is built, the matcher (same function, lines 250--1473) walks the descriptor candidates returned by the dual hash lookup against `lexer_state+2472` and `lexer_state+2480`. The candidates are copied into a local `v135[326]` buffer with a running count `v20`, then filtered in **phases**: each phase tests a descriptor bit against a category predicate and *zeroes* non-matching entries in place, decrementing `v22` (the live-candidate count). Surviving descriptors are compared operand-by-operand in a final pass.

```c
Descriptor *match_instruction(
    LexerState *lex,        // a1
    const char *opcode,     // a2  -- opcode name for the hash probe
    /* ... */,
    OperandToken *ops[],    // a6
    int op_count,           // a8
    uint64_t *diag_lock)    // a10
{
    InsnTableCtx *ctx = lex->ctx;         // v12 = *(a1 + 1096)
    uint32_t cat[32], width[32];
    classify_operands(ops, op_count, cat, width);

    // --- hash lookup: both tables, linked-list concat into v135 ---
    Descriptor *list1 = hash_lookup(lex->tbl_2472, opcode);  // sub_426D60
    Descriptor *list2 = hash_lookup(lex->tbl_2480, opcode);
    if (!list1 && !list2) {
        emit_diag(dword_29FB550, diag_lock, parser_state);   // "unknown opcode"
        return NULL;
    }
    Descriptor *cand[326];
    int n = 0;
    for (Descriptor *p = list1; p; p = p->next) cand[n++] = p->payload;
    for (Descriptor *p = list2; p; p = p->next) cand[n++] = p->payload;
    int live = n;

    // --- Phase 1: opcode-class gate (v12+617 & 0x20) ---
    // Only descriptors whose byte at +22 has bit 1 set survive (line 348).
    if (ctx->byte_617 & 0x20) {
        for (int i = 0; i < n; i++)
            if (cand[i] && !(cand[i]->flags_22 & 2)) { cand[i] = NULL; --live; }
        if (!live) { emit_diag(sub_708200(ctx), diag_lock, ...); return NULL; }
    }

    // --- Phase 2: modifier-bit gates (v12+644, .ftz/.sat/.rnd set) ---
    uint32_t m = ctx->dword_644;
    if (m) {                                        // lines 390-505
        // per-descriptor byte-flag filter selected by (m & 2) and (m & 4),
        // reading either desc->byte_21 & 1, desc->byte_20 >> 7, or desc->byte_12 & 1
        // depending on whether ctx->dword_640 == 27 (FMA family special-case).
        filter_by_modifier_bits(cand, &live, m, ctx);
        if (!live) { emit_diag(sub_707530(ctx), diag_lock, ...); return NULL; }
    }

    // --- Phase 3..N: one filter per feature bit in ctx+600..+630 ---
    // Each phase maps 1:1 to a PTX modifier class. The complete list from
    // sub_46C6E0 (lines 510-930):
    //   ctx+628 & 0x40 -> desc->byte_25 & 1    (predicated)
    //   ctx+600 & 0x02 -> desc->byte_13 & 1    (wide/no-wide)
    //   ctx+600 & 0x80 -> desc->byte_13 & 0x10 or sub_4CE100()  (vector form)
    //   ctx+621 & 0x70 -> desc->byte_23 & 0x08 (cache-op variant)
    //   ctx+630 & 0x02 -> desc->byte_26 & 0x08 (async-copy group)
    //   ctx+629 & 0x80 -> desc->byte_26 & 0x02 (TMA / tensor-mem)
    //   ctx+612 & 0x08 -> desc->byte_18 & 0x80 (level qualifier)
    //   ctx+612 & 0x70 -> desc->byte_19 & 0x01 (scope qualifier)
    //   ctx+610 & 0x3C0 ->desc->byte_18 & 0x02 (ordering .relaxed/.acq/.rel)
    //   ctx+612 & 0x04 -> desc->byte_18 & 0x40 (mmu / tex level)
    //   ctx+620 & 0x38000->desc->byte_23 & 0x10(shared-memory variant)
    //   ctx+629 & 0x40 -> desc->byte_26 & 0x01 (dst-predicate)
    //   ctx+627 & 0x30 -> desc->byte_24 & 0x10 when AST kind 13 (reserved)
    //   ctx+611 & 0x30 -> desc->byte_18 & 0x08 (wmma/mma layout)
    //   ctx+612 & 0x80 -> desc->byte_19 & 0x02 (half-precision lane)
    //   ctx+613 & 0x03 -> desc->byte_19 & 0x04 (tensor-core accumulator)
    // Each phase that drops live to 0 emits a distinct diagnostic
    // (sub_707610, sub_707CE0, sub_70A180, sub_708860, sub_707B60, sub_70AFA0,
    //  sub_70B080, sub_70AAD0, sub_70AEF0, sub_70A0D0, sub_707AB0, sub_709860,
    //  sub_70ACC0, sub_70AB30, sub_70ABA0) so the user sees exactly which
    //  modifier family disqualified every candidate.

    // --- Phase N+1: operand-category comparison (line 940-1560) ---
    // v50 = cand[j].  v50[8] = descriptor's op_count.
    // v50[9..9+op_count-1] = expected category sequence.
    for (int j = 0; j < n; j++) {
        Descriptor *d = cand[j];
        if (!d) continue;
        if (d->op_count != op_count)      goto fail;
        if (!op_count)                    break;        // opcode-only match ok
        if (d->cat[0] != cat[0])          goto fail;    // line 950

        for (int k = 1; k < op_count; k++) {
            if (ops[k]->kind == 64) continue;           // skip aggregate wrapper
            // Per-slot detailed check: the descriptor slot at v50[2*k+24]
            // stores an "operand-check selector" (0..16). sub_1CB0820 dispatches
            // on it and on cat[k]/width[k], and also consults the type bits via
            // a big switch at lines 1009-1469:
            //   0: width compare (with optional %tid/%ntid/%ctaid special-case,
            //      line 1232: recognizes "%gridid" -> 32->64 widening)
            //   1: integer-only (via sub_457610 / sub_457490)
            //   2..7: fp / bit / vector subclass checks
            //   8,9,A,B: exact-width (8/16/32/64) constraints
            //   C: %tid / %laneid / %warpid / %smid special-register whitelist
            //   D,F: width == 2 (i.e. half-word) gate
            //   E: AST kind == 3 (register-triplet)
            //   10: integer-or-half via sub_457A00 guard
            //   11: only accept non-"standard" widths if ptx_major > 1 or (2, m1)
            //   12: AST kind == 4 required
            //   13: AST kind == 61 (identifier) or sub_457B60/sub_457B80 pass
            //   14: AST kind == 15 required (predicate)
            //   16: AST kind == 4 and sub_44A220()[0] == 0 (symbol undef)
            // Any failure "goto LABEL_153" zeros cand[j] and --live.
            if (!check_op_slot(d->slot[k], cat[k], width[k], ops[k]))
                goto fail;
            // Also require d->cat[k] == cat[k] (category sequence identity,
            // line 964).
            if (d->cat[k+1] != cat[k]) goto fail;
        }
        // Bonus suffix-check (lines 1474-1553): d->cat[9] onward is a
        // 16-slot ragged "trailing-modifier sequence". Ordered pair-scan --
        // if any (expected, present) disagrees and expected is not zero, drop.
        if (!trailing_modifier_match(d)) goto fail;
        continue;
    fail:
        cand[j] = NULL;
        --live;
    }

    // --- Ambiguity / failure reporting ---
    if (!live) {
        // None survived the operand-category pass.
        emit_diag(dword_29FB630, diag_lock, parser_state); // "no matching variant"
        return NULL;
    }

    // If a7 (the expected sm_target_code) is 0 and exactly one candidate
    // survives, return it -- the first non-null one (line 994: `if (!a7) return *v107;`).
    // Otherwise, iterate survivors and keep the one whose d->sm_target (at
    // offset +232) equals a7.  The PTX version guard at (a1+160 > 1 || (a1+164 > 2
    // && a1+160 == 1)) additionally excludes pre-ISA-1.x descriptors.
    Descriptor *hit = NULL;
    for (int j = 0; j < n; j++) {
        if (!cand[j]) continue;
        if (cand[j]->sm_target == a7) { hit = cand[j]; break; }
    }
    if (hit) return hit;

    // Survivors exist but none match the active SM target.
    emit_diag(dword_29FB640, diag_lock, parser_state); // "no variant for this sm_XX"
    return NULL;
}
```

#### Ambiguity Resolution

The matcher is **not** a pure "first match wins" scheme. When multiple descriptors survive every filter, disambiguation is by the **SM target code** stored at descriptor byte `+232` (compared against `a7`, the compile target). If `a7 == 0` (target-independent lookup, as during macro expansion), the matcher returns the first surviving descriptor unconditionally (line 994). If more than one descriptor survives **and** more than one has matching `sm_target`, the matcher still returns the first one encountered in list order -- there is no tie-breaking heuristic, so instruction-table registration order (which is deterministic because `sub_46E000` registers in source order) is the silent arbiter. Truly-ambiguous encodings are prevented at table-build time rather than at parse time.

#### Failure Diagnostics

`sub_46C6E0` emits five distinct diagnostic message IDs via `sub_42FBA0(id, lock, parser_state)`:

| ID | Raised at | Meaning |
|---|---|---|
| `dword_29FB550` | Line 269, 978 | Opcode hash lookup returned empty (unknown opcode) or final candidate list is empty |
| `dword_29FAF70` | Line 290 | Modifier-filter dropped all candidates (via `sub_707530`/`sub_709510` which format "illegal modifier combination") |
| `dword_29FB630` | Line 978 | All candidates died in the operand-category pass; no variant accepts this operand signature |
| `dword_29FB640` | Line 1572 | Variants exist but none support the requested SM target |
| (family of `sub_707610`/`sub_70A180`/...) | Per-phase | Modifier-class-specific diagnostics, one per descriptor-bit filter phase |

Each per-phase emitter reports exactly which modifier family cost the match, which is why ptxas produces targeted messages like "`.wide` not valid for this instruction" rather than a generic "operand mismatch".

The classification examines token attributes set by the lexer. The bit tests mentioned in older wiki drafts (`(field >> 28) & 7`, `0x1000000`, `0x6000000`) live in the **lexer** (`sub_44F2A0` and its callees) where the token-kind value is assigned -- the matcher itself only reads the already-encoded token kind at `*v14`.

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
