# Build → Compile → Link → Strip → Package Flow

This page is the **end-to-end pipeline** that turns one user custom-op `.cpp` into the
shipped device artifacts — the linked `.so`, the symbol-pruned `.stripped.so`, and the
device-loadable `.packed.so`. It is the orchestration view: the [`build_custom_op.py`
Codegen](build-custom-op-codegen.md) page covers *what text the wrapper contains*; this
page covers *what commands run, in what order, with what flags, against which linker
specs*, and *what the packager actually does to the ELF*. Read them together — the
codegen page owns `wrapper.cpp`; this page owns everything from the first `xt-clang++`
invocation through the relocatable blob handed to the on-device prelinker.

Everything below is anchored to shipped artifacts read **verbatim**:

| Artifact | Path | Role |
|---|---|---|
| Driver | `…/gpsimd/script/build_custom_op.py` (395 lines) | the 5-phase orchestrator |
| Packager | `…/XtensaTools/lib/scripts/xt-pkg-loadlib.cpl` (288 lines, Perl) | strip + pack to relocatable load-lib |
| Per-core LSPs | `…/gpsimd/custom_op/lsp_fll_load_cpus/lsp_fll_load_cpu{0..7,_single}/` | 9 linker specs (`ldscripts/elf32xtensa.x` + `specs`) |
| Runtime lib | `…/custom_op/neuron/libneuroncustomop.a` | defines `customop_*` / `switchBack`; ELF32-Xtensa REL archive |
| Stack header | `…/custom_op/neuron/stack_switch.hpp` | `STACK_SIZE` default |
| Toolchain params | `…/XtensaTools/config/ncore2gp-params` | pins `RI-2022.9` / `14.09` / `Xm_ncore2gp` / `NX1.1.4` |

Claims are tagged `[HIGH/OBSERVED]` (verbatim script/LSP/ELF bytes), `[MED/INFERRED]`
(reasoning over those bytes), or `[CARRIED]` (from a cross-referenced lane). Source
report: **ABI-14**. Line cites are `build_custom_op.py:N` or `xt-pkg-loadlib.cpl:N`
unless another file is named. Package version: customop-lib **0.21.2.0**
(`build_custom_op.py:10`, `__version__ = '0.21.2.0'`).

---

## 0. Toolchain pin

Before any pipeline detail, pin the toolchain. The config params file states it outright
(`ncore2gp-params:15-27`): `[HIGH/OBSERVED]`

```
SWToolsRelease = RI-2022.9        # Cadence/Tensilica RI-2022.9 release train
SWToolsVername = 14.09            # XtensaTools 14.09  (SWToolsVersion = 1409000)
TargetHWVersion = NX1.1.4
ConfigName     = Xm_ncore2gp      # the Q7 "ncore2gp" core config
arch           = Xtensa24
uarchName      = Cairo
IsaIsBigEndian = 0                # little-endian
HasVectorPipe  = 1
```

So the device half is **XtensaTools 14.09 / RI-2022.9**, core config **`Xm_ncore2gp`**
(the GPSIMD "Vision-Q7" engine, internal microarch *Cairo*), little-endian, hardware
spec `NX1.1.4`. The LSP `specs` files corroborate the vintage with a Tensilica
2012 banner and `Customer ID=15949; Build=0xa0ff9` (`lsp_fll_load_cpu0/specs:2`), and
the Perl packager carries `Copyright (c) 2012-2013 by Tensilica Inc.`
(`xt-pkg-loadlib.cpl:4`). A rebuild **must** target a `ncore2gp`-equivalent core config
or the linker spec's vector-pipe / coproc options and the `-mcoproc` codegen will not
agree.

> **NOTE.** Every ELF the device half produces is **ELF32, little-endian, machine =
> `Tensilica Xtensa Processor`, type `REL`**. Confirmed by `readelf -h` on a member of
> `libc10.a`: `Class: ELF32 / Data: little endian / Type: REL / Machine: Tensilica
> Xtensa Processor`. The host driver `xt-clang++` and the Perl `xt-pkg-loadlib` are
> x86-64 ELF executables; only their *outputs* are Xtensa.

---

## 1. The pipeline at a glance

The driver's public entry is `compile()` (`build_custom_op.py:363-394`). Its body is a
strict five-phase sequence; reduced to ordered pseudocode it is exactly: `[HIGH/OBSERVED]`

```python
def compile(name, fn_names, src_files, schema, build_directory,
            includes=[], cflags=[], multicore=False, verbose=False):
    # Phase 0  — environment + tool presence (build_custom_op.py:377-382)
    assert shutil.which('xt-clang++')                      # :381

    # Phase 1  — compile every user source .cpp -> .o      (:384)
    objs = _compile_sources(src_files, cflags, includes, build_directory)

    # Phase 2  — codegen wrapper.cpp from the schema       (:385)
    wrapper = _create_wrapper(schema, fn_names, build_directory)

    # Phase 3  — compile wrapper.cpp -> wrapper.o          (:386)
    objs.append(_compile_wrapper(wrapper, cflags, build_directory))

    # Phase 4  — link objs + static libs -> <name>.so      (:388-389)
    base_name = os.path.splitext(name)[0]
    libs = _link(base_name, objs, build_directory, multicore)   # 1× or 8×

    # Phase 5  — strip + pack (GATED on '-g' not in cflags) (:391-392)
    if '-g' not in cflags:
        libs = _strip(libs, verbose)
    return libs
```

Map of phase → function → line:

| Phase | What | Function | Line |
|---|---|---|---|
| 0 | env vars, `PATH`, license, `which(CC)` | module body + `compile` | `19-50`, `377-382` |
| 1 | user sources → `.o` | `_compile_sources` → `_compile_single` | `281-294`, `262-279` |
| 2 | schema → `wrapper.cpp` | `_create_wrapper` → `_wrapper_gen` → `_parse_schema` | `202-260`, `113-200`, `53-111` |
| 3 | `wrapper.cpp` → `wrapper.o` | `_compile_wrapper` → `_compile_single` | `296-304` |
| 4 | link 1× or 8× → `.so` | `_link` → `_link_one` | `329-337`, `306-327` |
| 5 | strip + pack → `.stripped.so` / `.packed.so` | `_strip` → `_strip_one` | `360-361`, `339-358` |

> **GOTCHA.** Phase 2 (codegen) runs **after** Phase 1 (user sources). The wrapper is
> emitted only once the user objects already exist, but it is **not** consumed by them —
> the wrapper provides `get_func_cnt/get_func_name/get_func_ptr` and the per-op
> `*_stack_switch` thunks that the *runtime* (`libneuroncustomop.a`) calls. Ordering is
> therefore cosmetic for correctness but fixed by the code.

---

## 2. Environment & path setup (Phase 0)

Module load wires four env knobs and prepends the tool dir to `PATH`
(`build_custom_op.py:19-32`): `[HIGH/OBSERVED]`

```python
# license — defaulted if unset (build_custom_op.py:19-20)
LM_LICENSE_FILE ?= /opt/aws/neuron/gpsimd/tools/licenses/amzn_vq7_us_582883.out

NEURON_ROOT   = $NEURON_CUSTOM_OP  ?? /opt/aws/neuron/gpsimd/custom_op    # :24
XTENSA_CORE   = $Q7_CORE           ?? ncore2gp                           # :25
XTENSA_SYSTEM = $Q7_TOOLS          ?? .../tools/$XTENSA_CORE/config       # :26
XTENSA_BIN    = $XTENSA_TOOLS?/XtensaTools/bin  ?? /opt/aws/neuron/gpsimd/bin  # :31
PATH         += :$XTENSA_BIN                                              # :32
CC   = xt-clang++          # :36
LLIB = xt-pkg-loadlib      # :37   (the Perl packager front-end name)
```

The license default `amzn_vq7_us_582883.out` is a FlexLM checkout file; the compiler
front-end is FlexLM-gated, so a rebuild needs a valid `LM_LICENSE_FILE` even though the
*output* ELF carries no license — see [FlexLM Licensing Gate](flexlm-licensing.md).

The only hard precondition `compile()` enforces is `shutil.which(CC)` at line 381 —
if `xt-clang++` is not on `PATH`, it raises `Could not find xt-clang++`
(`build_custom_op.py:382`).

---

## 3. Compile (Phases 1 & 3): the verbatim `xt-clang++` command

All compilation — user sources and the generated wrapper — funnels through
`_compile_single` (`build_custom_op.py:262-279`). The command string is assembled at
`:269`:

```python
cmd = CC + CC_OPT + ' '.join(cflags) + INC + ' '.join(includes) + ' -o ' + obj + ' ' + src
```

with the fixed flag bundle `CC_OPT` (`:42`) and includes `INC` (`:41`):

```sh
xt-clang++ \
  -g -std=c++14 -stdlib=libc++-e \
  -fno-jump-tables -Os -Wall -Werror -Wno-c99-designator \
  -mcoproc -MMD -MP -fpic -mlongcalls -c \
  --xtensa-core=ncore2gp \
  --xtensa-system=/opt/aws/neuron/gpsimd/tools/ncore2gp/config \
  <user cflags…> \
  -I$NEURON_ROOT \
  -I$NEURON_ROOT/torch/include \
  -I$NEURON_ROOT/c10/include \
  <user includes…> \
  -o build/<name>.o  <src>.cpp
```

Flag-by-flag, why each is required for a faithful rebuild: `[HIGH/OBSERVED flags;
MED/INFERRED rationale]`

| Flag | Effect / why |
|---|---|
| `-g` | DWARF debug info. **Doubles as the strip gate** (§7): if the caller's `cflags` includes `-g`, Phase 5 is skipped. `CC_OPT` always carries `-g`, but the *gate* tests the caller's `cflags`, not `CC_OPT`. |
| `-std=c++14` | language level — the `at::Tensor` / c10 headers are built for C++14. |
| `-stdlib=libc++-e` | the **`-e` (exceptions-enabled) libc++** variant. Linked later as `-lc++-e` (§5). Mismatch here vs link → undefined `__cxa_*`. |
| `-fno-jump-tables` | no jump-table `.rodata` for switches — keeps codegen position-independent and the relocatable image flat for the prelinker's `R_XTENSA` relocator. |
| `-Os` | optimize for size; the per-core SRAM window is only **2 MiB** (§6) so size matters. |
| `-Wall -Werror -Wno-c99-designator` | warnings-as-errors, one carve-out. |
| `-mcoproc` | enable the Q7 coprocessor (vector pipe) instruction set — `HasVectorPipe=1` in the core config. Without it the SIMD intrinsics in user kernels won't assemble. |
| `-MMD -MP` | emit `.d` dependency files (build-system bookkeeping; harmless to outputs). |
| `-fpic` | position-independent code — mandatory: the device load address is per-core and resolved by the prelinker, not at link time. |
| `-mlongcalls` | relax `CALL8`/`J` into `L32R`+`CALLX8` so calls can span the full 2 MiB (or 32 MiB single-core) window without range errors. |
| `-c` | compile only; one object per source. |
| `--xtensa-core` / `--xtensa-system` | select the `ncore2gp` config + its `config/` dir. |

`_compile_sources` (`:281-294`) loops `src_files`, asserting each exists (`:288`, raises
`Cant find source file`), and returns the `.o` list. `_compile_wrapper` (`:296-304`) is a
thin wrapper that runs the **same** `_compile_single` on the generated `wrapper.cpp` with
an empty `includes` list.

> **QUIRK.** `_compile_single` runs `subprocess.run(cmd, shell=True, capture_output=True)`
> (`:272`) — the whole command is a single shell string, so paths with spaces will break.
> On failure it raises `Error compiling {src}` and only prints stdout/stderr when
> `verbose` (`:273-277`). A reimplementation should prefer an argv list; the shell form is
> the shipped behavior.

---

## 4. Codegen (Phase 2): one paragraph, then forward

Phase 2 (`_create_wrapper`, `:202-260`) parses each PyTorch schema (`_parse_schema`,
`:53-111`), emits a per-op `<fn>_wrapper()` that calls `customop_setup` →
`customop_next_*` arg pulls → the user `<fn>()` → `customop_return_tensor` →
`customop_cleanup`, wraps it in a `<fn>_stack_switch()` thunk that calls
`switch_stack_or_call_wrapper((uint32_t)<fn>_wrapper)` (`:196-198`), and finally emits the
dispatch trio `get_func_cnt` / `get_func_name` / `get_func_ptr` (`:237-252`). The wrapper
text is written to `build_directory/wrapper.cpp` (`:255-258`). The full character-level
breakdown — schema grammar, the 32-char name limit (`:222-223`), the `switchBack` asm
tail (`:192-193`) — lives on [`build_custom_op.py` Codegen](build-custom-op-codegen.md).
For this page the only thing that matters downstream is: **the wrapper compiles to one
more `.o` that joins `objs` before link.** `[HIGH/OBSERVED]`

---

## 5. Link (Phase 4): single vs 8× per-core matrix

`_link` (`:329-337`) branches on `multicore`:

```python
def _link(name, objs, build_directory, multicore):
    if multicore:
        # one link per core; output names are name_cpu0.so … name_cpu7.so
        return [_link_one(f'{name}_cpu{i}', objs, build_directory, MULTI_Q7_LIBS[i])
                for i in range(8)]            # NUM_CPUS = 8  (build_custom_op.py:47)
    else:
        return [_link_one(name, objs, build_directory, LIBS_SINGLE)]
```

`_link_one` (`:306-327`) builds the link command at `:314`:

```python
cmd = CC + LINK_OPT + INC + ' -o ' + lib + ' ' + ' '.join(objs) + linking_libs
```

`LINK_OPT` (`:43`) is `CC_OPT` minus the compile-only bits — note **no `-c`, no
`-fpic`, no `-MMD/-MP`**, but it keeps `-g -std=c++14 -stdlib=libc++-e -fno-jump-tables
-Os -Wall -Werror -Wno-c99-designator -mcoproc --xtensa-core/--xtensa-system`. The
per-target library string (`linking_libs`) is where single and multicore diverge:

**Single-core** (`LIBS_SINGLE`, `:45`):

```sh
xt-clang++ <LINK_OPT> -I… -o build/<name>.so  <all .o>  \
   $NEURON_ROOT/neuron/libneuroncustomop.a \
   $NEURON_ROOT/c10/lib/libc10.a \
   -lloader \
   -mlsp=$NEURON_ROOT/lsp_fll_load_cpus/lsp_fll_load_cpu_single \
   -lxmem -lhal -lc++-e -lm -lgcc \
   $NEURON_ROOT/neuron/libcweak.a
```

**Multicore** (`MULTI_Q7_LIBS[i]`, built in the loop `:48-50`): identical except
`-mlsp=…/lsp_fll_load_cpu{i}` for `i` in `0..7`, and the output base is `<name>_cpu{i}`.

The library-order contract a reimplementation must reproduce exactly: `[HIGH/OBSERVED
ordering; MED/INFERRED roles]`

| Token | Kind | Role |
|---|---|---|
| `<all .o>` | objects | user kernel objects + `wrapper.o` |
| `libneuroncustomop.a` | static | defines `customop_*`, `switchBack`, `switch_stack_or_call_wrapper`; **references** the wrapper's `get_func_*` |
| `libc10.a` | static | the retargeted c10 / `at::Tensor` core |
| `-lloader` | `.a` on lsp path | device loader runtime (`libloader.a`) |
| `-mlsp=…lsp_fll_load_cpu{i\|_single}` | **linker spec package** | selects the per-core `ldscripts/elf32xtensa.x` + `specs` (the org address + section map) |
| `-lxmem` | `.a` | device memory helpers |
| `-lhal` | `.a` | Xtensa HAL |
| `-lc++-e` | `.a` | exceptions-enabled libc++ runtime (matches `-stdlib=libc++-e`) |
| `-lm -lgcc` | `.a` | math + compiler runtime |
| `libcweak.a` | static, **last** | weak C-library fallbacks; placed last so strong defs win |

> **GOTCHA.** `-mlsp=` is a **path to a directory**, not a file: the linker driver reads
> `<dir>/specs` (gcc spec strings) and `<dir>/ldscripts/elf32xtensa.x` (the section
> script). The directory name encodes the core: `lsp_fll_load_cpu0 … cpu7` for SPMD,
> `lsp_fll_load_cpu_single` for one fat core. Get the `-mlsp` wrong and the image links
> to the wrong SRAM window — see §6.

> **QUIRK.** The output naming convention is a hard contract with the consumer. The
> comment at `:334` states it: *"Compiler assumes that multi-core library is in format
> `xxx_cpuX.so`."* So `_link_one`'s `name` argument is `<base>_cpu{i}` and `_strip` then
> derives `<base>_cpu{i}.stripped.so` / `.packed.so`. The downstream loader keys off the
> `_cpuX` suffix to map each image to its core.

---

## 6. Per-core LSPs: the 8× memory matrix

Each `-mlsp` dir is a complete linker spec. The two files that matter:

**`specs`** — identical across all nine dirs (verified `diff -q cpu0 vs _single` →
identical), a minimal gcc spec stub (`lsp_fll_load_cpu0/specs:25-35`): `[HIGH/OBSERVED]`

```
*startfile:  crti%O%s crtbegin%O%s
*endfile:    crtend%O%s crtn%O%s
*lib:
```

i.e. CRT begin/end glue, no extra default libs (the libs come from the explicit `-l…`
list in §5).

**`ldscripts/elf32xtensa.x`** — the section script. Five variants ship per dir
(`.x` default, `.xbn`, `.xn`, `.xr` relocatable, `.xu`); the link uses `.x`. Its
`MEMORY` block is the per-core difference (`cpu0 elf32xtensa.x:3-7`):

```ld
MEMORY {
  iram0_0_seg : org = 0x00000000, len = 0x1000      /* 4 KiB vectors */
  sram0_0_seg : org = 0x84000000, len = 0x200000    /* cpu0: 2 MiB SRAM window */
}
```

The SRAM `org` walks by `0x200000` (2 MiB) per core, confirmed by reading all nine
scripts:

| LSP dir | `sram0_0_seg` org | len | `_memmap_mem_sram0_end` |
|---|---|---|---|
| `lsp_fll_load_cpu0` | `0x84000000` | `0x200000` | `0x84200000` |
| `lsp_fll_load_cpu1` | `0x84200000` | `0x200000` | `0x84400000` |
| `lsp_fll_load_cpu2` | `0x84400000` | `0x200000` | `0x84600000` |
| `lsp_fll_load_cpu3` | `0x84600000` | `0x200000` | `0x84800000` |
| … | … | … | … |
| `lsp_fll_load_cpu7` | `0x84E00000` | `0x200000` | `0x85000000` |
| `lsp_fll_load_cpu_single` | `0x84000000` | **`0x2000000`** | `0x86000000` |

So the rule is **`org(i) = 0x84000000 + i·0x200000`** for the 8 SPMD cores, each owning a
disjoint 2 MiB window, and the single-core LSP collapses all eight windows into **one
32 MiB region** (`0x2000000`) at `0x84000000`. The `iram0_0_seg` (4 KiB at `0x0`) holds
the dispatch/reset/handler vectors and is identical across cores.

> **NOTE.** This is why `-fpic` + `-mlongcalls` are mandatory in §3: the same `objs` link
> eight times to eight different base addresses. The codegen is base-agnostic; the LSP
> supplies the address; the prelinker re-bases at load. `[MED/INFERRED]`

The SRAM section order the script imposes (`cpu0 elf32xtensa.x:94-309`) — a faithful
rebuild's image layout: `.text` → `.clib.rodata` → `.rtos.rodata` → `.clib.data` →
`.eh_frame` → `.ctors`/`.dtors` → `.rodata` (which folds in the C++ ctor/dtor tables,
`__XT_EXCEPTION_*`, the `.eh_frame`, and the `_bss_table` `LONG(_bss_start)/LONG(_bss_end)`
pair at `:195-198`) → `.clib.text` → `.rtos.text` → `.clib.percpu.data` /
`.rtos.percpu.data` → `.rtos.data` → `.interp` → `.data` → `.note.gnu.build-id` (`:275`)
→ `.bss` (between `_bss_start`/`_bss_end`, `:286-307`). The `_bss_table` LONG pair is the
runtime's BSS-clear descriptor.

The single user-facing knob in this region is the **stack size**, defaulted in
`stack_switch.hpp:15-19`:

```c
#ifndef STACK_SIZE
   #define STACK_SIZE 4196          // ~4 KiB default if the customer specifies none
#endif
static_assert(STACK_SIZE <= MAX_STACK_SIZE, "…");  // MAX_STACK_SIZE = 0x400000 (4 MiB)
```

`switch_stack_or_call_wrapper(wrapper_fn_ptr, STACK_SIZE)` decides at runtime whether the
4 KiB default is too small and an HBM stack switch (`allocate_hbm_stack`) is needed; the
default `4196` is deliberately conservative so most ops never switch.

---

## 7. Strip + Package (Phase 5): the gate and the packager

Phase 5 is **conditional** (`build_custom_op.py:391-392`):

```python
if '-g' not in cflags:          # the caller's cflags, NOT CC_OPT
    libs = _strip(libs, verbose)
```

> **CORRECTION / GATE.** The gate tests the **caller-supplied `cflags`**, not the fixed
> `CC_OPT` (which always has `-g`). If the host build passes `-g` in `cflags` for a debug
> build, **strip+pack is skipped entirely** and `compile()` returns the un-stripped
> `.so`. For a shippable artifact the caller must omit `-g` from `cflags`. The
> always-present `-g` in `CC_OPT` only affects the object DWARF, not the gate. A
> reimplementation that hardcodes "always strip" will diverge on debug builds.

`_strip_one` (`:339-358`) is misnamed — it runs the **packager** `xt-pkg-loadlib`, which
both strips and packs. Command (`:349`):

```python
cmd = LLIB + STRIP_OPT + ' -o ' + pack + ' -s ' + strip + ' ' + lib
```

with `STRIP_OPT` (`:44`) `= ' -e lib_func --xtensa-core=ncore2gp --xtensa-system=… '`,
expanding to:

```sh
xt-pkg-loadlib \
   -e lib_func \
   --xtensa-core=ncore2gp --xtensa-system=.../ncore2gp/config \
   -o build/<name>.packed.so \
   -s build/<name>.stripped.so \
   build/<name>.so
```

Outputs are derived from the linked lib's base (`:345-347`):
`<base>.packed.so` (`-o`, the device load-lib) and `<base>.stripped.so` (`-s`, the saved
intermediate). The `-e lib_func` names the **exported section symbol** placed at the head
of the packed blob's `.rodata` (see below).

### 7.1 What `xt-pkg-loadlib.cpl` actually does

The packager is a 288-line Perl script (`xt-pkg-loadlib.cpl`). It option-parses
`-o/-s/-e/-v/-p/-l` plus the `--xtensa-*core/params/system` triple (`:47-63`), and
forwards `--xtensa-core` as **both** host and target core (`:72-81`). Decoded to ordered
pseudocode (`:130-288`):

```sh
# inputs:  $inputlib = <name>.so      (the PIE linked image)
#          -e $symname (= lib_func)   -o $outfilename (.packed.so)  -s $saveStrippedName (.stripped.so)
#          --xtensa-core=ncore2gp  → forwarded as both host & target core (:72-81)
# (no -p given → $pagesize undefined → "overlay" path, not the PIE-pagesize path)

# 1. STRIP nonloadable sections                                      (:152-161)
xt-strip --xtensa-core=ncore2gp  $inputlib -o $strippedlib

# 2. enumerate sections; drop fixed xtensa/empty sections            (:163-196)
xt-objdump --xtensa-core=ncore2gp -h $strippedlib            # read section headers
emptysections = [ .xtensa.info .xt.lit .xt.prop .interp .got.loc .got ]
if no -l (symlookup): emptysections += [ .hash .dynstr .dynsym .rela.plt ]   # :175-180
for each section whose size == 00000000:  emptysections += --remove-section=<name>  # :182-187
xt-objcopy --xtensa-core=ncore2gp  $strippedlib $pagedlib  <emptysections>   # :193-196

# 3. SAVE the stripped/cleaned image to -s                           (:198-201)
xt-objcopy --xtensa-core=ncore2gp  $pagedlib  $saveStrippedName     # <name>.stripped.so

# 4. wrap the cleaned image as a RELOCATABLE host object             (:206-215)
xt-as       <host-core>  -o $emptytemp  <empty>           # build an empty .o
xt-objcopy  <host-core>  $emptytemp $flattenedlib \
            --add-section  libdata=$pagedlib \            # embed the device image as a blob
            --set-section-flags libdata=readonly,alloc

# 5. write a tiny linker script that names the blob's start symbol   (:242-275)
#    SECTIONS { .rodata : {  lib_func = . ;  *(libdata) } }
xt-as  <host-core>  -o $ofile  ".section libdata,\"a\"\n.align 4\n"   # alignment shim

# 6. final relocatable link: emit the packed load-lib               (:277-280)
xt-ld  <host-core>  -r -T $linkerscript  $ofile $flattenedlib  -o $outfilename  # .packed.so

# 7. clean up all temp files                                         (:282-288)
```

So the **`.packed.so` is a relocatable ELF object** whose `.rodata` contains, under the
exported symbol `lib_func`, the *cleaned, stripped, page-trimmed device image* embedded
as a single `libdata` section. It is **not** a normal shared object — it is a *load-lib*:
a host-linkable container carrying the device blob plus a named entry symbol. The
prelinker later pulls `libdata` out by `lib_func`, parses it, relocates it to the per-core
window, and emits the UCPL header. `[HIGH/OBSERVED packager; CARRIED prelinker handoff
from RT-19]`

> **NOTE.** Because no `-p`/`--shared-pagesize` is passed by `STRIP_OPT`, the script takes
> the **overlay** branch, not the PIE-pagesize branch (`:130-132`, `:154-161`,
> `:189-196`, `:221-271`). The overlay path is what removes `.hash/.dynstr/.dynsym/.rela.plt`
> (since `-l`/symlookup is also absent, `:175-180`) and embeds via `--add-section libdata=`.
> A `-p` value would instead keep PIE pagesize metadata and a different symbol-export path.
> The shipped build is overlay.

> **GOTCHA.** Step 2 removes **`.got` and `.got.loc`** unconditionally (`:171-172`). The
> device image therefore has no GOT — all cross-references are resolved by the prelinker's
> `R_XTENSA` relocator at load, which is exactly why §3 forbids jump tables and demands
> PIC. A rebuild that leaves a non-empty GOT in the image will produce a `.packed.so` the
> prelinker cannot fully relocate. `[MED/INFERRED]`

---

## 8. Artifact triple & the host-side handoff

For a single-core build of `relu.cpp`, `compile()` produces three files in
`build_directory` (`_strip_one:345-347`, `_link_one:312`): `[HIGH/OBSERVED]`

```
relu.so            # Phase 4 — the linked PIE device image (per LSP window)
relu.stripped.so   # Phase 5 -s — stripped/cleaned device image (no symtab, no GOT)
relu.packed.so     # Phase 5 -o — RELOCATABLE load-lib: libdata blob under `lib_func`
```

A multicore build produces the triple **eight times**, named
`relu_cpu0.{so,stripped.so,packed.so}` … `relu_cpu7.*`. The `_cpuX` suffix is the
contract from §5's `:334` comment; the consumer demuxes images to cores by that suffix.

The **`.packed.so` is the deliverable** that crosses from this build flow into the
runtime. The on-device prelinker (host-resident, in `libnrtucode.so`; entry chain
`prelink.c` → `prelink_load.c` → `prelink_relocate.c`) takes the packed load-lib,
extracts `libdata` by the `lib_func` symbol, validates it, parses its segments, loads
each segment, runs the `R_XTENSA` relocations to bind the image to the chosen per-core
SRAM window (the `0x84000000 + i·0x200000` base from §6), and emits the **UCPL header**
that makes the image device-stageable. That prelinker stage is documented at
[The Host Prelinker — UCPL / Segment Loader / R_XTENSA / Staging](../runtime/prelinker-ucpl.md).
`[CARRIED from RT-19]`

```
USER .cpp ──► [xt-clang++ -c] ──► .o ─┐
                                       ├─► [xt-clang++ -mlsp=cpuI] ──► .so
schema  ──► [wrapper.cpp gen] ─► .o ──┘                 │
                                                        ▼
                              [xt-pkg-loadlib: strip+objcopy+ld -r]
                                                        │
                                                        ▼
                                       relu[_cpuI].packed.so  (load-lib)
                                                        │
                              ┌─────────────────────────┘  HOST/DEVICE BOUNDARY
                              ▼
            [prelinker in libnrtucode.so: validate→parse→load→R_XTENSA→UCPL header]
                              │
                              ▼
                     device-stageable image, based at per-core SRAM window
```

---

## 9. Reimplementation checklist

To rebuild a Vision-Q7-compatible custom op from a `.cpp` without this driver: `[MED/INFERRED
from the verbatim pipeline above]`

1. **Toolchain:** XtensaTools **14.09 / RI-2022.9**, core config **`Xm_ncore2gp`**
   (little-endian, vector pipe on). Valid `LM_LICENSE_FILE` for the compiler front-end.
2. **Compile** each source and the generated wrapper with the exact `CC_OPT` bundle
   (`-Os -fpic -mlongcalls -mcoproc -stdlib=libc++-e -fno-jump-tables --xtensa-core=ncore2gp`).
   Keep `-g` out of the *shippable* `cflags` so strip runs.
3. **Codegen** the wrapper (`get_func_cnt/name/ptr` + per-op `*_stack_switch` thunks) per
   [the codegen page](build-custom-op-codegen.md); compile it into the object set.
4. **Link** with the precise library order (`libneuroncustomop.a libc10.a -lloader
   -mlsp=<lsp> -lxmem -lhal -lc++-e -lm -lgcc libcweak.a`); pick `lsp_fll_load_cpu_single`
   for one fat core (32 MiB @ `0x84000000`) or loop `lsp_fll_load_cpu0..7`
   (2 MiB each, `org = 0x84000000 + i·0x200000`). Name multicore outputs `<name>_cpu{i}`.
5. **Pack** each `.so` through the `xt-pkg-loadlib` recipe: `xt-strip` → drop
   `.got/.got.loc/.xt.*/.interp` and empty/`.dynsym` sections → embed the cleaned image
   as `libdata` in a relocatable object → `xt-ld -r` with `SECTIONS { .rodata :
   { lib_func = .; *(libdata) } }`. Output `<name>.packed.so`.
6. **Hand off** the `.packed.so` to the prelinker, which re-bases to the per-core window
   and emits the UCPL header.

---

## Cross-references

- [`build_custom_op.py` Codegen](build-custom-op-codegen.md) — the wrapper text this flow compiles.
- [LSP Linker Specs + ELF Layout](lsp-elf.md) — the `-mlsp` packages and section script in depth.
- [The Multicore API (8-core SPMD)](multicore-spmd.md) — the 8-core model behind the 8× link.
- [FlexLM Licensing Gate](flexlm-licensing.md) — the compiler-front-end license checkout.
- [Stack-Switch Dispatch](stack-switch.md) — `switch_stack_or_call_wrapper` / `STACK_SIZE` / `switchBack`.
- [Prelinker + UCPL](../runtime/prelinker-ucpl.md) — the device-side loader that consumes `.packed.so`.
