# SDMA / CCE / TDG Meta-Control Overlays

> *Userspace addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, SONAME `libnrt.so.1`, ELF64, **not stripped**, DWARF present, KaenaHal-2.31.0.0 `al_hal_sdma.c` / `al_hal_sdma_m2m.c` statically linked; `.text` VMA == file offset, so every `0x45…`/`0x9e…` is an analysis VMA). The firmware-side context schema is cited into `libncfw.so` (build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, SONAME `libncfw.so.2.31.1.0`, ELF64, not stripped, dlopen'd by `libnrt.so`).*
> *Evidence grade: **Confirmed (byte-anchored)** for the op-class base words and the default; **Inferred / per-arg LOW** for the human-readable name of each interior bit-field argument (no DWARF parameter names on the `al_hal_sdma.c` TU). Other versions will differ. · Part VIII — DMA & Descriptor Engine · [back to index](../index.md)*

## Abstract

The 16-byte UDMA descriptor's second word, `meta_ctrl` (Tx desc + 4), is owned in its *base* layout by [The 16-Byte Descriptor Wire Format](descriptor-format.md): that page fixes the two C overlays on the word, the default value `0x01080003`, the write-barrier at bit 26, and the rule `is_cme_desc == (ssmae_op == 0b010)`. This page does **not** re-derive any of that. It documents the one dimension descriptor-format deliberately punted (its §9 marks the op-class enumeration *"not grounded … (LOW confidence)"*): the **per-engine overlays** that the SDMA compute builders write into the `op_type[22:20]` / `ssmae_op[25:23]` selector region to turn a plain copy into a *compute-while-copy* macro-op.

The mental model is a base word with a small, fixed set of mutually exclusive **op-class overlays**. The plain memory-to-memory copy path leaves `meta_ctrl = 0x01080003` (the default authored by `al_tdma_m2s_meta_ctrl_default_value`, `.rodata` bytes `03 00 08 01` @`0x9e8828`), whose `ssmae_op = 0b010` is the CME-class tag. The TPB-compute SDMA path — exercised only by the `al_sdma_m2s_build_*_meta_ctrl` family (`0x451980`–`0x451dc0`) — replaces the selector bits with a distinct **top opcode word** per op: transpose `0x900000`, replication `0xA00000`, striding-split `0x800000`, add / min-max `0x2000000`, fma `0x2100000`, **cce_ext `0x2400000`** (the Collective-Compute-Engine overlay), gradient `0x2500000`. Each builder additionally packs operand dtype, precision, and sub-op nibbles into the interior of the same word. Three engine families ride the one word: **SDMA** (the plain copy + transpose/replication/split data-movement ops), **CCE** (the collective reduce/compute ops, base `0x2400000` plus the `0x2000000`-family arithmetic the on-device collective scheduler drives), and **TDG** (the descriptor-generator op classes the firmware reflects through its `cc_op_entry` `algo_type` selector).

The reason the encoding is worth a page of its own is the **two-sided contract**: the userspace HAL m2s builder in `libnrt.so` writes these op words into the descriptor that the silicon's SDMA engine executes, while the firmware serializer in `libncfw.so` reflects the *same* scheduling decisions through the on-device CC-context (`ncfw_log_spad_ctrl_cc_op_entry` @`0x1840`, the per-op scheduler descriptor). Userspace authors the op; firmware schedules and reflects it; both must agree on the meaning of the selector field. A reimplementer who builds the descriptor with the wrong op word, or who collides two op classes that share a base (`add` and `min_max` both start `0x2000000`), produces a descriptor the engine silently mis-executes.

For reimplementation, the contract is:

- **The base + overlay model** — `meta_ctrl` is the default `0x01080003` for plain copy; a compute op *replaces* the selector region with one of the fixed op words. The base 32-bit layout (the two C overlays, `write_barrier` bit 26, `is_cme_desc`) is owned by descriptor-format — reproduce it from there, not here.
- **The per-engine overlay table** — the exact top-opcode word and interior operand nibbles each `al_sdma_m2s_build_*_meta_ctrl` builder packs, and the `add`/`min_max` shared-base disambiguation.
- **The CCE overlay** — `cce_ext` base `0x2400000`, and that the collective-compute arithmetic (add / fma / gradient / min_max) is the `0x2000000`-family driven by the on-device collective scheduler whose op descriptor is `cc_op_entry`.
- **The userspace ↔ firmware agreement** — the userspace builder authors `meta_ctrl`; the firmware `cc_op_entry.algo_type/algo_sub_type` selects which on-device scheduler family consumes it. Both name the same op classes.

| | |
|---|---|
| **Word** | `meta_ctrl` — Tx desc `+0x4`, 32-bit, little-endian (Rx alias = `buf2_ptr_lo`, unused) |
| **Base layout owner** | [descriptor-format.md §3](descriptor-format.md) — two C overlays, default, `write_barrier` bit 26 |
| **Selector region** | `op_type[22:20]` + `ssmae_op[25:23]` — the per-engine overlay lives here |
| **Plain-copy default** | `0x01080003` (`al_tdma_m2s_meta_ctrl_default_value` @`0x9e8828`, `ssmae_op=0b010`) |
| **SDMA op builders** | `al_sdma_m2s_build_{transpose,replication,striding_split,add,fma,min_max,gradient,cce_ext}_meta_ctrl` @`0x451980`–`0x451dc0` |
| **CME-class tag** | `is_cme_desc` @`0x45ceb0` ⇒ `(u16@meta+2 >> 7) & 7 == 2` (the *default* `ssmae_op`) |
| **CCE overlay** | `cce_ext` base `0x2400000` (`al_sdma_m2s_build_cce_ext_meta_ctrl` @`0x451dc0`) |
| **write_barrier** | `meta_ctrl` bit 26 (`al_sdma_m2s_set_write_barrier` @`0x451670`, `orb $0x4,0x3(%rdi)`) — owned by descriptor-format |
| **Firmware mirror** | `cc_op_entry` (`ncfw_log_spad_ctrl_cc_op_entry` @`libncfw.so:0x1840`), `algo_type[3:0]` selects engine family |
| **Two-sided agreement** | userspace authors op word ↔ firmware `cc_op_entry` schedules / reflects it |

---

## 1. The Base-Plus-Overlay Model

### Purpose

`meta_ctrl` is a single 32-bit control word with one job on the plain-copy path (carry the CME-class tag, the CRC/endian controls, and the write-barrier) and a *second* job on the TPB-compute path (carry an op-class selector plus its operands). The two jobs share the same 32 bits because the SDMA engine is a *copy* engine with optional inline compute: every compute op is still a DMA, so it reuses the copy descriptor's word and overlays its op selector onto the otherwise-`0` selector region of the default.

The selector region is two adjacent fields named in the kernel authoring overlay (`union tdma_m2s_meta_ctrl`, owned by descriptor-format §3):

```text
meta_ctrl[22:20]  op_type    (3 bits)   ─┐  together: the per-engine op-class
meta_ctrl[25:23]  ssmae_op   (3 bits)   ─┘  selector. Default = op_type=0, ssmae_op=0b010.
```

On the plain copy, `op_type = 0` and `ssmae_op = 0b010`, so the selector region reads `0x01000000` within the default word `0x01080003`. The compute builders below do not *clear and re-pack* these two named fields individually; each builder ORs a single precomputed **top opcode word** (e.g. `0x2400000` for `cce_ext`) that lands its op code across bits `[26:20]` — i.e. it writes the `op_type` + `ssmae_op` region *and* one bit above it as a unit. This is why the table below lists whole `0x?00000`-shaped constants, not per-field values: that is literally how the binary encodes them (`movzwl … ; or $0x2400000,%eax`).

> **NOTE —** descriptor-format owns the *base* word: the two C overlays, the `0x01080003` default, `write_barrier` at bit 26, `notification` at bit 28, and `is_cme_desc == (ssmae_op==2)`. This page extends that with the op-class *values* of the selector region for the non-default ops. If a constant here conflicts with a base bit there, descriptor-format wins — but they do not conflict, because the compute ops are authored only on a path the plain-copy default never touches.

### Entry Point

The compute-op `meta_ctrl` is built leaf-style: a packet builder in another cell calls the per-op `_meta_ctrl` encoder, then the per-op `_descriptor` builder packs the full 16 bytes and `al_copy_descriptor`s (= `memcpy`) it into the ring slot.

```text
al_sdma_m2m_build_<op>_packet            ── packet orchestrator [other cell]
  ├─ al_sdma_m2s_build_<op>_meta_ctrl     ── 0x451980..0x451dc0  ── packs the op word1
  ├─ al_sdma_m2s_build_<op>_descriptor    ── packs word0..word3, then…
  └─ al_copy_descriptor (0x265980=memcpy) ── 16-byte publish into ring slot
```

The CCE / collective path reaches the same builders through the combo-op funnel:

```text
aws_cayman_sdma_m2m_build_combo_op        ── arch combo path [other cell]
  └─ al_sdma_m2s_build_{add,fma,gradient,min_max,replication,seed_init}_*  ── 0x451b80..0x451de0
al_sdma_m2m_build_op_extension_packet      ── the CCE extension packet
  └─ al_sdma_m2s_build_cce_ext_meta_ctrl   ── 0x451dc0  ── base 0x2400000
```

---

## 2. The Meta-Control Overlay Dimension Table

The selector region is a small space, so the table below is the **whole** op-class enumeration the M2M HAL emits — not a sample. Each row is one overlay on the one `meta_ctrl` word, grouped by the engine family that owns it. The "base word" is the precomputed `0x?00000` constant the builder ORs; "interior" lists the operand nibbles it packs below the selector. All op-word constants are **HIGH** (read from disasm and IDA, spot-verified byte-for-byte on `replication 0xA00000` / `transpose 0x900000` / `cce_ext 0x2400000`); the per-argument *names* are **LOW** (no DWARF parameter names — argument positions are certain, their human labels inferred from the packet-builder input structs `kbin_dma_desc_*_info_t`).

| engine | op-class | builder @addr | base word | interior fields (bit-packed) | default | Conf |
|---|---|---|---|---|---|---|
| **SDMA** | plain copy / CME | (default seed) | `0x01000000` region | `is_first=is_last=1`, `copy_source_data=1`, `ssmae_op=0b010` | **`0x01080003`** | HIGH |
| **SDMA** | striding-split | `0x451ae0` | `0x800000` | `[2:0]` split-count · `bit5` (a3>1) · `[15:8]` shape-idx · `[19:16]` dtype | — | HIGH |
| **SDMA** | transpose | `0x451980` | `0x900000` | `[4:0]` elem-bytes · `bit5` (shape>1) · `[15:8]` out-shape-idx · `[19:16]` byte-cnt | — | HIGH |
| **SDMA** | replication | `0x451b00` | `0xA00000` | `[15:0]` replication count; ext: `(a4<<29)\|0x4A00000` | — | HIGH |
| **CCE** | add | `0x451b80` | `0x2000000` | `[2:0]` op · `bit5` (a4) · `bit6`/`bit7` flags · `[15:12]`/`[19:16]` dtypes | — | HIGH |
| **CCE** | min/max | `0x451ca0` | `0x2000000`*+`[22:20]` | shares add base; **disambiguated** by `[22:20]` const-dtype range + `bit5` | — | HIGH |
| **CCE** | fma | `0x451bd0` | `0x2100000` | `[2:0]` op · `[7:6]` flags · `[11:8]`/`[15:12]`/`[19:16]` dtypes | — | HIGH |
| **CCE** | cce_ext | `0x451dc0` | `0x2400000` | `[2:0]` sub-op · `[15:12]` dtype · `[19:16]` dtype | — | HIGH |
| **CCE** | gradient | `0x451d70` | `0x2500000` | `[2:0]` op · `[7:6]` flags · `[15:8]`/`[19:16]` dtypes | — | HIGH |
| **TDG** | seed-init (desc-hi) | `0x451e40` | `0x6000000` | RNG seed-init; HIDWORD class on the descriptor, not meta | — | HIGH |
| **TDG** | gradient (desc-hi) | `0x451de0` | `0x6500001` | gradient descriptor high-class (`(a6<<29)\|0x6500001`) | — | HIGH |
| **TDG** | replication (ext) | `0x451b10` | `0x4A00000` | extra-replication high-class (`(a4<<29)\|0x4A00000`) | — | HIGH |

> **GOTCHA —** `add` and `min_max` **share** base word `0x2000000`. They are NOT distinguished by the top opcode — a reimplementation that switches only on the high bits will execute a min/max descriptor as an add. The discriminators are interior: `min_max` carries its constant-dtype in `[22:20]` and gates on `bit5` (the "no-constant" flag that zeroes the const-dtype), while `add` leaves `[22:20]=0`. The builders are separate functions (`0x451b80` add vs `0x451ca0` min_max) precisely so the interior packing differs; the op word alone is ambiguous.

> **QUIRK —** the `0x6…` words (seed-init `0x6000000`, gradient `0x6500001`, replication-ext `0x4A00000`) are **descriptor-high-word** classes, not `meta_ctrl` overlays — the `_descriptor` builders OR them into the descriptor's *upper* dwords (word2/word3 region, e.g. `HIDWORD=0x6000000`), gated by a "constant present" / "extra count" argument. They are listed here because a reimplementer scanning for the op constants will find them in the same builders, but they live outside the `meta_ctrl` word. The `meta_ctrl` overlay proper is the `0x?00000` ≤ `0x2500000` set.

### The engine-family split

The three families partition the op space by *who drives the op*, not by a separate hardware unit:

- **SDMA** — data-movement ops the runtime issues directly (plain copy, transpose, replication, striding-split). The plain copy is the only one with a wire-verified default; the rest are authored by the TPB-compute path.
- **CCE (Collective-Compute Engine)** — the reduce/compute ops the *collective* scheduler drives during all-reduce / all-gather: `add`, `fma`, `min_max`, `gradient`, and the `cce_ext` extension. These ride the `0x2…00000` family. The name comes from the builder `al_sdma_m2s_build_cce_ext_meta_ctrl` and the input struct `kbin_dma_desc_cce_info_t` (140 B: `num_sources`, `<sources>`, `num_dests`).
- **TDG (descriptor generator)** — the op classes the *firmware* schedules and reflects, plus the RNG seed-init / high-class descriptor words. The on-device reflection of these is `cc_op_entry.algo_type` (§4).

---

## 3. The Overlay Encode Path

The encode path is the same shape for every op: compute element/operand sizes, pack the interior nibbles, OR the base op word, store to the stack descriptor, `memcpy` 16 bytes. The pseudocode below models the CCE-extension and the plain-copy paths together (the two the collective stack actually exercises), and shows the `is_cme_desc` predicate the firmware/HAL agree on.

```c
// al_sdma_m2s_build_cce_ext_meta_ctrl — 0x451dc0  (the CCE reduce/compute overlay)
// args (positions HIGH, names inferred LOW): a1=dst_dtype, a2=src_dtype, a3=sub_op
function build_cce_ext_meta_ctrl(dst_dtype, src_dtype, sub_op) -> u32:
    meta  =  sub_op   & 0x7                       // [2:0]   CCE sub-op selector
    meta |= (src_dtype & 0xF) << 12               // [15:12] source operand dtype
    meta |= (dst_dtype & 0xF) << 16               // [19:16] dest operand dtype
    meta |=  0x2400000                            // base CCE op word (op_type/ssmae_op region)
    return meta                                   // op_type/ssmae_op now select CCE-class

// al_tdma_m2s_meta_ctrl_default_value — .rodata @0x9e8828, bytes "03 00 08 01"
// The plain-copy seed: NO builder runs; the orchestrator copies this constant.
const META_CTRL_DEFAULT = 0x01080003             // is_first|is_last|copy_source_data|(0b010<<23)

// is_cme_desc — 0x45ceb0  (the class tag both userspace and firmware key on)
function is_cme_desc(meta_ctrl_ptr) -> bool:
    op = (load_u16(meta_ctrl_ptr + 2) >> 7) & 7  // ssmae_op = bits [25:23] read via +2 halfword
    return op == 2                               // 0b010 == the DEFAULT ssmae_op == CME class

// The m2s build orchestrator applying the overlay (modeled on the *_packet path).
// On the plain copy it seeds the default; on a compute op it overwrites meta with the op word.
function build_m2s_meta(op_class, operands) -> u32:
    if op_class == PLAIN_COPY:
        return META_CTRL_DEFAULT                  // 0x01080003 ; ssmae_op=0b010 ; is_cme_desc=1
    switch op_class:                              // each replaces the selector region
        case TRANSPOSE:      return build_transpose_meta_ctrl(operands)   // 0x451980, |0x900000
        case REPLICATION:    return build_replication_meta_ctrl(operands) // 0x451b00, |0xA00000
        case STRIDING_SPLIT: return build_striding_split_meta_ctrl(...)   // 0x451ae0, |0x800000
        case ADD:            return build_add_meta_ctrl(operands)         // 0x451b80, |0x2000000
        case MIN_MAX:        return build_min_max_meta_ctrl(operands)     // 0x451ca0, |0x2000000 + [22:20]
        case FMA:            return build_fma_meta_ctrl(operands)         // 0x451bd0, |0x2100000
        case CCE_EXT:        return build_cce_ext_meta_ctrl(operands)     // 0x451dc0, |0x2400000
        case GRADIENT:       return build_gradient_meta_ctrl(operands)    // 0x451d70, |0x2500000

// The write-barrier overlay is ORTHOGONAL to the op class — owned by descriptor-format §3.
// al_sdma_m2s_set_write_barrier — 0x451670 : orb $0x4,0x3(%rdi)  → meta_ctrl bit (24+2)=26.
// It is applied AFTER the op word, on the same word, independent of the op class.
```

> **NOTE —** the plain-copy path never calls any `build_*_meta_ctrl`: the M2M HAL marks `meta_ctrl` *"currently unmodifiable, use the same value for all descriptors"* (descriptor-format §3) and copies the `0x01080003` constant verbatim. The op-class overlays are reached **only** through the compute / collective packet builders. So on a pure copy ring, every `meta_ctrl` is byte-identical to the default, and `is_cme_desc` is `1` for all of them — the CME tag is the *resting* state, not a special case.

---

## 4. Userspace ↔ Firmware Agreement

The op selector is authored on the userspace side (the descriptor the SDMA engine executes) and reflected on the firmware side (the on-device CC-context the collective scheduler walks). The two sides are independent code lines in two separate shared objects, so their agreement is the contract a reimplementer must preserve.

| concern | userspace (`libnrt.so`) | firmware (`libncfw.so`) | agree |
|---|---|---|---|
| op class lives in | `meta_ctrl[25:20]` selector region | `cc_op_entry+0 [3:0] algo_type` + `[6:4] algo_sub_type` | **structurally** |
| plain-copy / CME tag | `ssmae_op == 0b010` (`is_cme_desc` @`0x45ceb0`) | `algo_type` selects `{ring,mesh,hierarchical,kangaring}` | both default-keyed |
| selector authored by | `al_sdma_m2s_build_*_meta_ctrl` @`0x451980+` | (consumed, not authored — schedule view) | author→consume |
| selector reflected by | (built, not dumped) | `ncfw_log_spad_ctrl_cc_op_entry` @`libncfw.so:0x1840` | build→reflect |
| sub-op / variant | interior `[2:0]` sub-op + dtype nibbles | `cc_op_entry algo_sub_type [6:4]` (3-bit) | **3-bit each** |
| collective DMA channel | descriptor on the m2s/s2m ring | `dma_channel_apb_bcast` m2s/s2m tail ptr (`@libncfw.so:0x4899`) | same rings |

The agreement is **structural, not bit-identical**: the userspace `meta_ctrl[25:20]` selector and the firmware `cc_op_entry.algo_type[3:0] / algo_sub_type[6:4]` are *different encodings of the same op-class taxonomy* in two different on-wire structures. They are not the same bits — userspace packs the op into a DMA descriptor; firmware packs the scheduling decision into a 2-byte `cc_op_entry` header that the Xtensa sync core reads. What they share is the **enumeration**: a 4-bit `algo_type` + 3-bit `algo_sub_type` on the firmware side that names the same families (`ring`, `mesh`, `hierarchical`, `kangaring`) the userspace builders target, and a matching 3-bit sub-op width on both sides.

> **GOTCHA —** do not read the userspace `meta_ctrl` op word and the firmware `cc_op_entry.algo_type` as the *same integer*. They are a producer/consumer pair across two binaries, not a shared field. The userspace `0x2400000` `cce_ext` op word and the firmware `algo_type` value that schedules a collective-compute op are two encodings the *toolchain* keeps consistent — the agreement is a build-time invariant of the NEFF, not a runtime bit-equality. A reimplementer must reproduce *both* encodings and the mapping between them, not assume one number flows through unchanged.

> **QUIRK —** the firmware never authors a descriptor. `libncfw.so`'s entire job for this contract is to *reflect* the on-device CC-context as JSON (`ncfw_log_spad_ctrl_cc_op_entry`, keys `algo_type` @`0x6502a`, `algo_sub_type` @`0x65039`, `trigger_next` @`0x65049`). The actual op-class → behavior mapping executes in the Xtensa IRAM image, outside both `libnrt.so` and `libncfw.so` — so the *numeric* `algo_type` enum is opaque here (the value→name mapping lives in firmware IRAM, **LOW** confidence on exact codes). What is HIGH is the *structure*: a 4-bit family selector + 3-bit sub-variant, mirrored on both sides, ride the same SDMA rings.

> **CORRECTION (DMA-DESC) —** descriptor-format §9 originally tagged the full `ssmae_op`/`op_type` op-class enumeration as *"not grounded by this path (LOW confidence)"*. This page grounds it: the eight `al_sdma_m2s_build_*_meta_ctrl` builders (`0x451980`–`0x451dc0`) emit the op words verbatim (HIGH for the constants). The residual LOW is now narrowed to (i) the *human name* of each interior operand argument (no DWARF params), and (ii) the firmware-side *numeric* `algo_type` codes (in Xtensa IRAM). The op-class base words themselves are byte-confirmed.

---

## 5. Considerations

- **The op word is mutually exclusive with the CME default, by construction.** A compute op overwrites `ssmae_op` away from `0b010`, so `is_cme_desc` returns `false` for it. This is the intended discriminator: a descriptor is "CME-class" iff it is a plain copy. A reimplementer must not set both an op word and expect the CME tag to survive — the op word *is* the not-CME state.
- **`min_max` vs `add` collision** — the single most dangerous reimplementation trap on this page; see §2 GOTCHA. The op word does not disambiguate; the interior `[22:20]` + `bit5` do.
- **The CME/CRC meta-descriptor word0** (`(idx<<24)|0x04800000`, `FIRST|META`) is a *third* descriptor's `len_ctrl`, not a `meta_ctrl` overlay — it is owned by descriptor-format §3. Do not confuse the `0x04800000` len_ctrl meta-marker with any `meta_ctrl` op word.
- **The CME meta-descriptor payload** (`meta1`/`meta2` at the `tx_meta` view offsets `0x8`/`0xC`) is exercised by `al_udma_m2m_build_cme_packet` @`0x45d400` and the CAYMAN transpose builder — *not decoded on this page*. The op *selector* is here; the op *payload* (dims, strides, scale tables) lives in word2/word3 and is per-op (e.g. `kbin_dma_desc_fma_info_t` carries a 32-float scale array). **(MED — payload offsets per builder, not byte-diffed here.)**
- **Firmware `algo_type` numeric codes** are in the Xtensa IRAM image and not present in `libncfw.so`'s x86 serializer — only the *width* (4-bit type + 3-bit sub-type) and the family *names* are recovered. **(LOW for exact codes.)**
- **Per-arch invariance** — like the base descriptor, the op-word encoding is arch-invariant: the `0x451980`-band builders are leaf encoders with no arch branch; per-arch differences are which *combo* path reaches them (`aws_cayman_sdma_m2m_build_combo_op`), not the op words. **(HIGH — leaf builders carry no `al_hal_tpb_get_arch_type` call.)**

---

## Related Components

| Name | Relationship |
|---|---|
| `al_sdma_m2s_build_*_meta_ctrl` (`0x451980`–`0x451dc0`) | The eight leaf encoders that author each op-class overlay |
| `al_tdma_m2s_meta_ctrl_default_value` (`0x9e8828`) | The plain-copy default `0x01080003` the overlays replace |
| `is_cme_desc` (`0x45ceb0`) | The CME-class predicate (`ssmae_op==0b010`) that the overlays clear |
| `al_sdma_m2s_set_write_barrier` (`0x451670`) | The orthogonal bit-26 overlay (owned by descriptor-format) |
| `ncfw_log_spad_ctrl_cc_op_entry` (`libncfw.so:0x1840`) | The firmware reflection of the op class (`algo_type`/`algo_sub_type`) |
| `aws_cayman_sdma_m2m_build_combo_op` | The arch combo path that funnels into the compute builders |

## Cross-References

- [The 16-Byte Descriptor Wire Format](descriptor-format.md) — **owns** the base `meta_ctrl` word: the two C overlays, the `0x01080003` default, `write_barrier` bit 26, `is_cme_desc`. This page overlays the op-class selector onto it
- [The Ring / Trigger / Doorbell / Completion Cycle](ring-cycle.md) — how a descriptor carrying one of these op words is launched and its completion harvested
- [The cc_op_entry On-Device ISA](../collectives/cc-op-isa.md) — the firmware-side `cc_op_entry` op descriptor (`algo_type`/`algo_sub_type`) the userspace op selector agrees with
- [Serializer Families (per-arch CC-context dumpers)](../firmware/serializer-families.md) — the `libncfw.so` serializer tree that reflects the scheduling decisions this page's op words drive
- [back to index](../index.md)
