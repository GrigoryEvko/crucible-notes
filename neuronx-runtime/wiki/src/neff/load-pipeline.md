# The Load Pipeline (Parse → Build → Stage → Relocate)

> *All addresses, offsets, sizes, and struct ordinals on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA; BSS globals are cited by their `_ZL` symbol. Source asserts name `/opt/workspace/KaenaRuntime/{kmgr/dlr.cpp, kmgr/dlr_kelf.cpp, kelf/*, tdrv/*}`. Other versions will differ.*
> *Evidence grade: **Confirmed (symbol- and callgraph-anchored)** — every stage entry is an `nm`-confirmed symbol; the build-step ordering is read from the IDA call graph (`kbl_model_add` out-edges in source order); struct sizes are verbatim from `structures.json`. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

`nrt_load` is the runtime's **loader-linker**: it takes a NEFF byte blob and returns an executable model bound to one or more physical NeuronCores. Read it the way you read a dynamic linker turning an ELF into a running image — *parse the container into sections, build the per-core resource objects, place every section at a fixed device address, relocate the references that point between them, and hand back a handle*. The Neuron loader runs that same five-act play, but the "sections" are TAR members and JSON manifests, the "image base" is a device-DRAM (HBM) carve-out allocated by an `IOCTL`, and the "relocations" are compiler var-ids rewritten to physical addresses inside a TPB instruction stream and relative DMA-ring pointers rewritten to absolute device physical addresses.

The pipeline is **six stages** and almost entirely userspace: the first four stages — public-API guard, container parse, JSON/KELF graph parse, and KBIN build — touch **no device and fire no `IOCTL`**. The device boundary opens at stage 4 (staging), where each subgraph's state-buffer/HBM carve-out is allocated by `MEM_ALLOC_V2MT64` and weights/tables/instructions are DMA'd in. The central seam of the whole pipeline is `dlr_kelf_stage` (`@0xe0970`) — the function where the parse-side world (a `kelf_nn_t` of compiler-built `kbin` records) crosses into the device-side world (a per-physical-core `model_t`, one per subgraph). On a multi-NeuronCore (logical-NeuronCore, LNC) load that seam fans out **one POSIX thread per subgraph**, up to 256, each running the full device build on its own core in parallel.

This page is the **integration view**: it documents the end-to-end flow and the seams *between* stages, and deliberately does **not** re-derive the byte-level detail each stage owns — the 1024-byte header and tar walk belong to [NEFF Container](container.md), the `mem_ref` placement plan to [Static Memory Planning](memory-planning.md), the JSON→KBIN lowering to [kelf2kbin](kelf2kbin.md), and the per-core resource objects to [Compute-Resource Build](compute-resource-build.md). The value here is the staged flow, the three model records the loader juggles, the two-handle id space, the process-global parse cache, and the precise point where relocation happens.

For reimplementation, the contract is:

- **The six-stage sequence** and which functions own each stage — and the hard fact that stages 0–3 are `IOCTL`-free, with the device boundary opening only at `dlr_kelf_stage → kbl_model_add → dmem_alloc(MEM_ALLOC_V2MT64)`.
- **The three model records** — `nrt_model_t` (284 B, returned to the caller), `dlr_model` (480 B, the userspace registry value), and `model_t` (7744 B, one per physical core) — and the `dlr_kelf_model_t` (72 B) bridge that links them.
- **The two-handle id space** — `H_NN` (parse-side registry key, `dlr_model_set.last_nn_id++`) versus `H_MODEL` (device-side per-core key, `tdrv_get_unique_h_model()`) — and how `dlr_kelf_model_t.h_model` joins them.
- **The TNC parse cache** — the process-global single-slot UUID-keyed 4-state machine that lets concurrent loaders of the *same* NEFF share one parse result.
- **The relocation model** — that "relocation" is **not** one monolithic pass but two sub-layers run at stage time: var-id→physical-address resolution inside the instruction translator (recording instruction-IP patch deltas) and relative→absolute DMA-ring `buf_ptr` rewriting.

| | |
|---|---|
| **Public entry** | `nrt_load` `@0xa9fe0` (+ `nrt_load_collectives` `@0xaa4c0`) → `nrt_load_util` `@0xa9920` |
| **Load orchestrator** | `kmgr_load_nn_nc` `@0xde280` (5322 B, 1025 insns, 105 callees — the centerpiece) |
| **Parse → stage seam** | `dlr_kelf_stage` `@0xe0970` — KMGR↔KBL boundary; calloc(`0x48`) `dlr_kelf_model_t` |
| **Per-core build+stage** | `kbl_model_add` `@0x3058e0` (calloc(`0x1E40`) `model_t`; full build pipeline) |
| **Device boundary** | first `IOCTL` = `MEM_ALLOC_V2MT64` (#102, `0x80384E66`) inside `kbl_model_add` |
| **Caller→returned record** | `nrt_model_t` — 284 B (`0x11C`, ord 13480), `calloc` in `nrt_load_util` |
| **Userspace registry value** | `dlr_model` — 480 B (`0x1E0`, ord 13259), in `dlr_model_set.models[h_nn]` |
| **Per-physical-core record** | `model_t` — 7744 B (`0x1E40`), one per kbin/subgraph |
| **Parse↔stage bridge** | `dlr_kelf_model_t` — 72 B (`0x48`, ord 17521) |
| **Parse cache (TNC)** | `tnc` `@0xc5d880`; value `neff_load_info_t` 112 B (ord 13443); lock `tnc_lock` `@0xc5d840` |
| **Model registry** | `dlr_model_set` `@0xc5c720` — `unordered_map<uint32,dlr_model*>` + rwlock + `last_nn_id` |
| **Ready state** | `dlr_model.state(+0x1A8) = 4` (READY) on success |

---

## Stage Pipeline

The six stages, with the owning function and its entry address at each arrow. Stages 0–3 are pure userspace; the device-DRAM boundary opens inside stage 4.

```text
                            ┌──────────────── 100% userspace, no IOCTL ──────────────┐
caller (libneuronpjrt …)    │                                                        │   ┌── device DRAM ──┐
        │                   v                                                        v   v                 │
        │   S0 GUARD    S1 PARSE        S2 JSON/KELF       S3 KBL BUILD     S4 STAGE     S5 RELOCATE+READY   │
        v   ─────────   ──────────     ──────────────     ────────────     ─────────    ─────────────────  │
  nrt_load ──▶ nrt_load_util ──▶ neff_parse ──▶ parse_neff_json ──▶ gen_kbin ──▶ dlr_kelf_stage ──▶ (in S4) │
   0xa9fe0      0xa9920          0x4ca3f0       0xe1d10            0x4af930      0xe0970                      │
        │          │                │           dlr_kelf_load        │            │                         │
        │   kmgr_load_nn_nc         │            0xe0830     construct_kbin  kbl_model_add ──▶ MEM_ALLOC_V2MT64
        │     0xde280  ────────────▶│           kelf_load            0x496d50    0x3058e0       (#102)       │
        │   (orchestrates S1..S5)   │            0x49a6b0    kelf_load_from_neff   │  dmem_buf_copyin        │
        │          │            TNC cache         kelf::kelf::load   0x4c0870      │    0x229820  ──────────▶│
        │          │            tnc@0xc5d880       0x497dc0          │            │                         │
        │          │                │                │              │      sequencer_setup_instr 0x4483d0   │
        │          │                │                │              │            │  └▶ itf_setup_functions  │
        │          │                │                │              │            │       └▶ translate_*  ┐   │
        │          │                │                │              │            │       └▶ imcpy_load_vring │
        │          │                │                │              │            │            0x31bc10   │   │
        │   register dlr_model in dlr_model_set[h_nn]; state=4 (READY) ◀────────────────── relocate ◀───┘   │
        ▼                                                                                                   │
  nrt_model_t (284 B) returned to caller ◀── h_nn ── dlr_model (480 B) ── h_model ── model_t (7744 B)/core ─┘
```

The whole sequence is driven from inside one function, `kmgr_load_nn_nc` (`@0xde280`): S0 calls into it, and it inlines the TNC machine (S1's cache), calls `neff_parse` (S1), `parse_neff_json`/`dlr_kelf_load` (S2), `dlr_kelf_stage` (S4, which internally runs S3's `kbl_model_add` and S5's relocation), then registers the `dlr_model` and sets it READY (S5). A reimplementer should treat `kmgr_load_nn_nc` as the master state machine and the other entries as its phase callbacks.

---

## The Three Model Records and Two Handles

### Purpose

A reimplementer's first confusion is that "the model" is **three** different structs of three different sizes, plus a bridge, plus two distinct id spaces. Getting the relationship wrong scrambles every downstream lookup. This section pins the object graph before any stage detail, because every later stage reads or writes one of these records.

### The object graph

A single load produces one `nrt_model_t` (the public handle), one `dlr_model` (the userspace registry entry), and **one `model_t` per subgraph/physical core** (the device record). They are chained by the two handles:

```text
nrt_model_t (284 B)            dlr_model (480 B)               dlr_kelf_model_t (72 B)        model_t (7744 B) × nkbin
  .h_nn  ───────────────▶  dlr_model_set.models[h_nn]            (the parse↔stage bridge)        (one per physical core)
  .name                       .kelf_model(+0x1A0) ───────────▶  .h_model(+0x00) ──────────▶  tdrv model db lookup
  .uuid                       .state(+0x1A8) = 4 (READY)          .vcore / .sg_count            .h_model(+0x18F0)
  .vnc_idx                    .ref_count(+0x1B0)                  .uuid / .params
```

| Record | Size | Ordinal | Allocated by | Lifetime / role |
|---|---|---|---|---|
| `nrt_model_t` | 284 B (`0x11C`) | 13480 | `calloc` in `nrt_load_util` `@0xa9920` | returned to caller; freed in `nrt_unload`. Carries `vnc_idx`, `name`, `uuid`, `gid`, and `h_nn` |
| `dlr_model` | 480 B (`0x1E0`) | 13259 | `operator new(0x1E0)` in `kmgr_load_nn_nc` | userspace registry value in `dlr_model_set.models[h_nn]`; holds IO maps, ref-count, state, `kelf_model` bridge |
| `dlr_kelf_model_t` | 72 B (`0x48`) | 17521 | `calloc(0x48)` in `dlr_kelf_stage` | the parse↔stage **bridge**: `{h_model, vcore, sg_count, uuid, params}` |
| `model_t` | 7744 B (`0x1E40`) | — | `calloc(0x1E40)` in `kbl_model_add` | **one per kbin/physical core**; DMA rings, IO queues, instruction blocks, act-table storage, dmem allocator |

### The two handles

The loader runs **two independent monotone id allocators**, and a reimplementer who collapses them into one will mis-key either the registry or the device db.

| Handle | Allocator | Keys | Set where |
|---|---|---|---|
| `H_NN` (`{uint32 id}`) | `dlr_model_set.last_nn_id++` (under the registry wrlock) | the userspace `dlr_model` in `dlr_model_set.models` | `kmgr_load_nn_nc`, after staging succeeds |
| `H_MODEL` (`{uint32 id}`) | `tdrv_get_unique_h_model()` | the per-physical-core `model_t` inside the tdrv model db | `kmgr_load_nn_nc`, *before* calling `dlr_kelf_stage` |

> **QUIRK —** `H_NN` and `H_MODEL` are **distinct uint32 id spaces from distinct allocators**, and `dlr_kelf_model_t` is the only object that joins them: it is keyed in by the `dlr_model` (`+0x1A0 kelf_model`) on the `H_NN` side and carries the `H_MODEL` (`+0x00`) used to find every per-core `model_t` on the device side. The caller's `nrt_model_t.h_nn` reaches the device record only by the chain `h_nn → dlr_model → dlr_model.kelf_model → h_model → model_t`. There is no direct `H_NN → model_t` map. (HIGH — both allocators code-confirmed; `tdrv_get_unique_h_model` called once per load before stage.)

> **NOTE —** the per-core `model_t` is **one per subgraph**, not one per model. A logical-NeuronCore (LNC) NEFF with `nkbin` subgraphs produces `nkbin` `model_t` records all sharing the *same* `H_MODEL` but living on `vcore->tpbs[0..nkbin)`. `dlr_kelf_unstage` walks `[0, sg_count)` removing each by `(tpbs[i], h_model)`.

---

## Stage 0 — Public Entry and Guard

### Purpose

The public API surface: validate arguments, claim a logical-NeuronCore (vnc), parse the per-model config, and call the orchestrator. This is the only stage the framework sees; everything below it is internal.

### Entry Point

```text
nrt_load (0xa9fe0)                          ── public; NRT_2.0.0
  └─ nrt_load_util (0xa9920)                ── core impl
       ├─ neff_get_header_from_buffer       ── 0x4ca2c0 (preflight; size>0x3FF, header_size<size)
       ├─ nrt_vnc_usage_find_and_inc        ── claim a logical-NeuronCore index
       ├─ nrt_config_parse_model_config     ── fill kbl_model_param_t (timeout, hash-verify, scratchpad…)
       ├─ kmgr_load_nn_nc (0xde280)         ── == STAGES 1..5
       └─ calloc(0x11C) nrt_model_t         ── populate vnc_idx/name/uuid/h_nn/gid; return
nrt_load_collectives (0xaa4c0)              ── CC variant; + NEURON_RT_ROOT_COMM_ID + topology validate
```

### Algorithm

```c
// nrt_load @0xa9fe0 -> nrt_load_util @0xa9920 (the load-relevant slice).
function nrt_load(buf, size, start_nc, nc_count /* = -1 */, out_model):
    if nc_count not in {-1, 2}:                       // "vnc_count is deprecated. only use -1."
        return NRT_INVALID
    return nrt_load_util(buf, size, start_nc, out_model, /*device_id*/-1, /*world*/0)

function nrt_load_util(buf, size, vnc_idx, out, device_id, world_size):
    require runtime state == NRT_STATE_INIT           // state guard
    hdr = neff_get_header_from_buffer(buf, size)      // 0x4ca2c0 — see [container]
    if hdr == NULL: return NRT_INVALID
    if hdr.feature_bits & 0x400:                      // LNC-size feature gate
        vnc_size = nrt_config_0.virtual_core_size
    nrt_vnc_usage_find_and_inc(vnc_idx)               // claim the logical NeuronCore
    vcore = vtpb_get_virtual_core(vnc_idx)
    nrt_config_parse_model_config(&param)             // -> kbl_model_param_t (32 B)

    rc = kmgr_load_nn_nc(buf, size, vnc_idx, &param, &h_nn, hdr->uuid)   // 0xde280 == S1..S5
    if rc != OK:
        nrt_vnc_usage_dec(vnc_idx)                    // release claim; core/info dump
        return rc

    m = calloc(0x11C)                                 // nrt_model_t (284 B)
    if m == NULL: { nrt_vnc_usage_dec(vnc_idx); core_dump; return NRT_OOM }
    m.vnc_idx = vnc_idx; copy_name(m); copy_uuid(m); m.h_nn = h_nn
    dlr_db_get_nn_cached_interned_model_id(...)       // 0xdc150
    if inspect_mode: nrt_save_neff(buf, size)
    *out = m; return OK
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `nrt_load` | `0xa9fe0` | public entry; reject `nc_count ∉ {-1,2}` | HIGH |
| `nrt_load_collectives` | `0xaa4c0` | CC variant; root-comm-id + topology validate; sets `model->gid` | HIGH |
| `nrt_load_util` | `0xa9920` | core: state guard, vnc claim, config parse, calloc `nrt_model_t` | HIGH |
| `nrt_config_parse_model_config` | — | fills the 32 B `kbl_model_param_t` (a4 to the loader) | HIGH |

### Considerations

`nrt_load_collectives` is the same path with three additions: it requires `NEURON_RT_ROOT_COMM_ID`, runs instance-family topology validation and `enc_validate_global_comm`, then after the load sets `dlr_model.gid = device_id`, calls `kmgr_trace_set_cc_global_id`, and optionally `kmgr_validate_replica_groups`. The `gid` is logged as the model id in CC traces. Everything in stages 1–5 is identical.

---

## Stage 1 — Container Parse and the TNC Cache

### Purpose

Turn the NEFF byte blob into an in-memory member table (`neff_t::files`), with a process-global cache so that N workers concurrently loading the *same* NEFF parse it once. This stage owns the only place the loader is genuinely racy by design.

### Entry Point

```text
kmgr_load_nn_nc (0xde280)
  ├─ neff_copy_name (0x4cb5d0)                  ── 256-B model name from header
  ├─ pthread_mutex_lock(tnc_lock @0xc5d840)
  │    └─ TNC 4-state machine (key = hdr->uuid, 128-bit)   ── enter/wait/complete inlined
  ├─ neff_parse (0x4ca3f0)                      ── [container] hash-verify + libarchive tar walk
  └─ (creator) complete_cache_creation          ── state=READY; sem_post per waiter
```

### The TNC state machine

The Temporary-NEFF-Cache is a **single global slot** (`tnc @0xc5d880`) guarded by `tnc_lock`, not an LRU — exactly one NEFF parse can be cached at a time, ref-counted, returning to `UNINIT` when the count drops to zero. The four states are `UNINIT/PENDING/READY/FAILED == 0/1/2/3`; the UUID is compared as a 128-bit value.

```c
// kmgr_load_nn_nc @0xde280 — the TNC enter logic (asserts cite kmgr/dlr.cpp).
function tnc_enter(uuid) -> (neff_load_info*, role):
    lock(tnc_lock)
    switch tnc.cache_state:
      case UNINIT(0):                                  // first worker
          assert tnc.neff_load_info == NULL && tnc.ref_count == 0 && tnc.pending_wait == 0
          tnc.ref_count = 0x100000001                  // (1<<32)|1 packed
          tnc.uuid = uuid; tnc.cache_state = PENDING
          info = operator new(0x70)                    // neff_load_info_t (112 B)
          unlock; return (info, CREATOR)               // "first worker, creating cache"
      case READY(2) if memcmp(uuid)==0:
          ++tnc.ref_count; unlock; return (tnc.info, REUSE)        // "using cache"
      case PENDING(1) if memcmp(uuid)==0:
          ++tnc.ref_count; ++tnc.pending_wait; unlock
          sem_wait(tnc.wait_sem)                        // "waiting for cache creation"
          assert memcmp(tnc.uuid, uuid) == 0
          if tnc.cache_state == READY: return (tnc.info, REUSE)    // "using cache after wait"
          else: return (operator new(0x70), INDEPENDENT)          // "cache creation failed, do my own"
      default:                                          // uuid mismatch or slot busy
          unlock; return (operator new(0x70), INDEPENDENT)        // "proceeding independently"
```

The **CREATOR** then runs `neff_parse` and, on success, `complete_cache_creation` (`tnc.cache_state = READY`, `sem_post` once per `pending_wait`); on failure it runs `complete_cache_creation_on_failure` (`cache_state = FAILED`, still `sem_post`s every waiter so none deadlocks) and releases the slot.

> **GOTCHA —** the cache is keyed *only* by the NEFF UUID and holds *one* slot. A second worker loading a **different** NEFF while the first is still `PENDING`/`READY` does not wait and does not evict — it allocates its own `neff_load_info` and proceeds **INDEPENDENT**, parsing the whole NEFF itself. A reimplementer modeling this as a general N-entry cache will over-share; it is a single-entry, UUID-exact, ref-counted rendezvous whose only job is to coalesce concurrent loads of the *identical* model.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `kmgr_load_nn_nc` | `0xde280` | orchestrator; inlines the TNC enter/wait/complete logic | HIGH |
| `neff_parse` | `0x4ca3f0` | container parse → `neff_t::files` (owned by [container](container.md)) | HIGH |
| `neff_copy_name` | `0x4cb5d0` | 256-B name copy from header | HIGH |
| `release_tmp_neff_cache` | `0xdb4d0` | decrement TNC ref; reset to `UNINIT` at 0 | HIGH |

---

## Stage 2 — JSON Graph and KELF Parse

### Purpose

Walk the manifest chain (`neff.json` → `kelf-a.json` → per-subgraph `def.json` + per-engine blobs) and produce, for each subgraph, a populated `mla_resources` working set ready for the KBIN build. This is the parse half of the parse→build boundary.

### Entry Point

```text
kmgr_load_nn_nc (0xde280)
  ├─ parse_neff_json (0xe1d10)                  ── simdjson neff.json; find attrs.func_name=="__kelf"
  └─ dlr_kelf_load (0xe0830)                    ── calloc(0x18) kelf_nn_t; "Loaded mlaop on %u NeuronCores"
       └─ kelf_load (0x49a6b0)
            └─ kelf::kelf::load (0x497dc0)       ── kelf-a.json; version/target/graphs[]; arch gate
                 └─ (per graph) construct_kbin (0x496d50)
                      ├─ kelf_load_from_neff (0x4c0870)   ── def.json + per-engine json/bin → mla_resources
                      └─ gen_kbin (0x4af930)               ── mla_resources(440 B) → kbin(424 B)  [== S3 BUILD]
```

### Algorithm

```c
// The S2 slice of kmgr_load_nn_nc @0xde280, then dlr_kelf_load @0xe0830.
function parse_graph(neff, neff_load_info):
    json = neff_get_file_content(neff, "neff.json")        // or legacy "kelp.json"
    if !json: reject "Invalid NN: %s, neff.json is missing"
    parse_neff_json(json, &kelf_member, &in_info, &out_info) // 0xe1d10 — IO maps + __kelf node

    kg = calloc(0x18)                                       // kelf_nn_t (24 B)
    rc = kelf_load(neff, kelf_member, kg)                   // 0x49a6b0
    // kelf::kelf::load @0x497dc0: read kelf-a.json; parse version/target/graphs[];
    //   parse_target: "*"=any, "sunda"=v2, "cayman"=v3, "mariana"=v4; arch gate
    //   (nlog_set_error_cause(NEFF_ARCH_INCOMPAT) is the ONLY error cause this path sets).
    // per graph -> construct_kbin @0x496d50:
    //     init mla_resources (440 B, stack);
    //     kelf_load_from_neff (0x4c0870): def.json {var,dma_queue,sb_carveouts,cc_streams,fp8,ucode}
    //       + per-engine (pe,act,dve,sp,pool[,q7]) json -> parse_one_engine_instr
    //       -> load_bin_file(<eng>.bin) (malloc+memcpy from neff_t::files);
    //     gen_kbin (0x4af930): mla_resources -> kbin[i] (424 B)   <-- the S3 BUILD verb
    //     ~mla_resources();
    kelf_resolve_vtpb_remote_variables(kg->kbin, …)         // 0x499410 — cross-subgraph (LNC)
    vtpb_ptpb_num_cores_to_vtpb_num_cores(kg->nkbin, &n); require n == 1 logical core
    return kg
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `parse_neff_json` | `0xe1d10` | simdjson DOM of `neff.json`; `__kelf` node + IO maps (11410 B) | HIGH |
| `dlr_kelf_load` | `0xe0830` | `calloc` `kelf_nn_t` + `kelf_load`; logs `NeuronCores` count | HIGH |
| `kelf_load` | `0x49a6b0` | construct `kelf::kelf`; per-graph `get_kbin`; remote-var resolve | HIGH |
| `kelf::kelf::load` | `0x497dc0` | `kelf-a.json` version/target/graphs; arch gate | HIGH |
| `construct_kbin` | `0x496d50` | per-subgraph: init `mla_resources`, parse, `gen_kbin` | HIGH |
| `kelf_load_from_neff` | `0x4c0870` | the 2260-insn `def.json` + per-engine parse → `mla_resources` | HIGH |
| `gen_kbin` | `0x4af930` | `mla_resources(440 B)` → `kbin(424 B)` build step | HIGH |
| `kelf_resolve_vtpb_remote_variables` | `0x499410` | cross-subgraph (LNC) variable resolution after all kbins built | MEDIUM |

### Considerations

The `kbin` (424 B, one per subgraph) is the product of this stage and the **input** to staging — it is the compiler's section table for one core: per-engine instruction sets, per-queue DMA metadata, the `mem_ref_set` variable table (relocation source), the `sb_carveouts` state-buffer reservation that drives the device carve-out, and the `target` arch type. Its interior is owned by [kelf2kbin](kelf2kbin.md) and [KBIN Structures](kbin-structs.md); this page only tracks it as the unit `dlr_kelf_stage` fans out.

> **NOTE —** the parse→build split is *not* the parse→device split. `gen_kbin` (the "BUILD" verb) runs at **parse** time, in stage 2, still `IOCTL`-free — it transforms the parser working set into the device-staging record but allocates no device memory. Whether `gen_kbin` performs any *provisional* relocation versus deferring all of it to stage is not pinned (MED); the var-id→PA resolution that needs real device addresses clearly happens later, at stage time (§Stage 5).

---

## Stage 3+4 — KBL Build and Device-DRAM Stage

### Purpose

Stand up the per-physical-core device model. This is where the loader stops being a parser and becomes a linker-loader: it allocates the HBM image, copies weights/tables/instructions in, builds the DMA rings and IO queues, and assembles the engine instruction blocks. The stage entry is `dlr_kelf_stage` — the KMGR↔KBL seam — and the per-core worker is `kbl_model_add`.

### Entry Point

```text
dlr_kelf_stage (0xe0970)                       ── KMGR↔KBL SEAM; calloc(0x48) dlr_kelf_model_t
  ├─ vtpb_info_shared_alloc / _init_vtpb       ── shared per-vnc info; runs the mem_ref PLAN
  ├─ require vcore->num_tpbs >= kg->nkbin       ── assert "kg->nkbin == 1 || == vtpb_size"
  ├─ (nkbin == 1)  dlr_kelf_stage_model_add (0xe0730)
  │                  └─ kbl_model_add(&vcore->tpbs[0], …)
  └─ (nkbin > 1)   dlr_kelf_stage_multi_tpb_model_add (0xe0580)
                     └─ per kbin: pthread_create(dlr_kelf_stage_model_add_thread 0xe07f0)
                          └─ dlr_kelf_stage_model_add → kbl_model_add(&vcore->tpbs[i], …)
                        pthread_join all; rollback kbl_model_remove on any error
```

The per-core build pipeline inside `kbl_model_add`, in source order (read from its callgraph out-edges):

```text
kbl_model_add (0x3058e0)                        ── calloc(0x1E40) model_t
  ├─ dbtc_storage_init                          ── device-book / trace-counter storage
  ├─ dma_queue_bundle_instance_lut_init
  ├─ drs_create_data_refill_rings (0x31c850)    ── per-tensor HBM<->TPB data DMA rings
  ├─ io_init_mr_to_name_map / io_build_mr_to_name_lookup / mr_pointer_init_pointer_vars
  ├─ io_create_queues (0x4453a0)                ── IO queues
  ├─ act_local_storage_tbls_init (0x31a4e0)     ── act tables: dmem_alloc ×4 + dmem_buf_copyin ×4  [DEVICE COPY-IN]
  ├─ dve_dynamic_config_init
  ├─ [arch2] ucode_stage_libs (0x310ea0)        ── stage Q7 microcode libs
  ├─ hw_exec_queue_count_descs
  ├─ sequencer_setup_instr (0x4483d0)           ── per-engine instr blocks  [BUILD + RELOCATE + STAGE → S5]
  ├─ dma_ring_setup_queue_bundles
  └─ add_model (0x2275f0)                        ── register model_t in tdrv db (keyed by h_model)
     [any sub-builder failure -> model_free 0x3055f0: full teardown of the partial model_t]
```

### Algorithm — the staging seam

```c
// dlr_kelf_stage @0xe0970 — the KMGR↔KBL boundary.
function dlr_kelf_stage(kg, vcore, param, h_model, name, uuid, out_kelf_model):
    core_size = vtpb_get_vtpb_core_size(vcore)
    km = calloc(0x48)                                  // dlr_kelf_model_t (72 B)
    copy param/uuid/sg_count into km
    if vcore->num_tpbs < kg->nkbin:                    // "Insufficient number of pncs, required %u, available %u"
        free(km); return NRT_INVALID
    assert kg->nkbin == 1 || kg->nkbin == core_size    // kmgr/dlr_kelf.cpp:0xA7
    shared = vtpb_info_shared_alloc(...)
    vtpb_info_shared_init_vtpb(shared, …)              // 0x3148e0 — runs the mem_ref PLAN (see [memory-planning])

    if kg->nkbin == 1:
        rc = dlr_kelf_stage_model_add(kg->kbin[0], vcore, 0, param, h_model, name, uuid, shared)
    else:
        rc = dlr_kelf_stage_multi_tpb_model_add(kg, vcore, param, h_model, name, uuid, shared)  // fan-out

    vtpb_info_shared_free(shared)
    if rc == OK: { km->h_model = h_model; *out_kelf_model = km; }
    else        { free(km); }
    return rc

// dlr_kelf_stage_multi_tpb_model_add @0xe0580 — the LNC fan-out (frame 19544 B: the 256-slot arg array).
function multi_tpb(kg, vcore, …):
    for i in [0, kg->nkbin):            // up to 256
        args[i] = { &ret[i], kg->kbin[i], param, shared, vcore, /*pcore*/i, h_model, name, uuid }
        pthread_create(&tid[i], dlr_kelf_stage_model_add_thread, &args[i])   // 0xe07f0 trampoline
    for i in [0, kg->nkbin): pthread_join(tid[i])
    first_err = first non-zero ret[i]
    if first_err:                       // roll back already-staged cores
        for each successfully staged j: kbl_model_remove(&vcore->tpbs[j], h_model)
    return first_err
```

### The device boundary

`kbl_model_add` is where the first `IOCTL` fires. The act-table init (`act_local_storage_tbls_init`) and the per-`mem_ref` placement (run from `vtpb_info_shared_init_vtpb`, [memory-planning](memory-planning.md)) call `dmem_alloc(...)`, which issues **`MEM_ALLOC_V2MT64` (#102, `0x80384E66`)** to carve the subgraph's HBM region, then `dmem_buf_copyin` (`@0x229820`) DMAs the host buffer (weights, act tables, instruction RAM) into it.

> **QUIRK —** the entire parse (stages 0–3) is `IOCTL`-free. A reimplementer can run header validation, integrity check, tar walk, JSON parse, KELF lowering, and `gen_kbin` with **no device present** — the model is fully built host-side before the first byte touches HBM. The device boundary is exactly one function deep: `dlr_kelf_stage → kbl_model_add → dmem_alloc(MEM_ALLOC_V2MT64)`. (HIGH — confirmed: no `IOCTL` callees in stages 0–3; first allocation `IOCTL` inside `kbl_model_add`.)

> **GOTCHA —** the multi-tpb fan-out is genuine `pthread` parallelism (one thread per kbin, up to 256), and its rollback is **all-or-nothing across cores**: if any one thread's `kbl_model_add` fails, the joiner unwinds every successfully-staged sibling with `kbl_model_remove`. The 19544-byte stack frame of `dlr_kelf_stage_multi_tpb_model_add` is the 256-entry `kelf_model_add_thread_args_t[256]` array (64 B each) living on the joiner's stack — its lifetime must outlive every spawned thread, so it cannot be heap-freed before `pthread_join`. A reimplementation that allocates the per-thread args in a scope that exits before join will use-after-free.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `dlr_kelf_stage` | `0xe0970` | KMGR↔KBL seam; `calloc` bridge; single-vs-multi dispatch | HIGH |
| `dlr_kelf_stage_multi_tpb_model_add` | `0xe0580` | LNC fan-out: pthread-per-kbin, join, rollback | HIGH |
| `dlr_kelf_stage_model_add` | `0xe0730` | single-subgraph leaf → `kbl_model_add(&tpbs[i], …)` | HIGH |
| `dlr_kelf_stage_model_add_thread` | `0xe07f0` | pthread trampoline (`kelf_model_add_thread_args_t`) | HIGH |
| `kbl_model_add` | `0x3058e0` | per-core build + stage; `calloc(0x1E40)` `model_t` | HIGH |
| `model_free` | `0x3055f0` | full `model_t` teardown on the build error path | HIGH |
| `dmem_buf_copyin` | `0x229820` | copy host buffer into device DRAM (the staging copy) | HIGH |
| `add_model` | `0x2275f0` | register `model_t` in the tdrv db, keyed by `h_model` | HIGH |

---

## Stage 5 — Relocate and Register

### Purpose

Two things happen here. First, **relocation** — which is realized *during* the stage-4 build, not as a separate pass — rewrites compiler-relative references to absolute device addresses. Second, **register and ready** — back in `kmgr_load_nn_nc`, the finished `dlr_model` is interned into the registry and marked READY.

### Relocation is two sub-layers, run at stage time

Relocation is **not** one monolithic relocator. It is two distinct rewrites, both reached from `sequencer_setup_instr` (`@0x4483d0`) via `itf_setup_functions_one_eng` (`@0x2798c0`), because both need the device physical addresses that only exist after `dmem_alloc` has run.

```c
// itf_setup_functions_one_eng @0x2798c0 — the per-engine relocation hub, called from
// sequencer_setup_instr. Two sub-layers:

// (a) INSTRUCTION-STREAM var-id -> physical-address resolution.
// translate_pseudo_instrs_partial_v2 @0x2763d0 (and the v2/v3 translators it fronts):
function relocate_instructions(eng, model_mr_set, scratchpad):
    for each pseudo instr:
        mr  = lookup_memref_by_idx(model_mr_set, var_id)          // 0x22da30 (ht_find)
        pa  = mem_ref_to_addr(mr, offset, size)                   // 0x22da60: addr = offset + physical_address
        //   (or ioqs_mr_var_id_to_pa 0x445c00 / tensor_get_pa 0x30fae0 for IO/tensor operands)
        patch operand <- pa
        if expanded_slot_count != original:                       // pseudo->ISA may grow/shrink the stream
            kbin_patch_append(model.kbin_patch_info, INSERT|DELETE, ip_delta)   // 0x2fb0f0
            //   "Failed to add patch insert info. Debug info may be inaccurate"
            //   records into model_t.kbin_patch_info (+0x18A0); preserves the device-IP <-> NEFF-IP map.

// (b) DMA-RING buf_ptr relocation.
// imcpy_load_vring @0x31bc10:
function relocate_dma_rings(vring_set):
    for each indirect-memcpy rx ring entry:
        // tag bits (0x4000../0x8000..) select tx/rx ring; mask &0x3FFF.. = 62-bit phys field.
        buf_ptr = make_absolute(buf_ptr_relative)                // RELATIVE -> ABSOLUTE device phys
    vring_dump_to_pring(...)                                      // write finalized rings to device prings
    //   "Failed to load imcpy rings to device" / "trying to load an empty indirect memcpy vring set"
```

The first sub-layer also feeds the profiler's back-map: `kbin_patch_device_ip_to_neff_ip` (`@0x2fb3a0`) maps a device instruction pointer back to its NEFF IP, consumed by `nrt_profile_get_instruction_patch_info`. The patch records are *instruction-IP deltas*, not address relocations.

> **CORRECTION (W2-NEFF-LOAD) —** the `kbin_patch_*` family is sometimes read as the address relocator. It is **not**: it tracks **instruction-IP deltas** (an `INSERT` when a pseudo expands to more than one ISA slot, a `DELETE` when it collapses to zero), so that the host-side debug/profile IP view stays consistent with the device's post-expansion instruction layout. The actual *address* relocation is the var-id→PA resolution in `translate_*` (sub-layer a) and the `buf_ptr` rewrite in `imcpy_load_vring` (sub-layer b). Conflating the two mislabels the patch store. (HIGH — both code-confirmed; `kbin_patch_append` callers are the translators and the instruction-block builders.)

### Algorithm — register and ready

```c
// kmgr_load_nn_nc @0xde280 — the S5 tail, after dlr_kelf_stage returns.
function register_and_ready(dlr_model, kelf_model, neff_load_info, h_nn_out):
    dlr_model.kelf_model(+0x1A0) = kelf_model            // attach the device bridge
    copy uuid back into dlr_model
    dlr_model.input_info  = neff_load_info.input_info     // Rb_tree::operator= the IO maps
    dlr_model.output_info = neff_load_info.output_info
    dlr_model.state(+0x1A8) = 4                           // READY

    wrlock(dlr_model_set.lock)                            // 0xc5c720
      id = dlr_model_set.last_nn_id++                     // allocate H_NN
      dlr_model.h_nn = id
      dlr_model_set.models[id] = dlr_model                // open-coded unordered_map insert + prime-rehash
    unlock

    kmgr_trace_set_uuid(id)                               // 0xde160 — fold 128-bit uuid -> 64
    kmgr_set_mac_count(id, mac_count)                     // 0xde200 — from hlo_stats.json
    dlr_model::create_nds_model_info(dlr_model)           // 0xddd60 — NDS datastore handles
    *h_nn_out = id
    (creator) release_tmp_neff_cache() else free_neff_load_info()
    kmetric_update_nds_generic_status(...)                // exit metric: records load outcome
    // "Loaded NN \"%s\" ID: %u" / "NN: %s, loaded successfully"
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `sequencer_setup_instr` | `0x4483d0` | per-engine instruction-block setup; the relocation entry | HIGH |
| `itf_setup_functions_one_eng` | `0x2798c0` | per-engine relocation hub → translate + imcpy | HIGH |
| `translate_pseudo_instrs_partial_v2` | `0x2763d0` | var-id→PA resolution + `kbin_patch_append` | HIGH |
| `imcpy_load_vring` | `0x31bc10` | rewrite relative rx `buf_ptr` → absolute device phys | HIGH |
| `ioqs_mr_var_id_to_pa` | `0x445c00` | IO-queue var-id → physical address | HIGH |
| `tensor_get_pa` | `0x30fae0` | tensor device physical address resolve | HIGH |
| `kbin_patch_append` | `0x2fb0f0` | record INSERT/DELETE instruction-IP delta | HIGH |
| `kbin_patch_device_ip_to_neff_ip` | `0x2fb3a0` | device-IP → NEFF-IP back-map (profiler) | HIGH |
| `kmgr_trace_set_uuid` / `kmgr_set_mac_count` | `0xde160` / `0xde200` | fold uuid; set MAC count from `hlo_stats.json` | HIGH |
| `create_nds_model_info` | `0xddd60` | build NDS per-subgraph datastore handles | HIGH |

---

## Error and Cleanup Paths

The unwind is staged: each stage owns the resources it allocated and frees exactly those on its error edge. This is the leak-freedom table a reimplementer needs.

| Failure point | Stage | Cleanup |
|---|---|---|
| `neff_parse` fail (creator) | S1 | `complete_cache_creation_on_failure` (state=FAILED, `sem_post` waiters) + `release_tmp_neff_cache` |
| tar/hash/version reject | S1 | `neff_destroy` walks `neff_t::files` freeing each entry + `free(neff)`; status propagated |
| `neff.json`/`kelp.json` missing | S2 | `"neff.json is missing"`; `free_neff_load_info`; cache → FAILED |
| KELF parse / arch mismatch | S2 | dispose simdjson; `nlog_set_error_cause(NEFF_ARCH_INCOMPAT)` (the only error cause set here) |
| `gen_kbin` / `dlr_kelf_stage` fail | S3/S4 | `kmgr_unstage_kelf_model` → `dlr_kelf_unstage` (per-tpb `kbl_model_remove`) + `free_neff_load_info` |
| `kbl_model_add` sub-builder fail | S4 | `model_free` (full teardown of the partial `model_t`) on the unwind |
| multi-tpb mid-fan-out fail | S4 | per-thread `ret[i]!=0` → joiner rolls back every already-staged kbin via `kbl_model_remove` |
| `calloc` `nrt_model_t` fail | S0 | `nrt_vnc_usage_dec` + core/info dump (kmgr side already succeeded — the model is leaked unless caller retries) |

The symmetric unload is `nrt_unload → kmgr_unload_nn` (`@0xdc450`; guards `state==4 && ref==0`, hashtable erase, ref-drain spin) `→ kmgr_unstage_kelf_model → dlr_kelf_unstage` (per-tpb `kbl_model_remove → model_free`) `→ dlr_kelf_unload` (`kelf_free` + free) `→ operator delete(dlr_model, 0x1E0)`.

> **NOTE —** the one asymmetric edge is the S0 `nrt_model_t` `calloc` failure: by that point `kmgr_load_nn_nc` has already registered the `dlr_model` (state READY) in `dlr_model_set`. `nrt_load_util` decrements the vnc usage and dumps, but the registered model is **not** unloaded on this path — the kmgr-side success is not rolled back. (HIGH — the S5 register precedes the S0 `calloc`; no `kmgr_unload_nn` on the `calloc`-fail edge.)

---

## Related Components

| Name | Relationship |
|---|---|
| `kmgr_load_nn_nc` (`@0xde280`) | the master orchestrator; inlines TNC, calls every stage, registers the model |
| `dlr_kelf_stage` (`@0xe0970`) | the KMGR↔KBL seam where parse-side crosses into device-side |
| `kbl_model_add` (`@0x3058e0`) | the per-physical-core build+stage worker; first `IOCTL` fires here |
| `dlr_model_set` (`@0xc5c720`) | the process-global `H_NN`-keyed model registry |
| `tnc` (`@0xc5d880`) | the single-slot UUID-keyed parse cache that coalesces concurrent loads |

## Cross-References

- [Overview: the Model-File Journey](overview.md) — the high-level NEFF → KBIN → device story this page details stage-by-stage
- [NEFF Container (gzip-tar, No-Magic Header)](container.md) — Stage 1: `neff_parse`, the 1024-byte header, and the in-memory tar walk into `neff_t::files`
- [NEFF Section Taxonomy](section-taxonomy.md) — what each named TAR member becomes once unpacked (the "sections" this pipeline lowers)
- [kelf2kbin: JSON → KBIN Lowering](kelf2kbin.md) — Stage 2/3: `construct_kbin`/`gen_kbin`, the `mla_resources → kbin` transform
- [KBIN Structures](kbin-structs.md) — the 424-byte `kbin` record this pipeline fans out and the on-disk schema it carries
- [Static Memory Planning (mem_ref)](memory-planning.md) — Stage 4/5: `vtpb_info_shared_init_vtpb` placement and `mem_ref_to_addr` resolve that relocation consumes
- [Compute-Resource Build (KBL)](compute-resource-build.md) — the `kbl_model_add` per-core `model_t` interior (DMA rings, IO queues, instruction blocks)
- [The Submit Path (Bind → Stage → Doorbell)](../exec/submit-path.md) — where a READY `dlr_model` is handed to `kmgr_exec` for per-execution resource build (the load/exec boundary)
- [Public C API: Lifecycle and Init/Teardown](../runtime/api-lifecycle.md) — the `nrt_load` caller whose buffer this pipeline consumes
- [back to index](../index.md)
