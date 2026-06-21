# Expression Parser

The expression parser is the largest subsystem in cudafe++. This EDG 6.6 cluster occupies approximately 335KB of code (address range `0x4F8000`--`0x556600`) containing roughly 320 functions. The central function `scan_expr_full` (`sub_511D40`) alone occupies 80KB — approximately 2,000 decompiled lines with over 300 local variables. EDG uses a hand-written recursive descent parser, not a generated one (no yacc/bison). Each C++ operator precedence level has its own scanning function, and the call chain follows the precedence hierarchy: assignment, conditional, logical-or, logical-and, bitwise-or, bitwise-xor, bitwise-and, equality, relational, shift, additive, multiplicative, pointer-to-member, unary, postfix, primary.

CUDA-specific extensions are woven directly into this subsystem: cross-execution-space call validation at every function call site, remapping of GCC `__sync_fetch_and_*` builtins to NVIDIA `__nv_atomic_fetch_*` intrinsics, and constexpr-if gating of literal evaluation based on compilation mode.

## Key Facts

| Property | Value |
|---|---|
| Subsystem | Expression parsing (~320 functions) + expression utilities (~90 functions), EDG 6.6 |
| Address range | `0x4F8000`--`0x556600` (parsing), `0x558720`--`0x55FE10` (utilities) |
| Total code size | ~385KB |
| Central dispatcher | `sub_511D40` (`scan_expr_full`, 80KB, ~2,000 lines, 300+ locals) |
| Ternary handler | `sub_526E30` (`scan_conditional_operator`, 48KB) |
| Function call handler | `sub_545F00` (`scan_function_call`, 2,490 lines) |
| New-expression handler | `sub_54AED0` (`scan_new_operator`, 2,333 lines) |
| Identifier handler | `sub_5512B0` (`scan_identifier`, 1,406 lines) |
| Template rescan | `sub_5565E0` (`rescan_expr_with_substitution_internal`, 1,558 lines) |
| Atomic builtin remapper | `sub_537BF0` (`adjust_sync_atomic_builtin`, 1,108 lines, NVIDIA-specific) |
| Cross-space validation | `sub_505720` (`check_cross_execution_space_call`, 4KB) |
| Current token global | `word_126DD58` (16-bit token kind) |
| Expression context | `qword_106B970` (current scope/context pointer) |
| Trace flag | `dword_126EFC8` (debug trace), `dword_126EFCC` (verbosity level) |

## Architecture

### Recursive Descent, No Generator

EDG's expression parser is entirely hand-written C. There are no parser tables, no DFA state machines, and no grammar transformation output. Each operator precedence level maps to one or more `scan_*` functions that call down the precedence chain via direct function calls. The parser is effectively a family of mutually recursive functions whose call graph encodes the C++ grammar.

The top-level entry point is `scan_expr_full`, which serves a dual role: (1) it contains the primary-expression scanner as a massive switch on token kind, and (2) after scanning a primary expression, it enters a post-scan binary-operator dispatch loop that routes to the correct precedence-level handler based on the next operator token.

```text
scan_expr_full (sub_511D40)
  │
  ├─ [token switch] ─────────► Primary expressions
  │     case 1   → scan_identifier (sub_5512B0)
  │     case 2,3 → scan_numeric_literal (sub_5632C0)
  │     case 27  → scan_cast_or_expr (sub_544290)
  │     case 161 → scan_new_operator (sub_54AED0)
  │     case 162 → scan_throw_operator (sub_5211B0)
  │     ... (100+ token cases)
  │
  ├─ [postfix loop] ──────────► Postfix operators
  │     ()   → scan_function_call (sub_545F00)
  │     []   → scan_subscript_operator (sub_540560)
  │     .->  → scan_field_selection_operator (sub_5303E0)
  │     ++-- → scan_postfix_incr_decr (sub_510D70)
  │
  └─ [binary dispatch] ───────► Binary operators by precedence
        prec 64 → scan_simple_assignment_operator (sub_53FD70)
                  scan_compound_assignment_operator (sub_536E80)
        prec 60 → scan_conditional_operator (sub_526E30)
        prec 59 → scan_logical_operator (sub_526040)     [||]
        prec 58 → scan_logical_operator (sub_526040)     [&&]
        prec 57 → scan_comma_operator (sub_529720)
        ...     → scan_bit_operator (sub_525BC0)         [| ^ &]
        ...     → scan_eq_operator (sub_524ED0)          [== !=]
        ...     → scan_add_operator (sub_523EB0)         [+ -]
        ...     → scan_mult_operator (sub_5238C0)        [* / %]
        ...     → scan_shift_operator (sub_524960)       [<< >>]
        ...     → scan_ptr_to_member_operator (sub_522650) [.* ->*]
```

### Precedence Levels

The parser assigns numeric precedence levels internally, passed as the `a3` (third) parameter to `scan_expr_full`. The precedence integer increases with binding strength (higher values = tighter binding):

| Level | Operators | Handler |
|---|---|---|
| 57 | `,` (comma) | `scan_comma_operator` |
| 58 | `\|\|` | `scan_logical_operator` |
| 59 | `&&` | `scan_logical_operator` |
| 60 | `? :` (conditional) | `scan_conditional_operator` |
| 61 | `\|` | `scan_bit_operator` |
| 62 | `^` | `scan_bit_operator` |
| 63 | `&` | `scan_bit_operator` |
| 64 | `=` `+=` `-=` ... | `scan_simple_assignment_operator` / `scan_compound_assignment_operator` |

When `scan_expr_full` encounters a binary operator token whose precedence is lower than the current precedence parameter, it returns immediately, allowing the caller at that precedence level to consume the operator. This is the standard recursive descent technique: each level calls the next-higher-precedence scanner for its operands.

## scan_expr_full — The Central Dispatcher

`scan_expr_full` (`sub_511D40`, 80KB) is the largest function in the entire cudafe++ binary. Its structure follows this pattern:

```c
function scan_expr_full(result, scan_info, precedence, flags, ...) {
    // 1. Trace entry
    if (debug_trace_flag)
        trace_enter(4, "scan_expr_full")
    if (debug_verbosity > 3)
        fprintf(trace_stream, "precedence level = %d\n", precedence)

    // 2. Extract context flags from current scope
    context = current_scope          // qword_106B970
    in_cuda_extension = (context[20] & 0x08) != 0
    in_pack_expansion = context[21] & 0x01
    saved_pending_expr = pending_expression   // qword_106B968
    pending_expression = 0

    // 3. Handle template rescan context
    if (in_template_context) {
        if (context.flags == TEMPLATE_ONLY_DEPENDENT)
            init_expr_stack_entry(...)
            // Mark as template-argument context
    }

    // 4. Handle forced-parenthesized-expression flag
    if (flags & 0x08)
        goto scan_cast_or_expr       // sub_544290

    // 5. Check for decltype token (185)
    if (current_token == 185 && dialect == C++)
        call sub_6810F0(...)         // re-classify through lexer

    // 6. MASTER TOKEN SWITCH -- dispatch on word_126DD58
    switch (current_token) {
        case 1:   // identifier
            // Special-case: check if identifier is a hidden type trait
            if (identifier_is("__is_pointer"))  { set_token(320); scan_unary_type_trait(); break; }
            if (identifier_is("__is_invocable")) { set_token(225); scan_call_like_builtin(); break; }
            if (identifier_is("__is_signed"))   { set_token(324); scan_unary_type_trait(); break; }
            // Default: full identifier scan
            scan_identifier(result, flags, precedence, ...)
            break;

        case 2, 3, 123, 124, 125:  // numeric, char, utf literals
            // Context-sensitive literal handling:
            //   - Check constexpr-if context (execution-space dependent)
            //   - Route to appropriate literal scanner
            if (is_constexpr_if_context)
                value = compute_constexpr_literal()
                scan_constexpr_literal_result(value, result)
            else
                scan_numeric_literal(literal_data, result)  // sub_5632C0
            break;

        case 4, 5, 6, 181, 182:  // string literals
            scan_string_literal(literal_data, result)       // sub_5632C0
            // Vector deprecation check for CUDA
            if ((cuda_mode || cuda_device_mode) && has_vector_literal_flag)
                result.flags |= VECTOR_DEPRECATED
            break;

        case 7:   // postfix-string-context (interpolated strings)
            check_postfix_string_context(...)
            scan_string_expression(literal_data, result)    // sub_563580
            break;

        case 27:  // left-paren '('
            scan_cast_or_expr(result, scratch, flags)       // sub_544290
            // Disambiguates: C-cast, grouped expr, GNU statement expr, fold expr
            break;

        case 31, 32:  // prefix ++ / --
            scan_prefix_incr_decr(result, ...)              // sub_516080
            break;

        case 33:  // & (address-of)
            scan_ampersand_operator(result, ...)            // sub_516720
            break;

        case 34:  // * (indirection)
            scan_indirection_operator(result, ...)          // sub_517270
            break;

        case 35, 36, 37, 38:  // unary + - ~ !
            scan_arith_prefix_operator(result, ...)         // sub_517680
            break;

        case 77:  // lambda expression '['
            scan_lambda_expression(result, ...)             // sub_5BBA60
            break;

        case 99, 284:  // sizeof
            scan_sizeof_operator(result, ...)               // sub_517BD0
            break;

        case 109: // _Generic
            scan_type_generic_operator(result, ...)         // inlined
            break;

        case 152: // requires
            scan_requires_expression(result, ...)           // sub_52CFF0
            break;

        case 155: // new (in C++ concept context path)
            scan_new_operator(result, ...)                  // sub_54AED0
            break;

        case 161: // new-expression
            scan_class_new_expression(result, ...)          // sub_6C9940/sub_6C9C50
            break;

        case 162: // throw
            scan_throw_operator(result, ...)                // sub_5211B0
            break;

        case 166: // const_cast
            scan_const_cast_operator(result, ...)           // sub_520280
            break;

        case 167: // static_cast
            scan_static_cast_operator(result, ...)          // sub_51F670
            break;

        case 176: // reinterpret_cast
            scan_reinterpret_cast_operator(result, ...)     // sub_5209A0
            break;

        case 177: // dynamic_cast
            scan_named_cast_operator(result, ...)           // sub_53D590
            break;

        case 178: // typeid
            scan_typeid_operator(result, ...)               // sub_535370
            break;

        case 185: // decltype
            scan_decltype_operator(result, ...)             // sub_52A3B0
            break;

        case 195 ... 356:  // type traits (__is_class, __is_enum, etc.)
            scan_unary_type_trait_helper(result, ...)       // sub_51A690
            // or
            scan_binary_type_trait_helper(result, ...)      // sub_51B650
            break;

        case 243: // noexcept
            scan_noexcept_operator(result, ...)             // sub_51D910
            break;

        case 267: // co_yield
            // Coroutine yield expression handling
            scan_braced_init_list_full(result, ...)         // sub_5360D0
            add_await_to_operand(result, ...)               // sub_50B630
            break;

        case 269: // co_await
            // Recursive scan of operand, then wrap with await semantics
            scan_expr_full(result, info, precedence, flags | AWAIT)
            add_await_to_operand(result, ...)               // sub_50B630
            break;

        case 297: // __builtin_bit_cast
            scan_builtin_bit_cast(result, ...)              // sub_51CC60
            break;

        // ... approximately 100 additional cases
    }

    // 7. POST-SCAN BINARY OPERATOR DISPATCH LOOP
    //    After scanning a primary/prefix expression, check for binary operators
    while (true) {
        op = current_token
        op_prec = get_binary_op_precedence(op)
        if (op_prec < precedence)
            break    // operator binds less tightly than our level

        switch (op) {
            case '?':  scan_conditional_operator(result, info, flags)   // sub_526E30
            case '=':  scan_simple_assignment_operator(result, ...)     // sub_53FD70
            case '+=': scan_compound_assignment_operator(result, ...)   // sub_536E80
            case '||': scan_logical_operator(result, info, ...)        // sub_526040
            case '&&': scan_logical_operator(result, info, ...)        // sub_526040
            case '|':  scan_bit_operator(result, ...)                  // sub_525BC0
            case '^':  scan_bit_operator(result, ...)
            case '&':  scan_bit_operator(result, ...)
            case '==': scan_eq_operator(result, ...)                   // sub_524ED0
            case '!=': scan_eq_operator(result, ...)
            case '<':  scan_rel_operator(result, ...)                  // sub_543A90
            case '+':  scan_add_operator(result, ...)                  // sub_523EB0
            case '-':  scan_add_operator(result, ...)
            case '*':  scan_mult_operator(result, ...)                 // sub_5238C0
            case '/':  scan_mult_operator(result, ...)
            case '%':  scan_mult_operator(result, ...)
            case '<<': scan_shift_operator(result, ...)                // sub_524960
            case '>>': scan_shift_operator(result, ...)
            case '.*': scan_ptr_to_member_operator(result, ...)        // sub_522650
            case '->*': scan_ptr_to_member_operator(result, ...)
            case ',':  scan_comma_operator(result, ...)                // sub_529720
            // Postfix operators (not precedence-gated):
            case '(':  scan_function_call(result, ...)                 // sub_545F00
            case '[':  scan_subscript_operator(result, ...)            // sub_540560
            case '.':  scan_field_selection_operator(result, ...)      // sub_5303E0
            case '->': scan_field_selection_operator(result, ...)
            case '++': scan_postfix_incr_decr(result, ...)             // sub_510D70
            case '--': scan_postfix_incr_decr(result, ...)
        }
    }

    // 8. Restore saved state and return
    pending_expression = saved_pending_expr
    if (debug_trace_flag)
        trace_exit(...)
    return result
}
```

### Token Dispatch Map (Complete)

The master switch in `scan_expr_full` covers approximately 120 distinct token cases. The full dispatch table:

| Token Code(s) | Expression Form | Handler |
|---|---|---|
| 1 | Identifier (with `__is_pointer`/`__is_signed` detection) | `scan_identifier` (`sub_5512B0`) |
| 2, 3 | Integer / floating-point literal | `scan_numeric_literal` (`sub_5632C0`) |
| 4, 5, 6, 181, 182 | String literal (narrow, wide, UTF-8/16/32) | `scan_string_literal` (`sub_5632C0`) |
| 7 | Postfix string context | `sub_563580` |
| 8 | Literal operator call | `make_func_operand_for_literal_operator_call` (`sub_4FFFB0`) |
| 18, 80–136, 165, 180, 183 | Type keywords in expression context | `scan_type_returning_type_trait_operator` / `scan_identifier` |
| 25 | `__extension__` | `scan_expr_splicer` (`sub_52FD70`) or `scan_statement_expression` (`sub_4F9F20`) |
| 27 | `(` | `scan_cast_or_expr` (`sub_544290`) — disambiguates cast/group/fold/stmt-expr |
| 31, 32 | `++` / `--` (prefix) | `scan_prefix_incr_decr` (`sub_516080`) |
| 33 | `&` (address-of) | `scan_ampersand_operator` (`sub_516720`) |
| 34 | `*` (indirection) | `scan_indirection_operator` (`sub_517270`) |
| 35–38 | `+` `-` `~` `!` (unary) | `scan_arith_prefix_operator` (`sub_517680`) |
| 50 | `__builtin_expect` | `bound_function_in_cast` (`sub_503F70`) |
| 77 | `[` (lambda) | `scan_lambda_expression` (`sub_5BBA60`) |
| 99, 284 | `sizeof` | `scan_sizeof_operator` (`sub_517BD0`) |
| 109 | `_Generic` | `scan_type_generic_operator` (inlined) |
| 111, 247 | `alignof` / `_Alignof` | `scan_alignof_operator` (`sub_519300`) |
| 112 | `__intaddr` | `scan_intaddr_operator` (`sub_520EE0`) |
| 113 | `va_start` | `scan_va_start_operator` (`sub_51E8A0`) |
| 114 | `va_arg` | `scan_va_arg_operator` (`sub_51DFA0`) |
| 115 | `va_end` | `scan_va_end_operator` (`sub_51E4A0`) |
| 116 | `va_copy` | `scan_va_copy_operator` (`sub_51E670`) |
| 117 | `offsetof` | `scan_offsetof` (`sub_555530`) |
| 123 | `char` literal | `scan_utf_char_literal` (`sub_5659D0`) |
| 124 | `wchar_t` literal | `scan_wchar_literal` (`sub_5658D0`) |
| 125 | UTF character literal | `scan_wide_char_literal` (`sub_565950`) |
| 138–141 | `__FUNCTION__`/`__PRETTY_FUNCTION__`/`__func__` | `setup_function_name_literal` (`sub_50AC80`) |
| 143 | `__builtin_types_compatible_p` | `scan_builtin_operation_args_list` (`sub_534920`) |
| 144, 145 | `__real__` / `__imag__` | `scan_complex_projection` (`sub_51D210`) |
| 146 | `typeid` (execution-space variant) | `scan_typeid_operator` (`sub_535370`) |
| 152 | `requires` (C++20) | `scan_requires_expression` (`sub_52CFF0`) |
| 155 | Concept expression | `scan_new_operator` path (`sub_54AED0`) |
| 161 | `new` | `scan_class_new_expression` (`sub_6C9940`) |
| 162 | `throw` | `scan_throw_operator` (`sub_5211B0`) |
| 166 | `const_cast` | `scan_const_cast_operator` (`sub_520280`) |
| 167 | `static_cast` | `scan_static_cast_operator` (`sub_51F670`) |
| 176 | `reinterpret_cast` | `scan_reinterpret_cast_operator` (`sub_5209A0`) |
| 177 | `dynamic_cast` | `scan_named_cast_operator` (`sub_53D590`) |
| 178 | `typeid` | `scan_typeid_operator` (`sub_535370`) |
| 185 | `decltype` | `scan_decltype_operator` (`sub_52A3B0`) |
| 188 | `wchar_t` literal (alt) | `sub_5BCDE0` |
| 189 | `typeof` | `scan_typeof_operator` (`sub_52B540`) |
| 195–206 | Unary type traits | `scan_unary_type_trait_helper` (`sub_51A690`) |
| 207–292 | Binary type traits | `scan_binary_type_trait_helper` (`sub_51B650`) |
| 225, 226 | `__is_invocable` / `__is_nothrow_invocable` | `dispatch_call_like_builtin` (`sub_535080`) |
| 227–235 | Builtin operations | `sub_535080` / `sub_51BC10` / `sub_51B0C0` |
| 237 | `__builtin_constant_p` | `sub_5BC7E0` |
| 243 | `noexcept` (operator) | `scan_noexcept_operator` (`sub_51D910`) |
| 251–256 | Builtin atomic operations | `check_operand_is_pointer` (`sub_5338B0`/`sub_533B80`) |
| 257, 258 | Fold expression tokens | `scan_builtin_shuffle` (`sub_53E480`) |
| 259 | `__builtin_convertvector` | `scan_builtin_convertvector` (`sub_521950`) |
| 261 | `__builtin_complex` | `scan_builtin_complex` (`sub_521DB0`) |
| 262 | `__builtin_choose_expr` | `scan_c11_generic_selection` (`sub_554400`) |
| 267 | `co_yield` | Braced-init-list + coroutine `add_await_to_operand` (`sub_50B630`) |
| 269 | `co_await` | Recursive `scan_expr_full` + `add_await_to_operand` |
| 270 | `__builtin_launder` | `sub_51B0C0(60, ...)` |
| 271 | `__builtin_addressof` | `scan_builtin_addressof` (`sub_519CF0`) |
| 294 | Pack expansion | `scan_requires_expr` (`sub_542D90`) |
| 296 | `__has_attribute` | `scan_builtin_has_attribute` (`sub_51C780`) |
| 297 | `__builtin_bit_cast` | `scan_builtin_bit_cast` (`sub_51CC60`) |
| 300, 301 | `__is_pointer_interconvertible_with_class` | `sub_51BE60` |
| 302, 303 | `__is_corresponding_member` | `sub_51C270` |
| 304 | `__edg_is_deducible` | `sub_51B360` |
| 306, 307 | `__builtin_source_location` | `sub_5BC720` / `sub_534920` |

## scan_conditional_operator — Ternary `? :`

`scan_conditional_operator` (`sub_526E30`, 48KB) is the second-largest expression-scanning function. The ternary operator is notoriously complex because it must unify the types of two branches that may have completely different types. The function handles:

- **Type unification between branches**: determines the common type of the true and false expressions. This involves the usual arithmetic conversions for numeric types, pointer-to-derived to pointer-to-base conversions, null pointer conversions, and user-defined conversion sequences.
- **Lvalue conditional expressions** (GCC extension): when both branches are lvalues of the same type, the result is itself an lvalue.
- **Void branches**: if one or both branches are void expressions, the result type is void.
- **Throw in branches**: a throw expression in one branch causes the result to take the type of the other branch.
- **Constexpr evaluation**: when the condition is a constant expression, only one branch is semantically evaluated (the other is discarded).
- **Reference binding**: determines whether the result is an lvalue reference, rvalue reference, or prvalue.
- **Overloaded operator?**: resolution of user-defined conditional operators.

```c
function scan_conditional_operator(context, result, flags) {
    // 1. The condition has already been scanned -- it is in 'result'
    //    We are positioned at the '?' token

    // 2. Save expression stack state
    saved_stack = save_expr_stack()

    // 3. Scan true branch (between ? and :)
    //    Note: precedence resets -- assignment expressions allowed here
    init_expr_stack_entry(...)
    scan_expr_full(true_result, info, ASSIGNMENT_PREC, flags)

    // 4. Expect and consume ':'
    expect_token(':')

    // 5. Scan false branch
    scan_expr_full(false_result, info, ASSIGNMENT_PREC, flags)

    // 6. Type unification of true_result and false_result
    true_type  = get_type(true_result)
    false_type = get_type(false_result)

    if (both_void(true_type, false_type))
        result_type = void
    else if (is_throw(true_result))
        result_type = false_type
    else if (is_throw(false_result))
        result_type = true_type
    else if (arithmetic_types(true_type, false_type))
        result_type = usual_arithmetic_conversions(true_type, false_type)
    else if (same_class_lvalues(true_result, false_result))
        result_type = common_lvalue_type(true_type, false_type)  // GCC ext
    else if (pointer_types(true_type, false_type))
        result_type = composite_pointer_type(true_type, false_type)
    else
        // Try user-defined conversions (overload resolution)
        result_type = resolve_via_conversion_sequences(true_type, false_type)

    // 7. Apply cv-qualification merging
    result_type = merge_cv_qualifications(true_type, false_type, result_type)

    // 8. Build result expression node
    build_conditional_expr_node(result, condition, true_result, false_result, result_type)

    // 9. Restore stack
    restore_expr_stack(saved_stack)
}
```

The complexity arises from the 15+ different type-pair combinations (arithmetic-arithmetic, pointer-pointer, pointer-null, class-class with conversions, void-void, throw-anything, lvalue-lvalue GCC extension) that each require different conversion logic.

## scan_function_call — All Call Forms

`scan_function_call` (`sub_545F00`, 2,490 lines) handles every form of function call expression. It is invoked from the postfix operator dispatch in `scan_expr_full` when a `(` follows a primary expression, and also from various specialized paths.

The function handles:

1. **Regular function calls** with overload resolution
2. **Builtin function calls** — GCC/Clang `__builtin_*` with special semantics
3. **Pseudo-calls to builtins** — `va_start`, `__builtin_va_start`, etc.
4. **GNU `__builtin_classify_type`** — compile-time type classification
5. **SFINAE context** — failed overload resolution suppresses errors instead of aborting
6. **Template argument deduction** for function templates at call sites
7. **CUDA atomic builtin remapping** — delegates to `adjust_sync_atomic_builtin` (see below)

```c
function scan_function_call(callee_operand, flags, context, ...) {
    // 1. Classify the callee
    operand_kind = get_operand_kind(callee_operand)
    assert(operand_kind is valid)  // "scan_function_call: bad operand kind"

    // 2. Scan argument list
    scan_call_arguments(arg_list, ...)   // sub_545760

    // 3. Branch on callee kind
    if (is_builtin_function(callee_operand)) {
        // Check if this is a special builtin
        if (is_sync_atomic_builtin(callee_operand)) {
            // CUDA-specific: remap __sync_fetch_and_* → __nv_atomic_fetch_*
            result = adjust_sync_atomic_builtin(callee, args, ...)  // sub_537BF0
            return result
        }

        // check_builtin_function_for_call: validate args for builtins
        check_builtin_function_for_call(callee, arg_list, ...)

        // scan_builtin_pseudo_call: for builtins with special evaluation
        if (is_pseudo_call_builtin(callee))
            return scan_builtin_pseudo_call(callee, arg_list, ...)
    }

    // 4. Overload resolution
    if (has_overload_candidates(callee_operand)) {
        best = perform_overload_resolution(callee, arg_list, ...)
        if (best == AMBIGUOUS)
            emit_error(...)
        if (best == NO_MATCH && in_sfinae_context)
            return SFINAE_FAILURE
        callee = best.function
    }

    // 5. Template argument deduction (if callee is a function template)
    if (is_function_template(callee)) {
        deduced = deduce_template_args(callee, arg_list, ...)
        if (deduction_failed && in_sfinae_context)
            return SFINAE_FAILURE
        callee = instantiate_template(callee, deduced)
    }

    // 6. CUDA cross-execution-space check
    if (cuda_mode)
        check_cross_execution_space_call(callee, ...)  // sub_505720

    // 7. Apply implicit conversions to arguments
    for each (arg, param) in zip(arg_list, callee.params):
        convert_arg_to_param_type(arg, param)

    // 8. Build call expression node
    build_call_expression(result, callee, arg_list, return_type)
}
```

### scan_call_arguments (`sub_545760`, 332 lines)

The argument scanner called from `scan_function_call`:

```c
function scan_call_arguments(arg_list_out, ...) {
    // assert "scan_call_arguments"
    // Loop: scan comma-separated expressions until ')'
    while (current_token != ')') {
        scan_expr_full(arg, info, ASSIGNMENT_PREC, flags)
        append(arg_list_out, arg)
        if (current_token == ',')
            consume(',')
        else
            break
    }
    // Handle default arguments for missing trailing params
    // Handle parameter pack expansion
}
```

## scan_new_operator — All `new` Forms

`scan_new_operator` (`sub_54AED0`, 2,333 lines) implements the complete C++ `new` expression. The function name strings embedded in the binary confirm the following sub-operations:

| Sub-operation | Embedded Assert String |
|---|---|
| Entry point | `"scan_new_operator"` |
| Rescan in template | `"rescan_new_operator_expr"` |
| Token validation | `"scan_new_operator: expected new or gcnew"` |
| Token extraction | `"get_new_operator_token"` |
| Type parsing | `"scan_new_type"` |
| Paren-as-braced fallback | `"scan_paren_expr_list_as_braced_list"` |
| Array size deduction | `"deduce_new_array_size"` |
| Deallocation lookup | `"determine_deletion_for_new"` |
| Paren initializer | `"prep_new_object_init_paren_initializer"` |
| Brace initializer | `"prep_new_object_init_braced_initializer"` |
| No initializer | `"prep_new_object_init_no_initializer"` |
| Non-POD error | `"scan_new_operator: non-POD class has neither actual nor assumed ctor"` |

The function processes all forms:

```c
function scan_new_operator(result, flags, context, ...) {
    // Determine scope: ::new (global) vs. new (class-scope)
    is_global = check_and_consume("::")

    // Parse optional placement arguments: new(placement_args)
    if (current_token == '(')
        placement_args = scan_expression_list(...)

    // Parse the allocated type: new Type
    type = scan_new_type(...)

    // Parse optional array dimension: new Type[size]
    if (current_token == '[') {
        array_size = scan_expression(...)
        if (can_deduce_size)
            deduce_new_array_size(type, initializer)
    }

    // Parse optional initializer
    if (current_token == '(')
        init = prep_new_object_init_paren_initializer(type, ...)
    else if (current_token == '{')
        init = prep_new_object_init_braced_initializer(type, ...)
    else
        init = prep_new_object_init_no_initializer(type, ...)

    // Look up matching operator new
    new_fn = lookup_operator_new(type, placement_args, is_global, ...)

    // Look up matching operator delete (for exception cleanup)
    determine_deletion_for_new(new_fn, type, placement_args, ...)

    // For template-dependent types, defer to rescan at instantiation
    if (is_dependent_type(type))
        record_for_rescan(...)

    // Build new-expression node
    build_new_expr(result, new_fn, type, init, placement_args, array_size)
}
```

## scan_identifier — Name Resolution in Expression Context

`scan_identifier` (`sub_5512B0`, 1,406 lines) handles the case where the current token is an identifier in expression context. This is far more complex than a simple name lookup because identifiers in C++ can resolve to variables, functions, enumerators, type names (triggering functional-notation casts), anonymous union members, or preprocessing constants.

The function contains assert strings revealing its sub-operations:

| Assert String | Purpose |
|---|---|
| `"scan_identifier"` | Entry point |
| `"scan_identifier: in preprocessing expr"` | Identifier in `#if` context evaluates to 0 or 1 |
| `"anonymous_parent_variable_of"` | Navigate to parent variable of anonymous union member |
| `"anonymous_parent_variable_of: bad symbol kind on list"` | Error path for malformed anonymous union chain |
| `"make_anonymous_union_field_operand"` | Construct operand for anonymous union member access |
| `"get_with_hash"` | Hash-based lookup for cached resolution results |

```c
function scan_identifier(result, flags, precedence, ...) {
    // 1. Preprocessing-expression context
    //    In #if, undefined identifiers evaluate to 0
    if (in_preprocessing_expression) {
        // "scan_identifier: in preprocessing expr"
        result = make_integer_constant(0)
        return
    }

    // 2. Look up identifier in current scope
    lookup_result = scope_lookup(current_identifier, current_scope)

    // 3. If identifier resolves to a type name → functional-notation cast
    if (is_type_entity(lookup_result)) {
        scan_functional_notation_type_conversion(type, result, ...)  // sub_54E7C0
        return
    }

    // 4. If identifier is an anonymous union member
    if (is_anonymous_union_member(lookup_result)) {
        // Walk up to find the named parent variable
        parent = anonymous_parent_variable_of(lookup_result)
        result = make_anonymous_union_field_operand(parent, lookup_result)
        return
    }

    // 5. If identifier is a function (possibly overloaded)
    if (is_function_entity(lookup_result)) {
        result = make_func_operand(lookup_result)
        // Lambda capture check
        if (in_lambda_scope)
            check_var_for_lambda_capture(lookup_result, ...)
        return
    }

    // 6. Variable reference
    result = make_var_operand(lookup_result)

    // 7. Lambda capture analysis
    if (in_lambda_scope)
        check_var_for_lambda_capture(lookup_result, ...)

    // 8. Cross-execution-space reference check (CUDA)
    if (cuda_mode)
        check_cross_execution_space_reference(lookup_result, ...)
}
```

## CUDA-Specific: Cross-Execution-Space Call Validation

Two functions implement the CUDA execution space enforcement that prevents illegal calls between `__host__` and `__device__` code:

### check_cross_execution_space_call (`sub_505720`)

Called from `scan_function_call` and other call sites. The function extracts execution space information from bit-packed flags at entity offset `+182`:

```c
function check_cross_execution_space_call(callee, is_must_check, diag_ctx) {
    // Extract callee's execution space from entity flags
    if (callee != NULL) {
        is_not_device_only = (callee[182] & 0x30) != 0x20  // bits 4-5
        is_host_only       = (callee[182] & 0x60) == 0x20  // bits 5-6
        is_global          = (callee[182] & 0x40) != 0      // bit 6
    }

    // Early exits for special contexts
    if (compilation_chain == -1)    return   // not in compilation
    if (CU has CUDA flags cleared)  return   // not a CUDA compilation unit
    if (in_SFINAE_context)          return   // errors suppressed

    // Get caller's execution space from enclosing function
    enclosing_fn = CU_table[enclosing_CU_index].function  // at +224
    if (enclosing_fn != NULL) {
        caller_host_only = (enclosing_fn[182] & 0x60) == 0x20
        caller_not_device_only = (enclosing_fn[182] & 0x30) != 0x20
    } else {
        // Top-level code: treated as __host__
        caller_host_only = 0
        caller_not_device_only = 1
    }

    // Check for implicitly HD (constexpr or __host__ __device__ by inference)
    if (callee[177] & 0x10)  return   // callee is implicitly HD
    if (callee has deleted+explicit HD flags)  return

    // The actual cross-space check matrix:
    // caller=host,  callee=device  → error 3462 or 3463
    // caller=device, callee=host   → error 3464 or 3465
    // callee=__global__            → error 3508

    if (caller_not_device_only && caller_host_only) {
        // Caller is __host__ only
        if (callee is __device__ only) {
            if (is_trivial_device_copyable(callee))  // sub_6BC680
                return  // allow
            space1 = get_execution_space_name(enclosing_fn, 0)  // sub_6BC6B0
            space2 = get_execution_space_name(callee, 1)
            emit_error(3462 + has_explicit_host, ...)
        }
    } else if (caller_not_device_only) {
        // Caller is __device__ only
        if (callee is __host__ only)
            emit_error(3464 + has_explicit_device, ...)
    }

    if (callee is __global__) {
        emit_error(3508, is_must_check ? "must" : "cannot", ...)
    }
}
```

The bit encoding at entity offset `+182`:

| Bits | Mask | Meaning |
|---|---|---|
| 4–5 | `& 0x30` | `__device__` flag: `0x20` = device-only |
| 5–6 | `& 0x60` | `__host__` flag: `0x20` = host-only |
| 6 | `& 0x40` | `__global__` flag |

Error codes issued:

| Code | Meaning |
|---|---|
| 3462 | `__device__` function called from `__host__` context |
| 3463 | Variant of 3462 with `__host__` annotation note |
| 3464 | `__host__` function called from `__device__` context |
| 3465 | Variant of 3464 with `__device__` annotation note |
| 3508 | `__global__` function called from wrong context |

### check_cross_space_call_in_template (`sub_505B40`)

A simplified variant (2.7KB) used during template instantiation. The logic mirrors `check_cross_execution_space_call` but operates when `dword_126C5C4 == -1` (template instantiation depth guard). It does not take the `is_must_check` parameter and always checks both directions.

See the [Execution Spaces](../cuda/execution-spaces.md) page for full details on the CUDA execution model.

## CUDA-Specific: adjust_sync_atomic_builtin

`adjust_sync_atomic_builtin` (`sub_537BF0`, 1,108 lines) is the largest NVIDIA-specific function in the expression parser. It transforms GCC-style `__sync_fetch_and_*` atomic builtins into NVIDIA's own `__nv_atomic_fetch_*` intrinsics.

### Why This Remapping Exists

CUDA inherits GCC's `__sync_fetch_and_*` builtin family from the host-side C/C++ dialect, but NVIDIA's GPU ISA (PTX) uses a different instruction encoding for atomic operations. The GPU atomics have type-specific variants that the PTX backend needs to select the correct instruction. Rather than teaching the backend to decompose generic `__sync_*` builtins, NVIDIA front-loads the transformation in the parser, mapping each builtin to a type-suffixed `__nv_atomic_fetch_*` intrinsic that directly corresponds to a PTX atomic instruction.

The type suffix ensures correct instruction selection:

| Suffix | Type Category | PTX Atomic Type |
|---|---|---|
| `_s` | Signed integer | `.s32`, `.s64` |
| `_u` | Unsigned integer | `.u32`, `.u64` |
| `_f` | Floating-point | `.f32`, `.f64` |

### Remapping Table

| GCC Builtin | NVIDIA Intrinsic (base) |
|---|---|
| `__sync_fetch_and_add` | `__nv_atomic_fetch_add` |
| `__sync_fetch_and_sub` | `__nv_atomic_fetch_sub` |
| `__sync_fetch_and_and` | `__nv_atomic_fetch_and` |
| `__sync_fetch_and_xor` | `__nv_atomic_fetch_xor` |
| `__sync_fetch_and_or` | `__nv_atomic_fetch_or` |
| `__sync_fetch_and_max` | `__nv_atomic_fetch_max` |
| `__sync_fetch_and_min` | `__nv_atomic_fetch_min` |

### Pseudocode

```c
function adjust_sync_atomic_builtin(callee, args, arg_list, builtin_info, result_ptr) {
    // assert "adjust_sync_atomic_builtin" at line 6073

    original_entity = get_builtin_entity(callee)   // sub_568F30
    assert(original_entity != NULL)

    // Check arg count -- if extra args and first arg is not pointer type
    if (builtin_info.extra_arg_count && callee[8] != 1) {
        // Reset and emit diagnostic 3768 (wrong arg type for atomic)
        original_entity = NULL
        if (validate_arg_types(...))
            emit_error(3768, diag_ctx)
        return original_entity
    }

    // Walk argument list to find the pointee type (type of *ptr)
    if (args == NULL) {
        // Use declared arg count from builtin info
        arg_index = builtin_info.declared_arg_count
        // ... validate, may emit error 3769 or 1645
    } else {
        // Navigate to the relevant argument node
        // Extract the pointee type by unwinding cv-qualifiers
        arg_type = get_init_component_type(args)
        pointee = unwrap_cv_qualifiers(arg_type)  // while type_kind == 12
    }

    // Determine the type suffix based on pointee type
    if (is_integer_type(pointee)) {
        if (is_signed(pointee))
            suffix = "_s"    // signed
        else
            suffix = "_u"    // unsigned
    } else if (is_float_type(pointee)) {
        suffix = "_f"        // floating-point
    } else {
        // Not a supported atomic type
        if (validate_arg_types(...))
            emit_error(1645 or 852, diag_ctx)
        return original_entity
    }

    // Construct the NVIDIA intrinsic name
    // Map __sync_fetch_and_OP → __nv_atomic_fetch_OP + suffix
    base_name = map_sync_to_nv(original_entity.name)
    // e.g., "__sync_fetch_and_add" → "__nv_atomic_fetch_add"
    full_name = base_name + suffix
    // e.g., "__nv_atomic_fetch_add_s" for signed int

    // Look up or create the NVIDIA intrinsic entity
    nv_entity = lookup_nv_intrinsic(full_name)

    // Replace the callee with the NVIDIA intrinsic
    *result_ptr = nv_entity

    return original_entity
}
```

The function validates that the pointee type is one of the supported atomic types. If the user passes a pointer to an unsupported type (e.g., a struct), it falls through to emit diagnostic 1645 ("argument type not supported for atomic operation") or 852 (a more specific variant when the `__sync` function has explicit type constraints).

## Template Expression Rescanning

When a template is instantiated, expression trees from the template definition are re-evaluated with concrete template argument substitutions. This is handled by `rescan_expr_with_substitution_internal` (`sub_5565E0`, 1,558 lines), the third-largest function in the expression parser.

The function dispatches on expression kind (not token kind — these are IL expression nodes, not source tokens) and recursively rescans each sub-expression with substitutions applied:

| Assert String | Purpose |
|---|---|
| `"rescan_expr_with_substitution_internal"` | Entry point |
| `"operator_token_for_builtin_operator"` | Maps operator codes to tokens for rescan |
| `"operator_token_for_expr_rescan"` | Alternate operator-to-token mapping |
| `"invalid expr kind in expr rescan"` | Unreachable default case |
| `"rescan_braced_init_list"` | Rescans `{init-list}` nodes |
| `"make_operand_for_rescanned_identifier"` | Rebuilds identifier operands after substitution |
| `"symbol_for_template_param_unknown_entity_rescan"` | Handles dependent names during rescan |
| `"scan_rel_operator"` | Rescans relational operators (for comparison rewriting) |

The key insight is that during template definition parsing, the parser builds a partially-evaluated expression tree where template-dependent parts are stored as opaque nodes. During instantiation, this function walks that tree, substitutes concrete types/values, and re-runs the semantic analysis that was deferred.

## Supporting Infrastructure

### Diagnostic Emission (30+ wrapper functions, `0x4F8000`--`0x4F8F80`)

The expression parser uses a family of thin diagnostic wrapper functions at the beginning of the address range. Each wraps the core pattern: `create_diag(code)` -> `add_arg(type/entity/string)` -> `emit(diag)`. The variants differ only in argument count and types:

| Function | Identity | Arguments |
|---|---|---|
| `sub_4F8090` | `emit_diag_with_type_and_entity` | Type arg + entity arg |
| `sub_4F8160` | `emit_diag_1arg` | Single argument |
| `sub_4F8220` | `emit_diag_with_2_type_args` | Two type arguments |
| `sub_4F8320` | `emit_diag_with_entity_and_type` | Entity first, type second |
| `sub_4F8B20` | `issue_incomplete_type_diag` | Incomplete type diagnostic (assert confirmed) |

### Expression Stack (`exprutil.c`, `0x558720`+)

The expression parser maintains a stack of expression contexts via `qword_106B970`. Each stack entry (the "current context") holds compilation mode flags, scope depth, CUDA execution space state, and template context bits. Key operations:

| Function | Identity | Purpose |
|---|---|---|
| `sub_55D0D0` | `save_expr_stack` | Saves current expression stack state |
| `sub_55D100` | `init_expr_stack_entry` | Creates new stack frame |
| `sub_55DB50` | `pop_expr_stack` | Restores previous frame |
| `sub_55E490` | `set_operand_kind` | Sets the operand classification |
| `sub_55C180` | `alloc_ref_entry` | Allocates reference-entry for tracking |
| `sub_55C830` | `free_init_component` | Frees initializer component node |

### Comparison Rewriting (C++20, `0x501020`--`0x508DC0`)

The C++20 three-way comparison operator (`<=>`) triggers rewriting of traditional comparison expressions. `complete_comparison_rewrite` (`sub_505E80`, 6.9KB) rewrites `a < b` into `(a <=> b) < 0` when a spaceship operator exists. It uses a recursion counter at `qword_106B510` limited to 100 to prevent infinite rewrite loops. Related functions:

| Function | Identity |
|---|---|
| `sub_501020` | `determine_defaulted_spaceship_return_type` |
| `sub_5015D0` | `synthesize_defaulted_comparison_body` |
| `sub_501B00` | `check_comparison_category_type` |
| `sub_505E10` | `token_for_rel_op` — maps operator kinds to tokens (16->43, 17->44, 32->45, 33->46) |
| `sub_505E80` | `complete_comparison_rewrite` — core rewrite engine |
| `sub_506430` | `check_defaulted_eq_properties` |
| `sub_5068F0` | `check_defaulted_secondary_comp` |

### Range-Based For Loop Desugaring (`0x50C510`, 16.8KB)

`fill_in_range_based_for_loop_constructs` (`sub_50C510`) generates the desugared components of `for (auto x : range)`:

```cpp
// Source:     for (auto x : range_expr) body
// Desugared:  {
//               auto && __range = range_expr;
//               auto __begin = begin(__range);
//               auto __end = end(__range);
//               for (; __begin != __end; ++__begin) {
//                 auto x = *__begin;
//                 body
//               }
//             }
```

The function calls `sub_6EF7A0` (overload resolution) to look up `begin()` and `end()` via ADL, and emits error 2285 when no suitable `begin`/`end` is found.

## Key Global Variables

| Address | Name | Type | Description |
|---|---|---|---|
| `word_126DD58` | `current_token_code` | WORD | Current token kind (0–356) |
| `qword_126DD38` | `current_source_position` | QWORD | Encoded file/line/column |
| `qword_106B970` | `current_scope` | QWORD | Expression context stack pointer |
| `qword_106B968` | `pending_expression` | QWORD | Pending expression accumulator |
| `dword_126EFC8` | `debug_trace_flag` | DWORD | Nonzero enables trace output |
| `dword_126EFCC` | `debug_verbosity` | DWORD | Trace verbosity level (>3 prints precedence) |
| `dword_126EFB4` | `language_dialect` | DWORD | 1=C, 2=C++ |
| `qword_126EF98` | `standard_version` | QWORD | Language standard version level |
| `dword_126EFA8` | `in_template_context` | DWORD | Nonzero during template parsing |
| `dword_126EFA4` | `strict_mode` | DWORD | Strict conformance mode flag |
| `dword_126EFAC` | `extended_features` | DWORD | Extended features enabled |
| `xmmword_106C380` | `identifier_lookup_result` | 128-bit | SSE-packed identifier lookup (64 bytes total, 4 xmmwords) |
| `qword_106B510` | `comparison_rewrite_depth` | QWORD | Recursion counter for C++20 comparison rewriting (max 100) |
| `dword_106C2C0` | `gpu_compilation_mode` | DWORD | Nonzero during GPU compilation |
| `qword_126C5E8` | `compilation_unit_table` | QWORD | Base of CU array (784-byte stride) |
| `dword_126C5E4` | `current_CU_index` | DWORD | Index into compilation unit table |
| `dword_126C5D8` | `enclosing_function_CU_index` | DWORD | CU index of enclosing function |
| `dword_126C5C4` | `template_instantiation_depth` | DWORD | -1 = not in template instantiation |

## Diagnostic Codes

The expression parser emits approximately 50 distinct diagnostic codes:

| Code | Meaning |
|---|---|
| 57 | Pointer-to-member on non-class type |
| 58 | Pointer-to-member on incomplete type |
| 60 | Pointer-to-member on wrong class type |
| 165 | Wrong argument count for builtin |
| 244 | Type access violation in member selection |
| 529 | Pointer-to-member in concept context |
| 852 | Unsupported type for atomic operation (typed variant) |
| 1022 | Inaccessible member in selection |
| 1032 | Invalid `_Generic` controlling expression |
| 1036 | Unsupported predefined function name |
| 1436 | `__builtin_types_compatible_p` not available |
| 1543 | `__builtin_source_location` not available |
| 1596 | Invalid literal operator call |
| 1645 | Argument type not supported for atomic operation |
| 1733 | `new`-expression in module context |
| 1763 | GNU statement expression not available |
| 1777 | Statement expression in constexpr context |
| 2285 | No `begin`/`end` for range-based for |
| 2669 | `co_yield` outside coroutine |
| 2747 | `co_yield` not in function scope |
| 2866 | Statement expression in constexpr context |
| 2896 | Statement expression in template instantiation |
| 2982 | Comparison rewrite recursion limit exceeded |
| 3462 | `__device__` function called from `__host__` context |
| 3463 | Variant of 3462 with `__host__` annotation note |
| 3464 | `__host__` function called from `__device__` context |
| 3465 | Variant of 3464 with `__device__` annotation note |
| 3508 | `__global__` function called from wrong context |
| 3768 | Wrong argument type for atomic builtin (extra arg) |
| 3769 | Wrong argument type for atomic builtin (declared arg) |

## Function Index

Complete listing of confirmed functions in the expression parser, grouped by subsystem:

### Core Expression Scanning

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_511D40` | 80KB | `scan_expr_full` | DEFINITE |
| `sub_526E30` | 48KB | `scan_conditional_operator` | DEFINITE |
| `sub_545F00` | 16KB | `scan_function_call` | DEFINITE |
| `sub_54AED0` | 15KB | `scan_new_operator` | DEFINITE |
| `sub_5512B0` | 9KB | `scan_identifier` | DEFINITE |
| `sub_544290` | 6KB | `scan_cast_or_expr` | DEFINITE |
| `sub_5565E0` | 10KB | `rescan_expr_with_substitution_internal` | DEFINITE |
| `sub_529720` | 12KB | `scan_comma_operator` | DEFINITE |
| `sub_526040` | 15KB | `scan_logical_operator` | DEFINITE |
| `sub_543A90` | 1.4KB | `scan_rel_operator` | DEFINITE |
| `sub_540160` | 1.2KB | `apply_one_fold_operator` | DEFINITE |
| `sub_543FA0` | 1KB | `assemble_fold_expression_operand` | DEFINITE |

### Unary Operators

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_516080` | 7.6KB | `scan_prefix_incr_decr` | DEFINITE |
| `sub_516720` | 13KB | `scan_ampersand_operator` | DEFINITE |
| `sub_517270` | 4.4KB | `scan_indirection_operator` | DEFINITE |
| `sub_517680` | 5.1KB | `scan_arith_prefix_operator` | DEFINITE |
| `sub_517BD0` | 26KB | `scan_sizeof_operator` | DEFINITE |
| `sub_519300` | 9.4KB | `scan_alignof_operator` | DEFINITE |
| `sub_519CF0` | 6.1KB | `scan_builtin_addressof` | DEFINITE |
| `sub_510D70` | 8.2KB | `scan_postfix_incr_decr` | DEFINITE |

### Binary Operators

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_5238C0` | 5.4KB | `scan_mult_operator` | DEFINITE |
| `sub_523EB0` | 10.6KB | `scan_add_operator` | DEFINITE |
| `sub_524960` | 5.8KB | `scan_shift_operator` | DEFINITE |
| `sub_524ED0` | 5.6KB | `scan_eq_operator` | DEFINITE |
| `sub_525BC0` | 4.7KB | `scan_bit_operator` | DEFINITE |
| `sub_525450` | 8.6KB | `scan_gnu_min_max_operator` | DEFINITE |
| `sub_522650` | 19.8KB | `scan_ptr_to_member_operator` | DEFINITE |

### Assignment

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_53FD70` | 1.1KB | `scan_simple_assignment_operator` | DEFINITE |
| `sub_536E80` | 3.1KB | `scan_compound_assignment_operator` | DEFINITE |
| `sub_508770` | 4.7KB | `process_simple_assignment` | DEFINITE |

### Member Access

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_5303E0` | 15KB | `scan_field_selection_operator` | DEFINITE |
| `sub_4FEB60` | 4.5KB | `make_field_selection_operand` | DEFINITE |
| `sub_4FEF00` | 4.6KB | `do_field_selection_operation` | DEFINITE |
| `sub_540560` | 3.1KB | `scan_subscript_operator` | DEFINITE |

### Cast Operators

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_51EE00` | 8.3KB | `scan_new_style_cast` | DEFINITE |
| `sub_51F670` | 13.5KB | `scan_static_cast_operator` | DEFINITE |
| `sub_520280` | 8.8KB | `scan_const_cast_operator` | DEFINITE |
| `sub_5209A0` | 4.9KB | `scan_reinterpret_cast_operator` | DEFINITE |
| `sub_53C690` | 3.6KB | `scan_named_cast_operator` | HIGH |

### Type Traits

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_51A690` | 12KB | `scan_unary_type_trait_helper` | DEFINITE |
| `sub_51B650` | 7.2KB | `scan_binary_type_trait_helper` | DEFINITE |
| `sub_535080` | 0.2KB | `dispatch_call_like_builtin` | MEDIUM |
| `sub_534B60` | 1.8KB | `scan_call_like_builtin_operation` | DEFINITE |
| `sub_549700` | 2.2KB | `compute_is_invocable` | DEFINITE |
| `sub_550E50` | 1.3KB | `compute_is_constructible` | DEFINITE |
| `sub_510410` | 2.1KB | `compute_is_convertible` | DEFINITE |
| `sub_510860` | 2.3KB | `compute_is_assignable` | DEFINITE |

### CUDA-Specific

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_505720` | 4KB | `check_cross_execution_space_call` | DEFINITE |
| `sub_505B40` | 2.7KB | `check_cross_space_call_in_template` | DEFINITE |
| `sub_537BF0` | 7KB | `adjust_sync_atomic_builtin` | DEFINITE |
| `sub_520EE0` | 2.7KB | `scan_intaddr_operator` | DEFINITE |

### Initializers and Braced-Init-Lists

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_5360D0` | 4.7KB | `parse_braced_init_list_full` | DEFINITE |
| `sub_5392B0` | 0.2KB | `complete_braced_init_list_parsing` | DEFINITE |
| `sub_539340` | 1KB | `scan_braced_init_list_cast` | DEFINITE |
| `sub_539670` | 0.4KB | `get_braced_init_list` | DEFINITE |
| `sub_541000` | 2KB | `scan_member_constant_initializer_expression` | DEFINITE |
| `sub_541DC0` | 5.5KB | `prescan_initializer_for_auto_type_deduction` | DEFINITE |

### Coroutines

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_50B630` | 10KB | `add_await_to_operand` | DEFINITE |
| `sub_50C070` | 1.8KB | `check_coroutine_context` | HIGH |
| `sub_50E080` | 4.5KB | `make_coroutine_result_expression` | DEFINITE |

### C++20 Concepts and Requires

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_52CFF0` | 13.5KB | `scan_requires_expression` | DEFINITE |
| `sub_542D90` | 3.8KB | `scan_requires_expr` | DEFINITE |
| `sub_52EB60` | 8.6KB | `scan_requires_clause` | DEFINITE |
