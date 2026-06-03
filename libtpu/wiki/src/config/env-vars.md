# Environment Variables

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

libtpu reads its configuration from two sources that look the same from a shell but are wired completely differently inside the `.so`. The first is a small, fixed set of environment variables that the TPU code reads **directly** through `getenv("LITERAL_NAME")` — these are the variables a reimplementer must hard-code, because the name string is baked into a specific reader function and there is no registry behind them. The second, and far larger, surface is the absl/XLA **flag** machinery: `XLA_FLAGS`, `TF_XLA_FLAGS`, and the `--xla_*` / `--tpu_*` flags injected through `LIBTPU_INIT_ARGS` are not read by `getenv` at all — they are parsed once at bootstrap by `ParseCommandLineNonHelpFlags` against the absl flag tables the `dlopen` constructor storm pre-registered. This page owns the **direct-`getenv` catalog**: every literal env-var name the binary passes to `getenv`, the function that reads it, and what the value does. The flag surface and its injection channel live elsewhere (see the cross-references).

The decisive fact for a reimplementer is the line between the two. A grep of `.rodata` finds hundreds of `TPU_*`, `MEGASCALE_*`, `GRPC_*`, and `TF_*` strings, but most are *not* env-var reads: `TPU_CORE_TYPE_*`, `TPU_SEQUENCER_TYPE_*`, and similar are enum-name strings; `TPU_VISIBLE_DEVICES`, `TPU_HOST_BOUNDS`, and `TPU_SKIP_MDS_QUERY` are **absl flag names**, bound by the flag parser, not by `getenv`; the `MEGASCALE_*` and `GRPC_*` families are flag/option strings consumed by the distributed-runtime and gRPC layers respectively. Only ~205 distinct strings reach a literal `getenv()` in the decompiled corpus, and of those only a handful are TPU-specific; the rest are inherited from vendored libraries (hwloc, libpfm, gRPC, GCS, the Google `base/` init runtime, absl test harness). The catalog below separates the **TPU-owned direct reads** (CONFIRMED reader + address) from the **flag-bound** names (CONFIRMED string, parsed not `getenv`'d) so a reimplementer knows which to implement as `getenv` and which to implement as a flag.

A note on `secure_getenv`: despite the dispatch hardening elsewhere in the runtime, **no literal-argument `secure_getenv("NAME")` call survives in the decompiled corpus** — every confirmed env read is plain `getenv`. The bootstrap is not running with elevated privileges by the time it parses its environment, so the runtime does not bother to drop env access for setuid contexts.

| | |
|---|---|
| **Flag-injection channel** | `LIBTPU_INIT_ARGS` → `GetLibTpuInitArguments @ 0x20ccca20` (851 B) |
| **Lock + topology gate reader** | `tensorflow::tpu::TryAcquireTpuLock @ 0x20ccbc40` (3531 B) |
| **TfRT runtime selector** | `tpu::ShouldUseTfrt @ 0x1d0fc800` reads `ENABLE_TFRT_TPU_RUNTIME` |
| **Megascale gate** | `PJRT_Client_Create @ 0xe6a8840` reads `SKIP_MEGASCALE_PJRT_CLIENT` |
| **Uptime telemetry reader** | `InitializeUptimeMetricViaEnvironmentVariables @ 0x20a65720` reads `TPU_ML_PLATFORM[_VERSION]` |
| **Premapped-buffer reader** | `TpuStatesManager::GetOrCreateTpuSystemState @ 0xf956e40` |
| **`XLA_FLAGS` / `TF_XLA_FLAGS`** | NOT `getenv`'d — parsed by the absl/tsl flag-from-env machinery |
| **`TPU_LIBRARY_PATH`** | set by the wheel's `__init__.py`, read by the *framework* loader, not by libtpu |
| **Distinct literal `getenv` args (whole `.so`)** | ~205 (mostly vendored: hwloc, libpfm, gRPC, GCS, Google base) |
| **`secure_getenv` literal-arg sites** | none observed |
| **Confidence** | CONFIRMED = literal `getenv("NAME")` in decompiled reader at the cited address |

---

## How to Read This Catalog

Each row carries a **Reader** (the function and address that consumes the variable) and a **Confidence** that means a specific thing here:

- **CONFIRMED** — the binary contains a literal `getenv("NAME")` (or the flag string for a flag-bound row) inside the cited function. The reimplementer should reproduce this exactly.
- **HIGH** — the env-var string is present and its consuming subsystem is identified, but the precise reader call was inferred from the surrounding function rather than pinned to a single `getenv` line.
- **LOW** — the string is present but whether it is *read* (vs. defined as a flag name, an enum name, or a value emitted into a log) was not resolved.

> **GOTCHA —** a string in `.rodata` is not evidence of an env-var read. The single most common reimplementation error here is treating an absl flag name (e.g. `TPU_VISIBLE_DEVICES`) as a `getenv` target. libtpu binds those through the flag parser; calling `getenv("TPU_VISIBLE_DEVICES")` in a reimplementation would read a variable libtpu never reads. The catalog marks every flag-bound name explicitly.

---

## 1. Flag Injection

The primary configuration channel. A plugin `.so` has no command line of its own, so libtpu fabricates one from `LIBTPU_INIT_ARGS` and feeds it to the absl flag parser. The *mechanics* of the ingest — the space split, the `argv[0]` synthesis, the `vector<string>` → `char**` flatten — are documented in full on [../lifecycle/tftpu-initialize-bootstrap.md](../lifecycle/tftpu-initialize-bootstrap.md) §2; the *flags that string carries* are catalogued on [xla-flag-atlas.md](xla-flag-atlas.md). This page records only the env-var reads themselves.

| Variable | Reader (symbol @ addr) | Effect | Default | Confidence |
|---|---|---|---|---|
| `LIBTPU_INIT_ARGS` | `tensorflow::tpu::GetLibTpuInitArguments @ 0x20ccca20` | Space-split into an argv-style `vector<string>`; prepended with `"./tpu_driver"` and Cloud-TPU defaults; handed to `ParseCommandLineNonHelpFlags`. The injection point for every `--xla_*` / `--tpu_*` flag. | unset → empty argv (no injected flags) | CONFIRMED |
| `XLA_FLAGS` | absl/tsl `ParseFlagsFromEnvAndDieIfUnknown` (string `@ .rodata`, **not** `getenv`'d by TPU code) | Standard XLA flag-from-env channel; merged with the `LIBTPU_INIT_ARGS` argv into the same absl flag tables. | unset → no env flags | CONFIRMED (string present; parsed by flag machinery, not `getenv`) |
| `TF_XLA_FLAGS` | absl/tsl flag-from-env (string `@ .rodata`) | TensorFlow-bridge XLA flag channel; same flag tables. | unset | HIGH |

> **NOTE —** `LIBTPU_INIT_ARGS` is the *only* TPU-specific variable in this group read by a literal `getenv`. `XLA_FLAGS` / `TF_XLA_FLAGS` are read by the generic absl flag-from-env code (`absl::ParseCommandLine` reads them by way of `ABSL_FLAGS_FROM_ENV`-style scanning), so they are environment variables in effect but are not `getenv`'d at any TPU call site. A reimplementer must wire all three into one flag parse, but only `LIBTPU_INIT_ARGS` is a hand-rolled `getenv` in the TPU layer.

---

## 2. Device Selection and Topology

These variables describe the slice geometry the process should see. The split here is sharp and easy to get wrong: the **bounds** variables are read directly by `getenv` inside the lock gate, while the **visible-device** selectors are absl flag names.

### Direct `getenv` reads

| Variable | Reader (symbol @ addr) | Effect | Default | Confidence |
|---|---|---|---|---|
| `TPU_CHIPS_PER_HOST_BOUNDS` | `tensorflow::tpu::TryAcquireTpuLock @ 0x20ccbc40` | Per-host chip grid (`x,y,z`) used when computing the device lock and the local topology footprint. Read alongside the lock acquisition. | unset → derived from detected hardware | CONFIRMED |
| `TPU_CHIPS_PER_PROCESS_BOUNDS` | `tensorflow::tpu::TryAcquireTpuLock @ 0x20ccbc40` | Per-process chip grid; bounds the chips this process claims, narrowing the host bounds for multi-process-per-host layouts. | unset → equals host bounds | CONFIRMED |
| `ALLOW_MULTIPLE_LIBTPU_LOAD` | `tensorflow::tpu::TryAcquireTpuLock @ 0x20ccbc40` | When set, relaxes the single-loader cross-process lock so more than one libtpu instance may bind the device. | unset → single-load enforced | CONFIRMED |
| `TPU_LOAD_LIBRARY` | `tensorflow::tpu::TryAcquireTpuLock @ 0x20ccbc40` | Gates whether the cross-process TPU lock is even attempted (see [../lifecycle/tftpu-initialize-bootstrap.md](../lifecycle/tftpu-initialize-bootstrap.md) §5). | unset → lock attempted | CONFIRMED |

### Flag-bound names (NOT `getenv`'d)

| Name | Where bound | Effect | Confidence |
|---|---|---|---|
| `TPU_VISIBLE_DEVICES` | absl flag table | Comma-separated device index allow-list; consumed by the device enumerator via the flag parser, not `getenv`. | CONFIRMED (string present; flag-bound) |
| `TPU_VISIBLE_CHIPS` | absl flag table | Chip-level visibility allow-list (newer spelling alongside `TPU_VISIBLE_DEVICES`). | HIGH |
| `TPU_VISIBLE_DEVICE_PATHS` | absl flag table | Explicit device node paths to bind. | HIGH |
| `TPU_HOST_BOUNDS` | absl flag table | Host grid (`x,y,z`) — the *flag* form, distinct from the `getenv`'d `TPU_CHIPS_PER_HOST_BOUNDS`. | CONFIRMED (string present; flag-bound) |
| `TPU_SKIP_MDS_QUERY` | absl flag table | Skips the metadata-server topology query, forcing reliance on the locally supplied bounds/topology. | CONFIRMED (string present; flag-bound) |
| `TPU_TOPOLOGY_WRAP` / `TPU_TOPOLOGY_ALT` | absl flag table | Torus wrap mode / alternate topology selector for the slice geometry. | HIGH |
| `TPU_MEGACORE` | absl flag table | Megacore pairing mode for the chip generation. | LOW |
| `TPU_ACCELERATOR_TYPE` | absl flag table | Accelerator-type label (e.g. `v5e-4`), mapped to a topology by the PJRT topology name table. | LOW |

> **QUIRK —** there are *two* host-bounds knobs with different plumbing. `TPU_CHIPS_PER_HOST_BOUNDS` is a real `getenv` read inside `TryAcquireTpuLock`; `TPU_HOST_BOUNDS` is a flag name parsed by absl. They overlap in meaning but reach the runtime by different paths — a reimplementer must reproduce the first as `getenv` and the second as a flag, or one of the two will silently do nothing.

---

## 3. Runtime Mode and Buffer Tuning

Direct `getenv` reads that select the runtime backend and tune transfer buffers.

| Variable | Reader (symbol @ addr) | Effect | Default | Confidence |
|---|---|---|---|---|
| `ENABLE_TFRT_TPU_RUNTIME` | `tpu::ShouldUseTfrt @ 0x1d0fc800` (lambda `$_0`) | Selects the TfRT-based TPU runtime path over the legacy StreamExecutor path. Read once by a `ShouldUseTfrt` predicate. | unset → backend default | CONFIRMED |
| `SKIP_MEGASCALE_PJRT_CLIENT` | `pjrt::tpu_plugin::PJRT_Client_Create @ 0xe6a8840` | When set, `PJRT_Client_Create` (PJRT slot 15) bypasses wrapping the client in the Megascale multi-slice client and returns the single-slice client directly. | unset → Megascale client built when multi-slice config present | CONFIRMED |
| `TPU_PREMAPPED_BUFFER_SIZE` | `xla::TpuStatesManager::GetOrCreateTpuSystemState @ 0xf956e40` | Size (bytes) of the pre-mapped DMA staging buffer reserved per TPU system. | unset → runtime-chosen size | CONFIRMED |
| `TPU_PREMAPPED_BUFFER_TRANSFER_THRESHOLD_BYTES` | `xla::TpuStatesManager::GetOrCreateTpuSystemState @ 0xf956e40` | Transfer-size threshold above which the pre-mapped buffer path is used instead of per-transfer mapping. | unset → runtime default threshold | CONFIRMED |
| `DISABLE_HOST_SEND_RECV_REGISTRATION` | `_GLOBAL__sub_I_sendrecv_ops.cc @ 0x212c9af0` (static ctor) | Suppresses registration of the host-side send/recv ops at module-init time. Read in a file-static constructor during the `dlopen` storm. | unset → host send/recv registered | CONFIRMED |
| `PJRT_NPROC` | (PJRT process-count probe; string present) | Number of PJRT processes participating; used for sizing/partitioning. | unset → derived | HIGH |
| `CLOUD_TPU_TASK_ID` | (Cloud-TPU task identity; string present) | This process's task index within the Cloud-TPU job. | unset → 0 / derived | HIGH |

---

## 4. Distributed / Megascale and Coordination

The multi-slice (Megascale) layer is configured almost entirely by **flag-bound** `MEGASCALE_*` strings, not by `getenv`. The one direct `getenv` in the Megascale path is the bypass switch in §3 (`SKIP_MEGASCALE_PJRT_CLIENT`). The coordination/topology exchange that consumes these is documented on [../megascale/bootstrap/overview.md](../megascale/bootstrap/overview.md).

| Name | Consumer | Effect | Confidence |
|---|---|---|---|
| `MEGASCALE_COORDINATOR_ADDRESS` | Megascale bootstrap (flag/option) | Address of the coordination service used for cross-slice rendezvous and coordinator election. | CONFIRMED (string present; option-bound) |
| `MEGASCALE_NUM_SLICES` | Megascale bootstrap | Total slice count in the multi-slice job; drives barrier and topology-exchange sizing. | CONFIRMED (string present) |
| `MEGASCALE_SLICE_ID` | Megascale bootstrap | This process's slice index. | CONFIRMED (string present) |
| `MEGASCALE_PORT` / `MEGASCALE_DEBUG_PORT` | Megascale transport | Service / debug listen ports for the inter-slice transport. | HIGH |
| `MEGASCALE_TRANSPORT_TYPE` | Megascale transport | Selects the cross-slice transport (e.g. gRPC vs. DCN). | HIGH |
| `MEGASCALE_TOPOLOGY` | Megascale bootstrap | Multi-slice topology descriptor. | HIGH |
| `MEGASCALE_AUTHENTICATION` | Megascale transport | Auth mode for the coordination channel. | LOW |
| `MEGASCALE_TRACING` / `MEGASCALE_GRPC_ENABLE_XOR_TRACER` | Megascale tracing | Enables Megascale request tracing / the gRPC XOR tracer. | LOW |
| `TPU_WORKER_ID` | distributed init (flag/string) | Worker index within the distributed TPU job. | HIGH |
| `TPU_WORKER_HOSTNAMES` | distributed init (flag/string) | Comma-separated worker hostnames for the job. | HIGH |
| `TF_TASK_ID` / `TF_JOB_NAME` | `getenv` (TF distributed identity) | Task index / job name in a TensorFlow distributed setup. Read by literal `getenv`. | CONFIRMED |

> **NOTE —** the `MEGASCALE_*` family is consumed by the multi-slice runtime through its options/flag parsing rather than discrete `getenv` calls, which is why none appear in the literal-`getenv` set even though all ten strings are present in `.rodata`. The bypass (`SKIP_MEGASCALE_PJRT_CLIENT`) is the exception: it is a genuine `getenv` inside `PJRT_Client_Create`, because it must short-circuit *before* any Megascale option parsing happens.

---

## 5. Profiling, Dump, and Telemetry

A mix of direct `getenv` reads (telemetry platform labels, TF graph dumps) and flag-bound dump directives. The `XLA_FLAGS`-driven HLO dump knobs (`--xla_dump_to`, etc.) are **not** env vars — they are flags carried through the `LIBTPU_INIT_ARGS` channel (see [xla-flag-atlas.md](xla-flag-atlas.md)).

| Variable | Reader / Consumer | Effect | Confidence |
|---|---|---|---|
| `TPU_ML_PLATFORM` | `libtpu::telemetry::InitializeUptimeMetricViaEnvironmentVariables @ 0x20a65720` | ML-platform label (e.g. the framework name) stamped onto the uptime/runtime telemetry gauge. | CONFIRMED |
| `TPU_ML_PLATFORM_VERSION` | `libtpu::telemetry::InitializeUptimeMetricViaEnvironmentVariables @ 0x20a65720` | Platform version string for the same telemetry gauge. | CONFIRMED |
| `TF_DUMP_GRAPH_PREFIX` | `getenv` (TF graph dump) | Directory prefix for TensorFlow graph dumps. Read by literal `getenv` (multiple sites). | CONFIRMED |
| `TF_DUMP_GRAPH_NAME_FILTER` / `_GROUPS` / `_WRAPPED` / `_FMT` | `getenv` (TF graph dump) | Name filter / grouping / wrap / format controls for graph dumps. | CONFIRMED |
| `TF_GRAPH_TO_HLO_COMPILER_DUMP_DIR` | `getenv` | Dump directory for the graph→HLO compiler. | CONFIRMED |
| `TF_LOG_XLA_ACTIVITY` | `getenv` | Enables XLA activity logging. | CONFIRMED |
| `MLIR_CRASH_REPRODUCER_DIRECTORY` | `getenv` (MLIR) | Directory for MLIR crash reproducers. | CONFIRMED |
| `MLIR_BRIDGE_LOG_ENABLE_ONLY_TOP_LEVEL_PASSES` | `getenv` (MLIR) | Restricts MLIR bridge logging to top-level passes. | CONFIRMED |
| `XPROF_SKIP_DROP_EXCESS_XPLANE_BYTES` | `getenv` (xprof) | Profiler XPlane byte-budget control. | CONFIRMED |
| `TPU_CORE_DUMP_DIRECTORY` | flag/option (string present) | Directory for TPU core dumps. | LOW |
| `TPU_LOG_DIR` / `TPU_MAX_LOG_SIZE_MB` | flag/option (string present) | TPU log directory / size cap. | LOW |
| `TPU_VMODULE` / `TPU_VLOG_LEVEL` / `TPU_STDERR_LOG_LEVEL` | flag/option (string present) | Per-module / global / stderr verbose-logging levels. | LOW |

---

## 6. Inherited (Non-TPU) Environment

libtpu statically links a large stack of Google and third-party libraries, each of which reads its own environment. These are present and *functional* in the loaded `.so` but are **not** part of the TPU configuration surface a reimplementer of the TPU runtime needs to reproduce. They are catalogued by family rather than enumerated, because the list runs to ~180 distinct strings and reproducing them is reproducing those libraries, not libtpu.

| Family | Example variables | Origin | Confidence |
|---|---|---|---|
| Process bring-up | `GOOGLE_LOG_DIR`, `GOOGLE_STDERRTHRESHOLD`, `GOOGLE_MLOCK_HINT`, `GOOGLE_MAX_LOG_MB`, `GOOGLE_DEBUG_ON_FAILURE` | Google `base/` init runtime (runs inside `RealInitGoogle`) | CONFIRMED |
| Topology discovery | `HWLOC_*` (~50 vars: `HWLOC_XMLFILE`, `HWLOC_COMPONENTS`, `HWLOC_FSROOT`, …) | vendored hwloc | CONFIRMED |
| PMU / profiling | `LIBPFM_*`, `CPUPROFILE_*`, `FREQUENCY`, `JITDUMPDIR` | vendored libpfm / CPU profiler | CONFIRMED |
| gRPC | `GRPC_*` (large family) | vendored gRPC | CONFIRMED (strings present; read by gRPC, not TPU code) |
| Cloud storage | `GCS_*` (~30 vars), `GCE_METADATA_HOST`, `GOOGLE_APPLICATION_CREDENTIALS`, `NO_GCE_CHECK` | TF/GCS filesystem | CONFIRMED |
| Symbolization | `LLVM_SYMBOLIZER_PATH`, `LLVM_DISABLE_SYMBOLIZATION`, `LLVM_OVERRIDE_PRODUCER` | vendored LLVM | CONFIRMED |
| Accelerator paths | `CUDA_HOME` / `CUDA_PATH` / `CUDA_ROOT`, `ROCM_HOME` / `ROCM_PATH` / `ROCM_ROOT` | XLA host probes (no effect on the TPU path) | CONFIRMED |
| Test harness | `TEST_TMPDIR`, `TEST_SRCDIR`, `TEST_UNDECLARED_OUTPUTS_DIR`, `UNITTEST_ON_BORG`, `XML_OUTPUT_FILE` | Google test runtime (dormant in production) | CONFIRMED |
| Standard POSIX | `HOME`, `PATH`, `PWD`, `TMPDIR` / `TMP` / `TEMP`, `TZ` / `TZDIR` | libc / TF utilities | CONFIRMED |

> **NOTE —** `XLA_ALLOW_GET_DEFAULT_PLATFORM` is a genuine literal `getenv` read (in the XLA platform-manager layer), but it gates XLA's *default-platform fallback*, not anything TPU-specific. It is listed here rather than in §2 because a TPU-only reimplementation never reaches the code that consults it.

---

## 7. The `TPU_LIBRARY_PATH` Special Case

`TPU_LIBRARY_PATH` is the one variable users most associate with libtpu, yet libtpu.so itself does **not** `getenv` it. The wheel's Python wrapper sets it:

```python
# libtpu/__init__.py (from the wheel)
if not os.environ.get('TPU_LIBRARY_PATH'):
    os.environ['TPU_LIBRARY_PATH'] = get_library_path()   # path to this libtpu.so
```

The variable is then read by the **framework loader** (JAX/PJRT's plugin-discovery code in the host process) to locate the `.so` to `dlopen`. By the time libtpu's own code runs, the path has already done its job. A reimplementer of libtpu does not implement `TPU_LIBRARY_PATH`; a reimplementer of the *plugin loader* does. The two confirmed string references to `TPU_LIBRARY_PATH` in the report are this Python assignment and read, not a `getenv` inside the binary.

> **QUIRK —** the wheel ships `TPU_LIBRARY_PATH` as a *set-if-unset* in `__init__.py`, so importing `libtpu` is what wires the framework to the bundled `.so`. Setting `TPU_LIBRARY_PATH` in the environment *before* import overrides which `libtpu.so` is loaded — the bundled `__init__.py` honors a pre-existing value. This is the override hook for pointing JAX at a custom libtpu build.

---

## Cross-References

- [overview.md](overview.md) — the configuration-surface map: flags, knobs, the compilation environment, and where env vars sit among them
- [../lifecycle/tftpu-initialize-bootstrap.md](../lifecycle/tftpu-initialize-bootstrap.md) — the `LIBTPU_INIT_ARGS` ingest mechanics (space split, `argv[0]` synthesis, the flag parse) and the `TPU_LOAD_LIBRARY` lock gate
- [xla-flag-atlas.md](xla-flag-atlas.md) — the `--xla_*` / `--tpu_*` flags that `LIBTPU_INIT_ARGS` and `XLA_FLAGS` actually inject
- [flag-families.md](flag-families.md) — how the flag names are grouped and prefix-dispatched once parsed
- [../megascale/bootstrap/overview.md](../megascale/bootstrap/overview.md) — the multi-slice bootstrap that consumes the `MEGASCALE_*` options and is gated by `SKIP_MEGASCALE_PJRT_CLIENT`
