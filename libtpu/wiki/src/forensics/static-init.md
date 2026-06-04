# Static-Init Surface

> *All addresses on this page apply to `libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`) from the `libtpu-0.0.40-cp314` wheel. Other builds will differ. This is the binary-forensics census of `.init_array`; the runtime walk that executes these slots lives on the [lifecycle](../lifecycle/do-init-do-fini.md) pages.*

## Abstract

A C++ shared object front-loads work into its constructor table: every namespace-scope object with a non-trivial constructor, every `REGISTER_*` static, every Meyers singleton, every flag definition compiles to a translation-unit initializer that the dynamic linker invokes before the first exported symbol can be called. In `libtpu.so` that table — the ELF `.init_array` — holds **2900 function-pointer slots** spanning 23200 bytes. This is not a runtime data structure the plugin manages; it is a static manifest baked into the file, and its size alone tells you that loading libtpu pays a large, fixed, single-threaded constructor tax before `GetPjrtApi` ever returns a usable vtable.

This page is the *census*: where the table lives, how many slots it carries, how those slots map to named `_GLOBAL__sub_I_*` and `_GLOBAL__I_*` translation-unit initializers, and what kinds of objects the constructors build. It is anchored entirely against `readelf -d`/`readelf -S` (table location and size), the 2900 `R_X86_64_RELATIVE` relocations that supply the per-slot constructor addends (the on-disk slots are zero — see the GOTCHA below), and the IDA symbol table that names 2644 of the 2900 constructors. The toolchain signature is pure LLVM/clang+lld: there is **no** classic-GCC `frame_dummy` / `__do_global_ctors_aux` / `register_tm_clones` CRT scaffolding anywhere in the symbol table.

The page does **not** narrate the runtime *order* in which these slots fire, the `DT_INIT` thunk that walks them, or the matching `__cxa_atexit`/`.fini_array` teardown. That control-flow story is owned by [`lifecycle/do-init-do-fini.md`](../lifecycle/do-init-do-fini.md) and [`lifecycle/elf-entry-and-init-proc.md`](../lifecycle/elf-entry-and-init-proc.md); this page cross-links them rather than duplicating them.

For a reimplementer building a comparable plugin, the contract this page establishes is:

- **The table shape** — one `INIT_ARRAY` of 2900 8-byte slots, position-independent via `R_X86_64_RELATIVE` relocations (zero-filled on disk), plus a 2-slot `PREINIT_ARRAY` and a 2-slot `FINI_ARRAY`.
- **The constructor population** — 1885 named-by-source-file `_GLOBAL__sub_I_*.cc` initializers + 759 priority-tagged `_GLOBAL__I_*` initializers + ~234 anonymous in-span thunks + 22 low-address CRT/IFUNC initializers.
- **The work taxonomy** — which categories of static object (singletons, descriptor pools, flags, dialect/op registries, codec maps, RTTI) dominate the table, and *why* the count is in the thousands rather than the dozens.

| | |
|---|---|
| **`.init_array` VA / file off** | `0x215f26f0` / `0x213f26f0` (section [30], `INIT_ARRAY`, align 8) |
| **`.init_array` size** | `0x5aa0` = 23200 bytes → **2900 slots** (23200 / 8) |
| **`.preinit_array`** | `0x22048b30`, 16 bytes → 2 slots (section [33]) |
| **`.fini_array`** | `0x215f8190`, 16 bytes → 2 slots (section [31]) |
| **`DT_INIT` thunk** | `0xe635524` (`.init`, 0x17 bytes) — walker entry, see lifecycle |
| **`DT_FINI` thunk** | `0xe63553c` (`.fini`, 0x09 bytes) |
| **Slot population mechanism** | 2900 × `R_X86_64_RELATIVE` (addend = constructor VA) |
| **Named TU initializers** | 1885 `_GLOBAL__sub_I_*` + 759 `_GLOBAL__I_*` = 2644 |
| **Constructor-code span** | `.text.startup` `0x21217490` … `0x213818e4` = `0x16a454` (1,483,860 B / 1.41 MiB); named ctors run to `0x21380980`, the 22 CRT/IFUNC ctors sit below at `0x21211240` and lower |
| **CRT flavor** | LLVM/clang + lld — no GCC `frame_dummy`/`__do_global_ctors_aux` |

---

## The `.init_array` Table

### Location and size

`readelf -d` and `readelf -S` agree exactly. The dynamic tag `DT_INIT_ARRAY` points at `0x215f26f0` with `DT_INIT_ARRAYSZ = 23200` bytes; section header [30] `.init_array` confirms the same VA, file offset `0x213f26f0`, size `0x5aa0`, type `INIT_ARRAY`, flags `WAo` (writable — it is relocated at load), 8-byte alignment. Slot count is `23200 / 8 = 2900`, an exact integer with no padding tail.

```text
readelf -d:
  (INIT_ARRAY)     0x215f26f0
  (INIT_ARRAYSZ)   23200 (bytes)
  (PREINIT_ARRAY)  0x22048b30
  (PREINIT_ARRAYSZ)16 (bytes)
  (FINI_ARRAY)     0x215f8190
  (FINI_ARRAYSZ)   16 (bytes)
  (INIT)           0xe635524
  (FINI)           0xe63553c

readelf -S (relevant rows):
  [30] .init_array   INIT_ARRAY  215f26f0  213f26f0  005aa0  WAo  align 8
  [31] .fini_array   FINI_ARRAY  215f8190  213f8190  000010  WA   align 8
  [33] .preinit_array PREINIT_ARRAY 22048b30 21e48b30 000010 WA   align 8
```

The asymmetry is the first thing to notice: **2900 constructors, but only 2 finalizers** (`.fini_array` is 16 bytes). Almost nothing built at static-init time registers an `.fini_array` destructor; teardown of the singletons and pools is instead deferred to `__cxa_atexit` handlers (or simply leaked at process exit, which is normal for a long-lived plugin). The 2-slot `.preinit_array` runs before the main `.init_array` and is reserved for the very earliest hooks; its sequencing is a lifecycle concern.

### Slots are relocations, not on-disk pointers

A naïve `objdump -s -j .init_array` shows all-zeros — every 8-byte slot reads `00000000 00000000` in the file. The constructor addresses are **not stored as literal pointers**; each slot is the target of a base-relative relocation that the dynamic linker applies at load time.

> **GOTCHA —** do not read the `.init_array` bytes off disk and expect function pointers; you will get 2900 zeros. The real constructor addresses live in the relocation *addends*. There are exactly **2900 `R_X86_64_RELATIVE` relocations** whose `r_offset` falls inside `[0x215f26f0, 0x215f8190)` — one per slot — and the addend of each is the load-relative VA of the constructor. Slot 0 (lowest `r_offset`) has addend `0x21211240` (`__cpu_indicator_init`); the highest addend *value* across all slots is `0x21380980` (it lands at slot 2, not the last slot — addends are not monotone in slot order). Any census that parses the section bytes instead of the relocation table will count 2900 null constructors.

This is the expected encoding for a PIE/`-fPIC` shared object: position independence forces the loader, not the linker, to materialize absolute constructor addresses, and `R_X86_64_RELATIVE` (addend-carrying, symbol-less) is the cheapest form. The 2900 relocations are a non-trivial slice of the binary's total relative-relocation load.

| Anchor | Value |
|---|---|
| Relocations targeting `.init_array` | 2900 × `R_X86_64_RELATIVE` |
| Slot-0 addend | `0x21211240` |
| Highest in-range addend | `0x21380980` |
| On-disk slot bytes | all zero (relocated at load) |

---

## Constructor Population

### Census by symbol family

Of the 2900 slots, IDA's symbol table names 2644 distinct constructors. They split into two clang naming families plus an anonymous remainder and a small CRT prefix.

| Family | Count | Form | What it is |
|---|---|---|---|
| `_GLOBAL__sub_I_<file>.cc` | 1885 | source-file-keyed | Default-priority TU initializer, one per `.cc` with non-trivial namespace-scope ctors |
| `_GLOBAL__I_<priority>` | 759 | priority-keyed | Initializer for objects with an explicit `init_priority` attribute |
| anonymous in-span ctor | ~234 | unnamed `sub_` | Constructor code in the TU-init span that IDA did not resolve to a `_GLOBAL__` symbol |
| low-address / CRT thunk | 22 | e.g. `__cpu_indicator_init` | Pre-C++ initializers below the TU-init span (IFUNC/CPU detection, runtime helpers) |

The arithmetic closes: 1885 + 759 = 2644 named; the named-constructor code span is `[0x21217490, 0x21380980]`, and **2878** of the 2900 relocation addends land inside that span, leaving 2878 − 2644 = **234 anonymous ctors** in the span and **22** addends *below* it (`< 0x21217490`). No addend exceeds the span. The 234 anonymous entries are real constructors — the relocation points into executable code in the same region — that simply lack a `_GLOBAL__` symbol because clang emitted them for an anonymous-namespace object or because the symbol was stripped.

> **Note:** the binary's symbol table contains **1885** distinct `_GLOBAL__sub_I_*` symbols (all unique) and a separate priority-init family of **759** `_GLOBAL__I_*` symbols. These are deduped symbol-table counts; a grep over the decompile tree (which carries one record per artifact file) inflates them and should not be used for the slot accounting here. The `.init_array` table size is 2900 slots / 23200 bytes.

### The two naming families

clang emits two distinct shapes of TU initializer, and both appear here:

- **`_GLOBAL__sub_I_<file>.cc`** — the default case. Every translation unit that needs *any* non-trivial namespace-scope initialization gets exactly one of these, named after its source file. Because libtpu statically links many components that share source-file names, the same base name recurs with a `_N` dedup suffix: `metrics.cc` appears 8 times (`metrics.cc`, `metrics.cc_0` … `metrics.cc_6`), `flags.cc`, `trace_codec_factory.cc`, `performance_counters.cc`, `kernel_firmware_factory.cc`, and `hardware_attributes_factory.cc` 6 times each. These are genuinely distinct TUs from different statically-linked libraries that happened to use the same filename.

- **`_GLOBAL__I_<priority>`** — the `init_priority` case. When code annotates a global with `__attribute__((init_priority(N)))`, clang keys the initializer to the numeric priority instead of the filename, so the linker can order it. The distribution is lopsided: **757** of the 759 sit at priority `000102`, with one each at `000100` and `000101`. Priority 100 is the earliest-firing; the constructor at `0x21380980` is `_GLOBAL__I_000100` (demangled: `` `global constructor keyed to'000100``), the single highest-priority object in the whole binary.

> **NOTE —** the priority *number* in the symbol name (102, 101, 100) is the C++ `init_priority` value, which controls relative order within a TU/link unit; it is not the slot index in `.init_array`. The runtime firing order across the full 2900-slot table is a lifecycle concern — see [`do-init-do-fini.md`](../lifecycle/do-init-do-fini.md).

### The constructor-code span

All 2644 named constructors, and the ~234 anonymous in-span ones, live in the dedicated `.text.startup` section — a single contiguous code region `[0x21217490, 0x213818e4)`, size `0x16a454` = **1,483,860 bytes (1.41 MiB)**. The 2878 in-band addends run from the section base `0x21217490` up to the highest addend at `0x21380980` (which resolves to `_GLOBAL__I_000100`); the remaining bytes up to `0x213818e4` hold non-`.init_array` startup code such as `__cxx_global_array_dtor` thunks. clang/lld groups TU initializers into this section, which is why the `.init_array` slots (at `0x215f26f0`) point into one tight band rather than scattering across the 745 MB `.text`. The 22 low addends (`< 0x21217490`) point *outside* this band, into earlier code — these are the CRT/IFUNC initializers (`__cpu_indicator_init` at `0x21211240`, the upb registry constructor at `0x201e7360`, BoringSSL's power-on self-test, Rust's `ARGV_INIT_ARRAY` wrapper, `__do_init`, `setup_dl_debug_hook`) that must run before any C++ TU object is constructed.

---

## What the Constructors Build

The 2900 slots are not 2900 different *kinds* of work; they are thousands of instances of a half-dozen recurring static-registration idioms, each of which a large C++/MLIR/XLA codebase emits per-TU. Categorizing by the source-file name embedded in the `_GLOBAL__sub_I_*` symbol (a keyword scan over the 1885 named TUs) gives the breakdown below. Counts are lower bounds — a single TU often performs several kinds of registration, and the category is inferred from the filename plus corroborating decompiled bodies.

| Category | TU keyword evidence | What the constructor does | Approx. TUs |
|---|---|---|---|
| Op / kernel registration | `*_ops.cc` (156 TUs) | `REGISTER_OP` / `REGISTER_KERNEL` statics push op definitions into a global op registry | ≥156 |
| Factory / static registry | `*factory*` (79), `*registr*` (26) | Self-registering factories install a `make_*` callback into a name→factory map (driver, codec, kernel-firmware, snap-analyzer, device-scanner) | ≥79 |
| Flag registration | `flags.cc` / `*flags*` (50) | `ABSL_FLAG`/gflags definitions register a flag descriptor and default into the global flag table | ≥50 |
| Metrics / counters | `metrics.cc` (8), `performance_counters.cc` (6), `*metric*` (17) | Construct metric/counter descriptor singletons and register them | ≥17 |
| Dialect / pass / HLO registration | `*_registration.cc` (22), plus `mlir_*`/`*hlo*`/`*pass*` siblings | Register MLIR dialects/passes and HLO graph-optimization passes into pass-pipeline registries | ≥22 |
| Codec / static-map registration | `codec_metadata_*` (ghostlite/jellyfish/pufferfish/viperfish), `trace_codec_factory.cc` (×6) | Build per-codec static descriptor maps and register codec factories keyed by ASIC generation | ≥10 |
| Proto / descriptor-pool | `*proto*`, `*descriptor*` (7) | Register generated message descriptors into the protobuf descriptor pool; reflection plugins | ≥7 |
| Meyers singletons / RTTI | pervasive (not filename-keyed) | Construct function-local-static and namespace-scope singletons; emit type-info for polymorphic types | — |

> **QUIRK —** the codec category is keyed by ASIC fish-codenames — `ghostlite`, `jellyfish`, `pufferfish`, `viperfish` each get a `codec_metadata_*.cc` TU initializer, and `trace_codec_factory.cc` appears six times (`.cc_0` … `.cc_4`). A reimplementer cannot collapse these into one codec init: each generation registers its own static descriptor map at load time, so the per-generation dispatch the runtime relies on (see [`per-gen-function-dispatcher.md`](per-gen-function-dispatcher.md)) is *populated entirely by static-init*, not lazily.

### Why the count is in the thousands

Two structural facts drive the 2900 figure, and both matter to anyone estimating a comparable plugin's load cost:

1. **Static self-registration is per-TU and per-symbol.** The op/kernel/flag/factory idioms each emit *one global object per registered entity*. A file with 40 `REGISTER_OP` macros produces 40 namespace-scope objects, all constructed in that file's single `_GLOBAL__sub_I_` — and there are 156 `*_ops.cc` TUs. The registries (op table, flag table, codec map, pass pipeline, descriptor pool) are therefore fully materialized before the plugin answers its first query.

2. **libtpu statically links a very large tree.** The recurrence of common filenames (`metrics.cc` ×8, `flags.cc` ×6) is the fingerprint of many independent libraries — XLA, the MLIR/HLO compiler stack, the absl runtime, the per-generation `gxc`/`pxc`/`vxc` driver and profiler code — all folded into one `.so`. Each contributes its own TU initializers. The result is a constructor table an order of magnitude larger than a self-contained library would produce.

The practical consequence is that the `.init_array` walk is a meaningful, single-threaded, allocation-heavy phase of `dlopen(libtpu.so)`: 2900 constructors that touch global registries, allocate descriptor maps, and run RTTI setup, all before any TPU is even probed.

### CRT and pre-C++ initializers

Slot 0's addend `0x21211240` resolves to **`__cpu_indicator_init`**, not a `_GLOBAL__` constructor. This is the clang/GCC runtime helper that populates `__cpu_model` / `__cpu_features2` so that `__builtin_cpu_supports` and IFUNC resolvers work; it must run before any SIMD-dispatched code or IFUNC is called, which is why it occupies the earliest slot. It is one of the 22 low-address addends. The absence of `frame_dummy`, `register_tm_clones`, `deregister_tm_clones`, and `__do_global_ctors_aux` — all zero hits in the symbol table — confirms this is an lld-linked, clang-CRT object: there is no classic-GCC `crtbegin.o`/`crtend.o` constructor walker; the kernel/loader invokes `DT_INIT_ARRAY` directly.

> **NOTE —** because there is no `__do_global_ctors_aux` self-walk, the constructor ordering is whatever lld laid down in `.init_array` plus the `init_priority` keys (100/101/102) that pulled three objects to the front. A reimplementer relying on classic-GCC constructor-priority semantics will not find that machinery here; the ordering contract is the array order itself.

---

## Notable Constructors by Address

A handful of slots are worth calling out individually — either because of their position (first/last) or because they anchor a category. Addresses are the relocation addends (constructor VAs); names are from the IDA symbol table.

| Address | Symbol | Role |
|---|---|---|
| `0x21211240` | `__cpu_indicator_init` | Slot 0 — CPU-feature / IFUNC detector; must precede all SIMD dispatch |
| `0x21380980` | `_GLOBAL__I_000100` | Highest-priority (`init_priority(100)`) global ctor — earliest C++ object |
| `0x21371040` | `_GLOBAL__sub_I_flags.cc` | Flag-table registration cluster (also `0x21378ab0`, `0x21379660`, `0x2137ace0`, …) |
| `0x21218610` | `_GLOBAL__sub_I_xla_ops.cc` | XLA op registration (recurs as `xla_ops.cc_0` at `0x2121f2d0`, ×4 total) |
| span anchor | `_GLOBAL__sub_I_codec_metadata_{ghostlite,jellyfish,pufferfish,viperfish}.cc` | Per-generation codec descriptor-map registration |
| span anchor | `_GLOBAL__sub_I_mlir_bridge_pass_registration.cc` / `*_graph_optimization_pass_registration.cc` | MLIR/HLO pass-pipeline registration |

> **NOTE —** beyond the four anchors above, the named span is dense with op/factory/flag initializers at one-constructor granularity; enumerating all 2644 named slots would be a 2644-row dump with no reimplementation value. The category table and the symbol-family census above describe the *shape* of the population; the per-address detail for any single registry lives on that registry's own page (op tables → [`dispatch-table-taxonomy.md`](dispatch-table-taxonomy.md), RTTI → [`rtti-vtable-census.md`](rtti-vtable-census.md)).

### What was not traced

This page resolved the table geometry (size, slot count, relocation encoding) to `CERTAIN`, and the named-symbol families to `CERTAIN`/`HIGH`. What remains inferred:

- The **234 anonymous in-span constructors** were counted by subtraction (addends in `[0x21217490, 0x21380980]` minus named symbols), not individually decompiled. They are confirmed to be constructor code by their relocation targets; their specific categories are not enumerated.
- The **per-category TU counts** are filename-keyword lower bounds. A TU named `*_ops.cc` is high-confidence an op registrar, but a single TU may also register flags or metrics, so the categories overlap and the totals do not sum to 2644.
- The **runtime order and timing** of the walk — which slot fires first, how `__cxa_atexit` mirrors them, and what the 2-slot `.preinit_array` does — is out of scope by design and is owned by the lifecycle pages.

---

## Cross-References

- [Do-Init / Do-Fini](../lifecycle/do-init-do-fini.md) — owns the runtime `.init_array` *walk* and `__cxa_atexit`/`.fini_array` teardown; the control-flow companion to this static census
- [ELF Entry and Init Proc](../lifecycle/elf-entry-and-init-proc.md) — the `DT_INIT` (`0xe635524`) thunk and `.preinit_array` sequencing that drive the table
- [Lifecycle Overview](../lifecycle/overview.md) — where static-init sits in the full load → init → serve → teardown timeline
- [Module Init / Plugin Discovery](../lifecycle/module-init-plugin-discovery.md) — what runs *after* the constructor table is drained, when registries built here are first queried
- [Forensics Overview](overview.md) — the binary-anatomy map this census is one chapter of
- [ELF Anatomy](elf-anatomy.md) — owns the full section table; `.init_array`/`.fini_array`/`.preinit_array` section rows
- [RTTI / Vtable Census](rtti-vtable-census.md) — the type-info registration that many of these constructors perform
- [Per-Generation Function Dispatcher](per-gen-function-dispatcher.md) — the gxc/pxc/vxc dispatch tables that the codec/factory constructors populate at load time
- [Dispatch-Table Taxonomy](dispatch-table-taxonomy.md) — the op/kernel/factory registries filled by the `*_ops.cc` and `*_factory.cc` initializers
