# LSP Linker Specs + ELF Layout

This page is the **linker's view of where the custom-op image lands** in the Q7 NX
32-bit address space, per core. It decodes the shipped **Linker Support Packages**
(LSPs) — the Xtensa linker scripts and the gcc specs file that pin every output
section of the custom-op `.so` to an absolute VADDR. The companion
[Build → Compile → Link → Strip → Package Flow](build-flow.md) owns *what commands run*
and hands the linked relocatable blob to the on-device prelinker; this page owns *what
addresses those commands resolve to*. The
[`build_custom_op.py` Codegen](build-custom-op-codegen.md) page owns the `-mlsp`
selection; here we open the directory it selects and read the script inside. The
runtime counterpart — the same numbers seen from the executing engine — is
[The Q7 ELF VADDR + Per-Core Memory Model](q7-elf-vaddr.md).

Everything below is anchored to shipped artifacts read **verbatim as text** — the
ldscripts and the specs file are plain text and are citeable directly:

| Artifact | Path | Role |
|---|---|---|
| Per-core LSPs | `…/gpsimd/custom_op/lsp_fll_load_cpus/lsp_fll_load_cpu{0..7,_single}/` | 9 LSP dirs |
| Default ldscript | `…/lsp_fll_load_cpu{i}/ldscripts/elf32xtensa.x` | full-link script (pins addresses) |
| ld-mode variants | `…/ldscripts/elf32xtensa.{xn,xbn,xr,xu}` | `-n` / `-N` / `-r`/`-i` / `-Ur` scripts |
| gcc specs | `…/lsp_fll_load_cpu{i}/specs` | CRT object order + blanked default libs |
| Runtime archive | `…/custom_op/neuron/libneuroncustomop.a` | 10-member ELF32-Xtensa REL archive |
| Reference LSPs | `…/ncore2gp/xtensa-elf/lib/{sim,ldapp}/…` | toolchain's own ncore2gp LSPs (contrast) |
| Core params | `…/XtensaTools/config/ncore2gp-params` | MPU background map + `ISSSysRam*` |
| FLL loader demo | `…/XtensaTools/xtensa-elf/src/libloader/examples/fll_demo/fll_demo.c` | "FLL" = Fixed Location Library |

Claims are tagged `[HIGH/OBSERVED]` (verbatim ldscript/specs/ELF bytes + byte-exact
diff/hash), `[MED/INFERRED]` (reasoning over those bytes), or `[CARRIED]` (grounded in
a cross-referenced lane). Source report: **ABI-15**. Package version: customop-lib
**0.21.2.0**.

---

## 0. The LSP in one paragraph

The custom-op toolchain ships **nine** LSPs:
`lsp_fll_load_cpu0 … lsp_fll_load_cpu7` (the eight per-core SPMD variants) plus
`lsp_fll_load_cpu_single` (the single-core fallback). Each directory holds five
generated Xtensa ldscripts `elf32xtensa.{x,xn,xbn,xr,xu}` and one gcc `specs` file.
The default link script `.x` declares a deliberately minimal **two-region** memory
map: `iram0_0_seg` (org `0x0`, len `0x1000` = 4 KiB) and `sram0_0_seg`
(org `0x84000000 + cpu_id*0x200000`, len `0x200000` = 2 MiB). It places **only** the
four XEA3 vector/handler sections into IRAM, and **everything else** — `.text`,
`.rodata`, `.data`, ctors/dtors, eh_frame, the clib/rtos sections, **and** `.bss` —
into the per-core 2 MiB `sram0_0_seg`, which is a private sub-window of the
`0x84000000` `hbm_scratch` pinned translation window. The **only** per-core difference
across cpu0..7 is the `sram0_0_seg` origin (a perfect 2 MiB stride, 16 MiB tiling);
`cpu_single` is the same script with a 32 MiB window. The LSP reserves **no stack
segment and no heap segment** — those are runtime objects. `[HIGH/OBSERVED]`

> **NOTE — "FLL" = Fixed Location Library.** The directory name `lsp_fll_load` is not
> a placeholder. The toolchain's own `fll_demo.c` is headed *"example program to show
> how to load/use fixed location"* and loads a `xtlib_packaged_library` via
> `xtlib_load_overlay_ext(my_fll_ptr, &info)` at a **fixed** address, then calls its
> `_start`. The custom-op `.so` is exactly this: compiled `-fpic` but **linked against
> a fixed-address LSP** (`-mlsp`) and packed with `xt-pkg-loadlib`, producing a
> packaged library the FLL loader (`-lloader`) drops at the per-core fixed `sram`
> address. The absolute LSP origins (no PIC layout) *are* the "fixed location."
> `[HIGH/OBSERVED fll_demo.c + build recipe]`

---

## 1. The LSP file inventory

Nine LSP directories ship under
`…/gpsimd/custom_op/lsp_fll_load_cpus/`. Every directory carries an identical file
set (only the address-bearing bytes differ): `[HIGH/OBSERVED — ls + fd]`

| File | cpu0 size | Role |
|---|---|---|
| `ldscripts/elf32xtensa.x` | 9255 B / 366 lines | **default** full-link script — pins the per-core addresses |
| `ldscripts/elf32xtensa.xn` | 9248 B / 366 lines | `ld -n` (NMAGIC) variant |
| `ldscripts/elf32xtensa.xbn` | 9248 B / 366 lines | `ld -N` (OMAGIC) variant |
| `ldscripts/elf32xtensa.xr` | 1617 B / 74 lines | `ld -r` / `-i` relocatable (partial-link) script |
| `ldscripts/elf32xtensa.xu` | 1609 B / 74 lines | `ld -Ur` relocatable-with-ctors script |
| `specs` | 1229 B / 30 lines | gcc/xt-clang spec overrides |

Every script's line-1 comment names its generator verbatim:

```
/* This linker script generated from xt-genldscripts.tpp for LSP /opt/workspace/
   SundaCustomOpLibrary/custom_op/lsp_fll_load_cpus/lsp_fll_load_cpu0 */
```

So the scripts are **machine-generated** by Tensilica's `xt-genldscripts.tpp` template
from a per-LSP memory map (`.xmm`). The decl path `/opt/workspace/SundaCustomOpLibrary`
matches the DWARF decl path in the static lib (same build tree). The customop package
ships **only** the generated ldscripts + specs, not its own source `.xmm`.
`[HIGH/OBSERVED verbatim comment]`

**How the build selects them.** `build_custom_op.py` passes
`-mlsp=…/lsp_fll_load_cpus/lsp_fll_load_cpu{i}` (or `…_single`) to `xt-clang++` at link;
`-mlsp` points at the **directory**, and the driver picks the right ldscript variant by
link mode. The default full-executable link uses `elf32xtensa.x`. `[HIGH/OBSERVED —
build_custom_op.py MULTI_Q7_LIBS / LIBS_SINGLE]`

---

## 2. The five ldscript variants

The five `elf32xtensa.*` are the standard GNU/Xtensa ld script family, one per link
mode. Decoded by reading + diffing cpu0: `[HIGH/OBSERVED]`

| Variant | ld mode | What it is |
|---|---|---|
| `.x` | default | the full executable/loadable-library script: `MEMORY` + `PHDRS` + `ENTRY` + the full `_memmap_*` set + the `SECTIONS` placement. **This is the script that pins the per-core addresses.** |
| `.xn` | `ld -n` (NMAGIC) | **identical to `.x` except line-2 comment** (`"Linker Script for ld -n"`). Same layout — the target is fixed in-memory, no demand paging. |
| `.xbn` | `ld -N` (OMAGIC) | **identical to `.x` except line-2 comment** (`"Linker Script for ld -N"`). Same layout. |
| `.xr` | `ld -r` / `-i` | the **relocatable** (partial-link) script: **no `MEMORY`, no origins, no `_memmap_*`** — just `SECTIONS` concatenating `.text`/`.bss`/`.debug.*` at addr 0. Carries **zero** per-core/window information. |
| `.xu` | `ld -Ur` | **identical to `.xr` except line-2 comment** (`"Linker Script for ld -Ur"`). |

**Verification (byte-exact):** `[HIGH/OBSERVED]`

- `diff .x .xn` and `diff .x .xbn` (cpu0) = **line 2 only** (the mode comment).
- The `.xn` / `.xbn` carry the **same** per-core `sram0_0_seg` org as `.x`.
- `diff .xr .xu` = line 2 only; the `.xr` is byte-identical across cpu0..7 + single
  (excluding the line-1 path comment).

> **GOTCHA — the per-core identity lives in only three scripts.** The entire per-core /
> per-window difference is in the **address-bearing** scripts (`.x`/`.xn`/`.xbn`). The
> two relocatable scripts (`.xr`/`.xu`) are address-independent. For the custom-op
> default full link (`build_custom_op.py` emits a `.so`), the operative script is
> `elf32xtensa.x`. `[HIGH/OBSERVED]`

---

## 3. The `MEMORY{}` segment table — the two regions

The `.x` `MEMORY` block declares **exactly two** regions (verbatim, cpu0,
lines 3–7): `[HIGH/OBSERVED]`

```
MEMORY
{
  iram0_0_seg :   org = 0x00000000, len = 0x1000      ; 4 KiB low-NX IRAM
  sram0_0_seg :   org = 0x84000000, len = 0x200000    ; 2 MiB SRAM window bank
}
```

The complete segment table, with its boundary symbols (cpu0, lines 22–31):

| segment | org | len | end | phdr(s) | role |
|---|---|---|---|---|---|
| `iram0_0_seg` | `0x00000000` | `0x1000` | `0x00001000` | `iram0_0_phdr` | XEA3 vectors (§5) |
| `sram0_0_seg` | `0x84000000` (+ cpu·2 MiB) | `0x200000` | `0x84200000` | `sram0_0_phdr` + `sram0_0_bss_phdr` | code/rodata/data/bss (§5) |

```
_memmap_mem_iram0_start  = 0x0          _memmap_mem_iram0_end  = 0x10000   ; region = 64 KiB
_memmap_seg_iram0_0_start = 0x0         _memmap_seg_iram0_0_max = 0x1000    ; segment = 4 KiB
_memmap_mem_sram0_start  = 0x84000000   _memmap_mem_sram0_end  = 0x84200000 ; per core
_memmap_seg_sram0_0_start = 0x84000000  _memmap_seg_sram0_0_max = 0x84200000 ; per core
_memmap_region_map = 0x00000011         ; bits 0,4 set -> the two regions
```

> **NOTE — IRAM region (64 KiB) vs IRAM segment (4 KiB).** The IRAM *region* spans
> 64 KiB (`_memmap_mem_iram0_end = 0x10000`), but the linker *segment* uses only the
> first 4 KiB (`_memmap_seg_iram0_0_max = 0x1000`) — ample for a handful of vector
> stubs. The `sram` region == the `sram` segment (both `0x84000000..0x84200000`):
> there is no headroom region beyond the bank. `[HIGH/OBSERVED both boundary pairs]`

Reset / vector base (lines 33–35) sit at NX `0x0` (in IRAM):

```
_rom_store_table = 0;
PROVIDE(_memmap_reset_vector  = 0x0);
PROVIDE(_memmap_vecbase_reset = 0x0);
```

> **NOTE — there is no third region.** No `dram0` (the per-core dataram
> `[0x80000,0x90000)`), no stack region, no heap region. `region_map = 0x11`
> (two regions). The reconciliation with the runtime stack/heap design is §7.
> `[HIGH/OBSERVED — rg over the MEMORY block of all 9 = exactly these two segs]`

---

## 4. The per-core SRAM window bank map — the `0x84000000` / `0x200000` decode

The "SRAM window map" is a **single-bank-per-core** map: one 2 MiB bank per Q7 core.
Grepped + diffed + body-hashed byte-exact (all 9 LSPs): `[HIGH/OBSERVED]`

| LSP | `sram0_0_seg` org | len | bank range `[start,end)` |
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

`iram0_0_seg` = org `0x0`, len `0x1000` — **verified identical for all 9.**

```
org(cpu_i) = 0x84000000 + i*0x200000        // EXACT 2 MiB stride, all 8 confirmed
```

The 8 banks **tile** `[0x84000000, 0x85000000)` = 16 MiB with **no gap, no overlap**;
`cpu_single` owns `[0x84000000, 0x86000000)` = 32 MiB (16× the per-core bank).
`[HIGH/OBSERVED — python-verified tiling]`

### What the `0x84000000` base encodes

A "`0x84000000` per-window stride" framing conflates **base** with **stride**.
Byte-exact: `[HIGH/OBSERVED]`

```
0x84000000  = NX-local BASE of the hbm_scratch pinned 64-MiB translation window
0x200000    = per-Q7-core SRAM-bank STRIDE   (8 banks tile 16 MiB)
0x4000000   = the SBUF -> hbm_scratch 64-MiB INTER-window stride (the only true
              "0x84000000-class" step)
```

There is **no** sense in which the sram banks are spaced by `0x84000000`: the per-core
spacing is `0x200000`, and the whole 8-core set fits in 16 MiB. The `0x84000000` base
is structurally pinned by four shipped facts:

**(a) one 64-MiB window above SBUF.** `[HIGH/OBSERVED arithmetic]`
SBUF pinned-window NX base = `0x80000000`; `hbm_scratch` = `0x84000000`; delta =
`0x4000000` = 64 MiB = exactly one 64-MiB window (granule mask
`0xfffffffffc000000`, offset bits `[25:0]`). The two pinned 64-MiB windows are
**adjacent**: `0x80000000` (SBUF) then `0x84000000` (hbm_scratch).

**(b) inside the core's hardware "cached0" aperture.** `[HIGH/OBSERVED — ldapp LSP +
memmap.xmm]` The ncore2gp core's own reference `ldapp` LSP declares an upper-bus
region table whose cached half is:

```
dvtext0_seg    : org 0x4E000000, len 0x2000000    (32 MiB)
cached0_seg    : org 0x80000000, len 0x20000000   (512 MiB)   <- NAMED "cached"
noncached0_seg : org 0xA0000000, len 0x20000000   (512 MiB)   <- NAMED "noncached"
```

and `ldapp/memmap.xmm` names them literally:
`0x80000000: sysram : cached : 0x20000000 : executable, writable`. The custom-op
`sram` bank base `0x84000000` falls **squarely inside** the cached 512-MiB aperture
`[0x80000000, 0xA0000000)`.

**(c) the cacheattr marks that region — and only it — writeback.** `[HIGH/OBSERVED —
decoded from the ldscript]` The Xtensa cacheattr word is **8 nibbles, one per 512-MiB region**
of the 4 GiB space (region `r = [r·0x20000000, (r+1)·0x20000000)`). The custom-op `.x`
ships (lines 37–53):

```
_memmap_cacheattr_wb_base      = 0x00010004
_memmap_cacheattr_unused_mask  = 0xFFF0FFF0
_memmap_cacheattr_wb_trapnull  = 0x44414444
PROVIDE(_memmap_cacheattr_reset = _memmap_cacheattr_wb_trapnull);
```

Decoded (nibbles r0..r7, low → high):

| word | r0 | r1 | r2 | r3 | r4 | r5 | r6 | r7 | meaning |
|---|---|---|---|---|---|---|---|---|---|
| `0x44414444` (reset) | 4 | 4 | 4 | 4 | **1** | 4 | 4 | 4 | **only r4 writeback-cached; r0 (vectors) uncached** |
| `0xFFF0FFF0` (unused) | 0 | f | f | f | **0** | f | f | f | **only r0, r4 are live** (0-nibbles) |

Nibble `1` = writeback-cached; `4` = bypass/uncached. So region 4
(`[0x80000000, 0xA0000000)` — holding `sram0_0_seg`) is the **single writeback-cached
aperture**, and region 0 (the IRAM vectors) is uncached. The `unused_mask` `0xFFF0FFF0`
confirms the linker knows exactly two live regions: r0 (IRAM) + r4 (the window
aperture). Caching the `hbm_scratch` window is what makes per-core code-fetch and
data-access fast through the pinned window. `[MED/INFERRED the perf rationale; HIGH the
nibble decode]`

> **NOTE — every ncore2gp LSP caches the region holding its data; the customop just
> moves it up to r4.** Contrast the reference `sim` LSP: `cacheattr_reset = 0x44444111`
> (r0,r1,r2 = `1` = the local-bus iram `0x0` / dram `0x80000` / sram `0x100000`
> cached), and `ldapp`: `0x44114111` (r0,r1,r2,r4,r5 = `1`). The custom-op moves the
> cached region **up** to r4 (the window aperture) and leaves r0 (vectors) uncached.
> `[HIGH/OBSERVED all three cacheattr words]`

**(d) the MPU background map confirms the 2-GiB split.** `[HIGH/OBSERVED —
ncore2gp-params]` The `ncore2gp-params` MPU background map (2 entries):

```
#VirtStartAddr   SizeInBytes    AccessRights   MemoryType
0x00000000       0x80000000     0x00000007     0x00000006
0x80000000       0x80000000     0x00000007     0x00000006
```

The 32-bit space splits into two 2-GiB halves at `0x80000000`. Both pinned windows
(`0x80000000` SBUF, `0x84000000` hbm_scratch) and all 8 per-core sram banks live in the
**upper** 2-GiB MPU half. `ISSSysRamBytes = 0x40000000` (1 GiB) /
`ISSSysRamPAddr = 0x00100000` match the local-bus `sram` region the custom-op LSP
repoints up to `0x84000000`.

> **GOTCHA — NX `0x84000000` carries no SoC-side stride.** The shipped SoC maps do
> contain `84000000`-tailed entries (e.g. `TOP_SP_6` base `0x0000008400000000`), but
> those are 40-bit SoC physical bases whose hex tail merely **coincides** with the
> 32-bit NX `0x84000000`. The `hbm_scratch` window's real SoC tag is a **runtime**
> value programmed into a 64-MiB window register, not a static SoC stride. So
> `0x84000000` is purely an NX-local window base. `[HIGH/OBSERVED SoC-map grep; the
> runtime-tag fact CARRIED from the window lane]`

---

## 5. The `SECTIONS` → region layout — where each section lands

The `.x` `SECTIONS` block assigns each output section to one of the two regions via
`>region :phdr`. Content is identical across all 9 LSPs; only the `sram` base shifts
per core.

```
PHDRS { iram0_0_phdr PT_LOAD; sram0_0_phdr PT_LOAD; sram0_0_bss_phdr PT_LOAD; }
ENTRY(_start)
```

### IRAM (`iram0_0_seg` @ `0x0`, 4 KiB) — exactly four sections

```
.DispatchVector.text   (KEEP)             the XEA3 interrupt-dispatch vector
.ResetVector.text                         the reset vector
.ResetHandler.text     (.literal + .text) the reset handler
.DispatchHandler.text  (.literal + .text) the XEA3 dispatch handler
  -> _memmap_seg_iram0_0_end = ALIGN(0x8); _itb_default = 0x0;
     _memmap_mem_iram0_max = ABSOLUTE(.);
```

These are the **XEA3** (Xtensa Exception Architecture 3) vector/handler stubs of the
loaded custom-op library. `[HIGH/OBSERVED iram section list]`

> **NOTE — the vectors are loader/CRT-supplied, not from `libneuroncustomop.a`.** No
> custom-op object defines a `.DispatchVector`/`.ResetVector`/`.ResetHandler`/
> `.DispatchHandler` section: `readelf -S` on `start_exit.o` shows only `.text` /
> `.data` / `.bss`. The vectors are supplied by the toolchain CRT / FLL loader
> (`-lloader`, `crtbegin`), not by the customop archive. `[HIGH/OBSERVED — section
> absence in the archive members]`

### SRAM (`sram0_0_seg` @ `0x84000000 + i·2 MiB`) — everything else

**LOAD (`sram0_0_phdr`):** `[HIGH/OBSERVED — every >sram0_0_seg assignment]`

```
.text            (_stext/_text_start..; .entry.text, .init, .literal/.text/.literal.*/
                  .text.*, .fini; _etext/_text_end)                      <- THE CODE
.clib.rodata   .rtos.rodata   .clib.data
.eh_frame      .ctors         .dtors
.rodata          (incl. __XT_EXCEPTION_TABLE__/.xt_except_table, the C++ ctor/dtor
                  tables, the .xt_except_desc exception descriptors, AND the
                  _bss_table_start / LONG(_bss_start) / LONG(_bss_end) / _bss_table_end
                  pair the CRT reads to zero .bss)
.clib.text     .rtos.text
.clib.percpu.data  .rtos.percpu.data  .rtos.data
.interp        .data            (incl. .sdata/.sdata2/.jcr/__llvm_prf_*)
.note.gnu.build-id
```

**NOLOAD (`sram0_0_bss_phdr`):**

```
.bss (NOLOAD), ALIGN(8): .dynsbss/.sbss*/.scommon/.dynbss/.bss/COMMON/.clib.bss/
                         .clib.percpu.bss/.rtos.percpu.bss/.rtos.bss;
                         _bss_start/_bss_end;
                         _memmap_seg_sram0_0_end = ALIGN(8);
-> _memmap_mem_sram0_max = ABSOLUTE(.);
```

So **all** custom-op code (`.text`), read-only data (`.rodata` + clib/rtos rodata),
initialised data (`.data` + clib/rtos + percpu data), the C++ ctor/dtor and exception
tables, **and** the zero-init `.bss` are placed in the per-core `hbm_scratch`
sub-window. The `.bss` is **NOLOAD (NOBITS)** — it occupies address space in the window
but ships no bytes; the loader/CRT zeroes it via the `_bss_table` pair.
`[HIGH/OBSERVED]`

### Non-alloc (org 0)

The `.debug.*` / `.xt.insn` / `.xt.prop` / `.xt.lit` / `.xtensa.info` /
`.debug.xt.callgraph` / `.comment` / `.note.GNU-stack` tail sections are at addr 0
(debug/metadata, not loaded). `[HIGH/OBSERVED]`

### What the per-core bank holds

Per core, `sram0_0_seg` holds the whole custom-op image except the 4 KiB IRAM vectors:
`.text` (kernel + wrapper + runtime-lib code), `.rodata` (+ ctor/dtor + exception
tables + `_bss_table` pair + version strings), `.data`, and (NOLOAD) `.bss` — the
runtime globals (`_ctx`, `_sbuf_window`, the three xmem heap managers' bookkeeping, the
dma ctx, `stack_switch`'s switched/old/new stack pointers, `parallel.o`'s `cpu_id`).
Each core's writable globals therefore live at a per-core-distinct NX address — the
**link-time half** of the SPMD dual identity (the run-time half is the `rsr.prid`
read). `[HIGH section list; CARRIED that the specific .bss globals are these — placed
here by the .bss output section, located by the allocator/parallel lanes]`

---

## 6. The per-core variant diff — the 2 MiB stride is the only change

### cpu0 → cpu1 (the entire material per-core delta)

`diff cpu0/.x cpu1/.x` = **exactly six lines**: `[HIGH/OBSERVED]`

| line | symbol | cpu0 → cpu1 |
|---|---|---|
| 1 | LSP-path comment | `…lsp_fll_load_cpu0` → `…cpu1` |
| 6 | `sram0_0_seg` org | `0x84000000` → `0x84200000` |
| 24 | `_memmap_mem_sram0_start` | `0x84000000` → `0x84200000` |
| 25 | `_memmap_mem_sram0_end` | `0x84200000` → `0x84400000` |
| 30 | `_memmap_seg_sram0_0_start` | `0x84000000` → `0x84200000` |
| 31 | `_memmap_seg_sram0_0_max` | `0x84200000` → `0x84400000` |

Nothing else changes — same `ENTRY`, `PHDRS`, IRAM seg, `SECTIONS`, cacheattr,
`region_map`.

**Body-hash proof.** Deleting lines `{1,6,24,25,30,31}` from each cpu0..7 `.x` and
hashing the remainder yields the **same** sha256 (`ff467ef81f619dc2…`) for **all
eight** cores. The 2 MiB-stride `sram` origin (+ its four mirror `_memmap` symbols + the
path comment) is **provably the only** material per-core difference. One object set, 8
link variants, base-shifted bank. `[HIGH/OBSERVED — body-hash]`

### cpu0 → cpu_single

`diff cpu0/.x cpu_single/.x` = **exactly four lines**: `[HIGH/OBSERVED]`

| line | symbol | cpu0 → single |
|---|---|---|
| 1 | path comment | `…cpu0` → `…cpu_single` |
| 6 | `sram0_0_seg` len | `0x200000` → `0x2000000` (2 MiB → 32 MiB) |
| 25 | `_memmap_mem_sram0_end` | `0x84200000` → `0x86000000` |
| 31 | `_memmap_seg_sram0_0_max` | `0x84200000` → `0x86000000` |

Same org (`0x84000000`), 16× larger window. The `_start`/`_max` **start** values are
unchanged; only the **end/max** grow. The single-core `.so` gets the whole low 32 MiB
of the `hbm_scratch` window — the space the 8 multicore cores together tile (16 MiB)
plus the next 16 MiB. `[HIGH/OBSERVED]`

---

## 7. Stack + heap reconciliation — why the LSP places neither

The most consequential structural finding: the custom-op LSP reserves **no stack and
no heap**. This is deliberate, proven by contrast with the toolchain's own reference
LSP for the same `ncore2gp` core: `[HIGH/OBSERVED]`

| feature | reference `sim` LSP | custom-op `fll_load` LSP |
|---|---|---|
| regions | `iram0_0` `0x0`/4 KiB + `iram0_1` `0x1000`/60 KiB + `dram0_0` `0x80000`/64 KiB + `sram0` `0x100000`/1 GiB | `iram0_0` `0x0`/4 KiB + `sram0_0` `0x84000000`/2 MiB |
| `__stack` | `PROVIDE(__stack = 0x40100000)` | **absent** |
| `_heap_sentry` | `0x40100000` | **absent** |
| `dram0_0_seg` | `0x00080000`/64 KiB (== per-core dataram `[0x80000,0x90000)`) | **absent** |
| `region_map` | `0x00000007` (3 regions) | `0x00000011` (2 regions) |

> **GOTCHA — the only "stack"/"heap" token in the whole LSP is metadata.** `rg`
> `stack|heap|dram|__stack|_heap` over **all nine** `.x` returns **only**
> `.note.GNU-stack` — the non-alloc executable-stack-marker section at org 0, **not** a
> runtime stack. No `__stack`, no `_heap_sentry`, no `dram0` region anywhere.
> `[HIGH/OBSERVED]`

**The linker view explains the runtime design:**

- **Stack.** The LSP defines no `__stack`, so the on-core stack is not a linker-placed
  top-of-memory pointer. The FLL loader/CRT establishes the tiny on-core stack at load
  time; the kernel's stack budget is the runtime `STACK_SIZE` headroom constant, and
  when it does not fit, the stack is **switched** to a runtime-allocated HBM stack
  (`allocate_hbm_stack` → `neuron_hbm_allocate`). The absence of a stack segment is the
  linker-side confirmation that the stack is a runtime object. See
  [Stack-switch + the dual-stack model](stack-switch.md). `[HIGH/OBSERVED LSP absence;
  CARRIED runtime mechanism]`
- **Heap.** The LSP defines no heap region / no `_heap_sentry` and no `dram0` region.
  The three xmem heaps (HBM-scratch / dataram / libc) are **runtime-carved** — none of
  their bases come from the LSP; they are set by
  `init_neuron_{hbm,dataram}_allocator` / `_init_libc_heap_allocator` at runtime. The
  LSP only places the heap **managers' bookkeeping** (the mgr structs + headers live in
  the customop `.bss` → `sram0_0_seg`), not the pools. See
  [Device allocators](device-allocators.md). `[HIGH/OBSERVED LSP absence; CARRIED
  runtime pools]`
- **Dataram.** The per-core dataram `[0x80000,0x90000)` that the `sim` LSP maps as
  `dram0_0_seg` is **omitted** from the custom-op LSP; the runtime reaches it via the
  `data_scratch_map` base (direct NX deref), never a linker `dram0` region.
  `[HIGH/OBSERVED LSP omission]`

> **The LSP is a pure code/const/bss placement script:** it pins the loaded library's
> vectors (IRAM 4 KiB) and its code + rodata + data + bss (the per-core `hbm_scratch`
> window), and leaves the stack, all three heaps, and the dataram to the runtime.

---

## 8. The `specs` file

The `specs` is byte-identical across all 9 LSPs (sha `431deb1a…`). After the Tensilica
MIT-license banner (Customer ID = 15949; Build = `0xa0ff9`; © 2012 Tensilica) it sets
exactly three spec strings: `[HIGH/OBSERVED verbatim]`

```
*startfile:
crti%O%s crtbegin%O%s

*endfile:
crtend%O%s crtn%O%s

*lib:

```

It pins the CRT start/end object order (`crti`/`crtbegin` … `crtend`/`crtn` — the
frame the `.ctors`/`.dtors` sections of §5 feed) and **blanks** the default `*lib`
spec, so the link pulls **only** the explicit `-l` libs from `build_custom_op.py`
(`-lloader -lxmem -lhal -lc++-e -lm -lgcc` + the `.a`s — no implicit default
libraries). The specs carries **no addresses**; all address pinning is in the
ldscript. The specs being identical across all 9 means the per-core difference is
100 % in the ldscript `sram` origin. `[HIGH/OBSERVED]`

---

## 9. The LSP-defined symbols — and who consumes them

The `.x` defines a large set of linker symbols: `[HIGH/OBSERVED verbatim]`

- **Memory boundaries:** `_memmap_mem_iram0_start/end` (`0x0`/`0x10000`),
  `_memmap_mem_sram0_start/end` (per core), `_memmap_seg_{iram0_0,sram0_0}_start/max/end`.
- **Reset / vecbase:** `PROVIDE(_memmap_reset_vector = 0x0)`,
  `PROVIDE(_memmap_vecbase_reset = 0x0)`, `_rom_store_table = 0`, `_itb_default = 0x0`.
- **Cache attributes:** `_memmap_cacheattr_{wb,wt,bp}_base`, `…_unused_mask`,
  `…_{wb,wba,wbna,wt,bp}_trapnull`, `…_{wb,wt,bp}_strict`, `…_{wb,wt,bp}_allvalid`,
  `_memmap_region_map = 0x11`, `PROVIDE(_memmap_cacheattr_reset = …_wb_trapnull)`.
- **Section boundaries:** `_stext/_etext`, `_text_start/_text_end`,
  `_rodata_start/_end`, `_data_start/_end`, `_bss_start/_bss_end`,
  `_bss_table_start/_bss_table_end` (with the `LONG(_bss_start)`/`LONG(_bss_end)` pair),
  `_{DispatchVector,ResetVector,ResetHandler,DispatchHandler}_text_start/end`, the
  clib/rtos section bounds, `__XT_EXCEPTION_TABLE__`/`__XT_EXCEPTION_DESCS__(_END__)`.

> **NOTE — these symbols are the linker↔loader contract, not a customop API.** `nm`
> over both `libneuroncustomop.a` and `libcweak.a` shows **zero** undefined (`U`)
> references to any `_memmap_*` / `_bss_start` / `_bss_end` / `_stext` / `_etext` /
> `__stack` / `_heap_sentry` / `_DispatchVector_text_start` symbol. They are consumed
> by the **toolchain CRT + FLL loader** that sit outside the customop package: the
> `_start` prologue reads `_bss_table_start..end` to zero `.bss`,
> `_memmap_cacheattr_reset` to program the cache,
> `_memmap_reset_vector`/`_DispatchVector_text_start` for the vector base, etc.
> `start_exit.o` references only `_init`/`_fini`/`__clibrary_init` — the higher-level
> C++ init, not the memmap symbols. `[HIGH/OBSERVED — nm of both archives +
> start_exit.o]`

---

## 10. Reconciliation against the runtime VADDR map

The same numbers, seen from the executing engine, are owned by
[The Q7 ELF VADDR + Per-Core Memory Model](q7-elf-vaddr.md). The build-time linker view
and the runtime VADDR model are the **identical** bases/stride/section map where they
overlap, and **consistent** (not divergent) on the regions the linker does *not* place:

| LSP element | linker (this page) | runtime VADDR | match? |
|---|---|---|---|
| `iram0_0_seg` vectors | `[0x0, 0x1000)` 4 KiB | `[0,0x80000)` low-NX IRAM; customop uses first 4 KiB | MATCH |
| `sram0_0_seg` base | `0x84000000` | `hbm_scratch` pinned 64-MiB window NX base | MATCH (exact) |
| per-core bank table | `0x84000000 + cpu·0x200000` (8 banks tile 16 MiB) | identical all-9-LSP table | MATCH (byte-exact) |
| sections → bank | `.text/.rodata/.data/.bss` → 2 MiB bank | same section→VADDR map | MATCH |
| `cpu_single` bank | `0x84000000` / 32 MiB | `0x84000000` / 32 MiB | MATCH |
| `cacheattr` r4 cached | decoded here (§4c) | not decoded at runtime | **EXTENDS** |
| stack | NOT in LSP | runtime HBM stack | CONSISTENT (runtime) |
| heaps | NOT in LSP | runtime xmem heaps | CONSISTENT (runtime) |
| dataram `[0x80000,0x90000)` | NOT in LSP | direct NX deref via DSM | CONSISTENT (runtime) |
| NX MEM REG `0x100000` | NOT in LSP | MMIO CSR block | CONSISTENT (MMIO) |
| dyn 16-MiB windows + SBUF `0x80000000` | NOT in LSP | runtime TLB / pinned win | CONSISTENT (runtime) |
| firmware `.text` `0x01000000` | not the customop LSP | EXTISA `.text` @ `0x01000000` | **DISTINCT PROGRAM** |

> **CORRECTION — the LSPs *are* in the shipped corpus.** An earlier NX-address-space
> survey stated "a single LSP linker script … is not in the shipped corpus" and
> reconstructed the LSP view from headers + firmware. That premise is **wrong**: the
> nine LSPs are present and readable at
> `…/custom_op/lsp_fll_load_cpus/lsp_fll_load_cpu*/ldscripts/elf32xtensa.x` and are
> decoded here directly. The reconstructed map was largely correct, with one
> refinement: the customop `.so`'s **own** code (`.text`) is linked into the `sram`
> (`hbm_scratch`) window at `0x84000000`, **not** into the low-NX IRAM at `0x0`. The
> low-NX IRAM at `[0x00000,0x80000)` is the **SEQ POOL firmware** image (a separate
> artifact, `.text` @ `0x01000000`); the customop LSP's IRAM use is only the first
> 4 KiB for the XEA3 vectors. Both statements are true of **different images** loaded
> into the same NX aperture at different times. `[HIGH/OBSERVED — §1 inventory]`

> **CORRECTION — "IRAM/code identical" across cores, precisely.** A multicore-build
> survey reported "IRAM/code identical" across the 8 cores. Exactly: (a) the
> `iram0_0_seg` (4 KiB @ `0x0`, vectors/handlers) is byte-identical across all 9 LSPs;
> (b) the bulk **code** (`.text`) is **not in IRAM at all** — it is in `sram0_0_seg`
> (the per-core `hbm_scratch` window). The `.text` *contents* are identical across
> cores (same objects), but its load **address** shifts by the 2 MiB stride along with
> everything else in `sram`. So read it as "IRAM (vectors, 4 KiB) byte-identical;
> `.text` (in sram) content-identical but base-shifted." `[HIGH/OBSERVED — §5/§6]`

---

## 11. Per-generation / per-core applicability

**Per-generation.** The nine LSPs are generated for
`/opt/workspace/SundaCustomOpLibrary` (line-1 comment, all 9). There is exactly **one**
LSP family shipped — the Sunda-targeted layout — and it is reused across generations:
no per-gen LSP variants (no cayman/mariana/mariana_plus LSP dirs; `fd` over the package
shows only `lsp_fll_load_cpu*`). The 2-region map is the shared layout for all device
gens that take the prelink/staging path. The per-gen memory deltas that *do* exist (the
`POOL_NX_DRAM` size, the HBM-stack arena cap) are **runtime/firmware-image** facts, not
customop-LSP facts — the LSP itself is gen-invariant. The on-device staging is handled
by [the prelinker](../runtime/prelinker-ucpl.md).
`[HIGH/OBSERVED single LSP family + decl path; MED that it is byte-identical across the
gens it serves — there is only one to read]`

**Per-core.** The only per-core difference is the `sram0_0_seg` bank base (2 MiB
stride; §6 body-hash). The `iram0_0_seg`, `region_map`, cacheattr, `PHDRS`, `ENTRY`, and
the entire `SECTIONS` block are byte-identical across cpu0..7 (`cpu_single` differs only
in bank length). The per-core SRAM window map is: **same single 2 MiB bank,
base-shifted by the PRID-indexed 2 MiB stride** — the link-time half of the SPMD dual
identity. See [Multicore SPMD model](multicore-spmd.md). `[HIGH/OBSERVED]`

**Per-cluster.** The customop ABI targets the 8-core TPB-POOL cluster only
(`get_cpu_count() == 8`) — hence 8 per-core LSPs (+ single). The Q7 NX-local map (and
thus the LSP's NX bank bases) is identical across PREPROC and POOL cores; the
per-cluster difference is the **SoC-physical** base, not the NX bank.

> **GOTCHA — two unrelated strides.** The SoC per-core slot pitch is **1 MiB**
> (`0x100000`), but the NX customop bank stride is **2 MiB** (`0x200000`). These are
> two different strides (SoC-physical core spacing vs NX link-bank spacing). The 8 LSP
> banks are an NX-link partition of the `hbm_scratch` window, **not** a reflection of
> the 1-MiB SoC core slots. The on-chip `STATE_BUF` SRAM (SoC `0x2000000000`) is also
> **not** what `sram0_0_seg` names — "sram0_0" is Tensilica's generic
> data/code-RAM-region label, here re-pointed at the HBM-resident `hbm_scratch`
> window. `[HIGH/OBSERVED the two strides; CARRIED the SoC bases]`

For the full SoC-side address map and the per-core window decode see
[The LSP SRAM Window Map](../control/address/lsp-sram-window-map.md), and for the
on-device fixed-location load see [The Host Prelinker](../runtime/prelinker-ucpl.md).

---

## 12. Confidence ledger

**HIGH / OBSERVED** (verbatim ldscript/specs/ELF bytes + byte-exact diff/hash):

- The 9 LSP dirs each with `elf32xtensa.{x,xn,xbn,xr,xu}` + `specs`; generator comment
  (`xt-genldscripts.tpp`, decl path `SundaCustomOpLibrary`).
- The five-variant meaning; `.x`/`.xn`/`.xbn` differ only in line 2; `.xr`/`.xu`
  relocatable (no `MEMORY`) and address-independent.
- `MEMORY`: `iram0_0_seg` `0x0`/`0x1000` (identical all 9) + `sram0_0_seg`
  `0x84000000 + i·0x200000`/`0x200000` (cpu0..7) | `0x84000000`/`0x2000000` (single).
  Full 9-row table; 2 MiB stride; 16 MiB tiling; `region_map = 0x11`.
- Section→region: 4 XEA3 vector/handler sections → IRAM; everything else → sram; `.bss`
  NOLOAD; debug at org 0. No stack/heap/`dram0` in any of the 9 scripts (only
  `.note.GNU-stack`).
- Per-core diff = 6 lines (cpu0→cpu1); body-hash identical cpu0..7. cpu_single = 4-line
  diff (len 32 MiB).
- `specs` byte-identical (sha `431deb1a…`); `*startfile` crti/crtbegin, `*endfile`
  crtend/crtn, `*lib` empty.
- Cacheattr `0x44414444` → r4 cached / r0 uncached; `unused_mask 0xFFF0FFF0` → only
  r0,r4 live. Reference `sim` (`0x44444111`) / `ldapp` (`0x44114111`) contrast. MPU
  background map 2-GiB split @ `0x80000000`; `ISSSysRam 0x100000`/`0x40000000`. `ldapp`
  `cached0_seg 0x80000000`/`0x20000000` + `memmap.xmm` "cached" naming.
- Neither archive imports any LSP-defined symbol (`nm`); the customop objects define no
  vector sections.

**MED / INFERRED:**

- `sram0_0_seg` @ `0x84000000` **is** the `hbm_scratch` pinned-window NX base (grounded
  in the window lane).
- The cached window aperture (r4=1) makes per-core code/data access fast (architectural
  reading of the cacheattr nibble).
- `-n`/`-N` produce the identical layout because the target is fixed in-memory (no
  demand paging).

**CARRIED** (from cross-referenced lanes):

- The specific `.bss` runtime globals placed in the bank (located by the allocator /
  parallel / stack-switch lanes; placed here by the `.bss` output section).
- The runtime stack/heap/dataram mechanisms the LSP's silence confirms.
- The PREPROC-cluster cores would see the same NX bank map (the customop never links
  for PREPROC).
