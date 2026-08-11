# Index / Gather / Cross-Partition Broadcast Subkernels

> *All symbols and source on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 ship byte-identical `nkilib`). The four subkernels are readable Python under `nkilib/core/subkernels/` and `nkilib/core/utils/`. The ISA primitives they call (`nisa.*`) lower through compiled `.so` handlers (`libBIR.so`, `libwalrus.so`, the per-op `…Info.so`/codegen targets); their public signatures live in the sibling `neuronx_cc_stubs` package's `neuronxcc-stubs/nki/isa/__init__.pyi` (the cp310 wheel itself ships `nki/isa` only as compiled `.cpython-310-*.so`, no `.pyi`). Other wheels differ — treat every path and line number as version-pinned.*

## Abstract

Four small NKI subkernels carry the index/gather/broadcast plumbing that the Part-6 MoE and attention kernels build on. Two are **index producers/consumers**: `find_nonzero_indices` turns a `[T, C]` mask into per-column nonzero token-index lists (the routing decision for Mixture-of-Experts), and `indexed_flatten` scatters expert rows into a flat MoE buffer at dynamic block offsets. Two are **cross-partition broadcasts** that replicate one partition's data onto many: `stream_shuffle_broadcast` (a row `[1,N]→[B,N]` on the Vector/DVE engine) and `tp_broadcast` (a column `[P,1]→[B,P]` on the PE/Tensor engine, with an implicit transpose). All four are pure NKI Python traced through the [3-layer lowering stack](architecture-overview.md) — Python trace → `penguin.ir` → BIR — and emit only standard `nisa` intrinsics; none is a hand-coded ISA blob.

The reimplementation-relevant story is the **data layout**, not the line count. `find_nonzero_indices` is shaped entirely by one hardware fact: the GpSimd `nonzero_with_count` ISA op runs on **8 cores spaced 16 partitions apart** (`{0,16,32,…,112}`), so the kernel scatters 8 columns onto those partitions, PE-transposes so each column's `T` axis lies on a single GpSimd partition's free dimension, runs all 8 cores in parallel, then uses a 32-partition quadrant stream-shuffle to read the odd cores. `indexed_flatten` is shaped by **OOB-skip DMA** plus a hand-rolled two-rank "all-reduce max" (`sendrecv` + elementwise `maximum`, not a collective). The two broadcasts are the cheap intra-core replication primitives that the router and MLP toggle between based on which engine (DVE vs PE) is free.

> **GOTCHA — `tp_broadcast` is a *transpose*-broadcast, not a tensor-parallel one.** The name invites the cross-core reading, but the body (`nkilib/core/utils/tp_broadcast.py:60`) is a single `nisa.nc_transpose` of one source column with a stride-0 "repeated input access" view, strictly within one NeuronCore's 128 partitions, on the PE engine. Cross-core / TP broadcast lives in the collective layer (`nisa.sendrecv`, `InstCollectiveCompute`; see [NeuronCodegen Collectives](neuroncodegen-collectives.md)), never in `tp_broadcast.py`. Wiring this helper to a multi-core distribution will not work.

For reimplementation, the contract is:

- The GpSimd 8-core / 16-partition layout that dictates `find_nonzero_indices`'s scatter → transpose → shuffle pipeline, and the `[idx…,-1…,count]` output format of `nonzero_with_count`.
- The dynamic-offset append-DMA (`scalar_offset` + `indirect_dim`) that packs per-column indices to the front of the output, and the running write-cursor accumulation.
- `indexed_flatten`'s OOB-skip scatter and the `sendrecv` + `maximum` two-rank merge — and why `-1` padding makes that merge a correct union.
- The DVE-row vs PE-column broadcast distinction, the `[0]*32` shuffle mask, and the stride-0 transpose view.

| | |
|---|---|
| **`find_nonzero_indices`** | `nkilib/core/subkernels/find_nonzero_indices.py` (358 lines) — multi-column, LNC2, GpSimd-8-core |
| **`indexed_flatten`** | `nkilib/core/subkernels/indexed_flatten.py` (222 lines) — MoE blockwise scatter |
| **`stream_shuffle_broadcast`** | `nkilib/core/utils/stream_shuffle_broadcast.py` (43 lines) — DVE row broadcast |
| **`tp_broadcast`** | `nkilib/core/utils/tp_broadcast.py` (64 lines) — PE transpose-broadcast |
| **Key ISA ops** | `nisa.nonzero_with_count` (Pool/GpSimd, gen3+), `nc_stream_shuffle` (DVE), `nc_transpose`/`nc_matmul` (PE), `sendrecv` (DMA/sync) |
| **Torch refs** | `find_nonzero_indices_torch.py`, `indexed_flatten_torch.py` (golden references, same dir) |

---

## find_nonzero_indices — nonzero T-indices per column

### Purpose

For an input mask `[T, C]`, find for each column `c` the `T`-indices where `input[t,c] != 0`, packed to the front and `-1`-padded; emit also a per-column count. This is the MoE routing primitive: a column is one expert's affinity vector over `T` tokens, and the output is the routed-token list plus how many tokens routed. The golden reference (`find_nonzero_indices_torch.py:52-62`) is literally `torch.full((C,T), -1)` followed by a per-column `torch.nonzero` with the count written into `nonzero_counts` — the kernel reproduces this on hardware.

### Signature

```c
// find_nonzero_indices.py:33-40 — @nki.jit
find_nonzero_indices(input_tensor[T,C],
                     col_start_id = None,   // [1] HBM: dynamic column base; if set, only n_cols cols
                     n_cols       = None,
                     chunk_size   = None,   // splits T to bound SBUF; defaults to T_DIM
                     index_dtype  = nl.int32)
    -> (indices[C,T] or [n_cols,T],   // per column: nonzero T-indices, front-packed, -1 padded
        nonzero_counts[C] or [n_cols])
```

### The GpSimd hardware constraint

Everything below is dictated by one fact about the `nonzero_with_count` ISA op (constants at `find_nonzero_indices.py:25-30`):

```text
_QUADRANT_SIZE           = 32   // 128 partitions / 4 quadrants
_NUM_QUADRANTS           = 4
_NUM_GPSIMD_CORES        = 8    // 8 GpSimd cores process in parallel
_GPSIMD_CORES_PER_QUADRANT = 2
_PARTITIONS_PER_GPSIMD   = 16   // cores live on partitions 0,16,32,...,112
```

`nonzero_with_count` scans the **free axis** of each *active* partition and emits packed indices along that free axis. It only runs on the 8 GpSimd cores, which sit one per 16-partition stride: `{0,16,32,48,64,80,96,112}`, two per 32-partition quadrant. So the kernel processes **8 columns per round** — one column per core — and there are `n_column_rounds = ceil(C_per_shard / 8)` rounds (`line 134`).

> **QUIRK —** the op operates on partitions `{0,16,…,112}`, but it writes its results back onto those *same* partitions. The even cores `{0,32,64,96}` and odd cores `{16,48,80,112}` therefore share quadrants pairwise. Reading both out requires a quadrant-local stream-shuffle to slide the odd-core data from partition 16 down to partition 0 within each quadrant (step f below). This is why the store loop runs twice with a shuffle between.

### Why a PE transpose precedes nonzero_with_count

`find_nonzero` must find nonzeros along `T` (the token axis) **per column** `c`. The DMA load (`line 168-186`) lands `T` on partitions and `C` on the free axis — the opposite of what the op wants. Two moves reorient it so that, for one column, the full `T` axis lies on the free dimension of one GpSimd partition. [INFERRED] The reorientation purpose is read from the data layout plus the ISA's free-axis scan:

1. **Scatter** (`line 189-194`): `tensor_copy` column `j` → partition `j*16` of `input_gpsimd_aligned_sbuf` (one column onto each of the 8 GpSimd cores), on the scalar engine.
2. **Transpose** (`line 197-208`): per `T`-tile, `nc_matmul(is_transpose=True)` against a `shared_identity_matrix(128)` → PSUM, then `tensor_copy` to `…_transposed_sbuf`. Now each GpSimd partition's free axis carries its column's `T` values.

`nonzero_with_count` then runs once across all 8 cores in parallel.

### Algorithm

```c
function find_nonzero_indices(input[T,C], col_start_id, n_cols, chunk_size):   // line 33
    // --- shard setup (LNC2): split C across 2 NeuronCores ---  line 101-104
    C            = n_cols if col_start_id else C                  // dynamic-column subset
    shard_id     = program_id(0); num_shards = num_programs(0)
    C_per_shard  = C // num_shards;  C_offset = C_per_shard * shard_id
    CHUNK_T_TILES = chunk_size // 128;  NUM_CHUNKS = T // chunk_size

    indices        = shared_hbm[C, T]                            // line 118
    nonzero_counts = shared_hbm[C]
    if NUM_CHUNKS > 1:                                           // line 121-127
        memset(sbuf_init, -1); dma sbuf_init -> indices shard slice  // pre-pad tail
    identity_sb = shared_identity_matrix(128)                    // line 137

    for round in range(ceil(C_per_shard / 8)):                   // 8 columns per round  line 139
        n_cols_round = min(8, C_per_shard - 8*round)
        offsets[1,8] = 0                                         // per-column write cursors  line 147
        for chunk in range(NUM_CHUNKS):                          // line 148
            // (a) LOAD: one DMA of up to 8 cols x CHUNK_T_TILES T-tiles  line 168-186
            input_sbuf[128, CHUNK_T_TILES, 8] = dma(input.ap(
                pattern=[[C,128],[C*128,CHUNK_T_TILES],[1,n_cols_round]],
                offset = round*8 + C_offset + chunk*chunk_size*C,
                // col_start_id given => scalar_offset, indirect_dim=1, dge_mode=hwdge
            ))
            // (b) SCATTER cols onto GpSimd partitions {0,16,...,112}   line 189-194
            for j in range(n_cols_round):
                tensor_copy(aligned[:, :, j*16], input_sbuf[:, :, j], engine=scalar)
            // (c) PE TRANSPOSE per T-tile: reorient T onto the free axis  line 197-208
            for t in range(CHUNK_T_TILES):
                psum = nc_matmul(stationary=aligned[:, t, :], moving=identity_sb,
                                 is_transpose=True)
                tensor_copy(transposed[:, t, :], psum)
            // (d) the GpSimd op: 8 cores in parallel  line 211-216
            nonzero_with_count(dst=indices_sbuf[128, 1, chunk_size+1], src=transposed,
                               index_offset = chunk*chunk_size, padding_val = -1)
            //     per active partition: [idx0, idx1, ..., -1, ..., COUNT]   COUNT in slot chunk_size
            // (e) STORE EVEN cores: quadrant q -> partition q*32 = {0,32,64,96}  line 219-234
            for q in range(4): _store_indices_and_count(col = q*2, read_partition = q*32)
            // (f) SHUFFLE odd->even within each quadrant: out-part0 <- in-part16  line 237-238
            quad_mask = [16] + [255]*31           // 255 = leave unmodified
            nc_stream_shuffle(dst=indices_sbuf, src=indices_sbuf, shuffle_mask=quad_mask)
            // (g) STORE ODD cores: now their data sits at q*32   line 241-256
            for q in range(4): _store_indices_and_count(col = q*2+1, read_partition = q*32)
        tensor_copy(nonzero_counts_local[round*8 : +n_cols_round], offsets[0:n_cols_round])  // line 259
    dma(nonzero_counts.reshape(1,C)[C_offset : +C_per_shard], nonzero_counts_local)          // line 267
    return indices, nonzero_counts
```

`_store_indices_and_count` (`line 273-357`) is the per-core extractor. It is a **dynamic-offset append-DMA**: it reads the running cursor `offsets[col]`, writes this chunk's `chunk_size` index values to `indices` at `offset = out_col*T_DIM` with `scalar_offset = offset_tile, indirect_dim=1` (so the write starts at the cursor, packing chunk results contiguously), then reads the COUNT slot and `offsets[col] += count` (`tensor_tensor` add) to advance the cursor for the next chunk.

```c
function _store_indices_and_count(col, read_partition=q*32):   // line 273
    if col >= n_cols_round: return                              // line 308 — inactive core guard
    offset_tile  = offsets[col]                                 // current write cursor  line 317
    out_col      = C_offset + round*8 + col
    src_data[1,chunk_size] = indices_sbuf[read_partition, 0, 0:chunk_size]   // the index list  line 326
    dma(indices.ap(pattern=[[T,1],[1,chunk_size]], offset=out_col*T,
                   scalar_offset=offset_tile, indirect_dim=1),  src_data)    // append  line 330-338
    count = indices_sbuf[read_partition, 0, chunk_size:chunk_size+1]         // COUNT slot  line 346
    offsets[col] = offsets[col] + count                                      // advance cursor  line 352
```

> **GOTCHA —** the chunk loop's correctness depends on the pre-pad at `line 121-127`. When `NUM_CHUNKS > 1` the kernel only ever *appends* real indices up to each column's count, so the tail of `indices[c]` is never written by the chunk loop. If the HBM tile is not memset to `-1` first, that tail holds garbage instead of padding. With `NUM_CHUNKS == 1` the single chunk writes `chunk_size == T` per column and the front-packing leaves the rest as whatever `nonzero_with_count` wrote (its own `-1` pad), so the pre-pad is skipped.

### nonzero_with_count contract

`nisa.nonzero_with_count(dst, src, index_offset, padding_val)` is **not** in the public stub — it is a private op (call sites only, e.g. `find_nonzero_indices.py:211`). Its metadata ships in a dedicated `neuronxcc/include/isa/nonzero_with_count_info.cpython-310-*.so`, whose strings name the op `ISAInstructionInfo for s3d3_nonzero_with_count_struct - NonzeroWithCount`, `Dtype.INT32`, and "Find indices of nonzero elements … using GpSimd Engine" — pinning the GpSimd/Pool binding and the int32 output at the binary level. It lowers to a BIR `NonzeroWithCount` op (a dedicated codegen target `…/penguin/targets/generated/NonzeroWithCount.so` exports `.padding_val` / `.index_offset` / `.verify` / `.serialize`; the opcode is in `birpy/Opcodes.so` and present in `libBIR.so`, `libBIRSimulator.so`, `BirCodeGenLoopGen.so`). The two `int32` immediate args are range-checked in `libwalrus.so` by the literal verifier strings `indexOffset must be int32` and `paddingVal must be int32` (present in `libwalrus.so` only). The engine binding is Pool/GpSimd, **gen3+** (arch 30/40/50) — see the arch-gating limit noted in the evidence section. Output per active partition is int32 `[idx…, -1…, count]` with `count` in the last slot — the single-column benchmark helper documents this format directly (`find_nonzero_indices_with_count.py:45-54`: `[idx1, idx2, ..., -1, -1, ..., count]`, "Count is stored in the last position (index T)"). See [DVE Search & Datamove Encoding](../isa/dve-search-encoding.md) for the Nonzero datapath.

> **GOTCHA — two files call `nonzero_with_count`; only one is the router.** The canonical routing kernel is `find_nonzero_indices.py` (multi-column, LNC2, 8-core GpSimd). `find_nonzero_indices_with_count.py` under `experimental/benchmark/` is a *separate* single-column `[1,T]→[1,T+1]` helper, still carrying `TODO: Add pseudocode` / `TODO: Handle E>1` markers (`line 55-60`). Both call `nisa.nonzero_with_count(padding_val=-1)`; only the production kernel adds the 8-column scatter, PE transpose, quadrant shuffle, and append-DMA.

---

## indexed_flatten — scatter expert rows by dynamic block offset

### Purpose

Place each input row's `f_len`-sized blocks into a flat output buffer at dynamic positions given by `row_offsets`. For MoE blockwise-matmul: `input[E,T]` is `E` experts' packed token data; `row_offsets[e]` is the block index where expert `e`'s output belongs in the flat buffer. Out-of-bounds offsets are **skipped** — this is a sparse scatter, not a dense gather. The golden reference (`indexed_flatten_torch.py:56-65`) reshapes to `[E, T//f_len, f_len]` and writes `output_blocks[row_offsets[e]+p] = input[e,p]` guarded by `0 <= out_block_idx < num_output_blocks`.

> **NOTE —** despite "flatten" and the gather-adjacent framing, this is a **scatter-by-block-offset**, not an `output[i] = input[index[i]]` element gather. It is the index-driven *data-placement* primitive that pairs with `find_nonzero`'s routing output: nonzero produces the routed-token list and count; gather pulls those tokens; `indexed_flatten` places expert rows at their dynamic block positions in the flat MoE buffer (docstring: "Indexed flatten kernel for MoE blockwise matmul operations", `line 15`).

### Signature

```c
// indexed_flatten.py:27-35 — @nki.jit
indexed_flatten(input_tensor[E,T], f_len, output_len,
                row_offsets[N],              // [N] HBM: block offset per row
                row_offsets_start = None,    // optional sub-range base into row_offsets
                padding_val = -1)
    -> flattened_array[output_len]           // shared_hbm
```

Constraints (`kernel_assert`, `line 93-103`): `output_len % 128 == 0`; `output_len % f_len == 0`; `T % f_len == 0`; `(T//f_len) % 16 == 0` (a DMA requirement). LNC2 only. `row_offsets_start is None ⇒ N == E`; else `N >= E`. Best perf at `T <= 10240`/row.

The dynamic-DMA knobs come from `dma_copy` (public stub `__init__.pyi:414`): `oob_mode.skip = 1` (stub `line 81`, "skip out-of-bounds writes" `434`) and `dge_mode.hwdge = 2` (`line 14`, "HWDGE … NeuronCore-v3+" `437`) are public; `indirect_dim` and `scalar_offset` are **private** (not in the public stub — `scalar_offset` is named in `libwalrus.so`: "tensor_copy only supports scalar_offset for dynamic access, not vector_offset").

### Algorithm

```c
function indexed_flatten(input[E,T], f_len, output_len, row_offsets[N]):   // line 27
    partitions_per_row = T // f_len;  num_output_blocks = output_len // f_len
    // --- LNC2 shard, odd-E aware ---  line 113-116
    E_per_shard = ceil(E/2) if shard_id==0 else floor(E/2)     // shard 0 gets the extra row
    E_offset    = 0 if shard_id==0 else ceil(E/2)
    max_E_per_shard = ceil(E/2)                                // both NCs iterate this many times

    partial = private_hbm[num_output_blocks, f_len]            // PER-NC private buffer  line 123
    memset(sbuf_init, padding_val); dma -> partial            // init to -1  line 135-137

    // --- load this shard's offsets; invalid rows => OOB => all blocks skipped ---  line 149-168
    INVALID_OFFSET_VALUE = -1000000
    row_offsets_local[1, max_E_per_shard] = INVALID_OFFSET_VALUE
    dma(row_offsets_local[0:E_per_shard], row_offsets shard slice)   // scalar_offset if start given

    for row_local in sequential_range(max_E_per_shard):       // line 170
        row_offset_sb = row_offsets_local[row_local]
        row_idx       = min(row_local + E_offset, E - 1)       // clamp padded iterations  line 175
        for ptile in sequential_range(partition_tile_count):   // tiles of 128 partitions  line 177
            cnt = min(128, partitions_per_row - ptile*128)
            if cnt > 0:
                input_tile = dma(input.reshape(E, partitions_per_row, f_len)
                                    [row_idx, ptile*128 : +cnt, 0:f_len])         // line 183
                // THE DYNAMIC SCATTER, OOB-skip:
                dma(partial.ap(pattern=[[f_len,cnt],[1,f_len]], offset=ptile*128*f_len,
                               scalar_offset=row_offset_sb, indirect_dim=0),
                    input_tile,  oob_mode = nisa.oob_mode.skip)                    // line 189-198

    // --- hand-rolled all-reduce-max over the 2 ranks ---  line 200-219
    local  = dma(partial.reshape(128, output_len//128))                            // reload
    remote = sendrecv(local, send_to_rank=1-shard_id, recv_from_rank=1-shard_id, pipe_id=0)
    merged = tensor_tensor(local, remote, op=maximum)         // -1 loses to any real write
    if shard_id == 0: dma(flattened_array, merged)            // shard 0 commits
    return flattened_array
```

> **NOTE — the "all-reduce max" here is hand-rolled.** each NC scatters into its *own* `private_hbm` partial, then a single `nisa.sendrecv` point-to-point exchanges the other rank's partial and `tensor_tensor(op=maximum)` merges. Because both partials are initialized to `padding_val = -1` and the two NCs write *disjoint* blocks (each owns half the experts), the elementwise max is exactly the union of writes — any real datum beats `-1`. `sendrecv` is **private** (not in the public stub) — defined in `neuronxcc/nki/_private/private_api.so` with params `(src, dst, send_to_rank, recv_from_rank, pipe_id)`, where `pipe_id` "matches sendrecv message pairs from different NCs" (from the `.so` docstrings). It lowers via a `LowerToSendRecv` transform / `SundaISAInst` path in `libwalrus.so`, **not** the `CollectiveKind::AllReduce` / `InstCollectiveCompute` codegen. See [NeuronCodegen Collectives](neuroncodegen-collectives.md) for the genuine collective lowering.

> **QUIRK —** the inner loop iterates `max_E_per_shard` (= the *larger* shard's row count) on **both** NeuronCores, padding the smaller shard. The padding iterations are made harmless two ways: `row_idx = min(row_local + E_offset, E-1)` clamps the input read to a valid row, and `row_offsets_local` is pre-filled with `INVALID_OFFSET_VALUE = -1000000` so every block of a padded row lands OOB and is skipped. Balanced loop bounds keep the two NCs in lockstep for the `sendrecv`.

---

## stream_shuffle_broadcast — DVE row broadcast [1,N]→[B,N]

### Purpose

Replicate partition 0's row across the first `B` partitions of `dst`, on the Vector/DVE engine. The cheap broadcast when the free dimension `N` is small. Docstring (`stream_shuffle_broadcast.py:22`): "Broadcasts the first partition of src onto the partition dim of dst."

### Algorithm

```c
function stream_shuffle_broadcast(src[1,N], dst[B,N]):        // line 21
    assert src.shape[1] == dst.shape[1]                        // matching free dim  line 33
    shuffle_mask = [0]*32                                      // every out-part <- quadrant part 0
    for i in range(ceil(B / 32)):                              // stride quadrants 0,32,64,96  line 36
        cur = min(32, B - i*32)
        nc_stream_shuffle(src = src[0:1, :],
                          dst = dst[i*32 : i*32+cur, 0:N],
                          shuffle_mask = shuffle_mask)
```

### Mechanism

`nc_stream_shuffle` (public stub `__init__.pyi:1008`) is cross-partition data movement **within a 32-partition quadrant** on the Vector engine. `shuffle_mask` is 32 elements; `shuffle_mask[i]` names which quadrant-local input partition (`0..31`) output partition `i` copies from (stub `1015-1017`); the special value `255` leaves that output partition unmodified. The same mask applies to every quadrant independently. The stub also fixes the active-partition rounding: `num_active_partitions = ceil(max(src,dst)/32)*32` (`line 1030`) with start-partition rules at `1034-1038` — consistent with the quadrant-aligned loop here. Here the mask is `[0]*32`, so **every** output partition copies from quadrant-local partition 0; the `src[0:1,:]` view supplies partition 0. The loop strides the quadrant base (`0,32,64,96`), replicating the source row to all `B` partitions. The same `255`-means-unmodified convention is used by `find_nonzero`'s `quad_mask = [16]+[255]*31`. The op is realized by `StreamShuffleInstGen` and `LowerBroadcast` in `libwalrus.so`, both present as strings there.

> **QUIRK —** the in-place call `stream_shuffle_broadcast(x, x)` is the common idiom — confirmed at `topk_reduce.py:101` (`stream_shuffle_broadcast(global_token_indices_sb, global_token_indices_sb)`). It broadcasts partition 0 of `x` over `x` itself; safe because `nc_stream_shuffle` reads the source view (partition 0) and writes the destination partitions in one engine pass.

Cost is `max(MIN_II≈64, N)` Vector-engine cycles (`N` = elems/partition) — flat in `B`, so cheapest when `N` is small — from the `nc_stream_shuffle` stub's cost line.

---

## tp_broadcast — PE transpose-broadcast [P,1]→[B,P]

### Purpose

Broadcast a single source **column** `src[:, src_offset]` (shape `[P,1]`) onto every partition of `dst[B,P]`, with an implicit transpose, on the PE/Tensor engine. Each of the `B` destination partitions becomes the transposed source column. Docstring (`tp_broadcast.py:16-17, 32-34`): "moving a column tensor into every partition of a destination tensor … Uses a single transpose instruction (on PE) with repeated input access to broadcast src to dst."

### Algorithm

```c
function tp_broadcast(src[P,F], dst[B,P], src_offset, psum_address=None):   // line 28
    p_dim          = src.shape[0]                              // P
    broadcast_dim  = dst.shape[0]   // B ;   tp_dim = dst.shape[1] // P
    assert tp_dim == p_dim                                     // line 55
    tp_psum = psum[B, P] @ psum_address                        // line 58
    // one PE transpose of the src column, broadcast B times via a stride-0 view:
    nc_transpose(tp_psum,
                 src.slice(1, src_offset, src_offset+1)        // [P,1] — pick column src_offset
                    .broadcast(1, broadcast_dim))              // -> [P,B] with stride 0  line 60
    tensor_copy(dst, src=tp_psum)                              // PSUM -> SBUF  line 63
```

### Mechanism — the stride-0 "repeated input access"

`TensorView.broadcast` sets a **stride of 0** in the broadcast dimension: the same column is read `B` times. One PE `nc_transpose` then writes `[B,P]` to PSUM, and `tensor_copy` drains PSUM→SBUF. The older `private_nkl/utils/tp_broadcast.py` variant writes the stride-0 access pattern *literally* — `src.ap([[f_dim, p_dim], [0, broadcast_dim]], offset=src_offset)` (`line 32`), with the comment "Use repeated access to broadcast." The literal `[0, broadcast_dim]` (stride 0, count `broadcast_dim`) is direct evidence of the mechanism: a `[P,1]` column repeated `B` times, transposed in one PE pass. Two shipped source variants agree on it.

> **NOTE — three source copies of each broadcast helper exist, diverging in surface but not semantics.** `nkilib/core/utils/*` is canonical (`kernel_assert`, `TensorView.slice/.broadcast`). `neuronxcc/private_nkl/utils/*` is older (plain `assert`, literal stride-0 AP, hardcoded `psum address=(0,0)`, no `psum_address` arg, and a `WARNING: always broadcasts src[0:1]` due to a nested-indexing limitation). `neuronxcc/nki/_pre_prod_kernels/*` uses `nl.static_range` + `nl.mgrid` indexing for the stream-shuffle variant. All are mask-`[0]*32` / same-transpose equivalent; cite the canonical `core/utils/*` and treat the others as corroboration.

### stream_shuffle vs tp_broadcast — picking the engine

Both are intra-core cross-partition broadcasts; they differ in axis, engine, and cost.

| Axis | `stream_shuffle_broadcast` | `tp_broadcast` |
|---|---|---|
| What replicates | a partition-0 **row** `[1,N]` | a source **column** `[P,1]` |
| Result | `[B,N]` (no transpose) | `[B,P]` (with transpose) |
| Engine | Vector / DVE | PE / Tensor (+ a copy) |
| Mechanism | quadrant stream-shuffle, mask `[0]*32` | one `nc_transpose`, stride-0 view |
| Resource | DVE cycles `max(64,N)` | a PSUM bank + PE pass |
| Pick when | `N` small, DVE free | a transpose is needed anyway, or PE free |

A **third** intra-core broadcast also exists: `nc_matmul` with a ones-stationary (`is_stationary_onezero=True`) — also on PE. The router and MLP toggle between the DVE path (`stream_shuffle_broadcast`) and a PE path (`tp_broadcast` or matmul-ones) based on engine availability: `_pre_prod_kernels/moe_token_gen.py` carries the `use_PE_broadcast_w_bias` switch, and the MLP projection path carries `use_stream_shuffle_broadcast`.

> **NOTE —** all three are **intra-core**: within one NeuronCore's 128 partitions. None crosses cores. Cross-core broadcast is the collective layer (`nisa.sendrecv` / `InstCollectiveCompute`). See the `tp_broadcast` gotcha in the Abstract.

---

## How these feed the router / MoE

The four subkernels compose into the MoE routing pipeline (`nkilib/core/moe/moe_tkg/all_expert_mx_impl.py`). Per expert (`_find_routed_tokens`, ≈`line 380-429`):

```c
// for each expert_idx:
src = affinities.select(dim=1, index=expert_idx)              // pick this expert's column  line 390
dma_transpose(dst=[1,T], src)                                  // -> [1,T] (cast to f32 if needed)  line 396
nonzero_with_count(src=[1,T], dst=routed_indices_with_count[pmax, T+1],
                   padding_val = -1)                           // partition 0 = [idx..., -1..., count]  line 405
count = routed_indices_with_count[0, T]                        // the COUNT slot
iota(boundaries)                                               // block boundaries  line 417
dynamic_conditions = tensor_scalar(boundaries, count, op=less) // [1,1,1,0,...] which blocks to run  line 422
```

The count drives **dynamic block skipping**: `dynamic_conditions` (e.g. `[1,1,1,0]` for `count=483`, boundaries `[129,257,385,513]`) decides at runtime which expert blocks actually have tokens, so empty blocks are skipped. The routed indices then feed gather/scatter; `indexed_flatten` is the analogous index-driven block placement for the blockwise-matmul MoE path. The `nonzero_with_count` padding of `-1` is chosen specifically to feed an OOB-skip DMA gather (the same `oob_mode.skip` mechanism `indexed_flatten` uses) — `-1` indices are silently dropped.

The pipeline docstring (`all_expert_mx_impl.py:109-125`) lays it out: `nonzero_with_count(expert_affinities[e])` → static blocks `gather → expert_mlp → scatter`, then `while dynamic_decision[i]: gather → expert_mlp → scatter` (the dynamic blocks, skipped if empty). See [MoE Routing: router_topk](router-topk.md) for the router scatter that consumes these indices.

### nisa primitive → engine map

| Primitive | Engine | Confidence | Source |
|---|---|---|---|
| `nonzero_with_count` | Pool / GpSimd (gen3+) | HIGH | `nonzero_with_count_info.so` strings |
| `nc_matmul`, `nc_transpose` | PE / Tensor | CERTAIN | public stub |
| `nc_stream_shuffle` | Vector / DVE (32-part quadrants) | CERTAIN | public stub |
| `tensor_copy`, `tensor_tensor`, `tensor_scalar`, `iota`, `memset` | Vector / Scalar / Act (`engine=` arg) | CERTAIN | `engine=` argument |
| `dma_copy`, `dma_transpose`, `sendrecv` | DMA / sync | CERTAIN | public stub / private API |

So `find_nonzero` touches Pool + PE + Vector + DMA; `indexed_flatten` touches DMA + Vector + sync (`sendrecv`); `stream_shuffle_broadcast` is pure Vector; `tp_broadcast` is pure PE (+ a copy).

---

## Evidence anchors and limits

| Claim | Anchor |
|---|---|
| `tp_broadcast` is intra-core | `tp_broadcast.py:60` is one `nisa.nc_transpose` of `src.slice(...).broadcast(...)` plus a `tensor_copy` PSUM→SBUF (`line 63`). No `sendrecv`, no rank argument, no `program_id`, no collective op anywhere in the 64-line file; the docstring says "moving a column tensor into every **partition**" |
| GpSimd cores sit on partitions `{0,16,…,112}` | the named constants (`line 25-30`: `_PARTITIONS_PER_GPSIMD = 16`, `_NUM_GPSIMD_CORES = 8`), the scatter writing column `j` to partition `j*16` (`line 191`), and the store reading partition `q*32` with quadrant shuffle `[16]+[255]*31` (`line 237`) to recover the odd cores at `+16` — the scatter/shuffle strides depend on the geometry directly |
| `nonzero_with_count` output is `[idx…, -1…, count]`, count last | `find_nonzero` reads the count from slot `chunk_size` (`line 346-350`) and indices from `0:chunk_size` (`line 326-329`); the benchmark docstring states the format explicitly (`find_nonzero_indices_with_count.py:45-54`) |
| `indexed_flatten`'s merge is `sendrecv` + `maximum` | `line 206-215`: `nisa.sendrecv(send_to_rank=1-shard_id, recv_from_rank=1-shard_id, pipe_id=0)` then `tensor_tensor(op=nl.maximum)`. No `CollectiveKind`, no `InstCollectiveCompute`; the `-1`-init + disjoint-write + max = union argument holds because each shard owns half the experts (`E_offset`) |
| `stream_shuffle_broadcast` replicates partition 0 | `line 35` sets `shuffle_mask = [0]*32`, `line 39` passes `src=src[0:1,:]`, and the quadrant loop (`line 36`) strides `i*32` |
| `find_nonzero → NonzeroWithCount` at the binary level | `nonzero_with_count_info.so` carries `… - NonzeroWithCount` + `Dtype.INT32` + "using GpSimd Engine"; codegen target `NonzeroWithCount.so` exports `.padding_val`/`.index_offset`; the `indexOffset`/`paddingVal must be int32` verifiers are in `libwalrus.so` |

Limits:

- **[INFERRED] the arch gating** (gen3+, arch 30/40/50) for `nonzero_with_count`, which rests on `libBIR.so` analysis owned elsewhere rather than being re-derived here.
- The `nc_stream_shuffle` quadrant semantics, the active-partition rounding, and the cost model come from the `neuronx_cc_stubs` public `.pyi` stub (lines 1008-1044), not a microcode trace; the on-device DVE datapath is owned by [Part 2](../isa/dve-search-encoding.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `nonzero_with_count` (Pool/GpSimd ISA) | The op `find_nonzero_indices` is built around; [DVE Search & Datamove](../isa/dve-search-encoding.md) |
| `nc_stream_shuffle` (DVE ISA) | Drives both `stream_shuffle_broadcast` and `find_nonzero`'s quadrant shuffle |
| `nc_transpose` / `nc_matmul` (PE ISA) | `tp_broadcast`'s transpose and `find_nonzero`'s reorientation |
| `sendrecv` / collectives | `indexed_flatten`'s hand-rolled 2-rank merge vs the real collective layer |
| MoE `all_expert_mx` | The consumer: per-expert affinity → nonzero → gather/scatter |

## Cross-References

- [NKI Architecture Overview & the 3-Layer Lowering Stack](architecture-overview.md) — how these `@nki.jit` kernels trace to penguin.ir then BIR
- [MoE Routing: router_topk](router-topk.md) — the router scatter that consumes nonzero's routed-token indices
- [DVE Search & Datamove Encoding — Nonzero](../isa/dve-search-encoding.md) — the Nonzero datapath behind `nonzero_with_count`
- [nki.isa REDUCE / SELECT / DVE / MEMORY / DMA Intrinsics](isa-reduce-dve-dma.md) — the `nisa.*` intrinsic surface and validators
- [NeuronCodegen Collective Forward Builders](neuroncodegen-collectives.md) — the genuine cross-core collective layer (contrast with `tp_broadcast`/`sendrecv`)
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition SBUF and PSUM banks these subkernels address
