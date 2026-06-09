# The Preprocessor

The preprocessor in cudafe++ is EDG 6.6's `preproc.c` — the directive-recognition layer that sits between the lexer's raw token stream and every higher-level subsystem. It implements the C and C++ preprocessor: `#include`, `#define`, `#undef`, `#if`/`#ifdef`/`#ifndef`, `#elif`/`#elifdef`/`#elifndef`/`#else`/`#endif`, `#line`, `#error`, `#warning`, `#pragma`, `#ident`, `#assert`, `#unassert`, `#include_next`, and the C++23 conditional extensions. The subsystem occupies approximately `0x6F9310`--`0x6FE130` in the binary (roughly 20 KB of code in 36 functions), with the master dispatcher `pp_directive` at `0x6FC940` consuming a single 5,047-byte function. Source attribution is anchored by an assertion at `preproc.c:4834` (`"pp_directive: bad pp directive code"`) inside `pp_directive` itself, by `preproc.c:2964` inside `look_up_pragma_id`, and by `preproc.c:3521` inside `process_gnu_system_header_pragma`.

Unlike GCC's libcpp or Clang's `Lex`, EDG's preprocessor is not a separate phase. It runs *interleaved* with the lexer: the lexer returns a token, the parser/dispatcher sees it is `#` (token kind 1), and synchronously enters `pp_directive` which consumes additional tokens until end-of-line, then control returns to the caller as if a single token had been produced. There is no token buffer, no PP-token AST — the directive's effect (a macro definition, a conditional branch decision, an `#include` push) is applied to mutable preprocessor state and the parser continues. Macros are expanded by the lexer itself (`macro.c`, `sub_676860`'s cache layer) rather than by the directive handler.

## Key Facts

| Property | Value |
|---|---|
| Source file | `preproc.c` (EDG 6.6) |
| Address range | `0x6F9310`--`0x6FE130` |
| Function count | ~36 |
| Master dispatcher | `pp_directive` (`sub_6FC940`, 5,047 bytes, 269 basic blocks, 53 callees) |
| Directive recognizer | `identify_pp_directive` (`sub_6F9470`, 1,211 bytes, leaf, 71 basic blocks) |
| Conditional skip | `skip_to_endif` (`sub_6FA1F0`, 1,517 bytes) |
| `#if` evaluator | `perform_if` (`sub_6FA7E0`, 614 bytes) |
| `#elif`/`#else` handler | `sub_6FAA50` (436 bytes) |
| `#endif` handler | `sub_6F9EB0` (334 bytes) |
| `#include` processor | `sub_6FAFD0` (1,072 bytes) |
| `#define` processor | `sub_6FB400` (1,233 bytes) |
| `#pragma` lookup | `look_up_pragma_id` (`sub_6FBA20`, 390 bytes) |
| Pragma string conversion | `convert_pragma_to_string` (`sub_6F9B00`, 621 bytes) |
| STDC pragma processor | `process_stdc_pragmas` (`sub_6FBCD0`, 223 lines) |
| GCC pragma processor | `process_gnu_pragmas` (`sub_6FC1F0`, 1,775 bytes, 91 basic blocks) |
| Directive jump table | `0xA88440` (24 entries × 8 bytes) |
| Directive ID encoding | 0--22 (22 = unknown) |
| If-stack head | `qword_12C6F98` (frame array), `qword_106B6D8` (depth) |
| Stack growth threshold | `qword_12C6F90` (capacity, grows by 30 entries) |
| Frame size | 12 bytes (8 = saved source position, 4 = flags) |

## Architecture

```text
Parser / lexer cache (sub_676860)
        │  encounters token '#' (word_126DD58 == 1)
        ▼
pp_directive (sub_6FC940)
        │
        ├─ identify_pp_directive (sub_6F9470)   ─→ directive ID 0..22
        │
        ├─ switch (directive_id):
        │     0: #if           → perform_if(1)            (sub_6FA7E0)
        │     1: #ifdef        → perform_if(1)            ─┐
        │     2: #ifndef       → perform_if(0)            ─┤  share entry
        │     3: #elif         → skip_to_endif(0)         ─┤
        │     5: #elifdef      → skip_to_endif(0)         ─┤
        │     6: #elifndef     → skip_to_endif(0)         ─┘
        │     4: #else         → sub_6FAA50(1)
        │     7: #endif        → sub_6F9EB0
        │     8: #include      → sub_6FAFD0(0)
        │     9: #define       → sub_6B3360  (in macro.c)
        │    10: #undef        → token + sub_734430 lookup + sub_72B510
        │    11: #line         → sub_6FB400(0)
        │    12: #error        → sub_679800 + sub_4F7A70(35)  → fall through to #pragma
        │    13: #pragma       → look_up_pragma_id (sub_6FBA20)
        │                         → enter_pending_pragma (sub_6F9B00)
        │                         → optional GCC dispatch (sub_6FC1F0)
        │    14: #             → null directive (case 0xE) - silently consumed
        │    15: #ident        → conditional + sub_6FB400(1)
        │    16: #assert       → sub_6B4E80
        │    17: #unassert     → sub_6B5240
        │    18: (reserved)    → identical to #assert path
        │    19: #             → default = error
        │    20: #include_next → sub_6FAFD0(1)
        │    21: #warning      → sub_4F8E30(1105, ...) + skip-to-EOL
        │    22: unknown       → sub_4F8160(7, 11) error message
        │
        ▼
   tail: skip residual tokens, restore lex flags, optional PCH event
```

The five conditional cases (`#elif`/`#elifdef`/`#elifndef`) collapse into the same handler block because the *true* branch always means "previous branch was taken, now skip everything until matching `#endif`." Only `#elif` with the `defined` operator differs at the *evaluation* level (handled inside `perform_if`), not at dispatch.

## Directive Recognition: `identify_pp_directive`

`identify_pp_directive` (`sub_6F9470`) is a leaf function — no calls, just string comparisons. It receives the global pair `(src, n)` (current token text and length, kind = identifier) and returns an integer directive ID:

| ID | Spelling | Notes |
|---:|---|---|
| 0 | `if` | length 2 |
| 1 | `ifdef` | length 5 |
| 2 | `ifndef` | length 6 |
| 3 | `elif` | length 4 |
| 4 | `else` | length 4 |
| 5 | `elifdef` | length 7, C++23/C23 gated |
| 6 | `elifndef` | length 8, C++23/C23 gated |
| 7 | `endif` | length 5 |
| 8 | `include` | length 7 |
| 9 | `define` | length 6 |
| 10 | `undef` | length 5 |
| 11 | `line` | length 4 |
| 12 | `error` | length 5 |
| 13 | `pragma` | length 6 |
| 14 | (null directive `# <newline>`) | reached when `word_126DD58 == 10` (newline immediately after `#`) |
| 15 | (assigned at dispatch) | |
| 16 | `ident` | C-mode only (`!dword_106C2C0`) |
| 17 | `assert` | C-mode only |
| 18 | `unassert` | C-mode only |
| 19 | (carriage-return form) | reached when `word_126DD58 == 13` |
| 20 | `include_next` | length 12, GNU extension |
| 21 | `warning` | length 7 |
| 22 | (unknown) | sentinel returned for any unrecognized identifier |

The function dispatches first by `n` (length) via a nested switch, then memcmps inside each length bucket. Lengths that match a single keyword exactly (2 → `if`, 5 → `ifdef`/`endif`) fall through to the `dword_106BBA8` gate for C23/C++23 alternative keywords. The function reads three configuration globals:

- `dword_106BBA8` — set when the active standard supports `#elifdef`/`#elifndef`
- `dword_106C2C0` — nonzero in C++ mode, suppresses C-only directives `#ident`/`#assert`/`#unassert`
- `dword_126EFB4` and `dword_126EF68` — language family (2 = C++) and standard version number, used to fine-tune the C++23 boundary at `202301` vs C23 at `202310`

> ⚡ **QUIRK — `#elifdef` is gated by *two* version checks, not one.** `identify_pp_directive` first checks `dword_106BBA8` (a derived flag set during command-line processing). If unset, it never recognizes the keyword. If set, it then peeks `dword_126EFB4 == 2 ? dword_126EF68 > 202301 : dword_126EF68 > 202310` inside the recognizer itself to confirm. The result is that even with a future EDG core supporting both C and C++23 conditional includes, you can disable the recognition entirely by clearing `dword_106BBA8` — and then the version test never runs. This belt-and-suspenders design exists because EDG ships one binary that supports both C and C++ standards independently.

The function never returns 15, which is reserved for the position 0xF in the dispatch table.

## The Master Dispatcher: `pp_directive`

`pp_directive` (`sub_6FC940`) is invoked from `sub_676860` (the lex cache) the moment a `#` token is seen at line-start. The function is structured in five phases:

### Phase 1 — Lex Mode Save

Saves the current values of `dword_106B708`, `dword_106B71C`, `dword_106B720` (lexer flags controlling expansion and concatenation), `qword_126EDE8` (current source position), and `qword_126DD38` (current token pointer). These flags are overridden to put the lexer into *directive mode* — macros are not expanded, whitespace is significant, and certain tokens (e.g., `<...>` in `#include`) are scanned as header-name strings:

```c
dword_106B718 = 1;     // in_directive
dword_106B720 = 1;     // suppress macro expansion of next ident
dword_106B71C = 0;     // not in normal scan
dword_106B708 = 0;     // not in defined() argument
dword_106B6E0 = 0;
dword_106B6F4 = 1;
dword_106B6F8 = 1;
++*(_BYTE *)(qword_126DB48 + 18);  // bump preprocessor recursion depth
```

The recursion depth byte at offset 18 of `qword_126DB48` matters because `#include` recursively re-enters `sub_676860`, which can recursively call `pp_directive` — the increment ensures the inner invocations know they are nested.

### Phase 2 — Directive Identification

```c
sub_676860();                          // consume the directive name token
if (word_126DD58 == 10)         v6 = 14;   // newline → null directive
else if (word_126DD58 == 13)    v6 = 15;   // carriage-return form
else if (word_126DD58 == 1)     v6 = sub_6F9470();   // identifier → lookup
else                            v6 = 22;   // anything else → unknown
```

The two whitespace-only cases (14 = blank `#`, 15 = `#\r`) exist because both are required by C/C++ as legal *null directives* that must be silently consumed without error. They cannot be entries in the directive table because there is no token text to look up.

### Phase 3 — PCH Header-Stop Detection

Before dispatching, `pp_directive` checks whether the current location is the "PCH header stop" — the point at which precompiled header generation should freeze the state. The check at `0x6FCABF` compares the current source file/line stamp against `qword_106B6A0` (the stop position remembered from a prior `#pragma hdrstop`). On match, the directive is *consumed* but its effect is dropped and `dword_106B684` is left set so `pch_fixup_part_2` can later replay it from the PCH stream.

This path also detects the `#pragma hdrstop` and `#pragma no_pch` directives by their *text*, not by their dispatch ID — because at the moment of detection the pragma kind has not yet been resolved. The seven-byte memcmp `"hdrstop"` is hardcoded at offset `0x6FCC50`, and the six-byte `"no_pch"` at offset `0x6FCC68`.

### Phase 4 — The Switch

The dispatch is a `switch (v6)` with 23 cases plus a default. The compiler lowered it to a jump table at `0xA88440` (24 × 8-byte entries — 23 cases plus the default sink at offset `0x6FD4A5`). Disassembled targets:

| `v6` | Target | Directive |
|---:|---|---|
| 0 | `0x6FD240` | `#if` — complex evaluator (see Phase 4a) |
| 1 | `0x6FD230` | `#ifdef` — `perform_if(1)` |
| 2 | `0x6FD220` | `#ifndef` — `perform_if(0)` |
| 3 | `0x6FD3E0` | `#elif` — shares code with 5, 6 |
| 4 | `0x6FD3D0` | `#else` — `sub_6FAA50(1)` |
| 5 | `0x6FD3E0` | `#elifdef` |
| 6 | `0x6FD3E0` | `#elifndef` |
| 7 | `0x6FD028` | `#endif` — `sub_6F9EB0` |
| 8 | `0x6FD038` | `#include` — `sub_6FAFD0(0)` |
| 9 | `0x6FD210` | `#define` — `sub_6B3360` (in `macro.c`) |
| 10 | `0x6FD0E0` | `#undef` — inline path |
| 11 | `0x6FCFF0` | `#line` — `sub_6FB400(0)` |
| 12 | `0x6FD48F` | `#error` — emits and falls through to `#pragma` path |
| 13 | `0x6FCEE0` | `#pragma` — `look_up_pragma_id` → `enter_pending_pragma` / `process_gnu_pragmas` |
| 14 | `0x6FCCC0` | (null directive `# <newline>`) — silently consumed |
| 15 | `0x6FD050` | C++-mode `#define` — diagnostic 518, then `sub_6FB400(1)` |
| 16 | `0x6FCEA0` | C++-mode `#undef` — diagnostic 518, then pragma machinery |
| 17 | `0x6FCE80` | C-mode `#assert` — `sub_6B4E80` |
| 18 | `0x6FCE60` | C-mode `#unassert` — `sub_6B5240` |
| 19 | `0x6FD4A5` | (carriage-return form, default sink) |
| 20 | `0x6FD000` | `#include_next` — `sub_6FAFD0(1)` |
| 21 | `0x6FD090` | `#warning` — diag 1105, skip rest of line |
| 22 | `0x6FD070` | (unknown) — diag 11, set `dword_106B6E0 = 1` |
| (default) | `sub_4F2930("preproc.c", 4834, "pp_directive", "pp_directive: bad pp directive code")` |

The same directive ID often maps to different basic blocks depending on language mode — for example, ID 11 (`#line`) always reaches `0x6FCFF0`, but ID 15 reaches the C++-specific `0x6FD050` which emits diagnostic 518 ("invalid `#` directive in C++ mode") before delegating to the same `sub_6FB400` handler with a different first argument. This is how EDG enforces C-only directives: it accepts the syntax, then complains about the semantics.

### Phase 4a — The Inlined `#if defined(...)` Fast Path

Case 0 (`#if`) is the only case that the compiler chose *not* to delegate to `perform_if`. Instead it spans `0x6FD240`--`0x6FD3D0` (~400 bytes) and implements a hand-rolled recognizer for the common pattern `#if defined(IDENT)` — skipping the full expression parser when it can. The path:

1. Peek the next token (`sub_66B8A0`).
2. If it is `!` (token kind from char `33`), set `v41 = 1` (negation flag) and advance.
3. Memcmp the next identifier against the 7-byte literal `"defined"`.
4. If the next char is `(`, recognize as `#if defined(...)`.
5. Scan an identifier inside the parens (the `& 0xDF` mask is the ASCII-fold trick to recognize letters case-insensitively, but here it actually filters non-zero non-tab characters as a generic identifier-byte test).
6. Match closing `)`.
7. If the next byte is `\0`, the entire `#if` consists of just `defined(X)`. Look up X via `sub_6B5B00`. If found, push a frame with bit `8` set in flags (8 = `defined-positive`); if negated, set bit `4` instead.

If any step fails, the function jumps to `LABEL_186` and falls through to the general path, which calls `sub_5E1A80` to allocate an expression evaluator context, `sub_676860` to scan more tokens, `sub_52C970` ("`scan_pp_expression`" assertion), `sub_461980` to evaluate the resulting expression, and `sub_5E1B70` to release the evaluator.

The hand-inlined fast path exists because `#if defined(X)` is the single most common `#if` form in real C/C++ headers, and bypassing expression allocation is measurably faster.

### Phase 5 — Tail Cleanup

After the dispatch, three things must happen no matter which directive ran:

1. Consume any remaining tokens up to the newline (`while (word_126DD58 != 10) sub_676860(...);`). If a remaining token would be ignored, emit diagnostic 14 (`"extra tokens at end of #... directive"`) first.
2. Restore the saved lex flags (`dword_106B720`, `dword_106B71C`, `dword_106B708`) and source position (`qword_126EDE8`).
3. Call `sub_6F7660` — the post-directive callback that flushes pending `_Pragma` records and updates the PCH event stream. If the PCH header-stop flag was set during phase 3, additionally invoke `sub_6F3CE0` (`"Cannot generate precompiled header: %s\n"` finalizer) and `sub_6F4950` (`"header_stop_no_longer_pending"`).

The decrement `--*(_BYTE *)(qword_126DB48 + 18)` (line 899 in decompilation) balances the recursion-depth bump from phase 1. If this byte underflows, the next directive's `pp_directive` will misinterpret nested-context flags and emit phantom diagnostics; the bump/decrement asymmetry is the most fragile invariant in the function.

## Conditional Compilation: The If-Stack

`#if`, `#ifdef`, `#ifndef` push a 12-byte frame onto a stack rooted at `qword_12C6F98`. The depth is `qword_106B6D8`. The capacity is `qword_12C6F90`. Each frame:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 8 | Source position stamp (`qword_12C6F88`) of the directive |
| 8 | 4 | Frame flags: bit 0 = condition was true, bit 1 = was a `defined`-form, bit 2 = inside a system header |

When the stack hits capacity (`qword_106B6D8 + 1 == qword_12C6F90`), `sub_6B76D0` reallocates by 30 entries — not a doubling, but a fixed linear growth. The realloc size in bytes is computed as `12 * (qword_106B6D8 + 31) - 360`, which is an obfuscated way of writing "(old_size + 30) entries × 12 bytes, minus the previously-allocated 30 × 12 = 360 bytes" — i.e., the *additional* bytes needed. The result is passed to `sub_6B76D0`, EDG's `realloc_general`, which logs `"realloc_general:"` and `"malloc_with_check: allocating %lu at %p, total = %lu"` when the memory-trace flag is on.

`skip_to_endif` (`sub_6FA1F0`, 1,517 bytes) walks the token stream looking for matching `#endif`/`#elif`/`#else` while respecting *nested* conditionals. It pushes additional frames during the scan and pops them on encountering inner `#endif`, never disturbing the *outer* `pp_if_stack_depth`. The function emits `"push, pp_if_stack_depth = %ld\n"` when the trace flag `dword_126EFCC > 2` is set — the same trace also fires from `perform_if` and from the inlined fast path of `pp_directive` case 0.

`perform_if` evaluates the condition argument that arrives as parameter `a1` (1 = `#ifdef`/`#if`, 0 = `#ifndef`). It calls `sub_6FA000` if `__VA_ARGS__` or `__VA_OPT__` appear in the expression (these can legally appear inside `#if defined(__VA_ARGS__)` checks inside macro replacement lists), and either pushes a frame with the condition's truth value or, if false, immediately calls `skip_to_endif`.

> ⚡ **QUIRK — the if-stack grows in fixed 30-entry chunks, not by doubling.** Every realloc allocates exactly 30 additional frames (360 bytes). For a translation unit with deeply nested conditionals — say, `boost::preprocessor` macro expansions — this causes O(N) reallocations rather than O(log N). The cost is not visible in normal compilation because typical TUs never exceed 10--20 nested `#if`s, but pathological generated code can trigger thousands of realloc syscalls. This is also why `qword_12C6F90` starts at 30, not at 1 or 8: EDG pre-pays the first chunk in `sub_6FE130` (`preproc_pool_init`).

## `#include` Processing

`sub_6FAFD0` (`process_include`, 1,072 bytes, 46 basic blocks) handles both `#include` and `#include_next` — the parameter `a1` (0 vs 1) selects the search path. Recognizable strings inside the function: `"stdarg.h"` and `"cstdarg"`. These two header names are *special-cased* because they trigger EDG's built-in compiler-supplied `va_list` machinery rather than reading an actual file.

The function calls into `sub_6FADF0` (the include-path resolver) which iterates the `-I` directory list (`dword_126DDE8`/`dword_126E49C` are the head and length of the include-dir vector). Path resolution honors:

- Quote-form (`#include "..."`) searches the directory of the current source file first, then the system list
- Bracket-form (`#include <...>`) starts from the system list
- `#include_next` skips ahead in the same list until past the *current* file's directory

When a header has already been seen and is `#pragma once`-marked, the include is silently dropped. The lookup is keyed off `dword_106B6C0` (multiple-include guard table), which is reset to 0 in `phase 1`.

> ⚡ **QUIRK — `stdarg.h` and `cstdarg` are recognized by name, not by content.** A user header named `stdarg.h` anywhere on the include path will be entered by `process_include` exactly like the real one, but at the *handler* level inside `sub_6FAFD0` the name match short-circuits to use EDG's built-in declarations. This means a project that ships its own `stdarg.h` will get **two** definitions of `va_list` (one synthesized, one from the file), and the second will fail with a redeclaration diagnostic. The only escape is to name the shim differently or to use a `--no_stdinc` build option, which removes the special case entirely. The same is true for the C++ wrapper `cstdarg`.

## `#define` and Macro Processing

`#define` dispatches into `sub_6B3360` (in `macro.c`, at `0x6B3360`), not into `preproc.c`. The wrapper at `0x6FD210` is a single tail call. Macro storage, parameter parsing, and the macro-table hash all live in `macro.c`. The preprocessor's role is limited to:

1. Reading the macro name token after the `#define` keyword.
2. Determining whether the `(` immediately following (no whitespace) is the start of a parameter list (function-like macro) or just part of the replacement text (object-like macro).
3. Handing the line range to `macro.c` for table insertion.

The C++23 `__VA_OPT__` keyword and the C99 `__VA_ARGS__` identifier are recognized as *reserved* macro-parameter names at `0x6FA000` (the parameter-list parser, 87 lines). When seen, they emit diagnostic 969 (`__VA_ARGS__`) or 2939 (`__VA_OPT__`) if used outside a variadic macro definition. The two diagnostic numbers also appear in `look_up_pragma_id` (`sub_6FBA20`) at `0x6FBB59` and `0x6FBB8B` — the same recognizer is shared between macro definitions and pragma argument parsing.

`#undef` (case 10) is the only case that does *not* delegate to `macro.c`. Instead it inlines the lookup-and-unlink:

```c
sub_676860();                                         // consume name
v21 = sub_734430(name, len, &xmmword_106C380);        // macro-table lookup
if (v21 != NULL) {
    if (*(v21 + 88) & 2) sub_4F8160(7, 45);           // can't undef predefined
    else {
        sub_72B510(4, v21, &qword_126DD38, 1);        // detach symbol entry
        sub_746930(v21);                              // free macro body
    }
}
```

Diagnostic 45 (`"cannot undefine predefined macro"`) is suppressed when `dword_126EFA8` is set (the `--no_undefined_check` flag).

## `#pragma` Routing

When `pp_directive` reaches case 13, control passes to `look_up_pragma_id` (`sub_6FBA20`) which walks the linked list rooted at `qword_106B8A8` — the pragma kind registry built by `pragma_init` (covered in detail in the [Pragma Engine](pragma-engine.md) page). The function reads the next token (kind expected to be 1 = identifier) and linear-searches the list, comparing `strlen` and `strncmp` against each registered kind name fetched from `&off_E6CDE0`.

The lookup has one special hop: if the matched kind is *28* (the `diagnostic` family head), it consumes one more token and checks whether it spells `"diagnostic"` again. If not, an assertion fires at `preproc.c:2964`:

```c
if (v7 == 28) {
    sub_679800();
    if (memcmp(qword_126DDA0, "diagnostic", 10))
        sub_4F2930("preproc.c", 2964, "look_up_pragma_id", 0, 0);
}
```

This guard ensures the GCC compound pragma `#pragma GCC diagnostic ...` cannot be misinterpreted as `#pragma diagnostic_push` or similar; the registry chains them through kind 28 → kind 29 explicitly.

After the kind is identified, `pp_directive` calls one of three paths:

- **GCC dispatcher** (`process_gnu_pragmas` at `0x6FC1F0`) — if the kind is 28 (GCC umbrella). Handles `system_header`, `diagnostic push/pop`, `diagnostic [warning|error|ignored]`, `ivdep`, `target(...)`, and `device-hidden-visibility` (the last is a CUDA-only extension recognized inside the GCC dispatcher). The function is 1,775 bytes, 91 basic blocks, and consumes 16 unique strings — the longest dispatcher in the preprocessor file.
- **STDC dispatcher** (`process_stdc_pragmas` at `0x6FBCD0`) — if the kind is the STDC family head. Recognizes `FP_CONTRACT`, `FENV_ACCESS`, `CX_LIMITED_RANGE` (each one byte in BSS: `byte_126E55A`, `byte_126E559`, `byte_126E558`). Values are `1`=OFF, `2`=ON, `3`=DEFAULT.
- **Generic queue** (`enter_pending_pragma` at `0x6F9B00`) — everything else. The pragma is converted to its string form by `convert_pragma_to_string` and appended to the deferred-pragma list.

## C++ Module Pragmas

`sub_6FBFE0` (132 lines) handles a small CUDA/C++20 module-pragma family that recognizes the tokens `"begin"` and `"declare"` after a module name. This is the *only* surviving C++20 module functionality in the binary — the rest of `modules.c` is stubbed out. The function is called from the GCC dispatcher when the umbrella keyword resolves to a module-related kind, and its output is to set a flag in `dword_106C29C` (the module-mode global) which causes downstream parsing to apply module-export semantics.

In practice, this code is reachable but unused in normal `nvcc` invocations — the driver never passes `-fmodules` or its EDG equivalent. The presence of the recognizer in the binary is a vestige of EDG's broader C++20 work; CUDA compilation paths short-circuit before any of it activates.

## CUDA-Specific Hooks

The preprocessor itself contains only one CUDA-specific function: `attach_target_pragma_attribute` at `0x6FC110` (51 lines), invoked from `process_gnu_pragmas` when the GCC umbrella resolves to `target(...)`. It walks the parsed target string for CUDA-meaningful tokens (`__shared__`, `__constant__` appear in the called function `sub_72B510`'s string set) and attaches them as IL attributes on the *next* declaration — the deferred binding mechanism described in the [Pragma Engine](pragma-engine.md).

The `device-hidden-visibility` string inside `process_gnu_pragmas` (`0x6FC1F0`) is the CUDA equivalent of `__attribute__((visibility("hidden")))` but applied through the pragma machinery so that wrapping `#pragma GCC visibility push(hidden)` around device-side declarations produces the correct PTX symbol-export bits. The recognizer is at approximately `0x6FC4..` and sets a bit in the GCC-pragma stack frame, not in the IL directly — the IL attribute is attached later by `attach_target_pragma_attribute`.

## State Globals

| Global | Width | Purpose |
|---|---|---|
| `dword_106B708` | 4 | Lex flag: `1` = inside `defined()` argument, suppresses macro expansion |
| `dword_106B718` | 4 | `1` = currently inside a directive (set by `pp_directive` phase 1) |
| `dword_106B71C` | 4 | Lex flag: `1` = scanning header name (`<...>`) |
| `dword_106B720` | 4 | Lex flag: `1` = next identifier is suppressed from macro expansion |
| `dword_106B6E0` | 4 | `1` = current directive errored; skip rest of line silently |
| `dword_106B6F4` | 4 | `1` = pass directive through to preprocessor output (`-E` mode) |
| `dword_106B6F8` | 4 | `1` = preserve whitespace in passed-through tokens |
| `qword_106B6D0` | 8 | Base of *outer* if-stack frame (immune to `skip_to_endif`) |
| `qword_106B6D8` | 8 | Current if-stack depth |
| `qword_12C6F90` | 8 | If-stack capacity (grows in 30-frame chunks) |
| `qword_12C6F98` | 8 | If-stack base pointer |
| `qword_12C6F88` | 8 | Source position of *most recent* if-stack push |
| `dword_106B684` | 4 | `1` = PCH header-stop pending (suppress directive effects) |
| `dword_106B690` | 4 | `1` = PCH writeout mode |
| `dword_106B694` | 4 | `1` = PCH replay mode |
| `dword_106B6B0` | 4 | `1` = PCH active (either write or replay) |
| `dword_106C294` | 4 | `1` = `--generate_pp_output` (preprocess-only mode) |
| `dword_106C2C0` | 4 | `1` = C++ language mode |
| `dword_106C2B0` | 4 | `1` = relaxed mode (suppresses "extra tokens" diagnostic 14) |
| `dword_126EFC8` | 4 | Trace level: when set, `pp_directive` emits enter/exit traces |
| `dword_126EFCC` | 4 | Verbose trace level (>2 = print stack-depth changes) |
| `byte_126E558` | 1 | `#pragma STDC CX_LIMITED_RANGE` state (1/2/3) |
| `byte_126E559` | 1 | `#pragma STDC FENV_ACCESS` state |
| `byte_126E55A` | 1 | `#pragma STDC FP_CONTRACT` state |

## Initialization and Reset

Six functions handle preprocessor lifecycle:

| Function | Address | Role |
|---|---|---|
| `preprocessor_one_time_init` (`sub_4B37F0`) | `0x4B37F0` | Called from `fe_init` at startup (step 4 of 36). Calls into `sub_6FE130` and friends to allocate the initial 30-frame if-stack and zero the pragma registry pointer. |
| `sub_6FDD00` | `0x6FDD00` | If-stack init — assigns `qword_12C6F98` from the arena, sets `qword_12C6F90 = 30`. |
| `sub_6FDF00` | `0x6FDF00` | Stats dump — `"Preprocessing table use:"` and `"GCC pragma stack entries"`. Called from `fe_wrapup` when stats are enabled. |
| `sub_6FDFF0` | `0x6FDFF0` | Register the preprocessor's mutable globals (the 21 entries listed above) with the PCH save/restore subsystem. |
| `sub_6FE050` | `0x6FE050` | Reset — zeroes 21 globals. Called between source files in multi-TU runs. |
| `sub_6FE130` | `0x6FE130` | Pool init — allocates the GCC-pragma stack base (`qword_12C6F60`) and the directive output buffer (`qword_12C6F78`). |

The reset function `sub_6FE050` does **not** free the if-stack frames. It only zeros the depth counter, leaving the allocated buffer intact for reuse. This is correct because EDG's arena allocator (`sub_6BA0D0` / `sub_6B76D0`) is reset wholesale between TUs by the upper layer; the preprocessor relies on that bulk reset rather than tracking individual buffers.

## Address Range Map

| Range | Approx. size | Owner |
|---|---|---|
| `0x6F9310`--`0x6F9470` | 350 B | Token-processing helpers (shared with lexer) |
| `0x6F9470`--`0x6F992B` | 1,211 B | `identify_pp_directive` |
| `0x6F992B`--`0x6F9B00` | ~470 B | `convert_pp_directive_to_string` |
| `0x6F9B00`--`0x6F9D70` | 621 B | `convert_pragma_to_string` / `enter_pending_pragma` |
| `0x6F9D70`--`0x6F9EB0` | 320 B | GCC pragma stack push/pop helper |
| `0x6F9EB0`--`0x6FA000` | 334 B | `#endif` handler |
| `0x6FA000`--`0x6FA1F0` | 500 B | `__VA_ARGS__` / `__VA_OPT__` parameter recognizer |
| `0x6FA1F0`--`0x6FA7E0` | 1,517 B | `skip_to_endif` |
| `0x6FA7E0`--`0x6FAA50` | 614 B | `perform_if` |
| `0x6FAA50`--`0x6FAC20` | 436 B | `#elif`/`#else` handler |
| `0x6FAC20`--`0x6FADF0` | 470 B | Macro argument collection helper |
| `0x6FADF0`--`0x6FAFD0` | 480 B | Include path resolution |
| `0x6FAFD0`--`0x6FB400` | 1,072 B | `process_include` |
| `0x6FB400`--`0x6FB8E0` | 1,233 B | `#define` / `#line` body parser |
| `0x6FB8E0`--`0x6FBA20` | ~600 B | Token peek/get/wrap helpers |
| `0x6FBA20`--`0x6FBBB0` | 390 B | `look_up_pragma_id` |
| `0x6FBBB0`--`0x6FBCD0` | ~280 B | Pragma-to-IL wrappers |
| `0x6FBCD0`--`0x6FBFE0` | 800 B | `process_stdc_pragmas` |
| `0x6FBFE0`--`0x6FC110` | ~300 B | Module pragma recognizer |
| `0x6FC110`--`0x6FC1F0` | 226 B | `attach_target_pragma_attribute` |
| `0x6FC1F0`--`0x6FC8E0` | 1,775 B | `process_gnu_pragmas` |
| `0x6FC8E0`--`0x6FC940` | ~100 B | PCH event wrappers |
| `0x6FC940`--`0x6FDCFF` | 5,047 B | `pp_directive` (master dispatcher) |
| `0x6FDD00`--`0x6FE130` | ~1,070 B | Init/stats/reset support |

Total: approximately 20 KB of code across the 36 attributed functions, plus an additional ~5 KB of helpers that nominally belong to `lexical.c`/`macro.c` but are invoked exclusively from preprocessor paths.

## Cross-References

- The dispatcher `pp_directive` is called from `sub_676860` (the lex cache, in `lexical.c`) — see [Lexer & Tokenizer](lexer.md) for the `#`-recognition path.
- `look_up_pragma_id` and `enter_pending_pragma` hand off to the [Pragma Engine](pragma-engine.md) for the deferred per-construct binding lifecycle.
- The PCH header-stop machinery feeds into the PCH event log managed by `pch.c` (functions `sub_6F39A0` `add_pch_event`, `sub_6F4A10` `pch_fixup_part_2`).
- The `process_gnu_pragmas` GCC diagnostic-stack handlers are bridged to the [SARIF & Pragma Diagnostic Control](../diagnostics/sarif-pragmas.md) layer through the same descriptor pool documented in pragma-engine.
- Standard-version gating (`dword_126EFB4`, `dword_126EF68`, `dword_106BBA8`) is shared with the rest of the language-mode infrastructure; see [Experimental Flags](../config/experimental-flags.md).
