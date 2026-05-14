# cuda_tile Simplifier Walker

## Abstract

The `cuda_tile` simplifier walker is a recursive expression-tree rewriter sitting between the public MLIR `cuda_tile.*` operation layer and the storage/uniquer layer. It lifts a fully elaborated CUDA tile op graph into a private expression IR, runs constant folding, identity rewrites, and structural deduplication over that IR, then materializes canonical MLIR values back through the normal builders.

Separation is the key design. The simplifier never mutates arbitrary MLIR operations while reasoning — it works on compact expression records, memoizes simplified results in two caches, and re-emits public operations only after the recursive fold has selected a canonical shape.

Tileiras runs the simplifier *underneath* the canonicaliser layer rather than next to it. The public canonicaliser (covered by `dialects/cuda_tile/canonicalizers-and-folds.md`) sees only the result of this private walk. Reimplementers who try to merge the simplifier and the canonicaliser into one pass produce quadratic IR churn: the private expression IR is what lets the simplifier dedupe a shared subgraph in one place instead of repeatedly rewriting the public op graph. The page is organised as the boundary contract first (private expression kinds, the binary-arith dispatch table that maps internal kinds to public `arith.*` opcodes), then the materialiser last.

## Private Expression IR

Every private expression node has the same logical header:

- a 16-bit expression kind
- a small flag field
- a pointer to operands or trailing operand storage
- a packed operand count

The expression record is not an `Operation *` — it is a side representation built for folding. MLIR values enter and leave through mapping tables, which lets the walker share subexpressions and avoid rewriting the public op graph until the chosen replacement is ready.

Operand storage mirrors LLVM and MLIR trailing objects: small operand lists sit inline, while larger lists use a separately allocated trailing array. Tombstone nodes use the sentinel expression kind, removed by compaction before the packed count is recomputed.

## Recursive Simplifier

The main simplifier dispatches on 17 expression kinds. Unary nodes recurse into their only operand,
variadic boolean nodes fold every child, binary arithmetic nodes share one builder, and leaf nodes
classify bit-vector payloads as constants, undef values, or symbols. The caller chooses which cache
to use, and a tunable recursion limit protects the pass from pathological expression graphs.

```c
Value *simplify_expr(SimplifyContext *ctx, Expr *expr, CacheKind cache, unsigned depth) {
    if (depth > ctx->recursion_limit) {
        return materialize_without_deepening(ctx, expr);
    }

    if (Value *cached = cache_lookup(ctx, cache, expr)) {
        return cached;
    }

    Value *result = NULL;
    switch (expr->kind) {
    case EK_CONSTANT:
        result = materialize_constant(ctx, expr);
        break;
    case EK_NOT:
    case EK_NEG:
    case EK_ABS:
        result = simplify_unary(ctx, expr, cache, depth + 1);
        break;
    case EK_AND:
    case EK_OR:
        result = simplify_variadic_boolean(ctx, expr, cache, depth + 1);
        break;
    case EK_ADD:
    case EK_SUB:
    case EK_MUL:
    case EK_UDIV:
    case EK_SDIV:
        result = simplify_binary_arithmetic(ctx, expr, cache, depth + 1);
        break;
    case EK_SELECT:
        result = simplify_select(ctx, expr, cache, depth + 1);
        break;
    case EK_CAST:
        result = simplify_cast(ctx, expr, cache, depth + 1);
        break;
    case EK_BITVEC_CONST:
        result = materialize_bitvec_leaf(ctx, expr);
        break;
    default:
        bug_invalid_expr_kind(expr->kind);
    }

    cache_insert(ctx, cache, expr, result);
    return result;
}
```

## SimplifierExprKind enum

| Value | Name | Meaning |
|------:|------|---------|
| 0 | `EK_Sentinel0` | invalid or uninitialized record |
| 1 | `EK_Constant` | folded constant |
| 2 | `EK_NotI` | unary bitwise not |
| 3 | `EK_NegI` | unary arithmetic negate |
| 4 | `EK_AbsI` | unary absolute value |
| 5 | `EK_AndN` | variadic bitwise and |
| 6 | `EK_OrN` | variadic bitwise or |
| 7 | `EK_EqBin` | binary equality predicate |
| 8 | `EK_Select` | `select(condition, true_value, false_value)` |
| 9 | `EK_Add` | binary addition |
| 10 | `EK_Sub` | binary subtraction |
| 11 | `EK_Mul` | binary multiplication |
| 12 | `EK_DivU` | unsigned division |
| 13 | `EK_DivS` | signed division |
| 14 | `EK_Cast` | passthrough or narrowing/widening cast |
| 15 | `EK_BitVecConst` | bit-vector leaf payload |
| 16 | `EK_Sentinel16` | invalid terminator |

## Type Dispatcher

The second walker dispatches on a compact type-kind byte. Its covered range aligns with the CUDA-tile bytecode opcode band from `FToFOp` through `PtrToIntOp`, reserved slots included. That alignment does not make it the public bytecode enum — it is an internal type-kind enum that deliberately uses the same dense range so the dispatcher can share one contiguous jump table.

Most cases share one traversal body. A few are conditional on caller mode, and one uses a separate handler. The reimplementation detail to preserve is operand discovery: read the type-kind record, choose inline or heap trailing operands according to the storage flag, then recursively walk child types before calling the uniquing layer.

```c
Type *walk_cuda_tile_type(TypeWalkContext *ctx, TypeRecord *record, bool conditional_mode) {
    TypeKind kind = record->kind;
    TypeRange operands = type_record_operands(record);

    if (type_kind_is_conditional(kind) && !conditional_mode) {
        return record->original_type;
    }

    SmallVector<Type *, 8> rewritten;
    for (Type *operand : operands) {
        rewritten.push_back(walk_cuda_tile_type(ctx, type_record(operand), conditional_mode));
    }

    return unique_cuda_tile_type(ctx, kind, rewritten);
}
```

## Binary-Arith Dispatch Table

The inner switch that handles binary integer arithmetic does not branch op-by-op. Cases 9 through 13
share one body, and that body uses the index `v23 - 9` to read a 5-entry table at `dword_4F99CE0`.
The table maps the simplifier's local kind index into the public `arith.*` opcode that the
materialiser will emit.

| `v23 - 9` | `dword_4F99CE0[i]` | Arith opcode | Notes |
|----------:|-------------------:|--------------|-------|
| 0 | `0x52` | `arith.addi`  | case 9, integer add |
| 1 | `0x54` | `arith.subi`  | case 10, integer subtract |
| 2 | `0x56` | `arith.muli`  | case 11, integer multiply |
| 3 | `0x58` | `arith.divsi` | case 12, integer signed divide |
| 4 | `0x5A` | `arith.remsi` | case 13, integer signed remainder |

The 5-row partition reflects the simplifier's scope. Only integer arith ops are touched at the IR level. Floating-point arithmetic flows downstream: FMA shapes form in the dedicated fusion pass, and remaining floating-point rewrites fall to the LLVM optimiser. This keeps the simplifier's fold rules small and lets the table double as a sanity check on the inner switch.

## Materializer

`sub_3BBED30` is the materialiser. It consumes the per-op rewrite result produced by `simplify_binary_arithmetic` and its siblings, writing the chosen replacement back into the MLIR op graph. The rewrite result is a compact record:

```c
typedef struct {
    Operation        *original;       // op being replaced
    SmallVector_Value new_operands;   // canonical operand list
    OpKind            new_kind;       // local simplifier kind, mapped via dword_4F99CE0
    ArrayAttr         new_attrs;      // attribute set for the new op
} SimplifierResult;
```

The materialiser runs four steps in fixed order. It looks up the canonical `OperationName` for
`new_kind`, asks the uniquer for an interned op carrying that tuple, falls back to the builder if
the tuple has not been seen before, then rewires uses and erases the original.

Step 1 uses the public `arith.*` opcode from `dword_4F99CE0`, not the local simplifier kind. The
table lookup happens at the boundary between the private expression IR and public MLIR, so the
materialiser never sees the simplifier's internal numbering.

Steps 1 through 3 go through a paired uniquer and builder. `sub_3F1A460` is the uniquer: given a
`(name, operands, attrs)` tuple it returns the canonical, deduplicated `Operation *`. The
simplifier walks many subgraphs and tends to produce structurally identical ops; the uniquer
guarantees one canonical instance per tuple, which keeps later CSE and pattern matching cheap.
`sub_3F1A5E0` is the builder: given the same tuple, it materialises a fresh `Operation *` through
the MLIR OpBuilder API. The builder runs only when the uniquer has no entry yet.

The pair is split because some rewrites produce new constants. Those constants must go through the
StorageUniquer first (see [`mlir-infra/storage-uniquer-and-context-impl.md`](../mlir-infra/storage-uniquer-and-context-impl.md))
before they can appear as operands. The uniquer side is the lookup; the builder side is the
allocator that runs on a uniquer miss.

```c
Operation *materialize(const SimplifierResult *r) {
    OperationName name = lookupOpName(r->new_kind);

    Operation *unique = sub_3F1A460(name, r->new_operands, r->new_attrs);   // uniquer
    if (!unique) {
        unique = sub_3F1A5E0(name, r->new_operands, r->new_attrs);          // builder fallback
    }

    r->original->replaceAllUsesWith(unique);
    r->original->erase();
    return unique;
}
```

`replaceAllUsesWith` runs before `erase`. The order matters: erasing first would drop the SSA def while live uses still reference it, which the MLIR verifier rejects. The materialiser is the only place in the simplifier that mutates the public op graph; every step before it operates on the private expression IR and on uniquer queries.

## DenseElements Debug Printer

The `DenseElementsAttr` printer sits off the hot simplification path. An inbound-file debug replay path reaches it; from there it reads an element buffer and formats it as comma-separated values with no surrounding brackets and no shape prefix. Invalid element type codes abort through the same internal bug path used by invalid expression kinds.

## DenseElementsTypeCode

| Value | Name | Element type |
|------:|------|--------------|
| 0 | `DETC_Reserved0` | BUG (invalid) |
| 1 | `DETC_F32` | 32-bit IEEE float |
| 2 | `DETC_F64` | 64-bit IEEE float |
| 3 | `DETC_I8` | signed 8-bit integer |
| 4 | `DETC_I16` | signed 16-bit integer |
| 5 | `DETC_I32` | signed 32-bit integer |
| 6 | `DETC_I64` | signed 64-bit integer |
| 7 | `DETC_U8` | unsigned 8-bit integer |
| 8 | `DETC_U16` | unsigned 16-bit integer |
| 9 | `DETC_U32` | unsigned 32-bit integer |
| 10 | `DETC_U64` | unsigned 64-bit integer |
| 11 | `DETC_Reserved11` | BUG (invalid) |

```c
void append_dense_elements_debug_string(StringBuilder *out,
                                        const void *data,
                                        DenseElementsMetadata meta) {
    for (uint64_t i = 0; i < meta.element_count; ++i) {
        if (i != 0) {
            string_builder_append(out, ",");
        }

        switch (meta.type_code) {
        case DETC_F32:
            append_f32(out, load_f32(data, i));
            break;
        case DETC_F64:
            append_f64(out, load_f64(data, i));
            break;
        case DETC_I8:
        case DETC_I16:
        case DETC_I32:
        case DETC_I64:
            append_signed_decimal(out, load_signed_integer(data, meta, i));
            break;
        case DETC_U8:
        case DETC_U16:
        case DETC_U32:
        case DETC_U64:
            append_unsigned_decimal(out, load_unsigned_integer(data, meta, i));
            break;
        default:
            bug_invalid_dense_element_type(meta.type_code);
        }
    }
}
```

## Reimplementation Notes

A compatible simplifier should preserve the following contracts:

- Keep the expression IR private; do not use MLIR `Operation *` as the fold record.
- Memoize by expression node and cache mode so shared SSA subgraphs are folded once.
- Treat sentinel expression kinds and reserved dense-element type codes as bugs.
- Bound recursion and fall back to conservative materialization when the bound is exceeded.
- Re-emit public `cuda_tile.*` operations only through canonical builders and uniquers.
- Map the local binary-arith kind through `dword_4F99CE0` before any public-op lookup; never expose
  the simplifier's internal kind to the materialiser.
- Pair uniquer and builder so the uniquer fronts every materialise call and the builder runs only
  on a uniquer miss.
- In the materialiser, call `replaceAllUsesWith` before `erase`; never erase a def with live uses.
- Keep the DenseElements printer debug-only; it should not run during normal folding.
