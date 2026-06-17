# Diagnostic & Error-Code Catalog — Four Systems

> *All symbols, strings, addresses, and counts on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel; the catalog is byte-identical across cp310/cp311/cp312 — see [§Cross-ABI](#cross-abi-identity)). Other versions will differ.*

## Abstract

neuronx-cc does **not** have one error catalog. A single user-visible compilation failure can surface through **four independent diagnostic systems**, layered by how deep in the pipeline the failure occurs and which process emits it. A reader who sees `[NCC_EVRF001]`, `[F137]`, `error: 'penguin.matmul' op operand …`, or `[ERROR] [NCC_EARG002] Illegal argument(s)…` is looking at four structurally different mechanisms that share almost no code. This page is the map: which system a code came from, what its format means, when it fires, and how to recover its message body.

The four systems are: **(A)** the driver's three *fatal exit codes* `{[F134], [F137], [F139]}`, emitted by `CommandDriver.handleError` when a subcommand process dies abnormally; **(B)** the **224 native `NCC_<CAT><idx>` structured codes** baked per-callsite into the four HLO/penguin native binaries; **(C)** the `ErrorMessages.so` *template table* — a single flat `(category,index) → (cause,resolution)` dict of **2556 entries** that holds the message *bodies* for the backend `NeuronAssertion` path; and **(D)** the open-ended *MLIR/LLVM in-flight diagnostics* (`error:`/`warning:`/`note:`/`remark:`) plus a handful of ad-hoc numbered codes. Threaded through (B)/(C) are **two parallel `NeuronAssertion` namespaces** — a frontend Python one (`neuronxlogger/error.py`) and a backend Cython one (`neuronxcc/logging/`) — that share a rendering convention but keep independent registries.

For a reader who must interpret any neuronx-cc diagnostic, the contract is: **(1)** recognize the format and map it to one of the four systems; **(2)** for `NCC_` codes, know that the *token* is in the native binary but the *message body* is in `ErrorMessages.so` keyed by `(category,index)`; **(3)** read the category first-letter (`I…`=internal/compiler-bug, `E…`=external/user-error, bare=subsystem-tag) — but know that the `[INTERNAL_ERROR]`/`[ERROR]` prefix is actually chosen by *which assert function* fires, not by parsing the letter; **(4)** for `[F1xx]` codes, decode `1xx = 128 + signal`. The full 2556-row `ErrorMessages` dump lives in the **Part-14 error appendix (planned)**; this page summarizes its structure and gives representative rows.

| | |
|---|---|
| **System A — fatal exit codes** | `[F134] [F137] [F139]` — `neuronxcc/driver/CommandDriver.cpython-310.so`, `handleError` |
| **System B — native NCC_ codes** | **224** distinct `NCC_<CAT><idx>` tokens across `starfish/bin/{hlo-opt,hlo2penguin,hlo-neff-wrapper,xla_infergoldens}` |
| **System C — message templates** | **2556** entries, **353** categories — `neuronxcc/logging/ErrorMessages.cpython-310.so` `NEURON_ASSERT_ERROR_MESSAGES` |
| **System D — MLIR/LLVM diagnostics** | `error:`/`warning:`/`note:`/`remark:` + `n001/n002/n003` — `starfish/bin/hlo-opt`, `StaticIOTranspose.so` |
| **Two NeuronAssertion namespaces** | frontend `neuronxlogger.error` (`namespaces` registry) ↔ backend `neuronxcc.logging` (`register_namespace("neuronxcc", …)`) |
| **Render template** | `{debugloc} {prefix} [NCC_{CAT}{idx:03}] {cause} - {resolution}` |
| **Global support keyword** | `RESOLUTION_CONTACT_SUPPORT` (one entry in `NEURON_ASSERT_GLOBAL_KEYWORDS`) |

> **NOTE —** every count on this page was re-verified against the binary in this analysis: 224 via `strings | grep -oE 'NCC_[A-Z]+[0-9]{3}' | sort -u | wc -l` over the four native bins; 2556/353 via importing `ErrorMessages.cpython-310.so` standalone under CPython 3.10 and calling `len(NEURON_ASSERT_ERROR_MESSAGES)`. Both match exactly. The F-code set was re-confirmed as closed (`grep -rao '\[F[0-9]+\]'` over the entire wheel returns only F134/F137/F139, each once, all in `CommandDriver.so`).

---

## The Four Systems at a Glance

The single most important fact is that these are *four different things* that a reader will routinely confuse. The comparison table fixes the distinctions; the rest of the page expands each.

| System | Origin (emitter) | Format | Example | When emitted | Closed? | Confidence |
|---|---|---|---|---|---|---|
| **A — fatal exit** | `CommandDriver.handleError` (Python/Cython) | `[F1xx] <prose>` | `[F137] neuronx-cc was forcibly killed…` | parent process sees subprocess die with abnormal exit code / signal | **Yes** — exactly 3 | CONFIRMED (strings); signal map INFERRED |
| **B — native NCC_** | native HLO/penguin bins, per-callsite rodata | `[<prefix>] [NCC_<CAT><idx>] <cause> - <res>` | `[ERROR] [NCC_EVRF001] …` | a native HLO/penguin pass hits a curated check | **Yes** — 224 codes, sparse indices | CONFIRMED (token inventory) |
| **C — template table** | `ErrorMessages.so` dict, via `NeuronAssertion` | `(cat,idx) → (cause_tmpl, res_tmpl)` | `('EARG',2) → ("Illegal argument(s) - …", "Check that …")` | a backend BIR/walrus/codegen/NKI assert fires & looks up its body | **Yes** — 2556 entries, 353 cats | CONFIRMED (runtime dump) |
| **D — MLIR/LLVM** | embedded MLIR/LLVM diag engine + ad-hoc | `<loc> error:/warning:/note:/remark: <msg>` | `loc: error: 'op' op failed to verify` | MLIR op-verification / LLVM codegen, in-flight | **No** — open-ended | CONFIRMED (prefixes) |

> **GOTCHA —** systems B and C describe the *same* diagnostic from two angles. The 224 `NCC_` *tokens* are baked into the native binaries' rodata (system B), but for the **backend** assert path the message *body* is looked up at runtime from the `ErrorMessages.so` dict (system C). Crucially, the *frontend* native NCC categories (`EVRF`, `PYP`, `ISPP`, `MOD`, `HPM`, `CTR`, …) are **absent** from the `ErrorMessages.so` dict — their bodies are baked directly into the HLO bins' rodata. The two catalogs overlap at exactly one category: `IIOT`. So "look up the body in `ErrorMessages.so`" works for backend codes but *not* for the frontend native ones. (CONFIRMED by category presence/absence in the dumped dict.)

> **GOTCHA —** a single failing compile typically prints **two** lines: the failing subprocess emits its `[NCC_…]` / template line and then exits non-zero; the parent `CommandDriver` then prints the trailing `[F1xx]`. Do not read the `[F1xx]` as the root cause — it is the *envelope*. The diagnostic content is in the `[NCC_…]` line above it.

---

## System A — Driver Fatal Exit Codes `[F134] [F137] [F139]`

### Purpose

The three `F`-codes are the *only* top-level diagnostics neuronx-cc emits in its own (parent) process. Every subcommand (`hlo2penguin`, `walrus_driver`, etc.) runs inside a `multiprocessing.Process`; when that child dies abnormally the parent cannot see a `[NCC_…]` line — it sees only an OS exit code. `handleError` maps that exit code to one of three user-facing prose messages.

### Entry Point

```text
CommandDriver.run                                       ── runs subcommand in multiprocessing.Process
  └─ process.join(); read process.exitcode              ── "Subcommand returned with exitcode=<n>"
       └─ CommandDriver.handleError(exit_code, signal)  ── selects [F134] / [F137] / [F139]
```

Qualname `neuronxcc.driver.CommandDriver.handleError` is CONFIRMED verbatim in `CommandDriver.cpython-310.so`, along with the Cython entry symbol `__pyx_pw_9neuronxcc_6driver_13CommandDriver_3handleError` and the locals `exitcode` / `signal` (`__pyx_v_exit_code`, `__pyx_n_s_signal`). The auxiliary banner `"Subcommand returned with exitcode="` is CONFIRMED.

### The three codes (verbatim message bodies, CONFIRMED)

```text
[F134] neuronx-cc terminated abnormally - Please open a support ticket at
       https://github.com/aws-neuron/aws-neuron-sdk/issues/new
[F137] neuronx-cc was forcibly killed - This most commonly occurs due to
       insufficient system memory. Using a smaller data type, dimensions,
       batch size, or a larger instance type may help.
[F139] neuronx-cc terminated abnormally - Please open a support ticket at
       https://github.com/aws-neuron/aws-neuron-sdk/issues/new
```

A second diagnostic is printed alongside (CONFIRMED): *"Processes may be terminated if they encountered an internal error or if there was insufficient memory. Please review the log file for any error conditions. … Current memory usage for neuronxcc is …"*.

### Code ↔ signal mapping

The numeric codes are the POSIX shell convention `128 + signal`:

| Code | Decode | Signal | Cause | Wording confirms |
|---|---|---|---|---|
| `F134` | `128 + 6` | `SIGABRT` | child `abort()`ed — failed C++ assert / `std::abort` / uncaught native exception | "terminated abnormally" |
| `F137` | `128 + 9` | `SIGKILL` | child killed by signal 9 — overwhelmingly the Linux OOM-killer | "forcibly killed … insufficient system memory" |
| `F139` | `128 + 11` | `SIGSEGV` | child segfaulted | "terminated abnormally" (2nd variant) |

The `128+{6,9,11}` identity is **INFERRED** from POSIX convention, but is near-certain: the `F137` message explicitly ties the code to out-of-memory (the SIGKILL/OOM-killer fingerprint), and the names are the standard shell exit codes. The exact `exitcode → F-switch` branch inside `handleError` is the one derived part. `SIGINT` (Ctrl-C) is also referenced as a distinct symbol — it is *not* one of the F-codes (it is the clean-interrupt path).

> **NOTE —** `F134` and `F139` carry the *identical* "terminated abnormally — open a support ticket" body. The only thing distinguishing an abort from a segfault to the user is the trailing digit. Both mean "the compiler crashed; this is a bug" — versus `F137` which means "you ran out of RAM; shrink the model".

This is a **closed set**: a `\[F[0-9]{2,4}\]` sweep over the entire wheel returns exactly these three, each appearing once, all in `CommandDriver.so`. See [§3.1 command-dispatcher](command-dispatcher.md) for the subprocess/exit-code plumbing.

---

## System B — Native `NCC_<CAT><idx>` Structured Codes

### Purpose

The big one. 224 distinct `NCC_<CAT><III>` tokens are baked per-callsite into the four native HLO/penguin binaries. Each token names a specific, curated check inside a native pass. The token is fully rendered into the binary's rodata as a literal (e.g. the string `NCC_EVRF001` appears verbatim) — it is **not** a `%s%03d` format assembled at runtime, so the catalog is enumerable by a `strings` sweep.

### Code format & severity convention

```text
[<prefix>] [NCC_<CATEGORY><III>] <cause> - <resolution>
  <prefix>   = "[INTERNAL_ERROR]"  or  "[ERROR]"
  <CATEGORY> = 2–5 uppercase letters; FIRST LETTER = severity/origin class
                 I…  → INTERNAL (compiler bug)   → [INTERNAL_ERROR] prefix
                 E…  → EXTERNAL (user error)     → [ERROR] prefix
                 (bare, no I/E lead: CTR HPM MOD NCO OPT SPP DRV … = subsystem tag)
  <III>      = 3-digit zero-padded index (ErrorCode.__str__ = f"{category}{index:03}")
```

The `[INTERNAL_ERROR] [NCC_I` literal is CONFIRMED in rodata. The 4-character `I…`/`E…` rule is enforced by the CI linter `valid_error_category` in `neuronxlogger/error_validation.py` (CONFIRMED): an internal assert must use a 4-char category starting `'I'`; an external assert, 4-char starting `'E'`. (See the [prefix correction](#correction-the-prefix-is-chosen-by-the-assert-function-not-the-category) below — the *runtime* prefix is chosen by the assert function, not by re-parsing the letter.)

### Inventory (CONFIRMED — closed sweep)

**224 distinct `NCC_` tokens**, distributed across the four native bins as follows (re-verified `strings | grep -oE 'NCC_[A-Z]+[0-9]{3}' | sort -u`):

| Binary | Distinct codes | Categories present |
|---|---|---|
| `hlo2penguin` | **213** | CTR DRV ECFT EHCA EMOD ESFH ESPP ETUP EUET EUOC EVRF HPM IARC IARE IHCA IMOD IMUT INL ISPP ITUP IVRF MOD NCO PYP SPP |
| `hlo-opt` | 14 | CTR EUDT EUOC HPM IARE INL ITUP MOD OPT |
| `xla_infergoldens` | 8 | CTR EUDT HPM INFG ITUP |
| `hlo-neff-wrapper` | 5 | IHNW IIOT |

The 30 distinct categories present in these four bins are: `CTR DRV ECFT EHCA EMOD ESFH ESPP ETUP EUDT EUET EUOC EVRF HPM IARC IARE IHCA IHNW IIOT IMOD IMUT INFG INL ISPP ITUP IVRF MOD NCO OPT PYP SPP`.

> **QUIRK —** index sets are **sparse with gaps**, not contiguous. `EVRF` runs `001-058` but is missing `003,015,016,020-024`; `PYP` runs `001-058` missing `002`; `ISPP` runs `001-061` with gaps. The gaps are *retired* codes — `ErrorMessages.so` literally contains four `"OBSOLETE ERROR CODE. DO NOT USE."` templates (CONFIRMED). A reimplementer enumerating a category must treat each range as enumerated-with-gaps, never `for i in 1..n`.

### Category decode

The first-letter severity class is CONFIRMED; the word-expansion of each abbreviation is **STRONG** (inferred from the emitting binary and adjacent rodata strings):

| Category | Expansion | Bin | Severity |
|---|---|---|---|
| `PYP` | PYthon Pass (pybind HLO→penguin frontend) — largest user family | hlo2penguin | mixed |
| `EVRF` / `IVRF` | VeRiFier (HLO/penguin IR verifier) — 48 `EVRF` + 3 `IVRF` | hlo2penguin | ext / int |
| `ESPP` / `ISPP` / `SPP` | Setup-Penguin-Pass / penguin pipeline — 47 `ISPP` internal | hlo2penguin | mixed |
| `MOD` / `IMOD` / `EMOD` | (modular) Module compilation | hlo2penguin, hlo-opt | mixed |
| `IHCA` / `EHCA` | Hlo-Custom-call Attr handling | hlo2penguin | int / ext |
| `ITUP` / `ETUP` | TUPle flatten/handling | all three of hlo-opt/hlo2penguin/xla_infergoldens | int / ext |
| `EUDT` / `EUOC` / `EUET` | Unsupported-DType / -OpCode / -… | hlo-opt, hlo2penguin, xla_infergoldens | external |
| `INL` | INLine pass | hlo-opt, hlo2penguin | internal |
| `OPT` | opt-driver / HLO optimization driver | hlo-opt | external |
| `HPM` | Hlo-Pass-Metadata / pass-manager (common infra) | all native bins | internal |
| `CTR` | (compiler) ConTRol / context (common infra) | all native bins | mixed |
| `INFG` | INFer-Goldens | xla_infergoldens | internal |
| `IHNW` | Hlo-Neff-Wrapper | hlo-neff-wrapper | internal |
| `IIOT` | Io-transpOse Transpose | hlo-neff-wrapper | internal |
| `ECFT` `ESFH` `IMUT` `IARC` `IARE` `NCO` `DRV` | misc penguin / driver int/ext codes | hlo2penguin | mixed |

### Representative codes

A sample (not the full 224 — see the [consolidated table](#consolidated-cross-system-table)):

```text
NCC_OPT001-003   opt-driver: "No valid Hlo passes specified, exiting…" /
                 "AddPass cannot be called after Run" / "Required pass not found!"   (hlo-opt)
NCC_HPM001-003   "HloPassMetadata for currently running pass not found, either because
                 the pass did not call RecordPassStart or because a pass is
                 creating/switching modules without using HloModuleGroup::ReplaceModule."
NCC_EVRF001-058  HLO/penguin IR verifier failures (external, user-actionable)        (hlo2penguin)
NCC_PYP001-058   Python(pybind) HLO→penguin lowering errors                          (hlo2penguin)
NCC_ISPP001-061  Setup-Penguin-Pass internal asserts                                 (hlo2penguin)
NCC_IIOT001      "Duplicate io_transpose found for input: <name>"                    (hlo-neff-wrapper)
NCC_IIOT002      "Forbidden io_transpose found for input: <name>"                    (hlo-neff-wrapper)
NCC_IHNW003      "Reshape size mismatch for input: <name>"                           (hlo-neff-wrapper)
```

The `IIOT`/`IHNW` message strings are CONFIRMED verbatim in `hlo-neff-wrapper`. The `OPT`/`HPM` diagnostics are CONFIRMED in `hlo-opt`. The exact per-index `NCC_OPT001` ↔ message binding is **not** individually pinned (codes and diagnostic strings are separate rodata blobs) — STRONG by co-location.

---

## System C — The `ErrorMessages.so` Template Table

### Purpose

`ErrorMessages.cpython-310.so` (Cython, compiled from `ErrorMessages.pyx`) holds the **message bodies** for the *backend* `NeuronAssertion` path. It is a single flat Python dict, recovered verbatim by importing the `.so` standalone (it has zero third-party deps) under CPython 3.10 and dumping the global.

### Exact shape (CONFIRMED — runtime introspection)

```python
NEURON_ASSERT_ERROR_MESSAGES: Dict[Tuple[str, int], Tuple[str, str]]
#   KEY   = (category: str, index: int)        e.g. ('EVRF',1), ('BIR',199), ('NKI',16)
#   VALUE = (cause_template: str, resolution_template: str)
# Both templates are Python str.format strings with {kwarg} placeholders
# + the injected global {RESOLUTION_CONTACT_SUPPORT}.
```

It is **one flat tuple-keyed dict for all namespaces** — *not* an ordered list indexed by an enum, *not* a per-category nested dict. The per-error "namespace" in the `neuronxlogger` sense is the single string `"neuronxcc"`; the 353 categories are an internal sub-key inside the key tuple, not separate registrations.

### Counts (CONFIRMED — re-verified by import in this analysis)

| Metric | Value |
|---|---|
| Total entries (cause/resolution pairs) | **2556** |
| Distinct categories | **353** |
| `I…` internal (→ `[INTERNAL_ERROR]`) | 252 categories / **863** entries |
| `E…` external (→ `[ERROR]`) | 24 categories / **126** entries |
| bare/legacy (own embedded text / subsystem tag) | 77 categories / **1567** entries |
| resolution == `"{RESOLUTION_CONTACT_SUPPORT}"` only | **2095** |
| entries with a bespoke (non-contact) resolution | **461** |
| `"OBSOLETE ERROR CODE. DO NOT USE."` entries | **4** — all `('BIR',199/202/203/204)` |
| `NEURON_ASSERT_GLOBAL_KEYWORDS` | 1 entry (`RESOLUTION_CONTACT_SUPPORT`) |

> **QUIRK —** the count being 2556 (not the strings-era estimate of ~600) is explained by a **generated 901/902 per-pass family**: **446** of the 2556 entries are `(<CAT>,901) = ("<PassName> assertion error: {baseMessage}", …)` and `(<CAT>,902) = ("<PassName> error: {baseMessage}", …)`. 217 categories carry *only* `{901,902}` (pure per-pass wrappers); the rest add 901/902 on top of curated low-index codes. `901` = a failed C++ assert inside the pass (`{baseMessage}` = the assert text); `902` = a non-assert thrown pass error. This is the generic "any pass can fail" backstop; the low-index codes are the specific, curated diagnostics. See the index map below for the pass-name → category mapping (e.g. `ILCM`=LICM, `INIS`=NeuronISel, `ISIM`=SimulatorPass).

### Build mechanism

The dict is a **compile-time-frozen Cython constant**: the whole `{(cat,idx):(cause,res), …}` literal is stored as one large tuple-of-pairs in `.rodata`, materialised once in the module-exec and bound to the global. The module-exec `__pyx_pymod_exec_ErrorMessages` (entry `0x53c8`) contains only ~9 `PyDict_SetItem` calls total — far too few for 2556 inline insertions. This is exactly why D-A13 could not recover the `(cat,idx)↔text` pairing from `strings` alone: `strings` sees the text blob and the category tokens, but the *adjacency* is encoded in the frozen constant's pointer layout, not interleaved in the string section. The runtime import reconstitutes it losslessly. Cython build provenance is CONFIRMED via an `__assert_fail` string: `.../neuronxcc/logging/ErrorMessages.cxx`, function `int __pyx_pymod_exec_ErrorMessages(PyObject*)`.

### Wiring

`logging/__init__.so` (`__pyx_pymod_exec_logging`) performs the hand-off in one call:

```python
import logging
from neuronxlogger import register_namespace                       # system-(I) fn, neuronxlogger.error
from neuronxcc.logging.ErrorMessages import NEURON_ASSERT_ERROR_MESSAGES
logging.addLevelName(logging.CRITICAL + 10, "USER")                # USER  = 60
logging.addLevelName(logging.CRITICAL + 20, "OFF")                 # OFF   = 70
logging.addLevelName(logging.NOTSET   +  5, "TRACE")               # TRACE = 5
register_namespace("neuronxcc", NEURON_ASSERT_ERROR_MESSAGES)      # ONE call, whole flat dict
```

`register_namespace`, `NEURON_ASSERT_ERROR_MESSAGES`, and `addLevelName` are CONFIRMED symbols in `__init__.so`. The single namespace string `"neuronxcc"` registers the entire flat dict; there are *not* 353 separate registrations. The `USER`/`OFF`/`TRACE` level constants bolted onto the stdlib `logging` module are the neuronxcc severity overlay — see [§3.18 neuronlogger](neuronlogger.md) and [§3.19 python-logging-facade](python-logging-facade.md).

### Representative categories & rows

The table is too large to inline. Here are representative rows grouped by emitting subsystem (CONFIRMED templates; `{kwarg}` placeholders verbatim). The full 2556-row dump is the **Part-14 error appendix (planned)**.

```text
# EXTERNAL — driver / arguments (EARG, EDRV, EBRD, EOOM)            [Severity: ERROR/user]
('EARG',2)   "Illegal argument(s) - the following argument(s) are unrecognized: {…}"
('EOOM',1/2) "Maximum peak HBM usage of {usage} … exceeds {hbm_limit}GB …"
('EXSP',1)   "Size of HBM memory required for the model exceeds HBM limit of {hardware_target}. …"
('EBRD',*)   "Unable to open condensed format file {file_name}" / "Mismatched number of modules …"
('EBVF',30)  "Instructions generated by compiler {total_count} exceeds the typical limit of {max_instruction_limit}…"

# EXTERNAL — NKI kernel validation (ENKI, EDMA, EIL)                [Severity: ERROR/user]
('EDMA',2)   "Kernel partition size {kernel_num_partitions} doesn't match architecture … {arch_num_partitions}"
('EIL',1)    "InstCompareAndBranch cannot have a … "
('NKI',16)   "{error_text}"        # user NKI kernel_assert body; caller pre-.format()s error_text

# INTERNAL — register alloc / scheduler / ISA (ALS, BIR, GCA, SCH, SIM, IXCG …)  [INTERNAL]
('IXCG',*)   "ISA check failed …"  / ('BVF',1) "InstTensorScalarCache does not support …"
('ALS',10)   "Instruction must exist"
('BIR',199)  "OBSOLETE ERROR CODE. DO NOT USE."

# GENERATED per-pass backstop (446 entries, 901/902)
('ILCM',901) "LICM assertion error: {baseMessage}"          ('ILCM',902) "LICM error: {baseMessage}"
('INIS',901) "NeuronISel assertion error: {baseMessage}"    ('IBCG',901) "BIRCodeGenLoop assertion error: {baseMessage}"
```

### First-letter category histogram (distinct categories, CONFIRMED)

```text
A=3  B=9  C=4  D=4  E=24  G=1  I=252  J=2  L=12  M=5  N=4  O=2  P=3  R=1  S=8  T=3  V=1  X=15
```

The bare-category first letter is a **subsystem tag**, not a severity: `B*`=BIR, `L*`=loop/link/legalize/LNC, `S*`=schedule/sim/serdes, `X*`=XLA-bridge/codegen, `M*`=memory/MMP, `D*`=DMA/driver. (STRONG — from the per-category pass labels.)

### Largest categories (by entry count, from the per-category index map)

| Category | Entries | idx range (gaps) | Subsystem/pass |
|---|---|---|---|
| `BIR` | 354 | `1..722` (368 gaps) | BIR codegen / verification |
| `XCG` | 217 | `1..1016` (799 gaps) | XLA-bridge codegen |
| `ISIM` | 145 | `137..902` (621 gaps) | SimulatorPass |
| `SIM` | 141 | `1..206` (65 gaps) | simulator |
| `GCA` | 89 | `13..109` (8 gaps) | base-partition / collective allocation |
| `DRV` | 78 | `1..79` (1 gap) | walrus driver |
| `MMP` | 72 | `110..321` (140 gaps) | memory MMP |
| `LLC` | 64 | `1..64` | low-level codegen invariant |

---

## The Two `NeuronAssertion` Namespaces

There are **two independent** `NeuronAssertion` implementations. Do not conflate them — they share the rendering convention and the `RESOLUTION_CONTACT_SUPPORT` keyword but keep *independent* registries.

### (I) Frontend — `neuronxlogger/{error,error_validation}.py`

Pure Python, shipped as `.py` source (CONFIRMED, read in full). Surface: `NeuronAssertion(Exception)`, `ErrorCode(category,index)`, `neuron_internal_assert` / `neuron_external_assert` / `neuron_internal_assert_msg`, `register_namespace`. The class-level registry is `NeuronAssertion.namespaces: Dict[str, Dict]`. On a lookup miss, `_fallback_internal_assert` raises `ErrorCode("INAS",1)` — the canonical "namespace/code does not exist" failure (`INAS001`). `error_validation.py` is the **CI linter** (`valid_error_category`, `verify_neuron_assert_namespace`) — *not* a runtime path.

### (II) Backend — `neuronxcc/logging/{Assert,ErrorMessages,__init__}.so`

Cython package. `ErrorMessages.so` holds the 2556-entry dict (system C). `Assert.so` exposes `neuron_assert(namespace, index, condition, **kwargs)` plus `class NeuronAssertionError` (`getSrcInfo` / `getSrcInfoFromDebugLocation` / `__str__`, CONFIRMED symbols) and the rendered-prefix constants (`__pyx_kp_u_INTERNAL_ERROR_NCC_I`). `__init__.so` wires `ErrorMessages` into the frontend registry via `register_namespace("neuronxcc", …)`. The **same** C++ model is statically linked into the native bins as `logging::NeuronAssertion` / `lookupCause` / `lookupResolution`.

Pinned Python call sites (CONFIRMED): `cli/Daemon.py` → `neuron_assert('DAE',1/2,…)`; `nki/.../kernel_assert.py` → `neuron_assert("NKI",16, condition, error_text, **kwargs)`.

> **NOTE —** the split is **FRONTEND-native-rodata** (`EVRF`/`PYP`/`ISPP`/`HPM`/`CTR`/… — baked into the HLO bins, system B) **vs BACKEND-ErrorMessages.pyx** (the 353 categories of system C, reached via the linked C++ `NeuronAssertion` *and* the Python `neuron_assert`). They overlap at exactly one category: `IIOT` (present in `ErrorMessages.so` only as the generated `(IIOT,901)/(IIOT,902)` pair). So the same `NeuronAssertion` *model* is rendered in two places with disjoint category sets.

### Render path (CONFIRMED verbatim from `neuronxlogger/error.py`)

```c
// neuron_external_assert / neuron_internal_assert (error.py L77-190)
function neuron_*_assert(ns, category, index, condition, condition_text, debugloc, **kwargs):
    if condition: return                                    // assert passed — no-op
    log "Assertion failed: {condition_text}"                // Logger(file:line)
    ec = ErrorCode(category, index)                         // __str__ = f"{category}{index:03}"
    (cause, res) = _get_cause_resolution(ns, ec)            // (cat,idx) lookup in namespaces[ns]
    //   on missing ns OR missing key -> _fallback_internal_assert -> ErrorCode("INAS",1)
    raise NeuronAssertion(
        ec, condition_text,
        cause.format(**kwargs),
        res.format(**kwargs, RESOLUTION_CONTACT_SUPPORT=…),
        prefix,                                             // "[INTERNAL_ERROR]" or "[ERROR]"
        debugloc)

// NeuronAssertion.__init__ assembles the final string (error.py L34-49)
if not debugloc:  message = f"{prefix} [NCC_{ec}] {cause} - {res}"
else:             message = f"{debugloc} {prefix} [NCC_{ec}] {cause} - {res}"
```

Worked example (CONFIRMED templates):

```text
neuron_external_assert("neuronxcc","EARG",2, cond, …, unrecognized_args="--foo", usage="…")
  → "[ERROR] [NCC_EARG002] Illegal argument(s) - the following argument(s) are
     unrecognized: --foo. - Check that only recognized arguments are used: …"
```

### CORRECTION — the prefix is chosen by the assert *function*, not the category

> **CORRECTION (A13→AG09) —** an earlier reading said the `[INTERNAL_ERROR]` vs `[ERROR]` prefix is selected by parsing the category's first letter (`I…`/`E…`). The shipped `error.py` shows otherwise: `neuron_internal_assert` hard-codes `INTERNAL_ERROR_PREFIX` (L109) and `neuron_external_assert` hard-codes `EXTERNAL_ERROR_PREFIX` (L188). The runtime never branches on `CAT[0]`. The `I…`/`E…` naming is a **strong convention** enforced *at CI lint time* by `valid_error_category` (internal asserts must use 4-char `I*`, external 4-char `E*`), and it co-varies with the chosen prefix — but the prefix is determined by *which function the caller invoked*, not by the letter. For bare categories the prefix is whatever assert the native/Python call site uses; the bare first letter is a subsystem tag.

> **QUIRK —** `neuron_internal_assert_msg` (L115-152) still *validates* that `(cat,idx)` exists in the table (it calls `_get_cause_resolution` and discards the result) but then **overrides** the cause with a caller-supplied `cause_message` and forces `resolution = "{RESOLUTION_CONTACT_SUPPORT}"`. So a registered `(cat,idx)` can serve purely as a "this code is valid" gate with inline text — which is why some entries like `('ENKI',1)` exist as empty-string existence sentinels.

---

## System D — MLIR/LLVM In-Flight Diagnostics + Ad-Hoc Numbered Codes

### Purpose

`hlo-opt` links the full MLIR/LLVM diagnostic infrastructure. These are emitted *ad hoc* with source locations during op-verification and LLVM codegen — they are **not** a closed, numbered catalog. Severity is encoded in the prefix.

### Diagnostic prefixes (CONFIRMED literals in `hlo-opt`)

| Prefix | MLIR severity | Meaning |
|---|---|---|
| `error:` | `DiagnosticSeverity::Error` | fatal to the pass run |
| `warning:` | `DiagnosticSeverity::Warning` | non-fatal (also `Warning:` / `WARNING:`) |
| `note:` | (attached context) | informational, attaches to a prior diagnostic |
| `remark:` | `DiagnosticSeverity::Remark` | optimization remark / info |

Driver wrappers (CONFIRMED strings): `"LLVM compilation error: "`, `"LLVM ERROR: "`, `"JIT session error: "`, `"In-Flight Diagnostics:"`. Supporting flags: `mlir-print-op-on-diagnostic`, `crash-diagnostics-dir`, `-split-input-file` (`// -----` separators). All CONFIRMED.

### Ad-hoc numbered codes (NOT `NCC_`)

`StaticIOTranspose` (Cython, separate from the native bins) emits its own `n001/n002/n003` numbered diagnostics on the modular io_transpose path: `n001` = ≥2 modular files without `--netlist`; `n002` = duplicate/forbidden transpose; `n003` = reshape size mismatch. These are distinct from the `NCC_IIOT/IHNW` codes in `hlo-neff-wrapper` (which check the same conditions but render as `NCC_` tokens). See [§3.15 static-io-transpose](static-io-transpose.md).

Backend validation also emits free-form (non-`NCC_`) diagnostics: `"Data race detected:"` from `libBIRRacecheck.so` (`racecheck::RaceChecker`). The `neuronxcc::backend::ErrorType` LLVM `cl::opt` enum (`abort` / `error` / `ignore` / `strict`, STRONG) is a knob that escalates/suppresses backend validation failures — a *control*, not a code.

> **CORRECTION (per D-B13) —** an earlier note attributed `n001/002/003` codes to the CC-ops legalizers. That is **uncorroborated**: those sites carry no `NCC_` code; their only diagnostic is the up-cast *warning* `"We up-casted some collective-communication ops … performance penalty"` plus `TfCheckOp` aborts. The confirmable `n00x` codes are `StaticIOTranspose`'s.

---

## Consolidated Cross-System Table

Native `NCC_` rows: the message template body is the subsystem text per system C; exact per-index text is bound only for the codes with verbatim strings. Severity from category first-letter (`I`=internal, `E`/bare-with-user-msg=external).

| Code | Message (template / verbatim) | Trigger | Component | Severity |
|---|---|---|---|---|
| `[F134]` | "neuronx-cc terminated abnormally — open a support ticket…" | child exit 134 = 128+SIGABRT (abort/native assert) | driver/CommandDriver | FATAL |
| `[F137]` | "neuronx-cc was forcibly killed — …insufficient system memory…" | child exit 137 = 128+SIGKILL (OOM-killer) | driver/CommandDriver | FATAL |
| `[F139]` | "neuronx-cc terminated abnormally — open a support ticket…" | child exit 139 = 128+SIGSEGV (segfault) | driver/CommandDriver | FATAL |
| `DAE001` | "Lock directory does not exist" | `LockDirectory(dir)`, dir missing | cli/Daemon.py | ERROR(ext) |
| `DAE002` | invalid log level | `--verbose` ∉ {trace,debug,info,warning,error,critical,user} | cli/Daemon.py | ERROR(ext) |
| `NKI016` | `{error_text}` | user NKI `kernel_assert(condition,…)` false | nki/.../kernel_assert | ERROR(ext) |
| `INAS001` | "Error namespace {ns} does not exist or error code {ec} does not exist." | `_get_cause_resolution` lookup miss | neuronxlogger/error.py | INTERNAL |
| `NCC_OPT001-003` | no-valid-passes / AddPass-after-Run / required-pass-not-found | hlo-opt pass-pipeline setup | hlo-opt | ERROR(ext) |
| `NCC_HPM001-003` | "HloPassMetadata for currently running pass not found…" | RecordPassStart missing / module switch | hlo-opt + all native | INTERNAL |
| `NCC_EVRF001-058` | HLO/penguin IR verifier failures | IR verification check fails | hlo2penguin | ERROR(ext) |
| `NCC_PYP001-058` | pybind HLO→penguin lowering errors | unsupported op/attr in frontend lowering | hlo2penguin | INTERNAL |
| `NCC_ISPP001-061` | Setup-Penguin-Pass internal asserts | penguin pipeline invariant fails | hlo2penguin | INTERNAL |
| `NCC_IIOT001/002` | "Duplicate/Forbidden io_transpose found for input: …" | combineIoTransposes dup / IntermediateIOs | hlo-neff-wrapper | INTERNAL |
| `NCC_IHNW003` | "Reshape size mismatch for input: …" | declared reshape ≠ actual param shape | hlo-neff-wrapper | INTERNAL |
| `n001/n002/n003` | missing-netlist / dup-forbidden / reshape-mismatch | StaticIOTranspose modular-path validation | StaticIOTranspose.so | ERROR(ext) |
| `('EOOM',*)`/HBM/SB | "…exceeds HBM limit of {hardware_target}…", "…too big for SB…" | model exceeds device HBM/SBUF/instr capacity | libBIR/libwalrus via ErrorMessages | ERROR(ext) |
| `('EARG',*)` | "Illegal argument(s) - …unrecognized: {…}" | bad CLI/backend args | libwalrus via ErrorMessages | ERROR(ext) |
| `('I***',*)` (252 cats) | InstX-must-have / reg-alloc / scheduler / ISA-check templates | backend BIR/walrus/codegen invariant fails | libwalrus/libBIR via ErrorMessages | INTERNAL |
| `"Data race detected:"` | race report w/ instruction pair | `racecheck::RaceChecker` interference | libBIRRacecheck.so | ERROR(validation) |
| `error:`/`warning:`/`note:`/`remark:` | MLIR op-verify / LLVM codegen (open-ended) | MLIR/LLVM diagnostic engine in-flight | hlo-opt | per-prefix |
| `backend::ErrorType` | enum knob `abort/error/ignore/strict` | `--…error-type=<mode>` flag | libwalrus | (control) |

### Cross-ABI identity

The catalog is **ABI-independent**: the cp310/cp311/cp312 wheels carry the identical 224-token `NCC_` set and the identical 2556-entry `ErrorMessages` dict (D-AG09 reports an md5-identical sorted `(cat,idx,cause,res)` tuple set, `2c25078c8065b0381143fabf7aff60be`, across all three). A reimplementation may treat the catalog as fixed across CPython versions.

---

## Gaps & Caveats

1. **Frontend native NCC bodies are not in the dict.** The 224 native `NCC_` codes' *bodies* for frontend categories (`EVRF`/`PYP`/`ISPP`/`MOD`/`HPM`/`CTR`/…) are baked into the HLO bins' rodata, not in `ErrorMessages.so`. The exact per-index `NCC_OPT001` ↔ message binding is co-located but not individually pinned (codes and strings are separate rodata blobs).
2. **`128+signal` for F-codes is INFERRED** (POSIX convention + the F137-OOM wording); the message bodies and the OOM tie are CONFIRMED.
3. **The `n00x` CC-ops-legalizer claim is uncorroborated** (per D-B13); the confirmable `n00x` codes are `StaticIOTranspose`'s.
4. **Index sets are sparse** (retired/`OBSOLETE` codes); ranges are enumerated-with-gaps.
5. The MLIR/LLVM `error:`/`warning:`/`note:`/`remark:` tier is open-ended — only the structured `NCC_*` / `F*` / `DAE*` / `n00x` codes are enumerable.

---

## Related Components

| Name | Relationship |
|---|---|
| `CommandDriver.handleError` | emits system A (the three F-codes) |
| `neuronxlogger.error` | frontend `NeuronAssertion` namespace (I); owns `register_namespace`, `INAS001` fallback |
| `neuronxcc.logging.{Assert,ErrorMessages}` | backend `NeuronAssertion` namespace (II); the 2556-entry template dict |
| `hlo-opt` MLIR/LLVM engine | emits system D (in-flight diagnostics) |
| `error_validation.py` | CI linter enforcing the `I…`/`E…` 4-char category rule |

## Cross-References

- [NeuronLogger](neuronlogger.md) — §3.18, the `USER`/`OFF`/`TRACE` level overlay these systems log through
- [Python Logging Facade](python-logging-facade.md) — §3.19, the stdlib-`logging` bridge wiring in `logging/__init__.so`
- [Command Dispatcher & Subcommand Model](command-dispatcher.md) — §3.1, the subprocess/exit-code plumbing behind the F-codes
- [StaticIOTranspose & the io_transpose JSON Schema](static-io-transpose.md) — §3.15, source of the `n001/n002/n003` codes and the `IIOT` overlap
- Part-14 Error Appendix *(planned)* — the full 2556-row `(category,index)→(cause,resolution)` dump
