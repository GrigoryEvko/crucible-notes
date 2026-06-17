# KaenaHal: UDMA/SDMA Descriptor Build and IOFIC

> *All addresses, offsets, symbol names, and `kaena_khal` slot offsets on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped; all four `PT_LOAD` segments are identity-mapped, so `.text`/`.rodata`/`.data` are VMA == file offset (`.bss` is `NOBITS`). The vendored HAL package is `KaenaHal-2.31.0.0` (Amazon `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`); the four TUs owning this page root in `.../src/src/common/udma/{al_hal_udma_m2m.c, al_hal_udma_main.c, al_hal_udma_config.c}` and `.../src/src/common/{udma/al_hal_udma_iofic.c, iofic/al_hal_iofic.c}`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every symbol is sized by `nm -S` (all local `t`), every register offset is `objdump`-decoded from the body, and the descriptor bit constants are cross-reconciled against the kernel twin ([kernel/udma-m2m](../kernel/udma-m2m.md), [kernel/udma-iofic](../kernel/udma-iofic.md)). The UDMA TUs were compiled **without `-g` line tables** (`addr2line` returns `?:?`); the `.c:line` citations below come from the compiler-embedded `__assert_fail`/`al_hal_log` literals, **not** the DWARF line program — stated per claim. · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

This page documents the **userspace KaenaHal twin** of the kernel UDMA/IOFIC stack: the `al_udma_m2m_*` descriptor builders, the `al_udma_*` queue/completion/tgtid/TTT configuration layer, the `aws_hal_udma_get_*_offset` per-arch register-offset trampolines, and the `al_udma_iofic_*` / `al_iofic_*` interrupt-controller primitives — all statically linked into `libnrt.so`. It is the half of the DMA stack that runs in the runtime process rather than the kernel driver, and its decisive property is **byte-identity**: the 16-byte hardware descriptor these builders emit is bit-for-bit the same wire format the kernel `udma/udma_m2m.c` emits, and the IOFIC group/level/offset model is the same one `udma/udma_iofic.c` programs. The two code lines author the identical silicon-facing bytes from opposite sides of the `/dev/neuron` boundary.

The reader who owns the [16-byte UDMA descriptor](../dma/descriptor-format.md) page and the kernel [udma-m2m](../kernel/udma-m2m.md) builder already owns most of the frame. A copy is **never one descriptor**: it is a *pair* — one m2s/Tx descriptor that reads the **source** onto the queue's TX ring, one s2m/Rx descriptor that writes the **destination** onto the same queue's RX ring — each stamped with its own ring's 2-bit phase, both `memcpy`'d 16 bytes into their ring slot, and launched RX-doorbell-then-TX-doorbell. This page does **not** re-derive that wire format (the `len_ctrl` bit positions, the `meta_ctrl` overlays, the `length == 0 ⇒ 65536` overload, the write-barrier at bit 26): those are *boundaries* owned by [descriptor-format](../dma/descriptor-format.md). What it owns is the userspace *build surface* on top of that format — the packet-builder loop, the single-shot copy/CRC prep, the bulk reserve/set descriptor ops, and the queue/IOFIC configuration that brings the engine up.

The userspace side carries one structural element the kernel side has no analog for: the **`aws_hal_udma_get_*_offset` register-offset trampolines** (`al_hal_udma_m2m.c`). The kernel driver compiles one fixed register layout (`struct udma_gen_regs_v4`); the userspace HAL instead supports three silicon generations (SUNDA=2, CAYMAN=3, MARIANA=4) from one binary, so every per-queue register offset is resolved at runtime through an indirection in the global `kaena_khal` dispatch object — assert the arch is latched, assert the slot is non-NULL, tail-call the registered `*_sunda`/`*_cayman`/`*_mariana` leaf. The page applies the recurring H3 vocabulary to four units: the M2M descriptor build (§1, with the explicit byte-identity NOTE), the queue/completion/tgtid/TTT configuration (§2), the offset-trampoline indirection (§3), and the userspace IOFIC twin (§4, cross-linking the kernel cause model).

For reimplementation, the contract is:

- **The M2M build surface** — the paired Tx(source)+Rx(dest) descriptor model and its userspace builders (`build_packet` loop, `copy_prepare_one`, `crc_prepare_one`, `reserve_descriptors`, `set_descriptors`, `copy_start`), all of which emit the **byte-identical** 16-byte wire format owned by [descriptor-format](../dma/descriptor-format.md) — reproduce the *build algorithm* here, the *bytes* there.
- **The config layer** — the queue-scheduling / completion / target-ID-steering / Target-Translation-Table (rev-4 only) register pokes (`al_udma_*` in `al_hal_udma_config.c`), every one rev-gated on `udma->rev_id @+0x1880` against `AL_UDMA_REV_ID_4 == 4`.
- **The offset-trampoline indirection** — `aws_hal_udma_get_<reg>_offset` is `assert(arch != INVALID); fp = kaena_khal.khal_udma.<member>; assert(fp != NULL); jmp *fp`; the slot offset is `slot_vma − 0xCAEB80`, the per-arch return value lives in the registered leaf, not the trampoline.
- **The IOFIC twin** — the `al_iofic_*` per-group `0x40`-byte register window (`cause@+0x00`, `mask@+0x10`, `mask_clear@+0x18`, `control@+0x28`) with the `~mask` write inversion on unmask, and the `al_udma_iofic_*` policy that composes it; the *cause model* (two-level funnel, group catalog) is owned by [kernel/udma-iofic](../kernel/udma-iofic.md).

| | |
|---|---|
| **Vendored HAL** | `KaenaHal-2.31.0.0` (AnnapurnaLabs/Alpine; `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`) — statically linked, no own DWARF CU |
| **M2M build TU** | `.../common/udma/al_hal_udma_m2m.c` — builders + offset trampolines, `.text 0x45ced0..0x45fad6` |
| **Core/config TUs** | `al_hal_udma_main.c` (`0x45fae0..0x46048e`) · `al_hal_udma_config.c` (`0x475070..0x476e32`) |
| **IOFIC TUs** | `al_hal_udma_iofic.c` (3 fns) · `al_hal_iofic.c` (21 fns), `.text 0x476e40..0x47cdc5` |
| **Wire format** | 16-byte `union udma_desc` — **boundary**, owned by [descriptor-format](../dma/descriptor-format.md) (do **not** re-derive) |
| **Copy unit** | a **pair**: m2s/Tx (reads source) + s2m/Rx (writes destination), one queue, `memcpy` 16 B each |
| **Pair builder** | `al_udma_m2m_build_copy_packet @0x45d550` → `_build_cme_packet @0x45d400` → `_build_packet @0x45cf70` |
| **Single-shot** | `al_udma_m2m_copy_prepare_one @0x45d580` · `_crc_prepare_one @0x45dc50` |
| **Doorbell** | `al_udma_m2m_copy_start @0x45ed60` → `al_udma_desc_action_add` **RX then TX** |
| **Dispatch object** | `kaena_khal @.bss 0xCAEB80` — `khal_udma` offset-getter slots `+0x578..+0x618` (`0xCAF0F8..0xCAF198`) |
| **Arch gate** | `al_hal_tpb_get_arch_type @0x44bca0` ([hal-adapter §3](hal-adapter.md)); enum `{SUNDA=2, CAYMAN=3, MARIANA=4}` |
| **Rev gate** | `udma->rev_id @+0x1880` vs `AL_UDMA_REV_ID_4 == 4`; `al_udma_init` rejects `rev != 4` |
| **IOFIC group window** | `0x40`-byte stride; `cause@+0x00` · `mask@+0x10` · `mask_clear@+0x18` · `control@+0x28` |
| **Unmask convention** | `al_iofic_unmask @0x47c7a0` writes **`~bits`** to `mask_clear@+0x18` (set bit ⇒ unmasked) |

> **NOTE — byte-identity with the kernel is the spine of this page.** Every descriptor field, every bit position, and the RX-then-TX doorbell order are **the same** as the kernel `udma/udma_m2m.c` builder. The full field-by-field userspace↔kernel agreement proof lives in [descriptor-format §8](../dma/descriptor-format.md#8-userspace--kernel-byte-agreement); this page cites it and builds *on* it. Where a userspace builder pins a bit constant (e.g. `set_first` is `orl $0x04000000`), it is reproduced as a builder-level cross-check, not a fresh decode of the format.

---

## 1. The M2M Descriptor Build Surface

### Purpose

`al_hal_udma_m2m.c` is the userspace descriptor-authoring layer — the runtime-side twin of the kernel's `udma_m2m.c`. It turns a `(src, dst, size, barrier)` request into the two hardware descriptors the silicon executes, and it exposes three further surfaces the kernel path collapses: a multi-descriptor **packet builder** (`build_packet`) that walks a caller callback to emit N Tx + N Rx descriptors, **single-shot** copy/CRC prep, and **bulk** reserve/set/start operations for the vring packer. All of them emit the byte-identical 16-byte `union udma_desc` owned by [descriptor-format](../dma/descriptor-format.md).

The layer divides into four clusters, all local `t` symbols in `.text 0x45ced0..0x45f2f6`:

- **Descriptor control-word bit ops** (8 leaf setters/clearers) — toggle FIRST/LAST/CONCAT/DMB in a Tx control word; `get_tx_desc_size` decodes the `len0` trick.
- **Packet builders** (3) — `build_packet` (the core N-descriptor loop) and its wrappers `build_cme_packet` (adds an optional CME meta-descriptor) and `build_copy_packet` (= `build_cme_packet` with `cme=0`).
- **Single-shot + bulk** (5) — `copy_prepare_one`, `crc_prepare_one`, `reserve_descriptors`, `set_descriptors`, `copy_start`.
- **Queue maintenance + offset trampolines** (8) — `rearm_queue`/`reassign_queue` (unimplemented stubs), `set_axi_error_abort`, the two prefetch-threshold setters, and three `get_*_offset` trampolines (the full trampoline set is §3).

### Entry Point

The pair-build path, reached from the executor's submit funnel, is shape-identical to the kernel (`udma_m2m_build_descriptor`):

```text
vring_pack_descs / vring_add_dma_packet_v2 / al_mla_udma_m2m_build_packet_<arch>
  └─ al_udma_m2m_build_copy_packet (0x45d550)        ── thin wrapper: cme = 0
       └─ al_udma_m2m_build_cme_packet (0x45d400)     ── if cme!=0, prepend CME meta-desc
            └─ al_udma_m2m_build_packet (0x45cf70)     ── CORE: N Tx + N Rx descriptor loop
                 ├─ al_udma_m2m_build_tx_descriptor (0x45cac0)   ── BOUNDARY: 16B Tx slot (source)
                 ├─ al_udma_m2m_build_rx_descriptor (0x45cbb0)   ── BOUNDARY: 16B Rx slot (dest)
                 ├─ al_sdma_m2s_set_write_barrier / _set_notification_mask  ── BOUNDARY: al_hal_sdma
                 └─ al_hal_tpb_get_arch_type (0x44bca0)           ── arch gate (notif⇒v4, SOW≠v2)

[single-shot path]
ndma-equivalent caller
  └─ al_udma_m2m_copy_prepare_one (0x45d580)          ── resolve TX+RX handles, check room, build 1 pair
       └─ al_udma_m2m_build_copy_descriptor (0x45cca0)  ── BOUNDARY: the PAIR orchestrator
  └─ al_udma_m2m_copy_start (0x45ed60)                 ── doorbell: RX then TX
```

### Algorithm — the control-word bit ops

The eight leaf setters are the byte-level proof that the userspace control word is the same word the kernel packs. Each is a one-instruction RMW on the Tx descriptor's word0 (or `meta_ctrl` for the DMB path), `objdump`-firm:

```c
// al_udma_m2m_set_first  @0x45cf00 :  *desc |= 0x04000000   // BIT26 FIRST   (orl $0x04000000,(%rdi))
// al_udma_m2m_set_last   @0x45cf10 :  *desc |= 0x08000000   // BIT27 LAST
// al_udma_m2m_set_concat @0x45cf20 :  *desc |= 0x80000000   // BIT31 CONCAT
// al_udma_m2m_clear_first  @0x45cf30 : *desc &= ~0x04000000
// al_udma_m2m_clear_last   @0x45cf40 : *desc &= ~0x08000000
// al_udma_m2m_clear_concat @0x45cf50 : *desc &= ~0x80000000

// al_udma_m2m_get_tx_desc_size @0x45cf60 — decode the len0 trick (length 0 encodes 64 KiB)
function get_tx_desc_size(desc) -> u32:
    len = (u16) desc->len_ctrl                     // [15:0]
    return len ? len : 0x10000                     // 0 ⇒ 65536  (cmove $0x10000, objdump-firm)

// al_udma_m2m_set_dmb @0x45ced0 — set the write-barrier via the SDMA overlay
function set_dmb(desc):
    w = desc->meta_ctrl_word(at +4)
    al_sdma_m2s_set_write_barrier(&w)              // BOUNDARY: al_hal_sdma — flips meta_ctrl bit26
    desc->meta_ctrl_word(at +4) = w
```

> **NOTE — these bit positions are byte-identical to the kernel.** `FIRST = BIT26`, `LAST = BIT27`, `CONCAT = BIT31`, the `length == 0 ⇒ 65536` decode, and the write-barrier living in the `meta_ctrl` overlay are all owned by [descriptor-format §2/§3](../dma/descriptor-format.md#2-word0--len_ctrl) and proven to match the kernel `M2S_DESC_*` macros bit-for-bit. The setters above are reproduced only to show that the userspace `al_udma_m2m_*` surface manipulates **that same word**, not a different one. A reimplementer building these wrappers takes the bit values from the format page; the wrappers here are a 5-instruction RMW veneer over it.

### Algorithm — the packet builder

`al_udma_m2m_build_packet` (`@0x45cf70`) is the core N-descriptor emission loop — the userspace generalization of the kernel's single-pair build. It validates the TX-descriptor count against a per-arch cap, gates the notification mask and RX strong-ordering on arch, then walks a caller-supplied callback to fetch each block and emits a Tx then an Rx descriptor per block:

```c
// al_udma_m2m_build_packet @0x45cf70 — emit a multi-descriptor packet (N Tx + N Rx)
// fetch_block is the caller callback: (ctx, &block_ptr, &ring_id) -> per-block (addr, size)
function al_udma_m2m_build_packet(q, num_blocks, fetch_block, ctx, flags, want_notif, want_sow):
    if num_blocks > MAX_DESCS_PER_PACKET:          // 0x80 SUNDA / 0x41 CAYMAN+MARIANA  (per-arch cap)
        log("unsupported number of TX descs per packet: %u, max allowed: %u")
        return -EINVAL                             // -22

    arch = al_hal_tpb_get_arch_type()              // 0x44bca0, gate
    if want_notif and arch != MARIANA:             // notification mask is V4-only
        log("sdma notification mask is not supported on arch version: %u")
        return -EINVAL
    if want_sow and arch == SUNDA:                 // strong-ordered RX write forbidden on TRN1/v2
        log("udma strong ordering not supported in TRN1!")
        return -1

    // first Tx descriptor carries FIRST; last carries LAST; set WB / notif on meta as requested
    for i in 0 .. num_blocks-1:
        fetch_block(ctx, &blk, &tx_ring_id)        // pull one block's (addr,size) + ring phase
        tx_flags = flags
        if i == 0:           tx_flags |= 0x04000000   // FIRST (BIT26)
        if i == num_blocks-1: tx_flags |= 0x08000000  // LAST  (BIT27)
        rc = al_udma_m2m_build_tx_descriptor(slot, tx_ring_id, blk.addr, blk.size, meta, tx_flags)  // BOUNDARY
        if rc: { log("Failed to add TX descriptor %u of %u"); return rc }

    for i in 0 .. num_blocks-1:                     // matching Rx descriptors (dest side)
        fetch_block(ctx, &blk, &rx_ring_id)
        rx_flags = 0
        if want_sow: rx_flags |= 0x20000000          // BIT29 strong-order WR (arch>2 only)
        rc = al_udma_m2m_build_rx_descriptor(slot, rx_ring_id, blk.addr, blk.size, rx_flags)  // BOUNDARY
        if rc: { log("Failed to add RX descriptor %u of %u"); return rc }
    return 0

// al_udma_m2m_build_cme_packet @0x45d400 — optionally prepend a 16B CME meta-descriptor, then build_packet
function build_cme_packet(q, ..., cme):
    if cme != 0:
        meta_w0 = (cme_index << 24) | 0x04800000     // FIRST(BIT26) | META(BIT23); al_copy_descriptor publishes 16B
        al_copy_descriptor(slot, &meta_desc)         // BOUNDARY: = memcpy
    return al_udma_m2m_build_packet(q, ..., concat = (cme != 0))   // CONCAT-chain the meta desc

// al_udma_m2m_build_copy_packet @0x45d550 — plain copy: build_cme_packet with cme = 0
```

> **GOTCHA — the per-arch caps and gates here mirror the descriptor-format per-arch deltas, but are policy, not wire-format.** `build_packet` rejects a notification request on non-MARIANA and a strong-ordered RX write on SUNDA, and clamps `num_blocks` to a per-arch max (`0x80` on SUNDA, `0x41` on CAYMAN/MARIANA — userspace `init_engine @0x45c820`). None of these alters a single descriptor *bit*; they gate *which* capability a caller may request on a given generation. The byte layout of a descriptor built for v2 and one built for v4 with the same `(src,dst,size,phase,barrier)` is identical ([descriptor-format §7](../dma/descriptor-format.md#7-per-arch-deltas)).

### Algorithm — single-shot prepare and the doorbell

The single-shot path is the userspace analog of the kernel's `udma_m2m_copy_prepare_one` / `_copy_start`. It resolves both queue handles, checks free room on both rings, pulls one slot and phase from each, builds one pair, and (separately) rings both doorbells RX-then-TX:

```c
// al_udma_m2m_copy_prepare_one @0x45d580 — one TX + one RX descriptor for a single src->dst copy
function copy_prepare_one(udma, qid, s_addr, d_addr, size):
    assert(udma != NULL)                                    // al_hal_udma_m2m.c:635
    assert(qid < udma->num_of_queues)                       // :636  (num_of_queues @udma+0x25)
    if size == 0 or size > 0x10000:                         // valid range 1..65536
        log("invalid size: %u"); return -1

    txq = al_udma_q_handle_get(udma, qid, /*is_rx=*/0)      // BOUNDARY: al_hal_udma.c  (TX = base + 0x80 + 192*qid)
    rxq = al_udma_q_handle_get(udma, qid, /*is_rx=*/1)      //                          (RX = base + 0xC80 + 192*qid)
    if al_udma_available_get(txq) < 1:                      // (q[+0x34] - 16 - next_idx) & size_mask
        log("not enough room in TX queue %d, requested: %u, available: %u"); return -ENOMEM   // -12
    if al_udma_available_get(rxq) < 1:
        log("not enough room in RX queue %d, ..."); return -ENOMEM

    tx_slot = al_udma_desc_get(txq); tx_rid = al_udma_ring_id_get(txq)   // inlined: advance next_desc_idx, phase
    rx_slot = al_udma_desc_get(rxq); rx_rid = al_udma_ring_id_get(rxq)
    return al_udma_m2m_build_copy_descriptor(rx_slot, tx_slot, rx_rid, tx_rid, s_addr, d_addr, size)  // BOUNDARY

// al_udma_m2m_copy_start @0x45ed60 — doorbell: RX first, then TX (matches kernel :440-449)
function copy_start(udma, qid, m2s_count, s2m_count):
    txq = al_udma_q_handle_get(udma, qid, 0)
    rxq = al_udma_q_handle_get(udma, qid, 1)
    al_udma_desc_action_add(rxq, s2m_count)                 // BOUNDARY: RX doorbell ALWAYS (drtp_inc += s2m_count)
    al_udma_desc_action_add(txq, m2s_count)                 //           TX doorbell
    return 0
```

`crc_prepare_one` (`@0x45dc50`) is the same shape but reserves **two** TX descriptors plus one RX for a CRC op (it routes through `al_udma_m2m_build_crc_descriptor`); the room/assert logic is identical. The bulk operations — `reserve_descriptors` (`@0x45e4b0`, claim N contiguous TX+RX slots, returning the pre-reservation indices and advancing `next_desc_idx` with a phase flip on wrap) and `set_descriptors` (`@0x45e7e0`, write N prebuilt 16-byte descriptors into the rings, OR-ing the ring-id phase `(rid<<24) & 0x3000000` into each, with split `memcpy`s for ring wraparound) — are the vring packer's batch path. `rearm_queue` (`@0x45ef20`) and `reassign_queue` (`@0x45ef50`) are real stubs: each logs *"… is not implemented"* and returns `0xFFFFFFFF` (−1).

> **QUIRK — the doorbell order is RX then TX, identical to the kernel.** `copy_start` rings the RX (destination-write) ring's doorbell before the TX (source-read) ring's — the same RX-then-TX order the kernel `udma_m2m_copy_start` uses (`udma_m2m.c:440–449`), and for the same reason: the destination ring must be armed before the engine is told to start reading the source. A reimplementer who rings TX first can launch a source read before its destination slot is published. (`al_udma_desc_action_add` itself — the `mb()` + `drtp_inc` write — is a boundary into the `al_hal_udma_main.c` core.)

### The userspace `al_udma_m2m` build is byte-identical to the kernel

> **NOTE — do not re-derive the wire format.** The 16-byte descriptor these builders emit is the **same wire format** the kernel `udma/udma_m2m.c` emits, reconciled field-by-field and proven bit-for-bit identical for the copy pair. The bit layout (`len_ctrl` length / 2-bit phase / FIRST·LAST·CONCAT·DMB·SOW·INT·META, the `meta_ctrl` default `0x01080003`, the write-barrier at bit 26, the `length == 0 ⇒ 65536` overload) is **owned by [the 16-byte UDMA descriptor](../dma/descriptor-format.md)** and the userspace↔kernel agreement table is its §8. What this page owns is the userspace *build side*: the packet loop (§1), the single-shot prep, the bulk reserve/set, and the RX-then-TX launch. The userspace HAL exercises a **superset** of what the kernel writes — `CONCAT` (bit 31), `META` (bit 23), the CME meta-descriptor, and the notification mask are userspace-only features with no kernel macro on the M2M path; the hardware honors the same bits regardless of which code line set them, and the kernel simply never sets them.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `al_udma_m2m_set_dmb` | `0x45ced0` | set write-barrier via `al_sdma_m2s_set_write_barrier` (meta bit26) | HIGH |
| `al_udma_m2m_set_first`/`_last`/`_concat` | `0x45cf00`/`0x45cf10`/`0x45cf20` | OR FIRST(26)/LAST(27)/CONCAT(31) into word0 | CERTAIN |
| `al_udma_m2m_clear_first`/`_last`/`_concat` | `0x45cf30`/`0x45cf40`/`0x45cf50` | AND-NOT the same bits | CERTAIN |
| `al_udma_m2m_get_tx_desc_size` | `0x45cf60` | decode length (`0 ⇒ 65536`) | CERTAIN |
| `al_udma_m2m_build_packet` | `0x45cf70` | core N-descriptor loop; per-arch caps, notif/SOW gates | HIGH |
| `al_udma_m2m_build_cme_packet` | `0x45d400` | prepend optional CME meta-desc, then `build_packet` | HIGH |
| `al_udma_m2m_build_copy_packet` | `0x45d550` | plain-copy wrapper (`cme=0`) | HIGH |
| `al_udma_m2m_copy_prepare_one` | `0x45d580` | resolve TX+RX, check room, build one pair | HIGH |
| `al_udma_m2m_crc_prepare_one` | `0x45dc50` | 2 TX + 1 RX descriptors for a CRC op | HIGH |
| `al_udma_m2m_reserve_descriptors` | `0x45e4b0` | claim N contiguous TX+RX slots; phase flip on wrap | HIGH |
| `al_udma_m2m_set_descriptors` | `0x45e7e0` | write N prebuilt 16B descriptors; split `memcpy` on wrap | HIGH |
| `al_udma_m2m_copy_start` | `0x45ed60` | doorbell **RX then TX** | HIGH |
| `al_udma_m2m_rearm_queue`/`_reassign_queue` | `0x45ef20`/`0x45ef50` | **stubs** — log + return −1 | HIGH |
| `al_udma_m2m_build_{tx,rx,copy,crc}_descriptor` | `0x45cac0`/`0x45cbb0`/`0x45cca0`/`0x45cda0` | the 16B field packers — **boundary**, [descriptor-format](../dma/descriptor-format.md) | HIGH |

---

## 2. Queue, Completion, Target-ID and TTT Configuration

### Purpose

`al_hal_udma_config.c` (`.text 0x475070..0x476e32`) and `al_hal_udma_main.c` (`0x45fae0..0x46048e`) are the userspace engine/queue bring-up and tuning layer — the runtime-side analog of the kernel's `udma_main.c`/`udma_config.c`. Every function is a leaf register-programming routine operating on one `al_udma*` handle and hitting MMIO through `al_reg_read32`/`al_reg_write32` (the [hal-adapter](hal-adapter.md) platform-services primitives). The work splits into M2S/S2M queue scheduling and completion config, target-ID (MSI-X steering) config, the rev-4-only Target Translation Table, and the engine `init`/`q_enable`/`revision` core. Pervasively, every advanced/tgtid/TTT/IOFIC path **rev-gates** on `udma->rev_id @+0x1880` against `AL_UDMA_REV_ID_4 == 4`.

The `al_udma` handle is opaque in the `-g`-less TUs; the offsets below are `objdump`-pinned from the bodies and cross-checked against the kernel `struct udma` reg model:

| Field | Offset | Role | Evidence |
|---|---|---|---|
| `name[24]` | `+0x00` | engine name (`strncpy 0x18`, default `"NONE"`) | `al_udma_init` |
| `num_of_queues` | `+0x25` | u8, ≤16; loop/assert bound | `4756d0`/`475c30` |
| q_regs base | `+0x08` | per-queue MMIO block (M2S/S2M q blocks) | `475070` `mov 0x8(%rdi)` |
| m2s common | `+0x30` | axi/m2s/m2s-comp regs | `475190` `mov 0x30(%rdi)` |
| s2m common | `+0x38` | s2m-comp regs | `4752f0` `mov 0x38(%rdi)` |
| gen_regs | `+0x40` | tgtid steering (`gen_regs_get`, NULL-asserted) | `4756d0` `mov 0x40(%rdi)` |
| gen_int_regs | `+0x50` | IOFIC main (`+0x2000` secondary) | `476120` `mov 0x50(%rbp)` |
| tgtid/TTT regs | `+0x58` | ext-app + TTT (`+0x800` TTT_CTRL) | `475d90` `mov 0x58(%rbx)` |
| rev_id | `+0x1880` | `AL_UDMA_REV_ID_4 == 4`; rev gate | `cmpl $0x3/$0x4,0x1880` |

### Algorithm — the engine bring-up core

`al_udma_init` (`@0x45fd50`, the 1854-byte per-engine bring-up) is the userspace `udma_init`. It gates on `rev_id == 4`, sets up the per-queue arrays, programs clk-div/AXI config per arch, and wires the IOFIC:

```c
// al_udma_init @0x45fd50 — al_hal_udma_main.c — per-engine UDMA bring-up
function al_udma_init(udma, params):
    assert(udma != NULL); assert(params != NULL)            // :602/:603
    udma->rev_id = al_udma_revision_get(params->regs_base)  // 0x45fbe0 — read UDMA rev reg
    if udma->rev_id != 4:                                    // UDMA_REV_ID_4-only
        log("unsupported DMA rev id: %d"); return -EINVAL    // -22

    num_q = (params->num_of_queues == 0xFF) ? 16 : params->num_of_queues
    if num_q > 16: log("invalid num_of_queues parameter: %d, max: %d"); return -EINVAL
    udma->num_of_queues = num_q

    // reg bases: m2s = regs_base; s2m = regs_base + 0x20000; gen = +0x38000; pkt/clk = +0x3a300
    for q in 0 .. 15:                                        // init M2S q-struct (+0x80) + S2M q-struct (+0xC00)
        setup_queue(q); al_udma_q_enable(q_handle, /*enable=*/1)    // 0x45fb40 (x2 per queue)

    // arch/rev-specific clk-div + AXI config writes (al_reg_write32):
    //   CAYMAN: [m2s+0x310]=2296 ; MARIANA: [m2s+0x310]=4088 ; else: 0x400/0x100
    //   common: [m2s+pkt]=0xF4240 (1e6 clk), packet_size_cfg_set, [s2m+0x38c]=0

    al_udma_iofic_m2s_error_ints_unmask(udma)               // 0x476650  (§4)
    al_udma_iofic_s2m_error_ints_unmask(udma)               // 0x476a00  (§4)
    if params->iofic_mode:
        rc = al_udma_iofic_config_ex(udma, cfg)             // 0x476120  (§4)
        if rc: log("udma[%s] failed configure iofic"); return rc
    log("udma [%s] initialized. base m2s: %p, s2m: %p"); return 0

// al_udma_q_enable @0x45fb40 — enable/disable one queue's prefetch+scheduling
function al_udma_q_enable(q, enable):
    cfg = q->cfg_shadow(@+0x8c)
    if enable: cfg |= 0x30000   // EN_PREF | EN_SCHEDULING ; q->state(@+0x70) = 2
    else:      cfg &= ~0x30000  //                          ; q->state = 1
    al_reg_write32(q->q_regs(@+0x8) + 0x20, cfg)            // rings.cfg @+0x20
    q->cfg_shadow = cfg
```

### Algorithm — completion, target-ID and TTT config

The config layer is a band of leaf RMW routines, each a `al_reg_read32` / modify / `al_reg_write32` on one CSR field. The representative shapes:

```c
// al_udma_s2m_no_desc_cfg_set @0x4752f0 — "no descriptor" policy on S2M common +0x344
function s2m_no_desc_cfg_set(udma, drop_en, mask, timeout):
    r = al_reg_read32(udma->s2m_common(@+0x38) + 0x344)
    if drop_en == 0 and timeout == 0:
        log("setting timeout to 0 will cause the udma to wait forever instead of dropping the packet")
    r = (r & ~0x18000000) | (drop_en ? 0x08000000 : 0)     // bit27 drop-en, bit28 mask
            | (timeout & 0x00FFFFFF)                        // [23:0] timeout
    al_reg_write32(udma->s2m_common + 0x344, r)

// al_udma_gen_tgtid_conf_queue_set_adv @0x4756d0 — per-queue MSI-X target-ID steering, rev-gated
function gen_tgtid_conf_queue_set_adv(udma, qid, cfg):
    gen = al_udma_gen_regs_get(udma)                        // udma->gen_regs(@+0x40), NULL-asserted
    if udma->rev_id > 3:                                    // v4 (MARIANA)
        assert(qid < DMA_MAX_Q_V4 /*16*/)                  // al_hal_udma_config.c:1270
        // write 16*(qid+608) block: sel/val flags + tgtid words
    else:                                                   // v3 (CAYMAN)
        assert(qid < udma->num_of_queues)                  // :1198
        // pack sel/MSI-X bits into gen+0x23EC per-qid shifts + per-qid 16-bit tgtid words

// al_udma_ttt_en @0x475d90 — Target Translation Table enable, REV_ID_4 ONLY
function ttt_en(udma, enable):
    assert(udma->rev_id >= 4)                               // :1359 (cmpl $0x3,0x1880)
    rmw_bit0(udma->tgtid_regs(@+0x58) + 0x800, enable)     // TTT_CTRL bit0
```

The Target Translation Table (`ttt_en @0x475d90`, `ttt_entry_set @0x475e70`, `ttt_default_config @0x476040`) is **MARIANA-only**: `ttt_entry_set` writes a 7-dword entry at `tgtid_regs+0xC00` (`idx<<5 | mask`, key, `valid | flags<<1`, …) followed by two `al_data_memory_barrier` fences after the trigger words; `ttt_default_config` clears/inverts the `+0x820` ctrl, spins on the `+0x824` ready bit, calls `ttt_en(1)`, then writes a TTT entry for every queue. It is reached only from `al_udma_state_set_wait` on the NORMAL transition.

> **NOTE — most config functions have no static caller in this build.** Of the 24 config-TU functions, only a handful (`iofic_config_ex`, `iofic_m2s/s2m_error_ints_unmask` from `al_udma_init`; `ttt_default_config` from `state_set_wait`; `s2m_q_comp_get` from `perf_params_print`; `iofic_get_ext_app_bit` from `unmask_ext_app`) have a recovered static caller. The other ~17 (the M2S/S2M completion-coalescing, header-split, burst-config, and per-queue tgtid setters) have **no in-binary call edge** — they are vendored-library completeness reached via indirect/op-table paths, or dead on this build. Confidence they are not statically referenced: **HIGH**; that they are indirectly reachable: not provable from this surface.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `al_udma_init` | `0x45fd50` | per-engine bring-up; `rev==4` gate; queue loop; IOFIC wire | HIGH |
| `al_udma_q_enable` | `0x45fb40` | enable/disable queue prefetch+sched (cfg `\|0x30000`) | HIGH |
| `al_udma_revision_get` | `0x45fbe0` | read UDMA rev reg (`base+0x604`) | HIGH |
| `al_udma_num_queues_get` | `0x45fcf0` | return `num_of_queues @+0x25` | HIGH |
| `al_udma_m2s_comp_timeouts_set` | `0x475190` | M2S completion-timeout cfg (arb mode + coal) | HIGH |
| `al_udma_s2m_no_desc_cfg_set` | `0x4752f0` | S2M no-descriptor drop/mask/timeout policy | HIGH |
| `al_udma_s2m_q_comp_set`/`_get` | `0x475570`/`0x475650` | composite S2M completion cfg / read-back | HIGH |
| `al_udma_gen_tgtid_conf_queue_set_adv` | `0x4756d0` | per-queue MSI-X target-ID steering (v3/v4) | HIGH |
| `al_udma_gen_tgtid_conf_set_adv` | `0x475c30` | loop tgtid set over all queues | HIGH |
| `al_udma_gen_tgtid_msix_conf_set_adv` | `0x475c70` | global MSI-X tgtid mode | HIGH |
| `al_udma_ttt_en`/`_entry_set`/`_default_config` | `0x475d90`/`0x475e70`/`0x476040` | Target Translation Table — **MARIANA-only** | HIGH |
| `al_udma_m2s_prefetch_threshold_set` | `0x45f0d0` | RMW byte[15:8] of `m2s+0x308` (max 0x80) | HIGH |
| `al_udma_s2m_prefetch_threshold_set` | `0x45f140` | RMW byte[15:8] of `m2s+0x20308` | HIGH |

---

## 3. The Register-Offset Trampolines

### Purpose

This is the structural element the userspace HAL carries that the kernel driver has no analog for. The kernel `.ko` compiles **one** register layout (`struct udma_gen_regs_v4`); `libnrt.so` supports three generations (SUNDA/CAYMAN/MARIANA) from one binary, so every per-queue register *offset* is resolved at runtime through the global `kaena_khal` dispatch object. The 18 `aws_hal_udma_get_*_offset` functions of `al_hal_udma_m2m.c` (`.text 0x45f1b0..0x45fad6`) are all the identical arch-dispatch trampoline: assert the arch is latched, assert the `kaena_khal.khal_udma.<member>` slot is non-NULL, tail-call it. The trampoline owns the *indirection*; the per-arch *offset value* lives in the registered `*_sunda`/`*_cayman`/`*_mariana` leaf (a boundary into the per-arch device layer).

This is the same `kaena_khal`-mediated arch-gate-then-dispatch pattern the register accessors of [hal-registers](hal-registers.md) use, applied to the UDMA queue register map. The dispatch object and the arch gate are shared; only the sub-table (`khal_udma` rather than `khal_arch`) differs.

### Entry Point

```text
<tdrv offset-aggregator: get_dma_queue_base_low_offset / _head_offset / _tail_inc_offset / ...>
  └─ aws_hal_udma_get_<reg>_offset(qid?)              ── shape-A trampoline (this §)
       └─ al_hal_tpb_get_arch_type (0x44bca0)          ── assert arch != INVALID
       └─ assert kaena_khal.khal_udma.<member> != NULL
       └─ jmp *kaena_khal.khal_udma.<member>           ── tail-call the installed *_sunda/_cayman/_mariana leaf
```

### Algorithm

The canonical trampoline, byte-identical across all 18 (the only per-fn variables are the slot member, the assert string, and the optional `qid` arg). Verbatim tail-call shape from `objdump` (`0x45f300`): `lea 0x84f86c(%rip),%rax  # caeb80 <kaena_khal>` then `jmp *%rdx` with `%rdx` loaded from the slot:

```c
// CANONICAL khal_udma OFFSET RESOLVER — modelled on aws_hal_udma_get_m2s_queue_offset @0x45f370
// slot = kaena_khal.khal_udma.get_m2s_queue_offset (0xCAF118, i.e. kaena_khal +0x598)
function aws_hal_udma_get_m2s_queue_offset(qid):                  // 0x45f370, al_hal_udma_m2m.c
    if al_hal_tpb_get_arch_type() == AL_HAL_TPB_ARCH_TYPE_INVALID:   // gate §1 of hal-registers
        __assert_fail("al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID", ...)   // .rodata 0x81c890
    fp = kaena_khal.khal_udma.get_m2s_queue_offset               // 0xCAF118  (load fn-ptr from .bss slot)
    if fp == NULL:
        __assert_fail("kaena_khal.khal_udma.get_m2s_queue_offset", ...)   // FATAL, uniform policy
    return fp(qid)                                               // jmp *fp — tail-call per-arch leaf
```

Slot population is by the registrars `kaena_khal_register_funcs_v{2,3,4}` (the same set that fills `khal_arch`). The Mariana registrar `_v4 @0x4622e0` is `objdump`-proven to store the `*_mariana` leaf pointers into the `khal_udma` slots: `462d61: mov %rdx,0x590(%rax)` after `lea 0x468520` (`get_s2m_abort_cause_offset_mariana`), `462d6f: mov %rdx,0x598(%rax)` after `lea 0x468540`, `462d7d: mov %rdx,0x5a0(%rax)` after `lea 0x468550`. The slots are NULL until `kaena_khal_init`, which is why each trampoline asserts the slot non-NULL.

### Function Map — the 18 offset trampolines → `khal_udma` slot

Each trampoline, its `kaena_khal.khal_udma` slot (absolute VMA and offset from `kaena_khal @0xCAEB80`), and the register it resolves. The per-arch return value belongs to the per-arch device layer; here, each is pinned to its slot and the verbatim assert string that names it.

| Trampoline (symbol) | Addr | Slot VMA | Slot off | Resolves | Conf |
|---|---|---|---|---|---|
| `aws_hal_udma_get_m2s_offset` | `0x45f1b0` | `0xCAF0F8` | `+0x578` | M2S unit-region base offset | HIGH |
| `aws_hal_udma_get_s2m_offset` | `0x45f220` | `0xCAF100` | `+0x580` | S2M unit-region base offset | HIGH |
| `aws_hal_udma_get_m2s_abort_cause_offset` | `0x45f290` | `0xCAF108` | `+0x588` | M2S abort-cause reg offset | HIGH |
| `aws_hal_udma_get_s2m_abort_cause_offset` | `0x45f300` | `0xCAF110` | `+0x590` | S2M abort-cause reg offset | HIGH |
| `aws_hal_udma_get_m2s_queue_offset` | `0x45f370` | `0xCAF118` | `+0x598` | per-queue M2S(TX) reg-block base (arg `qid`) | HIGH |
| `aws_hal_udma_get_s2m_queue_offset` | `0x45f3e0` | `0xCAF120` | `+0x5A0` | per-queue S2M(RX) reg-block base (arg `qid`) | HIGH |
| `aws_hal_udma_get_m2s_queue_base_ptr_lo_offset` | `0x45f450` | `0xCAF128` | `+0x5A8` | M2S desc-ring base-ptr LOW | HIGH |
| `aws_hal_udma_get_s2m_queue_base_ptr_lo_offset` | `0x45f4c0` | `0xCAF130` | `+0x5B0` | S2M desc-ring base-ptr LOW | HIGH |
| `aws_hal_udma_get_m2s_queue_base_ptr_hi_offset` | `0x45f530` | `0xCAF138` | `+0x5B8` | M2S desc-ring base-ptr HIGH | HIGH |
| `aws_hal_udma_get_s2m_queue_base_ptr_hi_offset` | `0x45f5a0` | `0xCAF140` | `+0x5C0` | S2M desc-ring base-ptr HIGH | HIGH |
| `aws_hal_udma_get_m2s_queue_len_offset` | `0x45f610` | `0xCAF148` | `+0x5C8` | M2S desc-ring length | HIGH |
| `aws_hal_udma_get_s2m_queue_len_offset` | `0x45f680` | `0xCAF150` | `+0x5D0` | S2M desc-ring length | HIGH |
| `aws_hal_udma_get_m2s_queue_head_ptr_offset` | `0x45f6f0` | `0xCAF158` | `+0x5D8` | M2S ring head-ptr | HIGH |
| `aws_hal_udma_get_s2m_queue_head_ptr_offset` | `0x45f760` | `0xCAF160` | `+0x5E0` | S2M ring head-ptr | HIGH |
| `aws_hal_udma_get_m2s_queue_tail_ptr_offset` | `0x45f7d0` | `0xCAF168` | `+0x5E8` | M2S ring tail-ptr | HIGH |
| `aws_hal_udma_get_s2m_queue_tail_ptr_offset` | `0x45f840` | `0xCAF170` | `+0x5F0` | S2M ring tail-ptr | HIGH |
| `aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset` | `0x45f8b0` | `0xCAF178` | `+0x5F8` | M2S tail-ptr-inc (**doorbell**) | HIGH |
| `aws_hal_udma_get_s2m_queue_tail_ptr_inc_offset` | `0x45f920` | `0xCAF180` | `+0x600` | S2M tail-ptr-inc (**doorbell**) | HIGH |
| `aws_hal_udma_get_m2s_queue_data_tail_ptr_inc_offset` | `0x45f990` | `0xCAF188` | `+0x608` | M2S **data** tail-ptr-inc (M2S only) | HIGH |
| `aws_hal_udma_get_m2s_queue_sw_ctrl_offset` | `0x45fa00` | `0xCAF190` | `+0x610` | M2S queue SW-control (reset) | HIGH |
| `aws_hal_udma_get_s2m_queue_sw_ctrl_offset` | `0x45fa70` | `0xCAF198` | `+0x618` | S2M queue SW-control (reset) | HIGH |

> **QUIRK — the trampoline set is not symmetric pairs.** Of the 18 queue-register trampolines, 17 form M2S/S2M pairs — except `get_m2s_queue_data_tail_ptr_inc_offset` (`@0x45f990`, slot `+0x608`), which has **no S2M sibling**: the S2M (RX) ring has no separate *data* tail pointer, only the descriptor tail. A reimplementer who assumes a perfect M2S/S2M mirror and synthesizes an `s2m_data_tail_ptr_inc` slot will index a `khal_udma` member that does not exist (slot `+0x610` is the M2S SW-control, not an S2M data-tail). The asymmetry is a hardware fact, not a missing function.

> **NOTE — the slot offset is `slot_vma − 0xCAEB80`, and the assert string is the ground truth.** Each trampoline's `khal_udma` member is identified two ways that agree: the indirect-call site loads from a fixed `.bss` address (`kaena_khal + off`), and the per-fn `__assert_fail` literal names the member verbatim (`"kaena_khal.khal_udma.get_m2s_queue_offset"`, `.rodata @0x825xxx`). The slot offsets `+0x578..+0x618` are contiguous 8-byte function-pointer slots; the three at `+0x578/+0x580/+0x588` (m2s/s2m/m2s-abort-cause base offsets) precede the 18 queue-register slots in the same `khal_udma` sub-block.

---

## 4. The Userspace IOFIC Twin

### Purpose

`al_hal_iofic.c` (21 functions, `.text 0x47c060..0x47cdc5`) and `al_hal_udma_iofic.c` (3 functions, `0x476e40..0x4770d0`) are the userspace I/O-Fabric Interrupt Controller HAL — the runtime-side twin of the kernel `udma/udma_iofic.c`. Each `al_iofic_*` function is a thin defensive wrapper: NULL/range asserts → `al_hal_log` → `al_abort_program(-1)`, then one or two MMIO reg ops; no locking, no allocation, pure leaf MMIO. The userspace HAL programs the **same** two-level IOFIC the kernel does — a primary level with four group summaries A–D (D the secondary funnel) over a secondary level where the individual UDMA error/abort causes live. This page owns the userspace *register-poke mechanism*; the **cause model** (the two-level funnel geometry, the group catalog, the abort/completion policy) is owned by [kernel/udma-iofic](../kernel/udma-iofic.md) and not re-derived here.

### The per-group register window

`al_iofic_*` accesses one IOFIC *group's* `0x40`-byte control block, reached by casting the level base to `union iofic_regs *` and indexing `ctrl[group]` (stride `group << 6`). The four field offsets the HAL touches — `objdump`-byte-verified, and **identical** to the kernel's `iofic_grp_ctrl` layout:

| Field | Offset | Reg role | Userspace writer | Conf |
|---|---|---|---|---|
| `int_cause` | `+0x00` | Interrupt Cause (RW1C) | `al_iofic_read_cause @0x47c970` / `_clear_cause @0x47cb70` (W1C-complement) / `_poll_specific_cause @0x47cae0` | HIGH |
| `int_mask` | `+0x10` | Interrupt Mask (1=masked) | `al_iofic_mask @0x47c810` (RMW-OR) / `_read_mask` / `_write_mask` | HIGH |
| `int_mask_clear` | `+0x18` | Mask Clear (write `~bits` ⇒ unmask) | `al_iofic_unmask @0x47c7a0` / `_unmask_offset_get @0x47c720` | HIGH |
| `int_control` | `+0x28` | Control (posedge bit3 / MSI-X bit5 / auto-clear bit0) | `al_iofic_config @0x47c350` / `_control_flags_get` / `_set_on_posedge` | HIGH |

### Algorithm — the register pokes and the policy composition

```c
// al_iofic_config @0x47c350 — set HOW a group's causes latch/deliver (still masked after)
function al_iofic_config(regs_base, group, flags):
    regs = (union iofic_regs *) regs_base
    al_reg_write32(&regs->ctrl[group].int_control(@+0x28), flags)   // SET_ON_POSEDGE(bit3)|MASK_MSI_X(bit5) = 0x28

// al_iofic_unmask @0x47c7a0 — ARM causes for delivery (atomic vs HW auto-mask)
function al_iofic_unmask(regs_base, group, bits):
    regs = (union iofic_regs *) regs_base
    al_reg_write32(&regs->ctrl[group].int_mask_clear(@+0x18), ~bits)   // ~bits: SET bit => UNMASK

// al_iofic_clear_cause @0x47cb70 — W1C-complement clear
function al_iofic_clear_cause(regs_base, group, bits):
    al_reg_write32(&regs->ctrl[group].int_cause(@+0x00), ~bits)        // write-0-to-clear via inverted

// al_udma_iofic_error_ints_unmask_one @0x4770d0 — minimal per-bit error enable on group 0
function al_udma_iofic_error_ints_unmask_one(iofic_ctrl, bit):
    al_iofic_config(iofic_ctrl, 0, POSEDGE | MASK_MSI_X)              // 0x47c350
    al_iofic_abort_mask_clear(iofic_ctrl, 0, bit)                     // BOUNDARY: 0x47cfa0 (other cell)
    al_iofic_unmask(iofic_ctrl, 0, bit)                              // 0x47c7a0
    // called 6x with bit=0xFFFFFFFF by al_udma_m2m_set_axi_error_abort (the gen AXI sub-IOFICs)
```

The policy composition `al_udma_iofic_m2s_error_ints_unmask` (`@0x476650`) / `_s2m_error_ints_unmask` (`@0x476a00`) — called from `al_udma_init` — composes these pokes into the bring-up sequence: `al_iofic_config(grp, POSEDGE|MASK_MSI_X)` on the secondary group, then `al_iofic_abort_mask_clear`, then `al_iofic_unmask`, then lift the primary-D funnel. The M2S path uses group 0 and the v3/v4 error masks (`grp3` final unmask `256`(v3)/`1280`(v4)); the S2M twin uses group 1 with mask `0x7FFFE007`(v3)/`0xFFFFEF07`(v4). `al_iofic_handle_init` (`@0x47c060`) validates `group_num <= AL_IOFIC_MAX_GROUPS` and derives the rev from `(reg[+0x28] >> 28) & 3`.

> **GOTCHA — the `~bits` write inversion is identical to the kernel, and reimplementers invert the controller if they miss it.** `al_iofic_unmask` writes the **complement** of its `bits` argument (`~bits`) to the *mask-clear* register (`+0x18`), not the mask register (`+0x10`): a set bit in `bits` means "unmask this cause". `al_iofic_clear_cause` likewise writes `~bits` to `int_cause` (write-0-to-clear). The call-site convention is uniform — *set bit in `bits` ⇒ enable the behavior* — but the register-level semantics are inverted. This is the **same** inversion the kernel twin carries ([kernel/udma-iofic §2](../kernel/udma-iofic.md#2-the-register-block-layout)); a reimplementer who writes `bits` directly to `int_mask_clear` unmasks exactly the causes the caller meant to leave masked.

> **NOTE — the secondary-level summary bit table is rev-keyed, decoded from `.rodata`.** `al_udma_iofic_sec_level_int_get` (`@0x476e40`) selects a per-group secondary-summary bit by `udma->rev_id @+0x1880`: the table at `.rodata 0x855060` (rev≤3) is `{0x100, 0x200, 0x000, 0x000}` (only groups A/B summarize), and at `0x855070` (rev>3) is `{0x100, 0x200, 0x400, 0x800}` (group C added). This is the userspace view of the kernel's primary-D funnel bits (`INT_GROUP_D_M2S`=bit8, `_S2M`=bit9, `_2ND_IOFIC_GROUP_C`=bit10) — group C summarizes only on rev≥4, the only shipped silicon. The full two-level cause model is owned by [kernel/udma-iofic §1/§3](../kernel/udma-iofic.md#1-the-two-level-topology).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `al_udma_iofic_sec_level_int_get` | `0x476e40` | per-group secondary-summary bit (rev-keyed `.rodata` table) | HIGH |
| `al_udma_iofic_unmask_ext_app` | `0x476f70` | unmask ext-app cause on primary group 3 (`gen_int_regs @+0x50`) | HIGH |
| `al_udma_iofic_error_ints_unmask_one` | `0x4770d0` | minimal `config→abort_clear→unmask` on group 0 (6× AXI sub-IOFICs) | HIGH |
| `al_iofic_config` | `0x47c350` | write `int_control@+0x28 = flags` (posedge/MSI-X) | HIGH |
| `al_iofic_unmask` | `0x47c7a0` | write `~bits` to `int_mask_clear@+0x18` (arm delivery) | HIGH |
| `al_iofic_mask` | `0x47c810` | RMW-OR `int_mask@+0x10 \|= bits` | HIGH |
| `al_iofic_read_cause` | `0x47c970` | read `int_cause@+0x00` | HIGH |
| `al_iofic_clear_cause` | `0x47cb70` | write `~bits` to `int_cause@+0x00` (W1C-complement) | HIGH |
| `al_iofic_handle_init` | `0x47c060` | validate params; store `regs_base@+0`, `group_num@+0xc`, rev@+8 | HIGH |
| `al_iofic_cause_iter_init`/`_next` | `0x47c9e0`/`0x47c190` | snapshot all groups' cause; `tzcnt`-drain iterate | HIGH |
| `al_iofic_clear_cause_before_abort` | `0x47cd30` | read+clear+re-read; log if set before/after; return surviving-abort mask | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `al_udma_m2m_build_{tx,rx,copy,crc}_descriptor` (`0x45cac0`/`0x45cbb0`/`0x45cca0`/`0x45cda0`) | the 16-byte field packers — the wire-format boundary this page builds on, owned by [descriptor-format](../dma/descriptor-format.md) |
| `al_copy_descriptor` (`0x265980` = `memcpy`) / `al_reg_read32`/`al_reg_write32` (`0x2658a0`/`0x265c50`) | the [hal-adapter](hal-adapter.md) platform-services primitives every builder/config/IOFIC routine bottoms into |
| `kaena_khal` (`@.bss 0xCAEB80`) / `kaena_khal_register_funcs_v{2,3,4}` | the dispatch object whose `khal_udma` slots the §3 trampolines tail-call; same registrars that fill `khal_arch` ([hal-registers](hal-registers.md)) |
| `al_hal_tpb_get_arch_type` (`0x44bca0`) | the arch gate every trampoline and `build_packet` consults ([hal-adapter §3](hal-adapter.md)) |
| `al_udma_m2m_copy_start` / `al_udma_desc_action_add` | the RX-then-TX doorbell pair, mirroring the kernel `udma_m2m_copy_start` |
| `al_iofic_abort_mask_clear` (`0x47cfa0`) | the abort-mask poke the `error_ints_unmask` policy calls (sibling `al_hal_iofic.c` cell) |

## Cross-References

- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the wire format these userspace builders emit; owns every `len_ctrl`/`meta_ctrl` bit position, the barrier-bit table, the `len0` overload, and the **userspace↔kernel byte-agreement proof** (§8) — the boundary this page builds on, do **not** re-derive
- [UDMA Memory-to-Memory Builder](../kernel/udma-m2m.md) — the **kernel twin** of §1; the paired-ring model, the field-population rule, and the RX-then-TX doorbell — byte-identical to the userspace build documented here
- [UDMA IOFIC Interrupt Controller](../kernel/udma-iofic.md) — the **kernel twin** of §4; owns the two-level funnel geometry, the per-group cause catalog, and the polled-completion model (the reason the completion groups are dark); this page mirrors its register pokes
- [KaenaHal: Register and Reg-Offset Accessors](hal-registers.md) — the sibling `aws_hal_get_*`/`aws_reg_*` accessors through the *same* `kaena_khal` object (`khal_arch` sub-table); the §3 trampolines here are the `khal_udma` analog of that page's shape-A resolvers
- [KaenaHal: Overview and Platform-Services Adapter](hal-adapter.md) — the `al_reg_*` MMIO and `al_copy_descriptor` primitives every routine on this page bottoms into, and the `al_hal_tpb_get/set_arch_type` arch latch the §3 trampolines gate on
- [back to index](../index.md)
