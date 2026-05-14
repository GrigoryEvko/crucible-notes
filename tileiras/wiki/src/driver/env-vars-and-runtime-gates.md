# Driver Env Vars + Runtime Gates

## Abstract

`tileiras` carries two configuration surfaces beyond ordinary command-line
options. First, a small set of environment variables drives toolkit
discovery, ptxas knob forwarding, TMA policy, swizzle selection, and a
TileAS shared-memory debug escape hatch. Second, a family of pass-level
runtime gates lives as LLVM command-line options — not process environment
variables, but worth documenting here because they shape the same
compile-time behavior.

Environment-variable parsing is uneven by design. Some variables are
presence-only, some require an exact value, and one parses a base-10
integer. A faithful reimplementation should preserve those differences
rather than collapse every variable into a generic boolean parser.

## Per-Feature Runtime Gates

Each variable has a known consumer sub_ADDR and a known parse mode. The
parse modes vary on purpose: some are presence-only, one demands the literal
byte `"1"`, one parses a base-10 integer through `strtol`, and one is a path
forwarded verbatim into an `argv` slot. A faithful reimplementation must
preserve those differences rather than collapse them into a single boolean
parser.

| Variable | Consumer | Parse mode | Default behaviour | Hazard |
| --- | --- | --- | --- | --- |
| `TILEIR_DELAY_TMA_STORE_WAIT` | (varies) | `strtol` | `0` (no delay) | `strtol` failure on non-numeric input throws a `std::stoi`-shaped exception that aborts the process |
| `TILEIR_DEBUG_DUMP_BC` | (varies) | presence | false | Setting to anything (including `"0"`) enables BC dump |
| `TILEIR_DEBUG_DUMP_LLVM` | (varies) | presence | false | Setting to anything enables LLVM-IR dump |
| `TILEIR_PREFER_TMA_FOR_LOAD_STORE` | (varies) | string boolean (`"true"` / `"false"`) | false | Prefers TMA lowering over the default cp.async / vector heuristic |
| `TILEIR_ALWAYS_SWIZZLE` | (varies) | presence | normal swizzle heuristic | Setting to anything (including `"0"`) forces the swizzled-layout path |
| `TILE_AS_DEBUG_UNLIMITED_SMEM` | RCB | presence | SMEM cap = `232448` B | Setting to anything overrides the cap to `INT_MAX`; on hardware with less than `232448` B SMEM this miscompiles |
| `TILE_AS_DEBUG_VERBOSE` | (varies) | string-eq `"1"` | false | Compared to `"1"` only; other truthy strings like `"true"` are ignored |
| `MLIR_ENABLE_EVO` | (varies) | presence | false | Internal experimental switch; behaviour is undocumented and subject to silent change |
| `PTX_KNOBS_PATH` | ptxas subprocess | path string | (none) | Forwarded verbatim as `--knobs-file=<path>` into the ptxas argv |

Three operational details matter. First, `MLIR_ENABLE_EVO` and
`PTX_KNOBS_PATH` form an AND gate inside the ptxas-argv builder — setting
only one does not forward a knob file. Second, the presence-only gates do
not strip the value: assigning `"0"` or `"false"` still enables them,
opposite of what a deployment-script reader usually expects. Third,
`TILE_AS_DEBUG_VERBOSE` is a string-equality compare against `"1"`, so
`"true"` and `"yes"` are silently ignored.

The `TILEIR_DELAY_TMA_STORE_WAIT` hazard deserves its own paragraph because
the failure mode is brutal. The consumer parses the value with `strtol`,
but the calling path treats parse failure as an exception (a
`std::stoi`-shaped `std::invalid_argument`) that percolates up unhandled.
The result is `SIGABRT` on terminate. Setting
`TILEIR_DELAY_TMA_STORE_WAIT=foo` instead of `TILEIR_DELAY_TMA_STORE_WAIT=0`
produces no warning and no fallback to default — it aborts the compile.
Deployment scripts should either omit the variable entirely or set a
decimal integer.

## CUDA-Root Resolution

CUDA-root resolution is trickier than it looks: two separate resolvers live
in the binary and they disagree on the miss path. The driver-side resolver
`sub_5773C0` fires early during command-line processing; the NVVM-side
resolver `sub_1A41D30` fires later from inside libdevice and libnvvm lookup.
Both walk the same env-var chain — `CUDA_ROOT`, then `CUDA_HOME`, then
`CUDA_PATH` — but they part ways when every variable is unset.

| Resolver | sub_ADDR | Chain | Miss behaviour |
| --- | --- | --- | --- |
| Driver | `sub_5773C0` | `CUDA_ROOT` → `CUDA_HOME` → `CUDA_PATH` | Walks `/proc/self/exe` up two directories; aborts with `"cannot find CUDA installation"` if that path does not exist |
| NVVM | `sub_1A41D30` | `CUDA_ROOT` → `CUDA_HOME` → `CUDA_PATH` | Returns the empty string; no `/proc/self/exe` fallback, no abort |

The divergence is the hazard. A deployment that leaves `CUDA_ROOT` unset
but keeps `/proc/self/exe` resolvable inside the expected toolkit layout
sees the driver succeed silently. Later, when the NVVM path tries to locate
`libdevice.10.bc`, its resolver returns `""`, and the libdevice loader
joins that empty string against `nvvm/libdevice/libdevice.10.bc`. The user
gets a confusing `"libdevice.10.bc not found in $CUDA_ROOT/nvvm/libdevice"`
error even though the driver itself reported no problem. The workaround is
mechanical: always export `CUDA_ROOT` explicitly in production deployments,
even when `/proc/self/exe` would have been enough for the driver alone.

## Runtime-gate globals (static-ctor populated)

The runtime-gate layer is ordinary LLVM option storage for individual
passes. These flags help when debugging pass behavior, but `getenv` never
reads them.

| Gate family | Representative flags | Default behavior |
| --- | --- | --- |
| CDP inline pretreat | CDP launch-name table | Recognizes the known CDP launch helper names. |
| Unsafe algebra | `-opt-unsafe-algebra` | Enabled. |
| Dead barrier elimination | `-basic-dbe` | Disabled unless requested. |
| SCEV-CGP | `-scev-cgp-*`, `-do-scev-cgp`, `-do-function-scev-cgp` | Enabled with bounded search budgets. |
| Base-address strength reduction | `-do-base-address-strength-reduce` | Enabled at level `4`. |
| DOT and FFMA fusion | `-enable-dot`, `-enable-fma-to-ffma2`, `-balance-dot-chain` | DOT is on; FFMA2 fusion is off. |
| IPMSP | `-do-clone-for-ip-msp`, `-dump-ip-msp` | Clone budget is automatic; dump is off. |
| LSA | `-lsa-opt` | Enabled. |
| Memory-space optimization | `-track-indir-load`, `-dump-ir-before-memory-space-opt` | Tracking is on; dumps are off. |
| ProcessRestrict | `-process-restrict`, `-apply-multi-level-restrict` | Base restrict processing is on. |
| NVPTX printf lowering | `-nvvm-lower-printf` | Enabled. |
| Kernel selection | `-select-kernel-range`, `-select-kernel-list` | Empty selection means no narrowing. |

## Consumers

Each environment variable is consumed close to the operation it affects:

```c
PtxasArgs build_ptxas_args(const DriverState *state) {
    PtxasArgs args = default_ptxas_args(state);

    if (getenv("MLIR_ENABLE_EVO") != NULL) {
        const char *knobs = getenv("PTX_KNOBS_PATH");
        if (knobs != NULL)
            args_append(&args, concat("--knobs-file=", knobs));
    }

    return args;
}

bool unlimited_smem_debug(void) {
    return getenv("TILE_AS_DEBUG_UNLIMITED_SMEM") != NULL;
}

bool verbose_debug(void) {
    const char *value = getenv("TILE_AS_DEBUG_VERBOSE");
    return value != NULL && strcmp(value, "1") == 0;
}

int delay_tma_store_wait(void) {
    const char *value = getenv("TILEIR_DELAY_TMA_STORE_WAIT");
    if (value == NULL)
        return 0;
    // Parse path raises std::invalid_argument on bad input;
    // calling frame does not catch, so the process aborts.
    return std::stoi(value);
}
```

The two CUDA-root resolvers look structurally similar, but their miss paths
are not equivalent. The pseudocode below is the contract a reimplementation
must preserve byte-for-byte — in particular, the NVVM resolver must keep
its empty-string return, because downstream libdevice loading relies on
that sentinel to defer the actual lookup to a layered fallback that lives
outside this function.

```c
const char *resolveCudaRoot_driver(void) {              // sub_5773C0
    if (const char *p = getenv("CUDA_ROOT"))   return p;
    if (const char *p = getenv("CUDA_HOME"))   return p;
    if (const char *p = getenv("CUDA_PATH"))   return p;
    return walkSelfExeUpTwo();                          // /proc/self/exe -> ../../
}

const char *resolveCudaRoot_nvvm(void) {                // sub_1A41D30
    if (const char *p = getenv("CUDA_ROOT"))   return p;
    if (const char *p = getenv("CUDA_HOME"))   return p;
    if (const char *p = getenv("CUDA_PATH"))   return p;
    return "";                                          // hazard: no fallback
}
```
