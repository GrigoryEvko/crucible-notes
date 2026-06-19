# The Multicore API (8-core SPMD)

This page documents the **multicore identity ABI** of the GPSIMD custom-op library —
the three symbols that let a custom-op kernel discover *which* of the 8 Vision-Q7 cores
of a TPB POOL cluster it is running on and *how many* peers it has, plus the memory and
build model that makes 8 cores run the same program over disjoint data. The runtime
shape is **SPMD** (single program, multiple data): one program image per core, all 8
running byte-identical code, each self-identifying by its **PRID** special register and
partitioning its slice of the work over **per-core-private memory**, with **no
cross-core barrier, semaphore, or atomic** anywhere in the shipped library.

Everything below is anchored to the **shipped artifact**
`custom_op/neuron/libneuroncustomop.a` (an `ar` archive, **not stripped**, with **DWARF
v4**; 10 members), its per-core Linker-Support-Package (LSP) scripts under
`custom_op/lsp_fll_load_cpus/`, the device header `custom_op/neuron/neuron-utils.hpp`,
the build driver `script/build_custom_op.py`, and the **`ncore2gp`** Xtensa config
itself (`tools/ncore2gp/config/` + `…/xtensa-elf/arch/include/xtensa/config/`). Native
disassembly is the **shipped** `xtensa-elf-objdump --xtensa-core=ncore2gp` (a benign
"TIE checksum does not match" warning is emitted; every instruction decodes). Claims are
tagged **`[HIGH/OBSERVED]`** (opcode bytes / DWARF / ELF `.rela` / raw LSP text /
verbatim script source / `ncore2gp` config), **`[MED]`**, or **`[INFERRED]`** (ABI
reasoning over those bytes). See also [`build_custom_op.py` Codegen](build-custom-op-codegen.md)
(the build front-end and the 8× link matrix in full), [Q7 ELF VADDR + Per-Core Memory
Model](q7-elf-vaddr.md) (the address-space backdrop), and the **External-Lib Prelink
Validation** page ([../firmware/pool/prelink-validation.md](../firmware/pool/prelink-validation.md))
(the firmware-side `NUM_POOL_CORES = 8` invariant that the *device* enforces at load).
The host-side runtime teardown that joins the 8 cores after a custom op — DRAIN +
completion-semaphore — belongs to the 8-core SPMD execution model
(`runtime/spmd-teardown.md`, Part 8 — not yet authored) and the device collective barrier
to the NCFW management core (Part 10 — not yet authored).

---

## 1. The three symbols — `parallel.o` is the entire identity API

`parallel.o` is a **tiny** translation unit: `.text` is `0x28` bytes total, `.bss` is 4
bytes. It *is* the whole multicore-identity surface. `nm -n parallel.o | c++filt`:

| Sym (mangled) | Demangled | Section / off | ELF size | DWARF `decl_line` (`parallel.cpp`) |
|---|---|---|---|---|
| `_Z10get_cpu_idv` | `get_cpu_id()` | `.text` `0x00` | `0x0d` (13) | 11 |
| `_Z13get_cpu_countv` | `get_cpu_count()` | `.text` `0x10` | `0x07` (7) | 15 |
| `_GLOBAL__sub_I_parallel.cpp` | static ctor | `.text` `0x18` | `0x10` (16) | 9 (artificial) |
| `_ZL6cpu_id` | `cpu_id` | `.bss` `0x00` | 4 | 9 (FILE-LOCAL) |
| `_ZL27SUNDA_UCODE_LIB_VERSION_STR` | `SUNDA_UCODE_LIB_VERSION_STR` | `.data` `0x00` | 9 | `pool_nrt_version.h:4` |

`[HIGH/OBSERVED — ELF symtab binding/size; DWARF `DW_AT_decl_line`; DWARF file table
file 4 = `parallel.cpp`, file 1 = `pool_nrt_version.h`.]`

Both API functions return `uint32_t` (DWARF `DW_AT_type` → `const uint32_t` typedef →
`unsigned int` `byte_size 4`), and both are `DW_AT_external 1` — they are **exported**.
Their declarations match `neuron-utils.hpp` verbatim:

```c
/* neuron-utils.hpp, lines 3-12 (verbatim) */
/* Return the cpu_id of the processor caller is on, id is in range [0, get_cpu_count()) */
uint32_t get_cpu_id();
/* Return the total number of cpu available */
uint32_t get_cpu_count();
```

That `id in [0, get_cpu_count())` contract is, structurally, the MPI-style
`(rank ∈ [0, size))` pair — `get_cpu_id()` is the **rank**, `get_cpu_count()` is the
**size**. `[HIGH/OBSERVED header bytes + DWARF return types.]`

There is **no fourth symbol**. `num_cores` (used to fold the `8`, §3) exists **only** as
a DWARF constant — no `.bss`/`.data` slot. `SUNDA_UCODE_LIB_VERSION_STR` is an unrelated
version string (`.data` bytes `31 2e 32 31 2e 31 2e 30 00` = `"1.21.1.0\0"`), which
reconciles with `build_custom_op.py`'s `versions['ulib_to_ucode_version'] = '1.21.1.0'`.
`[HIGH/OBSERVED `.data` bytes + script source.]`

> **NOTE.** `parallel.cpp`'s `DW_AT_producer` is `XtensaTools-14.09 clang version 10.0.1`,
> `DW_AT_comp_dir` `/opt/workspace/SundaCustomOpLibrary/custom_op/library`. The `__FILE__`
> path is used **only** to map symbols to decl lines, not for any source content.

---

## 2. `get_cpu_id` — a cached PRID read, byte-exact

Core identification is a **cached** read of the **PRID** special register. Three pieces,
disassembled with the shipped `xtensa-elf-objdump --xtensa-core=ncore2gp -dr`:

### 2a. The init — the static constructor `_GLOBAL__sub_I_parallel.cpp` @ `0x18`

```
00000018 <_GLOBAL__sub_I_parallel.cpp>:
  18: 36 41 00   entry    a1, 32
  1b: 24 00 00   const16  a2, 0     ; R_XTENSA_SLOT0_ALT .bss  -> a2 = &cpu_id
  1e: 24 00 00   const16  a2, 0     ; R_XTENSA_SLOT0_OP  .bss
  21: 30 eb 03   rsr.prid a3        ; a3 = PRID special register 235 (0xEB), RAW
  24: 39 02      s32i.n   a3, a2, 0 ; cpu_id = PRID
  26: 1d f0      retw.n
```

DWARF marks the `0x1b..0x27` body as the inlined `__cxx_global_var_init` for `cpu_id`
(`parallel.cpp:9`). The constructor is registered through the `.ctors` array: `.ctors`
holds one 4-byte word `18 00 00 00` (= the offset of this very function) with a
`.rela.ctors` `R_XTENSA_32` against the `.text` section — so on core/library init this
runs exactly once. `[HIGH/OBSERVED: opcode bytes + `.rela.text {0x1b,0x1e → .bss+0}` +
`.ctors` word `0x18` + `.rela.ctors`.]`

> **QUIRK — the raw PRID is stored with NO mask and NO shift.** The logical core id ==
> the raw PRID value. The instruction *between* the `rsr.prid a3` read and the
> `s32i.n a3, a2, 0` store is **the store itself** — there is no
> `rsr.prid; extui/srli/and` sequence. At *this* layer the customop code treats PRID as
> the dense `[0..7]` core index directly. `[HIGH/OBSERVED: the `rsr.prid → s32i.n` pair
> is adjacent, nothing between them.]`

### 2b. `get_cpu_id()` @ `0x00` — just loads the cached word

```
00000000 <_Z10get_cpu_idv>:
   0: 36 41 00   entry    a1, 32
   3: 24 00 00   const16  a2, 0     ; R_XTENSA_SLOT0_ALT .bss  -> a2 = &cpu_id
   6: 24 00 00   const16  a2, 0     ; R_XTENSA_SLOT0_OP  .bss
   9: 28 02      l32i.n   a2, a2, 0 ; return cpu_id
   b: 1d f0      retw.n
```

`get_cpu_id()` returns the value the constructor latched = the raw PRID. The load target
`.bss+0` is `cpu_id` (the same `.rela` symbol the ctor stored to). The DWARF type of
`cpu_id` is `const uint32_t`: it is `const` at the source level (set once in the ctor)
but lives in `.bss` (NOBITS) and is mutated exactly once — the C++ "const but
dynamically-initialised global" idiom. `[HIGH/OBSERVED DWARF + the single store.]`

```c
/* The complete recovered semantics of parallel.o, naming the real symbols. */
static const uint32_t cpu_id;                 /* _ZL6cpu_id, .bss, file-local      */

void _GLOBAL__sub_I_parallel_cpp(void) {      /* runs once at core/lib init        */
    *(uint32_t *)&cpu_id = read_prid();        /* rsr.prid a3; s32i.n a3 -> &cpu_id  */
}                                              /* NO mask, NO shift                  */

uint32_t get_cpu_id(void)    { return cpu_id; }    /* l32i.n .bss+0 — cached         */
uint32_t get_cpu_count(void) { return 8u;     }    /* movi.n a2,8 — folded constant  */
```

### 2c. The hardware side — `PRID` is the dedicated SR 235, low 4 bits are the core index

The `rsr.prid` opcode is `30 eb 03`: the `0xEB` byte is the special-register number
**235**, which the `ncore2gp` config defines as `#define PRID 235` (`specreg.h:66`).
PRID is a **dedicated** special register — *not* a MISC register (those are SR 244/245,
`MISC_REG_0`/`MISC_REG_1`). `XCHAL_HAVE_PRID == 1`, `usePRID="1"` in `core.xparm`.
`[HIGH/OBSERVED: `specreg.h` + `core-isa.h` + `core.xparm`.]`

> **CORRECTION — it is the dedicated PRID SR, not a "MISC-register read".** Some prose
> frames Q7 core identity as a MISC-register read; the recovered opcode reads SR `0xEB`,
> which the shipped `specreg.h` names `PRID`. The MISC registers (`MISC_REG_0`/`_1`,
> SR 244/245) are a *separate* facility and are **never** read by `libneuroncustomop.a`.

The `ncore2gp` ISA defines the core-id field inside PRID as `PRID_ID_SHIFT 0`,
`PRID_ID_BITS 4`, `PRID_ID_MASK 0x0000000F` (`core-isa.h`). So the dense core index is
PRID's low **4 bits** (shift 0). `[HIGH/OBSERVED config.]` The customop layer applies no
mask of its own and relies on the per-core PRID wiring placing the index in those bits
with the high bits clear; the `cpu_id < 8` assert in `data_transfer.o` (§5) would trap any
PRID carrying value ≥ 8. So within this build PRID is treated as the bare 3-bit core id
`∈ {0..7}` (8 cores use 3 of the 4 field bits). `[HIGH on the runtime contract; INFERRED
that the integration wiring pre-places the index in `PRID[3:0]`.]`

---

## 3. `get_cpu_count` — the constant `8`, and where 8 is enforced

```
00000010 <_Z13get_cpu_countv>:
  10: 36 41 00   entry    a1, 32
  13: 0c 82      movi.n   a2, 8     ; return 8 — IMMEDIATE, no register/.bss read
  15: 1d f0      retw.n
```

`get_cpu_count()` is a **compile-time constant `8`** baked into the instruction stream.
The `8` is the DWARF `num_cores` (`parallel.cpp:8`, `DW_AT_const_value 8`, type
`const uint32_t`), constant-folded into the `movi.n`. There is **no** `num_cores` runtime
slot. `[HIGH/OBSERVED opcode + DWARF.]`

`NUM_CORES = 8` is enforced redundantly across four independent surfaces — they all agree:

| Surface | Mechanism | Evidence |
|---|---|---|
| **Device API** | `parallel.cpp` `num_cores=8` → `get_cpu_count()` returns 8 | `movi.n a2,8` + DWARF `const_value 8` `[HIGH/OBS]` |
| **SDMA path** | `data_transfer.cpp:171` `cpu_id < 8` fatal `_Assert` | assert string at `.data+0x7ec` (§5) `[HIGH/OBS]` |
| **Build matrix** | `build_custom_op.py` `NUM_CPUS = 8` → 8 link commands | script line 47 (§6) `[HIGH/OBS]` |
| **Per-core LSPs** | 8 dirs `lsp_fll_load_cpu0..cpu7` (+`_single`) | `lsp_fll_load_cpus/` listing (§6) `[HIGH/OBS]` |
| **Firmware** | `NUM_POOL_CORES = 8` / `total_cpus ∈ {1,8}` device assert | [../firmware/pool/prelink-validation.md](../firmware/pool/prelink-validation.md) `[HIGH cross-page]` |

`[HIGH/OBSERVED across ≥4 sources.]`

> **NOTE — the customop ABI targets the 8-core POOL cluster only.** `get_cpu_count()`
> being a hardcoded `8` means the custom-op ABI assumes a **fixed** 8-core POOL cluster;
> only one `parallel.o` ships, with count 8. (The smaller PREPROC Q7 cluster is a
> different engine, not a custom-op host — `[MED/INFERRED]`.) The firmware's
> `total_cpus ∈ {1, 8}` disjunction matches the single-core vs 8-core *build*
> (`cpu_single` LSP vs `cpu0..7`), not a third cluster size.

---

## 4. Who consumes `get_cpu_id` — it is a CUSTOMER-facing API

A full cross-object sweep over all 10 archive members (`nm | rg`):

- `get_cpu_id()` / `get_cpu_count()` are **defined only** in `parallel.o` and referenced
  by **no other member** — a sweep for `U _Z10get_cpu_idv` / `U _Z13get_cpu_countv`
  across the archive returns **zero** undefined references.
- `cpu_id` (`_ZL6cpu_id`) is **file-local** to `parallel.o`; nothing outside it reads the
  global.

`[HIGH/OBSERVED.]` So `get_cpu_id`/`get_cpu_count` are **exported ABI surface for the
customer kernel** (the `.so` the kernel author links). The library's own SDMA code in
`data_transfer.o` *re-reads* PRID directly (§5) rather than calling `get_cpu_id()` — the
two PRID consumers are independent; the SDMA window logic does **not** depend on
`parallel.o`'s cached `cpu_id`. The firmware-side consumer is the POOL dispatcher, which
hands a custom op its opcode tagged with the per-core index; the kernel then partitions
its channels by `get_cpu_id()` (§7). `[HIGH/OBSERVED here; HIGH cross-page for the
dispatcher — see ../firmware/pool/prelink-validation.md and the POOL dispatch page.]`

---

## 5. The SECOND PRID read — `data_transfer.o` SDMA window selection

A whole-archive `rsr.prid` sweep finds exactly **two** reads:

| Member | `rsr.prid` count | Use |
|---|---|---|
| `parallel.o` | 1 | the cached `cpu_id` (§2a) |
| `data_transfer.o` | 1 | the SDMA SoC-window index (this section) |
| *the other 8 members* | 0 | — |

`[HIGH/OBSERVED: `xtensa-elf-objdump -d` over all 10 members.]`

The second read is in `dram_addr_to_soc_addr(unsigned int)` @ `0x210`, block @ `0x23e`:

```
 23e: rsr.prid a5                                  ; raw PRID
 243: { bgeui.w15 a5, 8, 0x2ec ; l32i a4,a4,0 }    ; if PRID >= 8 -> _Assert (fatal)
 24b..0x2a8: binary-decision tree on a5 ->
     movi a5,{9,11,13,15,17,19,21,23} for prid {0,1,2,3,4,5,6,7}
     (= window_index = 2*cpu_id + 9 ; unreachable default -> _Assert(0))
 2b0: { movi a6,-1 ; slli a5, a5, 16 }             ; window_index << 16
```

The `0x243` `bgeui.w15 a5, 8, 0x2ec` branch targets a `const16 → callx8 _Assert` arm; the
guard string `…/data_transfer.cpp:171 cpu_id < 8` lives at `.data+0x7ec` and the
unreachable-default string `…/data_transfer.cpp:200 0` is the binary-tree fall-through.
`[HIGH/OBSERVED: every `movi` arm read; both assert strings present; `_Assert` is an
undefined ref called via `callx8`.]`

So the SDMA path maps the **same** raw PRID to a **different** value
(`window_index = 2·prid + 9`, the per-core 64 KiB SoC aperture) than `get_cpu_id()`
(the bare `prid`). Same register, same `{0..7}` domain, two independent decodes. The full
SDMA window math (`soc_addr = SoC_BASE + (window_index<<16) + (dram_addr − 0x80000)`, with
the `[0x80000,0x90000)` dataram staging window guarded by the `data_transfer.cpp:160`
assert) is covered on the data-transfer ABI pages; here it only matters as the second,
independent witness that core identity is **always** a fresh `rsr.prid`, never a shared
variable. `[HIGH/OBSERVED.]`

---

## 6. The `_cpuN.so` build matrix — 8 per-core variants + a single-core fallback

The full build front-end is documented on [`build_custom_op.py` Codegen](build-custom-op-codegen.md);
this section captures only what makes the **8 outputs differ**.

### 6a. One compile, eight links

`build_custom_op.py` (verbatim, lines 47-50, 329-335):

```python
NUM_CPUS = 8
MULTI_Q7_LIBS = []
for cpu_id in range(NUM_CPUS):
    MULTI_Q7_LIBS.append(f' {NEURON_ROOT}/neuron/libneuroncustomop.a '
        f'{NEURON_ROOT}/c10/lib/libc10.a -lloader '
        f'-mlsp={NEURON_ROOT}/lsp_fll_load_cpus/lsp_fll_load_cpu{cpu_id} '
        f'-lxmem -lhal -lc++-e -lm -lgcc {NEURON_ROOT}/neuron/libcweak.a')

def _link(name, objs, build_directory, multicore, verbose=False):
    if multicore:
        # Note: Compiler assumes that multi-core library is in format xxx_cpuX.so
        return [_link_one(f'{name}_cpu{i}', objs, build_directory, lib_cur_cpu, verbose=verbose)
                for i, lib_cur_cpu in enumerate(MULTI_Q7_LIBS)]   # 8 outputs
    else:
        return [_link_one(name, objs, build_directory, LIBS_SINGLE, verbose=verbose)]
```

From the **same** object set (the kernel + its generated wrapper, compiled **once**), the
multicore path links **eight** shared objects `name_cpu0.so … name_cpu7.so`, each against
a **different** per-core LSP; the single-core path links **one** `name.so` against
`lsp_fll_load_cpu_single`. The output naming `xxx_cpuX.so` is a contract the downstream
loader relies on (explicit code comment). Each `.so` is then packed/stripped via
`xt-pkg-loadlib` to `*.packed.so` / `*.stripped.so`. Compile flags (`CC_OPT`):
`xt-clang++ -g -std=c++14 -stdlib=libc++-e -fno-jump-tables -Os -mcoproc -fpic -mlongcalls
--xtensa-core=ncore2gp …`. `[HIGH/OBSERVED verbatim source.]`

### 6b. What differs between the 8 `.so` — the per-core LSP `sram0_0_seg` origin

A byte-exact `diff` of the per-core LSPs (`ldscripts/elf32xtensa.x`) shows the **only**
material per-core difference is the `sram0_0_seg` memory-segment **origin**:

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

So `sram0_0_seg.org = 0x84000000 + cpu_id · 0x200000` (exactly a **2 MiB** stride,
verified for all 8). The 8 per-core windows **tile** `[0x84000000, 0x85000000)` = 16 MiB
with no gap and no overlap; the single-core LSP spans the whole 32 MiB
`[0x84000000, 0x86000000)`. The `iram0_0_seg` (org `0x00000000`, len `0x1000` = 1 KiB) is
**identical** across all 9 LSPs. `[HIGH/OBSERVED: all 8 origins + the single.]`

The entire `cpu0` → `cpu1` delta is just the LSP-path comment, the `sram0_0_seg` org, and
the four `_memmap_*_sram0_*` symbols — nothing else (same `ENTRY(_start)`, same `PHDRS`,
same IRAM vectors, same code). `[HIGH/OBSERVED full diff.]`

```
6c6:   sram0_0_seg : org = 0x84000000   ->   org = 0x84200000
24,25: _memmap_mem_sram0_{start,end}  0x84000000/0x84200000 -> 0x84200000/0x84400000
30,31: _memmap_seg_sram0_0_{start,max} likewise
```

### 6c. Significance — the SRAM window holds the **whole image**, and is the per-core partition

In each LSP, only the four IRAM **vectors** (`.DispatchVector.text`, `.ResetVector.text`,
`.ResetHandler.text`, `.DispatchHandler.text`) go to `iram0_0_seg`; the main `.text`,
`.rodata`, `.data`, `.bss` (NOLOAD), `.ctors` (where `parallel.o`'s constructor lands)
and `.dtors` all go to `sram0_0_seg`. `[HIGH/OBSERVED LSP section→segment mapping.]` So
the per-core 2 MiB window holds the **entire program image plus its data and BSS** — and
`cpu_id` (in `.bss`) therefore lives at a *core-specific* address even though every core
runs identical code.

`0x84000000` is the pinned **`hbm_scratch`** window base; the LSPs carve its low 32 MiB
into eight 2 MiB private per-core sub-windows. The **core id is thus encoded into the
`.so` at LINK time** (the `sram0_0_seg` base, fixed per `-mlsp`), complementing the
**RUN-time** PRID read of §2. `[HIGH/OBSERVED LSP values; the "hbm_scratch sub-window"
identity is `[MED]` — grounded in the pinned-window record, not re-derived here.]`

> **NOTE — the shipped files are the LSP *recipes*, not the built `.so`.** The 8
> `_cpuN.so` are produced on the *customer's* machine by `build_custom_op.py` from the
> customer's kernel. What ships is the 8 per-core link recipes; the "diff a pair" above
> is a diff of the two LSPs that *define* `cpu0.so` vs `cpu1.so`. `[HIGH/OBSERVED — honest
> scoping.]` "`lsp_fll_load`" = LSP for the "FLL-load" loader (`-lloader`); the per-core
> LSP fixes the load/window addresses. `[INFERRED from the `-lloader` arg + LSP naming.]`

---

## 7. The work-partition / SPMD model

The model is **SPMD**: one program image per core (`_cpuN.so`), all 8 running the **same**
code, each identifying itself by PRID and slicing its share of the work.

### 7a. Same code, N images
The 8 `_cpuN.so` are linked from **identical objects** (§6a) — byte-identical code,
differing only in the linker-assigned SRAM window. This is the SPMD "single program,
multiple data" build. `[HIGH/OBSERVED.]`

### 7b. Self-identification at run time
Each core calls `get_cpu_id()` (its **rank**, the cached PRID `∈ [0,8)`) and
`get_cpu_count()` (its **size**, the constant 8) — the classic SPMD `(rank, size)` pair,
exactly the `neuron-utils.hpp` contract `id ∈ [0, get_cpu_count())`. `[HIGH/OBSERVED.]`

```c
/* The SPMD self-ID idiom a custom-op kernel uses (real symbols). */
uint32_t me = get_cpu_id();      /* rank  = cached raw PRID, in [0,8)            */
uint32_t n  = get_cpu_count();   /* size  = 8 (constant)                        */
/* partition this op's work over [me, n): e.g. channel sub-range below          */
uint32_t chans_per = (num_chans + n - 1) / n;     /* ceil-divide across cores   */
uint32_t lo = me * chans_per;
uint32_t hi = (lo + chans_per < num_chans) ? lo + chans_per : num_chans;
for (uint32_t c = lo; c < hi; ++c) process_channel(c);   /* this core's slice    */
```

### 7c. Partitioning by channel
The firmware-resolved POOL kernels partition the tensor by `get_cpu_id()` **across
channels**: the dispatcher logs `"P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."` and
the kernel logs `"num_chans = %0d"` / `"pool_num = %0d"` / `"pool_per = %0d"`. The dispatch
loop is **not** skipped per core — every core runs the same scan; the CPU ID is a
**work-partition gate** (which channels this core owns), not a dispatch gate. The exact
per-kernel slicing arithmetic lives in the kernel images, not this lib — the pseudo-code
above is the *shape*, not a recovered formula. `[HIGH cross-page (POOL dispatch /
prelink-validation) + HIGH API here; `[MED]` on the precise channel formula.]`

### 7d. Per-core private memory — the partition is by **private** memory, not shared

| Resource | Per-core scope | Anchor |
|---|---|---|
| **DATARAM / TCM** | private on-core DRAM (256 KiB/core); the `xmem` heap lives here | per-core DRAM map; no shared dataram heap `[HIGH cross-page]` |
| **SoC aperture** | private 64 KiB SoC window, `idx = 2·prid + 9`, cores `0x10000` apart | §5 `[HIGH/OBS]` |
| **HBM-scratch window** | private 2 MiB sub-window, `sram0_0_seg = 0x84000000 + prid·0x200000` | §6b `[HIGH/OBS]` |
| **HBM pool** | physically **shared** backing store; each core's *view* is window-private | allocator bookkeeping is per-core `[MED/INFERRED]` |

The big HBM region is the shared backing store, but each core's allocator bookkeeping is
in its **own** private dataram (`.bss`), and each core reaches HBM through its **own**
per-core SRAM window / SoC aperture. So HBM is **physically shared but each core's VIEW is
partitioned** (window-private): two cores would **not** see each other's allocations
unless the host hands over a shared HBM address. `[HIGH on the per-core windowing;
MED/INFERRED on the shared underlying pool.]`

> **The multicore model is per-core-PRIVATE memory, not shared.** This is exactly why the
> `xmem` heaps run with **no lock** — each core owns its own heap, so there is no
> concurrent access to a shared structure to race. The single-threaded-per-core
> assumption is the **design**, not an oversight. `[HIGH/OBSERVED across §6/§7d.]`

---

## 8. Inter-core synchronization — PROOF OF INDEPENDENCE

A full sync-primitive sweep over **all 10 members** of `libneuroncustomop.a`:

**Atomic-exclusive** (`l32ex` / `s32ex`): **ZERO** occurrences in **any** object.
`[HIGH/OBSERVED.]`

**Cross-core symbols** (`barrier` / `semaphore` / `spinlock` / `atomic` / `mutex` /
`sync` / `rendezvous` / `collective` / `allreduce` / `broadcast`): the **only** lock/mutex
symbols are libc++ internal **weak** threading shims —
`std::__1::__libcpp_{mutex,recursive_mutex}_*` (in `NeuronAllocator.o`, `start_exit.o`,
`wrapper_api.o`) and `std::__1::mutex::~mutex`. And these are **no-ops**:
`__libcpp_mutex_lock` is literally `entry; movi.n a2, 0; retw.n` (returns "success"
without taking anything); `__libcpp_mutex_trylock` returns 1. They are libc++ boilerplate
wrapping a single-thread no-op, **not** a device cross-core lock. There is **no** barrier,
**no** semaphore, **no** spinlock, and **no** atomic-CAS device symbol anywhere; the only
undefined "collective"-shaped reference is `std::__1::__basic_string_common::__throw_length_error`
(a string-class throw, not a collective). `[HIGH/OBSERVED.]`

**Ordering fences** (`memw` / `isync` / `wsr` / `rsr`) — all accounted for, **none**
cross-core:

| Member | counts | what they are |
|---|---|---|
| `data_transfer.o` | `memw×17`, `rsr×1` | SDMA doorbell barriers (DMA ordering) + the SDMA PRID read (§5) |
| `switch_stack.o` | `isync×4`, `wsr×12`, `rsr×9` | windowed-register stack-switch context save/restore (`ps/lbeg/lend/lcount/sar/prefctl/isl/wb/epc`) |
| `stack_switch.o` | `isync` (via `switch_stack`), `rsr.isl×1`, `memw×3` | ISL/stack context mechanics |
| `parallel.o` | `rsr×1` | the cached PRID read (§2a) |
| `start_exit.o`, `translation.o`, `NeuronAllocator.o`, `wrapper_api.o` | scattered `memw` | DMA/store ordering |

The `rsr`/`wsr` are **all** context-save SRs (no `wsr.misc`/`rsr.misc`, no `wer`/`rer`
inter-core register ops anywhere) plus the two `rsr.prid` reads. `[HIGH/OBSERVED — the SR
names are all context-save SRs; sweep confirms no inter-core SR/TIE op.]`

> **CORRECTION — the stack-switch SRs split across two members.** The full windowed
> context-save SR set (`ps/lbeg/lend/lcount/sar/prefctl/isl/wb/epc`) lives in
> `switch_stack.o` (`wsr×12`, `rsr×9`, `isync×4`); a *single* `rsr.isl` + 3 `memw` also
> appear in `stack_switch.o`. Both are per-core stack mechanics — neither is cross-core —
> so the conclusion is unchanged, but the SRs are not all in one object.

> **The 8 cores are FULLY INDEPENDENT within the custom-op library.** There is no barrier,
> no semaphore, no atomic, no shared lock in `libneuroncustomop.a`. Each core runs its
> kernel on its private memory, identifies itself by PRID, and does **not** synchronize
> with peers **at this layer**. The absence is *proven* by the exhaustive sweep plus the
> per-core-private memory model of §7d. `[HIGH/OBSERVED.]`

This is corroborated by the **hardware** config: the `ncore2gp` Xtensa subsystem is
`numOfCores="1"` with `MPCoherencySupport="0"` — i.e. **no multiprocessor cache
coherency**. The "8 cores" is a TPB-cluster *integration* fact (8 instances of this 1-core
config), not an Xtensa MP config; cross-core shared-memory sync is unsupported at the HW
level, which is exactly why the library's per-core-private model is the only safe one.
`[HIGH/OBSERVED `core.xparm` `MPCoherencySupport="0"`.]` (The config *does* expose
`setsOfIntercoreInterrupts="4"` / `numOfBroadcastIntPins="4"` SoC-level inter-core
interrupt pins — but `libneuroncustomop.a` never touches them.)

### Where cross-core / cross-rank sync actually lives (NOT here)

- The **device collective barrier** (the `CORE_BARRIER` pseudo-op, the NEFF device
  barrier) is a **separate, higher** layer: the NCFW management core's counted-semaphore
  barrier that brackets collective (ring/mesh) phases across **engines** and
  **ranks/dies**, driven by DMA-broadcast + semaphore-wait-GE over SoC semaphore CSRs. It
  is **not** the intra-POOL-cluster Q7 sync, does **not** appear in the custom-op kernel
  ABI, and the 8 Q7 cores running a custom op do **not** invoke it. (NCFW, Part 10 — not
  yet authored.) `[HIGH that it is absent from this lib.]`
- If a custom op needed the 8 cores to rendezvous, the synchronization would have to be
  supplied by the kernel/firmware **outside** this library (e.g. the POOL per-core
  `run_state_0..7` CSRs, or the TPB event/semaphore unit, or the SoC inter-core interrupt
  pins above) — but the customop lib itself ships **no** such primitive. `[MED/INFERRED —
  the HW rendezvous facilities exist but `libneuroncustomop.a` does not touch them.]`

The **host-side** join after a custom op (DRAIN + completion-semaphore wait) is the
runtime's responsibility, not the device library's; it belongs to the 8-core SPMD
execution model (`runtime/spmd-teardown.md`, Part 8 — not yet authored).

---

## 9. Synthesis — the full multicore model

```
BUILD TIME  (build_custom_op.py, multicore=True):
  one kernel source  --compile ONCE-->  link 8x against lsp_fll_load_cpu{0..7}
  --> name_cpu0.so .. name_cpu7.so.  Each .so differs ONLY in
      sram0_0_seg = 0x84000000 + i*0x200000  (its private 2 MiB hbm_scratch sub-window);
      IRAM vectors + .text/.rodata/.data/.bss layout otherwise identical.
  (single-core: name.so vs lsp_fll_load_cpu_single, whole 32 MiB.)

LOAD TIME:
  the FLL loader (-lloader) places each _cpuN.so on Q7 core N; the LSP fixes addresses.

INIT TIME:
  parallel.o's static ctor runs `rsr.prid` ONCE -> caches it in file-local cpu_id (.bss).
  data_transfer.o re-reads PRID on demand for SDMA window selection.

RUN TIME (SPMD):
  uint32_t me = get_cpu_id();     // cached raw PRID, in [0,8)
  uint32_t n  = get_cpu_count();  // 8 (constant)
  // partition the tensor (by channels) over this core's share.
  // private dataram (256 KiB) -> private SoC aperture (idx = 2*prid+9)
  //                           -> private 2 MiB hbm_scratch window.
  // NO cross-core barrier/semaphore/atomic is used or shipped.

IDENTITY IS ENCODED TWICE:
  at LINK time  (the sram window, fixed per .so)  and
  at RUN  time  (the PRID, read by the code).
  Both agree on the {0..7} domain: get_cpu_count()==8, cpu_id<8 assert, 8 LSPs.
```

---

## 10. Confidence ledger

**HIGH / OBSERVED** (opcode bytes via shipped `xtensa-elf-objdump`; DWARF
`const_value`/`decl_line`/`byte_size`; ELF symtab/`.rela`; raw LSP text; verbatim build
source; `ncore2gp` config):

- `parallel.o` symbol map; `get_cpu_id @0x00` (`l32i cpu_id`), `get_cpu_count @0x10`
  (`movi.n a2,8`), the static ctor `@0x18` (`rsr.prid a3 → s32i.n a3, &cpu_id`, **NO
  mask/shift**); raw `.text` bytes `36 41 00 / 24 / 28 02 / 1d f0 / 0c 82 / 30 eb 03 /
  39 02`.
- `cpu_id` = file-local `const uint32_t` in `.bss`; `num_cores` = DWARF const `8` (folded,
  no slot); `SUNDA_UCODE_LIB_VERSION_STR = "1.21.1.0"`.
- `get_cpu_id`/`get_cpu_count` referenced by **no** other archive member; `cpu_id`
  file-local — exported customer API.
- The PRID SR number is **235 (`0xEB`)**, a **dedicated** register (`specreg.h`), with
  core-id field `PRID[3:0]` (`PRID_ID_MASK 0x0F`, shift 0).
- `data_transfer.o` second PRID read: `cpu_id < 8` assert + `2·prid + 9` window switch
  (the only other `rsr.prid` in the archive).
- Build matrix: `NUM_CPUS = 8`, 8 link cmds with `-mlsp=cpu{0..7}`, output `name_cpu{i}.so`;
  single fallback `cpu_single`. Verbatim source.
- LSP diff: `sram0_0_seg.org = 0x84000000 + i·0x200000` (2 MiB stride, all 8), single =
  whole 32 MiB; IRAM identical; the only material per-core diff. Code+data live in the
  SRAM window.
- Sync sweep: **zero** `l32ex/s32ex`/barrier/semaphore/spinlock/atomic in any member; only
  libc++ **no-op** mutex shims; `memw` = DMA doorbell; `isync/wsr/rsr` = stack-switch
  context save + the two PRID reads. `numOfCores="1"`, `MPCoherencySupport="0"` in config.

**MED:** HBM is physically shared but each core's view/bookkeeping is private; the
per-kernel channel-slicing formula lives in the kernel images; `lsp_fll_load` = LSP for
the FLL loader (loader internals not decoded); the SRAM window = the pinned `hbm_scratch`
sub-window (grounded elsewhere, not re-derived).

**INFERRED:** the HW PRID register is pre-placed to the dense `[0..7]` index in `PRID[3:0]`
(the customop layer applies no mask; the `cpu_id < 8` assert catches extra high bits); the
POOL per-core `run_state_0..7` CSRs / SoC inter-core interrupt pins *could* provide a HW
rendezvous, but `libneuroncustomop.a` does not touch them; the hardcoded `8` ⇒ the customop
ABI targets the 8-core POOL cluster only.
