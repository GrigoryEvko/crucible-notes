# KaenaHal: Register and Reg-Offset Accessors

> *All addresses, offsets, symbol names, and `kaena_khal` slot offsets on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`/`.rodata`/`.data` are VMA == file offset (`.bss` is `NOBITS`). The vendored HAL package is `KaenaHal-2.31.0.0` (Amazon `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`); the accessor TUs root in `.../KaenaHal-2.31.0.0/.../src/src/common/arch/{aws_hal_arch_offsets.c, aws_hal_arch_regs.c, aws_hal_arch_dma.c, al_hal_tpb_arch.c}`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every trampoline's `kaena_khal` slot is byte-decoded from its indirect-call site and cross-checked against the per-fn assert string (`"kaena_khal.khal_arch.<member>"`); every inlined MMIO offset is `objdump`-verified; the arch gate and engine-enum/name tables are decoded from the `al_hal_tpb_arch.c` bodies and the `NEURON_ENG_*` backing arrays. · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

This page documents the **register / reg-offset accessor surface** of KaenaHal: the `aws_hal_get_*` and `aws_reg_*` functions (TUs `aws_hal_arch_offsets.c`, `aws_hal_arch_regs.c`), the SDMA capability constants (`aws_hal_arch_dma.c`), and the engine-enum/name helpers and arch-type gate (`al_hal_tpb_arch.c`) that the rest of the runtime calls to read CSR base addresses, compute register offsets, and write TPB sequencer registers **without naming a silicon generation at the call site**. It is the sibling of the engine-dispatch shims ([hal-tpb-shims](hal-tpb-shims.md)) — same `kaena_khal` dispatch object, but a different sub-table (`khal_arch` rather than `khal_sp`/`khal_stpb`) and a different job: where the shims start/stop the five compute *engines*, the accessors here resolve *where the registers live* and program a small set of them directly. The concrete per-arch base/offset *values* — what `get_tpb_base(PE)` actually returns on SUNDA versus MARIANA — are produced by the `*_sunda`/`*_cayman`/`*_mariana` leaves and are owned by [arch-csr-offsets](arch-csr-offsets.md); **this page owns the accessor mechanism, not the numbers it returns.**

The surface is built on one gate and three accessor shapes. The gate is `al_hal_tpb_get_arch_type` (`@0x44bca0`): a pure read of the `.bss` global `hal_target` (`@0xCAEB60`), range-asserted, returning the latched generation as `enum al_hal_tpb_arch_type {INVALID=0, INVALID_1=1, SUNDA=2, CAYMAN=3, MARIANA=4, NUM=5}` ([hal-adapter §3](hal-adapter.md)). Every accessor consults it first. The three shapes are: **(A) vtable trampolines** — assert the gate, assert a `kaena_khal.khal_arch.<member>` function pointer is non-NULL, tail-call it (the 18 base/offset resolvers of `aws_hal_arch_offsets.c` and the NX/XT register accessors of `aws_hal_arch_regs.c`); **(B) inlined direct-MMIO writers** — gate-check the arch, optionally log on SUNDA, then `al_reg_write32(base + literal_offset, value)` for the TPB sequencer stochastic-rounding registers, whose offsets are pinned inline (`POOL @+0x10C`, `ACT @+0x20C`, MARIANA PSUM `@+0xF10/+0xF14`); and **(C) arch-switch constant returners** — the three SDMA capability getters that return a generation-dependent literal (`max_fma_sources`, `max_cce_elements`, `max_hw_dge_count`). Two further helpers are pure and arch-free: the engine-enum/name resolvers over `NEURON_ENG_NAMES`/`NEURON_ENG_PUB_NAMES`.

A reimplementer who builds this surface reproduces, above all, the **arch-gate-then-dispatch indirection**: an accessor never branches on the arch value it reads — it uses the gate as a liveness check, then either tail-calls a fixed `khal_arch` slot whose contents were chosen once at bring-up (shape A), or it was itself compiled for exactly one generation and asserts that generation holds (shape B/C). The arch-specificity lives in *which pointer the registrar wrote* (A) or in *which literal the compiler folded* (B/C), never in the accessor's control flow. The page applies the recurring H3 vocabulary to four units: the arch-type gate; the offset/base resolvers (shape A); the inlined register writers and the engine-enum/name helpers (shape B + the pure helpers); and the SDMA capability constants (shape C).

For reimplementation, the contract is:

- **The arch-type gate** — `al_hal_tpb_get_arch_type` reads one `.bss` u32 (`hal_target @0xCAEB60`), asserts `!= INVALID` and `< NUM`, returns it. It is the single read every accessor on this page funnels through; reproduce the *range-asserted single read*, not a scattered `if (arch == …)` per accessor.
- **The vtable trampoline (shape A)** — `assert(gate() != INVALID); fp = kaena_khal.khal_arch.<member>; assert(fp != NULL); return fp(args)`. The per-fn assert string names the slot member verbatim; the slot offset is `slot_vma − 0xCAEB80`. NULL-slot policy here is uniformly **fatal assert** (unlike the engine shims' split policy — [hal-tpb-shims §3](hal-tpb-shims.md)).
- **The inlined writers (shape B)** — a handful of TPB sequencer registers are *not* dispatched: the accessor checks the arch in `{SUNDA,CAYMAN,MARIANA}` (or asserts MARIANA-only for the PSUM pair), optionally logs on SUNDA, and writes `al_reg_write32(base + literal)` with the literal folded inline. The offsets (`+0x10C/+0x110`, `+0x20C/+0x210`, `+0xF10/+0xF14`) are register positions a port must reproduce; they do **not** route through `khal_arch`.
- **The engine model** — `NC_ENGINE_TYPE {PE=0, ACT=1, POOL=2, DVE=3, SP=4}`, count `=5`, with two parallel name tables (`NEURON_ENG_NAMES` lower-case parse names, `NEURON_ENG_PUB_NAMES` display names) and a `strcmp`-ladder string→enum resolver. This is the `NEURON_ENG_NAMES` table the engine-dispatch page defers here ([hal-tpb-shims CORRECTION](hal-tpb-shims.md)).

| | |
|---|---|
| **Dispatch object** | `kaena_khal` (`@.bss 0xCAEB80`, nm `b kaena_khal`) — the flat per-arch op table; accessors here key the `khal_arch` sub-block |
| **Arch gate** | `al_hal_tpb_get_arch_type @0x44bca0` reads `hal_target @.bss 0xCAEB60` (u32); set once by `al_hal_tpb_set_arch_type @0x44bc00` ([hal-adapter §3](hal-adapter.md)) |
| **Registrars (fill slots)** | `kaena_khal_register_funcs_v2 @0x468740` (SUNDA=2) · `_v3 @0x46ed70` (CAYMAN=3) · `_v4 @0x4622e0` (MARIANA=4), via `kaena_khal_init @0x462290` |
| **Arch enum** | `enum al_hal_tpb_arch_type {INVALID=0, INVALID_1=1, SUNDA=2, CAYMAN=3, MARIANA=4, NUM=5}` (DWARF; consistent with [hal-adapter](hal-adapter.md)) |
| **Engine enum** | `enum NC_ENGINE_TYPE {PE=0, ACT=1, POOL=2, DVE=3, SP=4}`, count `=5` |
| **Offset resolvers (shape A)** | `aws_hal_arch_offsets.c` — 18 trampolines, `.text 0x44bf00..0x44c7b2`, slots `khal_arch +0x40..+0xE8` |
| **Register accessors (shape A/B)** | `aws_hal_arch_regs.c` — NX/XT local-reg writers + inlined sequencer writers, `.text 0x44c7c0..0x44da5f` |
| **SDMA constants (shape C)** | `aws_hal_arch_dma.c` — `max_fma_sources`/`max_cce_elements`/`max_hw_dge_count`, `.text 0x44be10..0x44befc` |
| **Engine helpers (pure)** | `al_hal_tpb_arch.c` — `get_tpb_eng_pub_name @0x44bd30`, `get_tpb_eng_count @0x44bd50`, `get_tpb_eng_type_from_str @0x44bd60` |
| **NULL-slot policy (all shape-A here)** | fatal `__assert_fail("kaena_khal.khal_arch.<member>")` — uniform, no silent `return 0` |
| **Shared TU path** | `.../KaenaHal-2.31.0.0/.../src/common/arch/aws_hal_arch_offsets.c` (`.rodata @0x820AB8`); arch-gate assert `.rodata @0x81C890` |

> **CORRECTION (HAL-REG-01) —** the map page ([hal-adapter §4](hal-adapter.md)) attributes "the concrete `aws_reg_*` generated accessors and per-arch register/offset return values" to this page in one breath. Split that: the **accessor functions** (`aws_hal_get_*`, `aws_reg_*` — the trampolines and inlined writers) are owned here; the **per-arch return values** (the actual base addresses and CSR offsets the `*_sunda`/`*_cayman`/`*_mariana` leaves yield) are owned by [arch-csr-offsets](arch-csr-offsets.md). This page documents the *mechanism that resolves a register location*; arch-csr-offsets documents *what location it resolves to per generation*. Where this page pins an offset inline (the stochastic-rounding writers of §3), that offset is generation-invariant and `objdump`-confirmed in *this* binary — it is part of the accessor body, not a per-arch leaf return.

---

## 1. The Arch-Type Gate

### Purpose

`al_hal_tpb_get_arch_type` (`@0x44bca0`) is the root every accessor on this page funnels through. It answers one question — "which silicon generation is this process bound to?" — with a single range-asserted read of the `.bss` global `hal_target` (`@0xCAEB60`). It is the same primitive [hal-adapter §3](hal-adapter.md) documents as the most-called HAL function in the binary (200+ callers); this page treats it as the gate that precedes shape-A and shape-B accessors. Crucially, an accessor that *calls* the gate almost never uses the returned value as a selector — shape-A trampolines discard it after the `!= INVALID` test and index a fixed `khal_arch` slot; shape-B/C accessors compare it against a compile-time constant set (`{2,3,4}` or `== 4`) purely to assert the body was reached on a generation it was built for. The arch-specific *behavior* is bound elsewhere (the registrar's slot fill, or the compiler's constant fold), not here.

### Entry Point

```text
<any aws_hal_get_* / aws_reg_* accessor on this page>
  └─ al_hal_tpb_get_arch_type (0x44bca0)        ── read hal_target@0xCAEB60, assert in (INVALID, NUM)
        ▲
        │ hal_target is latched once at bring-up:
  al_hal_tpb_set_arch_type (0x44bc00)            ── tdrv_identify_* / nrt_fake_dev_info
        └─ kaena_khal_init (0x462290)            ── fills kaena_khal.khal_arch slots per arch
             ├─ arch 2 → kaena_khal_register_funcs_v2 (0x468740)   ── *_sunda  leaves
             ├─ arch 3 → kaena_khal_register_funcs_v3 (0x46ed70)   ── *_cayman leaves
             └─ arch 4 → kaena_khal_register_funcs_v4 (0x4622e0)   ── *_mariana leaves
```

### Algorithm

The gate is a pure read with two asserts; the set side (boundary-owned, shown for the latch ordering) is documented in full at [hal-adapter §3](hal-adapter.md):

```c
// al_hal_tpb_get_arch_type @0x44bca0 — al_hal_tpb_arch.c — the gate every accessor consults
function al_hal_tpb_get_arch_type() -> enum al_hal_tpb_arch_type:
    arch = hal_target                                       // .bss 0xCAEB60 (u32)
    assert(arch != AL_HAL_TPB_ARCH_TYPE_INVALID)            // "hal_target != AL_HAL_TPB_ARCH_TYPE_INVALID"
    assert(arch <  AL_HAL_TPB_ARCH_TYPE_NUM)                // "hal_target < AL_HAL_TPB_ARCH_TYPE_NUM"
    return arch                                             // {SUNDA=2, CAYMAN=3, MARIANA=4}
```

Two consumption patterns sit on top of the same read. Shape A uses only the liveness test:

```c
// shape-A use: the value is a liveness gate, not a selector
if al_hal_tpb_get_arch_type() == AL_HAL_TPB_ARCH_TYPE_INVALID:
    __assert_fail("al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID", ...)  // .rodata 0x81C890
// ... then index a FIXED kaena_khal.khal_arch slot (the registrar already chose the arch-correct fp)
```

Shape B/C compares against a constant set to assert the body's generation precondition:

```c
// shape-B/C use: the value asserts a compile-time generation precondition
arch = al_hal_tpb_get_arch_type()
if (arch - SUNDA) > (MARIANA - SUNDA):   // i.e. arch not in {2,3,4}  → assert (e.g. aws_hal_arch_dma.c)
    __assert_fail(...)
// MARIANA-only writers instead assert  arch == AL_HAL_TPB_ARCH_TYPE_MARIANA(4)
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `al_hal_tpb_get_arch_type` | `0x44bca0` | the gate — range-asserted read of `hal_target` (boundary; [hal-adapter §3](hal-adapter.md)) | CERTAIN |
| `al_hal_tpb_set_arch_type` | `0x44bc00` | one-shot latch write + `kaena_khal_init` (boundary) | CERTAIN |
| `kaena_khal_init` | `0x462290` | dispatch on latched arch → registrar (boundary) | HIGH |
| `kaena_khal_register_funcs_v2/_v3/_v4` | `0x468740`/`0x46ed70`/`0x4622e0` | fill `khal_arch` slots with SUNDA/CAYMAN/MARIANA leaves (boundary) | CERTAIN |

### Considerations

> **QUIRK —** the gate reads `hal_target` (`@0xCAEB60`), a *different* global from the `kaena_khal` dispatch object (`@0xCAEB80`) the slots live in. The two are filled by the same `set_arch_type → kaena_khal_init` event, but a reimplementer must keep them distinct: `hal_target` is the latched enum the gate asserts against; `kaena_khal.khal_arch` is the table of resolved function pointers. The 0x20-byte gap between them (`0xCAEB60..0xCAEB80`) holds unrelated `.bss` scalars (e.g. `addr_check @0xCAEB68`), not part of either structure.

---

## 2. Base and Offset Resolvers — `aws_hal_arch_offsets.c` (Shape A)

### Purpose

The 18 functions of `aws_hal_arch_offsets.c` (`.text 0x44bf00..0x44c7b2`) resolve *where* a region or register lives for the current generation: TPB engine bases, PSUM bases, preprocessing bases, HBM bases, and a set of SDMA/FP8/sequencer config offsets. Every one is the identical vtable trampoline — gate, NULL-assert, tail-call into `kaena_khal.khal_arch` — so the page documents the *shape* once and tabulates the 18 slots rather than repeating the body. The concrete return values (a SUNDA `get_tpb_base(PE)` versus a MARIANA one) are produced by the registered `*_sunda`/`*_cayman`/`*_mariana` leaves and live in [arch-csr-offsets](arch-csr-offsets.md); here, each resolver is pinned to its `khal_arch` slot offset (`slot_vma − 0xCAEB80`) and the assert string that names it.

### Entry Point

```text
<consumer: tdrv_arch_csr_register_device_* / insert_set_fp8_conv_config / ucode_*_core_create / ...>
  └─ aws_hal_get_<region>_base / get_<x>_offset / get_<x>_params   ── shape-A trampoline (this §)
       └─ al_hal_tpb_get_arch_type (0x44bca0)        ── assert arch != INVALID
       └─ assert kaena_khal.khal_arch.<member> != NULL
       └─ jmp *kaena_khal.khal_arch.<member>          ── tail-call the installed *_sunda/_cayman/_mariana leaf
```

### Algorithm

The canonical offsets-resolver trampoline, byte-identical across all 18 (verbatim per the shared TU-path string `.rodata @0x820AB8` and arch-gate assert `.rodata @0x81C890`). The only per-fn variables are the slot member and the arg list:

```c
// CANONICAL khal_arch OFFSET/BASE RESOLVER — modelled on aws_hal_get_tpb_base @0x44c320
// slot = kaena_khal.khal_arch.get_tpb_base (0xCAEBC0, i.e. khal_arch +0x40)
function aws_hal_get_tpb_base(eng):                         // 0x44c320, aws_hal_arch_offsets.c
    if al_hal_tpb_get_arch_type() == AL_HAL_TPB_ARCH_TYPE_INVALID:   // 0x44bca0, gate §1
        __assert_fail("al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_INVALID",
                      ".../aws_hal_arch_offsets.c", 0x51, __PRETTY_FUNCTION__)
    fp = kaena_khal.khal_arch.get_tpb_base                  // 0xCAEBC0  (load fn-ptr from .bss slot)
    if fp == NULL:
        __assert_fail("kaena_khal.khal_arch.get_tpb_base",
                      ".../aws_hal_arch_offsets.c", 0x52, __PRETTY_FUNCTION__)   // FATAL, uniform policy
    return fp(eng)                                          // jmp *fp — tail-call per-arch leaf
```

### Function Map — the 18 offset/base resolvers → `khal_arch` slot

Each resolver, its `khal_arch` slot (absolute VMA and offset from `kaena_khal @0xCAEB80`), and its role. The per-arch return value belongs to [arch-csr-offsets](arch-csr-offsets.md). Slot offsets are the exact `.bss` address each trampoline loads-and-tests; the assert string names the member.

| Resolver (symbol) | Addr | Slot VMA | Slot off | Role | Conf |
|---|---|---|---|---|---|
| `aws_hal_get_tpb_base` | `0x44c320` | `0xCAEBC0` | `+0x40` | TPB engine region base (arg `eng`) | CERTAIN |
| `aws_hal_get_tpb_psum_base` | `0x44c380` | `0xCAEBC8` | `+0x48` | PSUM region base (arg `eng`) | CERTAIN |
| `aws_hal_get_preproc_base` | `0x44c4c0` | `0xCAEBD0` | `+0x50` | preprocessing region base (arg `eng`) | CERTAIN |
| `aws_hal_get_act_table_params` | `0x44c060` | `0xCAEBD8` | `+0x58` | ACT table params (4 args) | CERTAIN |
| `aws_hal_get_dve_parameter_ram_params` | `0x44c0f0` | `0xCAEBE0` | `+0x60` | DVE parameter-RAM params (2 args) | CERTAIN |
| `aws_hal_get_dve_table_offsets` | `0x44c170` | `0xCAEBE8` | `+0x68` | DVE table offsets (4 args) | CERTAIN |
| `aws_hal_get_dve_table_sizes` | `0x44c200` | `0xCAEBF0` | `+0x70` | DVE table sizes (4 args) — no static caller | HIGH |
| `aws_hal_get_sdma_cce_user_offset` | `0x44c520` | `0xCAEBF8` | `+0x78` | SDMA CCE user-region offset | CERTAIN |
| `aws_hal_get_sdma_data_conv_non_ocp_cfg_offset` | `0x44c600` | `0xCAEC00` | `+0x80` | SDMA non-OCP data-conv cfg offset | CERTAIN |
| `aws_hal_get_sdma_data_conv_fp8_ocp_cfg_offset` | `0x44c670` | `0xCAEC08` | `+0x88` | SDMA FP8-OCP data-conv cfg offset | CERTAIN |
| `aws_hal_get_dve_sequencer_emax_cfg_offset` | `0x44c6e0` | `0xCAEC10` | `+0x90` | DVE sequencer e-max cfg offset | CERTAIN |
| `aws_hal_get_eng_fp8_cfg_offset` | `0x44c590` | `0xCAEC18` | `+0x98` | per-engine FP8 cfg offset (arg `eng`) | CERTAIN |
| `aws_hal_get_hbm_base` | `0x44c750` | `0xCAEC20` | `+0xA0` | HBM region base (arg `hbm_idx`) | CERTAIN |
| `aws_hal_get_seq_params` | `0x44bf00` | `0xCAEC30` | `+0xB0` | NX-core sequencer params (7 args) | CERTAIN |
| `aws_hal_get_q7_params` | `0x44bfb0` | `0xCAEC38` | `+0xB8` | Q7 pooling-core params (7 args) | CERTAIN |
| `aws_hal_get_eng_hw_decode_table_params` | `0x44c290` | `0xCAEC48` | `+0xC8` | engine HW-decode-table params (5 args) — no static caller | HIGH |
| `aws_hal_get_tpb_evt_sem_inc_base` | `0x44c3e0` | `0xCAEC60` | `+0xE0` | TPB event-semaphore increment base (arg `eng`) | CERTAIN |
| `aws_hal_get_top_sp_evt_sem_inc_base` | `0x44c450` | `0xCAEC68` | `+0xE8` | TopSP event-semaphore increment base (arg `eng`) | CERTAIN |

> **NOTE — the `khal_arch` sub-block is sparse here.** The 18 slots above span `+0x40..+0xE8` but are *not* contiguous: gaps at `+0x68`(`0xCAEC28`, between `get_hbm_base` and `get_seq_params`), `+0x80`(`0xCAEC40`), `+0x90`/`+0x98`(`0xCAEC50`/`0xCAEC58`) hold members owned by other accessor cells (the NX/XT register slots of §3, the STPB band of [hal-tpb-shims](hal-tpb-shims.md), and so on). Slot offsets here are byte-firm from the trampoline disassembly; the gap members are out of this surface's scope and not enumerated.

### Considerations

> **GOTCHA —** `aws_hal_get_dve_table_sizes` (`@0x44c200`, slot `+0x70`) and `aws_hal_get_eng_hw_decode_table_params` (`@0x44c290`, slot `+0xC8`) have **zero recovered static callers** in this build, yet they are live, registered API: the registrars install real `*_sunda`/`*_cayman`/`*_mariana` leaves into their slots. They are reached only through cross-module or indirect paths (likely the encoder's table-setup path), so a reimplementer must still supply the leaves even though no in-binary call edge proves the consumer. They are *not* dead trampolines — the slot fill is the evidence of liveness, not the call graph. (HIGH that they are registered; the consumer is not traced.)

---

## 3. Register Accessors and Engine Helpers — `aws_hal_arch_regs.c` + `al_hal_tpb_arch.c`

### Purpose

Two further clusters round out the surface. The first is the **register-accessor cluster** of `aws_hal_arch_regs.c` (`.text 0x44c7c0..0x44da5f`): the NX (Xtensa sequencer) and XT (memcopy-DMA) local-register writers, the engine→XT index map, and a band of inlined sequencer-config writers. Most are shape-A trampolines (NX `start_addr`/`start_ctrl`/`dma_ctrl`/`run_state`, XT `memcopy_dmas`/`memcopy_queues`), but a meaningful minority are **shape-B inlined direct-MMIO writers** that pin exact CSR offsets in *this* binary — the TPB sequencer stochastic-rounding registers. The second cluster is the **engine-enum/name helpers** of `al_hal_tpb_arch.c`: the pure, table-driven resolvers over `NEURON_ENG_NAMES`/`NEURON_ENG_PUB_NAMES` that translate between engine index, parse name, and display name. This is the `NEURON_ENG_NAMES` table [hal-tpb-shims CORRECTION](hal-tpb-shims.md) defers to this page.

### Algorithm

The shape-B inlined writers are the part a reimplementer must build *directly* — they do not route through `khal_arch`, and their offsets are generation-invariant (`objdump`-confirmed). The POOL/ACT sequencer stochastic-rounding writers gate on `arch ∈ {2,3,4}`, log on SUNDA, then write a single CSR; the MARIANA-only PSUM writer asserts `arch == 4` and writes a ctrl/data pair:

```c
// aws_reg_write_tpb_pool_sequencer_stochastic_rnd @0x44d6e0 — aws_hal_arch_regs.c — INLINED (shape B)
function aws_reg_write_tpb_pool_sequencer_stochastic_rnd(base, val):
    arch = al_hal_tpb_get_arch_type()                       // gate §1
    if arch == AL_HAL_TPB_ARCH_TYPE_SUNDA:                  // ==2 → diagnostic log only
        al_hal_log(LEVEL_5, "Writing to sunda_tpb_pool_sequencer_stochastic_rnd :: %#x\n", val)
    else assert((arch - SUNDA) <= (MARIANA - SUNDA))        // else require arch in {2,3,4}
    al_reg_write32(base + 0x10C, val)                       // POOL seq stochastic_rnd CSR @+268; objdump-firm

// aws_reg_write_tpb_act_sequencer_stochastic_rnd @0x44d7d0 — same shape, CSR @ base+0x20C (+524)
// aws_reg_write_tpb_pool_sequencer_stochastic_rnd_mode @0x44d8a0 — CSR @ base+0x110 (+272)
// aws_reg_write_tpb_act_sequencer_stochastic_rnd_mode  @0x44d990 — CSR @ base+0x210 (+528)

// aws_reg_write_tpb_tpb_global_configuration_psum_stochastic_rounding @0x44d840 — MARIANA-ONLY (shape B)
function aws_reg_write_..._psum_stochastic_rounding(base, val):
    if al_hal_tpb_get_arch_type() != AL_HAL_TPB_ARCH_TYPE_MARIANA:    // !=4 → assert
        __assert_fail(...)
    al_reg_write32(base + 0xF14, 2)                         // ctrl @+3860 = 2  (arm)
    al_reg_write32(base + 0xF10, val)                       // data @+3856 = val (order: ctrl then data)
```

Two shape-B field-updaters operate on an in-DRAM shadow word (not MMIO) — they pack the POOL sequencer stochastic seed/dtype fields, gated only on `arch ∈ {2,3,4}`:

```c
// aws_reg_field_update_tpb_pool_sequencer_stochastic_rnd_seed @0x44d630 — 21-bit seed field
function ..._seed(word_ptr, seed):
    *word_ptr = (*word_ptr & 0xFFE00000) | (seed & 0x1FFFFF)    // bits [20:0]
// aws_reg_field_update_..._stochastic_rnd_dtype @0x44d690 — 4-bit dtype field
function ..._dtype(word_ptr, dtype):
    *word_ptr = (*word_ptr & 0x0FFFFFFF) | (dtype << 28)        // bits [31:28]; bits [27:21] preserved
```

The engine helpers are pure and arch-free — the engine model the rest of the surface (and the dispatch shims) shares:

```c
// al_hal_tpb_get_tpb_eng_count @0x44bd50 — leaf
function al_hal_tpb_get_tpb_eng_count() -> 5               // NC_ENGINE_TYPE has 5 members

// al_hal_tpb_get_tpb_eng_pub_name @0x44bd30 — display-name lookup
function al_hal_tpb_get_tpb_eng_pub_name(idx) -> char*:
    if idx > 4: return NULL                                // bounds check
    return NEURON_ENG_PUB_NAMES[idx]                       // .data.rel.ro @0xBF38A0

// al_hal_tpb_get_tpb_eng_type_from_str @0x44bd60 — parse-name → enum (strcmp ladder)
function al_hal_tpb_get_tpb_eng_type_from_str(s) -> enum NC_ENGINE_TYPE:
    if s=="pe":0  "act":1  "pool":2  "dve":3  "sp":4        // order = NEURON_ENG_NAMES
    else: return 5                                          // invalid sentinel
```

### Engine Name Tables

Two parallel `char*[5]` arrays, byte-decoded from `.data`/`.data.rel.ro` (VMA == file offset). The index *is* the `NC_ENGINE_TYPE` ordinal; the two tables differ only in the string set (lower-case parse names vs. capitalized display names):

| Idx (enum) | `NEURON_ENG_NAMES` (parse) `@0xC09600` | `NEURON_ENG_PUB_NAMES` (display) `@0xBF38A0` |
|---|---|---|
| 0 `PE` | `"pe"` | `"Tensor"` |
| 1 `ACT` | `"act"` | `"Scalar"` |
| 2 `POOL` | `"pool"` | `"GPSIMD"` |
| 3 `DVE` | `"dve"` | `"Vector"` |
| 4 `SP` | `"sp"` | `"Sync"` |

### Function Map — register writers and engine helpers

The inlined sequencer writers (shape B) carry their CSR offset; the field-updaters touch a shadow word; the engine helpers are pure. The shape-A trampolines in this TU (NX/XT local-reg accessors) are tabulated for completeness with their `khal_arch` slot.

| Function | Addr | Shape | Offset / slot · role | Conf |
|---|---|---|---|---|
| `aws_reg_write_tpb_pool_sequencer_stochastic_rnd` | `0x44d6e0` | B (MMIO) | `base+0x10C` · POOL seq stochastic-rnd | CERTAIN |
| `aws_reg_write_tpb_pool_sequencer_stochastic_rnd_mode` | `0x44d8a0` | B (MMIO) | `base+0x110` · POOL seq stochastic-rnd mode | CERTAIN |
| `aws_reg_write_tpb_act_sequencer_stochastic_rnd` | `0x44d7d0` | B (MMIO) | `base+0x20C` · ACT seq stochastic-rnd | CERTAIN |
| `aws_reg_write_tpb_act_sequencer_stochastic_rnd_mode` | `0x44d990` | B (MMIO) | `base+0x210` · ACT seq stochastic-rnd mode | CERTAIN |
| `aws_reg_write_..._psum_stochastic_rounding` | `0x44d840` | B (MMIO) | `base+0xF14`=2, `base+0xF10`=val · **MARIANA-only** PSUM ctrl+data | CERTAIN |
| `aws_reg_write_..._psum_stochastic_rounding_ctrl` | `0x44da00` | B (MMIO) | `base+0xF14` · **MARIANA-only** PSUM ctrl | CERTAIN |
| `aws_reg_field_update_..._stochastic_rnd_seed` | `0x44d630` | B (shadow) | bits `[20:0]` seed (mask `0x1FFFFF`) | CERTAIN |
| `aws_reg_field_update_..._stochastic_rnd_dtype` | `0x44d690` | B (shadow) | bits `[31:28]` dtype (`<<28`) | CERTAIN |
| `aws_reg_write_tpb_dve_sequencer_stochastic_rnd` | `0x44d750` | A | slot `khal_stpb +0x230` · DVE seq stochastic-rnd (dispatched) | CERTAIN |
| `aws_reg_write_tpb_dve_sequencer_stochastic_rnd_mode` | `0x44d910` | A | slot `khal_stpb +0x238` · DVE seq stochastic-rnd mode (dispatched) | CERTAIN |
| `aws_reg_write_tpb_nx_local_reg_..._tpb_base_address_lo` | `0x44cc20` | B (MMIO) | `base+0x20` · TPB base-addr-lo (SUNDA inline) | CERTAIN |
| `aws_reg_write_tpb_nx_local_reg_start_ctrl` | `0x44cd70` | A | slot `khal_arch +0x170` · NX program start-ctrl | CERTAIN |
| `aws_reg_get_tpb_nx_local_reg_run_state_state` | `0x44cee0` | A | slot `khal_arch +0x188` · NX run-state read | CERTAIN |
| `aws_hal_arch_eng_to_xt` | `0x44c910` | concrete | eng `0..4` → XT idx `0..4`, default `6` (+log) | CERTAIN |
| `aws_hal_arch_get_xt_local_reg_offset` | `0x44c970` | A | slot `khal_arch +0xF0` · per-XT register base resolver | CERTAIN |
| `aws_hal_get_hbm_size` | `0x44c7c0` | A | slot `khal_arch +0xA8` · per-VNC HBM byte size | CERTAIN |
| `aws_hal_get_tpb_eng_iram_size` | `0x44c830` | A | slot `khal_arch +0xC0` · TPB engine IRAM size | CERTAIN |
| `al_hal_tpb_get_tpb_eng_count` | `0x44bd50` | pure | constant `5` | CERTAIN |
| `al_hal_tpb_get_tpb_eng_pub_name` | `0x44bd30` | pure | `NEURON_ENG_PUB_NAMES[idx]`, bounds `idx>4→NULL` | CERTAIN |
| `al_hal_tpb_get_tpb_eng_type_from_str` | `0x44bd60` | pure | parse-name → enum (strcmp ladder), else `5` | CERTAIN |

### Considerations

> **QUIRK —** the DVE sequencer stochastic-rounding writers (`@0x44d750`, `@0x44d910`) are **dispatched** (shape A, through `khal_stpb +0x230/+0x238`), while their POOL and ACT siblings (`@0x44d6e0`, `@0x44d7d0`, …) are **inlined** (shape B, direct `al_reg_write32`). Same logical operation — write a sequencer's stochastic-rounding register — split across two implementation strategies in the *same source file*. The reason is that DVE's register position varies enough across generations to warrant per-arch leaves, whereas POOL/ACT sit at fixed offsets in every generation this binary supports (so the compiler folds the offset inline). A reimplementer must not normalize the two into one strategy: the DVE path needs a registered leaf, the POOL/ACT path needs the literal offset.

> **GOTCHA —** the PSUM stochastic-rounding writer (`@0x44d840`) writes **ctrl before data** — `base+0xF14 = 2` (arm), then `base+0xF10 = val` — and is **MARIANA-only** (asserts `arch == 4`). The ordering is `objdump`-firm and central to the hardware handshake: arming the control register first, then latching the value, is the sequence the PSUM stochastic-rounding unit expects. A port that reverses the two writes, or exposes the path on a non-MARIANA generation, diverges from the silicon contract. (The `_ctrl`-only variant `@0x44da00` writes just `base+0xF14`, also MARIANA-gated.)

> **NOTE — the inlined offsets are this binary's, the dispatched returns are not.** Where §3 pins a numeric CSR offset (`+0x10C`, `+0x20C`, `+0xF10/+0xF14`), that number is part of the accessor *body* in this build and reproduced verbatim by a reimplementer. Where §2/§3 mark a function shape-A (dispatched), the *value* it returns is generation-specific and produced by a leaf in [arch-csr-offsets](arch-csr-offsets.md) — this page guarantees only the resolution mechanism and the slot it routes through.

---

## 4. SDMA Capability Constants — `aws_hal_arch_dma.c` (Shape C)

### Purpose

Three functions in `aws_hal_arch_dma.c` (`.text 0x44be10..0x44befc`) report per-generation SDMA capability *limits* — not register locations, but scalar maxima the descriptor builders need to clamp their work. They are shape C: no `khal_arch` slot, no MMIO; each gate-checks `arch ∈ {SUNDA,CAYMAN,MARIANA}` then returns a literal selected by an `arch`-keyed switch. Because the values are folded into the function body, they are first-class evidence in *this* binary and are tabulated here in full (the values are generation capabilities, not per-arch leaf returns, so they belong on this page rather than [arch-csr-offsets](arch-csr-offsets.md)).

### Algorithm

```c
// aws_hal_get_sdma_max_fma_sources @0x44be10 — aws_hal_arch_dma.c (shape C)
function aws_hal_get_sdma_max_fma_sources() -> uint:
    arch = al_hal_tpb_get_arch_type()                       // gate §1
    assert((arch - SUNDA) <= (MARIANA - SUNDA))             // arch in {2,3,4} else assert (line 0x1C)
    switch arch:
        case SUNDA, CAYMAN:  return 16
        case MARIANA:        return 32
// _max_cce_elements @0x44be60:  SUNDA→1024 ; CAYMAN,MARIANA→2048   (assert line 0x2C)
// _max_hw_dge_count  @0x44beb0:  SUNDA→0    ; CAYMAN,MARIANA→2      (assert line 0x3E)
```

### Capability Table

The three SDMA capability constants by generation (decoded from the switch bodies; HIGH):

| Capability getter | Addr | SUNDA (2) | CAYMAN (3) | MARIANA (4) | Consumer |
|---|---|---|---|---|---|
| `aws_hal_get_sdma_max_fma_sources` | `0x44be10` | 16 | 16 | 32 | `parse_one_dma_block` |
| `aws_hal_get_sdma_max_cce_elements` | `0x44be60` | 1024 | 2048 | 2048 | `dma_util_expand_desc_complete_cce`, `encd_init_context` |
| `aws_hal_get_sdma_max_hw_dge_count` | `0x44beb0` | 0 | 2 | 2 | `gen_kbin` |

### Considerations

> **NOTE —** the SDMA capability getters are the one accessor family on this page whose per-generation *values* are documented here rather than deferred. They are not register offsets — they are descriptor-engine limits (FMA source fan-in, CCE element count, hardware DGE channel count) compiled directly into the function body, so a reimplementer reads them straight from this table. SUNDA's `max_hw_dge_count == 0` is the notable one: it is *why* the SUNDA-only DGE notification path elsewhere ([hal-tpb-shims §4](hal-tpb-shims.md)) and the hardware-DGE descriptor build are gated — SUNDA has no hardware DGE engines, so the count-zero is a real capability gap, not a placeholder.

## Related Components

| Name | Relationship |
|---|---|
| `al_hal_tpb_get_arch_type` (`0x44bca0`) / `al_hal_tpb_set_arch_type` (`0x44bc00`) | the arch gate every accessor consults and the one-shot latch that sets `hal_target` ([hal-adapter §3](hal-adapter.md)) |
| `kaena_khal_register_funcs_v{2,3,4}` (`0x468740`/`0x46ed70`/`0x4622e0`) | the registrars that fill `kaena_khal.khal_arch` with the `*_sunda`/`*_cayman`/`*_mariana` leaves every shape-A resolver tail-calls |
| `al_reg_write32` (`0x265c50`) | the FATAL-on-fail MMIO write primitive the shape-B inlined writers bottom into ([hal-adapter §2](hal-adapter.md)) |
| `aws_hal_stpb_program_start` / `stochastic_rounding_config` | the real STPB bodies that call this surface's resolvers and sequencer writers ([hal-tpb-shims §4](hal-tpb-shims.md)) |
| `insert_set_fp8_conv_config` | the FP8 config inserter that converges on five of this page's offset resolvers (`eng_fp8_cfg`, `sdma_cce_user`, two `data_conv`, `dve_sequencer_emax`) |

## Cross-References

- [Per-Arch Device Layer: CSR and Register-Offset Accessors](arch-csr-offsets.md) — the `*_sunda`/`*_cayman`/`*_mariana` leaves installed into the `khal_arch` slots; owns the **concrete per-arch base/offset values** every shape-A resolver here returns (this page owns the mechanism, that page owns the numbers)
- [KaenaHal: Overview and Platform-Services Adapter](hal-adapter.md) — `al_hal_tpb_get/set_arch_type`, the arch latch (`hal_target @0xCAEB60`) the gate reads, and `al_reg_write32`, the MMIO write the inlined writers use
- [KaenaHal: TPB/STPB Arch-Dispatch Shims](hal-tpb-shims.md) — the sibling `aws_hal_sp_*`/`aws_hal_stpb_*` engine-dispatch trampolines through the *same* `kaena_khal` object (different sub-tables); the CORRECTION there routes `NEURON_ENG_NAMES` to this page and the `aws_get_*` reg-offset shims to tdrv-arch-ops
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](tdrv-arch-ops.md) — owns the `aws_get_*` register-offset shims (`tpb_reg_offset` sub-vtable), a *different* dispatch object from the `kaena_khal.khal_arch` accessors documented here
- [Per-Arch Device Layer: Geometry and Address Map](arch-geometry.md) — the per-generation region geometry (HBM/PSUM/SBUF maps) the `get_*_base` resolvers index into
- [back to index](../index.md)
