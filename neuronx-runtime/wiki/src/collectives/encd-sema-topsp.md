# encd: Semaphore and TopSP Bring-Up

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. Every `encd_*` symbol cited here resolves via `addr2line` to one of two TUs — `/opt/workspace/KaenaRuntime/tdrv/encd.c` (the device-resident driver) and `tdrv/encd/archs/arch.c` (the per-silicon ops shim) — and is a `local` (`t`) symbol. `.text`/`.rodata` VMA == file offset, so every `0x2…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF- and symbol-anchored)** — function addresses are `nm`-verified, struct offsets are `structures.json`/disassembly-cross-checked, and every `__assert_fail`/`nlog_write` string is dumped verbatim from `.rodata`. There are **zero** libnccom/`nccl*`/`neuron*`/`nec_*` edges from any function on this page (callgraph out-edge sweep). · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

This is the semaphore-and-TopSP companion to the [DMA/devmem floor](encd-dma-devmem.md). Where the devmem page authors the *data-movement* descriptors a collective triggers, this page owns the *control-plane* bring-up those descriptors synchronize against: how a composed metaring op becomes a TopSP scratch-pad (SPAD) control/slot config stream the on-device sync processor walks; how the per-channel semaphores that gate every send/recv/reduce are allocated, named, and resolved to MMIO addresses; and how the per-step NCFW (Neuron Collective FirmWare) barrier-config the TopSP firmware sequencer consumes is composed into each SP-engine's `encd_ncfw_configs_t` image. The reference frame is a NIC's doorbell-and-completion fabric: the SPAD op-stream is the command ring the sync core consumes, the semaphores are the doorbell/completion registers, and the NCFW barrier-config is the firmware's per-step wait/post table.

The page has three pillars. The **TopSP config emit** (`encd_populate_metaring_topsp_config @0x240bf0` → `prep_metaring_topsp_config @0x2331d0`) walks a metaring's channels and, per channel, emits SPAD slot/CCE-descriptor config and computes the TopSP register addresses the op-stream stamps — the SP base address plus a per-semaphore R/I/D offset resolved through the per-arch ops shim. The **semaphore floor** (`encd_get_sema_addr_by_id @0x230710`, `alloc_sp_semaphores @0x234aa0`, `alloc_sema_value @0x231580`, and the channel credit/recv-count accessors) is the allocator and address resolver: it turns a `(sp_idx, sema_id, is_increment)` triple into a BAR-mapped device virtual address `bar0_top_sp_0_offset + sp_sema_{i,d}_ofst`. The **NCFW barrier-config build** (`encd_prep_ncfw_barr_config @0x2555d0`, `encd_set_ncfw_start_network_proxy_trigger @0x255530`, `encd_get_leader_sps_barrier_done_semas_inc_addrs @0x240d40`) writes the firmware's per-step barrier-sema bit table and arms the TopSP→network-proxy trigger.

The TopSP *register-addressing scheme* — the three arch-specific MMIO base layouts and the start/stop/basic-block-switch trigger offsets — is the bedrock all three pillars stand on, so it gets its own section (§1). The TopSP config *materialization* into HBM (the `encd_prep_topsp_config_{start,end}` NEFF→TopSP build and the `encd_start_executable` DMA-load) is summarized here and owned in detail by the bring-up; this page documents the *emit* and *addressing*, not the executable launch state machine. The kernel-side TopSP notification path is [kernel/topsp](../kernel/topsp.md); the firmware that consumes the barrier-config is [The NCFW Sequencer](../firmware/ncfw-sequencer.md) and the per-arch dumpers that reflect it are [Serializer Families](../firmware/serializer-families.md).

For reimplementation, the contract is:

- **The TopSP MMIO register-addressing scheme.** A TopSP's control registers live at a per-arch base — `0xFFFD0200000 + (idx << 22)` on one generation, `0x8280200000`/`0x808280200000 + ((idx % 8) << 30)` on the others — and a per-register MMIO offset (`host_trigger`/`stop_signal`/`basic_block_switch`) comes from the arch ops shim. Sema addresses use a *different* path: a BAR-mapped `bar0_top_sp_0_offset` plus a `(sp_idx, sema_id)`-indexed `sp_sema_{r,s,i,d}_ofst`. A reimplementer who conflates the two address spaces (raw MMIO trigger regs vs BAR-mapped sema vaddrs) mis-targets every doorbell.
- **The `(sp_idx, sema_id, is_increment)` → address resolution.** `encd_get_sema_addr_by_id` is the one choke point: it selects the *increment* offset (`sp_sema_i_ofst`) or the *decrement/direct* offset (`sp_sema_d_ofst`) by whether `sema_id == 0xFF`, then adds the bar0 TopSP-0 base. Every channel sema accessor (recv-count, send-credit, event sema, barrier-done) funnels through it.
- **The NCFW per-step barrier-config table.** Each barrier step writes `barrier_step_config[*].barrier_sema[*].addr.soc_addr` (an SP base + sema-R offset) and sets per-op semaphore bits, rejecting out-of-range and double-set bits. The trigger-arming and leader-barrier-done sema collection sit beside it. A reimplementer must reproduce the bit-validation, not just the address stamp.

| | |
|---|---|
| **Driver TU** | `tdrv/encd.c` (`/opt/workspace/KaenaRuntime`), GCC 14.2.1 — all `encd_*` `local` symbols |
| **Arch ops shim TU** | `tdrv/encd/archs/arch.c` — `encd_arch_*` tail-jumps through the 744-B ops table `s @0xc96d20` |
| **Metaring TopSP config** | `encd_populate_metaring_topsp_config @0x240bf0` → `prep_metaring_topsp_config @0x2331d0` (3.6 KB) |
| **Sema address resolver** | `encd_get_sema_addr_by_id @0x230710` — `bar0_top_sp_0_offset + sp_sema_{i,d}_ofst` |
| **Sema pool alloc** | `alloc_sp_semaphores @0x234aa0` · `alloc_sema_value @0x231580` · `free_channel_semaphores @0x232870` |
| **Channel sema accessors** | `encd_get_event_sema_iaddr @0x238590` · `get_channel_recv_cnt_sema_addr @0x2428b0` · `get_channel_send_credit_sema_addr @0x232bd0` |
| **NCFW barrier-config** | `encd_prep_ncfw_barr_config @0x2555d0` — writes `barrier_step_config[*].barrier_sema[*].addr.soc_addr` |
| **Proxy-trigger arm** | `encd_set_ncfw_start_network_proxy_trigger @0x255530` — writes `spe_config + 0x5b7c` |
| **Leader barrier-done semas** | `encd_get_leader_sps_barrier_done_semas_inc_addrs @0x240d40` — sema idx `host_cc ? 6 : 2` |
| **Start/stop trigger regs** | `enc_get_start_topsp_reg_addrs @0x235fe0` · `enc_get_stop_topsp_reg_addrs @0x236180` · `…basic_block_switch @0x236320` |
| **Arch TopSP-0 BAR offset** | `encd_arch_get_bar0_top_sp_0_offset @0x2559a0` (ops slot `s+0x68`) |
| **Multi-node boundary** | **none at this layer** — every callee is `encd_arch_*`, a HAL, or libc |

---

## 1. The TopSP register-addressing scheme

### Purpose

A TopSP (top sync processor) is the on-device sequencer that walks the SPAD op-stream and drives the DMA engines of one collective. To program one, the runtime needs two distinct address spaces, and confusing them is the single most common reimplementation trap. The first is the **raw MMIO control band**: each TopSP's trigger/stop/basic-block-switch registers live at a fixed physical base computed from the TopSP index and the silicon generation. The second is the **BAR-mapped semaphore band**: a TopSP's semaphores are addressed as device virtual addresses through a BAR window, not at their raw MMIO offset. §1 owns the first; §3 the second.

### The MMIO base computation

The control-register base for TopSP `idx` is a function of the arch type (`al_hal_tpb_get_arch_type()`), confirmed byte-identical across the three sibling reg-addr emitters `enc_get_start_topsp_reg_addrs @0x235fe0`, `enc_get_stop_topsp_reg_addrs @0x236180`, and `encd_get_basic_block_switch_topsp_reg_addrs @0x236320` (they differ only in the final per-register offset helper):

```c
// shared base in enc_get_{start,stop,basic_block_switch}_topsp_reg_addrs (0x235fe0 / 0x236180 / 0x236320)
// asserted: top_sp_idx < tdrv_arch_get_num_topsp()  ("encd_get_top_sp_base_addr", encd.c:0x304)
function topsp_base(idx, arch):
    if arch == 2:                                   // one generation (Trn2-class)
        return 0xFFFD0200000 + (idx << 22);         // 4 MiB stride per TopSP
    else:                                           // other generations
        if idx < 8:  base = 0x8280200000;
        else:        base = 0x808280200000;
        return base + ((idx % 8) << 30);            // 1 GiB stride within an 8-TopSP block

// each emitter then adds a per-register MMIO offset from the arch ops shim:
function enc_get_start_topsp_reg_addrs(drv_ctx, out[], max):
    assert(drv_ctx && drv_ctx->enc_ctx);            // "drv_ctx", "drv_ctx->enc_ctx"
    assert(max >= drv_ctx->sp_engine_n);            // "max_num_start_regs >= drv_ctx->sp_engine_n"
    for i in [0, drv_ctx->sp_engine_n):
        idx     = drv_ctx->sp_engines[i].sp->idx;   // top_sp_t.idx @+0
        out[i]  = topsp_base(idx, arch)
                + aws_hal_sp_topsp_get_host_trigger_reg_offset();      // "start"
    // _stop  variant: + aws_hal_sp_topsp_get_stop_signal_reg_offset()
    // _bbsw  variant: + aws_hal_sp_topsp_get_basic_block_switch_reg_offset()
```

The stop path has a fallback: `stop_sp_engines @0x236770` either calls `aws_hal_sp_topsp_set_stop_signal(sp_mem_handle)` for arch ∈ {2,3,4}, or, for the generic path, does a raw `al_reg_write32` of value `1` to `bar0 + top_sp_0_offset + sp_sema_i_ofst` — i.e. it *posts a semaphore* to stop the engine when no HAL stop primitive exists. Unknown arch → `NRT_FAILURE` (string `"Failed to set stop_signal local reg (topsp %d): unknown architecture"`).

> **QUIRK —** the arch==2 generation uses a `<< 22` (4 MiB) TopSP stride from a single base, while the other generations split at index 8 into two bases with a `<< 30` (1 GiB) intra-block stride and an `idx % 8` wrap. A reimplementer who assumes a uniform stride, or who forgets the `idx >= 8` base switch, addresses the wrong TopSP for every engine past the eighth on the non-arch-2 silicon. The three base constants `0xFFFD0200000` / `0x8280200000` / `0x808280200000` are objdump-confirmed identical in all three reg-addr siblings.

> **NOTE —** which arch *enum* (1/2/3/4) maps to which Neuron generation is only partially pinned: `al_hal_tpb_get_arch_type() == 2` selects the `<< 22` scheme (the `encd_get_coretype @0x230be0` switch tests arch ∈ {2,3,4} and a `tdrv_get_hw_revision`), and `encd_get_coretype` returns coretype `0xC` or `0x5` on its branches *(arch→generation mapping MED; the address arithmetic HIGH)*. The per-register MMIO offsets (`host_trigger`/`stop_signal`/`basic_block_switch`) are opaque `aws_hal_sp_topsp_*` HAL returns, summarized not reconstructed.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_get_start_topsp_reg_addrs` | `0x235fe0` | per-engine `host_trigger` ("start") MMIO reg addrs | HIGH |
| `enc_get_stop_topsp_reg_addrs` | `0x236180` | per-engine `stop_signal` MMIO reg addrs | HIGH |
| `encd_get_basic_block_switch_topsp_reg_addrs` | `0x236320` | per-engine `basic_block_switch` MMIO reg addrs | HIGH |
| `enc_get_num_topsps` | `0x235ec0` | `drv_ctx->sp_engine_n` (TopSP count) | HIGH |
| `enc_get_topsp_for_index` | `0x235f40` | `drv_ctx->sp_engines[idx].sp` (no bounds check) | HIGH |
| `stop_sp_engines` | `0x236770` | HAL stop-signal or raw `al_reg_write32(1)` sema-post fallback | HIGH |
| `encd_get_coretype` | `0x230be0` | arch-type switch → coretype `0xC`/`0x5` | MED |
| `encd_arch_get_bar0_top_sp_0_offset` | `0x2559a0` | BAR-window TopSP-0 base (ops slot `s+0x68`) | HIGH |

---

## 2. The TopSP config emit (`encd_populate_metaring_topsp_config`)

### Purpose

`encd_populate_metaring_topsp_config @0x240bf0` is the metaring entry into the TopSP config materialization: given a ring/kangaring metaring object, it validates the object and dispatches into the per-channel SPAD config builder `prep_metaring_topsp_config @0x2331d0`. That builder is the densest device-programming function in the sema/TopSP floor (≈3.6 KB, `0x2331d0..0x234000`): for each active channel it emits the channel's SPAD slot/CCE descriptor config, resolves the TopSP sema-R offsets the op-stream stamps, allocates the sema *values* the barrier increments, and stamps the cross-device address-routing and APB-broadcast bits.

### Entry Point

```text
enc metaring composer
  └─ encd_populate_metaring_topsp_config (0x240bf0)        ── validate + dispatch
       │   assert metaring!=NULL / ctx!=NULL / comm!=NULL  ── 0x844e7b / 0x8466d7 / 0x8413fc
       └─ prep_metaring_topsp_config (0x2331d0)            ── per-channel SPAD/CCE config emit
            ├─ encd_arch_get_sp_base_addr        (x12)     ── TopSP SP base for sema addrs
            ├─ encd_arch_get_sp_sema_r_ofst      (x10)     ── per-sema R offset (op-stream wait)
            ├─ encd_arch_get_sp_sema_i_ofst      (x2)      ── per-sema I offset (increment)
            ├─ alloc_sema_value.part.0           (x2)      ── reserve the barrier sema value
            ├─ get_dma_queue_info / add_dma_packet_cce     ── CCE-reduce slot descriptors
            ├─ set_addr_routing_bits             (x5)      ── cross-device dest addr bits
            └─ encd_arch_get_apb_bcast_{m2s,s2m}_offsets/_mask  ── APB broadcast offsets
       (on failure: nlog "[nec_dev %u] Failed to prepare mesh top sp config..."  @0x805700)
```

### Algorithm — the per-channel config emit

```c
// encd_populate_metaring_topsp_config @0x240bf0 — metaring → per-channel TopSP config
function encd_populate_metaring_topsp_config(metaring, ctx, comm):
    assert(metaring != NULL);                          // @0x844e7b
    assert(ctx != NULL);                               // @0x8466d7 ("ctx != NULL")
    assert(comm != NULL);                              // @0x8413fc ("comm != NULL")
    for ch in metaring->channels[0 .. channel_n]:      // channels[32], stride 2099736
        assert(ch.id < metaring->channel_n);           // "channel->id < ring->channel_n" @0x844b31
        if not encd_ring_is_channel_active(ch): continue;   // skip inactive (see encd-overview §4)
        if prep_metaring_topsp_config(ctx, ch, ...) != NRT_SUCCESS:
            nlog("[nec_dev %u] Failed to prepare mesh top sp config...");  // @0x805700
            return NRT_FAILURE;
    return NRT_SUCCESS;

// prep_metaring_topsp_config @0x2331d0 — emit one channel's SPAD/CCE config + sema addrs
function prep_metaring_topsp_config(ctx, channel, ...):
    strm = channel->comm->strm;                        // comm + 0x3011F4B8 (see §6 correction)
    assert(strm->sp_engines[0] != NULL);               // "strm->sp_engines[0] != NULL" @0x8035f0
    assert(qb_id < ENCD_MAX_DMA_QUEUE_...);            // "qb_id < ENCD_MAX_DMA_QUEUE_..." @0x8037d0
    nlog("Total no of channels for sp ...");           // @0x803688 (DEBUG)

    sp_base = encd_arch_get_sp_base_addr();            // TopSP SP base for this engine
    // (1) for each per-fold DMA queue: emit a CCE-reduce slot descriptor
    for f in [0, channel->fold_n):
        qinfo = get_dma_queue_info(channel, f);
        // resolve the metaring neighbor's TopSP sema-R offset (op-stream WAIT target)
        nbr_config = get_neighbor_metaring(channel, f);
        assert(nbr_config->type != NEIGHBOR_...);      // "nbr_config->type != NEIGH..." @0x8037f8
        assert(prev.nec_dev_id != NEC_POD_...);        // "(prev.nec_dev_id != NEC_POD..." @0x803840
        sema_r = sp_base + encd_arch_get_sp_sema_r_ofst(bar_ofst, sp_idx, sem_id);
        // (2) reserve the barrier sema VALUE this fold increments (devmem-backed)
        sema_val_addr = alloc_sema_value(channel, ...); // 0x231580
        sema_i = sp_base + encd_arch_get_sp_sema_i_ofst(bar_ofst, sp_idx, sem_id);
        // (3) stamp cross-device routing bits onto the dest sema addr (ports > 4)
        d_addr = set_addr_routing_bits(nbr_config, sema_i);
        // (4) emit the CCE-reduce slot descriptor into this fold's vring
        add_dma_packet_cce(ctx, qinfo->vring, ..., d_addr, ...);
    // (5) APB broadcast offsets for the multi-engine sync fan-out
    m2s  = encd_arch_get_apb_bcast_m2s_offsets(...);
    s2m  = encd_arch_get_apb_bcast_s2m_offsets(...);
    mask = encd_arch_get_apb_bcast_mask(...);
    return NRT_SUCCESS;
```

> **QUIRK —** the TopSP config emit uses **three different sema offsets for the same channel**: `sp_sema_r_ofst` for the op-stream's *wait* on a neighbor's arrival, `sp_sema_i_ofst` for the local *increment-on-completion* the barrier posts, and (via the stop path, §1) `sp_sema_i_ofst` again as the engine *stop* doorbell. They are distinct register windows behind one `sp_base`. A reimplementer who computes one offset and reuses it for wait, post, and stop will deadlock the sync core — the firmware waits on R, the DMA posts to I, and the two never meet. The R/I/D taxonomy is the arch-ops shim's (§3); this function only selects which one each descriptor needs.

> **NOTE —** the channel-*activation* (which channels are active, `encd_ring_is_channel_active`) and the SPAD op-stream *funnel* (`encd_dma_mark_end`) are owned by [encd Overview §3–4](encd-overview.md); this function is the *config populate* that runs before the op-stream is walked, not the op-stream emit itself. The CCE-reduce descriptors it emits go through the same `add_dma_packet_cce` / vring sink the [DMA/devmem floor](encd-dma-devmem.md) owns — cited here as the call edge, derived there.

### TopSP config materialization (summary)

The config `prep_metaring_topsp_config` populates is later *materialized* into the device — DMA-copied into HBM as each SP-engine's `encd_ncfw_configs_t` image, then DMA-loaded to SRAM/IRAM at executable start. That two-halves build (`encd_prep_topsp_config_start @0x240df0` / `encd_prep_topsp_config_end @0x2429b0`, both called from `enc_load_operations`) and the launch (`encd_start_executable @0x2431c0`) are the *bring-up* and are documented as the materialization path, not re-derived here. The relevant handshake for this page: `_start` programs the trigger/trigger_next/complete/complete_prev sema SOC addresses and the host-vs-device barrier select; `_end` builds the device-barrier `dma_sync` sema set across stream leaders (`NEFF_DEVICE_BARRIER_MAX_LEADERS = 4`) and DMA-copies the nx-dram configs to HBM via `encd_devmem_copyin @0x242970`.

| Materialization function | Address | Role | Confidence |
|---|---|---|---|
| `encd_prep_topsp_config_start` | `0x240df0` | NEFF→TopSP build half 1: trigger/complete sema SOC addrs, barrier select, SPAD slot bases | HIGH |
| `encd_prep_topsp_config_end` | `0x2429b0` | half 2: desc_count accumulate, device-barrier `dma_sync` set, HBM config copy-in | HIGH |
| `encd_start_executable` | `0x2431c0` | executable launch: CTRL/SLOT spad DMA-load + per-TopSP NX-DRAM config write + host trigger | HIGH |
| `encd_devmem_copyin` | `0x242970` | DMA a config buffer into a device `mhandle` (`ndl_memory_buf_copyin`) | HIGH |

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_populate_metaring_topsp_config` | `0x240bf0` | validate metaring/ctx/comm → per-channel dispatch | HIGH |
| `prep_metaring_topsp_config` | `0x2331d0` | per-channel SPAD/CCE config + sema-addr emit (3.6 KB) | HIGH |
| `encd_get_start_proxy_task_host_mem_addrs` | `0x240cf0` | collect per-stream proxy-task host-mem addrs (adjacent sibling) | HIGH |
| `add_dma_packet_cce` | `0x2307d0` | CCE-reduce descriptor packer the config emit calls ([devmem floor](encd-dma-devmem.md)) | HIGH |
| `set_addr_routing_bits` | `0x231340` | cross-device dest-addr bit stamp ([arch ops](encd-arch-ops.md)) | HIGH |

---

## 3. The semaphore floor

### Purpose

Every collective synchronization point — a fold's completion increment, a send-credit grant, a recv-count post, a barrier-done arrival — is a TopSP semaphore. The semaphore floor is the layer that (a) *allocates* the per-channel sema id pool, (b) *reserves* the devmem-backed sema *value* a DMA increments, and (c) *resolves* a `(sp_idx, sema_id, is_increment)` triple to the BAR-mapped device virtual address the descriptor or the firmware targets. The whole floor funnels its address resolution through one function, `encd_get_sema_addr_by_id @0x230710`.

### 3.1 The address resolver (`encd_get_sema_addr_by_id`)

The resolver is the choke point. It reads the per-NC `encd_context` through `comm`, asks the arch shim for the device handle and the BAR-window TopSP-0 base, then chooses the *increment* offset (`sp_sema_i_ofst`) or the *decrement/direct* offset (`sp_sema_d_ofst`) by whether the sema id is the sentinel `0xFF`:

```c
// encd_get_sema_addr_by_id @0x230710 — (sp_idx, sema_id, is_increment) → BAR-mapped device vaddr
function encd_get_sema_addr_by_id(comm, sp_idx, sema_id, is_increment):
    ctx     = *(comm + 0x3011F4C0);                    // encd_context* (see §6 correction)
    dev     = encd_arch_get_mla_device(ctx->vcore->nec_dev_id);  // 0x255ad0
    if dev == NULL:
        nlog("[nec_dev %u] failed to get mla ...");    // @0x802c98
        return 0;
    bar0_topsp0 = encd_arch_get_bar0_top_sp_0_offset();           // 0x2559a0 (ops s+0x68)
    // sentinel 0xFF selects the decrement/direct view; else the increment view
    if sema_id == 0xFF:                                           // cmp $0xff,%bx
        sema_ofst = encd_arch_get_sp_sema_d_ofst(bar0_topsp0, sp_idx, sema_id);  // 0x2558f0
    else:
        sema_ofst = encd_arch_get_sp_sema_i_ofst(bar0_topsp0, sp_idx, sema_id);  // 0x2558e0
    return dev->bar0_base + bar0_topsp0 + sema_ofst;
```

> **GOTCHA —** the `sema_id == 0xFF` test is *not* "no semaphore"; it selects the **decrement/direct** offset register instead of the increment register. The `is_increment` argument the channel accessors pass (`i` = 1, `d` = 0) is a *separate* knob from the `0xFF` sentinel — the four channel thunks expose `i`/`d` views by passing a different `is_increment`, while the `0xFF` path is the engine-stop/raw-post case. A reimplementer who treats `0xFF` as a null id, or who collapses `is_increment` and the `0xFF` branch into one flag, targets the wrong sema register window. *(the `0xFF`→`d_ofst` branch is disasm-HIGH; the precise `is_increment` plumbing through `sp_sema_{i,d}_ofst` is MED — the i/d naming is inferred from the accessor arg, not a spec.)*

### 3.2 The R/S/I/D sema-offset taxonomy

The arch ops shim (`tdrv/encd/archs/arch.c`) exposes four semaphore-offset families, each in a raw form and a BAR-mapped-vaddr form, tail-jumping through the ops table `s @0xc96d20`. Their semantics are inferred from naming and from which descriptor uses which (the increment-on-arrival DMA target is `_i_ofst`; the op-stream wait is `_r_ofst`):

| Offset family | Raw thunk | BAR-mapped thunk | Ops slot | Inferred role | Confidence |
|---|---|---|---|---|---|
| **R** (raise / read-wait) | `encd_arch_get_sp_sema_r_ofst @0x2558c0` | `…_r_ofst_for_bar_mapped_vaddr @0x255900` | `s+0x18` / `s+0x38` | op-stream WAIT on neighbor arrival | MED |
| **S** (set) | `encd_arch_get_sp_sema_s_ofst @0x2558d0` | `…_s_ofst… @0x255910` | `s+0x20` / `s+0x40` | direct set | MED |
| **I** (increment) | `encd_arch_get_sp_sema_i_ofst @0x2558e0` | `…_i_ofst… @0x255920` | `s+0x28` / `s+0x48` | DMA increment-on-completion | MED-HIGH |
| **D** (decrement) | `encd_arch_get_sp_sema_d_ofst @0x2558f0` | `…_d_ofst… @0x255930` | `s+0x30` / `s+0x50` | decrement / direct view (`0xFF` path) | MED |

> **NOTE —** the raw `_ofst` family takes `(bar_ofst, sp_idx, sem_id)` and is used where the full TopSP coordinate is known; the `_for_bar_mapped_vaddr` family takes only `sem_id` and is used where the BAR window already pins the engine — `encd_get_leader_sps_barrier_done_semas_inc_addrs` (§4) uses the `i_ofst_for_bar_mapped_vaddr` form. The ops-slot offsets (`s+0x18..0x50`) are objdump-confirmed from the arch.c thunk tail-jumps; the R/S/I/D → raise/set/increment/decrement expansion is inferred, hence MED.

### 3.3 The pool allocator and sema-value reservation

`alloc_sp_semaphores @0x234aa0` allocates the per-SP-engine sema id bitmap (calls `set_sp_sema_bitmap`, `encd_arch_get_barrier_sem_mapping`, gated on `encd_is_host_cc @0x234a90` and `nrt_is_neuron_switch_v1_family`); `alloc_sema_value @0x231580` reserves a devmem-backed sema *value* a fold's DMA increments (asserts `"channel->comm && …"`, string `@0x8030c8`). Teardown is `free_channel_semaphores @0x232870` and `free_mesh_resources @0x2326f0`.

### 3.4 The channel sema accessors

Five accessor groups expose a channel's semaphores by role, each resolving through `encd_get_sema_addr_by_id` (or a sibling helper) with a fixed `(sema_id, is_increment)`:

| Accessor | Address | Resolves | `is_increment` | Confidence |
|---|---|---|---|---|
| `encd_get_event_sema_iaddr` | `0x238590` | mesh event sema (`semaphore_ids[]` @+0x54) → `get_sema_addr_by_id` | 1 (`i`) | HIGH |
| `encd_get_event_sema_iaddr_from_vaddr_mesh` | `0x2385c0` | mesh event sema from a vaddr | 1 (`i`) | HIGH |
| `get_channel_recv_cnt_sema_addr` | `0x2428b0` | `recv_sema_ids[0]` (`channel @+2464`) → `get_sema_addr_by_id` | arg | HIGH |
| `encd_get_channel_recv_cnt_iaddr` / `_daddr` | `0x242910` / `0x242920` | recv-cnt `i`/`d` views | 1 / 0 | MED |
| `get_channel_send_credit_sema_addr` | `0x232bd0` | send-credit sema (out-of-cell helper) | arg | HIGH |
| `encd_get_channel_send_credit_iaddr` / `_daddr` | `0x242890` / `0x2428a0` | send-credit `i`/`d` views | 1 / 0 | MED |

> **QUIRK —** the `_iaddr`/`_daddr` thunk pairs do not encode "increment vs decrement *operation*"; they select which *address view* of the same semaphore the caller hands to a descriptor — the `i` (increment) view is the address a DMA posts to, the `d` (decrement/direct) view is the address the consumer reads/drains. The four thunks (`0x242890`/`0x2428a0`/`0x242910`/`0x242920`) record zero direct callers in the static callgraph: they are reached through the `enc_primitive` credit machinery by function pointer or inlined call. A reimplementer must keep both views per credit/recv semaphore; collapsing them to one address breaks the producer/consumer split. *(the `i`/`d` arg is HIGH; the producer-vs-consumer semantics are MED, inferred from usage.)*

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_get_sema_addr_by_id` | `0x230710` | `(sp_idx, sema_id, is_inc)` → `bar0 + bar0_topsp0 + sp_sema_{i,d}_ofst` | HIGH |
| `alloc_sp_semaphores` | `0x234aa0` | per-SP-engine sema id bitmap alloc (`set_sp_sema_bitmap`) | HIGH |
| `alloc_sema_value` | `0x231580` | reserve a devmem-backed sema value a fold increments | HIGH |
| `free_channel_semaphores` | `0x232870` | release a channel's sema ids | HIGH |
| `free_mesh_resources` | `0x2326f0` | release a mesh subtype's sema/DMA resources | HIGH |
| `encd_get_event_sema_iaddr` | `0x238590` | mesh event sema increment addr → `get_sema_addr_by_id` | HIGH |
| `get_channel_recv_cnt_sema_addr` | `0x2428b0` | channel recv-count sema addr (`recv_sema_ids[0]`) | HIGH |
| `get_channel_send_credit_sema_addr` | `0x232bd0` | channel send-credit sema addr (out-of-cell helper) | HIGH |
| `encd_get_channel_recv_cnt_iaddr` / `_daddr` | `0x242910` / `0x242920` | recv-count `i`/`d` address views | MED |
| `encd_get_channel_send_credit_iaddr` / `_daddr` | `0x242890` / `0x2428a0` | send-credit `i`/`d` address views | MED |
| `encd_arch_get_sp_sema_r_ofst` … `_d_ofst` | `0x2558c0` … `0x2558f0` | R/S/I/D raw sema offsets (ops `s+0x18..0x30`) | HIGH (addr) / MED (sem) |

---

## 4. The NCFW barrier-config build

### Purpose

The TopSP firmware (NCFW) runs the device-side barrier as a table of per-step wait/post entries inside each SP-engine's `encd_ncfw_configs_t.neff.barrier_configs`. The runtime composes that table host-side and ships it to HBM with the rest of the config (§2). This section owns the *composition*: `encd_prep_ncfw_barr_config @0x2555d0` writes the per-step barrier-sema SOC addresses and sets the per-op semaphore bits; `encd_set_ncfw_start_network_proxy_trigger @0x255530` arms the TopSP→network-proxy doorbell; and `encd_get_leader_sps_barrier_done_semas_inc_addrs @0x240d40` collects the stream-leader barrier-done sema increment addresses the proxy task waits on. The firmware that *consumes* this table is [The NCFW Sequencer](../firmware/ncfw-sequencer.md).

### Algorithm — the per-step barrier-sema config

```c
// encd_prep_ncfw_barr_config @0x2555d0 — compose one barrier step into spe_config
function encd_prep_ncfw_barr_config(ctx, stream_id, step, step_offset,
                                    btype, optype, bits_set, size):
    assert(ctx);                                                  // "ctx" @encd.c:0x37CD
    tpb_idx = encd_get_primary_tpb_index(ctx->vcore->nec_dev_id);
    sem_map = encd_arch_get_barrier_sem_mapping(...);             // barrier_sem_mapping_t (14 B)
    for spe in ctx->streams[stream_id].sp_engines[0 .. sp_engine_n]:
        sp_base = encd_arch_get_sp_base_addr();
        sema_r  = sp_base + encd_arch_get_sp_sema_r_ofst(bar_ofst, spe->sp->idx, sem_id);
        // write the per-step barrier-sema SOC address into the firmware config table
        spe->spe_config.neff.barrier_configs.device_barrier
            .barrier_step_config[step].barrier_sema[i].addr.soc_addr = sema_r;
        // validate & set the per-op semaphore bits (field.start_bit / field.num_bits)
        for bit in bits_set:
            if bit out of [field.start_bit, field.start_bit + field.num_bits):
                nlog("[nec_dev %u] Invalid bit set %d for barr type %d op type %d");  // @0x80a0e8
                return NRT_FAILURE;
            if bit already set:
                nlog("[nec_dev %u] Trying to set an already set bit %d...");          // @0x80a128
                return NRT_FAILURE;
            set_bit(spe_config...barrier_sema[i].bits, bit);

// encd_set_ncfw_start_network_proxy_trigger @0x255530 — arm the TopSP→proxy doorbell
function encd_set_ncfw_start_network_proxy_trigger(ctx):
    assert(ctx);                                                  // "ctx" @encd.c:0x37B9
    for s in [0, ctx->streams_n):                                 // streams @ctx+0x98, stride 0x138
        host_mem = ctx->start_proxy_task_host_mem[s].host_mem_addr; // ctx+0x1f8 (=504), stride 0x18 +0x8
        for spe in stream.sp_engines[0 .. sp_engine_n]:           // sp_engine_n @+0x80
            // write into spe_config.neff.barrier_configs.device_barrier.start_network_proxy
            spe->spe_config[+0x5b7c] = host_mem;                  // mov %rsi,0x5b7c(%rdx)

// encd_get_leader_sps_barrier_done_semas_inc_addrs @0x240d40 — collect leader barrier-done sema addrs
function encd_get_leader_sps_barrier_done_semas_inc_addrs(ctx, out[]):
    *out_count = 0;
    sema_idx = encd_is_host_cc(ctx) ? 6 : 2;                      // host-cc vs not
    for s in [0, ctx->streams_n):                                 // cmp %r13d,0x90(%r12)
        leader = ctx->streams[s].sp_engines[0];                   // stream leader SP
        out[(*out_count)++] = encd_arch_get_sp_sema_i_ofst_for_bar_mapped_vaddr(sema_idx);  // 0x255920
```

> **GOTCHA —** the barrier-done semaphore index is `host_cc ? 6 : 2` — the host-collective-communication path and the device-only path use **different** sema indices for the same logical "barrier done" signal. A reimplementer who hard-codes one index breaks the other mode: a host-CC barrier posts to sema 6 but a device-only waiter polls sema 2 (or vice versa), and the barrier never completes. The index choice is the *only* thing `encd_is_host_cc` gates here; the rest of the collection loop is identical across modes.

> **QUIRK —** `encd_set_ncfw_start_network_proxy_trigger` writes a *host* memory address (`start_proxy_task_host_mem.host_mem_addr`) directly into a *device* firmware-config field (`spe_config + 0x5b7c`, the `device_barrier.start_network_proxy` slot at byte 23420 of the 24376-byte `encd_ncfw_configs_t`). This is the TopSP→host-proxy doorbell: when the on-device sequencer reaches the network-proxy step, it writes that host address to wake the proxy thread. A reimplementer who treats `start_network_proxy` as a device address mis-targets the doorbell into device memory and the proxy thread never wakes. The `+0x5b7c` offset and the `ctx+0x1f8` source are objdump-confirmed.

### The barrier-sema mapping struct

`encd_arch_get_barrier_sem_mapping` returns a 14-byte `barrier_sem_mapping_t` that drives both the bit validation and the sema index selection:

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `sem_index` | +0 | `uint16_t[2][2]` | `sem_index[op_type][ctx_id]` — which sema per (op, NEFF/standalone) | HIGH |
| `sp_idx` | +8 | `uint16_t` | owning SP index | HIGH |
| `field` | +10 | `bit_field_t[2]` | `.start_bit` / `.num_bits` per op_type — the valid bit range | MED-HIGH |

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_prep_ncfw_barr_config` | `0x2555d0` | compose per-step barrier-sema SOC addr + set/validate op bits | HIGH |
| `encd_set_ncfw_start_network_proxy_trigger` | `0x255530` | arm TopSP→proxy doorbell (`spe_config + 0x5b7c`) | HIGH |
| `encd_get_leader_sps_barrier_done_semas_inc_addrs` | `0x240d40` | collect leader barrier-done sema addrs (idx `host_cc ? 6 : 2`) | HIGH |
| `encd_get_basic_block_host_mem` | `0x255850` | leader SP basic-block host-mem accessor (asserts `spe->leader`) | HIGH |
| `encd_arch_get_barrier_sem_mapping` | (arch shim) | `barrier_sem_mapping_t` per (arch, barr/op type) | MED |
| `encd_prep_barr_descs` | `0x254670` | barrier DMA-descriptor builder (the data half; [DMA floor](encd-dma-devmem.md)) | HIGH |

---

## 5. The token / topsp / devmem slice

### Purpose

Before a TopSP config can name a neighbor's semaphore, every rank must agree on the topology — each rank's `(rid, tpb_index, pod_node_id, nec_dev_id, sema ids, sp_idx)`. The token slice is that reconciliation: a per-channel 160-byte `encd_neighbor` "token" is marshalled, exchanged peer-to-peer, and deserialized back into the channel's ring/kangaring/mesh neighbor arrays. The token is the *unit* the sema/TopSP floor consumes — its `recv_sema_ids[16]`/`send_sema_id`/`peer_sema_id`/`compl_sema_id` payload is exactly what `encd_get_sema_addr_by_id` later resolves to addresses.

### The token (`encd_neighbor`, 160 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `rid` | +0 | `uint32` | rank id | HIGH |
| `tpb_index` | +4 | `uint32` | primary TPB index | HIGH |
| `pod_node_id` | +8 | `uint32` | pod node id | HIGH |
| `nec_dev_id` | +12 | `uint32` | NeuronCore id (`-1` unused, `-2` = `NEC_POD_MLA_DEV` sentinel) | HIGH |
| `fold_n` | +16 | `int` | fold count (`<= ENCD_MAX_FOLD_N_RING = 4`) | HIGH |
| `port` | +20 | `tdrv_hw_port_t` | HW port (`HOST = 6`, LOCAL/REMOTEV enum) | HIGH |
| `sp_idx` | +24 | `int` | TopSP index | HIGH |
| `recv_sema_ids[16]` | +32 | `uint16[16]` | per-fold recv sema ids (view A) | HIGH |
| `send_sema_id` / `peer_sema_id` / `compl_sema_id` | +64 / +66 / +68 | `uint16` | send/peer/completion sema ids | HIGH |
| *(mesh view B)* `semaphore_ids[53]` | +32 | `uint16[53]` | mesh sema id array (overlays view A) | HIGH |

The whole 160 bytes move as ten 16-byte `movdqu` copies; the `+32` payload is a union with a per-neighbor-sema view (A), a mesh `semaphore_ids[53]` view (B), and the IDA `net_idx_addrs[]` pseudo-name (C) — the same region under three names.

### Marshal / exchange / deserialize

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_alg_ring_get_nbr_tokens` | `0x241160` | alloc 3 token arrays (self/prev/next), fill via `get_neighbor_ring`; `token_sz = 160` | HIGH |
| `encd_alg_ring_set_nbr_tokens` | `0x2415b0` | deserialize prev/next tokens → `ring_nbrs[id]` (160-B SSE copies) | HIGH |
| `encd_alg_kangaring_get_tokens` | `0x241900` | marshal kangaring local-neighbor tokens; `token_sz = 32*(channel_n + 4*channel_n)` | HIGH |
| `encd_alg_kangaring_set_tokens` | `0x241ba0` | deserialize into `kangaring_nbrs[].{next,prev,peer_*,…}` | HIGH |
| `encd_alg_mesh_get_tokens` | `0x242240` | marshal mesh tokens; overwrite self slot with rid/tpb/pod/nec + `semaphore_ids[]` | HIGH |
| `encd_alg_mesh_set_tokens` | `0x242450` | deserialize mesh tokens (handles `nec_dev_id` `-1`/`-2`/match-or-fail) | HIGH |
| `encd_dma_get_event_sem_addr_and_size` | `0x242680` | out: `sp_engine->event_sema_vaddr` + size | HIGH |
| `encd_set_channel_network_conn` | `0x242700` | mark a ring/mesh neighbor as a HOST network conn (`port == HOST(6)`) | HIGH |

The *devmem* slice these tokens allocate against (the HBM/host-DRAM allocators and the lazy reservation object) is owned by [encd: DMA/devmem Floor §2](encd-dma-devmem.md) — this page does not re-derive the devmem floor; it consumes the sema-id payload the tokens carry.

> **NOTE —** the kangaring `token_sz = 32*(channel_n + 4*channel_n)` formula is exactly as decompiled; its rationale (five neighbor slots × 32 B) is inferred *(formula HIGH; the 5-slot rationale MED)*. The mesh deserialize's `nec_dev_id` sentinels (`-1` skip, `-2` `NEC_POD_MLA_DEV` copy, else match) and the `"Unexpected id at %d…"` failure are string-confirmed.

---

## 6. Verification notes

> The TopSP config emit, the semaphore floor, the NCFW barrier-config build, and the token slice were cross-checked against the IDA artifacts and direct disassembly of `libnrt.so` 2.31.24.0:
> - `encd_populate_metaring_topsp_config @0x240bf0` is `nm`-confirmed; its dispatch into `prep_metaring_topsp_config @0x2331d0` is the objdump `call 2331d0`, and its asserts `"metaring != NULL"` / `"ctx != NULL"` / `"comm != NULL"` are dumped verbatim from `.rodata` (`@0x844e7b` / `@0x8466d7` / `@0x8413fc`), as is the failure log `"…Failed to prepare mesh top sp config…"` (`@0x805700`).
> - The TopSP MMIO base constants `0xFFFD0200000` (`idx << 22`), `0x8280200000`/`0x808280200000` (`(idx % 8) << 30`) are objdump-confirmed identical across `enc_get_start/stop_topsp_reg_addrs` and `encd_get_basic_block_switch_topsp_reg_addrs`. The arch-type switch (`cmp $0x2`/`$0x3`/`$0x4`) is read directly from `encd_get_coretype @0x230be0`.
> - `encd_get_sema_addr_by_id @0x230710` is disasm-confirmed: it reads `comm + 0x3011F4C0`, calls `encd_arch_get_mla_device @0x255ad0`, `encd_arch_get_bar0_top_sp_0_offset @0x2559a0`, and branches on `cmp $0xff,%bx` between `encd_arch_get_sp_sema_d_ofst @0x2558f0` and `encd_arch_get_sp_sema_i_ofst @0x2558e0`. `encd_get_event_sema_iaddr @0x238590` tail-jmps (`jmp 230710`) into it after reading `semaphore_ids[]` at `+0x54`.
> - `encd_set_ncfw_start_network_proxy_trigger @0x255530` walks `ctx->streams` (stride `0x138`), reads the proxy host-mem from `ctx+0x1f8` (stride `0x18`, `+0x8`), and writes `spe_config + 0x5b7c` (`mov %rsi,0x5b7c(%rdx)`). `encd_get_leader_sps_barrier_done_semas_inc_addrs @0x240d40` calls `encd_is_host_cc @0x234a90` and selects the `_i_ofst_for_bar_mapped_vaddr @0x255920` sema index `6` vs `2`.
> - The R/S/I/D ops-slot offsets (`s+0x18..0x50`) are objdump-confirmed from the arch.c thunk tail-jumps through `s @0xc96d20`; the raise/set/increment/decrement *expansion* is inferred from usage, marked MED.
> - The **zero libnccom/`nccl*`/`neuron*`/`nec_*` edges** claim is a callgraph out-edge sweep over every function on this page; every callee is `encd_arch_*`, an `al_hal_*`/`aws_hal_*`/`ndl_*`/`dmem_*` HAL, the packet emitters, or libc.
>
> **[MED]** The `comm->ctx` / `comm->strm` byte offset (`0x3011F4C0` / `0x3011F4B8`) inside `encd_comm` is disasm-confirmed at the call sites, but the full `encd_comm` field layout is model-derived; field *identity* is HIGH, the intermediate path is a Phase-2 item.
> **[MED]** The R/S/I/D sema semantics, the `is_increment` (`i`/`d`) plumbing, and the `barrier_sem_mapping_t.field` bit-range interpretation are inferred from naming + the bit-validation in `encd_prep_ncfw_barr_config`; the per-silicon offset bodies are out-of-cell (set by `arch_init @0x256479`).
> **[LOW]** The arch enum → Neuron generation mapping (`arch == 2` → `<< 22` scheme) is pinned for the address arithmetic but not for the marketing generation name; `encd_get_coretype`'s `0xC`/`0x5` coretype constants are read but not named.

> **CORRECTION (ENC-SEMA-1) —** the IDA decompile renders `channel->comm->ctx` / `channel->comm->strm` as the nonsense array walks `comm[1].ring.channels[21].tp_inc_steps[81096].mark` / `…[81095].mark`. Disassembly shows these are *fixed byte offsets* `comm + 0x3011F4C0` (`ctx`) and `comm + 0x3011F4B8` (`strm`) — the same artifact corrected on [encd: DMA/devmem Floor §1](encd-dma-devmem.md). Read every `comm[1]…[81096].mark` as `comm->ctx` and `…[81095].mark` as `comm->strm`. *(field identity HIGH; exact intermediate `encd_comm` layout MED.)*

---

## Related Components

| Name | Relationship |
|---|---|
| `prep_metaring_topsp_config` (`@0x2331d0`) | the per-channel SPAD/CCE config emitter `encd_populate_metaring_topsp_config` dispatches into |
| `encd_prep_topsp_config_{start,end}` (`@0x240df0` / `@0x2429b0`) | the NEFF→TopSP config materialization into HBM the emit feeds |
| `barrier_composer::compose_ncfw_configs_for_barrier` | the composer that calls `encd_prep_ncfw_barr_config` |
| `enc_barrier_proxy_task::ctor` (`@0x1cf170`) | the proxy task that consumes `encd_get_leader_sps_barrier_done_semas_inc_addrs` |
| `encd_get_sema_addr_by_id` (`@0x230710`) | the address resolver every channel sema accessor funnels through |
| `add_dma_packet_cce` (`@0x2307d0`) | the CCE-reduce packer the config emit hands descriptors to ([DMA floor](encd-dma-devmem.md)) |

## Cross-References

- [TopSP Notification Path](../kernel/topsp.md) — the kernel-side TopSP doorbell/notification path that mirrors this device-side config emit
- [encd: DMA Descriptor and devmem Floor](encd-dma-devmem.md) — the data-movement half: the descriptor emit, the devmem allocators, and the lazy reservation this page's semaphores synchronize
- [encd: the Device-Resident Descriptor Emitter](encd-overview.md) — the floor map, the `encd_context`, the SPAD op-stream funnel, and the channel-activation accessors
- [The NCFW Sequencer (Xtensa LX Disassembly)](../firmware/ncfw-sequencer.md) — the TopSP firmware that consumes the barrier-config table this page composes
- [Serializer Families (per-arch CC-context dumpers)](../firmware/serializer-families.md) — the per-arch dumpers that reflect the NCFW config image this emit materializes
- [encd: Per-Arch Ops Dispatch](encd-arch-ops.md) — the `encd_arch_*` shim that supplies every SP base, sema offset, and TopSP-0 BAR offset
- [back to index](../index.md)
