# DVE Datamove / Misc Encoding — Shuffle / Transpose / Gather / Dropout / IndirectCopy / RangeSelect

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core, but `libwalrus.so` is rebuilt per wheel — re-confirm raw offsets against cp311/cp312). The encoder bodies and the pybind wire-struct/field strings live in `libwalrus.so` (build-id `92b4d331…`, `.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; `objdump -d -M intel` / IDA). The BIR `Inst*` op classes and the `NEURON_ISA_TPB_DTYPE` enum live in `libBIR.so` (build-id `a9b1ea38…`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of the **DVE data-movement and miscellaneous** instructions — the six ops whose silicon behavior is a copy/permute/gather/quantize-RNG rather than an arithmetic datapath: `StreamShuffle`, `StreamTranspose`, `Gather`, `Dropout`, `IndirectCopy`, and `RangeSelect`. Unlike the table-microcoded vector ops ([1.11 the DVE engine](../arch/dve-engine.md)), these carry **hand-encoded control bands** that the `CoreV2GenImpl`/`CoreV3GenImpl` C++ visitors stamp into the 64-byte bundle, plus — for `Gather`, `IndirectCopy` and `Dropout` — an **operand or threshold descriptor** packed into the access-pattern region.

The recovered wire structs name the families:

- **`StreamShuffle`** (`visitInstStreamShuffle`) decomposes into a `{stream_transpose 0x6A + stream_shuffle 0x69}` micro-pair; the shuffle half packs a **32-byte lane-select immediate** (`instr.immediate.uint8[32]`) across the entire `+0x20..+0x3F` control band.
- **`StreamTranspose`** (`visitInstStreamTranspose`) decomposes into `{stream_transpose 0x6B + stream_shuffle 0x69}`; the `0x6B` half is the `S4D4_TR` struct (`d3_transpose_ch`, `d4_tr_*`).
- **`Gather`** (`visitInstGather`) emits a `{reg_load 0x68 + pool_buffer_load 0x67}` pair per 512-element chunk; this page maps the **`0x68 reg_load`** half (`S4D4_GT` / `s_reg_load_*` / `gload_*`), whose paired `0x67` carries the index/data read ([2.10 PE-matmul page covers `0x67` only by reference](pe-matmul-encoding.md)).
- **`Dropout`** (`visitInstDropout`, op `0x7F`) is the `S3D3_DROPOUT` struct with a `threshold_imm_ptr` scalar-operand slot and an out-of-band-seeded RNG mode byte.
- **`IndirectCopy`** (`visitInstIndirectCopy`, op `0xE7`) is the `S4D4_IC` struct; its **TensorIndirect index descriptor** rides in the source band `+0x0C..+0x14` with the bit-29 indirect marker — the carried-descriptor contract shared with [2.7 indirect descriptors](indirect-descriptors.md).
- **`RangeSelect`** (`visitInstRangeSelect`, op `0xBC`) is `CoreV3`-only — the `S2D2_RS` struct with two **f32 bound immediates** at `+0x10`/`+0x14` and a paired `0x9B` select-reduce companion.

The bar for this page: a reader can **byte-encode any of these six instructions by hand**, knowing for each control byte its offset, width, semantic, value source, the recovered pybind/`NEURON_ISA_TPB_*_STRUCT` field name, and the disassembly store-site that pins it. Every field row carries a confidence tag: **CERTAIN** = exact store/cmp disassembled; **HIGH** = validator/string/helper xref cross-checked; **MEDIUM** = zero-init implied, no direct store. The control bands are recovered from the encoder bodies in `libwalrus.so`; offsets are pinned against the literal `mov`/`or`/`lea` constants in the disassembly, never inferred. Where a byte has no recovered name it is tagged INFERRED/unnamed — no field name is fabricated.

## At a glance

Each op shares the [2.1 64-byte bundle skeleton](instruction-bundle.md): `byte[0]` = opcode, `byte[1]` = `0x10` (`inst_word_len`, 16 dwords = 64 bytes), `byte[2:3]` = reserved `0x0000`. The bundle is `emplace_back`-ed as a `std::array<u8,64>`, **pxor-zeroed in full** (so any byte the encoder never writes reads `0x00`), header-stamped, field-filled, ISA-checked, then `fwrite(buf,1,0x40,bin)`-ed. The per-op **control band** is what this page maps.

| Opcode | BIR op / generator (libwalrus) | Engine | Wire struct (pybind) | Operand / descriptor band(s) |
|---|---|---|---|---|
| `0x69` stream_shuffle | `CoreV2GenImpl::visitInstStreamShuffle` `@0x123b460` (shuffle half) | DVE (5) | `…_STREAM_SHUFFLE` | 32-byte lane immediate @`+0x20..+0x3F`; AP @`+0x0C`/`+0x2C` |
| `0x6A` stream_transpose | `visitInstStreamShuffle` `@0x123b460` (transpose half) | DVE (5) | `S4D4_TR` (`d4_tr_*`) | src @`+0x0C`, dst @`+0x2C` |
| `0x6B` stream_transpose | `CoreV2GenImpl::visitInstStreamTranspose` `@0x1266df0` | DVE (5) | `S4D4_TR` (`d3_transpose_ch`) | src @`+0x0C`, dst @`+0x2C` |
| `0x68` reg_load | `CoreV2GenImpl::visitInstGather` `@0x12532e0` (reg_load half) | DVE (5) | `S4D4_GT` (`s_reg_load_*`/`gload_*`) | data-src @`+0x0C`, dst @`+0x2C` |
| `0x67` pool_buffer_load | `visitInstGather` `@0x12532e0` (pbl half) | DVE (5) | `…_POOL_BUFFER_LOAD` | index/src-AP @`+0x18` (see [2.10](pe-matmul-encoding.md)) |
| `0x7F` dropout | `CoreV2GenImpl::visitInstDropout` `@0x123e510` | DVE (5) | `S3D3_DROPOUT` (`d3_dropout_*`) | TENSOR3D src @`+0x10`, dst @`+0x30`; `threshold_imm_ptr` @`+0x23` |
| `0xE7` indirect_copy | `CoreV2GenImpl::visitInstIndirectCopy` `@0x1275c40` | DVE (5) | `S4D4_IC` (`s4d4_ic_*`) | indirect idx desc @`+0x0C..+0x14`; dst TENSOR4D @`+0x2C` |
| `0xBC` range_select | `CoreV3GenImpl::visitInstRangeSelect` `@0x135f8c0` | DVE (5) | `S2D2_RS` (`s2d2_rs_*`) | TENSOR2D src @`+0x18`, dst @`+0x30`; pairs with `0x9B` |

> **NOTE — three of these ops are *micro-pairs*, not single bundles.** `StreamShuffle` lowers to two bundles `{0x6A, 0x69}`, `StreamTranspose` (on the ≥32-lane path) to `{0x6B, 0x69}`, and `Gather` to `{0x68, 0x67}` per 512-element chunk; `RangeSelect` pairs with a `0x9B` select-reduce companion. The COLLECT pass inserts **both** opcodes of a pair into the per-instruction opcode set — the cleanest witness that the lowering is a multi-bundle sequence. A reimplementation that emits one bundle per BIR op for these four will under-emit. Each pair's two `movl $op` COLLECT writes are cited in the sections below.

## The bundle lifecycle (shared by all six)

Every DVE datamove visitor runs the same cycle; the `CodeGenMode` selector at `*(this+0x270)` picks the sink:

1. **Emplace + zero.** `SmallVectorImpl<std::array<u8,64>>::emplace_back`, then `pxor xmm0` + `movups xmm0 → [base+0x00/10/20/30]` blanket-zeroes all 64 bytes. *Every reserved/unwritten byte reads `0x00`.*
2. **Header.** `setupHeader` seeds `byte[0]=op`, `byte[1]=0x10`, `byte[2:3]=0` via a vtable `call *0x48(rax)` (slot 9).
3. **Sync band.** `setupSync*` thunks stamp the wait/update semaphore band (where present).
4. **dtype + control fields.** `sub_120E650(AP+0x30)` → ISA dtype byte → `+0x20`/`+0x21`; per-opcode control fields (this page). Scalar fields route through the bounds-checked setter `sub_12173A0` (objdump-mislabelled `leaveFunction+0xD40`), which takes a `lea [base+off]` dst, a field-name C-string, and the value — so every offset below is read off the literal `lea`/`mov [base+off]` store, never a register convention.
5. **Access patterns.** `assignAccess<NEURON_ISA_TPB_TENSOR4D/3D>` (`@0x603ee0`, V2) writes src → `+0x0C`/`+0x10`, dst → `+0x2C`/`+0x30`; `CoreV3` `TENSOR2D` via `@0x6171D0`.
6. **ISA check + emit.** `runISACheck` (`sub_12095A0`) appends to `this+0x40` and validates in place, then `fwrite(buf,1,0x40,findBin(I))`, then census `++map@this+0x1e0`.

`CodeGenMode` arms: **1 = GENERATE** (fill + `fwrite`); **2 = RUN_ISA_CHECKS** (build a *byte-identical* bundle on a stack scratch struct, feed the validator via `call *(rax)` slot 0, no `fwrite`); **0 = COLLECT** (`movl $op` + `_Rb_tree<unsigned>::_M_insert_unique` into the opcode set, emit nothing). The field map is shared between modes 1 and 2.

> **GOTCHA — on the DVE datamove path `CodeGenMode` lives at `*(this+0x270)`, not at `this+0x9C`.** The PE-side encoders read it from `this+156`, so a shared-lifecycle description carried over from that page will read the wrong field. Every DVE datamove body reads `mov eax,[rdi+0x270]` (Dropout `@0x123e527`; Shuffle `@0x123b62d` reads `[r14+0x270]`): `cmp $1` → GENERATE, `cmp $2` → RUN_ISA_CHECKS, else COLLECT.

---

## StreamShuffle — `visitInstStreamShuffle @0x123b460` → `{0x6A + 0x69}` pair

`bir::InstStreamShuffle`. The COLLECT pass inserts **both** `0x69` (`movl $0x69 @0x123b676`) and `0x6A` (`movl $0x6a @0x123b6d9`): the lowering is a two-bundle micro-sequence — a forward `stream_transpose(0x6A)` then a `stream_shuffle(0x69)`.

The pre-amble (`0x123b4f8..0x123b627`) walks the src-AP `Pattern[]` (stride `0x28`, leaf count at `+0x00`), `pmin`s against the dst sizes, computes a default lane byte `edx = min(...)`, then `eax = edx | 0x20` stored to `-0x420(rbp)` — the **shuffle passthrough sentinel** (`partition_count | 0x20`).

*Anchors: `or eax,20h` @ `0x123b624`, `mov [rbp-420h],al` @ `0x123b627`.*

### `0x6A` stream_transpose half (the dtype-bearing bundle)

GEN block `0x123b7de..0x123b960`; scratch base `-0x3a0(rbp)`, bundle base reg `r12`. Opcode seed `movb $0x6A @0x123b829`.

| Off | W | Field | pybind / `d4_tr_*` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x6A` | (stream_transpose) | setupHeader seed | `0x123b829` | CERTAIN |
| `+0x01` | 1 | `inst_word_len = 0x10` | — | hdr constant | hdr | MEDIUM |
| `+0x20` | 1 | in_dtype | (`d4_tr` dtype) | `sub_120E650(srcAP+0x30)→al` | `0x123b86f` | CERTAIN |
| `+0x21` | 1 | out_dtype | (`d4_tr` dtype) | `sub_120E650(dstAP+0x30)→al` | `0x123b87c` | CERTAIN |
| `+0x22` | 4 | num_active_channels | `instr.num_active_channels` | `(min(src.sz,dst.sz)+0x1f)&~0x1f` via `sub_12173A0(r12+0x22)` | `0x123b8b2` | CERTAIN |
| `+0x0C` | 16 | src AP (`TENSOR4D`) | `d4_tr_*` src | `assignAccess(src)` | `0x123b8ea` | CERTAIN |
| `+0x2C` | 16 | dst AP (`TENSOR4D`) | `d4_tr_*` dst | `assignAccess(dst)` | `0x123b8fc` | CERTAIN |

Guards: both element counts nonzero (`je → error 0x123c5b3`) — the `d4_tr_same_src_dst_count` / `d4_tr_transpose_src_dst_count` predicate family. `runISACheck @0x123b90f`, `fwrite @0x123b936`, census key `0x6A @0x123b96f`.

### `0x69` stream_shuffle half (the 32-byte lane immediate)

GEN block `0x123ba50..` (perm loop `..0x123c3be`); scratch base `-0x400(rbp)`. Opcode seed `movb $0x69 @0x123ba9b`.

The **perm collection** (`0x123bb00..0x123bb5d`) iterates the 2nd src-AP `Pattern[]` (stride `0x28`, DWORD at `+0x00`), appending each into a `SmallVector<u32> @-0x230(rbp)`, then asserts the count `== 0x20` (`cmp eax,0x20 @0x123bb5f`, else the `"Mask.size() == 32"` error path). The **lane-byte pack loop** (`0x123c344..0x123c3be`) then writes the 32 mask bytes:

```c
// for each of the 32 mask entries:
//   v = mask[i];  if (v == 0xFF) v = passthrough_sentinel;   // (= partition_count|0x20)
//   sub_12173A0(/*dst=*/&bundle[0x20 + i], "instr.immediate.uint8[i]", v);
// (i >= count → fill remainder with the passthrough byte: mov [r15],al @0x123c3bb)
```

| Off | W | Field | pybind name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x69` | (stream_shuffle) | setupHeader seed | `0x123ba9b` | CERTAIN |
| `+0x01` | 1 | `inst_word_len = 0x10` | — | hdr constant | hdr | MEDIUM |
| `+0x20..+0x3F` | 32 | `immediate.uint8[32]` lane map | `instr.immediate.uint8[i]` | per-lane mask byte; `0xFF` → passthrough (`partition_count\|0x20`) | loop base `r15 = r12+0x20` `@0x123c344` | CERTAIN |
| `+0x0C` | 16 | src AP | (shuffle src) | `assignAccess(src)` | RUN_ISA mirror `0x123c2d6` | CERTAIN |
| `+0x2C` | 16 | dst AP | (shuffle dst) | `assignAccess(dst)` | `0x123c2ea` | CERTAIN |

> **QUIRK — the shuffle bundle has NO dtype field.** Because the 32 lane bytes occupy the *entire* `+0x20..+0x3F` control band, there is no room for in/out dtype on the `0x69` bundle — the dtype rides on the paired `0x6A` transpose bundle. The perm-loop base address is exactly `r12+0x20` (`lea r15,[r12+20h] @0x123c344`), proving the lane map starts at byte `0x20`.

The validator `dbg_is_valid_stream_shuffle @0x13281a0` copies the union (base `rsp+0x6AF0`) and fans into per-field sub-checks; it reads `[+0]`opcode, `[+0x20..+0x22]` (dtype/active on the transpose side, `cmp r14b,0x6A @0x13282fa` selecting the transpose sub-validator), and the AP tails at `[+0x18]/[+0x1A]/[+0x38..+0x3E]`. The validator covers the *pair*. The reads are disassembled; the semantic names come from the `.rodata` predicate strings.

---

## StreamTranspose — `visitInstStreamTranspose @0x1266df0` → `{0x6B + 0x69}` pair

`bir::InstStreamTranspose`. Mirrors StreamShuffle but the transpose opcode is **`0x6B`**, not `0x6A` — the two values are the **two transpose directions** (`0x6A` = the forward-transpose half emitted *inside* StreamShuffle; `0x6B` = the standalone StreamTranspose). COLLECT inserts `0x6B` (`movl $0x6b @0x1266ead`), then on the wide path `0x69` (`movl $0x69 @0x1267459`).

**Head fork** (`0x1266e5f`): `cmp ebx,0x1f` where `ebx` = dst-AP partition size. If `ebx ≤ 31` ("fits one partition group"), the `0x1266ee0` path calls `bir::Module::setAttribute(attr #9)` and COLLECT-emits **only** `0x6B` (no shuffle half, `setAttribute @0x1266f0f`). If `ebx ≥ 32`, it emits the full `{0x6B + 0x69}` pair.

### `0x6B` stream_transpose half

GEN block `0x1267486..0x12670dd`; scratch base `-0x180(rbp)`. Opcode seed `movb $0x6B @0x1266fd7` / `@0x12674d2` (two GEN sites for the two head cases).

| Off | W | Field | pybind / `d3_transpose_*` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x6B` | (stream_transpose) | setupHeader seed | `0x1266fd7` | CERTAIN |
| `+0x01` | 1 | `inst_word_len = 0x10` | — | hdr constant | hdr | MEDIUM |
| `+0x20` | 1 | in_dtype | (`d4_tr` dtype) | `sub_120E650(srcAP+0x30)→al` | `0x1267049` (rbp-0x180) | CERTAIN |
| `+0x21` | 1 | out_dtype | (`d4_tr` dtype) | `sub_120E650(dstAP+0x30)→al` | `0x126705f` (rbp-0x17f) | CERTAIN |
| `+0x22` | 4 | num_active_channels | `instr.num_active_channels` | `(dst.num_elem+0x1f)&~0x1f` via `sub_12173A0` | `0x1267083` (rbp-0x17e) | CERTAIN |
| `+0x26` | 1 | transpose_ch = `0x1` | `d3_transpose_ch` | constant `1` (channel-transpose enable) | `0x1267058` (rbp-0x17a) | CERTAIN store / HIGH name |
| `+0x0C` | 16 | src AP (`TENSOR4D`) | `d4_tr_*` src | `assignAccess(src)` | `0x12670bc` | CERTAIN |
| `+0x2C` | 16 | dst AP (`TENSOR4D`) | `d4_tr_*` dst | `assignAccess(dst)` | `0x12670d0` | CERTAIN |

**Guard A** (`0x126711c..`): the src AP must **not** be TensorIndirect — a vtable `[r15]+0x80` probe then `reportError("STREAM_TRANSPOSE cannot have TensorIndirect AP as src…")`. **Guard B** (`0x1267171`): `cmp WORD [rbp-0x182],0x1` enforces `src_mem_pattern.num_elem[3] == 1` (the `+0x1E` word in the src-AP descriptor) — the string `"instr.src_mem_pattern.num_elem[3] == 1"` is present.

> **NOTE — `d3_transpose_ch = 1` is the only thing distinguishing the standalone transpose on the wire.** It is a constant-`1` store (`0x1267058: c6 85 86 fe ff ff 01  mov byte [rbp-17Ah],1`); only this op sets it. The shuffle-internal `0x6A` half leaves `+0x26` zeroed. The `0x69` shuffle half of StreamTranspose is structurally identical to StreamShuffle's §"`0x69`" — same 32-byte `immediate.uint8[]` lane map, same passthrough sentinel and `Mask.size()==32` assert (GEN seeds `@0x12680d7`/`@0x1268497`).

Validator `dbg_is_valid_stream_transpose @0x1325aa0` (base `rsp+0x7020`): reads `[+0]`opcode (`cmp 0x6B @0x1325bff`), `[+0x20]`in_dtype, `[+0x21]`out_dtype, `[+0x22]`active, `[+0x25]`. Predicates `has_valid_active_channel_range`, `s4d4_tr_same_src_dst_count`, `d4_tr_same_src_dst_count`. Recovered `S4D4_TR` field family: `d4_tr_same_src_dst_count`, `d4_tr_transpose_src_dst_count`, `d4_tr_op_bypass`, `d4_tr_reserved_z`, `d3_transpose_ch`.

---

## Gather — `visitInstGather @0x12532e0` → `{0x68 reg_load + 0x67 pool_buffer_load}`

`bir::InstGather`. This page maps the **`0x68 reg_load`** half; the paired `0x67 pool_buffer_load` control band (`S4_PB`) is mapped by [2.10](pe-matmul-encoding.md) and only summarized here for the join.

**Pre-amble** (`0x1253324..0x1253481`): `arg0 = getArgument(0)` = DATA/source AP (`rbp-0xd8`); `arg1 = getArgument(1)` = **INDEX AP** (`rbp-0xf0`); `dst = getOutput` (`rbp-0xf8`). The DATA AP must **not** be TensorIndirect — `reportError("TODO: support POOL_BUFFER_LOAD with TensorIndirect AP once ISA supports it" @0x1d6a2e0)`. `idx_dtype_val = arg0.AP[0x50][0x8] → rbp-0xe8` (used at `reg_load +0x22`). **Chunk math**: `n = getNumElementsPerPartition(); chunks = floor(n × 1/512)` (`mulss` by `1/512`, `roundss` trunc; chunk size `0x200` = 512). The gather streams the index tensor in **512-element chunks** — one `{reg_load, pool_buffer_load}` pair per chunk, `r13d` = chunk index. COLLECT inserts `0x67` (`movl $0x67 @0x12534e7`) and `0x68` (`movl $0x68 @0x1253579`).

### `0x68` reg_load half

GEN block `0x125372e..0x12537ea`; scratch base `-0x70(rbp)`, base reg `r14`. Opcode seed `movb $0x68 @0x125372e` (RUN_ISA mirror `@0x1253df8`).

| Off | W | Field | pybind / `s_reg_load_*`,`gload_*` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x68` | `s_reg_load_opcode` | setupHeader seed | `0x125372e` | CERTAIN |
| `+0x01` | 1 | `inst_word_len = 0x10` | — | hdr constant | hdr | MEDIUM |
| `+0x20` | 1 | in_dtype | `gload_valid_dtype` | `sub_120E650(arg0.AP+0x30)→al` | `0x1253756` | CERTAIN / name HIGH |
| `+0x21` | 1 | out_dtype | (`s_reg_load` dtype) | `sub_120E650(dst.AP+0x30)→al` | `0x125376b` | CERTAIN |
| `+0x22` | 1 | gload_active_channels | `gload_active_channels` | `idx_dtype_val(rbp-0xe8)` low byte | `0x1253785` | CERTAIN / name HIGH |
| `+0x24` | 2 | gather options / continuation flag | (`gather_valid_options` / `s_gather_index_dtype`) | `al = (chunk_index != 0) ? 1 : 0`; `ah = 0` | `0x125379c` (`mov [r14+24h],ax`) | CERTAIN store / semantic HIGH |
| `+0x28` | 4 | gload_vector_depth = `0` | `gload_vector_depth` | constant `0` (vs the `0x67` `0x1FF@+0x2C`) | `0x125377d` | CERTAIN |
| `+0x0C` | 16 | src AP (data) `TENSOR4D` | (`s_reg_load` src) | `assignAccess(arg0)` | `0x12537a1` | CERTAIN |
| `+0x2C` | 16 | dst AP `TENSOR4D` | (`s_reg_load` dst) | `assignAccess(dst)` | `0x12537b6` | CERTAIN |

`runISACheck @0x12537ca`, `fwrite @0x12537ea`, census key `0x68`. Recovered `S4D4_GT` / reg_load field family: `s_reg_load_opcode`, `_rl_reserved_zero`, `gload_valid_dtype`, `gload_active_channels`, `gload_vector_depth`; `s_gather_opcode`, `d4_gt_reserved_z`, `s_gather_index_dtype`, `d4_gt_same_src_dst`, `gather_valid_options` (all present in `.rodata`).

> **GOTCHA — the gather INDEX (`arg1`) is NOT in the reg_load AP slots.** The `+0x0C`/`+0x2C` `TENSOR4D` descriptors carry the **data-src** and **dst** — the index AP is consumed by the *paired* `0x67 pool_buffer_load` bundle (`S4_PB`: src-AP @`+0x18`, elem-count @`+0x28`, vec-depth `0x1FF` @`+0x2C`; per [2.10](pe-matmul-encoding.md)). The `0x68 reg_load` loads the gathered register/index base; the `0x67 pool_buffer_load` performs the indexed read into the pool buffer; the `+0x24` continuation flag (`0` on chunk 0, `1` after) chains the per-512-element chunks. The reg_load census loop links the pair by scanning the engine stream for `[rax+0x20]==0x67`/`0x68` (`@0x1253828`/`@0x125384d`).

Validator `dbg_is_valid_reg_load @0x1301820` (base `rsp+0x4770`): reads `[+0]`opcode, `[+1]`, `WORD[+0x18..+0x1E]` (src-AP), `[+0x20]`dtype, `[+0x22]`active, `[+0x24]`options; dispatch `cmp r9b,0x4A` selects `gload_valid_dtype` / `gload_active_cha` / `_rl_reserved_zer`.

---

## Dropout — `visitInstDropout @0x123e510` op `0x7F` (`S3D3_DROPOUT`)

Single-bundle op. COLLECT `movl $0x7F @0x123e5fb`; GEN seed `movb $0x7F @0x123e676` (RUN_ISA mirror `@0x123e985`). Scratch GEN base `-0xf0(rbp)`, base reg `r13`. Pre: `setupSync` thunks; `arg0 = getArgument(0)` = src AP (`rbp-0x118`); `dst = getOutput` (`rbp-0x110`). Uses **TENSOR3D** access (not 4D).

| Off | W | Field | pybind / `d3_dropout_*` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x7F` | `s_dropout_opcode` | setupHeader seed | `0x123e676` | CERTAIN |
| `+0x01` | 1 | `inst_word_len = 0x10` | — | hdr constant | hdr | MEDIUM |
| `+0x20` | 1 | in_dtype | (`d3_dropout` dtype) | `sub_120E650(src.AP+0x30)→al` | `0x123e6c7` | CERTAIN |
| `+0x21` | 1 | out_dtype | (`d3_dropout` dtype) | `sub_120E650(dst.AP+0x30)→al`; `d3_dropout_same_src_dst_type` asserts in==out | `0x123e6d3` | CERTAIN |
| `+0x22` | 1 | active / lane | `d3_dropout_src_dst_count_check` | `src.AP[0x50][0x8]` low byte | `0x123e701` | CERTAIN |
| `+0x23` | 1 | threshold_imm_ptr | `threshold_imm_ptr` | scalar-operand slot index from a `boost::variant` via converter `sub_12051E0` (4-fn visitor table `@0x123e751..`); `lea rsi,[r13+0x23]` | `0x123e740` | CERTAIN store / name HIGH |
| `+0x24` | 1 | threshold_dtype | `d3_dropout_threshold_dtype_check` | `sub_120E650(converter eax)` → dtype byte | `0x123e848` | CERTAIN |
| `+0x2B` | 1 | rng_mode / dropout_variant | — | `*(this+0xF0)` (generator RNG-config byte) | `0x123e737` | CERTAIN store / semantic MEDIUM |
| `+0x10` | 16 | src AP (`TENSOR3D`) | (`d3_dropout` src) | `assignAccess<TENSOR3D>(src)` | `0x123e856` | CERTAIN |
| `+0x30` | 16 | dst AP (`TENSOR3D`) | (`d3_dropout` dst) | `assignAccess<TENSOR3D>(dst)` | `0x123e864` | CERTAIN |

`runISACheck @0x123e877`, `fwrite 0x40` (shared tail).

> **NOTE — the threshold is a 1-byte *operand-slot index*, not an inline f32.** `+0x23 threshold_imm_ptr` does **not** hold the dropout keep/drop probability — it holds a 1-byte index into the engine's scalar-operand bank, resolved from a `boost::variant` (the threshold may be an immediate or a register). The actual `f32` threshold lives in the operand bank; the bundle carries only the pointer plus its dtype at `+0x24`. The store target is `lea rsi,[r13+0x23]` (`@0x123e740`), and the validator reads a single BYTE there.

> **GOTCHA — there is NO RNG seed field in the dropout bundle.** The `+0x2B rng_mode` byte selects *which* RNG mode/variant the dropout consumes (`*(this+0xF0)`, the generator's RNG-config global), but the RNG **seed/state** is established out-of-band by a separate `PseudoSetRngSeed` pseudo-instruction (`s_rng_opcode`). The dropout op draws from the engine's *current* RNG stream — a reimplementation must emit a seed-setter ahead of the dropout, not encode a seed into byte `0x2B`.

Validator `dbg_is_valid_dropout @0x12dbef0` (base `rsp+0x60C0`): reads `[+0]`opcode (`cmp 0x7F @0x12dc08b`), `[+1]`, `WORD[+0x1A..+0x1E]` (src-AP), `[+0x20]`in_dtype, `[+0x21]`out_dtype, `[+0x22]`active, `[+0x23]`threshold_imm_ptr, `[+0x24]`threshold_dtype, `[+0x2B]`rng_mode, `WORD[+0x3A..+0x3E]` (dst-AP). Predicates `d3_dropout_same_src_dst_type`, `d3_dropout_src_dst_count_check`, `threshold_imm_ptr`, `d3_dropout_threshold_dtype_check`, `d3_dropout_reserved_zero` (all present in `.rodata`).

---

## IndirectCopy — `visitInstIndirectCopy @0x1275c40` op `0xE7` (`S4D4_IC`)

Single-bundle op carrying a **TensorIndirect index descriptor** in the source band. COLLECT `movl $0xE7 @0x1275d2b`; GEN seed `movb $0xE7 @0x1275dab` (RUN_ISA mirror `@0x12760ec`). Scratch GEN base `-0xd0(rbp)`, base reg `r13`. Pre: `setupSync` thunks; `arg0 = getArgument<PhysicalAccessPattern>(0)` = **INDIRECT INDEX src AP** (`r15`); `arg1 = getArgument<PhysicalAccessPattern>(1)`; `dst = getOutput<PhysicalAccessPattern>` (`rbx`). **Guard**: `arg0` must be a `TensorIndirectDynamicAP` (`isTensorIndirectDynamicAP() @0x1275e2f`), else `reportError("INDIRECT_COPY is a prototype instruction of Tensor Indirect on TRN1, it must not …" @0x1d6cb70)`.

The TensorIndirect descriptor is resolved via `*(this+0x260)` (the TensorIndirect address resolver, vtable slot `0x20`) and packed into the source band `+0x0C..+0x14`:

| Off | W | Field | pybind / `s4d4_ic_*` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xE7` | `s_indirect_copy_opcode` | setupHeader seed | `0x1275dab` | CERTAIN |
| `+0x01` | 1 | `inst_word_len = 0x10` | — | hdr constant | hdr | MEDIUM |
| `+0x0C` | 4 | index_addr.addr_immediate | `instr.src_mem_pattern.i.p.index_addr.addr_immediate` | resolved index ADDR4 imm; `lea [r13+0xC]` | `0x1275e9f` | CERTAIN |
| `+0x0F` | 1 | (bit5) indirect-mode flag | (TensorIndirect AP-kind bit) | `or BYTE [r13+0xF],0x20` | `0x1275eb6` | CERTAIN |
| `+0x10` | 4 | data_addr.addr_immediate | `instr.src_mem_pattern.i.p.data_addr.addr_immediate` | resolved data ADDR4 imm; `lea [r13+0x10]` | `0x1275ed8` | CERTAIN |
| `+0x14` | 4 | index num_elem | `instr.src_mem_pattern.i.p.num_elem` | `getNumElementsPerPartition(arg0)`; `lea [r13+0x14]` | `0x1275efe` | CERTAIN |
| `+0x20` | 1 | in_dtype | `s_same_in_out_dtype_indirect_copy` (in arm) | `sub_120E650(arg0.AP+0x30)→al` | `0x1275f1b` | CERTAIN |
| `+0x21` | 1 | out_dtype | `s_same_in_out_dtype_indirect_copy` (out arm) | `sub_120E650(dst.AP+0x30)→al` | `0x1275f27` | CERTAIN |
| `+0x22` | 1 | active_channels | `s_valid_s4d4_ic_active_channels` | `arg0.AP[0x50][0x8]` | `0x1275f42` | CERTAIN |
| `+0x23` | 1 | src_num_elem_per_idx | `instr.src_num_elem_per_idx` | `getNumElementsPerPartition(dst) / WORD[arg.+0xf0]`; `lea [r13+0x23]` | `0x1275f6d` | CERTAIN |
| `+0x26` | 4 | src_buffer_size | `instr.src_buffer_size` | `getNumElementsPerPartition(arg0)`; `lea [r13+0x26]` | `0x1275f99` | CERTAIN |
| `+0x2C` | 16 | dst AP (`TENSOR4D`) | (`s4d4_ic` dst) | `assignAccess<TENSOR4D>(dst)` | `0x1275fbf` | CERTAIN |

`runISACheck @0x1275fd0`, `fwrite @0x1275ff0`.

> **NOTE — IndirectCopy carries its DATA source *inside* the index descriptor, not in a plain AP slot.** Only the **dst** uses a plain `TENSOR4D` slot at `+0x2C`; the source side is the three-word indirect descriptor `{index_addr@+0x0C, data_addr@+0x10, num_elem@+0x14}` with the bit-5 (bit-29 of the ADDR4) marker `or [r13+0xF],0x20`. This is the same indirect-descriptor contract as [2.7 INDIRECT16B/20B](indirect-descriptors.md): the `0x20` bit on byte 3 of the INDEX ADDR4 is the TensorIndirect mode selector. Here the descriptor is *embedded in the instruction control band* rather than in a `MEM_PATTERN` access slot, but the marker bit and the `{index, data}` ADDR4 pairing are identical (`or byte [r13+0Fh],20h` @ `0x1275eb6`).

Validator `dbg_is_valid_indirect_copy @0x1313fd0` (base `rsp+0x56C0`): reads `[+0]`opcode (`cmp 0xE7 @0x1314141`), `[+1]`, `[+0x20]`in_dtype, `[+0x21]`out_dtype, `[+0x22]`active, plus the `+0x0C..+0x14` descriptor and `+0x23`/`+0x26` in sub-checks. Predicates `s_same_in_out_dtype_indirect_copy`, `s_valid_s4d4_ic_active_channels`, `gather_valid_options` (shared option-validator).

---

## RangeSelect — `CoreV3GenImpl::visitInstRangeSelect @0x135f8c0` op `0xBC` (`S2D2_RS`)

`bir::InstRangeSelect`. **CoreV3-ONLY** — there is no `CoreV2GenImpl::visitInstRangeSelect` symbol. Uses **TENSOR2D** access patterns. COLLECT `movl $0xBC @0x135f95f` **and** `movl $0x9B @0x135fa9a` → the lowering emits a `{range_select 0xBC + companion 0x9B}` pair (`0x9B` = the paired select-reduce / mask micro-op). GEN seed `movb $0xBC @0x135fb14` / `@0x135feeb`; companion seed `movb $0x9B @0x136097f`. Base reg `r15`, scratch `-0x180(rbp)`. Takes **three** args: `arg0 = getArgument(0)` = data AP; `arg1 = getArgument(1)` = lower operand; `arg2 = getArgument(2)` = upper operand; `dst = getOutput`. Guard: src/dst element counts must match — `NeuronAssertion("inNumEles == outNumEles" @0x135fed6)`.

| Off | W | Field | pybind / `s2d2_rs_*` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xBC` | `s_range_select_opcode` | setupHeader seed | `0x135fb14` | CERTAIN |
| `+0x01` | 1 | `inst_word_len = 0x10` | — | hdr constant | hdr | MEDIUM |
| `+0x0D` | 1 | dtype/mode packed | (`s2d2_rs` dtype) | low nibble = dtype tag (`and 0xf`); high nibble = 2nd 4-bit field (`shl 4; or`); composed `@0x1360ece..0x1360edb` | `0x1360edb` | CERTAIN store / nibble HIGH |
| `+0x0E` | 1 | (mode flag) | — | `inst+0x124` via `sub_1343d50` | `0x1360f19` | CERTAIN |
| `+0x0F` | 1 | op-variant flag = `0x8` | — | constant `8` | `0x1360f0b` | CERTAIN |
| `+0x10` | 4 | LOWER bound (f32) | (`s2d2_rs` lower) | `cvtsi2ss(int [inst+0xF0])` → `movss [r15+0x10]` | `0x13611ff` | CERTAIN |
| `+0x14` | 4 | UPPER bound (f32) | (`s2d2_rs` upper) | `movss xmm0,[inst+0x128]` → `movss [r15+0x14]` | `0x13611c4` | CERTAIN |
| `+0x24` | 1 | (field a) | — | `inst+0x118` via `sub_1343e40` | `0x1360eeb` | CERTAIN |
| `+0x25` | 1 | (field b) | — | `inst+0x11c` via `sub_1343e40` | `0x1360efb` | CERTAIN |
| `+0x26` | 1 | bound-check field 0 | (`valid_rs_comp`) | `checkBoundRange(lower)` packed via variant dispatcher; `lea [r15+0x26]` | `0x1360f66` | CERTAIN offset |
| `+0x27` | 1 | bound-check field 1 | (`valid_rs_comp`) | `checkBoundRange(upper)`; `lea [r15+0x27]` | `0x13610ba` | CERTAIN offset |
| `+0x18` | 16 | data src AP (`core_v3::TENSOR2D`) | (`s2d2_rs` src) | `assignAccess<TENSOR2D>(arg0)` | `0x1361205` | CERTAIN |
| `+0x30` | 16 | dst AP (`core_v3::TENSOR2D`) | (`s2d2_rs` dst) | `assignAccess<TENSOR2D>(dst)` | `0x136121a` | CERTAIN |

`runISACheck` (`r13+0x40`, `sub @0x1361234..`), shared `fwrite` tail. The companion `0x9B` bundle is built separately (base differs) carrying the select-reduce mask half.

Op number `0xBC` (= 188) is the gen-3 DVE roster's `RangeSelect` addition ([1.11 named roster](../arch/dve-engine.md)), and the encoder is resident only in `CoreV3GenImpl` — `s2d2_rs_*` is therefore the V3 wire family, with no V2 counterpart. The bounds are **two f32 immediates** at `+0x10`/`+0x14`: the lower is an `int → float` convert, the upper a raw f32 immediate, both validated by the `backend::checkBoundRange` helpers (`0x134A150`/`0x134A420`) invoked immediately before the stores. The `S2D2_RS` `.rodata` family is present in full — `s_range_select_opcode`, `s_same_elem_count_rs_src_dst`, `valid_rs_comp_op`, `s_valid_range_select_reduce_op` — alongside the assertion `"fill_val in RangeSelect must be float MAX_NEG …"`. The companion `0x9B` half is not deep-transcribed here; it falls outside the named-encoder scope.

Validator `dbg_is_valid_range_select @0x13b9950` (`core_v3`, base `rsp+0x6FF0`): reads `[+0]`opcode (`cmp 0xBC @0x13b9bac`), `[+1]`, `[+0xC]`, `[+0xD]` (dtype/mode), `[+0x18]` (TENSOR2D AP base), `[+0x38]`; the f32 bounds (`+0x10`/`+0x14`) and `+0x26`/`+0x27` are checked in sub-validators. Predicate `s_valid_idx_bound_check_reg`.

---

## Crc32 — negative result

> **NOTE — there is no `Crc32` instruction in `neuronx_cc` 2.24.** An exhaustive sweep finds no CRC/checksum/hash **instruction** anywhere in the ISA emit path: the full `CoreV{2,3,4}GenImpl::visitInst*` roster contains no Crc/Checksum/Hash op; `nm -DC` for `visitInst{Crc,Checksum,Hash,Cksum}` → 0 hits; no `is_valid_crc*`/`s_crc_*` validator; no `crc` field family in the pybind; `.rodata` string sweep for `crc`/`crc32`/`InstCrc`/`cyclic` finds only unrelated `dap::Checksum` debug-adapter symbols and `.gnu.hash` ELF names. If a CRC is computed it is host/runtime software, not a tensor-engine instruction — there is no bundle to encode.

---

## Master offset table — DVE datamove/misc control bands

Byte offsets within the 64-byte bundle. `dt` = dtype byte (`sub_120E650`); `AP` = `assignAccess` descriptor; `f32` little-endian.

```text
 op    name              +0x20 +0x21 +0x22   other fields
 ----  ----------------  ----- ----- ------   --------------------------------------------
 0x69  stream_shuffle    --    --    --       +0x20..+0x3F = immediate.uint8[32] lane map
                                              (0xFF → passthrough = partition_cnt|0x20);
                                              AP +0x0C/+0x2C
 0x6A  stream_transpose  in_dt out_dt active  +0x0C src / +0x2C dst  (shuffle's xpose half)
 0x6B  stream_transpose  in_dt out_dt active  +0x26 transpose_ch=1; +0x0C/+0x2C AP;
       (standalone)                           guards: no-TensorIndirect, num_elem[3]==1
 0x68  reg_load          in_dt out_dt active  +0x24(u16) gather-options/continuation flag;
       (gather half)                          +0x28(u32) vector_depth=0; +0x0C/+0x2C AP
 0x67  pool_buffer_load  in_dt --    lane     +0x28 elem-cnt, +0x2C 0x1FF, +0x18 idx-AP  (2.10)
 0x7F  dropout           in_dt out_dt active  +0x23 threshold_imm_ptr, +0x24 thr_dtype,
                                              +0x2B rng_mode; +0x10/+0x30 TENSOR3D AP
 0xE7  indirect_copy     in_dt out_dt active  +0x0C index_addr, +0x0F bit5 ind-flag,
                                              +0x10 data_addr, +0x14 num_elem,
                                              +0x23 num_elem_per_idx, +0x26 buf_size,
                                              +0x2C dst TENSOR4D AP
 0xBC  range_select(V3)  --    --    --       +0x0D dt/mode, +0x0E flag, +0x0F=8,
                                              +0x10 f32 LOWER, +0x14 f32 UPPER,
                                              +0x24/+0x25 fields, +0x26/+0x27 bound-chk,
                                              +0x18/+0x30 TENSOR2D AP; pairs with 0x9B

 SHARED:  byte[0]=opcode, byte[1]=0x10 (inst_word_len).  CodeGenMode @*(this+0x270).
 Scalar field setter = sub_12173A0 (bounds-checked; dst = lea[base+off]).
 dtype LUT = sub_120E650.  assignAccess @0x603EE0 (V2 4D/3D) / 0x6171D0 (V3 2D).
 runISACheck = sub_12095A0.
```

## Opcode witness ledger

| Opcode | COLLECT (`movl`) | GEN seed (`movb`) | Validator |
|---|---|---|---|
| `0x69` stream_shuffle | `@0x123b676` | `@0x123ba9b` | `dbg_is_valid_stream_shuffle @0x13281a0` |
| `0x6A` stream_transpose | `@0x123b6d9` (in Shuffle) | `@0x123b829` | (pair-covered by `0x13281a0`) |
| `0x6B` stream_transpose | `@0x1266ead` | `@0x1266fd7`/`@0x12674d2` | `dbg_is_valid_stream_transpose @0x1325aa0` (`cmp 0x6B @0x1325bff`) |
| `0x68` reg_load | `@0x1253579` | `@0x125372e` | `dbg_is_valid_reg_load @0x1301820` |
| `0x67` pool_buffer_load | `@0x12534e7` | `@0x1253a58` | (S4_PB validator) |
| `0x7F` dropout | `@0x123e5fb` | `@0x123e676` | `dbg_is_valid_dropout @0x12dbef0` (`cmp 0x7F @0x12dc08b`) |
| `0xE7` indirect_copy | `@0x1275d2b` | `@0x1275dab` | `dbg_is_valid_indirect_copy @0x1313fd0` (`cmp 0xE7 @0x1314141`) |
| `0xBC` range_select (V3) | `@0x135f95f` | `@0x135fb14` | `dbg_is_valid_range_select @0x13b9950` (`cmp 0xBC @0x13b9bac`) |
| `0x9B` range_select companion | `@0x135fa9a` | `@0x136097f` | (select-reduce half) |

## Gaps and confidence

- **All six op encodings — CERTAIN at byte level.** Every opcode seed, every `+0x20`/`+0x21`/`+0x22` dtype/active store, the Shuffle 32-byte lane immediate base, the Transpose `transpose_ch=1`, the reg_load `+0x24`/`+0x28`, the Dropout `threshold_imm_ptr`/`threshold_dtype`/`rng_mode`, the IndirectCopy `+0x0C..+0x14` indirect descriptor + bit-5 marker, and the RangeSelect two f32 bounds are disassembled against the named encoder bodies.
- **Wire-struct + field names — CERTAIN present / HIGH bound.** `S3D3_DROPOUT`, `S4D4_GT`, `S4D4_IC`, `S4D4_TR`, `S2D2_RS` and their `d3_dropout_*`/`gload_*`/`s_reg_load_*`/`s4d4_ic_*`/`d4_tr_*`/`s_range_select_*`/`threshold_imm_ptr`/`d3_transpose_ch` members are all live strings in `libwalrus.so` `.rodata`.
- **Dropout `+0x2B rng_mode` semantics — MEDIUM.** The store itself is pinned (`mov [r13+0x2B],al` from `*(this+0xF0)`); that the value selects an RNG *variant*, with the seed set out-of-band by `PseudoSetRngSeed`, is inferred from the generator-global read and the absence of any seed field.
- **`0x67 pool_buffer_load` / `0x9B` companion — out of scope here.** The pool-buffer-load `S4_PB` band is [2.10](pe-matmul-encoding.md); the `0x9B` select-reduce companion is not deep-transcribed (out of the named-encoder scope).

## Cross-References

- [1.11 The DVE Engine](../arch/dve-engine.md) — the table-microcoded vector engine these hand-encoded datamove ops run on; the opcode roster (`RangeSelect` 188, the `Dropout`/`Gather`/`Copy` families) and the `enum_variant_string_opcode` Rosetta.
- [2.7 Indirect-Gather Descriptors — INDIRECT16B / 20B / MXINDIRECT16B](indirect-descriptors.md) — the bit-29 (`0x20`) indirect marker and `{index, data}` ADDR4 pairing that `IndirectCopy`'s embedded `+0x0C..+0x14` descriptor and `Gather`'s index path share.
- [2.1 The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the `setupHeader` skeleton (`byte[1]=0x10`) and pxor-zero contract every opcode here inherits.
- [2.3–2.4 Tensor descriptors / ADDR4](tensor-descriptors.md) — the `+0x0C`/`+0x10`/`+0x18`/`+0x2C`/`+0x30` access-pattern sub-bands and the ADDR4 atom the indirect descriptor's `index_addr`/`data_addr` immediates resolve to.
- [2.10 PE Matmul Encoding](pe-matmul-encoding.md) — sibling per-opcode field-table page (shared lifecycle, dtype LUT, `assignAccess`) and the `0x67 pool_buffer_load` (`S4_PB`) half of the `Gather` pair.
- [Part 7 — BIR Codegen Gather / Select (I15)](../bir/) — the upstream `InstGather`/`InstStreamShuffle`/`InstRangeSelect`/`InstIndirectCopy` BIR ops that feed these encoders, and the `klr::NcNGather`/`NcLocalGather` lowerings.
- [Build & Version Provenance](../reference/versions.md) — the single pinned build all addresses are read from.
