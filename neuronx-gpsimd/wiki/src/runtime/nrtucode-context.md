# `nrtucode_context_t` + Lifecycle

The `nrtucode_context_t` is the root host-side object of the **nrtucode** ucode
runtime — the small handle every other nrtucode call threads through. It is a
five-qword (`0x28`-byte) heap struct that bundles two function-pointer vtables
(a host read/write+logging back-end and a device-memory platform allocator), an
opaque user pointer, and a private log-formatting scratch buffer. This page
reconstructs that object field-exact and documents its five public lifecycle
entry points.

All addresses, offsets, slot indices and string offsets below were read from the
two shipped binaries via static analysis:

- **`libnrtucode_internal.so`** — `10,276,288` bytes, BuildID
  `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd`, **not stripped** (the symbol twin;
  ships in `aws-neuronx-gpsimd-customop-lib_0.21.2.0`). All `0x9bXXXX` addresses
  on this page are this binary's `.text`/`.data.rel.ro`.
- **`libnrtucode.so`** — the stripped sibling that actually ships in the
  customer wheel, BuildID `abf4e088ebef327b2abac3551f2b1de699d50f38`. Its five
  `.dynsym` export addresses (`0x308XXX`) are byte-identical codegen of the same
  source, used here only for the public-ABI address mapping.

Recovered `__FILE__` / assertion string tokens place this object in three source
units, all present verbatim in `.rodata`:

```
nrtucode_context.c
nrtucode_objcount.c
nrtucode_platform_memhandle_dummy.c
```

The struct's own type name is printed by the dummy-memhandle error string —
`nrtucode_context_t@%p: …` (`.rodata` `0x4a9f`) — which is the binary-derived
proof of the type name used throughout this page. `[string:nrtucode_context.c]`
`[string:0x4a9f]` `[HIGH][OBSERVED]`

> **NOTE** — Companion pages. This is the per-object deep-dive. The wider object
> graph (who points at whom) lives in
> [The nrtucode Object Model Graph](object-model-graph.md); the `0x28` context
> struct and its two vtables documented here are the shared data those pages
> describe. The logging gate and the leak-tracking allocator that consume the
> last two struct fields are in
> [nrtucode Logging + Leak-Tracking Allocator](nrtucode-logging-allocator.md);
> the `nrtucode_core_t` object that *holds* a context pointer is in
> [nrtucode_core_t Struct + Introspection/Boot](nrtucode-core.md); the
> device-memory staging/translation ABI behind `memhandle_impl` slot `+0x20` is
> the device-side pointer model in
> [Q7PtrType + Lazy Translation](../abi/q7ptrtype.md).

---

## 1. Export table

The context API is exactly five public C functions plus two internal-linkage
log helpers. Both address columns are the same code; the stripped column is the
public ABI the wheel exposes, the internal column carries the names.

| Symbol | Stripped `libnrtucode.so` | Internal `libnrtucode_internal.so` | Linkage |
| --- | --- | --- | --- |
| `nrtucode_context_create` | `0x308770` | `0x9b0290` | `T` (export) |
| `nrtucode_context_set_memhandle_impl` | `0x308870` | `0x9b0390` | `T` (export) |
| `nrtucode_context_destroy` | `0x3088b0` | `0x9b03d0` | `T` (export) |
| `nrtucode_context_get_userdata` | `0x308910` | `0x9b0430` | `T` (export) |
| `nrtucode_context_set_userdata` | `0x308950` | `0x9b0470` | `T` (export) |
| `nrtucode_context_vlog` | `0x308990` (`sub_308990`) | `0x9b04b0` | `t` (internal) |
| `nrtucode_context_log` | `0x308a20` (`sub_308A20`) | `0x9b0540` | `t` (internal) |

`[OBSERVED]` via `nm -D libnrtucode.so` and `nm libnrtucode_internal.so`.

> **CORRECTION** — A prior pass listed `nrtucode_context_create` at `0x308740`.
> That address is **`nrtucode_get_api_level`** (confirmed in the stripped
> `.dynsym`: `0000000000308740 T nrtucode_get_api_level`). `create` is at
> `0x308770`. The two are adjacent which is how they were transposed.
> `[OBSERVED][HIGH]`

---

## 2. The context struct — `0x28` bytes, 5 qwords

The struct has no IDA-typed definition (it is a `malloc(0x28)` blob in the
binary); the layout below is reconstructed from the create routine — which
writes every one of the five qwords — and cross-checked against each accessor.
Confidence is **HIGH/OBSERVED** for all five members: every store is in the
`nrtucode_context_create` body at `0x9b0290` (internal).

| Offset | Size | Type | Field | Init value | Conf |
| --- | --- | --- | --- | --- | --- |
| `+0x00` | 8 | `rw_impl_t*` | `rw_impl` | the `rw_impl` create arg (`%rsi`) | HIGH / OBSERVED |
| `+0x08` | 8 | `const memhandle_vtbl*` | `memhandle_impl` | `&plat_memhandle_dummy` (`0x9b8cf0`) | HIGH / OBSERVED |
| `+0x10` | 8 | `void*` | `userdata` | `NULL` | HIGH / OBSERVED |
| `+0x18` | 8 | `uint64_t` | `log_scratch_size` | `0x200` (512) | HIGH / OBSERVED |
| `+0x20` | 8 | `char*` | `log_scratch_buf` | `malloc(0x200)` | HIGH / OBSERVED |

There are **no other fields** — `malloc` is called with exactly `0x28`, and all
five qwords are written. Disassembly of the initialization sequence (internal
`0x9b0290`):

```asm
9b02c1:  mov  $0x28,%edi              ; sizeof(nrtucode_context_t) = 0x28
9b02c6:  call malloc@plt              ; -> %rax (then %r15)
9b02d8:  mov  %r14,(%rax)             ; +0x00  rw_impl   = create arg a2
9b02db:  lea  0x8a0e(%rip),%rax       ; &plat_memhandle_dummy (0x9b8cf0)
9b02e2:  mov  %rax,0x8(%r15)          ; +0x08  memhandle_impl = dummy stub
9b02e6:  movq $0x0,0x10(%r15)         ; +0x10  userdata = NULL
9b02ee:  mov  $0x200,%edi
9b02f3:  call malloc@plt              ; 512-byte log scratch -> %rax
9b02fd:  mov  %rax,0x20(%r15)         ; +0x20  log_scratch_buf
9b0301:  movq $0x200,0x18(%r15)       ; +0x18  log_scratch_size = 512
9b0309:  mov  %r15,(%r12)             ; *ctx_out = ctx
9b0311:  call nrtucode_objcount_increment
```

`[OBSERVED]` `[addr:0x9b02c1..0x9b0311]`.

> **GOTCHA** — `log_scratch_size` (`+0x18`) is the *capacity* of the
> `+0x20` buffer, not its current length. It starts at `512` and is grown by a
> `realloc` inside `nrtucode_context_vlog`/`_log` whenever a formatted line
> overflows the current buffer (see §5). Treating `+0x18` as a payload length
> will desync the formatter.

> **NOTE** — Decompiler qword-index to byte-offset mapping for cross-reference
> against IDA pseudocode of `create`: `v5[0]→+0x00`, `v5[1]→+0x08`,
> `v6[2]→+0x10`, `v6[3]→+0x18`, `v6[4]→+0x20`. The destroy routine's `free(ptr[4])`
> therefore frees the `+0x20` log buffer, not a fifth slot.

### Reconstructed C

```c
typedef struct nrtucode_context_t {           /* sizeof = 0x28 */
    void*                 rw_impl;             /* +0x00  {rw_impl_vtbl* vt; ...}  */
    const memhandle_vtbl* memhandle_impl;      /* +0x08  init &plat_memhandle_dummy */
    void*                 userdata;            /* +0x10  opaque, init NULL       */
    uint64_t              log_scratch_size;    /* +0x18  init 512, grows         */
    char*                 log_scratch_buf;     /* +0x20  malloc(512), fmt target */
} nrtucode_context_t;
```

---

## 3. `memhandle_impl` — the device-memory platform vtable (`ctx+0x08`)

`ctx->memhandle_impl` points at a five-slot, 8-byte-stride function-pointer
table that is the **device-memory platform interface**: bulk allocate / free /
read / write on device memory, plus an address-translation slot. Every method
takes the **context pointer** as its first argument (`%rdi = ctx`) so it can log
through `ctx`.

At create time the table is the static **`plat_memhandle_dummy`** (internal
`0x9b8cf0`, in `nrtucode_platform_memhandle_dummy.c`); a real platform
implementation is installed by `nrtucode_context_set_memhandle_impl`.

> **NOTE — section delta.** `plat_memhandle_dummy` lives in `.data.rel.ro`
> (VMA `0x9b8cf0`, file offset `0x9b6cf0`, **Δ = 0x2000**). When dumping its
> bytes from the file you must subtract `0x2000`; the slot *targets* come from
> the dynamic relocations, not the on-disk zero words (the image is unrelocated
> on disk). Confirmed via `readelf -SW`: `.rodata` Δ=0, `.text` Δ=0x1000,
> `.data.rel.ro` Δ=0x2000, `.data` Δ=0x3000.

| Slot | Dummy target (internal) | Role | Conf |
| --- | --- | --- | --- |
| `+0x00` | `dummy_device_malloc` (`0x9b1820`) | `device_malloc(ctx, …, uint64 size /*rdx*/, void** out /*rcx*/) -> err` | HIGH / OBSERVED |
| `+0x08` | `dummy_device_free` (`0x9b1850`) | `device_free(ctx, handle /*rsi*/)` | HIGH / OBSERVED |
| `+0x10` | `dummy_read_memhandle` (`0x9b1860`) | `read_memhandle(ctx, …) -> err` | MED / OBSERVED |
| `+0x18` | `dummy_write_memhandle` (`0x9b1870`) | `write_memhandle(ctx, handle /*rsi*/, off /*rdx*/, len /*rcx*/, src /*r8*/) -> err` | HIGH / OBSERVED |
| `+0x20` | *(NULL in dummy — no reloc)* | `device_addr(ctx, handle /*rsi*/) -> device address` | MED / OBSERVED slot |

So the **memhandle vtable size = `0x28` (5 slots).** The dummy fills slots
`0..3`; a real implementation must additionally supply slot `4` (`+0x20`).

The dynamic relocations prove the slot count exactly — four `R_X86_64_RELATIVE`
entries and **nothing at `+0x20`** (`0x9b8d10`):

```
0x9b8cf0  R_X86_64_RELATIVE  -> 0x9b1820   ; +0x00 device_malloc
0x9b8cf8  R_X86_64_RELATIVE  -> 0x9b1850   ; +0x08 device_free
0x9b8d00  R_X86_64_RELATIVE  -> 0x9b1860   ; +0x10 read
0x9b8d08  R_X86_64_RELATIVE  -> 0x9b1870   ; +0x18 write
            (no relocation at 0x9b8d10)     ; +0x20 stays NULL
```

`[OBSERVED]` `[addr:0x9b8cf0]` via `readelf -rW`.

> **GOTCHA** — Slot `+0x20` (`device_addr`) is **NULL in the dummy**. Calling it
> on a freshly-created context (before `set_memhandle_impl`) faults. The one
> call site that uses it (`nrtucode_ll_get_sequence_common`, internal `0x9b20cc`,
> `call *0x20(%rax)`) is *guarded* by a feature-flag check (`cmp $0,…+0x18`)
> first, so the live path never reaches the NULL slot through the dummy. The
> "device address / translate" naming is **INFERRED** from how its return value
> is consumed (fed into an instruction-sequence fetch); only the slot offset is
> OBSERVED. See [Q7PtrType + Lazy Translation](../abi/q7ptrtype.md) for the
> device pointer model this slot resolves into.

> **CORRECTION** — An earlier pass guessed `[+0x08]`=alloc/free combined,
> `[+0x18]`=write, `[+0x20]`=device-addr (4 slots). The binary shows a clean
> **5-slot** table: `+0x00` malloc / `+0x08` free / `+0x10` read / `+0x18` write
> / `+0x20` device-addr. `[OBSERVED][HIGH]`

### Dummy bodies

The dummy slots are deliberately inert; they are the "platform not attached"
trap. Bodies (internal):

```c
/* +0x00  dummy_device_malloc @0x9b1820 */
int dummy_device_malloc(void* ctx, ..., void** out /*rcx*/) {
    *out = NULL;                                 /* movq $0,(%rcx) */
    nrtucode_context_log(ctx, /*sev*/1 /*ERROR*/,
        "nrtucode_context_t@%p: memhandle platform implementation is "
        "required but is not attached", ctx);    /* .rodata 0x4a9f */
    return 8;                                     /* NRTUCODE unsupported/not-attached */
}
/* +0x08  dummy_device_free  @0x9b1850 : bare `ret`  (no-op)              */
/* +0x10  dummy_read_memhandle @0x9b1860: `mov $8,%eax; ret`             */
/* +0x18  dummy_write_memhandle@0x9b1870: `mov $8,%eax; ret`             */
```

`[OBSERVED]` `[addr:0x9b1820..0x9b1875]`. The error format string
`"nrtucode_context_t@%p: memhandle platform implementation is required but is
not attached"` is at `.rodata 0x4a9f` (internal) / `0x2f39` (stripped), and is
the binary proof that `ctx+0x08` is the device-memory platform interface and
that the default leaves it stubbed.

### Reconstructed C — `memhandle_vtbl`

```c
typedef struct memhandle_vtbl {
    int      (*device_malloc)(void* ctx, /*...*/ uint64_t size, void** out); /* +0x00 */
    void     (*device_free)(void* ctx, void* handle);                        /* +0x08 */
    int      (*read)(void* ctx, /*...*/);                                     /* +0x10 */
    int      (*write)(void* ctx, void* handle, uint64_t off,
                      uint64_t len, const void* src);                         /* +0x18 */
    uint64_t (*device_addr)(void* ctx, void* handle);                         /* +0x20 (real impl only) */
} memhandle_vtbl;

extern const memhandle_vtbl plat_memhandle_dummy;  /* 0x9b8cf0; slot 4 == NULL */
```

Slot meanings are confirmed by real call sites elsewhere in the library:
`nrtucode_ll_create` (`0x9b1c5d` `call *(%rax)` with `rdx=0x1000000` alloc;
`0x9b1c81/ca3/cc0` write x3; `0x9b1cd8` free error path),
`nrtucode_core_destroy` (`0x9b07a2` free of `core[+0x38]`), and
`nrtucode_ll_destroy` (`0x9b1dc1` free). The `read` slot (`+0x10`) has no
located host call site in this library, so its argument list is **INFERRED**
from read/write symmetry (MED).

---

## 4. `rw_impl` — the host read/write + logging vtable (`ctx+0x00`)

`ctx->rw_impl` is the **caller-supplied** object whose first qword is itself a
vtable pointer. This is the host-side small-I/O and logging back-end. Because the
table is provided by the caller, its layout is an **ABI contract this library
consumes**, not one it defines — the slots below are inferred from the call
sites that drive them.

| Slot | Role (inferred from call args) | Conf |
| --- | --- | --- |
| `+0x00` | `read(rw, addr /*rsi*/, len=4 /*rdx*/, out /*rcx*/) -> err` | HIGH OBSERVED call, role inferred |
| `+0x08` | `write(rw, src /*rsi*/, len=4 /*rdx*/, in/out /*rcx*/) -> err` | MED OBSERVED call, role inferred |
| `+0x10` | `log_emit(rw, severity, const char* msg, uint64 msg_len)` | HIGH / OBSERVED |
| `+0x18` | `log_enabled(rw) -> bool` (gate predicate) | HIGH / OBSERVED |
| `+0x20` | aux op (size-hint / buffer-config) | LOW / OBSERVED slot only |

The two log slots are the most firmly grounded. In `nrtucode_context_vlog`
(internal `0x9b04b0`) the gate is called **first**, and the emit only if the
gate passes:

```asm
9b04c3:  mov  (%rdi),%rax        ; rax = *rw_impl  (the vtable)
9b04c6:  call *0x18(%rax)        ; +0x18 log_enabled(rw)  -> gate
9b04cd:  mov  0x18(%r14),%rsi    ; ctx->log_scratch_size  (capacity)
9b04d1:  mov  0x20(%r14),%rdi    ; ctx->log_scratch_buf   (target)
9b04db:  call vsnprintf@plt      ; format into the scratch buffer
...
9b04fc:  call realloc@plt        ; grow buffer if line overflowed
9b0509:  mov  %rax,0x20(%r14)    ;   ctx->log_scratch_buf = new
9b050d:  mov  %r15,0x18(%r14)    ;   ctx->log_scratch_size = new cap
...
9b0517:  mov  (%r14),%rax        ; rax = *rw_impl
9b0522:  call *0x10(%rax)        ; +0x10 log_emit(rw, …, buf, len)
```

`[OBSERVED]` `[addr:0x9b04c6 / 0x9b0522]`. This is the direct evidence that the
last two context fields (`+0x18` size, `+0x20` buf) are the `vsnprintf` scratch
used to format a log line before handing it to the caller's `log_emit`.

> **NOTE** — The `rw_impl` and `memhandle_impl` vtables are logically distinct
> objects at different `ctx` offsets: `rw_impl` (`+0x00`) is host-side small-I/O
> + logging; `memhandle_impl` (`+0x08`) is the bulk device-memory allocator.
> Both are function-pointer tables but they are not interchangeable. The
> `rw_impl` slots `+0x00/+0x08` are observed used as a 4-byte read/check/write
> magic-word handshake in the boot-state path (`nrtucode_core_on_ucode_booted`,
> magics `0x6099CB34` / `0x502B2DA1`); `+0x20` is hit once by
> `nrtucode_core_enable_logs_with_size_hint` (`0x9b0c6e`) with unpinned
> semantics (LOW).

### Reconstructed C — `rw_impl_vtbl`

```c
typedef struct rw_impl_vtbl {            /* object at ctx->rw_impl: { rw_impl_vtbl* vt; … } */
    int  (*read)(void* rw, uint64_t addr, uint32_t len, void* out);   /* +0x00 */
    int  (*write)(void* rw, uint64_t addr, uint32_t len, void* in);   /* +0x08 */
    void (*log_emit)(void* rw, uint32_t severity, const char* msg,
                     uint64_t msg_len);                               /* +0x10 */
    bool (*log_enabled)(void* rw);                                    /* +0x18 */
    /* +0x20 aux (size-hint / config) — semantics LOW confidence */
} rw_impl_vtbl;
```

---

## 5. The five lifecycle entry points

All five share an identical null-guard idiom — a NULL argument is **fatal**, not
error-returned:

```c
fprintf(stderr, "nrtucode: invalid API usage in `%s`: `%s` is null\n",
        <function-name>, <arg-name>);   /* fmt @.rodata 0x5469 */
abort();
```

This is an assert-style contract. `[OBSERVED]` `[string:0x5469]`.

### (1) `nrtucode_context_create` — internal `0x9b0290` / stripped `0x308770`

```c
int nrtucode_context_create(int api_version,            /* a1 — checked, NOT stored */
                            rw_impl_t* rw_impl,         /* a2 — "rw_impl"  @0x46b0  */
                            nrtucode_context_t** ctx_out) /* a3 — "ctx_out" @0x5394 */
{
    if (!rw_impl)  ABORT("nrtucode_context_create", "rw_impl");   /* fatal */
    if (!ctx_out)  ABORT("nrtucode_context_create", "ctx_out");   /* fatal */
    *ctx_out = NULL;
    int rc = 4;                                  /* ERR_BAD_API_VERSION */
    if (api_version == 3) {                       /* the only accepted ABI level */
        nrtucode_context_t* c = malloc(0x28);
        rc = 5;                                   /* ERR_OOM (pre-set) */
        if (c) {
            c->rw_impl        = rw_impl;          /* +0x00 */
            c->memhandle_impl = &plat_memhandle_dummy; /* +0x08 default stub */
            c->userdata       = NULL;             /* +0x10 */
            char* s = malloc(0x200);              /* 512-byte log scratch */
            if (s) {
                c->log_scratch_buf  = s;          /* +0x20 */
                c->log_scratch_size = 0x200;      /* +0x18 */
                *ctx_out = c;
                rc = 0;
                nrtucode_objcount_increment();    /* lock inc [objcount] */
            } else {
                free(c);                          /* rc stays 5 (OOM) */
            }
        }
    }
    return rc;       /* 0 ok, 4 bad api_version, 5 OOM */
}
```

`[OBSERVED]` `[addr:0x9b0290]`. The version field is **checked** (`cmp $0x3,%edi`
at `0x9b02b6`) but **never stored** — `api_version == 3` is the only valid level
(see `nrtucode_get_api_level @0x308740`).

> **NOTE** — The IDA decompile renders the increment as `sub_309C70(512)`. The
> `512` is a decompiler artifact (`%eax`/`%rdi` are zeroed immediately before the
> call); `nrtucode_objcount_increment @0x9b17a0` is just `lock incl [objcount]; ret`.

### (2) `nrtucode_context_set_memhandle_impl` — internal `0x9b0390` / stripped `0x308870`

```c
void nrtucode_context_set_memhandle_impl(nrtucode_context_t* ctx,   /* "ctx" */
                                         memhandle_vtbl* memhandle_impl) /* "memhandle_impl" */
{
    if (!ctx) ABORT("…", "ctx");
    ctx->memhandle_impl = memhandle_impl;   /* mov %rsi,0x8(%rdi) @0x9b0395 */
}
```

Replaces the dummy stub with a real device-memory platform table. **No
validation** of the table; single store, void return. `[OBSERVED]`
`[addr:0x9b0395]`.

### (3) `nrtucode_context_destroy` — internal `0x9b03d0` / stripped `0x3088b0`

```c
int nrtucode_context_destroy(nrtucode_context_t* ctx) {
    if (!ctx) ABORT("…", "ctx");
    free(ctx->log_scratch_buf);    /* free([ctx+0x20])  @0x9b03d6/e0 */
    free(ctx);                     /* free(ctx)          @0x9b03e8    */
    nrtucode_objcount_decrement(); /* lock dec [objcount] @0x9b03ef   */
    return 0;                      /* always 0 */
}
```

`[OBSERVED]` `[addr:0x9b03d6]`.

> **GOTCHA** — Destroy releases **only** the log scratch (`+0x20`) and the struct
> itself. It does **not** free `rw_impl` (`+0x00`, caller-owned) and does **not**
> touch `memhandle_impl` (`+0x08`, a static/caller-owned platform table). A real
> `memhandle_impl` installed via `set_memhandle_impl` is the caller's to release.

### (4) `nrtucode_context_get_userdata` — internal `0x9b0430` / stripped `0x308910`

```c
void* nrtucode_context_get_userdata(nrtucode_context_t* ctx) {
    if (!ctx) ABORT("…", "ctx");
    return ctx->userdata;          /* mov 0x10(%rdi),%rax @0x9b0435 */
}
```

`[OBSERVED]` `[addr:0x9b0435]`.

### (5) `nrtucode_context_set_userdata` — internal `0x9b0470` / stripped `0x308950`

```c
void nrtucode_context_set_userdata(nrtucode_context_t* ctx, void* userdata) { /* "userdata" */
    if (!ctx) ABORT("…", "ctx");
    ctx->userdata = userdata;      /* mov %rsi,0x10(%rdi) @0x9b0475 */
}
```

`[OBSERVED]` `[addr:0x9b0475]`. Get/set touch exactly `+0x10` and nothing else.

---

## 6. Object-count bookkeeping (`nrtucode_objcount.c`)

`create` increments and `destroy` decrements a single global `int` counter,
`objcount` (internal `.bss 0x9bb564`), via two one-instruction helpers:

```asm
nrtucode_objcount_increment  0x9b17a0:  lock incl 0x9dbd(%rip)   # objcount
nrtucode_objcount_decrement  0x9b17b0:  lock decl 0x9dad(%rip)   # objcount
```

`[OBSERVED]` `[addr:0x9b17a0]`. The `lock` prefix makes the count atomic across
threads; companion `nrtucode_objcount_setup`/`_check` (`0x9b1780`/`0x9b17c0`)
implement a leak assertion at teardown. This is the live-object accounting the
runtime uses to detect a context that was created but never destroyed.

---

## 7. Adversarial self-verification

The five strongest claims, each re-challenged against the binary:

| Claim | Re-challenge | Result |
| --- | --- | --- |
| Struct size = `0x28` | `mov $0x28,%edi; call malloc` is the sole struct alloc at `0x9b02c1` | **HELD** |
| Members `+0x00/+0x08/+0x10/+0x18/+0x20` | every store present in `create` (`(%rax)`, `0x8/0x10/0x18/0x20(%r15)`) | **HELD** |
| `memhandle_impl` = 5-slot table, slot 4 NULL in dummy | exactly 4 `R_X86_64_RELATIVE` at `0x9b8cf0..d08`, **none** at `0x9b8d10` | **HELD** |
| `destroy` frees `+0x20` then `ctx` | `mov 0x20(%rdi),%rax; free; free(ctx)` at `0x9b03d6` | **HELD** |
| `get/set_userdata` touch only `+0x10` | `mov 0x10(%rdi),%rax` / `mov %rsi,0x10(%rdi)` | **HELD** |

No claim failed re-challenge; no correction beyond the two embedded above
(`create` address; 4-slot to 5-slot memhandle table).

---

## 8. Evidence index

| Probe | Finding |
| --- | --- |
| `nm -D libnrtucode.so` | 5 exports `0x308770/870/8b0/910/950`; `get_api_level 0x308740` |
| `nm libnrtucode_internal.so` | context fns `0x9b0290…0540`; `plat_memhandle_dummy 0x9b8cf0` |
| `readelf -rW` (dummy table) | 4 `R_X86_64_RELATIVE` → `9b1820/9b1850/9b1860/9b1870`; no reloc at `9b8d10` |
| `objdump -d 0x9b0290` (create) | `malloc 0x28`; stores at `+0x00/08/10/18/20`; `cmp $3,%edi`; `malloc 0x200` |
| `objdump -d 0x9b04b0` (vlog) | `call *0x18(rw_vt)` gate; scratch `+0x18`/`+0x20`; `call *0x10(rw_vt)` emit |
| `objdump -d 0x9b1820` (dummy) | malloc logs+`ret 8`; free bare `ret`; read/write `mov $8,%eax; ret` |
| `readelf -SW` | section deltas `.rodata 0` / `.text 0x1000` / `.data.rel.ro 0x2000` / `.data 0x3000` |
| `.rodata` strings | `0x4a9f` memhandle-not-attached; `0x5469` null-guard fmt; `0x5211` create name; `0x46b0` rw_impl; `0x5394` ctx_out; `__FILE__` `nrtucode_context.c` / `nrtucode_objcount.c` / `nrtucode_platform_memhandle_dummy.c` |
