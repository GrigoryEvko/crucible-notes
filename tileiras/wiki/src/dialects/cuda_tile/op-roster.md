# cuda_tile Operation Roster

## Abstract

A frontend emitting `cuda_tile` is writing tile values, structured kernel
control flow, token-ordered memory effects, tensor views, matrix
multiply-accumulate intent, and source-level numeric attributes — everything
the compiler will subsequently lower into private implementation dialects.
This page is the producer and reimplementation reference: operation families,
the behavior each family promises, and how a compiler should lower the surface
without leaning on internal registration details.

In this build, the token-ordered print operation is spelled `cuda_tile.print`.
The newer `cuda_tile.atan2` is rejected outright, so a frontend that supports
multiple TileIR revisions should gate it behind explicit version logic.

## Operation Families

| Family | Operations | Contract |
|---|---|---|
| Floating and integer arithmetic | `absf`, `absi`, `addf`, `addi`, `ceil`, `cmpf`, `cmpi`, `cos`, `cosh`, `divf`, `divi`, `exp`, `exp2`, `floor`, `fma`, `log`, `log2`, `maxf`, `maxi`, `minf`, `mini`, `mulf`, `mulhii`, `muli`, `negf`, `negi`, `pow`, `remf`, `remi`, `rsqrt`, `sin`, `sinh`, `sqrt`, `subf`, `subi`, `tan`, `tanh` | Operate elementwise on scalar or tile values while preserving rounding, signedness, overflow, comparison, and fast-math choices. |
| Integer logic | `andi`, `ori`, `shli`, `shri`, `xori` | Bitwise and shift operations over integer scalar or tile values. |
| Token-ordered memory | `load_ptr_tko`, `load_view_tko`, `store_ptr_tko`, `store_view_tko`, `atomic_cas_tko`, `atomic_rmw_tko`, `make_token`, `join_tokens`, `offset`, `global`, `get_global`, `make_tensor_view`, `make_partition_view` | Express pointer, view, global, token, and atomic memory behavior without committing to backend layout or scheduling. |
| Structured control flow | `module`, `entry`, `if`, `for`, `loop`, `yield`, `break`, `continue`, `return`, `assert`, `assume` | Keep kernel structure in the source dialect and verify region arity, yielded values, and early-exit ancestry. |
| Shape algebra | `broadcast`, `cat`, `extract`, `get_index_space_shape`, `get_num_tile_blocks`, `get_tensor_shape`, `get_tile_block_id`, `iota`, `permute`, `reshape` | Transform tile rank, tile extents, launch geometry, and indexing without choosing hardware layout. |
| Reductions and scans | `reduce`, `scan` | Carry the reduction dimension, identities, input/result types, and pure combiner body. |
| Matrix multiply-accumulate | `mmaf`, `mmai` | Preserve floating and integer MMA intent until atom selection and scheduler lowering. |
| Type conversion | `bitcast`, `exti`, `ftof`, `ftoi`, `int_to_ptr`, `itof`, `ptr_to_int`, `ptr_to_ptr`, `trunci` | Make widening, narrowing, bit reinterpretation, float/int conversion, and pointer casts explicit. |
| Constants, selection, diagnostics | `constant`, `select`, `print` | Materialize literal values, value selection, and token-ordered runtime diagnostics. |

The family boundaries are semantic, not syntactic. `fma` is arithmetic because
it is elementwise; `mmaf` and `mmai` are MMA because they contract matrix
dimensions. `assert` and `assume` live with control flow because regions and
dominance scope their meaning, even though their payload is an attribute or
predicate.

## Producer Contract

A valid producer should build modules with this shape:

```c
cuda_tile.module {
    cuda_tile.entry @kernel(%arg0 : !cuda_tile.tensor_view<...>) {
        %tok0 = cuda_tile.make_token : !cuda_tile.token
        %tile, %tok1 = cuda_tile.load_view_tko %view[%i, %j] token=%tok0
        %acc = cuda_tile.mmaf %a, %b, %c : ...
        %tok2 = cuda_tile.store_view_tko %view[%i, %j], %acc token=%tok1
        cuda_tile.return
    }
}
```

The exact textual syntax is described in [asm-printer.md](asm-printer.md), but
the contract is independent of formatting:

- memory effects are threaded through `cuda_tile.token`;
- tile values have static rank and element type;
- view values carry shape and stride metadata;
- structured control flow yields values rather than branching through `cf`;
- numeric choices such as rounding and signedness are attributes, not implicit
  frontend assumptions;
- debug info and optimization hints may be present but must not be required for
  semantic correctness.

## Lowering Sketch

The first lowering stage converts public `cuda_tile` into alias-aware TileAA.
Arithmetic and shape operations keep their mathematical meaning intact. Memory
operations gain explicit memref and token structure. Control flow is rewritten
only once region and token legality are already proven.

```c
Module lower_cuda_tile_to_tileaa(Module module, Target target) {
    require(module.only_uses_dialect("cuda_tile", "builtin", "arith"));
    verify_cuda_tile_module(module, target);

    TypeConverter types;
    types.add(convert_scalar_type);
    types.add(convert_tile_type);
    types.add(convert_pointer_type);
    types.add(convert_view_type);
    types.add(convert_token_type);

    RewritePatternSet patterns;
    add_arithmetic_patterns(patterns, types);
    add_shape_patterns(patterns, types);
    add_memory_patterns(patterns, types);
    add_control_flow_patterns(patterns, types);
    add_mma_patterns(patterns, types);

    apply_conversion(module, patterns);
    require(!module.contains_dialect("cuda_tile"));
    return module;
}
```

Lowering must not erase source-level facts prematurely. A `load_view_tko`
becomes an operation with explicit view, index, mask, fallback, memory
ordering, memory scope, and token dependencies — not an unstructured pointer
load until the alias and layout passes have the context to handle it safely.

## Numeric Operations

Arithmetic ops accept scalar or tile-shaped operands. Tile operands must agree
on shape and element type unless the op has an explicit shape-changing
contract. Floating operations carry rounding mode and flush-to-zero policy
forward until a lower dialect decides whether the target instruction can encode
those choices directly.

```c
Value lower_elementwise_arith(ArithOp op) {
    require_same_shape(op.operands);
    require_legal_element_type(op);

    NumericPolicy policy = {
        .rounding = op.rounding_mode,
        .flush_to_zero = op.flush_to_zero,
        .signedness = op.signedness,
        .overflow = op.overflow,
    };

    return tileaa_elementwise(op.kind, op.operands, policy);
}
```

`mulhii` returns the high half of a signed integer product. Implement it as a
wide multiply followed by a high-half extract — never as ordinary
multiplication that relies on target-width overflow.

## Operand and Result Tables

The most heavily emitted ops carry the following operand/attribute/result
shape. The `_tko` family threads a `cuda_tile.token` through every memory
effect.

### `cuda_tile.load_view_tko`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | view | `partition_view` | yes | source tile view |
| operand 1..R | indices | `index` | yes (R = tile rank) | per-axis tile coordinate |
| operand R+1 | mask | `tile<S × i1>` | optional | per-lane predicate |
| operand R+2 | other | `tile<S × element>` | optional | fallback value when masked off |
| operand R+3 | token | `cuda_tile.token` | yes | input ordering edge |
| result 0 | value | `tile<S × element>` | yes | matches view element type |
| result 1 | token | `cuda_tile.token` | yes | successor ordering edge |
| attr `mem_semantic` | enum | `weak\|relaxed\|acquire` | optional | acquire requires scope |
| attr `mem_scope` | enum | `tl_blk\|cluster\|gpu\|sys` | conditional | required for non-weak |
| attr `optimization_hints` | dict | architecture-keyed | optional | |
| attr `operandSegmentSizes` | dense i32 | length 5 | yes | `{view, indices, mask, other, token}` |

### `cuda_tile.store_view_tko`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | view | `partition_view` | yes | destination view |
| operand 1 | value | `tile<S × element>` | yes | element type matches view |
| operand 2..R+1 | indices | `index` | yes | per-axis tile coordinate |
| operand R+2 | mask | `tile<S × i1>` | optional | |
| operand R+3 | token | `cuda_tile.token` | yes | input ordering edge |
| result 0 | token | `cuda_tile.token` | yes | successor ordering edge |
| attr `mem_semantic` | enum | `weak\|relaxed\|release` | optional | acquire variants rejected |
| attr `mem_scope` | enum | as above | conditional | |
| attr `operandSegmentSizes` | dense i32 | length 5 | yes | |

### `cuda_tile.atomic_rmw_tko`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | pointers | `tile<S × ptr>` | yes | per-lane address |
| operand 1 | value | `tile<S × element>` | yes | RMW operand |
| operand 2 | mask | `tile<S × i1>` | optional | |
| operand 3 | token | `cuda_tile.token` | yes | |
| result 0 | old | `tile<S × element>` | yes | |
| result 1 | token | `cuda_tile.token` | yes | |
| attr `kind` | enum | `add\|addf\|and\|or\|xor\|xchg\|min\|max\|umin\|umax` | yes | |
| attr `ordering` | enum | full | yes | |
| attr `scope` | enum | full | conditional | |

### `cuda_tile.mmaf` / `cuda_tile.mmai`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | A | `tile<[B ×] M × K × elem_a>` | yes | rank 2 or 3 (batched) |
| operand 1 | B | `tile<[B ×] K × N × elem_b>` | yes | K agrees with A |
| operand 2 | C | `tile<[B ×] M × N × elem_c>` | yes | accumulator |
| result 0 | D | `tile<[B ×] M × N × elem_c>` | yes | shape equals C shape |
| attr `signedness_a` | enum | `signed\|unsigned` | integer MMA | required for `mmai` |
| attr `signedness_b` | enum | `signed\|unsigned` | integer MMA | required for `mmai` |
| attr `rounding` | enum | IEEE basic | optional | `mmaf` only |

### `cuda_tile.if`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | condition | `i1` | yes | scalar predicate |
| region 0 | then | terminated by `yield` | yes | yields result_types |
| region 1 | else | terminated by `yield` | required when results non-empty | yields result_types |
| result 0.. | values | any non-view type | optional | view-typed results rejected |

### `cuda_tile.for`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | lower | integer | yes | |
| operand 1 | upper | integer | yes | same width as lower |
| operand 2 | step | integer | yes | same width as lower |
| operand 3.. | iter args | any non-view | optional | types equal result_types |
| region 0 | body | terminated by `yield` | yes | block arg 0 = induction var |
| result 0.. | yielded iter args | any non-view | optional | |

## Memory and Token Operations

The `_tko` suffix means token ordered. Every token-ordered memory op consumes
an input token and produces a successor. Loads and atomics also produce data;
stores produce only the successor token. That discipline is the public memory
model — later passes refine it into barriers, async copies, and backend memory
instructions.

```c
LoadResult lower_load_ptr_tko(LoadPtrTkoOp op) {
    MemRef ref = make_memref_from_pointer(op.pointer, op.indices);
    MemoryPolicy policy = memory_policy(op.ordering, op.scope, op.hints);

    Value data = tileaa_load(ref, op.mask, op.padding, policy, op.input_token);
    Token next = token_after(data.memory_effect, op.input_token);
    return (LoadResult){ .value = data, .token = next };
}
```

Atomics check both memory ordering and element type. Integer bitwise modes
are integer-only; floating add is floating-only; compare-and-swap is restricted
to element widths the backend can update atomically.

## Structured Control Flow

`cuda_tile` ships its own region operations because frontends need a stable
kernel-level API. Later lowering may translate these regions into SCF, CFG, or
private control-flow dialects, but the verifier enforces these rules first:

- `if` result types match every non-empty yielding branch;
- `for` induction, bounds, step, iter args, and results are type-consistent;
- `loop` iter args and results are type-consistent;
- `break` exits the nearest compatible `loop`;
- `continue` exits to the next iteration of a compatible `for` or `loop`;
- `return` appears in an `entry` context and matches the entry function type;
- `yield` appears only in a parent op that expects region yields.

## MMA Operations

`mmaf` and `mmai` are deliberately narrow public abstractions: they describe
matrix multiply-accumulate intent, not final tensor-core instruction selection.
The verifier checks shape compatibility and element-type legality. Choosing
WGMMA, smaller MMA atoms, tensor-memory paths, or emulation is left to the
lowering pipeline.

```c
LogicalResult verify_mma_shape(Tile lhs, Tile rhs, Tile acc, Tile result) {
    require(lhs.rank == 2 || lhs.rank == 3);
    require(rhs.rank == lhs.rank);
    require(acc.rank == lhs.rank);
    require(result.rank == lhs.rank);

    if (lhs.rank == 3) {
        require(lhs.dim(0) == rhs.dim(0));
        require(lhs.dim(0) == acc.dim(0));
        require(lhs.dim(0) == result.dim(0));
    }

    require(lhs.k_dim == rhs.k_dim);
    require(lhs.m_dim == acc.m_dim);
    require(rhs.n_dim == acc.n_dim);
    require(acc.shape == result.shape);
    return success();
}
```

## Version Notes

- Emit `cuda_tile.print` for runtime diagnostic printing in this build.
- Do not emit `cuda_tile.print_tko` unless targeting a source tree that uses
  that mnemonic.
- Do not emit `cuda_tile.atan2` for this build; guard it behind a newer TileIR
  version check.
- Treat `cuda_tile.string` as implementation-specific unless the target
  contract explicitly documents it.

## Per-Op Method Quads

Every `cuda_tile.*` op carries four registered functions identified by string xrefs in the assembler binary. `build`
points at the op-constructor trampoline (or textual `parse` entry); `reg` points at the registration thunk that interns
the mnemonic into the dialect's `OperationName` table and installs the vtable, folder, and canonicalizer slots; `verify`
points at the `verify(Operation*)` / `InferType` body installed at `AbstractOperation+0x60`; and `lower` points at the
ConvertCudaTileToTileAA pattern-match handler that rewrites the op during the first lowering stage.

| # | mnemonic | build | reg | verify | lower |
|---|---|---|---|---|---|
| 1 | `cuda_tile.absf` | `sub_58C5C0` | `sub_671A00` | `sub_681C10` | `sub_5F8DC0` |
| 2 | `cuda_tile.absi` | `sub_58C930` | `sub_671CA0` | `sub_681AD0` | `sub_5F8DC0` |
| 3 | `cuda_tile.addf` | `sub_58CCA0` | `sub_66DD40` / `sub_670EC0` | `sub_681990` / `sub_6ACBB0` | `sub_5EBED0` |
| 4 | `cuda_tile.addi` | `sub_58D3A0` | — | `sub_681850` / `sub_6ACE40` | `sub_5EBED0` |
| 5 | `cuda_tile.andi` | `sub_58D7B0` | `sub_66D6F0` | `sub_681710` / `sub_6B1D80` | `sub_5F8DC0` |
| 6 | `cuda_tile.assert` | `sub_587B50` | — | `sub_6B1D80` | `sub_5EBED0` |
| 7 | `cuda_tile.assume` | `sub_5A1CA0` | — | `sub_6815D0` / `sub_6AD0C0` | `sub_5EBCF0` |
| 8 | `cuda_tile.atomic_cas_tko` | `sub_58DB20` | — | `sub_681490` / `sub_6B0720` | `sub_5EBED0` |
| 9 | `cuda_tile.atomic_rmw_tko` | `sub_58EF30` | — | `sub_681350` / `sub_6B09C0` | `sub_5EBED0` |
| 10 | `cuda_tile.bitcast` | `sub_5B13D0` | — | `sub_6B1D80` | `sub_5F8970` |
| 11 | `cuda_tile.break` | `sub_5AC120` | `sub_659CF0` / `sub_65A060` / `sub_669F80` | `sub_6B1D80` | — |
| 12 | `cuda_tile.broadcast` | `sub_590280` | `sub_671F40` | — | `sub_5EBED0` |
| 13 | `cuda_tile.cat` | `sub_5A8300` | — | `sub_6AAD90` | `sub_5EBED0` |
| 14 | `cuda_tile.ceil` | `sub_5B13D0` | `sub_6721E0` | `sub_681210` | `sub_5F8DC0` |
| 15 | `cuda_tile.cmpf` | `sub_590560` | — | `sub_6810D0` / `sub_6AD3A0` | `sub_5EBED0` |
| 16 | `cuda_tile.cmpi` | `sub_590F00` | — | `sub_680F90` / `sub_6AD630` | `sub_5EBED0` |
| 17 | `cuda_tile.constant` | `sub_5AFE90` | — | `sub_680E50` / `sub_696C90` / `sub_6B0C70` | `sub_5EBB10` |
| 18 | `cuda_tile.continue` | `sub_5AB850` | — | `sub_6B1D80` / `sub_6BF720` | `sub_60D120` / `sub_60E700` / `sub_60ECC0` / `sub_6C0250` |
| 19 | `cuda_tile.cos` | `sub_5B13D0` | — | `sub_680BD0` / `sub_6B1D80` | `sub_5F8DC0` |
| 20 | `cuda_tile.cosh` | `sub_5B13D0` | — | `sub_680D10` / `sub_6B1D80` | `sub_5F8DC0` |
| 21 | `cuda_tile.divf` | `sub_591400` | — | `sub_680A90` / `sub_6AD8C0` | `sub_5EBED0` |
| 22 | `cuda_tile.divi` | `sub_591B00` | — | `sub_680950` / `sub_6AB040` | `sub_5EBED0` |
| 23 | `cuda_tile.entry` | `sub_5B3D70` / `sub_5BAD00` | — | `sub_6B0ED0` / `sub_6BEC90` | `sub_5F8DC0` |
| 24 | `cuda_tile.exp` | `sub_592670` | — | `sub_6806D0` / `sub_6B1D80` | `sub_5F8DC0` |
| 25 | `cuda_tile.exp2` | `sub_5920A0` | — | `sub_680810` / `sub_6ADB50` | `sub_5EBED0` |
| 26 | `cuda_tile.exti` | `sub_592950` | — | `sub_6AB310` | `sub_5EBED0` |
| 27 | `cuda_tile.extract` | `sub_5A8B60` | — | `sub_6B1D80` | `sub_5EBED0` |
| 28 | `cuda_tile.floor` | `sub_593930` | — | `sub_680590` / `sub_6B1D80` | `sub_5F8DC0` |
| 29 | `cuda_tile.fma` | `sub_593C10` | — | `sub_680450` / `sub_6ADDD0` | `sub_5EB930` |
| 30 | `cuda_tile.for` | `sub_5BBFF0` | `sub_679CA0` | `sub_6B1D80` | `sub_60D120` / `sub_6C0A90` |
| 31 | `cuda_tile.ftof` | `sub_592E80` | — | `sub_6AB5C0` | `sub_5EBED0` |
| 32 | `cuda_tile.ftoi` | `sub_5933B0` | — | `sub_6AB870` | `sub_5EBED0` |
| 33 | `cuda_tile.get_global` | `sub_59E980` | — | `sub_6B1510` | `sub_5EBED0` |
| 34 | `cuda_tile.get_index_space_shape` | `sub_5A9D70` | — | `sub_6B1D80` | `sub_5F8DC0` |
| 35 | `cuda_tile.get_num_tile_blocks` | `sub_5B13D0` | — | `sub_680310` / `sub_6ABB30` | `sub_5EBED0` |
| 36 | `cuda_tile.get_tensor_shape` | `sub_5AA6E0` | — | `sub_6B1D80` | `sub_5EBED0` |
| 37 | `cuda_tile.get_tile_block_id` | `sub_5B13D0` | — | `sub_6801D0` / `sub_6ABDC0` | `sub_5EBED0` |
| 38 | `cuda_tile.global` | `sub_5B0720` / `sub_5B5450` | `sub_672480` | — | `sub_5EB750` |
| 39 | `cuda_tile.if` | `sub_5BCCD0` | `sub_66D820` / `sub_679AC0` / `sub_679CA0` | `sub_693950` / `sub_693BF0` / `sub_694300` / `sub_6950B0` / `sub_6A9460` / `sub_6B1D80` / `sub_6BA3C0` / `sub_6BEC90` | `sub_60D120` / `sub_60ECC0` / `sub_6C0A90` / `sub_6C0D60` |
| 40 | `cuda_tile.int_to_ptr` | `sub_5B13D0` | — | `sub_6B1D80` | `sub_5F8970` |
| 41 | `cuda_tile.iota` | `sub_5B13D0` | — | `sub_6B1D80` | `sub_5EBED0` |
| 42 | `cuda_tile.itof` | `sub_594400` | — | `sub_6AC050` | `sub_5EB570` |
| 43 | `cuda_tile.join_tokens` | `sub_5AAF80` | — | `sub_680090` / `sub_6B1D80` | `sub_5F8DC0` |
| 44 | `cuda_tile.load_ptr_tko` | `sub_5A30D0` | `sub_6727B0` | — | `sub_5F8DC0` |
| 45 | `cuda_tile.load_view_tko` | `sub_5A4420` | `sub_672A80` | — | `sub_5F8DC0` |
| 46 | `cuda_tile.log` | `sub_5B13D0` | `sub_672D50` / `sub_67FE10` | — | `sub_5F8DC0` |
| 47 | `cuda_tile.log2` | `sub_594980` | `sub_67FF50` | `sub_6B1D80` | `sub_5F8DC0` |
| 48 | `cuda_tile.loop` | `sub_5BDA00` | `sub_669F80` / `sub_679AC0` / `sub_679CA0` | `sub_6B1D80` | `sub_60D120` / `sub_6107C0` / `sub_6C0A90` / `sub_6C0D60` |
| 49 | `cuda_tile.make_partition_view` | `sub_594CF0` | `sub_672FF0` | — | `sub_5EBED0` |
| 50 | `cuda_tile.make_tensor_view` | `sub_5AE190` | — | `sub_6AC310` | `sub_5EBED0` |
| 51 | `cuda_tile.make_token` | `sub_5B13D0` | `sub_673290` / `sub_67FCD0` | — | `sub_5F8DC0` |
| 52 | `cuda_tile.maxf` | `sub_594FD0` | `sub_67FB90` | `sub_6AE060` | `sub_5EBED0` |
| 53 | `cuda_tile.maxi` | `sub_595630` | `sub_67FA50` | `sub_6AE2F0` | `sub_5EB390` |
| 54 | `cuda_tile.minf` | `sub_595B60` | `sub_67F910` | `sub_6AE570` | `sub_5EBED0` |
| 55 | `cuda_tile.mini` | `sub_5961C0` | `sub_67F7D0` | `sub_6AE800` | `sub_5EBED0` |
| 56 | `cuda_tile.mmaf` | `sub_5966F0` | `sub_673530` / `sub_67F690` | — | `sub_5EBED0` |
| 57 | `cuda_tile.mmai` | `sub_596A60` | `sub_67F550` | `sub_6AEA80` | `sub_5EBED0` |
| 58 | `cuda_tile.module` | `sub_5B5450` / `sub_5BE6E0` | — | `sub_6B1790` | `sub_5F8DC0` |
| 59 | `cuda_tile.mulf` | `sub_596EE0` | `sub_67F410` | `sub_6AED10` | `sub_5EBED0` |
| 60 | `cuda_tile.mulhii` | `sub_5979F0` | `sub_67F190` | `sub_6B1D80` | `sub_5EBED0` |
| 61 | `cuda_tile.muli` | `sub_5975E0` | `sub_67F2D0` | `sub_6AEFA0` | `sub_5EBED0` |
| 62 | `cuda_tile.negf` | `sub_5B13D0` | `sub_67F050` | `sub_6B1D80` | `sub_5F8DC0` |
| 63 | `cuda_tile.negi` | `sub_5B13D0` | `sub_67EF10` | `sub_6B1D80` | `sub_5EBED0` |
| 64 | `cuda_tile.offset` | `sub_597CD0` | `sub_6737D0` / `sub_67EDD0` | — | `sub_5EBED0` |
| 65 | `cuda_tile.ori` | `sub_5B13D0` | `sub_67EC90` | `sub_6B1D80` | `sub_5F8DC0` |
| 66 | `cuda_tile.permute` | `sub_59E060` | — | `sub_6AC5E0` | `sub_5EBED0` |
| 67 | `cuda_tile.pow` | `sub_597FB0` | `sub_673A70` / `sub_67EB50` | — | `sub_5F8DC0` |
| 68 | `cuda_tile.print` | `sub_5AD2C0` | — | `sub_6B1D80` | `sub_5EBED0` |
| 69 | `cuda_tile.ptr_to_int` | `sub_598290` | — | `sub_6B1D80` | `sub_5F8970` |
| 70 | `cuda_tile.ptr_to_ptr` | `sub_598570` | — | `sub_6B1D80` | `sub_5F8970` |
| 71 | `cuda_tile.reduce` | `sub_5BF2E0` | `sub_6631F0` / `sub_67EA10` | `sub_6AF220` / `sub_6BA3C0` | `sub_5EBED0` |
| 72 | `cuda_tile.remf` | `sub_5B13D0` | `sub_67E8D0` | `sub_6B1D80` | `sub_5F8DC0` |
| 73 | `cuda_tile.remi` | `sub_598850` | `sub_67E790` | `sub_6AF510` | `sub_5EBED0` |
| 74 | `cuda_tile.reshape` | `sub_598D80` | `sub_673D10` | — | `sub_5EBED0` |
| 75 | `cuda_tile.return` | `sub_5A9400` | — | `sub_6B1D80` | `sub_5F8DC0` / `sub_60ECC0` / `sub_6C07B0` |
| 76 | `cuda_tile.rsqrt` | `sub_599110` | `sub_67E650` | `sub_6AF790` | `sub_5EB1B0` |
| 77 | `cuda_tile.scan` | `sub_5B9B20` | `sub_6639E0` / `sub_67E510` | `sub_6B1A70` / `sub_6BA3C0` | `sub_5EBED0` |
| 78 | `cuda_tile.select` | `sub_5B13D0` | `sub_66DA50` / `sub_673FB0` / `sub_67E3D0` | `sub_694300` | `sub_5F8DC0` |
| 79 | `cuda_tile.shli` | `sub_599700` | `sub_67E290` | `sub_6AFA10` | `sub_5EBED0` |
| 80 | `cuda_tile.shri` | `sub_599B10` | `sub_67E150` | `sub_6AFC90` | `sub_5EBED0` |
| 81 | `cuda_tile.sin` | `sub_59A3B0` | `sub_67DED0` | `sub_6B1D80` | `sub_5F8DC0` |
| 82 | `cuda_tile.sinh` | `sub_59A040` | `sub_67E010` | `sub_6B1D80` | `sub_5F8DC0` |
| 83 | `cuda_tile.sqrt` | `sub_59A690` | `sub_67DD90` | `sub_6AFF10` | `sub_5EBED0` |
| 84 | `cuda_tile.store_ptr_tko` | `sub_5A55B0` | `sub_674250` / `sub_67DC50` | — | `sub_5F8DC0` |
| 85 | `cuda_tile.store_view_tko` | `sub_5A6790` | `sub_674510` / `sub_67DB10` | — | `sub_5F8DC0` |
| 86 | `cuda_tile.subf` | `sub_59B0E0` | `sub_67D9D0` | `sub_6B01A0` | `sub_5EBED0` |
| 87 | `cuda_tile.subi` | `sub_59B7E0` | `sub_67D890` | `sub_6B0430` | `sub_5EBED0` |
| 88 | `cuda_tile.tan` | `sub_5B13D0` | `sub_67D610` | `sub_6B1D80` | `sub_5F8DC0` |
| 89 | `cuda_tile.tanh` | `sub_59BBF0` | `sub_6747D0` / `sub_67D750` | — | `sub_5F8DC0` |
| 90 | `cuda_tile.trunci` | `sub_59BF60` | — | `sub_6AC890` | `sub_5EAFD0` |
| 91 | `cuda_tile.xori` | `sub_59C3A0` | `sub_674A70` / `sub_67D4D0` | `sub_696C90` | `sub_5F8DC0` |
| 92 | `cuda_tile.yield` | `sub_5AC9F0` | `sub_66D5B0` | `sub_6B1D80` / `sub_6BE9B0` / `sub_6BF0A0` / `sub_6BFBF0` | `sub_60D120` |

Four shared-fallback addresses dominate the empty cells. `sub_5B13D0` is the default builder for trivial unary ops —
the master bytecode-reader dispatcher doubles as a build trampoline for the 20-odd ops whose construction has no special
inits (`bitcast`, `ceil`, `cos`, `cosh`, `int_to_ptr`, `iota`, `log`, `make_token`, `negf`, `negi`, `ori`, `remf`,
`select`, `sin`, `sinh`, `tan`, and the `get_*` accessors). `sub_5EBED0` is the default Part A lower: the
ConvertCudaTileToTileAA arithmetic-group secondary op-kind dispatcher (13.4K, 496 basic blocks, 45 string xrefs), which
covers most elementwise, shape, view, and atomic ops. `sub_5F8970` is the default lower for Part C, the small
pointer-cast specialty set (`bitcast`, `int_to_ptr`, `ptr_to_int`, `ptr_to_ptr`); `mma`, `reduce`, `scan`, and the
transcendentals route through the Part A dispatcher instead. `sub_6B1D80` is the default register thunk's verifier
sentinel — the "no custom verify" stub that ops install when they need nothing beyond the trait-level invariants the
framework already enforces.

One count discrepancy is worth flagging. Some places in `overview.md` advertise 94 ops, but the actual roster in this
build is 92. The two missing names are `cuda_tile.atan2` (excluded from the closed-source assembler — OSS-only) and the
rename `cuda_tile.print_tko` → `cuda_tile.print` (counted once in the binary roster above but listed under both
spellings in OSS-facing documentation). Producers targeting this build should follow the version notes above and emit
only the 92 mnemonics in the table.

The dialect constructor at `sub_6B3ED0` (see [overview.md](overview.md)) walks the `reg`-column thunks in roster order
and each thunk in turn writes the mnemonic string into the `OperationName` slot at `Operation+0x40` described in
[../../mlir-infra/operation-layout.md](../../mlir-infra/operation-layout.md), installs the vtable pointer that captures
the fold and canonicalizer callbacks, and chains into the shared `sub_4481530` interning helper for the literal
`StringRef`. The `verify` column entries land in the `AbstractOperation+0x60` slot; the `lower` column entries are
matched as conversion patterns by the Part A and Part C dispatchers during ConvertCudaTileToTileAA.
