# NVVM Builtin Table Structure

770 builtins mapped to integer IDs (1–770) in a wyhash open-addressing hash table. Dual tables exist: pre-optimization (`sub_90AEE0`) and post-optimization (`sub_126A910`), both with identical content but separate address spaces.

| | |
|---|---|
| **Pre-opt table builder** | `sub_90AEE0` (109KB, populates all 770 entries) |
| **Pre-opt dispatcher** | `sub_913450` (name → ID lookup) |
| **Post-opt table builder** | `sub_126A910` (123KB) |
| **Post-opt dispatcher** | `sub_12731E0` (name → ID lookup) |
| **Hash function** | `sub_CBF760` (wyhash v4 family) |
| **Hash table insert** | `sub_90ADD0` → `sub_C92610` → `sub_C92740` |
| **Hash table find** | `sub_C92860` (find-only, quadratic probing) |
| **Rehash** | `sub_C929D0` (75% load factor trigger) |
| **Total builtins** | 770 (IDs 1–770) |
| **Storage** | Open-addressing at `context+480` (20-byte header) |

## Architecture

```
sub_913450 (public API: name → builtin ID)
  │
  ├─ Guard: context+492 == 0?
  │    └─ sub_90AEE0 (lazy init: populate all 770 entries, once)
  │
  ├─ strlen(name)
  ├─ sub_C92610(name, len)         → compute wyhash
  ├─ sub_C92860(context+480, ...)  → quadratic probe find
  │
  └─ return *(uint32*)(entry + 8)  → the builtin ID
```

## Hash Table Layout

The hash table is a 20-byte struct at `context+480`:

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `bucket_array_ptr` | Pointer to contiguous memory block |
| +8 | 4 | `capacity` | Number of buckets (power of 2) |
| +12 | 4 | `count` | Number of live entries |
| +16 | 4 | `tombstone_count` | Number of tombstone (`-8`) slots |

Contiguous memory block layout (allocated via `calloc(capacity+1, 12)`):

| Region | Size | Content |
|---|---|---|
| `[0 .. 8*cap-1]` | `8*cap` | Bucket array: `cap` qword pointers |
| `[8*cap .. 8*cap+7]` | 8 | Sentinel: value `2` (end-of-table marker) |
| `[8*cap+8 .. +4*cap-1]` | `4*cap` | Hash cache: `uint32` per slot |

Each bucket holds:
- `0` (NULL) — empty, never occupied
- `-8` (`0xFFFFFFFFFFFFFFF8`) — tombstone (deleted)
- pointer to string entry

### String Entry Layout

Heap-allocated via `sub_C7D670(length + 17, 8)`:

| Offset | Size | Field |
|---|---|---|
| +0 | 8 | `string_length` (size_t) |
| +8 | 4 | `builtin_id` (uint32, set after insertion) |
| +16 | N+1 | Null-terminated string data (the builtin name) |

## Hash Function — `sub_CBF760` (wyhash v4)

`sub_C92610` is a thin wrapper that tail-calls `sub_CBF760(data_ptr, length)`. Returns `uint32` (high dword XOR low dword of 64-bit result).

| Length | Strategy | Constants |
|---|---|---|
| 0 | Return `0x2D06800538D394C2` | — |
| 1–3 | First/middle/last byte XOR | Seed `0x87275A9B`, mul `0xC2B2AE3D27D4EB4F` |
| 4–8 | Two uint32 reads | XOR `0xC73AB174C5ECD5A2`, mul `0x9FB21C651E98DF25` |
| 9–16 | Two uint64 reads | XOR `0x6782737BEA4239B9`, `0xAF56BC3B0996523A` |
| 17–128 | Pair-from-ends, 128-bit multiply | Per-pair constants, accumulator |
| 129–240 | `sub_CBF370` (extended mixing) | — |
| >240 | `sub_CBF100` (bulk processing) | — |

Final avalanche: `0x165667919E3779F9 * ((sum) ^ (sum >> 37))`, fold hi32^lo32.

## Quadratic Probing — Triangular Numbers

Both `sub_C92740` (insert-or-find) and `sub_C92860` (find-only) use:

```
slot = hash & (capacity - 1)     // initial probe
step = 1
loop:
  if bucket[slot] matches → return slot
  if bucket[slot] == 0    → return slot (empty)
  if bucket[slot] == -8   → record first tombstone
  slot = (slot + step) & (capacity - 1)
  step++
```

Probe sequence: `h, h+1, h+3, h+6, h+10, h+15, h+21, ...` (triangular numbers `T(k) = k*(k+1)/2`). Guarantees full table coverage for power-of-2 capacity.

Match check is triple-gated: cached hash equality → length equality → `memcmp` content.

### Rehash at 75% Load — `sub_C929D0`

```
if (4 * count > 3 * capacity):       double capacity
elif (free_slots <= capacity/8):      rehash at same capacity (clear tombstones)
else:                                 no rehash
```

For 770 entries: table grows `16 → 32 → 64 → 128 → 256 → 512 → 1024`.

## Complete Builtin ID Inventory

### Synchronization & Compiler Intrinsics (IDs 1–7)

| ID | Name |
|---|---|
| 1 | `__syncthreads` |
| 2 | `__nvvm_bar0` |
| 3 | `__nvvm_membar_cta` |
| 4 | `__nvvm_membar_gl` |
| 5 | `__nvvm_membar_sys` |
| 6 | `__builtin_is_constant_evaluated` |
| 7 | `__builtin_unreachable` |

### Cluster Operations — SM 90+ (IDs 8–14)

| ID | Name |
|---|---|
| 8 | `__nv_clusterDimIsSpecifed_impl` |
| 9 | `__nv_clusterRelativeBlockRank_impl` |
| 10 | `__nv_clusterSizeInBlocks_impl` |
| 11 | `__nv_cluster_barrier_arrive_impl` |
| 12 | `__nv_cluster_barrier_wait_impl` |
| 13 | `__nv_cluster_barrier_arrive_relaxed_impl` |
| 14 | `__nv_threadfence_cluster_impl` |

### Barrier Extensions (IDs 15–20)

| ID | Name |
|---|---|
| 15–17 | `__nvvm_bar0_{popc,and,or}` |
| 18–20 | `__nvvm_bar{_sync_all,rier_sync,_warp_sync}` |

### Bit Manipulation (IDs 21–26)

`__nvvm_clz_{i,ll}`, `__nvvm_popc_{i,ll}`, `__nvvm_brev{32,64}`

### Math — Rounding/Abs/Saturate (IDs 27–56)

`__nvvm_{floor,ceil,abs,fabs,round,trunc,saturate}_{ftz_f,f,d}`, `__nvvm_{ex2,lg2,sin,cos}_approx_{ftz_f,f,d}`

### Reciprocal / Sqrt / Rsqrt (IDs 57–87)

`__nvvm_rcp_{rn,rz,rm,rp}_{ftz_f,f,d}`, `__nvvm_sqrt_{f,rn,rz,rm,rp}_{ftz_f,f,d}`, `__nvvm_rsqrt_approx_{ftz_f,f,d}`

### Type Conversions (IDs 88–184)

97 entries covering all float↔int, double↔int, float↔half, bitcast combinations with all four rounding modes and FTZ variants.

### Address Space & Memory Queries (IDs 185–204)

| ID | Name |
|---|---|
| 185 | `__nv_isGlobal_impl` |
| 186–188 | `__nv_bswap{16,32,64}_impl` |
| 189–192 | `__nv_is{Shared,Constant,Local,GridConstant}_impl` |
| 193–200 | `__nv_cvta_{generic_to,to_generic}_{global,shared,constant,local}_impl` |
| 201 | `__builtin_assume` |
| 202 | `__nv_isClusterShared_impl` |
| 203 | `__nv_cluster_query_shared_rank_impl` |
| 204 | `__nv_associate_access_property_impl` |

### Atomic Operations — Legacy NVVM (IDs 207–275)

69 entries: `__nvvm_atom_{,cta_,sys_}{add,xchg,min,max,inc,dec,and,or,xor}_gen_{i,ll,f,d,ui,ull,128}`

### FP Arithmetic (IDs 276–349)

`__nvvm_{min,max}_{i,ui,ll,ull}`, `__nvvm_f{min,max}_{f,ftz_f,d}`, `__nvvm_mulhi_{i,ui,ll,ull}`, `__nvvm_mul_{rn,rz,rm,rp}_{ftz_f,f,d}`, `__nvvm_div_*`, `__nvvm_add_*`

### Vote Operations (IDs 351–358)

`__nvvm_vote_{all,any,uni,ballot}` + `_sync` variants

### Match Operations (IDs 361–364)

`__match{32,64}_{any,all}_sync`

### FMA (IDs 383–403)

`__nvvm_fma_{rn,rz,rm,rp}_{ftz_f,f,d,ftz_f2,f2}`

### C++11 Atomics (IDs 417–473)

Sized variants: `__nv_atomic_{load,store,fetch_add,fetch_sub,fetch_and,fetch_or,fetch_xor,fetch_max,fetch_min,exchange,compare_exchange}_{1,2,4,8,16}_{u,s,f}`

### Surface Stores — sust (IDs 474–638)

165 entries covering `__nvvm_sust_b_{1d,1d_array,2d,2d_array,3d}_{i8,...,v4i32}_{clamp,trap,zero}`.

Pattern: `sust_b_<dim>_<type>_<oob_mode>` across 5 dimensions × 11 types × 3 OOB modes.

### CUDA Varargs (IDs 639–642)

`__cu_va_{start,end,arg,copy}`

### Tex/Surf Handler (ID 647)

`__nv_tex_surf_handler` — generic dispatch for texture/surface reads (surface *stores* use the dedicated sust builtins above).

### C++ ABI (IDs 648–677)

`__cxa_vec_{ctor,cctor,dtor,new2,new,new3,delete2,delete,delete3}`, `__gen_nvvm_mem{cpy,set}_*`, `_Znw{j,m,y}`, `_Zna{j,m,y}`, `_ZdlPv{,m,y}`, `_ZdaPv{,m,y}`

### WMMA Tensor Core — SM 70+ (IDs 678–707)

30 entries: `__hmma_m{16n16k16,32n8k16,8n32k16}_{ld_a,ld_b,ld_c_f16,ld_c_f32,st_c_f16,st_c_f32,mma_f16f16,mma_f32f16,mma_f16f32,mma_f32f32}`

### Integer/Binary Tensor Core — SM 75+ (IDs 708–745)

38 entries: `__imma_m{16n16k16,32n8k16,8n32k16}_{ld_a,ld_b,ld_c,st_c,mma}_{s8,u8}`, `__imma_m8n8k32_{s4,u4}`, `__bmma_m8n8k128_{b1}`

### Extended Tensor Core — SM 80+ (IDs 746–764)

`__dmma_m8n8k4_mma_f64`, `__mma_tf32_m16n16k8_mma_f32`, `__mma_bf16_m*_mma_f32` + load/store variants

### WGMMA — SM 90+ (IDs 765–768)

`__wgmma_mma_async_{f16,bf16,tf32,f8}`

### Alloca (IDs 769–770)

`_alloca`, `__builtin_alloca`

## Category Summary

| Category | ID Range | Count |
|---|---|---|
| Sync/barriers/cluster | 1–20 | 20 |
| Bit manipulation | 21–26 | 6 |
| Math (floor/ceil/abs/round/etc) | 27–56 | 30 |
| Reciprocal/sqrt/rsqrt | 57–87 | 31 |
| Type conversions | 88–184 | 97 |
| Address space queries/cvta | 185–204 | 20 |
| Atomic ops (NVVM legacy) | 207–275 | 69 |
| FP min/max, mulhi, arithmetic | 276–349 | 74 |
| Vote + match operations | 351–364 | 12 |
| Compare-and-swap | 370–379 | 10 |
| FMA | 383–403 | 21 |
| Shuffle + misc | 404–416 | 13 |
| C++11 atomics (sized) | 417–473 | 57 |
| Surface stores (sust) | 474–638 | 165 |
| CUDA varargs + math shim | 639–646 | 8 |
| Tex/surf handler | 647 | 1 |
| C++ ABI + memgen + new/delete | 648–677 | 30 |
| WMMA tensor core (f16) | 678–707 | 30 |
| IMMA/BMMA tensor core | 708–745 | 38 |
| Extended tensor (dmma/tf32/bf16) | 746–764 | 19 |
| WGMMA (SM 90+ warpgroup) | 765–768 | 4 |
| Alloca | 769–770 | 2 |
| **TOTAL** | | **770** |

## SM Generation Coverage

| Generation | Features Enabled |
|---|---|
| SM 70 (Volta) | WMMA (half-precision tensor core) |
| SM 75 (Turing) | IMMA (integer), BMMA (binary) |
| SM 80 (Ampere) | DMMA (double), TF32, BF16 |
| SM 90 (Hopper) | WGMMA (warpgroup), cluster ops, f8 |

All 770 builtins are registered regardless of target SM. Architecture gating happens in the lowering layer that consumes the builtin IDs.

## Key Observations

- **Lazy initialization**: The entire table is built on first lookup. Guard: `context+492 != 0`.
- **No texture reads (suld)**: Only surface *store* builtins are registered. Texture/surface reads go through `__nv_tex_surf_handler` (ID 647).
- **Write-once table**: Tombstone mechanics exist but deletions never occur for the builtin table.
- **Duplicate prefix optimization**: IDA shows SSE `xmmword` constant loads for long common prefixes (`__nvvm_sust_b_2d_array_*`) — this is compiler optimization of string literal loads, not a different code path.
