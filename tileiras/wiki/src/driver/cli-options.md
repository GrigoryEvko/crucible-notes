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

## Enum-valued Options as int32 Codes

The four enum-valued driver options — `--gpu-name`, `--host-arch`,
`--host-os`, `--sanitize` — are wired through `cl::opt<cl::ValuesClass>`
template instantiations that share one parser shape. Each option carries
its own `cl::values(...)` mapping table that pairs an accepted spelling
with an `int32` code, plus a default integer to use when the option is
absent. The parser walks the table once at command-line time, stores the
resulting integer, and downstream code never sees the original string.

`--gpu-name` maps spellings to the corresponding SM number and defaults
to `100`:

| String | int32 code | Notes |
| --- | ---: | --- |
| `"sm_100"` | 100 | Datacenter Blackwell (default) |
| `"sm_103"` | 103 | Blackwell variant |
| `"sm_110"` | 110 | Jetson Thor |
| `"sm_120"` | 120 | Consumer RTX 50** / Pro |
| `"sm_121"` | 121 | DGX Spark |

`--host-arch` defaults to `0`:

| String | int32 code | Notes |
| --- | ---: | --- |
| `"x86_64"` | 0 | Linux/Windows x86-64 |
| `"aarch64"` | 1 | ARM 64-bit |
| `"arm64ec"` | 2 | ARM64EC (Windows on ARM); reuses the aarch64 record at a sub-entry |

`--host-os` defaults to `0`:

| String | int32 code |
| --- | ---: |
| `"linux"` | 0 |
| `"windows"` | 1 |

`--sanitize` defaults to `0` and is the only option whose unset state
carries semantic weight downstream:

| String | int32 code | Notes |
| --- | ---: | --- |
| (unset) | 0 | No sanitizer |
| `"memcheck"` | 1 | Activates the `-sanitize=memcheck -g-tmem-access-check` nvdisasm tail |

The host-architecture lookup table is keyed by code and walked with two
strides — `39` for the x86_64 record and `36` for both aarch64 entries.
`arm64ec` reuses the aarch64 record at a distinct sub-entry; that
sub-entry is the only place the two ARM modes diverge in the host-side
code path. The host-OS index resolves to `7` for `linux` and `15` for
`windows`, both of which select a target-triple OS fragment and the
matching object-file format.

Each parser exposes an 8-slot vtable shared by all four options. The
slots are: typeinfo helper, destructor, `parse` (string → int32 map
probe), `print` (int32 → string lookup for `--help`), `valuesDefault`
initialiser, and three reserved slots. `parse` is the only operation
invoked at command-line time; `print` fires only when the user requests
help text.

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

The diagnostic strings above are the verbatim messages emitted by the
validator entry point; the full error-code table with severity bytes
lives in [Driver Program Handle](program-handle.md#public-error-codes).
The debug rule is not cosmetic — full device debug mode injects NVVM
debug options that disable several code-motion and block-merge
transforms, so the driver demands `-O0` rather than silently degrading
an optimized build.

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

A `TileIRPipelineOptions` value is the resolved configuration that reaches
the pass manager. The driver builds it in three layers, applied in order;
each layer can only overwrite fields the next layer explicitly touches, so
the precedence is unambiguous.

The first layer is the TableGen-declared per-field default. Every option
in the pipeline has a default literal written into the pass definition —
`opt-level = 2`, `num-warps = 4`, `rrt-size-threshold = 4096`, and so on.
Constructing a fresh `TileIRPipelineOptions` populates every field with
this baseline.

The second layer is the per-pass override that arrives through MLIR's
`--pass-pipeline="tileir{key=value, ...}"` syntax. When the user (or an
integrator) invokes the pipeline through that surface, MLIR's option
parser walks the brace-enclosed key=value list and writes each value into
the matching pipeline field, leaving every other field at its TableGen
default.

The third layer is driver-level legacy propagation. The command-line
driver predates the per-pass options syntax, and several user-facing
flags — `--opt-level`, `--gpu-name`, `--lineinfo`, `--device-debug`,
`--sanitize`, `--host-arch`, `--host-os` — must continue to work for
users who never type a `--pass-pipeline` argument. The driver therefore
copies each of those into the corresponding pipeline field after the
first two layers have settled.

```c
TileIRPipelineOptions make_pipeline_options(const DriverFlags &flags) {
    TileIRPipelineOptions opts;                           // TableGen defaults

    if (flags.pass_pipeline_set)
        parsePassPipelineOptions(opts, flags.pass_pipeline_text);  // brace-list overrides

    opts.opt_level          = flags.opt_level;            // legacy propagation
    opts.compute_capability = sm_number_of(flags.gpu_name);
    opts.emit_line_info     = flags.lineinfo ? LineInfo::FromInput : LineInfo::None;
    opts.device_debug       = flags.device_debug;
    opts.sanitize_memcheck  = flags.sanitize == Sanitizer::Memcheck;
    opts.host_arch          = flags.host_arch;
    opts.host_os            = flags.host_os;
    return opts;
}
```

The propagation exists because a single `--opt-level=2` should still
configure the pipeline correctly without forcing the user to spell out
`--pass-pipeline="tileir{opt-level=2}"`. A reimplementer who skips the
propagation step ends up with a tool whose `-O2` silently runs at the
pipeline default of `2` for most fields but at the driver default of `3`
in any field the driver does not propagate — a subtle divergence that
turns up only when an integrator's regression suite compares produced
SASS across versions.

Do not collapse `v2-opt-level` into driver `--opt-level`. The two are
independent axes: `v2-opt-level` defaults to `0` and is only meaningful
inside the V2 pipeline, which the driver does not select on its own.

## Diagnostics Surface

Four options produce artifacts useful for debugging:

| Option | Artifact |
| --- | --- |
| `--lineinfo` | Source line records in the generated device code. |
| `emit-line-info=<stage>` | A selected IR snapshot used as the line-info source. |
| `schedule-trace-file=<path>` | Chrome-timeline-style scheduler trace JSON. |
| `dump-host=<path>` | Generated host callback code. |

The driver does no semantic check on these paths beyond ordinary file I/O.
When a path is set, the corresponding pipeline stage owns the write and
reports failure through the normal compile error path.

## Related pages

[Driver main() Entry](main-entry.md#main) shows how `main` consumes the parsed
options; [Driver Overview](overview.md#what-the-driver-does) frames the overall
compile contract; [Driver Program Handle](program-handle.md#public-error-codes)
defines the public error-code numbering returned through the exit status;
[Host Launch ABI and ptxas Knobs](host-launch-and-ptxas-knobs.md#ptxas-knobs-file-format)
covers `--knobs-file=`, the only ptxas-side option the driver forwards.
