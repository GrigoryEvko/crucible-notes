# Declaration Parser

C++ declaration parsing is the most ambiguity-ridden phase of front-end compilation. A statement like `T(x);` is simultaneously a valid function-style cast (expression) and a variable declaration with redundant parentheses. EDG 6.6 in cudafe++ resolves this by splitting the work into two stages: a prescanning/disambiguation phase (`disambig.c`) that probes ahead in the token stream to classify ambiguous constructs, followed by committed parsing across four tightly-coupled source files -- `decl_spec.c` (declaration specifiers), `declarator.c` (declarator syntax), `decls.c` (symbol table insertion and semantic validation), and `decl_inits.c` (initializer processing). CUDA adds a fifth axis of complexity: every declaration may carry execution space attributes (`__device__`, `__host__`, `__global__`) and memory space qualifiers (`__shared__`, `__constant__`, `__managed__`), which are parsed as attribute category 4 and must be separated from standard C++ attributes before semantic analysis.

The core pipeline processes approximately 22,000 lines of decompiled logic across six major functions, each exceeding 1,000 lines. The design is a classic recursive-descent parser with significant state carried in stack-allocated structures (128-byte `decl_spec` accumulators packed as `__m128i` arrays) and global scope chain state (784-byte entries in the scope table at `qword_126C5E8`).

## Key Facts

| Property | Value |
|---|---|
| Source files | `decl_spec.c`, `declarator.c`, `decls.c`, `decl_inits.c`, `disambig.c` |
| Address range | `0x4A0000`--`0x4F8000` (~360 KB of code, ~530 functions) |
| Central dispatcher | `sub_4ACF80` (`decl_specifiers`, 4,761 lines) |
| Declarator entry | `sub_4B7BC0` (`declarator`, 284 lines) |
| Function declarator | `sub_4B8190` (`function_declarator`, 3,144 lines) |
| Recursive declarator | `sub_4BC950` (`r_declarator`, 2,578 lines) |
| Function declaration | `sub_4CE420` (`decl_routine`, 2,858 lines) |
| Variable declaration | `sub_4CA6C0` (`decl_variable`, 1,090 lines) |
| Top-level variable entry | `sub_4DEC90` (`variable_declaration`, 1,098 lines) |
| Disambiguation | `sub_4EA560` (`prescan_declaration`, ~400 lines) |
| Scope entry size | 784 bytes (at `qword_126C5E8`) |
| Decl specifier accumulator | 128 bytes (4 x `__m128i`, stack-allocated) |
| CUDA mode flag | `dword_126EFA8` (bool), dialect in `dword_126EFB4` (2 = C++) |
| Current token global | `word_126DD58` |
| Token advance | `sub_676860` (`get_next_token`) |

## Architecture

The declaration parsing pipeline operates as a five-stage waterfall. Each stage narrows the interpretation of the token stream until a fully-resolved declaration is inserted into the symbol table:

```text
Token Stream (from lexer)
  │
  ▼
STAGE 1: Disambiguation (disambig.c)
  │  prescan_declaration ─── lookahead to classify ambiguous constructs
  │  prescan_gnu_attribute ── skip __attribute__((...)) blocks
  │  find_for_loop_separator ── distinguish for-init from expression
  │
  ▼
STAGE 2: Declaration Specifiers (decl_spec.c)
  │  decl_specifiers ─── 4,761-line switch dispatching on token kind
  │  ├── storage class: auto, register, static, extern, typedef
  │  ├── type specifiers: int, char, void, class/struct/enum, typename
  │  ├── cv-qualifiers: const, volatile, restrict
  │  ├── function specifiers: inline, virtual, explicit, constexpr, consteval
  │  ├── CUDA attributes: __device__, __host__, __global__ (category 4)
  │  └── class_specifier / enum_specifier (recursive for definitions)
  │
  ▼
STAGE 3: Declarator (declarator.c)
  │  declarator ─── coordinates pointer/array/function declarators
  │  ├── pointer_declarator ── *, &, &&, ::*
  │  ├── r_declarator ── recursive descent on declarator-id
  │  ├── array_declarator ── [expression], []
  │  ├── function_declarator ── (params) cv-qualifiers -> trailing-return noexcept
  │  └── scan_declarator_attributes ── separates CUDA attrs from standard
  │
  ▼
STAGE 4: Declaration Processing (decls.c)
  │  decl_routine ─── function/method declarations (2,858 lines)
  │  decl_variable ── variable declarations with CUDA memory space
  │  variable_declaration ── top-level entry with CUDA error emission
  │  find_linked_symbol ── redeclaration detection
  │  id_linkage ── linkage determination (internal/external/none)
  │
  ▼
STAGE 5: Initializer Processing (decl_inits.c)
     ctor_inits_for_inheriting_ctor ── inheriting constructors
     dtor_initializer ── destructor init lists
     check_for_missing_initializer_full ── missing initializer diagnostics
```

## Stage 1: Disambiguation (disambig.c)

### The Problem

C++ has a famous syntactic ambiguity: many token sequences can be parsed as either declarations or expressions. The canonical example:

```cpp
T(x);          // declaration of variable x of type T?  or  function-style cast of x to T?
T(x)(y);       // declaration of function x returning T?  or  call to T(x) with argument y?
T * x;         // declaration of pointer-to-T named x?  or  multiplication of T and x?
```

The C++ standard resolves these with the "if it can be a declaration, it is a declaration" rule. EDG implements this by prescanning: before committing to a parse, the parser saves the lexer state, probes ahead through the token stream to determine whether the construct is a declaration, then restores the lexer state and dispatches to the appropriate parser.

### prescan_declaration (sub_4EA560)

This is the top-level disambiguation entry point, called when the parser encounters an ambiguous construct at statement or declaration level. It operates in a non-destructive lookahead mode: it consumes tokens tentatively, classifies the construct, then rewinds.

```python
prescan_declaration(flags):
    save_lexer_state()
    
    # Compute CUDA-aware skip mode
    if flags & 0x800 == 0:       # not in template context
        skip_mode = 16385         # 0x4001: standard prescan
    else:
        skip_mode = 67125249      # 0x3FFC001: template-aware prescan
    
    # In CUDA C++ mode, use cuda_skip_token for identifier classification
    if dword_126EFB4 == 2:       # CUDA C++ dialect
        while not at_end_of_tentative_scan():
            token = current_token()
            if is_cuda_keyword(token):
                cuda_skip_token(skip_mode)   # sub_6810F0
            else:
                advance_token()              # sub_676860
            classify_declaration_vs_expression()
    
    restore_lexer_state()
    return classification  # DECLARATION or EXPRESSION
```

The `skip_mode` is a bitmask encoding which token classes to recognize during prescanning. In CUDA mode, the wider mask (`0x3FFC001`) includes CUDA execution-space keywords so that `__device__ int x;` is correctly classified as a declaration even though `__device__` is not a standard C++ keyword.

### prescan_gnu_attribute (sub_4E9E70)

Attributes complicate disambiguation because `__attribute__((foo))` can appear almost anywhere in a declaration. This function skips over balanced GNU attribute sequences during prescanning:

```python
prescan_gnu_attribute():
    assert current_token == 142     # GNU __attribute__ token
    while current_token == 142:
        advance_token()             # consume __attribute__
        match_balanced_parens()     # skip ((...))
        
        # CUDA extension: check if identifier is CUDA keyword
        if dword_126EFB4 == 2:      # CUDA C++ mode
            if BYTE1(xmmword_106C390) & 2:  # CUDA extension flag
                cuda_skip_token(...)
```

### find_for_loop_separator (sub_4EC690)

A special-purpose disambiguator for `for` loops. In `for(init; cond; incr)`, the parser must find the semicolons that separate the three clauses. This is non-trivial because the `init` clause can contain declarations with complex types, nested parentheses, and template angle brackets.

```python
find_for_loop_separator():
    create_disambiguation_checkpoint()  # sub_67B4F0
    paren_depth = 0
    while true:
        token = current_token()
        if token == '(':
            paren_depth++
        elif token == ')':
            if paren_depth == 0:
                break
            paren_depth--
        elif token == ';' and paren_depth == 0:
            restore_checkpoint()
            return SEMICOLON_FOUND   # 0x4B = 75
        elif token == EOF:
            restore_checkpoint()
            return EOF               # 9
    restore_checkpoint()
    return NOT_FOUND                 # 0
```

## Stage 2: Declaration Specifiers (decl_spec.c)

### decl_specifiers (sub_4ACF80) -- The Central Dispatcher

This is the most complex function in the declaration parser: 4,761 decompiled lines, a `while(2)` loop containing a giant switch on token kinds, processing every specifier in a C++ declaration. It handles storage classes, type specifiers, cv-qualifiers, function specifiers, and CUDA attributes, accumulating results into a 128-byte stack structure.

#### Input Parameter: Context Flags

The `a1` parameter encodes the parsing context as a bitmask:

| Bit | Mask | Context |
|---|---|---|
| 2 | `0x4` | Inside class member declaration |
| 3 | `0x8` | Inside function parameter list |
| 4 | `0x10` | At block scope |
| 6 | `0x40` | Inside template parameter list |
| 14 | `0x4000` | Friend declaration |
| 15 | `0x8000` | At class scope |
| 18 | `0x40000` | In-declaration (re-entrant) |
| 20 | `0x100000` | Constexpr lambda context |

#### The Accumulator Structure

Results are accumulated into a stack-allocated structure (parameter `a2`) laid out as:

| Offset | Size | Field | Description |
|---|---|---|---|
| `+8` | 4 | `specifier_flags` | Bitmask of specifiers seen |
| `+32` | 8 | `source_position` | Position of first specifier |
| `+120` | 4 | `flags` | Parsing state flags |
| `+132` | 4 | `context` | Context discriminator |
| `+200` | 8 | `attribute_list` | Linked list of parsed attributes |
| `+208` | 8 | `attribute_list_alt` | Secondary attribute list (CUDA exec space) |
| `+228` | 4 | `modifiers` | Accumulated modifier bits |
| `+272` | 8 | `type_ptr` | Resolved type pointer |

#### Pseudocode

```python
decl_specifiers(context_flags, accumulator, type_chain, ...):
    debug_trace(3, "decl_specifiers")
    
    spec_bits = 0        # accumulated specifier combination flags
    error_flag = 0
    
    while true:  # while(2) in decompilation
        token = word_126DD58    # current token
        
        switch token:
        
        # ── Storage class specifiers ──
        case TOKEN_AUTO:         # 77
        case TOKEN_REGISTER:     # 119
        case TOKEN_STATIC:       # 99
        case TOKEN_EXTERN:       # 88
        case TOKEN_TYPEDEF:      # 103
            process_storage_class_specifier(
                auto_flag, ..., context_flags, accumulator,
                prev_scope, &spec_bits, &result, &type_out, &error_flag
            )
            continue
        
        # ── Type specifiers (keywords) ──
        case TOKEN_VOID .. TOKEN_DOUBLE:       # 81-119 range
        case TOKEN_SIGNED:
        case TOKEN_UNSIGNED:
        case TOKEN_CHAR:
        case TOKEN_INT:
        case TOKEN_FLOAT:
        case TOKEN_DOUBLE:
            # Validate combination with existing specifiers
            if spec_bits & CONFLICTING_TYPE_MASK:
                emit_error(84)     # conflicting type specifiers
            spec_bits |= type_specifier_bit(token)
            advance_token()
            continue
        
        # ── cv-qualifiers ──
        case TOKEN_CONST:        # 263
        case TOKEN_VOLATILE:     # 264
        case TOKEN_RESTRICT:     # 265, 266
            accumulator.modifiers |= cv_bit(token)
            advance_token()
            continue
        
        # ── Function specifiers ──
        case TOKEN_INLINE:
            spec_bits |= INLINE_BIT
            advance_token()
            continue
        
        case TOKEN_VIRTUAL:
            spec_bits |= VIRTUAL_BIT
            advance_token()
            continue
        
        case TOKEN_EXPLICIT:
            spec_bits |= EXPLICIT_BIT
            advance_token()
            continue
        
        # ── C++11/17/20 specifiers ──
        case TOKEN_CONSTEXPR:
            spec_bits |= CONSTEXPR_BIT
            if context_flags & 0x100000:    # constexpr lambda
                emit_error(1570)
            advance_token()
            continue
        
        case TOKEN_CONSTEVAL:
            spec_bits |= CONSTEVAL_BIT
            advance_token()
            continue
        
        case TOKEN_CONSTINIT:
            spec_bits |= CONSTINIT_BIT
            advance_token()
            continue
        
        case TOKEN_THREAD_LOCAL:
            spec_bits |= THREAD_LOCAL_BIT
            advance_token()
            continue
        
        # ── Class/struct/union/enum definitions ──
        case TOKEN_CLASS:        # 151
        case TOKEN_STRUCT:
        case TOKEN_UNION:
            class_specifier(scope, context_flags, ..., &result, &error_flag)
            continue
        
        case TOKEN_ENUM:
            enum_specifier(scope, context_flags, ..., &result, &error_flag)
            continue
        
        # ── typename specifier ──
        case TOKEN_TYPENAME:     # 183
            typename_specifier(&type_out, accumulator, context_flag, ...)
            continue
        
        # ── Identifier (type name or constructor) ──
        case TOKEN_IDENTIFIER:   # 1
            # This is the declaration/expression ambiguity hotspot
            if try_interpret_as_type_name(accumulator):    # sub_4C4F80
                continue
            if is_constructor_decl(enclosing_class):       # sub_4AC970
                continue
            # Not a type name — fall through to end of specifiers
            break
        
        # ── GNU __attribute__ / __declspec ──
        case TOKEN_ATTRIBUTE:    # 142
            parse_attribute_list(accumulator)
            # CUDA: execution space attributes separated here
            continue
        
        # ── typeof / decltype ──
        case TOKEN_TYPEOF:       # 189
        case TOKEN_DECLTYPE:     # 185
            parse_typeof_or_decltype(accumulator)
            continue
        
        # ── End of specifiers ──
        case TOKEN_SEMICOLON:    # 55
        default:
            break  # exit while loop
    
    # Post-processing: validate specifier combinations
    if spec_bits == 0 and no_type_found:
        emit_error(79)   # missing type specifier
    
    # CUDA: check execution space context
    if dword_126EFB4 == 2:   # CUDA C++ mode
        validate_cuda_execution_space(accumulator, context_flags)
        if invalid_cuda_context:
            emit_error(3537)  # execution space attribute in wrong context
```

#### Token Classification Map

The switch in `decl_specifiers` handles the following token kinds:

| Token Code | Keyword | Category |
|---|---|---|
| 1 | identifier | Type name or constructor check |
| 77 | `auto` | Storage class (C++03) / placeholder type (C++11) |
| 88 | `extern` | Storage class |
| 99 | `static` | Storage class |
| 103 | `typedef` | Storage class |
| 119 | `register` | Storage class |
| 80--108 | C type keywords | Type specifiers |
| 142 | `__attribute__` | GNU attribute |
| 151 | `class` | Class specifier |
| 183 | `typename` | Typename specifier |
| 185 | `decltype` | Decltype specifier |
| 189 | `typeof` | GNU typeof |
| 263--266 | cv-qualifiers | `const`, `volatile`, `restrict`, `__restrict` |

### process_storage_class_specifier (sub_4A31A0)

Validates and records a storage class specifier. C++ allows at most one storage class per declaration (with some exceptions for `thread_local`).

```python
process_storage_class_specifier(auto_flag, ..., context_flags, decl_info,
                                 prev_scope, spec_bits, result, type_out, error_flag):
    # Flag bits in context_flags:
    #   1=function, 4=class, 8=extern, 0x10=static, 0x200=register,
    #   0x4000=friend, 0x8000=at class scope, 0x100000=constexpr lambda

    if *spec_bits & STORAGE_CLASS_MASK:
        emit_error(80)     # duplicate storage class
        return
    
    if conflicting_with_previous_specifier:
        emit_error(81)     # conflicting storage class
        return
    
    switch current_storage_class:
        case EXTERN:
            if at_block_scope and not_cpp_mode:
                emit_error(85)
            if at_file_scope and not_standard_mode:
                emit_error(149)
            decl_info.linkage_byte = 3    # external linkage
        
        case STATIC:
            if in_class_definition and cpp_mode:
                emit_error(328)
        
        case REGISTER:
            emit_error(481)   # deprecated
        
        case AUTO:
            if dword_126EF4C:     # auto parameter support enabled
                # C++20: auto in parameter list = abbreviated template
                create_placeholder_type()    # sub_5BBA60
            else:
                emit_error(1598)  # auto type in invalid context
    
    *spec_bits |= storage_class_bit
```

### class_specifier (sub_4A57C0, 2,179 lines)

Parses `class`/`struct`/`union` specifiers including the full class body. This function manages scope entry/exit, base class lists, member declarations, access specifiers, and CUDA execution space propagation.

Key operations:
- Calls `scan_tag_name` (`sub_4A38A0`, 1,216 lines) to parse the class name, handling qualified names and template parameters
- Calls `check_for_class_modifiers` (`sub_4A3610`) to detect `final`/`__final`
- Manages the scope stack: pushes a class scope (kind 6 or 7) at `qword_126C5E8 + 784 * scope_index`
- Sets CUDA execution space flags at scope entry offset `+182` (bit `0x20`) for device-side class definitions
- Issues error 2407 for enum definitions in prohibited CUDA execution contexts

### enum_specifier (sub_4AA2F0, 1,437 lines)

Parses `enum`, `enum class`, and `enum struct` specifiers, including:
- Underlying type (`enum E : int`)
- Opaque enum declarations (`enum class E : int;`)
- Scoped vs. unscoped enum semantics
- Calls `scan_enumerator_list` (`sub_4A89F0`, 950 lines) for the enumerator body

### Specifier Validation Functions

After `decl_specifiers` accumulates all specifiers, several validation functions check that the combination is legal:

| Function | Address | Lines | Purpose |
|---|---|---|---|
| `check_use_of_constexpr` | `sub_4A22B0` | 153 | Validates `constexpr` on functions and variables |
| `check_use_of_consteval` | `sub_4A1BF0` | 104 | Validates `consteval` on functions only |
| `check_use_of_constinit` | `sub_4A1EC0` | 77 | Validates `constinit` on variables with static storage |
| `check_use_of_thread_local` | `sub_4A2000` | 111 | Validates `thread_local` placement |
| `check_explicit_specifier` | `sub_4A1DF0` | 45 | Validates `explicit` on constructors/conversions |
| `check_gnu_c_auto_type` | `sub_4A2580` | 52 | Validates GNU `__auto_type` |

Each follows the same pattern: examine the accumulated specifier bits and the entity kind at offset `+80` of the declaration node, and emit a targeted error if the combination is illegal. For example, `check_use_of_consteval`:

```python
check_use_of_consteval(decl_info):
    entity = decl_info[0]
    kind = entity[+80]       # symbol kind
    
    if kind != FUNCTION (10) and kind != MEMBER_FUNCTION (11):
        emit_error(2926)      # consteval on non-function
        entity[+177] &= 0xF9 # clear consteval bit
        return
    
    func_kind = entity[+166]
    if func_kind == DESTRUCTOR (2):
        emit_error(2927)      # consteval on destructor
        entity[+177] &= 0xF9
        return
    
    if func_kind == CONSTRUCTOR (1):
        if type_has_virtual_base(entity[+88]):
            emit_error(2928)  # consteval on ctor with virtual base
            entity[+177] &= 0xF9
            return
    
    if func_kind == CONVERSION (5):
        if certain_conversion_conditions:
            emit_error(2959)  # consteval on certain conversions
            entity[+177] &= 0xF9
```

## Stage 3: Declarator Parsing (declarator.c)

### Architecture

Declarator parsing uses inside-out construction: the C++ declarator syntax places the declared name in the center, with type constructors radiating outward (pointers to the left, arrays and function parameters to the right). The parser builds a derived-type chain that is later unwound against the base type from `decl_specifiers` to produce the final type.

```text
Declarator syntax (C++ grammar):
    declarator := pointer-declarator
    pointer-declarator := {*, &, &&, C::*} cv-qualifiers* direct-declarator
    direct-declarator := declarator-id | ( declarator ) | direct-declarator ( params ) | direct-declarator [ expr ]
    declarator-id := qualified-name | unqualified-name
```

The parser coordinates five specialized sub-parsers:

| Function | Address | Lines | Role |
|---|---|---|---|
| `declarator` | `sub_4B7BC0` | 284 | Top-level entry: dispatches to pointer/r_declarator |
| `r_declarator` | `sub_4BC950` | 2,578 | Recursive descent on direct-declarator |
| `pointer_declarator` | `sub_4B72A0` | 440 | `*`, `&`, `&&`, `::*` with cv-qualifiers |
| `array_declarator` | `sub_4B6760` | 518 | `[expr]` and `[]` |
| `function_declarator` | `sub_4B8190` | 3,144 | `(params) cv-quals -> ret noexcept` |

### scan_declarator_attributes (sub_4B3970) -- CUDA Attribute Separation

This is the critical function that separates CUDA execution space attributes from standard C++ attributes on declarators. In standard C++, attributes apply to the entity being declared. CUDA adds a parallel attribute dimension -- execution space -- that must be routed to a separate storage location.

The function iterates through the attribute list and sorts each attribute by its category byte at offset `+9`:

```python
scan_declarator_attributes(decl_info, attr_accumulator):
    attr_list = decl_info[+200]    # primary attribute list
    
    for each attr in attr_list:
        category = attr[+9]         # attribute category byte
        kind = attr[+8]             # attribute kind
        placement = attr[+10]       # where in declaration it appeared
        
        switch category:
            case 1:  # TYPE attribute (alignas, etc.)
                # Keep on primary list, set placement
                attr[+10] = 10      # after type specifier
                
            case 2:  # DECLARATION attribute ([[nodiscard]], etc.)
                if attr[+11] & 0x10:
                    # CUDA/vendor declaration attribute
                    route_to_vendor_list(attr)
                else:
                    # Standard declaration attribute
                    attr[+10] = 12  # before declarator
                
            case 3:  # STATEMENT attribute ([[fallthrough]], etc.)
                if decl_info[+131] & 8:  # class-key context
                    handle_class_key_stmt_attr(attr)
                
            case 4:  # CUDA EXECUTION SPACE attribute
                # __device__, __host__, __global__
                # Move to SECONDARY attribute list
                move_to_list(attr, decl_info[+184])
                
                # Error if misplaced
                if wrong_position:
                    emit_error(1847)  # attribute in wrong position
    
    # Mark all processed attributes
    for each attr in processed:
        attr[+11] |= 1    # set "consumed" flag
```

The separation into primary (offset `+200`) and secondary (offset `+184`) attribute lists is essential: downstream code (`decl_routine`, `decl_variable`) reads execution space from the secondary list and standard attributes from the primary list. This prevents CUDA execution space from interfering with standard attribute processing like `[[nodiscard]]` or `[[deprecated]]`.

### function_declarator (sub_4B8190, 3,144 lines)

The second-largest function in the declarator parser. It handles the complete C++ function declarator grammar including C++11 trailing return types, C++11/17 noexcept specifications, C++23 deducing `this`, and the C++ function qualifier trailer (`const`, `volatile`, `&`, `&&`).

```python
function_declarator(decl_info, context_flags):
    debug_trace(3, "function_declarator")
    
    # Parse parameter list
    expect_token('(')
    param_list = parse_parameter_list()
    expect_token(')')
    
    # C++ member function qualifiers
    cv_quals = 0
    while is_cv_qualifier(current_token):
        cv_quals |= cv_bit(current_token)
        advance_token()
    
    # Ref-qualifier (& or &&)
    ref_qual = NONE
    if current_token == '&':
        ref_qual = LVALUE_REF
        advance_token()
    elif current_token == '&&':
        ref_qual = RVALUE_REF
        advance_token()
    
    # Exception specification
    except_spec = NONE
    if current_token == TOKEN_THROW:
        except_spec = parse_throw_spec()
    elif current_token == TOKEN_NOEXCEPT:
        except_spec = parse_noexcept_spec()
    
    # C++11 trailing return type
    trailing_return = NULL
    if current_token == TOKEN_ARROW:   # ->
        advance_token()
        trailing_return = parse_type()
    
    # C++20 trailing requires clause
    requires_clause = NULL
    if current_token == TOKEN_REQUIRES:
        requires_clause = scan_trailing_requires_clause()
    
    # C++23 deducing this
    if has_explicit_this_parameter(param_list):
        mark_deducing_this()
    
    # Build function type node
    func_type = add_to_derived_type_list(
        FUNCTION_TYPE,
        param_list, cv_quals, ref_qual,
        except_spec, trailing_return, requires_clause
    )
    
    return func_type
```

### Derived Type Construction

`add_to_derived_type_list` (`sub_4B4CF0`, 600 lines) is the type-chain builder. Each declarator modifier (pointer, reference, array, function) appends a new node to a linked list. After parsing completes, `form_declared_type` (`sub_4B4870`) walks this chain bottom-up, applying each modifier to the base type to produce the final declared type.

For a declaration like `const int *(*fp)(double)`:

```text
Base type: const int
Derived chain: [function(double)] → [pointer] → [pointer]
Unwound: pointer to (pointer to function(double) returning const int)
```

## Stage 4: Declaration Processing (decls.c)

### decl_variable (sub_4CA6C0, 1,090 lines)

Processes variable declarations after specifiers and declarator have been parsed. This is where CUDA memory space qualifiers are applied and the variable entity is inserted into the symbol table.

#### CUDA Memory Space Bits

Variable entries carry a CUDA memory space bitmask at offset `+148`:

| Bit | Mask | Memory Space | Meaning |
|---|---|---|---|
| 0 | `0x01` | `__constant__` | Device-side constant memory |
| 1 | `0x02` | `__shared__` | Block-shared memory (per-SM) |
| 2 | `0x04` | `__managed__` | Unified memory (host + device accessible) |
| 4 | `0x10` | `__device__` | Device global memory |

These bits are set from the declaration state object (parameter `a2`), which carries the parsed CUDA attribute at offset `+240`:

```python
decl_variable(decl_specs, decl_state, storage_class, out_entity, out_flags):
    debug_trace(3, "decl_variable")
    assert(decl_state != NULL)              # decls.c:7730
    
    # Look up existing variable in scope
    existing = lookup_variable_in_scope(    # sub_4C84B0
        scope, name, type_info
    )
    
    # Create new variable entity
    var_entity = create_variable_entry(     # sub_5C9840
        name, type, storage_class
    )
    
    # Apply CUDA memory space from declaration state
    if dword_126EFA8:                       # CUDA mode enabled
        cuda_attr_ptr = decl_state[+240]
        if cuda_attr_ptr != NULL:
            # Extract memory space from attribute
            space = extract_memory_space(cuda_attr_ptr)
            var_entity[+148] = space        # set memory space bits
            
            # Scope walk: determine if variable is at namespace scope
            # or inside a function (affects valid memory space combinations)
            scope_idx = dword_126C5E4       # current scope index
            scope_base = qword_126C5E8      # scope table base
            while scope_idx > 0:
                scope_entry = scope_base + 784 * scope_idx
                scope_kind = scope_entry[+4]
                if scope_kind == 4:          # class scope — walk up
                    scope_idx = scope_entry[+256]  # parent scope
                    continue
                break
            
            # Template scope check
            if scope_entry[+9] & 0x20:       # is_template_scope
                handle_template_variable()
    
    # Check redeclaration compatibility
    if existing != NULL:
        old_space = existing[+148]
        new_space = var_entity[+148]
        if old_space != new_space:
            # Determine which string to use for error message
            if new_space & 0x04:
                space_name = "__managed__"
            elif new_space & 0x01:
                space_name = "__constant__"
            elif new_space & 0x02:
                space_name = "__shared__"
            elif new_space & 0x10:
                space_name = "__device__"
            emit_error(1306)      # CUDA memory space mismatch on redeclaration
    
    # Anonymous type check
    if type_is_anonymous(var_entity):
        emit_error(891)           # anonymous type in variable declaration
    
    # Apply remaining attributes
    set_variable_attributes(var_entity)     # sub_4C4750
```

### variable_declaration (sub_4DEC90, 1,098 lines) -- Top-Level Entry

This is the outermost entry point for processing a variable declaration. It wraps `decl_variable` with CUDA-specific validation, `constexpr`/`constinit` checks, and static data member definition handling.

#### CUDA-Specific Error Emission

The function contains a dense block of CUDA error checks for variable declarations:

```python
variable_declaration(decl_info, ...):
    # Early CUDA checks
    check_constexpr_variable_init(decl_info)    # sub_4DAC80
    
    # CUDA memory space string selection for error messages
    mem_space_bits = entity[+148]
    byte_149 = entity[+149]
    
    if mem_space_bits & 0x04:     # __managed__
        # No __managed__-specific string needed here
        pass
    
    # Build human-readable attribute name for diagnostics
    if byte_149 & 1:
        space_str = "__constant__"
    elif mem_space_bits & 4 == 0:
        space_str = "__managed__"
        if byte_149 & 1 == 0:
            space_str = "__device__"
            if mem_space_bits & 2:
                space_str = "__shared__"
    
    # CUDA variable constraint errors
    if is_shared_variable:
        if is_variable_length_array:
            emit_error(3510)      # __shared__ variable with VLA
    
    if is_constant_variable:
        if is_constexpr:
            emit_error(3568)      # __constant__ combined with constexpr
        if is_volatile:
            emit_error(3566)      # __constant__ combined with volatile
        if is_vla:
            emit_error(3567)      # __constant__ with VLA
    
    if has_cuda_attribute:
        if in_constexpr_if_discarded_branch:
            emit_error(3578)      # CUDA attribute in discarded branch
        if at_namespace_scope and is_structured_binding:
            emit_error(3579)      # CUDA attribute on structured binding
        if is_variable_length_array:
            emit_error(3580)      # CUDA attribute on VLA
    
    # Dispatch to decl_variable or define_static_data_member
    if is_static_member_definition:
        define_static_data_member(...)
    else:
        decl_variable(decl_specs, decl_state, storage_class, ...)
    
    # Post-declaration CUDA fixup
    cuda_variable_fixup(entity)     # sub_4CC150
    mark_defined_variable(entity)   # sub_4DC200
```

#### Complete CUDA Variable Error Table

| Error | Condition | Message Summary |
|---|---|---|
| 149 | Illegal CUDA storage class at namespace scope | Storage class not allowed here |
| 891 | Anonymous type in variable declaration | Anonymous type cannot be used |
| 892 | `auto`-typed CUDA variable (variant) | auto not allowed with CUDA qualifier |
| 893 | `auto`-typed CUDA variable | auto not allowed with CUDA qualifier |
| 1306 | Memory space mismatch on redeclaration | Conflicting CUDA memory space |
| 3483 | (CUDA variable context error) | CUDA attribute context mismatch |
| 3510 | `__shared__` variable with VLA | Variable-length arrays not allowed in `__shared__` |
| 3566 | `__constant__` with `volatile` | volatile incompatible with `__constant__` |
| 3567 | `__constant__` with VLA | Variable-length arrays not allowed in `__constant__` |
| 3568 | `__constant__` with `constexpr` | constexpr incompatible with `__constant__` |
| 3578 | CUDA attribute in `constexpr if` discarded branch | CUDA attribute in dead code |
| 3579 | CUDA attribute on structured binding at namespace scope | Structured binding cannot have CUDA attribute |
| 3580 | CUDA attribute on VLA | Variable-length arrays not allowed with CUDA attribute |
| 3648 | `__constant__` with external linkage | External `__constant__` not allowed |
| 1655 | Tentative definition of constexpr variable | Missing initializer |

### decl_routine (sub_4CE420, 2,858 lines)

The largest function in the declaration processing stage. It handles function and method declarations, integrating CUDA calling convention validation, attribute consistency checking, and template interaction.

#### Parameters

| Parameter | Offset | Description |
|---|---|---|
| `a1` | -- | `decl_specifiers` accumulator (`__m128i*`) |
| `a2` | -- | Declaration state object |
| `a3` | -- | Function info (offset `+64` = flags, `+80` = prior type) |
| `a4` | -- | SRK flags bitmask |
| `a5`--`a8` | -- | Output pointers and context |

#### SRK Flag Bits

The `a4` parameter carries "scan result kind" flags that describe what was parsed:

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `0x01` | `SRK_DECLARATION` -- forward declaration |
| 1 | `0x02` | `SRK_DEFINITION` -- has function body |
| 7 | `0x80` | `SRK_IMPLICIT` -- compiler-generated |
| 8 | `0x100` | `SRK_CONSTEXPR` -- constexpr function |

#### Function Entity Layout

After processing, a function entity contains:

| Offset | Size | Field | Description |
|---|---|---|---|
| `+80` | 1 | `entity_kind` | 10 = function, 11 = member function |
| `+88` | 8 | `descriptor` | Pointer to function descriptor |
| `+144` | 8 | `type` | Function type pointer |
| `+164` | 1 | `defined_flag` | Set when definition is seen |
| `+166` | 1 | `function_kind` | 1=ctor, 2=dtor, 5=conversion, 7=deduction guide |
| `+168` | 8 | `template_info` | Template instantiation info |
| `+177` | 1 | `attribute_flags` | bit 1=constexpr, bit 2=consteval |
| `+188` | 1 | `cuda_flags_1` | CUDA calling convention |
| `+189` | 1 | `cuda_flags_2` | CUDA execution space |
| `+192` | 8 | `parameter_list` | Head of parameter linked list |

#### Pseudocode

```python
decl_routine(decl_specs, decl_state, func_info, srk_flags, ...):
    debug_trace(3, "decl_routine")
    
    # Assertions
    assert func_info != NULL                    # decls.c:10057
    assert storage_class is valid               # decls.c:10059
    assert srk_flags & SRK_DECLARATION          # decls.c:10061
    assert func_type is routine type            # decls.c:10063
    if srk_flags & SRK_DEFINITION:
        assert body follows                     # decls.c:10068
    if srk_flags & SRK_IMPLICIT:
        assert compiler-generated context       # decls.c:10149
    
    # CUDA calling convention check
    if dword_126EFB4 == 2:                      # CUDA C++ mode
        check_cuda_calling_convention(          # sub_4C6AB0
            func_type, decl_specs
        )
        check_cuda_attribute_consistency(       # sub_4C6D50
            decl_state
        )
    
    # Look up existing declaration
    existing = find_linked_symbol(name, scope)
    
    if existing != NULL:
        # Redeclaration checks
        if existing.calling_convention != new_calling_convention:
            emit_error(948)         # calling convention mismatch
        
        if has_cuda_attribute(existing) and has_cuda_attribute(new):
            if not compatible_cuda_attributes(existing, new):
                emit_error(1430)    # function attribute mismatch
    
    # CUDA-specific restrictions
    if has_global_attribute:
        if return_type is auto:
            emit_error(1158)        # auto return type with __global__
    
    if is_deduction_guide:
        if has_any_cuda_attribute:
            emit_error(2885)        # CUDA attribute on deduction guide
    
    if is_explicit_instantiation:
        if conflicting_template_attributes:
            emit_error(1034)        # explicit instantiation conflict
    
    # Process CUDA attributes on the function
    process_cuda_attributes(decl_state)         # sub_42A250
    remove_cuda_trailing_return(decl_state)     # sub_42A210
    
    # Canonicalize trailing return type in CUDA mode
    if dword_126EFB4 == 2:
        canonicalize_return_type(func_type)      # sub_5DBCB0
    
    # Symbol table insertion
    entity = create_function_entity(name, func_type, storage_class)
    
    # Set defined flag
    assert entity.defined_flag is correct       # decls.c:10417
    
    # OpenMP variant handling (if active)
    if dword_106B4B8:                           # omp_declare_variant_active
        create_omp_variant_name("$$OMP_VARIANT%06d", variant_id)
```

## CUDA Attribute Integration

### Attribute Category System

EDG classifies attributes using a category byte at offset `+9` in the attribute node:

| Category | Value | Meaning | Examples |
|---|---|---|---|
| Type | 1 | Applies to the type | `alignas`, `__aligned__` |
| Declaration | 2 | Applies to the declaration | `[[nodiscard]]`, `[[deprecated]]` |
| Statement | 3 | Applies to a statement | `[[fallthrough]]`, `[[likely]]` |
| Execution space | 4 | CUDA execution space | `__device__`, `__host__`, `__global__` |

Category 4 is NVIDIA's addition to EDG's attribute system. Standard EDG uses categories 1-3. CUDA execution space attributes are recognized by the lexer as identifiers, classified as CUDA keywords by `get_token_main` (`sub_6810F0`) when `dword_106C2C0` (GPU mode) is active, and converted to attribute nodes with category 4 during attribute parsing.

### Attribute Node Layout

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `next` | Next attribute in linked list |
| `+8` | 1 | `kind` | Attribute kind (0 when cleared/consumed) |
| `+9` | 1 | `category` | 1=type, 2=decl, 3=stmt, 4=exec-space |
| `+10` | 1 | `placement` | Where in declaration it appeared (10=after type, 12=before declarator) |
| `+11` | 1 | `flags` | bit 0 = consumed, bit 4 = CUDA/vendor |
| `+16` | 8 | `payload` | Attribute-specific data |

### Execution Space Propagation

When a CUDA execution space attribute is parsed, it flows through three processing points:

1. **decl_specifiers** (`sub_4ACF80`): CUDA attributes are recognized as token 142 (attribute) and parsed into the attribute list. The attribute parser sets category 4 for execution space attributes.

2. **scan_declarator_attributes** (`sub_4B3970`): Separates category-4 attributes from the primary attribute list and moves them to the secondary list at offset `+184` of the declaration info structure.

3. **decl_routine / decl_variable**: Reads execution space from the secondary attribute list and applies it to the function/variable entity. For functions, the execution space goes to offsets `+188`/`+189` of the entity. For variables, the memory space goes to offset `+148`.

### warn_on_cuda_execution_space_attributes (sub_4A8990)

A safety valve that catches execution space attributes in places where they should not appear (e.g., on type definitions that are not function or variable declarations):

```python
warn_on_cuda_execution_space_attributes(attr_list):
    warned = false
    for each attr in attr_list:
        category = attr[+9]
        if category == 1 or category == 4:     # type or exec-space
            if not warned:
                emit_error(1882)               # invalid exec space attr
                warned = true
            attr[+8] = 0                       # clear kind (suppress further processing)
```

## Scope Chain and Context Tracking

The declaration parser relies heavily on the scope chain stored in the global scope table. Every declaration must be inserted at the correct scope, and many validation checks depend on whether the current scope is namespace-scope, class-scope, block-scope, or template-scope.

### Scope Entry Layout (784 bytes)

| Offset | Size | Field | Description |
|---|---|---|---|
| `+4` | 1 | `scope_kind` | 2=namespace, 4=class, 6=function, 8=nested block, 10=block, 12=template, 15/17=special |
| `+6` | 1 | `flags_1` | bit 1=extern, bit 2=inline namespace, bit 7=pending class flag |
| `+7` | 1 | `flags_2` | bit 1=has using directives |
| `+9` | 1 | `template_flags` | bit 5=is template scope, bit 1-3=template kind |
| `+12` | 4 | `scope_flags` | bit 2-3=scope modifier |
| `+182` | 1 | `cuda_flags` | bit 5 (`0x20`)=CUDA device-side scope |
| `+192` | 8 | `first_entity` | Head of entity linked list |
| `+216` | 8 | `type_pointer` | Associated type (for class scopes) |
| `+224` | 8 | `namespace_ptr` | Associated namespace |
| `+256` | 4 | `parent_scope` | Index of parent scope in table |
| `+368` | 8 | `source_begin` | Source position where scope begins |
| `+376` | 8 | `associated_entity` | Entity that opened this scope |
| `+408` | 4 | `parent_scope_idx` | Alternate parent scope index |

### Scope Table Globals

| Address | Name | Description |
|---|---|---|
| `qword_126C5E8` | `scope_table_base` | Array of 784-byte scope entries |
| `dword_126C5E4` | `current_scope_index` | Index into scope table |
| `dword_126C5DC` | `current_scope_id` | Current scope identifier |
| `dword_126C5B4` | `namespace_scope_id` | Nearest enclosing namespace scope |
| `dword_126C5BC` | `class_scope_depth` | Nesting depth of class scopes |
| `dword_126C5C4` | `lambda_scope_id` | Current lambda scope (-1 if none) |
| `dword_126C5C8` | `template_scope_id` | Current template scope (-1 if none) |

### Scope Walk for CUDA Memory Space

When processing a CUDA variable declaration, the parser walks up the scope chain to determine if the variable is at namespace scope (where `__device__`/`__constant__`/`__managed__` are valid) or inside a function body (where `__shared__` is additionally valid):

```python
determine_cuda_variable_scope(var_entity):
    scope_idx = dword_126C5E4
    scope_base = qword_126C5E8
    
    while scope_idx > 0:
        entry = scope_base + 784 * scope_idx
        kind = entry[+4]
        
        if kind == 4:                  # class scope
            # Walk through class scopes to find enclosing namespace/function
            scope_idx = entry[+256]    # parent scope
            continue
        
        if kind == 2:                  # namespace scope
            # Variable is at namespace scope
            # Valid spaces: __device__, __constant__, __managed__
            return NAMESPACE_SCOPE
        
        if kind == 6 or kind == 10:    # function or block scope
            # Variable is inside a function body
            # Valid spaces: __shared__, __device__, __constant__, __managed__
            return FUNCTION_SCOPE
        
        scope_idx = entry[+256]
    
    return FILE_SCOPE
```

## Linkage Determination

### id_linkage (sub_4C3380, 310 lines)

Determines whether an identifier has internal, external, or no linkage. This is called during `decl_variable` and `decl_routine` to set the linkage byte on the entity.

```python
id_linkage(entity, storage_class, scope):
    debug_trace(3, "id_linkage")
    
    kind = entity[+80]       # entity kind
    
    # C++ linkage rules
    if dword_126EFB4 == 2:    # C++ mode
        if storage_class == STATIC:
            return INTERNAL    # 0x10
        if storage_class == EXTERN:
            return EXTERNAL    # 0x20
        if scope_kind == NAMESPACE:
            if kind == FUNCTION:
                return EXTERNAL
            if kind == VARIABLE:
                if is_const_qualified and not explicitly_extern:
                    return INTERNAL
                return EXTERNAL
        if scope_kind == BLOCK:
            return NONE        # 0x00
    
    # C linkage rules (simpler)
    if storage_class == STATIC:
        return INTERNAL
    if scope_kind == FILE:
        return EXTERNAL
    
    return NONE
    
    # Debug output
    debug_print(linkage_string)   # "internal" / "external" / "none"
```

### find_linked_symbol (sub_4C1CC0, 608 lines)

The redeclaration detection engine. When a new declaration is processed, this function searches the current and enclosing scopes for a previously-declared symbol with the same name and compatible linkage:

```python
find_linked_symbol(name, scope, entity_kind):
    debug_trace(3, "find_linked_symbol")
    
    # Look up in symbol table
    existing = symbol_lookup(name, scope)    # sub_698940
    
    if existing == NULL:
        return NULL
    
    # For functions: handle overload sets
    if entity_kind == FUNCTION:
        # Walk overload set checking for compatible signature
        for each overload in existing.overload_set:
            if types_match(overload.type, new_type):
                return overload
        return NULL    # new overload, not redeclaration
    
    # For variables: check linkage compatibility
    if entity_kind == VARIABLE:
        if existing.linkage == new_linkage:
            return existing
        # Special case: extern at block scope refers to
        # namespace-scope variable with same name
        if new_storage_class == EXTERN and scope_kind == BLOCK:
            return walk_to_namespace_scope_and_search(name)
    
    return NULL
```

## Constructor and Destructor Initialization (decl_inits.c)

### ctor_inits_for_inheriting_ctor (sub_4A0310, 746 lines)

Builds the initialization sequence for inheriting constructors (C++11 `using Base::Base;`). The function iterates virtual base member lists to find matching base constructors and constructs the initialization order:

```python
ctor_inits_for_inheriting_ctor(decl_info):
    class_type = decl_info[+40][+32]    # enclosing class type
    member_list = class_type[+152]       # member list
    
    # Iterate virtual bases
    for each member in member_list:
        if member[+80] == 8:             # base class member kind
            base_type = resolve_base_type(member)
            base_ctor = find_base_constructor(base_type)
            
            if decl_info[+178] & 0x40:   # inheriting-ctor redirection
                # Walk class hierarchy via offset+216 link
                while has_redirect(current):
                    current = current[+216]
                base_ctor = find_redirect_target(current)
            
            # Check accessibility
            check_base_ctor_accessibility(base_ctor)   # sub_48B3F0
            
            # Build init entry
            init_entry = allocate_init_entry()          # sub_6BA0D0
            init_entry.target = base_ctor
            append_to_init_list(init_entry)
```

### dtor_initializer (sub_4A0EC0, 339 lines)

Builds the destructor initialization (destruction) list for a class. The destruction order is the reverse of construction order -- members are destroyed in reverse declaration order, then base classes in reverse order:

```python
dtor_initializer(decl_info):
    debug_trace(3, "dtor_initializer")       # decl_inits.c:10153
    
    class_type = decl_info[5][+32]
    member_list = class_type[+152]
    
    # Check for delegating constructor
    if decl_info[22] & 2:
        return    # delegating ctor, no separate dtor init needed
    
    # Pass 1: members with flag (offset[10] & 2)
    for each member in member_list:
        if member[10] & 2:
            if class_type[+132] != 11:       # not union
                dtor = resolve_member_destructor(member)
                entry = allocate_init_entry()
                entry.destructor = dtor
    
    # Pass 2: members with (offset[10] & 3) == 1
    for each member in member_list:
        if (member[10] & 3) == 1:
            dtor = resolve_member_destructor(member)
            entry = allocate_init_entry()
            entry.destructor = dtor
    
    # Base class destructors (reverse order)
    base_list = class_type[+96]
    for each base in reverse(base_list):
        dtor = resolve_base_destructor(base)   # sub_737270
        entry = allocate_init_entry()
        entry.destructor = dtor
```

### check_for_missing_initializer_full (sub_4A1540, 248 lines)

Checks whether a variable declaration is missing a required initializer:

```python
check_for_missing_initializer_full(entity, type, unused, deferred_error):
    kind = entity[+80]       # 7=variable, 9=static member
    
    # VLA check
    if is_variable_length_array(type):
        emit_error(252)       # VLA cannot have initializer
    
    # const check (C++ mode)
    if dword_126EFB4 == 2:    # C++ mode
        if is_const_qualified(type) and not has_initializer(entity):
            if not is_extern(entity):
                emit_error(257)   # const object requires initializer
    
    # Abstract class check
    if type[+160] & 2:        # abstract class flag
        if type[+132] & 0xFB == 8:    # array of abstract
            emit_error(812)   # array of abstract class
        else:
            emit_error(516)   # abstract class cannot be instantiated
    
    # constexpr check
    if entity has constexpr flag:
        if not has_initializer(entity):
            emit_error(517)   # constexpr variable requires initializer
```

## CUDA Mode Control Globals

The declaration parser is gated on several CUDA mode flags that control which code paths are active:

| Address | Name | Type | Description |
|---|---|---|---|
| `dword_126EFA8` | `is_cuda_compilation` | bool | Master CUDA mode flag |
| `dword_126EFB4` | `cuda_dialect` | int | 0=none, 1=C, 2=C++ |
| `dword_126EFAC` | `extended_cuda_features` | bool | Additional CUDA extensions enabled |
| `dword_126EFA4` | `cuda_host_compilation` | bool | Compiling host-side code |
| `dword_126EFB0` | `cuda_relaxed_constexpr` | bool | Allow `constexpr` on device functions |
| `dword_106C17C` | `constexpr_cuda_enabled` | bool | CUDA constexpr compatibility mode |
| `qword_126EF98` | `cuda_version_threshold_1` | int64 | Version gate (0x9E97 = 40599 = CUDA 12.x) |
| `qword_126EF90` | `cuda_version_threshold_2` | int64 | Version gate (0x78B3 = 30899 = CUDA 11.x) |
| `dword_126EF68` | `cpp_standard_version` | int | C++ standard year (201102, 201402, ...) |
| `dword_126EF64` | `cpp_extensions_enabled` | bool | Language extensions active |

### CUDA Version Gating

Several CUDA-specific code paths are guarded by version thresholds. The version values are encoded as `major * 1000 + minor * 10 + patch`:

```python
// CUDA 11.x and later: enable extended constexpr
if qword_126EF90 > 0x78B3:     // 30899 → CUDA version >= 11.x
    enable_extended_constexpr()

// CUDA 12.x and later: enable managed memory attributes
if qword_126EF98 > 0x9E97:     // 40599 → CUDA version >= 12.x
    enable_managed_attributes()

// Recent CUDA: enable namespace-scope CUDA variable checks
if qword_126EF98 > 0x1116F:    // 70000+ → very recent CUDA
    enable_strict_namespace_checks()
```

## Function Map

### decl_spec.c (0x4A1BF0--0x4B37F0)

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_4A1BF0` | `check_use_of_consteval` | 104 | Validate `consteval` specifier |
| `sub_4A1DF0` | `check_explicit_specifier` | 45 | Validate `explicit` specifier |
| `sub_4A1EC0` | `check_use_of_constinit` | 77 | Validate `constinit` specifier |
| `sub_4A2000` | `check_use_of_thread_local` | 111 | Validate `thread_local` specifier |
| `sub_4A22B0` | `check_use_of_constexpr` | 153 | Validate `constexpr` specifier |
| `sub_4A2580` | `check_gnu_c_auto_type` | 52 | Validate GNU `__auto_type` |
| `sub_4A2630` | `scan_edg_vector_type` | 203 | Parse vector type syntax |
| `sub_4A2B80` | `is_function_declaration_ahead` | 162 | Lookahead: function declaration? |
| `sub_4A2E40` | `process_auto_parameter` | 153 | C++20 auto parameters |
| `sub_4A31A0` | `process_storage_class_specifier` | 223 | Storage class validation |
| `sub_4A3610` | `check_for_class_modifiers` | 139 | Detect `final`/`__final` |
| `sub_4A38A0` | `scan_tag_name` | 1,216 | Parse class/enum name |
| `sub_4A4FD0` | `set_name_linkage_for_type` | 41 | Set type linkage |
| `sub_4A5140` | `update_membership_of_class` | 173 | Update class scope info |
| `sub_4A5510` | `attach_tag_attributes` | 143 | Attach attributes to types |
| `sub_4A57C0` | `class_specifier` | 2,179 | Parse class/struct/union definition |
| `sub_4A8990` | `warn_on_cuda_execution_space_attributes` | 33 | CUDA exec space warning |
| `sub_4A89F0` | `scan_enumerator_list` | 950 | Parse enum body |
| `sub_4AA2F0` | `enum_specifier` | 1,437 | Parse enum specifier |
| `sub_4AC550` | `typename_specifier` | 197 | Parse `typename T::type` |
| `sub_4AC970` | `is_constructor_decl` | 225 | Detect constructor declaration |
| `sub_4ACE00` | `enclosing_class_type` | 43 | Get enclosing class from scope |
| `sub_4ACF80` | `decl_specifiers` | 4,761 | **Central specifier dispatcher** |
| `sub_4B37F0` | `decl_spec_one_time_init` | 40 | Module initialization |

### declarator.c (0x4B3920--0x4C00A0)

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_4B3970` | `scan_declarator_attributes` | 297 | **Separate CUDA exec-space attrs** |
| `sub_4B3E80` | `scan_trailing_requires_clause` | 136 | C++20 requires clause |
| `sub_4B4230` | `check_for_restrict_qualifier_on_derived_type` | 124 | Restrict validation |
| `sub_4B4870` | `form_declared_type` | 53 | Combine base type + derived chain |
| `sub_4B4990` | `report_bad_return_type_qualifier` | 89 | cv-qual on return type |
| `sub_4B4CF0` | `add_to_derived_type_list` | 600 | Build derived type chain |
| `sub_4B5A70` | `delayed_scan_of_exception_spec` | 211 | Deferred exception spec |
| `sub_4B6760` | `array_declarator` | 518 | Parse `[expr]` |
| `sub_4B72A0` | `pointer_declarator` | 440 | Parse `*`, `&`, `&&`, `::*` |
| `sub_4B7BC0` | `declarator` | 284 | Top-level declarator entry |
| `sub_4B8190` | `function_declarator` | 3,144 | **Parse function signature** |
| `sub_4BC7F0` | `scan_requires_expr_parameters` | 61 | C++20 requires-expr params |
| `sub_4BC950` | `r_declarator` | 2,578 | Recursive descent declarator |
| `sub_4C00A0` | `scan_lambda_declarator` | 414 | Lambda declarator |

### decls.c (0x4C0840--0x4F0000)

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_4C0910` | `incompatible_types_are_SVR4_compatible` | 77 | SVR4 ABI compat check |
| `sub_4C0B10` | `set_default_calling_convention` | 112 | Calling convention setup |
| `sub_4C0CB0` | `record_overload` | 91 | Record function overload |
| `sub_4C0E90` | `set_linkage_for_class_members` | 107 | Propagate class linkage |
| `sub_4C10E0` | `set_linkage_environment` | 138 | Linkage environment setup |
| `sub_4C15D0` | `check_use_of_placeholder_type` | 175 | Validate `auto`/`decltype(auto)` |
| `sub_4C1CC0` | `find_linked_symbol` | 608 | Redeclaration detection |
| `sub_4C3380` | `id_linkage` | 310 | Linkage determination |
| `sub_4C3A80` | `qualified_name_redecl_sym` | 320 | Qualified redeclaration |
| `sub_4CA6C0` | `decl_variable` | 1,090 | **Variable declaration processing** |
| `sub_4CC150` | `cuda_variable_fixup` | 120 | CUDA post-decl variable fixup |
| `sub_4CE420` | `decl_routine` | 2,858 | **Function declaration processing** |
| `sub_4DAC80` | `check_constexpr_variable_init` | 60 | CUDA constexpr check |
| `sub_4DB440` | `process_asm_block` | 200 | Inline assembly declaration |
| `sub_4DC200` | `mark_defined_variable` | 26 | CUDA constexpr linkage |
| `sub_4DD710` | `check_trailing_return_type` | 80 | Auto type deduction check |
| `sub_4DEC90` | `variable_declaration` | 1,098 | **Top-level variable entry** |

### disambig.c (0x4E9E70--0x4EC690)

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_4E9E70` | `prescan_gnu_attribute` | 98 | Skip `__attribute__` in prescan |
| `sub_4EA560` | `prescan_declaration` | 400 | **Top-level disambiguation** |
| `sub_4EB270` | `prescan_declarator` | 200 | Prescan declarator tokens |
| `sub_4EC690` | `find_for_loop_separator` | 100 | Find `;` in for-init |

### decl_inits.c (0x4A0310--0x4A1BE0)

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_4A0310` | `ctor_inits_for_inheriting_ctor` | 746 | Inheriting ctor init list |
| `sub_4A0EC0` | `dtor_initializer` | 339 | Destructor init list |
| `sub_4A1540` | `check_for_missing_initializer_full` | 248 | Missing init diagnostic |
| `sub_4A1B60` | `decl_inits_init` | 11 | Module initialization |
| `sub_4A1BB0` | `decl_inits_reset` | 9 | Module reset |

## Cross-References

- [Lexer](lexer.md) -- token production, `word_126DD58`, `sub_676860` (`get_next_token`)
- [Template Engine](template-engine.md) -- template scope interaction during declarator parsing
- [CUDA Template Restrictions](template-cuda.md) -- `__global__` template argument validation, executed after `decl_routine`
- [Name Mangling](name-mangling.md) -- mangled name generation for declared entities
- [Overload Resolution](overload-resolution.md) -- overload set construction during `find_linked_symbol`
- [Constexpr Interpreter](constexpr-interpreter.md) -- invoked during `check_use_of_constexpr` for validation
