# nkilib Infrastructure — Allocator, Tiling & Common Types

> *All file:line citations on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 trees are byte-identical for these files). The source is the shipped, decompressed nkilib wheel under `nkilib/core/utils/` — readable Apache-2.0 NKI Python, not a stripped binary.*

## Abstract

Every production kernel in the nkilib reference tree — the flash-attention, RoPE, RMSNorm, MoE and conv kernels documented in [6.7.2 onward](attention-cte.md) — is built on a single layer of shared infrastructure that lives in `nkilib/core/utils/`. That layer does four things: it bump-allocates SBUF byte offsets at trace time (`allocator.py`, `modular_allocator.py`), it records the integer tiling geometry of each tensor axis (`tile_info.py`), it iterates a large dimension tile-by-tile with remainder handling (`tiled_range.py`), and it builds zero-copy strided views that lower to NKI access patterns (`tensor_view.py`). The shared enum vocabulary every kernel dispatches on lives in `common_types.py`. This page is the reference for all six files.

The single most important fact about this layer is that the nkilib "allocator" is **not** a register/graph-coloring allocator. It is a pure address-arithmetic layer that runs in the NKI trace and emits `nl.ndarray(..., address=(partition, byte_offset))` calls. The SBUF rectangle it manages is `(partition band × byte offset)`; it only ever bumps the **byte** axis, and it deliberately drops the partition dimension (`shape[0]`) from the byte budget. In manual mode it *pins* the rectangle so the BIR `MemoryLocation` arrives at the backend pre-colored, which the libwalrus coloring allocator must then honor as a fixed reservation. In auto mode it emits a symbolic tile and defers placement entirely. Contrast this with the backend's Chaitin-Briggs register allocator (Part 8): same tensors, two different allocators, one a source-level byte-bump hint generator, the other the authoritative physical placer. See [§ NKI vs Backend Allocator](#nki-allocator-vs-the-backend-coloring-allocator).

The page is organized one `##` unit per file, each with a fixed `### Purpose / ### State / ### Algorithm / ### Witness` grammar, followed by the 8-enum catalog and the two contrast sections. A reader who finishes it can reimplement the allocator address law, the ring-modulo reuse, the tile-remainder math, and the view→access-pattern handoff from the page alone.

For reimplementation, the contract is:

- **The SBUF address model** — `(partition, byte)` rectangle, byte-axis-only bump, partition dim excluded from the byte budget; manual-pin vs auto-symbolic mode selection.
- **Two multi-buffering schemes** — `BufferManager`'s scope/section high-water-mark recycling (with the `min_independent_addr` WAR-hazard guard) and `ModularAllocator`'s explicit ring-modulo reuse.
- **The tiling primitives** — `TiledDimInfo` (static per-axis geometry + remainder bounds) and `TiledRange` (the materialized remainder-aware loop iterator).
- **The view → AccessPattern handoff** — `TensorView` strided ops producing the `(stride, size)` AP-pair vector consumed by `base_tensor.ap(...)`.
- **The 8 dispatch enums** — exact members and values, with the live/dead status of each.

| | |
|---|---|
| **Allocator (scoped)** | `BufferManager` (`= SbufManager`), `allocator.py` (563 lines) |
| **Allocator (ring)** | `ModularAllocator`, `modular_allocator.py` (265 lines) |
| **Tile geometry** | `TiledDimInfo`, `tile_info.py` (111 lines) |
| **Tile iterator** | `TiledRange` / `TiledRangeIterator`, `tiled_range.py` (107 lines) |
| **Strided view** | `TensorView`, `tensor_view.py` (951 lines) |
| **Enum catalog** | `common_types.py` (96 lines) — 8 enums, zero dataclasses |
| **Address granularity** | byte offset on the free axis; partition dim (`shape[0]`) excluded |
| **Modes** | MANUAL (pin `address=(part,byte)`) / AUTO (`use_auto_alloc=True`, symbolic) |
| **Emit target** | `nl.ndarray(..., address=(base_partition, byte))` → BIR `MemoryLocation` |

---

## `allocator.py` — `BufferManager` (scoped stack + heap byte-bump)

### Purpose

`BufferManager` is the default SBUF allocator for nkilib kernels. Its file docstring (`allocator.py:16`) calls it a *"User space stack allocator with support of multi-buffer."* It manages one contiguous SBUF window `[sb_lower_bound, sb_upper_bound)` with two cursors growing toward each other — a stack from the bottom, a heap from the top — plus a scope tree that gives arena/region lifetime discipline and a section mechanism that implements in-loop double-buffering. The class is exported under two names: `BufferManager` and the backward-compat alias `SbufManager = BufferManager` (`allocator.py:563`); kernels still import `SbufManager` (e.g. `attention_block_tkg.py:62`).

### Module Primitives

Three free functions underpin every allocation (`allocator.py:32-66`):

| Function | Line | Behavior |
|---|---|---|
| `sizeinbytes(dtype)` | 32-54 | `str(dtype)` dispatch → element byte size; `kernel_assert(False, ...)` on miss |
| `align_to(value, alignment)` | 57-59 | `((v + a - 1) // a) * a` — verbatim `llvm::alignTo` |
| `num_elts(shape)` | 62-66 | product of all dims (called on `shape[1:]`, the per-partition free size) |

The `sizeinbytes` dispatch table (transcribed verbatim from `allocator.py:33-53`):

| dtype(s) | bytes | dtype(s) | bytes |
|---|---|---|---|
| `float32`, `int32`, `uint32` | 4 | `bfloat16`, `float16`, `uint16`, `int16` | 2 |
| `int8`, `uint8`, `float8_e4m3{,fn}`, `float8_e5m2` (`float8e4`/`float8e5`) | 1 | `float4_e2m1fn_x4` | 2 |
| `float8_e4m3fn_x4`, `float8_e5m2_x4` | 4 | | |

The two `_x4` micro-formats pack four sub-byte values into a wider container: four FP4 values in 2 bytes (`float4_e2m1fn_x4` = 2), four FP8 values in 4 bytes (`*_x4` = 4).

> **GOTCHA — the two trees disagree on `float4_e2m1fn_x4`.** The older `private_nkl/utils/StackAllocator.py` maps `float4_e2m1fn_x4 = 4` and additionally defines `tfloat32 = 4`. The current core `allocator.py:50-53` maps `float4_e2m1fn_x4 = 2` and has **no** `tfloat32` case — a `tfloat32` tensor hits the `kernel_assert(False)` at line 54. Use the core values: copying the old `= 4` mapping over-reserves every packed-FP4 buffer by 2×.

### The SBUF Address Model

An SBUF tensor occupies a `(partition band, byte offset)` rectangle. The allocator bumps only the **byte** axis; the partition axis is fixed by `base_partition` (default 0). The byte budget is computed per-partition (`alloc_stack`, `allocator.py:387-388`):

```c
// BufferManager.alloc_stack — allocator.py:387-388
N = num_elts(shape[1:]);                    // free-axis element count; shape[0]=partition EXCLUDED
bytes_per_partition = N * sizeinbytes(dtype);
```

> **QUIRK —** `shape[0]` — the partition dimension P (≤128) — is deliberately dropped from the address math. A `(128, 512)` bf16 tile costs `512 * 2 = 1024 B` of the byte axis, **not** `128 * 512 * 2`. This is exactly the byte-offset axis of the hardware SBUF geometry: each of the 128 partitions has its own copy of the free-axis bytes, so the byte cursor measures per-partition consumption. See [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md).

### Mode Selection — MANUAL vs AUTO

Mode is fixed at construction by `use_auto_alloc` (default `False`) and read by `is_auto_alloc()` (`allocator.py:240`). It changes exactly one thing: whether the emitted `nl.ndarray` carries an `address=`:

```c
// alloc_stack — allocator.py:411-420 (alloc_heap:475-484 is structurally identical)
if (use_auto_alloc)
    mloc = nl.ndarray(shape, dtype, buffer, name);                       // AUTO: symbolic — no address
else
    mloc = nl.ndarray(shape, dtype, buffer, name,
                      address=(base_partition, stack_curr_addr));        // MANUAL: PINNED rectangle
```

In AUTO mode the byte cursor still advances (so logical layout decisions are made), but the backend coloring allocator is free to place the tile anywhere. In MANUAL mode the `(partition, byte)` pair becomes an immovable reservation the backend must honor. A consequence: the cursor-inspection accessors `get_stack_curr_addr` / `get_heap_curr_addr` / `align_stack_curr_addr` all `kernel_assert(False)` under AUTO (`allocator.py:528-544`) — in AUTO the addresses are meaningless to the kernel.

### Stack, Heap, and the OOM Test

```c
// __init__ — allocator.py:185-188
stack_curr_addr = sb_lower_bound;   // grows UP
heap_curr_addr  = sb_upper_bound;   // grows DOWN
```

Both `alloc_stack` (373-437) and `alloc_heap` (439-499) run the **same** OOM predicate against the same gap (`allocator.py:390`, `:456`):

```c
// OOM guard, both paths (auto mode skips it entirely) — SAME predicate, DIFFERENT message literal
if (!is_auto_alloc() && stack_curr_addr + bytes_per_partition > heap_curr_addr)
    kernel_assert(False, "Stack out of memory");        // stack path: allocator.py:400
    // alloc_heap path emits "Heap out of memory" instead (allocator.py:466)
    // — both log stats + tree, then abort
```

> **NOTE — the heap OOM test is not the mirror of the stack one.** It is *not* `heap - req < stack`; it is the identical `stack_curr_addr + req > heap_curr_addr` (`allocator.py:456`). Both cursors are checked against the collision point between them, so the predicate is symmetric by construction.

`alloc_stack` requires an open scope (`kernel_assert` at 402-405), aligns the cursor (`align` defaults to `sizeinbytes(dtype)`), emits the ndarray, advances `stack_curr_addr += bytes_per_partition`, and updates the scope's high-water mark. `alloc_heap` grows down, realigns to a 4-byte floor (`align_to(heap_curr_addr - 3, 4)`, line 472), records the mloc on a LIFO `heap` list, and is **not** freed on scope close. `pop_heap` (501-517) re-queries `heap_top.shape[1:]` / `dtype` to recompute the size and restores `heap_curr_addr` with the same 4-byte realign.

The top-level `alloc` (336-371) is the dispatcher: HBM buffers (`nl.hbm` / `shared_hbm` / `private_hbm`) get a bare `nl.ndarray` with **no** address — DRAM placement is a separate downstream backend pass, never byte-managed here. Everything else routes to stack or heap by `default_stack_alloc`.

### Scopes — Arena Lifetime

`open_scope(interleave_degree=1, name="")` pushes a `Scope` dataclass (`allocator.py:69-161`) recording `starting_addr = stack_curr_addr`. `close_scope()` (317-335) resets `stack_curr_addr = scope.starting_addr` — freeing every tensor allocated in the scope at once — pops the scope, and propagates the scope's `min_independent_addr` to the parent. This is classic region discipline: O(1) bulk reclaim, no per-tensor free.

```c
// close_scope — allocator.py:321-334
closing = scopes[-1];
stack_curr_addr = closing.starting_addr;          // bump-reset: bulk free
scopes.pop();
if (scopes is empty) { tree_logger.flush(); _print_stats(); }
else scopes[-1].update_min_independent_addr(closing.min_independent_addr);
```

### Sections and the `min_independent_addr` WAR Guard

`increment_section()` (269-315), combined with `Scope.num_sections` (= the `interleave_degree`), `cur_section_id`, and `min_independent_addr`, implements in-loop multi-buffering **without** a ring modulo. The section id cycles `0..num_sections-1`. On wrap back to 0 the stack resets to `scope.starting_addr` (reuse section-0 addresses). For a non-zero section the new section starts at `min_independent_addr` — above the high-water mark of every prior section, including allocations made in sub-scopes:

```c
// increment_section — allocator.py:297-315
top = scopes[-1];
top.cur_section_id += 1;
if (top.cur_section_id == top.num_sections) {           // wrap to section 0
    top.cur_section_id = 0;
    stack_curr_addr = top.starting_addr;                // recycle section-0 addresses
    if (len(scopes) >= 2)                                // hand high-water mark up to parent
        scopes[-2].update_min_independent_addr(top.min_independent_addr);
    top.min_independent_addr = top.starting_addr;       // reset for the next cycle
} else {
    stack_curr_addr = top.min_independent_addr;         // new section starts ABOVE all prior ones
}
```

The `min_independent_addr` field is the correctness invariant of the whole scoped scheme — the `Scope` definition carries a 40-line worked ASCII proof of the WAR hazard it prevents (`allocator.py:111-153`). Without it, double-buffering would silently alias a live tensor. The proof's worked example: with `interleave_degree=2`, allocating `t1` then opening a sub-scope to allocate `t2` then closing it leaves the cursor back at `t2`'s base; the next iteration's `t1@i=1` would then overlap `t2@i=0` — an anti-dependency. Tracking the max address any prior section touched (updated on every alloc at line 423, on child-scope close at line 334, and on reset-to-section-0 at line 305) forces the new section above the collision point.

> **GOTCHA —** the older `private_nkl/utils/StackAllocator.py` (the `SbufManager` predecessor) **lacks** `min_independent_addr` entirely — its `increment_section` just resets or continues with no high-water tracking. A reimplementation that follows the old code will produce a silently-wrong double buffer whenever a section allocates into a sub-scope. The high-water guard is the single real correctness improvement of the current `BufferManager`.

### `create_auto_alloc_manager`

```c
// allocator.py:557-559
function create_auto_alloc_manager(logger):
    return BufferManager(0, nl.tile_size.total_available_sbuf_size, logger, use_auto_alloc=True);
```

The convenience constructor for "give me the whole SBUF window in AUTO mode." It is the default path when a kernel is called without an externally-supplied `sbm`.

### Witness

```c
// attention_block_tkg.py — the BufferManager witness in THIS wheel
:62   from ...core.utils.allocator import SbufManager, create_auto_alloc_manager
:356  sbm = sbm if sbm != None else create_auto_alloc_manager(logger=Logger("attn-block-tkg"))
:357  sbm.open_scope(name="attn-blk-tkg-scope")
:464  attn_out = sbm.alloc_stack((d_head, B_attn * q_heads_attn * S_tkg), dtype=X.dtype, buffer=nl.sbuf)
:858  sb = sbm.alloc_stack(buf.shape, dtype=buf.dtype, buffer=nl.sbuf)        // _to_sbuf helper
```

`attention_block_tkg.py:356` is the only `create_auto_alloc_manager` call site in this wheel; the production kernels that also call it are not shipped here (see [§ Corpus](#corpus-and-the-three-tree-reconciliation)).

---

## `modular_allocator.py` — `ModularAllocator` (ring / modulo reuse)

### Purpose

`ModularAllocator` is the explicit ring-buffer allocator. Its docstring (`modular_allocator.py:16-22`) describes *"Modular tensor allocator for multi-buffering patterns in SBUF … circular buffering … Common in attention kernels where tiles are reused across loop iterations."* Where `BufferManager` recycles implicitly by resetting a frame cursor, `ModularAllocator` recycles explicitly by modulo-indexing a fixed physical pool: a kernel asks for `block_dim` *logical* tiles backed by `num_free_tiles` *physical* buffers, and tile `i` aliases tile `i + num_free_tiles`. It has no scopes and no heap — just a flat bump cursor the loop body drives.

### State

```c
// __init__ — modular_allocator.py:61-68
self._current_address = initial_address;   // default 0 — the ONLY state
```

`get_current_address()` / `set_current_address(addr)` (70-86) are manual checkpoint/restore. A kernel snapshots the cursor before a loop and restores it each iteration to recycle a region — the `ModularAllocator` equivalent of `increment_section`.

### Algorithm — the Address Law

`alloc_sbuf_tensor` (88-216) builds a (possibly nested) list of tensors whose addresses follow a ring-modulo law. The docstring states it (`modular_allocator.py:102-104`); `_allocate_recursive` (219-265) implements it:

```c
// _allocate_recursive base case — modular_allocator.py:234-245
addr_offset = 0;
stride = 1;
for (dim_idx = len(block_dim) - 1; dim_idx >= 0; dim_idx--) {   // walk dims low→high (innermost first)
    idx = indices[dim_idx] % num_free_tiles[dim_idx];          // <-- THE RING: modulo physical count
    addr_offset += idx * stride;
    stride *= num_free_tiles[dim_idx];                          // row-major over the PHYSICAL pool
}
tensor = nl.ndarray(shape, dtype, buffer=nl.sbuf,
                    address=(base_partition, sca + addr_offset * tile_size_bytes));
```

where, with the same partition-excluded convention as `BufferManager` (`modular_allocator.py:178-182`):

```c
tile_size_bytes = num_elts(shape[1:]) * sizeinbytes(dtype);   // shape[0] partition dim EXCLUDED
```

The equivalent closed form for the common 2-D case (docstring `:102-104`):

```text
address[i][j] = sca + ( (i % num_free_tiles[0]) * stride0
                      + (j % num_free_tiles[1]) * stride1 ) * tile_size_bytes
```

After building the list, the cursor advances by only the **physical** footprint — `Π num_free_tiles * tile_size_bytes` (`modular_allocator.py:196-214`), not the logical one. That is the entire point: 16 logical tiles over 4 physical buffers reserve 4 tiles of byte space, and indices 0/4/8/12 share an address.

```c
// alloc_sbuf_tensor — modular_allocator.py:196-214
total_physical_tiles = 1;
for (i in range(len(num_free_tiles))) total_physical_tiles *= num_free_tiles[i];
nested_list = _allocate_recursive(...);
self._current_address += total_physical_tiles * tile_size_bytes;   // reserve PHYSICAL footprint only
```

Defaults and shapes (`:160-193`):

- `align_to` (param) → aligns the cursor via `align_to_fn` (imported from `allocator.py`) **before** allocation.
- `block_dim=None` → `[]` → a single tensor, cursor advances one `tile_size_bytes`.
- `num_free_tiles=None` → defaults to `block_dim.copy()` ⇒ **no aliasing** (every logical tile is distinct). To get reuse, the caller passes `num_free_tiles[d] < block_dim[d]`.
- return shape: `[]` → single tensor; length 1 → flat list; length N → N-deep nested list.
- `kernel_assert(len(block_dim) == len(num_free_tiles))` (172-175).

> **QUIRK —** the modulo lives in the *recursive index computation*, not in the data structure. The returned list has the full *logical* `block_dim` length (16 entries), but multiple entries are `nl.ndarray` handles pointing at the **same** byte address. The kernel writes `k_loaded[i]` for `i in range(16)` and the aliasing is invisible at the call site — the allocator pre-folded `i % num_free_tiles` into the emitted address. A reimplementation that materializes only `num_free_tiles` handles and asks the caller to do `i % K` themselves changes the kernel-facing API.

### Witness

`ModularAllocator` ships in this wheel purely as library code: it exists in both `nkilib/core/utils/modular_allocator.py` (this subject) and the older `neuronxcc/private_nkl/utils/modular_allocator.py` twin, but a sweep finds **zero** instantiation sites outside those two class definitions. Its consumers — the flash-attention and cumsum production kernels, which ping-pong Q groups with `num_free_tiles=[2]` — belong to the fuller kernel corpus that is not shipped here (see the corpus note below). The address law documented above comes straight from `modular_allocator.py`; the usage pattern is [INFERRED] for this wheel.

---

## `tile_info.py` — `TiledDimInfo` (per-axis tile geometry)

### Purpose

`TiledDimInfo` (`tile_info.py:29-111`) is a `@dataclass(NKIObject)` holding the integer tiling geometry of **one** tensor axis: how big the dimension is, how big each tile is, how many tiles cover it, and (optionally) a recursive sub-tile descriptor. Kernel-specific `*TileInfo` aggregates hold several of these — one per logical axis the kernel tiles.

### Fields

```c
// tile_info.py:44-50
tiled_dim_size  : int                    // size of the dimension being tiled
tile_size       : int                    // size of each tile
tile_count      : int                    // number of tiles to cover the dimension
subtile_dim_info: Optional[TiledDimInfo]  // recursive sub-tile descriptor (None = no subtiling)
```

There is no monolithic "TileInfo struct" in this layer. `TiledDimInfo` carries no dtype, no SBUF/PSUM/DRAM buffer kind, and not even a multi-axis shape — the four fields above are the whole class. Tile metadata is deliberately split three ways: `TiledDimInfo` holds the integer geometry of one axis, the `nl.ndarray` produced by the allocator holds dtype and buffer kind, and `TensorView.dtype` holds the view dtype.

### Factories and the Remainder Math

The dataclass is constructed only via static factories (`tile_info.py:35-59`; the comment at :53 reads *"ONLY CONSTRUCT THIS USING THE FACTORY METHODS BELOW"*):

```c
// build — tile_info.py:36-38
function build(tiled_dim_size, tile_size, subtile_info=None):
    tile_count = get_ceil_quotient(tiled_dim_size, tile_size);   // ceil-div, kernel_helpers.py:62
    return TiledDimInfo(tiled_dim_size, tile_size, tile_count, subtile_info);

// build_with_subtiling — tile_info.py:57-59 (two-level)
function build_with_subtiling(tiled_dim_size, tile_size, subtile_size):
    subtiled = build(tile_size, subtile_size);                   // inner: tile split into subtiles
    return build(tiled_dim_size, tile_size, subtiled);
```

The index/bound helpers map a tile number to a hardware slice. The remainder clamp at line 104-106 is what makes ragged last tiles correct:

```c
// get_tile_bound — tile_info.py:104-106  (the REMAINDER clamp)
function get_tile_bound(tile_idx):
    tile_start = tile_idx * tile_size;
    return min(tiled_dim_size - tile_start, tile_size);          // last tile shrinks to the remainder

// get_tile_indices — tile_info.py:67-68  (dynamic-slice descriptor)
function get_tile_indices(tile_num, tile_offset):
    return nl.ds(tile_num * tile_size, tile_offset);             // nl.ds = NKI dynamic slice
```

The subtile family (`get_subtile_indices` :73, `get_subtile_start` :81, `get_local_subtile_start` :86, `get_subtile_bound` :91, `get_local_subtile_bound` :97, `get_actual_subtile_num` :109) are the two-level versions; each opens with `kernel_assert(is_subtiled())`. `get_actual_subtile_num` returns `ceil(get_tile_bound(tile_idx) / subtile_dim_info.tile_size)` — the real subtile count inside a (possibly ragged) tile.

> **NOTE —** the `nl.ds`-emitting helpers carry a standing FE limitation comment (`tile_info.py:65-66`, `:71-72`): *"Now this only works if last item from mgrid. Fix once mgrid is properly supported."* A reimplementation targeting the current FE must respect that the dynamic-slice index must be the last mgrid item.

### Aggregation

The real per-kernel "tile info struct" aggregates `TiledDimInfo` members, one per axis: `MLPCTETileInfo` carries about nine of them with PSUM-bank-aware sizing (`NUM_HW_PSUM_BANKS` / `PSUM_BANK_SIZE`), while the RMSNorm variant needs only two. The typical sizes seen in this wheel are `nl.tile_size.pmax` (=128, the partition axis) for the outer/partition tile and `nl.tile_size.gemm_moving_fmax` (=512, the free axis) for the moving GEMM dimension. Those two numeric constants live inside the compiled `nki.language` module, so 128/512 are read from usage (e.g. `transpose.py:403`'s `nl.tile_size.pmax`) rather than transcribed from readable source.

---

## `tiled_range.py` — `TiledRange` (remainder-aware tile loop)

### Purpose

`TiledRange` is the runtime iterator companion to `TiledDimInfo`'s static descriptor. Where `TiledDimInfo` answers "how many tiles, how big" as queryable metadata, `TiledRange(size, tile_size)` returns a materialized **tuple** of `TiledRangeIterator` objects you loop over — like a remainder-aware `range` over tiles. It is the canonical "iterate a large dimension tile-by-tile" construct.

### State

```c
// TiledRangeIterator.__init__ — tiled_range.py:39-52
self.size         = tile_size;      // size of THIS tile (last tile = remainder, < tile_size)
self.index        = tile_index;     // 0-based tile index in the range
self.start_offset = start_offset;   // absolute start in the original dimension
self.end_offset   = end_offset;     // absolute end
```

### Algorithm

```c
// TiledRange — tiled_range.py:58-107
function TiledRange(size, tile_size):
    if isinstance(size, TiledRangeIterator):          // NESTED tiling: subtile a tile
        total_size  = size.size;
        base_offset = size.start_offset;              // inherit parent's absolute offset
    else:
        total_size  = size;
        base_offset = 0;
    num_tiles = ceil(total_size / tile_size);
    iterators = [];
    for (i in range(num_tiles)):
        relative   = i * tile_size;
        current    = min(tile_size, total_size - relative);   // <-- REMAINDER: last tile shrinks
        start      = base_offset + relative;
        end        = base_offset + relative + current;
        iterators.append(TiledRangeIterator(current, i, start, end));
    return tuple(iterators);                            // eagerly materialized
```

The docstring's worked example (`tiled_range.py:71-76`): `TiledRange(300, 128)` → `(128@0, 128@128, 44@256)` — two full tiles and a 44-wide remainder. Nested tiling passes a `TiledRangeIterator` as `size`, inheriting `start_offset` so subtiles carry absolute offsets.

### Witness

```c
// private_nkl/transpose.py — the real TiledRange witness in this wheel
:27   from neuronxcc.private_nkl.utils.tiled_range import TiledRange, TiledRangeIterator
:398  for I_tile in TiledRange(I, I_tile_size):
:399    for J_tile in TiledRange(J, J_tile_size):
:403      sb = nl.ndarray((nl.tile_size.pmax, num_128_tiles_per_I_tile, J_tile.size), ...)  // J_tile.size drives the alloc
:474      ... I_tile.start_offset * J ...                                                   // start_offset drives the HBM slice
```

The tile object's `.size` auto-handles the ragged last tile and `.start_offset` drives the source slice — exactly the large-tensor → tile pattern. Note the witness here uses the `private_nkl` twin import path; the `core/utils/tiled_range.py` and `private_nkl/utils/tiled_range.py` bodies are equivalent. `private_nkl/transpose.py` is the only file in this wheel that actually loops over a `TiledRange`; the production kernels that use it more heavily are not shipped here.

---

## `tensor_view.py` — `TensorView` (zero-copy strided view → AccessPattern)

### Purpose

`TensorView` (`tensor_view.py:36-951`, by far the richest of the six files) is a PyTorch/NumPy-style strided view over an `nl.ndarray`. No data is ever copied: the view holds `(shape, strides, offset)` metadata and, at `get_view()` time, emits an NKI array pattern — the `(stride, size)` pair list that becomes a BIR `AccessPattern`. It is the source-level builder of the same structure the backend lowers to DMA descriptors. See [Memref / View / Access Model](memref-view-model.md) and [BirCodeGenLoop Access-Pattern Builders](bircodegen-ap.md).

### State

```c
// TensorView state — tensor_view.py:51-57, 91-118
base_tensor   : nl.ndarray            // underlying tensor
shape         : Tuple[int,...]        // size per dim
strides       : Tuple[int,...]        // stride per dim, in ELEMENTS (not bytes)
offset        : int                   // base offset, in ELEMENTS
dtype         : object
scalar_offset : nl.ndarray = None     // dynamic scalar gather index
vector_offset : nl.ndarray = None     // per-partition gather index vector
indirect_dim  : Optional[int] = None  // base-tensor dim the indirect index addresses
```

`__init__` from a raw ndarray builds trivial row-major strides (`get_trivial_strides`, :68-89) and `offset=0`; from another `TensorView` it copies state, so wrapping is idempotent (:101-118). Every view op returns a **new** `TensorView` via `_copy` (:202-249), which validates strides ≥ 0, shape/stride length match, offset ≥ 0, and that `scalar_offset`/`vector_offset` are never both set.

### The AccessPattern Handoff

```c
// _get_pattern_and_offset + get_view — tensor_view.py:251-291
function _get_pattern_and_offset():
    ap_pattern = [];
    for (i in range(get_dim())):
        ap_pattern.append( (strides[i], shape[i]) );   // (step, num) PAIR per dim
    return ap_pattern, offset;

function get_view():                                    // → nl.ndarray array-pattern
    ap_pattern, offset = _get_pattern_and_offset();
    if (indirect_dim != None):
        if (vector_offset is not None):                 // per-partition gather
            return base_tensor.ap(pattern=ap_pattern, offset=offset,
                                  vector_offset=vector_offset, indirect_dim=indirect_dim, dtype=dtype);
        else:                                            // scalar gather
            return base_tensor.ap(pattern=ap_pattern, offset=offset,
                                  scalar_offset=scalar_offset, indirect_dim=indirect_dim, dtype=dtype);
    else:
        return base_tensor.ap(pattern=ap_pattern, offset=offset, dtype=dtype);
```

> **QUIRK —** the `(strides[i], shape[i])` list emitted at `tensor_view.py:258-261` is exactly the BIR `AccessPattern` AP-pair vector — `step = stride`, `num = size`, with `offset` as the base. `TensorView` is the front-end builder of the same structure the backend `AccessPattern` codegen lowers to DMA descriptors. So the full chain is: `TensorView.slice/permute/reshape` → AP pairs → BIR `AccessPattern` → DMA descriptor. The highest-dim step is the partition step the backend reads as `getStepBytesPerHighestDim`.

### View Ops

All return a new view via `_copy`. The SBUF partition-dim invariants (`dim 0` protected) apply only when `is_sbuf()` is true.

| Op | Line | Effect | SBUF dim-0 rule |
|---|---|---|---|
| `slice(dim,start,end,step)` | 293-331 | `offset += strides[dim]*start`; `size = ceil((end-start)/step)`; `stride *= step`; `end` clamped to `shape[dim]` | slicing `dim 0` of a vector_select view rejected |
| `permute(dims)` | 345-367 | reorder shape+strides (transpose) | `validate_permutation` asserts `dims[0]==0` |
| `broadcast(dim,size)` | 369-399 | `stride ← 0` (repeat); only size-1 dims | rejected on `dim 0` |
| `reshape_dim(dim,shape)` | 432-469 | split one dim into many (one `-1` allowed) | partition dim cannot be reshaped (unless trivial) |
| `flatten_dims(start,end)` | 471-509 | merge contiguous dims; asserts `stride[i]==shape[i+1]*stride[i+1]` | `start_dim > 0` |
| `expand_dim(dim)` / `squeeze_dim(dim)` | 511-557 | insert / remove a size-1 dim | not on `dim 0` |
| `select(dim,index)` | 666-681 | `int` → slice+squeeze (static); `ndarray` → `_dynamic_select` | inherits slice/squeeze rules |
| `reinterpret_cast(new_dtype)` | 120-200 | bit-reinterpret (NumPy `.view(dtype)`); scales all strides by byte-size ratio, only last dim changes count | requires contiguous last dim (`stride==1`), divisibility; blocked after dynamic/vector select |
| `rearrange(src,dst,fixed)` | 774-… | einops-style: detect src reshapes + dst flattens, compute permutation, apply reshape→permute→flatten | via the above ops |

`reinterpret_cast` is the subtle one. Because strides are in **elements**, a dtype change must rescale every stride so the byte distance is preserved (`tensor_view.py:160-200`): casting to a larger dtype divides strides by the size ratio (requires `stride[last]==1`, `last_dim_size % ratio == 0`, `offset % ratio == 0`); casting to smaller multiplies. It is blocked when `indirect_dim` is set, because `scalar_offset`/`vector_offset` are in base-tensor element units that would need rescaling (`kernel_assert` at :152-155).

### Indirect / Gather Addressing

```c
// _dynamic_select — tensor_view.py:559-586 (scalar gather)
function _dynamic_select(dim, index):
    kernel_assert(indirect_dim is None);                  // one dynamic select max
    kernel_assert(strides[dim] != 0);                     // not a broadcast dim
    view_stride = strides[dim];
    base_tensor, base_dim = _find_or_create_base_dim_for_stride(view_stride);
    return _copy(shape=remove(dim), strides=remove(dim),
                 scalar_offset=index, indirect_dim=base_dim, base_tensor=base_tensor);

// vector_select — tensor_view.py:588-626 (per-partition gather)
function vector_select(dim=0, vector_offset):
    kernel_assert(dim == 0);                              // only dim 0
    kernel_assert(strides[0] >= strides[i] for all i);   // dim 0 must have the largest stride
    base_tensor, base_dim = _find_or_create_base_dim_for_stride(strides[0]);
    new_shape = (vector_offset.shape[0],) + shape[1:];    // dim-0 size ← partition count
    return _copy(shape=new_shape, vector_offset=vector_offset,
                 indirect_dim=base_dim, base_tensor=base_tensor);
```

The clever part is `_find_or_create_base_dim_for_stride` (628-664): NKI's indirect AP needs a **physical** base-tensor dimension with the exact view stride. It first searches the base tensor's trivial strides (last dim → first) for an exact match. If none exists — e.g. after `slice(step>1)` or `reshape_dim` created a synthetic stride — it finds a base dim whose stride evenly divides the target, splits it via `base_tensor.reshape(...)` so the outer portion has exactly `view_stride`, and returns that. For non-HBM tensors it skips `dim 0` (`min_reshape_dim = 1`), since the partition dim is unreshapeable. If no factorization works it asserts (`:664`).

```c
// _find_or_create_base_dim_for_stride — tensor_view.py:647-664
for (i = ndim-1; i >= 0; i--)                             // exact match first, last dim first
    if (base_strides[i] == view_stride) return (base_tensor, i);
for (i = ndim-1; i >= min_reshape_dim; i--)               // else split a divisor dim
    if (view_stride % base_strides[i] == 0):
        split_factor = view_stride // base_strides[i];
        if (base_shape[i] % split_factor == 0 and base_shape[i] >= split_factor):
            // split dim i into (outer, split_factor); outer stride == view_stride
            return (base_tensor.reshape(split(i, split_factor)), i);
kernel_assert(False, "Cannot create base dim with stride ...");
```

### Witness

```c
// conv1d.py — the real TensorView witness in this wheel
:29   from ...core.utils.tensor_view import TensorView
:626  dst = TensorView(input_stacked)
:627       .slice(dim=0, start=partition_offset, end=partition_offset + c_in_tile_size, step=1)
:628       .slice(dim=1, start=first_valid_out, end=last_valid_out, step=1)
:629       .get_view(),                                          // view → DMA dst
:630  src = input_view.slice(dim=1, start=src_start, end=src_end, step=stride).get_view(),  // view → DMA src
```

`TensorView(...).slice(...).get_view()` is the HBM/SBUF-side slice descriptor feeding a DMA op; chained slices compose without copying. `is_sbuf()` / `is_hbm()` (62-66) gate the partition-dim invariants (dim 0 protected only for SBUF/PSUM). Other shipped users: `transformer_tkg.py`, `attention_block_tkg_sharding.py`, `attention_block_tkg.py`. Those four nkilib consumers plus the `private_nkl` twin are the complete `TensorView` user set in this wheel; the view algebra above comes directly from `tensor_view.py` and holds regardless.

---

## How They Compose Into a Kernel

The pipeline every nkilib kernel runs (witnessed in this wheel across `attention_block_tkg.py`, `conv1d.py`, `transpose.py`):

```text
(a) Build a *TileInfo (TiledDimInfo aggregate), sizing each axis to HW limits
    pmax=128 partition, gemm_moving_fmax=512 free, PSUM-bank-aware.            [tile_info.py]
(b) Create an allocator:
      SbufManager/create_auto_alloc_manager(...) + open_scope(name=...)        [allocator.py]
      OR ModularAllocator(0) for explicit ring tiles.                          [modular_allocator.py]
(c) alloc SBUF tiles: alloc_stack/alloc_heap (BufferManager) or
      alloc_sbuf_tensor(block_dim, num_free_tiles) (ModularAllocator ring).
      MANUAL mode pins (partition,byte) into nl.ndarray; AUTO defers.
(d) Iterate the big tensor:  for tile in TiledRange(dim, tile_size):          [tiled_range.py]
(e) Build the HBM-side gather/slice:
      TensorView(...).slice/select/permute().get_view()  →  DMA into the tile. [tensor_view.py]
(f) Recycle for the next stage: increment_section() / set_current_address(ckpt).
```

---

## NKI Allocator vs the Backend Coloring Allocator

The nkilib allocators and the libwalrus backend register allocator (Part 8 — the Chaitin-Briggs `ColoringAllocator`) are **two different allocators operating on the same tensors**. The NKI layer is a source-level byte-offset hint/pin generator that runs in the Python trace; the backend colorer is the authoritative physical placer that runs at BIR compile time. This is *why* both exist without conflicting.

| | NKI allocator (this page, front-end) | Backend coloring allocator (Part 8) |
|---|---|---|
| **When** | NKI trace/build time (Python) | BIR compile time (C++ `ColoringAllocator`, Chaitin-Briggs) |
| **Granularity** | byte-offset bump on the free axis | 2-D partition-band × byte first-fit / graph coloring |
| **Liveness** | scope frames / `min_independent_addr` high-water mark | live-interval analysis from a liveness pass |
| **Output** | `nl.ndarray(address=(part,byte))` PINNED, or symbolic | assigns physical `MemoryLocationSet` offsets |
| **Manual mode** | pre-colors → backend HONORS as a fixed reservation | treats pinned tiles as immovable; colors the rest |
| **Auto mode** | emits no address (symbolic tile) | fully colors it (select-node / first-fit + spill) |
| **HBM/DRAM** | bare `nl.ndarray`, no address | separate downstream DRAM placement pass |

The contrast with [6.5.15 — NKI Compiler-Option & Allocation Decorators](option-alloc-decorators.md) is sharper still and easy to confuse. That page documents the **compiler-side** `alloc` / `mod_alloc` / `auto_alloc` decorators and `verify_allocation` ISA constraints (PSUM 2 KiB/bank, SBUF `start_partition` tiers) — those are decorators on the *compiler's* placement of PSUM/SBUF. The `BufferManager` / `ModularAllocator` on **this** page are the **user-space** byte-bump allocators *inside* the nkilib kernels themselves. A `mod_alloc` decorator and a `ModularAllocator.alloc_sbuf_tensor` call share the word "modular" but are unrelated: one is a compiler annotation, the other a runtime ring-buffer address calculator.

---

## The 8-Enum Catalog — `common_types.py`

`common_types.py` (file A, `nkilib/core/utils/common_types.py`, 96 lines, md5 `0c2cff02…`, identical across cp310/11/12) defines exactly **8 enums and zero dataclasses**. Every member and value below is transcribed verbatim from the source.

> **NOTE — `common_types.py` is enums-only.** The MoE configuration dataclasses (`MoECTESpec`, `ShardOnBlockConfig`, `ShardOnIConfig`, `QuantizationConfig`, `MoECTEImplementation`) are *not* here despite the file's name; they live in `nkilib/core/moe/moe_cte/moe_cte.py` and are covered by the MoE kernel pages.

### `QKVOutputLayout` — `common_types.py:19-22`

```c
BSD  = 0   // (b, s, (n_q_heads + 2*n_kv_heads)*d_head) — fused [B, S, I]
NBSd = 1   // (num_heads, b, s, d_head)
NBdS = 2   // (num_heads, b, d_head, s)
```

`BSD` (default) and `NBSd` are the live device layouts; `NBdS` is device-**unsupported** — handled only in the reference torch path, hard-asserted-against in the QKV TKG kernels.

### `NormType` — `common_types.py:25-29`

```c
NO_NORM = 0;  RMS_NORM = 1;  LAYER_NORM = 2;  RMS_NORM_SKIP_GAMMA = 3
```

All four members live; no dead members.

### `ActFnType` — `common_types.py:32-36`

```c
SiLU = 0;  GELU = 1;  GELU_Tanh_Approx = 2;  Swish = 3
```

The four members are exactly `{SiLU, GELU, GELU_Tanh_Approx, Swish}`; the device map is `SiLU→nl.silu`, `GELU→nl.gelu` (exact erf), `GELU_Tanh_Approx→nl.gelu_apprx_tanh`, `Swish→nl.gelu_apprx_sigmoid` (`kernel_helpers.py:45-50`).

> **GOTCHA — two names mean less than they look.** There is no `SWIGLU` member: SwiGLU is a `gate * act(up)` *structural* pattern, built from `GateUpDim` plus the MLP, not an activation function. And `Swish` here maps to `nl.gelu_apprx_sigmoid`, the β=1.702 sigmoid approximation of GELU — not a generic Swish-β.

### `RouterActFnType` — `common_types.py:39-46`

```c
SIGMOID = 0;  SOFTMAX = 1
def __str__(self): return self.name.lower()   // "sigmoid" / "softmax" — the only enum with a __str__
```

Both members live (`router_topk` kernel); `SOFTMAX` gates the negmax/exp-sum path.

### `ExpertAffinityScaleMode` — `common_types.py:49-53`

```c
NO_SCALE = 0;  POST_SCALE = 1;  PRE_SCALE = 2;  PRE_SCALE_DELAYED = 3
```

`NO_SCALE`/`POST_SCALE`/`PRE_SCALE` are broadly live. `PRE_SCALE_DELAYED` is accepted at the API surface but normalized early to `PRE_SCALE` in the I/MX kernels (`if mode == PRE_SCALE_DELAYED: mode = PRE_SCALE`), and asserted-against in the block-shard and TKG paths — a documented-but-largely-unimplemented mode.

### `QuantizationType` — `common_types.py:56-62`

```c
NONE = 0;  STATIC = 1;  ROW = 2;  MX = 3;  STATIC_MX = 4;  ROW_MX = 5
```

`NONE`/`STATIC`/`ROW` (FP8 tensor-wise / row-wise) are widely used. `MX` (MXFP4/MXFP8) and `STATIC_MX` (FP8 "4× perf", `float8_e4m3fn_x4`-packed) are used in output-projection/MLP. `ROW_MX` is referenced only by a single `is_row_mx` predicate — effectively near-dead.

> **GOTCHA — the twin `common_types.py` is a smaller enum set.** The `_pre_prod_kernels` / `private_nkl` copy (file B≡C, 44 lines, md5 `9721a6bb…`) truncates `QuantizationType` to `{NONE, STATIC, ROW}` — it lacks `MX`/`STATIC_MX`/`ROW_MX` — and omits `QKVWeightLayout` and `GateUpDim` entirely. Downstream code in the `_pre_prod` namespace must not assume the 6-member enum.

### `QKVWeightLayout` — `common_types.py:65-91`

```c
CONTIGUOUS    = 0   // non-MX: checkpoint weights as-is [H, I]
MX_CONTIGUOUS = 1   // MX w/o DMA transpose: pack 4 consecutive H rows → float8_e4m3fn_x4
MX_INTERLEAVED= 2   // MX w/ DMA transpose: reorder rows to match transpose interleave, re-pack x4
```

All three live in the QKV kernels; default `CONTIGUOUS`; `MX_INTERLEAVED` is the DMA-transpose optimization path. The 27-line class docstring (`:65-87`) carries the exact pack/reshape recipes.

### `GateUpDim` — `common_types.py:94-96`

```c
GATE = 0;  UP = 1
```

Index selector into a fused gate/up weight tensor: `.select(dim=…, index=GateUpDim.GATE.value)` splits fused gate_up weights into halves. This is the structural mechanism behind "SwiGLU" (`gate * act(up)`) — there is no `SWIGLU` enum.

---

## Corpus and the Three-Tree Reconciliation

This wheel ships the `nkilib/core/utils/` infrastructure described above plus a set of `nkilib/experimental/` kernels (`transformer/`, `conv/`, `moe/bwd/`) and the older `neuronxcc/private_nkl/` twin tree — but **not** the full production MoE / flash-attention / cumsum kernel set. Files such as `attention_cte.py`, `cumsum.py`, `output_projection_*.py` and `qkv_tkg.py` are simply absent, which is why the witness lists on this page are short: only a handful of shipped kernels exercise each primitive. The infrastructure file bodies, the address laws, and the 8-enum catalog are all read directly from the shipped source and stand on their own; only the *breadth of use* is limited by what this wheel contains.

Three parallel copies of the utilities ship, consistent with the readable/orchestrator/compiled three-tree story of [the production-kernel inventory](production-kernel-inventory.md):

| Tree | Path | Status |
|---|---|---|
| **Current nkilib** | `nkilib/core/utils/*.py` | the subject of this page; full 8-enum / `min_independent_addr` / `TreeLogger` |
| **`_pre_prod`** | `neuronxcc/nki/_pre_prod_kernels/common_types.py` | reduced enum fork (file B) |
| **`private_nkl`** | `neuronxcc/private_nkl/utils/*.py` | older twin; `StackAllocator.py` = `SbufManager` predecessor (no high-water guard) |

---

## Cross-References

- [6.5.15 — NKI Compiler-Option & Allocation Decorators](option-alloc-decorators.md) — the **compiler-side** `alloc`/`mod_alloc`/`auto_alloc` decorators + `verify_allocation`; contrast with this page's user-space byte-bump allocators.
- [6.6.4 — Production-Kernel Inventory](production-kernel-inventory.md) — the readable-nkilib / `_pre_prod` orchestrator / `_private` compiled three-tree story this infrastructure sits under.
- [Memref / View / Access Model (Concrete Tensors)](memref-view-model.md) — the AP/memref model `TensorView.get_view()` lowers into.
- [BirCodeGenLoop Access-Pattern Builders](bircodegen-ap.md) — where the `(stride, size)` AP-pair vector becomes a BIR `AccessPattern` and then a DMA descriptor.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the `(partition, byte)` hardware rectangle the allocators bump along; why `shape[0]` is excluded from the byte budget.
- [NKI Architecture Overview & the 3-Layer Lowering Stack](architecture-overview.md) — the Python-trace → `penguin.ir` → BIR stack this layer feeds.
- Part 8 — The libwalrus Backend (`walrus/`) — the backend Chaitin-Briggs `ColoringAllocator` that honors manual pins and colors auto-mode symbolic tiles; the contrast in [§ NKI vs Backend Allocator](#nki-allocator-vs-the-backend-coloring-allocator).
- The 6.7.2+ kernels — [flash-attention](attention-cte.md), MoE, RMSNorm, RoPE — all build on the allocator / tiling / view primitives documented here.
