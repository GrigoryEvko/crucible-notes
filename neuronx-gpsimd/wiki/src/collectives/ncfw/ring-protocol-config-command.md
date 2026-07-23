# NCFW Ring Send/Wait + Config Schema + Host Command

This page consolidates the three top layers of the NCFW collective control stack:

1. **The ring SEND / WAIT-COMPLETE descriptor protocol** — how a collective ring
   step *posts* its DMA descriptors (descriptor fill, `used_desc_count`, the
   M2S/S2M tail-pointer doorbell) and *detects* completion (the EVT_SEM wait, the
   completion-marker poll), plus the cross-ring handshake / flow control.
2. **The collective-config schema** — the `cc_op` record format byte-exact, the
   `algo_type → cc_op`-sequence expansion, the `barrier_configs` /
   `basic_block_configs` sub-schemas, the reduce/dtype encoding, and config
   versioning/validation.
3. **The NCFW ↔ host command/control protocol** — the command set
   (`nrt_cc_*` / `encd_*`), the message format, the submission path (the dedicated
   TOP_SP init-DMA + the LOCAL_REG doorbell quintet), the completion/status path
   (the per-TOP_SP CC-status NQ + the `NRT_STATUS` codes), and the NEFF
   basic-block STEP state machine.

These three layers fit together as one pipeline: the **host command** (§3) DMAs a
**config block** (§2) whose `cc_op` table is a program of **ring steps** (§1) that
the NX/LX cores execute.

> **Why the host side is fully recoverable, the device side is not.** The NCFW
> management core is a scalar Xtensa-LX control core for which no TIE disassembler
> config ships — only `ncore2gp` (the Vision-Q7 NX core) is registered, so ~26–28 %
> of NCFW code bytes mis-decode as Vision FLIX bundles (see
> [LX ISA naming synthesis](./lx-isa-naming-archid-synthesis.md)). The on-device
> case bodies that *walk* the `cc_op` table and *re-arm* the rings are therefore
> not reliably decodable. But everything documented here is recovered from the
> **host x86-64** encoders: the *builder* `libnrt.so` (full DWARF) and the
> *decoder* `libncfw.so` (the firmware-struct pretty-printer), whose field offsets
> mirror the device structs. The data-plane Q7 transport leg (`rdma_desc_gen/start`)
> is scalar-Q7 and *is* decodable; it is reconciled where relevant.

> **Binary provenance and verification rules.**
> `libnrt.so.2.31.24.0` — BuildID `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, ELF64
> x86-64, not stripped, 122 956 336 B, `.debug_info` present.
> `libncfw.so` — BuildID `a98f8e1ca2294582835310c3a1092e0a5e500db5`, 615 640 B.
> On both libraries `.text`/`.rodata` are **VMA == file-offset**; `libncfw.so`
> `.data` carries a **+0x1000** delta (VMA `0x95020` vs file `0x94020`, per
> `readelf -SW`), and `libnrt.so`/`libncfw.so` have **no** `.data` delta beyond
> their own section deltas. `libncfw.so` and `libnrtucode_internal.so` use plain
> **C function-pointer tables** (slot N = symbol + 8·N), *not* C++ `_ZTV+0x10`
> vtables. Every count below is grounded on `nm <lib> | rg -c`.

---

# Section 1 — The Ring SEND / WAIT-COMPLETE Descriptor Protocol

A collective ring **step** is a pair of half-operations built by the `libnrt`
`enc_primitive` layer and lowered into the persistent
[pring descriptor ring](./pring-descriptors.md). The **send** posts a `COPY` data
descriptor plus a trailing semaphore-increment descriptor and arms the DMA; the
**wait-complete** detects arrival either by an EVT_SEM `WAIT-GE` rendezvous
(remote) or by a completion-marker busy-poll (local). The two sides chain through
a 2-semaphore producer/consumer credit loop.

## 1.1 The send builder — `enc_primitive::direct_send` @0x174830

`enc_primitive::direct_send(enc_half_chunk_index)` @0x174830
(`_ZN13enc_primitive11direct_sendE20enc_half_chunk_index`) issues, per chunk-half,
a call census whose every target is a real `libnrt` symbol:

```c
// enc_primitive::direct_send(enc_half_chunk_index h)  @0x174830   [OBSERVED HIGH]
void direct_send(enc_half_chunk_index h) {
    // 1. resolve the LOCAL SOURCE SoC address from the SBUF/HBM page table
    __get_pgt_offset(pagetable_type, h, size, &soc_addr, &off, &len, &stride);
    // 2. record that src SoC addr into the descriptor (net path)
    __record_net_src_direct_addr(soc_addr, src, dst, n, flagA, flagB);
    __update_net_src_addr_complete(idx);
    // 3. BUILD the COPY descriptor(s) — the data move, KBIN_DMA_DESC_OP_COPY
    encd_dma_copy(/* … */);            // or encd_dma_copy_sb2sb for SBUF→SBUF
    //    a trailing KBIN_DMA_DESC_INC_SEMA completion descriptor is baked in
    //    by __encd_dma_copy via encd_arch_get_sp_sema_i_ofst (EVT_SEM +0x1800)
    // 4. SIGNAL the downstream peer (§1.4): increment the peer's recv_sema
    __post_send(sema_idx, neigh);      // @0x148a20
    // 5. advance the step cursor and rotate the double-buffer
    mark_step(/*flush=*/true);
    inc_dst_buf_idx();
}
```

The chunk is split into halves by `enc_half_chunk_index {CHUNK_H0=0, CHUNK_H1=1,
ENC_CHUNK_SPLIT_N=2}` (DWARF DIE `<60c2a1>`, const_values confirmed) — the
pipelining unit; `direct_send` is issued per half.
[Call census + DWARF enum OBSERVED HIGH.]

## 1.2 The descriptor fill — what lands in the pring

`encd_dma_copy_sb2sb @0x23ebb0 → __encd_dma_common_sb2sb @0x23e1b0 →
encd_dma_copy @0x23cf80 / __encd_dma_copy @0x238c80`. The terminal
`__encd_dma_copy` resolves the per-descriptor semaphore-increment target via
`encd_arch_get_sp_sema_i_ofst` (the EVT_SEM SEMAPHORE_INC window **+0x1800**) — so
the `COPY` descriptor ring is built with a trailing SEMAPHORE_INC op baked in. The
three pring descriptor **classes** (`libnrt` enum + assert strings):

| Class | Role | Anchor |
|-------|------|--------|
| `KBIN_DMA_DESC_DATA` | data-move (the chunk copy) | the `OP_*` opcode rides this |
| `KBIN_DMA_DESC_INC_SEMA` | semaphore-increment completion signal | assert `desc->desc_type == KBIN_DMA_DESC_INC_SEMA` @0x802340 |
| `KBIN_DMA_DESC_EVENT` | event-wait gate | `vring_add_desc_event` @0x3122b0 |

The data-op opcodes are `KBIN_DMA_DESC_OP_{COPY,ADD,MAX,MIN,FMA,TRANSPOSE,INVALID}`
— the in-flight CCE reduce op for reduce-scatter (see §2.4 for where the *reduce
operator* actually lives).

The `INC_SEMA` descriptor is appended by **`vring_add_desc_semaphore_inc`
@0x312310**, byte-verified to call (in order):

```
312329:  call  tdrv_arch_get_evt_accel_addr   @0x309ef0
312338:  call  tdrv_arch_get_sem_inc_addr     @0x309a60   ; [r14+0x4] = sema id
```

i.e. the descriptor's `buf_ptr` is an EVT_SEM SEMAPHORE_INC CSR address, and the
DMA performs an atomic `+=` to it when the data move ahead of it completes. The
`EVENT`/wait descriptor is `vring_add_desc_event @0x3122b0`
(`tdrv_arch_get_evt_addr`). [Both builders + the sema-inc CSR resolution OBSERVED HIGH.]

**`used_desc_count` update.** The vring tracks per-direction fill via
`tx.used_desc_count` / `rx.used_desc_count` (`dma_ring_info` +0x18 — see
[pring-descriptors §2](./pring-descriptors.md)); the assert
`seed_set_vring->tx.used_desc_count <= DMA_RING_MAX_SEED_SET_RING_DESC_COUNT &&
…rx.used_desc_count <= …` bounds them. When the vring is dumped to the pring,
`used_desc_count` is the live descriptor count and `allocated_desc_count` (+0x1c)
is the ring capacity. `dma_ring_info_alloc_from_memchunk @0x22d5f0` writes the
handle fields `[rbx+0x8]=ring_mem`, `[rbx+0x10]=ring_offset_bytes`,
`[rbx+0x1c]=allocated_desc_count`. [Field-writes + assert OBSERVED HIGH.]

## 1.3 The recv/wait builder — `enc_primitive::direct_recv` @0x158540

`enc_primitive::direct_recv(enc_half_chunk_index, bool, bool, bool, bool)`
@0x158540 mirrors `direct_send` for the receive leg:

```c
// enc_primitive::direct_recv(enc_half_chunk_index h, bool, bool, bool, bool)  @0x158540
void direct_recv(enc_half_chunk_index h, /*…*/) {
    __get_pgt_offset(/*…*/);                 // resolve LOCAL DESTINATION addr
    __record_net_dest_addr(soc_addr, src, dst, n);
    __update_net_dest_addr_complete(idx);
    encd_dma_copy(/*…*/);                     // or encd_dma_reduce_copy for a fold
    __post_recv(sema_idx);                    // @0x1494d0 — arm recv + RETURN CREDIT
    mark_step(/*flush=*/true);
    inc_src_buf_idx();
}
```

The **WAIT** itself is the EVT_SEM `WAIT-GE` on the channel `recv_sema` (the kring
"mine" semaphore for kangaring): `add_semaphore_wait_ge_and_dec @0x273a20`
(SEMAPHORE_READ poll `>= target`, then SEMAPHORE_DEC). The full semaphore-op set
(`libnrt`, all OBSERVED HIGH):

| Op | Symbol | EVT_SEM window |
|----|--------|----------------|
| INC (signal) | `add_semaphore_inc` @0x273860 | sema_i **+0x1800** |
| SET (init/reset) | `add_semaphore_set` @0x273950 | sema_s **+0x1400** |
| DEC (consume) | `add_semaphore_dec` @0x2738b0 | sema_d **+0x1C00** |
| WAIT-GE + DEC | `add_semaphore_wait_ge_and_dec` @0x273a20 | sema_r **+0x1000** then sema_d |
| WAIT-GE | `add_semaphore_wait_ge` @0x273ac0 | sema_r +0x1000 |
| WAIT-EQ | `add_semaphore_wait_eq` @0x273990 | sema_r +0x1000 |
| WAIT-EQ + INC | `add_semaphore_wait_eq_and_inc` @0x2739d0 | |
| WAIT-EQ + CLEAR | `add_semaphore_wait_eq_and_clear` @0x273a70 | |

These map onto the TOP_SP-hosted **EVT_SEM** windows, byte-verified in the cayman
offset getters:

```
cayman_get_sp_sema_r_ofst @0x25c130:  lea rax,[rax+rbx*4+0x1000]   ; READ  / WAIT
cayman_get_sp_sema_s_ofst @0x25c360:  lea rax,[rax+rbx*4+0x1400]   ; SET
cayman_get_sp_sema_i_ofst @0x25c2e0:  lea rax,[rax+rbx*4+0x1800]   ; INC   / signal
cayman_get_sp_sema_d_ofst @0x25c260:  lea rax,[rax+rbx*4+0x1c00]   ; DEC   / consume
```

The `rbx*4` index scale confirms a 4-byte stride; the array is 256 deep
(`NEURON_ISA_TPB_NUM_SEMAPHORES`, DWARF). The TOP_SP base is
`cayman_get_sp_base_addr @0x25aee0: movabs rax,0x8280000000`.

> **NOTE — EVT_SEM window quartet.** read@0x1000 / set@0x1400 / inc@0x1800 /
> dec@0x1C00, ArraySize 256, 4-byte stride, relative to the TOP_SP SP base
> `0x8280000000` (cayman). This is consistent with the shared EVT_SEM anchor and
> with [pring-descriptors](./pring-descriptors.md).

A validator enforces that **every** collective op posts a completion semaphore:
`instr_col_setup_psr @0x26df50` carries the assert *"Collective instruction must
increment semaphore when done"* @0x80f938. The
`pcb_translate_one_instrution_*` functions emit the per-rank inc+wait instruction
pairs; their asserts name the full handshake set verbatim — a **PEER** inc/wait,
a **NEXT-TPB** inc/wait, and a **PREV-TPB** inc/wait (the ring `next_neigh` /
`prev_neigh` + kring `peers[]` fanout expressed as semaphore inc(signal)/
wait(rendezvous) pairs). [Strings + enforcement OBSERVED HIGH.]

## 1.4 The per-peer signal — `__post_send` @0x148a20 (directions byte-exact)

```asm
; enc_primitive::__post_send(int sema_idx, encd_neigh)  @0x148a20   [OBSERVED HIGH]
148a20:  cmp QWORD PTR [rdi+0xc8],0x0      ; has a network connection?
148a2a:  je  148a80                        ;   no  -> LOCAL path
148a30:  cmp edx,0x1                        ; neigh type == 1 (net) ?
148a35:  cmp esi,0x1ff ; jg <__assert>      ; sema_idx bounded to 0x1ff (511)
148a41:  add ecx,[rdi+0x16c]                ; running net-index (SEND index)
148a55:  and ecx,0x1ff                       ; wrap &0x1ff (NET_INDEX_LOOP_SIZE)
148a69:  mov [rdi+0x16c],ecx
148a76:  jmp encd_dma_update_net_index      ; NET  -> bump net-index sema
148a9a:  jmp encd_dma_inc_recv_sema  @0x245590  ; LOCAL -> INC the PEER's recv_sema
```

`__post_send` **increments the peer's RECV semaphore** — the "data is on the way /
has arrived" signal to the downstream rank.

The mirror, `__post_recv` @0x1494d0, **increments the SEND semaphore** = the
returned **credit**:

```asm
; enc_primitive::__post_recv(int sema_idx)  @0x1494d0   [OBSERVED HIGH]
1494e4:  add ecx,[rdi+0x168]                ; running net-index (RECV index, distinct)
1494f8:  and ecx,0x1ff
14950c:  mov [rdi+0x168],ecx
149515:  jmp encd_dma_update_net_index      ; NET
14953c:  jmp encd_dma_inc_send_sema  @0x252bf0  ; LOCAL -> INC the *send* sema (credit)
```

> **GOTCHA — two distinct net-index fields.** `__post_send` uses `[rdi+0x16c]`
> (the SEND net-index); `__post_recv` uses `[rdi+0x168]` (the RECV net-index).
> Separate running indices, both wrapped `&0x1ff` (`NET_INDEX_LOOP_SIZE = 512`).
> Treating them as one field corrupts the credit accounting.

## 1.5 Arming the DMA — the M2S/S2M tail-pointer doorbell

The doorbell that **launches** the posted descriptors is a write of N (the
descriptor count) to the per-queue tail-pointer-**increment** register. Byte-
confirmed in the cayman HAL getters:

```
aws_hal_udma_get_m2s_queue_offset_cayman @0x473a90:
   lea eax,[rdi+0x1] ; shl eax,0xc          => queue_base = (q+1) << 12
aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset_cayman @0x473bf0:  mov edx,0x1038   ; TDRTP_inc
aws_hal_udma_get_s2m_queue_tail_ptr_inc_offset_cayman @0x473c10:  mov edx,0x1038   ; RDRTP_inc
aws_hal_udma_get_m2s_queue_data_tail_ptr_inc_offset_cayman @0x473c30: mov edx,0x10e0  ; TDRDTP_inc
aws_hal_udma_get_m2s_queue_sw_ctrl_offset_cayman @0x473c50:        mov edx,0x10b0   ; q_sw_ctrl
```

| Doorbell | Offset | Role |
|----------|--------|------|
| `TDRTP_inc` (M2S / TX) | queue_base **+0x38 = abs 0x1038** | write N → advance the M2S tail by N (launch outbound COPY) |
| `RDRTP_inc` (S2M / RX) | queue_base **+0x38 = abs 0x1038** | write N → advance the S2M tail by N (publish empty recv buffers) |
| `TDRDTP_inc` (M2S DATA) | queue_base **+0xe0 = abs 0x10e0** | cayman "enhanced prefetch" independent data tail pointer |
| `q_sw_ctrl` | queue_base **+0xb0 = abs 0x10b0** | `rst_tail_ptr` / `rst_data_tail_ptr` (the re-arm reset) |

`VAL_MASK` is `0xffffff` (24-bit), queue stride **0x1000**, queue_base `(q+1)<<12`.
The getter exists ×4 per arch (cayman/mariana/sunda; sunda lacks the data-tail
pair); the offset pattern is identical. This matches the shared DMA tail-pointer
anchor and [pring-descriptors §7.1](./pring-descriptors.md). [All byte-exact.]

## 1.6 Local completion — the completion-marker poll (no semaphore)

`dma_wait_for_completion_handle @0x22def0` detects LOCAL DMA completion without a
semaphore, by polling a magic marker word the DMA's terminal completion descriptor
overwrites:

```c
// dma_wait_for_completion_handle(...)  @0x22def0   [OBSERVED HIGH — byte-exact]
#define DMA_COMPLETION_MARKER       0xabcdef01u            // sizeof(uint32_t) = 4
marker = DMA_COMPLETION_MARKER;                            // 22df0e: mov [rsp+0xc],0xabcdef01
for (i = 0; i < timeout; ++i) {
    dmem_buf_copyout(&got, 4, 4);                          // 22dfa0: read 4 bytes back
    if (got != DMA_COMPLETION_MARKER) break;               // 22dfa9: cmp eax,0xabcdef01
    usleep(10);                                            // 22dfb9: sleep 10us, retry
}                                                          // 22dfbe: cmp rbx,rbp ; bounded
// on success: re-arm the marker (dmem_buf_copyin) for the next use
```

Verbatim constants: *"DMA_COMPLETION_MARKER 0xabcdef01"* @0x4d510a6,
*"completion marker"* @0x844897. This is the synchronous, no-interrupt completion
underneath the semaphore handshake. [Byte-exact disasm + both constants OBSERVED HIGH.]

## 1.7 The RX-ring completion-sema resolver — `dma_ring_get_sema_to_inc` @0x22dc80

```asm
; dma_ring_get_sema_to_inc(per_engine_table, dma_ring_info*)  @0x22dc80   [OBSERVED HIGH]
22dc80:  cmp DWORD PTR [rsi],0x2        ; dma_ring_info.type == RX (2 / S2M)?
22dc83:  jne 22dc98                      ; (asserts type==2)
22dc85:  mov eax,[rsi+0x10]              ; a stored sema index (ring slot)
22dc88:  cmp eax,0xffffffff ; jne ret    ;   if set (!= -1), use it
22dc90:  movzx edx,dl ; mov eax,[rdi+rdx*4+0x10]  ; else index a per-engine table
; assert: desc->desc_type == KBIN_DMA_DESC_INC_SEMA
```

For the S2M (RX) pring, this resolves *which* EVT_SEM the ring's `INC_SEMA`
completion descriptor increments when inbound data lands — the bridge from a
completed RX pring to the `recv_sema` the waiting step polls. [Type==2 gate +
per-engine table + INC_SEMA assert OBSERVED HIGH.]

## 1.8 Cross-ring handshake + flow control

The classic 2-semaphore producer/consumer pair **plus** a credit:

- **SEND** → INC the peer's `recv_sema` (`__post_send` → `encd_dma_inc_recv_sema`);
  the receiver waits this.
- **RECV** → INC the `send_sema` = **returned credit** (`__post_recv` →
  `encd_dma_inc_send_sema`); the sender waits this before sending the next chunk.
- The **flow-control credit** is the channel `send_credit` (`ring_ctx` +0x02, u16);
  its CSR is `get_channel_send_credit_sema_addr @0x232bd0`:

```asm
; get_channel_send_credit_sema_addr  @0x232bd0   [OBSERVED HIGH]
232be0:  mov rax,[rdi+0x2009f8]            ; ctx
232be7:  movzx ebp,WORD PTR [rdi+0x9c0]    ; per-channel credit sema id
232bff:  call encd_arch_get_mla_device
232c13:  call encd_arch_get_bar0_top_sp_0_offset  ; base = TOP_SP_0 BAR0 EVT_SEM window
232c1b:  cmp bp,0xff                        ; 0xff sentinel = "no credit sema"
```

`net_initial_send_credits` is seeded `>0` (assert @0x8426ee) and
`< NET_INDEX_LOOP_SIZE` (assert @0x7ef158) — i.e. between 1 and 511.
`reset_send_credit @0x1495e0` zeroes it each phase.

**Step-to-step chaining.** The `cc_op.ring_send_complete` / `ring_wait_complete`
flags (§2.2) drive the on-device gate: a step marks `ring_send_complete` when its
send descriptors are posted; the next step's `ring_wait_complete` gates on the
prior completion semaphore reaching the step target (a counted, monotonic per-step
target). `trigger_next` (`cc_op` byte0 bit7) chains the records
("Adding CONTINUE mark" @0x8029e0). So **step N's recv/issue is gated on step
N-1's completion** — the counted-barrier chaining; the per-step *target values*
live in the LX firmware (MED). [Flag bits + composer order OBSERVED HIGH; the
"N waits N-1" semantics HIGH structure; the per-step target values MED.]

## 1.9 Descriptor lifecycle (post → exec → complete → signal → wait → re-arm)

| Phase | What happens | Confidence |
|-------|--------------|-----------|
| **POST** | `direct_send`/`direct_recv` build the `COPY` + `INC_SEMA` (+ optional `EVENT`) descriptors in the vring, bump `used_desc_count`, then `vring_dump_to_pring*` copies them into the standing physical pring | OBSERVED HIGH (host) |
| **EXEC** | a write of N to the M2S/S2M tail doorbell (+0x1038) advances the tail by N → the SDMA engine walks the pring (data move, embedded `INC_SEMA`, any `EVENT`) | INFERRED-STRONG (device) |
| **COMPLETE** | bytes land; the 4-byte completion-marker is overwritten (clearing `0xabcdef01`) AND the `INC_SEMA` desc fires (EVT_SEM `+= 1`) | INFERRED-STRONG |
| **SIGNAL** | the peer's `recv_sema` is incremented across the die (the cross-die EVT_SEM write; `dma_apb_bcast` fans it to masked peers) | OBSERVED HIGH (builder) |
| **WAIT-RELEASE** | the waiting rank's `WAIT-GE + DEC` on `recv_sema`/`mine` releases it once the target is reached; `mark_complete @0x149850` / `mark_end @0x157b80` close the step | OBSERVED HIGH (ops) |
| **RE-ARM** | the standing pring is **re-armed in place** — `q_sw_ctrl rst_tail_ptr` @+0x10b0 rewrites tail pointers, `reset_send_credit` re-seeds the credit, `switch_completed` + the basic-block-switch doorbell advance to the next block's pring. The ring persists across iterations | INFERRED-STRONG |

[POST/SIGNAL/WAIT-RELEASE host builders + sema ops OBSERVED HIGH; the on-device
EXEC/COMPLETE/RE-ARM walk is in the FLIX-undecodable LX path — INFERRED-STRONG,
corroborated by the decodable Q7 data-plane leg + the al_udma CSR semantics.]

## 1.10 Device-side reconciliation (Q7 transport leg)

One ring step = one send/wait pair = one `rdma_desc_gen` + `rdma_desc_start` on the
Q7 transport core. `rdma_desc_gen` builds the per-engine SDMA descriptor ring plus
the LOCAL sema descriptor (bumps `local_sem` on source-engine done = the host's
`KBIN_DMA_DESC_INC_SEMA` local leg) and the REMOTE sema descriptor (bumps
`remote_sem` on the peer when bytes land = the cross-die `recv_sema` INC).
`rdma_desc_start` drains the descriptor writes, splits by role (TX/RX), and
doorbells the tail: **TX → M2S `TDRTP_inc` @+0x1038**, **RX → S2M `RDRTP_inc`
@+0x1038**. So `direct_send` *is* the TX leg, `direct_recv` *is* the RX leg;
`__post_send`'s `recv_sema` INC *is* the REMOTE sema descriptor; `__post_recv`'s
`send_sema` INC *is* the credit return; the local completion-marker *is* the LOCAL
sema descriptor's generation-tag poll. [Host↔device leg correspondence HIGH
via the +0x1038 doorbell + local/remote sema descriptors; the per-collective
schedule is LX-resident — MED.]

---

# Section 2 — The Collective-Config Schema

The collective config is the **`neff_configs` program block** — `libnrt` DWARF DIE
`<14f9029>`, `byte_size 3592`. It is **not** a tagged message and **not** an
on-disk JSON file; it is an in-memory struct the host fills at PREPARE/START and
DMAs onto the TOP_SP (CTRL SPAD → SRAM 4 KiB; SLOT SPAD → TPB IRAM 32 KiB; EXEC cfg
→ NX DRAM). The `libncfw.so` "JSON" is a runtime ctx pretty-printer
(`ncfw_log_neff_configs` @0x12864), not a config file.

## 2.1 The top-level config — `struct neff_configs` (3592 B)

Every member offset from the `libnrt` DWARF (DIE `<14f9029>`):

| Offset | Field | Type | Role |
|--------|-------|------|------|
| +0 | `dma_alloc_bitmap` | array | which DMA engines this program owns |
| +64 | `desc_count` | array | per-block descriptor counts |
| +2144 | `slot_spad_base[6]` | `addr_t[6]` | SLOT scratchpad bases → TPB IRAM (loaded at START) |
| +2192 | `ctrl_spad_base` | `addr_t` | CTRL scratchpad base → SRAM; **holds the `cc_op` command table** |
| +2200 | `barrier_configs` | `neff_barrier_configs_t` (296 B) | §2.5 |
| +2496 | `basic_block_configs` | `basic_block_configs_t` (1048 B) | §2.6 |
| +3544 | `trigger` | `semaphore_t` (8 B) | kick THIS op |
| +3552 | `trigger_next` | `semaphore_t` (8 B) | chain to the NEXT op |
| +3560 | `complete` | `semaphore_t` (8 B) | this op's completion sema |
| +3568 | `complete_prev` | `semaphore_t` (8 B) | PREV op's completion (back-edge) |
| +3576 | `tpb_stop_sema` | `semaphore_t` (8 B) | TPB quiesce sema |
| +3584 | `op_num` | `uint32_t` | # `cc_op` entries in the ctrl-spad table |
| +3588 | `function_n` | `uint16_t` | # NEFF functions / basic blocks |
| +3590 | `tpb_compl_addr_num` | `uint8_t` | # TPB completion addresses |
| +3591 | *(packed `u8`)* | bitfield | `leader:1` `dbg_cc_nop:1` `host_cc:1` `__reserved0:5` |

The `+3591` flag byte is byte-verified: each field has `DW_AT_bit_size` (leader=1,
dbg_cc_nop=1, host_cc=1, __reserved0=5), all at `data_member_location 3591`.
`host_cc` selects host-CC mode (the leader-done sema id 6/7 vs 2/3, set by
`encd_enable_host_cc`); `leader` pairs with the `cc_op.reporter` bit.

> **CORRECTION — field types.** `op_num` is `uint32_t` (DWARF `<14f3a84>`),
> `function_n` is **`uint16_t`** (DWARF `<14f3a73>`), `tpb_compl_addr_num` is
> **`uint8_t`** (DWARF `<14f3a5d>`). Earlier descriptions that listed `function_n`
> as `u32` / `tpb_compl_addr_num` as `u16` are wrong — verified directly here. And
> `slot_spad_base` is an **array of 6** (`DW_AT_upper_bound : 5`), not a scalar.

**Carrier types.** `addr_t` (8 B) is a `struct addr` whose member `soc_addr`
(u64) is a SoC CSR pointer (`ncfw_log_addr` prints `{"soc_addr":"0x%016lX"}`).
`semaphore_t` (DWARF `<14f85c8>`, 8 B) wraps one `addr` — so `trigger` /
`trigger_next` / `complete` / `complete_prev` / `tpb_stop_sema` are semaphore
*handles* (an `addr`-wrapped `soc_addr` CSR pointer), not bare `u64`. The host
never posts a tagged opcode; it fills this address surface, and the `cc_op` table
inside `ctrl_spad` *is* the per-step collective program. [Every member offset +
type + the bit split OBSERVED HIGH.]

## 2.2 The `cc_op` record format (byte-exact, both views)

Each `ctrl_spad` slot = a 1-byte header + a `cc_op` command word. Two independent
views, both byte-verified.

### Decoder view — `ncfw_log_spad_ctrl_cc_op_entry` @0x1840 (libncfw)

Every bitfield extraction, disassembled:

| Field | Extraction | Address |
|-------|-----------|---------|
| `cc_op` (header) | `movzbl (%rax); and $0x1` | byte0 bit0 |
| `algo_type` | `and $0xf` | byte0 [0:3] @0x1a43 |
| `algo_sub_type` | `shr $0x4; and $0x7` | byte0 [4:6] @0x1c9a |
| `trigger_next` | `shr $0x7` | byte0 [7] @0x1efa |
| `reporter` | `movzbl 0x1(%rax); and $0x1` | byte1 [0] @0x2154 |
| `ring_wait_complete` | `movzbl 0x1(%rax); shr $1; and $0x1` | byte1 [1] @0x23b0 |
| `ring_send_complete` | `movzbl 0x1(%rax); sar $0x2; and $0x1` | byte1 [2] @0x260f |
| `channel_list` (RING) | `mov 0x3(%rax),%ebx` (u32) | +0x3 @0x2a64 |
| `sema_shift_offset` (MESH) | `movzwl 0x3(%rax)` (u16) | +0x3 @0x2eb6 |
| `sema_mask` (MESH) | `movzwl 0x5(%rax)` (u16) | +0x5 @0x30da |

The decoder body is **linear** (no `algo_type` switch) — a debug pretty-printer
that dumps both the RING (`channel_list`) and MESH (`sema_shift_offset`/
`sema_mask`) union views unconditionally; the live union selector is `algo_type`.
[Every immediate disassembled — OBSERVED HIGH.]

### Packer view — `create_spad_ctrl_entry` @0x232cd0 (libnrt)

The builder assembles an 8-byte word on the stack and returns it
(`mov rax,[rsp]` @0x232dc0). Byte-verified construction:

```c
// create_spad_ctrl_entry(entry*, has_op, trignext, chan_or_shift, mask) @0x232cd0
// WORD@[rsp+0] = the header + byte1 region:
word0 = (trigger_next << 15)         // 232d7e: shl edx,0xf
      | (0x3        << 12)           // 232d7b: shl edi(=3),0xc  -> sub_type field const 3
      | (0x1        <<  8)           // 232d78: shl eax(=1),0x8
      | 0x1                          // 232d81: or eax,1  -> header.cc_op ACTIVE
      | (prior_word0 & 0xfe);        // 232d96: preserve prior byte0[7:1]
// BYTE@[rsp+2] = the algo/flag nibble region:
byte2 = ( (r9 << 1)                  // 232da6: lea eax,[r9+r9]
        |  r13                       // 232daa: prior-header carry (byte[entry+8].bit0)
        | (r15 << 2)                 // 232d84: shl r15d,0x2
        | (c111 << 3)                // 232d88: byte[ctx+0x6838+0x111] << 3
        | (c112 << 4) ) & 0x1f       // 232d8d: byte[ctx+0x6838+0x112] << 4
        | (prior_byte2 & 0xe0);      // 232dad: preserve prior high 3 bits
// WORD@[rsp+4] = chan_or_shift (4th arg)  ; 232d6c
// WORD@[rsp+6] = mask          (5th arg)  ; 232d72
```

> **GOTCHA — packer interleave vs decoder per-byte view.** The packer builds the
> header+byte1 region as one interleaved `WORD@0` and the algo/flag nibble as a
> separate `BYTE@2`; the lexical bit positions differ from the decoder's per-byte
> reads, but the **semantic field set and the `cc_op = 1` ACTIVE flag are
> identical**. The literal `(trigger_next<<15) | (3<<12) | (1<<8) | 1` is the
> canonical bit signature — keep it BIT-IDENTICAL across the
> [spad cc_op table page](./spad-ccop-tsync.md). Field-set match HIGH; exact
> bit-for-bit equivalence of the two views MED.

### Field semantics

`algo_type` [0:3] is the union selector and algorithm route — the low nibble of
host `enc_alg_type` (§2.3): 0 RING / 1 HIER / 2 MESH / 3 KANGARING /
4 SINGLE_CYCLE_RING / 5 INTRA_RDH / 6 SINGLE_STEP_MESH / 7 INTER_RDH /
8 TWO_STEP_POD_MESH / 9 LATENCY_OPT_MESH / 10 BW_OPT_MESH (11 INVALID). All 0..10
fit the 4-bit field. `algo_sub_type` [4:6] = `enc_alg_mesh_type` (FULL/GROUPED/
TRN2/SWITCH). `reporter` (byte1 b0) marks the leader/reporter channel.
`channel_list` (RING, u32) is a bitmask of ≤32 ring channels; `sema_shift_offset`
+ `sema_mask` (MESH, u16+u16) overlay the same four bytes to compute the mesh
event's wait/post semaphore from a base.

### Per-entry log line (the host's own field roster)

`encd_dma_mark_end @0x237200` emits, verbatim @0x804088:
*"[nec_dev %2u, TOPSP %d, op %d] CTRL mark -alg %d-subalg %d-trignext %d-chlist %d
-reporter %d-sema_shift_offset %u-sema_mask %u-safe_mode %d"* (and a SLOT-CONTINUE
variant @0x804198). These name exactly the `cc_op` fields.

> **NOTE — `safe_mode` is host-only.** The mark log carries a `safe_mode` field
> the on-device `cc_op` decoder reads *no* width for — it is a host-side staging/
> safety flag, not part of the on-wire `cc_op` record.

## 2.3 The enums the config carries (from DWARF)

Exact enumerator names + values (DIE offsets noted):

- **`enc_alg_type`** (DIE `<61eb4>`, 12) — `ENC_ALG_RING=0, ENC_ALG_HIER=1,
  ENC_ALG_MESH=2, ENC_ALG_KANGARING=3, ENC_ALG_SINGLE_CYCLE_RING=4,
  ENC_ALG_INTRA_RDH=5, ENC_ALG_SINGLE_STEP_MESH=6, ENC_ALG_INTER_RDH=7,
  ENC_ALG_TWO_STEP_POD_MESH=8, ENC_ALG_LATENCY_OPT_MESH=9, ENC_ALG_BW_OPT_MESH=10,
  ENC_ALG_INVALID=11`.
- **`enc_alg_mesh_type`** (DIE `<125d7e>`) — `ENC_ALG_FULL_MESH=0,
  ENC_ALG_GROUPED_MESH=1, ENC_ALG_MESH_TRN2=2, ENC_ALG_MESH_SWITCH=3,
  ENC_ALG_MESH_INVALID=4`.
- **`enc_op_type`** (DIE `<3bbd1>`, prefix `ENC_`, **not** `ENC_OP_`) —
  `ENC_ALLGATHER=0, ENC_ALLREDUCE=1, ENC_BROADCAST=2, ENC_REDUCE=3,
  ENC_REDUCE_SCATTER=4, ENC_SEND=5, ENC_RECV=6, ENC_ALLTOALL=7, ENC_PERMUTE=8,
  ENC_PERMUTE_REDUCE=9, ENC_PERMUTE_IMPLICIT=10, ENC_PERMUTE_REDUCE_IMPLICIT=11,
  ENC_ALLTOALL_V=12, ENC_OP_INVALID=13` (alias `ENC_OP_N=13`). This is the
  high-level op the §2.7 expansion lowers into `cc_op` records.
- **`SDMA_CCETYPE`** (DIE `<337be>`) — `SDMA_CCETYPE_ADD=0, _FMA=1, _MAX=2,
  _MIN=3, _EXT=4, _GCE=5`.
- **`reduction_type_t`** (DIE `<60c987>`, nested in `enc_primitive`) —
  `RING_2R1W=0, RING_2R2W=1, KANGARING_NR1W=2`.

> **CORRECTION — `enc_op_type` prefix.** The DWARF prefix is `ENC_` (e.g.
> `ENC_ALLGATHER`), not `ENC_OP_`. There are **13 real op kinds 0..12** plus
> `ENC_OP_INVALID=13` (with the count alias `ENC_OP_N=13`). Earlier copies that
> stopped at `ENC_PERMUTE_IMPLICIT=10` were incomplete — `…REDUCE_IMPLICIT=11`,
> `ALLTOALL_V=12` are present.

> **CORRECTION — `encd_neigh` membership.** DIE `<3a704>` (prefix `ENCD_NEIGH_`):
> `LOCAL=0, NEXT=1, PREV=2, GATEWAY=3, PEER_RMTV=4, PEER_RMTV2=5, PEER_LOCAL=6,
> NEXT_PEER_RMTV=7, INVALID=8`, with `NUM=8` aliasing `INVALID`. Positions 5/6/7
> are `PEER_RMTV2 / PEER_LOCAL / NEXT_PEER_RMTV` (see
> [collective-enums](../ops/collective-enums.md) for the reduce-op layering).

## 2.4 The reduce op is NOT in the `cc_op` word

The `cc_op` decoder reads no `SDMA_CCETYPE`-width field. The reduce arithmetic
rides the SDMA CCE descriptor (`add_dma_packet_cce @0x2307d0`), and the fold *read/
write pattern* is `reduction_type_t` (the 3rd arg of `recv_reduce_*`). So:

```
cc_op.algo_type   = "HOW to move/route"
SDMA_CCETYPE      = "WHAT to compute"  (rides the CCE descriptor)
reduction_type_t  = "the fold ACCESS PATTERN" (RING_2R1W / RING_2R2W / KANGARING_NR1W)
SDMA_DTYPE        = dtype, pass-through (no re-encoding)
```

> **NOTE — `reduction_type_t` is a pattern, not an operator.** Per
> [collective-enums](../ops/collective-enums.md): the reduce-op *layers* are the
> ISA `CCE_OP{ADD0,MAX2,MIN3}` ⊂ DWARF `SDMA_CCETYPE{ADD0,FMA1,MAX2,MIN3,EXT4,
> GCE5}`; `DGE_COMPUTE_OP{NONE0,ADD1,MULTIPLY2,MAX3,MIN4}`; and `REDUCE_OP{ADD0,
> AVERAGE1,MAX2,BITWISE_OR3,AND4,XOR5}`. `reduction_type_t{RING_2R1W=0,RING_2R2W=1,
> KANGARING_NR1W=2}` is the ring read/write *pattern*, not a reduce op. Do not
> conflate the layers. [OBSERVED-absence of a reduce field in `cc_op` HIGH.]

## 2.5 The `barrier_configs` sub-schema (296 B)

`neff_configs.barrier_configs` (@+2200) is a `neff_barrier_configs_t` (DWARF
`<14f8fd3>`, byte_size **296**), a **union** of a lightweight host barrier or a
full device barrier, tagged by `execute_device_barrier`:

| Offset | Field | Role |
|--------|-------|------|
| +0 | `union { host_barrier(16 B) \| device_barrier(292 B) }` | selected by tag |
| +292 | `execute_device_barrier:1` + `__reserved0:5` (packed `u8`) | union tag |

- **`neff_host_barrier_configs`** (DWARF `<14f8e22>`, 16 B): `barrier_start@0`,
  `barrier_done@8` — host arms `barrier_start` (releases TOP_SPs into the
  collective); leader SPs INC `barrier_done`, host + non-leaders WAIT.
  See [neff-host-barrier](./neff-host-barrier.md).
- **`neff_device_barrier_configs`** (DWARF `<14f8ec5>`, 292 B):

| Offset | Field | Role |
|--------|-------|------|
| +0 | `barrier_step_config[4]` (4 × 52 B = 208 B) | the 4-step counted barrier |
| +208 | `dma_sync_sema` | |
| +240 | `dma_apb_bcast` | the APB broadcast fan-out |
| +260 | `start_network_proxy` (addr) | |
| +268 | `dma_sync_sema_value` (u32) | |
| +272 | `tdrbp_low` / +276 `tdrbp_high` (u32) | tail-descriptor base pointer |
| +280 | `desc_count` (u32) / +284 `total_desc_n` (u32) | |
| +288 | `dma_engines_bitmap` (u16) / +290 `queue_id` (u8) / +291 `__reserved` | |

  The per-step `barrier_step_config` element (byte-verified): `m2s_val@0` (u16),
  `s2m_val@2` (u16), `barrier_sema[4]@4` (addr×4 = up to 4 leader SPs' barrier
  semaphores), `target_sema_val[4]@36` (u32×4 per-leader target counts). The 4-step
  counted barrier is detailed on [neff-device-barrier](./neff-device-barrier.md).
  [All offsets OBSERVED HIGH.]

## 2.6 The `basic_block_configs` sub-schema (1048 B)

`neff_configs.basic_block_configs` (@+2496) is a `basic_block_configs_t` (DWARF
`<14f8cbb>`, byte_size **1048**):

| Offset | Field | Type | Role |
|--------|-------|------|------|
| +0 | `table` | 1024 B array | the per-block dispatch sub-table |
| +1024 | `switch_completed` | `semaphore_t` | the device sets this when a block's pring is done; gates the basic-block switch |
| +1032 | `pring_base_addr` | `addr` | the standing TopLevel-DMA pring base (HBM) |
| +1040 | `pring_total_desc_num` | `u32` | the pring descriptor count |
| +1044 | `__reserved` | `u32` | |

> **NOTE — host == device for this sub-struct.** The host offsets +1024/+1032/+1040
> are exactly `0x400/0x408/0x410` — byte-identical to the device-image offsets
> (`switch_completed@+0x400`, `pring_base_addr@+0x408`, `pring_total_desc_num@+0x410`).
> Unlike the top-level `neff_configs` (whose host and device offsets differ), this
> sub-struct *coincides*. See [pring-descriptors §4](./pring-descriptors.md) for
> the persistence mechanism (`switch_completed` + the basic-block-switch doorbell +
> the reprogram/reset descriptor section). [OBSERVED HIGH — DWARF + the matching
> 0x400/0x408/0x410.]

## 2.7 The `algo_type → cc_op`-sequence expansion

A high-level collective is **not** one `cc_op` record — it is a SEQUENCE of chained
records. The host pipeline:

1. **SELECT** — `enc_hier_primitive::__select_algorithms() @0x14c620` assigns ONE
   `enc_alg_type` per LEG. A hierarchical all-reduce carries five per-leg choices
   (the runtime log "Hier algorithm selections - alg_intra_allg … alg_inter_allr …
   alg_inter_redsct …"). Legal sets: intra all-gather/reduce-scatter ∈ {RING,
   KANGARING}; inter ∈ {RING, INTER_RDH, MESH, metaring}; inter all-reduce adds
   SINGLE_CYCLE_RING. Gates: SINGLE_CYCLE_RING is **all-reduce only**; KANGARING is
   **intra only**.
2. **BUILD** — the chosen primitive builds its own spad `cc_op` sequence:
   `enc_metaring_primitive::__handle_semaphore_init →
   encd_populate_metaring_topsp_config @0x240bf0 → prep_metaring_topsp_config
   @0x2331d0` (ring / kangaring / SCR); `enc_mesh_primitive::__handle_semaphore_init
   → encd_populate_mesh_topsp_config @0x240330` (mesh + sub-types).
   `prep_metaring_topsp_config` resolves, per channel, the SP base + `sema_r`
   offset, the APB-bcast m2s/s2m offsets + mask, and the d2d routing
   (`set_addr_routing_bits @0x231340`) — i.e. it computes the `trigger`/`complete`
   `soc_addr`s and the `channel_list`/sema fields each `cc_op` record needs.
3. **PACK + CHAIN** — `encd_dma_mark_end @0x237200` walks the steps, calls
   `create_spad_ctrl_entry` (@0x2379b9, @0x237b88) to pack each `cc_op` record,
   `mark_spad_slot_final_entry @0x22fc20` for the END mark, and emits the CTRL/SLOT
   mark log per op. The chain is `trigger_next` ("Adding CONTINUE mark" @0x8029e0)
   + `complete`/back-edge ("Adding END mark with compl address 0x%lx" @0x802a78);
   `op_num` counts the records. The per-slot `tp_inc_steps` list
   ("tp_inc_steps[%d] = m2s %d, s2m %d, repeat %d" @0x804248) is the concrete
   DMA tail-pointer-increment schedule each record drives.

| Collective | Decomposition |
|-----------|---------------|
| **RING all-reduce** | reduce-scatter (N-1 `recv_reduce_*` CCE-fold steps) + all-gather (N-1 `direct_recv`/`send` copy steps); each step a `cc_op` record with `algo_type=RING`, `channel_list` selecting channels, ring_wait/send_complete the step flags |
| **KANGARING** | same ring tape, `reduction_type_t = KANGARING_NR1W` |
| **MESH all-reduce** | a flat event tape; each `cc_op` (`algo_type=MESH`, `algo_sub_type` the mesh sub-kind) carries `sema_shift_offset`/`sema_mask` |
| **HIER all-reduce** | multiple chained records: intra RS (RING) → inter AR (MESH/RING/RDH) → intra AG (RING) |
| **SENDRECV** | a single directed SB2SB leg = one `cc_op` record |

[HIGH structure; the per-(world-size) numeric algo a leg resolves to depends on the
`enc_can_post_*` thresholds — MED, not enumerated.]

## 2.8 Versioning / validation — the CC signature (not a version field)

The config is **not** versioned by a version field; it is identified/validated by a
**CC signature** and guarded by size checks:

1. **Signature compute / reuse** — `enc_calculate_signature @0x108220` computes the
   comm's config signature (via `MD5_Init/Update/Final`); `nrt_cc_prepare` calls
   `enc_find_cc_context_by_signature @0x12a0c0` to REUSE an already-prepared context
   if the signature matches (skip rebuild). *"cannot calculate CC signature: not in
   parse state"* gates it.

   > **CORRECTION — symbol names.** `calculate_signature` and
   > `validate_rg_signature` appear in the DWARF as **inlined** subprograms (DIEs
   > `<6bead4>`, `<8187fe>`, both `DW_AT_inline:1`); they have **no standalone
   > `nm` symbol**. The discrete, callable symbol is **`enc_calculate_signature`
   > @0x108220** (`.cold` @0x46c40). Cite that address, not a non-existent
   > `calculate_signature` symbol.

2. **Peer consistency** — the inlined `validate_rg_signature` cross-checks the
   signature with peers; a mismatch raises *"failed signature check from peer: %u.
   This is likely caused by a compilation error generating mismatched collectives
   between the peers"* (@0x7de4d0) — a config-consistency hash ensuring all ranks
   compiled the same `cc_op` sequence.
3. **Size guard** — `ndl_program_engine` asserts `ncfw_configs_output_len <=
   encd_arch_get_ncfw_configs_sz()` (CONFIGURE) and `> 0 && <= …_sz()` (START); the
   serialized config must fit the per-arch device config region.
4. **Completion counting** — the host counts one CC notification per `cc_op` step
   per TOP_SP and asserts the tally (§3.4).

## 2.9 Per-gen schema stability (sunda v2 / cayman v3 / mariana v4 / mariana_plus v4+)

The `cc_op` record schema **and** the `neff_configs` sub-struct shapes
(`barrier_configs` / `basic_block_configs` / `semaphore_t` / `addr_t`) are
**schema-wide** — the host `libnrt` is a single binary serving all gens, and the
`libncfw` `cc_op` decoder bitfield is byte-identical across the four arch copies.
Only the **data sizes** scale per arch (the mesh-event count 50 sunda vs 108 the
rest via `encd_arch_get_ncfw_configs_max_mesh_events @0x2560d0`; the device
config-region size; the ctrl/slot spad sizes — cayman 0x1000/0x8000). The `cc_op`
*contract* is fixed.

> **GOTCHA — MAVERICK (NC-v5) NCFW is FILE-ABSENT.** The arch_id → codename map is
> `0x05=SUNDA/NC-v2, 0x0c=CAYMAN/NC-v3, 0x14=MARIANA/NC-v4,
> 0x1c=MARIANA_PLUS/NC-v4+`. The v5/MAVERICK NCFW image is **absent** from the
> shipped corpus, so every v5 claim is INFERRED / ABSENT. The v5
> `WAIT_MODE`/`UPDATE_MODE` enums diverge (drop the EVENT family, add
> `_REG_OFFSET` + `UNORDERED` + `EVT_SEM_NONBLOCKING_CMD`) — those are the inline
> EVENTS-block sync modes, *not* the `cc_op` record; the `cc_op` schema itself is
> inferred-unaffected, but a v5 collective lowers the surrounding barrier/sync
> differently. [v2–v4+ byte-grounded; v5 INFERRED/ABSENT.]

---

# Section 3 — The NCFW ↔ Host Command / Control Protocol

There is **no single "NCFW message opcode" wire format**. The host↔NCFW boundary is
a small fixed set of CONTROL ACTIONS — each either (a) a DMA of a structured config
block onto the device + a LOCAL_REG doorbell, or (b) a doorbell write baked into the
inference DMA stream. The "command payload" is the `neff_configs` program block
(§2); the "control" is the LOCAL_REG doorbell quintet.

## 3.1 The command set — five control actions + the per-step gate

| Command | Host builder/driver | Submission | Doorbell |
|---------|--------------------|-----------|----------|
| **INIT/CONFIGURE** | `encd_ncfw_init @0x251eb0 → encd_ncfw_configure_device_init @0x230c70` | read `config_addr` from NX LOCAL_REG; `ndl_program_engine(INIT cfg, sz)` | `set_init_signal` LOCAL_REG **+0x1540** (vtable +0x6f8) |
| **TSYNC-START** | `encd_ncfw_tsync_start @0x2526e0` | (one-shot) | `set_tsync_signal` **+0x1560** (vtable +0x700) |
| **START/KICK** | `enc_start_operations @0xfd5c0 → encd_start_executable @0x2431c0` | `topsp_init_dma` ("qTOPSPinit") DMAs CTRL SPAD→SRAM (4 KiB) + SLOT SPAD→TPB IRAM (32 KiB) + EXEC cfg→NX DRAM | `set_host_trigger` **+0x15a0** (abs **0x615a0** cayman/mariana, **0x60848** sunda; vtable +0x708) |
| **STEP/SWITCH** | `itf_translate_function_call_instr @0x279f10` bakes the switch into the function-call instruction | DMA wr32 to bbswitch reg, gated by an evt-wait on the TOP_SP ack evt; host polls `switch_completed` via `check_basic_block_switch @0x1d01c0` | `basic_block_switch` **+0x15e0** (abs **0x615e0**) |
| **STOP/ABORT** | `enc_stop_operations @0xfd3b0 → encd_stop_executable @0x240040 → stop_sp_engines @0x236770` | (one-shot, gated on ccop_owner) | `set_stop_signal` **+0x15c0** (abs **0x615c0**; vtable +0x718) |
| **per-STEP GATE** | `add_wait_before_cc_command @0x27b390` | `tdrv_add_ev_wait` on the per-TOP_SP ack evt id | (the per-TOP_SP ack evt addr, §3.4) |

### The LOCAL_REG doorbell quintet (byte-exact)

Each NX-core control action is a single 32-bit write of value 1 to a TOP_SP
LOCAL_REG control CSR, dispatched per-arch through the `kaena_khal` HAL vtable. The
cayman immediates:

```
aws_hal_sp_topsp_set_init_signal_cayman  @0x471ae0:  lea rdi,[rax+0x1540]   ; INIT
aws_hal_sp_topsp_set_tsync_signal_cayman @0x471b10:  lea rdi,[rax+0x1560]   ; TSYNC
aws_hal_sp_topsp_set_host_trigger_cayman @0x471b40:  lea rdi,[rax+0x15a0]   ; START
aws_hal_sp_topsp_set_stop_signal_cayman  @0x471b70:  lea rdi,[rax+0x15c0]   ; STOP
aws_hal_sp_topsp_get_host_trigger_reg_offset_cayman @0x47b080:  mov eax,0x615a0  ; abs START
                          (bbswitch reg getter)      @0x47b0a0:  mov eax,0x615e0  ; abs SWITCH
aws_reg_sunda_get_top_sp_nx_local_reg_host_trigger_offset @0x479120: mov eax,0x60848 ; abs START (sunda)
```

The cayman LOCAL_REG base is therefore `0x60000` (`0x615a0 − 0x15a0`). The vtable
dispatchers select the per-arch implementation by `al_hal_tpb_get_arch_type` then
`jmp *kaena_khal+slot` (init +0x6f8, tsync +0x700, host_trigger +0x708, stop
+0x718). [All byte-exact.]

> **NOTE — control-plane vs data-plane doorbells.** The CONTROL host doorbell
> (START/STEP/STOP) is a value-1 write to `LOCAL_REG+0x15a0 = abs 0x615a0` (cayman/
> mariana; sunda 0x60848). The DATA-plane tail-pointer doorbell (§1.5) is the
> separate `TDRTP_inc`/`RDRTP_inc` at queue+0x38 = abs 0x1038. Different surfaces,
> different planes — do not confuse them.

### Per-command anatomy (CONFIGURE)

```c
// encd_ncfw_configure_device_init(...)  @0x230c70  (source "KaenaRuntime/tdrv/encd.c")
void encd_ncfw_configure_device_init(...) {
    aws_hal_sp_topsp_read_config_addr(...);   // READ the NX config_addr from LOCAL_REG
    //   asserts "config_addr == 0" / "unknown architecture"
    get_ack_evt_addr_for_topsp_engine(...);   // resolve the TOP_SP ack EVT
    memset(cfg, 0, encd_arch_get_ncfw_configs_sz());   // alloc + zero the cfg buffer
    encd_arch_get_ncfw_configs_initialization(...);    // per-arch INIT config DRAM image
    ndl_program_engine(cfg, len);             // DMA the INIT config onto the NX
    //   assert "ncfw_configs_output_len <= encd_arch_get_ncfw_configs_sz()"
    aws_hal_sp_topsp_set_init_signal(...);    // the INIT DOORBELL (LOCAL_REG+0x1540)
}
```

INIT/CONFIGURE loads the `ncfw_configs_initialization` DRAM image (the boot
config); START loads the per-run `ncfw_configs_execution` image.

## 3.2 The command / config message format

The command descriptor the host DMAs onto the NX is the **`neff_configs` program
block** (§2.1, DWARF DIE `<14f9029>`, 3592 B). The per-step command entry inside
`ctrl_spad` is the `cc_op` record (§2.2). The reduce op and dtype are *not* in the
`cc_op` word (§2.4). The host cross-checks the field SET against the device-image
view (via `libncfw`); the host *offsets* are the host struct, and the device-image
offsets differ — the two are reconciled by FIELD ROLE, not byte equality, except
`basic_block_configs` which coincides (§2.6).

## 3.3 The submission path — dedicated TOP_SP init-DMA + doorbell

The START config block does **not** ride the generic `hw_exec_queue`. The START
command allocates a DEDICATED single-queue UDMA bundle ("qTOPSPinit",
`ctx->topsp_init_dma`) and DMAs the three regions onto the NX:

```c
// encd_start_executable(...)  @0x2431c0   [OBSERVED HIGH — disasm + strings]
void encd_start_executable(...) {
    model_dma_engine_and_queue_bundle_alloc(/* "qTOPSPinit" */);  // assert nqueues==1
    //   on failure "Failed to allocate dma queue for TOPSP init"  @0x805c50
    dma_pring_alloc(); vring_set_allocate(); vring_add_desc_transfer(); vring_dump_to_pring();
    // DMA the THREE regions onto the NX:
    //   ctrl_spad_base.soc_addr     -> CTRL SPAD -> SRAM     (0x1000 cayman)
    //      "TOPSP #%u will load CTRL SPAD to SRAM (0x%lx)"   @0x805d68
    //   slot_spad_base[0].soc_addr  -> SLOT SPAD -> TPB IRAM (0x8000 cayman)
    //      "TOPSP #%u will load SLOT SPAD to TPB IRAM (0x%lx)" @0x805dd8
    //   encd_arch_get_ncfw_configs_execution -> EXEC cfg -> NX DRAM
    //      assert "ncfw_configs_output_len > 0 && <= encd_arch_get_ncfw_configs_sz()"
    dma_wait_for_completion_handle(...);                  // the 0xabcdef01 marker poll (§1.6)
    aws_hal_sp_topsp_set_host_trigger(...);               // the START DOORBELL (LOCAL_REG+0x15a0)
    // "[nec_dev %u] TOP_SP #%d started. %lu bytes of alg config loaded at 0x%lx"  @0x806050
}
```

The cayman spad sizes are byte-verified: `cayman_get_sp_spad_ctrl_sram_size
@0x25af00: mov eax,0x1000` and `cayman_get_sp_spad_slot_tpb_iram_size @0x25af10:
mov eax,0x8000`. CONFIGURE uses `ndl_program_engine` (the engine-program path) for
the INIT cfg; per-step CC ops ride the inference instruction stream's EVT_SEM
`sema_i` DMA-tail doorbell (+0x1800) and the bbswitch reg write. The
`trigger`/`trigger_next`/`complete` `soc_addr`s and the `cc_op.channel_list`/sema
fields the host writes are the values that populate the device-side semaphore
descriptors the NX core polls. [All OBSERVED HIGH — the qTOPSPinit alloc, the
3-region load, the spad sizes, the doorbell.]

## 3.4 The completion / status path (NCFW → host)

Three complementary completion surfaces:

1. **Per-TOP_SP ack EVT** (per-step / per-command). `get_ack_evt_addr_for_topsp_engine
   @0x2657e0` resolves, per engine, the completion EVT address
   (`get_ack_evt_id_for_topsp_engine → tdrv_arch_get_evt_addr @0x309f50`). The host
   gates each subsequent CC command on this EVT (`add_wait_before_cc_command`) and
   the function-swap waits it.
2. **Collective TOP_SP ack sema** (per-collective). `tdrv_sync_get_collective_topsp_ack_first
   @0x30ab70` / `_last @0x30abd0` read the reserved sync-sema indices (assert
   `…COLLECTIVE_TOPSP_ACK_FIRST/_LAST != TDRV_SYNC_EVENT_UNSUPPORTED`); the leader
   TOP_SP posts them on collective completion; the host waits them.
3. **The CC-status Notification Queue.** The NQ carries typed entries; the relevant
   type is **`NOTIFICATION_TYPE_TOPSP_CC_STATUS = 9`** (DWARF `notification_type`
   enum DIE `<109b72>`: `TRACE=0, EVENT=1, ERROR=2, INFER_STATUS=3, DMA=4,
   THROTTLE=5, TOPSP_TRACE=6, TOPSP_EVENT=7, TOPSP_ERROR=8, TOPSP_CC_STATUS=9,
   MAX=10`).

### The CC-status NQ entry / counted-completion protocol

The NQ is set up by `notification_init` (from `encd_ncfw_init`). The validation runs
inside `exec_request_process_errors.isra.0 @0x2615b0`. The host counts a per-TOP_SP
"CC end notification" tally — one notification per `cc_op` step, an end marker per
TOP_SP — and validates each entry's TYPE against an expected per-step type. The
verbatim asserts:

- *"Unexpected CC notification at %u/%u, type: 0x%x, expected: 0x%x on TOPSP: %d"*
  @0x80c7c0
- *"Missing CC end notification from TOPSP %d … (current_count_topsp[i] %u)"*
  @0x80c750
- *"Host CC operation timed out for lnc %u, TOPSP sent %d notifications out of %d"*
  @0x80dfb0

> **CORRECTION — the NQ consumer symbol.** There is **no** symbol
> `exec_consume_cc_core_notifications` in `libnrt.so`. The actual consumers are
> `exec_request_process_errors.isra.0 @0x2615b0` (the per-TOP_SP count/type
> validator above), `notification_consume_errors @0x300350`,
> `consume_ready_exec_notification_v2 @0x2fcce0`, and
> `nrt_profile_session_append_cc_notifications @0xaf700`. Cite these — not a
> non-existent consume symbol. [count/type structure OBSERVED HIGH; the concrete
> per-step type integers are firmware-bound — MED.]

### The `NRT_STATUS` codes (DWARF enum DIE `<d6ed>`, byte_size 4, 28 enumerators)

| Code | Value | Code | Value |
|------|-------|------|-------|
| `NRT_SUCCESS` | 0 | `NRT_EXEC_UNIT_UNRECOVERABLE` | 101 |
| `NRT_FAILURE` | 1 | `NRT_EXEC_BAD_INPUT` | 1002 |
| `NRT_INVALID` | 2 | `NRT_EXEC_COMPLETED_WITH_NUM_ERR` | 1003 |
| `NRT_INVALID_HANDLE` | 3 | `NRT_EXEC_COMPLETED_WITH_ERR` | 1004 |
| `NRT_RESOURCE` | 4 | `NRT_EXEC_NC_BUSY` | 1005 |
| `NRT_TIMEOUT` | 5 | `NRT_EXEC_OOB` | 1006 |
| `NRT_HW_ERROR` | 6 | `NRT_COLL_PENDING` | 1100 |
| `NRT_QUEUE_FULL` | 7 | `NRT_EXEC_HW_ERR_COLLECTIVES` | 1200 |
| `NRT_LOAD_NOT_ENOUGH_NC` | 9 | `NRT_EXEC_HW_ERR_HBM_UE` | 1201 |
| `NRT_UNSUPPORTED_NEFF_VERSION` | 10 | `NRT_EXEC_HW_ERR_NC_UE` | 1202 |
| `NRT_FAIL_HOST_MEM_ALLOC` | 11 | `NRT_EXEC_HW_ERR_DMA_ABORT` | 1203 |
| `NRT_UNINITIALIZED` | 13 | `NRT_EXEC_SW_NQ_OVERFLOW` | 1204 |
| `NRT_CLOSED` | 14 | `NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE` | 1205 |
| `NRT_QUEUE_EMPTY` | 15 | `NRT_NETWORK_PROXY_FAILURE` | 1206 |

> **CORRECTION — the EXEC band ordinals.** The `EXEC_*` band starts at **1002**
> (`NRT_EXEC_BAD_INPUT`), not ~1000; `NRT_EXEC_SW_NQ_OVERFLOW` is **1204** (in the
> HW band, not ~1000). The collective-specific HW error is
> `NRT_EXEC_HW_ERR_COLLECTIVES = 1200`. Value gaps exist at 8 and 12. All four spot
> values (0/1/4/5, 1004, 1200, 1204, 1002) read directly from DWARF. [OBSERVED HIGH.]

## 3.5 The NEFF basic-block STEP state machine

A NEFF program is a sequence of basic blocks / functions; each function CALL is
translated at build time into a DMA-driven SWITCH to the next block's pring.
`itf_translate_function_call_instr @0x279f10` (source "instr_tpb_functions.c")
bakes into the function-call instruction:

1. an **evt-wait** on the destination TOP_SP's ack EVT (`tdrv_add_ev_wait +
   get_ack_evt_id_for_topsp_engine`) — the gate ensuring the prior block's TOP_SP op
   completed before the swap;
2. a **trigger** write to the `basic_block_switch` reg (LOCAL_REG+0x15e0, via
   `encd_get_basic_block_switch_topsp_reg_addrs @0x236320 + instr_col_add_block_switch
   + add_wr32`) — the SWITCH doorbell;
3. an **args-table swap** + indirect-memcpy queue swap re-pointing the per-function
   descriptor tables.

The host-side tracker `enc_network_proxy_task::check_basic_block_switch @0x1d01c0`
(source "proxy_queue.cc") polls per-stream `basic_block_id` / `processing` /
`remaining` from an `enc_fifo_set` map; the driver is `proxy_progress @0x1d03f0`.

The full host-driven state machine:

```
CONFIGURE  encd_ncfw_configure_device_init: DMA INIT cfg -> NX, set_init_signal
           [+ TSYNC: encd_ncfw_tsync_start -> set_tsync_signal]
PREPARE    nrt_cc_prepare: build comm context, set exec prings, enqueue proxy tasks
START      enc_start_operations -> encd_start_executable: DMA EXEC cfg + CTRL/SLOT
           SPADs -> NX (topsp_init DMA), start DMA engines, set_host_trigger
STEP       per basic block: the NX walks the block's pring (issuing the cc_op DMA-
           tail + EVT_SEM ops, §1); on done sets switch_completed; the baked
           function-call switch (evt-wait + bbswitch reg write) advances to the next
           block; the host network-proxy tracks progress. REPEAT until function_n
COMPLETE   the NX posts the per-TOP_SP ack EVT + the CC-status NQ; the host drains
           (exec_request_process_errors) and waits COLLECTIVE_TOPSP_ACK
STOP       nrt_cc_schedule -> encd_free_exec_prings; enc_stop_operations ->
           encd_stop_executable -> stop_sp_engines, set_stop_signal
```

## 3.6 The communicator lifecycle (create / prepare / exec / destroy)

A four-phase host state machine (the `nrt_cc_*` call census):

- **CREATE** — `nrt_cc_global_comm_init @0x7fd90` (`enc_init/setup_global_comm`,
  builds the `enc_comm_info` topology descriptor — 72 B: `neuron_dev@0, rank@4,
  rank_n@8, local_rank_n@12, local_rack_rank_n@16, node@20, node_n@24,
  enc_topo_mode@28, enable_pod@32, use_net@33, pod@36, pod_n@40 …`);
  `nrt_build_global_comm @0xa9390` / `nrt_load_collectives @0xaa4c0`;
  `nrt_cc_create_stream @0x7ff10` (`encd_enable_host_cc` sets the host_cc flag).
- **PREPARE** — `nrt_cc_prepare @0x7f610`: MD5-sign + reuse-by-signature
  (`enc_find_cc_context_by_signature @0x12a0c0`), `encd_set_exec_prings @0x23f9b0`
  (bind the PERSISTENT exec prings), `enc_enq_proxy_tasks @0x105560` (create the
  barrier + network proxy tasks), `hostcc_build_context` + `hostcc_load`.
- **EXEC** — `nrt_cc_schedule @0x7fc60`: `hostcc_exec`, `hostcc_wait_completion`,
  `hostcc_unload`, `encd_free_exec_prings @0x23f850`.
- **DESTROY** — `encd_free_exec_prings`, `enc_stop_operations → encd_stop_executable`,
  teardown. The exec prings live for the communicator's lifetime (set once at
  prepare, freed at destroy) — the persistent ring.

## 3.7 Host-command → NCFW device-dispatch reconciliation

The device side that RECEIVES these commands is the NCFW dispatch spine: a
DRAM+0x00 command-type vector (3 trampolines + default), a DRAM+0xB0 12-entry algo
dispatch table (indexed by `cc_op.algo_type`), the poll source (DRAM+0x10 `soc_addr`
sema bases + DRAM+0x98/+0x188 descriptors), a `waiti`-15 idle, and an idx-3 error
leg. See [main-dispatch-loop](./main-dispatch-loop.md).

| Host control action | Device receiver | Status |
|---------------------|-----------------|--------|
| INIT cfg DMA + `set_init_signal` | reset/boot path + the static DRAM+0x10 base table | RECONCILED (config load) |
| START: EXEC cfg + cc_op DMA + `set_host_trigger` | the command-type vector wakes from `waiti`-15; fetches the next `cc_op`, indexes DRAM+0xB0 by `algo_type` | RECONCILED (kick→dispatch) |
| the `cc_op.algo_type` nibble | the DRAM+0xB0 12-entry table case label | RECONCILED (algo routing) |
| the trigger / EVT_SEM `sema_i` doorbell | the poll source (DRAM+0x10 semas + DRAM+0x98/+0x188 descriptors) | RECONCILED (poll source) |
| STEP: `basic_block_switch` reg write | `switch_completed` + the next-block pring switch | RECONCILED (basic-block step) |
| STOP: `set_stop_signal` / `tpb_stop_sema` | the quiesce path; the idx-3 error leg may also handle abort | PARTIAL (stop vs error leg) |
| completion: CC-status NQ + ack EVT/sema | the device writes the completion-sema CSRs (DRAM+0x10) + the NQ entry; the host reaps | RECONCILED (signal-back) |

> **NOTE — no wire opcode.** The device never receives a tagged-opcode message; it
> INFERS the command class from `cc_op.header.cc_op` + `algo_type` + the
> trigger/switch semaphore that fired. The host writes those fields and the
> poll-source semaphores; the device read runs on the FLIX-undecodable LX core, so
> the host-command → device-vector mapping is INFERRED MED.

---

## Confidence ledger

**HIGH / OBSERVED** (disasm / DWARF / verbatim string / register immediate, this
pass): the `cc_op` decoder bitfields (libncfw @0x1840) and packer
(`create_spad_ctrl_entry @0x232cd0`); `neff_configs` (3592 B) every offset + type +
the +3591 bit split; the EVT_SEM window quartet (read 0x1000 / set 0x1400 / inc
0x1800 / dec 0x1C00, ×4 stride, base 0x8280000000); the M2S/S2M tail doorbell
(0x1038, base `(q+1)<<12`, data 0x10e0, sw_ctrl 0x10b0); `dma_wait_for_completion_handle`
(0xabcdef01); `__post_send`/`__post_recv` directions (recv_sema vs send_sema, net
indices 0x16c/0x168); `dma_ring_get_sema_to_inc` RX gate; `get_channel_send_credit_sema_addr`;
`vring_add_desc_semaphore_inc`; the control-plane doorbell quintet (0x1540/0x1560/
0x15a0=abs 0x615a0/0x15c0/0x15e0=abs 0x615e0, sunda 0x60848); the spad sizes
(0x1000/0x8000); the barrier (296 B) + device-barrier (292 B) +
`basic_block_configs` (1048 B) offsets; the enums (`enc_alg_type`, `enc_alg_mesh_type`,
`enc_op_type`, `SDMA_CCETYPE`, `reduction_type_t`, `encd_neigh`, `notification_type`);
`NRT_STATUS` (DIE `<d6ed>`); `NOTIFICATION_TYPE_TOPSP_CC_STATUS=9`;
`ENGINE_TOP_SP=5`; the qTOPSPinit + mark + signature strings.

**MED / INFERRED-STRONG**: "step N waits step N-1" counted-barrier semantics (the
per-step target values are LX-resident); the on-device EXEC/COMPLETE/RE-ARM walk;
the host↔device `neff_configs` offset mapping by FIELD ROLE; the per-step CC-status
NQ type integers; the host-command → device-vector binding.

**LOW / OPEN / ABSENT**: the concrete runtime `soc_addr` integers for the
trigger/complete semas + spad bases (host-written at prepare/start, not in any
static file); the trailing padded width of a `cc_op` record beyond +0x6; the
on-device LX schedule (the ~26–28 % FLIX mis-decode bound); the entire **MAVERICK
(NC-v5) NCFW image — FILE-ABSENT** (every v5 claim INFERRED/ABSENT).

## See also

- [pring (Persistent DMA Descriptor Ring)](./pring-descriptors.md) — the ring this
  protocol posts into.
- [NCFW spad-ctrl cc_op Table + tsync](./spad-ccop-tsync.md) — the `cc_op` command
  table (keep the bit layout identical).
- [Collective enums](../ops/collective-enums.md) — `cc_op` / reduce-op / `enc_*`
  enum source of truth.
- [Ring & Kangaring](./ring-kangaring.md) · [Mesh collective](./mesh-collective.md)
  · [Hierarchical collective](./hierarchical-collective.md) — the algorithms whose
  steps this protocol carries.
- [NEFF host barrier](./neff-host-barrier.md) · [NEFF device barrier](./neff-device-barrier.md)
  — the barrier sub-schemas.
- [Main dispatch loop](./main-dispatch-loop.md) — the device-side command receiver.
- [TOP_SP lowering](../ops/top-sp-lowering.md) · [Architecture synthesis](../ops/architecture-synthesis.md)
  — the lowering pipeline context.
