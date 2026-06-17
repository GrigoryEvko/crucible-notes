# The FW-IO MiscRAM Mailbox Protocol

> *All citations on this page are `file:line` into `aws-neuronx-dkms 2.27.4.0` (GPL-2.0), `neuron_fw_io.c` (896 lines) and `neuron_fw_io.h` (581 lines), unless noted. Register offsets are relative to `ndhal->ndhal_address_map.bar0_misc_ram_offset` and apply to all v2/v3 silicon this release supports.*
> *Evidence grade: **Confirmed (source-anchored)** — every register offset, opcode, error code, struct field, CRC seed/table entry, timeout, and control-flow branch below is read straight from the unstripped GPL DKMS C source. The two places marked **GOTCHA** / **MEDIUM** are the only inferences. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

FW-IO ("Firmware I/O") is the kernel↔firmware **mailbox** carried in a MiscRAM aperture of the device's BAR0 window. The cleanest familiar frame is a classic **request/response mailbox to an on-device service processor**: the driver builds a small framed message, protects it with a sequence number and a CRC32C, rings a doorbell register, and waits for the firmware to write back a response carrying the same sequence number. The service processor on the far side is the device's **Q7 management CPU** — *not* the NCFW collective sequencer and *not* the GPSIMD/Q7 vector cores (see the QUIRK below). Every "register" the driver appears to read off the device — device ID, serial number, ECC counters, power utilization, API version, reservation/server/rack IDs — is conceptually a question put to that management CPU through this aperture.

Two protocol generations coexist in one translation unit, and the single most important fact about this release is **which one is actually live**. The **legacy** generation (`fw_io_execute_request`, `neuron_fw_io.c:317`) is a queue-in-host-RAM design: the driver `kmalloc`s a 64 KB request buffer and a 64 KB response buffer in *host* memory, hands their physical addresses to firmware through four base-address MiscRAM registers, fills a `struct fw_io_request` in the host buffer, doorbells, and polls the host response buffer for the matching sequence number. The **new** generation (`fw_io_execute_request_new`, `:406`), gated on firmware API version ≥ 7, has no host queue at all: it writes the 8-byte request header *inline* into two MiscRAM registers (repurposing the legacy "request base address" slots), streams the payload through a 128-byte DATA window, sets an ACK register, doorbells, and polls the TRIGGER register back to zero before reading the response header inline. Legacy carries `FW_IO_CMD_READ` and `FW_IO_CMD_POST_TO_CW`; the new path carries `SET_POWER_PROFILE`, `GET_DATA`, and `SET_FEATURE` (`neuron_fw_io.h:118-124`).

A third path muddies the picture and a reimplementer must not miss it: **most register reads in this release never touch the mailbox command engine at all.** The public read helpers (`fw_io_device_id_read`, `fw_io_serial_number_read`, …) build a BAR address and call the DHAL function pointer `ndhal->ndhal_fw_io.fw_io_read_csr_array`, which on v2/v3 resolves to a wrapper that forwards to `fw_io_read_csr_array_direct` (`:561`) — a plain `readl` with a `0xdeadbeef` "not-ready" retry. The mailbox-backed read path (`fw_io_read_csr_array_readless` → `fw_io_read` → `FW_IO_CMD_READ`) is present but **has zero callers in 2.27.4.0** (dead code, see §5). This page documents both command engines in full and is honest about which is wired.

For reimplementation, the contract is:

- **The MiscRAM register map** — every offset relative to `bar0_misc_ram_offset`, what each one means, and the dual meaning of `0x1f0/0x1f4` across the two generations.
- **The 8-byte framing** — `union fw_io_request_hdr` `{seq, cmd, size, crc32}`, the CRC32C (Castagnoli) algorithm and its exact byte span, and the sequence-number wrap rule.
- **Both transaction engines** — the legacy host-queue poll loop and the new inline-register loop, with their per-command timeout/retry tables and their distinct response-polling conditions (response-seq vs trigger-clear).
- **The honesty boundary** — which read path is live (`_direct`), which is dead (`_readless`), and the `0xdeadbeef` not-ready sentinel.

| | |
|---|---|
| **Source** | `neuron_fw_io.c` / `neuron_fw_io.h` (aws-neuronx-dkms 2.27.4.0, GPL-2.0) |
| **Aperture** | `bar0 + ndhal->ndhal_address_map.bar0_misc_ram_offset + <reg>` |
| **Mailbox handle** | `struct fw_io_ctx` (`neuron_fw_io.h:249`), one per device, `nd->fw_io_ctx` |
| **Frame header** | `union fw_io_request_hdr` = `{u8 seq, u8 cmd, u16 size, u32 crc32}` (`:10`) |
| **Integrity** | CRC32C / Castagnoli, init `0xffffffff`, final XOR `0xffffffff` (`:308-315`) |
| **Doorbell** | `FW_IO_REG_TRIGGER_INT_NOSEC_OFFSET = 0x800`, write `1` (`:203`, `:232`) |
| **Legacy engine** | `fw_io_execute_request` @`:317` — host 64 KB queues, poll response-seq |
| **New engine** | `fw_io_execute_request_new` @`:406` — inline regs, gated API ≥ 7 (`:416`), poll trigger→0 |
| **Live read path** | `fw_io_read_csr_array_direct` @`:561` — `readl` + `0xdeadbeef` retry (via DHAL) |
| **Dead read path** | `fw_io_read_csr_array_readless` @`:590` — **zero callers** in 2.27.4.0 |
| **Commands** | `READ=1, POST_TO_CW=2, SET_POWER_PROFILE=3, GET_DATA=4, SET_FEATURE=5` (`:118-124`) |
| **Error codes** | `SUCCESS=0, FAIL=1, UNKNOWN_COMMAND=2` (`:126-130`) |

> **QUIRK — FW-IO targets the Q7 *management* CPU, not a sequencer and not a vector core.** AWS Neuron silicon carries three distinct Tensilica Xtensa-family roles, and meeting them for the first time invites conflation. (1) The **NCFW collective sequencer** is the Xtensa LX inside each NeuronCore's TPB sequencer block; its firmware is the eight blobs in `libncfw.so` ([firmware/overview](../firmware/overview.md)). (2) The **GPSIMD "Q7" vector cores** are Vision-Q7 (Xtensa LX + IVP) pool engines programmed by microcode in `libnrtucode_extisa.so` ([gpsimd/overview](../gpsimd/overview.md)). (3) The **Q7 management CPU** is the housekeeping processor that owns power, telemetry, ECC reporting, reset orchestration, and pod election — and *that* is the endpoint of this mailbox. FW-IO is a wire protocol over a MiscRAM aperture, not an ISA; nothing on this page disassembles or uploads firmware. Same ISA family, three different jobs.

---

## 1. The MiscRAM Register Map

### Purpose

Every FW-IO operation is an MMIO access to a small register file the firmware exposes inside the device's MiscRAM, reached at `bar0 + bar0_misc_ram_offset + <offset>`. The DHAL-supplied `bar0_misc_ram_offset` localizes the window per silicon generation; the offsets *within* the window are arch-invariant in this release and are enumerated verbatim in `neuron_fw_io.h:139-205`. The map has three functional zones: **telemetry/identity registers** (read by the public helpers), the **mailbox plumbing registers** (header/data/ack/trigger), and **out-of-band control registers** (reset, pod-election neighbor slots — the latter owned by [pod-election](pod-election.md), not touched by this TU).

### Register Catalog

The offsets below are the `enum` values at `neuron_fw_io.h:139-205`. The "dual meaning" rows are the single trap: `0x1f0`–`0x1fc` are *base-address pointers* under the legacy protocol but are *repurposed as the inline header/response slots* under the new protocol.

| Offset | Symbol | Role | Reader / Writer |
|---|---|---|---|
| `0x00` | `API_VERSION` | FW API version; gates new protocol (≥7), ECC decode (≥6), power (≥3) | `fw_io_api_version_read` `:123` |
| `0x24` | `DEVICE_ID` | device id; read with `operational=false` (pre-reset, discovery) | `fw_io_device_id_read` `:203` / `_write` `:209` |
| `0x30` | `INSTANCE_PARTITION_SZ` | inst[5:0], part[30:16], part-valid[31] | `fw_io_instance_partition_sz_read` `:181` |
| `0x38`/`0x3c` | `SERIAL_NUMBER_LO`/`HI` | 64-bit serial = `(hi<<32)|lo` | `fw_io_serial_number_read` `:78` |
| `0x40` | `SRAM_ECC` | SRAM ECC counters | `fw_io_ecc_read` `:36` |
| `0x44`–`0x50` | `HBM0..3_ECC` | per-channel HBM ECC counters | `fw_io_ecc_read` / `fw_io_get_total_ecc_err_counts` `:790` |
| `0x54`/`0x58` | `POWER_UTIL_D0`/`D1` | util[15:0], sample-counter[31:16]; `+4*die` | `fw_io_device_power_read` `:103` |
| `0x64` | `HBM_REPAIR_STATE` | 2 bits: 0=none, 1=pending, 2=failure (`&0x3`) | `fw_io_hbm_uecc_repair_state_read` `:57` |
| `0x70` | `SERVER_RACK_ID` | server[14:0]+valid[15], rack[30:16]+valid[31] | `fw_io_server_info_read` `:136` |
| `0x80`/`0x84` | `RESERVATION_ID_HI`/`LO` | 64-bit reservation id (note HI < LO numerically) | `fw_io_reservation_id_read` `:157` |
| `0xc0` | `RUNTIME_RESERVED0` | reserved `0xc0`–`0xf0` (not used here) | — |
| `0xf0` | `ACK` | new path sets `=1` before trigger | `fw_io_execute_request_new` `:451` |
| `0x100` | `DATA` (128 B, `0x100`–`0x17f`) | payload window: new-path req/resp, legacy metric blob | `:438`, `:485`, `:674` |
| `0x180`–`0x198` | neighbor / pod-election slots | LH/RH neighbor sernum, pod election status/sernum | owned by [pod-election](pod-election.md) |
| `0x1a0` | `RUNTIME_RESERVED1` | reserved `0x1a0`–`0x1d0` | — |
| `0x1d4`/`0x1d8` | `RESET_TPB_MAP_HI`/`LO` | TPB-reset block bitmaps | `fw_io_initiate_reset` `:627-628` |
| `0x1ec` | `RESET` | write reset type; reads back `0` once FW accepts | `fw_io_initiate_reset` `:632` / `fw_io_is_reset_initiated` `:643` |
| `0x1f0` | `REQUEST_BASE_ADDR_HIG` | legacy: req buf phys hi · **new: inline req header dw0 / resp read** | `:221` (legacy) · `:443`,`:471` (new) |
| `0x1f4` | `REQUEST_BASE_ADDR_LOW` | legacy: req buf phys lo · **new: inline req header dw1** | `:219` (legacy) · `:444` (new) |
| `0x1f8` | `RESPONSE_BASE_ADDR_HIGH` | legacy: resp buf phys hi · new: zeroed | `:225` (legacy) · `:447` (new) |
| `0x1fc` | `RESPONSE_BASE_ADDR_LOW` | legacy: resp buf phys lo · new: zeroed | `:223` (legacy) · `:448` (new) |
| `0x800` | `TRIGGER_INT_NOSEC` | doorbell: write `1`; new path polls back to `0` | `fw_io_trigger` `:232` · poll `:459` |

> **QUIRK — `0x1f0`/`0x1f4` carry pointers in one generation and a message header in the other.** Under legacy framing these are the high/low dwords of the *host* request-buffer physical address (`fw_io_init`, `:219-222`). Under the new framing the same two registers receive the 8-byte inline request header (`req_header.reg.dw0`→`0x1f0`, `dw1`→`0x1f4`, `:443-444`) and `0x1f0` is read back to recover the response header (`:471`). The `enum` names still say `*_BASE_ADDR_*`; the firmware-side relabeling is not documented in this TU. The *mechanics* are CERTAIN; the naming rationale is **MEDIUM**.

### Considerations

`fw_io_device_id_read` is the one telemetry read issued with `operational=false` (`:206`): it runs during discovery and pod election before the device is brought out of reset, so it must tolerate a `0xdeadbeef` not-ready value and retry (§5). All other telemetry helpers pass `operational=true` and expect an immediately valid `readl`. `RESERVATION_ID_HI=0x80`/`LO=0x84` is the one offset pair where the numeric order is inverted relative to the LO/HI naming; `fw_io_reservation_id_read` reads LO first then HI and composes `(hi<<32)|lo` correctly (`:164-177`).

---

## 2. The Frame and Its Integrity

### Purpose

Both engines send the same 8-byte framed header in front of a variable payload; only *where* the frame lives differs (host buffer vs inline registers). The frame is the reliability layer: a monotonic sequence number lets the driver match a response to its request, and a CRC32C lets the firmware reject a corrupted request.

### The Header Layout

`union fw_io_request_hdr` (`neuron_fw_io.h:10-21`) overlays a byte-field view (`hdr`) with a two-dword register view (`reg`). The dword view exists precisely so the new engine can shovel the header into MiscRAM registers with two `reg_write32`s.

| Field | Bytes | Role | Source |
|---|---|---|---|
| `hdr.sequence_number` | `[0]` | copied into the response; matched on poll | `:12` |
| `hdr.command_id` | `[1]` | one of `READ`/`POST_TO_CW`/`SET_POWER_PROFILE`/`GET_DATA`/`SET_FEATURE` | `:13` |
| `hdr.size` | `[2:3]` (u16) | total request size **including** the 8-byte header | `:14` |
| `hdr.crc32` | `[4:7]` | CRC32C of the whole request, header `crc32` zeroed first | `:15` |
| `reg.dw0` | `[0:3]` | header bytes 0–3 (seq, cmd, size) as one dword | `:18` |
| `reg.dw1` | `[4:7]` | header bytes 4–7 (crc32) as one dword | `:19` |

The legacy response header (`union fw_io_response_hdr`, `:29-36`) is only 4 bytes — `{seq, error_code, size}` and no CRC. The new response header (`union fw_io_response_hdr_new`, `:40-51`) is 8 bytes and *adds* a `crc32` field — see the GOTCHA in §4.

### Algorithm — CRC32C

The CRC is table-driven Castagnoli (reflected), seeded `0xffffffff` and finalized with a `^ 0xffffffff` (`:308-315`). The table's entry `[1]` is `0xf26b8303` (`:238`), the standard reflected-Castagnoli signature. The header and data are CRC'd as two separate spans with the header's `crc32` field pre-zeroed, so the firmware can recompute over the identical bytes.

```c
// dx_crc32c_add — neuron_fw_io.c:277 (per-byte table step, reflected)
function crc32c_add(data, len, csum):
    for i in 0 .. len-1:
        csum = (csum >> 8) ^ crc32c_table[(csum ^ data[i]) & 0xff]   // :282

// crc32c — neuron_fw_io.c:308
function crc32c(hdr, data, len) -> u32:
    csum = 0xffffffff                       // :310
    if hdr != NULL:
        crc32c_add(hdr, 8, &csum)           // :312 — the full 8-byte header (crc32 field = 0)
    crc32c_add(data, len, &csum)            // :313 — the payload
    return csum ^ 0xffffffff                // :314
```

> **NOTE —** the legacy caller passes `len = hdr.size - sizeof(hdr.hdr)` (`:351`), i.e. payload bytes only, with the 8-byte header supplied separately as `hdr`. The new caller passes `len = req_size` (`:432`). Both cover header(8) + payload, identically, with the header's `crc32` word zeroed before the computation (`:350`, `:431`).

---

## 3. Legacy Engine — `fw_io_execute_request`

### Purpose

The legacy transaction is a host-queue mailbox: the request lives in a driver-allocated 64 KB host buffer whose physical address firmware was told via `fw_io_init`, and the driver polls a *host-memory* response buffer for the matching sequence number. This engine carries the two original commands, `FW_IO_CMD_READ` (register reads) and `FW_IO_CMD_POST_TO_CW` (CloudWatch metrics).

### Entry Point

```text
fw_io_read (:519)  /  fw_io_post_metric (:650)
  └─ ndhal->ndhal_fw_io.fw_io_execute_request           ── DHAL pass-through (v2/v3)
       └─ fw_io_execute_request (:317)                  ── the host-queue transaction
            ├─ fw_io_init (:217)                         ── write 4 base-addr regs each retry
            ├─ crc32c (:308)                             ── frame integrity
            └─ fw_io_trigger (:232)                      ── doorbell 0x800 = 1
```

### Algorithm

```c
// fw_io_execute_request — neuron_fw_io.c:317
function fw_io_execute_request(ctx, command_id, req, req_size, resp, resp_size) -> int:
    max_req  = ctx.request_response_size - sizeof(fw_io_request)    // :321 (0xffff - 8)
    max_resp = ctx.request_response_size - sizeof(fw_io_response)   // :322
    if req_size  > max_req:  return -EINVAL                         // :324
    if resp_size > max_resp: return -EINVAL                         // :328

    // HACK: POST_TO_CW caller already holds ctx->lock; skip re-lock (:333-337)
    if command_id != FW_IO_CMD_POST_TO_CW: mutex_lock(ctx.lock)

    for i in 0 .. FW_IO_RD_RETRY-1:                                 // :340  (30 retries)
        fw_io_init(ctx.bar0, ctx.request_addr, ctx.response_addr)   // :342  re-arm base addrs
        if ++ctx.next_seq_num == 0: ctx.next_seq_num = 1            // :343  never 0

        memcpy(ctx.request.data, req, req_size)                     // :346
        ctx.request.hdr = { seq = ctx.next_seq_num,                 // :347
                            cmd = command_id,                       // :348
                            size = req_size + sizeof(fw_io_request),// :349  (payload + 8)
                            crc32 = 0 }                             // :350
        ctx.request.hdr.crc32 =                                     // :351
            crc32c(&ctx.request.hdr, ctx.request.data, size - 8)

        ctx.response.hdr.sequence_number = 0                        // :353  clear stale seq
        dma_rmb()                                                   // :354
        fw_io_trigger(ctx.bar0)                                     // :355  doorbell

        // poll the HOST response buffer for our seq, msleep(1)/iter, up to 1 s
        start = ktime_get()
        do:                                                        // :362
            resp_seq = READ_ONCE(response.hdr.sequence_number)      // :363  volatile u8
            if resp_seq == ctx.next_seq_num: break                 // :364
            msleep(1)
        while elapsed_us < FW_IO_RD_TIMEOUT                         // :367  (1,000,000 us)

        ret = -1
        if resp_seq != ctx.next_seq_num:                           // :370  timed out
            continue                                                //       retry (next seq)

        if response.hdr.error_code == FW_IO_SUCCESS:               // :375
            if (response.hdr.size - sizeof(fw_io_response)) > resp_size:  // :376 over-large
                goto done                                          //       (ret stays -1)
            memcpy(resp, response.data, response.hdr.size - 4)     // :382  copy payload out
            ret = 0; goto done                                     // :384

        ctx.fw_io_err_count++                                      // :387  FW reported error
        if response.hdr.error_code == FW_IO_UNKNOWN_COMMAND:      // :391  don't retry
            ret = -1; goto done
done:
    if command_id != FW_IO_CMD_POST_TO_CW: mutex_unlock(ctx.lock) // :400
    return ret
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `fw_io_execute_request` | `:317-404` | host-queue transaction, 30-retry poll loop | CERTAIN |
| `fw_io_init` | `:217-228` | write `request_addr`/`response_addr` into the 4 base-addr regs | CERTAIN |
| `fw_io_trigger` | `:232-235` | write `1` to `TRIGGER 0x800` | CERTAIN |
| `fw_io_read` | `:519-527` | wrap `execute_request(READ, u64 addrs, u32 vals)`; fault hook | CERTAIN |
| `fw_io_post_metric` | `:650-689` | stage ≤128 B blob in DATA window, then `execute_request(POST_TO_CW)` | CERTAIN |

### Considerations

The doorbell is re-armed *every retry*: `fw_io_init` rewrites all four base-address registers at the top of each loop iteration (`:342`), defending against a firmware-side or hardware-side clear of those pointers between attempts. The `FW_IO_RD_TIMEOUT` of 1 s (`neuron_fw_io.h:270`) is per-attempt; the header comment frames the 30-retry product as "up to 30 seconds in worst case" (`:266`). The `dma_rmb()` at `:354` orders the response-seq clear before the doorbell so a stale match cannot be observed. The `POST_TO_CW` lock-skip is a deliberate hack (commented `:333`, `:398`) because `fw_io_post_metric` already holds `ctx->lock` while staging the blob (`:671`).

---

## 4. New Engine — `fw_io_execute_request_new`

### Purpose

The new transaction removes the host queue entirely: the header goes inline into MiscRAM registers, the payload streams through the 128-byte DATA window, and completion is signaled by the firmware clearing the TRIGGER register rather than by writing a host-memory sequence number. It is gated on firmware API version ≥ 7 and carries the three later commands (`SET_POWER_PROFILE`, `GET_DATA`, `SET_FEATURE`).

### Entry Point

```text
fw_io_set_power_profile (:815) / fw_io_get_performance_profile (:830)
fw_io_enable_throttling_notifications (:851) / fw_io_get_available_profiles (:875)
  └─ fw_io_execute_request_new (:406)                  ── inline-register transaction
       ├─ fw_io_api_version_read (:123)                ── gate: API >= 7 else -ENOTSUPP
       └─ reg_write32 / reg_read32                     ── DATA window, header regs, ACK, TRIGGER
```

### Algorithm

```c
// fw_io_execute_request_new — neuron_fw_io.c:406
function fw_io_execute_request_new(ctx, command_id, req, req_size, resp, resp_size) -> int:
    fw_io_api_version_read(ctx.bar0, &api)                          // :414
    if api_read_failed or api < FW_IO_NEW_READLESS_READ_MIN_API_VERSION:  // :416 (< 7)
        return -ENOTSUPP                                            // :418

    mutex_lock(ctx.lock)                                            // :421
    retry_count = (command_id < FW_IO_CMD_MAX)                      // :423
                  ? fw_io_cmd_retry_tbl[command_id] : FW_IO_RD_RETRY
    for i in 0 .. retry_count-1:                                    // :424
        if ++ctx.next_seq_num == 0: ctx.next_seq_num = 1           // :425
        req_header = { seq = ctx.next_seq_num, cmd = command_id,   // :428-430
                       size = req_size + 8, crc32 = 0 }
        req_header.hdr.crc32 = crc32c(&req_header, req, req_size)   // :432

        // payload → DATA window (0x100), one dword at a time
        if req_size > 0:                                            // :435
            for j in 0 .. ceil(req_size/4)-1:
                reg_write32(DATA_OFFSET + j*4, ((u32*)req)[j])      // :438

        // header → repurposed base-addr regs
        reg_write32(REQUEST_BASE_ADDR_HIG 0x1f0, req_header.reg.dw0)// :443
        reg_write32(REQUEST_BASE_ADDR_LOW 0x1f4, req_header.reg.dw1)// :444
        reg_write32(RESPONSE_BASE_ADDR_HIGH 0x1f8, 0)              // :447
        reg_write32(RESPONSE_BASE_ADDR_LOW  0x1fc, 0)              // :448
        reg_write32(ACK 0xf0, 1)                                   // :451
        reg_write32(TRIGGER 0x800, 1)                              // :452  doorbell

        // poll TRIGGER back to 0 (FW-cleared completion), per-command timeout
        timeout = (command_id < FW_IO_CMD_MAX)                      // :457
                  ? fw_io_cmd_timeout_tbl[command_id] : FW_IO_RD_TIMEOUT
        start = ktime_get()
        do:                                                        // :458
            reg_read32(TRIGGER 0x800, &trigger)                    // :459
            if !trigger: break                                     // :460  FW done
            msleep(1)
        while elapsed_us < timeout                                 // :462
        if trigger: continue                                       // :463  timed out, retry

        // response header read back from 0x1f0 (dw0 only)
        reg_read32(REQUEST_BASE_ADDR_HIG 0x1f0, &resp_header.reg.dw0) // :471
        if resp_header.hdr.sequence_number != ctx.next_seq_num:    // :473  seq mismatch
            continue
        if resp_header.hdr.error_code == FW_IO_SUCCESS:            // :479
            data_size = resp_header.hdr.size - 8                    // :480
            copy_size = min(resp_size, data_size)                  // :482
            for j in 0 .. copy_size/4 - 1:                         // :484
                reg_read32(DATA_OFFSET + j*4, &((u32*)resp)[j])    // :485
            if copy_size % 4:                                      // :487  tail bytes
                reg_read32(DATA_OFFSET + (copy_size/4)*4, &tmp)
                memcpy(&resp[copy_size & ~3], &tmp, copy_size % 4) // :491
            ret = 0; break

        ctx.fw_io_err_count++                                      // :498
        ret = -1
        if resp_header.hdr.error_code == FW_IO_UNKNOWN_COMMAND:   // :501
            break
    mutex_unlock(ctx.lock)                                         // :506
    return ret
```

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `fw_io_execute_request_new` | `:406-508` | inline-register transaction, poll trigger→0 | CERTAIN |
| `fw_io_set_power_profile` | `:815-828` | `SET_POWER_PROFILE` w/ `fw_io_req_perfprofile_data` | CERTAIN |
| `fw_io_get_performance_profile` | `:830-849` | `GET_DATA` type=1 → `profile` | CERTAIN |
| `fw_io_enable_throttling_notifications` | `:851-873` | `SET_FEATURE` features=`0x01`/`0x00` | CERTAIN |
| `fw_io_get_available_profiles` | `:875-895` | `GET_DATA` type=2, op=feature → bitmap[32] | CERTAIN |
| `fw_io_post_metric_new` | `:691-697` | `POST_TO_CW` via new engine, no response | CERTAIN |

### Per-Command Timeout and Retry

The new engine consults two parallel `FW_IO_CMD_MAX`-entry tables keyed by command id (`:290-306`). These exist because the firmware-side completion latency varies by two orders of magnitude across commands — a power-profile change can take 90 s while a data read returns in ≤ 10 s (comment `:286-289`).

| Command | id | Timeout | Retries |
|---|---|---|---|
| `FW_IO_CMD_READ` | 1 | 1 s | 15 |
| `FW_IO_CMD_POST_TO_CW` | 2 | 1 s | 15 |
| `FW_IO_CMD_SET_POWER_PROFILE` | 3 | 90 s | 3 |
| `FW_IO_CMD_GET_DATA` | 4 | 10 s | 3 |
| `FW_IO_CMD_SET_FEATURE` | 5 | 60 s | 3 |

> **GOTCHA — the response CRC32 is computed by firmware but never read by the driver.** The new response header `union fw_io_response_hdr_new` carries a `crc32` field (`neuron_fw_io.h:45`; the comment at `:23-25` states firmware was *"updated to include crc32 field in response header"*). But the read-back at `:471` only fetches `resp_header.reg.dw0` — the seq/error/size dword — and never reads `dw1` (the response `crc32` at `0x1f4`). The response is accepted on a sequence-number match alone (`:473`); its CRC is structurally present and structurally ignored. A reimplementer who assumes the response is integrity-checked is wrong: in 2.27.4.0 the *request* is CRC-protected on the wire, the *response* is not validated. Surface this in any reliability or trust analysis — see [security/fw-io-trust](../security/fw-io-trust.md). CONFIDENCE: **CERTAIN** (the read-back span is a single `reg_read32` of `dw0`, `:471`; `dw1` is never read on the response).

### Considerations

The two engines diverge in their completion contract, and this is the easiest reimplementation error. Legacy completion = *firmware writes our seq into a host buffer* (`:363-364`). New completion = *firmware clears the TRIGGER register* (`:459-460`), after which the response header is read inline. A reimplementer must not poll TRIGGER on the legacy path nor poll a host seq on the new path. Note also that `execute_request_new` does **not** call `fw_io_init`: it overwrites `0x1f0`–`0x1fc` directly each iteration, so a device that ran the new path and later falls back to legacy relies on legacy's own `fw_io_init` (`:342`) re-establishing the host base addresses — there is no stale-pointer hazard because legacy re-arms every retry (MEDIUM on the cross-protocol switch sequencing, CERTAIN on the per-engine behavior).

---

## 5. The Register-Read Paths and the `0xdeadbeef` Sentinel

### Purpose

Despite the names, most "register reads" in this release do not run a mailbox command. The public helpers (`fw_io_device_id_read`, `fw_io_serial_number_read`, `fw_io_ecc_read`, …) form a BAR address and dispatch through the DHAL pointer `ndhal->ndhal_fw_io.fw_io_read_csr_array`, which on v2/v3 resolves to a thin wrapper that forwards to `fw_io_read_csr_array_direct`. That function is a plain `readl` — with one twist for reads issued before the device is operational.

### Algorithm — Direct Read

```c
// fw_io_read_csr_array_direct — neuron_fw_io.c:561
function fw_io_read_csr_array_direct(addrs, values, num_csrs, operational) -> int:
    if operational:                                    // :565  device is up — trust the read
        for i in 0 .. num_csrs-1:
            values[i] = readl(addrs[i])                // :567
        return 0

    if num_csrs != 1:                                  // :573  pre-operational: single read only
        return -1                                      //       (no multi-CSR atomicity in reset)

    for i in 0 .. FW_IO_RD_RETRY-1:                    // :578  retry while FW "not ready"
        start = ktime_get()
        do:
            values[0] = readl(addrs[0])                // :581
            if values[0] != 0xdeadbeef: return 0       // :583  real value arrived
            msleep(1)
        while elapsed_us < FW_IO_RD_TIMEOUT
    return -1                                          // :587  still not ready
```

The `0xdeadbeef` value is the firmware's "this register is not yet populated" sentinel (`:583`); the same constant is filtered when aggregating ECC counters across channels (`:806`). Only `fw_io_device_id_read` (`:206`) and `fw_io_is_reset_initiated` (`:644`) pass `operational=false` — both run around discovery/reset when the management CPU may not have written its registers yet.

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `fw_io_read_csr_array_direct` | `:561-588` | live read path: `readl` + `0xdeadbeef` retry | CERTAIN |
| `fw_io_register_read_region` | `:545-559` | record a BAR→device-phys mapping (readless region table) | CERTAIN |
| `fw_io_read_csr_array_readless` | `:590-616` | translate BAR ptrs → device-phys, mailbox `FW_IO_CMD_READ` | CERTAIN (dead) |
| `fw_io_is_reset_initiated` | `:637-648` | read `RESET 0x1ec`; true iff read ok **and** value == 0 | CERTAIN |
| `fw_io_initiate_reset` | `:618-635` | write TPB maps + reset type + doorbell | CERTAIN |

> **GOTCHA — `fw_io_read_csr_array_readless` is dead code in 2.27.4.0.** The mailbox-backed read path exists (`:590` → `fw_io_read` → `FW_IO_CMD_READ`) but has **zero callers** in this release: both v2 and v3 DHAL wrappers route `fw_io_read_csr_array` to `_direct`, never `_readless`. The header even declares a `fw_io_read_csr_array(void**, u32*, u32, bool)` (`neuron_fw_io.h:318-327`) with **no matching definition anywhere** in the tree — a vestigial prototype; the live entry is the DHAL function pointer of the same conceptual name. "Readless read over the mailbox" is therefore a present-but-unwired concept on v2/v3 silicon: a reimplementer targeting these generations needs only the `_direct` path. CONFIDENCE: **CERTAIN** (no callers; both wrappers verified to call `_direct`).

### Considerations

The reset handshake (consumed by [reset](reset.md)) is a write-then-poll over the same `RESET 0x1ec` register: `fw_io_initiate_reset` writes the reset type and rings the doorbell (`:632-634`), then the caller polls `fw_io_is_reset_initiated`, which returns true only when the register reads back `0` — i.e. the firmware has *cleared* the reset request, acknowledging it has accepted it (`:645`). The read uses `operational=false`, so it tolerates the `0xdeadbeef` window during reset.

---

## 6. Context Lifecycle

### Purpose

One `struct fw_io_ctx` (`neuron_fw_io.h:249-259`) is the per-device mailbox handle, allocated at PCI probe and torn down at remove. It owns the two host buffers, the monotonic sequence counter, the serializing mutex, and the firmware error tally.

### Structure

| Field | Type | Role | Source |
|---|---|---|---|
| `bar0` | `void __iomem *` | device BAR0 base for all MiscRAM access | `:250` |
| `next_seq_num` | `u8` | monotonic 1..255, wraps, never 0 | `:251` (`:343`, `:425`) |
| `request` / `response` | `struct fw_io_request/response *` | host 64 KB request/response buffers | `:252-253` |
| `request_addr` / `response_addr` | `u64` | `virt_to_phys(buf) \| pci_host_base` | `:254-255` (`:736-746`) |
| `request_response_size` | `u32` | `FW_IO_MAX_SIZE = 0xffff` | `:256` (`:729`) |
| `fw_io_err_count` | `u64` | incremented on each FW `error_code != SUCCESS` | `:257` (`:387`, `:498`) |
| `lock` | `struct mutex` | serializes every transaction | `:258` |

### Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `fw_io_setup` | `:716-758` | `kzalloc` ctx, seq=1, `mutex_init`, two 64 KB host bufs, register readless region | CERTAIN |
| `fw_io_destroy` | `:760-776` | NULL-safe `kfree` of request/response/ctx | CERTAIN |
| `fw_io_get_err_count` | `:705-711` | return `fw_io_err_count` (0 if ctx NULL) | CERTAIN |

### Considerations

`fw_io_setup` `kmalloc`s both 64 KB host buffers with `GFP_ATOMIC` (`:730`, `:739`) even though probe runs in process context; the physical addresses are OR'd with `ndhal->ndhal_address_map.pci_host_base` (`:737`, `:746`), which assumes the `virt_to_phys` result has those high bits clear. The `next_seq_num` wrap rule (`++; if 0 then 1`) means the sequence space is 1..255 and zero is reserved as the "no response yet" marker the legacy poll clears (`:353`). On any allocation failure `fw_io_setup` jumps to `fw_io_destroy`, which is NULL-safe on partially-built contexts.

---

## Cross-References

- [FW-IO Trust Boundary](../security/fw-io-trust.md) — the security view of this aperture: request-CRC-only, the ignored response CRC (§4 GOTCHA), and what a malicious or buggy management-CPU response can do
- [Collectives Firmware Overview](../firmware/overview.md) — the NCFW collective *sequencer* Xtensa core, a different on-device processor from the Q7 management CPU this mailbox targets
- [The Q7 GPSIMD Engine](../gpsimd/overview.md) — the Vision-Q7 *vector* cores, the third Xtensa-family role; the three-core distinction the QUIRK warns about
- [Kernel Driver Overview](overview.md) — where FW-IO sits in the driver; consumers in reset, pod-election, power, metrics, sysfs, and cdev
- [Reset State Machine](reset.md) — consumer of `fw_io_initiate_reset` / `fw_io_is_reset_initiated`
- [Pod Election (UltraServer)](pod-election.md) — owner of the `0x180`–`0x198` neighbor/pod MiscRAM slots; consumer of the device-id/serial/server-info reads
