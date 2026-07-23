# [P9.1] The DMA / Descriptor / Memory Subsystem

This page opens Part 9. It is the byte-level overview of how a GPSIMD/Trainium
data movement is *described*, *queued*, *fired*, and *completed* — the two-layer
descriptor model, the descriptor-type catalog, ring/queue management, the udma CSR
programming sequence, and the on-chip memory it moves data through. Every other
Part-9 page drills into one layer of what is laid out here.

> **Generation anchor.** The Vision-Q7 "Cairo" GPSIMD this wiki reconstructs is the
> **Cayman / NC-v3** generation. The shipped ISA headers name the four reachable
> generations explicitly: `sunda = NC-v2`, `cayman = NC-v3`, `mariana = NC-v4`,
> `maverick = NC-v5` (`aws_neuron_isa_tpb_dma_direct2d.h`, line 3: *"ISA header for
> NC-v3"* in the cayman tree). Unless tagged otherwise, every byte offset, opcode,
> and CSR address below is read from the **cayman** artifacts and is HIGH/OBSERVED.
> v5/Maverick interiors are header-OBSERVED only — flagged INFERRED where they bite.

Confidence/evidence tags follow the wiki convention: `HIGH/MED/LOW` × `OBSERVED`
(read directly from a shipped binary/header/CSR-JSON) / `INFERRED` (reasoned over
observed bytes) / `CARRIED` (grounded in a prior survey of a binary not present in
this extraction — re-verify before reuse). Table cell pipes are escaped.

---

## 1. Orientation — four representations, two device granularities

A DMA descends through four representations. The host/compiler layers (A, B) shape
*intent*; the two device layers (C, D) are the deliverable this Part decodes:

```
 LAYER A  COMPILER IR (neuronx-cc, host x86)        bir::InstDMA + DGEType + DMAQoSClass   [CARRIED]
 LAYER B  HOST-RUNTIME IR (libnrt.so kbin)          dma_desc / mem_ref → kbin_dma_ring_type [CARRIED]
 LAYER C  DEVICE HIGH-LEVEL DESCRIPTOR (Q7 ucode)   one 64-B TPB DMA instruction word       [HIGH/OBS]
 LAYER D  HARDWARE BD RING (udma SDMA engine)       N × 16-B SDMA_CME_BD_DESC ring entries   [HIGH/OBS]
```

The two **device** granularities are the heart of the model and coexist physically:

| Granularity | What | Who writes it | Size | Source |
|---|---|---|---|---|
| **Layer C** | 64-B TPB DMA instruction word | the DGE block / Q7 SWDGE ucode | 64 B (`_Static_assert`) | `neuron_cayman_arch_isa/tpb/*.h` |
| **Layer D** | 16-B hardware buffer descriptor (BD) | expanded from Layer C, consumed by the udma SDMA M2S/S2M engine | 16 B | `data_transfer.o` DWARF (CARRIED), udma CSR |

The single direction bit threaded through everything:

> **READ** = HBM→local = **M2S** = doorbell `TDRTP_inc`.
> **WRITE** = local→HBM = **S2M** = doorbell `RDRTP_inc`.

The rest of this page walks Layer C (the catalog, §2), Layer D + ring management
(§3, §4), the firmware fire/complete sequence (§5), and the on-chip memory the
descriptors address (§6). Cross-links to the deep-dive siblings are inline.

---

## 2. The Layer-C catalog — six 64-B TPB DMA instruction words

Every struct in this section is `ISA_STATIC_ASSERT(sizeof == 64)` in its shipped
header (`extracted/.../neuron_cayman_arch_isa/tpb/`), so the byte layout is gcc-pinned
and HIGH/OBSERVED. Two distinct address widths appear: **ADDR8** (8 B, full SoC reach)
for the data buffers a device DMA touches, vs **ADDR4** (4 B, the 29-bit SBUF/PSUM
window) for the index/compute operands. This split is deliberate — a device DMA must
reach the full SoC address space; a compute or index operand only the partition window.

The struct → wire-opcode binding is itself shipped, in
`neuron_cayman_arch_isa/tpb/instruction_mapping.json` (`struct2opcode`):

| Struct | Wire opcode | Byte | DGE-opcode field |
|---|---|---|---|
| `NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT` | `DMA_MEMCPY` | `0xb8` | `DGE_OPCODE_DMA_DIRECT2D = 0x0` |
| `NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT` | `DMA_INDIRECT` | `0xbb` | `DGE_OPCODE_DMA_INDIRECT1D = 0x1` |
| `NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT` | `DMA_TRANSPOSE` | `0xbd` | `DGE_OPCODE_DMA_TRANSPOSE = 0x2` |
| `NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT` | `DMA_GATHER_TRANSPOSE` | `0xf1` | `DGE_OPCODE_DMA_GATHER_TRANSPOSE = 0x3` |
| `NEURON_ISA_TPB_EXTENDED_RDMA_DESC_GEN_STRUCT` | extended op | `8` | (ExtInst) |
| `NEURON_ISA_TPB_EXTENDED_RDMA_DESC_START_STRUCT` | extended op | `9` | (ExtInst) |

(Opcode bytes are HIGH/OBSERVED in `aws_neuron_isa_tpb_common.h` lines 257/258/260/302;
the `struct2opcode` map closes the loop from struct to byte.)

### 2.1 Shared building blocks

These pack into every descriptor; offsets and bitfields are from
`aws_neuron_isa_tpb_common.h` (HIGH/OBSERVED):

```c
// header @+0 (4 B)
struct NEURON_ISA_TPB_HEADER {
    NEURON_ISA_TPB_OPCODE opcode;        // +0  e.g. DMAMemcpy = 0xb8
    uint8_t               inst_word_len; // +1  length in 16-B records (DMA words = 4)
    uint8_t               debug_cmd;     // +2
    uint8_t               debug_hint;    // +3
};
// events @+4 (8 B) — per-instruction semaphore wait/update gate
struct NEURON_ISA_TPB_EVENTS {
    NEURON_ISA_TPB_WAIT_MODE   wait_mode;       // +0
    uint8_t                    wait_idx;        // +1
    NEURON_ISA_TPB_UPDATE_MODE update_mode;     // +2
    uint8_t                    update_idx;      // +3
    uint32_t                   semaphore_value; // +4
};
// dma_configs (1 B) — the QoS hook
struct NEURON_ISA_TPB_DMA_CONFIGS { uint8_t priority_class : 3; uint8_t reserved : 5; };
// per-descriptor validity gate (1 B)
struct NEURON_ISA_TPB_BOUND_CHECK_REG {
    NEURON_ISA_TPB_REG_NUM bc_reg : 6;                     // bound REGISTER index;
    uint8_t                bc_disable_oob_error_notif : 1; // bc_reg+1 holds high 32b in wide-offset mode
    uint8_t                bc_enabled : 1;
};
// reduce-add compute mode (1 B enum)
enum NEURON_ISA_TPB_DGE_COMPUTE_OP {  // B = op(A,B)
    NONE = 0x00, ADD = 0x01, MULTIPLY = 0x02, MAX = 0x03, MIN = 0x04 };
```

> **NOTE (BOUND_CHECK_REG semantics, HIGH/OBSERVED).** The firmware emits this exact
> runtime trace from `dma_memcopy.cpp`:
> `bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx)` — two `{enabled, effective_addr
> <= bound_limit}` pairs, **src then dst** (verbatim in `libnrtucode.a`). `bc_reg`
> names a hardware bound register holding the buffer's upper limit; in wide-offset
> mode the *next* register (`bc_reg+1`) holds the high 32 bits. The header validity
> predicate (in the `has_valid_bound_check_reg` Rust comment) accepts either a fully
> disabled gate (`bc_enabled==0 && bc_reg==0 && disable_oob_notif==0`) or an enabled
> gate whose register read passes the marker check.

`ADDR8` reaches the full SoC space; its *marker* byte selects the addressing mode
(`aws_neuron_isa_tpb_common.h` lines 508–574, OBSERVED):
`0x00/0x40` immediate · `0x80/0xC0` from register · `0x20/0x28/0x60/0x68` from table ·
the `_WIDE_BIT (1<<2)`/`_REG_BIT (1<<3)`/`_ADDR_TBL_BIT (1<<5)`/`_SHAPE_REG_BIT
(1<<6)`/`_ADDR_REG_BIT (1<<7)` flags compose the rest. `ADDR4` is the union
`{addr_immediate (PartitionOffset), addr_reg}` over the 29-bit window (see §6 for the
partition-offset bit layout).

### 2.2 D1 — `DIRECT2D` (2-D strided memcpy, the DGE bread-and-butter)

```c
struct NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT {       // opcode DMAMemcpy = 0xb8
    NEURON_ISA_TPB_HEADER          header;         // +0   (4)
    NEURON_ISA_TPB_EVENTS          events;         // +4   (8)
    NEURON_ISA_TPB_DMA_CONFIGS     dma_configs;    // +12  (1)  priority_class
    uint8_t                        semaphore;      // +13       completion sem index
    uint8_t                        sem_increment;  // +14
    NEURON_ISA_TPB_DGE_COMPUTE_OP  compute_op;     // +15       reduce-add mode (CCE path)
    NEURON_ISA_TPB_ADDR8           src_start_addr; // +16  (8)
    int32_t                        src_step_elem[2];//+24  (8)  SIGNED x/y strides
    uint16_t                       src_num_elem[2];// +32  (4)  x/y counts
    uint16_t                       src_elem_size;  // +36  (2)
    NEURON_ISA_TPB_BOUND_CHECK_REG src_bound_reg;  // +38  (1)
    NEURON_ISA_TPB_BOUND_CHECK_REG dst_bound_reg;  // +39  (1)
    NEURON_ISA_TPB_ADDR8           dst_start_addr; // +40  (8)
    int32_t                        dst_step_elem[2];//+48  (8)
    uint16_t                       dst_num_elem[2];// +56  (4)
    uint16_t                       dst_elem_size;  // +60  (2)
    NEURON_ISA_TPB_DTYPE           in_dtype;       // +62  (1)
    NEURON_ISA_TPB_DTYPE           out_dtype;      // +63  (1)
};  // _Static_assert sizeof == 64
```

Header prose, verbatim: *"This instruction will generate DMA descriptors to initiate
a Memcpy. The descriptors will be supplied by the DGE block and the TPB engine will
update the DMA queue tail pointers when the descriptors are ready."* — i.e. D1 is a
*request*; the actual 16-B BDs (§3) and the tail-pointer doorbell (§4) are the engine's
job. When `compute_op != NONE` the path routes through the CDMA compute engine (CCE,
FMA + reduce); see [CCE in-transfer](cce-in-transfer.md).

### 2.3 D2 — `INDIRECT1D` (gather / scatter)

```c
struct NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT {     // opcode DMA_INDIRECT = 0xbb
    NEURON_ISA_TPB_HEADER             header;             // +0  (4)
    NEURON_ISA_TPB_EVENTS             events;             // +4  (8)
    uint8_t                           semaphore;          // +12
    uint8_t                           sem_increment;      // +13
    uint8_t                           idx_num_active_channels; // +14
    NEURON_ISA_TPB_DMA_INDIRECT_FLAGS flags;              // +15 (mode/dim bitfield)
    NEURON_ISA_TPB_ADDR8              src_start_addr;     // +16 (8)
    int32_t                           src_step_elem;      // +24 (4)
    uint16_t                          src_num_elem;       // +28
    uint16_t                          src_elem_size;      // +30
    NEURON_ISA_TPB_ADDR8              dst_start_addr;     // +32 (8)
    int32_t                           dst_step_elem;      // +40
    uint16_t                          dst_num_elem;       // +44
    uint16_t                          dst_elem_size;      // +46
    NEURON_ISA_TPB_ADDR4              src_idx_start_addr; // +48 (4)  the INDEX arrays
    NEURON_ISA_TPB_ADDR4              dst_idx_start_addr; // +52 (4)
    NEURON_ISA_TPB_DTYPE              in_dtype;           // +56
    NEURON_ISA_TPB_DTYPE              out_dtype;          // +57
    NEURON_ISA_TPB_BOUND_CHECK_REG    src_idx_bound_reg;  // +58
    NEURON_ISA_TPB_BOUND_CHECK_REG    dst_idx_bound_reg;  // +59
    NEURON_ISA_TPB_DGE_COMPUTE_OP     compute_op;         // +60
    NEURON_ISA_TPB_DMA_CONFIGS        dma_configs;        // +61
    uint8_t                           reserved[2];        // +62 (2)
};  // _Static_assert sizeof == 64
```

The index arrays (`src_idx_start_addr`/`dst_idx_start_addr`, ADDR4 — they live in the
SBUF window) drive the addressing; the bound regs gate the *indices*, not the data.
`flags.indirect_mode` selects `SRC_INDIRECTION=0` (gather), `DST_INDIRECTION=1`
(scatter), or `SRC_DST_INDIRECTION=2`; `flags.gather_dim`/`scatter_dim` (2 bits each)
pick the permuted axis from `{X=0,Y=1,Z=2,W=3}`. Full field/predicate detail and the
scatter race semantics are in [gather/scatter descriptors](gather-scatter-descriptors.md).

### 2.4 D3 — `GATHER_XPOSE` (index-driven gather *transpose*)

```c
struct NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT {   // opcode DMA_GATHER_TRANSPOSE = 0xf1
    NEURON_ISA_TPB_HEADER                     header;             // +0  (4)
    NEURON_ISA_TPB_EVENTS                     events;             // +4  (8)
    uint8_t                                   semaphore;          // +12
    NEURON_ISA_TPB_DMA_CONFIGS                dma_configs;        // +13
    uint8_t                                   idx_num_active_channels; // +14
    NEURON_ISA_TPB_BOUND_CHECK_REG            src_idx_bound_reg;  // +15
    NEURON_ISA_TPB_ADDR8                      src_start_addr;     // +16 (8)
    int32_t                                   src_step_elem[2];   // +24 (8) SIGNED
    uint16_t                                  src_num_elem[2];    // +32 (4)
    NEURON_ISA_TPB_ADDR4                      src_idx_start_addr; // +36 (4)
    NEURON_ISA_TPB_ADDR8                      dst_start_addr;     // +40 (8)
    uint8_t                                   reserved[3];        // +48 (3)
    NEURON_ISA_TPB_BOUND_CHECK_REG            dst_bound_reg;      // +51
    int32_t                                   dst_step_elem_1;    // +52 (dst_step_0 == sizeof(dtype_hi))
    uint16_t                                  dst_num_elem[2];    // +56 (4)
    uint16_t                                  elem_size;          // +60
    NEURON_ISA_TPB_DTYPE_PAIR                 dtype;              // +62  dtype_lo:4 (src) / dtype_hi:4 (dst)
    NEURON_ISA_TPB_DMA_GATHER_TRANSPOSE_FLAGS flags;             // +63  gather_dim:2 / reserved:6
};  // _Static_assert sizeof == 64
```

The transpose is realized by SIGNED `src_step_elem` (a negative stride reverses an
axis) together with the SBUF `axi2sram` cross-bar transpose engine (`transpose_en`,
§6) `flags.gather_dim` selects the permuted axis. `dtype` packs the src/dst type pair
into a single byte (`dtype_lo:4`/`dtype_hi:4`), and `dst_step_elem_1` only carries the
*second* dst stride because the first equals `sizeof(dtype_hi)` by construction.

### 2.5 D4 — `DIRECT2D_XPOSE` (tiled cross-bar transpose, no index array)

A distinct sibling of D3: it transposes by **tile geometry**, not an index array.

```c
struct NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT { // opcode DMA_TRANSPOSE = 0xbd
    NEURON_ISA_TPB_HEADER          header;            // +0  (4)
    NEURON_ISA_TPB_EVENTS          events;            // +4  (8)
    NEURON_ISA_TPB_BOUND_CHECK_REG src_bound_reg;     // +12
    NEURON_ISA_TPB_BOUND_CHECK_REG dst_bound_reg;     // +13
    NEURON_ISA_TPB_DTYPE           in_dtype;          // +14
    NEURON_ISA_TPB_DTYPE           out_dtype;         // +15
    NEURON_ISA_TPB_ADDR8           src_start_addr;    // +16 (8)
    int32_t                        src_step_elem[2];  // +24 (8)
    uint16_t                       src_num_elem[2];   // +32 (4)
    int32_t                        tile_src_row_step; // +36 (4)
    NEURON_ISA_TPB_ADDR8           dst_start_addr;    // +40 (8)
    int32_t                        dst_step_elem[2];  // +48 (8)
    uint16_t                       dst_num_elem[2];   // +56 (4)
    uint8_t                        tile_src_rows;     // +60   tile geometry
    uint8_t                        tile_src_cols;     // +61
    uint8_t                        semaphore;         // +62
    NEURON_ISA_TPB_DMA_CONFIGS     dma_configs;       // +63
};  // _Static_assert sizeof == 64
```

> **CORRECTION (vs the Part-9 backing survey).** Earlier drafts cataloged five 64-B
> words and folded transpose into D3. The shipped `struct2opcode` map and the header
> tree show **two** transpose descriptors: `DIRECT2D_XPOSE` (opcode `0xbd`, tile-geometry
> driven, `tile_src_rows`/`tile_src_cols`/`tile_src_row_step`) and `GATHER_XPOSE`
> (opcode `0xf1`, index-array driven). The catalog is **six** 64-B words, not five.

### 2.6 D5/D6 — extended RDMA (`RDMA_DESC_GEN` / `RDMA_DESC_START`, SBUF→SBUF P2P)

These are the cross-core / cross-die path — see [RDMA cross-die](rdma-cross-die.md)
for the full protocol. Header bytes (HIGH/OBSERVED, `aws_neuron_isa_tpb_extended_utils.h`):

```c
struct NEURON_ISA_TPB_EXTENDED_RDMA_DESC_GEN_STRUCT {     // extended_opcode = 8
    NEURON_ISA_TPB_HEADER             header;            // +0  (4)
    NEURON_ISA_TPB_EVENTS             events;            // +4  (8)
    NEURON_ISA_TPB_EXAMPLE_EXTENDED_OPCODES1 extended_opcode; // +12 == 8
    NEURON_ISA_TPB_EXT_COMPLETION_INFO completion_info;  // +13
    uint8_t                            local_sem;         // +14
    uint8_t                            remote_sem;        // +15
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD  remote_core_id;    // +16 (4)  physical id (GpSimd reg)
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD  remote_routing_id; // +20 (4)  routing id → SoC high bits
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD  dma_engine_mask;   // +24 (4)  pow-2 {1,2,4,8,16} engines
    NEURON_ISA_TPB_ADDR4               src_addr;          // +28 (4)  SBUF partition offset
    NEURON_ISA_TPB_ADDR4               dst_addr;          // +32 (4)
    uint32_t                           free_dim_bytes;    // +36      bytes/partition (0 = sem-only)
    uint8_t                            is_bidirectional;  // +40      (not yet supported)
    uint8_t                            remote_sem_prev;   // +41
    uint8_t                            reserved0[2];      // +42 (2)
    uint8_t                            reserved1[20];     // +44 (20)
};  // _Static_assert sizeof == 64

struct NEURON_ISA_TPB_EXTENDED_RDMA_DESC_START_STRUCT {   // extended_opcode = 9
    NEURON_ISA_TPB_HEADER  header; NEURON_ISA_TPB_EVENTS events;
    ... extended_opcode == 9; completion_info; reserved0[2]; reserved1[48];
};  // carries NO operands — it consumes the ring DESC_GEN built and triggers it.
```

Header prose (verbatim): *DESC_GEN* "Uses Q7 ucode with SWDGE to generate descriptors
including data transfers and semaphore updates" across "all 128 SBUF partitions
simultaneously"; `local_sem` is "incremented by all 16 DMA engines when local gpsimd
finishes triggering DMA, releases handle on local buffer"; `remote_sem` "incremented
by # of DMA engines when all bytes arrive at remote core's data buffer". `DESC_START`
sends the tail-pointer increment and "RdmaDescGen can happen earlier without waiting"
— the wait conditions sit on `DESC_START`.

The firmware backs this byte-for-byte: `libnrtucode.a` carries
`rdma_desc_gen [%s] ring_num=%d, tpb_idx=%d, free_dim_bytes=%d, remote_tpb_idx=%d,
routing_id=%d, dma_mask=0x%x` and the `[TX]`/`[RX]` role traces (`left_pop`/`right_push`
handshake, `sdma_bcast_base`, "Writing tail pointer increment").

---

## 3. Layer D — the 16-B hardware BD (`SDMA_CME_BD_DESC`)

The atomic unit every udma SDMA ring entry holds. Its byte layout is DWARF-derived
from `data_transfer.o` (`dma.hpp:79`, `byte_size 0x10`) and is **CARRIED** — that
object is not in this gpsimd extraction; re-verify against the host runtime before
shipping a reimplementation. The udma side that *consumes* it (§4) is HIGH/OBSERVED.

```c
struct SDMA_CME_BD_DESC {        // 16 B, pinned by MEMCOPY_CARVEOUT_CFG ("byte_off/16")
    uint32_t word0;              // +0  SDMA_DESC_WORD0      (length + ring/gen tag)
    uint32_t word1;              // +4  SDMA_CME_DESC_WORD1  (CME command / CRC controls)
    uint64_t buf_ptr;           // +8  SoC address the engine READS (M2S) or WRITES (S2M)
};
```

`WORD0` (bitfield, DWARF MSB-from-top): `length_meta:16` (bytes for this BD),
`netag0_meta:6`, `md:1`, then `ringid:2 first:1 last:1 int_en:1 no_snoop:1 dmb:1
concatenate:1`.

> **QUIRK (the generation-tag repurpose, CARRIED from `dma_data_transfer`).** The
> firmware does **not** write the DWARF `ringid` field. It packs the length into
> `bits[0:15]` and a **2-bit generation/ring tag into bits[24:25]** (byte3 bits 0–1
> — the DWARF `concatenate`+`dmb` positions, *not* the `ringid` field at byte3 bits
> 6–7). Mask is `&0xFCFF0000`. The completion poll (§5) matches against **these same
> two bits** (`byte3 & 0x3`). A clean-room implementation that trusts the DWARF field
> names here will mis-tag every descriptor.

`WORD1` (CME command word, DWARF): `block_attr:3 cached_crc_iv_index:3 endian_type:8
validate_crc:1 use_stored_crc_iv:1 send_crc:1 store_crc_result:1 store_source_crc:1
copy_src_data:1 optype:SDMA_CMETYPE op:SDMAOP write_barrier:1 netag0_rsvd:5`.

The two enums (DWARF `const_value`, CARRIED):

```text
SDMA_CMETYPE: COPY=0 CRC32=1 CRC32C=2 CKSUM16=3 CKSUM32=4 CRC16=5 CKSUM32_ADLER=6 CRC8=7
SDMAOP:       DRE=1  CME=2  CCE=4
```

The plain staging/move path is a **CME COPY** (`optype=COPY=0, op=CME=2`). CRC variants
drive the FCM "Fast CRC+Memcopy" engine; `op=CCE` drives the CDMA compute engine; `DRE`
is the strided/transpose op. The CME register file itself is a separate APB block
(`csrs/sdma/cme.json`, `SizeInBytes 0x1000`, described as the "AL7 crypto engine").

---

## 4. Ring / queue management + the udma CSR map

A full-duplex SDMA channel = one udma **M2S** (outbound, `udma_m2s.h`) + one udma
**S2M** (inbound, `udma_s2m.h`). Each side exposes **16 queues**; each queue owns a
software-produced **descriptor ring** and an engine-written **completion ring**. Ring
pointers are counted **in descriptors**, not bytes; ring base is 64-B aligned. The
full HW-engine behaviour is in [the al_udma HW engine](udma-hw-engine.md); QoS/DWRR/
rate-limiter binding is in [the DGE builder + QoS](dge-builder-qos.md). The exhaustive
field tables live in the [descriptor + ring field-table appendix](descriptor-ring-field-tables.md).

### 4.1 The per-queue ring registers (Cayman, HIGH/OBSERVED)

Per-queue stride `M2_S_Q_SIZE = 0x1000`; queue *i* block base = `0x1000 + i*0x1000`.
Offsets below are the absolute byte offset of queue 0's register (= queue-relative
offset, since `0x1000 = 0x1000 + 0`). All from `arch-headers/cayman/udma_m2s.h` /
`udma_s2m.h`:

| Reg | M2S off | S2M off | Access | Meaning |
|---|---|---|---|---|
| `*DRBP_LOW` | `0x1028` | `0x1028` | RW | descriptor-ring base, `addr[31:6]` |
| `*DRBP_HIGH`| `0x102c` | `0x102c` | RW | descriptor-ring base, `addr[63:32]` |
| `*DRL` | `0x1030` | `0x1030` | RW | ring length (in descriptors) |
| `*DRHP` | `0x1034` | `0x1034` | RO | head = next descriptor the engine prefetches |
| **`*DRTP_INC`** | **`0x1038`** | **`0x1038`** | **WO** | **DOORBELL** — write N to advance tail by N descriptors |
| `*DRTP` | `0x103c` | `0x103c` | RO | tail (next free slot) |
| `*DCP` | `0x1040` | `0x1040` | RO | prefetch-FIFO head |
| `*CRBP_LOW` | `0x1044` | `0x1044` | RW | completion-ring base low |
| `*CRBP_HIGH`| `0x1048` | — | RW | completion-ring base high (M2S) |
| `*CRHP` | `0x104c` | `0x104c` | RO | completion-ring head |
| `Q_CFG` | `0x1020` | `0x1020` | RW | queue enable + AXI attrs (§4.2) |
| `Q_STATUS` | `0x1024` | `0x1024` | RO | `q_full[31] q_dmb[30] scheduler[29] prefetch[28] q_used[24:0]` |
| `COMP_CFG` | `0x10a0` | `0x1054` | RW | completion-ring writeback control |
| `Q_SW_CTRL` | `0x10b0` | `0x1064` | WO | per-pointer resets |
| `TDRDTP_INC`| `0x10e0` | — | WO | M2S-only enhanced *data*-tail pointer |
| `PKT_CFG` | — | `0x105c` | RW | S2M-only header-split (`hdr_split_size=0x40` default) |

`*DRTP_INC` carries a 24-bit increment value (`VAL_MASK = 0xffffff`). `Q_SW_CTRL`
bits: `RST_DMB=0x1 RST_TAIL_PTR=0x2 RST_HEAD_PTR=0x4 RST_CURRENT_PTR=0x8
RST_DATA_TAIL_PTR=0x10 RST_Q=0x100` (HIGH/OBSERVED).

> The `*` is `T` for M2S (TDRBP, TDRTP_inc, TCRBP) and `R` for S2M (RDRBP, RDRTP_inc,
> RCRBP). The **descriptor and completion ring layouts are byte-for-byte identical**
> between the two engines — only the prefix renames. That symmetry is why a single
> `_dma_ctx_t` (§5.2) can drive both legs of one copy with one tail field.

### 4.2 Queue config / completion (the bits that bite)

`M2S Q_CFG` (`@0x1020`, HIGH/OBSERVED): `en_pref[16]=0x10000` + `en_scheduling[17]
=0x20000` are the **enable** bits; `AXI_qos[30:28]=0x70000000`, `force_full_line[31]
=0x80000000`, `pkt_len_offset[15:0]=0xffff`. `S2M Q_CFG` swaps `en_scheduling` for
`en_stream[17]=0x20000` ("accept packets from stream").

The completion-coalescing default **differs** between engines:

* `M2S Q_COMP_CFG` bits `en_comp_ring_update[0]` / `dis_comp_coal[1]` — writeback is
  configured per use (the custom-op staging path polls, §5, and does not rely on it).
* `S2M Q_COMP_CFG` **reset value = `0x0000000f`** — writeback is **ON by default**
  (`en_comp_ring_update=1` plus first-packet-promotion / buf2-len-location). Opposite
  posture to M2S. `S2M Q_PKT_CFG` reset = `0x40` (`hdr_split_size = 0x40`,
  `en_hdr_split[17]=0x20000`, `force_hdr_split[16]=0x10000`) — header split is an
  S2M-only feature: split the first N bytes into a separate landing buffer.

> **NOTE (window-size asymmetry).** On Cayman the M2S and S2M per-queue *ring* blocks
> are identical, but S2M drops the M2S egress-shaping registers (DWRR @`0x1080..0x108c`,
> two rate limiters @`0x1060..0x1074`) — so S2M's per-queue tail beyond `RCRHP` packs
> `COMP_CFG` at `0x1054` instead of M2S's `0x10a0`. Account for the different layout
> past the common ring header when you walk an S2M queue.

---

## 5. The firmware sequence — init → arm → fire → complete

The custom-op inline DMA staging path is decoded from `init_dma_queue` and
`dma_data_transfer` (`data_transfer.o`, CARRIED) cross-checked against the OBSERVED
udma CSR map (§4) and the OBSERVED firmware strings (`dma_memcopy.cpp`,
`dram_addr_to_soc_addr`, `dge_reshape_memcopy_transpose_fast`). This is the *polling*
completion path; the DGE/`nrt`-execute flow uses the async completion-ring +
notification path instead (see [DGE builder + QoS](dge-builder-qos.md)).

### 5.1 Sequence

```c
// INIT — once per process (init_dma_queue)
void init_dma_queue(void) {
    char *base = *data_scratch_map;
    tx_desc   = base + 0x108;   // three BD rings carved in dataram,
    comp_desc = base + 0x140;   //   memset 0xB8
    rx_desc   = base + 0x180;
    // 4 ring-config words: {0x0C000000 @+0x100/110/120/130, 3 @+0x104/.../134}
    ASSERT(MEM_WINDOW0_LO == 0xF0000000);          // SUNDA_APB_BASE
    ctx.m2s_inc_reg = sdk_dma_queue_m2s_offset + TAIL_INC_DISP - 0x1000; // → +0x38 TDRTP_inc
    ctx.s2m_inc_reg = sdk_dma_queue_s2m_offset + TAIL_INC_DISP - 0x1000; // → +0x38 RDRTP_inc
    ctx.dma_ring_tail_idx = 0; ctx.dma_ring_id = 1;
    // HAL/host arming: write *DRBP/*DRL/*CRBP per queue,
    //   M2S Q_CFG.en_pref|en_scheduling, S2M Q_CFG.en_pref|en_stream,
    //   M2S change_state.normal, pref_queue_en.en = 0xFFFF (all 16).
}

// FIRE — per memcpy (dma_data_transfer)
void dma_data_transfer(addr local, addr hbm, size_t n, int is_read) {
    u64 dataram_soc = dram_addr_to_soc_addr(local);  // §6.2 aperture
    void *primary = is_read ? ctx.m2s_inc_reg : ctx.s2m_inc_reg;
    for (size_t off = 0; off < n; off += CHUNK) {     // CHUNK = min(rem, 64 KiB)
        u32 chunk = min(n - off, 65536);
        bd *tx = ring(ctx.tx_desc, ctx.dma_ring_tail_idx);  // tail*16
        bd *rx = ring(ctx.rx_desc, ctx.dma_ring_tail_idx);
        if (is_read) { tx->buf_ptr = hbm + off;        rx->buf_ptr = dataram_soc + off; }
        else         { tx->buf_ptr = dataram_soc + off; rx->buf_ptr = hbm + off; }
        // WORD0: length in [0:15], gen-tag in [24:25] = ctx.dma_ring_id; mask &0xFCFF0000 first
        tx->word0 = (tx->word0 & 0xFCFF0000) | chunk | (ctx.dma_ring_id << 24);
        rx->word0 = (rx->word0 & 0xFCFF0000) | chunk | (ctx.dma_ring_id << 24);
        __asm__("memw"); __asm__("memw");
        *(volatile u32*)primary               = 1;     // advance one tail
        *(volatile u32*)other_inc_reg(is_read) = 1;     // BOTH M2S and S2M = one CME COPY SoC->SoC
        if (++ctx.dma_ring_tail_idx == 4) {             // ring depth = 4 BDs
            ctx.dma_ring_tail_idx = 0; ctx.dma_ring_id ^= 1; }  // flip gen tag on wrap
        // COMPLETION (before reusing a slot):
        while (((comp_bd->word0 >> 24) & 0x3) != expected_gen) /* busy poll */;
    }
}
```

Key facts: chunk = 64 KiB max; ring depth = **4** BDs; the gen tag flips on wrap and
is what the busy-poll matches (`byte3 & 0x3`); a single copy doorbells **both** the
M2S and S2M tail (`+1` each = one SoC→SoC CME COPY). The poll is **synchronous — no
interrupt, no notification queue** in this path.

### 5.2 `_dma_ctx_t` (40-B singleton, CARRIED)

`@ *data_scratch_map + 0x200`:

| off | size | field |
|---|---|---|
| `0x00` | 8 | `m2s_inc_reg` (M2S `TDRTP_inc` MMIO addr) |
| `0x08` | 8 | `s2m_inc_reg` (S2M `RDRTP_inc` MMIO addr) |
| `0x10` | 4 | `tx_desc*` |
| `0x14` | 4 | `rx_desc*` |
| `0x18` | 4 | `comp_desc*` |
| `0x1c` | 4 | `dma_ring_tail_idx` (wraps at 4) |
| `0x20` | 1 | `dma_ring_id` (gen tag) + `rsvd[7]` |

### 5.3 `NeuronMemcpyMethod` dispatch (CARRIED)

`neuron_memcpy` indexes a `.data` function table (no size/alignment fallback):
`[0] c_memcpy_data_transfer` (scalar), `[1] vec_memcpy_data_transfer` (`xb_vec2Nx8`
IVP SIMD, 128 B/iter + tail), `[2] dma_data_transfer` (SDMA, the path above) =
`DEFAULT_METHOD`.

> **GOTCHA (latent OOB, LOW/CARRIED).** `neuron_set_memcpy_method`'s guard is `>= 4`,
> so it accepts `m == 3` (= `MAX_METHODS`), a latent out-of-bounds read of `table[3]`.
> Not reached on the default path.

### 5.4 DGE mailbox / priority gate (host-side, HIGH/OBSERVED)

The host loader (`libnrtucode_internal.so`) exposes `nrtucode_core_get_dge_mailbox_addr`
(`0x9b11e0`) and `nrtucode_core_dge_get_priority_class_map` (`0x9b10f0`). Both gate on
`boot_state == BOOTED_LEGACY` and on the core kind being one of the five `*_NX_POOL`
kinds — the decompiled bit-test mask is `0x102020204` over the kind enum, i.e. exactly
`{SUNDA, CAYMAN, MARIANA, MARIANA_PLUS, MAVERICK}_NX_POOL` (the five generations). The
priority-class index is bounded `<= 4`, matching `DMA_CONFIGS.priority_class:3` valid
range 0..4; the mailbox sits at `core_base + 40`. So `priority_class` is a 5-value
field (P-classes 0..4), not the full 0..14 the compiler-side `DMAQoSClass` enumerates.

---

## 6. On-chip memory the descriptors address (TPB_0 SoC map)

All region bases below are HIGH/OBSERVED from `arch-headers/cayman/address_map.h`
(the `CAYMAN_TPB_0_*` block, lines ≈3468–3842). TPB_0 base = `0x2000000000`,
size `0x804000000` (≈32.0625 GiB decode window). There are 8 TPB tiles
(`CAYMAN_TPB_0..7`); the per-tile structure is identical.

| SoC base | size | region | symbol |
|---|---|---|---|
| `0x2000000000` | 32 MiB | **STATE_BUF** (SBUF) — systolic on-chip state buffer | `CAYMAN_TPB_0_STATE_BUF_*` |
| `0x2004000000` | 32 MiB | STATE_BUF_SCRATCH_RAM | `..._STATE_BUF_SCRATCH_RAM_*` |
| `0x2040000000` | **1 GiB** | **DGE_MEMORY** — descriptor-staging RAM (emitted BDs land here) | `..._DGE_MEMORY_*` |
| `0x2800000000` | 32 MiB | TPB_RESERVED_SBUF — SBUF decode aperture | `..._TPB_RESERVED_SBUF_*` |
| `0x2802000000` | 4 MiB | **PSUM_BUF** — PE-array partial-sum SRAM | `..._PSUM_BUF_*` |
| `0x2802700000` | 1 MiB | EVT_SEM — event + semaphore op windows | `..._EVT_SEM_*` |
| `0x2803100000` | 128 KiB ×8 | POOL_Q7_CORE{n}_IRAM (1 MiB pitch) | `..._POOL_Q7_CORE0_IRAM_*` |
| `0x2803180000` | 256 KiB ×8 | POOL_Q7_CORE{n}_DRAM (1 MiB pitch) | `..._POOL_Q7_CORE0_DRAM_*` |

`EVT_SEM` sub-windows (each `0x400`, OBSERVED): EVENT `@+0`, SEMAPHORE_READ `@+0x1000`,
SET `@+0x1400`, INC `@+0x1800`, DEC `@+0x1c00`. A semaphore op is a write to one of
the SET/INC/DEC apertures; the completion-semaphore increment of a DMA lands here.

The bank/cluster model and the partition geometry are detailed in
[the SBUF/PSUM bank model](sbuf-psum-banks.md) and [on-chip working memory](onchip-working-memory.md);
the SDMA-window/APB topology is in [SDMA windows + APB](sdma-windows-apb.md). The
essentials this overview needs:

### 6.1 ADDR4 partition addressing (the 29-bit window)

The `PartitionOffset` (ADDR4 immediate) bit layout, verbatim from
`aws_neuron_isa_tpb_common.h` (HIGH/OBSERVED):

```text
partOffset[17:0]  byte offset within the selected partition (256 KiB max)
partOffset[22:18] must be zero
partOffset[24:23] SBUF partition quadrant 0/1/2/3 (partitions 0-31, 32-63, 64-95, 96-128)
partOffset[25]    0 = SBUF address, 1 = PSUM address
```

This is why D5/D6 (RDMA) carry `src_addr`/`dst_addr` as ADDR4 — a partition offset is
all an SBUF→SBUF copy needs; the engine fans the same offset across all 128 partitions.

### 6.2 The dataram SoC aperture (`dram_addr_to_soc_addr`)

The SoC-mastering SDMA cannot natively reach the Q7's 32-bit local dataram, so each
core's low 64 KiB is mirrored into a SoC aperture (firmware `dram_addr_to_soc_addr`,
local window `[0x80000, 0x90000)`):

```c
u64 dram_addr_to_soc_addr(u32 dram) {  // dram in [0x80000, 0x90000)
    u32 idx = 2 * cpu_id + 9;          // {9, 11, 13, ... 23}
    return SoC_BASE + ((u64)idx << 16) + (dram - 0x80000);
}
// cpu0 -> 0x90000 ... cpu7 -> 0x170000; 64 KiB each, 0x10000 apart;
// exposes the LOW 64 KiB of each core's 256-KiB POOL_Q7_CORE{n}_DRAM.
```

### 6.3 The DGE staging carveout

The emitted 16-B BD stream lands in **DGE_MEMORY** (`0x2040000000`, 1 GiB). The
per-memcopy window is `MEMCOPY_CARVEOUT_CFG` (local-reg 39): lower-16 = carveout
**offset in #16-B descriptors** (`byte_off/16`, which pins the descriptor unit at
16 B = §3); upper-16 = carveout **size** (max #desc/memcopy). `MEMCOPY_DMA_CFG`
(local-reg 40): lower-16 = which DMAs the engine may use; upper-16 = which queues.

---

## 7. The legacy iDMA path (`DramRingDMA`) — cataloged, distinct

A simpler, separate descriptor path exists for instruction-cache refill, not tensor
data. `iram_dma.hpp`'s `DramRingDMA` pulls one IRAM cache line (`block_size` bytes)
from DRAM/HBM into device **SIRAM** (firmware strings, OBSERVED: `IRAM cache init,
block_size=0x%x`; `start_fill_siram: fetch_addr=0x%llx`; `DramRingDMA::allocate(%zu)
submit=%llu`; `submit_pending: first_slot=%zu count=%zu submit=%llu ring_id=%u`). It
**shares the udma engine** (16 M2S queue rings) but shares no code, CSRs, or 64-B word
format with the DGE tensor path. Listed for completeness; it does **not** use the §2
descriptors.

---

## 8. End-to-end — one trace through all four layers

```
[compile]  bir::InstDMACopy{DGEType, DMAQoSClass, ADDR4 MEM_PATTERN3D}        (A, CARRIED)
[load]     kbin builds dma_desc/mem_ref; routes onto kbin_dma_ring_type        (B, CARRIED)
           (CUSTOM_OP=16 is the GPSIMD custom-op ring)
[emit]     Q7/DGE: analyze_tensor_reshape -> backend -> GENERATE+DIMPUSH[+REGWRITE]
           -> one 64-B DIRECT2D / INDIRECT1D / *_XPOSE (local) or RDMA_DESC_GEN/START   (C, OBS)
           per-descriptor src/dst BOUND_CHECK_REG gate
[expand]   word -> N x 16-B SDMA_CME_BD_DESC (CME COPY) in the DGE_MEMORY carveout
           (64-KiB chunks, ring depth 4, gen-tag in WORD0 bits[24:25])               (D, OBS/CARRIED)
[fire]     write num_descriptors to TDRTP_inc (M2S/read) or RDRTP_inc (S2M/write)     (OBS CSR)
[complete] gen-tag busy-poll (custom-op inline) OR completion ring + notification
           (DGE/execute); P2P = two-semaphore (local_sem release / remote_sem ready)
```

The three DGE emit micro-ops (`GENERATE` / `DIMPUSH` / `REGWRITE`, all present as
verbatim firmware format strings — `push GENERATE to DMA[%d]: %s : addr=0x%llx,
elem_size=%d, sem_num=%i`, `push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]`, `push REGWRITE
to DMA[%d]`) and the reshape→backend pipeline that produces a 64-B word are covered in
[the DGE micro-op encoding](dge-microop-encoding.md) and
[the DGE builder + QoS](dge-builder-qos.md). The complete data-movement opcode
reference is in [the data-movement reference](data-movement-reference.md).

---

## 9. Confidence ledger

* **HIGH/OBSERVED** (verified against shipped cayman artifacts in this extraction):
  every 64-B word byte layout (§2, `_Static_assert sizeof==64`); the wire opcodes
  `0xb8/0xbb/0xbd/0xf1` and extended `8/9` + the `struct2opcode` binding; the full
  udma M2S/S2M per-queue ring CSR map + the `*DRTP_INC @0x1038` doorbell, queue stride
  `0x1000`, `Q_CFG`/`Q_STATUS`/`Q_SW_CTRL`/`COMP_CFG`/`PKT_CFG` bits and reset values
  (§4); the TPB_0 SoC memory map (§6); the ADDR4 partition-offset bit layout; the
  firmware DGE-emit / rdma / bounds / iDMA strings (§2.6, §5, §7, §8); the DGE
  mailbox/priority host gate (§5.4); the NC-v2..v5 generation mapping.
* **CARRIED** (grounded in a prior survey of `data_transfer.o` / libnrt.so, binaries
  not present here — re-verify before reuse): the 16-B `SDMA_CME_BD_DESC` WORD0/WORD1
  bitfields + the `SDMA_CMETYPE`/`SDMAOP` enums (§3); the WORD0 gen-tag repurpose
  (§3 QUIRK); the `_dma_ctx_t` 40-B singleton + the init/fire/poll byte sequence (§5);
  the `NeuronMemcpyMethod` table + its OOB guard (§5.3); the Layer-A/B compiler/host IR
  (§1, §8) and `CUSTOM_OP=16`.
* **INFERRED / walls**: v5/Maverick interiors are header-OBSERVED only (PSUM_BUF is not
  named in the Maverick `al_address_map_db.json` and may be renamed — treat the §6 map
  as Cayman-pinned, Maverick INFERRED). The 128-partition SBUF/PSUM row geometry is
  INFERRED from the PE-array width, not a single map entry.
