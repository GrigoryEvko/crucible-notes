# Lexer & Tokenizer

The lexer in cudafe++ is the EDG 6.6 tokenizer — a hand-coded, state-machine-driven scanner that converts raw source bytes into a stream of 357 distinct token kinds. It spans approximately 185 functions across the address range `0x668330`--`0x689130` and constitutes one of the densest subsystems in the binary. The design is a classic multi-layered scanner: a byte-level character scanner (`sub_679800`, 907 lines) feeds into a token acquisition engine (`sub_6810F0`, 3,811 lines), which in turn is wrapped by a cache-aware token delivery function (`sub_676860`, 1,995 lines). CUDA keyword recognition is injected at the `get_token_main` level, gated on `dword_106C2C0` (GPU compilation mode flag).

The lexer does not use generated tables from tools like flex. Instead, every character-class test, keyword match, and operator scan is written as explicit C switch/if chains, compiled into dense jump tables by the optimizer. This produces extremely large functions — `get_token_main` alone has approximately 300 local variables in its decompiled form — but eliminates the overhead of table-driven DFA transitions for a language as context-sensitive as C++.

## Key Facts

| Property | Value |
|---|---|
| Subsystem | Lexer / tokenizer (EDG 6.6, ~185 functions) |
| Address range | `0x668330`--`0x689130` |
| Token kinds | 357 (indexed from `off_E6D240` name table) |
| Primary scanner | `sub_679800` (`scan_token`, 907 lines) |
| Token acquisition | `sub_6810F0` (`get_token_main`, 3,811 lines, ~300 locals) |
| Cache + delivery | `sub_676860` (`get_next_token`, 1,995 lines) |
| Numeric literal scanner | `sub_672390` (`scan_numeric_literal`, 1,571 lines) |
| Keyword registration | `sub_5863A0` (`keyword_init`, in `fe_init.c`, 200+ keywords) |
| Universal char scanner | `sub_6711E0` (`scan_universal_character`, 278 lines) |
| Template arg scanner | `sub_67DC90` (`scan_template_argument_list`, 1,078 lines) |
| Token cache entry size | 80–112 bytes (8 cache entry kinds) |
| Scope entry size | 784 bytes (at `qword_126C5E8`) |
| GPU mode gate | `dword_106C2C0` |
| Current token global | `word_126DD58` |

## Architecture

The lexer is organized as four concentric layers, each calling into the one below it:

```text
Parser (expr.c, decls.c, statements.c)
  │
  ▼
get_next_token (sub_676860)         ← Cache management, macro rescan
  │
  ▼
get_token_main (sub_6810F0)         ← Keyword classification, CUDA gates
  │
  ▼
scan_token (sub_679800)             ← Character-level scanning
  │
  ▼
Input buffer (qword_126DDA0)        ← Raw bytes from source file
```

The parser never calls the character-level scanner directly. All token consumption flows through `get_next_token`, which checks the token cache and rescan lists before falling through to `get_token_main`. This layering allows the lexer to support lookahead, backtracking, macro expansion replay, and template argument rescanning without modifying the core scanner.

## Token System

### The 357 Token Kinds

Every token produced by the lexer carries a 16-bit token code stored in `word_126DD58`. The complete set of 357 token kinds is indexed through the name table at `off_E6D240`, which maps each token code to its string representation. The stop-token table at `qword_126DB48 + 8` contains 357 boolean entries used by the error recovery scanner to identify synchronization points.

Token codes are assigned in blocks:

| Range | Category | Examples |
|---|---|---|
| 1–51 | Operators and punctuation | `+`, `-`, `*`, `/`, `(`, `)`, `{`, `}`, `::`, `->` |
| 52–76 | Alternative tokens / digraphs | `and`, `or`, `not`, `<%`, `%>`, `<:`, `:>` |
| 77–108 | C89 keywords | `auto`(77), `break`(78), `case`(79), `char`(80), `while`(108) |
| 109–131 | C99/C11 keywords | `restrict`(119), `_Bool`(120), `_Complex`(121), `_Imaginary`(122) |
| 132–136 | MSVC keywords | `__declspec`(132), `__int8`(133), `__int16`(134), `__int32`(135), `__int64`(136) |
| 137–199 | C++ keywords | `catch`(150), `class`(151), `template`(160), `decltype`(185), `typeof`(189) |
| 200–206 | Compiler internal | Internal token kinds for the preprocessor |
| 207–330 | Type traits | `__is_class`(207), `__has_trivial_copy`, ..., NVIDIA-specific traits at 328–330 |
| 331–356 | Extended types / recent additions | `_Float32`(331)--`_Float128`(335), C++23/26 features |

### CUDA-Specific Token Kinds

Three NVIDIA type-trait keywords occupy dedicated token codes registered during `keyword_init`:

| Token Code | Keyword | Purpose |
|---|---|---|
| 328 | `__nv_is_extended_device_lambda_closure_type` | Tests if type is a device lambda |
| 329 | `__nv_is_extended_host_device_lambda_closure_type` | Tests if type is a host-device lambda |
| 330 | `__nv_is_extended_device_lambda_with_preserved_return_type` | Tests if device lambda preserves return type |

These are registered as standard type-trait keywords and participate in the same token classification path as the 60+ standard `__is_xxx`/`__has_xxx` traits.

### Token State Globals

When a token is produced, the following globals are populated:

| Address | Name | Type | Description |
|---|---|---|---|
| `word_126DD58` | `current_token_code` | WORD | 16-bit token kind (0–356) |
| `qword_126DD38` | `current_source_position` | QWORD | Encoded file/line/column |
| `qword_126DD48` | `token_text_ptr` | QWORD | Pointer to identifier/literal text |
| `src` | `token_start_position` | char* | Start of token in input buffer |
| `n` | `token_text_length` | size_t | Length of token text |
| `dword_126DF90` | `token_flags_1` | DWORD | Classification flags |
| `dword_126DF8C` | `token_flags_2` | DWORD | Additional flags |
| `qword_126DF80` | `token_extra_data` | QWORD | Context-dependent payload |
| `xmmword_106C380`--`106C3B0` | `identifier_lookup_result` | 4 x 128-bit | SSE-packed lookup result for identifiers (64 bytes) |

The 64-byte identifier lookup result is written into four SSE registers (`xmmword_106C380` through `xmmword_106C3B0`) by the identifier classification path. When a scanned identifier is also a keyword, the lookup result contains the keyword's token code, scope information, and classification flags. The compiler uses `movaps`/`movups` instructions to read/write this packed state in bulk.

## Token Cache

The token cache provides the lookahead, backtracking, and macro-expansion replay capabilities required by C++ parsing. Tokens are stored in a linked list of cache entries that can be consumed, rewound, and re-scanned.

### Cache Entry Layout (80–112 bytes)

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `next` | Next entry in cache linked list |
| `+8` | 8 | `source_position` | Encoded source location |
| `+16` | 2 | `token_code` | Token kind (0–356) |
| `+18` | 1 | `cache_entry_kind` | Discriminator for payload type (see below) |
| `+20` | 4 | `flags` | Token flags |
| `+24` | 4 | `extra_flags` | Additional flags |
| `+32` | 8 | `extra_data` | Context-dependent data |
| `+40`.. | varies | `payload` | Kind-specific payload data |

### Cache Entry Kinds

| Kind | Value | Payload | Description |
|---|---|---|---|
| identifier | 1 | Name pointer + lookup result | Identifier token with pre-resolved scope lookup |
| macro_def | 2 | Macro definition pointer | Macro definition for re-expansion (calls `sub_5BA500`) |
| pragma | 3 | Pragma data | Preprocessor pragma for deferred processing |
| pp_number | 4 | Number text | Preprocessing number (not yet classified as int/float) |
| (reserved) | 5 | — | Not observed in use |
| string | 6 | String data + encoding | String literal token |
| (reserved) | 7 | — | Not observed in use |
| concatenated_string | 8 | Concatenated string data | Wide or multi-piece concatenated string literal |

### Cache Management Globals

| Address | Name | Description |
|---|---|---|
| `qword_1270150` | `cached_token_rescan_list` | Head of list of tokens to re-scan (pushed back for lookahead) |
| `qword_1270128` | `reusable_cache_stack` | Stack of reusable cache entry blocks |
| `qword_1270148` | `free_token_list` | Free list for recycling cache entries |
| `qword_1270140` | `macro_definition_chain` | Active macro definition chain |
| `dword_126DB74` | `has_cached_tokens` | Boolean flag: nonzero when cache is non-empty |

### Cache Operations

| Address | Identity | Description |
|---|---|---|
| `sub_669650` | `copy_tokens_from_cache` | Copies cached preprocessor tokens for macro re-expansion (assert at `lexical.c:3417`) |
| `sub_669D00` | `allocate_token_cache_entry` | Allocates from free list at `qword_1270118` |
| `sub_669EB0` | `create_cached_token_node` | Creates and initializes token cache node |
| `sub_66A000` | `append_to_token_cache` | Appends token to cache list, maintains tail pointer |
| `sub_66A140` | `push_token_to_rescan_list` | Pushes token onto rescan stack at `qword_1270150` |
| `sub_66A2C0` | `free_single_cache_entry` | Returns cache entry to free list |

## Layer 1: scan_token (sub_679800)

`scan_token` is the character-level scanner. It reads raw bytes from the input buffer at `qword_126DDA0`, classifies them, and produces a single token. The function is 907 lines and dispatches on the first byte of each token.

### Character Dispatch

The scanner reads the byte at the current input position and enters one of the following paths:

| First Byte | Action |
|---|---|
| `0x00` (NUL) | Control byte processing (8 embedded control types, see below) |
| `0x09` (TAB), `0x0B` (VT), `0x0C` (FF), `0x20` (space) | Whitespace — advance and retry |
| `a`--`z`, `A`--`Z`, `_` | Identifier or keyword scanning |
| `0`--`9` | Numeric literal scanning (decimal, hex, octal, binary) |
| `'` | Character literal scanning |
| `"` | String literal scanning |
| `/` | Comment (`//` or `/* */`) or division operator |
| `.` | Dot operator, or float literal if followed by digit |
| `<` | Less-than, `<=`, `<<`, `<<=`, `<=>`, or template bracket |
| `>` | Greater-than, `>=`, `>>`, `>>=`, or template bracket |
| `+`, `-`, `*`, `%`, `^`, `~`, `!`, `=`, `&`, `\|` | Operator scanning (single or compound) |
| `(`, `)`, `[`, `]`, `{`, `}`, `;`, `,`, `?`, `@` | Single-character tokens |
| `#` | Preprocessor directive or stringification operator |
| `\` | Universal character name (`\uXXXX`, `\UXXXXXXXX`) or line continuation |

### Embedded Control Bytes (NUL Dispatch)

The input buffer uses embedded NUL bytes (`0x00`) as in-band control markers. When the scanner encounters a NUL, it reads the next byte as a control type code:

| Control Type | Value | Action |
|---|---|---|
| Newline marker | 1 | End of line — calls `sub_6702F0` (`refill_buffer`) to read next source line |
| (reserved) | 2 | — |
| Macro position | 3 | Macro expansion position marker — calls `sub_66A770` to update position tracking |
| End of directive | 4 | Marks end of a preprocessor directive |
| EOF (primary) | 5 | End of current source file — pops file stack |
| Stale position | 6 | Invalid position marker — emits diagnostic 1192 or 861 |
| Continuation | 7 | Backslash-newline continuation was here |
| EOF (secondary) | 8 | Secondary EOF marker for nested includes |

This in-band signaling approach avoids the cost of checking buffer boundaries on every character read. The `refill_buffer` function (`sub_6702F0`, 792 lines) places these marker bytes at the end of each source line, so the scanner can detect line endings and EOF without comparing the input pointer against a limit.

### Input Buffer System

| Address | Name | Description |
|---|---|---|
| `qword_126DDA0` | `current_input_position` | Read pointer into the input buffer |
| `qword_126DDD8` | `input_buffer_base` | Start of the allocated input buffer |
| `qword_126DDD0` | `input_buffer_end` | End of the allocated input buffer |
| `qword_126DDF0` | `file_stack` | Stack of open source files (for `#include`) |
| `qword_127FBA8` | `current_file_handle` | `FILE*` for the current source file |
| `dword_127FBA0` | `eof_flag` | Set when current file reaches EOF |
| `dword_127FB9C` | `multibyte_encoding_mode` | Values >1 enable multibyte character decoding via `sub_5B09B0` |
| `dword_126DDA8` | `source_line_counter` | Lines read from current source file |
| `dword_126DDBC` | `output_line_counter` | Lines emitted to preprocessed output |

### Buffer Refill: read_next_source_line (sub_66F4E0)

`sub_66F4E0` (735 lines) reads the next line from the source file into the input buffer. It calls `getc()` for single-byte mode or `sub_5B09B0` for multibyte mode (controlled by `dword_127FB9C > 1`). The function:

1. Reads characters one at a time until newline or EOF
2. Handles backslash-newline line splicing (joining continuation lines)
3. Places control byte markers at newline positions (type 1) and EOF (type 5/8)
4. Updates the line counter at `dword_126DDA8`
5. Manages trigraph warnings (diagnostic 1750) through the companion function `sub_6702F0`

## Layer 2: get_token_main (sub_6810F0)

`get_token_main` is the largest function in the lexer at 3,811 decompiled lines with approximately 300 local variables. It wraps `scan_token` and performs the complete token classification pipeline: keyword recognition, CUDA keyword gating, template parameter detection, operator overload name lookup, access specifier tracking, and namespace scope management.

### Token Classification Pipeline

After `scan_token` produces a raw token, `get_token_main` performs these classification steps:

```text
scan_token produces raw token
  │
  ├── Identifier?
  │     ├── Look up in keyword table
  │     │     ├── Standard C/C++ keyword → set token_code to keyword kind
  │     │     ├── CUDA keyword (dword_106C2C0 != 0) → set token_code
  │     │     ├── Type trait keyword → set token_code (207-356)
  │     │     └── Not a keyword → classify as identifier token
  │     │
  │     ├── Check template parameter context
  │     │     └── If inside template<>, classify as type-name or non-type
  │     │
  │     └── Entity lookup for context-sensitive classification
  │           ├── typedef name → classify as TYPE_NAME token
  │           ├── class/struct name → classify as CLASS_NAME
  │           ├── enum name → classify as ENUM_NAME
  │           ├── namespace name → classify as NAMESPACE_NAME
  │           └── template name → classify as TEMPLATE_NAME
  │
  ├── Numeric literal?
  │     └── Route to scan_numeric_literal (sub_672390)
  │
  ├── String/character literal?
  │     └── Handle encoding prefix (L, u8, u, U, R)
  │
  └── Operator/punctuation?
        ├── Check for template angle bracket context
        ├── Handle digraphs/alternative tokens
        └── Produce operator token code
```

### CUDA Keyword Detection

CUDA keyword handling is gated on `dword_106C2C0` (GPU mode). When this flag is nonzero, `get_token_main` recognizes CUDA-specific identifiers and routes them to the CUDA attribute processing path:

```c
// Pseudocode from get_token_main
if (token_is_identifier) {
    // ... standard keyword lookup ...

    if (dword_106C2C0 != 0) {  // GPU mode active
        // Check for __device__, __host__, __global__,
        // __shared__, __constant__, __managed__,
        // __launch_bounds__, __grid_constant__
        // Route to CUDA attribute handlers
        if (dword_106BA08) {   // CUDA attribute processing enabled
            sub_74DC30(...);   // CUDA attribute resolution
            sub_74E240(...);   // CUDA attribute application
        }
    }
}
```

The GPU mode flag `dword_106C2C0` is also checked during:
- Attribute token processing in `sub_686350` (`handle_attribute_token`, 584 lines)
- Deferred diagnostic emission in `sub_668660` (severity override via `byte_126ED55`)
- Entity visibility computation in `sub_669130`

### C++ Standard Version Gating

Throughout `get_token_main`, keyword classification is gated on the C++ standard version stored in `dword_126EF68`:

| Version Value | Standard | Keywords Enabled |
|---|---|---|
| 201102 | C++11 | `constexpr`, `decltype`, `nullptr`, `char16_t`, `char32_t`, `static_assert` |
| 201402 | C++14 | `binary literals`, `digit separators` |
| 201703 | C++17 | `if constexpr`, `char8_t`, `structured bindings` |
| 202002 | C++20 | `concept`, `requires`, `co_yield`, `co_return`, `co_await`, `consteval`, `constinit` |
| 202302 | C++23 | `typeof`, `typeof_unqual`, extended digit separators |

The language mode at `dword_126EFB4` controls broader dialect selection:

| Value | Mode | Effect |
|---|---|---|
| 1 | GNU/default | GNU extensions enabled, alternative tokens recognized |
| 2 | MSVC | MSVC keywords enabled (`__declspec`, `__int8`--`__int64`), some GNU extensions disabled |

### Context-Sensitive Token Classification

C++ requires the lexer to classify identifiers based on declaration context. The functions supporting this classification:

| Address | Identity | Description |
|---|---|---|
| `sub_668C90` | `classify_identifier_entity` | Dispatches on entity kind: typedef(3), class(4,5), function(7,9), namespace(19-22) |
| `sub_668E00` | `resolve_entity_through_alias` | Walks typedef/using chains (kind=3 with `+104` flag, kind=16 → `**[+88]`) |
| `sub_668F80` | `get_resolved_entity_type` | Resolves entity to underlying type through alias chains |
| `sub_668900` | `handle_token_identifier_type_check` | Determines if token is identifier vs typename vs template |
| `sub_666720` | `select_dual_lookup_symbol` | Selects between two candidate symbols in dual-scope lookup (372 lines) |

Entity classification reads the `entity_kind` byte at offset `+80` of entity nodes:

```c
switch (entity->kind) {    // offset +80
    case 3:                // typedef
        return TYPE_NAME;
    case 4: case 5:        // class / struct
        return CLASS_NAME;
    case 6:                // enum
        return ENUM_NAME;
    case 7:                // function
        return IDENTIFIER;
    case 9: case 10:       // namespace / namespace alias
        return NAMESPACE_NAME;
    case 19: case 20: case 21: case 22:  // template kinds
        return TEMPLATE_NAME;
    case 16:               // using declaration
        return resolve_through_using(entity);
    case 24:               // namespace alias (resolved)
        return NAMESPACE_NAME;
}
```

## Layer 3: get_next_token (sub_676860)

`get_next_token` (1,995 lines) is the token delivery function called by the parser. It manages the token cache, handles macro expansion replay, and calls `get_token_main` only when no cached tokens are available.

### Token Delivery Flow

```text
get_next_token (sub_676860)
  │
  ├── Check cached_token_rescan_list (qword_1270150)
  │     └── If non-empty: pop token, dispatch on cache_entry_kind
  │           ├── kind 1 (identifier): load xmmword_106C380..106C3B0
  │           ├── kind 2 (macro_def): call sub_5BA500 (macro expansion)
  │           ├── kind 3 (pragma): process deferred pragma
  │           ├── kind 4 (pp_number): return as-is
  │           ├── kind 6 (string): return string token
  │           └── kind 8 (concatenated_string): return concatenated string
  │
  ├── Check reusable_cache_stack (qword_1270128)
  │     └── If non-empty: pop and return cached token
  │           (assert: "get_token_from_reusable_cache_stack" at 4450, 4469)
  │
  ├── Check pending_macro_arg (qword_106B8A0)
  │     └── If set: process macro argument token
  │
  └── Fall through to get_token_main (sub_6810F0)
        └── Full token acquisition from source
```

The function sets the following globals on every token delivery:

- `word_126DD58` = token code
- `qword_126DD38` = source position
- `dword_126DF90` = token flags 1
- `dword_126DF8C` = token flags 2
- `qword_126DF80` = extra data

### CUDA Attribute Token Interception

When CUDA attribute processing is enabled (`dword_106BA08 != 0`), `get_next_token` intercepts identifier tokens and routes them through CUDA attribute resolution via `sub_74DC30` and `sub_74E240`. This allows CUDA execution-space attributes (`__device__`, `__host__`, `__global__`) to be recognized at the token level rather than requiring full declaration parsing.

## Numeric Literal Scanner: scan_numeric_literal (sub_672390)

The numeric literal scanner is 1,571 lines and handles every numeric literal format defined by C89 through C++23.

### Literal Prefix Dispatch

```text
scan_numeric_literal
  │
  ├── First char '0':
  │     ├── 0x/0X → hex literal (isxdigit validation)
  │     ├── 0b/0B → binary literal (C++14)
  │     ├── 0[0-7] → octal literal
  │     └── 0 alone → decimal zero
  │
  ├── First char '1'-'9':
  │     └── decimal literal
  │
  └── After integer part:
        ├── '.' → floating-point literal
        ├── 'e'/'E' → decimal float exponent
        ├── 'p'/'P' → hex float exponent
        └── suffix → type suffix parsing
```

### C++14 Digit Separators

Digit separators (`'` characters within numeric literals) are handled through a two-flag system:

| Address | Name | Purpose |
|---|---|---|
| `dword_126EEFC` | `cpp14_digit_separators_enabled` | Master enable for digit separator support |
| `dword_126DB58` | `digit_separator_seen` | Set when a separator is encountered in the current literal |

When `dword_126EEFC` is enabled, the scanner accepts `'` between digits:

```c
// Digit separator handling in scan_numeric_literal
while (isdigit(*pos) || (*pos == '\'' && dword_126EEFC)) {
    if (*pos == '\'') {
        dword_126DB58 = 1;  // mark separator seen
        pos++;
        if (!isdigit(*pos))
            emit_diagnostic(2629);  // separator not followed by digit
        continue;
    }
    // process digit...
}
```

C++23 extended digit separators (for binary, octal, hex) are gated on `dword_126EF68 > 202302`:

```c
if (dword_126EF68 > 202302) {
    // C++23: allow digit separators in binary/octal/hex
} else {
    emit_diagnostic(2628);  // C++23 feature used in earlier mode
}
```

### Integer Suffix Parsing

`sub_6748A0` (`convert_integer_suffix`, 137 lines) parses the following suffixes:

| Suffix | Type |
|---|---|
| (none) | `int` (or promoted per value) |
| `u` / `U` | `unsigned int` |
| `l` / `L` | `long` |
| `ll` / `LL` | `long long` |
| `ul` / `UL` | `unsigned long` |
| `ull` / `ULL` | `unsigned long long` |
| `z` / `Z` | `size_t` (C++23) |
| `uz` / `UZ` | `size_t unsigned` (C++23) |

`sub_674BB0` (`determine_numeric_literal_type`, 400 lines) applies the C++ promotion rules based on the literal value and suffix to determine the final type.

### Floating-Point Literal Handling

| Address | Identity | Description |
|---|---|---|
| `sub_675390` | `scan_float_exponent` | Scans `e`/`E`/`p`/`P` exponent suffix (57 lines) |
| `sub_6754B0` | `convert_float_literal` | Converts float literal string to value (338 lines) |

Float suffixes: `f`/`F` (float), `l`/`L` (long double), none (double).

## Universal Character Names: scan_universal_character (sub_6711E0)

`sub_6711E0` (278 lines, assert at `lexical.c:12384`) scans `\uXXXX` and `\UXXXXXXXX` universal character names in identifiers and string/character literals.

```c
void scan_universal_character(char *input, uint32_t *result) {
    int width;
    if (input[1] == 'u')
        width = 4;    // \uXXXX
    else
        width = 8;    // \UXXXXXXXX

    uint32_t value = 0;
    for (int i = 0; i < width; i++) {
        char c = *input++;
        if (!isxdigit(c)) {
            // emit error diagnostic
            return;
        }
        int digit;
        if (c >= '0' && c <= '9')
            digit = c - 48;      // '0' = 48
        else if (islower(c))
            digit = c - 87;      // 'a' = 97, 97-87 = 10
        else
            digit = c - 55;      // 'A' = 65, 65-55 = 10
        value = (value << 4) | digit;
    }
    *result = value;
}
```

`sub_671870` (`validate_universal_character_value`, 62 lines) performs range checking after scanning: surrogate pair values (`0xD800`--`0xDFFF`) are rejected, and values outside the valid Unicode range (`> 0x10FFFF`) produce an error.

The feature is controlled by `dword_106BCC4` (universal characters enabled) and `dword_106BD4C` (extended character mode).

## Keyword Registration: keyword_init (sub_5863A0)

`sub_5863A0` (1,113 lines, in `fe_init.c`) registers all C/C++ keywords with the symbol table during frontend initialization. It calls `sub_7463B0` (`enter_keyword`) once per keyword, passing the token ID and string representation. GNU double-underscore variants are registered via `sub_585B10`, and alternative tokens via `sub_749600`.

### Keyword Categories and Version Gating

Keywords are registered conditionally based on language mode and standard version:

```text
keyword_init (sub_5863A0)
  │
  ├── C89 core (always registered)
  │     auto(77), break(78), case(79), char(80), continue(82),
  │     default(83), do(84), double(85), else(86), enum(87),
  │     extern(88), float(89), for(90), goto(91), if(92),
  │     int(93), long(94), register(95), return(96), short(97),
  │     sizeof(99), static(100), struct(101), switch(102),
  │     typedef(103), union(104), unsigned(105), void(106), while(108)
  │
  ├── C99 (gated on C99+ mode)
  │     _Bool(120), _Complex(121), _Imaginary(122), restrict(119)
  │
  ├── C11 (gated on C11+ mode)
  │     _Generic(262), _Atomic(263), _Alignof(247), _Alignas(248),
  │     _Thread_local(194), _Static_assert(184), _Noreturn(260)
  │
  ├── C23 (gated on C23 mode)
  │     bool, true, false, alignof, alignas, static_assert,
  │     thread_local, typeof(189), typeof_unqual(190)
  │
  ├── C++ core (gated on C++ mode: dword_126EFB4 == 2)
  │     catch(150), class(151), friend(153), inline(154),
  │     mutable(174), operator(156), new(155), delete(152),
  │     private(157), protected(158), public(159), template(160),
  │     this(161), throw(162), try(163), virtual(164),
  │     namespace(175), using(179), typename(183), typeid(178),
  │     const_cast(166), dynamic_cast(167), static_cast(177),
  │     reinterpret_cast(176)
  │
  ├── C++ alternative tokens (gated on C++ mode)
  │     and(52), and_eq(64), bitand(33), bitor(51), compl(37),
  │     not(38), not_eq(48), or(53), or_eq(66), xor(50), xor_eq(65)
  │
  ├── C++ modern keywords (gated on standard version)
  │     C++11: constexpr(244), decltype(185), nullptr(237),
  │            char16_t(126), char32_t(127)
  │     C++17: char8_t(128)
  │     C++20: consteval(245), constinit(246), co_yield(267),
  │            co_return(268), co_await(269), concept(295), requires(294)
  │     C++23: typeof(189), typeof_unqual(190)
  │
  ├── GNU extensions (gated on dword_126EFA8)
  │     __extension__(187), __auto_type(186), __attribute(142),
  │     __builtin_offsetof(117), __builtin_types_compatible_p(143),
  │     __builtin_shufflevector(258), __builtin_convertvector(259),
  │     __builtin_complex(261), __builtin_has_attribute(296),
  │     __builtin_addressof(271), __builtin_bit_cast(297),
  │     __int128(239), __bases(249), __direct_bases(250),
  │     _Float32(331), _Float32x(332), _Float64(333),
  │     _Float64x(334), _Float128(335)
  │
  ├── MSVC extensions (gated on dword_126EFB0)
  │     __declspec(132), __int8(133), __int16(134),
  │     __int32(135), __int64(136)
  │
  ├── Clang extensions (gated on Clang version at qword_126EF90)
  │     _Nullable(264), _Nonnull(265), _Null_unspecified(266)
  │
  ├── Type traits (60+, gated by standard version)
  │     __is_class(207), __is_enum, __is_union, __has_trivial_copy,
  │     __has_virtual_destructor, ... through token code 327
  │
  ├── NVIDIA CUDA type traits (gated on GPU mode)
  │     __nv_is_extended_device_lambda_closure_type(328),
  │     __nv_is_extended_host_device_lambda_closure_type(329),
  │     __nv_is_extended_device_lambda_with_preserved_return_type(330)
  │
  └── EDG internal keywords (always registered)
        __edg_type__(272), __edg_size_type__(277),
        __edg_ptrdiff_type__(278), __edg_bool_type__(279),
        __edg_wchar_type__(280), __edg_opnd__(282),
        __edg_throw__(281), __edg_is_deducible(304),
        __edg_vector_type__(273), __edg_neon_vector_type__(274)
```

Version gating globals used during keyword registration:

| Address | Name | Values |
|---|---|---|
| `dword_126EFB4` | `language_mode` | 1 = K&R C / GNU default, 2 = C++ |
| `dword_126EF68` | `cpp_standard_version` | 199900, 201102, 201402, 201703, 202002, 202302 |
| `qword_126EF98` | `gnu_version` | e.g., `0x9FC3` = GCC 4.0.3 |
| `qword_126EF90` | `clang_version` | e.g., `0x15F8F`, `0x1D4BF` |
| `dword_126EFA8` | `gnu_extensions_enabled` | Boolean |
| `dword_126EFA4` | `extensions_enabled` | Boolean (Clang compat) |
| `dword_126EFAC` | `c_language_mode` | Boolean: C vs C++ |
| `dword_126EFB0` | `microsoft_extensions_enabled` | Boolean |

## String and Character Literal Scanning

### Character Literal Scanning

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_66CB30` | `scan_character_literal_prefix` | 34 | Detects encoding prefix (`L`, `u`, `U`, `u8`) |
| `sub_66CBD0` | `scan_character_literal` | 111 | Scans `'x'` / `L'x'` / `u'x'` / `U'x'` / `u8'x'` literals |

### String Literal Scanning

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_66C550` | `scan_string_literal` | 356 | Scans quoted string literals with escape sequences |
| `sub_676080` | `scan_raw_string_literal` | 391 | Scans `R"delimiter(content)delimiter"` raw strings |
| `sub_66E6E0` | `scan_identifier_suffix` | 94 | Checks for user-defined literal suffixes (C++11) |
| `sub_66E920` | `is_valid_ud_suffix` | 51 | Validates user-defined literal suffix names |
| `sub_6892F0` | `string_literal_concatenation_check` | 107 | Checks adjacent string literal tokens for concatenation |
| `sub_689550` | `process_user_defined_literal` | 332 | Handles C++11 UDL operator lookup |

### Encoding Prefixes

The lexer recognizes 5 string encoding prefixes, each producing a different string literal type:

| Prefix | Token | Character Type | Width |
|---|---|---|---|
| (none) | `"..."` | `char` | 1 byte |
| `L` | `L"..."` | `wchar_t` | 4 bytes (Linux) |
| `u8` | `u8"..."` | `char8_t` (C++20) / `char` | 1 byte |
| `u` | `u"..."` | `char16_t` | 2 bytes |
| `U` | `U"..."` | `char32_t` | 4 bytes |

## Scope Entry Layout

The lexer interacts heavily with the scope system. Scope entries are 784-byte records stored in an array at `qword_126C5E8`, indexed by `dword_126C5E4` (current scope index).

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 4 | `name_hash` | Hash of scope name for lookup |
| `+4` | 1 | `scope_kind` | Kind code (12 = file scope, see below) |
| `+6` | 1 | `scope_flags` | Bit flags: bit 5 = inline namespace |
| `+7` | 1 | `access_flags` | Bit 0 = in class context |
| `+10` | 1 | `extra_flags` | Bit 0 = module scope |
| `+12` | 1 | `template_flags` | Bit 0 = in template argument scan, bit 4 = has concepts |
| `+24` | 8 | `symbol_chain_or_hash_ptr` | Head of symbol chain or hash table |
| `+32` | 8 | `hash_table_ptr` | Hash table for O(1) lookup in large scopes |
| `+192` | 8 | `lazy_load_scope_ptr` | Pointer for lazy symbol loading (calls `sub_7C1900`) |
| `+208` | 4 | `scope_depth` | Nesting depth counter |
| `+376` | 8 | `parent_template_info` | Template context for template scope entries |
| `+416` | 8 | `module_info` | C++20 module partition data |
| `+632` | 8 | `class_info_ptr` | Pointer to class descriptor for class scopes |

Scope-related globals:

| Address | Name | Description |
|---|---|---|
| `dword_126C5E4` | `current_scope_index` | Index into scope table |
| `dword_126C5C4` | `class_scope_index` | Innermost class scope (-1 if none) |
| `dword_126C5C8` | `namespace_scope_index` | Innermost namespace scope (-1 if none) |
| `dword_126C5DC` | `file_scope_index` | File (global) scope index |
| `xmmword_126C520` | `entity_kind_to_language_mode_map` | 32-entry table mapping entity kinds to required language modes |

## Lexer State Stack

The lexer supports push/pop of its entire state for speculative parsing and template argument scanning.

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_688320` | `push_lexical_state` | 137 | Pushes current lexer state onto `qword_126DB40` stack |
| `sub_668330` | `pop_lexical_state_stack_full` | 166 | Pops state, restores stop-token table, macro chains (assert at `lexical.c:17808`) |

State stack nodes are 80-byte linked-list entries:

| Offset | Size | Field |
|---|---|---|
| `+0` | 8 | `next` (previous state) |
| `+8` | 8 | `cached_tokens` |
| `+16` | 8 | `source_position` |
| `+24`--`+72` | 48 | `token_cache_state` (saved cache pointers and flags) |

The push/pop mechanism is used for:
- Template argument list scanning (`sub_67DC90`, 1,078 lines)
- Speculative parsing in disambiguation contexts
- Macro expansion state save/restore

## Template Argument Scanning: scan_template_argument_list (sub_67DC90)

`sub_67DC90` (1,078 lines, assert at `lexical.c:19918`) scans template argument lists (`<...>`). This is one of the most complex lexer functions because of the `>>` ambiguity: in `vector<vector<int>>`, the closing `>>` must be split into two `>` tokens to close two template argument lists.

The scanner:

1. Pushes lexer state and sets template argument scanning mode (scope entry offset `+12`, bit 0)
2. Scans tokens while tracking nesting depth of `<>` pairs
3. Handles nested template-ids recursively
4. Creates token cache entries for deferred parsing
5. Uses the scope system to classify identifiers within template arguments
6. Disambiguates `>>` as either right-shift or double template close

The entity kind checks at offsets `+80` (values 19–22) identify template entities for recursive template-id scanning.

## Preprocessor Integration

The lexer handles several preprocessor-related responsibilities:

### Source Position Tracking

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_66D100` | `set_source_position` | 282 | Converts raw input position to file/line/column (called from dozens of locations) |
| `sub_66D5E0` | `emit_output_line` | 491 | Emits source text and `#line` directives to preprocessed output |
| `sub_66B1F0` | `emit_preprocessed_output` | 231 | Outputs `#line` directives via `qword_106C280` (output `FILE*`) |

### Macro Expansion Support

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_66A770` | `lookup_macro_at_position` | 41 | Scans macro chain (`qword_126DD80`) for macro enclosing given position |
| `sub_66A7F0` | `create_macro_expansion_record` | 44 | Allocates macro expansion tracking node |
| `sub_66A890` | `push_macro_expansion` | 41 | Pushes new expansion onto active stack |
| `sub_66A940` | `pop_macro_expansion` | 28 | Pops expansion from stack |
| `sub_66A9D0` | `is_in_macro_expansion` | 12 | Returns whether currently inside macro expansion |
| `sub_66A9F0` | `get_macro_expansion_depth` | 17 | Returns nesting depth of macro expansions |
| `sub_66A310` | `invalidate_macro_node` | 56 | Clears macro definition when it goes out of scope |
| `sub_66A5E0` | `free_macro_definition_chain` | 91 | Walks and frees macro chain via `qword_126DD70` / `qword_126DDE0` |

### Include File Handling

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_66BB50` | `open_source_file` | 332 | Opens include files via `sub_4F4970` (fopen wrapper), creates file tracking nodes |
| `sub_66EA70` | `open_next_input_file` | 364 | Opens next input source after current file ends, manages include-stack unwinding |
| `sub_67BAB0` | `scan_header_name` | 110 | Scans `<filename>` or `"filename"` for `#include` directives |

### Token Pasting and Stringification

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_67D1E0` | `handle_token_pasting` | 117 | Implements `##` preprocessor operator |
| `sub_67D440` | `stringify_token` | 251 | Implements `#` preprocessor operator |
| `sub_67D050` | `check_token_paste_validity` | 57 | Validates token paste produces a valid token |
| `sub_67D900` | `expand_macro_argument` | 204 | Expands a single macro argument during substitution |

## Operator Scanning

Multi-character operators are scanned by a set of dedicated functions in the `0x67ABB0`--`0x67BAB0` range. The scanner reads the first operator character and dispatches to the appropriate function to check for compound operators:

| First Char | Possible Tokens |
|---|---|
| `<` | `<`, `<=`, `<<`, `<<=`, `<=>`, `<%` (digraph `{`), `<:` (digraph `[`) |
| `>` | `>`, `>=`, `>>`, `>>=` |
| `+` | `+`, `++`, `+=` |
| `-` | `-`, `--`, `-=`, `->`, `->*` |
| `*` | `*`, `*=` |
| `&` | `&`, `&&`, `&=` |
| `\|` | `\|`, `\|\|`, `\|=` |
| `=` | `=`, `==` |
| `!` | `!`, `!=` |
| `:` | `:`, `::` |
| `.` | `.`, `...`, `.*` |

### Template Angle Bracket Disambiguation

`sub_67CB70` (`handle_template_angle_brackets`, 263 lines) handles the critical disambiguation of `<` and `>` in template contexts. In template argument lists, `<` opens and `>` closes, but in expressions, they are comparison operators. The function uses scope context information and the current parsing state (from the 784-byte scope entries) to make the determination.

## Error Recovery

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_6887C0` | `skip_to_token` | 317 | Error recovery: skips tokens until finding a synchronization point (`;`, `}`, etc.) |
| `sub_6886F0` | `expect_token` | 31 | Checks current token matches expected kind, emits diagnostic on mismatch |
| `sub_688560` | `peek_next_token` | 44 | Looks ahead at next token without consuming it |

The stop-token table at `qword_126DB48 + 8` (357 entries) controls which token kinds are valid synchronization points for error recovery.

## Built-in Type and Attribute Handling

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_685AB0` | `handle_builtin_type_token` | 289 | Processes built-in type keywords (`int`, `float`, etc.) into type tokens |
| `sub_685F10` | `process_decltype_token` | 212 | Handles `decltype()` expression in token stream |
| `sub_686350` | `handle_attribute_token` | 584 | Processes `[[attribute]]` and `__attribute__((x))` syntax, including CUDA attributes |
| `sub_686F40` | `process_asm_or_extension_keyword` | 244 | Handles `asm`, `__asm__`, and extension keywords |

## Diagnostic Strings

| String | Source | Condition |
|---|---|---|
| `"pop_lexical_state_stack_full"` | `sub_668330` | Assert at `lexical.c:17808` |
| `"copy_tokens_from_cache"` | `sub_669650` | Assert at `lexical.c:3417` |
| `"scan_universal_character"` | `sub_6711E0` | Assert at `lexical.c:12384` |
| `"get_token_from_cached_token_rescan_list"` | `sub_676860` | Assert at `lexical.c:4302` |
| `"get_token_from_reusable_cache_stack"` | `sub_676860` | Assert at `lexical.c:4450`, `4469` |
| `"scan_template_argument_list"` | `sub_67DC90` | Assert at `lexical.c:19918` |
| `"select_dual_lookup_symbol"` | `sub_666720` | Assert at `lexical.c:22477` |
| `"keyword_init"` | `sub_5863A0` | Assert at `fe_init.c:1597` |
| `"fe_translation_unit_init"` | `sub_5863A0` | Assert at `fe_init.c:2373` |

| Diagnostic Code | Context | Meaning |
|---|---|---|
| 870 | Character literal scanning | Invalid character in literal |
| 912 | `select_dual_lookup_symbol` | Ambiguous lookup result |
| 1192 | Control byte type 6 | Stale source position marker |
| 861 | Control byte type 6 | Invalid position reference |
| 1665 | `check_deferred_diagnostics` | Deferred macro-related warning |
| 1750 | `refill_buffer` | Trigraph sequence warning |
| 2628 | Numeric literal scanner | C++23 digit separator used in earlier mode |
| 2629 | Numeric literal scanner | Digit separator not followed by digit |

## Function Map

| Address | Identity | Confidence | Lines | Assert-string label |
|---|---|---|---|---|
| `sub_5863A0` | `keyword_init` / `fe_translation_unit_init` | 98% | 1,113 | `fe_init.c:1597` |
| `sub_666720` | `select_dual_lookup_symbol` | HIGH | 372 | `lexical.c:22477` |
| `sub_668330` | `pop_lexical_state_stack_full` | HIGH | 166 | `lexical.c:17808` |
| `sub_668660` | `check_deferred_diagnostics` | MEDIUM | 104 | `lexical.c` |
| `sub_6688A0` | `get_scope_from_entity` | HIGH | 32 | `lexical.c` |
| `sub_668C90` | `classify_identifier_entity` | MEDIUM | 89 | `lexical.c` |
| `sub_668E00` | `resolve_entity_through_alias` | MEDIUM | 88 | `lexical.c` |
| `sub_669650` | `copy_tokens_from_cache` | HIGH | 385 | `lexical.c:3417` |
| `sub_669D00` | `allocate_token_cache_entry` | MEDIUM | 119 | `lexical.c` |
| `sub_66A000` | `append_to_token_cache` | MEDIUM | 88 | `lexical.c` |
| `sub_66A140` | `push_token_to_rescan_list` | MEDIUM | 46 | `lexical.c` |
| `sub_66A3F0` | `create_source_region_node` | MEDIUM | 84 | `lexical.c` |
| `sub_66A5E0` | `free_macro_definition_chain` | MEDIUM | 91 | `lexical.c` |
| `sub_66A770` | `lookup_macro_at_position` | MEDIUM | 41 | `lexical.c` |
| `sub_66A890` | `push_macro_expansion` | MEDIUM | 41 | `lexical.c` |
| `sub_66AA50` | `process_preprocessor_directive` | MEDIUM | 380 | `lexical.c` |
| `sub_66B1F0` | `emit_preprocessed_output` | MEDIUM | 231 | `lexical.c` |
| `sub_66B910` | `skip_whitespace_and_comments` | MEDIUM | 105 | `lexical.c` |
| `sub_66BB50` | `open_source_file` | HIGH | 332 | `lexical.c` |
| `sub_66C550` | `scan_string_literal` | MEDIUM | 356 | `lexical.c` |
| `sub_66CBD0` | `scan_character_literal` | MEDIUM | 111 | `lexical.c` |
| `sub_66D100` | `set_source_position` | HIGH | 282 | `lexical.c` |
| `sub_66D5E0` | `emit_output_line` | HIGH | 491 | `lexical.c` |
| `sub_66DFF0` | `scan_pp_number` | MEDIUM | 268 | `lexical.c` |
| `sub_66EA70` | `open_next_input_file` | MEDIUM | 364 | `lexical.c` |
| `sub_66F4E0` | `read_next_source_line` | HIGH | 735 | `lexical.c` |
| `sub_6702F0` | `refill_buffer` | HIGH | 792 | `lexical.c` |
| `sub_6711E0` | `scan_universal_character` | HIGH | 278 | `lexical.c:12384` |
| `sub_671870` | `validate_universal_character_value` | MEDIUM | 62 | `lexical.c` |
| `sub_6719B0` | `scan_identifier_or_keyword` | HIGH | 400 | `lexical.c` |
| `sub_672390` | `scan_numeric_literal` | HIGH | 1,571 | `lexical.c` |
| `sub_6748A0` | `convert_integer_suffix` | MEDIUM | 137 | `lexical.c` |
| `sub_674BB0` | `determine_numeric_literal_type` | MEDIUM | 400 | `lexical.c` |
| `sub_675390` | `scan_float_exponent` | MEDIUM | 57 | `lexical.c` |
| `sub_6754B0` | `convert_float_literal` | MEDIUM | 338 | `lexical.c` |
| `sub_676080` | `scan_raw_string_literal` | MEDIUM-HIGH | 391 | `lexical.c` |
| `sub_676860` | `get_next_token` | HIGHEST | 1,995 | `lexical.c:4302` |
| `sub_679800` | `scan_token` | HIGH | 907 | `lexical.c` |
| `sub_67BAB0` | `scan_header_name` | MEDIUM | 110 | `lexical.c` |
| `sub_67CB70` | `handle_template_angle_brackets` | MEDIUM | 263 | `lexical.c` |
| `sub_67D050` | `check_token_paste_validity` | LOW | 57 | `lexical.c` |
| `sub_67D1E0` | `handle_token_pasting` | MEDIUM | 117 | `lexical.c` |
| `sub_67D440` | `stringify_token` | MEDIUM | 251 | `lexical.c` |
| `sub_67D900` | `expand_macro_argument` | MEDIUM | 204 | `lexical.c` |
| `sub_67DC90` | `scan_template_argument_list` | HIGH | 1,078 | `lexical.c:19918` |
| `sub_67F2E0` | `create_template_argument_cache` | MEDIUM | 184 | `lexical.c` |
| `sub_67F740` | `rescan_template_arguments` | MEDIUM-HIGH | 583 | `lexical.c` |
| `sub_680670` | `resolve_dependent_template_id` | MEDIUM | 240 | `lexical.c` |
| `sub_680AE0` | `handle_dependent_name_context` | MEDIUM | 235 | `lexical.c` |
| `sub_6810F0` | `get_token_main` | HIGHEST | 3,811 | `lexical.c` |
| `sub_685AB0` | `handle_builtin_type_token` | MEDIUM | 289 | `lexical.c` |
| `sub_685F10` | `process_decltype_token` | MEDIUM | 212 | `lexical.c` |
| `sub_686350` | `handle_attribute_token` | MEDIUM-HIGH | 584 | `lexical.c` |
| `sub_686F40` | `process_asm_or_extension_keyword` | MEDIUM | 244 | `lexical.c` |
| `sub_687F30` | `setup_lexer_for_parsing_mode` | MEDIUM | 216 | `lexical.c` |
| `sub_688320` | `push_lexical_state` | MEDIUM | 137 | `lexical.c` |
| `sub_688560` | `peek_next_token` | MEDIUM | 44 | `lexical.c` |
| `sub_6886F0` | `expect_token` | MEDIUM | 31 | `lexical.c` |
| `sub_6887C0` | `skip_to_token` | MEDIUM | 317 | `lexical.c` |

## Cross-References

- [Pipeline Overview](../pipeline/overview.md) — keyword registration during `sub_5863A0`
- [Entry Point & Initialization](../pipeline/entry.md) — frontend init calls keyword_init
- [Template Engine](template-engine.md) — template argument scanning at lexer level
- [Type System](type-system.md) — entity kind classification used by lexer
- [Token Kind Table](../reference/token-kinds.md) — full 357-entry token table
- [Scope Entry](../structs/scope-entry.md) — 784-byte scope entry structure
- [Entity Node Layout](../structs/entity-node.md) — entity node offsets used by identifier classification
- [Global Variable Index](../reference/global-variables.md) — all global addresses referenced here
- [Attribute System Overview](../attributes/overview.md) — CUDA attribute handling at token level
