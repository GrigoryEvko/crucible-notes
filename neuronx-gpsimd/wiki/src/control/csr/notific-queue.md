# CSR — NOTIFIC Queue (`notific_n_queue`)

The **NOTIFIC** block is the hardware instruction-notification queue: every TPB
engine (PE / ACT / POOL / DVE / SP), the DMA/DGE path, the error logic, the HAM
throttle monitor, and the AXI event/semaphore path emit fixed **16-byte
notification records** through this block, which coalesces them and writes them
over AXI into one of up to ten software-owned **ring queues (SW NQ)** in SoC RAM.
The host runtime and/or the SP core drains those rings. This page documents the
APB control block (`notific_10_queue` / `notific_1_queue`), the 16-byte wire
record (`aws_neuron_isa_notification.h`), the `notific_type` enum, the trigger
bits, the `wr_buffer` mask enum, and the nine interrupt outputs.

**Provenance.** Byte-exact from the shipped Cayman arch-regs CSR schema
`csrs/notific/notific_10_queue.json` (template `notific_n_queue.json.pp`,
`UnitName = notific_${makodata}_queue`), the shipped ISA header
`common/aws_neuron_isa_notification.h`, and the interrupt-trigger manifest
`intc/tpb_triggers.yaml`. These are firmware/RTL-generated artifacts shipped in
the customop library; semantics below come from the per-field `Description`
strings and the header struct definitions — there is no separate HAL doc
(`HalName` / `HalFilenameUnitName` / `RegFile.Description` are empty strings).

**Per-gen applicability.** `OBSERVED` on **Cayman / NC-v3** (arch-regs
`cayman-arch-regs`). The identical block ships under `mariana`, `mariana_plus`,
`maverick`, and `sunda` arch dirs in the customop-lib — the register layout and
the record header are arch-common. v5 (Maverick-next) is `INFERRED CARRIED`: no
v5-specific schema is present; treat any v5 statement here as carried-forward.

---

## 1. Regfile metadata (HIGH · OBSERVED)

| Property | Value | Source |
|---|---|---|
| `UnitName` | `notific_10_queue` (sibling `notific_1_queue`) | `.RegFile.UnitName` |
| `DataWidth` | 32 bits | `.RegFile.DataWidth` |
| `AddrWidth` | 12 bits → 4 KiB window | `.RegFile.AddrWidth` |
| `SizeInBytes` | `0x1000` | `.RegFile.SizeInBytes` |
| `InterfaceType` | **APB** (control plane). The queue WRITES to RAM over **AXI** (`nq_axi_id`). | `.RegFile.InterfaceType` |
| `Type` / `RegfileFlavor` | `REGFILE` / `POSEDGE` | `.RegFile.*` |
| Param `NUM_SW_Q` | **10** (`notific_1_queue`: **1**) — "Number of SW Queues each NOTIFIC can write to" | `.RegFile.Parameters[0]` |
| Param `SW_Q_RESET_TO_ALL_1` | **1023** = `0x3ff` (`notific_1_queue`: **1**) — reset-to-all-1s mask, width tracks `NUM_SW_Q` | `.RegFile.Parameters[1]` |

Two register-bundle arrays:

| Bundle | `AddressOffset` | `BundleSizeInBytes` | `ArraySize` | reg defs |
|---|---|---|---|---|
| `notific` | `0x000` | `0x100` | 1 | **41** (global control/status/buffers) |
| `notific_nq` | `0x100` | `0x28` | `NUM_SW_Q` (10) | **6** (per-queue ring descriptor) |

Register **definitions**: `41 + 6 = 47`. Physical readable registers after array
expansion: `41 + 6·10 = 101`. No `0xb1` placeholder reset-value leak is present
(the CSR-02/03 management-reg artifact) — every `ResetValue` here is one of
`{0, 0x1, 0xf, 0x0400, 0x1000, 0xffffffff}`. (HIGH · OBSERVED)

---

## 2. Bundle 0 `notific` — global control block (base `0x000`)

Offsets are absolute (bundle base `0x0`). `acc` = field access; `rst` = reset
value. Every row is literal from `notific_10_queue.json`. (HIGH · OBSERVED
unless tagged)

### 2.1 Timestamp (64-bit free-running ps counter)

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0x00` | `timestamp_lo` | RO | `timestamp[31:0]` | `0x0` | low 32b; reset only on hard reset / `rst_timestamp`. Tagged `TESTSPEC_DO_NOT_CHECK_READ_VALUE` (live counter). |
| `0x04` | `timestamp_hi` | RO | `timestamp[31:0]` | `0x0` | high 32b of the same counter. |

This 64-bit value is snapshotted into bits `[127:64]` of **every** notification
record. Per `notification.h` the unit is **1 ps** (not clocks; differs from the
older Tonga part). The increment is set by `timestamp_inc` (§2.10).

### 2.2 Backpressure control

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0x08` | `sw_backpressure` | RW | `on[NUM_SW_Q-1:0]` | `0x0` | 1 ⇒ backpressure incoming notifications when that SW NQ (in RAM) is full. One bit per SW NQ (10b used). Lossless mode. |
| `0x0c` | `hw_backpressure_lo` | RW | `on[31:0]` | `0x0` | 1 ⇒ backpressure when the HW input buffer is full. One bit per instr input (inputs 0..31). |
| `0x10` | `hw_backpressure_hi` | RW | `on[31:0]` | `0x0` | upper bits — one bit per instr input (inputs 32..63). |

⇒ up to **64 hardware instruction inputs** feed one NOTIFIC. (MED · INFERRED —
from the two 32-bit `hw_backpressure` halves.)

### 2.3 Queue enable / overflow policy

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0x14` | `nq_enable` | RW | `en[NUM_SW_Q-1:0]` | `0x0` | enable each SW NQ for writes after configuration. A notification to a *disabled* NQ is **dropped** and raises `intr_1` (`NOTIFIC_QUEUE_DISABLED_ERR_BIT`). |
| `0x18` | `nq_sw_overflow` | RW | `ignore_full_en[NUM_SW_Q-1:0]` | `0x0` | 1 ⇒ send the notification **even if the NQ is full**: write lands at the tail-pointer location and tail advances as usual; the queue then appears empty (oldest entry overwritten). Per-SW-NQ bit. Lossy ring-overwrite mode (vs lossless `sw_backpressure`). |

### 2.4 AXI / transport config

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0x28` | `nq_uart_mode` | RW | `en[0]` | `0x0` | UART mode: ALL notifications go to the queue **base address** (no tail advance) — a debug/streaming sink. |
| `0x2c` | `nq_axi_id` | RW | `id[15:0]` | **`0x1`** | AXI ID used for all transactions this NOTIFIC issues. (Only non-zero functional reset besides the timestamp increment.) |
| `0x30` | `wr_buf_enable_lo` | RW | `en[31:0]` | `0x0` | select which input instr buffers are enabled, one bit per input (low 32). |
| `0x34` | `wr_buf_enable_hi` | RW | `en[31:0]` | `0x0` | upper inputs. |

The `wr_buf_enable` bit positions are named by the enum
`NEURON_ISA_TPB_NOTIFIC_WR_BUFFER` (§6): they gate which record-class
write-buffers are live, so firmware masks whole error/evsem classes away per
NOTIFIC by clearing the corresponding bit. Per the header, "currently only
evsem and error wr_buffer controls are exposed."

### 2.5 AXI write-error + pipeline-stall diagnostics

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0x40` | `nq_error_addr_lo` | RO | `addr[31:0]` | `0x0` | low 32b of the address of the **last AXI write error** from the AXI queue. |
| `0x44` | `nq_error_addr_hi` | RO | `addr[31:0]` | `0x0` | upper bits. Captured when the AXI master gets a write-response error ⇒ `intr_4` (`NOTIFIC_AXI_WRITE_ERR_BIT=4`). |
| `0x48` | `nq_pipeline_stall_count` | RO | `cycles[31:0]` | `0x0` | # cycles the AXI queue backpressured the pipeline since last read. |
| `0x4c` | `nq_pipeline_stall` | RW | `clr[0]` | `0x0` | write-1 clears that counter. |

### 2.6 NIQ — Notification INPUT Queue debug readback

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0x50` | `niq_buffer_count` | RO | `count[8:0]` | `0x0` | number of INPUT-queue buffers in this NOTIFIC (≤ 512). |
| `0x54` | `niq_buffer_index` | RW | `idx[8:0]` | `0x0` | select which input buffer is exposed in `niq_buffer_0..4`. |
| `0x60` | `niq_buffer_0` | RO | `data[31:0]` | `0x0` | selected input buf `[31:0]`. |
| `0x64` | `niq_buffer_1` | RO | `data[31:0]` | `0x0` | `[63:32]`. |
| `0x68` | `niq_buffer_2` | RO | `data[31:0]` | `0x0` | `[95:64]`. |
| `0x6c` | `niq_buffer_3` | RO | `data[31:0]` | `0x0` | `[127:96]`. |
| `0x70` | `niq_buffer_4` | RO | `data[15:0]` | `0x0` | `[143:128]`. |

⇒ one NIQ entry = **144 bits (18 B)**: the 128-bit (16-B) record plus 16 b of
internal input metadata (queue-select / input-id). Read protocol: write `idx`,
then read `niq_buffer_0..4`. (MED · INFERRED — the 128 b/16 b split is from the
field widths, not labeled in-schema.)

### 2.7 NAQ — Notification AXI Queue (coalesced egress) debug readback

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0x80` | `naq_buffer_count` | RO | `count[8:0]` | `0x0` | # of AXI-queue buffers in this NOTIFIC. |
| `0x84` | `naq_buffer_index` | RW | `idx[8:0]` | `0x0` | select which AXI buffer is exposed. |
| `0x90`–`0xa8` | `naq_buffer_0..6` | RO | `data[31:0]` (`naq_buffer_6` exposes `[223:192]`) | `0x0` | selected AXI buf, 7×32 b = `[223:0]`. |

⇒ one NAQ entry = **224 bits (28 B)**: the post-coalescer staged AXI write
payload — a 16-B record plus dest-address / burst bookkeeping. The
Write-Order-Buffer (`wob_ctrl`) and coalescer (`coal_*`) drain the NAQ into AXI
bursts toward queue RAM. (MED · INFERRED.)

### 2.8 Statistics

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0xb0` | `nq_stats` | RO | `num_notifications[31:0]` | `0x0` | total notifications since reset **or since software read** (read-clearing). |

### 2.9 Coalescer

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0xb4` | `coal_timer` | RW | `coal_timer[31:0]` | **`0x1000`** | coalescer flush timer (cycles). |
| `0xb8` | `coal_ctrl` | RW | `force_aligned[7:0]` | `0x0` | full-line burst vs "what's in hand". |
| | | | `axi_fm_bypass[8]` | `0x0` | force total bypass. |
| | | | `clr[9]` | `0x0` | force cleanup of staged entries. |
| | | | `stream_en[13:10]` | **`0xf`** | enable coalescing for the **first 4 SW NQs** (1 bit / NQ; reset = all 4 on). |
| | | | `force_use_coal[14]` | `0x0` | coalesce even if SW NQs overlap. |
| | | | `coal_spare[20:15]` | `0x0` | spare, unused. |

The coalescer merges multiple notifications destined for the same SW NQ into one
wider AXI burst; **only the first 4 SW queues** are coalescible. A coalescer hit
across multiple streams raises `intr_8` (non-deterministic AXI).

### 2.10 Write-Order Buffer, threshold/full status, timestamp increment, spares

| Off | Reg | acc | Field `[pos]` | rst | Meaning |
|---|---|---|---|---|---|
| `0xbc` | `wob_ctrl` | RW | `axi_id_rotate[0]` | `0x0` | rotate AXI IDs on the lower 5b. |
| | | | `wr_clear[1]` | `0x0` | clear the WOB (performs AXI-ID rotation). |
| | | | `force_inorder[2]` | `0x0` | force WOB writes in-order. |
| `0xc0` | `nq_threshold_en` | RW | `en[NUM_SW_Q-1:0]` | `0x0` | enable per-NQ usage-threshold interrupt. 1 bit / NQ. |
| `0xc4` | `nq_full` | RO | `status[NUM_SW_Q-1:0]` | `0x0` | per-NQ full status (drives `intr_0`; the `nq_full_status` in the trigger doc). |
| `0xc8` | `nq_threshold_passed` | RO | `status[NUM_SW_Q-1:0]` | `0x0` | 1 ⇒ that NQ exceeded its threshold (drives `intr_6`). Sticky — re-arms only when `head_ptr` for that NQ is written. |
| `0xcc` | `timestamp_inc` | RW | `val[23:0]` | **`0x0400`** | increment added to the timestamp counter per tick. |
| `0xf0` | `spare_0s` | RW | `val[31:0]` | `0x0` | spare, default 0. |
| `0xf4` | `spare_1s` | RW | `val[31:0]` | **`0xffffffff`** | spare, default all-1s. |

> **CORRECTION (vs SX-CSR-06).** SX-CSR-06 lists `spare_0s` reset as
> `0x00000000`; the schema literal is `ResetValue: "0x0000"` (same numeric
> value). No semantic difference; recorded for byte-exactness.

---

## 3. Bundle 1 `notific_nq` — per-queue ring descriptor (base `0x100`, stride `0x28`)

Six registers replicated `NUM_SW_Q` (10) times. Offsets below are **within** the
per-queue descriptor; the descriptor for queue *i* starts at
`0x100 + i·0x28`. (HIGH · OBSERVED)

| +Off | Reg | acc | Field | rst | `SpecialAccess` | Meaning |
|---|---|---|---|---|---|---|
| `+0x00` | `base_addr_lo` | RW | `bits[31:0]` | `0` | `PulseOnW` | low 32b of the SoC/RAM address where this NQ ring lives. |
| `+0x04` | `base_addr_hi` | RW | `bits[31:0]` | `0` | `PulseOnW` | high 32b of NQ ring base address. |
| `+0x08` | `size` | RW | `bits[31:0]` | `0` | None | ring size **in bytes**. |
| `+0x0c` | `head_ptr` | RW | `bits[31:0]` | `0` | `PulseOnW` | **CONSUMER pointer.** Current head of the active NQ; updated **by software** after reading entries. Delta from `base_addr`. Writing it is the drain-ACK and re-arms the threshold interrupt. |
| `+0x10` | `tail_ptr` | RO | `tp[31:0]` | `0x0` | None | **PRODUCER pointer.** Current tail; last notification written. Does NOT guarantee the last few notifications have reached memory. Delta from `base_addr`. HW-advanced, SW-readable. |
| `+0x14` | `threshold` | RW | `val[31:0]` | `0x0` | None | usage threshold (bytes) above which `intr_6` fires for this NQ. |

`SpecialAccess=PulseOnW` on `base_addr_lo/hi` and `head_ptr` means a write also
emits a one-cycle pulse (latch / re-arm) — an RTL-gen attribute; software just
writes the value. (MED · INFERRED — RTL semantics.)

### 3.1 Absolute offset map (HIGH · OBSERVED)

Queue *i* descriptor base = `0x100 + i·0x28`:

| i | `base_lo` | `base_hi` | `size` | `head` | `tail` | `thresh` |
|---|---|---|---|---|---|---|
| 0 | `0x100` | `0x104` | `0x108` | `0x10c` | `0x110` | `0x114` |
| 1 | `0x128` | `0x12c` | `0x130` | `0x134` | `0x138` | `0x13c` |
| 2 | `0x150` | `0x154` | `0x158` | `0x15c` | `0x160` | `0x164` |
| 3 | `0x178` | `0x17c` | `0x180` | `0x184` | `0x188` | `0x18c` |
| 4 | `0x1a0` | `0x1a4` | `0x1a8` | `0x1ac` | `0x1b0` | `0x1b4` |
| 5 | `0x1c8` | `0x1cc` | `0x1d0` | `0x1d4` | `0x1d8` | `0x1dc` |
| 6 | `0x1f0` | `0x1f4` | `0x1f8` | `0x1fc` | `0x200` | `0x204` |
| 7 | `0x218` | `0x21c` | `0x220` | `0x224` | `0x228` | `0x22c` |
| 8 | `0x240` | `0x244` | `0x248` | `0x24c` | `0x250` | `0x254` |
| 9 | `0x268` | `0x26c` | `0x270` | `0x274` | `0x278` | `0x27c` |

The array spans `0x100..0x290` (`10·0x28 = 0x190`), well inside the 4 KiB
window.

---

## 4. The 16-byte notification record (wire format)

Source: `aws_neuron_isa_notification.h`. `NEURON_ISA_NOTIFICATION_NBYTES = 0x10`
⇒ **every queue entry is 16 B = 128 b**, little-endian, four `uint32` words.
(HIGH · OBSERVED)

### 4.1 Common header byte (word0 `[31:24]`)

The header is a single packed byte (`NEURON_ISA_NOTIFICATION_HEADER`); in a
little-endian packed bitfield `notific_type` occupies the byte's LSBs:

```c
typedef struct {
    uint8_t notific_type:5;             // header byte [4:0]  = word0[28:24]
    uint8_t software_queue_overflow:1;  // header byte [5]    = word0[29]
    uint8_t hardware_queue_overflow:1;  // header byte [6]    = word0[30]
    uint8_t phase:1;                    // header byte [7]    = word0[31]
} __attribute__((packed)) NEURON_ISA_NOTIFICATION_HEADER;
```

Bit defines (header lines 38-43):

| word0 bit | `#define` | meaning |
|---|---|---|
| `31` | `NOTIFICATION_PHASE_BIT` | poll bit; flips every ring wrap (epoch). |
| `30` | `NOTIFICATION_HW_QUEUE_OVERFLOW_BIT` | a HW NIQ buffer was full ⇒ one or more *subsequent* notifications dropped. |
| `29` | `NOTIFICATION_SW_QUEUE_OVERFLOW_BIT` | the RAM NQ was full ⇒ one or more *previous* notifications dropped. |
| `[28:24]` | `NOTIFICATION_TYPE_{LSB=24,MSB=28}`, width 5 | `notific_type` (§5). |

> The header notes the byte layout is "similar to Tonga except the type field is
> widened to 5 bits and the block_id field is removed."

### 4.2 Generic record (the common 16-B frame)

```c
typedef struct {
    uint16_t                          metadata_0;  // bytes[1:0]  = word0[15:0]
    uint8_t                           metadata_1;  // byte[2]     = word0[23:16]
    NEURON_ISA_NOTIFICATION_HEADER    header;      // byte[3]     = word0[31:24]
    uint32_t                          metadata_2;  // bytes[7:4]  = record bits[63:32]
    struct { uint32_t low; uint32_t high; } timestamp; // bytes[15:8] = bits[127:64], 1 ps
} __attribute__((packed)) NEURON_ISA_GENERIC_NOTIFICATION;
```

Field offset map (header lines 95-109):

| record bits | name | bytes | width |
|---|---|---|---|
| `[15:0]` | `metadata_0` | 0–1 | 16 |
| `[23:16]` | `metadata_1` | 2 | 8 |
| `[31:24]` | `header` | 3 | 8 |
| `[63:32]` | `metadata_2` | 4–7 | 32 |
| `[127:64]` | `timestamp` | 8–15 | 64 |

### 4.3 Phase-bit fast path (HIGH · OBSERVED)

Software polls the phase bit of the next-expected entry at
`base_addr + head_ptr`. When it matches the current epoch the entry is valid —
no `tail_ptr` readback is needed on the fast path (and recall `tail_ptr` does not
guarantee AXI drain completion; the phase bit in RAM is the authoritative arrival
signal). Each ring wrap inverts the expected phase.

### 4.4 Per-type record bodies (word0`[23:0]` + word1)

All bodies share the header byte and the trailing 64-b timestamp; they differ in
how the 24-b `[23:0]` + 32-b `[63:32]` payload is interpreted. (HIGH · OBSERVED)

| Notific kind | Body fields (header-literal) |
|---|---|
| **Generic** | `metadata_0:16`, `metadata_1:8`, `metadata_2:32`. |
| **Interrupt** (`ERROR`, `n_type=0`) | `group:3`, `n_type:1`, `block_id:10`, `cause_bits:32`. |
| **Warning** (`ERROR`, `n_type=1`) | `group:3`, `n_type:1`, `metadata_0:4`, `block_id:10`, `metadata_1:6`, `metadata_2:32`. PMU groups 0–2 carry FIS PMU cause bits; groups 3–4 pack up to 84 b extended metadata; groups 5–7 are SW-defined. |
| **DMA M2S/S2M SOP** | `dma_notific_type:4`, `queue:4`, `metadata:7` (BD Word_0[22:16]). |
| **DMA M2S/S2M EOP** | `nbytes:24`, `dma_notific_type:4`, `queue:4`, `crc:16` (CRC16). |
| **DMA …COMPLETION** | `dma_notific_type:4`, `queue:4` (rest reserved). |
| **DMA AXI_REQ** | `address_lo:24`, `dma_notific_type:4`, `arid:5`, `address_hi:23` (47-b araddr). |
| **DMA AXI_RSP** | `dma_notific_type:4`, `rid:5`. |
| **DMA APB** | `address_lo:24`, `dma_notific_type:4`, `address_hi:8` (32-b paddr). |
| **TPB IMPLICIT INST_START** | `program_counter:20` (64-B IRAM resolution), `debug_hint:4`, `soft_reset_counter:16`, `evt_wait_num_cycles:16`. |
| **TPB IMPLICIT INST_END** | `program_counter:20`, `debug_hint:4`, `input_data_crc:16`, `output_data_crc:16`. |
| **TPB EXPLICIT** | `program_counter:20`, `debug_hint:4`, `soft_reset_counter:16`, `debug_hint_extended:16`. |
| **TPB EVT_SEM** | `event_semaphore_id:8`, `is_semaphore:1`, `double_set_or_clear:1`, `event_semaphore_update_mode:8`, `event_semaphore_value:32`. |
| **TPB HAM** | `metadata_0:16`, `metadata_1:8`, `metadata_2:32` (periodic/activity/gpio/sw monitors). |
| **TPB ERROR** | `error_id:8` (`NEURON_ISA_TPB_ERROR_TYPE`), `metadata_lo:16` union, `metadata_hi:32` union (FP / MEM / FAKE / SEM / EVT / PSUM / SEQUENCER). |

The DMA sub-type lives in `dma_notific_type:4`
(`NEURON_ISA_DMA_NOTIFICATION_TYPE`): `M2S_SOP=0x1 M2S_EOP=0x2 M2S_COMPLETION=0x3
S2M_SOP=0x4 S2M_EOP=0x5 S2M_COMPLETION=0x6 AXI_REQ_0/1=0x8/0x9 AXI_RSP_0/1=0xa/0xb
APB_REQ_0/1=0xc/0xd`.

#### 4.5 DGE completion record (HIGH · OBSERVED)

A DGE op's completion is reported as a TPB **EXPLICIT** notification whose
`debug_hint == NEURON_ISA_TPB_INSTRUCTION_METADATA_TYPE_DGE (0x1)`
(`NEURON_ISA_TPB_DGE_METADATA_NOTIFICATION`):

```c
typedef struct {
    uint32_t program_counter:20;        // NX IRAM offset, 64-B resolution
    uint32_t debug_hint:4;              // == DGE (0x1)
    NEURON_ISA_NOTIFICATION_HEADER header;
    uint16_t dma_map;                   // bitmap of 16 DMA engine IDs used
    uint16_t num_packets:12;            // DMA packets per engine; 0 == 4096 (0x1000)
    uint16_t reserved:2;
    uint16_t infer_program_counter:1;   // 1 => take PC from most recent start_notification
    uint16_t dma_group_id:1;            // 0 => engine IDs 0-15, 1 => 16-31
    struct { uint32_t low, high; } timestamp;
} __attribute__((packed)) NEURON_ISA_TPB_DGE_METADATA_NOTIFICATION;
```

This is the firmware-visible payload the host reads from the SW NQ to learn which
DMA engines/packets a *dynamic* DGE instruction used. See
[DGE error notifications](../../firmware/dge/dge-errors.md).

---

## 5. `notific_type` enum (the notification kinds)

`NEURON_ISA_NOTIFICATION_TYPE`, header lines 66-92 — 24 values in the 5-bit
field. (HIGH · OBSERVED)

| Code | Name | Code | Name |
|---|---|---|---|
| `0x02` | `DMA` | `0x0e` | `TPB_POOL_EXPLICIT` |
| `0x03` | `ERROR` (interrupt/warning) | `0x0f` | `TPB_POOL_EVT_SEM` |
| `0x04` | `TPB_PE_INST_START` | `0x10` | `TPB_DVE_INST_START` |
| `0x05` | `TPB_PE_INST_END` | `0x11` | `TPB_DVE_INST_END` |
| `0x06` | `TPB_PE_EXPLICIT` | `0x12` | `TPB_DVE_EXPLICIT` |
| `0x07` | `TPB_PE_EVT_SEM` | `0x13` | `TPB_DVE_EVT_SEM` |
| `0x08` | `TPB_ACT_INST_START` | `0x14` | `TPB_SP_INST_START` |
| `0x09` | `TPB_ACT_INST_END` | `0x15` | `TPB_SP_INST_END` |
| `0x0a` | `TPB_ACT_EXPLICIT` | `0x16` | `TPB_SP_EXPLICIT` |
| `0x0b` | `TPB_ACT_EVT_SEM` | `0x17` | `TPB_SP_EVT_SEM` |
| `0x0c` | `TPB_POOL_INST_START` | `0x1b` | `TPB_AXI_EVT_SEM` |
| `0x0d` | `TPB_POOL_INST_END` | `0x1e` | `TPB_HAM` |
| | | `0x1f` | `TPB_ERROR` |

Codes `0x00`, `0x01`, `0x18`–`0x1a`, `0x1c`–`0x1d` are unassigned (reserved). The
engine quad pattern `{INST_START, INST_END, EXPLICIT, EVT_SEM}` repeats with a
4-code stride per engine: PE `0x04`, ACT `0x08`, POOL `0x0c`, DVE `0x10`, SP
`0x14`.

The `TPB_ERROR` (`0x1f`) body carries `error_id` from
`NEURON_ISA_TPB_ERROR_TYPE`: `FP_UNDERFLOW(0x00) FP_NAN(0x01) FP_INF(0x02)
FP_OVERFLOW(0x03) MEMORY_ERROR(0x04) FAKE_ERROR(0x05) SEMAPHORE_ERROR(0x06)
EVENT_ERROR(0x07) PSUM_COLLISION(0x08) SEQUENCER_NONFATAL(0x09)
SEQUENCER_FATAL(0x0a)`.

---

## 6. `wr_buffer` mask enum + trigger-bit map

### 6.1 `NEURON_ISA_TPB_NOTIFIC_WR_BUFFER` (HIGH · OBSERVED)

One-hot 64-bit class mask used by `wr_buf_enable_lo/hi` (§2.4) to mask records by
`header.notific_type`. Header lines 533-578:

| Bit | Class | Bit | Class |
|---|---|---|---|
| `1<<0x00` | `PE_0` (`ENG_MIN`) | `1<<0x0b` | `EVSEM` |
| `1<<0x01` | `PE_1` | `1<<0x0c` | `HAM` |
| `1<<0x02` | `POOL_0` | `1<<0x0d..0x14` | `Q7_0 .. Q7_7` |
| `1<<0x03` | `POOL_1` | `1<<0x15` | `ERROR_FP_UNDERFLOW` (`ERROR_MIN`) |
| `1<<0x04` | `ACT_0` | `1<<0x16` | `ERROR_FP_NAN` |
| `1<<0x05` | `ACT_1` | `1<<0x17` | `ERROR_FP_INF` |
| `1<<0x06` | `ACT_2` | `1<<0x18` | `ERROR_FP_OVERFLOW` |
| `1<<0x07` | `DVE_0` | `1<<0x19` | `ERROR_MEMORY_ERROR` |
| `1<<0x08` | `DVE_1` | `1<<0x1a` | `ERROR_FAKE_ERROR` |
| `1<<0x09` | `DVE_2` | `1<<0x1b` | `ERROR_SEMAPHORE_ERROR` |
| `1<<0x0a` | `SP` (`ENG_MAX`) | `1<<0x1c` | `ERROR_EVENT_ERROR` |
| | | `1<<0x1d` | `ERROR_PSUM_COLLISION` |
| | | `1<<0x1e..0x27` | per-engine `SEQUENCER_{NONFATAL,FATAL}` (PE/POOL/ACT/DVE/SP) up to `ERROR_MAX = 1<<0x27` |

The reduced `NEURON_ISA_TOPSP_NOTIFIC_WR_BUFFER` exposes only `EVSEM (1<<0)` and
`ERROR_SEQUENCER (1<<2, both fatal+nonfatal)` — matching the header note "only
evsem and error wr_buffer controls are exposed."

### 6.2 Trigger-bit map (HIGH · OBSERVED)

`#define`s at the tail of `notification.h` (lines 778-784) — these set the
NOTIFIC interrupt trigger bits driving the INTC:

| Bit | `#define` | Condition | → interrupt |
|---|---|---|---|
| 0 | `NOTIFIC_SW_NQ_FULL_ERR_BIT` | write to a full SW NQ (lossless path) | `tpb_notific_intr_0` |
| 1 | `NOTIFIC_QUEUE_DISABLED_ERR_BIT` | write to a disabled NQ (dropped) | `tpb_notific_intr_1` |
| 2 | `NOTIFIC_WR_BUFFER_FULL_BACKPRESSURE_BIT` | HW input buffer full → stall (`hw_backpressure`) | `tpb_notific_intr_2` |
| 3 | `NOTIFIC_WR_BUFFER_FULL_DROP_BIT` | HW input buffer full → drop | `tpb_notific_intr_3` |
| 4 | `NOTIFIC_AXI_WRITE_ERR_BIT` | AXI write-response error (+ `nq_error_addr`) | `tpb_notific_intr_4` |
| 5 | `NOTIFIC_AXI_OUTSTANDING_STALL_BIT` | AXI master at max outstanding writes | `tpb_notific_intr_5` |
| 6 | `NOTIFIC_SW_NQ_THLD_REACHED_BIT` | NQ usage ≥ `threshold` (re-arm on `head_ptr` write) | `tpb_notific_intr_6` |

Trigger bits 0–6 map 1:1 to the named `#define`s; `intr_7` (SW-NQ overlap) and
`intr_8` (coalescer multi-stream hit) are generated inside the block and have **no
`#define`** trigger bit — they are pure INTC sources (next section).

---

## 7. The nine NOTIFIC interrupt sources

From `intc/tpb_triggers.yaml`: `tpb_notific_intr_0 .. tpb_notific_intr_8` — **9
interrupts** total. All share `needs_cdc: true`, `source_clock:
clk_core_gated`, `source_reset_n: core_reset_extended_n`, `edge_triggered: true`,
`msix_mask: 0`. (HIGH · OBSERVED)

| Intr | Trigger bit (§6.2) | Condition (YAML literal) |
|---|---|---|
| `tpb_notific_intr_0` | bit 0 | instruction notification could not write — SW NQ full. Does NOT fire if `sw_backpressure=0` & `ignore_full=1`. `nq_full` indicates which NQs are full. |
| `tpb_notific_intr_1` | bit 1 | notification sent to a **disabled** NQ (dropped). |
| `tpb_notific_intr_2` | bit 2 | sent to a full **HW buffer** → stall to the originating block (needs `hw_backpressure`); which buffer is sent to INTC. |
| `tpb_notific_intr_3` | bit 3 | sent to a full **HW buffer** → notification dropped (`hw_backpressure` off). |
| `tpb_notific_intr_4` | bit 4 | AXI master received a write-response error. |
| `tpb_notific_intr_5` | bit 5 | AXI master stalled at max outstanding writes. |
| `tpb_notific_intr_6` | bit 6 | threshold reached for a SW NQ — **fires once**, then disabled for that NQ until its `head_ptr` is written. |
| `tpb_notific_intr_7` | — | **overlap** detected among 2+ enabled SW NQs. |
| `tpb_notific_intr_8` | — | **coalescer** hit multiple streams → AXI behavior non-deterministic. |

(The YAML additionally enumerates `wr_buffer0..29` drop/full diagnostic triggers
— one per HW input buffer — which feed `intr_2`/`intr_3`; they are not part of the
nine top-level NOTIFIC interrupts.) For INTC routing/binding of these nine, see
the [device→host interrupt/notification path](../interrupt/device-host-notification.md).

---

## 8. NUM_SW_Q = 10 vs NUM_SW_Q = 1

`notific_1_queue.json` is the **same block** with `NUM_SW_Q=1`: identical 41 + 6
register layout, identical `SizeInBytes=0x1000`, and the bundle-1 `ArraySize`
follows the parameter so the per-queue descriptor array collapses to a single
entry at `0x100..0x128`. The `[NUM_SW_Q-1:0]` fields
(`sw_backpressure`, `nq_enable`, `nq_sw_overflow`, `nq_threshold_en`, `nq_full`,
`nq_threshold_passed`) collapse to **1 bit**, and `SW_Q_RESET_TO_ALL_1` is `1`
(vs `1023`). (HIGH · OBSERVED)

| | `notific_10_queue` | `notific_1_queue` |
|---|---|---|
| `NUM_SW_Q` | 10 | 1 |
| `SW_Q_RESET_TO_ALL_1` | `1023` (`0x3ff`) | `1` |
| SW-NQ mask width | 10 | 1 |
| `notific_nq` array | 10 descriptors (`0x100..0x290`) | 1 descriptor (`0x100..0x128`) |
| coalescer (`stream_en[13:10]`) | first 4 NQs coalescible | only NQ 0 |

**Why two instantiations.** The 10-queue variant is the production
SW-visible queue set — it gives the routing layer (§9) a 4-bit index space so the
cluster can dedicate separate rings to different notification classes (per-engine
instruction completions, the error/`errors_NT_` route, the DGE-completion route,
HAM, Q7). The 1-queue variant is the minimal instantiation for a context that
needs a single notification sink (a single output ring with no per-class
routing) — same MMIO map, no per-queue index logic. (MED · INFERRED — from the
parameter difference and the routing structure.)

---

## 9. Queue protocol + producer/consumer signaling

### 9.1 Configuration (host/SP, via APB) — HIGH · OBSERVED

1. For each SW NQ *i*: write `notific_nq[i].base_addr_lo/hi` (RAM ring address),
   `.size` (bytes), optional `.threshold`; clear/program `head_ptr`.
2. Set bit *i* in `nq_enable` (`0x14`). Choose the overflow policy: either
   `sw_backpressure[i]=1` (lossless — stall producers when full) **or**
   `nq_sw_overflow.ignore_full_en[i]=1` (lossy ring-overwrite). Optionally set
   `nq_threshold_en[i]` and `coal_ctrl.stream_en` (NQ 0–3 only).

### 9.2 Enqueue (producer side) — pseudocode

```c
// Engine/firmware producer path through one NOTIFIC instance.
// Register/field symbols are the literal CSR/header names.
void notific_enqueue(notific_csr_t *nf,
                     const NEURON_ISA_NOTIFICATION *rec_seed,
                     uint8_t sw_nq_idx /* from tpb routing, sec 9.4 */)
{
    // 1. Producer (PE/ACT/POOL/DVE/SP seq, AXI, HAM, error) drops a record
    //    into a HW input buffer (NIQ). hw_backpressure_lo/hi gates full-buffer
    //    behaviour: stall -> tpb_notific_intr_2, or drop -> tpb_notific_intr_3.

    // 2. NOTIFIC builds the 16-B record: copy seed body, snapshot the 64-bit
    //    timestamp (timestamp_lo|hi, unit 1 ps) into rec[127:64], set the
    //    header byte (notific_type, phase = current ring epoch, and the
    //    hardware/software_queue_overflow bits if a prior drop occurred).
    NEURON_ISA_NOTIFICATION rec = *rec_seed;
    rec.generic.timestamp.low  = nf->timestamp_lo;
    rec.generic.timestamp.high = nf->timestamp_hi;
    rec.generic.header.phase   = ring_epoch(nf, sw_nq_idx);

    // 3. Routing/enable checks.
    if (!(nf->nq_enable & (1u << sw_nq_idx))) {        // disabled NQ
        raise(NOTIFIC_QUEUE_DISABLED_ERR_BIT);          // -> intr_1, drop
        return;
    }
    if (nq_is_full(nf, sw_nq_idx)) {                    // nq_full bit set
        if (nf->nq_sw_overflow & (1u << sw_nq_idx)) {
            rec.generic.header.software_queue_overflow = 1; // lossy overwrite
        } else if (nf->sw_backpressure & (1u << sw_nq_idx)) {
            raise(NOTIFIC_SW_NQ_FULL_ERR_BIT);          // -> intr_0, stall
            return;
        }
    }

    // 4. Coalescer (coal_*) + WOB (wob_ctrl) stage the record in the NAQ and
    //    issue an AXI write (AXI ID = nq_axi_id) to base_addr + tail_ptr;
    //    tail_ptr advances by 16 B (mod size). nq_uart_mode pins all writes to
    //    base_addr. On AXI write-response error: nq_error_addr_lo/hi <- addr,
    //    raise NOTIFIC_AXI_WRITE_ERR_BIT (-> intr_4).
    naq_stage_and_write(nf, sw_nq_idx, &rec);           // tail_ptr += 0x10
    nf->nq_stats++;                                     // read-clearing total
}
```

### 9.3 Drain (host or SP consumer) — pseudocode

```c
// Host/SP consumer of one SW NQ ring in RAM.
size_t notific_drain(notific_nq_desc_t *q /* notific_nq[i] */,
                     volatile uint8_t  *ring /* mapped base_addr */,
                     int expected_phase)
{
    size_t  off = q->head_ptr;                 // delta-from-base, consumer ptr
    size_t  consumed = 0;
    for (;;) {
        NEURON_ISA_NOTIFICATION *e = (void *)(ring + off);
        // Fast path: poll the phase bit; valid when it matches the epoch.
        if (e->generic.header.phase != expected_phase)
            break;                              // no new entry
        dispatch_by_type(e);                    // e->generic.header.notific_type
        off = (off + NEURON_ISA_NOTIFICATION_NBYTES) % q->size; // 0x10, ring
        if (off == 0) expected_phase ^= 1;      // wrap inverts phase
        consumed++;
    }
    // Drain ACK: write head_ptr. Frees ring space AND re-arms intr_6 for this NQ.
    q->head_ptr = off;                          // PulseOnW latches + re-arms
    return consumed;
}
```

Notes: `tail_ptr` (RO) reflects the last write but does **not** guarantee AXI
drain completion — the in-RAM phase bit is authoritative. `intr_6`
(threshold) and `intr_0` (full) let SW drain lazily instead of per-notification
polling. Diagnostics: `nq_stats` (read-clearing total), `nq_full` /
`nq_threshold_passed`, `nq_error_addr_lo/hi`, and the NIQ/NAQ buffer readback
(`*_buffer_index` + `*_buffer_N`) for HW debug.

### 9.4 Routing (which SW NQ a notification lands in) — MED · CARRIED

The SW-queue index is supplied by the cluster-level routing block (the `tpb`
top-level `notific` bundle; routing is described in
[tpb.md](tpb.md)). Each engine's three notification classes
(NX-implicit / explicit / engine-implicit) plus the error / event-semaphore /
HAM / Q7 classes are mapped to a 4-bit SW-queue number; a per-engine
`queue_idx_ctrl` bit lets the SW-queue index come from the **instruction** instead
of the static field. The 4-bit index selects one of this block's `NUM_SW_Q`
descriptors. (CARRIED from SX-CSR-06 §5 / SX-CSR-04; `tpb.md` is a stub pending
authoring — the routing CSR offsets are unverified here.)

The committed anchor (#901/#902/#913/#916): `notific_ctrl @ 0x808` lives in
`tpb.json` events_semaphores, inside the EVT_SEM container at `0x2802700000`; the
NOTIFIC queue documented here is the routing target of that cluster
event/notification path.

### 9.5 Producers / consumers

- **Producers:** TPB sequencers PE / POOL / ACT / DVE / SP (`0x04..0x17`); the
  DMA/DGE path (`0x02` + the DGE-metadata explicit record §4.5); error logic
  (`0x03` / `0x1f`); HAM (`0x1e`); AXI evt/sem (`0x1b`); the NX/Q7 cores via their
  per-core `notific` intr_info bundle.
- **Consumers:** host runtime and/or the SP core read the RAM rings and advance
  `head_ptr`. The DGE/error path surfaces to runtime via the `errors_NT_` queue.
  The host→firmware DGE mailbox is the complementary downstream channel: the
  NOTIFIC queue is the firmware→host completion/telemetry direction; together
  they form the DGE control loop. The drained record stream is what the
  [NTFF trace parser](../../neff/ntff-trace-parse-state.md) reconstructs.

---

## 10. Cross-references

- [Device→Host Interrupt / Notification Path](../interrupt/device-host-notification.md)
  — the producer/INTC side of the nine NOTIFIC interrupts.
- [DGE error notifications](../../firmware/dge/dge-errors.md) — the DGE-completion
  metadata record (§4.5).
- [NTFF trace parse state](../../neff/ntff-trace-parse-state.md) — host-side decode
  of the drained record stream.
- [CSR — tpb](tpb.md) — the cluster routing source (`sw_queue_num` /
  `queue_idx_ctrl`) that selects which `notific_nq[idx]` a record targets.
- The ENS notification-queue mirror struct
  ([host execution-state struct census](../../appendix/struct-exec-state-census.md))
  — NOTE: the dedicated `struct-device-firmware-globals.md` appendix page is not
  yet authored; the closest existing appendix target is linked here.

---

### Confidence summary

- §1–3, §4.1–4.5, §5, §6, §7, §8: **HIGH · OBSERVED** — literal from
  `notific_10_queue.json`, `notific_1_queue.json`,
  `aws_neuron_isa_notification.h`, and `tpb_triggers.yaml`.
- NIQ=144 b / NAQ=224 b internal splits (§2.6–2.7), 64 HW inputs (§2.2),
  `PulseOnW` semantics (§3): **MED · INFERRED** from field widths / RTL
  attributes.
- Routing CSR offsets (§9.4) and the 1-queue rationale (§8): **MED · CARRIED**
  from SX-CSR-06 / SX-CSR-04 pending `tpb.md` authoring.
- v5 applicability: **INFERRED CARRIED** (no v5-specific schema present).
