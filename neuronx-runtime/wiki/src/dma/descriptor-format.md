# The 16-Byte UDMA Descriptor

> *Userspace addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba…`, KaenaHal-2.31.0.0 `al_hal_udma_m2m.c`, statically linked; `.text` VMA == file offset, so every `0x45…` is an analysis VMA). Kernel citations are `file:line` into `aws-neuronx-dkms 2.27.4.0` (GPL-2.0), `udma/udma_m2m.c` + `udma/udma.h`.*
> *Evidence grade: **Confirmed (byte-anchored)** — the wire format is authored by two independent code lines (userspace KaenaHal + kernel DKMS) and reconciled bit-for-bit (§8). Other versions will differ. · Part VIII — DMA & Descriptor Engine · [back to index](../index.md)*

## Abstract

Every memory-to-memory copy a NeuronCore performs is submitted to the AnnapurnaLabs/Alpine **UDMA** engine as a fixed **16-byte hardware descriptor** — `union udma_desc`. This is the lowest-level wire format in the whole runtime: the kernel builds it (`udma/udma_m2m.c`), the userspace KaenaHal builds it (`al_hal_udma_m2m.c` in `libnrt.so`), the collective `encd` driver and the `vring` packer emit arrays of it, and the silicon consumes it. If you reimplement one structure from this book to byte accuracy, it is this one.

The descriptor is best understood by analogy to any DMA gather/scatter ring entry — a `{length, control-flags, address}` triple — with two Alpine-specific twists. First, a copy is **never a single descriptor**: it is always a *pair* — one **m2s / Tx** descriptor that reads the **source**, placed on the engine's TX ring, and one **s2m / Rx** descriptor that writes the **destination**, placed on the RX ring of the same queue. Second, the 16-bit length field cannot express a full 64 KiB transfer, so `length == 0` is overloaded to mean 65536 bytes (the "len0 trick"). The `meta_ctrl` word (present only on the Tx view) carries the SDMA copy-engine operation selector, the CRC/endian controls, and the write-barrier — and it is the source of the single most error-prone bit position in the format.

This page documents four artifacts a reimplementer must reproduce: (1) the **`union udma_desc` 16-byte layout** and its three views (tx / rx / tx_meta); (2) the **`len_ctrl` word0 bit-field** — length, ring-id/phase, and the FIRST/LAST/CONCAT/DMB/SOW/INT/META control bits, identical on the Tx and Rx packs; (3) the **`meta_ctrl` overlays** — the SDMA default `0x01080003`, the write-barrier bit, and the CME/CRC meta-descriptor word; and (4) the **per-arch deltas**, which are *policy and capability gating, not format changes* — the wire layout is arch-invariant across v2/v3/v4.

For reimplementation, the contract is:

- **The 16-byte `{len_ctrl, meta_ctrl/—, buf_ptr}` layout** and the rule that a copy emits a paired Tx (source) + Rx (destination) descriptor on two rings of one queue.
- **The `len_ctrl` bit-field** — exact bit positions for length, 2-bit ring-id/phase, and every control flag — and the `length == 0 ⇒ 65536` overload.
- **The `meta_ctrl` model** — the default value, the write-barrier at bit 26 (*not* bit 28), and that the four ordering modes (NONE / DMB / WRITE_BARRIER / SOW) live in three different bit locations.
- **The per-arch gating** (SOW is V3+, notification-mask is V4-only, max-descs-per-packet is per-arch) — none of which alters a single bit position.

| | |
|---|---|
| **Descriptor** | `union udma_desc` — 16 bytes, `__attribute__((packed, aligned(16)))` |
| **Kernel definition** | `udma/udma.h:38–58` (aws-neuronx-dkms 2.27.4.0, GPL-2.0) |
| **Word map** | `len_ctrl` @0x0 · `meta_ctrl` @0x4 (Tx only) · `buf_ptr` @0x8 (64-bit) |
| **Copy unit** | a **pair**: m2s/Tx (reads source) + s2m/Rx (writes destination), one queue |
| **Userspace builders** | `al_udma_m2m_build_{tx,rx,copy,crc}_descriptor` @ `0x45cac0 / 0x45cbb0 / 0x45cca0 / 0x45cda0` |
| **Kernel builders** | `udma_m2m_build_{tx,rx,_}descriptor` @ `udma_m2m.c:261 / 286 / 323` |
| **Default `meta_ctrl`** | `0x01080003` (`is_first=is_last=copy_source_data=1, ssmae_op=0b010`) — byte-identical both sides |
| **Length limit** | `MAX_DMA_DESC_SIZE = 65536` (`udma.h:479`); `length == 0` encodes 65536 |
| **Byte-agreement** | userspace == kernel, bit-for-bit, for the copy pair (verified, §8) |

---

## 1. The Descriptor Model

### Purpose

`union udma_desc` is the single submission-ring entry the UDMA hardware reads. The runtime never issues a copy as one descriptor: it builds a **pair** and rings the two rings' doorbells. The Tx (m2s, "memory-to-stream") descriptor names the **source** read; the Rx (s2m, "stream-to-memory") descriptor names the **destination** write. The header comment in the kernel TU states the convention verbatim (`udma_m2m.c:14–20`): *"m2s … is the same as Tx (and source) and s2m … is the same as Rx (and destination) … always copy between two memory locations by creating paired m2s and s2m descriptors."*

### Entry Point

Two code lines build the identical bytes. The kernel path:

```text
ndma_memcpy_* (neuron_dma.c)                         ── high-level DMA layer
  └─ udma_m2m_copy_prepare_one (udma_m2m.c:365)      ── resolve queues, check space, get phase
       └─ udma_m2m_build_descriptor (:323)           ── the PAIR orchestrator
            ├─ sdma_m2s_set_write_barrier (:96)       ── (WRITE_BARRIER mode only)
            ├─ udma_m2m_build_tx_descriptor (:261)    ── 16B Tx slot (source)
            └─ udma_m2m_build_rx_descriptor (:286)    ── 16B Rx slot (destination)
  └─ udma_m2m_copy_start (:416)                       ── doorbell: RX then TX
```

The userspace path is shape-identical, reached from the executor's submit funnel:

```text
hw_exec_queue_add_exec_request_impl (0x320810) / vring_pack_descs
  └─ al_udma_m2m_build_copy_descriptor (0x45cca0)    ── the PAIR orchestrator
       ├─ al_sdma_m2s_set_write_barrier (0x451670)    ── (WRITE_BARRIER mode only)
       ├─ al_udma_m2m_build_tx_descriptor (0x45cac0)  ── builds Tx on stack, then…
       ├─ al_udma_m2m_build_rx_descriptor (0x45cbb0)  ── builds Rx on stack, then…
       └─ al_copy_descriptor (0x265980 = jmp memcpy)  ── memcpy 16B into the ring slot
  └─ al_udma_m2m_copy_start (0x45ed60)                ── doorbell: RX then TX
```

> **NOTE —** both builders construct the descriptor on the stack and then **`memcpy` 16 bytes** into the ring slot (`al_copy_descriptor` is a tail-jump to `memcpy`; the kernel assigns the `union` by value). A reimplementation that writes fields piecemeal into device-visible ring memory is also correct, but the reference implementation publishes the whole 16-byte word atomically from a scratch copy.

### The paired-descriptor rule

`udma_m2m_build_descriptor` (`:323`) is the orchestrator. It seeds `meta_ctrl` from the default (`:329`), sets `tx_flags = M2S_DESC_FIRST | M2S_DESC_LAST` (a single-descriptor packet, `:331`), applies the barrier mode (§6), builds the Tx then the Rx descriptor, and — if `set_dst_int` — ORs `S2M_DESC_INT_EN` into the Rx flags (`:356–357`). The two descriptors share `size`, `barrier`, and their respective ring phase; they differ only in direction and address.

---

## 2. word0 — `len_ctrl`

### Encoding

`len_ctrl` (offset `0x0`, 32-bit, little-endian) carries the transfer length, the 2-bit ring-id/phase, and the packet-control flags. The Tx (m2s) view:

| bit(s) | mask | field | meaning | source |
|---|---|---|---|---|
| 31 | `0x80000000` | `CONCAT` | concatenate with next descriptor | userspace `set_concat` @`0x45cf20` |
| 30 | `0x40000000` | `DMB` | Data Memory Barrier | `M2S_DESC_DMB` `udma.h:60` |
| 28 | `0x10000000` | `INT_EN` | interrupt on completion | `M2S_DESC_INT_EN` `udma.h:61` |
| 27 | `0x08000000` | `LAST` | last descriptor in packet | `M2S_DESC_LAST` `udma.h:62` |
| 26 | `0x04000000` | `FIRST` | first descriptor in packet | `M2S_DESC_FIRST` `udma.h:63` |
| 25:24 | `0x03000000` | `RING_ID` | ring-id / **phase** (2-bit, 0–3) | `M2S_DESC_RING_ID_SHIFT=24` `udma.h:64` |
| 23 | `0x00800000` | `META` | meta / CME-descriptor marker | userspace `is_meta` @`0x45ce70` |
| 15:0 | `0x0000ffff` | `LENGTH` | byte length; **`0` ⇒ 65536** | `M2S_DESC_LEN_MASK` `udma.h:67` |

Bits 22:16 are unused by M2M. The userspace bit-setters prove every position byte-exactly: `set_first` @`0x45cf00` is `orl $0x04000000,(%rdi)`; `set_last` @`0x45cf10` is `orl $0x08000000,(%rdi)`; `set_concat` @`0x45cf20` is `orl $0x80000000,(%rdi)`; the predicates (`is_meta`/`is_first`/`is_last`/`is_concat`, `0x45ce70`–`0x45cea0`) read `(w0 >> {23,26,27,31}) & 1`.

### The S2M (Rx) view

The Rx descriptor uses the **same word0 layout**, with one direction-specific flag:

| bit | mask | field | meaning | source |
|---|---|---|---|---|
| 29 | `0x20000000` | `STRONG_ORDER_WR` | strong-ordered write — **V3+ only** | `S2M_DESC_STRONG_ORDER_WR` `udma.h:70` |
| 28 | `0x10000000` | `INT_EN` | interrupt on completion | `S2M_DESC_INT_EN` `udma.h:69` |
| 27 / 26 / 25:24 / 15:0 | — | LAST / FIRST / RING_ID / LENGTH | identical to Tx | — |

> **GOTCHA —** the kernel's `udma_m2m_build_rx_descriptor` (`:300–301`) packs the Rx descriptor with the **`M2S_*` macros** (`M2S_DESC_RING_ID_SHIFT`, `M2S_DESC_LEN_MASK`), not the `S2M_*` ones. This is harmless — both families are `shift=24, mask=0xffff` (`udma.h:64–72`) — but a reimplementer cross-reading the source will see "m2s" names on an "s2m" descriptor. It is a naming inconsistency, not a layout difference.

### Algorithm — the word0 pack

Both code lines compute `len_ctrl` the same way. The kernel form:

```c
// udma_m2m_build_tx_descriptor — udma_m2m.c:261 (Rx form at :286 is identical)
function build_tx(tx_slot, ring_id, src_addr, size, meta_ctrl, flags):
    if size > MAX_DMA_DESC_SIZE: return -EINVAL          // 0x10000  (:267)
    if size == MAX_DMA_DESC_SIZE: size = 0               // len0 trick (:271–273)
    len_ctrl  = flags                                    // already FIRST|LAST(|DMB)
    len_ctrl |= ring_id << M2S_DESC_RING_ID_SHIFT        // << 24    (:275)
    len_ctrl |= size & M2S_DESC_LEN_MASK                 // [15:0]   (:276)
    desc = { .len_ctrl = len_ctrl, .meta_ctrl = meta_ctrl, .buf_ptr = src_addr }
    memcpy(tx_slot, &desc, 16)                           // 16-byte publish (:278–279)
```

The userspace builder (`al_udma_m2m_build_tx_descriptor`, the pack at `0x45cb29–0x45cb47`) emits the byte-identical sequence: `shl $0x18,%ebp` (ring-id `<< 24`), `or %r15d,%r9d` (`| flags`), `or %r9d,%ebx` (`| (len | special)`), then stores word0 to the stack descriptor before the 16-byte copy.

### The `length == 0 ⇒ 65536` rule

```text
LENGTH field is 16 bits → max representable = 65535.
MAX_DMA_DESC_SIZE = 65536 (0x10000) does not fit.
⇒ a full-64 KiB transfer is encoded as LENGTH = 0.
```

> **QUIRK —** `length == 0` does **not** mean "empty descriptor" — it means a 64 KiB transfer. The accessor `al_udma_m2m_get_tx_desc_size` @`0x45cf60` makes this explicit: it returns `(u16)len ? (u16)len : 0x10000`. The kernel applies the `64K → 0` conversion in two places (the builders `:271/:296` *and* `udma_m2m_copy_prepare_one:380–382`); the double conversion is idempotent (`0` stays `0`). Transfers larger than 64 KiB are split into a `FIRST … CONCAT … LAST` chain of descriptors by the packing layer ([Virtual Rings](virtual-rings.md)), not expressed in one descriptor.

---

## 3. word1 — `meta_ctrl` (Tx only)

`meta_ctrl` (offset `0x4`) exists only on the Tx view. On the Rx view this slot is `buf2_ptr_lo` and M2M leaves it zero (the descriptor is zero-initialized: kernel `union udma_desc rx_desc = {}` `:289`; userspace clears the stack descriptor with `pxor xmm0 / movaps` @`0x45cbb2`). The same 32 bits have **two C overlays**, both describing the same word.

### Overlay A — `union tdma_m2s_meta_ctrl` (the authoring view)

The default copy/CRC view (`udma_m2m.c:23–50`), seeded into every descriptor:

| bit(s) | field | bit(s) | field |
|---|---|---|---|
| 0 | `is_last_block` | 14 | `validate_crc` |
| 1 | `is_first_block` | 15 | `use_stored_crc_iv` |
| 2 | `block_attr_rsvd` | 16 | `send_crc_result` |
| 5:3 | `cached_crc_iv_index` | 17 | `store_crc_result` |
| 6–13 | 8 × endianness byte/bit-swap controls | 18 | `store_source_crc` |
| | (result / intr-val / src / init) | 19 | `copy_source_data` |
| 22:20 | `op_type` | 25:23 | `ssmae_op` ("SDMA op" selector) |
| 31:26 | `rsvd0` | | |

### The default — `0x01080003`

```text
tdma_m2s_meta_ctrl_default_value  (udma_m2m.c:53–79)
  is_last_block   = 1   (bit 0)
  is_first_block  = 1   (bit 1)
  copy_source_data= 1   (bit 19)
  ssmae_op        = 0b010 (bits 25:23)
  everything else = 0
  ⇒ packed = (1<<0)|(1<<1)|(1<<19)|(2<<23) = 0x01080003
```

This is byte-identical on both sides: the kernel computes it; the userspace stores it in `.rodata` as the literal bytes `03 00 08 01` @`0x9e8828` (symbol `al_tdma_m2s_meta_ctrl_default_value`, mirrored @`0x9ea138`). The TU comment (`:52`) notes the meta word is *"currently unmodifiable, use the same value for all descriptors"* on the plain-copy path.

> **NOTE —** `is_cme_desc` (the "CME descriptor class" predicate, userspace @`0x45ceb0`) is simply `ssmae_op == 0b010` — i.e. the *default* `ssmae_op` value **is** the CME-class tag. It reads the `u16` at `meta_ctrl+2`, shifts right 7, masks 7, and compares to 2.

### Overlay B — `struct sdma_cme_desc_word` and the write-barrier

The second overlay (`udma_m2m.c:81–94`) re-describes the same 32 bits and is used only to flip one bit:

```text
[2:0] block_attr · [5:3] rsvd · [13:6] endian_type · [14] validate_crc ·
[15] rsvd · [16] send_crc · [18:17] rsvd · [19] copy_src_data ·
[22:20] optype · [25:23] op · [26] write_barrier · [31:27] netag0_rsvd
```

> **CORRECTION (DMA-DESC) —** `write_barrier` is **bit 26 of `meta_ctrl`**, not bit 28. An earlier draft placed it at bit 28; that is wrong. The position is double-proven: (i) summing the preceding `__packed` bit-field widths `3+3+8+1+1+1+2+1+3+3 = 26`; (ii) the userspace setter `al_sdma_m2s_set_write_barrier` @`0x451670` is literally `orb $0x4,0x3(%rdi); ret` — byte 3, bit 2 ⇒ meta bit `24+2 = 26`. The sibling `al_sdma_m2s_set_notification_mask` @`0x451680` is `orb $0x10,0x3(%rdi)` ⇒ meta bit `24+4 = 28` (a different control).

> **GOTCHA —** the value `0x04000000` is bit 26 in **both** words but means two different things: in `len_ctrl` it is `FIRST`; in `meta_ctrl` it is `write_barrier`. Likewise `0x10000000` (bit 28) is `INT_EN` in `len_ctrl` but `notification-mask` in `meta_ctrl`. Always tag which word a `0x0?000000` constant belongs to.

### The CME / CRC meta-descriptor word0

CME and CRC packets prepend a *third* descriptor whose `len_ctrl` is built specially (`al_udma_m2m_build_crc_descriptor` @`0x45cdea`):

```text
word0 = (idx << 24) | 0x04800000
      = (CME index << 24) | FIRST(bit26) | META(bit23)
```

i.e. the meta-descriptor is marked `FIRST + META`, and the CME index packs into the `[31:24]` region (overlapping the ring-id position — used only for these meta slots). Verbatim: `shl $0x18,%eax ; or $0x4800000,%eax`.

---

## 4. word2:3 — `buf_ptr`

The 64-bit pointer at offset `0x8` is the only address the descriptor carries:

- **Tx**: the **source** physical address (kernel `tx.buf_ptr = s_addr` `:279`; userspace stores the source reg before the copy).
- **Rx**: the **destination** physical address (kernel `rx.buf1_ptr = d_addr` `:304`).

There is no second buffer pointer in the M2M path — `buf2_ptr_lo` (the Rx alias of `meta_ctrl`) stays zero.

---

## 5. The Control-Bit Table

The authoritative cross-word control map. All confidence **HIGH** unless noted; `*` marks bits in the `meta_ctrl` word rather than `len_ctrl`.

| bit | field | word | mask | set by | arch |
|---|---|---|---|---|---|
| 31 | `CONCAT` | `len_ctrl` | `0x80000000` | `set_concat` / CME builder | all |
| 30 | `DMB` | `len_ctrl` | `0x40000000` | barrier = DMB (Tx) | all |
| 29 | `STRONG_ORDER_WR` | `len_ctrl` | `0x20000000` | barrier = SOW (Rx) | **V3+** |
| 28 | `INT_EN` | `len_ctrl` | `0x10000000` | `set_dst_int` | all |
| 27 | `LAST` | `len_ctrl` | `0x08000000` | build (FIRST\|LAST) | all |
| 26 | `FIRST` | `len_ctrl` | `0x04000000` | build (FIRST\|LAST) | all |
| 25:24 | `RING_ID` (phase) | `len_ctrl` | `0x03000000` | `ring_id << 24` | all |
| 23 | `META` | `len_ctrl` | `0x00800000` | CME / CRC meta-desc only | all |
| 15:0 | `LENGTH` | `len_ctrl` | `0x0000ffff` | `size & 0xffff` (`0`⇒64K) | all |
| 26* | `write_barrier` | `meta_ctrl` | `0x04000000` | `set_write_barrier` | all |
| 28* | `notification` | `meta_ctrl` | `0x10000000` | `set_notification_mask` | **V4** |
| 25:23* | `ssmae_op` | `meta_ctrl` | `0x03800000` | default `0b010` | all |
| 19* | `copy_source_data` | `meta_ctrl` | `0x00080000` | default `1` | all |
| 1* / 0* | `is_first_block` / `is_last_block` | `meta_ctrl` | `0x2` / `0x1` | default `1` | all |

---

## 6. Barrier Modes

The four memory-ordering modes (`enum`, `udma.h:196–201`) are routed in `udma_m2m_build_descriptor` (`:333–348`) and land in **three different bit locations**:

| mode | value | effect | location |
|---|---|---|---|
| `NONE` | 0 | (no flag) | — |
| `DMB` | 1 | `tx len_ctrl \|= BIT30` | Tx word0 |
| `WRITE_BARRIER` | 2 | `meta_ctrl bit26 = 1` | Tx word1 |
| `SOW` | 3 | `rx len_ctrl \|= BIT29` (V3+) | Rx word0 |

An invalid mode triggers `pr_err("Invalid m2m barrier is given")` and returns `-EINVAL` (`:346–347`). On the chunked-memcpy path both `ndma_get_m2m_barrier_type_v2` and `_v3` choose only `WRITE_BARRIER` (if `set_dmb`) or `NONE`; `DMB` and `SOW` are reachable only via explicit callers (e.g. the pod-election copy path). Doorbell ordering is **RX then TX** on both sides (`udma_m2m_copy_start:440–449`; userspace `0x45ed60`), so the destination ring is armed before the source read is launched.

---

## 7. Per-Arch Deltas

> **QUIRK —** the 16-byte layout and **every bit position above are arch-invariant** across SUNDA (v2), CAYMAN (v3), and MARIANA (v4). The per-arch differences are *capability gating and queue-tuning policy*, never a wire-format change. A descriptor built for v2 and one built for v4 with the same `(src, dst, size, phase, barrier)` are bit-for-bit identical.

| dimension | SUNDA (v2) | CAYMAN (v3) | MARIANA (v4) | source |
|---|---|---|---|---|
| `STRONG_ORDER_WR` (Rx bit29) | **forbidden** | allowed | allowed | `build_copy` arch gate @`0x45cd19`; `udma.h:70` "V3+ only" |
| `notification-mask` (meta bit28) | — | — | **required** | `build_packet` @`0x45cf70` ("not supported on arch < 4") |
| max TX descs / packet | `0x80` = 128 | `0x41` = 65 | `0x41` = 65 | userspace `init_engine` @`0x45c820` |
| max RX descs / packet | 128 | `0x3c` = 60 | `0x3c` = 60 | userspace `al_udma_s2m_max_descs_set` |
| V3 DGE burst workaround | — | `min_burst==max_burst==8` | — | `udma_m2m.c:105–106` |

> **GOTCHA —** requesting a strong-ordered write on SUNDA is not silently ignored — the userspace builder logs *"udma strong ordering not supported in TRN1!"* and the packet build **returns −1** (`build_copy_descriptor`, arch `<= 2` branch @`0x45cd19–0x45cd21`). The kernel applies one uniform cap (`UDMA_M2S_MAX_ALLOWED_DESCS_PER_PACKET_V4 = 128`, `8 ≤ max ≤ 128`, `udma_m2m.c:103`); userspace applies the tighter per-arch policy above.

---

## 8. Userspace ↔ Kernel Byte-Agreement

The format is authored twice and verified to agree field-by-field (userspace `libnrt.so` vs kernel `udma_m2m.c`):

| aspect | userspace | kernel | agree |
|---|---|---|---|
| descriptor size | 16 B (`al_copy_descriptor` = `memcpy`) | `sizeof(union udma_desc)` = 16 | **yes** |
| word0 pack | `ring<<24 \| flags \| len16` | `ring<<24 \| flags \| len16` | **yes** |
| 64K ⇒ len0 | `cmp $0x10000; cmove 0` @`0x45cb16` | `size==0x10000 ? 0` (`:271`) | **yes** |
| FIRST / LAST | `0x04000000` / `0x08000000` | `M2S_DESC_FIRST/LAST` BIT(26)/(27) | **yes** |
| DMB / INT_EN | BIT30 / `0x10000000` | `M2S_DESC_DMB` / `INT_EN` | **yes** |
| strong-order (Rx) | BIT29, arch>2 gate @`0x45cd1e` | `S2M_STRONG_ORDER_WR` BIT(29), V3+ | **yes** |
| ring-id / phase | `(rid<<24) & 0x3000000` | `<<24`, `& 0x3` wrap | **yes** |
| `meta_ctrl` default | `0x01080003` @`0x9e8828` | `0x01080003` (`:53–79`) | **yes** |
| write_barrier | meta bit26 (`orb $0x4,+3`) | meta bit26 (`cme_desc_word`) | **yes** |
| copy TX+RX pair | tx FIRST\|LAST + rx | `build_descriptor` same | **yes** |
| doorbell order | RX then TX (`0x45ed60`) | RX then TX (`:440–449`) | **yes** |
| `is_cme_desc` | `ssmae_op == 2` | `ssmae_op = 0b010` default | **yes** |

`CONCAT`, `META`, and the notification-mask are userspace-HAL features with **no kernel macro** on the M2M path — the kernel only emits plain copy pairs. The hardware honors the same `len_ctrl[31]=CONCAT` / `[23]=META` bits; the kernel simply never sets them. Both code lines author the identical 16-byte wire format; userspace exercises more of it.

> **Verification —** for the copy descriptor pair, a kernel-built and a userspace-built descriptor with the same `(src, dst, size, phase, barrier)` are bit-for-bit identical. The userspace size guard is marginally stricter (rejects `size == 0` because `(size−1)` underflows past `0xffff`), but both encode the valid `1..65536` range identically, so this is a guard difference, not a format divergence.

---

## 9. Considerations

- **`ssmae_op` / `op_type` op-classes beyond the default.** The M2M HAL marks `meta_ctrl` *"currently unmodifiable"* and only ever writes `ssmae_op = 0b010`. The full op-class enumeration (the SDMA/CCE/TDG compute ops set by the CAYMAN combo-op / transpose / CRC builders) is **not grounded** by this path — *(LOW confidence)*. See [Meta-Control Overlays](meta-ctrl-overlays.md) for what is known of the CCE op classes.
- **The CME meta-descriptor payload.** The `(idx<<24)|0x04800000` word0 is decoded above, but its `meta1`/`meta2` payload (the `tx_meta` view at offsets `0x8`/`0xC`) is exercised only by `al_udma_m2m_build_cme_packet` @`0x45d400` and the CAYMAN transpose builder — *not traced here*.
- **The completion descriptor** (`union udma_cdesc`, `udma.h:79–93`, with its own `UDMA_CDESC_LAST`/`FIRST` bits) is a *separate* 16-byte format on the completion ring — see [The Ring / Trigger / Doorbell / Completion Cycle](ring-cycle.md). This page documents only the submission descriptor.
- **The MLA address validator** (`addr_check` hook @`0xCAEB68`, installed at `tdrv` init) gates `(addr, size, is_src)` before a descriptor is built; its rejected ranges are not characterized here.

---

## Related Components

| Name | Relationship |
|---|---|
| `udma_m2m_build_descriptor` (kernel) / `al_udma_m2m_build_copy_descriptor` (userspace) | The two pair-orchestrators that author the format |
| `al_copy_descriptor` (`0x265980` = `memcpy`) | The 16-byte publish primitive |
| `union udma_cdesc` (`udma.h:79–93`) | The completion-ring descriptor (distinct format) |
| `vring` packer | Builds arrays of these descriptors host-side before dumping to a physical ring |

## Cross-References

- [Meta-Control Overlays](meta-ctrl-overlays.md) — the SDMA/CCE/TDG op-class detail of the `meta_ctrl` word
- [The Ring / Trigger / Doorbell / Completion Cycle](ring-cycle.md) — how a staged descriptor is launched and its completion harvested (the `0xabcdef01` marker model)
- [Virtual Rings (vring) and Packet Builders](virtual-rings.md) — the host-side authoring of descriptor arrays and `CONCAT` chaining for >64 KiB
- [UDMA Memory-to-Memory Builder](../kernel/udma-m2m.md) — the kernel build side, full function inventory
- [KaenaHal: UDMA/SDMA Descriptor Build and IOFIC](../runtime/hal-udma-iofic.md) — the userspace build side
- [IOFIC Interrupt Model](iofic.md) — why completion is polled, not interrupt-driven
- [The cc_op_entry On-Device ISA](../collectives/cc-op-isa.md) — the collective descriptor stream that rides these DMA rings
- [back to index](../index.md)
