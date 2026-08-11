# The LSP SRAM Window Map (build-time view)

This page documents the **linker's build-time view** of the GPSIMD/Q7
(`ncore2gp`) memory map: the `MEMORY{}` segment table baked into the shipped
customop linker scripts, the per-core SRAM window banks, and a byte-exact decode
of the **`0x84000000` window base** and the **`0x200000` (2 MiB) per-core stride**.

It is the **build-time counterpart** of the runtime VADDR layout
([q7-elf-vaddr](../../abi/q7-elf-vaddr.md)) and the unified SoC view
([unified-soc-memory-map](unified-soc-memory-map.md)). The linker bakes the
window base that the SoC↔Q7 translation hardware later resolves
([soc-q7-translation-windows](soc-q7-translation-windows.md)); the toolchain
mechanics of these LSPs are covered in [lsp-elf](../../abi/lsp-elf.md).

Everything below is read directly from the **shipped GNU `ld` linker scripts**
(`elf32xtensa.{x,xn,xbn,xr,xu}`) and the toolchain's own reference `ncore2gp`
LSPs (`memmap.xmm` + `elf32xtensa.x` under `xtensa-elf/lib/…`). The scripts and
memory-map files are plain text emitted by the build; they are citeable
binary-derived artifacts. No disassembly is needed for the LSP itself.

> **Generation:** This is the **`ncore2gp` (Cayman-era)** LSP family,
> byte-grounded from the shipped customop package
> (`aws-neuronx-gpsimd-customop-lib_0.21.2.0`). The scripts are generated for one
> declared target (`/opt/workspace/SundaCustomOpLibrary`, line-1 comment of every
> script). Any v5/MAVERICK projection is flagged **INFERRED**; the
> `0x84000000`/`0x200000` numbers below are **byte-read** from the scripts, not
> assumed.

---

## 1. The customop LSP family — nine LSPs, five script variants each

The customop toolchain ships **nine** LSP directories under
`custom_op/lsp_fll_load_cpus/`:

```
lsp_fll_load_cpu0  lsp_fll_load_cpu1  …  lsp_fll_load_cpu7   (per-core, 8)
lsp_fll_load_cpu_single                                       (single-core, 1)
```

Each LSP directory contains a `specs` file and an `ldscripts/` directory holding
**five** `elf32xtensa.*` linker-script variants. GNU `ld` selects one variant by
the link mode requested on the command line. Verified from the line-2 comment of
each script (`lsp_fll_load_cpu0`):

| Variant | line-2 comment | `ld` mode | lines | `MEMORY{}`? | Purpose |
|---|---|---|---|---|---|
| `elf32xtensa.x`   | `Linker Script for default link` | default (final exe) | 366 | yes | **Default** — final placed image; the script all the bytes below come from. |
| `elf32xtensa.xn`  | `Linker Script for ld -n`        | `ld -n` (`NMAGIC`)  | 366 | yes | "No-RAM-area" / non-page-aligned magic. |
| `elf32xtensa.xbn` | `Linker Script for ld -N`        | `ld -N` (`OMAGIC`)  | 366 | yes | Writable-text / no-demand-paging magic. |
| `elf32xtensa.xr`  | `Linker Script for ld -r or ld -i` | `ld -r`/`-i` (relocatable) | 74 | **no** | **Relocatable** partial link — sections at `org 0`, no memory layout. |
| `elf32xtensa.xu`  | `Linker Script for ld -Ur`       | `ld -Ur` (reloc + ctors) | 74 | **no** | Relocatable link that **resolves constructors** (`-Ur`). |

> **GOTCHA — the `.x*` set is NOT five different memory maps.** Among the three
> *placing* variants (`.x`, `.xn`, `.xbn`) the **only** difference is the
> line-2 comment string — `diff elf32xtensa.x elf32xtensa.xn` and
> `diff elf32xtensa.x elf32xtensa.xbn` each report **exactly one changed line**
> (line 2). The `MEMORY{}` block, `PHDRS`, `SECTIONS`, cacheattr words and every
> address symbol are byte-identical across all three. The `-n`/`-N` distinction
> matters to the *output ELF flags* (NMAGIC/OMAGIC page alignment), not to where
> the linker places `sram0_0_seg`.

> **NOTE — `.xr`/`.xu` carry no addresses.** Both relocatable variants are 74-line
> scripts with **no `MEMORY{}` and no `PHDRS`**: `SECTIONS{}` lays `.text 0 :`,
> `.bss 0 :` etc. at offset `0`, leaving final addresses to a later full link.
> They exist so an object-merge (`ld -r`) of the customop sources can be produced
> without committing to the `0x84000000` window. The window base is decided only
> in the **default `.x`** placing link.

The `specs` file (1229 bytes, all 9 identical in role) wires the CRT start/end
objects for the final link:

```
*startfile:  crti%O%s crtbegin%O%s
*endfile:    crtend%O%s crtn%O%s
*lib:                                  # empty — no implicit -l libraries
```

---

## 2. The `MEMORY{}` segment table — the linker's two regions

The default link script `elf32xtensa.x` declares a **two-region** `MEMORY{}`
map (cpu0, verbatim lines 3-7):

```ld
MEMORY
{
  iram0_0_seg :   org = 0x00000000, len = 0x1000      /* 4 KiB low-NX IRAM     */
  sram0_0_seg :   org = 0x84000000, len = 0x200000    /* 2 MiB SRAM window bank */
}
```

`_memmap_region_map = 0x00000011` (line 52) — bits 0 and 4 set = the two live
regions. The complete segment table:

| segment | org | len | end | phdr(s) | role |
|---|---|---|---|---|---|
| `iram0_0_seg` | `0x00000000` | `0x1000` | `0x00001000` | `iram0_0_phdr` | XEA3 vectors/handlers (§5) |
| `sram0_0_seg` | `0x84000000` (`+ cpu*2 MiB`) | `0x200000` | `0x84200000` | `sram0_0_phdr` + `sram0_0_bss_phdr` | code / rodata / data / bss (§5) |

> *(`cpu_single`: `sram0_0_seg org=0x84000000 len=0x2000000`, end `0x86000000`.)*

### 2.1 The memory-boundary symbols

The script also exports the `ld`-standard boundary symbols (cpu0, lines 22-31):

```ld
_memmap_mem_iram0_start   = 0x0;          _memmap_mem_iram0_end   = 0x10000;   /* 64 KiB region */
_memmap_seg_iram0_0_start = 0x0;          _memmap_seg_iram0_0_max = 0x1000;    /* 4 KiB segment */
_memmap_mem_sram0_start   = 0x84000000;   _memmap_mem_sram0_end   = 0x84200000;/* per core      */
_memmap_seg_sram0_0_start = 0x84000000;   _memmap_seg_sram0_0_max = 0x84200000;/* per core      */
```

> **QUIRK — the IRAM *region* is 64 KiB but the *segment* uses only 4 KiB.**
> `_memmap_mem_iram0_end = 0x10000` (64 KiB) describes the on-core instruction-RAM
> *region*, but the linker `MEMORY{}` segment `iram0_0_seg` is capped at
> `len = 0x1000` (4 KiB) — only the first 4 KiB are used for the customop's XEA3
> vectors. The **SRAM** region and segment are equal (both
> `0x84000000..0x84200000`): there is no headroom region beyond the 2 MiB bank.

### 2.2 Reset / vector base

```ld
_rom_store_table = 0;
PROVIDE(_memmap_reset_vector  = 0x0);
PROVIDE(_memmap_vecbase_reset = 0x0);
```

Reset and vector base sit at NX `0x0`, inside the `iram0_0_seg` IRAM region
(lines 33-35).

> **CORRECTION-class NOTE — there is no third region.** A grep across **all nine**
> customop `.x` scripts finds **no `__stack`, no `_heap_sentry`, no `dram0`/
> `dram0_0_seg`** — only the metadata marker `.note.GNU-stack`. The customop LSP
> *deliberately* strips the stack and heap that the toolchain's reference LSPs
> carry (see §6): the runtime HBM stack and the xmem heaps are carved at run time,
> not by the linker. The map is exactly two regions.

---

## 3. The SRAM window / bank map — the per-core banks

The "SRAM window map" is the per-core `sram0_0_seg` bank table. All nine scripts'
`sram0_0_seg` origins, byte-exact:

| LSP | `sram0_0_seg` org | len | bank range `[start, end)` |
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

`iram0_0_seg` is `org = 0x0, len = 0x1000` — **identical for all nine**.

### 3.1 The stride and tiling — byte-exact arithmetic

The per-core bank origin is an exact linear function of the core index:

```c
/* Per-core SRAM bank base, as the LSP generator emits it.
 * WINDOW_BASE and CORE_STRIDE are read verbatim from the scripts:
 *   WINDOW_BASE = sram0_0_seg org of cpu0   = 0x84000000   (hbm_scratch window base)
 *   CORE_STRIDE = org(cpu_{i+1}) - org(cpu_i) = 0x00200000  (2 MiB, constant)
 */
#define SRAM_WINDOW_BASE  0x84000000u          /* NX base of the hbm_scratch 64-MiB pinned window */
#define SRAM_CORE_STRIDE  0x00200000u          /* 2 MiB per Q7 core                               */
#define SRAM_BANK_LEN     0x00200000u          /* per-core bank length (== stride: banks tile)    */

static inline uint32_t sram_bank_base(unsigned cpu_id /* 0..7 */) {
    return SRAM_WINDOW_BASE + cpu_id * SRAM_CORE_STRIDE;
}
/*  cpu0 -> 0x84000000 ; cpu7 -> 0x84000000 + 7*0x200000 = 0x84E00000  */
```

Worked verification (all eight confirmed):

```
org(cpu1) - org(cpu0) = 0x84200000 - 0x84000000 = 0x200000  (2 MiB)
org(cpu7)             = 0x84000000 + 7 * 0x200000 = 0x84E00000
end(cpu7)             = 0x84E00000 + 0x200000     = 0x85000000
```

The eight per-core banks **tile** `[0x84000000, 0x85000000)` = **16 MiB** with
**no gap and no overlap** — each bank's `end` equals the next bank's `org`.
`cpu_single` owns `[0x84000000, 0x86000000)` = **32 MiB** (16× the per-core bank,
and 2× the eight-core tiled extent).

### 3.2 The 2-MiB stride is the only material per-core change

A diff of `cpu0/.x` against `cpu1/.x` reports **exactly six changed lines** — the
path comment plus the five `0x84000000 → 0x84200000` address mirrors:

```
line  1 : /* … lsp_fll_load_cpu0 …    -> …cpu1 … */   (path comment)
line  6 : sram0_0_seg org              0x84000000 -> 0x84200000
line 24 : _memmap_mem_sram0_start      0x84000000 -> 0x84200000
line 25 : _memmap_mem_sram0_end        0x84200000 -> 0x84400000
line 30 : _memmap_seg_sram0_0_start    0x84000000 -> 0x84200000
line 31 : _memmap_seg_sram0_0_max      0x84200000 -> 0x84400000
```

The entire `SECTIONS{}` block, the `PHDRS`, the cacheattr words, `ENTRY`,
`region_map`, and the `iram0_0_seg` line are byte-identical across `cpu0..7`. The
`cpu0 → cpu_single` diff is **exactly four lines** (path comment + the SRAM
length `0x200000 → 0x2000000` + the two `*_end`/`*_max` symbols
`0x84200000 → 0x86000000`; the `*_start` symbols stay `0x84000000`). So the
build is **one object set, eight base-shifted link variants** — the
PRID-indexed 2-MiB stride is provably the only per-core difference. This is the
**link-time half** of the SPMD dual identity (same image, per-core base;
runtime half = the `rsr.prid` read).

### 3.3 What the bank holds

Apart from the 4-KiB IRAM vectors, the **whole customop image lives in the 2-MiB
bank**: `.text` (kernel + wrapper + runtime-lib code), the `.clib`/`.rtos` rodata
and text, `.rodata` (with the C++ exception tables and the `_bss_table` pointer
pair), `.eh_frame`, `.ctors`/`.dtors`, `.data` (`.sdata`, `.jcr`,
`__llvm_prf_*`), `.note.gnu.build-id`, and the **NOLOAD** `.bss` (the runtime
globals — `.sbss`/`.scommon`/`COMMON`/`.clib.bss`/`.rtos.bss`). The exact
section→region routing is given in §5. With a 64-KiB device-code cap, the 2-MiB
bank leaves ~1.94 MiB of headroom for `.bss`/scratch; the single-core 32-MiB bank
is 16× larger. `[HIGH/OBS section list; cited placement detail]`

---

## 4. The `0x84000000` / `0x200000` decode

The page brief framed `0x84000000` as a "per-window stride". The byte-exact
reading shows this **conflates a base with two strides**:

| value | meaning | source |
|---|---|---|
| `0x84000000` | **WINDOW BASE** — the NX-local base of the `hbm_scratch` pinned 64-MiB translation window | `sram0_0_seg` org (cpu0), `_memmap_mem_sram0_start` |
| `0x200000` (2 MiB) | **per-Q7-core SRAM-bank stride** — the eight banks tile 16 MiB | `org(cpu_{i+1}) − org(cpu_i)` |
| `0x4000000` (64 MiB) | inter-pinned-window stride (SBUF→`hbm_scratch`) — the *only* sense in which `0x84000000` is "one step" | `0x84000000 − 0x80000000` |

There is **no** sense in which SRAM banks are spaced by `0x84000000`: per-core
spacing is `0x200000`, and the entire eight-core set fits in 16 MiB (§2/§3).

`0x84000000` as a base is structurally pinned by four shipped, byte-exact facts.

### 4.1 It is one 64-MiB window above the SBUF window

```
SBUF pinned 64-MiB window NX base      = 0x80000000
hbm_scratch pinned 64-MiB window base  = 0x84000000
delta = 0x84000000 - 0x80000000        = 0x04000000 = 64 MiB = exactly ONE 64-MiB window
```

The 64-MiB window granule is `0x4000000` (offset bits `[25:0]`; mask
`0xfffffffffc000000`). So the two pinned 64-MiB windows are **adjacent**:
`0x80000000` (SBUF) then `0x84000000` (`hbm_scratch`). `[HIGH/OBS arithmetic;
window masks/bases carried from the translation-window analysis]`

### 4.2 It lies inside the core's hardware "cached0" upper-bus aperture

The `ncore2gp` core's **own** reference `ldapp` LSP declares a richer `MEMORY{}`
whose upper-bus half is, verbatim:

```ld
dvtext0_seg    : org = 0x4E000000, len = 0x2000000    /* 32 MiB                       */
cached0_seg    : org = 0x80000000, len = 0x20000000   /* 512 MiB — NAMED "cached"     */
noncached0_seg : org = 0xA0000000, len = 0x20000000   /* 512 MiB — NAMED "noncached"  */
```

and `ldapp/memmap.xmm` names them literally:

```
BEGIN cached
0x80000000: sysram : cached : 0x20000000 : executable, writable ;
 cached0 : C : 0x80000000 - 0x9fffffff : .cached.rodata .cached.literal .cached.text .cached.data;
BEGIN noncached
0xa0000000: sysram : noncached : 0x20000000 : executable, writable ;
 noncached0 : C : 0xa0000000 - 0xbfffffff : …
```

So the core's hardware bus map defines a **cached 512-MiB aperture**
`[0x80000000, 0xa0000000)` and a non-cached 512-MiB aperture
`[0xa0000000, 0xc0000000)`. The customop SRAM bank base `0x84000000` falls
**squarely inside the cached `cached0` aperture**.

### 4.3 The customop cacheattr makes that region (and only it) writeback

The Xtensa `cacheattr` word is **8 nibbles**, one per 512-MiB region of the 4-GiB
space (region `r = [r*0x20000000, (r+1)*0x20000000)`; nibble `1` = writeback-cached,
`4` = bypass/uncached). The customop's chosen reset word (cpu0, lines 40-41/53):

```ld
_memmap_cacheattr_unused_mask = 0xFFF0FFF0;   /* line 40 — only r0 and r4 are "used" */
_memmap_cacheattr_wb_trapnull = 0x44414444;   /* line 41                              */
PROVIDE(_memmap_cacheattr_reset = _memmap_cacheattr_wb_trapnull);   /* line 53 */
```

Decode of `0x44414444` (nibble `r7…r0`):

```
0x 4 4 4 1 4 4 4 4
   r7r6r5r4 r3r2r1r0
   r0 [0x00000000,0x20000000) = 4  -> bypass/uncached  (the IRAM vectors)
   r4 [0x80000000,0xA0000000) = 1  -> WRITEBACK-CACHED (the window aperture holding sram0_0_seg)
   all others                 = 4  -> bypass/uncached
```

`unused_mask = 0xFFF0FFF0` zeros the nibble positions of the *live* regions →
only **`r0` (IRAM, uncached)** and **`r4` (window aperture, cached)** are real.

Contrast the reference words:

| LSP | `cacheattr_wb_trapnull` | cached region(s) | `region_map` |
|---|---|---|---|
| customop (`cpu0`) | `0x44414444` | **r4** `[0x80000000,0xA0000000)` | `0x11` |
| `sim` | `0x44444111` | r0/r1/r2 (local-bus iram/dram/sram) | `0x07` |
| `ldapp` | `0x44114111` | r0, **r4**, r5 | `0x37` |

In every `ncore2gp` LSP the nibble for the region holding the program's data is
`1` (writeback). The customop simply **moves that cached region up to r4** (the
window aperture through which the SoC-translation windows are reached) and leaves
`r0` (vectors) uncached. Caching the `hbm_scratch` window is what makes per-core
code-fetch / data-access from the bank fast `[INFERRED — the cacheattr nibble is
OBSERVED; the performance reading is architectural]`.

### 4.4 The MPU background map confirms the 2-GiB split

`config/ncore2gp-params` MPU background map:

```
#VirtStartAddr   SizeInBytes    AccessRights   MemoryType
 0x00000000      0x80000000     0x00000007     0x00000006
 0x80000000      0x80000000     0x00000007     0x00000006
ISSSysRamBytes = 0x40000000        /* 1 GiB  */
ISSSysRamPAddr = 0x00100000        /* = the sim sram region base */
```

The 32-bit space is split into two **2-GiB halves at `0x80000000`**. Both pinned
windows (`0x80000000` SBUF, `0x84000000` `hbm_scratch`) **and** all eight per-core
SRAM banks (`0x84000000..0x85000000`) live in the **upper 2-GiB MPU half**. The
`ISSSysRam` base/size (`0x100000` / 1 GiB) match the reference local-bus `sram`
region the customop LSP **repoints** to `0x84000000`.

### 4.5 `0x84000000` carries no SoC-side stride meaning

The shipped SoC address maps **do** contain `84000000`-tailed entries (e.g.
`TOP_SP_6` base `0x0000008400000000`, `tpb_evt_sem 0x8400000000`), but those are
**40-bit SoC physical bases** whose hex *tail* merely coincides with the 32-bit NX
`0x84000000` — they are not the NX window base. The `hbm_scratch` window's real
SoC tag is a **runtime** value (`hbm_scratch & ~0x3ffffff`), programmed into a
64-MiB window register, not a static SoC stride. So `0x84000000` is purely an
**NX-local window base**. `[HIGH/OBS SoC-map grep; runtime-tag fact carried from
the translation-window analysis]`

**Decode summary:**

```
0x84000000 = NX base of the hbm_scratch 64-MiB pinned window
           = SBUF_window_base(0x80000000) + one 64-MiB window(0x4000000)
           = inside the core's hardware "cached0" 512-MiB aperture (cacheattr r4)
           = the single writeback-cached cacheattr region of the customop LSP
           = in the upper 2-GiB MPU half.
0x200000   = the per-Q7-core SRAM-bank stride (8 banks tile 16 MiB).
0x4000000  = the SBUF -> hbm_scratch 64-MiB inter-window stride.
```

---

## 5. SECTIONS → region mapping

The default `.x` `SECTIONS{}` routes every output section to one of the two
regions via `>region :phdr` clauses. The two regions, their role, and the section
classes each holds:

| LSP region | NX range | sections placed (verbatim `>region`) | SoC / hw backing |
|---|---|---|---|
| `iram0_0_seg` (4 KiB) | `[0x0, 0x1000)` | `.DispatchVector.text`, `.ResetVector.text`, `.ResetHandler.text`, `.DispatchHandler.text` (all `>iram0_0_seg :iram0_0_phdr`) | on-core IRAM (low-NX i-fetch aperture); reset/vecbase `@0x0`; **uncached** (cacheattr `r0=4`). Vectors are loader/CRT-supplied, not in the customop `.a`. |
| `sram0_0_seg` (2 MiB / core) | `[0x84000000 + i*2 MiB, +2 MiB)` | **LOAD** (`:sram0_0_phdr`): `.text`, `.clib.rodata`, `.rtos.rodata`, `.clib.data`, `.eh_frame`, `.ctors`, `.dtors`, `.rodata` (+ exc tables + `_bss_table`), `.clib.text`, `.rtos.text`, `.clib.percpu.data`, `.rtos.percpu.data`, `.rtos.data`, `.interp`, `.data`, `.note.gnu.build-id`. **NOLOAD** (`:sram0_0_bss_phdr`): `.bss`. | the `hbm_scratch` pinned 64-MiB window; its low quarter (16 MiB for 8 cores) holds the per-core code/data — HBM-resident scratch reached through the pinned window as a *hit* (no dynamic-TLB eviction); **cached writeback** (cacheattr `r4=1`). |

Three `PHDRS` are declared — `iram0_0_phdr`, `sram0_0_phdr`,
`sram0_0_bss_phdr` (all `PT_LOAD`) — so the loadable image carries the IRAM
vectors, the SRAM code/data, and a separate program header for the NOLOAD `.bss`
(the `.bss` occupies bank address space but ships no bytes; the CRT zeroes it via
the `_bss_table` pointer pair emitted into `.rodata`).

Key role distinctions:

- **The `sram0_0_seg` is NOT the on-chip `STATE_BUF` SRAM.** `STATE_BUF` is a pure
  on-chip SRAM at a distinct SoC base; the customop's `sram0_0` is the
  HBM-resident `hbm_scratch` heap reached *through* the `0x84000000` window — a
  different physical store, despite the linker's generic `sram0` label (Tensilica's
  generic name for "the data/code RAM region", here re-pointed at the HBM-scratch
  NX window).
- **The DATARAM is not in the LSP.** The per-core on-core dataram
  (`[0x80000,0x90000)`; the reference `sim` LSP's `dram0_0_seg @ 0x80000`) is
  omitted; the runtime reaches it via a direct NX deref, never a linker `dram0`
  region (no `dram0` appears in any customop `.x`).
- **The IRAM region holds only the customop's XEA3 vectors** (4 KiB); it is
  distinct from the SEQ/Q7 **engine firmware** image (`.text @ 0x01000000`) — two
  different programs in the Q7 NX space at different times.
- **No system-visible/shared-window region exists in the LSP.** SBUF
  (`0x80000000`), PSUM, EVT_SEM, and general HBM tensor data are reached at
  **runtime** via the pinned/dynamic translation windows, not placed by the
  linker. The LSP is a pure per-core code/const/bss placement script — it declares
  no `0x80000000` and no `0x07/09/0a000000` origins.

---

## 6. Build-time `MEMORY{}` vs the runtime VADDR view

This page is the build-time map; [q7-elf-vaddr](../../abi/q7-elf-vaddr.md) gives
the runtime Q7 NX VADDR model. The two are the **same numbers where they overlap**
and **diverge only on regions the linker does not place** (which the runtime page
covers as runtime constructs). Per-region:

| element | linker (this page) | runtime VADDR | relation |
|---|---|---|---|
| `iram0_0_seg` vectors | `[0x0, 0x1000)` 4 KiB | low-NX IRAM, customop uses first 4 KiB | **MATCH** |
| `sram0_0_seg` base | `0x84000000` | `hbm_scratch` pinned 64-MiB window NX base | **MATCH** (exact base) |
| per-core bank table | `0x84000000 + cpu*0x200000` | identical all-9 table | **MATCH** (byte-exact) |
| section→bank routing | §5 | same section→VADDR map | **MATCH** |
| `cpu_single` bank | `0x84000000` / 32 MiB | `0x84000000` / 32 MiB | **MATCH** |
| cacheattr `r4`-cached | decoded §4.3 | (runtime view does not decode cacheattr) | **EXTENDS** — this page adds it |
| stack | **not in LSP** | runtime HBM stack (1 dyn 16-MiB window) | **CONSISTENT** (runtime-carved) |
| heaps | **not in LSP** | runtime xmem heaps (DSM / `hbm_scratch`) | **CONSISTENT** (runtime) |
| dataram `[0x80000,0x90000)` | **not in LSP** | per-core dataram, direct NX deref | **CONSISTENT** (runtime DSM base) |
| CSR block `0x100000` | **not in LSP** | MMIO CSR block | **CONSISTENT** (MMIO) |
| dyn windows + SBUF `0x80000000` | **not in LSP** | runtime TLB / pinned window | **CONSISTENT** (runtime) |
| firmware `.text @ 0x01000000` | **not the customop LSP** | EXTISA firmware image | **DISTINCT PROGRAM** |

**Reconciliation verdict:** perfect match on every region the linker *places*
(`iram0_0_seg`, `sram0_0_seg`); consistent — not divergent — on every region the
linker does *not* place (stack, heaps, dataram, CSR, windows: runtime constructs);
this build-time page **extends** the runtime view with the cacheattr `r4`-cached
decode; and the firmware `0x01000000` image is a legitimately separate program
from the customop `.so` at `0x84000000`. The linker bakes the `0x84000000` window
base; the SoC↔Q7 translation hardware
([soc-q7-translation-windows](soc-q7-translation-windows.md)) resolves it to the
runtime `hbm_scratch` SoC tag at execution time.

---

## 7. Per-generation / per-core / per-cluster applicability

- **Per-generation `[HIGH/OBS decl path; MED gen-invariance]`** — all nine scripts
  are generated for one declared target,
  `/opt/workspace/SundaCustomOpLibrary/…` (line-1 comment). There is exactly
  **one LSP family**; `fd` over the package shows only `lsp_fll_load_cpu*` — no
  per-gen `cayman`/`mariana`/`mariana_plus` LSP directories. The two-region map is
  therefore the shared layout across the gens this customop path serves. Per-gen
  memory *deltas* that do exist (e.g. POOL NX-DRAM 64→128 KiB cayman→mariana;
  HBM-stack arena cap) are **runtime/firmware-image** facts, not customop-LSP
  facts. `[HIGH — single LSP family OBSERVED; gen-identity MED, only one family
  ships]`
- **Per-core** — the only per-core difference is the `sram0_0_seg`
  bank base (2-MiB stride; §3.2). `iram0_0_seg`, `region_map`, cacheattr, `PHDRS`,
  `ENTRY`, and the whole `SECTIONS{}` block are byte-identical `cpu0..7`;
  `cpu_single` differs only in the bank *length*.
- **Per-cluster `[HIGH/OBS + INFERRED]`** — the customop ABI targets the **8-core
  TPB-POOL** cluster only (`get_cpu_count()==8`), hence 8 per-core LSPs + single.
  The Q7 NX-local bank bases are identical across PREPROC and POOL cores; the
  per-cluster difference is the **SoC-physical cluster base** (PREPROC vs POOL),
  not the NX bank.
  > **GOTCHA — the SoC slot pitch ≠ the NX bank stride.** The SoC per-core slot
  > pitch is **1 MiB** (`0x100000`) but the NX customop bank stride is **2 MiB**
  > (`0x200000`) — two **unrelated** strides (SoC-physical core spacing vs the
  > NX link-bank partition of the `hbm_scratch` window). Do not conflate them.
  > `[HIGH/OBS the two values; PREPROC NX-bank reuse INFERRED — the customop never
  > links for PREPROC]`

---

## 8. Reimplementation checklist

To rebuild a Vision-Q7-compatible customop link for one POOL core:

1. **Two regions only.** `MEMORY{ iram0_0_seg : org=0x0, len=0x1000;
   sram0_0_seg : org=0x84000000 + cpu_id*0x200000, len=0x200000; }`. No stack, no
   heap, no `dram0`.
2. **`cpu_single`** variant: same base, `len=0x2000000` (32 MiB).
3. **Three `PHDRS`** (`iram0_0_phdr`, `sram0_0_phdr`, `sram0_0_bss_phdr`, all
   `PT_LOAD`); route the four XEA3 vector sections to IRAM, everything else to the
   SRAM bank, `.bss` (NOLOAD) to the bss phdr.
4. **cacheattr `0x44414444`** (`r4` writeback-cached, `r0` bypass),
   `unused_mask = 0xFFF0FFF0`, `region_map = 0x11`.
5. **Reset/vecbase at `0x0`**; emit the `_bss_table` LONG pair into `.rodata` for
   the CRT to zero `.bss`.
6. **`specs`:** `crti crtbegin` startfile, `crtend crtn` endfile, empty `*lib`.
7. Emit `.xn`/`.xbn` as byte-copies of `.x` differing only in the magic comment;
   emit relocatable `.xr`/`.xu` with sections at `org 0` and **no** `MEMORY{}`.

The hardware-side counterpart (the TCAM/window register that translates
`0x84000000` to the runtime `hbm_scratch` SoC tag) is documented in
[soc-q7-translation-windows](soc-q7-translation-windows.md); the runtime VADDR
consumer is [q7-elf-vaddr](../../abi/q7-elf-vaddr.md); the unified cross-view is
[unified-soc-memory-map](unified-soc-memory-map.md).

---

## Confidence ledger

- **HIGH / OBSERVED** — the `MEMORY{}` block (`iram0_0_seg 0x0/0x1000` identical
  all 9; `sram0_0_seg 0x84000000 + i*0x200000 / 0x200000` cpu0..7,
  `0x84000000/0x2000000` single); the per-core bank table + 2-MiB stride + 16-MiB
  tiling; the 6-line `cpu0→cpu1` and 4-line `cpu0→cpu_single` diffs; `region_map
  0x11`; the five `.x*` variant set (3×366-line placing, 2×74-line relocatable,
  differing among `.x/.xn/.xbn` only at line 2); the cacheattr decode
  (`0x44414444`, `unused_mask 0xFFF0FFF0`, `r4` cached / `r0` uncached); the
  reference `ldapp` `cached0_seg 0x80000000/0x20000000` + `memmap.xmm "BEGIN
  cached"`; the `sim` cacheattr `0x44444111` + `__stack`/`_heap_sentry
  0x40100000`; the MPU bg-map 2-GiB split + `ISSSysRam 0x100000/0x40000000`; the
  absence of any `__stack`/`_heap_sentry`/`dram0` in all 9 customop scripts.
- **MED** — `sram0_0_seg @ 0x84000000` IS the `hbm_scratch` record-4 pinned-window
  NX base (grounded in the translation-window analysis); gen-invariance (only one
  LSP family ships); the specific `.bss` runtime-globals routing (cited placement).
- **INFERRED** — PREPROC cores reuse the same NX bank map (the customop never
  links for PREPROC); the window aperture being cached makes per-core access fast
  (architectural reading of the `r4=1` nibble); the vector region being uncached
  is the loader/CRT choice for the fixed vector aperture.
- **LOW / NOTED** — the customop LSP ships only the generated `ldscripts` + `specs`;
  its own `memmap.xmm` *source* is not in the package (the `.xmm` cross-checked
  here is the toolchain's reference one). No v5/MAVERICK LSP is shipped; any
  forward projection would be INFERRED.
