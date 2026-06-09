# Constexpr Interpreter

The constexpr interpreter is the compile-time expression evaluation engine inside cudafe++. It lives in EDG 6.6's `interpret.c` (69 functions at `0x620CE0`--`0x65DE10`, approximately 33,000 decompiled lines) and implements a virtual machine that executes arbitrary C++ expressions during compilation. Its central function, `do_constexpr_expression` (`sub_634740`), is the single largest function in the entire cudafe++ binary: 11,205 decompiled lines, 63KB of machine code, 128 unique callees, and 28 self-recursive call sites.

The interpreter exists because C++ constexpr evaluation requires the compiler to act as an execution engine. Since C++11, constexpr has grown from simple return-expression functions to a Turing-complete subset of C++ that includes loops, branches, dynamic memory allocation (C++20), virtual dispatch, exception-like control flow, and — as of C++26 — compile-time reflection. The interpreter must evaluate all of these constructs faithfully, track object lifetimes, detect undefined behavior, and convert results back into IL constants.

## Key Facts

| Property | Value |
|---|---|
| Source file | `interpret.c` (69 functions, ~33,000 decompiled lines) |
| Address range | `0x620CE0`--`0x65DE10` |
| Main evaluator | `sub_634740` (`do_constexpr_expression`), 11,205 lines, 63KB |
| Builtin evaluator | `sub_651150` (`do_constexpr_builtin_function`), 5,032 lines |
| Loop evaluator | `sub_644580` (`do_constexpr_range_based_for_statement`), 2,836 lines |
| Constructor evaluator | `sub_6480F0` (`do_constexpr_ctor`), 1,659 lines |
| Call dispatcher | `sub_657560` (`do_constexpr_call`), 1,445 lines |
| Top-level entry | `sub_65AE50` (`interpret_expr`) |
| Materialization | `sub_631110` (`copy_interpreter_object_to_constant`), 1,444 lines |
| Value extraction | `sub_64B580` (`extract_value_from_constant`), 2,299 lines |
| Arena block size | 64KB (`0x10000`) |
| Large alloc threshold | 1,024 bytes (`0x400`) |
| Max type size | 64MB (`0x4000000`) |
| Uninitialized marker | `0xDB` fill pattern |
| Self-recursive calls | 28 (in `do_constexpr_expression`) |
| Confirmed assert IDs | 38 functions with assert strings |
| C++26 reflection | 8 `std::meta::*` functions |

## Architecture Overview

The interpreter is structured as a tree-walking evaluator with arena-based memory, memoization caching, and a call stack that mirrors C++ function invocation. The rest of the compiler invokes it through `interpret_expr`, which sets up interpreter state, calls the recursive evaluator, and converts the result back to an IL constant.

```text
  AST expression node
        |
        v
  +-----------------+
  | interpret_expr  |  sub_65AE50 — allocates state, arena, hash table
  +-----------------+
        |
        v
  +---------------------------+
  | do_constexpr_expression   |  sub_634740 — the 11,205-line evaluator
  |                           |  dispatches on expression-kind code
  |  +-- arithmetic ops       |  cases 40-45: +, -, *, /, %
  |  +-- comparisons          |  cases 49-51: <, >, ==, !=, <=, >=
  |  +-- member access        |  cases 3-4: . and ->
  |  +-- type conversions     |  case 5: cast sub-switch (20+ type pairs)
  |  +-- pointer arithmetic   |  cases 46-48, 50: ptr+int, ptr-ptr
  |  +-- function calls ------+---> do_constexpr_call (sub_657560)
  |  +-- constructors   ------+---> do_constexpr_ctor (sub_6480F0)
  |  +-- builtins       ------+---> do_constexpr_builtin_function (sub_651150)
  |  +-- loops          ------+---> do_constexpr_range_based_for (sub_644580)
  |  +-- statements     ------+---> do_constexpr_statement (sub_647850)
  |  +-- dynamic_cast         |  inline within main evaluator
  |  +-- typeid               |  inline within main evaluator
  |  +-- offsetof             |  inline within main evaluator
  |  +-- bit_cast             |  calls translate_*_bytes functions
  +---------------------------+
        |
        v
  +-------------------------------------+
  | copy_interpreter_object_to_constant |  sub_631110 — materializes result
  +-------------------------------------+  back into IL constant nodes
        |
        v
  IL constant (returned to compiler)
```

### Entry Points

The interpreter has multiple entry points, each called from a different compilation phase:

| Entry | Address | Lines | Called from |
|---|---|---|---|
| `interpret_expr` | `sub_65AE50` | 572 | General constexpr evaluation (primary) |
| Entry for expression lowering | `sub_65A290` | 311 | Expression lowering phase (`sub_6E2040`) |
| Entry for expression trees | `sub_65A8C0` | 274 | Expression handling (`sub_5BB4C0`, `sub_5C3760`) |
| `interpret_dynamic_sub_initializers` | `sub_65CFA0` | 67 | Aggregate initialization |
| Misc entries | `sub_65BAB0`--`sub_65D150` | 150-470 | Template instantiation, `static_assert`, enum values |

All entry points follow the same pattern: allocate the interpreter state object, initialize the arena and hash table, call `do_constexpr_expression`, then extract and convert the result.

## Interpreter State Object

The interpreter state is a structure passed as the first argument (`a1`) to every evaluator function. It contains the evaluation stack, heap tracking, memoization cache, and diagnostic context.

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `hash_table` | Pointer to variable-to-value hash table |
| `+8` | 8 | `hash_capacity` | Hash table capacity mask (low 32) / entry count (high 32) |
| `+16` | 8 | `stack_top` | Current stack allocation pointer |
| `+24` | 8 | `stack_base` | Base of current arena block |
| `+32` | 8 | `heap_list` | Head of heap allocation chain (large objects) |
| `+40` | 4 | `scope_depth` | Current scope nesting counter |
| `+56` | 8 | `hash_aux_1` | Auxiliary hash table pointer |
| `+64` | 8 | `hash_aux_2` | Auxiliary hash table capacity |
| `+72` | 8 | `call_chain` | Current call stack chain (for recursion tracking) |
| `+88` | 8 | `diag_context_1` | Diagnostic context pointer |
| `+96` | 8 | `diag_context_2` | Source location for error reporting |
| `+112` | 8 | `diag_context_3` | Additional diagnostic metadata |
| `+132` | 1 | `flags_1` | Mode flags (bit 0 = strict mode) |
| `+133` | 1 | `flags_2` | Additional mode flags |

## Memory Model

The interpreter uses a dual-tier memory system: an arena allocator for small objects and direct heap allocation for large ones.

### Arena Allocator

Arena blocks are 64KB (`0x10000` bytes) each, linked together at offset `+24`:

```text
Block layout:
  +------------------+
  | next_block (+0)  |---> previous block (or null)
  | alloc_ptr  (+8)  |---> current bump position
  | capacity   (+16) |---> end of usable space
  | base       (+24) |---> start of block data
  +------------------+
  | usable space     |  64KB of object storage
  | ...              |
  +------------------+
```

Allocation follows a bump-pointer pattern:

```c
void *arena_alloc(interp_state *state, size_t size) {
    size = ALIGN_UP(size, 8);
    ptrdiff_t remaining = 0x10000 - (state->stack_top - state->stack_base);
    if (remaining < size) {
        // Allocate new 64KB block, link to chain
        new_block = sub_622D20();
        new_block->next = state->stack_base;
        state->stack_base = new_block;
        state->stack_top = new_block + HEADER_SIZE;
    }
    void *result = state->stack_top;
    state->stack_top += size;
    return result;
}
```

### Large Object Heap

Objects larger than 1,024 bytes (`0x400`) bypass the arena and are allocated individually via `sub_6B7340` (the compiler's general-purpose allocator). These allocations are tracked through an allocation chain so they can be freed when the interpreter scope exits.

### Object Header Layout

Every interpreter object has a header preceding the value bytes:

```text
  offset -10  [-10]  bitmap byte 2 (validity tracking)
  offset  -9  [ -9]  bitmap byte 1 (initialization tracking)
  offset  -8  [ -8]  type pointer (8 bytes, points to type_node)
  offset   0  [  0]  value bytes start here
              ...     value data (size depends on type)
```

New objects are initialized with value bytes filled to `0xDB` (decimal 219), which serves as an uninitialized-memory sentinel. Any read from an object whose bytes still contain `0xDB` triggers error 2700 (access to uninitialized object).

### Constexpr Value Representation

Values in the interpreter use a type-dependent representation:

| Type category | `kind` byte | Value size | Representation |
|---|---|---|---|
| `void` | 0 | 0 | Flag `0x40` set, no value bytes |
| `pointer` | 1 | 0 | Stored as reference metadata, not inline bytes |
| `integral` | 2 | 16 bytes | Two 64-bit words (supports `__int128`) |
| `float` | 3 | 16 bytes | IEEE 754 value in first 4/8 bytes, padded |
| `double` | 4 | 16 bytes | IEEE 754 value in first 8 bytes, padded |
| `complex` | 5 | 32 bytes | Real + imaginary parts |
| `class/struct` | 6 | 32 bytes | Reference to interpreter object |
| `union` | 7 | 32 bytes | Reference to interpreter object |
| `array` | 8 | N * elem_size | Recursive: element count times element size |
| `class` (variants) | 9, 10, 11 | Cached | Looked up in type-to-size hash table |
| `typedef` | 12 | (follow) | Chase to underlying type |
| `enum` | 13 | 16 bytes | Same as integral |
| `nullptr_t` | 19 | 32 bytes | Null pointer representation |

The reference representation for pointers and class objects uses 32 bytes (two `__m128i` values). The flag byte at offset `+8` within a reference encodes:

| Bit | Meaning |
|---|---|
| 0 | Has concrete object backing |
| 1 | Past-the-end pointer (one past array) |
| 2 | Has allocation chain (from `constexpr new`) |
| 3 | Has subobject path (member/base offset chain) |
| 4 | Has bitfield information |
| 5 | Is dangling (object lifetime ended) |
| 6 | Is const-qualified |

### Memoization Hash Table

The interpreter maintains a hash table that maps type pointers to precomputed value sizes, avoiding redundant recursive size computations for class types:

| Global | Purpose |
|---|---|
| `qword_126FEC0` | Hash table base pointer |
| `qword_126FEC8` | Capacity mask (low 32 bits) / entry count (high 32 bits) |

Each entry is 16 bytes: 8-byte key (type pointer), 4-byte size value, 4-byte padding. Collision resolution uses linear probing with a bitmask. The table grows (via `sub_620760`) when load factor exceeds 50%.

### Constexpr Allocation Tracking (C++20)

C++20 introduced `constexpr` dynamic memory allocation (`new`/`delete` in constexpr contexts). The interpreter tracks these through a global allocation chain:

| Global | Purpose |
|---|---|
| `qword_126FBC0` | Free list head |
| `qword_126FBB8` | Outstanding allocation count |

When `std::allocator<T>::allocate()` is called during constexpr (`sub_62B100`), the interpreter allocates from its arena, sets bit 2 in the object's flag byte, and links the allocation into the chain. `std::allocator<T>::deallocate()` (`sub_62B470`) validates that the freed pointer was actually allocated by `std::allocator::allocate()` and unlinks it. At the end of constexpr evaluation, any remaining allocations indicate a bug in the evaluated code (memory leaked during constant evaluation).

## The Main Evaluator: do_constexpr_expression

`sub_634740` is the heart of the interpreter. It takes four parameters:

```c
// Returns 1 on success, 0 on failure
int do_constexpr_expression(
    interp_state *a1,       // interpreter state
    expr_node    *a2,       // AST expression node to evaluate
    value_buf    *a3,       // output value buffer (32 bytes)
    address_t    *a4        // "home" address for reference tracking
);
```

The function body is organized as a nested switch statement. The outer switch dispatches on the expression category at `*(a2+24)`, and several cases contain inner switches for further dispatch.

### Outer Switch: Expression Categories

```c
int do_constexpr_expression(interp_state *a1, expr_node *a2,
                            value_buf *a3, address_t *a4) {
    int category = *(a2 + 24);    // expression category code
    switch (category) {
    case 0:   // ---- void expression ----
        a3->flags = 0x40;         // mark as void
        return 1;

    case 1:   // ---- operator expression ----
        return eval_operator(a1, a2, a3, a4);    // inner switch on *(a2+40)

    case 10:  // ---- sub-expression wrapper ----
        return do_constexpr_expression(a1, *(a2+40), a3, a4);  // recurse

    case 11:  // ---- typeid expression ----
        return do_constexpr_typeid(a1, a2, a3);  // inline

    case 17:  // ---- statement expression (GNU extension) ----
        return do_constexpr_statement(a1, *(a2+40), a3);  // sub_647850

    case 18:  // ---- variable lookup ----
        return lookup_variable(a1, a2, a3, a4);  // hash table at a1+0

    case 19:  // ---- function / static variable reference ----
        return resolve_static_ref(a1, a2, a3);

    case 21:  // ---- special expressions ----
        return eval_special(a1, a2, a3, a4);     // inner switch on *(a2+40)

    default:
        emit_error(2721);  // "expression is not a constant expression"
        return 0;
    }
}
```

### Inner Switch: Operator Codes (case 1)

The operator expression case dispatches on the operator code at `*(a2+40)`. This is the largest sub-switch, covering 100+ cases:

```c
int eval_operator(interp_state *a1, expr_node *a2,
                  value_buf *a3, address_t *a4) {
    int opcode = *(a2 + 40);
    switch (opcode) {

    // ---- Assignment ----
    case 0: case 1:
        // Evaluate RHS, store to LHS address
        if (!do_constexpr_expression(a1, rhs, &rval, NULL)) return 0;
        if (!do_constexpr_expression(a1, lhs, &lval, NULL)) return 0;
        assign_value(lval.address, &rval, type);
        *a3 = lval;
        return 1;

    // ---- Member access (. and ->) ----
    case 3: case 4:
        // Evaluate base object, compute member offset
        if (!do_constexpr_expression(a1, base_expr, &base, NULL)) return 0;
        member_offset = compute_member_offset(member_decl, base.type);
        a3->address = base.address + member_offset;
        return 1;

    // ---- Type conversion (cast) ----
    case 5:
        return eval_conversion(a1, a2, a3, a4);  // 20+ type-pair sub-switch

    // ---- Parenthesized expression ----
    case 9:
        return do_constexpr_expression(a1, *(a2+48), a3, a4);  // recurse

    // ---- Pointer increment/decrement ----
    case 22: case 23:
        if (!do_constexpr_expression(a1, operand, &val, NULL)) return 0;
        // Validate pointer is within array bounds
        pos = get_runtime_array_pos(val.address);
        if (pos < 0 || pos >= array_size) {
            emit_error(2692);  // array bounds violation
            return 0;
        }
        val.address += (opcode == 22) ? elem_size : -elem_size;
        *a3 = val;
        return 1;

    // ---- Unary negation / bitwise complement ----
    case 26:
        if (!do_constexpr_expression(a1, operand, &val, NULL)) return 0;
        if (is_integer_type(val.type))
            a3->int_val = ~val.int_val;  // or -val.int_val
        else if (is_float_type(val.type))
            a3->float_val = -val.float_val;
        return 1;

    // ---- Arithmetic binary operators ----
    case 40:  // addition
    case 41:  // subtraction
    case 42:  // multiplication
    case 43:  // division
    case 44:  // modulo
    case 45:  // (additional arithmetic)
        if (!do_constexpr_expression(a1, lhs, &left, NULL)) return 0;
        if (!do_constexpr_expression(a1, rhs, &right, NULL)) return 0;
        if (opcode == 43 && right.int_val == 0) {
            emit_error(61);    // division by zero = UB
            return 0;
        }
        result = apply_arithmetic(opcode, left, right, type);
        if (check_overflow(result, type)) {
            emit_error(2708);  // arithmetic overflow
            return 0;
        }
        *a3 = result;
        return 1;

    // ---- Pointer arithmetic ----
    case 46:  // pointer + integer
    case 47:  // integer + pointer
    case 48:  // pointer - integer
        if (!do_constexpr_expression(a1, ptr_expr, &ptr, NULL)) return 0;
        if (!do_constexpr_expression(a1, int_expr, &idx, NULL)) return 0;
        // Validate result stays within allocation bounds
        new_pos = get_runtime_array_pos(ptr.address) + idx.int_val;
        if (new_pos < 0 || new_pos > array_size) {  // past-the-end is valid
            emit_error(2735);  // pointer arithmetic underflow/overflow
            return 0;
        }
        a3->address = ptr.address + idx.int_val * elem_size;
        return 1;

    // ---- Comparison operators ----
    case 49: case 50: case 51:
        if (!do_constexpr_expression(a1, lhs, &left, NULL)) return 0;
        if (!do_constexpr_expression(a1, rhs, &right, NULL)) return 0;
        // Pointer comparison: validate same complete object
        if (is_pointer(left.type) && !same_complete_object(left, right)) {
            emit_error(2734);  // invalid pointer comparison
            return 0;
        }
        a3->int_val = apply_comparison(opcode, left, right);
        return 1;

    // ---- Compound assignment (+=, -=, etc.) ----
    case 74: case 75:
        // Evaluate LHS address, compute new value, store back
        ...

    // ---- Shift operators ----
    case 80: case 81: case 82: case 83:
        // Left shift, right shift (arithmetic and logical)
        ...

    // ---- Array subscript ----
    case 87: case 88: case 89: case 90: case 91:
        if (!do_constexpr_expression(a1, base, &arr, NULL)) return 0;
        if (!do_constexpr_expression(a1, index, &idx, NULL)) return 0;
        if (idx.int_val < 0 || idx.int_val >= array_dimension) {
            emit_error(2692);  // array bounds violation
            return 0;
        }
        a3->address = arr.address + idx.int_val * elem_size;
        return 1;

    // ---- Pointer-to-member dereference (.* and ->*) ----
    case 92: case 93:
        ...

    // ---- sizeof ----
    case 94:
        a3->int_val = compute_sizeof(operand_type);
        return 1;

    // ---- Comma operator ----
    case 103:
        do_constexpr_expression(a1, lhs, &discard, NULL);  // evaluate + discard
        return do_constexpr_expression(a1, rhs, a3, a4);   // return RHS

    default:
        emit_error(2721);  // not a constant expression
        return 0;
    }
}
```

### Type Conversion Sub-Switch (operator case 5)

Type conversions are one of the most complex parts of the evaluator. The sub-switch dispatches on source/target type pairs and handles overflow detection:

```c
int eval_conversion(interp_state *a1, expr_node *a2,
                    value_buf *a3, address_t *a4) {
    type_node *src_type = source_type(a2);
    type_node *dst_type = target_type(a2);
    int src_kind = src_type->kind;  // offset +132
    int dst_kind = dst_type->kind;

    // Evaluate the operand first
    value_buf operand;
    if (!do_constexpr_expression(a1, *(a2+48), &operand, NULL))
        return 0;

    // Dispatch on type pair
    if (src_kind == 2 && dst_kind == 2) {
        // int -> int: check truncation
        if (!fits_in_target(operand.int_val, dst_type)) {
            emit_error(2707);  // integer overflow in conversion
            return 0;
        }
        a3->int_val = truncate_to(operand.int_val, dst_type);
    }
    else if (src_kind == 2 && (dst_kind == 3 || dst_kind == 4)) {
        // int -> float/double
        a3->float_val = (double)operand.int_val;
    }
    else if ((src_kind == 3 || src_kind == 4) && dst_kind == 2) {
        // float/double -> int: check overflow
        if (operand.float_val > INT_MAX || operand.float_val < INT_MIN) {
            emit_error(2728);  // floating-point conversion overflow
            return 0;
        }
        a3->int_val = (int64_t)operand.float_val;
    }
    else if (src_kind == 1 && dst_kind == 2) {
        // pointer -> int (reinterpret_cast)
        if (!cuda_allows_reinterpret_cast()) {  // dword_106C2C0
            emit_error(2727);  // invalid conversion
            return 0;
        }
    }
    else if (src_kind == 6 && dst_kind == 6) {
        // class -> class (derived-to-base or base-to-derived)
        a3->address = adjust_pointer_for_base(operand.address, src_type, dst_type);
    }
    else if (src_kind == 19 && dst_kind == 1) {
        // nullptr_t -> pointer
        a3->address = 0;
        a3->flags |= 0;  // null pointer
    }
    // ... 15+ additional type pairs ...
    return 1;
}
```

### Variable Lookup (case 18)

When the evaluator encounters a variable reference, it looks up the variable's current value in the interpreter's hash table:

```c
int lookup_variable(interp_state *a1, expr_node *a2,
                    value_buf *a3, address_t *a4) {
    void *var_key = get_variable_entity(a2);
    uint64_t *table = a1->hash_table;       // offset +0
    uint64_t  mask  = a1->hash_capacity;    // offset +8, low 32 bits

    // Linear-probing hash lookup
    uint32_t idx = hash(var_key) & mask;
    while (table[idx * 2] != 0) {
        if (table[idx * 2] == var_key) {
            // Found: load value from stored address
            void *value_addr = table[idx * 2 + 1];
            load_value(a3, value_addr, get_type(a2));
            return 1;
        }
        idx = (idx + 1) & mask;
    }
    // Variable not in scope -> likely a static/global constexpr
    return resolve_static_ref(a1, a2, a3);
}
```

## Function Call Dispatch: do_constexpr_call

`sub_657560` (1,445 lines) handles all function call evaluation during constexpr. It is the central dispatcher that routes calls to the appropriate evaluator based on the callee kind.

```c
int do_constexpr_call(interp_state *a1, expr_node *call_expr,
                      value_buf *result, address_t *home) {
    // 1. Resolve the callee
    func_info callee;
    if (!eval_constexpr_callee(a1, call_expr, &callee))  // sub_643FE0
        return 0;

    // 2. Check recursion depth
    int depth = count_call_chain(a1->call_chain);
    if (depth > MAX_CONSTEXPR_DEPTH) {
        emit_error(2701);  // constexpr evaluation exceeded depth limit
        return 0;
    }

    // 3. Dispatch by callee kind
    if (callee.is_builtin) {
        // Route to builtin evaluator
        return do_constexpr_builtin_function(       // sub_651150
            a1, callee.descriptor, args, result, &success);
    }

    if (callee.is_constructor) {
        // Route to constructor evaluator
        return do_constexpr_ctor(a1, callee, args,  // sub_6480F0
                                 result, home);
    }

    if (callee.is_destructor) {
        // Route to destructor evaluator (two variants)
        return do_constexpr_dtor(a1, callee, args,  // sub_64EFE0 or sub_64FB10
                                 result);
    }

    if (callee.is_virtual) {
        // Virtual dispatch: resolve through vtable
        func_info resolved = resolve_virtual_call(callee, this_obj);
        if (!resolved.is_constexpr) {
            emit_error(269);  // virtual function is not constexpr
            return 0;
        }
        callee = resolved;
    }

    // 4. Check that function body is available
    if (!callee.has_body) {
        emit_error(2823);  // constexpr function not defined
        return 0;
    }

    // 5. Push call frame
    call_frame frame;
    frame.prev = a1->call_chain;
    a1->call_chain = &frame;
    frame.func = callee.entity;

    // 6. Bind arguments to parameters
    for (int i = 0; i < callee.param_count; i++) {
        value_buf arg_val;
        if (!do_constexpr_expression(a1, args[i], &arg_val, NULL))
            goto cleanup;
        bind_parameter(a1, callee.params[i], &arg_val);
    }

    // 7. Evaluate function body
    int ok = do_constexpr_statement(a1, callee.body, result);  // sub_647850

    // 8. Pop call frame, clean up allocations
cleanup:
    a1->call_chain = frame.prev;
    release_allocation_chain(a1, &frame);  // sub_633EC0
    return ok;
}
```

### Callee Resolution: eval_constexpr_callee

`sub_643FE0` (305 lines) resolves the callee expression of a function call. It handles direct calls, virtual dispatch (vtable lookup), and pointer-to-member-function calls. For virtual calls, it resolves overrides by walking the vtable of the most-derived type of the object being called on.

### Recursion Depth Tracking

The interpreter tracks call depth through the `call_chain` linked list at offset `+72` in the interpreter state. Each `do_constexpr_call` invocation pushes a frame; each return pops it. The chain is also used for diagnostic output — when a constexpr evaluation fails, the error message includes the call stack showing how the offending expression was reached.

## Constructor Evaluation: do_constexpr_ctor

`sub_6480F0` (1,659 lines) evaluates constructor calls during constexpr. It implements the full C++ construction sequence:

```c
int do_constexpr_ctor(interp_state *a1, func_info *ctor,
                      expr_node **args, value_buf *result,
                      address_t *target_addr) {
    class_type *cls = ctor->parent_class;

    // 1. Initialize virtual base classes (if most-derived)
    for (vbase in cls->virtual_bases) {
        address_t vbase_addr = target_addr + vbase.offset;
        if (vbase.has_initializer) {
            if (!do_constexpr_expression(a1, vbase.init, &val, &vbase_addr))
                return 0;
        } else {
            init_subobject_to_zero(vbase_addr, vbase.type);  // sub_62C030
        }
    }

    // 2. Initialize non-virtual base classes
    for (base in cls->bases) {
        address_t base_addr = target_addr + base.offset;
        if (base.has_ctor_call) {
            if (!do_constexpr_ctor(a1, base.ctor, base.args,
                                   &val, &base_addr))
                return 0;
        }
    }

    // 3. Initialize data members (in declaration order)
    for (member in cls->members) {
        address_t mem_addr = target_addr + member.offset;
        if (member.has_mem_initializer) {
            // From constructor's member-initializer-list
            if (!do_constexpr_expression(a1, member.init, &val, &mem_addr))
                return 0;
        } else if (member.has_default_initializer) {
            // From in-class default member initializer
            if (!do_constexpr_expression(a1, member.default_init,
                                         &val, &mem_addr))
                return 0;
        } else {
            // Default-initialize (zero for trivial types)
            init_subobject_to_zero(mem_addr, member.type);
        }
    }

    // 4. Execute constructor body (if non-trivial)
    if (ctor->has_body) {
        if (!do_constexpr_statement(a1, ctor->body, result))
            return 0;
    }

    // 5. Handle delegating constructors
    if (ctor->is_delegating) {
        return do_constexpr_ctor(a1, ctor->delegate_target,
                                 args, result, target_addr);
    }

    // 6. For trivial copy/move, use memcpy optimization
    if (ctor->is_trivial_copy) {
        copy_interpreter_subobject(target_addr, source_addr, cls);
        return 1;                                // sub_6337D0
    }

    return 1;
}
```

## Loop Evaluation: do_constexpr_range_based_for_statement

`sub_644580` (2,836 lines) evaluates all loop constructs during constexpr: `for`, `while`, `do-while`, and range-based `for`. It is self-recursive for nested loops.

```c
int do_constexpr_range_based_for_statement(
        interp_state *a1, stmt_node *loop, value_buf *result) {

    // --- Range-based for ---
    if (loop->kind == RANGE_FOR) {
        // 1. Evaluate range expression: auto&& __range = <expr>
        value_buf range_val;
        if (!do_constexpr_expression(a1, loop->range_expr, &range_val, NULL))
            return 0;

        // 2. Evaluate begin() and end()
        value_buf begin_val, end_val;
        if (!do_constexpr_call(a1, loop->begin_call, &begin_val, NULL))
            return 0;
        if (!do_constexpr_call(a1, loop->end_call, &end_val, NULL))
            return 0;

        // 3. Loop: while (begin != end)
        while (true) {
            // Evaluate condition: begin != end
            value_buf cond;
            if (!do_constexpr_expression(a1, loop->condition, &cond, NULL))
                return 0;
            if (!cond.int_val)
                break;  // loop finished

            // Bind loop variable: auto x = *begin
            value_buf elem;
            if (!do_constexpr_expression(a1, loop->deref_expr, &elem, NULL))
                return 0;
            bind_parameter(a1, loop->loop_var, &elem);

            // Execute loop body
            int body_result = do_constexpr_statement(  // sub_6593C0
                a1, loop->body, result);

            if (body_result == BREAK)   break;
            if (body_result == RETURN)  return body_result;
            // CONTINUE falls through to increment

            // Increment iterator: ++begin
            if (!do_constexpr_expression(a1, loop->increment, &begin_val, NULL))
                return 0;

            // Destroy loop variable for this iteration
            cleanup_iteration(a1, loop->loop_var);  // sub_658CE0
        }
        return 1;
    }

    // --- Traditional for/while/do-while ---
    if (loop->kind == FOR_LOOP) {
        // Initialize
        if (loop->init_stmt)
            do_constexpr_statement(a1, loop->init_stmt, NULL);

        while (true) {
            // Condition
            if (loop->condition) {
                value_buf cond;
                do_constexpr_expression(a1, loop->condition, &cond, NULL);
                if (!cond.int_val) break;
            }
            // Body
            int r = do_constexpr_statement(a1, loop->body, result);
            if (r == BREAK)  break;
            if (r == RETURN) return r;
            // Increment
            if (loop->increment)
                do_constexpr_expression(a1, loop->increment, NULL, NULL);
        }
    }
    return 1;
}
```

The loop body evaluation is delegated to `sub_6593C0` (816 lines), which handles per-iteration variable binding, break/continue/return propagation, and destruction of loop-scoped temporaries.

## Statement Evaluation: do_constexpr_statement

`sub_647850` (509 lines) evaluates compound statements, declarations, branches, and switch statements during constexpr:

```c
int do_constexpr_statement(interp_state *a1, stmt_node *stmt,
                           value_buf *result) {
    switch (stmt->kind) {
    case COMPOUND:
        // Push scope, evaluate each sub-statement, pop scope
        a1->scope_depth++;
        for (s in stmt->children) {
            int r = do_constexpr_statement(a1, s, result);
            if (r == RETURN || r == BREAK || r == CONTINUE)
                { a1->scope_depth--; return r; }
        }
        a1->scope_depth--;
        return OK;

    case DECLARATION:
        // Allocate interpreter storage, evaluate initializer
        return do_constexpr_init_variable(a1, stmt->decl, result);

    case IF_STMT:
        value_buf cond;
        do_constexpr_expression(a1, stmt->condition, &cond, NULL);
        if (cond.int_val)
            return do_constexpr_statement(a1, stmt->then_branch, result);
        else if (stmt->else_branch)
            return do_constexpr_statement(a1, stmt->else_branch, result);
        return OK;

    case SWITCH_STMT:
        value_buf switch_val;
        do_constexpr_expression(a1, stmt->condition, &switch_val, NULL);
        // Find matching case label
        case_label = find_case(stmt->cases, switch_val.int_val);
        return do_constexpr_statement(a1, case_label->body, result);

    case RETURN_STMT:
        if (stmt->return_expr)
            do_constexpr_expression(a1, stmt->return_expr, result, NULL);
        return RETURN;

    case FOR_STMT: case WHILE_STMT: case DO_STMT: case RANGE_FOR:
        return do_constexpr_range_based_for_statement(  // sub_644580
            a1, stmt, result);

    case BREAK_STMT:    return BREAK;
    case CONTINUE_STMT: return CONTINUE;

    case TRY_STMT:
        // try/catch in constexpr (C++26 direction, partially supported)
        ...
    }
}
```

## Builtin Function Evaluation: do_constexpr_builtin_function

`sub_651150` (5,032 lines) evaluates compiler intrinsics and `__builtin_*` functions at compile time. It dispatches on the builtin function ID (a 16-bit value at `*(a2+168)`), using a sparse comparison tree rather than a dense switch table.

```c
int do_constexpr_builtin_function(
        interp_state *a1,
        func_desc    *a2,       // function descriptor
        value_buf   **a3,       // argument array
        value_buf    *a4,       // result buffer
        int          *a5) {     // success/failure output

    uint16_t builtin_id = *(a2 + 168);

    // --- Arithmetic overflow detection ---
    // __builtin_add_overflow, __builtin_sub_overflow, __builtin_mul_overflow
    if (builtin_id == BUILTIN_ADD_OVERFLOW) {
        int64_t a = a3[0]->int_val, b = a3[1]->int_val;
        bool overflow;
        int64_t result = checked_add(a, b, &overflow);
        a3[2]->int_val = result;     // write to output parameter
        a4->int_val = overflow ? 1 : 0;
        return 1;
    }

    // --- Bit manipulation ---
    // __builtin_clz, __builtin_ctz, __builtin_popcount, __builtin_parity
    if (builtin_id == BUILTIN_CLZ) {
        uint64_t val = a3[0]->int_val;
        if (val == 0) { emit_error(61); return 0; }  // UB: clz(0)
        a4->int_val = __builtin_clzll(val);
        return 1;
    }
    if (builtin_id == BUILTIN_POPCOUNT) {
        a4->int_val = __builtin_popcountll(a3[0]->int_val);
        return 1;
    }
    if (builtin_id == BUILTIN_BSWAP32) {
        a4->int_val = __builtin_bswap32((uint32_t)a3[0]->int_val);
        return 1;
    }

    // --- String operations ---
    // __builtin_strlen, __builtin_strcmp, __builtin_memcmp,
    // __builtin_strchr, __builtin_memchr
    if (builtin_id == BUILTIN_STRLEN) {
        char *str = get_interpreter_string(a1, a3[0]);
        a4->int_val = strlen(str);
        return 1;
    }
    if (builtin_id == BUILTIN_STRCMP) {
        char *s1 = get_interpreter_string(a1, a3[0]);
        char *s2 = get_interpreter_string(a1, a3[1]);
        a4->int_val = strcmp(s1, s2);
        return 1;
    }

    // --- Floating-point classification ---
    // __builtin_isnan, __builtin_isinf, __builtin_isfinite,
    // __builtin_fpclassify, __builtin_huge_val, __builtin_nan
    if (builtin_id == BUILTIN_ISNAN) {
        a4->int_val = isnan(a3[0]->float_val) ? 1 : 0;
        return 1;
    }
    if (builtin_id == BUILTIN_NAN) {
        char *tag = get_interpreter_string(a1, a3[0]);
        a4->float_val = nan(tag);
        return 1;
    }

    // --- C++20/23 bit operations ---
    // std::bit_cast (via __builtin_bit_cast)
    if (builtin_id == BUILTIN_BIT_CAST) {
        // Serialize source object to target-format bytes
        translate_interpreter_object_to_target_bytes(  // sub_62A490
            a1, a3[0], byte_buffer);
        // Deserialize into destination type
        translate_target_bytes_to_interpreter_object(  // sub_62C670
            a1, byte_buffer, a4, dst_type);
        return 1;
    }

    // --- Type traits ---
    // __is_constant_evaluated()
    if (builtin_id == BUILTIN_IS_CONSTANT_EVALUATED) {
        a4->int_val = 1;  // always true inside constexpr evaluator
        return 1;
    }

    // --- Memory operations ---
    // __builtin_memcpy, __builtin_memmove
    if (builtin_id == BUILTIN_MEMCPY) {
        // Copy N bytes between interpreter objects
        copy_interpreter_bytes(a3[0]->address, a3[1]->address,
                               a3[2]->int_val);
        *a4 = *a3[0];  // return dest pointer
        return 1;
    }

    // ... 50+ additional builtin categories ...

    emit_error(2721);  // builtin not evaluable at compile time
    return 0;
}
```

### Builtin Categories Summary

| Category | Examples | Count |
|---|---|---|
| Arithmetic overflow | `__builtin_add_overflow`, `__builtin_mul_overflow` | 3 |
| Bit manipulation | `__builtin_clz`, `__builtin_ctz`, `__builtin_popcount`, `__builtin_bswap` | 8+ |
| String operations | `__builtin_strlen`, `__builtin_strcmp`, `__builtin_memcmp`, `__builtin_strchr` | 6+ |
| Math/FP classify | `__builtin_isnan`, `__builtin_isinf`, `__builtin_huge_val`, `__builtin_nan` | 8+ |
| Type queries | `__is_constant_evaluated`, `__has_unique_object_representations` | 4+ |
| Memory operations | `__builtin_memcpy`, `__builtin_memmove` | 3+ |
| C++20/23 `<bit>` | `std::bit_cast`, `std::bit_ceil`, `std::bit_floor`, `std::countl_zero` | 8+ |
| Atomic (limited) | Constexpr-evaluable atomic subset | 2+ |

## Destructor Evaluation

Two functions handle constexpr destructor calls, splitting responsibilities:

**`do_constexpr_dtor` variant 1** (`sub_64EFE0`, 503 lines) — Evaluates the destructor body itself. Runs the user-written destructor code, then destroys members in reverse declaration order.

**`do_constexpr_dtor` variant 2 / `perform_destructions`** (`sub_64FB10`, 877 lines) — Handles the full destruction sequence including base class destructors and array element destruction. Also implements `perform_destructions`, the post-evaluation cleanup that destroys all constexpr-created objects when their scope ends.

## Materialization: Interpreter Objects to IL Constants

After constexpr evaluation completes, the interpreter's internal objects must be converted back into IL constant nodes that the rest of the compiler can consume.

### copy_interpreter_object_to_constant

`sub_631110` (1,444 lines) traverses the interpreter's memory representation of an object and builds the corresponding IL constant tree:

```c
il_node *copy_interpreter_object_to_constant(
        interp_state *a1, address_t obj_addr, type_node *type) {
    int kind = type->kind;

    switch (kind) {
    case 2: case 13:  // integer, enum
        return make_integer_constant(load_int(obj_addr), type);

    case 3: case 4:   // float, double
        return make_float_constant(load_float(obj_addr), type);

    case 1:           // pointer
        if (is_null_pointer(obj_addr))
            return make_null_pointer_constant(type);
        // Non-null: build address expression with relocation
        return make_address_constant(
            translate_interpreter_offset(obj_addr),  // inline helper
            type);

    case 6: case 9: case 10: case 11:  // class/struct/union
        il_node *result = make_aggregate_constant(type);
        // Recursively convert each member
        for (member in get_members(type)) {
            address_t mem_addr = obj_addr + member.offset;
            il_node *mem_val = copy_interpreter_object_to_constant(
                a1, mem_addr, member.type);
            add_member_to_aggregate(result, mem_val);
        }
        return result;

    case 8:           // array
        il_node *result = make_array_constant(type);
        for (int i = 0; i < array_dimension(type); i++) {
            address_t elem_addr = obj_addr + i * elem_size;
            il_node *elem = copy_interpreter_object_to_constant(
                a1, elem_addr, elem_type);
            add_element_to_array(result, elem);
        }
        return result;
    }
}
```

This function also contains `get_reflection_string_entry` and `translate_interpreter_offset` as inlined helpers — the former handles C++26 reflection string extraction, and the latter converts interpreter memory addresses into IL address expressions with proper relocations.

### extract_value_from_constant (reverse direction)

`sub_64B580` (2,299 lines) performs the inverse: given an IL constant node (from a previously evaluated constexpr), it extracts the value into the interpreter's internal representation. This is used when a constexpr function references another constexpr variable whose value was already computed.

## `__builtin_bit_cast` Support

Two functions implement the byte-level serialization needed for `std::bit_cast`:

**`translate_interpreter_object_to_target_bytes`** (`sub_62A490`, 461 lines) — Serializes an interpreter object to a target-format byte sequence. Must handle endianness conversion, padding bytes, and bitfield layout according to the target ABI.

**`translate_target_bytes_to_interpreter_object`** (`sub_62C670`, 529 lines) — Deserializes target-format bytes back into an interpreter object. Validates that the source bytes represent a valid value for the destination type (e.g., no trap representations for `bool`).

## C++20 Constexpr Memory Support

### std::allocator<T>::allocate

`sub_62B100` (`do_constexpr_std_allocator_allocate`, 177 lines) — Handles `new` expressions in constexpr context. Allocates from the interpreter arena, sets the allocation-chain flag (bit 2), and links the allocation into the tracking chain.

### std::allocator<T>::deallocate

`sub_62B470` (`do_constexpr_std_allocator_deallocate`, 195 lines) — Handles `delete` in constexpr context. Validates the pointer was allocated by `std::allocator::allocate()` by searching the allocation chain (`qword_126FBC0` / `qword_126FBB8`).

### std::construct_at

`sub_64F920` (`do_constexpr_std_construct_at`, 108 lines) — Handles `std::construct_at()` (C++20). Validates the target pointer, then delegates to `do_constexpr_ctor` for actual construction.

## C++26 Reflection Support

EDG 6.6 includes experimental support for the P2996 compile-time reflection proposal. Eight dedicated functions implement `std::meta::*` operations:

| Function | Address | Lines | Reflection operation |
|---|---|---|---|
| `do_constexpr_std_meta_substitute` | `sub_628510` | 526 | `std::meta::substitute()` — template argument substitution |
| `do_constexpr_std_meta_enumerators_of` | `sub_62EB00` | 342 | `std::meta::enumerators_of()` — enum value list |
| `do_constexpr_std_meta_subobjects_of` | `sub_62F0B0` | 434 | `std::meta::subobjects_of()` — all subobjects |
| `do_constexpr_std_meta_bases_of` | `sub_62F7B0` | 339 | `std::meta::bases_of()` — base class list |
| `do_constexpr_std_meta_nonstatic_data_members_of` | `sub_62FD30` | 308 | `std::meta::nonstatic_data_members_of()` |
| `do_constexpr_std_meta_static_data_members_of` | `sub_630280` | 308 | `std::meta::static_data_members_of()` |
| `do_constexpr_std_meta_members_of` | `sub_6307E0` | 590 | `std::meta::members_of()` — all members |
| `do_constexpr_std_meta_define_class` | `sub_65DE10` | 553 | `std::meta::define_class()` — class synthesis |

These functions operate on "infovecs" — information vectors created by `make_infovec` (`sub_62E1B0`, 241 lines) that encode reflection metadata as interpreter-internal objects. The `get_interpreter_string` and `get_interpreter_string_length` helpers (also within `sub_65DE10`) extract string values from these infovecs for operations that take string parameters (member names, type names).

The `define_class` operation is particularly notable: it allows constexpr code to synthesize entirely new class types at compile time, a capability that goes beyond simple introspection.

## CUDA-Specific Constexpr Behavior

The interpreter checks several global flags to relax standard constexpr restrictions for CUDA device code:

| Global | Purpose |
|---|---|
| `dword_106C2C0` | Controls `reinterpret_cast` semantics in device constexpr |
| `dword_106C1D8` | Controls pointer dereference behavior (likely `--expt-relaxed-constexpr`) |
| `dword_106C1E0` | Controls `typeid` availability in device constexpr |
| `dword_126EFAC` | CUDA mode flag (enables/disables constexpr relaxations) |
| `dword_126EFA4` | Secondary CUDA mode flag (combined with EFAC for fine control) |

Standard C++ forbids `reinterpret_cast`, `typeid`, and certain pointer operations in constexpr contexts. CUDA relaxes these restrictions because GPU programming patterns frequently require type punning and address manipulation that the standard deems non-constant. When these flags are set, the interpreter suppresses the corresponding error codes and evaluates the expression as if it were permitted.

### Language Version Gates

| Global | Check | Meaning |
|---|---|---|
| `qword_126EF98` | `> 0x222DF` (140,255) | C++20 features enabled (standard 202002) |
| `qword_126EF98` | `> 0x15F8F` (89,999) | C++14 features enabled (standard 201402) |
| `dword_126EFB4` | `== 2` | Full C++20+ compilation mode |
| `dword_126EF68` | `>= 202001` | C++20 constexpr dynamic allocation enabled |

These version checks gate features like constexpr `new`/`delete` (C++20), constexpr `dynamic_cast` and `typeid` (C++20), and constexpr virtual dispatch (C++20).

## Error Codes

The interpreter emits detailed diagnostics when constexpr evaluation fails. Each error code identifies a specific category of failure:

| Error | Meaning |
|---|---|
| 61 | Undefined behavior detected (division by zero, `clz(0)`, etc.) |
| 269 | Virtual function called is not constexpr |
| 286 | Pure virtual function called |
| 2691 | Invalid pointer comparison direction |
| 2692 | Array bounds violation |
| 2700 | Access to uninitialized object |
| 2701 | Constexpr evaluation exceeded depth limit |
| 2707 | Integer overflow in type conversion |
| 2708 | Arithmetic overflow in computation |
| 2721 | Expression is not a constant expression |
| 2725 | Type too large for constexpr evaluation (> 64MB) |
| 2727 | Invalid type conversion in constexpr |
| 2728 | Floating-point conversion overflow |
| 2734 | Invalid pointer comparison (different complete objects) |
| 2735 | Pointer arithmetic out of bounds |
| 2751 | Null pointer dereference |
| 2760 | Pointer-to-member dereference failure |
| 2766 | Null pointer arithmetic |
| 2808 | Class too large for constexpr representation |
| 2823 | Constexpr function body not available |
| 2879 | `offsetof` on invalid member |
| 2921 | Direct value return failure |
| 2938 | Virtual base class offset not found |
| 2955 | Statement expression evaluation failure |
| 2993 | Object lifetime violation |
| 2999 | Variable-length array in constexpr |
| 3007 | Pointer-to-member comparison failure |
| 3024 | Dynamic initialization order issue |
| 3248 | Member access on uninitialized object |
| 3312 | Object representation mismatch (`bit_cast`) |

## Supporting Functions

### Value Management

| Function | Address | Lines | Purpose |
|---|---|---|---|
| `f_value_bytes_for_type` | `sub_628DE0` | 843 | Compute interpreter storage size for a type |
| `init_subobject_to_zero` | `sub_62C030` | 284 | Zero-initialize a constexpr subobject |
| `mark_mutable_members_not_initialized` | `sub_62D0F0` | 203 | Mark mutable members after copy |
| Copy scalar value | `sub_62B8A0` | 61 | Assign scalar value to interpreter object |
| Load value | `sub_64EA30` | 293 | Load value from interpreter object into buffer |
| Check initialized | `sub_62BF60` | 55 | Validate interpreter object is initialized |

### Object Addressing

| Function | Address | Lines | Purpose |
|---|---|---|---|
| `find_subobject_for_interpreter_address` | `sub_629D30` | 334 | Map address to subobject identity |
| `obj_type_at_address` | `sub_62A210` | 133 | Most-derived type at an address |
| `get_runtime_array_pos` | `sub_6341C0` | 224 | Array element index for a pointer |
| `last_subobject_path_link` | `sub_6345D0` | 21 | Tail of subobject path chain |
| `get_trailing_subobject_path_entry` | `sub_634630` | 82 | Trailing subobject for virtual bases |
| Copy subobject | `sub_6337D0` | 379 | Copy subobject between interpreter addresses |
| Validate subobject path | `sub_62B980` | 314 | Recursive validation of class hierarchy traversal |

### Condition and Allocation

| Function | Address | Lines | Purpose |
|---|---|---|---|
| `do_constexpr_condition` | `sub_658EE0` | 302 | Evaluate if/while/for condition |
| `do_constexpr_condition_alloc` | `sub_62D810` | 187 | Allocate storage for condition result |
| `do_constexpr_init_variable` | `sub_6509E0` | 427 | Initialize local variable in constexpr |
| Allocate value slot | `sub_62D4F0` | 183 | Allocate and init a value slot in arena |
| Release allocation chain | `sub_633EC0` | 157 | Free tracked constexpr allocations |

### Dynamic Initialization and Lambdas

| Function | Address | Lines | Purpose |
|---|---|---|---|
| `do_constexpr_dynamic_init` | `sub_64A040` | 1,111 | Dynamic initialization of constexpr variables |
| `do_constexpr_lambda` | (within `sub_64A040`) | — | Lambda capture evaluation |
| `do_array_constructor_copy` | (within `sub_64A040`) | — | Array construction via copy ctor |

### Debug and Diagnostics

| Function | Address | Lines | Purpose |
|---|---|---|---|
| Format constexpr value | `sub_632E80` | 268 | Format value for error messages |
| Dump constexpr value | `sub_6333E0` | 166 | `fprintf`-based debug dump |

## Complete Function Map

| Address | Lines | Identity | Confidence |
|---|---|---|---|
| `sub_628180` | 237 | Init/entry wrapper | MEDIUM |
| `sub_628510` | 526 | `do_constexpr_std_meta_substitute` | HIGH (95%) |
| `sub_628DE0` | 843 | `f_value_bytes_for_type` | VERY HIGH (99%) |
| `sub_629D30` | 334 | `find_subobject_for_interpreter_address` | VERY HIGH (99%) |
| `sub_62A210` | 133 | `obj_type_at_address` | VERY HIGH (99%) |
| `sub_62A490` | 461 | `translate_interpreter_object_to_target_bytes` | VERY HIGH (99%) |
| `sub_62AD90` | 194 | Allocate interpreter value storage | HIGH (85%) |
| `sub_62B100` | 177 | `do_constexpr_std_allocator_allocate` | VERY HIGH (99%) |
| `sub_62B470` | 195 | `do_constexpr_std_allocator_deallocate` | VERY HIGH (99%) |
| `sub_62B8A0` | 61 | Copy scalar value | HIGH (85%) |
| `sub_62B980` | 314 | Validate/traverse subobject path | HIGH (80%) |
| `sub_62BF60` | 55 | Validate initialization state | HIGH (85%) |
| `sub_62C030` | 284 | `init_subobject_to_zero` | VERY HIGH (99%) |
| `sub_62C670` | 529 | `translate_target_bytes_to_interpreter_object` | VERY HIGH (99%) |
| `sub_62D0F0` | 203 | `mark_mutable_members_not_initialized` | VERY HIGH (99%) |
| `sub_62D4F0` | 183 | Allocate constexpr value slot | HIGH (80%) |
| `sub_62D810` | 187 | `do_constexpr_condition_alloc` | VERY HIGH (99%) |
| `sub_62DB00` | 132 | Get value type size (wrapper) | HIGH (80%) |
| `sub_62DD10` | 242 | Builtin dispatch helper | MEDIUM (70%) |
| `sub_62E1B0` | 241 | `make_infovec` | VERY HIGH (99%) |
| `sub_62E670` | 276 | Init/entry wrapper | MEDIUM (60%) |
| `sub_62EB00` | 342 | `do_constexpr_std_meta_enumerators_of` | VERY HIGH (99%) |
| `sub_62F0B0` | 434 | `do_constexpr_std_meta_subobjects_of` | VERY HIGH (99%) |
| `sub_62F7B0` | 339 | `do_constexpr_std_meta_bases_of` | VERY HIGH (99%) |
| `sub_62FD30` | 308 | `do_constexpr_std_meta_nonstatic_data_members_of` | VERY HIGH (99%) |
| `sub_630280` | 308 | `do_constexpr_std_meta_static_data_members_of` | VERY HIGH (99%) |
| `sub_6307E0` | 590 | `do_constexpr_std_meta_members_of` | VERY HIGH (99%) |
| `sub_631110` | 1,444 | `copy_interpreter_object_to_constant` | VERY HIGH (99%) |
| `sub_632CB0` | 36 | Create reflection string object | MEDIUM (70%) |
| `sub_632D80` | 64 | `get_reflection_string_entry` helper | HIGH (85%) |
| `sub_632E80` | 268 | Format constexpr value for diagnostics | MEDIUM (65%) |
| `sub_6333E0` | 166 | Dump constexpr value (debug) | MEDIUM (65%) |
| `sub_6337D0` | 379 | Copy interpreter subobject | HIGH (85%) |
| `sub_633EC0` | 157 | Release allocation chain | HIGH (80%) |
| `sub_6341C0` | 224 | `get_runtime_array_pos` | VERY HIGH (99%) |
| `sub_6345D0` | 21 | `last_subobject_path_link` | VERY HIGH (99%) |
| `sub_634630` | 82 | `get_trailing_subobject_path_entry` | VERY HIGH (99%) |
| **`sub_634740`** | **11,205** | **`do_constexpr_expression`** | **ABSOLUTE (100%)** |
| `sub_643C50` | 202 | Prepare constexpr callee | HIGH (85%) |
| `sub_643FE0` | 305 | `eval_constexpr_callee` | VERY HIGH (99%) |
| `sub_644580` | 2,836 | `do_constexpr_range_based_for_statement` | VERY HIGH (99%) |
| `sub_647850` | 509 | `do_constexpr_statement` | HIGH (90%) |
| `sub_6480F0` | 1,659 | `do_constexpr_ctor` | VERY HIGH (99%) |
| `sub_64A040` | 1,111 | `do_constexpr_dynamic_init` / `do_constexpr_lambda` | VERY HIGH (99%) |
| `sub_64B580` | 2,299 | `extract_value_from_constant` | VERY HIGH (99%) |
| `sub_64DFA0` | 86 | Destructor chain walker | HIGH (80%) |
| `sub_64E170` | 404 | Perform destruction sequence | HIGH (85%) |
| `sub_64E9E0` | 26 | Predicate / flag check | MEDIUM (65%) |
| `sub_64EA30` | 293 | Load value from interpreter object | HIGH (85%) |
| `sub_64EFE0` | 503 | `do_constexpr_dtor` (variant 1) | VERY HIGH (99%) |
| `sub_64F8F0` | 14 | Trivial forwarding wrapper | MEDIUM (60%) |
| `sub_64F920` | 108 | `do_constexpr_std_construct_at` | VERY HIGH (99%) |
| `sub_64FB10` | 877 | `do_constexpr_dtor` (v2) / `perform_destructions` | VERY HIGH (99%) |
| `sub_6509E0` | 427 | `do_constexpr_init_variable` | VERY HIGH (99%) |
| **`sub_651150`** | **5,032** | **`do_constexpr_builtin_function`** | **ABSOLUTE (100%)** |
| **`sub_657560`** | **1,445** | **`do_constexpr_call`** | **VERY HIGH (99%)** |
| `sub_658CE0` | 134 | Loop iteration cleanup | HIGH (80%) |
| `sub_658EE0` | 302 | `do_constexpr_condition` | VERY HIGH (99%) |
| `sub_6593C0` | 816 | Loop body evaluator | HIGH (85%) |
| `sub_65A290` | 311 | Entry from expression lowering | MEDIUM (70%) |
| `sub_65A8C0` | 274 | Entry from expression trees | MEDIUM (70%) |
| `sub_65AE50` | 572 | `interpret_expr` (primary entry) | VERY HIGH (99%) |
| `sub_65BAB0`--`sub_65D150` | 150-470 | Misc entry points | MEDIUM (70%) |
| `sub_65CFA0` | 67 | `interpret_dynamic_sub_initializers` | VERY HIGH (99%) |
| `sub_65D9A0`--`sub_65DD20` | 7-68 | Small utility/accessor functions | MEDIUM (65%) |
| `sub_65DE10` | 553 | `do_constexpr_std_meta_define_class` | VERY HIGH (99%) |

## Cross-References

- [EDG 6.6 Overview](overview.md) — Position of `interpret.c` in the source tree
- [Type System](type-system.md) — The 22 type kinds that the interpreter evaluates
- [Template Engine](template-engine.md) — Constexpr evaluation during template instantiation
- [IL Overview](../il/overview.md) — IL constant nodes that materialization produces
- [Diagnostics Overview](../diagnostics/overview.md) — Error message system for constexpr failures
- [Pipeline Overview](../pipeline/overview.md) — Where constexpr evaluation sits in the compilation pipeline
