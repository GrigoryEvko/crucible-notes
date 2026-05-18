# Symbol Reference & Name Hiding

The symbol-reference subsystem in cudafe++ is EDG 6.6's `symbol_ref.c` — the central layer that fires every time a symbol is **named** at use-site rather than at declaration-site. It is invoked from the expression parser, the declaration parser, the template-instantiation engine, and the constant-expression interpreter; it answers four interleaved questions for each reference: *(1)* does this name shadow another visible name (name-hiding diagnostics), *(2)* is the entity reachable from the current execution space (the CUDA `__host__`/`__device__` cross-space check), *(3)* is the entity deprecated, deleted, or unavailable, and *(4)* should this use be appended to the cross-reference log (`-Xptxas --gen-xrefs` and friends). The subsystem occupies approximately `0x726F20`–`0x72D375` in the binary (about 21 KB of code across 17 functions plus two 29-byte assert trampolines at `0x408064`/`0x408081`). Source attribution is unambiguous: every non-trivial function in the file carries an assertion citing `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/symbol_ref.c`, and most also leak the original C symbol name through the assertion's third argument.

Unlike the lexer/preprocessor pair, this subsystem has **no** dispatch table. There is no `pp_directive`-style switch. Instead it is a cluster of seven public entry points that the rest of EDG calls directly, plus ten private helpers. Two of those entry points (`sub_72A650` and `sub_72B510`) are **near-identical twins** containing the same control flow, the same string set, and the same 41 callees — one is the IL-traversal version, the other is the source-parse version. CUDA's host-vs-device check happens deep inside both.

## Key Facts

| Property | Value |
|---|---|
| Source file | `symbol_ref.c` (EDG 6.6) |
| Address range | `0x726F20`–`0x72D375` (plus `0x408064`/`0x408081` assert trampolines) |
| Function count | 17 (15 in main range + 2 trampolines) |
| Total code | ~21 KB |
| Master reference recorder | `record_symbol_reference_full` (`sub_72B510`, 3,909 bytes, 250 basic blocks, 36 callers) |
| IL-traversal twin | `sub_72A650` (3,772 bytes, 243 basic blocks, 9 callers) |
| Scope-hiding driver | `check_name_hiding_for_scope` (`sub_728C00`, 2,585 bytes, 158 blocks) |
| Inherited-name driver | `check_hiding_by_inherited_names` (`sub_729620`, 2,310 bytes, 192 blocks) |
| Defeatable-hiding recorder | `record_defeatable_name_hiding` (`sub_7278E0`, 1,306 bytes, 11 callers) |
| Per-entity defeatable recorder | `record_defeatable_name_hiding_for_single_entity` (`sub_726F20`, 1,588 bytes, 3 callers) |
| Cross-reference writer | `write_xref_entry` (`sub_729F30`, 1,264 bytes, 57 callers across the whole binary) |
| Deprecation check | `check_use_of_deprecated_or_unavailable_entity` (`sub_72CEA0`, 1,195 bytes, 6 callers) |
| Implicit-invocation check | `reference_to_implicitly_invoked_function` (`sub_72CA40`, 1,113 bytes, 12 callers) |
| Unhiding check | `check_name_unhiding` (`sub_727560`, 892 bytes, 3 callers) |
| Param-list recorder | `record_param_id_list_declarations` (`sub_72C5D0`, 790 bytes, 4 callers) |
| Deleted-function check | `check_use_of_deleted_function` (`sub_72A460`, 485 bytes) |
| Template-param hiding | `check_name_hiding_by_template_parameters` (`sub_7288D0`, 283 bytes) |
| Param-name hiding | `check_name_hiding_by_parameter` (`sub_728A60`, 414 bytes) |
| Value-set marker | `mark_variable_value_set` (`sub_7289F0`, 97 bytes, 11 callers) |
| Deprecation-only wrapper | `check_use_of_deprecated_or_unavailable_entity` thunk at `sub_72A420` (62 bytes) |
| Cross-reference format | `"\t%c\t%s\t%lu\t%d\n"` (tab-separated: kind, name, line, column) |

## Architecture

```
Expression parser / decl parser / template engine / constexpr interpreter
        │  resolves an identifier or qualified-id to an entity node
        ▼
record_symbol_reference_full (sub_72B510, 3909 B)   ← 36 callers
        │
        ├─ Phase 1: classify reference kind (read, write, take-address, decltype-only)
        │
        ├─ Phase 2: deprecation / deletion / unavailability
        │     ├─ check_use_of_deprecated_or_unavailable_entity (sub_72CEA0)
        │     ├─ check_use_of_deleted_function                 (sub_72A460)
        │     └─ reference_to_implicitly_invoked_function      (sub_72CA40)
        │           └─ reference_to_trivial_default_constructor (folded in)
        │
        ├─ Phase 3: CUDA cross-space check  [CUDA-ONLY EXTENSION]
        │     ├─ nv_check_device_var_ref_in_host
        │     ├─ nv_check_host_var_ref_in_device
        │     └─ recognises "__shared__", "__constant__"
        │       diag: "outside the bodies of device functions"
        │       diag: "from a constexpr or consteval __device__ function"
        │
        ├─ Phase 4: mark value-set / mark static-data-member value-set
        │     ├─ mark_variable_value_set                       (sub_7289F0)
        │     └─ mark_static_data_member_value_set
        │
        └─ Phase 5: emit xref entry  →  write_xref_entry (sub_729F30)
                                         format: "\t%c\t%s\t%lu\t%d\n"

(parallel cluster — name hiding, fired from class-body parser & lookup)

check_name_hiding_for_scope (sub_728C00, 2585 B)
        ├─ Hidden name check for inherited names of <T>
        ├─ Checking hidden names declared in <T>
        ├─ check_defeatable_base_inaccessibility
        └─ → check_hiding_by_inherited_names (sub_729620, 2310 B)
                └─ → record_defeatable_name_hiding_for_single_entity
                                                     (sub_726F20, 1588 B)
                       ├─ make_new_hidden_name        (trampoline sub_408064)
                       └─ symbol_is_candidate_for_hiding (trampoline sub_408081)
```

The two-tier callgraph (top half = reference recording, bottom half = name hiding) is a hint that EDG implements *defeatable* name-hiding lazily — the hide is **proposed** by the class-body parser via `record_defeatable_name_hiding`, then **defeated or confirmed** at use-site by `record_symbol_reference_full`. There is no separate "name hiding committed" pass; the check happens implicitly when a hidden name is referenced.

## The Master: `record_symbol_reference_full`

`record_symbol_reference_full` at `0x72B510` is the busiest function in the entire EDG core after the lexer cache: 36 distinct callers, 41 callees, 250 basic blocks, 822 instructions, 104-byte stack frame. It is reached whenever an *unhidden* lookup resolves a name to an entity. The signature reconstructed from the assertion machinery and the callgraph:

```c
void record_symbol_reference_full(
    entity_t        *entity,              // resolved symbol
    reference_kind_t kind,                // read, write, address-of, decltype, etc.
    source_position_t pos,                // file/line/column triple
    int              flags                // suppress-warning bits
);
```

The function carries six separate `preproc.c`-style assertions, each of which leaks one phase name through the third argument to `sub_4F2930`:

- `"record_symbol_reference_full:"` — the function-entry sanity check (entity is non-null and well-formed)
- `"record_symbol_reference_full: projection symbol"` — a guard that fires when the recorder receives a *projection* (template parameter substitution stub) instead of a concrete entity; projections must be resolved before this point
- `"within_try_block not set properly"` — sets up the exception-spec-checking flag, which is consumed downstream by `check_use_of_implicitly_invoked_function` to decide whether to emit "potentially-throwing call inside non-throwing context"
- `"nv_check_device_var_ref_in_host"` and `"nv_check_host_var_ref_in_device"` — the two CUDA cross-space probes (see below)
- `"mark_static_data_member_value_set"` and `"mark_variable_value_set"` — invoked when the reference kind is *write* and the entity is a variable or a static data member

### Phase 3 — The CUDA Cross-Space Check

EDG's stock `symbol_ref.c` does **not** know anything about `__host__`/`__device__`. NVIDIA inlined the check at the recorder level rather than wrapping the function: the strings `"__shared__"` and `"__constant__"` appear in `sub_72B510` itself, not in a separate wrapper. The flow:

1. Read the current function's execution-space bit from the entity-context stack.
2. Read the referenced entity's execution-space bit (or, for variables in namespace scope, the `__constant__`/`__shared__`/`__device__` annotation bits).
3. If the reference is host→device: enter `nv_check_host_var_ref_in_device` path. Emit diagnostic *X* (resolved via the diagnostic table); the diagnostic text is rendered as `"<name> can be used as an unqualified name outside the bodies of device functions"` for the *taking the address* sub-case.
4. If the reference is device→host: enter `nv_check_device_var_ref_in_host` path. The diagnostic includes `"from a constexpr or consteval __device__ function"` when the offender is a `constexpr __device__` referring to a host entity.
5. The execution-space bit propagates upward into the call-graph so that `__device__ constexpr` evaluation contexts get the right check even when the parent function is `__host__`.

The host-vs-device gate is **not** symmetrical: there is one additional special case for `constexpr`/`consteval`. A `constexpr` function annotated only with `__device__` is allowed to call a `__host__ constexpr` helper, but only if the eventual evaluation happens at translation time. The diagnostic `"from a constexpr or consteval __device__ function"` is emitted lazily: it is encoded into the IL when the violation is detected, and only printed if the IL node escapes constant evaluation.

> ⚡ **QUIRK — `record_symbol_reference_full` exists in two near-identical copies.** `sub_72B510` (3,909 B, 36 callers) and `sub_72A650` (3,772 B, 9 callers) share every assertion string, the same `"__shared__"` and `"__constant__"` literals, the same `"write_xref_entry: bad reference kind"` guard, and the same 41-callee fan-out. The 137-byte size difference, the smaller block count (243 vs 250), and the divergent caller set show they are not a thunk pair but two *specializations*: the larger one is invoked from source-parse positions where the source position is the *current* token, the smaller one is invoked from IL-walk positions where the source position has to be reconstructed from an entity-link. NVIDIA appears to have manually inlined the position lookup along the IL path rather than passing an extra parameter — a micro-optimization that doubles the code footprint of the entire subsystem.

## The Cross-Reference Logger: `write_xref_entry`

`write_xref_entry` (`sub_729F30`, 1,264 bytes, 73 basic blocks, 57 callers spread across the entire binary) is the back-end of every symbol reference. The 57-caller fan-in includes functions from `class_decl.c`, `decl.c`, `lookup.c`, `templates.c`, `il_walk.c`, `macro.c`, and dozens of others — confirming that cross-reference output is wired into nearly every place a name resolves to an entity, not just into the four `symbol_ref.c` paths.

The format string is the tab-separated `"\t%c\t%s\t%lu\t%d\n"`:

| Field | Format | Meaning |
|---|---|---|
| `%c` | one character | reference kind: `R` = read, `W` = write, `T` = take-address, `D` = decltype, `C` = call, `I` = include, etc. |
| `%s` | string | fully-qualified entity name |
| `%lu` | unsigned long | source line number (1-based) |
| `%d` | int | source column number (1-based) |

There is a leading tab before every field; the actual file/scope prefix is written separately by a wrapper before this function is reached. The function checks `dword_*` flags to decide whether to emit a given kind — for example, *D* (decltype-only) emissions are suppressed when the user passed `--no_decltype_xrefs` (a flag whose name leaks in `convert_pp_directive_to_string` but not here).

The assertion `"write_xref_entry: bad reference kind"` fires when the `%c` field would otherwise be `\0` — a guard against the caller passing kind = 0, which used to silently emit blank xref lines in earlier EDG versions and corrupted downstream tools.

A near-clone `sub_72C460` (359 bytes, 2 callers) holds **only** the format string but not the kind-validation assertion. It is the fast path for kinds that have already been validated — likely inlined into hot loops in `record_param_id_list_declarations` where the same kind is emitted for every parameter without re-checking.

## Name Hiding: The Defeatable Mechanism

C++ name hiding has two flavours: *unconditional* (a derived-class member named `foo` hides every base-class `foo`) and *defeatable* (a `using` declaration or an explicit qualified call defeats the hide). EDG implements both in this file, but only the defeatable case generates persistent records.

### `record_defeatable_name_hiding` (`sub_7278E0`)

The 1,306-byte recorder is called from 11 sites (class-body parser, using-decl parser, lambda capture, etc.) and pushes an entry onto a per-scope *defeatable hide list*. The entry records `(hider, hidden_set, scope)` so that a later qualified reference can interrogate the list and decide:

- The reference is **unqualified** and resolves to `hider` → hide is confirmed; no diagnostic.
- The reference is **qualified** to a base-class scope where `hidden_set ⊇ {entity}` → hide is defeated; record the entity as the resolution target.
- The reference is unqualified but the *entity* is in `hidden_set` and *not* `hider` → hide is confirmed, emit `"<entity> can be used as an unqualified name"` followed by the actual diagnostic text.

The per-entity granularity (`record_defeatable_name_hiding_for_single_entity` at `0x726F20`) exists because a single `using BaseClass::member;` declaration can defeat a hide for *one* overload of `member` but not for others — the hide list is keyed on entity nodes, not name strings.

### `check_hiding_by_inherited_names` (`sub_729620`, 2,310 B)

The scanner that walks a derived class's base list. For each base, it emits the verbose-trace message `"Hidden name check for inherited names of <T>"` (when `dword_*` trace > 0) and recurses via the *Itanium-style* virtual-base linearization. The function is large because it has to:

1. Build the list of *names visible through inheritance* (not just direct bases).
2. For each name, intersect against the derived class's *own* members.
3. For each non-empty intersection, call `record_defeatable_name_hiding`.
4. Emit `"    ...doing lookup (inherited names)\n"` on the trace.

The trace strings (`"    ...doing lookup (using-directive)"`, `"    ...doing skip-curr-scope lookup for parameter"`, `"Checking hidden names declared in "`) are concentrated in this function and `check_name_hiding_for_scope`. They are only printed when `dword_106C2F4 > 0` (the `--dump_class_layout_for_classes` family of trace flags), making the name-hiding check observable from outside without source.

### `check_name_unhiding` (`sub_727560`)

The 892-byte symmetric counterpart that runs at the point a `using` declaration is *parsed*. For each member already in the hide list, it checks whether the new using-decl defeats it; if so, the entry is marked *defeated* but not removed. (Removal would lose the audit trail needed when the using-decl itself is later found to be ill-formed.)

The unhiding path goes through `dump_hidden` (an internal verbose-trace function shared by `check_name_hiding_for_scope`, `record_defeatable_name_hiding_for_single_entity`, `check_hiding_by_inherited_names`, and `check_name_unhiding` — its address is reached by static call so it doesn't appear as a separate function in the disassembly, but the literal `"dump_hidden"` is the assertion citation for the underlying helper).

> ⚡ **QUIRK — name-hiding tracing fires `dump_hidden` from four different functions, all citing the same source line.** The literal `"dump_hidden"` appears as a `__func__`-style assertion citation in `sub_726F20`, `sub_727560`, `sub_728C00`, and `sub_729620` — never as a string passed to a logging function. Either EDG inlined `dump_hidden` aggressively after assertion-injection (so all four sites carry the function's own `__func__`), or `dump_hidden` lives in an `#include`d source fragment that produces the same assertion in every translation unit it is pasted into. Either way, the consequence for reverse-engineering is that the trace function's *body* is not present as a discrete code region — its instructions are smeared across four parent functions, each with its own copy.

## Other Hiding Checks

| Function | Address | Size | Trigger |
|---|---|---:|---|
| `check_name_hiding_by_parameter` | `0x728A60` | 414 B | A function parameter shadows a name from the enclosing scope (warning −Wshadow=local) |
| `check_name_hiding_by_template_parameters` | `0x7288D0` | 283 B | A template-parameter declaration shadows an outer template parameter |
| `check_defeatable_base_inaccessibility` | (inlined inside `sub_728C00`/`sub_729620`) | — | A derived class's `private`/`protected` inheritance hides what would otherwise be a defeatable name |
| `check_name_hiding_for_scope` | `0x728C00` | 2,585 B | The umbrella check fired for every new scope entry (block, class body, namespace) |

`check_name_hiding_by_parameter` is the only one that runs in **C** mode as well as C++; the others are no-ops outside C++. The function reads the `dword_106C2C0` C++ flag (same global used by the preprocessor's directive recognizer) early in its body and skips the rest of the check when clear.

## Deprecation and Deleted-Function Checks

`check_use_of_deprecated_or_unavailable_entity` (`sub_72CEA0`, 1,195 bytes) is fired by `record_symbol_reference_full` after the cross-space gate. It walks the entity's `__attribute__((deprecated))`/`[[deprecated]]`/`[[unavailable]]` attribute chain and:

1. Picks the *most specific* deprecation reason (entity-level > overload-level > member-of-deprecated-class).
2. Inserts a deferred warning record so the diagnostic is only emitted at the *first* use, not on every reference.
3. For `[[unavailable]]`, escalates to a hard error.
4. For the special `__nv_deprecated_attribute__` (the CUDA-specific deprecation that gates `__CUDA_NO_HALF_OPERATORS__` and friends), reads the architecture flag `dword_106C32C` and suppresses the warning on architectures predating the deprecation.

The 62-byte wrapper `sub_72A420` is the call site for the deprecation check from contexts that need only the deprecation half — not the unavailable check. It exists because `[[unavailable]]` triggers a hard exit, which would be wrong from a *deferred* IL walk (you can't error in IL-walk mode, you have to defer).

`check_use_of_deleted_function` (`sub_72A460`, 485 bytes) handles `= delete`. It's small because deleted functions never have a per-function reason chain — the diagnostic text is always `"use of deleted function"` plus the entity's source location. The function's job is mainly to choose between two diagnostic IDs: error 1776 ("call to deleted function") vs error 1778 ("conversion to deleted function") depending on the reference kind.

`reference_to_implicitly_invoked_function` (`sub_72CA40`, 1,113 bytes) and `reference_to_trivial_default_constructor` (folded into `sub_72CEA0`) check whether the entity being referenced is an *implicit* member function (compiler-synthesised assignment operator, destructor, default constructor) and, if so, whether the implicit definition would itself violate any cross-space rule. This is how `__device__ struct S {};`'s implicit destructor gets re-checked when an `S` instance is destroyed in `__host__` code.

## Parameter-ID-List Recording: `record_param_id_list_declarations`

`record_param_id_list_declarations` (`sub_72C5D0`, 790 bytes) handles a specific corner: the K&R-era parameter identifier list `void f(a, b, c) int a; int b; int c; { ... }`. EDG accepts the syntax in C mode (per ISO C); the function walks the identifier list, looks each up in the *function body* scope, and emits xref entries for them via `write_xref_entry` (the format `"\t%c\t%s\t%lu\t%d\n"` appears here too).

It is called from four sites in the C declaration parser. In C++ mode the function is unreachable.

## Trampolines: The 29-Byte Assert Stubs

`sub_408064` and `sub_408081` are 29-byte tail-call thunks at the *low* end of the binary, far from the rest of `symbol_ref.c`. Their entire body is a load of `"/dvs/p4/.../symbol_ref.c"` and a load of `"make_new_hidden_name"` (or `"symbol_is_candidate_for_hiding"`) followed by a tail call to `sub_4F2930` (the assertion handler). They exist because:

1. Inlining the assertion site into 29 bytes is cheaper than a full call frame, but
2. They have to live at a *fixed* address so that the assertion handler's stack-unwinder can map the return address back to a function name without per-translation-unit debug data.

The fact that they sit at `0x408064` and `0x408081` — adjacent to other early-binary stubs — suggests they are produced by a linker fragment or a `__attribute__((cold))`-style placement pass that hoists never-executed assertion paths out of hot code. The C source for them is effectively:

```c
[[noreturn,cold]] void __assert_make_new_hidden_name(void) {
    assertion_handler("symbol_ref.c", LINE_OF_MAKE_NEW_HIDDEN_NAME,
                      "make_new_hidden_name", 0, 0);
}
```

— one per assert site, each in its own function so that the assertion handler can recover the *exact* line number from the return address.

## State Globals

`symbol_ref.c` is largely stateless — it operates on entity nodes passed in from callers — but it does read several language-mode and trace globals shared with the rest of EDG:

| Global | Purpose |
|---|---|
| `dword_106C2C0` | `1` = C++ language mode (suppresses C-only paths in `check_name_hiding_by_parameter`) |
| `dword_106C32C` | CUDA architecture number (used to gate `__nv_deprecated_attribute__` warnings) |
| `dword_106B6E0` | Current-directive-errored flag, shared with the preprocessor |
| Pragma-stack head | Read by deprecation check when the entity inherits its deprecation from a `#pragma GCC diagnostic` push |

The CUDA cross-space check inside `record_symbol_reference_full` reads execution-space bits from a thread-local context stack maintained by the parser, not from a global — which is why no `__device__`/`__host__` global appears in the global list. The execution-space bits live on the entity node itself (offset known to the IL layer) and on the *current scope* (offset known to the lookup engine).

## Cross-References

- `record_symbol_reference_full` is called from the [Expression Parser](expression-parser.md) at every identifier resolution and from the [Declaration Parser](declaration-parser.md) at every default-argument site.
- The CUDA host/device cross-space check inside `record_symbol_reference_full` is the *enforcement* arm of the rules documented in [Cross-Space Call Validation](../cuda/cross-space-validation.md).
- The xref emission format `"\t%c\t%s\t%lu\t%d\n"` is consumed by external tools when `--gen_module_id_file` or `--xref_output` is passed; see [CLI Flag Inventory](../config/cli-flags.md).
- Name-hiding diagnostics flow through the [SARIF & Pragma Diagnostic Control](../diagnostics/sarif-pragmas.md) layer for `#pragma GCC diagnostic`-style suppression.
- Deprecation attribute interpretation overlaps with the [Attribute System Overview](../attributes/overview.md); the `[[deprecated("reason")]]` reason string is rendered by the attribute layer and consumed here.
- The deleted-function check feeds error catalog entries 1776 and 1778; see [CUDA Error Catalog](../diagnostics/cuda-errors.md) for the full numeric assignments.
