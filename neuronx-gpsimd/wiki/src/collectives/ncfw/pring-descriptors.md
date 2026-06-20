# pring (Persistent DMA Descriptor Ring)

The **pring** is the *physical / persistent* DMA descriptor ring that the NCFW
management core (a scalar Xtensa-LX control core) and the TOP_SP NX cores actually
walk at runtime. It is **not** a collective-topology ring — peers, ranks and
rendezvous semaphores live in the ring/kangaring channel tape (see
[ring-kangaring](./ring-kangaring.md)). A pring is the lower-level transport those
collective steps *launch*: a contiguous array of 16-byte Annapurna-Labs UDMA
hardware buffer descriptors resident in device (HBM) memory, advanced by a
tail-pointer write to the [al_udma HW engine](../../dma/udma-hw-engine.md).

This page decodes, byte-exact:

- the **vring → pring** template-vs-physical split (the host builds a *virtual*
  template ring and copies it into the *physical* standing ring),
- the `al_udma_desc` descriptor element layout (the ring's atomic entry),
- the `dma_ring_info` host handle and the M2S/S2M ring pair,
- the per-basic-block **persistence** mechanism (`switch_completed` + the
  basic-block-switch doorbell + the reprogram/reset descriptor section),
- and the host-builder ↔ NCFW-structure reconciliation.

> **Why the host side is fully recoverable.** The NCFW *case bodies* — the
> on-device code that walks and re-arms the pring — run on the scalar Xtensa-LX
> management core, for which no TIE disassembler config ships; the only registered
> Xtensa config is `ncore2gp` (the Vision-Q7 NX core), so ~26–28 % of NCFW code
> bytes mis-decode as Vision FLIX bundles. But the pring is *data*, and both
> encoders are host x86-64: the **builder** lives in `libnrt.so` (with full DWARF —
> the authoritative struct source) and the **decoder** / pretty-printer lives in
> `libncfw.so` (whose printer byte-offsets mirror the firmware's DRAM-resident
> struct). Every offset, symbol and string below is read from those two host
> binaries or their DWARF. Addresses are file/VMA offsets in `libnrt.so` /
> `libncfw.so`, **not** device IRAM addresses.

---

## Provenance and confidence

| source | path / identity | role |
|--------|-----------------|------|
| `libnrt.so` | `…/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so.2.31.24.0`, 122 956 336 B, `.debug_info` present | the **encoder** that BUILDS prings; DWARF is the authoritative struct layout |
| `libncfw.so` | same package, 615 640 B, SHA-256 `598920d7…0aa3e49` | the host **decoder** that walks the firmware's DRAM config struct |

`.text`/`.rodata` are loaded VMA == file offset in both binaries (verified
`readelf -SW`); `libncfw.so` `.data` carries a +0x1000 delta (VMA `0x95020` vs
file `0x94020`) but **no** pring fact below is `.data`-resident. `libnrt.so`
`.data`/`.rodata` are also VMA == file offset for the ranges used here.

**Confidence tags.** `HIGH/OBSERVED` = read this session from disasm / DWARF /
verbatim string. `MED/INFERRED` = strong multi-fact convergence. `CARRIED` =
established on a sibling page and re-grounded here. The runtime *integer values*
of `pring_base_addr` / `pring_total_desc_num` are populated in HBM at NEFF load
and appear in **no** shipped static file — they are `LOW/OPEN` and never stated as
fact below.

> **CORRECTION to the survey hypothesis.** An earlier framing treated "pring" as a
> *persistent collective-ring topology* (a standing variant of the ring all-reduce
> with per-peer send/recv descriptors). The binaries do not support that reading.
> The config-key roster `pring_base_addr` / `pring_total_desc_num`
> (`libncfw.so` `.rodata` @0x654a8 / @0x654ba) and the copy primitives "Copying
> vring to pring" (@0x84728c) / "vring is copied to pring. ndesc=%u" (@0x81a6a8)
> are unambiguously a **descriptor**-ring vocabulary. `pring` = **physical ring**
> = a hardware DMA descriptor ring; `vring` = **virtual ring** = the host-side
> descriptor *template* it is copied from. The peer/rank topology is a *separate*
> object (the ring/kangaring channel tape). *(HIGH/OBSERVED.)*

---

## 1. The descriptor element — `al_udma_desc` (16 B union)

The pring's atomic entry is the Annapurna-Labs UDMA hardware buffer descriptor,
`union al_udma_desc`. DWARF DIE `<0x124eda>`, `DW_AT_byte_size 16`,
`DW_AT_alignment 16`, `decl_file 83`. It is a four-arm union over the same 16
bytes — the same word is read differently by the M2S (transmit) and S2M (receive)
sides of the engine.

**Table 1.0 — `union al_udma_desc` (16 B)** *(HIGH/OBSERVED — every member name +
`DW_AT_data_member_location` read from `libnrt.so` DWARF this session)*

| arm | off | w | field | type | role |
|-----|-----|---|-------|------|------|
| `tx` (M2S) | 0 | 4 | `len_ctrl` | u32 | transfer length + control bits |
| `tx` | 4 | 4 | `meta_ctrl` | u32 | per-descriptor metadata / CME control |
| `tx` | 8 | 8 | `buf_ptr` | u64 | 64-bit SoC source buffer address (engine **reads**) |
| `tx_meta` | 0 | 4 | `len_ctrl` | u32 | (shares bytes with `tx`) |
| `tx_meta` | 4 | 4 | `meta_ctrl` | u32 | |
| `tx_meta` | 8 | 4 | `meta1` | u32 | inline metadata word 1 |
| `tx_meta` | 12 | 4 | `meta2` | u32 | inline metadata word 2 |
| `rx` (S2M) | 0 | 4 | `len_ctrl` | u32 | |
| `rx` | 4 | 4 | `buf2_ptr_lo` | u32 | low 32 bits of a 2nd (split-buffer) pointer |
| `rx` | 8 | 8 | `buf1_ptr` | u64 | 64-bit SoC dest buffer address (engine **writes**) |
| `raw` | 0 | 8 | `qword0` | u64 | opaque copy/compare view |
| `raw` | 8 | 8 | `qword1` | u64 | |

The `tx` arm is the M2S (memory→stream) descriptor; the `rx` arm is the S2M
(stream→memory) descriptor; `tx_meta` is the M2S form that carries two inline
metadata dwords instead of a 64-bit pointer (used for semaphore/CCE control
descriptors, §7); `raw` is the byte-for-byte copy/compare view the
`vring_dump_to_pring` path moves.

A **pring** is therefore exactly:

```c
al_udma_desc pring[pring_total_desc_num];   /* contiguous, @ pring_base_addr in HBM */
```

### Reconciliation with the committed 16-B BD

This same 16-byte entry is documented on
[descriptor-ring-field-tables §1](../../dma/descriptor-ring-field-tables.md) as
`SDMA_CME_BD_DESC` (the "atomic ring entry every al_udma SDMA descriptor engine
reads"). The two are the **same struct**, named in two different DWARF units:

| `SDMA_CME_BD_DESC` (field-tables §1) | `al_udma_desc.tx` (here) | width |
|--------------------------------------|--------------------------|-------|
| `word0` (`SDMA_DESC_WORD0`, length+control bitfield) | `len_ctrl` | 4 |
| `word1` (`SDMA_CME_DESC_WORD1`, CME/CRC controls) | `meta_ctrl` | 4 |
| `buf_ptr` (u64 SoC address) | `buf_ptr` / `buf1_ptr` | 8 |

> **NOTE (upgrades a CARRIED claim).** The field-tables `SDMA_CME_BD_DESC` envelope
> was tagged CARRIED (its DWARF lives in `data_transfer.o`, not in that extraction).
> `al_udma_desc` here is the **same 16-B / 16-align envelope read directly** from
> `libnrt.so` DWARF (`<0x124eda>`), so the *outer* 16-B layout (two 4-B control
> dwords + an 8-B pointer) is now **OBSERVED**, not carried. The *interior* bit
> layout of `word0`/`word1` (length, ringid, `optype`/`op`, CRC controls) is still
> only on the field-tables page — `al_udma_desc.len_ctrl`/`meta_ctrl` are opaque
> u32s in this DWARF unit. The two pages are consistent.

> **GOTCHA (`len_ctrl` is not a plain length).** `len_ctrl` packs the byte count in
> the low 16 bits and control bits (`first`/`last`/`int_en`/ring-tag) in the high
> byte (field-tables Table 1.1). A reimplementation that writes a raw length into
> the whole dword will set spurious control bits. The firmware build mask for the
> length+gen-tag is `&0xFCFF0000` per the field-tables QUIRK.

---

## 2. The host handle — `dma_ring_info` (32 B)

One pring is owned host-side by a `struct dma_ring_info` (typedef
`dma_ring_info_t`). DWARF DIE `<0x128d55>`, `DW_AT_byte_size 32`, `decl_file 115`.

**Table 2.0 — `struct dma_ring_info` (32 B)** *(HIGH/OBSERVED — DWARF +
`dma_ring_info_alloc_from_memchunk` field stores confirm)*

| off | w | field | type | notes |
|----|---|-------|------|-------|
| 0 | 4 | `type` | `dma_ring_type_t` | `INVALID=0` / `TX=1` (M2S) / `RX=2` (S2M) |
| 8 | 8 | `ring_mem` | mem-ref `void*` | backing memchunk / dmem reference |
| 16 | 8 | `ring_offset_bytes` | u64 | byte offset of the ring base within `ring_mem` |
| 24 | 4 | `used_desc_count` | u32 | descriptors actually written |
| 28 | 4 | `allocated_desc_count` | u32 | descriptors allocated (= `pring_total_desc_num`) |

`pring_base_addr` is derived as `ring_mem` resolved to a device address +
`ring_offset_bytes`; `pring_total_desc_num` == `allocated_desc_count`.

**Table 2.1 — `enum dma_ring_type` (4 B)** — DWARF `<0x128f97>`, `encoding 7
(unsigned)` *(HIGH/OBSERVED)*

| name | value | direction |
|------|-------|-----------|
| `DMA_RING_TYPE_INVALID` | 0 | — |
| `DMA_RING_TYPE_TX` | 1 | M2S — memory → stream |
| `DMA_RING_TYPE_RX` | 2 | S2M — stream → memory |

A pring is asserted to be TX or RX, never INVALID, at use: the runtime check
`pring->type == DMA_RING_TYPE_TX || pring->type == DMA_RING_TYPE_RX`
(`libnrt.so` `.rodata` @0x81a620) backstops this.

**Allocator (`dma_ring_info_alloc_from_memchunk` @0x22d5f0)** — the field stores
match the DWARF offsets byte-for-byte:

```text
22d610:  mov  QWORD PTR [rbx+0x10], rbp     ; dma_ring_info.ring_offset_bytes    @+16
22d614:  mov  QWORD PTR [rbx+0x08], rax     ; dma_ring_info.ring_mem             @+8
22d61d:  mov  DWORD PTR [rbx+0x1c], r12d    ; dma_ring_info.allocated_desc_count @+28
```

`dma_pring_alloc` (@0x22d8b0) `calloc`s a 0x20-byte `dma_ring_info` then calls
`dma_ring_alloc` (@0x22d7a0). *(HIGH/OBSERVED — symbols `nm`-verified.)*

---

## 3. The vring template vs the pring physical ring

The persistence boundary is the **vring → pring copy**. The host first builds a
*virtual* ring (`vring`) — a linked list of large descriptor pages holding
**template/variable** descriptors (with variable IDs, edited each compile) — then
**copies** it into a *physical* ring (`pring`) of concrete device addresses that
the engines actually walk.

### 3.1 The vring (virtual / template ring)

**Table 3.0 — `struct vring` (344 B)** — DWARF `<0x128e70>`, `byte_size 344`
*(HIGH/OBSERVED)*

| off | w | field | type | role |
|----|---|-------|------|------|
| 0 | 256 | `name` | `char[256]` | debug name (logged by "Copying vring to pring %s") |
| 256 | 32 | `tx` | `vring_pages_t` | the M2S virtual descriptor pages |
| 288 | 32 | `rx` | `vring_pages_t` | the S2M virtual descriptor pages |
| 320 | 8 | `total_tx_size` | u64 | aggregate M2S byte count |
| 328 | 8 | `total_rx_size` | u64 | aggregate S2M byte count |
| 336 | 1 | `stochastic_rounding_en` | bool | per-ring SR flag |

**Table 3.1 — `struct vring_pages` (32 B)** — DWARF `<0x128e0f>` *(HIGH/OBSERVED)*

| off | w | field | type | role |
|----|---|-------|------|------|
| 0 | 4 | `used_desc_count` | u32 | descriptors filled |
| 8 | 8 | `head` | `vring_desc_page*` | first page |
| 16 | 8 | `tail` | `vring_desc_page*` | last page |
| 24 | 1 | `is_template` | bool | **this ring holds variable/template descriptors** |
| 28 | 4 | `max_var_id` | u32 | highest variable-descriptor id |

**`vring_desc_page` (DWARF `<0x128da4>`, `byte_size 0x100010`)** is a 1-MiB
descriptor page + linked-list pointers:

| off | w | field | type |
|----|---|-------|------|
| 0 | 0x100000 | `desc[65536]` | `al_udma_desc` (element `<0x124eda>`, upper_bound 65535) |
| 0x100000 | 8 | `next` | `vring_desc_page*` |
| 0x100008 | 8 | `prev` | `vring_desc_page*` |

So a vring is a doubly-linked list of 65 536-entry × 16-B descriptor pages
(0x100000 B of descriptors + 16 B of links). The `is_template` / `max_var_id`
fields are what make the vring *virtual*: it carries variable descriptors resolved
to concrete addresses only at the copy step.

**`vring_set_t` (DWARF `<0x128ede>`, `byte_size 5520`)** groups up to 16 vrings —
one per DMA queue / TOP_SP queue-bundle:

| off | w | field | type |
|----|---|-------|------|
| 0 | 5504 | `vrings` | `vring_t[16]` (upper_bound 15; 16 × 344 = 5504) |
| 5504 | 4 | `count` | u32 |
| 5512 | 8 | `tile_idx` | u64 |

> **NOTE (the 16).** `vring_set_t.vrings[16]` matches the 16 runtime TOP_SP /
> queue-bundle count and the al_udma's 16 M2S + 16 S2M queues
> ([udma-hw-engine](../../dma/udma-hw-engine.md), [field-tables §6](../../dma/descriptor-ring-field-tables.md)).
> One vring/pring per queue per tile. *(HIGH/OBSERVED struct; the binding to the 16
> queues is CARRIED from those pages.)*

### 3.2 The vring → pring copy (template → physical)

The decisive persistence boundary. The host builds a vring (virtual, template
descriptors) then **dumps** it into a pring (physical, concrete device addresses).
Copy primitives in `libnrt.so` *(all `nm`-verified, HIGH/OBSERVED)*:

| symbol | addr | role |
|--------|------|------|
| `vring_dump_to_pring` | 0x3137e0 | copy a vring into a pring |
| `vring_dump_to_pring_padded` | 0x3136f0 | copy + pad to a fixed descriptor count |
| `vring_dump_to_pring_descriptors` | 0x3136e0 | copy the descriptor body |
| `vring_dump_to_pring_descriptors_padded` | 0x311f10 | + padding |
| `dma_ring_create_prings_from_vring` | 0x22e310 | alloc a TX pring **and** an RX pring from one vring |

Verbatim strings (`libnrt.so` `.rodata`): `"Copying vring to pring %s"` (@0x84728c);
`"vring is copied to pring. ndesc=%u"` (@0x81a6a8) and `…ndesc=%u/%u` (@0x81a990);
`"Failed to write vring descriptors to pring"` (@0x802668);
`"[nec_dev %u] failed to dump vring %d to pring"` (@0x803200).

`dma_ring_create_prings_from_vring` (@0x22e310) makes the **pair** explicit — it
`dma_pring_alloc`s twice and `vring_dump_to_pring_descriptors` twice (one TX, one
RX):

```text
22e37f:  call  22d8b0 <dma_pring_alloc>                    ; TX pring
22e3af:  call  3136e0 <vring_dump_to_pring_descriptors>    ; dump M2S
22e435:  call  22d8b0 <dma_pring_alloc>                    ; RX pring
22e461:  call  3136e0 <vring_dump_to_pring_descriptors>    ; dump S2M
```

**The per-iteration-vs-persistent split, byte-grounded:**

| | vring (virtual / template) | pring (physical / standing) |
|---|---|---|
| where | host DRAM only | device HBM @ `pring_base_addr` |
| element | `al_udma_desc` with var ids | concrete `al_udma_desc[N]` |
| `is_template` | true | (n/a — concrete) |
| lifetime | rebuilt/edited per build/compile | dumped **once**, then re-armed in place |
| who walks it | nobody (it's a template) | the SDMA M2S/S2M engines, across iterations |

> **The persistence claim, scoped honestly.** "The pring survives across iterations"
> is `MED/INFERRED` — it follows from the switch-not-rebuild path (§5), the
> reprogram/reset *re-arm* section, and the per-communicator exec-pring lifetime,
> **not** from a decoded device loop (the device walk runs on the un-decodable LX /
> NX cores). The structures and the host build chain are `HIGH/OBSERVED`; the
> on-device re-use is the inference those structures force.

### 3.3 The `kbin_dma_ring_type_t` use classes

Orthogonal to the TX/RX *direction*, `enum kbin_dma_ring_type_t` (`libnrt.so`
`.rodata` @0x48446e7) classifies what a pring *carries*:

`KBIN_DMA_RING_TYPE_INDIRECT_MEMCPY`, `_H2T_COPY`, `_COLLECTIVES`, `_ACT_TBL`,
`_DYNAMIC_ACT_TBL`, `_MODEL`, `_ENG`, `_CUSTOM_OP` (bounded by
`KBIN_DMA_RING_TYPE_LAST`, @0x8021f8).

`KBIN_DMA_RING_TYPE_COLLECTIVES` is the class for the ring/kangaring collective
transport — the pring whose descriptors a collective step launches (see
[ring-kangaring](./ring-kangaring.md)). *(HIGH/OBSERVED — enum strings.)*

---

## 4. Where the pring binds — `basic_block_configs`

Each NCFW **basic block** carries a standing TopLevel-DMA pring (a pair: M2S + S2M,
§6). The binding fields live in the firmware's DRAM-resident `basic_block_configs`
struct. Two host views expose the **same three fields** at *different* byte offsets:
the `libncfw.so` decoder reads the **device-image** layout; the `libnrt.so` builder
writes the **host-config** layout, which is DMA'd onto the TOP_SP.

### 4.1 Device-image view (`libncfw.so` decoder)

`ncfw_log_basic_block_configs` (@0x11cc7, four arch copies at
0x11cc7 / 0x2aa6e / 0x43815 / 0x5c5bc) is the host x86 printer that walks the
firmware struct; its load offsets mirror the firmware's layout.

**Table 4.0 — device-image `basic_block_configs` pring fields** *(HIGH/OBSERVED —
the loads are byte-identical across all four arch copies)*

| field | struct off | width | config key (`.rodata`) | load instruction |
|-------|-----------|-------|------------------------|------------------|
| `switch_completed` | +0x400 | u64 | @0x65497 | `mov r12, [rax+0x400]` @0x12101 |
| `pring_base_addr` | +0x408 | u64 | @0x654a8 | `lea r12, [rax+0x408]` @0x12479 |
| `pring_total_desc_num` | +0x410 | u32 | @0x654ba | `lea r12, [rax+0x410]` @0x1266b |

The four arch copies repeat the identical loads at
0x2aea8 / 0x2b220 / 0x2b412 (cayman), 0x43c4f / 0x43fc7 / 0x441b9 (mariana),
0x5c9f6 / … (mariana_plus) — verified this session. The config-key roster also
ships four times (key block @0x65476.., 0x65b0b.., 0x661a0.., 0x66835..).

> **QUIRK (the printer prints the field *address*).** For the two pring fields the
> `libncfw.so` printer `lea`s `&field` and formats it with `%p` rather than
> dereferencing — so the decoder emits the *address* of `pring_base_addr`, not its
> value. The authoritative field **widths** come from the `libnrt.so` builder's log
> (§4.2), which prints the values as `%lx`/`%x`/`%lx`. Do not mistake the decoder's
> `%p` output for the pring base. *(HIGH/OBSERVED — the `lea`+`%p` pair.)*

**Placement.** `basic_block_configs` is reached from `ncfw_log_neff_configs`
(@0x12864) at sub-struct offset **+0x9c0** (`lea rdx,[rax+0x9c0]` @0x12ac6 → `call
11cc7`), adjacent to `barrier_config` at **+0x898** (`lea rdx,[rax+0x898]` @0x12a99
— see [neff-device-barrier](./neff-device-barrier.md)). *(HIGH/OBSERVED.)*

### 4.2 Host-config view (`libnrt.so` builder)

After the builder returns (§6), the host writes the three results into the host
`basic_block_configs` struct (base `r14`) and logs them. The stores
*(HIGH/OBSERVED, @0x2480ce..0x248179)*:

```text
2480ce:  call 23f280 <encd_basic_block_create_toplevel_dma_pring>
248101:  mov  QWORD PTR [r14+0x5fa8], rax   ; switch_completed       @+0x5fa8 (u64)
24810e:  mov  DWORD PTR [r14+0x5fb0], eax   ; pring_total_desc_num   @+0x5fb0 (u32)
248165:  mov  QWORD PTR [r14+0x5fa0], rbx   ; pring_base_addr        @+0x5fa0 (u64)
```

then logs (@0x807c18): `"basic_block_configs.pring_base_addr = %lx
basic_block_configs.pring_total_desc_num = %x basic_block_configs.switch_completed
= %lx"`. The widths are therefore **`pring_base_addr` u64**, **`pring_total_desc_num`
u32**, **`switch_completed` u64**.

> **CORRECTION / NOTE (host offsets ≠ device offsets).** The host struct
> (+0x5fa0 / +0x5fa8 / +0x5fb0) and the device-image struct (+0x408 / +0x400 /
> +0x410) carry the **same three semantic fields at different byte layouts** — two
> distinct structs (host config vs device-resident config). The host builds its
> config, then DMAs the device config onto the TOP_SP. They reconcile by **field
> role**, not numeric offset. A reimplementation must not assume the two layouts
> are byte-compatible. *(field-set identity HIGH; the host↔device byte mapping is
> by role — MED on byte-equivalence.)*

### 4.3 The per-block descriptor-count table

The `table` sub-key (@0x65491) decodes via `ncfw_log_basic_block_table` (@0x11452,
4 copies at 0x11452 / 0x2a1f9 / 0x42fa0 / 0x5bd47). It walks a ≤256-entry array of
4-byte records (loop bound `cmp …,0xff`, element stride `lea rdx,[rax*4]` @0x1157a,
zero-WORD terminator `movzx eax,WORD PTR [rax]` @0x11589 / `test ax,ax` @0x1158c):

**Table 4.1 — `basic_block_table` record (4 B)** *(stride + bound + terminator
HIGH/OBSERVED; per-field naming via the key strings)*

| off | w | field | key | load | conf |
|----|---|-------|-----|------|------|
| 0 | 2 | `desc_count` | @0x65420 | `movzx eax,WORD PTR [rax]` @0x11589 | HIGH/OBS |
| 2 | 1 | `reset_section_desc_count` | @0x65476 | `movzx eax,BYTE PTR [rax+0x2]` @0x1198e | HIGH/OBS |
| 3 | 1 | `type` | @0x650e3 | — | MED/INFERRED |

Plus a per-entry `channels` field/list (key @0x65243). The table maps each basic
block to how many pring descriptors it uses (`desc_count`) and how many of those
are the re-arm/reset section (`reset_section_desc_count`) — the data that drives
the persistence/re-arm of §5.

---

## 5. The persistence mechanism

Three independent, byte-exact evidence strands establish that the pring is a
**standing ring switched / re-armed across iterations**, not a one-shot per-iter
descriptor list.

### (a) `switch_completed` + the basic-block-switch doorbell

`basic_block_configs.switch_completed` (device +0x400 / host +0x5fa8, §4) is a
64-bit completion word. The TOP_SP has a dedicated **basic-block-switch doorbell**;
the host resolves its register address via `encd_get_basic_block_switch_topsp_reg_addrs`
(@0x236320) and polls it from `enc_network_proxy_task::check_basic_block_switch`
(@0x1d01c0, mangled `_ZN22enc_network_proxy_task24check_basic_block_switchEjPb`).
The firmware walks a basic block's standing pring, sets `switch_completed`, and
**switches** to the next basic block's pring — a switch, not a rebuild.
*(HIGH/OBSERVED — the field + the two symbols.)*

### (b) The reprogram / reset descriptor section (in-place re-arm)

`basic_block_table.reset_section_desc_count` (§4.3) sizes a **reprogram/reset
section** of descriptors that re-arm a standing pring *in place* between iterations
instead of re-allocating it. The re-arm payload is a fixed sextet of SPAD
(scratchpad register) descriptors that rewrite the ring's tail-pointer state — the
strings spell out each one *(HIGH/OBSERVED, `libnrt.so` `.rodata`)*:

| string @addr | what the descriptor re-writes |
|--------------|-------------------------------|
| `Descriptor added for reprogram m2s_low …` @0x806eb8 | M2S tail-pointer low dword |
| `Descriptor added for reprogram m2s_high …` @0x806f08 | M2S tail-pointer high dword |
| `Descriptor added for reprogram m2s_size …` @0x806f58 | M2S ring size |
| `Descriptor added for reprogram s2m_low …` @0x806fa8 | S2M tail-pointer low dword |
| `Descriptor added for reprogram s2m_high …` @0x806ff8 | S2M tail-pointer high dword |
| `Descriptor added for reprogram s2m_size …` @0x807048 | S2M ring size |
| `Descriptor added for reprogram CTRL SPAD …` @0x806c60 | control SPAD |
| `Descriptor added for reprogram SLOT %d SPAD …` @0x806d00 | per-slot SPAD |

Driven by the section builders (attested as `__PRETTY_FUNCTION__` rodata strings —
these are internal/static functions, not in the dynamic symtab):
`encd_basic_block_build_toplevel_reprogram_sdma_descs` (@0x9bc880),
`…_reprogram_spad_descs` (@0x9bc940), `…_reprogram_section_descs` (@0x9bc9c0).
Section-completion logs: `"Descriptors added to reprogram section … for basic
block %u toplevel_desc_n=%u"` (@0x806e00); the reset path
`"Failed to build Toplevel DMA descriptors for reset (network proxy)"` (@0x8073a8).

So the persistence is explicit at the descriptor level: a standing pring carries a
section that **rewrites its M2S/S2M tail pointers and ring sizes** to re-aim it,
rather than rebuilding the descriptor array. *(strings HIGH/OBSERVED; "in-place
re-arm = persistence" MED/INFERRED.)*

### (c) The exec prings (per-collective-communicator persistent rings)

For collectives the persistence is the cleanest: `nrt_cc_prepare` (@0x7f610, the
collective-communicator *prepare* entry) sets the exec prings **once** at prepare
time and frees them only at teardown:

```text
7fa97:  call 23f9b0 <encd_set_exec_prings>     ; set once at prepare
7fcbd:  call 23f850 <encd_free_exec_prings>    ; free at teardown / error
```

The error string (@0x7cff50): `"[host CC] Failed to set exec prings, global device
ID is %u, world size is %u, and root comm ID is %s"`. These prings are scoped to
the communicator (root comm ID, world size) — i.e. **persistent across the
collective's iterations**, the literal "persistent ring" for collectives.
*(HIGH/OBSERVED — the set/free pair under `nrt_cc_prepare` + the comm-scoped
string.)*

> **CONTRAST (two distinct pring classes).** The firmware supports **both** a
> per-invocation pring class — `"[nec_dev %u] Failed to alloc per-invocation
> prings"` @0x8054b0 — **and** the persistent TopLevel/exec class this page covers.
> They are separate allocation paths; do not conflate them. The vring (§3) is the
> per-build *template*; the per-invocation pring is a per-call ring; the
> TopLevel/exec pring is the *standing* ring. *(HIGH/OBSERVED — two distinct string
> sets + two builder paths.)*

---

## 6. The host builder — `encd_basic_block_create_toplevel_dma_pring`

The persistent-pring builder (`libnrt.so` @0x23f280) is fully x86-decodable. The
verified call chain *(HIGH/OBSERVED — every target + immediate disassembled this
session)*:

```text
23f2c9:  call   22c2d0 <get_default_hbm_index>            ; 1. pick HBM index
23f3d8:  call   228f20 <dmem_alloc_aligned>               ; 2. alloc HBM ring buffer -> pring_base_addr
23f498:  movabs rax, 0x200000001                          ; 3. the {TX=1, RX=2} type pair
23f4d9:  call   3c8a0  <calloc@plt>                        ; 4. alloc dma_ring_info(s)
23f4fc:  call   22d5f0 <dma_ring_info_alloc_from_memchunk> ;    build the handle (§2)
23f684:  call   3136f0 <vring_dump_to_pring_padded>        ; 5. COPY vring -> physical pring, padded
;   then log M2S (@0x805368) and S2M (@0x8053c0)
;   error path: 22d590 <dma_queue_free_static_rings>, 2293a0 <dmem_free>
```

Step 3's `movabs rax, 0x200000001` (encoded `48 b8 01 00 00 00 02 …`) packs the
type pair: low dword `1` = `DMA_RING_TYPE_TX` (M2S), high dword `2` =
`DMA_RING_TYPE_RX` (S2M). The builder produces a **pair per basic block** and logs
each:

```text
[nec_dev %u] TopLevelDMA M2S pring for basic block %u (addr=0%lx) (desc_cnt=%lx)   @0x805368
[nec_dev %u] TopLevelDMA S2M pring for basic block %u (addr=0%lx) (desc_cnt=%lx)   @0x8053c0
```

(failure: `"[nec_dev %u] Failed to create TopLevel DMA prings"` @0x807be0.) The
caller @0x2480ce writes the returned `{base, desc_count, switch_completed}` into
`basic_block_configs` (§4.2).

> **The M2S/S2M reset asymmetry (cross-page CORRECTION carried).** The two prings
> are **not** symmetric. Per [field-tables §6](../../dma/descriptor-ring-field-tables.md):
> `comp_cfg.en_comp_ring_update` resets to **1 (ON) on S2M** (`udma_s2m`
> `comp_cfg@0x54`) but **0 (OFF) on M2S** (`udma_m2s` `comp_cfg@0xa0`). A
> reimplementation must program the M2S and S2M completion-ring-update bits
> oppositely; assuming both default to the same value is a real bug. *(CARRIED —
> verified on field-tables.)*

---

## 7. Topology and semaphore bindings

The pring is the **transport**, not the topology. The collective ring/kangaring
topology — peer order, ranks, rendezvous semaphores — lives in the channel tape
(see [ring-kangaring](./ring-kangaring.md)); the pring carries the DMA descriptors
those steps launch.

### 7.1 M2S/S2M tail pointers ↔ the al_udma doorbells

The per-basic-block pring pair (M2S = TX, S2M = RX) is advanced by the al_udma
doorbells documented on [field-tables §6](../../dma/descriptor-ring-field-tables.md):
`TDRTP_inc` (TX/M2S) and `RDRTP_inc` (RX/S2M), both at queue-bank `+0x38` →
absolute **0x1038** for queue 0, queue stride **0x1000**, field `val[23:0]`. A
tail-pointer increment of N to the M2S bank launches N outbound descriptors from
the pring; an increment to the S2M bank posts N inbound buffers. *(M2S/S2M ↔
`TDRTP_inc`/`RDRTP_inc` HIGH — same naming, same role; the absolute-address
arithmetic CARRIED from field-tables.)*

### 7.2 The per-step trigger and the semaphore descriptors

The tail-pointer write is the per-step TOP_SP trigger. The pring's
`al_udma_desc.meta_ctrl` / `meta1` / `meta2` (the `tx_meta` arm, §1) carry the
per-descriptor **semaphore / CCE control** — the EVT_SEM `SEMAPHORE_INC` ops that
signal the collective's recv/send/post/dma-completion semaphores. These are added
to the vring (before the copy) by *(HIGH/OBSERVED, `nm`-verified)*:

| symbol | addr | role |
|--------|------|------|
| `vring_add_desc_semaphore_inc` | 0x312310 | append a SEMAPHORE_INC descriptor |
| `vring_add_desc_semaphore_inc_from_sb` | 0x312370 | …from a scoreboard source |
| `vring_add_desc_event` | 0x3122b0 | append an event descriptor |
| `encd_get_trigger_addr` | 0x23f150 | resolve the trigger (EVT_SEM) address baked into the descriptor |
| `dma_ring_get_sema_to_inc` | 0x22dc80 | resolve which EVT_SEM a ring's completion descriptor increments |

`dma_ring_get_sema_to_inc` (@0x22dc80) keys on the ring type — `cmp DWORD PTR
[rsi],0x2` tests `dma_ring_info.type == RX` (the S2M completion path), then reads
the ring's sema index from the handle. *(HIGH/OBSERVED — the type discriminant.)*
The concrete EVT_SEM index and the `sp_base+0x1800` increment window are
runtime-bound (the trigger-address arithmetic runs in the assert-guarded body and
the device cores); that binding is `CARRIED`, not re-derived here.

---

## 8. Per-generation presence

- The `libncfw.so` decoder `ncfw_log_basic_block_configs` ships in **all four** arch
  copies (sunda 0x11cc7, cayman 0x2aa6e, mariana 0x43815, mariana_plus 0x5c5bc)
  with **byte-identical** pring field offsets (+0x400 / +0x408 / +0x410, verified
  this session). `ncfw_log_basic_block_table` likewise 4× (0x11452 / 0x2a1f9 /
  0x42fa0 / 0x5bd47). The pring schema is **arch-wide**. *(HIGH/OBSERVED.)*
- `libnrt.so` is a **single** binary serving all generations; the
  pring / vring / `al_udma_desc` / `dma_ring_info` DWARF and the builders
  (`encd_basic_block_create_toplevel_dma_pring`, `dma_ring_create_prings_from_vring`,
  `encd_set_exec_prings`) are generation-agnostic. The only per-arch split is in
  the HW-queue-id getters and the `sp_base`/semaphore offsets.
- The pring *values* (`pring_base_addr`, `pring_total_desc_num`) are runtime HBM
  addresses written by the host builder at NEFF load / `cc_prepare` and DMA'd onto
  the TOP_SP — they are **not** in the static DRAM image (which is ~99 % zero). The
  per-gen difference is only the runtime HBM layout the per-arch getters compute.
  *(MED/INFERRED.)*
- CAYMAN = NC-v3. The v2–v4 structures are byte-grounded above; **v5 NCFW is
  FILE-ABSENT** in this extraction — no v5 pring interior is stated as fact.

---

## 9. Reconciliation summary

```text
host (libnrt enc_*/dma_ring_*)                       device (NCFW, LX/NX cores)
---------------------------------------------------  ------------------------------
vring (template; is_template / max_var_id)           - (host-only, per build)
  | vring_dump_to_pring_padded   (COPY)
  v
dma_ring_info (TX/RX, ring_mem+offset, alloc_cnt)    pring = al_udma_desc[N] @ HBM
  | encd_basic_block_create_toplevel_dma_pring
  | (M2S pring + S2M pring per basic block)
  v
host basic_block_configs                             device basic_block_configs
  pring_base_addr      @+0x5fa0  --- DMA to TOP_SP -->  pring_base_addr      @+0x408
  pring_total_desc_num @+0x5fb0  ------------------->   pring_total_desc_num @+0x410
  switch_completed     @+0x5fa8  ------------------->   switch_completed     @+0x400
  | nrt_cc_prepare -> encd_set_exec_prings (persist)
  v
the engines walk the basic block's standing pring,   (ring/kangaring channel tape
tail-pointer-increment per step, set switch_completed supplies the TOPOLOGY; the
+ ring the basic-block-switch doorbell to SWITCH to   pring carries the DMA +
the next block's pring; reprogram-section descriptors SEMAPHORE_INC descriptors that
RE-ARM the M2S/S2M tail pointers in place.            signal those semaphores.)
```

**Answer to the original ask, against the correct object:**

- *"ring topology / per-peer send/recv descriptors"* → those are the
  ring/kangaring channel tape, **not** the pring. The pring is the
  `al_udma_desc[N]` DMA transport those steps launch.
- *"the persistent descriptor that survives iterations vs the one-shot setup"* →
  the **vring** is the one-shot/per-build template; the **pring** is the persistent
  physical ring it is dumped into, switched/re-armed per basic block
  (`switch_completed` + basic-block-switch doorbell + reprogram section), and set
  **once** per collective communicator as the exec prings.
- *"field offsets"* → `al_udma_desc` 16 B (§1); `dma_ring_info` 32 B (§2); `vring`
  344 B, `vring_set_t` 5520 B (§3); device `basic_block_configs` pring fields
  @+0x400 / +0x408 / +0x410 (§4.1); host fields @+0x5fa0 / +0x5fa8 / +0x5fb0 (§4.2)
  — all byte-exact.

---

## See also

- [ring-protocol (config command)](./ring-protocol-config-command.md) — the
  send/wait protocol the pring's tail-pointer steps drive
- [ring / kangaring algorithm](./ring-kangaring.md) — the collective topology
  whose steps launch pring descriptors
- [NEFF device barrier](./neff-device-barrier.md) — `barrier_config` @+0x898,
  adjacent to `basic_block_configs` @+0x9c0
- [al_udma HW engine](../../dma/udma-hw-engine.md) — the M2S/S2M engine that walks
  the pring
- [descriptor ring field tables](../../dma/descriptor-ring-field-tables.md) — the
  16-B BD bit interior, the `TDRTP_inc`/`RDRTP_inc` doorbells, the M2S/S2M reset
  asymmetry
- [descriptor model](../../dma/descriptor-model.md) — the layered descriptor model
  (the 64-B TPB DMA word expanded into the 16-B BDs a pring holds)
