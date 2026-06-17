# Static Memory Planning (mem_ref)

> *All addresses, offsets, sizes, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA; `.data.rel.ro` (the vtables) likewise has VMA == file offset. Source asserts name `/opt/workspace/KaenaRuntime/tdrv/mem_ref.c`. Other versions will differ.*
> *Evidence grade: **Confirmed (symbol- and reloc-anchored)** — all 10 vtables reloc-walked from `.data.rel.ro` and reconciled with the demangled `.symtab`; every struct offset is verbatim from the IDA `structures.json`; the resolve math is read from the `mem_ref_to_addr` disassembly. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

A NeuronCore inference does **no** device-memory allocation. Every tensor a model touches — weights, I/O buffers, scratch, pointer tables, collective shadow buffers — is given a **fixed device physical address once, at model load**, and from then on the executor only *resolves* a `(variable, offset)` pair to an absolute address by an add. The data structure that carries this plan from the compiler to the silicon is `mem_ref`. Think of it as the runtime's static-single-assignment of *placement*: the compiler computes lifetimes and emits an immutable allocation plan; the loader instantiates it exactly once; the executor reads it zero-allocation. This is the runtime analog of a stack-frame layout decided at compile time rather than a heap malloc'd per call.

The single most important fact on this page — the central finding a reimplementer must internalize — is that **`mem_ref` is two completely different objects that share a name**, and conflating them produces wrong field offsets everywhere:

- **(A) the build-side C++ class tree** — a 10-class single-inheritance polymorphic hierarchy rooted at `mem_ref` (72-byte base), each class a `{var_id, type, dtype, shape, …}` object with a **3-slot vtable** `[dump, ~D2, ~D0]`. It lives only inside the KELF→KBIN lowering (`kelf2kbin.cpp`); its one domain virtual, `dump(kbin_mem_ref&)`, **serializes** the object into the on-disk 152-byte `kbin_mem_ref` record. The objects are owned by a parser symbol table (`mem_ref_info`, three `std::map`s).
- **(B) the load-side runtime C struct** — `mem_ref_t`, a flat 208-byte (`0xD0`) POD `calloc`'d by `mem_ref_get_new_mr`. It carries the **resolved** `physical_address`, the backing `dmem_t*`, and an embedded `ht_node_t` so the executor can find it by `var_id`. `mem_ref_to_addr` reads exactly this struct.

The on-disk `kbin_mem_ref` (152 B) is the bridge: representation A writes it (`mem_ref::dump`), the loader reads it into representation B (`mem_ref_get_new_mr`). The page documents both representations field-by-field, the 10-class tree with each class's vtable role, and the **build → plan → resolve** chain as annotated C pseudocode anchored to symbols and addresses.

For reimplementation, the contract is:

- **The two representations** — the 72-byte C++ base + 9 derived layouts (build), and the 208-byte `mem_ref_t` (load/resolve) — and the 152-byte `kbin_mem_ref` on-disk record that bridges them, including its four per-type union variants.
- **The 10-class single-inheritance tree** and its uniform 3-slot vtable `[dump, complete-dtor, deleting-dtor]`, including the one class (`mem_ref_ptr_table`) that overrides *only* the destructors and inherits its parent's `dump`.
- **The discriminator** — `kbin_mr_type_t` (12 values, `MR_INVALID=0` … `MR_REMOTE=11`) — and which class / placement strategy each value selects.
- **The build → plan → resolve chain**: `parse_one_variable` builds the C++ objects → `mem_ref::dump` emits `kbin_mem_ref` → `dlr_kelf_stage`/`vtpb_info_shared_init_vtpb` instantiates the plan once, assigning each variable a fixed `physical_address` → `mem_ref_to_addr` resolves zero-alloc as `addr = offset + physical_address`.

| | |
|---|---|
| **Build-side object** | C++ `mem_ref` tree — 10 classes, 72-byte base (struct `mem_ref_0`), 3-slot vtable |
| **On-disk record** | `kbin_mem_ref` / `kbin_mem_ref_t` — **152 bytes** (`structures.json` ord 6351/17632) |
| **Load-side object** | `mem_ref_t` — **208 bytes** (`0xD0`), `calloc` by `mem_ref_get_new_mr` (ord 8614) |
| **Plan header** | `kbin_mem_ref_set_t` — 24 B `{local_scratchpad_size, shared_scratchpad_size, nmem}` + `mem[]`; hung off `kbin_t+296` |
| **Discriminator** | `kbin_mr_type_t` — 12 values, `MR_INVALID=0 … MR_REMOTE=11` (`enums.json`) |
| **Vtable base** | `_ZTV7mem_ref` `@0xbf8c60`; vptr = `_ZTV + 0x10` |
| **Build entry** | `parse_one_variable` `@0x4b36b0` → `mem_ref::dump` `@0x4c3700` |
| **Plan driver** | `dlr_kelf_stage` `@0xe0970` → `vtpb_info_shared_init_vtpb` `@0x3148e0` |
| **Stage / clone** | `mem_ref_get_new_mr` `@0x2fb640` · `mem_ref_copy_and_stage_mr` `@0x2fb780` |
| **Resolve** | `mem_ref_to_addr` `@0x22da60` (`tdrv/dma_ring.c`) — `addr = offset + physical_address` |
| **Source TU** | `/opt/workspace/KaenaRuntime/tdrv/mem_ref.c` (string `@0x813f38`) |

---

## 1. The Two Representations

### Purpose

`mem_ref` names two structurally unrelated objects that bracket the model lifetime. A reimplementer who reads "mem_ref" as one type will place `physical_address`, `dtype`, and `shape` at the wrong offsets — the build-side object is a vptr-headed C++ class whose first field is a vtable pointer, while the load-side object is a flat POD whose first field is a `char* name`. They are distinct IDA structures (the C++ base is `mem_ref_0`, ord 17828, 72 B; the runtime POD is `mem_ref_t`, ord 8614, 208 B), and the only thing they share is the on-disk record they bracket.

### The comparison

| Aspect | (A) Build-side C++ `mem_ref` | (Bridge) `kbin_mem_ref` | (B) Load-side `mem_ref_t` |
|---|---|---|---|
| IDA struct | `mem_ref_0` (ord 17828) + 9 derived | `kbin_mem_ref_t` (ord 6351 / 17632) | `mem_ref_t` (ord 8614) |
| Size | **72 B** base; 72–136 derived | **152 B** | **208 B** (`0xD0`) |
| First field | `+0` vptr (`_ZTV + 0x10`) | `+0` `mr_type` (4 B) | `+0` `const char* name` |
| Lifetime | KELF→KBIN lowering only | serialized into the NEFF/KBIN | model-resident until unstage |
| Allocation | `new` per variable, owned by `mem_ref_info` | array in `kbin_mem_ref_set_t.mem[]` | `calloc(0xD0)` by `mem_ref_get_new_mr` |
| Polymorphic | **yes** — 10 classes, 3-slot vtable | no (POD with anon union) | no (POD with anon union) |
| Resolved address | — (not yet placed) | — (compiler offsets only) | **`physical_address` @+32** |
| Hashtable node | — | — | `ht_node_t` @+160, keyed by `var_id` |
| Written by | `parse_one_variable` `@0x4b36b0` | `mem_ref::dump` `@0x4c3700` | `mem_ref_get_new_mr` `@0x2fb640` |
| Read by | `mem_ref::dump` (serialize) | `mem_ref_get_new_mr` (load) | `mem_ref_to_addr` `@0x22da60` |

> **QUIRK —** the build-side C++ `dtype` is a 32-byte `std::string` (`mem_ref_0+16`, SSO) and `shape` a 24-byte `std::vector<uint64>` (`mem_ref_0+48`); on disk and at load they become a **fixed `char[16]`** and a **fixed `uint64[8]`**. The conversion is lossy-on-overflow by design: `mem_ref::dump` (`@0x4c3700`) FATALs if `dtype` exceeds 15 characters (`"Device print dtype %s has %lu characters. Only %u …"`, max 16 incl. NUL) or `shape` exceeds 8 dimensions (`"Device print only supports shape dimentions of up to %u …"`). The validators use constants `{15, 16}` and `{8}` respectively (`functions.json` `0x4c3700`). A reimplementer must enforce both caps at serialize time, not silently truncate.

### Why two objects

The split is the load/run boundary. The C++ tree is rich because lowering needs typed behavior per variable kind (a pointer table serializes differently from an I/O tensor); polymorphism via `dump` keeps that logic local to each class. The runtime POD is flat because the executor's hot path (`mem_ref_to_addr`, called from nine instruction-translation sites — §6) must resolve an address in ~50 instructions with no virtual dispatch, no allocation, and one hashtable lookup. The on-disk record is the minimal serialization between them: 152 bytes, no pointers that survive a process boundary except the `name`/`buffer`/`list` blobs the loader re-materializes.

---

## 2. The 10-Class Build-Side Tree

### Purpose

The build-side hierarchy is a textbook single-inheritance tree: one root (`mem_ref`, RTTI kind `__class_type_info`) and nine derived classes (all `__si_class_type_info` — single public base, no multiple or virtual inheritance). Each class models one `kbin_mr_type_t` variable kind and overrides `dump` to serialize its extra fields into the `kbin_mem_ref` union. The tree is the structural ground truth for the on-disk format: the union variant a record carries is determined by which class authored it.

### The tree

```text
mem_ref                          (root; __class_type_info; 72 B)   MR base
  ├─ mem_ref_sp                  (si:mem_ref;            72 B)     MR_SB (state buffer)
  ├─ mem_ref_io                  (si:mem_ref;            88 B)     MR_INPUT / MR_OUTPUT
  ├─ mem_ref_buffer              (si:mem_ref;           136 B)     MR_BUFFER
  ├─ mem_ref_tmp_buf             (si:mem_ref;           128 B)     MR_TMP_BUF
  ├─ mem_ref_virtual_tmp_buf     (si:mem_ref;           136 B)     MR_VIRTUAL_TMP_BUF
  ├─ mem_ref_pointer             (si:mem_ref;            80 B)     MR_PTR
  ├─ mem_ref_list                (si:mem_ref;            96 B)     MR_LIST
  │    └─ mem_ref_ptr_table      (si:mem_ref_list;      120 B)     MR_PTR_TABLE  (2-level)
  └─ mem_ref_remote_variable     (si:mem_ref;            72 B)     MR_REMOTE
```

### Vtable map (3 slots each)

Every vtable is exactly 3 entries, reloc-walked from `.data.rel.ro` (the object's vptr is `_ZTV + 0x10`, past the 16-byte offset-to-top / typeinfo header): **slot 0 `dump(kbin_mem_ref&)`**, **slot 1 `~T` (D2, complete-object dtor)**, **slot 2 `~T` (D0, deleting dtor)**. All 30 slot addresses cross-checked against the demangled `.symtab`.

| `_ZTV` VMA | Class | Ord / size | slot0 `dump` | slot1 `~D2` | slot2 `~D0` |
|---|---|---|---|---|---|
| `0xbf8c60` | `mem_ref` | 17828 / 72 | `0x4c3700` | `0x4c3980` | `0x4c3b10` |
| `0xbf8c88` | `mem_ref_sp` | 6151 / 72 | `0x4c4710` | `0x4c39d0` | `0x4c3b60` |
| `0xbf8cb0` | `mem_ref_io` | 6152 / 88 | `0x4c4800` | `0x4c3a20` | `0x4c3bb0` |
| `0xbf8cd8` | `mem_ref_buffer` | 6153 / 136 | `0x4c4c90` | `0x4c3ca0` | `0x4c3f10` |
| `0xbf8d00` | `mem_ref_tmp_buf` | 6154 / 128 | `0x4c4b30` | `0x4c3d20` | `0x4c3f90` |
| `0xbf8d28` | `mem_ref_virtual_tmp_buf` | 6155 / 136 | `0x4c4e00` | `0x4c3da0` | `0x4c4010` |
| `0xbf8d50` | `mem_ref_pointer` | 6042 / 80 | `0x4c4a10` | `0x4c3a70` | `0x4c3c00` |
| `0xbf8d78` | `mem_ref_list` | 551 / 96 | `0x4c4f80` | `0x4c3e20` | `0x4c3e90` |
| `0xbf8da0` | `mem_ref_ptr_table` | 6041 / 120 | **`0x4c4f80`** | `0x4c4090` | `0x4c4130` |
| `0xbf8dc8` | `mem_ref_remote_variable` | 6156 / 72 | `0x4c4910` | `0x4c3ac0` | `0x4c3c50` |

> **QUIRK —** `mem_ref_ptr_table` does **not** override `dump`: its vtable slot 0 holds `0x4c4f80`, the *identical* address as `mem_ref_list`'s slot 0 (`mem_ref_list::dump`). It overrides only the two destructors (`0x4c4090` / `0x4c4130`). This is reloc-proven — the `.data.rel.ro` entry at `0xbf8db0` (= `_ZTV17mem_ref_ptr_table + 0x10`) relocates to `0x4c4f80`, byte-for-byte the same target as `0xbf8d88` (`mem_ref_list`'s vptr). A reimplementer must serialize a `MR_PTR_TABLE` with the **list** serializer; its second pointer list is *not* written by `dump` and is reconstructed at load (§4 below). There is no `mem_ref_ptr_table::dump` symbol in the `.symtab`.

### Base layout — `mem_ref_0` (72 B)

The C++ object base; written by the base ctor `mem_ref::mem_ref(uint, kbin_mr_type, string, vector<u64>)` `@0x4c58f0`.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `_vptr` | `+0` | `void**` | vtable pointer = `_ZTV<T> + 0x10` |
| `var_id` | `+8` | `uint32` | model-unique variable id; the hashtable key at load |
| `type` | `+12` | `kbin_mr_type_t` | the discriminator (§3) |
| `dtype` | `+16` | `std::string` (32 B, SSO) | element dtype string; capped to 15 chars at dump |
| `shape` | `+48` | `std::vector<uint64>` (24 B) | tensor shape; capped to 8 dims at dump |

### Per-class extra fields

Only the fields **beyond** the 72-byte base are listed; offsets verbatim from `structures.json`.

| Class | Size | Extra fields (offset / type) |
|---|---|---|
| `mem_ref_sp` | 72 | *(base only)* — MR_SB; placement is the SB AXI offset |
| `mem_ref_remote_variable` | 72 | *(base only)* — remote fields ride the kbin union |
| `mem_ref_pointer` | 80 | `+72 uint32 referenced_var_id`; `+76 bool io_ref` |
| `mem_ref_io` | 88 | `+72 size_t size`; `+80 uint32 alignment` |
| `mem_ref_tmp_buf` | 128 | `+72 size`; `+80 alignment`; `+88 debug_tensor_md (40 B)` |
| `mem_ref_buffer` | 136 | `+72 size`; `+80 alignment`; `+88 uint8* buffer`; `+96 debug_tensor_md` |
| `mem_ref_virtual_tmp_buf` | 136 | `+72 size`; `+80 uint64 offset`; `+88 kbin_backing_buf_t backing_buf`; `+96 debug_tensor_md` |
| `mem_ref_list` | 96 | `+72 std::vector<uint32> list` (24 B) — the row-index list |
| `mem_ref_ptr_table` | 120 | `[0..96) = mem_ref_list`; `+96 std::vector<uint32> list` — **second** uint32 vector |

`debug_tensor_md` (C++, ord 6046, 40 B) = `+0 std::string prefix (32 B)` + `+32 uint32 pipe_id` — the device-print metadata embedded in the buffer/tmp_buf classes.

> **NOTE —** `mem_ref_ptr_table` is the only 2-level container in the tree. Its `mem_ref_list` base vector (`+72`) holds the table's row index list; the derived class adds a *second* `uint32` vector (`+96`) for the per-row pointer list. The deleting/complete dtor at `0x4c4090` frees the `+96` vector, then chains through the `mem_ref_list` base vector and the `mem_ref` base — the vtable progression `0xbf8da0 → 0xbf8d78 → 0xbf8c60` (reloc-walked) is the runtime witness of the SI chain.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `mem_ref::mem_ref(uint, kbin_mr_type, string, vector<u64>)` | `0x4c58f0` | base ctor; writes `var_id/type/dtype/shape` | HIGH |
| `mem_ref_io::mem_ref_io(uint, ulong, uint, kbin_mr_type, …)` | `0x4c6e60` | I/O ctor; adds `size/alignment` | HIGH |
| `mem_ref_list::mem_ref_list(uint, vector<u32>)` | `0x4c59e0` | list ctor; hardcodes `type = MR_LIST(9)` | HIGH |
| `mem_ref::dump(kbin_mem_ref&)` | `0x4c3700` | base serializer (the slot-0 virtual) | HIGH |
| `mem_ref_io::dump` | `0x4c4800` | adds `size/alignment` to record | HIGH |
| `mem_ref_list::dump` | `0x4c4f80` | serializes the uint32 list; **shared by `ptr_table`** | HIGH |
| `mem_ref_info::check_var_exists(string, uint)` | `0x4c5b40` | dedup query before insert | HIGH |
| `mem_ref_info::add_var_to_lookup_maps(string, uint, mem_ref*)` | `0x4c5ce0` | insert into all 3 maps; takes ownership | HIGH |

---

## 3. The Discriminator and the Bridge Record

### `kbin_mr_type_t` (12 values)

The universal type tag, present in the C++ base (`+12`), the on-disk record (`+0`), and the runtime POD (`+8`). Verbatim from `enums.json`:

| Value | Name | Class / placement |
|---|---|---|
| 0 | `MR_INVALID` | — (sentinel) |
| 1 | `MR_SB` | `mem_ref_sp` → SB AXI offset (`aws_hal_stpb_get_axi_offset`, size `0x2000000`) |
| 2 | `MR_BUFFER_STAGED` | staged DRAM buffer (alloc + copyin) |
| 3 | `MR_BUFFER` | `mem_ref_buffer` → DRAM buffer (alloc, optional copyin) |
| 4 | `MR_TMP_BUF` | `mem_ref_tmp_buf` → DRAM scratch (alloc only) |
| 5 | `MR_INPUT` | `mem_ref_io` → DRAM (host-fed at exec) |
| 6 | `MR_OUTPUT` | `mem_ref_io` → DRAM (host-drained at exec) |
| 7 | `MR_PTR` | `mem_ref_pointer` → pointer to another var; `offset==0` enforced at resolve |
| 8 | `MR_VIRTUAL_TMP_BUF` | `mem_ref_virtual_tmp_buf` → scratchpad page at planned offset |
| 9 | `MR_LIST` | `mem_ref_list` → uint32 index table |
| 10 | `MR_PTR_TABLE` | `mem_ref_ptr_table` → 2-level pointer/row table |
| 11 | `MR_REMOTE` | `mem_ref_remote_variable` → cross-subgraph collective var |

> **GOTCHA —** do **not** confuse `kbin_mr_type_t` with `dma_mem_usage_type_t` (`WEIGHT=6`, `SCRATCHPAD=11`, `SCRATCHPAD_NOT_SHARED=16`, …). Both are ~12-value enums and both appear in the staging path, but they index different axes: `kbin_mr_type_t` discriminates the `mem_ref` *kind*, while `dma_mem_usage_type_t` tags the *DRAM region* a staged buffer is allocated from. The staging code (`mem_ref_copy_and_stage_mr`) reads `mr_type` to choose a strategy and passes a `dma_mem_usage_type` to `dmem_alloc_aligned`.

`kbin_backing_buf_t` (`enums.json`) selects which scratchpad region backs a `MR_VIRTUAL_TMP_BUF`: `KBIN_BACKING_BUF_LOCAL=0`, `_SHARED=1`, `_SHARDED=2`, `_MAIN=3`.

### `kbin_mem_ref` — the 152-byte on-disk record

The serialization bridge (ord 6351 / 17632). Offsets verbatim from `structures.json`; the array stride in the stage loop is `152`.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `mr_type` | `+0` | `kbin_mr_type_t` | discriminator (4 B + 4 pad) |
| `name` | `+8` | `char*` | variable name blob |
| `size` | `+16` | `size_t` | byte size |
| `alignment` | `+24` | `size_t` | required alignment |
| `var_id` | `+32` | `uint32` | model-unique id |
| `dtype` | `+36` | `char[16]` | fixed dtype string |
| `shape` | `+56` | `uint64[8]` | fixed shape (≤8 dims) |
| `dtensor_md` | `+120` | `kbin_debug_tensor_md_t*` | device-print metadata (or NULL) |
| `buffer` | `+128` | `uint8*` | weight payload to copyin (or NULL) |
| *(union)* | `+136` | 16 B | per-type "extra" (below) |

The `+136` anonymous union has four variants (`structures.json` ords 17633/17634/17635/17636), selected by `mr_type`:

| Variant | `mr_type` | Fields |
|---|---|---|
| `$1B6FD2CB` | `MR_PTR` | `+0 uint32 var_id` (referenced); `+4 bool io_ref` |
| `$2D778864` | `MR_VIRTUAL_TMP_BUF` | `+0 uint64 offset`; `+8 kbin_backing_buf_t backing_buf` |
| `$F4BEDE1A` | `MR_LIST` / `MR_PTR_TABLE` | `+0 uint32 length`; `+8 uint32* list` |
| `$E7600784` | `MR_REMOTE` | `+0 uint32 remote_vtpb_rel_sg_id`; `+4 uint32 remote_var_id` |

`kbin_debug_tensor_md_t` (ord 6570, 516 B) = `+0 char prefix_str[512]` + `+512 uint32 pipe_id` (the fixed-size on-disk form of the C++ `debug_tensor_md`).

### `kbin_mem_ref_set_t` — the plan header (24 B + flexible array)

This 24-byte header **is** the static memory plan: the two arena sizes the compiler computed, plus the count and the array of records. Hung off `kbin_t+296` (`kbin_t.mem_ref_set`).

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `local_scratchpad_size` | `+0` | `uint64` | planned per-TPB local arena bytes |
| `shared_scratchpad_size` | `+8` | `uint64` | planned shared arena bytes |
| `nmem` | `+16` | `uint32` | variable count |
| `mem` | `+24` | `kbin_mem_ref_t[]` | the plan — `nmem` records, stride 152 |

---

## 4. The Runtime `mem_ref_t` (Load/Resolve)

### Purpose

`mem_ref_t` (ord 8614, 208 B) is the model-resident, placement-resolved form. `mem_ref_get_new_mr` `calloc`s it (`functions.json` constant `208`) and copies the type/size/var_id/dtype/shape out of a `kbin_mem_ref`, initializing `physical_address` to `-1` (constant `0xFFFFFFFFFFFFFFFF`) to mean "not yet staged". The staging pass then writes the real `physical_address`; the executor reads it.

### Layout (208 B)

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `name` | `+0` | `const char*` | `strdup` of the kbin name |
| `type` | `+8` | `kbin_mr_type_t` | discriminator |
| `size` | `+16` | `size_t` | byte size (bounds-checked at resolve) |
| `var_id` | `+24` | `uint32` | hashtable key |
| `remote` | `+28` | `bool` | set by `mem_ref_create_remote_mrs` |
| `shared_virtual_scratchpad` | `+29` | `bool` | shared-scratchpad flag |
| *(placement union)* | `+32` | 40 B | resolved state (below) |
| `dtype` | `+72` | `char[16]` | fixed dtype |
| `shape` | `+88` | `uint64[8]` | fixed shape |
| `debug_tensor_md` | `+152` | `kbin_debug_tensor_md_t*` | device-print metadata |
| `ht_node` | `+160` | `ht_node_t` (48 B) | embedded hashtable node, keyed by `var_id` |

The `+32` placement union (40 B) has variants selected by `mr_type` (`structures.json` ords 17675–17678):

| Variant | Used by | Fields |
|---|---|---|
| `$7D4EE824` (32 B) | staged tensors | `+32 uint64 physical_address`; `+40 dmem_t* mem`; `+48 uint64 mem_offset`; `+56 void* va` |
| `$1C96A3ED` (24 B) | `MR_PTR` | `+32 uint64 ptr_mem_addr`; `+40 uint64 mr_ptr_id`; `+48 bool is_io` |
| `$F4BEDE1A` (16 B) | `MR_LIST` | `+32 uint32 length`; `+40 uint32* list` |
| `$B7FF42DE` (40 B) | `MR_PTR_TABLE` | `+32 uint32 length`; `+40 uint32* var_ids`; `+48 bool* var_id_needs_translation`; `+56 uint64* addrs`; `+64 uint64 pointers_to_tables_index` |

> **NOTE —** the `$B7FF42DE` runtime variant closes the question the `dump` side left open (§2 QUIRK): the **second** pointer list of a `MR_PTR_TABLE` is *not* serialized — it is **reconstructed at load**. The runtime carries `var_ids` (the serialized index list), a parallel `var_id_needs_translation` mask, a resolved `addrs` array, and a `pointers_to_tables_index`. The loader fills `addrs` by resolving each `var_id` through the same `mem_ref_to_addr` machinery; the serialized record stores only the indices.

> **NOTE —** the embedded `ht_node_t` at `+160` is why `lookup_memref_by_idx` (`@0x22da30`) can return the `mem_ref_t*` base by pointer arithmetic from the node: `base = &ht_node − 160`. `ht_node_t` (ord 8620, 48 B) = `+0 key(var_id)`, `+8 value`, `+16 buf_size`, `+24 bucket`, `+32 key_type`, `+40 next`.

`dmem_t` (192 B) is the device-DRAM allocation handle a staged `mem_ref_t` points to (`mem` @+40): `+0 pcore`, `+8 mem_handle`, `+16 size`, `+24 _pa` (physical), `+32 _va`, `+40 align_offset`, `+48 allocated_size`, `+56 mem_type`, `+60 mem_usage_type`, `+64 tdram_channel`, … *(the exact `dmem_t` field that feeds `physical_address` — `_pa@+24` vs the `mem+offset` read in stage — is MED confidence; the `dmem` allocator is owned by the TDRV-core dmem cell.)*

### `mem_ref_info` — the build-side symbol table (144 B)

While lowering, the parser owns the C++ objects in three `std::map`s (`structures.json` ord 6245):

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `name_to_var_id` | `+0` | `std::map<string, uint32>` (48 B) | name → id |
| `var_id_to_name` | `+48` | `std::map<uint32, string>` (48 B) | id → name |
| `var_id_to_mem_refs` | `+96` | `std::map<uint32, mem_ref*>` (48 B) | id → object (**owns** the `mem_ref*`) |

---

## 5. Build → Plan → Resolve

### Entry Point

```text
BUILD (KELF JSON → C++ tree → on-disk plan)
kelf_load_from_neff (0x4c0870)
  └─ parse_one_variable (0x4b36b0)                  ── per "var" in def.json
       ├─ new mem_ref / mem_ref_io / mem_ref_list   ── 0x4c58f0 / 0x4c6e60 / 0x4c59e0
       ├─ mem_ref_info::check_var_exists (0x4c5b40)  ── dedup
       └─ mem_ref_info::add_var_to_lookup_maps (0x4c5ce0)
  └─ (lowering) mem_ref::dump (0x4c3700, virtual)   ── per object → kbin_mem_ref record

PLAN (load → calloc → place once)
dlr_kelf_stage (0xe0970)
  └─ vtpb_info_shared_init_vtpb (0x3148e0)          ── per vnc / subgraph
       ├─ dmem_allocator_create (0x2284c0)           ── MODEL allocator
       ├─ ht_init (0x267d80)                          ── mem_ref_set, free-cb = mem_ref_free_one
       ├─ tdrv_scratchpad_model_init (0x302b70)      ── size arenas from the plan header
       ├─ mem_ref_copy_and_stage_mr (0x2fb780)       ── per kbin_mem_ref: assign physical_address
       │    ├─ mem_ref_get_new_mr (0x2fb640)          ── calloc 0xD0, physical_address = -1
       │    ├─ aws_hal_stpb_get_axi_offset (0x458e00) ── MR_SB
       │    ├─ tdrv_scratchpad_get_mem (0x3026d0)     ── MR_VIRTUAL_TMP_BUF
       │    ├─ dmem_alloc_aligned (0x228f20)          ── buffer / IO / tmp
       │    ├─ dmem_buf_copyin (0x229820)             ── weight payload
       │    └─ ht_insert (0x267e70)                   ── key = var_id
       └─ mem_ref_create_remote_mrs (0x2fc050)       ── 2nd pass: MR_REMOTE

RESOLVE (exec; zero-alloc)
instr translation (9 sites — §6)
  ├─ lookup_memref_by_idx (0x22da30)                ── ht_find by var_idx
  └─ mem_ref_to_addr (0x22da60)                     ── addr = offset + physical_address
```

### Algorithm — BUILD

```c
// parse_one_variable @0x4b36b0 — one NEFF def.json "var" entry per call.
// Caller kelf_load_from_neff @0x4c0870; ctors confirmed via callgraph out-edges.
function parse_one_variable(neff, mla_resources, name, key, json_elem):
    read var_id, alignment, size, dtype, shape from json_elem      // simdjson dom
    if alignment not power-of-2: FATAL "alignment provided not power of 2…"

    switch (kbin_mr_type from json):                                // §3 discriminator
        case MR_BUFFER, MR_TMP_BUF:  mr = new mem_ref(var_id, type, dtype, shape)   // 0x4c58f0
        case MR_INPUT, MR_OUTPUT:    mr = new mem_ref_io(var_id, size, align, type, // 0x4c6e60
                                                          dtype, shape)
        case MR_LIST:                mr = new mem_ref_list(var_id, u32_vector)       // 0x4c59e0
        // (other kinds construct the matching subclass; ctor sets vptr = _ZTV<T>+0x10)
        ...

    if mem_ref_info.check_var_exists(name, var_id): return          // 0x4c5b40 — dedup guard
    mem_ref_info.add_var_to_lookup_maps(name, var_id, mr)           // 0x4c5ce0 — takes ownership

// Lowering then calls the slot-0 virtual on every object.
// mem_ref::dump @0x4c3700 (the base serializer):
function mem_ref_dump(this, out /* kbin_mem_ref& */):
    out.var_id = this.var_id                                        // +8 -> kbin+32
    out.mr_type = this.type
    if strlen(this.dtype) > 15: FATAL "Device print dtype %s has %lu chars. Only %u…"
    snprintf(out.dtype, 16, "%s", this.dtype)                       // char[16] @kbin+36
    if this.shape.size() > 8:   FATAL "Device print only supports shape dimentions up to %u…"
    memcpy(out.shape, this.shape.data(), 8 * sizeof(uint64))        // <=64 B @kbin+56

// mem_ref_io::dump @0x4c4800 — additionally writes size@kbin+16, alignment@kbin+24,
//   zeroes the kbin union.
// mem_ref_list::dump @0x4c4f80 (also the vtable slot-0 of mem_ref_ptr_table):
//   count = (end-begin)/4; out.union.length = count; out.union.list = calloc(count,4);
//   copy each uint32 element.  The ptr_table's SECOND list (obj+96) is NOT serialized.
```

### Algorithm — PLAN (place once)

```c
// mem_ref_copy_and_stage_mr @0x2fb780 — the staging loop; callees confirmed in callgraph.
// Walks kbin->mem_ref_set->mem[i], stride 152; assigns each var a fixed physical_address.
function copy_and_stage_one(kbin_mr /* kbin_mem_ref */, model_mr_set, allocator, scratchpad,
                            hbm_idx):
    mr = mem_ref_get_new_mr(kbin_mr)                  // 0x2fb640: calloc 0xD0; phys=-1 (0xFF..FF)

    switch (kbin_mr.mr_type):                          // §3 discriminator
        case MR_SB:                                    // state buffer — fixed AXI offset
            mr.physical_address = aws_hal_stpb_get_axi_offset(...)   // 0x458e00; size = 0x2000000
        case MR_VIRTUAL_TMP_BUF:                        // scratchpad page at planned offset
            mr.physical_address = tdrv_scratchpad_get_mem(scratchpad, kbin_mr.union.offset)  // 0x3026d0
        case MR_BUFFER, MR_BUFFER_STAGED, MR_TMP_BUF, MR_INPUT, MR_OUTPUT:
            mr.mem = dmem_alloc_aligned(allocator, kbin_mr.size, kbin_mr.alignment, hbm_idx)  // 0x228f20
            mr.physical_address = pa_of(mr.mem)         // from the dmem handle (MED: _pa vs mem+off)
            mr.mem_offset = 0
            if kbin_mr.buffer != NULL:
                dmem_buf_copyin(mr.mem, kbin_mr.buffer, kbin_mr.size)   // 0x229820 — weight upload
        case MR_PTR, MR_LIST, MR_PTR_TABLE:             // pointer / index tables (calloc'd)
            build_index_table(mr, kbin_mr)
        case MR_REMOTE:
            ;                                           // deferred to 2nd pass (below)

    ht_insert(model_mr_set, mr.var_id, &mr.ht_node)     // 0x267e70 — key = var_id
    log "Staged memref {%s} id {%d} to {0x%lx}. mem.addr {0x%lx}, HBM: %u (%s)"
        with mode "alloc+copy" or "alloc only"

// mem_ref_create_remote_mrs @0x2fc050 — 2nd pass for MR_REMOTE (collectives).
function create_remote_mrs(local_set, remote_sets, num_kbins):
    for each MR_REMOTE kbin_mr:
        assert kbin_mr.union.remote_vtpb_rel_sg_id < num_kbins      // mem_ref.c:0x12A
        src = ht_find(remote_sets[remote_vtpb_rel_sg_id], remote_var_id)
        if !src: FATAL "Failed to find remote variable %u in relative sg %u…"
        mr = mem_ref_get_new_mr(...); mr.remote = true
        mr.physical_address = src.physical_address; mr.mem = src.mem; mr.va = src.va
        ht_insert(local_set, mr.var_id, &mr.ht_node)
```

### Algorithm — RESOLVE (zero-alloc)

The resolve path is read byte-exactly from the `mem_ref_to_addr` disassembly (`0x22da60`–`0x22db32`):

```c
// mem_ref_to_addr @0x22da60 (tdrv/dma_ring.c). The plan consumer; NO allocation.
// Returns NRT_STATUS; out *addr.  Verbatim register reads in comments.
function mem_ref_to_addr(mr, offset, size, out addr):
    if mr->size < offset + size:                       // mov 0x10(%rdi); lea (%rsi,%rdx); cmp; jb
        nlog "invalid offset in %s, %lu < (%lu + %lu)"  // -> return 2 (INVALID)
        return NRT_INVALID
    if mr->type == MR_PTR:                              // cmpl $0x7, 0x8(%rdi); je
        assert offset == 0                              // test %rsi; jne -> __assert_fail dma_ring.c:0x4B4
        *addr = mr->physical_address                    // mov 0x20(%rdi)  (offset is 0 here)
        return NRT_SUCCESS
    pa = mr->physical_address                           // mov 0x20(%rdi)  (the +32 field)
    if pa == 0xFFFFFFFFFFFFFFFF:                         // cmp $-1; je
        nlog "Variable %s is not staged in HBM"
        return NRT_INVALID
    *addr = offset + pa                                 // add %rsi,%rax ; mov %rax,(%rcx)
    return NRT_SUCCESS
```

> **QUIRK — zero-alloc resolve.** The entire execute-time address translation is a bounds-check, a discriminator compare, an unstaged-sentinel compare, and a single `add`. No allocation, no virtual dispatch, no hashtable touch (the lookup happens once in `lookup_memref_by_idx` upstream). `addr = offset + physical_address` is the whole of static memory planning's payoff: because the compiler decided placement and the loader committed it once, every per-op address is an add. The `MR_PTR(7)` special-case (requiring `offset == 0`) is the one branch — a pointer mem_ref *is* its target's base, so a non-zero offset into it is a build error, hard-asserted at `dma_ring.c:0x4B4`.

> **GOTCHA —** the unstaged sentinel is `physical_address == -1` (`0xFFFFFFFFFFFFFFFF`), set by `mem_ref_get_new_mr` (constant `0xFFFFFFFFFFFFFFFF`) and only overwritten when staging succeeds. A reimplementer that initializes `physical_address` to `0` will let an unstaged variable resolve to device address `0 + offset` silently — exactly the corruption the `"Variable %s is not staged in HBM"` guard exists to prevent. Initialize to all-ones, not zero.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `kelf_load_from_neff` | `0x4c0870` | NEFF parse entry → `parse_one_variable` | HIGH |
| `parse_one_variable` | `0x4b36b0` | per-var: build C++ object, dedup, index | HIGH |
| `dlr_kelf_stage` | `0xe0970` | model-load staging entry | HIGH |
| `vtpb_info_shared_init_vtpb` | `0x3148e0` | per-vnc planner driver | HIGH |
| `tdrv_scratchpad_model_init` | `0x302b70` | sizes arenas from the plan header | HIGH (flow MED) |
| `mem_ref_get_new_mr` | `0x2fb640` | `calloc(0xD0)`; `physical_address = -1` | HIGH |
| `mem_ref_copy_and_stage_mr` | `0x2fb780` | per-record placement + copyin + insert | HIGH |
| `mem_ref_create_remote_mrs` | `0x2fc050` | 2nd pass, `MR_REMOTE` collective vars | HIGH |
| `mem_ref_free_one` | `0x2fb730` | ht free-cb: name, list ptr, dtensor_md, node | HIGH |
| `lookup_memref_by_idx` | `0x22da30` | `ht_find` → `mem_ref_t*` (base = node − 160) | HIGH |
| `mem_ref_to_addr` | `0x22da60` | resolve `addr = offset + physical_address` | HIGH |
| `mem_ref_get_va` | `0x2fc370` | host VA; `MR_VIRTUAL_TMP_BUF` spill → `mr->va` | HIGH |

---

## 6. The Resolve Consumers

### Purpose

`mem_ref_to_addr` is the single funnel through which instruction translation turns a model variable into a device address. Nine call sites (callgraph in-edges to `0x22da60`) consume the plan; all are in the per-instruction translation hot path, none allocates.

| Caller | Address | What it translates |
|---|---|---|
| `instr_col_setup_psr` | `0x26df50` | PSR (pseudo-sync-register) operand addresses |
| `build_enc_op_list_args_with_tensor_id` | `0x26e330` | collective-encoder op-list tensor args |
| `instr_col_translate_pseudo_gid_load` | `0x271430` | group-id load pseudo-instruction |
| `translate_one_pseudo_instr_v2` | `0x273ff0` | v2 pseudo-instruction operands |
| `translate_one_pseudo_dma_memcpy_instr_v2` | `0x31abd0` | v2 DMA-memcpy src/dst |
| `drs_expand_data_desc_model` | `0x31bf80` | data-descriptor model expansion |
| `translate_one_pseudo_instr_v3` | `0x3221a0` | v3 pseudo-instruction operands |
| `translate_pseudo_tpb_addr8` | `0x322710` | TPB 8-byte address pseudo |
| `expand_io_descs` | `0x4437e0` | I/O descriptor expansion |

> **NOTE —** `mem_ref::dump` (the build-side virtual) has **no** static callgraph in-edge, because it is reached only through the vtable (an indirect `call *0x0(%rax)` after loading the vptr). The lowering driver iterates the `mem_ref_info.var_id_to_mem_refs` map and dispatches `mr->dump(out)` virtually. A reimplementer reading a static call graph will not see the edge; the dispatch is real and per-object.

---

## Related Components

| Name | Relationship |
|---|---|
| `kbin_mem_ref` / `mem_ref::dump` | the 152-byte bridge record and its serializer — written by representation A, read into B |
| `vtpb_info_shared_init_vtpb` (`0x3148e0`) | the per-vnc planner driver that runs the whole PLAN phase once at load |
| `tdrv_scratchpad_model_init` (`0x302b70`) | sizes the local/shared scratchpad arenas from the plan header before staging |
| `dmem_t` / `dmem_alloc_aligned` | the device-DRAM allocator a staged `mem_ref_t` points to |
| `ht_node_t` / `ht_insert` / `ht_find` | the generic hashtable that indexes the model's `mem_ref_set` by `var_id` |

## Cross-References

- [kelf2kbin: JSON → KBIN Lowering](kelf2kbin.md) — `parse_one_variable` and the build-side C++ object construction that precedes `dump`
- [KBIN Structures](kbin-structs.md) — `kbin_t`, `kbin_mem_ref_set_t`, and the wider on-disk record schema
- [The Load Pipeline (Parse → Build → Stage → Relocate)](load-pipeline.md) — where `dlr_kelf_stage` and `vtpb_info_shared_init_vtpb` sit in the model-load sequence
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the DMA descriptors that carry the `physical_address` values this plan resolves; `buf_ptr` is a resolved `mem_ref` address
- [C++ Class Hierarchy and RTTI](../forensics/rtti-class-hierarchy.md) — the binary-wide RTTI/vtable census this 10-class tree belongs to
- [back to index](../index.md)
