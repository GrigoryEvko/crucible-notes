# Send / Recv Primitives and Protocols

> *All addresses on this page apply to `libnccom.so.2.31.24` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`; SONAME `libnccom.so.2`; internal tree **KaenaNCCL**, `/opt/workspace/KaenaNCCL/src`; transport unit `transport/net_neuron.cc`). ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`**; `.text`/`.rodata` VMA == file offset for the cited band, so every `0x…` is both a file offset and an analysis VMA.*
> *Evidence grade: **Confirmed (byte-anchored, with a negative result at its centre)** — the absence of GPU-style device primitives is proven by symbol sweep (`nm -C`) returning zero for `genericOp`/`recvReduceSend`/`directSend`/`cudaLaunchKernel`/`reduceCopy`; the "two-sided, never one-sided write" claim is proven by a `call *0xNN(%rax)` vtable-slot histogram over the whole `net_neuron` band (`0x49000…0x50c00`) finding **zero** `iwrite` (`0x98`) sites. Every struct size/offset is from the binary's own DWARF (`gdb ptype /o`); the `neuronStartNetworkProxy` template-build is read from disassembly (`objdump -d 0x483a0..0x48930`). Other versions will differ. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

This page documents what a reimplementer expects to find and **does not**: there is no GPU-style "primitives" template layer in `libnccom.so`. Upstream NVIDIA NCCL compiles, per architecture, a family of device-side primitive templates — `genericOp`, `recvReduceSend`, `recvReduceCopySend`, `directSend`, `directRecv` — that a CUDA warp kernel instantiates to walk a ring/tree slot at a time, summing chunks in registers and forwarding them. **None of that exists here.** `nm -C libnccom.so | rg 'genericOp|recvReduceSend|directSend|reduceCopy|cudaLaunchKernel'` returns **zero** matches (§1), there is no GPU, and no kernel launch site. The "send/recv primitive" is not a device function at all: on Trainium it **is** the host-side proxy state machine, and the two functions that *are* the primitives are `netNeuronSendProxy` (`0x4e510`) and `netNeuronRecvProxy` (`0x4fe20`) — ordinary host `progress()` callbacks invoked by the proxy loop ([proxy-engine](proxy-engine.md) §4).

A "primitive" in this binary is a **precompiled collective step** that arrives from libnrt as three descriptor FIFOs: `net_ops_info_t[]` (the per-op control blocks — credit pointers, loop sizes, the EXEC_COMPLETE terminator), `net_src_addr_t[]` (the send descriptors — local buffer, size, and the RDMA target `dst_rank`/`dst_addr`/`dst_mhandle`), and `net_dest_addr_t[]` (the recv descriptors). The libnccom export `neuronStartNetworkProxy` (`0x483a0`) validates the FIFOs' EXEC_COMPLETE terminator, builds a **352-byte** `ncclProxyArgs` template per ring channel with the FIFO pointers and counts stored at fixed offsets (`+200`/`+224`), and calls `ncclProxySaveNeuron` (`0x405f0`) twice — once with `type=1` (SEND) and once with `type=0` (RECV) — to register the template into per-stream proxy state keyed by `basic_block_id`. From then on the proxy thread (in libnrt, [proxy-engine](proxy-engine.md) §2) calls each registered arg's `progress` callback, which is exactly `netNeuron{Send,Recv}Proxy`.

The data those primitives move travels through a Neuron-extended `ncclNet_v6_t` plugin vtable ([net-plugin](net-plugin.md)), and the bulk path is **two-sided** `isend`/`irecv`, *not* one-sided `iwrite` — byte-proven: a vtable-slot call histogram over `net_neuron` (`0x49000…0x50c00`) finds `iwrite` (slot `0x98`) at **zero** call sites while `isend` (`0x58`), `irecv` (`0x60`), `iread` (`0xa8`), `iflush` (`0x68`), and `test` (`0x70`) all appear. The **reduction never happens in libnccom** — `redOp` and `dtype` are carried in the `ncclProxyArgs` (`+76`/`+72`) for upstream-ABI parity but no reduce/apply symbol is defined; the Trainium device engine sums the bytes once `nec_inc_semaphore` signals arrival. The page is organized as: (1) the **negative result** — which upstream primitive symbols are gone and the `iwrite`-zero histogram; (2) the three **descriptor-FIFO struct tables** that *are* the primitive's input; (3) the **352-byte `ncclProxyArgs` template** that wraps them; (4) the **`neuronStartNetworkProxy` template-build + register** path; (5) the **128-slot request ring** the two primitives drive (`reqStage`, `pollPosted`, the RDMA-read semaphore ring).

For reimplementation, the contract of this layer is:

- **Build no device kernel.** There is nothing to port from NCCL's `prims_ll.h`/`prims_simple.h`/`common.h` device side. The "primitive" is a host `progress()` callback driven once per `neuronNetworkProxyProgress` tick; the byte movement is the OFI plugin's `isend`/`irecv`/`iread` and the reduce is the device engine's. A reimplementer who re-targets NCCL's device primitives onto Trainium has rebuilt the half this library deliberately deleted.
- **Reproduce the three FIFOs verbatim.** `net_ops_info_t` (96 B), `net_src_addr_t` (72 B), `net_dest_addr_t` (48 B) are the wire format between the compiler/NRT producer and the proxy. Their offsets are DWARF-exact; the EXEC_COMPLETE terminator (`net_addr_mark_t::EXEC_COMPLETE`) on the last entry is asserted by `neuronStartNetworkProxy` and must be present.
- **Build the 352-byte `ncclProxyArgs` template and register it twice.** One SEND (`type=1`), one RECV (`type=0`) per ring channel; the FIFO pointers/counts go at `+200`/`+208` (ops-info) and `+224`/`+232` (addr), the protocol/dtype/redOp at `+64`/`+72`/`+76` (precompiled, never branched on by the proxy).
- **Drive a 128-slot `requestInfo` ring per op.** Each in-flight OFI request is one of 128 `requestInfo` slots (index `= counter & 0x7F`) walking the `reqStage` machine; on TRN2/TRN3 a parallel 128-slot `SemaphoreWrite` ring issues 4-byte RDMA *reads* of the peer's semaphore. Two-sided transfer only — never post `iwrite`.

| | |
|---|---|
| **The primitive (send)** | `netNeuronSendProxy(ncclProxyArgs*)` `0x4e510` — host `progress()` callback, walks src-FIFO, posts `isend`/`iread` |
| **The primitive (recv)** | `netNeuronRecvProxy(ncclProxyArgs*)` `0x4fe20` — pre-post `irecv`, post `irecv`, RDMA-read peer semaphore |
| **Template builder / register** | `neuronStartNetworkProxy` `0x483a0` (export) → `ncclProxySaveNeuron` `0x405f0` ×2 (SEND+RECV) |
| **Op template size** | `ncclProxyArgs` — **352 B** (`0x160`), DWARF `sizeof` confirmed |
| **Three FIFOs** | `net_ops_info_t` **96 B** (`+200`) · `net_src_addr_t` **72 B** (send `+224`) · `net_dest_addr_t` **48 B** (recv `+224`) |
| **Request pool** | `netNeuronAllocRequest` `0x4bf60` — `new(0x68)` tracker, `new(0x3000)`=128×96B `requestInfo`, `new(0x2400)`=128×72B `SemaphoreWrite` |
| **Completion reaper** | `pollPosted` `0x4d300` — `reqStage` machine over the 128-slot ring; `iflush`×2, `test`×5 |
| **Slot picker** | `ringGetPending` `0x4bec0` — nearest pending host-mem index behind `tail_idx` (mod loop size) |
| **Transport pattern** | `enc_pattern_t {ENC_PATTERN_RING=0, ENC_PATTERN_MESH=1, INVALID=2}` (DWARF) — proxy honours this, **not** protocol |
| **Bulk verb** | **two-sided** `isend`(`0x58`)/`irecv`(`0x60`); `iwrite`(`0x98`) = **0 sites** (whole binary) |
| **Sync verb** | `nec_inc_semaphore@NRT_2.0.0` (local engine) + `iread`(`0xa8`, 4 B) of peer semaphore on TRN2/TRN3 |
| **Reduction** | **absent** — no reduce/apply symbol; `redOp`@+76 / `dtype`@+72 carried for ABI, never applied |
| **Device primitive templates** | **ZERO** — `genericOp`/`recvReduceSend`/`directSend`/`reduceCopy`/`cudaLaunchKernel` all `nm -C` empty |

> **QUIRK — there are no device-side primitives, and the proof is symbol-absence plus a verb histogram, not an assertion.** A reimplementer porting NCCL will reach for the device primitive templates — `genericOp` and the `recvReduceSend`/`recvReduceCopySend`/`directSend`/`directRecv` family that a warp kernel instantiates — and for the `reduceCopy` that applies the op. **None is defined in this binary.** `nm -C libnccom.so | rg 'genericOp|recvReduceSend|recvReduceCopySend|directSend|directRecv|reduceCopy|::prims|cudaLaunchKernel|__device_stub'` returns **0**. The send/recv primitive is the host callback pair `netNeuron{Send,Recv}Proxy`; the reduction is performed off-binary by the Trainium device engine after `nec_inc_semaphore` signals the slice arrived. And the transfer is **two-sided**: the vtable-slot histogram over `net_neuron` (`0x49000…0x50c00`) finds `isend`(`0x58`), `irecv`(`0x60`), `iread`(`0xa8`), `iflush`(`0x68`), `test`(`0x70`) — but `iwrite`(`0x98`) and `iwriteInline`(`0xa0`) at **zero** sites, in this band and in the whole `.so`. A reimplementer who builds a one-sided RDMA-write data plane, or who ports the device reduce kernel, has rebuilt the wrong half: this library DMAs and polls; the device sums.

---

## 1. The Negative Result — No Primitive Template Layer

### Purpose

The central claim of this page is structural absence, and absence demands more rigour than presence: it is not enough that one function does not call a primitive — *no* primitive template may exist, and the data path must be shown to move bytes by two-sided messaging rather than one-sided write. This part is that proof, in two independent sweeps: a symbol sweep that finds the upstream primitive/reduce templates are not compiled in at all, and a vtable-slot call histogram that shows which OFI verbs the two host primitives actually issue (and which they never do). Each row is a direct tool observation against the binary.

### Algorithm

```c
// negative-result sweep over libnccom.so.2.31.24  [HIGH: each row is a direct tool observation]
// claim A: no GPU-style device primitive templates / no reduce
// claim B: the host primitives transfer TWO-SIDED (isend/irecv), never one-sided (iwrite)

(A) device-primitive + reduce symbol sweep:                  // nm -C libnccom.so | rg -c <pat>
      genericOp                              -> 0            // the upstream template generator
      recvReduceSend | recvReduceCopySend    -> 0            // the reduce-forward family
      directSend | directRecv                -> 0            // the copy family
      reduceCopy | ::prims | RunWorkElement  -> 0            // the apply + prims dispatch
      cudaLaunchKernel | __device_stub | ncclKernel -> 0     // there is no kernel, no GPU
      FuncSum | applyOp                      -> 0            // no reduction operator anywhere

(B) OFI-verb vtable-slot histogram, per host primitive:      // objdump -d <range> | count 'call *0xNN(%rax)'
    netNeuronSendProxy  0x4e510..0x4fe20:                     // ncclNet_v6_t *ncclNet in %rax
        *0x58 isend   -> 1     *0x90 getMrKey -> 1     *0xa8 iread -> 1
        *0x98 iwrite  -> 0   <-- NEVER                 *0xa0 iwriteInline -> 0  <-- NEVER
    netNeuronRecvProxy  0x4fe20..0x50c00:
        *0x60 irecv   -> 2     *0xa8 iread    -> 1
        *0x98 iwrite  -> 0   <-- NEVER
    pollPosted          0x4d300..0x4e510:
        *0x68 iflush  -> 2     *0x70 test     -> 5
    whole-binary iwrite slot scan:  call *0x98(%rax) -> 0     // not one site, anywhere
```

The `iread` (`0xa8`) sites are *not* a bulk one-sided path: in `netNeuronSendProxy` the single `iread` reads a remote MR when a send descriptor names one (`net_src_addr_t.dst_mhandle` present), and in `netNeuronRecvProxy` the `iread` is the 4-byte semaphore poll of the peer (§5). The bulk operand movement is `isend`/`irecv`; the receiver pulls metadata, the sender pushes data — never the reverse one-sided write upstream NCCL's NVLink/SHARP paths can use.

### Function Map

| Symbol | Addr | Liveness | Evidence | Confidence |
|---|---|---|---|---|
| `netNeuronSendProxy` | `0x4e510` | **LIVE — the send primitive** | host `progress()` callback; `isend`/`iread`/`getMrKey` only | HIGH |
| `netNeuronRecvProxy` | `0x4fe20` | **LIVE — the recv primitive** | host `progress()` callback; `irecv`×2/`iread` | HIGH |
| `genericOp` / `recvReduceSend` / `directSend` / `directRecv` | — | **ABSENT** | `nm -C` 0 matches — no device primitive template compiled in | HIGH |
| `reduceCopy` / `FuncSum` / `applyOp` / `::prims` | — | **ABSENT** | `nm -C` 0 matches — no reduction operator in this binary | HIGH |
| `cudaLaunchKernel` / `__device_stub` / `ncclKernel` | — | **ABSENT** | `nm -C` 0 matches — no GPU, no kernel launch | HIGH |
| `ncclNet_v6_t::iwrite` (slot `0x98`) | — | **NEVER CALLED** | `call *0x98(%rax)` = 0 sites whole binary — two-sided only | HIGH |

### Considerations

The two sweeps close different loopholes. The symbol sweep (A) rules out a *compiled-in* primitive template that might be reached indirectly — there is simply no `genericOp`/`reduceCopy`/`directSend` body in the `.symtab` for any path to call, so the question of *whether* it is called never arises. The verb histogram (B) closes the data-plane loophole: even with the host primitives present, a reimplementer might assume the bulk path is one-sided RDMA write (NCCL's collnet/NVLS style). It is not — `iwrite` (`0x98`) has zero call sites in `net_neuron` and zero in the entire `.so`, while `isend`/`irecv` are the bulk verbs and `iread` is reserved for the dynamic-recv metadata pull and the 4-byte semaphore poll. The histogram is validated by its own internal contrast: the *same* objdump pass that finds zero `iwrite` finds `isend` once and `irecv` twice and `test` five times in `pollPosted`, so the tool is correctly resolving live `call *0xNN(%rax)` slots — the absence of `0x98` is real, not a scan artifact. The reduction's absence (no `FuncSum`/`reduceCopy`) is the third leg: `ncclProxyArgs.redOp`(+76)/`dtype`(+72) are carried but inert here, and the device engine owns the sum ([overview](overview.md) — *the proxy is the primitive*).

---

## 2. The Primitive's Input — Three Descriptor FIFOs

### Purpose

A "primitive" on Trainium is a precompiled collective step, and its concrete representation is three parallel descriptor arrays handed across the libnrt→libnccom seam. `net_ops_info_t[]` is the per-net-op **control** stream — credit pointers, the ring loop size, the histogram/timeout metadata, and the EXEC_COMPLETE terminator. `net_src_addr_t[]` is the **send** descriptor stream — local buffer, size, and the RDMA target triple. `net_dest_addr_t[]` is the **recv** descriptor stream. The two address FIFOs alias the same `ncclProxyArgs.addr_fifo` slot (`+224`): a SEND arg points it at a `net_src_addr_t[]`, a RECV arg at a `net_dest_addr_t[]`. All offsets below are from the binary's own DWARF (`gdb ptype /o`) and are byte-exact.

### Data Tables

`net_ops_info_t` (96 B) — the per-net-op control block; `ops_info_fifo` at `ncclProxyArgs+200`, count at `+208`. The four `inc_*` fields are **pointers** to the device-visible semaphore words `nec_inc_semaphore` bumps, not amounts.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `sema_shift_offset` | +0 | `uint16_t` | per-op semaphore bit/word shift into the engine's sema bank | HIGH |
| `early_send_completion` | +2 | `bool` | report send done before wire-completion (credit pre-release) | HIGH |
| `early_recv_posting` | +3 | `bool` | drives `recvPrePost` (pre-post `irecv` ahead of the op) | HIGH |
| `inc_send_handshake` | +8 | `volatile uint32_t*` | sema word: per-op send-credit handshake | HIGH |
| `inc_send_complete` | +16 | `volatile uint32_t*` | sema word: send slice forwarded (the forward signal) | HIGH |
| `inc_recv_handshake` | +24 | `volatile uint32_t*` | sema word: per-op recv-credit handshake | HIGH |
| `inc_recv_complete` | +32 | `volatile uint32_t*` | sema word: recv slice arrived (consumed by device reduce) | HIGH |
| `tx_entry_cnt` / `rx_entry_cnt` | +40 / +44 | `uint32_t` | descriptor counts this op contributes to the addr FIFO | HIGH |
| `net_idx_loop_size` | +48 | `uint32_t` | the ring loop size passed to `ringGetPending` (mod base) | HIGH |
| `initial_send_credits` | +52 | `uint32_t` | credits posted at first-touch (state==1) | MED |
| `ending_recv_credits` | +56 | `uint32_t` | credits released at op completion | MED |
| `data_type_sz` | +64 | `size_t` | element size (for dynamic-size adjust) | HIGH |
| `is_dynamic_send_recv_sz` | +72 | `bool` | gate: size from `nec_get_dynamic_{send,recv}_*_bytes` | HIGH |
| `variable_peer` | +73 | `bool` | peer rank resolved per-op (mesh/RDH) rather than fixed | HIGH |
| `add_to_histogram` | +74 | `bool` | per-op latency timing gate | HIGH |
| `basic_block_id` | +76 | `uint32_t` | hashtable key (ties the op to its proxy-state block) | HIGH |
| `func_rel_op_idx` | +80 | `uint32_t` | logged as `func_rel_op:%d` in the 10 s watchdog | HIGH |
| `unrolled_count` | +84 | `uint32_t` | completion count gate for advancing `cur_net_op_idx` | HIGH |
| `enc_channel` | +88 | `void*` | the `enc_channel` (pattern/devmem/relay buffers) for this op | HIGH |

`net_src_addr_t` (72 B) — the **send** descriptor; `addr_fifo` at `ncclProxyArgs+224` for a SEND arg. The `dst_*` triple is the RDMA target read/written on the peer.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `net_op_idx` | +0 | `uint32_t` | which `net_ops_info_t` op this descriptor belongs to | HIGH |
| `complete` | +4 | `int` | per-descriptor completion latch | HIGH |
| `dev_addr` | +8 | `dma_addr_t` | device-side (engine) source address | HIGH |
| `host_addr` | +16 | `void*` | host-staging source address (if staged) | HIGH |
| `nccl_mhandle` | +24 | `void*` | local registered-MR handle for the source buffer | HIGH |
| `size` | +32 | `uint32_t` | byte count (overridden by `nec_get_dynamic_send_size_bytes` if dynamic) | HIGH |
| `mark` | +36 | `net_addr_mark_t` | `EXEC_COMPLETE` on the last entry **terminates** the FIFO | HIGH |
| `proxy_histogram_tag` | +40 | `void*` | per-descriptor latency tag | HIGH |
| `dst_rank` | +48 | `int` | RDMA peer rank | HIGH |
| `dst_addr` | +56 | `void*` | remote target address (read/write target on the peer) | HIGH |
| `dst_mhandle` | +64 | `void*` | remote MR handle — its presence selects the `iread` path | HIGH |

`net_dest_addr_t` (48 B) — the **recv** descriptor; the `addr_fifo` slot for a RECV arg. It is `net_src_addr_t` without the `dst_*` RDMA target (a receiver does not name a remote address) and with a `src_rank` instead.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `net_op_idx` | +0 | `uint32_t` | owning `net_ops_info_t` op | HIGH |
| `complete` | +4 | `int` | per-descriptor completion latch | HIGH |
| `dev_addr` | +8 | `dma_addr_t` | device-side (engine) destination | HIGH |
| `host_addr` | +16 | `void*` | host-staging destination (if staged) | HIGH |
| `nccl_mhandle` | +24 | `void*` | local MR handle for the recv buffer | HIGH |
| `size` | +32 | `uint32_t` | byte count (overridden by dynamic-recv if dynamic) | HIGH |
| `mark` | +36 | `net_addr_mark_t` | `EXEC_COMPLETE` terminator | HIGH |
| `src_rank` | +40 | `int` | source rank of the inbound message | HIGH |

> **NOTE — `net_addr_mark_t` is a three-state terminator, and EXEC_COMPLETE is the FIFO's end-of-stream.** The DWARF enum is `net_addr_mark {NET_TRANSFER=0, NET_OP_COMPLETE=1, EXEC_COMPLETE=2}`. A descriptor with `NET_TRANSFER` is an ordinary slice; `NET_OP_COMPLETE` closes one net-op within the stream; `EXEC_COMPLETE` on the **last** descriptor closes the whole basic block and triggers the ring-wrap (clear `addr_fifo_idx`/`cur_net_op_idx`, reset every per-device host-mem `index`). `neuronStartNetworkProxy` asserts the last entry carries it (`"net_src_addr_fifo[net_src_addr_fifo_n - 1].mark == EXEC_COMPLETE"` @`0x8f8b8`, recv twin @`0x8f900`); a FIFO without it would run the proxy off the end of the array (the per-visit bound is the separate `"args->addr_fifo_idx < args->addr_fifo_n"` assert @`0x90960`).

### Considerations

The three FIFOs are the genuine wire format a reimplementer must reproduce, and their split is deliberate: control (`net_ops_info_t`) is amortized over many address descriptors (`tx_entry_cnt`/`rx_entry_cnt` descriptors point back at one op via `net_op_idx`), so a long slice run carries one control block and many cheap 48-/72-byte address records. The `inc_*` fields being **pointers** (not amounts, as a name-only reading might assume) is the key correction: each names a device-visible semaphore word, and `nec_inc_semaphore` bumps `*inc_send_complete` / `*inc_recv_complete` to hand the slice to the engine — the libnrt-side producer fills these pointers from the engine's sema bank ([proxy-driver](../collectives/proxy-driver.md)). The dynamic-size path (`is_dynamic_send_recv_sz`, +72) is the one place `size` (+32) is advisory: when set, the actual bytes come from `nec_get_dynamic_send_size_bytes` / `nec_get_dynamic_recv_offset_bytes` at post time, and the recv side reports the realized count back via `nec_set_recv_size_bytes`. The producer of these FIFOs — where the Neuron compiler emits `dst_rank`/`dst_addr`/`dst_mhandle` and `net_idx_loop_size` — lives outside this binary in libnrt and is the one data-plane gap this page does not close ([proxy-driver](../collectives/proxy-driver.md)).

---

## 3. The Op Template — 352-Byte `ncclProxyArgs`

### Purpose

`ncclProxyArgs` is the per-op record the proxy loop walks — the "primitive" as a runtime object. It is an inherited NCCL struct (the `progress`/`transportComm`/`channel`/`sliceSteps`/`opCount` prefix is stock) extended with a Neuron tail (the FIFO pointers, the ring cursors, the `basic_block_id`, the `proxyType`). One template is built per ring channel and registered twice (SEND + RECV). The size and every offset below are DWARF-exact (`gdb ptype /o`, `sizeof == 352`).

### Data Tables

```text
ncclProxyArgs  (352 B / 0x160) — the per-op "primitive" record  [HIGH: gdb ptype /o]
  off   size  field                  role
  ----  ----  ---------------------  ------------------------------------------------------
  +0    8     progress               fn ptr == transportComm->proxy == netNeuron{Send,Recv}Proxy
  +8    8     transportComm          ncclTransportComm const* (carries proxy/alloc/free fn ptrs)
  +16   8     channel                ncclChannel*
  +24   8     sendbytes  / +32 recvbytes
  +40   4     sliceSteps / +44 chunkSteps / +48 nsteps   (NCCL prefix — vestigial on Neuron)
  +56   8     opCount                flow-control vs comm opCount ceiling
  +64   4     protocol               {LL=0,LL128=1,Simple=2} — PRECOMPILED, never branched on
  +68   4     segment
  +72   4     dtype                  ncclDataType_t — PRECOMPILED, carried for ABI
  +76   4     redOp                  ncclRedOp_t    — PRECOMPILED, NOT applied in libnccom
  +80   4     state                  0=done, 1=first-touch, 2=posting   (proxy gate)
  +88   8     posted / +96 received / +104 transmitted / +112 done / +120 end  (progress counters)
  +128  64    requests[8]            void*[8] — requests[0] = requestTracker*; [1..7] NCCL-legacy, unused
  +192  4     idle                   ANDed into the proxy's *idle accumulator
  +200  8     ops_info_fifo          net_ops_info_t*  [NEURON]  +208 ops_info_fifo_n (int)
  +212  4     cur_net_op_idx  +216 last_net_op_idx  +220 cur_net_op_compl_cnt   (op-walk cursors)
  +224  8     addr_fifo              net_src_addr_t* (SEND) / net_dest_addr_t* (RECV)  [NEURON]
  +232  4     addr_fifo_n            +240 addr_fifo_idx (u64 cursor)  +248 tail_idx (u32 ring tail)
  +252  4     numReq  +256 newReq
  +264  8     pre_post_fifo_idx      [NEURON] recv pre-post cursor (recvPrePost)
  +272  4     proxyType              1=SEND, 0=RECV  (asserted against the progress fn)
  +276  4     basic_block_id         hashtable key (per-stream proxy state)
  +280  40    mutex (pthread_mutex_t)  +320 next  +328 nextPeer  +336 nextGroup  +344 proxyAppendPtr
```

> **CORRECTION (PRIMS-1) — `requests` is an 8-element array, not a single `void**`.** An earlier reading treated `ncclProxyArgs+128` as one `requestTracker*` pointer (`requests`). The DWARF is `void *requests[8]` (64 B at +128). On the Neuron path only `requests[0]` holds the `requestTracker*` that `netNeuronAllocRequest` builds; `requests[1..7]` are the inherited NCCL `NCCL_STEPS` per-step slots and are dead here (the same legacy slack noted in [proxy-engine](proxy-engine.md) §4). A reimplementer must size the field as `void*[8]` for offset fidelity — everything from `+192` onward shifts by 56 bytes otherwise — but only wire `[0]`.

### Considerations

The struct is half inherited, half forked, and the seam is at `+200`. Everything below `+200` (through `idle` at +192) is recognizable stock NCCL `ncclProxyArgs` — the `progress` callback, the `sliceSteps`/`chunkSteps`/`nsteps` slice accounting, the `posted`/`received`/`transmitted`/`done` counters, the `protocol`/`dtype`/`redOp` op descriptor, the intrusive `next`/`nextPeer`/`nextGroup` list links. The Neuron extension is the FIFO block (`ops_info_fifo`@+200, `addr_fifo`@+224) and its cursors (`cur_net_op_idx`, `addr_fifo_idx`, `tail_idx`, `pre_post_fifo_idx`), plus `proxyType`@+272 and `basic_block_id`@+276. The single most consequential field for the no-reduce claim is the trio `protocol`/`dtype`/`redOp` (+64/+72/+76): they are populated by the producer for upstream-ABI parity, but the proxy code never branches on `protocol` and never reads `redOp` for arithmetic — the transport pattern it *does* honour is `enc_pattern_t` (`ENC_PATTERN_RING`/`MESH`), carried on the `enc_channel` (`net_ops_info_t+88`), not the NCCL protocol enum. `state` (+80) is the proxy gate: `1` is first-touch (reset cursors, post initial credits), `2` is the steady post/poll loop, `0` is done (the loop recycles the op, [proxy-engine](proxy-engine.md) §4). The 352-byte size and the free-list pool (`calloc 0xB008` = 8 + 128×352 in `ncclProxySaveNeuron`, `calloc` @`0x407f3`) are byte-confirmed.

---

## 4. Building and Registering the Primitive — `neuronStartNetworkProxy`

### Purpose

`neuronStartNetworkProxy` (`0x483a0`, an exported `T` symbol) is the libnccom-side entry that turns the three FIFOs into two registered `ncclProxyArgs` templates. It validates the EXEC_COMPLETE terminator on each address FIFO, resolves the ring connector and transport comm, builds one 352-byte template with the FIFO pointers/counts written into the Neuron tail, and calls `ncclProxySaveNeuron` twice — `type=1` (SEND) and `type=0` (RECV) — before kicking the proxy with `ncclProxyStart`. It is the producer half of the seam from libnccom's side; libnrt's `enc_network_proxy_task::init_network_proxy` is its caller ([proxy-engine](proxy-engine.md) §3).

### Entry Point

```text
enc_network_proxy_task::init_network_proxy (libnrt) ── per basic block
  └─ neuronStartNetworkProxy(comm, stream, basic_block_id,
                             net_ops_info_t[],n,  net_src_addr_t[],n,  net_dest_addr_t[],n)   0x483a0
       ├─ assert net_src_addr_fifo[n-1].mark == EXEC_COMPLETE          str @0x8f8b8
       ├─ assert net_dest_addr_fifo[n-1].mark == EXEC_COMPLETE         str @0x8f900
       ├─ neuronGetNetConnector(comm, enc_pattern_t, channel, bool)    0x49070  (resolve ring connector)
       ├─ getTransportComm(proxyType, net_ops_info_t*)                 (inlined — pick ncclTransportComm)
       ├─ build 352B ncclProxyArgs template (FIFO ptrs/counts → +200/+224)
       ├─ ncclProxySaveNeuron(tmpl, type=1 SEND, stream, peer, tComm, comm)   0x405f0  (call @0x48530)
       ├─ ncclProxySaveNeuron(tmpl, type=0 RECV, stream, peer, tComm, comm)   0x405f0  (call @0x485af)
       └─ ncclProxyStart(comm, type, stream)                           0x41230  (call @0x485cc)
```

### Algorithm

The build is straight-line: validate, resolve connector + transport comm, populate the template's Neuron tail, register SEND then RECV, start. The two `ncclProxySaveNeuron` call sites (`0x48530`, `0x485af`) and the `ncclProxyStart` call (`0x485cc`) are byte-anchored; the FIFO-pointer stores into `+200`/`+224` are read from the surrounding template fill.

```c
// neuronStartNetworkProxy — 0x483a0  [HIGH: Save/Start call sites + EXEC_COMPLETE asserts read from disasm]
ncclResult_t neuronStartNetworkProxy(ncclComm *comm, uint32 stream, uint32 basic_block_id,
        net_ops_info_t *ops, int ops_n,
        net_src_addr_t  *src, int src_n,           // the SEND descriptor FIFO
        net_dest_addr_t *dst, int dst_n):          // the RECV descriptor FIFO
    // (1) the producer must terminate every address FIFO with EXEC_COMPLETE
    assert(src[src_n - 1].mark == EXEC_COMPLETE);  // 0x8f8b8 — else proxy overruns the array
    assert(dst[dst_n - 1].mark == EXEC_COMPLETE);  // 0x8f900

    // (2) resolve the per-(pattern,channel) ring connector and the matching transport comm.
    //     pattern comes off the op's enc_channel; channel is the ring channel id.
    enc_pattern_t pattern = ops[0].enc_channel->pattern;             // ENC_PATTERN_RING / MESH
    ncclConnector *conn   = neuronGetNetConnector(comm, pattern, channel, /*isSend*/true);  // 0x49070
    const ncclTransportComm *tCommSend = getTransportComm(/*SEND*/1, ops);   // inlined
    const ncclTransportComm *tCommRecv = getTransportComm(/*RECV*/0, ops);

    // (3) build ONE 352-byte template; the FIFO pointers/counts live in the Neuron tail.
    ncclProxyArgs tmpl = {0};
    tmpl.channel        = &comm->channels[channel];
    tmpl.basic_block_id = basic_block_id;          // +276 — the per-stream hashtable key
    tmpl.protocol = ops[0].protocol;               // +64  — carried, never branched on
    tmpl.dtype    = ops[0].dtype;  tmpl.redOp = ops[0].redOp;   // +72/+76 — ABI parity, not applied
    tmpl.ops_info_fifo   = ops;  tmpl.ops_info_fifo_n = ops_n;  // +200 / +208

    // (4) register SEND (proxyType=1) then RECV (proxyType=0). Save copies tComm->proxy
    //     into tmpl.progress (== netNeuron{Send,Recv}Proxy), points addr_fifo at the right
    //     FIFO, allocates the requestTracker, and chains the arg under basic_block_args_map.
    tmpl.addr_fifo = src;  tmpl.addr_fifo_n = src_n;            // +224 / +232  (SEND view)
    ncclProxySaveNeuron(&tmpl, /*type*/1, stream, peer, tCommSend, comm);   // 0x405f0 @0x48530
    tmpl.addr_fifo = dst;  tmpl.addr_fifo_n = dst_n;            // +224 / +232  (RECV view)
    ncclProxySaveNeuron(&tmpl, /*type*/0, stream, peer, tCommRecv, comm);   // 0x405f0 @0x485af

    // (5) raise the comm opCount ceiling and wake the proxy for this stream.
    ncclProxyStart(comm, /*type*/ , stream);       // 0x41230 @0x485cc
    return ncclSuccess;

// ncclProxySaveNeuron — 0x405f0  [HIGH: pool calloc + AllocRequest read from disasm]
ncclResult_t ncclProxySaveNeuron(ncclProxyArgs *tmpl, int type, uint32 stream, uint32 peer,
                                 const ncclTransportComm *tComm, ncclComm *comm):
    args = pop_free_arg();                         // from ncclProxyPool (calloc 0xB008 = 8+128*352) @0x407f3
    *args = *tmpl;                                 // copy the 352-byte template
    args->progress  = tComm->proxy;                // == netNeuron{Send,Recv}Proxy
    args->proxyType = type;                        // +272 — 1=SEND / 0=RECV (asserted vs progress fn)
    if (tComm->allocRequest):
        tComm->allocRequest(args);                 // netNeuronAllocRequest 0x4bf60 — §5
    args->state = 1;                               // first-touch
    append args under state->basic_block_args_map[args->basic_block_id];
    return ncclSuccess;
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `neuronStartNetworkProxy` | `0x483a0` | export: validate FIFOs, build template, register SEND+RECV, start | HIGH |
| `ncclProxySaveNeuron` | `0x405f0` | pop free arg, copy template, bind `progress`/`proxyType`, `allocRequest`, chain by bb | HIGH |
| `ncclProxyStart` | `0x41230` | raise opCount ceiling; wake the proxy thread for this stream | HIGH |
| `neuronGetNetConnector` | `0x49070` | resolve the `(enc_pattern_t, channel)` ring connector | HIGH |
| `getTransportComm` | (inlined) | pick the `ncclTransportComm` (its `proxy`/`allocRequest`/`freeRequest` fn ptrs) by proxyType | MED |

### Considerations

The "build once, register twice" shape is the whole reason there is no separate send-kernel and recv-kernel: one template carries both the SEND and RECV view of a channel's step, and `ncclProxySaveNeuron` specializes it by overwriting `progress` (from `tComm->proxy`) and `proxyType`, and by pointing `addr_fifo` at the `net_src_addr_t[]` for the SEND arg and the `net_dest_addr_t[]` for the RECV arg. The `proxyType` field (+272) is asserted against the resolved `progress` fn — a mismatch trips `"progress function does not match any proxy type"` (@`0x90440`) inside `netNeuronAllocRequest` — so a reimplementer cannot register a SEND arg with the recv callback. The `allocRequest` hook is where the 128-slot request ring is born (§5), called from inside `Save` so every registered arg arrives with its `requestTracker` already built. The producer-side resolution of `peer` and the per-op `getTransportComm` selection are inlined and read MED (the connector/transport-comm bodies are not standalone symbols); what is byte-exact is the two `Save` calls (`0x48530`/`0x485af`), the `Start` call (`0x485cc`), the EXEC_COMPLETE asserts, and the FIFO-pointer offsets.

---

## 5. The Request Ring — `reqStage`, `pollPosted`, and the Semaphore Read

### Purpose

Once registered, each primitive drives a 128-slot OFI request ring: every in-flight `isend`/`irecv` occupies one `requestInfo` slot (index `= counter & 0x7F`), and the slot walks a `reqStage` state machine from post to completion. `pollPosted` (`0x4d300`) is the reap half — it advances slots through the machine, issues `iflush` for GDR ordering and `test` to poll. On TRN2/TRN3 a *parallel* 128-slot `SemaphoreWrite` ring issues 4-byte RDMA **reads** of the peer's semaphore (peer-arrival polling). This part fixes the ring's data structures and the masking discipline; the per-state send/recv post loop bodies are summarized, not re-transcribed (they are the two primitives' bodies, op-level in [proxy-engine](proxy-engine.md)).

### Data Tables

`requestTracker` (104 B) — the per-op request pool; `requests[0]` of the `ncclProxyArgs` points here, built by `netNeuronAllocRequest` (`0x4bf60`).

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `requests` (vector) | +0 | `std::vector<requestInfo>` | backing `new(0x3000)` = 128 × 96 B `requestInfo` | HIGH |
| `semaphoreWrites` (vector) | +24 | `std::vector<SemaphoreWrite>` | backing `new(0x2400)` = 128 × 72 B (TRN2/TRN3 RECV only) | HIGH |
| `semaphoreWritePending` | +48 | `uint64_t` | producer counter; slot `= &0x7F` | HIGH |
| `semaphoreWritePosted` | +56 | `uint64_t` | `iread`-issued counter | HIGH |
| `semaphoreWriteCompleted` | +64 | `uint64_t` | `test()`-reaped counter | HIGH |
| `addr_fifo_n` | +72 | `int` | descriptor count (mirror) | HIGH |
| `sidelink_present` | +76 | `bool` | `= neuronIsTRN2Family() \|\| neuronIsTRN3Family()`; gates the `iread` semaphore path | HIGH |
| `flushEnabled` | +80 | `bool` | GDR `iflush` gate in `pollPosted` | HIGH |
| `recv_count_tracker` | +88 | `void*` | target for `nec_set_recv_size_bytes` | HIGH |

`requestTracker::requestInfo` (96 B) — one in-flight OFI request (one ring slot).

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `ofi_req` | +0 | `void*` | live OFI request handle; `NULL` = slot free | HIGH |
| `state` | +8 | `reqStage` | the per-slot state machine value | HIGH |
| `ofi_comm` | +16 | `void*` | the OFI send/recv comm for this verb | HIGH |
| `addr` | +24 | `void*` | buffer addr for `iflush` | HIGH |
| `size` | +32 | `int` | byte count (from completion or dynamic-send size) | HIGH |
| `mhandle` | +40 | `void*` | registered-MR key | HIGH |
| `completeReport` | +48 | `int` | credit/completion accumulated into the args | HIGH |
| `net_op_idx` | +52 | `uint32_t` | which `net_ops_info_t` op produced this request | HIGH |
| `rank` | +56 | `int` | recv: source rank | HIGH |
| `comm` / `channel` | +64 / +72 | `ncclComm const*` / `enc_channel const*` | asserted non-null in `pollPosted` | HIGH |
| `histograms` / `histogram_tag` | +80 / +88 | `multi_timer_histograms*` / `std::string*` | per-op latency timing | HIGH |

`requestTracker::SemaphoreWrite` (72 B) — one remote-semaphore RDMA **read** (TRN2/TRN3 RECV only). Despite the name, the verb posted is `iread(…, 4, …)`.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `ofi_comm` | +0 | `void*` | the OFI comm for the read | HIGH |
| `ofi_req` | +8 | `void*` | the `iread` request; `NULL` until posted | HIGH |
| `src_buffer` | +16 | `void*` | local 4-byte landing buffer for the peer's semaphore value | HIGH |
| `src_key` | +24 | `uint64_t` | local MR key (from `getMrKey`) | HIGH |
| `dst_buffer` | +32 | `void*` | remote semaphore address to read | HIGH |
| `dst_mhandle` | +40 | `void*` | remote MR handle | HIGH |
| `addr_fifo_idx` | +48 | `uint64_t` | cursor into the recv addr FIFO | HIGH |
| `comm` / `channel` | +56 / +64 | `ncclComm const*` / `enc_channel const*` | owning comm/channel | HIGH |

### Algorithm

```c
// reqStage — DWARF enum (transport/net_neuron.cc:76); per-slot OFI state machine  [HIGH]
//   NONE=0  RECV=1  RECV_DONE=2  FLUSH=3  SEND=4  EMPTY=5  DONE=6  ERROR=7
// recv path:  NONE -> RECV -(test)-> RECV_DONE -(iflush, if flushEnabled)-> FLUSH -(test)-> DONE
// send path:  NONE/EMPTY -> SEND -(test)-> DONE       (EMPTY = zero-size, no OFI request)

// pollPosted — 0x4d300  [HIGH: reqStage asserts + iflush/test verb histogram (iflush x2, test x5)]
void pollPosted(const ncclProxyArgs *args, requestTracker &rt, int *completed, uint32 *report):
    for (slot_idx = (done & 0x7F); done < posted; ):          // walk from done cursor to posted
        requestInfo *r = &rt.requests[slot_idx];              // 128-slot ring, index & 0x7F
        switch (r->state):
          case RECV:
            if (ncclNet->test(r->ofi_req, &complete, &r->size)):    // *0x70(%rax)
                if (rt.flushEnabled):
                    ncclNet->iflush(r->ofi_comm, 1, &r->addr, &r->size, r->mhandle, &r->ofi_req);  // *0x68
                    r->state = FLUSH;                          // RECV_DONE -> FLUSH
                else: r->state = DONE;
          case FLUSH:
            if (ncclNet->test(r->ofi_req, &complete, NULL)):   // *0x70(%rax)
                r->state = DONE;
          case SEND:
            if (ncclNet->test(r->ofi_req, &complete, NULL)): r->state = DONE;
          case DONE: case EMPTY:
            *completed += 1;  *report += r->completeReport;    // credit back into the args
            r->ofi_req = NULL;  r->state = NONE;               // free the slot
            advance done;  slot_idx = (done & 0x7F);
    // asserts: "reqInfo->state == reqStage::RECV_DONE" / "::FLUSH" / "::NONE"  (state-machine guards)

// netNeuronAllocRequest — 0x4bf60  [HIGH: new sizes byte-anchored]
void netNeuronAllocRequest(ncclProxyArgs *args):
    requestTracker *rt = new(0x68);                            // 0x4c00b mov $0x68 — 104 B tracker
    rt->requests.reserve(128);  rt->requests.backing = new(0x3000);   // 0x4c02d — 128 * 96 B
    rt->sidelink_present = neuronIsTRN2Family() || neuronIsTRN3Family();   // 0x45fe0 / 0x45e30
    if (args->proxyType == RECV && rt->sidelink_present):
        rt->semaphoreWrites.backing = new(0x2400);            // 0x4c0d1 — 128 * 72 B SemaphoreWrite
    if (!progress_matches_proxyType(args)):
        assert_fail("progress function does not match any proxy type");   // 0x90440
    args->requests[0] = rt;
```

> **CORRECTION (PRIMS-2) — `enc_pattern_t` is zero-based: `ENC_PATTERN_RING = 0`, `ENC_PATTERN_MESH = 1`.** An earlier reading numbered the transport pattern `RING = 1`, `MESH = 2`. The binary's DWARF enum is `enc_pattern {ENC_PATTERN_RING=0, ENC_PATTERN_MESH=1, ENC_PATTERN_INVALID=2}` (`gdb ptype enc_pattern_t`). The proxy's pattern assert is `"(pattern == ENC_PATTERN_RING) || (pattern == ENC_PATTERN_MESH)"` (@`0x909b0`) — i.e. `pattern ∈ {0, 1}`, `INVALID(2)` excluded. A reimplementer who encodes `RING=1`/`MESH=2` mis-tags every `enc_channel.pattern` and trips the assert. (This is the proxy-honoured pattern enum — distinct from the dead NCCL `ncclPattern_t` and the topology `NCCL_TOPO_PATTERN_*`, see [algorithm-tree](algorithm-tree.md) CORRECTION ATREE-1.)

### Considerations

The ring discipline is the same `& 0x7F` masking on every counter — `posted`/`done` for the request ring, `semaphoreWritePending`/`Posted`/`Completed` for the semaphore ring — with the pool sizes (`0x3000`/`0x2400`) byte-pinned in `netNeuronAllocRequest` at `0x4c02d`/`0x4c0d1`. The `SemaphoreWrite` ring is the one piece of genuinely Trainium-specific transport: its name says "Write" but the posted verb is `iread(…, 4, …)` — a 4-byte RDMA **read** of the peer's semaphore word, i.e. arrival polling, gated on `sidelink_present` (`neuronIsTRN2Family || neuronIsTRN3Family`). On TRN1 the path is absent and arrival is signalled by a plain local `nec_inc_semaphore`; only on the sidelink families does the receiver actively read the peer's semaphore before bumping the local engine. The `flushEnabled` gate (`requestTracker+80`) decides whether a completed `irecv` takes the extra `iflush`→`FLUSH`→`test`→`DONE` detour (GDR completion ordering for device-direct buffers) or shortcuts straight to `DONE` (host-staged buffers). The forward step — handing the arrived slice to the device reduce — is the `nec_inc_semaphore` on `*inc_recv_complete` that closes the recv primitive; the reduce itself runs off-binary on the engine ([overview](overview.md)). The `reqStage` enum body is named in DWARF (`transport/net_neuron.cc:76`) and its eight values are read from the `pollPosted`/`recvProxy` asserts (`RECV_DONE`/`FLUSH`/`NONE`/`EMPTY`); the `pollPosted` `iflush`×2 / `test`×5 verb counts are the byte-anchored histogram from §1.

---

## Related Components

| Name | Relationship |
|---|---|
| inherited NCCL device primitives (`genericOp`, `recvReduceSend`, `prims_*`) | **ABSENT** from this fork — the layer this page proves does not exist; replaced by host proxy callbacks |
| `netNeuron{Send,Recv}Proxy` (`0x4e510`/`0x4fe20`) | the two host functions that **are** the send/recv primitives; their op-level loop is in [proxy-engine](proxy-engine.md) §4 |
| `ncclNet_v6_t` plugin (`isend`/`irecv`/`iread`/`iflush`/`test`) | the OFI/EFA vtable the primitives call; `iwrite` slot never used ([transport-efa](transport-efa.md), [net-plugin](net-plugin.md)) |
| libnrt `enc_network_proxy_task::init_network_proxy` | the producer that fills the three FIFOs and calls `neuronStartNetworkProxy` ([proxy-driver](../collectives/proxy-driver.md)) |
| Trainium device reduce engine | performs the reduction libnccom only DMAs into; consumes `*inc_recv_complete` ([overview](overview.md)) |

## Cross-References

- [Proxy & Progress Engine (and the libnrt Bridge)](proxy-engine.md) — the op-FIFO loop (`ncclProxyProgressOps`) that calls `op->progress()` == these primitives once per tick; the recycle/basic-block discipline that re-arms the `requestTracker` this page builds
- [Ring Algorithm Construction (Host-Side)](algorithm-ring.md) — the wiring (`comm->channels[].ring`) whose ring neighbours these primitives post to; the `enc_pattern_t` RING/MESH that the proxy honours instead of NCCL protocol
- [Bananaphone IPC and the Proxy Driver](../collectives/proxy-driver.md) — the libnrt-side producer that fills `net_ops_info_t`/`net_src_addr_t`/`net_dest_addr_t` (incl. `dst_rank`/`dst_addr`/`dst_mhandle` and `net_idx_loop_size`) — the one data-plane gap this page does not close
- [Inter-Node Transport: EFA / libfabric](transport-efa.md) — the `ncclNet_v6_t` verb plugin the primitives drive; the `iread` RDMA-read peer-semaphore path on TRN2/TRN3 detailed end to end
- [back to index](../index.md)
