# NEFF Section Taxonomy

> *All addresses, offsets, sizes, enum values, and key strings on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. `.text`/`.rodata` VMA equals file offset, so every `0x…` in those sections is an analysis VMA; `.data` globals (e.g. `NEURON_ENG_NAMES` `@0xc09600`) live past the `.data` start `0xc07e00`. Source TUs: `/opt/workspace/KaenaRuntime/kelf/{kelf.cpp, kelf2kbin.cpp}`. Other versions will differ.*
> *Evidence grade: **Confirmed (symbol-, string-, and enum-anchored)** — every handler is an `nm`/IDA-confirmed symbol at the cited address; every section-name literal is a `.rodata` string at the cited address; every section-type enum and struct offset is verbatim from `enums.json` / `structures.json`; the `dword_9DAB80` INSTR vector is byte-exact from the `.rodata` dump. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

A NEFF "section" is not a record in a binary section table — there is no such table ([NEFF Container](container.md) pins that there is no magic and no directory). The taxonomy lives at two layers, and this page owns the bridge between them. The **lower** layer is the unpacked TAR member set (`neff_t::files`, a `pathname → (buf,size)` RB-tree): the container walk has already turned the gzip-tar into named byte blobs, but a blob's *kind* is not stored anywhere — it is determined entirely by **which manifest key references it** plus its filename. The **upper** layer is the `def.json` schema ([NEFF Metadata Schema](metadata-schema.md)) that names those blobs. This page sits between: it documents the **recognized section kinds**, the **section-name → handler dispatch** that routes each `def.json` key to a dedicated `parse_one_*` builder, and the **in-memory struct each handler produces** — the members of the 424-byte `kbin` record that *are* the canonical section taxonomy.

Read it the way you read a linker's input-section classifier. The loader walks `def.json`'s top-level keys in a fixed order (`kelf_load_from_neff` `@0x4c0870`), and each recognized key is `at_key`-dispatched to a handler: `var` → `parse_one_variable`, `dma_queue` → `parse_one_dma_ring`, `replica_groups` / `src_target_pairs` / `ucode_lib` / `runtime_statebuffer_reservation` / `num_streams` / `fp8_conversion_config` each to their own builder, then a **per-engine loop** over the five engine names (`pe act pool dve sp`, resolved by `al_hal_tpb_get_tpb_eng_names` `@0x44bd00`, count `5` from `al_hal_tpb_get_tpb_eng_count` `@0x44bd50`) routes each engine's sub-JSON to `parse_one_engine_instr` `@0x4b7e30`. The handlers fill a 440-byte parser working set (`mla_resources`); `gen_kbin` `@0x4af930` lowers that into the `kbin` whose top-level members — `engine[5]`, `dma[32]`, `mem_ref_set`, `act_function_sets`, `dve_config`, `sb_carveouts`, `replica_groups`, `src_target_pairs`, `ucode_lib_set`, `cc_streams`, `fp8_conv_cfg`, `target` — are the sections.

The single fact a reimplementer must internalize is that the dispatch discriminator is a **string**, not an integer. There is no container-level `section_type` field. A member's kind is `(manifest key) + (filename)`; the named handlers carry the *real* type enums (`kbin_mr_type` for `var`, `kbin_dma_ring_type` for `dma_queue`, `kbin_sb_carveout_type` for carveouts, `al_hal_tpb_arch_type` for `target`). Two of the section-name compares are real `strcmp` (the engine name → type map in `al_hal_tpb_get_tpb_eng_type_from_str` `@0x44bd60`); most other discriminators are the LE-ASCII integer compares the metadata page decodes. This page pins every handler to a symbol+address and every section kind to its `def.json` key and its product struct.

For reimplementation, the contract is:

- **The recognized section kinds** — the container-member kinds (which manifest key + filename names each) and the parsed kinds (the `kbin` top-level members), and the policy for unrecognized members.
- **The section-name → handler dispatch** — the fixed `def.json` top-level key order in `kelf_load_from_neff`, the per-engine loop over the five engine names, and the `at_key`-string each handler keys off.
- **The product struct per handler** — which `mla_resources` region each fills and which `kbin` member it lowers to, with the per-section type enum.
- **The unknown/optional/version-gated policy** — engines absent from the NEFF get an empty placeholder (the SP-last invariant); `runtime_statebuffer_reservation` is conditionally required; the MARIANA-only synthesized `"all"` ucode lib.

| | |
|---|---|
| **def.json demux** | `kelf_load_from_neff` `@0x4c0870` — top-level key dispatch (5322-B containing fn) |
| **Engine-name set** | `NEURON_ENG_NAMES` `@0xc09600`; `{pe, act, pool, dve, sp}`; count `5` (`al_hal_tpb_get_tpb_eng_count` `@0x44bd50`) |
| **Name → eng-type** | `al_hal_tpb_get_tpb_eng_type_from_str` `@0x44bd60` — `strcmp`; `act=1 dve=3 pe=0 pool=2 sp=4`, else `5` |
| **Per-engine handler** | `parse_one_engine_instr` `@0x4b7e30` — instr/act-tables/dve-tables → `load_bin_file` |
| **Section product** | `kbin` — **424 B** (ord 7153); 12 top-level members = the canonical taxonomy |
| **Parser working set** | `mla_resources` — **440 B** (ord 6047/2763); filled by the handlers, lowered by `gen_kbin` `@0x4af930` |
| **Raw payload fetch** | `load_bin_file` `@0x4ae500` — `<sg>/<name>.bin` RB-tree lookup + `malloc`+`memcpy` |
| **Unknown-member policy** | no container `section_type`; kind = `(manifest key)+(filename)`; absent engine → empty placeholder |

---

## 1. The Two Section Layers

### Purpose

"Section" is overloaded in a NEFF, and a reimplementer who conflates the two layers will look for a section table that does not exist. This section fixes the vocabulary: a **container section** is a TAR member whose kind is implied by the manifest that names it; a **parsed section** is a member of the `kbin` record that the handlers build. The dispatch in §3 is the function that maps the first to the second.

### Container sections — TAR members named by manifests

The container walk has already produced `neff_t::files` (pathname → `(buf,size)`); see [NEFF Container](container.md). Nothing in that table records a member's *kind*. The kind is recovered at parse time: a member is "the graph manifest" because `kmgr_load_nn_nc` looks it up by the literal `"neff.json"`; it is "an engine instruction stream" because a `def.json` engine sub-JSON's `"instr"` key (read by `parse_instr` `@0x4aefe0`) names it; it is "a weight blob" because a `var` entry of `type=="file"` carries its `file_name`. There is **no** `section_type` integer at the container level.

| Container section kind | on-disk member | required | named by / handler @addr |
|---|---|---|---|
| graph manifest | `neff.json` (legacy `kelp.json` `@0x83f9cd`) | yes | `kmgr_load_nn_nc` `@0xde280` → `parse_neff_json` `@0xe1d10` |
| KELF descriptor | `<attrs.kelf>` (legacy `kelf-a.json`) | yes | `kelf::kelf::load` `@0x497dc0` |
| per-subgraph dir | `sg<N>/` | yes | `AE_IFDIR` — skipped as a tar entry (container §3) |
| per-subgraph section table | `sg<N>/def.json` | yes | `kelf_load_from_neff` `@0x4c0870` |
| engine sub-metadata (×5) | `sg<N>/<eng>.json` | per-eng | `json_parse_load_elements` `@0x4af3e0` → `parse_one_engine_instr` `@0x4b7e30` |
| engine instruction stream | `sg<N>/<instr-file>.bin` | per-eng | `parse_instr` `@0x4aefe0` → `load_bin_file` `@0x4ae500` |
| weights / constants | `sg<N>/<file_name>.bin` (or `.npy`) | opt | `parse_one_variable` `@0x4b36b0` (`type=="file"`) |
| act function-set tables (bkt+ctl) | `sg<N>/<bkt>.bin`, `<ctl>.bin` | per-act | `parse_one_engine_instr` (`"activation_function_sets"`) |
| dve tables (opcode/control/datapath) | `sg<N>/<table>.bin` | per-dve | `parse_one_engine_instr` (`"dve_tables"`) |
| gpsimd ucode library | `sg<N>/<ucode>.bin` | opt | `parse_one_ucode_lib` `@0x4b1610` |
| hlo stats (metadata) | `hlo_stats.json` `@0x83fa32` | opt | `get_model_hlo_stats` `@0x4c9c70` (mac_count only) |
| debug graph (metadata) | `sg<N>/wavegraph-bin.json` `@0x84986c` | opt | **dropped** in the tar walk (18-B suffix skip) |

> **QUIRK — the kind of a member is a property of the manifest, not the member.** The same raw `.bin` blob is "an instruction stream", "a weight tensor", or "an activation table" depending solely on which `def.json` key names it. A reimplementer cannot classify `neff_t::files` by inspecting the blobs; it must drive the classification from the `def.json` parse. There is no `enum section_type` to read off disk — the closest thing is the `kbin_mr_type` / `kbin_dma_ring_type` enums the *handlers* assign, after the fact, from the manifest string. (HIGH — no container `section_type` member in `structures.json`; every kind in the table above is recovered from a manifest key.)

### Parsed sections — the `kbin` top-level members

Once `def.json` is parsed and `gen_kbin` `@0x4af930` has lowered the working set, the canonical section taxonomy is the member list of the 424-byte `kbin` (ord 7153). These members **do** carry real type enums. Offsets verbatim from `structures.json`:

| `kbin` member | offset | type | fed by `def.json` key | handler | section-type enum |
|---|---|---|---|---|---|
| `engine[5]` | `+0` | `kbin_engine_t*[5]` | `pe`/`act`/`pool`/`dve`/`sp` | `parse_one_engine_instr` `@0x4b7e30` | `al_hal_tpb_eng_type` (5) |
| `dma[32]` | `+40` | `kbin_dma_t*[32]` | `dma_queue` (+ per-eng `dma`) | `parse_one_dma_ring` `@0x4b5f80` / `parse_one_dma_block` `@0x4bc620` | `kbin_dma_ring_type` (19) |
| `mem_ref_set` | `+296` | `kbin_mem_ref_set_t*` | `var` | `parse_one_variable` `@0x4b36b0` | `kbin_mr_type` (12) |
| `act_function_sets` | `+304` | (24 B inline) | `act.activation_function_sets` | `parse_one_engine_instr` | — |
| `dve_config` | `+328` | `kbin_dve_config_t*` | `dve.dve_tables` | `parse_one_engine_instr` | 4× `kbin_buffer_t` |
| `sb_carveouts` | `+336` | `kbin_sb_carveouts_t*` | `runtime_statebuffer_reservation` | `parse_sb_carveouts` (inlined) | `kbin_sb_carveout_type` (2) |
| `replica_groups` | `+344` | (16 B inline) | `replica_groups` | `parse_replica_groups` `@0x4b24b0` | — |
| `src_target_pairs` | `+360` | (16 B inline) | `src_target_pairs` | `parse_src_target_pairs` `@0x4b27f0` | — |
| `ucode_lib_set` | `+376` | (16 B inline) | `ucode_lib` (+MARIANA `"all"`) | `parse_one_ucode_lib` `@0x4b1610` | `kbin_ucode_lib` flags |
| `cc_streams` | `+392` | (8 B inline) | `num_streams` | (inline, `CCSTMI`) | — |
| `fp8_conv_cfg` | `+400` | (16 B inline) | `fp8_conversion_config` | `parse_fp8_conversion_config` (inlined) | — |
| `target` | `+416` | `al_hal_tpb_arch_type` | `kelf.target` | `parse_target` `@0x497c10` | `al_hal_tpb_arch_type` (6) |

The 12 members map 1:1 onto the `mla_resources` regions the handlers actually write (the metadata page §5 pins the `mla_resources` offsets). The `kbin` *interior* of each member (the leaf struct fields) is owned by [KBIN Structures](kbin-structs.md); this page owns the **routing** that fills them.

> **NOTE — `engine[]` is always five slots, even when the NEFF supplies fewer.** The per-engine loop iterates `al_hal_tpb_get_tpb_eng_count()` (`@0x44bd50`, returns the constant `5`) over `NEURON_ENG_NAMES` (`@0xc09600`). An engine key absent from `def.json` is filled with an empty placeholder rather than skipped (§4), so `engine[0..4]` are always populated and indexable by `al_hal_tpb_eng_type`. A reimplementer that builds a variable-length engine list will mis-index every downstream consumer that uses `engine[eng_type]`. (HIGH — loop bound `5` from `al_hal_tpb_get_tpb_eng_count`; placeholder branch confirmed below.)

---

## 2. The Section-Type Enums

The parsed sections carry the type enums a reimplementer must reproduce verbatim — the discriminator space each handler selects within. Values are from `enums.json` (member counts cross-checked: `kbin_dma_ring_type`=19, `kbin_mr_type`=12, `dma_mem_usage_type`=23, `kbin_dma_desc_op`=8 incl. invalid, `al_hal_tpb_arch_type`=6).

### Engine index — `al_hal_tpb_eng_type` (5)

`PE=0  ACT=1  POOL=2  DVE=3  SP=4` (`MAX=5` sentinel). This is the index into `kbin.engine[]` and `kbin.dma[].owner`. The name→type map is a real `strcmp` chain (not LE-ASCII), confirmed in `al_hal_tpb_get_tpb_eng_type_from_str` `@0x44bd60`: `"act"→1`, `"dve"→3`, `"pe"→0`, `"pool"→2`, `"sp"→4`, anything else `→5` (the sentinel). The `def.json` engine keys are exactly these five lowercase strings.

### `var` section kinds — `kbin_mr_type` (12)

`MR_INVALID=0  MR_SB=1  MR_BUFFER_STAGED=2  MR_BUFFER=3  MR_TMP_BUF=4  MR_INPUT=5  MR_OUTPUT=6  MR_PTR=7  MR_VIRTUAL_TMP_BUF=8  MR_LIST=9  MR_PTR_TABLE=10  MR_REMOTE=11`. Selected by the `var` entry's `type` string (the discriminator map is owned by [Metadata Schema](metadata-schema.md) §6; the `mem_ref` tree by [Memory Planning](memory-planning.md)).

### DMA ring kinds — `kbin_dma_ring_type` (19)

`ENG=0  H2T=1  H2T_SERVICE=2  H2T_COPY=3  COLLECTIVES=4  MODEL=5  DATA=6  IN=7  OUT=8  INDIRECT_MEMCPY=9  ACT_TBL=10  DYNAMIC_ACT_TBL=11  DVE_TBL=12  IOQ_SWITCHER=13  TOPSP_INIT=14  EMBEDDING_UPDATE=15  CUSTOM_OP=16  GENERIC=17  LAST=18`. Selected by the `dma_queue` entry's `owner` string in `parse_one_dma_ring`.

### Carveout kinds — `kbin_sb_carveout_type` (2)

`KBIN_SB_CARVEOUT_TYPE_INVALID=0  KBIN_SB_CARVEOUT_TYPE_EVTACCEL=1`. The only accepted `runtime_statebuffer_reservation` `type` is `"evtaccel"` → `1` (an 8-byte LE compare `0x6C65636361747665`; metadata §7).

### Target gen — `al_hal_tpb_arch_type` (6)

`INVALID=0  INVALID_1=1  SUNDA=2  CAYMAN=3  MARIANA=4  NUM=5`. Set from the KELF `target` string by `parse_target` `@0x497c10` (`"sunda"/"v2"→2`, `"cayman"/"v3"→3`, `"mariana"/"v4"→4`, `"*"→`present-HW arch). This is `kbin.target` `@+416`.

### Device-memory class — `dma_mem_usage_type` (23)

The class each parsed section is staged into at device time (the `dmem_alloc` usage argument): `GENERIC=0  INSTR_PE=1  INSTR_ACT=2  INSTR_POOL=3  IO=4  DRAM_SPILL=5  WEIGHT=6  NOTIFICATION=7  ACT_TABLE=8  INSTR_DVE=9  INSTR_SP=10  SCRATCHPAD=11  TENSOR=12  UCODE_LIB=13  POOL_STDIO=14  CC=15  SCRATCHPAD_NOT_SHARED=16  XT_CC=17  DMA_RING=18  DMA_RING_SPILL=19  DMA_RING_IO=20  DMA_RING_COLLECTIVES=21  MAX=22`.

> **QUIRK — the per-engine instruction usage class is a five-element lookup, not `INSTR_PE + eng_type`.** The `dword_9DAB80[5]` table (`.rodata @0x9DAB80`, byte-exact dump `01 00 00 00 02 00 00 00 03 00 00 00 09 00 00 00 0a 00 00 00` = `{1,2,3,9,10}`) maps `eng_type → usage_class`: `PE→INSTR_PE(1) ACT→INSTR_ACT(2) POOL→INSTR_POOL(3) DVE→INSTR_DVE(9) SP→INSTR_SP(10)`. The DVE and SP classes (9, 10) are **not** contiguous with PE/ACT/POOL (1,2,3) in the enum — `INSTR_DVE`/`INSTR_SP` sit at 9/10 with `IO`/`DRAM_SPILL`/`WEIGHT`/`NOTIFICATION`/`ACT_TABLE` (4–8) wedged between. A reimplementer that computes `INSTR_PE + eng_type` produces `INSTR_DVE`/`INSTR_SP` wrong for engines 3 and 4. Index the table, do not add. (HIGH — `.rodata` dump byte-exact; enum from `enums.json`.)

---

## 3. The Section-Name → Handler Dispatch

### Purpose

This is the heart of the page: the recognizer that turns each `def.json` top-level key into a built section. `kelf_load_from_neff` `@0x4c0870` reads the keys in a fixed order via `at_key`, routing each to a `parse_one_*` handler (several inlined). After the keyed handlers, a per-engine loop fills `engine[]` and the engine-local sections (act/dve tables, per-engine DMA). Each handler writes one `mla_resources` region.

### Entry Point

```text
construct_kbin (0x496d50)                        ── per-subgraph: init mla_resources, parse, gen_kbin
  └─ kelf_load_from_neff (0x4c0870)              ── def.json TOP-LEVEL DEMUX (at_key, fixed order)
       ├─ at_key "var"           → parse_one_variable      0x4b36b0   → MI (mem_ref_set; kbin_mr_type)
       ├─ at_key "check_var_ids" → gate: max(var_id)+1 == size; num_outputs != 0
       ├─ at_key "dma_queue"     → parse_one_dma_ring       0x4b5f80   → DI (dma[]; kbin_dma_ring_type)
       ├─ at_key "replica_groups"→ parse_replica_groups     0x4b24b0   → RGI            (opt)
       ├─ at_key "src_target_pairs"→ parse_src_target_pairs 0x4b27f0   → SRCTGTPI       (opt)
       ├─ [arch==MARIANA] synthesize default Q7 ucode lib "all" (flags=6)
       ├─ at_key "ucode_lib"     → parse_one_ucode_lib       0x4b1610   → UCODE_LIBS     (opt)
       │     └─ per fn: parse_one_ucode_lib_function 0x4b1180
       ├─ at_key "runtime_statebuffer_reservation" → parse_sb_carveouts (inlined) → SB_CARVE
       ├─ at_key "num_streams"   → CCSTMI.num_streams
       ├─ at_key "dma"           → parse_one_dma_block        0x4bc620   → per-block descriptors
       ├─ at_key "fp8_conversion_config" → parse_fp8_conversion_config (inlined) → FP8_CONV_CFG
       └─ FOR i in [0 .. al_hal_tpb_get_tpb_eng_count()==5):
            at_key(NEURON_ENG_NAMES[i] ∈ {pe,act,pool,dve,sp}) → <eng>.json filename
              └─ json_parse_load_elements 0x4af3e0  (load+parse the engine sub-JSON)
                   └─ parse_one_engine_instr 0x4b7e30
                        ├─ at_key "dma"        → parse_one_dma_block 0x4bc620   (per-eng DMA)
                        ├─ [eng=="act"] at_key "activation_function_sets" → load_bin_file ×2
                        │                 + "profile_meta_data" + "act_semaphore"
                        ├─ [eng=="dve"] at_key "dve_tables" → {opcode/control*/datapath}_table → load_bin_file
                        └─ ALL: parse_instr 0x4aefe0 → at_key "instr" → load_bin_file 0x4ae500
            if eng key ABSENT: "Engine %s not found ... empty placeholder" → instr_set {0,0}
```

### The section taxonomy table

The full kind → handler → product map. `Conf.`: `HIGH` = handler symbol + section-name string both code-confirmed this pass; `MED` = device-routing inferred from naming, enum HIGH.

| section name (`def.json` key / member) | on-disk form | handler symbol @addr | resulting struct | Conf. |
|---|---|---|---|---|
| `var` | array of obj (some `type=="file"` → sibling `.bin`/`.npy`) | `parse_one_variable` `@0x4b36b0` | `kbin_mem_ref_set_t` (24 B) + `kbin_mem_ref_t[]` (152 B) | HIGH |
| `dma_queue` | array of obj | `parse_one_dma_ring` `@0x4b5f80` | `kbin_dma_t` (296 B) + `kbin_dma_ring_instance_t[]` (264 B) | HIGH |
| `dma` (top-level + per-engine) | array of obj | `parse_one_dma_block` `@0x4bc620` | `kbin_dma_desc_t[]` (5456 B each) | HIGH |
| `replica_groups` | array | `parse_replica_groups` `@0x4b24b0` | `kbin_replica_group_set_t` (16 B) → subgroups | HIGH |
| `src_target_pairs` | array | `parse_src_target_pairs` `@0x4b27f0` | `kbin_src_target_pairs_set_t` (16 B) | HIGH |
| `ucode_lib` (+ MARIANA `"all"`) | array; `library` names a `.bin` | `parse_one_ucode_lib` `@0x4b1610` | `kbin_ucode_lib_t` (40 B) | HIGH |
| `runtime_statebuffer_reservation` | array of carveout obj | `parse_sb_carveouts` (inlined in `@0x4c0870`) | `kbin_sb_carveouts_t` (8 B) + `kbin_sb_carveout_t` (40 B) | HIGH |
| `num_streams` | uint | inline (`CCSTMI`) | `kbin_cc_streams_t` (8 B) | HIGH |
| `fp8_conversion_config` | obj | `parse_fp8_conversion_config` (inlined) | `kbin_fp8_conv_cfg_t` (16 B) | HIGH |
| `kelf.target` (tier-2) | string | `parse_target` `@0x497c10` | `al_hal_tpb_arch_type` `@+416` | HIGH |
| engine `instr` (×5) | raw headerless `.bin` (`ninstr*64` B) | `parse_instr` `@0x4aefe0` → `load_bin_file` `@0x4ae500` | `kbin_engine_t` (320 B) | HIGH |
| `act.activation_function_sets` | 2 `.bin` (bkt+ctl) | `parse_one_engine_instr` `@0x4b7e30` | `kbin_act_function_set_t` (2192 B) | HIGH |
| `act.profile_meta_data` | inline JSON (per-func 76 B) | `parse_one_engine_instr` (`"profile_meta_data"`) | `kbin_act_profile_data_t[25]` | HIGH |
| `dve.dve_tables` | up to 5 `.bin` (opcode/control*/datapath) | `parse_one_engine_instr` (`"dve_tables"`) | `kbin_dve_config_t` (64 B; 4× `kbin_buffer_t`) | HIGH |
| `var type=="file"` (weights) | sibling `.bin`/`.npy` | `parse_one_variable` → `load_bin_file` / `numpy_load` | weight bytes → `mem_ref.buffer`, device class `WEIGHT(6)` | MED |
| `hlo_stats.json` | top-level JSON | `get_model_hlo_stats` `@0x4c9c70` | `mac_count` only (not a `kbin` member) | HIGH |
| `wavegraph-bin.json` | per-subgraph JSON | (dropped in tar walk) | — (never loaded) | HIGH |

> **NOTE — the engine instruction stream is a raw, headerless `.bin`; `ninstr` is derived, never stored.** `parse_instr` `@0x4aefe0` reads the `"instr"` key (a `strcpy(key, "instr")` site), fetches the named blob with `load_bin_file` `@0x4ae500`, and stores `{data, size}` into an `instr_set` (16 B: `+0 data`, `+8 size`). The count `ninstr` (in `kbin_engine_t` `@+260`) is computed as `size >> 6` (each TPB instruction is 64 bytes) — there is no `ninstr` field on disk. `parse_instr` guards the size with a `uint32` ceiling (`"Instruction size %zu for %s exceeds maximum supported size %u"` `@0x82bcb0`, max `0xFFFFFFFF`). A reimplementer that expects a header or a count prefix on the `.bin` will mis-parse; the blob is `ninstr*64` opaque bytes and nothing else.

### Algorithm — the recognizer and the per-section build

```c
// kelf_load_from_neff @0x4c0870 — def.json top-level recognizer. Field lookup via at_key @0x49ced0.
// Opens the member with json_parse_load_elements @0x4af3e0 (simdjson parse_into_document).
// Fills mla_resources (440 B); gen_kbin @0x4af930 later lowers it into kbin (the section set).
function kelf_load_from_neff(neff, sg_dir, def_json_name, out mla_res):
    eng_count = al_hal_tpb_get_tpb_eng_count()          // 0x44bd50 -> 5
    eng_names = al_hal_tpb_get_tpb_eng_names()           // 0x44bd00 -> &NEURON_ENG_NAMES (0xc09600)
    json_parse_load_elements(neff, sg_dir, def_json_name, parser, root)   // 0x4af3e0

    // --- keyed sections, fixed order ---
    e = root.at_key("var")                               // REQUIRED
    if !e: reject                                        // subgraph has no outputs
    for v in e.get_array():
        parse_one_variable(neff, mla_res, sg_dir, name(v), v)   // 0x4b36b0 -> MI (kbin_mr_type)

    if e = root.at_key("check_var_ids"):                 // sanity gate
        require max(var_id)+1 == nmem                    // "Unexpected var_ids set! max=%u, size=%lu"
        require mla_res.num_outputs != 0                 // else "...no Outputs"

    if e = root.at_key("dma_queue"):
        for q in e.get_array(): parse_one_dma_ring(mla_res.DI, qname(q), q)   // 0x4b5f80 (kbin_dma_ring_type)
    if e = root.at_key("replica_groups"):   parse_replica_groups(mla_res, e)     // 0x4b24b0 (opt)
    if e = root.at_key("src_target_pairs"): parse_src_target_pairs(mla_res, e)   // 0x4b27f0 (opt)

    if mla_res.target == AL_HAL_TPB_ARCH_TYPE_MARIANA:   // arch==4: synthesize default Q7 ExtISA lib
        add_default_ucode_lib(mla_res, name="all", flags=6)

    if e = root.at_key("ucode_lib"):
        for u in e.get_array(): parse_one_ucode_lib(neff, mla_res, sg_dir, u)    // 0x4b1610 (opt)

    if e = root.at_key("runtime_statebuffer_reservation"):
        parse_sb_carveouts(mla_res, e)                   // inlined; type=="evtaccel" only (kbin_sb_carveout_type)
    else if mla_res.target == CAYMAN && !env(NEURON_RT_ALLOW_LEGACY_NEFF):
        FATAL  // "Missing or corrupted runtime_statebuffer_reservation field in def.json ..." @0x82cf70

    if e = root.at_key("num_streams"):  mla_res.CCSTMI.num_streams = e.get_uint64()
    if e = root.at_key("dma"):
        for b in e.get_array(): parse_one_dma_block(mla_res, b)                  // 0x4bc620
    if e = root.at_key("fp8_conversion_config"): parse_fp8_conversion_config(mla_res, e)   // inlined

    // --- per-engine sections: 5 slots, SP last ---
    for i in [0 .. eng_count):                           // pe(0) act(1) pool(2) dve(3) sp(4)
        eng_name = eng_names[i]
        e = root.at_key(eng_name)                        // value = the <eng>.json member name
        if !e:                                           // engine absent -> empty placeholder
            warn "Engine %s not found in NEFF %s, using empty placeholder"   // @0x82d050
            mla_res.INS.instr_sets[eng_name] = {0, 0}
            continue
        parse_one_engine_instr(neff, mla_res, &eng_name, sg_dir, e)            // 0x4b7e30

// parse_one_engine_instr @0x4b7e30 — one engine's sections.
function parse_one_engine_instr(neff, mla_res, eng_name, sg_dir, eng_json_name):
    json_parse_load_elements(neff, sg_dir, eng_json_name, parser, eroot)       // 0x4af3e0

    if e = eroot.at_key("dma"):                          // per-engine DMA descriptor blocks
        for b in e.get_array(): parse_one_dma_block(mla_res, b)                 // 0x4bc620

    if eng_name == "act":                                // ACT-table sections
        if t = eroot.at_key("activation_function_sets"):     // @0x8491d3
            load_bin_file(neff, sg_dir, bkt_name, &bkt_tbl, &bkt_sz)            // 0x4ae500
            load_bin_file(neff, sg_dir, ctl_name, &ctl_tbl, &ctl_sz)
        if p = eroot.at_key("profile_meta_data"): parse_profile(p)             // @0x849234, per-func 76 B
        if s = eroot.at_key("act_semaphore"):     mla_res.ACT_F.semaphore = s  // @0x8493c6

    if eng_name == "dve":                                // DVE-table sections @0x8493d4
        if dt = eroot.at_key("dve_tables"):
            for k in {"opcode_table","datapath_table","control_fast_table",
                      "control_slow_table","control_table"}:                    // @0x8493df..0x84941c
                if b = dt.at_key(k): load_bin_file(neff, sg_dir, b, &buf, &sz)  // 0x4ae500

    // ALL engines: the instruction stream
    parse_instr(neff, mla_res, eng_name, sg_dir, eroot)                        // 0x4aefe0

// parse_instr @0x4aefe0 — the raw instruction blob.
function parse_instr(neff, mla_res, eng_name, sg_dir, eroot):
    key = "instr"                                        // strcpy(key,"instr")
    if e = eroot.at_key(key):
        instr_file = e.get_string()                      // STRING = the raw .bin filename
        rc = load_bin_file(neff, sg_dir, instr_file, &data, &size)             // 0x4ae500
        if size > 0xFFFFFFFF:                            // uint32 ceiling
            error "Instruction size %zu for %s exceeds maximum supported size %u"   // @0x82bcb0
        mla_res.INS.instr_sets[eng_name] = {data, size}  // ninstr = size >> 6 (derived; 64 B/instr)
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `kelf_load_from_neff` | `0x4c0870` | the `def.json` top-level recognizer (fixed key order) | HIGH |
| `parse_one_variable` | `0x4b36b0` | `var` → `mem_ref` (`kbin_mr_type`) | HIGH |
| `parse_one_dma_ring` | `0x4b5f80` | `dma_queue` → `kbin_dma_t` (`kbin_dma_ring_type`) | HIGH |
| `parse_one_dma_block` | `0x4bc620` | `dma` blocks → `kbin_dma_desc_t[]` | HIGH |
| `parse_replica_groups` | `0x4b24b0` | `replica_groups` → `RGI` | HIGH |
| `parse_src_target_pairs` | `0x4b27f0` | `src_target_pairs` → `SRCTGTPI` | HIGH |
| `parse_one_ucode_lib` | `0x4b1610` | `ucode_lib` → `kbin_ucode_lib_t` | HIGH |
| `parse_one_ucode_lib_function` | `0x4b1180` | per-function ucode entry | HIGH |
| `parse_one_engine_instr` | `0x4b7e30` | per-engine sections (instr / act / dve / per-eng DMA) | HIGH |
| `parse_instr` | `0x4aefe0` | the `"instr"` raw-blob fetch (size-capped, `ninstr=size>>6`) | HIGH |
| `json_parse_load_elements` | `0x4af3e0` | open+parse a sub-JSON member (engine json, def.json) | HIGH |
| `load_bin_file` | `0x4ae500` | `<sg>/<name>.bin` RB-tree fetch + `malloc`+`memcpy` | HIGH |
| `al_hal_tpb_get_tpb_eng_count` | `0x44bd50` | engine count (returns `5`) | HIGH |
| `al_hal_tpb_get_tpb_eng_names` | `0x44bd00` | `&NEURON_ENG_NAMES` (`@0xc09600`) | HIGH |
| `al_hal_tpb_get_tpb_eng_type_from_str` | `0x44bd60` | engine name → `al_hal_tpb_eng_type` (`strcmp`) | HIGH |
| `parse_target` | `0x497c10` | `kelf.target` → `al_hal_tpb_arch_type` | HIGH |
| `gen_kbin` | `0x4af930` | lower `mla_resources` → `kbin` (the section set) | HIGH |

---

## 4. Section-Handling Policy — Optional, Gated, Unknown

### Purpose

The dispatch in §3 has three non-obvious policies a reimplementer will get wrong if they assume "every key is required and present": an absent engine becomes a placeholder, one carveout section is conditionally required, and one section kind is synthesized for one arch. These are the edge cases.

### Absent engine → empty placeholder (the SP-last invariant)

The per-engine loop does **not** require all five engine keys. When `at_key(eng_name)` misses, the loop logs `"Engine %s not found in NEFF %s, using empty placeholder"` (`@0x82d050`) and inserts an empty `instr_set {0, 0}` rather than failing. The result is that `kbin.engine[0..4]` are always populated — a NEFF that uses only `pe` and `act` still produces five engine slots, the unused three carrying zero-length instruction streams.

> **GOTCHA — the engine slots are dense and ordered, not sparse.** A reimplementer must allocate all five engine slots regardless of how many the NEFF supplies, in the fixed `pe, act, pool, dve, sp` order, because every downstream consumer indexes `engine[al_hal_tpb_eng_type]` (e.g. the device staging uses `dword_9DAB80[eng_type]` for the instruction memory class, §2). Skipping an absent engine shifts the indices and corrupts the lookup. The placeholder is `{0,0}`, not omitted. (HIGH — placeholder branch + warn string `@0x82d050` confirmed in `kelf_load_from_neff`.)

### `runtime_statebuffer_reservation` is conditionally required

The `sb_carveouts` section (`runtime_statebuffer_reservation`) is **required on a CAYMAN (v3) target** unless `NEURON_RT_ALLOW_LEGACY_NEFF=1` is set: absence FATALs with `"Missing or corrupted runtime_statebuffer_reservation field in def.json and NEURON_RT_ALLOW_LEGACY_NEFF=1 is not set. Potentially a legacy neff without proper compiler support"` (`@0x82cf70`). On other targets it is optional. The discriminator inside each carveout is the `type` string, and the only accepted value is `"evtaccel"` (`KBIN_SB_CARVEOUT_TYPE_EVTACCEL=1`); any other value rejects with `"Invalid runtime_statebuffer_reservation type %s"` (`@0x82d020`, in the inlined `parse_sb_carveouts` body).

> **NOTE — couple the carveout's required-ness to the tier-2 `target`, not to a universal flag.** A reimplementer that treats `runtime_statebuffer_reservation` as either always-optional or always-required will either accept a broken CAYMAN NEFF or reject a valid legacy/other-arch one. The presence requirement is `(target == CAYMAN) && !NEURON_RT_ALLOW_LEGACY_NEFF` — a three-way condition, gated by the `target` parsed in tier 2 ([Metadata Schema](metadata-schema.md) §5). (HIGH — both error strings + the gate confirmed in `kelf_load_from_neff`.)

### MARIANA-only synthesized `"all"` ucode lib

On a MARIANA (arch==4) target, the demux synthesizes a default Q7/GPSIMD Extended-ISA ucode library named `"all"` (flags `6`) before reading the `ucode_lib` key, so the ucode-lib set is non-empty even when `def.json` declares none. This is **not** a sixth engine — `q7` is a default ucode lib for the existing engines, and `al_hal_tpb_get_tpb_eng_count()` remains `5` on MARIANA. The `ext_isa_ucode_lib_def` key (`@0x84963b`) can override the default lib's function set (max 18 ops; [Metadata Schema](metadata-schema.md) §5).

> **CORRECTION (W2-NEFF-SECTIONS) —** an early reading of the MARIANA `"all"` library as a sixth engine key is wrong. The engine count is the constant `5` returned by `al_hal_tpb_get_tpb_eng_count` `@0x44bd50` on every arch (SUNDA/CAYMAN/MARIANA); `"all"` is a synthesized **ucode-lib** entry (`kbin.ucode_lib_set`), not an `engine[]` slot. The per-engine loop iterates exactly `{pe, act, pool, dve, sp}`. (HIGH — `al_hal_tpb_get_tpb_eng_count` returns `5` unconditionally; `"all"` is added to `UCODE_LIBS`, not `INS`.)

### Members the loader does not consume

Several container members are present but never built into a section: `wavegraph-bin.json` is dropped in the tar walk (the 18-byte suffix skip, [NEFF Container](container.md) §3); `hlo_stats.json` is read only for `mac_count` (`get_model_hlo_stats` `@0x4c9c70`), not lowered into `kbin`; producer/profiler metadata (`info.json`, `hlo_metrics.json`, `<eng>.asm` disassembly) is never looked up by any `at_key` in the loader. The unknown-member policy is therefore *silent ignore*: a member no manifest key references is simply not in any `kbin` section. There is no "unknown section" error path because there is no section table to validate against.

---

## Related Components

| Name | Relationship |
|---|---|
| `kelf_load_from_neff` (`@0x4c0870`) | the `def.json` recognizer — the section-name → handler dispatch this page owns |
| `parse_one_engine_instr` (`@0x4b7e30`) | the per-engine section builder (instr / act-tables / dve-tables / per-eng DMA) |
| `gen_kbin` (`@0x4af930`) | lowers the handler-filled `mla_resources` into the `kbin` section set |
| `load_bin_file` (`@0x4ae500`) | the raw-blob fetch every `.bin`-backed section routes through |
| `NEURON_ENG_NAMES` (`@0xc09600`) | the five engine-name strings the per-engine loop iterates |

## Cross-References

- [NEFF Container (gzip-tar, No-Magic Header)](container.md) — the layer **below**: the tar walk that produces `neff_t::files`, the named-member set this page classifies, and the `wavegraph-bin.json` suffix skip
- [NEFF Metadata Schema (TVM-Relay JSON)](metadata-schema.md) — the layer **above**: the `def.json` consumed-key set, the discriminator-string → enum maps (var `type`, dma `owner`, carveout `type`), and the per-entry sub-schemas
- [The Load Pipeline (Parse → Build → Stage → Relocate)](load-pipeline.md) — where this dispatch sits (stage S2) and where each built section is staged to the device (the `dma_mem_usage_type` class)
- [KBIN Structures](kbin-structs.md) — the interior of every `kbin` section member (`kbin_engine_t`, `kbin_dma_t`, `kbin_mem_ref_t`, `kbin_act_function_set_t`, …) the handlers here fill
- [kelf2kbin: JSON → KBIN Lowering](kelf2kbin.md) — `gen_kbin`, the transform from the `mla_resources` working set this page populates to the 424-byte `kbin`
- [Static Memory Planning (mem_ref)](memory-planning.md) — where the `var` / `mem_ref_set` section is placed and resolved at load time
- [back to index](../index.md)
