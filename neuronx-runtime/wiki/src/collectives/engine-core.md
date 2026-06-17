# Engine Core (enc_context and Accessors)

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present, and the host-side composer source TU is `/opt/workspace/KaenaRuntime/enc/enc.cc`. `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (symbol- and switch-anchored)** — the central dispatcher, its 13-case jump table, and every accessor's offset arithmetic are read from the IDA-recovered decompile + `switches.json` + `structures.json`; enums are verbatim from `enums.json`. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

Every collective a NeuronCore runs is staged through one heavyweight host-side object: `enc_context`, the per-NeuronCore parse/exec state machine that lives at `encd_context+288` (`drv_ctx->enc_ctx`). This page is the spec of that object and of the central dispatcher that drives it. The reference frame is a NCCL communicator's host state — a posted-op queue, a small fixed set of communicators, a parsed replica-group topology — except that here the queue is not executed on the host: it is *compiled*. The dispatcher reads each posted op, resolves one algorithm, builds an `enc_operation` descriptor, and appends it to the context's op tables; a later pass lowers those descriptors into the `cc_op_entry` ISA the silicon executes. `enc_context` is the membrane between the public `nrt_*` collective API and the device emitter.

The object has two faces. The **mutating** face is `enc_post_operation @0x11f790` (`enc.cc`, 21 252 B), the 13-case `enc_op_type` dispatcher that this page documents in detail: it runs a feasibility battery, resolves an `enc_alg_type` by fixed precedence, switches on the op-type into a per-op-type composer hand-off, and posts the resulting `enc_operation` through `enc_context::post_op @0x10c750`. The **read-mostly** face is a block of ~20 branch-free accessors (`enc_get_operation_* @0xfee10..`, `enc_get_comm_* @0xff130..`) that the profiler and NEFF-translation layers use to index the context's posted-op table and its `comms[12]` array. Both faces touch the *same* offsets, so the accessors are the cleanest ground truth for the object's layout: `all_posted_ops` @+128, `posted_ops[4]` @+152, `comms[12]` @+1520 (stride 561 904), `signature` @+992, `drv_ctx` back-pointer @+6 753 584.

A reimplementer's contract is to reproduce three things: (1) the **`enc_context` layout** — which member each raw word index resolves to, because the accessors index it by raw `enc_ctx[N]` dword/qword offsets, not named fields; (2) the **central dispatch** — the `enc_can_post_*` battery, the alg-precedence cascade (documented in full on [`overview.md` §2.1](overview.md) and [`algorithm-taxonomy.md` §2.1](algorithm-taxonomy.md), referenced not re-derived here), and the 13-case `enc_op_type` jump table whose targets this page pins consistently with the shipped overview; and (3) the **op/comm accessor arithmetic and the op-size scaling** (`enc_op_input_size`/`enc_op_output_size`), which encode how a collective's input and output byte counts scale with `rank_n` per op-type.

> **CORRECTION (ENG-1) — the central dispatcher is `enc_post_operation @0x11f790`, not `@0x105550`.** An early task seed pinned `enc_post_operation` at `0x105550`. That address is a *different, 8-byte* function: `enc_context::__post_op @0x105550`, a trivial forwarder whose entire body is `return enc_operation::post(enc_op);` (decompile confirmed). The real 13-case dispatcher is `_Z18enc_post_operationP12encd_contextR11enc_op_list @0x11f790` (`function_addresses.json`), whose op-dispatch switch is at `inst_addr 0x12038a` (13 cases, default `0x1213c0`, `switches.json` `func_addr 0x11f790`) — byte-identical targets to [`overview.md` §3](overview.md). The `0x105550` seed is discarded; every reference below uses `0x11f790`.

| | |
|---|---|
| **Context object** | `enc_context` — 6 753 648 B; reached via `encd_context+288` (`drv_ctx->enc_ctx`) |
| **Driver context** | `encd_context` — 624 B (`tdrv/encd.c`); `vcore` @+0, `ccop_owner` @+16, `enc_ctx` @+288, `ctx_id` @+561 |
| **Central dispatcher** | `enc_post_operation @0x11f790` (`enc.cc`, 21 252 B) — 13-case `enc_op_type` switch |
| **Op-dispatch jump table** | switch @`0x12038a`, 13 cases, default `0x1213c0` (`switches.json`) |
| **Op-teardown jump table** | switch @`0x1242ef`, 13 cases, default `0x124943` (same fn, op-end paths) |
| **Post sink** | `enc_context::post_op @0x10c750` → `__post_op @0x105550` → `posted_ops[stream]` / `all_posted_ops` |
| **Op accessors** | `enc_get_operation_* @0xfee10..0xff0e0` — index `all_posted_ops[op_idx]` |
| **Comm accessors** | `enc_get_comm_* @0xff130..0xff220` — index `comms[12]` (stride 140 476 dwords) |
| **Op-size arithmetic** | `enc_op_input_size @0xf9590` · `enc_op_output_size @0xf95e0` (×`rank_n` per op-type) |
| **Op family** | `enc_op_type` — 13 valid (`ALLGATHER=0 … ALLTOALL_V=12`), `OP_INVALID/OP_N=13` |
| **Device pattern** | `enc_pattern_t` — `RING=0`, `MESH=1`, `INVALID=2` (the `cc_op_entry.algo_type` field) |

---

## 1. The `enc_context` Object

### Purpose

`enc_context` is the per-NeuronCore collectives state the whole composer layer mutates. It holds the posted-op queue (the program being compiled), the small fixed set of communicators a NEFF binds (`comms[12]`), the parsed replica-group / source-target-pair topology, the per-rank op signature, and the per-stream op-slot cursors. It is **not** the global communicator (`enc_glb_comm`, the multi-node bootstrap object — that is [Comm Context and Bootstrap](comm-context.md)); it is the per-NC compile state that sits one level below it. Crucially, every accessor reaches it through `drv_ctx->enc_ctx` and then indexes it by raw word offsets, so the object's layout is reconstructable purely from the accessor disassembly.

### Layout

The members below are the ones the dispatcher and the accessors touch, with offsets from `structures.json` cross-checked against the decompiled raw-index accesses (e.g. `enc_ctx[16]`/`[17]` = qwords 16/17 = bytes 128/136 = `all_posted_ops {begin,end}`). Confidence **HIGH** for every offset confirmed by a live accessor; **MED** where the field is named from `structures.json` but not read by a function on this page.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `curr_op_slot_idx[4]` | +0 | `u32[4]` | per-stream op-slot cursor; `enc_post_operation` reads `[stream]` to assign `op_slot_idx` | HIGH |
| *(unordered_maps)* | +16 / +72 | `unordered_map` | `func_rel_op_idx` / `rel_op_idx` | MED |
| `all_posted_ops` | +128 | `vector<enc_operation*>` | begin=`word[16]`, end=`word[17]`; `(end-begin)>>3` = op count | HIGH |
| `posted_ops[4]` | +152 | `vector<enc_operation*>[4]` | per-stream posted ops; `post_op` appends `[stream_id]` | HIGH |
| `stream_id_map[12]` | +248 | `int[12]` | group_id → stream_id map (dword idx 62 base) | HIGH |
| `group_n` | +296 | `int` | replica-group count; range-checked in dispatch | HIGH |
| `state` | +300 | `enc_model_state_t` | PARSE=0/LOAD=1/START=2/STOP=3; signature requires PARSE | HIGH |
| `src_target_pairs` | +816 | `vector<enc_src_target_pairs_info>` | parsed source-target pairs (stride 80) | HIGH |
| `replica_groups` | +840 | `vector<enc_replica_group_info>` | parsed replica groups (stride 96); begin=`word[105]` | HIGH |
| `op_queue` | +864 | `vector<enc_ins*>` | pending op-list queue posted by `enc_post_function` | HIGH |
| `curr_function_id` / `function_n` | +888 / +892 | `u32` | current function cursor | MED |
| `functions` | +904 | `vector<enc_function_context>` | per-function stream/trigger tables | MED |
| `signature` | +992 | `uint8_t[16]` | per-rank op-signature MD5 (`enc_calculate_signature`) | HIGH |
| `comms[12]` | +1520 | `enc_comm[12]` | the bound communicators; stride 561 904 (140 476 dwords) | HIGH |
| `drv_ctx` | +6 753 584 | `encd_context*` | back-pointer to the driver context | HIGH |

The driver context that owns it is small and is the entry every public-API caller actually holds:

| `encd_context` field | Offset | Type | Meaning |
|---|---|---|---|
| `vcore` | +0 | `const virtual_core_t*` | `->nec_dev_id` used in every error log |
| `ccop_owner` | +16 | `bool` | this context owns the collective resources (guard on most entry points) |
| `streams_n` | +144 | `int` | active stream count |
| `enc_ctx` | +288 | `void*` (`enc_context*`) | **the object §1 documents** — every accessor dereferences this |
| `ctx_id` | +561 | `uint8` | NEFF comm (0) vs user comm (1) selector |

> **QUIRK —** the accessors index `enc_context` by **raw word indices**, not named fields, and the indices fold the member offset into a scale. `enc_get_comm_rank_n` reads `enc_ctx[140476 * comm_id + 384]` (a `_DWORD*` deref): `140476` dwords = 561 904 B = `sizeof(enc_comm)`, so that is `comms[comm_id]`; `+384` dwords = +1536 B = `comms[].ci.rank_n` (`comms` @+1520, `ci` @+8, `rank_n` @+8 → 1520+8+8 = 1536). A reimplementer reading the decompile must reverse this scale to recover the field; the magic constant `140476` is the comm stride, never a real index.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_post_operation` | `0x11f790` | central 13-case `enc_op_type` dispatcher (§2) | HIGH |
| `enc_context::post_op` | `0x10c750` | append `enc_operation` to `posted_ops[stream]` + `all_posted_ops` | HIGH |
| `enc_context::__post_op` | `0x105550` | 8-byte forwarder → `enc_operation::post` (the seed's mis-pin, ENG-1) | HIGH |
| `enc_post_function` | `0x124af0` | post all op-lists of a function; set first/last-in-function flags | HIGH |
| `enc_op_list::post` | `0x124aa0` | tail-call → `enc_post_operation(ctx->drv_ctx, this)` | HIGH |

---

## 2. The Central Dispatch (`enc_post_operation`)

### Purpose

`enc_post_operation @0x11f790` is the one function that turns a posted `enc_op_list` into a scheduled `enc_operation`. It validates the op, runs the six-gate `enc_can_post_*` feasibility battery, resolves exactly one `enc_alg_type` by a fixed precedence, switches the 13-value `enc_op_type` into a per-op-type handler that emits the op's INFO line and builds the descriptor, then posts it. The algorithm-resolution half (the battery and the precedence cascade) is owned by the taxonomy pages and is referenced, not re-derived, below; this page pins the **dispatch shape** — the 13-case jump table and the post sink — which is the part `enc_context` directly drives.

### Entry Point

```text
enc_fnc::post_instr (0x124cf0)                       ── per-function post
  └─ enc_post_function (0x124af0)                     ── iterate function's op_lists
       └─ enc_op_list::post (0x124aa0)                ── tail-call
            └─ enc_post_operation (0x11f790)          ── *** 13-case dispatch ***
                 ├─ enc_validate_operation            ── [boundary] op legality
                 ├─ enc_can_post_{mesh,single_cycle_ring,
                 │   inter_rdh,intra_rdh,kangaring,
                 │   hierarchical}_operation           ── [boundary] feasibility battery (6 gates)
                 ├─ <alg precedence cascade>           ── resolve one enc_alg_type (overview §2.1)
                 ├─ switch(enc_op_type) @0x12038a      ── 13 cases, default 0x1213c0
                 ├─ enc_operation::ctor                ── build the 248-B descriptor
                 └─ enc_context::post_op (0x10c750)    ── append to posted_ops[stream]/all_posted_ops
```

`enc_op_list::post_instr @0x124ac0` is the alternate entry that first marks `last_cc_op_in_function` on the final queued op-list, then tail-calls `enc_op_list::post`.

### Algorithm — the 13-case dispatch

The op-dispatch switch at `0x12038a` indexes the 13-value `enc_op_type` directly; each arm emits its INFO log, populates the op-type-specific fields of the `enc_operation`, and hands off to the resolved family composer. The cascade that fixes `alg` before the switch is the priority resolver documented on the sibling pages; reproduced here only as the call it makes.

```c
// enc_post_operation @0x11f790 — dispatch shape (op-dispatch switch @0x12038a)
function enc_post_operation(drv_ctx, op_list):                  // 0x11f790
    enc_ctx = drv_ctx->enc_ctx;
    if (!enc_validate_operation(op_list)):                      // "[nec_dev %d] op:%d failed to validate"
        return error;
    // group / stream / size context read from the op_list + replica group
    group_id   = op_list.group_id;                             // enc_op_list+16
    if (group_id < 0 || group_id >= enc_ctx->group_n):          // "Invalid group id: %d, group_n: %d"
        return error;
    stream_id  = enc_ctx->stream_id_map[group_id];              // +248 block; assert != UNINITIALIZED
    op_type    = op_list.ops[0].enc_op;                         // enc_op_args+0
    data_type  = ... ; priority_class = ... ;

    // --- feasibility battery: ALL six gates run unconditionally (overview §2.1) ---
    can_mesh        = enc_can_post_mesh_operation(...);
    can_kangaring   = enc_can_post_kangaring_operation(...);
    can_single      = enc_can_post_single_cycle_ring_operation(...);
    can_intra_rdh   = enc_can_post_intra_rdh_operation(...);
    can_inter_rdh   = enc_can_post_inter_rdh_operation(...);
    can_hier        = enc_can_post_hierarchical_operation(...);

    // --- precedence cascade: HIER > INTRA_RDH > MESH > KANGARING >
    //     SINGLE_CYCLE_RING > INTER_RDH > RING  (overview §2.1 / algorithm-taxonomy §2.1) ---
    alg = resolve_alg_by_precedence(can_hier, can_intra_rdh, can_mesh,
                                    can_kangaring, can_single, can_inter_rdh);

    // --- 13-case enc_op_type dispatch @0x12038a (default 0x1213c0 = unsupported) ---
    switch (op_type):
        case ALLGATHER(0):                // 0x122572  ring AG / mesh / hier
        case ALLREDUCE(1):                // 0x122bb6  ring AR (RS+AG) / mesh / hier / RDH
        case REDUCE_SCATTER(4):           // 0x121ebd  ring RS phase / mesh
        case SEND(5):                     // 0x123224  point-to-point
        case RECV(6):                     // 0x121958  point-to-point
        case ALLTOALL(7):                 // 0x120d97  assert alg == ENC_ALG_MESH
        case ALLTOALL_V(12):              // 0x120d97  (shares ALLTOALL) assert alg == MESH
        case PERMUTE(8):                  // 0x1207a6  permute composer
        case PERMUTE_IMPLICIT(10):        // 0x1207a6  (shares PERMUTE)
        case PERMUTE_REDUCE_IMPLICIT(11): // 0x121435  permute-reduce composer
            build_op_for_type(op_type, alg, ...);
            break;
        case BROADCAST(2):                // 0x1213c0  default -> unsupported
        case REDUCE(3):                   // 0x1213c0  default -> unsupported
        case PERMUTE_REDUCE(9):           // 0x1213c0  default -> unsupported on this path
        default:                          // 0x1213c0  "[nec_dev %d] not supported operation (op_type %d)"
            return NRT_INVALID;

    // build + post the descriptor (§2.1)
    enc_op = enc_operation::ctor({alg, data_type, channel_n, priority_class,
                                  copy_slice_sz, reduce_slice_sz, slice_n});
    enc_op->op_slot_idx = enc_ctx->curr_op_slot_idx[stream_id]++;   // +0 block
    return enc_context::post_op(enc_ctx, enc_op, stream_id);        // 0x10c750
```

The 13 jump-table targets are verbatim from `switches.json` (`func_addr 0x11f790`, `inst_addr 0x12038a`) and are identical to the roster pinned in [`overview.md` §3](overview.md):

| Op (`enc_op_type`) | Value | Dispatch target | Note |
|---|---|---|---|
| `ALLGATHER` | 0 | `0x122572` | ring AG / mesh / hier |
| `ALLREDUCE` | 1 | `0x122bb6` | ring AR (RS+AG) / mesh / hier / RDH |
| `BROADCAST` | 2 | `0x1213c0` (default) | unsupported on this path |
| `REDUCE` | 3 | `0x1213c0` (default) | unsupported on this path |
| `REDUCE_SCATTER` | 4 | `0x121ebd` | ring RS phase / mesh |
| `SEND` | 5 | `0x123224` | point-to-point |
| `RECV` | 6 | `0x121958` | point-to-point |
| `ALLTOALL` | 7 | `0x120d97` | mesh-only (`assert alg==MESH`) |
| `PERMUTE` | 8 | `0x1207a6` | permute composer |
| `PERMUTE_REDUCE` | 9 | `0x1213c0` (default) | unsupported on this path |
| `PERMUTE_IMPLICIT` | 10 | `0x1207a6` (shares `PERMUTE`) | permute composer |
| `PERMUTE_REDUCE_IMPLICIT` | 11 | `0x121435` | permute-reduce composer |
| `ALLTOALL_V` | 12 | `0x120d97` (shares `ALLTOALL`) | mesh-only |

> **GOTCHA —** the dispatch table maps **four** op-types onto the unsupported default `0x1213c0`: `BROADCAST(2)`, `REDUCE(3)`, and `PERMUTE_REDUCE(9)` join the `default` arm. `BROADCAST`/`REDUCE` are not standalone collective programs on this path (the ring per-channel dispatcher rejects them too), and `PERMUTE_REDUCE(9)` lands on the default while its *implicit* sibling `PERMUTE_REDUCE_IMPLICIT(11)` has its own arm `0x121435` — the reverse of the `PERMUTE`/`PERMUTE_IMPLICIT` pair (both → `0x1207a6`). A reimplementer cannot assume the explicit/implicit variants pair the same way across permute and permute-reduce.

> **QUIRK —** a *second* 13-case switch lives in the same function at `0x1242ef` (default `0x124943`). It is **not** a duplicate of the build dispatch — it is the per-op-type **teardown / final-INFO** dispatch (op-end paths), sharing the `enc_op_type` index but routing to the op-end blocks (`0x12470a..0x124937`). Both switches independently confirm the 13-value family; do not conflate their targets.

### Algorithm — the post sink

Once the descriptor is built, `enc_context::post_op @0x10c750` is the sink. It runs `__post_op` (the `enc_operation::post` forwarder, ENG-1) and, on success, appends the op pointer to two vectors: the per-stream `posted_ops[stream_id]` (@+152) and the global `all_posted_ops` (@+128). Both appends are the open-coded libstdc++ "fast path or `_M_realloc_append`" pattern:

```c
// enc_context::post_op @0x10c750
function post_op(enc_ctx, enc_op, stream_id):                  // 0x10c750
    nec_dev = enc_ctx->drv_ctx->vcore->nec_dev_id;
    if (__post_op(enc_ctx, enc_op) != 0):                      // 0x105550 -> enc_operation::post
        nlog("ENC","post_op",ERROR,"[nec_dev_id %d] failed to execute __post_op", nec_dev);
        return error;
    append(&enc_ctx->posted_ops[stream_id], enc_op);           // +152 block
    append(&enc_ctx->all_posted_ops,        enc_op);           // +128 — the table the accessors read
    return SUCCESS;
```

`all_posted_ops` is the table every op accessor (§3) indexes; `post_op` is its sole producer, so the `(end-begin)>>3` op count the accessors compute is exactly the number of ops `enc_post_operation` has posted.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_post_operation` | `0x11f790` | the 13-case dispatcher; runs battery + cascade + switch | HIGH |
| `enc_context::post_op` | `0x10c750` | descriptor sink → `posted_ops`/`all_posted_ops` | HIGH |
| `enc_post_function` | `0x124af0` | function-level post; first/last-in-function flagging | HIGH |
| `enc_op_list::post` / `post_instr` | `0x124aa0` / `0x124ac0` | virtual thin wrappers into the dispatcher | HIGH |
| *(switch)* `op-dispatch` | `@0x12038a` | 13 cases, default `0x1213c0` | HIGH |
| *(switch)* `op-teardown` | `@0x1242ef` | 13 cases, default `0x124943` | HIGH |

### Considerations

- **The alg cascade is referenced, not owned here.** The full `enc_can_post_*` predicates and the `HIER > INTRA_RDH > MESH > KANGARING > SINGLE_CYCLE_RING > INTER_RDH > RING` precedence are decoded on [`overview.md` §2.1](overview.md) and [`algorithm-taxonomy.md` §2.1](algorithm-taxonomy.md). This page pins only the dispatch-table half.
- **Per-op-type field population is the composer's, not the dispatcher's.** The arms set `channel_n`, `slice_n`, `copy_slice_sz`, `reduce_slice_sz`, and the permute chaining, but the descriptor-stream emission happens in the family composers ([Ring Scheduling](ring-scheduling.md), [Mesh Composer](mesh-composer.md), [Hierarchical and RDH](hierarchical-rdh.md)). Only the dispatch targets and the post sink are pinned here. *(field population: MED)*
- **All-to-all is mesh-exclusive.** `ALLTOALL(7)`/`ALLTOALL_V(12)` share `0x120d97` and hard-assert `alg == ENC_ALG_MESH` (`enc.cc:0xE4F`, `"alltoall cannot be supported without Mesh algorithm … rank_n(%d)"`). A ring-only topology cannot serve them.

---

## 3. The Operation Accessors

### Purpose

The read-mostly accessors index the posted-op table `all_posted_ops` (@+128) by `op_idx` and return one field of the indexed `enc_operation`. They are the profiler's and the NEFF-translator's window into the compiled program: branch-free leaves that clamp out-of-range / null to a sentinel and otherwise return a single dereference. Because they encode the exact `all_posted_ops` begin/end words and the `enc_operation` field offsets, they double as the authoritative layout witness for both.

### Algorithm — op-table indexing

Every op accessor shares the same guarded index of `all_posted_ops`: read begin (`enc_ctx[16]`), compute count as `(end - begin) >> 3` (the `>>3` is the pointer-array element size, 8 B), bounds-check `op_idx`, then dereference `begin[op_idx]` and read the field. `enc_get_operation_type` is the canonical form:

```c
// enc_get_operation_type @0xfee10  (the shared index pattern)
function enc_get_operation_type(drv_ctx, op_idx):              // 0xfee10
    enc_ctx = drv_ctx->enc_ctx;                                // encd_context+288
    if (op_idx < 0 || !enc_ctx):
        return ENC_OP_INVALID;                                 // sentinel 13
    begin = enc_ctx->word[16];                                 // all_posted_ops.begin (+128)
    count = (enc_ctx->word[17] - begin) >> 3;                  // (end - begin)/8
    if (op_idx >= count):
        return ENC_OP_INVALID;
    op = begin[op_idx];                                        // enc_operation*
    return *(enc_op_type*)(op + 16);                           // enc_operation.op_type @+16
```

The other op accessors differ only in the final field offset and sentinel. `enc_get_operation_comm_id @0xff090` chains one more deref — `op->comm(+8)->id(+80)` — and `enc_get_operation_num_elements @0xfefe0` is the one non-leaf: it sums `op->size_n(+208)` across the chained-op run, advancing `op_idx` until `instr_chaining(+216) == 3` (`ENC_INSTR_CHAINING_LAST`):

```c
// enc_get_operation_num_elements @0xfefe0 — accumulate over a chained-op run
function enc_get_operation_num_elements(drv_ctx, op_idx):      // 0xfefe0
    enc_ctx = drv_ctx->enc_ctx;
    if (!enc_ctx) return 0;
    if (op_idx < 0) return (size_t)-1;
    begin = enc_ctx->word[16]; count = (enc_ctx->word[17]-begin)>>3;
    if (op_idx >= count) return (size_t)-1;
    op = begin[op_idx];
    chaining = *(int*)(op + 216);                              // instr_chaining
    if (chaining == 0):
        return *(size_t*)(op + 208);                           // single op: size_n
    total = 0;
    while (true):                                              // chained run
        total += *(size_t*)(op + 208);                        // += size_n
        if (chaining == 3) break;                              // LAST
        if (++op_idx >= count) return (size_t)-1;
        op = begin[op_idx];
        chaining = *(int*)(op + 216);
    return total;
```

| Accessor | Address | `enc_operation` field | Offset | Sentinel | Confidence |
|---|---|---|---|---|---|
| `enc_get_operation_type` | `0xfee10` | `op_type` | +16 | `ENC_OP_INVALID` (13) | HIGH |
| `enc_get_operation_algorithm` | `0xfeee0` | `alg` | +20 | `ENC_ALG_INVALID` (11) | HIGH |
| `enc_get_operation_data_type` | `0xff0e0` | `data_type` | +24 | `SDMA_INVALID` (0) | HIGH |
| `enc_get_operation_comm_id` | `0xff090` | `comm(+8)->id(+80)` | chained | -1 | HIGH |
| `enc_get_operation_num_elements` | `0xfefe0` | Σ `size_n(+208)` over chain | +208/+216 | -1 | HIGH |
| `enc_get_operation_posted_status` | `0xfee60` | `instr_chaining(+216) <= 1` | +216 | logs on bad idx | HIGH |

> **NOTE —** `enc_get_operation_posted_status` does **not** return the field; it returns the *predicate* `instr_chaining <= 1` — i.e. "this op is the head of a chain (NONE or FIRST), not a continuation". A reimplementer who returns the raw `instr_chaining` value breaks the boolean contract the profiler expects. The companion `num_elements` (above) stops at `instr_chaining == 3` (LAST), so the two functions read the same field with opposite intents: head-detection vs run-summation.

### Considerations

- **`op_slot_idx` vs `op_idx`.** The accessor `op_idx` is the index into `all_posted_ops` (post order), distinct from the `enc_operation.op_slot_idx` (@+64) that `enc_post_operation` assigns from `curr_op_slot_idx[stream]`. The accessors take the post-order index; the device emit consumes the slot index.
- **Primary consumer is the profiler.** These accessors are called by `nrt_profile_get_model_collectives_ops_info` / `encd_profile_comm_info_query` to serialize op metadata into NTFF, and by the NEFF translator's `get_rank_n`. They are pure reads; nothing here mutates the context.

---

## 4. The Comm Accessors and the `comms[12]` Array

### Purpose

A NEFF binds at most 12 communicators per NeuronCore, stored inline as `enc_comm comms[12]` at `enc_context+1520`. The comm accessors return one `enc_comm_info` ("ci") field by `comm_id`. They are the same shape as the op accessors — guard, index, deref — but the index is a fixed-stride array walk rather than a vector, and the stride is the source of the `140476` magic constant.

### Algorithm — comm-array indexing

Each comm accessor guards `enc_ctx != null && comm_id <= 11`, then indexes `comms[comm_id]` by the dword stride `140476` (= `sizeof(enc_comm)` / 4 = 561 904 / 4) and adds the field's dword offset. `enc_get_comm_rank_n` is the canonical form:

```c
// enc_get_comm_rank_n @0xff190  (shared comm-index pattern)
function enc_get_comm_rank_n(drv_ctx, comm_id):               // 0xff190
    enc_ctx = drv_ctx->enc_ctx;                               // _DWORD* view
    if (enc_ctx && comm_id <= 11):
        return enc_ctx[140476 * comm_id + 384];               // comms[id].ci.rank_n
    return -1;
    // 140476 dwords  = 561904 B = sizeof(enc_comm)  -> comms[comm_id]
    // +384 dwords    = +1536 B  = comms@1520 + ci@8 + rank_n@8
```

The dword offset of each field decomposes as `(1520 + 8 + field_off) / 4`: `id@+80` → `(1520+80)/4 = 400`; `ci.rank@+8+4` → `(1520+12)/4 = 383`; `ci.rank_n@+8+8` → 384; `ci.local_rank_n@+8+12` → 385; `ci.node_n@+8+24` → 388.

| Accessor | Address | `enc_comm` field | Dword index | Confidence |
|---|---|---|---|---|
| `enc_get_comm_id` | `0xff130` | `id` (+80) | `140476·id + 400` | HIGH |
| `enc_get_comm_rank` | `0xff160` | `ci.rank` (+8+4) | `140476·id + 383` | HIGH |
| `enc_get_comm_rank_n` | `0xff190` | `ci.rank_n` (+8+8) | `140476·id + 384` | HIGH |
| `enc_get_comm_local_rank_n` | `0xff1c0` | `ci.local_rank_n` (+8+12) | `140476·id + 385` | HIGH |
| `enc_get_comm_node_n` | `0xff1f0` | `ci.node_n` (+8+24) | `140476·id + 388` | HIGH |
| `enc_get_comm_stream_id` | `0xff220` | `stream_id_map[id]` (+248) | dword 62 base | HIGH |

The `enc_comm` element these index has its `enc_comm_info` ("ci") inline at +8:

| `enc_comm` field | Offset | Type | `enc_comm_info` ("ci") field | Offset |
|---|---|---|---|---|
| `nccl_comm_node` | +0 | `enc_nccl_comm_node*` | `neuron_dev` | +0 |
| `ci` | +8 | `enc_comm_info` (72 B) | `rank` | +4 |
| `id` | +80 | `int` | `rank_n` | +8 |
| `stream_id` | +84 | `int` | `local_rank_n` | +12 |
| | | | `node_n` | +24 |

> **GOTCHA —** `enc_get_comm_stream_id @0xff220` carries a **dead branch**: after the `comm_id > 11 → return -1` guard, a folded `if (comm_id > 0xB) … stream_map_get_stream_id(...)` path remains but is unreachable (the earlier test already returned). It is a compiler remnant of an original two-path getter, not a live edge. A reimplementer should implement the live path only — `stream_id_map[comm_id]` from the `+248` block — and ignore the dead call. *(the dead-branch read is MED; the live path is HIGH.)*

### Considerations

- **`comms` vs the comm-bootstrap object.** `comms[12]` is the per-NC inline array these accessors read; it is distinct from the multi-node `enc_glb_comm` (size 564 408) built by [Comm Context and Bootstrap](comm-context.md). The `enc_comm_info` "ci" view here (rank / rank_n / node_n / `peers[]`) is the same shape composers read, but `comms[]` is the *bound* set, not the global.
- **`replica_subgroup_participants` is the one allocating accessor.** `enc_get_replica_subgroup_participants @0xff250` deep-copies `replica_groups[sg].participants` (@+40, stride 96) into a fresh buffer and clamps OOB `sg` to a Meyers-singleton empty vector — the only non-leaf in the accessor block. Its alloc/copy ordering is a [Comm Context](comm-context.md) concern.

---

## 5. Op-Size Arithmetic

### Purpose

A collective's input and output byte counts are not equal, and the ratio depends on the op-type: a reduce-scatter reads `rank_n ×` what it writes; an all-gather (and the all-reduce-class outputs) write `rank_n ×` what a single rank contributes. `enc_op_input_size @0xf9590` and `enc_op_output_size @0xf95e0` encode that per-op-type scaling — the byte-count math a reimplementer needs to size the source and destination tensors of any collective.

### Algorithm — per-op-type byte scaling

Both functions take `(size, rank_n, enc_op_type)` and return either `size` (identity) or `rank_n * size`, branching on the op-type. The decompile is exact:

```c
// enc_op_input_size @0xf9590 — input byte count per op-type
function enc_op_input_size(size, rank_n, op):                 // 0xf9590
    if (op == ENC_REDUCE_SCATTER):                            // 4
        return rank_n * size;                                 // RS reads rank_n shards
    if (op > ENC_REDUCE_SCATTER):
        if (op <= ENC_PERMUTE):                               // 5..8 (SEND..PERMUTE)
            return size;                                      // identity
        op -= 10;                                             // fold PERMUTE_IMPLICIT.. range
    if (op > ENC_ALLREDUCE):                                  // out of {ALLGATHER,ALLREDUCE}
        __assert_fail("…", "enc.cc", 0x5A);
    return size;                                              // ALLGATHER/ALLREDUCE input = size

// enc_op_output_size @0xf95e0 — output byte count per op-type
function enc_op_output_size(size, rank_n, op):                // 0xf95e0
    if (op == ENC_ALLREDUCE):                                 // 1 — output == size (in-place reduce)
        return size;
    if (op <= ENC_ALLREDUCE):                                 // ALLGATHER(0)
        return rank_n * size;                                 // AG writes rank_n shards
    if (op > ENC_PERMUTE):                                    // permute-implicit range
        if ((op - 10) <= 2): return size;                    // identity
        __assert_fail("…", "enc.cc", 0x6E);
    if (op <= ENC_REDUCE):                                    // BROADCAST(2)/REDUCE(3)
        __assert_fail("…", "enc.cc", 0x6E);
    return size;                                              // REDUCE_SCATTER..PERMUTE output = size
```

The two functions are duals on the `rank_n` factor: reduce-scatter is the *input*-scaled op (its input is `rank_n ×` its output); all-gather is the *output*-scaled op (its output is `rank_n ×` its input); all-reduce is identity in **both** because it reduces in place to the same size. The permute family is identity in both. The asserts (`enc.cc:0x5A` / `0x6E`) reject op-types outside the handled set.

| Op-type | input size | output size | Confidence |
|---|---|---|---|
| `ALLGATHER` (0) | `size` | `rank_n · size` | HIGH |
| `ALLREDUCE` (1) | `size` | `size` | HIGH |
| `REDUCE_SCATTER` (4) | `rank_n · size` | `size` | HIGH |
| `SEND`/`RECV`/`PERMUTE*` (5/6/8/10/11) | `size` | `size` | HIGH |
| `BROADCAST`/`REDUCE` (2/3) | asserts (unhandled) | asserts (unhandled) | HIGH |

### Considerations

- **`enc_op_input_size` has no in-band callers; `enc_op_output_size` is live.** `enc_op_output_size` is called by `enc_enqueue_operation` to size the output tensor; `enc_op_input_size` is a sibling utility with no direct caller in this slice (kept for symmetry). Both are reimplementation-grade math, grounded byte-for-byte in the decompile.
- **In-place detection is separate.** Whether a collective's input and output device-address ranges overlap (deciding the in-place fast path) is `enc_op_is_inplace @0xf9d90`, a 2-D strided-tile overlap test over `enc_tensor_t` operands — a distinct utility from the byte-count scaling here.

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_post_operation` (`@0x11f790`) | The 13-case dispatcher that mutates `enc_context` |
| `enc_context::post_op` (`@0x10c750`) | The descriptor sink into `posted_ops`/`all_posted_ops` |
| `enc_get_operation_*` (`@0xfee10..`) | The op-table accessors (post-order index) |
| `enc_get_comm_*` (`@0xff130..`) | The `comms[12]` accessors (fixed-stride index) |
| `enc_op_output_size` (`@0xf95e0`) | The per-op-type output-byte scaling consumed by `enc_enqueue_operation` |
| `enc_calculate_signature` (`@0x108220`) | Writes `enc_context.signature` (@+992) for cross-rank reconciliation |

## Cross-References

- [Overview: the Collective-Compute Architecture](overview.md) — the two-layer substrate and the end-to-end flow this dispatch feeds; §2.1 owns the alg-precedence cascade and §3 the op-dispatch roster
- [Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)](algorithm-taxonomy.md) — the `enc_alg_type` family the dispatcher resolves and the `enc_can_post_*` battery
- [Comm Context and Bootstrap](comm-context.md) — the global communicator (`enc_glb_comm`), replica-group parse, and the `comms[12]` binding the accessors read
- [The cc_op_entry On-Device ISA](cc-op-isa.md) — the device descriptor each posted `enc_operation` lowers into; the `enc_pattern_t` (`algo_type`) it carries
- [back to index](../index.md)
