# Host-Runtime Concurrency Primitives

> **Scope.** This page is a *concurrency audit* of the host-side nrtucode codec
> library `libnrtucode_internal.so` — the x86-64 shared object that builds
> ext-ISA loadable libraries (`ll`), `nrtucode_core` boot state, and nrtucode
> contexts for the GPSIMD device. The conclusion is a **negative result**: the
> layer is **lock-free and single-primitive**. It contains *exactly one* atomic
> construct — a process-global `int32` live-object counter — and **zero**
> mutexes, **zero** compare-and-swap, **zero** pthread linkage, **zero**
> thread-local storage, **zero** memory fences, and **zero** thread creation.
> The substantive host-side locking lives one library down the stack in
> `libnrt.so`, *not* here. The takeaway for a reimplementer: **you can build the
> host codec single-threaded and caller-serialized.**

**Binary under analysis.**
`neuronx-gpsimd/extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/libnrtucode_internal.so`
— ELF64 x86-64, 10,276,288 B, not stripped, BuildID(sha1)
`9cbf78c6f59cdb5839f155fdb2113bbe51e585fd`. Addresses are VAs; `.text`/`.rodata`
have VMA == file offset (`.bss@0x9bb560` is `NOBITS`, no file content). All
figures below come from stock `objdump -d` / `nm` / `nm -D` / `readelf` piped to
`rg`, never from a decompile (which inflates symbol-hit counts). The page default
is `[HIGH · OBSERVED]`; claims that depart from it carry an explicit tag.

---

## 1. The host concurrency model in one sentence

The host codec layer is **"caller-serialized, leak-counted."** It owns no
thread, spawns no thread, holds no lock, and waits on nothing. Its single atomic
is *not* a synchronization primitive guarding shared state — it is a
debug/hygiene **live-object counter** (`create → +1`, `destroy → −1`) read once
at process exit to print a leak / double-free diagnostic. The `lock` prefix
exists solely so that a host application that *does* drive create/destroy from
multiple threads still arrives at a correct net count; it imposes **no ordering**
on the objects themselves, because there is no shared mutable structure to
order.

Every clause is byte-pinned in §2–§3.

---

## 2. Proving the negatives (the core of this audit)

Each "ZERO X" claim is a reproducible binary query. The command and its exact
count are shown inline. `rg -c` exits non-zero with no matches (suppressing the
`0`), so a trailing `|| echo 0` is used where a "no-match" must be displayed.

> **NOTE.** `rg -c` prints nothing and returns exit 1 on zero matches; the
> `|| echo 0` idiom surfaces the true count. This is why the negatives below all
> read a literal `0`.

### 2a. The ONE atomic — two `lock`-prefixed instructions

```text
$ objdump -d libnrtucode_internal.so | rg -c 'lock (inc|dec)'
2
```

Both target the same global, via RIP-relative addressing:

```text
$ objdump -d libnrtucode_internal.so | rg 'lock '
  9b17a0: f0 ff 05 bd 9d 00 00   lock incl 0x9dbd(%rip)   # 9bb564 <objcount>
  9b17b0: f0 ff 0d ad 9d 00 00   lock decl 0x9dad(%rip)   # 9bb564 <objcount>
```

The lock target resolves to symbol **`objcount @ 0x9bb564`** (`.bss`, `int32`),
the *only* runtime-mutable shared global in the binary:

```text
$ nm libnrtucode_internal.so | rg -i 'objcount'
00000000009b17c0 t nrtucode_objcount_check
00000000009b17b0 t nrtucode_objcount_decrement
00000000009b17a0 t nrtucode_objcount_increment
00000000009b1780 t nrtucode_objcount_setup
00000000009bb564 b objcount
```

### 2b. ZERO compare-and-swap / xadd

```text
$ objdump -d libnrtucode_internal.so | rg -c 'cmpxchg|xadd' || echo 0
0
```

No `lock cmpxchg`, no `xadd`, no `lock xchg`-on-shared-mem used as a spinlock.
There is no lock-free CAS loop, no spinlock back-off, no ticket lock anywhere in
`.text`.

### 2c. ZERO pthread linkage

```text
$ nm -D libnrtucode_internal.so | rg -c 'pthread_' || echo 0
0
$ readelf -d libnrtucode_internal.so | rg 'NEEDED'
 0x0000000000000001 (NEEDED)  Shared library: [libc.so.6]
```

The **only** `DT_NEEDED` is `libc.so.6`. No `libpthread`, no `libstdc++`, no
`libatomic`.

> **NOTE.** On glibc ≥ 2.34 pthread is folded into libc, so the absent
> `libpthread` NEEDED is not *by itself* conclusive. The dynsym/symtab checks
> below close the gap: no pthread *symbol* is referenced at all. The full
> 17-entry undefined-import set is pure libc —
> `abort, calloc, __cxa_atexit, fprintf, free, fwrite, getenv, malloc, memcpy,
> memset, realloc, snprintf, stderr, strcmp, strncpy, strnlen, vsnprintf` — and
> a grep of the *entire* symbol table for any synchronization name is empty:
>
> ```text
> $ nm libnrtucode_internal.so | rg -i 'pthread|atomic|mutex|__sync|spin|rwlock|futex|sem_|barrier|once|cond_' | head
> (no output)
> ```

### 2d. ZERO thread-local storage

```text
$ readelf -SW libnrtucode_internal.so | rg -c '\.tdata|\.tbss' || echo 0
0
$ objdump -d libnrtucode_internal.so | rg -c '%fs:' || echo 0
0
```

No `.tdata`/`.tbss` sections, no `%fs:` accesses (and no `%gs:` either — verified
separately, `0`). Crucially, there is **no `%fs:0x28` stack canary** — this TU
was built without `-fstack-protector`, which independently confirms the absence
of any TLS slot.

### 2e. ZERO memory fences

```text
$ objdump -d libnrtucode_internal.so | rg -c 'mfence|lfence|sfence' || echo 0
0
```

No `mfence`/`lfence`/`sfence`, and no `pause` spin-wait hint (verified
separately, `0`). The library issues no explicit memory-ordering barrier of any
kind.

### 2f. ZERO thread creation

```text
$ nm -D libnrtucode_internal.so | rg -c 'pthread_create|thread|clone' || echo 0
0
```

The library never spawns a thread, never `clone()`s, never `fork()`s. It also
never yields, sleeps, or polls a host fd (no `sched_yield`/`nanosleep`/`usleep`/
`poll`/`select`/`epoll` import) and installs no signal handler (no `sigaction`/
`signal`).

> **NOTE.** The weak `_ITM_register/deregisterTMCloneTable` and `__gmon_start__`
> references are GCC's transactional-memory clone-table and profiling stubs
> emitted into *every* translation unit; they are unresolved weak refs, **not**
> actual TM or threading use.

### Audit summary table

| Category | Command | Count | Verdict |
| --- | --- | --- | --- |
| Atomic RMW (`lock inc`/`dec`) | `objdump -d \| rg -c 'lock (inc\|dec)'` | **2** | both on `objcount` |
| CAS / xadd | `objdump -d \| rg -c 'cmpxchg\|xadd'` | **0** | no compare-and-swap |
| pthread symbols (dynsym) | `nm -D \| rg -c 'pthread_'` | **0** | no pthread linkage |
| pthread symbols (full symtab) | `nm \| rg -i 'pthread\|mutex\|sem_\|futex…'` | **0** | none |
| TLS sections | `readelf -SW \| rg -c '\.tdata\|\.tbss'` | **0** | no TLS |
| `%fs:` accesses | `objdump -d \| rg -c '%fs:'` | **0** | no TLS / no canary |
| Fences | `objdump -d \| rg -c 'mfence\|lfence\|sfence'` | **0** | no barriers |
| Thread creation | `nm -D \| rg -c 'pthread_create\|thread\|clone'` | **0** | single-threaded |
| `DT_NEEDED` | `readelf -d \| rg 'NEEDED'` | **1** | `libc.so.6` only |

**The entire lock budget of a 10.3 MB binary is two instructions.**

---

## 3. The sole atomic — `objcount`, a lock-free live-object refcount

Four functions plus one `.bss int32` form the **entire** atomic surface:

| Symbol | Address | Role |
| --- | --- | --- |
| `objcount` | `0x9bb564` | `.bss int32`, the counter |
| `nrtucode_objcount_setup` | `0x9b1780` | `init_array` ctor — zero + register atexit |
| `nrtucode_objcount_increment` | `0x9b17a0` | create hook (`+1`) |
| `nrtucode_objcount_decrement` | `0x9b17b0` | destroy hook (`−1`) |
| `nrtucode_objcount_check` | `0x9b17c0` | atexit diagnostic |

### 3a. The two atomic instructions, as C pseudocode

```c
/* @0x9b17a0  nrtucode_objcount_increment  —  the ENTIRE function */
void nrtucode_objcount_increment(void) {
    /* 9b17a0: f0 ff 05 bd 9d 00 00   lock incl objcount(%rip) */
    _InterlockedIncrement(&objcount);   /* atomic RMW on the .bss word at 0x9bb564 */
    /* 9b17a7: c3                      ret  */
}

/* @0x9b17b0  nrtucode_objcount_decrement  —  the ENTIRE function */
void nrtucode_objcount_decrement(void) {
    /* 9b17b0: f0 ff 0d ad 9d 00 00   lock decl objcount(%rip) */
    _InterlockedDecrement(&objcount);   /* atomic RMW on the same word */
    /* 9b17b7: ret */
}
```

These are atomic read-modify-write on a single process-global 32-bit word.
Nothing is read or written *under* them — there is **no critical section**: the
inc/dec are single instructions with no lock held across any allocation, table,
or list. Granularity is one lock-free word shared by all three object classes
(context / core / ll); it is neither per-context, per-core, nor a global mutex.

> **GOTCHA.** A decompiler may render the increment as `inc(512)` (context path)
> or `inc(a1)` (core path) with a spurious argument. **CORRECTION:** the real
> function takes *no* argument — its body is literally `lock incl objcount; ret`.
> The apparent "argument" is a stale register the decompiler attributed to the
> call. Verify against the 8-byte disassembly above, not the C output.

### 3b. Setup — load-time constructor, atomic store-zero + atexit wiring

```c
/* @0x9b1780  nrtucode_objcount_setup  —  runs from .init_array[1] at load */
void nrtucode_objcount_setup(void) {
    /* 9b1780: 31 c0               xor  %eax,%eax           */
    /* 9b1782: 87 05 dc 9d 00 00   xchg %eax, objcount(%rip)  -> objcount = 0 */
    _InterlockedExchange(&objcount, 0);
    /* 9b1788: lea check; 9b178f: jmp atexit  (tail call) */
    return atexit(nrtucode_objcount_check);
}
```

> **NOTE.** The `xchg %eax, objcount` store is *implicitly* atomic on x86 (a
> memory-operand `xchg` is serializing) but its encoding carries **no `lock`
> prefix byte** — which is why it does *not* appear in the `rg -c 'lock (inc|dec)'`
> count of **2**. It is a one-shot zero-init store at load, not a runtime
> synchronizer. The lock budget remains exactly two prefixed RMW instructions.

Wiring is confirmed by the relocation table — `.init_array` (`@0x9b8cd8`) holds
two `R_X86_64_RELATIVE` relocs:

```text
$ readelf -r libnrtucode_internal.so | rg '9b0250|9b1780'
0000009b8cd8  …  R_X86_64_RELATIVE   9b0250   # frame_dummy
0000009b8ce0  …  R_X86_64_RELATIVE   9b1780   # nrtucode_objcount_setup
```

So `objcount_setup` runs automatically on `.so` load.

### 3c. Check — the atexit diagnostic (the *why* of the atomic)

```c
/* @0x9b17c0  nrtucode_objcount_check  —  registered via atexit */
void nrtucode_objcount_check(void) {
    int n = objcount;                          /* 9b17c1: mov objcount,%eax */
    if (n > 0)                                 /* 9b17c9: jg  -> leaked     */
        fprintf(stderr,
            "nrtucode: nonfatal internal error: %i object(s) leaked, improper "
            "teardown of library (did you forget to call nrt_close or "
            "nrtucode_context_destroy?)\n", objcount);     /* str @.rodata 0x47cb */
    if (objcount < 0)                          /* 9b17d3: js  -> double-free */
        fwrite("nrtucode: internal error: object(s) double-freed, improper "
               "teardown of library\n", 0x4f, 1, stderr);  /* str @.rodata 0x5271 */
}
```

The two diagnostic strings are byte-confirmed in `.rodata` at the exact `lea`
targets in the disassembly (`0x47cb`, `0x5271`):

```text
$ strings -t x libnrtucode_internal.so | rg -i 'leaked|double-freed'
  47cb  nrtucode: nonfatal internal error: %i object(s) leaked, …
  5271  nrtucode: internal error: object(s) double-freed, …
```

**Positive residue ⇒ leaked objects** (a `destroy` was missed); **negative ⇒
double-free** (`destroy` ran more often than `create`). This is purely a
hygiene / QA tripwire: it **never aborts and never blocks** — it only prints. The
atomicity exists so that a multi-threaded host arrives at a correct net count and
a trustworthy leak verdict.

---

## 4. Which APIs "take the lock" (i.e. touch `objcount`)

Six call sites — **three balanced create/destroy pairs** — and nothing else in
the binary calls inc/dec. Confirmed by xref of `0x9b17a0` / `0x9b17b0`:

```text
$ objdump -d libnrtucode_internal.so | rg 'call.*9b17a0|call.*9b17b0'
  9b0311: call 9b17a0 <…increment>   9b03ef: call 9b17b0 <…decrement>
  9b06ed: call 9b17a0 <…increment>   9b07ee: call 9b17b0 <…decrement>
  9b1c23: call 9b17a0 <…increment>   9b1de8: call 9b17b0 <…decrement>
```

| Object class | CREATE (`++`) | DESTROY (`−−`) |
| --- | --- | --- |
| context | `nrtucode_context_create @0x9b0290` (inc call @`0x9b0311`) | `nrtucode_context_destroy @0x9b03d0` (dec call @`0x9b03ef`) |
| core (boot state) | `nrtucode_core_create @0x9b0640` (inc call @`0x9b06ed`) | `nrtucode_core_destroy @0x9b0780` (dec call @`0x9b07ee`) |
| ll (loadable lib) | `nrtucode_ll_create @0x9b1a90` (inc call @`0x9b1c23`) | `nrtucode_ll_destroy @0x9b1da0` (dec call @`0x9b1de8`) |

**Increment is post-success; decrement is at-free.** `context_create`
`malloc`s the ctx (`0x28`) plus its buffer (`0x200`); it stores `*ctx_out` and
calls `inc()` **only when both succeed** — on the inner `malloc` failing it
`free`s the outer block and returns error *without* `inc`, so no spurious count.
`*_destroy` validates the handle, `free`s the block(s), **then** `dec()`s. No
allocation, table, or list is serialized — create/destroy are plain `malloc`/
`free` with an atomic counter bump. The object model these calls populate is
documented in [The nrtucode Object Model Graph](object-model-graph.md).

---

## 5. Contrast — where the real host locks live (`libnrt.so`, NOT here)

The substantive host-side concurrency is one library *down* the stack, in the
big NRT driver `libnrt.so`. That binary **does** link pthread and **does** hold
mutexes — the polar opposite of the codec layer. Grounded against the driver
(`neuronx-runtime/extracted/…/libnrt.so.2.31.24.0`):

```text
$ readelf -d libnrt.so… | rg 'NEEDED.*pthread'
 (NEEDED)  Shared library: [libpthread.so.0]          # present in libnrt, ABSENT in codec
$ nm -D libnrt.so… | rg -c 'pthread_mutex'
4
$ objdump -d libnrt.so… | rg -c 'lock (inc|dec|cmpxchg|xadd|add|or|and)'
809
```

So `libnrt.so` carries **809** lock-prefixed RMW instructions and pthread mutex
imports, versus the codec's **2** lock-prefixed instructions and **0** pthread.
The driver's concrete primitives (`model_db_lock`, `ulib_staging_lock`,
`allocator->lock`, the shared-dmem `ref_count`, the model-drain `pause`-spin)
guard the *shared, mutable, multi-model* state — the per-core model database, the
ext-ISA staging cache, and the device-memory allocator free-lists. Those details
are owned by [Multi-Model / Context Tree + dmem Allocator](multimodel-context-dmem.md);
each is **confirmed absent** from the codec by §2.

> **The layering, read off the binaries.** `libnrt` owns the shared mutable
> state and pays for it with fine-grained pthread mutexes + an atomic refcount +
> a pause-spin drain. `libnrtucode_internal` is the **stateless-by-design codec**
> beneath it: each `(context|core|ll)` it builds is an isolated heap object
> handed back to the caller; it shares nothing across objects, so it needs no
> lock. Its only thread-awareness is the atomic leak counter — defensive
> bookkeeping, not coordination. This is a textbook *"locks at the resource
> manager, lock-free at the pure transform"* split. `[HIGH · INFERRED]`

---

## 6. Contrast with the device-side DRF model

Two entirely different concurrency philosophies meet at the host↔device seam, and
they **do not share a synchronization domain**:

| Axis | **Host** — `libnrtucode_internal.so` (this page) | **Device** — Vision-Q7 "Cairo" firmware |
| --- | --- | --- |
| Concurrency primitive | one atomic `int32` counter (`objcount`), lock-free | LL/SC exclusive monitor (`L32EX`/`S32EX`) + barrier-stage `SYNC`/`SET`/`WAIT`, gen-tag completion |
| Threads spawned | none; caller-serialized | per-core, hardware-partitioned |
| Waiting | none (no poll/sleep/yield/fence) | **poll-driven, on-device only** |
| Shared mutable state | none beyond the leak counter | each core owns its DRAM region |
| Safety property | single-owner objects; counter is a diagnostic | **data-race-free by construction** (per-core memory ownership; ordering via `memw;memw`) |

The device side guarantees correctness **structurally** — each Q7 core owns its
DRAM region and they never share writable state; cores join via a host-driven
**DRAIN** plus a **completion semaphore**, with **no host lock participating**.
That device sync/event/notification path is documented in
[the Device→Host interrupt / notification path](../control/interrupt/device-host-notification.md).

**Synthesis.** The host nrtucode layer builds isolated library/context objects
(lock-free, leak-counted), hands the prepared image to `libnrt`, and `libnrt`
(mutex-protected) stages it to the device; on the device, correctness is then
guaranteed by per-core memory ownership + LL/SC + barrier ops. The host codec and
the device occupy **disjoint synchronization domains**; the codec's contribution
is a clean, lock-free, single-owner object factory. `[HIGH · INFERRED]`

---

## 7. Reimplementation guidance

1. **Build the host codec single-threaded and caller-serialized.** No internal
   thread, no internal lock, no wait. If your host calls `create`/`destroy`
   concurrently across threads, make `objcount` an atomic `int32`
   (`__atomic_add_fetch` / `std::atomic<int32_t>`); otherwise a plain `int`
   suffices.
2. **`objcount` is a diagnostic, not a guard.** Increment **post-success**,
   decrement **at-free**, read once at process exit (`atexit`). Print on `>0`
   (leak) and `<0` (double-free); never abort, never block.
3. **No barriers / TLS / canary are required** to match the shipped binary's
   behavior — it was built without `-fstack-protector` and emits no fence.
4. **All real host locking belongs in your `libnrt`-equivalent**, not the codec:
   the per-core model DB, the ext-ISA staging cache, and the dmem allocator
   free-lists are where mutexes go.

---

## Confidence ledger

| Claim | Conf | Tag | Basis |
| --- | --- | --- | --- |
| `DT_NEEDED = {libc.so.6}` only; no `libpthread` | HIGH | OBSERVED | `readelf -d` |
| 0 pthread/atomic/mutex/sem of 17 dynamic imports | HIGH | OBSERVED | `nm -D` + `rg` |
| 0 such symbols in full symtab | HIGH | OBSERVED | `nm` + `rg` |
| Exactly 2 `lock`-prefixed RMW, both on `objcount` | HIGH | OBSERVED | `objdump -d` |
| 0 `cmpxchg`/`xadd`/`mfence`/`lfence`/`sfence`/`pause` | HIGH | OBSERVED | `objdump -d` |
| 0 `%fs:`/`%gs:` TLS, no `.tdata`/`.tbss` | HIGH | OBSERVED | `objdump` + `readelf` |
| 0 thread-create / yield / sleep / poll / signal | HIGH | OBSERVED | `nm -D` |
| `objcount @ 0x9bb564` is the only runtime-mutable global | HIGH | OBSERVED | `nm` + xref |
| `objcount_setup` wired as `.init_array[1]` RELATIVE reloc | HIGH | OBSERVED | `readelf -r` |
| `check()` atexit-registered, prints leak/double-free | HIGH | OBSERVED | disasm + `.rodata` strings |
| 6 call sites = 3 create(`++`)/destroy(`−−`) pairs | HIGH | OBSERVED | `objdump` xref + `nm -n` |
| decompiler `inc(512)`/`inc(a1)` arg is an artifact | HIGH | OBSERVED | disasm (no arg) |
| real host mutexes live in `libnrt.so` (809 lock RMW, pthread) | HIGH | OBSERVED | `readelf`/`nm`/`objdump` on `libnrt.so` |
| host lock-free vs device DRF-by-construction | HIGH | CARRIED | device sync/notification pages |
