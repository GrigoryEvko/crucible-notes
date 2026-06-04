# ELF Entry & init_proc

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

This page documents the **ELF load-time machinery** that the dynamic linker
(`ld-linux-x86-64.so.2`) runs when a framework calls `dlopen("libtpu.so")`: the
dynamic-section init/fini tags, the `_init`/`_fini` legacy entry stubs reached
through `DT_INIT`/`DT_FINI`, and the C-runtime (CRT) trampoline that bridges the
linker's array-walk to libtpu's static-constructor storm. It is the
**link-editor / loader contract** — the set of `Elf64_Dyn` tags and the
fixed-shape stubs every conforming loader must honor, established at link time
and consumed by the loader. It does **not** reproduce the per-constructor
iteration or the `_GLOBAL__sub_I_*` set those arrays point at — that is owned by
[do-init-do-fini.md](do-init-do-fini.md). It documents the *plumbing*: which
tags exist, what each stub does, and in what order the loader fires them.

The familiar reference frame is the standard ELF init/fini ABI that any
`-shared` object built with GCC's `crtbegin.o`/`crtend.o` produces:
`DT_INIT` → `_init`, `DT_FINI` → `_fini`, plus `DT_PREINIT_ARRAY` /
`DT_INIT_ARRAY` / `DT_FINI_ARRAY` pointer arrays the loader walks. The
counter-intuitive finding is that libtpu is built with the **LLVM/clang CRT**,
not GCC's: the classic `crtbegin.o` boilerplate — `frame_dummy`,
`register_tm_clones`, `deregister_tm_clones`, the `__do_global_ctors_aux`
trampoline that *iterates* the legacy `.ctors` list — is **absent**. There is no
`.ctors`/`.dtors`, the `.tm_clone_table` is zero-length, and the only CRT
"trampoline" that survives is a pair of 17-/39-byte array-entry stubs
(`__do_init` @ `0xe63c000`, `__do_fini` @ `0xe63c020`). `_init`/`_fini`
themselves degenerate to vestigial glibc stubs. The whole job of "iterate the
constructors" is delegated to the **loader** walking `DT_INIT_ARRAY` directly —
there is no in-binary iterator to reverse.

For reimplementation, the contract is:

- **The eight `Elf64_Dyn` tags** that wire init/fini, with their values and the loader-mandated firing order (PREINIT → INIT → … → FINI).
- **The two legacy stubs** `_init`/`_fini` (`DT_INIT`/`DT_FINI`): exact bytes, and *why* they carry no real work.
- **The CRT trampoline shape** in this binary: the LLVM `__do_init`/`__do_fini` array-entry stubs and the set-once byte guards behind them — and the GCC boilerplate that is *missing*, so a reimplementer does not go looking for `frame_dummy`.
- **The GOT/PLT bring-up** the loader performs before any of the above, since the init code itself runs through relocated slots.

| | |
|---|---|
| **`DT_INIT` → `_init`** | `0xe635524` (`.init`, 23 B) — `__gmon_start__` check-and-call stub |
| **`DT_FINI` → `_fini`** | `0xe63553c` (`.fini`, 9 B) — empty `sub rsp,8 / add rsp,8 / ret` |
| **`DT_PREINIT_ARRAY`** | `0x22048b30`, `DT_PREINIT_ARRAYSZ` 16 B (2 entries) |
| **`DT_INIT_ARRAY`** | `0x215f26f0`, `DT_INIT_ARRAYSZ` 23200 B (2900 entries) |
| **`DT_FINI_ARRAY`** | `0x215f8190`, `DT_FINI_ARRAYSZ` 16 B (2 entries) |
| **CRT init stub** | `__do_init @ 0xe63c000` (17 B), guard `__do_init.__initialized` |
| **CRT fini stub** | `__do_fini @ 0xe63c020` (39 B), guard `__do_fini.__finalized` → `__cxa_finalize(__dso_handle)` |
| **GOT / PLT** | `.got @ 0x22048d50` (50256 B), `.got.plt @ 0x224c2980` (3808 B), `.plt @ 0x213f0830` (7584 B) |
| **CRT flavor** | LLVM/clang — **no** `frame_dummy` / `register_tm_clones` / `__do_global_ctors_aux`; no `.tm_clone_table` section emitted |
| **Confidence** | CONFIRMED (byte-anchored vs `readelf -d`, segment table, and disassembly) unless a row says otherwise |

---

## The Loader Contract — `Elf64_Dyn` Init/Fini Tags

### Purpose

Every action on this page is *driven by the loader reading the dynamic section*.
The link editor records the entry points and array bounds as `Elf64_Dyn` tags;
the loader (after relocation) honors them in a fixed order mandated by the
System V x86-64 gABI. The whole "what runs at `dlopen`" question reduces to:
which of these eight tags are present, what they point at, and the order rule.

### Tag Inventory

Read directly from `readelf -d` against the binary; every value is byte-exact.

| Tag (d_tag) | Hex | Value | Meaning |
|---|---|---|---|
| `DT_INIT` | `0x0c` | `0xe635524` | Address of `_init` — run after `DT_INIT_ARRAY`? No: **before** the array, per gABI ordering. |
| `DT_FINI` | `0x0d` | `0xe63553c` | Address of `_fini` — run **after** `DT_FINI_ARRAY`. |
| `DT_INIT_ARRAY` | `0x19` | `0x215f26f0` | Base of the constructor pointer array (`.init_array`). |
| `DT_INIT_ARRAYSZ` | `0x1b` | `23200` | 2900 × 8 B — count = size / 8. |
| `DT_FINI_ARRAY` | `0x1a` | `0x215f8190` | Base of the destructor pointer array (`.fini_array`). |
| `DT_FINI_ARRAYSZ` | `0x1c` | `16` | 2 × 8 B — 2 entries. |
| `DT_PREINIT_ARRAY` | `0x20` | `0x22048b30` | Base of the pre-init pointer array — runs **before any** constructor. |
| `DT_PREINIT_ARRAYSZ` | `0x21` | `16` | 2 × 8 B — 2 entries. |

> **NOTE —** there is no `DT_INIT_ARRAYSZ` ambiguity to resolve: the loader derives the entry *count* purely as `ARRAYSZ / sizeof(void*)`. libtpu encodes no terminator sentinel in any array, so a reimplementer must rely on the size tags, not on a NULL/`-1` end marker.

### The gABI Firing Order

The loader fires these in a strict, standardized sequence. The diagram is the
canonical `dlopen` order for this object; each band names the tag the loader is
consuming.

```text
dlopen("libtpu.so")  →  ld-linux-x86-64.so.2
  │
  ├─ (1) map segments, then apply ALL relocations
  │       .got / .got.plt / the three init arrays are filled here
  │       (see "GOT / PLT Bring-Up" below)
  │
  ├─ (2) DT_PREINIT_ARRAY  @ 0x22048b30   [2 entries]   ── runs FIRST, before any ctor
  │        [0] cpu_feature_fail_fast   0x2110abc0   ── CPU ISA hard gate; SIGILL on miss
  │        [1] setup_dl_debug_hook     0x2114eec0
  │
  ├─ (3) DT_INIT          @ 0xe635524    (_init)        ── __gmon_start__ check-and-call stub
  │
  ├─ (4) DT_INIT_ARRAY    @ 0x215f26f0   [2900 entries] ── the static-constructor storm
  │        [0] __cpu_indicator_init  0x21211240   ── GCC ifunc CPU-feature resolver (slot 0)
  │        [1] Rust ARGV_INIT_ARRAY  0x20a0d2b0   ── std::sys::args::unix init_wrapper
  │        └─ … 2898 more: _GLOBAL__sub_I_* / _GLOBAL__I_* / __cxx_global_var_init,
  │              with __do_init (0xe63c000) among them — slot order owned by static-init.md
  ▼
  libtpu mapped & constructed; nothing TPU-specific has RUN yet
  ───────────────────────────── at unload / process exit ─────────────────────────────
  ├─ (5) DT_FINI_ARRAY    @ 0x215f8190   [2 entries]    ── reverse-order destructors
  │        [0] __do_fini                  0xe63c020     ── guarded __cxa_finalize(__dso_handle)
  │        [1] rand_thread_state_clear_all 0x2063df60   ── per-thread BoringSSL/RNG clear
  │
  └─ (6) DT_FINI          @ 0xe63553c    (_fini)        ── empty stub
```

> **GOTCHA —** `DT_PREINIT_ARRAY` runs *before* `DT_INIT` and `DT_INIT_ARRAY`, but the gABI only permits `DT_PREINIT_ARRAY` in an **executable**, not in a shared object loaded with `dlopen`. libtpu ships one anyway (`PREINIT_ARRAYSZ = 16`). On glibc's loader the preinit array of a `dlopen`ed `.so` is honored as the first thing after relocation; a stricter loader could skip it, which would silently disable the CPU-feature hard gate. A reimplementer targeting libtpu's exact behavior must run `DT_PREINIT_ARRAY` for the shared object, not assume the executable-only restriction. The 11-feature CPU gate decode lives on [module-init-plugin-discovery.md](module-init-plugin-discovery.md#21-the-cpu-feature-hard-gate-preinit_array0).

> **QUIRK —** the *file image* of all three arrays is zero. Every slot is an `R_X86_64_RELATIVE` relocation; the on-disk `.init_array` is 23200 bytes of zeros, and the loader writes each target VA during step (1). A static dump of the array section before relocation shows no targets at all — they exist only at runtime. This is why the array contents are documented from the relocation table, not from the section bytes.

---

## `_init` — The `DT_INIT` Stub (`init_proc @ 0xe635524`)

### Purpose

`DT_INIT` points at the function the loader calls once, after `DT_PREINIT_ARRAY`
and before `DT_INIT_ARRAY`. In the GCC toolchain this is `_init`, the head of
the `.init` section that `crti.o` opens and `crtn.o` closes, and historically it
chained into `__do_global_ctors_aux`. In libtpu it is the **vestigial glibc
profiling stub** and nothing more — all real constructor work moved to
`DT_INIT_ARRAY`.

### Disassembly

The entire `.init` section is 23 bytes (`0xe635524`–`0xe63553b`):

```asm
0xe635524: 48 83 ec 08         sub  rsp, 8
0xe635528: 48 8b 05 31 38 a1 13 mov  rax, [rip+0x13a13831]        ; __gmon_start__ GOT slot @ 0x22048d60
0xe63552f: 48 85 c0            test rax, rax
0xe635532: 74 02               jz   loc_E635536                  ; skip if unbound
0xe635534: ff d0               call rax                          ; __gmon_start__()
0xe635536: 48 83 c4 08         add  rsp, 8
0xe63553a: c3                  ret
```

### Algorithm

```c
function _init():                       // .init_proc @ 0xe635524 (DT_INIT)
    // Standard glibc gmon (gprof) bring-up hook.
    if (&__gmon_start__ != NULL)        // weak symbol via relocated GOT slot
        __gmon_start__();               // arms profiling if linked; else no-op
    return;
    // No __do_global_ctors_aux tail-call. The .ctors list does not exist
    // in this binary; constructor iteration is the loader's DT_INIT_ARRAY walk.
```

> **NOTE —** `__gmon_start__` is a **weak undefined import** (`readelf --dyn-syms`: `NOTYPE WEAK DEFAULT UND __gmon_start__`, value `0`); `_init` reads its GOT slot at `0x22048d60` (an `R_X86_64_GLOB_DAT` reloc). The slot is bound only if a profiling runtime is present, which it is not in this wheel. The `test rax,rax` guard makes the call a no-op in production. The 8-byte `sub`/`add` of `rsp` is pure stack-alignment boilerplate from `crti.o`'s `_init` prologue. A reimplementer can treat `_init` as "does nothing" without behavioral loss.

---

## `_fini` — The `DT_FINI` Stub (`term_proc @ 0xe63553c`)

### Purpose

`DT_FINI` is the symmetric teardown entry: the loader calls it last, after
`DT_FINI_ARRAY`. In GCC it chained into `__do_global_dtors_aux`. Here it is
**empty** — all destructor work is in `DT_FINI_ARRAY`.

### Disassembly

The whole `.fini` section is 9 bytes (`0xe63553c`–`0xe635545`):

```asm
0xe63553c: 48 83 ec 08         sub  rsp, 8
0xe635540: 48 83 c4 08         add  rsp, 8
0xe635544: c3                  ret
```

It pushes and pops the stack and returns — a no-op. No `__cxa_finalize`, no
destructor iterator. The actual finalize call is in the `DT_FINI_ARRAY` entry
`__do_fini`, documented below.

---

## The CRT Trampoline — `__do_init` / `__do_fini`

### Purpose

This is the part the page brief calls the "CRT boilerplate", and it is where
libtpu most sharply diverges from the GCC layout. In a GCC `.so`,
`crtbegin.o` contributes `frame_dummy` (registers exception-handling frames),
`register_tm_clones` / `deregister_tm_clones` (transactional-memory clone
tables), and a `__do_global_ctors_aux` trampoline that *loops* over the
`.ctors` list. libtpu, built with the **LLVM/clang CRT**, ships **none of
these**. The only CRT artifacts left are two minimal array-entry functions:

- **`__do_init` @ `0xe63c000`** — one of the `DT_INIT_ARRAY` entries (a `_GLOBAL__sub_I_*`-peer slot, *not* slot 0 — its constructor-ordering position is owned by [static-init.md](../forensics/static-init.md)), a set-once byte guard. It does **not** iterate anything.
- **`__do_fini` @ `0xe63c020`** — a `DT_FINI_ARRAY` entry that runs the guarded `__cxa_finalize(__dso_handle)` that GCC's `__do_global_dtors_aux` would normally run.

### Disassembly — `__do_init` (17 bytes)

```asm
0xe63c000: 80 3d 79 78 e8 13 00  cmp  cs:__do_init.__initialized, 0
0xe63c007: 75 07                 jnz  locret_E63C010          ; already done → return
0xe63c009: c6 05 70 78 e8 13 01  mov  cs:__do_init.__initialized, 1
0xe63c010: c3                    ret
```

### Disassembly — `__do_fini` (39 bytes)

```asm
0xe63c020: 80 3d 5a 78 e8 13 00     cmp  cs:__do_fini.__finalized, 0
0xe63c027: 75 1d                    jnz  locret_E63C046        ; already done → return
0xe63c029: c6 05 51 78 e8 13 01     mov  cs:__do_fini.__finalized, 1
0xe63c030: 48 83 3d 30 cd a0 13 00  cmp  cs:__cxa_finalize_ptr, 0  ; weak null-check
0xe63c038: 74 0c                    jz   locret_E63C046
0xe63c03a: 48 8b 3d 7f 91 c1 13     mov  rdi, cs:__dso_handle
0xe63c041: e9 fa 47 db 12           jmp  __cxa_finalize        ; tail-call
0xe63c046: c3                       ret
```

### Algorithm

```c
function __do_init():                   // 0xe63c000  (a DT_INIT_ARRAY entry)
    if (__do_init.__initialized)        // 1-byte guard in .bss
        return;                         // idempotent: re-entry is a fast no-op
    __do_init.__initialized = 1;
    // That is the entire body. No frame_dummy, no ctor loop, no atexit().
    // GCC's __do_global_ctors_aux would loop .ctors here; clang's CRT leaves
    // constructor iteration entirely to the loader's DT_INIT_ARRAY walk.

function __do_fini():                   // 0xe63c020  (a DT_FINI_ARRAY entry)
    if (__do_fini.__finalized)          // 1-byte guard in .bss
        return;
    __do_fini.__finalized = 1;
    if (&__cxa_finalize != NULL)        // weak — present here
        __cxa_finalize(__dso_handle);   // run libtpu's registered atexit/dtor list
```

> **QUIRK —** the constructor "trampoline" carries *no work*. `__do_init` only flips a byte. A reader expecting the GCC `__do_global_ctors_aux` loop — fetch `__CTOR_LIST__`, walk backward calling each function pointer until the `-1` sentinel — will not find it, because clang emits constructors as direct `DT_INIT_ARRAY` entries and lets the loader iterate. The "static-constructor storm" is 2900 loader-driven calls, not one in-binary loop. The per-entry detail (what the 2900 pointers are, the `__do_init` guard's role, the `_GLOBAL__sub_I_*` set) is on [do-init-do-fini.md](do-init-do-fini.md).

> **NOTE —** `__cxa_finalize_ptr` is checked against NULL before the tail-call because `__cxa_finalize` is a weak import; the slot is bound in this binary (libc provides it), so the call fires at unload. `__do_fini` is the *only* path that runs the registered destructor/`atexit` list — `_fini` is empty. A reimplementer who wires teardown through `DT_FINI` instead of `DT_FINI_ARRAY[0]` will silently skip every C++ destructor. The full `FINI_ARRAY` body and destructor ordering are on [do-init-do-fini.md](do-init-do-fini.md).

### What Is Missing (and Why It Matters)

A reimplementer porting from a GCC mental model must *not* go hunting for these,
because they do not exist in this binary:

| GCC `crtbegin.o` artifact | Status in libtpu | Consequence |
|---|---|---|
| `frame_dummy` | absent | EH-frame registration is handled by `.eh_frame_hdr` + the loader, not a ctor |
| `register_tm_clones` / `deregister_tm_clones` | absent | no `.tm_clone_table` section emitted; no TM clone fixup |
| `__do_global_ctors_aux` (ctor loop) | absent | constructors are direct `DT_INIT_ARRAY` entries; loader iterates |
| `__do_global_dtors_aux` (dtor loop) | replaced by `__do_fini` | `__cxa_finalize(__dso_handle)` only; no `.dtors` walk |
| `.ctors` / `.dtors` sections | absent | superseded by `.init_array` / `.fini_array` |

> **Note:** there is no global-constructor trampoline of the GCC `__do_global_ctors_aux` kind to reverse. The surviving CRT trampoline is the pair `__do_init` / `__do_fini`, and only `__do_fini` does real work (`__cxa_finalize`). Constructor *iteration* is performed by the loader over `DT_INIT_ARRAY`. This is a clang-CRT vs GCC-CRT difference, not a missing function.

---

## GOT / PLT Bring-Up

### Purpose

Before *any* init code runs (step 1 of the firing order), the loader applies the
binary's relocations. The init stubs above already depend on this: `_init`
reads the `__gmon_start__` GOT slot (`0x22048d60`, an `R_X86_64_GLOB_DAT`),
`__do_fini` reads the `__cxa_finalize` slot and `__dso_handle` (`0x222551c0`)
from relocated data, and every one of
the 2900 `DT_INIT_ARRAY` slots is itself an `R_X86_64_RELATIVE` target the
loader must write. So GOT/PLT relocation is the true *first* event of the
lifecycle, logically prior to `DT_PREINIT_ARRAY`.

### Section Layout

| Section | VA | Size | Role |
|---|---|---|---|
| `.plt` | `0x213f0830` | 7584 B | Lazy/eager call thunks into imported functions (e.g. libc, libdl). |
| `.got` | `0x22048d50` | 50256 B | Global offset table — relocated data/function pointers. |
| `.got.plt` | `0x224c2980` | 3808 B | PLT-specific GOT slots; resolved on first call (or eagerly under `BIND_NOW`). |

The dominant relocation kind across libtpu is `R_X86_64_RELATIVE` (the three
init arrays are entirely `RELATIVE`); imported-symbol slots use
`R_X86_64_GLOB_DAT` (99 entries) / `R_X86_64_JUMP_SLOT` (`.rela.plt`, 473
entries). The system imports the loader must satisfy come from six `DT_NEEDED`
libraries — `libm.so.6`, `libpthread.so.0`, `libdl.so.2`, `librt.so.1`,
`libc.so.6`, `ld-linux-x86-64.so.2` — and number **383 versioned `UND FUNC`
imports** (`@GLIBC_*` / `@GCC_*`) in `.dynsym`; `dlopen`/`dlsym`/`dlerror`/`dlclose`
(from `libdl`) are the symbols the framework's discovery handshake later rides
on. (A further **123** weak `UND NOTYPE` symbols — 67 `TF_*` optional
TensorFlow C-API hooks plus 56 other weak references (`__gmon_start__`,
`_ZTH*` thread-init helpers, `__morestack`, grpc/xla weak globals) — are
satisfied only if a host binds them, not loader-mandated.)

> **NOTE —** the GOT/PLT mechanics here are entirely standard x86-64 PSABI; libtpu adds nothing custom at this layer. The single reimplementation-relevant fact is the *ordering dependency*: the init arrays are themselves relocation targets, so a loader that ran `DT_INIT_ARRAY` before completing `R_X86_64_RELATIVE` processing would jump through zeroed slots. Relocation strictly precedes all init firing.

---

## Related Components

| Component | Relationship |
|---|---|
| `cpu_feature_fail_fast @ 0x2110abc0` | `DT_PREINIT_ARRAY[0]` — the first thing the loader runs; CPU ISA hard gate |
| `setup_dl_debug_hook @ 0x2114eec0` | `DT_PREINIT_ARRAY[1]` — dynamic-linker debug rendezvous hook |
| `__cpu_indicator_init @ 0x21211240` | `DT_INIT_ARRAY[0]` — GCC ifunc CPU-feature resolver, runs before constructors |
| `__do_init @ 0xe63c000` | CRT init-array entry; set-once byte guard `__do_init.__initialized` |
| `__do_fini @ 0xe63c020` | `DT_FINI_ARRAY[0]` — guarded `__cxa_finalize(__dso_handle)` |
| `rand_thread_state_clear_all @ 0x2063df60` | `DT_FINI_ARRAY[1]` — per-thread BoringSSL/RNG cleanup at unload |
| `__gmon_start__` (weak UND, GOT slot `0x22048d60`) | Weak gmon hook called by `_init`; unbound (no-op) in this wheel |

## Cross-References

- [overview.md](overview.md) — the full load-to-unload lifecycle map; this page owns its Stage 0 ELF mechanics
- [do-init-do-fini.md](do-init-do-fini.md) — the per-constructor `DT_INIT_ARRAY` iteration, the `_GLOBAL__sub_I_*` set, the `__do_init`/`__do_fini` guards in context, and the full `FINI_ARRAY` body
- [module-init-plugin-discovery.md](module-init-plugin-discovery.md) — the `PREINIT_ARRAY` CPU-feature gate decode, the register-only constructor storm, and the deferred `PJRT_Plugin_Initialize` bootstrap
