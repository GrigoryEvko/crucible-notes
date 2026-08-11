# Host↔Device Descriptor Handoff (runtime side)

> **Scope — the host control flow that BUILDS, SUBMITS, DOORBELLS, and WAITS on
> DMA descriptors.** This page reverse-engineers the path inside the **host**
> Neuron runtime (`libnrt.so.2.31.24.0`, x86-64) and the GPSIMD custom-op runtime
> (`libnrtucode_internal.so`, 0.21.2.0) that turns a logical transfer request into
> a paged in-memory **vring** of 16-byte descriptors, lowers it to a physical
> **pring** (the device ring image), reserves slots in the per-direction
> `sw_dma_queue` cursors, stages the bytes to device DRAM, fires **one of three
> physically distinct doorbells**, and reaps completion through **two distinct
> wait models**. It is the *host* counterpart of the device-side DMA descriptor
> machinery: where [The DMA / Descriptor / Memory Subsystem](../dma/descriptor-model.md)
> and [DGE Descriptor-Builder + SDMA QoS/Arbitration](../dma/dge-builder-qos.md)
> own the descriptor **byte taxonomy** and the device submit→complete dataflow,
> *this* page owns only the host code path that fills and releases those bytes.
>
> The through-line is a single **lowering chain** — `dma_util_vring_append_descs`
> → `dma_ring_create_prings_from_vring` → `vring_dump_to_pring_descriptors` →
> `vring_addr_rewrite` → `hw_exec_queue_add_exec_request_impl` → the doorbell —
> plus a GPSIMD-only side channel that programs the device Q7 DGE's
> **DMA-priority gate** (the `dge_mailbox` + `priority_class_map` blocks). This is
> the runtime sibling of
> [Execute-Time GPSIMD Custom-Op Dispatch](execute-time-dispatch.md) (which fires
> the *per-inference* kickoff) and the host-API companion of
> [The DGE Host-Private API (priority/mailbox/PC-bounds)](dge-host-api.md).
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read from
> `objdump`/`nm`/`readelf`/`c++filt`/DWARF on the shipped binary), `INFERRED`
> (a rule applied to an observed fact), `CARRIED` (consolidated from a cited
> committed page, re-confirmed here only where spot-checked). The page default is
> `[HIGH × OBSERVED]`; claims that depart from it carry an explicit tag.

> **NOTE — provenance & artifacts.** Every host fact below is derived **solely
> from static analysis** of two shipped, redistributable ELF64 objects, read with
> stock tools:
>
> * `libnrt.so.2.31.24.0`
>   (`…/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/`),
>   **122 956 336 bytes**, BuildID `8bb57aba…`, version **2.31.24.0** (git
>   `0b044f4ce`), **not stripped** (`.debug_info` present, ~17 372 functions). For
>   this binary load addresses **equal** file offsets for `.text` (VMA `0x3dbc0`),
>   `.rodata` (`0x7cf000`), and `.data` (`0xc07e00`) — the `.data` delta is **zero**
>   here, so every libnrt address disassembles directly (confirmed
>   `readelf -SW`). Source paths in `.rodata` point to
>   `KaenaRuntime/tdrv/dma_ring.c` (string present in binary).
> * `libnrtucode_internal.so`
>   (`…/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/`),
>   BuildID `9cbf78c6…`, **not stripped**. **GOTCHA:** this object *does* carry a
>   section delta — `.text` VMA `0x9b01a0` vs file-offset `0x9af1a0` (Δ `0x1000`),
>   `.data` VMA `0x9ba4a8` vs `0x9b74a8` (Δ `0x3000`). `objdump` is fed **VMA**
>   addresses below (it resolves the delta internally); a raw `xxd` at the VMA
>   would land in the wrong bytes.
> * The shipped customop-lib headers
>   `…/custom_op/c10/include/nrtucode.h` (+ `nrtucode_private.h`), whose layout and
>   doc-comments are static and citeable (a binary-adjacent shipped artifact).
>
> The device is a Cadence Tensilica **Vision-Q7 NX "Cairo"** DSP (config
> `ncore2gp`); the host x86-64 runtime drives eight such cores per NeuronCore.
> Per-generation cores carry the KaenaHal `__FILE__` keys **sunda** (NC-v2),
> **cayman** (NC-v3), **mariana** (NC-v4) directly; the v5 **Maverick** key is
> header-observed only and so tagged **INFERRED** where it appears.

---

## 0. One-screen orientation — the host handoff spine

A GPSIMD DMA is built **once** on the host at NEFF load/stage into an in-memory
**vring** (a virtual, paged 16-byte-BD list), **lowered** to a physical **pring**
(the device ring image), **staged** to device DRAM, and at execute time
**released** by a single doorbell. There are **two** host→device write channels
and **two** completion models; the rest of this page exists to keep them apart.

```
  LOAD / STAGE  (once per NEFF, all in tdrv/dma_ring.c + dma_util.c)
  ─────────────────────────────────────────────────────────────────
   [BUILD]   dma_util_vring_append_descs  @0x316d10   §1
               selects a per-type BD writer, folds the N-D walk into a per-BD count
   [ALLOC]   dma_ring_create_prings_from_vring @0x22e310 → dma_pring_alloc @0x22d8b0  §2
               calloc the 32-B pring, bind at vring+0x158 (TX) / +0x150 (RX)
   [LOWER]   vring_dump_to_pring_descriptors @0x3136e0 → …_padded @0x311f10   §2
               copy the vring BD blocks (+0x100 TX / +0x120 RX) into the prings
   [RELOC]   vring_addr_rewrite          @0x313810   §2
               patch every 16-B BD's buf_ptr(+0) that lands in a rebased window
   [GATE]    nrtucode_core_dge_* (libnrtucode_internal.so)   §6
               GPSIMD-only: program the device Q7 DGE DMA-priority mailbox + class map

  EXEC-SETUP  (per inference)
  ─────────────────────────────────────────────────────────────────
   [SUBMIT]  kbl_compute_setup @0x306fb0 →
             hw_exec_queue_add_exec_request_impl @0x320810  §3
               build exec-descriptor BDs, dmem_buf_copyin to device, record doorbell offset
             hw_exec_queue_add_descriptors @0x3206f0 →
             sw_dma_queue_reserve_descriptors @0x448ce0 + …_set_descriptors  §3
               advance the host txq/rxq cursors, write BDs into the bound prings

  FIRE + WAIT
  ─────────────────────────────────────────────────────────────────
   [DOORBELL] §4 — three variants (see §4)
   [WAIT]     §5 — dmem 0xABCDEF01 sentinel poll  /  execute-time NQ poll
```

**Two host→device write channels** (do not conflate):

| Channel | Transport chain (byte-verified) | Used for |
|---|---|---|
| **A. CSR/BAR write** | `al_reg_write32` @0x265c50 → `csr_write` @0x315820 → `ndl_bar_write` @0xc35e0 | per-queue **tail-pointer-inc** doorbell; SP **top-sequencer host-trigger** |
| **B. NeuronCore semaphore ioctl** | `ndl_nc_semaphore_increment` @0xc3ba0 → `ioctl()` (req `0x80084e29`) | the execute-time **kickoff** that releases the whole prebuilt per-engine stream set |

Descriptor and completion-word *payloads* themselves cross the boundary via
`dmem_buf_copyin` / `dmem_buf_copyout` (the device-DRAM staging primitives), which
are orthogonal to the two doorbell channels.

**Two completion models** (§5): a **dmem busy-poll** against an `0xABCDEF01`
sentinel for the load/stage-time per-DMA copies, and the **execute-time
Notification-Queue (NQ) poll** for the per-inference release. They are different
code, different transports, different lifetimes.

**The direction bit** `[CARRIED — DMA Part]`: READ / HBM→local = **M2S** =
`TDRTP_inc`; WRITE / local→HBM = **S2M** = `RDRTP_inc`; both tail-inc CSRs sit at
queue-bank `+0x38` (§4.1).

---

## 1. Runtime descriptor construction — `dma_util_vring_append_descs`

`dma_util_vring_append_descs` **@0x316d10** (size 0x9cb / 2507 B) is the host
BD-filler: it turns one logical, *typed* transfer request into one or two al_udma
DMA "packets" of 16-byte BDs appended to a vring page.

### 1.1 Two packet buffers (TX + RX)

The arch max descriptor count per packet is fetched, then two packets are
allocated:

```c
// dma_util_vring_append_descs @0x316d10
uint32_t max_dpp = tdrv_arch_get_max_desc_per_packet(arch);   // @0x30adf0
dma_packet_t *tx  = al_udma_m2m_alloc_dma_packet(...);         // @0x45ca90  (rsp+0x40)
dma_packet_t *rx  = al_udma_m2m_alloc_dma_packet(...);         // @0x45ca90  (rsp+0x58)
// tx[+8]  <- split/odd flag (a bool from the dim-division below, `seta %bl`)
// tx[+0x10] <- dim multiplier (ebp)
```

`al_udma_m2m_alloc_dma_packet` is called **twice** (TX then RX); the first
packet's byte `+8` takes a bool (a split/odd flag derived from the dimension
arithmetic in §1.3) and `+0x10` takes the dimension multiplier.

### 1.2 Dispatch on the descriptor TYPE (the host branch selector)

The transfer template carries a desc-type selector at `template+4`; the host
branches on it. Byte-verified at `0x316d53`:

```c
uint32_t op = *(uint32_t *)(tmpl + 4);   // 316d53: mov 0x4(%r8),%eax
if (op == 0)        goto copy_path;       // 316e00: test %eax,%eax  → COPY  (jmp 0x317262)
else if (op == 5)   goto transpose_path;  // 316e08: cmp  $0x5,%eax  → TRANSPOSE (jmp 0x317491)
else /* 1..4 */     goto compute_family;  // 316e14: cmp  $0x3,%eax  → CCE/FMA/MIN/MAX/gradient ladder
```

The type values mirror `kbin_dma_desc_op_t` (`COPY=0, FMA=1, ADD=2, MIN=3, MAX=4,
TRANSPOSE=5`). **NOTE:** that enum's *byte table* is owned by the DMA Part; here
it is purely a **host branch selector** that picks which arch-specific BD writer
to invoke — no byte table is restated. `[HIGH × OBSERVED / CARRIED]`

### 1.3 The dimension fold (tiling → per-BD count)

The request's element count is divided by the per-arch packetization granule at
`r8+0x14ac`; the quotient becomes the descriptor count, and a non-zero remainder
takes an assert-style error exit. Byte-verified:

```c
uint32_t granule = *(uint32_t *)(tmpl + 0x14ac);  // 316d78: mov 0x14ac(%r8),%ebp
uint32_t ndesc   = elem_count / granule;          // 316d83: div %ebp
if (elem_count % granule != 0)                     // remainder != 0
    nlog_assert("... != 0");                        // 0x3176bc
```

This is where a multi-dimensional tensor walk is folded into a flat per-BD count.
`[MED × OBSERVED]` (the *granule-field semantics* — that `r8+0x14ac` is a
packetization granule — is the inference; the division and remainder-error are
OBSERVED).

### 1.4 Per-element BD writers

Per element, the build calls into the arch-specific BD-writer family
(`al_udma_m2m_build_{tx,rx}_descriptor`,
`al_sdma_m2s_build_{fma,min_max,gradient,seed_init,transpose,replication}_descriptor`).
The host's job ends at *selecting* the writer by type and feeding it
`(addr, len, dtype, op)`; the actual byte layout those writers emit is owned by
the DMA Part.

> **NOTE — the N-D expander peers.** A family of host helpers feeds §1.4 the
> per-tile `(addr,len)` tuples: `dma_util_expand_desc`,
> `_expand_desc_complete[_cce]`, `_expand_desc_n`, `_expand_tiling_dims`,
> `_combine_dimensions`, `_create_desc_template_addr`. These are the host-side
> analogue of the device DGE **DIMPUSH** expansion — the device end is owned by
> the DMA Part; they are named here only to anchor the call neighborhood.

---

## 2. The vring→pring lowering + ring allocation

The **vring** is the host-resident *virtual* BD list: a paged structure with up
to `0x10000` (64 Ki) BDs per page, pages chained at `page+0x100000`; each BD is
16 bytes with `buf_ptr` as the first u64. The **pring** is its lowered device-ring
image. Source file (`.rodata`): `KaenaRuntime/tdrv/dma_ring.c` (string present).

### 2.1 `dma_pring_alloc` @0x22d8b0 — allocate + bind

Args `(rdi=ctx, rsi=vring, rdx=count, rcx=?, r8d=ring-type/id, r9d=dir)`. Flow
byte-verified at `0x22d8be`–`0x22d965`:

```c
// dma_pring_alloc @0x22d8b0
if (*(uint8_t *)(vring + 0x20) != 0)          // 22d8be: cmpb $0,0x20(%rsi)  "sealed" bool
    assert(dma_ring.c:0x489);
pring_t *pring = calloc(1, 0x20);             // 22d8d3/d8e6: calloc(1, 0x20) — a 32-B pring obj
if (!pring) { nlog("Failed to alloc host DMA buffer (%lu bytes)"); return 4; }
if (dma_ring_alloc(ctx, pring, ...) != 0) {   // @0x22d7a0  device-ring backing alloc
    free(pring); return err;
}
switch (r9d /* dir */) {                       // r9d saved in r12d @0x22d8e3
  case 1: *(void **)(vring + 0x158) = pring; break;   // 22d920/d950  TX / M2S slot
  case 2: *(void **)(vring + 0x150) = pring; break;   // 22d926/d930  RX / S2M slot
  default: assert(dma_ring.c:0x49b);                    // 22da0a
}
```

> **CORRECTION — confirmed, not changed.** A first-pass `rg` of the disassembly
> *appeared* to pair `0x150` with `cmp $0x1` and `0x158` with `cmp $0x2`. Reading
> the actual control flow (`0x22d920: cmp $0x1,%r12d; je 0x22d950` →
> `mov %rbx,0x158(%rbp)`; fall-through `cmp $0x2,%r12d` → `mov %rbx,0x150(%rbp)`)
> reverses that: **`r9d==1` binds `vring+0x158` (TX)** and **`r9d==2` binds
> `vring+0x150` (RX)**. The report is **correct as written**; this note exists so a
> reimplementer does not trust a filtered grep over the branch order.

### 2.2 `dma_ring_create_prings_from_vring` @0x22e310 — the per-queue driver

Asserts `queue->vring != NULL` (`dma_ring.c:0x5af = line 1455`), then emits the TX
pring, then the RX pring:

```c
// TX path — gated on present (+0x108) AND not-yet-lowered (+0x118 bool == 0)
if (*(vring+0x108) != 0 && *(uint8_t *)(vring+0x118) == 0) {
    dma_pring_alloc(ctx, vring, /*count=*/*(uint32_t *)(vring+0x100), 0, /*r8=*/2, /*r9=*/1);
    vring_dump_to_pring_descriptors(ctx, /*pring=*/*(vring+0x158), vring,
                                    /*dst=*/vring+0x22, /*count=*/1, /*dir=*/1,
                                    /*n=*/*(uint32_t *)(vring+0x100));
    // fail → nlog "Failed to write descritpors to static tx ring for queue %s"   (sic)
    //        twin "Failed to allocate static tx ring …"
}
// RX path — symmetric on +0x128 / +0x138 bool, count = *(uint32_t *)(vring+0x120)
```

This names the vring's directional metadata blocks: **TX at `+0x100`..`+0x118`,
RX at `+0x120`..`+0x138`**; the `+0x118` / `+0x138` bools gate
"already-lowered / skip"; the per-ring **descriptor counts** are the u32 at
`+0x100` (TX) / `+0x120` (RX).

### 2.3 `vring_dump_to_pring_descriptors` @0x3136e0 → `…_padded` @0x311f10

`vring_dump_to_pring_descriptors` (15 B, a thin tail-call) → `…_padded`
**@0x3136f0** → `vring_dump_to_pring_descriptors_padded` **@0x311f10** (728 B).
The `_padded` variant logs `"Copying vring to pring %s"`, dumps the **TX block**
(`lea 0x100(vring)`) and the **RX block** (`lea 0x120(vring)`) via two
`…descriptors_padded` calls, and finally logs the count invariant:

```
"vring is copied to pring. ndesc=%u/%u"   // actual / expected  (string present)
```

The `_padded` family exists because a *static* ring is **zero-padded** to a fixed
depth; the count-mismatch path feeds the descriptor-count bound layer (the device
"wrote %d, expected %d" invariant, owned by the DMA Part). This host emitter is
the producer of that invariant.

### 2.4 `vring_addr_rewrite` @0x313810 — the BD relocation

Walks the vring page list and patches `buf_ptr` of every BD that falls in a
rebased `[base, base+size)` window:

```c
// vring_addr_rewrite @0x313810  (180 B)
for (page = first; page; page = *(void **)(page + 0x100000)) {  // next-page link
    uint64_t *bd = (uint64_t *)(page + 8);                       // BD array starts at page+8
    for (uint32_t i = 0; i < min(n, 0x10000); ++i, bd += 2) {    // 16-B stride (2 u64s)
        uint64_t buf_ptr = bd[0];                                 // buf_ptr is the first u64
        if (buf_ptr >= old_base && buf_ptr < old_base + size)
            bd[0] += delta;
    }
}
```

This is the host SoC-address patch applied to every staged BD's `buf_ptr` when a
memory region is rebased (the kbin pointer-patch on the descriptor stream). The
16-byte stride + `buf_ptr` at offset 0 of each BD **exactly matches** the
`SDMA_CME_BD_DESC` field map owned by the DMA Part.

---

## 3. The ring-submit path — `sw_dma_queue` cursors

At execute prep, `kbl_compute_setup` **@0x306fb0** (765 B; callees OBSERVED:
`db_physical_core_get_mla_and_tpb`, `hw_exec_queue_add_exec_request`,
`kbl_exec_cc_enq_proxy_tasks`, `model_ref_*`) re-materialises the template rings
and enqueues the exec descriptors.

### 3.1 `hw_exec_queue_add_exec_request_impl` @0x320810

3029-byte submit core. Callees observed:

* `al_udma_m2m_build_copy_descriptor` ×6 — builds the exec-descriptor BDs;
* `hw_exec_queue_add_descriptors` ×3 (§3.2);
* `get_dma_queue_tail_inc_offset` ×2 — composes the doorbell offset (§4.1);
* `dmem_buf_copyin` ×2 — stages the BDs to device DRAM;
* `tdrv_arch_get_evt_addr` / `_evt_accel_addr` — the event/sema CSR sources;
* `is_sow_supported`, `encd_get_{num_descs,…}_model_switch` — the
  `do_model_switch` BDs.

So the host builds the `device_exec_request_t` BDs (the 52-byte layout is owned by
the host-struct reference), copies them to the device exec-request buffer, and
**records the doorbell offset to fire later** — it does **not** fire here.

### 3.2 `hw_exec_queue_add_descriptors` @0x3206f0

Reads the **TX pring** (`*(exec_desc_q)+0x158`) and **RX pring** (`+0x150`) — the
*same slots* `dma_pring_alloc` bound in §2.1 — asserts both non-NULL, then for each
direction:

```c
sw_dma_queue_reserve_descriptors(txq = ehq+0x08, rxq = ehq+0x14, &tx_idx, &rx_idx, n);
sw_dma_queue_set_descriptors    (txq, ..., rx_pring, n);
// failure → nlog (find_queue_bundle_and_instance_by_name context)
```

The `hw_exec_queue` carries the **txq `sw_dma_queue` at `+0x08`** and the **rxq at
`+0x14`** (each 12 bytes; layout owned by the host-struct reference).

### 3.3 `sw_dma_queue_reserve_descriptors` @0x448ce0

Pure host cursor arithmetic over the 12-byte `sw_dma_queue`
`{next_desc_idx, desc_ring_id, desc_count}`. Byte-verified callees:

```c
// sw_dma_queue_reserve_descriptors @0x448ce0  (74 B)
*out_tx = sw_dma_queue_get_next_desc_idx(txq);  // 448cf7  @0x448c80
*out_rx = sw_dma_queue_get_next_desc_idx(rxq);  // 448d02  @0x448c80
sw_dma_queue_inc_next_desc_idx(txq, n);          // 448d10  @0x448c90
sw_dma_queue_inc_next_desc_idx(rxq, n);          // 448d1a  @0x448c90
```

**NOTE — no device write here.** This reads *both* queues' current `next_desc_idx`,
returns them as the reserved slot pair, and advances both by `n`. It is the host
**ring-position bookkeeping**; the device tail does not move until the doorbell in
§4.

### 3.4 `dma_ring_get_sema_to_inc` @0x22dc80 — the completion-semaphore selector

Asserts `ring-type == 2` (`dma_ring.c:0x4fe`), then selects which device semaphore
the device will increment on BD completion:

```c
// dma_ring_get_sema_to_inc @0x22dc80  (56 B)
if (*(uint32_t *)(pring + 0x10) != 0xFFFFFFFF)   // explicit per-ring sema id
    return *(uint32_t *)(pring + 0x10);
else
    return per_direction_sema_table[base + 0x10][dir];   // fall back to per-dir table
```

This is the *source* side of the §5.2 wait — it binds the completion semaphore the
device increments.

---

## 4. The doorbell — three physically distinct variants

The runtime issues **three** different "rings." The DMA Part owns the register
*values*; here is the host code that composes the address and issues the write.

### 4.1 Variant A — the per-queue **tail-pointer-inc** CSR doorbell

`get_dma_queue_tail_inc_offset` **@0x318940** (91 B) composes the SoC address of
the tail-inc CSR for `(base, queue_idx, dir)`. Byte-verified at `0x31894b`:

```c
// dir != 0  (M2S / TX)                          // dir == 0  (S2M / RX)
off = base
    + aws_hal_udma_get_m2s_offset()              //   + aws_hal_udma_get_s2m_offset()        @0x45f220
    + aws_hal_udma_get_m2s_queue_offset(idx)     //   + aws_hal_udma_get_s2m_queue_offset()  @0x45f3e0
    + aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset();  // + the s2m variant               @0x45f920
```

The leaf offset getters trampoline through the per-arch `kaena_khal` vtable
(`aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset` @0x45f8b0 indirects through
`*khal+0x5f8`). The **Cayman** (NC-v3) leaf @0x473bf0 is byte-exact:

```asm
473bf0 aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset_cayman:
  473bf6: call aws_hal_udma_get_m2s_queue_offset_cayman
  473bfb: mov  $0x1038,%edx          ; 0x1038 = 0x1000 + 0x38
  473c04: sub  %rax,%rdx             ; → (0x1000 + 0x38) − queue_region_offset
  473c0a: ret
```

i.e. the tail-inc CSR sits at **`+0x38` within the `0x1000`-stride per-queue
bank** — confirming the device-side `TDRTP_inc`/`RDRTP_inc @+0x38` from the
runtime side. The Cayman **data** tail-inc (a separate "data" doorbell) is at
`+0xe0` (`0x10e0`), the sw_ctrl reg at `+0xb0` (`0x10b0`). The actual write goes
**`al_reg_write32` → `csr_write` → `ndl_bar_write`** — a **BAR CSR write, not an
ioctl** (transport chain byte-verified: `0x265c51: call csr_write`).

### 4.2 Variant B — the SP **top-sequencer host-trigger**

`aws_hal_sp_topsp_set_host_trigger` **@0x457b30** trampolines through `*khal+0x708`.
The **Cayman** impl @0x471b40 is byte-exact:

```asm
471b40 aws_hal_sp_topsp_set_host_trigger_cayman:
  471b44: mov  $0x4,%esi            ; engine = 4 (the top-SP NX-local block)
  471b4b: call aws_hal_arch_cayman_get_xt_local_reg_offset
  471b50: lea  0x15a0(%rax),%rdi    ; xt_local_reg + 0x15a0
  471b57: mov  $0x1,%esi            ; value = 1
  471b60: jmp  al_reg_write32       ; → csr_write → ndl_bar_write (a BAR write)
```

So the host kicks the SP/SEQ by writing **`1` to the top-SP NX-local host-trigger
CSR at `xt_local_reg + 0x15a0`**, engine index `4`. This is the bring-up /
per-engine trigger — distinct from the per-DMA tail-inc and the per-inference
semaphore.

### 4.3 Variant C — the execute-time **kickoff** (the GPSIMD custom-op release)

`kbl_infer_kickoff` **@0x307320** → `exec_kickoff_infer` **@0x2632e0** (71 B; the
mla/tpb anchor is owned by the host-struct reference) issues **one**
`ndl_nc_semaphore_increment(mla->device, idx = tpb_idx, value = 1)`.

`ndl_nc_semaphore_increment` **@0xc3ba0** is a thin `ioctl()` wrapper —
byte-verified, **a kernel-driver ioctl, NOT a BAR write**:

```asm
c3ba0 ndl_nc_semaphore_increment:
  c3ba4: mov  0x278(%rdi),%edi      ; fd  = device->fd  (from mla->device + 0x278)
  c3bac: mov  %edx,0x8(%rsp)        ; value
  c3bb5: mov  %esi,0x4(%rsp)        ; idx
  c3bb9: mov  $0x80084e29,%esi      ; ioctl request number  (8-byte arg, dir/in)
  c3bc2: call ioctl@plt
  c3bcb: ret
```

That single semaphore release **unblocks the whole prebuilt per-engine 64-byte
sequencer stream set** — including the POOL stream's `LOAD_POOL_ARGUMENT` and the
idx-16 custom-op DMA triggers. `kbl_infer_kickoff` then enters
`exec_wait_round_robin` (§5.2). The ioctl request `0x80084e29` decodes as a
`_IOW`-style 8-byte argument.

### Summary — the three doorbells

| Doorbell | Host issuer | Transport | Granularity / purpose |
|---|---|---|---|
| **Tail-ptr-inc** | `get_dma_queue_tail_inc_offset` → `al_reg_write32` | `csr_write` → `ndl_bar_write` (BAR) | **per-DMA** BD-ring launch (M2S/S2M `+0x38`) |
| **SP host-trigger** | `aws_hal_sp_topsp_set_host_trigger` → `al_reg_write32` | `csr_write` → `ndl_bar_write` (BAR) | **per-engine** SEQ trigger (`xt_local+0x15a0` ← 1) |
| **NC semaphore** | `ndl_nc_semaphore_increment` | `ioctl(req=0x80084e29)` (driver) | **per-inference** kickoff (releases all engine streams) |

> **GOTCHA — three rings, three transports.** A reimplementer who routes the
> per-inference kickoff through a BAR write (like the tail-inc) will deadlock: the
> kickoff is an `ioctl` to the NeuronCore driver, because only the driver holds the
> mapping from the `mla->device` handle (fd at `+0x278`) to the physical semaphore.
> Conversely, routing the per-DMA tail-inc through the semaphore ioctl would
> serialize every BD ring through a syscall. The split is deliberate.

---

## 5. The completion-wait — two models

### 5.1 The load/stage-time per-DMA poll — `dma_alloc/wait_for_completion_handle`

**ALLOC** — `dma_alloc_completion_handle` @0x22de20 (196 B): obtains a handle slot
via `tdrv_arch_get_completion_handle_mem_location` @0x309520 (per-arch
sunda/cayman/mariana), `dmem_alloc(size=8, name="completion marker")` a device-DRAM
8-byte word, and `dmem_buf_copyin`s the seed. The 8-byte word is the device-side
**completion marker** the DMA engine overwrites.

**WAIT** — `dma_wait_for_completion_handle` @0x22def0 (468 B) busy-polls it.
Byte-verified, with the `0xABCDEF01` sentinel, `usleep(10)`, and the `×0x2710`
(=10000) platform scaling:

```c
// dma_wait_for_completion_handle @0x22def0
uint32_t armed = 0xABCDEF01;                       // 22df0e: movl $0xabcdef01,0xc(%rsp)
uint64_t timeout = handle_idx * 0x190;             // base loop count
if (tdrv_get_platform_type() == 1)
    timeout *= 0x2710;                              // 22dfe0: imul $0x2710,%rdx  (×10000)
// "loop %lu times usleep(%u) for %u descriptors"
for (uint64_t i = 0; i < timeout; ++i) {
    uint32_t word;
    dmem_buf_copyout(handle, &word, 4);             // 22dfa0: call dmem_buf_copyout
    if (word == 0xABCDEF01) {                       // 22dfa9: cmp $0xabcdef01,%eax  — still armed
        usleep(10);                                 // 22dfb0/b9: mov $0xa,%edi; call usleep
        continue;
    }
    // engine wrote the descriptor-done marker → SUCCESS
    uint32_t zero = 0, re = 0xABCDEF01;
    dmem_buf_copyin(handle, &zero, 4);              // 22e012: re-arm step 1 (write 0)
    dmem_buf_copyin(handle, &re,   4);              // 22e027: re-arm step 2 (write sentinel)
    nlog("DMA completed %u descriptors");
    return 0;
}
nlog("Error: DMA completion timeout");              // string present
return ERR;
```

So the load/stage-time per-DMA completion is a **software dmem busy-poll against a
sentinel, not an interrupt**. The sentinel value `0xABCDEF01`, the `usleep(10)`
spacing, and the arch-scaled timeout policy are all runtime-side. **The re-arm is
two separate copyins** (write `0`, then write `0xABCDEF01`) so the marker is reset
for reuse before the function returns.

> **GOTCHA — the two-step re-arm is racy if reordered.** The reset writes `0`
> *before* the sentinel; a reimplementer who writes the sentinel first (or fuses
> them into one 8-byte store with the wrong half) can leave the marker transiently
> showing a stale done-value, which the *next* wait would mis-read as immediate
> completion. The observed order (0 then sentinel) is the safe sequence.

### 5.2 The execute-time per-inference poll (NQ drain)

After the kickoff semaphore (§4.3), `kbl_infer_kickoff` polls via
`exec_wait_round_robin`; `exec_kickoff_infer` itself also calls
`notification_read_exec_queue` — the device→host **Notification-Queue** drain (the
16-byte NQ records; the `tpb_t.notification` ring). This completion path is owned by
the execute-time dispatch / interrupt references and is named here only to
distinguish it from §5.1:

* **§5.1** = the per-DMA marker poll, for *staging copies* (table dumps, refill
  rings) — `0xABCDEF01` dmem busy-poll.
* **§5.2** = the per-inference NQ poll, for the *released stream's* done signal —
  gated on the kickoff semaphore.

The callees are `[HIGH × OBSERVED]`. See
[Execute-Time GPSIMD Custom-Op Dispatch](execute-time-dispatch.md) for the NQ-poll body.

---

## 6. The device-side DGE DMA-priority gate, configured from the host

`libnrtucode_internal.so` exports the host accessors that program the **device** Q7
DGE's DMA-priority gate. The 4-byte `nrtucode_dge_mailbox_t`
`{reserved, priority, bitmask}` layout and the gate algorithm itself are owned by
the host-struct / [DGE host-API](dge-host-api.md) references; the **gate rule**, per
`nrtucode.h` (lines 504-505): *"If DGE operation priority > mailbox priority: apply
(&) the mailbox's DMA bitmask."* Here is the **host control flow** that writes the
config, plus a correction to the kind guard. All three accessor groups share one
shape: a NULL-core abort, a boot-state guard, a core-KIND bitmask guard, then a
core-ops-vtable read/write to a fixed offset within `core->base` (= `core+0x20`).

### 6.1 The shared guards

Byte-verified in `nrtucode_core_dge_set_priority_class_map` @0x9b1000 (and identical
in the others):

```c
if (*(uint32_t *)(core + 0x30) != 1)            // 9b100a: cmpl $1,0x30  — boot_state must be BOOTED
    { nrtucode_context_log(...); return 8; }     // 8 = INVALID / NOT_BOOTED
uint64_t kind = *(uint32_t *)(core + 0x10);     // 9b1010: mov 0x10  — core->kind
if (!( (1ull << kind) & 0x102020204 ))          // 9b101c/26: movabs $0x102020204; bt %rax,%rsi
    return ERR;                                  //   (9b1013: cmp $0x20  — upper-bound guard for bt)
```

The mask `0x102020204` has bits set at positions **2, 9, 17, 25, 32** (decoded by
brute force). Against `nrtucode.h`'s `nrtucode_coretype_t` (header confirms exactly
five `*_NX_POOL` entries) these are precisely the **five NX_POOL kinds**:

| Bit | Coretype (nrtucode.h) | Gen |
|---|---|---|
| 2 | `NRTUCODE_CORE_SUNDA_NX_POOL` | NC-v2 |
| 9 | `NRTUCODE_CORE_CAYMAN_NX_POOL` | NC-v3 |
| 17 | `NRTUCODE_CORE_MARIANA_NX_POOL` | NC-v4 |
| 25 | `NRTUCODE_CORE_MARIANA_PLUS_NX_POOL` | NC-v4+ |
| 32 | `NRTUCODE_CORE_MAVERICK_NX_POOL` | NC-v5 (Maverick header-observed → INFERRED gen) |

> **CORRECTION — the gate admits the NX_POOL cores ONLY.** Any prior phrasing of
> "pool kinds" is too broad: the bitmask `0x102020204` admits the **five NX_POOL
> cores** — **NOT** the Q7_POOL cores, and **NOT** ACT/DVE/PE/SP/TOPSP. The DGE
> DMA-priority mailbox is an **NX-pooling-engine facility**.

**The core-ops vtable.** `core`'s first member (`*(core)`) is an ops table:
`*(ops) = READ(ctx, soc_addr, len, *out)` and `*(ops+8) = WRITE(ctx, soc_addr, len,
*in)`. The accessors call these to touch device CSR/DRAM (the device-access HAL).
`core+0x20` is the core's SoC base.

### 6.2 `nrtucode_core_get_dge_mailbox_addr` @0x9b11e0

Args `(core, num_mailbox, *addr)`. If `num_mailbox <= 4` it **reads back** each of
the 4 mailboxes (loop idx 0..3: default scratch `0xffffff00`, then a READ via the
ops table), logs per-mailbox, and returns `*addr = core->base + 0x28`. Byte-verified:

```asm
9b1237: cmp  $0x4,%rsi            ; num_mailbox <= 4 guard
9b1247: lea  0x28(,%r15,4),%rbp   ; core->base + 0x28 + idx*4  — the readback cursor
9b1290: movl $0xffffff00,...      ; per-mailbox default scratch
```

The header (`nrtucode.h:506-522`) shows the intended host use:
`get_dge_mailbox_addr(core, 4, &addr)`, then the host *itself* `write()`s the 16
bytes (or one 4-byte mailbox at `addr + 3*4`) to the device. So the **mailbox array
base = `core->base + 0x28`** (= +40), **4 entries × 4 bytes**.

### 6.3 `nrtucode_core_private_set/get_dge_mailbox` @0x9b1380 / @0x9b1400

```c
// nrtucode_core_private_set_dge_mailbox @0x9b1380  (set one mailbox by value)
//   guards as §6.1 (boot_state, NX_POOL kind), then:
if (idx > 3) return 8;                          // 9b13a3: cmp $0x3,%rsi
ops->WRITE(ctx, core->base + 0x28 + idx*4, 4, &mailbox_value);  // 9b13b7: add $0x28,%rsi
// _get_dge_mailbox_values @0x9b1400 — symmetric, ops->READ from the same address
```

So the device mailbox is written/read **one 4-byte entry at a time** over the
core-ops channel; `idx > 3` is rejected.

### 6.4 `nrtucode_core_dge_set/get_priority_class_map` @0x9b1000 / @0x9b10f0

```c
// nrtucode_core_dge_set_priority_class_map @0x9b1000
//   guards as §6.1, then:
if (priority_classes == NULL) abort();          // 9b10bc: call abort@plt  (fprintf+abort)
if (num_classes > 4) return ERR;                // 9b1035: cmp $0x4,%rdx
ops->WRITE(ctx, core->base + 0x18, num_classes*4, priority_classes);  // 9b104d: add $0x18,%rsi
```

So the DGE **priority-CLASS MAP lives at `core->base + 0x18`** (= +24, ≤5 classes),
and the **mailbox array at `core->base + 0x28`** — adjacent device-config blocks.
Per `nrtucode.h:466`, `priority_classes[0]` maps to **ISA priority index 1** (class
0 is the built-in default). This is the host knob that maps a DMAQoSClass / op
priority onto the ISA priority byte the §6.1 gate compares.

> **CROSS-REF — where the op's priority comes from.** `model_t.cc_ctx.curr_priority_class`
> and the host encoder knobs `encd_set_ctx_curr_priority_class` @0x238b00 /
> `encd_get_ctx_curr_priority_class` @0x238b90 (in libnrt) decide which priority an
> *emitted* op carries into this device gate. `[MED × INFERRED]` (the linkage from
> the encoder knob to the device-gate compare is reasoned over the two observed
> sites, not single-stepped.)

> **NOTE — adjacency map of the device config block (all relative to `core->base`).**
> `+0x18` priority-class map (≤5 × u32) · `+0x28` `dge_mailbox[4]` (4 × 4 B). The
> two blocks are written by separate accessors but live in one contiguous
> device-config window; a reimplementer staging both can issue one combined WRITE
> covering `+0x18`..`+0x37` if the ops HAL permits a 32-byte span.

---

## 7. Reimplementer control-flow summary — the host handoff, end to end

**LOAD / STAGE (per NEFF, once):**

1. **Build** a paged vring of 16-byte BDs: `dma_util_vring_append_descs` @0x316d10
   selects a BD writer by desc-type (`COPY=0` / `TRANSPOSE=5` / `1..4` CCE family,
   via `mov 0x4(%r8),%eax`) and folds the N-D walk into a per-BD count via the arch
   packetization granule (`div` by `*(r8+0x14ac)`).
2. **Alloc + lower:** `dma_ring_create_prings_from_vring` @0x22e310 → for TX then RX,
   `dma_pring_alloc` @0x22d8b0 (`calloc(1,0x20)` the 32-B pring + `dma_ring_alloc` the
   device backing) and bind at **vring+0x158 (TX, r9d==1)** / **vring+0x150 (RX,
   r9d==2)**; `vring_dump_to_pring_descriptors` @0x3136e0 copies the BD blocks
   (vring+0x100 TX / +0x120 RX) into the prings, zero-padded to ring depth; the count
   invariant `"ndesc=%u/%u"` is checked.
3. **Relocate:** `vring_addr_rewrite` @0x313810 patches every BD `buf_ptr(+0)` for any
   rebased region (16-B stride, pages chained at `+0x100000`).
4. **Gate (GPSIMD only):** `set_dge_mailbox` writes the 4×4-B mailbox at
   `core->base+0x28`; `set_priority_class_map` writes the class map at
   `core->base+0x18` — both over the core-ops WRITE vtable, gated on
   `boot_state==BOOTED` and `kind ∈ {the 5 NX_POOL kinds}`.

**EXEC-SETUP (per inference):**

5. `kbl_compute_setup` @0x306fb0 → `hw_exec_queue_add_exec_request_impl` @0x320810
   builds the exec BDs (`al_udma_m2m_build_copy_descriptor`), `dmem_buf_copyin`s them
   to device, and **records** the tail-inc doorbell offset
   (`get_dma_queue_tail_inc_offset` = base + m2s/s2m queue offset + `+0x38`).
6. `hw_exec_queue_add_descriptors` @0x3206f0 → `sw_dma_queue_reserve/set_descriptors`
   advance the host txq (`ehq+0x08`) / rxq (`ehq+0x14`) cursors and write the BDs into
   the bound prings.

**FIRE + WAIT:**

7. **Doorbell:** per-DMA = `al_reg_write32(tail_inc_csr, n)` via `ndl_bar_write`
   (BAR); per-engine = `aws_hal_sp_topsp_set_host_trigger` (`xt_local+0x15a0` ← 1) via
   `ndl_bar_write`; per-inference kickoff =
   `ndl_nc_semaphore_increment(mla->device, tpb_idx, 1)` via `ioctl(0x80084e29)`
   (`exec_kickoff_infer`).
8. **Wait:** staging copies poll `dma_wait_for_completion_handle` (dmem `0xABCDEF01`
   sentinel busy-poll, `usleep(10)`, arch-scaled `×10000` timeout); the inference polls
   the NQ (`exec_wait_round_robin` / `notification_read_exec_queue`).

### Key host offsets

| Location | Field |
|---|---|
| `vring+0x100` | u32 TX desc count · `+0x108`/`+0x118` TX present / lowered gates |
| `vring+0x120` | u32 RX desc count · `+0x128`/`+0x138` RX present / lowered gates |
| `vring+0x150` | RX pring ptr (bound when `r9d==2`) |
| `vring+0x158` | TX pring ptr (bound when `r9d==1`) |
| `vring+0x20` | "sealed" bool (`dma_pring_alloc` guard) |
| `page+0x100000` | next vring page link · BD array at `page+8`, 16-B stride, `buf_ptr` at BD+0 |
| `pring+0x10` | completion sema id (`0xFFFFFFFF` ⇒ use per-dir table) |
| `hw_exec_queue+0x08` / `+0x14` | txq / rxq `sw_dma_queue` (12 B each) |
| `core+0x10` / `+0x20` / `+0x30` | kind / SoC base / boot_state (==1) |
| `core->base+0x18` | DGE priority-class map (≤5 × u32) |
| `core->base+0x28` | `dge_mailbox[4]` (4 × 4 B) |
| per-queue CSR bank (Cayman) | tail-inc `+0x38` (`0x1038`), data-tail-inc `+0xe0`, sw_ctrl `+0xb0` |
| SP top-SP host-trigger | `xt_local_reg + 0x15a0` ← 1 (engine 4) |
| NC semaphore ioctl | request `0x80084e29`, fd at `mla->device + 0x278` |

---

## 8. Cross-reference / dedup table

**Owned elsewhere (cited, never restated here):**

| Reference | Content |
|---|---|
| [DMA / Descriptor / Memory Subsystem](../dma/descriptor-model.md) | the `SDMA_CME_BD_DESC` 16-B byte map · 64-B TPB DMA words · the desc-op enum byte table |
| [DGE Descriptor-Builder + SDMA QoS/Arbitration](../dma/dge-builder-qos.md) | the device DGE builder (GENERATE/DIMPUSH/REGWRITE) · al_udma register VALUES · submit→complete HW dataflow |
| [The DGE Host-Private API](dge-host-api.md) | the `nrtucode_dge_mailbox_t` 4-B layout + the priority-gate algorithm in full |
| [Execute-Time GPSIMD Custom-Op Dispatch](execute-time-dispatch.md) | `LOAD_POOL_ARGUMENT` (`0x107A`) · the per-op micro-program · the NQ drain body |

**Owned here (the runtime control flow that fills / submits the above):**

| § | Functions |
|---|---|
| §1 | `dma_util_vring_append_descs` — host BD-filler + type dispatch |
| §2 | `dma_pring_alloc` / `dma_ring_create_prings_from_vring` / `vring_dump_to_pring_*` / `vring_addr_rewrite` — vring→pring lowering + relocation + ring alloc |
| §3 | `hw_exec_queue_add_exec_request_impl` / `_add_descriptors` / `sw_dma_queue_*` / `dma_ring_get_sema_to_inc` — ring-submit + sema-bind |
| §4 | `get_dma_queue_tail_inc_offset` / `set_host_trigger` / `ndl_nc_semaphore_increment` — the **three** doorbells + their transport (`ndl_bar_write` vs `ioctl`) |
| §5 | `dma_alloc/wait_for_completion_handle` — the `0xABCDEF01` dmem-poll completion |
| §6 | `nrtucode_core_{get_dge_mailbox_addr, private_set/get_dge_mailbox, dge_set/get_priority_class_map}` — the host control flow that programs the device DGE gate; **CORRECTION: the kind guard admits the five NX_POOL kinds only** |

### Confidence ledger

* **HIGH × OBSERVED** — every function address; the vring/pring offsets
  (`+0x100`/`+0x120`/`+0x150`/`+0x158`/`+0x20`); the DGE offsets (`+0x18` map,
  `+0x28` mailbox); the kind bitmask `0x102020204` → 5 NX_POOL kinds; the `+0x38`
  tail-inc; the `0xABCDEF01` sentinel + `usleep(10)` + `×10000` scaling; the three
  doorbell mechanisms and their transports.
* **MED** — the encoder ctx-priority → device-gate linkage (§6.4 cross-ref); the §1.3
  dim-division *granule-field* semantics (`r8+0x14ac`).
* **LOW** — absolute run-time CSR/SoC addresses and remapper VALUES (runtime policy /
  device-resident; owned by the DMA Part, not recomputed here).
