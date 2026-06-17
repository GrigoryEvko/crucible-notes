# KBIN Structures

> *All struct ordinals, sizes, field offsets, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). Every size and offset is read from DWARF (`structures.json`, ordinals cited per struct) and cross-checked against the `dump`-method bodies that write the records (the `mla_resources.hpp` emitters at `0x4c3610..0x4c6410`). `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA. Source TUs: `/opt/workspace/KaenaRuntime/kelf/{kelf2kbin.cpp, mla_resources.hpp}`. Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF- and dump-anchored)** — every struct size/offset is a DWARF member; every payload is re-verified against the decompiled `*::dump` that populates it (e.g. `dma_desc_data::dump` `@0x4c5180`, `mem_ref::dump` `@0x4c3700`). Where a record's interior union is only partially field-decoded it is tagged MEDIUM in place. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

The **KBIN** is the in-memory device binary the Neuron engines run: a `kbin_t` (ordinal 7153, **424 bytes**, `calloc`'d `0x1A8`) synthesized once per subgraph at model load by `gen_kbin` ([kelf2kbin](kelf2kbin.md)). It is **not** a file — there is no `.kbin` member in any NEFF, and no ELF anywhere on the runtime path. It is a flat C aggregate of twelve top-level members: an `engine[5]` array of per-engine instruction blocks, a `dma[32]` array of DMA-ring descriptors, a `mem_ref_set` variable/tensor table, and inline activation / DVE / replica-group / src-target-pair / ucode-lib / SB-carveout / cc-stream / fp8 members, closed by a 4-byte `target` architecture tag. This page is the **struct reference** for that aggregate: it owns the leaf field layouts that [kelf2kbin](kelf2kbin.md) (the lowering *algorithm*) and [Section Taxonomy](section-taxonomy.md) (the `def.json` *dispatch*) both defer to.

Read the KBIN the way you read a relocatable object file's in-memory image after a linker has resolved it: every section is a contiguous record, every record is a header struct with a variable-length tail (`engine.instr[ninstr*64]`, `mem_ref_set.mem[nmem]`, `dma.ring_instances[]`, `ring_instance.desc[ndesc]`), and the only "relocation" still pending is the device-IP fix-up applied at stage time ([kelf2kbin §3](kelf2kbin.md)). The records are produced by a family of polymorphic `dump` emitters (`mem_ref::dump`, `dma_desc_data::dump`, `dve_config::dump`) that serialize the build-side C++ objects into these POD records; this page pins the *output* layout each `dump` writes, not the C++ objects (those are owned by [Memory Planning](memory-planning.md)) and not the 64-byte instruction record inside `engine.instr[]` (owned by [Instruction Record](../isa/instruction-record.md)).

The single fact a reimplementer must internalize is that **the KBIN is a tree of header+flexible-array records, every count derived, every header `calloc`'d with its tail inline**. `kbin_engine_t` is `calloc(1, instr_size + 320)` — a 320-byte header with the raw instruction blob spliced directly after it; `kbin_mem_ref_set_t` is `calloc(1, 152*nmem + 24)`; a DMA ring instance is `calloc(1, 5456*ndesc + 264)`. There is no separate allocation for the array tail and no pointer indirection to reach it — the tail lives at `sizeof(header)` past the header base. Get one header size wrong and every record after it in the same allocation is mis-addressed. The page documents `kbin_t` and each member struct as a focused offset table, then gives the annotated top-level `c` definition and the packing/version quirks.

For reimplementation, the contract is:

- **The `kbin_t` 424-byte member map** — the twelve members, their offsets, whether each is a pointer-to-record or a 8/16/24-byte inline sub-struct, and the `target != 0` validity gate.
- **The header+flexible-array allocation rule** — each major record is one `calloc(header + stride*count)`; the count is derived (`ninstr = byte>>6`, `nmem = var count`, `ndesc`, `ring_instance_count`), never stored on disk as a prefix.
- **The 152-byte `kbin_mem_ref` record** — the full bridge serialization, its fixed `char[16]` dtype / `uint64[8]` shape, and its four `mr_type`-selected union variants.
- **The `kbin_dma_t` ring / `kbin_dma_ring_instance_t` / `kbin_dma_desc_t` (5456 B) descriptor chain**, the activation `kbin_act_function_set_t` (2192 B), the `kbin_dve_config_t` (64 B), and the small inline sets (replica / src-target / ucode / carveout / fp8 / cc-stream).

| | |
|---|---|
| **Top-level record** | `kbin_t` — **424 B** (ord 7153), `calloc(0x1A8)` per subgraph |
| **Engine block** | `kbin_engine_t` — **320 B** hdr + `instr[ninstr*64]` (ord 6559); `calloc(size+320)` |
| **DMA ring** | `kbin_dma_t` — **296 B** hdr + `ring_instances[]` (ord 6558); `calloc(8*n+296)` |
| **Ring instance** | `kbin_dma_ring_instance_t` — **264 B** hdr + `desc[]` (ord 6573) |
| **DMA descriptor** | `kbin_dma_desc_t` — **5456 B** (ord 6049); `calloc(5456*ndesc+264)` per instance |
| **Mem-ref set** | `kbin_mem_ref_set_t` — **24 B** hdr + `mem[nmem]` (ord 6557); `calloc(152*n+24)` |
| **Mem-ref record** | `kbin_mem_ref` — **152 B** (ord 6351); fixed `dtype[16]` / `shape[8]`, 16-B union `@+136` |
| **Act function set** | `kbin_act_function_set_t` — **2192 B** (ord 6569); `profile_data[25]` (76 B each) |
| **DVE config** | `kbin_dve_config_t` — **64 B** (ord 6556); `4× kbin_buffer_t {ptr,size}` |
| **Top-level emitter** | `gen_kbin` `@0x4af930`; per-record emitters `0x4c3610..0x4c6410` |

---

## 1. `kbin_t` — The 424-Byte Top-Level Record

### Purpose

`kbin_t` is the root of the device-binary tree, one per subgraph, returned inside a `kelf_nn` (ord 6845, 24 B). Its twelve members fall into three shapes a reimplementer must distinguish: **pointer-to-record** members (`engine[5]`, `dma[32]`, `mem_ref_set`, `dve_config`, `sb_carveouts`) whose target structs are documented in the following sections; **inline sub-struct** members (`act_function_sets` 24 B, `replica_groups`/`src_target_pairs`/`ucode_lib_set`/`fp8_conv_cfg` 16 B each, `cc_streams` 8 B) that live *inside* the 424 bytes; and one scalar `target` (`al_hal_tpb_arch_type`, 4 B) that closes the struct and is the load-time validity gate. `gen_kbin` zeroes all 424 bytes first, then fills member-by-member in a fixed order ([kelf2kbin §2](kelf2kbin.md)).

### Layout — `kbin_t` (ord 7153, 424 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `engine` | `+0` | `kbin_engine_t*[5]` | per-engine instruction blocks, indexed by `al_hal_tpb_eng_type` | HIGH |
| `dma` | `+40` | `kbin_dma_t*[32]` | up to 32 DMA rings (`kbin_dma_ring_type`) | HIGH |
| `mem_ref_set` | `+296` | `kbin_mem_ref_set_t*` | the static memory plan (variable/tensor table) | HIGH |
| `act_function_sets` | `+304` | inline 24 B | `{set* @+304, num @+312, act_tbl_semaphore @+320}` | HIGH |
| `dve_config` | `+328` | `kbin_dve_config_t*` | the 4 DVE micro-tables | HIGH |
| `sb_carveouts` | `+336` | `kbin_sb_carveouts_t*` | state-buffer reserved regions | HIGH |
| `replica_groups` | `+344` | inline 16 B | `{groups* @+344, num_groups @+352}` | HIGH |
| `src_target_pairs` | `+360` | inline 16 B | `{pairs_set* @+360, num @+368}` | HIGH |
| `ucode_lib_set` | `+376` | inline 16 B | `{libs* @+376, num_libs @+384}` | HIGH |
| `cc_streams` | `+392` | inline 8 B | `{num_streams @+392}` (`uint64`; init `1`) | HIGH |
| `fp8_conv_cfg` | `+400` | inline 16 B | fp8 conversion controls (`@+400`) | HIGH |
| `target` | `+416` | `al_hal_tpb_arch_type` | arch gen; **`0` ⇒ `"Invalid target = %u"`**, reject | HIGH |

The five inline members occupy bytes `+304..+416`; only `engine`/`dma`/`mem_ref_set`/`dve_config`/`sb_carveouts` are out-of-line pointers. The `dma[32]` array spans `+40..+296` (`32 × 8`), so it abuts `mem_ref_set` at `+296` with no padding.

### Annotated definition

```c
// kbin_t — ord 7153, sizeof = 424 (0x1A8). gen_kbin @0x4af930 calloc(1, 0x1A8) per subgraph,
// zeroes it, then fills member-by-member. Inline sub-structs marked /* inline */ live in the 424 B;
// the rest are pointers to header+flexible-array records documented in §2..§9.
typedef struct kbin_t {
    kbin_engine_t*          engine[5];          // +0    PE/ACT/POOL/DVE/SP instruction blocks (§2)
    kbin_dma_t*             dma[32];            // +40   DMA rings (§4)
    kbin_mem_ref_set_t*     mem_ref_set;        // +296  static memory plan (§3)

    struct /* inline */ {                       // +304  kbin_act_function_sets_t (24 B)
        kbin_act_function_set_t* sets;          // +304
        size_t                   num_function_sets; // +312
        uint32_t                 act_tbl_semaphore; // +320
    } act_function_sets;

    kbin_dve_config_t*      dve_config;         // +328  4 DVE micro-tables (§6)
    kbin_sb_carveouts_t*    sb_carveouts;       // +336  state-buffer reservations (§7)

    struct /* inline */ {                       // +344  kbin_replica_group_set_t (16 B)
        kbin_replica_group_t* groups;           // +344
        size_t                num_groups;        // +352
    } replica_groups;

    struct /* inline */ {                       // +360  kbin_src_target_pairs_set_t (16 B)
        kbin_src_target_pairs_t* pairs_set;     // +360
        size_t                   num;            // +368
    } src_target_pairs;

    struct /* inline */ {                       // +376  kbin_ucode_lib_sets_t (16 B)
        kbin_ucode_lib_t* libs;                 // +376
        size_t            num_libs;              // +384
    } ucode_lib_set;

    struct /* inline */ {                       // +392  kbin_cc_streams_t (8 B)
        uint64_t          num_streams;          // +392  (initialized to 1)
    } cc_streams;

    kbin_fp8_conv_cfg_t     fp8_conv_cfg;       // +400  inline 16 B (§9)
    al_hal_tpb_arch_type_t  target;             // +416  0 ⇒ reject ("Invalid target = %u")
} kbin_t;                                        // sizeof 424
```

> **QUIRK — five of the twelve "sections" are inline sub-structs, not pointers; do not heap-allocate them.** `act_function_sets` (24 B), `replica_groups`/`src_target_pairs`/`ucode_lib_set`/`fp8_conv_cfg` (16 B each), and `cc_streams` (8 B) live *inside* the 424-byte `kbin_t`; only `engine`, `dma`, `mem_ref_set`, `dve_config`, and `sb_carveouts` are out-of-line pointers. The inline sets still *point* to heap arrays for their elements (e.g. `act_function_sets.sets` is a pointer at `+304`), but the set-descriptor itself is embedded. A reimplementer that allocates a `kbin_replica_group_set_t*` and stores a pointer at `+344` writes the pointer into what should be the inline `{groups, num_groups}` fields and corrupts `+352`. The offset table is the ground truth: a 16-B inline set occupies two 8-byte slots in `kbin_t`, an 8-B inline set occupies one. (HIGH — DWARF places `replica_groups` at `+344` as the 16-B set struct inline, not a pointer.)

> **NOTE — `target` at `+416` is the only field that fails the load.** `gen_kbin` writes `target = mla.TI.target` last; if it is `0` (`AL_HAL_TPB_ARCH_TYPE_INVALID`) it errors `"Invalid target = %u"` and returns `NRT_INVALID` before the kbin is usable. Every other member can be empty (an absent engine is a `{0,0}` placeholder, a model with no DMA has `dma[..] = NULL`), but a zero `target` is fatal. The valid set is `SUNDA=2 / CAYMAN=3 / MARIANA=4` ([Section Taxonomy §2](section-taxonomy.md)). (HIGH — `gen_kbin` `target==0` guard.)

---

## 2. `kbin_engine_t` — The Instruction Block

### Purpose

Each of the five `engine[]` slots is a `kbin_engine_t`: a 320-byte header immediately followed inline by the raw instruction blob. There is exactly one allocation per engine — `calloc(1, instr_byte_size + 320)` — so the blob starts at `header_base + 320`, not behind a pointer. The header carries the engine type, the engine name string (the `def.json` key), and the derived instruction count; the blob is a verbatim copy of the headerless `.bin` named by the engine sub-JSON.

### Layout — `kbin_engine_t` (ord 6559, 320 B header + inline blob)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `eng_type` | `+0` | `uint32` (`al_hal_tpb_eng_type`) | `PE=0 ACT=1 POOL=2 DVE=3 SP=4` — the *true* type, kept even when the slot index differs | HIGH |
| `name` | `+4` | `char[256]` | the `def.json` engine key (`"pe"/"act"/…`); `strncpy` cap 255 | HIGH |
| `ninstr` | `+260` | `uint32` | **derived** `instr_byte_size >> 6` (64 B/instr); never stored on disk | HIGH |
| *(pad)* | `+264` | 56 B | header padding to 320 | HIGH |
| `instr` | `+320` | `uint8[ninstr*64]` | verbatim blob copy ([Instruction Record](../isa/instruction-record.md)) | HIGH |

> **GOTCHA — the byte copy uses `size`, the count uses `size>>6`; they are not interchangeable.** The `calloc` reserves `size + 320` bytes and `memmove`s `size` bytes into `instr[]`, but `ninstr` is `size >> 6`. If a malformed blob is not a multiple of 64, the trailing `size & 63` bytes are copied into `instr[]` (the allocation holds them) yet are not counted in `ninstr`, so the sequencer never fetches them. Store the raw `size` for the copy and the derived `size>>6` for the count separately. The slot index in `engine[]` is the *alphabetical* map-iteration order (`act, dve, pe, pool, sp`), **not** `eng_type` order — read the type from `engine[slot]->eng_type` (`+0`), never infer it from the slot index. Both quirks are detailed in [kelf2kbin §2](kelf2kbin.md); here the point is the layout: `eng_type` at `+0` is the authoritative type, `ninstr` at `+260` is derived. (HIGH — `gen_kbin` `calloc(size+320)`, `ninstr = size>>6`, `memmove(instr, data, size)`.)

---

## 3. `kbin_mem_ref_set_t` and the 152-Byte `kbin_mem_ref`

### Purpose

The `mem_ref_set` (`kbin_t+296`) is the static memory plan: a 24-byte header carrying the two arena sizes and the variable count, followed inline by `nmem` fixed-152-byte `kbin_mem_ref` records (`calloc(1, 152*nmem + 24)`). Each record is the serialization bridge between the build-side C++ `mem_ref` tree and the load-side `mem_ref_t` POD — the layout below is what `mem_ref::dump` ([Memory Planning §5](memory-planning.md)) writes and `mem_ref_get_new_mr` reads. This page owns the *record layout*; the placement/resolve semantics are owned by [Memory Planning](memory-planning.md).

### Layout — `kbin_mem_ref_set_t` (ord 6557, 24 B + flexible array)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `local_scratchpad_size` | `+0` | `uint64` | planned per-TPB local arena bytes | HIGH |
| `shared_scratchpad_size` | `+8` | `uint64` | planned shared arena bytes | HIGH |
| `nmem` | `+16` | `uint32` | variable count (asserted `== var_id_to_mem_refs.size()`) | HIGH |
| `mem` | `+24` | `kbin_mem_ref[nmem]` | the plan; stride 152 | HIGH |

### Layout — `kbin_mem_ref` (ord 6351, 152 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `mr_type` | `+0` | `kbin_mr_type_t` (4 B + 4 pad) | discriminator (`MR_INVALID=0 … MR_REMOTE=11`) | HIGH |
| `name` | `+8` | `char*` | variable-name blob | HIGH |
| `size` | `+16` | `size_t` | byte size | HIGH |
| `alignment` | `+24` | `size_t` | required alignment (power-of-2) | HIGH |
| `var_id` | `+32` | `uint32` | model-unique id | HIGH |
| `dtype` | `+36` | `char[16]` | fixed dtype string; `dump` FATALs > 15 chars | HIGH |
| `shape` | `+56` | `uint64[8]` | fixed shape; `dump` FATALs > 8 dims | HIGH |
| `dtensor_md` | `+120` | `kbin_debug_tensor_md_t*` | device-print metadata, or NULL | HIGH |
| `buffer` | `+128` | `uint8*` | weight payload to copy-in, or NULL | HIGH |
| *(union)* | `+136` | 16 B | per-`mr_type` extra (below) | HIGH |

The `+136` anonymous union has four `mr_type`-selected variants (DWARF ords 17633–17636):

| Variant | `mr_type` | Fields | Confidence |
|---|---|---|---|
| `MR_PTR` | `7` | `+0 uint32 referenced_var_id` · `+4 bool io_ref` | HIGH |
| `MR_VIRTUAL_TMP_BUF` | `8` | `+0 uint64 offset` · `+8 kbin_backing_buf_t backing_buf` | HIGH |
| `MR_LIST` / `MR_PTR_TABLE` | `9` / `10` | `+0 uint32 length` · `+8 uint32* list` | HIGH |
| `MR_REMOTE` | `11` | `+0 uint32 remote_vtpb_rel_sg_id` · `+4 uint32 remote_var_id` | HIGH |

> **QUIRK — the C++ `dtype`/`shape` (variable-length) become fixed `char[16]`/`uint64[8]` on disk, with hard caps.** The build-side object carries `dtype` as a 32-byte `std::string` and `shape` as a `std::vector<uint64>`; `mem_ref::dump` (`@0x4c3700`) copies them into the fixed record fields and FATALs on overflow — `> 15` dtype chars (`"Device print dtype %s has %lu characters…"`) or `> 8` shape dims (`"…shape dimentions of up to %u…"`). A reimplementer must enforce both caps at serialize time; there is no length field, so `dtype` is NUL-terminated within 16 bytes and `shape` is exactly 8 `uint64` slots (trailing dims zero). `kbin_debug_tensor_md_t` (ord 6570, 516 B) = `char prefix_str[512]` + `uint32 pipe_id`. (HIGH — caps `{15,16}` / `{8}` in `mem_ref::dump`.)

---

## 4. `kbin_dma_t` — The DMA Ring

### Purpose

A `kbin_dma_t` (one of the `dma[32]` slots) is a per-queue ring header followed inline by its ring instances. It carries the ring type, the owning engine, the queue-set count, two semaphore arrays (regular and pseudo-serialization, each capped at 32), and the DGE scratch addressing. Allocation is `calloc(1, 8*ring_instance_count + 296)`: the 296-byte header plus an inline array of `ring_instance_count` *pointers* to `kbin_dma_ring_instance_t`.

### Layout — `kbin_dma_t` (ord 6558, 296 B + inline pointer array)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `ring_type` | `+0` | `kbin_dma_ring_type_t` | one of 19 ring kinds (`ENG=0 … LAST=18`) | HIGH |
| `owner` | `+4` | `al_hal_tpb_eng_type` | the engine that owns the queue | HIGH |
| `queue_set_cnt` | `+8` | `uint32` | queue-set count | HIGH |
| `semaphore_cnt` | `+12` | `uint32` | active semaphores (≤32) | HIGH |
| `semaphores` | `+16` | `uint32[32]` | semaphore ids; `> 32` ⇒ `"Runtime supports maximum of %u semaphores per queue set"` | HIGH |
| `pseudo_serialization_semaphore_cnt` | `+144` | `uint32` | pseudo-serialization count (≤32) | HIGH |
| `pseudo_serialization_semaphores` | `+148` | `uint32[32]` | pseudo-serialization semaphore ids | HIGH |
| `dge_sb_scratch_addr` | `+276` | `uint32` | DGE state-buffer scratch base | HIGH |
| `dge_sb_scratch_size` | `+280` | `uint32` | DGE scratch size | HIGH |
| `flags` | `+284` | `uint32` | ring flags | HIGH |
| `ring_instance_count` | `+288` | `uint32` | inline-array length (asserted at fill) | HIGH |
| `ring_instances` | `+296` | `kbin_dma_ring_instance_t*[]` | inline pointer array | HIGH |

### Layout — `kbin_dma_ring_instance_t` (ord 6573, 264 B + inline desc array)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `name` | `+0` | `char[256]` | instance name; `strncpy` cap 255 | HIGH |
| `ndesc` | `+256` | `uint32` | descriptor count (asserted `i < num_descs`) | HIGH |
| *(pad)* | `+260` | 4 B | alignment to 264 | HIGH |
| `desc` | `+264` | `kbin_dma_desc_t[ndesc]` | inline descriptor array, stride **5456** | HIGH |

The instance is `calloc(1, 5456*ndesc + 264)`. The `5456` stride is the full `kbin_dma_desc_t` size — descriptors are the single largest record in the KBIN, dominated by their access-pattern payload (§5).

> **NOTE — two cap asserts frame the ring fill, at different levels.** `mla_resources.hpp:0x263` (`i < dma->ring_instance_count`) bounds the inline instance-pointer array of `kbin_dma_t`; `mla_resources.hpp:0x1E5` (`i < num_descs`) bounds the inline `desc[]` of each `kbin_dma_ring_instance_t`. A reimplementer must size both inline tails from their respective counts (`ring_instance_count` `@+288`, `ndesc` `@+256`) before writing into them — the `dump` emitters write through these arrays unconditionally up to the count. (HIGH — both asserts survive in `gen_kbin`'s inlined `dma_queue::dump` / `dma_ring_instance::dump`.)

---

## 5. `kbin_dma_desc_t` — The 5456-Byte Descriptor

### Purpose

`kbin_dma_desc_t` (ord 6049, **5456 B**) is the per-descriptor record inside a ring instance — the largest struct in the KBIN. It is a tagged record: an 8-byte `{desc_type, op}` head, then a large access-pattern payload union (5424 B) describing one source-to-destination CCE/DMA transfer (a `to` destination access-pattern plus a `from[]` array of source access-patterns, each a 3-D `{idx, off, num_dims, sizes[], steps[], dtype}` tensor descriptor), overlaid for `op == TRANSPOSE` / `op ∈ {FMA,ADD,MIN,MAX}` with a transpose / CCE-info sub-block near the union tail, closed by the block-id / function-name / priority tail. The interior is documented to the depth `dma_desc_data::dump` (`@0x4c5180`) reveals; the full union body is field-decoded only at its head and tail.

### Layout — `kbin_dma_desc_t` (ord 6049, 5456 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `desc_type` | `+0` | `kbin_dma_desc_type_t` | `DATA=0 / EVENT=1 / INC_SEMA=2` | HIGH |
| `op` | `+4` | `kbin_dma_desc_op_t` | CCE op: `COPY=0 FMA=1 ADD=2 MIN=3 MAX=4 TRANSPOSE=5` (`INVALID=-1`) | HIGH |
| *(payload union)* | `+8` | 5424 B | `to` AP + `from[]` AP array; transpose/CCE overlay near tail | MEDIUM |
| `block_id` | `+5432` | `uint32` | the descriptor block this desc belongs to | HIGH |
| `block_end_desc` | `+5436` | `bool` | marks the last desc in a block | HIGH |
| `reset_engine_round_robin` | `+5437` | `bool` | reset round-robin (set when `op==TRANSPOSE` or named function, arch > 2) | HIGH |
| `function_name` | `+5440` | `char*` | `strndup` of the block's function name (≤256), or NULL | HIGH |
| `priority_class` | `+5448` | `uint8` | descriptor priority | HIGH |

The payload-union interior, from `dma_desc_data::dump`: `desc->to` is a destination access-pattern (`{idx, off, num_dims, sizes[], steps[], dtype}`), `desc->from[i]` is the source access-pattern array (same shape, one per `from_var_id`), and for a compute op the transpose-info or CCE min/max/scale block is `_mm_loadu_si128`-copied into the union near its tail (descriptor byte ~5276..5412, just before `block_id`). The dtype of each access-pattern entry is a `kbin_dtype` resolved by the dtype map ([dtype System](dtype-system.md)).

> **QUIRK — the descriptor is 5456 bytes because each one embeds full 3-D access-patterns for source and destination, not a pointer to them.** Unlike the 16-byte hardware UDMA descriptor ([Descriptor Format](../dma/descriptor-format.md)), the *KBIN* descriptor is the compiler-level transfer spec the runtime later expands into hardware descriptors — it carries the symbolic `(var_id, offset, sizes[], steps[], dtype)` tensor access-patterns that `mem_ref_to_addr` resolves at translation time. The `from[]`/`to` access-patterns and the CCE/transpose overlay account for the 5424-byte union; a reimplementer must allocate the full `5456*ndesc` per ring instance even when most fields are zero. The exact internal field offsets of the union (the per-`op` overlay placement) are MEDIUM — the head (`desc_type`/`op`) and tail (`block_id`..`priority_class`) are DWARF-pinned, the union body is decoded from the `dump` `_mm_loadu_si128` stores rather than a member-by-member DWARF walk. (HIGH head/tail; MEDIUM union interior — `dma_desc_data::dump` `@0x4c5180`.)

---

## 6. `kbin_act_function_set_t` and `kbin_dve_config_t`

### Purpose

The activation and DVE engines carry micro-tables alongside their instruction streams. `act_function_sets` (`kbin_t+304`) is an inline 24-byte set descriptor pointing to an array of 2192-byte `kbin_act_function_set_t` records, each describing one activation bucket/control table pair plus up to 25 piecewise-linear profile descriptors. `dve_config` (`kbin_t+328`) points to a 64-byte `kbin_dve_config_t` — four `{buffer, size}` table pairs.

### Layout — `kbin_act_function_set_t` (ord 6569, 2192 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `name` | `+0` | `char[256]` | function-set name | HIGH |
| `bkt_tbl` | `+256` | `uint8*` | bucket-table blob (`load_bin_file`) | HIGH |
| `bkt_sz` | `+264` | `uint32` | bucket-table byte size | HIGH |
| `ctl_tbl` | `+272` | `uint8*` | control-table blob (`load_bin_file`) | HIGH |
| `ctl_sz` | `+280` | `uint32` | control-table byte size | HIGH |
| `profile_data` | `+284` | `kbin_act_profile_data_t[25]` | per-function PWL descriptors (76 B each) | HIGH |
| `num_functions` | `+2184` | `uint8` | active profile count (≤25) | HIGH |

`kbin_act_profile_data_t` (76 B) is the per-activation-function PWL table descriptor: `+0 func_id`, `+4 symmetry_point`, `+8 sym_invert_sign_point`, `+9 symmetry_opt_en`, `+10 sym_opt_use_neg_region`, `+12 exp_offset`, then the PWL control bases (`pwl_control_base_pos/neg`), the small/large pos/neg signal exponent thresholds and PWL controls (`+24..+44`), the special-value results (`+48 fnan`, `+52 fpinf`, `+56 fninf`, `+60 fzero`), the FMA constants (`+64 fma_const_0`, `+68 fma_const_1`), `+72 fma_indirection_src_sel`, `+73 imm_bias`. (HIGH offsets; field semantics MEDIUM beyond the offsets.)

### Layout — `kbin_dve_config_t` (ord 6556, 64 B)

Four `kbin_buffer_t` (`{uint8* buffer; size_t size}`, 16 B each), written by `dve_config::dump` (`@0x4c6410`):

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `opcode_table` | `+0` | `kbin_buffer_t` | DVE opcode micro-table | HIGH |
| `control_fast_table` | `+16` | `kbin_buffer_t` | fast control table | HIGH |
| `control_slow_table` | `+32` | `kbin_buffer_t` | slow control table | HIGH |
| `datapath_table` | `+48` | `kbin_buffer_t` | datapath micro-table | HIGH |

The `def.json` `dve.dve_tables` also accepts a fifth key, `control_table`; the 64-byte record holds only the four `kbin_buffer_t` slots above ([Section Taxonomy §3](section-taxonomy.md)).

---

## 7. The Small Inline Sets — Replica / Src-Target / Carveout

### Purpose

Three section kinds are collective / state-buffer metadata, each a small set-descriptor (inline in `kbin_t` for replica and src-target; a pointer for carveouts) leading to a heap array of fixed records.

### Layouts

| Struct | Ord / Size | Fields | Confidence |
|---|---|---|---|
| `kbin_replica_group_set_t` (inline `@+344`) | 16 | `+0 kbin_replica_group_t* groups · +8 size_t num_groups` | HIGH |
| `kbin_replica_group_t` | 6567 / 16 | `+0 kbin_replica_subgroup_t* subgroups · +8 size_t num_subgroups` | HIGH |
| `kbin_replica_subgroup_t` | — / 16 | `+0 uint32* participants · +8 size_t num_participants` | HIGH |
| `kbin_src_target_pairs_set_t` (inline `@+360`) | 16 | `+0 kbin_src_target_pairs_t* pairs_set · +8 size_t num` | HIGH |
| `kbin_src_target_pairs_t` | 6565 / 32 | `+0 rg_set · +8 kbin_src_target_pair_t* pairs · +16 size_t num_pairs · (+24 aux)` | MEDIUM |
| `kbin_src_target_pair_t` | — / 16 | `{src, target}` participant pair | MEDIUM |
| `kbin_sb_carveouts_t` (ptr `@+336`) | 6555 / 8 | `+0 size_t count · +8 kbin_sb_carveout_t[] carveouts` | HIGH |
| `kbin_sb_carveout_t` | 6260 / 40 | `+0 kbin_sb_carveout_type_t type · +8 u64 offset · +16 u64 size · +24 u64 start_partition · +32 u64 num_partitions` | HIGH |

> **NOTE — replica groups are a two-level nesting; src-target pairs embed a replica-group set.** A `kbin_replica_group_t` holds an array of `kbin_replica_subgroup_t`, each of which holds a `uint32[]` of participant ids — a group is a *set of subgroups of participants*. `kbin_src_target_pairs_t` (32 B) leads with a `rg_set` (a replica-group set) before its `pairs` array, so the collective src-target table re-uses the replica-group structure. The `kbin_src_target_pairs_t` field-level decode (the `+8`/`+16`/`+24` exact roles) is MEDIUM — the set sizes are DWARF; the 32-byte interior member roles are reconstructed from the parse/emit bodies (`enc_parse_src_target_pairs_info`, which takes a `kbin_src_target_pairs_t*`). (MEDIUM for the 32-B interior; HIGH for the set/subgroup/carveout layouts.)

---

## 8. `kbin_ucode_lib_t` and `kbin_cc_streams_t`

### Purpose

The ucode-lib set (`kbin_t+376`, inline 16 B) points to an array of 40-byte `kbin_ucode_lib_t` records — GPSIMD/Q7 micro-code libraries. On MARIANA a default `"all"` library (flags `6`) is synthesized even when `def.json` declares none ([Section Taxonomy §4](section-taxonomy.md)). The cc-streams member (`kbin_t+392`, inline 8 B) is a single `uint64 num_streams` (init `1`).

### Layout — `kbin_ucode_lib_t` (ord 6031, 40 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `content` | `+0` | `void*` | ucode image blob | HIGH |
| `content_size` | `+8` | `uint64` | image byte size | HIGH |
| `flags` | `+16` | `uint32` | lib flags (synthesized default = `6`) | HIGH |
| `cpu_id` | `+20` | `uint8` | target CPU id (multi-CPU Q7) | HIGH |
| `total_cpus` | `+21` | `uint8` | CPU count | HIGH |
| *(pad)* | `+22` | 2 B | alignment | HIGH |
| `function_set` | `+24` | `kbin_ucode_lib_function_set_t` | `{functions*, num_functions(u8)}` | HIGH |

Each ucode-lib kbin struct is `calloc`'d at `0x28` (40 B) per lib in `gen_kbin` (the failure path logs `"failed to allocate memory for ucode_lib(%d) kbin struct"`). `kbin_cc_streams_t` (ord 6560, 8 B) is the single `uint64 num_streams`.

---

## 9. `kbin_fp8_conv_cfg_t`

The fp8 conversion config (inline `@kbin_t+400`, ord 6044, 16 B) carries the floating-point-8 dynamic-range and saturation controls the CCE applies during dtype conversion:

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `fp8_range_extension` | `+0` | `bool` | enable extended fp8 dynamic range | HIGH |
| `emax_fp8_e4m3` | `+4` | `uint32` | max exponent for e4m3 | HIGH |
| `emax_fp8_e5m2` | `+8` | `uint32` | max exponent for e5m2 | HIGH |
| `ocp_sat` | `+12` | `bool` | OCP-style saturation | HIGH |
| `sis` | `+13` | `bool` | saturate-infinity-source (env `NEURON_RT_DBG_FP8_SIS` override) | HIGH |
| `sns` | `+14` | `bool` | saturate-NaN-source (env `NEURON_RT_DBG_FP8_SNS` override) | HIGH |

The `initialized` flag is seeded `0` by `construct_kbin` before the fp8 keys are parsed; `sis`/`sns` can be forced by environment variables independent of the `def.json` `fp8_conversion_config` ([Section Taxonomy §3](section-taxonomy.md)). fp8 *semantics* are owned by [fp8 dtype encoding](../isa/fp8-dtype-encoding.md).

> **CORRECTION (KBIN-1) —** the `kbin_fp8_conv_cfg_t` carries **no padding between `emax_fp8_e4m3` (`+4`) and `emax_fp8_e5m2` (`+8`)** — both are `uint32` at adjacent 4-byte slots, with the three bools packed at `+12/+13/+14`. The field order is `fp8_range_extension` (single bool `@+0`, three bytes pad to `+4`), the two `emax` words at `+4`/`+8`, then the saturation bools `ocp_sat`/`sis`/`sns` at `+12..+14`. A reading that places the bools before the `emax` words mis-offsets `emax_fp8_e5m2`. (HIGH — DWARF member offsets `{0,4,8,12,13,14}`.)

---

## Allocation-Size Reference

Every major KBIN record is one `calloc(header + stride*count)`; the count is always derived, never a disk prefix. The table a reimplementer sizes allocations from:

| Record | Allocation | Header | Stride × Count | Confidence |
|---|---|---|---|---|
| `kbin_t` | `calloc(1, 0x1A8)` | 424 (whole) | — | HIGH |
| `kbin_engine_t` | `calloc(1, size + 320)` | 320 | `1 × instr_byte_size` (blob) | HIGH |
| `kbin_dma_t` | `calloc(1, 8*n + 296)` | 296 | `8 × ring_instance_count` (ptrs) | HIGH |
| `kbin_dma_ring_instance_t` | `calloc(1, 5456*n + 264)` | 264 | `5456 × ndesc` | HIGH |
| `kbin_mem_ref_set_t` | `calloc(1, 152*n + 24)` | 24 | `152 × nmem` | HIGH |
| `kbin_ucode_lib_t` | `calloc(1, 0x28)` per lib | 40 (whole) | — | HIGH |

> **GOTCHA — every count is `size>>6`, `var_id_to_mem_refs.size()`, `ndesc`, or `ring_instance_count` — there is no count field on disk.** The KBIN tree stores counts *in* the header it allocates (`ninstr@+260`, `nmem@+16`, `ndesc@+256`, `ring_instance_count@+288`), but those are computed during lowering, not read from the NEFF. The NEFF blobs are headerless: the instruction `.bin` is `ninstr*64` opaque bytes with no prefix, the variable count is the `def.json` `var` array length, the descriptor count is the parsed block length. A reimplementer that looks for a length prefix in any KBIN-feeding blob will mis-parse; derive every count and write it into the record header. (HIGH — every count derivation is a `>>6` / `.size()` / array-length in the parse and emit bodies, never a `read_u32` from a blob.)

---

## Related Components

| Name | Relationship |
|---|---|
| `gen_kbin` (`@0x4af930`) | the emitter that `calloc`s and fills every struct on this page |
| `mem_ref::dump` family (`@0x4c3700..0x4c4f80`) | writes the 152-byte `kbin_mem_ref` records |
| `dma_desc_data::dump` (`@0x4c5180`) | writes the 5456-byte `kbin_dma_desc_t` records |
| `dve_config::dump` (`@0x4c6410`) | writes the 64-byte `kbin_dve_config_t` |
| `kelf_nn` (ord 6845, 24 B) | the `{nkbin, kbin[], sg_names[]}` wrapper that owns the `kbin_t` array |
| `free_kbin` (`@0x4b0da0`) | the teardown mirroring each `calloc` on this page |

## Cross-References

- [kelf2kbin: JSON → KBIN Lowering](kelf2kbin.md) — the `gen_kbin` lowering *algorithm* that produces every struct here; the per-member fill order and the `kbin_patch` IP relocation
- [NEFF Section Taxonomy](section-taxonomy.md) — the `def.json` key → handler dispatch that fills the `mla_resources` `gen_kbin` lowers into these structs, and the section-type enums (`kbin_mr_type`, `kbin_dma_ring_type`, `kbin_sb_carveout_type`, …)
- [Static Memory Planning (mem_ref)](memory-planning.md) — the build-side C++ `mem_ref` tree and the load-side `mem_ref_t` POD that bracket the 152-byte `kbin_mem_ref` record this page lays out
- [The 64-Byte Instruction Record Format](../isa/instruction-record.md) — the record inside `kbin_engine_t.instr[]`; the `ninstr = size>>6` derivation
- [The dtype System](dtype-system.md) — the `kbin_dtype` values carried in `kbin_mem_ref.dtype` and the DMA-descriptor access-pattern dtypes
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the *hardware* descriptor the 5456-byte `kbin_dma_desc_t` is later expanded into
- [back to index](../index.md)
