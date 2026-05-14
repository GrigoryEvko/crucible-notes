# .td Files Delta (OSS to binary)

## Abstract

The public `cuda_tile` dialect surface is declared by `Types.td`, `AttrDefs.td`, and `Ops.td`.
Tileiras matches almost all of that surface. The meaningful deltas are small and important:

- `Ops.td`: 92 operations are present unchanged, `print_tko` is renamed to `print`, and `atan2`
  is absent because it belongs to the later 13.2 dialect surface.
- `AttrDefs.td`: the attribute surface is present, including debug-info attributes and assumption
  predicate attributes.
- `Types.td`: all concrete public types, aliases, and predicate helpers are present, with one
  extra runtime mnemonic, `cuda_tile.string`, used by printing.
- `Float8E8M0FNU` survives as an alias dependency but is not part of the observed accepted element
  universe.

For reimplementation, treat tileiras as a `cuda_tile` 13.1 dialect with one mnemonic rename and one
binary-only string type used by printing.

## Types.td

`Types.td` declares five concrete types, thirteen aliases, and predicate/class-template helpers.
The public concrete types are:

| Definition | Mnemonic | Status |
| --- | --- | --- |
| `CudaTile_PointerType` | `cuda_tile.ptr` | present |
| `CudaTile_TileType` | `cuda_tile.tile` | present |
| `CudaTile_TensorViewType` | `cuda_tile.tensor_view` | present |
| `CudaTile_PartitionViewType` | `cuda_tile.partition_view` | present |
| `CudaTile_TokenType` | `cuda_tile.token` | present |
| tileiras-only | `cuda_tile.string` | added for `cuda_tile.print` formatting |

The public scalar aliases are present: `i1`, `i8`, `i16`, `i32`, `i64`, `f16`, `bf16`, `f32`,
`tf32`, `f64`, `f8E4M3FN`, `f8E5M2`, and `f8E8M0FNU`. The last alias is carried through ODS
predicate expansion but has no observed consumer in tileiras. Practical element-type validation
ends at `f8E5M2`.

The predicate helpers are also present: integer, float, number, tile-element, tile-of,
ranked-tile-of, scalar-tile-of, integer-tile, base-float-tile, float-tile, number-tile, and
pointer-tile predicates. These are generated into consuming verifier bodies rather than exposed as
standalone runtime APIs.

## AttrDefs.td

All public attributes are present. The important groups are:

| Group | Attributes |
| --- | --- |
| Arithmetic enums | `signedness`, `overflow`, `rounding`, `comparison_ordering`, `comparison_predicate` |
| Atomics and memory | `AtomicRMWModeAttr`, `MemoryScopeAttr`, `MemoryOrderingSemanticsAttr` |
| Assumption predicates | `div_by`, `same_elements`, `bounded` |
| Layout and padding | `optimization_hints`, `padding_value` |
| Debug info | `di_loc`, `di_compile_unit`, `di_file`, `di_lexical_block`, `di_subprogram` |
| Debug-info bases | `DIAttr`, `DINodeAttr`, `DIScopeAttr`, `DILocalScopeAttr` |

`DivByAttr`, `SameElementsAttr`, and `BoundedAttr` implement
`AssumePredicateAttrInterface`, so `cuda_tile.assume` verifies them through the same predicate
interface. `DivByAttr` requires a custom assembly format for its `div_by<...>` grammar.

`AtomicRMWModeAttr` has ten cases: `AND`, `OR`, `XOR`, `ADD`, `ADDF`, `MAX`, `MIN`, `UMAX`, `UMIN`,
and `XCHG`.

`OptimizationHintsAttr` accepts SM keys for `sm_80`, `sm_86`, `sm_87`, `sm_88`, `sm_89`, `sm_90`,
`sm_100`, `sm_103`, `sm_110`, `sm_120`, and `sm_121`. The useful keys are `kNumCTAInCGA`,
`kAllowTMA`, `kLatency`, and `kOccupancy`.

## Ops.td

`Ops.td` declares 94 operation records plus `CudaTile_FmaTile` and the load/store base templates.
Tileiras exposes the 13.1 operation surface:

| Definition | Mnemonic | Status |
| --- | --- | --- |
| `CudaTile_AbsFOp` | `absf` | present |
| `CudaTile_AbsIOp` | `absi` | present |
| `CudaTile_AddIOp` | `addi` | present |
| `CudaTile_AddFOp` | `addf` | present |
| `CudaTile_AndIOp` | `andi` | present |
| `CudaTile_AssertOp` | `assert` | present |
| `CudaTile_AssumeOp` | `assume` | present |
| `CudaTile_Atan2Op` | `atan2` | absent, 13.2-only |
| `CudaTile_AtomicCASTkoOp` | `atomic_cas_tko` | present |
| `CudaTile_AtomicRMWTkoOp` | `atomic_rmw_tko` | present |
| `CudaTile_BitcastOp` | `bitcast` | present |
| `CudaTile_BroadcastOp` | `broadcast` | present |
| `CudaTile_CatOp` | `cat` | present |
| `CudaTile_CosOp` | `cos` | present |
| `CudaTile_CosHOp` | `cosh` | present |
| `CudaTile_BreakOp` | `break` | present |
| `CudaTile_CeilOp` | `ceil` | present |
| `CudaTile_CmpFOp` | `cmpf` | present |
| `CudaTile_CmpIOp` | `cmpi` | present |
| `CudaTile_ConstantOp` | `constant` | present |
| `CudaTile_ContinueOp` | `continue` | present |
| `CudaTile_GetIndexSpaceShapeOp` | `get_index_space_shape` | present |
| `CudaTile_GetTensorShapeOp` | `get_tensor_shape` | present |
| `CudaTile_DivFOp` | `divf` | present |
| `CudaTile_DivIOp` | `divi` | present |
| `CudaTile_MmaFOp` | `mmaf` | present |
| `CudaTile_MmaIOp` | `mmai` | present |
| `CudaTile_ExtractOp` | `extract` | present |
| `CudaTile_ExpOp` | `exp` | present |
| `CudaTile_Exp2Op` | `exp2` | present |
| `CudaTile_ExtIOp` | `exti` | present |
| `CudaTile_ForOp` | `for` | present |
| `CudaTile_FloorOp` | `floor` | present |
| `CudaTile_FmaOp` | `fma` | present |
| `CudaTile_FToFOp` | `ftof` | present |
| `CudaTile_FToIOp` | `ftoi` | present |
| `CudaTile_EntryOp` | `entry` | present |
| `CudaTile_GetTileBlockIdOp` | `get_tile_block_id` | present |
| `CudaTile_GetNumTileBlocksOp` | `get_num_tile_blocks` | present |
| `CudaTile_GetGlobalOp` | `get_global` | present |
| `CudaTile_GlobalOp` | `global` | present |
| `CudaTile_IfOp` | `if` | present |
| `CudaTile_IntToPtrOp` | `int_to_ptr` | present |
| `CudaTile_IotaOp` | `iota` | present |
| `CudaTile_JoinTokensOp` | `join_tokens` | present |
| `CudaTile_TruncIOp` | `trunci` | present |
| `CudaTile_IToFOp` | `itof` | present |
| `CudaTile_LoadViewTkoOp` | `load_view_tko` | present |
| `CudaTile_LoadPtrTkoOp` | `load_ptr_tko` | present |
| `CudaTile_LogOp` | `log` | present |
| `CudaTile_Log2Op` | `log2` | present |
| `CudaTile_LoopOp` | `loop` | present |
| `CudaTile_MakeTensorViewOp` | `make_tensor_view` | present |
| `CudaTile_MaxFOp` | `maxf` | present |
| `CudaTile_MaxIOp` | `maxi` | present |
| `CudaTile_MinFOp` | `minf` | present |
| `CudaTile_MinIOp` | `mini` | present |
| `CudaTile_ModuleOp` | `module` | present |
| `CudaTile_MulFOp` | `mulf` | present |
| `CudaTile_MulIOp` | `muli` | present |
| `CudaTile_MulhiIOp` | `mulhii` | present |
| `CudaTile_NegIOp` | `negi` | present |
| `CudaTile_NegFOp` | `negf` | present |
| `CudaTile_MakeTokenOp` | `make_token` | present |
| `CudaTile_OffsetOp` | `offset` | present |
| `CudaTile_PermuteOp` | `permute` | present |
| `CudaTile_PowOp` | `pow` | present |
| `CudaTile_PrintTkoOp` | `print` | renamed from OSS `print_tko` |
| `CudaTile_PtrToIntOp` | `ptr_to_int` | present |
| `CudaTile_PtrToPtrOp` | `ptr_to_ptr` | present |
| `CudaTile_ReduceOp` | `reduce` | present |
| `CudaTile_RemIOp` | `remi` | present |
| `CudaTile_ReshapeOp` | `reshape` | present |
| `CudaTile_ReturnOp` | `return` | present |
| `CudaTile_ScanOp` | `scan` | present |
| `CudaTile_SelectOp` | `select` | present |
| `CudaTile_ShLIOp` | `shli` | present |
| `CudaTile_ShRIOp` | `shri` | present |
| `CudaTile_SinOp` | `sin` | present |
| `CudaTile_SinHOp` | `sinh` | present |
| `CudaTile_StorePtrTkoOp` | `store_ptr_tko` | present |
| `CudaTile_StoreViewTkoOp` | `store_view_tko` | present |
| `CudaTile_SubFOp` | `subf` | present |
| `CudaTile_SubIOp` | `subi` | present |
| `CudaTile_TanOp` | `tan` | present |
| `CudaTile_TanHOp` | `tanh` | present |
| `CudaTile_MakePartitionViewOp` | `make_partition_view` | present |
| `CudaTile_XOrIOp` | `xori` | present |
| `CudaTile_YieldOp` | `yield` | present |
| `CudaTile_OrIOp` | `ori` | present |
| `CudaTile_RemFOp` | `remf` | present |
| `CudaTile_RsqrtOp` | `rsqrt` | present |
| `CudaTile_SqrtOp` | `sqrt` | present |
| `CudaTile_FmaTile` | type constraint | present, verifier-only |

The renamed print op preserves the public operation role but uses `cuda_tile.print` throughout
tileiras parsing, bytecode, and diagnostics. The absent `atan2` operation is the only listed op
from the 13.2 surface; it should not be accepted by a strict tileiras-compatible 13.1 parser.

