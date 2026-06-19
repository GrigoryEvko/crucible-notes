# Multi-Model / Context Tree + the Host dmem Allocator

> **Scope — how libnrt owns device memory and lets many models share a core.**
> This page reconstructs the host-side memory-and-multiplexing core of
> `libnrt.so.2.31.24.0`: the one process-global context tree
> (`tdrv_ctx` → `mla[32]` → `tpb[8]`), the per-NeuronCore **model database**
> (a hash table keyed by `H_MODEL`), the **two-tier dmem allocator** (a per-core
> GLOBAL heap and one per-model MODEL heap) over the host-granted HBM/DRAM
> carveout, the **23 usage categories** and `TONGA_DRAM`/`HOST_DRAM` regions, the
> **LNC (Logical NeuronCore)** fractional-sharing model, the ext-isa `ulib`
> shared once per core under `ulib_staging_lock`, and the
> `nrt_tensor` / `nrt_allocate_tensor_set` IO-buffer path. Everything below is
> read from the shipped, DWARF-bearing host ELF — its structs, enums,
> disassembly, and `.rodata` bytes.
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read this
> session from the ELF: DWARF struct/enum, `objdump` disassembly, `.rodata`
> bytes, `nm`), `INFERRED` (an ABI/control-flow rule applied to an observed
> fact), `CARRIED` (taken from a backing static-analysis pass, re-grounded here).

> **NOTE — artifact & tooling.** All facts derive **solely from static
> analysis** of `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64`, file
> `/opt/aws/neuron/lib/libnrt.so.2.31.24.0`: **ELF64 x86-64**,
> **122 956 336 bytes**, **`BuildID[sha1]=8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`**,
> SONAME `libnrt.so.1`, *not stripped*, **with DWARF `.debug_info`**,
> **17 372 functions**. Section layout (`readelf -SW`): `.text` VMA `0x3dbc0`
> (== fileoff), `.rodata` VMA `0x7cf000` (== fileoff), `.data` VMA `0xc07e00`
> (== fileoff). **There is no `.data` delta for this binary** — every
> `.rodata`/`.data` byte read below is offset-clean (do *not* assume the libtpu
> `0x400000` or ncore2gp `0x200000` deltas). Anti-hang: addresses come from the
> IDA `*_functions.json` sidecar and `nm` (always piped); every disassembly is
> address-bounded. Embedded `__FILE__`/assert literals
> (`/opt/brazil-pkg-cache/…/KaenaHal-2.31.0.0/…`) are compiler-baked into the
> ELF and are **binary-derived and citeable**, not from any external tree.

Cross-references: the execution-state struct census in
[Host Execution-State Structs](../appendix/struct-exec-state-census.md) (the
`exec_request_state` / tensor-storage layouts this page's allocator feeds),
[the libnrt runtime synthesis](runtime-synthesis.md) (the surrounding spine),
[host concurrency primitives](concurrency-primitives.md) (the locks named here),
and [version + ext-isa getters](version-extisa-getters.md) (the `ulib` getters
behind §6). Those targets may be thin stubs; the links resolve.

---

## 0. One-screen orientation

The host runtime owns exactly **one** process-global object: a `tdrv_ctx_t`,
held in the file-scope pointer `tdrv_ctx_0`. Inside it is a fixed `mla[32]`
array — one `mla_t` (*Machine-Learning Accelerator*, i.e. a physically-attached
Neuron device) per slot — and each `mla_t` embeds `_tpbs[8]`, one `tpb_t` per
on-die NeuronCore (*TPB* = Tensor-Processing Block). **The `tpb_t` *is* the
per-NeuronCore context**: it carries (a) a per-core GLOBAL device-memory
allocator (`tpb_allocator`), (b) the per-core **model database** — a hash table
`model_db` keyed by `H_MODEL`, guarded by `model_db_lock` — and (c) the GPSIMD
Q7/NX ucode-core pointers plus the ext-isa `ulib` staging cache.

```
tdrv_ctx_0 ──▶ tdrv_ctx_t                              (18 884 656 B, one per process)
                ├ num_mla
                ├ mla[32]            ── mla_t           (590 008 B each)
                │   ├ _tpbs[8]       ── tpb_t           (39 320 B each = the per-core context)
                │   │   ├ tpb_allocator  ── dmem_allocator_t  TYPE_GLOBAL  (per-core heap)
                │   │   ├ model_db        ── ht_t*  (keyed by H_MODEL.id, under model_db_lock)
                │   │   │     └ model_t … model_t       (every resident model; each ref-counted)
                │   │   │           └ dmem_allocator (TYPE_MODEL, the model's own heap)
                │   │   ├ sunda.ulib_set_info_extisa_only  (the shared ext-isa, staged once)
                │   │   └ pooling_q7_nrtucode_core[8]      (the GPSIMD Q7 cores)
                │   └ dmem_logger    ── dma_mem_log_t  (1600 B; the 23-category usage ledger)
                └ host_did_to_rid_map[32], pod topology, arch `target`
```

Multiple loaded models share a core purely by **co-residence** in that core's
`model_db`; each `model_t` is independently ref-counted and carries its **own**
per-model dmem allocator, so unloading one model mass-frees *exactly* its device
memory without touching the others. Processes do **not** see physical cores —
they see **Logical NeuronCores** (LNC; a.k.a. virtual core / `vtpb` / `vnc`), a
`virtual_core_t` grouping 1–2 physical cores; `NEURON_RT_VIRTUAL_CORE_SIZE`
(default 2) sets the grouping. The LNC layer is the runtime-side
fractional/co-location primitive: the k8s 70/30 generate/upscale split lands as
**separate processes**, each with its own `tdrv_ctx_0`, its own `owned_vcores`
slice, and its own per-core `model_db`.

Device memory is one flat heap per `(NeuronCore, HBM channel)`:
`dmem_alloc_internal` calls the driver portal `ndl_memory_alloc` against the
host-granted HBM (`TONGA_DRAM`, loc=2) or host pinned DRAM (`HOST_DRAM`, loc=1),
records `(pa, va, size)` in a 192-byte `dmem_t`, links it into the owning
allocator's intrusive list, and books the byte cost under one of **23**
`dma_mem_usage_type` categories tracked in the `mla`'s `dmem_logger`.
`nrt_tensor_allocate` / `nrt_allocate_tensor_set` are the public IO-buffer faces
of that heap.

---

## 1. The process-global context tree: `tdrv_ctx → mla[32] → tpb[8]`

There is exactly one `tdrv_ctx_t`, stored in the file-scope pointer
`tdrv_ctx_0`. Three accessors gate it: `[HIGH × OBSERVED]`

| accessor | addr | role |
|---|---|---|
| `db_tdrv_ctx_get` | `0x226fb0` | the only reader — returns `tdrv_ctx_0` |
| `db_tdrv_ctx_init` | `0x226ca0` | set-once (asserts "tdrv_ctx already set") |
| `db_tdrv_ctx_clear` | `0x226fa0` | teardown — `tdrv_ctx_0 = 0` |

`tdrv_ctx_t` (DWARF size **18 884 656 B**): `[HIGH × OBSERVED]`

| off | type | field | note |
|---|---|---|---|
| `+0` | `uint32` | `num_mla` | devices actually present |
| `+8` | `mla_t[32]` | `mla` | **32 × 590 008 = 18 880 256 B** |
| `+18880264` | `uint32` | `num_ptpb` | |
| `+18880272` | `ptpb_info_t[256]` | `ptpbs` | physical-TPB info table (4096 B) |
| `+18884368` | `uint32[32]` | `host_did_to_rid_map` | host-device-id → routing-id |
| `+18884496` | `uint32` | `host_did_to_rid_map_size` | |
| `+18884504` | `uint64` | `reservation_id` | |
| `+18884512` | `uint32` | `pod_type` | UltraServer / pod topology |
| `+18884516` | `uint32` | `pod_sz` | |
| `+18884520` | `uint32` | `pod_node_id` | |
| `+18884524` | `neuron_ultraserver_mode` | `pod_mode` | UNSET/X4/X2H/X2V/X1 (enum 0..4) |
| `+18884528` | `al_hal_tpb_arch_type_t` | `target` | arch gate (see §1.1) |
| `+18884532…` | `bool`/`int`/`size_t` | `dbg_*` block | CC / mesh / ring tunables (cut) |

The `mla[32]` count is read straight from the DWARF array bound
(`mla_t[32]` @ `+8`, byte-size `18 880 256` = `32 × 590 008`), not from prose —
**32 is the hard fan-out of physically-attachable devices per process**.

> **NOTE — the `target` arch enum is the single most consequential numeric gate
> on this page.** `al_hal_tpb_arch_type_t` (DWARF enum, size 4):
> `INVALID=0`, `INVALID_1=1`, **`SUNDA=2`**, **`CAYMAN=3`**, **`MARIANA=4`**,
> `NUM=5`. Every `arch_type == N` comparison below resolves through *this*
> table; getting the name wrong inverts the device class. `[HIGH × OBSERVED]`

### 1.1 The physical-core resolver — every alloc/model/tensor path begins here

`db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb)` @ `0x2272a0` turns a
`(device_id, device_tpb_idx)` coordinate into the two tree pointers, using pure
pointer arithmetic into the static tree — **no locking**. `[HIGH × OBSERVED]`
Disassembly pins the two structure strides:

```asm
2272c0:  imul $0x900b8, %rdx, %rdx     ; 0x900b8 == 590008 == sizeof(mla_t)  → mla[device_id]
2272c7:  imul $0x9998,  %rsi, %rcx     ; 0x9998  == 39320  == sizeof(tpb_t)  → _tpbs[device_tpb_idx]
2272ce:  lea  0x8(%rax,%rdx,1), %rdi   ; mla = &ctx->mla[device_id]   (+8 == tdrv_ctx.mla offset)
2272d3:  lea  0x58(%rdx,%rcx,1), %rcx  ; tpb = &mla->_tpbs[device_tpb_idx] (0x58 = +8 ctx hop + +80 _tpbs)
```

```c
// db_physical_core_get_mla_and_tpb @0x2272a0  [HIGH × OBSERVED]
static int db_physical_core_get_mla_and_tpb(const physical_core_t *pc,
                                            mla_t **out_mla, tpb_t **out_tpb) {
    tdrv_ctx_t *ctx = db_tdrv_ctx_get();             // tdrv_ctx_0
    mla_t *mla = &ctx->mla[pc->device_id];           // imul 0x900b8
    tpb_t *tpb = &mla->_tpbs[pc->device_tpb_idx];    // imul 0x9998
    assert(mla->is_used);                            // resolver demands a live device
    assert(mla->tpb_map.map[pc->device_tpb_idx]);    // …and a mapped core
    *out_mla = mla; *out_tpb = tpb;
    return NRT_SUCCESS;
}
```

`physical_core_t` (DWARF size **24 B**) is the leaf the caller carries; it
back-points to its owning LNC: `[HIGH × OBSERVED]`

| off | type | field |
|---|---|---|
| `+0` | `uint32` | `device_id` |
| `+4` | `uint32` | `device_tpb_idx` |
| `+8` | `const virtual_core_t*` | `vcore` |
| `+16` | `uint8` | `fixes_gcc_bug` (1 B tail pad) |

`mla_t` (DWARF size **590 008 B**, one per attached device): `[HIGH × OBSERVED]`

| off | type | field | note |
|---|---|---|---|
| `+0` | `bool` | `is_used` | asserted by the resolver |
| `+4` | `uint32` | `init_state` | |
| `+8` | `uint32` | `mla_idx` | |
| `+16` | `mla_bars_t` | `bars` (64) | BAR0/BAR2 host MMIO apertures |
| `+80` | `tpb_t[8]` | `_tpbs` | **8 × 39 320 = 314 560 B** |
| `+314640` | `tpb_map_t` | `tpb_map` | which of the 8 cores are mapped |
| `+314648` | `uint32` | `device_id` | |
| `+314652` | `uint32` | `host_device_id` | |
| `+314656` | `uint32` | `routing_id` | |
| `+314664` | `ndl_device_t*` | `device` | the driver-portal handle |
| `+314672` | `top_sp_t[16]` | `top_sps` (262 016) | per-device top-SP block |
| `+576688` | `char[80]` | `bdf` | PCI B:D.F string |
| `+576768` | `dma_mem_log_t` | `dmem_logger` (1600) | the per-device usage ledger (§3.4) |
| `+578368` | `time_t` | `v2_last_nc_memory_error_ts` | |
| `+578376` | `xt_cc_t` | `xt_cc` (200) | |
| `+578576` | `aws_hal_intc_t` | `intc` (11432) | |

> **CORRECTION — `top_sps[16]` belongs in the `mla_t` map.** A backing pass
> elided the `top_sp_t[16] top_sps` block at `+314672` (262 016 B) and jumped
> straight from `_tpbs[8]` to `bdf @ +576688`. The DWARF places `top_sps` between
> them; the offsets above are the field-exact layout
> (`jq … select(.name=="mla_t")`). The `_tpbs[8]` count and `dmem_logger @
> +576768` are unaffected. `[HIGH × OBSERVED]`

`db_tdrv_ctx_init` also builds the host-device-id → routing-id map via the
driver IOCTL (`tdrv_get_host_device_id_rid_map`); if unimplemented it falls back
to a per-instance-FAMILY table. This is the topology the collectives layer uses
to address peers; for single-NeuronCore GPSIMD inference it is the identity
within one device. `[MED × INFERRED]`

---

## 2. The per-NeuronCore context: `tpb_t` (39 320 B)

`tpb_t` holds everything shared by all models resident on a single physical
NeuronCore. The DWARF layout (24 members; size **39 320 B == 0x9998**), members
material to this page: `[HIGH × OBSERVED]`

| off | type | field | note |
|---|---|---|---|
| `+0` | `volatile void*` | `tpb_mem_base` | core SBUF/reg window base |
| `+8` | `volatile void*` | `tpb_regs_base` | |
| `+16` | `volatile void*` | `ens_regs_base` | |
| `+24` | `int` | `idx` | `device_tpb_idx` (0..7) |
| `+32` | `notification_t` | `notification` (15816) | NQ rings |
| `+15848` | `pool_stdio_block_t` | `pool_stdio_block` (1176) | Q7 printf ring |
| **`+17024`** | `dmem_allocator_t*` | **`tpb_allocator`** | **the per-core GLOBAL heap** |
| `+17032` | *anon `sunda` union* (2960) | — | ext-isa ulib cache (§6) |
| **`+19992`** | `pthread_mutex_t` | **`model_db_lock`** (40) | guards `model_db` |
| **`+20032`** | `ht_t*` | **`model_db`** | the per-core MODEL DATABASE |
| `+20040` | `H_MODEL` | `h_running_model` | currently-executing model id |
| `+20048` | `aws_hal_dma[32]` | `dma` (1280) | 32 DMA-engine HAL handles |
| `+21328` | `cc_resource_pool_context` | `global_cc_resource` (872) | |
| `+22200` | `tdrv_scratchpad_t` | `scratchpad` (16440) | per-core DRAM scratch (§8) |
| `+38640` | `hw_exec_queue_t` | `hw_exec_queue` (224) | |
| `+38864` | `sync_point_t` | `sync_point` (16) | |
| `+38880` | `ib_addrs_one_eng_t[5]` | `ready_exec_program_ib_addrs` (280) | |
| `+39160` | `dmem_t*[2]` | `ready_exec_program_switch_bufs` | |
| `+39176` | `dmem_list_t` | `ready_exec_program_instr_mem_tracker` | |
| `+39192` | `nrtucode_core_t*[5]` | `nrtucode_core` | NX PE/ACT/POOL/DVE/SP cores |
| **`+39232`** | `nrtucode_core_t*[8]` | **`pooling_q7_nrtucode_core`** | the **8** GPSIMD Q7 cores |
| `+39296` | `nrtucode_loadable_library_t**` | `pooling_q7_ll` | |
| `+39304` | `size_t` | `num_pooling_q7_ll` | |
| `+39312` | `nrtucode_context_t*` | `nrtucode_context` | host handle to device ulib ctx |

The two members every loaded model touches are `tpb_allocator` (`+17024`) and
`model_db` (`+20032`); both are created once per core at `tdrv_init` (§5).
`pooling_q7_nrtucode_core[8]` has its **8** count read from the DWARF array
bound — these are the host-side pointers to the eight on-device Q7 GPSIMD cores
(`NUM_POOL_CORES = 8`, carried from the bring-up pass and re-grounded here).
`h_running_model` is the single "active" handle; the runtime serialises exec on
a core, so only one model executes at a time per core even though many may be
resident in `model_db`. `[HIGH × OBSERVED]` / `[MED × CARRIED]`

---

## 3. The host device-memory allocator: dmem over the HBM/DRAM carveout

### 3.1 Allocator + node structs

`dmem_allocator_t` (DWARF size **512 B**), built by `dmem_allocator_create`
@ `0x2284c0` via `calloc(1, 0x200)`: `[HIGH × OBSERVED]`

| off | type | field | note |
|---|---|---|---|
| `+0` | `dmem_allocator_type_t` | `type` | `GLOBAL=0` (per-core) \| `MODEL=1` (per-model) |
| `+8` | `const physical_core_t*` | `pcore` | the owning NeuronCore |
| `+16` | `pthread_mutex_t` | `lock` (40) | guards the intrusive list |
| `+64` | `dmem` | `dummy_head` (192) | sentinel head of doubly-linked list |
| `+256` | `dmem` | `dummy_tail` (192) | sentinel tail |
| `+448` | `size_t` | `largest_oversized_scratchpad_var` | |

`dmem_allocator_type_t` is a DWARF enum with exactly two values:
`DMEM_ALLOCATOR_TYPE_GLOBAL=0`, `DMEM_ALLOCATOR_TYPE_MODEL=1`. The create path
`calloc`s, sets `type`, stashes `pcore`, wires `dummy_head.next=tail` /
`dummy_tail.prev=head`, and `pthread_mutex_init`s the list lock.

`dmem_t` (DWARF size **192 B == 0xC0**), built by `calloc(0xC0, 1)` in
`dmem_alloc_internal`: `[HIGH × OBSERVED]`

| off | type | field | note |
|---|---|---|---|
| `+0` | `const physical_core_t*` | `pcore` | |
| `+8` | `uint64` | `mem_handle` | NDL driver handle (key for unmap/free) |
| `+16` | `size_t` | `size` | requested logical size |
| `+24` | `uint64` | `_pa` | device physical address (from driver) |
| `+32` | `volatile void*` | `_va` | host VA if mapped (lazy; 0 at alloc) |
| `+40` | `size_t` | `align_offset` | pad to satisfy alignment |
| `+48` | `size_t` | `allocated_size` | actual bytes charged (size + align pad) |
| `+56` | `dma_mem_location_t` | `mem_type` | `HOST_DRAM=1` \| `TONGA_DRAM=2` |
| `+60` | `dma_mem_usage_type_t` | `mem_usage_type` | one of 23 categories (§3.3) |
| `+64` | `uint32` | `tdram_channel` | which HBM channel (the `hbm_idx`) |
| `+128` | `uint64` | `ref_count` | atomic; alloc=1, acquire/free adjust |
| `+136` | `char*` | `hint` | `strdup`'d label, for logging/dumps |
| `+144` | `ndl_copy_buf_t*` | `cpy_buf` | per-NC staging copy buffer |
| `+152` / `+160` | `dmem*` | `next` / `prev` | intrusive list links |
| `+168` | `dmem_allocator_t*` | `allocator` | back-ptr (for unlink on free) |

`dmem_list_t` (16 B): `head`/`tail` of `dmem_ref_node_t` (16 B: `dmem*`,
`next`). This is the **weak GC index** ("`ref_list`"/`gc_tracker`): a secondary
list that records which `dmem_t`s belong to a logical group (a model) **without
owning them**.

`dma_mem_location_t` (DWARF enum): `DMA_MEM_LOC_INVALID=0`, `HOST_DRAM=1`,
`TONGA_DRAM=2`. **`TONGA_DRAM`(2) is on-package HBM; `HOST_DRAM`(1) is host pinned
DRAM** — the two physical pools the one heap spans. `[HIGH × OBSERVED]`

### 3.2 The allocation path — `dmem_alloc(_aligned) → dmem_alloc_internal`

`dmem_alloc_aligned` @ `0x228f20` and the thin `align=0` wrapper `dmem_alloc`
@ `0x228ed0` front the core `dmem_alloc_internal` @ `0x228640`. Disassembly of
the internal pins the category-table lookup and the driver portal:
`[HIGH × OBSERVED]`

```asm
228723:  lea  0x790f36(%rip),%rdx   # 9b9660 <dmem_usage_type_to_device_category>
22872a:  movzbl (%rdx,%rax,1),%edx  ; cat = table[mem_usage_type]
22872e:  cmp  $0x14,%edx            ; 0x14 == 20 == the "invalid-for-this-location" sentinel
228731:  je   228c48 …              ; → NRT_INVALID
…
… call c2820 <ndl_memory_alloc>     ; the driver portal — carves the HBM/DRAM
… call c2a00 <ndl_memory_get_pa>    ; resolve device physical address → dmem->_pa
… call c44d0 <ndl_get_copy_buf>     ; per-NC staging copy buffer
```

```c
// dmem_alloc_internal @0x228640  [HIGH × OBSERVED]
static int dmem_alloc_internal(dmem_allocator_t *al, dmem_t **out, size_t size,
        size_t align, dma_mem_location_t loc, uint32_t hbm_idx,
        dmem_list_t *ref_list, dma_mem_usage_type_t usage, const char *hint) {
    mla_t *mla; tpb_t *tpb;
    db_physical_core_get_mla_and_tpb(al->pcore, &mla, &tpb);   // §1.1
    int nc_id = tpb->idx;

    // 2. byte-category: host vs device table, index == usage type
    uint8_t cat = (loc == HOST_DRAM)
        ? dmem_usage_type_to_host_category[usage]      // .rodata @0x9b9640
        : dmem_usage_type_to_device_category[usage];   // .rodata @0x9b9660
    if (cat == 20)                                     // 0x14 sentinel ⇒ usage invalid here
        return NRT_INVALID;

    dmem_t *m = calloc(0xC0, 1);                       // 3. node
    int r = ndl_memory_alloc(mla->device, size, align, /*is_host=*/loc==HOST_DRAM,
                             hbm_idx, 0, nc_id, cat, &m->mem_handle);   // 4. carve
    if (r) {                                           //    ENOMEM forensics:
        if (errno == 12 && loc != HOST_DRAM) { dml_driver_dump(...); return NRT_RESOURCE; }
        if (errno == 12 && loc == HOST_DRAM) return NRT_FAIL_HOST_MEM_ALLOC;
        free(m); return r;
    }
    m->_pa = ndl_memory_get_pa(m->mem_handle);         // 5. fields
    m->_va = 0;  m->size = size;  m->align_offset = (align ? … : 0);
    m->allocated_size = size + m->align_offset;
    m->mem_type = loc;  m->mem_usage_type = usage;  m->tdram_channel = hbm_idx;
    m->pcore = al->pcore;  m->ref_count = 1;  m->hint = strdup(hint);
    m->cpy_buf = ndl_get_copy_buf(mla->device, nc_id);

    dml_add_entry(mla, DMEM_ACTION_ALLOC, m);          // 6. ledger + metrics + trace
    /* if enable_metrics: tdrv_nds_register_mem_alloc; ntrace_record_event(MEM_ALLOC);
       NRT_LOG_LEVEL_TRACE "MEM_USAGE: N bytes, channel C, total (used/cap)" */

    pthread_mutex_lock(&al->lock);                     // 7. link before dummy_tail
    list_insert_before(&al->dummy_tail, m);  m->allocator = al;
    pthread_mutex_unlock(&al->lock);

    if (ref_list) dmem_list_add(ref_list, m);          // 8. weak GC index (does not own)
    *out = m;  return NRT_SUCCESS;
}
```

`dmem_alloc_aligned`'s emulation path: if the driver lacks the aligned-alloc
IOCTL (`ndl_supports_align_12` latches 0 after a one-shot WARN), it over-allocates
`size + alignment` with `align=0`, then sets
`align_offset = ((pa + alignment) & -alignment) - pa` to satisfy alignment by
offsetting inside the over-allocation. `[HIGH × OBSERVED]`

> **GOTCHA — `errno==12` (`ENOMEM`) has *two* distinct outcomes by location.** A
> device (`TONGA_DRAM`) OOM returns `NRT_RESOURCE` and triggers a driver dump
> (plus a "running out of alignment boundaries" hint when `align != 0`); a
> `HOST_DRAM` OOM returns the *different* code `NRT_FAIL_HOST_MEM_ALLOC`. A
> reimplementation that collapses these to one error code will mis-route the
> caller's recovery. `[HIGH × OBSERVED]`

**FREE** `dmem_free` @ `0x2293a0`: atomically decrement `ref_count`; on reaching
0 → `ndl_memory_unmap` (if `_va`) → `ndl_memory_free(mem_handle)` → `free(hint)`
→ `dml_add_entry(DMEM_ACTION_FREE)` → `tdrv_nds_register_mem_free` → unlink from
the allocator list under `allocator->lock` → `free(dmem_t)`.
**ACQUIRE** `dmem_acquire_reference` @ `0x229390`: atomic `ref_count++` (a shared
`dmem`, e.g. a scratchpad page referenced by several models). `[HIGH × OBSERVED]`

### 3.3 The 23 usage categories + the location→category byte tables

`dma_mem_usage_type_t` is a DWARF enum with **exactly 23 members (0..22)**,
read straight from the enum (`jq … select(.name=="dma_mem_usage_type_t")`):
`[HIGH × OBSERVED]`

| | | | | |
|---|---|---|---|---|
| 0 `GENERIC` | 1 `INSTR_PE` | 2 `INSTR_ACT` | 3 `INSTR_POOL` | 4 `IO` |
| 5 `DRAM_SPILL` | 6 `WEIGHT` | 7 `NOTIFICATION` | 8 `ACT_TABLE` | 9 `INSTR_DVE` |
| 10 `INSTR_SP` | 11 `SCRATCHPAD` | 12 `TENSOR` | 13 `UCODE_LIB` | 14 `POOL_STDIO` |
| 15 `CC` | 16 `SCRATCHPAD_NOT_SHARED` | 17 `XT_CC` | 18 `DMA_RING` | 19 `DMA_RING_SPILL` |
| 20 `DMA_RING_IO` | 21 `DMA_RING_COLLECTIVES` | 22 `MAX` | | |

`MAX=22` doubles as the per-core aggregate index (the grand total). The "23" is
**doubly grounded**: it is the enum cardinality *and* the second array bound of
`dma_mem_log_t.memory_usage_nc[8][23]` (§3.4) — neither figure comes from prose.

Three `.rodata` byte tables (index == usage type), read byte-exact at the
`nm`-resolved addresses (`.rodata` VMA == fileoff, no delta):
`[HIGH × OBSERVED]`

| table | addr | 23 bytes |
|---|---|---|
| `dmem_usage_type_to_host_category` | `0x9b9640` | `04 01 01 04 01 04 03 06 04 01 01 14 02 01 04 14 14 04 11 11 11 11 00` |
| `dmem_usage_type_to_device_category` | `0x9b9660` | `0c 08 08 0c 08 0c 0a 10 0c 08 08 0b 09 08 0c 0e 0f 0c 12 12 12 12 00` |
| `dmem_usage_type_to_aggr_index` | `0x9b9e80` | `05 00 00 05 05 01 0b 05 00 00 03 02 00 05 0a 04 05 09 07 06 08 00 00` |

The host/device "category" is the coarse driver-side pool tag passed to
`ndl_memory_alloc`. Note `0x14 == 20` in the **host** table at indices 18..21
(`DMA_RING`, `DMA_RING_SPILL`, `DMA_RING_IO`, `DMA_RING_COLLECTIVES`) marks the
collectives/dma-ring families **host-invalid** — exactly the §3.2 step-2
rejection. `map_dmem_usage_type_to_aggr_index` @ `0x229cd0` reads the aggr table
for the per-tensor-class roll-up surfaced in `nrt_get_*_memory_stats`.

> **QUIRK — the rejection sentinel and the enum value 20 collide numerically but
> are unrelated.** The "invalid for this location" sentinel is the *category-byte*
> value `0x14 == 20`, read out of the host/device tables; it is **not** the
> usage-enum member `DMA_MEM_USAGE_TYPE_DMA_RING_IO = 20`. They share the integer
> 20 by coincidence: the host table happens to tag the dma-ring family with the
> reserved category byte `0x14`. Reading `cmp $0x14` as "compare against the
> `DMA_RING_IO` usage type" is wrong — the compare is against the *table output*,
> after the `movzbl (%rdx,%rax,1)`. `[HIGH × OBSERVED]`

### 3.4 The per-device usage ledger — `dma_mem_log_t` (1600 B)

`mla.dmem_logger` (DWARF size **1600 B**): `[HIGH × OBSERVED]`

| off | type | field | note |
|---|---|---|---|
| `+0` | `pthread_mutex_t` | `lock` (40) | |
| `+40` | `uint32` | `mla_idx` | |
| `+48` | `size_t` | `max_entries` | |
| `+56` | `size_t` | `entry_count` | |
| `+64` | `size_t` | `next_entry_idx` | |
| `+72` | `size_t` | `entries_dropped` | |
| `+80` | `size_t[4]` | `memory_usage` | aggregate (device/host/…) totals |
| `+112` | `size_t[8][23]` | `memory_usage_nc` | **per-(core, usage-type) byte counters** |
| `+1584` | `dma_mem_log_entry_t*` | `entries` | ring of recent alloc/free events |
| `+1592` | `bool` | `verbose_log_dev_mem` | |

`memory_usage_nc[8][23]` is **1472 B == 8 × 23 × 8**, matching the 8-core
`tpb` fan-out × 23-value usage enum exactly. Every alloc/free updates
`memory_usage_nc[nc][usage]` and the aggregates; `[nc][22]` (`MAX`) is the
per-core grand total reported as "used" against `aws_hal_get_hbm_size()`
("cap"). This ledger is the OOM forensic trail dumped (`dml_dump` /
`dml_driver_dump` / `dml_log_dev_mem`) on an `ENOMEM`. `[HIGH × OBSERVED]`

---

## 4. Multi-model sharing of a NeuronCore: the per-core `model_db`

### 4.1 The database

Each `tpb_t` owns one `model_db` (`ht_t*`, `+20032`) under `model_db_lock`
(`+19992`). `ht_t` (DWARF size **32 B** + flexible nodes): `size`,
`size_mask` (pow2−1), `count`, `free_node_fn`, then `ht_node_t[]`. `ht_node_t`
(DWARF size **48 B**): `key(uint64) @ +0`, `<val union> @ +8`, `buf_size @ +16`,
`bucket @ +24`, `key_type @ +32`, `next @ +40`. The key is `H_MODEL.id` (a
`uint32` in a 4-byte `H_MODEL` struct), so all models loaded onto a core coexist
in one table. `[HIGH × OBSERVED]`

### 4.2 Handle minting — `tdrv_get_unique_h_model` @ `0x3053f0`

```asm
3053f5:  lock xadd %rax,0x903d82(%rip)   # c09180 <last_model_handle.50>  (atomic fetch-add)
3053fe:  add  $0x1,%rax
305402:  je   305408                     ; wrap to 0 ⇒ bump again (0 reserved/invalid)
30540d:  lock xadd %rax,0x903d6a(%rip)   # c09180 <last_model_handle.50>  (re-bump on wrap)
```

`H_MODEL = ++(atomic last_model_handle_50)`; if the increment yields 0 it bumps
again, because **handle 0 is reserved/invalid**. The counter at `0xc09180` is
process-global and monotonic — handles are unique across all cores and models in
the process. `[HIGH × OBSERVED]`

### 4.3 Insert — `add_model` @ `0x2275f0`

```c
// add_model @0x2275f0  [HIGH × OBSERVED]
resolve mla/tpb;  pthread_mutex_lock(&tpb->model_db_lock);
if (ht_find(model_db, h.id) != 0)            // ht_find returns 0 on FOUND, nonzero on absent
    ht_insert(model_db, h.id, &m->ht_node);  // link via the EMBEDDED node (model_t+7496)
else
    return NRT_INVALID_HANDLE;               // "Model %u already in DB"
pthread_mutex_unlock(&tpb->model_db_lock);
```

The model is linked into the table via its **embedded** `ht_node` (`model_t +
7496`) — no separate node allocation.

> **QUIRK — `ht_find`'s return polarity is inverted from the obvious reading.**
> Here `ht_find(...) != 0` is the **"not present, safe to insert"** branch; a
> return of `0` means **found**. A reimplementer who assumes `0 == not-found`
> (the C idiom) will invert the duplicate-handle guard and silently double-insert.
> `[HIGH × OBSERVED]`

### 4.4 Lookup + ref — `get_model_ref_count` @ `0x2274c0`

```c
// get_model_ref_count @0x2274c0  [HIGH × OBSERVED]  — the read side
pthread_mutex_lock(&tpb->model_db_lock);
ht_node_t *node; ht_find(model_db, h.id, &node);
model_t *m = (model_t*)((char*)node - 7496);   // container_of: ht_node @ model_t+7496
atomic_fetch_add(&m->ref_count, 1);            // ref_count @ model_t+6488
pthread_mutex_unlock(&tpb->model_db_lock);
return m;
```

The `container_of` math is verified arithmetically against the decompiler's
pointer expressions: `&node[-157].next == node - 157·48 + 40 == node - 7496`
(the `ht_node` offset), and `&node[-21] == node - 21·48 == node - 1008`, i.e.
`7496 - 1008 == 6488 == model_t.ref_count`. An executing thread bumps the
model's ref so an in-flight unload cannot free it. `[HIGH × OBSERVED]`

### 4.5 Remove + drain — `remove_model` @ `0x227750`

```asm
227784:  call pthread_mutex_lock         ; lock model_db_lock
22779d:  call ht_find                    ; locate node
2277c0:  call ht_remove                  ; unlink from lookup FIRST
2277d7:  call pthread_mutex_unlock       ; unlock
2277e1:  lock cmpxchg %rdx,-0x3f0(%r13)  ; then spin-drain ref_count == 0 (model+6488 region)
2277ea:  jne  2278c5                     ; …busy-wait until every outstanding exec dropped its ref
```

```c
// remove_model @0x227750  [HIGH × OBSERVED]
lock;  ht_find;  ht_remove(model_db, h.id);  unlock;            // no new exec can acquire it now
while (atomic_load(&m->ref_count) != 0) _mm_pause();            // drain in-flight execs
return m;                                                       // orphaned model_t* → model_free
```

The unload removes the model from the lookup **first** (so no new exec can
acquire it), then **busy-waits** until every outstanding exec has dropped its
ref. This is the host-side serialisation seam between exec and unload.
`[HIGH × OBSERVED]`

### 4.6 Why this is the "sharing" model

Models share a core purely by co-residence in `model_db`; the runtime does
**not** time-slice or preempt. Concurrency between resident models is mediated by
(a) `h_running_model` + per-core exec serialisation (one model executes at a time
per core), (b) per-model `ref_count` (multiple host threads may hold/execute the
*same* model concurrently — exec is queued elsewhere), and (c) distinct per-model
device memory (§5.2), so loads/unloads are independent. `[HIGH × OBSERVED]` /
`[MED × INFERRED]`

---

## 5. The two-tier allocator + per-LNC shared resources (load time)

### 5.1 Tier 1 — the per-NeuronCore GLOBAL allocator

In `tdrv_init` (per core), the runtime builds:

```c
tpb->tpb_allocator = dmem_allocator_create(tpb_pcore, DMEM_ALLOCATOR_TYPE_GLOBAL);
pthread_mutex_init(&tpb->model_db_lock, ...);
```

The GLOBAL allocator owns long-lived, **core-scoped** device memory: notification
queues, scratchpad pages, the ucode/ext-isa staging, DMA rings — anything whose
lifetime is the core, not a single model. `nrt_tensor_allocate`'s DEVICE tensors
also draw from `tpb_allocator` (§7). `[HIGH × OBSERVED]` / `[MED × CARRIED]`

### 5.2 Tier 2 — the per-LNC MODEL allocator(s) — `vtpb_info_shared_init_vtpb` @ `0x3148e0`

Per Logical NeuronCore the runtime builds `num_kbins` shared "mr_set" slots (one
per NEFF subgraph/kbin staged onto the LNC). `vtpb_info_shared_mr_set_t`
(DWARF size **32 B**): `[HIGH × OBSERVED]`

| off | type | field |
|---|---|---|
| `+0` | `ht_t*` | `mem_ref_set` (the mem_ref name→dmem index) |
| `+8` | `dmem_allocator_t*` | `model_allocator` (`DMEM_ALLOCATOR_TYPE_MODEL`) |
| `+16` | `dmem_list_t*` | `gc_tracker` (weak list of this model's dmem) |
| `+24` | `tdrv_scratchpad_cleanup_info_t*` | `scratchpad_cleanup_info` |

```c
mr_set->model_allocator = dmem_allocator_create(pcore, DMEM_ALLOCATOR_TYPE_MODEL);
ht_init(&mr_set->mem_ref_set, 0x400, mem_ref_free_one);   // 1024-bucket name index
```

These slots are MODEL-scoped: weights, instruction streams, per-model DMA rings,
mr pointer vars — everything a NEFF owns. Freeing the model's allocator
mass-releases them.

### 5.3 Ownership transfer at model add — `kbl_model_add` @ `0x3058e0`

`model_t` is `calloc(0x1E40 == 7744 B)`. The relevant DWARF fields:
`[HIGH × OBSERVED]`

| off | type | field |
|---|---|---|
| `+0` | `char[256]` | `name` |
| `+6384` | `H_MODEL` | `h_model` |
| `+6464` | `dmem_allocator_t*` | `dmem_allocator` (the per-LNC MODEL allocator, moved in) |
| `+6472` | `dmem_list_t*` | `gc_tracker` |
| `+6480` | `ucode_lib_set_info_t*` | `ulib_set_info` |
| `+6488` | `volatile uint64` | `ref_count` |
| `+6528` | `al_hal_tpb_arch_type_t` | `target` |
| `+6664` | `ht_t*` | `static_mr_set` |
| `+6672` | `ht_t*` | `io_mr_to_name_map` |
| `+7496` | `ht_node_t` | `ht_node` (the `model_db` link node) |

`kbl_model_add` reads
`vtpb_info->shared_info->v2.mr_sets[vtpb_info->vtpb_rel_sg_id]`, asserts each of
`mem_ref_set`/`gc_tracker`/`model_allocator`/`scratchpad_cleanup_info` is
non-NULL, **MOVES them into the `model_t`, then ZEROES the shared slots**
(`*(_OWORD*)&mr_set->mem_ref_set = 0; *(_OWORD*)&mr_set->gc_tracker = 0;`). So
the per-LNC MODEL allocator + mem_ref index are **handed to** the model — each
loaded model takes ownership of its own MODEL allocator. After the move it:

- verifies the SBUF EVTACCEL carveout (§8),
- inits dbtc storage, the queue-bundle instance LUT, data-refill rings, the
  io-mr→name map, mr pointer vars, io queues, act/dve config,
- **for `al_hal_tpb_get_arch_type() == 2` (`SUNDA`)** calls `ucode_stage_libs`
  (§6) — disassembly: `call al_hal_tpb_get_arch_type; cmp $0x2,%eax; je …call
  ucode_stage_libs @0x310ea0`,
- runs `sequencer_setup_instr`, `dma_ring_setup_queue_bundles`, then
  `add_model` (§4.3) → `model_db`.

It logs "Added model: H on (nd*D*:nc*C*) mem_usage before/after" by pulling the
§3.4 ledger counters around the load. `[HIGH × OBSERVED]`

> **CORRECTION — the arch-type *names* are inverted in a backing pass; the
> numeric gates are correct.** A backing pass labelled the `ucode_stage_libs`
> gate "`arch_type==2` (the Cayman/GPSIMD-class device)" and the EVTACCEL/host
> branches "`arch_type==3` (Sunda/Inf1-class)". The DWARF enum
> `al_hal_tpb_arch_type_t` says the **opposite**: `SUNDA=2`, `CAYMAN=3`,
> `MARIANA=4`. Re-grounding on the binary: `kbl_model_add` at `0x305d25` does
> `call al_hal_tpb_get_arch_type; cmp $0x2,%eax; je 0x3062c2` and `0x3062c2`
> calls `ucode_stage_libs` — so **ext-isa ulib staging is gated on arch
> `SUNDA(2)`**, not Cayman. The EVTACCEL carveout branch (and the
> host-tensor reject, §7) is gated on `cmp $0x3` = **`CAYMAN(3)`**, not Sunda.
> The numeric comparisons (`==2`, `==3`) are right; the device *names* attached
> to them were swapped. Use `SUNDA=2 / CAYMAN=3 / MARIANA=4` throughout.
> `[HIGH × OBSERVED]`

### 5.4 Free — `model_free` @ `0x3055f0`

After `remove_model` drains `ref_count` to 0: free dma rings / vring set / ddrs
cache; dbtc/dve/act/cc cleanup; if `ulib_set_info` is per-model, free it (NOT the
shared ext-isa); free kbin patch info; cc init descs; **then**
`dmem_allocator_destroy(model->dmem_allocator)` — one call reclaims the model's
entire device-memory footprint — then `free(model_t)`.
`dmem_allocator_destroy` @ `0x228550`: for each node head..tail, set
`node.allocator = 0`, zero its links, `mutex_destroy`, `free`. The `dmem_t`s
themselves are released through `dmem_free`/driver teardown.
(The symbol ships as `model_free.part.0` @ `0x3055f0`.) `[HIGH × OBSERVED]`

---

## 6. ext-isa `ulib` sharing under `ulib_staging_lock` (the GPSIMD cache)

The anon `sunda` union inside `tpb_t` (`+17032`, 2960 B) resolves to (add 17032
for absolute `tpb_t` offset): `[HIGH × OBSERVED]`

| union off | abs `tpb_t` off | type | field |
|---|---|---|---|
| `+0` | `+17032` | `aws_hal_stpb` (1104) | `hal_stpb` |
| `+1104` | `+18136` | `dma_queue_info_t[5]` (1800) | `instr_fetch_queue` |
| `+2904` | `+19936` | `ucode_lib_set_info_t*` | `ulib_set_info_extisa_only` |
| `+2912` | `+19944` | `pthread_mutex_t` (40) | `ulib_staging_lock` |
| `+2952` | `+19984` | `bool` | `set_ulib_reg` |

`ucode_stage_libs` @ `0x310ea0` (called from `kbl_model_add` for `SUNDA`,
arch `==2`). Disassembly pins the cap and the single-lib branch:

```asm
310ed0:  cmp  $0x1,%rax          ; num_libs == 1 ? (the single-ext-isa GPSIMD case)
310eda:  cmp  $0x9,%rax          ; num_libs > 9 ?
310ede:  ja   310f20             ; → "Exceeded max number of GPSIMD libs" (cap = 9)
310f6c:  call pthread_mutex_lock ; ulib_staging_lock
…
310fb9:  call pthread_mutex_unlock
```

```c
// ucode_stage_libs @0x310ea0  [HIGH × OBSERVED]
if (ulib_set->num_libs > 9) return NRT_FAILURE;               // cap = 9 GPSIMD libs

if (ulib_set->num_libs == 1) {                                // the common GPSIMD ext-isa case
    pthread_mutex_lock(&tpb->sunda.ulib_staging_lock);
    if (tpb->sunda.ulib_set_info_extisa_only != NULL) {
        mod->ulib_set_info = tpb->sunda.ulib_set_info_extisa_only;       // REUSE the cached copy
    } else {
        ucode_alloc_new_staged_libs(pcore, tpb->tpb_allocator,          // @0x310660
                                    &tpb->sunda.ulib_set_info_extisa_only);
        mod->ulib_set_info = tpb->sunda.ulib_set_info_extisa_only;
        ucode_stage_libs_impl(...);                                     // @0x310c00 — STAGE ONCE
    }
    pthread_mutex_unlock(&tpb->sunda.ulib_staging_lock);
} else {                                                       // 2..9 libs (multi-core custom-op set)
    ucode_alloc_new_staged_libs(pcore, model_allocator, &mod->ulib_set_info);  // PER-MODEL copy
    ucode_stage_libs_impl(...);                                                 // no sharing
}
```

So the **single ext-isa GPSIMD library is staged into device memory ONCE per
NeuronCore and shared by every model on that core** (cached in
`tpb->sunda.ulib_set_info_extisa_only`, the staging serialised by
`ulib_staging_lock`, drawn from the **GLOBAL** `tpb_allocator`); custom-op
multi-lib sets (2..9) are staged **per-model** from the **model's own**
allocator. `[HIGH × OBSERVED]`

`ucode_lib_set_info_t` (DWARF size **48 B**, 6 members): `scratch_space(dmem*)`,
`ucode_table(dmem*)`, `extram(dmem*)`, `num_libs(int)`, `libs(ucode_lib_info_t*)`,
`lib_dmem(dmem*)`. `ucode_lib_info_t` (DWARF size **32 B**): `address(uint64)`,
`flags`, `cpu_id`, `total_cpus`, `num_funcs`, `funcs(ucode_func_info_t*)`.
`ucode_stage_libs_impl` `dmem_buf_copyin`'s each lib's bytes into `lib_dmem` at a
running offset and records `address = pa + align + offset`; `HIBYTE(cpu_id)==1`
counts single-core-custom-op libs. The on-device library table
`SUNDA_UCODE_LIB_LIBRARY_TABLE_ENTRY` (DWARF size **280 B** per entry; the array
is `[97]`) is the device-side mirror this stages into. `[HIGH × OBSERVED]`

> **NOTE — the `sunda`-named union, KaenaHal, and the arch name.** The union is
> DWARF-tagged `sunda`, and `libnrt` ships per-arch HAL source paths as
> `__FILE__` literals: `…/KaenaHal-2.31.0.0/…/src/src/{common,sunda,cayman,
> mariana}/…` (5 files each for `sunda`/`cayman`/`mariana`; **23** `common`;
> **0** `maverick`), e.g. `…/src/src/common/q7/aws_hal_q7.c` (the GPSIMD Q7
> HAL). These confirm `SUNDA`/`CAYMAN`/`MARIANA` (= NC-v2/v3/v4) as
> binary-citeable per-arch keys; **`Maverick` (v5) is header-OBSERVED only —
> there is no `maverick` source dir in the ELF**, so any v5 claim is
> `[INFERRED]`. `[HIGH × OBSERVED]` (sunda/cayman/mariana) / `[LOW × INFERRED]`
> (maverick).

---

## 7. Tensor / tensor-set IO-buffer management

### 7.1 `nrt_tensor_t` + storage

`nrt_tensor_t` (DWARF size **192 B**): `name(char*)`, `sto(storage*)`, `_offset`,
`_size`, `extra(void*)`, `ref_count(atomic)`, `output_completion_count(atomic)`.
`nrt_tensor_storage_t` (DWARF size **320 B**): `hbm_idx(uint32)`,
`allocated_size`, `type` (`INVALID`/`MALLOC`/`DMA`/`FAKE`), an anon union holding
either the `dmem*` or a host pointer, `ref_count`, `mem_owned_by_tensor(bool)`,
a `tensor_op_cv_lock`/`cv` async-completion condvar, `pending_exec_count_read`,
`pending_exec_count_write`, `vtpb_idx`. A device tensor's storage wraps a
`dmem_t`; the `pending_exec_count_*` + condvar implement
`nrt_tensor_check_output_completion` — a tensor cannot be read until the DMA
writing it has completed. `[HIGH × OBSERVED]`

### 7.2 `nrt_tensor_allocate` @ `0xbc320` → `tensor_allocate` @ `0x30e8a0`

The public wrapper's gates (args `a1`=mem-loc kind, `a2`=logical-core,
`a3`=size, `a4`=name, `a5`=out): `[HIGH × OBSERVED]`

- **State gate**: must be `NRT_STATE_INIT`, else CLOSED/UNINITIALIZED/incompatible.
- **Range gate**: `a2 < |nrt_config.visible_virtual_cores|`, else "Cannot
  allocate a tensor on Logical NeuronCore %d …" (`NRT_INVALID`).
- **Host-on-Inf1 reject**: `a1==1` (HOST) && `arch_type==3` (**`CAYMAN`**) →
  rejected. Disassembly @ `0xbc6bd`:
  `cmp $0x1,%r13d` (mem-loc == HOST) … `call al_hal_tpb_get_arch_type;
  cmp $0x3,%eax` (arch == `CAYMAN`) → log "Can not allocate tensor (name:%s) on
  Host for NC:%u" (`NRT_RESOURCE`).
- Resolve `virtual_core` via `vtpb_get_virtual_core(a2)`; `a1==0` → `TONGA_DRAM`
  (device); `a1==2` → host malloc tensor; else `HOST_DRAM`.

```c
// tensor_allocate @0x30e8a0 — DMA (device) path  [HIGH × OBSERVED]
db_physical_core_get_mla_and_tpb(&vcore->tpbs[0], &mla, &tpb);
uint32_t hbm_idx = vtpb_get_default_hbm_idx(vcore);          // = get_default_hbm_index(tpb->idx)
dmem_t *mem;
dmem_alloc(tpb->tpb_allocator, &mem, size, mem_loc, hbm_idx, /*ref_list=*/0,
           DMA_MEM_USAGE_TYPE_TENSOR, name);                 // GLOBAL allocator, usage=TENSOR
nrt_tensor_storage_t *sto = calloc(0x140, 1);                // 320 B storage
init_condvar(&sto->cv); init_lock(&sto->lock);
sto->dmem = mem; sto->hbm_idx = hbm_idx; sto->allocated_size = size;
sto->vtpb_idx = vcore->vtpb_idx; sto->mem_owned_by_tensor = 1;
```

`MALLOC` type instead does a plain `malloc(size)` wrapped in storage (host I/O
staging). So **user device tensors** (the IO buffers bound to NEFF var slots) are
drawn from the per-core **GLOBAL** `tpb_allocator` under usage category
`TENSOR`, on the LNC's **default HBM channel** — they are **not** part of any
model's MODEL allocator, so they outlive any single model load and are bound at
execute. `[HIGH × OBSERVED]`

> **GOTCHA — the host-on-`CAYMAN` reject inverts the device-class name.** The
> reject fires on `arch_type == 3 == CAYMAN`, not "Sunda/Inf1" as a backing pass
> labelled it. The numeric gate (`cmp $0x3`) is correct; the device name was
> swapped. (Same root cause as the §5.3 CORRECTION.) `[HIGH × OBSERVED]`

### 7.3 `nrt_allocate_tensor_set` @ `0xbeae0`

After the state gate, a tensor SET is `kbl_init_feature_map_set(&fmap)` — the set
**is** a `kbl_feature_map_set_t` (the feature-map set object the kelf loader and
metaneff bind against). The companion `nrt_tensor_list_t` (DWARF size **16 B**:
`tensors**`, `num_tensors`) is the flat-array form used by batched execute.
`[HIGH × OBSERVED]`

### 7.4 Memory stats — `nrt_get_vnc_memory_stats` / `dmem_get_memory_stats`

`dmem_get_memory_stats(vcore, stats)` sums
`mla.dmem_logger.memory_usage_nc[device_tpb_idx][22]` (the per-core aggregate)
over **all physical cores in the LNC** → `stats[0] = used`;
`stats[1] = aws_hal_get_hbm_size()` = `cap`. An LNC's reported HBM usage is the
**union of its 1–2 physical cores'** ledgers. `[HIGH × OBSERVED]`

---

## 8. The SBUF carveout vs the DRAM scratchpad

The host dmem heap (§3) is HBM/DRAM only. The on-chip 128-partition SBUF is
**not** in the dmem heap — it is reserved by the NEFF as a **carveout** and
**validated, not allocated**, at model-add. `kbin_sb_carveout_type_t` (DWARF
enum): `INVALID=0`, `KBIN_SB_CARVEOUT_TYPE_EVTACCEL=1` — the only carveout type
present. `kbl_model_add` (arch `== 3` = `CAYMAN`) scans for the EVTACCEL
carveout and asserts the device geometry. Disassembly @ `0x306070`+:
`[HIGH × OBSERVED]`

```asm
3060a0:  cmpl $0x1,(%rdx)        ; carveout type == KBIN_SB_CARVEOUT_TYPE_EVTACCEL(1) ?
30607f:  movb $0x0,0x1c32(%r14)  ; model.* flags
30617e:  test %rdx,%rdx          ; partition START
306181:  jne  306276             ;   != 0 ⇒ "must start at 0"
30618b:  cmp  $0x80,%rdx         ; partition COUNT == 128 (0x80) ?
306198:  cmp  $0x37ff8,%rdx      ; SB partition byte-offset == fixed base 0x37ff8 ?
3061a9:  cmp  $0x8,%rax          ; SB size == 8 ?
```

So the EVTACCEL carveout is a **static per-model reservation** checked against
the device geometry (START==0, COUNT==128, offset==`0x37ff8`, size==8), setting
`model.reset_evt_accel` / `program_fp8_cfg` flags; absence logs "model … has no
evtaccel reservation on SBUF". There is **no per-byte host allocator for SBUF —
the device engines own it**. `[HIGH × OBSERVED]`

**The per-core DRAM scratchpad** `tdrv_scratchpad_t` (DWARF size **16 440 B**,
`tpb_t+22200`): `lock`, `page_size @ +40`, `num_pages @ +48`,
`is_contiguous @ +52`, `tdrv_scratchpad_page_t[1024] pages @ +56` (each 16 B:
`dmem*`, `uint32 refs`). `tdrv_scratchpad_init` @ `0x302900`: if
`experimental_contiguous_scratchpad` && arch>2 && LNC>1 && driver-feature 0x80 →
`page_size = 0x4000000` (64 MB), `is_contiguous=1`.
`tdrv_scratchpad_get_mem` @ `0x3026d0` maps a `(offset,size)` into a page's dmem
(contiguous mode reverses page order). The scratchpad pages are
GLOBAL-allocator-backed and **ref-counted** (`dmem_acquire_reference`), so
several resident models can share a page; the per-model
`scratchpad_cleanup_info` handed at load tracks the model's claim.
`[HIGH × OBSERVED]`

---

## 9. The fractional / co-location (LNC) model

The runtime exposes Logical NeuronCores, not physical TPBs, to a process.
`virtual_core_t` (DWARF size **64 B**): `[HIGH × OBSERVED]`

| off | type | field |
|---|---|---|
| `+0` | `uint32` | `vtpb_idx` |
| `+4` | `uint32` | `vtpb` |
| `+8` | `uint32` | `nec_dev_id` |
| `+12` | `uint32` | `num_tpbs` (1..2) |
| `+16` | `physical_core_t[2]` | `tpbs` (48 B) |

The process-global slice is `owned_vcores[num_owned_vcores]`;
`vtpb_get_virtual_core(idx)` bounds-checks against `num_owned_vcores`;
`vtpb_get_virtual_core_by_id` / `_from_nec_dev_id` give alternate keys.

**LNC sizing — `parse_vnc_config` @ `0x83b40`:** `[HIGH × OBSERVED]`

```asm
83b67:  call nrt_get_dev_info         ; → (nd_count, nc_per_device, arch_type @ 0xc(%rsp))
83b76:  cmpl $0x2,0xc(%rsp)           ; arch_type == 2 == SUNDA ?  → force LNC size 1
…  rodata strings: "NEURON_RT_VIRTUAL_CORE_SIZE",
   "out != 0", "(nc_per_device % out) == 0",
   "Logical core size (%u) must be a power of two and less than or equal to %u",
   "Only LNC Size of 2 is supported at this moment on this instance type. Requested LNC Size %u"
```

```c
// parse_vnc_config @0x83b40  [HIGH × OBSERVED]
nrt_get_dev_info(&nd_count, &nc_per_device, &arch_type, ...);
if (arch_type == AL_HAL_TPB_ARCH_TYPE_SUNDA /* ==2 */) lnc_size = 1;   // SUNDA forces 1
else {
    lnc_size = getenv("NEURON_RT_VIRTUAL_CORE_SIZE") ? parsed : 2;     // default 2
    assert(lnc_size != 0);                                             // "out != 0"
    assert((nc_per_device % lnc_size) == 0);                           // partition cleanly
    // and: power-of-two, ≤ cap; some instance types accept only size 2
}
```

A device's `nc_per_device` physical cores are partitioned into LNCs of size ∈
{1,2,4,…}; each LNC presents as one core to the model. Within an LNC of 2,
`db_physical_core_get_mla_and_tpb` resolves each physical core independently;
model staging builds `num_kbins` mr_sets per LNC (one subgraph per physical core)
and the model spans both via `vtpb_info->vtpb_rel_sg_id`. `[HIGH × OBSERVED]`

> **CORRECTION — `SUNDA(2)` is what forces LNC size 1, and the env gate carries a
> stronger instance restriction than a backing pass noted.** A backing pass wrote
> "arch==SUNDA → LNC size forced to 1" (correct *outcome*) but elsewhere mislabels
> arch 2/3; the binary's `cmpl $0x2,0xc(%rsp)` at `0x83b76` confirms the forced-1
> case is **arch 2 == SUNDA**. Additionally, the ELF carries a string the pass
> omitted — "**Only LNC Size of 2 is supported at this moment on this instance
> type**" — so on some SKUs the *only* legal non-1 size is 2, narrower than the
> general "power-of-two" gate. `[HIGH × OBSERVED]`

**Co-location (the generate/upscale fractional pattern):** at the runtime level
there is **no single-process fraction**; co-location is achieved by **separate
processes** each opening a **disjoint** `owned_vcores` slice (the driver/KAI
scheduler partitions physical cores; the host runtime just sees its slice). Each
process therefore has its own `tdrv_ctx_0`, its own per-core `tpb_allocator` and
`model_db`; there is **no cross-process model sharing inside libnrt**. Resource
arbitration between two co-located workloads is the driver's HBM carveout +
`ndl_memory_alloc` per-channel accounting, surfaced per-process via the §3.4
ledger and `aws_hal_get_hbm_size()`. `[MED × INFERRED]`

---

## 10. The device-memory map (the host allocator's view)

Per attached device (`mla_t`), per HBM channel (`tdram_channel`), the host carves
a flat heap through `ndl_memory_alloc`. What lands where, by allocator tier:

| tier (lifetime) | allocator | contents (usage category) |
|---|---|---|
| **PER-core** | `tpb->tpb_allocator` (GLOBAL) | notification queues (`NOTIFICATION`); scratchpad pages (`SCRATCHPAD`/`DRAM_SPILL`); the **shared** ext-isa ulib (`UCODE_LIB`, staged once, §6); pool_stdio ring (`POOL_STDIO`); **user IO tensors** (`TENSOR`, §7); XT/CC scratch (`XT_CC`, `CC`); DMA rings (`DMA_RING*`) |
| **PER-model** | `model->dmem_allocator` (MODEL) | instruction streams (`INSTR_PE`/`ACT`/`POOL`/`DVE`/`SP`); weights/constants (`WEIGHT`); act tables (`ACT_TABLE`); model DMA rings + mr pointer vars (`DMA_RING*`, `GENERIC`); per-model custom-op ulib copies (`UCODE_LIB`, multi-lib case, §6) |
| **RESERVED** (not in the heap) | — | on-chip SBUF EVTACCEL carveout (validated only, §8; device owns SBUF/PSUM) |

**Accounting:** every byte is booked in
`mla.dmem_logger.memory_usage_nc[nc][usage]`; `[nc][22]` is the per-core total;
`cap = aws_hal_get_hbm_size()`. The channel is chosen by `tdram_channel`
(tensors use `vtpb_get_default_hbm_idx`; collectives spread).

**Ownership / GC summary:** `[HIGH × OBSERVED]`

- `dmem_t` — freed when its last `ref_count` drops (`dmem_free`, driver unmap).
- GLOBAL allocator — its intrusive list is freed at `tdrv_destroy` (the inverse
  of §5.1, in the teardown pass).
- MODEL allocator — its intrusive list is freed at `model_free` (one call frees
  the whole NEFF footprint, §5.4).
- `gc_tracker` (`dmem_list`) — a **weak** index; it does not own; used to
  enumerate a model's dmem for accounting / scratchpad cleanup.
- Shared dmem (scratchpad pages, ext-isa ulib) — **ref-counted**, survives any
  single model's unload.

---

## 11. Adversarial self-verification

The five strongest structural claims, re-challenged against the binary:

1. **`mla[32]` / `tpb[8]` / 23 categories are real array bounds, not prose.**
   DWARF: `tdrv_ctx_t.mla` is `mla_t[32]` (byte-size `18 880 256 = 32×590 008`);
   `mla_t._tpbs` is `tpb_t[8]` (`314 560 = 8×39 320`); `dma_mem_usage_type_t` has
   **23** enum members (0..22) **and** `dma_mem_log_t.memory_usage_nc` is
   `size_t[8][23]` (`1472 = 8×23×8`). The resolver's `imul $0x900b8`/`imul
   $0x9998` confirm the strides 590 008 / 39 320. **PASS.** `[HIGH × OBSERVED]`

2. **The three category byte tables are exact.** `objdump -s -j .rodata` at the
   `nm`-resolved addresses returns the 23-byte strings in §3.3 verbatim
   (`.rodata` VMA == fileoff, no delta). The host table's `0x14` at idx 18..21 is
   the §3.2 reject sentinel. **PASS.** `[HIGH × OBSERVED]`

3. **The arch-type gates are `SUNDA=2 / CAYMAN=3`, the *opposite* of a backing
   pass's names.** `kbl_model_add` @ `0x305d25`: `call al_hal_tpb_get_arch_type;
   cmp $0x2,%eax; je` → `ucode_stage_libs`; the EVTACCEL/host branches compare
   `$0x3`. The DWARF enum confirms `SUNDA=2, CAYMAN=3, MARIANA=4`. **Backing pass
   FAILED on names; corrected in §1.1/§5.3/§7.2.** `[HIGH × OBSERVED]`

4. **`container_of` from `ht_node` to `model_t` is byte-exact.** `ht_node @
   model_t+7496`, `ref_count @ +6488`; the decompiler's `node[-157].next` =
   `node − 7496` and `node[-21]` = `node − 1008` (⇒ `7496 − 1008 = 6488`)
   reproduce these from `ht_node_t` size 48. **PASS.** `[HIGH × OBSERVED]`

5. **Handle minting reserves 0; remove-then-drain is the exec/unload seam.**
   `tdrv_get_unique_h_model` @ `0x3053f0`: `lock xadd … last_model_handle.50;
   add $1; je <re-bump>` (0 skipped). `remove_model` @ `0x227750`:
   `ht_remove` (unlink) **before** the `lock cmpxchg` ref_count spin-drain.
   **PASS.** `[HIGH × OBSERVED]`

> **NOTE — what stays `INFERRED`.** §9's cross-process co-location arbitration is
> below libnrt's visibility (the multi-process partitioning is the driver's), so
> it is `[MED × INFERRED]`; the per-SKU `nc_per_device → LNC` fan-out is read only
> through the `SUNDA`-forcing + the pow2/modulo gate, not enumerated per instance
> family. The `Maverick` (NC-v5) arch is header-OBSERVED only (no `maverick`
> source dir in the ELF) → `[LOW × INFERRED]`.

---

## 12. Scope handoff

- The execute path that **consumes** the per-model dmem and binds the §7
  `nrt_tensor` IO buffers to `model_db` models — the exec-resource pool / async
  worker, `h_running_model` use, and the per-exec `ref_count` acquire (§4.4)
  under load — is the kmgr scope.
- The teardown that frees the GLOBAL allocators, the scratchpad pages, and the
  shared ext-isa ulib, drains the per-core `model_db`, and clears `tdrv_ctx_0`
  (the inverse of §1/§5/§6) is `tdrv_destroy`.
- The struct census these layouts feed is in
  [Host Execution-State Structs](../appendix/struct-exec-state-census.md).
