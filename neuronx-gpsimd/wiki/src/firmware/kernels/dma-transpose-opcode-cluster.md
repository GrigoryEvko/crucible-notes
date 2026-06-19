# DMA / Transpose Opcode Cluster — `0xb8` · `0xb9` · `0xba` · `0xbd` · `0xf1` · `0xb4` · `0xb6`

This page is the **kernel-opcode view of GPSIMD data movement**. It decodes the seven TPB
instruction opcodes that GX-OP-08's TIER-D grouped under "DMA / transpose" by enum-byte
adjacency, and then assembles the **complete GPSIMD data-movement opcode map** by folding in
`DMA_INDIRECT (0xbb)`, the sequencer scalar load/store pair (`0xaa`/`0xab`), the
Descriptor-Generation Engine (DGE), and the `al_udma` M2S/S2M SDMA hardware.

The descriptor / engine *view* of the same machinery is the **DMA Part (Part 9)**; the
bottom-of-stack DGE micro-op emit is on the committed [DGE Descriptor-Emit Path](../dge/dge-emit.md),
the backend choice on [DGE 3-Backend Selector](../dge/dge-backend-selector.md), and the transpose
stride-permute on [DGE Reshape Engine](../dge/dge-reshape.md). This page does **not** re-derive
those — it documents the instruction-word front door.

> **PREMISE CORRECTION — two of the seven are *not* DMA.** The struct→opcode binding and the
> operand structs (§3) prove that **`0xb4 TEST_EVENT_SEM`** and **`0xb6 COMPACT_CONTROL_INST`**
> are **NX SEQUENCER control-spine** ops (a semaphore/event batch test-update; a packed
> scalar-bytecode bundle). They sit in the `0xb0..0xb6` sem/branch/control sub-band, *adjacent
> to* but *distinct from* the `0xb8..0xbd` DMA sub-band. The genuine DMA/transpose cluster is the
> **five** `{0xb8, 0xb9, 0xba, 0xbd, 0xf1}`. Both control ops are still fully decoded below. `[HIGH/OBSERVED]`

> **PREMISE CORRECTION — there are three transposes, not one.** `0xbd`/`0xf1` are
> *descriptor-level* DMA-xbar transposes (the crossbar transposes during the transfer). They are
> **not** the DVE datapath transpose `0x6b STREAM_TRANSPOSE` (lane-permute in the vector pipe;
> see [StreamTranspose](./stream-transpose.md)). Any hypothesis equating `0x6b` with one of the
> `0xb9/0xba/0xbd/0xb4/0xb6` bytes is false — the opcode bytes are disjoint. `[HIGH/OBSERVED]`

> **NOTE — provenance.** Primary facts derive from the shipped customop-lib package
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`: the four per-gen arch-isa C headers under
> `…/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/` (the authoritative opcode /
> operand-struct / enum / validity contract — `aws_neuron_isa_tpb_common.h`, the seven operand
> `*.h` files), the per-gen `instruction_mapping.json` (`struct2opcode` binding), and a `gcc`
> struct compile-verify (`sizeof`/`offsetof`) against those headers. Dispatch facts (POOL
> `kernel_info_table` membership, the SUNDA `dma_memcopy` funcVA, the DGE GENERATE/DIMPUSH model,
> the M2S/S2M roles) are carried from the prior firmware decodes at their stated confidence.
> `v2..v4` (SUNDA / CAYMAN / MARIANA) are byte-grounded; `v5` MAVERICK is **header-OBSERVED** —
> the ISA contract is byte-exact, but the MAVERICK device-blob interiors are not re-disassembled,
> so MAVERICK dispatch interiors are flagged **INFERRED**.

Confidence convention: `[HIGH/OBSERVED]` = read directly from byte / header / `struct2opcode` /
compile-verify this pass; `[MED/INFERRED]` = reasoned over an OBSERVED fact; `[…/CARRIED]` =
re-used from a sibling firmware decode at its confidence without re-tracing the artifact here.

---

## 1. The seven names + per-generation presence `[HIGH/OBSERVED]`

The opcode bytes and the per-gen `// Y` maintained-flag come **byte-exact** from each generation's
`NEURON_ISA_TPB_OPCODE` enum in `aws_neuron_isa_tpb_common.h` — the enum identifier is the
authoritative mnemonic, **not** inferred from the opcode number.

| op     | enum identifier (`…OPCODE_…`)         | SUNDA v2 | CAYMAN v3 | MARIANA v4 | MAVERICK v5 | first gen | maverick line |
| ------ | ------------------------------------- | :------: | :-------: | :--------: | :---------: | --------- | ------------- |
| `0xb8` | `DMAMEMCPY`                            | **Y**    | **Y**     | **Y**      | **Y**       | SUNDA     | `common.h:267` |
| `0xb9` | `DMA_MEMCPY2`                          | —        | —         | —          | **Y**       | MAVERICK  | `common.h:268` |
| `0xba` | `DMA_IMMEDIATE`                        | —        | —         | —          | **Y**       | MAVERICK  | `common.h:269` |
| `0xbd` | `DMA_TRANSPOSE`                        | —        | **Y**     | **Y**      | **Y**       | CAYMAN    | `common.h:272` |
| `0xf1` | `DMA_GATHER_TRANSPOSE`                 | —        | **Y**     | **Y**      | **Y**       | CAYMAN    | `common.h:318` |
| `0xb4` | `TEST_EVENT_SEM`                       | —        | —         | **Y**      | **Y**       | MARIANA   | `common.h:264` |
| `0xb6` | `COMPACT_CONTROL_INST`                 | —        | —         | —          | **Y**       | MAVERICK  | `common.h:266` |

Per-gen enum line refs (`rg -n 'OPCODE_<x> ' aws_neuron_isa_tpb_common.h`, all four gens this pass):
SUNDA `0xb8@260`; CAYMAN `0xb8@257`, `0xbd@260`, `0xf1@302`; MARIANA `0xb4@261`, `0xb8@263`,
`0xbd@266`, `0xf1@312`; MAVERICK as tabled. SUNDA's `common.h` has **zero** hits for any of
`DMA_MEMCPY2 / DMA_IMMEDIATE / DMA_TRANSPOSE / DMA_GATHER_TRANSPOSE / TEST_EVENT_SEM /
COMPACT_CONTROL_INST` — confirming SUNDA carries only `DMAMEMCPY` in this cluster. `[HIGH/OBSERVED]`

The contiguous DMA sub-band (MAVERICK superset; `0xb7` is the band boundary, **absent**):

```
0xb3 POLL_SEM | 0xb4 TEST_EVENT_SEM | 0xb5 BRANCH_PREFETCH_HINT | 0xb6 COMPACT_CONTROL_INST
  | (0xb7 absent) |
0xb8 DMAMEMCPY | 0xb9 DMA_MEMCPY2 | 0xba DMA_IMMEDIATE | 0xbb DMA_INDIRECT |
0xbc RANGE_SELECT | 0xbd DMA_TRANSPOSE | 0xbe GET_SEQUENCE_BOUNDS | 0xbf SB2SB_COLLECTIVE
```

> **NOTE — the 5-column ledger.** The committed [Opcode Catalog Ledger](./opcode-catalog-ledger.md)
> shows these rows with a **5**-character gen flag (e.g. `0xB8 … YYYYY`) because that ledger splits
> MARIANA into MARIANA + MARIANA_PLUS (v4 / v4+). This page uses the **four** byte-grounded header
> directories (SUNDA / CAYMAN / MARIANA / MAVERICK); MARIANA_PLUS is a v4 sub-step that ships the
> same ISA contract. The two are consistent — `0xb8` is `Y` in every gen; `0xbd`/`0xf1` add at v3;
> `0xb4` at v4; `0xb6`/`0xb9`/`0xba` at v5. `[HIGH/OBSERVED]`

---

## 2. The struct binding — `struct2opcode` + compile-verify (all 64 B) `[HIGH/OBSERVED]`

`instruction_mapping.json`'s `struct2opcode` table is the authoritative opcode→operand-struct
binding. Each struct's **defining-gen set matches the opcode's presence set exactly** — the ISA
adds the opcode and its struct together. Every struct is 64 bytes by `gcc` compile-verify this pass.

| op     | operand struct (`NEURON_ISA_TPB_…`) | `struct2opcode` key (JSON) | `sizeof` | defining gens |
| ------ | ----------------------------------- | -------------------------- | :------: | ------------- |
| `0xb8` | `DMA_DIRECT2D_STRUCT`               | `OPCODE_DMA_MEMCPY`        | 64       | su ca ma mv   |
| `0xb9` | `DMA_COPY2D_STRUCT`                 | `OPCODE_DMA_MEMCPY2`       | 64       | mv only       |
| `0xba` | `DMA_IMMEDIATE_STRUCT`             | `OPCODE_DMA_IMMEDIATE`     | 64       | mv only       |
| `0xbd` | `DMA_DIRECT2D_XPOSE_STRUCT`        | `OPCODE_DMA_TRANSPOSE`     | 64       | ca ma mv      |
| `0xf1` | `DMA_GATHER_XPOSE_STRUCT`          | `OPCODE_DMA_GATHER_TRANSPOSE` | 64    | ca ma mv      |
| `0xb4` | `CTRL_TEST_ES_STRUCT`             | `OPCODE_TEST_EVENT_SEM`    | 64       | ma mv         |
| `0xb6` | `CTRL_CCI_STRUCT`                 | `OPCODE_COMPACT_CONTROL_INST` | 64    | mv only       |

> **GOTCHA — the JSON key for `0xb8` is `OPCODE_DMA_MEMCPY`, the enum is `OPCODE_DMAMEMCPY`.** Same
> `0xb8` opcode, two spellings of the *name* (no underscore in the enum, underscore in the JSON key
> and in the struct's docstring: *"DmaMemcpy Instruction"*). Resolve by the opcode byte, not the
> string. `[HIGH/OBSERVED]`

> **NOTE — the compiler-side pseudo.** A `PSEUDO_DMA_DIRECT2D = 0xd4` opcode
> (`common.h:294`, struct `PSEUDO_DMA_DIRECT2D_STRUCT`) is the symbolic, unresolved-address form of
> `0xb8` that the compiler emits and NRT lowers to the real `0xb8 DMA_DIRECT2D` at bind time —
> exactly the pseudo/real relationship `0xce → 0xaa` has on the [TensorLoad](./tensorload.md) page.
> `0xd4` is not in this cluster's byte band and is not decoded here. `[HIGH/OBSERVED]`

Compile-verify (`gcc -I …/neuron_maverick_arch_isa/tpb`, all seven structs):

```
NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT           sizeof=64   src_start_addr +16  dst_start_addr +40  in_dtype +62  out_dtype +63
NEURON_ISA_TPB_DMA_COPY2D_STRUCT             sizeof=64
NEURON_ISA_TPB_DMA_IMMEDIATE_STRUCT          sizeof=64
NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT     sizeof=64   src_start_addr +16  dst_start_addr +40
NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT       sizeof=64   src_idx_start_addr +36  dst_start_addr +40
NEURON_ISA_TPB_CTRL_TEST_ES_STRUCT           sizeof=64   evt_sem_ids +16  value_registers +32  threshold +48
NEURON_ISA_TPB_CTRL_CCI_STRUCT               sizeof=64   insts +4
```

---

## 3. The operand structs, field by field

All offsets below are the in-header `( lo - hi )` byte annotations, cross-checked against
`offsetof`. `ADDR8` is the 8-byte full-Neuron-address union; `ADDR4` is its 4-byte form;
`EVENTS` (8 B) is the standard wait/update-semaphore sync block; `DMA_CONFIGS` (1 B) is
`priority_class:3` + reserved.

### 3a. `0xb8 DMA_DIRECT2D_STRUCT` — the canonical 2-D mover `[HIGH/OBSERVED]`

Header docstring (`dma_direct2d.h`): *"DmaMemcpy … will generate DMA descriptors to initiate a
Memcpy. The descriptors will be supplied by the DGE block and the TPB engine will update the DMA
queue tail pointers when the descriptors are ready."*

| off | size | field             | type                   | role |
| --- | ---- | ----------------- | ---------------------- | ---- |
| 0   | 4    | `header`          | `HEADER`               | `opcode = 0xb8`; inst length; debug |
| 4   | 8    | `events`          | `EVENTS`               | wait/update semaphore sync |
| 12  | 1    | `dma_configs`     | `DMA_CONFIGS`          | `priority_class:3` |
| 13  | 1    | `semaphore`       | `uint8_t`              | completion semaphore index |
| 14  | 1    | `sem_increment`   | `uint8_t`              | semaphore increment on completion |
| 15  | 1    | `compute_op`      | `DGE_COMPUTE_OP`       | **reduce mode** (NONE / ADD / MUL / MAX / MIN) |
| 16  | 8    | `src_start_addr`  | `ADDR8`                | src base (HBM / SBUF / PSUM; imm / reg / table) |
| 24  | 8    | `src_step_elem[2]`| `int32_t[2]`           | signed 2-D strides |
| 32  | 4    | `src_num_elem[2]` | `uint16_t[2]`          | 2-D element counts |
| 36  | 2    | `src_elem_size`   | `uint16_t`             | bytes per element (src) |
| 38  | 1    | `src_bound_reg`   | `BOUND_CHECK_REG`      | src bounds-check register |
| 39  | 1    | `dst_bound_reg`   | `BOUND_CHECK_REG`      | dst bounds-check register |
| 40  | 8    | `dst_start_addr`  | `ADDR8`                | dst base |
| 48  | 8    | `dst_step_elem[2]`| `int32_t[2]`           | signed 2-D strides |
| 56  | 4    | `dst_num_elem[2]` | `uint16_t[2]`          | 2-D element counts |
| 60  | 2    | `dst_elem_size`   | `uint16_t`             | bytes per element (dst) |
| 62  | 1    | `in_dtype`        | `DTYPE`                | input dtype |
| 63  | 1    | `out_dtype`       | `DTYPE`                | output dtype |

A 2-D strided src tensor → 2-D strided dst tensor, per-element `elem_size`, optional in-flight
reduce (`compute_op`), bounds-checked. The `compute_op` enum (`common.h:1000`) is exactly five
ops: `NONE=0 (B=A) / ADD=1 (B+=A) / MULTIPLY=2 / MAX=3 / MIN=4` — this is the **scatter/reduce-add
DMA** the DGE Pool backend implements. The validity contract gates a CCE (compute-DMA) path
(`is_valid_dma_cce`: aligned start addresses + aligned steps + element-size check) when
`compute_op != NONE`. `[HIGH/OBSERVED]`

### 3b. `0xb9 DMA_COPY2D_STRUCT` — the "next-gen" mover (MAVERICK) `[HIGH/OBSERVED]`

Header docstring (`dma_copy2d.h`): *"Next-generation DMA copy instruction with explicit semaphore
wait/update, configurable DMA engine selection, and loop-friendly address modes."*

| off | size | field                  | type                  | role |
| --- | ---- | ---------------------- | --------------------- | ---- |
| 0   | 4    | `header`               | `HEADER`              | `opcode = 0xb9` |
| 4   | 4    | `wait_sema_value`      | `uint32_t`            | wait threshold (imm or reg) |
| 8   | 1    | `wait_sema_idx`        | `uint8_t`             | which semaphore to wait on |
| 9   | 1    | `wait_sema_mode`       | `WAIT_MODE`           | wait condition |
| 10  | 1    | `dma_engines`          | `DMA_ENGINE_CONFIG`   | `start_id:5` + `engine_count:2` (1/2/4/8) |
| 11  | 1    | `dma_flags`            | `DMA_FLAGS`           | `tdg:1` + `wr_done_sync:1` + `queue_id:3` |
| 12  | 1    | `addr_mode`            | `DMA_ADDR_MODE_PAIR`  | src 4 b + dst 4 b (12 modes, §3b-note) |
| 13  | 1    | `sema_update_mode`     | `DMA_SEMA_UPDATES`    | `rd_done:4` + `wr_done:4` update policy |
| 14  | 2    | `rd_done_sema_update_idx` | `uint16_t`         | read-done semaphore |
| 16  | 8    | `src_start_addr`       | `DMA_ADDR_UNION`      | src base (mode-dependent) |
| 24  | 8    | `src_step_elem[2]`     | `int32_t[2]`          | strides |
| 32  | 4    | `src_num_elem[2]`      | `uint16_t[2]`         | counts |
| 36  | 2    | `src_elem_size`        | `uint16_t`            | bytes/elem |
| 38  | 2    | `wr_done_sema_update_idx` | `uint16_t`         | write-done semaphore |
| 40  | 8    | `dst_start_addr`       | `DMA_ADDR_UNION`      | dst base |
| 48  | 8    | `dst_step_elem[2]`     | `int32_t[2]`          | strides |
| 56  | 4    | `dst_num_elem[2]`      | `uint16_t[2]`         | counts |
| 60  | 2    | `dst_elem_size`        | `uint16_t`            | bytes/elem |
| 62  | 1    | `remote_core_id`       | `REG_NUM`             | **target a remote NeuronCore** |
| 63  | 1    | `reserved`             | `uint8_t`             | — |

What `0xb9` adds over `0xb8`: a **built-in wait-on-semaphore prologue** (`wait_sema_*`); **explicit
DMA-engine bank selection** (`start_id` + `engine_count` of 1/2/4/8 from `DMA_ENGINE_COUNT
{ONE=0, TWO=1, FOUR=2, EIGHT=3}`); **loop-friendly indexed addressing** (the `DMA_ADDR_MODE_PAIR` —
no separate address-compute instruction); **separate `rd_done` / `wr_done` completion semaphores**
(`DMA_SEMA_UPDATE_MODE`: `NONE / LOCAL_SEM_INC / LOCAL_SEM_INC_COUNTER / LOCAL_COLLSYNC_INC /
REMOTE_SEM_INC / REMOTE_SEM_INC_COUNTER / REMOTE_COLLSYNC_INC` — local **or remote** sem/collsync
increment); and **`remote_core_id`** — cross-core DMA into a peer NeuronCore's memory. `[HIGH/OBSERVED]`

> **GOTCHA — `dma_flags.queue_id` is 3 bits, not 4.** `DMA_FLAGS` (`common.h:1041`) is
> `tdg:1 / wr_done_sync:1 (SEMA_SYNC) / queue_id:3 / reserved:3` — eight DMA queues, not sixteen.
> (The struct's inline comment "4b queue ID" is stale; the field is 3 bits.) `[HIGH/OBSERVED]`

The 12-entry `DMA_ADDR_MODE` enum (`common.h:1061`) per addressing leg (`addr_mode` packs src in
the low nibble, dst in the high):
`IMM64=0x0 / REG_PAIR=0x1 / REG_IMM_OFFSET32=0x2 / …32I=0x3 / REG_REG_OFFSET32=0x4 / …32I=0x5 /
REG_REG_OFFSET64=0x6 / …64I=0x7 / REG_REG_IMM_SCALE=0x8 / …_I=0x9 / REG_REG_REG_SCALE=0xa / …_I=0xb`
— immediate, register-pair, register+immediate-offset, register+register-offset (32/64-bit, signed
`I` variants), and scaled-index modes. `WAIT_MODE` ranges `NONE=0x0 / EQ/LT/LE/GT/GE_IMM=0x1..0x5 /
EQ/LT/LE/GT/GE_REG=0x81..0x85 / …_REG_OFFSET=0x91..0x95` (immediate / register / register-offset
threshold compares).

### 3c. `0xba DMA_IMMEDIATE_STRUCT` — raw-descriptor injection (MAVERICK) `[HIGH/OBSERVED]`

Header docstring (`dma_immediate.h`): *"send 1-3 immediate descriptors to DGE"*, with
`dma_engines.engine_count` *"must be 1"* and `dma_flags.tdg` / `wr_done_sync` *"must be zero"*.

| off | size | field               | type                | role |
| --- | ---- | ------------------- | ------------------- | ---- |
| 0   | 4    | `header`            | `HEADER`            | `opcode = 0xba` |
| 4   | 4    | `wait_sema_value`   | `uint32_t`          | wait threshold |
| 8   | 1    | `wait_sema_idx`     | `uint8_t`           | wait semaphore index |
| 9   | 1    | `wait_sema_mode`    | `WAIT_MODE`         | wait condition |
| 10  | 1    | `dma_engines`       | `DMA_ENGINE_CONFIG` | `engine_count` **must be 1** (single queue) |
| 11  | 1    | `dma_flags`         | `DMA_FLAGS`         | queue id; `tdg = 0`, `wr_done_sync = 0` |
| 12  | 1    | `descriptor_flags`  | `DESC_FLAGS`        | `num_descriptors:2` + `desc0/1/2_src:1` |
| 13  | 3    | `descriptor_reg[3]` | `uint8_t[3]`        | per-desc source register (REGISTER mode) |
| 16  | 48   | `descriptor_imm[3]` | `DESCRIPTOR_RAW[3]` | **3 × 16 B raw SDMA descriptors** |

The **escape hatch**: the compiler/runtime pre-builds up to three raw 16-byte SDMA descriptors and
injects them **straight into one DGE queue**, bypassing the GENERATE/DIMPUSH descriptor synthesis
(the bytes live in the instruction word at off 16-63). `DESCRIPTOR_RAW` is `uint8_t bytes[16]`;
`DESC_FLAGS.num_descriptors` selects 1/2/3 and each `descN_src` bit selects per-descriptor source
via `DMA_DESC_MODE { IMMEDIATE = 0 (16 B in-instruction), REGISTER = 1 (assembled from 4×32-bit
registers, `descN_src..descN_src+3`) }`. This is the low-level / hand-tuned DMA path. `[HIGH/OBSERVED]`

> **NOTE — the `0xba` constraints are prose, not predicates.** `engine_count == 1`,
> `num_descriptors ∈ {1,2,3}`, `tdg == 0`, `wr_done_sync == 0` are stated only in the header
> docstring — the in-header `is_valid_dma_immediate` checks just header + opcode. A decoder must
> enforce them itself. `[HIGH/OBSERVED]`

### 3d. `0xbd DMA_DIRECT2D_XPOSE_STRUCT` — non-indexed DMA-xbar transpose (v3+) `[HIGH/OBSERVED]`

Header docstring (`dma_direct2d_xpose.h`): *"This instruction will generate DMA descriptors to
initiate a xbar transpose in Neuron+. The descriptors will be supplied by the DGE block …"*

| off | size | field               | type              | role |
| --- | ---- | ------------------- | ----------------- | ---- |
| 0   | 4    | `header`            | `HEADER`          | `opcode = 0xbd` |
| 4   | 8    | `events`            | `EVENTS`          | sync |
| 12  | 1    | `src_bound_reg`     | `BOUND_CHECK_REG` | src bounds |
| 13  | 1    | `dst_bound_reg`     | `BOUND_CHECK_REG` | dst bounds |
| 14  | 1    | `in_dtype`          | `DTYPE`           | input dtype |
| 15  | 1    | `out_dtype`         | `DTYPE`           | output dtype |
| 16  | 8    | `src_start_addr`    | `ADDR8`           | src base |
| 24  | 8    | `src_step_elem[2]`  | `int32_t[2]`      | strides |
| 32  | 4    | `src_num_elem[2]`   | `uint16_t[2]`     | counts |
| 36  | 4    | `tile_src_row_step` | `int32_t`         | per-tile row stride |
| 40  | 8    | `dst_start_addr`    | `ADDR8`           | dst base |
| 48  | 8    | `dst_step_elem[2]`  | `int32_t[2]`      | strides |
| 56  | 4    | `dst_num_elem[2]`   | `uint16_t[2]`     | counts |
| 60  | 1    | `tile_src_rows`     | `uint8_t`         | **must == 16** |
| 61  | 1    | `tile_src_cols`     | `uint8_t`         | 1..128 (2 B dtype) / 1..64 (4 B dtype) |
| 62  | 1    | `semaphore`         | `uint8_t`         | completion sem |
| 63  | 1    | `dma_configs`       | `DMA_CONFIGS`     | priority |

> **GOTCHA — the `out_dtype` size comment in-header reads `// 5`.** That is a typo in the shipped
> header; `out_dtype` is a **1-byte** `DTYPE` at offset 15. The struct still
> `ISA_STATIC_ASSERT(…==64)`, and `offsetof(src_start_addr)==16` proves the field is one byte.
> Do not read it as a 5-byte field. `[HIGH/OBSERVED]`

Validity predicates (in-header `is_valid_dma_transpose`, OBSERVED): `nc >= V3`
(`has_valid_dma_transpose_nc`); `tile_src_rows == 16` (`has_valid_xpose_tile_rows_temporary`);
`tile_src_cols ∈ [1,128]` for 2-byte out / `[1,64]` for 4-byte (`has_valid_xpose_tile_cols_temporary`);
`in_dtype == out_dtype` (no cast); `dst_start_addr` **32 B-aligned** and a **SBUF** address;
`dst_step_elem` 32 B-aligned unless `dst_num_elem == 1` (xbar HW). A **non-indexed** bulk transpose:
the DMA crossbar transposes a 2-D strided src in 16×N tiles while it streams to the dst; src/dst are
full Neuron addresses (HBM↔SBUF or SB↔SB). `[HIGH/OBSERVED]`

### 3e. `0xf1 DMA_GATHER_XPOSE_STRUCT` — gather-by-index + transpose (v3+) `[HIGH/OBSERVED]`

Header docstring (`dma_gather_xpose.h`): *"DmaGatherTranspose performs a gather operation from HBM
or SBUF using dynamic indices, followed by xbar transpose, and writes the result to SBUF. This
instruction uses the SW-DGE backend with Q7 processors in the Gpsimd engine. Key use cases: MOE MLP
and chunked prefill attention."* The header also pins the dim convention: *minor2major*, `X`
(fastest) = `dim[0]` → `Y` → `Z` → `W` (slowest) = `dim[3]`.

| off | size | field                     | type                          | role |
| --- | ---- | ------------------------- | ----------------------------- | ---- |
| 0   | 4    | `header`                  | `HEADER`                      | `opcode = 0xf1` |
| 4   | 8    | `events`                  | `EVENTS`                      | sync |
| 12  | 1    | `semaphore`               | `uint8_t`                     | completion sem |
| 13  | 1    | `dma_configs`             | `DMA_CONFIGS`                 | priority |
| 14  | 1    | `idx_num_active_channels` | `uint8_t`                     | active gather channels |
| 15  | 1    | `src_idx_bound_reg`       | `BOUND_CHECK_REG`             | index bounds register |
| 16  | 8    | `src_start_addr`          | `ADDR8`                       | gather-source base (HBM / SBUF) |
| 24  | 8    | `src_step_elem[2]`        | `int32_t[2]`                  | source strides |
| 32  | 4    | `src_num_elem[2]`         | `uint16_t[2]`                 | source counts |
| 36  | 4    | `src_idx_start_addr`      | `ADDR4`                       | **UINT32 index tensor base** (4 B-aligned) |
| 40  | 8    | `dst_start_addr`          | `ADDR8`                       | dst base (SBUF) |
| 48  | 3    | `reserved[3]`             | `uint8_t[3]`                  | — |
| 51  | 1    | `dst_bound_reg`           | `BOUND_CHECK_REG`             | dst bounds |
| 52  | 4    | `dst_step_elem_1`         | `int32_t`                     | dst dim-1 stride (`dim-0 = sizeof(dtype_hi)`) |
| 56  | 4    | `dst_num_elem[2]`         | `uint16_t[2]`                 | dst counts |
| 60  | 2    | `elem_size`               | `uint16_t`                    | 2..256, even |
| 62  | 1    | `dtype`                   | `DTYPE_PAIR`                  | `dtype_lo:4` = src, `dtype_hi:4` = dst |
| 63  | 1    | `flags`                   | `DMA_GATHER_TRANSPOSE_FLAGS`  | `gather_dim:2 (INDIRECT_DIM)` + 6 reserved |

Validity predicates (in-header `is_valid_dma_gather_transpose`, OBSERVED): `nc >= V3`; **dtype 2 B
only** (`type_size_check(…,2)` on both legs) and `dtype_lo == dtype_hi` (*no cast in ucode yet*);
`elem_size ∈ [2,256]` and even; index tensor **UINT32, count a multiple of 16**
(`src_num_elem[1] % 16 == 0`, the Y/gather dim); `src_idx_start_addr` 4 B-aligned, `src` 2 B-aligned,
`dst_start_addr` 32 B-aligned (xbar); `dst_step_elem_1` 32 B-aligned unless `dst_num_elem[1]==1`;
cross-field `src_num_elem[1] == dst_num_elem[0]` (both = #indices). The gather dim **must == Y**
(`gather_dim == INDIRECT_DIM_Y`, the slowest dim, *"initially"* to simplify ucode) —
`INDIRECT_DIM { X=0, Y=1, Z=2, W=3 }`. **Gather (dynamic UINT32 indices, bounds-predicated) + xbar
transpose** in one instruction, src in HBM or SBUF, dst in SBUF, 16×128 tile. `[HIGH/OBSERVED]`

### 3f. `0xb4 CTRL_TEST_ES_STRUCT` — TestEventSem (control, *not* a DMA) `[HIGH/OBSERVED]`

Header docstring (`ctrl_test_es.h`): *"TestEventSem — Read (read each semaphore into corresponding
register) / Update (update each semaphore from corresponding register) / ConditionalUpdate (if
value ≥ threshold, read-modify-read + increment register; repeat_count basic-loop). One to sixteen
events or semaphores. … The evt/sem ids must be unique."*

| off | size | field               | type                  | role |
| --- | ---- | ------------------- | --------------------- | ---- |
| 0   | 4    | `header`            | `HEADER`              | `opcode = 0xb4` |
| 4   | 8    | `events`            | `EVENTS`              | sync |
| 12  | 1    | `mode`              | `TEST_EVT_SEM_MODE`   | `READ_SEM=0 / READ_EVT=1 / UPDATE_SEM=2 / UPDATE_EVT=3 / COND_UPDATE_SEM=4` |
| 13  | 1    | `update_type`       | `UPDATE_MODE`         | sem inc/dec/add/sub/write (read vs complete variants) |
| 14  | 1    | `repeat_count`      | `uint8_t`             | conditional-update basic-loop count |
| 15  | 1    | `num_evt_sem`       | `uint8_t`             | how many of the 16 slots are active (1..16) |
| 16  | 16   | `evt_sem_ids[16]`   | `uint8_t[16]`         | up to 16 event/semaphore ids (unique; unused = 0) |
| 32  | 16   | `value_registers[16]`| `REG_NUM[16]`        | the 16 paired registers |
| 48  | 4    | `threshold`         | `uint32_t`            | conditional-update threshold |
| 52  | 1    | `setter_signature`  | `uint8_t`             | setter id |
| 53  | 11   | `reserved0[11]`     | `uint8_t[11]`         | must be zero |

A semaphore/event **batch test-read-update** control op — up to 16 sem/evt ↔ 16 registers in one
instruction. The validity contract bars immediate-value update modes (`SEM_ADD_IMM_*` /
`SEM_SUB_IMM_*` / `SEM_WR_IMM_*`) — only register-sourced updates are legal here — and enforces
`num_evt_sem ∈ [1,16]`, slot uniqueness, and zeroed unused slots. NX SEQUENCER control-spine; no
POOL kernel, no DGE descriptor. See [TPB Event/Semaphore Regions (EVT_SEM)](../../control/address/evt-sem-regions.md)
for the addressed HW block (planned, Part 12). `[HIGH/OBSERVED]`

### 3g. `0xb6 CTRL_CCI_STRUCT` — CompactControlInst (control, *not* a DMA, MAVERICK) `[HIGH/OBSERVED header / interior INFERRED]`

`CTRL_CCI_STRUCT` is `header (4) + insts[15]` (`NEURON_ISA_TPB_CCINST`, **4 B each = 60 B**). The
"compact control instruction" carries its own `CCIHEADER { opcode; reserved[1]; debug_cmd;
debug_hint }`. Each `CCINST` is a 9-way union (`alu / mov / movimm / br / bl / mem / semr / semu /
nop`), discriminated by `CCINST_TYPE { ALU=0, MOV=1, MOVIMM=2, BR=3, BL=4, MEM=5, SEMR=6, SEMU=7,
NOP=8 }`, every member a 4-byte struct (e.g. `CCALU = {op, src0, src1, dst}`; `CCMOV_IMM = {op, dst,
uint16_t imm}`; `CCMEM = {op, srcdst, addr_reg, addr_imm}`; `CCBR = {op, cc_src, CCBR_TARGET target}`).
This is a **packed micro-bytecode bundle**: fifteen compact control instructions executed in
sequence by the NX sequencer — a code-density / inner-loop optimization, MAVERICK-new.

> **CORRECTION — `0xb6` is a full scalar bytecode, not just NOP/MOV/ALU.** The `CCOP` opcode space
> (`ctrl_cci.h`, 1-byte `op` field of every `CCINST`) is a small RISC-style ISA. It includes:
> `NOP=0x0`; `MOV=0x1`, `MOV_IMM_LO/HI/LO_S=0x2..0x4`; **32-bit ALU** `ADD/SUB/MUL/DIV/MOD/MAX/MIN
> =0x10..0x16` (saturating `S` `0x18..0x1e`); **64-bit ALU** `ADD64..MIN64=0x20..0x26` (`64S`
> `0x28..0x2e`); **float** `ADDF/SUBF/MULF/DIVF/TRUNCF/MAXF/MINF=0x30..0x36` and
> `ABS_VALUEF/ABS_MAXF/ABS_MINF=0x37..0x39` (integer abs `ABS_VALUE..ABS_MIN64=0x3a..0x3f`);
> **shift/rotate** `ASL/ASR/LSL/LSR/ROL/ROR=0x40..0x45` (64-bit `0x46..0x49`), `CLZ/CLZ64=0x4a/0x4b`;
> **logical/bitwise** `LOGICAL_AND..BITWISE_XOR=0x50..0x57`; **compares** `CMP*_EQ..GE=0x60..0x85`
> (signed `CMPS`, 64-bit `CMP64`/`CMP64S`, float `CMPF` variants); **branches** `BR_REL_IMM/_T/_F`,
> `BL_REL_IMM`, `BR_REL_REG…`, `BR_REG64…`, `BL_…=0x90..0x9b`; **load/store**
> `LDR/LDRH/LDRHS/LDRB/LDRBS/STR/STRH/STRB/LDRD/LDRQ/STRD/STRQ=0xa0..0xab`; and **semaphore**
> `SEM_READ=0xb0 / SEM_WRITE=0xb1 / SEM_ADD=0xb2`. So `0xb6` packs a tiny program — branches, memory
> access, and semaphore ops included — into one 64-byte instruction word. `[HIGH/OBSERVED — CCOP enum]`

Validity (in-header `is_valid_ctrl_cci`): `has_valid_neuron_header && has_tile_idx_zero &&
has_cci_opcode` (`opcode == CompactControlInst`). The structural contract is byte-grounded; the
MAVERICK device-side *interpreter* that walks `insts[15]` is **not** disassembled this pass, so the
execution interior is flagged **INFERRED**. `[HIGH/OBSERVED header → INFERRED interior]`

---

## 4. Dispatch surface per opcode `[HIGH where carved / MED where FLIX-desynced]`

The dispatch discriminator (carried from the sequencer-load/store decodes): an opcode is **(i)** a
**POOL software kernel** iff it appears in the carved Q7-POOL `kernel_info_table` (opcode at
entry+3, funcVA at entry+4); else **(ii)** SEQ/engine-**native** (decoded by the NX sequencer /
per-engine decode arm, no funcVA hop). The five DMA opcodes additionally **(iii) emit DGE
descriptors**.

| op     | POOL kernel?                          | native surface | DGE descriptor kind | dispatch chain |
| ------ | ------------------------------------- | -------------- | ------------------- | -------------- |
| `0xb8` | **SUNDA only** → `dma_memcopy @0x01002f80` (SUNDA EXTISA_0 table, 18 entries) | SEQ + DGE on CAYMAN+ | `DMA_DIRECT2D (0x0)` | SUNDA: table-scan → `callx8 dma_memcopy` → build DIRECT2D → DGE GENERATE/DIMPUSH → SDMA. CAYMAN+: SEQ/DGE decode → DGE backend → GENERATE/DIMPUSH → SDMA |
| `0xb9` | no                                    | NX seq (MAVERICK) | `DMA_DIRECT2D (0x0)` | NX decode → wait-sema → DGE backend → GENERATE/DIMPUSH → SDMA queue(`start_id … +count`) |
| `0xba` | no                                    | NX seq (MAVERICK) | `DMA_DIRECT2D (0x0)` | NX decode → inject `descriptor_imm[1..3]` straight to DGE queue(`start_id`), `engine_count = 1` |
| `0xbd` | no                                    | SEQ FLIX-inline `0x2faf` | `DMA_TRANSPOSE (0x2)` | SEQ/DGE decode → DGE reshape resolves xbar tile permute (16×N, step swap) → GENERATE/DIMPUSH (transposed strides) → SDMA |
| `0xf1` | no                                    | SEQ FLIX-inline `0x3020` | `DMA_GATHER_TRANSPOSE (0x3)` | SEQ/DGE decode → read UINT32 index tensor + bounds mask → per-row xbar-transpose descriptor → GENERATE/DIMPUSH → SDMA → dst SBUF |
| `0xb4` | no                                    | NX control front-end | none | sem/event control op (no DGE) |
| `0xb6` | no                                    | NX scalar/control unit (MAVERICK) | none | sequencer walks `insts[15]` (no DGE) |

The `DGE_OPCODE` column is the **internal descriptor-kind code** (`common.h:830`, cayman) — a
**separate** namespace from the 8-bit TPB instruction opcode:
`DMA_DIRECT2D=0x0 / DMA_INDIRECT1D=0x1 / DMA_TRANSPOSE=0x2 / DMA_GATHER_TRANSPOSE=0x3`. The TPB
opcode names the instruction; the `DGE_OPCODE` names the descriptor it lowers to. The
`{0xb8, 0xb9, 0xba} → DIRECT2D` fold is INFERRED-HIGH from the three sharing 2-dim-copy semantics
and the single `DIRECT2D` kind.

> **NOTE — `0xb8` is the generational pivot of the whole indirect/DMA family.** On SUNDA it is a
> POOL software kernel (`dma_memcopy @0x01002f80`, independently confirmed in the SUNDA EXTISA_0
> `kernel_info_table @0x02000760`, 18 entries). On CAYMAN+ it drops out of the 17-entry POOL table
> (`@0x02000380`) and is served by the SEQ handler + the DGE descriptor-gen layer instead — the
> same SUNDA-POOL→SEQ/DGE shift the indirection family follows. `[HIGH/OBSERVED for the SUNDA
> funcVA; MED for the CAYMAN+ exact decode-arm VA — FLIX-desynced, carried]`

> **GOTCHA — the FLIX-inline VAs `0x2faf`/`0x3020` are CARRIED, not re-byte-traced here.** No Q7
> Vision FLIX code-stream was disassembled this pass; the struct/enum facts above never cross the
> ncore2gp FLIX bundle decoder, so the byte-grounded facts are unaffected, but the per-gen handler
> VAs are MED. The MAVERICK NX decode arms for `0xb9`/`0xba`/`0xb6` are not carved — their chains
> are INFERRED from the struct fields. `[honesty ledger]`

---

## 5. Semantics + the transpose mechanism

| op     | class                          | src → dst spaces                        | descriptor lowering |
| ------ | ------------------------------ | --------------------------------------- | ------------------- |
| `0xb8` | bulk 2-D copy (+ optional reduce) | any Neuron addr → any (HBM/SBUF/PSUM both ways) | DGE `DIRECT2D` (Pool 2-dim) GENERATE/DIMPUSH |
| `0xb9` | bulk copy, next-gen (sema/engine/addr-rich) | any → any (incl. **remote core**)   | DGE `DIRECT2D`, built-in sema + engine-select |
| `0xba` | raw-descriptor injection (escape hatch) | (whatever the raw desc encodes) → DGE queue | 1-3 immediate 16 B descriptors, no GENERATE |
| `0xbd` | transpose, descriptor/xbar, non-indexed | any → SBUF (HBM/SB → SBUF); 16×N tiles | DGE `DMA_TRANSPOSE` (xbar stride permute) |
| `0xf1` | indirect gather + transpose, UINT32 idx | HBM or SBUF → SBUF; gather-by-index, 16×128 | DGE `GATHER_TRANSPOSE` (gather + xbar) |
| `0xb4` | control: sem/event batch test-update | n/a (sem ↔ reg)                       | none (NX control) |
| `0xb6` | control: packed scalar bytecode bundle | n/a (reg/imm/mem scalar)             | none (NX control) |

**The descriptor lowering** (bottom layer; documented in full on [DGE Descriptor-Emit Path](../dge/dge-emit.md)):
a 64 B high-level descriptor is expanded by the DGE into **16 B SDMA BD ring entries** via
`GENERATE` (one transfer BD: `{buf_ptr, elem_size, completion sem}`) + `DIMPUSH` (one loop level per
tensor dim, a `(num,num)[step,step]` src+dst pair-of-pairs) + `REGWRITE` (register-source field;
retired on v4+). The TPB engine bumps the DMA-queue tail pointer (the doorbell); the `al_udma`
**M2S** engine (read-from-source, AXI read + stream push) and the **S2M** engine (write-to-dest, AXI
write + stream accept) execute the BDs — one M2S+S2M pair = one full-duplex SDMA channel. The
`RD`/`WR` direction of each emitted BD is the M2S vs S2M leg. The HW engine and its CSRs are on
[CSR — UDMA M2S](../../control/csr/udma-m2s.md) / [CSR — UDMA S2M](../../control/csr/udma-s2m.md)
(planned, Part 12). `[HIGH/OBSERVED for the GENERATE/DIMPUSH model + M2S/S2M roles; the 64 B→16 B
fan is INFERRED-HIGH, CARRIED]`

**The transpose mechanism** (`0xbd`/`0xf1`): a **stride permutation realized by the DMA crossbar**
during the transfer. The DGE reshape swaps the src/dst `step_elem[]` axes (signed strides allow the
reverse-axis walk) so bytes land transposed in the dst. Tile geometry: 16 rows × (≤128 cols for 2 B
/ ≤64 for 4 B) for `0xbd`; 16×128 (2 B) for `0xf1`. The dst must be a 32 B-aligned **SBUF** address.
This is **distinct** from the DVE `0x6b STREAM_TRANSPOSE` (32×32 lane-permute in the vector
datapath, in-SBUF/PSUM, all ≤32-bit) — see [StreamTranspose](./stream-transpose.md). The transpose
**family** is therefore three mechanisms: `(a) 0xbd` DMA-xbar non-indexed, `(b) 0xf1` DMA-xbar
gather-by-index, `(c) 0x6b` DVE lane-permute. `(a)`/`(b)` live on the DGE/POOL descriptor path;
`(c)` on the DVE compute engine. `[HIGH/OBSERVED]`

---

## 6. Per-generation presence — consolidated `[HIGH/OBSERVED]`

| op     | SUNDA v2 | CAYMAN v3 | MARIANA v4 | MAVERICK v5 | notes |
| ------ | :------: | :-------: | :--------: | :---------: | ----- |
| `0xb8` | Y        | Y         | Y          | Y           | POOL-kernel on SUNDA; SEQ+DGE on CAYMAN+ |
| `0xb9` | —        | —         | —          | Y           | next-gen mover, v5-new |
| `0xba` | —        | —         | —          | Y           | raw-desc inject, v5-new |
| `0xbd` | —        | Y         | Y          | Y           | v3+ "Neuron+", xbar transpose |
| `0xf1` | —        | Y         | Y          | Y           | v3+ "Neuron+", gather + xbar |
| `0xb4` | —        | —         | Y          | Y           | v4+ sem/event control |
| `0xb6` | —        | —         | —          | Y           | v5-new packed control bundle |

The **opcode-present set == the struct-defining set == the `// Y` flag set**, per gen
(triple-confirmed this pass). The transposes add at CAYMAN (v3 = the header's `nc >= V3` "Neuron+"
predicate). Of MAVERICK's six new opcodes, **three** are in this cluster (`0xb6`/`0xb9`/`0xba`); the
other three (`0x26 ACTIVATE_MULTIPASS`, `0xf3 TENSOR_TENSOR_INT_WIDE`, `0xf4 TENSOR_SCALAR_INT_WIDE`)
are not. `[HIGH/OBSERVED]`

---

## 7. The complete GPSIMD data-movement opcode map

Folding the seven with the already-decoded movers gives the full picture. GPSIMD data movement is
**four layers** plus the orthogonal DVE datapath transpose and the NX control-spine.

| layer | opcode | mnemonic | struct / kind | role | gens | confidence |
| ----- | ------ | -------- | ------------- | ---- | ---- | ---------- |
| **L1 — sequencer scalar mem↔reg** (not bulk DMA) | `0xaa` | `TENSOR_LOAD` | `MEM_2D` | memory → ≤32 sequencer GPRs, 1 window ([TensorLoad](./tensorload.md)) | su ca ma mv | `[HIGH/OBSERVED]` |
| | `0xab` | `TENSOR_STORE` | `MEM_2D` | GPR/immediate → memory, mirror of load ([TensorStore](./tensorstore.md)) | su ca ma mv | `[HIGH/OBSERVED]` |
| **L2 — bulk tensor DMA** (DGE-emitter band) | `0xb8` | `DMAMEMCPY` | `DMA_DIRECT2D` / kind `0x0` | 2-D copy (+ reduce); canonical mover | su ca ma mv | `[HIGH/OBSERVED]` |
| | `0xb9` | `DMA_MEMCPY2` | `DMA_COPY2D` / kind `0x0` | next-gen copy: sema/engine/addr-mode + remote-core | mv | `[HIGH/OBSERVED]` |
| | `0xba` | `DMA_IMMEDIATE` | `DMA_IMMEDIATE` / kind `0x0` | inject 1-3 raw 16 B descriptors | mv | `[HIGH/OBSERVED]` |
| | `0xbb` | `DMA_INDIRECT` | `DMA_INDIRECT1D` / kind `0x1` | gather/scatter-by-index DMA (1-D + index tensors; scatter-add via `DGE_COMPUTE_OP`) — [Indirection Engine](./indirection-gather.md) | su ca ma mv | `[HIGH/CARRIED]` |
| | `0xbd` | `DMA_TRANSPOSE` | `DMA_DIRECT2D_XPOSE` / kind `0x2` | 2-D copy + xbar transpose, non-indexed, 16×N | ca ma mv | `[HIGH/OBSERVED]` |
| | `0xf1` | `DMA_GATHER_TRANSPOSE` | `DMA_GATHER_XPOSE` / kind `0x3` | gather(UINT32 idx) + xbar transpose → SBUF, 16×128, 2 B | ca ma mv | `[HIGH/OBSERVED]` |
| **L3 — the DGE** | — | (Descriptor-Generation Engine) | GENERATE / DIMPUSH / REGWRITE | expands each 64 B descriptor → 16 B SDMA BDs; 3 backends (Pool 2-dim / RTL 5+2-dim / software 4-dim) — [DGE Emit](../dge/dge-emit.md), [Selector](../dge/dge-backend-selector.md), [Reshape](../dge/dge-reshape.md) | all | `[HIGH/CARRIED]` |
| **L4 — the SDMA HW** | — | `al_udma` M2S / S2M | SDMA BD rings | M2S reads source (AXI read + stream push); S2M writes dest (AXI write + stream accept); one pair = one channel | all | `[HIGH/CARRIED]` |
| **orthogonal — DVE datapath transpose** | `0x6b` | `STREAM_TRANSPOSE` | `S4D4_TR` | 32×32 lane-permute in the DVE vector pipe, in-SBUF/PSUM, ≤32-bit — [StreamTranspose](./stream-transpose.md) | su ca ma mv | `[HIGH/CARRIED]` |
| **control-spine** (not data movement) | `0xb4` | `TEST_EVENT_SEM` | `CTRL_TEST_ES` | sem/event batch test-read-update (≤16) | ma mv | `[HIGH/OBSERVED]` |
| | `0xb6` | `COMPACT_CONTROL_INST` | `CTRL_CCI` | 15 packed scalar control/ALU/branch/mem/sem micro-ops | mv | `[HIGH/OBSERVED]` |

> **NOTE — SUNDA also has POOL software gather kernels.** On SUNDA the index-tensor movers
> `0x68 GATHER`, `0xe7 INDIRECT_COPY`, `0x79 EMBEDDING_UPDATE`, `0x74 TENSOR_SCALAR_ADDR` are POOL
> `kernel_info_table` kernels that *also* move data by index (via the IVP HW vector-gather or the
> DGE). They are the SUNDA-era software counterparts of the CAYMAN+ `0xbb`/`0xf1` DGE descriptors —
> covered on the [Indirection Engine](./indirection-gather.md) page. `[HIGH/CARRIED]`

---

## 8. Algorithms — annotated pseudocode (real symbols)

The four movers and the two control ops, as the decode/build that the struct fields imply. Names in
`code` are real header symbols; the device-side bodies are flagged where INFERRED.

### 8a. `0xb8` — the DIRECT2D memcpy descriptor build `[HIGH for the struct math; SUNDA body CARRIED]`

```c
// DMAMEMCPY (0xb8) -> NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT i; emits DGE_OPCODE_DMA_DIRECT2D (0x0).
// SUNDA: POOL kernel dma_memcopy@0x01002f80. CAYMAN+: SEQ + DGE descriptor-gen layer.
void dma_direct2d_emit(const NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT *i) {
    await_events(&i->events);                       // standard wait-on-semaphore prologue
    bounds_check(i->src_start_addr, i->src_bound_reg);
    bounds_check(i->dst_start_addr, i->dst_bound_reg);

    // One transfer BD per element-block; the DGE GENERATE micro-op carries the SoC address,
    // the element byte-count, and the completion semaphore.
    dge_generate(/*buf=*/resolve(i->src_start_addr), /*elem=*/i->src_elem_size,
                 /*sem=*/i->semaphore, /*compute=*/i->compute_op /* NONE|ADD|MUL|MAX|MIN */);

    // One DIMPUSH per tensor dimension: a (num,step) pair on each of the src and dst legs.
    for (int d = 0; d < 2; ++d)                      // 2-D: Pool backend = 2 DIMPUSH levels
        dge_dimpush(/*src=*/(i->src_num_elem[d], i->src_step_elem[d]),
                    /*dst=*/(i->dst_num_elem[d], i->dst_step_elem[d]));

    if (i->sem_increment) arm_completion(i->semaphore, i->sem_increment);
    ring_doorbell(/*queue=*/default_dma_queue);     // bump M2S/S2M tail pointer
}
```

### 8b. `0xb9` — the next-gen COPY2D, with explicit sema + engine + addr-mode `[HIGH struct / MED chain]`

```c
// DMA_MEMCPY2 (0xb9) -> DMA_COPY2D_STRUCT. MAVERICK-only; emits DGE_OPCODE_DMA_DIRECT2D (0x0).
void dma_copy2d_emit(const NEURON_ISA_TPB_DMA_COPY2D_STRUCT *i) {
    wait_on_sema(i->wait_sema_idx, i->wait_sema_value, i->wait_sema_mode);   // built-in prologue

    uint64_t src = resolve_addr(i->src_start_addr, lo_nibble(i->addr_mode)); // 12 DMA_ADDR_MODEs:
    uint64_t dst = resolve_addr(i->dst_start_addr, hi_nibble(i->addr_mode)); //  IMM64/REG_PAIR/REG+off/scale

    int n = 1 << i->dma_engines.engine_count;        // ONE/TWO/FOUR/EIGHT -> 1/2/4/8 engines
    for (int e = 0; e < n; ++e)
        dge_emit_direct2d(i->dma_engines.start_id + e, src, dst,
                          i->src_num_elem, i->src_step_elem, i->src_elem_size,
                          i->dst_num_elem, i->dst_step_elem, i->dst_elem_size,
                          /*queue=*/i->dma_flags.queue_id,         // 3-bit field: 8 queues
                          /*remote=*/i->remote_core_id);           // cross-core DMA target (REG_NUM)

    arm_sema(i->rd_done_sema_update_idx, i->wr_done_sema_update_idx, i->sema_update_mode);
}
```

### 8c. `0xba` — raw descriptor injection (the escape hatch) `[HIGH struct / MED chain]`

```c
// DMA_IMMEDIATE (0xba) -> DMA_IMMEDIATE_STRUCT. engine_count MUST be 1; bypasses GENERATE/DIMPUSH.
void dma_immediate_emit(const NEURON_ISA_TPB_DMA_IMMEDIATE_STRUCT *i) {
    wait_on_sema(i->wait_sema_idx, i->wait_sema_value, i->wait_sema_mode);
    int n = i->descriptor_flags.num_descriptors;     // 1..3
    for (int d = 0; d < n; ++d) {
        // DMA_DESC_MODE: IMMEDIATE(0) = the 16B in descriptor_imm[d]; REGISTER(1) = assembled from
        // four 32-bit registers named by descriptor_reg[d] (descN_src .. descN_src+3).
        NEURON_ISA_TPB_DESCRIPTOR_RAW bd =
            (descN_src(i, d) == DMA_DESC_MODE_IMMEDIATE) ? i->descriptor_imm[d]
                                                         : assemble_from_regs(i->descriptor_reg[d]);
        dge_push_raw_bd(i->dma_engines.start_id, bd); // straight into the one DGE queue
    }
}
```

### 8d. `0xbd` — the non-indexed DMA-xbar transpose `[HIGH struct / MED device schedule]`

```c
// DMA_TRANSPOSE (0xbd) -> DMA_DIRECT2D_XPOSE_STRUCT. nc>=V3; emits DGE_OPCODE_DMA_TRANSPOSE (0x2).
void dma_transpose_emit(const NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT *i) {
    assert(i->tile_src_rows == 16);                  // has_valid_xpose_tile_rows_temporary
    assert(i->tile_src_cols >= 1 &&                  // has_valid_xpose_tile_cols_temporary:
           i->tile_src_cols <= (dtype_bytes(i->out_dtype) == 2 ? 128 : 64));
    assert(i->in_dtype == i->out_dtype);             // has_valid_xpose_dtypes_temporary (no cast)
    assert(is_sbuf(i->dst_start_addr) && aligned(resolve(i->dst_start_addr), 32)); // xbar HW

    // The DGE reshape swaps the src/dst step axes so the crossbar transposes 16xN tiles in flight.
    for (tile across (i->src_num_elem, i->tile_src_row_step)) {
        dge_generate(/*buf=*/tile_src(i, tile), /*elem=*/dtype_bytes(i->out_dtype), i->semaphore);
        dge_dimpush(/*src=*/(rows=16, i->tile_src_row_step),         // row axis
                    /*dst=*/(cols=i->tile_src_cols, i->dst_step_elem[0])); // transposed: rows<->cols
    }
    ring_doorbell(/*queue from*/ i->dma_configs.priority_class);
}
```

### 8e. `0xf1` — the gather-by-index + xbar transpose `[HIGH struct / MED device schedule]`

```c
// DMA_GATHER_TRANSPOSE (0xf1) -> DMA_GATHER_XPOSE_STRUCT. nc>=V3; 2B dtype; SW-DGE backend.
void dma_gather_transpose_emit(const NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT *i) {
    assert(dtype_bytes(i->dtype.dtype_lo) == 2 && i->dtype.dtype_lo == i->dtype.dtype_hi);
    assert(i->elem_size >= 2 && i->elem_size <= 256 && (i->elem_size % 2) == 0);
    assert(i->flags.gather_dim == INDIRECT_DIM_Y);         // gather along the slowest dim "for now"
    assert(i->src_num_elem[1] % 16 == 0);                  // index count multiple of 16
    assert(i->src_num_elem[1] == i->dst_num_elem[0]);      // cross-field consistency

    const uint32_t *idx = (const uint32_t *)resolve4(i->src_idx_start_addr); // UINT32 index tensor
    for (int c = 0; c < i->idx_num_active_channels; ++c) {
        uint32_t row = idx[c];
        bounds_check_index(row, i->src_idx_bound_reg);
        uint64_t src_row = resolve(i->src_start_addr) + (uint64_t)row * i->src_step_elem[1];
        // build one 16x128 xbar-transpose descriptor per gathered row -> dst SBUF
        dge_generate(/*buf=*/src_row, /*elem=*/i->elem_size, i->semaphore);
        dge_dimpush(/*src=*/(16, i->src_step_elem[0]),
                    /*dst=*/(128, i->dst_step_elem_1));   // dst_step_elem_0 == sizeof(dtype_hi)
    }
}
```

### 8f. `0xb4` — the event/semaphore batch test `[HIGH/OBSERVED]`

```c
// TEST_EVENT_SEM (0xb4) -> CTRL_TEST_ES_STRUCT. NX control-spine; no DGE.
void test_event_sem(const NEURON_ISA_TPB_CTRL_TEST_ES_STRUCT *i) {
    for (int rep = 0; rep <= i->repeat_count; ++rep)          // ConditionalUpdate basic-loop
      for (int k = 0; k < i->num_evt_sem; ++k) {              // up to 16 sem/evt <-> 16 regs
        uint32_t id = i->evt_sem_ids[k], reg = i->value_registers[k];
        switch (i->mode) {                                    // TEST_EVT_SEM_MODE
          case READ_SEM:  case READ_EVT:   write_reg(reg, read_es(id, i->mode)); break;
          case UPDATE_SEM: case UPDATE_EVT: update_es(id, read_reg(reg), i->update_type); break;
          case COND_UPDATE_SEM:                                // if value>=threshold: rmw + inc reg
            if (read_es(id, READ_SEM) >= i->threshold)
                { update_es(id, read_reg(reg), i->update_type); inc_reg(reg); }
            break;
        }
      }
}
```

### 8g. `0xb6` — the compact-control bytecode interpreter `[HIGH header / interior INFERRED]`

```c
// COMPACT_CONTROL_INST (0xb6) -> CTRL_CCI_STRUCT. MAVERICK-only; NX scalar/control unit.
// INTERIOR INFERRED: the MAVERICK device interpreter that walks insts[15] is not byte-traced.
void compact_control_inst(const NEURON_ISA_TPB_CTRL_CCI_STRUCT *i) {
    for (int pc = 0; pc < 15; ) {                    // 15 packed 4-byte CCINSTs
        NEURON_ISA_TPB_CCINST c = i->insts[pc];
        switch (cc_class(c.alu.op)) {                // CCOP byte -> CCINST_TYPE class
          case ALU:    reg[c.alu.dst]   = alu(c.alu.op, reg[c.alu.src0], reg[c.alu.src1]); ++pc; break;
          case MOV:    reg[c.mov.dst]   = reg[c.mov.src];                                   ++pc; break;
          case MOVIMM: mov_imm(reg, &c.movimm);                                             ++pc; break;
          case MEM:    cc_load_store(c.mem.op, &reg[c.mem.srcdst], reg[c.mem.addr_reg], c.mem.addr_imm); ++pc; break;
          case SEMR:   reg[c.semr.dst]  = sem_read(c.semr.semid);                           ++pc; break;
          case SEMU:   sem_update(c.semu.op, c.semu.semid, reg[c.semu.src0], c.semu.imm);   ++pc; break;
          case BR:     pc = cc_branch(c.br.op, reg[c.br.cc_src], &c.br.target, pc);              break;
          case BL:     reg[c.bl.dst] = pc + 1; pc = cc_target(&c.bl.target, pc);                 break;
          case NOP:    ++pc;                                                                     break;
        }
    }
}
```

---

## 9. Adversarial self-verification ledger

The five strongest claims, re-challenged against the binary this pass:

1. **All seven opcode bytes + per-gen `// Y`** — `rg -n 'OPCODE_…'` over all four `common.h`: SUNDA
   `0xb8@260` (and **zero** hits for the six others); CAYMAN `0xb8@257 / 0xbd@260 / 0xf1@302`;
   MARIANA `0xb4@261 / 0xb8@263 / 0xbd@266 / 0xf1@312`; MAVERICK `0xb4@264 / 0xb6@266 / 0xb8@267 /
   0xb9@268 / 0xba@269 / 0xbd@272 / 0xf1@318`. ✓ **CONFIRMED.**
2. **All seven structs = 64 B, key offsets exact** — `gcc` `sizeof` = 64 ×7; `offsetof` DIRECT2D
   `src@16 / dst@40 / in_dtype@62 / out_dtype@63`, GATHER_XPOSE `idx@36 / dst@40`, CCI `insts@4`,
   TEST_ES `evt_sem_ids@16 / value_registers@32 / threshold@48`. ✓ **CONFIRMED.**
3. **`0xb6` is MAVERICK-only (absent v2-v4)** — `rg -c COMPACT_CONTROL_INST` = 0 in SUNDA/CAYMAN/
   MARIANA `common.h`; present only MAVERICK `@266`. Same for `DMA_MEMCPY2`/`DMA_IMMEDIATE`. ✓
   **CONFIRMED** (header-OBSERVED; interior of the `insts[15]` interpreter flagged INFERRED).
4. **`DGE_OPCODE` kind binding `{0x0/0x1/0x2/0x3}`** — `common.h` cayman `830-833`, mariana
   `923-926`, maverick `993-996`: `DMA_DIRECT2D=0x0 / DMA_INDIRECT1D=0x1 / DMA_TRANSPOSE=0x2 /
   DMA_GATHER_TRANSPOSE=0x3`. ✓ **CONFIRMED** (the `{0xb8/0xb9/0xba}→0x0` fold INFERRED-HIGH).
5. **`0xb8` is the only POOL kernel of the seven, SUNDA-only** — SUNDA EXTISA_0
   `kernel_info_table @0x02000760` (18 entries) registers `0xb8 → dma_memcopy @0x01002f80`; CAYMAN_0
   table `@0x02000380` (17 entries) omits `0xb8`; none of the other six appear in any carved table.
   ✓ **CONFIRMED** (SUNDA funcVA HIGH; CAYMAN+ decode-arm VA MED/CARRIED — FLIX-desynced).

**Corrections recorded this pass:** (a) `0xb4`/`0xb6` are NX control-spine, not DMA; (b) `0x6b`
StreamTranspose is the disjoint DVE op, not one of these bytes; (c) `dma_flags.queue_id` is **3
bits** (8 queues), not 4 — the struct's inline "4b queue ID" comment is stale; (d) the `0xb6` CCOP
space is a full scalar bytecode (branches, load/store, semaphore ops), not a bare ALU; (e) the
`out_dtype` `// 5` size comment in `dma_direct2d_xpose.h` is a header typo — the field is 1 byte.
**Honesty:** no Q7 FLIX code-stream was disassembled here, so the per-gen handler VAs (`0x2faf`,
`0x3020`, the CAYMAN+ DMA decode arm) are MED/CARRIED; the MAVERICK NX decode arms for
`0xb9`/`0xba`/`0xb6` are INFERRED from the struct fields.
