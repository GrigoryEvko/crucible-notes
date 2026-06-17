# Compute-Resource Build (KBL)

> *All addresses, offsets, sizes, frame/instruction counts, and call edges on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, ELF64 x86-64, **not** stripped, full DWARF, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). `.text` VMA == file offset, so every `0x3…`/`0x4…` is an analysis VMA verifiable against `nm -n libnrt.so`. DWARF roots every owned function under `/opt/workspace/KaenaRuntime/tdrv/{tdrv.c, ioq_switcher.c, kbin.c}`. Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF- and callgraph-anchored)** — every function address, `size`, `insn_count`, `block_count`, and `frame_size` is read from the IDA `functions.json` for this binary; struct offsets from `structures.json` cross-checked against decompiled field accesses; every call edge from the IDA call graph; every quoted string re-verified present in the binary. Boundary builders (`ddrs_build_dma_rings`, `ioqs_build_swap_data`) are named and their KBL-side contract pinned, but their interior is owned by [tdrv-dma-rings](../runtime/tdrv-dma-rings.md) and not re-derived. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

**KBL** is the `tdrv`-side facade (symbol prefix `kbl_`, source TU `tdrv/tdrv.c`) that turns a loaded, compiled NEFF — a tree of [KBIN](kbin-structs.md) records placed in device DRAM — into the per-execution **DMA plumbing** the NeuronCore engines actually run, and that owns the model's lifecycle on a physical core. Read it as the runtime's analogue of a dynamic linker's *bind* phase: the [load pipeline](load-pipeline.md) has already parsed the container, built the KBIN, and placed every tensor at a fixed HBM physical address; KBL is what binds the model's symbolic I/O variables to those addresses and emits the descriptor rings and pointer-swap descriptors that wire each tensor's HBM address into the per-engine DMA queues. The compute-resource build is the seam where a *loaded* model becomes an *executable* one.

Three responsibilities live here, and they map onto three lifecycle moments. **Load** is `kbl_model_add` (`@0x3058e0`, the 2907-byte master constructor): it `calloc`s the 7744-byte `model_t`, runs fourteen sub-builders, and extracts the input and output **feature-map sets** — name-keyed hashtables of the model's I/O tensors — via `kbl_get_fmap_set` (`@0x446960`). **Per-execution build** is `kbl_compute_build_compute_resources` (`@0x306790`, the hot-path core): per TPB it counts the model's dynamic queue-bundles and rings, then calls two boundary builders — `ddrs_build_dma_rings` (`@0x3179a0`, the DMA dynamic-ring-set builder) and `ioqs_build_swap_data` (`@0x446060`, the IO-queue switcher) — to produce the descriptor rings and the on-device pointer-rewrite descriptors that bind *this* execution's caller tensors. **Submit** is `kbl_compute_setup` (`@0x306fb0`): under the per-core `compute_req_lock` it stages the built resources into the hardware exec queue and hands back a `compute_idx`. **Kickoff** (`kbl_infer_kickoff` `@0x307320`) rings the doorbell and **stop**/**teardown** (`kbl_add_model_stop_request`, `kbl_model_remove` `@0x306440`, `model_free` `@0x3055f0`) tear the model down.

The single fact a reimplementer must internalize is that **tensors are bound by NAME, not by position**. A NEFF's DMA program is authored against symbolic `var_id`s; the loader records a `var_id → tensor-name` map (`io_mr_to_name_map`, `model_t+0x1A10`) and two `kbl_feature_map_set_t` hashtables (input at `model_t+0x1960`, output at `+0x1970`) keyed on a 256-char tensor name. At execution-resource build time, `ioqs_mr_var_id_to_pa` (`@0x445c00`) resolves a `var_id` to a name, picks input-vs-output by a per-mem-ref flag, looks the name up in the matching hashtable, and reads the tensor's physical address — and *that* PA is what gets baked into the swap descriptors `ioqs_build_swap_data` builds. The whole binding is a hashtable join on tensor names; this page owns that join, the per-TPB build sequence that drives it, and the build→setup→kickoff→stop state machine. It does **not** re-derive the ring/swap-descriptor interiors — those are owned by [tdrv-dma-rings](../runtime/tdrv-dma-rings.md), and KBL is documented here strictly as their caller.

For reimplementation, the contract is:

- **The per-TPB compute-resource build sequence** (`kbl_compute_build_compute_resources`): refcount-guard the model, count dynamic queue-bundles and rings, `calloc` the per-TPB `tdrv_compute_ioqs_resource_t`, drive `ddrs_build_dma_rings` then `ioqs_build_swap_data`, store the result in `tracker.ioqs_resources[tpb]`, decrement the refcount.
- **The feature-map NAME→tensor binding** — the `kbl_feature_map_set_t` 32-byte hashtable header, the node-`-1` trick that stores a tensor pointer one slot before the iterator node, and the `kbl_get_fmap_set` / `ioqs_mr_var_id_to_pa` lookup path that resolves a compiler `var_id` to an HBM physical address.
- **The compute-resource tracker** — `tdrv_compute_resources_t` (32 B) and the per-TPB `tdrv_compute_ioqs_resource_t` (40 B + flex array) that hold the built rings and swap descriptors between build and setup, plus the GC list that frees the transient swap blobs.
- **The model lifecycle state machine** — `kbl_model_add` (build) → `kbl_init_compute_resources` + `kbl_compute_build_compute_resources` (per-exec build) → `kbl_compute_setup` (stage) → `kbl_infer_kickoff` (doorbell) → `kbl_infer_exec_wait` (completion) → `kbl_add_model_stop_request` / `kbl_model_remove` (stop/teardown), and the `compute_idx_tail++` / `compute_idx_head` handshake that brackets one execution.

| | |
|---|---|
| **Source TUs** | `tdrv/tdrv.c` (lifecycle + drivers), `tdrv/ioq_switcher.c` (name→PA + swap data), `tdrv/kbin.c` (fmap-set extraction) |
| **Master constructor** | `kbl_model_add` `@0x3058e0` (2907 B, 609 insns, 82 BBs, 50 callees) — `calloc(0x1E40)` `model_t` |
| **Per-exec build core** | `kbl_compute_build_compute_resources` `@0x306790` (590 B, 139 insns, 13 BBs, 14 callees) |
| **Build caller** | `kmgr_exec_pre` `@0xdf820` (sole inbound edge `@0xdf99e`) |
| **Ring builder (boundary)** | `ddrs_build_dma_rings` `@0x3179a0` — [tdrv-dma-rings §2](../runtime/tdrv-dma-rings.md#2-ddrs_build_dma_rings--per-tpb-load-time-ring-construction) |
| **Swap builder (boundary)** | `ioqs_build_swap_data` `@0x446060` (2290 B) — IO-queue pointer/queue-base rewrite descriptors |
| **Name→PA resolver** | `ioqs_mr_var_id_to_pa` `@0x445c00` (216 B) — `var_id → name → fmap-set → tensor_get_pa` |
| **fmap-set extractor** | `kbl_get_fmap_set` `@0x446960` (362 B) — builds `kbl_fmap_set_t` from KBIN mem-refs |
| **fmap hashtable header** | `kbl_feature_map_set_t` — **32 B** + inline `ht_node_t[]`; input `@model_t+0x1960`, output `@+0x1970` |
| **Tracker** | `tdrv_compute_resources_t` — **32 B** (`{gc_tracker@0, ioqs_resources@0x10, ptpb_count@0x18}`) |
| **Per-TPB resource** | `tdrv_compute_ioqs_resource_t` — **40 B** + flex `dynamic_ring_sets[]` |
| **Submit staging** | `kbl_compute_setup` `@0x306fb0` (765 B) — `compute_req_lock`, `*compute_idx = compute_idx_tail++` |
| **Doorbell** | `kbl_infer_kickoff` `@0x307320` (240 B) → `exec_kickoff_infer` (IOCTL `INFERENCE_START`) |
| **Completion wait** | `kbl_infer_exec_wait` `@0x307410` (689 B) — spin on `compute_idx_head == compute_req_idx` |
| **Teardown** | `kbl_model_remove` `@0x306440` (521 B) → `model_free` `@0x3055f0` (740 B) |

---

## 1. The Per-TPB Compute-Resource Build

### Purpose

`kbl_compute_build_compute_resources` is the function the lane is named for: given a loaded `model_t` and the *caller's* input/output tensor sets for one execution, it produces — per physical TPB the model spans — the device-side DMA resources that bind those tensors and let the engines run. It is invoked once per execution from `kmgr_exec_pre` (`@0xdf820`, the only inbound edge, call site `@0xdf99e`), after `kbl_init_compute_resources` (`@0x306650`) has zeroed a `tdrv_compute_resources_t` tracker and `calloc`'d its per-TPB pointer array.

The function itself is *thin*: it does the bookkeeping (refcount guard, dynamic-bundle/ring counting, the per-TPB `calloc`) and then delegates the two heavy builds to boundary functions. `ddrs_build_dma_rings` materializes the model's dynamic descriptor rings against the caller tensors' physical addresses (with a PA-keyed LRU cache for reuse across executions — owned by [tdrv-dma-rings §2](../runtime/tdrv-dma-rings.md#2-ddrs_build_dma_rings--per-tpb-load-time-ring-construction)); `ioqs_build_swap_data` builds the **IO-queue switch** descriptors — the m2m copy descriptors that, at switch time, overwrite the on-device pointer slots and queue-base registers with this model/execution's physical addresses. KBL's contribution is the *driving loop and the per-TPB resource allocation*, not the descriptor formats.

### Entry Point

```text
kmgr_exec_pre (0xdf820)                                  ── per-execution pre-stage (KMGR)
  ├─ kbl_init_compute_resources (0x306650)               ── zero tracker; calloc(ptpb_count*8) ioqs_resources[]
  ├─ kbl_cc_get_model_is_first_exec (0x306a20)           ── decide collectives bootstrap (CC)
  ├─ kbl_exec_build_and_load_cc_resources (0x306e30)     ── [if CC] enc_setup_global_comm / enc_load_operations
  └─ kbl_compute_build_compute_resources (0x306790)      ── *** per-TPB DMA resource build ***
       ├─ get_model_ref_count (0x2274c0)                 ── pin the model_t (refcount++)
       ├─ dma_ring_count_dynamic_queue_bundles (0x22e280)── count dynamic bundles for this TPB
       ├─ dma_ring_count_dynamic_rings (0x22e160)        ── count dynamic rings (sizes the calloc)
       ├─ .calloc (0x3c8a0)                              ── tdrv_compute_ioqs_resource_t (8*nbundles+40)
       ├─ ddrs_build_dma_rings (0x3179a0)        [BOUNDARY] resolve templates → prings (PA-keyed LRU)
       ├─ ioqs_build_swap_data (0x446060)        [BOUNDARY] pointer / queue-base swap descriptors
       └─ model_ref_decrement (0x2275c0)                 ── unpin (refcount--), once per TPB iter
  (teardown) kbl_free_compute_resources (0x3066c0)       ── ddrs_free + ioqs_free_compute_resources per TPB
```

### Structures — the compute-resource tracker

The tracker is built by `kbl_init_compute_resources`, filled per TPB by the build, consumed by `kbl_compute_setup`, and freed by `kbl_free_compute_resources`. The per-TPB record is what `ddrs_build_dma_rings` / `ioqs_build_swap_data` write into.

| Struct | Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|---|
| `tdrv_compute_resources_t` (32 B) | `gc_tracker` | `+0x00` | `dmem_list_t` (16 B) | GC list for transient swap-descriptor blobs; `dmem_list_free` in teardown | HIGH |
| | `ioqs_resources` | `+0x10` | `tdrv_compute_ioqs_resource_t**` | `calloc(ptpb_count*8)` — one slot per TPB | HIGH |
| | `ptpb_count` | `+0x18` | `uint32_t` | number of physical TPBs the model spans | HIGH |
| `tdrv_compute_ioqs_resource_t` (40 B + flex) | `ioq_switch_tx_descs` | `+0x00` | `al_udma_desc*` | IO-queue switch TX descriptors (built by `ioqs_build_swap_data`) | HIGH |
| | `ioq_switch_rx_descs` | `+0x08` | `al_udma_desc*` | IO-queue switch RX descriptors | HIGH |
| | `ioq_switch_num_descs` | `+0x10` | `uint32_t` | descriptor count, consumed by `kbl_compute_setup` | HIGH |
| | `num_dynamic_rings` | `+0x18` | `size_t` | dynamic ring count (`dma_ring_count_dynamic_rings`) | HIGH |
| | `num_ring_sets` | `+0x20` | `size_t` | dynamic ring-set count | HIGH |
| | `dynamic_ring_sets` | `+0x28` | `dma_dynamic_ring_set_t*[]` | flex array, sized `8*num_ring_sets+40`; filled by `ddrs_build_dma_rings` | HIGH |

> **NOTE — the `calloc` is `8*nbundles + 40`, not `8*nbundles`.** The per-TPB `tdrv_compute_ioqs_resource_t` is a 40-byte header (`{tx_descs, rx_descs, num_descs, num_dynamic_rings, num_ring_sets}`) immediately followed inline by the `dynamic_ring_sets[]` pointer array. The allocation size folds the header and the flex tail into one `calloc(8*num_ring_sets + 40)` — the same header+flexible-array idiom every KBIN record uses ([kbin-structs](kbin-structs.md)). A reimplementer who allocates only `8*num_ring_sets` overwrites the five header fields when `ddrs_build_dma_rings` writes ring-set pointers at `+0x28`. (HIGH — `calloc` size and the `+0x28` flex base are DWARF-anchored against `structures.json`.)

### Algorithm — `kbl_compute_build_compute_resources`

```c
// kbl_compute_build_compute_resources @0x306790 — 590 B, 139 insns, 13 BBs, 14 callees.
// Per TPB: count dynamic bundles/rings, calloc the per-TPB resource, drive the two boundary
// builders, store the result. The model_t is RE-PINNED and unpinned inside the loop body.
function kbl_compute_build_compute_resources(pcore, h_model, input_set, output_set,
                                             /*in/out*/ tracker):
    for tpb in 0 .. tracker.ptpb_count:               // each physical TPB the model spans
        model = get_model_ref_count(h_model)           // 0x2274C0 — pin; NULL -> "Invalid model %u"
        if model == NULL: goto fail

        // (1) SIZE — how many dynamic bundles / rings this TPB's queue set carries
        nbundles  = dma_ring_count_dynamic_queue_bundles(model.qset, tpb)   // 0x22E280
        nrings    = dma_ring_count_dynamic_rings(model.qset, tpb)           // 0x22E160

        // (2) ALLOCATE the per-TPB resource: 40-B header + 8-B/ring-set flex tail (one calloc)
        res = calloc(1, 8*nbundles + 40)               // 0x3C8A0 -> "Failed to allocate dynamic_ring_set"
        if res == NULL: { model_ref_decrement(model); goto fail }
        res.num_dynamic_rings = nrings
        res.num_ring_sets     = nbundles

        // (3) BUILD THE DYNAMIC RINGS — resolve the model's template vrings against the
        //     caller tensors' physical addresses; PA-keyed LRU cache reuses identical placements.
        //     [BOUNDARY: ddrs_build_dma_rings owns the resolve/cache/dump; see tdrv-dma-rings §2]
        rc = ddrs_build_dma_rings(model.dmem_allocator, model.qset,
                                  model.io_mr_to_name_map,        // +0x1A10
                                  input_set, output_set,
                                  TONGA_DRAM, /*out*/ res)        // ring_mem_location
        if rc != NRT_SUCCESS:                          // "Failed to build dma rings for io"
            { model_ref_decrement(model); goto fail }

        // (4) BUILD THE IOQ-SWITCH SWAP DESCRIPTORS — the m2m copy descriptors that, at switch
        //     time, overwrite on-device pointer slots + queue-base registers with this model's PAs.
        //     ioqs_mr_var_id_to_pa (inside) resolves each var_id -> name -> fmap-set -> tensor PA.
        //     [BOUNDARY: ioqs_build_swap_data owns the swap-descriptor format; §2 here]
        rc = ioqs_build_swap_data(model, tpb, model.io_mr_to_name_map,
                                  input_set, output_set,
                                  model.num_hw_exec_queue_descs,  // +0x1A00
                                  /*out*/ res)
        if rc != NRT_SUCCESS:                          // "Failed to build ioq switching descriptors"
            { model_ref_decrement(model); goto fail }

        // (5) PUBLISH the per-TPB resource into the tracker, unpin the model
        tracker.ioqs_resources[tpb] = res
        model_ref_decrement(model)                     // 0x2275C0
    return NRT_SUCCESS
fail:
    return rc   // partial tracker is torn down by kbl_free_compute_resources (ddrs_free + ioqs_free)
```

> **QUIRK — the model is refcounted once *per TPB iteration*, not once for the whole build.** `get_model_ref_count` is called at the top of every loop body and `model_ref_decrement` at the bottom (and on every error exit), so a model spanning four TPBs takes and releases its refcount four times. The reason is that the `model_t` is resolved fresh from the registry by `h_model` each iteration — KBL never caches the `model_t*` across the loop — so the pin/unpin pairs with that resolution. A reimplementer who hoists the pin out of the loop and decrements once at the end will under-count if a concurrent `kbl_model_remove` races the build: the per-iteration pin is what keeps the model alive across each individual `ddrs_build_dma_rings` / `ioqs_build_swap_data` call. The shared `"Invalid model %u"` literal is the miss path, also used by `kbl_compute_setup` and `kbl_get_model_ref_count`. (HIGH — three `model_ref_decrement` edges and one `get_model_ref_count` edge in the call graph, matching the per-iter + per-error-path placement.)

> **GOTCHA — the two builders are a boundary; KBL passes them the tracker and the name map, and reads back rings + descriptors.** `ddrs_build_dma_rings` and `ioqs_build_swap_data` are the only two non-trivial callees, and both are owned by [tdrv-dma-rings](../runtime/tdrv-dma-rings.md) / the IO-queue switcher TU. What KBL guarantees them is precisely: a pinned `model_t`, the `io_mr_to_name_map` (so they can turn `var_id`s into names), the caller's `input_set`/`output_set` (so they can turn names into tensors and PAs), the ring-memory location (`TONGA_DRAM`), and a zero-initialized per-TPB `res` to fill. What KBL reads back is `res.dynamic_ring_sets[]` (the prings) and `res.{ioq_switch_tx_descs, ioq_switch_rx_descs, ioq_switch_num_descs}` (the swap descriptors). A reimplementer must not re-derive the ring resolve or the swap-descriptor encoding here — that is the same layering the page's evidence-grade note pins. (HIGH — callees list `@0x3179a0` and `@0x446060` and nothing else heavy.)

### Function Map — the build

| Function | Addr | Frame / Insns | Role | Confidence |
|---|---|---|---|---|
| `kbl_init_compute_resources` | `0x306650` | 24 / 29 | zero `tracker`; `calloc(ptpb_count*8)` the `ioqs_resources[]` array; `NRT_RESOURCE` on OOM | HIGH |
| `kbl_compute_build_compute_resources` | `0x306790` | 104 / 139 | per-TPB: count → `calloc` → `ddrs_build_dma_rings` → `ioqs_build_swap_data` → store | HIGH |
| `kbl_free_compute_resources` | `0x3066c0` | 56 / 60 | `dmem_list_free(gc_tracker)`; per-TPB `ddrs_free` + `ioqs_free_compute_resources`; free array | HIGH |
| `ddrs_build_dma_rings` | `0x3179a0` | 232 / 717 | [BOUNDARY] resolve template vrings → prings; PA-keyed LRU cache — [tdrv-dma-rings §2](../runtime/tdrv-dma-rings.md#2-ddrs_build_dma_rings--per-tpb-load-time-ring-construction) | HIGH |
| `ioqs_build_swap_data` | `0x446060` | 168 / 500 | [BOUNDARY] build scalar-ptr / ptr-table / queue-base swap descriptors + final event-set desc | HIGH |
| `ioqs_free_compute_resources` | `0x446030` | 8 / 13 | [BOUNDARY] free `ioq_switch_{tx,rx}_descs` on the per-TPB resource | HIGH |
| `ddrs_free` | `0x317750` | 24 / 38 | [BOUNDARY] refcount `-1`; free each ring-set's prings + `dmem_t` at 0 | HIGH |
| `get_model_ref_count` / `model_ref_decrement` | `0x2274c0` / `0x2275c0` | — | [BOUNDARY db.c] pin / unpin the `model_t` by `h_model` (per loop iteration) | HIGH |
| `dma_ring_count_dynamic_queue_bundles` / `_rings` | `0x22e280` / `0x22e160` | — | [BOUNDARY dma_ring.c] count dynamic bundles / rings to size the `calloc` | HIGH |

---

## 2. The Feature-Map NAME → Tensor Binding

### Purpose

The binding is the conceptual heart of the page: a NEFF's DMA program never names a tensor by an absolute address or an ordinal slot — it names a compiler **`var_id`**, and the runtime must resolve that to the physical address where *this load* placed the tensor. KBL does it with three layered structures, all keyed on a tensor **name** (a 256-char string): the per-model `io_mr_to_name_map` (`model_t+0x1A10`, `var_id → name`), and the two `kbl_feature_map_set_t` hashtables `input_fmap_set` (`+0x1960`) and `output_fmap_set` (`+0x1970`) (`name → kbl_fmap_t`). The model's *own* fmap sets are built once at load by `kbl_get_fmap_set`; the *caller's* per-execution tensor sets (the user's `nrt_tensor_set`) are the same hashtable shape, and `ioqs_mr_var_id_to_pa` is the join that turns a `var_id` into an HBM PA by chaining all three.

This is the same name-keyed-hashtable machinery the execute path uses to shadow-clone and copy host tensors ([the `kbl_feature_map_set_t` family in the execute lane](../exec/submit-path.md)); here the focus is the *load-time extraction* (`kbl_get_fmap_set`) and the *resolve* (`ioqs_mr_var_id_to_pa`) that together close the var_id → PA loop the swap-descriptor builder depends on.

### Structures — the name-keyed hashtable

| Struct | Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|---|
| `kbl_feature_map_set_t` (32 B + nodes) | `size` | `+0x00` | `uint64_t` | bucket-array length (init `0x400` for caller sets) | HIGH |
| | `size_mask` | `+0x08` | `uint64_t` | `size - 1`, the bucket index mask | HIGH |
| | `count` | `+0x10` | `uint64_t` | live entries | HIGH |
| | `free_node_fn` | `+0x18` | `HT_FOR_EACH` | per-node free callback (`= kbl_fmap_tensor_free` for model sets) | HIGH |
| | `nodes` | `+0x20` | `ht_node_t[]` | inline bucket array | HIGH |
| `ht_node_t` (48 B) | `key` | `+0x00` | `uint64_t` | hashed key | HIGH |
| | *(union)* | `+0x08` | 16 B | name / key payload | HIGH |
| | `buf_size` | `+0x10` | `size_t` | per-key buffer size | HIGH |
| | `bucket` | `+0x18` | `uint64_t` | bucket linkage (== tensor size in some paths) | HIGH |
| | `key_type` | `+0x20` | `ht_key_type_t` | asserted `HT_KEY_TYPE_NAME` in the copy/clone/ref paths | HIGH |
| | `next` | `+0x28` | `ht_node_t*` | **also the tensor-pointer slot** (`node[-1].next`, see QUIRK) | HIGH |
| `kbl_fmap_set_t` (16 B) | `fmap` | `+0x00` | `kbl_fmap_t*` | array (the model's IO descriptor) | HIGH |
| | `nfmap` | `+0x08` | `uint32_t` | element count | HIGH |
| `kbl_fmap_t` (272 B) | `name` | `+0x00` | `char[256]` | tensor name (the hashtable key) | HIGH |
| | `size` | `+0x100` | `uint64_t` | minimum required tensor byte size | HIGH |
| | `mr_idx` / `var_id` | `+0x108` | `uint32_t` | the compiler `var_id` this descriptor binds (MED: field-name `mr_idx` vs `var_id`) | MED |

> **QUIRK — the tensor pointer lives one node slot *before* the iterator node (`node[-1].next`).** The hashtable's free callback `kbl_fmap_tensor_free` (`@0x304fe0`, 20 B, the smallest function in the lane) reads `node[-1].next` as the `nrt_tensor_t*` to free: the feature-map entry stores its tensor pointer in the `next` field of the node *one 48-byte slot before* the node the iterator hands back. This is the `kbl_feature_map_t` (56 B) packing — `{tensor@0, ht_node@8}` — observed from the `node-1` trick: an entry is `{tensor_ptr, ht_node_t}`, so the iterator's node and the tensor slot are at a fixed `-48` byte delta. A reimplementer who treats `node.next` as the tensor pointer (instead of `node[-1].next`) reads the next bucket link as a tensor pointer and frees garbage. (HIGH — `kbl_fmap_tensor_free` body reads `node[-1].next`; the `kbl_feature_map_t` 56-B layout is consistent with it.)

### Algorithm — extract the model's fmap set at load (`kbl_get_fmap_set`)

```c
// kbl_get_fmap_set @0x446960 — 362 B, 94 insns, 16 BBs. Called TWICE by kbl_model_add (@0x305eb5
// input, @0x305edc output) to populate model_t.input_fmap_set / output_fmap_set from KBIN mem-refs.
// Two passes over the kbin_mem_ref_set_t: count matching refs, then alloc + copy. Direction is
// encoded as an mr_type: is_input -> 5, else 6 (the "6 - is_input" idiom).
function kbl_get_fmap_set(kbin_mem_ref_set, is_input, /*out*/ fmap_set):
    want_type = is_input ? 5 : 6                       // KBIN mr_type selecting IN vs OUT tensors

    // (1) COUNT — how many mem-refs are I/O tensors of this direction
    n = 0
    for each mr in kbin_mem_ref_set.mem[]:             // stride 152 (kbin_mem_ref)
        if mr.mr_type == want_type: n++

    // (2) ALLOCATE the kbl_fmap_t[] (272 B stride) and copy name/size/var_id
    fmap_set.fmap  = calloc(n, 272)                    // -> "Failed to allocate fmap_t for %u variables"
    fmap_set.nfmap = n
    j = 0
    for each mr in kbin_mem_ref_set.mem[]:
        if mr.mr_type != want_type: continue
        strncpy(fmap_set.fmap[j].name, mr.name, 255)   // +0x000  the binding key
        fmap_set.fmap[j].size   = mr.size              // +0x100  minimum tensor size
        fmap_set.fmap[j].mr_idx = mr.var_id            // +0x108  the compiler var_id
        j++
    return NRT_SUCCESS
```

### Algorithm — resolve a var_id to an HBM PA (`ioqs_mr_var_id_to_pa`)

```c
// ioqs_mr_var_id_to_pa @0x445c00 — 216 B, 68 insns, 8 BBs. The var_id -> name -> tensor -> PA join,
// called from ioqs_build_swap_data while emitting each scalar-pointer swap descriptor. The caller
// tensor sets (input/output) are name-keyed kbl_feature_map_set_t hashtables built per execution.
function ioqs_mr_var_id_to_pa(io_mr_to_name_map, input_set, output_set, var_id, /*out*/ pa):
    // (1) var_id -> tensor name (the model's load-time io-mr map; model_t+0x1A10)
    node = ht_find(io_mr_to_name_map, var_id)          // 0x267E10
    if node == NULL: return NRT_FAILURE                // "Failed to find io name for mr %u"
    name    = node.name
    is_input = node.is_input_flag                       // per-node direction picks the set

    // (2) name -> tensor (pick input vs output hashtable by the per-mem-ref flag)
    set    = is_input ? input_set : output_set
    tensor = ht_name_find(set, name)                    // 0x268000
    if tensor == NULL: return NRT_FAILURE              // "Failed to find fmap for tensor %s"

    // (3) tensor -> physical address (this PA is baked into the swap descriptor)
    *pa = tensor_get_pa(tensor)                         // 0x30FAE0  [BOUNDARY tensor.c]
    return NRT_SUCCESS
```

> **GOTCHA — the direction flag is per-mem-ref, not per-call; a single swap-data build mixes input and output lookups.** `ioqs_mr_var_id_to_pa` is called once per scalar pointer that `ioqs_build_swap_data` must rewrite, and the input-vs-output decision is read from the `io_mr_to_name_map` *node* (`is_input` flag), not from a parameter — one model's swap build resolves some `var_id`s against the input set and others against the output set within the same loop. A reimplementer that hard-codes "all swap pointers come from the input set" mis-resolves every output tensor's PA, and the model writes inference results to the wrong HBM addresses with no error (the lookup *succeeds*, against the wrong table). The two miss strings (`"Failed to find io name for mr %u"`, `"Failed to find fmap for tensor %s"`) distinguish a missing name-map entry from a missing tensor in the caller's set. (HIGH — `ioqs_mr_var_id_to_pa` reads the per-node flag and selects `input_set`/`output_set` accordingly.)

> **NOTE — the model's own fmap sets and the caller's tensor sets are the same hashtable shape but different lifetimes.** `kbl_get_fmap_set` builds `model_t.{input,output}_fmap_set` (`kbl_fmap_set_t`, the `{kbl_fmap_t*, nfmap}` descriptor arrays) *once at load* — these are the *contract* (names + sizes + var_ids the model expects), freed by `kbl_free_fmap_set` (`@0x446ad0`) at `kbl_model_remove`. The *caller's* `input_set`/`output_set` passed to the build are `kbl_feature_map_set_t` hashtables of *actual* `nrt_tensor_t`s for one execution, validated against the contract by `kbl_model_verify_io_tensor_set` (`@0x307db0`) before the build runs. The resolve in step (2) above hits the *caller's* hashtables; the contract sets gate validity, not addresses. (HIGH — `kbl_get_fmap_set` callers are `kbl_model_add` ×2; `kbl_model_verify_io_tensor_set` walks `model_t.{input,output}_fmap_set` against the caller set.)

### Function Map — the binding

| Function | Addr | Frame / Insns | Role | Confidence |
|---|---|---|---|---|
| `kbl_get_fmap_set` | `0x446960` | 72 / 94 | build `model_t.{input,output}_fmap_set` from KBIN mem-refs (load time, ×2) | HIGH |
| `kbl_free_fmap_set` | `0x446ad0` | 8 / 10 | `free(fmap_set.fmap)`; zero `{fmap, nfmap}` (teardown) | HIGH |
| `ioqs_mr_var_id_to_pa` | `0x445c00` | 56 / 68 | `var_id → name → tensor → PA` join (per swap descriptor) | HIGH |
| `kbl_fmap_tensor_free` | `0x304fe0` | 8 / 6 | hashtable free callback; frees `node[-1].next` as the tensor pointer | HIGH |
| `kbl_init_feature_map_set` | `0x307d90` | 0 / 3 | `ht_init(set, 0x400 buckets, free_fn)` for a caller tensor set | HIGH |
| `kbl_free_feature_map_set` | `0x307d50` | 16 / 14 | `ht_for_each(kbl_fmap_tensor_free)` if owning, then `ht_destroy`; NULL-safe | HIGH |
| `kbl_add_fmap_to_set` | `0x307c70` | 72 / 66 | insert/overwrite a named tensor into a caller set (`calloc(0x38)` `kbl_feature_map_t`) | HIGH |
| `kbl_create_reference_feature_map_set` | `0x308420` | 104 / 118 | clone a set by tensor *reference* (per-execution shadow set) | HIGH |
| `kbl_model_verify_io_tensor_set` | `0x307db0` | 88 / 110 | validate a caller set against the model's fmap contract (name + size) | HIGH |
| `ht_find` / `ht_name_find` / `tensor_get_pa` | `0x267e10` / `0x268000` / `0x30fae0` | — | [BOUNDARY] hashtable lookups + tensor PA read | HIGH |

---

## 3. The Model Lifecycle State Machine

### Purpose

KBL owns the model's whole life on a physical core, from the `calloc` of the 7744-byte `model_t` to its `free`. The build of §1 and the binding of §2 sit in the middle of a six-state progression that a reimplementer must drive in order: a model is **built** once, then per execution it is **resource-built**, **staged**, **kicked off**, **waited on**, and finally **stopped** and **torn down**. The `compute_idx` produced by staging and consumed by the wait is the handshake that makes one execution a discrete, ordered unit in the per-core hardware exec queue.

### The state machine

```text
LOAD (once per model, per physical core)
  kbl_model_add (0x3058e0) ── calloc(0x1E40) model_t; 14 sub-builders; kbl_get_fmap_set ×2
        │                      (input @model_t+0x1960, output @+0x1970)
        v
PER-EXECUTION BUILD (kmgr_exec_pre, once per inference)
  kbl_init_compute_resources (0x306650) ── tracker + ioqs_resources[ptpb_count]
        │
  kbl_compute_build_compute_resources (0x306790) ── per TPB: ddrs_build_dma_rings + ioqs_build_swap_data
        v                                            (§1; binds caller tensors by NAME, §2)
SUBMIT / STAGE (dlr_add_to_hw_exec_queue)
  kbl_compute_setup (0x306fb0) ── [compute_req_lock] per-TPB hw_exec_queue_add_exec_request
        │                          (feeds the built tx/rx swap descs + count)
        │                          *compute_idx = compute_idx_tail++          ◄── ticket issued
        v
KICKOFF (dlr_kickoff_exec)
  kbl_infer_kickoff (0x307320) ── exec_kickoff_infer → IOCTL INFERENCE_START (doorbell)
        v
COMPLETION (kmgr_exec_wait)
  kbl_infer_exec_wait (0x307410) ── spin: compute_idx_head == compute_req_idx, usleep(1)
        │                            until param.timeout → exec_wait_round_robin (NQ poll)
        v                            else NRT_TIMEOUT "...after %u us timeout"
STOP / TEARDOWN
  kbl_add_model_stop_request (0x308bf0) ── hw_exec_queue_add_model_stop_request
  kbl_model_remove (0x306440) ── remove_model; enc_semaphore_check; kbl_free_fmap_set ×2
        │                        enc_free_context; tdrv_scratchpad_cleanup
        v
  model_free (0x3055f0) ── per queue-bundle free rx/tx rings + vring_set + ddrs_cache; dbtc/dve/
                           ucode/profile/kbin_patch/mr_ptr_vars/io maps; dmem_allocator_destroy; free(mod)
```

### Algorithm — submit staging and the compute_idx ticket

```c
// kbl_compute_setup @0x306fb0 — 765 B, 180 insns, 22 BBs. Stage the built per-TPB resources into
// the hardware exec queue under the per-core compute_req_lock, and issue this execution's ticket.
function kbl_compute_setup(pcore, h_model, tracker, /*out*/ compute_idx):
    main_tpb = db_physical_core_get_mla_and_tpb(pcore)   // "Failed to find tpb for ND %u NC %u"
    if !lock(main_tpb.hw_exec_queue.compute_req_lock):   // "Failed to take compute req lock"
        return NRT_FAILURE

    for tpb in 0 .. tracker.ptpb_count:                  // each TPB gets its swap descriptors staged
        res = tracker.ioqs_resources[tpb]
        rc = hw_exec_queue_add_exec_request(tpb.hw_exec_queue,
                                            res.ioq_switch_tx_descs,
                                            res.ioq_switch_rx_descs,
                                            res.ioq_switch_num_descs)   // "Failed to add execution request"
        if rc != NRT_SUCCESS: { unlock(...); return rc }

    if tpb0_owns_collectives:                             // first TPB drives the CC proxy
        kbl_exec_cc_enq_proxy_tasks(model, exec_id)       // 0x306FA0 -> enc_enq_proxy_tasks

    *compute_idx = main_tpb.hw_exec_queue.compute_idx_tail++   // ◄── monotonic ticket; paired with the wait
    unlock(main_tpb.hw_exec_queue.compute_req_lock)
    return NRT_SUCCESS

// kbl_infer_exec_wait @0x307410 — 689 B, 154 insns. Block until the hardware retires this ticket.
function kbl_infer_exec_wait(pcore, h_model, compute_req_idx, /*out*/ output_info):
    tpb = db_physical_core_get_mla_and_tpb(pcore)
    deadline = now() + model.param.timeout                // model_t.param +0x1920
    while tpb.hw_exec_queue.compute_idx_head != compute_req_idx:   // not yet our turn
        if now() > deadline:
            return NRT_TIMEOUT                            // "...after %u us timeout"
        usleep(1)
    exec_wait_round_robin(tpb, /*out*/ output_info)       // NQ poll -> kbl_output_info_t (64 B)
    return NRT_SUCCESS
```

> **QUIRK — `compute_idx_tail++` issues the ticket; `compute_idx_head` retires it; the wait spins on their equality.** Staging increments a per-core `compute_idx_tail` and returns the *pre-increment* value as the caller's `compute_idx` — a monotonic ticket. `kbl_infer_exec_wait` then spins (`usleep(1)`) until the hardware's `compute_idx_head` catches up to that exact value, i.e. until every earlier-submitted execution on the core has retired and this one is current. This is a single-producer ordered queue: tickets are handed out under `compute_req_lock` (so they are dense and ordered) and consumed in order by the hardware. A reimplementer who lets two executions on the same core share or reorder their `compute_idx` breaks the head/tail handshake and either deadlocks the wait (head never equals a skipped ticket) or returns the wrong execution's `kbl_output_info_t`. The wait is bounded by `model_t.param.timeout` (`+0x1920`) and on expiry returns `NRT_TIMEOUT` rather than blocking forever. (HIGH — `kbl_compute_setup` issues `compute_idx_tail++` under the lock; `kbl_infer_exec_wait` spins on `compute_idx_head == compute_req_idx`.)

> **NOTE — `kbl_model_add` is the build phase; this page documents its *output contract*, not its 14-step interior.** The master constructor (`@0x3058e0`, 2907 B, 50 callees) runs `dbtc_storage_init`, `drs_create_data_refill_rings`, `io_init_mr_to_name_map` / `io_build_mr_to_name_lookup` (which populate the `io_mr_to_name_map` §2 depends on), `mr_pointer_init_pointer_vars`, `io_create_queues`, `act_local_storage_tbls_init`, `dve_dynamic_config_init`, `sequencer_setup_instr`, `dma_ring_setup_queue_bundles`, `add_model`, and the two `kbl_get_fmap_set` calls — the full NEFF→device build pipeline owned by [the load pipeline](load-pipeline.md). The relevant outputs for this page are the three structures the per-exec build consumes: the `io_mr_to_name_map` (`+0x1A10`), the two fmap sets (`+0x1960`/`+0x1970`), and the `dma_queue_set_t` (`+0x110`) whose dynamic bundles `kbl_compute_build_compute_resources` counts. (HIGH — `kbl_get_fmap_set` callers are exactly the two `kbl_model_add` sites `@0x305eb5`/`@0x305edc`.)

### Function Map — the lifecycle

| Function | Addr | Frame / Insns | Role | Confidence |
|---|---|---|---|---|
| `kbl_model_add` | `0x3058e0` | 312 / 609 | master constructor: `calloc(0x1E40)` `model_t`; 14 sub-builders; `kbl_get_fmap_set` ×2 | HIGH |
| `kbl_compute_setup` | `0x306fb0` | 136 / 180 | stage built resources under `compute_req_lock`; `*compute_idx = compute_idx_tail++` | HIGH |
| `kbl_infer_kickoff` | `0x307320` | 40 / 65 | doorbell: `exec_kickoff_infer` → IOCTL `INFERENCE_START`; `ntrace` timestamp | HIGH |
| `kbl_infer_exec_wait` | `0x307410` | 248 / 154 | spin on `compute_idx_head == req_idx` until `param.timeout`; `exec_wait_round_robin` NQ poll | HIGH |
| `kbl_exec_cc_enq_proxy_tasks` | `0x306fa0` | 0 / 2 | thunk → `..._internal` → `enc_enq_proxy_tasks` (collectives proxy, tpb0) | HIGH |
| `kbl_model_remove` | `0x306440` | 56 / 133 | `remove_model`; `enc_semaphore_check` gate; `kbl_free_fmap_set` ×2; `enc_free_context` → `model_free` | HIGH |
| `model_free` | `0x3055f0` | 104 / 167 | full `model_t` teardown: rings, vrings, ddrs cache, DVE/ucode/profile, allocator, `free(mod)` | HIGH |
| `hw_exec_queue_add_exec_request` | `0x321420` | — | [BOUNDARY hw_exec_queue.c] feed the staged tx/rx swap descriptors into the queue | HIGH |
| `exec_kickoff_infer` / `exec_wait_round_robin` | `0x2632e0` / `0x2655c0` | — | [BOUNDARY exec.c] doorbell IOCTL / NQ-poll completion | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `kbl_model_add` (`@0x3058e0`) | the master constructor that produces the `model_t`, the `io_mr_to_name_map`, and the two fmap sets this page's build consumes |
| `ddrs_build_dma_rings` (`@0x3179a0`) | [BOUNDARY] the dynamic-ring builder KBL drives per TPB; owns the resolve/cache/dump |
| `ioqs_build_swap_data` (`@0x446060`) | [BOUNDARY] the IO-queue switcher KBL drives per TPB; owns the swap-descriptor formats |
| `kbl_feature_map_set_t` (32 B) | the name-keyed tensor hashtable that binds `var_id`s to physical addresses |
| `tdrv_compute_resources_t` / `tdrv_compute_ioqs_resource_t` | the per-execution tracker carrying built rings + swap descriptors from build to setup |
| `kmgr_exec_pre` (`@0xdf820`) | the sole caller of the per-exec build; the KMGR side of the seam |
| `hw_exec_queue` / `compute_idx_{head,tail}` | the per-core ordered queue the staging issues tickets into and the wait retires |

## Cross-References

- [KBIN Structures](kbin-structs.md) — the `kbin_mem_ref` table `kbl_get_fmap_set` walks (`mr_type` 5/6 → IN/OUT) and the `kbin_dma_t` rings whose templates `ddrs_build_dma_rings` resolves; the loaded binary this builder consumes
- [The Load Pipeline (Parse → Build → Stage → Relocate)](load-pipeline.md) — the five-act loader that runs `kbl_model_add`; the `io_mr_to_name_map` / fmap-set / `dma_queue_set_t` outputs this page's build depends on
- [TDRV: DMA Descriptor Rings and Swap-Queue](../runtime/tdrv-dma-rings.md) — the `ddrs_build_dma_rings` ring builder and `dma_ring_info_t` prings KBL passes the tracker to; the boundary this page deliberately does not re-derive
- [The Submit Path: Bind → Stage → Doorbell](../exec/submit-path.md) — the execute-lane side: how `kbl_compute_setup`'s staged resources and `compute_idx` feed the `device_exec_request_t` build and the doorbell IOCTL
- [back to index](../index.md)
