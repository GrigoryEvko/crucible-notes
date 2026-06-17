# Per-Arch Device Layer: SDMA / UDMA M2M Engine

> *All addresses, offsets, symbol names, register CSR offsets, and per-arch return constants on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF (names; line info absent on the `al_hal_sdma.c` / `aws_hal_arch_regs.c` TUs — `addr2line` returns `:?` for those bands, so every claim below is `nm`/`objdump`/IDA-decompile-anchored, not line-resolved). All four `PT_LOAD` segments are identity-mapped, so `.text`/`.rodata` are VMA == file offset; every `0x44…`/`0x45…`/`0x46…`/`0x9e…` is an analysis VMA. The vendored HAL package is `KaenaHal-2.31.0.0` (Amazon `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`); the per-arch SDMA TUs root in `.../KaenaHal-2.31.0.0/.../src/src/{common,mariana}/sdma/al_hal_sdma{,_m2m}.c` + `.../src/common/arch/aws_hal_arch_regs.c` + `.../src/mariana/sdma/aws_hal_mariana_sdma.c`, with register magics from `{Cayman,Sunda,Mariana}ArchHeaders/.../arch-headers/<arch>/{tdma,udma_m2s,tpb_nx,cce}.h`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** for every leaf symbol+address, the arch-dispatch enum, the CSR field bases/strides, and the m2s descriptor opcode words; **Inferred / LOW** for the human-readable name of each interior descriptor operand argument (no DWARF parameter names on the `al_hal_sdma.c` TU). · Part IV — Userspace Runtime Core, DEEP · [back to index](../index.md)*

## Abstract

This page owns the **per-arch SDMA engine band** of the KaenaHal device layer — the silicon-specific bodies that program the AnnapurnaLabs UDMA *Stream-DMA* engine for compute-while-copy and notification, one set per generation (**SUNDA** = arch 2 / Trn1-class, **CAYMAN** = arch 3 / Trn2, **MARIANA** = arch 4 / Trn3-mesh). Where the [16-Byte UDMA Descriptor](../dma/descriptor-format.md) owns the wire format and [SDMA / CCE / TDG Meta-Control Overlays](../dma/meta-ctrl-overlays.md) owns the `meta_ctrl` op-class *encoding*, this page owns the **engine band that feeds those words to the silicon**: the CAYMAN SDMA M2M State-Buffer transpose builder, the TPB DGE/XT memcopy-config accessors, the TDMA-model notification-CSR write helpers (fabric-trigger / per-queue / AXI-req / APB-req), the UDMA-M2S prefetch dual-tail-pointer enable, the Xtensa control-core (CC-queue) bring-up registers, the embedded-semaphore region init, and the MARIANA-only Q7/RDM/seed/FP8/QoS slice. It is the band the descriptor builders write *through* and the notification path writes *into*.

The band's organizing principle is **arch dispatch over one CSR geometry**. Almost every accessor begins with `al_hal_tpb_get_arch_type()` (`@0x44bca0`, reading the global `hal_target` enum, asserts `!= INVALID` and `< NUM=5`) and then branches on `2 → SUNDA / 3 → CAYMAN / 4 → MARIANA` to pick a per-arch register stride, base offset, or feature gate. A second dispatch mechanism rides the same band: the *embedded-semaphore* and *m2m-queue* helpers do not branch inline — they tail-call through the global `kaena_khal` vtable (`.bss @0xCAEB80`), whose SDMA slots (`+0x510…+0x568` for `khal_sdma`, `+0x6F0/+0x6F8` for `khal_udma`) are filled at init by `kaena_khal_register_funcs_v{2,3,4}` (`@0x468740`/`@0x46ED70`/`@0x4622E0`). The al_sdma m2s/m2m descriptor builders sit at the bottom of this stack: leaf bit-packers (`al_sdma_m2s_build_*_meta_ctrl` / `_descriptor`, `0x451690`–`0x451e40`) that the per-arch combo path funnels into, and arch-dispatch trampolines (`aws_hal_sdma_*`, `0x451e90`–`0x4520f0`) that route embedded-semaphore writes into the matching `*_mariana` impl.

The per-arch story divides three ways. A first class of features is **arch-invariant**: the per-queue notification CSR strides (24-byte, bases `0x200`/`0x208`/`0x20c`/`0x214`) and the m2s descriptor opcode words are the same on all three. A second class is a **two-vs-one split**: the TDMA fabric-trigger CSR stride is 48 bytes on SUNDA/CAYMAN but 52 bytes on MARIANA; the UDMA-M2S prefetch and Xtensa-XT-local register writes are gated `arch_type > 2` (CAYMAN+MARIANA only) and silently no-op on SUNDA. A third class is **MARIANA-only**: the entire Q7 pooling-ucode loader, the retired RDM path (three `0xFFFFFFFF` stubs), the CCE stochastic-rounding seed registers, the FP8 data-converter defaults, the UDMA QoS gate, the B0 silicon-feature enablement, and the 32-region embedded-semaphore facility whose AXI base carries the `+0x800000000000` two-die-mesh high bit (`0x802701800`). These divergences, the descriptor build, and the CSR matrix are §1–§5.

For reimplementation, the contract is:

- **The arch-dispatch CSR matrix** — for each SDMA CSR family (per-queue notification, fabric-trigger AXI/APB, M2S prefetch, CC-queue, XT-local, embedded-sem), the field base, the per-arch stride, the index bound, and which generation gates the feature off. The leaf computes `base + arch_stride*idx + field_off` and bottoms into `al_reg_write32`/`al_reg_read32`.
- **The CAYMAN M2M transpose builder** — the dtype-size LUT, the eight 32-MiB SBUF base windows, the partition/offset bit-pack into the to-descriptor, and the `EINVAL` rejection of any non-SBUF destination. CAYMAN-only.
- **The al_sdma m2s descriptor build + meta_ctrl handoff** — how `al_sdma_m2s_build_<op>_descriptor` packs word0..word3, calls the matching `_meta_ctrl` encoder (whose op-word encoding is owned by [meta-ctrl-overlays](../dma/meta-ctrl-overlays.md)), and `al_copy_descriptor`s 16 bytes into the ring slot.
- **The MARIANA-only band** — the Q7 pooling-ucode loader, the seed/FP8/QoS/B0 CSR programs, the 32-region embedded-semaphore model (8 TPB + 16 TopSP populated, the mesh-high-bit AXI base), and the S2M embedded-sem descriptor field-pack.

| | |
|---|---|
| **What this page owns** | the per-arch SDMA engine leaves — transpose / DGE-XT / notification-CSR / M2S-prefetch / CC-queue / embedded-sem / MARIANA Q7-RDM-seed-FP8-QoS — and the al_sdma m2s descriptor build that feeds the [descriptor](../dma/descriptor-format.md) |
| **Arch enum** | `SUNDA=2` (Trn1-class) · `CAYMAN=3` (Trn2 / V3) · `MARIANA=4` (Trn3-mesh / V4); gate `al_hal_tpb_get_arch_type @0x44bca0` (`hal_target`, asserts `<NUM=5`) |
| **CAYMAN transpose band** | `.text 0x44b0e0..0x44b7ff` (`aws_cayman_sdma_m2m_build_transpose_packet @0x44b0e0`) |
| **TDMA-model notific band** | `.text 0x44da50..0x44fbf8` (`aws_reg_write_tdma_model_notific_cfg_*` + UDMA-M2S + CC-queue + embedded-sem thunks) |
| **al_sdma m2s builders** | `al_sdma_m2s_build_{transpose,replication,striding_split,add,fma,min_max,gradient,cce_ext,seed_init}_*` `@0x451690`–`0x451e40` (op-word encoding → [meta-ctrl-overlays](../dma/meta-ctrl-overlays.md)) |
| **arch-dispatch trampolines** | `aws_hal_sdma_{init_embedded_sem,embedded_sem_csr_index,_wr_address,_read_address,read_dma_csrs,s2m_set_embedded_sem_fields}` `@0x451e90`–`0x4520f0` (→ `kaena_khal` `+0x528…+0x550`) |
| **MARIANA SDMA band** | `.text 0x464ab0..0x465656` (`aws_hal_mariana_sdma_*` + Q7/RDM stubs + seed/FP8/QoS/B0) |
| **MARIANA registrar** | `kaena_khal_register_funcs_v4 @0x4622e0` installs 17 of the 24 MARIANA SDMA leaves into `kaena_khal.khal_arch` |
| **Mesh AXI base** | `0x802701800` = `0x2701800 \| 0x800000000000` (two-die-mesh high bit; `aws_hal_mariana_sdma_embedded_sem_csr_index @0x465490`) |
| **CCE user offset** | `0x2000` (`aws_hal_get_sdma_cce_user_offset_mariana @0x477450`); SUNDA/CAYMAN return `0` (feature absent — [arch-csr-offsets §3](arch-csr-offsets.md)) |
| **dtype-size LUT** | `data_type_sizes` `.rodata @0x9e30e0`/`@0x9e7ea0`/`@0x9e86e0`: `{0,8,1,1,2,2,2,2,4,4,4,4,8,1,1,1}` (idx 0 & >15 rejected) |

> **CORRECTION (ARCH-SDMA-01) —** the `al_hal_sdma.c` TU carries `__FUNCTION__` / assert strings that *order* the arch enum as `{INVALID, CAYMAN, MARIANA, SUNDA, NUM}` — a different numbering than the runtime's. The authoritative dispatch value is the one the per-arch `aws_reg_*` bodies actually `cmp` against and the registrars install: **`SUNDA=2, CAYMAN=3, MARIANA=4`** (cross-validated three ways — the `arch_type > 2` M2S gate that means "CAYMAN+MARIANA", the `(arch-3) > 1` XT-local gate that means the same, and the `kaena_khal_register_funcs_v{2,3,4}` → `{sunda,cayman,mariana}` mapping; consistent with [arch-geometry](arch-geometry.md) and [arch-csr-offsets](arch-csr-offsets.md)). The `al_hal_sdma.c` string ordering is a *source-enum-declaration* artifact, not the live `hal_target` value — do not drive a reimplementation off the string-order enum.

---

## 1. The Arch-Dispatch Shape and Engine Band

### Purpose

Every register touch in this band resolves through one of two dispatch mechanisms, and a reimplementer must know which one a given accessor uses before reading its body. The first is **inline arch dispatch**: the leaf reads `al_hal_tpb_get_arch_type()` and selects a per-arch stride/base/gate with a `switch` or a comparison, then performs the MMIO directly. The second is **vtable dispatch**: the leaf is a thin trampoline that asserts the arch is latched, loads a function pointer from the global `kaena_khal` struct, and tail-calls it — the per-arch body lives in a separate `*_mariana` / `*_cayman` / sunda leaf. The notification-CSR and prefetch families use inline dispatch; the embedded-semaphore and m2m-queue families use vtable dispatch. Understanding the split once lets a reader navigate the four source cells by reflex.

### Entry Point

The band is reached from three directions — the notification orchestrators, the descriptor build path, and the bring-up sequence — all bottoming into the per-arch CSR leaves or the `kaena_khal` SDMA slots:

```text
aws_hal_sdma_notification_axi_en / _apb_en        ── notification orchestrators [other cell]
  └─ aws_reg_write_tdma_model_notific_cfg_*        ── 0x44de30..0x44f66b  (inline arch dispatch)
       └─ al_hal_tpb_get_arch_type (0x44bca0)       ── 2/3/4 → per-arch stride
       └─ al_reg_write32 (0x265c50)                 ── → csr_write (tdrv/csr.c, BAR 0)

al_sdma_m2m_build_<op>_packet                       ── descriptor orchestrator [L-HAL-09]
  ├─ al_sdma_m2s_build_<op>_meta_ctrl (0x451980..)  ── op word  → meta-ctrl-overlays
  ├─ al_sdma_m2s_build_<op>_descriptor (0x4519a0..) ── pack word0..3 + al_copy_descriptor
  └─ unk_CAF200 al_mla_udma_m2m_build_packet         ── khal_udma slot, arch impl

tdrv_init / aws_hal_stpb_init                        ── bring-up [other cell]
  ├─ aws_hal_sdma_init_embedded_sem (0x451e90)       ── trampoline → kaena_khal+0x528
  │    └─ aws_hal_mariana_sdma_init_embedded_sem (0x465330)  ── 8 TPB + 16 TopSP regions
  ├─ aws_hal_mariana_sdma_set_fp8_defaults (0x464f70)
  ├─ aws_hal_mariana_sdma_set_udma_qos (0x4650c0)
  └─ aws_hal_mariana_sdma_enable_b0_features (0x465210)  ── arch_type==4 guard
```

### Algorithm

The canonical inline-dispatch CSR write — a per-queue notification field — shows the shape every Group-A/B accessor shares: arch-type read, index bound-check (assert-on-OOB → `al_abort_program`), per-arch stride select, absolute-VA compute, tail into `al_reg_write32`:

```c
// aws_reg_write_tdma_model_notific_cfg_per_queue_m2s_sw_queue — 0x44de30
//   per-queue notification target SW-queue CSR (arch-invariant 24-byte stride)
function write_notific_per_queue_m2s_sw_queue(base, idx, val):
    arch = al_hal_tpb_get_arch_type()                 // 0x44bca0 ; asserts arch != INVALID
    if idx >= 16:                                     // AWS_REG_<ARCH>_..._PER_QUEUE_COUNT = 16
        al_abort_program(-1)                          // "index < ..._PER_QUEUE_COUNT" assert
    // all three archs: stride 24, base 520 for m2s_sw_queue
    al_reg_write32(base + 24*idx + 520, val)          // → csr_write (BAR 0)

// aws_reg_write_tdma_model_notific_cfg_fabric_trigger_axi_req_sw_queue — 0x44e470
//   fabric-trigger AXI-req: stride DIVERGES per arch (the one notification stride that differs)
function write_fabric_trigger_axi_req_sw_queue(base, idx, val):
    arch = al_hal_tpb_get_arch_type()
    if idx >= 2:                                      // ..._FABRIC_TRIGGER_COUNT = 2
        al_abort_program(-1)
    stride = (arch == MARIANA) ? 52 : 48              // SUNDA/CAYMAN 0x30 ; MARIANA 0x34
    al_reg_write32(base + stride*idx + 1292, val)     // field base 0x50c (axi_req_sw_queue)
```

The canonical vtable-dispatch trampoline — every embedded-semaphore accessor — asserts the slot is populated and tail-calls it; only the MARIANA registrar fills the SDMA slots, so on SUNDA/CAYMAN these abort if reached:

```c
// aws_hal_sdma_init_embedded_sem — 0x451e90  (al_hal_sdma.c)
function init_embedded_sem(udma):
    assert(al_hal_tpb_get_arch_type() != INVALID)     // al_hal_sdma.c assert :0x28D/E
    fp = kaena_khal.khal_sdma.init_embedded_sem        // kaena_khal + 0x528 (CAF0A8)
    assert(fp != NULL)                                 // "kaena_khal.khal_sdma.init_embedded_sem"
    return fp(udma)                                    // tail-call → aws_hal_mariana_sdma_init_embedded_sem
```

### Function Map

The dispatch-mechanism roster — which accessors are inline-arch versus vtable-trampoline, and where the per-arch body lives.

| Function | Addr | Dispatch | Per-arch body | Confidence |
|---|---|---|---|---|
| `al_hal_tpb_get_arch_type` | `0x44bca0` | (the gate) | reads `hal_target`; asserts `<NUM=5` | CERTAIN |
| `aws_reg_write_tdma_model_notific_cfg_per_queue_*` | `0x44de30`–`0x44e3xx` | inline (stride 24, invariant) | one merged impl | HIGH |
| `aws_reg_write_tdma_model_notific_cfg_fabric_trigger_*` | `0x44e470`–`0x44f66b` | inline (stride 48/48/**52**) | merged, per-arch stride | HIGH |
| `aws_reg_{fetch,update}_udma_m2s_..._prefetch_*` | `0x44f670`–`0x44f81f` | inline (gated `arch>2`) | CAYMAN+MARIANA; SUNDA no-op | HIGH |
| `aws_reg_write_xt_cc_queue_*` / `xt_local_reg` | `0x44f820`–`0x44fb7d` | inline (skip on SUNDA) | CAYMAN+MARIANA | HIGH |
| `aws_reg_{write,read}_embedded_sem*` | `0x44fb80`/`0x44fbc0` | vtable (`kaena_khal+0x1A8/0x1B0`) | `*_mariana` only | HIGH |
| `aws_hal_sdma_{init_embedded_sem,…,s2m_set_embedded_sem_fields}` | `0x451e90`–`0x4520f0` | vtable (`kaena_khal+0x528…+0x550`) | `*_mariana` (v4 only) | HIGH |
| `al_sdma_m2m_get_queues` / `get_next_desc` | `0x4521b0`/`0x452170` | vtable (`khal_udma`) | per-arch queue accessors | HIGH |

### Considerations

> **NOTE —** the inline-dispatch leaves are a *single* merged function per field that branches on arch at runtime — the `__FUNCTION__` strings reveal the generator emitted one impl per `(arch × field)` (`aws_reg_write_cayman_…`, `…mariana_…`, `…sunda_…` all present in `.rodata`) and the linker folded them into one dispatch body. A reimplementer can either reproduce the merged-dispatch shape or emit three per-arch functions; the wire effect is identical. The strings are name-only artifacts — do **not** cite `aws_reg_write_cayman_…` as a code address; it is an inlined/folded name with no distinct entry point.

> **GOTCHA —** the vtable trampolines abort on SUNDA/CAYMAN, not return an error. The SDMA embedded-semaphore slots (`kaena_khal+0x528…+0x550`) are populated **only** by `kaena_khal_register_funcs_v4` (MARIANA); v2/v3 leave them NULL. A SUNDA/CAYMAN caller that reaches `aws_hal_sdma_init_embedded_sem` `__assert_fail`s on the NULL-slot check — the embedded-semaphore facility is a hard MARIANA-only capability, gated at the slot, not by a soft `arch_type` branch. The orchestrators (`tdrv_init`) only call it on the MARIANA path, so the abort is unreached in practice.

---

## 2. The CAYMAN SDMA M2M Transpose Builder

### Purpose

The CAYMAN ("v3 arch") SDMA M2M transpose is a single hardware capability with no SUNDA or MARIANA peer in this band: it realises an on-chip State-Buffer transpose by building a UDMA M2M copy whose **destination addresses land in SBUF partition windows**, mapping the transpose as an address-permuted copy. The builder validates dtype and dims, allocates a from-side and a to-side UDMA M2M packet, walks the descriptors, classifies each destination PA into one of eight 32-MiB SBUF base windows, and bit-packs the partition / offset / dtype into the to-descriptor. Any destination *not* in an SBUF window is rejected: *"v3 arch only supports transpose with SB as the destination"* → `EINVAL`.

### Entry Point

```text
vring_add_dma_packet_transpose (0x312ea0)            ── ring packer [other cell]
  └─ aws_cayman_sdma_m2m_transpose_prepare (0x44b730, file-local)
       ├─ al_sdma_m2m_get_queues (0x4521b0)           ── rx/tx queue pair
       └─ aws_cayman_sdma_m2m_build_transpose_packet (0x44b0e0)
            ├─ al_udma_m2m_alloc_dma_packet (0x45ca90) ── ×2 (from + to)
            ├─ al_udma_m2m_build_copy_packet (0x45d550)
            └─ al_udma_m2m_free_dma_packet (0x45cab0)  ── ×2
vring_add_dma_packet_transpose_flush (0x3133a0)
  └─ aws_cayman_sdma_m2m_build_transpose_flush_packet (0x44b670)
       └─ (1-desc flush copy: dst = tpb_base + 0x200000004000000, size field = 4)
```

### Algorithm

The core builder classifies each destination PA into the SBUF window table and packs the SBUF descriptor address. The 18-bit offset / partition split and the dtype-4 high-partition bit are the reimplementation-critical part:

```c
// aws_cayman_sdma_m2m_build_transpose_packet — 0x44b0e0  (CAYMAN-only)
//   args (positions HIGH, names inferred LOW): a2 carries a "partition base" in BYTE6
function build_transpose_packet(.., dtype, cols, rows, dir, a2):
    if (dtype - 1) > 0xE:                             // dtype validated 1..15
        al_hal_log("Invalid dtype %u passed"); return -EINVAL   // 0xFFFFFFEA
    elem = data_type_sizes_3640[dtype]               // .rodata @0x9e30e0, stride 4 (see §5)
    // reject unsupported shapes: W/Z dims, mismatched from/to desc counts or sizes …
    //   "transpose op %u currently unsupported on v3 arch"   @0x820688
    //   "v3 arch does not currently support transposes of different from/to sizes" @0x8207a8
    for each dest PA (v30) in the to-descriptor walk:
        hit = NONE
        for BASE in SBUF_WINDOWS[8]:                  // eight 32-MiB windows (§5)
            if (v30 - BASE) <= 0x1FFFFFF:             // window size 0x2000000 = 32 MiB
                hit = BASE; rel = v30 - BASE; break
        if hit == NONE:
            al_hal_log("v3 arch only supports transpose with SB as the destination")
            return -EINVAL                            // non-SBUF dest rejected
        partition = rel >> 18                          // 256-KiB partition stride
        win_off   = rel & 0x3FFFF                      // 18-bit intra-partition offset
        // to-descriptor bit-pack (field names inferred, MED):
        v51 = ((cols-1) & 0xF) | (16 * ctrl); HIBYTE(v51) = 2 * dir
        v52 = ((win_off & 0x3FFFF) << 8) | ((partition + a2.byte6) << 26)
        v52.lobyte = elem * partition
        if elem == 4:                                  // 4-byte dtype: high-partition rule
            v52.byte4 |= 4
            if partition > 0x3F: v52 += 0x80           // high-partition bit
        to_desc.addr = v52 + hit                       // window base re-added
    emit copy packet via al_udma_m2m_build_copy_packet
```

### Considerations

> **QUIRK —** the destination address is decomposed and *re-composed* against the same window base: `rel = PA - BASE`, then `to_desc.addr = (packed partition/offset) + BASE`. The packing is not a passthrough — it splits the linear SBUF offset into an 18-bit intra-partition offset (`& 0x3FFFF`) and a partition index (`>> 18`, 256-KiB stride) placed at bit 26, with the partition optionally biased by a per-call "partition base" carried in `a2`'s byte 6. A reimplementer who copies the PA verbatim into the descriptor produces a descriptor the engine writes to the wrong SBUF partition. The `elem==4` high-partition `+0x80` rule (partitions above `0x3F`) is the one dtype-conditional bit; it is invisible for 1- and 2-byte dtypes. **(Field names MED — the bit positions are exact from the decompile; the dir/ctrl/high-partition labels are inferred.)**

> **NOTE —** the flush packet (`0x44b670`) is a single-descriptor copy whose destination is `aws_hal_get_tpb_base(eng) + 0x200000004000000` with the descriptor size field forced to `4`. It is a fence/commit write to a TPB aperture that orders the transpose copy against subsequent TPB reads — the magic `0x200000004000000` added to the TPB base selects that flush/fence region. **(Aperture role inferred MED; the constant and size-4 field are HIGH from the decompile.)**

> **NOTE —** the same band carries the CAYMAN DGE/XT memcopy config — `aws_hal_cayman_tpb_dge_configure_dma` (`0x44b880`) and its `_get_register_offsets` twin (`0x44b900`), `_configure_dma_mapping` (`0x44b970`), and `aws_hal_cayman_tpb_xt_configure_feature_flag` (`0x44ba50`, XT local-reg index 38). The engine→XT mapping is via `aws_hal_arch_eng_to_xt` (`0x44c910`, 0..4 valid, 6 = invalid sentinel). The XT-local register *offsets* (`memcopy_dmas +0x13a0`, `_queues +0x13c0`, `_all_queues +0x13e0`) are owned by [arch-csr-offsets §3](arch-csr-offsets.md); this band's `*_get_register_offsets` twins resolve them for the instruction-block (EIB) builder. **(HIGH — symbols nm-confirmed; offsets cross-confirmed against the CSR-offsets matrix.)**

---

## 3. The al_sdma M2S Descriptor Build and the meta_ctrl Handoff

### Purpose

This is the band's tie to the wire format: the `al_sdma_m2s_build_<op>_descriptor` family (`0x4519a0`–`0x451e40`) is the leaf layer that packs the 16-byte UDMA M2S submission descriptor and hands its second word — `meta_ctrl` — to the matching `al_sdma_m2s_build_<op>_meta_ctrl` encoder. The descriptor's *base* layout (the `{len_ctrl, meta_ctrl, buf_ptr}` 16-byte triple, the FIRST/LAST/RING-ID bits) is owned by [descriptor-format](../dma/descriptor-format.md); the `meta_ctrl` *op-class encoding* (the `0x900000`/`0xA00000`/`0x2400000`… op words) is owned by [meta-ctrl-overlays](../dma/meta-ctrl-overlays.md). This page documents the **build step that joins them** — how a packet builder calls the meta-ctrl encoder, packs the descriptor words around it, and publishes 16 bytes into the ring slot — and points the op-word semantics at the overlay page rather than re-deriving them.

### Entry Point

```text
al_sdma_m2m_build_<op>_packet (L-HAL-09)             ── op orchestrator
  ├─ al_sdma_m2m_validate_op (0x452770)               ── dims/dtype/size caps
  ├─ al_sdma_m2s_build_<op>_meta_ctrl (0x451980..)    ── op WORD (→ meta-ctrl-overlays)
  ├─ al_sdma_m2s_build_<op>_descriptor (0x4519a0..)   ── pack word0..3 around meta_ctrl
  │    ├─ data_type_sizes.4241[dtype] (0x9e7ea0)       ── element size (bad dtype → al_hal_log)
  │    ├─ verify_transpose_dimensions (0x451690)       ── transpose-only, stride_map[] limits
  │    └─ al_copy_descriptor (0x265980 = memcpy)       ── 16-byte publish into ring slot
  └─ unk_CAF200 al_mla_udma_m2m_build_packet           ── final UDMA build, arch impl
```

### Algorithm

The transpose descriptor build is the representative case — it is the only `_descriptor` that *also* validates dimensions and looks up element size — and shows the meta_ctrl handoff explicitly. The op word is built by the matching `_meta_ctrl` leaf and ORed into the descriptor's word1; the dims fill word2/word3:

```c
// al_sdma_m2s_build_transpose_descriptor — 0x4519a0  (al_hal_sdma.c, leaf)
//   args (positions HIGH, names inferred LOW): a2=ring/queue byte, dtype, dims{w,x,y,z}, shape_idx
function build_transpose_descriptor(out ring_slot, a2, dtype, dims, shape_idx, elem_dir):
    if (dtype - 1) > 0xE:                              // dtype 1..15 valid
        al_hal_log("Invalid dtype %u passed")          // "dtype_sizeof" tag
        return -EINVAL                                 // 0xFFFFFFEA
    elem = data_type_sizes_4241[dtype]                // .rodata @0x9e7ea0 (§5)
    if verify_transpose_dimensions(elem, dims, shape_idx) != 0:   // 0x451690, stride_map[]
        al_hal_log("invalid matrix dimensions w_dim %u x_dim %u ...")   // @0x823000
        return -EINVAL

    // --- pack the 16-byte M2S descriptor on the stack (src[16]) ---
    desc.word0 = elem_or_len | 0x800000 | (a2 << 24)  // len_ctrl: op-present bit 23 | ring byte<<24
    // word1 = meta_ctrl: the op-class OVERLAY, encoded by the sibling leaf.
    //   The op word (transpose => 0x900000 base) + interior nibbles is OWNED by
    //   meta-ctrl-overlays; this build step only places its return into word1.
    desc.word1 = al_sdma_m2s_build_transpose_meta_ctrl(elem, dtype, shape_idx)  // 0x451980
                 //  == (elem & 0x1F) | (shape_idx>1 ? 0x20 : 0) | (shape_idx<<8)
                 //     | ((dtype & 0xF)<<16) | 0x900000        ← op word, see meta-ctrl-overlays
    desc.word2 = pack_dims_lo(dims.w, dims.x)          // dim fields
    desc.word3 = pack_dims_hi(dims.y, dims.z)

    al_copy_descriptor(ring_slot, &desc, 16)           // 0x265980 = memcpy ; atomic 16-byte publish
    return 0

// The plain-copy path NEVER calls a _meta_ctrl builder: meta_ctrl is the constant
// default 0x01080003 (al_tdma_m2s_meta_ctrl_default_value @0x9e8828). The op-class
// overlay is reached ONLY through the compute / collective packet builders.
```

### Considerations

> **NOTE —** the descriptor build and the meta_ctrl op word are *two functions, one word*. `al_sdma_m2s_build_<op>_descriptor` packs word0/word2/word3 and the `len_ctrl` op-present bit (`0x800000` in word0), then drops the matching `al_sdma_m2s_build_<op>_meta_ctrl` return into word1. The op-word constants (`transpose 0x900000`, `replication 0xA00000`, `add/min_max 0x2000000`, `cce_ext 0x2400000`, …), the `add`/`min_max` shared-base disambiguation, and the `0x6…` descriptor-high-word classes are **owned by [meta-ctrl-overlays](../dma/meta-ctrl-overlays.md)** — reproduce them from there, not from this build step. What this page fixes is the *plumbing*: which builder calls which encoder, that the result lands in word1, and that the 16 bytes are published by `al_copy_descriptor` (= `memcpy`) — not field-by-field into device memory.

> **GOTCHA —** `verify_transpose_dimensions` (`0x451690`) is invoked *only* by the transpose descriptor build, and it is the gate that encodes the hardware transpose constraints via the `stride_map[]` table (`.rodata @0x9e7fc0`): each dim must be nonzero, the dim product must fit `0x4000 >> popcount_class`, and per-shape limits (`<<9`, `>>5`, `>>10`, `0x401`) apply. A reimplementation of the other ops (add/fma/replication) must **not** route through this validator — they carry their own caps in `al_sdma_m2m_validate_op` (`0x452770`). Calling `verify_transpose_dimensions` for a non-transpose op rejects valid descriptors. **(stride_map[] 10-byte layout MED — inferred from the index math, no typed DIE.)**

---

## 4. The MARIANA-Only SDMA Slice

### Purpose

The MARIANA (Trn3-mesh / arch 4) SDMA band carries six capabilities absent on the earlier generations: the Q7 pooling-engine ucode loader, the retired RDM path, the CCE stochastic-rounding seed registers, the FP8 data-converter defaults, the UDMA QoS gate, the B0 silicon-feature enablement, and the 32-region embedded-semaphore facility used for collective/mesh notification. Seventeen of these 24 leaves install into the v4 `khal_arch` ops vtable via `kaena_khal_register_funcs_v4` (`@0x4622e0`); the other seven (seed write/read, FP8 defaults + the two value builders, UDMA QoS, B0 enable) are reached directly from `tdrv_init` / `insert_set_fp8_conv_config` / `set_collectives_dma_queues_props`. The mesh marker is concrete: `aws_hal_mariana_sdma_embedded_sem_csr_index` (`@0x465490`) calls `aws_hal_stpb_get_axi_offset(reg, 0x802701800)`, and `0x802701800 = 0x2701800 | 0x800000000000` — the two-die-mesh high bit on the AXI base.

### Algorithm

The embedded-semaphore region init is the substantive MARIANA-only program — it enables the control config, then programs 24 of 32 evt-sem increment-base regions (8 TPB + 16 TopSP), each via a lo/hi address write through the generic wrapper that re-dispatches into the MARIANA leaf:

```c
// aws_hal_mariana_sdma_init_embedded_sem — 0x465330  (aws_hal_mariana_sdma.c)
function init_embedded_sem(ctx):
    csr = *(ctx + 0x20)                                // HAL-context SDMA CSR base (MED)
    aws_reg_write_embedded_sem_ctrl_cfg(csr, 1)        // enable control config
    for i in 0..7:                                     // 8 TPB evt-sem inc bases
        addr = aws_hal_get_tpb_evt_sem_inc_base(i)
        aws_hal_sdma_embedded_sem_wr_address(ctx, i, addr)       // → vt+0x538 → mariana wr_address
    for i in 0..15:                                    // 16 TopSP evt-sem inc bases
        addr = aws_hal_get_top_sp_evt_sem_inc_base(i)
        aws_hal_sdma_embedded_sem_wr_address(ctx, 8 + i, addr)   // regions 8..23 (24 of 32 populated)

// aws_hal_mariana_sdma_embedded_sem_wr_address — 0x4653a0
function embedded_sem_wr_address(ctx, index, addr):
    assert(index < 32)                                 // AL_HAL_NUM_EMBEDDED_SEM_REGIONS_MARIANA
    csr = *(ctx + 0x20)
    aws_reg_write_embedded_semaphore_update_lo(csr, index, LODWORD(addr))
    aws_reg_write_embedded_semaphore_update_hi(csr, index, HIDWORD(addr))

// aws_hal_mariana_sdma_embedded_sem_csr_index — 0x465490  (reverse lookup; the MESH marker)
function embedded_sem_csr_index(ctx):
    axi = aws_hal_stpb_get_axi_offset(ctx, 0x802701800)  // 0x2701800 | mesh-high-bit
    for i in 0..7:  if axi == tpb_evt_sem_inc_base(i):    return i          // → region 0..7
    for i in 0..15: if axi == top_sp_evt_sem_inc_base(i): return 8 + i      // → region 8..23
    return -1                                            // not an evt-sem base

// aws_mariana_sdma_write_seed — 0x464da0  (CCE stochastic-rounding seed)
function write_seed(ctx, queue, data_converter, seed):
    assert(queue < 16 && data_converter < 16)            // cce.h COUNT asserts; else al_abort_program(-1)
    addr = *(ctx + 0x20) + 0x2000 + 8*(data_converter + 16*(queue + 8))   // cce_user offset 0x2000
    al_reg_write32(addr, seed)
```

### Function Map

The MARIANA SDMA leaves, with their v4 vtable slot (`vt+N` = byte offset into the `khal_arch` ops struct written by `kaena_khal_register_funcs_v4`; `—` = reached directly).

| Function | Addr | Role | vt slot | Confidence |
|---|---|---|---|---|
| `aws_hal_q7_ucode_eng_init_mariana` | `0x464b00` | POOL-engine Q7 ucode loader (IRAM+DRAM via `write_padded`) | `+0x3a8` | HIGH |
| `aws_hal_q7_swap_table_mariana` | `0x464ab0` | STUB `return 0` (swap-table absent on v4) | `+0x390` | CERTAIN |
| `aws_hal_rdm_{get_dma_eng_id,init,get_target_address}_mariana` | `0x464d10`–`0x464d70` | STUBS: log "not supported for arch v4", return `0xFFFFFFFF` | `+0x8/+0x10/+0x18` | CERTAIN |
| `aws_mariana_sdma_write_seed` / `read_seed` | `0x464da0` / `0x464e90` | CCE stochastic-rounding seed (cce_user `0x2000`) | — | HIGH |
| `aws_hal_mariana_sdma_set_fp8_defaults` | `0x464f70` | RMW 4 FP8 data-converter control regs (cce base + `0xc00/0xc04/0xc10/0xc14`) | — | HIGH |
| `aws_hal_mariana_sdma_get_{fp8_ocp,non_ocp}_control_reg_value_for_config` | `0x465080` / `0x4650b0` | pure FP8 control-word builders | — | HIGH |
| `aws_hal_mariana_sdma_set_udma_qos` | `0x4650c0` | QoS gate (`+0x40C00` bit4, `+0x304` bit0) + per-queue M2S/S2M (`+0xc04`/`+0xc0c`) | — | HIGH |
| `aws_hal_mariana_sdma_enable_b0_features` | `0x465210` | `arch==4`-gated B0 RMW (`+0x40F00/0x40F04/0x3B460/0x3A120/0x42300`) | — | HIGH |
| `aws_hal_mariana_sdma_init_embedded_sem` | `0x465330` | 8 TPB + 16 TopSP evt-sem region init | `+0x528` | HIGH |
| `aws_hal_mariana_sdma_embedded_sem_{wr,read}_address` | `0x4653a0` / `0x465400` | per-region lo/hi address write/read (`index<32`) | `+0x538/+0x540` | HIGH |
| `aws_hal_mariana_sdma_embedded_sem_csr_index` | `0x465490` | reverse lookup via mesh AXI base `0x802701800` | `+0x530` | HIGH |
| `aws_hal_mariana_sdma_read_dma_csrs` | `0x465460` | dump all 32 region addresses to caller array | `+0x548` | HIGH |
| `aws_hal_mariana_sdma_s2m_set_embedded_sem_fields` | `0x465500` | pack `al_embedded_sem_t` into S2M desc bytes; set `desc[+3] \|= 0x80` | `+0x550` | HIGH |
| `aws_sdma_get_cce_params_mariana` | `0x465580` | `*size=0x2000; *param=2048` | `+0x510` | CERTAIN |
| `aws_sdma_m2s_dual_tail_ptr_enable_mariana` | `0x465590` | RMW M2S prefetch enhanced + data-tail-ptr enable | `+0x518` | HIGH |
| `aws_sdma_set_queue_priority_mariana` | `0x465610` | RMW M2S `tdr_req_candidate_high_priority` (16-bit field) | `+0x520` | HIGH |
| `aws_sdma_m2m_get_sdma_m2s_max_allowed_desc_per_packet_mariana` | `0x465650` | `return 128` | `+0x560` | CERTAIN |

### Considerations

> **QUIRK —** the RDM (rev-DMA) path is *retired* on MARIANA — all three leaves (`0x464d10`/`0x464d40`/`0x464d70`) are `0xFFFFFFFF`-returning stubs that log "is not supported for arch v4", yet they are **still installed** into the v4 vtable (`vt+0x8/+0x10/+0x18`). A caller that dispatches RDM on MARIANA gets a clean `0xFFFFFFFF` error, not a crash — the stub is the documented "unsupported" return, distinct from the embedded-sem slots that abort on the wrong arch (§1). A reimplementer must install the error-returning stub, not leave the slot NULL: the slot is *present-but-unsupported*, a different state than *absent*.

> **GOTCHA —** the Q7 swap-table facility is three `xor eax,eax; ret` / NULL-out stubs (`0x464ab0`/`0x464ac0`/`0x464af0`) — absent on Trn3 — but `aws_hal_q7_ucode_eng_init_mariana` (`0x464b00`) is a **real** 0x20a-byte loader. A reimplementer who reads "Q7 absent on v4" from the swap-table stubs and skips the ucode loader leaves the POOL engine without its pooling microcode. The two are independent: the *swap table* is gone, the *ucode load* (IRAM "POOL eng iram" + DRAM "POOL eng dram" via `write_padded`, owner→engine map `0→2`, `1→5`) is live. **(owner→engine map MED — read off the decompile branch, engine enum names not in band.)**

> **NOTE —** the seed/FP8/QoS/B0 leaves take the SDMA CSR base *directly* as their first argument, while the embedded-sem and init leaves read it from `*(ctx + 0x20)` of a HAL-context object. The `cce_user` offset `0x2000` (`aws_hal_get_sdma_cce_user_offset_mariana @0x477450`) is added on top for the seed and FP8 register apertures. SUNDA and CAYMAN return `0` from the CCE-user-offset getter — the feature is MARIANA-only at the offset layer too ([arch-csr-offsets §3](arch-csr-offsets.md)). The S2M embedded-sem descriptor field-pack (`s2m_set_embedded_sem_fields @0x465500`) sets `desc[+3] |= 0x80` (the `strong_ord_wr` bit) and packs the 6-byte `al_embedded_sem_t` (`sow`/`wr_mode`/`wr_val`/`reg_sel`/`intra_reg_offset`) into descriptor bytes `a2[0..3]`. **(HAL-context `+0x20` member MED — consistent across seed/init/wr/read, but the enclosing struct is owned by another cell.)**

---

## 5. The SDMA CSR / Geometry Matrix

### Purpose

This is the band's data core: the register families the inline-dispatch accessors compute against, with the per-arch stride/base/gate, anchored to the leaf that programs them. Every offset is read from the leaf's address-expression immediate, not inferred. The matrix collapses to three behaviors — arch-invariant (the per-queue notification block), the one fabric-trigger stride divergence, and the MARIANA-only register apertures.

### The notification-CSR field map

The TDMA-model notification block is the largest SDMA CSR family. The per-queue sub-block is arch-invariant (24-byte stride, 16 queues); the fabric-trigger sub-block diverges (48 vs 52-byte stride, 2 indices). All fields are contiguous 4-byte CSRs.

| Field | Base off | Stride (sun/cay/mar) | Index bound | Leaf · addr | Conf |
|---|---|---|---|---|---|
| per_queue m2s_trigger | `0x200` (512) | 24 / 24 / 24 | `idx<16` | `aws_reg_write_..._m2s_trigger @0x44dfc0` | HIGH |
| per_queue m2s_sw_queue | `0x208` (520) | 24 / 24 / 24 | `idx<16` | `..._m2s_sw_queue @0x44de30` | HIGH |
| per_queue s2m_trigger | `0x20c` (524) | 24 / 24 / 24 | `idx<16` | `..._s2m_trigger @0x44e2e0` | HIGH |
| per_queue s2m_sw_queue | `0x214` (532) | 24 / 24 / 24 | `idx<16` | `..._s2m_sw_queue @0x44e150` | HIGH |
| fabric axi_req trigger | `0x500` | **48 / 48 / 52** | `idx<2` | `..._axi_req_trigger @0x44e8f0` | HIGH |
| fabric axi_req id_mask | `0x504` | 48 / 48 / 52 | `idx<2` | `..._axi_req_id_mask @0x44e5f0` | HIGH |
| fabric axi_req id_cmp | `0x508` | 48 / 48 / 52 | `idx<2` | `..._axi_req_id_cmp @0x44e770` | HIGH |
| fabric axi_req sw_queue | `0x50c` | 48 / 48 / 52 | `idx<2` | `..._axi_req_sw_queue @0x44e470` | HIGH |
| fabric axi_rsp {trigger,id_mask,id_cmp,sw_queue} | `0x510`/`0x514`/`0x518`/`0x51c` | 48 / 48 / 52 | `idx<2` | `..._axi_rsp_* @0x44eef0`/… | HIGH |
| fabric apb_req {trigger,addr_mask,addr_cmp,sw_queue} | `0x520`/`0x524`/`0x528`/`0x52c` | 48 / 48 / 52 | `idx<2` | `..._apb_req_* @0x44f4f0`/… | HIGH |

### The capability-gated register families

The M2S-prefetch, CC-queue, XT-local, and MARIANA-only families, with their gate and aperture.

| Family | Leaf · addr | Gate | Aperture / offset | Conf |
|---|---|---|---|---|
| M2S prefetch enhanced_enable | `aws_reg_update_..._enhanced_enable @0x44f690` | `arch>2` (SUNDA no-op) | reg `+4` bits[15:0] | HIGH |
| M2S prefetch data_tail_ptr_enable | `..._data_tail_pointer_enable @0x44f700` | `arch>2` | reg `+4` bits[31:16] | HIGH |
| M2S tdr_req high-priority | `..._tdr_req_candidate_high_priority @0x44f7f0` | `arch>2` | reg `+8` (u16) | HIGH |
| M2S q tdrdtp_inc | `aws_reg_write_..._m2s_q_tdrdtp_inc @0x44f740` | `arch>2` | `base + (queue<<12) + 0x10E0`, `queue<16` | HIGH |
| XT CC-queue size/start/tail | `aws_reg_write_xt_cc_queue_* @0x44f820`–`0x44f980` | arch 2..4, skip on SUNDA | word[0]/[1..8]/[9..12], ch 0..3 | HIGH |
| XT local reg (general LR) | `aws_reg_write_xt_local_reg @0x44fa30` | CAYMAN+MARIANA (`(arch-3)>1`) | `base + 32*(idx+128)`, `idx<60` | HIGH |
| embedded sem ctrl/update | `aws_reg_{write,read}_embedded_sem* @0x44fb80`/`0x44fbc0` | vtable (`kaena_khal+0x1A8/0x1B0`); MARIANA only | thunk → `*_mariana` | HIGH |
| CCE seed (per queue×dc) | `aws_mariana_sdma_write_seed @0x464da0` | MARIANA only | `cce(0x2000) + 8*(dc + 16*(q+8))` | HIGH |
| FP8 data-converter defaults | `aws_hal_mariana_sdma_set_fp8_defaults @0x464f70` | MARIANA only | cce base `+0xc00/0xc04/0xc10/0xc14` | HIGH |
| UDMA QoS | `aws_hal_mariana_sdma_set_udma_qos @0x4650c0` | MARIANA only | gate `+0x40C00`/`+0x304`; per-q `+0xc04`/`+0xc0c` | HIGH |
| B0 silicon features | `aws_hal_mariana_sdma_enable_b0_features @0x465210` | `arch==4` else `-1` | `+0x40F00/0x40F04/0x3B460/0x3A120/0x42300` | HIGH |

### Data Tables

```text
data_type_sizes  .rodata — int32[16], dtype 1..15 → element byte size (idx 0 & >15 rejected)
   transpose builder (cayman)  @0x9e30e0  (data_type_sizes.3640, stride 4)
   m2s transpose desc          @0x9e7ea0  (data_type_sizes.4241)
   m2m validate_op             @0x9e86e0  (data_type_sizes.4724)
   values (all three identical):  { 0, 8, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 8, 1, 1, 1 }
   interpretation: 1→8B · 2/3→1B · 4..7→2B · 8..11→4B · 12→8B · 13..15→1B (FP8 class)

SBUF base windows (cayman transpose, inlined constants; window size 0x2000000 = 32 MiB)
   { 0x2000000000,   0x3000000000,   0x6000000000,   0x7000000000,
     0x802000000000, 0x803000000000, 0x806000000000, 0x807000000000 }
   tiles 4..7 carry the +0x800000000000 mesh high-bit (matches arch-geometry sbuf_base_addresses)
   partition = (PA - BASE) >> 18 (256-KiB stride);  win_off = (PA - BASE) & 0x3FFFF (18-bit)

mariana_sdma_base  .rodata @0x9df220 — u64[128], per-engine SDMA base, stride 0x1000
   entry0 = 0x001002000000, entry1 = 0x001002001000, …  (consumed by khal-context init, MED)
```

### Considerations

> **GOTCHA —** the fabric-trigger stride is the **only** notification-CSR stride that diverges per arch: SUNDA and CAYMAN pack the 2-index fabric-trigger sub-block at 48 bytes/index, MARIANA at 52 bytes/index. The per-queue sub-block (16 indices) is a uniform 24-byte stride on all three. A reimplementer who keys the fabric-trigger stride off the per-queue stride, or who reuses the SUNDA/CAYMAN 48-byte stride on MARIANA, mis-addresses every fabric AXI/APB notification register on Trn3 by `4*idx`. The field *bases* (`0x500`–`0x52c`) are arch-invariant; only the per-index stride moves.

> **QUIRK —** `aws_reg_write_xt_local_reg` (`0x44fa30`) for arch==2 (SUNDA) does **no write and no assert** — it computes nothing and returns. SUNDA lacks the `tpb_xt_local_reg` window, so the leaf silently drops the write rather than erroring. This matches the CC-queue family's skip-on-SUNDA behavior, but a SUNDA caller gets a *silent no-op*, not a `-EINVAL`. A reimplementer who asserts "all archs write" will diverge from the reference on the (unreached-in-practice) SUNDA path; a reimplementer who treats the SUNDA branch as an error breaks the documented silent-skip contract. **(Intentional-vs-HAL-gen-quirk MED — cannot confirm without the arch-header source; the no-op is byte-observed HIGH.)**

> **NOTE —** the `act_sequencer_force_rspcomb_eight_deep` setter (`0x44da50`) is *named* `_cayman_neff` but its body gates on `arch==4` (MARIANA) for the RMW at `+708` — a name/behavior mismatch that is real in the binary. The matching DVE-sequencer pair (`0x44db00`/`0x44db80`) tail-calls through `kaena_khal` dve-sequencer fn-ptrs (`unk_CAEDC0`/`unk_CAEDC8`), populated only by the MARIANA registrar. This is the same `force_rspcomb_eight_deep` capability [arch-stpb](arch-stpb.md) pins as MARIANA-only at the STPB layer — consistent across the two bands despite the misleading `_cayman_neff` suffix. **(Name-vs-arch mismatch reported as-is; the `arch==4` gate is HIGH.)**

---

## Related Components

| Name | Relationship |
|---|---|
| `al_sdma_m2s_build_*_meta_ctrl` (`0x451980`–`0x451dc0`) | the op-word encoders this page's descriptor build hands word1 to — encoding owned by [meta-ctrl-overlays](../dma/meta-ctrl-overlays.md) |
| `al_sdma_m2m_validate_op` (`0x452770`) | the central per-op validator the packet builders gate on before this band's descriptor build |
| `al_mla_udma_m2m_build_packet_mariana` (`unk_CAF200`, v4 `@0x463460`) | the final UDMA build the descriptor feeds; the per-arch wire-emit |
| `kaena_khal_register_funcs_v4` (`0x4622e0`) | installs 17 MARIANA SDMA leaves into `kaena_khal.khal_arch`; the slot wiring §1/§4 dispatch through |
| `aws_hal_stpb_get_axi_offset` (`0x458e00`) | applies the `+0x800000000000` mesh high bit (`0x802701800`) the MARIANA embedded-sem reverse-lookup keys on |
| `aws_hal_get_sdma_cce_user_offset_mariana` (`0x477450`) | returns the `0x2000` CCE-user aperture the seed/FP8 registers sit above (SUNDA/CAYMAN return `0`) |

## Cross-References

- [SDMA / CCE / TDG Meta-Control Overlays](../dma/meta-ctrl-overlays.md) — **owns** the `meta_ctrl` op-class encoding (the `0x900000`/`0x2400000`… op words, the `add`/`min_max` disambiguation) this page's descriptor build hands word1 to
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — **owns** the descriptor wire format (`{len_ctrl, meta_ctrl, buf_ptr}`, FIRST/LAST/RING-ID, the `0x01080003` default) the al_sdma m2s builders pack into
- [Per-Arch Device Layer: CSR and Register-Offset Accessors](arch-csr-offsets.md) — the `csr_*` MMIO primitives the SDMA CSR writes bottom into, and the per-arch SDMA/FP8 config *offset values* (the `0x2000` CCE-user offset, the MARIANA-only gate) §5 references
- [Per-Arch Device Layer: Geometry and Address Map](arch-geometry.md) — the SBUF base table (`0x2000000000…` + mesh high-bit) the CAYMAN transpose windows §2 match, and the arch enum (`sunda=2/cayman=3/mariana=4`) this page dispatches on
- [Per-Arch Device Layer: STPB Engine-Init, DGE and Pooling](arch-stpb.md) — the STPB engine-init band that shares the DGE/XT memcopy-config accessors and the MARIANA-only `force_rspcomb_eight_deep` capability §5 cross-confirms
- [UDMA Memory-to-Memory Builder](../kernel/udma-m2m.md) — the kernel-side twin of the al_sdma m2m descriptor build, reconciled bit-for-bit against this userspace path
- [back to index](../index.md)
