# SB2SB Remote-Copy Collective Kernel (`remote_copy.cpp`, opcode `0xBF`)

This page reconstructs the **on-device data-plane leg of an on-engine collective**: the
Xtensa-Q7 (Vision-Q7 NX "Cairo", `ncore2gp`) firmware kernel `remote_copy.cpp` that performs
the **cross-engine / cross-die State-Buffer → remote-State-Buffer copy**. It is the concrete
byte-mover that the host collective trigger pseudo-ops lower **to**, and the device leg every
**AllReduce** trace actually rides. The wire opcode is **`0xBF`** —
`NEURON_ISA_TPB_OPCODE_SB2SB_COLLECTIVE`, decoded against the 64-byte
`NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT`.

Everything here is **byte-pinned to shipped artifacts**: the per-generation
`NX_POOL` (SEQ control) and `Q7_POOL` (data-plane) DEBUG firmware images carved out of
`libnrtucode.a`, disassembled with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`,
FLIX/VLIW, `IsaMaxInstructionSize=32`); the shipped clean C ISA headers (`neuron_*_arch_isa`),
compile-checked; and the firmware's **own embedded `P%i:` / `S:` / `R:` DEBUG format strings**,
which name every kernel sub-routine and descriptor field. Where the FLIX-scheduled inner loops
desync `objdump`'s linear sweep, the format strings and headers are ground truth and the claim is
tagged `MED`. Where the binary disagrees with an earlier reading, **the binary wins**, marked
with a **CORRECTION**.

Confidence tags follow the [project model](../../reference/confidence-model.md): `OBSERVED` = a
byte / string / instruction / header read from a shipped artifact; `INFERRED` =
reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor; crossed
with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE** (orientation).

> **GOTCHA — two engines run the SAME opcode.** `0xBF` is decoded **twice**: once on the per-NeuronCore
> **SEQ control engine** (`*_NX_POOL` member, the `S:` stream) — which reads the 64-byte
> instruction word and *kicks* the move — and once on the **Q7 POOL data-plane engine**
> (`*_Q7_POOL` member, the `P%i:` stream, `remote_copy.cpp`) — which builds the iDMA descriptor
> ring and *moves the bytes*. A reimplementation must split the same opcode across both cores; the
> SEQ leg never touches SBUF data, the Q7 leg never re-validates the header. §3 and §6.

> **GOTCHA — emit bodies are FLIX-desynced; strings + structs are ground truth.** The
> `rdma_desc_gen` inner descriptor-build loop and the pre-sync flag-poll are densely-scheduled
> FLIX VLIW with literal pools interleaved in `.text`; `objdump`'s linear sweep loses bundle sync
> across those spans (they render as `.byte` / spurious `ivp_*`). Every body-level field claim is
> tagged `MED` and grounded on (a) the **byte-exact `P%i:`/`S:`/`R:` format strings** (§3), (b) the
> **compile-verified 64-B struct** (§2), never on a desynced opcode read. The **scalar** control
> flow — the call graph (§4), the SEQ handler (§4f), and the TX/RX doorbell protocol (§5) —
> decodes cleanly and is tagged `HIGH/OBSERVED` with addresses.

---

## 0. The SB2SB leg in one diagram

```
  host lowering (PSEUDO_TRIGGER 0xC8/0xD9/0xDA)  →  emits per ring/mesh step:  0xBF S3D3_COLLECTIVE
        │                                                                          │
        ▼ (the device ucode only decodes the LOWERED leg, never the pseudo trigger)│
  ┌──────────────────────── SEQ control engine  (*_NX_POOL, 'S:' stream) ──────────▼─────────┐
  │  178-entry dispatch  0xBF → trampoline → impl → handler @IRAM 0xD1E4 "S: SB2SB_Collective"│
  │     read 64-B word → copy src/dst TENSOR3D operand block into frame → call iDMA triggers   │
  └───────────────────────────────────────────────────┬───────────────────────────────────────┘
                                                       │  (kicks the Q7 iDMA data path)
  ┌──────────────────── Q7 POOL data-plane engine  (*_Q7_POOL, 'P%i:' stream, remote_copy.cpp) ┐
  │  decode_extended_inst_sb2sb @0x31ad  "Decode : SB2SB_Collective"                            │
  │     ├─ call0 0x463c0   read decode-dispatch context                                         │
  │     ├─ call8 0x328c    decode the S3D3 params (total_src/dst_nelem, dtype, src_nc/dst_nc)   │
  │     └─ call8 0x2eb8 ── PRE-SYNC  "This TPB (NC %u) is letting NC %u know we are ready"       │
  │  main driver @0x3300 → program_window (map remote SBUF into local 32-b window) →            │
  │  call8 0x161f4 ─────── rdma_desc_gen @0x161f4 (entry a1,0x2540 ≈ 9.5 KiB frame)             │
  │     build descriptor ring: src_addr/dst_addr, dma_mask, swizzle[16], xt_addrs[16],          │
  │     + LOCAL semaphore descriptor + REMOTE semaphore descriptor                              │
  │  rdma_desc_start @~0x17240 ── TX/RX tail-pointer doorbell (left_pop / right_push)           │
  │     RX: s32i tail-inc (RDRTP) + right_push signal   TX: left_pop wait + s32i tail-inc (TDRTP)│
  │        → the SDMA CME COPY streams SBUF bytes to the remote SBUF aperture                   │
  └──────────────────────────────────────────────────────────────────────────────────────────┘
        cross-die routing: dst_addr high bits {CAYMAN_ID, CAYMAN_ID_VALID, DIE} (routing_id)
        completion: LOCAL dma_compl_sema + REMOTE recv_sema (peer) — the NCFW counted barrier
```

One-line verdict: **SB2SB is `decode → pre-sync → program_window → rdma_desc_gen → rdma_desc_start`
on the Q7 data plane, triggered by the SEQ `0xBF` handler, moving SBUF→remote-SBUF over the iDMA
CME BD ring, with cross-die addressing in the SoC dst_addr high bits and step ordering enforced by
the local/remote completion semaphores.**

---

## 1. Carved objects + addressing

The SB2SB leg lives in **two members per generation** of `libnrtucode.a`: the `NX_POOL` member
(SEQ control engine; the `0xBF` handler) and the `Q7_POOL` member (data plane; `remote_copy.cpp`).
All carves are sha-confirmed
(`ar p libnrtucode.a img_<NAME>_contents.c.o | objcopy -O binary --only-section=.rodata`):

| Member (DEBUG) | Engine | `.rodata` size | sha256[:12] | Role |
|---|---|---|---|---|
| `img_CAYMAN_NX_POOL_DEBUG_IRAM`  | SEQ  | 116768 B | `8e4412b99201` | **`0xBF` handler** @`0xD1E4` (anchor) |
| `img_CAYMAN_NX_POOL_DEBUG_DRAM`  | SEQ  | 28448 B  | `7bdf6ed7ccd2` | `"S: SB2SB_Collective"` @off `0x2d50` (anchor) |
| `img_CAYMAN_Q7_POOL_DEBUG_IRAM`  | Q7   | 125504 B | `513a8a22d94b` | **`remote_copy.cpp` code** (decode/gen/start) |
| `img_CAYMAN_Q7_POOL_DEBUG_DRAM`  | Q7   | 89344 B  | `226f4254d475` | **`P%i:` SB2SB/rdma string corpus** |
| `img_MARIANA_PLUS_NX_POOL_DEBUG_DRAM` | SEQ | 29024 B | `d2e1552a13f1` | **v4+ fast-path** (`wait_for_credit`, §10) |
| `img_MARIANA_PLUS_Q7_POOL_DEBUG_DRAM` | Q7 | 89472 B | `295fae9c1cdb` | **`tensor_reshape_transpose_sb2sb`** (§10) |
| `img_SUNDA_Q7_POOL_DEBUG_DRAM`   | Q7   | 42496 B  | `44e70bc520ca` | **NO SB2SB strings** (v2 absence proof, §9) |

The two CAYMAN `NX_POOL` shas (`8e4412b99201` / `7bdf6ed7ccd2`) match the
[dispatch-hub](../seq/dispatch-hub.md) / [boot](../seq/boot.md) anchors exactly; the CAYMAN
`Q7` DRAM `226f4254d475` matches the [DGE emit page](../dge/dge-emit.md) carve.
**[HIGH / OBSERVED]**

**Addressing rules.** `.rodata` file-offset **== device VA** for IRAM (the disassembly addresses
below are raw file offsets and equal device VAs). DRAM images load at device VA `0x80000`, so a
**DRAM-string VA = file-offset + `0x80000`** (e.g. `"S: SB2SB_Collective"` at file off `0x2d50`
= VA `0x82d50`; the Q7 strings at off `0xNNN` = VA `0x80NNN`). The Q7 IRAM head bytes are
`06 7f 00 00` = `j 0x1fc` (the reset vector).

> **NOTE — provenance of the engine split.** `decode_extended_inst_sb2sb` / `decode_sb2sb_collective`
> (the Q7 leg) is **NOT** in the Q7 `kernel_info_table` (the `0xf0` POOL extended-inst dispatch,
> covered on [pool-dispatch](../pool/pool-dispatch.md)) — SB2SB is reached via the SEQ `0xBF`
> handler + the Q7 `remote_copy` decode path, **not** via the POOL `kernel_info_table` key set.

---

## 2. The `0xBF` instruction word it decodes — `S3D3_COLLECTIVE_STRUCT` (64 B)

This is the instruction both engines decode. Layout read **byte-exact and compile-verified** from
the shipped ISA header (`ISA_STATIC_ASSERT(sizeof == 64)`), header doc verbatim: *"Neuron
'S3D3_Collective' Format — one 3d SRC Tensor, one 3d DST Tensor. Use for: SB2SB Collective using
Pool/Q7 iDMA engine."* Sources: `aws_neuron_isa_tpb_s3d3_collective.h` +
`aws_neuron_isa_tpb_common.h`. **[HIGH / OBSERVED]**

| off | size | field | C type | meaning |
|---|---|---|---|---|
| 0  | 4  | `header`              | `NEURON_ISA_TPB_HEADER`      | `{opcode(1B)==0xBF, inst_word_len, debug_cmd, debug_hint}` |
| 4  | 8  | `events`              | `NEURON_ISA_TPB_EVENTS`      | `{wait_mode, wait_idx, update_mode, update_idx, semaphore_value:u32}` — the per-instruction semaphore arrive/wait (§5f) |
| 12 | 1  | `in_dtype`            | `NEURON_ISA_TPB_DTYPE`       | source element dtype |
| 13 | 1  | `out_dtype`           | `NEURON_ISA_TPB_DTYPE`       | dest element dtype (validator enforces `in_dtype == out_dtype`) |
| 14 | 2  | `reserved0[2]`        | `uint8_t[2]`                 | pad (validator: must be 0) |
| 16 | 16 | `src_mem_pattern`     | `NEURON_ISA_TPB_TENSOR3D`    | SOURCE SBUF 3-D access pattern (§2a) |
| 32 | 1  | `lnc_size_fmt`        | `NEURON_ISA_TPB_LNC_SIZE_FMT`| Logical-NeuronCore grouping = PEER/DIE set selector (§2b) |
| 33 | 1  | `num_active_channels` | `uint8_t`                    | # active pooling channels (firmware `num_chans`) |
| 34 | 14 | `reserved1[14]`       | `uint8_t[14]`                | pad (validator: `[0..13]` all 0) |
| 48 | 16 | `dst_mem_pattern`     | `NEURON_ISA_TPB_TENSOR3D`    | DEST (remote) SBUF 3-D access pattern |
| **64** | | | | one 64-B TPB instruction word |

> **CORRECTION — the header comment at offset 14 says `(35-47)` for `reserved1`; that is a typo in
> the vendor comment.** By the byte arithmetic (`lnc_size_fmt`@32 + `num_active_channels`@33 +
> `reserved1[14]`@34..47, then `dst_mem_pattern`@48), `reserved1` occupies **offsets 34–47**, and
> the `s3d3_collective_reserved_zero` predicate enumerates `reserved1[0..13]` (14 bytes) — confirming
> 34–47, not 35–47.

### 2a. `NEURON_ISA_TPB_TENSOR3D` (16 B) — the SBUF 3-D access pattern

```c
typedef struct NEURON_ISA_TPB_TENSOR3D {
    NEURON_ISA_TPB_ADDR4 start_addr;    //  4  union{ addr_immediate PARTITION_OFFSET | addr_reg ADDR_REG4 } — the SBUF base
    int16_t              step_elem[3];  //  6  three signed strides, in elements
    uint16_t             num_elem[3];   //  6  three shape dims, element counts
} NEURON_ISA_TPB_TENSOR3D;              // 16
```

So src/dst are full **3-D strided SBUF tensors**. The element counts (`num_elem`) are what the
firmware totals into `total_src_nelem` / `total_dst_nelem` (logged at `P%i: SB2SB_Collective :
total_src_nelem = %zu, total_dst_nelem = %zu, …`).

### 2b. `NEURON_ISA_TPB_LNC_SIZE_FMT` (off 32) — the die/NC peer grouping

```c
typedef enum {
    LNC1 = 0,  // "NC copies to itself. Used for self-test only."
    LNC2 = 1,  // "NC's are grouped as follows: NC0-NC1, NC2-NC3, NC4-NC5, NC6-NC7"
    LNC4 = 2,  // "NC's are grouped as follows: NC0-NC3, NC4-NC7"
    LNC8 = 3,  // "NC's are grouped as follows: NC0-7"
} NEURON_ISA_TPB_LNC_SIZE_FMT;
```

> **QUIRK — the `0xBF` validator REJECTS `LNC4` and `LNC8`.** The header predicate
> `has_valid_LncSizefmt(fmt)` is exactly `is_valid_enum(fmt) && fmt != LNC4 && fmt != LNC8` — so on
> this ISA an SB2SB step is **legal only for `LNC1` (self-test) or `LNC2` (one peer)**. A single
> SB2SB leg therefore moves between a NeuronCore and **its one LNC2 partner** (or itself); wider
> world sizes are realised by **chaining legs** over a ring/mesh, not by a wider `lnc_size_fmt`.
> **[HIGH / OBSERVED]**

### 2c. Validator predicates (header-exact)

The `is_valid_sb2sb_collective(i, nc)` conjunction is read verbatim from the header:

| predicate | rule |
|---|---|
| `has_sb2sbcollective_opcode` | `header.opcode == Opcode::Sb2sbCollective` (i.e. `0xBF`) |
| `has_valid_nc_sb2sb_collective` | **`nc >= NeuronCoreVersion::V3`** — the gen gate (§9) |
| `has_valid_LncSizefmt` | `fmt != LNC4 && fmt != LNC8` (§2b) |
| `check_active_ports` | `num_active_channels != 0 && num_active_channels <= POOLING_NUM_CHANNELS` (`= 128`) |
| `s3d3_collective_reserved_zero` | `reserved0[0..1] == 0 && reserved1[0..13] == 0` |
| `tensor3d_valid(src, …)` | `WriteTensor::False, AllowedInPSUM::False, AllowedInSBUF::True` — **src must be in SBUF, not PSUM** |
| `tensor3d_valid(dst, …)` | `WriteTensor::True,  AllowedInPSUM::False, AllowedInSBUF::True` — **dst must be in SBUF, not PSUM** |
| `size_check_src` | `t3d_element_count(src) <= 256` |
| `dtype_equality_check` | `in_dtype == out_dtype` |

> **CORRECTION — `size_check_src` is `<= 256` elements, not `<= 128`.** The backing prose loosely
> conflated `num_active_channels <= 128` (`POOLING_NUM_CHANNELS`) with the source size cap; the header
> predicate `size_check_src(tensor) -> t3d_element_count(tensor) <= 256` is a **separate, larger**
> bound on the source tensor's element count. Both apply: **≤128 active channels AND ≤256 source
> elements**.

> **GOTCHA — both endpoints must be SBUF; PSUM is forbidden.** `AllowedInPSUM::False` on both
> src and dst means SB2SB is a State-Buffer→State-Buffer mover only; routing PSUM through it fails
> host-side validation before the device ever sees the `0xBF`.

### 2d. DTYPE pass-through

`NEURON_ISA_TPB_DTYPE` (header-exact, the same enum the [dtype model](dtype-model.md) page pins):
`INVALID=0 UINT64=1 INT8=2 UINT8=3 INT16=4 UINT16=5 BFLOAT16=6 FP16=7 INT32=8 UINT32=9 FP32=0xA
FP32R=0xB INT64=0xC FP8_E3=0xD FP8_E4=0xE FP8_E5=0xF`. The firmware logs the numeric dtype verbatim
(`dtype=%d`); since `dtype_equality_check` forces `in == out`, SB2SB is a **dtype-preserving raw
copy** — no convert.

---

## 3. The kernel structure — from its own DEBUG log strings (Q7 DRAM)

The Q7 DEBUG build keeps verbose `P%i:` (per-CPU) / `R:` / `S:` strings; each is at a fixed DRAM
offset (device VA = offset + `0x80000`), loaded by a `const16 aX,8 ; const16 aX,<lo>` pair feeding
the printf-like logger (`call8 0x18a2c / 0x18b84 / 0x60e8`). The set below is OBSERVED **byte-exact**
in `CAYMAN_Q7_POOL_DEBUG_DRAM` and names every sub-routine and descriptor field.
**[HIGH / OBSERVED]**

| DRAM off | string (byte-exact) | names |
|---|---|---|
| `0x0a20` | `P%i: SB2SB_Collective : total_src_nelem = %zu, total_dst_nelem = %zu, dtype=%d, dst_nc=%zu, src_nc=%zu, mask=0x%x` | **top-level op log** |
| `0x0afe` | `P%i: Decode : SB2SB_Collective` | decode-entry tag |
| `0x0abf` | `P%i: Decode : Sbuf2Sbuf` | (alias path) |
| `0x0b1e` / `0x0ad8` | `decode_sb2sb_collective` / `decode_extended_inst_sb2sb` | decode fn names |
| `0x0e71` | `P%i: SB2SB_Collective: num_chans=%0d, tpb_idx=%u` | channel count / TPB index |
| `0x0bde` | `P%i: SB2SB Pre-sync: This TPB (NC %u) is letting NC %u know that we are ready to receive data.` | **cross-die PRE-SYNC** |
| `0x0c3e` | `P%i: SB2SB Pre-sync: remote_pool_xt_addr=0x%x, remote_q7_xt_addr=0x%x, sb2sb_ready_to_receive_remote=0x%x` | remote window addrs + ready flag |
| `0x0a93` | `P%i: iDMA channel %d failed to initialize!` | per-channel iDMA init guard |
| `0x0f98` | `P%i: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u` | cross-die/engine classify (§7b) |
| `0x0bc4`/`0x0bd0`/`0x0f81` | `soc2xt_addr` / `xt_window.hpp` / `soc_window_manager.hpp` | window-remapper anchors |
| `0x0ffd` | `R: program_window: num=%d, vld=%d, xt_addr=0x%llx, soc_addr=0x%llx, u_mask=0x%llx, l_mask=0x%llx` | **the remapper window** (§7c) |
| `0x105e` | `R: program_window: num=%d, mask=0x%llx, match=0x%llx, replace=0x%llx` | remapper match/replace rule |
| `0x4851` | `P%i: Q7: rdma_desc_gen [%s] Start, cpu_id=%d` | **descriptor builder entry** |
| `0x4885` | `P%i: Q7: rdma_desc_gen [%s] ring_num=%d, tpb_idx=%d, free_dim_bytes=%d, remote_tpb_idx=%d, routing_id=%d, dma_mask=0x%x` | descriptor inputs |
| `0x48fe` | `P%i: Q7: rdma_desc_gen [%s] dma_mask=0x%04x, n_active_dmas=%d` | active-DMA bitmap |
| `0x493d` | `P%i: Q7: rdma_desc_gen [%s] src_addr=0x%llx, dst_addr=0x%llx` | the SoC addresses |
| `0x497b` | `P%i: Q7: rdma_desc_gen [%s] after remote_routing_id=$%d, dst_addr=0x%llx, n_active_dmas=%d` | **dst high-bit rewrite** (§7a) |
| `0x49d7` | `P%i: Q7: rdma_desc_gen [%s] Starting descriptor loop, num_indices=%d, num_dma_descs=%d` | loop bound |
| `0x4a2f` | `P%i: Q7: rdma_desc_gen [%s] Loop iter %zu/%d: base_addr=0x%llx, n_active_dmas=%d, bytes_per_P=%d` | per-iter |
| `0x4a91` | `P%i: Q7:   shuffled_sbuf_swizzle[16] = [%u, … x16]` | partition swizzle table |
| `0x4afa` | `P%i: Q7:   xt_addrs[16] = [0x%08x%08x, … x16]` | 16 Q7-window 64-b addrs |
| `0x4bd6` | `P%i: Q7: rdma_desc_gen [%s] Pushing local semaphore descriptor, tpb_idx=%d, sem=%d` | **LOCAL sem descriptor** |
| `0x4c2a` | `P%i: Q7: rdma_desc_gen [%s] Pushing remote semaphore descriptor, remote_tpb=%d, routing_id=%d, sem=%d` | **REMOTE sem descriptor** |
| `0x4c91` | `P%i: Q7: rdma_desc_gen [%s] End` | builder end |
| `0x47f4` | `P%i: Trigger DMA; sdma_bcast_base = 0x%08x, trigger addr = 0x%08x, mask = 0x%08x n_desc=%d` | **broadcast launch** (§6) |
| `0x4cb2` | `P%i: Q7: rdma_desc_start [%s] Start, cpu_id=%d` | launch entry |
| `0x4e27` | `P%i: Q7: rdma_desc_start [TX] Waiting for RX sync (left_pop)` | **TX wait** (§5b) |
| `0x4e65` / `0x4ea3` | `…[TX] Writing tail pointer increment` / `…written` | TX doorbell |
| `0x4ee1` | `P%i: Q7: rdma_desc_start [RX] Writing tail pointer increment` | **RX doorbell** (§5c) |
| `0x4f1f` / `0x4f63` | `…[RX] Signaling TX to proceed (right_push)` / `…TX signaled` | RX signal |
| `0x4f8e` | `P%i: Q7: rdma_desc_start [%s] End` | launch end |
| `0x5040` | `P%i: ERROR: DescriptorStream wrote %d descriptors, expected %d` | **FATAL count mismatch** (§11) |
| `0x4e17` | `remote_copy.cpp` | the source-file tag |

These strings alone pin the decomposition: **decode → pre-sync → program_window → `rdma_desc_gen`
(build descriptors incl. local+remote semaphore descs) → `rdma_desc_start` (TX/RX doorbell)**. §4–§5
confirm the call graph and TX/RX control flow instruction-exact.

---

## 4. The call graph — instruction-exact (master disasm, `XTENSA_CORE=ncore2gp`)

All addresses below are **Q7 IRAM file offsets == device VAs**.

### 4a. Q7 decode entry — `decode_extended_inst_sb2sb`, `@0x31ad`

```
  31ad:  const16 a10, 0xafe          ; (low half of "Decode : SB2SB_Collective" string ptr)
  31b0:  call8   0x18a2c             ; → LOG "P%i: Decode : SB2SB_Collective"
  31b4:  call0   0x463c0             ; read the decode-dispatch context
  31cd:  call8   0x328c              ; decode the S3D3 instruction params
  31d3:  call8   0x2eb8              ; *** the SB2SB PRE-SYNC function (§4b)
  31ff:  call8   0x2f38              ; (post-decode continuation)
```

**[HIGH/OBSERVED; MED worker identities]**

### 4b. Pre-sync — `@0x2eb8` (`entry a1, 32`)

```
  2eb8:  entry   a1, 32
   …
  2ee5:  call8   0x18a2c             ; → LOG "SB2SB Pre-sync: This TPB (NC %u) is letting NC %u know …"
   …                                 ;   (loads string @0xbde)
  2f22:  call8   0x4572c             ; the remote-ready post sub
```

The companion `remote_pool_xt_addr / remote_q7_xt_addr / sb2sb_ready_to_receive_remote` log
(`0xc3e`) is loaded inside a FLIX bundle here and desyncs; the window addresses it prints come from
§7. **[HIGH/OBSERVED; MED inner log site]**

### 4c. SB2SB main driver — `@0x3300`, hands off at `@0x3742`

The large driver (`entry a1, 32`) threads decode/pre-sync/channel-loop and, at IRAM `0x3742`:

```
  3742:  call8   0x161f4             ; *** rdma_desc_gen — build the descriptor ring (§4d)
```

**[HIGH/OBSERVED edge; MED loop bound]**

### 4d. `rdma_desc_gen` — `@0x161f4` (`entry a1, 0x2540`)

```
  161f4:  entry   a1, 0x2540         ; ~9.5 KiB frame (holds shuffled_sbuf_swizzle[16] / xt_addrs[16])
  16210:  const16 a10, 8
  16216:  const16 a10, 0x4851        ; → LOG "rdma_desc_gen [%s] Start, cpu_id=%d"  (cpu_id = rsr.prid)
   …                                 ;   subsequent logs 0x4885 / 0x493d / 0x4afa
```

The descriptor-array build and the local/remote semaphore-descriptor pushes (`0x4bd6` / `0x4c2a`)
sit in the FLIX-scheduled body and are desynced — their **semantics** are taken from the §3 log
strings. **[HIGH/OBSERVED entry; MED inner build]**

### 4e. `rdma_desc_start` — the TX/RX launch fn (body ≈ `0x17240..0x173f0`)

Reached after `rdma_desc_gen` returns; the instruction-exact protocol is §5. The crisp, scalar
TX/RX control flow decodes cleanly (unlike the desynced builder body).

### 4f. SEQ control-engine `0xBF` handler — `seq_iram` `@0xD1E4` (`entry a1, 32`)

This is the upstream trigger that *starts the whole chain*, from
`CAYMAN_NX_POOL_DEBUG_IRAM` (sha `8e4412b99201`):

```
  d1e4:  entry   a1, 32
  d1e7:  j       0xd1ed
  d1ed:  const16 a10, 8
  d1f0:  const16 a10, 0x2d50         ; string @VA 0x82d50 = "S: SB2SB_Collective"
  d1f3:  call8   0x18b84             ; → LOG "S: SB2SB_Collective"
   …     (operand-block copy of the decoded S3D3 src/dst TENSOR3D into the handler frame)
   …     call8   0x98c8  /  call8 0x98fc   ; program / trigger the Q7 iDMA move
   …     retw.n
```

**[HIGH/OBSERVED; MED helpers + `l32i/s32i` stride]** The SEQ dispatch routing (`0xBF` → trampoline → impl → `0xD1E4`) rides the 178-entry table
documented on [dispatch-hub](../seq/dispatch-hub.md).

---

## 5. The TX/RX protocol + semaphore handshake (instruction-exact)

`rdma_desc_start`'s role split and tail-pointer doorbell decode cleanly.

### 5a. Role determination — `@0x1736d`

```
  1736d:  rsr.prid a11             ; this core's processor-ID
  17370:  extui   a7, a7, 0, 1     ; a7 = bit0 of a loaded role/parity word
  17373:  bne     a7, a6, 0x173ec  ; mismatch → ERROR path (DescriptorStream wrote != expected)
  17376:  beqz.n  a6, 0x173a0      ; *** a6==0 → RX path ; a6!=0 → TX path falls through
```

### 5b. TX path (`a6 != 0`) — `@0x1737e`

```
  1737e:  callx8  a5               ; → LOG "[TX] Waiting for RX sync (left_pop)"  (string 0x4e27)
   …       (l32r the RX-sync / tail-inc reg address into a4)
  17392:  callx8  a5               ; → LOG "[TX] Writing tail pointer increment"  (string 0x4e65)
  1739b:  s32i.n  a3, a4, 0        ; *** WRITE tail-pointer-increment: store a3 (inc count) to [a4] = TDRTP_inc
   …       → LOG "[TX] Tail pointer increment written"  (string 0x4ea3) ; → common END
```

### 5c. RX path (`a6 == 0`) — `@0x173a0`

```
  173a6:  callx8  a5               ; → LOG "[RX] Writing tail pointer increment"  (string 0x4ee1)
  173a9:  s32i.n  a3, a4, 0        ; *** WRITE RX tail-pointer-increment to [a4] = RDRTP_inc
  173ae:  rsr.prid a11
  173b4:  callx8  a5               ; → LOG "[RX] Signaling TX to proceed (right_push)"  (string 0x4f1f)
   …       (l32r the right_push signal target) ; → common END
```

### 5d. Common END — `@0x173c9`

```
  173c9:  rsr.prid a11
  173cc:  callx8  a5
   …       → LOG "[%s] End"  (string 0x4f8e)
  173ea:  retw.n
```

**[HIGH / OBSERVED]**

### 5e. Interpretation — the producer/consumer ring handshake

The two ends of the SDMA producer/consumer descriptor ring:

- **RX** (the receiver / remote TPB) **first** advances its S2M tail (`RDRTP_inc`) to publish empty
  receive descriptors, then **`right_push`** signals the TX side that it may proceed.
- **TX** (the sender) **`left_pop`** waits for that RX signal, then advances its M2S tail
  (`TDRTP_inc`) by the descriptor count — which launches the CME COPY that streams the SBUF bytes to
  the remote SBUF aperture.

The two `s32i.n a3,a4,0` stores are the **DmaTrigger** primitive — "initiate a DMA transfer by
writing the DMA tail pointer" — the `TDRTP_inc` / `RDRTP_inc` `+0x38` doorbells of the SDMA CSR map.

> **QUIRK — `left_pop` / `right_push` are firmware-only literal tokens.** They are read from the
> device image; the sibling DMA/CSR notes record the ring producer/consumer protocol but not these
> exact tokens. **[HIGH/OBSERVED strings; MED direction]**

> **GOTCHA — the exact register a4 holds (TDRTP vs RDRTP) is FLIX-bound.** The absolute tail-inc CSR
> offset loaded into `a4` (per role) is in a desynced literal-pool span; that it is the M2S/S2M
> `*RTP_inc` doorbell is reconciled from the CSR map, not read from the literal.
> **[MED — literal value INFERRED]**

### 5f. The `0xBF` `events` semaphore (instruction-level arrive/wait)

The 64-byte instruction's `events` field (off 4, §2) carries `{wait_mode, wait_idx, update_mode,
update_idx, semaphore_value}` — the hardware semaphore the `0xBF` **waits on before issue** and
**updates on completion**. Combined with the `rdma_desc_gen` LOCAL + REMOTE semaphore descriptors
(§6), this is the on-chip end of the NCFW counted barrier: a step's SB2SB waits its inbound semaphore
≥ target (`wait_ge_and_dec`) and increments the peer's semaphore on completion (`add_semaphore_inc`).
The counted-barrier ops live on the LX/NCFW side; this leg posts the `dma_compl_sema`.
**[HIGH/OBSERVED semantics; MED NCFW mapping]**

---

## 6. The iDMA / RDMA descriptor it builds

`rdma_desc_gen` builds, per active channel/DMA, a descriptor ring whose inputs are named verbatim by
the §3 logs. The descriptor it programs is the **SDMA CME BD ring** (the `_dma_ctx_t` /
`SDMA_CME_BD_DESC` 16-B entry). The field set ↔ descriptor mapping:

| `rdma_desc_gen` input (log) | SDMA / ring meaning |
|---|---|
| `ring_num` | which BD ring (M2S / S2M queue index) |
| `tpb_idx` | this NeuronCore's TPB index (LOCAL) |
| `free_dim_bytes` | bytes per free-dim row of the move |
| `remote_tpb_idx` | the PEER NeuronCore's TPB index |
| `routing_id` / `remote_routing_id` | the cross-die routing id (→ `CAYMAN_ID` / neighbor route, §7a) |
| `dma_mask` / `n_active_dmas` | bitmap of which iDMA channels/queues participate (≤ `num_active_channels`) |
| `src_addr` (`0x%llx`) | SOURCE SoC addr (local SBUF) |
| `dst_addr` (`0x%llx`) | DEST SoC addr (remote SBUF, **post** `remote_routing_id` rewrite) |
| `shuffled_sbuf_swizzle[16]` / `xt_addrs[16]` | per-channel SBUF partition swizzle + the 16 Q7-window 64-b addresses |
| LOCAL semaphore descriptor (`0x4bd6`) | `{tpb_idx, sem}` — a BD that, on completion, increments **THIS** core's semaphore (`dma_compl_sema`) |
| REMOTE semaphore descriptor (`0x4c2a`) | `{remote_tpb, routing_id, sem}` — a BD that increments the **PEER** core's semaphore across the die fabric |

**Mapping onto the 16-B SDMA BD:** `SDMA_CME_BD_DESC = { word0 (length_meta = chunk bytes; small
generation/ring tag); word1 (CME command word: optype = COPY, op = SDMAOP, endian/crc controls);
buf_ptr (u64 SoC addr) = src_addr on the read BD, dst_addr on the write BD }`. The local/remote
semaphore descriptors are additional BDs in the **same** ring that post a semaphore increment at the
source and at the remote sink.

> **NOTE — the bit-exact 16-B word0/word1 packing is a forward Part-9 fact.** This page establishes
> the *mapping* (which named input → which descriptor field) from the byte-exact log strings; the
> exact 16-B BD encoding is deferred to the [RDMA descriptor gen/start](rdma-desc-gen-start.md)
> sibling and the [CCE in-transfer](../../dma/cce-in-transfer.md) page (**Part 9/10 — not yet
> authored, NOTE: forward link**). **[HIGH/OBSERVED names; MED packing]**

**The broadcast launch (`sdma_bcast`).** `rdma_desc_start` logs `Trigger DMA; sdma_bcast_base = 0x…,
trigger addr = 0x…, mask = 0x…, n_desc=%d` (`0x47f4` / `0x4d39`). `sdma_bcast_base` is the **broadcast
M2S/S2M aperture**: a single tail-inc write to the bcast doorbell advances the tails of a **group** of
queues at once (the `dma_mask` selecting which), launching the multi-channel move with one trigger.
**[HIGH/OBSERVED usage; MED exact group]**

---

## 7. Cross-die addressing — SoC dst_addr high bits + the local-window remapper

### 7a. The SoC address the descriptors carry

```
  bits [46:0]  LOCAL              47-bit per-die byte address (selects the SBUF byte inside the STATE_BUF aperture)
  bit  [47]    DIE                which die of the 2-die package
  bits [53:48] CAYMAN_ID          chip id in the multi-die mesh (2^6)
  bit  [54]    CAYMAN_ID_VALID    1 ⇒ route by chip id across the inter-die fabric ; 0 ⇒ stay on the originating chip
```

The `rdma_desc_gen` `routing_id` / `remote_routing_id` is the value that, written into
`{CAYMAN_ID, CAYMAN_ID_VALID}` (and the neighbor decoder's exit-die / neighbor-route bits), turns a
local `dst_addr` into a **REMOTE-die** SoC address. The firmware's `after remote_routing_id …
dst_addr=0x%llx` log (`0x497b`) is exactly this rewrite.
**[HIGH bitfield; MED routing_id rewrite]**

### 7b. `is_tpb` / `is_die_0` / `engine_idx` decode

The `engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u` log (`0xf98`) is
the firmware classifying a SoC base: whether it targets the TPB SBUF aperture (`is_tpb`), whether it
is die 0 (the `DIE[47]` bit), and the engine index — how the kernel decides local-vs-remote and which
engine's SBUF it touches. **[HIGH/OBSERVED string; MED constants]**

### 7c. The remote-SBUF → local-Q7-window remapper

Because the Q7 cores address SBUF through a **32-bit NX-local window** (`soc2xt_addr` / `xt_window.hpp`
/ `soc_window_manager.hpp`), a REMOTE SBUF SoC address must be programmed into a remapper window
before the Q7 iDMA can reach it. `program_window` writes the remapper CSRs that map `{xt_addr ↔
soc_addr}` with `u_mask`/`l_mask` and a match/replace rule (logs `0xffd` and `0x105e`). The pre-sync
log `remote_pool_xt_addr=0x%x, remote_q7_xt_addr=0x%x` (`0xc3e`) prints the two programmed local-window
views of the remote peer's POOL/Q7 SBUF. This is the same remapper the
[SoC window manager](../seq/soc-window-manager.md) page documents.
**[HIGH/OBSERVED logs; LOW CSR offsets]**

---

## 8. The cross-die PRE-SYNC handshake (before the DMA)

Before the bytes move, the two NeuronCores exchange a readiness handshake (so the sender does not
write into a buffer the receiver has not yet armed):

- `SB2SB Pre-sync: This TPB (NC %u) is letting NC %u know that we are ready to receive data.`
  (`0xbde`) — the **RECEIVER** posts a "ready_to_receive" flag/semaphore to the sender; the log prints
  both NC indices (self + peer).
- `SB2SB Pre-sync: remote_pool_xt_addr=0x%x, remote_q7_xt_addr=0x%x, sb2sb_ready_to_receive_remote=0x%x`
  (`0xc3e`) — the **SENDER** reads the remote peer's POOL/Q7 window addresses and the remote ready flag
  to confirm the receiver is armed.

The pre-sync function is `@0x2eb8` (§4b), called from the decode path at `0x31d3` **before**
`rdma_desc_gen`/`start`. This is the SB2SB analogue of the NCFW `recv_sema`/`post_sema`: the receiver
signals "buffer armed", the sender waits on it, then triggers the DMA.
**[HIGH/OBSERVED logs; MED flag-poll]**

---

## 9. Per-generation presence — proven by bytes

> **The SB2SB device leg exists from NC-v3 (CAYMAN) onward and is ABSENT on NC-v2 (SUNDA).** This is
> proven three independent ways.

| Codename | NC-ver | `s3d3_collective.h`? | `0xBF` opcode? | Q7 DRAM `remote_copy.cpp` / `SB2SB_Collective` / `rdma_desc_gen` | verdict |
|---|---|---|---|---|---|
| **TONGA**  | NC-v1 | — (legacy ISA only) | — | (no NCFW/EXTISA image in corpus) | **leg absent** `[CARRIED]` |
| **SUNDA**  | NC-v2 | **ABSENT** | **none** | **0 / 0 / 0** (0 hits) | **leg ABSENT** |
| **CAYMAN** | NC-v3 | present (`nc>=V3`) | `0xbf` | **1 / 3 / 10** | leg present |
| **MARIANA** | NC-v4 | present (`nc>=V3`) | `0xbf` | **1 / 3 / 10** | leg present |
| **MARIANA_PLUS** | NC-v4+ | present (`nc>=V3`) | `0xbf` | **1 / 3 / 10** + `tensor_reshape_transpose_sb2sb` | leg present + **v4+ fast-path** (§10) |
| **MAVERICK** | NC-v5 | present (`nc>=V3`) | `0xbf` | **header-OBSERVED; image only in `libnrtucode_internal.so`** | leg present → **interior INFERRED** `[MED/INFERRED]` |

Grounding:

1. **Header gate.** `has_valid_nc_sb2sb_collective(nc) -> (nc >= NeuronCoreVersion::V3)` is read
   verbatim from every present `aws_neuron_isa_tpb_s3d3_collective.h` (cayman/mariana/maverick). The
   **sunda** arch-isa dir has **no `s3d3_collective.h`** and `common.h` carries **no
   `SB2SB_COLLECTIVE` opcode** — only a stale `LNC_SIZE_FMT` doc-comment mentioning "sb2sb collective
   using q7 iDMA", with no opcode/struct behind it. **[HIGH/OBSERVED]**
2. **String fingerprint.** Carving every `*_Q7_POOL_DEBUG_DRAM` member: **SUNDA = 0 hits** for all of
   `remote_copy.cpp` / `SB2SB_Collective` / `rdma_desc_gen` / `left_pop`; CAYMAN/MARIANA/MARIANA_PLUS
   each = `1 / 3 / 10 / 1` (identical counts).
3. **Member existence.** `libnrtucode.a` ships `NX_POOL` + `Q7_POOL` members for **SUNDA, CAYMAN,
   MARIANA, MARIANA_PLUS only**. **MAVERICK has NO member in the shipped archive** — it exists solely
   in `libnrtucode_internal.so`'s `.rodata` (the internal NC-v5 twin).

> **MAVERICK (NC-v5) interiors are HEADER-OBSERVED → INFERRED.** MAVERICK's `s3d3_collective.h` and
> `0xBF` opcode are read directly from its arch-isa headers (so the *contract* is OBSERVED), but its
> firmware images are not in the shipped runtime archive — so the **kernel interior** (the byte-exact
> `rdma_desc_gen`/`rdma_desc_start` addresses) cannot be carved from a shipped blob and is **INFERRED**
> to match the CAYMAN..MARIANA_PLUS family by header contract + the codename crosswalk.
> **[contract HIGH/OBSERVED; interior MED/INFERRED]** See [codename crosswalk](../../reference/codename-crosswalk.md)
> and the [MAVERICK profile](../../generations/maverick-profile.md) (**forward link — not yet authored,
> NOTE**).

---

## 10. The MARIANA_PLUS (v4+) fast-path family — proven by bytes

> **QUIRK — v4+ adds a credit-gated fast copy path that is unique to MARIANA_PLUS.** Three strings,
> present **only** in the MARIANA_PLUS images and **absent** in SUNDA / CAYMAN / MARIANA, mark a new
> fast-path family:

| string | image | file off | present in |
|---|---|---|---|
| `dge_reshape_memcopy_transpose_fast` | `MARIANA_PLUS_NX_POOL_DEBUG_DRAM` | `0x34b0` | **MARIANA_PLUS only** |
| `dge_decode_fast.cpp` | `MARIANA_PLUS_NX_POOL_DEBUG_DRAM` | `0x34d3` | **MARIANA_PLUS only** |
| `wait_for_credit` | `MARIANA_PLUS_NX_POOL_DEBUG_DRAM` | `0x3914` | **MARIANA_PLUS only** |
| `tensor_reshape_transpose_sb2sb` | `MARIANA_PLUS_Q7_POOL_DEBUG_DRAM` | (Q7 corpus) | **MARIANA_PLUS only** |

Across all four `NX_POOL` DRAM gens (SUNDA/CAYMAN/MARIANA/MARIANA_PLUS) and all four `Q7_POOL`
DRAM gens, the three NX strings and the one Q7 string appear with count 1 in MARIANA_PLUS and
count 0 in every earlier gen. **[HIGH/OBSERVED]**

Read together with the `[TX] Waiting for RX sync (left_pop)` doorbell (§5b), `wait_for_credit` is the
v4+ name for the credit/handshake step the earlier gens spell as the `left_pop` wait — a credit-gated
producer/consumer gate added on the SEQ/sequencer side, paired with a
`dge_reshape_memcopy_transpose_fast` reshape-and-move fast path and a Q7
`tensor_reshape_transpose_sb2sb` leg. **The wire opcode is unchanged — still `0xBF`,
`S3D3_COLLECTIVE_STRUCT` — and the EXTISA byte set is byte-identical to MARIANA; the v4+ delta is the
firmware fast-path, not the ISA.**
**[CARRIED invariance; HIGH strings; MED credit-gate reading]**

---

## 11. Error / completion handling

- **iDMA channel init failure** — `iDMA channel %d failed to initialize!` (`0xa93`): a per-channel
  init guard. **[HIGH/OBSERVED]**
- **DescriptorStream count mismatch (FATAL)** — `rdma_desc_start`'s role/parity check
  (`0x17373 bne → 0x173ec`) and `ERROR: DescriptorStream wrote %d descriptors, expected %d` (`0x5040`)
  feed `const16 a2,0x60e8 ; callx8 a2` — the `_Assert`/error sink (the same `0x60e8` logger/assert the
  [decode-pool](decode-pool.md) path uses). A descriptor-count mismatch is a hard error.
  **[HIGH/OBSERVED; MED assert-sink identity]**
- **Completion is the semaphore/poll model.** The TX/RX tail-pointer doorbell launches the CME COPY;
  completion is signalled by the ring's completion BD (busy-poll on the generation tag) **and** by the
  LOCAL/REMOTE semaphore descriptors firing `dma_compl_sema` / the peer `recv_sema`. There is **no
  INTC-trigger completion** for the data plane — completion is semaphore-over-DMA.
  **[HIGH completion model; MED Q7 poll site]**

---

## 12. How an AllReduce trace rides this leg

> **This is the device leg the AllReduce trace rides.** The host emits a `PSEUDO_TRIGGER_COLLECTIVE`
> (`0xC8` / `0xD9` / `0xDA`) carrying a `collective_type` of `ALL_REDUCE` (`= 0x1` in the
> `NEURON_ISA_TPB_COLLECTIVE_TYPE` enum); the host lowering expands that into a
> **sequence of `0xBF` SB2SB legs** over the ring/mesh topology; the device ucode **never** decodes
> the pseudo trigger — it only decodes the lowered SB2SB legs, **which is this kernel**.

Per ring/mesh step *k*, for each participating NeuronCore:

1. The host lowering emits a `0xBF` `S3D3_COLLECTIVE` instruction with the src/dst SBUF 3-D patterns,
   `in_dtype == out_dtype`, `lnc_size_fmt` (`LNC1`/`LNC2`, §2b), `num_active_channels`, and the
   `events` semaphore wait/update.
2. The **SEQ** engine's `0xBF` handler (§4f) decodes it; the **Q7** `remote_copy` path runs:
   **pre-sync** (§8) → **program_window** (§7c) → **`rdma_desc_gen`** (§6, incl. LOCAL + REMOTE
   semaphore descriptors) → **`rdma_desc_start`** (§5; RX advances S2M tail + `right_push`; TX
   `left_pop`-waits then advances M2S tail = launch).
3. On completion the CME COPY's last BD increments the LOCAL `dma_compl_sema` and (via the remote
   semaphore descriptor, routed by `routing_id` across the die) the PEER's `recv_sema`. The next step's
   SB2SB on the peer waits its `recv_sema ≥ target` (the NCFW counted barrier) before issuing —
   **chaining the legs into a ring/mesh traversal**.
4. Cross-die legs set `CAYMAN_ID_VALID` / `DIE` in `dst_addr` (§7a) so the copy traverses the
   inter-die data fabric.

So: **one ring/mesh step = one (or `num_active_channels`-wide) SB2SB leg = one `rdma_desc_gen` +
`rdma_desc_start` invocation**; step-to-step ordering is the semaphore counted barrier; the whole
AllReduce is the sequence of these legs over the topology. The leg↔step correspondence + semaphore
chaining are HIGH from the combined ISA + device-decode evidence; the exact per-(ctype, topology,
world-size) leg SCHEDULE lives in the LX/NCFW firmware and is not decodable from the Q7 image.
**[MED/LOW schedule]**

For the orchestration side (algorithm selection, per-step peer/semaphore assignment), see the
collectives architecture pages: [architecture synthesis](../../collectives/ops/architecture-synthesis.md)
and [S3D3 collective](../../collectives/ops/s3d3-collective.md) (**Part 9 — not yet authored, NOTE:
forward links**). For the cross-die transport, see [RDMA cross-die](../../dma/rdma-cross-die.md) and
[CCE in-transfer](../../dma/cce-in-transfer.md) (**Part 10 — not yet authored, NOTE**).

---

## 13. Reconciliation table (this device leg ↔ ISA / cross-page anchors)

| this report (Q7 `remote_copy`) | anchor | status |
|---|---|---|
| opcode `0xBF` `S3D3_COLLECTIVE_STRUCT` (64 B, in/out dtype, src/dst TENSOR3D, `lnc_size_fmt`, `num_chans`) | `common.h:262 SB2SB_COLLECTIVE=0xbf`; `instruction_mapping.json` binds struct → opcode | **CONFIRMED byte-exact** |
| `decode_sb2sb_collective` / `decode_extended_inst_sb2sb` | Q7 DRAM strings `0xb1e` / `0xad8` | **CONFIRMED, located in Q7 image** |
| SEQ `0xBF` handler `@0xD1E4` | `"S: SB2SB_Collective"` @VA `0x82d50`; dispatch-hub 178-entry table | **CONFIRMED byte-exact** |
| TX/RX tail-pointer doorbell (`s32i a3,[tail_inc]`) | `TDRTP_inc`/`RDRTP_inc` `+0x38`; DmaTrigger | **CONFIRMED (the DmaTrigger write)** |
| `sdma_bcast` (one trigger, `dma_mask`) | broadcast M2S/S2M aperture | **CONFIRMED** |
| LOCAL/REMOTE semaphore descriptors | NCFW `recv/send/post/dma_compl_sema`; `add_semaphore_inc`/`wait_ge_and_dec` | **CONFIRMED (on-chip sema legs)** |
| `left_pop` / `right_push` | UDMA producer/consumer ring | **IMPROVED — literal tokens recovered here** |
| cross-die `dst_addr` `routing_id` | SoC `{CAYMAN_ID, _VALID, DIE}` | **CONFIRMED** |
| `program_window` remapper | the `soc2xt` window program; [SoC window manager](../seq/soc-window-manager.md) | **CONFIRMED (the window prog)** |
| per-gen presence: SUNDA absent / CAYMAN..MARIANA_PLUS present / MAVERICK header-only | `nc>=V3` gate; member existence; string fingerprint | **CONFIRMED** |
| v4+ fast-path (`wait_for_credit` / `…_fast` / `tensor_reshape_transpose_sb2sb`) | MARIANA_PLUS-only strings @`0x34b0`/`0x34d3`/`0x3914` | **CONFIRMED MARIANA_PLUS-only** |

---

## 14. Confidence summary

**HIGH / OBSERVED** (direct disasm / byte read / compile-verified header):

- Carves: CAYMAN `NX_POOL` IRAM (`8e4412b99201`, 116768 B) + DRAM (`7bdf6ed7ccd2`, 28448 B); CAYMAN
  `Q7_POOL` IRAM (`513a8a22d94b`, 125504 B, reset `j 0x1fc`) + DRAM (`226f4254d475`, 89344 B);
  MARIANA_PLUS NX/Q7 DRAM; SUNDA Q7 DRAM (absence proof).
- The 64-B `S3D3_COLLECTIVE_STRUCT` layout + all nine validator predicates (`nc>=V3`, `LNC4/LNC8`
  rejected, `num_active_channels ∈ [1,128]`, `src ≤ 256` elems, dtype equality, SBUF-only).
- The decode call graph: `0x31ad` "Decode:SB2SB" → `0x31d3` pre-sync(`0x2eb8`) → `0x3300` driver →
  `0x3742` `rdma_desc_gen`(`0x161f4` `entry a1,0x2540`); SEQ `0xBF` handler `0xD1E4` (`entry a1,32`,
  `const16 a10,0x2d50`, `call8 0x18b84`).
- The full `rdma_desc_start` TX/RX protocol byte-exact: `rsr.prid` role check (`0x1736d`),
  `extui a7,a7,0,1`, `bne a7,a6,0x173ec`, `beqz.n a6,0x173a0`, the two `s32i.n a3,a4,0` tail-inc stores
  (TX `0x1739b` / RX `0x173a9`), `retw.n` `0x173ea`.
- The full `P%i:`/`R:`/`S:` SB2SB/rdma DEBUG string corpus at fixed DRAM offsets; `left_pop`/
  `right_push` tokens recovered.
- Per-gen presence (SUNDA absent, CAYMAN..MARIANA_PLUS present) and the MARIANA_PLUS-only v4+ fast-path
  strings, both by `strings | rg -c` over the carves.

**MED** (strong inference, often across a documented FLIX/literal desync): the `rdma_desc_gen` inner
build (descriptor word0/word1, the semaphore-descriptor push sites, swizzle/xt_addr tables) — taken
from log strings; the exact tail-inc register (TDRTP vs RDRTP); `routing_id` → SoC high-bit rewrite;
the leg↔step correspondence + counted-barrier chaining; SEQ `0x98c8`/`0x98fc` as iDMA-trigger helpers
(positional); MAVERICK interior (header-observed → inferred).

**LOW / UNRECOVERED**: concrete remapper / `TDRTP_inc` absolute CSR offsets (not present in this
image); the exact pre-sync flag-poll arithmetic and the descriptor-count comparison constants (inside
FLIX bundles the linear sweep mis-decodes).
