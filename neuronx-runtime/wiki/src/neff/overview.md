# Overview: the Model-File Journey

> *All addresses, offsets, sizes, and symbol names on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA. Source TUs: `/opt/workspace/KaenaRuntime/{kelf/neff.cpp, kelf/kelf.cpp, kelf/kelf2kbin.cpp, kmgr/dlr.cpp, kmgr/dlr_kelf.cpp, tdrv/tdrv.c}`. Other versions will differ.*
> *Evidence grade: **Confirmed (symbol-anchored)** — every stage entry on this page is an `nm`/IDA-confirmed symbol at the cited address (cross-checked against `*_functions.json`); struct sizes are verbatim from `structures.json`. This page is the **map**; the deep derivations live in the sibling pages it links. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

A **NEFF** ("Neuron Executable File Format") is the runtime's self-contained model package — the single artifact a framework hands to `nrt_load`. Physically it is a **1024-byte flat binary header** followed by a **gzip-compressed POSIX-pax/ustar TAR payload**, and that payload is a bag of two kinds of members: **JSON manifests** (a TVM-relay graph manifest, a KELF descriptor, per-subgraph `def.json` definitions) and **raw headerless binary blobs** (per-engine instruction/data `.bin` files, activation tables). There is no magic number, no section table, and no directory inside the archive: the logical structure is carried entirely by **TAR member pathnames** plus a chain of JSON manifests that name each other by string. Read it the way you read a self-extracting tarball with a fixed preamble.

Loading is a **loader-linker**: the runtime turns that byte blob into an executable image bound to one or more physical NeuronCores, by running the classic dynamic-linker play — *unpack the container, parse the manifests, lower them into compiled per-engine programs, build the per-core resource objects, place every section at a fixed device-DRAM (HBM) address, relocate the references between them, and return a handle*. The Neuron loader is a **six-stage** pipeline whose first four stages are entirely userspace and fire **no device `IOCTL`**; the device boundary opens at staging, where each subgraph's state-buffer/HBM carve-out is allocated by `MEM_ALLOC_V2MT64` and weights/tables/instructions are DMA'd in. "Relocation" here is not one monolithic pass — it is realized at two layers during build/stage: compiler **var-ids rewritten to physical SBUF/HBM addresses** inside the TPB instruction stream, and **relative DMA-ring pointers rewritten to absolute device physical addresses**.

This page is the **map of Part V**. It fixes the version, names the six stages with their owning entry symbol and address, draws the end-to-end pipeline diagram, and points at the ten sub-pages that own each stage's byte-level detail. It deliberately does **not** re-derive the container walk, the JSON schema, the KELF→KBIN lowering, the `mem_ref` placement plan, or the per-core build — those are the deep siblings. The NEFF *producer-side* schema (what `neuronx-cc` emits) is canonically owned by the compiler/platform projects; this wiki derives only the *consumer* side from `libnrt.so`.

For reimplementation, the contract this part documents is:

- **The container ABI** — the 1024-byte `neff_header_t` (no magic; `pkg_version` discriminates `1`=raw-tar+SHA-256 / `2`=gzip-tar+MD5), the in-memory gzip-tar walk, and the member-name routing convention.
- **The manifest chain** — `neff.json` (TVM-relay graph; the `attrs.func_name=="__kelf"` discriminator) → the named KELF descriptor → per-subgraph `def.json` → per-engine `<eng>.bin` blobs.
- **The in-memory artifact model** — there is **no monolithic `.kbin` file**. Lowering produces, per subgraph, an in-RAM **`kbin`** (424-byte device-binary descriptor record) built from a `kelf` parser working set; staging stands up a per-physical-core **`model_t`** (7744 bytes).
- **The placement + relocation discipline** — a static `mem_ref` plan resolved once at load (zero per-inference allocation), with var-id→PA instruction patching and DMA-ring `buf_ptr` rewriting at stage time.
- **The stage boundaries** — where parse ends and the device begins (`dlr_kelf_stage` `@0xe0970`), and the two distinct id spaces (`h_nn` userspace registry handle vs `h_model` per-core tdrv handle).

| | |
|---|---|
| **Public entry** | `nrt_load` `@0xa9fe0` · `nrt_load_collectives` `@0xaa4c0` → `nrt_load_util` `@0xa9920` |
| **Load orchestrator** | `kmgr_load_nn_nc` `@0xde280` (centerpiece; stages 1–5) |
| **Parse / stage seam** | `dlr_kelf_stage` `@0xe0970` (first device `IOCTL` = `MEM_ALLOC_V2MT64`, #102 `0x80384E66`) |
| **Container header** | `neff_header_t` — 1024 B, ordinal 5896; payload at `+0x400` |
| **Lowered artifacts** | `kbin` (424 B / subgraph, in-RAM) · `model_t` (7744 B / physical core) — **no `.kbin` file** |
| **Returned handle** | `nrt_model_t` (284 B = `0x11C`), keyed by `h_nn` into `dlr_model_set` `@0xc5c720` |
| **Vendored decoders** | libarchive 3.8.0dev · zlib 1.3.1 · simdjson 0.9.0 (all statically linked) |
| **IOCTLs in stages 0–3** | none — container + JSON parse + lowering are 100 % userspace |

---

## The six stages

`nrt_load` is a six-stage, userspace-orchestrated pipeline. The table fixes each stage to its owning entry symbol+address and the Part-V page that owns its depth. Stages 0–3 fire **no** device `IOCTL`; the device boundary opens at stage 4.

| Stage | What it does | Entry symbol @ addr | Owned by |
|---|---|---|---|
| **S0 — Guard** | public API; reject deprecated `nc_count`, claim a vNC, parse `model_config` | `nrt_load` `@0xa9fe0` → `nrt_load_util` `@0xa9920` | [Load Pipeline](load-pipeline.md) |
| **S1 — Container unpack** | header preflight + hash verify; gzip-tar walk → `neff_t::files` RB-tree; TNC dedup cache | `neff_parse` `@0x4ca3f0` (via `kmgr_load_nn_nc` `@0xde280`) | [Container](container.md) |
| **S2 — Metadata parse** | simdjson over `neff.json` → `__kelf` node; KELF descriptor → per-subgraph `def.json` + `<eng>.bin` | `parse_neff_json` `@0xe1d10` → `kelf_load` `@0x49a6b0` → `kelf_load_from_neff` `@0x4c0870` | [Metadata Schema](metadata-schema.md), [Section Taxonomy](section-taxonomy.md), [dtype System](dtype-system.md) |
| **S3 — Lowering (kelf→kbin)** | per subgraph: parser working set (`mla_resources`, 440 B) → 424-B `kbin` device-binary record | `construct_kbin` `@0x496d50` → `gen_kbin` `@0x4af930` | [kelf2kbin](kelf2kbin.md), [KBIN Structures](kbin-structs.md) |
| **S4 — Compute build + DRAM stage** | per `kbin`: stand up 7744-B `model_t`; `dmem_alloc(TONGA_DRAM)` + copy-in weights/tables/instructions | `dlr_kelf_stage` `@0xe0970` → `kbl_model_add` `@0x3058e0` → `dmem_buf_copyin` `@0x229820` | [Compute-Resource Build](compute-resource-build.md), [Memory Planning](memory-planning.md) |
| **S5 — Relocate + ready** | var-id→PA instruction translate; DMA-ring `buf_ptr` → absolute phys; register `dlr_model`, state→READY | `sequencer_setup_instr` `@0x4483d0` · `imcpy_load_vring` `@0x31bc10` · `kbin_patch_device_ip_to_neff_ip` `@0x2fb3a0` | [Load Pipeline](load-pipeline.md) |

> **NOTE —** the stage boundaries are not equal in cost or risk. Stages 0–3 are pure host work on a `malloc`'d member table and can be retried freely. From stage 4 on, device memory is allocated and DMA'd; a mid-pipeline failure must roll back per-core `model_t` records (`model_free` `@0x3055f0`) and, on a multi-subgraph load, the already-staged subgraphs of sibling threads. The detailed error/cleanup matrix lives in [Load Pipeline](load-pipeline.md).

---

## The pipeline, end to end

The diagram traces one NEFF byte blob from the framework call to a model-ready handle. Each arrow is annotated with the entry symbol@addr that drives that transition; the boxes are the artifacts that exist between transitions. The vertical seam between S3 and S4 is the **parse/stage boundary** — to its left everything is host memory and zero `IOCTL`s; to its right device DRAM is allocated and DMA copy-in begins.

```text
  framework (libneuronpjrt, …)
        │  nrt_load @0xa9fe0  /  nrt_load_collectives @0xaa4c0
        ▼
  ┌─────────────────────────────────────────── HOST / userspace · NO IOCTL ───────────────────────────────────┐
  │                                                                                                            │
  │  NEFF byte blob  (1024-B header + gzip-pax-tar payload @+0x400)                                            │
  │        │  neff_parse @0x4ca3f0           ── S1 CONTAINER UNPACK                                            │
  │        │    · header preflight (neff_get_header_from_buffer @0x4ca2c0)                                     │
  │        │    · hash verify (pkg1 SHA-256 / pkg2 MD5)                                                        │
  │        │    · libarchive gzip-tar walk → zlib inflate → per-member malloc                                  │
  │        ▼                                                                                                   │
  │  neff_t::files   (RB-tree: pathname → (buf,size))   + TNC dedup cache (tnc @0xc5d880)                      │
  │        │  parse_neff_json @0xe1d10               ── S2 METADATA PARSE                                      │
  │        │    · simdjson neff.json → node attrs.func_name=="__kelf" → attrs.kelf member name                │
  │        │    · kelf_load @0x49a6b0 → kelf::kelf::load @0x497dc0  (kelf-a.json: version/target/graphs[])     │
  │        ▼                                                                                                   │
  │  per-subgraph def.json + <eng>.bin members      ── S2 SECTION TAXONOMY                                     │
  │        │  construct_kbin @0x496d50 → kelf_load_from_neff @0x4c0870                                         │
  │        │    · parse var / dma_queue / sb_carveouts / cc_streams / fp8 / ucode + per-engine instr           │
  │        │    · load_bin_file @0x4ae500  (fetch <sg_dir>/<eng>.bin from neff_t::files)                       │
  │        ▼                                                                                                   │
  │  mla_resources  (440-B parser working set, per subgraph)                                                   │
  │        │  gen_kbin @0x4af930                     ── S3 KELF → KBIN LOWERING                                │
  │        ▼                                                                                                   │
  │  kbin[ ]   (424-B device-binary record, ONE PER subgraph)   ←── NO monolithic .kbin file                  │
  │                                                                                                            │
  └────────────────────────────────────────────────────────────────────────────────────────────────────────┘
        │  dlr_kelf_stage @0xe0970          ═══ PARSE / STAGE SEAM · first IOCTL = MEM_ALLOC_V2MT64 ═══
        ▼
  ┌─────────────────────────────────────────── DEVICE · IOCTL + DMA ──────────────────────────────────────────┐
  │        │  kbl_model_add @0x3058e0        ── S4 KBL COMPUTE-RESOURCE BUILD                                  │
  │        │    · calloc 7744-B model_t; dma_queue luts; io_create_queues; sequencer_setup_instr              │
  │        ▼                                                                                                   │
  │  model_t  (7744-B, ONE PER physical core)   +   dmem_allocator (mem_ref plan placed once)                 │
  │        │  dmem_alloc(TONGA_DRAM) + dmem_buf_copyin @0x229820   ── S4 STAGE TO DEVICE DRAM                  │
  │        ▼                                                                                                   │
  │  weights / act-tables / instruction RAM resident in HBM                                                    │
  │        │  ── S5 RELOCATE (two layers, in-place during build)                                              │
  │        │    (a) var-id → PA : sequencer_setup_instr @0x4483d0 · ioqs_mr_var_id_to_pa @0x445c00            │
  │        │                       · tensor_get_pa @0x30fae0 · kbin_patch_append @0x2fb0f0 (IP deltas)        │
  │        │    (b) ring buf_ptr → absolute phys : imcpy_load_vring @0x31bc10                                 │
  │        │    profiler IP back-map: kbin_patch_device_ip_to_neff_ip @0x2fb3a0                               │
  │        ▼                                                                                                   │
  │  dlr_model registered in dlr_model_set @0xc5c720 ; state = 4 (READY)                                       │
  └────────────────────────────────────────────────────────────────────────────────────────────────────────┘
        │  kmgr_load_nn_nc returns ; nrt_load_util calloc(0x11C) nrt_model_t
        ▼
  MODEL-READY  →  nrt_model_t (284-B handle, h_nn key)  returned to caller
```

> **GOTCHA —** "relocation" is not a single relocator pass over a relocation table the way an ELF dynamic linker runs `R_X86_64_*` entries. It is two unrelated rewrites performed during the stage-4 build: (a) the per-instruction operand resolution that maps compiler **var-ids** to physical SBUF/HBM addresses (`sequencer_setup_instr` → `translate_one_pseudo_*` → `ioqs_mr_var_id_to_pa` `@0x445c00` / `tensor_get_pa` `@0x30fae0`), and (b) the DMA-ring rewrite that turns a **relative** ring `buf_ptr` (tag bits select tx/rx, low bits = 62-bit phys) into an **absolute** device physical address (`imcpy_load_vring` `@0x31bc10`). The `kbin_patch_*` family (`@0x2fb000`…) is a *separate* concern — it records INSERT/DELETE **instruction-IP deltas** so the host-side profiler/debugger can map a device IP back to its NEFF IP; it does **not** relocate addresses. See [Load Pipeline](load-pipeline.md).

---

## Stage-by-stage, at map altitude

This section walks the six stages once more at orientation altitude — what each consumes, what it produces, and the single fact that gates the handoff to the next stage. The intent is to let a reimplementer scope the work per stage and jump to the owning page; the algorithms themselves live in those pages, not here.

### S0 — Guard (`nrt_load` `@0xa9fe0`)

The public entry rejects a deprecated `nc_count` (only `-1` or `2` accepted; string `"vnc_count is deprecated. only use -1."`), claims a virtual NeuronCore index, runs `neff_get_header_from_buffer` `@0x4ca2c0` as a cheap preflight (size `> 0x3FF`, `header_size < size`), parses the per-load `kbl_model_param_t` (timeout, ring-cache size, hash-validate, scratchpad), and calls the orchestrator. The collectives variant `nrt_load_collectives` `@0xaa4c0` adds a `NEURON_RT_ROOT_COMM_ID` requirement, topology validation, and a global-comm id that is later folded into the model. **Consumes** a host byte buffer + size; **produces** a validated header pointer (in-place, no copy) and a vNC claim. Owner: [Load Pipeline](load-pipeline.md).

### S1 — Container unpack (`neff_parse` `@0x4ca3f0`)

The orchestrator `kmgr_load_nn_nc` `@0xde280` first checks the process-global TNC cache (`tnc` `@0xc5d880`, UUID-keyed, single slot, 4-state UNINIT/PENDING/READY/FAILED): a concurrent loader of the *same* NEFF reuses one parse result instead of re-walking the archive. The creator runs `neff_parse`, which gates version (major `≤ 2`) and the forward-compat feature mask (`feature_bits & 0x7FFFFFFFF8000000` rejects), verifies integrity (`pkg_version==1` SHA-256 / `==2` MD5), then drives libarchive over the in-RAM `[+0x400 .. +0x400+data_size)` window and inserts each member into `neff_t::files` (an RB-tree keyed by pathname). **Consumes** the validated buffer; **produces** the member table. No `IOCTL`. Owner: [NEFF Container](container.md).

### S2 — Metadata parse (`parse_neff_json` `@0xe1d10`)

`parse_neff_json` runs simdjson over `neff.json` (legacy fallback `kelp.json`), finds the node whose `attrs.func_name=="__kelf"`, reads its `attrs.kelf` string as the KELF descriptor member name, and builds the input/output IO maps from the graph's `arg_nodes`/`heads`. `dlr_kelf_load` `@0xe0830` → `kelf::kelf::load` `@0x497dc0` then reads the KELF descriptor (`version`/`target`/`graphs[]`), gating the target arch (`sunda`/`cayman`/`mariana`, or `*` = host arch; `NEURON_RT_ALLOW_LEGACY_NEFF=1` escapes a mismatch). Per graph, `construct_kbin` `@0x496d50` splits the `def.json` path and `kelf_load_from_neff` `@0x4c0870` parses the subgraph definition and fetches each `<eng>.bin` blob via `load_bin_file` `@0x4ae500`. **Consumes** the member table; **produces** a populated `mla_resources` (440 B) parser working set per subgraph. Owners: [Metadata Schema](metadata-schema.md), [Section Taxonomy](section-taxonomy.md), [dtype System](dtype-system.md).

### S3 — Lowering (`gen_kbin` `@0x4af930`)

`gen_kbin` transforms each subgraph's `mla_resources` working set into the 424-byte `kbin` device-binary descriptor — the per-engine instruction sets, the DMA-ring metadata, the `mem_ref_set` variable table, the state-buffer carve-out, the activation-function sets, cc-streams and fp8 config. This is the step that earns the part its name and the "no `.kbin` file" fact: the artifact is born in RAM here, never read from the archive. For LNC (multi-core) loads, `kelf_resolve_vtpb_remote_variables` `@0x499410` resolves cross-subgraph variable references after all `kbin`s are built. **Consumes** `mla_resources`; **produces** the `kbin[]` array (`nkbin` records). Owners: [kelf2kbin](kelf2kbin.md), [KBIN Structures](kbin-structs.md).

### S4 — Compute build + DRAM stage (`dlr_kelf_stage` `@0xe0970`)

This is the seam. `dlr_kelf_stage` requires the core count to match (`nkbin == 1` or `== num_tpbs`), then either does a single `kbl_model_add` (one subgraph) or fans out **one POSIX thread per `kbin`** (up to 256) via `dlr_kelf_stage_multi_tpb_model_add` `@0xe0580`. Each `kbl_model_add` `@0x3058e0` `calloc`s the 7744-byte `model_t`, builds the DMA-queue luts, IO queues, activation-table storage, and instruction blocks, and — crucially — issues the first device `IOCTL`, `MEM_ALLOC_V2MT64` (#102), to carve the subgraph's SBUF/HBM, then `dmem_buf_copyin` `@0x229820` DMAs weights/tables/instructions into HBM. **Consumes** a `kbin`; **produces** a staged per-core `model_t` with device-resident data. Owners: [Compute-Resource Build](compute-resource-build.md), [Memory Planning](memory-planning.md).

### S5 — Relocate + ready (`kmgr_load_nn_nc` `@0xde280` tail)

Relocation runs inside the S4 build (see the GOTCHA above), not as a separate pass. Once every subgraph is staged, the orchestrator registers the userspace `dlr_model` in `dlr_model_set` `@0xc5c720` under a fresh `h_nn`, copies the IO maps and UUID back, folds the trace UUID and MAC count, builds the NDS datastore model-node objects, sets `state = 4` (READY), and releases or frees the TNC cache slot. `nrt_load_util` then `calloc`s the 284-byte `nrt_model_t` and returns it. On failure anywhere from S3 on, the unwind path tears down partially-built `model_t`s (`model_free` `@0x3055f0`) and rolls back already-staged subgraphs. Owner: [Load Pipeline](load-pipeline.md).

---

## The in-memory artifact model

A reimplementer's most likely wrong assumption is that "KBIN" names an on-disk file inside the NEFF, symmetric with the JSON manifests. It does not.

> **QUIRK —** there is **no monolithic `.kbin` file** in a NEFF and none on disk at any point. The on-disk artifacts are JSON manifests plus raw `<eng>.bin` blobs. "KBIN" is an **in-memory** product of lowering: `gen_kbin` `@0x4af930` materializes a **424-byte `kbin` record per subgraph** (ordinal 7153) from the `mla_resources` parser working set; there are `nkbin` of them, one per subgraph == one per NeuronCore. The device-resident form is the per-core **`model_t`** (7744 B = `0x1E40`), stood up by `kbl_model_add` `@0x3058e0` — also never serialized to a file. The NEFF carries the *inputs* to lowering; the `kbin` and `model_t` are the *outputs*, and they live only in RAM and device DRAM.

The three records the loader juggles, and their distinct sizes and id spaces, are:

| Record | Size | Lives in | Keyed by | Role |
|---|---|---|---|---|
| `kbin` | 424 B (`0x1A8`) | host RAM (one per subgraph) | array index in `kelf_nn_t.kbin[]` | device-binary descriptor; lowering output, staging input |
| `model_t` | 7744 B (`0x1E40`) | per physical core | `h_model` (= `tdrv_get_unique_h_model()`) | the tdrv device model (DMA rings, IO queues, instr blocks) |
| `nrt_model_t` | 284 B (`0x11C`) | host RAM (returned) | `h_nn` (= `dlr_model_set.last_nn_id++`) | the caller's opaque handle |

The two id spaces are independent monotone counters bridged by `dlr_kelf_model_t` (72 B): the caller's `h_nn` keys the userspace `dlr_model` in `dlr_model_set` `@0xc5c720`; `h_model` keys the per-core `model_t` inside the KBL layer. The full record graph (`nrt_model_t.h_nn` → `dlr_model` → `dlr_kelf_model_t.h_model` → per-core `model_t`) and the process-global TNC parse cache (`tnc` `@0xc5d880`, a single-slot UUID-keyed 4-state dedup) are derived in [Load Pipeline](load-pipeline.md).

---

## Part V page map

The ten Part-V pages decompose the journey above. Read this page for orientation, then the deep page for the stage you are reimplementing.

| Page | Owns | Anchor entry |
|---|---|---|
| [NEFF Container](container.md) | the 1024-B header ABI; in-memory gzip-tar walk; member-name routing | `neff_parse` `@0x4ca3f0` |
| [Metadata Schema](metadata-schema.md) | the TVM-relay `neff.json` / `kelf-a.json` / `def.json` JSON schemas (consumer side) | `parse_neff_json` `@0xe1d10` |
| [Section Taxonomy](section-taxonomy.md) | the member-name → section mapping (`<eng>.bin`, act-tables, sb_carveouts, profiling) | `kelf_load_from_neff` `@0x4c0870` |
| [The dtype System](dtype-system.md) | `nrt_dtype_t` and the I/O tensor descriptors (`io_node_info_t`, 144 B) | `parse_neff_json` `@0xe1d10` |
| [kelf2kbin](kelf2kbin.md) | the `mla_resources` (440 B) → `kbin` (424 B) lowering transform | `gen_kbin` `@0x4af930` |
| [KBIN Structures](kbin-structs.md) | the 424-B `kbin` interior (engine[5] / dma[32] / mem_ref_set / …) | `kbin` ordinal 7153 |
| [Static Memory Planning (mem_ref)](memory-planning.md) | the placement plan; the two `mem_ref` objects (build-side class tree vs on-disk record) | `mem_ref_to_addr` |
| [Compute-Resource Build (KBL)](compute-resource-build.md) | the per-core `model_t` constructor (DMA rings, IO queues, sequencer setup) | `kbl_model_add` `@0x3058e0` |
| [The Load Pipeline](load-pipeline.md) | the integration view: stage flow, three model records, two-handle id space, TNC cache, relocation | `kmgr_load_nn_nc` `@0xde280` |

> **NOTE —** the NEFF schema is canonically **owned by the producer** (`neuronx-cc` / the neuron-platform compiler), which emits these manifests; this wiki derives only what `libnrt.so` *consumes*, so a manifest field the runtime never reads is out of scope here. Where the consumer and producer disagree, the producer's published schema wins — link out, do not re-derive the producer side.

---

## Cross-References

- [NEFF Container](container.md) — the 1024-byte no-magic header and the in-memory gzip-tar unpack (stage S1)
- [Section Taxonomy](section-taxonomy.md) — how TAR member pathnames map to logical sections (stage S2)
- [kelf2kbin](kelf2kbin.md) — the JSON-working-set → in-RAM `kbin` lowering (stage S3); the source of the "no `.kbin` file" fact
- [Static Memory Planning (mem_ref)](memory-planning.md) — the load-time placement plan that makes inference zero-allocation (stage S4)
- [The Load Pipeline](load-pipeline.md) — the integration view that owns the deep stage flow, the model records, and the two relocation layers (stages S0–S5)
- [back to index](../index.md)
