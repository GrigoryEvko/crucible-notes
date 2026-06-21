# IL Comparison & Deep Copy

The IL comparison and deep copy engines are two tightly coupled subsystems of the EDG 6.6 IL core that serve template instantiation, constant sharing, and overload resolution. The comparison engine determines structural equivalence between two IL expression trees or constant nodes — needed when the compiler must decide whether two template arguments are "the same" or whether a constant has already been allocated. The deep copy engine clones expression trees while optionally substituting template parameters for their actual arguments — the core mechanism behind template instantiation. Both subsystems are recursive tree walkers dispatched by node-kind switches, and both operate on the same IL node layout described in [IL Overview](overview.md).

These two engines share the address range `0x5D0750`--`0x5DFAD0` in the binary (roughly 37KB of compiled code). The comparison engine occupies `0x5D0750`--`0x5D2160`, constant sharing infrastructure sits at `0x5D2170`--`0x5D2D80`, the expression copy engine fills `0x5D2DE0`--`0x5D5550`, and the template parameter substitution dispatcher extends from `0x5DC000`--`0x5DFAD0`.

## Key Facts

| Property | Value |
|---|---|
| Subsystem | IL core (EDG 6.6) |
| Comparison engine | `sub_5D0750` (`compare_expressions`), 588 lines |
| Constant comparison | `sub_5D1350` (`compare_constants`), 525 lines |
| Dynamic init comparison | `sub_5D1FE0` (`compare_dynamic_inits`), ~80 lines |
| Constant sharing allocator | `sub_5D2390` (`alloc_shareable_constant`), ~200 lines |
| Expression tree copier | `sub_5D2F90` (`i_copy_expr_tree`), 424 lines |
| Constant deep copier | `sub_5D3B90` (`i_copy_constant_full`), 305 lines |
| Template substitution dispatcher | `sub_5DC000` (`copy_template_param_expr`), 1416 lines |
| Template constant dispatcher | `sub_5DE290` (`copy_template_param_con`), 819 lines |
| Constant sharing hash buckets | 2039 |
| Recursion depth guard | `dword_126F1D0` (incremented/decremented around `compare_expressions`) |

---

## Part 1: The Comparison Engine

### Why It Exists

Three front-end subsystems need structural equality testing on IL trees:

1. **Template argument deduction.** When the compiler deduces template arguments from a function call, it must compare the deduced value against a previously deduced value for the same parameter. Two independently constructed expression trees representing `sizeof(int)` must compare as equal even though they are distinct heap allocations.

2. **Constant sharing.** Identical constants across the translation unit are deduplicated into a single canonical node in file-scope memory. The comparison engine is the hash table's equality predicate — after two constants hash to the same bucket, `compare_constants` determines whether they are structurally identical.

3. **Overload resolution.** When the compiler checks whether two function template specializations have equivalent signatures, it compares their template argument expressions for equivalence.

### compare_expressions (sub_5D0750)

This is the main entry point. It takes two expression-node pointers and a flags word, and returns 1 (match) or 0 (mismatch). It uses a 36-case switch on the expression-node kind byte (offset +24 in the node layout).

```python
compare_expressions(expr_a, expr_b, flags) -> bool:

    if expr_a == expr_b:
        return TRUE                          // pointer identity short-circuit

    if expr_a->kind != expr_b->kind:
        return FALSE                         // different node types never match

    recursion_depth++                        // dword_126F1D0

    switch expr_a->kind:

        case 0 (null):
            result = FALSE                   // two null nodes are never "equal"

        case 1 (operation):
            if expr_a->op_code != expr_b->op_code:
                result = FALSE
            else:
                // compare each operand in the linked list pairwise
                result = compare_operand_lists(expr_a->operands, expr_b->operands, flags)
                if result:
                    result = equiv_types(expr_a->result_type, expr_b->result_type)

        case 2 (constant reference):
            result = compare_constants(expr_a->constant, expr_b->constant, flags)

        case 3 (entity reference):
            // first try pointer equality on the referenced entity
            if expr_a->entity == expr_b->entity:
                result = TRUE
            elif sharing_enabled and same_sharing_symbol(expr_a, expr_b):
                result = TRUE
            else:
                // deep entity comparison via equiv_types + compare_template_variables
                result = equiv_types(expr_a->entity->type, expr_b->entity->type)

        case 4, 19 (type reference):
            result = (expr_a->type_ptr == expr_b->type_ptr)
                  or equiv_types(expr_a->type_ptr, expr_b->type_ptr)

        case 5, 18 (dynamic init):
            result = compare_dynamic_inits(expr_a->init, expr_b->init, flags)

        case 6 (source position):
            result = (expr_a->offset == expr_b->offset)

        case 7 (full expression info):
            result = compare_flags(expr_a, expr_b)
                 and equiv_types(...)
                 and compare_expressions(expr_a->sub_expr, expr_b->sub_expr, flags)

        case 8 (template arguments):
            // element-by-element comparison of arg lists
            result = compare_template_arg_lists(expr_a->args, expr_b->args, flags)

        case 10, 33 (sub-expression wrapper):
            result = compare_expressions(expr_a->inner, expr_b->inner, flags)

        case 11, 32 (unary with boolean):
            result = (expr_a->bool_field == expr_b->bool_field)
                 and compare_expressions(expr_a->inner, expr_b->inner, flags)

        case 12, 14, 15 (typed value):
            result = (expr_a->value_byte == expr_b->value_byte)
                 and compare_type_or_value(...)

        case 13 (two-byte key):
            result = (expr_a->key_word == expr_b->key_word)

        case 16 (always-equal sentinel):
            result = TRUE

        case 17, 22, 35 (opaque pointer):
            result = (expr_a->ptr == expr_b->ptr)

        case 20 (pointer with fallback):
            result = (expr_a->ptr == expr_b->ptr)
                  or deep_compare_via_sub_7B2260(...)

        case 21 (keyed sub-expression):
            result = (expr_a->key == expr_b->key)
                 and compare_expressions(expr_a->inner, expr_b->inner, flags)

        case 23 (simple sub-expression):
            result = compare_expressions(expr_a->inner, expr_b->inner, flags)

        case 24 (nested expression pair):
            result = compare_pair(expr_a, expr_b, flags)

        case 25 (lambda/closure):
            result = chase_closure_ptrs_and_compare(...)

        case 28 (attributed expression):
            result = (expr_a->attr_flags == expr_b->attr_flags)
                 and compare_expressions(expr_a->inner, expr_b->inner, flags)

        case 30 (template specialization):
            if expr_a->hash != expr_b->hash:
                result = FALSE
            else:
                result = compare_template_specializations(expr_a, expr_b)

        case 31 (function template args):
            result = compare_each_arg_type(expr_a->args, expr_b->args)

        default:
            internal_error("compare_expressions: bad expr kind")

    recursion_depth--
    return result
```

**Flags interpretation.** The third argument `flags` is a bitmask:

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `0x01` | Strict mode — entity references must be pointer-identical, not just structurally equivalent |
| 1 | `0x02` | Check constraints — compare template constraints alongside types |
| 2 | `0x04` | Allow specialization — used by the equivalence wrapper (`sub_5D1320`) when comparing for specialization matching |

**Recursion depth guard.** The global `dword_126F1D0` is incremented on entry and decremented on exit. This counter is not used for depth limiting — it exists so that diagnostic routines (guarded by `dword_126EFC8`) can print indented traces via `sub_5C4B70` (`dump_expr_tree`).

### compare_constants (sub_5D1350)

Constants are the most structurally complex IL nodes. A single constant node is 184 bytes and carries a `constant_kind` byte at offset +148 that selects among 16 primary kinds, some of which contain nested sub-kinds. The comparison function uses an outer switch on `constant_kind` and inner switches for aggregate and template-parameter sub-kinds.

```python
compare_constants(const_a, const_b, flags) -> bool:

    if const_a == const_b:
        return TRUE

    if const_a->constant_kind != const_b->constant_kind:
        return FALSE

    switch const_a->constant_kind:

        case 0, 14 (trivial kinds):
            return TRUE

        case 1 (integer):
            return compare_integer_values(const_a->value, const_b->value)
               and (const_a->flags == const_b->flags)

        case 2 (string literal):
            return memcmp(const_a->bytes, const_b->bytes, const_a->length) == 0

        case 3, 5 (float):
            return compare_float_value(const_a->value, const_b->value)

        case 4 (complex):
            return compare_float_value(const_a->real, const_b->real)
               and compare_float_value(const_a->imag, const_b->imag)

        case 6 (address constant):
            // nested switch on address_kind (offset +152), 6 sub-kinds:
            switch const_a->address_kind:
                case 0, 1: pointer equality or compare_entities(...)
                case 2:    recursive type comparison
                case 3, 6: pointer equality at offset +160
                case 5:    type comparison via deep_compare
            // uses while(2) loop for manual tail-call optimization
            // on case 2 and case 13

        case 7 (template argument):
            return compare_template_arg(...)

        case 8 (pair of constants):
            return compare_constants(const_a->first, const_b->first, flags)
               and compare_constants(const_a->second, const_b->second, flags)

        case 9 (dynamic init):
            return compare_dynamic_inits(const_a->init, const_b->init, flags)

        case 10 (aggregate):
            // walk linked lists of sub-constants in lockstep
            a_elem = const_a->first_element
            b_elem = const_b->first_element
            while a_elem and b_elem:
                if not compare_constants(a_elem, b_elem, flags): return FALSE
                a_elem = a_elem->next; b_elem = b_elem->next
            return (a_elem == NULL and b_elem == NULL)

        case 11 (constant + scope):
            return compare_constants(const_a->sub, const_b->sub, flags)
               and (const_a->scope_byte == const_b->scope_byte)

        case 12 (template parameter constant):
            // deeply nested -- 14 sub-kinds at offset +152:
            switch const_a->template_param_kind:
                case 0:  pack parameter comparison
                case 1:  compare_expressions on embedded expr
                case 2:  compare_types via sub_5B3080
                case 3:  compare_types + type + flags
                case 4, 12: recursive compare_constants
                case 5-10: type equality + sub_5BFB80 comparison
                case 11: type + template argument list
                case 13: type equality only

        case 13 (entity ref constant):
            return (const_a->flags == const_b->flags)
               and (pointer_equal_or_sharing_match(const_a->entity, const_b->entity))

        case 15 (literal value):
            return (const_a->value_ptr == const_b->value_ptr)

        default:
            internal_error("compare_constants: bad constant kind")
```

**Manual tail-call optimization.** For cases 6 (address constant with type sub-kind) and 13 (entity ref), the function uses `while(2)` (an infinite loop that reassigns the operands and continues from the top of the comparison) instead of making a recursive call. This avoids stack growth when comparing chains of address constants, which can be deeply nested in pointer-to-member types.

### compare_dynamic_inits (sub_5D1FE0)

Dynamic initializers represent runtime initialization expressions (constructors, aggregate init, etc.). The comparison function dispatches on the init kind byte at offset +48:

```python
compare_dynamic_inits(init_a, init_b, flags) -> bool:

    if init_a->kind != init_b->kind:
        return FALSE
    if init_a->flags != init_b->flags:
        return FALSE
    // entity fields at +8, +16 compared with sharing-aware equality

    switch init_a->kind:
        case 0, 1: return TRUE (after header match)
        case 2, 6: return compare_constants(init_a->constant, init_b->constant)
        case 3, 4: return compare_expressions(init_a->expr, init_b->expr)
        case 5:    return compare_entity_ref(...)
                       and compare_sub_exprs(...)
```

---

## Part 2: Constant Sharing

### Why It Exists

Without deduplication, every occurrence of the integer constant `42` in a translation unit would allocate a separate 184-byte constant node in the IL. For large programs (especially heavy template users), this wastes significant memory. The constant sharing system maintains a hash table of canonical constants: when a new constant is about to be allocated, `alloc_shareable_constant` first checks whether an identical constant already exists in the hash table. If so, the existing node is returned; if not, a new canonical copy is created in the file-scope region and inserted into the table.

### Shareability Predicate

Not all constants can be shared. The predicate `constant_is_shareable` (`sub_5D2210`) checks several blocking conditions:

```python
constant_is_shareable(constant) -> bool:

    if not sharing_enabled (dword_126EE48):
        return FALSE

    if constant has parent:
        // parent must be type 2 (constant); checks sharing flag 0x40 at byte+81
        // and calls compare_constants on the parent's value
        return parent_is_shareable(...)

    // blocking conditions for parentless constants:
    if constant->associated_entry != NULL:  return FALSE   // already bound to an entry
    if constant->extra_data != 0:           return FALSE   // has auxiliary data
    if constant->flags & 0x02:              return FALSE   // flag bit 1 blocks sharing

    switch constant->constant_kind:
        case 2 (string):    return string_sharing_enabled (dword_126E1C0)
        case 6 (address):   return TRUE unless has extra payload at +176
                             or address_subkind==4 with data
        case 7 (template):  return (constant->extra_ptr == NULL)
        case 10 (aggregate): return FALSE   // aggregates never shared
        case 12 (template param): return FALSE   // template params never shared
        default:            return TRUE
```

The rationale for excluding aggregates and template parameters: aggregate constants contain linked lists of sub-constants that would require recursive sharing checks, and template parameter constants are inherently unique to their instantiation context.

### Hash Table Structure

The hash table is allocated during `il_init` (`sub_5CFE20`) as a 16,312-byte block (stored at `qword_126F228`), yielding 2039 bucket slots (16312 / 8 = 2039). Each bucket is a pointer to the head of a singly-linked chain of constant nodes.

```text
Hash Table Layout (qword_126F228):

    +--------+--------+--------+     +--------+
    | slot 0 | slot 1 | slot 2 | ... |slot 2038|
    +--------+--------+--------+     +--------+
        |        |        |
        v        v        v
      const -> const -> NULL    (singly-linked chains)
       |
       v
      const -> NULL
```

**Why 2039?** The number 2039 is prime. Using a prime number as the hash table size ensures that the modular-reduction step (`hash % 2039`) distributes keys uniformly even when the hash function produces patterns with common factors. The compiled code computes the modulus through an optimized multiply-and-shift sequence (multiply by `0x121456F`, then shift) rather than a hardware division instruction.

### alloc_shareable_constant (sub_5D2390)

This is the entry point for all constant allocation when sharing is enabled. It implements a hash-table lookup with MRU (most recently used) reordering of the chain:

```python
alloc_shareable_constant(local_constant) -> constant*:

    total_alloc_count++                              // qword_126F208

    if not sharing_enabled or not constant_is_shareable(local_constant):
        return alloc_constant(local_constant)        // fallback to non-shared alloc

    if local_constant has parent:
        // parent's shared pointer is already the canonical copy
        assert parent->type == 2
        return parent->shared_ptr

    // ---- hash table lookup ----
    hash = compute_constant_hash(local_constant)     // sub_5BE150
    bucket_index = hash % 2039
    bucket_ptr = &hash_table[bucket_index]

    prev = NULL
    curr = *bucket_ptr
    while curr != NULL:
        comparison_count++                           // qword_126F200
        if compare_constants(curr, local_constant, 0):
            // ---- HIT: MRU reorder ----
            if prev != NULL:
                // unlink curr from current position
                prev->next = curr->next
                // move curr to front of chain
                curr->next = *bucket_ptr
                *bucket_ptr = curr
            if curr is in same region:
                region_hit_count++                   // qword_126F218
            else:
                global_hit_count++                   // qword_126F220
            return curr
        prev = curr
        curr = curr->next

    // ---- MISS: allocate new canonical constant ----
    new_bucket_count++                               // qword_126F210
    new_constant = alloc_in_file_scope(184)          // sub_5E11C0 or sub_5E1620
    memcpy(new_constant, local_constant, 184)        // 11.5 x SSE + 8-byte tail
    clear_sharing_flags(new_constant)
    fixup_constant_references(new_constant)          // sub_5D39A0
    // link at head of chain
    new_constant->next = *bucket_ptr
    *bucket_ptr = new_constant
    return new_constant
```

**MRU optimization rationale.** When a hash bucket chain contains many constants (collision), recently matched constants are likely to be matched again soon (temporal locality from template instantiation expanding the same types repeatedly). Moving the matched node to the front of the chain converts an O(n) average-case lookup into O(1) for repeated accesses to the same constant.

**Statistics counters.** The sharing system maintains four counters for profiling:

| Counter | Address | Meaning |
|---|---|---|
| `qword_126F200` | comparisons | Total `compare_constants` calls during sharing |
| `qword_126F208` | total_allocs | Total calls to `alloc_shareable_constant` |
| `qword_126F210` | new_buckets | Number of cache misses (new canonical entries) |
| `qword_126F218` | region_hits | Sharing hits where the existing constant is in the same region |
| `qword_126F220` | global_hits | Sharing hits where the existing constant is in a different region |

### String Constant Interning (sub_5DBAB0)

String literals receive a separate interning pass through `intern_string_constant` at `0x5DBAB0`. This function reuses the same 2039-bucket hash table (`qword_126F228`) but with string-specific comparison logic:

```python
intern_string_constant(string, context_a, context_b) -> constant*:

    hash = compute_constant_hash(string)
    bucket_index = hash % 2039

    // linear chain search with exact match (flag=1)
    for each entry in chain:
        if compare_constants(entry, local_constant, 1):   // strict mode
            move_to_front(entry)                           // MRU
            return entry

    // miss: allocate new string constant in file-scope region
    new = alloc_constant_with_source_sequence(ck_string)
    memcpy(new, local_constant, 184)
    new->string_data = alloc_string_storage(strlen(string) + 1)
    strcpy(new->string_data, string)
    clear_sharing_flags(new)
    fixup_constant_references(new)
    link_at_chain_head(bucket_index, new)
    free_local_constant(local_constant)
    return new
```

### fixup_constant_references (sub_5D39A0)

After a constant is copied into the shared region, some of its internal pointers may still reference nodes in the source (non-shared) region. `fixup_constant_references` walks the constant's internal structure and redirects these dangling references:

- If the constant's associated IL entry is not in the shared region, the back-pointer at offset +128 is cleared.
- For template parameter constants (kind 12), sub-kinds 1 and 5-10 may embed expression trees at offsets +160/+168. If these expressions are not in the shared region, they are deep-copied via `copy_expr_tree` or reattached via `attach_to_region`.
- For literal value constants (kind 15) with expression sub-kind 13, the constant kind is rewritten based on the expression's kind (expr kind 2 becomes const kind 2, etc.), effectively inlining the expression into the constant.

---

## Part 3: The Deep Copy Engine

### Why It Exists

Template instantiation requires cloning expression trees from template definitions while replacing template parameter references with the actual arguments provided at the instantiation site. This is not a simple `memcpy` — every node in the tree must be visited, its pointers updated to reference the new region's copies, and template parameter nodes must be intercepted and replaced with substituted values. The deep copy engine provides this transformation.

Default argument expansion also uses the copy engine: when a function call omits an argument that has a default, the default's expression tree is cloned from the function declaration into the call site.

### i_copy_expr_tree (sub_5D2F90)

The central expression copier. It takes an expression node, a flags word, and a substitution-list context, then returns a freshly allocated deep copy.

```python
i_copy_expr_tree(src_expr, flags, subst_list) -> expr_node*:

    // shallow clone: allocate new node, copy fixed fields
    dest = allocate_expr_node_clone(src_expr)      // sub_5C28B0

    switch src_expr->kind:

        case 0  (null):           // no children to copy
        case 3  (entity ref):     // entity pointer is shared, not copied
        case 4  (type ref):       // type pointer is shared
        case 16 (sentinel):       // no data
        case 19 (template ref):   // entity pointer is shared
        case 20 (type constant):  // type pointer is shared
        case 22 (opaque ptr):     // shallow only
        case 30 (template spec):  // shallow only
            break                 // nothing beyond the shallow clone

        case 1 (operation):
            // recursively copy the operand linked list
            dest->operands = i_copy_list_of_expr_trees(src_expr->operands, flags, subst)

        case 2 (constant reference):
            // deep-copy the constant node
            dest->constant = i_copy_constant_full(src_expr->constant, NULL, flags, subst)

        case 5 (dynamic init):
            dest->init = i_copy_dynamic_init(src_expr->init, flags, subst)

        case 6 (call expression):
            // walk argument list, copy each argument expression
            dest->args = copy_arg_list(src_expr->args, flags, subst)

        case 7 (full expression info):
            // copy 6 sub-fields (type, scope, sub-expression, etc.)
            copy_full_expr_children(dest, src_expr, flags, subst)

        case 8 (template arguments):
            dest->type_list = copy_type_list(src_expr->type_list, flags)

        case 9 (pack expansion):
            dest->type = copy_type(src_expr->type)
            dest->inner = i_copy_expr_tree(src_expr->inner, flags, subst)

        case 10 (object lifetime):
            push_lifetime_scope()
            dest->inner = i_copy_expr_tree(src_expr->inner, flags, subst)
            attach_lifetime(dest)

        case 11, 23, 32, 33 (sub-expression list):
            dest->list = i_copy_list_of_expr_trees(src_expr->list, flags, subst)

        case 12, 14, 15 (typed value):
            // conditional copy based on value byte
            if src_expr->value_byte matches copy-condition:
                dest->inner = i_copy_expr_tree(src_expr->inner, flags, subst)

        case 13 (two-byte key):
            // no children beyond the key value

        case 17 (entity reference, copyable):
            if flags & 0x80:   // copy_entities mode
                dest->entity = alloc_constant_from_entity(src_expr->entity)

        case 18 (substitution slot):
            // look up in subst_list for replacement
            dest = resolve_from_substitution_list(subst_list, src_expr->index)

        case 21, 26, 27 (expression + list):
            dest->inner = i_copy_expr_tree(src_expr->inner, flags, subst)
            dest->list = i_copy_list_of_expr_trees(src_expr->list, flags, subst)

        case 24 (list + pointer):
            dest->list = i_copy_list_of_expr_trees(src_expr->list, flags, subst)
            dest->ptr = copy_pointer_target(src_expr->ptr)

        case 25 (expression + flags):
            dest->inner = i_copy_expr_tree(src_expr->inner, flags, subst)
            dest->flags |= propagated_flags

        case 28 (attributed expression):
            dest->inner = i_copy_expr_tree(src_expr->inner, flags, subst)
            // attribute flags are already copied in the shallow clone

        case 31 (expression + extracted pointer):
            dest->inner = i_copy_expr_tree(src_expr->inner, flags, subst)
            dest->extra = extract_pointer(src_expr)

        case 34 (constexpr fold):
            dest = copy_constexpr_fold(src_expr)    // sub_65AE50

        default:
            internal_error("i_copy_expr_tree: bad expr kind")

    // ---- post-copy entity resolution (LABEL_11) ----
    if flags & 0x10:   // resolve_refs mode
        for kinds 2, 3, 7, 19:
            resolve_entity_ref(dest)               // sub_5B3030

    return dest
```

**Flags interpretation for the copy engine:**

| Bit | Mask | Meaning |
|---|---|---|
| 4 | `0x10` | `resolve_refs` — after copying, resolve entity references through the symbol table |
| 7 | `0x80` | `copy_entities` — copy entity nodes themselves (not just references to them) |
| 12 | `0x1000` | `mark_instantiated` — stamp copied nodes with the instantiation flag |
| 14 | `0x5000` | `preserve_source_pos` — carry source-position annotations from source to copy |

### i_copy_list_of_expr_trees (sub_5D38C0)

A helper that walks a linked list of expression nodes (connected via the `next` pointer at offset +16), copies each via `i_copy_expr_tree`, and links the copies into a new list:

```python
i_copy_list_of_expr_trees(head, flags, subst) -> expr_node*:

    result_head = NULL
    result_tail = NULL

    curr = head
    while curr != NULL:
        copy = i_copy_expr_tree(curr, flags, subst)
        if result_head == NULL:
            result_head = copy
        else:
            result_tail->next = copy
        result_tail = copy
        curr = curr->next

    return result_head
```

### i_copy_constant_full (sub_5D3B90)

The constant copier handles the 184-byte constant node and its recursive sub-structure. It maintains a substitution list to avoid duplicating shared type definitions across the copy tree.

```python
i_copy_constant_full(src, dest_or_null, flags, subst_list) -> constant*:

    if dest_or_null:
        dest = dest_or_null                 // copy in place
    else:
        dest = alloc_constant_node()        // sub_5E11C0

    memcpy(dest, src, 184)                  // 11 x SSE + 8-byte tail
    clear_sharing_flag(dest, bit 2 at [5].byte[3])
    clear_sharing_flag(dest, bit 5 at [9].byte[3])

    switch dest->constant_kind:

        case 10 (aggregate):
            // walk linked list of sub-constants, deep-copy each
            for each element in dest->element_list:
                element = i_copy_constant_full(element, NULL, flags, subst_list)
                relink(element)

        case 11 (constant + scope):
            dest->sub_constant = i_copy_constant_full(
                dest->sub_constant, NULL, flags, subst_list)

        case 9 (dynamic init):
            dest->init = i_copy_dynamic_init(dest->init, flags, subst_list)

        case 6 (address constant with type definition):
            // substitution-list management: look up whether this type
            // has already been copied in this tree; if so, reuse the copy
            existing = lookup_in_subst_list(subst_list, src->type_def)
            if existing:
                dest->type_def = existing
            else:
                dest->type_def = copy_type(src->type_def)
                add_to_subst_list(subst_list, src->type_def, dest->type_def)

        case 12 (template parameter constant):
            switch dest->template_param_kind:
                case 0, 2, 3, 13: // no extra copy needed
                case 1:           dest->value_expr = copy_expr_tree(dest->value_expr)
                case 4, 12:       dest->inner = i_copy_constant_full(dest->inner, ...)
                case 5-10:        dest->extra_expr = copy_expr_tree(dest->extra_expr)
                case 11:          dest->type = copy_type(dest->type)
                                  dest->arg_list = copy_template_arg_list(dest->arg_list)

    fixup_constant_references(dest)         // sub_5D39A0
    return alloc_shareable_constant(dest)   // sub_5D2390 -- may deduplicate
```

**Substitution list purpose.** When copying an expression tree that references the same type definition in multiple sub-expressions (e.g., two occurrences of `decltype(x)` in a single template), the substitution list ensures both references point to the same copied type node, preserving the sharing relationship from the original tree.

### Public Wrappers

The `i_copy_*` functions are internal — they take a substitution-list parameter that must be cleaned up after use. Public wrappers handle this lifecycle:

| Wrapper | Address | Internal function | Purpose |
|---|---|---|---|
| `copy_expr_tree` | `sub_5D3940` | `i_copy_expr_tree` | Expression deep copy with auto-cleanup |
| `copy_constant_full` | `sub_5D4300` | `i_copy_constant_full` | Constant deep copy with auto-cleanup |
| `copy_dynamic_init` | `sub_5D4CF0` | `i_copy_dynamic_init` | Dynamic init deep copy with auto-cleanup |
| `copy_constant` | `sub_5D4D50` | `i_copy_constant_full` | Simple constant copy (flags=0) |

Each wrapper allocates a local substitution list, calls the internal function, then appends the list entries to the global free list at `qword_126F1E0`.

---

## Part 4: Template Parameter Substitution

### Why It Exists

The deep copy engine (Part 3) performs a mechanical tree clone — it duplicates structure but does not transform content. Template instantiation requires more: when the copier encounters a node referencing template parameter `T`, it must replace that node with the actual type argument (e.g., `int`). When it encounters `sizeof(T)`, it must evaluate the expression with `T=int` and produce the constant `4`. The template parameter substitution engine is the transformation layer that sits on top of the copy engine, intercepting template-parameter nodes and performing the substitution.

### copy_template_param_expr (sub_5DC000)

This is the central dispatcher for expression-level template substitution. At 1416 lines and 7872 bytes of compiled code, it is the largest single function in the comparison/copy subsystem. It takes up to 10 arguments:

```c
copy_template_param_expr(
    expr_node*   a1,     // expression to substitute
    template_ctx a2,     // template argument context
    template_ctx a3,     // secondary context (for nested templates)
    type*        a4,     // expected result type
    scope*       a5,     // current scope
    int          a6,     // flags
    int*         a7,     // error_flag (output: set to 1 on failure)
    diag_info*   a8,     // diagnostics context
    constant*    a9,     // scratch constant (pre-allocated workspace)
    constant**   a10     // output constant pointer
) -> expr_node*          // substituted expression, or NULL (use a9/a10)
```

The function dispatches on `expr->kind` and, for operation nodes, further dispatches on the operation code:

```python
copy_template_param_expr(expr, tctx, ...):

    switch expr->kind:

        case 0 (empty):
            return expr                       // pass through unchanged

        case 1 (operation):
            switch expr->op_code:

                case 116 (type expression):
                    return copy_template_param_type_expr(expr, tctx, ...)

                case 5 (cast):
                    // substitute the cast's target type
                    new_type = copy_template_param_type(expr->target_type, tctx)
                    // recursively substitute the operand
                    new_operand = copy_template_param_expr(expr->operands, tctx, ...)
                    return build_cast_node(new_type, new_operand)

                case 0, 25, 28, 29, 53-57, 71, 72, 87, 88, 103 (unary/simple binary):
                    // substitute operand(s) recursively
                    new_ops = substitute_operand_list(expr->operands, tctx)
                    return build_operation_node(expr->op_code, new_ops, new_type)

                case 26, 27, 39-43, 58-63 (binary with type check):
                    // substitute both operands
                    lhs = copy_template_param_expr(expr->operands[0], tctx, ...)
                    rhs = copy_template_param_expr(expr->operands[1], tctx, ...)
                    // post-substitution type promotion:
                    do_conversions_on_operands_of_copied_template_expr(op, &lhs, &rhs)
                    return build_operation_node(op, lhs, rhs, result_type)

                case 39 (ternary / conditional):
                    cond  = copy_template_param_expr(operands[0], ...)
                    true_ = copy_template_param_expr(operands[1], ...)
                    false_= copy_template_param_expr(operands[2], ...)
                    return build_conditional(cond, true_, false_)

                case 44, 45 (imaginary):
                    internal_error("imaginary operators not implemented")

        case 2 (constant reference):
            return copy_template_param_con(expr->constant, tctx, ...)

        case 3 (variable / entity reference):
            // look up the variable in the substitution context
            subst = find_substitution(tctx, expr->entity)
            if subst found:
                // check type compatibility between expected and actual
                if is_pointer_compatible(expected_type, subst->type):
                    return build_value_from_constant(subst)
                else:
                    apply_type_conversion(subst, expected_type)
            return expr   // no substitution needed

        case 5 (function call in constant context):
            // dispatch on call sub-kind
            switch expr->call_subkind:
                case 1: substitute type + validate value
                case 2: delegate to copy_template_param_con

        case 19 (template parameter reference):
            // same logic as case 3 for template parameters
            subst = find_substitution(tctx, expr->template_param)
            return build_substituted_expr(subst)

        case 20 (type constant):
            new_type = copy_template_param_type(expr->type, tctx)
            return build_type_constant_expr(new_type)

        case 21 (builtin operation):
            return copy_template_param_builtin_operation(expr, tctx, ...)
            // asserts no error in process

        case 22 (type reference):
            new_type = copy_template_param_type(expr->type, tctx)
            return build_type_ref_expr(new_type)

        case 23 (expression wrapper):
            inner = copy_template_param_expr(expr->inner, tctx, ...)
            return wrap(inner)

        case 30 (pack expansion):
            return expand_pack(expr, tctx, ...)

        case 31 (dependent entity):
            // complex hash-map based instantiation tracking
            // with get_with_hash / vector_insert / entity list processing
            return instantiate_dependent_entity(expr, tctx, ...)
```

**Post-substitution type conversions.** The internal helper `do_conversions_on_operands_of_copied_template_expr` (at il.c line 18885) handles arithmetic promotions that must occur after template parameter substitution. For example, `T + U` where `T=int` and `U=double` requires promoting the `int` operand to `double` — this promotion is not present in the template definition's expression tree because the types are unknown there. The function handles:

- **Shift operators** (ops 53-54): promote the result type to the promoted LHS type.
- **Comparison operators** (ops 58-63): compute the common type and apply usual arithmetic conversions.
- **Arithmetic operators** (default): compute common type via `sub_5657C0` and insert implicit conversion nodes.
- **Imaginary operators** (ops 44-45): explicitly not implemented (triggers `internal_error`).

### copy_template_param_con (sub_5DE290)

The constant-level substitution dispatcher. At 819 lines, it handles the case where a constant node in a template definition contains a reference to a template parameter:

```python
copy_template_param_con(constant, tctx, expected_type, scope, flags,
                        error_flag, diag, scratch) -> constant*:

    switch constant->constant_kind:

        case 12 (template parameter constant):
            // this is the core case -- the constant IS a template parameter
            switch constant->template_param_kind:

                case 0 (value parameter):
                    // look up the bound value in the template argument list
                    binding = lookup_template_arg(tctx, constant->param_index)
                    if binding is a pack:
                        return expand_pack_element(binding, ...)
                    return binding->value_constant

                case 1 (expression parameter):
                    // try overload resolution first
                    result = copy_template_param_con_overload_resolution(...)
                    if result: return result
                    // fall back to full expression-level substitution
                    return copy_template_param_expr(constant->expr, tctx, ...)

                case 2 (non-member entity parameter):
                    return copy_template_param_unknown_entity_con(constant, FALSE, ...)

                case 3 (member entity parameter):
                    return copy_template_param_unknown_entity_con(constant, TRUE, ...)

                case 4 (nested constant parameter):
                    return copy_template_param_con(constant->inner, tctx, ...)

                case 5-10 (scalar value parameters: sizeof, alignof, etc.):
                    // look up the substitution via sub_5BFB80
                    // perform type equality check
                    // apply type conversions if needed
                    return substituted_scalar_constant(...)

                case 11 (entity + argument pack):
                    // entity substitution with argument list processing
                    return substitute_entity_with_args(...)

                case 12 (nested recursive):
                    return copy_template_param_con(constant->inner, tctx, ...)

        case 6 (address/aggregate constant):
            switch constant->address_kind:
                case 3 (function call):
                    // substitute callee type + each argument recursively
                    callee_type = copy_template_param_type(constant->callee_type, tctx)
                    for each arg in constant->args:
                        arg = copy_template_param_con(arg, tctx, ...)
                    return build_call_constant(callee_type, args)
                default:
                    if is_dependent_type(constant->type):
                        return deep_copy_constant(constant)
                    // handle address-space attribute patterns

        case 15 (expression constant):
            switch constant->expr_constant_kind:
                case 46 (strip_template_arg):
                    // dispatch on template argument type:
                    //   0 = type argument -> type substitution
                    //   1 = value argument -> value substitution
                    //   2 = template argument -> template substitution
                case 6:  return type_substitution(...)
                case 13: return non_type_param_substitution(...)
                case 2:  return recursive copy_template_param_con(inner, ...)

        default:
            internal_error("copy_template_param_con: unexpected kind")
```

### copy_template_param_con_with_substitution (sub_5DFAD0)

The top-level entry point for template constant substitution, called from the template instantiation driver. It manages the IL region switch (moving allocation to file-scope for the duration of instantiation), handles the initial overload-resolution check, and performs post-substitution type normalization:

```python
copy_template_param_con_with_substitution(constant, template_args, scope,
                                          expected_type, access, flags,
                                          error_flag, scratch):

    saved_region = current_region
    switch_to_file_scope_region()            // with debug trace

    local_scratch = alloc_local_constant()

    // ---- special case: overload resolution for expression parameters ----
    if constant->kind == 12 and constant->param_kind == 1:
        overload_info = lookup_overload_candidate(constant)
        if overload_info:
            result = copy_template_param_con_overload_resolution(
                         constant, overload_info, tctx, ...)
            if result: goto post_process

    // ---- validate expected type ----
    if expected_type is pointer_type:
        validate_pointer_binding(expected_type)

    // ---- main substitution ----
    result = copy_template_param_expr(constant->expr, tctx, ...)
    // or: result = copy_template_param_con(constant, tctx, ...)
    // depending on whether the constant embeds an expression

    post_process:
    // ---- post-substitution type normalization ----
    if result->type is pointer_type:
        validate_binding(result)
        result = try_implicit_conversion(result)
    elif result->type is array_type:
        result = try_implicit_conversion(result)
        result = array_to_pointer_decay(result)
    elif result->type is function_type:
        result = try_implicit_conversion(result)
        result = function_to_pointer_decay(result)
    else:
        result = general_conversion(result)

    // ---- handle deferred instantiation ----
    if is_deferred_instantiation(result):
        copy_deferred_data_into_scratch(result, scratch)

    restore_region(saved_region)
    free_local_constant(local_scratch)
    return result
```

### Supporting Functions

| Function | Address | Lines | Purpose |
|---|---|---|---|
| `copy_template_param_type_expr` | `sub_5DDEB0` | 82 | Handles op=116 type expressions within template substitution; extracts and substitutes the type, checks dependent-type status |
| `copy_template_param_expr_list` | `sub_5DE010` | 77 | Iterates an expression linked list, calling `copy_template_param_expr` on each element; shares a single scratch constant across all iterations |
| `copy_template_param_value_expr` | `sub_5DE1A0` | 55 | Single-expression variant; passes the expression's own type as the expected type |
| `copy_template_param_con_overload_resolution` | `sub_5DF6A0` | 180 | Attempts overload resolution during template substitution when the template parameter refers to a set of overloaded functions; validates result type compatibility |
| `copy_template_param_unknown_entity_con` | `sub_5DB420` | 213 | Handles entity constants where the entity kind is not known until substitution time (using declarations, namespace aliases, variables, templates, types) |

---

## Part 5: Data Flow Between the Subsystems

The four subsystems interact in a specific calling pattern during template instantiation:

```text
Template Instantiation Driver
  |
  +-> copy_template_param_con_with_substitution (entry point)
        |
        +-> copy_template_param_expr (expression-level dispatch)
        |     |
        |     +-> copy_template_param_con (constant-level dispatch)
        |     |     |
        |     |     +-> copy_template_param_unknown_entity_con
        |     |     +-> copy_template_param_con_overload_resolution
        |     |     +-> [recursive: copy_template_param_expr]
        |     |     +-> [recursive: copy_template_param_con]
        |     |
        |     +-> copy_template_param_type (type-level, in type.c)
        |     +-> copy_template_param_type_expr
        |     +-> copy_template_param_expr_list
        |     +-> copy_template_param_builtin_operation
        |
        +-> alloc_shareable_constant (deduplication on output)
              |
              +-> compare_constants (hash table equality check)
              +-> fixup_constant_references
```

The comparison engine is not called during the copy itself — it is called only at the end, when the newly constructed constants are passed through `alloc_shareable_constant` for deduplication. This means the copy engine may temporarily create duplicate constants that are later merged by the sharing infrastructure. The design separates concerns: the copy engine focuses on correctness (producing a valid substituted tree), while the sharing engine focuses on efficiency (deduplicating identical results).

---

## Part 6: Initialization and Reset

### il_one_time_init (sub_5CF7F0)

Called once at program startup. Validates seven name-table arrays end with the `"last"` sentinel string, checks the `sizeof_il_entry` guard value (9999), and initializes 60+ allocation pools via `pool_init` (`sub_7A3C00`) with element sizes ranging from 1 byte to 1344 bytes. Conditionally initializes C++-mode pools (guarded by `dword_106BF68 || dword_106BF58`).

### il_init (sub_5CFE20)

Called at the start of each translation unit. Zeroes all global pool heads, allocates and zeroes the two hash tables:

- **Character type table:** 3240 bytes at `qword_126F2F8` (5 character types x 81 slots = 405 entries, 8 bytes each).
- **Constant sharing table:** 16312 bytes at `qword_126F228` (2039 buckets, 8 bytes each).

Sets the three sharing mode bytes (`byte_126E558`, `byte_126E559`, `byte_126E55A`) to 3 (all sharing enabled), and tail-calls `il_init_float_constants` (`sub_5EAF00`).

### il_reset_secondary_pools (sub_5D0170)

Zeroes ~80 qword globals in the `0x126F680`--`0x126F978` range. These are transient counters, list heads, and cached type pointers used during template instantiation. Called separately from `il_init`, suggesting it resets state between instantiation passes within the same translation unit.

---

## Address Map

| Address | Function | Lines | Role |
|---|---|---|---|
| `0x5CF7F0` | `il_one_time_init` | ~200 | One-time startup validation + pool init |
| `0x5CFE20` | `il_init` | ~100 | Per-TU hash table allocation + state reset |
| `0x5D0170` | `il_reset_secondary_pools` | ~40 | Reset instantiation-transient state |
| `0x5D0750` | `compare_expressions` | 588 | Expression tree structural equality |
| `0x5D1320` | `compare_expressions_for_equivalence` | ~10 | Thin wrapper (flags=4) |
| `0x5D1350` | `compare_constants` | 525 | Constant structural equality, 16 kinds |
| `0x5D1FE0` | `compare_dynamic_inits` | ~80 | Dynamic init comparison |
| `0x5D2160` | `compare_constants_default` | ~5 | Thin wrapper (flags=0) |
| `0x5D2170` | `expr_tree_contains_template_param_constant` | ~50 | Template param presence check |
| `0x5D2210` | `constant_is_shareable` | ~100 | Shareability predicate |
| `0x5D2390` | `alloc_shareable_constant` | ~200 | Hash table deduplication allocator |
| `0x5D2890` | `alloc_il_entry_from_constant` | ~20 | Wraps constant in IL entry |
| `0x5D2F90` | `i_copy_expr_tree` | 424 | Expression tree deep copy (35-case switch) |
| `0x5D38C0` | `i_copy_list_of_expr_trees` | ~40 | Linked-list copy helper |
| `0x5D3940` | `copy_expr_tree` | ~30 | Public wrapper with cleanup |
| `0x5D39A0` | `fixup_constant_references` | ~80 | Post-copy pointer fixup |
| `0x5D3B90` | `i_copy_constant_full` | 305 | Constant deep copy (16-kind switch) |
| `0x5D4300` | `copy_constant_full` | ~20 | Public wrapper with cleanup |
| `0x5D47A0` | `i_copy_dynamic_init` | ~150 | Dynamic init deep copy |
| `0x5D4C00` | `copy_lambda_capture` | ~60 | Lambda capture list copy |
| `0x5D4DB0` | `alloc_constant` | ~150 | Non-shared constant allocation with kind-specific cleanup |
| `0x5DBAB0` | `intern_string_constant` | ~92 | String literal interning via hash table |
| `0x5DC000` | `copy_template_param_expr` | 1416 | Template substitution — expression dispatcher |
| `0x5DDEB0` | `copy_template_param_type_expr` | 82 | Template substitution — type expressions |
| `0x5DE010` | `copy_template_param_expr_list` | 77 | Template substitution — expression list |
| `0x5DE1A0` | `copy_template_param_value_expr` | 55 | Template substitution — single value expr |
| `0x5DE290` | `copy_template_param_con` | 819 | Template substitution — constant dispatcher |
| `0x5DF6A0` | `copy_template_param_con_overload_resolution` | 180 | Template substitution — overload resolution |
| `0x5DFAD0` | `copy_template_param_con_with_substitution` | 288 | Template substitution — top-level entry |
