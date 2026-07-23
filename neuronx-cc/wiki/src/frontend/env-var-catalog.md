# Environment-Variable Catalog & the `NEURON_CC_FLAGS` Boundary

> *All symbols, strings and line numbers on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/`, native ELFs under `neuronxcc/starfish/`). The cp311/cp312 wheels carry an identical surface. Treat every offset as version-pinned.*

## Abstract

A surprising amount of folklore surrounds the Neuron compiler's environment surface, almost all of it centred on one variable — `NEURON_CC_FLAGS`. The single most important fact this page establishes is **negative**: the in-wheel compiler driver (`CommandDriver` → `Arguments` → `CompileCommand`) **never reads `NEURON_CC_FLAGS`**. There is no `getenv("NEURON_CC_FLAGS")`, no `os.environ["NEURON_CC_FLAGS"]`, anywhere in the driver. The variable is a **framework-side convention**: the XLA-Neuron PJRT plugin / `torch-neuronx` wrapper (which live *outside* this wheel) read it, token-split it, and **prepend** the resulting tokens to the `neuronx-cc compile …` argv before exec. By the time the driver's argparser sees them, they are ordinary command-line flags — identical to the flags catalogued in [3.8 (`flag-catalog`)](flag-catalog.md). The only place the wheel itself honours the variable is the NKI `BaremetalKernel._compile` subprocess path, which re-forwards it when it shells out to a child compiler for on-device benchmarking.

Everything else partitions cleanly along a **compiler-vs-runtime boundary**. A small set of `NEURON_*` variables genuinely steer in-wheel codegen — the NKI target/internal gates, the `libwalrus` scratchpad sizing knob, and a cluster of `getenv()`-read feature knobs inside the native `hlo-opt` HLO passes (`NEURON_REMAT_*`, `NEURON_COLLECTIVE_*`, `NEURON_DISABLE_*`). A second set, all prefixed `NEURON_RT_*`, belongs to the Neuron **runtime** (`libnrt`, out-of-wheel); the compiler touches them only through its runtime-*client* layer (the `kra/` package and the `Autotuner`) when it actually runs a NEFF on hardware for profiling/benchmarking. They do not affect an offline, hardware-less compile. Finally, `TF_CPP_*` / `XLA_*` are read inside the statically-linked TF/XLA dependency, not by neuronx-cc logic.

One trap is disambiguated here once and for all: **`NEURON_ASSERT*` is not an environment variable.** It is a C++ assertion macro plus an `error_injector` test-harness case-name family plus an argparse keyword list — three distinct things, none of them an `os.environ` key.

| | |
|---|---|
| **The big negative** | No `getenv`/`environ` read of `NEURON_CC_FLAGS` in `Arguments.so`, `CommandDriver.so`, `CompileCommand.so` — proven by string-table absence |
| **Only in-wheel `NEURON_CC_FLAGS` reader** | `nki/.../NumpyKernel.so` `BaremetalKernel._compile` (`environ.get` + split + `subprocess`) |
| **Pure-codegen env vars (offline compile)** | `NEURON_PLATFORM_TARGET_OVERRIDE`, `NEURON_INTERNAL_USE`, `NEURON_SCRATCHPAD_PAGE_SIZE`, `NEURON_REMAT_*` ×2, `NEURON_COLLECTIVE_*` ×3, `NEURON_DISABLE_*` ×2 |
| **Runtime-client only (needs hardware)** | `NEURON_RT_VISIBLE_CORES`, `…_VIRTUAL_CORE_SIZE`, `…_ROOT_COMM_ID`, `…_DEBUG_OUTPUT_DIR`, `…_DBG_FALLBACK_TO_SINGLE(_NC)`, `…_ONE_TMPBUF_PAGE_SIZE_MB` |
| **Third-party (embedded TF/XLA)** | `TF_CPP_MIN_LOG_LEVEL`, `TF_CPP_MAX_VLOG_LEVEL`, `TF_CPP_VMODULE`, `TF_CPP_VLOG_FILENAME`, `TF_CPP_LOG_THREAD_ID`, `XLA_FLAGS`, `XLA_HLO_DEBUG`, `XLA_IR_DEBUG` |
| **Not env vars at all** | `NEURON_ASSERT*` (macro + test harness + CLI keyword), `NEURON_ISA_TPB_*` (mangled ISA type names) |
| **Config / response files** | **None.** No `getenv`, no `fromfile_prefix_chars`, no `.cfg/.ini/.json` reader in any driver module |

---

## 1. Reimplementation Contract

A reimplementer of the neuronx-cc front end must replicate the following observable behaviour:

1. **The driver is env-agnostic.** Parse `sys.argv` only. Do **not** read `NEURON_CC_FLAGS`, nor any other variable, in the argument layer. All defaults come from argparse `default=` kwargs (see [3.2 `two-parser-architecture`](two-parser-architecture.md) and [3.9 `flag-visibility-argkind`](flag-visibility-argkind.md)). There is no config file and no `@response-file` mechanism.
2. **`NEURON_CC_FLAGS` is resolved by the caller.** If you are writing the *framework* shim, read the variable, shlex-split it, and prepend the tokens to the compiler argv. If you are writing the *compiler*, do nothing — the tokens arrive as normal argv.
3. **NKI benchmark re-forwarding.** The one in-wheel honourer (`BaremetalKernel._compile`) must, when it spawns a child compiler for on-device profiling, read `NEURON_CC_FLAGS` from the environment, split it, concatenate with the kernel's `additional_compile_opt`, and emit a `neuronx_cc … compile --framework XLA …` child argv.
4. **Codegen knobs.** Honour the NKI gates (`NEURON_PLATFORM_TARGET_OVERRIDE`, `NEURON_FRAMEWORK_DEBUG`, `NEURON_INTERNAL_USE`) at trace/lower time, `NEURON_SCRATCHPAD_PAGE_SIZE` at backend-allocation time, and the `hlo-opt` `getenv` knobs inside the named HLO passes.
5. **Runtime boundary.** Treat `NEURON_RT_*` strictly as runtime-client plumbing: read them only when running a NEFF on hardware (profile/benchmark/autotune); never let them influence an offline compile's codegen.

The classification table in [§8](#8-classification-matrix) is the centrepiece — it tags every variable by reader binary, effect, scope and confidence.

---

## 2. ⭐ `NEURON_CC_FLAGS` — the variable the driver does not read

### 2.1 The negative proof

The claim "the driver does not read `NEURON_CC_FLAGS`" is provable directly from the Cython string tables. Every wheel module is Cython-compiled, so the original Python string constants survive as interned `__pyx_n_s_` / `__pyx_n_u_` / `__pyx_k_` symbols; a module that reads an env var *must* contain both an `environ`/`getenv` symbol **and** the variable's literal. Scanning the three driver modules that form the compile entry chain:

```text
module                       NEURON_CC_FLAGS   getenv   environ   shlex
driver/Arguments.so                0             0         0        0      ← argparse model
driver/CommandDriver.so            0             0         3        3      ← dispatcher
driver/commands/CompileCommand.so  0             0         0        0      ← pipeline builder
driver/Job.so                      0             0         3        3      ← sub-tool argv
```

`Arguments.so` — the argparse construction and validation layer where the entire `--flag` catalogue lands — contains **zero** `environ`/`getenv`/`NEURON_CC_FLAGS` strings. `CompileCommand.so`, which marshals the argparse `Namespace` into the pipeline and builds every sub-tool flag string, is likewise empty of all three. The variable's literal does not appear in any driver module at all.

> **NOTE —** the literal `NEURON_CC_FLAGS` exists in exactly five wheel paths: `nki/docs/examples/prof-kernel.py` (a doc example), `nki/compiler/backends/neuron/NumpyKernel.so` (the benchmark reader), and the three native ELFs `starfish/bin/{hlo-opt,hlo2penguin,xla_infergoldens}` (a diagnostic string, see §2.4). It is absent from the entire `driver/` tree.

### 2.2 The `environ`/`shlex` in `CommandDriver.so` and `Job.so` is *not* a `CC_FLAGS` read

`CommandDriver.so` and `Job.so` do reference `environ` and `shlex` — but their use is unrelated to flag injection. Both modules' `shlex` references are method names `quote` and `join`, never `split`:

```text
CommandDriver.so  shlex methods:  join ×5   quote ×4   split ×4
Job.so            quoting:        addquotes ×14   join ×5   quote ×4
```

`CommandDriver`'s only quoting use is `shlex.quote` inside the diagnostic command-echo path — the same logic that writes the copy-pasteable replay command (`Job.getReplayCommandLine` wraps args with `addquotes`; the default log file is `log-neuron-cc.txt`, whose literal is present). It *escapes* an already-built argv for human-readable logging; it does not *split* any env var into argv. The `environ` references are generic process-environment plumbing (child-process spawning), not a keyed read of `NEURON_CC_FLAGS`.

> **GOTCHA —** Presence of `shlex` in `CommandDriver.so` is the usual source of the "the driver shlex-splits `NEURON_CC_FLAGS`" myth. It does not: the four `split` hits coexist with the command-echo path, and no `NEURON_CC_FLAGS` literal is anywhere in the module to split. Splitting *does* happen — but in the framework shim (out-of-wheel) and in the NKI benchmark path (§2.3).

### 2.3 The one in-wheel reader: `BaremetalKernel._compile`

The single place the wheel honours the variable is the NKI benchmark/baremetal compile path.

```text
reader:  nki/compiler/backends/neuron/NumpyKernel.so
symbols: BaremetalKernel ×98   BenchmarkKernel ×34   _compile ×38   NumpyKernel.py ×3
content: NEURON_CC_FLAGS ×3   environ ×3   split ×3   subprocess ×3   shlex ×3
         + additional_compile_opt + neuronxcc_flags + "neuronx_cc … compile --framework XLA"
```

Reconstructed flow (Cython decompile + `__pyx` string table):

```python
# nki/compiler/backends/neuron/NumpyKernel.py  (BaremetalKernel._compile, reconstructed)
def _compile(self, ...):
    # 1. read the framework's flag string straight from the process environment
    flags_str = os.environ.get("NEURON_CC_FLAGS", "")        # __pyx_n_u_NEURON_CC_FLAGS
    # 2. token-split it (shlex imported by the module) and merge with kernel-local opts
    neuronxcc_flags = flags_str.split() + list(self.additional_compile_opt)
    # 3. assemble the child compiler argv
    cmd = ["neuronx_cc", "compile", "--framework", "XLA", *neuronxcc_flags, ...]
    # 4. run the compiler as a child process for on-device benchmarking
    subprocess.run(cmd, ...)
```

This is NKI running the *real* compiler as a subprocess to benchmark a kernel on hardware. It re-forwards the same `NEURON_CC_FLAGS` a framework integration would, so the user's flags reach the benchmarked compile. It is the lone in-wheel place the variable is consumed.

> **NOTE —** This reader does **not** make the variable "compiler-read" in the codegen sense. It is the benchmark harness behaving as a framework would: it splits the string and prepends it to a *child* `neuronx_cc compile` argv. The child driver still never reads the env var — it just receives the already-split flags.

### 2.4 Native diagnostic — confirms the framework-prepend model

`hlo-opt`, `hlo2penguin` and `xla_infergoldens` embed `NEURON_CC_FLAGS` only inside a user-facing error string:

```text
Use NEURON_CC_FLAGS environment variable and set `--target=`.
```

This is a *diagnostic*, not a read: `getenv` is imported (5 hits in `hlo-opt`) but is used for the §5 knobs, not for `NEURON_CC_FLAGS`. The message instructs the *framework user* to pass `--target` through `NEURON_CC_FLAGS` — i.e. it documents the framework-prepend contract from inside the binary.

### 2.5 Canonical usage

The in-wheel doc example pins the value shape:

```python
# nki/docs/examples/prof-kernel.py:9-10
os.environ["NEURON_FRAMEWORK_DEBUG"] = "1"
os.environ["NEURON_CC_FLAGS"] = " --disable-dge "   # leading/trailing spaces tolerated; split discards them
```

The value is a free-form flag fragment whose tokens map 1:1 onto the [3.8 flag catalogue](flag-catalog.md) (`--disable-dge`, `--target=`, `--auto-cast`, `-O2`, …). The relationship is purely textual: `NEURON_CC_FLAGS` is the **source string**, the driver's argparser is the **sink**, and there is no in-driver merge step — the merge is a string prepend performed by the caller.

---

## 3. NKI codegen gates — `PLATFORM_TARGET_OVERRIDE` / `FRAMEWORK_DEBUG` / `INTERNAL_USE`

These three are genuine in-wheel, codegen-affecting variables, all read inside the NKI backend.

### 3.1 `NEURON_PLATFORM_TARGET_OVERRIDE` — the env equivalent of `--target`

```text
reader:  nki/compiler/backends/neuron/TraceKernel.so
symbols: NEURON_PLATFORM_TARGET_OVERRIDE   _get_platform_target   TraceKernel.py
```

`TraceKernel._get_platform_target` calls `os.environ.get("NEURON_PLATFORM_TARGET_OVERRIDE", …)`; the result feeds the platform/target used to specialize and compile the NKI kernel, overriding the auto-detected hardware arch. It is the NKI-path equivalent of the `--target`/`--arch` CLI flag (same vocabulary: `trn2`, `gen3`, etc.). The `environ.get` reader sits in `_get_platform_target`. Unset ⇒ target auto-detected, or taken from compile opts.

### 3.2 `NEURON_FRAMEWORK_DEBUG` — NKI framework-kernel debug

```text
reader: nki/compiler/backends/neuron/FrameworkKernel.so  (FrameworkKernel._debug_kernel_arg)
also:   set to "1" in nki/docs/examples/prof-kernel.py:9
```

`FrameworkKernel._debug_kernel_arg` contains a nested `has_env` closure — an env-presence test. Setting the variable enables debug handling/printing of framework-kernel argument shapes/types during NKI lowering. *Evidence: the literal, the `has_env` closure, and the `="1"` usage in the doc example.* Unset ⇒ debug off.

### 3.3 `NEURON_INTERNAL_USE` — the internal-API unlock gate

```text
readers: nki/_private/private_api.so   nki/compiler/backends/neuron/KernelBuilder.so
```

This gate unlocks non-public NKI kwargs. The binaries carry the inline documentation verbatim:

```text
private_api.so:   "… DMA QoS priority classes, which can b[e set via] the non-public
                   kwarg `dma_qos` … Requires setting environment variable
                   `NEURON_INTERNAL_USE` to `1`:"    (+ dma_qos, DMAQoSClass literals)

KernelBuilder.so: "… being used internally, based on the environment variable
                   NEURON_INTERNAL_USE`. If not, we raise an error that an unknown
                   kwarg is being [used]"
```

So `NEURON_INTERNAL_USE=1` is checked in `KernelBuilder` (the `NeuronCodegen` path) before accepting an internal kwarg such as `dma_qos`; unset ⇒ the kwarg is rejected with an "unknown kwarg" error, and the `dma_qos` value is otherwise converted to a `DMAQoSClass`. *Evidence: explicit inline documentation plus the literal in both modules.* This *is* codegen-affecting: it changes what kernel features are reachable.

---

## 4. `NEURON_SCRATCHPAD_PAGE_SIZE` and the tmpbuf fallback

```text
literal hosts:  starfish/lib/libwalrus.so   driver/jobs/WalrusDriver.so   starfish/bin/analyze_neff_artifacts.py
```

`NEURON_SCRATCHPAD_PAGE_SIZE` sizes (in MiB) the tmpbuf/scratchpad page used by the backend allocator. `libwalrus.so` carries the literal next to a `getenv` import; `WalrusDriver.so` carries it where it forwards/honours the value on the `walrus_driver` invocation. The offline analyzer pins the default exactly:

```python
# starfish/bin/analyze_neff_artifacts.py
DEFAULT_TMPBUF_PAGE_SIZE_IN_MB = 512                              # :19
...
tmpbf_page_size_mb_default = DEFAULT_TMPBUF_PAGE_SIZE_IN_MB        # :453
if os.environ.get("NEURON_SCRATCHPAD_PAGE_SIZE"):                 # :454
    tmpbf_page_size_mb_default = int(os.environ.get("NEURON_SCRATCHPAD_PAGE_SIZE"))   # :455
elif os.environ.get("NEURON_RT_ONE_TMPBUF_PAGE_SIZE_MB"):         # :456
    tmpbf_page_size_mb_default = int(os.environ.get("NEURON_RT_ONE_TMPBUF_PAGE_SIZE_MB"))   # :457
...
help="Specify size of tmpbuf page in MiB. The default is scratchpad size environment
      variable if it exists, otherwise 512 MiB."                  # :474
```

The `analyze_neff` reader is an explicit `os.environ.get` at `:454–457`; that `libwalrus`'s adjacent `getenv` is the *same* read is **[INFERRED]**. `NEURON_SCRATCHPAD_PAGE_SIZE` is the in-wheel/compiler-owned knob (compiler-exec-time, COMPILER scope).

> **QUIRK —** `NEURON_RT_ONE_TMPBUF_PAGE_SIZE_MB` is a *runtime*-origin variable (the `NEURON_RT_*` namespace), yet the offline analyzer reads it as a **fallback** (the `elif`), so a runtime-configured page size is honoured when the compiler-owned knob is unset. It is the one place a `NEURON_RT_*` value reaches a non-hardware code path — and even then only as a default for a CLI option, never as codegen.

---

## 5. Native `hlo-opt` feature knobs — `getenv` inside HLO passes

These are `NEURON_`-prefixed but are **backend feature knobs**, not runtime vars. They are read by `getenv()` inside specific HLO passes in `starfish/bin/hlo-opt` (the HLO→tensorizer frontend optimization stage that runs *before* walrus). All seven literals are present in the binary:

```text
NEURON_REMAT_LARGE_BROADCAST_MIN_SIZE_IN_MB
NEURON_REMAT_LARGE_TP_ALLGATHER_CP_LAYER
NEURON_COLLECTIVE_MATMUL_NXD
NEURON_COLLECTIVE_MATMUL_SB_TO_SB_THRESHOLD_IN_MB
NEURON_COLLECTIVE_PERMUTE_AGGRESSIVE
NEURON_DISABLE_BOUNDARY_MARKER
NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM
```

| Variable | Type | Pass / effect |
|---|---|---|
| `NEURON_REMAT_LARGE_BROADCAST_MIN_SIZE_IN_MB` | int (MiB) | min broadcast size above which a large broadcast is **rematerialized** instead of kept live (memory/recompute tradeoff) |
| `NEURON_REMAT_LARGE_TP_ALLGATHER_CP_LAYER` | int/layer-id | which tensor-parallel all-gather (context-parallel layer) gets rematerialized; tunes large-AG remat under TP/CP sharding |
| `NEURON_COLLECTIVE_MATMUL_NXD` | bool | enables the NxD (neuronx-distributed) collective-matmul lowering variant |
| `NEURON_COLLECTIVE_MATMUL_SB_TO_SB_THRESHOLD_IN_MB` | int (MiB) | SBUF→SBUF size threshold selecting the collective-matmul data-movement strategy |
| `NEURON_COLLECTIVE_PERMUTE_AGGRESSIVE` | bool | aggressive rewrite of collective-permute into all-gather form |
| `NEURON_DISABLE_BOUNDARY_MARKER` | bool (=1 disables) | disables insertion of boundary markers used by the all-reduce / dynamic-slice rewrite passes (escape hatch) |
| `NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM` | bool (=1 disables) | disables moving a dynamic-slice off a parameter (the slice-from-param movement opt) |

The `NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM` knob is the highest-confidence of the set: the binary carries the diagnostic

```text
NOTICING NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM=1
```

which pins a `getenv` read with explicit `=1` on/off semantics. The other six rest on the literal, the `getenv` import, and adjacent pass-rewrite strings; their exact numeric defaults are **[UNRESOLVED]**, not byte-decoded from the stripped logic. All are codegen-affecting and run during the HLO→tensorizer stage.

> **NOTE —** These do not appear anywhere in the Python driver — they are read by the native ELF. A reimplementer wiring them must `getenv` them *inside the corresponding HLO pass*, not at argv-parse time.

---

## 6. The runtime boundary — `NEURON_RT_*`

`NEURON_RT_*` are owned by the Neuron **runtime** (`libnrt` / the kaena runtime daemon), which is **not part of this wheel**. The compiler package touches them only through its runtime-*client* layer — the `kra/` package (Kaena Runtime Adapter: the on-device NEFF executor/profiler used for benchmarking and autotuning) and the `Autotuner`. These modules *read* the variables to mirror the runtime's core topology, or *write* them to configure child runtime processes they spawn. **They do not influence codegen of an offline compile** — they only matter when the compiler actually runs a NEFF on hardware (benchmark / autotune / profile).

| Variable | Type | In-wheel reader (proof) | Effect |
|---|---|---|---|
| `NEURON_RT_VISIBLE_CORES` | csv/range (`"0-3"`) | `kra/kralib_cc.so` `Proc.__init__` (environ get **and** set-item); `kra/profiler.so` `_get_all_ntffs` | NeuronCore set the KRA executor/profiler targets; bounds the per-core ntff enumeration loop. KRA re-sets it into each spawned worker's env |
| `NEURON_RT_VIRTUAL_CORE_SIZE` | int (LNC grouping) | `kra/profiler.so` `_get_specific_ntff` | how physical cores group into a logical NeuronCore (LNC2 etc.); profiler reads it to pick the right ntff |
| `NEURON_RT_ROOT_COMM_ID` | `host:port` | `kra/kralib_cc.so` (reads + re-sets); `kra/profile_lib.so`; `Autotuner.so` (**sets** it) | collective rendezvous endpoint for forming the comm group when benchmarking/autotuning collective kernels |
| `NEURON_RT_DEBUG_OUTPUT_DIR` | path | `kra/krtlib.so` (next to `"Initializing debug handler: "`); `nki_standalone/kernel_tracer.so` | directory where the runtime debug handler writes dumps |
| `NEURON_RT_DBG_FALLBACK_TO_SINGLE` (+ `_NC`) | bool | `kra/kralib_cc.so` (NEFF-exec debug-flag neighbourhood) | runtime debug fallback to single-core execution |
| `NEURON_RT_ONE_TMPBUF_PAGE_SIZE_MB` | int (MiB) | `analyze_neff_artifacts.py:456` (fallback) | tmpbuf page-size fallback — see [§4](#4-neuron_scratchpad_page_size-and-the-tmpbuf-fallback) |

The `Autotuner` is the clearest case of *writing* a runtime var: it embeds the full assignment literal

```text
NEURON_RT_ROOT_COMM_ID=localhost:63182
```

setting a distinct port (`63182`) for its on-device autotuning trials so they do not clash with a user runtime; the KRA default seen elsewhere is `localhost:61234`. `VISIBLE_CORES` is an environ get-and-set in `Proc.__init__`, and `ROOT_COMM_ID` has the Autotuner setting the full literal. For `VIRTUAL_CORE_SIZE`, `DEBUG_OUTPUT_DIR`, and `DBG_FALLBACK_TO_SINGLE` the literal and its reader are located but the exact branch is **[UNRESOLVED]**.

> **GOTCHA — `NEURON_RT_VISIBLE_CORES` is not a compiler flag.** It is tempting to file it as one because the compiler package reads it. The read lives in the KRA *runtime client*, fires only on the on-device benchmark / profile / autotune path, and is irrelevant to a hardware-less `neuronx-cc compile`. The same applies to the whole `NEURON_RT_*` set.

---

## 7. Third-party and non-variables

### 7.1 `TF_CPP_*` / `XLA_*` — embedded TF/XLA logging & debug

These belong to the TF/XLA library statically linked into the native bins (`hlo-opt`, `hlo2penguin`, `xla_infergoldens`, `hlo-neff-wrapper`, `snapshot-unpack`). They are read by `getenv` *inside* that embedded dependency — they configure a third party, not neuronx-cc logic. Literals confirmed in `hlo-opt`:

```text
TF_CPP_MIN_LOG_LEVEL   TF_CPP_MAX_VLOG_LEVEL   TF_CPP_VMODULE
TF_CPP_VLOG_FILENAME   TF_CPP_LOG_THREAD_ID    XLA_FLAGS   XLA_HLO_DEBUG   XLA_IR_DEBUG
```

`TF_CPP_MIN_LOG_LEVEL` and `TF_CPP_MAX_VLOG_LEVEL` are *set* by the native ELF's `main` (via `setenv`, not by the Python driver — the literals appear only in the native bins, never in a wheel `.py`/`.so`) to mute TF's startup chatter before the TF runtime initializes. `XLA_FLAGS` is the passthrough that tunes the embedded XLA compiler the HLO frontend is built on. The two log-level vars are pinned by the literal in all native bins plus the `setenv` in `hlo2penguin`'s main; the rest rest on the literal alone.

> **NOTE —** The "driver sets `TF_CPP_*`" framing means the **native ELF main** (`hlo2penguin`/`hlo-opt`), not the Python `CommandDriver.main`. No wheel `.py` sets these. A reimplementer of the Python driver should not add a `TF_CPP_*` write; the native tool does it for itself.

### 7.2 `NEURON_ASSERT*` is **not** an environment variable

The raw `NEURON_*` sweep surfaces `NEURON_ASSERT` / `NEURON_ASSERTION*` with high counts. None is an env var; they are three unrelated things:

- **`libwalrus.so` `error_injector` test harness.** `NEURON_ASSERTION_WITH_INST` / `_WITH_AP` / `_WITH_MEMLOC` / `_WITH_MEMLOCSET` / `_WITH_FORMATTING` / `NON_NEURON_ASSERTION_WRAP_EXCEPTION` are **test-case names** in a C++ error-injector. The adjacent strings make this unambiguous:

  ```text
  Simulate a NEURON_ASSERT()
  Simulate a NEURON_ASSERT() with bir::Instruction argument.
  Simulate a NEURON_ASSERT() with bir::AccessPattern argument.
  Simulate a NEURON_ASSERT() with bir::MemoryLocation argument.
  Simulate a NEURON_ASSERT() with formatted output.
  …deliberately called…           ("NEURON_ASSERT was deliberately called")
  ```
  These exercise the `NEURON_ASSERT()` C++ macro from a unit-test injector; they are not env reads.

- **`driver/Job.so` & `Arguments.so` keyword constant.** `neuron_assert`, `neuron_external_assert`, and `NEURON_ASSERT_GLOBAL_KEYWORDS` are accessed via attribute lookup (`GetAttrStr`), i.e. a module-level keyword-list constant + argparse options — part of the `--flag` surface (assertion-emission CLI controls), **not** `os.environ` keys.

- **`NEURON_ISA_TPB_*`** (≈ mangled type names, e.g. `NEURON_ISA_TPB_ADDR4EEEvRT_RKN3bir13AccessPatternEb`) and `NEURON_SRC` / `NEURON_LOG8L` are substrings of mangled C++ symbols / log tokens — excluded from the catalogue.

> **GOTCHA —** No `NEURON_ASSERT*` environment variable exists. It is listed here only to neutralise a false positive: anyone grepping `NEURON_` over the wheel will find hundreds of `NEURON_ASSERT*`/`NEURON_ASSERTION*`/`NEURON_ISA_TPB_*` hits, every one of which is a macro, a test-case name, a CLI keyword, or a mangled type — never an env var.

---

## 8. Classification matrix

The centrepiece. `Scope`: **COMPILER** = in-wheel consumer affecting an offline compile; **RUNTIME** = `libnrt`-owned, read only by the runtime client on hardware; **FRAMEWORK** = consumed out-of-wheel; **3rd-PARTY** = embedded TF/XLA.

| Variable | Reader binary (proof) | Effect | Scope | Confidence |
|---|---|---|---|---|
| `NEURON_CC_FLAGS` | `NumpyKernel.so` `BaremetalKernel._compile` only (driver: **none**) | framework prepends split tokens to compile argv; NKI benchmark re-forwards | FRAMEWORK | CERTAIN |
| `NEURON_PLATFORM_TARGET_OVERRIDE` | `TraceKernel.so` `_get_platform_target` | force NKI target platform (= `--target`) | COMPILER | CERTAIN |
| `NEURON_FRAMEWORK_DEBUG` | `FrameworkKernel.so` `_debug_kernel_arg.has_env` | dump NKI framework-kernel arg shapes/types | COMPILER | HIGH |
| `NEURON_INTERNAL_USE` | `private_api.so`, `KernelBuilder.so` | unlock internal kwargs (`dma_qos`→`DMAQoSClass`) | COMPILER | CERTAIN |
| `NEURON_SCRATCHPAD_PAGE_SIZE` | `libwalrus.so` `getenv`; `WalrusDriver.so`; `analyze_neff_artifacts.py:454` | tmpbuf/scratchpad page size (MiB), default 512 | COMPILER (exec) | CERTAIN |
| `NEURON_RT_ONE_TMPBUF_PAGE_SIZE_MB` | `analyze_neff_artifacts.py:456` (fallback) | tmpbuf page-size fallback default | RT→compiler | CERTAIN |
| `NEURON_REMAT_LARGE_BROADCAST_MIN_SIZE_IN_MB` | `hlo-opt` `getenv` (HLO pass) | large-broadcast remat threshold | COMPILER | HIGH |
| `NEURON_REMAT_LARGE_TP_ALLGATHER_CP_LAYER` | `hlo-opt` `getenv` (HLO pass) | TP all-gather remat selection | COMPILER | HIGH |
| `NEURON_COLLECTIVE_MATMUL_NXD` | `hlo-opt` `getenv` (HLO pass) | enable NxD collective-matmul lowering | COMPILER | HIGH |
| `NEURON_COLLECTIVE_MATMUL_SB_TO_SB_THRESHOLD_IN_MB` | `hlo-opt` `getenv` (HLO pass) | SBUF→SBUF strategy threshold | COMPILER | HIGH |
| `NEURON_COLLECTIVE_PERMUTE_AGGRESSIVE` | `hlo-opt` `getenv` (HLO pass) | aggressive permute→all-gather rewrite | COMPILER | HIGH |
| `NEURON_DISABLE_BOUNDARY_MARKER` | `hlo-opt` `getenv` (HLO pass) | disable boundary-marker insertion | COMPILER | HIGH |
| `NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM` | `hlo-opt` `getenv` (`=1` log proof) | disable slice-from-param movement | COMPILER | CERTAIN |
| `NEURON_RT_VISIBLE_CORES` | `kra/kralib_cc.so` `Proc.__init__`; `kra/profiler.so` | target NeuronCore set (benchmark/profile) | RUNTIME | CERTAIN |
| `NEURON_RT_VIRTUAL_CORE_SIZE` | `kra/profiler.so` `_get_specific_ntff` | LNC grouping factor | RUNTIME (LNC) | HIGH |
| `NEURON_RT_ROOT_COMM_ID` | `kra/kralib_cc.so`; `Autotuner.so` (**sets** `…=localhost:63182`) | collective rendezvous endpoint | RUNTIME (CC) | CERTAIN |
| `NEURON_RT_DEBUG_OUTPUT_DIR` | `kra/krtlib.so`; `kernel_tracer.so` | runtime debug dump dir | RUNTIME | HIGH |
| `NEURON_RT_DBG_FALLBACK_TO_SINGLE`(`_NC`) | `kra/kralib_cc.so` | runtime single-core debug fallback | RUNTIME | HIGH |
| `TF_CPP_MIN_LOG_LEVEL` / `TF_CPP_MAX_VLOG_LEVEL` | native bins (set by ELF `main` via `setenv`) | mute embedded TF logging | 3rd-PARTY (TF) | CERTAIN |
| `TF_CPP_VMODULE` / `…_VLOG_FILENAME` / `…_LOG_THREAD_ID` | native bins | per-module VLOG / vlog file / thread-id | 3rd-PARTY (TF) | HIGH |
| `XLA_FLAGS` / `XLA_HLO_DEBUG` / `XLA_IR_DEBUG` | native bins / `libwalrus.so` | embedded-XLA debug/optimization passthrough | 3rd-PARTY (XLA) | HIGH |
| `NEURON_ASSERT*` / `NEURON_ASSERTION*` / `NEURON_ISA_TPB_*` | — | macro / error-injector test names / CLI keyword / mangled types | **NOT ENV** | CERTAIN |

### Boundary takeaways

- **Pure codegen-affecting (offline compile):** `NEURON_PLATFORM_TARGET_OVERRIDE`, `NEURON_INTERNAL_USE`, `NEURON_SCRATCHPAD_PAGE_SIZE`, and the seven `hlo-opt` `NEURON_REMAT_/COLLECTIVE_/DISABLE_` knobs — plus `NEURON_CC_FLAGS` *indirectly* (its tokens become real CLI flags upstream).
- **Profiling/benchmark/autotune-only (needs hardware):** the `NEURON_RT_*` set, read via the KRA runtime client / profiler / Autotuner; irrelevant to a hardware-less compile.
- **Third-party (embedded TF/XLA):** `TF_CPP_*`, `XLA_*` — tuned for log noise / XLA debug, not neuronx-cc behaviour.

> **NOTE — No config or response files.** Across `Options.so`, `Arguments.so`, `CommandDriver.so`, `Actions.so`, `CompileCommand.so` and `Job.so` there is no `getenv`, no `fromfile_prefix_chars`/`@`-response-file, and no `.cfg`/`.ini`/`.json` config reader. Every option comes from `sys.argv`; `NEURON_CC_FLAGS` is the only "external" source and it is resolved to argv by the caller, outside the wheel. See [3.2 `two-parser-architecture`](two-parser-architecture.md).

---

## 9. Evidence summary

The five strongest claims, re-challenged against the binary:

- **The driver does not read `NEURON_CC_FLAGS`.** `Arguments.so` has zero `getenv`, zero `environ`, zero `NEURON_CC_FLAGS`; `CompileCommand.so` has zero of all three; `CommandDriver.so` has `environ` and `shlex` but no `NEURON_CC_FLAGS`, and its `shlex` use is `quote`/`join` for command echo, not `split`. The literal lives only in `NumpyKernel.so`, three native diagnostics, and one doc example.
- **The only in-wheel reader is NKI `BaremetalKernel._compile`.** `NumpyKernel.so` is the unique wheel module carrying `NEURON_CC_FLAGS` together with `environ`, `split`, `subprocess`, `neuronxcc_flags`, `additional_compile_opt`, and the `neuronx_cc … compile --framework XLA` literal. No other wheel `.so` pairs the literal with an env read.
- **`NEURON_ASSERT*` is a test harness, macro, or keyword — not an env var.** `libwalrus.so` carries `Simulate a NEURON_ASSERT() with bir::Instruction argument` and `error_injector`; `Job.so` and `Arguments.so` carry `neuron_assert` and `NEURON_ASSERT_GLOBAL_KEYWORDS` as attribute lookups. No module pairs `NEURON_ASSERT*` with `os.environ` or `getenv` as a key.
- **`NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM` is read with `=1` semantics.** The `NOTICING NEURON_DISABLE_MOVEMENT_OF_SLICE_FROM_PARAM=1` log string sits beside the `getenv` import in `hlo-opt`. The other six `hlo-opt` knobs carry the literal, the `getenv`, and adjacent pass strings, but no decoded default.
5. **"`TF_CPP_*` are set by the native ELF main, not the Python driver."** No wheel `.py`/`.so` contains `TF_CPP_MIN_LOG_LEVEL`; the literal is present in all five native bins, and the `setenv`/`getenv` callsites are inside the native `main` (corroborated by the `hlo2penguin` main env-read trace). **Holds**, with the precision that "driver main" = native ELF main, not `CommandDriver.main` — flagged in §7.1.

## Limits of this reading

Three things are located but not fully decoded, and are **[UNRESOLVED]**: the exact numeric *defaults* of the `hlo-opt` knobs, the precise on/off branch of `NEURON_RT_DBG_FALLBACK_TO_SINGLE`, and the runtime-side semantics of `NEURON_RT_VIRTUAL_CORE_SIZE` beyond its LNC-grouping name. Every literal on this page was located in a named binary.

---

## See also

- [3.2 The Two-Parser Architecture](two-parser-architecture.md) — why the driver has no config/response file and where defaults live
- [3.5 Sub-Tool argv Construction & Replay](subtool-argv.md) — how `NEURON_CC_FLAGS` tokens, once argv, flow to each Job's sub-tool command line
- [3.8 CompileCommand Flag Catalog](flag-catalog.md) — the `--flag` sink that `NEURON_CC_FLAGS` tokens land in
- [3.7 walrus_driver Backend CLI](walrus-driver-cli.md) — where `NEURON_SCRATCHPAD_PAGE_SIZE` is honoured
