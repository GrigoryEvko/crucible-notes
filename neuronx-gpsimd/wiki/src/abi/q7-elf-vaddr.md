# The Q7 ELF VADDR + Per-Core Memory Model

> **Scope.** This page reconstructs the **32-bit NX-local address space** that a
> single GPSIMD Q7 core (Cadence Tensilica Vision-Q7 "Cairo", Xtensa config
> `ncore2gp`) dereferences, and the per-core view across the 8-core POOL
> cluster. It answers three questions a reimplementer must keep separate:
> (1) **where the linker puts each section** of the loaded custom-op library
> (the per-core LSP `elf32xtensa.x` → VADDR mapping); (2) **where the fixed
> hardware/runtime regions live** (IRAM, dataram, the Sequencer-register MMIO
> block, the pinned and dynamic SoC windows); and (3) **how the per-engine POOL
> firmware image's own VADDRs** (`.text@0x01000000`, data band `0x02000000`,
> `.dynamic@0x03000000`) reconcile against the custom-op map at `0x84000000`.
> These are **three different programs** in the same NX space at different times
> and must never be conflated.
>
> **Evidence base.** Every address on this page is read this task from a shipped
> artifact: the nine per-core LSP linker scripts
> `custom_op/lsp_fll_load_cpus/lsp_fll_load_cpu{0..7,_single}/ldscripts/elf32xtensa.x`
> (read as text); the EXTISA Q7 POOL firmware ELFs carved from
> `c10/lib/libnrtucode_internal.so` and inspected with host `readelf -lSW`;
> the shipped header `c10/include/neuron_sunda_arch_isa/tpb/aws_neuron_isa_tpb_nx_map.h`;
> `aws_neuron_isa_tpb_common.h` (per-gen); the per-gen `arch-headers/*/address_map.yaml`;
> and the static archive `neuron/libneuroncustomop.a` via the shipped
> `xtensa-elf-nm`/`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`,
> `XTENSA_SYSTEM=.../XtensaTools/config`). Confidence/provenance follows each
> claim: HIGH/MED/LOW × OBSERVED (read this task) / INFERRED (derived) /
> CARRIED (from a cited Part-7 page or the shared pack).

---

## 0. The model in one screen

A GPSIMD Q7 core addresses a **32-bit NX-local** space (`void*` is 4 bytes on
this ELF32 target). Three distinct programs occupy that space at different
times:

1. **The per-engine POOL firmware** (the baked POOL ucode + the relocatable
   EXTISA kernel containers). Linked at `.text` VADDR `0x01000000`, data band
   `0x02000000`, `.dynamic` `0x03000000`. This is the *engine's own* program.
2. **The loaded custom-op library** (`libneuroncustomop.a` linked through an
   LSP). Its LSP puts only the four XEA3 vector stubs in the low IRAM
   (`0x0`, 4 KiB) and **everything else** — code, rodata, data, bss — in a
   **per-core 2 MiB slice** at NX `0x84000000 + cpu_id·0x200000`.
3. **The fixed hardware/runtime regions** every core sees identically: the
   on-core **dataram** at NX `[0x80000, 0x90000)` (direct deref), the **NX MEM
   REG** MMIO block at `[0x100000, 0x100850)`, the **SBUF** 64 MB pinned window
   at `0x80000000`, the **hbm_scratch** 64 MB pinned window at `0x84000000`, and
   three **dynamic 16 MB windows** at `0x07/09/0a000000`.

The per-core model is **identical-layout-via-PRID, not distinct VADDRs**: the
firmware image and the fixed regions present the *same* NX map on every core. A
core learns *which* of the eight it is at run time via `rsr.prid`
(`get_cpu_id`), and computes its private SoC apertures from that. The **only**
NX address that shifts per core is the custom-op library's `sram0_0_seg` base
(`0x84000000 + cpu_id·2 MiB`) — the link-time half of the dual identity.

> **GOTCHA (the three programs, three bands).** `0x01000000` (firmware `.text`)
> and `0x84000000` (custom-op code) are **not the same image**. The firmware
> EXTISA container is `ET_EXEC` linked at `0x01000000` and relocated by the
> dynamic-kernel-load path; the custom-op `.so` is a separate staged image at
> `0x84000000`. A funcVA `0x010000xx` in `kernel_info_table` belongs to the
> firmware kernel; the custom-op's own functions live at `0x84000000+i·2 MiB`.
> Both statements are true of *different* images in *different* NX bands.

---

## 1. The unified Q7 NX 32-bit VADDR map

This is the consolidated NX-local map a single Q7 core dereferences. Part (A) is
the directly-dereferenceable fixed regions; part (B) is the windowed regions (an
NX slice that is a movable view of a 57-bit SoC address). Sizes/bases are
re-verified this task from the named artifact or CARRIED from the cited sibling.

| NX base / range | size | what it is / which section maps there | conf / src |
|---|---|---|---|
| `[0x00000, 0x80000)` | 512 KiB | **Low-NX IRAM** (instruction + early state). Firmware flat IRAM image loads at byte 0 (reset vector). Custom-op uses only the first **4 KiB** (`iram0_0_seg` org `0x0` len `0x1000`, the four XEA3 vector sections). LSP region bound `_memmap_mem_iram0_end = 0x10000` (64 KiB), but the *segment* uses `0x1000`. | HIGH / OBSERVED (LSP) |
| `[0x80000, 0x90000)` | 64 KiB | **Per-core on-core DATARAM** (the Q7's own DRAM seen locally). Direct NX deref — "the pointer is the address", no TLB. The custom-op runtime carves its dataram + libc xmem heaps here; the `.bss` globals (`_ctx`, `_sbuf_window`, heap mgrs, dma ctx) resolve into this window. | HIGH / OBSERVED (assert `data_transfer.cpp:160`) |
| `[0x90000, 0x100000)` | ~448 KiB | **Gap** between dataram and the MEM REG block. | INFERRED (structural) |
| `[0x100000, 0x100850)` | ~2 KiB | **NX MEM REG block** ("Sequencer Registers"): MMIO CSRs incl. `ENGINE_BASE_ADDR` (LO `0x028` / HI `0x02C`), `SEQUENCER_WINDOW` (`0x100/0x104`), `MEM_WINDOW0..7` (`0x200..0x23C` lo/hi pairs), `TPB_WINDOW0..1` (`0x300..0x30C`). **The window-config registers the dynamic-window refill programs live here.** | HIGH / OBSERVED (`nx_map.h`) |
| `0x07000000` | 16 MB win | **Dynamic window slot 0** → HW `MEM_WINDOW3` (`0x100218`). | HIGH / OBSERVED |
| `0x09000000` | 16 MB win | **Dynamic window slot 1** → HW `MEM_WINDOW5` (`0x100228`). | HIGH / OBSERVED |
| `0x0a000000` | 16 MB win | **Dynamic window slot 2** → HW `MEM_WINDOW6` (`0x100230`). Round-robin `%3`; carry general HBM tensor, PSUM, on-chip SBUF-scratch, EVT_SEM, the HBM stack (≤4 MB fits one), and remote die/chip. | HIGH / OBSERVED (region tags); MED (per-slot routing) |
| `0x80000000` | 64 MB win | **SBUF 64 MB PINNED window** (record 3). NX `[0x80000000, 0x84000000)` → SoC SBUF. SoC tag = `ENGINE_BASE − 41 MiB`, cached in `_sbuf_window`. Never evicted. | HIGH / OBSERVED (NX base); MED / CARRIED (SoC tag) |
| `0x84000000` | 64 MB win | **hbm_scratch 64 MB PINNED window** (record 4) → the HBM-resident scratch heap. **The custom-op `.so` is linked into the LOW slice of this window** (§2/§3). Not the on-chip `STATE_BUF_SCRATCH_RAM`. | HIGH / OBSERVED (NX base + LSP org) |

The window finalizer and the two granule masks (16 MB → offset bits `[23:0]`,
mask `0xff…ff000000`; 64 MB → offset bits `[25:0]`, mask `0xff…fc000000`), and
the masked-tag compare that selects a window, are decoded in
[neuron_translate + the Window Table](neuron-translate-windows.md). The
`0x07/0x09/0x0a000000` region tags above are the literal `slli a?, a?, 24/25`
constants emitted by `_init_translate_ctx` (`movi a5,7; slli a5,a5,24` →
`0x07000000`; `movi a9,9; slli …` → `0x09000000`; `slli a5,a10,25` with a10=5 →
`0x0a000000`), read this task by `xtensa-elf-objdump` of `translation.o`
(HIGH/OBSERVED).

> **NOTE (the allocator-proxy `0x80000000`).** The HBM xmem allocator uses
> `0x80000000` as an offset-bookkeeping *proxy* base; it numerically coincides
> with the SBUF window NX base but is **never dereferenced through it** (the
> allocator subtracts it back). Do not conflate the allocator proxy anchor with
> the real SBUF HW window. (CARRIED from [neuron-translate-windows.md](neuron-translate-windows.md) / the xmem
> allocator page.)

### As a C-style memory-map header

```c
// Q7 NX-local 32-bit address space (one core's view), all values HIGH/OBSERVED
// unless tagged. void* and every NX address are 32-bit on this ELF32 target.

#define NX_IRAM_BASE          0x00000000u   // low-NX IRAM (firmware flat / vectors)
#define NX_IRAM_REGION_END    0x00010000u   //   LSP region bound (64 KiB)
#define NX_CUSTOMOP_VEC_END   0x00001000u   //   custom-op uses only first 4 KiB

#define NX_DATARAM_BASE       0x00080000u   // per-core on-core DRAM, DIRECT deref
#define NX_DATARAM_END        0x00090000u   //   64 KiB; xmem dataram+libc heaps + .bss

#define NX_MEM_REG_BASE       0x00100000u   // Sequencer-register MMIO block
#define NX_MEM_REG_END        0x00100850u   //   highest named reg = RSVD_SPACE4 @+0x84c
//   register offsets inside the block (from aws_neuron_isa_tpb_nx_map.h):
#define NX_REG_ENGINE_BASE_LO 0x028u        //   ENGINE_BASE_ADDR_LO
#define NX_REG_SEQ_WINDOW_LO  0x100u        //   SEQUENCER_WINDOW_LO
#define NX_REG_MEM_WINDOW0_LO 0x200u        //   MEM_WINDOW0_LO  (== SUNDA_APB_BASE)
#define NX_REG_MEM_WINDOW3_LO 0x218u        //   -> dynamic slot 0 @ NX 0x07000000
#define NX_REG_MEM_WINDOW5_LO 0x228u        //   -> dynamic slot 1 @ NX 0x09000000
#define NX_REG_MEM_WINDOW6_LO 0x230u        //   -> dynamic slot 2 @ NX 0x0a000000

#define NX_DYNWIN0_BASE       0x07000000u   // dynamic 16 MB window 0 (MEM_WINDOW3)
#define NX_DYNWIN1_BASE       0x09000000u   // dynamic 16 MB window 1 (MEM_WINDOW5)
#define NX_DYNWIN2_BASE       0x0a000000u   // dynamic 16 MB window 2 (MEM_WINDOW6)
#define NX_DYNWIN_SIZE        0x01000000u   //   16 MB each; offset bits [23:0]

#define NX_SBUF_WIN_BASE      0x80000000u   // SBUF 64 MB pinned window (record 3)
#define NX_HBMSCRATCH_BASE    0x84000000u   // hbm_scratch 64 MB pinned (record 4)
#define NX_PINNED_WIN_SIZE    0x04000000u   //   64 MB each; offset bits [25:0]

// the custom-op .so lives in the LOW slice of the hbm_scratch window:
#define NX_CUSTOMOP_BASE(cpu) (NX_HBMSCRATCH_BASE + (cpu) * 0x00200000u)  // 2 MiB stride

// NOT linker-placed — runtime objects (see §5):
//   stack : translated HBM-scratch SoC addr via one dynamic window (<=4 MB)
//   heaps : dataram (DSM) pools + the hbm_scratch heap
```

---

## 2. The linker section → VADDR mapping (the custom-op LSP)

The custom-op toolchain ships **nine** LSPs (`lsp_fll_load_cpu{0..7,_single}`),
each a directory containing five Xtensa ldscripts
`elf32xtensa.{x,xn,xbn,xr,xu}` and a `specs` file. The default full link uses
`elf32xtensa.x`. Read byte-exact this task (`lsp_fll_load_cpu0`):

### The MEMORY block — exactly two regions

```
MEMORY {
  iram0_0_seg : org = 0x00000000, len = 0x1000      /* 4 KiB low-NX IRAM        */
  sram0_0_seg : org = 0x84000000, len = 0x200000    /* 2 MiB hbm_scratch sub-win */
}
_memmap_region_map      = 0x00000011               /* bits 0,4 = the two regions */
PROVIDE(_memmap_reset_vector  = 0x0)
PROVIDE(_memmap_vecbase_reset = 0x0)
ENTRY(_start)
/* boundary symbols (cpu0): */
_memmap_mem_iram0_start = 0x0       ; _memmap_mem_iram0_end   = 0x10000   /* 64 KiB */
_memmap_seg_iram0_0_start = 0x0     ; _memmap_seg_iram0_0_max = 0x1000    /* 4 KiB  */
_memmap_mem_sram0_start = 0x84000000; _memmap_mem_sram0_end   = 0x84200000
_memmap_seg_sram0_0_start = 0x84000000; _memmap_seg_sram0_0_max = 0x84200000
```

(HIGH/OBSERVED — the MEMORY block, `region_map`, the reset-vector/vecbase
provides, and all four boundary-symbol pairs read this task.)

> **GOTCHA (what "9 SRAM origins" means).** The MEMORY block of *one* LSP has
> only **two** regions. The "9 SRAM origins" are the **nine LSP variants'**
> `sram0_0_seg` origins — one per `lsp_fll_load_cpu{0..7,_single}` — enumerated
> in §3a. There is no nine-region MEMORY block.

### The IRAM segment (`iram0_0_seg` @ NX `0x0`, 4 KiB) — four sections

| line | section | role |
|---|---|---|
| 58 | `.DispatchVector.text` (KEEP) | XEA3 dispatch vector |
| 66 | `.ResetVector.text` | reset vector |
| 74 | `.ResetHandler.text` (`.literal`+`.text`) | reset handler |
| 82 | `.DispatchHandler.text` (`.literal`+`.text`) | XEA3 dispatch handler |

These four close `_memmap_seg_iram0_0_end = ALIGN(0x8)` and
`_memmap_mem_iram0_max`. **`xtensa-elf-nm` over `libneuroncustomop.a` defines
NONE of these four sections** (zero hits for
`.DispatchVector`/`.ResetVector`/`.ResetHandler`/`.DispatchHandler`) — they are
supplied by the loader/CRT, not the custom-op archive. The archive **does**
define `_start` (`00000af8 T _start`, in `start_exit.o`), so `ENTRY(_start)`
resolves into the custom-op CRT object while the IRAM vector stubs are
toolchain-supplied. (HIGH/OBSERVED this task; refines the earlier MED guess on
the vector providers.)

### The SRAM segment (`sram0_0_seg` @ NX `0x84000000 + cpu·2 MiB`) — everything else

The `>sram0_0_seg :sram0_0_phdr` LOAD segment (16 output sections, in order):

```
.text  (_stext; .entry.text/.init/.literal*/.text*/.fini; _etext)   <- CODE
.clib.rodata  .rtos.rodata  .clib.data
.eh_frame  .ctors  .dtors
.rodata     (incl. __XT_EXCEPTION_TABLE__/.xt_except_table, the C++
             ctor/dtor tables, and the _bss_table_start..end pair the CRT
             reads to zero .bss: LONG(_bss_start); LONG(_bss_end))
.clib.text  .rtos.text
.clib.percpu.data  .rtos.percpu.data  .rtos.data
.interp  .data  (incl .sdata/.sdata2/.jcr/__llvm_prf_*)
.note.gnu.build-id
```

The `(NOLOAD)` bss segment (the only `>sram0_0_seg :sram0_0_bss_phdr`):

```
.bss (NOLOAD), ALIGN(8): .dynsbss/.sbss*/.scommon/.dynbss/.bss/COMMON/
       .clib.bss/.clib.percpu.bss/.rtos.percpu.bss/.rtos.bss; _bss_start/_bss_end;
       closes _memmap_seg_sram0_0_end = ALIGN(8).
```

So **all** custom-op code + rodata + data + percpu + ctor/dtor + exception
tables + (NOLOAD) bss land in the per-core hbm_scratch sub-window. The `.bss` is
NOBITS — it occupies NX address space but ships no bytes; the CRT zeroes it via
the `_bss_table` pair. (HIGH/OBSERVED — every `>sram0_0_seg` assignment read.)

The debug/metadata tail (`.debug.*`, `.xt.insn/.xt.prop/.xt.lit`,
`.xtensa.info`, `.comment`, `.note.GNU-stack`) sits at `addr 0` and is not
loaded. `.note.GNU-stack` is the executable-stack *marker*, not a runtime stack.

> **CORRECTION (firmware data band ≠ DMA window).** The shared pack listed the
> firmware program-header data band as `.data@0x00080000`. The carved firmware
> ELF disagrees: its data band is **`0x02000000`** (`readelf -lSW`, §4).
> `0x00080000` is the **dataram DMA-staging window** `[0x80000, 0x90000)` (§1),
> a *different* region entirely. The two were transcribed together in the brief;
> they are distinct facts.

> **NOTE (stack + heap absent from the LSP).** `rg` over the ldscript: **no**
> `__stack`, **no** `_heap_sentry`, **no** `dram0` region — only the
> `.note.GNU-stack` metadata marker. `xtensa-elf-nm` over the archive: **no**
> undefined ref to `__stack`/`_heap_sentry`. The stack and the three xmem heaps
> are runtime-carved, not linker-placed (§5).

### The five ldscript variants

`.x` = the address-bearing full link (this page). `.xn`/`.xbn` are identical to
`.x` except the mode comment (fixed in-memory target, no demand paging).
`.xr`/`.xu` are **relocatable**: no MEMORY block, no origins, address-independent.
The per-core identity lives only in `.x`/`.xn`/`.xbn`. The `specs` file (pins
`crti/crtbegin..crtend/crtn`, blanks `*lib`) carries **no** addresses.
(CARRIED/OBSERVED; `specs` read this task.)

---

## 3. The per-core model — identical-via-PRID, one link-time per-core VADDR

### 3a. The nine SRAM origins (re-grepped all 9 LSPs this task)

| LSP | `sram0_0_seg` org | len | `_memmap` range |
|---|---|---|---|
| `lsp_fll_load_cpu0` | `0x84000000` | `0x200000` | `[0x84000000, 0x84200000)` |
| `lsp_fll_load_cpu1` | `0x84200000` | `0x200000` | `[0x84200000, 0x84400000)` |
| `lsp_fll_load_cpu2` | `0x84400000` | `0x200000` | `[0x84400000, 0x84600000)` |
| `lsp_fll_load_cpu3` | `0x84600000` | `0x200000` | `[0x84600000, 0x84800000)` |
| `lsp_fll_load_cpu4` | `0x84800000` | `0x200000` | `[0x84800000, 0x84A00000)` |
| `lsp_fll_load_cpu5` | `0x84A00000` | `0x200000` | `[0x84A00000, 0x84C00000)` |
| `lsp_fll_load_cpu6` | `0x84C00000` | `0x200000` | `[0x84C00000, 0x84E00000)` |
| `lsp_fll_load_cpu7` | `0x84E00000` | `0x200000` | `[0x84E00000, 0x85000000)` |
| `lsp_fll_load_cpu_single` | `0x84000000` | `0x2000000` | `[0x84000000, 0x86000000)` |

`org = 0x84000000 + cpu_id·0x200000` (exact **2 MiB** stride). The eight cores'
SRAM windows **tile** `[0x84000000, 0x85000000)` = 16 MiB with no gap or overlap;
`cpu_single` owns the whole 32 MiB single-core build region. The
`iram0_0_seg = org 0x0, len 0x1000` is **identical for all 9** (verified this
task). (HIGH/OBSERVED — `rg` of every `elf32xtensa.x` plus the `_memmap_mem_sram0`
boundary symbols on cpu0/cpu5/cpu7/single.)

### 3b. The two identity halves

- **Link-time (per-core DISTINCT NX address).** The custom-op `.so`'s
  `sram0_0_seg` base shifts per core (3a). So a core's custom-op
  `.text/.rodata/.data/.bss` live at a per-core-distinct NX address — cpu3's
  code at `0x84600000..`, cpu5's at `0x84A00000..`. One object set, eight link
  variants, base-shifted. (HIGH/OBSERVED.)
- **Run-time (per-core SELECTION by PRID).** Everything *else* in the NX map (the
  low IRAM, the dataram window `[0x80000, 0x90000)`, the MEM REG block
  `@0x100000`, the pinned SBUF/hbm_scratch window NX bases, the three dynamic
  window NX bases) is the **same** 32-bit map on every core. A core identifies
  itself by `rsr.prid` (`get_cpu_id` = raw PRID, no mask) and computes its
  private SoC apertures from the PRID (e.g. the dataram SDMA aperture index =
  `2·prid + 9`). The assert `data_transfer.cpp:171 cpu_id < 8` (read this task)
  guards the dense `[0..7]` index. (HIGH/OBSERVED + CARRIED cross-report.)

### 3c. Shared vs per-core-local regions

**Per-core LOCAL** (own region; same NX view, distinct SoC backing):

- **on-core DATARAM** `POOL_Q7_CORE{i}_DRAM` 256 KiB (`size: 262144` this task)
  → NX view `[0x80000, 0x90000)`, per-core PRIVATE.
- **on-core IRAM** `POOL_Q7_CORE{i}_IRAM` 128 KiB (`size: 131072` this task),
  1 MiB SoC slot pitch.
- **custom-op hbm_scratch sub-window** 2 MiB per core (3a), link-assigned, PRIVATE.
- **SDMA SoC aperture** 64 KiB per core, `idx·0x10000` apart.
- **`.bss` runtime globals** (cached PRID, the heap mgrs): per-core, in the
  per-core dataram / per-core sram window.

**SHARED** (one physical region; per-core *view* is window-private):

- **SBUF** (32 MiB), **PSUM** (4 MiB), **DGE_MEMORY** (1 GiB),
  **STATE_BUF_SCRATCH_RAM** (32 MiB): one per TPB, reached via the pinned SBUF
  window / the dynamic windows. The HBM-scratch pool is shared backing; each
  core reaches it through its **own** pinned `0x84000000` window (a HIT) and its
  own 2 MiB code slice.
- **The NX MEM REG block `@0x100000`** is per-core MMIO (each core has its own
  Sequencer register file), but the NX **address** is identical on all cores.

There is **no** cross-core sync in the custom-op library (zero
atomics/barriers/semaphores); each core is fully independent with per-core
private memory. (HIGH/OBSERVED cross-report.)

### 3d. PREPROC (4×Q7) vs POOL (8×Q7)

`PREPROC_n → Q7_CORE0..3` (4 cores); `TPB_n POOL → Q7_CORE0..7` (8 cores). Both
are the **same** Q7-cluster IP (`LOCAL_REG @ +0x3060000`, `CORE0_IRAM @
+0x3100000`, 1 MiB slot pitch, IRAM 128 KiB / DRAM 256 KiB intra-slot). POOL
adds front-matter PREPROC lacks (`POOL_IRAM` 32 KiB, `POOL_NX_IRAM` 128 KiB,
`POOL_NX_DRAM` 64 KiB, profile CAM/table). **The custom-op ABI targets the
8-core POOL cluster only** (`get_cpu_count()` hardcoded 8). Each core's NX-LOCAL
map is identical across both PREPROC and POOL — the difference is the
SoC-physical base of the cluster (`LOCAL[46:0]` + `DIE[47]`), **not** the NX
view. (HIGH/OBSERVED — POOL CORE0 IRAM/DRAM sizes re-grepped this task.)

---

## 4. The firmware-image VADDRs — reconciled against the custom-op map

The per-engine POOL firmware is a **separate** artifact from the loaded
custom-op `.so`. This task carved the EXTISA Q7 POOL ELFs from
`libnrtucode_internal.so` (a host x86-64 `.so` with **16 embedded ELF32-Xtensa**
images, `e_machine = 94`) and read their headers with host `readelf`.

### 4a. The EXTISA ELF (CAYMAN_0) — `readelf -lSW` this task

Type `EXEC`, machine "Tensilica Xtensa Processor", `ELFCLASS32`, entry
`0x01005610`. **Three LOAD program headers**:

```
  Type    Offset   VirtAddr   PhysAddr   FileSiz MemSiz Flg Align
  LOAD    0x000100 0x01000000 0x01000000 0x06f1e 0x06f1e R E 0x80   -> .text
  LOAD    0x007080 0x02000000 0x02000000 0x00450 0x0048c RWE 0x80   -> .rodata.eh_frame.ctors.dtors.data.kernel_info_table.globstruct.bss
  LOAD    0x007500 0x03000000 0x03000000 0x00c08 0x00c08  WE 0x80   -> .dynamic.rela.got
```

Section VADDRs (`readelf -SW`):

| section | VADDR | size | flags |
|---|---|---|---|
| `.text` | `0x01000000` | `0x6f1e` | AX |
| `.rodata` | `0x02000000` | `0x1ec` | A |
| `.eh_frame` | `0x020001f8` | `0x4` | A |
| `.ctors` | `0x020001fc` | `0x38` | A |
| `.dtors` | `0x02000234` | `0x8` | A |
| `.data` | `0x02000240` | `0x140` | WA |
| `kernel_info_table` | `0x02000380` | `0x88` | WA (17 records, 8-byte) |
| `.globstruct` | `0x02000408` | `0x48` | WA (magic `0x6099cb34`) |
| `.bss` | `0x02000450` | `0x3c` | NOBITS |
| `.dynamic` | `0x03000000` | `0xa4` | WA |
| `.rela.got` | `0x030000c8` | `0xb40` | RELA (`ES=0xc`) |

`kernel_info_table` entry 0 (raw `@file 0x7400`, read this task with xxd):
`00 00 00 7e 80 00 00 01` = `{spec=0, opcode=0x7e}` funcVA **`0x01000080`**. The
`opcode=0xf0` row `@0x7430` = `00 00 00 f0 70 33 00 01` → funcVA **`0x01003370`**.
The `.globstruct` magic `@file 0x7488` = `34 cb 99 60` (LE = `0x6099cb34`),
confirmed this task. So the funcVAs sit in the `.text` `0x01000000` band and the
table itself sits at `0x02000380` in the data band. (HIGH/OBSERVED — `readelf
-lSW` + xxd this task.)

> **CORRECTION (the `.rela.got` reloc count).** The backing report described
> `.rela.got` as "17 `R_XTENSA_RELATIVE` funcVA relocs". `readelf -rW` this task
> shows the table holds **240 entries** (`0xb40 / 0xc`), of which **30** are
> `R_XTENSA_RELATIVE` (plus 101 `SLOT0_ALT`, 101 `SLOT0_OP`, 8 `NONE`). The
> "17" is the **`kernel_info_table` record count** (`0x88 / 8 = 17`), which is
> correct for *that* table — it was mis-attributed to `.rela.got`. Net: 17
> kernel-info records, 30 `R_XTENSA_RELATIVE` relocs, 240 reloc-table entries.

### 4b. Per-image / per-gen stability of the firmware VADDRs

`readelf` on CAYMAN_0..3 + MARIANA_PLUS_0 (carved + inspected this task):

| image | entry | `.text` band | data band | `.dynamic` band |
|---|---|---|---|---|
| CAYMAN_0 | `0x01005610` | `0x01000000` | `0x02000000` | `0x03000000` |
| CAYMAN_1 | `0x01000410` | `0x01000000` | `0x0200000c` | `0x03000000` |
| CAYMAN_2 | `0x01000828` | `0x01000000` | `0x0200000c` | `0x03000000` |
| CAYMAN_3 | `0x01003c74` | `0x01000000` | `0x02000000` | `0x03000000` |
| MPLUS_0 | `0x01005658` | `0x01000000` | `0x02000000` | `0x03000000` |

The **three-band VADDR layout** (`.text @0x01000000` / data `@0x02000000` /
`.dynamic @0x03000000`) is **stable** across all engines and generations. Only
the **entry point** and the **per-segment sizes** vary per image. (A couple of
images start the data LOAD a few bytes high — `0x0200000c` — because `.rodata`
is empty and the first populated section is `.data`/`.eh_frame`; the *band* is
unchanged.) The SUNDA EXTISA container (entry `0x010000c8`, same three-band
layout) lives in the `neuronx-runtime` tree, not this gpsimd tree, and is
CARRIED rather than re-carved. (HIGH/OBSERVED on the five gpsimd-tree images.)

### 4c. Reconciliation — why `0x01000000` (firmware) vs `0x84000000` (custom-op)

These are **two different programs** in the Q7 NX space:

- **The firmware engine image** links its `.text` into the `0x01000000` band and
  its data into the `0x02000000` band. The **flat baked POOL/Q7 IRAM image**
  boots at NX byte 0 (reset vector). The **EXTISA ELF** — an `ET_EXEC`
  relocatable kernel container (note `.rela.got`) — is *linked* at `0x01000000`
  and *relocated/loaded* by the dynamic-kernel-load / prelink path to its run
  location. So `0x01000000/0x02000000/0x03000000` are the EXTISA's **link-time**
  VADDRs, not necessarily the final run-time NX addresses; the flat image that
  boots at reset is the one at NX 0. The two are consistent. (HIGH/OBSERVED on
  the VADDRs; MED on the link-vs-load distinction, grounded in `ET_EXEC` +
  `.rela.got` + the prelink path.)
- **The custom-op library** is a separate loaded `.so`: its LSP places its code
  at `0x84000000` (the hbm_scratch window), **not** in the IRAM band. It runs as
  a packaged fixed-location library dropped into the per-core SRAM window; the
  firmware engine image is what dispatches into it.

The funcVAs `0x010000xx` (the `kernel_info_table` entries) belong to the EXTISA
firmware-kernel container; the custom-op `.so`'s own code is at
`0x84000000 + i·2 MiB`. Both VADDR sets are real, in different NX bands.

---

## 5. Stack + heap — runtime, not linker-placed

The LSP reserves **neither** stack nor heap (§2; re-verified: no
`__stack`/`_heap_sentry`/`dram0` in the ldscript, no undefined ref in the
archive). Where they live:

- **STACK.** Established at load time by the FLL loader/CRT (not a linker
  `__stack`). The kernel's budget is a runtime heuristic; the switch test is
  `remaining = SP − ISL` (the ISL stack-limit SR). If a kernel needs more,
  `switch_stack_or_call_wrapper` `neuron_hbm_allocate`s a ≤4 MB HBM stack,
  `neuron_translate`s it to a 32-bit NX SP, lays out a downward stack, and
  installs `ISL = new_stack_base` so the HW guards the HBM floor. The HBM stack
  is reached through **one** dynamic 16 MB window. So the stack NX address is a
  **runtime** translated HBM-scratch SoC address, never a link-time reservation.
  The HBM-stack **arena** capacity varies per gen — **cayman 24 GiB, mariana
  36 GiB, sunda 16 GiB** (`HBM_STACK_CAPACITY_GIB`, read this task across the
  per-gen `common.h`). That is the arena, not the per-stack `MAX_STACK_SIZE`
  (4 MB). (HIGH/OBSERVED the LSP absence + the capacities; CARRIED the mechanism.)
- **HEAPS.** The three xmem heaps are runtime-carved. The dataram + libc heaps
  come from the data-scratch map, inside `[0x80000, 0x90000)`; the HBM heap is
  over `extended_isa::sdk::hbm_scratch` reached through the `0x84000000` pinned
  window. The LSP places only the heap **managers'** `.bss` bookkeeping
  (`>sram0_0_seg`), not the pools — corroborated by the archive's
  `_hbm_heap_mgr` / `_dataram_heap_mgr` / `_dataram_libc_heap_mgr` BSS symbols
  (`xtensa-elf-nm` this task). (HIGH/OBSERVED the LSP absence; CARRIED the
  allocator detail.)

---

## 6. The 64 KiB device-code cap + the staging landing

The host runtime stages the prelinked custom-op image onto the device. The
enforced **maximum staged-image size is `0x10000` = 64 KiB** (inclusive); a
library larger is rejected ("Prelinked library would be larger than the
available buffer on device"). (CARRIED.) This sits **well inside** the per-core
2 MiB `sram0_0_seg` window (§3a): the custom-op image (code+rodata+data, ≤64 KiB)
fits with ~1.94 MiB of headroom backing the `.bss` runtime globals and in-window
scratch. The 16 MiB host-side `device_malloc` is a staging allocation, **not**
the on-device window; on-device residency is the 64 KiB image inside the 2 MiB
LSP window. The firmware-image region enum (`0=IRAM/1=DRAM/2=SRAM/3=EXTRAM`) is
distinct from the LSP's `iram0_0`/`sram0_0` segment names — do not conflate the
resolver's region enum with the LSP's segment names. (CARRIED.)

---

## 7. The consolidated reconciliation table (NX VADDR ↔ section ↔ SoC)

| NX VADDR / region | what / section | SoC backing | kind |
|---|---|---|---|
| `0x0` (+vector shift) | reset vector (firmware flat) / XEA3 vectors 4 KiB (custom-op `iram0_0_seg`) | on-core IRAM (`POOL_Q7_CORE{i}_IRAM` 128 KiB) | DIRECT (boot) |
| `0x01000000` | firmware EXTISA `.text` (link VA), funcVAs `0x010000xx` | on-core IRAM (relocated by DKL/prelink) | DIRECT (relocated) |
| `0x02000000` | firmware EXTISA data band (`kernel_info_table @0x02000380`) | on-core DRAM | DIRECT |
| `[0x80000, 0x90000)` | per-core dataram / custom-op `.bss` globals + xmem dataram/libc heaps | `POOL_Q7_CORE{i}_DRAM` 256 KiB; SDMA alias `idx=2·prid+9` | DIRECT (no TLB) |
| `[0x100000, 0x100850)` | NX MEM REG block (`MEM_WINDOW0..7` etc.) | per-core MMIO | DIRECT (MMIO) |
| `0x07/09/0a000000` | 3 dynamic 16 MB windows (general HBM/PSUM/SBUF-scratch/EVT_SEM/HBM stack/remote die) | any 16 MB SoC region (programmed into `MEM_WINDOW3/5/6`) | DYNAMIC TLB |
| `0x80000000` | SBUF 64 MB pinned window | SoC SBUF | PINNED (record 3) |
| `0x84000000` (+`i·2 MiB`) | hbm_scratch 64 MB pinned window; **custom-op `.so` code/data/bss `sram0_0_seg` per-core sub-window** | HBM hbm_scratch heap (per-core 2 MiB slice) | PINNED (record 4) |
| *(stack)* | NOT in LSP — runtime HBM stack (translated NX SP via 1 dyn win) | HBM-scratch (≤4 MB) | RUNTIME |
| *(heaps)* | NOT in LSP — runtime xmem heaps | dataram (DSM) / HBM | RUNTIME |

---

## 8. Per-gen / per-engine stability summary

- **Custom-op LSP NX map** (`iram0_0` `0x0`/4 KiB + `sram0_0` `0x84000000`/2 MiB
  per core) is the shipped Sunda-targeted layout
  (`/opt/workspace/SundaCustomOpLibrary/...`). The vtable/staging contract is
  generation-invariant. (HIGH.)
- **Firmware-image VADDR bands** (`.text 0x01000000` / data `0x02000000` /
  `.dynamic 0x03000000`) are **stable** across POOL EXTISA engines and across
  CAYMAN/MARIANA/MARIANA_PLUS/SUNDA (verified this task on five gpsimd-tree
  images). `kernel_info_table @0x02000380` is gen-stable. Only entry point +
  segment sizes vary. (HIGH/OBSERVED.)
- **SoC-physical geometry** (cluster bases, 1 MiB IRAM/DRAM slot pitch, SBUF
  32 MiB / PSUM 4 MiB / DGE 1 GiB / SBUF-scratch 32 MiB) is byte-identical
  across cayman/mariana/mariana_plus. The only cross-gen delta in the POOL
  neighbourhood is **`POOL_NX_DRAM` 64 KiB (cayman) → 128 KiB
  (mariana/mariana_plus)** — verified this task (`size: 65536` cayman vs
  `_size: 0x20000` mariana_plus). The HBM-stack arena capacity varies per gen
  (24/36/16 GiB). (HIGH/OBSERVED.)
- **The Q7 NX-LOCAL map** (fixed regions + window NX bases) is per-core-invariant
  and per-cluster-invariant: PREPROC and POOL cores see the same 32-bit NX view;
  only the SoC-physical base of the cluster (`LOCAL[46:0]`/`DIE[47]`) differs.
  (HIGH/OBSERVED cross-report.)

> **NOTE (v5 / Maverick).** The Maverick (NC-v5) `arch_isa` ships a `common.h`
> but **no** `HBM_STACK_CAPACITY_GIB` constant, so its stack-arena value is not
> directly observed; its POOL geometry is header-OBSERVED only and the NX-map
> claims for v5 are **INFERRED** by carrying the gen-invariant layout.

---

## 9. Confidence / observed-vs-inferred ledger

| Claim | Anchor (read this task) | Conf / Prov |
|---|---|---|
| Custom-op MEMORY block: `iram0_0` `0x0`/`0x1000` (all 9) + `sram0_0` `0x84000000+i·0x200000`/`0x200000`; `region_map 0x11`; reset/vecbase `0x0` | `rg` of all 9 `elf32xtensa.x` | HIGH / OBSERVED |
| 9 SRAM origins + 2 MiB stride + 16 MiB tiling; single = `0x84000000`/32 MiB | `rg` of all 9 `sram0_0_seg` org + `_memmap_mem_sram0` symbols | HIGH / OBSERVED |
| `_start = 0x00000af8 T`; 4 IRAM vector sections NOT in archive; no `__stack`/`_heap_sentry` | `xtensa-elf-nm libneuroncustomop.a` | HIGH / OBSERVED |
| EXTISA 3 LOAD segs `0x01000000/0x02000000/0x03000000`; `.text 0x6f1e`, `kernel_info_table @0x02000380` (17 rec), `.globstruct @0x02000408` magic `0x6099cb34`, `.bss @0x02000450` | `readelf -lSW` + xxd CAYMAN_0 | HIGH / OBSERVED |
| Per-image entries `0x01005610/410/828/3c74`, MPLUS_0 `0x01005658`; bands stable | `readelf -l` × 5 carved images | HIGH / OBSERVED |
| `.rela.got` = 240 entries, 30 `R_XTENSA_RELATIVE` (CORRECTS "17") | `readelf -rW CAYMAN_0` | HIGH / OBSERVED |
| `NX_MEM_REG_BASE 0x00100000`; `MEM_WINDOW0..7 @0x200..0x23C`; `MEM_WINDOW3/5/6_LO @0x218/0x228/0x230`; `ENGINE_BASE @0x028/0x02C`; block top `RSVD_SPACE4 @+0x84c` | `aws_neuron_isa_tpb_nx_map.h` | HIGH / OBSERVED |
| dataram window `[0x80000,0x90000)` assert; `cpu_id < 8`; `MEM_WINDOW0_LO == SUNDA_APB_BASE` | `strings` `data_transfer.cpp:160/171/240` | HIGH / OBSERVED |
| dynamic-window region tags `0x07/0x09/0x0a000000` | `xtensa-elf-objdump translation.o` (`slli a?,a?,24/25`) | HIGH / OBSERVED |
| `POOL_Q7_CORE0_IRAM` 131072 / `_DRAM` 262144; `POOL_NX_DRAM` 65536 (cayman) / `0x20000` (mariana_plus) | per-gen `address_map.yaml` | HIGH / OBSERVED |
| `HBM_STACK_CAPACITY_GIB` cayman 24 / mariana 36 / sunda 16 | per-gen `common.h` | HIGH / OBSERVED |
| SBUF SoC tag = `ENGINE_BASE − 41 MiB`; the `0x80000000` allocator proxy ≠ SBUF window | runtime value, not in static bytes | MED / CARRIED |
| EXTISA `.text 0x01000000` is link-time VA; flat firmware boots at NX 0 | `ET_EXEC` + `.rela.got` + prelink path | MED / INFERRED |
| `[0x90000, 0x100000)` NX gap; PRID pre-masked to dense `[0..7]` | structural / `cpu_id<8` assert | INFERRED |

---

## 10. See also

- [LSP Linker Specs + ELF Layout](lsp-elf.md) — the full ldscript / specs decode
  and the build-flow this page's VADDRs come from.
- [The Device-Side Custom-Op ABI Reference](device-abi-reference.md) — the
  normative per-symbol contract; the final link assigns the VADDRs decoded here.
- [neuron_translate + the Window Table](neuron-translate-windows.md) — the
  consumer of the `0x07/09/0a000000` dynamic windows and the `0x80000000`/
  `0x84000000` pinned windows; the masked-tag compare and the MMIO refill.
- `control/address/lsp-sram-window-map.md` — the build-time SRAM-window view
  (Part 13, *not yet authored* — forward reference, no link until it exists).
