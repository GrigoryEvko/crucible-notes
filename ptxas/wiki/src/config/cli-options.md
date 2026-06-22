# CLI Options

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas v13.0.88 accepts approximately 160 command-line options: 51 documented in `--help` output and roughly 109 internal/undocumented options discovered through binary analysis. All option names are registered via `sub_432A00` (6,427 bytes at `0x432A00`) using a generic option framework shared with other NVIDIA tools. The framework library (`sub_1C960C0`--`sub_1C97640`) supports short options (`-X`), long options (`--name`), and four value types: boolean toggle, list append, scalar value, and multi-value. Internal option names are stored ROT13-encoded in the binary.

| | |
|---|---|
| **Total options** | ~160 (51 documented + ~109 internal) |
| **Option registration** | `sub_432A00` (0x432A00, 6,427 bytes) |
| **Option parser** | `sub_434320` (0x434320, 10,289 bytes) |
| **Framework constructor** | `sub_1C960C0` |
| **Argv processor** | `sub_1C96680` |
| **Help printer** | `sub_1C97640` |
| **Options block** | 1,352 bytes on stack in compilation driver |
| **Name obfuscation** | ROT13 for internal option names |

## Architecture

```text
          argv
           │
           ▼
   ┌───────────────────┐      ┌─────────────────────────┐
   │  sub_1C960C0      │      │   sub_432A00             │
   │  Parser ctor      │◄─────│   Register ~160 options  │
   │  (56-byte context)│      │   (name, type, default,  │
   └───────┬───────────┘      │    help text, callback)  │
           │                  └─────────────────────────┘
           ▼
   ┌───────────────────┐
   │  sub_1C96680      │
   │  Process argv     │
   │  Detect - and --  │
   │  Type dispatch:   │
   │    1=bool toggle  │
   │    2=list append  │
   │    3=scalar value │
   │    4=multi-value  │
   └───────┬───────────┘
           │
           ▼
   ┌───────────────────┐
   │  sub_434320       │
   │  Validate combos  │
   │  Populate 1352B   │
   │  options block    │
   └───────┬───────────┘
           │
           ▼
     Compilation driver
      (sub_446240)
```

## Quick Start

The most common ptxas invocations and essential options, ordered by frequency of use:

```bash
# 1. Basic compilation: PTX -> cubin for a specific GPU
ptxas -arch sm_90 -o kernel.cubin kernel.ptx

# 2. Compilation with optimization control
ptxas -arch sm_100 -O3 -o kernel.cubin kernel.ptx

# 3. Debug build with line info
ptxas -arch sm_90 -g -lineinfo -o kernel.cubin kernel.ptx

# 4. Register-limited compilation (occupancy tuning)
ptxas -arch sm_90 -maxrregcount 64 -o kernel.cubin kernel.ptx

# 5. Verbose output with resource statistics
ptxas -arch sm_90 -v -o kernel.cubin kernel.ptx

# 6. Relocatable object for separate linking
ptxas -arch sm_90 -c -o kernel.o kernel.ptx

# 7. Fast-compile mode (trade codegen quality for build speed)
ptxas -arch sm_100 -Ofc max -o kernel.cubin kernel.ptx

# 8. Parallel compilation with multiple threads
ptxas -arch sm_90 -split-compile 0 -o kernel.cubin kernel.ptx

# 9. Internal knob override (developer/debugging)
ptxas -arch sm_90 -knob DUMPIR=AllocateRegisters -o kernel.cubin kernel.ptx

# 10. Discover all 1,099 internal knob values
DUMP_KNOBS_TO_FILE=/tmp/knobs.txt ptxas -arch sm_90 -o kernel.cubin kernel.ptx
```

| Goal | Options |
|---|---|
| Maximize performance | `-O3 -allow-expensive-optimizations -fmad` |
| Maximize occupancy | `-maxrregcount N` (N = 32, 64, 128, ...) |
| Minimize compile time | `-Ofc max -split-compile 0` |
| Debug build | `-g -lineinfo -sp-bounds-check` |
| Spill diagnostics | `-v -warn-spills -warn-lmem-usage` |
| Internal tuning | `-knob NAME=VALUE` (see [Knobs System](knobs.md)) |

## Option Discovery Methodology

Options were extracted from four independent sources:

1. **Official `--help` output** — 51 options with full metadata.
2. **Binary string extraction** — `strings(1)` reveals plaintext option names used in error messages and format strings.
3. **ROT13 decode** — Internal option names stored as ROT13 in the registration function. Decoding `fj4575628` yields `sw4575628`, `pbzcvyre-fgngf` yields `compiler-stats`, etc.
4. **Decompiled code cross-reference** — String references in option processing functions (`sub_434320`, `sub_4428E0`, `sub_43A400`) confirm option semantics.

Tables below use these markers:
- Unmarked rows = documented in `--help`
- Rows marked **(internal)** = discovered through RE, not in `--help`

## Core Compilation

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--opt-level` | `-O` | int | `3` | Optimization level (0–4) |
| `--output-file` | `-o` | file | `elf.o` | Output file name and location |
| `--gpu-name` | `-arch` | enum | `sm_75` | Target GPU architecture (`sm_XX`, `compute_XX`, `lto_XX`) |
| `--compile-only` | `-c` | bool | false | Generate relocatable object |
| `--entry` | `-e` | list | (all) | Entry function name(s) to compile |
| `--verbose` | `-v` | bool | false | Print code generation statistics |
| `--version` | `-V` | bool | — | Print version information |
| `--help` | `-h` | bool | — | Print help text |
| `--machine` | `-m` | int | `64` | Host architecture bitness (only 64 supported) |
| `--input-as-string` | `-ias` | list | — | PTX modules as strings instead of files |
| `--options-file` | `-optf` | list | — | Include CLI options from file |
| `--compile-functions` **(internal)** | — | list | — | Restrict compilation to named functions |
| `--ptx-length` **(internal)** | — | int | — | PTX input length for `--input-as-string` mode |
| `--tool-name` **(internal)** | — | string | — | Tool name for diagnostics (nvcc integration) |
| `--cuda-api-version` **(internal)** | — | int | (auto) | CUDA API version for compatibility |
| `--abi-compile` **(internal)** | — | bool | false | Compile using strict ABI conventions |

## Debug and Instrumentation

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--device-debug` | `-g` | bool | false | Generate debug information for device code |
| `--generate-line-info` | `-lineinfo` | bool | false | Generate line-number information |
| `--sp-bounds-check` | `-sp-bounds-check` | bool | false | Stack-pointer bounds checking; auto-enabled with `-g` or `-O0` |
| `--suppress-debug-info` | `-suppress-debug-info` | bool | false | Suppress debug sections in output; ignored without `-g` or `-lineinfo` |
| `--device-stack-protector` | `-device-stack-protector` | bool | false | Stack canaries; heuristic per-function risk assessment |
| `--sanitize` | `-sanitize` | enum | — | Instrumented code: `memcheck` or `threadsteer` |
| `--g-tensor-memory-access-check` | `-g-tmem-access-check` | bool | (with -g) | Tensor memory access checks for tcgen05 |
| `--gno-tensor-memory-access-check` | `-gno-tmem-access-check` | bool | false | Override: disable tensor memory access checks |
| `--dont-merge-basicblocks` | `-no-bb-merge` | bool | false | Prevent basic block merging (debuggable code) |
| `--return-at-end` | `-ret-end` | bool | false | Preserve last return instruction for breakpoints |
| `--make-errors-visible-at-exit` | `-make-errors-visible-at-exit` | bool | false | Generate instructions to surface memory faults at exit |
| `--trap-into-debugger` **(internal)** | — | bool | false | Insert trap instructions for debugger attachment |
| `--device-stack-protector-size` **(internal)** | — | int | (varies) | Stack protector canary size |
| `--device-stack-protector-frame-size-threshold` **(internal)** | — | int | (varies) | Frame size threshold for canary insertion |

## Register and Occupancy Control

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--maxrregcount` | `-maxrregcount` | int/enum | (unlimited) | Max registers per function; accepts N, `archmax`, `archmin` |
| `--minnctapersm` | `-minnctapersm` | int | — | Min CTAs per SM; ignored if `-maxrregcount` is set |
| `--maxntid` | `-maxntid` | list | — | Max thread-block dimensions; ignored if `-maxrregcount` is set |
| `--device-function-maxrregcount` | `-func-maxrregcount` | int/enum | (unlimited) | Max registers for device functions (with `-c`); overrides `--maxrregcount` for non-entry functions |
| `--register-usage-level` | `-regUsageLevel` | int | `5` | Register-usage optimization aggressiveness (0–10); BETA |
| `--override-directive-values` | `-override-directive-values` | bool | false | CLI values override PTX directives for `minnctapersm`, `maxntid`, `maxrregcount` |
| `--first-reserved-rreg` **(internal)** | — | int | — | First reserved register number (tools integration) |
| `--reg-fatpoint` **(internal)** | — | string | — | Fatpoint register allocation mode selector |
| `--no-fastreg` **(internal)** | — | bool | false | Disable fast register allocation path |
| `--no-spill` **(internal)** | — | bool | false | Disable register spilling (debug/stress) |

## Performance and Optimization

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--Ofast-compile` | `-Ofc` | enum | `0` | Fast-compile level: `0` (disabled), `min`, `mid`, `max` |
| `--fast-compile` **(internal)** | — | bool | false | Internal fast-compile flag (predecessor of `--Ofast-compile`) |
| `--allow-expensive-optimizations` | `-allow-expensive-optimizations` | bool | (auto at O2+) | Allow max resources for expensive optimizations |
| `--split-compile` | `-split-compile` | int | — | Max concurrent threads for optimizer; 0 = num CPUs |
| `--fmad` | `-fmad` | bool | true | Contract FP multiply + add into FMA (FMAD/FFMA/DFMA) |
| `--optimize-float-atomics` | `-opt-fp-atomics` | bool | false | FP atomic optimizations (may affect precision) |
| `--disable-optimizer-constants` | `-disable-optimizer-consts` | bool | false | Disable optimizer constant bank |
| `--cloning` **(internal)** | — | enum | (auto) | Inline function cloning control (`yes`/`no`) |
| `--perf-per-watt-opt-level` **(internal)** | — | int | — | Performance-per-watt optimization level |
| `--lds128convert` **(internal)** | — | enum | (auto) | LDS.128 conversion: `always`, `nonconst`, `never` |
| `--opt-pointers` **(internal)** | — | bool | (varies) | Enable pointer optimization passes |
| `--fastpath-off` **(internal)** | — | bool | false | Disable fast-path optimizations |
| `--full-double-div` **(internal)** | — | bool | (varies) | Full-precision double division |
| `--limit-fold-fp` **(internal)** | — | bool | (varies) | Limit floating-point constant folding |
| `--shift-right` **(internal)** | — | bool | false | Shift-right optimization control |
| `--dont-reserve-null-pointer` **(internal)** | — | bool | false | Do not reserve null pointer in address space |

## Cache Control

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--def-load-cache` | `-dlcm` | enum | (arch-dep) | Default cache modifier on global/generic load |
| `--def-store-cache` | `-dscm` | enum | (arch-dep) | Default cache modifier on global/generic store |
| `--force-load-cache` | `-flcm` | enum | — | Force cache modifier on global/generic load |
| `--force-store-cache` | `-fscm` | enum | — | Force cache modifier on global/generic store |

## Warnings and Diagnostics

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--warning-as-error` | `-Werror` | bool | false | Promote all warnings to errors |
| `--disable-warnings` | `-w` | bool | false | Inhibit all warnings |
| `--warn-on-spills` | `-warn-spills` | bool | false | Warn when registers spill to local memory |
| `--warn-on-local-memory-usage` | `-warn-lmem-usage` | bool | false | Warn when local memory is used |
| `--warn-on-double-precision-use` | `-warn-double-usage` | bool | false | Warn when doubles are used |
| `--suppress-stack-size-warning` | `-suppress-stack-size-warning` | bool | false | Suppress undetermined-stack-size warning |
| `--suppress-double-demote-warning` | `-suppress-double-demote-warning` | bool | false | Suppress double demotion warning on SM without double support |
| `--suppress-async-bulk-multicast-advisory-warning` | `-suppress-async-bulk-multicast-advisory-warning` | bool | false | Suppress `.multicast::cluster` advisory |
| `--suppress-sparse-mma-advisory-info` | `-suppress-sparse-mma-advisory-info` | bool | false | Suppress `mma.sp` advisory |
| `--print-potentially-overlapping-membermasks` **(internal)** | — | bool | false | Diagnostic for overlapping member masks |
| `--no-membermask-overlap` **(internal)** | — | bool | false | Disable member mask overlap checks |

## Output Format and Relocation

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--preserve-relocs` | `-preserve-relocs` | bool | false | Preserve relocations in linked executable |
| `--position-independent-code` | `-pic` | bool | false (whole-prog: true) | Generate PIC; default on for whole-program compilation |
| `--compiler-annotations` | `-annotate` | bool | false | Annotate compiler-internal information in binary |
| `--binary-kind` **(internal)** | — | enum | (arch-dep) | Target binary format: `mercury`, `capmerc`, `sass` |
| `--force-rela` **(internal)** | — | bool | false | Force RELA-style relocations |
| `--gen-std-elf` **(internal)** | — | bool | false | Generate standard ELF (vs NVIDIA custom format) |
| `--link-info` **(internal)** | — | string | — | Link information for assembler |
| `--force-externals` **(internal)** | — | bool | false | Force functions as external |
| `--forcetext` **(internal)** | — | bool | false | Force text-mode SASS output |
| `--emit-internal-clo` **(internal)** | — | bool | false | Emit internal compiler-level object metadata |
| `--hide-user-functions` **(internal)** | — | bool | false | Hide user function symbols in output |

## Workaround Flags

Hardware and software bug workarounds tied to internal NVIDIA bug-tracking IDs. All names are ROT13-encoded in the binary (e.g., `fj2614554` decodes to `sw2614554`). These flags toggle specific code paths that avoid known errata or compiler defects. New workarounds appear (and old ones become permanent) with each ptxas release. The validator in `sub_434320` enforces architecture restrictions: a flag set on an unsupported architecture is silently cleared with a diagnostic.

| Long Name | Short Name | Type | Default | Arch Gate | Description |
|---|---|---|---|---|---|
| `--sw2614554` **(internal)** | — | bool | false | all | Thread-safety workaround; incompatible with `--split-compile`. When set, forces single-threaded compilation — validator emits `"'--sw2614554' ignored because of '--split-compile'"` and disables split-compile. Addresses a race condition in the parallel optimizer. |
| `--sw2837879` **(internal)** | — | bool | false | all | Backend codegen workaround. No architecture gating or validator logic; consumed directly in DAG/OCG pipeline phases. Specific behavioral effect not traced beyond registration. |
| `--sw1729687` **(internal)** | — | bool | false | sm_50–sm_53 | Maxwell-era hardware errata workaround. Validator checks `(arch_ordinal - 14) > 2` and clears the flag with a warning on any architecture beyond sm_53. Activates an alternate codegen path on Maxwell GPUs. |
| `--sw200428197` **(internal)** | — | bool | false | sm_80+ | Sanitizer-compatible ABI workaround. Forces scratch register reservation for CUDA sanitizer instrumentation state and applies ABI-minimum register counts. Consumed in function/ABI setup (`sub_43F400`, `sub_441780`) alongside `--compile-as-tools-patch`. Validator clears it with `"-arch=X ignored because of --sw200428197"` on sm_75 and earlier. |
| `--sw200387803` **(internal)** | — | bool | false | deprecated | Retired workaround. Setting it triggers a deprecation advisory (`dword_29FBDB0`) but no behavioral change — the underlying fix has been permanently integrated. |
| `--sw200764156` **(internal)** | — | bool | **true** | sm_90 only | Hopper-specific hardware errata. Default is `true` (unique among all `sw*` flags). Help text reads `"Enable/Disable sw200764156"`, confirming it is a toggle that can be turned off. On any architecture other than sm_90, the user-set value is discarded: `"option -arch=X ignored because of --sw200764156"`. |
| `--sw4575628` **(internal)** | — | bool | false | sm_100+ | Cache and texturing mode workaround. Validator clears it with a warning on architectures sm_100 and earlier. In target configuration (`sub_43A400`), the target profile at offset +2465 independently determines whether the workaround is needed; if both the profile and the CLI flag are set simultaneously, the CLI flag is cleared with `"--sw4575628 conflicts with specified texturing mode"`. |
| `--sw200531531` **(internal)** | — | bool | (varies) | unknown | Known only from ROT13 decode (`fj200531531`). No help text, no validator cross-references, no decompiled consumption. Consumed in backend passes not covered by available decompiled functions. |
| `--sw200380282` **(internal)** | — | bool | (varies) | unknown | Known only from ROT13 decode (`fj200380282`). Same as `--sw200531531` — registered but with no traceable validator or target configuration logic. |
| `--sw4915215` **(internal)** | — | bool | false | all (behavior varies) | Generation-dependent workaround. On Blackwell (sm_100+, generation=100), when enabled alongside non-PIC mode, emits informational `"sw4915215=true"`. On other architectures, emits a different informational. Behavioral effect is in backend codegen. |
| `--sw4936628` **(internal)** | — | bool | false | all | Stored at options block offset +503, adjacent to `--blocks-are-clusters` in the registration sequence. No architecture gating in the validator. Specific behavioral effect requires deeper backend tracing; registration proximity suggests cluster/CTA-level code generation relevance. |

### EIATTR-Level Workarounds

Three EIATTR attributes encode workaround metadata directly in the output ELF. These are set by target architecture rather than CLI flags — ptxas emits them unconditionally when the target requires it, and the GPU driver applies fixups at load time.

| EIATTR Code | Name | Knob Name | Description |
|---|---|---|---|
| 42 (`0x2A`) | `EIATTR_SW1850030_WAR` | `OneFlapJne1850030` | Instruction offsets requiring driver-side fixup for HW bug 1850030. |
| 48 (`0x30`) | `EIATTR_SW2393858_WAR` | `OneFlapJne2393858` | Instruction offsets requiring driver-side fixup for HW bug 2393858. |
| 53 (`0x35`) | `EIATTR_SW2861232_WAR` | — | Instruction offsets for HW bug 2861232 workaround. |
| 54 (`0x36`) | `EIATTR_SW_WAR` | — | Generic software workaround container (variable payload). |
| 71 (`0x47`) | `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` | — | Offsets of `MEMBAR.SYS` instructions needing software workaround. |

## Tool and Patch Modes

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--compile-as-tools-patch` | `-astoolspatch` | bool | false | Compile patch code for CUDA tools; forces ABI-minimum regcount |
| `--extensible-whole-program` | `-ewp` | bool | false | Extensible whole-program mode |
| `--compile-as-at-entry-patch` **(internal)** | `-asatentrypatch` | bool | false | Compile as at-entry instrumentation patch |
| `--compile-as-entry-exit-patch` **(internal)** | — | bool | false | Compile as entry/exit instrumentation patch |
| `--compile-device-func-without-entry` **(internal)** | — | bool | false | Allow device function compilation without entry point |
| `--assyscall` **(internal)** | — | bool | false | System-call instrumentation mode |
| `--fdcmpt` **(internal)** | — | bool | false | Forward-compatibility mode |
| `--enable-syscall-abi` **(internal)** | — | bool | false | Enable syscall ABI for device functions |
| `--assume-extern-functions-do-not-sync` **(internal)** | — | bool | false | Assume external functions do not synchronize |
| `--function-pointer-is-function-pointer` **(internal)** | — | bool | false | Treat function pointers as true function pointers |

## Statistics and Profiling

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--compiler-stats` **(internal)** | — | bool | false | Print per-phase timing (Parse, CompileUnitSetup, DAGgen, OCG, ELF, DebugInfo) and peak memory |
| `--compiler-stats-file` **(internal)** | — | file | — | Write statistics to JSON file |
| `--fdevice-time-trace` **(internal)** | — | file | — | Chrome DevTools trace format (JSON) for time profiling |
| `--ftrace-phase-after` **(internal)** | — | string | — | Trace/dump IR state after named optimization phase |
| `--perf-stats` **(internal)** | — | bool | false | Print performance statistics |
| `--dump-perf-stats` **(internal)** | — | bool | false | Dump performance statistics to output |
| `--phase-wise` **(internal)** | — | bool | false | Per-phase statistics breakdown |
| `--use-trace-pid` **(internal)** | — | bool | false | Include process ID in trace output |
| `--verbose-tkinfo` **(internal)** | — | bool | false | Verbose token/parse information |

## Mercury and Capsule Mercury

These options control the Mercury intermediate encoding and Capsule Mercury format, which is the default output format on sm_100+ (Blackwell).

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--cap-merc` **(internal)** | — | bool | (arch-dep) | Generate Capsule Mercury format |
| `--self-check` **(internal)** | — | bool | false | Validate capmerc by comparing reconstituted SASS with original |
| `--out-sass` **(internal)** | — | bool | false | Output reconstituted SASS from capmerc |
| `--opportunistic-finalization-lvl` **(internal)** | — | int | — | Opportunistic finalization level for Mercury pipeline |

## Threading and Parallelism

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--jobserver` | `-jobserver` | bool | false | Enable GNU Make jobserver support (`make -j<N>`) |
| `--threads-dynamic-scheduling` **(internal)** | — | bool | (varies) | Dynamic scheduling for thread pool tasks |
| `--threads-min-section-size` **(internal)** | — | int | (varies) | Minimum section size for thread pool partitioning |

## Texture and Memory Modes

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--legacy-bar-warp-wide-behavior` | `-legacy-bar-warp-wide-behavior` | bool | false | Legacy PTX bar semantics; deprecated, ignored for sm_70+ |
| `--set-texmode-independent` **(internal)** | — | bool | false | Set texture mode to independent |
| `--set-texmode-raw` **(internal)** | — | bool | false | Set texture mode to raw |
| `--disable-fast-video-emulation` **(internal)** | — | bool | false | Disable fast video emulation path |
| `--treat-bf16-as-e6m9` **(internal)** | — | bool | false | Treat BF16 as E6M9 format |
| `--legacy-cvtf64` **(internal)** | — | bool | false | Legacy cvt.f64 conversion behavior |
| `--use-gmem-for-func-addr` **(internal)** | — | bool | false | Global memory for function addresses |
| `--blocks-are-clusters` **(internal)** | — | bool | false | Treat blocks as clusters (sm_90a+ TBC) |
| `--enable-extended-smem` **(internal)** | — | bool | false | Extended shared memory support |
| `--disable-smem-reservation` **(internal)** | — | bool | false | Disable shared memory reservation |
| `--membermask-overlap` **(internal)** | — | bool | (varies) | Member mask overlap control |
| `--ld-prefetch-random-seed` **(internal)** | — | int | — | Random seed for load prefetch heuristic |
| `--max-stack-size` **(internal)** | — | int | (auto) | Max kernel stack size |

## Constant Bank Allocation

NVIDIA GPUs provide 18 hardware constant banks (`c[0]` through `c[17]`), each a 64 KB read-only memory segment accessible by all threads in a warp with uniform-address broadcast — loads from constant banks cost a single memory transaction when all threads in the warp read the same address. The compiler assigns different data categories (kernel parameters, driver state, user constants, PIC tables, etc.) to separate banks to avoid address-space collisions. These options override the default bank assignments; all are ROT13-encoded.

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--sw-kernel-params-bank` **(internal)** | — | int | (varies) | Constant bank for kernel parameters |
| `--sw-driver-bank` **(internal)** | — | int | (varies) | Constant bank for driver data |
| `--sw-compiler-bank` **(internal)** | — | int | (varies) | Constant bank for compiler-generated constants |
| `--sw-user-bank` **(internal)** | — | int | (varies) | Constant bank for user constants |
| `--sw-pic-bank` **(internal)** | — | int | (varies) | Constant bank for PIC data |
| `--sw-ocl-param1-bank` **(internal)** | — | int | (varies) | Constant bank for OpenCL parameter set 1 |
| `--sw-ocl-param2-bank` **(internal)** | — | int | (varies) | Constant bank for OpenCL parameter set 2 |
| `--sw-devtools-data-bank` **(internal)** | — | int | (varies) | Constant bank for developer tools data |
| `--sw-bindless-tex-surf-table-bank` **(internal)** | — | int | (varies) | Constant bank for bindless texture/surface table |

## Stress Testing

Internal options for compiler stress testing and regression verification.

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--stress-no-crp` **(internal)** | — | bool | false | Disable CRP (Caller/callee Register Partitioning) |
| `--stress-maxrregcount` **(internal)** | — | int | — | Override maxrregcount for stress testing |
| `--stress-noglobalregalloc` **(internal)** | — | bool | false | Disable global register allocation |

## Query and Control Interface

Internal options for the query/control interface used by nvcc and other tools.

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--ext-desc-file` **(internal)** | — | file | — | External description file for instruction metadata |
| `--ext-desc-string` **(internal)** | — | string | — | External description string for instruction metadata |
| `--query-controls` **(internal)** | — | string | — | Query control parameters |
| `--query-schema` **(internal)** | — | string | — | Query schema definition |
| `--apply-controls` **(internal)** | — | string | — | Apply control parameters to compilation |
| `--profile-options` **(internal)** | — | string | — | Pass profiling options to backend |
| `--knob` **(internal)** | `-knob` | list | — | Set internal knob: `-knob NAME=VALUE`; repeatable; see [Knobs System](knobs.md) |
| `--omega-knob` **(internal)** | — | string | — | Pass omega-subsystem knob settings |
| `--expand-macros-in-omega` **(internal)** | — | bool | false | Expand macros in omega (instruction expansion) phase |
| `--force-expand-macros-after-errors` **(internal)** | — | bool | false | Force macro expansion after errors |
| `--enable-func-clone-sc` **(internal)** | — | bool | false | Enable function cloning for self-check |
| `--use-alternate-query-implementation` **(internal)** | — | bool | false | Alternate query implementation |
| `--use-alternate-const-ptr-implementation` **(internal)** | — | bool | false | Alternate constant pointer implementation |

## Syscall Integration

Internal options for system-call based operations (texturing, bulk copy).

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--use-tex-grad-syscall` **(internal)** | — | bool | false | Syscall for texture gradient operations |
| `--use-tex-surf-syscall` **(internal)** | — | bool | false | Syscall for texture/surface operations |
| `--use-bulk-copy-syscall` **(internal)** | — | bool | false | Syscall for bulk copy operations |

## Knobs Configuration

The `-knob` flag is the primary CLI mechanism for setting internal knob values — the 1,099 tuning parameters documented in [Knobs System](knobs.md). It is **not** listed in `--help` output and uses a single-dash prefix (not `--knob`).

### Syntax

```text
-knob NAME=VALUE         Set a typed knob (int, float, double, string, range)
-knob NAME               Set a boolean knob (presence = true)
-knob "A=1~B=2~C=3"     Multiple knobs in one argument, separated by ~ (tilde)
```

Multiple `-knob` flags are accumulated (list-append semantics):

```bash
ptxas -knob SchedNumBB_Limit=100 -knob DisableCSE -knob RegAllocBudget=5000 \
      -arch sm_90 -o out.cubin input.ptx
```

Knob names are **case-insensitive**. The name is resolved via ROT13-encoded lookup tables in `GetKnobIndex` (`sub_6F0820` for DAG knobs, `sub_79B240` for OCG knobs). An unrecognized name produces warning 7203: `"Invalid knob specified (%s)"`.

### Value Types

The value after `=` is parsed according to the knob's registered type:

| Type | Syntax | Example |
|---|---|---|
| Boolean | (no value) | `-knob DisableCSE` |
| Integer | decimal, `0x` hex, `0` octal | `-knob SchedNumBB_Limit=100` |
| Float | decimal with `.` | `-knob CostWeight=0.75` |
| Double | decimal with `.` | `-knob PriorityScale=1.5` |
| String | raw text | `-knob DUMPIR=AllocateRegisters` |
| Int-range | `low..high` | `-knob AllowedRange=100..200` |
| Int-list | comma-separated | `-knob TargetOpcodes=1,2,3,4` |

### Conditional Overrides (WHEN=)

Knobs can be set conditionally based on shader or instruction hash, applied only when a specific function is compiled:

```bash
# Apply knob only when shader hash matches
ptxas -knob "WHEN=SH=0xDEADBEEF;SchedNumBB_Limit=200" -arch sm_90 -o out.cubin input.ptx

# Multiple conditional overrides separated by ~
ptxas -knob "WHEN=SH=0xDEAD;DisableCSE~WHEN=IH=0x1234;RegAllocBudget=1000" ...
```

Condition prefixes: `SH=` (shader hash), `IH=` (instruction hash), `K=` (direct knob, no condition).

### Interaction with Other Knob Sources

`KnobsInit` (`sub_79D990`) processes knob sources in this order — **later sources override earlier ones** for the same knob index:

| Priority | Source | Mechanism |
|---|---|---|
| 1 (lowest) | Environment variables | `KnobsInitFromEnv` (`sub_79C9D0`), comma-separated `name=value` pairs |
| 2 | Knobs file | `ReadKnobsFile` (`sub_79D070`), plain-text with `[knobs]` header |
| 3 | `-knob` CLI flags | Accumulated list-append from argv processing |
| 4 | PTX `.pragma` | Per-function; disabled by `DisablePragmaKnobs` knob |
| 5 (highest) | WHEN= overrides | Per-function conditional, matched by shader/instruction hash |

### Environment Variable: DUMP_KNOBS_TO_FILE

The `DUMP_KNOBS_TO_FILE` environment variable causes ptxas to write all 1,099 knob names and their resolved values to a file:

```bash
DUMP_KNOBS_TO_FILE=/tmp/all_knobs.txt ptxas -arch sm_90 -o out.cubin input.ptx
```

This is the primary mechanism for discovering which knobs exist, their current defaults for a given architecture, and verifying that CLI overrides took effect.

### Commonly Used Knobs

| Knob | Type | Purpose |
|---|---|---|
| `DUMPIR` | string | Dump IR after a named phase (e.g., `AllocateRegisters`) |
| `DisableCSE` | bool | Disable common subexpression elimination |
| `DisablePhases` | string | `+`-delimited list of phases to skip |
| `SchedNumBB_Limit` | int | Basic block limit for scheduling heuristic |
| `RegAllocBudget` | int | Budget for register allocation cost model |
| `EmitLDCU` | bool | Emit LDCU instructions (SM90: requires `-forcetext -sso`) |
| `IgnorePotentialMixedSizeProblems` | bool | Suppress mixed-size register warnings |
| `DisablePragmaKnobs` | bool | Ignore all `.pragma` knob directives in PTX |

For the complete knob type system, file format, and all 1,099 knob categories, see [Knobs System](knobs.md).

## Version and Architecture Queries

| Long Name | Short Name | Type | Default | Description |
|---|---|---|---|---|
| `--list-arch` | `-arch-ls` | bool | — | Print supported GPU architectures |
| `--list-version` | `-version-ls` | bool | — | Print supported PTX ISA versions |

## Option Interaction Rules

Several options interact in non-obvious ways, as revealed by the validation logic in `sub_434320`:

1. **`--maxrregcount` dominance** — When `--maxrregcount` is specified, `--minnctapersm` and `--maxntid` are ignored. The register constraint calculator (`sub_43B660`) enforces this precedence.

2. **`--override-directive-values`** — Only affects `--minnctapersm`, `--maxntid`, and `--maxrregcount`. Without this flag, PTX directives (`.maxnreg`, `.minnctapersm`, `.maxntid`) take precedence over CLI values.

3. **`--device-function-maxrregcount` vs `--maxrregcount`** — The former overrides the latter for device functions only, and only under `--compile-only` mode. For whole-program compilation, `--device-function-maxrregcount` is ignored.

4. **`--Ofast-compile` vs `--fast-compile`** — The documented `--Ofast-compile` supersedes the internal `--fast-compile`. Both may conflict with `--allow-expensive-optimizations` (the validator in `sub_434320` checks for this).

5. **`--device-debug` auto-enables** — Setting `-g` auto-enables `--sp-bounds-check` and `--g-tensor-memory-access-check`. The flag `--gno-tensor-memory-access-check` explicitly overrides regardless of ordering.

6. **`--suppress-debug-info` requires** — Has no effect unless `--device-debug` or `--generate-line-info` is also specified.

7. **`--compile-as-tools-patch` forces** — Automatically sets maxrregcount to ABI minimum. Interacts with `--sw200428197` workaround in the function/ABI setup path (`sub_43F400`).

8. **`--split-compile` and `--allow-expensive-optimizations`** — Both activate the thread pool (`sub_1CB18B0`). The jobserver client (`sub_1CC7300`) integrates with GNU Make's `--jobserver-auth=` to respect parallel build limits.

## Recovered Options Not Yet Documented

Confidence: HIGH for names (extracted from the `cli_flag` string class in `ptxas_strings.json`); MED for semantics (inferred from name + adjacent registrations, not yet traced to consumers).

| Long Name | Type (guess) | Likely Purpose |
|---|---|---|
| `--nv-host` **(internal)** | bool | NVIDIA host-side tooling integration flag; gates host-only diagnostics |
| `--okey` **(internal)** | string | Omega-knob signing/encoding key; pairs with `--omega-knob` |
| `-dump-perf-metrics-file` **(internal)** | file | Write perf-metric snapshots to a named file |
| `-dump-perf-metrics-file-default` **(internal)** | bool | Use the default per-arch filename for the metrics dump |

> ⚡ **QUIRK — single-letter+digit option fragments**
> Strings like `-b9q9`, `-fof6`, `-sqli8` show up in the `cli_flag` partition with no help text and no decompiled consumer. They are not options — they are address-table fragments mis-classified by the string-category heuristic (the prefix `-` followed by alnum is otherwise a strong CLI signal). Confirmed: none of these tokens have a corresponding entry in `sub_432A00`'s registration list.

## Function Map

| Address | Size | Identity |
|---|---|---|
| `0x403588` | 75 B | Usage printer (calls `sub_1C97640`) |
| `0x432A00` | 6,427 B | Option registration (~160 options) |
| `0x434320` | 10,289 B | Option parser and validator |
| `0x439880` | 2,935 B | Chrome trace JSON parser (`--fdevice-time-trace`) |
| `0x43A400` | 4,696 B | Target configuration (cache defaults, `--sw4575628`) |
| `0x43B660` | 3,843 B | Register/resource constraint calculator |
| `0x446240` | 11,064 B | Compilation driver (options block consumer) |
| `0x4428E0` | 13,774 B | PTX input setup (`--compile-only`, `--extensible-whole-program`) |
| `0x60B040` | 4,500 B | Stress test option handler |
| `0x703AB0` | 10,000 B | Binary-kind / capmerc CLI parser |
| `0x1C960C0` | ~1,500 B | Option parser constructor |
| `0x1C96680` | ~2,000 B | Argv processor |
| `0x1C97210` | ~1,500 B | Option value validator |
| `0x1C97640` | — | Options help printer |
