# Toolchain Inventory & Versions

Reproducing anything in this reference requires pinning two distinct toolchains, and they
are not the same toolchain. The **vendor build toolchain** is the Cadence/Tensilica Xtensa
cross-compiler (`xt-clang++`, a Clang 10.0.1 fork, plus a GNU Binutils 2.34 fork) that turns
C++ custom-op source into the ELF32-Xtensa device objects that run on the Vision-Q7 "Cairo"
core. The **analysis toolchain** is the host-x86 tooling this wiki was actually produced with
— most importantly the *same* `xtensa-elf-objdump` shipped in the SDK, run on the host with
`--xtensa-core=ncore2gp`, which is the only correct way to disassemble Q7 device code (see
[FLIX Bundle-Decoding Methodology](flix-decoding.md)).

Every version string below was read directly out of a shipped file: the tool's own
`--version` banner, a string embedded in the binary, the canonical `*-params` configuration
file, the device object `.comment` section, the `.deb` control metadata, or the FlexLM
license artifacts. Confidence tags follow [the Confidence & Walls Model](confidence-model.md):
a string read verbatim from a binary is `[OBSERVED]`; a value derived from tool behavior is
`[INFERRED]`. Where prompt-level folklore disagrees with the binary, **the binary wins**.

All paths are relative to the extraction root
`neuronx-gpsimd/extracted/nested/gpsimd_tools_tgz/tools/` (call it `$T`) for the toolchain
tarball, or to the two `.deb` extraction roots for the SDK packages. The toolchain tarball's
on-device install root is `/opt/aws/neuron/gpsimd/tools/`.

---

## The two version axes: hardware "RI" ≠ software "RI"

The biggest trap in this toolchain is that "RI-20xx" is overloaded: Cadence stamps a
*hardware* micro-architecture release **and** a *software-tools* release with the same
`RI_YYYY_N` naming scheme, and for this core the two land on different years. The decoder
is shipped: `$T/XtensaTools/include/xtensa-versions.h` (byte-identical copy at
`$T/XtensaTools/xtensa-elf/include/xtensa/xtensa-versions.h`):

```c
#define XTENSA_HWVERSION_RI_2020_4   281040   /* versions NX1.1.4, LX7.1.4 */   // line 317
#define XTENSA_HWVERSION_RI_2022_9   281090   /* versions NX1.1.9, LX7.1.9 */   // line 338
#define XTENSA_SWVERSION_RI_2020_4   1404000  /* versions 14.04 */              // line 430
#define XTENSA_SWVERSION_RI_2022_9   1409000  /* versions 14.09 */              // line 437
#define XTENSA_SWVERSION_14_09       XTENSA_SWVERSION_RI_2022_9                 // line 510
#define XTENSA_SWVERSION             XTENSA_SWVERSION_RI_2022_9                 // line 519
```

Reading `ncore2gp-params` against this header pins both axes precisely:

| Axis | Param key (value) | Maps to header symbol | "RI" label | Confidence |
|---|---|---|---|---|
| **Hardware** | `HWMicroArchLatest = 281040`, `TargetHWVersion = NX1.1.4` | `XTENSA_HWVERSION_RI_2020_4 = 281040` | hardware = **RI-2020.4** | `[HIGH/OBSERVED]` |
| **Software tools** | `SWToolsVersion = 1409000`, `SWToolsVername = 14.09`, `SWToolsRelease = RI-2022.9` | `XTENSA_SWVERSION_RI_2022_9 = 1409000` | tools = **RI-2022.9** | `[HIGH/OBSERVED]` |

> **Note — NX1.1.4 ≡ LX7.1.4 ≡ HW RI-2020.4; tools are RI-2022.9.** `versions.h:317`
> defines, verbatim, that HW number `281040` *is* `NX1.1.4` *is* `LX7.1.4` *is* the
> `RI_2020_4` **hardware** symbol. The core in this corpus carries exactly that number
> (`HWMicroArchLatest = 281040`), so all three labels are correct names for the same
> hardware: NX is the family name, LX7 the legacy alias, RI-2020.4 the hardware release.
> The **software tools** that build for it are a *later* release: `14.09 = RI-2022.9 =
> 1409000` (`versions.h:437,510,519`). So a statement like "the hardware is RI-2020.4" and a
> statement like "the toolchain is RI-2022.9" are *both* true and *not* contradictory — they
> name different axes. Do not collapse them: `XTENSA_SWVERSION_RI_2020_4` is `14.04`
> (`1404000`), a different tools release that this core does **not** ship.

---

## The single source of truth: `ncore2gp-params`

Almost every identity claim resolves to one auto-generated config file, present in two
byte-identical copies:

- `$T/XtensaTools/config/ncore2gp-params`
- `$T/ncore2gp/config/ncore2gp-params`

This is the file the Xtensa registry indexes on; with no other core registered, `ncore2gp`
is the only Xtensa core these tools will load. Its header carries the customer/build stamp,
and its body carries the release and hardware identity:

```
# Customer ID=19270; Build=0xc23fe; Copyright (c) 2004-2018 Tensilica Inc.  ALL RIGHTS RESERVED.
SWToolsRelease = RI-2022.9
SWToolsVername = 14.09
SWToolsVersion = 1409000
HWMicroArchLatest = 281040
HWMicroArchEarliest = 281040
TargetHWVersion = NX1.1.4
ConfigName = Xm_ncore2gp
arch = Xtensa24
uarchName = Cairo
HWConfigID0 = 0xC4019686
HWConfigID1 = 0x2908E4E3
BuildUniqueID = 795646
BuildMode = Evaluation
IsaUseCoprocessor = 1
IsaCoprocessorCount = 7
SW_ABI = windowed
SW_FloatingPointABI = 1
```

| Key | Value | Meaning | Confidence |
|---|---|---|---|
| `ConfigName` | `Xm_ncore2gp` | Customer config name (`m` = managed/multicore variant) | `[HIGH/OBSERVED]` |
| `arch` | `Xtensa24` | ISA architecture family (the XEA3-era 24-bit-PC Xtensa) | `[HIGH/OBSERVED]` |
| `uarchName` | `Cairo` | Tensilica micro-architecture codename for this NX core | `[HIGH/OBSERVED]` |
| `HWConfigID0` | `0xC4019686` | Config ID word 0 — burned into the ELF, checked by the loader | `[HIGH/OBSERVED]` |
| `HWConfigID1` | `0x2908E4E3` | Config ID word 1 | `[HIGH/OBSERVED]` |
| `Customer ID` | `19270` | TPG customer (Amazon) | `[HIGH/OBSERVED]` |
| `Build` | `0xc23fe` (`795646`) | TPG build stamp = `BuildUniqueID` | `[HIGH/OBSERVED]` |
| `BuildMode` | `Evaluation` | This config was generated under an evaluation license | `[HIGH/OBSERVED]` |
| `IsaCoprocessorCount` | `7` | Seven coprocessors — the `-mcoproc` target | `[HIGH/OBSERVED]` |
| `SW_ABI` | `windowed` | Windowed register ABI (call0 not used) | `[HIGH/OBSERVED]` |

`ConfigID 0xC4019686 / 0x2908E4E3` is the same word the loader validates against the running
core, so it is the most operationally meaningful identity in the file.

---

## Part A — The vendor build toolchain (ELF32-Xtensa target)

To recompile a custom op into the exact device objects the runtime loads you need an
`RI-2022.9` Xtensa Tools release configured for the `ncore2gp` core. The objects it produces
are confirmed `ELF 32-bit LSB relocatable, Tensilica Xtensa` (read from a member of
`libneuroncustomop.a`), and each carries the provenance stamp in its `.comment` section:

```
$ readelf -p .comment stack_switch.o      # member of libneuroncustomop.a
  [     1]  XtensaTools-14.09 clang version 10.0.1
```

That single string ties the device objects to **XtensaTools 14.09 (RI-2022.9)** and
**clang 10.0.1** — the strongest provenance in the corpus.

### A.1 The Clang/LLVM compiler — `xt-clang` / `xt-clang++`

`$T/XtensaTools/bin/xt-clang` and `xt-clang++` are thin 39 KB ELF wrappers, *not* the
compiler; the embedded string `%s/llvm/bin/clang` shows they exec the real driver under
`$T/XtensaTools/llvm/bin/`, where `clang → clang-10` and `clang++ → clang`. The wrapper sets
`TENSILICA_LLVM_TARGET_TRIPLE` and loads the per-core code-generator plugin.

| Field | Value | Where read | Confidence |
|---|---|---|---|
| Driver | `xt-clang` / `xt-clang++` (wrapper) → `clang-10` | `clang` symlink → `clang-10` | `[HIGH/OBSERVED]` |
| Clang version | **clang 10.0.1** | device object `.comment`; `strings libclangBasic.so.10` → `"Clang 10.0.1 "` | `[HIGH/OBSERVED]` |
| LLVM SONAME | `.so.10` (LLVM 10) | `ls $T/XtensaTools/llvm/lib/*.so.10` | `[HIGH/OBSERVED]` |
| Build tree | `RI-2022.9` | `strings clang-10` → `/home/xpgcust/tree/RI-2022.9/ib/p4root/Xtensa/Software/llvm/...` | `[HIGH/OBSERVED]` |
| Code-gen plugin | `$T/ncore2gp/config/llvm/lib/libXtensaCodeGen.so` (`SONAME libLLVMXtensaCodeGen.so.10`) | loaded at driver start; per-config | `[HIGH/OBSERVED]` |

`libXtensaCodeGen.so` is what makes this Clang a Q7 backend; it is loaded with an executable
stack, so on a hardened host you must allow that (otherwise the driver aborts with
`cannot enable executable stack as shared object requires`). The version banner in
`libclangBasic.so.10` is the stock Clang 10.0.1 string — the Tensilica delta is entirely in
the loaded plugin and the `ncore2gp-params` ISA description, not in the version number.

### A.2 The Binutils — `xtensa-elf-*`

A full GNU Binutils fork ships under `$T/XtensaTools/bin/` (host x86-64 ELF). All print the
identical banner once a core is in scope. They refuse to run with no default core registered
(the error lists `ncore2gp` as the only available core); pass `--xtensa-core=ncore2gp` or set
`XTENSA_CORE=ncore2gp` and `XTENSA_SYSTEM=$T/XtensaTools/config`.

```
$ xtensa-elf-objdump --xtensa-core=ncore2gp --version
GNU objdump (GNU Binutils) 2.34.20200201 Xtensa Tools 14.09
Copyright (C) 2020 Free Software Foundation, Inc.
```

| Tool | Banner | Use |
|---|---|---|
| `xtensa-elf-objdump` | `GNU Binutils 2.34.20200201 Xtensa Tools 14.09` | Disassemble Q7 objects/firmware (target `elf32-xtensa-le`) |
| `xtensa-elf-as` | same | Assemble `.S` |
| `xtensa-elf-ld` | same | Link against an LSP |
| `xtensa-elf-nm` | same | Symbol tables |
| `xtensa-elf-readelf` | same | ELF headers (runs with **no** core registered) |
| `xtensa-elf-addr2line` | same | Source line lookup |
| `xtensa-elf-objcopy` / `-strip` / `-ar` / `-ranlib` / `-c++filt` / `-size` / `-strings` / `-gprof` | same | Standard binutils roles |

`Binutils 2.34.20200201 Xtensa Tools 14.09` is `[HIGH/OBSERVED]` — from every tool's
`--version` banner. Target object format `elf32-xtensa-le` (`objdump -i`:
`header little endian, data little endian`).

### A.3 The compile/link recipe — `build_custom_op.py`

The exact invocation is the shipped build script
`opt/aws/neuron/gpsimd/script/build_custom_op.py` (customop-lib package). Verbatim lines:

```python
if 'LM_LICENSE_FILE' not in os.environ:
    os.environ['LM_LICENSE_FILE'] = '/opt/aws/neuron/gpsimd/tools/licenses/amzn_vq7_us_582883.out'

NEURON_ROOT  = os.environ.get('NEURON_CUSTOM_OP', '/opt/aws/neuron/gpsimd/custom_op')
XTENSA_CORE  = os.environ.get('Q7_CORE',  'ncore2gp')
XTENSA_SYSTEM= os.environ.get('Q7_TOOLS', f'/opt/aws/neuron/gpsimd/tools/{XTENSA_CORE}/config')
CC='xt-clang++'

CC_OPT  = ' -g -std=c++14 -stdlib=libc++-e -fno-jump-tables -Os -Wall -Werror '
          '-Wno-c99-designator -mcoproc -MMD -MP -fpic -mlongcalls -c '
          '--xtensa-core=ncore2gp --xtensa-system=... '

LINK_OPT= CC_OPT minus { -MMD -MP -fpic -mlongcalls -c }

LIBS_SINGLE = ' .../neuron/libneuroncustomop.a .../c10/lib/libc10.a -lloader '
              '-mlsp=.../lsp_fll_load_cpus/lsp_fll_load_cpu_single '
              '-lxmem -lhal -lc++-e -lm -lgcc .../neuron/libcweak.a'
```

| Flag | Effect | Confidence |
|---|---|---|
| `-std=c++14` | The custom-op ABI is fixed at C++14 | `[HIGH/OBSERVED]` |
| `-stdlib=libc++-e` | Link the *embedded / exceptions-disabled* libc++ (`libc++-e.a`) | `[HIGH/OBSERVED]` |
| `-mcoproc` | Enable the 7 coprocessors (`IsaCoprocessorCount = 7`); required for the vector pipe | `[HIGH/OBSERVED]` |
| `-mlongcalls` | Far calls relaxed to L32R relays — code can sit anywhere in the 1 GB SRAM window | `[HIGH/OBSERVED]` |
| `-fno-jump-tables` | Avoid `.rodata` jump tables (placement/relocation constraints) | `[HIGH/OBSERVED]` |
| `-Os` | Optimize for size — IRAM/DRAM are 64 KB each | `[HIGH/OBSERVED]` |
| `-fpic` | Position-independent device code | `[HIGH/OBSERVED]` |
| `-mlsp=<lsp>` | Select the linker support package (memory map) — see A.4 | `[HIGH/OBSERVED]` |

The core is selected by `--xtensa-core=ncore2gp` / `--xtensa-system=<sys>`, **not** by a
`-target` triple. The flags `-fno-exceptions` and `-mtext-section-literals` do **not** appear
in this recipe; exceptions are excluded by linking `libc++-e` rather than by a compile flag.
See [build_custom_op.py Codegen](../abi/build-custom-op-codegen.md) and
[Build → Compile → Link → Strip → Package Flow](../abi/build-flow.md).

The final package strip/pack step uses `xt-pkg-loadlib -e lib_func --xtensa-core=ncore2gp
--xtensa-system=<sys> -o <base>.packed.so -s <base>.stripped.so <lib>` (`STRIP_OPT` carries
`-e lib_func`, the exported entry symbol).

### A.4 LSP linker support packages

The build links a per-CPU LSP, not a single linker script. The customop-lib package ships
eleven under `custom_op/lsp_fll_load_cpus/`:

```
lsp_fll_load_cpu0 … lsp_fll_load_cpu7      # one per SPMD core (8-core multicore)
lsp_fll_load_cpu_single                    # single-core build
```

Single-core links `-mlsp=…/lsp_fll_load_cpu_single`; multicore links the matching
`lsp_fll_load_cpu{0..7}` per core. The vendor toolchain also ships stock LSPs under
`$T/ncore2gp/xtensa-elf/lib/` (`min-rt`, `app-sim`, `gdbio`, `min-rt-mc`, …); each is an
`.specs` file + `memmap.xmm` + a generated `ldscripts/elf32xtensa.x`. The `specs` file carries
its own Cadence stamp:

```
# Customer ID=19270; Build=0xc23fe; Copyright (c) 2001-2015 Cadence Design Systems, Inc.
```

(Customer ID `19270` and build `0xc23fe` match the params header; the copyright reads
"Cadence Design Systems" in the LSP `specs` and "Tensilica Inc." in the `*-params` header —
both are genuine binary stamps of different vintage.) The `min-rt` `memmap.xmm` build-time
memory model:

| Region | Base | Size | Attributes |
|---|---|---|---|
| `iram0` | `0x0` | `0x10000` (64 KB) | executable, writable; reset/dispatch vectors + `.iram0.text` |
| `dram0` | `0x80000` | `0x10000` (64 KB) | writable; `.dram0.{rodata,data,bss}` |
| `sram` | `0x100000` | `0x40000000` (1 GB) | executable, writable; STACK, HEAP, all `.text`/`.data`/`.bss`/`.literal` |

See [LSP Linker Specs + ELF Layout](../abi/lsp-elf.md) and
[The LSP SRAM Window Map](../control/address/lsp-sram-window-map.md).

### A.5 Device runtime libraries

| Library | Path | Role |
|---|---|---|
| `libc++-e.a` | `$T/ncore2gp/xtensa-elf/lib/` | LLVM libc++ (exceptions-disabled `-e` variant), 2.0 MB |
| `libc++abi-e.a` | `$T/ncore2gp/xtensa-elf/lib/` | libc++abi (`-e` variant) |
| `libhal.a` | `$T/ncore2gp/xtensa-elf/arch/lib/` | Hardware abstraction layer |
| `libsim.a`, `libminrt.a`, `libtinyrt.a`, `libxos.a` | `$T/ncore2gp/xtensa-elf/arch/lib/` | Runtimes / RTOS |
| `libxmem` (`-lxmem`) | SDK `custom_op` tree | GPSIMD memory/DMA helpers |
| `libloader` (`-lloader`) | SDK `custom_op` tree | Q7 ELF loader runtime |
| `libneuroncustomop.a`, `libc10.a`, `libcweak.a` | SDK `custom_op/{neuron,c10}/lib/` | Custom-op ABI + retargeted c10 + weak C symbols |
| `-lm -lgcc` | toolchain | math + compiler runtime |

### A.6 The FlexLM licensing gate

> **Callout — the Xtensa tools are FlexLM-gated; the compiler path is *not*.** Licensing
> goes through FlexNet (FlexLM). The shipped artifacts make the gate exact:
>
> - **Client library:** `$T/XtensaTools/Tools/lib/tenlp.so` — Tensilica's FlexLM client.
>   Its embedded banner: `@(#) FlexNet Licensing v11.15.1.0 build 225974 (ipv6) x64_lsb
>   (liblmgr.a), Copyright (c) 1988-2018 Flexera.` It is a license **client only** — no
>   `lmgrd`/`lmutil` daemon ships.
> - **What actually checks out a license:** the ISS (`libsimxtcore`), the legacy XCC/TIE
>   extension engine (`extend.so`), and the TIE compiler (`tcgen`). The thin `xt-*` driver
>   shells and the **Clang/LLVM-10 compiler path are ungated** — you can compile without a
>   live checkout, but the ISS oracle and TIE flow require one.
> - **License file:** `build_custom_op.py` forces `LM_LICENSE_FILE`, when unset, to
>   `/opt/aws/neuron/gpsimd/tools/licenses/amzn_vq7_us_582883.out` — and that file **ships**
>   in the tools `.deb` at exactly that path. The name decodes as *Amazon Vision-Q7, US,
>   license #582883*. Its two `INCREMENT` lines, vendor daemon `xtensad`, version `14.0`,
>   expiry `13-jul-2031`, `uncounted` (node-locked):
>   ```
>   INCREMENT XT_XCC_TIE_ED097265 xtensad 14.0 13-jul-2031 uncounted ...
>   INCREMENT XT_XPLORER_SE       xtensad 14.0 13-jul-2031 uncounted ...
>   ```
>   `XT_XCC_TIE_*` is the compiler + TIE feature; `XT_XPLORER_SE` is the Xplorer IDE feature.
> - **Mechanism docs:** the placeholder `$T/XtensaTools/Tools/lic/license.dat` documents
>   vendor daemon `xtensad`, `LM_LICENSE_FILE` for the search path, and node-locked
>   `FEATURE XTENSA_EVAL xtensad ... HOSTID=...` keys. `BuildMode = Evaluation` is consistent
>   with that evaluation feature set.
>
> **Reproduction consequence:** the shipped `amzn_vq7_us_582883.out` is node-locked
> (EC2-hostid-derived); you need a host-matching `xtensad` key to drive the ISS/TIE flow. The
> *compile-and-link* recipe is fully known and ungated — this is a *closable-with-license*
> wall (only the ISS/TIE side is gated). See [FlexLM Licensing Gate](../abi/flexlm-licensing.md).

---

## Part B — The analysis toolchain (this wiki's tooling)

This wiki was produced from static analysis only — no silicon, no symbol-rich debug build.

### B.1 The native Q7 disassembler — `xtensa-elf-objdump --xtensa-core=ncore2gp`

The single most important analysis tool is the **same** `xtensa-elf-objdump` from Part A.2,
run on host x86-64 with the `ncore2gp` core in scope. It is the only correct way to decode Q7
device code, because it understands the `ncore2gp` FLIX bundle formats and TIE opcodes; a
generic upstream `objdump` does not.

| Field | Value | Confidence |
|---|---|---|
| Tool | `$T/XtensaTools/bin/xtensa-elf-objdump` (1.34 MB ELF64) | `[HIGH/OBSERVED]` |
| Version | `GNU Binutils 2.34.20200201 Xtensa Tools 14.09` | `[HIGH/OBSERVED]` |
| Core | `--xtensa-core=ncore2gp` (only core in the registry) | `[HIGH/OBSERVED]` |
| Registry | `XTENSA_SYSTEM=$T/XtensaTools/config` | `[HIGH/OBSERVED]` |
| Target | `elf32-xtensa-le` | `[HIGH/OBSERVED]` |

> **Callout — the missing scalar-LX disasm config.** `ncore2gp` is the *Vision* core (512-bit
> FLIX vector DSP) and the right config for vector custom-op code and per-generation vector
> firmware. It is the **wrong** config for the NCFW management core, which is a *scalar*
> Xtensa-LX control core using the windowed **XEA2** ABI — not a Vision FLIX core. Decoding
> NCFW bytes with `--xtensa-core=ncore2gp` mis-parses the scalar `op0 = e/f` density bytes as
> Vision FLIX bundle headers (the spurious "~20–30% FLIX" artifact). The corpus ships
> **exactly one** core config (`ncore2gp-params`) and **zero** `.tie`/`.flix`/NCFW configs, so
> there is genuinely no scalar-LX config registered; NCFW disassembly falls back to manual LX
> decoding (the `op0 = e/f` 3-byte-length rule with resync at `retw.n`). The limitation is the
> absent LX config, not a real FLIX layer in NCFW. See
> [FLIX Bundle-Decoding Methodology](flix-decoding.md).

### B.2 Host binutils (x86 ELF analysis)

Stock host GNU Binutils, used for everything that is *not* Q7 code (the x86-64 host shared
objects, the `.deb` payloads, the unstripped per-config libraries):

| Tool | Version | Use |
|---|---|---|
| `objdump`, `nm`, `readelf`, `addr2line`, `strings` | `GNU Binutils 2.46-3.fc44` | x86-64 host ELF analysis |

`2.46-3.fc44` is `[HIGH/OBSERVED]` from each tool's `--version`. Count claims are grounded
with `nm <obj> | rg -c`, never grepped from decompile dumps.

### B.3 The unstripped CAS libraries (ISS oracle)

Two per-config x86 shared objects ship **not stripped** with a full `.symtab`, so host
`nm`/`readelf`/`addr2line` resolve their symbols directly — these are the cycle-accurate
simulator cores used as the differential-validation oracle:

| Library | Path | Size | Symbols | State |
|---|---|---|---|---|
| `libcas-core.so` | `$T/ncore2gp/config/` | 45,878,080 B (45 MB) | 179,079 (`nm`), 1,232 dynamic | not stripped, full `.symtab` |
| `libctype.so` | `$T/ncore2gp/config/` | — | full `.symtab` | not stripped |

Decoded with `nm -D`, `readelf -d/-s/-S`, `objdump -d/-s`; the unstripped symbol tables map
ISS addresses to function names. See the libcas/libfiss oracle coverage in the validation
chapters.

### B.4 IDA extraction (v3)

The decompiled corpus under `neuronx-gpsimd/ida/` is the IDA v3 extraction: per-binary `ctree`
AST exports (`{addr, name, calls:[{addr, callee, callee_addr, args}], …}`), per-function
address/identity JSON sidecars, `nm` dynamic export/import sidecars, and `objdump`
section/private-header dumps. It covers both the SDK packages (e.g. `customop-lib` host/device
objects) and the XtensaTools binaries themselves (the
`*__XtensaTools__bin__xtensa-elf-objdump` directory is one of 55+ tool extractions). All count
and address claims trace back to these sidecars or to a direct re-run of `nm`/`objdump` on the
binary.

---

## SDK package versions (shipped `.deb` control metadata)

Pinned from the Debian control fields inside the two `.deb` archives under
`neuronx-gpsimd/archives/` (read via `ar p … control.tar.gz | tar xzO ./control`), not from
the directory names:

| Package | Version | Architecture | Notes | Confidence |
|---|---|---|---|---|
| `aws-neuronx-gpsimd-tools` | `0.21.0.0-bc9b5fad5` | amd64 | `Depends: libtinfo5, libncursesw5`; "gpsimd_tools built using CMake"; trailing `bc9b5fad5` is the source git short-hash | `[HIGH/OBSERVED]` |
| `aws-neuronx-gpsimd-customop-lib` | `0.21.2.0` | amd64 | "custom_op_trn1_install built using CMake"; ships `build_custom_op.py`, the LSPs, the ABI archives, and the FlexLM `.out` | `[HIGH/OBSERVED]` |

Both maintained by `neuron-maintainers@amazon.com`. Internal version axes read from
`build_custom_op.py`: `__version__ = '0.21.2.0'`, `ulib_to_ucode_version = '1.21.1.0'`,
`ulib_to_isa_version = '1.0.2520.0'` `[MED/OBSERVED]`. No standalone "Neuron SDK 2.x" string
is present in these artifacts; the package versions and the `14.09 / RI-2022.9` toolchain
stamp are the version anchors.

---

## Reproduction checklist

To rebuild a device object byte-for-byte, in order:

1. The `aws-neuronx-gpsimd-tools 0.21.0.0-bc9b5fad5` toolchain (the `RI-2022.9` / `14.09`
   Xtensa Tools configured for `ncore2gp`), installed at `/opt/aws/neuron/gpsimd/tools/`.
2. For the ISS/TIE flow: a valid `xtensad` FlexLM key for the build host (the shipped
   `amzn_vq7_us_582883.out` is node-locked). The compile/link path itself is ungated.
3. The `aws-neuronx-gpsimd-customop-lib 0.21.2.0` package for `build_custom_op.py`, the LSPs,
   and the custom-op ABI archives.
4. `xt-clang++` (clang 10.0.1) with the exact `CC_OPT` flag string from §A.3 and
   `--xtensa-core=ncore2gp`, linked against the matching `lsp_fll_load_cpu{N}` and the §A.5
   runtime libraries.

To re-derive any *disassembly* you need only the shipped `xtensa-elf-objdump
--xtensa-core=ncore2gp` (no license for read-only objdump of an already-built ELF), plus host
binutils `2.46` for the x86 artifacts — and the awareness that NCFW is scalar-LX/XEA2, not
Vision FLIX (§B.1 callout).

---

### Version ledger (headline strings, with provenance)

| String | Value | Read from | Axis |
|---|---|---|---|
| Binutils banner | `GNU Binutils 2.34.20200201 Xtensa Tools 14.09` | `xtensa-elf-*  --version` | SW tools |
| Clang | `clang version 10.0.1` | device object `.comment`; `libclangBasic.so.10` | SW tools |
| SW tools release | `14.09 = RI-2022.9 = 1409000` | `ncore2gp-params`; `xtensa-versions.h:437,510,519` | SW tools |
| HW micro-arch | `NX1.1.4 = LX7.1.4 = 281040 = HW RI-2020.4` | `ncore2gp-params`; `xtensa-versions.h:317` | hardware |
| Config ID | `0xC4019686 / 0x2908E4E3` | `ncore2gp-params` (`HWConfigID0/1`) | hardware |
| SDK tools pkg | `aws-neuronx-gpsimd-tools 0.21.0.0-bc9b5fad5` | `.deb` control | SDK |
| SDK customop pkg | `aws-neuronx-gpsimd-customop-lib 0.21.2.0` | `.deb` control | SDK |
| Host analysis binutils | `2.46-3.fc44` | host `--version` | analysis |
| FlexNet client | `FlexNet Licensing v11.15.1.0 build 225974` | `tenlp.so` | licensing |
