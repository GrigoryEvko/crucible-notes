# Per-Engine Instruction-Block Assembly Pipeline

How the compiler's `def.json` per-engine program — the `<eng>.bin` 64-byte-slot
sequencer stream — becomes the loaded, device-resident sequencer image in HBM.
This is the **producer** side of the on-device-IP ↔ NEFF-IP relocation table
described in [Relocation & Weight Patches](relocation-weights.md): every patch
that page *consumes* is *constructed* here, as a side effect of laying out the
image.

All offsets, symbols, struct fields and enum values below are taken from the
host runtime `libnrt.so` (`2.31.24.0-0b044f4ce`, x86-64, ships RTTI + DWARF):
IDA-typed structs/enums (`*_structures.json` / `*_enums.json`), decompiled
function bodies (objdump over the IDA `[start,end]` ranges), and the embedded
NEFF sample carved from `.rodata` (`C08220/tar/sg00/`). The `ib_*` / `itf_*`
pipeline functions are plain C (no `_ZTV` vtable dispatch — calls are direct in
every decompiled body), so the vtable-slot rule does not apply to them.

> **Confidence tags.** `[OBS]` byte-exact from a shipped struct/enum, a
> decompiled body, or the sample. `[INF]` reasoned from observed control/data
> flow. `[CARR]` carried from a sibling page, re-grounded here.
> `HIGH/MED/LOW` = confidence.

---

## 1. The verdict up front

**The `def.json` engine program is not the loaded image.** `[HIGH/OBS]` The
compiler's per-engine `<eng>.bin` (the 64-byte-slot pseudo-stream of
[Sequencer Microcode](seq-microcode.md)) is only the **MAIN-body source**. The
runtime assembles a much larger device image *around* it by buffer
concatenation, in this fixed physical order:

```
[ PREAMBLE ( MODEL_SWITCH inside ) ][ MAIN ][ POSTAMBLE ][ FUNCTION* ]
```

PREAMBLE and POSTAMBLE are runtime-synthesized (notify / barrier / DGE /
ulib-load / reset-branch); MAIN is the pseudo-**expanded** `def.json` stream;
FUNCTION\* are extra callable functions spliced after the body. Each region is
recorded as a whole-region `INSERT` patch; the MAIN/FUNCTION bodies additionally
carry the per-slot `INSERT`/`DELETE`/`MODIFY` patches from pseudo-expansion. The
assembled buffer is DMA-staged into HBM(`TONGA_DRAM`) as one `inst_block`,
identically to a weight tensor.

The whole assembler is **`ib_create_one_block @0x2f7e50`** (1374 decompiled
lines; `instruction_block_sunda.c`). Its output is one **`ib_code_one_eng_t`**
(the *eib*) — `{code, preamble, model_switch, core}` — which a later pass
resolves into the device-side **`ib_addrs_one_eng`** used by the trace-time
relocation walk.

---

## 2. The five-stage spine `[HIGH/OBS]`

The driver is **`sequencer_setup_instr @0x4483d0`** (`sequencer_sunda.c`). It
receives `kbin->engine[]` (per-engine `kbin_engine_t`, `eng_type`
PE0/ACT1/POOL2/DVE3/SP4) and the loaded instr-sets, and produces, per engine,
one `ib_addrs_one_eng` plus a `kbin_patch_info` entry. It **asserts the last
engine is SP**:

```c
// sequencer_setup_instr_0x4483d0.c:78-79
if (kbin->engine[al_hal_tpb_get_tpb_eng_count(pcore) - 1]->eng_type != 4)  // != SP
  __assert_fail("kbin->engine[(int)al_hal_tpb_get_tpb_eng_count() - 1]->eng_type"
                " == AL_HAL_TPB_ENG_SP",
                ".../tdrv/sequencer_sunda.c", 0x239, "sequencer_setup_instr");
```

SP is the 8-byte-slot control sequencer; it is set up last.

| Stage | Entry point | What it produces |
|-------|-------------|------------------|
| 1 LOAD | `parse_one_engine_instr @0x4b7e30` → `parse_instr @0x4aefe0` | `instr_set{buffer, size}` keyed by engine name — the raw `<eng>.bin` |
| 2 IDENTIFY | `itf_identify_functions` (driver `:109`) | `itf_one_eng_functions_t` → `functions[]` / `blocks[]` |
| 3 TRANSLATE | `itf_setup_functions_one_eng @0x2798c0` → `translate_pseudo_instrs_partial_v2 @0x2763d0` | per-block `translated_pseudo_instrs` + per-block `patch_info` |
| 4 ASSEMBLE | `ib_create_eib @0x323b70` → `ib_create_eib_impl @0x2fab60` → `ib_create_one_block @0x2f7e50` | one `ib_code_one_eng_t` (eib) + the model `patch_info` + the HBM `inst_block` |
| 5 RESOLVE | `ib_fill_debug_info @0x323c90` → `ib_fill_debug_info_impl @0x2f69d0` | `ib_addrs_one_eng` (device base + code ranges) |

**Stage 1 — load.** `[HIGH/OBS]` `parse_instr` does `strcpy(key, "instr")`,
reads `<eng>.json["instr"] = "<eng>.bin"`, calls `load_bin_file(...)` for a
private copy, and emplaces `instr_set{buffer, size}` keyed by engine name. So
the `def.json` per-engine **section** is the `{dma[], instr→.bin, tables}`
triple; the `.bin` is the 64-B pseudo-stream. `src_size` = its byte length.

**Stage 2 — identify.** `[HIGH/OBS]`
```c
// sequencer_setup_instr_0x4483d0.c:109
itf_identify_functions(v24->instr, src_size, eng_type,
                       &functions_table, &num_functions_out, &num_blocks_with_collectives);
```
splits the flat `.bin` into a function/block hierarchy: `function[0]` is MAIN
(`func_id == 0`); `function[1..]` are named callable functions (`func_id != 0`).
The struct census is §3.

**Stage 4 — model patch reserve.** `[HIGH/OBS]` Before assembling, the driver
reserves the model patch list with `kbin_patch_allocate(kbin_patch_info, eng, 8)`
(driver `:175`), then calls `itf_setup_functions_one_eng` (`:182`) and
`ib_create_eib` (`:223`).

**Stage 5 — resolve.** `[HIGH/OBS]` `ib_fill_debug_info(eib_out, final_inst,
&ib_addrs[arr_idx])` (driver `:253`) converts the host eib into the
device-resolved `ib_addrs_one_eng`. On any error the driver calls
`kbin_patch_free_all(kbin_patch_info)` (`:266`) — the partial patch list is a
relocation/trace aid, not required for execution.

> **CORRECTION (vs prior notes).** `ib_fill_debug_info`'s body *is* recovered
> this pass (`ib_fill_debug_info_impl @0x2f69d0`, a 30-line function;
> `ib_fill_debug_info @0x323c90` is a thunk to it). The field copy is therefore
> `[OBS]`, not inferred — see §8.

---

## 3. The struct census — host pre-stage vs device-resolved `[HIGH/OBS]`

All field offsets are byte-exact from `libnrt.so_structures.json`; engine/patch
enums from `libnrt.so_enums.json`.

```c
ib_code_range_t {                 // 16 B  (struct size 16)
  size_t offset;                  // +0x00
  size_t size;                    // +0x08
};

ib_code_one_eng_t {               // 56 B — the EIB, ib_create_one_block output
  inst_block      *code;          // +0x00  the staged 64-B image (HBM-resident)
  ib_code_range_t  preamble;      // +0x08  PREAMBLE region {offset,size}
  ib_code_range_t  model_switch;  // +0x18  MODEL_SWITCH sub-region
  ib_code_range_t  core;          // +0x28  MAIN body region
};

ib_addrs_one_eng (.sunda) {       // 56 B — device-resolved (a union; .sunda variant)
  dmem_t          *base;              // +0x00  HBM instruction buffer
  ib_code_range_t  one_offset;        // +0x08
  ib_code_range_t  model_switch_offset;// +0x18
  ib_code_range_t  core_offset;       // +0x28  MAIN body
};
```

`inst_block` (96-B header + buffer; `ib_allocate_block` output) — every field
verified against the JSON:

```c
inst_block {
  char       name[32];     // +0x00
  size_t     buf_len;      // +0x20
  size_t     inst_len;     // +0x28  the actual image byte length
  physical_core_t *pcore;  // +0x30
  dmem_allocator_t *allocator; // +0x38
  kbin_engine_t *eng;      // +0x40
  dmem_t    *dmem;         // +0x48
  dma_addr_t start_addr;   // +0x50  = dmem->_pa + dmem->align_offset (device base)
  dmem_list_t *gc_tracker; // +0x58
  uint8_t    buf[];        // +0x60  the 64-B slots
};
```

The function/block hierarchy (Stages 2-3):

```c
itf_one_eng_functions_t { ht_t *functions; buf_t func_ctx_ptrs; };   // 32 B

itf_function_t {                          // 352 B
  char    function_name[256];   // +0
  uint32_t func_id;             // +256   0 = MAIN
  uint8_t  return_reg_lo;       // +260
  uint8_t  return_reg_hi;       // +261
  bool     reset_semaphores;    // +262
  buf_t    block_ctx_ptrs;      // +264   buf of itf_block_t*
  dma_queue_bundle_t *imcpy_queues;   // +288
  size_t   imcpy_queue_instance_idx;  // +296
  ht_node_t node;               // +304
};

itf_block_t {                             // 200 B
  uint32_t function_id;         // +0
  uint32_t block_id;            // +4
  uint32_t cc_block_id;         // +8
  size_t   kbin_begin_offset;   // +16    [start,end) byte window in the SOURCE .bin
  size_t   kbin_end_offset;     // +24
  buf_t    translated_pseudo_instrs; // +32   the EXPANDED output
  buf_t    post_process;        // +56    branch-fixup records
  kbin_patch_info_t patch_info; // +80    this block's patch rows
  ib_code_range_t block_preamble_offset; // +160 (0xA0)
  ib_code_range_t block_offset; // +176 (0xB0)  place in final_instr_buf
  bool     has_ccops;           // +192
  bool     requires_cache_align;// +193
};
```

The patch structs (full byte format and the relocation walk live in
[Relocation & Weight Patches](relocation-weights.md); reproduced here because
this page builds them):

```c
kbin_patch_location_t {           // 16 B
  uint32_t offset;                // +0   byte offset in the assembled image
  uint32_t count;                 // +4   in SLOTS (= byte_count >> 6)
  kbin_patch_type_t    type;      // +8
  kbin_patch_section_t section;   // +12
};
kbin_eng_patch_t  { int count; int array_count; kbin_patch_location_t *locations; }; // 16 B
kbin_patch_info_t { kbin_eng_patch_t eng_patch[5]; };  // 80 B (PE/ACT/POOL/DVE/SP)
```

### 3.1 The two enums these patches use `[HIGH/OBS]`

| `kbin_patch_type_t` | value | `kbin_patch_section_t` | value |
|---------------------|-------|------------------------|-------|
| `KBIN_PATCH_TYPE_INSERT` | 0 | `KBIN_PATCH_SECTION_PREAMBLE`  | 0 |
| `KBIN_PATCH_TYPE_MODIFY` | 1 | `KBIN_PATCH_SECTION_POSTAMBLE` | 1 |
| `KBIN_PATCH_TYPE_DELETE` | 2 | `KBIN_PATCH_SECTION_MAIN`      | 2 |
|  |  | `KBIN_PATCH_SECTION_FUNCATION` | 3 |

> **QUIRK.** The section-4 name is misspelled `FUNCATION` (not `FUNCTION`) in the
> shipped enum, and the C code uses that spelling literally
> (`v37 = KBIN_PATCH_SECTION_FUNCATION`). The wiki keeps the misspelling for the
> enum value; "FUNCTION" refers to the *region*.

`al_hal_tpb_eng_type_t`: `PE=0, ACT=1, POOL=2, DVE=3, SP=4`, `MAX_ENG=5` (same
for Sunda/Cayman/Mariana). `[OBS]`

### 3.2 Where these live in `model_t` `[HIGH/OBS]`

```
model_t @ +0x18A0 (6304)  kbin_patch_info_t kbin_patch_info;     // 80 B, eng_patch[5]
model_t @ +0x1988 (6536)  ib_addrs_one_eng_t *ib_addrs[5];       // 40 B, per-engine ptrs
model_t @ +0x19B0 (6576)  ib_code_range_t     ib_functions[5];   // 80 B, FUNCTION ranges
```

These three offsets are the base arithmetic the trace-time relocation reader
walks (`mod + (eng+0x18A)*16` and `mod + eng*8 + 0x1988`) — see
[Relocation & Weight Patches](relocation-weights.md). The region↔range map is
1:1: `PREAMBLE↔one_offset/preamble`, `MODEL_SWITCH↔model_switch_offset/
model_switch`, `MAIN↔core_offset/core`, `FUNCTION↔ib_functions[eng]`.

---

## 4. Stage 3 — `itf_setup_functions_one_eng @0x2798c0` `[HIGH/OBS]`

Drives the per-block pseudo expansion. For each function, then each block, it
computes the source window, picks the patch **section** from `func_id`, and
calls the per-slot expander.

```c
// itf_setup_functions_one_eng_0x2798c0.c  (annotated)
for (func_id = 0; func_id < num_functions; ++func_id) {
  itf_function_t *fn = functions_table->func_ctx_ptrs.buf[func_id];   // :74
  // ... imcpy vring set-up for func_id != 0 ...
  for (each block in fn->block_ctx_ptrs /*+264*/) {                   // :147
    itf_block_t *blk = *block_ptr;
    size_t fn_start_offset    = function[0_of_fn].kbin_begin_offset;  // :153  (*v32)
    size_t src_original_start = blk->kbin_begin_offset;               // :155
    size_t src_size_bytes     = blk->kbin_end_offset - blk->kbin_begin_offset; // :152

    kbin_patch_section_t section = KBIN_PATCH_SECTION_FUNCATION;      // :166
    if (!fn->func_id /*+256*/) section = KBIN_PATCH_SECTION_MAIN;     // :167-168

    translate_pseudo_instrs_partial_v2(
        pcore, src_buf, fn_start_offset, src_original_start, src_size_bytes,
        &blk->translated_pseudo_instrs,  // OUT: expanded slots                :175
        qset, fn, blk, ...,
        &blk->patch_info,                // OUT: this block's patch rows        :189
        section, eng, ...);                                          // :169-201
  }
}
```

`kbin_begin_offset` is the **source** byte; `translated_pseudo_instrs.size_used`
is the **expanded** byte — this is where 1→N expansion changes the slot count.
The assert at `:73` (`instr_tpb_functions.c:0x1F5`) guards `func_id *
sizeof(itf_function_t*) < func_ctx_ptrs.size_used`.

---

## 5. Stage 3 core — `translate_pseudo_instrs_partial_v2 @0x2763d0` `[HIGH/OBS]`

Turns ONE source `.bin` slot into N device slots and emits the per-slot patch
records. This is the only producer of the per-slot `INSERT`/`DELETE` records.
The loop is byte-exact:

```c
// translate_pseudo_instrs_partial_v2_0x2763d0.c:84-157  (annotated)
v34 = src_original_start;            // byte cursor into the .bin
v37 = v34 - fn_start_offset;         // function-relative byte
while (v34 < src_original_start + src_original_size) {
  num_instructions_inserted = 0;
  size_used = src_translated->size_used;          // translated offset BEFORE emit  :92

  translate_one_pseudo_instr_v2(
      &src[v34], buf_end, v37 >> 6 /*fn slot idx*/, eng, ...,
      src_translated, &num_instructions_inserted, ...,
      v34 >> 6 /*src slot idx*/, ...);            // emits the REAL device slots    :93

  if (num_instructions_inserted > 1)              // 1 -> N>1                       :132-134
    kbin_patch_append(patch_info, eng, size_used,
        (num_instructions_inserted - 1) << 6,     // record N-1 INSERTed slots
        KBIN_PATCH_TYPE_INSERT, patch_section);
  else if (num_instructions_inserted == 0)        // 1 -> 0 (pure pseudo)           :149-151
    kbin_patch_append(patch_info, eng, size_used,
        0x40, KBIN_PATCH_TYPE_DELETE, patch_section);  // record 1 DELETEd slot
  // else (== 1): 1->1 passthrough, NO patch record (the common case)

  v34 += 64; v37 += 64;                            // advance one 64-B source slot
}
```

The `0x40` / `<< 6` stride throughout confirms 64-byte slots (matching
[Sequencer Microcode](seq-microcode.md)). On append failure the function only
**warns** (`"Failed to add patch insert/delete info. Debug info may be
inaccurate"`, `:139` / `:156`) and continues — the patch list is a
relocation/trace aid, not an execution requirement.

> **NOTE.** `kbin_patch_append @0x2fb0f0` writes the 16-byte location as a single
> SSE store `{offset, byte_count>>6, type, section}` — i.e. it stores `count` in
> **slots**, dividing the byte count by 64 at insert time. It returns
> `NRT_SUCCESS` immediately if `byte_count == 0`, and doubles `array_count` on
> overflow.

**Worked (sample `pe.bin`).** `[HIGH/INF expansion shape, OBS structure]` The
single source slot is `PSEUDO_TRIGGER_COLLECTIVE` (opcode `0xC8`,
`pe.asm`: *"`$S[10]>0 $S[22]++@complete ctype=ALL_REDUCE input_tensor_id=3
output_tensor_id=4 num_elements=32 dtype=fp32 op=ADD group_id=0`"*).
`translate_one_pseudo_instr_v2` expands it into the concrete TOP-SP collective
micro-program (the `$S[10]` wait, the replica-group CC-DMA triggers, the
`$S[22]++`-on-complete post-increment) — N>1 device slots — so this pass emits
one `INSERT(MAIN, count=N-1)`. The expansion factor N is queue/data-dependent and
not present in the static `.bin`; only the `INSERT`/`DELETE` **structure** is
`[OBS]`.

---

## 6. Stage 4 body — `ib_create_one_block @0x2f7e50` `[HIGH/OBS]`

### 6.1 Buffer setup `[HIGH/OBS]`

```c
// ib_create_one_block_0x2f7e50.c
buf_init(&process_req_preamble, 0x2000);              // :293  exec-request scratch
buf_init(&final_instr_buf, src_size + 65984);         // :305-307  THE OUTPUT IMAGE (+0x10100)
buf_init(&seq_config_buf, 0x200);                     // :316  seq-config sub-buffer
kbin_patch_allocate(&src_buf_patch,    eng, 0x400);   // :325  MAIN block patches (1024)
kbin_patch_allocate(&src_fn_buf_patch, eng, 0x400);   // :337  FUNCTION block patches
buf_init(&post_process, 8);                           // :1173 global branch-fixup offsets
```

The output image is `src_size + 0x10100` — the source body plus fixed headroom
for preamble/postamble/function expansion. The `0x400`/1024-slot per-engine
patch reserve matches the patch-table sizing in
[Relocation & Weight Patches](relocation-weights.md).

### 6.2 The fixed region append sequence `[HIGH/OBS]`

The image is built into `final_instr_buf` in exactly this order. Each region's
boundary is bracketed by a patch call.

**(A) PREAMBLE** — one whole-region `INSERT`. The MODEL_SWITCH sub-region is
recorded inside it. The builder calls (all present in `ib_create_one_block`):

| line | call | role |
|------|------|------|
| `:346` | `ib_add_inference_wait_v2` | the per-inference wait gate |
| `:356-357` | `model_switch_block = {final_instr_buf.size_used, ...}` | record MODEL_SWITCH start |
| `:367` | `insert_engine_explicit_notify(NOTIFY_HINT_SWITCH_START)` | switch begin |
| `:370,378` | `add_set_ordering_mode` / `psqs_insert_model_switch_preamble` | per-set queue swap |
| `:387,409` | `psqs_swap_single_queue` | imcpy / dynamic-PWP vring init |
| `:434-444` | `add_ldr32_addr` / `add_cmpeq_imm_br_rel` / `add_dma_tail_inc` / `add_br_rel` | the seq\_config compare/branch |
| `:513,642` | `insert_dge_config_v2` | SW/HW dynamic-DMA generation-engine config (POOL=2 programs the DGE DMA-mapping CSRs, `:607`) |
| `:683-708` | `insert_model_switch_program_xt_feature_flags_v3` | `seq_feature_flag` bitmap (dve bg-transpose, dge\_notif, evt\_accel carveout) |
| `:743-764` | `insert_background_transpose_enable` | PE |
| `:781,791` | `add_ldr32_addr(+44)` / `insert_set_fp8_conv_config` | seq\_config body + fp8 conv |
| `:921-934` | `insert_pool_ulib_config_v2` | POOL=2: program the Q7 ucode swap table |
| `:942` | `ucode_ll_get_load_sequence` | POOL=2: emit the Q7-library IMEM LOAD SEQUENCE into the image |
| `:460,975` | `insert_collectives_init` | SP=4: per-TOPSP WR32 init |
| `:1076-1099` | `ib_insert_collectives_start_v2` | SP=4: per-stream TOPSP-trigger WR32 |
| `:1114-1116` | `ib_insert_ioq_switch_v2` | PE=0: dma\_tail\_inc on exec-desc-q + ev\_wait |
| `:1117-1119` | `add_args_table_swap` | if `ptr_table_count==1` and (all-engine-swap supported or POOL=2) |
| `:1137-1148` | `ib_add_vnc_all_engine_barrier` / `pcb_insert_rt_barrier` | if `shared_info->num_sg > 1` |

```c
// :1169  the entire preamble is ONE whole-region INSERT at translated offset 0
kbin_patch_append(patch_info, eng, 0, final_instr_buf.size_used,
                  KBIN_PATCH_TYPE_INSERT, KBIN_PATCH_SECTION_PREAMBLE);
```

> **GOTCHA.** The POOL preamble alone INSERTs the GPSIMD Q7-library **IMEM LOAD
> SEQUENCE** (`ucode_ll_get_load_sequence`, `:942`) — the bridge that gets the
> custom-op micro-library into device IMEM before the
> [Kernel-Info Table](../firmware/pool/kernel-info-table.md) dispatch can call
> it. That is why a POOL image is materially larger than its `pool.bin`.

**(B) MAIN body** — the expanded `def.json` stream; `INSERT`/`DELETE`/`MODIFY`.

```c
// :1181  append function[0] (MAIN); section base = 0
ib_add_one_function_to_final_buf(pcore, eng, func_ctxs[0], functions_table,
    cc_ctx, ptr_mem_ref_set, &final_instr_buf, &post_process,
    src_buf_patch /*=src*/, 0 /*function_section_offset*/);
// :1185  splice MAIN block-level patch list into the model patch_info
kbin_patch_extend(src_buf_patch, patch_info, eng);
```

**(C) POSTAMBLE** — one whole-region `INSERT`.

```c
offset_0 = final_instr_buf.size_used;                    // :1188  postamble start
ib_insert_common_postamble(&final_instr_buf, eng, qset, pcb_sems, pcore);  // :1189  (§7)
// :1191  the EXECUTION-RESET branch back to the inference-wait gate
add_br_rel(scratch, 0, program_start_offset - final_instr_buf.size_used, 0);
// :1202
kbin_patch_append(patch_info, eng, offset_0,
                  final_instr_buf.size_used - offset_0,
                  KBIN_PATCH_TYPE_INSERT, KBIN_PATCH_SECTION_POSTAMBLE);
```

**(D) FUNCTION\*** — extra callable functions; `INSERT`/`MODIFY`/`DELETE` ×
`FUNCATION`.

```c
function_section_offset = final_instr_buf.size_used;     // :1205  where functions begin
for (j = 1; j < num_functions; ++j) {                    // :1210
  assert(func_ctxs[j] != NULL);                          // :1213-1214 (sunda.c:0x766)
  ib_add_one_function_to_final_buf(pcore, eng, func_ctxs[j], functions_table,
      cc_ctx, ptr_mem_ref_set, &final_instr_buf, &post_process,
      src_fn_buf_patch, function_section_offset);        // :1215
}
// ... branch-fixup post-process pass (§6.3) ...
ib_functions[eng] = { function_section_offset,
                      final_instr_buf.size_used - function_section_offset }; // :1340
kbin_patch_extend(src_fn_buf_patch, patch_info, eng);    // :1329
```

> **NOTE.** `kbin_patch_extend @0x2fb240` splices a per-engine `kbin_eng_patch_t`
> from `src`→`dst`: it `next_power_of_two`-grows the destination, `memcpy`s the
> source `count` locations after the destination's existing ones, and asserts
> `dst.count <= dst.array_count` (`kbin_patch.c:0x6E`). MAIN block patches come
> through `src` (`:1185`); FUNCTION patches through `src_fn_buf_patch` (`:1329`).

### 6.3 The branch-fixup post-process pass — the MODIFY producer `[HIGH/OBS]`

After the FUNCTION loop, `ib_create_one_block` walks `post_process` (8-byte
final-buffer offsets recorded by `ib_process_special_instr`, §7.3) and resolves
the compiler's `0xD3`/`0xDF` call/exit placeholders into concrete relative
branches — an **in-place 1→1 rewrite** (operand/address relocated, slot count
unchanged), which is exactly the `MODIFY` semantics consumed in
[Relocation & Weight Patches](relocation-weights.md):

```c
// ib_create_one_block_0x2f7e50.c:1225-1326  (annotated)
for (size_t k = 0; k < post_process.size_used; k += 8) {
  uint64_t O = *(uint64_t *)&post_process.buf[k];        // slot's final-buffer offset
  uint8_t op = final_instr_buf.buf[O];                   // :1238  the opcode byte

  if (op == 0xD3) {                                      // PSEUDO_FUNCTION_CALL
    name  = &buf[O + 12];                                // 32-B function name
    blk   = ht_name_find(functions_table->functions, name);   // :1251
    target = *(size_t *)((char*)blk + 176);              // :1267  block_offset.offset
    add_br_rel(slot, 0, target - O, events);             // :1286  call -> relative branch
  } else if (op == 0xDF && buf[O+12] == 1) {             // exit / function return
    add_br_rel(slot, 0, offset_0 - O, &slot.events);     // :1311  v227[0] == offset_0
    BYTE3(slot[0]) |= buf[O + 3];                        // :1312  OR the debug/hint bit
  }
  // store the rewritten 64-B slot back, 4 x 16-B SSE  :1316-1319
  *(__m128i*)&final_instr_buf.buf[O+ 0] = rewritten[0];
  *(__m128i*)&final_instr_buf.buf[O+16] = rewritten[1];
  *(__m128i*)&final_instr_buf.buf[O+32] = rewritten[2];
  *(__m128i*)&final_instr_buf.buf[O+48] = rewritten[3];
}
```

The `0xD3` call resolves to the called function's `block_offset.offset`
(`itf_block_t +176`); the `0xDF` exit resolves to `offset_0` (the POSTAMBLE
start, set at `:1201` `v227[0] = offset_0`). The store-back is four 16-byte SSE
moves = one 64-B slot, confirming the slot granularity.

> **CORRECTION (vs prior notes).** The `0xDF` exit branch targets `offset_0`
> (POSTAMBLE start), **not** the FUNCTION-section base. `v227[0]` is loaded with
> `offset_0` at `:1201` and read back at `:1311`.

### 6.4 Stage + fill the eib `[HIGH/OBS]`

```c
// ib_create_one_block_0x2f7e50.c:1338-1363
size_used = final_instr_buf.size_used;
block = ib_allocate_block(pcore, allocator, "One block", eng, size_used, tracker); // :1341
memcpy(block->buf, final_instr_buf.buf, size_used);    // :1349
block->inst_len = size_used;                           // :1350
ib_transfer_block_to_tdram(block, eng->eng_type);      // :1351  HBM stage DMA (§8)

eib->model_switch.offset = model_switch_block.offset;  // :1358
eib->code                = block;                      // :1359
eib->model_switch.size   = program_start_offset - model_switch_block.offset; // :1360
eib->core.offset         = offset;                     // :1362  PREAMBLE end == MAIN start
eib->core.size           = offset_0 - offset;          // :1363  MAIN size
// ib_functions[eng] was written at :1340 (FUNCTION range)
```

`ib_allocate_block @0x27aca0` is a `calloc(buf_size + 96, 1)` of the
96-byte-header `inst_block`, then `strncpy(name, "One block", 0x1F)` and fills
`pcore/allocator/eng/buf_len/gc_tracker`. On any failure path,
`ib_create_one_block` tears down both patch lists (`kbin_patch_free(src,
fn)`), all buffers, and the block — the patch list is a debug/relocation aid,
not required for execution.

The wrapper `ib_create_eib_impl @0x2fab60` `calloc`s the 56-B
`ib_code_one_eng_t`, calls `ib_create_one_block` to fill it, and on error calls
`ib_free_eib`; otherwise `*eib_out = eib`.

---

## 7. Stage 3 sub — `ib_add_one_function_to_final_buf @0x27c4f0` `[HIGH/OBS]`

Appends one function's translated blocks and carries their patch records. Called
for `function[0]` (MAIN) and each `function[j>0]` (FUNCTION).

### 7.1 Section select and patch rebase `[HIGH/OBS]`

```c
// ib_add_one_function_to_final_buf_0x27c4f0.c:123-129
func_id = func_ctx->func_id;
section = 3 - (func_id == 0);             // :125  func_id!=0 -> FUNCATION(3), ==0 -> MAIN(2)
v85 = func_id ? function_section_offset : 0;  // :127-128  patch-offset rebase base
```

### 7.2 Per-block copy + patch carry `[HIGH/OBS]`

For each block:

- **Empty `translated_pseudo_instrs`** (`:139`): carry the block's pre-built
  patch rows **verbatim** —
  ```c
  // :147
  kbin_patch_append(src_buf_patch, eng,
      final_instr_buf->size_used - v85,
      v69[1] << 6,                       // count slots
      (kbin_patch_type_t)   v69[2],      // TYPE  copied from the block's row
      (kbin_patch_section_t)v69[3]);     // SECTION copied from the block's row
  ```
  **This is where `MODIFY(1)` records survive.** `translate_pseudo_instrs` only
  emits `INSERT`/`DELETE`; `MODIFY` rows are produced upstream and copied through
  here with their original `type`/`section`.

- **Non-empty block**: optional cache-align padding
  (`ipb_br_label_cache_align_target` if `requires_cache_align`, `:181` — pads to
  a cache-block boundary, contributing extra `INSERT` slots into the running
  delta), then `ib_process_special_instr` on the block's **first** slot
  (`offset 0`, `:203`) and **last** slot (`last_instr_start_offset = size - 64`,
  `:239`/`:332`), accumulating `num_extra_instrs`. The block middle is bulk-copied:
  ```c
  // :218 / :404  first and last slots handled specially, middle copied bulk
  buf_append(final_instr_buf, translated_pseudo_instrs.buf + 64, size - 128);
  ```

- **The block's net slot delta is recorded by SIGN**:
  ```c
  // :294-308  (and the alternate path :379-388)
  v54 = func_id ? (block_start - function_section_offset) : block_start;
  type = KBIN_PATCH_TYPE_INSERT;
  if (num_extra_instrs < 0) { count = -num_extra_instrs; type = KBIN_PATCH_TYPE_DELETE; }
  else                        count =  num_extra_instrs;
  kbin_patch_append(src_buf_patch, eng, v54, count << 6, type, section);
  ```
  The block's pre-existing per-slot patches are also merged in offset order
  (`:265-291`), each carried with its own `(type=v49[2], section=v49[3])`.

- `block_offset` (+176) and `block_preamble_offset` (+160) are filled with the
  block's place in `final_instr_buf` (`:172`/`:197`/`:317`/`:320`) — the
  `ib_code_range` the §6.3 `0xD3` call fixup reads.

Finally `ipb_postprocess_instrs(&branch_md, buf, func_section_start, size)`
(`:423`) fixes branch targets across the function and, **for `func_id != 0`**,
sets the function-entry slot's `+3` byte high bit and a stride-128 OR pass
(`:439-452`) — the call/return marker the §6.3 pass keys on:
```c
// :432-455
if (func_id) {
  buf[func_section_start + 3] |= 1u;     // :439  function-entry marker
  for (o = func_section_start + 64; o < size; o += 128) {
    buf[o + 3]  |= 1u;                    // :448
    buf[o + 67] |= 1u;                    // :449
  }
}
```

### 7.3 `ib_process_special_instr @0x27c240` — the special-opcode handler `[HIGH/OBS]`

Reads the **translated-stream** pseudo opcode (byte 0 — distinct from the device
ISA) and dispatches:

| opcode | name | behaviour |
|--------|------|-----------|
| `0xD3` | `PSEUDO_FUNCTION_CALL` | `itf_translate_function_call_instr` expands it, `instr_added → num_extra_instrs` (an `INSERT`); records the slot's final-buffer offset into `global_post_process` (for the §6.3 fixup); appends the original 64-B slot as placeholder. `:32-44` |
| `0xA9` | branch / TensorLoad | if a post-process record matches `src_buf_offset`, track via `ipb_set_br_by_section_neff_pc`; else copy. `:48-63` |
| `0xCC` | label | `ipb_set_br_label_by_label_id(..., block->block_preamble_offset.offset)`; `num_extra_instrs = -1` — a `DELETE`: the label slot drops out of the image. `:65-71` |
| `0xDD` | `PSEUDO_BRANCH_PREFETCH_HINT` | `ipb_add_br_hint_offset` (track the hint). `:75-87` |
| `0xDF` | exit / return (`byte[12] == 1`) | record the slot's offset into `global_post_process` (for the §6.3 exit-branch fixup). `:89-103` |
| else | — | `buf_append` the 64-B slot unchanged (1→1 passthrough). |

> **NOTE.** `0xCC` (label) is the only `DELETE` this handler produces; `0xD3` is
> the `INSERT` source for function calls; `0xD3`/`0xDF` are the `MODIFY` sources
> resolved in §6.3 once all section offsets are known.

---

## 8. Stage 4 tail — the HBM(TDRAM) stage + device resolve `[HIGH/OBS]`

`ib_transfer_block_to_tdram @0x27ad80` DMA-stages the assembled image into HBM
exactly like a weight tensor:

```c
// ib_transfer_block_to_tdram_0x27ad80.c:15-44  (annotated)
const nrt_global_config_t *gc = nrt_gconf();
size_t align = max(0x8000, gc->seq_instr_block_size[eng_type]);  // nrt_global_config_t +120
assert((align & (align - 1)) == 0);            // :22  power-of-two (common.c:0x4C)
assert(align % gc->seq_instr_block_size[eng_type] == 0);         // :24  (common.c:0x4D)

uint32_t hbm = get_default_hbm_index(ib->pcore->device_tpb_idx); // :26
assert(eng_type <= 4);                          // :27 (ib_get_engine_usage_type, common.c:0x37)
dmem_alloc_aligned(ib->allocator, &ib->dmem, ib->inst_len,
                   TONGA_DRAM, hbm, ib->gc_tracker,
                   dword_9DAB80[eng_type] /* per-eng usage tag */, NULL, align); // :29
ib->start_addr = ib->dmem->_pa + ib->dmem->align_offset;         // :39  device base
dmem_buf_copyin(ib->dmem, ib->buf, 0, ib->inst_len);             // :40  the DMA
```

`seq_instr_block_size` is `uint64_t[5]` (per-engine) at `nrt_global_config_t
+120`. The `≥0x8000`, power-of-two alignment is the sequencer
instruction-block granularity, so the SEQ front-end can block-fetch the stream;
`dword_9DAB80[eng_type]` is the per-engine usage tag. `dmem_buf_copyin →
dmem_device_copy → ndl_memory_copy_as` is the same async copy-buffer DMA used for
weight tensors. The per-engine sequencer **image** lands in HBM(`TONGA_DRAM`),
addressed by `block->start_addr` (= `ib_addrs_one_eng.base->_pa +
align_offset`); the engine's hardware sequencer fetches its program from there.

### 8.1 The POSTAMBLE builder — `ib_insert_common_postamble @0x27b190` `[HIGH/OBS]`

Builds the per-inference reset/notify tail into a 64 KB stack scratch:

```c
// ib_insert_common_postamble_0x27b190.c  (annotated)
if (tdrv_arch_instr_block_supports_dma_indirect1d_bound_check() && eng_type == 2) // POOL
  off = insert_dma_indirect1d_bound_check_instrs_v2(scratch, 0, pcore);    // :33
off = add_sync_barrier(eng_type, scratch, off, 0, 1);                      // :38
off = add_sema_reset(eng_type, scratch, off, pcb_sems);                    // :39  reset pcb sems
off = add_sync_barrier(eng_type, scratch, off, 0, 0);                      // :40
for (each model-type / dynamic-act-table ring in qset)                     // :44-82
  off = add_dma_bundle_rearm(scratch, off, ring, eng_type);                // re-arm next inference
off = add_notify(scratch, off, 0, 3);                                      // :84  execution-DONE notify
assert(off <= 0x10000);                                                    // :85 (common.c:0x148)
buf_append(ibuf, scratch, off);                                            // :87
```

The `add_br_rel` after it (`ib_create_one_block:1191`) loops the sequencer back
to the inference-wait gate.

### 8.2 Device resolve — `ib_fill_debug_info_impl @0x2f69d0` `[HIGH/OBS]`

```c
// ib_fill_debug_info_impl_0x2f69d0.c:19-29  (annotated)
ib_addrs_one_eng_t *a = calloc(0x38, 1);           // 56-B device struct
*ib_addrs = a;
buf_append(final_inst, eib->code->buf, eib->code->inst_len);   // accumulate the image
a->sunda.base               = eib->code->dmem;                 // :25  HBM dmem
a->sunda.one_offset         = <from eib->code->inst_len pair>; // :26  _mm_loadh_ps(&inst_len)
a->sunda.model_switch_offset = eib->model_switch;              // :27
a->sunda.core_offset        = eib->core;                       // :28
```

`model_switch_offset` and `core_offset` are copied straight from the eib;
`base` is the inst\_block's `dmem`.

> **CORRECTION (vs prior notes).** `one_offset` is **not** a copy of
> `eib->preamble`. The recovered body builds it from the `inst_block`'s
> `inst_len` neighbourhood (`_mm_loadh_ps(&code->inst_len)`, `:26`), i.e. it is
> derived during resolve rather than carried from the eib's PREAMBLE range.
> `model_switch_offset`/`core_offset` are direct eib copies (`:27-28`).

---

## 9. End-to-end — the `sg00` sample, assembled `[HIGH]`

Ground truth carved from `C08220/tar/sg00/`. `def.json` (`version "0.6-"`)
names per-engine JSONs `pe→"pe.json"`, `pool→"pool.json"`, plus `act`/`dve`/`sp`;
`dma_queue = { q_gradient_in {owner pool, sem 10, in}, q_gradient_out {owner
pool, sem 11, out} }`; `var = { input(var_id 0, 32×fp32), SB(1), output(2),
gradient_in(3), gradient_out(4) }`; `replica_groups = [[]]` (one trivial group).
`act.bin`/`dve.bin`/`sp.bin` are 0 bytes (empty programs). `[OBS]`

**PE engine (eng_type 0).** `[OBS]` `pe.json = {"dma":[], "instr":"pe.bin",
"name":"pe_array_json"}`. `pe.bin` is 64 bytes — one slot, `byte0 = 0xC8`
(`PSEUDO_TRIGGER_COLLECTIVE`).

1. `parse_one_engine_instr`/`parse_instr` loads `pe.bin` into `instr_set`.
2. `itf_identify_functions`: one function (`func_id 0`), one block,
   `kbin_begin=0`, `kbin_end=64`.
3. `itf_setup_functions_one_eng` (`func_id 0` → MAIN) →
   `translate_pseudo_instrs_partial_v2` expands the single `0xC8` slot into the
   collective micro-program (N>1 slots) → emits `INSERT(MAIN, count=N-1)`.
4. `ib_create_one_block`:
   - (A) PREAMBLE (for PE: background-transpose enable, ioq-switch, args-table-swap)
     → `kbin_patch_append(PE, 0, |preamble|, INSERT, PREAMBLE)`.
   - (B) `ib_add_one_function_to_final_buf(function[0])` appends the N collective
     slots → `kbin_patch_extend` splices the MAIN `INSERT`.
   - (C) `ib_insert_common_postamble` + reset branch →
     `kbin_patch_append(PE, offset_0, |postamble|, INSERT, POSTAMBLE)`.
   - (D) no extra functions (`num_functions==1`) → `ib_functions[PE] = {end, 0}`.
   - Stage: `ib_allocate_block` + `ib_transfer_block_to_tdram` → the PE image in HBM.
   - `eib_PE = { code, model_switch={mso, ...}, core={preamble_end, |MAIN|} }`.
5. `ib_fill_debug_info` → `ib_addrs[PE] = { base=dmem, one_offset,
   model_switch_offset, core_offset=MAIN }`.

**Trace time** (consumed in [Relocation & Weight Patches](relocation-weights.md)):
a fault inside the expanded collective at device PC X within
`[base + core_offset.offset, +size-0x40]` resolves through
`kbin_patch_device_ip_to_neff_ip → get_neff_ip`; the `INSERT(MAIN, count=N-1)`
entry makes the walk **skip** the N-1 synthetic slots, mapping X back to NEFF IP
"1" — the single `PSEUDO_TRIGGER_COLLECTIVE` line in `pe.asm`.

**POOL engine (eng_type 2).** `[OBS structure, INF expansion shape]`
`pool.bin` is 192 bytes — three slots: `byte0 = 0xC1` (DMA-trigger), `0xC1`,
`0xA0` (event-semaphore), matching `pool.asm`:
*"`PSEUDO_DMA_TRIGGER q_gradient_in`"*,
*"`PSEUDO_DMA_TRIGGER $S[22]>0 q_gradient_out`"*, *"`EVENT_SEMAPHORE $S[11]>0`"*.
Each `0xC1` expands into the WRITE to the resolved DMA tail-pointer for
`q_gradient_in`/`q_gradient_out` (resolved from the queue **name** to the model's
allocated tail-pointer); the `0xA0` is a 1→1 passthrough (no patch). The POOL
preamble additionally runs `insert_pool_ulib_config_v2` + `ucode_ll_get_load_
sequence` — the Q7-library IMEM LOAD SEQUENCE is `INSERT`ed into the POOL image,
the bridge to the [Kernel-Info Table](../firmware/pool/kernel-info-table.md)
dispatch. A custom-op (`0xCA`/`EXTENDED_INST`) source slot would expand to a
`[dma_config][LOAD_POOL_ARGUMENT][DRAIN][EVENT_SEMAPHORE]` micro-program — an
`INSERT(POOL, count≥5)` whose inserted slots the `get_neff_ip` POOL walk skips
back to the single source line.

The whole 5-engine `def.json` → **5 `ib_code_one_eng_t`/`ib_addrs_one_eng` + one
`kbin_patch_info` (5 `eng_patch` lists) → 5 HBM-resident `inst_block`s.** SP
(eng 4) is assembled **last** (driver assert §2); its 8-B-slot sync program
(empty in this sample) is built by the same spine minus the 64-B-engine
specifics.

---

## 10. The complete pipeline, one diagram `[HIGH]`

```
NEFF tar sgNN/def.json
  | kelf_load_from_neff, per engine PE/ACT/POOL/DVE/SP
  v
<eng>.json  {dma[], "instr":"<eng>.bin", tables}
  | parse_one_engine_instr -> parse_instr -> load_bin_file
  v
instr_set { uint8_t* buffer /*the 64-B-slot pseudo .bin*/, uint32_t size }
  | itf_identify_functions
  v
itf_one_eng_functions_t { functions[], func_ctx_ptrs[] }   (func_id, blocks)
  | itf_setup_functions_one_eng   (per function -> section MAIN(2)/FUNCATION(3))
  |   translate_pseudo_instrs_partial_v2   (per block, per 64-B src slot)
  |     translate_one_pseudo_instr_v2 -> N device slots
  |     kbin_patch_append: 1->N = INSERT(N-1), 1->0 = DELETE(1)   [block->patch_info]
  v
per-block { translated_pseudo_instrs, post_process, patch_info, block_offset }
  | ib_create_eib -> ib_create_one_block            (assemble the image)
  |   (A) PREAMBLE (+ MODEL_SWITCH) ; kbin_patch_append(INSERT, PREAMBLE)  :1169
  |   (B) ib_add_one_function_to_final_buf(fn0)=MAIN ; kbin_patch_extend   :1185
  |       (carries block patches verbatim incl MODIFY; INSERT/DELETE by sign)
  |   (C) ib_insert_common_postamble + reset br ; kbin_patch_append(INSERT, POSTAMBLE) :1202
  |   (D) ib_add_one_function_to_final_buf(fn1..)=FUNCATION ; kbin_patch_extend :1329
  |       post-process: 0xD3 call -> br_rel(fn entry), 0xDF exit -> br_rel  [MODIFY] :1239
  |   ib_functions[eng] = {function_section_offset, size}                  :1340
  |   ib_allocate_block + memcpy + ib_transfer_block_to_tdram (HBM stage DMA) :1351
  v
ib_code_one_eng_t (eib) { code=inst_block, preamble, model_switch, core }
  | ib_fill_debug_info  (resolve device base + ranges)
  v
ib_addrs_one_eng { base=dmem, one_offset, model_switch_offset, core_offset }
  + model_t.ib_functions[eng] {offset,size}                       @ +0x19B0
  + model_t.kbin_patch_info.eng_patch[eng]  (INSERT/MODIFY/DELETE x PRE/POST/MAIN/FN) @ +0x18A0
  | (run time) engine HW sequencer fetches from base->_pa + align_offset
  | (trace time) kbin_patch_device_ip_to_neff_ip -> get_neff_ip
  v
device PC  <->  NEFF IP (the .asm source line)
```

---

## 11. Key design points

1. The `def.json` per-engine `.bin` is **only** the MAIN body. The runtime
   synthesizes PREAMBLE (+ MODEL_SWITCH) and POSTAMBLE around it and splices
   FUNCTION\* after it, by buffer concatenation in `ib_create_one_block`.
2. The patch list is a **side effect of layout**: each region append is
   bracketed by `kbin_patch_append` (whole-region `INSERT`) / `kbin_patch_extend`
   (splice block patches). Per-slot `INSERT`/`DELETE` come from
   `translate_pseudo_instrs` (1→N / 1→0); `MODIFY` is the in-place `0xD3`/`0xDF`
   call/exit branch fixup plus the verbatim block-level carry-through. Byte-format
   and consumption: [Relocation & Weight Patches](relocation-weights.md).
3. The assembled image stages to HBM(`TONGA_DRAM`) via `dmem_alloc_aligned` +
   `dmem_buf_copyin` — the same DMA path as weight tensors — aligned to
   `seq_instr_block_size` (≥0x8000, power-of-two). Device base =
   `dmem._pa + align_offset` = `ib_addrs_one_eng.base`.
4. The struct outputs: host `ib_code_one_eng_t` (56 B,
   `{code, preamble, model_switch, core}`) → device `ib_addrs_one_eng` (56 B,
   `{base, one_offset, model_switch_offset, core_offset}`) + `ib_functions[eng]`
   (FUNCTION range) + `kbin_patch_info.eng_patch[eng]`. The `model_t` offsets
   `0x18A0`/`0x1988`/`0x19B0` are byte-verified.
5. SP (eng 4) is assembled **last** (driver assert); it is the 8-B-slot sync
   sequencer ([Sequencer Microcode](seq-microcode.md)), built by the same spine.
6. POOL (eng 2) additionally INSERTs the Q7-library LOAD SEQUENCE
   (`ucode_ll_get_load_sequence`) + the ulib swap-table config — the GPSIMD IMEM
   bridge to the [Kernel-Info Table](../firmware/pool/kernel-info-table.md)
   dispatch.

## 12. Open items

- `translate_one_pseudo_instr_v2`'s full per-pseudo expansion table (how many
  device slots each RT-class pseudo yields) is queue/data-dependent and not in
  the static `.bin`; only the `INSERT`/`DELETE` **structure** is `[OBS]`, the
  count N is runtime. `[LOW]`
- The DGE / feature-flag / legacy-ordering / bg-transpose preamble sub-steps are
  arch-gated (`tdrv_arch_instr_block_supports_*`); the exact arch matrix
  (Sunda / Cayman / Mariana) per step is observable in the branch guards but not
  exhaustively cross-tabulated here. `[LOW]`
- `itf_translate_function_call_instr` / `ipb_postprocess_instrs` internal branch
  encoding (the exact `add_br_rel` event/offset packing) is `[OBS]` at the call
  sites; the slot-level branch byte layout is documented in
  [Sequencer Microcode](seq-microcode.md), not re-derived here. `[LOW]`
- The MODEL_SWITCH (`model_switch_offset`) range is built here, but its
  execution-side trigger (fast model reconfigure) is not exercised in the trace
  path. `[CARR]`

---

*Container byte format that holds the `def.json`/`<eng>.bin` triple:
[Container Byte Format](container-byte-format.md). The 64-byte words this page
emits: [Sequencer Microcode](seq-microcode.md). The patches it constructs,
consumed at trace time: [Relocation & Weight Patches](relocation-weights.md).*
