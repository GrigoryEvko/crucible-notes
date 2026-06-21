# The Pragma Engine

cudafe++ inherits from EDG 6.6 a unified directive registry that handles every `#`-prefixed preprocessor directive *and* every `#pragma` variant through a single object pool, lifecycle pipeline, and dispatch table. The registry is a 13-function cluster at addresses `0x6F61B0`–`0x6F8320`, with deferred per-directive processing routed through the preprocessor subsystem (`enter_pending_pragma` at `0x6F9B00`, `look_up_pragma_id` at `0x6FBA20`). The same 24-byte `pragma_kind_description` struct represents `#if`, `#include`, `#pragma pack`, `#pragma nv_diag_suppress`, `_Pragma("...")` and `__pragma(...)`, and the same 120-byte `pending_pragma` is the in-flight container during parsing. A 56-slot dispatch table at `0xD55F08` (`funcs_6F71AE` in IDA's naming) routes each immediate-binding pragma to its semantic handler. This page reconstructs that engine end to end. Confidence: **HIGH** for the registry layout, dispatch table, lifecycle, and kind name strings (all derived from `pragma_init` at `0x6F8320` and `look_up_pragma_id`); **MEDIUM** for the precise meaning of the eight bit-flags in the descriptor (inferred from caller behavior).

For the diagnostic-control sub-language (`#pragma nv_diag_suppress`, `nv_diag_push`/`pop`), see [SARIF & Pragma Diagnostic Control](../diagnostics/sarif-pragmas.md). For the GCC pragma stack used by the host-output emitter (`#pragma GCC diagnostic push/pop` injected into `.cudafe1.cpp`), see the same page. The present document covers the *engine* that registers, parses, queues, dispatches, and embeds every directive.

## Architecture at a Glance

```text
                       ┌──────────────────────────────┐
   pragma_init  ──────▶│  Kind registry (linked list) │
   sub_6F8320          │  qword_106B8A8 ─→ kind1 ─→…  │
                       │  qword_106B740[name]= kind*  │
                       └──────────────┬───────────────┘
                                      │  look_up_pragma_id
                                      ▼
   token stream  ─────▶  enter_pending_pragma  ─────▶  pending queue
   ('#pragma X y')       sub_6F9B00                     qword_106B8A0
                              │
                              │  (binding kind decides what happens)
                              ▼
   ┌────────────────┬──────────────────┬──────────────────────────┐
   │   immediate    │    construct     │    declaration / stmt    │
   │   (bind=3)     │    (bind=2)      │    (bind=5 / bind=8)     │
   │                │                  │                          │
   │ funcs_6F71AE[] │ add_pragma_to_il │ deferred until next decl │
   │ ↓ dispatch     │  (0x6F66B0)      │  process_curr_construct  │
   │ pack/diag/...  │                  │   pragmas (0x6F7BB0)     │
   └────────────────┴──────────────────┴──────────────────────────┘
```

Three lifecycle layers exist in parallel because the C++ grammar lets a pragma appear anywhere a token can — between tokens of a single declaration, between declarations, inside a function body, at file scope, and inside `_Pragma()` and `__pragma()` operators that are themselves part of an expression. EDG's solution is to allocate the descriptor at recognition time, queue it on `qword_106B8A0`, and resolve its binding (which construct it attaches to) only when the parser knows the next construct's identity.

## The Kind Descriptor — 24 Bytes

Every pragma kind and every preprocessor directive shares the same descriptor record allocated by `pragma_init` at `0x6F8320`:

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 8 | `next` | Next descriptor in linked list (`qword_106B8A8` is head) |
| 8 | 1 | `kind_id` | Stable 1-byte identifier (1–42 for built-ins; 0x37 max for handlers) |
| 9 | 3 | (padding) | |
| 12 | 4 | `category` | Coarse classifier (1=core directive, 2=conditional, 3=NV-extension, 5=immediate-pragma) |
| 16 | 1 | `name_index` | Index into the pragma-name string array (also indexes `funcs_6F71AE`) |
| 17 | 1 | `attr_flags` | Bit field: `0x01` = preserve in IL, `0x04` = bind to construct, `0x08` = run immediately, `0x10` = stack-managed, `0x20` = mode-conditional |
| 18 | 1 | `binding_mode` | Lower 5 bits encode binding: `8`=block-scope, `9`=any-scope, `0xA`=global-only, `0xC`=token-bound, `0xD`=immediate-after-recognition |
| 19 | 1 | `token_kind` | Token class assigned at lex time (3=`#`-keyword, 5=identifier-form, 8=function-form like `nv_diag_suppress(...)`) |

The descriptor's `name_index` field is the most important coupling: it indexes simultaneously into the pragma-name string table (the values reported by `look_up_pragma_id`) and into the immediate-dispatch table `funcs_6F71AE[]` (described below). This is why the registry can be unified — every directive maps to one slot, and the slot determines both its spelling and its handler.

The bit pattern at offset 17 is the most surprising part of the layout. Selected encodings extracted from `pragma_init`:

```text
   kind_id  name                  off17    off19    binding
   ─────────────────────────────────────────────────────────────
       1   if                     0x01      5       conditional / token
       5   elifndef               0x02      3       conditional / immediate
       6   endif                  0xA1      8       conditional / block
       8   define                 0x78      8       macro definition
       9   undef                  0x72      5       macro removal
      13   pragma                 0x58      3       meta-directive
      14   include                0x58      3       file-inclusion
      22   error                  0x90      3       fatal directive
      23   warning                0x90      3       conditional fatal
      27   ident                  0x04      8       legacy
      40   pack                  0x88      3       stack-managed pragma
```

> ⚡ **QUIRK — kind 14 (`include`) and kind 13 (`pragma`) share the same flags**
> Both have `off17 = 0x58` and `off19 = 3`. They are siblings in the registry because EDG treats `#include` as a *meta-directive that produces tokens* and `#pragma` as a *meta-directive that produces a pragma list*. The distinction lives entirely in the handler at `funcs_6F71AE[name_index]`, not in the descriptor. A consequence: when `pp_directive` (the dispatcher at `0x6FC940`) reaches its switch on `kind_id`, the two cases call entirely different code paths despite having identical descriptor flags.

## Registration: `pragma_init` (0x6F8320)

The registry is built once during `il_one_time_init`. Its assertion fingerprint is the string `"il_one_time_init: incorrect initialization of pragma_ids"` (`0xA62438`) which fires if `qword_106B8A8` is non-NULL on entry. The function allocates 24 bytes per kind via `sub_6BA0D0` (the global 8-byte-aligned arena), increments `qword_106B728` (the descriptor counter), and prepends to the linked list rooted at `qword_106B8A8`:

```c
void pragma_init(void) {
    if (trace_enabled) trace_enter(3, "pragma_init");
    qword_106B8A8 = NULL;                    // empty list
    memset(qword_106B740, 0, 0x158);         // kind table (43 slots × 8B)

    for (int k = 1; k <= 42; k++) {
        if (qword_106B740[k])                // re-init guard
            assertion("pragma.c", 125,
                      "add_pragma_kind_description",
                      "add_pragma_kind_description: duplicate pragma kind");

        descriptor *d = arena_alloc(24);
        d->kind_id      = k;
        d->category     = category_table[k];
        d->name_index   = name_index_table[k];
        d->attr_flags   = attr_table[k];
        d->binding_mode = binding_table[k];
        d->token_kind   = token_kind_table[k];
        d->next         = qword_106B8A8;     // prepend
        qword_106B8A8   = d;
        qword_106B740[k] = d;                // direct-index cache
        qword_106B728++;
    }
}
```

The `qword_106B740` array provides O(1) lookup by `kind_id` (used by `alloc_pending_pragma` to find the descriptor for the kind just recognized), while the linked list rooted at `qword_106B8A8` is what `look_up_pragma_id` walks for spelling-based lookup. Both are kept in sync.

Several kinds are registered only conditionally:

- Kinds 15, 16, 17 (the C++ module directives) — only if `dword_126EFB4 == 2` (C++ mode).
- Kind 19 (`__attribute__((deprecated))` / Apple legacy) — only if `dword_106BE00` is set.
- Kind 25 (C++11 attributes) — only in C++ mode with `__cplusplus >= 201102`, *and* not when `dword_106C0D4` (strict-mode flag) is non-zero.
- Kinds 29, 28 (the `__has_include`-style extensions) — only when `dword_126EFA8` is set *and* `qword_126EF98 > 0x9D07` (the EDG language-version cookie for C2X).
- Kinds 40, 41 (the modern `nv_diag_*` variants vs the legacy `pragma nv_diag` form) — controlled by `dword_126EFA4` and a separate version cookie at `0x9DD2`.
- Kind 42 (the catch-all "unrecognized" sink) — always last.

> ⚡ **QUIRK — the 56-slot dispatch table is sparse, with two `nullsub` poison entries**
> The immediate-dispatch table at `0xD55F08` has exactly 56 slots (`name_index` ranges 0–0x37). Slots 0x44–0x46 (indices 68–70 by raw offset) are `nullsub_3` and `nullsub_6` — IDA's name for synthetic no-op functions. They are not dead code: they exist so that the dispatcher (`funcs_6F71AE[name_index]`) never indirects through a NULL pointer. The handler at `process_immediate_pragmas` checks `if (v6) v6(v1);` after the load — so the nullsub variant and a NULL handler would behave identically; the nullsub exists to give a clean stack trace ("pragma kind has no immediate handler") rather than a `(nil)` PC in a crash dump.

## The 120-Byte `pending_pragma`

When the lexer recognizes a `#pragma` token (or a `_Pragma(...)` operator, or a `__pragma(...)` Microsoft extension), `alloc_pending_pragma` (`sub_6F6540`) builds a 120-byte record. Reusable records are kept on a freelist at `qword_106B738` to avoid allocator churn on translation units that contain many pragmas (a common case with CUDA's `#pragma unroll` heavy code).

Layout:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 8 | `next` | Next pending pragma in queue (`qword_106B8A0` is head) |
| 8 | 8 | `kind_desc` | Pointer to the 24-byte descriptor |
| 16 | 48 | `arg_buffer` | In-place token buffer; init'd by `sub_668BF0(this+16, 1)` |
| 64 | 8 | `position_start` | Source-sequence cookie at `#` |
| 72 | 8 | `position_end` | Source-sequence cookie at trailing newline |
| 80 | 8 | `bound_entity` | Resolved target (function, decl, statement) — NULL until binding |
| 88 | 1 | `state_flags` | Lower nibble = state machine (`0`=fresh, `1`=parsed, `3`=bound, `8`=processed). Upper 3 bits = sub-flags |
| 89 | 7 | (padding) | |
| 96 | 8 | `il_entry` | Pointer to embedded IL pragma node — see `add_pragma_to_il` |
| 104 | 8 | `next_in_construct` | Chains construct-bound pragmas for one declaration |
| 112 | 4 | `column_cookie` | Synthetic column for binary-search ordering |
| 116 | 4 | `reserved` | |

The `arg_buffer` at offset 16 is a 48-byte inline allocation managed by `sub_668BF0` (a small-string-optimisation–style container shared with the symbol-name and macro-arg subsystems). Pragmas whose argument list fits in 48 bytes (the overwhelming majority — `unroll 16`, `nv_diag_suppress 20012`, `pack(push, 8)`) need no spilling; larger argument lists call `sub_5E0600` to copy into the per-pragma-string arena at `qword_12C6F70`.

```c
pending_pragma *alloc_pending_pragma(uint8_t kind_id, source_pos *pos) {
    pending_pragma *p = qword_106B738;       // freelist head
    if (p) qword_106B738 = p->next;
    else { p = arena_alloc(120); qword_106B730++; }

    p->next             = NULL;
    sub_668BF0(p + 16, 1);                   // init inline buffer
    p->kind_desc        = qword_106B740[kind_id];
    p->il_entry         = NULL;
    p->next_in_construct = NULL;
    p->bound_entity     = NULL;
    p->position_start   = qword_126EFB8;     // current source cookie
    p->position_end     = qword_126EFB8;
    p->state_flags      = (p->state_flags & 0xF0) | 1;     // state = parsed

    uint8_t k = p->kind_desc->kind_id;
    if (k > 0x2A || ((1ULL<<k) & ~0x7FFCFFFFFEEULL) == 0) {
        if (((1ULL<<k) & 0x30000000) == 0 && ((1ULL<<k) & 0x10) == 0)
            assertion("pragma.c", 495,
                      "alloc_pending_pragma",
                      "alloc_pending_pragma: bad pragma kind");
    }
    /* ... append to qword_106B8A0 queue ... */
    dword_126DB74 = 1;                       // "pending work" flag
    return p;
}
```

The two bit-masks `0x7FFCFFFFFEE` and `0x30000000` together encode the set of legal pragma kinds at this allocation site. They split into:

- `0x7FFCFFFFFEE` (the "always-legal" mask): every kind except 0, 4, 27–30 and 35–42.
- `0x30000000` (the "scope-init-required" mask): kinds 28 and 29 — these are the `__has_include` family and need additional initialisation via `sub_5E7560` before being queued.
- The `& 0x10` test: kind 4 (`elif`) — also legal but with the column cookie cleared.

> ⚡ **QUIRK — `pending_pragma` records are reused across the entire compilation**
> `qword_106B738` is never trimmed. Once `qword_106B730` (the high-water mark) reaches its peak for a translation unit, subsequent pragmas re-use those exact 120-byte slots. This means **pragma records survive deeper than the parser's recursion** — a `#pragma pack(push, 8)` queued near the top of a header file may still be physically alive in memory (on the freelist, with `state_flags = 8`) while a `#pragma pack(pop)` near the bottom of the source file reuses its memory. The bug class to watch: any consumer that captures a `pending_pragma*` outside the engine's lifecycle calls (none in cudafe++ does — confirmed via xref scan — but the discipline is non-obvious from the code alone).

## Recognition and Lookup — `look_up_pragma_id` (0x6FBA20)

When the preprocessor's tokenizer encounters `#pragma <ident>`, `look_up_pragma_id` walks the linked list at `qword_106B8A8` and returns the descriptor whose name (indirected through `off_E6CDE0[name_index]`) matches the token's UTF-8 spelling. The lookup is linear — there are roughly 30 active kinds at any given time, so a hash table would lose to cache locality here.

```c
descriptor *look_up_pragma_id(token *tok, size_t n) {
    descriptor *d = qword_106B8A8;           // list head
    if (!d) return qword_106B890;            // → "unrecognized" sink

    const char *src = tok->text;
    while (d) {
        const char *name = off_E6CDE0[d->name_index];
        if (strlen(name) == n && !strncmp(name, src, n))
            break;
        d = d->next;
    }
    if (!d) return qword_106B890;            // unrecognized → kind 42

    /* Two-token disambiguation for `nv_diagnostic` (kind 28).        */
    /* `nv_diagnostic push` and `nv_diagnostic pop` look identical at */
    /* the first token; the lookup peeks past the `diagnostic` ident  */
    /* to enforce that the next token IS the `push`/`pop` keyword.    */
    if (d->kind_id == 28) {
        sub_679800();                        // consume `diagnostic`
        if (memcmp(qword_126DDA0, "diagnostic", 10) == 0) {
            d = d->next;
            if (d->kind_id != 29)
                assertion("preproc.c", 2964, "look_up_pragma_id", 0, 0);
        }
    }
    return d;
}
```

The reserved-identifier filter at the head of the function rejects `__VA_ARGS__` and `__VA_OPT__` outside macro contexts — these are syntactic sugar of the C preprocessor, not pragma names, and accidentally matching them would corrupt the macro expansion path.

The name table at `off_E6CDE0` is not a contiguous string array but a table of `char*` pointers indexed by `name_index`. The actual strings live at addresses `0x90FD0D` (`hd_warning_disable`) through `0x90FE41` (`unrecognized`) — they are interleaved with operator-name strings in the same data page because the linker grouped all "small const strings" together. The dispatch table `funcs_6F71AE` mirrors the same indexing convention, which is what makes the engine "unified": `name_index = i` means *both* "name is `off_E6CDE0[i]`" *and* "immediate handler is `funcs_6F71AE[i]`".

## Immediate-Pragma Dispatch — `funcs_6F71AE[0..55]`

The 56-slot dispatch table at `0xD55F08` is the engine's hot path. Every pragma whose descriptor has `binding_mode = 0xD` (immediate-after-recognition) is processed here as soon as `enter_pending_pragma` finishes building its record. The complete table, with the responsible source-file attribution recovered from per-handler assertion strings:

| Slot | Function | Likely role (from cross-refs) |
|---|---|---|
| 0 | `sub_411F40` | error recovery / NULL-sink |
| 1 | `sub_411F30` | error recovery |
| 2 | `sub_74A900` | `#pragma message` emitter |
| 3 | `sub_4120B0` | `#pragma error` |
| 4 | `sub_417790` | `#pragma warning` |
| 5 | `sub_66C410` | `#pragma comment` (linker) |
| 6 | `sub_66C440` | `#pragma comment(user, ...)` |
| 7 | `sub_66B630` | `#pragma push_macro` |
| 8 | `sub_66B6C0` | `#pragma pop_macro` |
| 9 | `sub_66B710` | `#pragma push_macro` body |
| 10 | `sub_66B720` | `#pragma pop_macro` body |
| 11 | `sub_761410` | `#pragma weak` |
| 12 | `sub_761480` | `#pragma weak` alias form |
| 13 | `sub_730EF0` | `#pragma alloc_text` |
| 14 | `sub_730F00` | `#pragma alloc_seg` |
| 15 | `sub_4D81D0` | `#pragma intrinsic` |
| 16 | `sub_7916D0` | `#pragma extname` / `redefine_extname` |
| 17 | `sub_661990` | `pack_pragma` (caller of `convert_pragma_to_string`) |
| 18 | `sub_6FBCA0` | `#pragma once` (9-byte tail-call) |
| 19 | `sub_6FBBB0` | `#pragma system_header` |
| 20 | `sub_6FB920` | `#pragma push_options` |
| 21 | `sub_6FB960` | `#pragma pop_options` |
| 22 | `sub_447420` | `#pragma optimize` |
| 23 | `sub_6FBCB0` | `#pragma reset_options` (21-byte) |
| 24 | `sub_417F40` | `#pragma optimize_for_synchronized` |
| 25 | `sub_6FC1F0` | `#pragma GCC ...` umbrella handler |
| 26 | `sub_4F8F80` | `nv_diag_suppress` |
| 27 | `sub_4F76E0` | `nv_diag_remark`/`warning`/`error`/`once`/`default` |
| 28 | `sub_6F8220` | `nv_diagnostic push` |
| 29 | `sub_6F8290` | `nv_diagnostic pop` |
| 30 | `sub_5F7D50` | `#pragma unroll` |
| 31 | `sub_7424E0` | `#pragma nvopt` |
| 32 | `sub_74A090` | `#pragma nv_abi` |
| 33 | `sub_49D7B0` | `#pragma can_instantiate_class` (defer) |
| 34 | `sub_49D7F0` | `#pragma can_instantiate_class` (delayed-proc) |
| 35 | `sub_7615A0` | `#pragma inline_template` |
| 36 | `sub_730ED0` | `#pragma alloc_text` (legacy) |
| 37 | `sub_730EE0` | `#pragma alloc_seg` (legacy) |
| 38 | `sub_7C1A00` | `#pragma include_alias` (open) |
| 39 | `sub_7C1A30` | `#pragma include_alias` (close) |
| 40 | `sub_675CC0` | `#pragma conform(forScope)` push |
| 41 | `sub_675CD0` | `#pragma conform(forScope)` pop |
| 42 | `sub_67C940` | `#pragma start_map_region` |
| 43 | `sub_67C9A0` | `#pragma stop_map_region` |
| 44 | `sub_6FBF50` | `__pragma(...)` Microsoft form |
| 45 | `sub_5859C0` | `#pragma omp begin declare variant` |
| 46 | `sub_585AE0` | `#pragma omp end declare variant` |
| 47 | `sub_4D8070` | `#pragma DEVICE_BUILTIN` |
| 48 | `nullsub_3` | (reserved) |
| 49 | `sub_4D80C0` | `#pragma hd_warning_disable` |
| 50 | `sub_4D8100` | `#pragma nv_exec_check_disable` |
| 51 | `sub_4D8120` | `#pragma TEXTUTE_TYPE`/`SURFACE_TYPE` |
| 52 | `sub_6FBFE0` | `#pragma db_opt` |
| 53 | `sub_5CF740` | `#pragma define_type_info` |
| 54 | `sub_6BCE40` | `#pragma hdrstop` / `no_pch` |
| 55 | `nullsub_6` | (reserved sink) |

Note the famous typo at slot 51: the binary literally spells the keyword as `"TEXTUTE_TYPE"` (string at `0x90FD53`). This is preserved as-is because the matching `#pragma TEXTUTE_TYPE` lines appear in NVIDIA's own `crt/host_defines.h` and changing the spelling would break every CUDA toolkit installation older than this one.

`process_immediate_pragmas` (`sub_6F7660`) is the dispatcher:

```c
void process_immediate_pragmas(void) {
    pending_pragma *v0 = qword_106B8A0;
    qword_106B8A0 = NULL;                    // drain queue atomically
    while (v0) {
        descriptor *d = v0->kind_desc;
        if (d->category != 3 ||              // not an NV-extension class
            (v0->state_flags & 8))           // already processed
            { v0 = v0->next; continue; }

        if ((d->attr_flags & 0x08) == 0) {   // not immediate-class
            v0->state_flags |= 8;
        } else {
            uint8_t idx = d->name_index;
            if (idx > 0x37)
                assertion("pragma.c", 1230, "process_immediate_pragmas", 0, 0);
            void (*h)(pending_pragma*) = funcs_6F71AE[idx];
            if (h) h(v0);
            v0 = v0->next;
            if (!v0 && qword_106B8A0)
                assertion("pragma.c", 1237, "process_immediate_pragmas", 0, 0);
            continue;
        }
        sub_6F66B0(v0, 0, 0, (d->attr_flags & 4) != 0);   // bind to IL
        v0 = v0->next;
    }
}
```

The second assertion (line 1237) catches the case where a handler *itself* queues new pending pragmas into `qword_106B8A0` mid-dispatch but the dispatcher has already cleared the head pointer. The handler `sub_6FC1F0` (the GCC umbrella) is the only one that actually does this — it parses `#pragma GCC target("...")` into one immediate side-effect *and* one deferred construct-bound pragma — and it carefully sets `qword_106B8A0` after the recursive call rather than during it.

## IL Embedding — `add_pragma_to_il` (0x6F66B0)

For construct-bound pragmas (binding `0x0C` = bound to the next token; flag `0x04` set), the engine does not execute a handler. Instead it materialises a pragma node directly inside the EDG IL tree at the position where the parser is currently building. The function `add_pragma_to_il` walks the scope stack at `qword_126C5E8 + 784 * dword_126C5E4` (the current "in-flight construct" record):

```c
void add_pragma_to_il(pending_pragma *p, uint8_t entity_kind,
                      void *entity_node, int force_next) {
    int v6 = dword_126C5E4;                  // current construct depth
    if (v6 == -1 || !(scope[v6].flags & 2))  // scope cannot host pragmas
        return;

    if (entity_node) {
        if (entity_kind == 21) {             // synthetic "next construct"
            entity_node->flags |= 1;         // mark next-construct anchor
            il_node *n = sub_5E7570(p->kind_desc->kind_id, NULL);
            n->src_pos    = p->position_start;
            n->orig_token = p->arg_buffer[12];
            n->in_template = p->kind_desc->binding_mode & 1;
            n->bound_kind = entity_kind;
            n->bound_to   = entity_node;
            return;
        }
        /* Look up source-correspondence record for the entity */
        scope_entry *src = sub_5B9EE0(entity_node, entity_kind);
        if (!src) assertion("pragma.c", 945, "add_pragma_to_il",
                            "add_pragma_to_il:",
                            "invalid entity kind (no source corresp)");
        src->il_flags |= 0x80;               // "has associated pragma"
        /* ... walk to anchor & insert ... */
    }
}
```

The `0x80` bit on the entity's IL flag is the consumer's flag: when the backend later walks the IL to emit the host stub, it sees the bit and re-attaches the pragma to its output. This is how `#pragma unroll N` survives all the way to the `.cudafe1.cpp` output — `unroll` is a construct-bound pragma whose IL node is read by the loop-emission code.

> ⚡ **QUIRK — pragmas can be re-bound to a *different* construct than the one parsed next**
> `add_pragma_to_il` accepts an `entity_kind = 21` ("synthetic next construct") that lets a pragma attach to the *future* construct that will be built. This is how `#pragma next_construct` semantics work for CUDA's `__launch_bounds__` and `nv_abi`: the pragma is recognized while the parser is still inside a template-argument list, but its IL node defers attachment until the function definition that follows. The bookkeeping bit (`flags & 1` on the anchor) is set on a temporary IL node that the parser overwrites once the real construct arrives. If overwriting fails, the assertion `"invalid next_construct call"` at pragma.c:1045 fires.

## Construct-Bound Pragmas — Three-Function Cycle

A construct-bound pragma — anything that decorates the next declaration or statement — passes through three pragma.c functions in sequence:

1. **`select_curr_construct_pragmas`** (`0x6F73C0`, 661 bytes). Called by the parser when it starts a new declaration. Sweeps the pending queue for pragmas whose `position_end <= current_position` and whose `binding_mode == 0xC`. Moves them to a per-construct list rooted at `scope[depth].construct_pragmas`. Asserts `"previous list not NULL"` if the slot wasn't drained — this catches missed `process_curr_construct_pragmas` calls.

2. **`process_curr_construct_pragmas`** (`0x6F7BB0`, 554 bytes). Called by the parser at the point it has resolved the construct's identity (function-decl, var-decl, statement, etc.). Walks the per-construct list and either (a) routes to the immediate handler if the construct is incompatible with the pragma (e.g. `#pragma unroll` on a non-loop produces a diagnostic via `funcs_6F71AE[30]`), or (b) calls `add_pragma_to_il` to embed the pragma in the construct's IL node.

3. **`extract_curr_construct_pragmas`** (`0x6F7FF0`, 187 bytes) and its inverse `reactivate_curr_construct_pragmas` (`0x6F80B0`, 275 bytes). Used when the parser backtracks (template-argument speculation, SFINAE substitution). `extract` lifts the pragma list off the construct and stashes it on `scope[depth].suspended_pragmas`; `reactivate` reinstates it if the speculative parse succeeds. The assertion `"pragma list not already empty"` at `0x6F80B0:reactivate` fires if both lists are non-empty — a state that should be impossible under the EDG invariants.

The fourth function in the cycle — `discard_curr_construct_pragmas` — is invoked when the speculative parse *fails*. It releases the pragma records back to `qword_106B738` (the freelist).

## `_Pragma` and `__pragma` Operators

C99/C++11 `_Pragma("string")` and Microsoft's `__pragma(token-string)` are expression-context cousins of `#pragma`. Both route through `enter_pending_pragma` after string materialisation:

- `_Pragma("X Y Z")` is processed by `sub_6B5E50` (the lexer's literal-string path) which calls `scan_pragma_string` to strip quotes and re-tokenise the contents, then invokes the standard pragma path. The diagnostic source position remains the location of the `_Pragma` keyword, *not* the position of any tokens inside the string.
- `__pragma(X Y Z)` is processed by `sub_687F30` (Microsoft macro-context handler). The argument list is a token sequence rather than a string literal, but the destination is identical.

Both operators reject construct-bound pragmas: the error `"this pragma cannot be used in a _Pragma operator"` (and the `__pragma` variant) fires when a kind with `binding_mode & 0xC` reaches this entry point. Only `binding_mode == 0xD` (immediate) is legal inside operator forms.

> ⚡ **QUIRK — `_Pragma` re-enters the registry but `__pragma` does not allocate a new descriptor**
> `_Pragma`'s contents are re-tokenised by `begin_rescan_of_pragma_tokens` (`0x6F70C0`), which means the string `"GCC diagnostic ignored \"-Wattributes\""` undergoes the full lexer pipeline a second time — keywords are recognised, escapes processed, etc. `__pragma`, in contrast, parses its argument as a raw token stream in the current lexer state. Practical consequence: macros inside `_Pragma()` *do* expand (because re-tokenisation runs the preprocessor again); macros inside `__pragma()` do not. CUDA headers exploit this difference for cross-compiler portability.

## Engine-Wide Counters and Statistics

Three `dword`s at the head of the pragma-engine data block track allocation statistics. They are printed to the diagnostic stream when `-trace pragma` is active:

| Symbol | Address | Counter |
|---|---|---|
| `num_pragmas_allocated` | `0x106B728` | Lifetime descriptor allocations (always 42 plus conditional kinds) |
| `num_pending_pragmas_allocated` | high-water of `qword_106B730` | Peak in-flight pragmas |
| `num_pragmas_in_reusable_caches` | derived from `qword_106B738` chain length | Freelist size |
| `num_pragma_descriptions_allocated` | `qword_106B728` | == `num_pragmas_allocated` |
| `num_gcc_pragma_options_stack_entries_allocated` | `0x106BD80` | High-water of the GCC `push_options`/`pop_options` stack |
| `avail_gcc_pragma_options_stack_entries` | `0x106BD78` | Current free entries in the GCC option-stack pool |

These appear as labels-only in the binary because the diagnostic stream printer iterates a "(label, address)" tuple table at `0xA88600` and prints each one with `fprintf(s, "%s = %lu\n", lbl, *(uint64_t*)addr)`. The matching `pragma_count: %lu` at `0xA791C7` is the printf format.

## Cross-Reference Summary

The pragma engine touches the following neighbouring subsystems:

- **Diagnostic system.** Severity overrides via `nv_diag_*` pragmas — see [SARIF & Pragma Diagnostic Control](../diagnostics/sarif-pragmas.md). Pragma-induced diagnostics use `sub_4F8200` (the `pragma_must_precede_*` family) at the binding-mode mismatch points.
- **CUDA attributes.** `nv_abi`, `unroll`, `nvopt`, `DEVICE_BUILTIN`, `hd_warning_disable` are all routed through the same dispatch table that handles standard pragmas — see [Attribute System Overview](../attributes/overview.md).
- **EDG lexer.** `_Pragma` / `__pragma` token recognition is in `sub_6B5E50` and `sub_687F30` — see [Lexer & Tokenizer](./lexer.md).
- **IL embedding.** `add_pragma_to_il` reuses the IL allocator and walker — see [IL Allocation](../il/allocation.md) and [Keep-in-IL](../il/keep-in-il.md).
- **Output generation.** GCC pragmas injected into `.cudafe1.cpp` are emitted by `sub_467E50` and are *not* engine-managed pragmas (they are output-only text strings) — see [.int.c File Format](../output/int-c-format.md).

## Function Address Index

| Address | Source | Role |
|---|---|---|
| `0x6F61B0` | `pragma.c` | `alloc_pragma` (descriptor allocation) |
| `0x6F62C0` | `pragma.c` | `alloc_copy_of_pending_pragma`, `make_copy_of_pragma_list` |
| `0x6F6540` | `pragma.c` | `alloc_pending_pragma` |
| `0x6F66B0` | `pragma.c` | `add_pragma_to_il` |
| `0x6F6B00` | `pragma.c` | `create_il_entry_for_pragma` |
| `0x6F7060` | `pragma.c` | `process_curr_token_pragmas` |
| `0x6F73C0` | `pragma.c` | `select_curr_construct_pragmas` |
| `0x6F7660` | `pragma.c` | `process_immediate_pragmas` |
| `0x6F78E0` | `pragma.c` | `extract_specific_pragmas` |
| `0x6F7BB0` | `pragma.c` | `process_curr_construct_pragmas` |
| `0x6F7FF0` | `pragma.c` | `extract_curr_construct_pragmas` |
| `0x6F80B0` | `pragma.c` | `reactivate_curr_construct_pragmas` |
| `0x6F81D0` | `pragma.c` | `process_pragmas_at_end_of_source` |
| `0x6F8320` | `pragma.c` | `pragma_init`, `add_pragma_kind_description` |
| `0x6F9B00` | `preproc.c` | `enter_pending_pragma` |
| `0x6FBA20` | `preproc.c` | `look_up_pragma_id` |
| `0x6FC1F0` | `preproc.c` | `process_gnu_target_pragma`, `process_gnu_system_header_pragma` (GCC umbrella) |
| `0x6FC940` | `preproc.c` | `pp_directive` dispatcher |
| `0xD55F08` | `.data` | `funcs_6F71AE[56]` — immediate-handler dispatch table |
| `0x106B728` | `.bss` | `num_pragmas_allocated` |
| `0x106B738` | `.bss` | Pending-pragma freelist head |
| `0x106B740` | `.bss` | Kind-descriptor lookup array (`[1..42]`) |
| `0x106B8A0` | `.bss` | Pending-pragma queue head |
| `0x106B8A8` | `.bss` | Kind-descriptor linked-list head |
