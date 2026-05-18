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
| **Bool conversion** | `sub_127FEC0` — expression-to-`i1` helper |
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
| `0x03` | Lvalue-to-rvalue (non-decl-ref form) | `EmitAddressOf` (`sub_1286D80`) + `EmitLoadFromAddress` (`sub_1287CD0`) — structurally identical to `0x14`; member access (`.`/`->`) is inner opcode `0x49`, not this case |
| `0x11` | Compound-statement expression | `sub_1296570` — statement-list walker, scope push, value of last expression-statement |
| `0x13` | Init expression | `EmitInitExpr` (`sub_1281220`) |
| `0x14` | Declaration reference | `EmitAddressOf` + `EmitLoadFromAddress` |
| default | | Fatal: `"unsupported expression!"` |

### Inner switch — complete opcode reference

When the outer kind is `0x01` (operation), the byte at `expr+0x38` selects which C operator to emit. The complete dispatch table follows. Every opcode is listed; no gaps exist between documented entries.

| Opcode | C operator | Handler / delegate | LLVM pattern |
|---|---|---|---|
| `0x00` | Constant subexpr | `sub_72B0F0` (evaluate) + `sub_1286D80` (load) | Constant materialization |
| `0x03` | Compound special A | `EmitCompoundAssign` (`sub_1287ED0`) | Read-modify-write |
| `0x05` | Dereference (`*p`) | Elide if child is `&`: `IsAddressOfExpr` (`sub_127B420`). Otherwise: recursive `EmitExpr` + `EmitLoad` (`sub_128B370`) | `%val = load T, ptr %p` |
| `0x06` | Compound special B | `EmitCompoundAssign` (`sub_1287ED0`) | Read-modify-write |
| `0x08` | Compound special C | `EmitCompoundAssign` (`sub_1287ED0`) | Read-modify-write |
| `0x15` | Array decay | See [Array decay](#array-decay) | `%arraydecay = getelementptr inbounds ...` |
| `0x19` | Parenthesized `(x)` | Tail-call optimization: `a2 = child`, restart loop | (no IR emitted) |
| `0x1A` | `sizeof` / `alignof` | `EmitSizeofAlignof` (`sub_128FDE0`) | Constant integer |
| `0x1C` | Bitwise NOT (`~x`) | `sub_15FB630` (xor with -1) | `%not = xor i32 %x, -1` |
| `0x1D` | Logical NOT (`!x`) | Two-phase: bool-conversion (`sub_127FEC0`) + `zext` | `%lnot = icmp eq ..., 0` / `%lnot.ext = zext i1 ... to i32` |
| `0x1E` | Type-level const | `ConstantFromType` (`sub_127D2C0`) | Compile-time constant |
| `0x1F` | Type-level const | `ConstantFromType` (`sub_127D2C0`) | Compile-time constant |
| `0x23` | Pre-increment `++x` | `EmitIncDec` (`sub_128C390`): inc=1, isPostfix=0 | `%inc = add ...` / `%ptrincdec = getelementptr ...` |
| `0x24` | Pre-decrement `--x` | `EmitIncDec` (`sub_128C390`): inc=0, isPostfix=0 | `%dec = sub ...` / `%ptrincdec = getelementptr ...` |
| `0x25` | Post-increment `x++` | `EmitIncDec` (`sub_128C390`): inc=1, isPostfix=1 | Returns old value; `%inc = add ...` |
| `0x26` | Post-decrement `x--` | `EmitIncDec` (`sub_128C390`): inc=0, isPostfix=1 | Returns old value; `%dec = sub ...` |
| `0x27`-`0x2B` | `+`, `-`, `*`, `/`, `%` | `EmitBinaryArithBitwise` (`sub_128F9F0`) | `add`/`sub`/`mul`/`sdiv`/`srem` (or `u`/`f` variants) |
| `0x32` | Comma `(a, b)` | Emit both sides; return RHS | (LHS discarded) |
| `0x33` | Subscript `a[i]` | `EmitSubscriptOp` (`sub_128B750`): GEP + load | `%arrayidx = getelementptr ...` + `load` |
| `0x34` | Pointer subtraction | See [Pointer subtraction](#pointer-subtraction) | `%sub.ptr.div = sdiv exact ...` |
| `0x35` | `<<` | `EmitBinaryArithBitwise` (`sub_128F9F0`) → `sub_1288B70` (shift-left helper, CICC opcode 23, string label `"shl"`) | `shl` |
| `0x36` | `>>` | `EmitBinaryArithBitwise` (`sub_128F9F0`) → `sub_1289360` (shift-right helper, CICC opcode 24 signed / 25 unsigned, string label `"shr"`) | `ashr` (signed) / `lshr` (unsigned) |
| `0x37` | `&` | `EmitBinaryArithBitwise` (`sub_128F9F0`) inline with all-ones-mask constant-fold shortcut; CICC opcode 26, string label `"and"` | `and` |
| `0x38` | `\|` | `EmitBinaryArithBitwise` (`sub_128F9F0`) inline with zero-operand constant-fold shortcut; CICC opcode 27, string label `"or"` | `or` |
| `0x39` | `^` | `EmitBinaryArithBitwise` (`sub_128F9F0`) → `sub_15FB440(28, …)`; string label `"xor"` | `xor` |
| `0x3A` | `==` | `EmitCompare` (`sub_128F580`): triple `(1, 32, 32)` = `(FCMP_OEQ, ICMP_EQ, ICMP_EQ)` | `fcmp oeq` / `icmp eq` |
| `0x3B` | `!=` | `EmitCompare` (`sub_128F580`): triple `(14, 33, 33)` = `(FCMP_UNE, ICMP_NE, ICMP_NE)` | `fcmp une` / `icmp ne` |
| `0x3C` | `>` | `EmitCompare` (`sub_128F580`): triple `(2, 38, 34)` = `(FCMP_OGT, ICMP_SGT, ICMP_UGT)` | `fcmp ogt` / `icmp sgt`/`ugt` |
| `0x3D` | `<` | `EmitCompare` (`sub_128F580`): triple `(4, 40, 36)` = `(FCMP_OLT, ICMP_SLT, ICMP_ULT)` | `fcmp olt` / `icmp slt`/`ult` |
| `0x3E` | `>=` | `EmitCompare` (`sub_128F580`): triple `(3, 39, 35)` = `(FCMP_OGE, ICMP_SGE, ICMP_UGE)` | `fcmp oge` / `icmp sge`/`uge` |
| `0x3F` | `<=` | `EmitCompare` (`sub_128F580`): triple `(5, 41, 37)` = `(FCMP_OLE, ICMP_SLE, ICMP_ULE)` | `fcmp ole` / `icmp sle`/`ule` |
| `0x41`-`0x46` | Type-level consts | `ConstantFromType` (`sub_127D2C0`) | Compile-time constant |
| `0x49` | Member access `.`/`->` | See [Member access](#member-access) | `getelementptr` + `load` (or bitfield path) |
| `0x4A` | `+=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1288F60` | Load + add + store |
| `0x4B` | `-=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1288370` | Load + sub + store |
| `0x4C` | `*=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1288770` | Load + mul + store |
| `0x4D` | `/=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1289D20` | Load + div + store |
| `0x4E` | `%=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1288DC0` | Load + rem + store |
| `0x4F` | `<<=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1288B70` (string `"shl"`, CICC opcode 23) | Load + shl + store |
| `0x50` | `>>=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1289360` (string `"shr"`, CICC opcodes 24/25) | Load + ashr/lshr + store |
| `0x51` | `&=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1288090` (string `"and"`, CICC opcode 26) | Load + and + store |
| `0x52` | `\|=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1287F30` (string `"or"`, CICC opcode 27) | Load + or + store |
| `0x53` | `^=` | the compound-assign wrapper (`sub_12901D0`) + `sub_1288230` | Load + xor + store |
| `0x54` | `,=` (rare) | the compound-assign wrapper (`sub_12901D0`) + `sub_128BE50` | Comma-compound |
| `0x55` | `[]=` (subscript compound) | the compound-assign wrapper (`sub_12901D0`) + `sub_128B750` | GEP + R-M-W |
| `0x56` | Bitfield assign | See [Bitfield Codegen](#bitfield-codegen) | R-M-W sequence |
| `0x57` | Logical AND `&&` | See [Logical AND](#logical-and-short-circuit) | `land.rhs`/`land.end` + PHI |
| `0x58` | Logical OR `\|\|` | See [Logical OR](#logical-or-short-circuit) | `lor.rhs`/`lor.end` + PHI |
| `0x59`, `0x5A`, `0x5D` | Type-level consts | `ConstantFromType` (`sub_127D2C0`) | Compile-time constant |
| `0x5B` | Statement expression `({...})` | RValue dispatch (`sub_127FF60`, scalar vs aggregate); create empty BB if `(*a1)[7] == 0` | Body emission |
| `0x5C`, `0x5E`, `0x5F` | Lvalue-load variant (EDG node-kind alias) | `EmitLvalueLoad` (`sub_1287ED0`) — thin two-call wrapper: `EmitAddressOf` (`sub_1286D80`) then `EmitLoadFromAddress` (`sub_1287CD0`); identical shape to `0x03`/`0x06`/`0x08` and to outer-kind `0x14` decl-ref | `%addr = ...` + `%val = load T, ptr %addr` |
| `0x67` | Ternary `?:` | See [Ternary operator](#ternary--conditional-operator) | `cond.true`/`cond.false`/`cond.end` + PHI |
| `0x68` | Type-level const | `ConstantFromType` (`sub_127D2C0`) | Compile-time constant |
| `0x69` | Special const | Special-constant materializer (`sub_1281200`) | Constant materialization |
| `0x6F` | Label address `&&label` | GCC extension: `sub_12A4D00` (lookup) + `sub_1285E30`(builder, label, 1) | `blockaddress(@fn, %label)` |
| `0x70` | Label value | `sub_12A4D00` + `sub_12812E0`(builder, label, type) | Indirect goto target |
| `0x71` | Computed goto `goto *p` | `sub_12A4D00` + `sub_1285E30`(builder, label, 0) | `indirectbr` |
| `0x72` | `va_arg` | `sub_12A4D00` on va_list child + `sub_1286000` | `va_arg` lowering |
| default | | `FatalDiag` (`sub_127B550`) | `"unsupported operation expression!"` |

#### Compare predicate triple encoding

The `EmitCompare` (`sub_128F580`) triple `(fpPred, sIntPred, uIntPred)` carries three LLVM `CmpInst::Predicate` values selected by operand type. The function dispatches: floating-point operand → `FCmp` with `fpPred`, signed integer (or pointer, signed semantics) → `ICmp` with `sIntPred`, unsigned integer → `ICmp` with `uIntPred`. Internally, the helper calls `sub_15FEC10` with opcode `51` (ICmp) or `52` (FCmp) and one of the predicate numbers; on the constant-fold path it calls `sub_15A37B0(pred, lhs, rhs)`. Predicate numbering matches LLVM: `FCMP_OEQ=1`, `FCMP_OGT=2`, `FCMP_OGE=3`, `FCMP_OLT=4`, `FCMP_OLE=5`, `FCMP_UNE=14`; `ICMP_EQ=32`, `ICMP_NE=33`, `ICMP_UGT=34`, `ICMP_UGE=35`, `ICMP_ULT=36`, `ICMP_ULE=37`, `ICMP_SGT=38`, `ICMP_SGE=39`, `ICMP_SLT=40`, `ICMP_SLE=41`. Diagnostic strings inside the helper are `"cmp"`.

Shifts and bitwise operators (`<<`, `>>`, `&`, `|`, `^`) are not handled here -- they ride `EmitBinaryArithBitwise` (`sub_128F9F0`) under cases `0x35`-`0x39`. That helper's inner `switch (*(_BYTE*)(a2 + 56))` dispatches the same opcode byte a second time: cases `'\''`-`'+'` (`0x27`-`0x2B`) call the per-operator arithmetic helpers (`sub_1288F60` add, `sub_1288370` sub, `sub_1288770` mul, `sub_1289D20` div, `sub_1288DC0` rem); cases `'5'`-`'9'` (`0x35`-`0x39`) handle shift-left, shift-right, and-with-mask shortcut, or-with-zero shortcut, and xor respectively. The default arm raises `"unsupported binary expression!"`.

#### Increment / decrement detail

`EmitIncDec` (`sub_128C390`, 16 KB) handles integer, floating-point, and pointer types. It reads the expression type to select the arithmetic operation:

- **Integer path**: `add`/`sub nsw i32 %x, 1` with name `"inc"` or `"dec"`. For prefix variants, the incremented value is returned; for postfix, the original value is returned and the increment is stored.
- **Floating-point path**: `fadd`/`fsub float %x, 1.0` with the same return-value semantics.
- **Pointer path**: `getelementptr inbounds T, ptr %p, i64 1` (or `i64 -1` for decrement) with name `"ptrincdec"`. Element type comes from the pointed-to type.

All paths load the current value, compute the new value, store back, and return either old or new depending on prefix/postfix.

#### Compound assignment wrapper mechanics

The compound-assign wrapper (`sub_12901D0`) implements the common load-compute-store pattern for all compound assignment operators (`+=`, `-=`, etc.):

```c
// sub_12901D0 pseudocode
Value *compound_assign_wrapper(ctx, expr, impl_fn, flags) {
    Value *addr = EmitAddressOf(ctx, expr->lhs);     // sub_1286D80
    Value *old_val = EmitLoadFromAddress(ctx, addr);  // sub_1287CD0
    Value *rhs_val = EmitExpr(ctx, expr->rhs);        // sub_128D0F0 (recursive)
    Value *new_val = impl_fn(ctx, old_val, rhs_val);  // per-operator function
    EmitStore(ctx, new_val, addr);                     // store back
    return new_val;
}
```

Each `impl_fn` is a small function (typically 200-400 lines) that handles integer/float type dispatch and signedness. For example, `sub_1288F60` (AddAssign) selects between `add`, `fadd`, and pointer-GEP addition.

#### Member access multi-path handler

Opcode `0x49` handles struct field access (`.` and `->`) through a multi-path dispatcher:

1. **Simple scalar field** (field count == 1): Computes field address via `EmitAddressOf` (`sub_1286D80`), checks the volatile bit (`v349 & 1`), copies 12 DWORDs of field descriptor into the local frame, then loads via `EmitLoadFromAddress` (`sub_1287CD0`).

2. **Bitfield field**: If the field descriptor indicates a bitfield, routes to `EmitBitfieldAccess` (`sub_1282050`) which emits the shift/mask extraction sequence.

3. **Nested/union access** (field count > 1): Calls `ComputeCompositeMemberAddr` (`sub_1289860`) for multi-level GEP computation, then `EmitComplexMemberLoad` (`sub_12843D0`).

4. **Write-only context**: If the assignment bit (`a2+25`, bit 2) is set, returns null -- the caller only needs the address, not the loaded value.

#### Statement expression, label address, and va_arg

**Statement expression** (`0x5B`): Reads the compound body via `sub_127FF60` (scalar/aggregate RValue dispatch — picks `sub_12A6C40` for aggregate or `sub_128F980` for scalar). If no return basic block exists yet (`(*a1)[7] == 0`), creates an anonymous empty BB via `CreateBasicBlock` + `SetInsertPoint` to serve as the fall-through target. The value of the last expression in the block is the statement expression's result.

**Label address** (`0x6F`): Implements the GCC `&&label` extension. Looks up the label via `LookupLabel` (`sub_12A4D00`), then creates a `blockaddress(@current_fn, %label)` constant via `sub_1285E30(builder, label, 1)`. The second argument `1` distinguishes "take address" from "goto to".

**Computed goto** (`0x71`): The `goto *ptr` extension. Same `LookupLabel` call, but `sub_1285E30(builder, label, 0)` with flag `0` emits an `indirectbr` instruction targeting the resolved label.

**`va_arg`** (`0x72`): Extracts the va_list child node at `+72`, its sub-child at `+16`, resolves both via `sub_12A4D00`, then calls `EmitVaArg` (`sub_1286000`) which lowers to a `va_arg` LLVM instruction with the appropriate type.

### Constant vs. instruction dispatch

Throughout all operator emission, a consistent pattern selects between constant folding and IR instruction creation. The byte at `Value+16` encodes the LLVM Value subclass kind: values <= `0x10` are constants (`ConstantInt`, `ConstantFP`, etc.) and values > `0x10` are instructions. This check appears 20+ times throughout the function, always with the same structure:

```c
// Constant-fold or emit IR? Decision pattern (appears 20+ times)
if (*(uint8_t*)(value + 16) > 0x10) {
    // Real IR instruction -- create via IR builder
    result = CreateCast(opcode, value, destTy, &out, 0);    // sub_15FDBD0
    result = CreateBinOp(opcode, lhs, rhs, &out, 0);       // sub_15FB440
} else {
    // Compile-time constant -- constant-fold at LLVM ConstantExpr level
    result = ConstantExprCast(opcode, value, destTy, 0);    // sub_15A46C0
    result = ConstantFoldBinOp(lhs, rhs, 0, 0);            // sub_15A2B60
}
```

The dispatch table for the constant-fold vs IR-instruction paths:

| Operation | IR path (Value > 0x10) | Constant path (Value <= 0x10) |
|---|---|---|
| Binary op | `CreateBinOp` (`sub_15FB440`) | `ConstantFoldBinOp` (`sub_15A2B60`) |
| Unary NOT | `CreateUnaryOp` (`sub_15FB630`) | `ConstantFoldUnary` (`sub_15A2B00`) |
| Cast | `CreateCast` (`sub_15FDBD0`) | `ConstantExprCast` (`sub_15A46C0`) |
| Int compare | `sub_15FEC10`(op=51, pred) | `sub_15A37B0`(pred, lhs, rhs) |
| Float compare | `sub_15FEC10`(op=52, pred) | `sub_15A37B0`(pred, lhs, rhs) |
| Sub (constant) | `CreateBinOp`(13=Sub) | `ConstantFoldSub` (`sub_15A2B60`) |
| SDiv exact | `CreateBinOp`(18=SDiv) + `SetExactFlag` | `ConstantFoldSDiv` (`sub_15A2C90`) |

When the constant path is taken, no LLVM instruction is created and no BB insertion occurs -- the result is a pure `llvm::Constant*` that can be used directly. This is critical for expressions like `sizeof(int) + 4` where no runtime code should be emitted.

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
2. Emit LHS as boolean via the bool-conversion helper (`sub_127FEC0`).
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

Phase 1 calls the bool-conversion helper (`sub_127FEC0`) which produces the `icmp eq ... 0` comparison. Phase 2 zero-extends the `i1` back to the expression's target type. If the value is already a compile-time constant, the constant folder handles it directly.

**Bitwise NOT** (opcode `0x1C`) produces `xor` with all-ones:

```llvm
%not = xor i32 %x, -1
```

Created via `CreateUnaryOp` (`sub_15FB630`) which synthesizes `xor` with `-1` (all bits set). Optional `zext` follows if the result needs widening.

### Dereference with address-of elision

Opcode `0x05`. Before emitting a load for unary `*`, the function checks if the child is an address-of expression via `IsAddressOfExpr` (`sub_127B420`). If so, the dereference and address-of cancel out -- no IR is emitted, only a debug annotation is attached. This handles the common pattern `*&x` becoming just `x`.

## Bitfield Codegen

Bitfield loads and stores are lowered to shift/mask/or sequences by two dedicated functions. A bitfield-fast-path selector (`sub_127F680`) determines whether the bitfield fits within a single naturally-aligned container element (fast path) or must be processed byte-by-byte (general path).

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

The largest case (630+ lines). After stripping typedefs, dispatches on the canonical type tag at `+140`:

| Tag | Type | Output |
|---|---|---|
| 10 | Struct | `ConstantStruct` or `ConstantAggregateZero` |
| 11 | Union | Anonymous `{member_type, [N x i8]}` |
| 8 | Array | `ConstantArray` |
| 12 | Typedef | Strip and re-dispatch |
| other | | Fatal: `"unsupported aggregate constant!"` |

**Struct** (tag 10): Walks the EDG field list and initializer list in parallel. The field chain is traversed via `+112` pointers; the initializer list via `+120` next pointers.

- Padding/zero-width fields are skipped (flag byte at +146, bit 3).
- For each non-bitfield field, `GetFieldIndex` (`sub_1277B60`) returns the LLVM struct element index. If gaps exist between the previous and current index, intermediate slots are filled with `Constant::getNullValue` (`sub_15A06D0`).
- Each field's initializer is processed by recursive `EmitConstExpr` call.
- Packed struct fields (flag at +145, bit 4) have their sub-elements extracted individually via `ConstantExpr::extractvalue` (`sub_15A0A60`).
- Missing trailing fields are padded with null values.
- If the struct has no fields and the initializer list is empty, returns `ConstantAggregateZero::get` (`sub_1598F00`) as a shortcut.
- Final assembly: `ConstantStruct::get` (`sub_159F090`) with type compatibility check via `Type::isLayoutIdentical` (`sub_1643C60`). If packed, `StructType::get(elts, n, true)` (`sub_15943F0`).

#### Struct bitfield packing (post-processing)

When any bitfield field is detected during the main walk (flag bit 2, `&4` at +144), the function re-enters a post-processing phase after the main field loop. This packs bitfield constant values byte-by-byte into the struct's byte array:

```c
// Bitfield packing pseudocode — sub_127D8B0, case 0xA post-processing
StructLayout *layout = DataLayout::getStructLayout(structTy);  // sub_15A9930

for (each bitfield field where flag &4 at +144 && name at +8 is non-null) {
    uint32_t byte_offset = field->byte_offset;
    uint32_t elem_idx = StructLayout::getElementContainingOffset(layout, byte_offset);
                                                                // sub_15A8020
    // Validate the target byte is zero
    assert(elements[elem_idx] == ConstantInt::get(i8, 0),
           "unexpected error while initializing bitfield!");

    // Evaluate bitfield initializer
    Constant *val = EmitConstExpr(ctx, init_expr, 0);          // recursive
    assert(val != NULL, "bit-field constant must have a known value at compile time!");

    APInt bits = extractAPInt(val);  // at constant+24, width at constant+32
    uint8_t bit_width = field->bit_width;    // at +137
    if (bits.width > bit_width)
        bits = APInt::trunc(bits, bit_width);                  // sub_16A5A50

    // Pack into struct bytes, one byte at a time
    uint8_t bit_offset = field->bit_offset;  // at +136 (within first byte)
    while (remaining_bits > 0) {
        uint8_t available = (first_byte ? 8 - bit_offset : 8);
        uint8_t take = min(remaining_bits, available);

        APInt slice = bits;
        if (slice.width > take)
            slice = APInt::trunc(slice, take);                 // sub_16A5A50
        if (take < 8)
            slice = APInt::zext(slice, 8);                     // sub_16A5C50
        slice = slice << bit_offset;                           // shl
        existing_byte |= slice;                                // sub_16A89F0

        elements[byte_index] = ConstantInt::get(ctx, existing_byte);
        bits = bits >> take;                                   // sub_16A7DC0
        remaining_bits -= take;
        bit_offset = 0;       // subsequent bytes start at bit 0
        byte_index++;
    }
}
```

This implements the C standard's bitfield byte-packing model: bits are inserted starting at the field's `bit_offset` within its containing byte, potentially spanning multiple bytes. Values wider than 64 bits use heap-backed APInt word arrays.

**Union** (tag 11): Finds the initialized member via two paths:

1. **Designated initializer** (kind 13): `*(init+184)` is the designated field, `*(init+120)` is the actual value expression.
2. **Implicit**: Walk the field chain (`type+160`) looking for the first non-skip, non-bitfield field. Named bitfield members are explicitly rejected: `"initialization of bit-field in union not supported!"`. If no field is found: `"cannot find initialized union member!"`.

The member value is emitted recursively. Padding to the full union byte size is added as `[N x i8] zeroinitializer`. The result is an anonymous `{member_type, [N x i8]}` struct via `ConstantStruct::getAnon` (`sub_159F090`).

**Array** (tag 8): Resolves element type via `GetArrayElementType` (`sub_8D4050`), walks the initializer linked list via `+120` next pointers, calls `EmitConstExpr` recursively for each element. Designated initializers (kind 11) are supported: `*(node+176)` gives the designated element index, `*(node+184)` gives the range count. Type mismatches are handled by `sub_127D000` (resize constant to target type).

When the declared dimension exceeds the initializer count, remaining elements are filled with `Constant::getNullValue`. The result uses `ConstantArray::get` (`sub_159DFD0`) when all elements have the same LLVM type (the common case), or falls back to an anonymous struct via `StructType::get` + `ConstantStruct::get` for heterogeneous cases (which should not occur in well-formed C but is handled defensively).

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
| `sub_127FEC0` | bool-conversion helper | Expression to `i1` conversion |
| `sub_127F650` | `EmitLiteral` | Numeric/string literal emission |
| `sub_1286D80` | `EmitAddressOf` | Compute pointer to lvalue |
| `sub_1287CD0` | lvalue-load workhorse | Loads from computed lvalue with full attribute trimmings (14 args: out-buf, ctx, srcloc, type-info, ...); diagnostic `"unexpected error generating l-value!"` on lvalue-build failure |
| `sub_1287ED0` | `EmitLvalueLoad` | Two-call lvalue-load wrapper: `EmitAddressOf` (`sub_1286D80`) + `EmitLoadFromAddress` (`sub_1287CD0`); shared dispatch target for inner cases `0x03`/`0x06`/`0x08`/`0x5C`/`0x5E`/`0x5F`. Not a compound-assign emitter — the real compound-assign machinery is `sub_12901D0`. |
| `sub_128C390` | `EmitIncDec` | Pre/post increment/decrement |
| `sub_128F9F0` | `EmitBinaryArithBitwise` | Binary arithmetic (`+`,`-`,`*`,`/`,`%`) and bitwise (`&`,`\|`,`^`); diagnostic `"unsupported binary expression!"` |
| `sub_128F580` | `EmitCompare` | Equality and relational comparisons (`==`,`!=`,`<`,`>`,`<=`,`>=`); string label `"cmp"` |
| `sub_128B750` | `EmitSubscriptOp` | Array subscript (GEP + load) |
| `sub_128FDE0` | `EmitSizeofAlignof` | `sizeof` and `alignof` operators |
| `sub_12901D0` | compound-assign wrapper | Wrapper dispatching to per-operator impl |
| `sub_1296570` | compound-statement-as-expression emitter | Walks statement chain (`v16 = *(v16+16)`), pushes scope, emits each via `sub_1296350`; diagnostic `"unexpected: last statement in statement expression is notan expression!"` |
| `sub_12897E0` | `EmitBitfieldStore` (inner) | Actual bitfield store logic |
| `sub_127A030` | `GetLLVMType` | EDG type to LLVM type translation |
| `sub_127F680` | bitfield-fast-path selector | Bitfield path selector |
| `sub_128A3C0` | `__nv_*_rz` intrinsic-call helper | `__nv_*_rz` intrinsic call helper |
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
| `sub_128B370` | `EmitLoad` | Load with volatile/type/srcloc |
| `sub_128BE50` | `EmitCommaOp` | Comma operator RHS extraction |
| `sub_1289860` | `ComputeCompositeMemberAddr` | Multi-level GEP for nested fields |
| `sub_12843D0` | `EmitComplexMemberLoad` | Nested struct/union field load |
| `sub_127FF60` | scalar/aggregate RValue dispatch | Branches on aggregate type (`sub_127B420`): aggregate path calls `sub_12A6C40`, scalar path calls `sub_128F980`; writes result into out-buffer struct (offsets 0/8/16) |
| `sub_1281200` | special-constant materializer | Special constant materialization |
| `sub_1281220` | `EmitInitExpr` | Init expression emission |
| `sub_1285E30` | `EmitBlockAddress` | `blockaddress` / indirect branch |
| `sub_1286000` | `EmitVaArg` | `va_arg` lowering |
| `sub_127FC40` | `CreateAlloca` | Alloca with name and alignment |
| `sub_127B420` | `IsAddressOfExpr` | Check if child is `&` (for elision) |
| `sub_127B3A0` | `IsVolatile` | Volatile type query |
| `sub_127B390` | `GetSMVersion` | Returns current SM target |
| `sub_127B460` | `IsPacked` | Packed struct type query |
| `sub_127B550` | `FatalDiag` | Fatal diagnostic (never returns) |
| `sub_127C5E0` | `AttachDebugLoc` | Debug location attachment |
| `sub_127D2C0` | `ConstantFromType` | Type-level constant (sizeof, etc.) |
| `sub_12A4D00` | `LookupLabel` | Label resolution for goto/address |
| `sub_1648A60` | `AllocateInstruction` | Raw instruction memory allocation |
| `sub_1648B60` | `AllocatePHI` | PHI node memory allocation |
| `sub_164B780` | `SetValueName` | Assigns `%name` to IR value |
| `sub_157E9D0` | `InsertIntoBasicBlock` | BB instruction list insertion |
| `sub_1623A60` | `CloneDebugLoc` | Debug location cloning |
| `sub_1623210` | `RegisterDebugLoc` | Debug location list registration |
| `sub_161E7C0` | `ReleaseDebugLoc` | Debug location list removal |
| `sub_15F1EA0` | `InitInstruction` | Instruction field initialization |
| `sub_15F1F50` | `InitPHINode` | PHI node initialization (opcode 53) |
| `sub_15F2350` | `SetExactFlag` | Mark `sdiv`/`udiv` as `exact` |
| `sub_15F55D0` | `GrowOperandList` | Realloc PHI operand array |
| `sub_15FEC10` | `CreateCmpInst` | ICmp/FCmp instruction creation |
| `sub_15FE0A0` | `CreateIntResize` | Trunc/zext/sext helper |
| `sub_15FB630` | `CreateUnaryOp` | Unary NOT (xor -1) |
| `sub_15F9CE0` | `SetGEPOperands` | GEP operand filling |
| `sub_15FA2E0` | `SetInBoundsFlag` | Mark GEP as inbounds |
| `sub_8D23B0` | `IsArrayType` | Array type check |
| `sub_72B0F0` | `EvaluateConstantExpr` | EDG constant evaluation |
| `sub_731770` | `NeedsBitfieldTemp` | Bitfield temp alloca check |

### Constant expression helper functions

| Address | Recovered name | Role |
|---|---|---|
| `sub_127D8B0` | `EmitConstExpr` | Master constant expression emitter |
| `sub_127D000` | `ResizeConstant` | Resize constant to target type |
| `sub_127D120` | `DestroyAPFloatElement` | APFloat cleanup in aggregate loop |
| `sub_127D2E0` | `PushElementBulk` | Bulk push to element vector |
| `sub_127D5D0` | `PushElement` | Single push to element vector |
| `sub_1277B60` | `GetFieldIndex` | Struct field index query |
| `sub_1276020` | `GetOrCreateGlobalVar` | Global variable creation/lookup |
| `sub_1277140` | `GetOrCreateFunction` | Function creation/lookup |
| `sub_1280350` | `LookupFunctionStaticVar` | Static local variable resolution |
| `sub_126A1B0` | `CreateStringGlobalConst` | Global string constant creation |
| `sub_1598F00` | `ConstantAggregateZero::get` | Zero-initialized aggregate |
| `sub_15991C0` | `ConstantDataArray::getRaw` | Raw byte array constant |
| `sub_159DFD0` | `ConstantArray::get` | Typed array constant |
| `sub_159F090` | `ConstantStruct::get` | Struct constant |
| `sub_15943F0` | `StructType::get` | Anonymous struct type |
| `sub_15A06D0` | `Constant::getNullValue` | Zero constant for any type |
| `sub_15A0A60` | `ConstantExpr::extractvalue` | Sub-element extraction |
| `sub_15A2E80` | `ConstantExpr::getGEP` | Constant GEP expression |
| `sub_15A4510` | `ConstantExpr::getBitCast` | Constant bitcast |
| `sub_15A4A70` | `ConstantExpr::getAddrSpaceCast` | Constant addrspacecast |
| `sub_15A4180` | `ConstantExpr::getPtrToInt` | Constant ptrtoint |
| `sub_15A8020` | `StructLayout::getElemContainingOffset` | Bitfield byte lookup |
| `sub_15A9930` | `DataLayout::getStructLayout` | Struct layout query |
| `sub_620E90` | `edg::IsSignedIntConst` | Signedness query |
| `sub_620FA0` | `edg::GetSignedIntValue` | Signed integer extraction |
| `sub_620FD0` | `edg::GetUnsignedIntValue` | Unsigned integer extraction |
| `sub_622850` | `edg::GetIntConstAsString` | `__int128` decimal string extraction |
| `sub_622920` | `edg::ExtractFieldOffset` | Field offset extraction |
| `sub_709B30` | `edg::ExtractFloatBits` | Float raw bits extraction |
| `sub_722AB0` | `edg::ReadIntFromBuffer` | Endian-aware integer read |
| `sub_8D4050` | `edg::GetArrayElementType` | Array element type query |
| `sub_8D4490` | `edg::GetArrayElementCount` | Array dimension query |

## LLVM Opcode Constants

Numeric opcode constants used in `CreateBinOp`, `CreateCast`, and instruction creation calls throughout the expression codegen:

| Number | LLVM instruction | Used by |
|---|---|---|
| 13 | `sub` | Pointer subtraction step 4 |
| 18 | `sdiv` | Pointer subtraction step 5 (with `exact` flag) |
| 32 | `shl` | Left shift (`<<`) |
| 33 | `ashr` / `lshr` | Right shift (`>>`, signedness-dependent) |
| 34 | `and` (FP variant) | Bitwise AND |
| 35 | `or` (FP variant) | Bitwise OR |
| 36 | `xor` (FP variant) | Bitwise XOR |
| 37 | `zext` | Zero-extend (bool-to-int, `lnot.ext`, `land.ext`) |
| 38 | `and` | Bitwise AND (integer) |
| 39 | `sitofp` / `or` | Signed int-to-float / bitwise OR (integer) |
| 40 | `uitofp` / `xor` | Unsigned int-to-float / bitwise XOR (integer) |
| 41 | `fptosi` / funnel shift | Signed float-to-int / rotate |
| 42 | `fptoui` | Unsigned float-to-int |
| 43 | `fptrunc` | Float-to-float truncation |
| 44 | `fpext` | Float-to-float extension |
| 45 | `ptrtoint` | Pointer-to-integer cast |
| 46 | `inttoptr` | Integer-to-pointer cast |
| 47 | `bitcast` / `addrspacecast` | Pointer casts |
| 51 | ICmp instruction kind | Integer comparison creation |
| 52 | FCmp instruction kind | Float comparison creation |
| 53 | PHI node kind | PHI creation for `&&`, `\|\|`, `?:` |

## PHI Node Construction Detail

PHI nodes are used by three expression types: logical AND (`0x57`), logical OR (`0x58`), and ternary (`0x67`). The construction sequence is identical across all three:

1. **Allocate**: `AllocatePHI` (`sub_1648B60`) with 64 bytes.
2. **Initialize**: `InitPHINode` (`sub_15F1F50`) with opcode 53 (PHI), type, and zero for parent/count/incoming.
3. **Set capacity**: `*(phi+56) = 2` -- two incoming edges.
4. **Set name**: `SetValueName` (`sub_164B780`) with `"land.ext"`, `"lor.ext"`, or `"cond"`.
5. **Reserve slots**: `sub_1648880(phi, 2, 1)` -- reserve 2 incoming at initial capacity 1.

Adding each incoming value:

```c
count = *(phi+20) & 0xFFFFFFF;           // current operand count
if (count == *(phi+56))                   // capacity full?
    GrowOperandList(phi);                 // sub_15F55D0: realloc

new_idx = (count + 1) & 0xFFFFFFF;
*(phi+20) = new_idx | (*(phi+20) & 0xF0000000);  // update count, preserve flags

// Large-mode flag at *(phi+23) & 0x40 selects operand array location:
base = (*(phi+23) & 0x40) ? *(phi-8) : phi_alloc_base - 24*new_idx;

// Value slot: base + 24*(new_idx-1) — 24 bytes per slot (value ptr + use-list pointers)
slot = base + 24*(new_idx - 1);
*slot = value;                           // incoming value
slot[1] = value.use_next;               // link into value's use-list
slot[2] = &value.use_head | (slot[2] & 3);
value.use_head = slot;

// Basic block slot: stored after all value slots as parallel array
bb_offset = base + 8*(new_idx-1) + 24*num_incoming + 8;
*bb_offset = incoming_bb;
```

The PHI operand layout is `[val0, val1, ..., bb0, bb1, ...]` where each value slot occupies 24 bytes (value pointer + doubly-linked use-list pointers), and basic block pointers form a parallel 8-byte array after all value slots.

## Duplicate Implementations

Two additional copies of the bitfield codegen exist at `sub_923780` (store) and `sub_925930` (load) -- identical algorithms with the same string names, same opcodes, same control flow. These are in the `0x92xxxx` range (NVVM frontend region) while the primary copies are in the `0x128xxxx` range (codegen helper region). They likely correspond to different template instantiations or address-space variants in the original NVIDIA source code.

## Diagnostic String Index

| String | Origin function | Trigger |
|---|---|---|
| `"unsupported expression!"` | `EmitExpr` (`sub_128D0F0`) | Default case in outer switch |
| `"unsupported operation expression!"` | `EmitExpr` (`sub_128D0F0`) | Default case in inner switch |
| `"constant expressions are not supported!"` | `EmitConstExpr` (`sub_127D8B0`) | Unsupported context kind (`sub_6E9180` returns true) |
| `"unsupported constant variant!"` | `EmitConstExpr` (`sub_127D8B0`) | Unknown constant kind in main switch; also byte != 0/1/2 in address-of |
| `"unsupported float variant!"` | `EmitConstExpr` (`sub_127D8B0`) | Float kind 5, or kind < 2 |
| `"long double"` / `"__float80"` / `"__float128"` | `EmitConstExpr` (`sub_127D8B0`) | Warning 0xE51: extended precision truncated to double on CUDA target |
| `"failed to lookup function static variable"` | `EmitConstExpr` (`sub_127D8B0`) | Function static address with type tag > 0x10 |
| `"taking address of non-string constant is not supported!"` | `EmitConstExpr` (`sub_127D8B0`) | `&literal` where literal kind != 2 (non-string) |
| `"unsupported cast from address constant!"` | `EmitConstExpr` (`sub_127D8B0`) | Type mismatch that is not ptr-to-ptr or ptr-to-int |
| `"unsupported aggregate constant!"` | `EmitConstExpr` (`sub_127D8B0`) | Type tag not in {8, 10, 11, 12} for aggregate case |
| `"initialization of bit-field in union not supported!"` | `EmitConstExpr` (`sub_127D8B0`) | Union initializer targeting a named bitfield |
| `"cannot find initialized union member!"` | `EmitConstExpr` (`sub_127D8B0`) | Union field chain exhausted without finding target |
| `"bit-field constant must have a known value at compile time!"` | `EmitConstExpr` (`sub_127D8B0`) | Bitfield initializer evaluates to NULL |
| `"unexpected error while initializing bitfield!"` | `EmitConstExpr` (`sub_127D8B0`) | Pre-existing byte in struct is not zero when packing |
| `"unexpected non-integer type for cast from pointer type!"` | `EmitCast` (`sub_128A450`) | `ptrtoint` destination is not integer |
| `"unexpected destination type for cast from pointer type"` | `EmitCast` (`sub_128A450`) | `inttoptr` source is not integer |
| `"error generating code for loading from bitfield!"` | `EmitBitfieldLoad` (`sub_1284570`) | Alignment assertion failure |
| `"expected result type of bassign to be void!"` | `EmitExpr` (`sub_128D0F0`) | Bitfield assign result type validation |

## Cross-References

- [IRGen Types](irgen-types.md) -- type translation from EDG to LLVM
- [Statement Codegen](irgen-stmts.md) -- statement-level emission that calls into `EmitExpr`
- [Cast Codegen detail](irgen-expressions.md#cast--conversion-codegen) -- `EmitCast` subsystem
- [Diagnostics](../infra/diagnostics.md) -- diagnostic emission infrastructure
- [Address Spaces](../reference/address-spaces.md) -- NVPTX address space model affecting pointer casts
