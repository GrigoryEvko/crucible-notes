# Environment Variable and Runtime Gate Catalog

`tileiras` consumes external configuration through two distinct mechanisms.
The first is the libc `getenv(3)` family, reachable via the PLT stub at
`0x004055B0` and through the wrapper `sub_45AE9A0` (`getenv`-into-
`std::string`). The second is a band of process-wide scalar globals in
the `0x5B6xxxx` region of `.bss`/`.data` -- the "runtime gates" -- whose
default values are written by C++ static constructors during
dynamic-linker init, bound by name to LLVM `cl::opt` storage, then read
directly by optimizer passes. Both mechanisms wire up during program
startup: env vars are pulled lazily on first consumer use (or at ctor
time for a couple of LLVM-Support strings), while gates populate
unconditionally before `main` runs and then optionally get overwritten by
`--<flag>` command-line arguments parsed through the LLVM CommandLine
library. Together they form the entire externally-tunable surface of
`tileiras` -- no config file, no JSON, no INI, just env vars plus
`cl::opt` flags backed by these scalar globals.

## Table 1: Environment Variables

Columns: env var name | consumer `sub_ADDR` | behavior | default when unset.

| Env var | Consumer (`sub_ADDR`) | Behavior | Default |
|---------|-----------------------|----------|---------|
| `MLIR_ENABLE_EVO` | `sub_2D381B0` (`serializeAndDumpSass`) | Master gate for the ptxas-knob-file path. Tested for non-null only -- any non-empty value (including `"0"`, `"false"`) enables. When set together with `PTX_KNOBS_PATH`, ` --knobs-file=<path>` is appended to `basePTXOptions` before `ptxas` is spawned. | Disabled -- knob-file path is skipped via early `goto` even when `PTX_KNOBS_PATH` is set. |
| `PTX_KNOBS_PATH` | `sub_2D381B0` | Path to a `ptxas` internal-knob text file. Forwarded verbatim as `--knobs-file=<path>`; tileiras itself does not parse the contents. Append uses libstdc++'s string max-size guard `0x3FFFFFFFFFFFFFFFLL - 14`. | Disabled. AND-gated on `MLIR_ENABLE_EVO`; unsetting either skips the append. |
| `TILE_AS_DEBUG_UNLIMITED_SMEM` | `sub_12C8DF0` (TileAS memory planner) | String-equality test against literal `"1"` via `sub_44E1F60`. When equal, the per-CTA dynamic shared-memory ceiling used by the memory planner is raised from 232448 B (227 KiB, the Blackwell SM100/SM103/SM120 limit) to `0x7FFFFFFF`, effectively no ceiling. Used to bypass smem-overcommit checks for diagnostic compiles. | Ceiling = 232448 B (`0xE3C00`). Stored as `ptr[16]` = max-smem-per-CTA in the per-kernel DenseMap built at `sub_12BB050`. |
| `TILEIR_PREFER_TMA_FOR_LOAD_STORE` | `sub_7B6970` (TMA-vs-`cp.async` chooser) | Value is SSO-copied and then string-compared against `"true"` / `"false"` downstream. `"true"` selects the TMA `cp.async.bulk` path for load/store legalization on SM100+; absence defaults the comparison RHS to literal `"false"`, leaving TMA non-preferred. Non-boolean values fall through with implementation-defined effect. | `"false"` (5 bytes) -- TMA path is **not** preferred; legacy `cp.async` or vector load/store wins the heuristic. |
| `TILEIR_DELAY_TMA_STORE_WAIT` | `sub_8D9DD0` via `sub_45AE9A0` | Active only when `*a1 == 3` (TMA-store pipeline tier). Read into a `std::string`, then parsed by `strtol(base=10)` after `errno` clear. Empty / non-numeric throws `std::stoi("stoi")`; out-of-range likewise. Final return is `parsed != 0`. Effect: defers the `cp.async.bulk.wait_group` barrier after a TMA store. | Disabled -- function returns `(*a1 == 4)` as the default (pipeline-tier gate only); env-var absence leaves delay-wait off. |
| `TILEIR_ALWAYS_SWIZZLE` | `sub_7A9D60` (swizzle selector) | Returns `1` (true) immediately if the env-var is non-null. Any value -- including `"0"`, `"false"`, `"no"` -- short-circuits the swizzle-selection chain (`sub_7A9520`, `sub_7A9D30`, `sub_79DA60`, `sub_7A9750`) and forces the swizzled layout. Diagnostic switch only. | Disabled -- swizzle heuristic runs normally. |
| `CUDA_ROOT` | `sub_5773C0` (driver) **and** `sub_1A41D30` (`NVVM::getCUDAToolkitPath()`) | First probe in both chains. `sub_5773C0` SSO-copies into an `std::string`; `sub_1A41D30` returns the raw `const char *` from getenv memory. | Falls through to `CUDA_HOME`. |
| `CUDA_HOME` | `sub_5773C0` and `sub_1A41D30` | Second probe. Same copy-semantics per resolver as `CUDA_ROOT`. | Falls through to `CUDA_PATH`. |
| `CUDA_PATH` | `sub_5773C0` and `sub_1A41D30` | Third probe. | `sub_5773C0`: falls back to `sub_45AA3C0(scratch, argv[0])` -- a `/proc/self/exe` walk that strips two trailing path components (`bin/`). `sub_1A41D30`: returns `byte_4FA453E` (rodata empty/null sentinel), which produces the user-visible "Please specify the toolkit path" error from `sub_1A41DB0`. |
| `LLVM_OVERRIDE_PRODUCER` | `ctor_611` @ `0x00538D90` | Read once during C++ static-ctor execution. Stored into global `qword_5BDF538` -- the producer string used by the `disable-bitcode-version-upgrade` `cl::opt` at LLVM bitcode load time. | Built-in `a2100git` rodata symbol (LLVM version tag). |
| `LLVM_DISABLE_SYMBOLIZATION` | `sub_45B5AC0` (LLVMSymbolizer probe) | Presence (any non-null) disables the in-process `llvm-symbolizer` invocation used by `PrettyStackTrace` / signal-handler backtraces. | Symbolization enabled (subject to finding the symbolizer binary). |
| `LLVM_SYMBOLIZER_PATH` | `sub_45B5AC0` | Absolute or relative path to a custom `llvm-symbolizer`. When set, `strlen` is passed to `sub_45B0940` (program-path resolver) and PATH search is bypassed. | PATH-walk for basename `"llvm-symbolizer"` (15 bytes). |
| `LLVM_ENABLE_SYMBOLIZER_MARKUP` | `sub_45B6090` | Empty / unset early-returns 0. Non-empty engages the `{{{bt:...}}}` symbolizer-markup pipeline for stack traces. | Markup path skipped. |
| `HOME` | `sub_45AC290` (`llvm::sys::path::home_directory`) | Copied to out-`SmallString` if non-null. Else falls back to `getpwuid_r(getuid(), …, sysconf(_SC_GETPW_R_SIZE_MAX)=70)` and uses `pw_dir`. | passwd-database `pw_dir`; if also empty, returns failure. |
| `PWD` | `sub_45AA940` (`llvm::sys::fs::current_path`) | `getenv("PWD")`, then validates dev+ino equality against `"."` via `sub_45A9AD0`/`sub_45A6C90`. On match, uses `$PWD` (preserves symlink spelling). | `getcwd(3)` with adaptive doubling buffer from 4096 up. |
| `PATH` | `sub_45AA3C0` (`GetMainExecutable`) **and** `sub_45B0940` (`ExecuteAndWait`) | `sub_45AA3C0`: only when `argv[0]` lacks `/`; tokenized via `strtok_r(":", …)`, each entry tried with `realpath` + `__xstat`. `sub_45B0940`: only when caller's pre-resolved-path arg is null. | `/proc/self/exe` resolution skips PATH when `argv[0]` contains `/`. |
| `TMPDIR` / `TMP` / `TEMP` / `TEMPDIR` | `sub_45ACEA0` (`llvm::sys::path::system_temp_directory`) | Probed in this order; first non-null wins, copied into out-`SmallString`. | Fallback writes 4-byte literal `0x706D742F` (= `"/tmp"`) when `ErasedOnReboot` path taken. |
| `TERM` | `sub_45AE730` (color-term detection) | `strlen($TERM)`-switched comparison against hard-coded ASCII packs for `ansi`, `cygwin`, `linux`, `xterm`, `vt100`, `screen`, `rxvt`. Generic tail-check accepts any value containing `"color"`. | Returns 0 (colors disabled). |

Two other `getenv`-touching helpers exist but stay dormant: one for
command-line-option scanning (`sub_4535E90` -- `llvm::cl::ParseCommandLineOptions`'s `EnvVar`
arg, dormant because `main` passes null) and one for response-file expansion
(`sub_45AEBB0`). They are listed for completeness; in production runs of
`tileiras` they read no environment variables.

## Table 2: Runtime Gates

Columns: address (`byte_*` / `qword_*`) | populating ctor | default value |
consumer pass / routine. Width is 1 byte for `byte_*`, 4 bytes for the
low-DWORD of a `qword_*` integer slot. Numeric `cl::opt<int>` slots are
laid out as `[qword_X]` (Initial) + `[qword_X+0x10]` (Default) plus a
`BYTE4(qword_X+0x10)=1` "has-default" flag; consumers always read the
Initial via `LODWORD`.

The consumer-side read of any `cl::opt<int>` gate is a literal `mov` from
the Initial slot, equivalent to:

```c
/* every pass that consults a numeric gate compiles to this shape; the
 * address is baked in by static-init linkage, so the call site is one
 * load and no indirection. */
static inline int32_t read_gate_int(const void *initial_slot) {
    return *(const int32_t *)initial_slot;
}

/* boolean gates use a 1-byte slot; nonzero means enabled. */
static inline bool read_gate_bool(const uint8_t *byte_slot) {
    return *byte_slot != 0;
}
```

Honouring the same `--flag` surface in a reimplementation only requires
wiring each per-option storage cell to a `cl::opt<T>` so
`ParseCommandLineOptions` can overwrite it; everything downstream is a
direct read.

| Address | Populator | Default | Consumer / `cl::opt` name |
|---------|-----------|---------|---------------------------|
| `byte_5B6A640` | `ctor_372` @ `0x491070` | `0` | `-basic-dbe` -- basic dead-barrier-elim (`sub_27DD410`) |
| `unk_5B6A5A0` (location) | `ctor_371` @ `0x490FF0` | `1` | `-opt-unsafe-algebra` -- `cl::location` external (`sub_27D7EE0` UFSimp) |
| `qword_5B6AEC0` (lo32) | `ctor_374_0` @ `0x492550` | `8` | `-scev-cgp-cross-block-limit` |
| `qword_5B6B040` (lo32) | `ctor_374_0` | `3` | `-scev-cgp-idom-level-limit` |
| `qword_5B6B100` (lo32) | `ctor_374_0` | `500` | `-scev-cgp-inst-limit` |
| `qword_5B6B700` (lo32) | `ctor_374_0` | `4096` | `-scev-cgp-tid-max-value` |
| `qword_5B6B880` (lo32) | `ctor_374_0` | `-1` | `-scev-cgp-control` (transformation budget) |
| `qword_5B6BA00` (lo32) | `ctor_374_0` | `2` | `-do-function-scev-cgp` |
| `byte_5B6BAC0` | `ctor_374_0` | `1` | `-do-scev-cgp-aggresively` (sic) |
| `qword_5B6BC40` (lo32) | `ctor_374_0` | `0` | `-dump-base-address-strength-reduce` |
| `qword_5B6BDC0` (lo32) | `ctor_374_0` | `4` | `-do-base-address-strength-reduce` (BASR master, 0..4) |
| `qword_5B6BE80` (lo32) | `ctor_374_0` | `2` | `-do-scev-cgp` (module-level enable) |
| `byte_5B6BF60` | `ctor_375` @ `0x4934D0` | `0` | `-enable-fma-to-ffma2` |
| `byte_5B6C020` | `ctor_375` | `1` | `-enable-dot` (DOT lowering master) |
| `byte_5B6C0E0` | `ctor_375` | `1` | `-aggressive-no-sink` |
| `qword_5B6C1A0` (lo32) | `ctor_375` | `64` | `-max-chain-length` (idpa cap) |
| `qword_5B6C260` (lo32) | `ctor_375` | `2` | `-max-chain-width` |
| `byte_5B6C320` | `ctor_375` | `1` | `-balance-dot-chain` |
| `qword_5B6C3E0` (lo32) | `ctor_376` @ `0x494040` | `-1` | `-do-clone-for-ip-msp` |
| `qword_5B6C4B0` (BYTE4) | `ctor_376` | `0` | `-dump-ip-msp` |
| `byte_5B6CAC0` | `ctor_378` @ `0x494DB0` | `1` | `-lsa-opt` -- copy-struct-args-to-local |
| `byte_5B6CC40` | `ctor_379_0` @ `0x495350` | `1` | `-track-indir-load` |
| `byte_5B6CD00` | `ctor_379_0` | `0` | `-dump-ir-after-memory-space-opt` |
| `byte_5B6CDC0` | `ctor_379_0` | `0` | `-dump-ir-before-memory-space-opt` |
| `unk_5B6CF80` (location) | `ctor_379_0` | `1` | `-param-always-point-to-global` (via `qword_5B6CE80` location ptr) |
| `byte_5B6D4C0` | `ctor_381` @ `0x495F80` | `1` | `-nvvm-lower-printf` |
| `byte_5B6D580` | `ctor_382` @ `0x496190` | `0` | `-dump-process-restrict` |
| `qword_5B6D640` (lo32) | `ctor_382` | `1` | `-process-restrict` (master enable) |
| `byte_5B6D700` | `ctor_382` | `0` | `-apply-multi-level-restrict` |
| `byte_5B6D7C0` | `ctor_382` | `0` | `-allow-restrict-in-struct` |
| `qword_5B6E480` (header) | `ctor_385` @ `0x4975D0` | empty | `-select-kernel-range` (`cl::list`) |
| `qword_5B6E580` (header) | `ctor_385` | empty | `-select-kernel-list` (`cl::list`) |
| `qword_5B6E848` (header) | `ctor_387` @ `0x497BF0` | empty | NVPTXSetFunctionLinkages range list (`cl::list`) |
| `qword_5B6E908` (header) | `ctor_387` | empty | NVPTXSetFunctionLinkages name list (`cl::list`) |

A second class of gates is .bss-resident with no static writer: they
default to zero and flip the first time their runtime consumer touches
them, behaving as a one-shot latch. They never appear in `--help`
because they are never bound to `cl::opt` storage -- pure runtime state.
Examples: `byte_5B6AF80` (`scev-cgp-check-latency` cache, written by
`sub_27F7D20`), `byte_5B6B4C0` (BASR pre-filter predicate, written by
`sub_2800C10`), `dword_5B6B7C0` and `dword_5B6B940` (SCEV-CGP runtime
counters), and `byte_5B6D260` (MSPO cfg-selector switch set on first call
to `sub_2862FD0`).

## How env vars get propagated to passes

The lifecycle is split across three phases. **Phase 1 (ctor time):** at
dynamic-linker init the C++ static constructors `ctor_3xx` run and write
default values into the `0x5B6xxxx` band, simultaneously calling
`sub_4534CC0(..., "<name>", <len>)` to register each gate's textual flag
name with LLVM's CommandLine global registry. A few env vars are pulled
this early too -- `LLVM_OVERRIDE_PRODUCER` in `ctor_611` lands in
`qword_5BDF538` before `main` ever runs, so changing it post-launch has no
effect. **Phase 2 (`main` startup):** the driver invokes
`llvm::cl::ParseCommandLineOptions`, which walks `argv` and overwrites any
gate whose `--<name>` flag appears, leaving all others at their
ctor-installed defaults. The driver then calls `sub_5773C0` to populate
its toolkit-root `std::string` from `CUDA_ROOT` / `CUDA_HOME` / `CUDA_PATH`
(or `/proc/self/exe`), and `sub_1A41D30` does the same for `NVVM` later
when libnvvm is asked to locate libdevice. **Phase 3 (per-pass /
per-kernel):** consumer passes read the gates directly through the global
addresses cached at compile time -- there is no `getOption()` indirection
at the call site, the compiler emitted a literal `mov` from
`0x5B6xxxx`. The TILEIR-prefixed env vars are an exception to this static
ladder: each is fetched on first use inside its consumer (`sub_7B6970`,
`sub_7A9D60`, `sub_8D9DD0`, `sub_12C8DF0`), bypassing the gate band
entirely because they were never registered with CommandLine. The result
is two parallel surfaces -- the `cl::opt`-backed gates that respond to
both `--flag` and (for a handful) env vars, and the standalone TILEIR /
TILE\_AS env vars that have no `--flag` equivalent and are reachable only
by setting the variable.
