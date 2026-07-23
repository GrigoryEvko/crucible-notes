# Host Execution-State Structs + Census Close

> **Scope — the host runtime's execution-state object tree, and the close of the
> STRUCT lane.** This appendix does two jobs. **(1)** It lays out, field-by-field,
> the host `libnrt.so` structures that carry an *in-flight* GPSIMD inference: the
> `model_t` NEFF container, the kbl exec / HW-queue tree
> (`hw_exec_queue_t` / `exec_request_state` / `tpb_xu_schedule`), the
> `dge_mailbox` DMA-priority gate, and the `tdrv_ctx_t` top-level — pinning the
> `mla[32] → tpb_t[8]` tree and the five GPSIMD/Q7 anchor offsets inside `tpb_t`
> (`+0x9918`, `+0x9940`, `+0x9980`, `+0x9990`, and the `+0x4288` ext-isa union).
> **(2)** It *closes* the STRUCT lane: the device-management opaque quartet
> (`nrtucode_core_t` / `nrtucode_context_t` / `nrtucode_loadable_library_t` /
> `nrtucode_opset_t`) recovered byte-exact from the host ucode build, the
> on-DEVICE `dge_mailbox[4]` + the `.globstruct` claim seam, the last host anon
> sub-structs, and a census-close ledger that tallies what is documented
> field-exact across the three layout pages versus what stays opaque/INFERRED.
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read
> from the shipped ELF: DWARF struct/enum, `objdump`/`nm`/`c++filt` disassembly,
> `.rodata` bytes, or an IDA `*_structures.json` sidecar — all binary-derived and
> citeable), `INFERRED` (an ABI/control-flow rule applied to an observed fact),
> `CARRIED` (consolidated from a committed sibling page and re-grounded here only
> where spot-checked). Callouts use literal `QUIRK` / `GOTCHA` / `NOTE` /
> `CORRECTION`.

> **NOTE — artifacts & provenance.** Two shipped, redistributable ELF64 objects,
> read with stock binutils:
>
> * **`libnrt.so.2.31.24.0`** (the host x86-64 Neuron runtime,
>   `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64`): **122 956 336 bytes**,
>   `BuildID[sha1]=8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, **not stripped**,
>   carries DWARF `.debug_info` (~17 372 functions). For this binary the load
>   addresses **equal** file offsets for `.text` (VMA `0x3dbc0`), `.rodata`
>   (`0x7cf000`), and `.data` (`0xc07e00`) — **the `.data` delta is zero** (do not
>   assume the libtpu `0x400000` or ncore2gp `0x200000` deltas). Every `model_t` /
>   `tpb_t` / `tdrv_ctx_t` / exec-tree figure below is a DWARF
>   `DW_AT_byte_size` / `DW_AT_data_member_location` read, cross-checked against the
>   disassembled structure strides (`imul`/`calloc` immediates).
> * **`libnrtucode_internal.so`** (the GPSIMD custom-op host codec,
>   `aws-neuronx-gpsimd-customop-lib_0.21.2.0`): **10 276 288 bytes**, BuildID
>   `9cbf78c6…e585fd`, **not stripped** (full `.symtab`). Section deltas here are
>   **non-zero** and non-uniform — `.rodata` Δ=0, `.text` Δ=`0x1000`,
>   `.data.rel.ro` Δ=`0x2000`, `.data` Δ=`0x3000` (`readelf -SW`); `objdump`/`nm`
>   are fed VMAs. The opaque quartet (§7) is recovered from its `malloc`/ctor/dtor
>   bytes, **not** lifted from a recovered DWARF type.
>
> The device is a Cadence Tensilica **Vision-Q7 NX "Cairo"** DSP (config
> `ncore2gp`), 8 cores per NeuronCore POOL cluster; the domain is AWS Annapurna
> Trainium ("Cayman" = NC-v3). Embedded `__FILE__`/assert literals
> (`…/KaenaHal-2.31.0.0/…`, `KaenaRuntime/tdrv/dma_ring.c`, `nrtucode_*.c`) are
> compiler-baked into the ELFs and are binary-derived and citeable.

**Cross-references.** This page is the execution-state cap on the runtime lane:
the allocator/context tree it dissects is built in
[Multi-Model / Context Tree + the Host dmem Allocator](../runtime/multimodel-context-dmem.md);
the exec tree it lays out is *driven* in
[Execute-Time GPSIMD Custom-Op Dispatch](../runtime/execute-time-dispatch.md);
the descriptor BDs the exec request carries are built in
[Host↔Device Descriptor Handoff](../runtime/host-device-descriptor-handoff.md);
the quartet is graphed in
[The nrtucode Object Model Graph](../runtime/object-model-graph.md) and detailed
per-object in [nrtucode_core_t](../runtime/nrtucode-core.md),
[nrtucode_context_t](../runtime/nrtucode-context.md),
[nrtucode_ll_create](../runtime/nrtucode-ll-create.md), and
[nrtucode_opset](../runtime/nrtucode-opset.md); the `dge_mailbox` gate is the host
API in [The DGE Host-Private API](../runtime/dge-host-api.md). The lane's
companion layout pages are
[Struct Census Overview](struct-census-overview.md),
[Host-Runtime Struct Layouts](struct-host-runtime-layouts.md), and
[Device-Firmware Global Structs](struct-device-firmware-globals.md) (the
`.globstruct` page); all links resolve to those committed pages.

---

## 0. One-screen orientation — where exec state lives

The host owns exactly one process-global `tdrv_ctx_t` (file-scope pointer
`tdrv_ctx_0`). Inside it, a fixed `mla[32]` array (one device each) embeds
`_tpbs[8]` (one `tpb_t` per on-die NeuronCore). **The `tpb_t` *is* the per-core
context** — and its tail is where the GPSIMD/Q7 anchors live. A loaded NEFF is a
`model_t` in the core's `model_db`. An in-flight inference is an
`exec_request_state` threaded onto the core's single `hw_exec_queue`, scheduled by
a `tpb_xu_schedule` worker.

```
tdrv_ctx_0 ─▶ tdrv_ctx_t                                  (18 884 656 B, one/process)
               ├ +0x8     mla[32] ── mla_t                  (590 008 B each)
               │   └ +80   _tpbs[8] ── tpb_t                 (39 320 B = 0x9998 each)
               │        ├ +0x4280  tpb_allocator            ── the per-core GLOBAL heap
               │        ├ +0x4288  «sunda» ext-isa union (2960 B)  ── the staged GPSIMD ulib (§6)
               │        ├ +0x4E40  model_db (ht*)            ── model_t … model_t  (§3)
               │        ├ +0x96F0  hw_exec_queue   (224 B)   ── exec_request_state[] tree   (§4)
               │        ├ +0x9918  nrtucode_core[5]          ── NX PE/ACT/POOL/DVE/SP host ptrs
               │        ├ +0x9940  pooling_q7_nrtucode_core[8] ── the 8 GPSIMD Q7 cores
               │        ├ +0x9980  pooling_q7_ll             ── staged loadable-library handles
               │        └ +0x9990  nrtucode_context          ── host handle to the device ulib ctx
               └ +18884528  target arch · pod topology
```

The five anchor offsets the SCOPE pins — `+0x4288`, `+0x9918`, `+0x9940`,
`+0x9980`, `+0x9990` — are the seam between the *generic* host runtime and the
**nrtucode** device-management layer (§7's quartet). Each `nrtucode_core_t*` /
`nrtucode_context_t*` slot here points at one of those opaque codec objects.
`[HIGH × OBSERVED]`

---

## Part 1 — Host execution-state structs

## 1. `tdrv_ctx_t` — the process-global root (18 884 656 B)

One `tdrv_ctx_t`, in `tdrv_ctx_0`, gated by three accessors
(`db_tdrv_ctx_get @0x226fb0` reader, `db_tdrv_ctx_init @0x226ca0` set-once,
`db_tdrv_ctx_clear @0x226fa0` teardown). The top-level members material to exec
state (DWARF size **18 884 656 B**): `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0` | 4 | `uint32` | `num_mla` | devices actually present |
| `+8` | 18 880 256 | `mla_t[32]` | `mla` | **32 × 590 008** — the device array (§2) |
| `+18880264` | 4 | `uint32` | `num_ptpb` | physical-TPB count |
| `+18880272` | 4096 | `ptpb_info_t[256]` | `ptpbs` | physical-TPB info table |
| `+18884368` | 128 | `uint32[32]` | `host_did_to_rid_map` | host-device-id → routing-id |
| `+18884504` | 8 | `uint64` | `reservation_id` | pod reservation |
| `+18884512` | 4 | `uint32` | `pod_type` | UltraServer / pod topology |
| `+18884524` | 4 | `neuron_ultraserver_mode` | `pod_mode` | UNSET/X4/X2H/X2V/X1 (enum 0..4) |
| `+18884528` | 4 | `al_hal_tpb_arch_type_t` | `target` | arch gate — see NOTE |

The `mla[32]` count is the **DWARF array bound** (byte-size `18 880 256 =
32 × 590 008`), not prose; `db_physical_core_get_mla_and_tpb @0x2272a0` confirms
the stride with `imul $0x900b8,%rdx` (`0x900b8 = 590 008 = sizeof(mla_t)`).
`[HIGH × OBSERVED]`

> **NOTE — the `target` arch enum is the device-class gate.**
> `al_hal_tpb_arch_type_t` (DWARF enum, size 4): `INVALID=0`, `INVALID_1=1`,
> **`SUNDA=2`**, **`CAYMAN=3`**, **`MARIANA=4`**, `NUM=5`. Every `arch_type == N`
> comparison in the runtime (ulib staging gated on `SUNDA(2)`; EVTACCEL/host-reject
> on `CAYMAN(3)`) resolves through *this* table. `[HIGH × OBSERVED]`

## 2. The `mla_t → tpb_t` tree

`mla_t` (DWARF size **590 008 B**, one per attached device), members material to
exec state: `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0` | 1 | `bool` | `is_used` | asserted live by the core resolver |
| `+80` | 314 560 | `tpb_t[8]` | `_tpbs` | **8 × 39 320** — the per-core contexts (§2.1) |
| `+314640` | 8 | `tpb_map_t` | `tpb_map` | which of the 8 cores are mapped |
| `+314664` | 8 | `ndl_device_t*` | `device` | driver-portal handle (fd at `+0x278`) |
| `+576768` | 1600 | `dma_mem_log_t` | `dmem_logger` | per-device 23-category usage ledger |

The resolver `db_physical_core_get_mla_and_tpb @0x2272a0` turns a
`(device_id, device_tpb_idx)` into `(mla, tpb)` with pure pointer math — `imul
$0x9998,%rsi` pins `sizeof(tpb_t) = 39 320 = 0x9998`. `[HIGH × OBSERVED]`

### 2.1 `tpb_t` — the per-NeuronCore context (39 320 B = 0x9998), the GPSIMD anchors

`tpb_t` holds everything shared by all models on a physical core, including the
exec queue and the GPSIMD/Q7 ucode-core pointers. DWARF size **39 320 B**; the
members this page pins (offsets in dec, hex in the «note» column): `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0` | 8 | `volatile void*` | `tpb_mem_base` | core SBUF/reg window base |
| `+24` | 4 | `int` | `idx` | `device_tpb_idx` (0..7) |
| `+32` | 15816 | `notification_t` | `notification` | NQ rings (exec completion) |
| `+15848` | 1176 | `pool_stdio_block_t` | `pool_stdio_block` | Q7 printf/stderr ring |
| `+17024` | 8 | `dmem_allocator_t*` | `tpb_allocator` | the per-core GLOBAL heap |
| **`+17032`** | **2960** | *anon «sunda» union* | — | **ext-isa ulib cache — anchor `0x4288`** (§6) |
| `+19992` | 40 | `pthread_mutex_t` | `model_db_lock` | guards `model_db` |
| `+20032` | 8 | `ht_t*` | `model_db` | the per-core MODEL DATABASE (§3) |
| `+20040` | 4 | `H_MODEL` | `h_running_model` | currently-executing model id |
| `+22200` | 16440 | `tdrv_scratchpad_t` | `scratchpad` | per-core DRAM scratch |
| **`+38640`** | **224** | `hw_exec_queue_t` | **`hw_exec_queue`** | the per-core HW exec queue (§4) — `0x96F0` |
| `+38864` | 16 | `sync_point_t` | `sync_point` | exec sync point |
| `+38880` | 280 | `ib_addrs_one_eng_t[5]` | `ready_exec_program_ib_addrs` | per-engine IB addrs |
| `+39160` | 16 | `dmem_t*[2]` | `ready_exec_program_switch_bufs` | double-buffer |
| **`+39192`** | **40** | `nrtucode_core_t*[5]` | **`nrtucode_core`** | NX PE/ACT/POOL/DVE/SP host ptrs — anchor `0x9918` |
| **`+39232`** | **64** | `nrtucode_core_t*[8]` | **`pooling_q7_nrtucode_core`** | the **8** GPSIMD Q7 cores — anchor `0x9940` |
| **`+39296`** | 8 | `nrtucode_loadable_library_t**` | **`pooling_q7_ll`** | staged Q7 ll handles — anchor `0x9980` |
| `+39304` | 8 | `size_t` | `num_pooling_q7_ll` | count of staged ll's |
| **`+39312`** | 8 | `nrtucode_context_t*` | **`nrtucode_context`** | host handle to the device ulib ctx — anchor `0x9990` |

**The five SCOPE anchors, byte-converted against the DWARF +
disassembly** (`0x4288 = 17032`, `0x9918 = 39192`, `0x9940 = 39232`,
`0x9980 = 39296`, `0x9990 = 39312`): `[HIGH × OBSERVED]`

| anchor (hex) | dec | field | type | size |
|---|---|---|---|---|
| `+0x4288` | 17032 | «sunda» ext-isa union | anon union | **2960 B** |
| `+0x9918` | 39192 | `nrtucode_core[5]` | `nrtucode_core_t*[5]` | 40 B |
| `+0x9940` | 39232 | `pooling_q7_nrtucode_core[8]` | `nrtucode_core_t*[8]` | 64 B |
| `+0x9980` | 39296 | `pooling_q7_ll` | `nrtucode_loadable_library_t**` | 8 B |
| `+0x9990` | 39312 | `nrtucode_context` | `nrtucode_context_t*` | 8 B |

The `pooling_q7_nrtucode_core[8]` count is the DWARF array bound — the **8** host
pointers to the on-device Q7 GPSIMD cores (`NUM_POOL_CORES = 8`). Each non-NULL
slot is a `nrtucode_core_t*` into the §7 quartet; `nrtucode_context @+0x9990` is
the single `nrtucode_context_t*` all eight cores thread through.
`[HIGH × OBSERVED]` / `[MED × CARRIED]` (the per-engine PE/ACT/POOL/DVE/SP mapping
of the `nrtucode_core[5]` slots).

> **NOTE — the `+0x4288` union is the *last host anon sub-struct* the lane
> documents.** It is DWARF-tagged `sunda` and is **2960 B**; resolved against the
> union base (add 17032 for the absolute `tpb_t` offset): `hal_stpb`
> (`aws_hal_stpb`, 1104 B) @`+0`, `instr_fetch_queue` (`dma_queue_info_t[5]`,
> 1800 B) @`+1104`, `ulib_set_info_extisa_only` (`ucode_lib_set_info_t*`) @`+2904`,
> `ulib_staging_lock` (`pthread_mutex_t`, 40 B) @`+2912`, `set_ulib_reg` (`bool`)
> @`+2952`. The single ext-isa GPSIMD library is staged here **once per core** and
> shared by every model on it (drawn from the GLOBAL `tpb_allocator`, serialised by
> `ulib_staging_lock`). `[HIGH × OBSERVED]`

> **GOTCHA — anchor `+0x9940` is `nrtucode_core_t*[8]`, NOT an inline array of 8
> `nrtucode_core_t`.** The slot is a pointer array (8 × 8 = 64 B); each entry
> points at a separate `malloc(0x70)` codec object in `libnrtucode_internal.so`'s
> heap (§7). A reimplementer who sizes `+0x9940` as `8 × 0x70 = 0x380` will overrun
> the next member (`pooling_q7_ll @+0x9980`, which is only 64 B = `0x40` above
> `+0x9940`, exactly `8 × 8`). The pointer-array reading is the only one consistent
> with the `+0x9980 − +0x9940 = 0x40` gap. `[HIGH × OBSERVED]`

## 3. `model_t` — the NEFF-loaded container (7744 B = 0x1E40)

A loaded NEFF is a `model_t`, `calloc(0x1E40)` in `kbl_model_add @0x3058e0`,
ref-counted, linked into the core's `model_db` via its **embedded** `ht_node`.
DWARF fields material to exec/census: `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0` | 256 | `char[256]` | `name` | NEFF model name |
| `+6384` | 4 | `H_MODEL` | `h_model` | the model handle (`++last_model_handle`) |
| **`+6464`** | 8 | `dmem_allocator_t*` | `dmem_allocator` | the per-model MODEL heap (moved-in at load) |
| `+6472` | 8 | `dmem_list_t*` | `gc_tracker` | weak index of this model's dmem |
| **`+6480`** | 8 | `ucode_lib_set_info_t*` | `ulib_set_info` | staged ucode/ext-isa set — `model+0x1950` |
| **`+6488`** | 8 | `volatile uint64` | `ref_count` | atomic; bumped per in-flight exec |
| `+6528` | 4 | `al_hal_tpb_arch_type_t` | `target` | per-model arch |
| `+6664` | 8 | `ht_t*` | `static_mr_set` | static mem-ref set |
| `+6672` | 8 | `ht_t*` | `io_mr_to_name_map` | io-mr → name map |
| **`+6856`** | **624** | `encd_context` | **`cc_ctx`** | the embedded encoder context (§9) — `curr_priority_class` at `cc_ctx+272` |
| `+7480` | 8 | `tdrv_scratchpad_cleanup_info_t*` | `scratchpad_cleanup_info` | per-model scratchpad claim |
| **`+7496`** | 48 | `ht_node_t` | `ht_node` | the `model_db` link node (embedded) |
| `+7584` | 1 | `bool` | `reset_evt_accel` | EVTACCEL carveout flag |
| `+7585` | 1 | `bool` | `program_fp8_cfg` | fp8 config flag |

The exec path's `container_of` from a `model_db` `ht_node` back to its `model_t`
is byte-exact: `ht_node @ model_t+7496`, `ref_count @ model_t+6488` (a 1008-byte
back-step, `7496 − 1008 = 6488`). `ulib_set_info @+6480` is the `model + 0x1950`
the custom-op predicate dereferences (`ucode_model_has_custom_ops @0x311150`:
`mov 0x1950(%rdi),%rax; cmpl $0x1,0x18(%rax)`). The 624-byte `encd_context` (§9) is
**embedded inline** as `cc_ctx @+6856`, not a separate allocation — so the priority
class an emitted op carries into the §5 DGE mailbox gate lives *inside* the
`model_t`. `[HIGH × OBSERVED]`

> **CORRECTION — `ulib_set_info` is at `model_t+6480` (= `0x1950`), and the
> compared `num_libs` is at `ucode_lib_set_info_t+0x18` (24), not `+8`.** A
> `+8`-reading confuses the 48-byte `ucode_lib_set_info_t` with a *separate*
> 16-byte `kbin_ucode_lib_set` `{libs@0; num_libs@8}`. The instruction
> `cmpl $0x1,0x18(%rax)` dereferences offset 24 of the **48-byte** struct
> (`DW_AT_data_member_location = 24`). `[HIGH × OBSERVED]`

## 4. The kbl exec / HW-queue tree

At execute time the host appends an exec request to the core's single
`hw_exec_queue` (`tpb_t+38640`) and lets a `tpb_xu_schedule` worker drain it. The
through-line is `kmgr_exec @0xdfd50 → kmgr_sync_exec @0xdca70 →
tpb_xu_schedule_exec @0xe8040 → tpb_xu_schedule_request @0xe7540 →
dlr_add_to_hw_exec_queue @0xdd820`, and the per-request descriptor BDs land in the
prings via `hw_exec_queue_add_exec_request_impl @0x320810`.

### 4.1 `hw_exec_queue_t` (224 B = 0xE0, `tpb_t+0x96F0`)

The per-core HW exec queue carries the model's exec-descriptor queue, the two
`sw_dma_queue` cursors the descriptor submit path advances, a compute index
head/tail pair, and the last exec request's config/hashes. DWARF size **224 B**,
10 members, byte-exact: `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0` | 8 | `dma_queue_info_t*` | `model_exec_desc_q` | the exec-descriptor queue (carries the TX/RX prings, §4.1 NOTE) |
| `+8` | 12 | `sw_dma_queue` | `txq` | TX (M2S) cursor `{next_desc_idx, desc_ring_id, desc_count}` |
| `+20` | 12 | `sw_dma_queue` | `rxq` | RX (S2M) cursor (`+20 = 0x14`) |
| `+32` | 40 | `pthread_mutex_t` | `compute_req_lock` | serialises the compute index |
| `+72` | 8 | `volatile uint64` | `compute_idx_head` | producer index into the request ring |
| `+80` | 8 | `volatile uint64` | `compute_idx_tail` | consumer index |
| `+88` | 8 | `const physical_core_t*` | `pcore` | the owning NeuronCore |
| `+96` | 8 | `dmem_t*` | `req_buf` | the device exec-request staging buffer |
| `+104` | 64 | `act_dve_table_hash_t` | `last_exec_req_tbl_hashes` | act/dve table-hash memo of the last request |
| `+168` | 52 | `exec_req_tpb_config_t` | `last_exec_req_flags` | last request's per-engine TPB config (memo) |

> **NOTE — the prings hang off `model_exec_desc_q`, not the queue head.** The
> descriptor submit path (`hw_exec_queue_add_descriptors @0x3206f0`) reads the **TX
> pring** at `*(exec_desc_q)+0x158` and the **RX pring** at `+0x150` — the
> `dma_queue_info_t` reached through `hw_exec_queue+0x00`, *not* offsets on the
> 224-byte queue object itself. The `txq`/`rxq` `sw_dma_queue` cursors (`+8`/`+20`)
> are the host-side ring-position bookkeeping that the doorbell (a tail-inc CSR or
> the `ndl_nc_semaphore_increment` ioctl) later releases.
> `compute_idx_head`/`_tail` (`+72`/`+80`) index the in-flight
> `exec_request_state` ring (§4.2). The `last_exec_req_*` memo lets the runtime
> skip re-emitting an identical per-engine config. `[HIGH × OBSERVED]`

> **CORRECTION — `hw_exec_queue_t` member 0 is `model_exec_desc_q`
> (`dma_queue_info_t*`), not a `tpb*` back-pointer.** The owning-core link is the
> `pcore` member at `+88` (`physical_core_t*`), and the queue reaches its core via
> that, not a leading `tpb*`. A draft that placed a `tpb*` at `+0` and the cursors
> at `+0x08`/`+0x14` had the cursor offsets right (`txq@+8`, `rxq@+20=0x14`) but
> mis-typed `+0` — the DWARF member-0 is the descriptor queue, and the prings the
> handoff path reads (`+0x150`/`+0x158`) are fields of *that* `dma_queue_info_t`.
> `[HIGH × OBSERVED]`

### 4.2 `exec_request_state` (472 B = 0x1D8, anon sub-struct at `+0x392`)

One `exec_request_state` is the host's per-inference record: the request state
machine, the per-LNC `(mla, tpb, model)` pointer pairs (sized `[2]` for the
2-physical-core LNC), the feature-map output set, per-TPB timing/error fan-out, and
a trailing 80-byte anon sub-struct. DWARF size **472 B**, 17 members: `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0` | 4 | `exec_state_t` | `state` | the request state machine |
| `+4` | 8 | `NRT_STATUS[2]` | `exec_rets` | per-core return codes |
| `+16` | 8 | `const virtual_core_t*` | `vcore` | the LNC this exec runs on |
| `+24` | 4 | `uint32` | `tpb_count` | physical cores in the LNC (1..2) |
| `+32` | 8 | `uint64` | `inference_id` | monotonic inference id |
| `+40` | 8 | `const kbl_feature_map_set_t*` | `fmap_output_set` | the bound output tensor set |
| `+48` | 8 | `kbl_output_info_t*` | `out_info` | output info |
| `+64` | 16 | `mla_t*[2]` | `mlas` | per-core device pointers |
| `+80` | 16 | `tpb_t*[2]` | `tpbs` | per-core context pointers |
| `+96` | 16 | `model_t*[2]` | `mods` | per-core resident models |
| `+112` | 14 | `bool[2][7]` | `exec_fatal_status` | per-(core, engine) fatal flags |
| `+128` | 208 | `uint64[2][13]` | `vcore_notif_ts` | per-(core, NQ) notification timestamps |
| `+336` | 16 | `uint64[2]` | `tpb_duration` | per-core exec duration |
| `+352` | 16 | `uint64[2]` | `tpb_cc_duration` | per-core collectives duration |
| `+368` | 18 | `kbl_infer_errors_t[2]` | `infer_err` | per-core infer-error record |
| **`+392`** | **80** | *anon `$A584FD54…`* | — | **the SCOPE's `+0x392` anchor — an 80-B anon sub-struct** |

The request is built in `kmgr_exec_pre @0xdf820` (clone tensors → phys mem; build
`kmgr_exec_resources`), enqueued by the XU scheduler (§4.3), stepped by
`exec_request_progress_one_step @0x263330`, and reaped by
`tpb_xu_get_last_completed @0xe8410 → kmgr_exec_resources_free`. `[HIGH × OBSERVED]`

> **CORRECTION — `+0x392` is the *start of an 80-byte anon sub-struct*, not a
> scalar field.** The byte offset `0x392 = 914` lands exactly on
> `exec_request_state::$A584FD54F05A9924078F239AAB597970` (DWARF size 80), the last
> member, running to the struct end at `472 = 0x1D8`. A reading that treats
> `+0x392` as a lone field is wrong: it is the base of the trailing anon block (the
> request's private completion/timing scratch). `[HIGH × OBSERVED]`

> **GOTCHA — exec is serialised per core, but multiple host threads may hold the
> same model.** `h_running_model` (`tpb_t+20040`) is the single "active" handle;
> the runtime serialises *exec* on a core, so only one `exec_request_state` runs at
> a time per core even though many `model_t`s are resident. Concurrency between
> resident models is the per-model `ref_count` (`model_t+6488`), not exec
> overlap. `[HIGH × OBSERVED]` / `[MED × INFERRED]`

### 4.3 `tpb_xu_schedule` — the per-core XU scheduler (a function family, no named struct)

`tpb_xu_schedule_*` is the execution-unit worker that owns the `hw_exec_queue`
drain: `tpb_xu_schedule_exec @0xe8040 → tpb_xu_schedule_request @0xe7540` pulls the
next `exec_request_state`, `dlr_add_to_hw_exec_queue @0xdd820` appends it, the
kickoff doorbell fires (`exec_kickoff_infer @0x2632e0 → ndl_nc_semaphore_increment
@0xc3ba0`, an `ioctl(req=0x80084e29)` on `mla->device`'s fd at `+0x278`), the
worker blocks on the pooled completion eventfd
(`tpb_xu_sync_exec_get_pooled_comp_efd @0xe87e0 + read()`), and
`exec_request_progress_one_step` drains the INFER_STATUS NQ (POOL = NQ id 6) and
the Q7 stdio ring. `[HIGH × OBSERVED]`

> **CORRECTION — `tpb_xu_schedule` is a *function family*, not a DWARF struct.** A
> sweep of `libnrt`'s structure DB finds **no** `tpb_xu_schedule` / `_t` /
> `_request` named type (no `xu_schedule` substring anywhere). The XU scheduler's
> per-request state IS the `exec_request_state` (§4.2) and the `hw_exec_queue`
> (§4.1) it threads; the "schedule" name lives only on the `tpb_xu_schedule_*`
> entry points. So §4.3 documents the scheduling *path*, and the structures it
> mutates are §4.1/§4.2 — there is no separate scheduler struct to lay out.
> `[HIGH × OBSERVED]`

## 5. `dge_mailbox` — the DMA-priority gate (4-byte entry × 4)

The DGE (Descriptor Generation Engine) DMA-priority gate is a **4-byte packed
struct** (`nrtucode.h`, `sizeof == 4`), an array of **4** living in the per-Q7-core
**device-DRAM** control block at `dram_base + 0x28` (host-relative this is *not* a
host struct — see §8). The packed layout: `[HIGH × OBSERVED]`

```c
typedef struct nrtucode_dge_mailbox {   /* sizeof == 4 */
    uint8_t  reserved;   /* byte 0  — must be 0                                 */
    uint8_t  priority;   /* byte 1  — [0..255]; HIGHER value = LOWER priority; default 0xFF */
    uint16_t bitmask;    /* bytes 2-3 — 16-bit DMA-enable mask (LE); default 0xFFFF */
} nrtucode_dge_mailbox_t;
```

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0` | 1 | `uint8` | `reserved` | must be 0 |
| `+1` | 1 | `uint8` | `priority` | priority tier; `0xFF` = lowest (never masks) |
| `+2` | 2 | `uint16` | `bitmask` | which of 16 DMA channels this tier may use |

**The gate rule** (`nrtucode.h` 504-505): the device starts with all DMAs enabled
(`0xFFFF`); per mailbox, *"if the DGE operation's priority > the mailbox priority,
AND (`&`) the mailbox's DMA bitmask"*. A default mailbox `{0, 0xFF, 0xFFFF}` masks
nothing. The host programs it through the §8 accessors: `get_dge_mailbox_addr`
returns the array **base** `dram_base+0x28` (and resets trailing slots to the
`0xffffff00` sentinel = `{0, 0xFF, 0xFFFF}`); `private_set_dge_mailbox` writes one
4-byte entry at `dram_base+0x28+idx*4` (`idx ≤ 3`). `[HIGH × OBSERVED]`

> **CORRECTION — the gate admits the five `NX_POOL` kinds ONLY.** Every mailbox
> accessor gates `core->coretype` against the 64-bit mask `0x102020204` (bits
> `{2, 9, 17, 25, 32}` = `SUNDA/CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK` NX_POOL), via
> `movabs $0x102020204,%rsi; bt %rax,%rsi`. It does **not** admit the Q7_POOL
> kinds `{6,13,21,29,37}` (that mask is `0x2020202040`, used by the opcode→library
> mapper, not the mailbox). The DGE DMA-priority mailbox is an **NX-pooling-engine
> facility**. (v5/Maverick bit 32 compiles in only in the internal build; the
> customop build caps at `cmp eax,0x19` → `{2,9,17,25}`.) `[HIGH × OBSERVED]`

---

## Part 2 — The census close

## 6. The ext-isa staging anchor (recap of `tpb_t+0x4288`)

The `+0x4288` «sunda» union (§2.1 NOTE) is the host-side cache the GPSIMD ulib is
staged into once per core. Its `ulib_set_info_extisa_only` slot
(`ucode_lib_set_info_t*` @ union `+2904`) points at the 48-byte
`ucode_lib_set_info_t` `{scratch_space, ucode_table, extram, num_libs@+24,
libs, lib_dmem}`; the `ulib_staging_lock` @ union `+2912` serialises
`ucode_stage_libs @0x310ea0`. This is the host mirror of the device-side ext-isa
library table the §7 `nrtucode_loadable_library_t` stages. `[HIGH × OBSERVED]`

## 7. The device-management opaque quartet (byte-exact from the host ucode build)

The four `nrtucode_*` objects are the device-management codec the host's
`pooling_q7_nrtucode_core[8]` / `pooling_q7_ll` / `nrtucode_context` anchors point
into. They are **plain C `malloc` blocks** — no object carries a C++ vtable —
recovered byte-exact from the `mov $size,%edi ; call malloc@plt` ctor prologue and
the field stores, **not** from a recovered DWARF type.

> **CORRECTION — the quartet is OPAQUE in BOTH binaries' DWARF; sizes come from the
> ctor `malloc` bytes.** Two independent grounds: (a) in **`libnrt.so`**'s DWARF
> the four `nrtucode_*` types are **forward-declared** — every `nrtucode_core` /
> `nrtucode_context` DIE carries `DW_AT_declaration: 1` (incomplete), and the host
> holds them only as opaque handles (`nrtucode_core_t*`; the
> `nrtucode_loadable_library` typedef is a 16-byte handle wrapper, still
> `DW_AT_declaration`). libnrt sees **no member layout**. (b) In
> **`libnrtucode_internal.so`** the `*_structures.json` contains **only the five
> ELF loader types** (`Elf64_Sym`, `Elf64_Rela`, `Elf64_Dyn`, `Elf64_Verneed`,
> `Elf64_Vernaux`; `jq '.[].name'` → those five, nothing
> else) — and that binary ships **no DWARF**, so IDA never reconstructed the
> device-struct fields either. Therefore every quartet size below is a
> `mov $size,%edi ; call malloc@plt` ctor-immediate read (HIGH × OBSERVED from the
> binary), **carried** here from the four committed `nrtucode-*` runtime pages, not
> a DWARF lift. `nm libnrtucode_internal.so | rg -c '_ZTV'` = **0**: zero C++
> vtables, so the two platform tables a `nrtucode_context_t` holds are flat C
> fn-ptr arrays (slot N = `symbol + 8*N`; the `_ZTV…+0x10` rule does **not**
> apply). `[HIGH × OBSERVED]`

| object (public-header name) | binary tag | `malloc` size | ctor | dtor | leak-tracked? |
|---|---|---|---|---|---|
| `nrtucode_context_t` | `nrtucode_context_t@%p` | **`0x28` (40 B)** | `0x9b0290` | `0x9b03d0` | yes |
| `nrtucode_core_t` | `nrtucode_core_t@%p` | **`0x70` (112 B)** | `0x9b0640` | `0x9b0780` | yes |
| `nrtucode_loadable_library_t` | `nrtucode_ll_t@%p` | **`0x48` (72 B)** | `0x9b1a90` | `0x9b1da0` | yes |
| `nrtucode_opset_t` | `nrtucode_opset_t@%p` | **`0x830` (2096 B)** | `0x9b24c0` | `0x9b25c0` | **no** |

### 7.1 `nrtucode_context_t` (0x28 / 40 B) — the codec root

The handle every other nrtucode call threads through; five qwords, every byte
accounted: `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0x00` | 8 | `const rw_table*` | `rw_impl` | ctor arg2; 5-slot reg/DRAM r/w + log table (never NULL) |
| `+0x08` | 8 | `const memhandle_table*` | `memhandle_impl` | `&plat_memhandle_dummy` default; 5-slot device-mem table |
| `+0x10` | 8 | `void*` | `userdata` | opaque embedder cookie (init 0) |
| `+0x18` | 8 | `uint64` | `log_scratch_size` | `vsnprintf` scratch capacity (init `0x200`) |
| `+0x20` | 8 | `char*` | `log_scratch_buf` | `malloc(0x200)`; freed FIRST in destroy |

`nrtucode_get_api_level @0x9b0260` returns **3** — the only `api_level` `create`
accepts. The two platform tables are 5-slot C fn-ptr arrays
(`rw_impl`: read `+0`, write `+8`, log_emit `+0x10`, log_enabled `+0x18`, aux
`+0x20`; `memhandle_impl`: device_malloc `+0`, device_free `+8`, read `+0x10`,
write `+0x18`, device_addr `+0x20`). `[HIGH × OBSERVED]`

### 7.2 `nrtucode_core_t` (0x70 / 112 B) — the booted-engine handle

Each `pooling_q7_nrtucode_core[i]` slot points at one of these. Only the host
fields live in the 0x70 block; the live device state (boot magic, log CB, mailbox,
pc-bounds) lives in a device-DRAM control block at `core+0x20` (`dram_base`),
reached via the context's `rw_impl`: `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0x00` | 8 | `nrtucode_context_t*` | `context` | back-ptr (the only stored inter-object edge) |
| `+0x08` | 8 | `void*` | `userdata` | per-core cookie (init 0) |
| `+0x10` | 4 | `uint32` | `coretype` | `NRTUCODE_CORE_*`; gated against NX_POOL mask `0x102020204` |
| `+0x18` | 8 | `uint64` | `iram_base` | ctor arg3 (device IRAM SOC window; write-only here) |
| `+0x20` | 8 | `uint64` | `dram_base` | ctor arg4 — device control-block base |
| `+0x28` | 8 | `uint64` | `apb_base` | ctor arg5 (APB/CSR window; write-only here) |
| `+0x30` | 4 | `uint32` | `boot_state` | 0=NOT_BOOTED, 1=BOOTED_LEGACY (every device method gates on `==1`) |
| `+0x38` | 8 | `void*` | `log_memhandle` | device log-ring handle (0 when logs off) |
| `+0x40` | 4 | `uint32` | `log_buf_size` | log buffer size |
| `+0x44` | 4 | `uint32` | `log_read_cursor` | bytes consumed by `print_logs` |
| `+0x48` | 0x21 | `char[0x21]` | `friendly_name` | `"nrtucode_core_t@%p"`; NUL forced @`+0x68` |

### 7.3 `nrtucode_loadable_library_t` (0x48 / 72 B) — the staged ext-isa library

The `pooling_q7_ll` anchor (`tpb_t+0x9980`) is a `nrtucode_loadable_library_t**`;
each is one prelinked GPSIMD ext-ISA kernel image + its on-device staging buffer.
The binary tag is `nrtucode_ll_t`; the public header calls it
`nrtucode_loadable_library_t`. `[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0x00` | 8 | `nrtucode_context_t*` | `context` | owning ctx (arg1) |
| `+0x08` | 8 | `void*` | `device_memhandle` | 16 MiB staged-image handle (valid iff `prelinked_size != 0`) |
| `+0x10` | 8 | `uint64` | `library_selector` | env/CPTC/caller-resolved selector (CPTC → index 3) |
| `+0x18` | 8 | `uint64` | `prelinked_size` | total staged device bytes (0 on the coretype-6 path; ≤ `0x10000` cap) |
| `+0x20` | 0x21 | `char[0x21]` | `friendly_name` | `"nrtucode_ll_t@%p"`; NUL @`+0x40` |

The staged image is a `"UCPL "`-magic (`0x204C504355`) 32-byte header + two
relocated body segments, written through the memhandle `write` slot; the device
buffer is hard-capped at **64 KiB** (`cmpq $0x10001,0x18; jb`). `[HIGH × OBSERVED]`

### 7.4 `nrtucode_opset_t` (0x830 / 2096 B) — the opcode/spec presence set

The host's record of *which* `(opcode, spec)` pairs a loaded model uses. A single
`malloc(0x830)`; **not** objcount-tracked (the only quartet member that is not):
`[HIGH × OBSERVED]`

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0x000` | 8 | `nrtucode_context_t*` | `context` | owning ctx (arg1) |
| `+0x008` | 0x800 | `uint8_t*[256]` | `opcode_slot[256]` | one ptr per opcode; NULL=absent, else `calloc(1,0x100)` spec bitmap |
| `+0x808` | 0x28 | `char[0x28]` | `friendly_name` | `"nrtucode_opset_t@%p"`; NUL forced @`+0x828` |

`0x830 = 8 + 256·8 + 0x28`. Only opcode `0xF0` (`EXTENDED_INST`) ever populates a
bitmap byte (spec read from `instr[0x0c]`); for every other opcode the non-NULL
slot pointer alone signals presence. `[HIGH × OBSERVED]`

> **CORRECTION — `nrtucode_opset_t.friendly_name` is `char[0x28]` (40 B), not
> `char[0x21]` + a `0x07` pad.** `set_friendly_name` writes the forced terminating
> NUL at **`+0x828`** (`movb $0x0,0x828(%r14)` @`0x9b294e`), only reachable if the
> field runs to the struct end. The `0x21` is the default-name `snprintf` cap, not
> the field size. (Supersedes the older `object-model-graph` row; the
> `nrtucode-opset` page already carries the fix.) `[HIGH × OBSERVED]`

### 7.5 Quartet size reconciliation

The four sizes are consistent across every page that cites them — the
object-model graph, the four per-object pages, and this census. The two historical
divergences are both resolved in-place: (a) the `opset` `friendly_name` width
(§7.4 CORRECTION), and (b) the `ll` name (`nrtucode_ll_t` binary tag vs
`nrtucode_loadable_library_t` public header — same object, `0x48`). No size
mismatch remains. `[HIGH × OBSERVED]`

## 8. The on-DEVICE `dge_mailbox[4]` + the `.globstruct` claim seam

The §5 host `nrtucode_dge_mailbox_t` is staged into the per-Q7-core **device-DRAM
control block** at `dram_base + 0x28` — `dram_base = nrtucode_core_t+0x20`. That
control block is the device-side projection of a `nrtucode_core_t`, reached only
through the `rw_impl` read/write slots, **never** a host struct field: `[HIGH × OBSERVED]`

| `dram_base+` | size | field | reached by |
|---|---|---|---|
| `+0x00` | 4 | **claim / ready magic** | `on_ucode_booted` (read) / `destroy` (release) — the `.globstruct` seam |
| `+0x04` | 4 | log buffer size (device) | `enable_logs` |
| `+0x08` | 8 | log buffer device addr | `enable_logs` |
| `+0x10` | 4 | log write cursor | `print_logs` |
| `+0x18` | ≤20 | `priority_class_map[≤4]` (u32 each) | `dge_set/get_priority_class_map` |
| **`+0x28`** | 16 | **`dge_mailbox[0..3]`** (4 × 4 B) | `get_dge_mailbox_addr` returns this base; `private_set/get_dge_mailbox` index it |
| `+0x38` | 8 | `pc_bounds_soc_addr_lo` | `enable/disable/get_pc_bounds_check` |
| `+0x40` | 8 | `pc_bounds_soc_addr_hi` | `enable/disable/get_pc_bounds_check` |

**The `.globstruct` claim seam.** The boot/claim handshake
(`nrtucode_core_on_ucode_booted @0x9b0ab0`) is a single-owner lock on the device
DRAM sentinel at `dram_base+0x00` — **the same word as the device firmware
`.globstruct[0]`** (cross-link the device-globals page, [Device-Firmware Global
Structs](struct-device-firmware-globals.md)): `[HIGH × OBSERVED]`

| state | magic (LE bytes) | transition |
|---|---|---|
| READY / unclaimed (`.globstruct[0]` ready word) | `0x6099CB34` (`34 cb 99 60`) | a freshly-booted image advertises this |
| CLAIMED | `0x502B2DA1` | `on_ucode_booted` test-then-writes it; sets `boot_state=1` |
| release (on destroy) | `0x6099CB34` | `core_destroy` restores the unclaimed sentinel |

The claim is a **read-then-write, not a CAS** — two distinct `rw_impl`
transactions; mutual exclusion relies on the embedder's `rw_impl` backend
serialising device accesses. `[HIGH × OBSERVED]`

> **NOTE — `dram_base+0x28` (device) ≠ `nrtucode_core_t+0x28` (host).** The host
> field at `core+0x28` is `apb_base` (the APB/CSR window, write-only); the **device**
> mailbox array is at `dram_base(=core+0x20)+0x28`. Conflating the two — treating
> the mailbox as host offset `+0x28` — is the single biggest address-space trap in
> the quartet. `[HIGH × OBSERVED]`

## 9. The last host anon sub-structs (sizes confirmed)

The SCOPE's "last host anon sub-structs" beyond the named tree above, each
size-confirmed against the host DWARF/disasm: `[HIGH × OBSERVED unless noted]`

| sub-struct | size | location | confirm |
|---|---|---|---|
| `tpb_t` «sunda» ext-isa union | **2960 B** | `tpb_t+0x4288` (17032) | DWARF union byte_size (`tpb::$06E30118…`); staged once per core (§6) |
| `exec_request_state::$A584FD54…` anon | **80 B** | `exec_request_state+0x392` (914) | DWARF anon-member byte_size; the SCOPE's `+0x392` anchor (§4.2) |
| `kmgr_exec_resources` | **56 B (0x38)** | per-exec, built `kmgr_exec_pre @0xdf820`, freed `kmgr_exec_resources_free` | `{trace_exec_id, trace_nc_idx, model_id, tdrv_resources*, exec_mode, +0x28 anon[16]}` |
| `encd_context` | **624 B (0x270)** | embedded as `model_t.cc_ctx @+6856` | `curr_priority_class @+272`; knobs `encd_set/get_ctx_curr_priority_class @0x238b00/@0x238b90` |

**`kmgr_exec_resources` (56 B, 6 members):** `trace_exec_id(u64)@+0`,
`trace_nc_idx(u32)@+8`, `model_id(u64)@+16`,
`tdrv_resources(tdrv_compute_resources_t*)@+24`, `exec_mode(kmgr_exec_mode_t)@+32`,
and a 16-byte anon `$D4E97756…` @`+40`. It is the transient per-`nrt_execute`
handle the XU worker reaps via `tpb_xu_get_last_completed`. `[HIGH × OBSERVED]`

**`encd_context` (624 B) is NOT a free-standing per-model alloc — it is *embedded*
in `model_t` at `cc_ctx @+6856`.** Its `curr_priority_class` (`uint8` at
`encd_context+272`) decides which ISA priority an *emitted* op carries into the
device DGE mailbox gate (§5/§8); the host knobs are
`encd_set/get_ctx_curr_priority_class @0x238b00/@0x238b90`. Other members of note:
`model_allocator(dmem_allocator_t*)@+8`, `config(encd_config, 112 B)@+160`,
`sbuf_evt_accel_valid@+362`, `vring_rewrite_mutex(pthread_mutex_t)@+584`.
`[HIGH × OBSERVED]`

---

## 10. Census-close ledger (the STRUCT lane, #984–#987)

This page closes the four-page STRUCT lane. The lane partitions every recovered
structure across **three layout pages** plus this census close:

| lane page | scope | status |
|---|---|---|
| [Struct Census Overview](struct-census-overview.md) (#984) | the lane index + the host/device split + the confidence taxonomy | committed |
| [Host-Runtime Struct Layouts](struct-host-runtime-layouts.md) (#985) | the generic host allocator/context/tensor structs (`dmem_*`, `model_db` `ht_*`, `nrt_tensor*`, `virtual_core_t`, the 23-category ledger) | committed |
| [Device-Firmware Global Structs](struct-device-firmware-globals.md) (#986) | the on-device `.globstruct` + firmware globals (the ready/claim sentinel word, device control blocks) | committed |
| **Host Execution-State Structs + Census Close** (#987, this page) | the exec-state tree + the quartet + the census tally | **this page** |

### What this page documents field-exact

**Host exec-state tree (DWARF-grounded, `libnrt.so`):** `tdrv_ctx_t` (top-level +
the `mla[32]` bound), `mla_t` (the exec-material members), `tpb_t` (full 24-member
list incl. all five GPSIMD/Q7 anchors), `model_t` (the exec/census members + the
embedded `cc_ctx`), `hw_exec_queue_t` (all 10 members, 224 B), `exec_request_state`
(all 17 members, 472 B), `kmgr_exec_resources` (all 6 members, 56 B), the
`tpb_t+0x4288` «sunda» union (2960 B), and `encd_context` (624 B, embedded as
`model_t.cc_ctx`). **9 host structures, every cited offset DWARF-exact.**

**Device-management quartet (`libnrtucode_internal.so`, ctor-recovered):**
`nrtucode_context_t` (0x28), `nrtucode_core_t` (0x70),
`nrtucode_loadable_library_t` (0x48), `nrtucode_opset_t` (0x830) — **4 objects,
every byte accounted from the ctor**, plus the `nrtucode_dge_mailbox_t` 4-byte gate
entry and the device-DRAM control block (`dram_base+{0x00,0x18,0x28,0x38,0x40}`).

### What remains opaque / INFERRED

| item | status | why |
|---|---|---|
| the quartet's full field layout | absent (opaque) | OPAQUE in both binaries — `libnrt` DWARF forward-declares them (`DW_AT_declaration`, 8-B handle / 16-B `loadable_library` wrapper); `libnrtucode_internal.so` has no DWARF. Sizes are ctor-`malloc` reads, interiors from ctor field stores (carried) — never a DWARF lift |
| `tpb_xu_schedule` as a struct | absent | not a DWARF type — a function family; the state it threads is `exec_request_state` + `hw_exec_queue` |
| `exec_request_state::$A584FD54` / `kmgr_exec_resources::$D4E97756` anon interiors | MED × INFERRED | the anon sub-structs' *byte_size* is OBSERVED (80 B / 16 B); their internal members are not separately named in the DWARF |
| per-engine mapping of `nrtucode_core[5]` | MED × INFERRED | the 5-slot array is OBSERVED; the PE/ACT/POOL/DVE/SP slot assignment is reasoned |
| v5 / Maverick interiors | LOW × INFERRED | no `maverick` source dir in `libnrt`; the `MAVERICK_NX_POOL` (bit 32) gate compiles in only in the internal customop build |

### Confidence split

- **HIGH × OBSERVED** — every host struct size + member offset cited from the
  `libnrt` DWARF: `tdrv_ctx_t` (18 884 656 B, `mla[32]@+8`), `mla_t`, `tpb_t`
  (39 320 B incl. all 5 anchors `+0x4288`/`+0x9918`/`+0x9940`/`+0x9980`/`+0x9990`),
  `model_t` (7744 B, `cc_ctx@+6856`, `ht_node@+7496`), `hw_exec_queue_t` (224 B, 10
  members), `exec_request_state` (472 B, 17 members, `+0x392` = 80-B anon),
  `kmgr_exec_resources` (56 B), the «sunda» union (2960 B), `encd_context` (624 B);
  the `tpb_t` 0x9998 / `mla_t` 0x900b8 strides from the resolver `imul`s; all four
  quartet `malloc` sizes (0x28/0x70/0x48/0x830) from the ctor prologues; the
  `nrtucode_dge_mailbox_t` 4-byte layout + the `0x102020204` NX_POOL gate; the
  `dram_base+0x28` device mailbox base; the `.globstruct` claim magics
  `0x6099CB34`/`0x502B2DA1`; the quartet's opacity in both binaries (`libnrt`
  `DW_AT_declaration`; `libnrtucode_internal` `jq` → 5 ELF loader types) and its
  zero `_ZTV` count.
- **MED × INFERRED** — the two exec anon sub-structs' interiors; the per-engine
  mapping of `nrtucode_core[5]`; the cross-process co-location arbitration (below
  libnrt's visibility).
- **LOW × INFERRED / CARRIED** — the v5/Maverick struct interiors; the quartet
  sizes are themselves `[HIGH × OBSERVED]` from the binary but **CARRIED** into
  this page from the four committed `nrtucode-*` runtime pages.

### Lane tally

**13 structures documented field-exact across the lane's exec-state cap (9 host +
4 codec quartet)**, plus 3 device-side blocks (the `nrtucode_dge_mailbox_t` entry,
the device-DRAM control block, and the `.globstruct` claim word) cross-linked to
the device-globals page. The lane's generic host structs (`dmem_*`, `ht_*`,
`nrt_tensor_storage_t`, the usage ledger) are owned by the Host-Runtime Layouts
page (#985); this page restates none of them, only the **execution-state** subset
and the device-management quartet. **STRUCT lane (#984–#987): exec-state cap
delivered.** `[HIGH × OBSERVED]`
