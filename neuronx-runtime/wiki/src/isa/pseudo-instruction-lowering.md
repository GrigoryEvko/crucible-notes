# Pseudo-Instruction Lowering (SP / TopSP Micro-Op Builders)

> *All addresses, offsets, struct ordinals, opcode words, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce` (runtime-lib **2.31.24.0-0b044f4ce**; `libnrt.so.2.31.24.0`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). `.text` VMA equals file offset for the cited ranges. Struct offsets are read from DWARF (`structures.json`), enum values from DWARF (`enums.json`), opcode/descriptor immediates from `objdump`, function addresses/sizes from the function table (`functions.json`). Source TUs are `/opt/workspace/KaenaRuntime/tdrv/instr_sunda.c`, `instr_common.c`, `instr_cayman.c`, `instr_collectives.c`, `instr_sunda_embedding_update.c`, `instr_sunda_pseudo_range_check.c`, `instr_sunda_swap_queue_set.c`. Other versions will differ.*

## Abstract

Lowering is one of the [two host-side concerns over the 64-byte word](overview.md): a **compiler back-end pass that runs at model-load time**. The compiler emits a NEFF whose per-engine stream is mostly real `NEURON_ISA_TPB` opcodes interleaved with a band of **pseudo-opcodes** (`0xC1..0xDF`, plus the control-flow stragglers `0xA9` `COMPARE_BRANCH` and `0xDD` `PSEUDO_BRANCH_PREFETCH_HINT`). A pseudo-opcode is a host-only macro: the device sequencer can never fetch one. This pass walks the stream, and for each pseudo-instruction expands it into **one or more concrete 64-byte records** that the device *can* fetch — exactly an assembler's macro-expansion phase, except the "macro" is a single 64-byte slot and the "expansion" is a sequence of real slots written into a per-engine code chunk. The dual concern, [validation](validator-architecture.md), then proves each emitted record legal before it is staged into HBM ([load pipeline](../neff/load-pipeline.md)).

The pass is built from a small, regular kit. At the leaf is a family of **micro-op builders** in `instr_sunda.c` — `add_br_rel`, `add_cmp_imm_br_rel`, `add_add32_imm`, `add_wr32`, `add_ldr32_addr`, `add_str32_addr`, `add_evsem`, `add_semaphore_inc`, `add_poll_sem`, `add_sema_clear`, and ~70 siblings. Each builder takes `(uint8_t *chunk, size_t offset, …, EVENTS *events)`, zero-initialises a 64-byte stack record of a `NEURON_ISA_TPB_*` type, stamps the 16-bit opcode word at `+0x00`, fills a per-opcode descriptor + operands, optionally copies the 8-byte `EVENTS` predicate at `+0x04`, and tail-calls the universal append primitive `add_ins` (`@0x322480`), which `memmove`s exactly `0x40` bytes into the chunk and returns the new byte offset. Above the leaves sit the **per-pseudo translators** — `translate_one_pseudo_instr_v2` / `_v3` (the single-instruction demux), the collective-column translators (`instr_col_translate_ptc/ptc2_full/psr/pcprid/pseudo_gid_load`), and the heavyweight `translate_one_pseudo_embedding_update_instr_v2` / `_range_check_instr_v2` — each of which emits an opcode-specific *recipe* of leaf calls. The shared SP record sink (`add_ins`) is the one chokepoint every emitter passes through, which is why lowering is reproducible as "a set of record-builders over one buffer-append", not a monolith.

This page documents the SP/TopSP slice of that pass: the **lowering driver** (how `translate_one_pseudo_instr` fans out to leaf builders that all funnel through `add_ins`), a representative **SP micro-op builder** for the branch + ALU classes (the `instr_sunda.c` control/compute encoders, opcode words `0x10a9` / `0x10a8`), the **collective-column translators** (the CC pseudo band `0xC8..0xDC` → `enc_op_list` + a trailing trigger record), and the **event-semaphore emitters** (`add_evsem` / `add_poll_sem` / `add_sema_clear` / `update_evtsema_first_and_last_inst`, opcode words `0x10a0` / `0x10b3` / `0x10b0`). A pseudo that expands to a record count ≠ 1 desynchronises the device-IP ↔ NEFF-IP map, which the lowering pass keeps coherent via the [`kbin_patch` IP-delta family](#5-the-ip-delta-patch-interaction-when-n--1) owned by the load pipeline. The [64-byte record layout](instruction-record.md) — the union arms these builders fill — is **not** re-derived here; this page is the producer, that page is the format.

For reimplementation, the contract is:

- **The append primitive.** `add_ins(chunk, offset, &record)` (`@0x322480`) is the single sink — `memmove(chunk+offset, record, 0x40); return offset+0x40`. Every one of the ~78 builders ends in a tail-call to it. `TPB_INST_NBYTES = 0x40` is proven by the `<<6` instruction-index→byte-offset shifts and the `memmove` length throughout.
- **The builder ABI.** `size_t __fastcall add_<op>(uint8_t *chunk, size_t offset, …operands…, NEURON_ISA_TPB_EVENTS *events)`. Zero a 64-byte stack record, store the opcode word `u16` at `+0x00` (high byte `0x10` = `inst_word_len` 16 dwords = 64 B), fill the per-opcode descriptor at `+0x0c` and operands, copy `*events` to `+0x04` iff `events != NULL`, tail-call `add_ins`. Return = updated chunk offset.
- **The per-pseudo recipe.** One pseudo lowers to an *ordered sequence* of builder calls — not a 1:1 rewrite. The driver (`translate_one_pseudo_instr_v2 @0x273ff0` / `_v3 @0x3221a0`) is an opcode demux; the body of each translator is the recipe. A reimplementer reproduces lowering as `{demux → per-opcode recipe → leaf builders → add_ins}`.
- **The IP-delta side-effect.** When a pseudo expands to N ≠ 1 records, the translator calls `kbin_patch_append` (`@0x2fb0f0`, owned by the [load pipeline](../neff/load-pipeline.md)) to record an INSERT (N>1) / DELETE (N=0) instruction-IP delta, keeping the host debug/profile IP view consistent with the device's post-expansion layout. This is an IP-delta, not an address relocation.
- **The arch gate.** The heavy translators branch on `al_hal_tpb_get_arch_type()` — `2` = SUNDA (v2), `3`/`4` = CAYMAN/MARIANA (v3). The SP micro-op builders themselves are the `instr_sunda.c` (V2) encoders; the v3 single-instruction demux down-translates SUNDA input via `translate_one_sunda_to_cayman_instr`.

| | |
|---|---|
| **Append primitive** | `add_ins` `@0x322480` — `memmove 0x40`; 78 callers; returns `offset + 0x40` |
| **Word size** | `TPB_INST_NBYTES = 0x40` (every record; proven by `<<6` indexing + `memmove` length) |
| **Per-instruction demux** | `translate_one_pseudo_instr_v2` `@0x273ff0` / `_v3` `@0x3221a0` |
| **Per-block driver** | `translate_pseudo_instrs_partial_v2` `@0x2763d0` |
| **Branch builders** | opcode word `0x10a9` (`CTRL_BR`) — `add_br_rel` `@0x271ae0`, `add_cmp_imm_br_rel` `@0x271d50`, `add_cmp_reg_br_rel` `@0x271b30`, 12 more; `0x10b5` br-hint `add_br_hint_rel_imm` `@0x271920` |
| **ALU builders** | opcode word `0x10a8` (`CTRL_AL`) — `add_add32_imm` `@0x272090`, `add_sub32` `@0x2722d0`, 11 more |
| **Collective translators** | `instr_col_translate_ptc` `@0x270160`, `_ptc2_full` `@0x270770`, `_psr` `@0x2705f0`, `_pcprid` `@0x271240`, `_pseudo_gid_load` `@0x271430`; trailing trigger `instr_col_add` `@0x26dcd0` |
| **Event-sema emitters** | `add_evsem` `@0x2737d0` (`0x10a0`), `add_semaphore_inc` `@0x273860` (`0x10a0`), `add_poll_sem` `@0x271820` (`0x10b3`), `add_sema_clear` `@0x271880` (`0x10b0`), `update_evtsema_first_and_last_inst` `@0x271780` |
| **Heavy translators** | `translate_one_pseudo_embedding_update_instr_v2` `@0x2770f0` (3391 B), `translate_one_pseudo_range_check_instr_v2` `@0x277ea0` (881 B) |
| **IP-delta patch** | `kbin_patch_append` `@0x2fb0f0` (load-pipeline-owned) — INSERT/DELETE when N ≠ 1 |
| **Source TUs** | `tdrv/instr_sunda.c`, `instr_common.c`, `instr_cayman.c`, `instr_collectives.c`, `instr_sunda_{embedding_update,pseudo_range_check,swap_queue_set}.c` |

---

## 1. The Lowering Driver: `translate_one_pseudo_instr` → `add_ins`

### Purpose

The driver is the per-instruction demux that turns one pseudo-opcode into a recipe of leaf-builder calls. It is the spine the whole pass hangs from: the per-block pass `translate_pseudo_instrs_partial_v2` (`@0x2763d0`) walks each function's instruction stream in `+0x40` strides, and for each record whose `byte[0]` is in the pseudo band hands it to the demux. Real opcodes pass through as a verbatim 64-byte copy; pseudo opcodes are *expanded*. The defining structural fact is that **every record the recipe emits passes through `add_ins`** (`@0x322480`), so a reimplementer models the driver as a switch whose every arm is a sequence of `add_*` calls that each tail-call one append primitive — never as a bespoke per-opcode codegen.

### Entry Point

```text
sequencer_setup_instr @0x4483d0                       ── per-engine NEFF lowering (sole driver caller)
  └─ itf_setup_functions_one_eng @0x2798c0
       └─ translate_pseudo_instrs_partial_v2 @0x2763d0 ── per-block; +0x40 stride walk
            ├─ init_pseudo_range_check_ctx @0x277e90   ── ctx->in_use = 0
            ├─ translate_one_pseudo_instr_v2 @0x273ff0 ── single-instr demux (SUNDA/V2)
            │    │  (or _v3 @0x3221a0 for CAYMAN/MARIANA)
            │    ├─ instr_col_translate_* (CC band 0xC8..0xDC)        → §3
            │    ├─ translate_one_pseudo_embedding_update_instr_v2    → tiled SDMA recipe
            │    ├─ translate_one_pseudo_range_check_instr_v2         → bound-check recipe
            │    ├─ translate_one_pseudo_dge_instr_v2 @0x322c40       → DGE direct2d/indirect1d
            │    ├─ add_dma_tail_inc_bcast / add_dma_rearm_bcast / add_evsem  → §4
            │    └─ add_ins @0x322480 (×N)             ── append each real 64-byte record
            └─ kbin_patch_append @0x2fb0f0 (when N ≠ 1)               → §5
```

### Algorithm

```c
// Models the lowering driver. translate_pseudo_instrs_partial_v2 @0x2763d0 (per-block)
// dispatching translate_one_pseudo_instr_v2 @0x273ff0 (per-instruction demux).
function translate_pseudo_instrs_partial(eng_chunk, in_stream, n_in, out_buf):
    init_pseudo_range_check_ctx(&prc_ctx)             // 0x277e90: ctx->in_use = 0
    out_off = 0
    for i in 0 .. n_in:                               // walk input stream
        pseudo = &in_stream[i << 6]                   // <<6 == ×TPB_INST_NBYTES (0x40)
        n_before = inst_count(out_buf)
        translate_one_pseudo_instr_v2(out_buf, &out_off, pseudo, &prc_ctx)
        n_emitted = inst_count(out_buf) - n_before
        // one pseudo may expand to N >= 0 real records; keep the IP map coherent (§5)
        if n_emitted != 1:
            kbin_patch_append(model.kbin_patch_info,  // 0x2fb0f0 — load-pipeline-owned
                              n_emitted > 1 ? INSERT : DELETE, ip_delta)
    return out_off

function translate_one_pseudo_instr_v2(out, p_off, pseudo, prc_ctx):   // 0x273ff0
    switch pseudo->header.opcode:                     // byte[0] discriminant
        // --- collective band → §3 (each emits enc_op_list + a trailing trigger record) ---
        case 0xC8: return instr_col_translate_ptc(out, p_off, pseudo)            // PTC
        case 0xCB: return instr_col_translate_psr(out, p_off, pseudo)            // SENDRECV
        case 0xD9: return instr_col_translate_ptc2_full(out, p_off, pseudo)      // PTC2 (+0xDA ext)
        case 0xDB: return instr_col_translate_pcprid(out, p_off, pseudo)         // rank-id load
        case 0xDC: return instr_col_translate_pseudo_gid_load(out, p_off, pseudo)// gid load
        // --- heavy SDMA / bound-check recipes ---
        case PSEUDO_EMBEDDING_UPDATE:
            return translate_one_pseudo_embedding_update_instr_v2(out, p_off, pseudo) // 0x2770f0
        case PSEUDO_RANGE_CHECK:
            return translate_one_pseudo_range_check_instr_v2(out, p_off, pseudo, prc_ctx) // 0x277ea0
        case PSEUDO_DMA_MEMCPY: case PSEUDO_DMA_DIRECT2D:
            return translate_one_pseudo_dge_instr_v2(out, p_off, pseudo)         // 0x322c40
        // --- a real opcode: verbatim 64-byte copy, no expansion ---
        default:
            *p_off = add_ins(out, *p_off, pseudo)      // 0x322480: memmove 0x40
            return NRT_SUCCESS
```

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `translate_pseudo_instrs_partial_v2` | `0x2763d0` | — | per-block `+0x40` walk + IP-delta patch | HIGH |
| `translate_one_pseudo_instr_v2` | `0x273ff0` | — | single-instruction demux (SUNDA/V2) | HIGH |
| `translate_one_pseudo_instr_v3` | `0x3221a0` | — | demux for CAYMAN/MARIANA; SUNDA→cayman down-translate | HIGH |
| `add_ins` | `0x322480` | — | universal append: `memmove 0x40`, 78 callers | CERTAIN |
| `init_pseudo_range_check_ctx` | `0x277e90` | 6 | reset the deferred-branch ctx | HIGH |

### Considerations

The demux key is the raw opcode byte (`byte[0]`), the same discriminant the [record format](instruction-record.md#2-the-opcode-taxonomy) uses. Two arms are worth a reimplementer's care. First, the **default arm is not a no-op**: a real opcode is copied verbatim via a single `add_ins`, which counts as exactly one emitted record — so the IP-delta check in the per-block pass (§5) is naturally satisfied and never fires for non-pseudo records. Second, the `v3` demux (`@0x3221a0`) handles a *disjoint* pseudo set — `PSEUDO_JPEG_DECODE` (`0xD7`, CC-top, `device_tpb_idx==0` only) and the DGE transpose family — and down-translates a SUNDA-targeted input through `translate_one_sunda_to_cayman_instr` before lowering; the `v2`/`v3` split is by silicon generation, not by a different algorithm.

> **NOTE —** the demux runs on the *pseudo* stream; by the time [validation](validator-architecture.md) runs, the pseudo band is gone and only real opcodes remain. A surviving pseudo would be rejected by `is_valid_neuron_instruction` as an unknown opcode — which is the safety net that catches a missing demux arm in a reimplementation.

---

## 2. The SP Micro-Op Builder (Branch + ALU)

### Purpose

The micro-op builders are the leaves every recipe calls. The control-flow (`CTRL_BR`, opcode word `0x10a9`) and scalar-ALU (`CTRL_AL`, opcode word `0x10a8`) families in `instr_sunda.c` are the representative class: they encode the branch and compute records that the embedding-update bound-check prologue, the function call/return frame, and the inference-wait sequences are built from. All 24 builders in this slice (`.text 0x271920..0x2724c4`) share one ABI and one sink, so documenting one branch builder and one ALU builder pins the whole family. They are the runtime-side SP ISA the on-chip **TopSP** (Top Sequencer Processor) executes for control flow.

### Entry Point

```text
translate_one_pseudo_embedding_update_instr_v2 @0x2770f0   ── bound-check prologue
ib_create_one_block @0x2f7e50                              ── function-body branch
itf_translate_function_return_instr @0x27a6d0              ── return-addr branch
  └─ add_br_rel @0x271ae0 / add_cmp_imm_br_rel @0x271d50 / add_add32_imm @0x272090  …
       └─ add_ins @0x322480                                ── memmove 0x40 → chunk
```

### Algorithm

```c
// Representative branch builder: add_cmp_imm_br_rel @0x271d50 (0xa2 B).
// Emits CTRL_BR (op word 0x10a9): "cmp(reg) <cmp_op> imm32 → relative branch".
// ABI: size_t __fastcall (chunk, offset, cmp_reg, cmp_op, cmp_dtype, cmp_imm32,
//                         rel32, events). dtype here is INT32 (8). HIGH (objdump-pinned).
function add_cmp_imm_br_rel(chunk, offset, cmp_reg, cmp_op, cmp_dtype,
                            cmp_imm32, rel32, events):
    rec = zero(64)                                  // NEURON_ISA_TPB_CTRL_BR_STRUCT
    rec.u16[0x00] = 0x10a9                           // header.opcode; hi 0x10 = inst_word_len 16
    rec.u8[0x03]  = 2                                // header.debug_hint = 2 (rel-encoded marker)
    if events: memcpy(&rec[0x04], events, 8)         // EVENTS predicate (wait/update tuple)
    rec.u8[0x0c]  = cmp_op                            // descriptor byte0: BR_CMP_OP (e.g. EQIMM 3)
    rec.u8[0x0d]  = cmp_dtype                         // descriptor byte1: DTYPE (INT32 = 8)
    rec.u8[0x0e]  = BR_TARGET_MODE_RELATIVE_IMMEDIATE // descriptor byte2 = 3 (rel-imm target)
    rec.u32[0x10] = cmp_imm32                         // cmp_immediate (4 B)
    rec.u8[0x20]  = cmp_reg                           // cmp_reg0
    rec.u64[0x30] = (u64)(u32)rel32                   // br_immediate (rel32, zero-extended)
    return add_ins(chunk, offset, &rec)              // 0x322480: append, return offset+0x40

// Representative ALU builder: add_add32_imm @0x272090 (0x70 B).
// Emits CTRL_AL (op word 0x10a8): "dst = src0 + imm32" (INT32, immediate src1).
// ABI: (chunk, offset, dst_reg, src0_reg, imm32, events). HIGH (objdump-pinned).
function add_add32_imm(chunk, offset, dst_reg, src0_reg, imm32, events):
    stage = zero(64)                                 // SSE staging scratch @ [rsp+0x40]
    stage.u32[0x0c] = 0x10804                         // {op=ADD 4, dtype=INT32 8, src1_datasrc=IMM 1}
    movaps_copy_down(rec, stage)                     // movdqa/movaps stage → 64-B record @ [rsp]
    rec.u16[0x00] = 0x10a8                            // header.opcode (CTRL_AL)
    if events: memcpy(&rec[0x04], events, 8)
    rec.u8[0x10]  = src0_reg                          // src0_lo
    rec.u8[0x14]  = dst_reg                           // dst_lo
    rec.u64[0x18] = imm32                             // immediate (ALU_IMMEDIATE, 8 B field)
    return add_ins(chunk, offset, &rec)
```

### Function Map

| Function | Address | Size | Opcode word | Role | Confidence |
|---|---|---|---|---|---|
| `add_br_rel` | `0x271ae0` | 0x4c | `0x10a9` | relative-immediate branch (rel32 `@+0x30`) | HIGH |
| `add_br_reg` | `0x271a90` | 0x4b | `0x10a9` | register-indirect branch (`target_reg_lo/hi @+0x22/0x23`) | HIGH |
| `add_br_label_id` | `0x2719f0` | 0x4a | `0x10a9` | branch to a label id (resolved in post-pass) | HIGH |
| `add_cmp_imm_br_rel` | `0x271d50` | 0xa2 | `0x10a9` | `cmp(reg, imm32) → rel branch`; generic `cmp_op` | HIGH |
| `add_cmp_reg_br_rel` | `0x271b30` | 0x8e | `0x10a9` | `cmp(reg0, reg1) → rel branch` | HIGH |
| `add_cmpeq_imm_br_rel` | `0x271f90` | 0x86 | `0x10a9` | `cmp == imm → rel`; descriptor `0x30803` | HIGH |
| `add_br_hint_rel_imm` | `0x271920` | 0xcb | `0x10b5` | branch-with-outcome-hint; asserts `\|off\| < 2^31` | HIGH |
| `add_add32_imm` | `0x272090` | 0x70 | `0x10a8` | `dst = src0 + imm32`; descriptor `0x10804` | HIGH |
| `add_sub32_imm` | `0x272100` | 0x70 | `0x10a8` | `dst = src0 − imm32`; descriptor `0x10805` | HIGH |
| `add_add32` | `0x272250` | 0x74 | `0x10a8` | `dst = src0 + src1` (reg); descriptor `0x804` | HIGH |
| `add_sub32` | `0x2722d0` | 0x74 | `0x10a8` | `dst = src0 − src1` (reg); descriptor `0x805` | HIGH |
| `add_max32` / `add_min32` | `0x2723d0` / `0x272450` | 0x74 | `0x10a8` | `MAX`/`MIN` reg; descriptors `0x808`/`0x809` | HIGH |
| `add_ins` | `0x322480` | — | — | the shared 64-byte append sink | CERTAIN |

### Encoding

The two families fill the two 64-byte arms documented on the [record-format page](instruction-record.md#worked-arm-layouts): `CTRL_BR` and `CTRL_AL`. The descriptor at `+0x0c` is the per-family selector — three packed bytes `{op/cmp, dtype, target/src-mode}`. For branches: `{BR_CMP_OP, DTYPE, BR_TARGET_MODE}`, e.g. `add_cmpgt_imm_br_rel` stamps the literal `0x30806` = `{cmp=GTIMM 6, INT32 8, REL_IMM 3}`. For ALU: `{ALU_OP, DTYPE, DATA_SRC}`, e.g. `add_add32_imm` stamps `0x10804` = `{op=ADD 4, INT32 8, src1_datasrc=IMM 1}`. The descriptor-fixed builders (the `cmpgt/lt/eq/ne` forms and the reg/imm ALU forms) write the constant via an **SSE staging idiom**: zero a `0x40`-byte scratch at `[rsp+0x40]`, write opcode/descriptor/operands there, then `movdqa`/`movaps`-copy each 16-byte lane *down* into the real 64-byte record at `[rsp+0x00]` before `add_ins`. The operands then land at record offsets `src0_lo @+0x10`, `dst_lo @+0x14`, `immediate @+0x18` for ALU; `cmp_reg0 @+0x20`, `br_immediate @+0x30` for branch.

> **CORRECTION (TDRV-CORE-10) —** an earlier seed (NX-091) placed the cmp/ALU descriptor at record `+0x4c` and operands at `+0x50/+0x54/+0x58`. Those are the **staging-stack** offsets (`[rsp+0x40..]`), not the record offsets. The builders stage at `[rsp+0x40]` then `movaps`/`movdqa`-copy down to the 64-byte record at `[rsp]`; the *true record* offsets are descriptor `@+0x0c`, operands `@+0x10/+0x14/+0x18`, `br_immediate @+0x30`. Proof: `add_add32_imm` `0x2720aa..` writes `DWORD[rsp+0x4c],0x10804` then `movaps [rsp],xmm1`, landing `0x10804` at record `+0x0c`. (HIGH — DWARF struct + the staging→record copy disassembly.)

### Considerations

The descriptor byte `+0x0d` is a `DTYPE`, not a "bitwise width" flag: `0x08`=`INT32`, `0x09`=`UINT32` (`add_bit_or_imm` uses `UINT32` because a logical op is unsigned, not because the byte selects a width). The byte `+0x0e` is the *mode* selector — `BR_TARGET_MODE` for branches (`REL_IMM 3` / `REL_REGISTER 4` / `REGISTER 1`), `DATA_SRC` for ALU (`IMMEDIATE 1` / `REGISTER 0`). Only `add_br_hint_rel_imm` (the `0x10b5` outlier) validates anything: it asserts `|br_rel_offset| < 2^31` and `nlog_write`s "Invalid branch relative offset…Offset must be fit within 32 bits" before the assert at `instr_sunda.c:0x99`. The 32-bit reach is re-checked at branch-resolution time by `ipb_postprocess_instrs` (`@0x3237a0`); a reimplementer must enforce it in both the emit and the resolve pass.

---

## 3. The Collective-Instruction Column Translators

### Purpose

The collective (CC) band — `PSEUDO_TRIGGER_COLLECTIVE` (`0xC8`), `PSEUDO_SENDRECV` (`0xCB`), `PSEUDO_TRIGGER_COLLECTIVE2` (`0xD9`, + its `0xDA` `PSEUDO_EXTENSION`), `PSEUDO_CUR_PROCESSING_RANK_ID` (`0xDB`), `PSEUDO_GID_LOAD` (`0xDC`) — is lowered differently from compute pseudos: a CC pseudo does **not** expand into a tensor-op recipe. Instead it (a) decodes the pseudo into an internal `enc_op_list_args` that the encoder-engine ("ENC") consumes to plan the collective, then (b) emits a **single trailing trigger record** (a `WR32` CSR-write, or a `NOP` under `dbg_cc_nop`) into the instruction buffer — the on-stream marker that fires the planned collective. The translators are the column that walks the NEFF CC sub-graph and rewrites it into that {ENC-plan + trigger} pair. This is the NCCOM/collectives lowering ABI, distinct from the public NRT collectives API.

### Entry Point

```text
translate_one_pseudo_instr_v2 @0x273ff0   switch(opcode):
  0xC8 → instr_col_translate_ptc        @0x270160
  0xCB → instr_col_translate_psr        @0x2705f0
  0xD9 → instr_col_translate_ptc2_full  @0x270770  (consumes following 0xDA PTC2E)
  0xDB → instr_col_translate_pcprid     @0x271240
  0xDC → instr_col_translate_pseudo_gid_load @0x271430
       └─ build_cc_context @0x26fee0 → build_enc_op_list_args_with_tensor_id @0x26e330
          └─ instr_col_enc_enqueue @0x26dc30  ── ENC plans collective → trigger_addr
             └─ instr_col_add @0x26dcd0       ── append trailing trigger record
                └─ add_wr32 @0x2733d0 / add_nop @0x273d40 → buf_append @0x265e80
```

### Algorithm

```c
// Representative CC translator: instr_col_translate_ptc @0x270160 (0x48e B).
// Lowers PSEUDO_TRIGGER_COLLECTIVE (0xC8) into an ENC op-list plan + one trigger record.
function instr_col_translate_ptc(out_buf, ins):
    if !build_cc_context(&cc_ctx, kbin): fail("Failed to build cc context")  // 0x26fee0
    if cc_ctx.streams_n > 1: fail("PTC instructions only support single streams")
    enc_stream_map_set_stream_id(cc_ctx, ins->group_id)
    require(ins.events.update_mode == SEM_INC_COMPLETE)   // CC must inc a sema on complete
    enc_op = enc_op_of_ctype(ins->ctype)                  // ALLREDUCE/REDUCE_SCATTER/ALLGATHER/…
    if enc_op in {ALL_REDUCE, REDUCE_SCATTER}:
        require((1u << ins->alu_op) & 0x350)              // valid reduce ALU (ADD/MULT/MAX/MIN)
    in_n  = ptc_input_total_num_elements(enc_op, n_elem, rank_n)   // 0x2700a0: ×rank_n if input spans ranks
    out_n = ptc_output_total_num_elements(enc_op, n_elem, rank_n)  // 0x270100: ×rank_n if output spans ranks
    // assemble the per-rank op list (maps COLLECTIVE_TYPE × ALU_OP × CHAINING → enc_op_type)
    build_enc_op_list_args_with_tensor_id(&args, cc_ctx, ins, in_n, out_n)  // 0x26e330
    instr_col_enc_enqueue(cc_ctx, &args, &trigger_addr)   // 0x26dc30: ENC plans → trigger CSR addr
    // emit the single trailing trigger record (the on-stream collective fire)
    instr_col_add(out_buf, cc_ctx, ins, trigger_addr)     // 0x26dcd0

// The trailing trigger emitter: instr_col_add @0x26dcd0 (0x166 B).
function instr_col_add(out_buf, cc_ctx, ins, trigger_addr):
    rec = zero(64)
    if ins.chaining in {NONE, FINAL}:                     // copy events; FINAL overlays first-instr sema
        copy_events(rec, ins)
        if ins.chaining == FINAL:
            rec.wait_mode      = cc_ctx.collective2_first_instr[+4]
            rec.semaphore_value= cc_ctx.collective2_first_instr[+8]
    if gconf.dbg_cc_nop:        add_nop(out_buf, rec.events)          // 0x273d40 (debug: no fire)
    elif cc_ctx.ccop_owner:     add_wr32(out_buf, trigger_addr, rec)  // 0x2733d0 (CTRL_WR doorbell)
    buf_append(out_buf, &rec, 0x40); ++num_instructions_inserted
```

### Function Map

| Function | Address | Size | Pseudo (opcode) | Role | Confidence |
|---|---|---|---|---|---|
| `instr_col_translate_ptc` | `0x270160` | 0x48e | `0xC8` PTC | allreduce/reduce-scatter/allgather/… plan + trigger | HIGH |
| `instr_col_translate_psr` | `0x2705f0` | 0x175 | `0xCB` SENDRECV | single ENC_SEND/ENC_RECV op + trigger | HIGH |
| `instr_col_translate_ptc2_full` | `0x270770` | 0xa0c | `0xD9` (+`0xDA`) | PTC2/PTC2E pair; PERMUTE; SBUF-partition decode | HIGH |
| `instr_col_translate_pcprid` | `0x271240` | 0x1e8 | `0xDB` | rank-id buffer addr → `add_ldr32_addr` (4-B load only) | HIGH |
| `instr_col_translate_pseudo_gid_load` | `0x271430` | 0x274 | `0xDC` | global device-id → `add_mov32_imm`/`add_str32_imm_*` | HIGH |
| `build_enc_op_list_args_with_tensor_id` | `0x26e330` | 0x1748 | — | op-list assembler (`N × 248-B enc_op_args`) | HIGH |
| `instr_col_add` | `0x26dcd0` | 0x166 | — | trailing trigger record (`add_wr32`/`add_nop`) | HIGH |
| `get_ptc2_dma_configs_priority_class_v3` | `0x2716b0` | 0x84 | `0xDA` | 3-bit DMA priority from PTC2E `buf[41] & 7` (arch>2) | HIGH |
| `ptc_input_total_num_elements` | `0x2700a0` | 0x51 | — | ×rank_n for ops whose input spans ranks (mask `0x90`) | HIGH |
| `ptc_output_total_num_elements` | `0x270100` | 0x51 | — | ×rank_n for ops whose output spans ranks (mask `0x81`) | HIGH |

### Encoding

The op-list assembler `build_enc_op_list_args_with_tensor_id` is the dense core: a big switch on `ins->ctype` crosses an internal op-class id (`ALLGATHER=0, ALLREDUCE=1, REDUCE_SCATTER=4, ALLTOALL=7, PERMUTE=8, …`) with the `ALU_OP` (reduce subcode 0..3 from ADD/MULT/MAX/MIN) and `INSTR_CHAINING` (NONE/INITIAL/BODY/FINAL), validates per-op `cc_dim` support via bitmasks, resolves input0/input1/output mem-refs by tensor id, enforces MR-list symmetry, computes per-element byte sizes, then `calloc(num_ops, 248)`s the `enc_op_args[]` array and SIMD-stores each 248-byte op. The element-count scalers `ptc_input/output_total_num_elements` use two bit-masks over `enc_op`: `0x90` (bits 4,7 = `REDUCE_SCATTER`, `ALLTOALL`) scales the *input* by `rank_n`; `0x81` (bits 0,7 = `ALLGATHER`, `ALLTOALL`) scales the *output* — the asymmetry encodes which side of each collective fans across ranks.

> **QUIRK —** the CC translators do **not** emit a tensor-compute recipe — the actual reduce/gather is planned in the ENC engine (`instr_col_enc_enqueue`, out of this slice) and fired by a *single* `WR32` doorbell record written by `instr_col_add`. A reimplementer who expects `0xC8` to expand into N compute instructions will mis-model the IP-delta (§5): a CC pseudo lowers to **exactly one** on-stream record (the trigger), or zero under `dbg_cc_nop`'s `add_nop` path it is still one record but a NOP. The collective's "body" lives off-stream in the ENC plan.

### Considerations

`instr_col_translate_ptc2_full` is the heaviest CC arm: it consumes the `0xD9` PTC2 **and** the mandatory following `0xDA` PTC2E (asserting `buf+64` is opcode `0xDA`, else `NRT_INVALID`), decodes a packed 26-bit SBUF-partition offset (asserting bits 25 and 26..31 clear), enforces `VAR_ID` start-address markers (`(x & 0xFC) == 0x10`) on src0/dst/src1, and reads the 3-bit DMA priority class from PTC2E `buf[41] & 7` on cayman+ (arch>2). The `pcprid` and `pseudo_gid_load` arms are the lightweight CC loads: `pcprid` resolves the current-rank-id buffer address and emits a single `add_ldr32_addr` (only 4-byte loads are supported — it asserts otherwise), and `pseudo_gid_load` resolves the global device-id and emits `add_mov32_imm` (register dst) or `add_str32_imm_addr`/`add_str32_imm_reg` (memory dst), keyed on the destination marker.

---

## 4. The Event-Semaphore Emitters (evtsema / poll-sem / sema-clear)

### Purpose

Inter-engine synchronization is the substrate every lowered recipe threads through: a DMA tile must wait on a semaphore before the next reads its result, a function block must increment a sema on entry/exit, a bound-check must poll a sema before branching. The event-semaphore emitters are the leaf builders that encode those sync micro-ops as standalone records (opcode words `0x10a0` `CTRL_ES`, `0x10b3` `CTRL_POLL_SEM`, `0x10b0` `CTRL_ER`), plus two *in-place patchers* (`update_evtsema*`) that overwrite the `EVENTS` field of records already in a chunk rather than appending new ones. Together they are how the lowering pass expresses the wait/update tuple both inline (riding in a compute record's `EVENTS @+0x04`) and as dedicated sync slots.

### Entry Point

```text
translate_one_pseudo_instr_v2 @0x273ff0       → add_evsem @0x2737d0
translate_one_pseudo_embedding_update_instr_v2 @0x2770f0 → add_semaphore_wait_eq_and_clear (sync)
tsync_load_one_program @0x3037a0 / add_sync_barrier @0x27af20 → add_semaphore_inc @0x273860
translate_one_pseudo_range_check_instr_v2 @0x277ea0  → (poll/clear in bound-check prologue)
  └─ add_evsem / add_poll_sem / add_sema_clear / add_semaphore_inc
       └─ add_ins @0x322480
  └─ update_evtsema_first_and_last_inst @0x271780   ── in-place EVENTS patch (no add_ins)
```

### Algorithm

```c
// add_semaphore_inc @0x273860 — emit "semaphore[id] += 1" as a CTRL_ES record (op 0x10a0).
// (add_semaphore_inc_val @0x273820 is identical with a caller-supplied delta @+0x28.)
function add_semaphore_inc(chunk, offset, semaphore_id):
    rec = zero(64)                                  // NEURON_ISA_TPB_CTRL_ES_STRUCT
    rec.u16[0x00] = 0x10a0                            // header.opcode (CTRL_ES)
    rec.u8[0x22]  = 0x15                              // events_extended.update_mode = SEM_ADD_IMM_COMPLETE (21)
    rec.u8[0x23]  = semaphore_id                      // events_extended.update_idx
    rec.u32[0x28] = 1                                 // events_extended.sem_update_value (hard 1)
    return add_ins(chunk, offset, &rec)              // 0x322480

// add_poll_sem @0x271820 — emit a CTRL_POLL_SEM (op 0x10b3): poll sema `addr` n_read
// times into `reg`, writeback to `wb_addr`. ALU sub-op = MIN. -> add_ins.
function add_poll_sem(chunk, offset, addr, n_read, reg, wb_addr, events):
    rec = zero(64)
    rec.u16[0x00] = 0x10b3                            // CTRL_POLL_SEM (4275)
    rec.alu_op    = MIN                               // poll-min semantics
    fill_poll_fields(rec, addr, n_read, reg, wb_addr)
    if events: memcpy(&rec[0x04], events, 8)
    return add_ins(chunk, offset, &rec)

// add_sema_clear @0x271880 — emit a CTRL_ER (op 0x10b0): clear sema range [first, last].
function add_sema_clear(chunk, offset, range_first, range_last, events):
    rec = zero(64)                                  // NEURON_ISA_TPB_CTRL_ER_STRUCT
    rec.u16[0x00] = 0x10b0                            // CTRL_ER (event/semaphore reset, 4272)
    simd_fill_range(rec, range_first, range_last)
    if events: memcpy(&rec[0x04], events, 8)
    return add_ins(chunk, offset, &rec)

// In-place patcher (NOT an append): update_evtsema_first_and_last_inst @0x271780.
// Splits a wait onto the block's FIRST record and an update onto its LAST.
function update_evtsema_first_and_last_inst(chunk, chunk_size, block_start, block_end, events):
    require(block_end <= chunk_size)                 // else nlog + assert instr_sunda.c:0x2E
    first = &chunk[block_start]; last = &chunk[block_end - 0x40]
    first.events.wait_mode       = events.wait_mode      // wait on entry
    first.events.semaphore_value = events.semaphore_value
    last.events.update_mode      = events.update_mode    // update on exit
    last.events.semaphore_value  = events.semaphore_value
```

### Function Map

| Function | Address | Size | Opcode word | Role | Confidence |
|---|---|---|---|---|---|
| `add_evsem` | `0x2737d0` | 0x4f | `0x10a0` | generic event/sema op (`events_extended @+0x20`) | HIGH |
| `add_semaphore_inc` | `0x273860` | — | `0x10a0` | `sema[id] += 1`; `update_mode 0x15`, value `@+0x28` | HIGH |
| `add_semaphore_inc_val` | `0x273820` | — | `0x10a0` | `sema[id] += value`; caller delta `@+0x28` | HIGH |
| `add_poll_sem` | `0x271820` | 0x58 | `0x10b3` | poll sema `addr` n times → `reg`, writeback (ALU MIN) | HIGH |
| `add_sema_clear` | `0x271880` | 0x87 | `0x10b0` | clear sema range `[first, last]` (`CTRL_ER`) | HIGH |
| `update_evtsema_first_and_last_inst` | `0x271780` | 0x97 | — | in-place: wait on first record, update on last | HIGH |
| `update_evtsema` | `0x271770` | 0x08 | — | in-place: overwrite one record's `EVENTS @+0x04` | HIGH |
| `insert_events` | `0x271910` | 0x06 | — | in-place: write `new_events` at a byte offset | HIGH |
| `get_event_semaphore_wait_update` | `0x276cb0` | 0x2f | — | pure packer of the 8-byte `EVENTS` tuple | HIGH |

### Encoding

`CTRL_ES` (`0x10a0`) carries its sync payload in an `events_extended` block at record `+0x20` (12 B: `wait_mode @+0x20`, `wait_idx @+0x21`, `update_mode @+0x22`, `update_idx @+0x23`, `sem_wait_value @+0x24`, `sem_update_value @+0x28`) — distinct from the standard 8-byte `EVENTS @+0x04` the other families use. The semaphore-increment forms hard-stamp `update_mode = 0x15` (`SEM_ADD_IMM_COMPLETE`, enum value 21) at `+0x22` and the semaphore id at `+0x23`; `_inc` writes a literal `1` at `+0x28`, `_inc_val` writes the caller's delta. The pure packer `get_event_semaphore_wait_update` (`@0x276cb0`) builds the *standard* 8-byte tuple as `(value<<32)|(update_idx<<24)|(update_mode<<16)|(wait_idx<<8)|wait_mode` — the value a builder copies to `+0x04` when `events != NULL`.

> **GOTCHA —** `update_evtsema_first_and_last_inst` and `update_evtsema` **mutate records already in the chunk**; they do *not* call `add_ins` and do *not* change the instruction count. A reimplementer modelling lowering purely as "append builders" will miss the split-sync mechanism: the wait predicate is patched onto a block's first record and the update onto its last (so a block's entry blocks until a sema is ready and its exit signals completion), without emitting any new slot. Because they emit zero records, they are invisible to the IP-delta accounting (§5).

### Considerations

The split-sync pattern (`update_evtsema_first_and_last_inst`) is the runtime's way of fencing a *block* without a dedicated barrier record: rather than emit a wait instruction and a separate signal instruction, it folds the wait into the first existing record's `EVENTS` and the signal into the last's. The standalone emitters serve the cases where a block boundary is not available — `add_semaphore_inc` for `add_sync_barrier`/`tsync_load_one_program`'s explicit barriers, `add_poll_sem`/`add_sema_clear` for the bound-check prologue's deferred-branch arming. All four append-emitters share the leaf ABI and the `add_ins` sink; only the patchers break it.

---

## 5. The IP-Delta Patch Interaction (when N ≠ 1)

### Purpose

A pseudo that expands to a record count ≠ 1 breaks the 1:1 correspondence between NEFF instruction index and device instruction index. The compiler's NEFF IP for a pseudo no longer equals the device IP of its expansion: an embedding-update pseudo can tile into dozens of records (INSERT), a `PSEUDO_BRANCH_LABEL` (`0xCC`) is consumed to a PC and emits nothing (DELETE). The host debug/profile view — which maps a device IP back to a NEFF IP for the profiler — would drift unless every such expansion is recorded. The lowering pass keeps that map coherent by appending an **instruction-IP delta** to the `kbin_patch` store whenever N ≠ 1.

### Entry Point

```text
translate_pseudo_instrs_partial_v2 @0x2763d0     ── per-block lowering; tracks n_emitted
  └─ kbin_patch_append @0x2fb0f0                  ── INSERT (N>1) / DELETE (N=0) ip_delta
       │  (load-pipeline-owned; records into model_t.kbin_patch_info @+0x18A0)
       └─ (consumed later) kbin_patch_device_ip_to_neff_ip @0x2fb3a0  ── profiler back-map
```

### Algorithm

```c
// The IP-delta side-effect, raised when one pseudo lowers to N != 1 records.
// kbin_patch_append @0x2fb0f0 is OWNED BY THE LOAD PIPELINE — this is the call site only.
function on_pseudo_lowered(model, pseudo_neff_ip, n_emitted, device_ip):
    if n_emitted > 1:                                // pseudo tiled into many records
        kbin_patch_append(model.kbin_patch_info, INSERT,
                          delta = n_emitted - 1,     // extra device slots vs NEFF
                          pseudo_neff_ip, device_ip) // 0x2fb0f0
    elif n_emitted == 0:                             // pseudo consumed (e.g. 0xCC label)
        kbin_patch_append(model.kbin_patch_info, DELETE,
                          delta = 1, pseudo_neff_ip, device_ip)
    // n_emitted == 1 (a real opcode, or a 1:1 pseudo): no patch — IP map already aligned
```

### Function Map

| Function | Address | Role | Confidence | Owner |
|---|---|---|---|---|
| `kbin_patch_append` | `0x2fb0f0` | record INSERT/DELETE instruction-IP delta | HIGH | [load pipeline](../neff/load-pipeline.md) |
| `kbin_patch_device_ip_to_neff_ip` | `0x2fb3a0` | device-IP → NEFF-IP back-map (profiler) | HIGH | [load pipeline](../neff/load-pipeline.md) |

### Considerations

The patch store is **not** the address relocator — it tracks *instruction-IP deltas*, not operand addresses. The actual address relocation (var-id → physical-address resolution, DMA-ring `buf_ptr` rewriting) is a separate sub-layer inside the translators and `imcpy_load_vring`. A reimplementer must keep the two distinct: the IP-delta map exists so `nrt_profile_get_instruction_patch_info` can translate a device IP back to the NEFF IP a profiling sample was taken at, even though one NEFF pseudo became many device records. This page is the *producer* of the N-record expansions that raise the deltas; the [load pipeline](../neff/load-pipeline.md#the-relocation-model) owns the patch store, its struct (`model_t.kbin_patch_info @+0x18A0`), and the back-map consumer.

> **CORRECTION (W2-NEFF-LOAD) —** the `kbin_patch_*` family is sometimes read as the address relocator. It is **not**: it records instruction-IP deltas (INSERT when a pseudo expands to >1 ISA slot, DELETE when it collapses to 0) so the host debug/profile IP view stays consistent with the device's post-expansion layout. The address relocation is the var-id→PA resolution in `translate_*` and the `buf_ptr` rewrite in `imcpy_load_vring`. (HIGH — `kbin_patch_append`'s callers are the translators and the instruction-block builders; cross-referenced from the load-pipeline page.)

---

## Related Components

| Name | Relationship |
|---|---|
| `add_ins` `@0x322480` | the single 64-byte append sink every builder tail-calls |
| `translate_one_pseudo_instr_v2` / `_v3` | the per-instruction demux that selects each recipe |
| `instr_col_*` translators | the collective-band column lowering ({ENC plan + trigger}) |
| `add_evsem` / `add_poll_sem` / `add_sema_clear` | the standalone sync-record emitters |
| `update_evtsema_first_and_last_inst` | the in-place split-sync patcher (emits no record) |
| `kbin_patch_append` `@0x2fb0f0` | the IP-delta store fed when an expansion is N ≠ 1 |

## Cross-References

- [The 64-Byte Instruction Record Format](instruction-record.md) — the `CTRL_BR` / `CTRL_AL` / `CTRL_ES` / `MEM_2D` union arms these builders fill; the layout this page produces *into*
- [Overview: the TPB Engine Instruction Model](overview.md) — the device-vs-runtime split and the lowering→validation→stage flow this page is concern A of
- [ISA Validator Architecture and Entry Tree](validator-architecture.md) — the dual concern; every record this pass emits is vetted by `is_valid_neuron_instruction` before HBM
- [The Load Pipeline (Parse → Build → Stage → Relocate)](../neff/load-pipeline.md) — owns `sequencer_setup_instr` (the pass driver) and the `kbin_patch` IP-delta family this page raises deltas into
- [back to index](../index.md)
