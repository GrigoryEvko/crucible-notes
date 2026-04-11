# PTX Parser (Flex + Bison)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas front-end parses PTX assembly text into internal IR using a classic two-stage architecture: a Flex-generated DFA scanner (lexer) and a Bison-generated LALR(1) shift-reduce parser. Unlike most compiler front-ends, the parser does **not** construct an AST. Instead, Bison reduction actions directly build IR nodes, populate the instruction table, and emit validation calls -- the parse tree is consumed inline and never materialized as a data structure. A separate macro preprocessor handles `.MACRO`, `.ELSE`/`.ELIF`/`.ENDIF`, and `.INCLUDE` directives at the character level before tokens reach the Flex DFA. The instruction table builder (`sub_46E000`, 93 KB) registers all PTX opcodes with their legal type combinations during parser initialization, and an instruction lookup subsystem classifies operands into 12 categories at parse time.

| | |
|---|---|
| **Flex scanner** | `sub_720F00` (15.8 KB, 64 KB with inlined helpers) |
| **DFA table** | `off_203C020` (transition/accept array) |
| **Scanner rules** | ~552 Flex rules, 165 named tokens (codes 258--422) + 25 character literals |
| **Scanner prefix** | `ptx` (all Flex symbols: `ptxlex`, `ptxensure_buffer_stack`, etc.) |
| **Bison parser** | `sub_4CE6B0` (48 KB, spans `0x4CE6B0`--`0x4DA337`) |
| **Grammar size** | **513 productions** (443 with custom actions + 70 default), **1,099 states**, **193 terminals**, **182 non-terminals** |
| **LALR tables** | 9 tables at `0x1D121A0`--`0x1D16148` (≈19.9 KB): `yypact`, `yydefact`, `yytable`, `yycheck`, `yypgoto`, `yydefgoto`, `yyr1`, `yyr2`, `yytranslate` -- see [Grammar Parameters](#grammar-parameters) for VA mapping |
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
│  Token codes:      258-422 (163 named emitted)           │
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

The parser is a standard Bison-generated LALR(1) shift-reduce parser spanning 48 KB (addresses `0x4CE6B0`--`0x4DA337`). It contains exactly **513 grammar productions** (rules 1--513, plus the implicit `$accept` augmentation) with **443 reduction cases** carrying non-default semantic actions; the remaining 70 rules use Bison's default action (`$$ = $1`, no code emitted). The function calls `ptxlex` (`sub_720F00`) to obtain tokens and uses **nine** LALR tables for state transitions, action lookup, and goto computation:

| Table | Address | Bison name | Element type | Entries | Purpose |
|---|---|---|---|---|---|
| `byte_1D15FA0` | `0x1D15FA0` | `yytranslate` | `uint8` | 423 | External token code → internal terminal index |
| `word_1D121A0` | `0x1D121A0` | `yycheck` | `int16` | 2,269 | Expected terminal at action-table offset |
| `word_1D13360` | `0x1D13360` | `yytable` | `int16` | 2,269 | Shift destination / negated reduce rule |
| `word_1D14520` | `0x1D14520` | `yypgoto` | `int16` | 182 | Goto table offset per non-terminal |
| `word_1D146A0` | `0x1D146A0` | `yypact` | `int16` | 1,099 | Action-table offset per state |
| `word_1D14F40` | `0x1D14F40` | `yydefgoto` | `int16` | 182 | Default goto per non-terminal |
| `word_1D150C0` | `0x1D150C0` | `yydefact` | `int16` | 1,099 | Default reduction rule per state |
| `word_1D15B80` | `0x1D15B80` | `yyr1` | `int16` | 514 | LHS symbol number per rule |
| `byte_1D15960` | `0x1D15960` | `yyr2` | `uint8` | 514 | RHS length per rule |

> **Table naming correction.** Earlier versions of this page listed `word_1D146A0` as `yydefact`, `word_1D13360` as `yypact`, and `word_1D150C0` as `yypgoto`. These labels were wrong: the decompiled indexing patterns (below) identify `word_1D146A0` as `yypact` (indexed by state, values range `[-921..2101]` with the `-921` sentinel), `word_1D13360` as `yytable` (indexed by `yypact[state]+token`), and `word_1D150C0` as `yydefact` (indexed by state, values are reduction rule numbers `0..513`). The true `yypgoto` and `yydefgoto` (not previously documented) live at `0x1D14520` and `0x1D14F40`.

### Grammar Parameters

The following constants were recovered by cross-correlating the five hardcoded sentinels in the decompiled state machine with the `.rodata` table sizes and boundary signatures. File references use `sub_4CE6B0_0x4ce6b0.c` in `/ptxas/decompiled/`.

| Bison macro | Value | Derivation |
|---|---|---|
| `YYNSTATES` | **1,099** | `yypact` has 1,099 `int16` entries (size `0x1D150C0 - 0x1D146A0 = 0x0A20 = 2,592 B`, last non-zero at byte 2,197; upper bound of `0xA20/2 = 1,296` slots leaves 197 trailing zero-pad bytes). `yydefact` (same index domain) shows exactly 1,099 entries with identical tail. |
| `YYNTOKENS` | **193** | Extracted directly from decompiled line 2398: `v31 = (unsigned __int16)word_1D15B80[v1136] - 193;` (the `yyr1[rule] - YYNTOKENS` conversion from LHS symbol number to non-terminal index). Cross-verified by `yyr1[1] = 193` (the `$accept` nonterminal's symbol number equals `YYNTOKENS`). Also equals the number of distinct `yytranslate` output values (192 real terminals + undef slot). |
| `YYNNTS` | **182** | `yydefgoto` and `yypgoto` each have 182 `int16` entries (gap `0x1D150C0 - 0x1D14F40 = 0x180 = 384 B`, last non-zero at byte 363). Independently confirmed: `yyr1` LHS values cover exactly the range `[193..374]` ⇒ `374 - 193 + 1 = 182` distinct non-terminal symbol numbers. Total grammar symbols: `YYNTOKENS + YYNNTS = 375`. |
| `YYNRULES` | **513** | `yyr1` has 514 `int16` entries (`yyr1[0] = 0` unused, rules 1..513). Gap `0x1D15FA0 - 0x1D15B80 = 0x420 = 1,056 B` ⇒ ≤528 slots; last non-zero at byte 1,027 ⇒ 514 entries. `yyr2` matches: 514 byte-entries, with `yyr2[510..513] = {0,0,0,0}` encoding four epsilon rules (invisible to the "last-non-zero" heuristic). `yydefact` contains values up to 513. **The previous "~512 productions" figure was off by one** -- the switch-case max of 512 (line 7395) is the highest rule number carrying a custom semantic action; rule 513 (`yyr1[513] = 374`, `yyr2[513] = 0`) has a default action and falls through `default: goto LABEL_34;` at line 7398. |
| `YYLAST` | **2,268** | Hardcoded as `0x8DC` in the bounds checks at decompiled lines 1376 (`if ( (unsigned int)(v4 + v9) > 0x8DC ) goto LABEL_8;`), 1383, 1427, and 2400. `yycheck` and `yytable` each hold `YYLAST + 1 = 2,269` `int16` entries (last non-zero at byte 4,537 of 4,544 allocated). |
| `YYFINAL` | **3** | Accept state, used at decompiled line 1434 (`if ( i == 3 ) goto LABEL_1306;`) and again at line 7470 (`if ( i != 3 )`), both branching to cleanup and successful return. |
| `YYPACT_NINF` | **-921** | Initial `v4 = -921` at line 1347, used as the "no shift action" sentinel for `yypact`. 708 of 1,099 states carry this value (≈64% of states have no shift action and rely solely on `yydefact`). |
| `YYTABLE_NINF` | **-513** | Literal `i == -513` check at line 1500 (`if ( !word_1D13360[v8] \|\| i == -513 )`), the "no action" sentinel for `yytable`. Matches `-YYNRULES` convention. |
| `YYMAXUTOK` | **422** | Upper bound on external token codes: `if ( v1128 <= 422 )` at line 1372, followed by `yytranslate[v1128]` lookup. Token codes above 422 are treated as undef. `yytranslate` therefore holds exactly 423 entries (`yytranslate[0..422]`). |
| `YYTERROR` | **1** | Error-recovery lookahead token; `word_1D121A0[yypact + 1] != 1` check at line 1429 tests `yycheck[yypact[state] + YYTERROR] == YYTERROR`. Also `yytranslate[256] = 1` (Bison always places `error` at external code 256). |
| `YYEMPTY` | **-2** | Initial `v1128 = -2` at line 1351, with `v1128 == -2` guarding the lookahead-empty path at line 1359. |
| `YYINITDEPTH` | **200** | Initial stack size: `_WORD src[200]` at line 1340 (the `yyssa` on-stack array) and `v1131 = 200` at line 1352. |
| `YYMAXDEPTH` | **10,000** | Upper bound on stack doubling: `0x2710 = 10,000` at line 1455 (`v34 = 10000; if ( 2 * v1131 <= 0x2710 ) v34 = 2 * v1131;`). On overflow the parser aborts with `"memory exhausted"` (line 1467). |
| **Start symbol** | **non-terminal 194** (`nt-index 1`) | `yyr1[1] = 193` encodes `$accept` (rule 1 is always `$accept: start $end`). Rule 2 has `yyr1[2] = 195, yyr2[2] = 0` (a midrule-action auxiliary). Rule 3 -- the first substantive rule -- has `yyr1[3] = 194, yyr2[3] = 2`, making **symbol 194** the grammar's start non-terminal (lowest user-declared non-terminal after `$accept`). |
| Longest RHS | **15** (at rule 190) | Maximum of the 514-entry `yyr2` byte array. |
| Epsilon rules | **31** | Count of `yyr2[i] == 0` for `i` in `1..513`. |

**Terminal alphabet composition.** The 193 terminals decompose as: `YYEOF` (code 0 → terminal 0), `YYerror` (code 256 → terminal 1), `YYUNDEF` (code 257 → terminal 2), 165 named tokens emitted by the Flex scanner (codes 258--422 → terminals 3--167), and **25 single-character literal tokens** at ASCII codes `! % & ( ) * + , - / : ; < = > ? @ [ \ ] ^ { | } ~` (→ terminals 168--192). This explains the "162 token types (codes 258--422)" figure in the page header as an undercount: the true named-token range is 258--422, i.e. 165 distinct named tokens; with the 25 character literals and the three housekeeping tokens, the grammar's terminal count is 193.

**Memory footprint of the tables.** `.rodata` VA range `0x1D121A0..0x1D161E8` holds ≈19.9 KB of parser data: `yycheck` 4,538 B + `yytable` 4,538 B + `yypgoto` 364 B + `yypact` 2,198 B + `yydefgoto` 364 B + `yydefact` 2,198 B + `yyr2` 514 B + `yyr1` 1,028 B + `yytranslate` 423 B = 16,165 B logical, padded to 19,968 B including inter-table alignment. The 48 KB function body plus tables brings the total Bison footprint to ≈68 KB.

### State Machine Walkthrough

The main parser loop (decompiled lines 1354--1507) follows the textbook Bison deterministic-LALR skeleton:

```
for ( state = 0; ; yypact_val = yypact[state] )  // line 1354
{
    if ( yypact_val == YYPACT_NINF )  goto yydefault;  // line 1357
    if ( lookahead == YYEMPTY )                        // line 1359
        lookahead = ptxlex();                          // line 1363 (sub_720F00)
    if ( lookahead <= 0 )      translated = 0;         // EOF
    else if ( lookahead > 422 ) translated = 2;         // YYUNDEF
    else                       translated = yytranslate[lookahead];  // line 1374
    idx = yypact_val + translated;                     // line 1375
    if ( idx > YYLAST )        goto yydefault;         // line 1376 (0x8DC)
    if ( yycheck[idx] != translated ) goto yydefault;  // line 1386
    // match: perform shift or reduce via yytable
    action = yytable[idx];                             // line 1497
    if ( action > 0 )                                  // shift
        push_state(action);
    else if ( action == 0 || action == YYTABLE_NINF )  // error
        goto yyerrlab;
    else
        reduce(-action);                               // negated = rule number
    continue;
  yydefault:
    action = yydefact[state];                          // line 1389
    if ( action != 0 ) reduce(action);
    else yyerrlab;
}
```

After a reduction the goto computation (lines 2392--2403) is:

```
nt_idx = yyr1[reduced_rule] - YYNTOKENS;  // line 2398 (- 193)
new_state = yypgoto[nt_idx] + current_state;
if ( new_state > YYLAST || yycheck[new_state] != current_state )
    new_state = yydefgoto[nt_idx];        // line 2403
else
    new_state = yytable[new_state];       // line 2401
push_state(new_state);
```

This is bit-for-bit identical to the Bison 3.x `yacc.c` skeleton (cf. Bison source tree `data/yacc.c`, functions `yybackup:` and `yynewstate:`).

### Conflict Count (Not Recoverable)

Bison's deterministic parser tables preserve only the *winning* action for every (state, terminal) pair; shift-reduce and reduce-reduce conflicts resolved at generation time (via `%left`/`%right`/`%nonassoc` precedence or Bison's default shift-wins rule) leave no trace in `yytable`/`yycheck`. The tables cannot distinguish "state had a single action" from "state had several actions and the generator picked one". We can therefore characterize only the **structural shape** of conflict-resolution, not its count:

- 708 / 1,099 states (≈64%) have `yypact[s] = YYPACT_NINF` (no shift action); these states unconditionally reduce via `yydefact[s]`.
- 255 states have `yypact[s] ≠ NINF` and `yydefact[s] = 0`: pure "shift-only" states, no default reduction.
- 136 states have both `yypact[s] ≠ NINF` and `yydefact[s] ≠ 0`: a shift set *and* a default reduction. This is the normal LALR configuration for states where the lookahead set partitions into "shiftable tokens → shift" and "everything else → reduce rule N". These 136 states are the **candidates** where a shift-reduce conflict could have been present in the source grammar and resolved by precedence, but the table shape alone does not prove a conflict occurred.
- 0 states have both `yypact[s] = NINF` and `yydefact[s] = 0` ⇒ no state is "dead"; every state has at least one action.

The absence of any runtime conflict-handling code (no `yyconfl`/`yyconflp` tables, no GLR dispatch, no `%glr-parser` scaffolding) confirms a clean LALR(1) generation with **zero runtime conflict-resolution logic**. Any conflicts were resolved at generation time and baked into the tables; the generator's statistics (e.g. `"N shift/reduce, M reduce/reduce"`) are gone. If an upper bound is needed for reimplementation purposes, 136 is a conservative overcount of the states that *could* have contained conflicts before resolution.

The parser is definitively not a GLR parser (no `%glr-parser` / `yyconfl` evidence), nor a push-pull API parser (the entry point at line 7490 takes parameters by value and returns a status code synchronously).

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

This is the largest single function in the front-end region at 93 KB (`disasm/sub_46E000_0x46e000.asm`, 17,842 lines, 1,464,386 bytes). It is not a normal function body but a massive initialization sequence that calls `sub_46BED0` exactly **1,141 times** -- once per legal PTX instruction variant. Every call registers one `(opcode name, operand encoding, type-suffix code, validator index)` tuple.

> **Reconstruction note.** `sub_46E000` is intentionally **not** present in `ptxas/decompiled/` for this build -- at 93 KB of straight-line calls it overflows the hand-decompilation budget. Everything in this section is reconstructed from three orthogonal sources that fully determine the answer: (1) the decompiled registration function `sub_46BED0_0x46bed0.c` (321 lines) which fixes the exact shape of each descriptor, (2) the decompiled matcher prologue `sub_46C6E0_0x46c6e0.c` (first 200 lines read for descriptor-field offsets and the 12-category operand classifier), and (3) the 1,080 `.weak .func` lowering targets in `ptxas/extracted/embedded_ptx_intrinsics.json` (`entry_count: 1080`, categorized as `cuda_other: 549, sm70_intrinsics: 433, sm20_math: 70, redux_sync: 17, sanitizer: 7, sm80_intrinsics: 4`) cross-referenced against the 322 SASS mnemonics in `ptxas/extracted/opcode_master.json` (`opcode_master.count: 322`) and the public PTX ISA 8.x reference. The per-family variant counts below were built by decoding the little-endian imm32 operands of every `mov REG, imm32; ...; call sub_46BED0` call site in `disasm/sub_46E000_0x46e000.asm` and reading the pooled C-string at that RVA from `.rodata`; the sum of per-family counts equals exactly 1,141, matching the static call count observed in the disassembly.

### 368-byte Descriptor Layout

Every registration allocates one descriptor at `sub_46BED0_0x46bed0.c:63` (`v18 = sub_424070(v15, 368)`). The layout below is recovered by mapping every field write in `sub_46BED0` to a byte offset, then cross-checking each offset against the reads in `sub_46C6E0` (the matcher) where the same descriptor is walked operand-by-operand. All offsets are cited back to the decompiled line where the write occurs.

| Offset | Width | Field | C expression (decompiled) | Source line | Purpose |
|---|---|---|---|---|---|
| +0 | 8  | `name` | `*v18 = a3` | `sub_46BED0:72` | Interned opcode-name pointer (hash key; pointer-equality is identity because names are pooled via `sub_4280C0`) |
| +8 | 4  | `sem_index` | `*((_DWORD*)v18 + 2) = a5` | `sub_46BED0:76` | Per-opcode semantic index passed to the validator (the `r8d` argument) |
| +12 | 16 | `validator_pair` | `_mm_loadu_si128(&a7)` -> `*(__m128i*)((char*)v18 + 12)` | `sub_46BED0:77,79` | Two 8-byte function pointers or flag words loaded as an SSE vector; the matcher dereferences the first slot as the descriptor's *accept* callback |
| +28 | 4  | `validator_flags` | `*((_DWORD*)v18 + 7) = a8` | `sub_46BED0:78` | Validator flag word (SM gating, ftz/sat legality, uniform-register allowance) |
| +32 | 4  | `token_count` | `*((_DWORD*)v18 + 8) = v12` | `sub_46BED0:73` | Count of non-space chars in the encoding string (= number of operand slots the descriptor expects) |
| +36 | 4 x 16 = 64 | `type_class[16]` | `*((_DWORD*)v18 + v21 + 9) = N` (or `+10`, depending on pre/post-increment branch) | `sub_46BED0:94,105,109,113,126,140,151,157,167,180,204` | Per-operand **type class** filled by the first switch (values: 1=F, 2=H, 3=N, 4=I, 5=B, 6=P, 7=O, 8=E, 9=T, 10=Q, 11=R). Slot *i* is read by the matcher around line 950 of `sub_46C6E0` as `v50[9+i]`. |
| +104 | 8 x 16 = 128 | `width_mask[16]` | `v18[v21+13] = sub_1CB0790()` / `v41[14] = v49` | `sub_46BED0:96-97,117,142,153,159,170,183,206` | Per-operand **width-set bitmask object** (opaque handle returned by `sub_1CB0790`; `sub_1CB0850(mask, w)` pushes a legal bit width). Defaults are added when the encoding char is followed by a token boundary rather than `[`. |
| +232 | 4  | `suffix_len` | `*((_DWORD*)v18 + 58) = v44` | `sub_46BED0:80` | Length of the `a4` type-suffix string (= number of populated slots in the two arrays below) |
| +236 | 4 x 16 = 64 | `type_subclass[16]` | `*((_DWORD*)v18 + v27 + 59) = K` | `sub_46BED0:243-307` | Per-operand **fine-grained type flavor** from the second switch (22 distinct class codes, tabulated in the "Type-Suffix Code String" subsection below) |
| +300 | 4 x 16 = 64 | `type_digit[16]` | `*((_DWORD*)v18 + v27 + 75) = v28-48` | `sub_46BED0:310` | Decimal digit payload for digit-tagged suffix chars (rounding mode, vector lane index, predicate-arg count) |
| +360 | 8  | `list_next` | `v18[45] = 0` | `sub_46BED0:66` | Intra-bucket linked-list pointer (next descriptor sharing this opcode name). Set to 0 at allocation; populated by `sub_42CA00` at line 319 when a second variant with the same name is inserted. |
| **368** |  | *(end)* |  |  | Total allocation size is exactly 368 bytes (`sub_424070(v15, 368)` at line 63). The memset at lines 68-71 zeroes everything except the first qword before the field writes begin. |

Notes on the layout:

- The `type_class[]` and `width_mask[]` arrays are indexed by `v21`, the running operand-slot counter (initialized to `-1` at line 67 so the first `++v21` writes slot 0). `v21` is bumped either pre-switch (`N`, `O`, `P`, `T`, `E` cases, which use `++v21`) or deferred into `v48` and committed at `LABEL_26` (`F`, `H`, `I`, `B`, `Q`, `R` cases, which allow a `[width|width|...]` group to follow). Both paths write the same logical slot, but the offsets differ in the decompiled source: `v18+v21+9` in the pre-increment path vs `v18+v21+10` in the deferred path -- that is because at the deferred write-site `v21` is still the *previous* operand's index, so the `+10` reaches one slot forward. Either way the class lands at `byte_offset = 36 + 4*slot`.
- The `width_mask[]` array holds **pointers** to bitmask objects, not bitmasks themselves. `sub_1CB0790()` allocates an empty mask; `sub_1CB0850(mask, w)` adds bit `w` to it. The matcher tests a candidate operand width against the mask via `sub_1CB06F0`/`sub_1CB0700` (not decompiled here; referenced from `sub_46C6E0`).
- The `type_subclass[]` / `type_digit[]` split lets one suffix character encode either a symbolic tag (upper- or lowercase letter, 22 distinct values) or a small integer (`0`..`9`). A digit-class slot has `type_subclass[i] = 0` and `type_digit[i] = digit`; the matcher looks at `type_digit[i]` only when `type_subclass[i] == 0`.
- The 16-slot cap on per-operand arrays is the hard upper bound on operand count for any PTX instruction: no registered opcode has more than 16 operands (the biggest real examples are `_mma.warpgroup` at 12 and `wmma.mma` at 10).

Each call passes four distinguishing arguments in the System V AMD64 calling convention (verified against the hand-decoded prologue in `sub_46BED0_0x46bed0.c` lines 4--12, which declares `a1..a8`):

| Register | Arg | Meaning | Used by `sub_46BED0` at |
|---|---|---|---|
| `rdi` | `a1` | Lexer state object pointer (the 2,528-byte struct at `parser_state+1096`) | `v29 = *(char**)(a1 + 2472)` for the hash insert (`sub_46BED0_0x46bed0.c:317`) |
| `rsi` | `a2` | Operand **encoding string** (`a2->__size` is iterated byte-by-byte) | Outer `while` loop at lines 54--232 |
| `rdx` | `a3` | Opcode **name string** (interned) | Stored at `*v18 = a3` (line 72) and used as the hash key at line 318 |
| `rcx` | `a4` | Type-**suffix** code string (per-operand type-class bits) | `v44 = strlen(a4)`, second loop at lines 234--316 |
| `r8d` | `a5` | Numeric tag stored at `*((_DWORD *)v18 + 2)` (line 76) -- a per-opcode semantic index | Not processed; just stored |
| `xmm0` / `[rsp+00h]` | `a7` | 16-byte "validator vector" (function pointers / flag words), stored via `_mm_loadu_si128` at `*(__m128i *)(v18 + 12)` (line 79) | Opaque payload |
| `[rsp+08h]` | `a8` | Validator index, stored at `*((_DWORD *)v18 + 7)` (line 78) | Opaque payload |

Concretely, an `add.f32` registration looks like this in the raw disassembly (addresses from `disasm/sub_46E000_0x46e000.asm`):

```
0x472fe9: b9 13 92 ce 01   mov     ecx, (offset a10000+3); "000"     ; a4 = type suffixes "000"
0x472fee: ba 39 81 d0 01   mov     edx, offset aAdd_0; "add"          ; a3 = opcode name "add"
0x472ff3: be 35 b8 02 02   mov     esi, (offset aNanNotAllowedW+18h); "F32"  ; a2 = encoding "F32"
0x472ff8: 48 89 df         mov     rdi, rbx                           ; a1 = lexer state
0x472ffb: 41 b8 2f 00 00 00 mov     r8d, 2Fh                          ; a5 = 47  (semantic index)
        ... xmm0 / stack-slot setup for a7/a8 ...
0x47302d: e8 9e 8e ff ff   call    sub_46BED0
```

The string "F32" is itself a substring of a longer pooled string `"NaN not allowed with W..."` -- IDA represents it as `(offset aNanNotAllowedW+18h)` because ptxas deduplicates its read-only strings aggressively, so a 3-character suffix can be reused across many callers. IDA also truncates long symbol comments: names such as `_tcgen05.guardrails.sp_consistency_across_idesc_mod_scale` appear with `"..."` in the comment, but the full string is recoverable by decoding the little-endian imm32 in the `mov edx, imm32` instruction bytes and reading the null-terminated C-string at that address from `.rodata` (range `0x1CE2E00..0x240BF90`). The catalog below was built by exactly that procedure.

### Operand Encoding Alphabet

The encoding string is consumed character-by-character by the first `switch` in `sub_46BED0` (lines 90--229). The original wiki listed six codes; the decompiled switch actually handles **eleven** type-class codes plus structural punctuation:

| Code | Type class | `sub_46BED0` case | Value written at `*((_DWORD*)v18 + v21 + 10)` | Default widths added when no `[..\|..]` follows |
|---|---|---|---|---|
| `F` | Float | line 107 | 1 | {32, 64} (via two `sub_1CB0850(mask, …)` calls) |
| `H` | Half / packed half (`.f16`, `.f16x2`, `.bf16`) | line 111 | 2 | {32, 64} |
| `I` | Signed / unsigned integer | line 124 | 4 | {16, 32, 64} |
| `B` | Bitwise / typeless | line 92 | 5 | {1, 16, 32, 64} (the extra `1` is `.b1` used by 1-bit matrix ops) |
| `N` | Numeric literal / immediate | line 139 | 3 | {32} (or 0 when a digit follows before a boundary) |
| `P` | Predicate | line 156 | 6 | {32} |
| `E` | "Extended" / narrow FP (`.bf16`, `.tf32`, `.e4m3`, `.e5m2`) | line 103 | 8 | none (widths always explicit) |
| `O` | Opaque typed handle (`.texref`, `.surfref`, `.samplerref`) | line 150 | 7 | none |
| `Q` | Quarter / sub-byte matrix (`.s4`, `.u4`, `.s8`-in-matrix) | line 165 | 10 | {8, 16, 32} |
| `R` | Register class with small default set | line 178 | 11 | {4, 8, 16} |
| `T` | Tensor-float (tf32 accumulator scalar) | line 202 | 9 | none |

The loop accumulates **decimal width digits** into `v46` at line 217 (`v46 = v26 + 10*v46 - 48`), then commits `v46` to the current operand's width mask on `|`, `]`, or boundary via `sub_1CB0850(descriptor, width)` (line 223). Structural punctuation:

- `[` opens a width-set group (line 210, `continue`, no state change)
- `|` commits the pending digit as an additional legal width (`LABEL_12` at line 220)
- `]` commits the final width and closes the group (same `LABEL_12`)

So `B[8|16|32|64]` is parsed as: open group, push width 8, push width 16, push width 32, push width 64, close. The result is a B-class descriptor whose "allowed widths" mask contains exactly those four bits. `F[32|64]` restates the default set explicitly.

Encoding-string first-character frequencies observed across all 1,141 registrations:

| First char | Count | Mnemonic |
|---|---|---|
| `F` | 454 | float |
| `I` | 254 | integer |
| `B` | 118 | bitwise |
| `H` | 77  | half |
| `E` | 59  | extended FP (bf16/tf32/e4m3/e5m2) |
| `P` | 21  | predicate |
| `Q` | 6   | sub-byte matrix |
| `R` | 4   | register class (default {4,8,16}) |
| `N` | 3   | numeric immediate |
| `T` | 1   | tf32 tensor (`cvt.tf32.f32`) |
| `O` | 1   | opaque handle (`istypep` only) |

The remaining 143 of the 1,141 registrations pass an **empty string** for the encoding (`""`). These are the control-flow, barrier, async-bulk, and `tcgen05.mma` opcodes that take no typed-operand sequence -- the encoding is determined entirely by state-space modifiers on the opcode itself, checked by the semantic validator rather than by descriptor-list matching.

### Type-Suffix Code String (`a4`)

The second string argument drives the loop at lines 234--316 of `sub_46BED0_0x46bed0.c`. Each character maps the *i*-th **operand's** fine-grained type flavor into `*((_DWORD *)v18 + v27 + 59)` (the descriptor's per-operand type-subclass array, sixteen DWORDs starting at byte offset +236):

| Char | Stored value | Char | Stored value | Char | Stored value |
|---|---|---|---|---|---|
| `A` | 20 | `M` | 17 | `b` | 8  |
| `C` | 13 | `P` | 15 | `c` | 9  |
| `D` | 14 | `Q` | 16 | `d` | 10 |
| `L` | 22 | `S` | 18 | `e` | 11 |
| `T` | 19 | `U` | 3  | `f` | 5  |
| `V` | 21 |    |   | `h` | 6  |
|    |   |    |   | `i` | 12 |
|    |   |    |   | `l` | 7  |
|    |   |    |   | `s` | 4  |
|    |   |    |   | `u` | 2  |
|    |   |    |   | `x` | 1  |

Digit characters (`0`--`9`) write `0` to the class slot and the digit value to `*((_DWORD *)v18 + v27 + 75)` (default case at line 309). Common suffix strings from the catalog:

- `"000"` (49 full-string matches, 877 `'0'` characters in total) -- "three operands, all default type class" (e.g. scalar `add`, `sub`, `min`, `max`)
- `"0000"` -- four operands all default (e.g. `fma`, `mad`)
- `"M0"` / `"0M"` -- memory operand paired with a typed register (e.g. `ld`, `st`)
- `"hhhhdC"`, `"fhhddC"`, `"sddsdC"` -- the MMA per-operand tag sequence (h=half, f=float32, s=int, d=int32 accumulator, C=`.cc` flag)
- `"0i1"` / `"0i1s"` -- texture sequence (sampler index, texref, coord, optional shadow)

### Two Hash Tables: +2472 and +2480

`sub_46E000` populates two separate hash tables on the lexer state object, both allocated by back-to-back `sub_425CA0` calls in the prologue (disasm lines 8--24). The two tables correspond to different **string namespaces**:

- `lexer_state+2472` (offset 0x9A8) -- the **primary** opcode table (128 buckets, `sub_425CA0(sub_427630, sub_4277B0, 0x80)`); every user-visible PTX opcode lives here.
- `lexer_state+2480` (offset 0x9B0) -- a smaller 16-bucket table (`sub_425CA0(…, 0x10)`) used by the matcher as a secondary probe.

`sub_46BED0` always inserts into `+2472` (line 317: `v29 = *(char**)(a1 + 2472)`). The matcher side (`sub_46C690_0x46c690.c:11`) probes `+2472` first, then falls back to `+2480`; `sub_46C6E0:258` adds a special-case fast path that probes **only** `+2480` when the opcode starts with ASCII `_` (byte value 95). That fast path is used for the 18 `_`-prefixed **compiler-private** pseudo-opcodes (`_mma`, `_mma.warpgroup`, `_ldsm`, `_warpgroup.*`, `_tcgen05.guardrails.*`, `_movm`, `_gen_proto`, `_jcall`, `_match`, `_checkfp.divide`, `_sulea.*`, `_createpolicy.*`, `_ldldu`, `_warpsync`), which are emitted by earlier passes and are not part of the public PTX ISA. The `+2480` table is therefore the **internal-opcodes** index and is populated by code paths inside `sub_46E000` that are *not* visible as `sub_46BED0` calls (they use `sub_425CA0` infrastructure directly); identifying those sites is a follow-up item.

### Registration Function -- `sub_46BED0` Pseudocode

```c
// Reimplementation derived from sub_46BED0_0x46bed0.c, lines 4--321.
//   a1 : LexerState*  — primary hash table at a1+2472
//   a2 : const char*  — encoding string (iterated by first switch)
//   a3 : const char*  — opcode name (interned; becomes hash key)
//   a4 : const char*  — type suffix string (iterated by second switch)
//   a5 : int          — semantic index / validator selector (stored at +8)
//   a6 : __int64      — extra payload (stack arg, unused in hot path)
//   a7 : __m128i      — 16-byte validator function pointer pair (stored at +12)
//   a8 : int          — validator flags (stored at +28)
char *register_opcode(LexerState *a1, const char *a2, const char *a3,
                      const char *a4, int a5, __int64 a6, __m128i a7, int a8)
{
    // Count "tokens" in the encoding string: every non-space char advances v12.
    int token_count = 0;
    for (const char *p = a2; *p; p++)
        token_count += ((*__ctype_b_loc())[*p] & 0x400) ? 0 : 1;   // 0x400 = _ISspace

    // Allocate a 368-byte descriptor from the compilation pool (line 63).
    Pool *pool = /* pool handle cached behind sub_4280C0 */;
    Descriptor *d = pool_alloc(pool, 368);             // sub_424070
    memset(d, 0, 368);
    d->name       = a3;                                // *v18 = a3                (line 72)
    d->token_cnt  = token_count;                       // ((int*)d)[8]              (line 73)
    d->sem_index  = a5;                                // ((int*)d)[2]              (line 76)
    d->validator  = a7;                                // 16-byte pair at byte +12  (line 79)
    d->flags      = a8;                                // ((int*)d)[7]              (line 78)
    d->suffix_len = strlen(a4);                        // ((int*)d)[58]             (line 80)

    // --- Parse encoding string a2 into per-operand (type_class, width_mask) tuples
    int     operand_idx    = -1;
    unsigned pending_digits = 0;
    for (size_t j = 0, n = strlen(a2); j < n; /* advanced inside */) {
        char c = a2[j++];
        switch (c) {
        case 'F':   ((int*)d)[++operand_idx + 9] = 1;  // float                  (line 107)
                    d->width_mask[operand_idx] = mask_new();
                    if (end_of_token(a2, j)) { mask_add(..., 32); mask_add(..., 64); }
                    break;
        case 'H':   /* class=2, defaults {32,64} */           break;    // line 111
        case 'I':   ((int*)d)[++operand_idx + 9] = 4;  // integer                (line 124)
                    d->width_mask[operand_idx] = mask_new();
                    if (end_of_token(a2, j)) { mask_add(..., 16); mask_add(..., 32); mask_add(..., 64); }
                    break;
        case 'B':   /* class=5, defaults {1,16,32,64}   */   break;    // line 92
        case 'N':   /* class=3, defaults {32}            */  break;    // line 139
        case 'P':   /* class=6, defaults {32}            */  break;    // line 156
        case 'E':   /* class=8, widths always explicit   */  break;    // line 103
        case 'O':   /* class=7, no width                 */  break;    // line 150
        case 'Q':   /* class=10, defaults {8,16,32}      */  break;    // line 165
        case 'R':   /* class=11, defaults {4,8,16}       */  break;    // line 178
        case 'T':   /* class=9, no width                 */  break;    // line 202
        case '[':   continue;                                          // line 210
        case ']':
        case '|':   goto commit_digit;                                 // line 213
        default:
            if ((unsigned)(c - '0') <= 9u) {                           // line 215
                pending_digits = pending_digits * 10 + (c - '0');
                if (!end_of_token(a2, j)) continue;
commit_digit:
                if (operand_idx < 0) operand_idx = 0;
                mask_add(d->width_mask[operand_idx], pending_digits);   // sub_1CB0850 (line 223)
                pending_digits = 0;
            }
            break;
        }
    }

    // --- Parse suffix string a4 into per-operand type-subclass array (lines 234--316)
    for (size_t k = 0; k < d->suffix_len; k++) {
        switch (a4[k]) {
        case 'A': d->type_sub[k] = 20; break;  case 'b': d->type_sub[k] = 8;  break;
        case 'C': d->type_sub[k] = 13; break;  case 'c': d->type_sub[k] = 9;  break;
        case 'D': d->type_sub[k] = 14; break;  case 'd': d->type_sub[k] = 10; break;
        case 'L': d->type_sub[k] = 22; break;  case 'e': d->type_sub[k] = 11; break;
        case 'M': d->type_sub[k] = 17; break;  case 'f': d->type_sub[k] = 5;  break;
        case 'P': d->type_sub[k] = 15; break;  case 'h': d->type_sub[k] = 6;  break;
        case 'Q': d->type_sub[k] = 16; break;  case 'i': d->type_sub[k] = 12; break;
        case 'S': d->type_sub[k] = 18; break;  case 'l': d->type_sub[k] = 7;  break;
        case 'T': d->type_sub[k] = 19; break;  case 's': d->type_sub[k] = 4;  break;
        case 'U': d->type_sub[k] = 3;  break;  case 'u': d->type_sub[k] = 2;  break;
        case 'V': d->type_sub[k] = 21; break;  case 'x': d->type_sub[k] = 1;  break;
        default:                               // digit                     (line 309)
            d->type_sub[k]        = 0;
            d->type_sub_digit[k]  = a4[k] - '0';
            break;
        }
    }

    // --- Append into the hash bucket at LexerState+2472 ---
    HashTable *tbl = *(HashTable**)((char*)a1 + 2472);
    HashBucket *bucket = sub_426D60(tbl, d->name);       // bucket lookup (line 318)
    sub_42CA00(d, bucket);                               // link into bucket list (line 319)
    return sub_426150(tbl, d->name, d);                  // commit (line 320)
}
```

The hash insert uses `d->name` (the interned opcode pointer, deduped across the entire binary) as the key. Because PTX opcode names are pooled, the string pointer equality *is* the hash key equality -- no `strcmp` is needed on insert or lookup.

### Catalog -- 1,141 Registrations by PTX Family

The tables below give **every** registered opcode, with its variant count and the distinct operand encoding strings it accepts. Methodology: all 1,141 `mov edx, <opcode>; mov esi, <encoding>; mov ecx, <suffix>; ...; call sub_46BED0` call sites were extracted from `disasm/sub_46E000_0x46e000.asm` by decoding the little-endian imm32 in each `mov REG, imm32` instruction's byte stream and reading the null-terminated C-string at that address from `ptxas_rodata.bin`. This bypasses IDA symbol-comment truncation (`"_tcgen05.guardrails.sp_consistency_acro"...`) and substring-reference opacity (`1D07286h` -> inner offset of the pooled string `"F32F16"` -> the 3-byte substring `"F16"`).

Totals by category:

| Category | Distinct opcodes | Total variants |
|---|---|---|
| Arithmetic (add/sub/mul/mad/fma/div/rem/abs/neg/min/max/sad/dp2a/dp4a/copysign/...) | 32 | 96 |
| Comparison and select (`set`, `setp`, `selp`, `slct`, `testp`) | 5 | 80 |
| Logic and bitwise (`and`, `or`, `xor`, `not`, `shl`, `shr`, `shf`, `lop3`, `cnot`) | 10 | 16 |
| Bit manipulation (`popc`, `clz`, `brev`, `bfe`, `bfi`, `bfind`, `prmt`, `bmsk`, `szext`, `fns`) | 10 | 10 |
| Transcendental (`rcp`, `sqrt`, `rsqrt`, `sin`, `cos`, `lg2`, `ex2`, `tanh`) | 8 | 19 |
| Data movement (`mov`, `cvt`, `cvt.pack`, `cvta`, `cvta.to`, `isspacep`, `_movm`, `movmatrix`) | 8 | 44 |
| Memory load/store (`ld`, `st`, `ldu`, `ldmatrix`, `stmatrix`, `prefetch`, `alloca`, `cctl`, `createpolicy`, ...) | 23 | 55 |
| Atomic / reduction (`atom`, `red`, `red.async`) | 3 | 41 |
| Barrier / fence / mbarrier (`bar*`, `barrier*`, `membar*`, `fence*`, `mbarrier.*`, `setmaxnreg.*`, `nanosleep`) | 35 | 56 |
| Control flow (`bra`, `brx.idx`, `call`, `ret`, `exit`, `trap`, `brkpt`, `_jcall`, `_gen_proto`) | 9 | 17 |
| Texture / surface (`tex`, `tex.base`, `tex.level`, `tex.grad`, `tld4`, `txq*`, `sust.*`, `sured.*`, `_sulea.*`, `suq`, `suld.b`) | 15 | 222 |
| Warp-level collectives (`shfl`, `vote`, `redux`, `match`, `activemask`, `elect`, `pmevent*`, `getctarank`, `_warpsync`, `_match`) | 11 | 17 |
| Cooperative copy / tensor memory (`cp.async*`, `cp.reduce.async*`, `multimem.*`, `clusterlaunchcontrol.*`, `tensormap.*`, `griddepcontrol`) | 21 | 75 |
| Tensor cores -- MMA / wgmma / wmma / tcgen05 / `_mma.warpgroup` / `_ldsm` / `_tcgen05.guardrails.*` | 37 | **360** |
| Video SIMD (`v*`, `v*2`, `v*4`) | 23 | 31 |
| Verification / debug (`_checkfp.divide`, `istypep`) | 2 | 2 |
| **Total** | **252** | **1,141** |

The three dominant families -- **tensor cores (360 = 31.6%)**, **texture (222 = 19.5%)**, and **arithmetic (96 = 8.4%)** -- account for 60% of all registrations. The tensor-core bloat comes almost entirely from `_mma.warpgroup` (135 variants) and the public `_mma`/`mma` families (41+38). Texture bloat is structural: each of `tex`, `tex.base`, `tex.level`, `tex.grad` registers the identical **8-encoding x 6-mode** cross-product = 48 variants, giving 192 before adding `tld4`, `txq`, `sust`, `sured`, `_sulea`, `suq`, `suld.b`.

#### Arithmetic (96 variants / 32 opcodes)

| PTX opcode | Variants | Distinct encodings | Sample encoding strings |
|---|---|---|---|
| `abs` | 7 | 7 | `F16`, `H32`, `E16`, `E32`, `F32`, `F64`, `I` |
| `add` | 11 | 11 | `F16`, `H32`, `F32`, `F64`, `I`, `E16`, `E32`, `N32`, `H64`, `F32F16`, `F32E16` |
| `addc` | 1 | 1 | `I` |
| `copysign` | 1 | 1 | `F[32\|64]` |
| `div` | 3 | 3 | `F32`, `F64`, `I` |
| `div.full` | 1 | 1 | `F32` |
| `dp2a` | 1 | 1 | `I32I32` |
| `dp2a.hi` | 1 | 1 | `I32I32` |
| `dp2a.lo` | 1 | 1 | `I32I32` |
| `dp4a` | 1 | 1 | `I32I32` |
| `fma` | 9 | 9 | `F16`, `H32`, `F32`, `F64`, `E16`, `E32`, `H64`, `F32F16`, `F32E16` |
| `mad` | 2 | 2 | `F32`, `F64` |
| `mad.hi` | 1 | 1 | `I` |
| `mad.lo` | 1 | 1 | `I` |
| `mad.wide` | 1 | 1 | `I[16\|32]` |
| `mad24.hi` | 1 | 1 | `I32` |
| `mad24.lo` | 1 | 1 | `I32` |
| `madc.hi` | 1 | 1 | `I` |
| `madc.lo` | 1 | 1 | `I` |
| `max` | 9 | 8 | `F16`, `H32`, `E16`, `E32`, `F32`, `F64`, `I`, `N32` |
| `min` | 9 | 8 | `F16`, `H32`, `E16`, `E32`, `F32`, `F64`, `I`, `N32` |
| `mul` | 7 | 7 | `F16`, `H32`, `F32`, `F64`, `E16`, `E32`, `H64` |
| `mul.hi` | 1 | 1 | `I` |
| `mul.lo` | 1 | 1 | `I` |
| `mul.wide` | 1 | 1 | `I[16\|32]` |
| `mul24.hi` | 1 | 1 | `I32` |
| `mul24.lo` | 1 | 1 | `I32` |
| `neg` | 7 | 7 | `F16`, `H32`, `E16`, `E32`, `F32`, `F64`, `I` |
| `rem` | 1 | 1 | `I` |
| `sad` | 1 | 1 | `I` |
| `sub` | 10 | 10 | `F16`, `H32`, `F32`, `F64`, `I`, `E16`, `E32`, `H64`, `F32F16`, `F32E16` |
| `subc` | 1 | 1 | `I` |

Full expansion for `add` (all 11 variants with suffix codes):

| # | Opcode | Encoding | Suffix | Meaning |
|---|---|---|---|---|
| 0 | `add` | `F16` | `000` | `add.f16` / `add.f16x2` |
| 1 | `add` | `H32` | `000` | `add.bf16` / `add.bf16x2` |
| 2 | `add` | `F32` | `000` | `add.f32` |
| 3 | `add` | `F64` | `000` | `add.f64` |
| 4 | `add` | `I`   | `000` | integer `add.s{16,32,64}` / `add.u{16,32,64}` |
| 5 | `add` | `E16` | `xxx` | extended-FP form (each `x` tags packed bf16/f16) |
| 6 | `add` | `E32` | `ddd` | extended-FP form (tf32, each `d` tags 64-bit accumulator) |
| 7 | `add` | `N32` | `000` | numeric-literal mixed form |
| 8 | `add` | `H64` | `000` | 64-bit half-packed form |
| 9 | `add` | `F32F16` | `010` | mixed `.f32 = .f32 + .f16` (SM 100 down-cast form) |
| 10 | `add` | `F32E16` | `0x0` | mixed `.f32 = .f32 + .bf16` |

And the full `fma` expansion (9 variants):

| # | Encoding | Suffix | Meaning |
|---|---|---|---|
| 0 | `F16` | `0000` | `fma.rn.f16` / `fma.rn.f16x2` |
| 1 | `H32` | `0000` | `fma.rn.bf16` |
| 2 | `F32` | `0000` | `fma.rn.f32` |
| 3 | `F64` | `0000` | `fma.rn.f64` |
| 4 | `E16` | `xxxx` | extended-FP quad form (bf16/f16) |
| 5 | `E32` | `dddd` | extended-FP quad form (tf32) |
| 6 | `H64` | `0000` | 64-bit half-packed FMA |
| 7 | `F32F16` | `0110` | mixed `f32 += f16 * f16` |
| 8 | `F32E16` | `0xx0` | mixed `f32 += bf16 * bf16` |

#### Comparison and select (80 variants / 5 opcodes)

| PTX opcode | Variants | Distinct encodings | Sample encoding strings |
|---|---|---|---|
| `selp` | 3 | 3 | `F`, `I`, `B` |
| `set` | 54 | 27 | `F16F16`, `I16F16`, `I32F16`, `I32H32`, `F16F32`, `F16F64`, +21 more |
| `setp` | 16 | 8 | `F16`, `H32`, `F32`, `F64`, `I`, `B`, `E16`, `E32` |
| `slct` | 6 | 6 | `FF32`, `IF32`, `BF32`, `FI32`, `II32`, `BI32` |
| `testp` | 1 | 1 | `F[32\|64]` |

`set` blows up to 54 variants because it registers every `setp`-to-data type pair, i.e. it emits an integer/bitwise destination rather than a predicate. All 16 `setp` variants come from a (6 type classes) x (with/without predicate-merge) cross product plus two forms for `E16`/`E32`. Full `setp` expansion:

| # | Encoding | Suffix | Form |
|---|---|---|---|
| 0..5  | `F16`, `H32`, `F32`, `F64`, `I`, `B` | `P00`  | `setp.cmp.type dst, a, b` |
| 6..11 | `F16`, `H32`, `F32`, `F64`, `I`, `B` | `P00P` | `setp.cmp.and/or/xor.type dst, a, b, !p` |
| 12 | `E16` | `Pxx`  | extended bf16/f16 compare |
| 13 | `E16` | `PxxP` | extended bf16/f16 compare with predicate merge |
| 14 | `E32` | `Pdd`  | tf32 compare |
| 15 | `E32` | `PddP` | tf32 compare with predicate merge |

#### Logic and bitwise (16 variants / 10 opcodes)

| PTX opcode | Variants | Distinct encodings | Sample encoding strings |
|---|---|---|---|
| `and` | 2 | 2 | `B`, `P` |
| `cnot` | 1 | 1 | `B` |
| `lop3` | 2 | 1 | `B32` |
| `not` | 2 | 2 | `B`, `P` |
| `or` | 2 | 2 | `B`, `P` |
| `shf.l` | 1 | 1 | `B32` |
| `shf.r` | 1 | 1 | `B32` |
| `shl` | 1 | 1 | `B` |
| `shr` | 2 | 2 | `I`, `B` |
| `xor` | 2 | 2 | `B`, `P` |

#### Bit manipulation (10 variants / 10 opcodes, one apiece)

| PTX opcode | Encoding | | PTX opcode | Encoding |
|---|---|---|---|---|
| `bfe`   | `I[32\|64]` | | `clz`   | `B[32\|64]` |
| `bfi`   | `B[32\|64]` | | `fns`   | `B32` |
| `bfind` | `I[32\|64]` | | `popc`  | `B[32\|64]` |
| `bmsk`  | `B32`       | | `prmt`  | `B32` |
| `brev`  | `B[32\|64]` | | `szext` | `I32` |

#### Transcendental (19 variants / 8 opcodes)

| PTX opcode | Variants | Encodings |
|---|---|---|
| `cos`   | 1 | `F32` |
| `ex2`   | 5 | `F16`, `H32`, `F32`, `E16`, `E32` |
| `lg2`   | 1 | `F32` |
| `rcp`   | 2 | `F32`, `F64` |
| `rsqrt` | 2 | `F32`, `F64` |
| `sin`   | 1 | `F32` |
| `sqrt`  | 2 | `F32`, `F64` |
| `tanh`  | 5 | `F16`, `F32`, `H32`, `E16`, `E32` |

Note: `rcp.approx.ftz.f32`, `rcp.approx.f64`, etc. are **not** separate registrations. Approximation mode, ftz, saturation, and rounding mode are all carried by modifier tokens on the opcode and rejected by the semantic validator rather than by a per-mode descriptor. This is why `rcp` has only 2 variants despite several user-visible modes.

#### Data movement (44 variants / 8 opcodes)

| PTX opcode | Variants | Distinct encodings | Sample encoding strings |
|---|---|---|---|
| `_movm` | 3 | 3 | `B16`, `I8I4`, `I4I2` |
| `cvt` | 28 | 26 | `F16F32`, `H32F32`, `E16F32`, `E32F32`, `F32E16`, `F[16\|32\|64]F[16\|32\|64]`, +20 more |
| `cvt.pack` | 3 | 3 | `I8I32B32`, `I16I32`, `I[2\|4]I32B32` |
| `cvta` | 1 | 1 | `I[32\|64]` |
| `cvta.to` | 1 | 1 | `I[32\|64]` |
| `isspacep` | 2 | 1 | *(empty)* |
| `mov` | 5 | 5 | `F`, `I`, `B`, `P`, `B128` |
| `movmatrix` | 1 | 1 | `B16` |

`cvt` has the most per-opcode diversity of any family: 28 type-pair variants. Full list:

| # | Encoding | Meaning |
|---|---|---|
| 0 | `F16F32` | `cvt.f16.f32` |
| 1 | `H32F32` | `cvt.bf16.f32` / `cvt.bf16x2.f32.f32` |
| 2 | `E16F32` | `cvt.e4m3/e5m2/bf16.f32` |
| 3 | `E32F32` | `cvt.tf32.f32` |
| 4 | `F32E16` | inverse extended-FP upconvert |
| 5 | `F[16\|32\|64]F[16\|32\|64]` | generic float-to-float |
| 6 | `F[16\|32\|64]I[8\|16\|32\|64]` | integer-to-float |
| 7 | `I[8\|16\|32\|64]F[16\|32\|64]` | float-to-integer |
| 8 | `I[8\|16\|32\|64]I[8\|16\|32\|64]` | integer-to-integer |
| 9 | `Q16F32` | `cvt.s4x2/u4x2/s8x2/u8x2.f32` packed |
| 10 | `Q16H32` | packed int-from-bf16 |
| 11 | `H32Q16` | inverse |
| 12 | `T32F32` | tf32 from f32 |
| 13 | `E16E16` | extended-to-extended |
| 14 | `F[16\|64]E16` | upconvert from extended |
| 15 | `E16F[16\|64]` | downconvert to extended |
| 16 | `E16I[8\|16\|32\|64]` | int-to-extended |
| 17 | `I[8\|16\|32\|64]E16` | extended-to-int |
| 18 | `R8F32` | f32-to-4bit packed (register class R) |
| 19 | `H32R8` | 4bit-to-bf16 |
| 20 | `E32Q16` | tf32-to-quad-int |
| 21 | `Q16E32` | quad-int-to-tf32 |
| 22-23 | `H32F32`, `E32F32` | relaxed forms with `011d`/`d11d` suffixes (rounding-mode variants) |
| 24 | `Q32F32` | quad 32-bit-to-f32 |
| 25 | `R16F32` | f32-to-packed 4x4 |
| 26 | `R8H32` | bf16-to-4bit packed |
| 27 | `R8E32` | tf32-to-4bit packed |

`cvt.pack` adds 3 more for `cvt.pack.sat.<T>.<Tsrc>` widening-pack forms.

#### Memory load/store (55 variants / 23 opcodes)

| PTX opcode | Variants | Distinct encodings | Sample encoding strings |
|---|---|---|---|
| `_createpolicy.fractional` | 1 | 1 | `B64` |
| `_createpolicy.range` | 1 | 1 | `B64` |
| `_ldldu` | 1 | 1 | `B[8\|16\|32\|64\|128]B[8\|16\|32\|64\|128]` |
| `alloca` | 2 | 1 | `I[32\|64]` |
| `applypriority` | 1 | 1 | *(empty)* |
| `cctl` | 2 | 1 | *(empty)* |
| `cctlu` | 2 | 1 | *(empty)* |
| `createpolicy.cvt` | 1 | 1 | `B64` |
| `createpolicy.fractional` | 2 | 1 | `B64` |
| `createpolicy.range` | 1 | 1 | `B64` |
| `discard` | 1 | 1 | *(empty)* |
| `ld` | 12 | 4 | `F`, `I[8\|16\|32\|64]`, `B[8\|16\|32\|64]`, `B128` |
| `ldmatrix` | 2 | 2 | `B[8\|16]`, *(empty)* |
| `ldu` | 4 | 4 | `B128`, `F`, `I[8\|16\|32\|64]`, `B[8\|16\|32\|64]` |
| `mapa` | 1 | 1 | `I[32\|64]` |
| `prefetch` | 1 | 1 | *(empty)* |
| `prefetchu` | 1 | 1 | *(empty)* |
| `st` | 8 | 4 | `F`, `I[8\|16\|32\|64]`, `B[8\|16\|32\|64]`, `B128` |
| `st.async` | 6 | 5 | `I[32\|64]`, `B[32\|64]`, `F[32\|64]`, `I[8\|16\|32\|64]`, `B[8\|16\|32\|64]` |
| `st.bulk` | 2 | 1 | *(empty)* |
| `stackrestore` | 1 | 1 | `I[32\|64]` |
| `stacksave` | 1 | 1 | `I[32\|64]` |
| `stmatrix` | 1 | 1 | `B[8\|16]` |

`ld` has 12 variants = 4 encodings x 3 suffix-string shapes (regular, vector-packed, `.U`-suffix uniform). `B128` appears three times with different suffix flags (`0M`, `0M`, `0MU`) to separate non-vector 128-bit loads, vector-packed 128-bit loads, and uniform 128-bit loads.

#### Atomic / reduction (41 variants / 3 opcodes)

| PTX opcode | Variants | Distinct encodings |
|---|---|---|
| `atom`      | 21 | 10 (`F32`, `H32`, `F64`, `I[32\|64]`, `B[32\|64]`, `B128`, `F16`, `B16`, `E16`, `E32`) |
| `red`       | 16 | 8  (same set minus `B16` and `B128`) |
| `red.async` | 4  | 2  (`I[32\|64]`, `B[32\|64]`) |

The 21 `atom` variants span 10 encodings x up to 3 suffix variants each. The suffix tail `"0M0U"` marks forms where a `.uni` (uniform) post-modifier is legal. `atom` with `B128` appears three times: `atom.cas.b128`, vector-packed `B128`, and the uniform form.

#### Barrier / fence / mbarrier (56 variants / 35 opcodes)

| PTX opcode | Variants | Encoding |
|---|---|---|
| `bar`, `barrier` (plain) | 2 each | *(empty)* |
| `bar.arrive`, `barrier.arrive` | 1 each | *(empty)* |
| `bar.cta`, `barrier.cta` (plain) | 2 each | *(empty)* |
| `bar.cta.arrive`, `barrier.cta.arrive` | 1 each | *(empty)* |
| `bar.cta.red`, `bar.red`, `barrier.cta.red`, `barrier.red` | 4 each (16 total) | `I32`, `P` |
| `bar.warp` | 1 | *(empty)* |
| `barrier.cluster.arrive`, `barrier.cluster.wait` | 1 each | `P` |
| `fence` | 1 | `P` |
| `fence.proxy` | 2 | `P`, *(empty)* |
| `membar` | 1 | `P` |
| `membar.proxy` | 1 | *(empty)* |
| `mbarrier.arrive`, `mbarrier.arrive_drop`, `mbarrier.try_wait`, `mbarrier.try_wait.parity` | 2 each | `B64` |
| `mbarrier.*` (nine other variants) | 1 each | `B64` |
| `setmaxnreg.dec`, `setmaxnreg.inc` | 1 each | `I32` |
| `nanosleep` | 1 | `I32` |

The `bar.red`/`barrier.red` fourfold expansion is (`I32` integer arg, `P` predicate arg) x (cta, non-cta scope). The 13 distinct `mbarrier.*` opcodes (init, inval, complete_tx, expect_tx, pending_count, test_wait, test_wait.parity, try_wait, try_wait.parity, arrive, arrive.expect_tx, arrive_drop, arrive_drop.expect_tx) sum to 17 total variants because 4 of them register both "with-count" and "without-count" forms.

#### Control flow (17 variants / 9 opcodes)

| PTX opcode | Variants | Encoding | Notes |
|---|---|---|---|
| `_gen_proto` | 1 | *(empty)* | Internal prototype-generation pseudo-op |
| `_jcall` | 1 | *(empty)* | Internal indirect-call pseudo-op |
| `bra` | 2 | *(empty)* | Unconditional and predicated |
| `brkpt` | 1 | *(empty)* | |
| `brx.idx` | 1 | *(empty)* | Indexed branch |
| `call` | **8** | *(empty)* | (prototype vs no-prototype) x (return vs no-return) x (uniform vs divergent) |
| `exit` | 1 | *(empty)* | |
| `ret` | 1 | *(empty)* | |
| `trap` | 1 | *(empty)* | |

`call` registering 8 variants is a surprise: each one corresponds to a different argument-list **shape** that the Bison grammar produces (`call (retvals), fn, (args);` versus `call.uni`, prototype reference, no prototype, direct target, indirect target, etc.). They all share the empty encoding string because the arguments are validated semantically after parsing, not through the descriptor type matcher.

#### Texture / surface (222 variants / 15 opcodes)

| PTX opcode | Variants | Distinct encodings |
|---|---|---|
| `_sulea.b` | 2 | 1 (`B[8\|16\|32\|64]`) |
| `_sulea.p` | 2 | 1 (*empty*) |
| `suld.b` | 1 | 1 (`B[8\|16\|32\|64]`) |
| `suq` | 1 | 1 (`B32`) |
| `sured.b` | 2 | 2 (`B32`, `I[32\|64]`) |
| `sured.p` | 2 | 2 (`B32`, `B64`) |
| `sust.b` | 1 | 1 (`B[8\|16\|32\|64]`) |
| `sust.p` | 1 | 1 (`B32`) |
| **`tex`** | **48** | 8 |
| **`tex.base`** | **48** | 8 |
| **`tex.grad`** | **48** | 8 |
| **`tex.level`** | **48** | 8 |
| `tld4` | 16 | 2 (`I32F32`, `F32F32`) |
| `txq` | 1 | 1 (`B32`) |
| `txq.level` | 1 | 1 (`B32`) |

Each of the four `tex*` opcodes registers the **exact same 8 encodings** (`F16F32`, `F16I32`, `F32F32`, `F32I32`, `H32F32`, `H32I32`, `I32F32`, `I32I32`) **six times**, once per texture-mode suffix (`"0i1"`, `"0i1s"`, and four more for 1D / 2D / 3D / cube / array / shadow-array combinations). That is 4 x 8 x 6 = 192 registrations on its own -- 17% of the entire instruction table -- which is why texture is the single largest non-MMA category.

#### Warp-level collectives (17 variants / 11 opcodes)

| PTX opcode | Variants | Encoding |
|---|---|---|
| `_match` | 1 | `B[32\|64]` |
| `_warpsync` | 1 | *(empty)* |
| `activemask` | 1 | `B32` |
| `elect` | 1 | *(empty)* |
| `getctarank` | 1 | `I[32\|64]` |
| `match` | 1 | `B[32\|64]` |
| `pmevent`, `pmevent.mask` | 1 each | *(empty)* |
| `redux` | 3 | `B32`, `I32`, `F32` |
| `shfl` | 2 | `B32` (with/without predicate) |
| `vote` | 4 | `P` (x3 modes) + `B32` (`.ballot`) |

`vote` has 4 variants: `vote.all.pred`, `vote.any.pred`, `vote.uni.pred`, and `vote.ballot.b32`.

#### Cooperative copy / tensor memory (75 variants / 21 opcodes)

| PTX opcode | Variants | Distinct encodings |
|---|---|---|
| `clusterlaunchcontrol.query_cancel` | 2 | `PB128`, `B32B128` |
| `clusterlaunchcontrol.try_cancel.async` | 1 | `B128` |
| `cp.async` | 6 | *(empty)* |
| `cp.async.bulk` | 8 | *(empty)* |
| `cp.async.bulk.commit_group` | 1 | `E16` |
| `cp.async.bulk.prefetch` | 2 | *(empty)* |
| `cp.async.bulk.prefetch.tensor` | 4 | *(empty)* |
| `cp.async.bulk.tensor` | 10 | *(empty)* |
| `cp.async.bulk.wait_group` | 1 | *(empty)* |
| `cp.async.commit_group` | 1 | *(empty)* |
| `cp.async.mbarrier.arrive` | 1 | `B64` |
| `cp.async.wait_all` | 1 | *(empty)* |
| `cp.async.wait_group` | 1 | *(empty)* |
| `cp.reduce.async.bulk` | 12 | `I[32\|64]`, `B[32\|64]`, `F[32\|64]`, `F16`, `E16` |
| `cp.reduce.async.bulk.tensor` | 2 | *(empty)* |
| `griddepcontrol` | 1 | *(empty)* |
| `multimem.ld_reduce` | 7 | `I[32\|64]`, `B[32\|64]`, `F[32\|64]`, `H32`, `E[16\|32]`, `F16`, `Q[8\|16\|32]` |
| `multimem.red` | 5 | `I[32\|64]`, `B[32\|64]`, `F[16\|32\|64]`, `H32`, `E[16\|32]` |
| `multimem.st` | 6 | `I[32\|64]`, `B[32\|64]`, `F[16\|32\|64]`, `H32`, `E[16\|32]`, `Q[8\|16\|32]` |
| `tensormap.cp_fenceproxy` | 1 | *(empty)* |
| `tensormap.replace` | 2 | `B[32\|64]` |

`cp.async.bulk.tensor` registers 10 variants (one per tensor map dimensionality x load/store x multicast) without any per-variant type checking -- the 10 come from different suffix strings using the `M` and `C` tags.

#### Tensor cores -- MMA / wgmma / wmma / tcgen05 (360 variants / 37 opcodes)

This is the largest single category, accounting for 31.6% of the entire instruction table. `_mma.warpgroup` alone registers **135 variants**, making it the single largest opcode in the binary:

| PTX opcode | Variants | Distinct encodings | Notes |
|---|---|---|---|
| `_ldsm` | 4 | 4 | `B[8\|16]`, `I8I4`, `I4I2`, *empty* |
| `_mma` | 41 | 23 | Internal MMA used by compiler-generated lowering |
| **`_mma.warpgroup`** | **135** | 8 | 8 encodings x up to 18 suffix shapes each |
| `_tcgen05.guardrails.allocation_granularity` | 1 | 1 | *(empty)* |
| `_tcgen05.guardrails.are_columns_allocated` | 2 | 1 | *(empty)* |
| `_tcgen05.guardrails.check_sparse_usage` | 1 | 1 | *(empty)* |
| `_tcgen05.guardrails.datapath_alignment` | 1 | 1 | *(empty)* |
| `_tcgen05.guardrails.in_physical_bounds` | 2 | 1 | *(empty)* |
| `_tcgen05.guardrails.is_current_warp_valid_owner` | 1 | 1 | *(empty)* |
| `_tcgen05.guardrails.is_phase_valid` | 1 | 1 | *(empty)* |
| `_tcgen05.guardrails.sp_consistency_across_idesc_mod` | 1 | 1 | *(empty)* |
| `_warpgroup.arrive` | 1 | 1 | `F32F16F16F32` |
| `_warpgroup.commit_batch` | 1 | 1 | `F32F16F16F32` |
| `_warpgroup.wait` | 2 | 2 | `F32F16F16F32`, *(empty)* |
| **`mma`** | **38** | 20 | Public MMA |
| `tcgen05.alloc` | 1 | 1 | `B32` |
| `tcgen05.commit` | 2 | 1 | `B64` |
| `tcgen05.cp` | 1 | 1 | *(empty)* |
| `tcgen05.dealloc` | 1 | 1 | `B32` |
| `tcgen05.fence` | 1 | 1 | *(empty)* |
| `tcgen05.ld` | 2 | 1 | `B32` |
| `tcgen05.ld.red` | 4 | 2 | `F32`, `I32` |
| **`tcgen05.mma`** | **20** | 1 | *(empty)* -- validation is entirely via suffix strings |
| `tcgen05.mma.ws` | 8 | 1 | *(empty)* |
| `tcgen05.relinquish_alloc_permit` | 1 | 1 | `B32` |
| `tcgen05.shift` | 1 | 1 | *(empty)* |
| `tcgen05.st` | 2 | 1 | `B32` |
| `tcgen05.wait` | 1 | 1 | *(empty)* |
| `wgmma.commit_group` | 1 | 1 | `I32I8I8` |
| `wgmma.fence` | 1 | 1 | `I32I8I8` |
| **`wgmma.mma_async`** | **30** | 8 | Public warpgroup MMA |
| `wgmma.wait_group` | 1 | 1 | *(empty)* |
| `wmma.load.a`, `wmma.load.b` | 12 each | 5 each | `F16`, `F32`, *empty*, `I8`, `F64` |
| `wmma.load.c` | 8 | 4 | `F16`, `F32`, `I32`, `F64` |
| `wmma.mma` | 10 | 10 | `F16F16`, `F32F16`, `F32F32`, `F16F32`, `I32B1B1I32`, `I32I4I4I32`, `I32I8I8I32`, `F64F64F64F64`, `E32T32T32E32`, `F32Q8Q8F32` |
| `wmma.store.d` | 8 | 4 | `F16`, `F32`, `I32`, `F64` |

The 8 encoding shapes shared by `_mma.warpgroup` and `wgmma.mma_async` are the story of SM 90 / SM 100 tensor cores:

| Encoding (A, B, C, D) | MMA kind |
|---|---|
| `F16F16F16F16` | half-precision |
| `F32F16F16F32` | mixed-precision (f32 accumulator, f16 operands) |
| `F32E16E16F32` | bf16 / fp8 accumulator (extended-FP operands) |
| `F32T32T32F32` | tf32 |
| `F16Q8Q8F16` | int8 x int8 -> f16 (packed half accumulator) |
| `F32Q8Q8F32` | int8 x int8 -> f32 |
| `I32I8I8I32` | int8 x int8 -> int32 |
| `I32B1B1I32` | 1-bit binary matrix (`bmma`) |

Each of those eight encodings is registered up to **18 times** for `_mma.warpgroup` via different suffix strings, covering `(dense vs sparse) x (scale vs no-scale) x (stride A vs B) x (predicate fill)` = up to 16+ modes. The `P`-tailed suffixes (`hUUhP`, `fUUfP`, ...) are the predicate-returning forms used to signal MMA completion back to the warpgroup scheduler.

#### Video SIMD (31 variants / 23 opcodes)

Every video/SIMD opcode has exactly 1 or 2 variants and uses the same `I32I32I32` encoding (three-operand int32 SIMD); differences are all carried in the suffix string:

| Shape | Count | Encoding |
|---|---|---|
| `vabsdiff`, `vadd`, `vmax`, `vmin`, `vshl`, `vshr`, `vsub` | 2 each | `I32I32I32` |
| `vset` | 2 | `I32I32` |
| `v*2` (8 opcodes: `vadd2`, `vsub2`, `vavrg2`, `vabsdiff2`, `vmin2`, `vmax2`, `vset2`, `vmad`*) | 1 each | `I32I32I32` |
| `v*4` (7 opcodes: `vadd4`, `vsub4`, `vavrg4`, `vabsdiff4`, `vmin4`, `vmax4`, `vset4`) | 1 each | `I32I32I32` |

(*`vmad` is technically not a packed form but registers with the same encoding.*) These are the PTX 5.0 video SIMD intrinsics -- effectively thin wrappers around the `.s32`/`.u32` variants of each underlying SASS instruction, so the semantic difference lives entirely in the suffix string (e.g. `"sddsdC"` vs `"uuuddC"`).

#### Verification / debug (2 variants / 2 opcodes)

| PTX opcode | Encoding | Notes |
|---|---|---|
| `_checkfp.divide` | `F32` | Internal hook used by `div.approx.f32` lowering to insert NaN/divide-by-zero checks |
| `istypep` | `O` | The only registration with an `O` (opaque-handle) encoding; operand is a `.texref`, `.surfref`, or `.samplerref` symbol |

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
