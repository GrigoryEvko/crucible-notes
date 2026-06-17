# Ring Scheduling Math

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. The composer source TU is `/opt/workspace/KaenaRuntime/enc/enc.cc` and `enc/enc_primitive.cc`/`enc_primitive.h`; the device-resident driver is `tdrv/encd.c`. `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (decompile- and DWARF-anchored)** — the step schedule is read line-by-line from `__compose_allreduce_channel @0x171600`; the per-channel dispatch from `__compose_channel_ring @0x177340` (op-type switch); every struct offset is verbatim DWARF; the rank/neighbor math from `nccl_setup_alg_ring_info @0x1005c0`. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

A NeuronCore runs a ring all-reduce the textbook NCCL way — `(nranks-1)` reduce-scatter steps followed by `(nranks-1)` all-gather steps over a logical ring of ranks — but it does **not** run that loop on a host CPU. libnrt *compiles* the entire ring step sequence into an on-device program at model-load time: a per-channel list of `cc_op_entry` collective-op steps plus the SDMA descriptors that move and reduce each chunk, which the NeuronCore's sync core then walks with no host in the loop. This page owns the **device-side** ring step loop — the math that decides, for each of the up-to-32 ring channels, which chunk every rank pushes to its successor on every step, and which `enc_primitive` leaf emits the wire artifact for that step. The schedule engine is the C++ class `enc_metaring_primitive` (`compose_operation @0x178170`); the per-channel step composer for all-reduce is `__compose_allreduce_channel @0x171600`.

The reference frame is the standard NCCL ring. In NCCL an all-reduce of `S` bytes over `N` ranks splits `S` into `N` chunks; each rank owns one chunk position and, on step `i` of the reduce-scatter phase, receives the partial for chunk `(rank − i)` from its predecessor, adds its own contribution, and forwards the running sum to its successor — so after `N−1` steps each rank holds the fully reduced value for one distinct chunk. The all-gather phase then rotates those `N` reduced chunks around the ring in another `N−1` steps until every rank holds every chunk. libnrt reproduces this exactly, with two Neuron-specific refinements: every chunk is **half-chunk pipelined** (each step issues two ops, `CHUNK_H0` then `CHUNK_H1`, from the `enc_half_chunk_index` enum, so the two halves of a chunk are in flight on adjacent links simultaneously), and the in-flight reduction is performed *inside the SDMA engine* — the `recv_reduce_send` step carries an `SDMA_CCETYPE` reduce opcode (`ADD`/`FMA`/`MAX`/`MIN`), so the partial sum is accumulated by the DMA hardware as it lands, never touching a compute kernel.

The boundary this page draws is between the two libraries. libnccom (the NCCL math fork, reached over a `dlsym`'d `neuron*` ABI) builds only the host-side ring **order** — the prev/next/userRanks wiring it decides from the logical topology. It does **not** contain the device step loop, the half-chunk pipeline, the chunk-size math, or any `enc_primitive::send`/`recv_reduce_send`/`direct_recv_send` leaf. libnrt **re-derives** the device neighbor order from the physical MLA NeuronLink topology into `enc_alg_metaring.ring_ranks[32]` (`nccl_setup_alg_ring_info @0x1005c0`), sizes the chunks (`__set_dynamic_chunk_size @0x14bba0`), and emits the step program. The page documents (1) the `enc_alg_metaring` schedule container and its `enc_ring` per-channel payload, (2) the chunk-group / half-chunk indexing math, (3) the per-channel op-type dispatch, and (4) the two-phase all-reduce step schedule as C pseudocode pinned to the `enc_primitive` leaf each step calls.

For reimplementation, the contract is:

- **The schedule container** — `enc_alg_metaring` (37544 B) holds `channel_n` ring channels, each with an `enc_ring{prev,next,user_ranks,duplicate}` payload that *is* the device traversal order. The composer reads `ring_ranks[ch].prev/next` to build the per-channel `node`/`node_next` endpoints; a `prev == -1` channel is emitted empty.
- **The chunk indexing** — the message is split into `pgta` chunk-groups of `rank_n · ch_n · chunk_size_n` elements; within each group every step indexes a half-chunk (`CHUNK_H0`/`CHUNK_H1`) and the step's chunk position `k` walks the `user_ranks[]` traversal array forward (reduce-scatter) then backward (all-gather).
- **The two-phase step schedule** — reduce-scatter (`send` → `(rank_n−2)`× `recv_reduce_send` → final `recv_reduce_copy_send` / `recv_reduce_copy`+`direct_copy_send`) then all-gather (`(rank_n−2)`× `direct_recv_send` → final `direct_recv`), each step issued twice for the two half-chunks, bracketed by `advance_send`/`advance_recv` chunk rotation and `mark_step`/`mark_end` bookkeeping. Each leaf emits the `cc_op_entry` + SDMA descriptor for that step.

| | |
|---|---|
| **Schedule engine** | `enc_metaring_primitive::compose_operation @0x178170` (per-op master emit loop) |
| **Per-channel dispatch** | `__compose_channel_ring @0x177340` — op-type switch → per-collective composer |
| **All-reduce step composer** | `__compose_allreduce_channel @0x171600` (reduce-scatter + all-gather; the MATH) |
| **Reduce-scatter / all-gather composers** | `__compose_redsct_channel @0x16d800` · `__compose_allgather_channel @0x16f940` |
| **Schedule container** | `enc_alg_metaring` (37544 B; `ring_ranks[32]` @+2824, `type` @+37512) DWARF `<125a6e>` |
| **Per-channel ring payload** | `enc_ring` (24 B; `prev@0, next@4, user_ranks@8, duplicate@16`) DWARF `<1258fb>` |
| **Rank/neighbor math** | `nccl_setup_alg_ring_info @0x1005c0` → `ring_ranks[ch].{prev,next,user_ranks}` |
| **Chunk sizing** | `__set_dynamic_chunk_size @0x14bba0` — align to `sdma_data_type_size[dtype]`, `/chunks /channels`, 32-granularity (`>>5`) |
| **Half-chunk enum** | `enc_half_chunk_index` — `CHUNK_H0=0, CHUNK_H1=1, ENC_CHUNK_SPLIT_N=2` DWARF `<60c2a1>` |
| **In-DMA reduce opcode** | `SDMA_CCETYPE` — `ADD=0, FMA=1, MAX=2, MIN=3, EXT=4, GCE=5` DWARF `<337be>` |
| **Step leaves** | `enc_primitive::{send@0x16c960, recv_reduce_send@0x16ad70, direct_recv_send@0x16f820, …}` |
| **Host-side boundary** | libnccom builds ring *order* only; the step loop is libnrt — see CORRECTION below |

> **CORRECTION (RING-1) — the device-side ring step loop lives in libnrt, not libnccom.** An NCCL-shaped mental model places the ring all-reduce step loop (the `send`/`recvReduceSend`/`directRecvCopySend` sequence) inside the NCCL library. In the Neuron stack that loop is **absent from libnccom** and **present in libnrt**: a symbol sweep of `libnccom.so` finds none of the `enc_metaring_primitive::__compose_*` composers or the `enc_primitive::{send,recv_reduce_send,direct_recv_send,…}` leaves; all of them resolve in `libnrt.so` (`enc_primitive.cc`/`.h`, `.text` band `0x148xxx..0x178xxx`). libnccom decides only the host-side ring **order** (the `prev`/`next`/`userRanks` wiring); libnrt's `nccl_setup_alg_ring_info @0x1005c0` **re-derives** the device neighbor order from the physical MLA topology into `enc_alg_metaring.ring_ranks[32]`, and `__compose_allreduce_channel @0x171600` emits the step program. This closes the OPEN "device-side ring step loop" item flagged on the host-side sibling [Ring Algorithm Construction (Host-Side)](../nccom/algorithm-ring.md).

---

## 1. The Schedule Container

### Purpose

`enc_alg_metaring` is the per-algorithm channel container the ring composer reads and the device driver populates. One copy is embedded in each `enc_comm` (at `+88` for `ring`, `+37632` for `kangaring`, `+75176` for `rdh`). It holds the per-channel ring state (`channels[32]`), the per-channel device traversal order (`ring_ranks[32]`), the multi-rail kangaring order (`kangaring_ranks[32]`), and the metaring-type label that selects which composer runs. The composer never invents an order; it reads `ring_ranks[ch].prev/next/user_ranks`, which `nccl_setup_alg_ring_info` filled from the MLA topology before any op is posted.

### Layout — `enc_alg_metaring` (DWARF `<125a6e>`, 37544 B)

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `channel_n` | +0 | `int` | number of active ring channels (1..32) | HIGH |
| `channels[32]` | +8 | `enc_channel[88]` | per-channel transport state (net connectors, devmem, drv channel) | HIGH |
| `ring_ranks[32]` | +2824 | `enc_ring[24]` | RING device traversal order — set by `nccl_setup_alg_ring_info` | HIGH |
| `kangaring_ranks[32]` | +3592 | `enc_kangaring[1060]` | KANGARING per-rail order — set by `nccl_setup_alg_kangaring_info` | HIGH |
| `type` | +37512 | `metaring_type_t` | `RING=0/KANGARING=1/SINGLE_CYCLE_RING=2/RDH=3/INVALID=4` — selects the composer | HIGH |
| `one_rank_per_device` | +37516 | `bool` | one-rank-per-device vs one-rank-per-chip ring shape | HIGH |
| `is_hybrid_ring` | +37517 | `bool` | hybrid (gateway-bridged) ring — allows partial channel activation | HIGH |
| `tokens_exchanged` | +37518 | `bool` | neighbor-token bring-up latch | HIGH |
| `deadlock_free_rank_list` | +37519 | `bool` | sorted (deadlock-free) rank ordering applied | HIGH |
| `comm` | +37520 | `enc_comm*` | owning communicator | HIGH |
| `drv_alg` | +37528 | `encd_alg_metaring*` | device-resident driver counterpart | HIGH |
| `skip_send` / `skip_recv` | +37536 / +37537 | `bool` | per-direction elision flags | HIGH |

### The per-channel payload — `enc_ring` (DWARF `<1258fb>`, 24 B)

`ring_ranks[ch]` is the device step order for channel `ch`. It is the structural witness that the ring is a **linear ordering**, not a tree: there is no `up`/`down`/`parent` field anywhere.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `prev` | +0 | `int` | ring predecessor rank for this channel (`-1` ⇒ empty channel) | HIGH |
| `next` | +4 | `int` | ring successor rank for this channel | HIGH |
| `user_ranks` | +8 | `int*` | full ring traversal order, `malloc`'d `rank_n` ints — the per-step chunk-position table | HIGH |
| `duplicate` | +16 | `bool` | second-direction / rail-duplicate flag (the two ring directions) | HIGH |

> **NOTE —** `user_ranks` is the array the step loop walks to compute each step's chunk position. `nccl_setup_alg_ring_info` allocates it (`malloc(rank_n*4)`) and `memcpy`s the traversal order in, ordered by `get_sorted_ranks` (a deadlock-free rank sort over the MLA-device topology). `enc_free_alg_metaring @0xfdd30` frees it per-channel only when `type == RING`. A reimplementer must reproduce both the allocation and the per-channel free; the `kangaring` path stores its order in `enc_kangaring.logical_path[256]` instead.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nccl_setup_alg_ring_info` | `0x1005c0` | fills `ring_ranks[ch].{prev,next,user_ranks,duplicate}` from MLA topology; sets `is_hybrid_ring` | HIGH |
| `nccl_setup_alg_rings` | `0x107880` | per-generation gate; drives ring + kangaring info builders | HIGH |
| `init_metaring_algorithm` | `0xfe570` | device-alg dispatcher → `alg_ring_init`/`alg_kangaring_init`/`alg_inter_rdh_init` by `type` | HIGH |
| `enc_get_metaring_type` | `0xfc860` | `enc_alg_type` → `metaring_type` (0→RING, 3→KANGARING, 4→SINGLE_CYCLE, 7→RDH) | HIGH |
| `enc_free_alg_metaring` | `0xfdd30` | per-channel `free(ring_ranks[i].user_ranks)` (RING only) + `free_channel` | HIGH |

### Considerations

The rank/neighbor math in `nccl_setup_alg_ring_info` is **not** a copy of libnccom's ring order — it is a re-derivation from the device's physical NeuronLink port topology (`encd_arch_get_mla_device_idx` / `encd_arch_get_port` / `encd_arch_get_p2p_port`), asserting `my_mla_idx < rank_n` and `next/prev != NEC_UNKNOWN`. For the one-rank-per-device hybrid case (`node_n==1 && vcore==2 && mla_cycle_n==2 && rank_n==16` with all peers sharing `dev%4`) it sets `is_hybrid_ring` and forces `channel_n = 4`. The emit log `"[nec_dev %d ch %d] one_rank_per_device ring my %d-%d next %d-%d prev %d-%d duplicate %d reverse %d user_ranks %s"` (`.rodata @0x7e17e0`) is the per-channel witness of the derived order. The MLA-port physical mapping is owned by [encd: Per-Arch Ops Dispatch](encd-arch-ops.md); this page consumes its output.

---

## 2. Chunk Sizing and the Half-Chunk Pipeline

### Purpose

Before any step is emitted, `compose_operation` sizes the unit of transfer. A ring step does not move the whole message — it moves one *chunk* (one rank's share), and each chunk is further split into two *half-chunks* so the two halves pipeline across adjacent links. The chunk-size math decides how many bytes ride each `send`/`recv_reduce_send`, and the chunk-group math decides how many times the whole two-phase schedule repeats for a message larger than one ring's worth of chunks.

### Entry Point

```text
enc_metaring_primitive::compose_operation (0x178170)        ── per-op master emit loop
  ├─ __set_dynamic_chunk_size      (0x14bba0)   ── size one chunk; pick nr_channel_chunks
  ├─ __set_active_channels         (0x14cf20)   ── which of channel_n channels run
  ├─ __dmem_map_and_register_devmem / __register_net_buffers
  ├─ build_pagetable               (0x151420)   ── per-op page table (allreduce/allgather/redsct/p2p)
  ├─ for ch in active_channels_ids:
  │     __compose_channel_ring(ch) (0x177340)   ── per-channel step dispatch (§3)
  └─ __exchange_ring_output_addr   (0x1517c0)   ── cross-rank output-addr exchange
```

### Algorithm — chunk and half-chunk sizing

```c
// __set_dynamic_chunk_size @0x14bba0 — size one chunk, choose nr_channel_chunks.
function set_dynamic_chunk_size(prim):
    dtype_sz = sdma_data_type_size[prim.data_type];   // table @0x8580c0 (8B entries)
    // align the per-element granule up to the SDMA data-type size
    granule  = align_up(dtype_sz, dtype_sz);          // (v4+31-(v4+31)%v4)/v4 style round
    total    = prim.chunk_size_n * prim.nr_channel_chunks;
    per_ch   = prim.size_n / prim.channel_n;          // bytes this channel carries
    // 32-element granularity: shift the total down by 5 (÷32), re-round to the granule
    new_chunk = granule + (total >> 5) - 1;
    new_chunk -= new_chunk % granule;                 // round to a whole granule
    prim.chunk_size_n      = new_chunk;               // bytes per chunk
    prim.nr_channel_chunks = total / new_chunk;       // chunks per channel

// __compose_allreduce_channel @0x171600 — chunk-GROUP count for the whole message.
// One ring pass moves rank_n*ch_n chunks of chunk_size_n; a big message needs several passes.
function chunk_group_count(prim, rank_n, ch_n):
    group_elems = rank_n * ch_n * prim.chunk_size_n;            // function_unrolled_count*chunk
    pgta = ceil_div(prim.size_n, group_elems);                 // chunk-group passes
    return pgta;                                               // outer loop bound
```

Each step then issues the op twice — once for `CHUNK_H0`, once for `CHUNK_H1`:

```c
// the half-chunk pipeline: every ring step is two ops, one per half.
enc_primitive::send(prim, CHUNK_H0, use_gateway);
enc_primitive::send(prim, CHUNK_H1, use_gateway);
//                        ^ enc_half_chunk_index: H0=0, H1=1
// set_chunk_size_n asserts !(sz_n & 1): the chunk MUST be even so the two halves are equal.
```

> **GOTCHA —** the half-chunk split is **mandatory**, not optional. `enc_primitive::set_chunk_size_n` asserts `!(sz_n & 1)` (`enc_primitive.h:0x1FA`) — a chunk size must be even so it splits into two equal `CHUNK_H0`/`CHUNK_H1` halves. A reimplementation that treats a ring step as a single op moving the whole chunk will both desynchronize the pipeline (the device program expects exactly two ops per step) and trip the parity assert. The 32-element (`>>5`) granularity in `__set_dynamic_chunk_size` is what guarantees the chunk is a multiple of 32 elements — and therefore even.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `compose_operation` | `0x178170` | per-op master loop: size → active → buffers → page table → per-channel compose → exchange | HIGH |
| `__set_dynamic_chunk_size` | `0x14bba0` | align to `sdma_data_type_size[dtype]`, `/chunks /channels`, 32-granularity (`>>5`) | HIGH |
| `__set_active_channels` | `0x14cf20` | which channels run (`encd_ring_is_channel_active` / `set_active_channels_cnt`) | HIGH |
| `enc_primitive::set_chunk_size_n` | `0x1468e0` | set step chunk size; asserts `!(sz & 1)` | HIGH |
| `enc_primitive::half_chunk_size` | `0x1468b0` | per-half (H0/H1) byte size | HIGH |
| `build_pagetable` | `0x151420` | per-op page table (allreduce/allgather/redsct/p2p/permute variants) | HIGH |

---

## 3. Per-Channel Op-Type Dispatch

### Purpose

`__compose_channel_ring @0x177340` is the per-channel entry the master loop calls once per active channel. It reads the channel's `prev`/`next` from `ring_ranks[ch]`, builds the `node` (self) and `node_next` (forward endpoint) on its stack, and switches on `enc_op_type` to the per-collective step composer. An empty channel (`prev == -1`) emits nothing.

### Algorithm — the dispatch

```c
// __compose_channel_ring @0x177340 — per-channel step dispatcher.
function compose_channel_ring(prim, ch):
    metaring = prim.metaring;
    prev = metaring.ring_ranks[ch].prev;          // +2824 + ch*24 + 0
    if prev == -1:                                // decompile line 69: v82 == -1
        return compose_empty(prim, ch);           // 0x15a6f0 — NOP/empty channel

    build node(self) and node_next(forward) from ring_ranks[ch] and ci->peers[]
    rdh          = (metaring.type == RDH);
    single_cycle = (metaring.type == SINGLE_CYCLE_RING);

    switch prim.op_type:                           // enc_op_type
      case ALLGATHER:                              // 0
        rdh ? __compose_inter_rdh(ch, ALLGATHER)
            : __compose_allgather_channel(ch)      // 0x16f940
      case ALLREDUCE:                              // 1
        single_cycle ? __compose_single_cycle_allreduce_channel(ch)   // 0x16cdc0
        : rdh        ? __compose_inter_rdh(ch, ALLREDUCE)             // 0x15ae70
        :              __compose_allreduce_channel(ch)               // 0x171600  (§4)
      case REDUCE_SCATTER:                         // 4
        rdh ? __compose_inter_rdh(ch, REDUCE_SCATTER)
            : __compose_redsct_channel(ch)         // 0x16d800
      case SEND: case RECV:                        // 5, 6
        __compose_p2p_channel(ch, op_type)         // 0x175d20
      case PERMUTE: case PERMUTE_IMPLICIT:         // 8, 10
        __compose_permute_channel(ch)              // 0x176650
      case PERMUTE_REDUCE_IMPLICIT:                // 11
        __compose_permute_reduce_channel(ch)       // 0x175110
      default:                                     // BROADCAST/REDUCE/ALLTOALL
        nlog_error("not supported operation (op_type %d)")   // decompile line 444
        return NRT_INVALID
```

> **QUIRK —** the ring path **rejects** `BROADCAST(2)`, `REDUCE(3)`, and `ALLTOALL(7)` outright — they fall to the `default` error arm (`"[nec_dev %d] not supported operation (op_type %d)"`, decompile line 444). All-to-all in particular needs the fully-peered mesh family (`"alltoall cannot be supported without Mesh algorithm"`, `.rodata @0x7e6638`); a ring is a linear order and cannot express the `N·(N−1)` exchange. A reimplementer who routes those op-types to a ring composer will hit the assert path, not a silent miscompile — which is the intended behavior: the algorithm selector (`enc_post_operation`, see [Algorithm Taxonomy §2.1](algorithm-taxonomy.md)) must never resolve `ALLTOALL` to RING.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `__compose_channel_ring` | `0x177340` | per-channel op-type dispatch; reads `ring_ranks[ch].prev/next`; empty on `prev==-1` | HIGH |
| `__compose_allreduce_channel` | `0x171600` | RING all-reduce (reduce-scatter + all-gather) — §4 | HIGH |
| `__compose_redsct_channel` | `0x16d800` | RING reduce-scatter only | HIGH |
| `__compose_allgather_channel` | `0x16f940` | RING all-gather only | HIGH |
| `__compose_single_cycle_allreduce_channel` | `0x16cdc0` | SINGLE_CYCLE_RING all-reduce (fused 1-pass variant) | HIGH |
| `__compose_p2p_channel` | `0x175d20` | SEND / RECV point-to-point | HIGH |
| `__compose_permute_channel` | `0x176650` | PERMUTE / PERMUTE_IMPLICIT | HIGH |
| `__compose_inter_rdh` | `0x15ae70` | RDH fall-through for ALLGATHER/ALLREDUCE/REDUCE_SCATTER | HIGH |
| `__compose_empty` | `0x15a6f0` | empty-channel NOP | HIGH |

---

## 4. The All-Reduce Step Schedule

### Purpose

`__compose_allreduce_channel @0x171600` is the heart of the page: the per-channel emitter that turns one all-reduce op into the canonical NCCL ring step program. It builds an `enc_primitive` step object, computes the chunk-group count, and runs the two-phase loop — reduce-scatter then all-gather — issuing one `enc_primitive` leaf per step per half-chunk. Each leaf produces the `cc_op_entry` step descriptor and the SDMA descriptors that move (and, in the reduce-scatter phase, reduce) that half-chunk; the byte layout of those artifacts is owned by [The cc_op_entry On-Device ISA](cc-op-isa.md) and [enc Primitives](enc-primitives.md).

### Entry Point

```text
__compose_allreduce_channel (0x171600)
  ├─ enc_primitive::enc_primitive (0x1abd40)   ── build the per-step state (rank, node, node_next, peers)
  ├─ post_send / advance_send / post_recv      ── prologue: prime the send/recv credit + chunk index
  ├─ for group in [0, pgta):                    ── chunk-group passes (large messages)
  │    ├─ PHASE 1  reduce-scatter
  │    │    send(H0); send(H1)                  ── push own chunk to next
  │    │    (rank_n-2)× recv_reduce_send(H0/H1) ── recv prev → reduce → fwd next
  │    │    recv_reduce_copy_send | recv_reduce_copy+direct_copy_send  ── final reduce
  │    └─ PHASE 2  all-gather
  │         (rank_n-2)× direct_recv_send(H0/H1) ── recv reduced chunk, forward
  │         direct_recv(H0); direct_recv(H1)    ── recv last chunk, do NOT forward
  │         reset_send_credit
  ├─ advance_recv(-concat_first); mark_complete; mark_step
  └─ mark_end                                   ── close the op into the SPAD op-stream
```

### Algorithm — the two-phase ring step loop

The pseudocode below models `__compose_allreduce_channel @0x171600` directly from the decompile. `na` is the channel's `user_ranks[]` traversal array (`enc_ring.user_ranks`); the reduce-scatter middle loop walks it forward (`&na[rank_n]` down to `&na[3]`), the all-gather loop walks it back up. `rank_n` is the ring length; `ch_n` the active-channel count.

```c
// __compose_allreduce_channel @0x171600 — RING all-reduce, one channel.
function compose_allreduce_channel(prim, ch, self_node, next_node):     // 0x171600
    rank_n = self_node.rank_n;
    na     = prim.metaring.ring_ranks[ch].user_ranks;   // the traversal order array
    p = build_enc_primitive(prim.rank, op_idx, ..., self_node, next_node, peers,
                            in_sz, out_sz, SDMA_DTYPE, ...);            // ctor @0x1abd40

    // ---- prologue: prime the ring (credit + chunk index rotation) ----
    p.post_send(1);  p.mark_step(0);                     // lines 324-325
    p.advance_send(1);                                   // rotate send chunk index +1
    p.post_recv(concat_first);  p.mark_step(0);          // lines 327-329

    // chunk-group passes: a message bigger than rank_n*ch_n chunks loops here
    group_elems = rank_n * ch_n * p.chunk_size_n;        // function_unrolled_count*chunk
    pgta = ceil_div(p.size_n, group_elems);              // line 331
    k_base = rank_n * (ch % ch_n);                       // this channel's chunk window (v76)

    for group in [0, pgta):                              // line 348 do/while
        // re-clamp the chunk size on the final (short) group  (lines 351-371)
        remained = p.size_n - p.chunk_size_n * group_off;
        assert(remained != 0);                           // "remained_n"
        if remained < p.chunk_size_n * group_elems:
            p.chunk_sz_n = reclamp_tail(remained, rank_n, dtype_sz);  // assert <= chunk_size_n, even

        // ===== PHASE 1 — REDUCE-SCATTER (rank_n-1 steps) =====
        // step 0: push our own chunk to the successor
        p.k = na[rank_n-1];                              // first chunk position
        p.send(CHUNK_H0, use_gateway);                   // 0x16c960
        p.send(CHUNK_H1, use_gateway);                   // lines 375-376

        // middle steps: recv from prev, reduce in-DMA, forward to next  (rank_n-2 of them)
        for nptr in (&na[rank_n] .. &na[3]):             // line 380 do/while, runs if rank_n>2
            p.k = nptr[-2];                              // chunk position for this step
            p.recv_reduce_send(CHUNK_H0, (SDMA_CCETYPE)p.data_op_type, /*copy=*/0,
                               is_hybrid_ring_op, use_gateway);          // 0x16ad70
            p.recv_reduce_send(CHUNK_H1, (SDMA_CCETYPE)p.data_op_type, 0,
                               is_hybrid_ring_op, use_gateway);          // lines 384-385

        // final reduce step: write the fully-reduced chunk for this rank's slot
        p.k = na[0];
        if encd_is_host_cc(drv_ctx):                     // host-CC path forwards
            p.recv_reduce_copy_send(CHUNK_H0, cce, /*copy=*/1, is_hybrid, use_gw);  // 0x16aed0
            p.recv_reduce_copy_send(CHUNK_H1, cce, 1, is_hybrid, use_gw);           // lines 398-399
        else:                                            // device path: reduce then copy-forward
            p.recv_reduce_copy(CHUNK_H0, cce, 0, &compl_addr, is_hybrid);           // 0x16b030
            p.direct_copy_send(CHUNK_H0, 0,0, use_gw, out_shared_scratchpad);       // 0x16f710
            p.recv_reduce_copy(CHUNK_H1, cce, 0, &compl_addr, is_hybrid);
            p.direct_copy_send(CHUNK_H1, 0,0, use_gw, out_shared_scratchpad);

        // ===== PHASE 2 — ALL-GATHER (rank_n-1 steps) =====
        // middle steps: recv a reduced chunk, forward it on  (rank_n-2 of them)
        for nptr in (&na[rank_n] .. &na[3]):             // line 427 do/while, runs if rank_n>2
            p.k = nptr[-1];                              // walk back up the traversal order
            p.direct_recv_send(CHUNK_H0, 0,0, is_hybrid, use_gw, out_scratch, is_host_cc);  // 0x16f820
            p.direct_recv_send(CHUNK_H1, 0,0, is_hybrid, use_gw, out_scratch, is_host_cc);  // 433-434

        // final all-gather step: recv the last chunk but do NOT forward (ring closed)
        p.k = na[1];
        p.direct_recv(CHUNK_H0, 0,0, is_hybrid, is_host_cc);            // 0x158540 (441-442)
        p.direct_recv(CHUNK_H1, 0,0, is_hybrid, is_host_cc);
        p.reset_send_credit();                            // 0x1495e0
        group_off += group_elems;

    // ---- epilogue: unwind the chunk index, mark completion, close the op ----
    p.advance_recv(-concat_first);                       // line 449
    p.mark_complete();
    p.mark_step(0);
    if encd_is_neighbor_pod(channel.drv_channel, ENCD_NEIGH_PREV):    // cross-pod hop
        p.post_recv(1);  p.mark_step(1);
    p.mark_end();                                        // close into SPAD op-stream
```

The step-count identity is the textbook NCCL ring, confirmed by the loop bounds: the reduce-scatter middle loop runs from `&na[rank_n]` down to `&na[3]` — exactly `rank_n − 2` iterations — preceded by the `send` step (1) and followed by the final reduce step (1), totaling `rank_n − 1` reduce-scatter steps. The all-gather phase is symmetric: `rank_n − 2` `direct_recv_send` steps plus the final `direct_recv`, totaling `rank_n − 1`. Every step is two leaf calls (`CHUNK_H0` + `CHUNK_H1`).

> **QUIRK —** the reduction is performed **inside the SDMA engine**, not by a compute kernel. Each `recv_reduce_send`/`recv_reduce_copy_send` carries `(SDMA_CCETYPE)prim.data_op_type` — the in-DMA collective-compute-engine reduce opcode (`ADD=0/FMA=1/MAX=2/MIN=3`) — so the partial sum is accumulated by the DMA hardware as the chunk lands. A reimplementer modeling the ring as "DMA then reduce kernel" has the wrong cost model: there is no separate reduce pass; the `cce` opcode rides the receive descriptor. The host-CC path (`encd_is_host_cc`) is the one exception — it forwards the reduced chunk (`recv_reduce_copy_send`) where the device path splits into `recv_reduce_copy` + a separate `direct_copy_send`.

> **NOTE —** the final epilogue distinguishes a same-pod neighbor from a cross-pod one: `encd_is_neighbor_pod(channel.drv_channel, ENCD_NEIGH_PREV)` gates an extra `post_recv(1)`/`mark_step(1)` pair. This is the hybrid/gateway hop — when the predecessor is reached over the network rather than direct D2D, the ring step rides a gateway buffer, gated per-hop by `encd_hybrid_ring_is_next_neigh_use_gateway` / `encd_is_host_cc`. The gateway routing decision is per-channel state owned by [encd: the Device-Resident Descriptor Emitter §4](encd-overview.md).

### Function Map — the step leaves

Each leaf is an `enc_primitive` method (`enc/enc_primitive.h`, src string `@0x7e1a90`) that emits the `cc_op_entry` + SDMA descriptor for one ring step. The reduce-scatter phase uses the first four; the all-gather phase the next three; the rest are bookkeeping.

| Leaf | Address | Phase | Emits | Confidence |
|---|---|---|---|---|
| `send` | `0x16c960` | RS step 0 | push own chunk → next | HIGH |
| `recv_reduce_send` | `0x16ad70` | RS middle | recv prev → in-DMA reduce → fwd next | HIGH |
| `recv_reduce_copy_send` | `0x16aed0` | RS final (host-CC) | reduce + local copy + fwd | HIGH |
| `recv_reduce_copy` | `0x16b030` | RS final (device) | final reduce, no forward | HIGH |
| `direct_copy_send` | `0x16f710` | RS→AG bridge | in-place copy + fwd | HIGH |
| `direct_recv_send` | `0x16f820` | AG middle | recv reduced chunk, forward | HIGH |
| `direct_recv` | `0x158540` | AG final | recv last chunk, no forward | HIGH |
| `advance_send` / `advance_recv` | `0x148900` / `0x148990` | prologue/epilogue | rotate the chunk index ±1 | HIGH |
| `post_send` / `post_recv` | `0x148ae0` / `0x149570` | prologue/hops | post the DMA / arm credit | HIGH |
| `reset_send_credit` | `0x1495e0` | per-group | reset send credit between passes | HIGH |
| `mark_step` / `mark_end` | `0x1497e0` / `0x157b80` | bookkeeping | step / op-close marks | HIGH |

### Considerations

The reduce-scatter-only (`__compose_redsct_channel @0x16d800`) and all-gather-only (`__compose_allgather_channel @0x16f940`) composers reuse the same two halves of this loop independently — `REDUCE_SCATTER` emits only Phase 1 and ends after the final reduce; `ALLGATHER` emits only Phase 2. The `SINGLE_CYCLE_RING` variant (`__compose_single_cycle_allreduce_channel @0x16cdc0`) fuses the two phases into a single pass and is a separate composer; its exact step-count delta is not traced on this page (OPEN, §5). KANGARING all-reduce (`__compose_allreduce_channel_kangaring @0x1726f0`) carries a `vector<node_peer>` rail set and issues the same step taxonomy per rail; the multi-rail `logical_path[256]` construction inside `nccl_setup_alg_kangaring_info @0x1059e0` is not transcribed here (OPEN, §5).

---

## 5. Verification Notes

> The ring step schedule, the dispatch, the chunk math, and the schedule container were cross-checked against the IDA decompile + DWARF of `libnrt.so` 2.31.24.0:
> - **`__compose_allreduce_channel @0x171600`**: the two-phase step sequence is read line-by-line from the decompile — `send(H0)`/`send(H1)` (lines 375-376), the `recv_reduce_send` middle loop `&na[rank_n]→&na[3]` (lines 380-385, `rank_n−2` iters), `recv_reduce_copy_send` / `recv_reduce_copy`+`direct_copy_send` final reduce (lines 398-424), the `direct_recv_send` all-gather loop (lines 427-434), and `direct_recv`/`reset_send_credit`/`mark_end` (lines 441-457). The `pgta` chunk-group bound and `k = na[…]` chunk-position writes are at lines 331-451. **HIGH.**
> - **`__compose_channel_ring @0x177340`**: the `prev == -1` empty check (line 69) and the `enc_op_type` switch with per-collective targets (lines 354-444, including the `"not supported operation"` default at line 444) are decompile-confirmed. **HIGH.**
> - **`__set_dynamic_chunk_size @0x14bba0`**: the `sdma_data_type_size[dtype]` align, `/channel_n`, and `>>5` (32-element) granularity are at decompile lines 18-37. **HIGH.**
> - **Structs** `enc_alg_metaring` (DWARF `<125a6e>`, 37544 B; `ring_ranks@2824`, `type@37512`), `enc_ring` (`<1258fb>`, 24 B; `prev@0/next@4/user_ranks@8/duplicate@16`), `enc_half_chunk_index` (`<60c2a1>`), `SDMA_CCETYPE` (`<337be>`) are verbatim DWARF.
> - **Boundary**: the absence of all `__compose_*` / `enc_primitive::*` step symbols from `libnccom.so` (and their presence in `libnrt.so`) is the proof behind `CORRECTION (RING-1)`; symbol presence cross-checked against the host-side sibling cell.
>
> **[MED]** The closed-form step-count `(rank_n−1)` per phase is the standard ring identity and matches the decoded loop bounds (`&na[rank_n]→&na[3]` = `rank_n−2` middle steps + 1 send + 1 final); the exact register expression for the loop bound is read but not algebraically re-derived. The `duplicate`/`reverse` flag → two-ring-direction mapping is inferred from the `enc_ring.duplicate` field + the `nccl_setup_alg_rings` double pass, not byte-pinned.
> **[OPEN]** The `SINGLE_CYCLE_RING` step-count difference vs the two-pass ring (`__compose_single_cycle_allreduce_channel @0x16cdc0` body not transcribed). The KANGARING `logical_path[256]` rail-fill math inside `nccl_setup_alg_kangaring_info @0x1059e0`. The exact `cc_op_entry` bytes each leaf emits — owned by [The cc_op_entry On-Device ISA](cc-op-isa.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_metaring_primitive::compose_operation` (`@0x178170`) | the per-op master loop that drives `__compose_channel_ring` per active channel |
| `nccl_setup_alg_ring_info` (`@0x1005c0`) | fills `ring_ranks[]` — the device traversal order this loop walks |
| `enc_primitive::{send,recv_reduce_send,direct_recv_send,…}` | the step leaves that emit each ring step's `cc_op_entry` + SDMA descriptor |
| `__compose_redsct_channel` / `__compose_allgather_channel` | reuse Phase 1 / Phase 2 of this composer independently |
| `encd_dma_mark_end` (`@0x237200`) | the emitter funnel `mark_end` calls down into to close the op into the SPAD op-stream |

## Cross-References

- [Ring Algorithm Construction (Host-Side)](../nccom/algorithm-ring.md) — the libnccom side that builds the ring *order* (prev/next/userRanks); this page is the device step loop it feeds (see CORRECTION RING-1)
- [encd: the Device-Resident Descriptor Emitter](encd-overview.md) — the `encd` floor each step leaf emits into, and the hybrid-ring gateway-routing state the epilogue consults
- [Engine Core (enc_context and Accessors)](engine-core.md) — the `enc_context` parse/exec state and `enc_operation` the composer reads its op params from
- [The cc_op_entry On-Device ISA](cc-op-isa.md) — the byte-exact collective-op descriptor each `enc_primitive` leaf produces
- [Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)](algorithm-taxonomy.md) — where RING sits among the four families and how the selector resolves it
- [enc Primitives (Send/Recv Leaves)](enc-primitives.md) — the per-step `enc_primitive` leaf bodies and their SDMA descriptor emission
- [Overview: the Collective-Compute Architecture](overview.md) — the two-layer composer→emitter split this page details the ring path of
- [back to index](../index.md)
