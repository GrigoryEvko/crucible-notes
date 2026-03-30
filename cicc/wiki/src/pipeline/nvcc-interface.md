# nvcc-to-cicc Interface Contract

When nvcc compiles device code, it invokes cicc as an external process, passing the preprocessed CUDA source (or LLVM bitcode) along with a carefully translated set of flags. cicc never sees the raw `-fmad=1` or `-prec_sqrt=0` flags that the user typed on the nvcc command line -- those are rewritten through a **flag translation table** implemented as a global `std::map` red-black tree at `sub_8FE280`. This page documents the complete interface contract: how nvcc invokes cicc, how flags are translated, how the mode cookie selects CUDA vs. OpenCL behavior, what input formats are accepted, and what output modes are available.

The flag translation is split into two stages. Stage 1 (`sub_8FE280`) translates nvcc-facing flags into cicc-facing flags, producing a dual-slot result with an EDG front-end flag and an internal cicc flag. Stage 2 (`sub_95EB40`) further expands each cicc-facing flag into a three-column architecture mapping, routing each flag to the EDG frontend, the NVVM optimizer, and the LLC backend. The composition of these two stages means a single nvcc flag like `-fmad=1` can silently become `--emit-llvm-bc` (always injected), nothing to EDG, nothing to OPT, and `-nvptx-fma-level=1` to LLC.

| | |
|---|---|
| **Flag translation tree** | `sub_8FE280` -- global `std::map` at `qword_4F6D2A0`, 40+ entries |
| **Tree guard** | `qword_4F6D2C8` (set to 1 after first initialization) |
| **Tree node size** | 72+ bytes: key at +32, length at +40, `FlagPair*` at +64 |
| **CLI parser (Path A)** | `sub_900130` (39 KB, 12 parameters) |
| **Flag catalog (Path A/B)** | `sub_9624D0` (75 KB, 2,626 lines, 4 output vectors) |
| **3-column arch table** | `sub_95EB40` (38 KB, 23 architectures, 3-column fan-out) |
| **Mode cookies** | `0xABBA` = CUDA, `0xDEED` = OpenCL |
| **Default architecture** | `compute_75` / `sm_75` (Turing) |
| **Input extensions** | `.bc`, `.ci`, `.i`, `.cup`, `.optixir`, `.ii` |
| **Default opt level** | `-opt=3` (O3) |

## Invocation Contract

nvcc invokes cicc as a subprocess with a single input file and a set of translated flags. The general invocation form is:

```
cicc [mode-flags] [translated-flags] [pass-through-flags] -o <output> <input>
```

For the standard CUDA compilation path (no explicit `-lXXX` mode flag), cicc enters `sub_8F9C90` (real main, 10,066 bytes at `0x8F9C90`), parses all arguments into ~12 local variables, resolves the Path A / Path B dispatch variable `v253`, and calls one of:

- **Path A** (EDG pipeline): `sub_902D10` -- invokes `sub_900130` for CLI parsing, then the EDG frontend via `sub_905880`, then the LibNVVM pipeline via `sub_905EE0`.
- **Path B** (standalone LLVM pipeline): `sub_1262860` -- similar flow but through standalone LLVM infrastructure at `0x1262860`.

Path selection is controlled by `v253`, which defaults to 2 (unresolved) and is resolved through the obfuscated environment variable `NV_NVVM_VERSION`. For SM >= 100 (Blackwell and later), the default is Path B unless the `-nvc` flag is present. For SM < 100, the default is Path A. See [Entry Point](entry.md) for the full dispatch matrix.

When cicc is invoked in multi-stage mode (`-lnk`, `-opt`, `-llc`, `-libnvvm`), the entry point dispatches to `sub_905EE0` (Path A, 43 KB) or `sub_1265970` (Path B, 48 KB), which orchestrate the LNK, OPT, and LLC sub-pipelines internally.

### Parameter Passing to sub_900130

The Path A CLI parser `sub_900130` receives 12 parameters and performs a two-pass argument scan:

```c
unsigned int sub_900130(
    const char *input_file,    // a1: input filename
    const char *opencl_src,    // a2: OpenCL source path (NULL for CUDA)
    const char *output_file,   // a3: output filename
    __int64    *arg_vector,    // a4: pointer to std::vector<std::string>
    char        mode_flag,     // a5: mode flag (0=normal, 1=special)
    __int64     job_desc,      // a6: output compilation job struct
    __int64     error_out,     // a7: error string output
    _BYTE      *m64_flag,     // a8: output - set to 1 if -m64 seen
    _BYTE      *discard_names, // a9: output - set to 1 if -discard-value-names
    __int64     trace_path,    // a10: device time trace path
    __int64     trace_pid,     // a11: trace PID
    __int64     trace_env      // a12: trace env value
);
// Returns: 0 = success, 1 = error
```

**Pass 1**: Scans for `-arch` flag via `sub_8FD0D0`, extracts architecture string.

**Pass 2**: Iterates all arguments, looking each up in the red-black tree at `qword_4F6D2A0`. For tree hits, the EDG slot is pushed to the EDG argument vector (`v145`) and the cicc slot is pushed to the backend argument vector (`v148`). For tree misses, sequential string comparisons handle extended flags (`-maxreg=N`, `-split-compile=N`, `--Xlgenfe`, `--Xlibnvvm`, etc.).

Before any user flags, `sub_900130` unconditionally injects:
- `--emit-llvm-bc` into the EDG argument vector
- `--emit-nvvm-latest` into the backend argument vector

After all arguments are processed, architecture strings are appended:
- `--nv_arch` + `sm_XX` to EDG arguments
- `-arch=compute_XX` to backend arguments

## Mode Cookies

The `sub_9624D0` flag catalog function takes a fourth parameter `a4` that selects the language mode. This is not a user-visible flag -- it is passed internally by the pipeline orchestrator.

| Cookie | Hex | Decimal | Language |
|--------|-----|---------|----------|
| `0xABBA` | `0xABBA` | 43,962 | CUDA compilation |
| `0xDEED` | `0xDEED` | 57,069 | OpenCL compilation |

The cookie affects multiple behaviors:

**Precision division routing.** In CUDA mode (`0xABBA`), `-prec-div=0` maps to `-nvptx-prec-divf32=1` (not 0) at LLC, while `-prec-div=1` maps to `-nvptx-prec-divf32=2`. In OpenCL mode (`0xDEED`), the mapping is straightforward: `-prec-div=0` maps to `-nvptx-prec-divf32=0`, `-prec-div=1` to `-nvptx-prec-divf32=1`, and OpenCL additionally supports `-prec-div=2` mapping to `-nvptx-prec-divf32=3`.

**Fast-math routing.** In CUDA mode, `-fast-math` maps to `-R __CUDA_USE_FAST_MATH=1` for EDG and `-opt-use-fast-math` for OPT, with no LLC flag. In OpenCL mode, `-fast-math` maps to `-R FAST_RELAXED_MATH=1 -R __CUDA_FTZ=1` for EDG and `-opt-use-fast-math -nvptx-f32ftz` for OPT.

**Default precision.** `-prec-sqrt` defaults to 1 (precise) in CUDA mode, 0 (imprecise) in OpenCL mode.

**Discard value names.** In CUDA mode (`0xABBA`), without explicit override, value names are discarded by default (`a1+232 = 1`), generating `-lnk-discard-value-names=1`, `-opt-discard-value-names=1`, and `-lto-discard-value-names=1`. In OpenCL mode (`0xDEED`), this only applies when `(a13 & 0x20)` is set (LTO generation active).

**OptiX IR emission.** The `--emit-optix-ir` flag is only valid when the cookie is `0xABBA` or `0xDEED`.

**Internal compile call.** The LibNVVM compile function `nvvmCUCompile` (dispatch ID `0xBEAD`) is called with phase code 57,069 (`0xDEED`) regardless of the outer cookie -- this is the internal LibNVVM compile phase code, not a language selector.

## Flag Translation Table

`sub_8FE280` populates a global `std::map<std::string, FlagPair*>` in the red-black tree at `qword_4F6D2A0`. Each `FlagPair` is a 16-byte struct with two slots: slot 0 for the EDG frontend passthrough, slot 1 for the internal cicc flag. The function is called exactly once, guarded by `qword_4F6D2C8`.

### Red-Black Tree Structure

```
qword_4F6D2A0  -- tree root pointer (std::_Rb_tree)
dword_4F6D2A8  -- sentinel node (tree.end())
qword_4F6D2B0  -- root node pointer
qword_4F6D2B8  -- begin iterator (leftmost node)
qword_4F6D2C8  -- initialization guard (1 = already built)
```

Each node is 72+ bytes:

| Offset | Field |
|--------|-------|
| +0 | Color (0=red, 1=black) |
| +8 | Parent pointer |
| +16 | Left child pointer |
| +24 | Right child pointer |
| +32 | Key data pointer (std::string internals) |
| +40 | Key length |
| +48 | Key capacity |
| +64 | Value pointer (`FlagPair*`) |

Lookup is via `sub_8FE150` (lower_bound + insert-if-not-found). Insert is via `sub_8FDFD0` (allocate node + rebalance). Comparison uses standard `std::string::compare`.

### Complete nvcc-to-cicc Mapping

The table below shows every entry in the `sub_8FE280` red-black tree. Slot 0 is forwarded to the EDG frontend; slot 1 is forwarded to the cicc backend pipeline. `<null>` means no flag is generated for that slot.

| nvcc flag | EDG passthrough (slot 0) | cicc internal (slot 1) | Notes |
|-----------|--------------------------|------------------------|-------|
| `-m32` | `--m32` | `<null>` | |
| `-m64` | `--m64` | `<null>` | Also sets `*a8 = 1` |
| `-fast-math` | `<null>` | `-fast-math` | |
| `-ftz=1` | `<null>` | `-ftz=1` | |
| `-ftz=0` | `<null>` | `-ftz=0` | |
| `-prec_sqrt=1` | `<null>` | `-prec-sqrt=1` | Underscore to hyphen |
| `-prec_sqrt=0` | `<null>` | `-prec-sqrt=0` | Underscore to hyphen |
| `-prec_div=1` | `<null>` | `-prec-div=1` | Underscore to hyphen |
| `-prec_div=0` | `<null>` | `-prec-div=0` | Underscore to hyphen |
| `-fmad=1` | `<null>` | `-fma=1` | `fmad` renamed to `fma` |
| `-fmad=0` | `<null>` | `-fma=0` | `fmad` renamed to `fma` |
| `-O0` | `--device-O=0` | `-opt=0` | Dual-mapped |
| `-O1` | `--device-O=1` | `-opt=1` | Dual-mapped |
| `-O2` | `--device-O=2` | `-opt=2` | Dual-mapped |
| `-O3` | `--device-O=3` | `-opt=3` | Dual-mapped |
| `-Osize` | `<null>` | `-Osize` | |
| `-Om` | `<null>` | `-Om` | |
| `-Ofast-compile=max` | `<null>` | `-Ofast-compile=max` | |
| `-Ofc=max` | `<null>` | `-Ofast-compile=max` | Alias |
| `-Ofast-compile=mid` | `<null>` | `-Ofast-compile=mid` | |
| `-Ofc=mid` | `<null>` | `-Ofast-compile=mid` | Alias |
| `-Ofast-compile=min` | `<null>` | `-Ofast-compile=min` | |
| `-Ofc=min` | `<null>` | `-Ofast-compile=min` | Alias |
| `-Ofast-compile=0` | `<null>` | `<null>` | No-op |
| `-Ofc=0` | `<null>` | `<null>` | No-op alias |
| `-g` | `--device-debug` | `-g` | Dual-mapped |
| `-show-src` | `<null>` | `-show-src` | |
| `-disable-allopts` | `<null>` | `-disable-allopts` | |
| `-disable-llc-opts` | `<null>` | `disable-llc-opts` | |
| `-w` | `-w` | `-w` | Dual-mapped |
| `-Wno-memory-space` | `<null>` | `-Wno-memory-space` | |
| `-disable-inlining` | `<null>` | `-disable-inlining` | |
| `-aggressive-inline` | `<null>` | `-aggressive-inline` | |
| `--kernel-params-are-restrict` | `--kernel-params-are-restrict` | `-restrict` | Dual-mapped, renamed |
| `-allow-restrict-in-struct` | `<null>` | `-allow-restrict-in-struct` | |
| `--device-c` | `--device-c` | `--device-c` | Dual-mapped |
| `--generate-line-info` | `--generate-line-info` | `-generate-line-info` | Dual-mapped |
| `--enable-opt-byval` | `--enable-opt-byval` | `-enable-opt-byval` | Dual-mapped |
| `--no-lineinfo-inlined-at` | `<null>` | `-no-lineinfo-inlined-at` | |
| `--keep-device-functions` | `--keep-device-functions` | `<null>` | EDG only |
| `--emit-optix-ir` | `--emit-lifetime-intrinsics` | `--emit-optix-ir` | Triggers lifetime intrinsics in EDG |
| `-opt-fdiv=0` | `<null>` | `-opt-fdiv=0` | |
| `-opt-fdiv=1` | `<null>` | `-opt-fdiv=1` | |
| `-new-nvvm-remat` | `<null>` | `-new-nvvm-remat` | |
| `-disable-new-nvvm-remat` | `<null>` | `-disable-new-nvvm-remat` | |
| `-disable-nvvm-remat` | `<null>` | `-disable-nvvm-remat` | |
| `-discard-value-names` | `--discard_value_names=1` | `-discard-value-names=1` | Also sets `*a9 = 1` |
| `-gen-opt-lto` | `<null>` | `-gen-opt-lto` | |

Key translation patterns:
- **Underscore to hyphen**: nvcc uses underscores (`-prec_sqrt`), cicc uses hyphens (`-prec-sqrt`).
- **Rename**: `-fmad` becomes `-fma` internally.
- **Dual-mapping**: `-O0` through `-O3` emit both an EDG flag (`--device-O=N`) and a cicc flag (`-opt=N`).
- **Alias expansion**: `-Ofc=X` is silently rewritten to `-Ofast-compile=X`.
- **Implicit dependency**: `--emit-optix-ir` adds `--emit-lifetime-intrinsics` to the EDG frontend, enabling lifetime intrinsic generation that the OptiX IR output path requires.

### Extended Flags (Not in Tree)

The following flags are handled by sequential string comparison in `sub_900130` when a tree lookup misses:

| nvcc flag | Expansion | Notes |
|-----------|-----------|-------|
| `-maxreg=N` | `-maxreg=<N>` to backend | |
| `-split-compile=N` | `-split-compile=<N>` to OPT | Error if specified twice |
| `-split-compile-extended=N` | `-split-compile-extended=<N>` to OPT | Mutually exclusive with `-split-compile` |
| `--Xlgenfe <arg>` | `<arg>` to EDG | |
| `--Xlibnvvm <arg>` | `<arg>` to backend | |
| `--Xlnk <arg>` / `-Xlnk <arg>` | `-Xlnk` + `<arg>` to backend | |
| `--Xopt <arg>` / `-Xopt <arg>` | `-Xopt` + `<arg>` to backend | |
| `--Xllc <arg>` / `-Xllc <arg>` | `-Xllc` + `<arg>` to backend | |
| `-Xlto <arg>` | `<arg>` to LTO vector | |
| `-covinfo <file>` | `-Xopt -coverage=true -Xopt -covinfofile=<file>` | |
| `-profinfo <file>` | `-Xopt -profgen=true -Xopt -profinfofile=<file>` | |
| `-profile-instr-use <file>` | `-Xopt -profuse=true -Xopt -proffile=<file>` | |
| `-lto` | `-gen-lto` to backend; enables LTO | |
| `-olto <file>` | `-gen-lto-and-llc` + flag + next arg | |
| `--promote_warnings` | `-Werror` to backend; flag to EDG | |
| `-inline-info` | `-Xopt -pass-remarks=inline` + missed + analysis | |
| `-jump-table-density=N` | `-jump-table-density=<N>` to backend | |
| `-opt-passes=<val>` | `-opt-passes=<val>` to backend | |
| `--orig_src_file_name <val>` | `--orig_src_file_name` + `<val>` to EDG | |
| `--force-llp64` | Pass to EDG; sets `byte_4F6D2DC = 1` | |
| `--partial-link` | Complex: may add `-memdep-cache-byval-loads=false` to OPT and LLC | Sets `byte_4F6D2D0 = 1` |
| `--tile-only` | Pass to EDG + `--tile_bc_file_name` + output path | |
| `--device-time-trace` | Pass to EDG; next arg becomes trace path | |
| `-jobserver` | `-jobserver` to backend or pass to EDG | |

## Input Extensions

Input files are identified by extension during the argument loop in `sub_8F9C90`. The **last** matching file wins (the input variable `s` is overwritten each time). Extension matching proceeds by checking trailing characters: last 3 for `.bc`/`.ci`, last 2 for `.i`, last 3 for `.ii`, last 4 for `.cup`, last 8 for `.optixir`.

| Extension | Format | Condition | Address |
|-----------|--------|-----------|---------|
| `.bc` | LLVM bitcode | Always accepted | `0x8FAA0A` |
| `.ci` | CUDA intermediate (preprocessed) | Always accepted | `0x8FAA29` |
| `.i` | Preprocessed C/C++ | Always accepted | `0x8FA9xx` |
| `.ii` | Preprocessed C++ | Always accepted | `0x8FBF7E` |
| `.cup` | CUDA source | Only after `--orig_src_path_name` or `--orig_src_file_name` | `0x8FBFC4` |
| `.optixir` | OptiX IR | Always accepted | `0x8FC001` |

Unrecognized arguments (those failing both tree lookup and sequential matching, and lacking a recognized extension) are silently appended to the `v266` pass-through vector, which is forwarded to sub-pipelines.

If no input file is found after parsing all arguments:

```
Missing input file
Recognized input file extensions are: .bc .ci .i .cup .optixir
```

Note that `.ii` is not mentioned in the error message despite being accepted -- this appears to be a minor oversight in the error string.

## Output Modes

cicc can produce several output formats, controlled by the combination of flags in the `a13` compilation mode bitmask. The bitmask is accumulated during flag parsing in `sub_9624D0`:

| a13 Value | Mode | Output Format |
|-----------|------|---------------|
| `0x07` | Default (all phases) | PTX text assembly |
| `0x10` | Debug/line-info | PTX with debug metadata |
| `0x21` | `-gen-lto` | LTO bitcode (`.lto.bc`) |
| `0x23` | `-lto` (full LTO) | LTO bitcode + link |
| `0x26` | `-link-lto` | Linked LTO output |
| `0x43` | `--emit-optix-ir` | OptiX IR (`.optixir`) |
| `0x80` | `-gen-opt-lto` | Optimized LTO bitcode |
| `0x100` | `--nvvm-64` | 64-bit NVVM mode modifier |
| `0x200` | `--nvvm-32` | 32-bit NVVM mode modifier |

The default output is PTX text, written through the LLC backend's PTX printer. The output file path is specified by `-o <file>` (fatal if missing in multi-stage modes). When no output path is provided in simple mode, `sub_900130` constructs a `.ptx` filename from the input.

### PTX Text Output (Default)

The standard path runs all four internal phases: LNK (IR linking), OPT (NVVM optimizer), optionally OptiX IR emission, then LLC (code generation). The LLC backend writes PTX assembly text to the output file. In `sub_905EE0`, the output writing (Phase 4) checks the first bytes of the result for ELF magic (`0x7F`, `0xED`) to detect accidentally binary output; if the mode is text mode (0) and ELF headers are present, it indicates an internal error.

### LTO Bitcode Output

When `-lto` or `-gen-lto` is active, cicc produces LLVM bitcode instead of PTX. The `-gen-lto` flag sets `a13 = (a13 & 0x300) | 0x21` and adds `-gen-lto` to the LTO argument vector. The `-gen-lto-and-llc` variant additionally runs LLC after producing the LTO bitcode, generating both outputs. The `-olto` flag takes a next argument (the LTO optimization level) and combines LTO bitcode generation with LLC execution.

### OptiX IR Output

The `--emit-optix-ir` flag sets `a13 = (a13 & 0x300) | 0x43`. In the flag translation tree, it also injects `--emit-lifetime-intrinsics` into the EDG frontend, enabling lifetime intrinsic emission that is required for the OptiX IR format. In the flag catalog (`sub_9624D0`), it additionally routes `-do-ip-msp=0` and `-do-licm=0` to the optimizer, disabling interprocedural memory space promotion and LICM for OptiX compatibility.

### Split Compile

The `-split-compile=N` flag (or `-split-compile-extended=N`) routes to the optimizer as `-split-compile=<N>` (or `-split-compile-extended=<N>`). These are mutually exclusive and error if specified more than once (`"split compilation defined more than once"`). When `-split-compile-extended` is used, it also sets the flag at `a1+1644` to 1. The split compile mechanism divides the compilation unit into N partitions for parallel processing.

## Exit Codes

The process exit code is the return value of `sub_8F9C90` (real main), stored in `v8`:

| Code | Meaning | Source |
|------|---------|--------|
| 0 | Success | Normal compilation; `-irversion` query |
| 1 | Argument error | Missing input file, missing output file, CLI parse failure |
| `v264` | Pipeline error | Return code from `sub_905EE0` / `sub_1265970` / `sub_905880` |

Within the pipeline, error codes from `sub_905EE0` are set via `*a8`:

| `*a8` Value | Meaning |
|-------------|---------|
| 0 | Success (`NVVM_SUCCESS`) |
| -1 | File open/read error |
| 1 | `NVVM_ERROR_OUT_OF_MEMORY` |
| 4 | `NVVM_ERROR_INVALID_INPUT` |
| 5 | `NVVM_ERROR_INVALID_CU` (null compilation unit) |

Error messages are written to `qword_4FD4BE0` (stderr stream) via `sub_223E0D0`. All LibNVVM-originated errors are prefixed with `"libnvvm : error: "`. Representative errors:

- `"Error processing command line: <cmd>"` (from `sub_900130` failure)
- `"Missing input file"` / `"Missing output file"`
- `"<src>: error in open <file>"` (file I/O)
- `"libnvvm: error: failed to create the libnvvm compilation unit"`
- `"libnvvm: error: failed to add the module to the libnvvm compilation unit"`
- `"libnvvm: error: failed to get the PTX output"`
- `"Invalid NVVM IR Container"` (error code 259, from `sub_C63EB0`)
- `"Error opening '<file>': file exists!"` / `"Use -f command line argument to force output"`
- `"Error: Failed to write time profiler data."`
- `"Unparseable architecture: <val>"`
- `"libnvvm : error: <flag> is an unsupported option"`
- `"libnvvm : error: <flag> defined more than once"` (duplicate `-maxreg`, etc.)

## Special Behaviors

### .cup Extension Gate

The `.cup` extension (CUDA preprocessed source) is only accepted as an input file when the **preceding** argument is `--orig_src_path_name` or `--orig_src_file_name`. These are metadata flags inserted by nvcc to track the original source file path for diagnostic messages. The check is:

```c
// At 0x8FBFC4 and 0x8FBFDE:
if (strcmp(argv[i-1], "--orig_src_path_name") == 0 ||
    strcmp(argv[i-1], "--orig_src_file_name") == 0) {
    s = argv[i];  // accept .cup as input
}
```

This means cicc will silently ignore a `.cup` file that appears without a preceding metadata flag. When accepted, the `.cup` extension triggers `--orig_src_path_name` / `--orig_src_file_name` handling in `sub_900130`, which forwards the original source path to the EDG frontend for accurate error location reporting.

### -Ofc Alias Handling

The `-Ofc=X` form is a shorthand alias for `-Ofast-compile=X`, handled entirely within the `sub_8FE280` flag translation tree. The tree contains six entries for fast-compile control:

| Tree Key | cicc Internal | Effect |
|----------|---------------|--------|
| `-Ofast-compile=max` | `-Ofast-compile=max` | Identity |
| `-Ofc=max` | `-Ofast-compile=max` | Alias |
| `-Ofast-compile=mid` | `-Ofast-compile=mid` | Identity |
| `-Ofc=mid` | `-Ofast-compile=mid` | Alias |
| `-Ofast-compile=min` | `-Ofast-compile=min` | Identity |
| `-Ofc=min` | `-Ofast-compile=min` | Alias |
| `-Ofast-compile=0` | `<null>` | No-op |
| `-Ofc=0` | `<null>` | No-op alias |

The aliasing happens at the tree level, before `sub_9624D0` ever sees the flag. By the time the flag catalog processes the argument, `-Ofc=max` and `-Ofast-compile=max` are indistinguishable. See [Optimization Levels](../config/optimization-levels.md) for what each fast-compile tier actually does.

In `sub_9624D0`, `-Ofast-compile` is stored at offset `a1+1640` as an integer:

| Level string | Integer value | Behavior |
|--------------|---------------|----------|
| `"0"` | 1 | Disabled (then reset to 0) |
| `"max"` | 2 | Most optimizations skipped; forces `-lsa-opt=0`, `-memory-space-opt=0` |
| `"mid"` | 3 | Medium pipeline |
| `"min"` | 4 | Close to full optimization |

Any other value produces: `"libnvvm : error: -Ofast-compile called with unsupported level, only supports 0, min, mid, or max"`.

Only one `-Ofast-compile` is permitted per invocation. A second occurrence triggers: `"libnvvm : error: -Ofast-compile specified more than once"`.

### Discard Value Names

The `-discard-value-names` flag has complex interaction semantics. In the tree, it dual-maps to `--discard_value_names=1` (EDG, note underscores) and `-discard-value-names=1` (cicc, note hyphens). Additionally, per-phase overrides are possible via `-Xopt -opt-discard-value-names=0`, `-Xlnk -lnk-discard-value-names=0`, or `-Xlto -lto-discard-value-names=0`.

In CUDA mode, without explicit flags, value names are discarded by default. In OpenCL mode, the default only applies when LTO generation is active (`a13 & 0x20`). This reflects the fact that value names are useful for debugging but waste memory in production builds.

### Wizard Mode Interaction

The `-v` (verbose), `-keep` (keep intermediates), and `-dryrun` flags are parsed in `sub_8F9C90` but are **only effective when wizard mode is active**. Wizard mode is gated by `getenv("NVVMCCWIZ") == 553282`, which sets `byte_4F6D280 = 1`. Without wizard mode, these flags are silently accepted but have no effect -- `v259` (verbose) and `v262` (keep) remain 0. This is a deliberate anti-reverse-engineering measure.

### Default Values When Flags Are Absent

When a flag is not explicitly provided, `sub_9624D0` applies these defaults (checking stored-value sentinels):

| Flag | Default Value | Sentinel Offset |
|------|---------------|-----------------|
| `-opt=` | `-opt=3` (O3) | `a1+400` |
| `-arch=compute_` | `-arch=compute_75` (Turing) | `a1+560` |
| `-ftz=` | `-ftz=0` (no flush-to-zero) | `a1+592` |
| `-prec-sqrt=` | `-prec-sqrt=1` (CUDA) / `-prec-sqrt=0` (OpenCL) | `a1+624` |
| `-prec-div=` | `-prec-div=1` (precise) | `a1+656` |
| `-fma=` | `-fma=1` (enabled) | `a1+688` |
| `-opt-fdiv=` | `-opt-fdiv=0` | `a1+464` |

## Configuration

### Four Output Vectors

`sub_9624D0` builds four independent `std::vector<std::string>` that are serialized into `char**` arrays at function exit:

| Vector | Seed | Output | Pipeline Phase |
|--------|------|--------|----------------|
| `v324` (LNK) | `"lnk"` | `a5`/`a6` | Phase 1: IR linker |
| `v327` (OPT) | `"opt"` | `a7`/`a8` | Phase 2: NVVM optimizer |
| `v330` (LTO) | (none) | `a9`/`a10` | Phase 3: LTO passes |
| `v333` (LLC) | `"llc"` | `a11`/`a12` | Phase 4: Code generation |

Each vector element is a 32-byte `std::string` with SSO. At exit, elements are serialized via `malloc(8 * count)` for the pointer array and `malloc(len+1)` + `memcpy` for each string.

### Architecture Bitmask Validation

Architecture validation in `sub_9624D0` uses a 64-bit bitmask `0x60081200F821`:

```c
offset = arch_number - 75;
if (offset > 0x2E || !_bittest64(&0x60081200F821, offset))
    // error: "is an unsupported option"
```

Valid architectures (bit positions): SM 75, 80, 86, 87, 88, 89, 90, 100, 103, 110, 120, 121. The `a`/`f` sub-variants share the base SM number for bitmask validation but receive distinct routing in `sub_95EB40`.

### Compilation Mode Flags Bitmask (a13)

The `a13` parameter in `sub_9624D0` is an IN/OUT bitmask tracking compilation mode:

| Bit/Mask | Source Flag | Meaning |
|----------|-------------|---------|
| `0x07` | (default) | Phase control: all phases active |
| `0x10` | `-g`, `--generate-line-info` | Debug/line-info enabled |
| `0x20` | `-gen-lto`, `-gen-lto-and-llc` | LTO generation enabled |
| `0x21` | `-gen-lto` | Gen-LTO mode |
| `0x23` | `-lto` | Full LTO mode |
| `0x26` | `-link-lto` | Link-LTO mode |
| `0x43` | `--emit-optix-ir` | OptiX IR emission mode |
| `0x80` | `-gen-opt-lto` | Optimized LTO lowering |
| `0x100` | `--nvvm-64` | 64-bit NVVM mode |
| `0x200` | `--nvvm-32` | 32-bit NVVM mode |
| `0x300` | (mask) | 64/32-bit mode bits mask |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `sub_8F9C90` | `0x8F9C90` | 10,066 B | Real main entry point |
| `sub_8FE280` | `0x8FE280` | ~35 KB | Flag translation tree builder (nvcc -> cicc) |
| `sub_8FE150` | `0x8FE150` | -- | Tree lookup (lower_bound + insert) |
| `sub_8FDFD0` | `0x8FDFD0` | -- | Tree insert + rebalance |
| `sub_8FD0D0` | `0x8FD0D0` | -- | Architecture flag scanner (first pass) |
| `sub_900130` | `0x900130` | 39 KB | CLI processing Path A (12 params) |
| `sub_902D10` | `0x902D10` | ~9 KB | Path A orchestrator |
| `sub_904450` | `0x904450` | -- | Push flag to argument vector |
| `sub_905880` | `0x905880` | ~6 KB | EDG frontend stage |
| `sub_905EE0` | `0x905EE0` | 43 KB | Path A multi-stage pipeline driver |
| `sub_908220` | `0x908220` | -- | LLC output callback (ID 56993) |
| `sub_908850` | `0x908850` | -- | Triple construction (`nvptx64-nvidia-cuda`) |
| `sub_9085A0` | `0x9085A0` | -- | OPT output callback (ID 64222) |
| `sub_95EB40` | `0x95EB40` | 38 KB | 3-column architecture mapping table builder |
| `sub_9624D0` | `0x9624D0` | 75 KB | Flag catalog (4 output vectors, ~111 flags) |
| `sub_1262860` | `0x1262860` | -- | Path B simple dispatch |
| `sub_1265970` | `0x1265970` | 48 KB | Path B multi-stage pipeline driver |

### Global Variables

| Address | Variable | Purpose |
|---------|----------|---------|
| `qword_4F6D2A0` | Flag tree root | `std::map` root for `sub_8FE280` |
| `dword_4F6D2A8` | Flag tree sentinel | `tree.end()` |
| `qword_4F6D2B0` | Flag tree root node | Root node pointer |
| `qword_4F6D2B8` | Flag tree begin | Leftmost node (begin iterator) |
| `qword_4F6D2C8` | Init guard | Set to 1 after `sub_8FE280` first call |
| `byte_4F6D2D0` | Partial-link flag | Set by `--partial-link` |
| `byte_4F6D2DC` | LLP64 flag | Set by `--force-llp64` |
| `unk_4F06A68` | Data model width | 8 = 64-bit, 4 = 32-bit |
| `unk_4D0461C` | Address space 3 flag | Enables `p3:32:32:32` in datalayout |
| `byte_4F6D280` | Wizard mode | Set by `NVVMCCWIZ=553282` |

## Cross-References

- [Entry Point & CLI](entry.md) -- full `sub_8F9C90` analysis, Path A/B dispatch, wizard mode
- [CLI Flag Inventory](../config/cli-flags.md) -- complete flag listing across all five parsing sites
- [Optimization Levels](../config/optimization-levels.md) -- O0-O3 and fast-compile tier pipeline details
- [Environment Variables](../config/env-vars.md) -- `NVVMCCWIZ`, `NV_NVVM_VERSION`
- [EDG Frontend](edg.md) -- what happens after EDG flags are forwarded
- [OptiX IR](optix-ir.md) -- OptiX IR emission pipeline
- [Optimizer](optimizer.md) -- how `-opt=N` and fast-compile flags affect the optimization pipeline
