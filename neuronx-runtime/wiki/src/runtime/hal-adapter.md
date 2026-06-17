# KaenaHal: Overview and Platform-Services Adapter

> *All addresses, offsets, and symbol names on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`, `.rodata`, **and `.data` are VMA == file offset** (read `.data`/`.rodata` globals at their VMA directly); `.bss` is `NOBITS`. The vendored HAL package is `KaenaHal-2.31.0.0` (Amazon `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`); the adapter and helper code root in `/opt/workspace/KaenaRuntime/tdrv/{hal_platform.c, helper.c}` and the barrier inlines in `/opt/amazon/develop/include/inf/common/plat/al_hal_plat_services.h`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every primitive is DWARF-attributed by `addr2line` to its exact `hal_platform.c`/`helper.c` line, sized by `nm -S`, and its body cross-checked against the decompile + `objdump` (thunks confirmed by tail-`jmp`, barriers byte-decoded). · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

**KaenaHal** is the userspace hardware-abstraction layer the Neuron runtime drives to touch silicon: the `al_udma_*` / `al_iofic_*` / `al_sdma_*` / `al_mla_*` DMA-engine, interrupt-controller, and stream-DMA accessors, plus the `aws_hal_*_{sunda,cayman,mariana}` per-generation register HALs. It is a **vendored package** — `KaenaHal-2.31.0.0`, derived from AnnapurnaLabs/Alpine ("AL") SoC HAL conventions — statically linked into `libnrt.so`. Unlike the rest of the runtime, KaenaHal carries **no DWARF compile-unit of its own** in this binary; its `al_*`/`aws_hal_*` bodies reach `.text` only through inlined headers ([overview §1](overview.md)). What *is* first-party, DWARF-rooted, and documented here is the thin layer that makes that vendored code compile and run unchanged: the **`al_hal_plat_services` porting adapter** in `tdrv/hal_platform.c`.

The Alpine HAL is written against a small set of "platform services" macros — read/write a 32-bit register, blit a descriptor, fence memory, log, allocate, busy-wait, abort — that every SoC port must supply. KaenaRuntime supplies them as a flat band of 24 `al_*`-named leaf functions (`@0x2658a0..0x265f8a`, all `t`/local symbols) that bottom out into the runtime's own CSR layer (`tdrv/csr.c`), log ring (`nlog/nlog.c`), and libc. This is the seam between the vendored HAL and the host runtime: above it, `al_udma_*` and `aws_hal_*` issue register transactions in Alpine's vocabulary; below it, those transactions become `csr_read`/`csr_write`/`axi_write` against a mapped BAR. A reimplementer who ports KaenaHal to a new host re-implements **exactly this band** and nothing else of the vendored tree changes.

This page is the **map** for the four KaenaHal sub-pages. It pins what KaenaHal-2.31.0.0 *is* and where its provenance strings live; it enumerates the `al_hal_plat_services` primitive band (register / memory / barrier / log / alloc) with each leaf pinned to symbol + address + the `csr.c`/`nlog` boundary it crosses; it shows a representative primitive (`al_reg_write32`, the FATAL-on-fail write) and the full-fence barrier as annotated C; and it documents how **`al_hal_tpb_get_arch_type`** (`@0x44bca0`) — the single most-called HAL primitive (200+ callers) — latches the silicon generation that every per-arch dispatch (the `tdrv_arch_ops` vtable of [tdrv-arch-ops](tdrv-arch-ops.md), the `aws_hal_*_{sunda,cayman,mariana}` register HALs) keys off. The per-arch register-offset shims, the concrete register accessors, and the UDMA/SDMA descriptor build are owned by the three sub-pages this map routes to.

For reimplementation, the contract of the adapter layer is:

- **The platform-services primitive set** — the ~14 `al_*` leaves an Alpine HAL port must supply (`al_reg_read32`/`write32`, `al_mem_read/write_buf`, `al_copy_descriptor`, `al_memset`/`al_reg_init`, the five `al_*_data_memory_barrier` fences, `al_hal_log`, `al_zalloc`/`al_free`, `al_udelay`, `al_abort_program`), each with its host-side callee and its failure policy.
- **The two failure policies** — register *read* failure logs `"PCIe read failed %p"` + backtrace + `__assert_fail` (recoverable-looking, but asserts); register *write* failure is **FATAL** — `al_abort_program(-1)` → `exit()`. A reimplementer must replicate the asymmetry: a bad write tears the process down, a bad read trips an assertion.
- **The barrier collapse** — all five `al_*_data_memory_barrier` variants emit byte-identical `lock orq $0,(%rsp)` on x86 TSO. The local/write/smp distinctions are no-ops on this ISA; a port to a weaker-ordered host (ARM) must re-derive them from the *semantics*, not copy the x86 collapse.
- **The arch latch** — `al_hal_tpb_get_arch_type` reads one `.bss` global (`hal_target @0xcaeb60`), asserts it is a valid `enum al_hal_tpb_arch_type`, and returns it; `al_hal_tpb_set_arch_type` writes it once at bring-up and runs `kaena_khal_init`. Every per-generation fan-out in the runtime resolves through this single read.

| | |
|---|---|
| **Vendored HAL** | `KaenaHal-2.31.0.0` (AnnapurnaLabs/Alpine; `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`) — statically linked, no own DWARF CU |
| **Adapter TU** | `/opt/workspace/KaenaRuntime/tdrv/hal_platform.c` — the `al_hal_plat_services` porting layer (first-party) |
| **Helper TU** | `/opt/workspace/KaenaRuntime/tdrv/helper.c` — `buf_*`, SDMA loc-bit encoders, kbin→SDMA translators |
| **Barrier source** | `/opt/amazon/develop/include/inf/common/plat/al_hal_plat_services.h:311-319` (inlined fences) |
| **Primitive band** | `al_reg_read32 @0x2658a0` … `al_free @0x265ce0` — 24 fns, `.text 0x2658a0..0x265f8a`, all `t`/local |
| **CSR boundary (read)** | `al_reg_read32 @0x2658a0` → `csr_read @0x315680` (`tdrv/csr.c`) |
| **CSR boundary (write)** | `al_reg_write32 @0x265c50` → `csr_write @0x315820`; `al_mem_write_buf @0x265990` → `axi_write @0x315850` |
| **Log boundary** | `al_hal_log @0x265b80` → `nlog_vwrite @0x7a3e0`; error paths → `nlog_write @0x224d40`, `nlog_backtrace @0x508220` |
| **Arch latch** | `al_hal_tpb_get_arch_type @0x44bca0` (200+ callers) reads `hal_target @.bss 0xcaeb60`; set by `al_hal_tpb_set_arch_type @0x44bc00` |
| **Arch enum** | `enum al_hal_tpb_arch_type {INVALID=0, INVALID_1=1, SUNDA=2, CAYMAN=3, MARIANA=4, NUM=5}` |

> **NOTE — provenance is AWS-internal, not third-party.** The verbatim `.rodata` path strings `/opt/brazil-pkg-cache/packages/KaenaHal/KaenaHal-2.31.0.0/AL2_x86_64/generic-flavor/src/...` and `/opt/amazon/develop/include/inf/common/plat/al_hal_plat_services.h` mark KaenaHal as a **vendored** package, but "vendored" here does not mean an external upstream. AnnapurnaLabs is an AWS subsidiary, and KaenaHal is Amazon's own Alpine-lineage SoC HAL, built in the same `brazil-pkg-cache` toolchain as KaenaRuntime. The `al_*` naming is the Alpine house convention the code inherits, not evidence of a foreign dependency. For a reimplementer the practical consequence is that the adapter contract (the platform-services macro set) is the *only* stable boundary; the vendored bodies above it are an Amazon-controlled tree that can — and does — version-skew against the runtime package (KaenaHal `2.31.0.0` vs runtime `2.31.24.0`).

---

## 1. What KaenaHal Is — and Where the Adapter Sits

### Purpose

KaenaHal is the register-access and DMA-engine HAL the Neuron runtime speaks to drive Trainium/Inferentia silicon. It is **layer [4]** of the six-layer runtime stack ([overview §1](overview.md)): TDRV (layer [3]) calls Alpine `al_*` primitives; those primitives become PCIe CSR transactions at the bottom. The vendored tree splits into three families a reimplementer must distinguish:

- **`al_udma_*` / `al_iofic_*` / `al_sdma_*` / `al_mla_*`** — the generic Alpine UDMA (universal DMA), IOFIC (I/O fabric interrupt controller), stream-DMA, and MLA accessors. Arch-neutral; they speak register offsets, not silicon names. Owned by [hal-udma-iofic](hal-udma-iofic.md).
- **`aws_hal_*_{sunda,cayman,mariana}`** — the per-generation register HALs (notification queues, XT-CC engine bring-up, SDMA M2M build) that specialize the generic accessors for one of three silicon generations. Owned by [hal-registers](hal-registers.md) and [hal-tpb-shims](hal-tpb-shims.md).
- **`al_hal_plat_services` (`al_reg_*`, `al_mem_*`, `al_*_barrier`, `al_hal_log`, `al_zalloc`/`al_free`, …)** — the bottom porting primitives the other two families are written against. **This is what `hal_platform.c` supplies, and what this page documents.**

The boundary is precise. The primitive band (`@0x2658a0..0x265f8a`) is the *callee* of the upper HAL and the *caller* of the host runtime; the caller-prefix histogram for in-edges into the band is `aws_hal` 1138, `al_udma` 668, `aws_reg` 115, `al_sdma` 110, `al_iofic` 80, `al_mla` 66 — the entire vendored HAL funnels its register/memory/log/alloc through these ~14 functions.

### Entry Point

The descent through the adapter band, with the host-side boundary each primitive crosses:

```text
 upper KaenaHal (al_udma_* / al_iofic_* / al_sdma_* / al_mla_* / aws_hal_*_{sunda,cayman,mariana} / aws_reg_*)
        │  call (BOUNDARY — vendored, no DWARF CU)
        ▼
 al_hal_plat_services adapter band  (tdrv/hal_platform.c, 0x2658a0..0x265f8a)
        ├─ al_reg_read32        (0x2658a0) ──► csr_read   (0x315680, tdrv/csr.c)
        ├─ al_reg_write32       (0x265c50) ──► csr_write  (0x315820)        [FATAL on fail]
        ├─ al_mem_write_buf     (0x265990) ──► axi_write  (0x315850)        [4B-aligned burst]
        ├─ al_mem_read_buf      (0x265a20) ──► (inlined dword loop, no axi_read)
        ├─ al_copy_descriptor   (0x265980) ──► memcpy                       [16B desc blit]
        ├─ al_memset/al_reg_init(0x265b10/0x265ac0) ──► al_mem_write_buf / dword stores
        ├─ al_*_data_memory_barrier (0x265930..0x265970) ── lock orq $0,(%rsp)  [x86 full fence]
        ├─ al_hal_log           (0x265b80) ──► nlog_vwrite (0x7a3e0, nlog/nlog.c)
        ├─ al_zalloc/al_free    (0x265cd0/0x265ce0) ──► calloc / free
        ├─ al_udelay            (0x265c30) ──► usleep                       [poll backoff]
        └─ al_abort_program     (0x265c40) ──► exit                         [NORETURN]
```

### Considerations

The adapter is the *only* place a reimplementer must re-author to move KaenaHal to a new host. Everything above it (the `al_udma`/`aws_hal` register logic) is host-independent; everything below it (`csr.c`, `nlog`, libc) is the host's own plumbing. The asymmetry worth noting up front: `al_mem_write_buf` device writes go through `axi_write` (an AXI burst), but `al_mem_read_buf` device reads are a plain inlined dword loop with no `axi_read` call — device-read is MMIO-direct, device-write is AXI-bursted. A port that mirrors the write path onto the read path will add a burst engine the original does not use on reads.

---

## 2. The `al_hal_plat_services` Primitive Band

### Purpose

These are the platform-services primitives the Alpine HAL is written against — the macro set every SoC port must supply. KaenaRuntime supplies them as the 24-function band at `0x2658a0..0x265f8a`; of those, ~14 are the genuine platform-services leaves (the rest are `helper.c` buffer/sim utilities documented inline). The table below is the primitive contract: each leaf, its category, its role, and the host boundary it bottoms into.

### Primitive Map

| Primitive (symbol) | Addr | Category | Role / host callee | Conf |
|---|---|---|---|---|
| `al_reg_read32` | `0x2658a0` | reg | read 32-bit CSR → `csr_read @0x315680`; on fail log `"PCIe read failed %p"` + `nlog_backtrace` + `__assert_fail` (`hal_platform.c:31`) | CERTAIN |
| `al_reg_read32_array` | `0x265920` | reg | bulk register read; tail-thunk → `csr_read_array(offsets,data,size)` | CERTAIN |
| `al_reg_write32` | `0x265c50` | reg | write 32-bit CSR → `csr_write @0x315820`; on fail log `"\n csr_write failed"` then `al_abort_program(-1)` — **NORETURN** | CERTAIN |
| `al_copy_descriptor` | `0x265980` | mem | blit a 16-byte UDMA descriptor into a ring slot; tail-thunk → `memcpy(dst,src,size)` | CERTAIN |
| `al_mem_write_buf` | `0x265990` | mem | 4B-aligned bulk device write → `axi_write @0x315850(dst,src,size/4)`; asserts src/dst 4B-aligned, `size%4==0`, `ret==0` | CERTAIN |
| `al_mem_read_buf` | `0x265a20` | mem | 4B-aligned bulk device read; **inlined dword loop** (no `axi_read`); asserts alignment + `size%4==0` | CERTAIN |
| `al_reg_init` | `0x265ac0` | mem | fill `size` bytes of device mem with a repeated 32-bit `value` via `al_mem_write_buf` loop | HIGH |
| `al_memset` | `0x265b10` | mem | 4B-granular device-memory memset to `pattern` (2-way unrolled dword stores); asserts `size%4==0` | CERTAIN |
| `al_data_memory_barrier` | `0x265930` | barrier | full memory fence — `lock orq $0,(%rsp)` | CERTAIN |
| `al_local_data_memory_barrier` | `0x265940` | barrier | full fence (`plat_services.h:311`); the `mb()` before DMA `drtp_inc` | CERTAIN |
| `al_local_write_data_memory_barrier` | `0x265950` | barrier | full fence (`plat_services.h:313`); write-barrier collapses on x86 | CERTAIN |
| `al_smp_data_memory_barrier` | `0x265960` | barrier | full fence (`plat_services.h:317`) | CERTAIN |
| `al_smp_read_data_memory_barrier` | `0x265970` | barrier | full fence (`plat_services.h:319`) | CERTAIN |
| `al_hal_log` | `0x265b80` | log | variadic HAL log shim; `va_start` → `nlog_vwrite(level, fn_name, fmt, va)` — bridges Alpine `al_*log` macros to KaenaRuntime `nlog` | CERTAIN |
| `al_udelay` | `0x265c30` | control | busy-poll backoff; tail-thunk → `usleep(usec)` | CERTAIN |
| `al_abort_program` | `0x265c40` | control | fatal HAL abort; tail-thunk → `exit(code)` — **NORETURN** | CERTAIN |
| `al_zalloc` | `0x265cd0` | alloc | zeroed HAL alloc; tail-thunk → `calloc(1,size)` | CERTAIN |
| `al_free` | `0x265ce0` | alloc | tail-thunk → `free(ptr)` | CERTAIN |

> **NOTE — the five barriers are byte-identical on x86.** All of `al_data_memory_barrier`, `al_local_*`, `al_local_write_*`, `al_smp_*`, and `al_smp_read_*` emit the exact six-byte sequence `f0 48 83 0c 24 00` = `lock orq $0x0,(%rsp)` — a full fence. On x86 TSO the local-vs-SMP and full-vs-write distinctions collapse to the same instruction, confirmed by `objdump` of all five. A reimplementer must not read meaning into the five distinct names *from this binary*: the names carry the Alpine API surface (so the upper HAL compiles), but on x86 they are one fence. Porting to a weakly-ordered host (ARM) requires re-deriving each from its intended ordering, since the x86 build erases the distinctions.

### Considerations

Several primitives have **no recorded in-binary caller** in this build — `al_reg_init`, `al_reg_read32_array`, the `al_smp_*` and `al_local_write_*` barrier variants — meaning they are reached only via the vendored HAL's indirect/inlined paths, the funtime/sim backend, or are dead on this build. They are documented because the *platform-services contract* requires them: a port must supply them even though this particular libnrt build does not exercise every one. The `helper.c` siblings in the same `.text` band — `buf_init @0x265e10` / `buf_append @0x265e80` (the growable byte buffer the instruction-block builders use), `dma_reset @0x265cf0` (UDMA Function-Level-Reset, `dev_base+0x48` ← `0x8000`/BIT(15)), and the sim-only `set_dma_perf_emulation @0x265d90`/`restart_qemu_inkling @0x265df0`/`set_sim_debug_flags @0x265d30` pokes — are first-party KaenaRuntime utilities, **not** Alpine platform services; they share the TU but not the porting contract.

---

## 3. A Representative Primitive and the Arch Bind

### Purpose

Two pieces of the band repay a close reading: a register primitive (to fix the failure-policy asymmetry every reimplementer trips on) and the arch latch (to fix how silicon-generation dispatch is bound). Both are reproduced below as annotated C.

### Algorithm

`al_reg_write32` (`@0x265c50`) is the write-side of every `al_*`/`aws_hal_*` register accessor, and it is **fatal on failure** — the opposite of the read side, which asserts but is shaped to look recoverable. Its companion `al_reg_read32` (`@0x2658a0`) is shown for the contrast:

```c
// al_reg_read32 @0x2658a0 — hal_platform.c:21 — read side: assert-on-fail
function al_reg_read32(reg):
    word = csr_read(reg)                                    // 0x315680, tdrv/csr.c
    if csr_read_failed:                                     // boundary returns status
        nlog_write("PCIe read failed %p", reg)              // 0x224d40
        nlog_backtrace()                                    // 0x508220
        __assert_fail(..., "hal_platform.c", 31, ...)       // trips an assertion
    return word

// al_reg_write32 @0x265c50 — hal_platform.c:43 — write side: FATAL on fail
function al_reg_write32(reg, val):
    rc = csr_write(reg, val)                                // 0x315820, tdrv/csr.c
    if rc != 0:                                             // "ret == 0" assert form
        nlog_write("\n csr_write failed")                   // 0x224d40
        al_hal_log(LEVEL_ASSERT, __func__,
                   "%s:%d:%s: Assertion failed! (%s)\n", ...) // 0x265b80 → nlog_vwrite
        al_abort_program(-1)                                // 0x265c40 → exit() — NORETURN
    // (success: nothing returned; the write is fire-and-acknowledge)
```

The arch bind. `al_hal_tpb_get_arch_type` (`@0x44bca0`) is the single most-called HAL primitive in the binary (200+ callers across `aws_hal_*`/`aws_reg_*`/`al_udma_*`/`al_sdma_*`, `tdrv_*`, `encd_*`, `vring_*`). It is a pure read of one `.bss` global, asserted in range; the *write* (`al_hal_tpb_set_arch_type @0x44bc00`) happens once at device identification and pulls in the rest of the vendored HAL via `kaena_khal_init`:

```c
// al_hal_tpb_set_arch_type @0x44bc00 — al_hal_tpb_arch.c — called once at bring-up
// by tdrv_identify_board_info / tdrv_identify_hw_platform / nrt_fake_dev_info
function al_hal_tpb_set_arch_type(target):
    assert(target != AL_HAL_TPB_ARCH_TYPE_INVALID)          // arch.c:0x22
    assert(target <  AL_HAL_TPB_ARCH_TYPE_NUM)              // arch.c:0x23
    v = (target == INVALID_1 /*1*/) ? INVALID /*0*/ : target // fold arch 1 → 0
    if kaena_khal_init(v) != OK:                            // 0x462290 — pulls in vendored HAL state
        al_hal_log(ERROR, "Failed to initialize kaena_khal for arch %u\n", v)
        return -1
    hal_target = target                                     // .bss 0xcaeb60 — the latch
    return 0

// al_hal_tpb_get_arch_type @0x44bca0 — the 200+-caller central read
function al_hal_tpb_get_arch_type():
    arch = hal_target                                       // .bss 0xcaeb60
    assert(arch != AL_HAL_TPB_ARCH_TYPE_INVALID)            // arch.c:0x3B
    assert(arch <  AL_HAL_TPB_ARCH_TYPE_NUM)                // arch.c:0x3C
    return arch                                             // enum {SUNDA=2,CAYMAN=3,MARIANA=4}
```

This one read is the **root of every per-generation fan-out** in the runtime. `tdrv_arch_ops_init` reads it to pick which registrar fills the arch vtable ([tdrv-arch-ops §2](tdrv-arch-ops.md)); the `aws_hal_*_{sunda,cayman,mariana}` register HALs branch on it; even feature gates like `is_sow_supported` (`@0x266440`) are `al_hal_tpb_get_arch_type() != SUNDA(2)`. A reimplementer who scatters `if (arch == …)` through the HAL instead of latching it once here will reproduce neither the single-assert dispatch shape nor the `kaena_khal_init`-at-set ordering.

### Function Map

| Function | Addr | Category | Role | Confidence |
|---|---|---|---|---|
| `al_reg_read32` | `0x2658a0` | reg | read-side CSR primitive; assert-on-fail | CERTAIN |
| `al_reg_write32` | `0x265c50` | reg | write-side CSR primitive; **FATAL**-on-fail | CERTAIN |
| `al_hal_log` | `0x265b80` | log | the `al_*log` → `nlog_vwrite` bridge every assert path uses | CERTAIN |
| `al_abort_program` | `0x265c40` | control | NORETURN `exit()` the FATAL write path tail-calls | CERTAIN |
| `al_hal_tpb_get_arch_type` | `0x44bca0` | arch | central arch latch read (200+ callers); range-asserted | CERTAIN |
| `al_hal_tpb_set_arch_type` | `0x44bc00` | arch | one-shot latch write + `kaena_khal_init`; folds arch 1→0 | CERTAIN |
| `kaena_khal_init` | `0x462290` | arch | vendored-HAL init the set path drives (boundary) | HIGH |

### Considerations

> **GOTCHA — write failure is unrecoverable; read failure is not.** The two register primitives have *opposite* failure policies, and the difference is structural, not stylistic. `al_reg_read32` logs and `__assert_fail`s — which in a release build with assertions compiled out would *fall through and return a garbage word*. `al_reg_write32` logs and calls `al_abort_program(-1)` → `exit()` — a hard process exit no build configuration removes. A reimplementer must preserve this: a failed doorbell or descriptor write must tear the process down (a silently-dropped write corrupts device state invisibly), whereas a failed CSR read is treated as a programming error to assert on. Copying one policy onto both primitives is a real bug, not a cleanup.

---

## 4. Map of the Four KaenaHal Pages

KaenaHal is documented across four pages; this one is the entry/map. The split is by *what the vendored layer does*, not by file:

| Page | Scope | Owns |
|---|---|---|
| **hal-adapter** (this page) | the `al_hal_plat_services` porting band + the arch latch | the ~14 `al_*` primitives, the two failure policies, the barrier collapse, `al_hal_tpb_get/set_arch_type` |
| [hal-tpb-shims](hal-tpb-shims.md) | TPB/STPB arch-dispatch shims | the `aws_get_*`/`aws_hal_*` per-arch register-offset shims keyed off the arch latch, the TPB engine-name table (`NEURON_ENG_NAMES`) |
| [hal-registers](hal-registers.md) | register and reg-offset accessors | the concrete `aws_reg_*` generated accessors and per-arch register/offset return values |
| [hal-udma-iofic](hal-udma-iofic.md) | UDMA/SDMA descriptor build + IOFIC | the `al_udma_*`/`al_sdma_*` ring/descriptor builders, `al_iofic_*` interrupt-controller config, the M2M combo-op path |

> **NOTE — the kbin→SDMA dtype and CCE-op translators live in the helper layer but are documented elsewhere.** `kbin_dtype_to_sdma_dtype` (`@0x266460`), `kbin_cce_op_to_sdma_cce_op` (`@0x2664c0`), and `kbin_get_dtype_size` (`@0x266520`) sit in the same `tdrv/helper.c` TU as the adapter primitives, and they translate NEFF/kbin descriptor metadata into the Alpine-SDMA encodings the UDMA build path consumes. They are **owned by [neff/dtype-system](../neff/dtype-system.md)**, which dumps the `CSWTCH.19`/`CSWTCH.21`/`data_type_sizes.0` backing tables in full; this page does not re-derive them. The seam to remember: the helper layer *hosts* the dtype translators, but the dtype system documents them.

## Related Components

| Name | Relationship |
|---|---|
| `csr_read @0x315680` / `csr_write @0x315820` / `axi_write @0x315850` (`tdrv/csr.c`) | the host CSR/AXI layer every `al_reg_*`/`al_mem_*` primitive bottoms into — the real BAR-access mechanism |
| `nlog_vwrite @0x7a3e0` / `nlog_write @0x224d40` / `nlog_backtrace @0x508220` (`nlog/nlog.c`) | the runtime log ring `al_hal_log` and every adapter error path bridges to |
| `tdrv_arch_ops_init @0x308e80` ([tdrv-arch-ops](tdrv-arch-ops.md)) | reads `al_hal_tpb_get_arch_type` to pick the per-arch registrar that fills the arch vtable |
| `kaena_khal_init @0x462290` | vendored-HAL initialization the `set_arch_type` latch drives once at bring-up |
| `buf_init @0x265e10` / `buf_append @0x265e80` (`helper.c`) | first-party growable-buffer utilities sharing the adapter TU; not platform services |

## Cross-References

- [KaenaHal: TPB/STPB Arch-Dispatch Shims](hal-tpb-shims.md) — the `aws_get_*`/`aws_hal_*` per-arch register-offset shims and engine-name table keyed off the arch latch documented here
- [KaenaHal: Register and Reg-Offset Accessors](hal-registers.md) — the concrete `aws_reg_*` accessors and per-arch register return values the adapter's `al_reg_*` primitives ultimately read/write
- [KaenaHal: UDMA/SDMA Descriptor Build and IOFIC](hal-udma-iofic.md) — the `al_udma_*`/`al_sdma_*`/`al_iofic_*` upper HAL that is the dominant caller of this adapter band
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](tdrv-arch-ops.md) — the userspace arch vtable whose one-shot install reads `al_hal_tpb_get_arch_type`, the latch §3 documents
- [The dtype System](../neff/dtype-system.md) — owns the kbin→SDMA dtype/CCE-op translators that share the `helper.c` TU with this adapter but are documented there, not here
- [Userspace Runtime Core — Internal Architecture Map](overview.md) — layer [4] of the six-layer stack; how a runtime call descends through this adapter to the CSR boundary
- [UDMA Core (Annapurna/Alpine Fork)](../kernel/udma-main.md) — the kernel-side Alpine UDMA this userspace HAL mirrors; same `al_udma` lineage, opposite side of the `/dev/neuron` boundary
- [back to index](../index.md)
