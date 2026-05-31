# Data Structure Layouts

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

This page documents the key internal data structures in ptxas v13.0.88: the compilation context ("god object"), the Ori Code Object, symbol tables, constant/shared memory descriptors, the pool allocator's object model, and the generic container types (hash maps, linked lists, growable arrays) that underpin nearly every subsystem.

All offsets are byte offsets from the structure base unless otherwise noted. Types are inferred from decompiled access patterns. Field names are reverse-engineered -- the binary is stripped.

## Compilation Context (the "God Object")

The compilation context is the central state object passed to every phase in the pipeline. It is not the Code Object (which is per-function); it is the per-compilation-unit container that owns the Code Object, the knob system, the output stream, the function list, and all per-pass configuration. The `sub_7FBB70` (PerKernelEntry) function receives this as `a1`, and every phase's `execute()` receives it as the second argument.

The context is a polymorphic C++ object with a vtable at offset +0. It is allocated by the compilation driver and persists for the lifetime of a single compilation unit. Key observations:

- The vtable at `+0` provides 263+ virtual methods (vtable spans to offset 2104+)
- The object's struct size is **1832 bytes** as recorded in `*(ctx+1536)` by the deep-clear routine `sub_C1B7A0` (`*(a1+1536) = 1832`), but the constructor `sub_7F7DC0` writes fields out to **+2136**, indicating the in-memory layout extends slightly past the nominal struct size for tail extension fields
- The knob/options system is accessed through an indirection at `+1664` (pointer to knob container object)
- The output stream lives at `+1440`

The full constructor is `sub_7F7DC0` (1270 lines). The map below was rebuilt by:

1. Reading every initialization line of `sub_7F7DC0` (the canonical ctor) to nail down field types from initial values and accessor widths
2. Cross-referencing 29 phase-execute functions where `a1` is provably the ctx (filtered by requiring both `*(a1+1584)` and `*(a1+1664)` accesses in the same file) and tallying offset access frequency with `rg --no-filename --only-matching '\(a1 \+ \d{3,4}\)' | sort | uniq -c`
3. For each top-frequency offset, opening the use sites to derive the semantic role from how the value is written, read, indexed, dispatched, and gated against

### Compilation Context Field Map

The map is grouped by functional region rather than strict offset order, so that callers reading the cleanup code (which iterates by region) and callers reading dispatch code (which jumps between regions) can both find what they need.

#### Header / Lifetime / Allocator (+0..+96)

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +0 | `vtable*` | `vtable` | `*(_QWORD *)a1` in every virtual dispatch; ctor `sub_7F7DC0` line 176 sets `*(a1)=a3` |
| +8 | `ptr` | `parent` / `driver_ctx` | Back-pointer; `sub_A3A7E0` reads `v2 = *(a1+8)` then `v2[198]` for Code Object |
| +12 | `u32` | `flags_word_a` | Ctor `sub_7F7DC0:177`: `*(a1+12) = 0` |
| +16 | `allocator*` | `master_allocator` | Pool allocator object (656 bytes, vtable `off_21DBC80`); allocated in ctor lines 178-216 with size 10240; the value at +16 is the canonical handle used by every other field that needs to allocate |
| +24 | `refcount*` | `weak_ref_block_a` | Ctor lines 219-229: 24-byte refcount block, refcount=1 |
| +32 | `refcount*` | `weak_ref_block_b` | Ctor lines 225-229: second 24-byte refcount block; vtable[16] used for allocation of size 24 |
| +40 | `vtable*` | `secondary_vtable` | Ctor line 233: `*(a1+40) = off_21DBEF8` (sub-object vtable for stream/iostream-style operations) |
| +48 / +56 | `ptr` | `sub_alloc_view_1/2` | Ctor lines 234-235: `*(a1+48)=v16, *(a1+56)=0` (allocator views for sub-object) |
| +64 | `ptr` | (zero-init) | Ctor line 238 |
| +72 | `u32` | (zero-init counter) | Ctor line 239 |
| +80 | `allocator*` | `first_lookup_container_alloc` | Ctor line 236: `*(a1+80) = v16`. **Correction**: the +80..+100 region is a growable-container header in the standard ptxas layout, not a standalone `last_exit_code_alloc` field. Ctor line 603 calls `sub_7F0C10((_QWORD*)(a1+80), 512)`, which is the generic grow helper whose argument is the container base. Inside `sub_7F0C10` (`sub_7F0C10_0x7f0c10.c:13-34`), the helper reads `*((unsigned int*)a1+5)` = offset +20 from its argument as the capacity, and `*((int*)a1+4)` = offset +16 as the current count. So the container laid over +80 has: allocator@+80, buffer@+88, count@+96 (u32), capacity@+100 (u32). The 512 capacity suggests a symbol-id or name-hash table companion to the +144 name table |
| +88 | `ptr` | `first_lookup_container_buffer` | Ctor line 240: `*(a1+88) = 0`. Buffer pointer for the +80 container; after the ctor:603 grow-to-512 call, this points at an array of 512 qwords (4 KiB) |
| +96 | `i32` | `compile_unit_index` / `container_count` | Ctor line 231: `*(_QWORD*)(a1+96) = 0xFFFFFFFFLL` -- a **qword** write of -1 that sets `+96` to `-1` (the container count-sentinel) and `+100` to `0` (initial capacity). `sub_C64F70:71` writes the low 32 bits into the +20 slot of every timing record (`v46 = *(_DWORD*)(*a1+96)`). **Partial**: the original documentation called this `compile_unit_index`, but the qword-init pattern is identical to the count/capacity init used by every other container in the ctx. Either (a) +96 genuinely serves dual duty as container-count **and** cu-index (unlikely because the two would collide on grow), or (b) it is the container count and the "cu_idx" column in timing records is actually a phase-sequence number. Marked partial until a disambiguating write site is found |
| +100 | `u32` | `container_capacity_or_reserved` | Implicit from ctor:231 qword write setting the high dword to 0, and from `sub_7F0C10`'s generic grow helper reading offset +20 as capacity. After ctor:603 line, this becomes `512`. Marked **partial** -- could also be a tail half of +96 if +96 is truly a qword semantic field |

#### Embedded Hash-Map Containers, Bin 1 (+104..+200)

This bin holds three or four small hash-map / sorted-array containers. The allocator view at +16 is used to allocate their bucket arrays. Each container has the layout `{header*, ptr, ptr, ptr, ptr, u32 count}` packed across ~24-40 bytes.

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +104 | `ptr` | `bin1_container_a_hdr0` | Ctor line 241: `*(_QWORD*)(a1+104) = 0`. First qword of an opaque five-qword region that looks like a second growable-container or hash-map slot. Not wrapped by `sub_7F0C10`, which suggests it is a manually-managed structure rather than the standard `{alloc, buf, count, cap}` layout -- possibly a small `std::list`-style sentinel node (prev/next/head/tail/count) |
| +112 | `ptr` | `bin1_container_a_hdr1` | Ctor line 242: `*(_QWORD*)(a1+112) = 0` |
| +120 | `ptr` | `bin1_container_a_hdr2` | Ctor line 243: `*(_QWORD*)(a1+120) = 0` |
| +128 | `ptr` | `bin1_container_a_hdr3` | Ctor line 244: `*(_QWORD*)(a1+128) = 0` |
| +136 | `u32` | `bin1_container_a_count` | Ctor line 245: `*(_DWORD*)(a1+136) = 0`. Only the low dword is written (ctor uses `_DWORD *`), confirming that the four preceding qwords are pointers and +136 is a counter, not part of a larger pointer array. The five-field layout `{ptr, ptr, ptr, ptr, u32}` is the canonical ptxas intrusive-list-sentinel-plus-count pattern |
| +144 | `allocator*` | `name_table_alloc` | Allocator handle for the name table; ctor line 237 sets `*(a1+144)=v16` and `sub_7F0C90((a1+144), 1)` initializes the table at line 247 |
| +152 | `ptr` | `name_table_buffer` | Bucket / element array for name table; ctor line 246: `*(a1+152) = 0`; later grown by `sub_7F0C90(a1+144, 64)` at line 630 |
| +160 | `i32` | `name_table_count` | Ctor line 232 sets `0xFFFFFFFFLL` (-1 sentinel = empty), line 309 then sets to `0`; `sub_C64F70:68` reads `*(*a1+160)` as `v8` and writes it into timing records |
| +168 | `i32` | `name_table_capacity` | Implied from grow path |
| +200 | `ptr` | `name_alt_array` | Ctor line 315: separate 24-byte refcount-managed buffer for name aliases |

#### Function Object Table (+208..+340)

The most-accessed structural region of the ctx. This is the **function-id-indexed lookup table**: given a function id, dereference `*(a1+296) + 8*id` to get the per-function `Code Object` pointer. The table is a growable inline array (the inline buffer immediately follows the metadata fields).

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +208 | `__m128i` | (init constant) | Ctor line 325: `_mm_load_si128(xmmword_21D24D0)` |
| +224 | `i32` | `func_table_iter_state` | Ctor line 337: `*(a1+224)=0` |
| +232 | `ptr` | `current_ori_insn` | **Most-accessed** uncharacterized field. Stores the Ori instruction currently being legalized so that error reporters can blame the right line. Written by `sub_9BCBA0:300` (`*(a1+232) = a2`), `sub_8127C0:1246` (`*(a1+232) = v24` next to insn-loc store), `sub_1246DF0:138`, `sub_80A730:240`, etc. Cleared to 0 in ctor line 338 |
| +240 | `i32` | `current_pipeline_phase_bin` | Ctor line 339: `*(a1+240) = 7`; observed used as a 3-bit bin tag for grouping IR transforms |
| +248..+251 | `u32` | (init zero) | Ctor (4 zeroed dwords from 252 region) |
| +252 | `i32` | `current_func_table_count` | Ctor line 340: `*(a1+252)=0`; written/read alongside +296 in many phases |
| +256..+261 | `u8[6]` | `legalization_state_bytes` | Ctor lines 341-346: six bytes individually zeroed; observed used as bitfields for "this insn is in legalization-pending state" |
| +264 | `i32` | `current_ori_insn_loc` | **Paired with +232**: source location of the current insn. Loaded as `*(_DWORD *)(insn+20)` and written every time `+232` is updated. Confirmed in `sub_9BCBA0:298`, `sub_1246DF0:139`, `sub_8127C0:1247`, `sub_80A730:281` (all show `*(a1+264) = *(_DWORD *)(insn+20)`) |
| +272 | `node*` | `pending_legalize_list_head` | Linked list head used by `sub_752CF0:17` to walk Ori instructions whose opcode bits match `(opcode & 0xFFFFCFFF) == 0x89` (a specific instruction class). Each node has `next = *(v+8)` and a payload at +72/+84/+204/+232/+264. Confirmed iterating list in `sub_18F4850:89-117` (`v8 = *(a1+272); ...; v8 = *(_QWORD *)(v8+8)`) |
| +280 | `ptr` | `current_iter_node` | Pointer used as a moving cursor on `+272` list during phase iteration; `sub_7846F0:201` reads `v7 = *(a1+280)` then dereferences `*v7+21` for an instruction id |
| +288 | `allocator*` | `func_table_alloc` | Ctor line 328: `*(a1+288) = v23`; the allocator handle used by the grow path at line 611 |
| +296 | `Code Object**` | `function_table_buffer` | **Documented** -- the function-id-indexed lookup table. `*(a1+296) + 8*id` returns the per-function `Code Object`. Read at hundreds of sites |
| +304 | `i32` | `function_table_count` | Ctor line 324 sets `0xFFFFFFFFLL` (-1 sentinel); grows on demand. Read as `*(_DWORD *)(a1+304)` everywhere a phase iterates over all functions, e.g., `sub_781F80:329`, `sub_7846F0:217`, `sub_A0F020:361` |
| +308 | `i32` | `function_table_capacity` | Ctor line 605: `v55 = *(_DWORD *)(a1+308); if (v55 <= 127) { v56 = grow ... }` |
| +312 | `allocator*` | `func_table_alloc_view` | Same allocator, second handle (used by grow loop) |
| +320 | `Code Object**` | `function_table_buffer_alt` | Secondary buffer pointer used by grow path at ctor line 1239-1262 |
| +328 | `i32` | `function_table_count_alt` | Ctor line 326, line 1235: `v157 = *(a1+328)` |
| +332 | `i32` | `function_table_capacity_alt` | Ctor line 1236: `v158 = *(a1+332)` |
| +336 | `allocator*` | `func_table_alloc_view2` | Ctor line 330 |

#### Function Name Table (+344..+416)

A second growable array, parallel to the function object table, storing per-function names.

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +344 | `ptr` | (zero) | Ctor line 350 |
| +352 | `i32` | `name_table_alt_count` | Ctor line 331: `0xFFFFFFFFLL` |
| +360 | `allocator*` | `func_name_alloc` | Ctor line 332 |
| +368 | `name**` | `function_name_array` | **Documented** -- buffer pointer |
| +376 | `i32` | `function_name_count` | Ctor line 333: `0xFFFFFFFFLL` (-1 = empty); read at `sub_781F80:982`, `sub_A0F020:141` (`for (i=0; i <= *(a1+376); ++i)`) |
| +380 | `i32` | `function_name_capacity` | Ctor line 631: `v61 = *(_DWORD *)(a1+380); if (v61 <= 15) ...` (grow path) |
| +384 | `allocator*` | `func_name_alloc_view` | Ctor line 334 |
| +392 / +400 / +408 | `ptr` / `i32` / `ptr` | additional small buffers | Ctor lines 335-336 |
| +416 | `ptr` | (zero) | Ctor line 353 |

#### Code Object Stage Buffers (+424..+712)

A bank of growable arrays each managed by the same `{allocator, buffer, count, capacity}` quad pattern. Used to hold per-stage transient data (intermediate results between consecutive passes).

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +424 | `i32` | (sentinel -1) | Ctor line 354 |
| +432 | `allocator*` | `stage_array_alloc_a` | Ctor line 358: `*(a1+432)=v23`; line 374-375 dispatches `(**(a1+432)+32)(a1+432)` to free |
| +440 | `i32*` | `stage_array_buffer_a` | Ctor line 378: stores newly allocated buffer; sized as `4*count + 4` |
| +448 | `i32` | `stage_array_count_a` | Ctor lines 364/377/413: -1 sentinel, then 0 |
| +452 | `i32` | `stage_array_capacity_a` | Ctor line 379: `*(a1+452) = 1` |
| +456 | `allocator*` | `stage_array_alloc_b` | Ctor line 403 |
| +464 / +472 / +480 / +488 | various | additional growable-array slots | Ctor lines 391-405 (see "Inline buffer block" below) |
| +496 / +504 | `i32 / allocator*` | -1 sentinel / allocator | Ctor lines 392/406 |
| +512 | `i32*` | `function_worklist_buffer` | Active function-id worklist (typically a `int[N]`). Ctor line 400. Read with `*(_DWORD *)(*(a1+512) + 4*idx)` -- e.g., `sub_796D60:102-104` indexes into the function table via this list |
| +520 | `i32` | `function_worklist_count` | Ctor line 394: `0xFFFFFFFFLL` (sentinel). Read at `sub_796D60:95` (`v36 = *(a1+520)`), `sub_781F80:349`, `sub_793220:87`. Often used as `for (i=1; i <= *(a1+520); ++i)`, indicating 1-based indexing |
| +528 | `allocator*` | `worklist_alloc` | Ctor line 409 |
| +536 / +544 | `i32 / i32` | sentinel slots | Ctor lines 396/402 |
| +552 | `inline_buf*` | `worklist_inline_storage` | Ctor line 393: `*(a1+552) = a1+576` (small-buffer optimization base) |
| +560 | `u64` | `worklist_inline_capacity` | Ctor line 395: `0x500000000LL` (capacity = 5 in upper dword) |
| +568 | `allocator*` | `worklist_alloc_view` | Ctor line 410 |
| +576..+615 | `i32[5]` (inline) | `worklist_inline_buffer` | The actual 5-element inline storage area for the worklist; if more than 5 entries are needed, the heap path at +512 takes over |
| +616 | `inline_buf*` | `secondary_inline_storage` | Ctor line 399: `*(a1+616) = a1+640` |
| +624 | `u64` | `secondary_inline_capacity` | Ctor line 401: `0x200000000LL` (capacity = 2) |
| +632 | `allocator*` | `secondary_alloc_view` | Ctor line 411 |
| +640..+655 | `i32[2]` (inline) | `secondary_inline_buffer` | 2-element inline buffer |
| +672 | `inline_buf*` | `tertiary_inline_storage` | Ctor line 405: `*(a1+672) = a1+696` |
| +680 | `u64` | `tertiary_inline_capacity` | Ctor line 407: `0xA00000000LL` (capacity = 10) |
| +688 | `allocator*` | `tertiary_alloc_view` | Ctor line 412 |
| +696..+735 | `i32[10]` (inline) | `tertiary_inline_buffer` | 10-element inline buffer |

#### Call-Graph / Traversal State (+736..+1008)

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +736 | `refcount*` | `traversal_refcount_a` | Ctor lines 415-419 allocate a 24-byte refcount block |
| +744 / +752 / +760 | `ptr` / `ptr` / `ptr` | linked list head/tail/iter | Ctor lines 421-423; subsequent passes traverse via these |
| +776 | `u8*` | `opcode_attribute_table` | **Correction** -- not a linked-list slot. Ctor lines 904-932 allocate a **1428-byte** buffer via `(*(a1+16)->alloc)(v139, 1428)`, write `*v140 = 355` into the first 4 bytes (marker / max opcode id), zero-initialize the rest in a 4-qword-stride loop, then store `*(a1+776) = v141` where `v141 = v140 + 1` (i.e., **the pointer skips the 8-byte header**). Immediately afterward, ctor lines 934-1229 OR individual flag bits into byte offsets `v142[17]`, `[25]`, `[29]`, `[37]`, `[64]`, ..., `[1420]`, `[1424]` -- the decompiler types `v142` as `_QWORD*` but the bounds and `\|= 0xXX` patterns prove it is effectively a `_BYTE*` with byte indices. This buffer is the **per-opcode attribute/capability bitmap**: each of the ~355 Ori opcodes has one or more attribute bytes describing legality, pool, scheduling class, etc. The mask values observed (0x9C, 0x11, 0x1C, 0x1E, 0x40, 0x80, ...) select distinct attribute subsets. Observed masks stored per-opcode include: 0x01 (valid), 0x02 (has-side-effect), 0x04 (reads-memory), 0x08 (writes-memory), 0x10 (late-expansion), 0x20 (sm9+-only), 0x40 (cluster-aware), 0x80 (dual-issue) -- these are inferred from usage context, and the table should be treated as **partial** until each bit is cross-referenced with a phase-execute body that tests it |
| +784 | `allocator*` | `opcode_attribute_table_alloc` | Ctor line 933: `*(a1+784) = v139` where `v139 = *(_QWORD*)(a1+16)`. Paired with +776: `sub_7F7DC0:928-930` uses `*(_QWORD*)(a1+784)` to free an earlier buffer (the deallocator call dereferences `*(a1+784)` and dispatches vtable[32]) |
| +792 | `cg_node*` | `call_graph_object` | Lazily allocated by `sub_784220` if null. Holds a function pre-order walk over the call graph; `*(cg+0) = node_count`, `*(cg+8) = i32* node_array`, `*(cg+16) = allocator`, `*(cg+24) = visited_flag`. Read in `sub_793220:62` (`*(_DWORD *)(a1+940) = *(_DWORD *)(v2+4)`), `sub_A0F020:159, 190, 201, 205`. Allocated using `4 * (function_count+1) + 8` bytes |
| +800 | `allocator*` | `call_graph_alloc` | Ctor line 434; passed as second handle in `sub_784220:69` |
| +808 / +816 | `ptr` | aux ptrs | Ctor lines 435-437 |
| +824 | `i32` | `current_func_id` | Ctor line 438: `0xFFFFFFFFLL` (-1 = "no current function"). Read by phase passes that need to know which function is being processed (`sub_7846F0:210, 220` -> `sub_923600(a1, *(a1+824))`), `sub_A0F020:107` |
| +832 / +840 | `ptr` | aux growable-array slots | Ctor lines 439-440 |
| +848 | `i32` | `aux_count` | Ctor line 441 |
| +856 / +864 / +872 | various | linked list slots | Ctor lines 442-444 |
| +880 | `__m128i` | (SSE init) | Ctor line 428 from `xmmword_2027590` |
| +896 | `i32` | `assembler_mode` | Ctor line 445; read in `sub_784220:258` (`if (*(a1+896) == 4) sub_74AEE0(...)`) -- value 4 triggers a special handling path |
| +900 | `i32` | `cluster_dimension_mode` | Ctor lines 849-861: set from `*(a2+1796)` -- 0/1/2 selecting cluster geometry mode (0=none, 1=auto, 2=explicit) |
| +904 / +908 | `u8` / `u8` | bit flags | Ctor lines 446-447 |
| +912 / +940 | `_OWORD` | SSE-init slots | Ctor lines 430, 433 |
| +928 | `i32` | (zero) | Ctor line 448 |
| +932 | `i32` | `relocation_id_seed` | Ctor line 449: `*(a1+932) = -1` |
| +936 | `u8` | flag | Ctor line 450 |
| +940 | `i32` | `cg_node_count_cached` | Ctor (read from `*(a1+792)+4`); see +792 above |
| +956 | `i32` | `unroll_factor_default` | Ctor line 451: `*(a1+956) = 15` |
| +960 | `allocator*` | (allocator view) | Ctor line 431 |
| +968 / +976 | `ptr` / `i32` | aux array | Ctor lines 452-453 |
| +984 / +1000 | `ptr` | (zero) | Ctor lines 436, 454 |
| +992 | `back_ptr_block*` | `back_ptr_to_self` | Ctor lines 894-900: 32-byte block holding `[a1, allocator, 0, -1]` -- a self-reference used for callback hooks |
| +1008 | `journal*` | `memstate_rewrite_journal_hdr` | 24-byte refcount block `{refcount=2, freelist_head, allocator}` allocated at ctor lines 456-465. **Identified**: per-BB **memory-state-token rewrite journal** — the drain at `sub_8116B0_0x8116b0.c:184-212` walks a trail-bucket array, splicing old values back via `*target = *(header+8); *(header+8) = old_value` (classic undo-trail pattern). Captures rewrites dispatched through `vtable+2456` (operands whose backing descriptor has `*(desc+64)==8`, opcode ≠ 263) during the per-instruction sweep. Supersedes the older "analysis_pool_alloc" label |
| +1016 | `i32` | `memstate_journal_count` | Non-empty DWORD guard checked before drain (`sub_8116B0:184`) |
| +1024 | `rewrite_rec*` | `memstate_journal_bucket_base` | Trail-bucket array (24-byte records `{old_value, target_ptr, tag_dword}`) |
| +1032 | `i32` | `memstate_journal_bucket_size` | Trail-bucket array size |
| +1048 | `journal*` | `ssa_handle_rewrite_journal_hdr` | Second 24-byte refcount block (ctor lines 468-477), identical structure to +1008. **Identified**: **SSA-value handle rewrite journal** — captures operands matched by `sub_7DEB10` (the value/SSA-handle classifier) and dispatched through `vtable+2464`. Drained at `sub_8116B0_0x8116b0.c:213-250` **immediately after** the memstate journal, in deterministic order. Two parallel channels keep per-BB memory-token rewrites and SSA-handle rewrites independent so after-BB commits can roll each back without cross-contamination |
| +1056 | `i32` | `ssa_journal_count` | Non-empty DWORD guard for drain |
| +1064 | `rewrite_rec*` | `ssa_journal_bucket_base` | Trail-bucket array base |
| +1072 | `i32` | `ssa_journal_bucket_size` | Trail-bucket array size |

#### Linked Lists, Hash Maps, and Misc Containers (+1120..+1352)

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +1120..+1136 | `ptr` | (zero-init) | Ctor lines 478-480 |
| +1144 | `node*` | `function_list_head` | **Documented**; ctor line 481 |
| +1152 | `node*` | `aux_list_head_a` | Ctor line 482; written/read in `sub_781F80:384, 389` -- secondary linked list traversed during legalization |
| +1160 | `node*` | `entry_list_head` | **Documented**; ctor line 483 |
| +1168 | `node*` | `aux_list_head_b` | Ctor line 484 |
| +1176..+1231 | `hash_map` (56B) | `embedded_hash_map_a` | Ctor line 485 calls `sub_7F0A90(a1+1176, a1)`. Layout: `{back_ptr_to_ctx, bucket_array_ptr, head, tail, ptr, count}`. Used by sub-passes to map id->object |
| +1232..+1287 | `hash_map` (56B) | `embedded_hash_map_b` | Ctor line 486 calls `sub_7F0B20(a1+1232, a1)`. Same layout as the previous map |
| +1288 / +1296 | `ptr` | linked list ptrs | Ctor lines 488-489 |
| +1304 | `refcount*` | `aux_refcount` | Ctor lines 491-495 |
| +1312 / +1320 / +1328 | `ptr` | aux slots | Ctor lines 497-499 |
| +1344 / +1352 | `ptr` | aux slots | Ctor lines 501-502 |
| +1360 | `subobj*` | `optional_56B_subobj` | Ctor lines 881-892: only allocated if `*(a2+920) > 0`, and bit `+1378 \|= 8` is set. A 56-byte sub-object initialized via `sub_7DC3C0(v135, a1, a2)` |
| +1372 | `i32` | `legalization_iter_phase` | Ctor line 709: `*(a1+1372) = 0`. Read in `sub_7846F0:240` (`if (!*(_DWORD *)(a1+1372) && *(char*)(a1+1415) < 0)`) -- gates a legalization sub-mode |

#### Phase Bitfield Bank (+1368..+1421)

A 54-byte region of densely packed boolean / multi-bit gate flags. The constructor `sub_7F7DC0` lines 686-872 initialize this region by reading individual fields from the option struct (`a2`) and packing them into bit positions. Some bytes (`+1376`, `+1396`, `+1412`, `+1414`, `+1416`, `+1418`) were partially documented before; the table below adds the missing ones and corrects the type widths.

| Offset | Type | Field | Initial value (ctor line) | Bit semantics |
|--------|------|-------|---------------------------|---------------|
| +1368 | `u8` | `pipeline_iter_flags` | line 693: 0 | bit 0x01 = "ConvertUnsupportedOps invoked", bit 0x02 = "diagnostic dump active" (gates `sub_793220:54` lazy init at `+792`), bit 0x10 = "deep mode", bit 0x40 = ?, bit 0x80 = sign-bit checked at `sub_908EB0:60` |
| +1369 | `u8` | `pipeline_iter_flags_b` | line 694: 0 | bit 0x80 cleared at `sub_781F80:359`; sign-bit checked in `sub_752CF0:?` and `sub_793220:52` |
| +1370 | `u8` | `pipeline_iter_flags_c` | line 686: `&= 0xA0`; line 691: `& 0x5F` | bit 0x02 (constructor enables it via `(8 * (a2&1)) \| v35 & 0xB3 \| 4`), bits 0x04, 0x08, 0x10, 0x20, 0x40 each gate distinct legalization sub-passes (see `sub_781F80:325-1383` for full bit-by-bit flow) |
| +1371 | `u8` | `legalize_call_flags` | line 695: 0; line 1375: `\|= 0x80` | bit 0x20 = "scan for opcode 0x89 instructions" (set/checked in `sub_752CF0:14, 29`); bit 0x40 = "found 0x89 instructions to legalize"; bit 0x80 = "all 0x89 instructions handled" |
| +1372 | `i32` | `legalization_iter_phase` | line 709: 0 | See above |
| +1376 | `u8` | `scheduling_mode_flags` | **Documented**: bit 0x08 forward, 0x10 bidirectional, 0x20 disable scheduling | line 696: 0 |
| +1377 | `u8` | `regalloc_flags_a` | line 697: 0 | bit 0x20 cleared at `sub_781F80:358` |
| +1378 | `u8` | `subobj_present_flags` | line 699: 0; line 887: `\|= 8` if `*(a2+920) > 0` | bit 0x04 = "subobj at +1360 was constructed", bit 0x08 = "subobj initialized" |
| +1379 | `u8` | `aux_flags` | line 700: 0 | |
| +1380..+1382 | `u8 x3` | flag bytes | lines 702-704: 0 | +1381 bit 0x40 (already known: cutlass flag, cleared bit 6 in some passes; tested in `sub_913A30:41`); +1382 bit 0x20 ("instruction class scan needed", read in `sub_796D60:63`) |
| +1383 | `u8` | `init_marker` | line 707: `0x80` | Set unconditionally to 0x80 by ctor; never observed cleared (is the "context fully constructed" marker) |
| +1384 | `i32` | `pipeline_progress_b` | line 692-701: zeroed via `& 0xFFFC7FFF` mask | A second progress counter (distinct from +1552); incremented by passes at certain transitions |
| +1385 | `u8` | `phase_bin_flags` | line 689: `&= 0x80` | bit 0x04 read in `sub_796D60:63` |
| +1386..+1389 | `u8 x4` | flag bytes | lines 705, 708, 710-711 | +1387 line 705: `& 0xFC` (clears low 2 bits); +1388 zeroed; +1389 line 690: `&= 0xE0` |
| +1392 | `u8` | flag byte | line 711: 0 | |
| +1393 | `u8` | `option_byte` | line 712: `& 0xF8` | Three-bit option |
| +1396 | `u8` | `phase_58_gate_bits` | **Documented** | Filled bit-by-bit from `*(a2+184), +192, +200, +204, +208, +212, +216` (lines 713-727) |
| +1397 | `u8` | `phase_extension_bits` | line 729: 0 | bits set from `*(a2+220), +224, +228, +248, +232, +280, +316` (lines 728-743). 7 distinct option bits packed |
| +1398 | `u8` | `late_expansion_prereq` | line 744 | bit 0x02 = LateExpansion prerequisite (already known); set unconditionally `\|= 2`; bits 0-1 from `*(a2+1448) & 3`; field finally masked with `& 0x1F` at line 873 |
| +1399 | `u8` | `derived_phase_bits` | line 877 | Computed bit-OR of bits 3 and 4 from `*(a1+1413)` (so this byte is _derived_ from other flags, not from input options) |
| +1400 | `u32` | `error_count_or_threshold` | line 752 | Set from `*(a2+272)` if non-negative; gates rate-limited diagnostic emission |
| +1404 | `u8` | `error_count_set_marker` | line 751 | Set to 1 once `+1400` has been initialized from options |
| +1408 | `u8` | flag byte | line 755: `&= 0xE0` | low 5 bits used as a sub-mode tag |
| +1412 | `u8` | `compilation_flags_byte` | **Documented** | line 756: `& 0xC0` -- ctor packs `*(a2+136), +137, +138, +139` into low bits, then more from `*(a2+1060), +1064` |
| +1413 | `u8` | `optimizer_gate_bits` | lines 759-782 | Most bit-rich byte: ctor sets bit 1 from `*(a2+137)`, bit 0 from `*(a2+138)`, bit 2 from `(a1+1412 & 0x380)==0`, bit 3 from `*(a2+1060)`, bits 4-5 from `*(a2+1064)`, bit 6 from `*(a2+1068)`, bit 7 from `*(a2+424)==1`. Special override line 774-778: `if (*(a2+348) > 36863 && (v91&8)==0 && (v91&0x30)!=0x10) v91 \|= 0x20` -- forces bit 5 on for SM_90+ with certain non-CUTLASS settings |
| +1414 | `u8` | `late_expansion_flags` | lines 786-794 | **Documented** bit 0x02 = LateExpansion prerequisite; ctor packs more bits from `*(a2+140), +1080, +1084, +1088, +144` |
| +1415 | `u8` | `optimization_path_flags` | lines 796-801 | bit 2 from `*(a2+172)`, bit 3 from `*(a2+176)`, bit 4 from `*(a2+180)`. Sign-bit (bit 7) tested at `sub_7846F0:240` to gate the legalization sub-mode |
| +1416 | `u8` | `output_detail_flags` | **Documented**: bits 4-5 control latency reporting | lines 803-807: bit 6 from `*(a2+148)`, bit 2 from `*(a2+156)` |
| +1417 | `u8` | `late_expansion_aux_bits` | lines 808-833 | Most-touched flag byte (modified in 12+ separate ctor lines from many a2 fields including +152, +772, +1516, +324, +1096, +1048, +764) |
| +1418 | `u8` | `codegen_mode_flags` | **Documented** | lines 814-834: packs bits from `*(a2+1112), +1528, +1524, +1100, +1104, +1532` and forces bit 6 ON unconditionally (line 831: `v117 \|= 0x40u`) |
| +1419 | `u8` | `cluster_and_misc_bits` | lines 835-845 | bit 0 from `*(a2+1760)==1`, bit 3 from `*(a2+1116)`, bit 4 from `*(a2+1120)`, bit 5 from `*(a2+1124)`, bit 6 from `*(a2+1128)`, bit 7 from `*(a2+1768)` |
| +1420 | `u8` | `cluster_geometry_bits` | lines 847-866 | bit 0 from `*(a2+1794)`, bit 1 from `*(a2+1792)`, bit 2 from `*(a2+1800)`, bit 3 from `*(a2+1132)`, bits 4-5 from `*(a2+1076) << 4`, bits 6-7 from `*(a2+1076)` -- entirely cluster-launch related |
| +1421 | `u8` | `aggregator_flags` | lines 738/742/867-871 | bit 1 set unconditionally; bits 2-3 from `*(a2+1808) & 3`; bit 6 from `*(a2+1136)`; bit 7 from `*(a2+1140)` |
| +1424 | `i32` | `pipeline_option_word` | line 506 | `*(a2+704)` |
| +1428 | `i32` | `function_index` | **Documented** | line 507: `*(a2+352)` |
| +1432 | `i32` | `loop_unroll_threshold` | line 508 | `*(a2+360)` |

#### Output Stream and Timing Records (+1440..+1656)

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +1440..+1551 | `stream` (~112B) | `output_stream` | **Documented**; ctor line 509: `sub_7F7CB0((a1+1440), a1)` -- the iostream-style output object |
| +1552 | `i32` | `pipeline_progress` | **Documented**; ctor line 524: `*(a1+1552) = 0` |
| +1560 | `allocator*` | `timing_records_alloc` | Ctor line 530: `*(a1+1560) = v44` (allocator handle for the timing growable array). The wiki previously described `+1560` as "the records pointer", but `+1560` is the allocator and `+1568` is the actual records buffer |
| +1568 | `timing_record*` | `timing_records_buffer` | Ctor line 510: `*(a1+1568) = 0`. Each timing record is **32 bytes** with layout `{u32 phase_id, char* phase_name, u32 invocation_depth, u32 cu_index, u32 spare}`. Confirmed in `sub_C64F70:75-81`: writes `*(buffer + 32*idx) = phase_id`, `+8 = name_str`, `+16 = depth`, `+20 = cu_idx`, `+24 = spare` |
| +1576 | `i32` | `timing_count` | **Documented** -- but ctor line 512 actually initializes it to `0xFFFFFFFFLL` (-1) |
| +1584 | `sm_backend*` | `sm_backend` | **Documented** |
| +1592 | `ptr` | (zero-init) | Ctor line 513 |
| +1600..+1648 | `ptr` | aux slots | Ctor lines 514-520; mostly zero-initialized |
| +1648 | `ssa_tracker*` | `ssa_tracker` | A 16-byte tracker object lazily allocated at `sub_A0F020:86-90`. Layout: `{ctx_back_ptr, byte flag_a, byte flag_b}`. Used by the dead-code-elimination / use-def cleanup pass to mark whether SSA needs rebuilding |
| +1656 | `i32` | `aux_counter` | Ctor line 521 |
| +1664 | `knob_container*` | `knob_container` | **Documented** |

#### Output String Buffer (+1672..+1716)

A `std::string`-like growable byte buffer used to accumulate the output filename or module identifier passed in via `*(a2+856)`.

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +1672 | `u64` | `output_string_capacity` | Ctor line 525: 0; checked at line 678 (`if (v74 >= *(_QWORD *)(a1+1672)) sub_66B450(...)` -- realloc) |
| +1680 | `char*` | `output_string_start` | Ctor line 527 |
| +1688 | `char*` | `output_string_pos` | Ctor line 532; advanced via `*(a1+1688) += strlen(filename)` at line 684 |
| +1696 | `allocator*` | `output_string_alloc` | Ctor line 531 |
| +1704 | `i32` | `compile_timeout_ms` | Ctor line 528: `*(a2+116)` -- a timeout/limit option |
| +1708 | `i32` | `verbosity_level` | Ctor line 533: `*(a2+120)` -- verbosity option |
| +1712 | `i32` | `report_format` | Ctor line 534: `*(a2+124)` |
| +1716 | `i32` | `report_level` | Ctor line 540: `*(a2+128)` |

#### Register-File / Inline Limit Block (+1720..+1768)

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +1720 | `i32` | `regfile_size_hint` | Ctor line 538: `*(a1+1720) = 512` (default). Overridden at line 903 from `*(a2+132)` if non-negative. Used as a target register file size budget for register allocation |
| +1728 | `allocator*` | `regalloc_alloc_view` | Ctor line 536 |
| +1736 / +1744 / +1752 | `ptr / i32 / i32` | aux | Ctor lines 539, 537, 541; +1744 = -1 sentinel |
| +1760 | `ptr` | (zero) | Ctor line 543 |
| +1768 | `i32` | `phase_invocation_depth` | Ctor line 544: 0. Read at `sub_C64F70:70` as `v9 = *(*a1 + 1768) + 1` -- this is the per-phase invocation depth that gets recorded into each timing record at offset +16 |
| +1776 | `sched_state*` | `scheduler_state` | Ctor line 545: 0. Holds the live scheduling state during the scheduler pass; layout includes `+16 = bucket_array, +24 = bucket_count, +28 = bucket_capacity` (per `sub_92E1F0:114-127`) |
| +1784 | `latency_model*` | `latency_model` | Ctor line 546: 0. Polymorphic cost-model object with vtable `{[0] is_available() -> bool, [8] estimate_latency(insn*) -> double}`. Used at `sub_8BF4B0:11-30`, `sub_92E1F0:110`, `sub_8D1730:104-265`, `sub_931920:283-307` -- the scheduler asks this object for instruction latencies. See [Scheduling Latency Model](#scheduling-latency-model) below |
| +1792 / +1800 / +1808 / +1816 | `ptr` | aux slots | Ctor lines 547, 549-551 |
| +1824 | `i32` | `aux_state_word` | Ctor line 552 |
| +1832 | `refcount*` | `aux_refcount_b` | Ctor line 548: stores `*(a1+24)` (the second weak ref block); bumps its refcount via `++*v47` |

#### Backend Configuration Block (+1840..+1976)

This region holds pointers and dwords copied from the options struct (`a2`), forming a snapshot of the inputs that drive backend behavior. Many were previously documented as "backend stuff at +18xx".

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +1840 | `ptr` | (zero) | Ctor line 556 |
| +1848 | `i32` | (zero) | Ctor line 557 |
| +1856 | `pool_config*` | `compile_pool_config` | Ctor line 559: `*(a2+88)` -- driver-supplied pool configuration |
| +1864 | `bb_structure*` | `bb_structure` | **Documented** |
| +1872 | `per_func_data*` | `per_func_data` | **Documented** |
| +1880 | `function_context*` | `function_context` | **Documented** |
| +1888 | `optix_config*` | `optix_config` | Ctor line 562: `*(a2+984)` -- pointer to OptiX-specific configuration block (only set when compiling for OptiX IR) |
| +1896 | `i32` | `optix_target_version` | Ctor line 568: `*(int *)(a2+980)` -- companion to +1888 (OptiX target version number) |
| +1904 | `allocator*` | `backend_alloc_view` | Ctor line 563 |
| +1912 | `ptr` | (zero) | Ctor line 566 |
| +1920 | `i32` | `backend_count_sentinel` | Ctor line 564: `0xFFFFFFFFLL` |
| +1928 | `codegen_ctx*` | `codegen_ctx` | **Documented** |
| +1936 / +1944 | `ptr` | (zero) | Ctor lines 569-570 |
| +1952 | `ctx*` | `self_pointer` | Ctor line 571: `*(a1+1952) = a1` -- a self-reference, used by sub-objects that need a back-pointer to the owning context but only have a stable handle on `+1952` |
| +1960 / +1968 | `ptr` | (zero) | Ctor lines 572-573 |
| +1976 | `i32` | (zero) | Ctor line 575 |

#### Post-Instruction Hook Queues + Late Tail (+1984..+2136)

**Correction (was "NvOptRecipe / Late Tail"):** this region is NOT the NvOptRecipe state. `sub_9F4040` (the NvOptRecipe applier, see `phase-manager.md` → Task IR-24 commit `1ac448a`) does not read any offset in [+1984, +2096] on its context parameter. Instead, the region holds **two symmetric post-instruction hook queues** used by the instruction-append callback dispatcher `sub_7DD3C0`. Each queue is a `{head, tail, count}` triple of 8+8+4 = 20 bytes plus 4 bytes of padding per slot, and both queues follow an identical "pending → committed" splice pattern observed in `sub_7D92F0:82-161`, `sub_7B4020:282-348`, `sub_833E40:210-280`, and `sub_856260:260-304`.

Queue A (committed side only, pending side lives before +1984):

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +1984 | `hook*` | `post_insn_hooks_A_committed_head` | `sub_7D92F0:487,505,512` — spliced as `list.head` during pending-flush. **Not** `nvoptrecipe_state` — prior label was demonstrably wrong (the field is the list head of the committed-side post-instruction hooks, not opt-recipe state) |
| +1992 | `hook*` | `post_insn_hooks_A_committed_tail` | `sub_7D92F0:513` — written as `list.tail` during splice |
| +2000 | `i32` | `post_insn_hooks_A_committed_count` | `sub_7D92F0:515` — `*(a1+2000) += v54` where v54 is the old pending count |
| +2008 | `ptr` | _pad / unused_ | Ctor-only zero store; no Compilation-Context reader in the decompiled tree |
| +2016 | `ptr` | _pad / unused_ | Ctor-only |
| +2024 | `i32` | _pad / unused_ | Ctor-only |

Queue B (full pending + committed, 6-slot triple):

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +2032 | `hook*` | `post_insn_hooks_B_pending_head` | `sub_7B4020:282`, `sub_7D92F0:132`, `sub_833E40:210`, `sub_856260:270` — null-check guards the splice |
| +2040 | `hook*` | `post_insn_hooks_B_pending_tail` | `sub_7B4020:322` — used as `list.tail` during splice |
| +2048 | `i32` | `post_insn_hooks_B_pending_count` | `sub_7B4020:323` — added into committed count on splice |
| +2056 | `hook*` | `post_insn_hooks_B_committed_head` | `sub_7B4020:289,318,337,344`; **read by dispatcher `sub_7DD3C0_0x7dd3c0.c:9`** which walks it and dispatches `hook->vtable[0](hook, new_instr)` for every instruction append — this is the actual on-append callback wiring |
| +2064 | `hook*` | `post_insn_hooks_B_committed_tail` | `sub_7B4020:345`, `sub_7D92F0:159`, `sub_7DD3C0:9`, `sub_833E40:242` |
| +2072 | `i32` | `post_insn_hooks_B_committed_count` | `sub_7B4020:347`, `sub_7D92F0:161`, `sub_833E40:244`, `sub_856260:304` |
| +2080 / +2088 / +2096 | `ptr` / `ptr` / `i32` | _pad / unused_ | Ctor-only zero stores; all external readers at these offsets operate on unrelated classes (sub_661950 is a scheduler cost model under vtable `off_21E6D00`, not the Compilation Context under `off_21DBC80`) |
| +2104 | `i32` | `optix_mode` | Ctor lines 574, 579, 661-670: copied from `*(a2+112)`. If `== 0` overridden to 1; if `== 3` overridden to 4. Drives an OptiX-vs-CUDA mode switch |
| +2112 | `ptr` | `extension_object_a` | Ctor line 585: `*(a2+872)` (driver-supplied extension object) |
| +2120 | `ptr` | `extension_object_b` | Ctor line 591: `*(a2+1408)` |
| +2128 | `u8` | `extension_object_b_present` | Ctor line 600: `!v53 = (*(int *)(a2+1416) != 0)` |
| +2132 | `i32` | `tail_sentinel` | Ctor line 599: -1 |
| +2136..+2167 | -- | (cloned-variant tail only, 32 bytes) | The alternate constructor `sub_A60B60` builds a **2168-byte** object (not "24KB RegisterStatCollector" as prior docs claimed) — this is the per-nested-parse PTX input-buffer cursor for nested text re-entry (e.g. `.include` scopes). +2136 = include-buffer-name list head, +2144 = raw buffer read pointer (`const char *`, init 0xFFFFFFFF), +2152 = remaining-bytes DWORD counter, +2160 = saved-line-number linked list head. Evidence: `sub_A60B60_0xa60b60.c:754,771,822`; `sub_71C140_0x71c140.c:31-47` (fills +2136/+2144/+2152 from an `a2` string); `sub_71C910_0x71c910.c:394` error `"ptxset_lineno called with no buffer"`, lines 403-436 pop linked list nodes via `sub_4248B0`, lines 463-497 decrement +2152 per read. The primary constructor `sub_7F7DC0` tops out at +2132 and never touches this tail |

### SM Backend Object at +1584

The pointer at `context+0x630` (decimal 1584) is the single most confusing field in the compilation context, because it serves multiple roles through a single polymorphic C++ object. Different wiki pages historically called it different names depending on which role they observed:

- **Legalization pages** see it dispatching `MidExpansion`, `LateExpansionUnsupportedOps`, etc., and call it "SM backend" or "arch_backend"
- **Scheduling pages** see it providing hardware latency profiles at `*(sm_backend+372)` and call it "scheduler context" or "hw_profile"
- **Optimization pages** see it dispatching `GvnCse` (vtable[23]) and `OriReassociateAndCommon` (vtable[44]) and call it "optimizer state" or "function manager"
- **Codegen/template pages** see it holding register file capacity at `+372` and hardware capability flags at `+1037`

**It is one object.** The canonical name is **`sm_backend`**. It is constructed per-compilation-unit in `sub_662920` with a switch on SM version bits (`v3 >> 12`). Each SM generation gets a different-sized allocation and a different vtable:

| SM Case | Size | Base Constructor | Vtable | SM Generations |
|---------|------|------------------|--------|----------------|
| 3 | 1712B | `sub_A99A30` | `off_2029DD0` | sm_30 (Kepler) |
| 4 | 1712B | `sub_A99A30` | `off_21B4A50` | sm_50 (Maxwell) |
| 5 | 1888B | `sub_A99A30` | `off_22B2A58` | sm_60 (Pascal) |
| 6 | 1912B | `sub_A99A30` | `off_21D82B0` | sm_70 (Volta) |
| 7 | 1928B | `sub_ACDE20` | `off_21B2D30` | sm_80 (Ampere) |
| 8 | 1992B | `sub_662220` | `off_21C0C68` | sm_89 (Ada) |
| 9 | 1992B | `sub_662220` | `off_21D6860` | sm_90+ (Hopper/Blackwell) |

Key sub-fields on the SM backend:
- `+372` (`i32`): codegen factory value / encoded SM architecture version (e.g., 28673 = sm_80)
- `+1037` (`u8`): hardware capability flags (bit 0 = has high-precision FP64 MUFU seeds)
- Vtable slots provide architecture-specific dispatch for 50+ operations

### Pipeline Progress Counter at +1552

The field at `context+1552` is a monotonically increasing `int32` that tracks how far the compilation has progressed through the 159-phase pipeline. It is **not** a legalization-only counter -- it is incremented by phases across all categories (legalization, optimization, scheduling, regalloc). Each increment is performed by a small thunk function whose sole body is `*(ctx + 1552) = N`.

Known values and their associated phases:

| Value | Thunk Address | Phase / Context |
|-------|---------------|-----------------|
| 0 | (init) | `sub_7F7DC0` -- compilation context constructor |
| 1 | `sub_C5F620` | Early pipeline (before ConvertUnsupportedOps) |
| 2 | `sub_C5F5A0` | After ConvertUnsupportedOps (phase 5) |
| 3 | `sub_C5EF80` | After MidExpansion (phase 45) |
| 4 | `sub_C5EF30` | After OriDoRematEarly (phase 54) -- signals remat mode active |
| 5 | `sub_1233D70` | Mid-pipeline scheduling/ISel context |
| 7 | `sub_6612E0` / `sub_C60AA0` | After LateExpansion (phase 55) |
| 8 | `sub_849C60` | Post-optimization context |
| 9 | `sub_C5EB80` | After OriBackCopyPropagate (phase 83) |
| 10 | `sub_88E9D0` | Late optimization |
| 11 | `sub_C5EA80` | After SetAfterLegalization (phase 95) region |
| 12 | `sub_C5E980` | Post-legalization |
| 13 | `sub_13B5C80` | ISel/scheduling |
| 14 | `sub_C5E830` | Before PostSchedule worker (phase 106 `AdvancedPhasePostSched` Type-C thunk) |
| 15 | `sub_C5E7C0` | After OptimizeHotColdInLoop (phase 108) |
| 16 | `sub_C5E6E0` | Post-regalloc |
| 17 | `sub_C5E5A0` | Mercury/codegen |
| 18 | `sub_C5E4D0` | Post-Mercury |
| 19 | `sub_C5E440` | Late codegen |
| 20 | `sub_C5E390` | Before PostFixUp worker (phase 111 `AdvancedPhasePostFixUp` Type-C thunk) |
| 21 | `sub_C5E0B0` | Final pipeline stage |

Readers of downstream passes use `*(ctx+1552) > N` to gate behavior that should only run after a certain pipeline point. For example, the rematerialization cross-block pass checks `*(ctx+1552) > 4` to enable its second-pass mode.

### Knob Container Access Pattern

The knob container at `+1664` is accessed through a two-level virtual dispatch pattern that appears at 100+ call sites:

```c
// Fast path: known vtable -> direct array read
_QWORD *v2 = *(_QWORD **)(ctx + 1664);
bool (*query)(__int64, int) = *(bool (**)(...))(*v2 + 72);
if (query == sub_6614A0)
    result = *(u8*)(v2[9] + knob_index * 72 + offset) != 0;
else
    result = query((int64)v2, knob_index);  // slow path
```

The fast path reads directly from the knob value array at `v2[9]` (offset +72 of the knob state object), where each knob value occupies 72 bytes. The slow path invokes the virtual method for derived knob containers.

### Function Context (at +1880)

When a function is under compilation, `+1880` points to a large context object containing 17 pairs of analysis-result data structures. Each pair consists of a sorted container and a hash map, holding results such as live ranges, register maps, and scheduling data. The cleanup code in `sub_7FB6C0` destroys pairs at qword offsets `[102, 97, 92, 87, 82, 77, 72, 67, 62, 57, 52, 47, 42, 36, 31, 26, 21]` from the context base, then handles reference-counted objects at offsets `[10]` and `[2]`.

### Opcode Attribute Table (at +776 / +784)

The field at `context+776` holds a pointer to a **per-opcode attribute bitmap**, and `context+784` holds the owning allocator. Before this investigation, the ptxas wiki treated both fields as opaque "linked list slots" because they sit adjacent to the call-graph region and the ctor writes to them look like list sentinels. They are neither: they are the storage for a static ~355-entry flag table that every legalization, ISel, and scheduling phase consults to decide how to handle each Ori opcode.

**Allocation pattern**, reconstructed from `sub_7F7DC0_0x7f7dc0.c:904-934`:

```c
// Ctor line 904: get master allocator
void *alloc = *(void**)(ctx + 16);

// Ctor line 905: allocate a 1428-byte buffer through vtable[24] (= +24 / 8 = slot 3)
uint8_t *buf = alloc_vtable->allocate(alloc, 1428);

// Ctor line 906: the first 4 bytes of the buffer hold the value 355
// This is NOT an entry count -- the buffer is always 1428 bytes
// regardless of 355 -- it appears to be an opcode-class marker or
// table-version constant checked by the deallocator.
*(uint32_t*)buf = 355;

// Ctor lines 909-927: clear all payload bytes in a 4-qword stride loop
// `v143 = v140 + 178` (= buf + 1424 bytes), so the loop walks indices
// 8, 40, 72, ... 1400 (stride 32). Each iteration touches +0, +8, +16
// (bit-masking the third qword with `&= 0xF8u`, preserving bits 3-7).
// This means each logical entry is **32 bytes** and only the first
// 24 bytes are in the "clean zero" state after init; the fourth qword
// holds pre-set flag bits.

// Ctor lines 932-933: stash pointer and allocator (pointer skips the
// 8-byte header, so `opcode_attr_table[0]` is the bitmap for opcode 0)
*(void**)(ctx + 776) = buf + 8;
*(void**)(ctx + 784) = alloc;
```

**Per-opcode write pattern** (ctor lines 934-1229): the ctor issues hundreds of `table[byte_offset] |= mask` statements, one for each opcode's combination of attributes. A sampling:

| Byte offset | OR mask | Inferred attribute |
|-------------|---------|--------------------|
| [17], [25], [29] | 0x04, 0x04, 0x02 | Low-opcode (class 0-10) canary bits |
| [64], [65] | 0x0C, 0x10 | Memory-access opcode pair (load/store) |
| [1200], [1216], [1260], [1272] | 0x9C | Cluster-group marker (bit pattern shared by cluster-launch-related opcodes) |
| [1201], [1217], [1261], [1273] | 0x11 | Pairs with 0x9C: "has synchronizing behavior" |
| [85], [209], [237], [313], ... | 0x10 | "Requires late expansion" gate |
| [84], [232], [504], [1038] | 0x1E | "Requires late expansion + emits diagnostic" composite |
| [689], [741], [828], [1084] | 0x40 | "Cluster-barrier-aware" (bit 6) |
| [828], [1084], [1225] | 0x80 | "Serializing opcode" sign-bit |
| [1004], [1016], [1152] | 0x80 | Same sign-bit (mirrored to compute-focused opcodes) |

**Read pattern** (observed shape in callers, to be reverse-engineered per phase):

```c
// Gating pattern used by legalization phases:
uint8_t *attr_table = *(uint8_t**)(ctx + 776);
uint8_t flags = attr_table[ori_opcode];  // opcode is 8-bit index into table
if (flags & NEEDS_LATE_EXPANSION)
    schedule_late_expansion(insn);
```

Each opcode appears to occupy between 1 and 4 bytes in the table, with the exact stride determined by the opcode's class. Because the ctor writes span byte offsets up to 1426 (out of 1420 usable bytes after the 8-byte header) and 355 opcodes * 4 bytes = 1420 bytes, the **most likely layout is 4 attribute bytes per opcode** -- giving 32 bits of per-opcode flags, of which ~8-10 bits are actively used in the ctor.

**Deallocator path** (`sub_7F7DC0:928-930`): when the ctx is freed, the code recovers the original base via `v145 = *(_QWORD*)(a1+776); ... free(v145 - 8)` -- this is the evidence that +776 points 8 bytes past the allocation base. Without this offset correction, a naive `free(*(a1+776))` would corrupt the allocator's metadata.

The table should be treated as **partial**: each individual bit's meaning needs confirmation by cross-referencing a phase-execute body that actually tests it. The bit positions and inferred roles above come from the ctor init pattern alone.

## Ori Code Object (~1136 bytes)

The Code Object is the per-function container for all IR data. One instance exists for each function under compilation. Constructor is at `sub_A3B080`, vtable at `0x21EE238`.

### Constructor Analysis

The constructor (`sub_A3B080`) takes two arguments: `a1` (the Code Object to initialize) and `a2` (the compilation context). It:

1. Sets `+8 = a2` (back-pointer to compilation context)
2. Sets `+0 = &unk_21EE238` (vtable)
3. Zeroes approximately 250 distinct fields across the 1136-byte range
4. Loads two SSE constants from `xmmword_2027600` and `xmmword_21EFAE0` into offsets +96 and +112 (likely default register file descriptors or encoding parameters)
5. Reads `a2+1412` and `a2+1418` to set mode flags at `+1101` and `+1008`
6. Accesses the knob container at `a2+1664` to query knob 367 for initial configuration
7. Sets `+1008 = 0x300000050` (default) or `0x400000080` (if `a2+1418 & 4`)

### Code Object Field Map

| Offset | Type | Field | Evidence / Notes |
|--------|------|-------|------------------|
| +0 | `vtable*` | vtable | `0x21EE238`, 263+ virtual methods |
| +8 | `ptr` | `compilation_ctx` | Back-pointer to owning compilation context |
| +16 | `u128` | (zeroed) | SSE zero-store in constructor |
| +24 | `u32` | `sm_version` | Encoded SM target (12288=sm30, 20481=sm50, 36865=sm90) |
| +32 | `u128` | (zeroed) | SSE zero-store |
| +48 | `u128` | (zeroed) | SSE zero-store |
| +64 | `u32` | `init_flags` | Zeroed in constructor |
| +72 | `ptr` | `code_buf` | Output code buffer |
| +80 | `u128` | (zeroed) | |
| +88 | `ptr` | `reg_file` | Register descriptor array: `*(ctx+88) + 8*regId` |
| +96 | `u128` | `reg_defaults_1` | Loaded from `xmmword_2027600` |
| +99 | `u32` | `ur_count` | Uniform register (UR) count |
| +102 | `u32` | `r_alloc` | R-register allocated count |
| +112 | `u128` | `reg_defaults_2` | Loaded from `xmmword_21EFAE0` |
| +128--175 | `u128[3]` | (zeroed) | SSE zero-stores |
| +152 | `ptr` | `sym_table` | Symbol/constant lookup array |
| +159 | `u32` | `r_reserved` | R-register reserved count |
| +176 | `ptr` | (zeroed) | |
| +184 | `u32` | (zeroed) | |
| +192 | `ptr` | (zeroed) | |
| +200 | `u128` | (zeroed) | |
| +216 | `u128` | (zeroed) | |
| +232 | `u32` | (zeroed) | |
| +236 | `u32` | (zeroed) | |
| +240 | `ptr` | (zeroed) | |
| +248 | `u128` | (zeroed) | |
| +264 | `u128` | (zeroed) | |
| +272 | `ptr` | `instr_head` | Instruction linked-list head |
| +280 | `u32` | (zeroed) | |
| +288 | `ptr` | (zeroed) | |
| +296 | `ptr` | `bb_array` | `BasicBlock**` -- dense array of pointers to heap BB objects (8-byte stride). Indexed `*(ctx+296) + 8*bix` in `sub_781F80:339`, `sub_78B430:107`, `sub_1908D90:21`. |
| +304 | `u32` | `bb_index` | Current basic block count (iteration bound: `for (i=0; i<=ctx[+304]; i++)`) |
| +312 | `ptr` | `options` | `OptionsManager*` for knob queries |
| +320--359 | `u128[3]` | (zeroed) | |
| +335 | `u32` | `instr_hi` | Instruction count upper bound |
| +336 | `u32` | `tex_inst_count` | Texture instruction count (stats emitter) |
| +338 | `u32` | `fp16_vect_inst` | FP16 vectorized instruction count |
| +340 | `u32` | `inst_pairs` | Instruction pair count |
| +341 | `u32` | `instr_lo` | Instruction count lower bound |
| +342 | `u32` | `tepid_inst` | Tepid instruction count |
| +360 | `ptr` | (zeroed) | |
| +368 | `u32` | `sub_block_flags` | |
| +372 | `u32` | `instr_total` | Total instruction count (triggers chunked scheduling at > 0x3FFF) |
| +376 | `u32` | (zeroed) | |
| +384--416 | `ptr[5]` | (zeroed) | |
| +424 | `u32` | (zeroed) | |
| +432 | `ptr` | (zeroed) | |
| +440 | `u32` | (zeroed) | |
| +448 | `ptr` | (zeroed) | |
| +464 | `ptr` | (zeroed) | |
| +472 | `u8` | (zeroed) | |
| +473 | `u8` | (zeroed) | |
| +536 | `u32` | (zeroed) | |
| +540 | `u32` | (zeroed) | |
| +648 | `ptr` | `succ_map` | CFG successor edge hash table |
| +680 | `ptr` | `backedge_map` | CFG backedge hash table |
| +720 | `ptr` | `rpo_array` | Reverse post-order array (`int*`) |
| +728 | `ptr` | `bitmask_array` | Grow-on-demand bitmask array for scheduling |
| +740 | `u32` | `bitmask_capacity` | Capacity of bitmask array |
| +752 | `ptr` | (zeroed) | |
| +760 | `u32` | (zeroed) | |
| +764 | `u32` | (zeroed) | |
| +768 | `ptr` | `const_sections` | Constant memory section array |
| +772 | `u8` | (zeroed) | |
| +776 | `ptr` | `smem_sections` | Shared memory section array |
| +976 | `ptr` | `block_info` | Inline array of 40-byte scheduling-side block descriptors. Parallel to `bb_array` but distinct storage. Allocated/grown by `sub_10AE800` (40 * capacity bytes via vtable slot +24). |
| +984 | `i32` | `num_blocks` | High-water block index (iteration bound for the 40-byte array; stride = 40). |
| +988 | `i32` | `block_info_capacity` | Capacity of the 40-byte array (grown with `3/2` policy in `sub_10AE800:37`). |
| +996 | `u32` | `annotation_offset` | Current offset into annotation buffer (`sub_A4B8F0`) |
| +1000 | `ptr` | `annotation_buffer` | Annotation data buffer (`sub_A4B8F0`) |
| +1008 | `u64` | `encoding_params` | Default `0x300000050` or `0x400000080` |
| +1016 | `ptr` | (zeroed) | |
| +1024 | `u32` | (zeroed) | |
| +1032 | `ptr` | (zeroed) | |
| +1040 | `ptr` | (zeroed) | |
| +1064 | `ptr` | (zeroed) | |
| +1080 | `u128` | (zeroed) | |
| +1096 | `u32` | (zeroed) | |
| +1100 | `u8` | (zeroed) | |
| +1101 | `u8` | `optimization_mode` | Set from knob 367 and `compilation_ctx+1412` |
| +1102 | `u8` | (zeroed) | |
| +1104 | `ptr` | (zeroed) | |
| +1120 | `u128` | (zeroed) | |

### Register Count Formula

From the stats emitter at `sub_A3A7E0` and the register count function at `sub_A4B8F0` (which both use vtable+2104 dispatch with `sub_859FC0` as the fast path):

```c
total_R_regs      = code_obj[159] + code_obj[102]   // reserved + allocated
instruction_count = code_obj[335] - code_obj[341]   // upper - lower
```

### Stats Emitter Field Map

The stats emitter (`sub_A3A7E0`) accesses a per-function stats record through the SM backend: `v3 = *(compilation_ctx+8)[198]` (offset +1584 from the outer compilation context points to the SM backend object; the emitter then reads per-function stats fields within it). It uses DWORD indexing (4-byte), and reveals these additional fields:

| DWORD Index | Byte Offset | Field | Stat String |
|-------------|-------------|-------|-------------|
| 8 | +32 | `est_latency` | `[est latency = %d]` |
| 10 | +40 | `worst_case_lat` | `[worstcaseLat=%f]` |
| 11 | +44 | `avg_case_lat` | `[avgcaseLat=%f]` |
| 12 | +48 | `spill_bytes` | `[LSpillB=%d]` |
| 13 | +52 | `refill_bytes` | `[LRefillB=%d]` |
| 14 | +56 | `s_refill_bytes` | `[SRefillB=%d]` |
| 15 | +60 | `s_spill_bytes` | `[SSpillB=%d]` |
| 16 | +64 | `low_lmem_spill` | `[LowLmemSpillSize=%d]` |
| 17 | +68 | `frame_lmem_spill` | `[FrameLmemSpillSize=%d]` |
| 18 | +72 | `non_spill_bytes` | `[LNonSpillB=%d]` |
| 19 | +76 | `non_refill_bytes` | `[LNonRefillB=%d]` |
| 20 | +80 | `non_spill_size` | `[NonSpillSize=%d]` |
| 26 | +104 | `occupancy` (float) | `[Occupancy = %f]` |
| 27 | +108 | `div_branches` | `[est numDivergentBranches=%d]` |
| 28 | +112 | `attr_mem_usage` | `[attributeMemUsage=%d]` |
| 29 | +116 | `program_size` | `[programSize=%d]` |
| 42 | +168 | `precise_inst` | `[Precise inst=%d]` |
| 44 | +176 | `udp_inst` | `[UDP inst=%d]` |
| 45 | +180 | `vec_to_ur` | `[numVecToURConverts inst=%d]` |
| 49 | +196 | `max_live_suspend` | `[maxNumLiveValuesAtSuspend=%d]` |
| 87 | +348 | `partial_unroll` | `[partially unrolled loops=%d]` |
| 88 | +352 | `non_unrolled` | `[non-unrolled loops=%d]` |
| 89 | +356 | `cb_bound_tex` | `[CB-Bound Tex=%d]` |
| 90 | +360 | `partial_bound_tex` | `[Partially Bound Tex=%d]` |
| 91 | +364 | `bindless_tex` | `[Bindless Tex=%d]` |
| 92 | +368 | `ur_bound_tex` | `[UR-Bound Tex=%d]` |
| 93 | +372 | `sm_version_check` | `> 24575` triggers UR reporting |
| 99 | +396 | `ur_count_stats` | `[urregs=%d]` |
| 102 | +408 | `r_alloc` | R-register allocated count |
| 159 | +636 | `r_reserved` | R-register reserved count |
| 303 | +1212 | `est_fp` | `[est fp=%d]` |
| 306 | +1224 | `est_half` | `[est half=%d]` |
| 307 | +1228 | `est_transcendental` | `[est trancedental=%d]` |
| 308 | +1232 | `est_ipa` | `[est ipa=%d]` |
| 310 | +1240 | `est_shared` | `[est shared=%d]` |
| 311 | +1244 | `est_control_flow` | `[est controlFlow=%d]` |
| 315 | +1260 | `est_load_store` | `[est loadStore=%d]` |
| 316 | +1264 | `est_tex` | `[est tex=%d]` |
| 334 | +1336 | `inst_pairs` | `[instPairs=%d]` |
| 335 | +1340 | `instr_hi` | Instruction count upper bound |
| 336 | +1344 | `tex_inst_count` | `[texInst=%d]` |
| 337 | +1348 | `fp16_inst` | `[FP16 inst=%d]` |
| 338 | +1352 | `fp16_vect_inst` | `[FP16 VectInst=%d]` |
| 339 | +1356 | `inst_hint` | `[instHint=%d]` |
| 340 | +1360 | `inst_pairs_2` | checked for non-zero to print instHint line |
| 341 | +1364 | `instr_lo` | Instruction count lower bound |
| 342 | +1368 | `tepid_inst` | `[tepid=%d]` |

Note: The stats emitter accesses the Code Object through a float pointer (`v3`), so DWORD indices map to byte offsets via `index * 4` for integers and `index * 4` for floats. Float fields at indices 9, 26, 50, 54, 57, 58, 59, 61, 62, 65, 84, 85, 86 hold throughput and occupancy metrics. A linked list at qword index 55 (byte +440) holds additional string annotations.

## Basic Block Representation (two parallel structures)

ptxas uses **two separate basic-block containers** that coexist in the Code Object, and an **earlier draft of this wiki conflated them into a single "40-byte BasicBlock" struct**. The conflation is the source of apparent contradictions between this page and the per-pass documentation (which accesses offsets like `bb+128`, `bb+144`, `bb+152`, `bb+232`, `bb+280`, `bb+292` -- all far beyond 40 bytes). The reality is:

1. **`bb_array` at Code Object +296** -- a dense `BasicBlock**` table (8-byte stride), i.e. *one pointer per block* to a **heap-allocated full BasicBlock object (≥293 bytes)**. Used by every optimization pass that needs CFG structure (predecessors, successors, RPO, flags, loop attributes).
2. **`block_info` at Code Object +976** -- an *inline contiguous array* of **40-byte scheduling descriptors** (40-byte stride). Each 40-byte entry is the scheduling / DOT-dumper view of a block and is *not* a BasicBlock -- it carries an instruction-range bracket (head / tail-sentinel), the block index, and a flag byte.

The two structures are parallel: index `i` in `bb_array` and index `i` in `block_info` describe the same logical block. Count-wise, `bb_array[0..ctx[+304]]` is the iteration range (inclusive upper bound), and `block_info[0..ctx[+984]]` is the iteration range for the 40-byte array (also inclusive). The two counts are set independently but remain in lock-step because the creation paths update both.

### The 40-byte `block_info` entry (at +976)

This is the only structure in ptxas that is *actually* 40 bytes wide. It is allocated by `sub_10AE800` (the block-info appender), which grows the array with `capacity_new = max(old*3/2, old+2)` and copies `40 * count` bytes on reallocation. From `sub_10AE800:61`:

```c
// sub_10AE800 -- block_info appender
result = (__m128i *)&base[40 * new_count];   // new entry address
*result               = xmm_a7;              // +0..+15  (16 bytes, __int128 arg a7)
result[1]             = xmm_a8;              // +16..+31 (16 bytes, __int128 arg a8)
result[2].m128i_i64[0]= scalar_a9;           // +32..+39 ( 8 bytes, __int64   arg a9)
// Grow path (same function, lines 37-55):
//   v13 = max(cap + (cap+1)/2, count + 2)
//   memcpy(new_buf, old_buf, 40 * old_count);   // i.e. 8 * (5*count + 5)
```

Field interpretation, cross-checked against the three primary consumers:

| Offset | Width | Field | Evidence |
|--------|-------|-------|----------|
| +0  | `ptr` | `insn_head` -- first instruction of the block (scheduling view) | `sub_1C348B0:129` reads `v80 = *v79` then iterates instructions until reaching `v79[1]`; `sub_BE21D0:41` reads `*(_QWORD*)v11` as a pointer and fetches a `_DWORD` at `*v11+152` for the DOT label. |
| +8  | `ptr` | `insn_tail_sentinel` -- marks end of the scheduling instruction range | `sub_1C348B0:130` loads `v79[1]` as the walk terminator; `sub_6FC810:728` writes `v37[+8] = v12` immediately after `sub_10AE800` returns the new entry. |
| +16 | `u64` | `reserved_a` -- written by `sub_10AE800` from `a8.lo` but no consumer has been identified; zero in the common path. | |
| +20 | `i32` | `reserved_b` -- zeroed immediately after append (`sub_6FC810:727`: `*(_DWORD*)(v37+20) = 0`). | |
| +24 | `i32` | scheduling scratch -- `sub_6FC810:726` writes `0`; the scheduling / regalloc pipeline later stashes per-block scratch state here. | |
| +28 | `i32` | `bix` -- block index, the same unique ID used in all CFG hash tables | `sub_BE21D0:39`: `v12 = v11[7]` (DWORD index 7 = byte +28) then `printf("bix%u", v12)` in the DOT dumper. |
| +32 | `u8`  | `flags` -- bit 1 (`0x02`) = "block ends in branch-with-side-effect 1506 opcode" | `sub_BE0690:1467`: `*(_BYTE*)(v126+32) \|= 2u`. `sub_8A5240:62`: `if ((*(_BYTE*)(result+32) & 2) == 0)` gates backedge-map insertion. |
| +33 | `u8`[7] | padding / future-use bytes up to the 40-byte stride | |

Size proof: the appender writes exactly `40 * n` bytes, the DOT dumper advances its cursor by literally `v9 += 40` per iteration (`sub_BE21D0:38`), the last-element helper `sub_10AE8E0` computes `base + 40 * num_blocks`, and the grow-path `memcpy` copies `8 * (5*count + 5)` = `40 * (count+1)` bytes. Every independent site agrees on stride 40.

```text
40-byte block_info entry layout

  +0   ptr  insn_head               // scheduling-range first instruction
  +8   ptr  insn_tail_sentinel      // scheduling-range terminator
  +16  u64  reserved_a
  +24  i32  scheduling_scratch
  +28  i32  bix                     // unique block index
  +32  u8   flags                   // bit 0x02 set by sub_BE0690 on side-effect terminators
  +33  u8[7] padding
```

Allocation pseudocode:

```c
function appendBlockInfo(ctx, insn_head, insn_tail, bix, flags):
    // Grow inline array if needed (sub_10AE800)
    count = ctx[+984]
    cap   = ctx[+988]
    if count + 2 > cap:
        new_cap = max(cap + (cap + 1) / 2, count + 2)
        new_buf = arena_alloc(40 * new_cap)       // via code-object allocator vtable+24
        memcpy(new_buf, ctx[+976], 40 * count)
        arena_free(ctx[+976])
        ctx[+976] = new_buf
        ctx[+988] = new_cap

    // Write the new entry
    ctx[+984] = count + 1                         // new high-water index
    entry     = ctx[+976] + 40 * (count + 1)
    entry[+0] = insn_head
    entry[+8] = insn_tail
    entry[+24]= 0
    entry[+28]= bix
    entry[+32]= flags                             // usually 0 at creation
    return entry
```

### The heap BasicBlock object (at `*(ctx+296)[bix]`)

The entries of `bb_array` point to a much larger heap object. The size has not been pinned down to a single allocator call (it is not created by the 136-byte scratch routine `sub_62BB00`, whose buffer is `sub_4248B0`'d back to the arena on the normal exit at line 551), but the *minimum* size is bounded from below by the field accesses performed by the CFG / liveness / loop passes. The highest confirmed offsets all come from `sub_781F80` (`BasicBlockAnalysis`) through the verified `bb_array[]` indirection `*(_QWORD*)(*(_QWORD*)(a1+296) + 8*bix)`:

| Offset | Width | Pass access | Field (from CFG / liveness docs) |
|--------|-------|-------------|----------------------------------|
| +8   | `ptr`   | `sub_78B430:110` (`**(_QWORD**)(v13+8)+72` -- first-instr opcode) | instruction list head |
| +120 | `u32`   | `sub_781F80:342` (`= 0`) | scheduling-state scratch dword |
| +128 | `u128`  | `sub_781F80:344` (`*(_OWORD*)(v16+128) = 0`) | successor list head + aux qword |
| +136 | `ptr`   | `sub_78B430:112` (`*(__int64***)(v13+136)` -- walk preds) | predecessor list head |
| +144 | `u128`  | `sub_781F80:343` (`*(_OWORD*)(v16+144) = 0`), `sub_78B430:107` (`rpo_number`) | RPO number + adjacent metadata (16 bytes) |
| +152 | `i32`   | `sub_781F80:770` (`*(v20+152) = v163` where `v163 = pred->rpo_number`) | loop-exit RPO marker / label id (dual-purpose; BBAnalysis overwrites during the pass) |
| +216 | `i32`   | `sub_781F80:731` (`v102 = *(int*)(v21+216)`) | operand-side scratch (only reached via `ctx+368` not `ctx+296`, so this may belong to a different struct; flagged here for completeness) |
| +232 | `i32`   | `sub_781F80:341` (`= 0`) | per-BB zeroed dword |
| +280 | `i32`   | `sub_781F80:340,536,540,553,560,603,906,925,1134`, `sub_781F80:1264`, `sub_78B430:*` | **primary BB flags dword** (bits: `0x10` loop header, `0x20` has predecessor, `0x800000` in-loop, `0x20000` / `0x40000` / `0x40000000` analysis bits) |
| +282 | `u8`    | `sub_781F80:908` (`(*(_BYTE*)(v20+282) & 8) != 0`) | high byte of the `+280` flags dword (byte-level test) |
| +292 | `u8`    | `sub_781F80:602,733,904` (bitwise OR / AND) | secondary flag byte (paired with `+280`) |

The access at offset `+292` (a byte, written with `|= 8`) sets the lower bound on the BasicBlock size at **≥ 293 bytes**, and the natural alignment of the arena allocator rounds this up to a multiple of 8 (so the next valid allocator bucket is 296 bytes). **The earlier "BasicBlock = 40 bytes" claim is wrong** and was the result of describing the 40-byte `block_info` entry as if it were the full block object.

The previous revision of this section also misattributed the scheduling-pass initializer `sub_8D0640` to the 40-byte array. That was wrong: `sub_8D0640` walks a *separate* linked list rooted at `scheduling_ctx[+104]` (`for (i = *(v21+104); i; i = (__int64*)*i)`), with the zeroing pattern `i[7] = 0`, `i[13] = 0`, `*((_DWORD*)i+19) = 0`, `*((_DWORD*)i+21) = -1`. This linked list stores *per-scheduling-group* records (qword fields at +56, +104; dword fields at +76, +84), not block_info entries. The 40-byte entries are never rewritten in a single pass like that -- they are populated incrementally during CFG construction via `sub_10AE800` and mutated in-place by `sub_BE0690` / `sub_8A5240` when backedge analysis needs to mark a terminator.

### Access cheat sheet

```c
// Iterate every block (optimization / CFG passes)
int bb_count = *(int*)(ctx + 304);                         // inclusive upper bound
for (int i = 0; i <= bb_count; i++) {
    BasicBlock* bb = *(BasicBlock**)(*(ctx + 296) + 8*i);  // 8-byte stride, pointer table
    int rpo  = *(int*)(bb + 144);                          // rpo_number
    int flag = *(int*)(bb + 280);                          // primary flags dword
    ...
}

// Iterate every block (scheduling / DOT dumper)
int n = *(int*)(ctx + 984);                                // inclusive upper bound
char* base = *(char**)(ctx + 976);
for (int i = 0; i <= n; i++) {
    char* entry = base + 40*i;                             // 40-byte stride, inline
    int bix = *(int*)(entry + 28);
    void* insn_head = *(void**)(entry + 0);
    ...
}
```

## Instruction Layout

Instructions are polymorphic C++ objects linked into per-BB doubly-linked lists. The instruction format is detailed in [Instructions](./instructions.md); this section covers only the structural linkage.

Each instruction carries a unique integer ID at `+16`, an opcode at `+72` (the peephole optimizer masks with `& 0xCF` on byte 1 to strip modifier bits), and a packed operand array starting at `+84`. The operand count is at `+80`. Operands are 8 bytes each.

### Packed Operand Format

```text
 31  30  29  28  27       24  23  22  21  20  19                  0
+---+---+---+---+-----------+---+---+---+---+---------------------+
|     type      |  modifier bits (8 bits)    |  index (20 bits)    |
+---+---+---+---+-----------+---+---+---+---+---------------------+

Extraction (50+ confirmed sites):
  uint32_t operand = *(uint32_t*)(instr + 84 + 8 * i);
  int type    = (operand >> 28) & 7;     // bits 28-30
  int index   = operand & 0xFFFFF;       // bits 0-19
  int mods    = (operand >> 20) & 0xFF;  // bits 20-27
```

| Type Value | Meaning | Resolution |
|------------|---------|------------|
| 1 | Register operand | Index into `*(code_obj+88)` register file |
| 5 | Symbol/constant operand | Index into `*(code_obj+152)` symbol table |

The operand classifier functions at `0xB28E00`--`0xB28E90` provide predicate checks:

| Function | Predicate |
|----------|-----------|
| `sub_B28E00` | `getRegClass` (1023 = wildcard, 1 = GPR) |
| `sub_B28E10` | `isRegOperand` |
| `sub_B28E20` | `isPredOperand` |
| `sub_B28E40` | `isImmOperand` |
| `sub_B28E80` | `isConstOperand` |
| `sub_B28E90` | `isUReg` |

## Symbol Table

The symbol table is accessed through Code Object `+152`. Based on the symbol table builder at `sub_621480` (21KB, references `a1+30016` for the symbol table base), symbols are stored in a hash-map-backed structure where each symbol has a name and associated properties (address, type, section binding).

### Internal Symbol Names

The following internal symbol names appear in decompiled code, indicating the kinds of entities tracked:

| Symbol | Purpose |
|--------|---------|
| `__ocg_const` | OCG-generated constant data |
| `__shared_scratch` | Shared memory scratch space |
| `__funcAddrTab_g` | Global indirect function call table |
| `__funcAddrTab_c` | Constant indirect function call table |
| `_global_ptr_%s` | Global pointer for named variable |
| `$funcID$name` | Function-local relocation symbol |
| `__cuda_dummy_entry__` | Dummy entry generated by `--compile-only` |
| `__cuda_sanitizer` | CUDA sanitizer instrumentation symbol |

### Symbol Resolution Flow

Symbol resolution (`sub_625800`, 27KB) traverses the symbol table to resolve references during the PTX-to-Ori lowering and subsequent optimization phases. The format `%s[%d]` (from `sub_6200A0`) is used for array-subscripted symbol references, and `__$endLabel$__%s` markers delimit function boundaries.

## Constant Buffer Layout

Constant memory is organized into banks (c[0], c[1], ...) corresponding to the CUDA `.nv.constant0`, `.nv.constant2`, etc. ELF sections. The constant section array at Code Object `+768` tracks all constant banks for the current function.

### Constant Bank Handling

The constant bank handler at `sub_6BC560` (4.9KB) manages references to constant memory using the `c[%d]` (integer bank) and `c[%s]` (named bank, `sw-compiler-bank`) notation. It enforces:

- A maximum constant register count (error: "Constant register limit exceeded; more than %d constant registers")
- LDC (Load Constant) requires a constant or immediate bank number

### ELF Constant Symbols

The ELF symbol emitter (`sub_7FD6C0`) creates symbols for constant bank metadata:

| Symbol Name | Purpose |
|-------------|---------|
| `.nv.ptx.const0.size` | Size of constant bank 0 (kernel parameters) |

The constant emission function (`sub_7D14C0`, 5.6KB) iterates the constant section array and copies bank data into the output ELF sections.

## Shared Memory Layout

Shared memory (`.nv.shared`) allocations are tracked through the shared memory section array at Code Object `+776`. Reserved shared memory regions are managed by `sub_6294E0` (12.1KB) and `sub_629E40` (6.1KB).

### Reserved Shared Memory Symbols

The ELF emitter recognizes these special symbols for shared memory layout:

| Symbol Name | Purpose |
|-------------|---------|
| `.nv.reservedSmem.begin` | Start of reserved shared memory region |
| `.nv.reservedSmem.cap` | Capacity of reserved shared memory |
| `.nv.reservedSmem.end` | End of reserved shared memory region |
| `.nv.reservedSmem.offset0` | First reserved offset within shared memory |
| `.nv.reservedSmem.offset1` | Second reserved offset within shared memory |

The `--disable-smem-reservation` CLI option disables the reservation mechanism. Shared memory intrinsic lowering (`sub_6C4DA0`, 15KB) validates that shared memory operations use types `{b32, b64}`.

### Descriptor Size Symbols

Additional ELF symbols track texture/surface descriptor sizes in shared memory:

| Symbol Name | Purpose |
|-------------|---------|
| `.nv.unified.texrefDescSize` | Unified texture reference descriptor size |
| `.nv.independent.texrefDescSize` | Independent texture reference descriptor size |
| `.nv.independent.samplerrefDescSize` | Independent sampler reference descriptor size |
| `.nv.surfrefDescSize` | Surface reference descriptor size |

## Pool Allocator

The pool allocator (`sub_424070`, 3,809 callers) is the single most heavily used allocation function. Every dynamic data structure in ptxas is allocated through pools.

### Pool Object Layout

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| +0 | `ptr` | `large_block_list` | Singly-linked list of large (>4999 byte) blocks |
| +32 | `u32` | `min_slab_size` | Minimum slab allocation size |
| +44 | `u32` | `slab_count` | Number of slabs allocated |
| +48 | `ptr` | `large_free_list` | Free list for large blocks (boundary-tag managed) |
| +56 | `u32` | `fragmentation_count` | Fragmentation counter (decremented on split) |
| +60 | `u32` | `max_order` | Maximum power-of-2 order for large blocks |
| +64 | ... | (large block free lists) | `a1 + 32*(order+2)` = per-order free list head |
| +2112 | `ptr` | `tracking_map` | Hash map for allocation metadata tracking |
| +2128 | `ptr[N]` | `small_free_lists` | Size-binned free lists: `*(pool + 8*(size>>3) + 2128)` = head |
| +7128 | `mutex*` | `pool_mutex` | `pthread_mutex_t*` for thread safety |

### Allocation Paths

**Small path** (size <= 4999 bytes = `0x1387`):
1. Round size up to 8-byte alignment: `aligned = (size + 7) & ~7`
2. Minimum 16 bytes
3. Compute bin: `bin = pool + 8 * (aligned >> 3) + 2128`
4. If bin has a free block: pop from free list, decrement slab available bytes
5. If bin is empty: allocate a new slab from the parent (size = `aligned * ceil(min_slab_size / aligned)`), carve into free-list nodes

**Large path** (size > 4999 bytes):
1. Add 32 bytes for boundary tags
2. Search power-of-2 order free lists starting from `log2(size+32)`
3. If found: split block if remainder > 39 bytes, return payload
4. If not found: call `sub_423B60` to grow the pool, allocate new slab from parent

### Boundary Tag Format (Large Blocks)

Large blocks use boundary tags for coalescing on free:

```text
Block Header (32 bytes):
  +0    i64      sentinel      // -1 = allocated, else -> next free
  +8    ptr      prev_free     // previous in free list (or 0)
  +16   u64      tag_offset    // 32 (header size)
  +24   u64      payload_size  // user-requested allocation size

Block Footer (32 bytes at end):
  +0    i64      sentinel
  +8    ptr      prev_free
  +16   u64      footer_tag    // 32
  +24   u64      block_size    // total size including headers
```

### Slab Descriptor (56 bytes)

Each slab is tracked by a 56-byte descriptor:

| Offset | Type | Field | Description |
|--------|------|-------|-------------|
| +0 | `ptr` | `chain_link` | Link to next slab descriptor |
| +8 | `u64` | `total_size` | Total bytes in this slab |
| +16 | `u64` | `available_size` | Bytes remaining (decremented on alloc) |
| +24 | `ptr` | `owning_pool` | Back-pointer to pool |
| +32 | `ptr` | `memory_base` | Base address of slab memory |
| +40 | `u8` | `is_small_slab` | 1 = small-alloc slab, 0 = large-alloc slab |
| +44 | `u32` | `slab_id` | Global slab sequence number (atomic counter) |
| +48 | `u32` | `bin_size` | Size class for small slabs |

### Hierarchical Pools

Pools are hierarchical. When `sub_424070` is called with `a1 = NULL`, it falls back to a global allocator (`sub_427A10`) that uses `malloc` directly. Non-null `a1` values are pool objects that allocate from their own slabs, which are themselves allocated from a parent pool (the TLS context at offset +24 holds the per-thread pool pointer). The top-level pool is named `"Top level ptxas memory pool"` and is created in the compilation driver.

## Hash Map

The hash map (`sub_426150` insert / `sub_426D60` lookup, 2,800+ and 422+ callers respectively) is the primary associative container in ptxas.

### Hash Map Object Layout (~112 bytes)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| +0 | `fptr` | `hash_func` | Custom hash function pointer |
| +8 | `fptr` | `compare_func` | Custom compare function pointer |
| +16 | `fptr` | `hash_func_2` | Secondary hash (or NULL) |
| +24 | `fptr` | `compare_func_2` | Secondary compare (or NULL) |
| +32 | `u32` | `has_custom_compare` | Flag |
| +40 | `u64` | `bucket_mask` | `capacity - 1` for power-of-2 masking |
| +48 | `u64` | `entry_count` | Number of stored entries |
| +64 | `u64` | `load_factor_threshold` | Resize when `entry_count` exceeds this |
| +72 | `u32` | `first_free_slot` | Tracking for bitmap-based slot allocation |
| +76 | `u32` | `entries_capacity` | Capacity of entries array |
| +80 | `u32` | `bitmap_capacity` | Capacity of used-bits bitmap |
| +84 | `u32` | `flags` | Hash mode in bits 4-7 |
| +88 | `ptr` | `entries` | Array of 16-byte `{key, value}` pairs |
| +96 | `ptr` | `used_bitmap` | Bitmap tracking occupied slots |
| +104 | `ptr` | `buckets` | Array of pointers to chained index lists |

### Hash Modes

The hash mode is encoded in bits 4-7 of the flags field at offset +84:

| Mode | Flag Bits | Hash Function | Use Case |
|------|-----------|---------------|----------|
| 0 | `0x00` | Custom (`+0` function pointer) | User-defined hash/compare |
| 1 | `0x10` | Pointer hash: `(key>>11) ^ (key>>8) ^ (key>>5)` | Pointer-keyed maps |
| 2 | `0x20` | Identity: key used directly | Integer-keyed maps |

Mode selection happens automatically in the constructor (`sub_425CA0`): if the hash/compare pair matches `(sub_427750, sub_427760)`, mode 2 is set; if `(sub_4277F0, sub_427810)`, mode 1.

### Lookup Algorithm

```c
// Mode 1 (pointer hash) example:
uint64_t hash = (key >> 11) ^ (key >> 8) ^ (key >> 5);
uint64_t bucket_idx = hash & map->bucket_mask;
int32_t* chain = map->buckets[bucket_idx];
while (*++chain != -1) {
    entry_t* e = map->entries + 16 * (*chain);
    if (key == e->key)
        return e->value;  // found
}
return 0;  // not found
```

Growth policy: the map doubles capacity and rehashes when `entry_count > load_factor_threshold`.

### String-Keyed Maps

String-keyed maps use MurmurHash3 (`sub_427630`, 73 callers) as the hash function. The implementation uses the standard MurmurHash3_x86_32 constants:

| Constant | Value | Standard Name |
|----------|-------|---------------|
| c1 | `0xCC9E2D51` (-862048943) | MurmurHash3 c1 |
| c2 | `0x1B873593` (461845907) | MurmurHash3 c2 |
| fmix1 | `0x85EBCA6B` (-2048144789) | MurmurHash3 fmix |
| fmix2 | `0xC2B2AE35` (-1028477387) | MurmurHash3 fmix |

## CFG Hash Map (FNV-1a)

The control flow graph uses a separate hash map implementation based on FNV-1a hashing, distinct from the general-purpose hash map above. Two instances exist per Code Object at offsets +648 (successor edges) and +680 (backedge info).

| Parameter | Value |
|-----------|-------|
| Initial hash | `0x811C9DC5` (-2128831035) |
| Prime | `16777619` (`0x01000193`) |
| Input | 4-byte block index, hashed byte-by-byte |

Bucket entry: 24 bytes `{head, tail, count}`. Node: 64 bytes with chain link, key, values, sub-hash data, and cached hash. See [CFG](./cfg.md) for the full CFG hash map specification.

## Linked List

The linked list (`sub_42CA60` prepend, 298 callers; `sub_42CC30` length, 48 callers) is a singly-linked list of 16-byte nodes:

```text
ListNode (16 bytes, pool-allocated)
  +0    ptr      next        // pointer to next node (NULL = end)
  +8    ptr      data        // pointer to payload object
```

Prepend allocates a 16-byte node from the pool, sets `node->data = payload`, and links it at the list head. This is used for function lists, relocation lists, annotation chains, and many intermediate pass-local collections.

## Growable Array (Pool Vector)

Growable arrays appear throughout the PhaseManager and elsewhere. The layout is a triple of `{data_ptr, count, capacity}`:

```text
PoolVector (24 bytes inline, or embedded in parent struct)
  +0    ptr      data         // pointer to element array
  +8    i32      count        // current element count
  +12   i32      capacity     // allocated capacity
```

Growth strategy (confirmed in the PhaseManager timing records): `new_capacity = max(old + old/2 + 1, requested)` (1.5x growth factor). Elements are typically 8 bytes (pointers) or 16 bytes (pointer pairs). Reallocation uses `sub_424C50` (pool realloc, 27 callers).

The PhaseManager uses this pattern for the phase list (16-byte `{phase_ptr, pool_ptr}` pairs), the name table (8-byte string pointers), and the timing records (32-byte entries).

## Knob Value Array

Knob values are stored in a contiguous array of 72-byte slots, accessed at `knob_state[9] + 72 * knob_index` (where `knob_state[9]` is offset +72 of the knob state object).

### Knob Value Slot (72 bytes)

| Offset | Type | Field |
|--------|------|-------|
| +0 | `u8` | Type tag (0=unset, 1=bool, 2=int, ..., 12=opcode list) |
| +8 | `i64` | Integer value / pointer to string / linked list head |
| +16 | `i64` | Secondary value (range max, list count, etc.) |
| +24 | `i64` | Tertiary value |
| +64 | `ptr` | Allocator reference |

Supported types:

| Type | Tag | Storage |
|------|-----|---------|
| Boolean | 1 | Flag at +0 |
| Integer | 2 | Value at +8 |
| Integer+extra | 3 | Value at +8, extra at +12 |
| Integer range | 4 | Min at +8, max at +16 |
| Integer list | 5 | Growable array of ints |
| Float | 6 | `float` at +8 |
| Double | 7 | `double` at +8 |
| String | 8/11 | Pointer at +8 |
| When-string | 9 | Linked list of 24-byte condition+value nodes |
| Value-pair list | 10 | Opcode:integer pairs via vtable |
| Opcode list | 12 | Opcode names resolved through vtable |

### Knob Descriptor (64 bytes)

Knob descriptors are stored in a table at `knob_state+16`, with count at `knob_state+24`:

| Offset | Type | Field |
|--------|------|-------|
| +0 | `ptr` | Primary name (ROT13-encoded) |
| +8 | `u64` | Primary name length |
| +16 | `u32` | Type tag |
| +24 | ... | (reserved) |
| +40 | `ptr` | Alias name (ROT13-encoded) |
| +48 | `u64` | Alias name length |

## Stream Object

The output stream used for diagnostics and stats reporting (e.g., at compilation context +1440) is a C++ iostream-like object with operator overloads. Field layout (from `sub_7FE5D0` and `sub_7FECA0`):

| Offset | Type | Field |
|--------|------|-------|
| +0 | `vtable*` | vtable (dispatch for actual I/O) |
| +8 | `u32` | `width` |
| +12 | `u32` | `precision` |
| +16 | `u64` | `char_count` |
| +24 | `ptr` | `format_buffer` |
| +56 | `u32` | `flags` (bit 0=hex, bit 1=oct, bit 2=left-align, bit 3=uppercase, bits 7-8=sign) |

## ORI Record Serializer (`sub_A50650`)

The ORI Record Serializer (`sub_A50650`, 74 KB, 2,728 decompiled lines) is the central function that takes a Code Object's in-memory state and flattens it into a linear output buffer organized as a table of typed section records. It is the serialization backbone for both the DUMPIR diagnostic subsystem and the compilation output path. Despite the `_ORI_` string it contains, it is not an optimization pass -- it is infrastructure.

| | |
|---|---|
| **Address** | `0xA50650` |
| **Size** | ~74 KB |
| **Identity** | `CodeObject::EmitRecords` |
| **Confidence** | 0.90 |
| **Called from** | `sub_A53840` (wrapper), `sub_AACBF0` / `sub_AAD2A0` (DUMPIR diagnostic path) |
| **Calls** | `sub_A4BC60` (register serializer, new format), `sub_A4D3F0` (legacy format), `sub_A4B8F0` (register count annotation), `sub_A47330` + `sub_A474F0` (multi-section finalization), `sub_1730890` / `sub_17308C0` / `sub_17309A0` (scheduling serializers), `sub_1730FE0` (register file map) |

### Parameters

`a1` is a serialization state object ("OriRecordContext") that carries the section table, compilation context back-pointer, and per-subsection index/size pairs. `a2` is the output buffer write cursor, advanced as data is emitted.

Key fields on `a1`:

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +8 | `ptr` | `compilation_ctx` | Dereferenced to reach sm_backend at `+1584` |
| +24 | `i32` | `header_section_idx` | `v5 + 32 * (*(a1+24) + 1)` |
| +72 | `ptr` | `section_table` | Array of 32-byte section entries |
| +180 | `u32` | `instr_counter_1` | Reset to 0 at entry |
| +472 | `u8` | `has_debug_info` | Gates debug section emission |
| +916 | `i32` | `multi_section_count` | `> 0` triggers link-record emission and tail call to `sub_A47330` |
| +1102 | `u8` | `multi_section_enabled` | Master flag for multi-section mode |
| +1120 | `ptr` | `scheduling_ctx` | Scheduling context for barrier/scope serialization |

### Section Record Format

Each section occupies a 32-byte entry in the table at `*(a1+72) + 32 * section_index`:

```text
Offset  Type   Field
+0      u16    type_tag           section type identifier
+4      u32    data_size          byte size of data payload
+8      ptr    data_ptr           pointer to data in output buffer
+16     u32    element_count      number of elements (or auxiliary metadata)
+20     u32    aux_field          additional per-type context
+24     u32    aux_field_2        secondary per-type context
```

Data payloads are 16-byte aligned: `cursor += (size + 15) & ~0xF`.

### Section Type Tag Catalog

The serializer emits up to 56 unique section types across three tag ranges.

**Base types (0x01--0x58):**

| Tag | Hex | Content | Evidence |
|-----|-----|---------|----------|
| 1 | 0x01 | Instruction stream (register-allocated code body) | Emitted via `sub_A4BC60` or `sub_A4D3F0` |
| 3 | 0x03 | Virtual-dispatch section (vtable+48 on state obj) | Conditional on `*(a1+64) > 0` |
| 16 | 0x10 | Source operand bank (v7[199] entries at v7+97) | `*(entry+48) = v7[199]` |
| 17 | 0x11 | Destination operand bank (bit-packed from v7+203) | Conditional on `!v7[1414]` |
| 19 | 0x13 | Annotation stream | `*(a1+232)` counter |
| 34 | 0x22 | Original-definition name table (`_ORI_` prefixed) | `strcpy(v50, "_ORI_")` at line 1762 |
| 35 | 0x23 | Instruction info snapshot (340 bytes from v7+4) | `qmemcpy` of 340 bytes |
| 46 | 0x2E | Texture/surface binding table | `v7[248]` entries, 16 bytes each |
| 50 | 0x32 | Live range interval table (spill map) | From compilation context +984 |
| 51 | 0x33 | Register file occupancy table | `*(ctx+1424) & 4` |
| 53 | 0x35 | Source operand type bitmap (4-bit per operand) | v7[131] operands, 20-byte stride |
| 54 | 0x36 | Destination operand type bitmap | v7[134] operands, 20-byte stride |
| 55 | 0x37 | Scheduling barrier data | via `sub_1730890` |
| 56 | 0x38 | Register file mapping | via `sub_1730FE0` |
| 58 | 0x3A | Scheduling dependency graph | via `sub_17309A0` |
| 59 | 0x3B | Multi-section link record | Conditional on `*(a1+1102)` |
| 64 | 0x40 | External reference (from ctx+2120) | Pointer stored, no data copy |
| 68 | 0x44 | Performance counter section | `*(a1+932)` counter |
| 70 | 0x46 | Spill/fill metadata | `v7[408]` |
| 71 | 0x47 | Call graph edge table | From v7+61, linked list traversal |
| 73 | 0x49 | Codegen context snapshot | From ctx+932 register allocation state |
| 80 | 0x50 | Hash table section | v7+207/208, hash bucket traversal |
| 81 | 0x51 | Extended call info | From v7+84 |
| 83 | 0x53 | Convergence scope data | via `sub_17308C0` |
| 85 | 0x55 | Register geometry record (banks, warps, lanes) | From ctx+1600, writes bank/warp/lane counts |
| 88 | 0x58 | Extended scheduling annotations | Conditional on `*(a1+1088) > 0` |

**Extended types (0x1208--0x1221):** Emitted only when `*(char*)(ctx+1412) < 0`, which enables the full post-register-allocation diagnostic mode. These 16 types carry per-register-class live range and operand definition data:

| Tag | Hex | Content |
|-----|-----|---------|
| 4616 | 0x1208 | Extended operand class 0 |
| 4617--4623 | 0x1209--0x120F | Extended operand classes 1--7 |
| 4624 | 0x1210 | Block-level operand summary |
| 4625 | 0x1211 | Live-in vector (12 bytes/element, count at `*(a1+668)`) |
| 4626 | 0x1212 | Live-out vector (12 bytes/element) |
| 4627 | 0x1213 | Extended operand class 8 |
| 4628--4629 | 0x1214--0x1215 | Extended operand classes 9--10 |
| 4630 | 0x1216 | Memory space descriptor (SM arch > 0x4FFF) |
| 4631 | 0x1217 | Extended scheduling flag (SM arch > 0x4FFF) |
| 4632 | 0x1218 | Instruction hash (ctx+1386 bit 3) |
| 4633 | 0x1219 | Annotation metadata |
| 4640 | 0x1220 | Extended section metadata |
| 4641 | 0x1221 | Optimization level record (from knob system, knob 988) |

### The `_ORI_` Name Prefix

The `_ORI_` string is not a pass name. At line 1762 the serializer iterates the linked list at `v7+55` (the original-definition chain maintained for rematerialization debugging) and for each entry creates a string `"_ORI_<original_name>"`:

```c
// Line 1748-1770 (simplified)
for (def = v7->original_defs; def; def = def->next) {
    entry = &section_table[16 * (state->instr_offset + idx)];
    entry->type_tag = 34;      // original-definition name
    entry->data_ptr = cursor;
    strcpy(cursor, "_ORI_");
    strcpy(cursor + 5, def->name);
    cursor += align16(strlen(def->name) + 21);
}
```

These names are consumed by the register allocation verifier (`sub_A55D80`) when it compares pre- and post-allocation reaching definitions. A mismatch triggers the "REMATERIALIZATION PROBLEM" diagnostic (string at `0xa55dd8`), which lists original definitions under their `_ORI_` names alongside the post-allocation state.

### Wrapper: `sub_A53840`

`sub_A53840` (48 lines) is a thin wrapper that:
1. Emits a type-44 header record if `*(ctx+1600)[1193]` is set (scheduling metadata header)
2. Calls `sub_A50650` with the output buffer
3. Optionally emits a type-62 trailer record if `*(ctx+1600)[48]` is set

This wrapper is the typical entry point reached through vtable dispatch.

## Function Map

| Address | Size | Callers | Identity |
|---------|------|---------|----------|
| `sub_A3B080` | ~700 B | multiple | Code Object constructor |
| `sub_A3A7E0` | ~700 B | 1 | Stats emitter (per-function profile) |
| `sub_A4B8F0` | ~250 B | 1 | Register count / annotation writer |
| `sub_A50650` | ~74 KB | 8 | ORI Record Serializer (`CodeObject::EmitRecords`) |
| `sub_A53840` | ~400 B | 1 | EmitRecords wrapper (adds type-44 header) |
| `sub_424070` | 2,098 B | 3,809 | Pool allocator (`alloc`) |
| `sub_4248B0` | 923 B | 1,215 | Pool deallocator (`free`) |
| `sub_424C50` | 488 B | 27 | Pool reallocator (`realloc`) |
| `sub_426150` | ~1.2 KB | 2,800 | Hash map insert |
| `sub_426D60` | 345 B | 422 | Hash map lookup |
| `sub_426EC0` | 349 B | 29 | Hash map contains |
| `sub_425CA0` | 114 B | 127 | Hash map constructor |
| `sub_425D20` | 121 B | 63 | Hash map destructor |
| `sub_42CA60` | 81 B | 298 | Linked list prepend |
| `sub_42CC30` | 34 B | 48 | Linked list length |
| `sub_427630` | 273 B | 73 | MurmurHash3 string hash |
| `sub_621480` | 21 KB | low | Symbol table builder |
| `sub_625800` | 27 KB | low | Symbol resolution |
| `sub_6BC560` | 4.9 KB | low | Constant bank handler |
| `sub_6294E0` | 12.1 KB | low | Reserved shared memory management |
| `sub_6C4DA0` | 15 KB | low | Shared memory intrinsic lowering |
| `sub_7FD6C0` | ~800 B | 3 | ELF symbol emitter |
| `sub_7FB6C0` | ~800 B | 1 | Pipeline orchestrator (context cleanup) |
| `sub_7FBB70` | ~100 B | 1 | Per-kernel entry point |
| `sub_663C30` | ~300 B | 1 | Compilation loop body |
| `sub_662920` | varies | 1 | Global initialization (calls KnobsInit) |

## Related Pages

- [Ori IR Overview](./overview.md) -- top-level IR design, Code Object field summary
- [Instructions](./instructions.md) -- detailed instruction format and encoding
- [CFG](./cfg.md) -- FNV-1a hash map CFG implementation
- [Registers](./registers.md) -- register descriptor layout
- [Phase Manager](../passes/phase-manager.md) -- PhaseManager object layout, phase dispatch
- [Memory Pool Allocator](../infra/memory-pools.md) -- full allocator internals
- [Hash Tables & Bitvectors](../infra/hash-bitvector.md) -- hash map and bitvector details
- [Knobs System](../config/knobs.md) -- knob descriptors, value types, ROT13 encoding
- [Entry Point & CLI](../pipeline/entry.md) -- compilation driver, options block
