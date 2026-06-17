# TDRV: arch-ops Dispatch and Sync-Event Accessors

> *All addresses, offsets, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`, `.rodata`, **and `.data` are VMA == file offset** (read `.data`/`.rodata` globals at their VMA directly); `.bss` is `NOBITS`. Provenance strings `/opt/workspace/KaenaRuntime/tdrv/{tdrv_arch_type.c, tpb_reg_offset.c, hw_exec_queue.c}` root every function. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — the 488-byte vtable layout is from DWARF/IDA `structures.json`; the per-arch slot fill is read verbatim from the three `tdrv_arch_register_*` decompiles; every dispatcher body and assert string is cross-checked against the binary. · Part IV — TDRV Runtime · [back to index](../index.md)*

## Abstract

`tdrv_arch_ops` is the **userspace analog of the kernel DHAL**. Where the Neuron *kernel* driver runs one arch-neutral `.c` tree on three incompatible chip generations by routing every subsystem call through `ndhal->ndhal_<area>.<method>(...)` ([kernel/dhal-core](../kernel/dhal-core.md)), the *userspace* runtime does the same trick one layer up: a single process-global struct-of-function-pointers, `tdrv_arch_ops` (`@.bss 0xc97180`; the IDA decompiler labels the symbol `tdrv_arch_ops_1`), holds every per-silicon callback the generic `tdrv` core needs, and the *value* behind each slot is chosen exactly once, at first use, by the latched architecture. The arch-neutral `tdrv_*` accessors never branch on generation — they index a fixed slot, NULL-assert it, and tail-call. This is the indirection seam between the generic tonga device-driver core and its three per-silicon leaves (**sunda** = Trainium-class arch 2, **cayman** = arch 3, **mariana** = arch 4).

The table is **488 bytes** and the gate that guards its one-time install is `tdrv_arch_ops_initialized` (`@.bss 0xc97368`); the two symbols are exactly `0x1e8` = 488 B apart, which is **61 eight-byte slots**. Of those 61 physical slots, DWARF names **47 top-level members**: 45 are scalar 8-byte slots (function pointers and two `const *` data-table pointers), and two are *embedded by-value sub-vtables* that each span several slots — `tpb_reg_offset` (a `tdrv_tpb_reg_offset_ops`, 72 B = 9 slots, at `+280`) and `seq_refill` (a `tdrv_seq_refill_ops`, 56 B = 7 slots, at `+368`). Because the sub-vtables are embedded, not pointed-to, the whole arch layer is one `.bss` object reachable by member access off `tdrv_arch_ops` with at most one extra hop for the two nested groups. This is the same embed-by-value decision the kernel DHAL makes for its 18 sub-vtables.

Unlike the kernel DHAL — which is a `kmalloc`'d global wired during PCI probe — `tdrv_arch_ops` is a **lazily-installed `.bss` singleton**. There is no explicit "init the arch layer" call on the hot path; instead *every* generic `tdrv_arch_*` accessor opens with the same four-instruction guard, and the *first* call anywhere in the process to reach that guard runs `tdrv_arch_ops_init` (`@0x308e80`), which reads `al_hal_tpb_get_arch_type()` and dispatches into one of `tdrv_arch_register_{sunda,cayman,mariana}` (each `@0x30b6a0 / 0x30c7d0 / 0x30d900`, 871 B). That registrar performs ~45 plain stores into the global and sets the gate. This page applies the recurring H3 vocabulary to four units: **(1)** the global table and its 47-member / 61-slot layout; **(2)** the lazy-singleton install and the three dispatch shapes the generic accessors use; **(3)** the per-arch registration fill, slot-by-slot, with the sunda leaf addresses; and **(4)** the two embedded sub-vtables — `tpb_reg_offset` (the TPB/APB register-offset shims) and the sync-event accessors over `sync_events`.

For reimplementation, the contract is:

- **The table layout** — `struct tdrv_arch_ops` is 488 B / 47 named members / 61 physical 8-byte slots, of which two members (`tpb_reg_offset @+280`, `seq_refill @+368`) are embedded sub-vtables; reproduce the exact byte offsets because the generic dispatchers reach slots by hard-coded displacement (`mov 0x110(%rax)` for `get_evt_addr`, etc.).
- **The lazy-singleton install** — the gate `tdrv_arch_ops_initialized @0xc97368` is checked at the head of every accessor; on first miss `tdrv_arch_ops_init` runs a `switch(al_hal_tpb_get_arch_type()){2→sunda,3→cayman,4→mariana, default→__assert_fail}` and sets the gate to 1. `tdrv_arch_ops_init` always returns 0, so the cold `ensure_arch_ops_initialized` abort arm is structurally never taken.
- **The three dispatch shapes** — (A) fn-pointer dispatch: read slot, NULL-assert, tail-call; (B) caps-field read: read `instr_block_caps->`<bool/u64>, assert the caps ptr; (C) sync-event read: read a `uint8` semaphore id from `sync_events->`<EVENT>, assert it is not `0xFF` (`TDRV_SYNC_EVENT_UNSUPPORTED`). The register-offset shims (`aws_get_*`) are a fourth, *un-gated* variant.
- **The per-arch fill** — one registrar per silicon, each ~45 stores, identical slot set, different leaf symbols + different `instr_block_caps`/`sync_events` data tables. The caps booleans and the sync-event semaphore ids are the per-generation feature matrix.

| | |
|---|---|
| **Global vtable** | `tdrv_arch_ops` (`@.bss 0xc97180`, IDA `tdrv_arch_ops_1`) — **488 B / 47 named members / 61 8-byte slots** |
| **Init gate** | `tdrv_arch_ops_initialized` (`@.bss 0xc97368`) — `int`; `0` = uninit, `1` = ready; span `0xc97368−0xc97180 = 0x1e8 = 488` |
| **Lazy install** | `tdrv_arch_ops_init` (`@0x308e80`) — `switch(al_hal_tpb_get_arch_type())` → registrar; always returns 0 |
| **Cold abort** | `ensure_arch_ops_initialized` (`@0x308e50`, `__noreturn`) — `"ret == 0 && \"Failed to initialize arch ops\""` (never reached) |
| **Registrars** | `tdrv_arch_register_sunda @0x30b6a0` · `_cayman @0x30c7d0` · `_mariana @0x30d900` — 871 B each |
| **Arch source** | `al_hal_tpb_get_arch_type` (`@0x44bca0`) → `enum al_hal_tpb_arch_type {INVALID=0, INVALID_1=1, SUNDA=2, CAYMAN=3, MARIANA=4, NUM=5}` |
| **Embedded sub-vtables** | `tpb_reg_offset` (`tdrv_tpb_reg_offset_ops`, 72 B, `@+280`) · `seq_refill` (`tdrv_seq_refill_ops`, 56 B, `@+368`) |
| **Caps table** | `instr_block_caps` (`@+248`) → `tdrv_instr_block_caps` (32 B): `sunda_instr_block_caps @0xbf6ca0`, `cayman @0xbf3680`, `mariana @0xbf3760` |
| **Sync events** | `sync_events` (`@+472`) → `tdrv_sync_events` (20 B, `uint8[20]`): `sunda_sync_events @0x9de8a0`, `cayman @0x9def80`, `mariana @0x9df660` |
| **Source TU** | `/opt/workspace/KaenaRuntime/tdrv/tdrv_arch_type.c` (assert file for all generic accessors + init) |

> **CORRECTION (TDRV-ARCH-OPS) —** earlier sibling pages ([tdrv-lifecycle](tdrv-lifecycle.md), [overview](overview.md)) describe `tdrv_arch_ops` as "488 B / 47 slots." Both numbers are facts about the *same* table but count different things, and the slot figure is the one that matters for a reimplementer reaching slots by displacement. DWARF gives **47 named members** but **61 physical 8-byte slots** — the discrepancy is the two embedded sub-vtables (`tpb_reg_offset` = 9 slots, `seq_refill` = 7 slots), each a single named member spanning multiple slots: `47 + (9−1) + (7−1) = 61 = 488/8`. This page standardizes on **488 B / 61 slots (47 named members)**; "47 slots" undercounts the physical layout by 14.

---

## 1. The Global Table — `tdrv_arch_ops`

### Purpose

`tdrv_arch_ops` (`@.bss 0xc97180`) is the entire userspace arch layer in one zero-initialized `.bss` object. It is **not** a table of pointers to sub-vtables: 45 of its members are inline 8-byte slots (43 function pointers + the two `const *` data-table pointers `instr_block_caps` and `sync_events`), and the remaining two members — `tpb_reg_offset` and `seq_refill` — are sub-vtables embedded **by value**. That single decision is the same one the kernel DHAL makes ([kernel/dhal-core §1](../kernel/dhal-core.md)): the whole arch layer is one allocation (here, one `.bss` reservation), every callback is reachable as `tdrv_arch_ops.<member>` or `tdrv_arch_ops.<group>.<member>`, and a registrar fills the slots by plain assignment into the live global.

The grouping axis is **subsystem**, not call frequency. The 47 members cluster into: device geometry (`get_num_tpb`, `get_num_seng`, `get_default_hbm_index`, `get_ptpbs_from_hbm_index`, …), DMA-engine addressing (`get_dma_eng_sdma_base`, `get_dma_engine_udma_relbase`, `is_h2d_engine`, …), the BAR/register offset map (the `tpb_reg_offset` sub-vtable + `get_tpb_mem_bar_offset` + `get_sem_inc_addr`/`get_sem_read_addr`), the instruction-block feature gate (`instr_block_caps` + `seq_refill`), the sync-event semaphore table (`sync_events` + `get_inf_init_start`), the event-address accessors (`get_evt_addr`, `get_evt_accel_addr`), the CSR register/deregister hooks (`csr_register_device`, `csr_get_regions`, `csr_deregister_device`), and three directly-stored sync-event op emitters (`add_ev_wait/set/clr`).

### Layout

The 47 named members in offset order, from DWARF `structures.json` (size 488). Offsets are **byte offsets** into the `.bss` object; "slots" are the `offset/8` index a `mov N(%rax)` dispatch reads. The two sub-vtable members are flagged; the reader should expand them via §4.

| Off | Slot | Member | Type / role | Conf |
|---|---|---|---|---|
| +0 | 0 | `get_num_dma_per_tpb` | geometry: DMA engines per TPB | CERTAIN |
| +8 | 1 | `get_num_dma_queues_per_engine` | geometry | CERTAIN |
| +16 | 2 | `get_num_seng` | geometry: sequencer-engine count | CERTAIN |
| +24 | 3 | `get_num_tpb_per_seng` | geometry | CERTAIN |
| +32 | 4 | `get_num_tpb` | geometry: TPBs on the core | CERTAIN |
| +40 | 5 | `get_num_tpb_per_hbm` | geometry | CERTAIN |
| +48 | 6 | `get_num_topsp` | geometry: TopSP count | CERTAIN |
| +56 | 7 | `get_default_hbm_index` | geometry: default HBM channel | CERTAIN |
| +64 | 8 | `get_ptpbs_from_hbm_index` | topology: ptpb set for an HBM group | CERTAIN |
| +72 | 9 | `get_dma_queue_alloc_table` | DMA: queue allocation table | CERTAIN |
| +80 | 10 | `get_completion_handle_mem_location` | exec: completion-handle placement | CERTAIN |
| +88 | 11 | `get_seed_set_workspace_offset` | stochastic-rounding seed workspace | CERTAIN |
| +96 | 12 | `supports_collectives_dma_props` | bool: CC DMA props | CERTAIN |
| +104 | 13 | `get_tpb_mem_bar_offset` | BAR: TPB-mem BAR offset | CERTAIN |
| +112 | 14 | `get_tpb_csr_apb_bar_offset` | BAR: CSR-APB BAR offset | CERTAIN |
| +120 | 15 | `get_tpb_notific_apb_bar_offset` | BAR: notific-APB BAR offset | CERTAIN |
| +128 | 16 | `get_dma_eng_sdma_misc_base` | DMA-engine SDMA misc base | CERTAIN |
| +136 | 17 | `get_dma_eng_ens_base` | DMA-engine ENS base | CERTAIN |
| +144 | 18 | `get_nx_core_type` | ucode: NX core type | CERTAIN |
| +152 | 19 | `get_q7_pool_core_type` | ucode: Q7/pool core type | CERTAIN |
| +160 | 20 | `supports_embedded_sem` | bool: embedded semaphores | CERTAIN |
| +168 | 21 | `dve_dynamic_config_init` | DVE dynamic config init hook | CERTAIN |
| +176 | 22 | `act_local_storage_tbls_init` | ACT local-storage table init | CERTAIN |
| +184 | 23 | `get_dma_queue_regs_bcast_offset` | DMA: bcast reg offset | CERTAIN |
| +192 | 24 | `get_dma_eng_sdma_base` | DMA-engine SDMA base | CERTAIN |
| +200 | 25 | `get_dma_engine_udma_relbase` | DMA-engine UDMA relbase | CERTAIN |
| +208 | 26 | `get_dma_engine_bar_offset_adjustment` | DMA BAR adjust | CERTAIN |
| +216 | 27 | `get_h2d_dma_engine_id` | DMA: host-to-device engine id | CERTAIN |
| +224 | 28 | `is_h2d_engine` | bool: engine is H2D | CERTAIN |
| +232 | 29 | `get_dma_eng_sdma_misc_relbase` | DMA misc relbase | CERTAIN |
| +240 | 30 | `supports_data_tail_ptr_inc` | bool: data tail-ptr increment | CERTAIN |
| +248 | 31 | `instr_block_caps` | `const tdrv_instr_block_caps*` — IB feature gate (§4.3) | CERTAIN |
| +256 | 32 | `get_sim_debug_flags_offset` | sim debug-flags MMIO offset | CERTAIN |
| +264 | 33 | `get_evt_accel_addr` | event-accel carveout addr (`bool set`) | CERTAIN |
| +272 | 34 | `get_evt_addr` | per-TPB sync-event MMIO addr `(tpb_idx,event_id)` | CERTAIN |
| +280 | 35–43 | **`tpb_reg_offset`** | embedded `tdrv_tpb_reg_offset_ops` (72 B / 9 slots) — §4.1 | CERTAIN |
| +352 | 44 | `get_sem_inc_addr` | semaphore-increment MMIO addr | CERTAIN |
| +360 | 45 | `get_sem_read_addr` | semaphore-read MMIO addr | CERTAIN |
| +368 | 46–52 | **`seq_refill`** | embedded `tdrv_seq_refill_ops` (56 B / 7 slots) — §4.2 | CERTAIN |
| +424 | 53 | `csr_register_device` | CSR region register hook | CERTAIN |
| +432 | 54 | `csr_get_regions` | CSR region enumerate | CERTAIN |
| +440 | 55 | `csr_deregister_device` | CSR region deregister | CERTAIN |
| +448 | 56 | `add_ev_wait` | emit "wait sync-event id" into an IB chunk | CERTAIN |
| +456 | 57 | `add_ev_set` | emit "set sync-event id" | CERTAIN |
| +464 | 58 | `add_ev_clr` | emit "clear sync-event id" | CERTAIN |
| +472 | 59 | `sync_events` | `const tdrv_sync_events*` — semaphore-id table (§4.3) | CERTAIN |
| +480 | 60 | `get_inf_init_start` | INF_INIT_START sync-event accessor | CERTAIN |

> **NOTE —** the slot column makes the dispatch verifiable: `tdrv_arch_get_evt_addr` decompiles to `get_evt_addr = tdrv_arch_ops_1.get_evt_addr; … return get_evt_addr(...)`, and the disassembly reads `mov 0x110(%rax)` — `0x110 = 272 = slot 34`, matching the table exactly. Likewise `add_ev_wait` is `mov 0x1c0(%rax)` (`0x1c0 = 448 = slot 56`) and `sync_events` is `mov 0x1d8(%rax)` (`0x1d8 = 472 = slot 59`).

### Considerations

The embed-by-value design means the arch layer has **no internal pointers to free**: `tdrv_arch_ops` lives in `.bss` for the life of the process and is never torn down. It also means a registrar that returned early would leave a **partially-populated** global with a mix of set and NULL slots — but the registrars are straight-line stores with no fallible work (§3), and the gate is set only after the registrar returns, so a partial table is never observed by a second accessor. The two `const *` data members (`instr_block_caps`, `sync_events`) point at per-arch `.rodata`/`.data` tables (§4.3), not at functions — they are the static feature matrix, read directly without an indirect call.

---

## 2. The Lazy-Singleton Install and Dispatch Shapes

### Purpose

There is no explicit "initialize the arch layer" call on the runtime hot path. Instead the table is a **lazy singleton**: every generic `tdrv_arch_*` accessor opens with the same guard, and whichever accessor fires *first* in the process triggers the one-time install. This is the structural difference from the kernel DHAL, which is built eagerly during `neuron_pci_probe` ([kernel/dhal-core §2](../kernel/dhal-core.md)); the userspace table defers until first use because the silicon arch is not known until `al_hal_tpb_get_arch_type()` can read it from an opened device.

### Entry Point

```text
<any tdrv_arch_* accessor>                       ── reads tdrv_arch_ops_initialized @0xc97368
  └─ tdrv_arch_ops_init (0x308e80)               ── one-shot; runs only on first miss
       └─ al_hal_tpb_get_arch_type (0x44bca0)    ── 2=SUNDA / 3=CAYMAN / 4=MARIANA
       └─ switch:
            case 2 → tdrv_arch_register_sunda    (0x30b6a0, 871 B)   ── §3
            case 3 → tdrv_arch_register_cayman   (0x30c7d0, 871 B)
            case 4 → tdrv_arch_register_mariana  (0x30d900, 871 B)
            default→ __assert_fail("0 && \"Unknown architecture\"", tdrv_arch_type.c:0x41)
       └─ tdrv_arch_ops_initialized = 1
  └─ (cold, never reached) ensure_arch_ops_initialized (0x308e50, __noreturn)
```

### Algorithm

The install dispatcher, modelling `tdrv_arch_ops_init` (`@0x308e80`) verbatim:

```c
function tdrv_arch_ops_init():                        // 0x308e80, tdrv_arch_type.c
    if tdrv_arch_ops_initialized:                      // 0xc97368 — re-entrancy fast path
        return 0
    arch = al_hal_tpb_get_arch_type()                  // 0x44bca0 → enum al_hal_tpb_arch_type
    switch arch:
        case CAYMAN /*3*/: tdrv_arch_register_cayman()  // 0x30c7d0
        case MARIANA/*4*/: tdrv_arch_register_mariana() // 0x30d900
        case SUNDA  /*2*/: tdrv_arch_register_sunda()   // 0x30b6a0
        default:
            __assert_fail("0 && \"Unknown architecture\"",
                          "/opt/workspace/KaenaRuntime/tdrv/tdrv_arch_type.c", 0x41, ...)
    tdrv_arch_ops_initialized = 1                       // gate set AFTER the registrar returns
    return 0                                            // ALWAYS 0 on success
```

The guard that every generic accessor inlines at its head, and the three dispatch shapes the accessors take. Shape A (fn-pointer dispatch) models `tdrv_arch_get_evt_addr` (`@0x309f50`); shape B (caps read) models `tdrv_arch_instr_block_supports_hw_dge` (`@0x30a020`); shape C (sync-event read) models `tdrv_sync_get_inference_start` (`@0x30a5c0`):

```c
// The shared install guard, inlined at the head of EVERY generic accessor:
macro ENSURE_ARCH_OPS():
    if !tdrv_arch_ops_initialized:                      // 0xc97368
        if tdrv_arch_ops_init():                        // 0x308e80 — but it always returns 0,
            ensure_arch_ops_initialized()               //   so this __noreturn arm is dead code

// SHAPE A — function-pointer dispatch (e.g. get_evt_addr, slot 34 @+272):
function tdrv_arch_get_evt_addr(tpb_idx, event_id):     // 0x309f50
    ENSURE_ARCH_OPS()
    fn = tdrv_arch_ops.get_evt_addr                      // mov 0x110(%rax)
    if fn == NULL:
        __assert_fail("tdrv_arch_ops.get_evt_addr != NULL", tdrv_arch_type.c, 0x189, ...)
    return fn(tpb_idx, event_id)                         // tail-call into the per-arch leaf

// SHAPE B — caps-field read (e.g. supports_hw_dge, caps slot 31 @+248):
function tdrv_arch_instr_block_supports_hw_dge():       // 0x30a020
    ENSURE_ARCH_OPS()
    caps = tdrv_arch_ops.instr_block_caps                // const tdrv_instr_block_caps*
    if caps == NULL:
        __assert_fail("tdrv_arch_ops.instr_block_caps != NULL", tdrv_arch_type.c, 0x19C, ...)
    return caps->supports_hw_dge                         // bool @ caps+8; NO indirect call

// SHAPE C — sync-event read (e.g. INFERENCE_START, sync_events slot 59 @+472):
function tdrv_sync_get_inference_start():               // 0x30a5c0
    ENSURE_ARCH_OPS()
    id = tdrv_arch_ops.sync_events->INFERENCE_START      // uint8 @ sync_events+0
    if id == 0xFF:                                        // TDRV_SYNC_EVENT_UNSUPPORTED
        __assert_fail("tdrv_arch_ops.sync_events->INFERENCE_START != TDRV_SYNC_EVENT_UNSUPPORTED",
                      tdrv_arch_type.c, 0x203, ...)
    return id
```

### Function Map

| Function | Addr | Shape | Role | Confidence |
|---|---|---|---|---|
| `tdrv_arch_ops_init` | `0x308e80` | — | one-shot installer; `switch(arch)` → registrar; returns 0 | CERTAIN |
| `ensure_arch_ops_initialized` | `0x308e50` | — | `__noreturn` cold abort; structurally never reached | CERTAIN |
| `al_hal_tpb_get_arch_type` | `0x44bca0` | — | returns latched `enum al_hal_tpb_arch_type` (boundary) | CERTAIN |
| `tdrv_arch_get_evt_addr` | `0x309f50` | A | slot 34 → per-arch `get_evt_addr` | CERTAIN |
| `tdrv_arch_get_evt_accel_addr` | `0x309ef0` | A | slot 33 → evt-accel carveout addr | CERTAIN |
| `tdrv_arch_get_sim_debug_flags_offset` | `0x309e90` | A | slot 32 → sim debug-flags offset | CERTAIN |
| `tdrv_arch_get_num_tpb` | `0x309050` | A | slot 4 → TPB count | CERTAIN |
| `tdrv_arch_get_default_hbm_index` | `0x3093d0` | A | slot 7 → default HBM channel | CERTAIN |
| `tdrv_arch_get_sem_inc_addr` | `0x309a60` | A | slot 44 → semaphore-inc addr | CERTAIN |
| `tdrv_arch_get_sem_read_addr` | `0x309ad0` | A | slot 45 → semaphore-read addr | CERTAIN |
| `tdrv_arch_refill_rings_create` | `0x309c60` | A | `seq_refill.refill_rings_create` (sub-vtable, §4.2) | CERTAIN |
| `tdrv_add_ev_wait` | `0x30a470` | A | slot 56 → emit wait-event op | CERTAIN |
| `tdrv_add_ev_set` | `0x30a4e0` | A | slot 57 → emit set-event op | CERTAIN |
| `tdrv_add_ev_clr` | `0x30a550` | A | slot 58 → emit clear-event op | CERTAIN |
| `tdrv_arch_instr_block_supports_hw_dge` | `0x30a020` | B | `caps->supports_hw_dge` | CERTAIN |
| `tdrv_sync_get_inference_start` | `0x30a5c0` | C | `sync_events->INFERENCE_START`, assert ≠ 0xFF | CERTAIN |
| `tdrv_sync_get_hw_exec_queue_request_load` | `0x30aab0` | C | `sync_events->HW_EXEC_QUEUE_REQUEST_LOAD` | HIGH |
| `tdrv_sync_get_collectives_ctx_load` | `0x30ab10` | C | `sync_events->COLLECTIVES_CTX_LOAD` | HIGH |

### Considerations

> **QUIRK —** the `ensure_arch_ops_initialized` abort arm (`0x308e50`) is dead code by construction. `tdrv_arch_ops_init` returns `0` on **every** success path and only ever `__assert_fail`s (never returns non-zero) on an unknown arch, so the inlined guard `if (tdrv_arch_ops_init()) ensure_arch_ops_initialized();` can never take the call. It exists because the source was written defensively as `ret = tdrv_arch_ops_init(); assert(ret == 0 && "Failed to initialize arch ops");` and the compiler outlined the `assert` body into a shared `__noreturn` cold function (`tdrv_arch_type.c:0x51`). A reimplementer can drop the second assert entirely; it guards an impossible state.

> **GOTCHA —** the install is **not** thread-safe. The guard is a plain `if (!tdrv_arch_ops_initialized)` with no lock, no atomic, and no double-check — unlike the kernel DHAL's `DEFINE_MUTEX(ndhal_init_lock)` double-checked locking ([kernel/dhal-core §2](../kernel/dhal-core.md)). Two threads racing the *first* `tdrv_arch_*` call can both observe the gate unset and both run `tdrv_arch_register_*`. The registrar stores are idempotent (same leaf addresses written twice), and the gate write is a single aligned store, so the race is benign in practice — but it is benign only because the registration is pure repeated assignment of constants. A reimplementer who adds allocation or any non-idempotent side effect to a registrar **must** add a lock; the shipped code relies on the install being a replayable sequence of constant stores. In normal runtime use the first arch accessor is reached deep inside single-threaded `tdrv_init` ([tdrv-lifecycle §1](tdrv-lifecycle.md)), so the race window never opens.

---

## 3. Per-Arch Registration — `tdrv_arch_register_{sunda,cayman,mariana}`

### Purpose

Each of the three registrars (`@0x30b6a0 / 0x30c7d0 / 0x30d900`, 871 B each) performs the per-silicon fill: ~45 plain stores into `tdrv_arch_ops`, plus the two `const *` data-table pointers and the two embedded sub-vtables. All three write the **same slot set** — the difference between generations is entirely (a) which leaf symbol each function-pointer slot gets, and (b) which `instr_block_caps` / `sync_events` data table the two pointer slots get. There is no V4-on-V3 layering as in the kernel DHAL; each userspace registrar is self-contained and writes every slot it owns.

### Algorithm

The sunda registrar, normalized from the SIMD-coalesced decompile of `tdrv_arch_register_sunda` (`@0x30b6a0`). The compiler fuses adjacent pointer stores into 16-byte `movdqa` pair-writes (`_mm_unpacklo_epi64`), which is why the raw decompile shows ~22 statements for ~45 stores; expanded to one store per slot:

```c
function tdrv_arch_register_sunda():                          // 0x30b6a0
    A = &tdrv_arch_ops                                         // 0xc97180

    // --- geometry / topology (slots 0..11) ---
    A.get_num_dma_per_tpb            = ..._num_dma_per_tpb_sunda
    A.get_num_dma_queues_per_engine  = tdrv_arch_get_num_dma_queues_per_engine_sunda
    A.get_num_seng                   = ...; A.get_num_tpb_per_seng = ..._per_seng_sunda
    A.get_num_tpb                    = tdrv_arch_get_num_tpb_sunda          // 0x30b540
    A.get_num_tpb_per_hbm            = ...; A.get_num_topsp = ...
    A.get_default_hbm_index          = tdrv_arch_get_default_hbm_index_sunda // 0x30af90
    A.get_ptpbs_from_hbm_index       = ...; A.get_dma_queue_alloc_table = ...
    A.get_completion_handle_mem_location = ...; A.get_seed_set_workspace_offset = ...

    // --- caps / DMA-engine / BAR / ucode (slots 12..30) ---
    A.supports_collectives_dma_props = ...
    A.get_tpb_mem_bar_offset         = tdrv_arch_get_tpb_mem_bar_offset_sunda
    A.get_tpb_csr_apb_bar_offset     = ...; A.get_tpb_notific_apb_bar_offset = ...
    A.get_dma_eng_sdma_misc_base / _ens_base / _sdma_base / udma_relbase / ... = ..._sunda
    A.get_nx_core_type               = ...; A.get_q7_pool_core_type = ...
    A.supports_embedded_sem          = tdrv_arch_supports_embedded_sem_sunda
    A.dve_dynamic_config_init        = dve_dynamic_config_init_sunda
    A.act_local_storage_tbls_init    = act_local_storage_tbls_setup
    A.is_h2d_engine / get_h2d_dma_engine_id / ... = ..._sunda
    A.supports_data_tail_ptr_inc     = tdrv_arch_supports_data_tail_ptr_inc_sunda

    // --- the two const* DATA tables (NOT functions) ---
    A.instr_block_caps               = &sunda_instr_block_caps      // 0xbf6ca0 (§4.3)
    A.sync_events                    = &sunda_sync_events           // 0x9de8a0 (§4.3)

    // --- event-addr + sim-debug (slots 32..34) ---
    A.get_sim_debug_flags_offset     = tdrv_arch_get_sim_debug_flags_offset_sunda // 0x30b0a0
    A.get_evt_accel_addr             = tdrv_arch_get_evt_accel_addr_sunda         // 0x30b0b0
    A.get_evt_addr                   = tdrv_arch_get_evt_addr_sunda               // 0x30b1b0

    // --- embedded tpb_reg_offset sub-vtable (slots 35..43) ---  §4.1
    A.tpb_reg_offset.get_tpb_mem_offset    = get_tpb_mem_offset_sunda             // 0x30b4e0
    A.tpb_reg_offset.get_apb_base          = get_apb_base_sunda                   // 0x30b3b0
    A.tpb_reg_offset.get_tpb_dve_seed_set_reg_offset = get_tpb_dve_seed_set_reg_offset_sunda
    A.tpb_reg_offset.{get_tpb_top_apb_offset, get_tpb_csr_apb_offset,
        get_tpb_notific_apb_offset, get_tpb_addr, get_apb_pcie_base,
        get_apb_dma_base}                  = ..._sunda

    // --- semaphore addr (slots 44..45) ---
    A.get_sem_inc_addr               = ...; A.get_sem_read_addr = tdrv_arch_get_sem_read_addr_sunda // 0x30b190

    // --- embedded seq_refill sub-vtable (slots 46..52) ---  §4.2
    A.seq_refill.{get_refill_queue_tpb_ids, get_refill_queue_h2d_fallback_ids,
        refill_rings_create, refill_rings_hw_init,
        dma_get_queue_tail_inc_offset, dma_get_queue_tail_offset,
        dma_get_queue_head_offset}         = sequencer_*_sunda

    // --- CSR hooks (slots 53..55) ---
    A.csr_register_device            = tdrv_arch_csr_register_device_sunda  // 0x30b110
    A.csr_get_regions                = ...; A.csr_deregister_device = ...

    // --- directly-stored sync-event op emitters (slots 56..58) — ARCH-AGNOSTIC ---
    A.add_ev_wait = add_ev_wait     // 0x273c50   (NOT *_sunda — shared across all arches)
    A.add_ev_set  = add_ev_set      // 0x273c00
    A.add_ev_clr  = add_ev_clr      // 0x273ca0

    // --- inf_init_start (slot 60) ---
    A.get_inf_init_start             = tdrv_arch_get_inf_init_start_sunda   // 0x30b250
```

### Function Map

A representative subset of the sunda leaf implementations the registrar installs (addresses nm-verified). Cayman/mariana install the analogous `_cayman` / `_mariana` leaves into the *same* slots — e.g. slot 34 gets `tdrv_arch_get_evt_addr_cayman @0x30c190` and `tdrv_arch_get_evt_addr_mariana @0x30d2b0`.

| Slot | Member | Sunda leaf | Addr | Confidence |
|---|---|---|---|---|
| 4 | `get_num_tpb` | `tdrv_arch_get_num_tpb_sunda` | `0x30b540` | CERTAIN |
| 7 | `get_default_hbm_index` | `tdrv_arch_get_default_hbm_index_sunda` | `0x30af90` | CERTAIN |
| 32 | `get_sim_debug_flags_offset` | `tdrv_arch_get_sim_debug_flags_offset_sunda` | `0x30b0a0` | CERTAIN |
| 33 | `get_evt_accel_addr` | `tdrv_arch_get_evt_accel_addr_sunda` | `0x30b0b0` | CERTAIN |
| 34 | `get_evt_addr` | `tdrv_arch_get_evt_addr_sunda` | `0x30b1b0` | CERTAIN |
| 35 | `tpb_reg_offset.get_tpb_mem_offset` | `get_tpb_mem_offset_sunda` | `0x30b4e0` | CERTAIN |
| 40 | `tpb_reg_offset.get_apb_base` | `get_apb_base_sunda` | `0x30b3b0` | CERTAIN |
| 45 | `get_sem_read_addr` | `tdrv_arch_get_sem_read_addr_sunda` | `0x30b190` | CERTAIN |
| 53 | `csr_register_device` | `tdrv_arch_csr_register_device_sunda` | `0x30b110` | CERTAIN |
| 56 | `add_ev_wait` | `add_ev_wait` (shared) | `0x273c50` | CERTAIN |
| 57 | `add_ev_set` | `add_ev_set` (shared) | `0x273c00` | CERTAIN |
| 58 | `add_ev_clr` | `add_ev_clr` (shared) | `0x273ca0` | CERTAIN |
| 60 | `get_inf_init_start` | `tdrv_arch_get_inf_init_start_sunda` | `0x30b250` | CERTAIN |

### Considerations

> **QUIRK —** three slots are **not** per-arch. The sync-event op emitters `add_ev_wait/set/clr` (slots 56–58) are stored as the bare symbols `add_ev_wait @0x273c50`, `add_ev_set @0x273c00`, `add_ev_clr @0x273ca0` — *not* `_sunda`/`_cayman`/`_mariana` variants — in all three registrars. The instruction encoding for "wait/set/clear sync-event id" into an IB chunk is identical across generations, so the same leaf is installed everywhere. A reimplementer who mechanically appends `_<arch>` to every slot's leaf name will fabricate three symbols that do not exist. These three are the userspace analog of the kernel DHAL's `vc` (version-common) base — the slots whose implementation does not vary by generation.

> **NOTE —** the registrar decompiles look smaller than 45 stores because the compiler coalesces adjacent same-width slot writes into 16-byte `movdqa` pair-stores (`_mm_unpacklo_epi64(load(off_BF35xx), (m128)leaf_addr)`), pulling the *left* half of each pair from a `.data.rel.ro` relocation pool (`off_BF35C0..off_BF3660`) and the *right* half from the immediate leaf address. The pairing is a code-size optimization, not a semantic grouping; expand each `movdqa` into its two 8-byte slot stores to recover the per-slot fill above. The three registrars are byte-for-byte the same size (871 B) precisely because they share this fused store pattern and differ only in the relocation targets.

---

## 4. The Embedded Sub-Vtables and Static Feature Tables

The two embedded sub-vtable members and the two `const *` data tables are where the per-arch data — register offsets, IB feature gates, and sync-event semaphore ids — actually lives.

### 4.1 `tpb_reg_offset` — the TPB/APB Register-Offset Shims

`tpb_reg_offset` (`@+280`, `tdrv_tpb_reg_offset_ops`, 72 B / 9 slots) is the register-offset address map. Its 9 slots are consumed not by `tdrv_arch_*` accessors but by a separate family of **public `aws_get_*` C-API shims** in `tpb_reg_offset.c` (`@0x310420..0x310655`), each of which reads one slot, NULL-asserts it, and tail-calls:

| Sub-slot | Member | Public shim | Addr | Conf |
|---|---|---|---|---|
| +0 | `get_tpb_mem_offset` | `aws_get_tpb_mem_offset` | `0x310420` | CERTAIN |
| +8 | `get_tpb_top_apb_offset` | `aws_get_tpb_top_apb_offset` | `0x310460` | CERTAIN |
| +16 | `get_tpb_csr_apb_offset` | `aws_get_tpb_csr_apb_offset` | `0x3104a0` | CERTAIN |
| +24 | `get_tpb_notific_apb_offset` | `aws_get_tpb_notific_apb_offset` | `0x3104e0` | CERTAIN |
| +32 | `get_tpb_addr` | `aws_get_tpb_addr` | `0x310520` | CERTAIN |
| +40 | `get_apb_base` | `aws_get_apb_base` | `0x310560` | CERTAIN |
| +48 | `get_apb_pcie_base` | `aws_get_apb_pcie_base` | `0x3105a0` | CERTAIN |
| +56 | `get_apb_dma_base` | `aws_get_apb_dma_base` | `0x3105e0` | CERTAIN |
| +64 | `get_tpb_dve_seed_set_reg_offset` | `aws_get_tpb_dve_seed_set_reg_offset` | `0x310620` | CERTAIN |

```c
// The register-offset shims are a FOURTH dispatch shape — UN-GATED:
function aws_get_apb_base(pcore):                          // 0x310560, tpb_reg_offset.c
    fn = tdrv_arch_ops.tpb_reg_offset.get_apb_base          // sub-slot +40 (parent +280+40 = +320)
    if fn == NULL:
        __assert_fail("tdrv_arch_ops.tpb_reg_offset.get_apb_base != NULL",
                      "/opt/workspace/KaenaRuntime/tdrv/tpb_reg_offset.c", 0x2C, ...)
    return fn(pcore)                                        // NO ENSURE_ARCH_OPS() guard
```

> **GOTCHA —** the `aws_get_*` shims **omit the lazy-install guard**. Unlike the `tdrv_arch_*` accessors of §2, an `aws_get_*` shim reads `tdrv_arch_ops.tpb_reg_offset.<slot>` directly with no `if (!tdrv_arch_ops_initialized) tdrv_arch_ops_init()` preamble. If one is ever called before any `tdrv_arch_*` accessor has triggered the install, the slot is a NULL `.bss` cell and the function `__assert_fail`s on the `!= NULL` check rather than lazily initializing. In the shipped runtime this never happens because `tdrv_init` reaches a guarded `tdrv_arch_*` accessor long before any `aws_get_*` consumer ([tdrv-lifecycle §1](tdrv-lifecycle.md)), but a reimplementation that exposes `aws_get_*` as a standalone entry point must add the install guard or document the ordering precondition. The honest reading: the table install is an invariant the `aws_get_*` shims *assume*, not one they *enforce*.

### 4.2 `seq_refill` — the Sequencer Refill-Ring Sub-Vtable

`seq_refill` (`@+368`, `tdrv_seq_refill_ops`, 56 B / 7 slots) groups the seq-engine refill-ring lifecycle and the DMA-queue MMIO offset getters. Three are reached through `tdrv_arch_*` trampolines in this layer; the others belong to adjacent cells.

| Sub-slot | Member | Generic accessor | Conf |
|---|---|---|---|
| +0 | `get_refill_queue_tpb_ids` | (other cell) | HIGH |
| +8 | `get_refill_queue_h2d_fallback_ids` | (other cell) | HIGH |
| +16 | `refill_rings_create` | `tdrv_arch_refill_rings_create @0x309c60` | CERTAIN |
| +24 | `refill_rings_hw_init` | `tdrv_arch_refill_rings_hw_init @0x309cd0` | CERTAIN |
| +32 | `dma_get_queue_tail_inc_offset` | `tdrv_arch_dma_get_queue_tail_inc_offset @0x309d40` | CERTAIN |
| +40 | `dma_get_queue_tail_offset` | `tdrv_arch_dma_get_queue_tail_offset @0x309db0` | CERTAIN |
| +48 | `dma_get_queue_head_offset` | `tdrv_arch_dma_get_queue_head_offset @0x309e20` | CERTAIN |

### 4.3 `instr_block_caps` and `sync_events` — the Static Feature Matrix

The two `const *` data members point at per-arch tables — these are the userspace arch layer's *data* counterpart to the function-pointer slots, the per-generation feature matrix read with no indirect call.

`instr_block_caps` (`@+248`) → `tdrv_instr_block_caps` (32 B): one `uint64` base + ten feature booleans + a validity-check fn. Read by the twelve shape-B `tdrv_arch_instr_block_*` accessors, which drive instruction-block (IB) codegen:

| Off | Field | Accessor | Conf |
|---|---|---|---|
| +0 | `tpb_mem_relbase` (u64) | `tdrv_arch_instr_block_get_tpb_mem_relbase` | CERTAIN |
| +8 | `supports_hw_dge` | `..._supports_hw_dge` | CERTAIN |
| +9 | `supports_sw_dge_notif` | `..._supports_sw_dge_notif` | CERTAIN |
| +10 | `supports_xt_feature_flags` | `..._supports_xt_feature_flags` | CERTAIN |
| +11 | `supports_evt_accel_carveout` | `..._supports_evt_accel_carveout` | CERTAIN |
| +12 | `supports_legacy_relaxed_ordering` | `..._supports_/requires_legacy_relaxed_ordering` | CERTAIN |
| +13 | `supports_fp8_conv_config` | `..._supports_fp8_conv_config` | CERTAIN |
| +14 | `supports_bg_transpose` | `..._supports_bg_transpose` | CERTAIN |
| +15 | `supports_dma_indirect1d_bound_check` | `..._supports_dma_indirect1d_bound_check` | CERTAIN |
| +16 | `supports_pool_ulib_config` | `..._supports_pool_ulib_config` | CERTAIN |
| +17 | `supports_args_table_swap_all_engines` | `..._supports_args_table_swap_all_engines` | CERTAIN |
| +24 | `final_inst_validity_check` (fn) | `tdrv_arch_instr_block_final_inst_validity_check` | CERTAIN |

Per-arch caps tables: `sunda_instr_block_caps @0xbf6ca0`, `cayman_instr_block_caps @0xbf3680`, `mariana_instr_block_caps @0xbf3760` — the bool matrix across these three is the IB feature gate per generation.

> **NOTE —** `requires_legacy_relaxed_ordering` (`tdrv_arch_instr_block_requires_legacy_relaxed_ordering`) is the one shape-B variant that takes an argument: it reads `caps->supports_legacy_relaxed_ordering` (caps +12) *and* AND-s it with `kbin->target ≤ CAYMAN(3)`, NULL-asserting the `kbin_t*` first (`"kbin != NULL"`, `tdrv_arch_type.c:0x1C0`). So a caps bool can be "supported" yet "not required" on mariana — the requires-accessor is a function of both the static caps table and the target arch carried in the kbin.

`sync_events` (`@+472`) → `tdrv_sync_events` (20 B): a `uint8[20]` of semaphore ids, one per named sync event, with `0xFF` = `TDRV_SYNC_EVENT_UNSUPPORTED`. The shape-C accessors read one byte and assert it is not `0xFF`:

```text
+0  INFERENCE_START               +7  INF_INIT_LAST                +14 COLLECTIVES_CTX_LOAD
+1  CORE_BARRIER                  +8  IOQ_CODE_SWITCH              +15 COLLECTIVE_TOPSP_ACK_FIRST
+2  SYNC_BARRIER                  +9  RANGE_CHECK_START            +16 COLLECTIVE_TOPSP_ACK_LAST
+3  PREAMBLE_PINNED_QUEUES_START  +10 RANGE_CHECK_LAST             +17 DEBUG_TENSOR_READ_EVENT_SYNC_START
+4  PREAMBLE_PINNED_QUEUES_LAST   +11 ACT_TABLES_LOAD              +18 DEBUG_TENSOR_READ_EVENT_SYNC_LAST
+5  TOPSP_STOP_SIGNAL             +12 DVE_TABLES_LOAD              +19 NUM_RESERVED_SEMAPHORES
+6  INF_INIT_START                +13 HW_EXEC_QUEUE_REQUEST_LOAD
```

Per-arch tables: `sunda_sync_events @0x9de8a0`, `cayman_sync_events @0x9def80`, `mariana_sync_events @0x9df660`. A generation that does not implement an event stores `0xFF` in its slot, and the corresponding `tdrv_sync_get_*` accessor aborts if asked for it — this is how the runtime gates collectives / debug-tensor features per silicon without an `#ifdef`.

### Considerations

The split between function-pointer slots and the two `const *` data tables is the same factoring the kernel DHAL uses (function-pointer sub-vtables vs. the pure-scalar `ndhal_arch`/`ndhal_address_map`/`ndhal_udma` tables, [kernel/dhal-core §1](../kernel/dhal-core.md)): callbacks for behavior that *acts* on hardware, static tables for the per-generation *constants* (feature bools, semaphore ids). A reimplementer should keep the two separate — the caps/sync-event tables can be expressed as plain `static const` arrays selected by arch, while only the genuinely-varying behaviors need indirect dispatch.

## Related Components

| Name | Relationship |
|---|---|
| `hw_exec_queue_add_exec_request_impl` (`0x320810`) | calls `tdrv_arch_get_evt_addr` (slot 34) + `tdrv_sync_get_hw_exec_queue_request_load` (slot 59) to build the HW-exec-queue event-write descriptor |
| `ib_create_one_block` / `ib_add_inference_wait_v2` | dominant consumers of the shape-B caps reads + `add_ev_wait/set` (slots 56–57) |
| `tpb_eng_init_hals_v2` | calls the three `seq_refill` DMA-offset getters (sub-slots +32/+40/+48) |
| `tdrv_init` / `tdrv_destroy` | trigger the lazy install (first arch accessor) and consume `tdrv_sync_get_inference_start` at teardown |
| `tdrv_arch_register_{sunda,cayman,mariana}` | the per-silicon fill bodies whose leaf implementations specialize every slot |

## Cross-References

- [Per-Arch Device Layer: Geometry and Address Map](arch-geometry.md) — the `get_num_*` / `get_*_hbm_index` geometry leaves behind slots 0–11 and the `get_dma_eng_*` addressing slots
- [Per-Arch Device Layer: CSR and Register-Offset Accessors](arch-csr-offsets.md) — the concrete per-arch return values of the `tpb_reg_offset` shims (§4.1) and the `csr_register_device`/`csr_get_regions` leaves
- [KaenaHal: Overview and Platform-Services Adapter](hal-adapter.md) — `al_hal_tpb_get_arch_type`, the arch latch that `tdrv_arch_ops_init` reads to pick a registrar
- [DHAL Core (ndhal Vtable-of-Vtables)](../kernel/dhal-core.md) — the kernel-side analog: the same embed-by-value vtable-of-vtables pattern, eagerly built at PCI probe with double-checked locking instead of this lazy `.bss` singleton
- [TDRV: Device Bring-Up and Lifecycle](tdrv-lifecycle.md) — where the first arch accessor fires during `tdrv_init`, triggering the one-time install
