# kelf2kbin: JSON → KBIN Lowering

> *All addresses, offsets, struct ordinals, enum values, and assert line-numbers on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). `.text`/`.rodata` VMA equals file offset for the cited ranges, so every `0x…` in those sections is an analysis VMA. Struct offsets/sizes are read from DWARF (`structures.json`), enum values from DWARF (`enums.json`), and the lowering/patch bodies from the decompiled output and disassembly. Source TUs: `/opt/workspace/KaenaRuntime/kelf/{kelf.cpp, kelf2kbin.cpp, mla_resources.hpp}` and `/opt/workspace/KaenaRuntime/tdrv/kbin_patch.c`. Other versions will differ.*
> *Evidence grade: **Confirmed (symbol-, offset-, and assert-anchored)** — every lowering step is a decompiled `gen_kbin` line (`@0x4af930`) with its `mla_resources.hpp` assert pin; the relocation arithmetic is the `kbin_patch_*` family (`@0x2fb000..`) read from disassembly. The IP/branch translator `get_neff_ip` (`@0x2faee0`) and `kbin_patch_device_ip_to_neff_ip` (`@0x2fb3a0`) decompiled to no pseudocode and were walked at the instruction level. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

`kelf2kbin` is the **lowering pass** that turns the per-engine kernel program a NEFF carries — the "kelf" — into the device binary the engines run — the "kbin". Neither artifact is a file on disk. The on-disk container is a NEFF (gzip-tar) holding JSON manifests (`kelf-a.json`, `sg<N>/def.json`, per-engine sub-JSONs) that *name* sibling raw, headerless `.bin` blobs (the instruction streams, activation/DVE tables, ucode libs) inside the same tar. The kelf is that bundle in memory; the kbin is the 424-byte `kbin` struct (ordinal 7153) the loader synthesizes from it. So the "compiler" in `kelf2kbin.cpp` is not an ELF section/symbol/relocation parser — it is a JSON→struct translator. There is no monolithic `.kbin` file anywhere; the device binary is built fresh at every load. (The compiler-side kelf may be an ELF; the runtime never sees ELF sections.)

The lowering is a four-stage funnel, and this page owns the **TRANSLATE→EMIT** core plus the **RELOCATE** tail. The PARSE stage ([Section Taxonomy](section-taxonomy.md)) walks `def.json`'s keys into a 440-byte parser working set (`mla_resources`, ordinal 2763) via the `parse_one_*` handlers. `gen_kbin` (`@0x4af930`, TU `kelf2kbin.cpp`) then lowers `mla_resources` into the `kbin`: it zeroes the 424 bytes, fills the five engine slots (each a `kbin_engine_t` whose `instr[]` is a verbatim `memmove` of the raw blob and whose `ninstr` is *derived* as `byte_size >> 6`), fills up to 32 DMA rings, the variable/tensor `mem_ref_set`, the activation/DVE/replica/src-target/ucode/SB-carveout/fp8/cc-stream members, and finally writes `target`. After `gen_kbin` returns, a separate **RELOCATE** layer fixes up addresses: `kelf_resolve_vtpb_remote_variables` (`@0x499410`) rebinds remote-variable memory references across vTPB cores, and at stage/profile time the `kbin_patch_*` family (`@0x2fb000..`) performs the **device-IP ↔ NEFF-IP** translation — the branch-target relocation that maps an instruction address in the on-device buffer back to its index in the original NEFF stream.

The single fact a reimplementer must internalize is that the kbin is **emitted, not copied**. The engine instruction blob is the *only* part that survives byte-for-byte (an opaque `ninstr*64` `memmove`); everything around it — counts, types, sizes, the engine-type tag, the target gen — is re-derived during lowering. This page documents (1) the `gen_kbin` lowering pipeline as annotated C, pinned to `mla_resources.hpp` asserts; (2) the `kbin_patch` relocation model — the per-engine patch-location list and the `device_ip → neff_ip` arithmetic; and (3) the in-memory-only nature of the artifacts and the `_GLOBAL__sub_I_kelf2kbin` static initializer that builds the dtype map the parse stage consumes. The `kbin` *interior* (leaf field layouts) is owned by [KBIN Structures](kbin-structs.md); this page owns the lowering **algorithm**.

For reimplementation, the contract is:

- **The kelf→kbin staging model** — kelf = JSON + raw `.bin` blobs in a NEFF tar; kbin = the 424-byte struct synthesized at load; no `.kbin` on disk.
- **The `gen_kbin` lowering pipeline** — zero, then per-member fill, in the fixed order engine → dma → mem_ref → act → dve → replica/src-target/ucode/sb/fp8/cc → target, with the size derivations (`ninstr = size>>6`) and the invariants the asserts enforce.
- **The engine-fill verbatim-blob rule** — `calloc(1, size+320)`, `eng_type = name→type`, `strncpy(name,255)`, `ninstr = size>>6`, `memmove(instr, data, size)`; the instruction bytes are never decoded here.
- **The `kbin_patch` relocation** — the `{offset, count, type, section}` patch-location record, the INSERT/MODIFY/DELETE accumulation, and the `device_ip → neff_ip` translation that walks the patch list per section.

| | |
|---|---|
| **Lowering core** | `gen_kbin` `@0x4af930` (TU `kelf/kelf2kbin.cpp`); assert pin `kelf2kbin.cpp:0x801` |
| **Parser working set** | `mla_resources` — **440 B** (ord 2763); filled by PARSE, consumed by `gen_kbin` |
| **Lowered product** | `kbin` — **424 B** (ord 7153); one per subgraph (`calloc 0x1A8`) |
| **Engine slot** | `kbin_engine_t` — **320 B** hdr + `instr[ninstr*64]` inline; `ninstr = byte_size >> 6` |
| **Name → eng-type** | `al_hal_tpb_get_tpb_eng_type_from_str` `@0x44bd60`; `pe=0 act=1 pool=2 dve=3 sp=4`, else `5` (assert) |
| **Free path** | `free_kbin` `@0x4b0da0`; `free_kbin_replica_groups` `@0x4acf40` |
| **vTPB relocate** | `kelf_resolve_vtpb_remote_variables` `@0x499410` (post-`gen_kbin`, pre-stage) |
| **IP/branch patch** | `kbin_patch_*` `@0x2fb000..`; translator `kbin_patch_device_ip_to_neff_ip` `@0x2fb3a0` → `get_neff_ip` `@0x2faee0` |
| **Patch record** | `kbin_patch_location_t` — 16 B `{offset, count, type, section}` (DWARF) |
| **dtype map ctor** | `_GLOBAL__sub_I_kelf2kbin.cpp` `@0x79210` → `__static_init` `@0x78d80` builds `str_to_dtype_map` `@bss 0xcb04a0` |

---

## 1. The Staging Model

### Purpose

A reimplementer who reads "kelf2kbin" as an ELF-to-binary converter will look for a section table, a symbol table, and relocation entries that do not exist. This section fixes the model: both kelf and kbin are **in-memory** artifacts, the translation is JSON→struct, and the only on-disk inputs are JSON manifests plus raw headerless `.bin` blobs.

### What the artifacts are

The on-disk NEFF is a gzip-tar ([NEFF Container](container.md)) whose members the loader has already unpacked into `neff_t::files` (a `pathname → (buf,size)` RB-tree). Among those members:

- `kelf-a.json` — the top-level KELF descriptor: `{version, target, graphs[]}`, parsed by `kelf::kelf::load` `@0x497dc0`.
- `sg<N>/def.json` — the per-subgraph section table, parsed by `kelf_load_from_neff` `@0x4c0870` ([Section Taxonomy](section-taxonomy.md)).
- `sg<N>/<file>.bin` — the raw blobs the manifests name: per-engine instruction streams, activation bucket/control tables, DVE tables, ucode libraries, weight tensors.

There is **no** `.kbin` member, and no ELF. The "kbin" is the 424-byte `kbin` struct the loader builds at load time, one per subgraph, returned inside a `kelf_nn` (ord 6845, 24 B: `{nkbin, kbin[nkbin], sg_names[nkbin]}`). The instruction blobs inside it are headerless: a `.bin` of `ninstr` instructions is exactly `ninstr * 64` bytes ([Instruction Record](../isa/instruction-record.md)), with no count prefix — the loader derives `ninstr` by dividing the byte length by 64.

### Entry Point

The lowering sits at stage **S2→S3** of the load pipeline ([Load Pipeline](load-pipeline.md)), reached per-subgraph from `dlr_kelf_load`:

```text
dlr_kelf_load (0xe0830)                          ── NEFF → kelf_nn wrapper
  └─ kelf_load (0x49a6b0)                         ── alloc kbin[]/sg_names[]; per-subgraph loop
       ├─ kelf::kelf::load (0x497dc0)             ── PARSE kelf-a.json {version,target,graphs[]}
       └─ FOR each subgraph:
            kelf::kelf::get_kbin (0x498d10)        ── assert graphs.count(name)==1 (kelf.cpp:0xD9)
              └─ kelf::graph::construct_kbin (0x496d50)  ── init mla_resources; parse; lower
                   ├─ kelf_load_from_neff (0x4c0870)     ── PARSE def.json → mla_resources (440 B)
                   └─ gen_kbin (0x4af930)                ── TRANSLATE+EMIT → kbin (424 B)   ◀ THIS PAGE
       └─ (multi-vTPB) kelf_resolve_vtpb_remote_variables (0x499410)  ── RELOCATE remote vars
  ── later, at stage/profile time ──
  kbin_patch_* (0x2fb000..) / kbin_patch_device_ip_to_neff_ip (0x2fb3a0)  ── RELOCATE device↔neff IP
```

`construct_kbin` (`@0x496d50`) is the per-subgraph driver: it splits the `def.json` path on the last `/` into `{sg_dir, basename}`, zero-inits the stack `mla_resources`, seeds `TI.target = parent_kelf->target`, `CCSTMI.num_streams = 1`, and `FP8_CONV_CFG.initialized = 0`, then calls the PARSE half (`kelf_load_from_neff`) and the EMIT half (`gen_kbin`) in sequence.

> **QUIRK — there is no `.kbin` file and no ELF anywhere in the runtime path.** "kelf" and "kbin" are runtime in-memory tokens, not file extensions you will find in a NEFF. The container holds JSON + raw `.bin`; the kbin is `calloc`'d and filled at load. A reimplementer that searches a NEFF tar for a `.kbin` member, or that runs an ELF section/relocation parser over the kelf, will find nothing to parse — the lowering is `simdjson` DOM walks producing C structs, and the only relocation is the post-emit `kbin_patch` IP fix-up in §3. (HIGH — no `.kbin`/ELF member is referenced by any `at_key` or `neff_get_file_content` call in the kelf TUs; the only file fetches are JSON manifests and `.bin` blobs via `load_bin_file` `@0x4ae500`.)

---

## 2. The `gen_kbin` Lowering Pipeline

### Purpose

`gen_kbin` (`@0x4af930`, TU `kelf2kbin.cpp`) is the TRANSLATE+EMIT core: it consumes the filled `mla_resources` and writes the `kbin`. It is a straight-line per-member fill — there is no optimization, no reordering, no dead-section elimination — but it carries the *derivations* (counts, types, sizes) and the *invariants* (the `mla_resources.hpp` asserts) a reimplementer must reproduce exactly. The order is fixed; each step writes one `kbin` member.

### Algorithm — the lowering

```c
// gen_kbin @0x4af930 — TU kelf/kelf2kbin.cpp. Lowers mla_resources (440 B) into kbin (424 B).
// Decompiled landmarks cited as line@kelf2kbin.cpp.dec; assert pins are kelf2kbin.cpp / mla_resources.hpp.
function gen_kbin(string& subgraph_name, mla_resources& mla, kbin* k):
    assert(k != NULL)                              // "kbin"  kelf2kbin.cpp:0x801

    // --- (0) zero the whole product ---
    zero(k, 424)                                   // engine[0]@+0 = 0 ... target@+416 = 0

    // --- (1) ENGINE FILL: walk INS.instr_sets rb-tree (key = engine name string) ---
    // The map is ALPHABETICAL: act, dve, pe, pool, sp — NOT eng_type order (0..4).
    k_slot = 0
    for (name, is) in mla.INS.instr_sets:           // is = instr_set {void* data, u32 size}
        log("dump instructions for: %s size: %u", name, is.size)    // line 210
        eng = calloc(1, is.size + 320)              // line 211: 320-B hdr + size-B inline blob
        et  = al_hal_tpb_get_tpb_eng_type_from_str(name)            // line 212; 0x44bd60
        assert(et != 5)                             // mla_resources.hpp:0x16 "eng_name2type"
        eng->eng_type = et                          // +0   (kept even though slot index ≠ et)
        strncpy(eng->name, name, 0xFF)              // line 217: +4, 255-byte cap
        eng->ninstr = is.size >> 6                  // line 220: +260  DERIVED (64 B / instr)
        memmove(eng->instr, is.data, is.size)       // line 221: +320  VERBATIM blob copy
        k->engine[k_slot++] = eng                   // +0..+32  (k_slot = map-iteration index)

    // --- (2) DMA FILL: per DI.dma_queues entry (dma_queue::dump inlined) ---
    for (qname, q) in mla.DI.dma_queues:
        log("dump Queue: %s", qname)                // line 238
        dma = calloc(1, 8 * q.ring_instances + 296) // line 241: 296-B hdr + ptr[] inline
        // semaphore sets, <=32 each:
        //   "Runtime supports maximum of %u semaphores per queue set, found: %lu" (32)
        for ri in q.ring_instances:                 // dma_ring_instance::dump
            inst = calloc(1, 5456 * ri.ndesc + 264) // line 316: 264-B hdr + desc[] (5456 B each)
            assert(i < dma->ring_instance_count)    // mla_resources.hpp:0x263
            strncpy(inst->name, ri.name, 0x100)     // line 327
            for d in ri.descs:                      // dma_desc::dump (polymorphic)
                assert(i < num_descs)               // mla_resources.hpp:0x1E5
                lower_dma_desc(d, inst->desc[i])    // data/event/inc_semaphore arms
        // platform gates: >1 act_load queue rejected; HW-DGE count vs
        //   aws_hal_get_sdma_max_hw_dge_count()
        k->dma[..] = dma                            // +40..

    // --- (3) MEM_REF FILL: variable/tensor table (mem_ref_info::dump inlined) ---
    nvars = mla.MI.var_id_to_mem_refs.size()
    mrs = calloc(1, 152 * nvars + 24)               // line 394: 24-B hdr + mem_ref[] (152 B each)
    if !mrs: error "Failed to allocate memory for kbin memref set"   // line 397
    for vid, mr in mla.MI.var_id_to_mem_refs:
        mr->dump(mrs->mem[vid])                     // polymorphic: sp/io/buffer/pointer/
                                                    //   remote_variable/tmp_buf/virtual_tmp_buf/list
    assert(mrs->nmem == nvars)                      // mla_resources.hpp:0x49C  line 582
    k->mem_ref_set = mrs                            // +296

    // --- (4) the inline-member sections ---
    if mla.ACT_F.num_function_sets:                 // act_function_sets (24 B inline @+304)
        k->act_function_sets = { ACT_F.func_sets, ACT_F.num_function_sets, ACT_F.semaphore }   // line 590
    dve_config::dump(k)                             // 0x4c6410 -> kbin.dve_config @+328
    lower_replica_groups(mla, k)                    // -> +344 (16 B inline)
    lower_src_target_pairs(mla, k)                  // -> +360 (16 B inline)
    lower_ucode_libs(mla, k)                        // -> +376 (16 B inline; calloc 0x28 per lib, line 758)
    lower_sb_carveouts(mla, k)                      // -> +336
    lower_fp8_conv_cfg(mla, k)                      // -> +400 (16 B inline)
    k->cc_streams.num_streams = mla.CCSTMI.num_streams   // +392

    // --- (5) TARGET: the architecture gen the engines validate against ---
    if mla.TI.target == 0:                          // line 751
        error("Invalid target = %u", 0); return NRT_INVALID
    k->target = mla.TI.target                       // +416  al_hal_tpb_arch_type

    return NRT_SUCCESS
```

### The engine-fill verbatim-blob rule

The engine slots are the heart of the lowering and the only place a reimplementer can get the *bytes* wrong. Each slot is a single `calloc(1, size + 320)` (the 320-byte header inline with the variable-length blob; freed by one `free()` in `free_kbin` `@0x4b0da0`). The fields:

| Field | Offset | Value | Source |
|---|---|---|---|
| `eng_type` | `+0` | `al_hal_tpb_get_tpb_eng_type_from_str(name)` | line 212; `0x44bd60` |
| `name` | `+4` | `strncpy(name, 255)` — the `def.json` engine key | line 217 |
| `ninstr` | `+260` | `is.size >> 6` (**derived**, never stored on disk) | line 220 |
| `instr` | `+320` | `memmove(is.data, is.size)` — opaque `ninstr*64` bytes | line 221 |

The instruction bytes are copied **verbatim**: `gen_kbin` never decodes a single 64-byte record. Opcode interpretation, the pseudo-band, and the EXIT sentinel are all downstream concerns ([Instruction Record](../isa/instruction-record.md), [Pseudo-Instruction Lowering](../isa/pseudo-instruction-lowering.md)). The only thing `gen_kbin` knows about the blob is that it is a multiple of 64 bytes — and even that it does not check; `parse_instr` (`@0x4aefe0`, the PARSE side) caps the size at `uint32` but does not enforce `% 64 == 0`.

> **GOTCHA — the `engine[]` array slot index is NOT the engine type.** The engine fill walks `INS.instr_sets`, a `std::map<string, instr_set*>` keyed by the engine *name string*, so the iteration order is **alphabetical**: `act, dve, pe, pool, sp` → slots `0,1,2,3,4`. That is *not* the `al_hal_tpb_eng_type` order (`pe=0, act=1, pool=2, dve=3, sp=4`). The true engine type is preserved in `engine[slot]->eng_type` (`+0`), so a downstream consumer must read the type from the slot, not infer it from the slot index. A reimplementer that fills `engine[eng_type]` instead of `engine[map_index]` produces a different slot layout — harmless only if every consumer also indexes by `eng_type`, which is not guaranteed (the stage-side invariant relied on downstream is `engine[count-1]->eng_type == SP(4)`, which the alphabetical order happens to satisfy because `sp` sorts last). (HIGH — the fill loop reads `__pos + 32` = the map node's string key and `al_hal_tpb_get_tpb_eng_type_from_str` independently; the slot counter `k_slot` is a plain post-increment, line 211–221.)

> **NOTE — `ninstr` is derived by a right-shift, with no remainder check.** `eng->ninstr = is.size >> 6` (line 220) is an unconditional `>>6`. If a malformed `.bin` is not a multiple of 64, the trailing `size & 63` bytes are still `memmove`'d into `instr[]` (the `calloc` reserves `size`, not `ninstr*64`) but are *not* counted in `ninstr`, so the sequencer never fetches them. The blob length is the source of truth for the byte copy; `ninstr` is a derived view. A reimplementer must store the raw `size` for the copy and the derived `size>>6` for the count — they are not interchangeable. (HIGH — `calloc(1, size + 320)` line 211 uses `size`; `ninstr = size >> 6` line 220.)

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `gen_kbin` | `0x4af930` | TRANSLATE+EMIT: `mla_resources` → `kbin` | HIGH |
| `gen_kbin` (`.cold`) | `0x62c10` | unwind / OOM tail | HIGH |
| `free_kbin` | `0x4b0da0` | free `engine[0..count)`, dma, mem_ref_set, … | HIGH |
| `free_kbin_replica_groups` (`.part.0`) | `0x4acf40` | free replica-group set | HIGH |
| `al_hal_tpb_get_tpb_eng_type_from_str` | `0x44bd60` | engine name → `al_hal_tpb_eng_type` (`strcmp`; else `5`) | HIGH |
| `dve_config::dump` | `0x4c6410` | DVE tables → `kbin.dve_config` (`+328`) | HIGH |
| `mem_ref::dump` (family) | `0x4c3700..0x4c4f80` | polymorphic `mem_ref` → `kbin_mem_ref` | HIGH |
| `dma_desc::dump` (family) | `0x4c3610..0x4c5180` | polymorphic `dma_desc` → `kbin_dma_desc` | HIGH |
| `construct_kbin` | `0x496d50` | per-subgraph driver: path-split, init, parse, lower | HIGH |
| `kelf_load_from_neff` | `0x4c0870` | PARSE `def.json` → `mla_resources` (owned by [Section Taxonomy](section-taxonomy.md)) | HIGH |

The `dump` methods for `dma_queue`, `dma_ring_instance`, and `mem_ref_info` are **inlined** into `gen_kbin` — they have no standalone address. Their identity is recoverable only from their surviving asserts: `mla_resources.hpp:0x263` (`i < dma->ring_instance_count`), `:0x1E5` (`i < num_descs`), `:0x49C` (`mr_set->nmem == var_id_to_mem_refs.size()`). A reimplementer reading `gen_kbin`'s body sees their logic spliced into the main flow at the line numbers cited above, not as calls.

---

## 3. The `kbin_patch` Relocation

### Purpose

`gen_kbin` produces a kbin whose instruction stream still carries **NEFF-relative** branch targets. When the stream is staged into device memory ([Load Pipeline](load-pipeline.md)), instructions can be inserted (e.g. semaphore/DMA setup) or deleted, so an instruction's *device* address no longer equals its NEFF index. The `kbin_patch_*` family (`@0x2fb000..`, TU `tdrv/kbin_patch.c`) records those edits as a per-engine list of patch locations and provides the **device-IP → NEFF-IP** translation a profiler/debugger needs to map a fault address back to the source instruction. This is a *consumer* of the emitted kbin, distinct from `gen_kbin` proper, but it is the relocation half of the kelf→kbin story and is documented here.

### The patch-location record

The patch state is `kbin_patch_info_t` (80 B): one `kbin_eng_patch_t` per engine (5 of them), each a growable `{count, array_count, locations}` array of 16-byte `kbin_patch_location_t` records.

| Struct | Size | Layout | Source |
|---|---|---|---|
| `kbin_patch_info_t` | 80 | `kbin_eng_patch_t eng_patch[5]` | DWARF |
| `kbin_eng_patch_t` | 16 | `+0 int count · +4 int array_count · +8 kbin_patch_location_t* locations` | DWARF |
| `kbin_patch_location_t` | 16 | `+0 u32 offset · +4 u32 count · +8 kbin_patch_type_t type · +12 kbin_patch_section_t section` | DWARF |

The `count` field of a location (`+4`) is the instruction count of the edit: `kbin_patch_append` (`@0x2fb0f0`) stores `byte_count >> 6` into it (the same 64-byte/instruction divide as the engine fill). The two enums that classify a patch:

```text
kbin_patch_type_t   : INSERT=0  MODIFY=1  DELETE=2          (enums.json)
kbin_patch_section_t: PREAMBLE=0  POSTAMBLE=1  MAIN=2  FUNCTION=3
```

The append/extend/grow mechanics are unremarkable doubling-array bookkeeping: `kbin_patch_allocate` (`@0x2fb000`) `calloc`s `array_count` 16-byte slots; `kbin_patch_append` (`@0x2fb0f0`) doubles on overflow; `kbin_patch_extend` (`@0x2fb240`) concatenates one engine's list onto another, growing to the next power of two (`next_power_of_two`) and asserting `dst->count <= dst->array_count` (`kbin_patch.c:0x6E`); `kbin_patch_add_base_offset` (`@0x2fb350`) bumps every location's `offset` by a base — the only field that is address-relative.

### Algorithm — `device_ip → neff_ip`

The translator is `kbin_patch_device_ip_to_neff_ip` (`@0x2fb3a0`); the inner walk is `get_neff_ip` (`@0x2faee0`). Both decompiled to no Hex-Rays output and were reconstructed from disassembly. The model: a device instruction address is classified into a *section* (MAIN buffer or one of the per-function buffers), then the patch list for that section is replayed to accumulate the NEFF index.

```c
// kbin_patch_device_ip_to_neff_ip @0x2fb3a0 (TU tdrv/kbin_patch.c). Reconstructed from disasm.
// model: H_MODEL mod; eng = esi; device_ip = rdx; out neff_ip.
function device_ip_to_neff_ip(model, eng_type, device_ip, out neff_ip):
    ep = model.eng_patch[eng_type]                 // patches @ (eng_type+0x18A)<<4 + model
    n  = ep.count;  locs = ep.locations
    // (a) find which kbin_patch_section the device_ip lives in, MAIN(2) first:
    sect = find_first(locs[0..n), section == MAIN(2))         // 0x2fb3eb loop
    main = model.main_instruction_buffer                       // [mod+0x1988 + sect*8]
    first_main = main.base - 0x40                              // last_main = first_main + len
    if first_main <= device_ip < main.base + main.len:         // in MAIN
        neff_ip = get_neff_ip(locs, n, sect_patch_first,
                              base = main.base, first = first_main, device_ip)   // 0x2faee0
        return SUCCESS
    // (b) otherwise probe the per-FUNCTION buffers (section == FUNCTION(3)):
    if model.function_buffer && first_func <= device_ip < last_func:
        sect_fn = find_first(locs[0..n), section == FUNCTION(3))
        // neff_ip = neff_ip(function_buffer) + neff_ip(its MAIN base)   (two get_neff_ip calls, 0x2fb5d2/0x2fb5f9)
        neff_ip = get_neff_ip(fn_args...) + get_neff_ip(main_args...)
        return SUCCESS
    error("device_ip 0x%lx is out of bounds of main and function buffer")   // 0x2fb4db
    return FAIL

// get_neff_ip @0x2faee0. Walk the section's patch list, accumulating the NEFF instruction index.
// neff_ip starts at 1; address starts at base_address (the device address of NEFF instr #1).
function get_neff_ip(patches, npatch, first_patch, base_address, first_address, device_ip):
    neff_ip = 1                                    // r11d
    address = base_address
    for p in patches[first_patch .. npatch) where p.section == THIS_SECTION:
        if address >= device_ip: break             // reached the target region
        switch p.type:
            INSERT(0):                             // device has these, NEFF does not
                address += p.count << 6            // 0x2faf55: skip count*64 device bytes
                // neff_ip NOT advanced — inserted instrs have no NEFF index
            MODIFY(1):                             // 1-for-1 replacement
                address += 0x40;  neff_ip += 1     // 0x2faf98: one device instr, one NEFF instr
            DELETE(2):                             // NEFF has these, device does not
                neff_ip += p.count                // 0x2faf16: advance NEFF index, device unchanged
            default: assert_fail("Invalid patch type %d")    // kbin_patch.c:0xAC
    // (device_ip - address) bytes past the last patch are 1:1 instructions:
    neff_ip += (device_ip - address) >> 6          // 0x2faf70: add the un-patched run
    return neff_ip
```

> **QUIRK — the three patch types move the device and NEFF cursors asymmetrically.** `INSERT` advances the *device* address (`count<<6` bytes) but **not** the NEFF index — an inserted instruction (staging glue) has no source in the NEFF, so a device IP landing in it has no exact NEFF IP. `DELETE` advances the *NEFF* index (`+count`) but **not** the device address — the deleted instructions existed in the NEFF stream but were dropped on the way to the device. `MODIFY` advances both by one (a 1-for-1 rewrite). Only after replaying every patch before the target does the function add the straight `(device_ip - address) >> 6` run of un-patched 1:1 instructions. A reimplementer that treats all three types as a uniform `±count` shift will mis-map every IP after the first INSERT or DELETE. (HIGH — the three `switch` arms are explicit in `get_neff_ip` disasm at `0x2faf55` (INSERT, `shl esi,6`), `0x2faf98` (MODIFY, `+0x40`/`+1`), `0x2faf16` (DELETE, `+esi`); the default arm asserts at `kbin_patch.c:0xAC`.)

> **NOTE — the section probe order is MAIN, then FUNCTION, never PREAMBLE/POSTAMBLE.** `device_ip_to_neff_ip` searches for a `KBIN_PATCH_SECTION_MAIN(2)` location first, falls through to `KBIN_PATCH_SECTION_FUNCTION(3)`, and emits `"Failed to find section %d in the patch list"` if neither is present. `PREAMBLE(0)` and `POSTAMBLE(1)` are valid `kbin_patch_section` values (a kbin program is framed `preamble | main | function… | postamble`) but the IP translator only resolves addresses inside the MAIN and FUNCTION buffers — a device IP in the preamble/postamble glue is out of bounds for this query. (HIGH — the section constants `2` and `3` are the literal `cmp` operands in the `0x2fb3eb` / `0x2fb560` probe loops; the error string is at the `0x2fb43c` / `0x2fb596` `lea`.)

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `kbin_patch_allocate` | `0x2fb000` | `calloc` the per-engine location array | HIGH |
| `kbin_patch_free` / `_free_all` | `0x2fb070` / `0x2fb0c0` | free one / all engine lists | HIGH |
| `kbin_patch_append` | `0x2fb0f0` | append `{offset, count=byte>>6, type, section}`; doubling grow | HIGH |
| `kbin_patch_extend` | `0x2fb240` | concat one engine list onto another (`next_power_of_two`) | HIGH |
| `kbin_patch_add_base_offset` | `0x2fb350` | bump every location `offset` by a base | HIGH |
| `kbin_patch_device_ip_to_neff_ip` | `0x2fb3a0` | classify device_ip → section → translate (no Hex-Rays; disasm-walked) | MEDIUM |
| `get_neff_ip` | `0x2faee0` | replay a section's patch list → NEFF instruction index (disasm-walked) | MEDIUM |
| `kbl_model_get_kbin_patch_info` | `0x3076d0` | KBL/profile consumer that surfaces patch info | MEDIUM |
| `kelf_resolve_vtpb_remote_variables` | `0x499410` | vTPB remote-variable mem-ref rebind (regex match + offset rebind) | MEDIUM |

> **NOTE — `kelf_resolve_vtpb_remote_variables` is a separate relocation, run between `gen_kbin` and stage.** On a multi-vTPB model it walks the kbin's `mem_ref_set`, regex-matches remote-variable names, and rebinds each `var_id` to its memory offset across the vTPB core range (`"Failed to resolve remote variables for subgraphs %lu to %lu"`). Its body is large and regex-heavy; the role is HIGH but the field-level mechanics are MEDIUM (not exhaustively walked). It is a *memory*-reference relocation, orthogonal to the *IP* relocation in this section. (MEDIUM — symbol + call-shape confirmed at `@0x499410`; field-by-field rebind arithmetic not fully traced.)

---

## 4. The dtype-Map Static Initializer

### Purpose

`gen_kbin` does not type the DMA descriptors itself — the PARSE stage does, using a `std::map<string, kbin_dtype>` (`str_to_dtype_map`, `@bss 0xcb04a0`) that is built once at library load. The constructor lives in this TU's static-init thunk, so it is documented here; the map's *contents* and the three-layer dtype system are owned by [The dtype System](dtype-system.md).

### The static-init chain

```text
_GLOBAL__sub_I_kelf2kbin.cpp  (0x79210)            ── C++ static-ctor thunk for the TU
  └─ __static_initialization_and_destruction_0  (0x78d80)   ── builds str_to_dtype_map (19 entries)
       └─ str_to_dtype_map  @bss 0xcb04a0  : std::map<std::string, kbin_dtype>  (node 0x48)
```

The 19 string→`kbin_dtype` entries (`"float32"→5`, `"bfloat16"→7`, `"float8e3/4/5"→1/2/3`, the packed `_x4` aliases mapping to unsigned-int containers, …) are emitted by the static initializer at `@0x78d80` and consumed by `parse_one_desc_ap`/`parse_one_dma_block` during the PARSE half, keyed by `"<ap_name>_dtype"`. The full table, the value spaces, and the `kbin_dtype → SDMA → size` translators are in [The dtype System](dtype-system.md) §2–§4 — this page only pins *where the map is constructed* (the `kelf2kbin.cpp` static init) and *that the lowering relies on it being built before any subgraph is parsed*.

> **NOTE — the dtype map is a process-global built by the TU's static constructor, not by `gen_kbin`.** A reimplementer must construct the string→dtype table during library initialization (before the first `nrt_load`), not lazily inside the lowering — `parse_one_desc_ap` looks it up with no null guard on the map root. The `_GLOBAL__sub_I_kelf2kbin.cpp` thunk (`@0x79210`) is the runtime's standard C++ TU static-init entry; the actual map build is `__static_initialization_and_destruction_0` (`@0x78d80`). (HIGH — the thunk calls into `@0x78d80`, whose body inserts 19 entries into the `@bss 0xcb04a0` `std::map`.)

---

## Related Components

| Name | Relationship |
|---|---|
| `kelf_load_from_neff` (`@0x4c0870`) | the PARSE half — fills the `mla_resources` this page's `gen_kbin` lowers |
| `gen_kbin` (`@0x4af930`) | the lowering core this page documents |
| `kbin_patch_*` (`@0x2fb000..`) | the IP/branch relocation consumed at stage/profile time |
| `kelf_resolve_vtpb_remote_variables` (`@0x499410`) | the memory-reference relocation run between emit and stage |
| `free_kbin` (`@0x4b0da0`) | the teardown that mirrors the per-member `calloc`s |

## Cross-References

- [KBIN Structures](kbin-structs.md) — the interior of every `kbin` member (`kbin_engine_t`, `kbin_dma_t`, `kbin_mem_ref_t`, …) the leaf field layouts this page's lowering fills
- [NEFF Section Taxonomy](section-taxonomy.md) — the PARSE half: the `def.json` key → `parse_one_*` handler dispatch that populates the `mla_resources` `gen_kbin` consumes
- [The 64-Byte Instruction Record Format](../isa/instruction-record.md) — the record inside `kbin_engine_t.instr[]`; the `ninstr = size>>6` derivation and the opaque blob `gen_kbin` `memmove`s
- [The dtype System](dtype-system.md) — the `str_to_dtype_map` this TU's static init builds, and the `kbin_dtype → SDMA → size` translators
- [Pseudo-Instruction Lowering (SP / TopSP Builders)](../isa/pseudo-instruction-lowering.md) — the *instruction-level* rewrite the staged kbin undergoes, which is what the `kbin_patch` INSERT/DELETE records account for
- [The Load Pipeline (Parse → Build → Stage → Relocate)](load-pipeline.md) — where this lowering sits (S2→S3) and where the kbin is staged to the device
- [back to index](../index.md)
