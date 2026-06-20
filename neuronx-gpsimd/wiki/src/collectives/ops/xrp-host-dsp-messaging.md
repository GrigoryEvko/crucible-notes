# XRP Host↔DSP Messaging Transport

> **VERDICT (this page, one line):** **There is no Cadence XRP framework anywhere in
> the shipped Neuron runtime.** The host↔device messaging that carries commands to the
> on-device Xtensa cores (TOP_SP / POOL‑Q7 / NCFW) and returns completions is an
> **entirely bespoke** Annapurna‑Labs ("AL") HAL + Neuron `tdrv` queue stack. This page
> proves the negative from a concrete zero‑match string/symbol sweep, then decodes the
> three real transports byte/struct‑exact:
> **(A) the general `hw_exec_queue`** AL/UDMA descriptor ring, **(B) the `xt_cc` Q7
> command queue** (the JPEG‑decode rider), **(C) the EVT_SEM doorbells + Notification
> Queue completion ring**, plus the host‑side async‑exec worker pool that sits above
> them.

All facts below are recovered by static analysis of three **host x86‑64** shared
objects shipped in the Neuron runtime package (DMCA 17 U.S.C. §1201(f) interoperability):

| object | path / size | BuildID | sections used |
|---|---|---|---|
| `libnrt.so.2.31.24.0` | `…/opt/aws/neuron/lib/` · 122,956,336 B | `8bb57aba…102e` | `.text`/`.rodata` VMA==fileoff; `.data` VMA `0xc07e00`==fileoff (**no delta**) |
| `libncfw.so` | `…/opt/aws/neuron/lib/` · 615,640 B | `a98f8e1c…0db5` | symbol/string sweep |
| `libnrtucode_extisa.so` | `…/opt/aws/neuron/lib/` · 9,656,488 B | — | device‑ring strings |

Tools: GNU `objdump`/`nm`/`readelf`/`strings` on the **host** binaries. The Q7 / TOP_SP /
NCFW receive loops run on **Xtensa** cores; this page decodes the **host producer side**
(authoritative for the on‑wire format the host emits) and the embedded Q7 firmware blob
sizes. Confidence tags: **HIGH** = byte‑exact disasm / register immediate / verbatim
string / `nm`; **MED** = strong cross‑binary inference; **OBSERVED** = read from a shipped
artifact; **INFERRED** = reconstructed.

---

## 1. The XRP verdict — exhaustive evidence of absence  [HIGH / OBSERVED]

A Cadence‑XRP host↔DSP RPC stack, if present, would ship `xrp_queue`/`xrp_request`/
`xrp_comm` structs, an `xrp_run_command`/`xrp_enqueue_command` API, a `libxrp` object, and
would normally ride `rpmsg`/`virtio` or a hardware mailbox. **None of those symbols,
strings, or objects exist in this corpus.** The proof is a concrete zero‑match sweep, not
an assertion:

```text
# symbol table (nm -a) — Cadence-XRP API names:
$ nm -a libnrt.so.2.31.24.0 | rg -ci 'xrp_|_xrp|xrp_queue|xrp_request|xrp_comm|cadence'   → 0
$ nm -a libncfw.so          | rg -ci 'xrp_|_xrp|xrp_queue|xrp_request|xrp_comm|cadence'   → 0
$ nm -a libnrtucode_extisa.so | rg -ci 'xrp_|_xrp|xrp_queue|xrp_request|xrp_comm|cadence' → 0

# strings — XRP / mailbox / remoteproc family:
$ strings -a libnrt.so.2.31.24.0 | rg -ci 'cadence|rpmsg|remoteproc|xtensa_remote|hw_mailbox|dsp_rpc' → 0
$ strings -a libnrt.so.2.31.24.0 | rg -ci 'xrp'                                                       → 3   ← see below
$ strings -a libncfw.so / libnrtucode_extisa.so | rg -ci 'xrp|cadence|rpmsg|remoteproc'               → 0,0
```

The **only** three `xrp` substring hits in the entire 123 MB `libnrt` are Linux **RxRPC
socket‑family constants** in a `getaddrinfo`/socket name table — categorically *not*
Cadence XRP:

```text
.rodata constant-name dump (line numbers from `strings -n`):
  473473: __USE_KERNEL_IPV6_DEFS 0      ← neighbour above
  473476: AF_RXRPC PF_RXRPC             ← "xrp" hit #1
  473479: MCAST_JOIN_GROUP 42           ← neighbour below
  476454: PF_RXRPC 33                   ← "xrp" hit #2
  476594: SOL_RXRPC 272                 ← "xrp" hit #3
```

> **GOTCHA.** The substring "rxrpc" matches a naive `rg xrp`. It is the kernel **AF_RXRPC**
> address family (constant `33`) and `SOL_RXRPC` socket option level (`272`), sitting
> inside a flat dump of `AF_*`/`PF_*`/`SOL_*`/`MCAST_*` constants. This is a libc/socket
> name table, **not** a DSP‑RPC framework. There is no `libxrp.so` dependency, no
> `xrp_run_command`, no `rpmsg`/`remoteproc` shim anywhere in the runtime `.so` set.

**Conclusion.** The host↔DSP transport is the bespoke **AL HAL** (Annapurna‑Labs UDMA/DGE
register schema) + Neuron **`tdrv`** queue layer (`/opt/workspace/KaenaRuntime/tdrv/*.c`,
verbatim source strings recovered below). §§2–6 decode it.

---

## 2. The general transport — `hw_exec_queue` (AL/UDMA descriptor ring)  [HIGH / OBSERVED]

This is the **primary** host→device command path for inference / exec / model‑switch and
for collective DMA legs. Source‑file string (verbatim, `.rodata`):
`/opt/workspace/KaenaRuntime/tdrv/hw_exec_queue.c`. The full descriptor framing
(TX=M2S/`TDRTP_inc`, RX=S2M/`RDRTP_inc`, the two‑`al_udma_m2m_alloc_dma_packet` build) is
decoded in the committed sibling [`host‑device‑descriptor‑handoff`](../../runtime/host-device-descriptor-handoff.md);
this page only adds the **doorbell** and the **completion baking**.

### 2.1 Function roster (ELF symtab, OBSERVED addresses)

| symbol | addr | role |
|---|---|---|
| `hw_exec_queue_init` | `0x3201c0` | ring init |
| `hw_exec_queue_add_descriptors` | `0x3206f0` | the ring push |
| `hw_exec_queue_add_exec_request` | `0x321420` | public enqueue |
| `hw_exec_queue_add_exec_request_impl` | `0x320810` | builds the descriptors |
| `hw_exec_queue_add_model_stop_request` | `0x321660` | model‑stop |
| `hw_exec_queue_add_halt_request` | `0x321690` | halt |
| `kbl_acquire/release_hw_exec_queue_lock` | `0x308d90`/`0x308e00` | mutex |
| `dlr_add_to_hw_exec_queue` | `0xdd820` | model‑level entry |
| `notification_read_exec_queue` | `0x2ff170` | completion read (§4) |

### 2.2 The ring push — `hw_exec_queue_add_descriptors` @`0x3206f0`  [HIGH / OBSERVED]

The ctx embeds an AL SW‑DMA queue at `+0x8` and the ring base/length in `ctx+0x150` /
`ctx+0x158` (both must be non‑NULL):

```asm
320701:  mov  0x158(%rax),%r15        ; ring2  (ctx+0x158)
320708:  mov  0x150(%rax),%r10        ; ring   (ctx+0x150)
...
32074b:  call sw_dma_queue_reserve_descriptors   ; @0x448ce0  reserve N slots
...
320788:  call sw_dma_queue_set_descriptors       ; @0x448d30  → dma_ring_copy_descriptors @0x22eca0
```

### 2.3 The descriptors + completion baking — `…_add_exec_request_impl` @`0x320810`  [HIGH / OBSERVED]

IDA call‑graph for `0x320810` (callees, verbatim): the request is a **set** of UDMA M2M
copy descriptors whose completion‑event addresses are *baked into the descriptor stream*:

```text
al_udma_m2m_build_copy_descriptor   @0x45cca0   build the 16-B AL copy descriptor(s)
dmem_buf_copyin                     @0x229820   stage payload into device DMEM
tdrv_arch_get_evt_accel_addr        @0x309ef0   EVT-accel (SBUF) event address
tdrv_arch_get_evt_addr              @0x309f50   EVT_SEM semaphore address
tdrv_sync_get_hw_exec_queue_request_load @0x30aab0  reserved-sema slot for this queue
get_dma_queue_tail_inc_offset       @0x318940   the tail-pointer-INC register offset
```

The exact 16‑byte AL copy‑descriptor byte layout is `al_udma_m2m_build_copy_descriptor`
territory (ABI‑12); here we observe the *callees*, not the descriptor word layout.

### 2.4 The doorbell — `al_udma_desc_action_add` @`0x461f60`  [HIGH / OBSERVED]

The whole UDMA "ring the queue" primitive is a **single 32‑bit tail‑count write** at
`udma_q_regs + 0x38`, preceded by a producer‑ordering fence and a bounds check:

```asm
461f74:  cmp   %ebp,0x6c(%rbx)            ; if (num_descs > q->max@+0x6c) ...
461fae:  call  al_hal_log                 ;   ... log + abort
461fb8:  call  al_abort_program
461fbf:  mov   0x8(%rbx),%rbx             ; udma_regs = q[+0x08]
461fc3:  call  al_local_data_memory_barrier   ; @0x265940  THE PRODUCER FENCE
461fc8:  mov   %ebp,%esi                  ; esi = num_descs
461fca:  add   $0x38,%rbx                 ; rdi = udma_regs + 0x38   THE TAIL REG
461fd1:  call  al_reg_write32             ; @0x265c50  al_reg_write32(udma_regs+0x38, num_descs)
```

```c
/* al_udma_desc_action_add — reconstructed, names/offsets are real */
void al_udma_desc_action_add(al_udma_q *q, uint32_t num_descs) {
    if (num_descs > q->max /* +0x6c */) { al_hal_log(...); al_abort_program(); }
    void *udma_regs = q->regs /* +0x08 */;
    al_local_data_memory_barrier();                       /* payload visible before index advance */
    al_reg_write32(udma_regs + 0x38, num_descs);          /* the doorbell: tail-count write */
}
```

> **NOTE.** `+0x38` is the per‑queue **tail‑pointer‑inc** register inside the UDMA queue
> register bank. The committed [`rdma‑cross‑die`](../../dma/rdma-cross-die.md) page records
> the same `+0x038` doorbell with the full per‑queue address formula
> `0x1000 + q*0x1000 + 0x038` (16 M2S queues mirrored by 16 S2M).

### 2.5 The UDMA tail‑inc register offset (per‑arch HAL)  [HIGH / OBSERVED]

`get_dma_queue_tail_inc_offset` @`0x318940` resolves the per‑arch offset. The **CAYMAN
(NC‑v3)** getter computes it relative to the queue bank:

```asm
aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset_cayman @0x473bf0:
  473bfb:  mov  $0x1038,%edx
  473c04:  sub  %rax,%rdx          ; offset = 0x1038 - m2s_queue_offset
aws_hal_udma_get_s2m_queue_tail_ptr_inc_offset_cayman @0x473c10:  identical, s2m
```

`head_ptr`/`tail_ptr`/`tail_ptr_inc`/`data_tail_ptr_inc` getters exist for m2s & s2m across
`cayman`/`mariana`/`sunda`. The NX‑core's own register view is named verbatim in the
`.rodata` log strings:
`sunda_tpb_nx_local_reg_tpb_nx_local_regs_dma_tx_ring_{base,length,head_ptr,tail_ptr,tail_inc_ptr}`
plus the `dma_rx_ring_*` mirror — i.e. the DGE NX core hosts a **TX** (host→dev) and **RX**
(dev→host) descriptor ring whose base/length/head/tail/tail_inc the host programs.

### 2.6 The inference kickoff — `exec_kickoff_infer` @`0x2632e0`  [HIGH / OBSERVED]

On the inference‑start path the doorbell is a **semaphore increment routed through the
kernel driver**, not a userspace MMIO write:

```asm
2632f9:  call tdrv_sync_get_inference_start   ; @0x30a5c0  reserved "inference start" sema idx
2632fe:  mov  0x4(%rbx),%esi                  ; nc id
263314:  call ndl_nc_semaphore_increment      ; @0xc3ba0
```

`ndl_nc_semaphore_increment` @`0xc3ba0` issues a `/dev/neuron` ioctl:

```asm
c3bb9:  mov  $0x80084e29,%esi    ; ioctl request code
c3bc2:  call ioctl@plt
```

So the exec‑start doorbell rides the **EVT_SEM substrate via ioctl `0x80084e29`** (kin
codes `0x80084e2a`, `0xc0084e2b`).

---

## 3. The Q7 command queue — `xt_cc` (the JPEG‑codec transport)  [HIGH / OBSERVED]

`xt_cc` = the Xtensa **"CC"** (codec/compute) core = the **Q7**. `NUM_XT_CC_Q7 4`
(verbatim string). Source: `/opt/workspace/KaenaRuntime/tdrv/xt_cc.c` + `aws_hal_xt_cc.c`
(both verbatim). In this build the queue carries exactly **one** op — `PseudoJpegDecode`.

### 3.1 The embedded Q7 firmware blobs (`.rodata`, sizes read this session)  [HIGH / OBSERVED]

The Q7 device firmware ships embedded in `libnrt` as named blobs (size word precedes each
blob; values read from `.rodata`, VMA==fileoffset):

| blob symbol | data addr | size symbol | **size (read)** |
|---|---|---|---|
| `v4_q7_xt_cc_iram_bin` | `0x86b0a0` | `0x86b080` | `0x19a0` = **6560 B** |
| `v4_q7_xt_cc_dram_bin` | `0x86ca60` | `0x86ca40` | `0x0400` = **1024 B** |
| `v4_plus_q7_xt_cc_iram_bin` | `0x896520` | `0x896500` | `0x19a0` = **6560 B** |
| `v4_plus_q7_xt_cc_dram_bin` | `0x897ee0` | `0x897ec0` | `0x0400` = **1024 B** |
| `v3_q7_xt_cc_iram_bin` | `0x9b7060` | `0x9b7040` | `0x19a0` = **6560 B** |
| `v3_q7_xt_cc_dram_bin` | `0x9b8a20` | `0x9b8a00` | `0x0400` = **1024 B** |

i.e. per‑arch v3/v4/v4_plus Q7 codec images, **6.5 KiB IRAM + 1 KiB DRAM** each.
`xt_cc_init` @`0x314fb0` loads them to **HBM `0x12_0000_0000`**:

```asm
314feb:  movabs $0x1200000000,%rcx        ; HBM target for the Q7 ucode
31500b:  call   ucode_set_q7_ucode_bins   ; @0x2261e0
315019:  call   aws_hal_xt_cc_init        ; @0x44bae0
```

### 3.2 The HAL firmware load + queue init  [HIGH / OBSERVED]

| HAL fn | addr | action |
|---|---|---|
| `aws_hal_xt_cc_init` | `0x44bae0` | validates `0x20`‑byte handle → `aws_hal_q7_ucode_eng_init` @`0x451080` (loads Q7 IRAM/DRAM via vtable `kaena_khal.khal_q7.ucode_eng_init`) |
| `aws_hal_xt_cc_queue_init` | `0x44bb70` | programs ring size + start_addr |
| `aws_hal_xt_cc_top_start` | `0x44bbc0` | tail‑calls `aws_reg_write_xt_cc_queue_tail` (the doorbell) |
| `aws_hal_xt_cc_release_run_stall` | `0x44bac0` | writes `q7_release_run_stall` to start the Q7 executing |

### 3.3 The `xt_cc` queue register block — BYTE‑EXACT (4 queues q0..q3)  [HIGH / OBSERVED]

Recovered from the per‑queue `lea` offsets off reg‑block base `%rbx` in
`aws_reg_write_xt_cc_queue_start_addr` @`0x44f880` and `aws_reg_write_xt_cc_queue_tail`
@`0x44f980`:

| register | q0 | q1 | q2 | q3 | width | role |
|---|---|---|---|---|---|---|
| `size_in_entries` | `+0x00` | `+0x00` | `+0x00` | `+0x00` | 32b | written at the caller‑passed reg ptr |
| `start_addr` **LO** | `+0x04` | `+0x0c` | `+0x14` | `+0x1c` | 32b | ring base low 32 |
| `start_addr` **HI** | `+0x08` | `+0x10` | `+0x18` | `+0x20` | 32b | ring base high 32 (`shr $0x20`) |
| `tail` **(DOORBELL)** | `+0x24` | `+0x28` | `+0x2c` | `+0x30` | 32b | producer tail write |

```asm
aws_reg_write_xt_cc_queue_start_addr @0x44f880  (q3 arm shown):
  44f88d:  call al_hal_tpb_get_arch_type
  44f892:  cmp  $0x2,%eax ; je 44f8dd (ret)     ; arch 2: no xt_cc engine → no-op
  44f897:  jb   …assert                          ; arch <2: __assert_fail (xt_cc.c:0x3da)
  44f89d:  cmp  $0x4,%eax ; ja …assert           ; arch >4: __assert_fail (xt_cc.c:0x3de)
  44f8be:  lea  0x1c(%rbx),%rdi ; call al_reg_write32   ; q3 start_addr LO @+0x1c
  44f8d4:  shr  $0x20,%rsi ; lea 0x20(%rbx) ; …          ; q3 start_addr HI @+0x20
aws_reg_write_xt_cc_queue_tail @0x44f980:
  44f9b1:  lea  0x30(%rbx),%rdi   ; q3 tail @+0x30
  44f9c5:  lea  0x2c(%rbx),%rdi   ; q2 tail @+0x2c
  44f9d4:  lea  0x24(%rbx),%rdi   ; q0 tail @+0x24
  44f9e3:  lea  0x28(%rbx),%rdi   ; q1 tail @+0x28
```

> **QUIRK.** The arch dispatch is `al_hal_tpb_get_arch_type ∈ {2 → ret no‑op, 3/4 → write,
> else → __assert_fail}`. **Arch 2 has no Q7/`xt_cc` engine** — the writes silently
> return. The 64‑bit ring base is split into the LO/HI register pair via `shr $0x20`.

### 3.4 The host enqueue — `xt_cc_queue_init` @`0x3150d0`  [HIGH / OBSERVED]

```asm
3150fa:  lea  0x0(%r13,%r13,2),%rax    ; rax = max_req*3
3150ff:  push $0x0
315104:  push $0x11                    ; usage_type 0x11 = DMA_MEM_USAGE_TYPE_XT_CC
315106:  lea  0x0(%r13,%rax,4),%r14    ; r14 = max_req + max_req*12 = max_req*13
315111:  shl  $0x2,%r14                ; r14 = max_req*13*4 = max_req*0x34  (alloc size, BYTES)
315115:  mov  $0x1,%ecx               ; align = 1
315120:  call dmem_alloc               ; @0x228ed0  alloc ring in DEVICE DMEM
...
31517f:  mov  %r15d,0x8(%rbx)          ; ctx+0x08 = queue_id
315186:  mov  %r13,0x10(%rbx)          ; ctx+0x10 = max_requests
315191:  movl $0x0,0x18(%rbx)          ; ctx+0x18 = 0  (producer count / next-slot index)
315198:  call aws_hal_xt_cc_queue_init
```

> **CORRECTION (vs SX‑CCL‑13 §3d).** The backing report states the `xt_cc` ring is allocated
> at **`max_req * 0x28`** (40 B/slot). The binary computes **`max_req * 0x34`** (52 B/slot):
> `max_req*13*4`. The DMEM ring **over‑provisions to 52 B/slot**, while the *request record
> actually copied in* is 40 B (`0x28`, §3.5). The `0x28` figure is the **copyin length**,
> not the per‑slot allocation stride. Both are real and distinct.

### 3.5 The 40‑byte request record — `xt_cc_queue_add_request` @`0x315230`  [HIGH / OBSERVED]

```asm
315235:  mov  $0xffffffff,%edx
315240:  cmp  %rcx,%rdx ; jb …          ; assert arg2(jpeg_size) <= UINT32_MAX   "jpeg_size <= UINT32_MAX"
315249:  cmp  %r8,%rdx  ; jb …          ; assert arg3(rgb_size)  <= UINT32_MAX   "rgb_size <= UINT32_MAX"
315252:  mov  0x18(%rdi),%r10d          ; count = ctx+0x18
315259:  mov  0x10(%rdi),%rdi           ; max   = ctx+0x10
31525d:  cmp  %rdi,%r10 ; jae 315318    ; if (count >= max) → log "queue is full" + RET 2
; --- build the 40-byte record at [rsp+0x10] ---
3152a9:  movl $0x1,0x10(%rsp)           ; rec[+0x00] u32 = 1           (type/valid tag)
3152b1:  mov  %r10d,0x14(%rsp)          ; rec[+0x04] u32 = count       (slot index)
31527c:  mov  %rsi,0x18(%rsp)           ; rec[+0x08] u64 = arg0 = jpeg_addr
31528c:  mov  %rax,0x20(%rsp)           ; rec[+0x10] u64 = arg1 = rgb_addr   (rax = arg1)
315298:  mov  %ecx,0x28(%rsp)           ; rec[+0x18] u32 = arg2 = jpeg_size
3152a1:  mov  %r8d,0x2c(%rsp)           ; rec[+0x1c] u32 = arg3 = rgb_size
315266:  mov  %r9,(%rsp)                ; arg4 flag (extra word, pushed)
; --- copy the record into the device DMEM ring + advance producer index ---
3152c4:  lea  0x10(%rsp),%rsi           ; src = &record
3152c9:  mov  $0x28,%ecx                ; len = 0x28 = 40 bytes      (the request record)
3152ce:  lea  (%rax,%rax,4),%rdx        ; off = count*5   (count = ctx+0x18, DMEM-word units)
3152d6:  call dmem_buf_copyin           ; @0x229820  copyin(ctx[0], &record, count*5, 0x28)
3152f8:  add  $0x1,%eax                 ; count + 1
3152fb:  mov  %eax,0x18(%rbx)           ; ctx+0x18 = count + 1   (advance producer index)
315346:  mov  $0x2,%eax                 ; queue-full return path → RET 2
```

```c
/* The xt_cc request record — 40 B copied, struct-exact */
struct xt_cc_request {        /* offset  size  field            (= JPEG semantics)   */
    uint32_t valid;           /* +0x00   4     type/valid tag    (= 1)                */
    uint32_t slot;            /* +0x04   4     slot index        (= producer count)   */
    uint64_t arg0;            /* +0x08   8     jpeg_addr                               */
    uint64_t arg1;            /* +0x10   8     rgb_addr                                */
    uint32_t arg2;            /* +0x18   4     jpeg_size  (asserted <= UINT32_MAX)     */
    uint32_t arg3;            /* +0x1c   4     rgb_size   (asserted <= UINT32_MAX)     */
};                            /* copied as len 0x28 (40 B) incl. the trailing flag    */
```

### 3.6 The caller + JPEG semantics — `translate_one_pseudo_instr_v3` @`0x322200`  [HIGH / OBSERVED]

The `PseudoJpegDecode` pseudo‑op resolves the five args (`rsi=jpeg_addr`, `rdx=rgb_addr`,
`rcx=jpeg memref[0x10]=jpeg_size`, `r8=rgb memref[0x10]=rgb_size`, `r9=flag`) via
`mem_ref_to_addr`. Corroborating `.rodata` strings: `PseudoJpegDecode`,
`aws_neuron_isa_tpb_pseudo_jpeg_decode.h`, `DBGINFO: rgb_addr = 0x%lx`,
`jpeg_size <= UINT32_MAX`, `rgb_size <= UINT32_MAX`. Each `PseudoJpegDecode` posts a 40‑byte
record into the Q7 DMEM ring and rings the per‑Q7 tail doorbell; the Q7 codec firmware
decodes the JPEG into the RGB buffer.

> **NOTE.** The trace‑event/CSR table names the four per‑Q7 doorbells:
> `AWS_NEURON_CC_TOP_Q7_0_JPEG_CMD_QUEUE_TAIL 9`, `…_Q7_1_… 10`, `…_Q7_2_… 11`
> (and `…_Q7_3_… 12`) — CSRs **9..12**, one per Q7, matching `NUM_XT_CC_Q7 4`.

---

## 4. The completion / response path — semaphores + Notification Queue  [HIGH / OBSERVED]

The device signals completion two complementary ways, both on the **EVT_SEM / NQ**
substrate.

### 4.1 Reserved completion semaphores + the EVT_SEM windows  [HIGH / OBSERVED]

The host pre‑allocates reserved semaphore slots and bakes their **address** into the request
descriptors (`tdrv_arch_get_evt_addr` / `…_accel_addr`, §2.3) so the device increments them
on done. The reserved‑slot roster is the `tdrv_sync_get_*` family (each reads a reserved
index from `tdrv_arch_ops`):

| symbol | addr | role |
|---|---|---|
| `tdrv_sync_get_inference_start` | `0x30a5c0` | exec kickoff sema (§2.6) |
| `tdrv_sync_get_hw_exec_queue_request_load` | `0x30aab0` | hw_exec_queue load sema |
| `tdrv_sync_get_collective_topsp_ack_first` | `0x30ab70` | TOP_SP collective ACK (leader) |
| `tdrv_sync_get_collective_topsp_ack_last` | `0x30abd0` | TOP_SP collective ACK (last) |
| `tdrv_sync_get_num_reserved_semaphores` | `0x30acf0` | reserved‑pool count |

The `…collective_topsp_ack_*` pair is the substrate behind the verbatim string
`[nec_dev %u] received clearance from top_sp to execute enc_barrier` — the TOP_SP posts
these on collective completion. The host then **waits** on the reserved sema
(`nrt_async_sema_wait` / `NrtAsyncSemaWait` trace event).

The completion semaphores live in the per‑engine **EVT_SEM** CSR aperture, whose four
sub‑windows are recovered byte‑exact (CAYMAN_TPB_4 shown; relative bases):

| window | rel‑base | size | role |
|---|---|---|---|
| `…SEMAPHORE_READ` | `0x1000` | `0x400` | atomic read |
| `…SEMAPHORE_SET` | `0x1400` | `0x400` | absolute set |
| `…SEMAPHORE_INC` | `0x1800` | `0x400` | atomic increment (the completion path) |
| `…SEMAPHORE_DEC` | `0x1c00` | `0x400` | atomic decrement |

Each window is `0x400` = 1024 B = **256 semaphores × 4 B** → **ArraySize 256** — matching the
committed [`rdma‑cross‑die`](../../dma/rdma-cross-die.md) EVT_SEM model (read@`0x1000` /
set@`0x1400` / inc@`0x1800` / dec@`0x1c00`).

### 4.2 The Notification Queue (NQ) — device→host event ring  [HIGH / OBSERVED]

`notification_read_exec_queue` @`0x2ff170` selects an NQ slot and reads it via
`aws_hal_notific_nq_read` @`0x451040`, which dispatches through the `kaena_khal` vtable slot
`+0x438`:

```asm
2ff19c:  lea  (%rdx,%rdx,4),%rax        ; rax = idx*5
2ff1a0:  lea  (%rdx,%rax,2),%rax        ; rax = idx + idx*5*2 = idx*11
2ff1a7:  shl  $0x5,%rax                 ; rax = idx*11*32 = idx*0x160   (352 B stride)
2ff1ab:  lea  0x210(%rdi,%rax,1),%rdi   ; ptr = nq_base + 0x210 + idx*0x160
2ff1b3:  call aws_hal_notific_nq_read   ; @0x451040
aws_hal_notific_nq_read @0x451040:
  45104b:  jmp  *0x438(%rax)             ; per-arch nq_read via kaena_khal vtable +0x438
```

> **CORRECTION (vs SX‑CCL‑13 §3 TL;DR / §4b).** The backing report states the NQ slot stride
> is **`0xa0` (160 B)**. The disassembly proves the stride is **`0x160` (352 B)**:
> `idx*5 → idx*11 → <<5 = idx*352`, with the array based at `nq_base + 0x210`. Use **`0x160`**.
> (The `+0x210` base and the vtable `+0x438` dispatch in the report are correct.)

Host "consume" entry points — the **collective / CC‑core** completion drain:
`consume_ready_exec_notification_v2 @0x2fcce0`, `notification_consume_errors @0x300350`
(+ `notification_consume_error_block @0x2ff250`), and the profile-side
`nrt_profile_session_append_cc_notifications @0xaf700`. The full NOTIFIC CSR schema
belongs to the Part‑13 NOTIFIC‑Queue page (`../../control/csr/notific-queue.md`, stub);
this page links it by path.

> **CORRECTION — there is no `exec_consume_cc_core_notifications` (nor
> `exec_consume_nc_status_notifications` / `exec_consume_gpsimd_stdio`) symbol in
> `libnrt.so`.** Verified absent this pass (`nm | rg -c` = 0 for each). The real
> CC-notification consumers are `consume_ready_exec_notification_v2 @0x2fcce0`,
> `notification_consume_errors @0x300350`, and `exec_request_process_errors.isra.0
> @0x2615b0` (the per-TOP_SP count/type validator). This matches the standing
> correction in
> [ring-protocol-config-command](../ncfw/ring-protocol-config-command.md); cite
> these, not a non-existent consume symbol. `[OBSERVED HIGH — symbol presence/absence
> via `nm` over `libnrt.so`.]`

### 4.3 The device‑side ring abstraction (`libnrtucode_extisa`)  [HIGH strings / INFERRED loop]

The POOL/Q7 ucode tracks a DRAM producer/consumer ring named **`DramRingDMA`** with explicit
submit/comp tickets (verbatim format strings):

```text
DramRingDMA::allocate(%zu) submit=%llu comp=%llu first_slot=%zu min_comp=%llu
submit_pending: first_slot=%zu count=%zu submit=%llu ring_id=%u
  submitted, ticket=%lld new submit=%lld
drain_completions(%zu): comp=%llu submit=%llu drain_slot=%zu
```

The device maintains a monotonic **submit** counter and a **comp(letion)** counter; a slot is
free when `comp` catches up to `submit`; `drain_completions` advances `comp`. This is the
consumer‑side mirror of the host producer ring (§§2–3). The field *semantics* are OBSERVED;
the on‑core ticket arithmetic runs on Xtensa → **INFERRED‑STRONG**.

---

## 5. The shared‑memory ring / synchronization model  [HIGH host / INFERRED device]

The bespoke transport is a classic producer/consumer shared‑memory ring — **not** a
mailbox‑doorbell RPC, **not** Cadence XRP:

```c
/* PRODUCER (host) */
reserve_slot();                         /* sw_dma_queue_reserve_descriptors  OR  xt_cc count<max */
write_payload_into_device_ring();       /* dma_ring_copy_descriptors (UDMA)  OR  dmem_buf_copyin (DMEM) */
al_local_data_memory_barrier();         /* FENCE before the doorbell (UDMA path)  @0x265940 */
ring_doorbell();                        /* see table below */
record_producer_index();                /* xt_cc: ctx+0x18 ;  UDMA: tail-ptr register */

/* CONSUMER (device Xtensa: NX-DGE / Q7 / TOP_SP) — INFERRED */
poll_head(); execute(); inc_reserved_evt_sem(); push_nq_entry();

/* COMPLETION (host) */
nrt_async_sema_wait(reserved_sema);     /* and/or */
notification_read_exec_queue() => aws_hal_notific_nq_read();   /* exec_consume_* */
```

| path | doorbell primitive | ring location |
|---|---|---|
| `hw_exec_queue` | `al_reg_write32(udma_q + 0x38, num_descs)` (after barrier) | UDMA M2S/S2M descriptor ring (NX‑local DGE `dma_tx_ring_*` / `dma_rx_ring_*`) |
| `xt_cc` | `aws_reg_write_xt_cc_queue_tail(reg, val, q)` → `+0x24/+0x28/+0x2c/+0x30` | device **DMEM**, `dmem_alloc(usage 0x11)`, base in start_addr regs |
| inference start | `ndl_nc_semaphore_increment` (EVT_SEM via `/dev/neuron` ioctl `0x80084e29`) | — (sema only) |

Ordering: `al_local_data_memory_barrier` before the UDMA doorbell (OBSERVED); the
EVT_SEM increment‑then‑wait gives the cross‑engine happens‑before.

---

## 6. The host‑side async‑exec worker pool (NOT a host↔DSP transport)  [HIGH / OBSERVED]

Sitting **above** the device queues is a host‑CPU threadpool the trace taxonomy calls
`AsyncPostRequest` / `async_post_request`. It is **host‑internal** (pthreads + POSIX
semaphores + `std::queue<kmgr_async_exec_req*>`), source `kmgr_async_exec.cc`:

| symbol (demangled) | addr |
|---|---|
| `kmgr_async_exec_init` | `0xe7020` |
| `kmgr_async_exec_add_work` | `0xe6d20` |
| `kmgr_async_exec_poll` | `0xe6ab0` |
| `kmgr_async_exec_destroy` | `0xe6cd0` |
| `kmgr_exec_worker_do_work` | `0xe6310` |
| `kaew_post_request` (file‑local) | `0xe5cd0` |

`kaew_post_request` @`0xe5cd0` does backpressure + enqueue, then the worker thread drives the
device:

```asm
e5d07:  call sem_getvalue@plt          ; available-slots sema (worker+0x50) — "available_request_slots"
e5d7d:  call sem_wait@plt              ; backpressure on inflight-limit sema (worker+0x30)
e5d85:  call pthread_mutex_lock@plt    ; worker queue lock
…       push kmgr_async_exec_req into std::queue
e5dd3:  call dlr_add_to_hw_exec_queue  ; @0xdd820  → kbl_infer_kickoff → exec_kickoff_infer (§2.6)
```

So `AsyncPostRequest` is the host request‑**dispatcher**; the actual host↔DSP boundary is the
UDMA/`xt_cc` rings + EVT_SEM/NQ of §§2–5. Calling the worker pool "the XRP transport" would be
a category error.

A **separate** host‑CPU collectives path exists (`hostcc_build_context` @`0x2666d0`,
`hostcc_load` @`0x2669d0`, `hostcc_exec` @`0x266f30`, `hostcc_wait_completion` @`0x267050`) —
a host‑driven collective (e.g. over the host NIC/EFA fabric), a *peer* of the device TOP_SP
sequencer, not its transport.

---

## 7. Transport‑sharing — collective vs custom‑op vs JPEG  [HIGH / OBSERVED]

The corpus uses **parallel** bespoke command transports, unified only by the EVT_SEM + NQ
**completion** substrate. There is **no** single universal "XRP message header."

| op class | command path | completion |
|---|---|---|
| **Collectives** | general `hw_exec_queue` DMA legs **+** the TOP_SP `host_trigger` doorbell (`kaena_khal.khal_sp.topsp_set_host_trigger`; the SP local‑reg trigger). The SPAD cc_op program is HBM‑staged + DMA‑loaded onto TOP_SP (**engine 5**), **not** posted via `xt_cc`. | `tdrv_sync_get_collective_topsp_ack_first/_last` + NQ (`consume_ready_exec_notification_v2 @0x2fcce0` / `notification_consume_errors @0x300350`) |
| **Custom‑op** | **device‑local**: TPB custom‑op **instruction** arg interface (`rd_args_from_insns`) consumed on the Q7/POOL core + `respond(TPB_WRITE_RESPONSE)`; launched by on‑device `switch_stack_or_call_wrapper`. **No** host `xt_cc`/`hw_exec_queue` enqueue. | `respond` handshake (device‑local) |
| **JPEG decode** | the **sole** rider of the `xt_cc` Q7 command queue (§3): 40‑B record → Q7 DMEM ring → per‑Q7 `JPEG_CMD_QUEUE_TAIL` doorbell (CSRs 9..12) | EVT_SEM + NQ |

> **NOTE.** The TOP_SP collective trigger path (the `kaena_khal` SP vtable host‑trigger and
> the SP local‑reg doorbell) is decoded structurally in
> [`top‑sp‑lowering`](top-sp-lowering.md); the binary evidence cited here is the verbatim
> vtable string `kaena_khal.khal_sp.topsp_set_host_trigger`,
> `topsp_get_host_trigger_reg_offset`, and the `enc_get_topsp_trigger_addrs` prototype —
> independent of that page's prose. The NCFW management‑core command schema is
> [`ring‑protocol‑config‑command`](../ncfw/ring-protocol-config-command.md) territory; this
> page found **no** separate NCFW host command queue distinct from `hw_exec_queue` — NCFW
> collective context is staged via the collective `hw_exec_queue` + a `topsp_init` DMA.

**Unifying substrate.** All three completion paths converge on the EVT_SEM reserved
semaphores + the Notification Queue. That is the closest thing to a "universal" layer — but
it is a **semaphore/event** substrate, not a message transport. See the
[`architecture‑synthesis`](architecture-synthesis.md) overview for how these queues compose.

---

## 8. Confidence / OBSERVED‑vs‑INFERRED ledger

**HIGH / OBSERVED** (byte‑exact disasm / register immediate / verbatim string / `nm`):

- **XRP absence**: zero `xrp_`/`libxrp`/`rpmsg`/`remoteproc`/`cadence` symbols or strings in
  `libnrt`/`libncfw`/`libnrtucode_extisa`; the only three `xrp` hits are `AF_RXRPC`/
  `PF_RXRPC 33`/`SOL_RXRPC 272` socket constants.
- `hw_exec_queue` roster; `add_descriptors` (ctx `+0x150`/`+0x158`, reserve/set descriptors);
  `add_exec_request_impl` callees (`al_udma_m2m_build_copy_descriptor`, `dmem_buf_copyin`,
  `tdrv_arch_get_evt_addr/_accel_addr`, `get_dma_queue_tail_inc_offset`,
  `tdrv_sync_get_hw_exec_queue_request_load`); the `al_udma_desc_action_add` doorbell
  (`barrier` + `al_reg_write32(udma_q+0x38, num_descs)`, bound `+0x6c`); the CAYMAN
  tail‑inc offset `0x1038 − q_offset`.
- `exec_kickoff_infer` → `tdrv_sync_get_inference_start` + `ndl_nc_semaphore_increment`
  (ioctl `0x80084e29`).
- `xt_cc`: `NUM_XT_CC_Q7 4`; embedded v3/v4/v4_plus blobs (IRAM `0x19a0`, DRAM `0x400`);
  `xt_cc_init` HBM target `0x12_0000_0000`; the register block (size `+0x00`, start_addr
  LO/HI `+0x04..+0x20`, tail `+0x24..+0x30`); arch dispatch (2→noop, 3/4→write, else assert).
- `xt_cc_queue_init` (`dmem_alloc` usage `0x11`, **alloc stride `0x34`**, ctx `+0x08` qid /
  `+0x10` max / `+0x18` producer‑count); `xt_cc_queue_add_request` (40‑B record, copyin
  `len 0x28` at `off count*5`, producer‑index++, UINT32_MAX asserts, queue‑full RET 2).
- The four EVT_SEM windows (READ `0x1000`/SET `0x1400`/INC `0x1800`/DEC `0x1c00`, each
  `0x400`=256 semaphores); the `tdrv_sync_get_*` reserved‑sema roster; the NQ read path
  (`notification_read_exec_queue`, **stride `0x160`** at base `+0x210`,
  `aws_hal_notific_nq_read` via vtable `+0x438`); the `exec_consume_*` trace events.
- `DramRingDMA` submit/comp/ticket/drain device‑ring strings (`extisa`).
- `kmgr_async_exec` worker pool (sems, mutex, `std::queue`, `AsyncPostRequest`);
  `kaew_post_request` → `dlr_add_to_hw_exec_queue`. Separate `hostcc_*` host‑CPU collectives.

**MED / INFERRED‑STRONG** (multiple OBSERVED facts converge):

- The **device consumer loop** (Q7/NX poll the ring, execute, post completion) runs on the
  Xtensa cores (no x86 disasm); inferred from the host producer side + the `DramRingDMA`
  submit/comp strings.
- The exact 16‑byte AL UDMA copy‑descriptor word layout (built by
  `al_udma_m2m_build_copy_descriptor`; not byte‑dumped here — ABI‑12 territory).

**LOW / OPEN — boundaries to sibling pages:**

- The NCFW management‑core command semantics →
  [`ring‑protocol‑config‑command`](../ncfw/ring-protocol-config-command.md).
- The device‑memory staging ABI (`dmem_buf_copyin` / `dmem_alloc` / `DMEM_GET_VA`) used by
  both transports → the 5‑slot memhandle table in
  [`nrtucode‑context`](../../runtime/nrtucode-context.md) /
  [`prelinker‑ucpl`](../../runtime/prelinker-ucpl.md).
- The full NOTIFIC CSR schema → NOTIFIC‑Queue page (`../../control/csr/notific-queue.md`, stub).
- The on‑core Q7 JPEG codec decode loop (inside the v3/v4 `q7_xt_cc` bins) — Xtensa,
  blob sizes only.
