# Overload Resolution

The overload resolution engine in cudafe++ is EDG 6.6's implementation of the C++ overload resolution algorithm (ISO C++ [over.match]). It lives in `overload.c` — approximately 100 functions spanning address range `0x6BE4A0`--`0x6EF7A0` (roughly 200KB of compiled code). Overload resolution is one of the most complex subsystems in any C++ compiler because it sits at the intersection of nearly every other language feature: implicit conversions, user-defined conversions, template argument deduction, SFINAE, partial ordering, reference binding, list initialization, copy elision, and operator overloading each contribute decision branches to the algorithm. EDG implements the standard three-phase architecture — candidate collection, viability checking, best-viable selection — with NVIDIA-specific extensions for CUDA execution-space filtering.

## Key Facts

| Property | Value |
|---|---|
| Source file | `overload.c` (~100 functions) |
| Address range | `0x6BE4A0`--`0x6EF7A0` |
| Total code size | ~200KB |
| Main selection entry | `sub_6E6400` (`select_overloaded_function`, 1,483 lines, 20 parameters) |
| Operator dispatch | `sub_6EF7A0` (`select_overloaded_operator`, 2,174 lines) |
| Viability checker | `sub_6E2040` (`determine_function_viability`, 2,120 lines) |
| Candidate evaluator | `sub_6C4C00` (candidate evaluation, 1,044 lines) |
| Main driver | `sub_6CE6E0` (overload resolution driver, 1,246 lines) |
| Built-in candidates | `sub_6CD010` (built-in operator candidates, 752 lines) |
| Candidate iterator | `sub_6E4FA0` (`try_overloaded_function_match`, 633 lines) |
| Conversion scoring | `sub_6BEE10` (`standard_conversion_sequence`, 375 lines) |
| ICS comparison | `sub_6CBC40` (implicit conversion sequence comparison, 345 lines) |
| Qualification compare | `sub_6BE6C0` (`compare_qualification_conversions`, 127 lines) |
| Copy constructor select | `sub_6DBEA0` (`select_overloaded_copy_constructor`, 625 lines) |
| Default constructor select | `sub_6E9080` (`select_overloaded_default_constructor`, 358 lines) |
| Assignment operator select | `sub_6DD600` (`select_overloaded_assignment_operator`, 492 lines) |
| CTAD entry | `sub_6E8300` (`deduce_class_template_args`, 285 lines) |
| List initializer | `sub_6D7C80` (`prep_list_initializer`, 2,119 lines) |
| Overload set traversal | `sub_6BA230` (iterate overload set) |
| Overload debug trace | `dword_126EFC8` (enable), `qword_106B988` (output stream) |
| CUDA extensions flag | `byte_126E349` |
| Language mode | `dword_126EFB4` (2 = C++) |
| Standard version | `dword_126EF68` (201103 = C++11, 201703 = C++17, 202301 = C++23) |

## Why Overload Resolution Is Hard

Overload resolution is not a simple "find the best match" operation. The C++ standard defines it as a partial ordering problem over implicit conversion sequences, where each sequence is itself a multi-step chain of type transformations. The key sources of complexity:

1. **Implicit conversion sequences (ICS).** Each argument-to-parameter match produces an ICS consisting of up to three steps: a standard conversion (lvalue-to-rvalue, array-to-pointer, etc.), optionally a user-defined conversion (constructor or conversion function), then another standard conversion. Ranking two ICSs against each other requires comparing each step independently.

2. **User-defined conversions.** When no standard conversion exists, the compiler must search for converting constructors on the target type AND conversion operators on the source type, then perform a nested overload resolution among those candidates. This creates recursive invocations of the overload engine.

3. **Template argument deduction.** Function templates produce candidates only after deduction succeeds. Deduction may fail (SFINAE), producing no candidate. Successfully deduced candidates participate in a separate tie-breaking rule: non-template functions are preferred over template specializations, and "more specialized" templates are preferred over "less specialized" ones ([over.match.best] p2.5).

4. **Partial ordering.** When comparing two function templates that are both viable, the compiler must determine which is "more specialized" by attempting deduction in both directions (templates.c handles this). The result feeds back into overload ranking.

5. **Operator overloading.** Built-in operators (like `+` on `int`) compete with user-defined `operator+`. The compiler synthesizes "built-in candidate functions" representing every valid built-in operator signature, adds them to the candidate set alongside user-defined operators, and runs the same best-viable algorithm on the combined set.

6. **Special contexts.** Copy-initialization vs. direct-initialization, list-initialization, reference binding, and conditional-operator type determination each have their own overload resolution sub-procedures with modified candidate sets and ranking rules.

## Architecture: Three-Phase Pipeline

```text
                          PHASE 1                    PHASE 2                   PHASE 3
                     Candidate Collection        Viability Check         Best-Viable Selection
                    ┌───────────────────┐     ┌──────────────────┐     ┌──────────────────────┐
                    │                   │     │                  │     │                      │
  f(args...) ──────►│ Name lookup       │────►│ For each cand:   │────►│ Pairwise comparison  │
                    │ ADL (arg-dep.)    │     │  - param count   │     │ of viable candidates │
                    │ Using-declarations│     │  - conversions   │     │ via ICS ranking      │──► winner
                    │ Template deduction│     │  - constraints   │     │                      │
                    │ Built-in synth    │     │  - access check  │     │ Tie-breakers:        │──► or ambiguity
                    │                   │     │                  │     │  - non-template pref │
                    └───────────────────┘     └──────────────────┘     │  - partial ordering  │──► or no match
                                                                      │  - cv-qual ranking   │
                                                                      └──────────────────────┘
```

### Phase 1: Candidate Collection

Candidates are collected into an overload set — a linked list of entries allocated via `sub_6BA0D0` and iterated via `sub_6BA230`. The overload set is built by the caller before invoking `select_overloaded_function`. Sources of candidates include:

- **Name lookup results.** All declarations visible by name at the call site, including base class members and using-declarations.
- **Argument-dependent lookup (ADL).** Additional functions found by searching the associated namespaces of the argument types (Koenig lookup). These are added to the set by the name lookup machinery before overload resolution begins.
- **Template specializations.** For each function template in the name lookup result, template argument deduction is attempted. If deduction succeeds, the resulting specialization is added as a candidate. If deduction fails, the template is silently dropped (SFINAE).
- **Built-in operator candidates.** For operator expressions, `sub_6CD010` synthesizes candidate functions representing every valid built-in operator signature for the given operand types. These synthetic candidates use single-character type classification codes to match operand patterns.

### Phase 2: Viability Checking

`determine_function_viability` (`sub_6E2040`, 2,120 lines) is the core viability checker. For each candidate function, it determines whether all arguments can be implicitly converted to the corresponding parameter types.

```c
determine_function_viability (sub_6E2040, 2120 lines)
    Input:  candidate function F, argument list A[0..n-1]
    Output: viability flag, per-argument conversion summaries

    // Guard: SFINAE context handling
    if (in_sfinae_context)
        push_diagnostic_suppression()

    // PASS 1: Basic eligibility
    if (F is deleted)
        return NOT_VIABLE
    if (F is template && deduction_failed)
        return NOT_VIABLE
    if (F has fewer params than args && !F.is_variadic)
        return NOT_VIABLE
    if (F has more params than args && excess params lack defaults)
        return NOT_VIABLE

    // Handle implicit 'this' parameter for member functions
    if (F is non-static member function) {
        this_match = selector_match_with_this_param(
            object_operand, F.this_param_type)       // sub_6D0A80
        if (this_match == FAILED)
            return NOT_VIABLE
    }

    // PASS 2: Per-argument conversion check
    for i in 0..n-1:
        log("determine_function_viability: arg %d", i)

        param_type = F.params[i].type
        arg_type   = A[i].type

        // Compute implicit conversion sequence
        ics = compute_standard_conversion_sequence(   // sub_6BEE10
                  arg_type, param_type, context_flags)

        if (ics == NO_CONVERSION) {
            // Try user-defined conversion
            ics = try_user_defined_conversion(
                      arg_type, param_type)           // sub_6BF610
            if (ics == NO_CONVERSION)
                return NOT_VIABLE
        }

        // Check narrowing for list-initialization
        if (context == LIST_INIT && ics.is_narrowing)
            return NOT_VIABLE

        // Record per-argument match summary
        summaries[i] = ics

    log("(pass 2)")  // second pass through for detailed scoring

    // All arguments convertible -- candidate is viable
    return VIABLE, summaries[]
```

The function implements a two-pass approach visible in the debug trace output: pass 1 performs a quick rejection check (parameter count, deleted status, deduction success), and pass 2 computes the full conversion sequence for each argument. The per-argument summaries are stored in a 48-byte structure (`set_arg_summary_for_user_conversion` at `sub_6BE990` initializes these).

### Phase 3: Best-Viable Selection

`select_overloaded_function` (`sub_6E6400`, 1,483 lines, 20 parameters) performs the final selection. It is the master entry point for overload resolution — called from the expression parser, from CTAD, and from special member function selection.

```c
select_overloaded_function (sub_6E6400, 1483 lines, 20 params)
    Input:  overload_set, arg_list, context_flags, ...
    Output: best_function or AMBIGUOUS or NO_MATCH

    log("Entering select_overloaded_function with ...")

    // Early exit: dependent type arguments => defer to instantiation time
    if (selector_type_is_dependent)
        return DEPENDENT

    // Step 1: Iterate candidates and check viability
    viable_set = []
    try_overloaded_function_match(                    // sub_6E4FA0
        overload_set, arg_list, &viable_set, ...)

    if (viable_set is empty)
        return NO_MATCH

    if (viable_set has exactly 1 candidate)
        return viable_set[0]

    // Step 2: Pairwise comparison of viable candidates
    //   For each pair (F1, F2), compare their ICS for each argument
    best = viable_set[0]
    ambiguous = false

    for each candidate C in viable_set[1..]:
        cmp = compare_candidates(best, C)
        //   compare_candidates calls compare_conversion_sequences (sub_6BFF70)
        //   for each argument position, and applies tie-breakers

        if (cmp == C_IS_BETTER)
            best = C
            ambiguous = false
        else if (cmp == NEITHER_BETTER)
            ambiguous = true

    // Step 3: Verify best is strictly better than ALL others
    if (ambiguous) {
        // Final check: is there a single candidate that beats all?
        for each candidate C in viable_set:
            if (C != best) {
                cmp = compare_candidates(best, C)
                if (cmp != BEST_IS_BETTER)
                    return AMBIGUOUS
            }
    }

    return best
```

#### Candidate Comparison Rules

The pairwise comparison between two viable candidates F1 and F2 follows [over.match.best]. The result is one of: F1-better, F2-better, or indistinguishable.

```c
compare_candidates(F1, F2):
    // Rule 1: Compare implicit conversion sequences argument-by-argument
    f1_better_count = 0
    f2_better_count = 0

    for i in 0..n-1:
        cmp = compare_conversion_sequences(           // sub_6BFF70
                  F1.ics[i], F2.ics[i])
        if (cmp == F1_BETTER) f1_better_count++
        if (cmp == F2_BETTER) f2_better_count++

    if (f1_better_count > 0 && f2_better_count == 0)
        return F1_IS_BETTER
    if (f2_better_count > 0 && f1_better_count == 0)
        return F2_IS_BETTER

    // Rule 2: Non-template preferred over template
    if (F1 is non-template && F2 is template)
        return F1_IS_BETTER
    if (F2 is non-template && F1 is template)
        return F2_IS_BETTER

    // Rule 3: More-specialized template preferred
    if (both are templates) {
        partial = partial_ordering(F1.template, F2.template)
        if (partial == F1_MORE_SPECIALIZED)
            return F1_IS_BETTER
        if (partial == F2_MORE_SPECIALIZED)
            return F2_IS_BETTER
    }

    // Rule 4: Compare qualification conversions
    cmp_qual = compare_qualification_conversions(     // sub_6BE6C0
                   F1.qual_info, F2.qual_info)
    if (cmp_qual != 0)
        return cmp_qual

    return NEITHER_BETTER
```

## Implicit Conversion Sequence (ICS) Model

An ICS is the sequence of transformations needed to convert an argument type to a parameter type. EDG computes and stores ICS information in a compact structure.

### Standard Conversion Sequence

`standard_conversion_sequence` (`sub_6BEE10`, 375 lines) computes the standard-conversion component of an ICS. It produces a conversion rank used in comparison.

| Rank | Name | Examples | Priority |
|---|---|---|---|
| Exact Match | No conversion needed | `int` to `int`, lvalue-to-rvalue | 1 (best) |
| Promotion | Integer/float promotion | `short` to `int`, `float` to `double` | 2 |
| Conversion | Standard conversion | `int` to `double`, derived-to-base | 3 |
| User-Defined | User conversion + std conversion | `Foo` to `Bar` via constructor | 4 |
| Ellipsis | Match via `...` parameter | Any type to variadic | 5 (worst) |

Within the same rank, additional criteria refine the comparison:

1. **Qualification adjustment.** `const T` to `T` is worse than `T` to `T`. `compare_qualification_conversions` (`sub_6BE6C0`) encodes cv-qualification as a bitmask (`const` = 0x20, `volatile` = 0x40, `restrict` = 0x80) and compares subset relationships.
2. **Derived-to-base distance.** Conversion through a shorter inheritance chain is better. Checked via `sub_7AB300`.
3. **Reference binding.** Binding to `T&&` is preferred over binding to `const T&` when the argument is an rvalue.

### User-Defined Conversion Sequence

When no standard conversion exists, `try_conversion_function_match_full` (`sub_6D0F50`, 1,085 lines) searches for a user-defined conversion path. It considers:

1. **Converting constructors** on the target type (non-explicit constructors that accept the source type).
2. **Conversion functions** on the source type (`operator T()` members).

For each candidate conversion, it checks:

```c
try_conversion_function_match_full (sub_6D0F50, 1085 lines)
    Input:  source_class_type, dest_type, context_flags
    Output: selected conversion function/constructor, or AMBIGUOUS, or NONE

    log("considering conversion functions for [%lu.%d]")

    if (source is not class type)
        error("try_conversion_function_match_full: source not class")

    // Iterate conversion function candidates of source class
    for each conv_func in source_class.conversion_functions:  // via sub_6BA230
        return_type = conv_func.return_type
        if (return_type is compatible with dest_type) {
            // Check standard conversion from return_type to dest_type
            post_ics = compute_standard_conversion_sequence(
                           return_type, dest_type)
            if (post_ics != NO_CONVERSION)
                add_to_viable(conv_func, post_ics)
        }

    // Also check for converting constructors on dest type
    conversion_from_class_possible(                  // sub_6D28C0/6D2ED0
        source_class_type, dest_type, &viable_set)

    // Select best among viable user-defined conversions
    if (viable_set has 1 candidate)
        return viable_set[0]
    if (viable_set has multiple candidates)
        return best-of or AMBIGUOUS

    return NONE
```

The `conversion_from_class_possible` functions (`sub_6D28C0` 252 lines, `sub_6D2ED0` 293 lines) emit full debug traces with entry/exit messages:

```text
Entering conversion_from_class_possible, dest_type = <type>
Candidate functions list: ...
Leaving conversion_from_class_possible: <result>
```

## The Main Overload Resolution Driver

`sub_6CE6E0` (1,246 lines) is the central driver function — "THE MONSTER" — that coordinates the overload resolution pipeline. It is called from `determine_selector_match_level` and from the candidate evaluation logic, acting as the type-comparison and scoring backbone that feeds the higher-level selection functions.

```c
overload_resolution_driver (sub_6CE6E0, 1246 lines)
    // This function performs the detailed type comparison and conversion
    // sequence computation that determines how well a candidate matches.
    //
    // It is called per-candidate, per-argument-position from the viability
    // checker and the candidate evaluator.

    // 1. Quick identity check
    if (arg_type == param_type)
        return EXACT_MATCH

    // 2. Chase typedef chains to canonical types
    arg_canon  = canonical_type(arg_type)
    param_canon = canonical_type(param_type)

    // 3. Apply lvalue-to-rvalue conversion
    if (param expects rvalue && arg is lvalue)
        apply lvalue_to_rvalue conversion, record in ICS

    // 4. Apply array-to-pointer / function-to-pointer decay
    if (arg is array)      convert to pointer-to-element
    if (arg is function)   convert to pointer-to-function

    // 5. Check for standard conversions (integral promotion, float promotion,
    //    integral conversion, floating conversion, pointer conversion,
    //    pointer-to-member conversion, boolean conversion)
    std_conv = find_applicable_standard_conversion(arg_canon, param_canon)
    if (std_conv != NONE)
        return std_conv with rank

    // 6. Check for qualification conversion (add const/volatile)
    qual_conv = check_qualification_conversion(arg_canon, param_canon)
    if (qual_conv)
        return EXACT_MATCH with qual adjustment

    // 7. Check derived-to-base conversion
    if (is_class(arg_canon) && is_class(param_canon)) {
        if (is_derived_from(arg_canon, param_canon))
            return CONVERSION_RANK with derived-to-base marker
    }

    // 8. No standard conversion found
    return NO_CONVERSION
```

### Candidate Evaluation Function

`sub_6C4C00` (1,044 lines) is the candidate evaluation function — it scores each candidate by computing the full set of implicit conversion sequences across all arguments and produces the data that `compare_candidates` uses.

```c
evaluate_candidate (sub_6C4C00, 1044 lines)
    Input:  candidate F, argument list args[], match_context
    Output: per-argument ICS array, overall viability

    for each argument position i:
        // Compute the implicit conversion sequence
        ics = overload_resolution_driver(             // sub_6CE6E0
                  args[i].type, F.params[i].type, flags)

        if (ics == NO_CONVERSION) {
            // Try user-defined conversion
            udc = try_user_defined_conversion(args[i].type, F.params[i].type)
            if (udc == NONE)
                mark F as non-viable for position i
                return NON_VIABLE
            ics = user_defined_ics(udc)
        }

        // Record the ICS for this position
        F.arg_summaries[i] = ics

    // Compute overall match quality
    F.match_level = worst(F.arg_summaries[0..n-1])
    return VIABLE
```

## Candidate Iteration

`try_overloaded_function_match` (`sub_6E4FA0`, 633 lines, and variant `sub_6E5B20`, 367 lines) iterates the overload set and calls `determine_function_viability` for each candidate.

```c
try_overloaded_function_match (sub_6E4FA0, 633 lines)
    Input:  overload_set, arg_list, context
    Output: viable_candidates[]

    log("try_overloaded_function_match")

    // Traverse the overload set
    cursor = overload_set.head
    while (cursor != NULL):                           // via sub_6BA230

        candidate = cursor.function
        log("try_overloaded_function_match: considering %s",
            candidate.name)                           // via sub_5B72C0

        // Set up traversal symbol for template deduction
        set_overload_set_traversal_symbol(cursor)

        // Check viability
        viable = determine_function_viability(        // sub_6E2040
                     candidate, arg_list, context)

        if (viable) {
            add candidate to viable_candidates[]
            record conversion summaries
        }

        cursor = cursor.next
```

## Operator Overloading

Operator overloading resolution follows a specialized path because it must consider both user-defined operators AND synthesized built-in operator candidates.

### Entry Point: select_overloaded_operator

`sub_6EF7A0` (2,174 lines) is the master entry point for operator overloading. It is called from the expression parser whenever an operator expression involves a class-type operand.

```c
select_overloaded_operator / check_for_operator_overloading
    (sub_6EF7A0, 2174 lines)
    Input:  operator_kind, lhs_operand, rhs_operand (if binary), context
    Output: selected function (user-defined or built-in), or use-builtin flag

    log("Entering check_for_operator_overloading")

    // Guard: dependent operands => defer
    if (lhs is dependent || rhs is dependent)
        log("check_for_operator_overloading: dep operand")
        return DEPENDENT

    // Step 1: Collect user-defined operator candidates
    //   Search member operators of lhs class
    //   Search non-member operators via name lookup + ADL
    user_candidates = collect_user_operator_candidates(
                          operator_kind, lhs, rhs)

    // Step 2: Generate built-in operator candidates
    builtin_candidates = generate_builtin_candidates(   // sub_6CD010
                             operator_kind, lhs.type, rhs.type)

    // Step 3: Combine candidate sets
    combined = user_candidates + builtin_candidates

    // Step 4: Run standard overload resolution on combined set
    result = select_overloaded_function(                 // sub_6E6400
                 combined, [lhs, rhs], OPERATOR_CONTEXT)

    if (result is a built-in candidate) {
        // Adjust operands for built-in semantics
        adjust_operand_for_builtin_operator(             // sub_6E0E50
            lhs, rhs, operator_kind)
        return USE_BUILTIN
    }

    log("Leaving f_check_for_operator_overloading")
    return result.function
```

### Built-in Operator Candidate Generation

`sub_6CD010` (752 lines) generates synthetic candidate functions representing built-in operators. It uses a type classification code scheme where each type category is encoded as a single character.

#### Type Classification Codes

| Code | Meaning | Query Function |
|---|---|---|
| `A` / `a` | Arithmetic type | `sub_7A7590` (`is_arithmetic`) |
| `B` | Boolean type | is_bool |
| `b` | Boolean-equivalent | is_pointer/bool |
| `C` | Class type | `sub_7A8A30` (`is_class`) |
| `D` / `I` / `i` | Integer/integral type | `sub_7A71E0` (`is_integral`) |
| `E` | Enum type | `sub_7A70F0` (`is_enum`) |
| `F` | Pointer-to-function | is_function_pointer |
| `H` | Handle type (CLI) | is_handle |
| `M` | Pointer-to-member | `sub_7A8D90` (`is_member_pointer`) |
| `N` | nullptr_t | is_nullptr |
| `O` | Pointer-to-object | is_object_pointer |
| `P` | Pointer (any) | is_pointer |
| `S` | Scoped enum | is_scoped_enum |
| `h` | Handle-to-CLI-array | is_handle_array |
| `n` | Non-bool arithmetic | is_non_bool_arithmetic |

The function `matches_type_code` (`sub_6BECA0`) dispatches on these codes to check whether an operand matches a candidate pattern. The function `name_for_type_code` (`sub_6BE4A0`, 67 lines) converts codes to human-readable strings for diagnostics (e.g., `A` becomes `"arithmetic"`).

#### Candidate Pattern Matching

`try_builtin_operands_match` (`sub_6ED2A0`, 812 lines) matches operands against built-in operator patterns. The patterns are encoded as strings like `"A;P"` where each character is a type code and `;` separates operand positions.

```c
try_builtin_operands_match (sub_6ED2A0, 812 lines)
    Input:  operator_kind, pattern_string, operand types
    Output: match result

    log("try_builtin_operands_match: considering %s", pattern_string)

    for i in 0..num_operands-1:
        code = pattern_string[i]    // after skipping separators
        log("try_builtin_operands_match: operand %d", i)

        if (!matches_type_code(operand[i].type, code))
            log("try_builtin_operands_match: ran off pattern")
            return NO_MATCH

    return MATCH with conversion cost
```

`try_conversions_for_builtin_operator` (`sub_6EE340`, 1,058 lines) contains a large switch over operator kinds that selects the appropriate type pattern tables. It checks `dword_126EF68` for C++17 features (`>= 201703`) and `dword_126EFB4` for language mode.

## Special Member Function Selection

Overload resolution for special member functions uses dedicated entry points that share the same underlying machinery but provide specialized candidate sets and matching rules.

### Copy/Move Constructor Selection

```c
select_overloaded_copy_constructor (sub_6DBEA0, 625 lines)
    Input:  class_type, source_operand, context_flags
    Output: selected constructor symbol, or NULL

    log("Entering select_overloaded_copy_constructor, class_type = %s",
        class_type.name)

    // Iterate all constructors of the class
    for each ctor in class_type.constructors:         // via sub_6BA230
        log("select_overloaded_copy_constructor: considering %s",
            ctor.name)                                // via sub_5B72C0

        // Check copy parameter match
        match = determine_copy_param_match(           // sub_6DBAC0
                    ctor, source_operand)

        // determine_copy_param_match calls:
        //   sub_6CE6E0 (type comparison)
        //   sub_6BE5D0 (value category check)
        //   sub_6DB6E0 (deduce_one_parameter for template ctors)

        if (match.viable) {
            if (match better than current_best)
                current_best = ctor

            // Check for ambiguity
            if (match == current_best && ctor != current_best)
                ambiguous = true
        }

    log("Leaving select_overloaded_copy_constructor, cctor_sym = %s",
        current_best.name)
    return current_best
```

The value category check (`sub_6BE5D0`, `copy_function_not_callable_because_of_arg_value_category`, 39 lines) is critical for C++11 move semantics: it rejects copy constructors when the source is an rvalue and a move constructor is available, and vice versa.

### Default Constructor Selection

```c
select_overloaded_default_constructor (sub_6E9080, 358 lines)
    Input:  class_type
    Output: selected constructor symbol

    log("Entering select_overloaded_default_constructor, class_type = %s",
        class_type.name)

    // Collect zero-argument constructors
    // Check for default arguments (a 1-param ctor with default is a default ctor)
    // Run standard overload resolution with empty argument list

    log("Leaving select_overloaded_default_constructor, ctor_sym = %s",
        result.name)
    return result
```

### Assignment Operator Selection

```c
select_overloaded_assignment_operator (sub_6DD600, 492 lines)
    Input:  class_type, rhs_operand
    Output: selected assignment operator symbol

    log("Entering select_overloaded_assignment_operator, class_type = %s",
        class_type.name)

    // Iterate assignment operator candidates
    for each assign_op in class_type.assignment_operators:  // via sub_6BA230
        log("select_overloaded_assignment_operator: considering %s",
            assign_op.name)

        // Check parameter match (similar to copy constructor)
        // ...

    log("Leaving select_overloaded_assignment_operator, assign_sym = %s",
        result.name)
    return result
```

### Copy Elision

C++17 guaranteed copy elision is handled by `handle_elided_copy_constructor_no_guard` (two variants: `sub_6DCD60` 166 lines and `sub_6DD180` 169 lines). Even with elision, the compiler must verify that the copy/move constructor would be callable — the constructor is selected via `select_overloaded_copy_constructor` but never actually invoked. The wrapper `arg_copy_can_be_done_via_constructor` (`sub_6DCC00`, 55 lines) performs this check.

## List Initialization

`prep_list_initializer` (`sub_6D7C80`, 2,119 lines) implements C++11 brace-enclosed initializer list resolution. It is one of the largest functions in overload.c, reflecting the combinatorial complexity of list initialization.

```c
prep_list_initializer (sub_6D7C80, 2119 lines)
    Input:  init_list (braced expression list), target_type, context
    Output: converted initializer expression

    // The algorithm (per [dcl.init.list]):
    //
    // 1. If T has an initializer_list<X> constructor and the braced-init-list
    //    can be converted to initializer_list<X>, use that constructor.
    //
    // 2. If T is an aggregate, perform aggregate initialization.
    //
    // 3. If T has constructors, overload resolution selects a constructor
    //    with the elements of the braced-init-list as arguments.
    //
    // 4. If T is a reference, bind to a temporary or element.
    //
    // At each step, check for narrowing conversions (C++11 requirement).

    // Gate: C++11 required
    if (dword_126EF68 < 201103)   // std_version < C++11
        return LEGACY_PATH

    // Step 1: Check for initializer_list constructor
    init_list_ctor = find_initializer_list_constructor(    // sub_6DFEC0
                         target_type, element_type)
    if (init_list_ctor) {
        init_list_obj = make_initializer_list_object(      // sub_6DFEC0
                            init_list, element_type)
        return set_up_for_constructor_call(init_list_ctor,
                                           init_list_obj)
    }

    // Step 2: Aggregate initialization (recursive for nested braces)
    if (is_aggregate(target_type)) {
        for each element in init_list:
            // Recursively call prep_list_initializer for nested braces
            prep_list_initializer(element, member_type, ...)  // recursive
        return aggregate_init_expr
    }

    // Step 3: Constructor overload resolution
    result = select_overloaded_function(                   // sub_6E6400
                 target_type.constructors, init_list.elements, LIST_INIT)

    // Step 4: Check for narrowing
    check_narrowing_conversions(init_list, result)

    return result
```

The `find_initializer_list_constructor` / `make_initializer_list_object` function (`sub_6DFEC0`, 692 lines) handles `std::initializer_list<T>` construction. It iterates constructors to find one taking `initializer_list<T>` and sets up the backing array via `set_overload_set_traversal_symbol`.

## Class Template Argument Deduction (CTAD)

C++17 CTAD is implemented by `deduce_class_template_args` (`sub_6E8300`, 285 lines). CTAD works by synthesizing a set of "deduction guides" — function-like entities derived from the class template's constructors — and running overload resolution on them.

```c
deduce_class_template_args (sub_6E8300, 285 lines)
    Input:  class_template, constructor_arguments, context
    Output: deduced template arguments

    // Step 1: Generate implicit deduction guides from constructors
    //   For each constructor C(P1, P2, ...) of class template T<A, B, ...>:
    //     Create guide: T(P1, P2, ...) -> T<deduced-A, deduced-B, ...>

    // Step 2: Add explicit deduction guides (user-provided)

    // Step 3: Run overload resolution among all guides
    selected_guide = select_overloaded_function(          // sub_6E6400
                         deduction_guides, constructor_args, CTAD_CONTEXT)

    // Step 4: Extract deduced template arguments from selected guide
    return selected_guide.deduced_args
```

CTAD delegates entirely to `select_overloaded_function` for the actual resolution — the deduction guides are treated as ordinary function candidates with synthesized parameter types.

## Auto Type Deduction

`deduce_auto_type` (`sub_6DB010`, 314 lines) implements C++11 `auto` type deduction, which is structurally similar to template argument deduction. It handles the special case of `auto x = {1, 2, 3}` where the deduced type is `std::initializer_list<int>`.

## Conversion Infrastructure

### Reference Binding

`prep_reference_initializer_operand` (`sub_6D47B0`, 1,121 lines) handles reference initialization, which has its own overload-resolution sub-algorithm for selecting the correct binding path:

1. **Direct binding.** If the initializer is an lvalue of the right type (or derived), bind directly.
2. **Conversion-through-temporary.** If a user-defined conversion exists, create a temporary and bind the reference to it.
3. **Direct reference binding check.** `conversion_for_direct_reference_binding_possible` (`sub_6D4610`, 49 lines) checks whether direct binding is possible.

### Operand Conversion

After overload resolution selects a function, the arguments must be physically converted to match the parameter types:

| Function | Lines | Role |
|---|---|---|
| `sub_6D6650` (`user_convert_operand`) | 427 | Applies user-defined conversion (constructor call or conversion function call) |
| `sub_6E1430` (`convert_operand_into_temp`) | 418 | Creates a temporary and converts operand into it |
| `sub_6E1C40` (`prep_argument` variant 1) | 69 | Prepares argument for function call |
| `sub_6E1E40` (`prep_argument` variant 2) | 69 | Simplified argument preparation |
| `sub_6EB1C0` (`adjust_overloaded_function_call_arguments`) | 249 | Post-resolution argument adjustment |
| `sub_6E0E50` (`adjust_operand_for_builtin_operator`) | 199 | Adjusts operands for built-in operator semantics |

The high-level call setup function `select_and_prepare_to_call_overloaded_function` (`sub_6EB550`, 392 lines) combines overload resolution with argument preparation in a single entry point.

### Dynamic Initialization

`determine_dynamic_init_for_class_init` (`sub_6DEBC0`, 679 lines) determines whether a class object initialization requires a runtime (dynamic) initialization routine rather than static initialization. It checks whether the constructor is trivial, whether the initializer is a constant expression, and whether the target requires dynamic dispatch.

### Conditional Operator

`conditional_operator_conversion_possible` (`sub_6EBFC0`, 326 lines) handles the special overload resolution for the ternary conditional operator (`? :`), which has unique type-determination rules involving common type computation between the second and third operands.

## Ambiguity Diagnostics

When overload resolution fails due to ambiguity, dedicated diagnostic functions produce the error messages:

| Function | Lines | Role |
|---|---|---|
| `sub_6D7040` (`diagnose_overload_ambiguity` standalone) | 191 | Formats and emits ambiguity diagnostic with candidate list |
| `sub_6D35E0` (`user_defined_conversion_possible` with diagnosis) | 399 | Handles ambiguity in user-defined conversion resolution |

The diagnostic output uses `sub_4F59D0`, `sub_4F5C10`, `sub_4F5CF0`, and `sub_4F5D50` for type-to-string formatting, producing messages in the format:

```text
ambiguous overload for 'operator+(A, B)':
  candidate: operator+(int, int)
  candidate: operator+(A::operator int(), int)
```

## Missing Sentinel Warning

`warn_if_missing_sentinel` (`sub_6E9C60`, 1,170 lines) is a large function that checks for missing sentinel arguments (NULL terminators) in variadic function calls. It references multiple CUDA extension flags (`byte_126E349`, `byte_126E358`, `byte_126E3C0`, `byte_126E3C1`, `byte_126E481`) because CUDA functions have different variadic conventions.

## CUDA Execution Space Interaction

CUDA introduces an additional dimension to overload resolution: **execution space compatibility**. In standard C++, any visible function is a candidate. In CUDA, a candidate from the wrong execution space may be excluded or penalized.

### How Execution Spaces Affect Candidates

The CUDA execution space interaction with overload resolution happens at two levels:

**Level 1: Post-resolution validation (expr.c).** After overload resolution selects the best viable function, `check_cross_execution_space_call` (`sub_505720`, 4KB) validates that the selected function is callable from the current execution context. If the call is illegal (e.g., calling a `__device__`-only function from `__host__` code), error 3462--3465 is emitted. This check runs AFTER overload resolution, not during candidate filtering.

**Level 2: Overload-internal CUDA awareness (overload.c).** Within overload.c itself, the CUDA extensions flag `byte_126E349` gates CUDA-specific behavior in several functions:

- `try_conversion_function_match_full` (`sub_6D0F50`): Checks `byte_126E349` when evaluating whether a conversion function is viable. In CUDA mode, conversion functions from the wrong execution space may be excluded from consideration during the user-defined conversion search.

- `warn_if_missing_sentinel` (`sub_6E9C60`): Uses `byte_126E349` and `byte_126E358` to adjust sentinel checking behavior for CUDA-annotated variadic functions.

The key architectural decision is that CUDA does NOT filter candidates during Phase 1 (candidate collection) or Phase 2 (viability checking) of overload resolution proper. Instead, execution-space validation is a separate pass that runs after the standard C++ overload algorithm completes. This preserves EDG's clean separation between the standard-conforming overload engine and NVIDIA's CUDA extensions.

### Cross-Space Validation

The execution space is encoded in the entity node at offset `+182` as a bitfield:

| Bit Pattern | Meaning |
|---|---|
| `(byte & 0x30) == 0x20` | `__device__` only |
| `(byte & 0x60) == 0x20` | `__host__` only |
| `(byte & 0x60) == 0x40` | `__global__` |
| `(byte & 0x30) == 0x30` | `__host__ __device__` |

The cross-space checker (`sub_505720`) compares the caller's execution space with the callee's and emits:

| Error | Condition |
|---|---|
| 3462 | `__device__` called from `__host__` |
| 3463 | Variant of 3462 for HD context |
| 3464 | `__host__` called from `__device__` |
| 3465 | Variant of 3464 with `__device__` note |
| 3508 | `__global__` called from wrong context |

A template-instantiation variant (`sub_505B40`, `check_cross_space_call_in_template`) performs the same checks during template instantiation.

## Debug Tracing

Overload resolution includes extensive debug tracing controlled by `dword_126EFC8`. When enabled, functions emit trace output via `sub_48AFD0` / `sub_48AE00` to the stream at `qword_106B988`:

```text
Entering select_overloaded_function with ...
  try_overloaded_function_match: considering foo(int)
    determine_function_viability: arg 0
    (pass 2)
  try_overloaded_function_match: considering foo(double)
    determine_function_viability: arg 0
    (pass 2)
  comparing candidates: foo(int) vs foo(double)
Leaving select_overloaded_function: foo(int)
```

The trace format `[%lu.%d]` is used in conversion function matching to identify candidates by internal ID.

## Overload Set Management

Overload sets are managed via two key functions in the memory management subsystem:

| Function | Role |
|---|---|
| `sub_6BA0D0` | Allocate a new overload set entry |
| `sub_6BA230` | Iterate/traverse an overload set (linked list walk) |
| `sub_6EC650` | Overload set traversal utility (212 lines) |
| `sub_6ECA20` | Overload set construction from multiple sources (137 lines) |
| `sub_6ECCE0` | Overload set initialization wrapper (23 lines) |

The linked-list representation means candidate iteration is O(n) per traversal, but overload sets are typically small (< 100 candidates), so this is not a performance concern.

## Complete Function Map

| Address | Size (lines) | Identity | Confidence |
|---|---|---|---|
| `0x6BE4A0` | 67 | `name_for_type_code` | VERY HIGH |
| `0x6BE5D0` | 39 | `copy_function_not_callable_because_of_arg_value_category` | VERY HIGH |
| `0x6BE6C0` | 127 | `compare_qualification_conversions` | HIGH |
| `0x6BE990` | 68 | `set_arg_summary_for_user_conversion` | VERY HIGH |
| `0x6BEAF0` | 30 | `set_explicit_flag_on_param_list` | HIGH |
| `0x6BEB60` | 69 | `find_conversion_function` | VERY HIGH |
| `0x6BECA0` | 70 | `matches_type_code` | VERY HIGH |
| `0x6BEE10` | 375 | `standard_conversion_sequence` | HIGH |
| `0x6BF610` | 80 | `check_user_defined_conversion` | HIGH |
| `0x6BF710` | 163 | `evaluate_conversion_for_argument` | HIGH |
| `0x6BFA50` | 129 | `process_builtin_operator_candidate` | HIGH |
| `0x6BFD00` | 67 | `name_for_overloaded_operator` | HIGH |
| `0x6BFE40` | 48 | `check_ambiguous_conversion` | HIGH |
| `0x6BFF70` | 100 | `compare_conversion_sequences` | HIGH |
| `0x6C4C00` | 1,044 | candidate evaluation | HIGH |
| `0x6C5C90` | 386 | candidate scoring/ranking | MEDIUM |
| `0x6C8B70` | 418 | argument conversion computation | MEDIUM |
| `0x6C92B0` | 383 | template argument deduction for overloads | MEDIUM |
| `0x6CBC40` | 345 | implicit conversion sequence comparison | MEDIUM |
| `0x6CD010` | 752 | built-in operator candidate generation | HIGH |
| `0x6CE010` | 226 | operator overload candidate setup | MEDIUM |
| `0x6CE6E0` | 1,246 | overload resolution driver ("THE MONSTER") | HIGH |
| `0x6D03D0` | 170 | `determine_selector_match_level` (6-param) | HIGH |
| `0x6D0790` | 132 | `determine_selector_match_level` (4-param) | HIGH |
| `0x6D0A80` | 225 | `selector_match_with_this_param` | HIGH |
| `0x6D0F50` | 1,085 | `try_conversion_function_match_full` | HIGH |
| `0x6D28C0` | 252 | `conversion_from_class_possible` (9-param) | HIGH |
| `0x6D2ED0` | 293 | `conversion_from_class_possible` (10-param) | HIGH |
| `0x6D35E0` | 399 | `user_defined_conversion_possible` / `diagnose_overload_ambiguity` | HIGH |
| `0x6D3DC0` | 360 | `conversion_possible` | HIGH |
| `0x6D4610` | 49 | `conversion_for_direct_reference_binding_possible` | HIGH |
| `0x6D47B0` | 1,121 | `prep_reference_initializer_operand` | HIGH |
| `0x6D61F0` | 176 | reference init helper | MEDIUM |
| `0x6D6650` | 427 | `user_convert_operand` / `set_up_for_conversion_function_call` | HIGH |
| `0x6D7040` | 191 | `diagnose_overload_ambiguity` (standalone) | HIGH |
| `0x6D7410` | 239 | `prep_conversion_operand` | HIGH |
| `0x6D79E0` | 93 | conversion operand wrapper | MEDIUM |
| `0x6D7C80` | 2,119 | `prep_list_initializer` | HIGH |
| `0x6DACA0` | 154 | list init parameter deduction helper | MEDIUM |
| `0x6DB010` | 314 | `deduce_auto_type` | HIGH |
| `0x6DB6E0` | 236 | `deduce_one_parameter` | HIGH |
| `0x6DBAC0` | 175 | `determine_copy_param_match` | HIGH |
| `0x6DBEA0` | 625 | `select_overloaded_copy_constructor` | HIGH |
| `0x6DCC00` | 55 | `arg_copy_can_be_done_via_constructor` | HIGH |
| `0x6DCD60` | 166 | `handle_elided_copy_constructor_no_guard` (variant 1) | HIGH |
| `0x6DD180` | 169 | `handle_elided_copy_constructor_no_guard` (variant 2) | HIGH |
| `0x6DD600` | 492 | `select_overloaded_assignment_operator` | HIGH |
| `0x6DE110` | 31 | `actualize_class_object_from_braced_init_list_for_bitwise_copy` | HIGH |
| `0x6DE1D0` | 75 | `full_adjust_class_object_type` | HIGH |
| `0x6DE320` | 111 | `set_up_for_constructor_call` | HIGH |
| `0x6DE5A0` | 174 | `temp_init_from_operand_full` | HIGH |
| `0x6DE9E0` | 7 | `temp_init_from_operand` (wrapper) | HIGH |
| `0x6DE9F0` | 114 | `find_top_temporary` | HIGH |
| `0x6DEBC0` | 679 | `determine_dynamic_init_for_class_init` | HIGH |
| `0x6DF8C0` | 107 | conversion with dynamic init wrapper | MEDIUM |
| `0x6DFBF0` | 92 | convert and determine dynamic init helper | MEDIUM |
| `0x6DFEC0` | 692 | `make_initializer_list_object` / `find_initializer_list_constructor` | HIGH |
| `0x6E0E50` | 199 | `adjust_operand_for_builtin_operator` | HIGH |
| `0x6E1250` | 79 | argument preparation helper | MEDIUM |
| `0x6E1430` | 418 | `convert_operand_into_temp` | HIGH |
| `0x6E1C40` | 69 | `prep_argument` (5-param) | HIGH |
| `0x6E1E40` | 69 | `prep_argument` (4-param) | HIGH |
| `0x6E2040` | 2,120 | `determine_function_viability` | HIGH |
| `0x6E4FA0` | 633 | `try_overloaded_function_match` (variant 1) | HIGH |
| `0x6E5B20` | 367 | `try_overloaded_function_match` (variant 2) | HIGH |
| `0x6E61D0` | 121 | overload match wrapper | MEDIUM |
| `0x6E6400` | 1,483 | `select_overloaded_function` (20 params) | HIGH |
| `0x6E8300` | 285 | `deduce_class_template_args` (CTAD) | HIGH |
| `0x6E8890` | 199 | type comparison for overload | MEDIUM |
| `0x6E8E20` | 93 | overload candidate evaluation helper | MEDIUM |
| `0x6E9080` | 358 | `select_overloaded_default_constructor` | HIGH |
| `0x6E9750` | 281 | argument list builder | MEDIUM |
| `0x6E9C60` | 1,170 | `warn_if_missing_sentinel` | HIGH |
| `0x6EAF90` | 105 | `node_for_arg_of_overloaded_function_call` | HIGH |
| `0x6EB1C0` | 249 | `adjust_overloaded_function_call_arguments` | HIGH |
| `0x6EB550` | 392 | `select_and_prepare_to_call_overloaded_function` | HIGH |
| `0x6EBFC0` | 326 | `conditional_operator_conversion_possible` | HIGH |
| `0x6EC650` | 212 | overload set iterator | MEDIUM |
| `0x6ECA20` | 137 | overload set builder | MEDIUM |
| `0x6ECCE0` | 23 | overload set init wrapper | LOW |
| `0x6ECD70` | 160 | util.h insert operation | MEDIUM |
| `0x6ECFB0` | 193 | util.h insert variant | MEDIUM |
| `0x6ED2A0` | 812 | `try_builtin_operands_match` | HIGH |
| `0x6EE340` | 1,058 | `try_conversions_for_builtin_operator` | HIGH |
| `0x6EF7A0` | 2,174 | `select_overloaded_operator` / `check_for_operator_overloading` | HIGH |

## Key Globals

| Global | Usage |
|---|---|
| `dword_126EFB4` | Language mode (2 = C++) |
| `dword_126EF68` | Language standard version (201103/201703/202301) |
| `dword_126EFA4` | GNU extensions enabled |
| `dword_126EFAC` | Extended mode flag |
| `dword_126EFC8` | Debug trace enabled (controls overload trace output) |
| `dword_126EFCC` | Debug output level |
| `qword_106B988` | Overload debug output stream |
| `qword_106B990` | Overload debug output stream (alternate) |
| `qword_12C6B30` | Overload candidate list |
| `byte_126E349` | CUDA extensions flag |
| `byte_126E358` | Extension flag (likely `__CUDA_ARCH__`-related) |
| `dword_106BEA8` | Overload configuration flag |
| `dword_106BEC0` | Overload configuration flag |
| `dword_106C2A8` | Used by selector match level |
| `dword_106C2B8` | Operator-related flag |
| `dword_106C2BC` | Operator mode flag |
| `dword_106C104` | Operator configuration |
| `dword_106C124` | Operator configuration |
| `dword_106C140` | Operator configuration |
| `dword_106C16C` | Operator configuration |
| `dword_126C5C4` | Template nesting depth |
| `dword_126C5E4` | Scope stack depth |
| `qword_126C5E8` | Scope stack base |
