# Per-Arch Device Layer: Notification and INTC HAL

> *All addresses, offsets, symbol names, CSR register offsets, and per-arch register/constant values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`/`.rodata` are VMA == file offset (`.bss` is `NOBITS`). The vendored HAL package is `KaenaHal-2.31.0.0` (Amazon `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`); the per-arch notification TUs root in `.../KaenaHal-2.31.0.0/.../src/src/{sunda,cayman,mariana}/notific/aws_hal_notific_nq_<arch>.c`, the INTC body in `.../src/src/mariana/intc/aws_hal_intc.c`, the generic facade in `.../src/src/common/{intc,notific}/aws_hal_<intc,notific>.c`. Register field names and per-NQ bound asserts cite `{Sunda,Cayman,Mariana}ArchHeaders/.../arch-headers/<arch>/{notific_10_queue,intc_msi}.h`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** for every leaf symbol+address, the arch-dispatch enum, the NQ/INTC CSR field bases and strides, the 16-byte entry size, `NQ_COUNT=10`, the `0xAAE60` coalescer-timer constant, and the MSI-X mask/cause/timer offsets (`objdump`/decompile-anchored in the per-arch cells, `.rodata` reg-name strings byte-read). **Inferred / LOW** for the human-readable name of interior NQ-object operands lacking a DWARF parameter name. · Part IV — Userspace Runtime Core, DEEP · [back to index](../index.md)*

## Abstract

This page owns the **per-arch notification / INTC band** of the KaenaHal device layer — the silicon-specific bodies that program two cooperating hardware blocks: the **NOTIFIC notification-queue (NQ) engine** (a DMA-fed ring producer that streams 16-byte completion / collective-status / timestamp records into host or HBM memory), and the **TOP-INTC MSI-X interrupt controller** (the unit that masks/unmasks PCIe MSI-X vectors and user-errtrig interrupt groups, latches and reads interrupt cause, masks notification-log lines, and runs the interrupt-moderation timer). One set of NQ leaves exists per generation — **SUNDA** = arch 2 / Trn1-class+Inf2, **CAYMAN** = arch 3 / Trn2, **MARIANA** = arch 4 / Trn3-mesh — while the byte-decoded INTC leaves recovered here are the MARIANA set. Where the kernel half ([notification-queues](../kernel/notification-queues.md)) allocates the ring backing store and programs three base/size registers from the driver side, **this page owns the userspace HAL half**: the full NQ lifecycle (init / configure / enable / reset / available-count / read), the per-engine notification-block config (timestamp, write-buffer enable, AXI id, hardware-backpressure, coalescer), and the MSI-X interrupt-controller register programming the kernel driver does *not* perform.

The band's organizing principle is **arch dispatch through one CSR geometry**, identical in shape to the [SDMA band](arch-sdma.md). Upper runtime never names a silicon generation: it calls the generic `aws_hal_notific_*` / `aws_hal_intc_*` facade (`.text 0x450420..0x450fcd`, [hal-registers](hal-registers.md) sibling), each shim asserting the arch is latched and tail-jumping through a fixed `kaena_khal` slot (`.bss @0xCAEB80`) into the matching `*_sunda` / `*_cayman` / `*_mariana` leaf. The notific group occupies `kaena_khal` slots `+0x3B0..+0x400` (init, timestamp, wr-buf, axi-id, hw-bp, bp-config, coalescer, nq-size); the NQ-ring sub-block continues at `+0x408..+0x4D0`; the INTC group sits at `+0x480..+0x4D8`. The slots are filled at bring-up by `kaena_khal_register_funcs_v2 @0x468740` (SUNDA), `_v3 @0x46ed70` (CAYMAN), `_v4 @0x4622e0` (MARIANA) — the same registrars the rest of the device HAL keys off.

The per-arch story divides three ways and §1–§5 walk it. A first class is **arch-invariant**: the NQ register window is byte-for-byte identical across all three generations (per-NQ `base_lo/base_hi/f_size/head/tail` at `+256/+260/+264/+268/+272` with a 40-byte per-queue stride, `NQ_COUNT=10`, 16-byte entry, `0xAAE60` coalescer timer, the `+44/+48/+52/+176/+184/+188/+204` per-engine config block) — a reimplementer writes one register map for all three NQ leaves. A second class is the **per-arch divergence in the INTC layer and the mesh address space**: the MARIANA NQ-id→mailbox-base map carries the `+0x800000000000` two-die-mesh high bit on its upper half, and the MSI-X 4-group controller is the byte-decoded MARIANA path. A third class is **policy divergence**: CAYMAN's Q7 swap-table is a no-op stub while SUNDA's is a real 3-DWORD register write, and the CAYMAN Q7 ucode loader is live where SUNDA's path differs. These divergences, the NQ ring program, and the MSI-X mask/cause/timer program are the body of this page.

For reimplementation, the contract is:

- **The NQ register window and ring lifecycle** — the arch-invariant per-NQ field map (`base_lo @+256`, `base_hi @+260`, `f_size @+264`, `head @+268`, `tail @+272`, stride 40), the per-engine config block (`+44/+48/+52/+176/+184/+188/+204`), the `flat`-vs-`indexed` register-layout selector (`*(reg_handle+8) == 1`), the 16-byte entry, and the configure→init→queue_config / enable→reset / read→available_count call chains.
- **The MSI-X interrupt controller** — the `aws_hal_intc_t` handle layout (size `0x2CA8`, the `top_intc_msix_base @+8`, the `intc_user_errtrig_base[56] @+24`, the shadow masks), the top-MSI-X register offsets (`set-mask +16`, `clear-mask +24`, `mod-timer +40`, `per-vector ctrl +2060+16*vec`, `per-group interval +1024+8*grp`), and the 4-group errtrig sub-block (`cause +0`, `cause_set +8`, `mask +16`, `mask_clear +24`, `log_msk +56`, addressed `errtrig_base[a2] + grp<<12 + sub<<6 + REG`).
- **The arch-dispatch shim model** — every generic `aws_hal_{notific,intc}_*` asserts `al_hal_tpb_get_arch_type() != INVALID`, asserts a non-NULL `kaena_khal` slot, and tail-jumps; the per-arch body is selected by *which pointer the registrar wrote*, never by an `if (arch == …)` in the leaf.
- **The per-arch quirks** — the MARIANA mesh-high-bit NQ mailbox map, the CAYMAN Q7-swap no-op vs SUNDA real-write, the `flat`/`indexed` discriminator, and the reset coalescer-flush pulse.

| | |
|---|---|
| **What this page owns** | the per-arch NOTIFIC NQ leaves (`aws_hal_notific_nq_*`), the per-engine notific config (`aws_hal_notific_{set_axi_id,set_wr_buf_enable,update_hw_bp,set_coalescer_*,init,get_timestamp}_*`), and the MARIANA MSI-X INTC leaves (`aws_hal_intc_*_mariana`) |
| **Arch enum** | `SUNDA=2` (Trn1-class / Inf2) · `CAYMAN=3` (Trn2 / V3) · `MARIANA=4` (Trn3-mesh / V4); gate `al_hal_tpb_get_arch_type @0x44bca0` (reads `hal_target`, asserts `!= INVALID`, `< NUM=5`) — consistent with [hal-registers](hal-registers.md), [arch-sdma](arch-sdma.md) |
| **SUNDA NQ band** | `.text 0x469960..0x46b88b` (`aws_hal_notific_nq_*_sunda`, 24 fns; TU `sunda/notific/aws_hal_notific_nq_sunda.c`) |
| **CAYMAN NQ band** | `.text 0x470010..0x4716ca` (`aws_hal_notific_nq_*_cayman`, 24 fns; TU `cayman/notific/aws_hal_notific_nq_cayman.c`) |
| **MARIANA NQ band** | `.text 0x463560..0x464aa2` (`aws_hal_notific_nq_*_mariana`, 22 notific fns + 2 udma shims; TU `mariana/notific/aws_hal_notific_nq_mariana.c`) |
| **MARIANA INTC band** | `.text 0x478290..0x478ad0+` (13 `aws_hal_intc_*_mariana`; TU `mariana/intc/aws_hal_intc.c`) |
| **Generic facade** | `aws_hal_{notific,intc}_*` `.text 0x450420..0x450fcd` ([hal-registers](hal-registers.md) sibling) — arch-dispatch shims |
| **Dispatch object** | `kaena_khal @.bss 0xCAEB80`; notific slots `+0x3B0..+0x400`, NQ-ring slots `+0x408..+0x4D0`, intc slots `+0x480..+0x4D8` |
| **Registrars** | `kaena_khal_register_funcs_v2 @0x468740` (SUNDA) · `_v3 @0x46ed70` (CAYMAN) · `_v4 @0x4622e0` (MARIANA) |
| **NQ entry size** | `16` (`aws_hal_notific_nq_get_notification_size_<arch> == 16`, hard-coded leaf, all three archs) |
| **NQ register window** | per-NQ `base_lo +256`, `base_hi +260`, `f_size +264`, `head +268`, `tail +272`; stride 40 (`40*idx`), `idx<NQ_COUNT=10` |
| **Coalescer timer** | `0xAAE60` (= 700000 dec) written to `reg+0xB4` (180) by `notific_init` on the 10-queue path (all three archs) |
| **INTC handle** | `aws_hal_intc_t` ordinal 1311, size `11432 = 0x2CA8`; `top_intc_msix_base @+8`, `errtrig_base[56] @+24` |
| **Mesh NQ-base bit** | `mariana_intc_nq_id_to_mb_base_map @.rodata 0x9f2160`: blocks 0–3 `0x8580000400+0x200*qid`, blocks 4–7 `+0x800000000000` |

> **NOTE —** the NQ register window documented here is the *same* `0x28`-byte (40-byte) per-instance block the [kernel NQ engine](../kernel/notification-queues.md) programs from the driver side (`BASE_ADDR_LO +0x100`, `BASE_ADDR_HI +0x104`, `F_SIZE +0x108`, `HEAD +0x10c`, stride `0x28`). The kernel writes the first three at queue setup; this userspace HAL writes the same three *and* drives the ring consumer (head advance, tail poll, phase walk) plus the per-engine config and coalescer the kernel never touches. The two halves agree bit-for-bit on the register block — cross-validation, not duplication.

---

## 1. The Arch-Dispatch Shape and the NQ/INTC Bands

### Purpose

Every register touch in this band reaches the silicon through one indirection a reimplementer must understand before reading any leaf: the **generic-facade tail-jump**. Upper runtime (`notification_configure`, `aws_hal_intc_top_notific_init`, `tsync_timestamps_*`, `exec_check_intc_sw_notif_queue_overflow`) calls an arch-neutral `aws_hal_notific_*` / `aws_hal_intc_*` function in the facade band (`0x450420..0x450fcd`). That function does not program a register — it asserts the arch is latched, loads a function pointer from the `kaena_khal` ops table, and tail-jumps into the per-arch leaf this page documents. The arch-specificity lives entirely in *which pointer the registrar installed* at bring-up; the leaf itself never branches on `arch`. Understanding the split once lets a reader navigate the four arch cells (sunda/cayman/mariana NQ + mariana INTC) by reflex.

### Entry Point

The band is reached from three directions — the notification orchestrators, the interrupt-controller bring-up, and the timestamp-sync path — all bottoming through the facade shim into the per-arch leaf:

```text
notification_configure / notification_enable_v2          ── NQ orchestrators [other cell]
  └─ aws_hal_notific_set_coalescer_stream (0x450e50)      ── facade shim (hal-registers)
       └─ al_hal_tpb_get_arch_type (0x44bca0)             ── assert arch != INVALID
       └─ jmp *kaena_khal.khal_notific.set_coalescer_stream  ── +0x3E8 → *_<arch> leaf (this page)

aws_hal_intc_top_notific_init / errtrig_notific_init     ── INTC bring-up [other cell]
  ├─ aws_hal_notific_set_axi_id (0x450cd0)                ── facade shim → +0x3D0 → *_<arch>
  └─ aws_hal_intc_set_notific_queue_base (0x450890)       ── facade shim → +0x4A8 → *_mariana
       └─ aws_hal_notific_nq_set_base_addr (0x451070)     ── NQ base programmer (this page §3)

exec_check_intc_sw_notif_queue_overflow                  ── cause poll [other cell]
  └─ aws_hal_intc_read_cause (0x450990)                  ── facade shim → +0x4D0 → *_mariana §4
```

### Algorithm

The facade shim is the canonical dispatch body (one disassembled instance: `aws_hal_intc_mask_vector @0x450420`); every one of the 22 shims is byte-identical but for the slot offset and arg list:

```c
// CANONICAL arch-dispatch shim — modelled on aws_hal_intc_mask_vector @0x450420
//   (generic facade, common/intc/aws_hal_intc.c) ; slot = kaena_khal + 0x480
function aws_hal_intc_mask_vector(hdl, vec):
    if al_hal_tpb_get_arch_type() == AL_HAL_TPB_ARCH_TYPE_INVALID:   // 0x44bca0 gate
        __assert_fail("al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID", ...)  // .rodata 0x81C890
    fp = kaena_khal.khal_intc.aws_hal_intc_mask_vector             // 0xCAEB80 + 0x480 = 0xCAF000
    if fp == NULL:
        __assert_fail("kaena_khal.khal_intc.aws_hal_intc_mask_vector != NULL", ...)        // .rodata 0x8228D8
    return fp(hdl, vec)                                            // jmp *fp — tail-call per-arch leaf
```

Only one notific shim carries real logic before the tail-call: `aws_hal_notific_init @0x450a10` allocates the per-queue NQ-object array — `al_zalloc(0x160 * num_queues)` (= 352 bytes/queue) into `ctx+0x10` — then tail-calls the arch leaf:

```c
// aws_hal_notific_init @0x450a10 — common/notific/aws_hal_notific.c (REAL logic, then dispatch)
function aws_hal_notific_init(ctx, num_queues, bp_mode, uart_mode, axi_base):
    assert(al_hal_tpb_get_arch_type() != INVALID)
    al_hal_log(LEVEL_4, "Initializing ENS hal with AXI base %p", axi_base)   // .rodata 0x822C48
    if ctx == NULL || axi_base == NULL: return -14                  // 0xFFFFFFF2
    ctx->axi_base   = axi_base                                      // +0x00
    ctx->num_queues = num_queues                                    // +0x08
    ctx->nq_array   = al_zalloc(0x160 * num_queues)                 // +0x10 ; 352 B/queue
    if ctx->nq_array == NULL: return -12                            // 0xFFFFFFF4
    return kaena_khal.khal_notific.init(ctx, num_queues, bp_mode, uart_mode, axi_base)  // +0x3B0
```

### Function Map

The facade-to-leaf slot map — which generic shim routes to which `kaena_khal` slot, and the per-arch leaf address per generation. (Slot offset = `slot_vma − 0xCAEB80`.)

| Generic shim | Slot off | SUNDA leaf | CAYMAN leaf | MARIANA leaf | Conf |
|---|---|---|---|---|---|
| `aws_hal_notific_init` | `+0x3B0` | `0x46b490` | `0x470180` | `0x4637c0` | HIGH |
| `aws_hal_notific_get_timestamp` | `+0x3B8` | `0x46aeb0` | `0x46ffa0` | `0x4635e0` | HIGH |
| `aws_hal_notific_set_wr_buf_enable` | `+0x3C0` | `0x46af80` | `0x46ffd0` | `0x463610` | HIGH |
| `aws_hal_notific_update_wr_buf_enable` | `+0x3C8` | `0x46b050` | `0x470010` | `0x463650` | HIGH |
| `aws_hal_notific_set_axi_id` | `+0x3D0` | `0x46b130` | `0x470060` | `0x4636a0` | HIGH |
| `aws_hal_notific_update_hw_bp` | `+0x3D8` | `0x46b220` | `0x4700b0` | `0x4636f0` | HIGH |
| `aws_hal_notific_set_bp_config` | `+0x3E0` | `0x46b300` | `0x470100` | `0x463740` | HIGH |
| `aws_hal_notific_set_coalescer_stream` | `+0x3E8` | `0x46b5d0` | `0x470220` | `0x463860` | HIGH |
| `aws_hal_notific_set_coalescer_bypass` | `+0x3F0` | `0x46b6d0` | `0x4702c0` | `0x463900` | HIGH |
| `aws_hal_notific_get_timestamp_inc` | `+0x3F8` | `0x46b760` | `0x470300` | `0x463940` | HIGH |
| `aws_hal_notific_nq_get_notification_size` | `+0x400` | `0x469e90` | `0x470650` | `0x463c90` | CERTAIN |
| `aws_hal_intc_mask_vector` | `+0x480` | — | — | `0x4783d0` | HIGH |
| `aws_hal_intc_set_cause` / `read_cause` | `+0x4C8`/`+0x4D0` | — | — | `0x478950` / `0x478a20` | HIGH |
| `aws_hal_intc_set_notific_queue_base` | `+0x4A8` | — | — | `0x4782f0` | HIGH |

### Considerations

> **NOTE —** the SUNDA/CAYMAN columns of the INTC rows are intentionally `—`: only the MARIANA INTC leaves (`aws_hal_intc.c` under `src/mariana/intc`) were byte-decoded in the recovered cells. The arch enum and the `kaena_khal` slot wiring are identical across generations, so a reimplementer can expect a sunda/cayman INTC leaf at the same slot — but this page does **not** fabricate their addresses or register values. The byte-decoded INTC register program in §4 is the MARIANA one; treat any sunda/cayman INTC offset as *not traced* until decoded from its own band.

> **GOTCHA —** the generic `aws_hal_notific_nq_get_notification_size @0x450fc0` is a *pure* 13-byte tail-jump (`lea kaena_khal; jmp *0x400(%rax)`) with **no arch-valid assert and no args** — the only shim that skips the gate. A reimplementer who copies the canonical shim shape to it adds a spurious assert. The per-arch leaf it lands on is a constant `mov $0x10; ret` on all three archs, so the entry size is fixed at 16 bytes everywhere; the indirection exists only so the *size* is a registered capability, not because it varies.

---

## 2. The Per-Engine Notification-Block Config

### Purpose

Before any NQ ring is driven, the per-engine **notification block** is brought up: timestamp readout, write-buffer-enable mask, AXI user-id, hardware-backpressure mask, software-backpressure mode, and the coalescer (stream-enable / bypass / moderation timer). These ten leaves operate on a `notific` engine object whose MMIO base is `*(obj+0)`, with a `1`-vs-`10`-queue register flavour selected by `*(obj+8) == 1` (`notific_1_queue` for a single-NQ engine such as a TopSP, `notific_10_queue` for the per-NeuronCore TPB). The field byte-offsets are **arch-invariant** — sunda, cayman, and mariana write the identical register window — so the reimplementation target is one register map plus the read-modify-write idioms below.

### Entry Point

```text
aws_hal_intc_{top,errtrig}_notific_init / tdrv_init      ── engine bring-up [other cell]
  └─ aws_hal_notific_init_<arch> (sunda 0x46b490 / cayman 0x470180 / mariana 0x4637c0)
       ├─ al_reg_write32 (uart_mode +40 ; wob_ctrl +188 = 1)
       ├─ [10-queue] al_reg_write32 (coal_timer +180 = 0xAAE60 ; obj+180 = 15)
       ├─ aws_hal_notific_set_wr_buf_enable_<arch>(obj, -1)        ── enable all wr-buf lanes
       └─ aws_hal_notific_set_bp_config_<arch>(obj, bp_mode)
tsync_timestamps_start / _finish                          ── timestamp sync [other cell]
  ├─ aws_hal_notific_get_timestamp_inc_<arch> (reg+204)
  └─ aws_hal_notific_set_coalescer_bypass_<arch> (reg+184 bit8)
```

### Algorithm

The per-engine config leaves share two idioms: a 64-bit masked read-modify-write (write-buffer-enable, hardware-backpressure) and a single-bit/bitfield RMW (axi-id, coalescer). The bodies are arch-invariant; the addresses below are the SUNDA leaves, with cayman/mariana noted as identical:

```c
// aws_hal_notific_init_<arch> — sunda 0x46b490 / cayman 0x470180 / mariana 0x4637c0
function notific_init(obj, sel, bp_mode, wr_buf_val, reg_base):
    al_reg_write32(reg_base + 40, wr_buf_val)              // +0x28 nq_uart_mode  (a5+10)
    al_reg_write32(reg_base + 188, 1)                      // +0xBC wob_ctrl = 1  (a5+47)
    if sel != 1:                                           // 10-queue flavour only
        al_reg_write32(reg_base + 180, 0xAAE60)            // +0xB4 coal_timer = 700000
        obj[180] = 15                                      // +0xB4 stream-enable shadow seed
    set_wr_buf_enable(obj, -1)                             // enable every write-buffer lane
    set_bp_config(obj, bp_mode)                            // 0 = all-BP, 1 = no-BP

// aws_hal_notific_update_wr_buf_enable_<arch> — sunda 0x46b050 / cayman 0x470010 / mariana 0x463650
//   masked 64-bit update of the write-buffer-enable register pair (+48 lo / +52 hi)
function update_wr_buf_enable(obj, val, mask):
    old = obj->wr_buf_cache                                // +0x98 (obj[19])
    new = old ^ (mask & (old ^ val))                       // standard masked-update idiom
    al_reg_write32(reg_base + 48, LODWORD(new))            // +0x30 wr_buf_enable_lo
    al_reg_write32(reg_base + 52, HIDWORD(new))            // +0x34 wr_buf_enable_hi
    obj->wr_buf_cache = new

// aws_hal_notific_set_axi_id_<arch> — sunda 0x46b130 / cayman 0x470060 / mariana 0x4636a0
function set_axi_id(obj, axi_id, rotate):
    al_reg_write32(reg_base + 44, axi_id)                  // +0x2C nq_axi_id
    r = al_reg_read32(reg_base + 188)                      // +0xBC wob_ctrl
    al_reg_write32(reg_base + 188, (r & 0xFFFFFFFE) | (rotate & 1))   // bit0 = axi_id_rotate

// aws_hal_notific_set_bp_config_<arch> — sunda 0x46b300 / cayman 0x470100 / mariana 0x463740
function set_bp_config(obj, mode):
    if mode == 0:   al_reg_write32(reg_base + 8, 1023)     // +0x08 sw_backpressure = 0x3FF (all-BP)
    elif mode == 1: al_reg_write32(reg_base + 8, 0)        //        sw_backpressure = 0 (no-BP)
    else: al_hal_log(1, "[%s]: Unknown mode!"); return -1
    al_reg_write32(reg_base + 12, 0xFFFFFFFF)              // +0x0C hw_backpressure_lo
    al_reg_write32(reg_base + 16, 0xFFFFFFFF)              // +0x10 hw_backpressure_hi
    obj->hw_bp_cache = -1                                  // +0xA0 (obj[20])

// aws_hal_notific_set_coalescer_stream_<arch> — sunda 0x46b5d0 / cayman 0x470220 / mariana 0x463860
function set_coalescer_stream(obj, stream, en):
    if stream >= 4 || stream >= obj->num_streams:          // num_streams = obj[2]
        al_hal_log(1, "cannot %sable coalescer stream %u"); return -22
    bits = update_shadow(obj[45], stream, en)              // +0xB4 stream-enable shadow
    r = al_reg_read32(reg_base + 184)                      // +0xB8 coal_ctrl
    al_reg_write32(reg_base + 184, (r & ~0x3C00) | ((bits << 10) & 0x3C00))   // stream_en bits[13:10]
```

### Function Map

The per-engine config leaves, per generation, with the register field each touches. All field offsets are arch-invariant; addresses differ per arch.

| Leaf (role) | SUNDA | CAYMAN | MARIANA | Register field | Conf |
|---|---|---|---|---|---|
| `notific_init` (engine bring-up) | `0x46b490` | `0x470180` | `0x4637c0` | `+40` uart, `+188` wob=1, `+180` coal=`0xAAE60` (10-q) | HIGH |
| `get_timestamp` (64-bit) | `0x46aeb0` | `0x46ffa0` | `0x4635e0` | `(rd(+4)<<32)\|rd(+0)` | HIGH |
| `get_timestamp_inc` | `0x46b760` | `0x470300` | `0x463940` | `rd(+204)` (`+0xCC`) | HIGH |
| `set_wr_buf_enable` | `0x46af80` | `0x46ffd0` | `0x463610` | `+48`/`+52`; cache `+0x98` | HIGH |
| `update_wr_buf_enable` | `0x46b050` | `0x470010` | `0x463650` | masked RMW `+48`/`+52` | HIGH |
| `set_axi_id` | `0x46b130` | `0x470060` | `0x4636a0` | `+44`; `+188` bit0 rotate | HIGH |
| `update_hw_bp` | `0x46b220` | `0x4700b0` | `0x4636f0` | masked RMW `+12`/`+16`; cache `+0xA0` | HIGH |
| `set_bp_config` | `0x46b300` | `0x470100` | `0x463740` | `+8` mode, `+12`/`+16` = `0xFFFFFFFF` | HIGH |
| `set_coalescer_stream` | `0x46b5d0` | `0x470220` | `0x463860` | `+184` bits`[13:10]` (mask `0x3C00`) | HIGH |
| `set_coalescer_bypass` | `0x46b6d0` | `0x4702c0` | `0x463900` | `+184` bit8 (`<<8`) | HIGH |

### Considerations

> **QUIRK —** `notific_init` writes the coalescer moderation timer `coal_timer @+0xB4 = 0xAAE60` (= 700000 decimal) **only on the 10-queue flavour** (`sel != 1`). On a single-NQ engine (a TopSP, `sel == 1`) the coalescer-timer write and the `obj[180] = 15` stream-shadow seed are skipped — a single-NQ engine has no coalescer to moderate. The `0xAAE60` constant is identical across all three archs (sunda `0x46b490`, cayman `0x470180`, mariana `0x4637c0`); it is a fixed clock-period/divisor, not a per-generation value. A reimplementer who unconditionally writes the timer programs a reserved register on single-NQ engines. **(Constant HIGH; "clock period/divisor" role MED — inferred from the `+0xB4` write paired with the `+0xCC` increment-register read.)**

> **GOTCHA —** the write-buffer-enable and hardware-backpressure leaves keep a 64-bit *shadow cache* in the engine object (`+0x98` for wr-buf, `+0xA0` for hw-bp) and the masked-update path reads the **cache**, not the register, before the RMW. A reimplementer who re-reads the MMIO register instead of the cached shadow will diverge whenever a prior `set_bp_config` forced `hw_backpressure = 0xFFFFFFFF` / cache `= -1`: the register and the intended cache can differ, and the cache is authoritative for the next masked update. The cache write accompanies every register write — drop neither.

---

## 3. The NQ Ring Lifecycle

### Purpose

This is the band's core: the per-NQ ring producer/consumer that userspace polls for execution-completion notifications. The lifecycle is `configure → init (SW validate + copy) → queue_config (HW base/size program)`, then `enable → reset` to arm/quiesce, then `read → available_count` to drain. The NQ object is a 352-byte (`0x160`) software handle holding a `0x150`-byte copy of its init params; the hardware side is the per-NQ register window inside the engine's notific block. The register map is **arch-invariant** across sunda/cayman/mariana — one map drives all three.

### Entry Point

```text
notification_configure                                   ── ring setup [other cell]
  └─ aws_hal_notific_nq_configure_<arch> (sunda 0x46a2a0 / cayman 0x4709c0 / mariana 0x464000)
       ├─ aws_hal_notific_nq_init_<arch>        ── validate cfg, copy 0x150 init_params, cap=size/16
       │    └─ aws_hal_notific_nq_get_notification_size_<arch> (== 16)
       └─ aws_hal_notific_nq_queue_config_<arch> ── write base_lo/base_hi/f_size MMIO

notification_enable_v2                                   ── arm/disarm [other cell]
  └─ aws_hal_notific_nq_enable_<arch> (sunda 0x46a330 / cayman 0x470a50 / mariana 0x464090)
       └─ aws_hal_notific_nq_reset_<arch> (thunk) → _aws_hal_notific_nq_reset_<arch>
            └─ aws_hal_notific_nq_write_hw_head_value_<arch>.part.0   ── [out of band]

userspace NQ poll                                        ── consume [kernel/notification-queues]
  └─ aws_hal_notific_nq_read_<arch> (sunda 0x46a820 / cayman 0x470ec0 / mariana 0x464500)
       └─ aws_hal_notific_nq_available_count_<arch> ── tail-ptr read + phase-bit wait
```

### Algorithm

Two programs are reimplementation-critical: the HW-program sequence (`queue_config` → the three base/size registers) and the reset/quiesce sequence (drain tail, pulse coalescer, zero ring, reset head). The register offsets are byte-identical across archs; addresses below are the SUNDA leaves with the per-NQ stride called out:

```c
// aws_hal_notific_nq_queue_config_<arch> — sunda 0x469ff0 / cayman 0x4707b0 / mariana 0x463df0
//   program the three per-NQ HW registers from the SW init copy
function nq_queue_config(nq, base_pa, f_size):
    reg  = *(nq + 16)                                      // engine notific reg-handle
    flat = (*(reg + 8) == 1)                               // 1 => notific_1_queue (single NQ)
    idx  = nq->init_params.queue_index                     // nq+29 byte
    off  = flat ? 0 : 40 * idx                             // 10-queue: 40-byte per-NQ stride
    if !flat: assert(idx < 10)                             // "index < AWS_REG_<ARCH>_..._NQ_COUNT"
    al_reg_write32(reg_base + off + 256, LODWORD(base_pa)) // +0x100 nq_base_addr_lo
    al_reg_write32(reg_base + off + 260, HIDWORD(base_pa)) // +0x104 nq_base_addr_hi
    al_reg_write32(reg_base + off + 264, f_size)           // +0x108 nq_f_size

// _aws_hal_notific_nq_reset_<arch> — sunda 0x469a50 / cayman 0x470310 / mariana 0x463950
//   quiesce the ring: disable, flush coalescer, drain tail, reset head, zero backing buffer
function nq_reset(nq):
    reg = *(nq + 16) ; idx = nq->init_params.queue_index
    off = (*(reg+8) == 1) ? 0 : 40 * idx
    disable_mask = ROL32(-2, idx)                          // clear THIS queue's enable bit
    al_reg_write32(reg_base + 24, nq_overflow_shadow & ...)// +0x18 sw_overflow.ignore_full_en
    al_reg_write32(reg_base + 20, nq_enable_shadow & disable_mask)  // +0x14 nq_enable
    if idx <= 3:                                           // coalescer flush pulse (10-queue)
        r = al_reg_read32(reg_base + 184)                  // +0xB8 coal_ctrl
        al_reg_write32(reg_base + 184, r | 0x200)          //   set   coal_ctrl.clr (bit9)
        al_reg_write32(reg_base + 184, r & ~0x200)         //   clear coal_ctrl.clr
    while al_reg_read32(reg_base + off + 272) != 0:        // +0x110 nq_tail_ptr — spin until drained
        usleep(100)
    nq->next_index = 0 ; nq->phase = 0                     // reset SW consumer cursor + phase
    al_reg_write32(reg_base + off + 268, recomputed_head)  // +0x10C nq_head_ptr
    if nq->init_params.on_hbm: (*nq->hbm_zero_fp)(...)     // nq+0x150 HBM-zero callback
    else:                      al_memset(nq->ring_buf, 0, nq->capacity * 16)

// aws_hal_notific_nq_available_count_<arch> — sunda 0x46a510 / cayman 0x470bf0 / mariana 0x464230
//   count pending entries; two consumer models keyed on the phase-bit flag (nq+52)
function nq_available_count(nq, *out):
    if nq->phase_bit_mode:                                 // nq+52 set ; asserts !on_hbm
        // walk ring entries, comparing entry.byte[3]>>7 to the expected phase (nq+8)
        *out = walk_until_phase_mismatch(nq)
    else:
        tail = al_reg_read32(reg_base + off + 272)         // +0x110 nq_tail_ptr
        assert(tail % 16 == 0)                             // "tail % get_notification_size() == 0"
        count = ((tail / 16) - nq->next_index) mod nq->capacity
        // poll last entry's phase byte up to 10000x usleep(100) for the DMA to land
        if !wait_for_arrival(nq): al_hal_log("Failed to wait for notifs to arrive on nq %u")
        *out = count
```

### Function Map

The NQ-ring lifecycle leaves, per generation. The `flat`/`indexed` selector, the `idx<10` bound, and the 16-byte entry are identical across all three.

| Leaf (role) | SUNDA | CAYMAN | MARIANA | Conf |
|---|---|---|---|---|
| `nq_get_notification_size` (== 16) | `0x469e90` | `0x470650` | `0x463c90` | CERTAIN |
| `nq_init` (SW validate + 0x150 copy) | `0x469ea0` | `0x470660` | `0x463ca0` | HIGH |
| `nq_queue_config` (HW base/hi/f_size) | `0x469ff0` | `0x4707b0` | `0x463df0` | HIGH |
| `nq_set_init_params_cookie` (`size<=0x100`) | `0x46a260` | `0x470980` | `0x463fc0` | HIGH |
| `nq_configure` (high-level + warns) | `0x46a2a0` | `0x4709c0` | `0x464000` | HIGH |
| `nq_enable` (reset + set enable bit) | `0x46a330` | `0x470a50` | `0x464090` | HIGH |
| `nq_available_count` (tail-poll / phase walk) | `0x46a510` | `0x470bf0` | `0x464230` | HIGH |
| `nq_read` (drain + wrap + head advance) | `0x46a820` | `0x470ec0` | `0x464500` | HIGH |
| `_nq_reset` (internal quiesce) | `0x469a50` | `0x470310` | `0x463950` | HIGH |
| `nq_reset` (public 5-byte thunk → `_nq_reset`) | `0x46ac70` | `0x4712d0` | `0x464910` | HIGH |
| `nq_set_base_addr` (base_lo/hi only) | `0x46ac80` | `0x4712e0` | `0x464920` | HIGH |
| `nq_stats_get` (id 6 → `rd(+176)`) | `0x46ae20` | `0x471420` | `0x464a60` | HIGH |

### Considerations

> **QUIRK —** two register-layout flavours hide behind one register map, selected at runtime by `*(reg_handle + 8) == 1`. On the `notific_1_queue` flavour (single NQ, e.g. a TopSP) the per-NQ fields sit at *fixed* offsets `+256/+260/+264/+268/+272` with no index stride; on the `notific_10_queue` flavour (per-NeuronCore TPB, 10 NQs) each NQ adds `40*idx` and the leaf bounds-checks `idx < NQ_COUNT(10)` → `al_abort_program(-1)` on violation (`"index < AWS_REG_<ARCH>_NOTIFIC_10_QUEUE_NOTIFIC_NQ_COUNT"`). A reimplementer who hard-codes the 40-byte stride mis-addresses every register on a single-NQ engine; one who drops the bound-check writes out of the 10-NQ array on an OOB index. The scalar config block (§2) and the field *bases* are identical between flavours — only the per-NQ stride and bound differ.

> **GOTCHA —** the `_nq_reset` coalescer-flush is a **set-then-clear pulse** on `coal_ctrl.clr` (`reg+184` bit9, `0x200`), gated `idx <= 3`, and it is followed by a **busy-wait** on `nq_tail_ptr` (`reg+272`) reaching zero with `usleep(100)` between polls. A reimplementer who writes the flush bit once (without clearing it) leaves the coalescer permanently flushing; one who skips the tail-drain spin races the in-flight DMA and zeroes a ring the hardware is still writing. The mariana `_nq_reset` (`0x463950`) uses the same bit and gate; the value (`0x200`) and the `idx<=3` gate are arch-invariant.

> **NOTE —** the public `nq_reset` is a 5-byte tail-jmp thunk into the private `_nq_reset` on every arch (sunda `0x46ac70`→`0x469a50`, cayman `0x4712d0`→`0x470310`, mariana `0x464910`→`0x463950`); the private body is also called directly from `nq_enable`. A reimplementer reproduces one body and exposes it twice — the thunk is the vtable-wired public entry (`kaena_khal +0x440`), the private symbol is the intra-band callee. The `read`/`reset` head-advance bottoms into `aws_hal_notific_nq_write_hw_head_value_<arch>.part.0` (sunda `0x6237d`, cayman `0x623a0`, mariana `0x6235a`), a compiler-outlined helper **out of this band** — its head-recompute math is owned by its own cell, not re-derived here.

---

## 4. The MSI-X Interrupt Controller (MARIANA INTC)

### Purpose

The notification engine *produces* records; the **INTC MSI-X controller** is what raises the PCIe interrupt that tells the host a record landed, and what the host masks/acks. The 13 byte-decoded leaves (`aws_hal_intc_*_mariana`, TU `src/mariana/intc/aws_hal_intc.c`) operate on an `aws_hal_intc_t` handle (size `0x2CA8 = 11432`) and program the TOP-INTC MSI-X 4-group unit: mask/unmask a PCIe MSI-X vector, mask/unmask a user-errtrig interrupt group, set/read interrupt cause, mask/unmask the notification log-mask, run the moderation timer, and point an NQ at its mailbox base. This is the interrupt-side surface the kernel driver does **not** implement ([notification-queues](../kernel/notification-queues.md): "ships no in-kernel ISR") — it is driven entirely from userspace through this HAL.

### Algorithm

The two reimplementation-critical programs are the MSI-X vector mask/unmask (top-MSI-X register block) and the cause set/read (4-group errtrig sub-block). Both are byte-anchored from the MARIANA cell:

```c
// aws_hal_intc_mask_vector_mariana @0x4783d0 — mask one MSI-X vector
function intc_mask_vector(hdl, vec):
    if vec > 7: return -22                                 // 0xFFFFFFEA
    msix = hdl->top_intc_msix_base                         // handle +8
    hdl->top_intc_masks[0] |= (1 << vec)                   // +472 shadow (OR)
    al_reg_write32(msix + 16, hdl->top_intc_masks[0])      // +16 set-mask
    al_reg_write32(msix + 2060 + 16*vec, 1)                // +2060+16*vec per-vector ctrl = 1 (mask)

// aws_hal_intc_unmask_vector_mariana @0x478430 — unmask one MSI-X vector (mirror)
function intc_unmask_vector(hdl, vec):
    if vec > 7: return -22
    msix = hdl->top_intc_msix_base
    hdl->top_intc_masks[0] &= ROL32(-2, vec)               // +472 shadow (clear bit vec)
    al_reg_write32(msix + 24, hdl->top_intc_masks[0])      // +24 clear-mask
    al_reg_write32(msix + 2060 + 16*vec, 0)                // per-vector ctrl = 0 (unmask)

// aws_hal_intc_set_cause_mariana @0x478950 — latch an interrupt cause bit
function intc_set_cause(hdl, group, src):
    (g, sub, bit) = intc_get_trigger_info(group, src)      // 0x450080 — resolve grp/sub/bit
    assert(g < 4)                                          // "index < AWS_REG_MARIANA_INTC_4GRP_MSIX_UNIT_CTRL_COUNT"
    base = hdl->intc_user_errtrig_base[group]              // handle + 8*group + 24
    al_reg_write32(base + (g<<12) + (sub<<6) + 8, 1 << bit)// +8 int_cause_set_grp

// aws_hal_intc_read_cause_mariana @0x478a20 — read an interrupt cause group
function intc_read_cause(hdl, group, src):
    (g, sub, _) = intc_get_trigger_info(group, src)
    assert(g < 4)
    base = hdl->intc_user_errtrig_base[group]
    return al_reg_read32(base + (g<<12) + (sub<<6) + 0)    // +0 int_cause_grp

// aws_hal_intc_set_mod_timer_period_mariana @0x4786b0 — moderation-timer period
function intc_set_mod_timer_period(hdl, period):
    assert(period < 16)
    msix = hdl->top_intc_msix_base
    r = al_reg_read32(msix + 40)                           // +40 mod-timer ctrl
    al_reg_write32(msix + 40, (r & ~0x0F000000) | (period << 24))   // bits[27:24]

// aws_hal_intc_set_notific_queue_base_mariana @0x4782f0 — point an NQ at its mailbox base
function intc_set_notific_queue_base(hdl, block_idx, queue_id):
    assert(block_idx < 8 && queue_id < 8)
    nq   = hdl->user_errtrig_ens[block_idx].nq             // handle + 192*block_idx + 696
    base = mariana_intc_nq_id_to_mb_base_map[8*(block_idx > 3) + queue_id]   // .rodata 0x9f2160
    aws_hal_notific_nq_set_base_addr(nq, base)             // 0x451070 (§3 NQ base programmer)
```

### Register / Handle Map

The `aws_hal_intc_t` handle and the two MSI-X register sub-blocks, byte-decoded from the MARIANA cell. The errtrig sub-block is addressed `errtrig_base[a2] + (grp<<12) + (sub<<6) + REG`.

| Handle field | Offset | Role | Conf |
|---|---|---|---|
| `bar0_base` | `+0` | BAR0 MMIO base | HIGH |
| `top_intc_msix_base` | `+8` | top MSI-X register block base | HIGH |
| `top_intc_notific_base` | `+16` | top notific block base | HIGH |
| `intc_user_errtrig_base[56]` | `+24` | per-block errtrig reg base (`+8*a2`) | HIGH |
| `top_intc_masks[2]` | `+472` | MSI-X mask shadow (`[0]` used) | HIGH |
| `intc_user_errtrig_masks*` | `+480` | errtrig mask shadow (`+4*mask_index`) | HIGH |
| `user_errtrig_ens[56]` (192 B each) | `+680` | per-block ENS; `.nq @ +696+192*a2` | HIGH |

| Top MSI-X register (rel. `top_intc_msix_base`) | Offset | Role | Conf |
|---|---|---|---|
| set-mask | `+16` | OR mask shadow → mask vector | HIGH |
| clear-mask | `+24` | AND mask shadow → unmask vector | HIGH |
| mod-timer ctrl | `+40` | period bits`[27:24]`; reset bit `0x10` | HIGH |
| per-group interval | `+1024+8*grp` | low byte = interval (`grp<8`, val `<=0xFF`) | HIGH |
| per-vector ctrl | `+2060+16*vec` | `1` = mask / `0` = unmask | HIGH |

| 4-group errtrig register (`+grp<<12 +sub<<6 +REG`) | REG | Leaf | Conf |
|---|---|---|---|
| `int_cause_grp` | `+0` | `read_cause @0x478a20` | HIGH |
| `int_cause_set_grp` | `+8` | `set_cause @0x478950` | HIGH |
| `int_mask_grp` | `+16` | `mask_interrupt @0x478490` | HIGH |
| `int_mask_clear_grp` | `+24` | `unmask_interrupt @0x4785a0` | HIGH |
| `int_log_msk_grp` | `+56` | `mask/unmask_notification @0x478710/0x478830` | HIGH |

### Considerations

> **QUIRK —** the MARIANA NQ-id → mailbox-base table (`mariana_intc_nq_id_to_mb_base_map @.rodata 0x9f2160`, `u64[16]`) carries the **two-die-mesh high bit** on its upper half: blocks 0–3 use `0x8580000400 + 0x200*queue_id` (entries `[0..7]`), blocks 4–7 use `0x808580000400 + 0x200*queue_id` (entries `[8..15]`, `+0x800000000000`). `set_notific_queue_base_mariana` indexes it `[8*(block_idx > 3) + queue_id]` — so the high die's blocks resolve to the mesh-high-bit mailbox addresses automatically. This is the *same* `+0x800000000000` mesh marker the [SDMA band](arch-sdma.md) pins on the MARIANA embedded-semaphore AXI base (`0x802701800`); it is a Trn3-only address-space bit, absent on SUNDA and CAYMAN. A reimplementer who builds the table without the high half routes high-die NQ mailbox writes to the wrong die.

> **GOTCHA —** the four mask/cause leaves (`mask/unmask_interrupt`, `mask/unmask_notification`, `set/read_cause`) do **not** address the register directly from their group argument — they first resolve `(g, sub, bit)` through `intc_get_trigger_info @0x450080` and `get_intc_user_errtrig_mask_index @0x450020` (boundary callees), then form the address `errtrig_base[a2] + (g<<12) + (sub<<6) + REG`. The `<<12` group / `<<6` sub-index packing and the `g < 4` bound (`AWS_REG_MARIANA_INTC_4GRP_MSIX_UNIT_CTRL_COUNT`) are byte-firm, but the trigger-id → `(g, sub, bit)` decode itself lives in those two callees, not in these leaves. A reimplementer must reproduce the decoder (not traced here) before the register address is well-defined — the leaves are correct only *given* the resolved triple. **(Register packing HIGH; trigger-id decode boundary-owned, MED.)**

---

## 5. Per-Arch Divergences and the Q7 / udma Shims

### Purpose

Most of this band is arch-invariant (the NQ register window, §2/§3). The genuine per-arch divergences — the ones a reimplementer must special-case — are concentrated in three places: the `flat`/`indexed` NQ-layout selector (§3, runtime not arch), the MARIANA mesh-high-bit mailbox map (§4), and the **Q7 pooling-engine / udma-shim policy** that splits sunda-vs-cayman. This section pins those policy splits and the arch-neutral udma shims that ride the MARIANA NQ band.

### The Q7 swap-table and ucode policy

The Q7 (POOL pooling-engine) reserved-register and ucode path is the one place sunda and cayman diverge in *behaviour*, not just address:

```c
// SUNDA: aws_hal_q7_swap_table_sunda @0x46b7d0 — REAL 3-DWORD register write
function q7_swap_table_sunda(arch, lo_hi, b):
    base = aws_hal_arch_get_xt_local_reg_offset(arch, 2, 1)   // 0x44c970 — resolve XT-local base
    al_reg_write32(base + 0x83c, LODWORD(lo_hi))              // tpb_nx_local_regs.nx_rsvd_space0 (idx 527)
    al_reg_write32(base + 0x840, HIDWORD(lo_hi))              // nx_rsvd_space1 (idx 528)
    al_reg_write32(base + 0x844, b)                           // nx_rsvd_space2 (idx 529)

// CAYMAN: aws_hal_q7_swap_table_cayman @0x471470 — STUB, returns 0 (mechanism inactive)
function q7_swap_table_cayman(): return 0
// aws_hal_q7_swap_table_get_register_offsets_cayman @0x471480 — writes 0 to up to 3 out-ptrs
// aws_hal_q7_swap_file_io_table_cayman @0x4714b0 — STUB, returns 0
```

The CAYMAN Q7 *ucode* loader, by contrast, is **live** (a real `0x20a`-byte loader) — the swap-table being a no-op does not mean Q7 is absent:

```c
// aws_hal_q7_ucode_eng_init_cayman @0x4714c0 — POOL-engine (Q7) microcode loader
function q7_ucode_eng_init_cayman(.., a3, .., owner):
    pset = (owner == 0) ? 2 : (owner == 1) ? 5 : error("Invalid q7 owner %u")
    (iram_base, iram_size, dram_base, dram_size, ncores) = aws_hal_get_q7_params_cayman(pset)  // 0x47ae70
    if a3[1] > iram_size: return error("IRAM ucode too big!")
    if a3[3] > dram_size: return error("DRAM ucode too big!")
    for core in 0..ncores-1:
        write_padded(iram_base + core*stride, a3[0], a3[1], "POOL eng iram")   // 0x473eb0
        write_padded(dram_base + core*stride, a3[2], a3[3], "POOL eng dram")
```

### The MARIANA udma-M2M shims

The MARIANA NQ band (`0x463560..0x464aa2`) opens with two arch-neutral UDMA-M2M forwarding shims that share the band but belong to the AL UDMA driver, not the notific engine:

```c
// al_mla_udma_m2m_build_rx_descriptor_mariana @0x463560 — thin forwarder
function build_rx_descriptor(params):
    if params == NULL || *params == NULL:
        al_hal_log("%s: Invalid parameters (params=%p)"); return -22   // 0xFFFFFFEA
    return al_udma_m2m_build_rx_descriptor(params->q, params->size, params->buf, ...)  // 0x45cbb0

// al_mla_udma_addr_check_mariana @0x4635c0 — indirect through global addr_check hook
function addr_check(args):
    if addr_check_hook == NULL: return 0                  // .bss 0xcaeb68 ; installed out-of-band
    return (*addr_check_hook)(args)
```

### Considerations

> **QUIRK —** the CAYMAN Q7 *swap-table* is a no-op (`xor eax,eax; ret` at `0x471470`/`0x4714b0`, zero-writes at `0x471480`) while the SUNDA swap-table (`0x46b7d0`) is a real 3-DWORD write to `tpb_nx_local_regs.nx_rsvd_space0/1/2` (`base+0x83c/0x840/0x844`, register indices 527/528/529, `objdump`-confirmed offsets). The two are *not* interchangeable: a reimplementer who reads "swap-table is a stub" from the cayman path and skips the sunda register write breaks pooling-engine reserved-space programming on Trn1; one who reads "swap-table is real" from sunda and writes registers on cayman touches an inactive mechanism. The Q7 *ucode loader* is independent and live on cayman (`0x4714c0`) — the swap-table being absent does not retire the pooling microcode load.

> **NOTE —** the two MARIANA udma-M2M shims (`0x463560`, `0x4635c0`) are arch-neutral in body — byte-identical sunda/cayman siblings exist — and forward into the shared AL UDMA M2M driver (`al_udma_m2m_build_rx_descriptor @0x45cbb0`) or the global `addr_check` hook (`.bss @0xcaeb68`). They sit in the notific band only because the v4 registrar installs them adjacent to the notific leaves; they are not part of the NQ/INTC surface and their wire format is owned by the [UDMA M2M builder](../kernel/udma-m2m.md). A reimplementer documents them with the SDMA/UDMA band, not the notification band — they are co-located, not co-functional.

---

## Related Components

| Name | Relationship |
|---|---|
| `aws_hal_{notific,intc}_*` facade (`0x450420`–`0x450fcd`) | the generic arch-dispatch shims that tail-jump into this page's per-arch leaves (mechanism owned by [hal-registers](hal-registers.md)) |
| `al_hal_tpb_get_arch_type` (`0x44bca0`) | the arch gate every facade shim asserts on before dispatch (boundary; [hal-registers §1](hal-registers.md)) |
| `kaena_khal_register_funcs_v{2,3,4}` (`0x468740`/`0x46ed70`/`0x4622e0`) | install the SUNDA/CAYMAN/MARIANA notific+intc leaves into `kaena_khal` slots `+0x3B0..+0x4D8` |
| `aws_hal_notific_nq_write_hw_head_value_<arch>.part.0` (`0x6237d`/`0x623a0`/`0x6235a`) | out-of-band head-recompute helper the `read`/`reset` paths call to advance the HW head |
| `intc_get_trigger_info` (`0x450080`) / `get_intc_user_errtrig_mask_index` (`0x450020`) | resolve the `(group, sub, bit)` triple the §4 mask/cause leaves address with |
| `aws_hal_notific_nq_set_base_addr` (`0x451070`) | the NQ base programmer the MARIANA `intc_set_notific_queue_base` re-dispatches through |

## Cross-References

- [Notification Queue Engine](../kernel/notification-queues.md) — the **kernel half**: allocates the ring backing store and programs the same `0x28`-byte per-NQ register block (`BASE_ADDR_LO +0x100`…`HEAD +0x10c`) this userspace HAL drives; cross-validates the register window bit-for-bit
- [TopSP Notification Path](../kernel/topsp.md) — the single-NQ (`notific_1_queue`, `sel==1`) engine variant whose flat-layout NQ this page's `flat`-selector (§3) and coalescer-timer skip (§2) special-case
- [Per-Arch Device Layer: CSR and Register-Offset Accessors](arch-csr-offsets.md) — the `*_sunda`/`*_cayman`/`*_mariana` base/offset leaves and the per-arch register/constant values the notific engine's reg-handle resolves through
- [KaenaHal: Register and Reg-Offset Accessors](hal-registers.md) — the **mechanism** owner of the arch gate, the `kaena_khal` dispatch object, and the generic-facade shim shape (§1) this page's leaves sit behind
- [Per-Arch Device Layer: SDMA / UDMA M2M Engine](arch-sdma.md) — the sibling SDMA band sharing the `+0x800000000000` mesh high-bit (§4 NQ-mailbox map ↔ embedded-sem AXI base) and the arch enum; owns the udma-M2M shims (§5) that ride the MARIANA notific band
- [IOFIC Interrupt Model](../dma/iofic.md) — the AnnapurnaLabs IOFIC interrupt-group taxonomy the MSI-X 4-group cause/mask sub-block (§4) is the per-arch realisation of
- [back to index](../index.md)
