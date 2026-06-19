# Descriptor + Ring Field-Table Reference

This is the per-field **lookup appendix** for laying out a GPSIMD DMA descriptor or
al_udma ring register byte-by-byte. Every field is one row: `(offset, width, name,
semantics, valid values)`. The narrative — descriptor taxonomy, address-generation
math, the DGE builder pipeline, the CCE op model, QoS, cross-die RDMA, completion —
lives in the [Consolidated Data-Movement + Collectives Reference](data-movement-reference.md);
the DGE pseudo→real lowering and `$S[]` micro-op stream live in the
[DGE Micro-Op Encoding](dge-microop-encoding.md). This page is field rows only.

The al_udma per-engine CSR semantics (engine-global banks, arbiters, rate limiters)
are catalogued in the CSR pages `control/csr/udma-m2s.md` and `control/csr/udma-s2m.md`
(stubs at time of writing); the **per-queue ring bank** they reference is reproduced in
full in [section 6](#6-the-al_udma-v4-ring-register-bank) below.

---

## Provenance and confidence

Every row is grounded in a shipped, redistributable artifact read with stock tools, or
carried byte-exact from a cited sibling page. Tags per row/table:

- **HIGH/OBSERVED** — byte/field-exact in a shipped artifact read this pass (a
  compile-verified ISA header, the al_udma v4 RTL register-definition JSON, the
  `instruction_mapping.json` struct→opcode table, or a recovered firmware string).
- **HIGH/CARRIED** — byte-exact in a cited sibling page; the source artifact (host
  `kbin_dma_desc`, the firmware RDMA disasm, the 16-B SDMA BD DWARF) is **not** in this
  extraction, so it is reproduced, not re-derived, here.
- **MED** — strong inference across a cross-gen or cross-layer reconciliation.

Primary artifacts (all under
`extracted/.../custom_op/c10/include/`, absolute paths in the verification log):

| ID | Artifact | What it grounds |
|----|----------|-----------------|
| H1 | `neuron_{cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_dma_*.h` + `..._common.h` + `..._extended.h`, all `ISA_STATIC_ASSERT(...==64)` | the six 64-B words, the v5 inline forms, every shared sub-struct, the OPCODE / DGE / INDIRECT enums, the ADDR8/4 markers |
| H2 | `arch-headers/maverick/.../udma_v4/Registers/al_udma_{m2s,s2m}_regs.json` (UnitName `UDMA_M2S` / `UDMA_S2M`) | the M2S_Q (39) / S2M_Q (36) per-queue register offsets + bitfields |
| H3 | `neuron_{mariana,maverick}_arch_isa/tpb/instruction_mapping.json` (`struct2opcode`) | confirms each struct maps to a **distinct** opcode |
| H4 | `lib/libnrtucode_internal.so` (Cairo Vision-Q7 image; IDA v3 sidecars) | the `priority_class` 5-value map, the GENERATE/DIMPUSH/REGWRITE + DGE `$S[]` emit strings |

Carried byte-exact (DWARF / native-disasm in a cited sibling — **not** in this extraction):
the 16-B `SDMA_CME_BD_DESC` (C1, §1); the RDMA `op8`/`op9` `inst_specific` operand
layout (C2, §2.4/2.5); the CCE 16-B compute descriptor (C3, §7) including the host
`kbin_dma_desc_cce_info_t` (140 B) and `sdma_data_type_size[]` table.

**Conventions** (apply to every table): `off` = byte offset from the struct/bank start
(decimal for structs, hex for register banks). `w` = width in bytes, or `bN` for an
`N`-bit packed bitfield, or `[hi:lo]` for an RTL bit range. Multi-byte scalars are
little-endian. Packed C bitfields list LSB-first within the byte. All 64-B words:
`byte0 = opcode`, `byte1 = inst_word_len = 0x10` (16 dwords = 64 B). The struct layout
is the **mariana (NC-v4)** reference; `ISA_STATIC_ASSERT==64` is identical
cayman..maverick for the shared forms (gen map: SUNDA = NC-v2, **CAYMAN = NC-v3**,
MARIANA = NC-v4, MAVERICK = NC-v5).

> **CORRECTION (catalog size).** This catalog covers **SIX** 64-B descriptor words, not
> five. `DMA_DIRECT2D_XPOSE` (opcode `0xbd`, tile-geometry-driven) and
> `DMA_GATHER_XPOSE` (opcode `0xf1`, index-array-driven) are **distinct structs** with
> **distinct field layouts** — confirmed by `struct2opcode` (H3) mapping
> `..._DIRECT2D_XPOSE_STRUCT → DMA_TRANSPOSE` and `..._GATHER_XPOSE_STRUCT →
> DMA_GATHER_TRANSPOSE` separately, and by two separate shipped headers. They are §2.3
> and §2.4 below.

---

## 1. The 16-B hardware BD — `SDMA_CME_BD_DESC`  *(HIGH/CARRIED — C1)*

The atomic ring entry every al_udma SDMA descriptor engine reads. `byte_size 0x10`
(DWARF `dma.hpp:79`). The DWARF lives in `data_transfer.o`, which is **not** in this
extraction → this whole section is **CARRIED**.

**Table 1.0 — `SDMA_CME_BD_DESC` (16 B)**

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `word0` | `SDMA_DESC_WORD0` bitfield (Table 1.1) |
| 4 | 4 | `word1` | `SDMA_CME_DESC_WORD1` CME-command bitfield (Table 1.2) |
| 8 | 8 | `buf_ptr` | u64 SoC address the engine **reads** (TX/M2S) or **writes** (RX/S2M) |

**Table 1.1 — `SDMA_DESC_WORD0` (4 B)** — DWARF bit layout, bit0 = LSB of the dword

| bits | w | field | semantics / valid values |
|------|---|-------|--------------------------|
| [15:0] | b16 | `length_meta` | transfer length in bytes for **this** BD; `0` ⇒ 64 KiB |
| [23:16] | b8 | `netag0_meta` | byte2: `[7:2]` network-tag meta, `[1]` `rsvd_dummy`, `[0]` `md` |
| [31:24] | b8 | byte3 controls | `[7:6]` `ringid`, `[5]` `first`, `[4]` `last`, `[3]` `int_en`, `[2]` `no_snoop`, `[1]` `dmb`, `[0]` `concatenate` |

> **QUIRK (firmware gen-tag repurpose).** The firmware `dma_data_transfer` path packs the
> length in `[15:0]` and a 2-bit GENERATION/RING tag in `[25:24]` = byte3 `[1:0]` (the
> DWARF `dmb`+`concatenate` positions, **not** the DWARF `ringid` at byte3 `[7:6]`).
> Build mask `&0xFCFF0000`. The completion busy-poll matches the **same** two bits:
> `(comp_BD.byte3 & 0x3)`.  *(MED/CARRIED.)*

**Table 1.2 — `SDMA_CME_DESC_WORD1` (4 B)** — CME command / CRC controls

| bits | w | field | semantics / valid values |
|------|---|-------|--------------------------|
| [31:29] | b3 | `block_attr` | block attribute |
| [28:26] | b3 | `cached_crc_iv_index` | CRC IV cache slot (0..7) |
| [25:18] | b8 | `endian_type` | endianness selector |
| [17] | b1 | `validate_crc` | verify the incoming CRC |
| [16] | b1 | `use_stored_crc_iv` | seed CRC from cached IV |
| [15] | b1 | `send_crc` | append computed CRC to the stream |
| [14] | b1 | `store_crc_result` | write CRC result back |
| [13] | b1 | `store_source_crc` | store source-side CRC |
| [12] | b1 | `copy_src_data` | also copy the source payload (CRC+copy) |
| [11:9] | b3 | `optype` | `SDMA_CMETYPE` (Table 1.3a) |
| [8:6] | b3 | `op` | `SDMAOP` (Table 1.3b) |
| [5] | b1 | `write_barrier` | order this BD's write before the next |
| [4:0] | b5 | `netag0_rsvd` | reserved network-tag bits |

**Table 1.3a — `SDMA_CMETYPE`** (`optype`, WORD1[11:9])  ·  **Table 1.3b — `SDMAOP`** (`op`, WORD1[8:6])

| val | CMETYPE | meaning | | val | SDMAOP | meaning |
|-----|---------|---------|-|-----|--------|---------|
| 0 | `COPY` | plain memcpy (default) | | 1 | `DRE` | strided / transpose engine |
| 1 | `CRC32` | CRC-32 | | 2 | `CME` | the COPY/memcpy data path |
| 2 | `CRC32C` | CRC-32C | | 4 | `CCE` | in-flight compute (FMA/ADD/MIN/MAX/convert; §7) |
| 3 | `CKSUM16` | 16-bit checksum | | | | |
| 4 | `CKSUM32` | 32-bit checksum | | | | |
| 5 | `CRC16` | CRC-16 | | | | |
| 6 | `CKSUM32_ADLER` | Adler-32 | | | | |
| 7 | `CRC8` | CRC-8 | | | | |

---

## 2. The six 64-B TPB DMA instruction words  *(HIGH/OBSERVED H1 — D4/D5 CARRIED C2)*

All `ISA_STATIC_ASSERT==64`. `byte0 = opcode`, `byte1 = inst_word_len = 0x10`. Src/dst
start addresses are `ADDR8` (8 B, §5.7). The opcode set (Table 4.0):
`DMAMEMCPY=0xb8`, `DMA_INDIRECT=0xbb`, `DMA_TRANSPOSE=0xbd`, `SB2SB_COLLECTIVE=0xbf`,
`EXTENDED_INST=0xf0`, `DMA_GATHER_TRANSPOSE=0xf1` — all byte-exact in `..._common.h`.

### 2.1  `DIRECT2D` / `DMAMEMCPY` — opcode `0xb8`  *(HIGH/OBSERVED H1)*

`aws_neuron_isa_tpb_dma_direct2d.h:24-43`, `NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT`.

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` (Table 4.1); `opcode=0xb8` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` (Table 4.2) |
| 12 | 1 | `dma_configs` | `DMA_CONFIGS` `{priority_class:3, rsvd:5}` (Table 4.12) |
| 13 | 1 | `semaphore` | completion semaphore index (0..255) |
| 14 | 1 | `sem_increment` | amount added to the completion sema on `dmacomplete` |
| 15 | 1 | `compute_op` | `DGE_COMPUTE_OP` (Table 4.5); `!= NONE` selects the CCE arith path |
| 16 | 8 | `src_start_addr` | `ADDR8` |
| 24 | 8 | `src_step_elem[2]` | `int32` **signed** step for X(dim0)/Y(dim1) (2× i32) |
| 32 | 4 | `src_num_elem[2]` | `uint16` element **count** X/Y (2× u16) |
| 36 | 2 | `src_elem_size` | `uint16` bytes per element (source) |
| 38 | 1 | `src_bound_reg` | `BOUND_CHECK_REG` (Table 4.3) |
| 39 | 1 | `dst_bound_reg` | `BOUND_CHECK_REG` |
| 40 | 8 | `dst_start_addr` | `ADDR8` |
| 48 | 8 | `dst_step_elem[2]` | `int32` **signed** X/Y |
| 56 | 4 | `dst_num_elem[2]` | `uint16` count X/Y |
| 60 | 2 | `dst_elem_size` | `uint16` bytes per element (dest) |
| 62 | 1 | `in_dtype` | `NEURON_ISA_TPB_DTYPE` (Table 4.4b) |
| 63 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` |

**Validity** (`is_valid_dma_memcpy_direct`, verbatim from the header comment): valid
header + events; valid `addr8` src/dst; `is_valid_dge_shape_reg` on each; shape-reg mode
⇒ `num_elem == 0 && step_elem[1] == 0`; valid memcpy dtypes; bound-check regs valid vs
the addr marker; valid `compute_op`. Plain-memcpy (no-CCE) enforces byte equality
`Σ src_num·src_elem == Σ dst_num·dst_elem`; the CCE branch (`is_valid_dma_cce`) instead
enforces aligned start addrs/steps for `compute_op != NONE`.

### 2.2  `INDIRECT1D` / `DMA_INDIRECT` — opcode `0xbb`  *(HIGH/OBSERVED H1)*

`aws_neuron_isa_tpb_dma_indirect1d.h:23-47`, `NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT`.
1-D src/dst (single `step`/`num`), with **two** index-vector addresses.

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `header` | `opcode=0xbb` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` |
| 12 | 1 | `semaphore` | completion semaphore index |
| 13 | 1 | `sem_increment` | completion sema increment |
| 14 | 1 | `idx_num_active_channels` | # active index channels |
| 15 | 1 | `flags` | `DMA_INDIRECT_FLAGS` (Table 4.6) |
| 16 | 8 | `src_start_addr` | `ADDR8` |
| 24 | 4 | `src_step_elem` | `int32` **signed** (one dim — 1-D) |
| 28 | 2 | `src_num_elem` | `uint16` count |
| 30 | 2 | `src_elem_size` | `uint16` bytes/elem (source) |
| 32 | 8 | `dst_start_addr` | `ADDR8` |
| 40 | 4 | `dst_step_elem` | `int32` **signed** |
| 44 | 2 | `dst_num_elem` | `uint16` count |
| 46 | 2 | `dst_elem_size` | `uint16` bytes/elem (dest) |
| 48 | 4 | `src_idx_start_addr` | `ADDR4` (gather index vector, SBUF partition 0) |
| 52 | 4 | `dst_idx_start_addr` | `ADDR4` (scatter index vector, SBUF partition 0) |
| 56 | 1 | `in_dtype` | `DTYPE` (src) |
| 57 | 1 | `out_dtype` | `DTYPE` (dst) |
| 58 | 1 | `src_idx_bound_reg` | `BOUND_CHECK_REG` — gates the **src index** array |
| 59 | 1 | `dst_idx_bound_reg` | `BOUND_CHECK_REG` — gates the **dst index** array |
| 60 | 1 | `compute_op` | `DGE_COMPUTE_OP`; `!= NONE` ⇒ scatter-add (reduce) |
| 61 | 1 | `dma_configs` | `DMA_CONFIGS` `{priority_class:3, rsvd:5}` |
| 62 | 2 | `reserved[2]` | must be zero (`has_dma_indirect_check_reserved_zero`) |

**Key vs `DIRECT2D`:** the bound regs at `+58`/`+59` gate the **index** arrays (addressing
is index-driven); the two index addrs (`+48` src / `+52` dst) let one instruction gather
**and** scatter. Index addrs must lie in SBUF partition 0
(`has_dma_indirect_idx_addr_in_sbuf_partition0`).

> **NOTE (index count ≤ 4096).** The header's `has_dma_indirect_valid_index_count`
> caps each indirect leg at `num_elem <= 4096` (waived in shape-from-register mode), not
> the "128 lanes" of an earlier draft. `idx_num_active_channels` (the active index
> channels) is the separate per-lane control checked by `check_dma_indirect_indices`.

### 2.3  `DIRECT2D_XPOSE` / `DMA_TRANSPOSE` — opcode `0xbd`, NC-v3+  *(HIGH/OBSERVED H1)*

`aws_neuron_isa_tpb_dma_direct2d_xpose.h:24-42`,
`NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT`. **Tile-geometry-driven** xbar transpose: no
index array; the transpose is described by a static tile (`tile_src_rows × tile_src_cols`)
plus a `tile_src_row_step`. Identical struct body across cayman/mariana/maverick.

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `header` | `opcode=0xbd` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` |
| 12 | 1 | `src_bound_reg` | `BOUND_CHECK_REG` |
| 13 | 1 | `dst_bound_reg` | `BOUND_CHECK_REG` |
| 14 | 1 | `in_dtype` | `DTYPE` (2-B or 4-B only) |
| 15 | 1 | `out_dtype` | `DTYPE` (must equal `in_dtype`) |
| 16 | 8 | `src_start_addr` | `ADDR8` |
| 24 | 8 | `src_step_elem[2]` | `int32` **signed** X/Y (2× i32) |
| 32 | 4 | `src_num_elem[2]` | `uint16` X/Y (2× u16) |
| 36 | 4 | `tile_src_row_step` | `int32` **signed** — per-row source step inside a tile |
| 40 | 8 | `dst_start_addr` | `ADDR8` (must be **32-B aligned**) |
| 48 | 8 | `dst_step_elem[2]` | `int32` **signed** X/Y (each `%32==0` unless `num==1`) |
| 56 | 4 | `dst_num_elem[2]` | `uint16` X/Y |
| 60 | 1 | `tile_src_rows` | `uint8` — must be **16** (current uCode) |
| 61 | 1 | `tile_src_cols` | `uint8` — `1..128` for 2-B `out_dtype`, `1..64` for 4-B |
| 62 | 1 | `semaphore` | completion semaphore index |
| 63 | 1 | `dma_configs` | `DMA_CONFIGS` `{priority_class:3, rsvd:5}` |

**Validity** (`is_valid_dma_transpose`): NC ≥ V3; `tile_src_rows == 16`; tile-cols/dtype
size co-constraint above; `in_dtype == out_dtype` and 2-B for the transpose proper;
`dst_start_addr % 32 == 0`; `dst_step_elem[i] % 32 == 0 || dst_num_elem[i] == 1`.

> **QUIRK (header comment typo).** The header annotates `out_dtype` as `// 5 (15)`. The
> `5` is a stray width column; `out_dtype` is a **1-byte** `DTYPE` at **offset 15**
> (`src_start_addr` immediately follows at 16, and `ISA_STATIC_ASSERT==64` holds). Lay it
> as 1 byte.

> **GOTCHA (vs `GATHER_XPOSE`).** `DIRECT2D_XPOSE` has **no** index field and **no**
> `idx_num_active_channels`; the transpose source is a contiguous 2-D tile. `GATHER_XPOSE`
> (§2.4) is the index-array variant. They share an opcode prefix family but **not** a
> layout — do not alias them.

### 2.4  `GATHER_XPOSE` / `DMA_GATHER_TRANSPOSE` — opcode `0xf1`, NC-v3+  *(HIGH/OBSERVED H1)*

`aws_neuron_isa_tpb_dma_gather_xpose.h:40-59`, `NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT`.
**Index-array-driven** gather-then-xbar-transpose (SW-DGE on the Q7). Note `dtype` is a
**`DTYPE_PAIR`** (Table 4.4a) and `dst_step_elem_0` is **implicit** (`== sizeof(dtype_hi)`).

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `header` | `opcode=0xf1` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` |
| 12 | 1 | `semaphore` | completion semaphore index |
| 13 | 1 | `dma_configs` | `DMA_CONFIGS` `{priority_class:3, rsvd:5}` |
| 14 | 1 | `idx_num_active_channels` | # active index channels |
| 15 | 1 | `src_idx_bound_reg` | `BOUND_CHECK_REG` — gates the index array |
| 16 | 8 | `src_start_addr` | `ADDR8` (HBM or SBUF; immediate `%2==0`) |
| 24 | 8 | `src_step_elem[2]` | `int32` **signed** X/Y (2× i32) |
| 32 | 4 | `src_num_elem[2]` | `uint16` X/Y; gather dim (Y) `%16==0` |
| 36 | 4 | `src_idx_start_addr` | `ADDR4` (index tensor, UINT32, immediate `%4==0`) |
| 40 | 8 | `dst_start_addr` | `ADDR8` (SBUF, must be **32-B aligned**) |
| 48 | 3 | `reserved[3]` | must be zero |
| 51 | 1 | `dst_bound_reg` | `BOUND_CHECK_REG` |
| 52 | 4 | `dst_step_elem_1` | `int32` **signed** (`dst_step_elem_0 == sizeof(dtype_hi)`) |
| 56 | 4 | `dst_num_elem[2]` | `uint16` X/Y; `dst_num_elem[0] == src_num_elem[Y]` |
| 60 | 2 | `elem_size` | `uint16`; valid range `[2,256]`, **even** |
| 62 | 1 | `dtype` | `DTYPE_PAIR` `{dtype_lo:4 src, dtype_hi:4 dst}`; `lo==hi`, 2-B |
| 63 | 1 | `flags` | `{gather_dim:2 (INDIRECT_DIM), reserved:6}`; `gather_dim == Y` |

**Constraints** (header): 2-B dtype only (`dtype_lo == dtype_hi`, BF16/FP16); index UINT32
in multiples of 16; `gather_dim` must be Y (slow axis) initially; dst 32-B aligned (xbar);
16×128 xbar transpose tiles for 2-B dtypes. Use cases: MoE MLP, chunked-prefill attention.

### 2.5  `RDMA_DESC_GEN` / `EXTENDED` `op8` — cross-die SBUF→SBUF P2P  *(HIGH/CARRIED C2; envelope re-verified H1)*

The 64-B word is a `NEURON_ISA_TPB_EXTENDED_STRUCT`
(`aws_neuron_isa_tpb_extended.h:41-48`): `header@0`, `events@4`, `extended_opcode@12`,
`completion_info@13` (Table 4.7), `inst_specific0[18]@14..31`, `inst_specific1[32]@32..63`.
`op8` (firmware `@IRAM 0x161f4`) populates the `inst_specific` region:

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `header` | `opcode=0xf0` (`EXTENDED_INST`) |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` |
| 12 | 1 | `extended_opcode` | `== 8` (`RDMA_DESC_GEN`) |
| 13 | 1 | `completion_info` | `EXT_COMPLETION_INFO` (Table 4.7) |
| 14 | 1 | `local_sem` | u8 — local (source-release) semaphore index |
| 15 | 1 | `remote_sem` | u8 — remote (data-ready) semaphore index |
| 16 | 4 | `remote_core_id` | IMM — peer core id |
| 20 | 4 | `remote_routing_id` | IMM — folded into the SoC high bits (CAYMAN_ID/EXIT_DIE) |
| 24 | 4 | `dma_engine_mask` | IMM, power-of-2 fan `{1,2,4,8,16}` active engines |
| 28 | 4 | `src_addr` | ADDR (local SBUF partition offset) |
| 32 | 4 | `dst_addr` | ADDR (peer LOCAL offset; rewritten to routed SoC addr) |
| 36 | 4 | `free_dim_bytes` | u32 — bytes/partition copied across all 128 SBUF parts |
| 40 | 1 | `is_bidirectional` | u8 — 1 = bidirectional leg |
| 41 | 1 | `remote_sem_prev` | u8 — prior remote-sema index (chained barrier) |
| 42 | — | reserved | (to 63) |

**Effect:** builds, per masked engine, a CME-COPY BD ring copying
`[src, src+free_dim_bytes]` across all 128 SBUF partitions, plus a local sema BD
(`EVT_SEM.inc(local_sem)`) and a remote sema BD (`EVT_SEM.inc(remote_sem)` routed to the
peer). The routing-id fold and the two-semaphore protocol are in the data-movement
reference.

### 2.6  `RDMA_DESC_START` / `EXTENDED` `op9`  *(HIGH/CARRIED C2)*

Firmware `@IRAM 0x1723c`. Same `EXTENDED_STRUCT` envelope; carries **no** operands.

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `header` | `opcode=0xf0` (`EXTENDED_INST`) |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` — the **wait** conditions sit here |
| 12 | 1 | `extended_opcode` | `== 9` (`RDMA_DESC_START`) |
| 13 | 1 | `completion_info` | `EXT_COMPLETION_INFO` |
| 14 | — | (no operands) | reserved to 63 |

**Effect:** consumes/drains the ring `op8` built; role-splits TX (M2S `TDRTP_inc`) vs RX
(S2M `RDRTP_inc`) by PRID parity and doorbells. `GEN` can be issued early/free; the wait
sits on `START`.

---

## 3. The v5 (Maverick / NC-v5) inline forms  *(HIGH/OBSERVED H1; runtime behavior INFERRED)*

v5 supersedes the GENERATE/DIMPUSH/REGWRITE emit by folding the 16-B BD **inline**. Both
structs are shipped in the `neuron_maverick_arch_isa` headers (copyright comment "NC-v5"),
`ISA_STATIC_ASSERT==64`, and `struct2opcode` (H3) maps them to `DMA_IMMEDIATE` / `DMA_MEMCPY2`.

> **NOTE.** Field **layout** is HIGH/OBSERVED (shipped header + `struct2opcode`). The
> firmware-side **runtime semantics** of these v5 forms are not exercised in this
> extraction's firmware image → treat the *behavior* (not the layout) as INFERRED.

### 3.1  `DMA_IMMEDIATE` — opcode `0xba`  ·  `aws_neuron_isa_tpb_dma_immediate.h:36-46`

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `header` | `opcode=0xba` |
| 4 | 4 | `wait_sema_value` | u32 (imm or reg) |
| 8 | 1 | `wait_sema_idx` | u8 (imm or reg) |
| 9 | 1 | `wait_sema_mode` | `NEURON_ISA_TPB_WAIT_MODE` (Table 4.13) |
| 10 | 1 | `dma_engines` | `DMA_ENGINE_CONFIG` (Table 4.8); restricted to `engine_count == ONE` |
| 11 | 1 | `dma_flags` | `DMA_FLAGS` `{tdg:1, wr_done_sync:1, queue_id:3, rsvd:3}`; `tdg` must be 0 |
| 12 | 1 | `descriptor_flags` | `DESC_FLAGS` `{num_descriptors:2, desc0_src:1, desc1_src:1, desc2_src:1, rsvd:3}` |
| 13 | 3 | `descriptor_reg[3]` | register selectors for the 3 inline descriptors |
| 16 | 48 | `descriptor_imm[3]` | 3× `DESCRIPTOR_RAW` (`uint8[16]`) — the §1 16-B BDs folded inline |

Each `DESCRIPTOR_RAW[16]` is laid out as the §1 `SDMA_CME_BD_DESC`. `num_descriptors`
must be `1/2/3`; per-descriptor `descN_src` chooses immediate (the inline `[16]`) vs the
4×u32 register set named by `descriptor_reg[N]` (`DMA_DESC_MODE` `IMMEDIATE=0` / `REGISTER=1`).

### 3.2  `DMA_MEMCPY2` / `DMA_COPY2D` — opcode `0xb9`  ·  `aws_neuron_isa_tpb_dma_copy2d.h:22-43`

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `header` | `opcode=0xb9` |
| 4 | 4 | `wait_sema_value` | u32 (imm or reg) |
| 8 | 1 | `wait_sema_idx` | u8 (imm or reg) |
| 9 | 1 | `wait_sema_mode` | `WAIT_MODE` (Table 4.13) |
| 10 | 1 | `dma_engines` | `DMA_ENGINE_CONFIG` `{start_id:5, engine_count:2, rsvd:1}` (Table 4.8) |
| 11 | 1 | `dma_flags` | `DMA_FLAGS` `{tdg:1, wr_done_sync:1, queue_id:3, rsvd:3}` |
| 12 | 1 | `addr_mode` | `DMA_ADDR_MODE_PAIR` `{src:4 low, dst:4 high}` (Table 4.9) |
| 13 | 1 | `sema_update_mode` | `DMA_SEMA_UPDATES` `{rd_done:4, wr_done:4}` (Table 4.10) |
| 14 | 2 | `rd_done_sema_update_idx` | u16 read-done sema index (imm or reg) |
| 16 | 8 | `src_start_addr` | `DMA_ADDR_UNION` (8 B; `imm64` or reg-ptr struct) |
| 24 | 8 | `src_step_elem[2]` | `int32` **signed** X/Y |
| 32 | 4 | `src_num_elem[2]` | `uint16` X/Y |
| 36 | 2 | `src_elem_size` | `uint16` |
| 38 | 2 | `wr_done_sema_update_idx` | u16 write-done sema index (imm or reg) |
| 40 | 8 | `dst_start_addr` | `DMA_ADDR_UNION` (8 B) |
| 48 | 8 | `dst_step_elem[2]` | `int32` **signed** X/Y |
| 56 | 4 | `dst_num_elem[2]` | `uint16` X/Y |
| 60 | 2 | `dst_elem_size` | `uint16` |
| 62 | 1 | `remote_core_id` | `REG_NUM` (peer for the P2P case) |
| 63 | 1 | `reserved` | must be zero |

> **GOTCHA (`rd_done`/`wr_done` index split).** The read-done index sits at `+14` (in the
> header half) and the write-done index at `+38` (in the middle, between the src and dst
> address blocks) — they are **not** contiguous.

> **QUIRK (`queue_id` width).** The header comment says "4b queue ID", but the actual C
> bitfield `DMA_FLAGS.queue_id` is **3 bits** (`queue_id:3`, common.h). Trust the struct,
> not the comment — queue id is 0..7.

---

## 4. Shared sub-structs (referenced by every word above)  *(HIGH/OBSERVED H1)*

All from `..._common.h` (mariana; maverick identical for the shared forms; v5 enums
§4.8–4.13 from the maverick header). Packed structs list fields LSB-first within the byte.

**Table 4.1 — `NEURON_ISA_TPB_HEADER` (4 B)**  ·  **Table 4.2 — `NEURON_ISA_TPB_EVENTS` (8 B)**

| off | w | HEADER | | off | w | EVENTS |
|----|---|--------|-|----|---|--------|
| 0 | 1 | `opcode` (Table 4.0) | | 0 | 1 | `wait_mode` (`WAIT_MODE`, Table 4.13) |
| 1 | 1 | `inst_word_len` = `0x10` | | 1 | 1 | `wait_idx` (u8 sema to wait on) |
| 2 | 1 | `debug_cmd` | | 2 | 1 | `update_mode` (`UPDATE_MODE`, Table 4.14) |
| 3 | 1 | `debug_hint` | | 3 | 1 | `update_idx` (u8 sema to update) |
| | | | | 4 | 4 | `semaphore_value` (u32 wait threshold) |

> The `events.semaphore`/`sem_increment` *completion* fields inside each descriptor
> (e.g. `DIRECT2D +13/+14`) are **distinct** from the `EVENTS` wait/update pair above:
> `EVENTS` gates **issue**; the descriptor's own `semaphore` posts **completion**.

**Table 4.0 — `NEURON_ISA_TPB_OPCODE` (DMA-relevant values)**  *(HIGH/OBSERVED H1)*

| val | mnemonic | struct | | val | mnemonic | struct |
|-----|----------|--------|-|-----|----------|--------|
| `0x68` | `GATHER` | (vector gather) | | `0xbf` | `SB2SB_COLLECTIVE` | `S3D3_COLLECTIVE` |
| `0xb8` | `DMAMEMCPY` | `DMA_DIRECT2D` (§2.1) | | `0xd4` | `PSEUDO_DMA_DIRECT2D` | compiler pseudo |
| `0xb9` | `DMA_MEMCPY2` | `DMA_COPY2D` (§3.2) | | `0xe7` | `INDIRECT_COPY` | (vector indirect copy) |
| `0xba` | `DMA_IMMEDIATE` | `DMA_IMMEDIATE` (§3.1) | | `0xf0` | `EXTENDED_INST` | `EXTENDED_STRUCT` (§2.5/2.6) |
| `0xbb` | `DMA_INDIRECT` | `DMA_INDIRECT1D` (§2.2) | | `0xf1` | `DMA_GATHER_TRANSPOSE` | `DMA_GATHER_XPOSE` (§2.4) |
| `0xbd` | `DMA_TRANSPOSE` | `DMA_DIRECT2D_XPOSE` (§2.3) | | | | |

**Table 4.3 — `NEURON_ISA_TPB_BOUND_CHECK_REG` (1 B, packed)**

| bits | field | semantics / valid values |
|------|-------|--------------------------|
| b0..5 | `bc_reg:6` | `REG_NUM` holding the buffer/table upper **limit**; wide-offset ⇒ `bc_reg+1` = high 32 bits |
| b6 | `bc_disable_oob_error_notif:1` | suppress the OOB-error notification |
| b7 | `bc_enabled:1` | 1 = armed; 0 = inert |

**Validity** (`has_valid_bound_check_reg`): inert iff `(bc_enabled==0 && bc_reg==0 &&
bc_disable_oob_error_notif==0)`, **or** armed iff `(bc_enabled==1 &&
is_valid_register_read_with_marker(bc_reg, marker))`. A half-set register (enabled but
`bc_reg==0` with notif on) is **invalid**.

**Table 4.4a — `DTYPE_PAIR` (1 B)**  ·  **Table 4.4b — `DTYPE` enum values (selected)**

| bits | DTYPE_PAIR | | val | DTYPE | val | DTYPE |
|------|-----------|-|-----|-------|-----|-------|
| b0..3 | `dtype_lo:4` (src) | | `0x2` | INT8 | `0xA` | FP32 |
| b4..7 | `dtype_hi:4` (dst) | | `0x3` | UINT8 | `0xB` | FP32R |
| | (`GATHER_XPOSE`: `lo==hi`) | | `0x6` | BFLOAT16 | `0xD` | FP8_EXP3 |
| | | | `0x7` | FP16 | `0xE` | FP8_EXP4 |
| | | | `0x8` | INT32 | `0xF` | FP8_EXP5 |

> The 4-bit `DTYPE_BASIC` used inside `DTYPE_PAIR` and the wider `DTYPE` enum share these
> low values; `DTYPE` additionally encodes `FP4_EXP2=0x10` and the CPTC trellis dtypes
> `0x19..0x1F` (5-bit), which do **not** fit a `DTYPE_PAIR` nibble.

**Table 4.5 — `DGE_COMPUTE_OP` (1 B enum)**  ·  **Table 4.6 — `DMA_INDIRECT_FLAGS` (1 B, packed)**

| val | DGE_COMPUTE_OP | | bits | DMA_INDIRECT_FLAGS |
|-----|----------------|-|------|--------------------|
| `0x00` | `NONE` (B = A; memcpy) | | b0..1 | `indirect_mode:2` — SRC=0 (gather), DST=1 (scatter), SRC_DST=2 |
| `0x01` | `ADD` (B += A) | | b2 | `idx_bound_is_err:1` — OOB index is an error (vs skip) |
| `0x02` | `MULTIPLY` (B *= A) | | b3 | `non_unique_dst_idx:1` — permit duplicate scatter idx (scatter-ADD) |
| `0x03` | `MAX` (B = max(A,B)) | | b4..5 | `gather_dim:2` — `INDIRECT_DIM` X/Y/Z/W = 0/1/2/3 |
| `0x04` | `MIN` (B = min(A,B)) | | b6..7 | `scatter_dim:2` — `INDIRECT_DIM` X/Y/Z/W = 0/1/2/3 |

> `INDIRECT_DIM`: `X=0, Y=1, Z=2, W=3` — **minor2major** (X fastest), the **reverse** of
> numpy major2minor. When no shape-reg is used, the indirect dim must be `X` (only dim).

**Table 4.7 — `EXT_COMPLETION_INFO` (1 B, packed)** — `extended.h:22-28`

| bits | field | semantics / valid values |
|------|-------|--------------------------|
| b0 | `has_read:1` | the inst issues ≥1 read tensor |
| b1 | `reserved0:1` | must be zero |
| b2 | `has_write:1` | the inst issues ≥1 write tensor |
| b3..5 | `num_active_ports:3` | `0` ⇒ 8 ports (128 partitions); `1..7` ⇒ that many ports (16 partitions/port, one Q7/port from `q7[0]`) |
| b6..7 | `reserved1:2` | must be zero |

**Table 4.8 — `DMA_ENGINE_CONFIG` (1 B)** *(v5)*  ·  **`DMA_ENGINE_COUNT` enum**

| bits | DMA_ENGINE_CONFIG | | val | DMA_ENGINE_COUNT |
|------|-------------------|-|-----|------------------|
| b0..4 | `start_id:5` (first engine id) | | 0 | `ONE` (fan 1) |
| b5..6 | `engine_count:2` (`DMA_ENGINE_COUNT`) | | 1 | `TWO` (fan 2) |
| b7 | `reserved:1` | | 2 | `FOUR` (fan 4) |
| | | | 3 | `EIGHT` (fan 8) |

**Table 4.9 — `DMA_ADDR_MODE` (the 12 v5 addressing modes; `addr_mode` pair @+12)** *(HIGH/OBSERVED H1)*

| val | mode | val | mode |
|-----|------|-----|------|
| `0x0` | `IMM64` | `0x6` | `REG_REG_OFFSET64` |
| `0x1` | `REG_PAIR` | `0x7` | `REG_REG_OFFSET64_I` |
| `0x2` | `REG_IMM_OFFSET32` | `0x8` | `REG_REG_IMM_SCALE` |
| `0x3` | `REG_IMM_OFFSET32_I` | `0x9` | `REG_REG_IMM_SCALE_I` |
| `0x4` | `REG_REG_OFFSET32` | `0xa` | `REG_REG_REG_SCALE` |
| `0x5` | `REG_REG_OFFSET32_I` | `0xb` | `REG_REG_REG_SCALE_I` |

`addr_mode` byte = `{src_addr_mode:4 low, dst_addr_mode:4 high}` (`DMA_ADDR_MODE_PAIR`).

**Table 4.10 — `DMA_SEMA_UPDATE_MODE` (per-leg sema/collsync update)** *(v5; HIGH/OBSERVED H1)*

| val | mode | | val | mode |
|-----|------|-|-----|------|
| `0x0` | `NONE` | | `0x9` | `REMOTE_SEM_INC` |
| `0x1` | `LOCAL_SEM_INC` | | `0xb` | `REMOTE_SEM_INC_COUNTER` |
| `0x3` | `LOCAL_SEM_INC_COUNTER` | | `0xd` | `REMOTE_COLLSYNC_INC` |
| `0x5` | `LOCAL_COLLSYNC_INC` | | | |

Bit decode: bit0 enabled, bit1 inc/counter, bit2 sem-vs-collsync, bit3 local-vs-remote.
`sema_update_mode` byte @+13 = `{rd_done:4 low, wr_done:4 high}` (`DMA_SEMA_UPDATES`).

**Table 4.11 — `DGE_OPCODE` (the @+15 KIND, compiler pseudo only)**

| val | name | resolves to real opcode |
|-----|------|-------------------------|
| 0x0 | `DMA_DIRECT2D` | `0xb8` |
| 0x1 | `DMA_INDIRECT1D` | `0xbb` |
| 0x2 | `DMA_TRANSPOSE` | `0xbd` |
| 0x3 | `DMA_GATHER_TRANSPOSE` | `0xf1` |

> **QUIRK (offset +15 duality).** In a **compiler pseudo** word (`PSEUDO_DMA_DIRECT2D
> 0xd4`) byte `+15` is `dge_op` (`DGE_OPCODE` 0..3); in the **resolved real** word
> (`DMAMEMCPY 0xb8`) the **same byte** is `compute_op` (`DGE_COMPUTE_OP` 0..4). Two
> fields, one byte, keyed by `byte0` (the opcode).

**Table 4.12 — `DMA_CONFIGS` (1 B, packed)** [`DIRECT2D +12` / `INDIRECT1D +61` / `XPOSE +63`]

| bits | field | semantics / valid values |
|------|-------|--------------------------|
| b0..2 | `priority_class:3` | device DGE priority class (valid 0..4 → drives AXI QoS; §6) |
| b3..7 | `reserved_bitfield:5` | must be zero |

> **NOTE (two QoS namespaces).** This device `priority_class` is a **3-bit, 5-value**
> field (P0..P4) — the firmware map `nrtucode_core_dge_get_priority_class_map@0x9b10f0`
> bounds the index with `if (a3 <= 4)`. The **compiler-side** `DMAQoSClass` is a separate
> **0..14** enum that the device path saturates down into `priority_class` (17→5→4 cap).
> Keep them distinct: a descriptor byte holds `priority_class` (0..4), never `DMAQoSClass`.

**Table 4.13 — `WAIT_MODE` (selected)**  ·  **Table 4.14 — `UPDATE_MODE` (selected)** *(HIGH/OBSERVED H1)*

| val | WAIT_MODE | | val | UPDATE_MODE |
|-----|-----------|-|-----|-------------|
| `0x0` | `NONE` | | `0x00` | `NONE` |
| `0x1` | `WAIT_FOR_SEM_EQ_IMM` | | `0x03` | `SEM_INC_READ` (on read-complete) |
| `0x4` | `WAIT_FOR_SEM_GT_IMM` | | `0x13` | `SEM_INC_COMPLETE` (on full complete) |
| `0x5` | `WAIT_FOR_SEM_GE_IMM` | | `0x15` | `SEM_ADD_IMM_COMPLETE` |
| `0x81..0x85` | `..._REG` variants (reg threshold) | | `0x99` | `SEM_WR_REG_COMPLETE` |

> **CORRECTION.** An earlier draft wrote "`wait_mode 0x04` = wait ≥ value" and
> "`update_mode 0x13` = inc-on-complete". Per the enum (common.h): `0x04` is
> `WAIT_FOR_SEM_GT_IMM` (**strictly greater**; `GE` is `0x05`), and `0x13` is
> `SEM_INC_COMPLETE` (correct). Use `0x05` for "wait ≥".

### 5. The ADDR8 / ADDR4 marker byte (high byte; mask `0xFC`)  *(HIGH/OBSERVED H1)*

`ADDR8` is an 8-B union (`common.h:603`); its **high byte** is the `marker` selecting the
addressing form. `ADDR4` is the 4-B compute-word address (`common.h:532`).

**Table 5a — ADDR8 marker constants (`MASK 0xFC`)**  ·  **Table 5b — ADDR4 markers**

| marker | ADDR8 name | meaning (addr : shape) | | marker | ADDR4 name |
|--------|-----------|------------------------|-|--------|-----------|
| `0x00` | `IMM` | addr imm · shape imm | | `0x00` | `IMM` |
| `0x40` | `SHAPE_REG` | addr imm · shape reg (bit6; `num_elem` must be 0) | | `0x40` | `SHAPE_REG` |
| `0x80` | `ADDR_REG` | addr reg · shape imm (bit7) | | `0x80` | `ADDR_REG` |
| `0xC0` | `ADDR_SHAPE_REG` | addr reg · shape reg | | `0xC0` | `ADDR_SHAPE_REG` |
| `0x20` | `ADDR_TBL_SHAPE_IMM` | addr tbl · shape imm (bit5) | | `0x20` | `INDIRECT_IMM` |
| `0x60` | `ADDR_TBL_SHAPE_REG` | addr tbl · shape reg (bit5\|bit6) | | `0xA0` | `INDIRECT_REG` |
| `+0x08` | `ADDR_TBL_OFFSET_REG` | (bit3) table offset from register | | | |
| `+0x04` | `ADDR_TBL_OFFSET_WIDE` | (bit2) wide (64-bit) table offset | | | |

Bit defs (ADDR8): `OFFSET_WIDE = 1<<2`, `OFFSET_REG = 1<<3`, `ADDR_TBL = 1<<5`,
`SHAPE_REG = 1<<6`, `ADDR_REG = 1<<7`; `MASK = 0xFC`. A `SHAPE_REG` marker selects
shape-from-register (runtime-sized) mode: `num_elem` **must** be 0 and the index cap is
waived. The `PSEUDO_ADDR8` union adds `addr_var` (NEFF variable id) + `addr_unknown`.

---

## 6. The al_udma v4 ring register bank  *(HIGH/OBSERVED H2)*

`UnitName UDMA_M2S` (`AddrWidth 18`, `SizeInBytes 0x40000`) / `UDMA_S2M`
(`SizeInBytes 0x38000`). Engine-global bundle bases (`RegistersBundleArrays`
`AddressOffset`, re-verified H2):

| M2S bundle | base | M2S bundle | base | S2M bundle | base | S2M bundle | base |
|-----------|------|-----------|------|-----------|------|-----------|------|
| `AXI_M2S` | `0x100` | `M2S_comp` | `0x400` | `AXI_S2M` | `0x100` | `S2M_comp` | `0x380` |
| `M2S` | `0x200` | `M2S_stat` | `0x500` | `S2M` | `0x200` | `S2M_lma` | `0x3c0` |
| `M2S_rd` | `0x300` | `M2S_feature` | `0x600` | `S2M_rd` | `0x300` | `S2M_stat` | `0x500` |
| `M2S_dwrr` | `0x340` | `M2S_shadow_access` | `0x700` | `S2M_wr` | `0x340` | `S2M_feature` | `0x600` |
| `M2S_rate_limiter` | `0x380` | `M2S_resp_err` | `0x900` | `M2S_stream_rate_limiter` | `0x3c0` | `S2M_resp_err` | `0x900` |
| `M2S_Q[16]` | `0x1000` | `M2S_DYN_MTU` | `0x38000` | `S2M_Q[16]` | `0x1000` | `S2M_DYN_MTU` | `0x33000` |
| `ap_trfc_gen` | `0x39800` | | | | | | |

Each per-queue bank is at `0x1000 + i*0x1000` (`i = 0..15`, stride `0x1000`). Offsets
below are **relative** to the bank base; an absolute address is `0x1000 + i*0x1000 + off`.

> **The two doorbells.** `TDRTP_inc` (TX/M2S) and `RDRTP_inc` (RX/S2M) are both at bank
> `+0x38` → absolute `0x1038` for queue 0. Each has a single field `val[23:0]` (24-bit,
> `VAL_MASK 0xffffff`): writing `N` increments the tail by `N` descriptors = the launch.

### 6.1  `M2S_Q[i]` — 39 registers (`al_udma_m2s_regs.json`)  *(HIGH/OBSERVED H2, count = 39)*

| off | w | R/W | register | role / key bitfields |
|----|---|-----|----------|----------------------|
| `0x00` | 4 | RW | `desc_pref_cfg` | `{fifo_depth[7+LOGQ:0]`, `fifo_start_addr[22+LOGQ:16]}` prefetch-FIFO carveout |
| `0x04` | 4 | RW | `desc_pref_cfg2` | prefetch policy 2 |
| `0x08` | 4 | RO | `desc_pref_fifo` | prefetch-FIFO state |
| `0x0c` | 4 | RO | `hdr_pref_fifo` | header prefetch-FIFO state |
| `0x18` | 4 | RW | `desc_rd_aruser` | AXI ARUSER for the descriptor **read** |
| `0x1c` | 4 | RW | `cmpl_wr_awuser` | AXI AWUSER for the completion **write** |
| `0x20` | 4 | RW | `cfg` | see Table 6.1a below |
| `0x24` | 4 | RO | `status` | `{q_used[24:0], q_isolated[26], axi_rd_timeout[27], prefetch[28], scheduler[29], q_dmb[30], q_full[31]}` |
| `0x28` | 4 | RW | `TDRBP_low` | TX Desc Ring Base Ptr `addr[31:6]` (64-B aligned; rst 1) |
| `0x2c` | 4 | RW | `TDRBP_high` | TX Desc Ring Base Ptr `[63:32]` |
| `0x30` | 4 | RW | `TDRL` | TX Desc Ring **length** `offset[23:0]` (in descriptors) |
| `0x34` | 4 | RO | `TDRHP` | TX Desc Ring **head** ptr (next BD the engine prefetches) |
| `0x38` | 4 | RW | `TDRTP_inc` | **the doorbell**: `val[23:0]` increments `Q_TDRTP` by N = launch outbound DMA |
| `0x3c` | 4 | RO | `TDRTP` | TX Desc **tail** ptr (current accumulated tail) |
| `0x40` | 4 | RO | `TDCP` | TX Desc **current** ptr (in-flight) |
| `0x44` | 4 | RW | `TCRBP_low` | TX Completion Ring Base Ptr `[31:6]` |
| `0x48` | 4 | RW | `TCRBP_high` | TX Completion Ring Base Ptr `[63:32]` |
| `0x4c` | 4 | RO | `TCRHP` | TX Completion Ring **head** ptr (HW-advanced; SW polls) |
| `0x50` | 4 | RO | `TCRHP_internal` | internal completion head (pre-coalescing) |
| `0x54` | 4 | RW | `gdma_pref` | generic-DMA prefetch ctrl |
| `0x58` | 4 | RW | `cfg_2` | ring config 2 |
| `0x60` | 4 | RW | `rate_limit_cfg_1` | token-bucket rate-limit config |
| `0x64` | 4 | RW | `rate_limit_cfg_cycle` | rate-limit cycle config |
| `0x68` | 4 | RW | `rate_limit_cfg_token_size_1` | |
| `0x6c` | 4 | RW | `rate_limit_cfg_token_size_2` | |
| `0x70` | 4 | RW | `rate_limit_sw_ctrl` | |
| `0x74` | 4 | RW | `rate_limit_mask` | |
| `0x80` | 4 | RW | `dwrr_cfg_1` | DWRR scheduler config 1 |
| `0x84` | 4 | RW | `dwrr_cfg_2` | `{q_qos[7:0]}` DWRR per-queue QoS |
| `0x88` | 4 | RW | `dwrr_cfg_3` | `{weight[7:0]}` DWRR per-queue weight |
| `0x8c` | 4 | RW | `dwrr_sw_ctrl` | |
| `0xa0` | 4 | RW | `comp_cfg` | `{en_comp_ring_update[0]` (rst **0**), `dis_comp_coal[1]}` |
| `0xb0` | 4 | RW | `q_sw_ctrl` | `{rst_dmb[0], rst_tail_ptr[1], rst_head_ptr[2], rst_current_ptr[3], q_isolation[4], rst_q[8]}` |
| `0xc0` | 4 | RO | `q_tx_pkt` | transmitted-packet count |
| `0xd0` | 4 | RW | `read_data_snp` | snoop status |
| `0xd4` | 4 | WO | `TDRTP_set` | direct-**set** the tail pointer (maverick) |
| `0xd8` | 4 | WO | `TDRHP_set` | direct-**set** the head pointer (maverick) |
| `0x100` | 4 | RW | `force_target_q_mapping` | |
| `0x104` | 4 | RW | `out_of_order` | |

**Table 6.1a — M2S `cfg` (`+0x20`) bitfields**  *(HIGH/OBSERVED H2)*

| bits | field | rst | bits | field | rst |
|------|-------|-----|------|-------|-----|
| [15:0] | `pkt_len_offset` | 0 | [21] | `id_priority_filter_en_pref` | 1 |
| [16] | `en_pref` | 0 | [22] | `id_priority_filter_en_data` | 1 |
| [17] | `en_scheduling` | 0 | [23] | `bw_fix_tx_pkthdr` | 0 |
| [18] | `force_full_line` | **1** | [27:24] | `axi_awcache_comp` | 3 |
| [19] | `vmid_check_en` | 0 | [31:28] | `AXI_qos` | `0xe` |
| [20] | `allow_lt_min_pref` | 0 | | | |

> **CORRECTION (vs earlier `cfg` notes).** Per the RTL JSON: `en_pref[16]` +
> `en_scheduling[17]` are the **enable** bits; `force_full_line` is **bit 18** (not 31);
> `AXI_qos` is `[31:28]` (4-bit, reset `0xe`), not `[30:28]`. This wire QoS is the
> 4-bit AXI value; the descriptor's 0..4 `priority_class` (§4.12) feeds it via the QoS map.

### 6.2  `S2M_Q[i]` — 36 registers (`al_udma_s2m_regs.json`)  *(HIGH/OBSERVED H2, count = 36)*

Byte-for-byte the M2S ring renamed `TDR→RDR` / `TCR→RCR`, with S2M-specific completion +
packet-handler regs and **no DWRR / no rate-limiter** (RX is QoS-tagged, not
bandwidth-shaped).

| off | w | R/W | register | role / key bitfields |
|----|---|-----|----------|----------------------|
| `0x00` | 4 | RW | `desc_pref_cfg` | prefetch carveout |
| `0x04` | 4 | RW | `desc_pref_cfg2` | |
| `0x18` | 4 | RW | `desc_rd_aruser` | |
| `0x1c` | 4 | RW | `cmpl_wr_awuser` | |
| `0x20` | 4 | RW | `cfg` | see Table 6.2a below |
| `0x24` | 4 | RO | `status` | `{q_used[24:0], q_isolated[26], prefetch[28], rx[29], q_full[31]}` |
| `0x28` | 4 | RW | `RDRBP_low` | RX Desc Ring Base Ptr `[31:6]` |
| `0x2c` | 4 | RW | `RDRBP_high` | RX Desc Ring Base Ptr `[63:32]` |
| `0x30` | 4 | RW | `RDRL` | RX Desc Ring length |
| `0x34` | 4 | RO | `RDRHP` | RX Desc Ring head ptr |
| `0x38` | 4 | RW | `RDRTP_inc` | **the RX doorbell**: `val[23:0]` "posted N empty rx buffers" = write-leg launch |
| `0x3c` | 4 | RO | `RDRTP` | RX Desc tail ptr |
| `0x40` | 4 | RO | `RDCP` | RX Desc current ptr |
| `0x44` | 4 | RW | `RCRBP_low` | RX Completion Ring Base Ptr `[31:6]` |
| `0x48` | 4 | RW | `RCRBP_high` | RX Completion Ring Base Ptr `[63:32]` |
| `0x4c` | 4 | RO | `RCRHP` | RX Completion Ring head ptr |
| `0x50` | 4 | RO | `RCRHP_internal` | |
| `0x54` | 4 | RW | `comp_cfg` | `{en_comp_ring_update[0]` (rst **0**), `dis_comp_coal[1]` (rst 1), `first_pkt_promotion[2]` (rst 1), `buf2_len_location[3]` (rst 1), `desc_size[15:12]` (rst 4)}` |
| `0x58` | 4 | RW | `comp_cfg_2` | |
| `0x5c` | 4 | RW | `pkt_cfg` | `{hdr_split_size[15:0]` (rst 64), `en_hdr_split[17]}` — S2M-only header split |
| `0x60` | 4 | RW | `qos_cfg` | `{q_qos[7:0]}` |
| `0x64` | 4 | RW | `q_sw_ctrl` | DMB soft ctrl |
| `0x68` | 4 | RO | `q_rx_pkt` | received-packet count |
| `0x6c` | 4 | WO | `RDRTP_set` | direct-set the tail pointer |
| `0x70` | 4 | WO | `RDRHP_set` | direct-set the head pointer |
| `0xd4` | 4 | RW | `data_cfg` | `{max_axi_beats[4:0]` (rst 8)}` |
| `0xe0` | 4 | RW | `append_orig_addr_low` | the S2M_lma append session: splice an original |
| `0xe4` | 4 | RW | `append_orig_addr_high` | buffer addr/len into the landed packet |
| `0xe8` | 4 | RW | `append_orig_len` | |
| `0xec` | 4 | RO | `append_status` | |
| `0x100` | 4 | RW | `write_engine` | |
| `0x104` | 4 | RW | `lma_cfg` | |
| `0x108` | 4 | RW | `auto_cmpl` | |
| `0x11c` | 4 | RO | `fill_level_status` | |
| `0x120` | 4 | RW | `gdma_pref` | |
| `0x124` | 4 | RW | `force_target_q_mapping` | |

**Table 6.2a — S2M `cfg` (`+0x20`) bitfields**  *(HIGH/OBSERVED H2)*

| bits | field | rst | bits | field | rst |
|------|-------|-----|------|-------|-----|
| [3:0] | `axi_awcache_hdr` | 3 | [21] | `hint_if_no_desc` | 0 |
| [7:4] | `axi_awcache_data` | 3 | [22] | `drop_if_no_desc` | 0 |
| [16] | `en_pref` | 0 | [23] | `data_force_full_line` | 0 |
| [17] | `en_stream` | 0 | [27:24] | `axi_awcache_comp` | 3 |
| [18] | `cmpl_force_full_line` | **1** | [31:28] | `AXI_qos` | `0xe` |
| [20] | `allow_lt_min_pref` | 0 | | | |

> **CORRECTION (S2M `en_comp_ring_update` reset).** An earlier draft stated S2M
> `comp_cfg.en_comp_ring_update` resets to **1** ("write-back ON by default — OPPOSITE
> M2S"). The RTL JSON shows **rst 0** for *both* engines. What differs on S2M is the
> companion defaults: `dis_comp_coal` rst **1**, `first_pkt_promotion` rst **1**,
> `buf2_len_location` rst **1**, `desc_size` rst **4** — none of which exist on M2S.

> **CORRECTION (S2M append-register names).** The three append registers are
> `append_orig_addr_low@0xe0`, `append_orig_addr_high@0xe4`, `append_orig_len@0xe8`,
> `append_status@0xec` (RTL names), and the 36th register is
> `force_target_q_mapping@0x124` — earlier drafts ended at `gdma_pref@0x120` and
> mis-named the len/status pair `append_orig_addr_len`/`append_orig_addr_status`.

> **NOTE (cross-gen size).** The register JSON is the maverick (NC-v5) `udma_v4` IP:
> M2S `SizeInBytes 0x40000`, S2M `0x38000`, `AddrWidth 18`. Cayman (NC-v3) shipped a
> smaller M2S window (`0x20000`); the per-queue **offsets** above are identical, only the
> unit envelope and the tail-set register family (cayman `TDRDTP_inc@0xe0` vs maverick
> `TDRTP_set@0xd4`) differ.  *(MED, cross-gen.)*

---

## 7. The CCE compute-descriptor extension  *(HIGH/CARRIED — C3)*

A CCE transfer is the **same** 16-B `SDMA_CME_BD_DESC` ring entry with the meta-control
word set (`SDMAOP.op = CCE = 4`). The CDMA channel reads N source streams off M2S, applies
a per-element ALU op **in flight**, and writes the single reduced/scaled result down S2M.
The byte layout of the M2S compute descriptor and the host-side `kbin_dma_desc_cce_info_t`
are **not** in this extraction (no `kbin_dma_desc` symbol, no `sdma_data_type_size` table
here) → this whole section is **CARRIED** from the host/firmware analysis.

**Table 7.0 — CCE 16-B M2S compute descriptor (W0..W3, overlays `SDMA_CME_BD_DESC`)**

| word | off | field | semantics / valid values |
|------|-----|-------|--------------------------|
| W0 | 0 | ctrl / meta-idx | `write_barrier \| 0x800000` (bit23 "has meta-ctrl") \| `(meta_idx << 24)` |
| W1 | 4 | meta-ctrl word | the per-op META-CTRL (Table 7.1); the WORD1 analogue with `optype=CCE` |
| W2 | 8 | sema/length + op-high | `sema/length(&0xFFFFFF)` \| op-high nibble; selectors `a_sel[25:24]`, `b_sel[27:26]`, `c_sel[29:28]` |
| W3 | 12 | op payload | FMA per-source FP32 **scale**; `0` for ADD/MIN/MAX |

**Table 7.1 — the META-CTRL word (`al_sdma_m2s_build_*_meta_ctrl`)**

| bits | field | semantics / valid values |
|------|-------|--------------------------|
| [25] | CCE meta-ctrl VALID | 1 = this is a CCE descriptor |
| [22:20] | op selector | `FMA=0x100000` (bit20), `EXT=0x400000` (bit22), `GCE=0x500000` (22+20); ADD/MIN/MAX leave 0 |
| [11:8] | in/mid dtype nibble | input/intermediate dtype |
| [15:12] | scale/out dtype nibble | |
| [19:16] | accumulation dtype nibble | mixed precision: read in, accumulate wider, write out |
| [4] | `use_const` | use the min/max const operand |
| [6] | `write_barrier` | |
| [7] | `last`/notif | marks the **final** source of a multi-source reduce (flushes the accumulator to S2M) |

**Table 7.2 — `SDMA_CCETYPE` (the device CCE op enum)**  *(HIGH/CARRIED — C3)*

| val | name | meaning | host `kbin_dma_desc_op_t` → `SDMA_CCETYPE` remap |
|-----|------|---------|-------------------------------------------------|
| 0 | `ADD` | pure accumulate (`cce_info` NULL) | kbin `ADD=2` → 0 |
| 1 | `FMA` | `acc' = input*scale + acc` | kbin `FMA=1` → 1 |
| 2 | `MAX` | B=MAX, optional const clamp | kbin `MAX=4` → 2 |
| 3 | `MIN` | B=MIN, optional const clamp | kbin `MIN=3` → 3 |
| 4 | `EXT` | device-only combo multi-desc packet (base `0x2400000`, chained `0x40000000`) | — |
| 5 | `GCE` | device-only gradient compress/decompress (base `0x2500000`) | — |

(kbin `COPY=0`/`TRANSPOSE=5` are **not** CCE ops.)

**Table 7.3 — CCE operand selectors (W2 nibbles)**  *(HIGH/CARRIED — C3)*

| selector | bits | default | meaning |
|----------|------|---------|---------|
| `a_sel` | [25:24] | 1 = input | `al_sdma_m2s_fma_operand`: stored_buffer=0 (accumulator), input_buffer=1 (source), scale=2 (FP32, W3) |
| `b_sel` | [27:26] | 2 = scale | ⇒ `acc' = input*scale + acc` |
| `c_sel` | [29:28] | 0 = stored | |

`scale_dtype` **must** be FP32; one FP32 W3 per per-source descriptor (per-rank, not
per-element). Optional stochastic-rounding sub-descriptor `0x6000000` on down-convert.

**Table 7.4 — host `kbin_dma_desc_cce_info_t` (140 B)**  *(HIGH/CARRIED — C3)*

| off | w | field | semantics / valid values |
|----|---|-------|--------------------------|
| 0 | 4 | `num_sources` | number of source streams reduced into one dest (1..32) |
| 4 | 132 | (union) | per-op source descriptor union (FMA scale array / min-max const / EXT-GCE payload) |
| 136 | 4 | `num_dests` | number of destination streams (typically 1) |

> **GOTCHA (`sdma_data_type_size[fp32r] == 0`).** The device size table reports `0` bytes
> for `FP32R` (`0xB`), **not** 4. A reimplementer computing reduce-packet size as
> `dtype_size * N` must special-case FP32R (it is a partial-fp32 accumulation format that
> the size table deliberately zeroes); otherwise the packet-size and stride math collapses.

**CCE vs plain COPY** — the minimal delta on the same 16-B BD / ring / doorbell:

| field | plain COPY | CCE compute |
|-------|-----------|-------------|
| `SDMAOP.op` | `CME=2` | `CCE=4` |
| W0 bit23 | 0 | `0x800000` (meta-ctrl present) |
| meta-ctrl[25] | — | 1 (valid) |
| W3 | — | FP32 scale (FMA) / 0 (ADD/MIN/MAX) |
| sources | 1 | 1..32; the `last` bit (meta-ctrl[7]) flushes the reduce |

CCE limits: max elements v2→1024, v3/v4→2048; reduce packet size =
`min(dtype_size·N, cc_cce_reduce_prio_pkt_size[priority_class]) & ~0x1F` (32-B-granular
priority cap).

---

## Firmware grounding (H4)

The descriptor builder that emits these words is the SW-DGE in `libnrtucode_internal.so`.
Its recovered emit strings (verbatim) confirm the field model end-to-end:

```
S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i
S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]
S: push REGWRITE to DMA[%d]
S: DGE $S[%i]+=%i@dmacomplete src=[0x%x]@0x%llx[0x%x,0x%x][%u,%u]
       dst=[0x%x]@0x%llx[0x%x,0x%x][%u,%u] cast:0x%x->0x%x
       bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx) compute_op: 0x%x
```

The `$S[]` template maps one-to-one onto the §2.1 field layout: `[%u,%u]` = `num_elem`
X/Y, `[%d,%d]`/`[0x%x,0x%x]` = `step_elem` X/Y, `cast:0x%x->0x%x` = `in_dtype`→`out_dtype`,
`bounds:(...)` = the two `BOUND_CHECK_REG`s, `compute_op: 0x%x` = the `DGE_COMPUTE_OP`.
