# EDG Diagnostic Identifier Catalogue

The EDG frontend embedded in CICC v13.0 carries a second diagnostic naming layer separate from the numeric rule IDs surfaced in SARIF output. Roughly **4,539** snake_case lowercase string identifiers live in the binary's read-only string segment, organised into prefix families that mirror EDG's internal diagnostic source tree. These identifiers are the symbolic names EDG itself uses when an internal lookup needs to materialise a diagnostic record — the numeric ID at offset `+176` of the diagnostic record (see [Diagnostics & Optimization Remarks](./diagnostics.md)) is derived from these names at table-build time.

This catalogue documents the identifier scheme, prefix taxonomy, suppression mechanics, and deprecation behaviour so a reimplementation can pre-allocate the same name space and produce byte-compatible SARIF output. It is the symbolic counterpart to the numeric `-W#####` flag inventory.

## Why the symbolic names matter

A faithful reimplementation has to satisfy three constraints that the numeric ID alone cannot express:

1. **SARIF stability across versions.** EDG's SARIF emitter writes `ruleId` as the numeric form, but downstream tools (CI dashboards, lint aggregators) frequently key on the symbolic name pulled out of the rendered message text. NVIDIA's `nv_diag_*` pragma family also accepts symbolic suppression in some configurations.
2. **`#pragma diag_suppress` keyed lookups.** The dispatch function `sub_67D520` consults a per-source-range suppression map. The map key derived from a `#pragma diag_suppress` directive is the symbolic identifier hashed through the same path used at diagnostic emission time. Two reimplementations cannot produce matching suppression behaviour unless they agree on the identifier set.
3. **Deprecation cascades.** Several identifiers (`abi_tag_ignored`, `attribute_deprecated_with_message`, the entire `c23_*_deprecated` set) trigger a second diagnostic to be enqueued as a child note. The child's identifier is fixed by the parent's name, not by the parent's numeric ID. Reordering or renaming the parent breaks the child's "see also" linkage.

| | |
|---|---|
| **Identifier source segment** | `.rodata` string table within CICC's read-only data |
| **Lookup function** | `sub_67C860(string_id) -> const char*` |
| **Identifier-to-number resolution** | Built once at EDG init; cached in a flat array |
| **Suppression dispatch** | `sub_67D520(diag_record)` returns nonzero if suppressed |
| **`#pragma diag_*` parser** | Recognises `nv_diag_suppress`, `nv_diag_warning`, `nv_diag_error`, `nv_diag_remark`, `nv_diag_default`, `nv_diag_once`, `nv_diagnostic` |
| **Total identifiers (snake_case lowercase)** | ~4,539 across 200+ prefix families |
| **Identifier character class** | `[a-z][a-z0-9_]*`, never starts with a digit, never contains uppercase |
| **Longest identifier observed** | `cppcx_value_type_contains_virtual_function_with_runtime_class` (and similar 60+ char names) |

## Identifier Grammar

Every identifier in the catalogue obeys a uniform grammar that downstream parsers can rely on:

```
identifier := family_prefix "_" descriptor_tail
family_prefix := "abi" | "align" | "ambig" | "ambiguous" | "attr" | "attribute"
              |  "bad" | "c11" | "c23" | "cli" | "cl"
              |  "cppcli" | "cppcx" | "constexpr" | "cuda" | "cxx"
              |  "default" | "exp" | "ifc" | "invalid" | "ms"
              |  "nv" | "nv_abi" | "nv_atomic" | "nv_diag"
              |  "no" | "nonstd" | "omp" | "openmp" | "pragma"
              |  "ptr" | "strict" | "template" | "type" | "vector" | ...
descriptor_tail := snake_case description, may include digits and embedded keywords
```

The family prefix is itself sometimes a two-segment compound (`nv_abi_pragma_*`, `nv_atomic_load_*`, `nv_diag_*`) — the parser must allow the longest-match prefix rather than splitting on the first underscore.

## Prefix Family Inventory

The 200+ prefix families fall into roughly nine functional clusters. The tables below list the most populated families with representative members and the role they play in dispatch.

### Language Standard / Dialect Diagnostics

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `cxx_` | 52 | C++ feature-availability and conformance | `--no_cxx_feature_test`, per-feature `-W` | `cxx_constexpr`, `cxx_generic_lambdas`, `cxx_aggregate_nsdmi` |
| `c11_` | 5 | C11 atomic builtin diagnostics | `--no_c11_atomic` | `c11_atomic_builtins_malformed`, `c11_atomic_bit_field` |
| `c23_` | 6 | C23 deprecation warnings for legacy spellings | `--no_c23_compat_warnings` | `c23_alignas_deprecated`, `c23_bool_deprecated` |
| `cl_` | 128 | Command-line option diagnostics | Cannot be suppressed (CLI errors are always fatal) | `cl_ambiguous_option`, `cl_back_end_requires_il_file` |
| `nonstd_` | 34 | Non-standard extension warnings | `--no_anachronisms`, `-Wno-nonstandard` | `nonstd_long_long`, `nonstd_implicit_int`, `nonstd_friend_decl` |

The `cl_*` family is unique: these diagnostics fire during command-line parsing before the EDG diagnostic dispatch is fully initialised, so they bypass the normal suppression path. They are always emitted via the early-exit error path in `lgenfe_main`.

### Microsoft-Compatible Language Modes

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `cppcx_` | 20 | C++/CX (Windows Runtime) extension errors | `--no_cppcx`, gated by `--microsoft_compatibility_mode` | `cppcx_array_only_one_dimension_allowed`, `cppcx_public_value_class_constructor` |
| `cppcli_` | — | C++/CLI managed code restrictions | `--no_cppcli`, gated by `--microsoft_compatibility_mode` | (compound prefix; folded into `cli_*` table entries) |
| `cli_` | 18 | Managed type and member rules | `--no_cppcli` | `cli_array_invalid_element_type`, `cli_typeid_of_managed_pointer` |

The `cl_cppcli_only_in_microsoft_cplusplus` and `cl_cppcx_and_cppcli` identifiers are gating diagnostics emitted when these modes are requested outside Microsoft compatibility — they are `cl_*` family members rather than `cppcli_*` or `cppcx_*` because they fire during CLI parsing.

### CUDA / NVIDIA-Specific Diagnostics

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `nv_` | 36 | NVIDIA frontend extensions, atomic builtins, pragma handling | Per-diagnostic `nv_diag_suppress` | `nv_exec_check_disable`, `nv_atomic_load_order_error` |
| `nv_abi_pragma_*` | 9 | `#pragma nv_abi_*` parse and validation | `nv_diag_suppress` | `nv_abi_pragma_bad_format`, `nv_abi_pragma_overflow_value` |
| `nv_atomic_*` | 11 | Atomic operation legality in device code | `nv_diag_suppress` | `nv_atomic_operations_scope_fallback_to_membar` |
| `nv_diag_*` | 7 | The pragma directive identifiers themselves (recognised as keywords, not emitted) | n/a | `nv_diag_suppress`, `nv_diag_warning`, `nv_diag_default`, `nv_diag_once`, `nv_diagnostic` |
| `cuda_` | 5 | CUDA-specific frontend errors | `nv_diag_suppress` | `cuda_device_code_unsupported_operator`, `cuda_demote_unsupported_floating_point` |

The seven `nv_diag_*` strings are themselves *not* diagnostic identifiers — they are the pragma directive keywords recognised by the lexer. The parser reads `#pragma nv_diag_suppress <id>` and uses `<id>` (which may be a numeric value or a symbolic name from this catalogue) as the suppression key.

### Pragma Handling

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `pragma_` | 8 | Generic `#pragma` placement and parsing | Always emitted (parse errors) | `pragma_inside_function`, `pragma_must_precede_declaration`, `pragma_pack_show_args_ignored` |

The `#pragma diagnostic push` / `#pragma diagnostic pop` pair is parsed separately and produces the literal error message `no '#pragma diagnostic push' was found to match this 'diagnostic pop'` rather than a snake_case identifier — these are among the few diagnostics with no catalogue entry.

### Template and Concept Diagnostics

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `template_` | 27 | Template parameter, argument, instantiation errors | `-Wno-template-*`, per-diagnostic suppression | `template_arg_cannot_point_to_subobject`, `template_param_pack_not_at_end` |
| `ambiguous_` / `ambig_` | 26 | Overload resolution and lookup ambiguities | `-Wno-ambiguous-*` | `ambiguous_overloaded_function`, `ambiguous_partial_spec`, `ambig_literal_operator` |
| `default_` | 25 | Defaulted special-member function rules | `--no_defaulted_functions` | (within `cxx_defaulted_functions` family and standalone `default_*` entries) |

The two spellings `ambiguous_` (long form) and `ambig_` (abbreviated) are *not* synonyms — they refer to different diagnostic record types. `ambig_suppresses_copy_asgn_decl` is emitted as a *child note* attached to a parent `ambiguous_assignment_operator` diagnostic. The abbreviated form indicates "this diagnostic exists only as a sub-entry of a longer diagnostic chain". Reimplementations that fold the two into one prefix will produce different child-list structures than the original.

### Attributes and ABI

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `attribute_` | 20 | `__attribute__` / `[[...]]` placement and validity | `-Wattribute-*` | `attribute_ignored_on_unnamed_type`, `attribute_deprecated_with_message` |
| `attr_` | 28 | Attribute compatibility with declaration kind | `-Wattribute-*` | `attr_disallows_pure_virtual_function`, `attr_arg_out_of_small_integer_range` |
| `abi_` | 4 | `[[gnu::abi_tag]]` and Itanium ABI features | `--no_abi_tag` | `abi_tag_ignored`, `abi_tag_redefinition` |
| `align_*` / `alignof_*` / `alignment_*` | 7 | Alignment specifier and `alignof` operator | Generally always emitted | `alignment_reduction_ignored`, `alignof_incomplete_array` |

### `constexpr` / `consteval` Diagnostics

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `constexpr_` | 111 | Compile-time evaluation errors and constraint violations | Always emitted (these are language requirements) | `constexpr_access_one_past_array_end`, `constexpr_bad_deallocation_size` |
| `invalid_constexpr` / `invalid_consteval` | — | Specifier misuse on declarations | Always emitted | `invalid_constexpr`, `invalid_consteval` |

The `constexpr_*` family is the single largest functional cluster (111 identifiers). It mirrors the structure of the EDG constant evaluator's error states one-to-one: each evaluation failure path has its own identifier, and identifiers ending in `_pos` (`constexpr_allocation_pos`) are companion records that carry the position of an earlier note rather than freestanding diagnostics.

### OpenMP and Parallelism

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `omp_` | 34 | OpenMP runtime entrypoint declarations (header-emitted) | n/a — these are predeclared library names | `omp_get_thread_limit`, `omp_get_max_active_levels` |
| `openmp_` | 0 | (No diagnostics use this prefix despite the namespace) | n/a | n/a |

The `omp_*` strings are *not* diagnostic identifiers. They are predeclared OpenMP runtime function names that the EDG header emits when `--openmp` is enabled. They appear in the string table because the binary builds OpenMP support, but they belong to the runtime API layer rather than the diagnostic catalogue.

### Module-Boundary (IFC) Diagnostics

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `ifc_` | 26 | C++20 modules IFC (interface) file consistency | `--no_modules` | `ifc_bad_function_definition`, `ifc_partition_mismatch`, `ifc_too_many_template_args` |

The IFC family fires during module import when the imported BMI's table-of-contents disagrees with what the current translation unit expects. These are always fatal — there is no per-diagnostic suppression because an inconsistent module file would produce silent miscompilation.

### Generic Bad/Invalid/Expected Diagnostics

| Prefix | Count | Scope | Suppression Flag | Example Identifier |
|---|---|---|---|---|
| `bad_` | 216 | Catch-all malformed input or unexpected state | Per-diagnostic | `bad_alias_templ_redecl`, `bad_attribute_template_substitution` |
| `invalid_` | 149 | Specifier / qualifier / argument validity errors | Per-diagnostic | `invalid_access_specifier`, `invalid_bit_cast_type` |
| `no_` | 195 | Feature-not-available errors (driven by `--no_*` CLI flags) | n/a — controlled by the gating flag itself | `no_aligned_new`, `no_array_designators_in_cpp_mode` |
| `exp_` | 59 | Expected-token parse errors | Always emitted | `exp_class_or_typename`, `exp_comma_or_rbracket`, `exp_concept_name` |
| `missing_` | 60 | Required clause/keyword missing | Always emitted | (member-context errors during declaration parsing) |

The `no_*` family is the inverse of a normal diagnostic family: rather than reporting that something *happened*, these identifiers report that a feature is *unavailable*. They fire when source code uses a construct that has been disabled via `--no_<feature>` on the command line, and the corresponding identifier name typically embeds the flag name itself (`no_aligned_new` ↔ `--no_aligned_new`).

## Severity Mapping

EDG severity is stored at offset `+180` of the diagnostic record as a single byte. The identifier name does not directly encode severity — the same identifier can be emitted at different severities depending on context and `-W*` flags. The byte values observed:

| Severity Code | Meaning | Default Suppression | Promotable to Error |
|---|---|---|---|
| 0 | Internal info | Suppressed unless `--verbose` | No |
| 1 | Remark (informational note) | Suppressed by default | `nv_diag_warning` → 4 |
| 2 | Note (attached to parent) | Always emitted with parent | No |
| 3 | Warning (deprecation, anachronism) | Emitted by default | `-Werror=<id>` → 4 |
| 4 | Error | Always emitted | Already error |
| 5 | Catastrophic / parse-stop | Always emitted | Already error |

Severity 5 diagnostics force the compiler to abort the current translation unit immediately after rendering. The dispatch logic in `sub_6837D0` increments `unk_4F074B0` (error count) for severity 4 and `unk_4F074B8` for catastrophic, and when either threshold reaches `unk_4F07478` (error limit, set by `--error_limit`), diagnostic number 1508 is emitted and the compiler exits.

## Suppression Mechanics

Five suppression channels stack in priority order — once any channel suppresses a diagnostic, the lower-priority channels are not consulted:

1. **Severity floor.** `byte_4F07481[0]` holds the minimum severity to emit. Diagnostics below this threshold are dropped before any other check.
2. **Duplicate dedup.** `byte_4CFFE80[4*errnum+2]` carries per-numeric-ID bit flags; identical diagnostic numbers within the same source range are counted once and emitted once.
3. **`#pragma diag_suppress`.** `sub_67D520` walks a per-source-position list of active suppressions installed by `#pragma nv_diag_suppress`. The pragma accepts both numeric IDs and the symbolic names from this catalogue.
4. **Command-line `-W` flags.** The flag table built at CLI parse time maps numeric IDs and prefix patterns (like `-Wno-template-*`) to severity overrides.
5. **Error-limit cutoff.** When the running error count exceeds `--error_limit`, diagnostic 1508 fires and further diagnostics are dropped.

The `nv_diag_default` pragma resets a previously-modified diagnostic to its compile-time default severity. The `nv_diag_once` pragma installs a once-per-translation-unit suppression after the first emission.

## Deprecation Behaviour

Identifiers ending in `_deprecated`, `_ignored`, or starting with `c23_*_deprecated` participate in a uniform deprecation chain. When the parent diagnostic fires:

1. The parent is emitted at severity 3 (warning) with the deprecation message text.
2. A child note is enqueued in `note_list` with severity 2 carrying a "use X instead" hint. The child's identifier is derived from the parent by suffix replacement.
3. If `--Werror=deprecated` is in effect, the parent is promoted to severity 4 *and* the child note is promoted to severity 3 to remain visible.

The `attribute_deprecated_with_message` identifier is special: it accepts a custom message string supplied via `[[deprecated("...")]]` and embeds it into the rendered output via format placeholder `%s`. The identifier itself is fixed; only the message text varies.

## QUIRKs

**QUIRK 1: Two namespaces with identical-looking prefixes.** The `omp_*` and `strict_f*` identifiers look like diagnostic identifiers (they match the snake_case pattern) but belong to entirely different subsystems. `omp_*` are OpenMP runtime function names emitted into predeclared headers; `strict_f*` are LLVM strict-FP intrinsic operation names appearing in `SelectionDAG` lowering tables. Treating either as a diagnostic identifier — passing either to `#pragma nv_diag_suppress` — will produce a "diagnostic number not found" error from `sub_67D520` because the build-time identifier-to-number resolution skips both prefixes. The 4,539 count given in this page is for the raw string match; the true diagnostic count is closer to 4,200 once these two namespaces are excluded.

**QUIRK 2: `ambig_*` records are not standalone diagnostics.** The five `ambig_*` identifiers (`ambig_literal_operator`, `ambig_suppresses_copy_asgn_decl`, `ambig_suppresses_copy_ctor_decl`, and two related entries) are emitted only as *child notes* of a parent `ambiguous_*` diagnostic. They have their own diagnostic numbers and can be individually suppressed by number, but suppressing the parent silently suppresses all `ambig_*` children attached to it — even if the children are configured as `nv_diag_error` and would otherwise be promoted. The interaction is one-directional: promoting an `ambig_*` child has no effect on the parent's severity.

**QUIRK 3: The `_pos` suffix marks position-only continuation records.** Several `constexpr_*` identifiers come in pairs: `constexpr_allocation_mismatch` is the primary diagnostic carrying the error message, and `constexpr_allocation_pos` is a continuation record carrying *only* a source-location annotation pointing at the original allocation site. The continuation record's `+180` severity byte is always 2 (note), and its `+96` `has_source_location` is always nonzero. Reimplementations that emit only the primary diagnostic without the `_pos` companion produce technically-valid output but lose the secondary source-position annotation that IDEs rely on to draw the "see previous allocation here" link. The `_pos` records are essentially severity-2 placeholders whose entire purpose is to carry a second source location that the EDG diagnostic record format has no other slot for.

## Reimplementation Checklist

A reimplementation that aims for byte-identical SARIF output must, in order:

1. **Lift the identifier set.** Extract the snake_case lowercase strings from the binary's read-only string table at the addresses CICC v13.0 places them. The total cardinality is 4,539 raw matches; the diagnostic-only subset (excluding `omp_*` runtime names and `strict_f*` LLVM intrinsics) is ~4,200.
2. **Preserve prefix grammar.** The longest-match prefix rule matters: `nv_abi_pragma_bad_format` parses as family `nv_abi_pragma` with tail `bad_format`, not as `nv` + `abi_pragma_bad_format`. The suppression key derivation depends on getting the family split right.
3. **Assign numeric IDs deterministically.** EDG's identifier-to-number resolution runs once at init and assigns sequential numeric IDs in a fixed traversal order. Changing the order changes every `ruleId` in SARIF output and every `-W#####` flag.
4. **Wire the `ambig_*` ↔ `ambiguous_*` parent-child linkages.** The five abbreviated identifiers must be attached to their long-form parents in `child_list`, not emitted as top-level diagnostics.
5. **Wire the `_pos` continuation records.** Every `constexpr_*_pos` identifier must be attached as a severity-2 note to the corresponding primary `constexpr_*` diagnostic, carrying only a source location.
6. **Implement the suppression stack.** All five priority levels (severity floor, dedup, `#pragma diag_suppress`, `-W` flags, error-limit cutoff) must be present and ordered correctly. Missing any level produces visible behavioural differences.
7. **Honour the deprecation child-emission rule.** Identifiers that emit a deprecation warning must enqueue a severity-2 child note with the matching "use X instead" identifier. The `--Werror=deprecated` promotion must lift both parent and child by one severity step.

## Cross-References

- [Diagnostics & Optimization Remarks](./diagnostics.md) — record layout, dispatch architecture, terminal and SARIF renderers.
- [CICC Pipeline](../pipeline/index.md) — where EDG diagnostic emission sits in the overall compilation flow.
- [Methodology](../methodology.md) — how the string table was mined and how identifiers were grouped.
