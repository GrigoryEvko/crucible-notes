# Serializer Families (per-arch CC-context dumpers)

> *All addresses on this page apply to `libncfw.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (ELF64 x86-64 DYN, 615,640 B, md5 `e01ea384a76e59d511b4f005b7db98ac`, SONAME `libncfw.so.2.31.1.0.cf13a49f`, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`; **not** stripped — full symtab, **no DWARF**; PIE base 0, `.text` @ `0x10c0` and `.rodata` @ `0x65000` both VMA == file offset per `readelf -S`, so every `0x1xxx`/`0x65xxx` is both an analysis VMA and a host file offset). `NEEDED` is `libc.so.6` only. Other versions will differ.*
>
> *Evidence grade: **Confirmed (symbol- and string-anchored)** — the 38×4 family inventory, the four `.rodata` key bands, the `(out, indent, key, struct_ptr)` convention, the `ncfw_log_addr` trail-comma arg, and the per-arch context offsets are read straight from `nm`/`objdump`/`strings`/`readelf` on the binary. The recovered CC-context struct offsets are **HIGH** where the dereference is literal in the sunda copy, **MEDIUM** where assumed identical across the three clones or not byte-diffed. · Part X — Collectives Firmware (libncfw) · [back to index](../index.md)*

## Abstract

`libncfw.so`'s second job — after the firmware-blob provider — is a **structural reflection of the on-device collective-communication (CC) context**, emitted as JSON. Given a parsed in-memory CC-context struct (built by libnrt's collectives layer, uploaded to each NeuronCore's Xtensa sync core, and consumed there from DRAM), the serializer tree pretty-prints the entire structure into a **1 MB caller buffer**. It is a debug/telemetry dumper, never on the hot path; libnrt caches its function pointer (`libncfw_ctx_log_func` @`libnrt 0xc96d08`) and calls it for diagnostic context dumps. The serializer is the only non-trivial control flow in the library, and because it is the *mirror image* of the struct the sequencer reads, recovering every `snprintf` key and struct dereference reconstructs the on-device CC-context schema — rings, meshes, hierarchical algos, kangaring peer rings, APB-broadcast DMA, NEFF host/device barriers, tsync, basic-block tables, TPB completion — **without the device**.

The tree is a fixed recursive descent of **38 distinct serializer families**, each cloned byte-for-byte into four arch copies (sunda / cayman / mariana / mariana_plus), for **152 helper functions**. Every node shares one calling convention — `int ncfw_log_<family>(out, indent, key, struct_ptr)` — and one safety idiom: each `snprintf` is bounds-checked against `0x100000 − strlen(out)`, and a per-arch global latch `ncfw_log_buffer_full` is set on any truncation, so the top function returns `28`/ENOSPC if the 1 MB buffer overflowed and `0` otherwise. The algorithm subtree appears **twice** — once as live runtime state (16-byte channel rows, under `algo`) and once as the full static descriptor (148-byte channel rows, under `configs`) — because the sequencer keeps mutable counters and immutable topology separately. The four arch dispatchers select which context layout to walk; the leaf bodies are identical, differing only in the two top-level context offsets.

This page owns the **byte-level family tree and the recovered CC-context schema**. It does not re-derive the host object's ELF identity or the provider ABI ([The Carrier Library](carrier-library.md)), the embedded Xtensa blobs ([Embedded Payloads](embedded-payloads.md)), or the on-device executor's meaning ([The NCFW Sequencer](ncfw-sequencer.md)). The two richest leaf records it dumps — the 148-byte ring channel descriptor and the packed `cc_op_entry` scheduler descriptor — are owned field-for-field by their own Part IX pages ([The 148-Byte Ring Channel Descriptor](../collectives/channel-descriptor.md), [The cc_op_entry On-Device Collective ISA](../collectives/cc-op-isa.md)); here they appear as serializer nodes corroborating those layouts.

For reimplementation, the contract is:

- **The family inventory** — 38 distinct serializer families × 4 arch clones = 152 functions, an `nm`-grounded census (§1); the recursive-descent topology that wires them (§3).
- **The uniform calling convention** — every node is `int ncfw_log_<x>(out, indent, key, struct_ptr)`, opening an object/array, emitting children, closing with `},`/`}`; the bounds-check + `ncfw_log_buffer_full` latch; and the one exception, `ncfw_log_addr`'s leading trail-comma byte (§2).
- **The two parallel views** — runtime `_ctx` (16-byte rows) vs static `_configs` (148-byte rows), the same algorithm dumped twice (§3).
- **The reconstructed CC-context struct map** — every offset recovered from the snprintf keys + derefs, with confidence (§4), and the per-arch top-level sizing delta (sunda `+0x3060`/`+0x30C0` vs the other three `+0x4280`/`+0x42E0`).

| | |
|---|---|
| **Entry** | `libncfw_ctx_log` @`0x1309` → `{sunda,cayman,mariana,mariana_plus}_ncfw_ctx_log` @`0x1a12b`/`0x32ed2`/`0x4bc79`/`0x64a20` |
| **Family count** | **38 distinct families × 4 arch clones = 152 functions** (`nm`-verified, §1) |
| **Census** | 152 serializer helpers + 4 arch dispatchers + 3 public exports + CRT/PLT + 4 `.bss` `ncfw_log_buffer_full` data syms |
| **Convention** | `int ncfw_log_<x>(char *out, int indent, const char *key, void *struct_ptr)` — uniform; `ncfw_log_addr` adds a leading `char` |
| **Buffer** | 1 MB (`0x100000`) caller buffer; truncation latch `ncfw_log_buffer_full` `.bss` @`0x95029`..`0x9502c` (×4) |
| **Returns** | `28`/ENOSPC if any latch set; `0` otherwise; `22`/EINVAL from the dispatcher on unknown arch |
| **Key bands** | JSON-key block replicated ×4 in `.rodata` @`0x65000` / `0x656a6` / `0x65d2f` (≈`0x65d1a`) / `0x663c4` (≈`0x663af`) |
| **Root node** | `ncfw_log_ctx` @`0x19c0e` → `"ncfw_ctx_top_sp"` → `configs` / `neff` / `algo` |
| **Ctx offsets** | sunda `neff=ctx+0x3060`, `algo=ctx+0x30C0`; cayman/mariana/mariana_plus `+0x4280`/`+0x42E0` (`objdump`-confirmed) |

> **CORRECTION — the libncfw serializer-family "38" is NOT the ntff-protobuf "38".** Two unrelated `38`s appear across this wiki and must never be conflated. **This page's 38** is the count of distinct `ncfw_log_*`-style serializer *families* in `libncfw.so`, each cloned ×4 (38 × 4 = 152 functions) — an `nm`-grounded fact about the CC-context JSON dumper (proof in §1). The **other 38** is the number of `ntff` (Neuron Trace File Format) protobuf parse tables in `libnrt.so`'s Kaena profiler path — a completely different binary, a different subsystem, and a coincidence of value. The two share nothing: not a binary, not a struct, not a code path. A reader who sees "38" near either should check whether the context is *libncfw CC-context serializers* (this page) or *libnrt ntff protobuf tables* (the profiler pages); they are never the same 38.

---

## 1. The Family Census — counting from `nm`, not from memory

### Purpose

The single number a reimplementer must trust before building anything is *how many serializer families there are*, because the whole tree is a fixed set of them and getting the count wrong means missing a node or inventing one. The count is **38 distinct families**, and it must be derived empirically — a naïve symbol grep over-counts, because one `ncfw_log_*` name is a `.bss` data object, not a function, and the top-level `ncfw_ctx_log` family does *not* carry the `ncfw_log_` prefix.

### The derivation

```text
nm libncfw.so | rg ' ncfw_log_'                          → 152 symbols   (NAÏVE — wrong unit)
  of which ' b ncfw_log_buffer_full'                     →   4 are .bss DATA (the truncation latch)
  of which ' [tT] ncfw_log_'                             → 148 are FUNCTIONS

148 function symbols / 4 arch copies = 37 distinct ncfw_log_* family bases
  (each base appears exactly 4×: sunda + cayman/mariana/mariana_plus, verified by uniq -c)

+ the top family ncfw_ctx_log (NO ncfw_log_ prefix) × 4 copies = 1 more distinct family

⇒ 37 + 1 = 38 distinct serializer families × 4 arch = 152 serializer functions.
```

> **GOTCHA — the naïve count of 152 *symbols* is right by accident, for the wrong reason.** `nm | rg ' ncfw_log_'` returns 152, which equals the function total — but only because the 4 `ncfw_log_buffer_full` *data* symbols happen to offset the 4 `ncfw_ctx_log` *function* symbols that the `ncfw_log_` filter misses (the top family has no `ncfw_log_` prefix). The two errors cancel. A reimplementer who trusts the grep gets the right total and the *wrong inventory* — they would list `ncfw_log_buffer_full` as a serializer and omit `ncfw_ctx_log`. Count functions with `nm | rg ' [tT] ncfw_log_'` (= 148), divide by 4 (= 37 bases), and add the prefix-less `ncfw_ctx_log` family (= 38).

This **confirms** the firmware-overview census (`38 families × 4 = 152`, metadata `total_functions = 181`). The `181` total is the full symtab object count — 152 serializer helpers + 4 arch dispatchers + 3 public exports + ~12 CRT/PLT thunks + the per-arch data symbols — not 181 serializer functions; the serializer-function figure is exactly 152. (No correction to the overview is needed; it states `38 families × 4 arch clones = 152 helpers` correctly, including the 4 `ncfw_ctx_log` tops in the 38.)

### The 38 families by structural group

The tree groups into seven structural regions. Rather than print all 152 cloned helpers, the table gives the *shape* — the group, how many distinct families it holds, and the sunda-base entry point of a representative. Each family has exactly four arch copies, so the family-count column totals `3 + 7 + 4 + 3 + 3 + 9 + 9 = 38`, and `38 × 4 = 152`.

| Group | Families | Representative (sunda base) | Reflects | Confidence |
|---|---|---|---|---|
| Top / ctx | 3 | `ncfw_ctx_log` @`0x19f01`, `ncfw_log_ctx` @`0x19c0e` | 1 MB `memset` + `model_name` + root `ncfw_ctx_top_sp` → configs/neff/algo | HIGH |
| Algo — config view | 7 | `ncfw_log_algo_ring_channels_configs` @`0x6020` | static 148 B ring rows, neighbors, kring, mesh events; `hierarchical_configs` = `__stub` | HIGH |
| Algo — runtime view | 4 | `ncfw_log_algo_ring_ctx` @`0x16a00` | live 16 B ring rows, mesh `event_index`, hierarchical `run_state` | HIGH |
| Spad-ctrl / cc-op | 3 | `ncfw_log_spad_ctrl_cc_op_entry` @`0x1840` | the packed per-op scheduler descriptor (ring/mesh union) | HIGH |
| Addr helpers | 3 | `ncfw_log_addr` @`0x41c3`, `ncfw_log_soc_addr` @`0x8a1b` | `soc_addr` u64 formatting (single + array; trail-comma variant) | HIGH |
| NEFF barrier subtree | 9 | `ncfw_log_neff_device_barrier_config` @`0xfef5` | host/device barriers, step `m2s/s2m`, `tdrbp`, dma-sync-sema | HIGH |
| Config / dev / basic-block / tsync | 9 | `ncfw_log_dev_configs` @`0xc371` | `tpb_id`/`seng_id`/`dev_id`, tsync, 4 B basic-block table | HIGH |

### Per-family address map (sunda base + 3 clones)

The complete family list, sunda-base address, arch-clone band, and what each dumps. Each name appears exactly 4× (`nm` `uniq -c` = 4 for every base); the clone addresses live in disjoint bands — cayman ≈ `0x1c000`..`0x33000`, mariana ≈ `0x35000`..`0x53000`, mariana_plus ≈ `0x3a000`..`0x65000` (IDA disambiguates with `_0`/`_1`/`_2`). Confidence **HIGH** (the four-copy multiplicity and the sunda addresses are `nm`-verified) unless noted.

| Family (base name) | sunda @ | what it dumps |
|---|---|---|
| `ncfw_ctx_log` | `0x19f01` | top: `memset` 1 MB, `"{"`, `model_name`, calls root |
| `ncfw_log_ctx` | `0x19c0e` | root `ncfw_ctx_top_sp` → configs / neff / algo |
| `ncfw_log_configs` | `0x19915` | `configs` → algo_configs / neff_configs / dev_configs |
| `ncfw_log_algo_configs` | `0x1961c` | static algo container → ring / mesh / hierarchical configs |
| `ncfw_log_algo_ring_configs` | `0x8544` | 32× channel @148 B → `channels` |
| `ncfw_log_algo_ring_channels_configs` | `0x6020` | the 148-byte static channel descriptor (the core row) |
| `ncfw_log_algorithm_ring_neighbor` | `0x37f1` | next/prev neighbor, 28 B (`net_idx_addrs`, `fold_n`, `type`) |
| `ncfw_log_algo_ring_kring_peer_semas` | `0x4d64` | kangaring `mine` + `peers[3]`, 4× u64 |
| `ncfw_log_dma_channel_apb_bcast` | `0x4899` | m2s/s2m tail ptr + mask (20 B) |
| `ncfw_log_algo_mesh_configs` | `0x19365` | mesh container → mesh events |
| `ncfw_log_configs_algo_mesh_events` | `0x8d82` | mesh event table (80-byte rows) |
| `ncfw_log_algo_hierarchical_configs` | `0x18fcb` | **`__stub`** — emits the literal `__stub` |
| `ncfw_log_algo_ctx` | `0x18cd2` | runtime algo container → ring / mesh / hierarchical ctx |
| `ncfw_log_algo_ring_ctx` | `0x16a00` | 32× runtime channel rows @16 B |
| `ncfw_log_algo_mesh_ctx` | `0x1884c` | `event_index` u16 |
| `ncfw_log_algo_hierarchical_ctx` | `0x183c6` | `run_state` u8 |
| `ncfw_log_spad_ctrl_entry` | `0x3424` | `{header, cc_op_entry}` wrapper (`lea +1` before cc_op) |
| `ncfw_log_spad_ctrl_entry_header` | `0x13b5` | header `cc_op` enable bit (`*p & 1`) |
| `ncfw_log_spad_ctrl_cc_op_entry` | `0x1840` | the per-op scheduler descriptor (packed bitfield + union) |
| `ncfw_log_addr` | `0x41c3` | single `soc_addr` u64; **leading trail-comma arg** |
| `ncfw_log_soc_addr` | `0x8a1b` | array of u64 soc addrs (no sunda caller — §3) |
| `ncfw_log_dma_reprogram_info` | `0x4571` | m2s_low/m2s_high/s2m_low (no sunda caller; `s2m_high` key absent — §4) |
| `ncfw_log_neff_ctx` | `0x1653b` | `barrier_completed` → op_ctx, basic_block_ctx |
| `ncfw_log_neff_configs` | `0x12864` | dma_alloc_bitmap, barrier_configs, bb_configs, addr block |
| `ncfw_log_neff_barrier_config` | `0x10f8b` | `execute_device_barrier` → host **or** device barrier |
| `ncfw_log_neff_host_barrier_config` | `0xd613` | host barrier soc_addr pair |
| `ncfw_log_neff_device_barrier_config` | `0xfef5` | dma_sync_sema, apb_bcast, tdrbp, desc, queue, network_proxy |
| `ncfw_log_neff_device_barrier_step_config` | `0xed02` | step rows (stride 52): m2s_val/s2m_val → semaphores, sema_values |
| `ncfw_log_neff_device_barrier_semaphores` | `0xddbc` | soc_addr array |
| `ncfw_log_neff_device_barrier_sema_values` | `0xe6e1` | `target_sema_val` u32 array |
| `ncfw_log_device_barrier_dma_sync_sema` | `0xf5d0` | soc_addr |
| `ncfw_log_dev_configs` | `0xc371` | tpb_id, seng_id, dev_id, tpb_ctrl_ack |
| `ncfw_log_configs_dev_tsync` | `0xad33` | 3× soc_addr + `timestamp_tpb_val` |
| `ncfw_log_configs_neff_dma_alloc_bitmap` | `0xce01` | 8 entries {queue_id, dma_engines_bitmap} |
| `ncfw_log_op_ctx` | `0x150a2` | ctrl_entry, tpb_compl[2], id, reporter |
| `ncfw_log_basic_block_ctx` | `0x15ff1` | ctrl_spad_addr, curr_id → table |
| `ncfw_log_basic_block_configs` | `0x11cc7` | pring_base_addr, pring_total_desc_num |
| `ncfw_log_basic_block_table` | `0x11452` | ≤255× 4 B {desc_count, reset_section_desc_count} |

---

## 2. The Calling Convention

### Purpose

The whole tree is navigable once one signature and one safety idiom are known. Every one of the 152 helpers has the same shape, so the recursion reads uniformly; the single deviation (`ncfw_log_addr`'s leading flag byte) is the one thing a reimplementer must special-case.

### The uniform node signature

```c
// Every one of the 38 leaf/node families (sunda base + 3 arch clones each):
int ncfw_log_<family>(const char *out_buf, int indent, const char *key, void *struct_ptr);
//   emits  <indent spaces> + (key ? "\"key\": {" : "{")   for an object   (or "[" for an array)
//   walks children / emits scalar fields
//   closes with "}," / "}"            indent grows +2 per nesting level
//   every write bounds-checked vs (0x100000 - strlen(out_buf))   — the 1 MB cap
```

### The EMIT idiom and the truncation latch

The body of every node is a sequence of this idiom, once per `snprintf`. The running write cursor is implicit — it is `out + strlen(out)` — so the cursor is re-found by a `strlen` before every write (an O(n²) dumper, acceptable because it is a debug path, not the hot path):

```c
// The universal serializer-node EMIT idiom (verified @ncfw_log_addr 0x41c3, et al.):
size_t budget = 0x100000 - strlen(out);                 // 0x41f2 mov $0x100000 ; 0x41ed/0x4204 strlen ×2
int n = snprintf(out + strlen(out), budget,             // write at the implicit cursor
                 "%*s\"%s\": {", indent, "", key);       // fmt frag @0x65000/0x65001 (object-open)
if (n < 0 || (size_t)n >= budget)                        // truncated or error
    ncfw_log_buffer_full = 1;                            // .bss @0x95029 (sunda copy) — LATCH, never reset here
```

`ncfw_log_buffer_full` is a global, not a per-call flag. There are **four copies** in `.bss` (`@0x95029`/`0x9502a`/`0x9502b`/`0x9502c`, one per arch clone; `nm` shows all four `b ncfw_log_buffer_full`), and the sunda tree writes the `0x95029` copy. Because no node clears it, a single truncation anywhere in a multi-thousand-node dump turns the whole return into `28`/ENOSPC — the buffer is all-or-nothing. The top `ncfw_ctx_log[_N]` first `memset`s the 1 MB buffer to zero (so the first `strlen` reads a clean cursor of 0), emits `"{\n"` then `"model_name": "%s",\n"` (fmt @`0x6566e`), calls the root `ncfw_log_ctx(out, 2, "ncfw_ctx_top_sp", …)` (key @`0x65685`), inspects the latch, and returns `28` or `0`.

### The one exception — `ncfw_log_addr`'s leading flag byte

```c
// Models ncfw_log_addr @0x41c3 — note the EXTRA leading char arg (saved %al @-0x34, verified objdump).
int ncfw_log_addr(char trail_comma, const char *out, int indent, const char *key, uint64_t *p):
    // 0x41e3  mov %al,-0x34(%rbp)   <-- the trail-comma flag, NOT present on any other family
    EMIT("%*s\"%s\": \"0x%016lX\"%s", indent, "", key, *p, trail_comma ? ",\n" : "\n");
    //   actual constant @0x65127 is `%s: "0x%016lX"` ; the trailing `%s` is "," or "" (fmt @0x65109)
```

> **NOTE — `ncfw_log_addr` breaks the uniform 4-arg signature.** The 38 families almost all share `(out, indent, key, struct_ptr)`, but the `soc_addr` leaf `ncfw_log_addr` @`0x41c3` *prepends* a `char trail_comma` — the firmware-side "is this the last field in its object?" flag, passed in `%al` and saved at `-0x34` (confirmed `objdump`: `88 45 cc  mov %al,-0x34(%rbp)` at `0x41e3`). It exists because the last field in a JSON object must omit the trailing comma. This is the single most-called leaf in the tree (every `soc_addr` routes through it), so a reimplementer who assumes a uniform 4-arg signature for *every* node will mis-call it. The array variant `ncfw_log_soc_addr` @`0x8a1b` uses the `"0x%016lX"%s` form (fmt @`0x65109`) for comma-separated array elements.

---

## 3. The Recursive Descent

### Purpose

The 152 helpers wire into one fixed tree rooted at `ncfw_log_ctx`. The topology *is* the CC-context's nesting, so the call chain is the schema's outline; reproducing it reproduces the JSON shape exactly.

### Entry and dispatch

```text
libncfw_ctx_log(ctx, out, model_name, arch_id)  @0x1309
  └─ switch(arch_id) { 5 | 12 | 20 | 28 }           -- binary-search cmpl tree; unknown → 22/EINVAL
       └─ <arch>_ncfw_ctx_log(ctx, out, model_name) -- arch_id dropped (callee is arch-specialized)
            └─ ncfw_ctx_log[_N]  -- memset(out,0,0x100000); "{"; "model_name"; ncfw_log_ctx(root)
```

### The tree (sunda copy; identical topology in all four arches)

```text
ncfw_log_ctx @0x19c0e  ("ncfw_ctx_top_sp")
 ├─ ncfw_log_configs @0x19915  (ctx+0)                         -- STATIC config view
 │   ├─ ncfw_log_algo_configs @0x1961c
 │   │   ├─ ncfw_log_algo_ring_configs @0x8544                 -- 32× @148B
 │   │   │   └─ ncfw_log_algo_ring_channels_configs @0x6020    -- the 148-byte row
 │   │   │       ├─ ncfw_log_algorithm_ring_neighbor @0x37f1   -- next_neigh, prev_neigh (28B ea)
 │   │   │       ├─ ncfw_log_algo_ring_kring_peer_semas @0x4d64
 │   │   │       └─ ncfw_log_dma_channel_apb_bcast @0x4899 → ncfw_log_addr ×2
 │   │   ├─ ncfw_log_algo_mesh_configs @0x19365
 │   │   │   └─ ncfw_log_configs_algo_mesh_events @0x8d82 → ncfw_log_dma_channel_apb_bcast
 │   │   └─ ncfw_log_algo_hierarchical_configs @0x18fcb  ("__stub")
 │   ├─ ncfw_log_neff_configs @0x12864  (configs+8744)
 │   │   ├─ ncfw_log_addr  (ctrl_spad_base + 6-entry addr[] + 5 trailing soc_addr)
 │   │   ├─ ncfw_log_configs_neff_dma_alloc_bitmap @0xce01
 │   │   ├─ ncfw_log_neff_barrier_config @0x10f8b
 │   │   │   ├─ ncfw_log_neff_host_barrier_config @0xd613 → ncfw_log_addr        (if !execute_device_barrier)
 │   │   │   └─ ncfw_log_neff_device_barrier_config @0xfef5                       (if execute_device_barrier)
 │   │   │       ├─ ncfw_log_device_barrier_dma_sync_sema @0xf5d0
 │   │   │       ├─ ncfw_log_dma_channel_apb_bcast @0x4899
 │   │   │       └─ ncfw_log_neff_device_barrier_step_config @0xed02
 │   │   │           ├─ ncfw_log_neff_device_barrier_semaphores @0xddbc
 │   │   │           └─ ncfw_log_neff_device_barrier_sema_values @0xe6e1
 │   │   └─ ncfw_log_basic_block_configs @0x11cc7 → ncfw_log_basic_block_table @0x11452
 │   └─ ncfw_log_dev_configs @0xc371  (configs+12336)
 │       └─ ncfw_log_configs_dev_tsync @0xad33 → ncfw_log_addr ×3
 ├─ ncfw_log_neff_ctx @0x1653b  (ctx+NEFF_OFF)                 -- RUNTIME neff state
 │   ├─ ncfw_log_op_ctx @0x150a2  (neff+0)
 │   │   └─ ncfw_log_spad_ctrl_entry @0x3424
 │   │       ├─ ncfw_log_spad_ctrl_entry_header @0x13b5  (cc_op bit)
 │   │       └─ ncfw_log_spad_ctrl_cc_op_entry @0x1840   (algo_type/sub_type/trigger/reporter/union)
 │   └─ ncfw_log_basic_block_ctx @0x15ff1  (neff+32) → ncfw_log_addr, ncfw_log_basic_block_table
 └─ ncfw_log_algo_ctx @0x18cd2  (ctx+ALGO_OFF)                 -- RUNTIME algo state
     ├─ ncfw_log_algo_ring_ctx @0x16a00       (32× 16B runtime rows)
     ├─ ncfw_log_algo_hierarchical_ctx @0x183c6  (algo+512)
     └─ ncfw_log_algo_mesh_ctx @0x1884c          (algo+516)
```

### The two parallel views

The single most important structural fact is that the algorithm subtree appears **twice**, because the sequencer keeps live state and a static descriptor separately. The same ring algorithm is reflected once as a 16-byte runtime row and once as a 148-byte static descriptor:

| View | Root family | Children | What it reflects | Row size |
|---|---|---|---|---|
| **static config** | `ncfw_log_algo_configs` @`0x1961c` (under `configs`) | `ring_configs` / `mesh_configs` / `hierarchical_configs` | full descriptor: neighbors, 4 semas, kangaring, APB-bcast | 148 B / channel |
| **runtime state** | `ncfw_log_algo_ctx` @`0x18cd2` (under `algo`) | `ring_ctx` / `mesh_ctx` / `hierarchical_ctx` | live counters: recv_cnt, send_credit, run_state | 16 B / channel |

> **QUIRK — two families have no caller reachable from `ncfw_ctx_log` in the sunda trace.** `ncfw_log_soc_addr` @`0x8a1b` (the soc-addr *array* leaf) and `ncfw_log_dma_reprogram_info` @`0x4571` are present in all four arch copies but are not reached from the sunda descent above. They are either dead/legacy or reached only through a non-sunda context layout (**LOW** — the xref is unresolved). The latter also carries a recovered firmware-side gap (§4): its key run is `m2s_low` @`0x65137` / `m2s_high` @`0x6513f` / `s2m_low` @`0x65148`, with **no `s2m_high` key string anywhere** (`strings libncfw.so | rg -w s2m_high` → empty), so the serializer emits an `m2s_low`/`m2s_high`/`s2m_low` triple where a four-field layout would expect a closing `s2m_high`.

---

## 4. The Reconstructed CC-Context Struct Map

### Purpose

The serializer's struct dereferences, read together with the verbatim `.rodata` keys it pairs them with, *are* the CC-context schema. This section is the recovered layout: each field, its inferred byte offset, the JSON key it is dumped as, and a confidence tag. All offsets are from the **sunda (arch-5)** copy; the three clones dereference identical leaf offsets (HIGH for sunda; MEDIUM that v3/v4/v4+ leaf offsets match — clone bodies look identical but were not byte-diffed individually). The two top-level offsets *do* differ per arch (below).

### The root and its top-level offsets

```c
// ncfw_log_ctx(out, 2, "ncfw_ctx_top_sp", ctx+NEFF_OFF, ctx+ALGO_OFF, ctx+0)  — objdump-confirmed offsets
//   sunda  (id 5):  neff = ctx+0x3060 (12384) ; algo = ctx+0x30C0 (12480) ; configs = ctx+0
//   cayman/mariana/mariana_plus (12/20/28): neff = ctx+0x4280 (17024) ; algo = ctx+0x42E0 (17120)
```

> **QUIRK — the only per-arch difference is two top-level offsets; the leaf bodies are byte-for-byte clones.** Each of the 38 families is duplicated into four arch copies, and the cayman/mariana/mariana_plus copies are **byte-for-byte clones of SUNDA** — they dereference identical struct offsets over the identical (×4-replicated) key strings. The sole structural divergence is the top-level context sizing: sunda places `neff`/`algo` at `ctx+0x3060`/`+0x30C0` (`objdump`: `add $0x3060`/`$0x30c0` at `0x1a0cb`/`0x1a0d9`), the other three at `ctx+0x4280`/`+0x42E0` (`add $0x4280`/`$0x42e0` at `0x32e72`/`0x32e80`), so v3/v4/v4+ contexts are ≥17 KB versus sunda's ~12.4 KB before the neff/algo blocks. A reimplementer who assumes the four arches have *different leaf schemas* will over-engineer: write **one** serializer, parameterize the two top-level offsets.

### The container blocks (sunda)

| Struct / field | inferred offset | dumped-as key | Confidence |
|---|---|---|---|
| `ncfw_ctx_top_sp.configs` | `ctx+0` | `configs` | HIGH |
| `ncfw_ctx_top_sp.neff` | `ctx+0x3060` (sunda) | `neff` | HIGH |
| `ncfw_ctx_top_sp.algo` | `ctx+0x30C0` (sunda) | `algo` | HIGH |
| `configs.algo` | `+0` | `algo` | HIGH |
| `configs.neff` | `+8744` (`0x2228`) | `neff` | HIGH |
| `configs.dev` | `+12336` (`0x3030`) | `dev` | HIGH |
| `algo(runtime).ring` | `+0` | `ring` | HIGH |
| `algo(runtime).hierarchical` (u8 `run_state`) | `+512` (`0x200`) | `hierarchical` | HIGH |
| `algo(runtime).mesh` (u16 `event_index`) | `+516` (`0x204`) | `mesh` | HIGH |

### The ring channel rows — static (148 B) and runtime (16 B)

The static 148-byte row is owned field-for-field by [The 148-Byte Ring Channel Descriptor](../collectives/channel-descriptor.md); the offsets the serializer dereferences corroborate it. Stride 148 is the outer loop's `imul` arithmetic in `ncfw_log_algo_ring_configs` @`0x8544` (`148LL * i + base`, `i = 0..31`).

| Static row field (`ncfw_log_algo_ring_channels_configs` @`0x6020`) | offset | dumped-as key | Confidence |
|---|---|---|---|
| `next_neigh` (`ring_neighbor`, 28 B) | `+0` | `next_neigh` | HIGH |
| `prev_neigh` (`ring_neighbor`, 28 B) | `+28` | `prev_neigh` | HIGH |
| `recv_sema.addr.soc_addr` (u64) | `+56` | `recv_sema` | HIGH |
| `send_sema.addr.soc_addr` (u64) | `+64` | `send_sema` | HIGH |
| `post_sema.addr.soc_addr` (u64) | `+72` | `post_sema` | HIGH |
| `dma_compl_sema.addr.soc_addr` (u64) | `+80` | `dma_compl_sema` | HIGH |
| `kring_peer_semas` (4× u64) | `+88` | `kring_peer_semas` | HIGH |
| `dma_apb_bcast` (20 B) | `+120` | `dma_apb_bcast` | HIGH |
| `channel_id` (u8; printed from loop arg, §note) | `+140` | `channel_id` | HIGH |
| `spad_slot_idx` (u8) | `+141` | `spad_slot_idx` | HIGH |
| `fold_n` (u8) | `+142` | `fold_n` | HIGH |
| `kangaring_is_primary` (u8) | `+143` | `kangaring_is_primary` | HIGH |
| `kangaring_num_peers` (u8) | `+144` | `kangaring_num_peers` | HIGH |

| Runtime row field (`ncfw_log_algo_ring_ctx` @`0x16a00`; 32× stride 16) | offset | dumped-as key | Confidence |
|---|---|---|---|
| `recv_cnt` (u16) | `+0` | `recv_cnt` | HIGH |
| `send_credit` (u16) | `+2` | `send_credit` | HIGH |
| `repeat_cnt` (u16) | `+4` | `repeat_cnt` | HIGH |
| `m2s_val` (u16) | `+6` | `m2s_val` | HIGH |
| `s2m_val` (u16) | `+8` | `s2m_val` | HIGH |
| *(gap, not dumped)* | `+10..+13` | — | LOW |
| `slot_idx` (u8) | `+14` | `slot_idx` | HIGH |
| `run_state` (u8) | `+15` | `run_state` | MED |

### The nested ring records

| Record / field | offset | dumped-as key | Confidence |
|---|---|---|---|
| `ring_neighbor.net_idx_addrs[fold_n]` (u64 ea) | `+0` (8×i) | `net_idx_addrs` | HIGH |
| `ring_neighbor.fold_n` (u8, loop bound) | `+24` | `fold_n` | HIGH |
| `ring_neighbor.type` (u8) | `+25` | `type` | MED |
| `kring_peer_semas.mine.addr.soc_addr` | `+0` | `mine` | HIGH |
| `kring_peer_semas.peers[0..2].addr.soc_addr` | `+8`/`+16`/`+24` | `peers[].{peer_id,addr}` | HIGH |
| `dma_channel_apb_bcast.m2s_tail_ptr` (u64 soc_addr) | `+0` | `m2s_tail_ptr` | HIGH |
| `dma_channel_apb_bcast.s2m_tail_ptr` (u64 soc_addr) | `+8` | `s2m_tail_ptr` | HIGH |
| `dma_channel_apb_bcast.mask` (u32) | `+16` | `mask` | HIGH |

### The cc_op scheduler descriptor

Owned by [The cc_op_entry On-Device Collective ISA](../collectives/cc-op-isa.md); the serializer's deref (`*p & 0xF` at `0x1a43`, confirmed `objdump`) corroborates the packed layout. `spad_ctrl_entry = {u8 header @+0; cc_op_entry @+1}`; `ncfw_log_spad_ctrl_entry` @`0x3424` `lea +1`s before the cc_op call.

| cc_op_entry field | offset | bits | dumped-as key | Confidence |
|---|---|---|---|---|
| `algo_type` | `+0` | `[3:0]` | `algo_type` | HIGH |
| `algo_sub_type` | `+0` | `[6:4]` | `algo_sub_type` | HIGH |
| `trigger_next` | `+0` | `[7]` | `trigger_next` | HIGH |
| `reporter` | `+1` | `[0]` | `reporter` | HIGH |
| `ring_wait_complete` | `+1` | `[1]` | `ring_wait_complete` | HIGH |
| `ring_send_complete` | `+1` | `[2]` | `ring_send_complete` | HIGH |
| union `ring.channel_list` (u32) | `+3` | — | `channel_list` | HIGH |
| union `mesh.sema_shift_offset` (u16) | `+3` | — | `sema_shift_offset` | HIGH |
| union `mesh.sema_mask` (u16) | `+5` | — | `sema_mask` | HIGH |

The serializer dumps **both** the `ring` and `mesh` views unconditionally (no `algo_type` branch) — the union is overlaid storage at `+3`, and the reader must apply `algo_type` to know which view is live.

### The NEFF barrier subtree

| Struct / field | offset | dumped-as key | Confidence |
|---|---|---|---|
| `neff_ctx.op` | `+0` | `op` (→ op_ctx) | HIGH |
| `neff_ctx.basic_block` | `+32` | `basic_block` | HIGH |
| `neff_ctx.barrier_completed` (u8) | `+92` | `barrier_completed` | HIGH |
| `op_ctx.ctrl_entry` (spad_ctrl_entry) | `+0` | `ctrl_entry` | HIGH |
| `op_ctx.tpb_compl[i].addr.soc_addr` (u64) | `+8 + 16*i`, i=0..1 | `tpb_compl` | HIGH |
| `op_ctx.id` (u32; overlaps tpb_compl[1] region) | `+24` | `id` | MED |
| `op_ctx.reporter` (u8) | `+28` | `reporter` | MED |
| `neff_barrier_config.execute_device_barrier` (u8) | `+292` bit0 | `execute_device_barrier` | HIGH |
| `device_barrier_config.dma_sync_sema` | `+208` | `dma_sync_sema` | HIGH |
| `device_barrier_config.dma_apb_bcast` (20 B) | `+240` | `dma_apb_bcast` | HIGH |
| `device_barrier_config.start_network_proxy` (soc_addr) | `+260` | `start_network_proxy` | HIGH |
| `device_barrier_config.dma_sync_sema_value` (u32) | `+268` | `dma_sync_sema_value` | HIGH |
| `device_barrier_config.tdrbp_low` (u32) | `+272` | `tdrbp_low` | HIGH |
| `device_barrier_config.tdrbp_high` (u32) | `+276` | `tdrbp_high` | HIGH |
| `device_barrier_config.desc_count` (u32) | `+280` | `desc_count` | MED |
| `device_barrier_config.dma_engines_bitmap` (u16) | `+288` | `dma_engines_bitmap` | MED |
| `device_barrier_config.queue_id` (u8) | `+290` | `queue_id` | MED |
| `barrier_step_config[i]` (stride 52): `m2s_val`/`s2m_val` (u16) | `+0`/`+2` | `m2s_val`/`s2m_val` | HIGH |
| `barrier_step_config[i].semaphores[]` (soc_addr) | `+4` | `barrier_sema` | HIGH |
| `barrier_step_config[i].target_sema_val[]` (u32) | `+36` | `target_sema_val` | HIGH |
| `host_barrier_config.{barrier_start,barrier_done}.addr.soc_addr` | `+0`/`+8` | `barrier_start`/`barrier_done` | HIGH/MED |

### The config / dev / basic-block / tsync leaves

| Struct / field | offset | dumped-as key | Confidence |
|---|---|---|---|
| `dev_configs.tsync` | `+0` | `tsync` | HIGH |
| `dev_configs.tpb_ctrl_ack` (u64) | `+36` | `tpb_ctrl_ack` | MED |
| `dev_configs.tpb_id` (u8 lo nibble) / `seng_id` (hi nibble) | `+44` | `tpb_id`/`seng_id` | MED |
| `dev_configs.dev_id` (u8 lo nibble) | `+45` | `dev_id` | MED |
| `configs_dev_tsync` 3× soc_addr | `+8`/`+16`/`+24` | `eng_tpb`/`timestamp_local`/`timestamp_tpb` | HIGH |
| `configs_dev_tsync.timestamp_tpb_val` (u32) | `+32` | `timestamp_tpb_val` | HIGH |
| `configs_neff_dma_alloc_bitmap[i]` (8× stride 8): `queue_id`/`dma_engines_bitmap` (u32) | `+0`/`+4` | `queue_id`/`dma_engines_bitmap` | HIGH |
| `basic_block_ctx.ctrl_spad_addr.soc_addr` (u64) | `+48` | `ctrl_spad_addr` | HIGH |
| `basic_block_ctx.curr_id` (u32) | `+56` | `curr_id` | MED |
| `basic_block_table[i]` (≤255× stride 4): `desc_count` (u16) / `reset_section_desc_count` (u8) | `+0`/`+2` | `desc_count`/`reset_section_desc_count` | HIGH |
| `neff_configs.dma_alloc_bitmap` | `+0` | `dma_alloc_bitmap` | HIGH |
| `neff_configs.barrier_configs` | `+2200` | `barrier_configs` | HIGH |
| `neff_configs.basic_block_configs` | `+2496` | `basic_block_configs` | HIGH |
| `neff_configs.op_num` (u32) | `+3584` | `op_num` | HIGH |
| `neff_configs.tpb_compl_addr_num` (u8) | `+3590` | `tpb_compl_addr_num` | HIGH |
| `neff_configs.leader` (bit0) | `+3591` | `leader` | HIGH |

> **CORRECTION — the `s2m_high` key is absent from the binary.** `ncfw_log_dma_reprogram_info` @`0x4571` dumps a 16-byte struct (`m2s_low @+0`, `m2s_high @+4`, `s2m_low @+8`, `s2m_low @+12`) but the `.rodata` carries only three keys: `m2s_low` @`0x65137`, `m2s_high` @`0x6513f`, `s2m_low` @`0x65148` — and `strings libncfw.so | rg -w s2m_high` returns **nothing**. The `+12` field is dumped under a *second* `s2m_low` key, almost certainly a firmware-side label bug for the intended `s2m_high`. The offsets are HIGH (the dereference is literal); that the duplicated key drops a distinct field is the inferred part. This family has no sunda caller (§3), so the bug never reaches the production dump on arch-5.

---

## 5. Considerations

- **The serializer reflects, it does not define.** libncfw is the only host-side window into the CC-context schema, but it does **not** define the on-device *meaning* of the scheduler — that runs in the Xtensa IRAM image and is partially opaque behind the sequencer TIE ([The NCFW Sequencer](ncfw-sequencer.md)). The struct is *built* by libnrt's collectives layer ([The 148-Byte Ring Channel Descriptor](../collectives/channel-descriptor.md), [The cc_op_entry On-Device Collective ISA](../collectives/cc-op-isa.md)) and *executed* by firmware; libncfw only mirrors the bytes. *(layout: HIGH; firmware behavior: out of scope.)*
- **`hierarchical_configs` is a stub.** `ncfw_log_algo_hierarchical_configs` @`0x18fcb` emits the literal `__stub` (key @`0x65648`) rather than walking a hierarchical descriptor — the static hierarchical config view is unimplemented in this version. The runtime view (`ncfw_log_algo_hierarchical_ctx` @`0x183c6`) does dump one byte (`run_state`). A reimplementer should treat the static hierarchical descriptor as absent here, not infer a layout from the empty stub.
- **Per-arch leaf identity is asserted, not exhaustively byte-diffed.** The cayman/mariana/mariana_plus clone leaf offsets are taken to match sunda because the clone bodies look identical and the four `.rodata` key bands are byte-identical; only the two top-level context offsets were proven to differ. Leaf-offset identity across v3/v4/v4+ is **MEDIUM**. *(D4 deep-dive: byte-diff the v3/v4/v4+ `ctx_log` copies to confirm.)*
- **The `op_ctx +24` ambiguity.** `op_ctx.id` (`+24`) overlaps the `tpb_compl[1]` address region (`+8 + 16×1 = +24`). This is either a union or a short-struct; the serializer dumps both, so the JSON shows `tpb_compl[1]` and `id` reading the same bytes. Unresolved (**MED**).
- **Interior offsets not fully derived.** `configs_algo_mesh_events` (80-byte row interior), `basic_block_configs`, `neff_barrier_step` sub-record interiors, and `dma_alloc_bitmap` element fields are recovered by *name* (verbatim keys) but their exact in-row offsets were not all derived from the dereferences. Marked **MED** in §4 where applicable.

---

## Related Components

| Name | Relationship |
|---|---|
| `libncfw_ctx_log` (`@0x1309`) | The dispatch entry that selects the arch and enters this tree |
| `ncfw_log_buffer_full` (`.bss @0x95029`..`0x9502c`) | The four per-arch truncation latches that gate the `28`/ENOSPC return |
| `prep_metaring_topsp_config` (`@0x2331d0`, libnrt) | The producer of the 148-byte ring rows this tree reflects |
| `create_spad_ctrl_entry` (`@0x232cd0`, libnrt) | The producer of the `cc_op_entry` this tree reflects |
| Xtensa sync-core IRAM (the eight blobs) | The executor that consumes the struct this tree mirrors |

## Cross-References

- [The 148-Byte Ring Channel Descriptor](../collectives/channel-descriptor.md) — the static ring-channel config row, field-for-field; this tree's `ncfw_log_algo_ring_channels_configs` reflects it
- [The cc_op_entry On-Device Collective ISA](../collectives/cc-op-isa.md) — the per-op scheduler descriptor `ncfw_log_spad_ctrl_cc_op_entry` dumps, with the ring/mesh union proof
- [Collectives Firmware — Section Map (overview)](overview.md) — the two-job map, the `5/12/20/28` arch key, and the `38 families × 4 = 152` census this page grounds
- [The Carrier Library (libncfw.so)](carrier-library.md) — the host object, the provider ABI, and the serializer dispatch + convention this page extends
- [The NCFW Sequencer (Xtensa LX Disassembly)](ncfw-sequencer.md) — the on-device executor that consumes the CC-context this tree reflects
- [back to index](../index.md)
