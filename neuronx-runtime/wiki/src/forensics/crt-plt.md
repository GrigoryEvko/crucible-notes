# CRT / PLT / Loader Surface

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB **DYN** x86-64, NOT stripped, DWARF v4.
>
> **Part II — Binary Anatomy & Forensics** / DEEP reference · **Evidence grade:** every count below is re-derived from this exact binary — `readelf -rW`/`-dW`/`-SW`, `objdump -d -j .plt`/`-j .init`/`-j .fini`, `readelf --dyn-syms`, the `.rela.plt` `JUMP_SLOT` table bucketed by symbol-version node — and cross-checked against the IDA decompiled sidecars under `ida/**/decompiled/`. · [back to forensics index](overview.md)

## Abstract

`libnrt.so` is a `DYN` object with entry point `0x0` and no `_start`; the C-runtime startup and dynamic-linker glue on this page is the boilerplate that lets that object reach libc, libstdc++, libgcc, and its own sibling shared libraries. It is two distinct machineries that a generic `gcc -shared` artifact also carries, here pinned to `libnrt`'s exact addresses. The first is the **PLT/GOT lazy-binding apparatus**: a `.plt` of **442** 16-byte entries (`0x3c020`, `0x1ba0/16`) — one reserved trampoline `PLT0` plus **441** import stubs — backed 1:1 by **441** `R_X86_64_JUMP_SLOT` relocations in `.rela.plt` (`0x38bb0`, `0x2958/24`) and **444** `.got.plt` slots (`0xc06fe8`, `0xde0/8` = 3 reserved + 441). The second is the **init/fini framing** that the [Static-Init](static-init.md) page owns the *array* half of: `_init`/`_fini` (`DT_INIT`/`DT_FINI`), the GCC `crtstuff.c` glue (`frame_dummy`, `register_tm_clones`, `__do_global_dtors_aux`), and the `__cxa_*` exception/atexit imports the constructors feed.

The interesting content of the PLT is *what* it imports and *why those imports matter*. The 441 stubs are not uniform: bucketed by symbol-version node they are **227 `GLIBC_` (libc + pthread/sem + dl), 150 `GLIBCXX_` (libstdc++), 42 `@@NRT_` (libnrt's own exported symbols, reached through the SONAME-versioned dynamic table), 17 `CXXABI_` (the C++ exception/typeinfo runtime), 2 `GCC_` (libgcc unwinder), and 3 unversioned weak hooks** (`__gcov_dump`/`__gcov_flush` and one Abseil cord TLS-init thunk). A handful of those 441 are the entire reason the runtime is a *system* component rather than a leaf library: `dlopen`/`dlsym` (`0x3d3c0`/`0x3d950`) are how libnrt brings up its sibling images `libncfw.so` and `libnrtucode_extisa.so` at runtime; `ioctl`/`mmap`/`mmap64`/`munmap` are the entire kernel boundary to the Neuron device node; the 30 `pthread_*`/`sem_*` imports are the threading floor; and the libstdc++ `operator new`/`operator delete` family is the allocator every C++ container on the heap routes through.

This page is organized as: the **PLT/GOT lazy-binding model** (the `call → PLT[n] → GOT → resolver → rebind` cycle) as it actually disassembles in this binary; the **508-entry classification dimension table** that describes the whole CRT/PLT cell by its axes rather than dumping 441 trampoline rows; the **imports-that-matter table** naming the concrete symbols a reimplementer must wire and why each one is consequential; the **first-call resolution** as annotated pseudocode anchored to `PLT0` @`0x3c020`; and the lazy-binding/RELRO **quirks**. The `.init_array` constructor table, the `_GLOBAL__sub_I_*` thunks, and the teardown/`__cxa_finalize` walk are **not** re-derived here — they are owned by [Static-Init](static-init.md); this page references that surface and owns only the trampoline mechanics and the import catalogue.

For reimplementation, the contract is:

- **The lazy-bind geometry** — `.plt` is `PLT0 + 441` stubs; each stub `i` lives at `0x3c030 + 0x10*i` and is `jmp *GOT.plt[3+i](%rip); push $i; jmp PLT0`. `PLT0` (`0x3c020`) is `push GOT.plt[1]; jmp *GOT.plt[2]`. The 3 reserved `.got.plt` slots are `[0]=_DYNAMIC` (`0xc05540`), `[1]=link_map` (filled by `ld.so`), `[2]=_dl_runtime_resolve` (filled by `ld.so`). There is **no** `.plt.sec`/CET second PLT, and **no** IFUNC/IRELATIVE (0 `R_X86_64_IRELATIVE`).
- **The import classification** — 441 = 227 `GLIBC_` + 150 `GLIBCXX_` + 42 `@@NRT_` + 17 `CXXABI_` + 2 `GCC_` + 3 unversioned. A port must reproduce *which* node each import binds against, because that is the ABI floor (`GLIBC_2.28`, `GLIBCXX_3.4.22`, `CXXABI_1.3.11`, `GCC_4.2.0`) the host must satisfy.
- **The imports that matter** — `dlopen`/`dlsym` (sibling-`.so` bring-up), `ioctl`/`mmap*` (the device kernel boundary), the `pthread_*` floor, the `operator new`/`delete` allocator set, and the `__cxa_*` exception runtime. These ~50 stubs carry the runtime's external behavior; the other ~390 are string/stream/math leaf calls.
- **The self-PLT quirk** — 42 stubs target libnrt's *own* `@@NRT_2.0.0`/`@@NRT_3.0.0` exports (e.g. `nrt_unload@plt` → `0xaa190`, `nrta_get_sequence@plt` → `0x7c450`); these re-enter the same `.text` through the dynamic symbol table because they are SONAME-versioned exports, not because they are external.

| | |
|---|---|
| **`.plt`** | `0x3c020`, `0x1ba0` = **442** × 16 B (`PLT0` + 441 stubs) |
| **`.rela.plt`** | `0x38bb0`, `0x2958` = **441** `JUMP_SLOT` × 24 B (`PLTRELSZ 10584` = 441×24) — **re-derived: `readelf -rW \| grep JUMP_SLOT \| wc -l` = 441** |
| **`.got.plt`** | `0xc06fe8`, `0xde0` = **444** slots (3 reserved + 441) |
| **`PLT0`** | `0x3c020` — `push GOT.plt[1] (0xc06ff0); jmp *GOT.plt[2] (0xc06ff8)` |
| **First stub** | `0x3c030` `sem_destroy@plt` → `GOT.plt[3]` (`0xc07000`); index `push $0x0` |
| **`DT_INIT` / `_init`** | `0x3c000` (`crti.o`; weak-checks `__gmon_start__` via `.got` @`0xc06ee0`) |
| **`DT_FINI` / `_fini`** | `0x7ce8e8` (`crtn.o`; empty body) |
| **`__gmon_start__`** | `R_X86_64_GLOB_DAT` @`0xc06ee0` (`.got`, **not** a PLT slot), `WEAK UND` → NULL |
| **Catalog total** | **508** = 441 PLT + `_init`/`_fini`/`PLT0` (3) + 6 first-party `.text` + `__gmon_start__` (1) + 57 re-listed import names ([§ reconciliation](#the-508-entry-classification)) |
| **PLT model** | classic lazy-binding; **no** `.plt.sec`/CET; **no** IFUNC/IRELATIVE (0 `R_X86_64_IRELATIVE`) |

> **NOTE — the array half of the CRT lives on a sibling page.** `_init`/`_fini`, the 77-entry `.init_array`, the `_GLOBAL__sub_I_*` constructor thunks, `frame_dummy`/`register_tm_clones`, and the `__cxa_finalize` teardown are documented in full by [Static-Init](static-init.md). This page owns the **PLT/GOT lazy-bind mechanics** and the **import catalogue**; it cites the init/fini functions only where they touch the PLT (e.g. `_init`'s `__gmon_start__` GOT read, `__do_global_dtors_aux`'s `__cxa_finalize@plt` call). The constructor table is deliberately not duplicated.

---

## The PLT/GOT Lazy-Binding Model

`libnrt`'s PLT is the textbook System V x86-64 *lazy* PLT — the same shape GCC/Clang emit for any `-shared` object built without `-Wl,-z,now`. A reimplementer who knows the `_dl_runtime_resolve` dance already owns this; the only libnrt-specific facts are the addresses and the 42 self-referential `@@NRT_` stubs.

### The three players

```text
.plt        0x3c020   442 × 16 B   PLT0 (the resolve trampoline) + 441 import stubs
.got.plt    0xc06fe8  444 × 8 B    [0]=_DYNAMIC  [1]=link_map  [2]=_dl_runtime_resolve
                                   [3..443] = 441 import slots (one per stub)
.rela.plt   0x38bb0   441 × 24 B   one R_X86_64_JUMP_SLOT per slot [3..443], in stub order
```

The three reserved `.got.plt` head slots are the loader's scratch: `[0]` (`0xc06fe8`) holds `&_DYNAMIC` (`0xc05540`, confirmed in the raw dump — little-endian `4055c000…`); `[1]` (`0xc06ff0`) and `[2]` (`0xc06ff8`) are zero on disk and filled by `ld.so` at map time with this object's `link_map` pointer and the address of `_dl_runtime_resolve`. Every import slot `[3+i]` is **pre-initialized on disk to point back at its own stub's second instruction** (the `push $i`), so the *first* call to an unresolved import falls through to the resolver; after resolution the loader overwrites the slot with the real target and every subsequent call jumps straight there.

### Entry-point flow — call → PLT[n] → GOT → resolver → rebind

```text
caller                          first call (cold)                  later calls (warm)
  │                                                                       │
  │ call sem_destroy@plt                                                  │ call sem_destroy@plt
  ▼                                                                       ▼
0x3c030 sem_destroy@plt:                                          0x3c030 sem_destroy@plt:
  jmp *0xc07000(%rip) ──► GOT.plt[3] ── still points to ─┐          jmp *0xc07000(%rip) ──► GOT.plt[3]
                          0x3c036 (the push below)        │                                  │
  0x3c036: push $0x0   ◄──────────────────────────────────┘                                  │ now = &sem_destroy
  0x3c03b: jmp 0x3c020 (PLT0)                                                                 ▼
            │                                                                          libc sem_destroy
            ▼
0x3c020 PLT0:
  push GOT.plt[1]        # 0xc06ff0 — this object's link_map
  jmp *GOT.plt[2]        # 0xc06ff8 — _dl_runtime_resolve(link_map, reloc_index=0)
            │
            ▼
  _dl_runtime_resolve → _dl_fixup:
     reloc = .rela.plt[0]                 # JUMP_SLOT for sem_destroy
     target = lookup(sem_destroy@GLIBC_2.2.5)
     *GOT.plt[3] = target                 # REBIND: slot now points at libc
     jmp target                           # tail into the resolved function
```

The `push $i` immediate (`$0x0` for stub 0, `$0x1` for `ftell@plt`, `$0x2` for `_Znam@plt`, …) is the index into `.rela.plt` that `_dl_fixup` uses to find the symbol; it is exactly the stub's position after `PLT0`, so stub `i` pushes `i`. PLT0's `push GOT.plt[1]` supplies the `link_map`, the second argument the resolver needs to scope the lookup to this object's dependency set.

> **QUIRK — 42 of the 441 stubs resolve back into `libnrt`'s own `.text`.** `nrt_unload@plt` (`0x3c060`) targets `GOT.plt[6]`, whose `JUMP_SLOT` names `nrt_unload@@NRT_2.0.0` with a **non-zero** reloc target `0xaa190` — an address *inside* this same binary. Likewise `nrt_get_version@plt` → `0x940b0` and `nrta_get_sequence@plt` → `0x7c450` (`@@NRT_3.0.0`). These are not external imports: they are libnrt's SONAME-versioned **exports** that some internal call sites reach *through the PLT* (and thus through the dynamic symbol table) rather than by a direct `call`, because the compiler emitted a PLT-relative call for a symbol marked default-visibility-exported. A reimplementation that assumes "every PLT stub is an external dependency" will wrongly treat 42 of its own functions as undefined. The tell is the reloc-target column in `readelf -rW`: external imports show `0000000000000000`, self-exports show their own VMA.

---

## The 508-Entry Classification

The CRT/PLT cell catalogs **508** entries. Dumping 441 trampoline rows would teach a reimplementer nothing — every row is the same three instructions with a different index and GOT offset. The value is the *classification*: which kinds exist, how many of each, and what role each kind plays. The table below is the dimension table for the whole cell; the [imports-that-matter](#the-imports-that-matter) section then drills into the rows that carry behavior.

### By kind

| Kind | Count | Role | Conf |
|---|---:|---|---|
| **PLT-libc** (`@GLIBC_`, excl. pthread/sem/dl/cxa) | **191** | libc + librt syscall/stdio/str·mem/time wrappers — the bulk leaf surface | CERTAIN |
| **PLT-libstdc++** (`@GLIBCXX_`) | **150** | `std::basic_string`/`ostream`/`istream`/`locale`/`ios_base` ctors-dtors, `operator new`/`delete` (`_Znwm`/`_Znam`/`_ZdlPv`), `_Rb_tree_*`, `__throw_*` | CERTAIN |
| **PLT-nrt-self** (`@@NRT_2.0.0`/`@@NRT_3.0.0`) | **42** | libnrt's own SONAME-versioned exports reached *through* the PLT (see quirk above) | CERTAIN |
| **PLT-pthread/sem** (`@GLIBC_`) | **30** | `pthread_create`/`join`/`mutex_*`/`cond_*`/`rwlock_*`/`key_*`/`attr_*`, `sem_*` — threading floor | CERTAIN |
| **PLT-cxxabi/unwind** (`@CXXABI_`) | **17** | `__cxa_throw`/`begin_catch`/`allocate_exception`/`guard_*`, `__dynamic_cast`, sized/aligned `new`/`delete`, `_Hash_bytes` | CERTAIN |
| **PLT-dl** (`@GLIBC_`) | **4** | `dlopen`/`dlsym`/`dlclose`/`dlerror` — the sibling-`.so` loader (`libncfw`/`libnrtucode_extisa`) | CERTAIN |
| **PLT-libgcc** (`@GCC_`) | **2** | `_Unwind_Resume`, `__umodti3` — unwinder + 128-bit modulo intrinsic | CERTAIN |
| **PLT-cxa-atexit** (`@GLIBC_`) | **2** | `__cxa_atexit`/`__cxa_finalize` — the teardown registration the ctors feed | CERTAIN |
| **PLT-weak-hooks** (unversioned) | **3** | `__gcov_dump`/`__gcov_flush` (gcov, weak→NULL); one Abseil `cordz` TLS-init thunk | HIGH |
| **init-fini** (`_init`, `_fini`, `PLT0`) | **3** | `DT_INIT` @`0x3c000`, `DT_FINI` @`0x7ce8e8`, the resolve trampoline @`0x3c020` | CERTAIN |
| **first-party `.text`** (crtstuff + EH helper + cold slice) | **6** | `deregister_tm_clones`/`register_tm_clones`/`__do_global_dtors_aux`/`frame_dummy` (crtstuff), `__cxa_call_terminate` (`0x70c92`), `get_xu_common.part.0` (`0x7b000`) | CERTAIN |
| **weak external hook** | **1** | `__gmon_start__` `WEAK UND` (gprof), read by `_init` via `.got` @`0xc06ee0` | CERTAIN |
| **re-listed import names** (IDA duplicates) | **57** | the same imports re-emitted by IDA without the leading `.`/weak UND names not defined here — **not** distinct functions | HIGH |

The 441 PLT rows split exactly as 191 + 150 + 42 + 30 + 17 + 4 + 2 + 2 + 3 = **441**, re-derived by bucketing the `.rela.plt` `JUMP_SLOT` symbol column on its `@@?<node>` version tag.

> **NOTE — the 508 reconciliation, re-derived.** 441 PLT stubs + 3 (`_init`/`_fini`/`PLT0`) + 6 first-party `.text` + 1 (`__gmon_start__`) = **451** genuinely distinct entries. The remaining **57** are the same imports the IDA sidecar re-listed without the catalog's leading dot (e.g. it carries both `.sem_destroy` *and* `sem_destroy`) plus weak/UND import *names* IDA materializes that are referenced but not defined in `.text` (e.g. the `WEAK UND` `gettid`, `pidfd_spawnp`, `posix_spawn_file_actions_*` glibc-feature probes seen in `readelf --dyn-syms`). They are byte-identical to PLT stubs already counted, or zero-resolving weak placeholders — a naming duplication in the catalog, not 57 extra functions. Every one of the 508 is thus accounted for; none is unclassified.

> **CORRECTION (CRT-PLT vs L-CRT-01 §B/§F bucketing) —** the seed catalog split the libc surface as "199 libc + 30 pthread/sem". Re-bucketing the 441 `JUMP_SLOT` relocs strictly by version node gives a cleaner partition: the `@GLIBC_` node holds **227** symbols total = 191 pure-libc/librt + 30 pthread/sem + 4 dl + 2 `__cxa_atexit`/`__cxa_finalize`. The seed's "199 libc" folded the 4 `dl*` and the 2 `__cxa_*` into its libc count and split pthread differently; the version-node partition above is the reproducible one (`readelf -rW | grep JUMP_SLOT`, then `sed -E 's/.*@@?//'`). The **total 441 is unchanged**; only the sub-bucketing is corrected.

---

## The Imports That Matter

Of the 441 imports, roughly 390 are leaf calls into libc string/stream/math or libstdc++ container glue — present, but they carry no runtime *decision*. The ~50 below are the ones a reimplementer must wire deliberately, because each one is a boundary to another subsystem, the kernel, or the heap. Symbol names are the verbatim mangled/demangled tokens from `.rela.plt`; "Why it matters" names the concrete consumer in this binary.

| Import (verbatim) | From | Why it matters | Conf |
|---|---|---|---|
| `dlopen` / `dlsym` / `dlclose` / `dlerror` | libdl (`@GLIBC_2.2.5`) | **sibling-`.so` bring-up.** `encd_libncfw_init` (`0x251cc0`) calls `dlopen@plt` (`0x3d3c0`) then `dlsym@plt` (`0x3d950`) to resolve `libncfw_get_image_func` (stored @`0xc96d10`) from **`libncfw.so`**; `ucode_init_module` (`0x225940`) does the same at `0x2259a8`/`0x2259e6` to load **`libnrtucode_extisa.so`**. These 4 stubs are how the public runtime reaches the firmware/ucode images it does *not* `DT_NEEDED`. | CERTAIN |
| `ioctl` | libc (`@GLIBC_2.2.5`) | **the device kernel boundary.** Every command to the Neuron device node funnels through `ioctl` via the `KaenaDriverLib` (`ndl.c`) shim; there is no other syscall path to the hardware. A port's entire driver surface binds here. | CERTAIN |
| `mmap` / `mmap64` / `munmap` | libc (`@GLIBC_2.2.5`) | **device/DMA memory mapping.** `mmap64` maps device BARs and DMA-able host buffers into the process; `munmap` tears them down. The kernel boundary for memory, paired with `ioctl` for control. | CERTAIN |
| `pthread_create` / `pthread_join` | libpthread (`@GLIBC_2.2.5`) | **worker bring-up/teardown.** The `kmgr` async-exec and `xu_worker` pools spawn here; the threading floor a port must provide. | CERTAIN |
| `pthread_mutex_lock` / `_unlock`, `pthread_cond_wait` / `_signal`, `pthread_rwlock_*` | libpthread (`@GLIBC_2.2.5/2.3.2`) | **the synchronization primitives** behind `g_shared_proxy_queues`, the kv-store, and the ndebug `stream_locks[]` array ([Static-Init](static-init.md) slot 25). 30 `pthread_*`/`sem_*` imports in total. | CERTAIN |
| `_Znwm` / `_Znam` / `_ZdlPv` / `_ZdaPv` (`operator new`/`new[]`/`delete`/`delete[]`) | libstdc++ (`@GLIBCXX_3.4`) | **the C++ heap allocator.** Every `std::` container and first-party C++ object that escapes the stack allocates through these. 11 operator new/delete variants total — the baseline four plus the `CXXABI_1.3.9/3.11` sized (`_ZdlPvm`) and aligned (`…St11align_val_t`) forms. | CERTAIN |
| `__cxa_throw` / `__cxa_begin_catch` / `__cxa_allocate_exception` / `__cxa_end_catch` | libstdc++/cxxabi (`@CXXABI_1.3`) | **the C++ exception engine.** The runtime throws and catches across its C++ surface (NEFF parse, collectives); `__cxa_call_terminate` (`0x70c92`, first-party) is the terminator these feed on an EH cleanup failure. | CERTAIN |
| `__cxa_atexit` / `__cxa_finalize` | libc (`@GLIBC_2.2.5`) | **the teardown spine.** Each non-trivial C++ global registers `~T` via `__cxa_atexit(&~T,&obj,&__dso_handle)` at construction; `__do_global_dtors_aux` (`0x7af90`, `.fini_array[0]`) calls `__cxa_finalize@plt` at unload. Full walk → [Static-Init](static-init.md). | CERTAIN |
| `__cxa_guard_acquire` / `_release` / `_abort` | cxxabi (`@CXXABI_1.3`) | **function-local static init guards** — the thread-safe one-time init for `static T x = …` inside functions, distinct from the `.init_array` globals. | CERTAIN |
| `_Unwind_Resume` | libgcc (`@GCC_3.0`) | **stack unwinding** — the cleanup-resume the LSDA landing pads ([`.gcc_except_table`](elf-anatomy.md)) tail into; one of only 2 `@GCC_` imports. | CERTAIN |
| `__dynamic_cast` | cxxabi (`@CXXABI_1.3`) | **RTTI down/cross-cast** — the runtime's only `dynamic_cast` path; the [RTTI hierarchy](rtti-class-hierarchy.md) is single-inheritance, so this is the `__si_class_type_info` walk. | HIGH |
| `__assert_fail` | libc (`@GLIBC_2.2.5`) | **the assert sink** — `get_xu_common.part.0` (`0x7b000`) and ~2,300 other call sites fatal through it; the first leaf a port stubs. | CERTAIN |
| `__stack_chk_fail` | libc (`@GLIBC_2.4`) | **`-fstack-protector` trap** — a genuine `JUMP_SLOT` import (`@GLIBC_2.4`), present because the build enables stack canaries; the abort path every guarded frame jumps to on a smashed canary. | CERTAIN |

> **GOTCHA — the absence of an import is itself a fact.** `libnrt` `DT_NEEDED`s only 9 libraries ([ELF Anatomy §3](elf-anatomy.md)); there is **no** PLT stub for any protobuf, Abseil, simdjson, libarchive, zlib, or Rust symbol, because all of those are *statically vendored* into `.text` ([SBOM](vendored-sbom.md)) and call each other by direct `call`, never through the PLT. A reimplementer who links those dependencies dynamically will see hundreds of new `JUMP_SLOT` relocs that this binary does not have. The PLT histogram (227 GLIBC / 150 GLIBCXX / 42 self / 17 CXXABI / 2 GCC) is the *signature* of a statically-vendored monolith: the only external imports are the C library, the C++ runtime, the unwinder, and the loader itself.

---

## First-Call Resolution

The cold-path resolution, modeling the stub + `PLT0` + `_dl_fixup` as annotated pseudocode. The warm path is one instruction (`jmp *GOT.plt[3+i]`); only the first call per import touches the resolver.

```c
// sem_destroy@plt @0x3c030  (stub i=0; identical shape for all 441, index and GOT offset vary)
function plt_stub_i(/* args already in registers */):
    target = *GOT_plt[3 + i]            // 0x3c030: jmp *0xc07000(%rip) — slot [3] for i=0
    goto target
    // On disk GOT_plt[3+i] points at the NEXT instruction, so a cold call falls through:
  push_index:
    push i                              // 0x3c036: push $0x0 — reloc index into .rela.plt
    goto PLT0                           // 0x3c03b: jmp 0x3c020

// PLT0 @0x3c020 — the one resolve trampoline shared by all 441 stubs
function PLT0(reloc_index_on_stack):
    push *GOT_plt[1]                    // 0x3c020: push 0xc06ff0(%rip) — this object's link_map
    goto *GOT_plt[2]                    // 0x3c026: jmp *0xc06ff8(%rip) — _dl_runtime_resolve

// _dl_runtime_resolve → _dl_fixup (in ld.so; modeled for completeness)
function dl_fixup(link_map, reloc_index):
    reloc  = &link_map.rela_plt[reloc_index]        // .rela.plt @0x38bb0, 24 B stride
    sym    = link_map.dynsym[ELF64_R_SYM(reloc.info)]
    target = symbol_lookup(sym.name, sym.version)   // e.g. sem_destroy@GLIBC_2.2.5
    *(link_map.got_plt[3 + reloc_index]) = target   // REBIND the slot in place
    goto target                                     // tail-call the real function
```

The rebind at `*(got_plt[3+reloc_index]) = target` is the whole point of lazy binding: the GOT slot that started by pointing at `push $i` now points at the resolved symbol, so the stub's leading `jmp *GOT.plt[3+i]` becomes a direct one-instruction dispatch on every subsequent call. The cost is paid exactly once per import actually called; imports never called are never resolved.

### `_init` and the `__gmon_start__` weak read

`_init` (`DT_INIT`, `0x3c000`) is the only place the CRT touches the *non-PLT* GOT. It is pure `crti.o`:

```asm
; _init  0x3c000  (crti.o) — DT_INIT
endbr64
sub  $0x8,%rsp
mov  0xbcaed1(%rip),%rax     ; __gmon_start__ via .got @0xc06ee0  (GLOB_DAT, WEAK UND -> NULL)
test %rax,%rax
je   0x3c016
call *%rax                   ; gprof hook — never taken in a non-profiled run
add  $0x8,%rsp
ret
```

`__gmon_start__` is a `WEAK UND` `NOTYPE` symbol bound through an `R_X86_64_GLOB_DAT` reloc at `.got` `0xc06ee0` — **not** a PLT `JUMP_SLOT`. It resolves to NULL in any build without gprof instrumentation, so the `test`/`je` skips the call and `_init`'s only observable effect is the stack adjustment.

> **CORRECTION (CRT-PLT vs L-CRT-01 §E) —** the seed catalog lists `__gmon_start__` at the IDA placeholder VA `0xcba770`. That address is IDA's synthetic slot for an *undefined* symbol, not where the runtime reads it. `_init` reads `__gmon_start__` from its real `.got` entry at **`0xc06ee0`** (`mov 0xbcaed1(%rip),%rax` at `0x3c008` → `0xc06ee0`), bound by an `R_X86_64_GLOB_DAT` reloc, confirmed by `readelf -rW | grep gmon`. Cite `0xc06ee0` for the GOT slot; `0xcba770` is a disassembler artifact.

---

## Lazy-Bind and RELRO Quirks

The PLT is classic lazy, but two structural facts will bite a reimplementer who assumes a modern hardened default.

> **QUIRK — this is a *lazy* PLT, not `BIND_NOW`.** `readelf -dW` shows `PLTREL RELA` and a `JMPREL` table but **no `DT_BIND_NOW`** and **no `DF_1_NOW`** in `DT_FLAGS_1`. So `.got.plt` is *not* made read-only after relocation — slots are written lazily on first call throughout the process lifetime. A reimplementation built with `-Wl,-z,now -Wl,-z,relro` (the common hardened default) produces a **different** relocation profile: all 441 `JUMP_SLOT`s applied eagerly at load, `.got.plt` folded into `.data.rel.ro` and `mprotect`ed read-only. That binary is functionally equivalent but its `.plt`/`.got.plt`/RELRO layout will not byte-match libnrt. The lazy choice here means a profiler attaching mid-run sees partially-resolved `.got.plt` slots (some pointing at stubs, some at targets) — normal, not corruption.

> **GOTCHA — RELRO covers `.got` but `.got.plt` stays writable.** The `GNU_RELRO` segment makes `.data.rel.ro`, `.dynamic`, and `.got` (`0xc057c0`, the 250 `GLOB_DAT` + the `__gmon_start__` slot) read-only after the loader applies their relocations — that is why `__gmon_start__` @`0xc06ee0` is safe to read in `_init`. But `.got.plt` (`0xc06fe8`) is deliberately *outside* RELRO precisely because lazy binding must keep writing to it. So the lazy-bound import slots are a writable region for the process lifetime, while the non-PLT GOT is locked. A reimplementer mirroring the security posture must reproduce that split (lazy PLT GOT writable, data GOT relro), or switch to full `BIND_NOW` and accept the layout divergence above. There is **no** `.plt.sec`/CET-IBT second PLT here, so a port targeting Intel CET would add `endbr64` landing pads the original PLT lacks.

---

## Related Components

| Component | Relationship |
|---|---|
| `.init_array` / `_GLOBAL__sub_I_*` | the 77 constructors framed by `_init`; owned by [Static-Init](static-init.md), not re-derived here |
| `__cxa_finalize` / glibc atexit | the teardown engine the `__cxa_atexit@plt` registrations feed; `.fini_array[0]` = `__do_global_dtors_aux` @`0x7af90` |
| `libncfw.so` | `dlopen`'d by `encd_libncfw_init` (`0x251cc0`) via `dlopen@plt`/`dlsym@plt` — the firmware image |
| `libnrtucode_extisa.so` | `dlopen`'d by `ucode_init_module` (`0x225940`) — the ExtISA microcode image |
| `_dl_runtime_resolve` / `ld-linux` | the resolver `PLT0` jumps to via `GOT.plt[2]`; one of the 9 `DT_NEEDED` |

## Cross-References

- [Static-Init Pipeline](static-init.md) — the `.init_array` 77-ctor table, `_GLOBAL__sub_I_*` thunks, `frame_dummy`/`register_tm_clones`, and the `__cxa_finalize` teardown this page frames but does not duplicate
- [ELF Anatomy](elf-anatomy.md) — the section/segment table (`.plt`/`.rela.plt`/`.got.plt` geometry), the `DT_NEEDED` floor, and the full relocation histogram whose 441 `JUMP_SLOT` count this page re-derives
- [Vendored-Library SBOM](vendored-sbom.md) — why protobuf/Abseil/simdjson/Rust have **no** PLT stubs (statically vendored), which makes the 227/150/42/17/2 import histogram the signature of a vendored monolith
- [Overview and Heavy-Frame Census](overview.md) — the first-party/vendored byte split and the `ucode_init_module`/`encd_libncfw_init` dlopen edges this page anchors
- [Runtime Core Overview](../runtime/overview.md) — the `tdrv`/`ndl.c` device layer behind the `ioctl`/`mmap` kernel-boundary imports, and the `pthread`/`kmgr` worker pools behind the threading imports
