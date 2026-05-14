# Driver CLI Options

## Abstract

The `tileiras` command-line surface has two layers. Normal users see only
the first — a small driver layer with input file, output file, target
selection, optimization level, debug mode, line info, and memcheck. The
second is the TileIR pipeline option structure, which surfaces when the
driver constructs the pass pipeline or when an integrator embeds the
pipeline directly.

The two layers reuse a few names on purpose. Driver `--opt-level` defaults
to `3`; the embedded pipeline option named `opt-level` defaults to `2`.
Treat them as separate axes unless the driver has explicitly copied the
command-line choice into the pipeline options.

## Driver Options

| Option | Values | Default | Effect |
| --- | --- | --- | --- |
| `<tileir-bytecode>` | path | required | Input bytecode buffer parsed as TileIR MLIR bytecode. |
| `--output-file`, `-o` | path | `elf.o` | Host relocatable output path. |
| `--gpu-name` | `sm_100`, `sm_103`, `sm_110`, `sm_120`, `sm_121` | `sm_100` | GPU target selected for lowering and ptxas. |
| `--host-arch` | `x86_64`, `aarch64`, `arm64ec` | platform-dependent | Host architecture used for target triples and callbacks. |
| `--host-os` | `linux`, `windows` | platform-dependent | Host operating-system component of the generated target. |
| `--sanitize` | `memcheck` | unset | Enables memcheck-oriented TileIR instrumentation. |
| `--opt-level`, `-O` | integer `0..3` | `3` | Driver optimization level. Values above `3` are rejected. |
| `--lineinfo` | boolean | `false` | Emits line information without requiring full debug mode. |
| `--device-debug`, `-g` | boolean | `false` | Enables full device debug; valid only with `-O0`. |

The driver parses these with LLVM command-line semantics — aliases are exact
aliases, boolean flags follow LLVM's normal spelling rules, and unknown
options are rejected before any compilation work starts.

## ValuesClass Int32 Codes

The four enum-valued driver options (`--gpu-name`, `--host-arch`, `--host-os`, `--sanitize`) are wired through
byte-equivalent template instantiations of `cl::opt<cl::ValuesClass>::opt`. Each builder differs only in the
string-pair table it consumes and the int32 target slot it initialises; the parser vtable layout, default handling,
and `cl::values(...)` walk are identical across all four. The resolved value is always a single int32 — string
parsing happens once at command-line time, and downstream code reads only the integer.

`--gpu-name` is built by `sub_577620` against the 5-pair table, parser vtable `&unk_59A7378`, default value `100`:

| String | int32 code | Notes |
| --- | ---: | --- |
| `"sm_100"` | 100 | Datacenter Blackwell (default) |
| `"sm_103"` | 103 | Blackwell variant |
| `"sm_110"` | 110 | Jetson Thor |
| `"sm_120"` | 120 | Consumer RTX 50** / Pro |
| `"sm_121"` | 121 | DGX Spark |

`--host-arch` is built by `sub_577950` against the 3-pair table, parser vtable `&unk_59A7468`, default `0`:

| String | int32 code | Notes |
| --- | ---: | --- |
| `"x86_64"` | 0 | Linux/Windows x86-64 |
| `"aarch64"` | 1 | ARM 64-bit |
| `"arm64ec"` | 2 | ARM64EC (Windows on ARM); uses stride-36 sub-entry of the aarch64 record |

`--host-os` is built by `sub_577C80` against the 2-pair table, parser vtable `&unk_59A7558`, default `0`:

| String | int32 code |
| --- | ---: |
| `"linux"` | 0 |
| `"windows"` | 1 |

`--sanitize` is built by `sub_577FB0` against the 1-pair table, parser vtable `&unk_59A7648`, default `0`:

| String | int32 code | Notes |
| --- | ---: | --- |
| (unset) | 0 | No sanitizer |
| `"memcheck"` | 1 | Activates the `-sanitize=memcheck -g-tmem-access-check` nvdisasm tail |

`sub_40FD330` reads the resolved `host-arch` int and applies stride 39 for `x86_64` (code 0), stride 36 for
`aarch64` (code 1), and stride 36 for `arm64ec` (code 2 — which uses a sub-entry of the aarch64 record). This is
the only place `arm64ec` differs from `aarch64`. `sub_40FD7E0` reads the resolved `host-os` int and applies
OS-index 7 for `linux` (code 0) and OS-index 15 for `windows` (code 1).

The four parser vtables `&unk_59A7378` / `&unk_59A7468` / `&unk_59A7558` / `&unk_59A7648` share an 8-slot layout:
vtable+0 typeinfo helper, +8 destructor, +16 `parse` (string → int32 map probe), +24 `print` (int32 → string
lookup), +32 `valuesDefault` (initialise from a `cl::values(...)` builder), +40 reserved, +48 reserved, +56
reserved. The `parse` slot is the only operation invoked at command-line-parse time; the `print` slot fires only
when `--help` is requested.

## Validation Algorithm

The option validator is deliberately strict. It checks the bytecode buffer
and the requested target before allocating the program handle, keeping
failure paths simple and steering clear of partially initialized session state.

```c
int validate_driver_options(const ByteSpan *input, const DriverOptions *opts) {
    if (input == NULL || input->data == NULL)
        return error("input buffer is null");                                     // code 2

    if (!is_tileir_bytecode(*input)) {
        if (looks_like_mlir_bytecode(*input))
            return error("failed to parse IR bytecode (it looks like MLIR bytecode instead)");  // code 3
        return error("input does not correspond to Tile IR bytecode");            // code 3
    }

    if (!is_supported_gpu(opts->gpu_name))
        return error("unsupported GPU target");                                   // code 2

    if ((uint32_t)opts->opt_level > 3)
        return error("invalid optimization level");                               // code 2

    if (opts->device_debug && opts->opt_level != 0)
        return error("optimized debugging is not supported, "
                     "change optimization level to 0 or disable full debug info"); // code 2

    return 0;
}
```

The diagnostic strings above are the verbatim messages emitted by `sub_57A480`; the full error-code table with severity bytes lives in [Driver Program Handle](program-handle.md#public-error-codes). The debug rule is not cosmetic — full device debug mode injects NVVM debug options that disable several code-motion and block-merge transforms, so the driver demands `-O0` rather than silently degrading an optimized build.

## Pipeline Options

The TileIR pass pipeline carries a much larger option structure. These
options matter most to integrators who build a pass pipeline directly or
expose advanced tuning flags in a higher-level tool.

| Pipeline option | Default | Effect |
| --- | --- | --- |
| `opt-level` | `2` | TileIR pipeline optimization level when invoked outside the driver wrapper. |
| `v2-opt-level` | `0` | Separate optimization level for the TileIR V2 path. |
| `num-warps` | `4` | Logical warps per CTA for scheduling and partitioning. |
| `num-ctas` | `1` | CTAs per cluster used by cluster-aware launch metadata. |
| `pipeline-strategy` | `none` | Selects no software pipeline, unspecialized, or warp-specialized flow. |
| `unspecialized-pipeline-num-stages` | `4` | Stage count for the unspecialized pipeline. |
| `dynamic-persistent` | `false` | Enables the dynamic persistent-kernel rewrite. |
| `emit-line-info` | `none` | Selects the IR snapshot used to build source line records. |
| `schedule-trace-file` | empty | Writes scheduler trace JSON when non-empty. |
| `dump-host` | empty | Dumps generated host-side callback code when non-empty. |
| `host-triple` | `native` | Host triple used by host-code generation. |
| `rrt-size-threshold` | `4096` | Resource-reservation-table compression threshold. |
| `max-constraint-iterations` | `10` | Iteration cap for resource-constraint generation. |
| `approx` | `false` | Allows approximate math in eligible lowerings. |
| `ftz` | `false` | Enables flush-to-zero math behavior. |
| `index-bitwidth` | `32` | Bit width used for MLIR index lowering; `0` means host word size. |
| `enable-random-delay` | `false` | Stress option for scheduler delay injection. |
| `enable-debug-logging` | `false` | Enables TileIR callback debug logging paths. |
| `use-nvgpucomp-libnvvm` | `false` | Routes NVVM compilation through NVGpuComp when enabled. |

The two scheduler knobs — `rrt-size-threshold` and
`max-constraint-iterations` — are compile-time controls. Lower thresholds
compress the resource reservation table earlier; lower iteration caps make
the solver stop sooner and fall back to conservative scheduling when
constraints remain unresolved.

## Effective Option Merge

A reimplementation should model the driver layer and the pipeline layer
separately, then copy only the values the driver is known to own.

```c
TileIRPipelineOptions build_pipeline_options(const DriverOptions *driver) {
    TileIRPipelineOptions opts = tileir_pipeline_default_options();

    opts.opt_level = driver->opt_level;
    opts.compute_capability = parse_sm_number(driver->gpu_name);
    opts.emit_line_info = driver->lineinfo ? LINEINFO_FROM_INPUT : LINEINFO_NONE;
    opts.device_debug = driver->device_debug;
    opts.sanitize_memcheck = driver->sanitize_memcheck;
    opts.host_arch = driver->host_arch;
    opts.host_os = driver->host_os;

    return opts;
}
```

Do not collapse `v2-opt-level` into driver `--opt-level`. It is an
independent pipeline field that defaults to `0` unless the embedding tool
sets it.

## Diagnostics Surface

Three options create artifacts useful for debugging:

| Option | Artifact |
| --- | --- |
| `--lineinfo` | Source line records in the generated device code. |
| `emit-line-info=<stage>` | A selected IR snapshot used as the line-info source. |
| `schedule-trace-file=<path>` | Chrome-timeline-style scheduler trace JSON. |
| `dump-host=<path>` | Generated host callback code. |

The driver does not check whether those paths are semantically useful beyond
ordinary file I/O. When a path is set, the corresponding pipeline stage owns
the write and reports failure through the normal compile error path.
