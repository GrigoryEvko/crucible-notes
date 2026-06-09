# Token Kind Table

Every token produced by cudafe++'s lexer carries a 16-bit token kind stored in the global `word_126DD58`. There are exactly 357 token kinds, numbered 0 through 356, with names indexed from a read-only string pointer table at `off_E6D240` in the `.rodata` segment. A parallel 357-entry byte array at `byte_E6C0E0` maps each token kind to an operator-name index, used by the `initialize_opname_kinds` routine (`sub_588BB0`) to populate the operator name display table at `qword_126DE00`. A boolean stop-token table at `qword_126DB48 + 8` (357 entries) marks which token kinds are valid synchronization points for error recovery in `skip_to_token` (`sub_6887C0`).

Token kind assignment follows a block scheme established by the EDG 6.6 frontend: operators and punctuation occupy the lowest range, followed by alternative tokens (C++ digraphs and named operators), C89 keywords, C99/C11 extensions, MSVC keywords, core C++ keywords, compiler internals, type-trait intrinsics, and finally the newest C++23/26 and extended-type additions at the top. CUDA-specific additions from NVIDIA occupy three dedicated slots (328--330) within the type-trait block, plus additional entries in the extended range. This ordering reflects the historical accretion of the C and C++ standards: each new standard appended its keywords at the end rather than filling gaps.

## Key Facts

| Property | Value |
|---|---|
| Total token kinds | 357 (indices 0--356) |
| Name table | `off_E6D240` (357 string pointers in `.rodata`) |
| Operator-to-name map | `byte_E6C0E0` (357-byte index array) |
| Operator name display table | `qword_126DE00` (48 string pointers, populated by `sub_588BB0`) |
| Stop-token table | `qword_126DB48 + 8` (357 boolean entries) |
| Current token global | `word_126DD58` (WORD) |
| Keyword registration function | `sub_5863A0` (`keyword_init`, 1,113 lines, `fe_init.c`) |
| Keyword entry function | `sub_7463B0` (`enter_keyword`) |
| GNU variant registration | `sub_585B10` (`enter_gnu_keyword`) |
| Alternative token entry | `sub_749600` (registers named operator alternative) |

## Token Kind Ranges

| Range | Count | Category | Description |
|---|---|---|---|
| 0 | 1 | Special | End-of-file / no-token sentinel |
| 1--31 | 31 | Operators and punctuation | Core operators (`+`, `-`, `*`, etc.) and delimiters (`(`, `)`, `{`, `}`, `;`) |
| 32--51 | 20 | Operators (continued) | Compound and remaining operators (`<<`, `>>`, `->`, `::`, `...`, `<=>`) |
| 52--76 | 25 | Alternative tokens / digraphs | C++ named operators (`and`, `or`, `not`) and digraphs (`<%`, `%>`, `<:`, `:>`) |
| 77--108 | 32 | C89 keywords | All keywords from ANSI C89/ISO C90 |
| 109--131 | 23 | C99/C11 keywords | `restrict`, `_Bool`, `_Complex`, `_Imaginary`, character types |
| 132--136 | 5 | MSVC keywords | `__declspec`, `__int8`--`__int64` |
| 137--199 | 63 | C++ keywords | Core C++ keywords plus C++11/14/17/20/23 additions |
| 200--206 | 7 | Compiler internal | Preprocessor and internal token kinds |
| 207--327 | 121 | Type trait intrinsics | `__is_xxx` / `__has_xxx` compiler intrinsic keywords |
| 328--330 | 3 | NVIDIA CUDA type traits | NVIDIA-specific lambda type-trait intrinsics |
| 331--356 | 26 | Extended types / recent additions | `_Float32`--`_Float128`, C++23/26 features, scalable vector types |

## Complete Token Table

### Operators and Punctuation (0--51)

These tokens are produced directly by the character-level scanner `sub_679800` (`scan_token`). Multi-character operators are resolved by dedicated scanning functions in the `0x67ABB0`--`0x67BAB0` range.

| Kind | Name | C/C++ Construct | Notes |
|---:|---|---|---|
| 0 | `<eof>` | End of file | Sentinel / no-token marker |
| 1 | `<identifier>` | Identifier | Any non-keyword identifier |
| 2 | `<integer literal>` | Integer constant | Decimal, hex, octal, or binary |
| 3 | `<floating literal>` | Floating-point constant | Float, double, or long double |
| 4 | `<character literal>` | Character constant | `'x'`, includes wide/u8/u16/u32 |
| 5 | `<string literal>` | String literal | `"..."`, includes wide/u8/u16/u32/raw |
| 6 | `;` | Semicolon | Statement terminator |
| 7 | `(` | Left parenthesis | Grouping, function call |
| 8 | `)` | Right parenthesis | |
| 9 | `,` | Comma | Separator, comma operator |
| 10 | `=` | Assignment | `a = b` |
| 11 | `{` | Left brace | Block/initializer open |
| 12 | `}` | Right brace | Block/initializer close |
| 13 | `+` | Plus | Addition, unary plus |
| 14 | `-` | Minus | Subtraction, unary minus |
| 15 | `*` | Star | Multiplication, pointer dereference, pointer declarator |
| 16 | `/` | Slash | Division |
| 17 | `<` | Less-than | Comparison, template open bracket |
| 18 | `>` | Greater-than | Comparison, template close bracket |
| 19 | `&` | Ampersand | Bitwise AND, address-of, reference declarator |
| 20 | `?` | Question mark | Ternary conditional |
| 21 | `:` | Colon | Label, ternary, bit-field width |
| 22 | `~` | Tilde | Bitwise complement, destructor |
| 23 | `%` | Percent | Modulo |
| 24 | `^` | Caret | Bitwise XOR |
| 25 | `[` | Left bracket | Array subscript, attributes `[[` |
| 26 | `.` | Dot | Member access |
| 27 | `]` | Right bracket | |
| 28 | `!` | Exclamation | Logical NOT |
| 29 | `\|` | Pipe | Bitwise OR |
| 30 | `->` | Arrow | Pointer member access |
| 31 | `++` | Increment | Pre/post increment |
| 32 | `--` | Decrement | Pre/post decrement |
| 33 | `==` | Equal | Equality comparison; also `bitand` alt-token for `&` |
| 34 | `!=` | Not-equal | Inequality comparison |
| 35 | `<=` | Less-or-equal | Comparison |
| 36 | `>=` | Greater-or-equal | Comparison |
| 37 | `<<` | Left shift | Also `compl` alt-token for `~` |
| 38 | `>>` | Right shift | Also `not` alt-token for `!` |
| 39 | `+=` | Add-assign | Compound assignment |
| 40 | `-=` | Subtract-assign | |
| 41 | `*=` | Multiply-assign | |
| 42 | `/=` | Divide-assign | |
| 43 | `%=` | Modulo-assign | |
| 44 | `<<=` | Left-shift-assign | |
| 45 | `>>=` | Right-shift-assign | |
| 46 | `&&` | Logical AND | Also address of rvalue reference |
| 47 | `\|\|` | Logical OR | |
| 48 | `^=` | XOR-assign | Also `not_eq` alt-token for `!=` |
| 49 | `&=` | AND-assign | |
| 50 | `\|=` | OR-assign | Also `xor` alt-token for `^` |
| 51 | `::` | Scope resolution | Also `bitor` alt-token for `\|` |

### Alternative Tokens and Digraphs (52--76)

C++ alternative tokens (ISO 14882 clause 5.5) and C/C++ digraphs. These are registered during `keyword_init` (`sub_5863A0`) via `sub_749600` when in C++ mode (`dword_126EFB4 == 2`).

| Kind | Name | Equivalent | Notes |
|---:|---|---|---|
| 52 | `and` | `&&` | Logical AND |
| 53 | `or` | `\|\|` | Logical OR |
| 54 | `->*` | `->*` | Pointer-to-member via pointer |
| 55 | `.*` | `.*` | Pointer-to-member via object |
| 56 | `...` | `...` | Ellipsis (variadic) |
| 57 | `<=>` | `<=>` | Three-way comparison (C++20) |
| 58 | `#` | `#` | Preprocessor stringification |
| 59 | `##` | `##` | Preprocessor token paste |
| 60 | `<%` | `{` | Digraph for left brace |
| 61 | `%>` | `}` | Digraph for right brace |
| 62 | `<:` | `[` | Digraph for left bracket |
| 63 | `:>` | `]` | Digraph for right bracket |
| 64 | `and_eq` | `&=` | Bitwise AND-assign |
| 65 | `xor_eq` | `^=` | Bitwise XOR-assign |
| 66 | `or_eq` | `\|=` | Bitwise OR-assign |
| 67 | `%:` | `#` | Digraph for hash |
| 68 | `%:%:` | `##` | Digraph for token paste |
| 69--76 | (reserved) | — | Reserved for future alternative tokens |

### C89 Keywords (77--108)

Always registered unconditionally. These form the base keyword set present in every compilation mode.

| Kind | Name | C/C++ Construct |
|---:|---|---|
| 77 | `auto` | Storage class (C89); type deduction (C++11) |
| 78 | `break` | Loop/switch exit |
| 79 | `case` | Switch case label |
| 80 | `char` | Character type |
| 81 | `const` | Const qualifier |
| 82 | `continue` | Loop continuation |
| 83 | `default` | Switch default label; defaulted function (C++11) |
| 84 | `do` | Do-while loop |
| 85 | `double` | Double-precision float |
| 86 | `else` | If-else branch |
| 87 | `enum` | Enumeration |
| 88 | `extern` | External linkage |
| 89 | `float` | Single-precision float |
| 90 | `for` | For loop |
| 91 | `goto` | Unconditional jump |
| 92 | `if` | Conditional |
| 93 | `int` | Integer type |
| 94 | `long` | Long integer modifier |
| 95 | `register` | Register storage hint (deprecated in C++17) |
| 96 | `return` | Function return |
| 97 | `short` | Short integer modifier |
| 98 | `signed` | Signed integer modifier |
| 99 | `sizeof` | Size query operator |
| 100 | `static` | Static storage / internal linkage |
| 101 | `struct` | Structure |
| 102 | `switch` | Multi-way branch |
| 103 | `typedef` | Type alias (C-style) |
| 104 | `union` | Union type |
| 105 | `unsigned` | Unsigned integer modifier |
| 106 | `void` | Void type |
| 107 | `volatile` | Volatile qualifier |
| 108 | `while` | While loop |

### C99/C11/C23 Keywords (109--131)

Gated on the C standard version at `dword_126EF68` (values: 199901 = C99, 201112 = C11, 202311 = C23).

| Kind | Name | Standard | C/C++ Construct |
|---:|---|---|---|
| 109 | `inline` | C99 | Inline function hint (already C++ keyword at 154) |
| 110--118 | (reserved) | — | — |
| 119 | `restrict` | C99 | Pointer restrict qualifier |
| 120 | `_Bool` | C99 | Boolean type (C-style) |
| 121 | `_Complex` | C99 | Complex number type |
| 122 | `_Imaginary` | C99 | Imaginary number type |
| 123--125 | (reserved) | — | — |
| 126 | `char16_t` | C++11/C23 | 16-bit character type |
| 127 | `char32_t` | C++11/C23 | 32-bit character type |
| 128 | `char8_t` | C++17/C23 | UTF-8 character type |
| 129--131 | (reserved) | — | — |

### MSVC Keywords (132--136)

Gated on `dword_126EFB0` (Microsoft extensions enabled, language mode 2/MSVC).

| Kind | Name | MSVC Construct |
|---:|---|---|
| 132 | `__declspec` | MSVC declaration specifier |
| 133 | `__int8` | 8-bit integer type |
| 134 | `__int16` | 16-bit integer type |
| 135 | `__int32` | 32-bit integer type |
| 136 | `__int64` | 64-bit integer type |

### C++ Core Keywords (137--199)

Gated on C++ mode (`dword_126EFB4 == 2`). Some keywords within this range were added in C++11 through C++23 and have additional standard-version gates.

| Kind | Name | Standard | C/C++ Construct |
|---:|---|---|---|
| 137 | `bool` | C++98 | Boolean type |
| 138 | `true` | C++98 | Boolean literal |
| 139 | `false` | C++98 | Boolean literal |
| 140 | `wchar_t` | C++98 | Wide character type |
| 141--149 | (reserved) | — | — |
| 142 | `__attribute` | GNU | GCC attribute syntax |
| 143 | `__builtin_types_compatible_p` | GNU | GCC type compatibility test |
| 144--149 | (reserved) | — | — |
| 150 | `catch` | C++98 | Exception handler |
| 151 | `class` | C++98 | Class definition |
| 152 | `delete` | C++98 | Deallocation; deleted function (C++11) |
| 153 | `friend` | C++98 | Friend declaration |
| 154 | `inline` | C++98 | Inline function/variable |
| 155 | `new` | C++98 | Allocation expression |
| 156 | `operator` | C++98 | Operator overload |
| 157 | `private` | C++98 | Access specifier |
| 158 | `protected` | C++98 | Access specifier |
| 159 | `public` | C++98 | Access specifier |
| 160 | `template` | C++98 | Template declaration |
| 161 | `this` | C++98 | Current object pointer |
| 162 | `throw` | C++98 | Throw expression |
| 163 | `try` | C++98 | Try block |
| 164 | `virtual` | C++98 | Virtual function/base |
| 165 | (reserved) | — | — |
| 166 | `const_cast` | C++98 | Const cast expression |
| 167 | `dynamic_cast` | C++98 | Dynamic cast expression |
| 168 | (reserved) | — | — |
| 169 | `export` | C++98/20 | Export declaration (original C++98, revived for modules in C++20) |
| 170 | `export` | C++20 | Module export (alternate registration slot) |
| 171--173 | (reserved) | — | — |
| 174 | `mutable` | C++98 | Mutable data member |
| 175 | `namespace` | C++98 | Namespace declaration |
| 176 | `reinterpret_cast` | C++98 | Reinterpret cast expression |
| 177 | `static_cast` | C++98 | Static cast expression |
| 178 | `typeid` | C++98 | Runtime type identification |
| 179 | `using` | C++98 | Using declaration/directive |
| 180--182 | (reserved) | — | — |
| 183 | `typename` | C++98 | Dependent type name |
| 184 | `static_assert` | C++11 | Static assertion; also `_Static_assert` in C11 |
| 185 | `decltype` | C++11 | Decltype specifier |
| 186 | `__auto_type` | GNU | GCC auto type extension |
| 187 | `__extension__` | GNU | GCC extension marker (suppress warnings) |
| 188 | (reserved) | — | — |
| 189 | `typeof` | C++23/GNU | Type-of expression |
| 190 | `typeof_unqual` | C++23 | Unqualified type-of expression |
| 191--193 | (reserved) | — | — |
| 194 | `thread_local` | C++11 | Thread-local storage; also `_Thread_local` in C11 |
| 195--199 | (reserved) | — | — |

### Compiler Internal Tokens (200--206)

These tokens are used internally by the preprocessor and the token cache. They never appear in user-visible diagnostics.

| Kind | Name | Purpose |
|---:|---|---|
| 200 | `<pp-number>` | Preprocessing number (not yet classified as integer or float) |
| 201 | `<header-name>` | Include file name (`<file>` or `"file"`) |
| 202 | `<newline>` | Logical newline token (preprocessor directive boundary) |
| 203 | `<whitespace>` | Whitespace token (preprocessing mode only) |
| 204 | `<placemarker>` | Token-paste placeholder (empty argument in `##`) |
| 205 | `<pragma>` | Pragma token (deferred for later processing) |
| 206 | `<end-of-directive>` | End of preprocessor directive |

### Type Trait Intrinsics (207--327)

These are compiler intrinsic keywords that implement the C++ type traits (from `<type_traits>`) without requiring template instantiation. They are registered during `keyword_init` with C++ standard version gating — earlier traits (C++11) are always available in C++ mode, while newer traits (C++20, C++23, C++26) require the corresponding standard version at `dword_126EF68`. Some traits are MSVC-specific (gated on `dword_126EFB0`) or Clang-specific (gated on `qword_126EF90`).

The complete list of type-trait intrinsics, organized alphabetically within each sub-category:

#### Unary Type Predicates

| Kind | Name | Standard | Tests Whether... |
|---:|---|---|---|
| 207 | `__is_class` | C++11 | Type is a class (not union) |
| 208 | `__is_enum` | C++11 | Type is an enumeration |
| 209 | `__is_union` | C++11 | Type is a union |
| 210 | `__is_pod` | C++11 | Type is POD (plain old data) |
| 211 | `__is_empty` | C++11 | Type has no non-static data members |
| 212 | `__is_polymorphic` | C++11 | Type has at least one virtual function |
| 213 | `__is_abstract` | C++11 | Type has at least one pure virtual function |
| 214 | `__is_literal_type` | C++11 | Type is a literal type (deprecated C++17) |
| 215 | `__is_standard_layout` | C++11 | Type is standard-layout |
| 216 | `__is_trivial` | C++11 | Type is trivially copyable and has trivial default constructor |
| 217 | `__is_trivially_copyable` | C++11 | Type is trivially copyable |
| 218 | `__is_final` | C++14 | Class is marked `final` |
| 219 | `__is_aggregate` | C++17 | Type is an aggregate |
| 220 | `__has_virtual_destructor` | C++11 | Type has a virtual destructor |
| 221 | `__has_trivial_constructor` | C++11 | Type has a trivial default constructor |
| 222 | `__has_trivial_copy` | C++11 | Type has a trivial copy constructor |
| 223 | `__has_trivial_assign` | C++11 | Type has a trivial copy assignment |
| 224 | `__has_trivial_destructor` | C++11 | Type has a trivial destructor |
| 225 | `__has_nothrow_constructor` | C++11 | Default constructor is noexcept |
| 226 | `__has_nothrow_copy` | C++11 | Copy constructor is noexcept |
| 227 | `__has_nothrow_assign` | C++11 | Copy assignment is noexcept |
| 228 | `__has_trivial_move_constructor` | C++11 | Type has a trivial move constructor |
| 229 | `__has_trivial_move_assign` | C++11 | Type has a trivial move assignment |
| 230 | `__has_nothrow_move_assign` | C++11 | Move assignment is noexcept |
| 231 | `__has_unique_object_representations` | C++17 | Type has unique object representations |
| 232 | `__is_signed` | C++11 | Type is a signed arithmetic type |
| 233 | `__is_unsigned` | C++11 | Type is an unsigned arithmetic type |
| 234 | `__is_integral` | C++11 | Type is an integral type |
| 235 | `__is_floating_point` | C++11 | Type is a floating-point type |
| 236 | `__is_arithmetic` | C++11 | Type is an arithmetic type |
| 237 | `nullptr` | C++11 | Null pointer literal (not a trait; shares range) |
| 238 | `__is_fundamental` | C++11 | Type is a fundamental type |
| 239 | `__int128` | GNU | 128-bit integer type (not a trait; shares range) |
| 240 | `__is_scalar` | C++11 | Type is a scalar type |
| 241 | `__is_object` | C++11 | Type is an object type |
| 242 | `__is_compound` | C++11 | Type is a compound type |
| 243 | `__is_reference` | C++11 | Type is an lvalue or rvalue reference |
| 244 | `constexpr` | C++11 | Constexpr specifier (not a trait; shares range) |
| 245 | `consteval` | C++20 | Consteval specifier (not a trait; shares range) |
| 246 | `constinit` | C++20 | Constinit specifier (not a trait; shares range) |
| 247 | `_Alignof` | C11 | Alignment query (C11 spelling) |
| 248 | `_Alignas` | C11 | Alignment specifier (C11 spelling) |
| 249 | `__bases` | GCC | Direct base classes (GCC extension) |
| 250 | `__direct_bases` | GCC | Non-virtual direct base classes (GCC extension) |
| 251 | `__builtin_arm_ldrex` | Clang | ARM load-exclusive intrinsic |
| 252 | `__builtin_arm_ldaex` | Clang | ARM load-acquire-exclusive intrinsic |
| 253 | `__builtin_arm_addg` | Clang | ARM MTE add-tag intrinsic |
| 254 | `__builtin_arm_irg` | Clang | ARM MTE insert-random-tag intrinsic |
| 255 | `__builtin_arm_ldg` | Clang | ARM MTE load-tag intrinsic |
| 256 | `__is_member_pointer` | C++11 | Type is a pointer to member |
| 257 | `__is_member_function_pointer` | C++11 | Type is a pointer to member function |
| 258 | `__builtin_shufflevector` | Clang | Clang vector shuffle intrinsic |
| 259 | `__builtin_convertvector` | Clang | Clang vector conversion intrinsic |
| 260 | `_Noreturn` | C11 | No-return function specifier |
| 261 | `__builtin_complex` | GNU | GCC complex number construction |
| 262 | `_Generic` | C11 | Generic selection expression |
| 263 | `_Atomic` | C11 | Atomic type qualifier/specifier |
| 264 | `_Nullable` | Clang | Nullable pointer qualifier |
| 265 | `_Nonnull` | Clang | Non-null pointer qualifier |
| 266 | `_Null_unspecified` | Clang | Null-unspecified pointer qualifier |
| 267 | `co_yield` | C++20 | Coroutine yield expression |
| 268 | `co_return` | C++20 | Coroutine return statement |
| 269 | `co_await` | C++20 | Coroutine await expression |
| 270 | `__is_member_object_pointer` | C++11 | Type is a pointer to data member |
| 271 | `__builtin_addressof` | GNU | Address-of without operator overload |

#### EDG Internal Keywords (272--283)

These are not user-facing keywords. They are injected by the EDG frontend into synthesized declarations for built-in types, throw specifications, and vector types.

| Kind | Name | Purpose |
|---:|---|---|
| 272 | `__edg_type__` | EDG internal type placeholder |
| 273 | `__edg_vector_type__` | SIMD vector type (GCC `__attribute__((vector_size))` lowering) |
| 274 | `__edg_neon_vector_type__` | ARM NEON vector type |
| 275 | `__edg_scalable_vector_type__` | ARM SVE scalable vector type |
| 276 | `__edg_neon_polyvector_type__` | ARM NEON polynomial vector type |
| 277 | `__edg_size_type__` | Placeholder for `size_t` before it is typedef'd |
| 278 | `__edg_ptrdiff_type__` | Placeholder for `ptrdiff_t` before it is typedef'd |
| 279 | `__edg_bool_type__` | Placeholder for `bool` / `_Bool` |
| 280 | `__edg_wchar_type__` | Placeholder for `wchar_t` |
| 281 | `__edg_throw__` | Throw specification in synthesized declarations |
| 282 | `__edg_opnd__` | Operand reference in synthesized expressions |
| 283 | (reserved) | — |

#### More Type Predicates and Binary Traits (284--327)

| Kind | Name | Standard | Tests Whether... |
|---:|---|---|---|
| 284 | `__is_const` | C++11 | Type is const-qualified |
| 285 | `__is_volatile` | C++11 | Type is volatile-qualified |
| 286 | `__is_void` | C++11 | Type is `void` |
| 287 | `__is_array` | C++11 | Type is an array |
| 288 | `__is_pointer` | C++11 | Type is a pointer |
| 289 | `__is_lvalue_reference` | C++11 | Type is an lvalue reference |
| 290 | `__is_rvalue_reference` | C++11 | Type is an rvalue reference |
| 291 | `__is_function` | C++11 | Type is a function type |
| 292 | `__is_constructible` | C++11 | Type is constructible from given args |
| 293 | `__is_nothrow_constructible` | C++11 | Construction is noexcept |
| 294 | `requires` | C++20 | Requires expression/clause |
| 295 | `concept` | C++20 | Concept definition |
| 296 | `__builtin_has_attribute` | GNU | Tests if declaration has given attribute |
| 297 | `__builtin_bit_cast` | C++20 | Bit cast intrinsic (`std::bit_cast` implementation) |
| 298 | `__is_assignable` | C++11 | Type is assignable from given type |
| 299 | `__is_nothrow_assignable` | C++11 | Assignment is noexcept |
| 300 | `__is_trivially_constructible` | C++11 | Construction is trivial |
| 301 | `__is_trivially_assignable` | C++11 | Assignment is trivial |
| 302 | `__is_destructible` | C++11 | Type is destructible |
| 303 | `__is_nothrow_destructible` | C++11 | Destruction is noexcept |
| 304 | `__edg_is_deducible` | EDG | EDG internal: template argument is deducible |
| 305 | `__is_trivially_destructible` | C++11 | Destruction is trivial |
| 306 | `__is_base_of` | C++11 | First type is base of second (binary trait) |
| 307 | `__is_convertible` | C++11 | First type is convertible to second (binary trait) |
| 308 | `__is_same` | C++11 | Two types are the same (binary trait) |
| 309 | `__is_trivially_copy_assignable` | C++11 | Copy assignment is trivial |
| 310 | `__is_assignable_no_precondition_check` | EDG | Assignable without precondition validation |
| 311 | `__is_same_as` | Clang | Alias for `__is_same` (Clang compatibility) |
| 312 | `__is_referenceable` | C++11 | Type can be referenced |
| 313 | `__is_bounded_array` | C++20 | Type is a bounded array |
| 314 | `__is_unbounded_array` | C++20 | Type is an unbounded array |
| 315 | `__is_scoped_enum` | C++23 | Type is a scoped enumeration |
| 316 | `__is_literal` | C++11 | Alias for `__is_literal_type` |
| 317 | `__is_complete_type` | EDG | Type is complete (not forward-declared) |
| 318 | `__is_nothrow_convertible` | C++20 | Conversion is noexcept (binary trait) |
| 319 | `__is_convertible_to` | MSVC | MSVC alias for `__is_convertible` |
| 320 | `__is_invocable` | C++17 | Callable with given arguments |
| 321 | `__is_nothrow_invocable` | C++17 | Call is noexcept |
| 322 | `__is_trivially_equality_comparable` | Clang | Bitwise equality is equivalent |
| 323 | `__is_layout_compatible` | C++20 | Types have compatible layouts |
| 324 | `__is_pointer_interconvertible_base_of` | C++20 | Pointer-interconvertible base (binary trait) |
| 325 | `__is_corresponding_member` | C++20 | Corresponding members in layout-compatible types |
| 326 | `__is_pointer_interconvertible_with_class` | C++20 | Member pointer is interconvertible with class pointer |
| 327 | `__is_trivially_relocatable` | C++26 | Type can be trivially relocated |

### NVIDIA CUDA Type Traits (328--330)

Three NVIDIA-specific type-trait intrinsics occupy dedicated token kinds. These are registered during `keyword_init` when GPU mode is active (`dword_106C2C0 != 0`) and participate in the same token classification pipeline as all other type traits. They are used internally by the CUDA frontend to detect extended lambda closure types during device/host separation.

| Kind | Name | Purpose |
|---:|---|---|
| 328 | `__nv_is_extended_device_lambda_closure_type` | Tests whether a type is the closure type of an extended device lambda. Used during device code generation to identify lambda closures that require special treatment (wrapper function generation, address-space conversion). |
| 329 | `__nv_is_extended_host_device_lambda_closure_type` | Tests whether a type is the closure type of an extended host-device lambda (`__host__ __device__`). These lambdas require dual code generation paths and wrapper functions for both host and device. |
| 330 | `__nv_is_extended_device_lambda_with_preserved_return_type` | Tests whether a device lambda has an explicitly specified (preserved) return type rather than a deduced one. Affects how the compiler generates the wrapper function return type. |

When extended lambdas are disabled, these traits are predefined as macros expanding to `false`:

```c
// Fallback definitions in preprocessor preamble:
#define __nv_is_extended_device_lambda_closure_type(X) false
#define __nv_is_extended_host_device_lambda_closure_type(X) false
#define __nv_is_extended_device_lambda_with_preserved_return_type(X) false
```

### Extended Types and Recent Additions (331--356)

These are the newest token kinds, added for extended floating-point types (ISO/IEC TS 18661-3) and recent C++23/26 features.

| Kind | Name | Standard | C/C++ Construct |
|---:|---|---|---|
| 331 | `_Float32` | TS 18661-3 | 32-bit IEEE 754 float |
| 332 | `_Float32x` | TS 18661-3 | Extended 32-bit float |
| 333 | `_Float64` | TS 18661-3 | 64-bit IEEE 754 float |
| 334 | `_Float64x` | TS 18661-3 | Extended 64-bit float |
| 335 | `_Float128` | TS 18661-3 | 128-bit IEEE 754 float |
| 336--340 | (reserved) | — | — |
| 341--356 | (recent additions) | C++23/26 | Reserved for MSVC C++/CLI traits (`__is_ref_class`, `__is_value_class`, `__is_interface_class`, `__is_delegate`, `__is_sealed`, `__has_finalizer`, `__has_copy`, `__has_assign`, `__is_simple_value_class`, `__is_ref_array`, `__is_valid_winrt_type`, `__is_win_class`, `__is_win_interface`) and additional future extensions |

## Token Cache

The token cache provides lookahead, backtracking, and macro-expansion replay for C++ parsing. Tokens are stored in a linked list of cache entries, each 80--112 bytes depending on payload.

### Cache Entry Layout

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `next` | Next entry in linked list |
| `+8` | 8 | `source_position` | Encoded file/line/column |
| `+16` | 2 | `token_code` | Token kind (0--356) |
| `+18` | 1 | `cache_entry_kind` | Payload discriminator (see table below) |
| `+20` | 4 | `flags` | Token classification flags |
| `+24` | 4 | `extra_flags` | Additional flags |
| `+32` | 8 | `extra_data` | Context-dependent data |
| `+40`.. | varies | `payload` | Kind-specific data (40--72 bytes) |

### Cache Entry Kinds

Eight discriminator values select the payload interpretation at offset `+40`:

| Kind | Value | Payload Content | Size | Description |
|---:|---:|---|---|---|
| identifier | 1 | Name pointer + 64-byte lookup result | 72 | Identifier with pre-resolved scope/symbol lookup. The 64-byte lookup result mirrors `xmmword_106C380`--`106C3B0`. |
| macro_def | 2 | Macro definition pointer | 8 | Reference to a macro definition for re-expansion. Dispatched to `sub_5BA500`. |
| pragma | 3 | Pragma data | varies | Preprocessor pragma deferred for later processing |
| pp_number | 4 | Number text pointer | 8 | Preprocessing number not yet classified as integer or float |
| (reserved) | 5 | — | — | Not observed in use |
| string | 6 | String data + encoding byte | varies | String literal with encoding prefix information |
| (reserved) | 7 | — | — | Not observed in use |
| concatenated_string | 8 | Concatenated string data | varies | Wide or multi-piece concatenated string literal |

### Cache Management Globals

| Address | Name | Description |
|---|---|---|
| `qword_1270150` | `cached_token_rescan_list` | Head of list of tokens to re-scan (pushed back for lookahead) |
| `qword_1270128` | `reusable_cache_stack` | Stack of reusable cache entry blocks |
| `qword_1270148` | `free_token_list` | Free list for recycling cache entries |
| `qword_1270140` | `macro_definition_chain` | Active macro definition chain |
| `qword_1270118` | `cache_entry_free_list` | Free list for `allocate_token_cache_entry` |
| `dword_126DB74` | `has_cached_tokens` | Boolean: nonzero when cache is non-empty |

### Cache Operations

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_669650` | `copy_tokens_from_cache` | 385 | Copies cached preprocessor tokens for macro re-expansion (assert at `lexical.c:3417`) |
| `sub_669D00` | `allocate_token_cache_entry` | 119 | Allocates from free list at `qword_1270118`, initializes fields |
| `sub_669EB0` | `create_cached_token_node` | 83 | Creates and initializes cache node with source position |
| `sub_66A000` | `append_to_token_cache` | 88 | Appends token to cache list, maintains tail pointer |
| `sub_66A140` | `push_token_to_rescan_list` | 46 | Pushes token onto rescan stack at `qword_1270150` |
| `sub_66A2C0` | `free_single_cache_entry` | 18 | Returns cache entry to free list |

## Keyword Registration

All keywords are registered during frontend initialization by `sub_5863A0` (`keyword_init` / `fe_translation_unit_init`, 1,113 lines, in `fe_init.c`). The function calls `sub_7463B0` (`enter_keyword`) for each keyword, passing the numeric token kind and the keyword string. GNU double-underscore variants (e.g., `__asm` and `__asm__` for `asm`) are registered via `sub_585B10` (`enter_gnu_keyword`), which automatically generates both `__name` and `__name__` forms from a single root. Alternative tokens are registered via `sub_749600`.

### Version Gating Architecture

Registration is conditional on a set of global configuration flags established during CLI processing:

| Address | Name | Controls | Values |
|---|---|---|---|
| `dword_126EFB4` | `language_mode` | C vs C++ dialect | `1` = C (GNU default), `2` = C++ |
| `dword_126EF68` | `cpp_standard_version` | Standard version level | `199711` (C++98), `201103` (C++11), `201402` (C++14), `201703` (C++17), `202002` (C++20), `202302` (C++23) |
| `dword_126EFAC` | `c_language_mode` | C mode flag | Boolean |
| `dword_126EFB0` | `microsoft_extensions` | MSVC keywords | Boolean |
| `dword_126EFA8` | `gnu_extensions` | GCC keywords | Boolean |
| `dword_126EFA4` | `clang_extensions` | Clang keywords | Boolean |
| `qword_126EF98` | `gnu_version` | GCC version threshold | Encoded: e.g., `0x9FC3` = GCC 4.0.3 |
| `qword_126EF90` | `clang_version` | Clang version threshold | Encoded: e.g., `0x15F8F`, `0x1D4BF` |

### Registration Pattern

The pseudocode below shows the version-gated registration pattern reconstructed from `sub_5863A0`:

```c
void keyword_init(void) {
    // C89 keywords -- always registered
    enter_keyword(77, "auto");
    enter_keyword(78, "break");
    enter_keyword(79, "case");
    // ... all C89 keywords ...
    enter_keyword(108, "while");

    // C99 keywords -- gated on C99+ standard
    if (c_standard_version >= 199901) {
        enter_keyword(119, "restrict");
        enter_keyword(120, "_Bool");
        enter_keyword(121, "_Complex");
        enter_keyword(122, "_Imaginary");
    }

    // C11 keywords
    if (c_standard_version >= 201112) {
        enter_keyword(184, "_Static_assert");
        enter_keyword(247, "_Alignof");
        enter_keyword(248, "_Alignas");
        enter_keyword(260, "_Noreturn");
        enter_keyword(262, "_Generic");
        enter_keyword(263, "_Atomic");
        enter_keyword(194, "_Thread_local");
    }

    // C++ mode keywords
    if (language_mode == 2) {  // C++ mode
        enter_keyword(137, "bool");
        enter_keyword(138, "true");
        enter_keyword(139, "false");
        enter_keyword(140, "wchar_t");
        enter_keyword(150, "catch");
        enter_keyword(151, "class");
        // ... all C++ core keywords ...
        enter_keyword(183, "typename");

        // Alternative tokens (C++ only)
        enter_alt_token(52, "and", /*len*/3);
        enter_alt_token(53, "or", 2);
        enter_alt_token(64, "and_eq", 6);
        // ... all alternative tokens ...

        // C++11 keywords
        if (cpp_standard_version >= 201103) {
            enter_keyword(244, "constexpr");
            enter_keyword(185, "decltype");
            enter_keyword(237, "nullptr");
            enter_keyword(126, "char16_t");
            enter_keyword(127, "char32_t");
            enter_keyword(184, "static_assert");
            enter_keyword(194, "thread_local");
        }

        // C++20 keywords
        if (cpp_standard_version >= 202002) {
            enter_keyword(245, "consteval");
            enter_keyword(246, "constinit");
            enter_keyword(267, "co_yield");
            enter_keyword(268, "co_return");
            enter_keyword(269, "co_await");
            enter_keyword(294, "requires");
            enter_keyword(295, "concept");
        }
    }

    // GNU extensions -- gated on gnu_extensions flag
    if (gnu_extensions) {
        enter_gnu_keyword(187, "__extension__");
        enter_gnu_keyword(186, "__auto_type");
        enter_gnu_keyword(142, "__attribute");
        enter_keyword(117, "__builtin_offsetof");
        enter_keyword(143, "__builtin_types_compatible_p");
        enter_keyword(239, "__int128");
        // ... all GNU extensions ...
    }

    // MSVC extensions
    if (microsoft_extensions) {
        enter_keyword(132, "__declspec");
        enter_keyword(133, "__int8");
        enter_keyword(134, "__int16");
        enter_keyword(135, "__int32");
        enter_keyword(136, "__int64");
    }

    // Type traits (C++11+, ~60 traits)
    if (language_mode == 2) {
        enter_keyword(207, "__is_class");
        enter_keyword(208, "__is_enum");
        // ... all type traits through 327 ...
    }

    // CUDA type traits (GPU mode)
    if (gpu_mode) {
        enter_keyword(328, "__nv_is_extended_device_lambda_closure_type");
        enter_keyword(329, "__nv_is_extended_host_device_lambda_closure_type");
        enter_keyword(330, "__nv_is_extended_device_lambda_with_preserved_return_type");
    }

    // Extended float types (GNU)
    if (gnu_extensions) {
        enter_keyword(331, "_Float32");
        enter_keyword(332, "_Float32x");
        enter_keyword(333, "_Float64");
        enter_keyword(334, "_Float64x");
        enter_keyword(335, "_Float128");
    }

    // Post-keyword init: scope setup, builtin registration
    // ...
}
```

### GNU Double-Underscore Registration

`sub_585B10` (`enter_gnu_keyword`, assert at `fe_init.c:698`) implements the pattern where a single keyword `name` is registered in two or three forms:

- If `name` starts with `_`: registers `name` as-is and `__name__` (e.g., `_Bool` stays, plus `___Bool__` if applicable)
- Otherwise: registers `__name` and `__name__` (e.g., `asm` produces `__asm` and `__asm__`)

The function uses a stack buffer of 49 characters maximum (`name + 5 <= 0x31`), prepends `__` (encoded as `0x5F5F` in little-endian), copies the name, and appends `__` with a null terminator. Both variants call `sub_7463B0` (`enter_keyword`) with the same token kind.

## Operator Name Table

The operator name display table at `qword_126DE00` maps operator kinds to printable names for diagnostics and error messages. It is populated by `sub_588BB0` (`initialize_opname_kinds`) during `fe_wrapup.c` initialization.

The initialization loop iterates all 357 entries of `byte_E6C0E0` (operator-to-name index), mapping each non-zero entry to the corresponding string from `off_E6D240` (the token name table). Two special cases are hardcoded:

| Operator Kind | Display Name | Special Case |
|---|---|---|
| 42 | `()` | Function call operator (overridden from default) |
| 43 | `[]` | Array subscript operator (overridden from default) |

Additionally, the array positions for `new[]` and `delete[]` are hardcoded separately, since these operator names do not correspond to single tokens.

The routine validates that all entries in the range `qword_126DE08` through `qword_126DF80` (the 48 operator name slots) are non-null, and panics with `"initialize_opname_kinds: bad init of opname_names"` if any gap is found.

## Token State Globals

When a token is produced by the lexer, the following globals are populated:

| Address | Name | Type | Description |
|---|---|---|---|
| `word_126DD58` | `current_token_code` | WORD | 16-bit token kind (0--356) |
| `qword_126DD38` | `current_source_position` | QWORD | Encoded file/line/column |
| `qword_126DD48` | `token_text_ptr` | QWORD | Pointer to identifier/literal text |
| `src` | `token_start_position` | char* | Start of token in input buffer |
| `n` | `token_text_length` | size_t | Length of token text |
| `dword_126DF90` | `token_flags_1` | DWORD | Classification flags |
| `dword_126DF8C` | `token_flags_2` | DWORD | Additional flags |
| `qword_126DF80` | `token_extra_data` | QWORD | Context-dependent payload |
| `xmmword_106C380`--`106C3B0` | `identifier_lookup_result` | 4 x 128-bit | SSE-packed lookup result (64 bytes, 4 XMM registers) |

## Cross-References

- [Lexer & Tokenizer](../edg/lexer.md) — full lexer subsystem documentation, architecture, and function map
- [Pipeline Overview](../pipeline/overview.md) — keyword registration during initialization
- [Entry Point & Initialization](../pipeline/entry.md) — `keyword_init` in the startup sequence
- [Global Variable Index](global-variables.md) — all global addresses referenced here
- [Template Engine](../edg/template-engine.md) — template argument scanning and `>>` disambiguation
- [CUDA Lambda Overview](../lambdas/overview.md) — NVIDIA type-trait token usage in lambda transforms
- [Attribute System Overview](../attributes/overview.md) — CUDA attribute handling at token level
- [EDG Source File Map](edg-source-map.md) — `lexical.c` and `fe_init.c` binary layout
