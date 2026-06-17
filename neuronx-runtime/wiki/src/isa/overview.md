# Overview: the TPB Engine Instruction Model

> *All addresses, offsets, struct ordinals, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce` (runtime-lib **2.31.24.0-0b044f4ce**; `libnrt.so.2.31.24.0`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). `.text` VMA equals file offset for the cited ranges. Struct offsets are read from DWARF (`structures.json`), enum values from DWARF (`enums.json`), function addresses/sizes/caller-counts from the function table (`functions.json`). Other versions will differ.*

## Abstract

The Neuron device — the TPB (Tensor Processing Block) — fetches and executes a flat stream of [fixed 64-byte instruction records](instruction-record.md), one per engine, on five engines in parallel (PE / ACT / POOL / DVE / SP). The device's job is narrow: fetch a record, decode the opcode byte, run it. Everything *before* that fetch — turning the compiler's NEFF into a stream the sequencer can safely fetch — is the runtime's job, and it reduces to **two host-side concerns over that 64-byte word**: **lowering** the compiler's pseudo-instructions into real hardware records, and **validating** every record's encoding before it is staged into device HBM. This is the runtime-side analog of the gap between an assembler's *macro-expansion* pass and its *encode-time assert* battery — except both run at model-load time, on a word the hardware will fetch verbatim, with no second chance to catch a malformed record once it is in HBM.

Concretely, the compiler emits a NEFF whose per-engine instruction stream is mostly real `NEURON_ISA_TPB` opcodes but is interleaved with a band of **pseudo-opcodes** (`0xC1..0xDF`, plus the control-flow stragglers `0xA9` COMPARE_BRANCH and `0xDD` PSEUDO_BRANCH_PREFETCH_HINT). A pseudo-opcode is a host-only macro: `PSEUDO_FUNCTION_BEGIN` (`0xD1`) splits the stream into named functions; `PSEUDO_FUNCTION_CALL` (`0xD3`) / `RETURN` (`0xD2`) are a call/return frame the device cannot execute directly; `PSEUDO_DMA_DIRECT2D` (`0xD4`) and the DGE family lower to concrete DMA descriptors; `PSEUDO_JPEG_DECODE` (`0xD7`) is a CC-top trigger. The **lowering** subsystem (the `itf_*` / `translate_*` / `psqs_*` / `add_*` family in `tdrv/instr_*.c`) rewrites every pseudo-opcode into one or more real 64-byte records during instruction-block assembly. The **validation** subsystem (the `is_valid_*` / `dbg_is_valid_*` forest generated from `aws_neuron_isa_tpb_assert.h`) then proves each emitted record is structurally legal for its opcode, engine, and silicon generation. Only records that survive both passes reach the sequencer.

This page is the **map of Part VI** — the six pages that document that two-concern model end to end. It orients them and does not duplicate their derivations: the [64-byte record format](instruction-record.md) owns the union layout; [Pseudo-Instruction Lowering](pseudo-instruction-lowering.md) owns the lowering pass; [Validator Architecture](validator-architecture.md) and [Per-Arch Validators](validators-per-arch.md) own the validation forest; [FP8 and dtype Encoding](fp8-dtype-encoding.md) owns the dtype enum the validators check against. Here we fix the shared frame: the device-vs-runtime ownership split, the lowering→validation→stage flow, the five-engine roster, and the entry points that anchor every sub-page.

For reimplementation, the contract a Part-VI reader must hold across all six pages:

- **The fixed word.** Every instruction is 64 bytes (`TPB_INST_NBYTES = 0x40`); the stream is a flat array walked in `+0x40` strides with `byte[0]` as the opcode discriminant. No framing, no length prefix, no variable width. This is the substrate both concerns operate on.
- **Lowering is host-only macro expansion.** The pseudo band (`0xC1..0xDF` + `0xA9`/`0xDD`) never reaches the device. A pseudo-opcode expands to N≥1 real records (`add_ins @0x322480` is the universal 64-byte append primitive); `PSEUDO_FUNCTION_BEGIN`/`PSEUDO_INST` are stream sentinels that are consumed, not emitted.
- **Validation is generated and doubled.** Every opcode has a silent `bool` validator and a `dbg_` twin returning `NEURON_ISA_TPB_DEBUG_BOOL` (1281 B), both emitted from one ISA-spec header, both decoding the *same* word; only per-field legality constants vary per arch (v2/v3/v4).
- **The runtime owns lowering and validation; the device owns execution.** No code on this page or its siblings touches an IOCTL, a doorbell, or device-side execution — that is [Part VII](../exec/overview.md). The boundary is the staging copy into HBM (`ib_transfer_block_to_tdram`).
- **The engine roster is five.** Lowering and validation are both keyed on a 5-engine enum `{PE=0, ACT=1, POOL=2, DVE=3, SP=4}` (`TOP_SP=5` is the collective-trigger sequencer, not a record-executing engine); per-engine tables (instruction-block usage type, staging size, kbin patch sections) index it.

| | |
|---|---|
| **Instruction word** | 64 bytes / `TPB_INST_NBYTES = 0x40`; flat array, `+0x40` stride, `byte[0]` = opcode |
| **Two host-side concerns** | pseudo-instruction **lowering** + per-record **validation** |
| **Lowering — NEFF-load pass** | `sequencer_setup_instr` `@0x4483d0` → `itf_identify_functions` `@0x2790c0` → `itf_setup_functions_one_eng` `@0x2798c0` |
| **Lowering — per-instr** | `translate_one_pseudo_instr_v2` `@0x273ff0` / `_v3` `@0x3221a0`; `translate_pseudo_instrs_partial_v2` `@0x2763d0`; append primitive `add_ins` `@0x322480` |
| **Validation — entry** | `ib_is_valid_engine_instruction_v2` `@0x2f6b80` / `_v3` `@0x3aa1e0` / `_v4` `@0x43f810` |
| **Validation — dispatchers** | silent `is_valid_neuron_instruction` `@0x2f2350`; debug `debug_invalid_neuron_isa_instruction` `@0x2ecbb0` (256-case) |
| **Engine roster** | `NEURON_ISA_TPB_NEURON_ENGINE` = `{PE 0, ACT 1, POOL 2, DVE 3, TPB_SP 4, TOP_SP 5}` |
| **Staging into HBM** | `ib_transfer_block_to_tdram` `@0x27ad80` → `dmem_alloc_aligned(TONGA_DRAM)` / `dmem_buf_copyin` |
| **Source TUs** | `tdrv/instr_tpb_functions.c`, `instr_cayman.c`, `instr_common.c`, `instr_dge.c`, `instr_pseudo_branching.c`, `instruction_block*.c`; `aws_neuron_isa_tpb_{enums,assert,util}.h` (ISA spec) |

---

## 1. The Ownership Split: Runtime Lowers and Vets, Device Executes

The cleanest way to hold the whole subsystem in mind is by *who owns what byte of the 64-byte word, when*. There are three actors and one word.

```text
COMPILER (kelf2kbin)          RUNTIME (libnrt, host)                       DEVICE (TPB sequencer)
─────────────────────         ────────────────────────────────────        ──────────────────────
emits per-engine stream  ──►  (A) LOWER  pseudo → real records       ──►   fetch record @ +0x40 stride
  real opcodes +                  itf_* / translate_* / add_*              decode byte[0] = opcode
  pseudo band 0xC1..0xDF      (B) VALIDATE each real record                run on engine PE/ACT/POOL/DVE/SP
  (+ 0xA9, 0xDD)                  is_valid_* / dbg_is_valid_*              (no host code past the doorbell)
                              (C) STAGE  host buffer → device HBM
                                  ib_transfer_block_to_tdram
```

The line between RUNTIME and DEVICE is the staging copy `ib_transfer_block_to_tdram` (`@0x27ad80`): it `dmem_alloc_aligned`s a TONGA_DRAM region and `dmem_buf_copyin`s the finished, lowered, vetted host buffer into it. Everything left of that copy is the subject of Part VI; everything right of it (the doorbell, the sequencer fetch, the completion harvest) is [Part VII — Execution](../exec/overview.md). No function documented across the six Part-VI pages issues an IOCTL, takes a device lock, or touches a doorbell.

This split is *why* the lowering and validation forests are so large and yet touch no hardware: they are pure host-side transforms over a byte buffer. The lowering family is a tree of `add_*` record-emitters built on one 64-byte `memmove` primitive (`add_ins @0x322480`, 78 callers); the validation family is a forest of side-effect-free `bool` predicates over the union and its sub-fields. Neither owns a single device-state mutation.

> **NOTE —** the runtime owns the *encoding correctness* of the stream, not its *execution*. A record that passes validation can still produce a wrong tensor — validation proves the opcode/operands are structurally legal (defined enum values, in-range addresses, consistent dtype↔size, reserved bits zero), not that the computation is what the model intended. Semantic correctness is the compiler's contract; the runtime is the last *structural* gate before HBM.

---

## 2. The Lowering→Validation→Stage Flow

A NEFF's per-engine instruction stream becomes a device-resident sequencer program in the stages below. Stages 1–2 are the lowering pass; stage 3 is validation; stage 4 is staging. The pass runs **once per engine per model load**, driven from `sequencer_setup_instr` (`@0x4483d0`, the sole caller of the lowering entry points).

```text
NEFF per-engine instruction stream  (real opcodes interleaved with pseudo band 0xC1..0xDF)
        │
  [1]  IDENTIFY FUNCTIONS                      itf_identify_functions @0x2790c0
        │   walk +0x40 stride; split at PSEUDO_FUNCTION_BEGIN (0xD1)
        │   build itf_function_t (name, return regs, reset-sema flag) keyed in a ht
        │   split each function into itf_block_t at 0xD1 / 0xCC; mark CC blocks (has_ccops)
        │
  [2]  LOWER PSEUDO-INSTRUCTIONS               itf_setup_functions_one_eng @0x2798c0
        │   per function, per block:
        │     translate_pseudo_instrs_partial_v2 @0x2763d0  ── the heavy per-block lowering
        │       ├─ translate_one_pseudo_instr_v2 @0x273ff0 / _v3 @0x3221a0  (single-instr demux)
        │       │     CALL/RETURN frame, JPEG CC-top, DGE direct2d/gather-transpose, addr8 xlate
        │       ├─ translate_one_pseudo_swap_queue_set_v2 → psqs_insert_qsi_swap @0x278c30
        │       └─ add_ins @0x322480 (×N)   ── append each real 64-byte record to the block buf
        │   imcpy vring commit; branch-label / prefetch-hint resolution (ipb_postprocess_instrs)
        │
  [3]  VALIDATE EACH RECORD                    ib_is_valid_engine_instruction_v{2,3,4}
        │     ├─ ib_hal_to_isa_engine  ── HAL engine id → NEURON_ISA_TPB engine
        │     ├─ instruction_engine_check ── opcode legal ON this engine?
        │     └─ is_valid_neuron_instruction @0x2f2350 ── SILENT per-opcode field gate (bool)
        │           (on failure: debug_invalid_neuron_isa_instruction @0x2ecbb0 → named reason)
        │
  [4]  STAGE INTO DEVICE HBM                    ib_transfer_block_to_tdram @0x27ad80
              alignment = max(0x8000, gconf->seq_instr_block_size[eng])
              dmem_alloc_aligned(TONGA_DRAM, usage = dword_9DAB80[eng]) ; dmem_buf_copyin
              ─────────────────────────── RUNTIME │ DEVICE boundary ───────────────────────────
```

Two facts make this flow reimplementable rather than a black box. First, the lowering is *structural*, not interpretive: `itf_identify_functions` reads only `byte[0]` (split sentinel `0xD1`) and the block-boundary opcodes (`0xD1`/`0xCC`) to partition the stream, and reads three fixed bytes of the `PSEUDO_FUNCTION_BEGIN` record (`+48` reset-sema, `+49`/`+50` return-addr regs) to build the call frame — no opcode-semantic knowledge is needed to split. Second, the validation runs on the *already-lowered* stream: by stage 3 the pseudo band is gone, so the validators only ever see real opcodes (`is_valid_neuron_instruction` rejects an unknown opcode, which a surviving pseudo would be). The final-record check (`ib_final_inst_validity_check`, see [record format §6](instruction-record.md#6-instruction-block-io-ib_final_inst_validity_check)) additionally asserts the stream ends in a legal terminator.

> **GOTCHA —** the stream stride is *always* `0x40`, even though each record carries an `inst_word_len` byte at `+1`. Every walker in this subsystem — the lowering splitter, the EXIT scanner, the validators — advances by `TPB_INST_NBYTES = 0x40`. A reimplementation that strides by `inst_word_len` desynchronizes the stream on the first record whose length tag is not 16. The length byte is a sequencer-fetch hint, never the stream stride.

---

## 3. The Two Concerns at a Glance

The table fixes the two host-side concerns against their entry points, the word region each touches, and the page that owns the detail. It is the index a reimplementer returns to when deciding which sub-page answers a question.

| Concern | Owns | Entry point(s) | Word region touched | Page |
|---|---|---|---|---|
| **Lowering** (macro-expand) | pseudo band `0xC1..0xDF` + `0xA9`/`0xDD` → real records | `itf_identify_functions` `@0x2790c0`; `translate_one_pseudo_instr_v2` `@0x273ff0` | whole record (emits new 64-B records) | [Pseudo-Instruction Lowering](pseudo-instruction-lowering.md) |
| **Validation** (encode-assert) | every real record's legality | `ib_is_valid_engine_instruction_v{2,3,4}` `@0x2f6b80`/`@0x3aa1e0`/`@0x43f810` | every field, per opcode + engine + arch | [Validator Architecture](validator-architecture.md) |
| ↳ silent gate | the `bool` the load path gates on | `is_valid_neuron_instruction` `@0x2f2350` | header + events + per-opcode fields | [Validator Architecture](validator-architecture.md) |
| ↳ debug twin | the named failure reason | `debug_invalid_neuron_isa_instruction` `@0x2ecbb0` | same checks, returns `DEBUG_BOOL` | [Validator Architecture](validator-architecture.md) |
| ↳ per-arch deltas | v2/v3/v4 legality-constant clones | three validator bands (`0x28xxxx`/`0x32xxxx`/`0x3bxxxx`) | per-field legality + address apertures | [Per-Arch Validators](validators-per-arch.md) |
| **Substrate** (the word itself) | the 64-byte union + opcode taxonomy | `NEURON_ISA_TPB_HEADER`/`EVENTS` + 118 arms (DWARF) | the full record layout | [Instruction Record](instruction-record.md) |
| **dtype sub-concern** | the `DTYPE` enum the validators check | `NEURON_ISA_TPB_DTYPE_0` (DWARF) | `in_dtype`/`out_dtype` payload bytes | [FP8 / dtype Encoding](fp8-dtype-encoding.md) |

The lowering and validation forests share leaf vocabulary — both reach `is_valid_enum`, `is_valid_dtype`, and `type_size_check`, and both consume the same `NEURON_ISA_TPB_*` enums — but they run at different stages and have opposite shapes: lowering is a *producing* tree (one pseudo in, N real records out), validation is a *deciding* forest (one record in, one bit out). Holding both as transforms over the same 64-byte word is the core mental model of Part VI.

---

## 4. The Shape of Lowering: a Producing Tree on One Append Primitive

Lowering is the runtime turning each pseudo-opcode into the real records the device can fetch. The defining structural fact is that **every emitted record passes through one primitive**: `add_ins` (`@0x322480`, 78 callers) is a `memmove(dst+offset, record, 0x40); return offset+64` — the universal 64-byte append. The entire `add_*` family (`add_mov32_imm`, `add_sync_barrier`, `add_sema_reset`, `add_dma_rearm_bcast`, `add_br_reg`, `add_wr32_v3`, …) builds a 64-byte record in a stack buffer and calls `add_ins` to append it. A reimplementer reproduces lowering as a set of record-builders over one buffer-append function, not as a monolithic codegen.

The expansion is one-pseudo-to-many: a single pseudo-opcode lowers to a *sequence* of real records. The contract per pseudo class (detail and offsets in [Pseudo-Instruction Lowering](pseudo-instruction-lowering.md)):

| Pseudo-opcode | Lowers to (real-record sequence) | Lowering function |
|---|---|---|
| `PSEUDO_FUNCTION_BEGIN` `0xD1` | *consumed* — splits the stream into `itf_function_t`; emits nothing | `itf_identify_functions` `@0x2790c0` |
| `PSEUDO_FUNCTION_CALL` `0xD3` | TOPSP block-switch + ack-event waits + reg-writes + optional args-table swap + queue-set swap + return-addr save | `itf_translate_function_call_instr` `@0x279f10` |
| `PSEUDO_FUNCTION_RETURN` `0xD2` | drain + sync-barrier (+ optional sema-reset + 2nd barrier) + `add_br_reg` to return address | `itf_translate_function_return_instr` `@0x27a6d0` |
| `PSEUDO_DMASWAP_QUEUE_SET` `0xCF` | drain + per-DMA sema-reset loop + queue-set-swap body + trailing drain | `psqs_insert_qsi_swap` `@0x278c30` |
| `PSEUDO_DMA_DIRECT2D` `0xD4` (+`0xDA` ext) | one `DMA_TRANSPOSE` `0xBD` record + DGE bound-check MOVs | `translate_pseudo_dma_direct2d_xpose_instr_v3` `@0x321ac0` |
| `PSEUDO_JPEG_DECODE` `0xD7` | CC-top queue request + `add_wr32_v3` CSR-write notify (TOP_SP, device_tpb_idx==0 only) | `translate_one_pseudo_instr_v3` `@0x3221a0` |
| `PSEUDO_BRANCH_PREFETCH_HINT` `0xDD` | resolved in a second pass against the `COMPARE_BRANCH` `0xA9` target; rel-immediate patched in place | `ipb_postprocess_instrs` `@0x3237a0` |

Two structural consequences fall out of this shape. First, lowering needs a **post-pass**: branch labels and prefetch hints reference records whose final offsets are only known after every function is laid into the final buffer, so `ipb_postprocess_instrs` walks the assembled stream a second time to patch `COMPARE_BRANCH` rel32 targets and rewrite prefetch-hint records. Second, the call/return frame is built from **three bytes of the `PSEUDO_FUNCTION_BEGIN` record** (`+48` reset-sema flag, `+49`/`+50` return-addr reg lo/hi) read during stage 1 — the lowering pass needs no separate metadata table, just the record's own payload.

> **QUIRK —** the per-block buffers are sized for a 64-byte unit but the lowering of one pseudo can emit many records; `ib_insert_common_postamble` asserts the running offset stays `<= 0x10000` before each append. A reimplementer who assumes one pseudo maps to one record will under-size every block buffer and mis-place every branch target.

---

## 5. The Shape of Validation: a Deciding Forest, Doubled

Validation is the inverse shape: one real record in, one legality bit out. Its defining fact is that **every validator exists twice** — a silent `is_valid_<op>` returning `bool` (the fast path the load actually gates on) and a `dbg_is_valid_<op>` twin returning a 1281-byte `NEURON_ISA_TPB_DEBUG_BOOL` that names the failing assertion. Both run the *identical* check tree; the twin differs only by carrying the diagnostic message machinery (an inlined `qmemcpy(…, 0x500)` of the err-msg matrix), which is why a silent leaf like `is_valid_dtype` is 46 bytes while its twin is 2233. Both families are **generated** from `aws_neuron_isa_tpb_assert.h`, once per opcode, with a v2/v3/v4 clone per silicon target.

The forest is gated through a single entry per generation. `ib_is_valid_engine_instruction_v{2,3,4}` maps the HAL engine id to an ISA engine, asserts the opcode is legal on that engine (`instruction_engine_check` — a matmul on the SP engine is malformed regardless of fields), then runs the silent dispatcher `is_valid_neuron_instruction` (`@0x2f2350`, a switch on `byte[0]` whose every arm is an inlined per-opcode predicate). Only on failure does the load path re-run through `debug_invalid_neuron_isa_instruction` (`@0x2ecbb0`, a 256-case opcode jump table) to turn the rejected bit into a named reason. Every per-opcode validator ANDs a shared preamble — `has_valid_neuron_header` (opcode-enum + `inst_word_len==16`) and `has_valid_neuron_events` (the wait/update sync tuple) — with opcode-specific field/enum/reserved-zero checks, merged into one result.

> **NOTE —** for a reimplementation that does not need failure diagnostics, the entire `dbg_*` forest and the `DEBUG_BOOL` machinery are omittable — the silent forest alone is a complete, sound validator. The two forests cannot disagree, because both compile the *same* predicate tree from one generated source. Full entry tree, the `DEBUG_BOOL` ABI, and a representative validator/twin pair are on [Validator Architecture](validator-architecture.md).

---

## 6. The Engine Roster

Both concerns are keyed on the engine. The ISA defines six `NEURON_ISA_TPB_NEURON_ENGINE` values, but only five execute records; `TOP_SP` is the collective-trigger sequencer that drives CC-top operations (e.g. the `PSEUDO_JPEG_DECODE` path), not a per-record datapath. The five record-executing engines are the dimension every per-engine table indexes — the instruction-block usage type, the staging block size, and the kbin patch section all carry one slot per engine.

| Engine | Enum value | Role (record class executed) | Per-engine note |
|---|---|---|---|
| **PE** | `_PE = 0` | matmul / PE array (LDWEIGHTS, MATMUL, sparse/MX variants) | DMA-usage slot `dword_9DAB80[0] = 1` |
| **ACT** | `_ACT = 1` | activation, quantize, table-load, exp | postamble also rearms DYNAMIC_ACT_TBL rings (`ib_insert_common_postamble`) |
| **POOL** | `_POOL = 2` | pooling / tensor-arith / cast / reduce | DMA-usage slot `dword_9DAB80[2] = 3` |
| **DVE** | `_DVE = 3` | batch-norm / gather / shuffle / DVE read-accumulator | DMA-usage slot `dword_9DAB80[3] = 9` |
| **SP** | `_TPB_SP = 4` | sync / control / DMA dispatch (DRAIN, NOTIFY, branch, DMAMEMCPY) | DMA-usage slot `dword_9DAB80[4] = 10` |
| *(TOP_SP)* | `_TOP_SP = 5` | collective-trigger sequencer (CC-top), **not** a record-executing engine | drives block-switch / ack-event waits, not a `dword_9DAB80` slot |

The `dword_9DAB80[5] = {1,2,3,9,10}` table (read from `.rodata @0x9dab80`, HIGH) maps engine index 0..4 to the `DMA_MEM_USAGE_TYPE` fed to `dmem_alloc_aligned` when staging that engine's block (stage 4). The kbin patch info per block is a `kbin_eng_patch_t[5]` — one slot per engine — and the staging alignment reads `gconf->seq_instr_block_size[eng]` per engine. A reimplementer building the per-engine instruction-block table reproduces exactly this 5-wide indexing; the sixth enum value never indexes these arrays.

> **CORRECTION (ISA-OVERVIEW-1) —** the [record-format sibling page](instruction-record.md#real-opcode-value-clusters-70-values-abbreviated-by-range) lists `JPEG_DECODE = 0xFF` alongside `INVALID = 0xFF`. The DWARF `NEURON_ISA_TPB_OPCODE` enum is authoritative: real `JPEG_DECODE` is **`0x81`** and `PSEUDO_JPEG_DECODE` (the host-lowered CC-top form on TOP_SP) is **`0xD7`**; `0xFF` is the terminal `INVALID`/sentinel only. A reimplementer keying the JPEG-decode lowering off `0xFF` would never match the real opcode. (Engine enum, opcode bytes, and the lowering paths cross-checked against `enums.json` + `functions.json`; the byte values `PSEUDO_FUNCTION_BEGIN 0xD1`, `RETURN 0xD2`, `CALL 0xD3`, `PSEUDO_JPEG_DECODE 0xD7`, `PSEUDO_INST 0xDF`, `COMPARE_BRANCH 0xA9` all agree across both pages.)

---

## 7. Where Each Sub-Page Picks Up

Part VI is six pages. This map is the orientation layer; each page below owns one slice of the lowering-or-validation model and carries the byte-level derivation this page deliberately omits.

| Page | Owns | Key anchors |
|---|---|---|
| [The 64-Byte Instruction Record Format](instruction-record.md) | the union substrate both concerns operate on | `TPB_INST_NBYTES = 0x40`; 12-B preamble (`HEADER`+`EVENTS`); 118 arms; opcode taxonomy; `ib_read_write`/`ib_has_pseudo_exit`/`ib_final_inst_validity_check` |
| [Pseudo-Instruction Lowering](pseudo-instruction-lowering.md) | the host-only macro-expansion pass (concern A) | `itf_identify_functions` `@0x2790c0`; `itf_setup_functions_one_eng` `@0x2798c0`; `translate_one_pseudo_instr_v{2,3}`; `add_ins` `@0x322480`; CALL/RETURN/JPEG/DGE/swap-queue lowering |
| [FP8 and dtype Encoding](fp8-dtype-encoding.md) | the `DTYPE` enum the record's dtype fields select | `NEURON_ISA_TPB_DTYPE_0` (0=INVALID … 13/14/15 = FP8_E3/E4/E5, 16 FP4_E2); `type_size_check` 1/2/4/8-B legality |
| [ISA Validator Architecture](validator-architecture.md) | the two-family validation forest (concern B) | silent `is_valid_neuron_instruction` `@0x2f2350`; debug `debug_invalid_neuron_isa_instruction` `@0x2ecbb0`; `DEBUG_BOOL` (1281 B); `is_valid_enum`/`is_valid_dtype` leaves |
| [Per-Arch Validators](validators-per-arch.md) | the v2/v3/v4 legality-constant clones | `ib_is_valid_engine_instruction_v{2,3,4}`; the three validator bands; per-arch address apertures + `is_valid_enum`/`type_size_check` arch copies |
| [The Load Pipeline](../neff/load-pipeline.md) | the model-load stage that drives all of the above | `sequencer_setup_instr` `@0x4483d0`; where lowered+vetted streams enter device HBM via `ib_transfer_block_to_tdram` |

A reader who internalizes this page can navigate the six in any order: lowering and validation are independent transforms over the same word, joined only at the load-pipeline driver (stage 0) and the staging copy (stage 4). The record-format page is the prerequisite for all five others; the FP8/dtype page is a leaf shared by both concerns.

---

## Cross-References

- [The 64-Byte Instruction Record Format](instruction-record.md) — the `NEURON_ISA_TPB_INST_UNION` substrate; opcode→struct dispatch and the `ib_*` stream I/O
- [ISA Validator Architecture and Entry Tree](validator-architecture.md) — the silent/debug validator forest, `DEBUG_BOOL`, and `is_valid_enum`
- [Pseudo-Instruction Lowering (SP / TopSP Builders)](pseudo-instruction-lowering.md) — how the `0xC1..0xDF` pseudo band is rewritten into real records before device fetch
- [Per-Arch Instruction Validators (SUNDA / CAYMAN / MARIANA Deltas)](validators-per-arch.md) — the v2/v3/v4 validator clones and per-arch legality/address-aperture deltas
- [FP8 and dtype Encoding](fp8-dtype-encoding.md) — the `NEURON_ISA_TPB_DTYPE` enum the record's dtype fields select from
- [The Load Pipeline (Parse → Build → Stage → Relocate)](../neff/load-pipeline.md) — the model-load driver (`sequencer_setup_instr`) that runs lowering, validation, and staging over each engine's stream
- [back to index](../index.md)
