# CLI Flag Inventory

cicc v13.0 accepts approximately **97 unique flag keys** across five parsing sites, expanding to ~160 flag+value combinations when counting value variants, and ~169 when including all architecture triplets. Flags are parsed in `sub_8F9C90` (real main), `sub_900130` (LibNVVM path A), `sub_12CC750`/`sub_9624D0` (LibNVVM option processors), and `sub_12C8DD0` (flag catalog builder with 65 registered configurations).

## Mode Selection

The top-level entry point `sub_8F9C90` sets a mode variable `v263` that selects the compilation pipeline:

| Flag | Mode | Description |
|------|------|-------------|
| `-lgenfe` | 1 | EDG C++ frontend (legacy genfe path) |
| `-libnvvm` | 2 | LibNVVM API path |
| `-lnk` | 3 | Linker path (forces `keep=true`) |
| `-opt` | 4 | Optimizer-only path (forces `keep=true`) |
| `-llc` | 6 | LLC backend-only path |

Within the LibNVVM option processors, mode is stored at offset `a1+248`:

| Flag | Value | Mode |
|------|-------|------|
| `-lnk` | 1 | Linker |
| `-opt` | 2 | Optimizer |
| `-nvc` | 3 | NVC |
| `-llc` | 3 | LLC backend |
| `-libnvvm` | 4 | LibNVVM |

## Architecture Specification

Architecture can be specified in many forms, all converging to a numeric SM value. Trailing `a` or `f` suffixes are stripped before numeric parsing. On parse failure: `"Unparseable architecture: <val>"`.

| Form | Example | Source |
|------|---------|--------|
| `-arch <val>` | `-arch sm_90` | `sub_8F9C90` |
| `-arch<val>` | `-archsm_90` | `sub_8F9C90` (compact) |
| `--nv_arch <val>` | `--nv_arch sm_100a` | `sub_8F9C90` |
| `-mcpu=sm_<N>` | `-mcpu=sm_90` | LLVM-style |
| `-opt-arch=sm_<N>` | `-opt-arch=sm_90` | Optimizer |
| `-arch=compute_<N>` | `-arch=compute_100` | Compute capability |
| `__CUDA_ARCH=<N>` | `__CUDA_ARCH=900` | Raw define |

The flag catalog (`sub_12C8DD0`) registers 24 architecture entries covering SM 75, 80, 86, 87, 88, 89, 90, 90a, 100, 100a, 100f, 103, 103a, 103f, 110, 110a, 110f, 120, 120a, 120f, 121, 121a, 121f. Each registration emits three forms: `-arch=compute_<N>`, `-mcpu=sm_<N>`, and `-opt-arch=sm_<N>`.

## I/O and General Flags

| Flag | Effect |
|------|--------|
| `-o <file>` | Output file (fatal if missing) |
| `-v` | Verbose mode |
| `-dryrun` | Do not execute compilation |
| `-keep` | Keep intermediate files |
| `-irversion` | Print IR version and exit |
| `-nvvmir-library <f>` | NVVM IR library file (also `=` form) |
| `-w` | Suppress warnings |
| `--promote_warnings` / `-Werror` | Promote warnings to errors |
| `-m64` | 64-bit mode flag |

Recognized input extensions: `.bc`, `.ci`, `.i`, `.cup`, `.optixir`, `.ii`. The `.cup` extension triggers `--orig_src_path_name` / `--orig_src_file_name` handling.

## Optimization Flags

| Flag | Effect |
|------|--------|
| `-opt=0` / `-opt=1` / `-opt=2` / `-opt=3` | Optimization level (default: 3) |
| `-Om` | Optimize for minimum code size |
| `-Osize` | Optimize for size |
| `-Ofast-compile=0\|min\|mid\|max` | Fast-compile tiers (see [Optimization Levels](./optimization-levels.md)) |
| `-disable-allopts` | Disable all optimizations |
| `-opt-disable-allopts` | Disable optimizer-level opts |
| `-lnk-disable-allopts` | Disable linker opts |
| `-llc-disable-allopts` / `-disable-llc-opts` | Disable LLC opts |
| `-aggressive-inline` | Aggressive inlining |
| `-disable-inlining` | Disable all inlining |
| `-inline-budget=40000` | Set inline cost budget |
| `-restrict` | Enable restrict pointer analysis |
| `-allow-restrict-in-struct` | Allow restrict inside struct |

## Floating Point Control

| Flag | Default | Effect |
|------|---------|--------|
| `-ftz=0\|1` | 0 | Flush-to-zero |
| `-fma=0\|1` | 1 | Fused multiply-add |
| `-prec-div=0\|1\|2` | 1 | Division precision (0=fast, 1=precise, 2=extra) |
| `-prec-sqrt=0\|1` | 1 | Square root precision |
| `-opt-fdiv=0\|1` | 0 | Optimizer fast-div |
| `-no-signed-zeros` | off | No signed zeros |
| `-fast-math` | off | Emits `-R FAST_RELAXED_MATH=1 -R __CUDA_FTZ=1` |
| `-unsafe-math` | off | Unsafe math optimizations |
| `-enable-mad` | off | Enable multiply-add |

## Pass-Through Flags

These forward arguments to specific pipeline stages:

| Flag | Target |
|------|--------|
| `--Xlgenfe <arg>` | EDG frontend |
| `--Xlibnvvm <arg>` | LibNVVM optimizer |
| `--Xlnk <arg>` / `-Xlnk <arg>` | Linker |
| `--Xopt <arg>` / `-Xopt <arg>` | Optimizer |
| `--Xllc <arg>` / `-Xllc <arg>` | LLC backend |
| `-Xlto <arg>` | LTO |

## LTO Flags

| Flag | Effect |
|------|--------|
| `-lto` / `-gen-lto` | Enable / emit LTO bitcode |
| `-olto` | Enable LTO + LLC (consumes next arg) |
| `-gen-lto-and-llc` | Emit LTO bitcode + run LLC |
| `-gen-opt-lto` | Emit optimized LTO |
| `-link-lto` | Link LTO modules |
| `--trace-lto` | Trace LTO operations |

## Debug Flags

| Flag | Effect |
|------|--------|
| `-g` | Full debug info |
| `-generate-line-info` | Line info only |
| `-debug-compile` | Debug compilation mode |
| `-show-src` / `-asm-verbose` / `-enable-verbose-asm` | Verbose PTX output |
| `-line-info-inlined-at=0` / `-no-lineinfo-inlined-at` | Disable inlined-at info |

## PTX Backend Controls (emitted to LLC)

| Flag | Effect |
|------|--------|
| `-nvptx-emit-src` | Emit source annotations in PTX |
| `-nvptx-f32ftz` | Enable f32 FTZ in backend |
| `-nvptx-fma-level=0\|1` | FMA level control |
| `-nvptx-prec-divf32=0\|1\|2\|3` | Division precision |
| `-nvptx-prec-sqrtf32=0\|1` | Sqrt precision |
| `-nvptx-kernel-params-restrict` | Kernel params are restrict |

## Hidden / Internal Flags

These are not user-facing and serve application-specific workarounds or internal state:

| Flag | Purpose |
|------|---------|
| `-vasp-fix` / `-vasp-fix1=true` / `-vasp-fix2=true` | VASP application compatibility |
| `-rp-aware-mcse=true\|false` | Register-pressure aware MachineCSE |
| `-disable-load-select-transform=true` | Internal transform control |
| `-host-ref-{ec,eg,ek,ic,ig,ik}` | Host reference tracking |
| `-has-global-host-info` | Host info presence flag |
| `-aggressive-positive-stride-analysis=false` | Disable aggressive stride analysis |

## Function Address Map

| Address | Function | Role |
|---------|----------|------|
| `0x8F9C90` | `sub_8F9C90` | Real main entry point |
| `0x900130` | `sub_900130` | LibNVVM Path A CLI parser |
| `0x12CC750` | `sub_12CC750` | LibNVVM option processor (variant 1) |
| `0x9624D0` | `sub_9624D0` | LibNVVM option processor (variant 2) |
| `0x12C8DD0` | `sub_12C8DD0` | Flag catalog builder (65 entries) |
| `0x12C8B40` | `sub_12C8B40` | Individual flag registration |
| `0x12C8530` | `sub_12C8530` | Catalog map lookup by key |

The two option processors `sub_12CC750` and `sub_9624D0` are near-identical. Key differences: `sub_12CC750` defaults `-memory-space-opt=0` while `sub_9624D0` defaults to `1`; `sub_9624D0` has `-passes=` while `sub_12CC750` has `-disable-struct-lowering`.
