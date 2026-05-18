# Op Mnemonic Master Table

Every MLIR operation mnemonic registered by — or observed in lowerings
driven by — the `tileiras` ELF (CUDA Toolkit 13.1, SHA256
`f0eb415767f403c96cbabf0817c3bcf70a50f88dfc8845fe36ebe21635fa6707`).
Nine dialect namespaces, ~640 first-class ops, alphabetical within each
namespace. Columns: verbatim mnemonic in backticks, mnemonic length in
bytes (`-` where the registrar uses a non-flat path that does not pass
the literal length to `RegisteredOperationName::insert`), TypeID singleton
sentinel (per-op `&unk_NNNNNNN` where the registrar exposes a per-op slot,
range reference where the dialect uses a contiguous slab without per-op
isolation), one-clause semantic, primary wiki page. Sentinel addresses
use IDA-style `&unk_NNNNNNN` form, preserving the verbatim hexadecimal
address from `.bss`/`.data`. The mnemonic length column matches the
second argument passed to `sub_4461CA0` (the
`RegisteredOperationName::insert` callee). Where the glossary lists a
range without a per-op slot, the entry cites the full range.

## How the TypeID column is consumed

Every dispatcher in the binary reads `OperationName::TypeID` through one double-indirection from the `Operation` pointer:

```c
/* The OperationName slot sits at fixed offset +0x30 on an mlir::Operation,
 * and the TypeID pointer sits at +0x10 of OperationName::Impl. Both offsets
 * are stable across the binary; every dyn_cast / OpInterface lookup in
 * tileiras decompiles to this same shape. */
static inline const void *operation_typeid(const void *op) {
    const void *opname_impl = *(const void *const *)((const uint8_t *)op + 0x30);
    return *(const void *const *)((const uint8_t *)opname_impl + 0x10);
}

/* Dispatching on an op is therefore one pointer-equality test per arm
 * against a sentinel address from the table below. A reimplementer who
 * wants the same dispatch performance must publish exactly one stable
 * address per op kind for pointer-equality identity. */
static inline bool op_is(const void *op, const void *sentinel) {
    return operation_typeid(op) == sentinel;
}
```

Pointer-identity sentinels (the dominant form in the slab columns below) are
plain `.bss` slots; their *address* is the TypeID, no load of the byte is
ever made. Meyers-cached sentinels (the cute interface anchors) hold the
TypeID in a 64-bit qword that is filled in on first use through the
`mlir::TypeID::getFullName()` interner. For the full sentinel-form
breakdown and the address-band index, see
[TypeID Sentinel Table](typeid-sentinel-table.md).

## §1 cuda_tile.* (92 ops)

TypeID slab range `0x5785D0..0x57A8E0`. Per-op TypeID slots are in this
range but the registration thunk does not expose individual `&unk_*`
isolated addresses to the surface decompilation; entries are cited via
the range.

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `cuda_tile.absf` | 14 | range `0x5785D0..0x57A8E0` | element-wise abs on float tile | `dialects/cuda_tile.md` |
| `cuda_tile.absi` | 14 | range `0x5785D0..0x57A8E0` | element-wise abs on integer tile | `dialects/cuda_tile.md` |
| `cuda_tile.addf` | 14 | range `0x5785D0..0x57A8E0` | element-wise float add | `dialects/cuda_tile.md` |
| `cuda_tile.addi` | 14 | range `0x5785D0..0x57A8E0` | element-wise integer add | `dialects/cuda_tile.md` |
| `cuda_tile.andi` | 14 | range `0x5785D0..0x57A8E0` | bitwise AND | `dialects/cuda_tile.md` |
| `cuda_tile.assert` | 16 | range `0x5785D0..0x57A8E0` | runtime assertion in compiled tile code | `dialects/cuda_tile.md` |
| `cuda_tile.assume` | 16 | range `0x5785D0..0x57A8E0` | optimizer hint (LLVM `assume`) | `dialects/cuda_tile.md` |
| `cuda_tile.atomic_cas_tko` | 24 | range `0x5785D0..0x57A8E0` | atomic compare-and-swap, token-ordered | `dialects/cuda_tile.md` |
| `cuda_tile.atomic_rmw_tko` | 24 | range `0x5785D0..0x57A8E0` | atomic read-modify-write, token-ordered | `dialects/cuda_tile.md` |
| `cuda_tile.bitcast` | 17 | range `0x5785D0..0x57A8E0` | bit-pattern-preserving type pun | `dialects/cuda_tile.md` |
| `cuda_tile.break` | 15 | range `0x5785D0..0x57A8E0` | structured-loop break | `dialects/cuda_tile.md` |
| `cuda_tile.broadcast` | 19 | range `0x5785D0..0x57A8E0` | scalar / lower-rank to tile | `dialects/cuda_tile.md` |
| `cuda_tile.cat` | 13 | range `0x5785D0..0x57A8E0` | tile concatenation | `dialects/cuda_tile.md` |
| `cuda_tile.ceil` | 14 | range `0x5785D0..0x57A8E0` | ceil rounding | `dialects/cuda_tile.md` |
| `cuda_tile.cmpf` | 14 | range `0x5785D0..0x57A8E0` | float comparison | `dialects/cuda_tile.md` |
| `cuda_tile.cmpi` | 14 | range `0x5785D0..0x57A8E0` | integer comparison | `dialects/cuda_tile.md` |
| `cuda_tile.constant` | 18 | range `0x5785D0..0x57A8E0` | dense / splat constant | `dialects/cuda_tile.md` |
| `cuda_tile.continue` | 18 | range `0x5785D0..0x57A8E0` | structured-loop continue | `dialects/cuda_tile.md` |
| `cuda_tile.cos` | 13 | range `0x5785D0..0x57A8E0` | elementary cosine | `dialects/cuda_tile.md` |
| `cuda_tile.cosh` | 14 | range `0x5785D0..0x57A8E0` | hyperbolic cosine | `dialects/cuda_tile.md` |
| `cuda_tile.divf` | 14 | range `0x5785D0..0x57A8E0` | float division | `dialects/cuda_tile.md` |
| `cuda_tile.divi` | 14 | range `0x5785D0..0x57A8E0` | integer division | `dialects/cuda_tile.md` |
| `cuda_tile.entry` | 15 | range `0x5785D0..0x57A8E0` | kernel entry op (1 region) | `dialects/cuda_tile.md` |
| `cuda_tile.exp` | 13 | range `0x5785D0..0x57A8E0` | natural exponent | `dialects/cuda_tile.md` |
| `cuda_tile.exp2` | 14 | range `0x5785D0..0x57A8E0` | base-2 exponent | `dialects/cuda_tile.md` |
| `cuda_tile.exti` | 14 | range `0x5785D0..0x57A8E0` | integer extension | `dialects/cuda_tile.md` |
| `cuda_tile.extract` | 17 | range `0x5785D0..0x57A8E0` | tile element extract | `dialects/cuda_tile.md` |
| `cuda_tile.floor` | 15 | range `0x5785D0..0x57A8E0` | floor rounding | `dialects/cuda_tile.md` |
| `cuda_tile.fma` | 13 | range `0x5785D0..0x57A8E0` | fused multiply-add | `dialects/cuda_tile.md` |
| `cuda_tile.for` | 13 | range `0x5785D0..0x57A8E0` | structured for loop (1 region) | `dialects/cuda_tile.md` |
| `cuda_tile.ftof` | 14 | range `0x5785D0..0x57A8E0` | float-to-float cast | `dialects/cuda_tile.md` |
| `cuda_tile.ftoi` | 14 | range `0x5785D0..0x57A8E0` | float-to-int cast | `dialects/cuda_tile.md` |
| `cuda_tile.get_global` | 20 | range `0x5785D0..0x57A8E0` | reference module-level global | `dialects/cuda_tile.md` |
| `cuda_tile.get_index_space_shape` | 31 | range `0x5785D0..0x57A8E0` | shape of the launch index space | `dialects/cuda_tile.md` |
| `cuda_tile.get_num_tile_blocks` | 29 | range `0x5785D0..0x57A8E0` | tile-block count | `dialects/cuda_tile.md` |
| `cuda_tile.get_tensor_shape` | 26 | range `0x5785D0..0x57A8E0` | shape of a tensor view | `dialects/cuda_tile.md` |
| `cuda_tile.get_tile_block_id` | 27 | range `0x5785D0..0x57A8E0` | per-block id | `dialects/cuda_tile.md` |
| `cuda_tile.global` | 16 | range `0x5785D0..0x57A8E0` | module-level global declaration | `dialects/cuda_tile.md` |
| `cuda_tile.if` | 12 | range `0x5785D0..0x57A8E0` | structured conditional (2 regions) | `dialects/cuda_tile.md` |
| `cuda_tile.int_to_ptr` | 20 | range `0x5785D0..0x57A8E0` | integer-to-pointer cast | `dialects/cuda_tile.md` |
| `cuda_tile.iota` | 14 | range `0x5785D0..0x57A8E0` | sequential-int constant tile | `dialects/cuda_tile.md` |
| `cuda_tile.itof` | 14 | range `0x5785D0..0x57A8E0` | int-to-float cast | `dialects/cuda_tile.md` |
| `cuda_tile.join_tokens` | 21 | range `0x5785D0..0x57A8E0` | merge multiple tokens | `dialects/cuda_tile.md` |
| `cuda_tile.load_ptr_tko` | 22 | range `0x5785D0..0x57A8E0` | pointer load, token-ordered | `dialects/cuda_tile.md` |
| `cuda_tile.load_view_tko` | 23 | range `0x5785D0..0x57A8E0` | view load, token-ordered | `dialects/cuda_tile.md` |
| `cuda_tile.log` | 13 | range `0x5785D0..0x57A8E0` | natural log | `dialects/cuda_tile.md` |
| `cuda_tile.log2` | 14 | range `0x5785D0..0x57A8E0` | base-2 log | `dialects/cuda_tile.md` |
| `cuda_tile.loop` | 14 | range `0x5785D0..0x57A8E0` | generic structured loop (1 region) | `dialects/cuda_tile.md` |
| `cuda_tile.make_partition_view` | 29 | range `0x5785D0..0x57A8E0` | construct a `partition_view` | `dialects/cuda_tile.md` |
| `cuda_tile.make_tensor_view` | 26 | range `0x5785D0..0x57A8E0` | construct a `tensor_view` | `dialects/cuda_tile.md` |
| `cuda_tile.make_token` | 20 | range `0x5785D0..0x57A8E0` | mint a synchronisation token | `dialects/cuda_tile.md` |
| `cuda_tile.maxf` | 14 | range `0x5785D0..0x57A8E0` | float max | `dialects/cuda_tile.md` |
| `cuda_tile.maxi` | 14 | range `0x5785D0..0x57A8E0` | integer max | `dialects/cuda_tile.md` |
| `cuda_tile.minf` | 14 | range `0x5785D0..0x57A8E0` | float min | `dialects/cuda_tile.md` |
| `cuda_tile.mini` | 14 | range `0x5785D0..0x57A8E0` | integer min | `dialects/cuda_tile.md` |
| `cuda_tile.mmaf` | 14 | range `0x5785D0..0x57A8E0` | float tile MMA | `dialects/cuda_tile.md` |
| `cuda_tile.mmai` | 14 | range `0x5785D0..0x57A8E0` | integer tile MMA | `dialects/cuda_tile.md` |
| `cuda_tile.module` | 16 | range `0x5785D0..0x57A8E0` | top-level container (1 region) | `dialects/cuda_tile.md` |
| `cuda_tile.mulf` | 14 | range `0x5785D0..0x57A8E0` | float multiply | `dialects/cuda_tile.md` |
| `cuda_tile.mulhii` | 16 | range `0x5785D0..0x57A8E0` | high-half integer multiply | `dialects/cuda_tile.md` |
| `cuda_tile.muli` | 14 | range `0x5785D0..0x57A8E0` | integer multiply | `dialects/cuda_tile.md` |
| `cuda_tile.negf` | 14 | range `0x5785D0..0x57A8E0` | float negation | `dialects/cuda_tile.md` |
| `cuda_tile.negi` | 14 | range `0x5785D0..0x57A8E0` | integer negation | `dialects/cuda_tile.md` |
| `cuda_tile.offset` | 16 | range `0x5785D0..0x57A8E0` | view offset arithmetic | `dialects/cuda_tile.md` |
| `cuda_tile.ori` | 13 | range `0x5785D0..0x57A8E0` | bitwise OR | `dialects/cuda_tile.md` |
| `cuda_tile.permute` | 17 | range `0x5785D0..0x57A8E0` | tile permutation | `dialects/cuda_tile.md` |
| `cuda_tile.pow` | 13 | range `0x5785D0..0x57A8E0` | power | `dialects/cuda_tile.md` |
| `cuda_tile.print` | 15 | range `0x5785D0..0x57A8E0` | tile-aware diagnostic print (renamed from OSS `print_tko`) | `dialects/cuda_tile.md` |
| `cuda_tile.ptr_to_int` | 20 | range `0x5785D0..0x57A8E0` | pointer-to-integer cast | `dialects/cuda_tile.md` |
| `cuda_tile.ptr_to_ptr` | 20 | range `0x5785D0..0x57A8E0` | pointer recast | `dialects/cuda_tile.md` |
| `cuda_tile.reduce` | 16 | range `0x5785D0..0x57A8E0` | reduction (1 region) | `dialects/cuda_tile.md` |
| `cuda_tile.remf` | 14 | range `0x5785D0..0x57A8E0` | float remainder | `dialects/cuda_tile.md` |
| `cuda_tile.remi` | 14 | range `0x5785D0..0x57A8E0` | integer remainder | `dialects/cuda_tile.md` |
| `cuda_tile.reshape` | 17 | range `0x5785D0..0x57A8E0` | view reshape | `dialects/cuda_tile.md` |
| `cuda_tile.return` | 16 | range `0x5785D0..0x57A8E0` | terminator | `dialects/cuda_tile.md` |
| `cuda_tile.rsqrt` | 15 | range `0x5785D0..0x57A8E0` | reciprocal sqrt | `dialects/cuda_tile.md` |
| `cuda_tile.scan` | 14 | range `0x5785D0..0x57A8E0` | prefix-sum (1 region) | `dialects/cuda_tile.md` |
| `cuda_tile.select` | 16 | range `0x5785D0..0x57A8E0` | predicated select | `dialects/cuda_tile.md` |
| `cuda_tile.shli` | 14 | range `0x5785D0..0x57A8E0` | left shift | `dialects/cuda_tile.md` |
| `cuda_tile.shri` | 14 | range `0x5785D0..0x57A8E0` | right shift | `dialects/cuda_tile.md` |
| `cuda_tile.sin` | 13 | range `0x5785D0..0x57A8E0` | elementary sine | `dialects/cuda_tile.md` |
| `cuda_tile.sinh` | 14 | range `0x5785D0..0x57A8E0` | hyperbolic sine | `dialects/cuda_tile.md` |
| `cuda_tile.sqrt` | 14 | range `0x5785D0..0x57A8E0` | square root | `dialects/cuda_tile.md` |
| `cuda_tile.store_ptr_tko` | 23 | range `0x5785D0..0x57A8E0` | pointer store, token-ordered | `dialects/cuda_tile.md` |
| `cuda_tile.store_view_tko` | 24 | range `0x5785D0..0x57A8E0` | view store, token-ordered | `dialects/cuda_tile.md` |
| `cuda_tile.subf` | 14 | range `0x5785D0..0x57A8E0` | float subtract | `dialects/cuda_tile.md` |
| `cuda_tile.subi` | 14 | range `0x5785D0..0x57A8E0` | integer subtract | `dialects/cuda_tile.md` |
| `cuda_tile.tan` | 13 | range `0x5785D0..0x57A8E0` | elementary tangent | `dialects/cuda_tile.md` |
| `cuda_tile.tanh` | 14 | range `0x5785D0..0x57A8E0` | hyperbolic tangent | `dialects/cuda_tile.md` |
| `cuda_tile.trunci` | 16 | range `0x5785D0..0x57A8E0` | integer truncation | `dialects/cuda_tile.md` |
| `cuda_tile.xori` | 14 | range `0x5785D0..0x57A8E0` | bitwise XOR | `dialects/cuda_tile.md` |
| `cuda_tile.yield` | 15 | range `0x5785D0..0x57A8E0` | terminator for region-bearing ops | `dialects/cuda_tile.md` |

## §2 nv_tileaa.* (73 ops)

Per-op TypeID slots in dense range `0x5B46D28..0x5B46F68` (8-byte stride).
The slab anchors below the `nv_tileas` slab.

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nv_tileaa.addf` | 14 | range `0x5B46D28..0x5B46F68` | float add | `dialects/nv_tileaa.md` |
| `nv_tileaa.addptr` | 16 | range `0x5B46D28..0x5B46F68` | pointer + integer offset | `dialects/nv_tileaa.md` |
| `nv_tileaa.assert` | 16 | range `0x5B46D28..0x5B46F68` | runtime assertion | `dialects/nv_tileaa.md` |
| `nv_tileaa.assume` | 16 | range `0x5B46D28..0x5B46F68` | optimizer assumption | `dialects/nv_tileaa.md` |
| `nv_tileaa.atomic_cas` | 20 | range `0x5B46D28..0x5B46F68` | scalar atomic CAS | `dialects/nv_tileaa.md` |
| `nv_tileaa.atomic_rmw` | 20 | range `0x5B46D28..0x5B46F68` | scalar atomic RMW | `dialects/nv_tileaa.md` |
| `nv_tileaa.bitcast` | 17 | range `0x5B46D28..0x5B46F68` | bit-preserving type cast | `dialects/nv_tileaa.md` |
| `nv_tileaa.block_tile` | 20 | range `0x5B46D28..0x5B46F68` | per-CTA tile selection | `dialects/nv_tileaa.md` |
| `nv_tileaa.broadcast` | 19 | range `0x5B46D28..0x5B46F68` | scalar→tile / rank lift | `dialects/nv_tileaa.md` |
| `nv_tileaa.call` | 14 | range `0x5B46D28..0x5B46F68` | call into emitted device function | `dialects/nv_tileaa.md` |
| `nv_tileaa.call_elementwise_intrinsic` | 36 | range `0x5B46D28..0x5B46F68` | call libdevice math intrinsic | `dialects/nv_tileaa.md` |
| `nv_tileaa.cancel_next_program_id` | 32 | range `0x5B46D28..0x5B46F68` | cluster-launch-control cancel | `dialects/nv_tileaa.md` |
| `nv_tileaa.cat` | 13 | range `0x5B46D28..0x5B46F68` | tile concat | `dialects/nv_tileaa.md` |
| `nv_tileaa.clampf` | 16 | range `0x5B46D28..0x5B46F68` | float clamp | `dialects/nv_tileaa.md` |
| `nv_tileaa.conv_dot` | 18 | range `0x5B46D28..0x5B46F68` | convolution dot helper | `dialects/nv_tileaa.md` |
| `nv_tileaa.conv_tile` | 19 | range `0x5B46D28..0x5B46F68` | convolution tile helper | `dialects/nv_tileaa.md` |
| `nv_tileaa.create_mem_token` | 26 | range `0x5B46D28..0x5B46F68` | mint memory-lifetime token | `dialects/nv_tileaa.md` |
| `nv_tileaa.create_queue` | 22 | range `0x5B46D28..0x5B46F68` | construct typed queue | `dialects/nv_tileaa.md` |
| `nv_tileaa.divf` | 14 | range `0x5B46D28..0x5B46F68` | float divide | `dialects/nv_tileaa.md` |
| `nv_tileaa.dot` | 13 | range `0x5B46D28..0x5B46F68` | matrix dot | `dialects/nv_tileaa.md` |
| `nv_tileaa.elementwise_inline_asm` | 32 | range `0x5B46D28..0x5B46F68` | inline-PTX elementwise emitter | `dialects/nv_tileaa.md` |
| `nv_tileaa.execute` | 17 | range `0x5B46D28..0x5B46F68` | launch-time execute marker | `dialects/nv_tileaa.md` |
| `nv_tileaa.exp2` | 14 | range `0x5B46D28..0x5B46F68` | base-2 exponent | `dialects/nv_tileaa.md` |
| `nv_tileaa.expand_dims` | 21 | range `0x5B46D28..0x5B46F68` | rank lift | `dialects/nv_tileaa.md` |
| `nv_tileaa.extern_elementwise` | 28 | range `0x5B46D28..0x5B46F68` | external (libdevice) elementwise | `dialects/nv_tileaa.md` |
| `nv_tileaa.extract` | 17 | range `0x5B46D28..0x5B46F68` | scalar extract | `dialects/nv_tileaa.md` |
| `nv_tileaa.extract_slice` | 23 | range `0x5B46D28..0x5B46F68` | sub-slice extract | `dialects/nv_tileaa.md` |
| `nv_tileaa.fma` | 13 | range `0x5B46D28..0x5B46F68` | fused multiply-add | `dialects/nv_tileaa.md` |
| `nv_tileaa.fp_to_fp` | 18 | range `0x5B46D28..0x5B46F68` | float-to-float cast | `dialects/nv_tileaa.md` |
| `nv_tileaa.func` | 14 | range `0x5B46D28..0x5B46F68` | function op | `dialects/nv_tileaa.md` |
| `nv_tileaa.gather_load` | 21 | range `0x5B46D28..0x5B46F68` | indexed gather (global) | `dialects/nv_tileaa.md` |
| `nv_tileaa.generate` | 18 | range `0x5B46D28..0x5B46F68` | functional generate (region) | `dialects/nv_tileaa.md` |
| `nv_tileaa.get_dim_size` | 22 | range `0x5B46D28..0x5B46F68` | extract dimension size | `dialects/nv_tileaa.md` |
| `nv_tileaa.get_global` | 20 | range `0x5B46D28..0x5B46F68` | global lookup | `dialects/nv_tileaa.md` |
| `nv_tileaa.get_num_programs` | 26 | range `0x5B46D28..0x5B46F68` | grid intrinsic: program count | `dialects/nv_tileaa.md` |
| `nv_tileaa.get_program_id` | 24 | range `0x5B46D28..0x5B46F68` | grid intrinsic: program id | `dialects/nv_tileaa.md` |
| `nv_tileaa.global` | 16 | range `0x5B46D28..0x5B46F68` | module-level global | `dialects/nv_tileaa.md` |
| `nv_tileaa.histogram` | 19 | range `0x5B46D28..0x5B46F68` | parallel histogram primitive | `dialects/nv_tileaa.md` |
| `nv_tileaa.inject_ir` | 19 | range `0x5B46D28..0x5B46F68` | embed lowered IR fragment | `dialects/nv_tileaa.md` |
| `nv_tileaa.int_to_ptr` | 20 | range `0x5B46D28..0x5B46F68` | integer-to-pointer cast | `dialects/nv_tileaa.md` |
| `nv_tileaa.is_valid_program_id` | 29 | range `0x5B46D28..0x5B46F68` | grid intrinsic predicate | `dialects/nv_tileaa.md` |
| `nv_tileaa.join_mem_token` | 24 | range `0x5B46D28..0x5B46F68` | merge memory tokens | `dialects/nv_tileaa.md` |
| `nv_tileaa.launch_func` | 21 | range `0x5B46D28..0x5B46F68` | host-side launch op | `dialects/nv_tileaa.md` |
| `nv_tileaa.load` | 14 | range `0x5B46D28..0x5B46F68` | scalar memory load | `dialects/nv_tileaa.md` |
| `nv_tileaa.make_memref` | 21 | range `0x5B46D28..0x5B46F68` | construct memref | `dialects/nv_tileaa.md` |
| `nv_tileaa.make_range` | 20 | range `0x5B46D28..0x5B46F68` | iota-style range | `dialects/nv_tileaa.md` |
| `nv_tileaa.mark_for_reuse` | 24 | range `0x5B46D28..0x5B46F68` | lifetime-extension marker | `dialects/nv_tileaa.md` |
| `nv_tileaa.message` | 17 | range `0x5B46D28..0x5B46F68` | host-printable diagnostic | `dialects/nv_tileaa.md` |
| `nv_tileaa.mulf` | 14 | range `0x5B46D28..0x5B46F68` | float multiply | `dialects/nv_tileaa.md` |
| `nv_tileaa.mulhiui` | 17 | range `0x5B46D28..0x5B46F68` | unsigned high-half multiply | `dialects/nv_tileaa.md` |
| `nv_tileaa.optimization_barrier` | 30 | range `0x5B46D28..0x5B46F68` | optimizer barrier | `dialects/nv_tileaa.md` |
| `nv_tileaa.permute` | 17 | range `0x5B46D28..0x5B46F68` | tile permutation | `dialects/nv_tileaa.md` |
| `nv_tileaa.plugin` | 16 | range `0x5B46D28..0x5B46F68` | plugin-injection op | `dialects/nv_tileaa.md` |
| `nv_tileaa.pragma` | 16 | range `0x5B46D28..0x5B46F68` | pragma carrier | `dialects/nv_tileaa.md` |
| `nv_tileaa.print` | 15 | range `0x5B46D28..0x5B46F68` | tile-aware print | `dialects/nv_tileaa.md` |
| `nv_tileaa.ptr_to_int` | 20 | range `0x5B46D28..0x5B46F68` | pointer-to-integer cast | `dialects/nv_tileaa.md` |
| `nv_tileaa.queue.get` | 19 | range `0x5B46D28..0x5B46F68` | typed-queue dequeue | `dialects/nv_tileaa.md` |
| `nv_tileaa.queue.put` | 19 | range `0x5B46D28..0x5B46F68` | typed-queue enqueue | `dialects/nv_tileaa.md` |
| `nv_tileaa.queue.yield` | 21 | range `0x5B46D28..0x5B46F68` | typed-queue dataflow yield | `dialects/nv_tileaa.md` |
| `nv_tileaa.reduce` | 16 | range `0x5B46D28..0x5B46F68` | reduction | `dialects/nv_tileaa.md` |
| `nv_tileaa.return` | 16 | range `0x5B46D28..0x5B46F68` | function-return terminator | `dialects/nv_tileaa.md` |
| `nv_tileaa.rsqrt` | 15 | range `0x5B46D28..0x5B46F68` | reciprocal sqrt | `dialects/nv_tileaa.md` |
| `nv_tileaa.scan` | 14 | range `0x5B46D28..0x5B46F68` | prefix-sum | `dialects/nv_tileaa.md` |
| `nv_tileaa.scatter_store` | 23 | range `0x5B46D28..0x5B46F68` | indexed scatter (global) | `dialects/nv_tileaa.md` |
| `nv_tileaa.splat` | 15 | range `0x5B46D28..0x5B46F68` | scalar broadcast | `dialects/nv_tileaa.md` |
| `nv_tileaa.sqrt` | 14 | range `0x5B46D28..0x5B46F68` | square root | `dialects/nv_tileaa.md` |
| `nv_tileaa.store` | 15 | range `0x5B46D28..0x5B46F68` | scalar memory store | `dialects/nv_tileaa.md` |
| `nv_tileaa.subf` | 14 | range `0x5B46D28..0x5B46F68` | float subtract | `dialects/nv_tileaa.md` |
| `nv_tileaa.tiled_atomic_rmw` | 26 | range `0x5B46D28..0x5B46F68` | tile-wide RMW | `dialects/nv_tileaa.md` |
| `nv_tileaa.tiled_load` | 20 | range `0x5B46D28..0x5B46F68` | tile load | `dialects/nv_tileaa.md` |
| `nv_tileaa.tiled_store` | 21 | range `0x5B46D28..0x5B46F68` | tile store | `dialects/nv_tileaa.md` |
| `nv_tileaa.view` | 14 | range `0x5B46D28..0x5B46F68` | layout-aware view construction | `dialects/nv_tileaa.md` |
| `nv_tileaa.yield` | 15 | range `0x5B46D28..0x5B46F68` | region terminator | `dialects/nv_tileaa.md` |

Note: enumeration follows the registrar walk in p2-C01:441-513 and yields
72 mnemonics including the `queue.*` and `make_*` decompositions; the
"61 canonical ops" count cited in the dialect summary collapses
`make_memref` / `make_range` / `view` to their corresponding
`make_*` family count. All entries above are first-class.

## §3 nv_tileas.* (58 ops)

Anchor `&unk_5B44F08`. RTTI `nv_tile_ir::as`. `async.pipeline.*` cluster
dominates the surface area.

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nv_tileas.alloc_tensor` | 22 | anchor `&unk_5B44F08` | tensor buffer allocation | `dialects/nv_tileas.md` |
| `nv_tileas.async.cancel_next_program_id` | 38 | anchor `&unk_5B44F08` | async cluster cancel | `dialects/nv_tileas.md` |
| `nv_tileas.async.copy` | 20 | anchor `&unk_5B44F08` | DMA-async copy | `dialects/nv_tileas.md` |
| `nv_tileas.async.dot` | 19 | anchor `&unk_5B44F08` | async MMA | `dialects/nv_tileas.md` |
| `nv_tileas.async.extract_slice` | 29 | anchor `&unk_5B44F08` | async sub-slice extract | `dialects/nv_tileas.md` |
| `nv_tileas.async.future_wait` | 27 | anchor `&unk_5B44F08` | wait on async future | `dialects/nv_tileas.md` |
| `nv_tileas.async.gather_tma_load` | 31 | anchor `&unk_5B44F08` | TMA gather load | `dialects/nv_tileas.md` |
| `nv_tileas.async.insert_slice` | 28 | anchor `&unk_5B44F08` | async slice insert | `dialects/nv_tileas.md` |
| `nv_tileas.async.load` | 20 | anchor `&unk_5B44F08` | async load | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.agent_switch` | 37 | anchor `&unk_5B44F08` | warp-specialized agent boundary | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.consume_one` | 36 | anchor `&unk_5B44F08` | one-stage consume | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.consume_one_async` | 42 | anchor `&unk_5B44F08` | one-stage async consume | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.consumer_read` | 38 | anchor `&unk_5B44F08` | consumer protocol read | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.consumer_release` | 41 | anchor `&unk_5B44F08` | consumer protocol release | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.consumer_wait` | 38 | anchor `&unk_5B44F08` | consumer protocol wait | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.create_iterator` | 40 | anchor `&unk_5B44F08` | pipeline iterator construction | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.create_null_token` | 42 | anchor `&unk_5B44F08` | null-token constructor | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.create_pipeline` | 40 | anchor `&unk_5B44F08` | pipeline constructor | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.inc_iter` | 33 | anchor `&unk_5B44F08` | iterator advance | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.produce_one` | 36 | anchor `&unk_5B44F08` | one-stage produce | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.produce_one_async` | 42 | anchor `&unk_5B44F08` | one-stage async produce | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.producer_acquire` | 41 | anchor `&unk_5B44F08` | producer protocol acquire | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.producer_commit` | 40 | anchor `&unk_5B44F08` | producer protocol commit | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.producer_write` | 39 | anchor `&unk_5B44F08` | producer protocol write | `dialects/nv_tileas.md` |
| `nv_tileas.async.pipeline.yield` | 30 | anchor `&unk_5B44F08` | pipeline-region terminator | `dialects/nv_tileas.md` |
| `nv_tileas.async.scatter_tma_store` | 33 | anchor `&unk_5B44F08` | TMA scatter store | `dialects/nv_tileas.md` |
| `nv_tileas.async.store` | 21 | anchor `&unk_5B44F08` | async store | `dialects/nv_tileas.md` |
| `nv_tileas.async.tiled_atomic_rmw` | 32 | anchor `&unk_5B44F08` | tile RMW (async) | `dialects/nv_tileas.md` |
| `nv_tileas.async.tiled_load` | 26 | anchor `&unk_5B44F08` | async tiled load | `dialects/nv_tileas.md` |
| `nv_tileas.async.tiled_tma_load` | 30 | anchor `&unk_5B44F08` | TMA tile load | `dialects/nv_tileas.md` |
| `nv_tileas.async.tiled_tma_store` | 31 | anchor `&unk_5B44F08` | TMA tile store | `dialects/nv_tileas.md` |
| `nv_tileas.async.to_async` | 24 | anchor `&unk_5B44F08` | future conversion | `dialects/nv_tileas.md` |
| `nv_tileas.async.token_to_async` | 30 | anchor `&unk_5B44F08` | token-to-future conversion | `dialects/nv_tileas.md` |
| `nv_tileas.async.wait` | 20 | anchor `&unk_5B44F08` | async wait barrier | `dialects/nv_tileas.md` |
| `nv_tileas.cancel_next_program_id` | 32 | anchor `&unk_5B44F08` | cluster cancel | `dialects/nv_tileas.md` |
| `nv_tileas.convert_layout` | 24 | anchor `&unk_5B44F08` | layout conversion (smem ↔ rmem ↔ tmem) | `dialects/nv_tileas.md` |
| `nv_tileas.copy` | 14 | anchor `&unk_5B44F08` | sync copy | `dialects/nv_tileas.md` |
| `nv_tileas.create_none` | 21 | anchor `&unk_5B44F08` | null SSA value | `dialects/nv_tileas.md` |
| `nv_tileas.dot` | 13 | anchor `&unk_5B44F08` | sync matrix dot | `dialects/nv_tileas.md` |
| `nv_tileas.expand_dims` | 21 | anchor `&unk_5B44F08` | rank lift | `dialects/nv_tileas.md` |
| `nv_tileas.extract_slice` | 23 | anchor `&unk_5B44F08` | sub-slice extract | `dialects/nv_tileas.md` |
| `nv_tileas.gather_load` | 21 | anchor `&unk_5B44F08` | indexed gather | `dialects/nv_tileas.md` |
| `nv_tileas.generate` | 18 | anchor `&unk_5B44F08` | functional generate (region) | `dialects/nv_tileas.md` |
| `nv_tileas.insert_slice` | 22 | anchor `&unk_5B44F08` | slice insert | `dialects/nv_tileas.md` |
| `nv_tileas.load` | 14 | anchor `&unk_5B44F08` | scalar load | `dialects/nv_tileas.md` |
| `nv_tileas.make_tiled_tma_desc` | 29 | anchor `&unk_5B44F08` | TMA descriptor builder | `dialects/nv_tileas.md` |
| `nv_tileas.pragma` | 16 | anchor `&unk_5B44F08` | pragma carrier | `dialects/nv_tileas.md` |
| `nv_tileas.reduce` | 16 | anchor `&unk_5B44F08` | reduction | `dialects/nv_tileas.md` |
| `nv_tileas.reinterpret` | 21 | anchor `&unk_5B44F08` | reinterpret cast | `dialects/nv_tileas.md` |
| `nv_tileas.scan` | 14 | anchor `&unk_5B44F08` | prefix-sum | `dialects/nv_tileas.md` |
| `nv_tileas.scatter_store` | 23 | anchor `&unk_5B44F08` | indexed scatter | `dialects/nv_tileas.md` |
| `nv_tileas.shuffle` | 17 | anchor `&unk_5B44F08` | warp shuffle | `dialects/nv_tileas.md` |
| `nv_tileas.store` | 15 | anchor `&unk_5B44F08` | scalar store | `dialects/nv_tileas.md` |
| `nv_tileas.tiled_atomic_rmw` | 26 | anchor `&unk_5B44F08` | tile-wide RMW | `dialects/nv_tileas.md` |
| `nv_tileas.tiled_load` | 20 | anchor `&unk_5B44F08` | tile load | `dialects/nv_tileas.md` |
| `nv_tileas.tiled_store` | 21 | anchor `&unk_5B44F08` | tile store | `dialects/nv_tileas.md` |
| `nv_tileas.view` | 14 | anchor `&unk_5B44F08` | view op | `dialects/nv_tileas.md` |
| `nv_tileas.yield` | 15 | anchor `&unk_5B44F08` | region terminator | `dialects/nv_tileas.md` |

## §4 cute.* (59 ops)

Anchor `&unk_5B496B8`. Hardware-independent CuTe layout algebra.

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `cute.add_offset` | 15 | anchor `&unk_5B496B8` | offset addition into a layout/iter | `dialects/cute.md` |
| `cute.complement` | 15 | anchor `&unk_5B496B8` | layout complement | `dialects/cute.md` |
| `cute.copy` | 9 | anchor `&unk_5B496B8` | high-level copy | `dialects/cute.md` |
| `cute.copy_atom_call` | 19 | anchor `&unk_5B496B8` | apply copy atom | `dialects/cute.md` |
| `cute.cosize` | 11 | anchor `&unk_5B496B8` | layout cosize | `dialects/cute.md` |
| `cute.deref_desc_iter` | 20 | anchor `&unk_5B496B8` | dereference descriptor iter | `dialects/cute.md` |
| `cute.derefine` | 13 | anchor `&unk_5B496B8` | layout refinement | `dialects/cute.md` |
| `cute.fast_divmod.create_divisor` | 31 | anchor `&unk_5B496B8` | fast-divmod divisor ctor | `dialects/cute.md` |
| `cute.fast_divmod.divide` | 23 | anchor `&unk_5B496B8` | fast-divmod divide | `dialects/cute.md` |
| `cute.fast_divmod.get_divisor` | 28 | anchor `&unk_5B496B8` | fast-divmod accessor | `dialects/cute.md` |
| `cute.fast_divmod.make_divisor` | 29 | anchor `&unk_5B496B8` | fast-divmod factory | `dialects/cute.md` |
| `cute.filter_zeros` | 17 | anchor `&unk_5B496B8` | strip zero modes | `dialects/cute.md` |
| `cute.flat_divide` | 16 | anchor `&unk_5B496B8` | flat divide | `dialects/cute.md` |
| `cute.gemm` | 9 | anchor `&unk_5B496B8` | GEMM scheduling op | `dialects/cute.md` |
| `cute.get_iter` | 13 | anchor `&unk_5B496B8` | accessor: iter | `dialects/cute.md` |
| `cute.get_layout` | 15 | anchor `&unk_5B496B8` | accessor: layout | `dialects/cute.md` |
| `cute.get_layouts_from_tile` | 26 | anchor `&unk_5B496B8` | accessor: layouts from tile | `dialects/cute.md` |
| `cute.get_shape` | 14 | anchor `&unk_5B496B8` | accessor: shape | `dialects/cute.md` |
| `cute.get_stride` | 15 | anchor `&unk_5B496B8` | accessor: stride | `dialects/cute.md` |
| `cute.group_modes` | 16 | anchor `&unk_5B496B8` | layout shape op | `dialects/cute.md` |
| `cute.inttoptr` | 13 | anchor `&unk_5B496B8` | int-to-pointer | `dialects/cute.md` |
| `cute.local_partition` | 20 | anchor `&unk_5B496B8` | partition view | `dialects/cute.md` |
| `cute.local_tile` | 15 | anchor `&unk_5B496B8` | tile view | `dialects/cute.md` |
| `cute.logical_divide` | 19 | anchor `&unk_5B496B8` | logical divide | `dialects/cute.md` |
| `cute.make_atom` | 14 | anchor `&unk_5B496B8` | atom constructor | `dialects/cute.md` |
| `cute.make_desc_iter` | 19 | anchor `&unk_5B496B8` | descriptor-iter ctor | `dialects/cute.md` |
| `cute.make_fragment_like` | 23 | anchor `&unk_5B496B8` | fragment construction | `dialects/cute.md` |
| `cute.make_tiled_copy` | 20 | anchor `&unk_5B496B8` | tiled-copy constructor | `dialects/cute.md` |
| `cute.make_tiled_mma` | 19 | anchor `&unk_5B496B8` | tiled-MMA constructor | `dialects/cute.md` |
| `cute.make_tuple` | 15 | anchor `&unk_5B496B8` | tuple constructor | `dialects/cute.md` |
| `cute.make_view` | 14 | anchor `&unk_5B496B8` | view constructor | `dialects/cute.md` |
| `cute.memref.alloc_smem` | 22 | anchor `&unk_5B496B8` | smem allocation | `dialects/cute.md` |
| `cute.memref.alloca` | 18 | anchor `&unk_5B496B8` | stack alloca | `dialects/cute.md` |
| `cute.memref.load` | 16 | anchor `&unk_5B496B8` | memref load | `dialects/cute.md` |
| `cute.memref.store` | 17 | anchor `&unk_5B496B8` | memref store | `dialects/cute.md` |
| `cute.memref.store_vec` | 21 | anchor `&unk_5B496B8` | vector memref store | `dialects/cute.md` |
| `cute.mma_atom_call` | 18 | anchor `&unk_5B496B8` | apply MMA atom | `dialects/cute.md` |
| `cute.prefetch` | 13 | anchor `&unk_5B496B8` | prefetch | `dialects/cute.md` |
| `cute.prefetch_atom_call` | 23 | anchor `&unk_5B496B8` | apply prefetch atom | `dialects/cute.md` |
| `cute.print` | 10 | anchor `&unk_5B496B8` | diagnostic print | `dialects/cute.md` |
| `cute.print_tma_desc_im2col` | 26 | anchor `&unk_5B496B8` | print TMA im2col desc | `dialects/cute.md` |
| `cute.print_tma_desc_tiled` | 25 | anchor `&unk_5B496B8` | print TMA tiled desc | `dialects/cute.md` |
| `cute.ptr.store` | 14 | anchor `&unk_5B496B8` | typed pointer store | `dialects/cute.md` |
| `cute.ptrtoint` | 13 | anchor `&unk_5B496B8` | pointer-to-int | `dialects/cute.md` |
| `cute.recast_iter` | 16 | anchor `&unk_5B496B8` | recast iterator | `dialects/cute.md` |
| `cute.recast_layout` | 18 | anchor `&unk_5B496B8` | recast layout | `dialects/cute.md` |
| `cute.right_inverse` | 18 | anchor `&unk_5B496B8` | layout inverse | `dialects/cute.md` |
| `cute.select` | 11 | anchor `&unk_5B496B8` | layout selector | `dialects/cute.md` |
| `cute.size` | 9 | anchor `&unk_5B496B8` | layout size | `dialects/cute.md` |
| `cute.static` | 11 | anchor `&unk_5B496B8` | static-shape attr op | `dialects/cute.md` |
| `cute.stencil_divide` | 19 | anchor `&unk_5B496B8` | stencil divide | `dialects/cute.md` |
| `cute.tile_to_shape` | 18 | anchor `&unk_5B496B8` | tile materialisation | `dialects/cute.md` |
| `cute.tiled_divide` | 17 | anchor `&unk_5B496B8` | tiled divide | `dialects/cute.md` |
| `cute.tiled.copy.partition_D` | 27 | anchor `&unk_5B496B8` | tiled-copy D-partition | `dialects/cute.md` |
| `cute.tiled.copy.partition_S` | 27 | anchor `&unk_5B496B8` | tiled-copy S-partition | `dialects/cute.md` |
| `cute.tiled.copy.retile` | 22 | anchor `&unk_5B496B8` | tiled-copy retile | `dialects/cute.md` |
| `cute.tiled.mma.partition` | 24 | anchor `&unk_5B496B8` | tiled-MMA partition | `dialects/cute.md` |
| `cute.tiled.mma.partition_shape` | 30 | anchor `&unk_5B496B8` | tiled-MMA partition shape | `dialects/cute.md` |
| `cute.unpack_tuple` | 17 | anchor `&unk_5B496B8` | tuple unpacker | `dialects/cute.md` |

## §5 cute_nvgpu.* (73 ops)

TypeID slab range `0x5B47FF8..0x5B481A8` (54 slots, 8-byte stride);
remaining ops fall into per-op accessor singletons in same arena.
Anchor `&unk_5B482C8`.

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `cute_nvgpu.arch.alloc_rmem` | 26 | range `0x5B47FF8..0x5B481A8` | rmem allocation | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.alloc_smem` | 26 | range `0x5B47FF8..0x5B481A8` | smem allocation | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.SM100.copy_s2t` | 35 | range `0x5B47FF8..0x5B481A8` | smem→tmem copy (Blackwell) | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.SM100.tma_load` | 35 | range `0x5B47FF8..0x5B481A8` | TMA load (Blackwell) | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.SM100.tma_reduce` | 37 | range `0x5B47FF8..0x5B481A8` | TMA reduce (Blackwell) | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.SM100.tma_store` | 36 | range `0x5B47FF8..0x5B481A8` | TMA store (Blackwell) | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.SM100.tmem_load` | 36 | range `0x5B47FF8..0x5B481A8` | TMEM load | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.SM100.tmem_store` | 37 | range `0x5B47FF8..0x5B481A8` | TMEM store | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.SM80.cp_async` | 34 | range `0x5B47FF8..0x5B481A8` | Ampere `cp.async` | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.ldsm` | 25 | range `0x5B47FF8..0x5B481A8` | `ldmatrix` family | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.copy.stsm` | 25 | range `0x5B47FF8..0x5B481A8` | `stmatrix` family | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.get_dyn_smem` | 28 | range `0x5B47FF8..0x5B481A8` | dynamic-smem accessor | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.get_dyn_smem_size` | 33 | range `0x5B47FF8..0x5B481A8` | dynamic-smem size query | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.make_warp_uniform` | 33 | range `0x5B47FF8..0x5B481A8` | warp-uniform marker | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM100.umma` | 30 | range `0x5B47FF8..0x5B481A8` | Blackwell UMMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM100.umma_block_scaled` | 43 | range `0x5B47FF8..0x5B481A8` | Blackwell UMMA block-scaled | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM100.umma_block_scaled_sparse` | 50 | range `0x5B47FF8..0x5B481A8` | Blackwell UMMA bs sparse | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM100.umma_sparse` | 37 | range `0x5B47FF8..0x5B481A8` | Blackwell UMMA sparse | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM120.block_scaled` | 38 | range `0x5B47FF8..0x5B481A8` | sm_120 block-scaled MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM80` | 24 | range `0x5B47FF8..0x5B481A8` | Ampere MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM80.sparse` | 31 | range `0x5B47FF8..0x5B481A8` | Ampere MMA sparse | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM89` | 24 | range `0x5B47FF8..0x5B481A8` | Ada MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.mma.SM90` | 24 | range `0x5B47FF8..0x5B481A8` | Hopper WGMMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.prefetch_tma_desc` | 33 | range `0x5B47FF8..0x5B481A8` | TMA desc prefetch | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.sm100.alloc_tmem` | 32 | range `0x5B47FF8..0x5B481A8` | TMEM alloc | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.sm100.dealloc_tmem` | 34 | range `0x5B47FF8..0x5B481A8` | TMEM dealloc | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.sm100.relinquish_tmem_alloc_permit` | 50 | range `0x5B47FF8..0x5B481A8` | TMEM permit release | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.arch.sm100.retrieve_tmem_ptr` | 39 | range `0x5B47FF8..0x5B481A8` | TMEM pointer retrieval | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.get_copy_s2t_smem_desc_view` | 43 | range `0x5B47FF8..0x5B481A8` | atom accessor: s2t smem-desc | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.get_value` | 25 | range `0x5B47FF8..0x5B481A8` | atom value accessor | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.ldsm` | 20 | range `0x5B47FF8..0x5B481A8` | `ldmatrix` atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.make_exec_tma` | 29 | range `0x5B47FF8..0x5B481A8` | executable TMA atom builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.make_non_exec_tiled_tma_load` | 44 | range `0x5B47FF8..0x5B481A8` | non-exec tiled TMA load builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.make_non_exec_tiled_tma_reduce` | 46 | range `0x5B47FF8..0x5B481A8` | non-exec tiled TMA reduce builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.make_s2t_copy` | 29 | range `0x5B47FF8..0x5B481A8` | s2t copy atom builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.make_tma_load` | 29 | range `0x5B47FF8..0x5B481A8` | TMA load atom builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.make_tma_reduce` | 31 | range `0x5B47FF8..0x5B481A8` | TMA reduce atom builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.make_tma_store` | 30 | range `0x5B47FF8..0x5B481A8` | TMA store atom builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.make_tmem_copy` | 30 | range `0x5B47FF8..0x5B481A8` | TMEM copy atom builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.non_exec_tiled_tma_load` | 39 | range `0x5B47FF8..0x5B481A8` | non-exec tiled TMA load atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.non_exec_tiled_tma_reduce` | 41 | range `0x5B47FF8..0x5B481A8` | non-exec tiled TMA reduce atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.non_exec_tiled_tma_store` | 40 | range `0x5B47FF8..0x5B481A8` | non-exec tiled TMA store atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.s2t_copy` | 24 | range `0x5B47FF8..0x5B481A8` | s2t copy atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.simt_async_copy` | 31 | range `0x5B47FF8..0x5B481A8` | SIMT async copy atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.stsm` | 20 | range `0x5B47FF8..0x5B481A8` | `stmatrix` atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.tma_load` | 24 | range `0x5B47FF8..0x5B481A8` | TMA load atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.tma_reduce` | 26 | range `0x5B47FF8..0x5B481A8` | TMA reduce atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.tma_store` | 25 | range `0x5B47FF8..0x5B481A8` | TMA store atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.tmem_load` | 25 | range `0x5B47FF8..0x5B481A8` | TMEM load atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.tmem_store` | 26 | range `0x5B47FF8..0x5B481A8` | TMEM store atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.universal_copy` | 30 | range `0x5B47FF8..0x5B481A8` | universal copy atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.atom.universal_fma` | 29 | range `0x5B47FF8..0x5B481A8` | universal FMA atom | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.cast_tma_desc_to_integer` | 35 | range `0x5B47FF8..0x5B481A8` | TMA desc-to-int reinterpret | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.copy_tma_desc` | 24 | range `0x5B47FF8..0x5B481A8` | TMA desc copy | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.get_grid_constant_pointer` | 36 | range `0x5B47FF8..0x5B481A8` | nvvm.grid_constant accessor | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.get_tma_desc_addr` | 28 | range `0x5B47FF8..0x5B481A8` | TMA desc-address probe | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.make_sm120_mma_bs` | 28 | range `0x5B47FF8..0x5B481A8` | sm_120 block-scaled MMA constructor | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.make_tma_desc_im2col` | 31 | range `0x5B47FF8..0x5B481A8` | TMA im2col desc builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.make_tma_desc_im2col_at` | 34 | range `0x5B47FF8..0x5B481A8` | TMA im2col desc builder (at) | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.make_tma_desc_tiled` | 30 | range `0x5B47FF8..0x5B481A8` | TMA tiled desc builder | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.make_tma_desc_tiled_at` | 33 | range `0x5B47FF8..0x5B481A8` | TMA tiled desc builder (at) | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.prefetch_tma_desc` | 28 | range `0x5B47FF8..0x5B481A8` | TMA desc prefetch | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.sm100.mma` | 20 | range `0x5B47FF8..0x5B481A8` | Blackwell MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.sm100.mma_bs` | 23 | range `0x5B47FF8..0x5B481A8` | Blackwell block-scaled MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.sm100.mma_bs_sp` | 26 | range `0x5B47FF8..0x5B481A8` | Blackwell block-scaled sparse MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.sm100.mma_sp` | 23 | range `0x5B47FF8..0x5B481A8` | Blackwell sparse MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.SM120.mma_bs` | 23 | range `0x5B47FF8..0x5B481A8` | sm_120 block-scaled MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.sm80.mma` | 19 | range `0x5B47FF8..0x5B481A8` | Ampere MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.sm80.sparse_mma` | 26 | range `0x5B47FF8..0x5B481A8` | Ampere sparse MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.sm89.mma` | 19 | range `0x5B47FF8..0x5B481A8` | Ada MMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.sm90.mma` | 19 | range `0x5B47FF8..0x5B481A8` | Hopper WGMMA | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.smem_desc_view` | 25 | range `0x5B47FF8..0x5B481A8` | smem descriptor view | `dialects/cute_nvgpu.md` |
| `cute_nvgpu.update_tma_desc` | 26 | range `0x5B47FF8..0x5B481A8` | TMA desc mutate | `dialects/cute_nvgpu.md` |

## §6 cutlass.* (84 ops, 38 unique families)

Fold-record range `0x5B47490..0x5B476A0` covers the op-info blocks.
Includes block_striped collectives, generic and named barriers, the
pipeline state machine, the seq_bar protocol, and the tile_scheduler
family (DP, static-persistent, StreamK, MODS-trace).

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `cutlass.async.exec` | 18 | range `0x5B47490..0x5B476A0` | async-execute wrapper | `dialects/cutlass.md` |
| `cutlass.barrier_id` | 18 | range `0x5B47490..0x5B476A0` | barrier-id allocator | `dialects/cutlass.md` |
| `cutlass.block_striped.load` | 26 | range `0x5B47490..0x5B476A0` | block-striped load | `dialects/cutlass.md` |
| `cutlass.block_striped.load_add` | 30 | range `0x5B47490..0x5B476A0` | block-striped load+add | `dialects/cutlass.md` |
| `cutlass.block_striped.reduce` | 28 | range `0x5B47490..0x5B476A0` | block-striped reduce | `dialects/cutlass.md` |
| `cutlass.block_striped.store` | 27 | range `0x5B47490..0x5B476A0` | block-striped store | `dialects/cutlass.md` |
| `cutlass.generic_barrier.arrive_increment` | 40 | range `0x5B47490..0x5B476A0` | generic-barrier arrive-increment | `dialects/cutlass.md` |
| `cutlass.generic_barrier_sync` | 28 | range `0x5B47490..0x5B476A0` | generic-barrier sync | `dialects/cutlass.md` |
| `cutlass.generic_barrier.wait_eq` | 31 | range `0x5B47490..0x5B476A0` | generic-barrier wait-eq | `dialects/cutlass.md` |
| `cutlass.generic_barrier.wait_less_than` | 38 | range `0x5B47490..0x5B476A0` | generic-barrier wait-less-than | `dialects/cutlass.md` |
| `cutlass.named_barrier.arrive` | 28 | range `0x5B47490..0x5B476A0` | named-barrier arrive | `dialects/cutlass.md` |
| `cutlass.named_barrier.arrive_and_wait` | 37 | range `0x5B47490..0x5B476A0` | named-barrier arrive+wait | `dialects/cutlass.md` |
| `cutlass.pipeline.consume` | 24 | range `0x5B47490..0x5B476A0` | pipeline consume | `dialects/cutlass.md` |
| `cutlass.pipeline.consumer_release` | 33 | range `0x5B47490..0x5B476A0` | consumer release | `dialects/cutlass.md` |
| `cutlass.pipeline.consumer_try_wait` | 34 | range `0x5B47490..0x5B476A0` | consumer try-wait | `dialects/cutlass.md` |
| `cutlass.pipeline.consumer_wait` | 30 | range `0x5B47490..0x5B476A0` | consumer wait | `dialects/cutlass.md` |
| `cutlass.pipeline.create` | 23 | range `0x5B47490..0x5B476A0` | pipeline ctor | `dialects/cutlass.md` |
| `cutlass.pipeline.get_producer_barrier` | 37 | range `0x5B47490..0x5B476A0` | producer-barrier query | `dialects/cutlass.md` |
| `cutlass.pipeline.get_producer_mask` | 34 | range `0x5B47490..0x5B476A0` | producer-mask query | `dialects/cutlass.md` |
| `cutlass.pipeline.init` | 21 | range `0x5B47490..0x5B476A0` | pipeline init | `dialects/cutlass.md` |
| `cutlass.pipeline.make_participants` | 34 | range `0x5B47490..0x5B476A0` | participant set construction | `dialects/cutlass.md` |
| `cutlass.pipeline.produce` | 24 | range `0x5B47490..0x5B476A0` | pipeline produce | `dialects/cutlass.md` |
| `cutlass.pipeline.producer_acquire` | 33 | range `0x5B47490..0x5B476A0` | producer acquire | `dialects/cutlass.md` |
| `cutlass.pipeline.producer_commit` | 32 | range `0x5B47490..0x5B476A0` | producer commit | `dialects/cutlass.md` |
| `cutlass.pipeline.producer_tail` | 30 | range `0x5B47490..0x5B476A0` | producer tail | `dialects/cutlass.md` |
| `cutlass.pipeline.producer_try_acquire` | 37 | range `0x5B47490..0x5B476A0` | producer try-acquire | `dialects/cutlass.md` |
| `cutlass.pipeline.state.create` | 29 | range `0x5B47490..0x5B476A0` | state ctor | `dialects/cutlass.md` |
| `cutlass.pipeline.state.get_count` | 32 | range `0x5B47490..0x5B476A0` | state count accessor | `dialects/cutlass.md` |
| `cutlass.pipeline.state.get_index` | 32 | range `0x5B47490..0x5B476A0` | state index accessor | `dialects/cutlass.md` |
| `cutlass.pipeline.state.get_phase` | 32 | range `0x5B47490..0x5B476A0` | state phase accessor | `dialects/cutlass.md` |
| `cutlass.pipeline.state.increment` | 32 | range `0x5B47490..0x5B476A0` | state increment | `dialects/cutlass.md` |
| `cutlass.pipeline.switch_by_executor` | 35 | range `0x5B47490..0x5B476A0` | executor-keyed dispatch | `dialects/cutlass.md` |
| `cutlass.seq_bar.arrive` | 22 | range `0x5B47490..0x5B476A0` | seq-bar arrive | `dialects/cutlass.md` |
| `cutlass.seq_bar.create` | 22 | range `0x5B47490..0x5B476A0` | seq-bar ctor | `dialects/cutlass.md` |
| `cutlass.seq_bar.init` | 20 | range `0x5B47490..0x5B476A0` | seq-bar init | `dialects/cutlass.md` |
| `cutlass.seq_bar.state.create` | 28 | range `0x5B47490..0x5B476A0` | seq-bar state ctor | `dialects/cutlass.md` |
| `cutlass.seq_bar.wait` | 20 | range `0x5B47490..0x5B476A0` | seq-bar wait | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.advance_to_next_work` | 43 | range `0x5B47490..0x5B476A0` | scheduler advance | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.compute_epilogue` | 39 | range `0x5B47490..0x5B476A0` | epilogue trigger | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.create_dp_params` | 39 | range `0x5B47490..0x5B476A0` | DP scheduler params ctor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.create_dp_work_tile_info` | 47 | range `0x5B47490..0x5B476A0` | DP work-tile-info ctor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.create_SM100_scheduler` | 45 | range `0x5B47490..0x5B476A0` | sm_100 scheduler factory | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.create_static_persistent_params` | 54 | range `0x5B47490..0x5B476A0` | static-persistent params ctor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.create_static_persistent_work_tile_info` | 62 | range `0x5B47490..0x5B476A0` | static-persistent work-tile-info ctor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.create_streamk_params` | 44 | range `0x5B47490..0x5B476A0` | StreamK params ctor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.create_streamk_work_tile_info` | 52 | range `0x5B47490..0x5B476A0` | StreamK work-tile-info ctor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.fetch_next_work` | 38 | range `0x5B47490..0x5B476A0` | fetch next work | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.fixup` | 28 | range `0x5B47490..0x5B476A0` | partial-tile fixup | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.fixup_increment` | 38 | range `0x5B47490..0x5B476A0` | fixup increment | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.fixup_wait` | 33 | range `0x5B47490..0x5B476A0` | fixup wait | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.get_current_work` | 39 | range `0x5B47490..0x5B476A0` | current work accessor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.get_grid_shape` | 37 | range `0x5B47490..0x5B476A0` | grid-shape accessor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.get_workid_response_ptr` | 46 | range `0x5B47490..0x5B476A0` | workid response ptr | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.get_work_k_tile_count` | 44 | range `0x5B47490..0x5B476A0` | work k-tile count | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.get_work_k_tile_start` | 44 | range `0x5B47490..0x5B476A0` | work k-tile start | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.get_workspace_sizes` | 42 | range `0x5B47490..0x5B476A0` | workspace sizes | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.initial_work_tile_info` | 45 | range `0x5B47490..0x5B476A0` | initial work-tile info | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.initialize_workspace` | 43 | range `0x5B47490..0x5B476A0` | initialize workspace | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.make_dp_params` | 37 | range `0x5B47490..0x5B476A0` | DP params builder | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.make_static_persistent_params` | 52 | range `0x5B47490..0x5B476A0` | static-persistent params builder | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.make_streamk_params` | 42 | range `0x5B47490..0x5B476A0` | StreamK params builder | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.mods_report_mainloop_end` | 47 | range `0x5B47490..0x5B476A0` | MODS-trace mainloop end | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.mods_report_mainloop_start` | 49 | range `0x5B47490..0x5B476A0` | MODS-trace mainloop start | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.mods_report_smid` | 39 | range `0x5B47490..0x5B476A0` | MODS-trace smid report | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.mods_throttle` | 36 | range `0x5B47490..0x5B476A0` | MODS-trace throttle | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.params_get_value` | 39 | range `0x5B47490..0x5B476A0` | params accessor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.query_next_work` | 38 | range `0x5B47490..0x5B476A0` | query next work | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.static_fetch_next_work` | 45 | range `0x5B47490..0x5B476A0` | static fetch next work | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.work_tile_info_get_value` | 47 | range `0x5B47490..0x5B476A0` | work-tile-info accessor | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.work_tile_info_set_value` | 47 | range `0x5B47490..0x5B476A0` | work-tile-info mutator | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.work_tile_info_to_coord_mnkl` | 51 | range `0x5B47490..0x5B476A0` | work-tile-info MNKL coords | `dialects/cutlass.md` |
| `cutlass.tile_scheduler.work_tile_info_to_cta_coord` | 50 | range `0x5B47490..0x5B476A0` | work-tile-info CTA coords | `dialects/cutlass.md` |

## §7 mlir::nvgpu.* (upstream, observed in lowerings)

Upstream MLIR `nvgpu` dialect; statically linked into tileiras. Dialect
TypeID anchor is provided by the upstream registration; per-op TypeIDs
are not exposed by tileiras's own registrar. The list below enumerates
every upstream `nvgpu.*` mnemonic observed in tileiras-driven lowerings
(produced by `convert-nvgpu-to-nvvm` consumers and equivalent upstream
dialects).

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvgpu.device_async_copy` | 23 | upstream | device-async copy | `dialects/upstream-nvgpu.md` |
| `nvgpu.device_async_create_group` | 31 | upstream | device-async group ctor | `dialects/upstream-nvgpu.md` |
| `nvgpu.device_async_wait` | 23 | upstream | device-async wait | `dialects/upstream-nvgpu.md` |
| `nvgpu.ldmatrix` | 14 | upstream | ldmatrix wrapper | `dialects/upstream-nvgpu.md` |
| `nvgpu.mma.sp.sync` | 17 | upstream | sparse MMA sync | `dialects/upstream-nvgpu.md` |
| `nvgpu.mma.sync` | 14 | upstream | dense MMA sync | `dialects/upstream-nvgpu.md` |
| `nvgpu.tma.async.load` | 20 | upstream | TMA async load | `dialects/upstream-nvgpu.md` |
| `nvgpu.tma.async.store` | 21 | upstream | TMA async store | `dialects/upstream-nvgpu.md` |
| `nvgpu.tma.create.descriptor` | 27 | upstream | TMA descriptor ctor | `dialects/upstream-nvgpu.md` |
| `nvgpu.warpgroup.generate.descriptor` | 35 | upstream | warpgroup descriptor ctor | `dialects/upstream-nvgpu.md` |
| `nvgpu.warpgroup.mma` | 19 | upstream | warpgroup MMA | `dialects/upstream-nvgpu.md` |
| `nvgpu.warpgroup.mma.init.accumulator` | 36 | upstream | warpgroup MMA acc init | `dialects/upstream-nvgpu.md` |

## §8 NVVM.* (213 ops)

TypeID slab `0x5B8D610..0x5B8DCB8` (1704 bytes / 8 = 213 entries, 8-byte
stride, dense). Dialect TypeID `&unk_5B8DCC0` sits 8 bytes above the
highest op slot. Walked via `RegisteredOperationName::insert` at
`sub_4461CA0` from the registrar driver `sub_2EFC390`. Order below is
the categorical roster from p5-HH01 (within each category alphabetical
where the registrar permits it; otherwise registrar walk order).

### §8.1 Barriers (10)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.barrier` | 0xC | `&unk_5B8DC80` | block-level barrier | `dialects/nvvm.md` |
| `nvvm.barrier0` | 0xD | `&unk_5B8DCA8` | legacy `bar.sync 0` | `dialects/nvvm.md` |
| `nvvm.barrier.arrive` | 0x13 | `&unk_5B8DCA0` | barrier arrive | `dialects/nvvm.md` |
| `nvvm.barrier.cta.arrive` | 0x17 | `&unk_5B8DC98` | CTA barrier arrive | `dialects/nvvm.md` |
| `nvvm.barrier.cta.red` | 0x14 | `&unk_5B8DC90` | CTA barrier reduction | `dialects/nvvm.md` |
| `nvvm.barrier.cta.sync` | 0x15 | `&unk_5B8DC88` | CTA barrier sync | `dialects/nvvm.md` |
| `nvvm.bar.warp.sync` | 0x12 | `&unk_5B8D758` | `bar.warp.sync` | `dialects/nvvm.md` |
| `nvvm.cluster.arrive` | 0x13 | `&unk_5B8DC10` | cluster arrive | `dialects/nvvm.md` |
| `nvvm.cluster.arrive.relaxed` | 0x1B | `&unk_5B8DC08` | cluster arrive relaxed | `dialects/nvvm.md` |
| `nvvm.cluster.wait` | 0x11 | `&unk_5B8DB70` | cluster wait | `dialects/nvvm.md` |

### §8.2 mbarrier (20)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.mbarrier.arrive` | 0x14 | `&unk_5B8D870` | mbarrier arrive | `dialects/nvvm.md` |
| `nvvm.mbarrier.arrive.expect_tx` | 0x1E | `&unk_5B8D890` | arrive with tx-count expectation | `dialects/nvvm.md` |
| `nvvm.mbarrier.arrive.expect_tx.shared` | 0x25 | `&unk_5B8D888` | arrive expect_tx (shared) | `dialects/nvvm.md` |
| `nvvm.mbarrier.arrive.nocomplete` | 0x1F | `&unk_5B8D880` | arrive nocomplete | `dialects/nvvm.md` |
| `nvvm.mbarrier.arrive.nocomplete.shared` | 0x26 | `&unk_5B8D878` | arrive nocomplete (shared) | `dialects/nvvm.md` |
| `nvvm.mbarrier.arrive.shared` | 0x1B | `&unk_5B8D868` | arrive (shared) | `dialects/nvvm.md` |
| `nvvm.mbarrier.init` | 0x12 | `&unk_5B8D860` | mbarrier init | `dialects/nvvm.md` |
| `nvvm.mbarrier.init.shared` | 0x19 | `&unk_5B8D858` | mbarrier init (shared) | `dialects/nvvm.md` |
| `nvvm.mbarrier.inval` | 0x13 | `&unk_5B8D850` | mbarrier invalidate | `dialects/nvvm.md` |
| `nvvm.mbarrier.inval.shared` | 0x1A | `&unk_5B8D848` | mbarrier invalidate (shared) | `dialects/nvvm.md` |
| `nvvm.mbarrier.test.wait` | 0x17 | `&unk_5B8D840` | mbarrier test-wait | `dialects/nvvm.md` |
| `nvvm.mbarrier.test.wait.shared` | 0x1E | `&unk_5B8D838` | mbarrier test-wait (shared) | `dialects/nvvm.md` |
| `nvvm.mbarrier.try_wait.parity` | 0x1D | `&unk_5B8D820` | try-wait parity | `dialects/nvvm.md` |
| `nvvm.mbarrier.try_wait.parity.shared` | 0x24 | `&unk_5B8D818` | try-wait parity (shared) | `dialects/nvvm.md` |
| `nvvm.mbarrier.try_wait.parity.timelimit` | 0x27 | `&unk_5B8D810` | try-wait parity timelimit | `dialects/nvvm.md` |
| `nvvm.mbarrier.try_wait.timelimit` | 0x20 | `&unk_5B8D808` | try-wait timelimit | `dialects/nvvm.md` |
| `nvvm.mbarrier.txn` | 0x11 | `&unk_5B8D828` | mbarrier transaction count | `dialects/nvvm.md` |
| `nvvm.mbarrier.txn.cta` | 0x15 | `&unk_5B8D830` | mbarrier transaction (CTA) | `dialects/nvvm.md` |
| `nvvm.mbarrier.wait` | 0x12 | `&unk_5B8D800` | mbarrier wait | `dialects/nvvm.md` |
| `nvvm.mbarrier.wait.parity` | 0x19 | `&unk_5B8D7F8` | mbarrier wait parity | `dialects/nvvm.md` |

### §8.3 TMA / cp.async.bulk (12)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.cp.async.bulk.commit.group` | 0x1F | `&unk_5B8DB20` | bulk commit group | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.global.shared.cta` | 0x24 | `&unk_5B8DB08` | bulk global←shared.cta | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.prefetch` | 0x1B | `&unk_5B8DB10` | bulk prefetch | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.shared.cluster.global` | 0x28 | `&unk_5B8DB18` | bulk shared.cluster←global | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.shared.cluster.shared.cta` | 0x2C | `&unk_5B8DB00` | bulk shared.cluster←shared.cta | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.tensor.global.shared.cta` | 0x2B | `&unk_5B8DAD0` | TMA tensor global←shared.cta | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.tensor.global.shared.cta.ext` | 0x2F | `&unk_5B8DAD8` | TMA tensor global←shared.cta ext | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.tensor.prefetch` | 0x22 | `&unk_5B8DAE8` | TMA tensor prefetch | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.tensor.reduce` | 0x20 | `&unk_5B8DAE0` | TMA tensor reduce | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.tensor.shared.cluster.global` | 0x2F | `&unk_5B8DAF0` | TMA tensor shared.cluster←global | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.tensor.shared.cta.global` | 0x2B | `&unk_5B8DAF8` | TMA tensor shared.cta←global | `dialects/nvvm.md` |
| `nvvm.cp.async.bulk.wait_group` | 0x1D | `&unk_5B8DAC8` | bulk wait group | `dialects/nvvm.md` |

### §8.4 cp.async (Ampere) (5)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.cp.async.commit.group` | 0x1A | `&unk_5B8DAC0` | cp.async commit group | `dialects/nvvm.md` |
| `nvvm.cp.async.mbarrier.arrive` | 0x1D | `&unk_5B8DAB8` | cp.async mbarrier arrive | `dialects/nvvm.md` |
| `nvvm.cp.async.mbarrier.arrive.shared` | 0x24 | `&unk_5B8DAB0` | cp.async mbarrier arrive (shared) | `dialects/nvvm.md` |
| `nvvm.cp.async.shared.global` | 0x1B | `&unk_5B8DAA8` | cp.async shared←global | `dialects/nvvm.md` |
| `nvvm.cp.async.wait.group` | 0x18 | `&unk_5B8DAA0` | cp.async wait group | `dialects/nvvm.md` |

### §8.5 tcgen05 (Blackwell) (18)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.tcgen05.alloc` | 0x12 | `&unk_5B8D750` | tcgen05 alloc | `dialects/nvvm.md` |
| `nvvm.tcgen05.commit` | 0x13 | `&unk_5B8D740` | tcgen05 commit | `dialects/nvvm.md` |
| `nvvm.tcgen05.commit.arrive` | 0x1A | `&unk_5B8D748` | tcgen05 commit-arrive | `dialects/nvvm.md` |
| `nvvm.tcgen05.cp` | 0xF | `&unk_5B8D738` | tcgen05 copy | `dialects/nvvm.md` |
| `nvvm.tcgen05.dealloc` | 0x14 | `&unk_5B8D730` | tcgen05 dealloc | `dialects/nvvm.md` |
| `nvvm.tcgen05.fence` | 0x12 | `&unk_5B8D728` | tcgen05 fence | `dialects/nvvm.md` |
| `nvvm.tcgen05.ld` | 0xF | `&unk_5B8D720` | tcgen05 load | `dialects/nvvm.md` |
| `nvvm.tcgen05.mma` | 0x10 | `&unk_5B8D710` | tcgen05 MMA | `dialects/nvvm.md` |
| `nvvm.tcgen05.mma.block_scale` | 0x1C | `&unk_5B8D718` | tcgen05 MMA block-scale | `dialects/nvvm.md` |
| `nvvm.tcgen05.mma_smem_desc` | 0x1A | `&unk_5B8D6E8` | tcgen05 mma smem desc | `dialects/nvvm.md` |
| `nvvm.tcgen05.mma.sp` | 0x13 | `&unk_5B8D700` | tcgen05 MMA sparse | `dialects/nvvm.md` |
| `nvvm.tcgen05.mma.sp.block_scale` | 0x1F | `&unk_5B8D708` | tcgen05 MMA sparse block-scale | `dialects/nvvm.md` |
| `nvvm.tcgen05.mma.ws` | 0x13 | `&unk_5B8D6F8` | tcgen05 MMA warp-spec | `dialects/nvvm.md` |
| `nvvm.tcgen05.mma.ws.sp` | 0x16 | `&unk_5B8D6F0` | tcgen05 MMA ws sparse | `dialects/nvvm.md` |
| `nvvm.tcgen05.relinquish_alloc_permit` | 0x24 | `&unk_5B8D6E0` | tcgen05 relinquish permit | `dialects/nvvm.md` |
| `nvvm.tcgen05.shift` | 0x12 | `&unk_5B8D6D8` | tcgen05 shift | `dialects/nvvm.md` |
| `nvvm.tcgen05.st` | 0xF | `&unk_5B8D6D0` | tcgen05 store | `dialects/nvvm.md` |
| `nvvm.tcgen05.wait` | 0x11 | `&unk_5B8D6C8` | tcgen05 wait | `dialects/nvvm.md` |

### §8.6 wgmma / wmma / mma / ldmatrix-stmatrix (12)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.wgmma.commit.group.sync.aligned` | 0x24 | `&unk_5B8D620` | wgmma commit group sync | `dialects/nvvm.md` |
| `nvvm.wgmma.fence.aligned` | 0x18 | `&unk_5B8D628` | wgmma fence aligned | `dialects/nvvm.md` |
| `nvvm.wgmma.mma_async` | 0x14 | `&unk_5B8D618` | wgmma async MMA | `dialects/nvvm.md` |
| `nvvm.wmma.load` | 0xE | `&unk_5B8D658` | wmma load | `dialects/nvvm.md` |
| `nvvm.wmma.mma` | 0xD | `&unk_5B8D650` | wmma MMA | `dialects/nvvm.md` |
| `nvvm.wmma.store` | 0xF | `&unk_5B8D648` | wmma store | `dialects/nvvm.md` |
| `nvvm.mma.block_scale` | 0x14 | `&unk_5B8D8D8` | MMA block-scale | `dialects/nvvm.md` |
| `nvvm.mma_smem_desc` | 0x12 | `&unk_5B8D7C8` | MMA smem desc | `dialects/nvvm.md` |
| `nvvm.mma.sparse.block_scale` | 0x1B | `&unk_5B8D8D0` | MMA sparse block-scale | `dialects/nvvm.md` |
| `nvvm.mma.sync` | 0xD | `&unk_5B8D7D0` | MMA sync | `dialects/nvvm.md` |
| `nvvm.ldmatrix` | 0xD | `&unk_5B8D898` | ldmatrix | `dialects/nvvm.md` |
| `nvvm.stmatrix` | 0xD | `&unk_5B8D768` | stmatrix | `dialects/nvvm.md` |

### §8.7 shfl / vote / redux / match / elect (5)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.elect.sync` | 0xF | `&unk_5B8DA78` | elect leader | `dialects/nvvm.md` |
| `nvvm.match.sync` | 0xF | `&unk_5B8D7E8` | match.sync | `dialects/nvvm.md` |
| `nvvm.redux.sync` | 0xF | `&unk_5B8D790` | redux.sync | `dialects/nvvm.md` |
| `nvvm.shfl.sync` | 0xE | `&unk_5B8D780` | shfl.sync | `dialects/nvvm.md` |
| `nvvm.vote.sync` | 0xE | `&unk_5B8D660` | vote.sync | `dialects/nvvm.md` |

### §8.8 Convert / cvt.packfloat (11)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.convert.bf16x2.to.f4x2` | 0x1B | `&unk_5B8DB68` | bf16x2→f4x2 | `dialects/nvvm.md` |
| `nvvm.convert.bf16x2.to.f8x2` | 0x1B | `&unk_5B8DB60` | bf16x2→f8x2 | `dialects/nvvm.md` |
| `nvvm.convert.f16x2.to.f4x2` | 0x1A | `&unk_5B8DB58` | f16x2→f4x2 | `dialects/nvvm.md` |
| `nvvm.convert.f16x2.to.f8x2` | 0x1A | `&unk_5B8DB50` | f16x2→f8x2 | `dialects/nvvm.md` |
| `nvvm.convert.f32x2.to.f4x2` | 0x1A | `&unk_5B8DB48` | f32x2→f4x2 | `dialects/nvvm.md` |
| `nvvm.convert.f32x2.to.f6x2` | 0x1A | `&unk_5B8DB40` | f32x2→f6x2 | `dialects/nvvm.md` |
| `nvvm.convert.f32x2.to.f8x2` | 0x1A | `&unk_5B8DB38` | f32x2→f8x2 | `dialects/nvvm.md` |
| `nvvm.convert.f4x2.to.f16x2` | 0x1A | `&unk_5B8DB30` | f4x2→f16x2 | `dialects/nvvm.md` |
| `nvvm.convert.float.to.tf32` | 0x1A | `&unk_5B8DB28` | float→tf32 | `dialects/nvvm.md` |
| `nvvm.cvt.packfloat` | 0x12 | `&unk_5B8DA90` | cvt.packfloat | `dialects/nvvm.md` |
| `nvvm.cvt.packfloat.f32` | 0x16 | `&unk_5B8DA98` | cvt.packfloat.f32 | `dialects/nvvm.md` |

### §8.9 read.ptx.sreg.* (73)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.read.ptx.sreg.clock` | 0x18 | `&unk_5B8DC18` | sreg clock | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.clock64` | 0x1A | `&unk_5B8DC20` | sreg clock64 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.cluster.ctaid.x` | 0x22 | `&unk_5B8DC48` | cluster.ctaid.x | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.cluster.ctaid.y` | 0x22 | `&unk_5B8DC40` | cluster.ctaid.y | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.cluster.ctaid.z` | 0x22 | `&unk_5B8DC38` | cluster.ctaid.z | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.cluster.ctarank` | 0x22 | `&unk_5B8DBC8` | cluster.ctarank | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.clusterid.x` | 0x1E | `&unk_5B8DBC0` | clusterid.x | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.clusterid.y` | 0x1E | `&unk_5B8DBB8` | clusterid.y | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.clusterid.z` | 0x1E | `&unk_5B8DBB0` | clusterid.z | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.cluster.nctaid.x` | 0x23 | `&unk_5B8DBF8` | cluster.nctaid.x | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.cluster.nctaid.y` | 0x23 | `&unk_5B8DBF0` | cluster.nctaid.y | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.cluster.nctaid.z` | 0x23 | `&unk_5B8DBE8` | cluster.nctaid.z | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.cluster.nctarank` | 0x23 | `&unk_5B8DC00` | cluster.nctarank | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.ctaid.x` | 0x1A | `&unk_5B8DC60` | ctaid.x | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.ctaid.y` | 0x1A | `&unk_5B8DC58` | ctaid.y | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.ctaid.z` | 0x1A | `&unk_5B8DC50` | ctaid.z | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg0` | 0x1A | `&unk_5B8DA70` | envreg0 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg1` | 0x1A | `&unk_5B8DA18` | envreg1 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg10` | 0x1B | `&unk_5B8DA68` | envreg10 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg11` | 0x1B | `&unk_5B8DA60` | envreg11 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg12` | 0x1B | `&unk_5B8DA58` | envreg12 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg13` | 0x1B | `&unk_5B8DA50` | envreg13 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg14` | 0x1B | `&unk_5B8DA48` | envreg14 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg15` | 0x1B | `&unk_5B8DA40` | envreg15 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg16` | 0x1B | `&unk_5B8DA38` | envreg16 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg17` | 0x1B | `&unk_5B8DA30` | envreg17 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg18` | 0x1B | `&unk_5B8DA28` | envreg18 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg19` | 0x1B | `&unk_5B8DA20` | envreg19 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg2` | 0x1A | `&unk_5B8D9C0` | envreg2 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg20` | 0x1B | `&unk_5B8DA10` | envreg20 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg21` | 0x1B | `&unk_5B8DA08` | envreg21 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg22` | 0x1B | `&unk_5B8DA00` | envreg22 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg23` | 0x1B | `&unk_5B8D9F8` | envreg23 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg24` | 0x1B | `&unk_5B8D9F0` | envreg24 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg25` | 0x1B | `&unk_5B8D9E8` | envreg25 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg26` | 0x1B | `&unk_5B8D9E0` | envreg26 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg27` | 0x1B | `&unk_5B8D9D8` | envreg27 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg28` | 0x1B | `&unk_5B8D9D0` | envreg28 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg29` | 0x1B | `&unk_5B8D9C8` | envreg29 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg3` | 0x1A | `&unk_5B8D9A8` | envreg3 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg30` | 0x1B | `&unk_5B8D9B8` | envreg30 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg31` | 0x1B | `&unk_5B8D9B0` | envreg31 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg4` | 0x1A | `&unk_5B8D9A0` | envreg4 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg5` | 0x1A | `&unk_5B8D998` | envreg5 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg6` | 0x1A | `&unk_5B8D990` | envreg6 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg7` | 0x1A | `&unk_5B8D988` | envreg7 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg8` | 0x1A | `&unk_5B8D980` | envreg8 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.envreg9` | 0x1A | `&unk_5B8D978` | envreg9 | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.globaltimer` | 0x1E | `&unk_5B8D918` | globaltimer | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.gridid` | 0x19 | `&unk_5B8D8F8` | gridid | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.laneid` | 0x19 | `&unk_5B8D8C8` | laneid | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.lanemask.eq` | 0x1E | `&unk_5B8D8C0` | lanemask.eq | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.lanemask.ge` | 0x1E | `&unk_5B8D8B8` | lanemask.ge | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.lanemask.gt` | 0x1E | `&unk_5B8D8B0` | lanemask.gt | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.lanemask.le` | 0x1E | `&unk_5B8D8A8` | lanemask.le | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.lanemask.lt` | 0x1E | `&unk_5B8D8A0` | lanemask.lt | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.nclusterid.x` | 0x1F | `&unk_5B8DBE0` | nclusterid.x | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.nclusterid.y` | 0x1F | `&unk_5B8DBD8` | nclusterid.y | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.nclusterid.z` | 0x1F | `&unk_5B8DBD0` | nclusterid.z | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.nctaid.x` | 0x1B | `&unk_5B8D910` | nctaid.x | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.nctaid.y` | 0x1B | `&unk_5B8D908` | nctaid.y | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.nctaid.z` | 0x1B | `&unk_5B8D900` | nctaid.z | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.nsmid` | 0x18 | `&unk_5B8D778` | nsmid | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.ntid.x` | 0x19 | `&unk_5B8DC78` | ntid.x | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.ntid.y` | 0x19 | `&unk_5B8DC70` | ntid.y | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.ntid.z` | 0x19 | `&unk_5B8DC68` | ntid.z | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.nwarpid` | 0x1A | `&unk_5B8D640` | nwarpid | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.smid` | 0x17 | `&unk_5B8D770` | smid | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.tid.x` | 0x18 | `&unk_5B8D678` | tid.x | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.tid.y` | 0x18 | `&unk_5B8D670` | tid.y | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.tid.z` | 0x18 | `&unk_5B8D668` | tid.z | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.warpid` | 0x19 | `&unk_5B8D638` | warpid | `dialects/nvvm.md` |
| `nvvm.read.ptx.sreg.warpsize` | 0x1B | `&unk_5B8D630` | warpsize | `dialects/nvvm.md` |

### §8.10 cluster_launch_ctrl (7)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.clusterlaunchcontrol.query_cancel.get_first_ctaid` | 0x36 | `&unk_5B8DBA8` | query first ctaid | `dialects/nvvm.md` |
| `nvvm.clusterlaunchcontrol.query_cancel.get_first_ctaid.x` | 0x38 | `&unk_5B8DBA0` | query first ctaid.x | `dialects/nvvm.md` |
| `nvvm.clusterlaunchcontrol.query_cancel.get_first_ctaid.y` | 0x38 | `&unk_5B8DB98` | query first ctaid.y | `dialects/nvvm.md` |
| `nvvm.clusterlaunchcontrol.query_cancel.get_first_ctaid.z` | 0x38 | `&unk_5B8DB90` | query first ctaid.z | `dialects/nvvm.md` |
| `nvvm.clusterlaunchcontrol.query_cancel.is_canceled` | 0x32 | `&unk_5B8DB88` | query is-canceled | `dialects/nvvm.md` |
| `nvvm.clusterlaunchcontrol.try_cancel` | 0x24 | `&unk_5B8DB78` | try cancel | `dialects/nvvm.md` |
| `nvvm.clusterlaunchcontrol.try_cancel.multicast` | 0x2E | `&unk_5B8DB80` | try cancel multicast | `dialects/nvvm.md` |

### §8.11 Fences (14)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.fence.acq_rel.cluster` | 0x1A | `&unk_5B8D6B8` | acq_rel cluster | `dialects/nvvm.md` |
| `nvvm.fence.acq_rel.cta` | 0x16 | `&unk_5B8D6B0` | acq_rel CTA | `dialects/nvvm.md` |
| `nvvm.fence.acq_rel.gpu` | 0x16 | `&unk_5B8D6A8` | acq_rel GPU | `dialects/nvvm.md` |
| `nvvm.fence.acq_rel.sys` | 0x16 | `&unk_5B8D6A0` | acq_rel sys | `dialects/nvvm.md` |
| `nvvm.fence.acquire` | 0x12 | `&unk_5B8D948` | acquire fence | `dialects/nvvm.md` |
| `nvvm.fence.mbarrier.init` | 0x18 | `&unk_5B8D940` | mbarrier-init fence | `dialects/nvvm.md` |
| `nvvm.fence.proxy` | 0x10 | `&unk_5B8D930` | proxy fence | `dialects/nvvm.md` |
| `nvvm.fence.proxy.acquire` | 0x18 | `&unk_5B8D938` | proxy acquire | `dialects/nvvm.md` |
| `nvvm.fence.proxy.release` | 0x18 | `&unk_5B8D928` | proxy release | `dialects/nvvm.md` |
| `nvvm.fence.release` | 0x12 | `&unk_5B8D920` | release fence | `dialects/nvvm.md` |
| `nvvm.fence.sc` | 0x11 | `&unk_5B8D680` | sc fence | `dialects/nvvm.md` |
| `nvvm.fence.sc.cluster` | 0x15 | `&unk_5B8D698` | sc cluster | `dialects/nvvm.md` |
| `nvvm.fence.sc.cta` | 0x11 | `&unk_5B8D690` | sc CTA | `dialects/nvvm.md` |
| `nvvm.fence.sc.gpu` | 0x11 | `&unk_5B8D688` | sc GPU | `dialects/nvvm.md` |

### §8.12 dot_accum (2)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.dot.accumulate.2way` | 0x18 | `&unk_5B8DA88` | dot accumulate 2-way | `dialects/nvvm.md` |
| `nvvm.dot.accumulate.4way` | 0x18 | `&unk_5B8DA80` | dot accumulate 4-way | `dialects/nvvm.md` |

### §8.13 griddep / proxy / tensormap (5)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.griddepcontrol.launch.dependents` | 0x25 | `&unk_5B8D8F0` | griddepcontrol launch dependents | `dialects/nvvm.md` |
| `nvvm.griddepcontrol.wait` | 0x18 | `&unk_5B8D8E8` | griddepcontrol wait | `dialects/nvvm.md` |
| `nvvm.prefetch` | 0xD | `&unk_5B8D7B0` | prefetch | `dialects/nvvm.md` |
| `nvvm.prefetch.tensormap` | 0x17 | `&unk_5B8D7A8` | prefetch tensormap | `dialects/nvvm.md` |
| `nvvm.tensormap.cp_fenceproxy` | 0x1C | `&unk_5B8D6C0` | tensormap cp_fenceproxy | `dialects/nvvm.md` |

### §8.14 Misc (19)

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `nvvm.add.packed.f32x2` | 0x15 | `&unk_5B8DCB8` | packed f32x2 add | `dialects/nvvm.md` |
| `nvvm.atomicrmw` | 0xE | `&unk_5B8DCB0` | LLVM atomicrmw wrapper | `dialects/nvvm.md` |
| `nvvm.breakpoint` | 0xF | `&unk_5B8DC30` | breakpoint | `dialects/nvvm.md` |
| `nvvm.exit` | 9 | `&unk_5B8D970` | thread exit | `dialects/nvvm.md` |
| `nvvm.fabs` | 9 | `&unk_5B8D958` | float abs | `dialects/nvvm.md` |
| `nvvm.fma.packed.f32x2` | 0x15 | `&unk_5B8D950` | packed f32x2 FMA | `dialects/nvvm.md` |
| `nvvm.fmax` | 9 | `&unk_5B8D7E0` | float max | `dialects/nvvm.md` |
| `nvvm.fmin` | 9 | `&unk_5B8D7D8` | float min | `dialects/nvvm.md` |
| `nvvm.inline_ptx` | 0xF | `&unk_5B8D8E0` | inline PTX | `dialects/nvvm.md` |
| `nvvm.load.ext` | 0xD | `&unk_5B8D968` | extended load | `dialects/nvvm.md` |
| `nvvm.mapa` | 9 | `&unk_5B8D7F0` | mapa | `dialects/nvvm.md` |
| `nvvm.mul` | 8 | `&unk_5B8D7C0` | multiply | `dialects/nvvm.md` |
| `nvvm.mul.packed.f32x2` | 0x15 | `&unk_5B8D7B8` | packed f32x2 multiply | `dialects/nvvm.md` |
| `nvvm.rcp.approx.ftz.f` | 0x15 | `&unk_5B8D7A0` | reciprocal approx ftz | `dialects/nvvm.md` |
| `nvvm.red` (family — TypeID-only; no literal mnemonic string) | 8 | `&unk_5B8D798` | atomic reduction family; concrete forms surfaced in the string table are `nvvm.redux.sync` and `nvvm.barrier.cta.red`; the variant-3 `red_op`/`red_type` parser slots are described in `dialects/nvvm/properties-blob-and-attr-parsers.md` | `dialects/nvvm.md` |
| `nvvm.setmaxregister` | 0x13 | `&unk_5B8D788` | set-max-register | `dialects/nvvm.md` |
| `nvvm.st.bulk` | 0xC | `&unk_5B8DC28` | bulk store | `dialects/nvvm.md` |
| `nvvm.store.ext` | 0xE | `&unk_5B8D960` | extended store | `dialects/nvvm.md` |
| `nvvm.sub.packed.f32x2` | 0x15 | `&unk_5B8D760` | packed f32x2 subtract | `dialects/nvvm.md` |

## §9 llvm-extras (upstream `llvm.*` ops observed in tileiras lowerings)

The MLIR `llvm` dialect is statically linked from upstream and registered
via `addOperation<>` chains; tileiras does not surface a per-op
`&unk_*` slot for these. The list below enumerates the `llvm.*`
mnemonics emitted by tileiras-driven lowerings. Dialect TypeID anchor is
`&unk_5BA8F60`.

| mnemonic | length | TypeID singleton | brief semantic | primary wiki page |
| --- | ---: | --- | --- | --- |
| `llvm.alloca` | 11 | upstream | stack alloca | `dialects/upstream-llvm.md` |
| `llvm.atomicrmw` | 14 | upstream | atomic RMW (the binary has no `llvm.atomic_cmpxchg` string; compare-and-swap is the separate `llvm.cmpxchg` op below) | `dialects/upstream-llvm.md` |
| `llvm.bitcast` | 12 | upstream | bit-pattern type pun | `dialects/upstream-llvm.md` |
| `llvm.call` | 9 | upstream | LLVM call | `dialects/upstream-llvm.md` |
| `llvm.cmpxchg` | 12 | upstream | atomic compare-and-swap | `dialects/upstream-llvm.md` |
| `llvm.dbg.cu` | 11 | upstream | DI compile-unit | `dialects/upstream-llvm.md` |
| `llvm.extractelement` | 19 | upstream | vector element extract | `dialects/upstream-llvm.md` |
| `llvm.fence` | 10 | upstream | LLVM fence | `dialects/upstream-llvm.md` |
| `llvm.func` | 9 | upstream | LLVM function | `dialects/upstream-llvm.md` |
| `llvm.getelementptr` | 18 | upstream | get-element-ptr (the binary has no abbreviated `llvm.gep` string; only the spelled-out form is present) | `dialects/upstream-llvm.md` |
| `llvm.global_ctors` | 17 | upstream | LLVM global constructors array | `dialects/upstream-llvm.md` |
| `llvm.global_dtors` | 17 | upstream | LLVM global destructors array | `dialects/upstream-llvm.md` |
| `llvm.global.annotations` | 23 | upstream | LLVM global annotations array | `dialects/upstream-llvm.md` |
| `llvm.insertelement` | 18 | upstream | vector element insert | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.align` | 20 | upstream | coroutine intrinsic — frame alignment query | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.begin` | 20 | upstream | coroutine intrinsic — frame begin | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.end` | 18 | upstream | coroutine intrinsic — frame end | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.free` | 19 | upstream | coroutine intrinsic — free frame storage | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.id` | 17 | upstream | coroutine intrinsic — identity token | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.promise` | 22 | upstream | coroutine intrinsic — promise/frame conversion | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.resume` | 21 | upstream | coroutine intrinsic — resume suspended frame | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.save` | 19 | upstream | coroutine intrinsic — save suspend index | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.size` | 19 | upstream | coroutine intrinsic — frame size query | `dialects/upstream-llvm.md` |
| `llvm.intr.coro.suspend` | 22 | upstream | coroutine intrinsic — suspend point | `dialects/upstream-llvm.md` |
| `llvm.intr.dbg.declare` | 21 | upstream | debug-info declare | `dialects/upstream-llvm.md` |
| `llvm.intr.dbg.label` | 19 | upstream | debug-info label | `dialects/upstream-llvm.md` |
| `llvm.intr.dbg.value` | 19 | upstream | debug-info value | `dialects/upstream-llvm.md` |
| `llvm.inttoptr` | 13 | upstream | int-to-pointer | `dialects/upstream-llvm.md` |
| `llvm.mlir.constant` | 18 | upstream | MLIR constant for LLVM type | `dialects/upstream-llvm.md` |
| `llvm.ptrtoint` | 13 | upstream | pointer-to-int | `dialects/upstream-llvm.md` |
| `llvm.return` | 11 | upstream | return | `dialects/upstream-llvm.md` |
| `llvm.select` | 11 | upstream | select | `dialects/upstream-llvm.md` |
| `llvm.shufflevector` | 18 | upstream | vector shuffle | `dialects/upstream-llvm.md` |
