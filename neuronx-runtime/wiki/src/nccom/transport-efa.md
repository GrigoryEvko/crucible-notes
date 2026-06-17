# Inter-Node Transport: EFA / libfabric

> *All addresses on this page apply to `libnccom-net.so` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `3415f096d479e7d7bef506bb68bc0fa7551a654a`; SONAME `libnccom-net.so`). ELF64 x86-64, **DYN, NOT stripped** — full `.symtab`, 322 `t`/`T` text functions; `.text`/`.rodata` VMA == file offset, so every `0x…` is both a file offset and an analysis VMA. The transport reaches the NIC through `DT_NEEDED libfabric.so.1` (versioned symbols `FABRIC_1.0`/`1.1`/`1.8`) and reads the PCIe topology through `DT_NEEDED libhwloc.so.15`. Source-file evidence in the binary (demangled symbols / assert strings): `nccl_ofi_rdma.cpp`, `nccl_ofi_net.cpp`, `nccl_ofi_freelist`, `nccl_ofi_mr_cache`, `nccl_ofi_idpool`, `nccl_ofi_scheduler`, `nccl_ofi_topo`, `ofiutils`.*
> *Evidence grade: **Confirmed (byte-anchored)** — build-id, SONAME, NEEDED, libfabric versioned imports, and the 322-function `.symtab` re-read from the binary; the libfabric op-vector slots (`fi_writedata`/`fi_cq_read`/`fi_cq_readerr`/`fi_cq_strerror`), the FI_REMOTE_WRITE flag bit, the immediate-data shift constants, the access mask, and the 128-inflight cap read directly from `objdump -d` of `send_progress`/`ofi_process_cq_rail`/`reg_mr`/`send`. Struct field offsets are from decompile of the same functions; the demangled symbol signatures (`nccl_net_ofi_rdma_req*`, `nccl_net_ofi_rdma_domain*`, `nccl_ofi_mr_ckey const*`) corroborate the type names. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

`libnccom-net.so` is the **inter-node** half of the Neuron collective transport: the EFA/libfabric network plugin that `libnccom.so` dlopens to move a collective's bytes across the AWS fabric between servers. It is a clean-room fork of **aws-ofi-nccl** — the "AWS Libfabric" NCCL net plugin (the name string lives at `0x3b470`) — specialized to the **EFA** libfabric provider: `nccl_ofi_ofiutils_get_providers` (`0x26b80`) sets `provider_filter = "efa"` (`"Setting provider_filter to efa"`, `0x3ac80`) and rejects fabrics older than 1.22.0 (`"EFA provider requires at least libfabric version 1.22.0."`, `0x30288`). If you already hold a mental model of upstream aws-ofi-nccl's RDMA protocol — a per-NIC libfabric endpoint, a per-rail completion queue, RDMA-WRITE-with-immediate as the bulk verb, a receiver-driven control message that advertises the remote address+rkey, and a CQ-polling progress function — then you already know this plugin. The fork keeps that protocol nearly intact and re-homes the *driver loop* in libnrt (see [proxy-engine](proxy-engine.md)); this `.so` provides the per-op data path it drives.

The plugin exports the classic NCCL net-plugin ABI as three C structs — `ncclNetPlugin_v4`/`_v5`/`_v6` (in `.data` at `0x43760`/`0x437e0`/`0x438a0`) — whose function pointers `libnccom.so` resolves by `dlsym("ncclNetPlugin_v6")` and binds slot-for-slot ([net-plugin](net-plugin.md)). Behind those shims the plugin ships **two** transport implementations selected at init by `OFI_NCCL_PROTOCOL` (`ofi_nccl_protocol`, `0x295d0`): the rich **RDMA** protocol (`nccl_ofi_rdma.cpp`) — multi-rail RDMA-WRITE-with-immediate, eager send, control messages, a 6-state connection machine — and a simpler **SENDRECV** tagged `fi_tsend`/`fi_trecv` path (`nccl_ofi_sendrecv.cpp`). **This page owns the RDMA fast path**, which is the EFA production transport; SENDRECV is referenced only as its contrast.

The page is organized as the per-op data path a reimplementer must reproduce. After the at-a-glance table and the rail/req/cq struct tables, two `### Algorithm` units carry the heart of it: **`send_progress`** (`0x12850`) — the post side, which fans a request across rails as `fi_writedata` (RDMA-WRITE+imm), `fi_senddata` (eager), or `fi_writemsg`, and queues on `-FI_EAGAIN`; and **`ofi_process_cq_rail`** (`0x16b40`) — the harvest side, which drains each rail's CQ with `fi_cq_read`, dispatches FI_REMOTE_WRITE immediate-data completions by decoding `(rkey<<28)|(comm_id<<10)|seq`, and routes everything else through a per-request context vtable. The connection lifecycle, MR cache, freelist, idpool, and multi-rail scheduler are documented as the supporting structure around those two functions.

For reimplementation, the contract of this transport is:

- **The per-rail QP-equivalent triple.** EFA exposes no raw verbs QP to the plugin; the role is played by a `(fid_ep, fid_av, fid_cq)` per "rail," grouped under a comm. A reimplementer must build the rail array (`fid_ep`+remote `fi_addr_t` per data rail; `fid_domain`+`fid_cq` per domain rail) and stripe one transfer across all data rails.
- **RDMA-WRITE-with-immediate as the bulk verb, two-sided control.** The sender posts `fi_writedata` (libfabric `fid_ep->rma_ops + 0x30`) carrying a 64-bit immediate; the receiver pre-advertises its target address+rkey via a SEND_CTRL control message on the control rail. The immediate word `(rkey<<28)|(comm_id<<10)|seq` is the entire demux key the receiver uses to retire the message — there is no payload header.
- **CQ-driven progress, no thread.** Forward progress is one `ofi_process_cq` pass per `progress()` call; the function drains every rail's `fid_cq` via `fi_cq_read`, splits FI_REMOTE_WRITE (flag bit `0x20`) immediate completions from context-dispatched ones, and counts completions into each request until `ncompls == expected`.
- **The four support structures** — `nccl_ofi_freelist` (request pool), `nccl_ofi_mr_cache` (registration cache), `nccl_ofi_idpool` (mr-key allocator), `nccl_ofi_scheduler` (rail-striping threshold policy) — and the EAGAIN-deferral discipline that queues a post on the endpoint's `pending_reqs` deque rather than spinning.

## At a glance

| | |
|---|---|
| **What it is** | Inter-node EFA/libfabric net plugin; fork of **aws-ofi-nccl**, RDMA protocol |
| **Plugin name** | `"AWS Libfabric"` (`0x3b470`); provider `"efa"`; libfabric ≥ 1.22.0 |
| **Exported ABI** | `ncclNetPlugin_v6` `.data 0x438a0` (·`_v5 0x437e0` ·`_v4 0x43760`) — bound by `libnccom.so` dlsym |
| **Init top** | `nccl_net_ofi_create_plugin` `0xabe0` → `nccl_net_ofi_rdma_init` `0x171d0` (`OFI_NCCL_PROTOCOL == "RDMA"`) |
| **Post (isend impl)** | `send` `0x1c980` → `send_progress` `0x12850` — `fi_writedata` / `fi_senddata` / `fi_writemsg` |
| **Harvest** | `ofi_process_cq` `0x1ab80` → `ofi_process_cq_rail` `0x16b40` — `fi_cq_read` loop + dispatch |
| **Control msg** | `recv` `0x1dbb0` → `send_ctrl_post` `0x12780` — SEND_CTRL advertises addr+rkey |
| **Connect** | `connect` `0x1ac00` — 6-state machine (handle+80, states 0..5); AV-insert via `fid_av->ops + 8` |
| **MR register** | `reg_mr` `0x15e40` — per-rail `fi_mr_regattr` (`fid_domain->mr_ops + 0x18`); access mask `0x3F00` |
| **Immediate word** | `(rkey << 28) | (comm_id << 10) | seq` — `shl $0x1c` / `shl $0xa` at `0x1cba0`/`0x1cbb3` |
| **Inflight cap** | **128** — `cmpq $0x80,0x48(%rdi)` at `0x1c9ce` (comm `num_inflight_reqs`) |
| **Request size** | `nccl_net_ofi_rdma_req_t` ≈ **528 B** (freelist init `528/16/16/128`) |
| **Support structs** | freelist `0x26560` · mr_cache `0x24d20` · idpool `0x265b0` · scheduler `0x22af0` |
| **NIC boundary** | `libfabric.so.1` — 6 versioned PLT syms + per-`fid` inline op vectors (`call *N(%rax)`) |
| **EFA PCI IDs** | `0xefa0` / `0xefa1` / `0xefa2` |

> **QUIRK — there is no QP, and the immediate word *is* the message header.** A reimplementer porting from verbs-style RDMA will look for a queue pair, a receive descriptor with an inline header, and a payload prefix that says "this is for connection X, sequence Y." None exists. The transport object is the *comm* (`send_comm`/`recv_comm`); the QP/CQ role is split across a `(fid_ep, fid_av, fid_cq)` per rail. The bulk write carries **no header** — the entire demux is the 64-bit immediate `(rkey<<28)|(comm_id<<10)|seq` (`send` `0x1c980`: `shl $0x1c` then `shl $0xa` then `or`, `0x1cba0`–`0x1cbb8`). On the harvest side, a FI_REMOTE_WRITE completion (flag bit `0x20` at `cq_entry+9`, `testb $0x20,0x9(%r14)` `0x16ca4`) carries only that immediate; the receiver re-derives `comm_id` to find the comm, `seq` to `msgbuff_retrieve` the request, and `rkey` for bookkeeping. Drive the data plane off a payload header and you have rebuilt a protocol this fork deliberately does not use.

---

## 1. The QP-Equivalent: Rails, Comms, and the libfabric Boundary

### Purpose

EFA does not hand the plugin a verbs queue pair. The connection abstraction is the **comm** (`send_comm`/`recv_comm`), and the QP/CQ/AV role is played by a per-**rail** triple of libfabric objects. A "rail" is one EFA endpoint path to the peer; multi-rail striping (one transfer fanned across several rails) is how the plugin saturates an EFA NIC's parallel SRD queues. This unit fixes the three layers a reimplementer must build — the endpoint's *rails*, the domain's *rails*, and the comm that owns them — and the exact libfabric op-vector slots the data path reaches through.

### The three rail layers

```text
nccl_net_ofi_rdma_ep_t  (connect@0x1ac00, post_rx_buffs_on_rail@0x181d0)
  +48  num_rails (u16)              +50  num_control_rails (u16)
  +56  data rails    ──► ep_rail[]  (stride 0xA0)   ── one libfabric fid_ep per rail
  +64  control rails ──► ep_rail[]  (stride 0xA0)   ── control-message endpoints
  +80  pending_reqs  ──► std::deque<rdma_req*>      ── EAGAIN-deferred posts
  +88  pending_reqs lock (pthread_mutex)
  +152 domain* ──► nccl_net_ofi_rdma_domain_t

nccl_net_ofi_rdma_domain_t  (reg_mr@0x15e40)
  +24  mr_cache*    (nccl_ofi_mr_cache_t)
  +32  mr_key idpool* (nccl_ofi_idpool_t)
  +128 num_rails (u16)
  +136 domain rails ──► domain_rail[]  (stride 0x18) ── fid_domain + fid_cq per rail
       │
       ▼  domain_rail:  +0 rail_id   +8 fid_domain*   +0x18 fid_cq*   (fi_cq_read target)

nccl_net_ofi_rdma_send_comm_t  (calloc 0xE8; connect@0x1ac00)
  +0x48 (72)  num_inflight_reqs  (==128 cap, cmpq $0x80 @0x1c9ce)
  +0x58 (88)  req freelist*       +0x64 (100) comm_id (u64; <<10 into imm)
  +0x78 (120) next_msg_seq_num (u16, &0x3FF)   +0x80 (128) msgbuff*
  +0x90 (144) comm mutex          +0xD0 (208) comm_active byte (checked top of send)
  +0xD8 (216) data rails  ──► send_comm_rail[]  (stride 0x10: +0 fid_ep*, +8 remote fi_addr_t)
  +0xE0 (224) control rails ──► send_comm_rail[]  (stride 0x10)
```

### Struct tables — the rail / comm / domain layout

`nccl_net_ofi_rdma_ep_rail` (stride `0xA0` = 20 qwords). The data-path field is the `fid_ep` at `+8`; the rest are the rx-buffer refill bookkeeping that `post_rx_buffs_on_rail` (`0x181d0`) drives.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `rail_id` / ep base | +0 | u64 | rail index / endpoint base | HIGH |
| `fid_ep` | +8 | `struct fid_ep*` | libfabric endpoint for this rail (post target) | HIGH |
| `rx_buff_posted` | +88 | u64 | count of rx buffers currently posted | MED |
| `rx_buff_target` | +96 | u64 | desired posted-buffer high-water | MED |
| `rx_buff lock` | +104 | mutex | guards the refill counters | MED |
| `alloc_rx_buff_req` | +152 | fn ptr | `{eager,ctrl}_rx_buff_req_alloc` — refill callback | HIGH |

`nccl_net_ofi_rdma_domain_rail` (stride `0x18`). This is the CQ side of the QP-equivalent: each rail polls its own `fid_cq`.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `rail_id` | +0 | u64 | rail index | HIGH |
| `fid_domain` | +8 | `struct fid_domain*` | libfabric domain; `mr_ops + 0x18` = `fi_mr_regattr` | HIGH |
| `fid_cq` | +0x18 | `struct fid_cq*` | completion queue; `cq_ops + 8` = `fi_cq_read` | HIGH |

> **CORRECTION (EFA-1) — `fid_cq` sits at `domain_rail + 0x18`, not `+16`.** An earlier structural reading placed the per-rail `fid_cq` at `+16` inside an `0x18`-stride `domain_rail`. The `ofi_process_cq_rail` prologue reads it at `+0x18`: `mov 0x18(%rdi),%rax ; call *0x8(%rax)` (`0x16bfc`/`0x16c03`), i.e. `fid_cq = *(domain_rail + 0x18)`, then `fi_cq_read = cq->cq_ops + 8`. The `fid_domain` is at `+8` (`reg_mr` `0x15e40` reads `mr_ops` off `domain_rail+8`). The stride is the *array* stride `0x18`; the `fid_cq` is the last 8-byte slot of each entry. A reimplementer who places `fid_cq` at `+16` polls a misaligned pointer.

`send_comm_rail` / `recv_comm_rail` (stride `0x10`). The minimal post unit: an endpoint plus the resolved remote address.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `fid_ep` | +0 | `struct fid_ep*` | the rail's endpoint (RMA/MSG ops live here) | HIGH |
| `remote_addr` | +8 | `fi_addr_t` | AV-inserted remote address (`fi_av_insert` result) | HIGH |

### The libfabric op-vector boundary

The plugin reaches libfabric two ways. Discovery/teardown go through **6 versioned PLT symbols**; the per-object data path goes through **inline op vectors** — function pointers inside each `fid` struct, called `call *N(%rax)`, libfabric's standard inline-ops design. The op-vector slots below are read directly from `objdump -d` (the offset in `call *0xNN(%rax)`, where `%rax` is the relevant `ops` table).

| libfabric op | Reached via | Slot | Call site (verified) | Confidence |
|---|---|---|---|---|
| `fi_getinfo` / `fi_fabric` / `fi_dupinfo` / `fi_freeinfo` / `fi_version` / `fi_strerror` | PLT (`FABRIC_1.0`/`1.1`/`1.8`) | — | `nm -D` versioned imports | HIGH |
| `fi_writedata` (RDMA-WRITE+imm) | `fid_ep->rma_ops` | `+0x30` | `send_progress` `0x129c4` `call *0x30(%rax)` | HIGH |
| `fi_senddata` (eager) | `fid_ep->msg_ops` | `+0x20` | `send_progress` `0x12a74` `call *0x20(%rax)` | HIGH |
| `fi_writemsg` | `fid_ep->rma_ops` | `+0x40` | `send_progress` `0x12b87` `call *0x40(%rax)` | HIGH |
| `fi_recv` / inject | `fid_ep->msg_ops` | `+0x18` | `send_progress` `0x12c4d` `call *0x18(%rax)` | HIGH |
| `fi_cq_read` | `fid_cq->cq_ops` | `+0x8` | `ofi_process_cq_rail` `0x16c03` `call *0x8(%rax)` | HIGH |
| `fi_cq_readerr` | `fid_cq->cq_ops` | `+0x40` | `ofi_process_cq_rail` `0x16c85` `call *0x40(%rax)` | HIGH |
| `fi_cq_strerror` | `fid_cq->cq_ops` | `+0x18` | `ofi_process_cq_rail` `0x16ee4` `call *0x18(%rax)` | HIGH |
| `fi_mr_regattr` | `fid_domain->mr_ops` | `+0x18` | `reg_mr` `0x15e40` `call *0x18(%rax)` | HIGH |
| `fi_av_insert` | `fid_av->av_ops` | `+0x8` | `connect`/`finish_connect` AV-insert | MED |

> **CORRECTION (EFA-2) — the CQ ops slots are `+0x40`/`+0x18`, not `+0x18`/`+0x38`.** A by-name inference had `fi_cq_readerr` at `cq_ops+24` and `fi_cq_strerror` at `cq_ops+56`. The disassembly of `ofi_process_cq_rail` is unambiguous: after `fi_cq_read` at `cq_ops+0x8` (`0x16c03`), the `-FI_EAVAIL` path calls `fi_cq_readerr` at `cq_ops+0x40` (`0x16c85`) and formats the error with `fi_cq_strerror` at `cq_ops+0x18` (`0x16ee4`). The `+0x38` and `+0x48` indirect calls in the same function (`0x17182`/`0x16f2a`) are the **per-request context vtable** handlers, not CQ ops — see §3. These are different op tables reached off different base pointers; a reimplementer who maps `readerr`→`+24` calls the wrong slot.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nccl_net_ofi_create_plugin` | `0xabe0` | init top; reads `OFI_NCCL_PROTOCOL`, dispatches RDMA vs SENDRECV | HIGH |
| `nccl_net_ofi_rdma_init` | `0x171d0` | RDMA init: get EFA provider, create device/domain/ep per NIC | HIGH |
| `nccl_ofi_ofiutils_get_providers` | `0x26b80` | `fi_getinfo` with `provider_filter="efa"`, version gate | HIGH |
| `nccl_ofi_ofiutils_init_connection` | `0x26fd0` | `fi_fabric`/`fi_domain`/`fi_cq`/`fi_av`/`fi_endpoint` wireup | HIGH |
| `rdma_domain_create_endpoint` | `0x134c0` | per-domain endpoint (rail) creation | HIGH |
| `post_rx_buffs_on_rail` | `0x181d0` | refill rx buffers on a rail (`alloc_rx_buff_req` → post `fi_recv`) | HIGH |
| `query_provider_capabilities` | `0xa4f0` | `FI_OPT_INJECT_RMA_SIZE`, `FI_OPT_EFA_EMULATED_WRITE`, FI_HMEM | HIGH |

---

## 2. The Post Side — `send_progress`

### Purpose

`send_progress` (`0x12850`) is the function that puts a request on the wire. It is called both directly from `send` (`0x1c980`, the `isend` implementation) and from the EAGAIN-drain path; it walks the request's per-rail transfer descriptors and, for each rail, posts the appropriate libfabric verb — `fi_writedata` (RDMA-WRITE-with-immediate, the bulk path), `fi_senddata` (eager small-message send), `fi_writemsg` (the iov/multi-descriptor write), or the inject/`fi_recv` path for rx-buffer reposts. On `-FI_EAGAIN` it does **not** spin: it queues the request on the endpoint's `pending_reqs` deque under lock, and `ofi_process_cq` retries it on the next progress pass.

### Entry Point

```text
isend_v9 0x7bc0 ──► send 0x1c980                       (the ncclNet isend impl)
  guard comm_active (+208) ; inflight < 128 (+72, cmpq $0x80 @0x1c9ce)
  process_cq_if_pending(ep) 0x1c450
  msgbuff_retrieve(seq) 0x258f0   ── expects SEND_CTRL already arrived (remote addr+rkey)
  alloc rdma_send_req from comm freelist 0x15130
  build imm = (rkey<<28)|(comm_id<<10)|seq   (shl $0x1c @0x1cba0 ; shl $0xa @0x1cbb3)
  get_threshold_schedule 0x226a0            ── eager vs RDMA-write + rail striping
  └─► send_progress 0x12850                  ── post across rails
        post_rdma_write      → fi_writedata  (rma_ops + 0x30)
        post_rdma_eager_send → fi_senddata   (msg_ops + 0x20)
        (iov path)           → fi_writemsg   (rma_ops + 0x40)
        -FI_EAGAIN(-11)      → queue on ep pending_reqs (ep+80) under lock (ep+88)
```

### Algorithm

```c
// send_progress — 0x12850   [HIGH: rma/msg op slots + EAGAIN deque read from disasm]
// Posts a request's outstanding rail-transfers to the NIC. One req may stripe
// across several data rails; each rail gets one libfabric post this pass.
int send_progress(nccl_net_ofi_rdma_req *req):
    comm    = req->comm;                              // req+328
    ep      = comm->ep;
    n_rails = ep->num_rails;                          // ep+48

    for (i = req->first_unposted_rail; i < n_rails; i++):
        send_comm_rail *rail = &comm->data_rails[i];  // comm+216, stride 0x10
        fid_ep *eph   = rail->fid_ep;                 // rail+0
        fi_addr_t dst = rail->remote_addr;            // rail+8 (AV-inserted)

        switch (req->type):                           // req+508
          case WRITE:                                 // RDMA-WRITE-with-immediate (bulk)
            // fi_writedata(ep, buf,len,desc, dst, remote_addr, rkey, imm, context)
            void  *rma = eph->rma_ops;                // mov 0x30(%rdi),%rax @0x129c0
            rc = (*(rma + 0x30))(eph,                  // call *0x30(%rax)  @0x129c4
                     req->buff[i], req->len[i], req->desc[i],
                     dst, req->remote_addr[i], req->rkey,   // req+360.. per-rail scratch
                     req->imm_data,                    // req+408 = (rkey<<28)|(comm_id<<10)|seq
                     &req->ctx[i]);
            break;

          case SEND:                                  // eager small-message path
            void *msg = eph->msg_ops;
            rc = (*(msg + 0x20))(eph,                  // fi_senddata  call *0x20(%rax) @0x12a74
                     req->buff[i], req->len[i], req->desc[i],
                     req->imm_data, dst, &req->ctx[i]);
            break;

          case WRITE_IOV:                              // multi-descriptor write
            void *rma = eph->rma_ops;
            rc = (*(rma + 0x40))(eph, &req->writemsg[i], FI_REMOTE_CQ_DATA);  // fi_writemsg @0x12b87
            break;

          default:                                     // rx-buffer repost (post_rx_buffer)
            void *msg = eph->msg_ops;
            rc = (*(msg + 0x18))(eph, ...);            // fi_recv  call *0x18(%rax) @0x12c4d
            break;

        if (rc == -FI_EAGAIN /* -11 */):               // NIC send-queue full
            lock(ep->pending_reqs_lock);               // ep+88
            ep->pending_reqs.push_back(req);           // ep+80 std::deque<rdma_req*>
            unlock(ep->pending_reqs_lock);
            return 0;                                   // retried by ofi_process_cq next pass
        if (rc < 0):
            log("Error posting RDMA %s request. RC: %zd, Error: %s");  // 0x33770
            return rc;
        req->first_unposted_rail = i + 1;

    return 0;
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `send` | `0x1c980` | `isend` impl: guard, build imm, schedule, call `send_progress` | HIGH |
| `send_progress` | `0x12850` | post one pass across rails; `fi_writedata`/`senddata`/`writemsg`; EAGAIN deque | HIGH |
| `send_ctrl_post` (`.isra.0`) | `0x12780` | post a SEND_CTRL on the control rail (recv-side rendezvous) | HIGH |
| `rma_write` / `rma_write_inline` | `0x1c5f0` / `0x1e960` | `iwrite` / `iwriteInline` ABI ops (explicit one-sided write) | HIGH |
| `get_threshold_schedule` | `0x226a0` | eager-vs-write threshold + rail-striping schedule | HIGH |
| `process_cq_if_pending` | `0x1c450` | drain ep CQ if there are deferred/pending reqs before a new post | HIGH |
| `allocate_req` | `0x15130` | pop a request from the comm freelist (`"No freelist items available"`) | HIGH |

### Considerations

The single subtle invariant is the **EAGAIN-deferral discipline**: `send_progress` never busy-waits on a full NIC queue. On `-FI_EAGAIN (-11)` it parks the request on `ep->pending_reqs` (a `std::deque<rdma_req*>` at `ep+80`, guarded by the mutex at `ep+88`) and returns success-with-no-progress; the next `ofi_process_cq` pass (§3) re-drives the deque. This is why `send` (`0x1c980`) calls `process_cq_if_pending` (`0x1c450`) *before* allocating a new request — it gives previously-deferred posts a chance to drain first, bounding the deque. The second invariant is the **128-inflight cap** (`cmpq $0x80,0x48(%rdi)` at `0x1c9ce`, comparing the comm's `num_inflight_reqs` at `+72`): `send` refuses to allocate a request past 128 outstanding, returning a NULL request so the proxy retries the op rather than overrunning the freelist. The per-rail striping (which rails a transfer uses, and the eager-vs-write split) is decided once by `get_threshold_schedule` (`0x226a0`) before `send_progress` runs; `send_progress` only executes the schedule. Note the immediate word is built **in `send`, not `send_progress`** — `req->imm_data` (`req+408`) is already `(rkey<<28)|(comm_id<<10)|seq` by the time the post runs, so every rail's write of a striped message carries the same demux key.

---

## 3. The Harvest Side — `ofi_process_cq_rail`

### Purpose

`ofi_process_cq_rail` (`0x16b40`) is the completion harvester for one rail. `ofi_process_cq` (`0x1ab80`) calls it once per domain rail; together they are the plugin's entire progress engine. The function drains the rail's `fid_cq` with `fi_cq_read` in a loop, and for each 40-byte `fi_cq_data_entry` splits two cases: a **FI_REMOTE_WRITE** completion (flag bit `0x20`) carries only the immediate word and is retired by decoding `(rkey<<28)|(comm_id<<10)|seq`; everything else carries a per-request **context** whose vtable is dispatched through `context->handle_cq_entry`. On `-FI_EAGAIN` it returns idle; on `-FI_EAVAIL` it reads the error entry with `fi_cq_readerr` and formats it with `fi_cq_strerror`.

### Entry Point

```text
test_v2 0x81d0 ──► test 0x1c1f0 ──► ofi_process_cq 0x1ab80   (drives ALL rails)
  for domain_rail in domain->rails:
    └─► ofi_process_cq_rail 0x16b40
          ret = fi_cq_read(cq, buf, cq_read_count)        cq_ops+0x8   @0x16c03
          per 40-byte fi_cq_data_entry:
            if entry.flags & 0x20 (FI_REMOTE_WRITE):       testb $0x20,0x9(%r14) @0x16ca4
              ── immediate-data path: decode (rkey<<28)|(comm_id<<10)|seq
              ── get_req_from_imm_data → msgbuff_retrieve → ++ncompls
            else:
              ── context vtable: (*(ctx->handle_cq_entry))(ctx, entry, rail_id)
                   == rdma_req_handle_cq_entry 0x18e40        (ctx+0x40, call *0x40(%rax))
          ret == -FI_EAGAIN(-11): return idle
          ret == -FI_EAVAIL(-259): fi_cq_readerr(cq,&err,0)  cq_ops+0x40 @0x16c85
                                   fi_cq_strerror(...)        cq_ops+0x18 @0x16ee4
                                   handle_error_entry         (ctx+0x48, call *0x48(%rax)) @0x16f2a
```

### Algorithm

```c
// ofi_process_cq_rail — 0x16b40   [HIGH: cq ops slots + FI_REMOTE_WRITE bit + ctx vtable read from disasm]
int ofi_process_cq_rail(nccl_net_ofi_rdma_device *dev,
                        nccl_net_ofi_rdma_domain_rail *rail):
    fid_cq *cq = rail->fid_cq;                       // rail+0x18  (mov 0x18(%rdi) @0x16bfc)
    fi_cq_data_entry buf[cq_read_count];             // 40 B each; cq_read_count = ofi_nccl_cq_read_count()

    for (;;):
        ssize_t n = (*(cq->cq_ops + 0x8))(cq, buf, cq_read_count);   // fi_cq_read @0x16c03
        if (n > 0):
            for (k = 0; k < n; k++):
                fi_cq_data_entry *e = &buf[k];        // +0 op_context, +8 flags, +24 data(imm)

                if (e->flags & 0x20):                 // FI_REMOTE_WRITE — testb $0x20,0x9(%r14) @0x16ca4
                    // bulk RDMA-WRITE-with-immediate landed; no payload header.
                    uint64_t imm  = e->data;          // immediate word
                    uint32_t seq  =  imm        & 0x3FF;          // [0:10]
                    uint32_t cid  = (imm >> 10)  & 0x3FFFF;       // [10:28]  comm id
                    uint32_t rkey =  imm >> 28;                   // [28:]
                    req = get_req_from_imm_data(dev, imm);        // device+184 comm map → msgbuff_retrieve(seq)
                    lock(req->mutex);                             // req+464
                    if (++req->ncompls == req->expected):         // req+344 vs req+400
                        req->state = 2;                           // req+504 = DONE
                    unlock(req->mutex);
                else:
                    // context-carried completion (send done, ctrl recv, eager recv, rx-buff, conn).
                    context *ctx = (context*)e->op_context;       // e+0
                    rc = (*(ctx->handle_cq_entry))(ctx, e, rail->rail_id);  // ctx+0x40 @0x16c85
                    if (rc != 0): log("Context progress failed: %d");
            continue;                                  // drain until EAGAIN

        if (n == -FI_EAGAIN /* -11 */):                // queue empty
            return 0;                                   // idle this pass

        if (n == -FI_EAVAIL /* -259 */):               // an error completion is waiting
            fi_cq_err_entry err;
            (*(cq->cq_ops + 0x40))(cq, &err, 0);        // fi_cq_readerr @0x16c85
            const char *s = (*(cq->cq_ops + 0x18))(cq, ...);  // fi_cq_strerror @0x16ee4
            if (err.flags & 0x20):
                log("Remote write completed with error. RC: %d ...");   // 0x34b50
            else:
                ctx = (context*)err.op_context;
                (*(ctx->handle_error_entry))(ctx, cq, &err, rail->rail_id);  // ctx+0x48 @0x16f2a
            continue;

        log("Unable to retrieve completion queue entries. RC: %zd");  // 0x30ef8
        return n;
```

### The per-request context vtable

A non-immediate completion's `op_context` is the `nccl_net_ofi_context_t` embedded in each request; the relevant vtable slots are two function pointers reached off the context base.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `handle_cq_entry` | +0x40 | fn ptr | success-path dispatch → `rdma_req_handle_cq_entry` `0x18e40` | HIGH |
| `handle_error_entry` | +0x48 | fn ptr | error-path dispatch → `rdma_req_handle_error_entry` `0x119b0` | HIGH |

`rdma_req_handle_cq_entry` (`0x18e40`) then switches on the request `type` (`req+508`): a SEND completion runs `inc_req_completion` (`0x143c0`); a control/eager/rx-buffer recv runs the matching `handle_ctrl_recv` / `handle_eager_recv` / `handle_rx_buff_recv` / `handle_close_msg_recv` callback (named in `rdma_process_completions`).

### Request struct (the unit `ncompls` accumulates into)

`nccl_net_ofi_rdma_req_t` (≈ 528 B; freelist `528/16/16/128`). Only the data-path fields are listed.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `comm` | +328 | comm* | owning send/recv comm | HIGH |
| `dev_id` | +336 | int | device id | HIGH |
| `msg_seq_num` | +340 | u16 | sequence (low 10 bits of imm) | HIGH |
| `ncompls` | +344 | int | completions seen so far (++ per CQ entry) | HIGH |
| `eager` | +352 | byte | eager-path flag (set in `send`) | MED |
| per-rail xfer scratch | +360..+408 | — | per-rail addr/len/rkey for the post | MED |
| `expected` | +400 | int | total completions required (== rail count striped) | HIGH |
| `imm_data` | +408 | u64 | `(rkey<<28)|(comm_id<<10)|seq` | HIGH |
| `buff` / `size` | +416 / +424 | void* / size | payload pointer / size (clamped to ctrl-advertised) | HIGH |
| `mr_handle` | +432 | mr_handle* | local MR for this transfer | HIGH |
| `ctrl_elem` | +440 | freelist_elem* | control-message copy buffer | MED |
| `rkey` | +448 | u32 | remote key (from ctrl message) | HIGH |
| `mutex` | +464 | pthread_mutex_t | guards `state`/`ncompls` | HIGH |
| `state` | +504 | int | 2 = DONE (set when `ncompls == expected`); connect-resp also uses 2 | HIGH |
| `type` | +508 | int | req-type enum (SEND, WRITE, SEND_CTRL, RECV_SEGMS, …) | HIGH |
| `free` | +520 | fn ptr | `free_{send,recv,…}_req` | HIGH |

### Considerations

The harvest is **lock-minimal and demux-by-immediate**. A FI_REMOTE_WRITE completion (the common case — every bulk slice) takes only the per-request mutex (`req+464`) to bump `ncompls` and, on the last completion, flip `state` to DONE — there is no per-comm or per-rail lock on the hot path. The demux is pure arithmetic on the immediate: `comm_id` (`imm[10:28]`) indexes the device's comm map (reached off `device+184`) to find the comm, `seq` (`imm[0:10]`) drives `msgbuff_retrieve` to find the request, `rkey` is bookkeeping. Because the bulk write carries no header, **a multi-rail message completes when its per-rail completions sum to `expected`** (`req+400`) — `expected` is the rail count the scheduler chose, and each rail's FI_REMOTE_WRITE bumps the same request's `ncompls`. The two failure modes are distinct return codes a reimplementer must handle separately: `-FI_EAGAIN (-11)` is the normal "queue empty, idle" exit; `-FI_EAVAIL (-259)` means an error completion is queued and *must* be drained with `fi_cq_readerr` before the next `fi_cq_read` will return data — skipping the readerr wedges the CQ. The error entry's own `flags & 0x20` distinguishes a failed remote-write (logged directly, `0x34b50`) from a context-carried error (dispatched through `handle_error_entry` at `ctx+0x48`).

---

## 4. The Connection Lifecycle and Memory Registration

### Purpose

Before any data flows, the active side runs a 6-state connection machine in `connect` (`0x1ac00`) and both sides register their buffers per-rail in `reg_mr` (`0x15e40`). The connection exchanges EFA endpoint names out-of-band (through `libnccom`'s bootstrap) and AV-inserts the peer's addresses on every rail; registration produces one `fid_mr` per rail under a shared mr-key from the idpool, cached so repeated registrations of the same buffer are free.

### The connection state machine

```text
connect 0x1ac00   (state stored in conn_handle+80, advanced 0..5)
  state 0  create_send_comm: calloc 0xE8, alloc rail arrays, mutex_init, install ops
           (reg_mr_send_comm, dereg, send, send_close_deferred, rma_write, rma_write_inline);
           allocate comm_id from device idpool; post_rx_buffs on all rails;
           AV-insert remote control addr:  fi_av_insert = fid_av->av_ops + 8;
           build conn msg into freelist elem; alloc SEND_CONN(11) + RECV_CONN_RESP(13) reqs.
  state 1  post_send_conn: fi_senddata(conn_msg, 528, ctrl_addr, ctx) on control_rail[0];
           -FI_EAGAIN → ofi_process_cq(ep), retry.
  state 2/3 ofi_process_cq drives; wait recv_conn_resp req->state==2 (mutex+464 guards state+504).
  state 4  finish_connect: validate remote num_rails/control_rails/comm_id;
           init_send_comm_rails: per data+control rail AV-insert remote rail addresses.
  state 5  comm active; *scomm = send_comm; return 0.
```

### Memory registration

```c
// reg_mr — 0x15e40   [HIGH: access mask + per-rail fi_mr_regattr slot read from disasm]
int reg_mr(nccl_net_ofi_rdma_domain *domain, nccl_ofi_mr_ckey const *ckey,
           int type, nccl_net_ofi_rdma_mr_handle **out):
    // (1) cache hit short-circuits the whole registration
    lock(domain->mr_cache_lock);
    handle = mr_cache_lookup_entry(domain->mr_cache, ckey);      // domain+24, 0x24ef0
    if (handle): { unlock; *out = handle; return 0; }

    // (2) allocate handle + per-rail fid_mr array; one mr-key from the idpool
    n_rails = domain->num_rails;                                  // domain+128
    handle  = calloc(0x18);                                       // {num_rails, key, mr[]}
    handle->num_rails = n_rails;
    handle->mr_key    = idpool_allocate_id(domain->mr_key_idpool);// domain+32; -1 if pool size 0
    handle->mr        = calloc(n_rails * 8);                      // fid_mr* per rail

    // (3) build fi_mr_attr; access = 0x3F00 = SEND|RECV|READ|WRITE|REMOTE_READ|REMOTE_WRITE
    fi_mr_attr attr = {0};
    attr.mr_iov.base = ckey; attr.iov_count = 1;
    attr.access        = 0x3F00;                                  // movq $0x3f00,0x30(%rsp) @0x15f89
    attr.requested_key = handle->mr_key;
    attr.iface         = (type == 4) ? FI_HMEM_CUDA : 0;          // SYSTEM=0 / CUDA=4 (LOW)
    if (attr.iface == 4) attr.device.cuda = -1;
    uint64_t flags = (ckey->kind == 2) ? FI_MR_DMABUF /*0x10000000000*/ : 0;  // DMABUF path

    // (4) register per rail through the domain rail's fid_domain mr_ops
    for (i = 0; i < n_rails; i++):
        domain_rail *dr = &domain->rails[i];                     // domain+136, stride 0x18
        fid_domain *fd  = dr->fid_domain;                        // dr+8
        rc = (*(fd->mr_ops + 0x18))(fd, &attr, flags, &handle->mr[i]);  // fi_mr_regattr call *0x18(%rax)
        if (rc < 0): log("Unable to register memory (type = %d) ...");  // 0x31a18

    mr_cache_insert_entry(domain->mr_cache, ckey, handle);
    unlock(domain->mr_cache_lock);
    *out = handle;  return 0;
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `connect` | `0x1ac00` | 6-state active-connect machine; AV-insert; install comm ops | HIGH |
| `listen` / `accept` | `0x19dd0` / `0x1ece0` | passive side: post conn-req recv / send conn-resp | HIGH |
| `reg_mr` | `0x15e40` | per-rail `fi_mr_regattr`; mr_cache + idpool; access `0x3F00` | HIGH |
| `dereg_mr` | `0x15610` | `fi_close` each `fid_mr` + `idpool_free_id` | HIGH |
| `recv` | `0x1dbb0` | `irecv` impl: register MR, post SEND_CTRL (advertise addr+rkey) + RECV_SEGMS | HIGH |
| `flush` | `0x1d770` | post-recv GDR flush (small `fi_read`) | HIGH |

### Considerations

The conn message is 528 bytes sent eagerly (`fi_senddata`, state 1); its wire layout (per-rail EFA endpoint names at `+16`, control-rail names at `+272`, each a `0x40`-byte slot) is the one interop-relevant format the plugin defines, and its field *meaning* is inferred from the memcpy/AV-insert pattern (MED). The registration's `access = 0x3F00` is the full RMA permission set (`FI_SEND|FI_RECV|FI_READ|FI_WRITE|FI_REMOTE_READ|FI_REMOTE_WRITE`) — both sides register read/write-able so either RDMA-write (sender→receiver target) or the GDR-flush read works. The DMABUF path (`flags = FI_MR_DMABUF` when `ckey->kind == 2`) is how device memory reaches the NIC without a CUDA peer-memory call: it is gated upstream by `kernel_version_rdma_dmabuf_ioctl_ok` (`0x275e0`) and `nccl_ofi_dmabuf_viable` (`0x276b0`). The FI_HMEM iface numeric (`4 == FI_HMEM_CUDA`) is the one **LOW**-confidence claim on this page — inferred from the `type==4 → device.cuda=-1` branch, not from a named constant.

---

## 5. Support Structures — Freelist, MR-Cache, ID-Pool, Scheduler

### Purpose

Four small structures carry the per-op state the data path leans on every request: the **freelist** that pools the ≈528-byte requests so `send` never `malloc`s on the hot path; the **mr_cache** that short-circuits re-registration; the **idpool** that hands out the mr-keys the immediate word's `rkey` field carries; and the **scheduler** that decides eager-vs-write and rail striping. None is exotic — they are the same aws-ofi-nccl utilities — but their mechanics are exactly the parts that bite a reimplementer who treats them as afterthoughts.

### Function Map

| Structure | Init | Key ops | Role | Confidence |
|---|---|---|---|---|
| `nccl_ofi_freelist` | `0x26560` | `add 0x26020`, `entry_alloc 0x12da0`, `allocate_req 0x15130` | request pool; grows in blocks; `"No freelist items available"` (`0x323e0`) | HIGH |
| `nccl_ofi_mr_cache` | `0x24d20` | `lookup_entry 0x24ef0`, `insert_entry 0x24f90`, `del_entry 0x251e0` | registration cache keyed by `mr_ckey` | HIGH |
| `nccl_ofi_idpool` | ctor `0x26ad0` | `allocate_id 0x265b0`, `free_id 0x266d0`, `get_size 0x26830` | mr-key allocator; size-0 pool → key `-1` | HIGH |
| `nccl_ofi_scheduler` | `0x22af0` | `get_threshold_schedule 0x226a0`, `release_schedule 0x22a40` | rail-striping + eager threshold | HIGH |
| `nccl_ofi_msgbuff` | `0x25360` | `insert 0x25580`, `retrieve 0x258f0`, `complete 0x25ac0` | seq→request map (10-bit window) | HIGH |

### Considerations

> **GOTCHA — the mr-key can legitimately be `-1`, and `reg_mr` must not treat that as failure.** `idpool_allocate_id` returns `-1` when the key pool size is zero (EFA providers that do not require an application-supplied key — `nccl_ofi_mr_keys_need_own_key` at `0x27310` decides this from the `fi_info` `mr_mode`). `reg_mr` stores that `-1` as `requested_key` and registers anyway; the immediate word's `rkey` field then comes from the provider-assigned key read back off the `fid_mr`, not from the idpool. A reimplementer who asserts `key >= 0` after `allocate_id` breaks every EFA configuration that does not need application keys.

> **GOTCHA — the message-sequence window is 10 bits, and it wraps.** `next_msg_seq_num` (comm `+120`) is masked `& 0x3FF`, and `seq` occupies `imm[0:10]` — so at most **1024** distinct in-flight sequence numbers exist, and the receiver's `msgbuff` is a sliding window over them. The 128-inflight cap (§2) is well inside this window, so wrap is not normally reachable, but a reimplementer who widens the inflight cap past ~1024 without widening the immediate's `seq` field will alias two live messages onto one sequence number — the `comm_id<<10` shift leaves exactly 10 bits for `seq`, no more.

> **QUIRK — the freelist grows but never shrinks, and that is the point.** `nccl_ofi_freelist` (`0x26560`) extends in blocks on demand (`"Could not extend freelist: %d"`, `0x31df0`) but holds the high-water set of requests for the comm's lifetime; requests are *recycled* through `allocate_req`/`free_*_req`, never returned to the allocator per-op. This is the same recycle-don't-free discipline the proxy layer uses one level up ([proxy-engine](proxy-engine.md) §4) — the request is the unit of reuse, so the hot path is pointer-bump allocation, not `malloc`. A reimplementer who frees requests to the system allocator on completion reintroduces the per-op allocation the freelist exists to remove.

The scheduler (`get_threshold_schedule`, `0x226a0`) is the one piece a reimplementer can tune independently: it reads `OFI_NCCL_MIN_STRIPE_SIZE` (`0x29e30`), `OFI_NCCL_SCHED_MAX_SMALL_MSG_SIZE` (`0x29ff0`), and `OFI_NCCL_FORCE_NUM_RAILS` (`0x2bf10`) to decide how many rails a transfer stripes across and whether it goes eager (`fi_senddata`) or RDMA-write (`fi_writedata`). Small messages take one rail eager; large messages stripe across all data rails as RDMA-writes — the `expected` completion count (`req+400`, §3) is exactly the number of rails the schedule chose.

---

## Related Components

| Name | Relationship |
|---|---|
| `ncclNetPlugin_v6` (`.data 0x438a0`) | the ABI struct `libnccom.so` binds; its `isend`/`irecv`/`iwrite`/`test` slots are this transport's entry points ([net-plugin](net-plugin.md)) |
| `nccl_ofi_sendrecv.cpp` (`init 0x10960`) | the alternate tagged `fi_tsend`/`fi_trecv` protocol — no imm-data; selected when `OFI_NCCL_PROTOCOL != "RDMA"` |
| `libfabric.so.1` (EFA provider) | the NIC boundary — 6 versioned PLT syms + per-`fid` inline op vectors this page maps |
| `libhwloc.so.15` | PCIe topology for rail↔CPU grouping (`nccl_ofi_topo_create 0x23d20`) |
| `netNeuron{Send,Recv}Proxy` (libnccom) | the per-op state machine in `libnccom.so` that calls these ABI ops once per tick ([send-recv-prims](send-recv-prims.md)) |

## Cross-References

- [The Net Plugin (ncclNet_v6)](net-plugin.md) — the `ncclNetPlugin_v4/v5/v6` ABI tables and the dlopen/dlsym handoff from `libnccom.so` into this plugin; the slot-for-slot binding of the ops this page implements
- [Intra-Node Transport (P2P)](transport-intra.md) — the *other* transport edge (local DMA between NeuronCores), selected by `p2pCanConnect` where this one is selected by `netNeuronCanConnect`; no network, no rails
- [Send / Recv Primitives and Protocols](send-recv-prims.md) — the `libnccom.so`-side `netNeuron{Send,Recv}Proxy` callbacks whose `isend`/`irecv`/`iread` calls drive *this* plugin's `send`/`recv`/`rma_read`; the request ring that mirrors this freelist one level up
- [Proxy & Progress Engine (and the libnrt Bridge)](proxy-engine.md) — the op-FIFO loop that calls the net-plugin `test()` (→ `ofi_process_cq`) once per tick; the recycle discipline that re-arms the per-op state this page's freelist pools
- [Bananaphone IPC and the Proxy Driver](../collectives/proxy-driver.md) — the libnrt-side producer that fills the FIFOs whose `dst_rank`/`dst_addr`/`dst_mhandle` become this transport's RDMA targets
- [back to index](../index.md)
