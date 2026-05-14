# TypeID Sentinel Address Table

An MLIR `TypeID` is the runtime identity tag MLIR uses for fast structural
RTTI: every concrete C++ class the framework compares at runtime (a
`Dialect`, an `Op`, a `Type`, an `Attribute`, an `Interface`, even a
`Trait`) has exactly one `mlir::TypeID` value associated with it through
`mlir::TypeID::get<T>()`. The implementation produces that value by
reading the address of a per-class static storage object — the *address
itself* is the identity. A typical compiled MLIR binary therefore
contains hundreds of one-byte (or eight-byte Meyers-cached) anonymous
globals in `.bss` / `.rodata` whose sole role is to be compared against
each other by pointer equality. In `tileiras` (88 MB Blackwell-era CUDA
13.1 MLIR-based optimizing assembler) those sentinels cluster densely in
the `0x5B37B90 .. 0x5BE6138` band of the static-data segment, and one
sentinel address suffices to back-trace from a stripped function to the
exact `Op`/`Type`/`Attr` class it dispatches on. This page is the canonical
reverse-direction lookup table: address in, dialect-and-class out.

The binary uses two sentinel idioms in parallel. First, *static
pointer-identity* sentinels: one-byte `.bss` slots whose address is the
TypeID. No code ever writes the byte; the pointer is the value. These
dominate the cute / cute_nvgpu / nv_tileas / NVVM op slabs. Second,
*Meyers-cached* sentinels: an `{8-bit guard, 64-bit qword}` pair where
the qword fills in on first use by interning a C++-mangled
`mlir::TypeID::getFullName()` string through a process-wide pool
(`sub_44A6CA0` in this binary; upstream MLIR ships the same
RTTI-string-to-pointer interner under `llvm::ManagedStatic`). After init,
the qword holds the TypeID. These dominate the cute interface anchors
and a few out-of-strand singletons. Both forms reach the per-op
dispatcher exactly the same way: through a load of `*(qword*)(op + 48) +
16` (the `OperationName::TypeID` slot) and a pointer-equality test
against a sentinel address baked into the dispatcher arm.

A third special case is the "shared no-properties guard"
`&unk_5BE6138` — the global `OperationName::TypeID` reserved for the
sentinel class `mlir::detail::UnregisteredOpProperties`. Every
NVVM-to-LLVM and TileAS layout-classifier dispatcher tests against it
first to short-circuit the no-properties path or detect an op being
mid-rewritten. Every arm references it, making it the single most-cited
sentinel in the binary.

## How sentinels are consumed at runtime

Pointer-identity and Meyers-cached sentinels reach the dispatcher through
the same `OperationName::TypeID` slot; only the lazy-init step differs.
The minimum-cost lookup that a reimplementation must reproduce is:

```c
/* Pointer-identity sentinel — the address is the TypeID. */
const void *type_id_pointer_identity(const void *sentinel_byte_slot) {
    return sentinel_byte_slot;            /* no load; pointer is the value */
}

/* Meyers-cached sentinel — first call interns the C++ mangled
 * mlir::TypeID::getFullName() string through the process-wide pool
 * (sub_44A6CA0 in this binary), races resolved by the Itanium ABI
 * guard byte. After init, the qword holds the TypeID. */
const void *type_id_meyers_cached(uint8_t *guard, const void **qword,
                                  const char *type_full_name) {
    if (__atomic_load_n(guard, __ATOMIC_ACQUIRE) == 0) {
        if (__cxa_guard_acquire(guard)) {
            *qword = intern_typeid_string(type_full_name);
            __cxa_guard_release(guard);
        }
    }
    return *qword;
}

/* Dispatch is pointer-equality on the resolved TypeID, applied against
 * the OperationName::TypeID slot reached through Operation+0x30 ->
 * OperationName::Impl+0x10. */
static inline bool op_is_sentinel(const void *op, const void *sentinel) {
    const void *opname_impl = *(const void *const *)((const uint8_t *)op + 0x30);
    const void *type_id     = *(const void *const *)((const uint8_t *)opname_impl + 0x10);
    return type_id == sentinel;
}
```

Allocating a fresh TypeID storage per call instead of through one static slot will produce one new identity per call site, which makes pointer-equality dispatch impossible. The address-band discipline below — every sentinel of a kind lives in one contiguous slab emitted by one translation unit — is what guarantees one address per kind.

## Address-band index

The table partitions the sentinel space into the contiguous bands the
linker emitted for each dialect / category. Numbers under "Count" are
the distinct sentinels inside that band referenced elsewhere in the
binary; the rest is padding.

| Band | Count | Owner | Form |
|---|---|---|---|
| `0x5B37B90 .. 0x5B37C28` | 5 | Upstream MLIR Op/DialectInterface anchors | Meyers (8-byte qword) |
| `0x5B37BE8 .. 0x5B37BF0` | 2 | Dialect one-shot init guards | Guard byte |
| `0x5B37F20 .. 0x5B38170` | 4 | cuda_tile AbstractOperation singletons (`.data.rel.ro`) | Pointer-identity |
| `0x5B38080`, `0x5B381A8` | 2 | cuda_tile misc AttributeConcept / OperationState | Pointer-identity |
| `0x5B38BB0 .. 0x5B38BC8` | 4 | cuda_tile dialect Type TypeIDs | Pointer-identity |
| `0x5B38C40 .. 0x5B38C68` | 2 | nv_tile_ir::as Op-interface anchors | Meyers |
| `0x5B38F80` | 1 | TmaDescriptorTypeInterface anchor | Meyers |
| `0x5B445F8 .. 0x5B44890` | 3 | cutlass_ir::cute Layout / View / CopyAtom interfaces | Meyers |
| `0x5B44EB8 .. 0x5B44FD8` | 21 | nv_tileas op-info kindPtr singletons | Pointer-identity |
| `0x5B44F08` | 1 | nv_tileas op-ctor descriptor block tag | Pointer-identity |
| `0x5B452B0 .. 0x5B45970` | 6 | nv_tileas per-op attribute-vector sentinels | Pointer-identity |
| `0x5B45370` | 1 | nv_tileas pragma `ocg*` attr-vector | Pointer-identity |
| `0x5B46980 .. 0x5B469A0` | 2 | nv_tileaa NamedAttr-vector slots | Pointer-identity |
| `0x5B46D28 .. 0x5B46F68` | 33 | nv_tileaa per-op FoldRecord descriptors | Pointer-identity |
| `0x5B46E08`, `0x5B46E80`, `0x5B46E88`, `0x5B46F30`, `0x5B46FA0`, `0x5B46FA8` | 6 | nv_tileaa producer-side / element-type sentinels | Pointer-identity |
| `0x5B46FF0 .. 0x5B470D0` | 8 | cutlass_ir::cute core type-interface anchors | Meyers |
| `0x5B47490 .. 0x5B476A0` | ~20 | cutlass dialect per-op OpInfoBlock | Pointer-identity |
| `0x5B47FF8 .. 0x5B481A8` | 49 | cute_nvgpu Op TypeIDs (slab) | Pointer-identity |
| `0x5B482C8` | 1 | cute_nvgpu dialect TypeID | Pointer-identity |
| `0x5B48580 .. 0x5B48B20` | 12 | cute_nvgpu per-op attribute-table sentinels | Pointer-identity |
| `0x5B48D88 .. 0x5B48E58` | 27 | cute_nvgpu concrete Type TypeIDs | Pointer-identity |
| `0x5B496B8` | 1 | cute dialect TypeID | Pointer-identity |
| `0x5B49A98 .. 0x5B49B18` | 17 | cute dialect concrete Type TypeIDs | Pointer-identity |
| `0x5B8D610 .. 0x5B8DCB8` | 213 (197 referenced) | NVVM Op TypeID slab | Pointer-identity (8-byte slot stride) |
| `0x5BAADB8` | 1 | IntegerType variant (i32 / blocked layout id 1) | Pointer-identity |
| `0x5BA8F60` | 1 | LLVM dialect TypeID | Pointer-identity |
| `0x5BE3FF8` | 1 | scf.if AbstractOperation kindPtr | Pointer-identity |
| `0x5BE4008` | 1 | nv_tileas.convert_layout AbstractOperation kindPtr | Pointer-identity |
| `0x5BE5858` | 1 | arith.constant AbstractOperation kindPtr | Pointer-identity |
| `0x5BE5908` | 1 | arith dialect TypeID | Pointer-identity |
| `0x5BE5C40` | 1 | nv_tileas.async.pipeline.consume_one (paired form) | Pointer-identity |
| `0x5BE5FC0 .. 0x5BE6138` | ~10 | MLIR builtin FloatType / FloatVariant table | Pointer-identity |
| `0x5BE6138` | 1 | Shared no-properties / null-OperationName guard | Pointer-identity |

The runtime invariant this layout captures: a sentinel address in
`0x5B44E*` / `0x5B44F*` is an `OperationName::opInfo` slot (the
descriptor passed at registration time), whereas one in `0x5BE3F*` /
`0x5BE4*` / `0x5BE5*` is the *paired* `kindPtr` slot
(`AbstractOperation::TypeID`) that ends up in `op->getName().getTypeID()`
after uniquing. The two ranges contain duplicates of each op identity at
two different indirection levels; resolvers and rewriters generally
compare against the kindPtr form, op-builders and registrars against
the opInfo form.

## Master sentinel table

Sorted by sentinel address, ascending. For each row: dialect, the C++
class or op/type/attr name, byte length of the sentinel's storage (1 for
pointer-identity, 8 for the qword half of a Meyers pair, 9 for the
guard+qword combined), and the wiki page that documents the matching op /
type / interface in detail.

| Sentinel | Dialect | Class / op / attr name | Bytes | First-cited page |
|---|---|---|---|---|
| `0x5B37B90` | upstream MLIR | RegionBranchTerminatorOpInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B37B98` | upstream MLIR | RegionBranchTerminatorOpInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B37BE8` | upstream MLIR | RegionBranchOpInterface (cache slot) | 8 | dialects/cute/interfaces.md |
| `0x5B37BF0` | nv_tileaa | dialect one-shot init guard | 1 | dialects/nv_tileaa/index.md |
| `0x5B37C20` | upstream MLIR | OpAsmDialectInterface (guard) | 1 | dialects/index.md |
| `0x5B37C28` | upstream MLIR | OpAsmDialectInterface (TypeID dword) | 8 | dialects/index.md |
| `0x5B37F20` | cuda_tile | cuda_tile.return AbstractOperation (primary) | 1 | dialects/cuda_tile/return.md |
| `0x5B37FA8` | cuda_tile | cuda_tile.return AbstractOperation (secondary interface) | 1 | dialects/cuda_tile/return.md |
| `0x5B38080` | cuda_tile | ArrayAttr element AttributeConcept | 1 | dialects/cuda_tile/attrs.md |
| `0x5B380C0` | cuda_tile | cuda_tile.if AbstractOperation | 1 | dialects/cuda_tile/if.md |
| `0x5B38170` | cuda_tile | cuda_tile.continue AbstractOperation | 1 | dialects/cuda_tile/continue.md |
| `0x5B381A8` | cuda_tile | OperationState concept (sub_669F80) | 1 | dialects/cuda_tile/index.md |
| `0x5B38BB0` | cuda_tile | cuda_tile.partition_view (TypeID) | 1 | dialects/cuda_tile/types.md |
| `0x5B38BB8` | cuda_tile | cuda_tile.tensor_view (TypeID) | 1 | dialects/cuda_tile/types.md |
| `0x5B38BC0` | cuda_tile | cuda_tile.tile (TileType TypeID) | 1 | dialects/cuda_tile/types.md |
| `0x5B38BC8` | cuda_tile | cuda_tile.ptr (PointerType TypeID) | 1 | dialects/cuda_tile/types.md |
| `0x5B38C40` | nv_tile_ir::as | ProducerOpInterface (guard) | 1 | dialects/nv_tileas/interfaces.md |
| `0x5B38C48` | nv_tile_ir::as | ProducerOpInterface (TypeID qword) | 8 | dialects/nv_tileas/interfaces.md |
| `0x5B38C60` | nv_tile_ir::as | AgentLikeOpInterface (guard) | 1 | dialects/nv_tileas/interfaces.md |
| `0x5B38C68` | nv_tile_ir::as | AgentLikeOpInterface (TypeID qword) | 8 | dialects/nv_tileas/interfaces.md |
| `0x5B38F80` | cutlass_ir::cute | TmaDescriptorTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B445F8` | cutlass_ir::cute | LayoutTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B44600` | cutlass_ir::cute | LayoutTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B44610` | cutlass_ir::cute | ViewTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B44618` | cutlass_ir::cute | ViewTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B44888` | cutlass_ir::cute | CopyAtomTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B44890` | cutlass_ir::cute | CopyAtomTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B44EB8` | nv_tileas | nv_tileas.view (opInfo) | 1 | dialects/nv_tileas/view.md |
| `0x5B44EC8` | nv_tileas | nv_tileas.tiled_store (opInfo) | 1 | dialects/nv_tileas/tiled-store.md |
| `0x5B44ED0` | nv_tileas | nv_tileas.tiled_load (opInfo) | 1 | dialects/nv_tileas/tiled-load.md |
| `0x5B44ED8` | nv_tileas | nv_tileas.tiled_atomic_rmw (opInfo) | 1 | dialects/nv_tileas/tiled-atomic-rmw.md |
| `0x5B44EE0` | nv_tileas | nv_tileas.store (opInfo) | 1 | dialects/nv_tileas/store.md |
| `0x5B44EF0` | nv_tileas | nv_tileas.scatter_store (opInfo) | 1 | dialects/nv_tileas/scatter-store.md |
| `0x5B44EF8` | nv_tileas | nv_tileas.async.pipeline.consumer_release (opInfo) | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B44F08` | nv_tileas | op-ctor descriptor block tag | 1 | dialects/nv_tileas/index.md |
| `0x5B44F10` | nv_tileas | nv_tileas.pragma (paired opInfo) | 1 | dialects/nv_tileas/pragma.md |
| `0x5B44F18` | nv_tileas | nv_tileas.async.pipeline.consumer_yield | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B44F20` | nv_tileas | nv_tileas.producer_write | 1 | dialects/nv_tileas/producer-write.md |
| `0x5B44F38` | nv_tileas | nv_tileas.async.pipeline.produce_one | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B44F58` | nv_tileas | nv_tileas.produce_one_async | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B44F68` | nv_tileas | nv_tileas.consumer_read | 1 | dialects/nv_tileas/consumer-read.md |
| `0x5B44F70` | nv_tileas | nv_tileas.async.pipeline.consume_one | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B44F78` | nv_tileas | nv_tileas.consume_one_async | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B44F90` | nv_tileas | nv_tileas.load (opInfo) | 1 | dialects/nv_tileas/load.md |
| `0x5B44FA8` | nv_tileas | nv_tileas.gather_load (opInfo) | 1 | dialects/nv_tileas/gather-load.md |
| `0x5B44FB8` | nv_tileas | nv_tileas.async.pipeline.consumer_release-family (paired) | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B44FD8` | nv_tileas | nv_tileas.convert_layout (opInfo) | 1 | dialects/nv_tileas/convert-layout.md |
| `0x5B44FF0` | nv_tileas | nv_tileas.async.pipeline.acquire (positional) | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B45070` | nv_tileas | nv_tileas.alloc_tensor | 1 | dialects/nv_tileas/alloc-tensor.md |
| `0x5B452B0` | nv_tileas | nv_tileas.scatter_store attr-vec ("atom") | 1 | dialects/nv_tileas/scatter-store.md |
| `0x5B45370` | nv_tileas | nv_tileas.pragma attr-vec (`ocgEnter/LeaveDirectives`) | 1 | dialects/nv_tileas/pragma.md |
| `0x5B453E0` | nv_tileas | nv_tileas.async.pipeline.consumer_wait attr-vec | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B45600` | nv_tileas | nv_tileas.gather_load attr-vec | 1 | dialects/nv_tileas/gather-load.md |
| `0x5B458C0` | nv_tileas | nv_tileas.async.pipeline.create_iterator attr-vec | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B45970` | nv_tileas | nv_tileas.async.gather_tma_load attr-vec | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5B46980` | nv_tileaa | NamedAttr-vector slot (2-slot pattern) | 8 | dialects/nv_tileaa/index.md |
| `0x5B469A0` | nv_tileaa | NamedAttr-vector slot (head) | 8 | dialects/nv_tileaa/index.md |
| `0x5B46D28` | nv_tileaa | nv_tileaa.yield FoldRecord | 1 | dialects/nv_tileaa/yield.md |
| `0x5B46D30` | nv_tileaa | nv_tileaa.view FoldRecord | 1 | dialects/nv_tileaa/view.md |
| `0x5B46D68` | nv_tileaa | nv_tileaa.splat FoldRecord | 1 | dialects/nv_tileaa/splat.md |
| `0x5B46D70` | nv_tileaa | nv_tileaa.scatter FoldRecord | 1 | dialects/nv_tileaa/scatter.md |
| `0x5B46D88` | nv_tileaa | nv_tileaa.return FoldRecord | 1 | dialects/nv_tileaa/return.md |
| `0x5B46D98` | nv_tileaa | nv_tileaa.queue.yield FoldRecord | 1 | dialects/nv_tileaa/queue.md |
| `0x5B46DA0` | nv_tileaa | nv_tileaa.queue.put FoldRecord | 1 | dialects/nv_tileaa/queue.md |
| `0x5B46DA8` | nv_tileaa | nv_tileaa.queue.get FoldRecord | 1 | dialects/nv_tileaa/queue.md |
| `0x5B46DB0` | nv_tileaa | nv_tileaa.ptr_to_int FoldRecord | 1 | dialects/nv_tileaa/ptr-to-int.md |
| `0x5B46DC0` | nv_tileaa | nv_tileaa.pragma FoldRecord | 1 | dialects/nv_tileaa/pragma.md |
| `0x5B46DD8` | nv_tileaa | nv_tileaa.opt_barrier FoldRecord | 1 | dialects/nv_tileaa/opt-barrier.md |
| `0x5B46DE0` | nv_tileaa | nv_tileaa.mulhiui FoldRecord | 1 | dialects/nv_tileaa/mulhiui.md |
| `0x5B46DF0` | nv_tileaa | nv_tileaa.message FoldRecord | 1 | dialects/nv_tileaa/message.md |
| `0x5B46DF8` | nv_tileaa | nv_tileaa.mark_for_reuse FoldRecord | 1 | dialects/nv_tileaa/mark-for-reuse.md |
| `0x5B46E08` | nv_tileaa | nv_tileaa.make_memref (opInfo) | 1 | dialects/nv_tileaa/make-memref.md |
| `0x5B46E18` | nv_tileaa | nv_tileaa.launch_func FoldRecord | 1 | dialects/nv_tileaa/launch-func.md |
| `0x5B46E20` | nv_tileaa | nv_tileaa.join_mem_token FoldRecord | 1 | dialects/nv_tileaa/queue.md |
| `0x5B46E28` | nv_tileaa | nv_tileaa.is_valid_program_id FoldRecord | 1 | dialects/nv_tileaa/program-id.md |
| `0x5B46E30` | nv_tileaa | nv_tileaa.int_to_ptr FoldRecord | 1 | dialects/nv_tileaa/ptr-to-int.md |
| `0x5B46E38` | nv_tileaa | nv_tileaa.inject_ir FoldRecord | 1 | dialects/nv_tileaa/inject-ir.md |
| `0x5B46E40` | nv_tileaa | nv_tileaa.histogram FoldRecord | 1 | dialects/nv_tileaa/histogram.md |
| `0x5B46E70` | nv_tileaa | nv_tileaa.generate FoldRecord | 1 | dialects/nv_tileaa/generate.md |
| `0x5B46E78` | nv_tileaa | nv_tileaa.gather_load FoldRecord | 1 | dialects/nv_tileaa/gather-load.md |
| `0x5B46E80` | nv_tileaa | nv_tileaa.func (opInfo) | 1 | dialects/nv_tileaa/func.md |
| `0x5B46E88` | nv_tileaa | nv_tileaa.fp_to_fp (opInfo) | 1 | dialects/nv_tileaa/fp-to-fp.md |
| `0x5B46E98` | nv_tileaa | nv_tileaa.extract_slice FoldRecord | 1 | dialects/nv_tileaa/extract-slice.md |
| `0x5B46EA8` | nv_tileaa | nv_tileaa.extern_ew FoldRecord | 1 | dialects/nv_tileaa/extern-ew.md |
| `0x5B46EC8` | nv_tileaa | nv_tileaa.ew_inline_asm FoldRecord | 1 | dialects/nv_tileaa/ew-inline-asm.md |
| `0x5B46EE0` | nv_tileaa | nv_tileaa.create_queue FoldRecord | 1 | dialects/nv_tileaa/queue.md |
| `0x5B46EE8` | nv_tileaa | nv_tileaa.create_mem_token FoldRecord | 1 | dialects/nv_tileaa/queue.md |
| `0x5B46F10` | nv_tileaa | nv_tileaa.cancel_next_program_id FoldRecord | 1 | dialects/nv_tileaa/program-id.md |
| `0x5B46F28` | nv_tileaa | nv_tileaa.broadcast FoldRecord | 1 | dialects/nv_tileaa/broadcast.md |
| `0x5B46F30` | nv_tileaa | nv_tileaa.block_tile (opInfo) | 1 | dialects/nv_tileaa/block-tile.md |
| `0x5B46F38` | nv_tileaa | nv_tileaa.bitcast FoldRecord | 1 | dialects/nv_tileaa/bitcast.md |
| `0x5B46F58` | nv_tileaa | nv_tileaa.assert FoldRecord | 1 | dialects/nv_tileaa/assert.md |
| `0x5B46F60` | nv_tileaa | nv_tileaa.addptr FoldRecord | 1 | dialects/nv_tileaa/addptr.md |
| `0x5B46F68` | nv_tileaa | nv_tileaa.addf FoldRecord | 1 | dialects/nv_tileaa/addf.md |
| `0x5B46FA0` | upstream MLIR | IntegerType variant (dot-operand layout id 2) | 1 | dialects/index.md |
| `0x5B46FA8` | upstream MLIR | IntegerType TypeID model (i1 / shared variant) | 1 | dialects/index.md |
| `0x5B46FF0` | cutlass_ir::cute | MmaAtomTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B46FF8` | cutlass_ir::cute | MmaAtomTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B47000` | cutlass_ir::cute | PrefetchAtomTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B47008` | cutlass_ir::cute | PrefetchAtomTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B47020` | cutlass_ir::cute | PrintableTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B47028` | cutlass_ir::cute | PrintableTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B47030` | cutlass_ir::cute | IteratorTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B47038` | cutlass_ir::cute | IteratorTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B47058` | cutlass_ir::cute | PointerTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B47060` | cutlass_ir::cute | PointerTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B47068` | cutlass_ir::cute | AtomTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B47070` | cutlass_ir::cute | AtomTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B47080` | cutlass_ir::cute | DescriptorIteratorTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B47088` | cutlass_ir::cute | DescriptorIteratorTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B470C8` | cutlass_ir::cute | MaybeStaticTypeInterface (guard) | 1 | dialects/cute/interfaces.md |
| `0x5B470D0` | cutlass_ir::cute | MaybeStaticTypeInterface (TypeID qword) | 8 | dialects/cute/interfaces.md |
| `0x5B47490 .. 0x5B476A0` | cutlass | Per-op OpInfoBlock band (~20 slots) | varies | dialects/cutlass/index.md |
| `0x5B47FF8 .. 0x5B481A8` | cute_nvgpu | Op TypeID slab (49 slots, 8-byte stride) | 1 each | dialects/cute_nvgpu/index.md |
| `0x5B482C8` | cute_nvgpu | dialect TypeID | 1 | dialects/cute_nvgpu/index.md |
| `0x5B48580` | cute_nvgpu | relinquish_tmem_alloc_permit attr-table | 8 | dialects/cute_nvgpu/relinquish-tmem-alloc-permit.md |
| `0x5B485A0` | cute_nvgpu | arch.sm100.dealloc_tmem attr-table | 8 | dialects/cute_nvgpu/dealloc-tmem.md |
| `0x5B485C0` | cute_nvgpu | arch.sm100.alloc_tmem attr-table | 8 | dialects/cute_nvgpu/alloc-tmem.md |
| `0x5B486A0` | cute_nvgpu | sm89.mma attr-table | 8 | dialects/cute_nvgpu/sm89-mma.md |
| `0x5B48700` | cute_nvgpu | sm90.mma attr-table | 8 | dialects/cute_nvgpu/sm90-mma.md |
| `0x5B48780` | cute_nvgpu | sm100.mma attr-table | 8 | dialects/cute_nvgpu/sm100-mma.md |
| `0x5B48800` | cute_nvgpu | SM120.block_scaled attr-table (17 entries) | 8 | dialects/cute_nvgpu/sm120-block-scaled.md |
| `0x5B488E0` | cute_nvgpu | sm100.umma attr-table | 8 | dialects/cute_nvgpu/sm100-umma.md |
| `0x5B489E0` | cute_nvgpu | stsm attr-table | 8 | dialects/cute_nvgpu/stsm.md |
| `0x5B48A20` | cute_nvgpu | sm80.cp_async attr-table | 8 | dialects/cute_nvgpu/sm80-cp-async.md |
| `0x5B48AF0` | cute_nvgpu | SM100.tma_store attr-table | 8 | dialects/cute_nvgpu/tma-store.md |
| `0x5B48B20` | cute_nvgpu | SM100.tma_reduce attr-table | 8 | dialects/cute_nvgpu/tma-reduce.md |
| `0x5B48D88` | cute_nvgpu | atom.non_exec_tiled_tma_reduce / SmemDescType | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48D90` | cute_nvgpu | atom.non_exec_tiled_tma_store / TmaDescriptorTiledType | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48D98` | cute_nvgpu | atom.non_exec_tiled_tma_load / TmaDescriptorIm2colType | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DA0` | cute_nvgpu | atom.stsm | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DA8` | cute_nvgpu | atom.ldsm | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DB0` | cute_nvgpu | atom.simt_async_copy | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DB8` | cute_nvgpu | atom.universal_copy | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DC0` | cute_nvgpu | atom.tma_reduce | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DC8` | cute_nvgpu | atom.tma_store | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DD0` | cute_nvgpu | atom.tma_load | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DD8` | cute_nvgpu | tma_descriptor_im2col | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DE0` | cute_nvgpu | tma_descriptor_tiled | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DE8` | cute_nvgpu | atom.s2t_copy | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DF0` | cute_nvgpu | atom.tmem_store | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48DF8` | cute_nvgpu | atom.tmem_load | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48E00` | cute_nvgpu | SM120.mma_bs (block-scaled) | 1 | dialects/cute_nvgpu/sm120-block-scaled.md |
| `0x5B48E08` | cute_nvgpu | sm100.mma_bs_sp | 1 | dialects/cute_nvgpu/sm100-mma.md |
| `0x5B48E10` | cute_nvgpu | sm100.mma_bs | 1 | dialects/cute_nvgpu/sm100-mma.md |
| `0x5B48E18` | cute_nvgpu | sm100.mma_sp | 1 | dialects/cute_nvgpu/sm100-mma.md |
| `0x5B48E20` | cute_nvgpu | sm100.mma | 1 | dialects/cute_nvgpu/sm100-mma.md |
| `0x5B48E28` | cute_nvgpu | sm90.mma (WGMMA) | 1 | dialects/cute_nvgpu/sm90-mma.md |
| `0x5B48E30` | cute_nvgpu | smem_desc_view | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48E38` | cute_nvgpu | smem_desc | 1 | dialects/cute_nvgpu/types.md |
| `0x5B48E40` | cute_nvgpu | sm89.mma (FP8 e4m3/e5m2) | 1 | dialects/cute_nvgpu/sm89-mma.md |
| `0x5B48E48` | cute_nvgpu | sm80.sparse_mma | 1 | dialects/cute_nvgpu/sm80-mma.md |
| `0x5B48E50` | cute_nvgpu | sm80.mma | 1 | dialects/cute_nvgpu/sm80-mma.md |
| `0x5B48E58` | cute_nvgpu | atom.universal_fma (SM70 path) | 1 | dialects/cute_nvgpu/types.md |
| `0x5B496B8` | cute | dialect TypeID | 1 | dialects/cute/index.md |
| `0x5B49A98` | cute | cute.tuple | 1 | dialects/cute/types.md |
| `0x5B49AA0` | cute | cute.fast_divmod_divisor | 1 | dialects/cute/types.md |
| `0x5B49AA8` | cute | cute.tiled_mma | 1 | dialects/cute/types.md |
| `0x5B49AB0` | cute | cute.tiled_copy | 1 | dialects/cute/types.md |
| `0x5B49AB8` | cute | cute.coord_tensor | 1 | dialects/cute/types.md |
| `0x5B49AC0` | cute | cute.memref (CuteMemRefType) | 1 | dialects/cute/types.md |
| `0x5B49AC8` | cute | cute.ptr (CutePtrType) | 1 | dialects/cute/types.md |
| `0x5B49AD0` | cute | cute.sparse_elem | 1 | dialects/cute/types.md |
| `0x5B49AD8` | cute | cute.composed_layout (ComposedLayoutType) | 1 | dialects/cute/types.md |
| `0x5B49AE0` | cute | cute.layout (LayoutType) | 1 | dialects/cute/types.md |
| `0x5B49AE8` | cute | cute.swizzle (SwizzleType) | 1 | dialects/cute/types.md |
| `0x5B49AF0` | cute | cute.tile (CuteTileType) | 1 | dialects/cute/types.md |
| `0x5B49AF8` | cute | cute.shape (CuteShapeType) | 1 | dialects/cute/types.md |
| `0x5B49B00` | cute | cute.stride | 1 | dialects/cute/types.md |
| `0x5B49B08` | cute | cute.coord (CuteCoordType) | 1 | dialects/cute/types.md |
| `0x5B49B10` | cute | cute.int_tuple (IntTupleType) | 1 | dialects/cute/types.md |
| `0x5B49B18` | cute / cute_nvgpu | ConstrainedInt + AtomIType (shared) | 1 | dialects/cute/types.md |
| `0x5B8D610 .. 0x5B8DCB8` | NVVM | Op TypeID slab — 213 slots, 197 referenced (see slab close-up) | 8 each | dialects/nvvm/index.md |
| `0x5BA8F60` | LLVM | dialect TypeID | 1 | dialects/index.md |
| `0x5BAADB8` | upstream MLIR | IntegerType variant (i32 / blocked layout id 1) | 1 | dialects/index.md |
| `0x5BE3FF8` | scf | scf.if AbstractOperation kindPtr | 1 | dialects/index.md |
| `0x5BE4008` | nv_tileas | nv_tileas.convert_layout AbstractOperation kindPtr | 1 | dialects/nv_tileas/convert-layout.md |
| `0x5BE5858` | arith | arith.constant AbstractOperation kindPtr | 1 | dialects/index.md |
| `0x5BE5908` | arith | dialect TypeID | 1 | dialects/index.md |
| `0x5BE5C40` | nv_tileas | nv_tileas.async.pipeline.consume_one (paired) | 1 | dialects/nv_tileas/async-pipeline.md |
| `0x5BE5FC0` | upstream MLIR | FloatType singleton (F16 entry, MED) | 1 | dialects/index.md |
| `0x5BE5FE0` | upstream MLIR | MemRefType TypeID model | 1 | dialects/index.md |
| `0x5BE6000` | upstream MLIR | FloatType singleton (F32 entry, MED) | 1 | dialects/index.md |
| `0x5BE6028` | upstream MLIR | FloatType singleton (F64 entry, MED) | 1 | dialects/index.md |
| `0x5BE6030` | upstream MLIR | FloatType singleton (slot between F64 and TF32, MED) | 1 | dialects/index.md |
| `0x5BE6038` | nv_tile_ir | tf32 (nv_tf32) storage sentinel | 1 | dialects/index.md |
| `0x5BE6040` | upstream MLIR | FloatType singleton (MED) | 1 | dialects/index.md |
| `0x5BE6048` | upstream MLIR | bf16 storage sentinel | 1 | dialects/index.md |
| `0x5BE6090` | upstream MLIR | f8E5M2 storage sentinel | 1 | dialects/index.md |
| `0x5BE60A0` | upstream MLIR | f8E4M3FN storage sentinel | 1 | dialects/index.md |
| `0x5BE6138` | MLIR detail | UnregisteredOpProperties / no-properties guard (shared) | 1 | dialects/index.md |

## NVVM op TypeID slab close-up: `0x5B8D610 .. 0x5B8DCB8`

The largest sentinel cluster in the binary is the contiguous NVVM-op
slab at `0x5B8D610 .. 0x5B8DCB8`. It is **1704 bytes long (0x6A8)**,
holds **213 8-byte slots** at uniform 8-byte stride, and the NVVMToLLVM
lowering dispatcher (`sub_2D67A80`, 92 KB) tests 197 of those slots as
per-op TypeID sentinels in a folded `dyn_cast` cascade walking the slab
from top-of-range (`0x5B8DCB8`) down. The remaining 16 slots correspond
to NVVM op classes handled exclusively by the SelectionDAG MatcherTable
path (`sub_1A833C0`) and never appear as explicit dispatcher arms.

Why it is contiguous: the linker emits one
`mlir::TypeID::Storage`-array initialization per dialect, where every
op-class registered through the TableGen-generated
`registerNVVMDialect()` entry point produces one 8-byte slot containing
the address of the class's `static thread_local TypeID::UniqueIdHolder`.
All 213 slots come from one translation unit's static data, so they
land in a single `.rodata` section with no padding between slots —
exactly the pattern observed.

How to read offset → op name: index `i = (slab_address - 0x5B8D610) / 8`.
The dispatcher walks arms in slab-descending order, so the first arm
reached at line ~2067 of `sub_2D67A80` matches `0x5B8DCB8`
(NVVM::CpAsyncCommitGroupOp). Each subsequent arm decrements the slot by
8. Slot `0x5B8D610 + 8*i` for `i ∈ [0, 212]` therefore corresponds to
the `(212 - i)`-th arm in walk order.

Selected anchor sentinels from inside the slab, with their op classes:

| Sentinel | NVVM Op class | Intrinsic-ID family |
|---|---|---|
| `0x5B8DCB8` | NVVM::CpAsyncCommitGroupOp | (top of dispatcher) |
| `0x5B8DCA8` | NVVM::CpAsyncWaitGroupOp | 8397 |
| `0x5B8DC90` | NVVM::Tcgen05DeallocOp | 8381, 0x20CD |
| `0x5B8DB58` | NVVM::AtomicRMWOp | (variant via sub_4261FA) |
| `0x5B8DB50` | NVVM::ReduceOp (variant 1) | (via sub_2E657E0) |
| `0x5B8DB48` | NVVM::ReduceOp (variant 2) | (via sub_2E657C0) |
| `0x5B8DB40` | NVVM::ReduceOp (variant 3, vec) | (via sub_2E65720) |
| `0x5B8DB38` | NVVM::AtomicCAS / nvvm.red.b128 | (via sub_2E65750) |
| `0x5B8DAF8` | NVVM::CpAsyncBulkTensorReduceOp | 8974-9011 |
| `0x5B8DAF0` | NVVM::CpAsyncBulkTensorPrefetchOp | 9150 |
| `0x5B8DAE8` | NVVM::CpAsyncBulkTensorSharedCTAToGlobalOp | 8956 |
| `0x5B8DAE0` | NVVM::CpAsyncBulkTensorSharedCTAToGlobalExtOp | 8956 |
| `0x5B8DAD8` | NVVM::CpAsyncBulkTensorSharedClusterToGlobalOp | 8951 |
| `0x5B8DAB8` | NVVM::Tcgen05FenceOp (fence pair v0) | 8609 |
| `0x5B8DAB0` | NVVM::Tcgen05FenceOp (fence pair v1) | 8610 |
| `0x5B8DAA8` | NVVM::CvtPackfloatF32Op | 0x21B3 = 8627 |
| `0x5B8DAA0` | NVVM::ElectSyncOp | 0x21A5 = 8613 |
| `0x5B8DA98` | NVVM::PrefetchOp | 0x21F7 = 8695 |
| `0x5B8DA90` | NVVM::CpAsyncShared.*.GlobalOp | 0x210F |
| `0x5B8D928` | NVVM::CvtFloatToFp8 / CvtPackedOp | 8305-8308 |
| `0x5B8D920` | NVVM::WgmmaCommitGroupSyncAlignedOp | 0x226A = 8810 |
| `0x5B8D918` | NVVM::WgmmaCommitGroup / WaitGroup | 8797-8799 |
| `0x5B8D910` | NVVM::WgmmaMmaAsync (block-variant 0x245C) | 0x245C = 9308 |
| `0x5B8D8F8` | NVVM::MmaBlockScaleOp | 9398 = 0x24B6 |
| `0x5B8D8F0` | NVVM::MmaSync sibling | 9035 |
| `0x5B8D8E8` | NVVM::MmaSync sibling | 9036 |
| `0x5B8D8D8` | NVVM::WgmmaMmaAsyncOp (full) | 0x226A = 8810 |
| `0x5B8D8D0` | NVVM::WgmmaMmaAsync sibling (operand-walked) | -- |
| `0x5B8D898` | NVVM::LdmatrixOp | 9153-9170 |
| `0x5B8D7E0` | NVVM::CpAsyncBulkTensorBaseOp | 8919-8966 |
| `0x5B8D7F8` | NVVM::CpAsyncShared.*.GlobalOp variant | 9259 / 9263 |
| `0x5B8D7F0` | NVVM::CpAsyncBulkSharedClusterToSharedCTAOp | 9217 |
| `0x5B8D7E8` | NVVM::CpAsyncCommitGroupOp / CpAsyncShared | 9220 / 9222 |
| `0x5B8D7D0` | NVVM::MmaOp (mma.sync) | (MatcherTable) |
| `0x5B8D7C8` | NVVM::WmmaOp (load/store/mma) | (MatcherTable) |
| `0x5B8D768` | NVVM::StmatrixOp | 9858-9866 |
| `0x5B8D700` | NVVM::Tcgen05MMAOp (full) | 10521-10525 |
| `0x5B8D6F8` | NVVM::Tcgen05MMABlockScaleOp | 10524-30 |
| `0x5B8D6F0` | NVVM::Tcgen05MMASparseOp | 10522-23 |
| `0x5B8D6E8` | NVVM::Tcgen05MMAWsOp | 10522-23 |
| `0x5B8D6E0` | NVVM::Tcgen05MMAWsSpOp | 10534 (gated) |
| `0x5B8D6D8` | NVVM::Tcgen05MMASpBlockScaleOp | 10522-30 |
| `0x5B8D6D0` | NVVM::Tcgen05ShiftOp | 10540 |
| `0x5B8D6C8` | NVVM::Tcgen05CommitOp | 9669-70, 10447 |
| `0x5B8D6C0` | NVVM::Tcgen05CommitArriveOp | 9671 = 0x25C7 |
| `0x5B8D6B8` | NVVM::Tcgen05CpOp | 9136 |
| `0x5B8D6B0` | NVVM::Tcgen05AllocOp | 8376, 0x20B7 |
| `0x5B8D6A8` | NVVM::Tcgen05DeallocOp | 8381, 0x20CD |
| `0x5B8D6A0` | NVVM::Tcgen05RelinquishAllocPermitOp | 8390-91 |
| `0x5B8D698` | NVVM::Tcgen05WaitOp | 9399 |
| `0x5B8D690` | NVVM::Tcgen05FenceOp | 8609 sibling |
| `0x5B8D688` | NVVM::Tcgen05LdmatrixOp | 9674-83 |
| `0x5B8D680` | NVVM::Tcgen05StmatrixOp | 9684-89 |
| `0x5B8D610 .. 0x5B8D670` | NVVM::Mbar / barrier / cluster / setmaxnreg / fence band (~25) | varies |

Block-anchor band assignments (within the slab, from the dispatcher walk
order):

| Slab band | Op-class family |
|---|---|
| `0x5B8DCB8 .. 0x5B8DC90` | cp.async commit/wait + tensormap descriptor builder + Tcgen05Dealloc |
| `0x5B8DC88 .. 0x5B8DC28` | 16 cp.async.bulk commit/wait fence-band siblings |
| `0x5B8DC20 .. 0x5B8DC00` | 3 cp.async.bulk commit/wait variants |
| `0x5B8DBF8 .. 0x5B8DB70` | 17 cp.async.bulk.tensor TMA store/load fan-out (1D-5D × im2col × multicast × L2hint) |
| `0x5B8DB68 .. 0x5B8DB58` | 3 atomic / red sibs |
| `0x5B8DB50 .. 0x5B8DB38` | 3 nvvm.red ops (variants by red_op × scope × type) |
| `0x5B8DB28 .. 0x5B8DB00` | 4 cp.async.commit / wait band |
| `0x5B8DAF8 .. 0x5B8DAE0` | 3 cp.async.bulk.tensor.reduce variants (S2G / G2S / prefetch) |
| `0x5B8DAD8 .. 0x5B8DAC0` | 3 ldmatrix-cluster siblings |
| `0x5B8DAB8 .. 0x5B8DAB0` | 2 nvvm.tcgen05.fence variants |
| `0x5B8DAA8 .. 0x5B8DA90` | cvt.packfloat / elect.sync / prefetch / cp.async.shared.global |
| `0x5B8DA88 .. 0x5B8DA78` | 3 cp.async-cluster-bulk siblings |
| `0x5B8DA70 .. 0x5B8DA18` | 6 mbarrier-init/inval/arrive variants |
| `0x5B8D9C0 .. 0x5B8D9D0` | 9 fence.{proxy,sc,acq_rel} cluster fan-out (0x2200 family) |
| `0x5B8D9B8 .. 0x5B8D978` | 9 mbarrier.test_wait/parity/timelimit fan-out |
| `0x5B8D928 .. 0x5B8D8F8` | cvt.float.to.fp8 / wgmma fence/commit/wait / mma.block_scale |
| `0x5B8D8F0 .. 0x5B8D8E8` | 2 mma.sync siblings (9035, 9036) |
| `0x5B8D8D8 .. 0x5B8D8D0` | wgmma.mma_async (full + sibling) |
| `0x5B8D8C8 .. 0x5B8D898` | ldmatrix-shape fan-out (m8n8 / m8n16 / m16n16) |
| `0x5B8D8A8 .. 0x5B8D898` | 3 stmatrix × num × trans variants (9637-38, 9858+) |
| `0x5B8D880 .. 0x5B8D7F8` | 4 cp.async.bulk.tensor.shared::cluster.global variants |
| `0x5B8D7F0 .. 0x5B8D7E8` | 2 nvvm.cp.async.shared (8463 / 9220) |
| `0x5B8D7E0` | nvvm.cp.async.bulk.tensor rank fan-out (8919-8966) |
| `0x5B8D7D8 .. 0x5B8D7C8` | 4 mma.sync / wmma siblings (9434-9505 dword table) |
| `0x5B8D7C0 .. 0x5B8D6F8` | 16 tcgen05.mma {full, sp, ws, ws.sp, block_scale, ...} |
| `0x5B8D6F0 .. 0x5B8D680` | 16 tcgen05 misc (ld/st/cp/commit/alloc/dealloc/wait) |
| `0x5B8D670 .. 0x5B8D610` | ~25 generic ops / cluster / setmaxnreg / lazy-tail siblings |

Slot stride and storage rationale: each slot is exactly 8 bytes because
the slab stores raw `void*` pointers, and on x86-64 the AT&T psABI
guarantees `_Alignof(void*) == sizeof(void*) == 8`. The address of slot
`i` is `0x5B8D610 + 8*i`, no per-slot padding. The dispatcher reads each
sentinel address as an immediate operand baked into the per-arm `cmp`
instruction, so any reimplementation must keep the slab contiguous and
8-byte aligned for the fold-up cascade to remain a single `cmp/je`
chain.

The shared `&unk_5BE6138` no-properties guard sits ~0x59 KB later than
the slab, in a different translation unit. Upstream MLIR intends this:
`UnregisteredOpProperties::TypeID` lives in `mlir/IR/OperationSupport.cpp`,
separate from the dialect's generated `registerNVVMDialect()`
translation unit. Placing the no-properties sentinel outside the slab
guards against a pointer-equality false-positive when an arm tests
`op.getName().getTypeID() == &slab[i]` against an op whose properties
record was never built.

## Cross-references

The Cross-references column in the master table points to the canonical wiki page for each sentinel's op or type. Conventions:

- `dialects/<dialect>/<op-mnemonic>.md` for op-info / op-class sentinels
- `dialects/<dialect>/types.md` for concrete Type TypeIDs
- `dialects/<dialect>/interfaces.md` for type-interface anchors (Meyers
  pairs)
- `dialects/<dialect>/index.md` for dialect-level TypeIDs and ranges
  whose per-op decomposition belongs to a separate strand
- `dialects/index.md` for upstream MLIR / cross-dialect anchors

Two cross-dialect sharing patterns are worth highlighting:

1. `0x5B49B18` is reused by both `cute.ConstrainedInt` and
   `cute_nvgpu.AtomIType`. The two share pointer identity because the
   inline printer emits the same `i<N>(<divby M>)?` surface syntax for
   both, and the underlying AbstractType class is parameterised on the
   same set of attributes — TableGen emits a single TypeID.
2. The PrintableTypeInterface qword `0x5B47028` is attached to every
   `cute` and almost every `cute_nvgpu` concrete type (27+ installs).
   When you trace a sentinel comparison against `0x5B47028`, you are
   inside the PrintableTypeInterface dispatch, not a per-type check.

Pairing convention: `nv_tileas.convert_layout` exemplifies the two-form
encoding. Its OperationName `opInfo` slot (the descriptor passed to
`sub_4461CA0` at op registration) is `0x5B44FD8`, while its
`AbstractOperation::TypeID` slot (the `kindPtr` reachable via
`*(qword*)(op+48)+16` after uniquing) is `0x5BE4008`. Resolvers compare
against the kindPtr; op-builders against the opInfo. Treat them as the
same op identity at two different indirection levels.

## Tier

T3 — deep reference for TypeID sentinel resolution.

## Confidence

HIGH for every sentinel address listed in the master table — these are
verbatim `.rodata` / `.bss` byte addresses extracted from `tileiras_full.c`
xrefs and cross-validated against multiple decompiled dispatchers
(`sub_2D67A80`, `sub_7ACC40`, `sub_7ABCD0`, `sub_7A19B0`, `sub_7A2090`).
HIGH for the slab-contiguity claim on `0x5B8D610 .. 0x5B8DCB8`: the 213
slots × 8-byte stride invariant is structurally enforced by both the
linker (single-translation-unit `registerNVVMDialect()` emission) and by
the dispatcher's folded `dyn_cast` cascade (the optimizer would not
fold-up unless every arm tests an immediate operand at fixed offset).
HIGH for the dialect attribution of every sentinel that participates in
a leaf-predicate or registrar-grep witness (the eight nv_tileas op-info
singletons in the `0x5B44E* / 0x5B44F*` cluster, every `cute` /
`cute_nvgpu` / `cuda_tile` concrete-type sentinel, every NVVM slab arm
named in the dispatcher decompile). MED for the op-name attribution of
inferred slab arms (the band assignments above mark groups; per-slot
exact-address attribution within a multi-arm band is reconstructed from
neighbouring code rather than a literal `cmp` instruction). MED for the
five unresolved FloatType slots in `0x5BE5FC0 / 6000 / 6028 / 6030 /
6040` and for the 49-slot `cute_nvgpu` Op TypeID range
`0x5B47FF8 .. 0x5B481A8` whose individual op-mnemonic mapping is a
strand-F follow-up.
