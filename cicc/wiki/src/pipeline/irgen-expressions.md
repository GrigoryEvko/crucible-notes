# Expression & Constant Codegen

The central expression emitter `sub_128D0F0` (56 KB, 1751 decompiled lines) is the single function responsible for translating every C/C++ expression in the EDG AST into LLVM IR. It is a large recursive two-level switch: the **outer switch** classifies the expression node kind (operation, literal, member access, call, etc.), and the **inner switch** dispatches across 40+ C operators to emit the corresponding LLVM IR instruction sequences. Every named temporary in the output (`%arraydecay`, `%land.ext`, `%sub.ptr.div`, `%cond`, etc.) originates from explicit `SetValueName` calls within this function, closely mirroring Clang's IRGen naming conventions.

Two companion subsystems handle specialized expression domains: **bitfield codegen** (`sub_1282050` store, `sub_1284570` load) lowers C bitfield accesses to shift/mask/or sequences, and **constant expression codegen** (`sub_127D8B0`, 1273 lines) produces `llvm::Constant*` values for compile-time evaluable expressions. **Cast codegen** (`sub_128A450`, 669 lines) maps every C cast category to the appropriate LLVM cast opcode.

| | |
|---|---|
| **Master dispatcher** | `sub_128D0F0` — `EmitExpr` (56 KB, address `0x128D0F0`) |
| **Bitfield store** | `sub_1282050` — `EmitBitfieldStore` (15 args, R-M-W sequence) |
| **Bitfield load** | `sub_1284570` — `EmitBitfieldLoad` (12 args, extract sequence) |
| **Constant expressions** | `sub_127D8B0` — `EmitConstExpr` (1273 lines, recursive) |
| **Cast/conversion** | `sub_128A450` — `EmitCast` (669 lines, 11 LLVM opcodes) |
| **Bool conversion** | `sub_127FEC0` — `EmitBoolExpr` (expr to `i1`) |
| **Literal emission** | `sub_127F650` — `EmitLiteral` (numeric/string constants) |

## Master Expression Dispatcher

### Reconstructed signature

```c
// sub_128D0F0
llvm::Value *EmitExpr(CodeGenState **ctx, EDGExprNode *expr,
                      llvm::Type *destTy, unsigned flags, unsigned flags2);
```

The `ctx` parameter is a pointer-to-pointer hierarchy:

| Offset | Field |
|---|---|
| `*ctx` | IRBuilder state (current function, insert point) |
| `ctx[1]` | Debug info context: `[0]` = debug scope, `[1]` = current BB, `[2]` = insertion sentinel |
| `ctx[2]` | LLVM module/context handle |

### EDG expression node layout

Every expression node passed as `expr` has a fixed layout:

| Offset | Size | Field |
|---|---|---|
| +0x00 | 8 | Type pointer (EDG type node) |
| +0x18 | 1 | **Outer opcode** (expression kind byte) |
| +0x19 | 1 | Flags byte |
| +0x24 | 12 | Source location info |
| +0x38 | 1 | **Inner opcode** (operator sub-kind, for kind=1) |
| +0x48 | 8 | Child/operand pointer |

Type nodes carry a tag at offset +140: `12` = typedef alias (follow +160 to unwrap), `1` = void. The typedef-stripping idiom appears 15+ times throughout the function:

```c
// Type unwrapping — strips typedef aliases to canonical type
for (Type *t = expr->type; *(uint8_t*)(t + 140) == 12; t = *(Type**)(t + 160))
    ;
```

### Outer switch — expression categories

The byte at `expr+0x18` selects the top-level expression category:

| Kind | Category | Handler |
|---|---|---|
| `0x01` | Operation expression | Inner switch on `expr+0x38` (40+ C operators) |
| `0x02` | Literal constant | `EmitLiteral` (`sub_127F650`) |
| `0x03` | Member/field access | `EmitAddressOf` + `EmitLoadFromAddress` |
| `0x11` | Call expression | `EmitCall` (`sub_1296570`) |
| `0x13` | Init expression | `EmitInitExpr` (`sub_1281220`) |
| `0x14` | Declaration reference | `EmitAddressOf` + `EmitLoadFromAddress` |
| default | | Fatal: `"unsupported expression!"` |

### Inner switch — C operators to LLVM IR

When the outer kind is `0x01` (operation), the byte at `expr+0x38` selects which C operator to emit. The complete mapping follows, grouped by category.

#### Arithmetic and comparison (opcodes 0x27-0x2B, 0x35-0x39)

All delegate to `EmitBinaryArithCmp` (`sub_128F9F0`), which reads the expression type to choose between integer and floating-point LLVM opcodes and selects signed vs. unsigned variants.

#### Shift and bitwise (opcodes 0x3A-0x3F)

Each passes an opcode triple `(signedOp, intOp, fpOp)` to `EmitShiftOrBitwise` (`sub_128F580`):

| Opcode | C operator | LLVM (int) | Triple |
|---|---|---|---|
| `0x3A` | `<<` | `shl` | `(1, 32, 32)` |
| `0x3B` | `>>` | `ashr` / `lshr` | `(14, 33, 33)` |
| `0x3C` | `&` | `and` | `(2, 38, 34)` |
| `0x3D` | `^` | `xor` | `(4, 40, 36)` |
| `0x3E` | `\|` | `or` | `(3, 39, 35)` |
| `0x3F` | rotate | funnel shift | `(5, 41, 37)` |

The `signedOp` value controls whether right-shift produces `ashr` (arithmetic, preserves sign) or `lshr` (logical, zero-fills).

#### Increment / decrement (opcodes 0x23-0x26)

All four variants call `EmitIncDec` (`sub_128C390`) with two boolean flags:

| Opcode | C operator | `isPrefix` | `isIncrement` |
|---|---|---|---|
| `0x23` | `++x` (prefix) | 1 | 1 |
| `0x24` | `--x` (prefix) | 0 | 0 |
| `0x25` | `x++` (postfix) | 1 | 0 |
| `0x26` | `x--` (postfix) | 0 | 1 |

#### Compound assignment (opcodes 0x4A-0x55)

All use a generic wrapper `EmitCompoundAssignWrapper` (`sub_12901D0`) that takes a per-operator implementation function:

| Opcode | C operator | Implementation |
|---|---|---|
| `0x4A` | `+=` | `sub_1288F60` (AddAssign) |
| `0x4B` | `-=` | `sub_1288370` (SubAssign) |
| `0x4C` | `*=` | `sub_1288770` (MulAssign) |
| `0x4D` | `/=` | `sub_1289D20` (DivAssign) |
| `0x4E` | `%=` | `sub_1288DC0` (RemAssign) |
| `0x4F` | `&=` | `sub_1288B70` (AndAssign) |
| `0x50` | `\|=` | `sub_1289360` (OrAssign) |
| `0x51` | `<<=` | `sub_1288090` (ShlAssign) |
| `0x52` | `>>=` | `sub_1287F30` (ShrAssign) |
| `0x53` | `^=` | `sub_1288230` (XorAssign) |

#### Simple and delegating opcodes

| Opcode | Category | Behavior |
|---|---|---|
| `0x00` | Constant subexpr | Evaluate via `sub_72B0F0`, attach debug loc, load |
| `0x03,0x06,0x08,0x5C,0x5E,0x5F` | Compound/special | Delegate to `EmitCompoundAssign` (`sub_1287ED0`) |
| `0x05` | Dereference (`*p`) | If child is address-of: elide. Otherwise: recursive emit + load |
| `0x19` | Parenthesized `(x)` | Tail-call: strip parens, loop with `a2 = child` |
| `0x1A` | `sizeof` / `alignof` | Delegate to `sub_128FDE0` |
| `0x1E,0x1F,0x41-0x46,0x59,0x5A,0x5D,0x68` | Type-level constants | All delegate to `ConstantFromType` (`sub_127D2C0`) |
| `0x32` | Comma `(a, b)` | Emit both sides, return RHS value |
| `0x33` | Subscript `a[i]` | Emit base + index, GEP + load via `sub_128B750` |
| `0x49` | Member access | Compute field GEP, bitfield or normal load |
| `0x56` | Bitfield assignment | Full R-M-W (see [Bitfield Codegen](#bitfield-codegen) below) |
| `0x5B` | Statement expression `({...})` | Emit body via `EmitStmtExpr`, create empty BB if needed |
| `0x69` | Special constant | Delegate to `sub_1281200` |
| `0x6F` | Label address (`&&label`) | GCC extension: `blockaddress` via `sub_1285E30` |
| `0x70` | Label value | Indirect goto target materialization |
| `0x71` | Computed goto (`goto *p`) | `sub_1285E30` with different flag |
| `0x72` | `va_arg` | Delegate to `sub_1286000` |
| default | | Fatal: `"unsupported operation expression!"` |

### Constant vs. instruction dispatch

Throughout all operator emission, a consistent pattern selects between constant folding and IR instruction creation. The byte at `Value+16` encodes the LLVM Value subclass kind: values <= `0x10` are constants (`ConstantInt`, `ConstantFP`, etc.) and values > `0x10` are instructions:

```c
if (*(uint8_t*)(value + 16) > 0x10) {
    // Real IR instruction -- create via IR builder
    result = CreateCast(opcode, value, destTy, &out, 0);    // sub_15FDBD0
    result = CreateBinOp(opcode, lhs, rhs, &out, 0);       // sub_15FB440
} else {
    // Compile-time constant -- constant-fold
    result = ConstantExprCast(opcode, value, destTy, 0);    // sub_15A46C0
    result = ConstantFoldBinOp(lhs, rhs, 0, 0);            // sub_15A2B60
}
```

## Key Expression Patterns

### Array decay

Opcode `0x15`. Converts an array lvalue to a pointer to its first element.

When `IsArrayType` (`sub_8D23B0`) confirms the source is an array type, the emitter creates an inbounds GEP with two zero indices. The GEP instruction is constructed manually: allocate 72 bytes for 3 operands via `AllocateInstruction`, compute the result element type, propagate address space qualifiers from the source, then fill operands (base, `i64 0`, `i64 0`) and mark `inbounds`:

```llvm
%arraydecay = getelementptr inbounds [N x T], ptr %arr, i64 0, i64 0
```

If the source is already a pointer type (not an array), the function either passes through directly or inserts a `ptrtoint` / `zext` if the types differ.

### Pointer subtraction

Opcode `0x34`. The classic 5-step Clang pattern for `(p1 - p2)`:

```llvm
%sub.ptr.lhs.cast = ptrtoint ptr %p1 to i64
%sub.ptr.rhs.cast = ptrtoint ptr %p2 to i64
%sub.ptr.sub      = sub i64 %sub.ptr.lhs.cast, %sub.ptr.rhs.cast
%sub.ptr.div      = sdiv exact i64 %sub.ptr.sub, 4    ; element_size=4 for int*
```

Step 5 (the `sdiv exact`) is **skipped entirely** when the element size is 1 (i.e., `char*` arithmetic), since division by 1 is a no-op. The element size comes from the pointed-to type at offset +128. The `exact` flag on `sdiv` tells the optimizer that the division is known to produce no remainder -- a critical optimization hint.

### Logical AND (short-circuit)

Opcode `0x57`. Creates two basic blocks and a PHI node for C's short-circuit `&&` evaluation:

```llvm
entry:
    %lhs = icmp ne i32 %a, 0
    br i1 %lhs, label %land.rhs, label %land.end

land.rhs:
    %rhs = icmp ne i32 %b, 0
    br label %land.end

land.end:
    %0 = phi i1 [ false, %entry ], [ %rhs, %land.rhs ]
    %land.ext = zext i1 %0 to i32
```

The construction sequence:

1. Create blocks `land.end` and `land.rhs` via `CreateBasicBlock` (`sub_12A4D50`).
2. Emit LHS as boolean via `EmitBoolExpr` (`sub_127FEC0`).
3. Conditional branch: `br i1 %lhs, label %land.rhs, label %land.end`.
4. Switch insertion point to `%land.rhs`.
5. Emit RHS as boolean.
6. Unconditional branch to `%land.end`.
7. Switch to `%land.end`, construct PHI with 2 incoming edges.
8. Zero-extend the `i1` PHI result to the expression's declared type (`i32` typically) with name `land.ext`.

The PHI node is allocated as 64 bytes via `AllocatePHI` (`sub_1648B60`), initialized with opcode 53 (PHI), and given a capacity of 2. Incoming values are stored in a compact layout: `[val0, val1, ..., bb0, bb1, ...]` where each value slot occupies 24 bytes (value pointer + use-list doubly-linked-list pointers), and basic block pointers form a parallel array after all value slots.

### Logical OR (short-circuit)

Opcode `0x58`. Identical structure to logical AND but with **inverted branch sense**: the TRUE outcome of the LHS branches to `lor.end` (short-circuits to true), and FALSE falls through to evaluate the RHS:

```llvm
entry:
    %lhs = icmp ne i32 %a, 0
    br i1 %lhs, label %lor.end, label %lor.rhs

lor.rhs:
    %rhs = icmp ne i32 %b, 0
    br label %lor.end

lor.end:
    %0 = phi i1 [ true, %entry ], [ %rhs, %lor.rhs ]
    %lor.ext = zext i1 %0 to i32
```

Internally, the AND and OR paths share a common tail (merging at a single code point with a variable holding either `"lor.ext"` or `"land.ext"`).

### Ternary / conditional operator

Opcode `0x67`. Constructs a full three-block diamond with PHI merge for `a ? b : c`:

```llvm
entry:
    %cond.bool = icmp ne i32 %test, 0
    br i1 %cond.bool, label %cond.true, label %cond.false

cond.true:
    %v1 = <emit true expr>
    br label %cond.end

cond.false:
    %v2 = <emit false expr>
    br label %cond.end

cond.end:
    %cond = phi i32 [ %v1, %cond.true ], [ %v2, %cond.false ]
```

The function creates three blocks (`cond.true`, `cond.false`, `cond.end`), records which basic block each arm finishes in (since the true/false expression emission might create additional blocks), and builds the PHI from those recorded blocks. When one arm is void, the PHI is omitted and whichever arm produced a value is returned directly.

### Logical NOT and bitwise NOT

**Logical NOT** (opcode `0x1D`) is a two-phase emit:

```llvm
%lnot     = icmp eq i32 %x, 0         ; Phase 1: convert to bool
%lnot.ext = zext i1 %lnot to i32      ; Phase 2: extend back to declared type
```

Phase 1 calls `EmitBoolExpr` which produces the `icmp eq ... 0` comparison. Phase 2 zero-extends the `i1` back to the expression's target type. If the value is already a compile-time constant, the constant folder handles it directly.

**Bitwise NOT** (opcode `0x1C`) produces `xor` with all-ones:

```llvm
%not = xor i32 %x, -1
```

Created via `CreateUnaryOp` (`sub_15FB630`) which synthesizes `xor` with `-1` (all bits set). Optional `zext` follows if the result needs widening.

### Dereference with address-of elision

Opcode `0x05`. Before emitting a load for unary `*`, the function checks if the child is an address-of expression via `IsAddressOfExpr` (`sub_127B420`). If so, the dereference and address-of cancel out -- no IR is emitted, only a debug annotation is attached. This handles the common pattern `*&x` becoming just `x`.

## Bitfield Codegen

Bitfield loads and stores are lowered to shift/mask/or sequences by two dedicated functions. A path selector `CanUseFastBitfieldPath` (`sub_127F680`) determines whether the bitfield fits within a single naturally-aligned container element (fast path) or must be processed byte-by-byte (general path).

### EDG bitfield descriptor

The bitfield metadata object carries:

| Offset | Type | Field |
|---|---|---|
| +120 | qword | Container type node |
| +128 | qword | Byte offset within struct |
| +136 | byte | Bit offset within containing byte |
| +137 | byte | Bit width of the field |
| +140 | byte | Type tag (12 = array wrapper, walk chain) |
| +144 | byte | Flags (bit 3 = signed bitfield) |
| +160 | qword | Next/inner type pointer |

### Fast path (single-container load)

When the bitfield plus its bit range fits within one container element, the fast path loads the entire container and extracts the field with a single shift and mask:

```c
// Example: struct { unsigned a:3; unsigned b:5; } s;
// s.b: byte_offset=0, bit_offset=3, bit_width=5, container=i8
```

**Load** `s.b` (fast path):

```llvm
%container  = load i8, ptr %s
%shifted    = lshr i8 %container, 3            ; "highclear" -- position field at bit 0
%result     = and i8 %shifted, 31              ; "zeroext" -- mask to 5 bits (0x1F)
```

The shift amount is computed as `8 * elem_size - bit_width - bit_offset - 8 * (byte_offset % elem_size)`. When this evaluates to zero, the `lshr` is constant-folded away.

For **signed** bitfields, the zero-extend is replaced with an arithmetic sign extension via shift-left then arithmetic-shift-right:

```llvm
%shifted = lshr i8 %container, 3              ; "highclear"
%signext = ashr i8 %shifted, 5                ; "signext" -- propagates sign bit
```

**Store** `s.b = val` (fast path read-modify-write):

```llvm
%container     = load i8, ptr %s
%bf.value      = and i8 %val, 31              ; mask to 5 bits
%cleared       = and i8 %container, 7         ; "bf.prev.cleared" -- clear bits [3:7]
%positioned    = shl i8 %bf.value, 3          ; "bf.newval.positioned"
%merged        = or  i8 %cleared, %positioned ; "bf.finalcontainerval"
store i8 %merged, ptr %s
```

The clear mask is `~((1 << bit_width) - 1) << bit_position)`. For containers wider than 64 bits, both the clear mask and the value mask are computed via APInt operations (`sub_16A5260` to set bit range, `sub_16A8F40` to invert).

### Byte-by-byte path (spanning load)

When the bitfield spans multiple container elements, it is processed one byte at a time. Each iteration loads a byte, extracts the relevant bits, zero-extends to the accumulator width, shifts into position, and ORs into the running accumulator.

For example, a 20-bit field starting at byte 0, bit 0:

```llvm
; Byte 0: bits [0:7]
%bf.base.i8ptr = bitcast ptr %s to ptr         ; pointer cast
%byte0.ptr     = getelementptr i8, ptr %bf.base.i8ptr, i64 0
%bf.curbyte.0  = load i8, ptr %byte0.ptr
%bf.byte_zext.0 = zext i8 %bf.curbyte.0 to i32
; accumulator = %bf.byte_zext.0 (shift=0 for first byte)

; Byte 1: bits [8:15]
%byte1.ptr     = getelementptr i8, ptr %bf.base.i8ptr, i64 1
%bf.curbyte.1  = load i8, ptr %byte1.ptr
%bf.byte_zext.1 = zext i8 %bf.curbyte.1 to i32
%bf.position.1  = shl i32 %bf.byte_zext.1, 8   ; "bf.position"
%bf.merge.1     = or  i32 %bf.byte_zext.0, %bf.position.1  ; "bf.merge"

; Byte 2: only 4 bits remain (20 - 16 = 4)
%byte2.ptr         = getelementptr i8, ptr %bf.base.i8ptr, i64 2
%bf.curbyte.2      = load i8, ptr %byte2.ptr
%bf.end.highclear  = lshr i8 %bf.curbyte.2, 4  ; "bf.end.highclear" -- clear top 4 bits
%bf.byte_zext.2    = zext i8 %bf.end.highclear to i32
%bf.position.2     = shl i32 %bf.byte_zext.2, 16
%bf.merge.2        = or  i32 %bf.merge.1, %bf.position.2
```

The byte-by-byte store path mirrors this in reverse: for boundary bytes (first and last), it loads the existing byte, masks out the target bits with AND, positions the new bits with SHL, and merges with OR. Middle bytes that are entirely overwritten skip the read-modify-write and store directly.

### The `bf.*` naming vocabulary

All bitfield IR values use a consistent naming scheme:

| Name | Path | Meaning |
|---|---|---|
| `bf.base.i8ptr` | Both | Pointer cast to `i8*` |
| `bf.curbyte` | Load | Current byte in iteration loop |
| `bf.end.highclear` | Load | `lshr` to clear unused high bits in last byte |
| `bf.byte_zext` | Load | `zext` of byte to accumulator width |
| `bf.position` | Both | `shl` to position byte/value within accumulator/container |
| `bf.merge` | Load | `or` to merge byte into accumulator |
| `bf.highclear` | Load | `lshr` before sign extension |
| `bf.finalval` | Load | `ashr` for sign extension |
| `highclear` | Load fast | Fast-path `lshr` to clear high bits |
| `zeroext` | Load fast | Fast-path zero-extend result |
| `signext` | Load fast | Fast-path `ashr` sign extension |
| `bf.value` | Store | `and(input, width_mask)` -- isolated field bits |
| `bf.prev.cleared` | Store fast | Container with old field bits cleared |
| `bf.newval.positioned` | Store fast | New value shifted to field position |
| `bf.finalcontainerval` | Store fast | `or(cleared, positioned)` -- final container |
| `bf.reload.val` | Store | Truncated value for compound assignment reload |
| `bf.reload.sext` | Store | Sign-extended reload via shift pair |
| `bassign.tmp` | Store | Alloca for temporary during bitfield assignment |

### Wide bitfield support (> 64 bits)

Both load and store functions handle bitfields wider than 64 bits through APInt operations. The threshold check `width > 0x40` (64) appears throughout: values <= 64 bits use inline `uint64_t` masks computed as `0xFFFFFFFFFFFFFFFF >> (64 - width)`, while wider values allocate heap-backed APInt word arrays. Every code path carefully frees heap APInts after use. This supports `__int128` bitfields in CUDA.

### Volatile and alignment

Volatile detection uses a global flag at `unk_4D0463C`. When set, `sub_126A420` queries whether the GEP target address is in volatile memory, propagating the volatile bit to load/store instructions. The alignment parameter for bitfield container loads must be 1; the function asserts on other values with `"error generating code for loading from bitfield!"`.

### Duplicate implementations

Two additional copies exist at `sub_923780` (store) and `sub_925930` (load) -- identical algorithms with the same string names, same opcodes, same control flow. These likely correspond to different template instantiations or address-space variants in the original NVIDIA source. The `0x92xxxx` copies are in the main NVVM frontend region while the `0x128xxxx` copies are in the codegen helper region.

## Constant Expression Codegen

`EmitConstExpr` (`sub_127D8B0`) converts EDG constant expression AST nodes into `llvm::Constant*` values. It is recursive: aggregate initializers call it for each element.

```c
// sub_127D8B0
llvm::Constant *EmitConstExpr(CodeGenState *ctx, EDGConstExprNode *expr,
                               llvm::Type *arrayElemTyOverride);
```

The constant kind byte at `expr[10].byte[13]` is the primary dispatch:

| Kind | Category | Output type |
|---|---|---|
| `1` | Integer constant | `ConstantInt` |
| `2` | String literal | `ConstantDataArray` |
| `3` | Floating-point constant | `ConstantFP` |
| `6` | Address-of constant | `GlobalVariable*`, `Function*`, or string global |
| `0xA` | Aggregate initializer | `ConstantStruct`, `ConstantArray`, or `ConstantAggregateZero` |
| `0xE` | Null/empty | Returns 0 (no constant) |
| default | | Fatal: `"unsupported constant variant!"` |

### Integer constants

For normal integers (up to 64 bits), the value is extracted via `edg::GetSignedIntValue` or `edg::GetUnsignedIntValue` depending on signedness, masked to the actual bit width, and passed to `ConstantInt::get(context, APInt)`.

For **`__int128`** (type size == 16 bytes), the EDG IL stores the value as a decimal string. The path is: `edg::GetIntConstAsString(expr)` returns the decimal text, then `APInt::fromString(128, str, len, radix=10)` parses it into a 128-bit APInt. This string-based transfer suggests the EDG IL uses text encoding for portability of wide integers.

APInt memory management follows the standard pattern: values > 64 bits use heap-allocated word arrays (checked via `width > 0x40`). Every path frees heap APInts after consumption.

When the target LLVM type is a pointer (tag 15), the integer constant is first created, then `ConstantExpr::getIntToPtr` converts it.

### String literals

The character width is determined from a lookup table `qword_4F06B40` indexed by the encoding enum at `expr[10].byte[8] & 7`:

| Index | Width | C type |
|---|---|---|
| 0 | 1 byte | `char` / UTF-8 |
| 1 | platform | `wchar_t` |
| 2 | 1 byte | `char8_t` |
| 3 | from global | platform-dependent |
| 4 | from global | platform-dependent |

The raw byte buffer is built by copying `byte_count` bytes from the EDG node, reading each character through `edg::ReadIntFromBuffer(src, width)` -- an endian-aware read function (the EDG IL may store string data in a platform-independent byte order). The buffer is then passed to `ConstantDataArray::getRaw(data, byte_count)` to create the LLVM constant.

For each character width, the LLVM element type is selected: `i8` for 1-byte, `i16` for 2-byte, `i32` for 4-byte, `i64` for 8-byte. Empty strings create zero-element arrays. If the array type override `a3` provides a larger size than the literal, the remaining bytes are zero-filled.

### Floating-point constants

Raw bit patterns are extracted via `edg::ExtractFloatBits(kind, data_ptr)`, then reinterpreted into native `float` or `double` values:

| EDG kind | C type | Conversion path |
|---|---|---|
| 2 | `float` | `BitsToFloat` -> `APFloat(float)` -> `IEEEsingle` semantics |
| 4 | `double` | `BitsToDouble` -> `APFloat(double)` -> `IEEEdouble` semantics |
| 6 | `long double` | **Truncated to double** (with warning 0xE51) |
| 7 | `__float80` | **Truncated to double** (with warning 0xE51) |
| 8, 13 | `__float128` | **Truncated to double** (with warning 0xE51) |

All extended-precision types (long double, `__float80`, `__float128`) are silently lowered through the double path. NVPTX has no hardware support for 80-bit or 128-bit floats, so CICC truncates them to 64-bit IEEE 754. When the compilation context has the appropriate flag (bit 4 at offset +198), a diagnostic warning is emitted identifying the specific type being truncated.

### Address-of constants

Sub-dispatched by a byte at `expr[11].byte[0]`:

- **Byte 0 -- Variable/global reference**: Calls `GetOrCreateGlobalVariable` (`sub_1276020`), returning a `GlobalVariable*` as a constant pointer. Debug info is optionally attached.
- **Byte 1 -- Function reference**: Calls `GetOrCreateFunction` (`sub_1277140`). For static-linkage functions, resolves through `LookupFunctionStaticVar`.
- **Byte 2 -- String literal reference** (`&"..."`): Validates the node kind is 2 (string), then calls `CreateStringGlobalConstant` (`sub_126A1B0`).

Post-processing applies a constant GEP offset if `expr[12].qword[0]` is nonzero, and performs pointer type cast if the produced type differs from the expected type. Same-address-space mismatches use `ConstantExpr::getBitCast`; cross-address-space mismatches use `ConstantExpr::getAddrSpaceCast`. Pointer-to-integer mismatches use `ConstantExpr::getPtrToInt` with address-space normalization to `addrspace(0)` first.

### Aggregate initializers

The largest case (630+ lines). After stripping typedefs, dispatches on the canonical type tag:

**Struct** (tag 10): Walks the EDG field list and initializer list in parallel. Padding/zero-width fields are skipped (flag byte at +146, bit 3). For each field, calls `EmitConstExpr` recursively for the field's initializer, pushes the result into an element vector. Missing trailing fields are filled with `Constant::getNullValue`. If the struct is empty and the initializer list is empty, returns `ConstantAggregateZero::get` as a shortcut.

Bitfield fields within structs are deferred to a post-processing pass that packs bits byte-by-byte using APInt operations: for each bitfield, the compiled constant value is extracted, truncated to the field's bit width, then shifted and ORed into the appropriate byte positions of the struct constant. The iteration processes one byte at a time, handling first-byte, middle-byte, and last-byte boundary cases identically to the runtime bitfield store path.

**Union** (tag 11): Finds the initialized member (via designated initializer if present, otherwise the first non-skip non-bitfield field). Emits the member value recursively, then pads with `i8 x N` zero bytes to the full union size. The result is an anonymous `{member_type, [N x i8]}` struct. Named bitfield members in unions are explicitly rejected: `"initialization of bit-field in union not supported!"`.

**Array** (tag 8): Resolves element type, walks the initializer linked list, calls `EmitConstExpr` recursively for each element. When the declared dimension exceeds the initializer count, remaining elements are filled with `Constant::getNullValue`. The result uses `ConstantArray::get` when all elements have the same type, or falls back to an anonymous struct for heterogeneous cases (which should not occur in well-formed C).

## Cast / Conversion Codegen

`EmitCast` (`sub_128A450`) handles every C-level cast category. The function first checks for early exits (skip flag, identity cast where source type equals destination type), then dispatches by source and destination type tags.

```c
// sub_128A450
llvm::Value *EmitCast(CodeGenState **ctx, EDGCastNode *expr,
                      uint8_t is_unsigned, llvm::Type *destTy,
                      uint8_t is_unsigned2, char skip_flag,
                      DiagContext *diag);
```

### Type classification

Type tags at `*(type+8)`:

| Tag | Type |
|---|---|
| 1-6 | Floating-point (1=half, 2=float, 3=double, 4=fp80, 5=fp128, 6=bf16) |
| 11 | Integer (bit-width encoded in upper bits) |
| 15 | Pointer |
| 16 | Vector/aggregate |

The test `(tag - 1) > 5` means "NOT a float" (tags 1-6 are float types).

### Tobool patterns

When the destination type is `i1` (bool), the codegen produces comparison-against-zero:

**Integer/float source** (tags 1-6, 11):

```llvm
%tobool = icmp ne i32 %val, 0          ; integer source
%tobool = fcmp une float %val, 0.0     ; float source
```

Float-to-bool uses `fcmp une` (unordered not-equal), which returns true for any non-zero value including NaN. Integer-to-bool uses `icmp ne` with a zero constant of matching type.

**Pointer source** (tag 15):

```llvm
%tobool = icmp ne ptr %val, null
```

A shortcut exists: if the source expression is already a comparison result (opcode 61) and the source is already the bool type, the comparison result is returned directly without creating a new instruction.

### Integer-to-integer (trunc / zext / sext)

The helper `sub_15FE0A0` internally selects the operation based on relative widths:

- `dest_width < src_width` -> `trunc`
- `dest_width > src_width` AND unsigned -> `zext`
- `dest_width > src_width` AND signed -> `sext`

All produce a value named `"conv"`.

### Pointer casts

**Pointer-to-pointer**: In LLVM opaque-pointer mode (which CICC v13 uses for modern SMs), same-address-space casts hit the identity return path and produce no IR. Cross-address-space casts use `addrspacecast` (opcode 47).

**Pointer-to-integer**: `ptrtoint` (opcode 45). Asserts that the destination is actually an integer type.

**Integer-to-pointer**: A two-step process. First, the integer is widened or narrowed to the pointer bit-width (32 or 64, obtained via `sub_127B390`). Then `inttoptr` (opcode 46) converts the properly-sized integer to a pointer:

```llvm
%conv1 = zext i32 %val to i64          ; step 1: widen to pointer width
%conv  = inttoptr i64 %conv1 to ptr    ; step 2: int -> ptr
```

### Float-to-integer and integer-to-float

Two paths exist for these conversions:

**Standard path**: Uses LLVM's native cast opcodes. Triggered when the global flag `unk_4D04630` is set (relaxed rounding mode), or when the destination is 128-bit, or when the source is `fp128`:

| Direction | Signed opcode | Unsigned opcode |
|---|---|---|
| int -> float | `sitofp` (39) | `uitofp` (40) |
| float -> int | `fptosi` (41) | `fptoui` (42) |

**NVIDIA intrinsic path**: For SM targets that require round-to-zero semantics on float-int conversions. Constructs an intrinsic function name dynamically and emits it as a plain function call:

```c
// Name construction pseudocode
char buf[64];
if (src_is_double)  strcpy(buf, "__nv_double");
else                strcpy(buf, "__nv_float");

strcat(buf, is_unsigned ? "2u" : "2");

if (dest_bits == 64) strcat(buf, "ll_rz");
else                 strcat(buf, "int_rz");
```

Producing names like:

| Intrinsic | Conversion |
|---|---|
| `__nv_float2int_rz` | `f32` -> `i32`, signed, round-to-zero |
| `__nv_float2uint_rz` | `f32` -> `u32`, unsigned, round-to-zero |
| `__nv_double2ll_rz` | `f64` -> `i64`, signed, round-to-zero |
| `__nv_double2ull_rz` | `f64` -> `u64`, unsigned, round-to-zero |
| `__nv_float2ll_rz` | `f32` -> `i64`, signed, round-to-zero |

These are emitted as plain LLVM function calls (`call i32 @__nv_float2int_rz(float %val)`), not as LLVM intrinsics. The NVIDIA PTX backend later pattern-matches these `__nv_` calls to `cvt.rz.*` PTX instructions. The intrinsic call is created by `sub_128A3C0`, which builds a function type, looks up or creates the declaration in the module, and emits a `CallInst` with one argument.

If the source integer is 32-bit but the target needs 64-bit conversion, the function first converts `i32` to `i64`, then **recursively calls itself** to convert `i64` to the target float type.

### Float-to-float (fptrunc / fpext)

The source and destination type tags are compared directly. If the destination tag is larger (wider float), opcode 44 (`fpext`) is used. If smaller, opcode 43 (`fptrunc`).

```llvm
%conv = fpext float %val to double       ; float -> double
%conv = fptrunc double %val to float     ; double -> float
```

### Cast control flow summary

```
EmitCast(ctx, expr, is_unsigned, destTy, is_unsigned2, skip, diag)
  |
  +-- skip_flag set          --> return 0
  +-- destTy == BoolType?
  |     +-- src is float       --> fcmp une %val, 0.0    "tobool"
  |     +-- src is ptr/int     --> icmp ne %val, null/0  "tobool"
  +-- srcTy == destTy          --> return expr (identity)
  +-- ptr -> ptr               --> bitcast(47)           "conv"
  +-- ptr -> int               --> ptrtoint(45)          "conv"
  +-- int -> ptr               --> resize + inttoptr(46) "conv"
  +-- int -> int               --> trunc/zext/sext       "conv"
  +-- int -> float
  |     +-- standard           --> sitofp(39)/uitofp(40) "conv"
  |     +-- nvidia             --> __nv_*2*_rz call      "call"
  +-- float -> int
  |     +-- standard           --> fptosi(41)/fptoui(42) "conv"
  |     +-- nvidia             --> __nv_*2*_rz call      "call"
  +-- float -> float
        +-- wider              --> fpext(44)             "conv"
        +-- narrower           --> fptrunc(43)           "conv"
```

## IR Instruction Infrastructure

### BB insertion linked list

After creating any LLVM instruction, it must be inserted into the current basic block. This appears ~30 times across the expression codegen functions as a doubly-linked intrusive list manipulation. The low 3 bits of list pointers carry tag/flag bits (alignment guarantees valid pointers have zero in those positions):

```c
// Repeated BB insertion pattern
Value *tail = ctx[1][1];           // current BB's instruction list tail
if (tail) {
    Value *sentinel = ctx[1][2];   // sentinel node
    InsertIntoBB(tail + 40, inst); // sub_157E9D0
    // Linked list fixup (doubly-linked with 3-bit tag):
    inst->prev = (*sentinel & ~7) | (inst->prev & 7);   // preserve tag bits
    inst->parent = sentinel;
    ((*sentinel & ~7) + 8) = inst + 24;    // old_tail.next = inst
    *sentinel = (*sentinel & 7) | (inst + 24);  // sentinel.head = inst
}
```

Instruction offsets: +24 = prev pointer, +32 = parent block, +48 = debug location metadata slot.

### Debug metadata attachment

After every BB insertion, debug location metadata is cloned and attached:

```c
SetValueName(inst, &name);                    // sub_164B780: e.g. "lnot.ext"
Value *debugLoc = *ctx_debug;
if (debugLoc) {
    Value *cloned = CloneDebugLoc(debugLoc, 2);  // sub_1623A60
    if (inst->debugLoc)
        ReleaseDebugLoc(inst + 48);              // sub_161E7C0: free old
    inst->debugLoc = cloned;
    if (cloned)
        RegisterDebugLoc(cloned, inst + 48);     // sub_1623210
}
```

### Global flags

| Address | Purpose |
|---|---|
| `dword_4D04720` + `dword_4D04658` | Debug info emission control. When both zero, source location is forwarded before dispatch |
| `dword_4D04810` | Bitfield optimization flag. When set, enables `bassign.tmp` alloca path for bitfield assignments |
| `unk_4D04630` | When set, forces standard LLVM casts (`sitofp`/`fptosi`) instead of `__nv_*_rz` intrinsics |
| `unk_4D04700` | When set, marks tobool results as "potentially inexact" via flag bit |
| `unk_4D0463C` | Volatile detection flag. When set, queries address volatility |

## Helper Function Reference

| Address | Recovered name | Role |
|---|---|---|
| `sub_128D0F0` | `EmitExpr` | Master expression dispatcher (this page) |
| `sub_128A450` | `EmitCast` | All C-level casts |
| `sub_127D8B0` | `EmitConstExpr` | Compile-time constant expressions |
| `sub_1282050` | `EmitBitfieldStore` | Bitfield write (R-M-W) |
| `sub_1284570` | `EmitBitfieldLoad` | Bitfield read (extract) |
| `sub_127FEC0` | `EmitBoolExpr` | Expression to `i1` conversion |
| `sub_127F650` | `EmitLiteral` | Numeric/string literal emission |
| `sub_1286D80` | `EmitAddressOf` | Compute pointer to lvalue |
| `sub_1287CD0` | `EmitLoadFromAddress` | Load via computed address |
| `sub_1287ED0` | `EmitCompoundAssign` | Generic compound assignment |
| `sub_128C390` | `EmitIncDec` | Pre/post increment/decrement |
| `sub_128F9F0` | `EmitBinaryArithCmp` | Binary arithmetic and comparison |
| `sub_128F580` | `EmitShiftOrBitwise` | Shift and bitwise operators |
| `sub_128B750` | `EmitSubscriptOp` | Array subscript (GEP + load) |
| `sub_128FDE0` | `EmitSizeofAlignof` | `sizeof` and `alignof` operators |
| `sub_12901D0` | `EmitCompoundAssignWrapper` | Wrapper dispatching to per-operator impl |
| `sub_1296570` | `EmitCall` | Function call emission |
| `sub_12897E0` | `EmitBitfieldStore` (inner) | Actual bitfield store logic |
| `sub_127A030` | `GetLLVMType` | EDG type to LLVM type translation |
| `sub_127F680` | `CanUseFastBitfieldPath` | Bitfield path selector |
| `sub_128A3C0` | `EmitIntrinsicConvCall` | `__nv_*_rz` intrinsic call helper |
| `sub_12A4D50` | `CreateBasicBlock` | Create named BB |
| `sub_12A4DB0` | `EmitCondBranch` | Conditional branch emission |
| `sub_12909B0` | `EmitUnconditionalBranch` | Unconditional branch emission |
| `sub_1290AF0` | `SetInsertPoint` | Switch current BB |
| `sub_15FB440` | `CreateBinOp` | Binary instruction creation |
| `sub_15FDBD0` | `CreateCast` | Cast instruction creation (IR path) |
| `sub_15A46C0` | `ConstantExprCast` | Cast (constant-fold path) |
| `sub_15A0680` | `ConstantInt::get` | Integer constant creation |
| `sub_159C0E0` | `ConstantInt::get` (APInt) | Wide integer constant creation |
| `sub_159CCF0` | `ConstantFP::get` | Float constant creation |
| `sub_1648A60` | `AllocateInstruction` | Raw instruction memory allocation |
| `sub_1648B60` | `AllocatePHI` | PHI node memory allocation |
| `sub_164B780` | `SetValueName` | Assigns `%name` to IR value |
