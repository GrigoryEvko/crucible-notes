# encd: Per-Arch Ops Dispatch and Virtual-Inference-Layout Translation

> *All addresses, offsets, and slot displacements on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; `.text`/`.rodata` are VMA == file offset, so every `0x25…`/`0x26…` is an analysis VMA, while `.bss` is `NOBITS` (the dispatch global `s @0xc96d20` has an address but no file bytes). Provenance strings `/opt/workspace/KaenaRuntime/tdrv/encd/{archs/arch.c, arch_device_mapping.c, nec_vil.c}` root every function. Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF- and disasm-anchored)** — every `encd_arch_*` shim resolves via `addr2line` to one of those three TUs, is a `local` (`t`) symbol, and its `s`-slot displacement was read from the live `mov s+0xNNN(%rip)` / `jmp *s.<member>` load and cross-checked against the `soc_struct_t` offset in `structures.json`. · Part IX — On-Device Collectives, DEEP · [back to index](../index.md)*

## Abstract

The on-device collectives emitter ([encd Overview](encd-overview.md)) compiles a collective into device descriptor streams, and to do so it must constantly ask the silicon questions: *which routing port reaches that peer, which MLA owns this NeuronCore, how many sequencer engines does an MLA have, which DMA engine bridges these two reduction half-domains.* The answers differ per chip generation, and **none** of the ~95 `encd_arch_*` entry points contains the answer. Each is a thin forwarder — typically a single guarded indirect `jmp *s.<member>` — into one global per-arch ops table, `s` (`@.bss 0xc96d20`, type `soc_struct_t`, **744 B / ~93 function-pointer slots**), installed exactly once at bring-up by `arch_init @0x2563f0`. That installer reads `al_hal_tpb_get_arch_type()` and fills `s` from one of three per-silicon binders — **sunda** (arch 2), **cayman** (arch 3), **mariana** (arch 4). This page documents the *dispatch layer*: the forwarder shapes, the one global table they index, and the seam between the generic `encd` floor and its three per-silicon leaves. The leaf *constants* themselves — what each `<arch>_get_<property>` returns — are the byte-verified matrix on [arch-geometry](../runtime/arch-geometry.md); this page owns the indirection that reaches them, not the values.

Two things complicate the simple "forwarder → slot" picture, and both are this page's substance. First, not every `encd_arch_*` accessor goes through `s`: a cluster bypasses the table and calls the **`nec_vil_*` "Virtual Inference Layout"** directly. `nec_vil` is the layer that translates a *virtual* `nec_dev_id` — the runtime's per-NeuronCore identifier — into the *physical* objects it names: the MLA hardware device (`mla_t*`), the worker/vcore index inside that MLA, and the TopSP sync-processor object for a worker. This translation is arch-*independent* math over the virtual-core layout (`vtpb`), which is why it bypasses the per-arch table. Second, a small `arch_device_mapping.c` sub-layer builds and queries the per-local-device **topology maps** — `mla_to_rid`, `rid_to_mla`, and the `[32][4]` `port_map` — and four of its query functions are the device-floor backing of *exported* `nec_*` callbacks that the separately-shipped libnccom NCCL fork imports. So although the `encd_arch_*` floor makes no NCCL call itself, the libnccom boundary crosses *into* it here.

The reference frame is a userspace DHAL whose answers are *fabric topology* rather than *device-driver geometry*. The runtime already carries a 488-byte device-driver vtable, `tdrv_arch_ops`, for `num_tpb`/BAR-offset/sync-event questions ([tdrv-arch-ops](../runtime/tdrv-arch-ops.md)); `soc_struct_t` is its **larger sibling for the collectives fabric** — routing ports, peer RIDs, SBUF partition geometry, inter-RDH DMA maps, switch extents. The two are distinct `.bss` objects, filled by distinct registrars (`tdrv_arch_register_*` vs `*_arch_init`), reached by distinct accessor families (`tdrv_arch_*` vs `encd_arch_*`). A reimplementer must keep them separate: the device-side `encd_arch_*` table this page documents is **not** the userspace `tdrv_arch_ops` table.

For reimplementation, the contract is:

- **The forwarder → ops-table model.** Every `encd_arch_*` accessor is one of four shapes over the global `s @0xc96d20`: a pure `jmp *s.<member>`; a `nec_dev_id != -1` guard then `jmp *s.<member>`; a NULL-slot guard then dispatch (for members an arch may legitimately leave unset); or a direct `jmp <nec_vil_*/vtpb_*>` that bypasses `s` entirely. Reproduce the exact `s` byte-offset for each member, because the dispatch reaches it by hard-coded displacement.
- **The `s` install and the arch enum.** `s` is filled once by `arch_init @0x2563f0`, which dispatches `al_hal_tpb_get_arch_type()` → `{2→sunda_arch_init, 3→cayman_arch_init, 4→mariana_arch_init}(&s)`; each binder `memset`s the table then SIMD-pair-stores ~93 leaf pointers. Slots an arch does not implement stay NULL — which is exactly why the NULL-slot-guard forwarder shape exists.
- **The `nec_vil` translation.** A virtual `nec_dev_id` resolves to a physical `(mla, vcore/worker, topsp)` triple by pure layout math over `vtpb` (TPBs per virtual core, vcores per MLA, TopSPs per vcore) — no per-arch table. The `encd_arch_*` accessors that query MLA/worker/TopSP identity bypass `s` and call `nec_vil_*` directly.
- **The `NEC_NET_MLA_DEV` sentinel.** `nec_dev_id == (uint32_t)-1 == 0xFFFFFFFF` is the network-MLA pseudo-device; about half the dev-keyed forwarders open by asserting `nec_dev_id != NEC_NET_MLA_DEV` to reject it before any topology lookup. A reimplementer must reproduce the sentinel and the guard, or a network-MLA id will index real per-device tables out of range.

| | |
|---|---|
| **Dispatch global** | `s` (`@.bss 0xc96d20`, `soc_struct_t`, **744 B / ~93 fn-ptr slots**) — LOCAL OBJECT, zero-init |
| **Install** | `arch_init @0x2563f0` → `al_hal_tpb_get_arch_type()` → `2→sunda / 3→cayman / 4→mariana` binder(`&s`) |
| **Binders** | `sunda_arch_init @0x25fd40` · `cayman_arch_init @0x25df70` · `mariana_arch_init @0x25a930` |
| **Arch enum** | `SUNDA=2` (V2) · `CAYMAN=3` (V3 / Trn2) · `MARIANA=4` (V4 / Trn3) |
| **Forwarder TU** | `tdrv/encd/archs/arch.c` — all `encd_arch_*` `local` symbols, lines ~86–451 |
| **Sentinel** | `NEC_NET_MLA_DEV = (uint32_t)-1 = 0xFFFFFFFF` (assert expr `"nec_dev_id != NEC_NET_MLA_DEV"` `@0x845659`) |
| **VIL layer** | `tdrv/encd/nec_vil.c` — `nec_vil_get_{mla,mla_idx,num_vcores_per_mla,num_topsp_per_vcore,topsp,topsp_idx}` `@0x2602xx–0x2605xx` |
| **Topology maps** | `arch_device_mapping.c` — `device_id_maps` (772 B) `mla_to_rid`/`rid_to_mla`/`port_map[32][4]`, built by `encd_arch_build_port_and_rid_map @0x256540` |
| **libnccom seam** | exported `nec_*` one-instr thunks (`nec_is_mla_available @0x1bfe00`, `nec_mla_idx_to_rid @0x1bfe10`, …) tail-jmp **into** the mapping queries |
| **Geometry values** | owned by [arch-geometry](../runtime/arch-geometry.md) — this page owns the *dispatch*, not the per-arch constants |

> **CORRECTION (ENC-ARCH-OPS-01) —** the early cell survey of this band ([P1-L-ENC-20/21]) printed the arch enum two different ways — `V2=sunda/V3=cayman/V4=mariana` in one cell and `V2=mariana/V3=sunda/V4=cayman` in an adjacent one. Both are transcription slips of the *same* `arch_init` switch. The shipped dispatch is unambiguous: `cmp $0x2 → sunda_arch_init`, `cmp $0x3 → cayman_arch_init`, `cmp $0x4 → mariana_arch_init` (`@0x2563f0`), confirmed against the sibling pages ([arch-geometry CORRECTION ARCH-GEOM-01](../runtime/arch-geometry.md), [tdrv-arch-ops](../runtime/tdrv-arch-ops.md)). This page uses **sunda=2 / cayman=3 / mariana=4** throughout.

---

## 1. The Forwarder → Ops-Table Model

### Purpose

The `encd_arch_*` family is the *entire* device-side collectives arch interface expressed as one indirection seam. The generic `encd` emitter ([encd Overview](encd-overview.md), [DMA & devmem Floor](encd-dma-devmem.md)) is written once, arch-neutral; every place it needs a per-silicon answer it calls an `encd_arch_*` accessor, and that accessor forwards into the global ops table `s`. The seam means the emitter never branches on chip generation — the *value* behind a slot was chosen once, at install, by the latched arch — and the per-arch logic lives entirely in the leaves the binders install.

### Entry Point

```text
arch_init (0x2563f0)                                   ── tdrv/encd/archs/arch.c, ONE-TIME install
  └─ al_hal_tpb_get_arch_type ()                       ── 2=SUNDA / 3=CAYMAN / 4=MARIANA
  └─ switch:
       case 2 → sunda_arch_init   (&s)  (0x25fd40)     ── memset(s,0,744); pair-store ~93 sunda_* ptrs
       case 3 → cayman_arch_init  (&s)  (0x25df70)
       case 4 → mariana_arch_init (&s)  (0x25a930)
       default→ nlog_write("Architecture not found %d"); return NRT_FAILURE
  => s @0xc96d20 now holds one arch's fn-ptr set; every encd_arch_* shim reads it.

<encd caller> → encd_arch_get_X (nec_dev, …)           ── the steady-state dispatch
       (optional) → __assert_fail (nec_dev / NULL-slot guard)
       → jmp *s.X   OR   jmp <nec_vil_* / vtpb_*>       ── the actual per-arch / VIL impl
```

> **NOTE —** `arch_init` and the three `*_arch_init` binders are *not* documented here — they are the writers of the table this page reads, owned by [arch-geometry §1](../runtime/arch-geometry.md). This page documents the readers: the ~95 `encd_arch_*` forwarders that index `s` after it is filled. The install fires deep inside single-threaded `enc_init` (`enc.cc`), so the table is fully populated before any steady-state forwarder runs.

### Algorithm — the four forwarder shapes

Every `encd_arch_*` accessor reduces to one of four shapes. The first three index `s`; the fourth bypasses it. The `s`-slot displacement in each `jmp *` is the member's byte offset into `soc_struct_t`, read from the live load.

```c
// Global dispatch table, installed once by arch_init (§Entry Point):
//   soc_struct_t s @0xc96d20  — 744 B, ~93 fn-ptr members, .bss LOCAL, zero-init
//   member byte-offset == the displacement in `jmp *s+0xNNN(%rip)` == structures.json offset

// SHAPE A — pure tail-jump thunk (no guard). e.g. encd_arch_get_p2p_port @0x2559f0
function encd_arch_get_p2p_port(local, src, dst, out_port):       // arch.c
    return s.get_p2p_port(local, src, dst, out_port);             // jmp *s+0x78  (+120)
    // trusts the slot is non-NULL — set for every supported arch

// SHAPE B — nec_dev sentinel guard, then dispatch. e.g. encd_arch_get_resource_pool @0x255aa0
function encd_arch_get_resource_pool(nec_dev):                    // arch.c
    if nec_dev == NEC_NET_MLA_DEV /* 0xFFFFFFFF */:
        __assert_fail("nec_dev_id != NEC_NET_MLA_DEV",            // expr @0x845659
                      "/opt/workspace/KaenaRuntime/tdrv/encd/archs/arch.c", 131,
                      "encd_arch_get_resource_pool");             // PRETTY_FUNCTION @0x9be730
    return s.get_resource_pool(nec_dev);                          // jmp *s+0xC8  (+200)

// SHAPE C — NULL-slot guard, for a member an arch may legitimately leave unset.
//           e.g. encd_arch_get_max_links_per_pod_port @0x255f10
function encd_arch_get_max_links_per_pod_port():                  // arch.c:267
    if s.get_max_links_per_pod_port == NULL:                      // slot may be NULL per-arch
        __assert_fail("s.get_max_links_per_pod_port",             // expr @0x845677
                      ".../arch.c", 0x10B, ...);
    return s.get_max_links_per_pod_port();                        // jmp *s+0x170 (+368)

// SHAPE D — bypass the table, tail-jump a NAMED nec_vil_*/vtpb_* helper (§2).
//           e.g. encd_arch_get_num_workers_per_mla @0x255b10
function encd_arch_get_num_workers_per_mla():                     // arch.c
    return nec_vil_get_num_vcores_per_mla();                      // direct jmp, NOT via s
```

Two refinements on the shapes. Shape A has variadic-safe and arg-fixup sub-variants the disassembly shows but the model can ignore: an `xor eax,eax; jmp` clears the SysV vararg-fp-count register before a `jmp *s` (e.g. `encd_arch_get_inter_rdh_fold_n @0x255fe0`), and a `movzx edx,dl; jmp *s` zero-extends a `bool` argument before forwarding (e.g. `encd_arch_get_barrier_sem_mapping @0x256110`). Both are calling-convention housekeeping, not logic. Shape C is rare — only members that some generation genuinely lacks carry it (e.g. `get_max_links_per_pod_port`, `get_seng_rel_devid`, `get_die_idx_from_tpb_idx`); the verbatim assert expression is the *member name itself* (`"s.get_seng_rel_devid"` `@0x845694`) rather than the `nec_dev` sentinel string, which is how the two guard kinds are told apart in the binary.

> **QUIRK —** one forwarder is a deliberate *alias*: `encd_arch_get_switch_v1_address @0x255a10` forwards to the table member `s.get_pds_address` (`+136`), not to a member named `get_switch_v1_address`. The public accessor name and the table slot name differ on purpose — "switch-v1 address" and "PDS (peer-destination) address" are the same hardware concept (a peer-destination switch address built from a port / routing-id / pod-node / relaxed-ordering stamp), exposed under the collectives-facing name while the table keeps the hardware name. A reimplementer matching accessor names to slot names one-to-one will fail to wire this one; the slot offset (`+136`), not the name, is authoritative.

> **GOTCHA —** the `encd_arch_*` accessors are split across **four `.text` bands and four declaration orders**, and the `s`-slot offsets are **non-monotonic** with address. Within one source-declaration run the slots climb (`+304, +312, +320, …`), but the bands interleave: `arch.c` declares the SBUF/NCFW group (`+480..+528`, `+664..+736`) far from the routing group (`+112..+296`), so address order tells you nothing about slot order. The band is laid out in source-declaration order; `s` itself groups members by *feature*, not by export order. A reimplementer who infers the struct layout from the symbol address order will mis-place every slot — read each member's offset from its own `jmp *s+0xNNN` load, never from its neighbors'.

### The ops-table model — slot | accessor | per-arch backend

The table below is the *dispatch* view of `s`: which `encd_arch_*` accessor reads which slot, and the per-arch leaf the binder installs there. It is deliberately a **dimension table, not the full 93-row dump** — it names the functional groups the forwarders touch and one representative slot per group; the exhaustive 744-byte member map and the per-arch constant each leaf returns are owned by [arch-geometry §2](../runtime/arch-geometry.md). "Per-arch backend" names the sunda/cayman/mariana family installed into the slot (one is live at runtime, chosen by `arch_init`).

| `s` slot | Accessor (`encd_arch_*`) | Group / role | Per-arch backend | Conf |
|---|---|---|---|---|
| +112 | `get_p2p_port_alloc_tbl_entry` | P2P routing: port-alloc table lookup | `{sunda,cayman,mariana}_get_p2p_port_alloc_tbl_entry` | HIGH |
| +120 | `get_p2p_port` | P2P routing: `(local,src,dst)` → `tdrv_hw_port_t` | `<arch>_get_p2p_port` (cayman `@0x25d1c0`) | HIGH |
| +136 | `get_switch_v1_address` → `get_pds_address` | peer-destination switch address build (alias) | `<arch>_get_pds_address` (cayman `@0x25b310`) | HIGH |
| +144 | `set_peer_rid` | stamp peer routing-id into a phys addr | `<arch>_set_peer_rid` (cayman `@0x25b260`) | HIGH |
| +152 | `get_mesh_dma_engine_id_from_tbl` | mesh DMA-engine routing table lookup | `<arch>_get_mesh_dma_engine_id_from_tbl` | HIGH |
| +200 | `get_resource_pool` | per-NC `cc_resource_pool_context*` (the comm-pool anchor) | `<arch>_get_resource_pool` (cayman `@0x25c760`) | HIGH |
| +258 | `get_barrier_sem_mapping` | barrier-type → semaphore mapping (returns struct) | `<arch>_get_barrier_sem_mapping` | HIGH |
| +280 | `get_dma_alloc_bitmap` | `(nec_dev,qb)` → `{qid,bitmap}` (8 B) | `<arch>_get_dma_alloc_bitmap` | HIGH |
| +424 | `get_num_sb_ports` | SBUF geometry: state-buffer port count | `<arch>_get_num_sb_ports` | HIGH |
| +432 | `get_pod_port` | inter-node POD-port resolve → `NRT_STATUS` | `<arch>_get_pod_port` | HIGH |
| +496 | `get_ncfw_configs_sz` (const) | NCFW config-struct size | `arch_common_get_ncfw_configs_sz @0x256fe0` → 17024 | HIGH |
| +664 | `get_switch_exit_port` | switch fabric: exit port (`hop=1`) | `<arch>_get_switch_exit_port` | HIGH |
| +680 | `get_max_devs` | switch fabric: devices spanned (mariana family-conditional) | `<arch>_get_max_devs` | HIGH |
| +736 | `get_mesh_max_folds` | mesh: max fold count (mariana family-conditional) | `<arch>_get_mesh_max_folds` | HIGH |

> **NOTE —** the constant accessors at `+496/+504/+520/+528` are an exception to "every arch installs its own leaf": those four NCFW-size getters share a single arch-common body (`arch_common_get_ncfw_configs_sz @0x256fe0` → `0x4280`, `…_max_mesh_events @0x256ff0` → 108, `…_ctrl_spad_offset @0x257000` → 17104, `…_slot_spad_offset @0x257010` → 17056), and `arch_init` binds the same address into all three arches' tables — unless a per-arch override exists (`@0x25ae8x` / `@0x25e4bx`, owned by [arch-geometry](../runtime/arch-geometry.md)). The values are HIGH; *which* binder installs the base vs the override is a MED binding question left to the geometry page.

### Function Map — representative forwarders

A representative slice of the ~95 forwarders across the four `arch.c` bands (`0x2559b0..0x2563c0`), one per shape and group. The full inventory is the `encd_arch_*` symbol family in `arch.c`; these pin the shapes and the high-traffic slots.

| Function | Address | Shape | `s` slot / target | Confidence |
|---|---|---|---|---|
| `encd_arch_get_evt_accel_addr` | `0x2559b0` | A | `s.get_evt_accel_addr` (+384) | HIGH |
| `encd_arch_get_p2p_port` | `0x2559f0` | A | `s.get_p2p_port` (+120) | HIGH |
| `encd_arch_get_switch_v1_address` | `0x255a10` | A | `s.get_pds_address` (+136, alias) | HIGH |
| `encd_arch_get_resource_pool` | `0x255aa0` | B | `s.get_resource_pool` (+200) | HIGH |
| `encd_arch_get_dma_alloc_bitmap` | `0x255cc0` | B | `s.get_dma_alloc_bitmap` (+280) | HIGH |
| `encd_arch_get_max_dma_queue_bundles` | `0x255cf0` | A | `s.get_max_dma_queue_bundles` (+288) | HIGH |
| `encd_arch_get_max_links_per_pod_port` | `0x255f10` | C | `s.get_max_links_per_pod_port` (+368, NULL-guard) | HIGH |
| `encd_arch_get_barrier_sem_mapping` | `0x256110` | A | `s.get_barrier_sem_mapping` (+600, `movzx` fixup) | HIGH |
| `encd_arch_get_seng_rel_devid` | `0x256130` | C | `s.get_seng_rel_devid` (+616, NULL-guard) | HIGH |
| `encd_arch_get_switch_exit_port` | `0x256230` | B | `s.get_switch_exit_port` (+664) | HIGH |
| `encd_arch_get_mesh_max_folds` | `0x2563c0` | B | `s.get_mesh_max_folds` (+736) | HIGH |
| `encd_arch_get_num_workers_per_mla` | `0x255b10` | D | `nec_vil_get_num_vcores_per_mla` (§2) | HIGH |
| `encd_arch_get_mla_device_idx` | `0x255b20` | D | `nec_vil_get_mla_idx` (§2) | HIGH |

---

## 2. The `nec_vil` Virtual-Inference-Layout Translation

### Purpose

`nec_vil` is the layer that turns the runtime's *virtual* NeuronCore identifier — `nec_dev_id`, a small integer the composer and emitter thread through everything — into the *physical* hardware objects it stands for. A set of `encd_arch_*` accessors (Shape D) bypass the per-arch table `s` and call `nec_vil_*` directly, because the translation is arch-*independent*: it is pure layout arithmetic over the virtual-core (`vtpb`) model, identical across sunda/cayman/mariana. The questions it answers are identity, not behavior — *which MLA owns this NeuronCore, which worker/vcore is it inside that MLA, which TopSP sync-processor serves a worker* — so there is nothing for a per-arch leaf to specialize.

The model has three nested counts, all read from `vtpb`: TPBs per virtual core (`vtpb_get_vtpb_core_size`), virtual cores per MLA, and TopSPs per virtual core. A `nec_dev_id` indexes a `virtual_core_t` (via `vtpb_get_virtual_core_from_nec_dev_id`); the vcore carries the physical `tpbs[]` and the `device_tpb_idx`, and the per-MLA / per-TopSP indices fall out as quotient/remainder against those three counts.

### Entry Point

```text
encd_arch_get_mla_device          (0x255ad0, Shape D, nec_dev guard)
  └─ nec_vil_get_mla              (0x260360)   ── nec_dev_id → mla_t*  (the MLA hw device)
encd_arch_get_mla_device_idx      (0x255b20, Shape D, guard)
  └─ nec_vil_get_mla_idx          (0x260310)   ── nec_dev_id → MLA index
encd_arch_get_num_workers_per_mla (0x255b10, Shape D)
  └─ nec_vil_get_num_vcores_per_mla (0x260240) ── vcores(workers)/MLA   [nec_vil.c:8]
encd_arch_get_num_topsp_per_worker(0x255bb0, Shape D)
  └─ nec_vil_get_num_topsp_per_vcore (0x2604b0)── TopSPs/worker
encd_arch_get_topsp_idx_for_worker(0x255bc0, Shape D, guard)
  └─ nec_vil_get_topsp_idx        (0x260500)
       └─ nec_vil_get_vcoreid_rel_mla (0x260290)
            └─ nec_vil_get_num_vcores_per_mla (0x260240)
encd_arch_get_topsp_for_worker    (0x255c00, Shape D + NULL-check + nlog)
  └─ nec_vil_get_topsp            (0x260580)
encd_arch_get_worker_idx_rel_mla  (0x255ea0, Shape D, guard)
  └─ nec_vil_get_vcoreid_rel_mla  (0x260290)
```

### Algorithm — virtual `nec_dev_id` → physical `(mla, worker, topsp)`

```c
// nec_vil_get_num_vcores_per_mla @0x260240 — nec_vil.c:8 — the base count
function nec_vil_get_num_vcores_per_mla():
    num_tpb   = tdrv_arch_get_num_tpb();          // 0x309050 — TPBs on the core (tdrv_arch_ops)
    core_size = vtpb_get_vtpb_core_size()[0];     // TPBs per virtual core; assert query == NRT_SUCCESS
    return num_tpb / core_size;                   // vcores (= workers) per MLA

// nec_vil_get_vcoreid_rel_mla @0x260290 — worker index *within* its MLA
function nec_vil_get_vcoreid_rel_mla(nec_dev_id):
    vcore = vtpb_get_virtual_core_from_nec_dev_id(nec_dev_id);   // resolve the vcore object
    assert(vcore != NULL);                                       // "vcore != NULL" @ nec_vil.c
    core_size      = vtpb_get_vtpb_core_size()[0];
    vcores_per_mla = nec_vil_get_num_vcores_per_mla();           // 0x260240
    // global vcore ordinal = first TPB's device index / TPBs-per-vcore; mod by per-MLA count
    return (vcore->tpbs[0].device_tpb_idx / core_size) % vcores_per_mla;

// encd_arch_get_topsp_idx_for_worker @0x255bc0 → nec_vil_get_topsp_idx @0x260500
//   relative TopSP idx (within a worker) → absolute TopSP index, via the worker-rel-MLA ordinal
function nec_vil_get_topsp_idx(nec_dev_id, rel_topsp_idx):
    worker_rel = nec_vil_get_vcoreid_rel_mla(nec_dev_id);        // 0x260290
    per_vcore  = nec_vil_get_num_topsp_per_vcore();              // 0x2604b0
    return worker_rel * per_vcore + rel_topsp_idx;               // absolute TopSP index

// encd_arch_get_topsp_for_worker @0x255c00 — the ONLY non-trivial forwarder in the band
function encd_arch_get_topsp_for_worker(nec_dev_id, rel_topsp_idx, out_topsp):
    if nec_dev_id == NEC_NET_MLA_DEV:                            // Shape-B guard (assert @arch.c:177)
        __assert_fail("nec_dev_id != NEC_NET_MLA_DEV", ...);
    res = nec_vil_get_topsp(nec_dev_id, rel_topsp_idx);         // 0x260580
    if res == NULL:                                             // unlike the pure thunks, it null-checks
        nlog_ERROR("[nec_dev %u] failed to get topsp", nec_dev_id);  // string @0x80a1b0
        return NRT_FAILURE;
    *out_topsp = res;
    return NRT_SUCCESS;
```

> **QUIRK —** `encd_arch_get_topsp_for_worker @0x255c00` is the **single forwarder in the whole `encd_arch_*` band that contains real logic** — a NULL-check on the resolved TopSP and an `nlog` error path returning `NRT_FAILURE` (string `"[nec_dev %u] failed to get topsp"` `@0x80a1b0`). Every other Shape-D accessor is a bare `jmp` into `nec_vil_*`, trusting the result. The asymmetry is deliberate: a missing TopSP at `encd_init_context` time is a recoverable bring-up failure the caller must see, whereas a missing MLA index is an invariant violation the `nec_dev` sentinel guard already excludes. A reimplementer who collapses all Shape-D accessors into bare thunks loses the one error path the context-init caller depends on.

> **GOTCHA —** the `nec_vil` math reads counts from **two different arch layers**, and conflating them mis-sizes the layout. `nec_vil_get_num_vcores_per_mla` reads `tdrv_arch_get_num_tpb()` from the **488-byte `tdrv_arch_ops`** table ([tdrv-arch-ops slot 4](../runtime/tdrv-arch-ops.md)) — the device-driver geometry vtable — and `vtpb_get_vtpb_core_size()` from the **`vtpb`** virtual-core model, *not* from the 744-byte `soc_struct_t` this page's forwarders dispatch through. So a single `nec_vil` call touches three distinct arch sources: `tdrv_arch_ops` (num_tpb), `vtpb` (core size), and — for the callers — the `soc_struct_t` routing slots. They are not interchangeable; `num_tpb` lives in the *driver* vtable, never the *fabric* vtable.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nec_vil_get_mla` | `0x260360` | `nec_dev_id` → `mla_t*` (MLA hw device object) | HIGH |
| `nec_vil_get_mla_idx` | `0x260310` | `nec_dev_id` → MLA index | HIGH |
| `nec_vil_get_num_vcores_per_mla` | `0x260240` | `num_tpb / vtpb_core_size` → workers/MLA (`nec_vil.c:8`) | HIGH |
| `nec_vil_get_vcoreid_rel_mla` | `0x260290` | worker index relative to its MLA | HIGH |
| `nec_vil_get_num_topsp_per_vcore` | `0x2604b0` | TopSPs per worker | HIGH |
| `nec_vil_get_topsp_idx` | `0x260500` | relative → absolute TopSP index | HIGH |
| `nec_vil_get_topsp` | `0x260580` | `(nec_dev,rel_topsp_idx)` → `top_sp_t*` | HIGH |
| `nec_vil_get_nec_dev_for_host_nec_dev` | `0x2603d0` | host `nec_dev` id → device `nec_dev` id | HIGH |
| `nec_vil_get_num_dma_per_vcore` | `0x260600` | DMA engines per worker (Shape-D target) | HIGH |

---

## 3. The Device-Identity Maps (`arch_device_mapping.c`)

### Purpose

The forwarders of §1 answer "which port reaches that peer," but the *table* those answers index — the per-local-device map from MLA index to routing-id (RID), from RID back to MLA, and from `(mla, port)` to the peer MLA on the other end — is built and queried by a small `arch_device_mapping.c` sub-layer. It operates on a `device_id_maps` struct (772 B) embedded in the `cc_resource_pool_context` that `encd_arch_get_resource_pool` hands out (`device_id_maps @+96`). The build runs once per local device; thereafter the queries are pure table reads with precondition asserts. This sub-layer is where the libnccom boundary crosses *into* the `encd_arch_*` floor: four of its queries are the device-side backing of exported `nec_*` callbacks the NCCL fork imports.

### Layout — `device_id_maps` (772 B, at `cc_resource_pool_context+96`)

| Field | Offset | Type | Meaning | Conf |
|---|---|---|---|---|
| `device_id_maps_build` | +0 | `bool` | build-once latch; queries assert it set | HIGH |
| `port_map` | +4 | `int[32][4]` | per-MLA × per-port → peer MLA idx; init `-2` (`-2` = unconnected) | HIGH |
| `mla_to_rid` | +516 | `int[32]` | MLA idx → routing id; init `-4` | HIGH |
| `rid_to_mla` | +644 | `int[32]` | routing id → MLA idx; init `-3` | HIGH |

Constants in the code: `MAX_MLA_DEVICES = 32` (asserts `mla_idx < 32`), 4 ports per MLA, the distinct init sentinels `-2/-3/-4`, and the "no peer" return `-22` (`-EINVAL`) from the port query.

### Algorithm — build then query

```c
// encd_arch_build_port_and_rid_map @0x256540 — arch_device_mapping.c:53 — POPULATE once (1011 B, 55 bb)
function encd_arch_build_port_and_rid_map(local_nec_dev_id):
    assert(local_nec_dev_id != NEC_NET_MLA_DEV);                       // @0x80a2b0
    h2r = db_get_host_device_id_to_rid_map();                          // driver query
    if !h2r: nlog_ERROR("Failed to retrieve host device ID to routing ID map ..."); return;
    maps = encd_arch_get_resource_pool(local_nec_dev_id)->device_id_maps;  // §1, +96
    // SIMD-init all three tables to their distinct sentinels
    memset_int(maps.mla_to_rid, -4, 32);  memset_int(maps.rid_to_mla, -3, 32);
    memset_int(maps.port_map,   -2, 32*4);
    // fill mla<->rid from (mla_indexes, host_device_ids, count)
    for (mla, host_dev) in h2r:
        rid = host_dev.rid;
        if nrt_is_neuron_switch_v1_family(): rid &= 0xF;               // switch_v1: mask low nibble
        assert(rid < num_mlas /* on instance family */);              // "Mismatching routing ID ..."
        maps.mla_to_rid[mla] = rid;  maps.rid_to_mla[rid] = mla;
    // for each mla, walk its 4 ports → peer rid → peer mla → port_map
    for mla in [0, count):
        for port in [0, 4):
            peer_rid = encd_arch_get_peer_rid(maps.mla_to_rid[mla], port);   // §1 (s.get_peer_rid)
            maps.port_map[mla][port] = maps.rid_to_mla[peer_rid];            // -2 stays if no peer
    maps.device_id_maps_build = true;

// The queries — pure reads, each asserting the precondition set {dev!=sentinel, mla<32, built}
function encd_arch_is_mla_available(dev, mla):    return maps.mla_to_rid[mla] >= 0;          // 0x256940
function encd_arch_mla_idx_to_rid(dev, mla):      assert(maps.mla_to_rid[mla] >= 0);
                                                  return maps.mla_to_rid[mla];               // 0x256a30
function encd_arch_rid_to_mla_idx(dev, local_rid):                                           // 0x256b30
    if nrt_is_neuron_switch_v1_family():
        local_rid &= 0xF;
        assert(encd_get_server_id(rid) == encd_get_server_id(my_rid));   // same-server check
    return maps.rid_to_mla[local_rid];
function encd_arch_get_peer_mla_idx(dev, mla, port):                                         // 0x256d50
    e = maps.port_map[mla][port];  return (e == -2) ? -22 /*-EINVAL*/ : e;   // -2 = unconnected
function encd_arch_peer_directly_connected(dev, mla, peer):                                  // 0x256ea0
    if mla == peer: return true;
    for port in [0,4): if maps.port_map[mla][port] == peer: return true;
    return false;
```

> **NOTE —** `encd_get_server_id` is present in the binary **only** as an outlined `.part.0` cold pad (`@0x256510`) that `__assert_fail`s `"TRN_PDS_ROUTING_ID_ASSERT_UNUSED(routing_id)"` (`arch_device_mapping.c:0x17`); its live body — the `rid >> 4` server-id extraction the switch-v1 same-server check uses — is **inlined** into its callers. Per the audit convention it is cited as an inlined function with its name-string and line tag, not as a callable code address. A reimplementer should treat `encd_get_server_id(rid)` as `rid >> 4` (the switch-v1 server field above the masked low nibble), recovered from the masking arithmetic at the `rid_to_mla` call site, not from a standalone function.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_arch_build_port_and_rid_map` | `0x256540` | populate `device_id_maps` once for a local device (1011 B) | HIGH |
| `encd_arch_is_mla_available` | `0x256940` | `mla_to_rid[mla] >= 0` | HIGH |
| `encd_arch_mla_idx_to_rid` | `0x256a30` | `mla_to_rid[mla]` (assert `>= 0`) | HIGH |
| `encd_arch_rid_to_mla_idx` | `0x256b30` | `rid_to_mla[rid]`; switch-v1 server-consistency check | HIGH |
| `encd_arch_get_peer_mla_idx` | `0x256d50` | `port_map[mla][port]`; `-22` if unconnected | HIGH |
| `encd_arch_peer_directly_connected` | `0x256ea0` | scan `port_map[mla][0..3]` for `peer` | HIGH |
| `encd_get_server_id` | inlined (`.part.0 @0x256510`) | `rid >> 4` server-id extraction (cold pad only) | MED |

### The libnccom seam

The NCCL fork (`aws-neuronx-collectives`, `libnccom.so.2`) does not link the `encd` floor directly; it imports a set of exported `nec_*` symbols (nm type `T`) that are **one-instruction tail-call thunks** into these queries. The thunk is the membrane: libnccom calls `nec_*`, which `jmp`s straight into `encd_arch_*`, which reads the `device_id_maps` table this section builds.

| Exported `nec_*` (libnccom imports) | Address | Tail-jumps into | Conf |
|---|---|---|---|
| `nec_build_port_and_rid_map` | `0x1bfdf0` | `encd_arch_build_port_and_rid_map @0x256540` | HIGH |
| `nec_is_mla_available` | `0x1bfe00` | `encd_arch_is_mla_available @0x256940` | HIGH |
| `nec_mla_idx_to_rid` | `0x1bfe10` | `encd_arch_mla_idx_to_rid @0x256a30` | HIGH |
| `nec_rid_to_mla_idx` | `0x1bfe20` | `encd_arch_rid_to_mla_idx @0x256b30` | HIGH |
| `nec_get_peer_mla_idx` | `0x1bfe30` | calls `encd_arch_get_peer_mla_idx @0x256d50` | HIGH |

> **QUIRK —** the `encd_arch_*` floor itself makes **zero** libnccom / `nccl*` / `dlsym` calls — every callee is another `encd_arch_*`, a `nec_vil_*`/`vtpb_*` helper, a `dmem_*`/driver query, or libc. The NCCL boundary does not *originate* here; it *terminates* here, through the exported `nec_*` thunks. So a reimplementer who searches the `encd_arch_*` band for an outgoing NCCL edge will find none and wrongly conclude the layer is NCCL-unaware — in fact it is the device-side floor the NCCL fork stands on, reached *inward* via the thunk table. The boundary itself (the `nec_*` ABI) is owned by [Comm Context and Bootstrap](comm-context.md).

---

## 4. The Replica-Group / Mesh Gating That Consumes the Maps

### Purpose

The topology answers of §1–3 exist to feed one decision: *is this collective's replica-group layout safe to run with a given mesh/RDH algorithm, or must a deadlock-avoidance fallback be forced?* That gating lives in the host composer (`enc/enc.cc`, owned by [Engine Core](engine-core.md)), but it is the dominant *consumer* of the `encd_arch_*` identity queries, so the seam is documented here. The gating reads MLA device indices and direct-connectivity through `encd_arch_get_mla_device_idx` (§2) and `encd_arch_peer_directly_connected` (§3), and sets two latches on the `enc_context`: `use_2dev_proxy` and `disable_rmv_dst_routing`.

### Algorithm — NEFF-load deadlock screening

```c
// enc_context::check_neff_replica_groups_for_deadlock_scenarios @0xfbe50 — enc.cc:2068 — top orchestrator
function check_neff_replica_groups_for_deadlock_scenarios():            // from enc_init_replica_groups
    check_replica_groups_8r_2_neighbor_dev_one_rank_per_dev();          // 0xfb270 → use_2dev_proxy
    check_replica_groups_for_deadlock_with_rmv_dst_routing();           // 0xfbcf0 → disable_rmv_dst_routing

// check_replica_groups_8r_2_neighbor_dev_one_rank_per_dev @0xfb270 — enc.cc:2083
function check_replica_groups_8r_2_neighbor_dev_one_rank_per_dev():
    if env NEURON_RT_DBG_FORCE_2DEV_PROXY: use_2dev_proxy = true; return;   // debug override
    if !(nrt_is_trn2_trn3_family() && virtual_core_size == 2
         && local_rank_n == 64 && replica_group_count > 1): return;    // family / shape gate
    for rg in replica_groups:
        a = cayman_is_group_vnc2_8r_2_neighbor_dev(rg);                // 0xfad40
        b = cayman_is_group_vnc2_8r_one_rank_per_dev(rg);             // 0xfaf00
    if (saw a 2-neighbor-dev RG) && (saw a one-rank-per-dev RG):
        use_2dev_proxy = true;                                        // both present → force proxy

// cayman_is_group_vnc2_8r_2_neighbor_dev @0xfad40 — enc.cc:2132 — the topology test
function cayman_is_group_vnc2_8r_2_neighbor_dev(rg):                  // 8-rank RG
    devs = { encd_arch_get_mla_device_idx(p) for p in rg.participants };   // §2, distinct set
    if |devs| != 2: return false;                                    // need exactly 2 MLAs
    return encd_arch_peer_directly_connected(local_dev, devs[0], devs[1]); // §3
```

> **GOTCHA —** the deadlock gating is **family- and shape-gated before it ever reads topology** — it short-circuits unless the chip is Trn2/Trn3 family (`nrt_is_trn2_trn3_family`), the virtual-core size is 2, the local rank count is exactly 64, and there is more than one replica group. A reimplementer who runs the `cayman_is_group_*` topology tests unconditionally will (a) waste the MLA-index queries on layouts that cannot deadlock and (b) risk forcing `use_2dev_proxy` on a single-group or non-Trn2/3 job that does not need it, degrading its DMA path. The gate, not the topology test, decides whether the screen runs. The companion `check_replica_groups_for_deadlock_with_rmv_dst_routing @0xfbcf0` is gated the same way on `nrt_is_neuron_switch_v1_family` plus three debug/host-cc/multistream escape hatches. These gating bodies are owned by [Engine Core](engine-core.md); this page documents only the `encd_arch_*` queries they consume.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `check_neff_replica_groups_for_deadlock_scenarios` | `0xfbe50` | top orchestrator (calls the two screens) | HIGH |
| `check_replica_groups_8r_2_neighbor_dev_one_rank_per_dev` | `0xfb270` | set `use_2dev_proxy` on 2-neighbor + one-rank-per-dev | HIGH |
| `cayman_is_group_vnc2_8r_2_neighbor_dev` | `0xfad40` | 8-rank RG → 2 MLAs directly connected? | HIGH |
| `cayman_is_group_vnc2_8r_one_rank_per_dev` | `0xfaf00` | 8-rank RG → 8 distinct MLAs, 2×4 / 4×2 split | HIGH |
| `check_replica_groups_for_deadlock_with_rmv_dst_routing` | `0xfbcf0` | set `disable_rmv_dst_routing` on multihop RGs | HIGH |

---

## 5. Verification Notes

> The forwarder model, the `nec_vil` translation, the device-identity maps, and the gating seam were cross-checked against the IDA artifacts of `libnrt.so` 2.31.24.0:
> - Every `encd_arch_*` / `nec_vil_*` address on this page resolves in `functions.json` and `addr2line`'s to `tdrv/encd/archs/arch.c`, `tdrv/encd/arch_device_mapping.c`, or `tdrv/encd/nec_vil.c` — all `local` (`t`) symbols.
> - The `s @0xc96d20` displacement of each forwarder was read from the live `jmp *s+0xNNN(%rip)` / `mov s+0xNNN(%rip),%rax` instruction and equals the `soc_struct_t` member offset in `structures.json` for every slot cited (e.g. `get_apb_bcast_m2s_offsets` disasm `s+0x130 == 304`; `get_switch_exit_port` `s+0x298 == 664`; `get_mesh_max_folds` `s+0x2e0 == 736`).
> - The `s` size (744 B), the LOCAL `.bss` symbol, and the `arch_init @0x2563f0` switch `{2→sunda, 3→cayman, 4→mariana}` are `readelf`/objdump-confirmed; the per-arch binders that fill `s` are owned by [arch-geometry](../runtime/arch-geometry.md).
> - The `NEC_NET_MLA_DEV = 0xFFFFFFFF` sentinel, the guard expression `"nec_dev_id != NEC_NET_MLA_DEV"` (`@0x845659`), and the NULL-slot guard expressions (`"s.get_max_links_per_pod_port"` `@0x845677`, `"s.get_seng_rel_devid"` `@0x845694`) are verbatim `.rodata` asserts.
> - The `device_id_maps` offset table (772 B; `port_map @+4`, `mla_to_rid @+516`, `rid_to_mla @+644`), the `-2/-3/-4` init sentinels, the `-22` no-peer return, and `MAX_MLA_DEVICES = 32` are `structures.json` + verbatim asserts (`"mla_idx < MAX_MLA_DEVICES"` `@0x84580d`, `"maps->device_id_maps_build"`).
> - The five exported `nec_*` → `encd_arch_*` tail thunks (`0x1bfdf0..0x1bfe30`, nm type `T`) are objdump-verified single-instruction `jmp`s; they are imported by `aws-neuronx-collectives`.
>
> **[MED]** `encd_get_server_id` is inlined; its `rid >> 4` body is reconstructed from the switch-v1 masking arithmetic at the `encd_arch_rid_to_mla_idx` call site, not from a standalone disassembled function (only the `.part.0` cold assert pad `@0x256510` exists).
> **[MED]** A few `soc_struct_t` member prototypes (the `(...)`-typed `get_inter_rdh_fold_n`, the bcast-offset return widths) are IDA-reconstructed from the table type, not independently re-derived; the slot *offsets* are HIGH, the exact arg lists for the non-`(void)` members are MED.
> **[LOW]** The per-arch *constant* each leaf returns is **not** asserted on this page — it is owned by [arch-geometry §2](../runtime/arch-geometry.md). This page asserts only the dispatch indirection (which accessor reaches which slot), which is HIGH.

---

## Related Components

| Name | Relationship |
|---|---|
| `arch_init` (`@0x2563f0`) | installs the `s` table this page's forwarders read; owned by [arch-geometry](../runtime/arch-geometry.md) |
| `sunda_arch_init` / `cayman_arch_init` / `mariana_arch_init` | the per-silicon binders that fill `s`'s ~93 slots (one live at runtime) |
| `encd_arch_get_resource_pool` (`@0x255aa0`) | hands out the `cc_resource_pool_context` whose `device_id_maps @+96` §3 builds |
| `vtpb_get_virtual_core_from_nec_dev_id` / `vtpb_get_vtpb_core_size` | the `vtpb` layer `nec_vil` resolves a `nec_dev_id` against |
| `tdrv_arch_get_num_tpb` (`@0x309050`) | the `tdrv_arch_ops` geometry leaf `nec_vil_get_num_vcores_per_mla` divides by core size |
| `nec_build_port_and_rid_map` (`@0x1bfdf0`) | the exported libnccom-facing thunk into `encd_arch_build_port_and_rid_map` |

## Cross-References

- [Per-Arch Device Layer: Geometry and Address Map](../runtime/arch-geometry.md) — the byte-verified constant matrix behind every `soc_struct_t` slot this page dispatches through; owns the values, this page owns the indirection
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](../runtime/tdrv-arch-ops.md) — the *userspace* 488-byte `tdrv_arch_ops` vtable; the distinct device-driver sibling of the 744-byte `soc_struct_t`, and the source of the `num_tpb` `nec_vil` reads
- [encd: Device-Side Descriptor Emitter](encd-overview.md) — the emitter floor that calls these `encd_arch_*` accessors for every routing / port / MLA question
- [encd: DMA Descriptor and devmem Floor](encd-dma-devmem.md) — the reduce worker that stamps `set_addr_routing_bits` (a per-arch slot) and the mesh-event wiring keyed by the per-arch fold table
- [Comm Context and Bootstrap](comm-context.md) — the `nec_*` ABI boundary the exported thunks of §3 belong to, and the libnccom NCCL fork that imports them
- [back to index](../index.md)
