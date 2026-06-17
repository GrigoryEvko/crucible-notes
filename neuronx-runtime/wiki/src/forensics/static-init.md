# Static-Init Pipeline

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB **DYN** x86-64, NOT stripped, DWARF v4.
>
> **Part II — Binary Anatomy & Forensics** / DEEP reference · **Evidence grade:** the constructor table below is decoded byte-for-byte from `.init_array` (`objdump -s -j .init_array`, then each 8-byte little-endian pointer symbolized against `nm -n`) and corroborated by `readelf -SW`/`readelf -dW` and the IDA decompiled sidecars under `ida/**/decompiled/`. · [back to forensics index](overview.md)

## Abstract

`libnrt.so` is a `DYN` object with **no `_start` and no `main`** — its entry point is `0x0`. Everything that must be standing before the first `nrt_*` API call is built by the **load-time static-initialization pipeline**: the dynamic loader (`ld.so`) calls `DT_INIT` (`_init`), then walks `DT_INIT_ARRAY` — a flat array of **77** function pointers — in array order. Those 77 constructors register the protobuf descriptor pools that back the NTFF/NEFF schemas, default-construct the `nrt_config` singleton and arm its destructor, zero the per-engine execution-unit state arrays, select the simdjson µarch kernel, probe whether stderr/stdout are TTYs, and bring up the abseil `status`/`cord` and libstdc++ iostream globals. By the time `ld.so` returns from the array, the C++/Rust "global control plane" of the runtime exists; the first-party device/arch state (`kaena_khal`, `tdrv_arch_ops`, the vcore tables) is **not** built here — it is installed lazily during `nrt_init`.

The shape is a textbook GCC/Clang static-init surface with two Neuron-specific twists. First, the array is **priority-stratified**: GCC sorts `__attribute__((init_priority))` constructors ahead of default-priority ones, so the array's first nine slots are the protobuf descriptor pre-registration (priorities 90/101/102) plus the libstdc++ `ios_base::Init` and the Rust `ARGV_INIT_ARRAY` hook — these run *before* any first-party KaenaRuntime TU. This ordering is not cosmetic: it is the runtime's defense against the **static-initialization-order fiasco**, since the first-party `*.pb.cc` message types depend on protobuf's descriptor-pool and `fixed_address_empty_string` already being live. Second, two of the 77 slots are not C++ TU thunks at all — they are a Rust `init_wrapper` (slot 1) that captures `argv` into the statically-linked Rust runtime, and `frame_dummy` (slot 9), the GCC `crtbegin.o` glue that arms the transactional-memory clone tables.

This page is organized as: the **entry-point call-tree** into the array; the **constructor table** (every identifiable slot pinned to its address and the global it builds), with the count derived from the section size rather than assumed; the **`.init_array` walk** and one representative constructor as annotated pseudocode; the **CRT glue** (`crti`/`crtn`/`_init`/`_fini`) that frames the array; and the **teardown** path (`.fini_array` → `__cxa_finalize`). The destructor side is the mirror image: GCC registers each C++ object's `~T` through `__cxa_atexit(&~T, &obj, &__dso_handle)` at construction time, and unload runs them in reverse via `__do_global_dtors_aux` → `__cxa_finalize(&__dso_handle)`.

For reimplementation, the contract is:

- **The array geometry** — `.init_array` is `0x268` bytes of 8-byte pointers = **77 constructors**, run in array order by the loader; `.fini_array` is `0x10` = 2 destructors, run in reverse. There is **no `__libc_csu_init`** (that is the executable's CRT; a shared object's array is walked by `ld.so` directly).
- **The priority stratification** — the first 9 slots are `init_priority`-tagged (90/101/102) and must run before the 68 default-priority slots; a reimplementation that loses the ordering risks a use-before-init of the protobuf descriptor pool.
- **What each major constructor builds** — protobuf descriptor/default-instance registration, the `nrt_config_0` singleton's container init + `__cxa_atexit`, the `tensoropxu`/barrier-state zeroing, the ndebug stream-lock array, the nlog TTY probe — pinned per slot.
- **The destructor model** — every C++ global registers `~T` via `__cxa_atexit` at construction; `__cxa_finalize(&__dso_handle)` at unload runs them LIFO. The two `.fini_array` slots frame that finalize.

| | |
|---|---|
| **Entry into array** | `ld.so` → `DT_INIT_ARRAY` @`0xbf2b08`, `DT_INIT_ARRAYSZ` `616` |
| **Constructor count** | **77** (`0x268 / 8`; verified — see [§ count derivation](#constructor-count--derived-not-assumed)) |
| **`DT_INIT` / `_init`** | `0x3c000` (`crti.o`; `__gmon_start__` weak-check only) |
| **`DT_FINI` / `_fini`** | `0x7ce8e8` (`crtn.o`; empty body) |
| **`.fini_array`** | `0xbf2d70`, `0x10` = 2 dtors (`__do_global_dtors_aux` @`0x7af90`, `ndebug_stream_module_close` @`0x744d0`) |
| **First-party singleton built here** | `nrt_config_0` @`0xc5c480` (slot 11, `_GLOBAL__sub_I_nrt_config.cpp` @`0x74920`) |
| **Destructor registration** | `__cxa_atexit(&~T, &obj, &__dso_handle)`; `__dso_handle` @`0xbf2d80` |
| **Composition** | 73 `_GLOBAL__sub_I_*` TU thunks + 4 non-`_GLOBAL` slots (Rust ARGV, `frame_dummy`, `nlog_constructor`, `ndebug_stream_module_init`) |

---

## Constructor count — derived, not assumed

The "77 constructors" figure is **verified from the section geometry**, not carried over from a stub. Two independent loader-authoritative sources agree, and the array was fully decoded:

```bash
# 1. Section size: 0x268 bytes of 8-byte pointers
readelf -SW libnrt.so.2.31.24.0 | grep init_array
#  [24] .init_array  INIT_ARRAY  ...bf2b08 bf2b08 000268 08  WA ...
python3 -c 'print(0x268 // 8)'        # -> 77

# 2. The loader's own size tag agrees
readelf -dW libnrt.so.2.31.24.0 | grep INIT_ARRAYSZ
#  (INIT_ARRAYSZ)  616 (bytes)         # 616 / 8 = 77
```

Decoding the 77 pointers (`objdump -s -j .init_array` → little-endian → `nm -n`) resolves **every** slot — none is null, none is `<no-sym>`. The composition is **73 `_GLOBAL__sub_I_*` per-TU thunks + 4 non-`_GLOBAL` entries** (the Rust `ARGV_INIT_ARRAY::init_wrapper`, `frame_dummy`, `nlog_constructor`, `ndebug_stream_module_init`).

> **CORRECTION (STATIC-INIT, "77" reinstated against doubt) —** an audit flagged "77 ctors" as a possibly-unverified pre-sweep figure. It is **correct**: `.init_array` size `0x268` (= 616 B) ÷ 8 = **77**, confirmed independently by `DT_INIT_ARRAYSZ = 616` and by a full per-slot decode. The figure that does *not* equal 77 is the raw `nm | grep _GLOBAL__sub_I_` count: that returns **75**, which is misleading in *both* directions. It is too high by 2 (it counts `_GLOBAL__sub_I_nrt_async.cpp.cold` @`0x3dbd8` and `_GLOBAL__sub_I_nrt_inspect_config.cpp.cold` @`0x41833` — exception-cleanup `.cold` fragments that are **not** in `.init_array`), and too low relative to 77 by the 4 non-`_GLOBAL` slots above. The arithmetic that reconciles: `75 − 2 .cold = 73` in-array `_GLOBAL` thunks; `73 + 4` non-`_GLOBAL` = **77** array slots. Always ground the constructor count on `.init_array` size, never on a symbol-name grep.

---

## Entry Point

A shared object has no `__libc_start_main` / `__libc_csu_init`. The loader reads the dynamic array directly: it runs `DT_INIT` once, then iterates `DT_INIT_ARRAY` for `DT_INIT_ARRAYSZ / 8` entries in **forward** array order (GCC has already sorted the array by `init_priority`, so "array order" *is* priority order).

```text
ld.so  dl_init / call_init(libnrt.so)
  ├─ DT_INIT        → _init           0x3c000   (crti.o)
  │     └─ if (__gmon_start__ != 0) call __gmon_start__   # GOT @0xc06ee0, weak — normally NULL
  │
  └─ DT_INIT_ARRAY  → 0xbf2b08 .. 0xbf2d70   (77 × 8-byte fn-ptrs, forward order)
        [ 0] ios_init(prio 90)        0x7a3b0   libstdc++ std::ios_base::Init
        [ 1] Rust ARGV_INIT_ARRAY     0x527e80  std::sys::args::unix capture argv
        [ 2..8] protobuf prio 101/102 0x77c10…  descriptor / default-instance pre-reg
        [ 9] frame_dummy              0x7afd0   → register_tm_clones (crtbegin.o)
        [10..29] first-party NRT TUs  0x74510…  nrt_async/config/exec/.../enc/nccl,
        │                                       nlog, ndebug, kelf2kbin, simdjson
        [30..76] protobuf + abseil     0x796f0…  arena…wire_format…status…cord…charconv
```

> **NOTE — `_init` does almost nothing here.** `_init` (`0x3c000`) is pure glibc `crti.o`: `endbr64; sub $8,%rsp; test __gmon_start__; je; call *__gmon_start__; add $8,%rsp; ret`. `__gmon_start__` is a `WEAK UND` gprof hook that resolves to `NULL` in any non-profiled run, so `_init`'s only observable effect is the stack adjustment. All real construction is in the array. The legacy `_init`/`_fini` mechanism survives only because GCC still emits `crti.o`/`crtn.o`; the array is the modern path.

---

## The Constructor Table

Every one of the 77 slots resolves to a named function. The table below pins the **identifiable / structurally-significant** slots — the boundaries between priority bands, the first-party NRT thunks, and the non-`_GLOBAL` glue — and summarizes the long protobuf/abseil runtime tail by its *range* rather than dumping 47 near-identical rows. Slot index is the array position (= execution order); address is the decoded pointer; "Constructs" is what the body does at load (from the decompiled sidecar where cited).

| Slot | Thunk @addr | TU / origin | Constructs at load | Conf |
|---|---|---|---|---|
| 0 | `0x7a3b0` `_GLOBAL__sub_I.00090_ios_init.cc` | libstdc++ (prio 90) | `std::ios_base::Init` → `cin/cout/cerr` live | CERTAIN |
| 1 | `0x527e80` `ARGV_INIT_ARRAY::init_wrapper` | Rust std (`sys::args::unix`) | captures `argc/argv` into the Rust runtime | HIGH |
| 2 | `0x77c10` `_GLOBAL__sub_I.00101_ntff.pb.cc` | protobuf (prio 101) | `ntff::_*_default_instance_` objects + `__cxa_atexit` dtors | CERTAIN |
| 3 | `0x78920` `.00101_neuron_trace.pb.cc` | protobuf (prio 101) | `neuron_trace` default instances | HIGH |
| 4 | `0x79780` `.00101_descriptor.pb.cc` | protobuf (prio 101) | `descriptor.proto` default instances | HIGH |
| 5 | `0x79cb0` `.00101_generated_message_util.cc` | protobuf (prio 101) | message-util statics | HIGH |
| 6 | `0x7a0c0` `.00101_cpp_features.pb.cc` | protobuf (prio 101) | feature-set default instances | HIGH |
| 7 | `0x788e0` `.00102_ntff.pb.cc` | protobuf (prio 102) | ntff second-phase statics | HIGH |
| 8 | `0x78d40` `.00102_neuron_trace.pb.cc` | protobuf (prio 102) | neuron_trace second-phase statics | HIGH |
| 9 | `0x7afd0` `frame_dummy` | GCC `crtbegin.o` | → `register_tm_clones` (TM clone tables) | CERTAIN |
| 10 | `0x74510` `_GLOBAL__sub_I_nrt_async.cpp` | KaenaRuntime | zeroes `tensoropxu`/`barrier_state` (127-entry loop) | HIGH |
| **11** | **`0x74920` `_GLOBAL__sub_I_nrt_config.cpp`** | **KaenaRuntime** | **`nrt_config_0` @`0xc5c480` container init + `__cxa_atexit(~nrt_config)`** | **CERTAIN** |
| 12 | `0x749a0` `_GLOBAL__sub_I_nrt_exec.cpp` | KaenaRuntime | exec-path statics | HIGH |
| 13 | `0x74a40` `_GLOBAL__sub_I_nrt_inspect.cpp` | KaenaRuntime | inspect statics | HIGH |
| 14 | `0x74b10` `_GLOBAL__sub_I_nrt_inspect_config.cpp` | KaenaRuntime | inspect-config statics | HIGH |
| 15 | `0x74c90` `_GLOBAL__sub_I_nrt_profile.cpp` | KaenaRuntime | profiler `session_ctx` statics | HIGH |
| 16 | `0x74ce0` `_GLOBAL__sub_I_dlr.cpp` | KaenaRuntime/kmgr | `dlr_model_set` (`std::set`) | HIGH |
| 17 | `0x74dc0` `_GLOBAL__sub_I_kmgr_async_exec.cc` | KaenaRuntime/kmgr | async-exec worker statics | HIGH |
| 18 | `0x74e60` `_GLOBAL__sub_I_tpb_xu.cc` | KaenaRuntime/kmgr | `_tpb_xus` @`0xc64a40` (`0x24000` B) | MED |
| 19 | `0x74f00` `_GLOBAL__sub_I_xu_worker.cc` | KaenaRuntime/kmgr | `workers` @`0xc88ae0` | MED |
| 20 | `0x74fa0` `_GLOBAL__sub_I_kv_store.cc` | KaenaRuntime | kv-store mutexes | HIGH |
| 21 | `0x74ff0` `_GLOBAL__sub_I_enc.cc` | KaenaRuntime/enc | `g_shared_proxy_queues` `std::map` + `__cxa_atexit` | HIGH |
| 22 | `0x75040` `_GLOBAL__sub_I_neuron_nccl.cc` | KaenaRuntime/enc | `nccl_init_error` ostringstream + `__cxa_atexit` | HIGH |
| 23 | `0x77b50` `_GLOBAL__sub_I_replica_groups.cc` | KaenaRuntime/enc | replica-group statics | HIGH |
| 24 | `0x77b60` `nlog_constructor` | KaenaRuntime/nlog | `nlog_stderr_is_tty` / `nlog_stdout_is_tty` via `isatty` | CERTAIN |
| 25 | `0x77bb0` `ndebug_stream_module_init` | KaenaRuntime/ndebug | `pthread_mutex_init` over `stream_locks[]` array | CERTAIN |
| 26 | `0x788f0` `_GLOBAL__sub_I_ntff.pb.cc` | protobuf/ntff | `std::ios_base::Init` (KaenaProfilerFormat TU) | HIGH |
| 27 | `0x78d50` `_GLOBAL__sub_I_neuron_trace.pb.cc` | protobuf/trace | neuron_trace final statics | HIGH |
| 28 | `0x79210` `_GLOBAL__sub_I_kelf2kbin.cpp` | KaenaRuntime/kelf | kelf2kbin globals | HIGH |
| 29 | `0x792b0` `_GLOBAL__sub_I_simdjson.cpp` | simdjson 0.9.0 | µarch implementation selection | HIGH |
| 30–62 | `0x796f0`–`0x7a090` (contiguous) | protobuf 5.26.1 runtime | descriptor/arena/message/wire_format/text_format TUs | HIGH |
| 63–67 | `0x7a110`–`0x7a1d0` | protobuf + abseil | cpp_features, extension_set_heavy, zero_copy, statusor | HIGH |
| 68–76 | `0x7a200`–`0x7a380` | abseil LTS 20230802 | status, cord (+rep_btree/ring/navigator), charconv | HIGH |

Family totals across the 77 slots: **protobuf 44, first-party KaenaRuntime ~17, abseil 10, simdjson 1, Rust-std 1, libstdc++-ios 1, `frame_dummy` 1.** The seven priority-tagged protobuf slots (2–8) plus the long default-priority protobuf tail (26–67) dominate the array — the runtime's descriptor pool is the single largest load-time cost.

> **NOTE — the two-priority protobuf scheme.** protobuf emits *two* constructors per `.proto`: a high-priority one (`.00101_`/`.00102_`) and a default-priority one. The priority pair runs in slots 2–8 and builds the `_*_default_instance_` objects — e.g. `ntff::_version_info_default_instance_[0] = off_BF70F0` (the `DefaultTypeInternal` vtable) and `[2] = &fixed_address_empty_string` — then arms each object's `~DefaultTypeInternal` via `__cxa_atexit`. The default-priority `_GLOBAL__sub_I_*.pb.cc` thunks later in the array perform the descriptor-table registration into the global pool. Splitting registration across two priority bands is exactly how protobuf avoids the order fiasco: the default instances and the empty-string sentinel must exist before any descriptor references them.

---

## The `.init_array` Walk

The loader's iteration is trivial; the interesting logic is per-constructor. The walk, modeling glibc `call_init`:

```c
// ld.so call_init(libnrt.so) — DT_INIT then DT_INIT_ARRAY
function call_init(map):
    if map->l_info[DT_INIT]:                 // 0x3c000
        (*DT_INIT)()                          // _init: __gmon_start__ weak-check, no-op
    array = map->l_info[DT_INIT_ARRAY]->ptr   // 0xbf2b08
    n     = map->l_info[DT_INIT_ARRAYSZ] / 8  // 616 / 8 == 77
    for i in 0 .. n-1:                        // FORWARD order (already priority-sorted)
        (*array[i])()                         // each ctor builds its TU's globals
```

### A representative constructor — the `nrt_config` singleton

Slot 11, `_GLOBAL__sub_I_nrt_config.cpp` @`0x74920`, is the most consequential first-party constructor: it brings the runtime-config singleton `nrt_config_0` (@`0xc5c480`, 656 B) into a valid empty state and registers its destructor. It does **not** read any environment variables — field population is deferred to `nrt_config_parse_init_config` at `nrt_init` time. Its entire job is to make the three STL containers inside the struct well-formed before anything can touch them:

```c
// _GLOBAL__sub_I_nrt_config.cpp  @0x74920   (decompiled sidecar)
function GLOBAL__sub_I_nrt_config_cpp():
    // nrt_config_0 @ .bss 0xc5c480 — zero-init by .bss, but the two
    // std::vector and one std::map need their internal pointers wired.
    nrt_config_0.visible_virtual_cores._M_start          = 0     // empty vector<int>
    nrt_config_0.visible_virtual_cores._M_end_of_storage = 0
    nrt_config_0.dbg_cc_dma_packet_size._M_start          = 0    // empty vector<int>
    nrt_config_0.dbg_cc_dma_packet_size._M_end_of_storage = 0

    // profile_buf_sizes: std::map<notification_type,uint> — wire the Rb-tree header
    hdr = &nrt_config_0.profile_buf_sizes._M_t._M_impl._M_header
    hdr._M_color  = _S_red
    hdr._M_parent = 0
    hdr._M_left   = hdr                       // empty-tree self-loop
    hdr._M_right  = hdr
    nrt_config_0.profile_buf_sizes._M_node_count = 0

    // Arm the destructor: LIFO-runs at unload via __cxa_finalize
    __cxa_atexit(&nrt_config::~nrt_config, &nrt_config_0, &__dso_handle)   // __dso_handle @0xbf2d80
```

The `__cxa_atexit` call is the load-time half of the teardown contract: it pushes `(~nrt_config, &nrt_config_0, &__dso_handle)` onto glibc's exit list so the destructor runs at `dlclose`/exit. Two other first-party constructors do the same — `_GLOBAL__sub_I_enc.cc` (slot 21, for `g_shared_proxy_queues`) and `_GLOBAL__sub_I_neuron_nccl.cc` (slot 22, for `nccl_init_error`) — alongside every protobuf default-instance.

Two more representative bodies, for contrast in what a constructor *does*:

```c
// nlog_constructor  @0x77b60  (slot 24) — pure runtime probe, no allocation
function nlog_constructor():
    nlog_stderr_is_tty = (isatty(fileno(stderr)) != 0)
    nlog_stdout_is_tty = (isatty(fileno(stdout)) != 0)

// ndebug_stream_module_init  @0x77bb0  (slot 25) — array of locks
function ndebug_stream_module_init():
    for lock in stream_locks[] .. &streams:           // walks lock array up to streams[]
        if pthread_mutex_init(lock, NULL) != 0:
            __assert_fail("pret == 0",
                          "/opt/workspace/KaenaRuntime/ndebug/ndebug_stream.c", 0x24,
                          "ndebug_stream_module_init")
```

> **GOTCHA — the static-init-order fiasco surface, and where it would bite.** GCC guarantees ordering only *within* a translation unit and, across TUs, only by `init_priority`. The runtime relies on this in two places a reimplementation must preserve. (1) **protobuf-before-first-party:** the priority-101/102 protobuf constructors (slots 2–8) build `fixed_address_empty_string` and the `_default_instance_` objects; the first-party `*.pb.cc` and any code constructing an `ntff`/`neuron_trace` message at load would dereference a half-built descriptor pool if it ran first. The `init_priority` band is what forces protobuf ahead of the 68 default-priority slots. (2) **`ios_base::Init`-before-stream-use:** slot 0 (priority 90) constructs the iostream globals; `nlog_constructor` (slot 24) and any constructor that logs must not run before it. A naive reimplementation that emits constructors in source order — or that lets the linker reorder the array — reintroduces the fiasco. The defense is entirely in the `init_priority` attribute and the linker's stable sort of `.init_array`; there is no runtime guard. Equally, note what is **deliberately not** in the array: `kaena_khal`, `tdrv_arch_ops`, `ngc`, and the vcore tables are installed lazily during `nrt_init`, precisely so they are not subject to load-order constraints against the device being present.

---

## CRT Glue — crti / crtn / `_init` / `_fini`

The array is framed by the legacy GCC/glibc CRT functions. They are compiler/linker boilerplate, not hand-written, but a reimplementation linking with `crt*.o` inherits them and must account for their effect on the `.fini_array`.

### `_init` and `_fini`

```asm
; _init  0x3c000  (crti.o) — DT_INIT
endbr64
sub  $0x8,%rsp
mov  0xbcaed1(%rip),%rax     ; __gmon_start__ via GOT @0xc06ee0  (WEAK UND -> NULL)
test %rax,%rax
je   .skip
call *%rax                   ; gprof hook, never taken in normal runs
.skip:
add  $0x8,%rsp
ret

; _fini  0x7ce8e8  (crtn.o) — DT_FINI
endbr64
sub  $0x8,%rsp
add  $0x8,%rsp               ; empty body
ret
```

### `frame_dummy` and the TM-clone glue

`frame_dummy` (slot 9, `0x7afd0`) is `crtbegin.o`'s `.init_array` entry. It tail-jumps `register_tm_clones` (`0x7af50`), which probes the weak `_ITM_registerTMCloneTable` (@`0xc06f18`); since `libnrt.so` links no transactional-memory clones, the table pointer is `NULL` and the call is a no-op. Its mirror `deregister_tm_clones` (`0x7af20`) is called from `__do_global_dtors_aux` at unload.

### Teardown — `.fini_array` and `__cxa_finalize`

`.fini_array` @`0xbf2d70` holds **2** pointers, run by the loader in **reverse** at `dlclose`/exit:

```text
.fini_array (run in reverse):
  [1] 0x744d0  ndebug_stream_module_close   → runs FIRST  (pthread_mutex_destroy over stream_locks[])
  [0] 0x7af90  __do_global_dtors_aux        → runs SECOND
        └─ if (!completed.0):                          # guard @ .bss 0xc37c40
               if (&__cxa_finalize != 0):
                   __cxa_finalize(&__dso_handle)        # __dso_handle @0xbf2d80
                   //   ^ runs ALL __cxa_atexit dtors registered at construction,
                   //     LIFO: ~nrt_config, ~g_shared_proxy_queues, ~nccl_init_error,
                   //           every protobuf ~DefaultTypeInternal, ~ios_base::Init
               deregister_tm_clones()
               completed.0 = 1
  DT_FINI → _fini (0x7ce8e8): empty
```

> **QUIRK — the destructors are NOT in `.fini_array`; they are in glibc's atexit list.** Only 2 functions sit in `.fini_array`, yet dozens of C++ destructors run at unload. The mechanism is `__cxa_atexit`: each constructor that builds a non-trivial C++ global registered `(&~T, &obj, &__dso_handle)` at load time (see the `nrt_config` body above). `__do_global_dtors_aux` — itself `.fini_array[0]` — calls `__cxa_finalize(&__dso_handle)`, which drains exactly the atexit entries tagged with *this* DSO's handle, in LIFO order. So the construction order in `.init_array` (forward) determines the destruction order through the atexit stack (reverse), and the `&__dso_handle` argument is what scopes the finalize to `libnrt.so` so a `dlclose` of this library does not run another DSO's destructors. The `completed.0` guard (@`0xc37c40`, the first byte of `.bss`) makes the teardown idempotent against a double `dlclose`.

---

## Related Components

| Component | Relationship |
|---|---|
| `nrt_config_0` @`0xc5c480` | constructed empty by slot 11; fields filled at `nrt_init` by `nrt_config_parse_init_config` |
| `kaena_khal` / `tdrv_arch_ops` | **not** built here — installed lazily at device bring-up (`al_hal_tpb_set_arch_type`, `tdrv_arch_ops_init`) |
| protobuf descriptor pool | registered by slots 2–8 (priority) + 26–67 (runtime); backs the NTFF/NEFF schemas |
| `__cxa_finalize` / glibc atexit | the teardown engine the constructors feed via `__cxa_atexit(&~T,&obj,&__dso_handle)` |

## Cross-References

- [Globals and Singletons Atlas](globals-atlas.md) — the `.bss`/`.data` targets these constructors build (`nrt_config_0`, `tensoropxu`, `g_shared_proxy_queues`, `stream_locks`) and which globals are lazily installed instead
- [CRT / PLT / Loader Surface](crt-plt.md) — the `_init`/`_fini`/PLT0 mechanics and the 441 lazy-bind stubs that frame this array; `frame_dummy`/`register_tm_clones` detail
- [Vendored-Library SBOM](vendored-sbom.md) — pins protobuf 5.26.1 / abseil LTS 20230802 / simdjson 0.9.0 whose constructors fill slots 2–8 and 26–76; `VerifyVersion` @`0x6fb690` fatals on skew
- [Configuration: nrt_config and nrt_global_config](../runtime/config-structs.md) — the full `nrt_config` (656 B) layout the slot-11 constructor empty-initializes, and the heap `ngc` it does *not* build
