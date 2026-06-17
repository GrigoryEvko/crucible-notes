# KaenaHal: TPB/STPB Arch-Dispatch Shims

> *All addresses, offsets, symbol names, and vtable slot offsets on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`/`.rodata`/`.data` are VMA == file offset (`.bss` is `NOBITS`). The vendored HAL package is `KaenaHal-2.31.0.0` (Amazon `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`); the shim TUs root in `.../KaenaHal-2.31.0.0/.../src/src/common/{sp/aws_hal_sp.c, tpb/aws_hal_stpb*.c}`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every trampoline is byte-decoded (`lea kaena_khal / mov 0xNNN(%rax) / test / jmp *%rax`), its slot offset cross-checked against the matching `kaena_khal_register_funcs_v{2,3,4}` store site, and its assert strings (`"al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID"`, `"kaena_khal.khal_stpb.<member>"`) read verbatim from `.rodata`. · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

This page documents the **TPB/STPB arch-dispatch shim layer** of KaenaHal: the thin band of `aws_hal_sp_*` and `aws_hal_stpb_*` trampolines that the runtime calls to drive the five Tensor-Processing-Block compute engines (**PE**, **ACT**, **Pooling**, **DVE**, and the **SP/TopSP** sync-processor) without ever naming a silicon generation at the call site. Each trampoline performs the identical three-step dance: it (a) asserts the global HAL arch is bound — `al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID` ([hal-adapter §3](hal-adapter.md)), (b) loads a per-arch backend function pointer from a fixed slot in the process-global dispatch object `kaena_khal` (`@.bss 0xCAEB80`), and (c) **tail-calls** that pointer with the same arguments (`jmp *%rax`, args sibling-saved). No engine register logic lives in the shim itself — the real CSR programming is in the per-arch leaves (`aws_hal_stpb_{sunda,cayman,mariana}_{act,dve,pe,pooling}_*` and the SP analogs), which are owned by [arch-stpb](arch-stpb.md) and recorded here only as the *targets* of each slot.

The shim layer is the userspace-HAL twin of the `tdrv_arch_ops` vtable seam ([tdrv-arch-ops](tdrv-arch-ops.md)), one layer down and with a different binding model. Where `tdrv_arch_ops` is a 488-byte struct-of-pointers filled lazily on first use, `kaena_khal` is a **1960-byte (`0x7A8`, 245 eight-byte slots)** flat dispatch object filled *eagerly* at bring-up: `al_hal_tpb_set_arch_type` runs `kaena_khal_init`, which dispatches to one of `kaena_khal_register_funcs_v2 @0x468740` (→ SUNDA, arch 2), `_v3 @0x46ed70` (→ CAYMAN, arch 3), or `_v4 @0x4622e0` (→ MARIANA, arch 4). Each registrar writes the same slot set with its generation's leaf addresses. The shim addresses on this page span four contiguous `.text` cells — SP/TopSP + STPB notification (`0x457880..0x4582f9`), STPB-generic + ACT/DVE (`0x458340..0x45b474`), DVE/PE/Pooling (`0x45b480..0x45bf1c`), and STPB-Pooling DGE (`0x45bf20..0x45c1f0`) — and key into `kaena_khal` slots in the `+0x1F8..+0x388` and `+0x6D8..+0x748` bands.

Two facts repay reading before the tables. First, the layer is **not** uniformly trampolines: a minority of `aws_hal_stpb_*` exports are *real* straight-line bodies that touch CSRs directly and are **not** in the vtable at all — the master bring-up `aws_hal_stpb_init`, the SP orchestrator `aws_hal_sp_init`, `program_start`, `check_run_state`, `get_axi_offset`, the two `stochastic_rounding_*` writers, and the four STPB notification-enable bodies (`error`/`ham`/`evsem_hw_bp`/`dge`). Second, the trampolines split into **two NULL-slot policies** that are a genuine, byte-observable behavioral fork: DVE ops `__assert_fail` on a missing backend (mandatory engine), while PE / Pooling / SP-arch ops silently `return 0` (optional per arch). A reimplementer who installs one policy for all engines reproduces neither the abort surface nor the silent-success semantics. The page applies the recurring H3 vocabulary to four units: the dispatch object and the canonical trampoline; the SP/TopSP shims and the real `sp_init`; the STPB engine shims (ACT/DVE/PE/Pooling) and the two policies; and the STPB notification surface plus the real generic primitives.

For reimplementation, the contract is:

- **The canonical trampoline** — `assert(arch != INVALID); fp = kaena_khal[slot]; if(!fp) {assert | return 0}; return fp(args)`, emitted as a 6-step machine idiom (`push %rbx; mov %rdi,%rbx; call al_hal_tpb_get_arch_type; lea kaena_khal; mov 0xNNN(%rax); test; jmp *%rax`). The arch assert is the *only* validation in a pure trampoline; everything else is the backend's job.
- **The `kaena_khal` slot map** — the byte offset each shim indexes (SP block `+0x6D8..+0x748`; STPB-ACT `+0x200..+0x2E0`; DVE `+0x1F8`,`+0x208`,`+0x280..+0x2B0`; PE `+0x2E8..+0x328`; Pooling `+0x330..+0x388`), and the three registrars that fill them per generation. Slot offset = `symbol_addr − 0xCAEB80`.
- **The two NULL-slot policies** — DVE = fatal assert (`"kaena_khal.khal_stpb.dve_<x>"`); PE / Pooling / SP-style = no-op `return 0`. The split encodes a per-silicon capability matrix: PE/Pooling backends are legitimately absent on some generations; DVE backends never are.
- **The real (non-vtable) bodies** — `aws_hal_stpb_init`, `aws_hal_sp_init`, `program_start`, `check_run_state`, `get_axi_offset`, `stochastic_rounding_config/_enable`, and the four notification-enable functions are straight-line CSR/notific code, **not** trampolines, and must be reimplemented directly rather than dispatched.

| | |
|---|---|
| **Dispatch object** | `kaena_khal` (`@.bss 0xCAEB80`, nm `b kaena_khal`) — **1960 B / `0x7A8` / 245 eight-byte slots** |
| **Arch latch** | `hal_target` (`@.bss 0xCAEB60`, u32) read by `al_hal_tpb_get_arch_type @0x44bca0`; set by `al_hal_tpb_set_arch_type @0x44bc00` ([hal-adapter §3](hal-adapter.md)) |
| **Registrars** | `kaena_khal_register_funcs_v2 @0x468740` (SUNDA=2) · `_v3 @0x46ed70` (CAYMAN=3) · `_v4 @0x4622e0` (MARIANA=4) |
| **Arch enum** | `enum al_hal_tpb_arch_type {INVALID=0, INVALID_1=1, SUNDA=2, CAYMAN=3, MARIANA=4, NUM=5}` (DWARF `const_value`) |
| **Engine enum** | `enum NC_ENGINE_TYPE {PE=0, ACT=1, POOL=2, DVE=3, SP=4}` (DWARF; confirmed by `read_program_counter`'s 5-way switch) |
| **Shim cells (`.text`)** | SP/TopSP+notif `0x457880..0x4582f9` · STPB+ACT/DVE `0x458340..0x45b474` · DVE/PE/Pooling `0x45b480..0x45bf1c` · Pooling-DGE `0x45bf20..0x45c1f0` |
| **Slot bands used** | SP `+0x6D8..+0x748` · STPB-ACT `+0x200..+0x2E0` · DVE `+0x1F8`,`+0x208`,`+0x280..+0x2B0` · PE `+0x2E8..+0x328` · Pooling `+0x330..+0x388` |
| **Arch assert (all trampolines)** | `"al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID"` (`.rodata @0x81c890`) |
| **NULL-slot policies** | DVE → `__assert_fail("kaena_khal.khal_stpb.dve_<x>")` · PE/Pooling/SP-style → `return 0` |

> **CORRECTION (HAL-TPB-SHIMS) —** the map page ([hal-adapter §4](hal-adapter.md)) describes this leaf as owning "the `aws_get_*`/`aws_hal_*` per-arch register-offset shims … and the TPB engine-name table (`NEURON_ENG_NAMES`)." That scope statement is mis-routed: the `aws_get_*` register-offset shims dispatch through the **`tdrv_arch_ops.tpb_reg_offset` sub-vtable**, not `kaena_khal`, and are documented at [tdrv-arch-ops §4.1](tdrv-arch-ops.md); the `NEURON_ENG_NAMES` table belongs to [hal-registers](hal-registers.md). What this page actually owns — and what the four `L-HAL` cells under analysis contain — is the **`kaena_khal`-keyed `aws_hal_sp_*` / `aws_hal_stpb_*` engine-dispatch trampolines** plus the SP/STPB notification surface. The two shim families are structurally distinct (different dispatch object, different binding model); do not conflate them.

---

## 1. The Dispatch Object and the Canonical Trampoline

### Purpose

Every shim on this page resolves through one process-global object, `kaena_khal` (`@.bss 0xCAEB80`). It is a flat array of 245 eight-byte function-pointer slots (size `0x7A8 = 1960 B`, confirmed by `nm -S`), logically partitioned by the source into named sub-tables — `khal_sp`, `khal_stpb`, `khal_arch`, `khal_notific`, `khal_sdma` — that the assert strings expose by name (`"kaena_khal.khal_sp.regs_init"`, `"kaena_khal.khal_stpb.dve_init"`, …). A shim never branches on the arch value it asserts; it uses the assert purely as a liveness gate, then indexes a *fixed* slot whose contents were chosen once at bring-up. The arch-specificity lives entirely in *which pointer* the registrar wrote into that slot, never in the shim's control flow. This is the same indirection discipline as `tdrv_arch_ops` ([tdrv-arch-ops §2](tdrv-arch-ops.md)), with the binding moved earlier (eager, at `set_arch_type`) and the object flattened (one array, not a struct-of-sub-vtables).

### Entry Point

The slots are filled eagerly, before any shim fires, by the `set_arch_type → kaena_khal_init → register_funcs_vN` chain:

```text
al_hal_tpb_set_arch_type (0x44bc00)              ── once, at device identification
  └─ kaena_khal_init (0x462290)                  ── dispatches on the latched arch
       ├─ arch 2 (SUNDA)   → kaena_khal_register_funcs_v2 (0x468740)   ── fills *_sunda  leaves
       ├─ arch 3 (CAYMAN)  → kaena_khal_register_funcs_v3 (0x46ed70)   ── fills *_cayman leaves
       └─ arch 4 (MARIANA) → kaena_khal_register_funcs_v4 (0x4622e0)   ── fills *_mariana leaves
            e.g.  mov aws_hal_stpb_mariana_act_init, 0x2E0(kaena_khal)
                  mov ..._mariana_dve_init,          0x2B0(kaena_khal)

<any aws_hal_sp_* / aws_hal_stpb_* shim>          ── runtime dispatch
  └─ al_hal_tpb_get_arch_type (0x44bca0)          ── assert arch != INVALID
  └─ jmp *kaena_khal[slot]                         ── tail-call the installed leaf
```

### Algorithm

The canonical trampoline, byte-verified on `aws_hal_stpb_dve_regs_init` (`@0x45b480`) and structurally identical across all shims. The only per-shim variables are the slot offset and the NULL-slot policy:

```c
// CANONICAL TPB/STPB ARCH-DISPATCH TRAMPOLINE
// modelled on aws_hal_stpb_dve_regs_init @0x45b480 (kaena_khal.khal_stpb.dve_regs_init)
// machine idiom: push %rbx; mov %rdi,%rbx; call ...; lea kaena_khal; mov 0xNNN(%rax); test; jmp *%rax
function aws_hal_stpb_<engine>_<op>(handle, args...):       // shim @0x45bNNN
    if al_hal_tpb_get_arch_type() == AL_HAL_TPB_ARCH_TYPE_INVALID:   // 0x44bca0 reads hal_target@0xCAEB60
        __assert_fail("al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID",
                      ".../tpb/aws_hal_stpb_<tu>.c", <line>, __PRETTY_FUNCTION__)

    fp = *(void**)(&kaena_khal + SLOT)                     // SLOT = symbol_addr - 0xCAEB80, e.g. +0x208

    if fp == NULL:
        // --- POLICY DIVERGES HERE ---
        // DVE engine  : fatal — the backend is mandatory on every generation
        __assert_fail("kaena_khal.khal_stpb.dve_<op>", ".../aws_hal_stpb_dve.c", <line>, ...)
        // PE / Pooling : no-op — backend legitimately absent on some arches
        return 0

    return fp(handle, args...)                              // jmp *%rax — tail-call, args sibling-saved
```

Three shims carry extra validation *before* the trampoline body, layered on top of the canonical pattern (all return `0xFFFFFFEA` = `-22` = `-EINVAL` on failure, via `al_hal_log`):

```c
// aws_hal_stpb_dve_init @0x45b820  /  aws_hal_stpb_act_init @0x45b2e0  /  aws_hal_stpb_pooling_init @0x45c170
function aws_hal_stpb_<eng>_init(cfg):
    if cfg == NULL:
        al_hal_log(..., "%s: invalid input =%p\n", cfg); return -EINVAL      // 0xFFFFFFEA
    if cfg->inst_debug_level > 8:                          // dve/act read +0xC0; pooling reads +0xE0
        al_hal_log(..., "%s: invalid inst_debug_level=%u\n", lvl); return -EINVAL
    // ... then the canonical assert-arch → load-slot → tail-call body above
```

### Function Map

The infrastructure functions of the dispatch object (the registrars and the arch latch are boundary-owned; included for the slot-fill provenance):

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `kaena_khal` (object) | `0xCAEB80` | 245-slot dispatch array; every shim indexes it | CERTAIN |
| `kaena_khal_init` | `0x462290` | dispatch on latched arch → registrar | HIGH |
| `kaena_khal_register_funcs_v2` | `0x468740` | SUNDA fill — installs `*_sunda` leaves | CERTAIN |
| `kaena_khal_register_funcs_v3` | `0x46ed70` | CAYMAN fill — installs `*_cayman` leaves | CERTAIN |
| `kaena_khal_register_funcs_v4` | `0x4622e0` | MARIANA fill — installs `*_mariana` leaves | CERTAIN |
| `al_hal_tpb_get_arch_type` | `0x44bca0` | arch-liveness assert source (boundary) | CERTAIN |
| `__assert_fail@plt` | `0x03c880` | NULL-slot / arch-INVALID abort | CERTAIN |
| `al_hal_log` | `0x265b80` | `_init` validation log path (boundary) | CERTAIN |

### Considerations

> **QUIRK —** the trampoline asserts the arch but never *uses* it. `al_hal_tpb_get_arch_type()` returns the latched generation, but the shim discards the return value after the `!= INVALID` test — it does not index `kaena_khal` by arch, because the registrar already wrote the arch-correct pointer into the single fixed slot at bring-up. The assert is a liveness gate ("has anyone called `set_arch_type`?"), not a selector. A reimplementer who tries to reconstruct an `arch → backend` table *inside* the shim has misread the design: there is exactly one slot per op, and its value is the answer.

> **GOTCHA —** the dispatch object is filled eagerly, not lazily. Unlike `tdrv_arch_ops` ([tdrv-arch-ops §2](tdrv-arch-ops.md)), there is **no install-on-first-use guard** in these shims — a shim that runs before `al_hal_tpb_set_arch_type → kaena_khal_init` has populated the slots will either trip the arch-INVALID assert (because `hal_target` is still `0`) or, if the arch were somehow latched without the registrar running, read a NULL `.bss` slot and take the engine's NULL policy. The shipped ordering guarantees `set_arch_type` precedes any engine shim, so neither path opens in practice; a reimplementation that re-orders bring-up must preserve "latch-and-register before first engine dispatch."

---

## 2. SP / TopSP Shims and the Real `sp_init`

### Purpose

The Sync-Processor (SP) and its top-level instance (TopSP) form engine type **SP=4** — the unit that drives program start/stop signaling, host triggers, basic-block switching, and config-address reads for the TPB cluster. Its HAL surface (TU `src/common/sp/aws_hal_sp.c`) is **15 trampolines** through the `khal_sp` sub-table (`kaena_khal +0x6D8..+0x748`) plus **two real bodies**: the orchestrator `aws_hal_sp_init` and its thin alias `aws_hal_sp_topsp_init`. The SP trampolines follow the canonical pattern of §1 with the engine's no-op NULL policy; the slot block is contiguous, one 8-byte slot per op, in `aws_hal_sp.c` declaration order.

### Entry Point

```text
encd_ncfw_init / aws_hal_stpb_init (0x458350)
  └─ aws_hal_sp_topsp_init (0x458090)       ── 11-byte alias
       └─ aws_hal_sp_init (0x457f30)         ── REAL orchestrator (349 B), NOT a trampoline
            ├─ aws_hal_sp_regs_init        (0x457900) → khal_sp.regs_init         (+0x6E0)
            ├─ aws_hal_sp_ucode_seq_init   (0x457970) → khal_sp.ucode_seq_init    (+0x6E8)
            ├─ aws_hal_sp_dma_init         (0x457dd0) → khal_sp.dma_init          (+0x738)
            ├─ aws_hal_sp_release_run_stall(0x4579e0) → khal_sp.release_run_stall (+0x6F0)
            └─ aws_hal_notific_init (0x450a10, count=0xA)   ── notification bring-up
```

### Algorithm

`aws_hal_sp_init` (`@0x457f30`) is the one substantial SP body — a validate-then-sequence orchestrator that drives four SP trampolines then notification init. It is **not** in the vtable; reimplement it directly:

```c
function aws_hal_sp_init(handle, nt, u8_flag, init_flag):   // 0x457f30, aws_hal_sp.c
    if handle == NULL:
        al_hal_log(..., "invalid input =%p", handle); return -EINVAL
    if handle->sp_mem_handle == NULL or handle->sp_regs == NULL:   // +0x00 / +0x08
        al_hal_log(..., "invalid input sp_mem_handle=%p sp_regs=%p", ...); return -EINVAL
    if handle->inst_debug_level > 8:                        // +0xA8
        al_hal_log(..., "invalid inst_debug_level=%u", ...); return -EINVAL

    if init_flag:                                           // arg4 gates register bring-up
        if aws_hal_sp_regs_init(handle)      != 0: return err   // → khal_sp.regs_init      +0x6E0
        if aws_hal_sp_ucode_seq_init(handle) != 0: return err   // → khal_sp.ucode_seq_init +0x6E8
        aws_hal_sp_dma_init(handle)                              // → khal_sp.dma_init       +0x738
        aws_hal_sp_release_run_stall(handle)                    // → khal_sp.release_run_stall +0x6F0

    if nt != 0:
        if aws_hal_notific_init(nt, 0xA, u8_flag, 0, handle->ens_regs) != 0:   // +0x10, 0x450a10
            al_hal_log(..., "Failed to initialize notific. %d", rc)
    return 0

// aws_hal_sp_topsp_init @0x458090 — 11-byte body:
function aws_hal_sp_topsp_init(a1, a2, a3, a4):
    return aws_hal_sp_init(a1, a2, a3, a4)                  // bare tail-call alias
```

### Function Map — the SP/TopSP shim → slot table

The 15 SP trampolines, each pinned to its `kaena_khal` slot. Slot offset is `symbol_addr − 0xCAEB80`; all dispatch through the `khal_sp` sub-table. NULL policy is the no-op/return-0 family (SP is not the DVE mandatory class). The `_init` pair are the two real bodies.

| Shim (symbol) | Addr | Slot off | `khal_sp` member / role | Conf |
|---|---|---|---|---|
| `aws_hal_sp_topsp_evsem_notif_hw_bp_enable` | `0x457880` | `+0x6D8` | TopSP evsem HW-breakpoint notif enable | CERTAIN |
| `aws_hal_sp_regs_init` | `0x457900` | `+0x6E0` | program SP config/CSR registers | CERTAIN |
| `aws_hal_sp_ucode_seq_init` | `0x457970` | `+0x6E8` | init SP sequencer ucode | CERTAIN |
| `aws_hal_sp_release_run_stall` | `0x4579e0` | `+0x6F0` | release run/stall gate | CERTAIN |
| `aws_hal_sp_topsp_set_init_signal` | `0x457a50` | `+0x6F8` | assert TopSP init signal | CERTAIN |
| `aws_hal_sp_topsp_set_tsync_signal` | `0x457ac0` | `+0x700` | assert TopSP time-sync signal | CERTAIN |
| `aws_hal_sp_topsp_set_host_trigger` | `0x457b30` | `+0x708` | assert TopSP host trigger | CERTAIN |
| `aws_hal_sp_topsp_get_host_trigger_reg_offset` | `0x457ba0` | `+0x710` | host-trigger reg offset (no args) | CERTAIN |
| `aws_hal_sp_topsp_set_stop_signal` | `0x457c10` | `+0x718` | assert TopSP stop signal | CERTAIN |
| `aws_hal_sp_topsp_get_stop_signal_reg_offset` | `0x457c80` | `+0x720` | stop-signal reg offset (no args) | CERTAIN |
| `aws_hal_sp_topsp_get_basic_block_switch_reg_offset` | `0x457cf0` | `+0x728` | basic-block-switch reg offset | CERTAIN |
| `aws_hal_sp_topsp_read_config_addr` | `0x457d60` | `+0x730` | read TopSP config address | CERTAIN |
| `aws_hal_sp_dma_init` | `0x457dd0` | `+0x738` | init SP engine DMA path | CERTAIN |
| `aws_hal_sp_program_start` | `0x457e40` | `+0x740` | per-engine program start (args `handle,a2`) | CERTAIN |
| `aws_hal_sp_check_run_state` | `0x457ec0` | `+0x748` | poll SP run state | CERTAIN |
| `aws_hal_sp_init` | `0x457f30` | — (real) | orchestrator (349 B); not in vtable | CERTAIN |
| `aws_hal_sp_topsp_init` | `0x458090` | — (real) | 11-byte alias → `sp_init` | CERTAIN |

### Considerations

> **NOTE —** `khal_sp +0x6D0` (`instr_debug_level_set`) is the slot immediately *below* this block and belongs to an adjacent cell; the SP block on this page begins at `+0x6D8`. The `khal_sp` slots are contiguous `+0x6D8..+0x748` (15 trampoline slots), which is why the SP shim addresses are likewise contiguous `0x457880..0x457ec0` in `aws_hal_sp.c` declaration order — the source order, the address order, and the slot order all coincide for this engine.

> **QUIRK —** the SP handle (`aws_hal_sp.c:19`) and the master `aws_hal_stpb` handle share the first six fields verbatim (`{mem_handle, regs, ens_regs, addr, memcpy_fn, cookie}` at `+0x00..+0x28`), then diverge: the SP handle carries five NQ-index `u32`s and `inst_debug_level` at `+0xA8`, while the STPB handle carries per-engine 208-byte `eng_info` blocks. `aws_hal_sp_init` reads `+0x00/+0x08/+0x10/+0xA8` explicitly. A reimplementer should treat the SP handle as a distinct, smaller struct, not a prefix-cast of the STPB handle.

---

## 3. STPB Engine Shims — ACT / DVE / PE / Pooling and the Two Policies

### Purpose

The STPB ("Static/Scalar TensorProcessingBlock") engine HAL fans out to the four compute engines under `kaena_khal.khal_stpb`: **ACT** (activation, engine 1, TU `aws_hal_stpb_act.c`), **DVE** (Deep Vector, engine 3, `aws_hal_stpb_dve.c`), **PE** (processing-element array, engine 0, `aws_hal_stpb_pe.c`), and **Pooling** (engine 2, `aws_hal_stpb_pooling.c`). Each engine's per-op shims are canonical trampolines (§1) keyed into a contiguous `khal_stpb` slot block. The distinction a reimplementer must honor is the NULL-slot policy: DVE shims abort on a missing backend, while PE / Pooling / ACT-family shims silently `return 0`. The policy is byte-encoded — DVE shims carry a per-op `"kaena_khal.khal_stpb.dve_<op>"` assert string; PE/Pooling shims carry none and emit a `je <ret 0>` instead of `je <assert>`.

### Entry Point

```text
tpb_eng_init_hals_v2
  └─ aws_hal_stpb_init (0x458350)            ── master bring-up (REAL, §4), 2231 B
       ├─ aws_hal_stpb_pooling_init (0x45c170) → khal_stpb.pooling_init      (+0x368) [no-op NULL]
       ├─ aws_hal_stpb_act_init     (0x45b2e0) → khal_stpb.act_init          (+0x2E0) [no-op NULL]
       ├─ aws_hal_stpb_dve_init     (0x45b820) → khal_stpb.dve_init          (+0x2B0) [ASSERT NULL]
       ├─ aws_hal_stpb_pe_init      (0x45bc20) → khal_stpb.pe_init           (+0x318) [no-op NULL]
       └─ aws_hal_sp_init           (0x457f30)  ── §2
```

### Algorithm

The two policies, byte-decoded from the shim epilogues. The arch assert and slot load are identical; only the NULL handling differs:

```c
// DVE policy — backend mandatory; e.g. aws_hal_stpb_dve_ucode_seq_init @0x45b4f0 (+0x280)
function aws_hal_stpb_dve_<op>(handle, args...):
    if al_hal_tpb_get_arch_type() == INVALID: __assert_fail("...!= ..._INVALID", "aws_hal_stpb_dve.c", ...)
    fp = kaena_khal[DVE_SLOT]
    if fp == NULL:
        __assert_fail("kaena_khal.khal_stpb.dve_<op>", "aws_hal_stpb_dve.c", <line>, ...)   // FATAL
    return fp(handle, args...)

// PE/Pooling policy — backend optional; e.g. aws_hal_stpb_pe_regs_init @0x45ba10 (+0x2F0)
function aws_hal_stpb_pe_<op>(handle, args...):
    if al_hal_tpb_get_arch_type() == INVALID: __assert_fail("...!= ..._INVALID", "aws_hal_stpb_pe.c", ...)
    fp = kaena_khal[PE_SLOT]
    if fp == NULL:
        return 0                                                                            // SILENT no-op
    return fp(handle, args...)
```

The SUNDA registrar leaves at least one Pooling slot deliberately NULL — `kaena_khal_register_funcs_v2 @0x468740` writes `0xCAEEE0` (`pooling_hw_decode_table_init`, slot `+0x360`) as NULL while `_v3`/`_v4` install real `cayman`/`mariana` backends. The no-op policy is exactly what makes that legal: on SUNDA, `aws_hal_stpb_pooling_hw_decode_table_init` is a silent success, not an abort.

### Function Map — the STPB shim → slot table

The STPB engine trampolines, by engine, each pinned to its `khal_stpb` slot and NULL policy. The ACT-family real bodies (`write_profile`, `get_profile_bin[_with_flags]`, the stochastic/program/run-state primitives) are §4. Slot off = `symbol_addr − 0xCAEB80`.

| Engine | Shim (symbol) | Addr | Slot off | NULL policy | Conf |
|---|---|---|---|---|---|
| ACT | `aws_hal_stpb_act_instr_debug_level_set` | `0x459470` | `+0x210` | no-op | CERTAIN |
| ACT | `aws_hal_stpb_act_regs_init` | `0x4594d0` | `+0x218` | no-op | CERTAIN |
| ACT | `aws_hal_stpb_act_ucode_seq_init` | `0x459520` | `+0x2B8` | no-op | CERTAIN |
| ACT | `aws_hal_stpb_act_release_run_stall` | `0x4595a0` | `+0x2C0` | no-op | CERTAIN |
| ACT | `aws_hal_stpb_act_dma_init` | `0x4595f0` | `+0x2C8` | no-op | CERTAIN |
| ACT | `aws_hal_stpb_act_load_local` | `0x45b1b0` | `+0x2D0` | no-op (guards `idx<8` first) | CERTAIN |
| ACT | `aws_hal_stpb_act_hw_decode_table_init` | `0x45b250` | `+0x2D8` | no-op | CERTAIN |
| ACT | `aws_hal_stpb_act_init` | `0x45b2e0` | `+0x2E0` | no-op (guards `lvl≤8` first) | CERTAIN |
| DVE | `aws_hal_stpb_dve_reset_rng_seed` | `0x45b8f0` | `+0x1F8` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_instr_debug_level_set` | `0x45b390` | `+0x200` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_regs_init` | `0x45b480` | `+0x208` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_get_timestamp_inc` | `0x45b410` | `+0x278` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_ucode_seq_init` | `0x45b4f0` | `+0x280` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_release_run_stall` | `0x45b580` | `+0x288` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_dma_init` | `0x45b5f0` | `+0x290` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_write_parameter_ram` | `0x45b670` | `+0x298` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_write_tables` | `0x45b700` | `+0x2A0` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_hw_decode_table_init` | `0x45b780` | `+0x2A8` | **ASSERT** | CERTAIN |
| DVE | `aws_hal_stpb_dve_init` | `0x45b820` | `+0x2B0` | **ASSERT** (guards `lvl≤8` first) | CERTAIN |
| PE | `aws_hal_stpb_pe_instr_debug_level_set` | `0x45b960` | `+0x2E8` | no-op | CERTAIN |
| PE | `aws_hal_stpb_pe_regs_init` | `0x45ba10` | `+0x2F0` | no-op | CERTAIN |
| PE | `aws_hal_stpb_pe_ucode_seq_init` | `0x45ba60` | `+0x2F8` | no-op | CERTAIN |
| PE | `aws_hal_stpb_pe_release_run_stall` | `0x45bb70` | `+0x300` | no-op | CERTAIN |
| PE | `aws_hal_stpb_pe_dma_init` | `0x45bbc0` | `+0x308` | no-op | CERTAIN |
| PE | `aws_hal_stpb_pe_hw_decode_table_init` | `0x45bae0` | `+0x310` | no-op | CERTAIN |
| PE | `aws_hal_stpb_pe_init` | `0x45bc20` | `+0x318` | no-op | CERTAIN |
| PE | `aws_hal_stpb_pe_get_timestamp_inc` | `0x45b9c0` | `+0x320` | no-op | CERTAIN |
| PE | `aws_hal_tpb_bg_xpose_get_offset` | `0x45bc70` | `+0x328` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pool_instr_debug_level_set` | `0x45bcf0` | `+0x330` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_regs_init` | `0x45bd50` | `+0x338` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_ucode_seq_init` | `0x45bda0` | `+0x340` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_release_seq_run_stall` | `0x45be20` | `+0x348` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_release_eng_run_stall` | `0x45be70` | `+0x350` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_dma_init` | `0x45bec0` | `+0x358` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_hw_decode_table_init` | `0x45bf20` | `+0x360` | no-op (**NULL on SUNDA**) | CERTAIN |
| Pool | `aws_hal_stpb_pooling_init` | `0x45c170` | `+0x368` | no-op (guards `lvl≤8` first) | CERTAIN |
| Pool | `aws_hal_stpb_pooling_set_dge_dma_mapping` | `0x45bfb0` | `+0x370` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_get_dge_dma_mapping_register_offset_and_val` | `0x45c010` | `+0x378` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_set_dge_carveout` | `0x45c090` | `+0x380` | no-op | CERTAIN |
| Pool | `aws_hal_stpb_pooling_get_dge_carveout_register_offset_and_val` | `0x45c0f0` | `+0x388` | no-op | CERTAIN |

### Considerations

> **GOTCHA —** the two NULL policies are not interchangeable, and copying one onto all engines is a real bug. DVE's fatal assert says "a DVE backend is required on every supported generation; its absence is a configuration error worth aborting on." PE/Pooling's silent `return 0` says "this op may legitimately have no backend on this silicon (e.g. SUNDA leaves `pooling_hw_decode_table_init` NULL), and the caller treats `0` as success." A reimplementer who makes PE/Pooling abort will crash on SUNDA's intentionally-empty slots; one who makes DVE silently no-op will let a mis-registered runtime proceed with a dead vector engine. The split is a per-silicon capability matrix expressed in the shim's NULL arm — reproduce it verbatim.

> **QUIRK —** `aws_hal_stpb_dve_init` reads its `inst_debug_level` bound-check at arg `+0xC0` (192), but the DWARF `aws_hal_stpb_eng_info` struct's `+192` member is `impl_nq_index`, not a debug level — so the `_init` argument is a *wider* "engine create-params" struct that overlays `inst_debug_level` at `+0xC0`, not the bare 208-byte `eng_info` block. (MED confidence: the field mismatch is byte-firm; the exact wider struct type is not re-derived here.) Pooling's `_init` reads the same level at `+0xE0` (224), confirming the per-engine arg structs differ in layout. Do not assume a single uniform `_init` arg type across engines.

> **NOTE —** `aws_hal_tpb_bg_xpose_get_offset` (`@0x45bc70`, slot `+0x328`) is named `aws_hal_tpb_*`, not `aws_hal_stpb_pe_*`, but it physically lives in `aws_hal_stpb_pe.c` and dispatches through the PE slot block — it is a PE-engine background-transpose register-offset helper despite the prefix. The name is a source-side outlier, not a different engine.

---

## 4. The STPB Notification Surface and the Real Generic Primitives

### Purpose

Not every `aws_hal_stpb_*` export is a trampoline. A band of them are **real straight-line bodies** that touch CSRs and notification registers directly and are deliberately absent from `kaena_khal` — they implement arch-neutral logic (or, for `dge`, a SUNDA-only path) that does not vary by generation enough to warrant per-arch dispatch. These are the master bring-up `aws_hal_stpb_init`, the per-engine `program_start` / `check_run_state` / `get_axi_offset` primitives, the two `stochastic_rounding_*` writers, and the four notification-enable bodies (`error` / `ham` / `evsem_hw_bp` / `dge`). A reimplementer must build these directly; dispatching them through the vtable would be wrong because no slot exists.

### Algorithm

The four STPB notification-enable bodies compose an arch notification mask (via the `khal_arch` mask getters) and write TPB notific control registers — they are *not* in the `khal_stpb` vtable, only their mask sources are arch-dispatched:

```c
// aws_hal_stpb_error_notif_enable @0x458210 (70 B) — REAL, not a trampoline
function aws_hal_stpb_error_notif_enable(regs, nt, en, q):
    mask = aws_hal_arch_get_notific_error_mask()           // 0x473d60 → khal_arch +0xB0
    if en: aws_reg_update_tpb_notific_sw_queue_num3_errors_nt_(regs, q)   // 0x44c9f0
    aws_hal_notific_update_wr_buf_enable(nt, en ? mask : 0, mask)         // 0x450c50

// aws_hal_stpb_ham_notif_enable @0x458260 (75 B) — analogous, mask @0x473dd0 (khal_arch +0xB8)
// aws_hal_stpb_evsem_notif_hw_bp_enable @0x4582b0 (50 B) — mask @0x473d00 (khal_arch +0xA8),
//     → aws_hal_notific_update_hw_bp(handle, en ? mask : 0, mask)        // 0x450d50

// aws_hal_stpb_dge_notif_enable @0x4582f0 (9 B) — SUNDA-ONLY leaf
function aws_hal_stpb_dge_notif_enable(regs, en):
    return aws_reg_write_tpb_dge_notific_ctrl(regs, en)    // 0x44caf0 — asserts arch==2, RMW sets bit1
```

The two most-consulted real primitives are `get_axi_offset` (a one-line engine-relative → absolute AXI adder, the cell's single most x-ref'd export, ~20 NEFF/instruction-builder callers) and `program_start` (the 5-engine launch):

```c
// aws_hal_stpb_get_axi_offset @0x458e00 — REAL; the address-translation seam
function aws_hal_stpb_get_axi_offset(engine, rel):
    return rel + aws_hal_get_tpb_base(engine)              // engine-relative → absolute TPB AXI

// aws_hal_stpb_program_start @0x458c10 — REAL; per-engine launch (order POOL,ACT,PE,DVE,SP)
function aws_hal_stpb_program_start(handle, starts[5]):
    for eng in {POOL,ACT,PE,DVE,SP}:                       // arg constants 2,1,0,3,4
        if starts[eng] != 0xFFFFFFFFFFFFFFFF:              // -1 sentinel = "skip this engine"
            base = aws_hal_arch_get_xt_local_reg_offset(eng)
            write start_addr_lo/hi(base, starts[eng]); write start_ctrl(base) = 1
```

### Function Map — the real (non-vtable) STPB/SP bodies

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `aws_hal_stpb_init` | `0x458350` | 2231 B | master engine-cluster bring-up; sequences POOL/ACT/DVE/PE/SP inits + notif | CERTAIN |
| `aws_hal_stpb_regs_init` | `0x458340` | thin | tail-calls `aws_hal_stpb_evsem_notif_init(tpb_regs, evsem_nq_index)` | CERTAIN |
| `aws_hal_stpb_program_start` | `0x458c10` | — | 5-engine launch; `-1` sentinel skips | CERTAIN |
| `aws_hal_stpb_check_run_state` | `0x458d60` | — | sums run-state across 5 engine NX local-reg blocks | CERTAIN |
| `aws_hal_stpb_get_axi_offset` | `0x458e00` | — | engine-rel → absolute AXI adder (most x-ref'd) | CERTAIN |
| `aws_hal_stpb_stochastic_rounding_config` | `0x458e20` | — | 4-dtype loop → POOL/DVE/ACT seq stochastic-rnd regs; arch>3 adds PSUM | CERTAIN |
| `aws_hal_stpb_stochastic_rounding_enable` | `0x458ec0` | — | writes stochastic-rnd mode to 3 sequencers; arch>3 PSUM ctrl | CERTAIN |
| `aws_hal_stpb_read_program_counter` | `0x458fb0` | — | 5-way engine switch → `aws_reg_read_<eng>_spc`; default asserts | CERTAIN |
| `aws_hal_stpb_error_notif_enable` | `0x458210` | 70 B | error-notif mask compose + write (not in vtable) | CERTAIN |
| `aws_hal_stpb_ham_notif_enable` | `0x458260` | 75 B | HW-power-monitor notif enable | CERTAIN |
| `aws_hal_stpb_evsem_notif_hw_bp_enable` | `0x4582b0` | 50 B | evsem HW-breakpoint notif enable | CERTAIN |
| `aws_hal_stpb_dge_notif_enable` | `0x4582f0` | 9 B | DGE notif ctrl; **SUNDA-only** (asserts arch==2) | CERTAIN |
| `aws_hal_sp_init` | `0x457f30` | 349 B | SP orchestrator (§2) | CERTAIN |
| `aws_hal_tpb_cmn_act_init_common_profile_params` | `0x45c220` | 315 B | ACT profile-params packer (helper, not a shim) | HIGH |

### Considerations

> **GOTCHA —** `aws_hal_stpb_dge_notif_enable` (`@0x4582f0`) is the one notification body that is *not* arch-neutral: its callee `aws_reg_write_tpb_dge_notific_ctrl` (`@0x44caf0`) **asserts `arch == 2` (SUNDA)** and read-modify-writes bit 1 (`|= 2u`) of the `xt_local_reg` DGE notific-ctrl register. On CAYMAN/MARIANA this path is never taken (no static caller; indirect/entry only). A reimplementer porting forward must not assume DGE notification has a CAYMAN/MARIANA register equivalent at this seam — the SUNDA assert is structural, not incidental.

> **NOTE —** the four notification-enable bodies are reached from `notification_enable_v2`, and their masks come from the `khal_arch` mask getters (`get_notific_{error,ham,evsem}_mask` at `khal_arch +0xB0/+0xB8/+0xA8`), which *are* arch-dispatched. So the notification surface is a hybrid: the enable *logic* is arch-neutral first-party code, but the *mask values* it composes are per-generation, pulled through a different sub-table of the same `kaena_khal` object. The concrete per-arch mask values are owned by [arch-stpb](arch-stpb.md) and not re-derived here (LOW: the numeric masks are produced by per-arch `khal_arch` impls in other cells).

## Related Components

| Name | Relationship |
|---|---|
| `kaena_khal_register_funcs_v{2,3,4}` (`0x468740` / `0x46ed70` / `0x4622e0`) | the three registrars that fill every `khal_sp`/`khal_stpb` slot with the SUNDA/CAYMAN/MARIANA leaf addresses this page's shims tail-call |
| `al_hal_tpb_get_arch_type` (`0x44bca0`) / `al_hal_tpb_set_arch_type` (`0x44bc00`) | the arch latch every trampoline asserts and `kaena_khal_init` keys off ([hal-adapter §3](hal-adapter.md)) |
| `aws_hal_stpb_{sunda,cayman,mariana}_{act,dve,pe,pooling}_*` | the per-arch backend leaves installed into the slots — the real CSR programming, owned by [arch-stpb](arch-stpb.md) |
| `aws_hal_notific_init` (`0x450a10`) / `aws_hal_notific_update_wr_buf_enable` (`0x450c50`) / `_update_hw_bp` (`0x450d50`) | the notification primitives the real `sp_init` / STPB notif-enable bodies drive |
| `tpb_eng_init_hals_v2` / `encd_ncfw_init` | the bring-up callers that enter `aws_hal_stpb_init` / `aws_hal_sp_topsp_init` |

## Cross-References

- [Per-Arch Device Layer: STPB Engine-Init, DGE and Pooling](arch-stpb.md) — the per-arch backend leaves (`aws_hal_stpb_{sunda,cayman,mariana}_*`) this page's slots tail-call; owns the real engine CSR programming and the per-arch notific mask values
- [KaenaHal: Overview and Platform-Services Adapter](hal-adapter.md) — `al_hal_tpb_get/set_arch_type`, the arch latch (`hal_target @0xCAEB60`) every trampoline asserts, and `kaena_khal_init`
- [KaenaHal: Register and Reg-Offset Accessors](hal-registers.md) — the concrete `aws_reg_*` accessors the real bodies (`program_start`, `stochastic_rounding_*`, the notif-enable writers) call, and the `NEURON_ENG_NAMES` engine-name table
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](tdrv-arch-ops.md) — the parallel userspace arch vtable (`tdrv_arch_ops`) one layer up; the `aws_get_*` register-offset shims (a *different* dispatch object) live there, not here
- [TopSP Notification Path](../kernel/topsp.md) — the kernel-side TopSP notification engine the SP/TopSP shims (`set_init_signal`, `set_stop_signal`, `topsp_evsem_notif_hw_bp_enable`) program from userspace
- [back to index](../index.md)
