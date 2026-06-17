# encd: DMA Descriptor and devmem Floor

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. Every `encd_*` and `__encd_*` symbol cited here resolves via `addr2line` to a single TU, `/opt/workspace/KaenaRuntime/tdrv/encd.c`, and is a `local` (`t`) symbol. `.text` VMA == file offset, so every `0x23…`/`0x24…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF- and symbol-anchored)** — function addresses, struct offsets, and `__assert_fail` strings are nm/`structures.json`/`strings.json`-verified. There are **zero** libnccom/`nccl*`/`neuron*`/`nec_*` edges from any function on this page; the only cross-binary boundary in the encd floor (`libncfw.so`) lives on the bring-up sibling, not here. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

This is the floor of the on-device collectives emitter: the layer that turns a single composed collective op-step into concrete al_udma descriptor streams, owns the device memory those streams read and write, and wires a mesh *event* into its DMA-trigger engines and rings. It sits one level below the descriptor-stream funnel mapped on [encd Overview](encd-overview.md) — the funnel (`encd_dma_mark_end`) closes out a SPAD op into the TopSP control stream; this page authors the *data-movement* half: the per-channel SDMA/CCE packets that op will trigger. The reference frame is a NIC's gather/scatter descriptor driver, with three responsibilities fused into one TU: a **descriptor emitter** (lower a copy/reduce step into `add_dma_packet`/`add_dma_packet_cce` calls against per-fold virtual rings), a **device-memory allocator** (thin `dmem_*` wrappers plus a lazy-reservation object that defers the real HBM allocation to first checkout), and a **mesh-event wiring pass** (allocate DMA-trigger engines, rings, and semaphores for one all-to-all event).

The descriptor emitter is a thin-over-heavy split. The composer's send/recv leaves ([enc Primitives](enc-primitives.md)) call thin wrappers — `encd_dma_copy @0x23cf80`, `encd_dma_reduce_copy @0x23e070`, the `_sb2sb` variants — that gate on a per-function skip flag, call the heavy worker, then bump the channel's descriptor counters. The heavy workers walk the channel's `fold_n` DMA folds, resolve the metaring neighbor and address-routing bits for each, and emit one `add_dma_packet` (copy) or `add_dma_packet_cce` (collective-compute-engine reduce) per fold range into that fold's vring. A central invariant threads every emitter: the per-channel `tp_inc_record.{m2s_val,s2m_val}` are `uint16` descriptor counters bumped by every tx/rx descriptor emitted, asserted to never cross `UINT16_MAX`, and required to end **equal across all folds** — the device runs all folds in lockstep, so an unequal count desynchronizes the collective. Short folds are padded to equal length with no-op descriptors before the op closes.

The devmem floor is the substrate the emitter and the composer allocate against. Four primitives wrap `dmem_alloc_aligned` for per-NeuronCore HBM (`encd_devmem_alloc @0x245e70`), and a 40-byte **reservation** object (`encd_devmem_reservation`) lets a caller declare a buffer's size and alignment up front and defer the real allocation until first checkout — so a model that may or may not touch a buffer pays no HBM until it does. The mesh-event wiring (`encd_init_mesh_event @0x24b830`) is where a composed all-to-all event becomes device resources: it computes the event's fold count, allocates its DMA-trigger engines and the maximum rings they need, and stamps the semaphore value the peers wait on. This page owns those four artifacts; the descriptor bitfields are [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md), the vring geometry is [Virtual Rings](../dma/virtual-rings.md), and the SPAD op-stream/channel-activation map is [encd Overview](encd-overview.md).

For reimplementation, the contract is:

- **The thin→heavy descriptor-emit split and the tp_inc discipline.** Every emit entry point is a gate-then-bump wrapper around a heavy per-fold worker; the heavy worker emits `add_dma_packet`/`add_dma_packet_cce` per fold and the wrapper accumulates `tp_inc_record.{m2s_val,s2m_val}` (`uint16`, `< UINT16_MAX`, equal across folds at op end). A reimplementer who emits per-vector instead of per-fold, or lets the per-fold counts diverge, breaks the lockstep contract — the device's `tx_descs_n[f] == tx_descs_n[f-1]` assertions are not advisory.
- **The lazy reservation object.** `encd_devmem_reservation` (40 B) carries `{size, alignment, mhandle=0, allocator, nec_dev_id}` and binds to either the per-TPB global allocator or the per-model allocator; the real `dmem_alloc_aligned` happens on first `encd_devmem_res_checkout`, idempotent if `mhandle` is already set, and PA/VA are read back through it. A reimplementer must reproduce the deferral, not allocate eagerly at reserve time.
- **The mesh-event device wiring.** `encd_init_mesh_event` lowers one composed event into `{num_folds, DMA-trigger engines, max rings, sem_val}` on the event's `dst_nbr_grp`. It is parameterized by the event type and the per-arch num-folds matrix; the composer fills the neighbor groups and event metadata, this function allocates the device resources behind them.

| | |
|---|---|
| **Emitter TU** | `tdrv/encd.c` (`/opt/workspace/KaenaRuntime`), GCC 14.2.1 — all `encd_*`/`__encd_*` `local` symbols |
| **Copy emit (thin)** | `encd_dma_copy @0x23cf80` → out-of-cell `__encd_dma_copy @0x238c80`; `encd_dma_copy_sb2sb @0x23ebb0` |
| **Reduce emit (thin)** | `encd_dma_reduce_copy @0x23e070` → `__encd_dma_reduce_copy @0x23d0c0`; `encd_dma_reduce_copy_sb2sb @0x23ec20` |
| **Reduce worker (heavy)** | `__encd_dma_reduce_copy @0x23d0c0` (834 insn, 102 bb) — per-fold CCE reduce + metaring routing |
| **SBUF dispatch core** | `__encd_dma_common_sb2sb @0x23e1b0` (611 insn) — partition-aware vs fallback path selection |
| **Completion mark** | `encd_dma_mark_completion @0x23ed00` — per-fold completion-semaphore packet (`PacketCompletion`) |
| **Fold padding** | `encd_compute_and_pad_dummy_dma_descriptors @0x23cf00` → `encd_pad_dummy_dma_descriptors @0x23cbb0` |
| **Descriptor counters** | `encd_dma_channel.tp_inc_record` @+2099692 — `{m2s_val:u16, s2m_val:u16}`, `< UINT16_MAX`, equal per-fold |
| **devmem alloc** | `encd_devmem_alloc @0x245e70` (default HBM) · `encd_devmem_alloc_hbm @0x24a4a0` (caller `hbm_idx`) |
| **Reservation object** | `encd_devmem_reservation` — 40 B; `mhandle @+16` (0 until first checkout), `allocator @+24` |
| **Reservation checkout** | `encd_devmem_res_checkout @0x24a810` (default HBM) · `…_rmtv_hbm @0x24a730` (RMTV bank) |
| **Host memory** | `encd_hostmem_alloc @0x24a980` (`HOST_DRAM` + `ndl_memory_map`) · `encd_hostmem_paddr @0x24a950` |
| **Mesh-event wiring** | `encd_init_mesh_event @0x24b830` — `{num_folds, DMA-trigger engines, max rings, sem_val}` |
| **Multi-node boundary** | **none at this layer** — every callee is `encd_arch_*`, a `vring_*`/`dmem_*` HAL, or libc |

---

## 1. The thin→heavy descriptor-emit split

### Purpose

A composer leaf knows *what* to move — a source/dest address vector, a size, and a peer neighbor — but not *how* the device lays that into descriptors. The emit floor is the lowering: it takes the leaf's op-params and authors al_udma packets into the channel's per-fold vrings. Every emit entry point is a **thin wrapper** that does three things — gate on a per-function skip flag, call a heavy worker that emits the packets, then bump the channel's descriptor counters — wrapped around one of three **heavy workers**: the out-of-cell `__encd_dma_copy @0x238c80` (plain copy, owned by a sibling cell), the in-cell `__encd_dma_reduce_copy @0x23d0c0` (CCE reduce), and the SBUF dispatch core `__encd_dma_common_sb2sb @0x23e1b0`.

The split exists so the *gate* and the *counter discipline* live in exactly one place per op-class, regardless of which composer leaf called in. The skip flag is the per-function unrolled/no-op latch: `*(ctx->functions + 12*function_id + 4)` — when a function is marked DMA-skipped, the wrapper returns without emitting, but a reimplementer must note the wrapper still exists so the *call shape* is uniform.

### Entry Point

```text
enc_primitive::{direct_copy,direct_pull,direct_recv,send,pull,rdh_copy}
  └─ encd_dma_copy (0x23cf80)                 ── gate + __encd_dma_copy + bump
       └─ __encd_dma_copy (0x238c80)          ── [out-of-cell] per-fold copy packets
            └─ add_dma_packet → vring_add_dma_packet_v2 (0x312440)

enc_primitive::{rdh_reduce,__recv_reduce_write,direct_reduce_send_*,__direct_fetch_send}
  └─ encd_dma_reduce_copy (0x23e070)          ── gate + __encd_dma_reduce_copy + bump
       └─ __encd_dma_reduce_copy (0x23d0c0)   ── per-fold CCE reduce, metaring routing
            ├─ get_neighbor_metaring / get_port_to_neighbor_for_metaring
            ├─ set_addr_routing_bits          ── [encd-arch-ops] cross-device addr bits
            └─ add_dma_packet_cce → vring_add_dma_packet_cce (0x3134e0)

enc_primitive::direct_send / direct_reduce_send_permute
  └─ encd_dma_copy_sb2sb (0x23ebb0) / encd_dma_reduce_copy_sb2sb (0x23ec20)
       └─ __encd_dma_common_sb2sb (0x23e1b0)  ── SBUF src/dst dispatch (partition-aware?)
            ├─ ideal path:    __encd_dma_copy / __encd_dma_reduce_copy   (per-fold)
            └─ fallback path: encd_dma_copy   / encd_dma_reduce_copy     (per-vector)
```

### Algorithm — the DMA-descriptor emit (copy/reduce into a vring)

The heavy reduce worker is the canonical shape. It walks a fold range, resolves the destination neighbor and routing bits per fold, and emits one CCE packet per fold range; the thin wrapper around it adds the gate and the counter bump. The plain-copy path is structurally identical with `add_dma_packet` in place of `add_dma_packet_cce`.

```c
// encd_dma_reduce_copy @0x23e070 — THIN wrapper (the copy variant @0x23cf80 is identical shape)
function encd_dma_reduce_copy(channel, op_params):
    ctx = channel->comm->ctx;                                  // encd_context* (comm + 0x3011F4C0)
    assert(channel && channel->comm && channel->comm->ctx);    // "channel && channel->comm && channel->comm->ctx"
    // (1) GATE: per-function DMA-skip latch
    fn_skip = *(ctx->functions + 12*ctx->curr_function_id + 4); // encd_function_info_t stride 12
    if fn_skip: return NRT_SUCCESS;                            // op marked unrolled/no-op
    // (2) OVERFLOW GUARD before any emission (assert @0x17BD/0x17CD/0x17CE)
    assert(UINT16_MAX - channel->tp_inc_record.m2s_val > tx_descs_n[0]);   // string @0x804f40
    assert(UINT16_MAX - channel->tp_inc_record.s2m_val > rx_descs_n[0]);   // string @0x804f80
    // (3) HEAVY: emit per-fold packets, returns per-fold tx/rx descriptor counts
    __encd_dma_reduce_copy(channel, op_params, &tx_descs_n[], &rx_descs_n[]);
    // (4) BUMP: accumulate the channel's descriptor counters
    channel->tp_inc_record.m2s_val += tx_descs_n[0];
    channel->tp_inc_record.s2m_val += rx_descs_n[0];
    return NRT_SUCCESS;

// __encd_dma_reduce_copy @0x23d0c0 — HEAVY per-fold CCE reduce-copy worker (834 insn, 102 bb)
function __encd_dma_reduce_copy(channel, op_params, out_tx_n[], out_rx_n[]):
    assert(channel->fold_n > 0);                               // "channel->fold_n > 0" @0x844d74
    assert(sdma_data_type_size[op_params.dtype] > 0);          // @0x804940
    // fold range [fold_start_idx, fold_end_idx) within the channel's fold_n folds
    assert(fold_end_idx <= channel->fold_n && total_folds <= channel->fold_n
           && fold_end_idx > fold_start_idx);                  // @0x8050e0
    for f in [fold_start_idx, fold_end_idx):
        vring = get_dma_queue_info(channel, f)->vring;         // per-fold queue's vring
        assert(vring != NULL);                                 // "vrings[%d] is null" @0x8049f8
        // resolve the metaring neighbor for this fold and validate its type
        s_neigh = get_neighbor_metaring(channel, op_params.src, f);   // assert valid type @0x8050b0
        d_neigh = get_neighbor_metaring(channel, op_params.dst, f);   // assert valid type @0x805080
        // stamp cross-device address-routing bits onto the dest addr (ports > 4)
        d_addr  = set_addr_routing_bits(d_neigh, d_addr_base);  // [encd-arch-ops]
        // emit one CCE reduce packet for this fold's slice; counts come back per fold
        ok = add_dma_packet_cce(ctx, vring, s_addr, d_addr, slice_sz,
                                SDMA_CCETYPE_ADD, &tx_n, &rx_n);
        assert(ok);                                            // "failed to add CCE packet" @0x805138
        add_dma_packet_profile_info(channel, f);               // profile tag
        out_tx_n[f] = tx_n; out_rx_n[f] = rx_n;
    // every fold must have produced the SAME descriptor count as fold 0 (lockstep)
    // (asserted by the caller chain; see §1.1)
```

The four-line wrapper is the whole reuse story: `encd_dma_copy`, `encd_dma_reduce_copy`, and both `_sb2sb` variants are the same gate-emit-bump skeleton with a different heavy worker plugged in. The `_sb2sb` wrappers add one step — they route through `__encd_dma_common_sb2sb` (§1.2) instead of calling the worker directly, because an on-chip State-Buffer source/dest needs its AXI ports resolved before the fold mapping is even known.

### 1.1 The tp_inc descriptor-counter discipline

The single invariant threaded through every emitter is the per-channel descriptor count. `encd_dma_channel.tp_inc_record` @+2099692 is an 8-byte `spad_slot_entry` whose first two `uint16` fields are the running tx/rx descriptor counts for the op currently being composed:

| Field | Offset (in `tp_inc_record`) | Type | Meaning | Confidence |
|---|---|---|---|---|
| `m2s_val` | +0 | `uint16` | tx (source/m2s) descriptors emitted so far this op | HIGH |
| `s2m_val` | +2 | `uint16` | rx (dest/s2m) descriptors emitted so far this op | HIGH |
| `repeat` | +4 | `uint16` | step repeat count | MED |
| `last_sync` | +6 (bit 0) | `bool` | terminating-sync flag | MED |

Three rules govern it, each enforced by a verbatim `__assert_fail` string:

1. **No overflow.** Before any emission, `UINT16_MAX - m2s_val > tx_descs_n[0]` and the s2m analog (`@0x804f40`/`@0x804f80`). The counters are 16-bit because the device's tp-inc semaphore field is 16-bit; a collective whose op-stream needs more than 65535 descriptors per channel is rejected, not wrapped.
2. **Equal across folds.** Every emitter asserts `tx_descs_n[f] == tx_descs_n[f-1] && rx_descs_n[f] == rx_descs_n[f-1]` (`@0x804c30`, `@0x805238`, `@0x804a18`). All `fold_n` DMA folds run in lockstep on the device, so each must emit the identical descriptor count; a short fold is padded (§1.3) before this assert.
3. **Zero at op start for mesh.** The mesh-reduce path additionally asserts `tp_inc_record.m2s_val == 0 && s2m_val == 0` (`@0x804e20`) at entry — a mesh event composes from a clean counter, not an accumulating one.

> **GOTCHA —** the counter is bumped by the *thin wrapper*, not the heavy worker, and only by `tx_descs_n[0]` / `rx_descs_n[0]` — fold 0's count. This is correct **only because** the equal-across-folds invariant holds: every fold has the same count, so fold 0 is representative. A reimplementer who emits unequal fold counts and then trusts the fold-0 bump will silently mis-account every channel after the first divergent op. The fold equality is the precondition that makes the single-element bump valid.

### 1.2 The SBUF dispatch core (`__encd_dma_common_sb2sb`)

When a copy's source or destination lives in on-chip State Buffer (SBUF), the fold mapping is not a simple metaring walk — the SB *partitions* must map cleanly onto the channel's folds for the partition-aware fast path, and fall back to a per-vector emit otherwise. `__encd_dma_common_sb2sb @0x23e1b0` is that decision:

```c
// __encd_dma_common_sb2sb @0x23e1b0 — SBUF src/dst dispatch core (611 insn)
function __encd_dma_common_sb2sb(channel, vectors[], vector_size, op_type):
    assert(vector_size > 0 && channel->fold_n > 0);            // @0x844ddc / @0x844d74
    // count how many src/dst addresses are in SBUF; must be all-or-nothing
    src_in_sb = count(v -> encd_is_address_in_sbuf(v.src));
    dst_in_sb = count(v -> encd_is_address_in_sbuf(v.dst));
    assert(src_in_sb == 0 || src_in_sb == vector_size);        // @0x805160
    assert(dst_in_sb == 0 || dst_in_sb == vector_size);        // @0x8051a0
    // test whether SB ports map cleanly onto folds (partition-aware "ideal" layout)
    if map_sb_ports_to_fold(channel, vectors) is clean:        // skip_partition_aware_dma_usage == 0
        for f in [0, channel->fold_n):                         // PER-FOLD fast path
            if op_type == DMA_OP_COPY:        __encd_dma_copy(channel, f, ...);
            else:                             __encd_dma_reduce_copy(channel, f, ...);
    else:                                                      // "SB layout not ideal..." @0x8051e0
        for v in vectors:                                      // PER-VECTOR fallback
            if op_type == DMA_OP_COPY:        encd_dma_copy(channel, v);       // re-enters thin wrapper
            else:                             encd_dma_reduce_copy(channel, v);
```

> **QUIRK —** the fallback path re-enters the **thin** wrappers (`encd_dma_copy`/`encd_dma_reduce_copy`), not the heavy workers — so the per-vector fallback re-runs the gate and the counter bump per vector, whereas the ideal path calls the heavy workers directly per fold and bumps once. A reimplementer who routes the fallback through the heavy worker double-skips the gate and mis-bumps the counter. The `skip_partition_aware_dma_usage` predicate (string `@0x9bd070`) names the latch; "SB layout not ideal for partition aware dma usage" (`@0x8051e0`, a DEBUG log) is the fallback's witness.

### 1.3 Fold padding and completion marking

Two closing operations make the per-fold counts equal and stamp the completion semaphore. `encd_compute_and_pad_dummy_dma_descriptors @0x23cf00` computes how many no-op descriptors a short fold needs from the difference of two tp_inc snapshots, then calls `encd_pad_dummy_dma_descriptors @0x23cbb0` to append that many `add_dma_packet` no-ops per fold:

```c
// encd_compute_and_pad_dummy_dma_descriptors @0x23cf00 (27 insn)
function encd_compute_and_pad_dummy_dma_descriptors(channel, m2s_prev, m2s_curr, s2m_prev, s2m_curr):
    // the tx and rx deficits must match — a copy is a paired tx+rx descriptor
    assert((m2s_prev - m2s_curr) == (s2m_prev - s2m_curr));    // @0x804fc0 (assert @encd.c:0x16CE)
    pad_n = m2s_prev - m2s_curr;                               // descriptors short of the longest fold
    encd_pad_dummy_dma_descriptors(channel, pad_n);            // append pad_n no-ops per fold

// encd_dma_mark_completion @0x23ed00 — terminal completion-semaphore packet
function encd_dma_mark_completion(channel):
    assert(channel->ring != NULL);                            // "DMA rings are null" @0x805218
    for f in [0, channel->fold_n):
        // write the completion sema value into compl_sema_id via one DMA packet
        ok = add_dma_packet(ctx, vring[f], sema_src, compl_sema_addr, ...);  // @0x803978 on fail
        add_dma_packet_profile_info(channel, f, PacketCompletion);
    assert(tx_descs_n[f] == tx_descs_n[f-1] && rx_descs_n[f] == rx_descs_n[f-1]);  // @0x805238
```

> **NOTE —** the padding deficit is asserted **symmetric** — `(m2s_prev - m2s_curr) == (s2m_prev - s2m_curr)` — because every emitted copy is a paired tx (m2s) + rx (s2m) descriptor ([descriptor-format §1](../dma/descriptor-format.md)), so the tx and rx counts always move together. An asymmetric deficit means a fold emitted a tx without its rx (or vice versa), which is a malformed op-stream the assert catches at compose time rather than at device run time. The completion mark writes the `compl_sema_id` (`encd_dma_channel @+2532`) the host-CC wait path later polls.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_dma_copy` | `0x23cf80` | thin copy wrapper: gate → `__encd_dma_copy` → bump | HIGH |
| `encd_dma_reduce_copy` | `0x23e070` | thin reduce wrapper: gate → `__encd_dma_reduce_copy` → bump | HIGH |
| `__encd_dma_reduce_copy` | `0x23d0c0` | heavy per-fold CCE reduce worker, metaring routing (834 insn) | HIGH |
| `__encd_dma_common_sb2sb` | `0x23e1b0` | SBUF dispatch: partition-aware vs fallback path | HIGH |
| `encd_dma_copy_sb2sb` | `0x23ebb0` | thin SBUF copy → `__encd_dma_common_sb2sb(DMA_OP_COPY)` | HIGH |
| `encd_dma_reduce_copy_sb2sb` | `0x23ec20` | thin SBUF reduce → `__encd_dma_common_sb2sb(DMA_OP_REDUCE_COPY)` | HIGH |
| `encd_dma_mark_completion` | `0x23ed00` | per-fold completion-semaphore packet (`PacketCompletion`) | HIGH |
| `encd_compute_and_pad_dummy_dma_descriptors` | `0x23cf00` | compute pad deficit (symmetric assert) | HIGH |
| `encd_pad_dummy_dma_descriptors` | `0x23cbb0` | append N no-op DMA packets per fold | HIGH |
| `__encd_dma_copy` | `0x238c80` | [out-of-cell] heavy per-fold plain-copy worker | HIGH |

> **CORRECTION (ENC-DMA-1) —** the IDA decompile renders `channel->comm->ctx` as the nonsense array walk `channel->comm[1].ring.channels[21].tp_inc_steps[81096].mark`. Disassembly shows this is a *fixed byte offset* `comm + 0x3011F4C0` holding the `encd_context*`, with the sibling `…[81095].mark` (`comm + 0x3011F4B8`) holding the `encd_stream*` — confirmed by the matching assert strings `"channel && channel->comm && channel->comm->ctx"`. Read every `comm[1]…[81096].mark` as `comm->ctx` and every `…[81095].mark` as `comm->strm`; the array-flatten is an IDA model artifact. *(field identity HIGH; exact intermediate `encd_comm` layout MED.)*

---

## 2. The devmem allocation floor

### Purpose

The descriptor streams of §1 read and write device HBM, and the composer above them allocates channel buffers, semaphore-value pools, and SPAD sections in that same HBM. The devmem floor is the allocator the whole collectives engine calls — a thin wrapper over the universal tdrv `dmem_*` allocator that adds a per-NeuronCore naming convention, a usage-type tag, and a default-HBM-index policy, plus a **lazy reservation** object that decouples "declare a buffer" from "allocate it".

### 2.1 The direct allocators

Four functions wrap `dmem_alloc_aligned` / `dmem_free` for device HBM, and three more for host DRAM. The `dmem_t` handle (192 B) they return is the universal tdrv allocation: PA at `_pa + align_offset` (`+24` + `+40`), VA at `_va + align_offset` (`+32` + `+40`).

```c
// encd_devmem_alloc @0x245e70 — per-NC HBM allocate (default hbm index)
function encd_devmem_alloc(ctx, allocator, size, alignment):
    hbm_idx = get_default_hbm_index(ctx);                     // arch-default bank
    name    = "encd-devmem-alloc-%d" % ctx->vcore->nec_dev_id;
    mh = dmem_alloc_aligned(allocator, size, alignment, TONGA_DRAM,
                            hbm_idx, DMA_MEM_USAGE_TYPE_CC, name);
    nlog("[nec_dev %u] allocated 0x%lx, size %ld", ...);      // @ string table
    return mh;
// encd_devmem_alloc_hbm @0x24a4a0 — identical but caller supplies hbm_idx (RMTV path)

// encd_hostmem_alloc @0x24a980 — page-rounded HOST_DRAM + ndl_memory_map
function encd_hostmem_alloc(ctx, size, alignment):
    mh = dmem_alloc_aligned(ctx->model_allocator, round_page(size), alignment,
                            HOST_DRAM, ..., "encd-hostmem-alloc-%d");
    if ndl_memory_map(mh->mem_handle, ...) fails:
        dmem_free(mh); return NRT_RESOURCE;                   // "Failed to map host memory (%d %s)"
    return { mhandle: mh, va: mh->_va + align_off, pa: mh->_pa + align_off };
// encd_hostmem_paddr @0x24a950 == handle->_pa(+24) + handle->align_offset(+40)
```

### 2.2 The lazy reservation object

The reservation defers the real allocation. `encd_devmem_reservation` is a 40-byte descriptor that records a buffer's size, alignment, and target allocator, with its `mhandle` field zero until first checkout — so a buffer that a given model never touches costs no HBM.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `size` | +0 | `size_t` | requested buffer size | HIGH |
| `alignment` | +8 | `size_t` | requested alignment | HIGH |
| `mhandle` | +16 | `dmem_t*` | **0 until first checkout**; the real allocation | HIGH |
| `allocator` | +24 | `dmem_allocator_t*` | per-TPB global (`tpb->tpb_allocator`) or per-model (`ctx->model_allocator`) | HIGH |
| `nec_dev_id` | +32 | `uint32_t` | owning NeuronCore id, for logging | HIGH |

```c
// encd_global_devmem_reserve @0x24a5c0 / encd_model_devmem_reserve @0x24a6b0 — declare, don't allocate
function encd_*_devmem_reserve(ctx, size, alignment):
    res = calloc(1, 0x28);                                    // 40 B, mhandle = 0 (verified @0x24a60c)
    res->size = size; res->alignment = alignment;
    res->allocator = (global) ? tpb->tpb_allocator           // assert "allocator != NULL" @0x2916
                              : ctx->model_allocator;         // model: assert ctx->ccop_owner @0x291C
    res->nec_dev_id = ctx->vcore->nec_dev_id;
    return res;

// encd_devmem_res_checkout @0x24a810 — first-touch allocation (idempotent)
function encd_devmem_res_checkout(res):
    if res->mhandle != NULL: return res->mhandle;             // already checked out — idempotent
    res->mhandle = encd_devmem_alloc(ctx, res->allocator, res->size, res->alignment);
    return res->mhandle;
// encd_devmem_res_checkout_rmtv_hbm @0x24a730: hbm_idx = encd_arch_get_rmtv_hbm_idx;
//   *first_checkout = (res->mhandle was NULL); encd_devmem_alloc_hbm(... hbm_idx)
// encd_get_pa_from_res @0x24a8d0 = res->mhandle->_pa + align_offset   (assert "mem_res && mem_res->mhandle")
// encd_get_va_from_res @0x24a910 = DMEM_GET_VA(res->mhandle)
```

> **QUIRK —** `encd_devmem_res_checkout` is **idempotent on `mhandle`** — a second checkout returns the existing handle, it does not allocate twice — but `encd_devmem_res_checkout_rmtv_hbm` additionally writes `*first_checkout` so the caller can run one-time RMTV bring-up only on the genuine first touch. A reimplementer who treats the two checkouts as interchangeable loses the first-touch signal the RMTV path needs. The model reservation asserts `ctx->ccop_owner` at reserve time (`@0x291C`): only the anchor TPB of a vcore holds the real comm state, so only it may bind a reservation to the per-model allocator.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_devmem_alloc` | `0x245e70` | per-NC HBM alloc, default bank, `DMA_MEM_USAGE_TYPE_CC` | HIGH |
| `encd_devmem_alloc_hbm` | `0x24a4a0` | as above, caller-supplied `hbm_idx` (RMTV) | HIGH |
| `encd_devmem_free` | `0x245e40` | `dmem_free` + NULL the slot | HIGH |
| `encd_total_allocated_devmem` | `0x24a570` | read per-NC devmem-in-use under lock | HIGH |
| `encd_global_devmem_reserve` | `0x24a5c0` | lazy reservation on per-TPB global allocator | HIGH |
| `encd_model_devmem_reserve` | `0x24a6b0` | lazy reservation on per-model allocator (`ccop_owner`) | HIGH |
| `encd_devmem_res_checkout` | `0x24a810` | first-touch alloc into default HBM (idempotent) | HIGH |
| `encd_devmem_res_checkout_rmtv_hbm` | `0x24a730` | first-touch alloc into RMTV bank, sets `*first_checkout` | HIGH |
| `encd_devmem_res_free` | `0x24a850` | free mhandle (if any) + free reservation + NULL | HIGH |
| `encd_get_pa_from_res` | `0x24a8d0` | device PA of a checked-out reservation | HIGH |
| `encd_get_va_from_res` | `0x24a910` | mapped VA of a checked-out reservation | HIGH |
| `encd_hostmem_alloc` | `0x24a980` | `HOST_DRAM` dmem + `ndl_memory_map` | HIGH |
| `encd_hostmem_free` | `0x24a960` | `dmem_free` a host mhandle (NULL-safe) | HIGH |
| `encd_hostmem_paddr` | `0x24a950` | host PA = `_pa(+24) + align_offset(+40)` | HIGH |

---

## 3. The mesh-event DMA wiring (`encd_init_mesh_event`)

### Purpose

A mesh all-to-all collective is composed as a fixed array of *events* — handshake, reduce-copy, broadcast, sync, copy-from-host — each describing which neighbor groups it touches and what it triggers ([Mesh Composer](mesh-composer.md)). `encd_init_mesh_event @0x24b830` is where one composed event becomes device resources: it computes the event's fold count, allocates the DMA-trigger engines and the maximum number of rings those engines need, and stamps the semaphore value the peers wait on. It is the device-side sink the composer's per-event leaves and the pod/switch initializers all bottom out into — one call per event, up to `ENC_MESH_MAX_NUM_EVENTS = 64` per subtype.

The event record it fills sits inside the channel's mesh subtype. The relevant slice:

| `encd_mesh_event` field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `abs_id` / `valid` | +0 / +4 | `int` / `bool` | event slot id; valid latch (skipped if clear) | HIGH |
| `src_nbr_grp` | +8 | `encd_mesh_nbr_grp` | source neighbor group (`num_neuron_devs`) | HIGH |
| `dst_nbr_grp` | +40 | `encd_mesh_nbr_grp` | dest group: `num_neuron_devs`, `num_drv_queue_idx`, `drv_queue_idx[][]` | HIGH |
| `valid_dma_trigger_evt` | +88 | `bool` | event arms a DMA-completion trigger | HIGH |
| `valid_direct_trigger_evt` | +89 | `bool` | event arms a direct trigger | HIGH |
| `valid_wait_evt` | +90 | `bool` | event waits on a peer semaphore | HIGH |
| `sem_val` | +92 | `uint32_t` | semaphore value peers wait on (stamped here) | HIGH |
| `evt_type` | +100 | `enc_mesh_event_type_t` | one of the 53-value mesh event enum | HIGH |
| `m2s_tp_inc_val[256]` | +104 | `uint32_t[]` | per-fold tx tp-inc values | HIGH |
| `s2m_tp_inc_val[256]` | +1128 | `uint32_t[]` | per-fold rx tp-inc values | HIGH |

### Algorithm

```c
// encd_init_mesh_event @0x24b830 — wire one composed mesh event to device resources
// (inlines encd_mesh_get_num_folds, mesh_init_dma_trigger_evt, mesh_alloc_dma_engine,
//  mesh_alloc_max_dma_rings)
function encd_init_mesh_event(drv_mesh, evt_id, evt_type, src_grp, dst_grp, sem_val):
    assert(evt_id < ENC_MESH_MAX_NUM_EVENTS);                 // < 64, "evt_id < ENC_MESH_MAX_NUM_EVENTS"
    mesh_subtype = drv_mesh->mesh_subtype[subtype_id];        // stride 2237488
    ctx = mesh_subtype->mesh->comm->ctx;                      // assert chain @0x8083c8
    event = &mesh_subtype->mesh_events[evt_id];               // stride 2152
    // (1) EMPTY event: type-less, no src/dst — record and return
    if evt_type == 0 && src_grp.num_neuron_devs == 0:
        nlog("Initialized empty evt %d"); event->valid = false; return;  // @0x808428
    assert(ctx->streams_n == 1);                              // "Mesh does not work with multiple streams" @0x8084f8
    // (2) NUM FOLDS: per-arch matrix keyed by (arch family, neighbor_n)
    num_folds = encd_mesh_get_num_folds(ctx, evt_type, src_grp, dst_grp);  // family 1/4; nbr 2/4/24
    assert(f == num_folds);                                   // fold loop consistency @0x84523b
    // (3) DIRECT-TRIGGER event: src/dst peers only, no DMA rings
    if event->valid_direct_trigger_evt:
        nlog("Mesh direct trigger evt %d num src %d num dst %d", ...);     // @0x808490
        event->sem_val = sem_val; return;
    // (4) DMA-TRIGGER event: allocate engines + the MAX rings they need across folds
    assert(dst_grp.drv_queue_idx && dst_grp.drv_queue_idx[0]);  // @0x808530
    engine = mesh_alloc_dma_engine(mesh_subtype, dst_grp, num_folds);  // per-fold DMA engine
    if mesh_alloc_max_dma_rings(mesh_subtype, engine, num_folds) fails:
        nlog("failed to allocate max number of DMA rings"); return NRT_RESOURCE;  // @0x808568
    mesh_init_dma_trigger_evt(event, engine, num_folds);
    event->sem_val = sem_val;
    nlog("Mesh dma trigger evt %d dma ring cnt %d", ...);     // @0x8085a0
    nlog("Mesh evt_id %d type %u sem_val=%d", evt_id, evt_type, sem_val);  // @0x8086d8
```

> **GOTCHA —** the event splits three ways on its trigger class, and only the **DMA-trigger** branch allocates rings. An *empty* event records `valid = false` and returns (a schedule slot with no work); a *direct-trigger* event stamps `sem_val` and returns with **no ring allocation** (it fires a semaphore directly, not via a DMA completion); only a *DMA-trigger* event runs `mesh_alloc_dma_engine` + `mesh_alloc_max_dma_rings`. A reimplementer who allocates rings for every valid event over-commits the DMA-engine bitmap and starves later events — the trigger class, not the `valid` bit, gates the allocation. The `valid_direct_trigger_evt` vs `valid_dma_trigger_evt` fields are set by the *composer* per event type ([Mesh Composer](mesh-composer.md)); this function only acts on them.

> **NOTE —** `encd_mesh_get_num_folds` is a per-arch matrix keyed by silicon family (1 or 4) and neighbor count (2, 4, or 24), inlined here — the exact matrix is opaque arch-table data (`encd_arch_get_mesh_max_folds` / `…_dma_engine_id_from_tbl`) and is summarized, not reconstructed, on this page *(num-folds matrix LOW; the fold-loop structure HIGH)*. The `mesh_alloc_dma_engine` / `mesh_alloc_max_dma_rings` helpers are inlined static functions identified by their assert strings (`@0x808530`, `@0x808568`); their internal DMA-engine-bitmap allocation is the same per-arch table machinery the ring channel init uses ([encd Overview §1.1](encd-overview.md)).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_init_mesh_event` | `0x24b830` | wire one event → `{num_folds, engines, rings, sem_val}` | HIGH |
| `encd_check_mesh_event_deferable` | `0x24cd10` | predicate: event deferable iff wait-only, no trigger | HIGH |
| `encd_mesh_get_num_folds` | inlined (name-string `@encd.c:0x2BBA`) | per-arch num-folds matrix | MED |
| `mesh_init_dma_trigger_evt` | inlined (name-string `@encd.c:0x2B32`) | bind engine + folds into the event record | MED |
| `mesh_alloc_dma_engine` | inlined (name-string `@encd.c:0x2ADC`) | per-fold DMA-engine bitmap allocation | MED |
| `mesh_alloc_max_dma_rings` | inlined (name-string `@encd.c:0x2A9C`) | allocate the max rings the engines need | MED |

> **NOTE —** the four `mesh_*` helpers above are **inlined** into `encd_init_mesh_event` and appear in the binary only as `.rodata` assert-name strings, not as distinct code addresses. They are cited as name-strings with their `encd.c` line tag, per the audit convention — a reimplementer should treat them as the four phases of the one large function, not as separately-callable entry points.

---

## 4. Per-metaring channel activation and tail accounting

### Purpose

A ring/kangaring channel emits into a *set* of per-fold DMA queues, and the device walks all folds in lockstep — so the emitter must keep two kinds of accounting straight: which folds a channel actually owns (its `fold_n` and the per-fold `drv_queues`), and how many descriptors each has emitted (the `tp_inc_record` of §1.1). The channel-*count* activation (how many of a metaring's channels run a given op) is owned by [encd Overview §4](encd-overview.md); this page owns the *per-channel, per-fold* tail-pointer accounting that the descriptor emit drives, and the channel's fold/queue layout the emit reads.

### The channel's fold and queue layout

The `encd_dma_channel` (2 099 736 B) is one giant per-channel object embedding the full ring set. The fields the emit floor reads:

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `fold_n` | +28 | `int` | number of DMA folds; the central emit loop bound (asserted `> 0`) | HIGH |
| `drv_queues` | +32 | `encd_dma_drv_queue_t[256]` | per-fold DMA-engine/queue handles | HIGH |
| `compl_sema_id` | +2532 | `uint16_t` | completion sema written by `encd_dma_mark_completion` | HIGH |
| `tp_inc_steps` | +2536 | `spad_slot_entry[262144]` | per-op tp-inc step records (also aliases `ctx`/`strm`, §1) | HIGH |
| `tp_inc_step_n` | +2099688 | `int` | count of tp-inc step records | HIGH |
| `tp_inc_record` | +2099692 | `spad_slot_entry` | the live `{m2s_val, s2m_val}` counters | HIGH |
| `comm` | +2099704 | `encd_comm*` | `→ctx`, `→strm` via fixed byte offset (§1 correction) | HIGH |
| `ring` / `kangaring` / `mesh` | +2099712 / +2099720 / +2099728 | `encd_alg_metaring*` / `encd_alg_mesh*` | the algorithm objects this channel belongs to | HIGH |

The tail-pointer accounting is the `tp_inc_record` counter pair walked through §1.1 plus the per-fold queue resolution: each emit iteration calls `get_dma_queue_info(channel, f)` to find fold `f`'s queue, reads its `vring` (`dma_queue_info_t @+320`), and emits into it. The fold count `fold_n` is fixed at channel init ([encd Overview §1.1](encd-overview.md)); the emit floor only reads it and asserts every loop stays within it (`fold_end_idx <= fold_n`, `@0x8050e0`).

> **QUIRK —** the channel carries a 262144-entry `tp_inc_steps` array (+2536) *and* a single live `tp_inc_record` (+2099692). The array is the per-op *history* (one `spad_slot_entry` per composed step, later dumped into the SPAD op-stream); the single record is the *running* tx/rx descriptor counter for the op being composed right now. A reimplementer who conflates them — bumping the array instead of the record, or sizing the record as an array — breaks both the lockstep check (§1.1) and the SPAD-slot dump ([encd Overview §3](encd-overview.md)). They share the `spad_slot_entry` layout but play different roles.

---

## 5. Verification notes

> The emitter split, the devmem floor, the mesh-event wiring, and the channel accounting were cross-checked against the IDA artifacts of `libnrt.so` 2.31.24.0:
> - Every address on this page resolves in `functions.json` and `addr2line`'s to `tdrv/encd.c` — all `local` (`t`) symbols. The emit cell (`encd_dma_copy @0x23cf80` … `encd_dma_mark_completion @0x23ed00`) and the devmem cell (`encd_devmem_alloc @0x245e70` … `encd_hostmem_alloc @0x24a980`) and the mesh-event cell (`encd_init_mesh_event @0x24b830`) are three contiguous nm-confirmed bands.
> - The `tp_inc_record.{m2s_val,s2m_val}` discipline (`uint16`, `< UINT16_MAX`, equal-across-folds) is anchored to the verbatim asserts `"UINT16_MAX - channel->tp_inc_record.m2s_val > tx_descs_n[0]"` (`@0x804f40`), `"tx_descs_n[f] == tx_descs_n[f - 1] && rx_descs_n[f] == rx_descs_n[f - 1]"` (`@0x805238`), and the symmetric pad assert `"(m2s_val_prev - m2s_val_curr) == (s2m_val_prev - s2m_val_curr)"` (`@0x804fc0`).
> - `encd_devmem_reservation` = 40 B with `mhandle @+16` is `calloc(1, 0x28)`-verified at `0x24a60c`; `encd_hostmem_paddr` = `_pa(+24) + align_offset(+40)` on `dmem_t` is objdump-confirmed.
> - `encd_init_mesh_event`'s event-record offsets (`evt_id*2152`, `sem_val @+92`, `evt_type @+100`) match the arithmetic in the decompiled body; the four inlined `mesh_*` helpers are cited as name-strings, not code addresses.
> - The **zero libnccom/`nccl*`/`neuron*`/`nec_*` edges** claim is a callgraph out-edge sweep over all functions on this page; every callee is `encd_arch_*`, a `vring_*`/`dmem_*`/`ndl_*` DMA HAL, the packet emitters (`add_dma_packet`/`add_dma_packet_cce`), or libc.
>
> **[MED]** The `comm->ctx` / `comm->strm` byte offset (`0x3011F4C0` / `0x3011F4B8`) inside `encd_comm` is disasm-confirmed at the emitter call sites, but the full `encd_comm` field layout (the IDA `comm[1]…mark` artifact) is model-derived; field *identity* is HIGH, the intermediate path is a Phase-2 item.
> **[MED]** The mesh num-folds matrix (`encd_mesh_get_num_folds`, family 1/4 × neighbor 2/4/24) and the DMA-engine-bitmap allocation inside `mesh_alloc_dma_engine`/`mesh_alloc_max_dma_rings` route through opaque `encd_arch_*` tables; the fold-loop *structure* is HIGH, the table *values* are not reconstructed here.
> **[LOW]** `DMA_OP_COPY = 0` / `DMA_OP_REDUCE_COPY = 1` and `SDMA_CCETYPE_ADD` are inferred from the op-type branch in `__encd_dma_common_sb2sb` and the reduce path, not from a named enum dump.

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_primitive::{send,direct_recv,direct_reduce_send_*}` | the composer leaves that call the thin emit wrappers ([enc Primitives](enc-primitives.md)) |
| `encd_dma_mark_end` (`@0x237200`) | the SPAD op-stream funnel that closes the op these packets belong to ([encd Overview §3](encd-overview.md)) |
| `add_dma_packet` (`@0x22ff20`) / `add_dma_packet_cce` (`@0x2307d0`) | the packet emitters the heavy workers call ([encd Overview](encd-overview.md)) |
| `vring_add_dma_packet_v2` (`@0x312440`) / `_cce` (`@0x3134e0`) | the al_udma vring sinks the packet emitters hand off to ([Virtual Rings §4](../dma/virtual-rings.md)) |
| `set_addr_routing_bits` (`@0x231340`) | the cross-device address-bit stamp the reduce worker applies ([Per-Arch Ops Dispatch](encd-arch-ops.md)) |
| `encd_init_mesh_event` (`@0x24b830`) | the device sink every mesh composer event bottoms out into ([Mesh Composer](mesh-composer.md)) |

## Cross-References

- [encd: Device-Side Descriptor Emitter](encd-overview.md) — the floor map, the `encd_context`, the SPAD op-stream hand-off, and the channel-*activation* accessors this page's accounting complements
- [encd: Semaphore and TopSP Bring-Up](encd-sema-topsp.md) — the semaphore pool, sema SOC-address resolution, and TopSP/NCFW bring-up the completion mark and mesh-event wiring depend on
- [Virtual Rings (vring) and Packet Builders](../dma/virtual-rings.md) — the per-channel virtual rings these emitters author into, and the dump that commits them
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the wire format of every packet the emit floor produces; owns the tx/rx-pair rule and every bit position
- [Mesh Composer (alg_mesh_initializer)](mesh-composer.md) — the host pass that fills the mesh events and neighbor groups `encd_init_mesh_event` wires to the device
- [back to index](../index.md)
