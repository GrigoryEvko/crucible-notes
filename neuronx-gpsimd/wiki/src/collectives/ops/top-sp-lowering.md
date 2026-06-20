# TOP_SP Collective Lowering

> **Scope.** This page documents how the collective pseudo-ops
> ([TriggerCollective `0xC8`](trigger-collective.md), [ALL_REDUCE](all-reduce.md),
> [SENDRECV `0xCB`](sendrecv.md), the barriers) are **lowered onto and orchestrated
> by the TOP_SP** — the per-NeuronCore Xtensa-NX *collective sequencer*
> (`engine_idx 5`). It covers (1) the TOP_SP's role, (2) the two host→TOP_SP trigger
> surfaces (the `kaena_khal` HAL vtable doorbell **and** the per-step `EVT_SEM`
> DMA-tail doorbell), (3) the SPAD `cc_op` program the TOP_SP NX core walks,
> (4) the per-pseudo-op lowering, (5) the `EVT_SEM`/tsync usage, and (6) the
> **TOP_SP-vs-SB2SB-vs-NCFW reconciliation**. Every offset/symbol/enum/field below is
> re-grounded to the shipped host runtime `libnrt.so.2.31.24.0`
> (BuildID `8bb57aba…c102e`), the NCFW JSON pretty-printer `libncfw.so`
> (BuildID `a98f8e1c…0db5`), the device ucode `libnrtucode_extisa.so`, and the
> shipped per-arch headers / address-map `.pkl`. `.text`/`.rodata` are
> `VMA == file-offset` in `libnrt.so`; `.data` carries **no** delta in this binary
> (`readelf -SW`: `.data` VMA `0xc07e00` == file-offset `0xc07e00`), so all the
> `objdump --start-address` reads below are direct.

---

## 0 · TL;DR

| # | Finding | Tag |
|---|---------|-----|
| 1 | The TOP_SP is the **on-device collective sequencer**, not a passive coordinator: an embedded Xtensa-NX core (`ENGINE_TOP_SP = 5`) that, once kicked, **walks a SPAD `cc_op` program** the host loaded into its SRAM/IRAM, issuing the DMA tail-pointer increments and `EVT_SEM` arrive/wait/dec ops that sequence each collective step. | HIGH / OBSERVED |
| 2 | There are **two** host→TOP_SP trigger surfaces: (a) the **NX-program-start doorbell** — value-`1` write to `LOCAL_REG + 0x15a0` (`= 0x615a0` cayman/mariana, `0x60848` sunda) dispatched through `kaena_khal` vtable **`+0x708`**; (b) the **per-step DMA-tail doorbell** — a write to the `EVT_SEM` `SEMAPHORE_INC` slot (`sp_base + 0x1800 + idx*4`) baked into the DMA descriptor ring. | HIGH / OBSERVED |
| 3 | The TOP_SP program is the **SPAD `cc_op` table** — a 4 KiB CTRL SPAD → SRAM + 32 KiB SLOT SPAD → TPB IRAM, built by `create_spad_ctrl_entry @0x232cd0` (packs `header.cc_op = 1` + the `algo_type`/flag/`channel_list`-or-`sema` union), staged in HBM, then DMA'd onto each TOP_SP. The 8-byte entry layout is recovered byte-exact from the binary's struct DB (`spad_ctrl_entry`). | HIGH / OBSERVED |
| 4 | **TOP_SP and NCFW cooperate** — the NCFW management firmware's runtime context is *rooted at the TOP_SP*: `libncfw`'s `ncfw_ctx_log` emits its whole context under the key **`ncfw_ctx_top_sp`**. The NCFW collective program *is* the TOP_SP's `cc_op` program; the TOP_SP NX core is the NCFW's per-NeuronCore executor. | HIGH (rooting) / INFERRED-STRONG (the host↔NX/LX handoff) |
| 5 | Per-pseudo-op: **collective** → an `algo_type`-tagged `cc_op` step-sequence (metaring/mesh/1-rank config builders); **barrier** → `EVT_SEM` arrive/wait/dec; **sendrecv** → a single SB2SB TX/RX leg the TOP_SP sequences and the [POOL/Q7 SB2SB kernel](s3d3-collective.md) executes. | HIGH (edges) / MED (per-world-size numeric algo) |

> **CORRECTION (vs the [ALL_REDUCE](all-reduce.md) table, row B).** That table lists the
> host-trigger as "via `kaena_khal` vtable `+0x710`". The byte-exact dispatch is:
> the **write** (`set_host_trigger`) goes through slot **`+0x708`**; **`+0x710`** is the
> *offset getter* (`get_host_trigger_reg_offset`). The all-reduce *prose* (its §7) already
> states this; only the summary-table cell uses the `+0x710` shorthand. This page is the
> authoritative split. (The earlier SX-CCL-03 anchor that named `+0x710` as "the trigger
> slot" is superseded.)

---

## 1 · The TOP_SP role — what `engine_idx 5` actually is

`ENGINE_TOP_SP` is a distinct TPB engine, byte-exact in the shipped per-arch ISA headers:

```c
// neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_common.h:145
//   (mariana :146, sunda :144, maverick :148 — identical value)
NEURON_ISA_TPB_NEURON_ENGINE_TOP_SP = 5,
```

`[HIGH / OBSERVED]` It is **not** `TPB_SP` (`engine 4`): `TPB_SP` is the per-NeuronCore
scalar-processor in the data plane; `TOP_SP` (`engine 5`) is the *top-level sequencer-
processor* — an embedded Xtensa-NX core with its own program counter (`top_sp_nx_spc_lo/hi`),
a local `EVT_SEM` semaphore unit, the global `timestamp_inc` tick, and a notification
fabric. Once the host kicks it (§3a), it walks the SPAD `cc_op` program (§5), per step
issuing the DMA tail-pointer increments that launch the SB2SB/DMA legs (§5d) and the
`EVT_SEM` arrive/wait/dec ops that gate the barriers (§6b), driving the global tsync (§8)
and posting completion as the elected leader/reporter (§6b). The host's own log strings
name it doing exactly this (all OBSERVED in `libnrt.so` `.rodata`):

| String (`@.rodata`) | Emitter | What it tells us |
|---|---|---|
| `TOP_SP will signal the proxy thread at the start of neff %s` (`@0x7fcd00`) | `enable_topsp_to_proxy_signal @0x207ad0` | the TOP_SP is an active agent that signals the host proxy at NEFF start |
| `received clearance from top_sp to execute enc_barrier for enc_context %p` (`@0x7f7e38`) | `enc_barrier_proxy_task::step @0x1ce830` | the TOP_SP is the barrier rendezvous authority that *gates* the host |
| `tp_inc_steps[%d] = m2s %d, s2m %d, repeat %d` (`@0x804248`) | `encd_dma_mark_end @0x237200` | each TOP_SP slot carries a list of tail-pointer-increment steps it issues |

> **NOTE.** "engine 5" here is the *architectural* engine class. The runtime enumerates a
> bounded number of *usable* TOP_SP instances per worker — see §4 (`tdrv_arch_get_num_topsp_*`).

See the end-to-end walk in [A Collective, End to End](../../orientation/collective-end-to-end.md)
and the NCFW side in [NCFW Main Dispatch Loop](../ncfw/main-dispatch-loop.md).

---

## 2 · The host-control vtable — `kaena_khal` HAL (byte-exact)

The host drives the TOP_SP through the **`kaena_khal`** HAL vtable — a `.bss` global at
`0xcaeb80`, populated at runtime by the per-arch HAL init `kaena_khal_register_funcs_v3
@0x46ed70`. The TOP_SP control slots were recovered from the cayman population block
`0x46f648 … 0x46f6e9` (`lea <fn> ; mov %rdx,<slot>(%rax)`): `[HIGH / OBSERVED]`

| Slot | Populated with (cayman) `@` | Role |
|---|---|---|
| `+0x6f0` | `aws_hal_sp_release_run_stall_cayman` `@0x46f64f` | release NX run-stall |
| `+0x6f8` | `aws_hal_sp_topsp_set_init_signal_cayman` `@0x46f65d` | NX init doorbell |
| `+0x700` | `aws_hal_sp_topsp_set_tsync_signal_cayman` `@0x46f66b` | tsync-start doorbell |
| **`+0x708`** | **`aws_hal_sp_topsp_set_host_trigger_cayman`** `@0x46f679` | **← collective-program START (the write)** |
| `+0x710` | `aws_hal_sp_topsp_get_host_trigger_reg_offset_cayman` `@0x46f687` | host-trigger *offset getter* |
| `+0x718` | `aws_hal_sp_topsp_set_stop_signal_cayman` `@0x46f695` | stop the NX program |
| `+0x720` | `…get_stop_signal_reg_offset_cayman` `@0x46f6a3` | stop offset getter |
| `+0x728` | `…get_basic_block_switch_reg_offset_cayman` `@0x46f6b1` | next-basic-block switch |
| `+0x730` | `…read_config_addr_cayman` `@0x46f6bf` | read NX `config_addr` |
| `+0x738` | `aws_hal_sp_dma_init_cayman` `@0x46f6cd` | SP DMA init |
| `+0x740` | `aws_hal_sp_program_start_cayman` `@0x46f6db` | SP program start |

The two top-level dispatchers select the per-arch implementation by `arch_type` and then
indirect through the vtable — this is where the `+0x708` (write) vs `+0x710` (offset)
split lives, byte-exact:

```c
// aws_hal_sp_topsp_set_host_trigger @0x457b30   (the WRITE)
void aws_hal_sp_topsp_set_host_trigger(void *hdl) {
    if (al_hal_tpb_get_arch_type() == 0) __assert_fail(...);   // 0 ⇒ no arch
    void (*fn)(void*) = kaena_khal[0x708/8];   // @0x457b44: mov 0x708(%rax),%rax
    if (!fn) __assert_fail(...);
    fn(hdl);                                   // @0x457b54: jmp *%rax
}

// aws_hal_sp_topsp_get_host_trigger_reg_offset @0x457ba0   (the OFFSET GETTER)
uint64_t aws_hal_sp_topsp_get_host_trigger_reg_offset(void) {
    if (al_hal_tpb_get_arch_type() == 0) __assert_fail(...);
    uint64_t (*fn)(void) = kaena_khal[0x710/8];  // @0x457bb4: mov 0x710(%rax),%rax
    return fn();                                  // @0x457bc4: jmp *%rax
}
```

`[HIGH / OBSERVED — both indirections (`48 8b 80 08 07 00 00` / `48 8b 80 10 07 00 00`)
read this session.]`

---

## 3 · The host→TOP_SP trigger path (two surfaces)

### 3a · The NX-program-START doorbell (the `kaena_khal` write)

Each `set_*` arch function computes the per-engine `LOCAL_REG` base via
`aws_hal_arch_cayman_get_xt_local_reg_offset(esi = 4 /* block class */, dl = 0)` — a jump
table whose index-0/`dl=0` arm returns `0x60000` (the `LOCAL_REG` aperture base) — then
adds a fixed control offset and calls `al_reg_write32(addr, 1)`:

```c
// aws_hal_sp_topsp_set_host_trigger_cayman @0x471b40   [HIGH / OBSERVED]
void set_host_trigger_cayman(void) {
    uint64_t base = aws_hal_arch_cayman_get_xt_local_reg_offset(4, 0);  // → 0x60000
    al_reg_write32(base + 0x15a0, 1);   // @0x471b50: lea 0x15a0(%rax) ; mov $1,%esi ; jmp al_reg_write32
}
```

The complete cayman `LOCAL_REG` doorbell set, with the immediate-return offset getters
(each a single `mov $imm,%eax ; ret`): `[HIGH / OBSERVED — register immediates read this session]`

| Doorbell | `LOCAL_REG + ofst` | getter returns (cayman) | write value | set fn |
|---|---|---|---|---|
| `init_signal` | `+0x1540` | — | `1` | `set_init_signal_cayman @0x471ae0` |
| `tsync_signal` | `+0x1560` | — | `1` | `set_tsync_signal_cayman @0x471b10` |
| **`host_trigger`** | **`+0x15a0`** | **`0x615a0`** `@0x47b080` | **`1`** | **`set_host_trigger_cayman @0x471b40`** |
| `stop_signal` | `+0x15c0` | `0x615c0` | `1` | `set_stop_signal_cayman @0x471b80` |
| `basic_block_switch` | `+0x15e0` | `0x615e0` | — | (getter only) |

So the **host-trigger doorbell** = a single 32-bit write of `1` to `LOCAL_REG + 0x15a0`
(`= 0x60000 + 0x15a0 = 0x615a0` on cayman). It is fired **once per executable** from
`encd_start_executable @0x2431c0` *after* the SPAD has been loaded (its callee set includes
`aws_hal_sp_topsp_set_host_trigger` alongside the two SPAD-size getters — §5a).

**Cross-arch** `[HIGH / OBSERVED]`:

| Arch | `host_trigger` getter `@` | value | base fn (`get_sp_base_addr`) |
|---|---|---|---|
| cayman | `aws_reg_cayman_…host_trigger_offset @0x47b080` | `0x615a0` | `0x8280000000` `@0x25aee0` |
| mariana | `…mariana_…_offset @0x477990` | `0x615a0` | `0x8280000000` `@0x257040` |
| sunda | `…sunda_…_offset @0x479120` | `0x60848` | `0xfffd0000000` `@0x25e610` |

> **QUIRK (sunda compaction).** Sunda does **not** share cayman's `LOCAL_REG` layout. Its
> `set_host_trigger_sunda @0x46c480` tail-jumps into a dedicated
> `aws_reg_sunda_write_top_sp_nx_local_reg_set_host_trigger @0x479110` that does
> `add $0x848,%rdi ; jmp al_reg_write32` (host_trigger `0x60848`); the adjacent stop path
> is `add $0x84c,%rdi` (`0x6084c`). The compacted `0x848` offset (vs `0x15a0`) is the
> defining sunda divergence.

### 3b · The per-step DMA-tail doorbell (baked into the inference blocks)

The instruction-block builder bakes a TOP_SP trigger **address** into the DMA descriptor
ring, so that a tail-pointer write hits the TOP_SP's `EVT_SEM` `SEMAPHORE_INC` slot. The
call chain (all disassembled): `[HIGH / OBSERVED]`

```c
// ib_create_one_block @0x2f7e50 / ib_add_inference_wait_v2 @0x2f6e20
//   → enc_get_topsp_trigger_addrs @0xfcb10          (loops over the used TOP_SPs)
//        count = ctx->num_topsp     // @0xfcb32: movslq 0x90(%rdi)
//        assert(*num_addrs < MAX_TOP_SP_PER_MLA)    // @0xfcbf3: __assert_fail
//        for each stream: encd_get_trigger_addr(ctx, strm_id, &out)

// encd_get_trigger_addr @0x23f150   [HIGH / OBSERVED — full disasm]
NRT_STATUS encd_get_trigger_addr(encd_context *ctx, int strm_id, uint64_t *out) {
    if (strm_id >= ctx->streams_n)        return /*assert*/;   // ctx+0x90
    if (strm_id >= encd_arch_get_max_num_streams()) return ...;
    encd_stream *s = &ctx->streams[strm_id];                   // ctx+0x98 + strm_id*0x138
    int sp_id      = *(int*)(*(void**)s + 0x6830);             // [[stream]+0x6830]
    uint64_t base  = encd_arch_get_sp_base_addr();             // cayman → 0x8280000000
    uint64_t ofst  = encd_arch_get_sp_sema_i_ofst(sp_id, ...); // the INC window, §4
    *out = base + ofst;                                        // = sp_base + 0x1800 + idx*4
    return 0;
}

// host-visible (BAR-mapped) alias  encd_get_trigger_host_addr @0x23f200
//   uses [[stream]+0x6820] + encd_arch_get_sp_sema_i_ofst_for_bar_mapped_vaddr @0x255920
```

⇒ the collective DMA descriptor's tail-pointer write targets the TOP_SP's `EVT_SEM`
`SEMAPHORE_INC` slot; that increment is the **per-iteration / per-step** trigger the
TOP_SP NX program polls (its `WAIT_FOR_SEM_GE`) to advance to the next step.

> **The two-level trigger model.** (a) §3a **starts** the TOP_SP program once per
> executable (`host_trigger` doorbell); (b) §3b is the **per-step** trigger the DMA legs
> use to hand off. The TOP_SP program, once started by (a), spins on `EVT_SEM` semaphores
> the DMA legs increment via (b). `[HIGH / OBSERVED the two distinct surfaces; the
> "program polls the sema the DMA increments" is INFERRED-STRONG — the GE-wait runs on the
> NX core, which has no shipped disassembler config, but the INC-trigger-address and the
> GE-wait target are the same `+0x1800` window.]`

---

## 4 · The TOP_SP-hosted `EVT_SEM` semaphore windows (byte-exact)

The TOP_SP base + per-op semaphore-window offsets, recovered from the cayman
implementations (each: `addr = sp_base + WINDOW + sema_idx*4`): `[HIGH / OBSERVED]`

| Function | Window | Op | Use |
|---|---|---|---|
| `cayman_get_sp_base_addr @0x25aee0` | `0x8280000000` | — | TOP_SP_0 SoC base |
| `cayman_get_sp_sema_r_ofst @0x25c130` | `+0x1000 + idx*4` | `SEMAPHORE_READ` | WAIT poll |
| `cayman_get_sp_sema_s_ofst @0x25c360` | `+0x1400 + idx*4` | `SEMAPHORE_SET` | init / reset |
| `cayman_get_sp_sema_i_ofst @0x25c2e0` | `+0x1800 + idx*4` | `SEMAPHORE_INC` | ARRIVE / trigger (§3b) |
| `cayman_get_sp_sema_d_ofst @0x25c260` | `+0x1C00 + idx*4` | `SEMAPHORE_DEC` | and-dec / reset |

These are **byte-identical** to the `EVT_SEM` aperture used by the cross-die DMA legs (see
[RDMA Cross-Die SBUF→SBUF P2P §5.1](../../dma/rdma-cross-die.md): `TPB_*_EVT_SEM` =
**256** hardware semaphores, four 4-B-stride windows `read 0x1000` / `set 0x1400` /
`inc 0x1800` / `dec 0x1C00`). Inside `cayman_get_sp_sema_i_ofst` the per-instance/die
routing is `rdx << 0x1e` (the instance) and, for `sp_id ≥ 8`, `rax << 0x2f` (**bit 47, the
DIE bit**) before the `lea 0x1800(%rax,%rbx,4)`.

The per-instance base table `cayman_sp_base_addresses @0x9d2020` (16 × `u64`, read raw):
`[HIGH / OBSERVED]`

```
[0] 0x8280000000   [1] 0x82c0000000  …  [7] 0x8440000000      // TOP_SP_0..7,  stride 0x40000000
[8] 0x808280000000 [9] 0x8082c0000000 … [15] 0x808440000000   // TOP_SP_8..15, + bit-47 DIE mirror
```

> **CORRECTION (vs SX-CCL-12 §4).** The backing survey transcribed the die mirror as
> `0x8280800000` (a bit-23 mirror). The actual `.rodata` bytes are **`0x808280000000`** —
> the **bit-47** die mirror (the `<< 0x2f` the sema functions apply), *not* bit-23.
> Re-grounded to the raw table here.

**TOP_SP count (runtime):** `tdrv_arch_get_num_topsp_cayman @0x30c690 = 16`,
`…_mariana @0x30d7b0 = 16`, `…_sunda @0x30af40 = 6`. `[HIGH / OBSERVED — `mov $imm,%eax`.]`
This is the runtime-usable count per worker (`MAX_TOP_SP_PER_MLA`); it differs from the
architectural CSR block view — the exact CSR-block↔runtime-index mapping is `[MED]`
(partition arithmetic via `nec_vil_get_num_topsp_per_vcore`). See the top_sp CSR block
detail at `control/csr/rdm-top-sp.md` (Part-13).

---

## 5 · The TOP_SP collective program — the SPAD `cc_op` table

### 5a · The SPAD load (host builds it, DMA's it onto the TOP_SP)

`encd_start_executable @0x2431c0` DMA-loads **two** device-memory regions onto each TOP_SP
(decompiled, OBSERVED): `[HIGH / OBSERVED — strings + size immediates]`

```c
// in encd_start_executable, per TOP_SP sp_engine (decompiled @0x2431c0)
if (!sp->hbm_only_ctrl_spad) {
    sz = encd_arch_get_sp_spad_ctrl_sram_size();              // cayman → 0x1000 (4 KiB)
    encd_devmem_load_with_dma_add_descriptor(
        ..., sp->spad,            sz, "TOPSP CTRL SPAD",
        sp->spe_config.neff.ctrl_spad_base.soc_addr);         // → SRAM
    nlog("[nec_dev %u] TOPSP #%u will load CTRL SPAD to SRAM (0x%lx)", ...);
}
if (!sp->hbm_only_slot0_spad) {
    sz = encd_arch_get_sp_spad_slot_tpb_iram_size();          // cayman → 0x8000 (32 KiB)
    encd_devmem_load_with_dma_add_descriptor(
        ..., (char*)sp->spad + 0x100000, sz, "TOPSP SLOT SPAD",
        sp->spe_config.neff.slot_spad_base[0].soc_addr);      // → TPB IRAM
    nlog("[nec_dev %u] TOPSP #%u will load SLOT SPAD to TPB IRAM (0x%lx)", ...);
}
// kick:  aws_hal_sp_topsp_set_host_trigger(sp)   (§3a)
```

| Region | Size getter `@` | Bytes | Destination | NCFW-ctx basic-block offset `@` |
|---|---|---|---|---|
| **CTRL SPAD** (the `cc_op` command table) | `cayman_get_sp_spad_ctrl_sram_size @0x25af00` | `0x1000` (4 KiB) | SP **SRAM** | `…ctrl_spad_offset @0x257000 → 0x42d0` |
| **SLOT SPAD** (per-slot data) | `cayman_get_sp_spad_slot_tpb_iram_size @0x25af10` | `0x8000` (32 KiB) | **TPB IRAM** | `…slot_spad_offset @0x257010 → 0x42a0` |

The configs are first staged in HBM (`Loaded TOP_SP #%d configs to HBM, op cnt %d`
`@0x805b70`, emitted by `encd_prep_topsp_config_end @0x2429b0`) and DMA'd onto the TOP_SP
via a dedicated init DMA (`Failed to allocate dma queue for TOPSP init` `@0x805c50`). The
SLOT SPAD is staged at `spad + 0x100000` (1 MiB into the staging buffer).

### 5b · The `cc_op` command-entry layout (recovered from the struct DB)

The 8-byte entry the TOP_SP NX core walks, byte-exact from `libnrt.so`'s IDA struct DB:
`[HIGH / OBSERVED — struct member bitfields]`

```c
typedef struct {                 // spad_ctrl_entry  (size 8)
    struct { uint8_t cc_op:1;    //  byte0 bit0  — the "active cc-op" flag
             uint8_t :7; } header;
    struct {                     // spad_ctrl_cc_op_entry  (size 7, at +1)
        uint8_t algo_type:4;     //  byte1 bits[0:3]   — enc_alg_type (RING/HIER/MESH/KANGARING/…)
        uint8_t algo_sub_type:3; //  byte1 bits[4:6]
        uint8_t trigger_next:1;  //  byte1 bit7        — chain to next entry
        uint8_t reporter:1;      //  byte2 bit0        — this TOP_SP posts completion
        uint8_t ring_wait_complete:1;  // byte2 bit1   (complete_prev)
        uint8_t ring_send_complete:1;  // byte2 bit2   (complete)
        uint8_t safe_mode:1;     //  byte2 bit3
        uint8_t unique_tensors:1;//  byte2 bit4
        uint8_t :3;
        uint8_t  __reserved;     //  byte3
        union {                  //  bytes4..7
            uint32_t channel_list;                        // ring/kangaring
            struct { uint16_t sema_shift_offset, sema_mask; }; // mesh
        };
    } cc_op;
} spad_ctrl_entry;
```

This is the same record `libncfw`'s `ncfw_log_spad_ctrl_cc_op_entry` decodes on the device
side — it emits exactly these JSON keys: `"cc_op"`, `"algo_type"`, `"algo_sub_type"`,
`"trigger_next"`, `"reporter"`, `"ring_wait_complete"`, `"ring_send_complete"`,
`"channel_list"`, `"sema_shift_offset"`, `"sema_mask"` (4 arch copies:
`…_entry`/`_0`/`_1`/`_2`). `[HIGH / OBSERVED — string refs in libncfw `.rodata`.]`

### 5c · The packer — `create_spad_ctrl_entry @0x232cd0`

The host packs each entry as a single 8-byte word; the cc_op bit positions in the packer
**match the struct DB exactly** (the packer's `<<8`/`<<12`/`<<15` land the fields where the
firmware decoder reads them): `[HIGH / OBSERVED — decompilation + struct DB cross-check]`

```c
// create_spad_ctrl_entry(channel, mark_first, mark_continue, sema_shift_offset, sema_mask)
//   @0x232cd0   (decompiled; source /opt/workspace/KaenaRuntime/tdrv/encd.c:0xB31)
spad_ctrl_entry v = {0};
// — header WORD (byte0+byte1): bit0=cc_op active, bits[8:11]=algo_type, bits[12:14]=sub_type, bit15=trigger_next —
*(uint16_t*)&v.header = (trigger_next << 15) | (algo_sub_type << 12) | (algo_type << 8) | 1;
// — cc_op byte2 flag nibble —
*((uint8_t*)&v.cc_op + 1) =
    ((unique_tensors << 4) | (permute_chain << 3) | (ring_wait_complete << 2)
     | (ring_send_complete << 1) | reporter) & 0x1F;
// — the union (channel bitmask OR mesh sema pair) —
if (channel->ring)                    v.cc_op.channel_list = bitmask_of_local_channels;
else /* mesh leg */ { v.cc_op.sema_shift_offset = sema_shift_offset;
                      v.cc_op.sema_mask         = sema_mask; }
```

The `channel_list` bitmask is built by walking `ring->channels[]` and OR-ing `1 << abs_id`
for every channel whose owning `sp->idx` matches this TOP_SP
(`if (sp->idx == channel->sp_engine->sp->idx) channel_list |= 1 << abs_id;`). The
`ring_send_complete` / `ring_wait_complete` flags are set by comparing a neighbour
channel's `sp->idx` against this one (`> idx ⇒ send_complete` needs
`neff.complete.addr.soc_addr`; `< idx ⇒ wait_complete` needs
`neff.complete_prev.addr.soc_addr`). `[HIGH / OBSERVED — decompilation.]`

> **CORRECTION / upgrade (vs SX-CCL-12 §5b).** The survey rated the packer↔decoder bit-
> equivalence "MED". With the struct DB in hand, the equivalence is **HIGH**: the packer's
> word bits map 1:1 onto `spad_ctrl_entry`'s declared bitfields (`algo_type` byte1[0:3],
> `algo_sub_type` byte1[4:6], `trigger_next` byte1[7], the byte2 flag nibble).

### 5d · The per-entry "mark" + the chain markers

`encd_dma_mark_end @0x237200` "marks" each TOP_SP DMA step into the SPAD table and logs the
full `cc_op` field roster verbatim — byte-for-byte the §5b record:
`[HIGH / OBSERVED]`

```
[nec_dev %2u, TOPSP %d, op %d] CTRL mark -alg %d-subalg %d-trignext %d-chlist %d
   -reporter %d-sema_shift_offset %u-sema_mask %u-safe_mode %d            (@0x804088)
[nec_dev %2u, TOPSP %d, op %d] SLOT CONTINUE mark -alg %d-subalg %d-trignext %d
   -chlist %d-sema_shift_offset %u-sema_mask %u-safe_mode %d              (@0x804198)
[nec_dev %2u, ch %d, op %2d, slot %d] tp_inc_steps[%d] = m2s %d, s2m %d, repeat %d  (@0x804248)
```

The `trigger_next` / `complete_prev` chaining is finalized by `mark_spad_slot_final_entry
@0x22fc20`: `[HIGH / OBSERVED]`

```
[nec_dev %d TOPSP %d] Adding CONTINUE mark                                       (@0x8029e0)
[nec_dev %d TOPSP %d] Adding END mark with compl address 0x%lx (semaphore bits : %lx)  (@0x802a78)
   // asserts: num_compl_assert_addr <= TPB_COMPL_ADDR_MAX_NUM
   //          compl_addr_sema_bits == first_compl_addr_sema_bits
```

So each slot carries a list of **tail-pointer-increment** steps (`m2s` + `s2m` descriptor
counts, with a `repeat`) — the concrete "TOP_SP walks the steps issuing the DMA legs"
mechanism. The full SPAD `cc_op` table mechanics live in
[NCFW spad-ctrl `cc_op` Table + tsync](../ncfw/spad-ccop-tsync.md); the `cc_op` word
enumeration in [Collective-Type + `cc_op` Enum Reference](collective-enums.md).

---

## 6 · Per-pseudo-op-class lowering onto TOP_SP actions

### 6a · Collective → an `algo_type`-tagged step sequence

The chosen algorithm class builds its **own** TOP_SP config, with `algo_type` set to the
[`enc_alg_type`](collective-enums.md) (RING / HIER / MESH / KANGARING / …). The composer →
config-builder edges are confirmed in the callee sets: `[HIGH / OBSERVED — call edges + error strings]`

| Primitive (`__handle_semaphore_init`) | Sema init | TOP_SP config builder | Error string |
|---|---|---|---|
| `enc_metaring_primitive @0x14be70` | `encd_alg_init_metaring_semaphores` | `encd_populate_metaring_topsp_config @0x240bf0` | `failed to populate metaring topsp config for type(%d)` |
| `enc_mesh_primitive @0x14a3a0` | `encd_alg_init_mesh_semaphores` | `encd_populate_mesh_topsp_config @0x240330` | `failed to populate mesh topsp config for mesh` |
| `enc_1rank_primitive @0x14cd10` | `encd_alg_init_metaring_semaphores` | `encd_populate_metaring_topsp_config` (1-rank degenerate ring) | `failed to populate metaring topsp config 1-rank ring` |

`encd_populate_metaring_topsp_config @0x240bf0` delegates the heavy lifting to
`prep_metaring_topsp_config @0x2331d0` (sets `metaring->channel_config_init`); the bracket
is `encd_prep_topsp_config_start @0x240df0` / `encd_prep_topsp_config_end @0x2429b0`. A
**HIER** all-reduce thus becomes *multiple chained* TOP_SP `cc_op` entries (intra-RS ring
leg → inter all-reduce mesh/ring leg → intra-AG ring leg — see [ALL_REDUCE](all-reduce.md)),
each an `algo_type`-tagged entry chained by `trigger_next` / `complete_prev`. The per-leg
*numeric* `enc_alg_type` per `(world-size, topology)` is `[MED]` (algorithm-selection
thresholds — see all-reduce). The run_state / scoreboard cursor the device walks per step is
in [Ring + Kangaring](../ncfw/ring-kangaring.md).

### 6b · Barrier (`CORE_BARRIER 0xD8` / `SYNC_BARRIER 0xD5`) → `EVT_SEM` ops

Barriers lower into `EVT_SEM` `arrive(INC +0x1800)` / `wait(GE, read +0x1000)` /
`dec(+0x1C00)` ops on the TOP_SP-hosted 256-semaphore array (§4). The TOP_SP **gates** the
host's barrier (`received clearance from top_sp to execute enc_barrier`, §1) — it is the
rendezvous authority. One TOP_SP per VNC is elected **leader** that posts completion,
matching `cc_op.reporter`: `[HIGH / OBSERVED]`

```c
// encd_get_topsp_is_leader @0x253420
bool encd_get_topsp_is_leader(encd_ctx *ctx, int topsp_idx) {
    if (topsp_idx >= ctx->num_topsp /*+0x80*/) __assert_fail(...);
    // per-TOP_SP struct stride = 0x6878
    return *(uint8_t*)((char*)ctx->topsp_arr/*+0x88*/ + topsp_idx * 0x6878);
}
// companions: encd_get_topsp_expected_notif_count @0x253460,
//             encd_get_topsp_cc_op_reports_count   @0x2533e0
```

Semaphore allocation is bounded by `MAX_TOP_SP_SEMAPHORES`
(`alloc_sp_semaphores @0x234aa0`: `Failed to allocate %d semaphores in TOP_SP #%d. There
may be too many communicators`). See the barrier opcodes in
[TriggerCollective (0xC8)](trigger-collective.md).

### 6c · Sendrecv (`0xCB`) → a single SB2SB TX/RX leg

`SENDRECV` lowers to `direct_send` / `direct_recv` → the SB2SB iDMA copy. The TOP_SP
sequences the tail-pointer increment that launches the leg (§3b / §5d's `tp_inc_steps`); the
[POOL/Q7 SB2SB kernel](s3d3-collective.md) does the byte movement. A ring/kangaring step is
*itself* a sendrecv, so the TOP_SP ring program **is** a sequence of sendrecv legs. See
[SENDRECV (0xCB)](sendrecv.md). `[HIGH structure / device leg is POOL/Q7, §7.]`

---

## 7 · TOP_SP-vs-SB2SB-vs-NCFW reconciliation

| Layer | Engine | Role | Evidence |
|---|---|---|---|
| **TOP_SP** | Xtensa-NX, `engine 5` | the per-NeuronCore **collective sequencer / orchestrator** — walks the `cc_op` program, issues DMA-tail increments + `EVT_SEM` ops, drives tsync, gates barriers, posts completion as leader | §1–§6, all OBSERVED |
| **SB2SB kernel** | POOL / Q7 | the **data plane** — runs the actual SBUF→SBUF iDMA copy; **not** the TOP_SP | §7b |
| **NCFW** | management core (scalar Xtensa-LX) | the **firmware whose per-TOP_SP context the TOP_SP NX core executes** | §7c |

### 7b · SB2SB = the data plane (NOT the TOP_SP)

`libnrtucode_extisa.so`'s device decoder runs the copy with the Pool/Q7 pre-sync handshake
— and crucially the device decode set carries **no** trigger ops: `[HIGH / OBSERVED]`

```
P%i: Decode : SB2SB_Collective                                                  (@0x551c6)
P%i: SB2SB_Collective : total_src_nelem = %zu, total_dst_nelem = %zu, dtype=%d  (@0x550e8)
P%i: SB2SB Pre-sync: This TPB (NC %u) is letting NC %u know that we are ready to receive  (@0x552a6)
P%i: SB2SB Pre-sync: remote_pool_xt_addr=0x%x, remote_q7_xt_addr=0x%x, sb2sb_ready_to_rece (@0x55306)
// ABSENT from the device decoder: "TriggerAllReduce", "TriggerCollective"
```

So: the TOP_SP **sequences** (issues the tail-pointer that launches the copy); the POOL/Q7
SB2SB kernel **moves the bytes**. Two engines, two roles.

### 7c · NCFW = the firmware whose per-TOP_SP context the TOP_SP runs

`libncfw`'s top-level context logger `ncfw_ctx_log @0x19f01` emits its **entire** runtime
context under the key **`ncfw_ctx_top_sp`** (`@0x65685`, + arch copies `_0`/`_1`/`_2`). The
NCFW collective context — the `spad_ctrl`/`cc_op` record (§5b), the ring/mesh/hier algo
ctxs each carrying a `"run_state"` phase cursor (`ncfw_log_algo_ring_ctx`,
`ncfw_log_algo_hierarchical_ctx`, both referencing `"run_state" @0x65621`), the dev tsync
struct (`ncfw_log_configs_dev_tsync`: `timestamp_local` / `timestamp_tpb` /
`timestamp_tpb_val`), the neff/leader config (`ncfw_log_neff_configs`: `ctrl_spad_base` /
`slot_spad_base_%d` / `"leader"`) — is a **per-TOP_SP** context. `[HIGH / OBSERVED that the
ctx key is `ncfw_ctx_top_sp` and the fields are present.]`

i.e. the NCFW and the TOP_SP are **not** competing orchestrators: the NCFW collective
program **is** the TOP_SP's `cc_op` program, and the TOP_SP NX core is the NCFW's
per-NeuronCore **executor**. `["TOP_SP executes the NCFW firmware" is INFERRED-STRONG — the
host↔device-core handoff crosses the Xtensa-NX/LX cores (no shipped LX disassembler config;
NCFW mgmt core = scalar Xtensa-LX, not FLIX), but the data-structure rooting pins the
relationship.]`

> **One line.** host (`libnrt` `enc_*`) lowers the pseudo-op → builds the NCFW/TOP_SP SPAD
> `cc_op` program → DMA-loads it onto the TOP_SP (CTRL→SRAM, SLOT→TPB IRAM) → kicks it via
> the `LOCAL_REG + 0x15a0` host_trigger doorbell → the TOP_SP walks the program, per-step
> incrementing `EVT_SEM` semaphores (`sema_i +0x1800`) the DMA legs also bump (§3b),
> launching SB2SB POOL/Q7 copies and rendezvousing via the `EVT_SEM` array → posts
> completion as leader → signals the host proxy.

The maverick (v5) TOP_SP cluster is **header-OBSERVED only**: the maverick address-map
`.pkl` exposes a `…_TOP_SP_CLUSTER0_IF_STREAM2AXI_0` REGFILE (`base 0xc000055000`, the
stream-to-AXI DMA command-inject path) corroborating the "TOP_SP issues DMA tail-pointer
increments" role — but the v5 doorbell/sema interiors are **INFERRED** (v2–v4 are the
byte-grounded arches above).

---

## 8 · `EVT_SEM` + `timestamp_inc` / tsync (TOP_SP drives the global tick)

- **The `EVT_SEM` array** is the TOP_SP's per-instance 256-semaphore unit (§4) — the
  substrate for both the collective step semaphores (the `sema_i` the DMA legs increment)
  and the barrier rendezvous (§6b). `[HIGH / OBSERVED]`
- **The tsync START is a TOP_SP doorbell.** `encd_ncfw_tsync_start @0x2526e0` calls
  `aws_hal_sp_topsp_set_tsync_signal @0x457ac0` (at call-site `0x252797`) — writes `1` to
  `LOCAL_REG + 0x1560`. The one-time NX init is `encd_ncfw_configure_device_init @0x230c70`
  → `aws_hal_sp_topsp_set_init_signal` (`LOCAL_REG + 0x1540`) + `read_config_addr` +
  `get_ack_evt_addr_for_topsp_engine`. `[HIGH / OBSERVED — callee sets + the §3 offsets.]`
- **So the TOP_SP is the global time-sync anchor:** it hosts the `EVT_SEM` array, owns the
  `timestamp_inc` tick, and its `tsync_signal` doorbell starts the global timestamp
  broadcast/alignment. The NCFW `dev_tsync` struct (`timestamp_local` / `timestamp_tpb` /
  `timestamp_tpb_val`, §7c) is the firmware's read/align of that counter.
  `[doorbell HIGH / OBSERVED; the `timestamp_inc → dev_tsync` alignment is MED/INFERRED —
  the read/compare runs on the NX/LX cores.]`

---

## 9 · Reimplementation checklist (byte-grounded constants)

| Constant | Value | Source `@` |
|---|---|---|
| `ENGINE_TOP_SP` | `5` | `aws_neuron_isa_tpb_common.h:145` |
| `kaena_khal` vtable slot — host_trigger **set** | `+0x708` | `kaena_khal_register_funcs_v3 @0x46f679`; dispatcher `@0x457b44` |
| `kaena_khal` vtable slot — host_trigger **offset getter** | `+0x710` | `@0x46f687`; dispatcher `@0x457bb4` |
| host_trigger `LOCAL_REG` offset | `+0x15a0` (cayman/mariana `0x615a0`; sunda `0x60848`) | `set_host_trigger_cayman @0x471b50`; getter `@0x47b080` |
| init / tsync / stop doorbells | `+0x1540` / `+0x1560` / `+0x15c0` | `@0x471af0` / `@0x471b20` / `@0x471b90` |
| `EVT_SEM` windows (read/set/inc/dec) | `+0x1000` / `+0x1400` / `+0x1800` / `+0x1C00` (`+ idx*4`) | `@0x25c161` / `@0x25c391` / `@0x25c311` / `@0x25c291` |
| TOP_SP SoC base (cayman) / die mirror | `0x8280000000`, stride `0x40000000`, bit-47 die | `cayman_sp_base_addresses @0x9d2020` |
| runtime TOP_SP count | cayman 16 / mariana 16 / sunda 6 | `@0x30c690` / `@0x30d7b0` / `@0x30af40` |
| CTRL SPAD → SRAM size | `0x1000` (4 KiB) | `@0x25af00` |
| SLOT SPAD → TPB IRAM size | `0x8000` (32 KiB) | `@0x25af10` |
| NCFW-ctx ctrl/slot SPAD offsets | `0x42d0` / `0x42a0` | `@0x257000` / `@0x257010` |
| per-TOP_SP struct stride | `0x6878` | `encd_get_topsp_is_leader @0x25342b` |
| stream stride / sp_id load / BAR alias | `0x138` / `[[stream]+0x6830]` / `+0x6820` | `encd_get_trigger_addr @0x23f172/0x23f183`; host variant `@0x23f223` |
| `cc_op` entry size / header bit0 | `8` B / `.cc_op = 1` | struct DB `spad_ctrl_entry` / packer `@0x232cd0` |

---

### Cross-references

- [TriggerCollective (0xC8)](trigger-collective.md) · [ALL_REDUCE](all-reduce.md) ·
  [S3D3 Collective (SB2SB, 0xBF)](s3d3-collective.md) · [SENDRECV (0xCB)](sendrecv.md)
- [Collective-Type + `cc_op` Enum Reference](collective-enums.md)
- [NCFW Main Dispatch Loop](../ncfw/main-dispatch-loop.md) ·
  [NCFW spad-ctrl `cc_op` Table + tsync](../ncfw/spad-ccop-tsync.md) ·
  [Ring + Kangaring](../ncfw/ring-kangaring.md)
- [RDMA Cross-Die SBUF→SBUF P2P](../../dma/rdma-cross-die.md) ·
  TOP_SP CSR block: `control/csr/rdm-top-sp.md` (Part-13)
- [A Collective, End to End](../../orientation/collective-end-to-end.md)
