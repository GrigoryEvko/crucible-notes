# The libtpu.so / sdk.so Two-Binary Split

> *All facts on this page apply to the `libtpu` 0.0.40 wheel, tagged `cp314-cp314-manylinux_2_31_x86_64` (libtpu.so version string 0.103). libtpu.so build-id `89edbbe81c5b328a958fe628a9f2207d`; sdk.so build-id `4e9025466f71009fccb46a803806411c63744a0a`. Other wheel builds will differ.*

## Abstract

The `libtpu` wheel ships **two** ELF shared objects side by side in one directory: `libtpu.so` (745 MiB — the PJRT/XLA-TPU plugin) and `sdk.so` (21 MiB — its smaller sibling). The natural assumption is that these are two halves of one system, with `sdk.so` providing a runtime layer that `libtpu.so` loads at startup. **That assumption is wrong, and the page corrects it up front.** The two objects are independently linked, share no symbols, and neither names the other in `DT_NEEDED`. `sdk.so` is a self-contained **CPython extension module** — it exports `PyInit_sdk` and imports the Python C-API, so it is reached only by `import libtpu.sdk` from a Python interpreter. `libtpu.so` is a native PJRT plugin reached only by `dlopen` from a host runtime (JAX, the XLA TPU client); it imports **zero** Python symbols. They co-reside in the wheel for packaging convenience, not because of any link-time or load-time dependency.

The contrast that matters for a reimplementer is the *symbol-population shape*, not the dependency edge. `libtpu.so` is a near-closed object: 918,698 `FUNC` symbols in its full `.symtab` but only **225 defined exports** in its `.dynsym`, every one a versioned C-ABI thunk (`@@VERS_1.0`). It statically embeds its entire C++ world — it does **not** link `libstdc++`. `sdk.so` is the mirror image: 78,311 `FUNC` symbols but **36,789 dynamic symbols**, no symbol versioning at all, and it *does* link `libstdc++`/`libgcc_s` like an ordinary extension. One object hides everything behind a 225-entry stable ABI; the other is a normal, openly-linked Python module. The "split" in the wheel is a split between *two delivery vehicles for the same vendor's code*, not a host/device or compiler/runtime decomposition.

This page establishes the side-by-side ELF facts, proves the no-shared-symbols / no-`DT_NEEDED` relationship from the dynamic tables, identifies the 225-entry C-ABI surface of `libtpu.so` (including `GetPjrtApi` and `GetLibtpuSdkApi`), explains the `sdk.so` Python-module nature, and records how the larger object's address space was made navigable for analysis. Where the raw layout reasoning treated the two as a single tonnage to be windowed, the binaries say they are two unrelated link units; that correction is filed below.

For reimplementation, the contract is:

- **The packaging contract:** what the wheel actually contains, which file the Python loader resolves, and which file the C runtime `dlopen`s.
- **The link relationship:** that the two objects are independent — no `DT_NEEDED` edge, no symbol re-export, disjoint dynamic-symbol roles (PJRT C-ABI vs CPython module).
- **The exported-surface model of `libtpu.so`:** a 225-symbol versioned C ABI (`@@VERS_1.0`) of thin thunks over a statically-linked C++ core, with `GetPjrtApi` and `GetLibtpuSdkApi` as the two roots.
- **The analysis-navigation model:** why a 745 MiB object with 884,843 functions is audited by address window and segment, not by symbol name.

| | |
|---|---|
| **Wheel** | `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` |
| **Objects** | `libtpu/libtpu.so` (745 MiB), `libtpu/sdk.so` (21 MiB) |
| **Python loader target** | `libtpu.so` only — `__init__.py::get_library_path()` |
| **PJRT entry** | `GetPjrtApi@@VERS_1.0` — `libtpu.so` `0x0e6a83a0` (5-byte thunk) |
| **SDK C-ABI entry** | `GetLibtpuSdkApi@@VERS_1.0` — `libtpu.so` `0x109028c0` (5-byte thunk) |
| **sdk.so entry** | `PyInit_sdk` — `sdk.so` `0x00739c02` (16 bytes) |
| **Inter-object link** | **none** — no `DT_NEEDED`, no shared/re-exported symbol |

---

## At-a-Glance: The Two Objects Side by Side

Every row is read directly from the two ELF files; the version pin fixes the build-ids.

| Property | `libtpu.so` | `sdk.so` | Confidence |
|---|---|---|---|
| On-disk size | 781,691,048 B (745 MiB) | 22,541,240 B (21 MiB) | CERTAIN |
| File mode in wheel | `0755` (executable bit set) | `0644` | CERTAIN |
| ELF type | `DYN` (shared object), x86-64 | `DYN` (shared object), x86-64 | CERTAIN |
| `file` ELF flavor | version 1 (SYSV) | version 1 (GNU/Linux) | CERTAIN |
| Build-id format | `md5/uuid`, `89edbbe8…2207d` | `sha1`, `4e902546…44a0a` | CERTAIN |
| Gold-version note | absent | present (`.note.gnu.gold-version`) | CERTAIN |
| `DT_SONAME` | **none** | **none** | CERTAIN |
| `DT_NEEDED` count | 6 | 6 | CERTAIN |
| `DT_NEEDED` list | `libm libpthread libdl librt libc ld-linux` | `libpthread libm libstdc++ libgcc_s libc ld-linux` | CERTAIN |
| Links `libstdc++`? | **no** (statically embedded) | **yes** | CERTAIN |
| Symbol versioning | `VERS_1.0` (`@@VERS_1.0`) | **none** | CERTAIN |
| `.dynsym` entries | 743 | 36,789 | CERTAIN |
| Defined global exports | 225 | ~36,600 (module symbols) | HIGH |
| `.symtab` `FUNC` count | 918,698 | 78,311 | CERTAIN |
| IDA function count | 884,843 | 94,732 | CERTAIN |
| `DT_FLAGS` | absent | `BIND_NOW` (`FLAGS_1: NOW`) | CERTAIN |
| `RELACOUNT` (RELATIVE relocs) | 1,069,006 | 618 | CERTAIN |
| Loaded by | host runtime via `dlopen` | CPython via `import` | HIGH |
| Module entry | `GetPjrtApi`, `GetLibtpuSdkApi`, … | `PyInit_sdk` | CERTAIN |

> **CORRECTION (SPLIT-1) —** The two objects were earlier reasoned about as a single "binary layout split" — one body of ~979k functions to be carved into address windows, with `sdk.so` implicitly a second face of the same system. The dynamic tables overturn that framing. `libtpu.so` and `sdk.so` are **independent link units**: neither lists the other in `DT_NEEDED`, neither defines a symbol the other imports, and their dynamic-symbol roles are categorically different (a 225-entry versioned PJRT C-ABI versus a CPython extension exporting `PyInit_sdk`). The combined "function total" is an artifact of summing two unrelated IDA databases, not evidence of one binary. Treat them as two binaries that happen to share a directory.

> **GOTCHA —** The wheel is documented as a "stripped 745 MB plugin," but both objects are **`not stripped`** per `file(1)`. Their full `.symtab` survives (918,698 `FUNC` symbols in `libtpu.so`), which is why IDA recovers ~884k named functions instead of `sub_` blanks. The "stripped" intuition fails here; analysis depth is governed by the surviving `.symtab`, not by the `.dynsym`.

---

## libtpu.so — The PJRT Plugin

### Purpose

`libtpu.so` is the native Google-TPU PJRT/XLA plugin: the object a host ML runtime `dlopen`s to obtain a C-ABI handle onto the TPU compiler, runtime, and device fleet. It is the file the `libtpu` Python package resolves and advertises. The package `__init__.py` does exactly one functional thing — point an environment variable at this file:

```text
get_library_path()  ->  <pkg>/libtpu.so
configure_library_path():
    if not os.environ.get('TPU_LIBRARY_PATH'):
        os.environ['TPU_LIBRARY_PATH'] = get_library_path()
```

`sdk.so` is never named in `__init__.py`. Nothing in the Python loader touches it. A host runtime that reads `TPU_LIBRARY_PATH` and `dlopen`s the result therefore loads **only** `libtpu.so`.

### Exported C ABI

`libtpu.so` exposes a deliberately tiny surface: **225 defined global symbols** in `.dynsym`, every functional one carrying the `@@VERS_1.0` version tag from the object's single `VERS_1.0` version definition (`VERDEFNUM 2` — the base plus `VERS_1.0`). Against 918,698 `FUNC` symbols in the full `.symtab`, this is an export ratio under 0.025% — the object is a sealed C++ monolith presenting a hand-curated C door.

The exports cluster into named C-ABI families, every member a `*_DoWork` / `Tpu*_*` / `TfTpu_*` style entry:

| ABI family (prefix) | Role | Confidence |
|---|---|---|
| `GetPjrtApi` | PJRT C-API vtable root — the host runtime's primary entry | CERTAIN |
| `GetLibtpuSdkApi` | libtpu "SDK" C-API vtable root (a second, distinct ABI surface) | HIGH |
| `TpuCompiler_*` | XLA-for-TPU compiler: `New`, `Compile`, `RunHloPasses`, `RunBackend`, `ShapeSize`, `Free` | HIGH |
| `TpuCompile_*` | Compilation-cache + program build: `CompileAndBuild`, `CreateCompilationCacheKey`, fingerprints | HIGH |
| `TpuConfigurationApi_*` | Host/pod configuration: server address, memory limit, TPUs-per-host | HIGH |
| `TpuComputationPlacer_*` | Device assignment: `New`, `AssignDevices`, `AssignLocalDevices` | HIGH |
| `TpuCoreLocation_*`, `TpuDeviceDescription_*` | Topology / device-description accessors | HIGH |
| `TfTpu_*`, `TfTpuOrdinalSelector_*` | Runtime bootstrap + core-ordinal selection | HIGH |
| `HardwareLayout_*`, `SparseCore_*` | Layout math and SparseCore queries | MEDIUM |
| `TF_InitKernel`, `TFNPD_InitPlugin` | TensorFlow kernel / next-pluggable-device init | MEDIUM |
| `__start_*` / `__stop_*` | Linker-set bounds (`google_malloc`, `malloc_hook`, `pb_defaults`, `linkarr_upb_AllExts`) | HIGH |

> **QUIRK —** `GetPjrtApi@@VERS_1.0` is a **5-byte** `FUNC` at `0x0e6a83a0`, and `GetLibtpuSdkApi@@VERS_1.0` is likewise 5 bytes at `0x109028c0`. Five bytes is a single `jmp rel32` — these are tail-call thunks, not the implementations. The exported name is a stable trampoline into a statically-linked interior function whose own symbol is internal. A reimplementer must not look for the real PJRT-API constructor *at* the exported address; follow the jump. The thunk indirection is what lets the 745 MiB interior churn build-to-build while the 225-entry door stays binary-stable. (The `GetPjrtApi` thunk and the object it returns are detailed on its own page.)

> **NOTE —** `libtpu.so` carries `RELACOUNT 1069006` — over a million `R_X86_64_RELATIVE` relocations — and an `INIT_ARRAY` of 23,200 bytes (2,900 init pointers) plus a 16-byte `PREINIT_ARRAY`. The relative-reloc tonnage and the large constructor array are the load-time cost of statically linking the entire C++ runtime and protobuf/Abseil machinery into one PIE object. `sdk.so`, linking `libstdc++` dynamically, needs only 618 relative relocations.

### Why no libstdc++ in DT_NEEDED

`libtpu.so`'s `DT_NEEDED` is `libm, libpthread, libdl, libc, librt, ld-linux` — pure C runtime, **no `libstdc++`, no `libgcc_s`**. The object is unmistakably C++ (the export families are C++ subsystems behind C shims), so the C++ standard library is **statically embedded**. This is the standard hermetic-build posture for a redistributable plugin: depend only on the glibc baseline guaranteed by the `manylinux_2_31` tag, carry everything else inside. The statically-linked library inventory that this produces is catalogued separately (see the Embedded-Library Atlas).

---

## sdk.so — The CPython Extension Module

### Purpose

`sdk.so` is **not** a runtime layer beneath `libtpu.so`. Its dynamic table proves it is an ordinary CPython extension module:

- It exports **`PyInit_sdk`** (16-byte `FUNC` at `0x00739c02`) — the CPython module-init entry point. By the import protocol, `import libtpu.sdk` causes CPython to `dlopen` this file and call `PyInit_sdk`.
- Its undefined imports are dominated by the **Python C-API**: `PyObject_GenericGetAttr`, `PyType_GenericNew`, `PyCapsule_*`, `PyExc_ValueError`, `PyInterpreterState_Get`, `PyModule_NewObject`, and ~170 more non-glibc `UND` symbols, essentially all `Py*`. These are satisfied by the running CPython interpreter, not by any sibling `.so`.
- It links `libstdc++.so.6` and `libgcc_s.so.1` dynamically — the normal posture for an extension built into a Python environment that already provides the C++ runtime.

### The independence proof

The relationship between the two objects is **none**, and that is provable three ways from the binaries:

```text
1. DT_NEEDED edges:
     libtpu.so NEEDED: { libm, libpthread, libdl, librt, libc, ld-linux }
     sdk.so    NEEDED: { libpthread, libm, libstdc++, libgcc_s, libc, ld-linux }
   -> "libtpu.so" appears in neither list. "sdk.so" appears in neither list.
      No load-time edge in either direction.

2. Symbol re-export / import:
     sdk.so UND set has ZERO Tpu*/Pjrt*/GetLibtpu*/HardwareLayout*/SparseCore* names.
     -> sdk.so does not consume libtpu.so's 225-entry ABI.
     libtpu.so UND set has ZERO Py* symbols.
     -> libtpu.so does not consume CPython, so it is not loaded as a Python module.

3. Entry-point category:
     libtpu.so module root: GetPjrtApi / GetLibtpuSdkApi  (versioned C-ABI thunks)
     sdk.so    module root: PyInit_sdk                    (CPython module init)
   -> Categorically different load mechanisms (dlopen-by-runtime vs import-by-CPython).
```

> **GOTCHA —** The shared directory and the near-identical 6-entry `DT_NEEDED` lists make the two objects *look* like a paired set. They are not paired at the link level. `sdk.so` is the compiled form of the `libtpu.sdk` Python submodule; `libtpu.so` is the C plugin. A reimplementation that wires `sdk.so` as a dependency of `libtpu.so` (or vice versa) is modeling an edge that does not exist in the ELF. The only thing they share is the vendor, the wheel, and the glibc baseline.

### Symbol-population shape

`sdk.so`'s shape is the inverse of `libtpu.so`'s. It has **36,789 dynamic symbols** — roughly 50× `libtpu.so`'s 743 — because a Python extension exposes its bound C++ classes and protobuf message types broadly across its `.dynsym` rather than hiding them behind a versioned C door. It carries **no symbol versioning** (`grep VERS_1.0` over its `.dynsym` returns 0); the `@@VERS_1.0` discipline is a `libtpu.so`-only convention. Its `STRSZ` is 4.5 MiB of dynamic-string-table — the demangled-symbol cost of that wide export surface. `BIND_NOW` / `FLAGS_1: NOW` requests eager binding, normal for an extension that should fail fast at import if a `Py*` symbol is missing.

> **NOTE —** `sdk.so` reports `94,732` functions to IDA but only `78,311` `FUNC` entries in `.symtab`; the difference is IDA-recovered functions with no surviving symbol (thunks, outlined cold paths, compiler-generated helpers). The `45,156` strings and `164` switches in its database are an order of magnitude below `libtpu.so`'s (`1,249,324` strings, `33,016` switches), consistent with a 21 MiB module versus a 745 MiB monolith.

---

## Wheel Packaging Around the Two Objects

### Inventory

The wheel is a single distribution — `libtpu` `0.0.40`, one wheel tag, import-root `libtpu` — with exactly **six payload files** under the `libtpu/` package directory and a `dist-info`. The two `.so` files dominate; everything else is small text.

| File | Size | Kind | Role | Confidence |
|---|---|---|---|---|
| `libtpu/libtpu.so` | 781,691,048 B | ELF `DYN` | PJRT plugin (the loaded object) | CERTAIN |
| `libtpu/sdk.so` | 22,541,240 B | ELF `DYN` | CPython extension (`libtpu.sdk`) | CERTAIN |
| `libtpu/__init__.py` | 1,131 B | Python | Sets `TPU_LIBRARY_PATH` → `libtpu.so` | CERTAIN |
| `libtpu/LICENSE` | 229 B | text | Google Cloud Platform terms | CERTAIN |
| `libtpu/THIRD_PARTY_NOTICES.txt` | 731,537 B | text | OSS notices for `libtpu.so` | HIGH |
| `libtpu/SDK_THIRD_PARTY_NOTICES.txt` | 103,306 B | text | OSS notices for `sdk.so` | HIGH |

> **NOTE —** The two **separate** third-party-notices files (`THIRD_PARTY_NOTICES.txt` for the plugin, `SDK_THIRD_PARTY_NOTICES.txt` for the SDK) are independent corroboration of the independence finding: the two objects have different OSS dependency closures and are licensed/audited as distinct deliverables. If `sdk.so` were merely a slice of `libtpu.so`, one notices file would suffice.

### Wheel metadata

| Field | Value | Confidence |
|---|---|---|
| Distribution | `libtpu` 0.0.40 | CERTAIN |
| Wheel tag | `cp314-cp314-manylinux_2_31_x86_64` | CERTAIN |
| `Requires-Python` | `>=3.11` | CERTAIN |
| `Requires-Dist` | *(none)* | CERTAIN |
| Entry points | *(none)* | CERTAIN |
| `purelib` | false (platform wheel) | CERTAIN |
| Summary | "Google Cloud TPU runtime library." | CERTAIN |

The wheel declares **no `Requires-Dist`** — it pulls in no Python packages. Its only Python file is the 1.1 KiB `__init__.py`; all functionality lives in the two native objects. The `cp314` ABI tag pins it to CPython 3.14, but note that `libtpu.so` itself imports no Python symbols — the ABI tag is dictated by **`sdk.so`** (`PyInit_sdk` against the CPython 3.14 C-ABI), not by the plugin. This is why the platform wheel is `cp314`-specific even though its primary payload is interpreter-agnostic native code.

> **QUIRK —** Because `libtpu.so` carries no `Py*` dependency and is reached purely through `TPU_LIBRARY_PATH` + `dlopen`, the plugin would run identically under any CPython (or under a non-Python host) — its `cp314` tagging is an artifact of bundling the interpreter-bound `sdk.so` in the same wheel. A reimplementer extracting just the PJRT plugin can ignore the CPython version pin entirely; one extracting `sdk.so` cannot.

---

## Analysis Navigation of the 745 MiB Object

### Why address windows, not symbol names

A 745 MiB object with **884,843** IDA functions cannot be decompiled in one pass — IDA caps and decompiler limits force a windowed strategy. The object was made navigable two ways:

- **By segment.** The loadable map is dominated by one enormous `.text` (`0x0e63c000`–`0x21217484`, ~300 MiB of code) plus large read-only constant pools: `.lrodata` (108 MiB), `.rodata` (58 MiB), and a 28 MiB `.eh_frame`. Writable data is comparatively tiny (`.data.rel.ro` ~10 MiB, `.data` 2.4 MiB, `.bss` 853 KiB). The segment map is the first-order coordinate system: a finding at an address is placed by which segment owns it. (The full ELF section map is owned by the ELF-Anatomy page.)
- **By function window.** The 884,843-function space was decompiled in offset/limit chunks of ~10,000 functions each (`off=…  lim=10000`), ~97 windows spanning offsets 56,102 → 876,102. Each window records its own decompiled/ctree counts and failure tally; aggregate decompilation failures across the object total **511** (~0.06%), and analysis problems peak at **7,915**. This is how a target too large for a single database is audited deterministically — every function lives in a known, reproducible window.

```text
libtpu.so loadable-segment skeleton (selected, by size):
  .text          0x0e63c000 .. 0x21217484   ~300 MiB   code (perm r-x)
  .lrodata       0x01884a00 .. 0x084931d0   ~108 MiB   large const (perm r--)
  .rodata        0x084a0000 .. 0x0be8af28    ~58 MiB   const     (perm r--)
  .eh_frame      0x0c989cb8 .. 0x0e635524    ~28 MiB   unwind    (perm r--)
  .data.rel.ro   0x215f81a0 .. 0x22048b30    ~10 MiB   reloc data(perm rw-)
  .data          0x222551c0 .. 0x224bf798   ~2.4 MiB   data      (perm rw-)
  .bss           0x224c3880 .. 0x22598c30   ~853 KiB   zero-init (perm rw-)
  [init thunks]  .text.startup / .text.unlikely / google_malloc / malloc_hook
```

> **NOTE —** `sdk.so`'s database is small enough that no windowing is needed — its `.text` is `0x7394a0`–`0xb6b7a2` (~4.4 MiB) inside a single 6.6 MiB `LOAD`, and IDA decompiled essentially all of its 94,732 functions in one pass. The windowed strategy is a `libtpu.so`-only necessity born of its three-orders-of-magnitude larger code segment.

> **GOTCHA —** Do not read the IDA "total function" figures (`libtpu.so` ~884,843; the summed cross-object `~979,575`) as a measure of distinct source functions. They include thunks, template instantiations, outlined cold blocks, and per-window overlaps in the selection metadata. The numbers are useful as *relative scale* (one object is ~9× the other) and as *navigation indices*, not as a source-function census.

---

## Related Components

| Component | Relationship |
|---|---|
| `GetPjrtApi` thunk | The 5-byte export at `0x0e6a83a0` that returns the PJRT C-API vtable; primary `dlopen` entry into `libtpu.so` |
| `GetLibtpuSdkApi` thunk | The second 5-byte export at `0x109028c0`; the distinct "SDK" C-ABI root inside `libtpu.so` (named like, but unrelated to, the `sdk.so` file) |
| `PyInit_sdk` | The CPython module-init entry of `sdk.so`; reached by `import libtpu.sdk`, never by `libtpu.so` |
| Embedded C++ runtime | The statically-linked `libstdc++` + Abseil/protobuf inside `libtpu.so` that explains its missing `DT_NEEDED` edge |

> **NOTE —** The naming collision between the export `GetLibtpuSdkApi` (a function *inside* `libtpu.so`) and the file `sdk.so` is a trap. They are unrelated: `GetLibtpuSdkApi` is a C-ABI vtable root served by the plugin; `sdk.so` is a Python module that does not import it. "SDK" denotes two different things in the two contexts.

## Cross-References

- [Overview](overview.md) — forensics entry point; situates this two-object split within the binary anatomy
- [ELF Anatomy](elf-anatomy.md) — owns the full section/segment map of `libtpu.so` summarized here
- [Embedded-Library Atlas](embedded-library-atlas.md) — the statically-linked C++/protobuf/Abseil inventory that explains `libtpu.so`'s absent `libstdc++` `DT_NEEDED`
- [GetPjrtApi Thunk & tpu_plugin Object](../lifecycle/get-pjrt-api-thunk.md) — follows the 5-byte `GetPjrtApi` thunk into the PJRT-API constructor
- [Module-Init & Plugin Discovery](../lifecycle/module-init-plugin-discovery.md) — how a host runtime resolves `TPU_LIBRARY_PATH` and `dlopen`s `libtpu.so`
