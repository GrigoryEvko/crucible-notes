# nrtucode Logging + Leak-Tracking Allocator

This page reconstructs the **host-runtime logging subsystem** and the
**object leak / double-free detector** of the nrtucode codec, as shipped in
the two host x86-64 libraries:

| Lib | Size | BuildID | Symbols | Role |
| --- | --- | --- | --- | --- |
| `libnrtucode_internal.so` | `10,276,288 B` | `9cbf78c6…585fd` | **not stripped** | the symbol twin (names + both leak branches) |
| `libnrtucode.so` | `3,208,440 B` | `abf4e088…50f38` | stripped | the production ship lib |

Every address below is an **internal-twin VMA** unless tagged `STRIPPED`.
The page default is `[HIGH · OBSERVED]`; claims that depart from it carry an
explicit tag.
Section model of the internal twin (`readelf -SW`):

```text
.rodata        VMA 0x000046b0 == fileoff 0x046b0   (Δ = 0  → a .rodata RVA reads directly with `dd skip=RVA`)
.text          VMA 0x009b01a0 == fileoff 0x9af1a0  (Δ = 0x1000)
.data.rel.ro   VMA 0x009b8cf0 == fileoff 0x9b6cf0  (Δ = 0x2000)
.bss           VMA 0x009bb560  NOBITS  size 0x10   (Δ = 0x3000)
```

> **NOTE.** The whole `.bss` is **16 bytes** (`{completed.0, objcount, xtlib_globals}`).
> That single fact is the strongest structural proof that the "leak-tracking
> allocator" is **not** a per-allocation tracking table — there is nowhere to
> put one.

The codec layer is two unrelated subsystems that the survey names together:
a **logging path** (a formatter/sink plus a device-log-ring drain) and a
**leak detector** (a single atomic counter, `objcount`). Both are decoded in
full here.

> **CORRECTION (premise).** There is **no** `malloc`/`free`-wrapping debug
> allocator with a ptr/size/callsite/tag table. The "leak tracker" is a single
> global `int` (`objcount`) of *live high-level handles* with an `atexit` hook.
> No table, no hash map, no callsite capture. Detailed below in §7.

---

## 1. The log API surface

Seven entry points across three source units (`nrtucode_context.c`,
`nrtucode_core.c`, `prelink_log.c`). Five of the seven are **exported** in the
stripped ship lib — verified `nm -D libnrtucode.so`:

```text
$ nm -D libnrtucode.so | rg 'enable_logs|disable_logs|set_max_loglevel|print_logs'
00000000003090a0 T nrtucode_core_enable_logs
00000000003090b0 T nrtucode_core_enable_logs_with_size_hint
0000000000308d10 T nrtucode_core_disable_logs
0000000000309250 T nrtucode_core_set_max_loglevel
00000000003092e0 T nrtucode_core_print_logs
```

| Internal VMA | Stripped VMA | Symbol | Kind | Layer |
| --- | --- | --- | --- | --- |
| `0x9b04b0` | `sub_308990` (local) | `nrtucode_context_vlog` | static `t` | formatter/sink |
| `0x9b0540` | `sub_308a20` (local) | `nrtucode_context_log` | static `t` | formatter/sink (variadic front-end) |
| `0x9b0bc0` | `0x3090a0` | `nrtucode_core_enable_logs` | export `T` | ring lifecycle |
| `0x9b0bd0` | `0x3090b0` | `nrtucode_core_enable_logs_with_size_hint` | export `T` | ring lifecycle |
| `0x9b0830` | `0x308d10` | `nrtucode_core_disable_logs` | export `T` | ring lifecycle |
| `0x9b0d70` | `0x309250` | `nrtucode_core_set_max_loglevel` | export `T` | level register |
| `0x9b0e00` | `0x3092e0` | `nrtucode_core_print_logs` | export `T` | **ring drain** |

The two `context_*` formatters are internal helpers, not exported. The five
`core_*` methods are the public log-control API.

> **NOTE.** The library does **not** itself write to `stdout` or a file. It
> *formats* a line and pushes it to a caller-supplied sink (a vtable callback).
> Whatever transport carries that line beyond the callback (e.g. the host
> stdout pipe) belongs to the embedding NRT runtime, out of this blob's scope.

---

## 2. The context-layer formatter + the `rw_impl` sink + the gate

The format/emit path hangs off the `nrtucode_context_t` (the
[nrtucode_context](nrtucode-context.md) handle). `ctx[+0x00]` is the
`rw_impl` object whose first bytes are a function-pointer table:

```c
/* The rw_impl vtable (ctx[0] points at an object whose [0..] IS this table). */
struct rw_impl_vtbl {
    int  (*read )(void* h, uint64_t addr, uint32_t len, void* out);       /* +0x00 small-IO / CSR read  */
    int  (*write)(void* h, uint64_t addr, uint32_t len, const void* in);  /* +0x08 small-IO / CSR write */
    void (*log_emit )(void* ctx, uint32_t sev, const char* msg, size_t len); /* +0x10 THE LOG SINK   */
    bool (*log_enabled)(void* h);                                         /* +0x18 THE LOG GATE        */
    /* +0x20 device_addr (used by enable_logs, see §5)                                                 */
};
```

### 2a. `nrtucode_context_vlog` @ `0x9b04b0`

Disassembled byte-exact:

```text
9b04c3: 48 8b 07          mov  rax,[rdi]          ; rax = ctx->rw_impl  (ctx[+0x00])
9b04c6: ff 50 18          call [rax+0x18]         ; *** GATE: log_enabled(rw_impl) -> al ***
9b04c9: 84 c0 ; 74 58     test al,al ; je 9b0525  ; if disabled -> return 0, emit nothing
9b04cd: 49 8b 76 18       mov  rsi,[r14+0x18]     ; rsi = log_scratch_size  (ctx[+0x18])
9b04d1: 49 8b 7e 20       mov  rdi,[r14+0x20]     ; rdi = log_scratch_buf   (ctx[+0x20])
9b04d5: mov rdx,r12 ; mov rcx,r15 ; call vsnprintf@plt
9b04e0: 4c 63 f8          movsxd r15,eax          ; r15 = formatted length
9b04e3: 49 8b 56 20       mov  rdx,[r14+0x20]     ; rdx = scratch_buf (reloaded -> emit arg2)
9b04e7: 41 81 ff ff 3f .. cmp  r15d,0x3fff ; ja   ; *** HARD CAP 0x3fff: do not grow past it ***
9b04f0: 4d 39 7e 18       cmp  [r14+0x18],r15 ; jae; if scratch already big enough, skip the grow
9b04fc: e8 .. realloc@plt(buf,r15)               ; else grow; on success [+0x20]=buf,[+0x18]=r15
9b0517: 49 8b 06          mov  rax,[r14]          ; rax = ctx->rw_impl (again)
9b051a: 4c 89 f7          mov  rdi,r14            ; *** arg0 = ctx (NOT rw_impl) ***
9b051d: 89 de            mov  esi,ebx             ; arg1 = severity
9b051f: 4c 89 f9          mov  rcx,r15            ; arg3 = msg_len     (rdx still = scratch buf = arg2)
9b0522: ff 50 10          call [rax+0x10]         ; *** SINK: log_emit(ctx, sev, msg, len) ***
9b0525: 31 c0 ; c3        xor eax,eax ; ret       ; returns 0
```

Reconstructed:

```c
/* @0x9b04b0  nrtucode_context_vlog(ctx, severity, fmt, va_list) */
void nrtucode_context_vlog(nrtucode_context_t* ctx, uint32_t sev,
                           const char* fmt, va_list ap) {
    rw_impl_vtbl* vt = *(rw_impl_vtbl**)ctx->rw_impl;       /* ctx[+0x00] */
    if (!vt->log_enabled(ctx->rw_impl)) return;            /* THE GATE — emit nothing if disabled */
    int n = vsnprintf(ctx->log_scratch_buf,                /* ctx[+0x20] */
                      ctx->log_scratch_size, fmt, ap);     /* ctx[+0x18] */
    if ((uint32_t)n <= 0x3fff && ctx->log_scratch_size < (size_t)n) {
        char* g = realloc(ctx->log_scratch_buf, n);        /* grow scratch, cap 0x3fff */
        if (g) { ctx->log_scratch_buf = g; ctx->log_scratch_size = n; }  /* on OOM keep old buf */
    }
    vt->log_emit(ctx, sev, ctx->log_scratch_buf, n);       /* THE SINK — arg0 is ctx, not rw_impl */
}
```

> **GOTCHA — the sink's arg0 is `ctx`, indexing `rw_impl`'s table.** The
> `call [rax+0x10]` indexes the *`rw_impl`* table (`rax = ctx[0]`), but the
> first argument it passes is `r14 = ctx`, **not** `rw_impl`. Since
> `ctx[0] == rw_impl`, the callback can reach either, but a reimplementation
> must pass the **context handle** to `log_emit`, not the rw_impl handle.

> **QUIRK — the scratch grows but never shrinks, and is gated by a `0x3fff`
> cap.** A line longer than `0x3fff` bytes is `vsnprintf`-truncated to the
> current buffer and **never** triggers a grow (the `ja` at `0x9b04ee` skips
> the realloc). The scratch is a 512-byte `malloc` at `context_create`
> (`0x9b02f3 malloc(0x200)`) that ratchets upward up to the cap.

### 2b. `nrtucode_context_log` @ `0x9b0540` — the variadic front-end

`context_log(ctx, sev, fmt, ...)` assembles an on-stack SysV `va_list` (GP
register-save area, the `movabs rax,0x3000000018` header at `0x9b05b5`
encoding `{gp_offset=0x18, fp_offset=0x30}`) and then runs an **inlined copy**
of the exact same gate → `vsnprintf` → grow → `log_emit` pipeline as `2a`
(`0x9b05c3..0x9b0622`). It is the entry point every internal diagnostic and
`print_logs` (§5) calls.

---

## 3. The log-level model (the "severity" values)

The severity argument is a **`uint32`**. A caller census of every
`context_log` site (the `mov esi,0xN` immediately before each
`call 0x9b0540`) shows the library's own diagnostics use exactly **two**
literal levels:

| Level | Meaning | Used by (string@RVA) |
| --- | --- | --- |
| `1` | **ERROR** | every API-misuse / `boot_state` / OOM / `"invalid log buffer tail"` (`0x5400`) message; `prelink_error_log_callback` forces `1` (§10) |
| `4` | **INFO / DEBUG** | lifecycle: `"Initialized %s"` (`0x52fa`), `"%s renamed %s"` (`0x51be`), `"%s logs enabled; …"` (`0x5229`), destroy traces |

Device-originated lines (§5) carry their **own** severity byte, which can be
any value `0..255`; `print_logs` passes it through verbatim
(`movsx esi,r12b` @ `0x9b0f55`). So the on-wire numeric level space is
**byte-wide**; the library uses `{1=ERROR, 4=INFO/DEBUG}` only for its *own*
messages.

`set_max_loglevel` (§5) is fed by a probe (§5a) that descends `4 → 1` calling
`log_enabled` and stops at the lowest still-enabled level — so the scale runs
**ERROR-low … DEBUG-high**.

> **NOTE — the gate is a callback, not an env var or a level compare.**
> Whether a line is emitted at all is decided by the embedder's
> `log_enabled()` (rw_impl `+0x18`); the severity is passed through for the
> embedder to filter. There is **no** host-side `getenv` on the logging path
> (see §9). `[HIGH · OBSERVED for the two literals + the device pass-through;
> MED · INFERRED for the full canonical name↔number map beyond `{1,4}` — the
> scale *direction* is observed via the `4→1` descent probe, but `2=WARN`/`3=?`
> names are not spelled out in either blob.]`

---

## 4. The `nrtucode_core_t` log state + the device control block

The core handle is `malloc(0x70)` (`0x9b067a`). Its log-relevant fields
(cross-anchored to the core struct survey):

| Off | Field | Meaning |
| --- | --- | --- |
| `+0x00` | `context*` | the `nrtucode_context_t` (the rw_impl / log handle) |
| `+0x20` | `dram_base` | device DRAM base of the per-core **control block** (create arg4) |
| `+0x30` | `boot_state` | `0=NOT_BOOTED`, `1=BOOTED_LEGACY`. **Every** core log method gates on `cmp DWORD[core+0x30],1`; mismatch → log `0x47a7` + return 8 |
| `+0x38` | `log_memhandle` | device handle of the log **ring** (`0` = logs disabled) |
| `+0x40` | `log_buf_size` | host copy of the ring size (rounded up to a multiple of `0x20`) |
| `+0x44` | `log_read_cursor` | host-side **tail**: bytes already drained |
| `+0x48` | `friendly_name` | `char[0x21]`, the **host log prefix** (the `%s` in `print_logs`) |

The `friendly_name` default is written by `core_create`:
`snprintf(name, 0x21, "nrtucode_core_t@%p", core)` (fmt `0x50ad`). The NRT
runtime renames it via `set_friendly_name` (`"%s renamed %s"`, `0x51be`) to
identify the engine.

The **device control block** at `dram_base`, reconstructed from the
`rw_impl->read`/`write` offsets used by enable/disable/set_max/print:

| Off from `dram_base` | Width | Field | Written / read by |
| --- | --- | --- | --- |
| `+0x04` | `u32` | ring **size** | `enable_logs` writes; `disable_logs` zeroes |
| `+0x08` | `u64` | ring **device addr** | `enable_logs` writes; `disable_logs` zeroes |
| `+0x10` | `u32` | ring **head** (producer cursor) | `enable_logs` zeroes; **`print_logs` reads** |
| `+0x14` | `u32` | **max log level** | `set_max_loglevel` writes; `disable_logs` reads |

A 3-field SPSC-ring descriptor (`+0x04/+0x08/+0x10`) plus a level register
(`+0x14`), all in the per-core device control block. The offsets/widths are
`[HIGH · OBSERVED]`; the field *names* are `[MED · INFERRED]` from the access
pattern and the `"bufsize=%u max level=%u"` string.

---

## 5. The device-log-ring lifecycle (enable / set\_max / disable / drain)

This is the **host side of the `'S:'`/`'P%i:'` firmware-log channel**. The
device firmware is the single **producer** that writes
`[severity:u8][C-string\0]` records into a device-memory ring and advances
the `head`; `print_logs` is the single **consumer**. The relationship to the
device producer and the `pool_stdio` ring is reconciled in §6.

### 5a. `enable_logs` / `enable_logs_with_size_hint` @ `0x9b0bc0` / `0x9b0bd0`

`enable_logs` is a one-liner: `mov esi,0x20000 ; jmp enable_logs_with_size_hint`
→ the **default ring size is `0x20000` (128 KiB)**. The size-hint variant
(byte-exact `0x9b0c40..0x9b0d20`):

```c
/* @0x9b0bd0  nrtucode_core_enable_logs_with_size_hint(core, size) */
int enable_logs(nrtucode_core_t* c, uint32_t size) {
    if (c->boot_state != BOOTED_LEGACY) { log(c, 1, "…", "BOOTED_LEGACY"); return 8; }  /* 0x47a7 */
    int rc = disable_logs(c); if (rc) return rc;                /* tear down any existing ring first */
    size = (size + 0x1f) & ~0x1fu;                              /* round up to a multiple of 0x20 */
    rw_impl_vtbl* mh = memh(c);                                 /* ctx[+0x08] = memhandle vtable */
    mh->device_malloc(ctx(c), size, &c->log_memhandle);        /* call [rax+0x00]  -> core[+0x38] */
    c->log_buf_size = size;                                     /* core[+0x40] = size  (9b0c59) */
    c->log_read_cursor = 0;                                     /* core[+0x44] = 0     (9b0c5c) */
    uint64_t devaddr = mh->device_addr(ctx(c), c->log_memhandle); /* call [rax+0x20]   (9b0c6e) */
    rwt(c)->write(rwh(c), c->dram_base + 0x08, 8, &devaddr);    /* descriptor: ring addr (9b0c96) */
    rwt(c)->write(rwh(c), c->dram_base + 0x04, 4, &size);       /* descriptor: ring size (9b0cb9) */
    uint32_t zero = 0;
    rwt(c)->write(rwh(c), c->dram_base + 0x10, 4, &zero);       /* descriptor: head = 0  (9b0cdc) */
    int lvl = 4;                                                /* level probe (9b0ce7): descend 4..1 */
    while (lvl > 0 && !rwt(c)->log_enabled(rwh(c))) lvl--;      /* call [rax+0x18]; find lowest enabled */
    nrtucode_core_set_max_loglevel(c, lvl);                     /* (9b0d07) */
    log(c, 4, "%s logs enabled; bufsize=%u max level=%u", ...); /* 0x5229 */
    return 0;
}
```

> **NOTE — two disjoint allocators in one function.** The **buffer** is
> `device_malloc`'d through the *memhandle* vtable (`ctx[+0x08]`, slot `+0x00`);
> the **descriptor** is published through the *rw_impl* vtable (`ctx[+0x00]`,
> slot `+0x08`) as three CSR writes. The host heap is never touched here.

### 5b. `set_max_loglevel` @ `0x9b0d70`

```text
9b0d7d: cmp [rdi+0x30],0x1 ; jne -> log 0x47a7 + ret 8
9b0d8d: add rsi,0x14       ; target = dram_base + 0x14
9b0d9b: call [rax+0x8]     ; rw_impl->write(dram_base+0x14, 4, &level)
```

Writes the device max level. The **device firmware** is what compares each
line's severity byte against this register and decides whether to emit it into
the ring.

### 5c. `disable_logs` @ `0x9b0830`

Gates on `boot_state==1`; if `core[+0x38]==0` (no ring) it only reads back
`dram_base+0x14` and returns. Otherwise it **clears the descriptor**
(`write 0` to `dram_base+0x08` 8B and `+0x04` 4B), **`device_free`s** the ring
(memhandle slot `+0x08`, `call [r8+0x8]` on `core[+0x38]`), and zeroes
`core[+0x38]`.

### 5d. `print_logs` @ `0x9b0e00` — **the ring drain**

Byte-exact, the host consumer of the firmware log ring:

```c
/* @0x9b0e00  nrtucode_core_print_logs(core)  — the ring drain */
int nrtucode_core_print_logs(nrtucode_core_t* c) {
    if (c->boot_state != BOOTED_LEGACY) { log(c,1,"…","BOOTED_LEGACY"); return 8; }   /* 9b0e1a / 0x47a7 */
    if (!c->log_memhandle)                                                            /* 9b0e20 */
        { log(c, 1, "…%s: Logs must be enabled before printing them"); return 8; }    /* 0x4ec3 */
    uint32_t head;
    rwt(c)->read(rwh(c), c->dram_base + 0x10, 4, &head);   /* device producer head — rw_impl read +0x00 (9b0e3f) */
    uint32_t tail = c->log_read_cursor;                    /* core[+0x44] (9b0e4e) */
    if (head == tail) return 0;                            /* nothing new (9b0e54) */
    if (head > c->log_buf_size)                            /* corruption guard (9b0e59) */
        { log(c, 1, "%s: …invalid log buffer tail index %u", head); return 6; }       /* 0x5400 */
    uint32_t len = head - tail;
    char* buf = malloc(len); if (!buf) return 5;           /* HOST staging buffer (9b0ee3) */
    int rc = memh(c)->read(ctx(c), c->log_memhandle, tail, len, buf); /* memhandle read +0x10 (9b0f08) */
    if (rc) { free(buf); return rc; }
    for (char* p = buf; p < buf + len; ) {                 /* parse loop 9b0f40..9b0fa3 */
        uint8_t sev = (uint8_t)*p;                         /* record: [sev:u8][C-string\0] (movzx 9b0f1f) */
        size_t  L   = strnlen(p + 1, (buf + len) - (p + 1));            /* (9b0f49) */
        nrtucode_context_log(ctx(c), sev, "%s: %.*s",      /* fmt 0x5043, prefix = friendly_name */
                             c->friendly_name, (int)(L - 1), p + 1);    /* arg = strlen-1 (lea r8d,[rax-1]) */
        p += 1 + L; while (p < buf + len && *p == 0) p++;  /* skip trailing NUL run to next record */
    }
    c->log_read_cursor += len;                             /* advance tail (9b0fa5) */
    free(buf);
    return 0;
}
```

> **The wire format, read off the loop.** Device records are
> `[severity:u8][NUL-terminated C-string]` packed back-to-back, with optional
> trailing NUL padding between records (the inner `*p==0` skip loop at
> `0x9b0f90`). The host re-emits each as `"<friendly_name>: <message>"` at the
> record's own severity. The `head` read uses the **small-IO `rw_impl->read`**
> (slot `+0x00`); the **body** uses the **bulk `memhandle->read`**
> (slot `+0x10`) — two different vtables.

---

## 6. Reconciliation with the device producer (`'S:'`/`'P%i:'` + `pool_stdio`)

The
[SEQ Error Handler](../firmware/seq/error-handler.md) and
[File-IO Manager](../firmware/kernels/file-io-manager.md) pages establish the
**device** side of this channel. The reconciliation, made explicit:

* **The ring `print_logs` drains == the device `'S:'`/`'P%i:'` `pool_stdio`
  ring.** The device-side SEQ logger at IRAM `0x18b84` is a newlib
  `vfprintf`-style logger (it builds a `FILE*` at DRAM `0x84d28`) that writes
  `[severity:u8][C-string\0]` records into a device-memory SPSC ring, advancing
  a producer `head`. That is exactly the record format and the descriptor
  (`base/size/head`) that `enable_logs` (§5a) provisions at `core[+0x38]` and
  `print_logs` (§5d) parses. The file-io-manager page documents this on its
  device half and **forward-references this page** as the host consumer.
  `[Host-consumer format + friendly_name re-emit: HIGH · OBSERVED. That the
  drained ring is the *identical* ring the `0x18b84` logger feeds: MED —
  the on-device head-advance is SEQ-firmware scope, not re-derived in this
  blob.]`

* **The `'S:'`/`'P%i:'` prefix is a DEVICE-firmware string, not a host one.**
  It is baked into the *firmware* `printf` format (`'S: '` = SEQ line prefix,
  `'P%i: '` = POOL core *i*), and it travels **inside** the `%.*s` body of
  `print_logs`. The host adds its *own* prefix on top — the per-core
  `friendly_name`. A fully-rendered host line is therefore:

  ```text
  "<friendly_name>: S: ErrorHandler : Bad Opcode(0x..)"
       \________ host prefix ________/  \__ device 'S:'-prefixed message (rides in %.*s) __/
  ```

  There is **no** `'S:'`/`'P%i:'` literal anywhere in `libnrtucode*.so`
  (verified — the only `'S:'` byte-matches in the host lib are coincidental
  binary data).

* **Two distinct device→host text channels.** The file-io-manager's own
  256-byte-slot DRAM ring (a raw `write(fd, buf, len)` byte pipe for a loaded
  kernel's `stdout`/`stderr`, **no** severity byte, OVERWRITE wrap) is a
  *separate* channel that this host library does **not** drain. `print_logs`
  handles only the `[sev][cstr]` `'S:'`/`'P%i:'` log ring; the file-io-manager
  page's §6 CORRECTION distinguishes the two.

* **Severity is two distinct axes.** The SEQ error handler's engine-internal
  error severity (recoverable vs fatal — see
  [SEQ Error Handler](../firmware/seq/error-handler.md)) is a *different* axis
  from this **log** severity byte. The device firmware gates ring emission by
  comparing each record's log-severity byte against the `dram_base+0x14`
  max-level that `set_max_loglevel` wrote; the host then passes the byte
  through to `log_emit`. `[MED — the two severity spaces are distinct; no blob
  spells out a correspondence between them.]`

* **The error-notify ring is a different channel.** The MSI-X error
  notification ring referenced by the SEQ handler is *not* this log ring; it is
  the NRT runtime's concern. This library handles only the human-readable log
  path. `[MED — consistent with the SEQ page's multi-channel model.]`

---

## 7. The object leak / double-free detector — `nrtucode_objcount`

This is the "leak-tracking allocator." It is **a single atomic `int`** of live
high-level handles plus an `atexit` reporter — four tiny functions and one
`.bss` word, all in `nrtucode_objcount.c`.

> The atomic itself (the `lock incl`/`lock decl` sites, the `.bss int`, the
> two diagnostic strings) is the subject of
> [Concurrency Primitives](concurrency-primitives.md) §3. This page documents
> the **leak-tracking + double-free-check semantics** — what the detector does
> at create/destroy and at teardown — and cites that page for the concurrency
> fact. The anchors below are the exact same ones it pins.

### 7a. The four functions + the counter

| Symbol | VMA | Body |
| --- | --- | --- |
| `nrtucode_objcount_setup` | `0x9b1780` | `.init_array[1]` ctor — `xchg %eax,objcount` (zero) + `jmp atexit(check)` |
| `nrtucode_objcount_increment` | `0x9b17a0` | `lock incl objcount(%rip)` — the **create** hook (see [Concurrency Primitives](concurrency-primitives.md) §3) |
| `nrtucode_objcount_decrement` | `0x9b17b0` | `lock decl objcount(%rip)` — the **destroy** hook (same page) |
| `nrtucode_objcount_check` | `0x9b17c0` | the `atexit` diagnostic (§7c) |
| `objcount` | `0x9bb564` | `.bss int32` (STRIPPED `0x311c74`) — the live-handle counter |

`setup` runs from `.init_array[1]` at library load — verified
`readelf -rW`: the `R_X86_64_RELATIVE` at `0x9b8ce0` resolves to `0x9b1780`.
Its body is `xchg %eax,objcount` (an *implicitly* atomic zeroing store on x86)
followed by `jmp atexit`. No explicit caller.

### 7b. Create/destroy semantics — who inc/dec the counter

Exactly **three object classes**, in **balanced create/destroy pairs**, and
nothing else in the binary touches inc/dec (confirmed by xref of
`0x9b17a0`/`0x9b17b0`):

| Object class | CREATE → `inc` | DESTROY → `dec` |
| --- | --- | --- |
| context | `nrtucode_context_create` (inc @ `0x9b0311`) | `nrtucode_context_destroy` (dec @ `0x9b03ef`) |
| core | `nrtucode_core_create` (inc @ `0x9b06ed`) | `nrtucode_core_destroy` (dec @ `0x9b07ee`) |
| ll (loadable library) | `nrtucode_ll_create` (inc @ `0x9b1c23`) | `nrtucode_ll_destroy` (dec @ `0x9b1de8`) |

> **The increment/decrement discipline (the detector's contract):**
> * **`inc` is post-success.** `context_create` `malloc`s the ctx (`0x28`)
>   **and** its scratch buffer (`0x200`); it stores `*ctx_out` and calls `inc`
>   **only when both succeed**. On the inner `malloc` failing it frees the
>   outer block and returns error **without** `inc` — so a failed create
>   contributes **no** spurious count.
> * **`dec` is at-free.** `*_destroy` validates the handle, frees the block(s),
>   **then** `dec`s. A destroy of an already-freed handle would `dec` a second
>   time, driving `objcount` negative — which is exactly how the **double-free**
>   case (§7c) is detected at teardown.
> * **`opset_create` does NOT inc** (verified — no call to `0x9b17a0`). Opsets,
>   kernels, and ll sub-buffers are **not** counted. The counter is a **net
>   live-handle** count of `{context, core, ll}` only, **not** a heap-block
>   count.

### 7c. The teardown report — `nrtucode_objcount_check` @ `0x9b17c0`

The `atexit` hook. **Internal twin** (full, `0x60` bytes), byte-exact:

```c
/* @0x9b17c0  nrtucode_objcount_check  (atexit hook, internal twin) */
void nrtucode_objcount_check(void) {
    if (objcount > 0)                                   /* 9b17c9: jg -> leak branch */
        fprintf(stderr,                                 /* str @.rodata 0x47cb */
            "nrtucode: nonfatal internal error: %i object(s) leaked, improper "
            "teardown of library (did you forget to call nrt_close or "
            "nrtucode_context_destroy?)\n", objcount);
    if (objcount < 0)                                   /* 9b17d3 / 9b17fd: js -> double-free branch */
        fwrite("nrtucode: internal error: object(s) double-freed, improper "
               "teardown of library\n", 0x4f, 1, stderr); /* str @.rodata 0x5271, len 0x4f */
    /* objcount == 0 : silent (the clean case) */
}
```

* **`objcount > 0` ⇒ a leak** — a `create` had no matching `destroy` (a missed
  `nrt_close` / `nrtucode_context_destroy`).
* **`objcount < 0` ⇒ a double-free** — `destroy` ran more often than `create`.
* **`objcount == 0` ⇒ silent**, the clean path.

Both diagnostics are **non-fatal**: no `abort`, no exit-code change, no block —
purely a `stderr` tripwire at process teardown.

---

## 8. Production-vs-debug (internal-twin) split

The two libraries are **not** byte-identical in the leak detector — a concrete
counter-example, at the byte level:

| | Internal twin `0x9b17c0` | Stripped production `0x309c90` |
| --- | --- | --- |
| function size | `0x60` bytes | `0x31` bytes |
| `objcount > 0` (**leak**) branch | **present** (`fprintf`, str `0x47cb`) | **absent** (compiled out) |
| `objcount < 0` (**double-free**) branch | present (`fwrite`, str `0x5271`) | **present** (`fwrite`, str `0x36d7`) |
| `"…object(s) leaked…"` string in `.rodata` | present (`0x47cb`) | **absent** |
| `"…double-freed…"` string in `.rodata` | present (`0x5271`) | present (`0x36d7`) |

Stripped production `objcount_check`, byte-exact:

```text
309c9e: 78 01             js  309ca1          ; ONLY the double-free branch
309ca0: c3                ret                 ; objcount >= 0 -> silent (no leak fprintf)
309ca1: …                 fwrite(<str 0x36d7>, 0x4f, 1, stderr)
```

Verification:

```text
$ strings libnrtucode.so | rg -i 'leaked'           # -> NO hit (production)
$ strings libnrtucode.so | rg -i 'double-freed'     # -> present
nrtucode: internal error: object(s) double-freed, improper teardown of library
```

> **NOTE — what ships where.** The **double-free** check is in both libs (a
> real, dangerous condition). The **leak-count** report is an internal/debug
> extra — the production build drops both the leak branch and its string. The
> `.init_array` ctor + `inc`/`dec` hooks + the double-free check are
> production-resident.

A **second** production-vs-debug axis sits at the *firmware-image* level (§9):
the device-log **verbosity** (whether the firmware emits the verbose `'S:'`
log lines at all) is selected by an environment-driven image flavor, not by a
host compile flag. The host logging API is fully present in **both** host libs.

---

## 9. Host-vs-device allocator split + the env gates

### 9a. Two disjoint allocators, never mixed

* **Host allocator = libc, called directly, no wrapper, no tracking.** Eight
  `malloc` sites (plus 1 `calloc`, 2 `realloc` — the log-scratch grows — and 13
  `free`):

  | Site | Function | Allocation |
  | --- | --- | --- |
  | `0x9b02c6` | `context_create` | `malloc(0x28)` — the `nrtucode_context_t` |
  | `0x9b02f3` | `context_create` | `malloc(0x200)` — the **512-B log scratch** |
  | `0x9b067a` | `core_create` | `malloc(0x70)` — the `nrtucode_core_t` |
  | `0x9b0ee3` | `print_logs` | `malloc(head − tail)` — the **host staging buffer** (freed same fn) |
  | `0x9b1b02` | `ll_create` | `malloc(0x48)` — the ll object |
  | `0x9b1b51`/`0x9b1b62` | `ll_create` | ll sub-buffers |
  | `0x9b24e4` | `opset_create` | `malloc(0x830)` — the opset (not counted, §7b) |

* **Device allocator = the `ctx->memhandle_impl` vtable** (`ctx[+0x08]`):
  `device_malloc(+0x00)`, `device_free(+0x08)`, `read(+0x10)`, `write(+0x18)`,
  `device_addr(+0x20)`. The log **ring** (`core[+0x38]`) is `device_malloc`'d;
  ll device segments likewise. (The platform memhandle table — default
  `plat_memhandle_dummy`, replaced via `set_memhandle_impl`.)

The logging subsystem itself uses exactly **one of each**: the host log
**scratch** (libc) and the device log **ring** (memhandle). `objcount` counts
**host handles only**; the device heaps are accounted on the device side,
invisible to it.

### 9b. The env gates — none of them touch logging

`getenv` is imported and called at **5** sites (`objdump | rg -c
'call.*<getenv@plt>'` → `5`):

| Site (fn) | Env var |
| --- | --- |
| `ll_get_libraries_from_opcodes` `0x9b18c9` | `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE` |
| `ll_create` `0x9b1acb` | `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE` |
| `get_memory_image` `0x9b298e` | `NRTUCODE_MPLUS_ON_MARIANA` (removed flag) |
| `get_memory_image` `0x9b2a02` | `NEURON_UCODE_FLAVOR` |
| `get_ext_isa_internal` `0x9b2b62` | `NEURON_UCODE_FLAVOR` |

> **NOTE.** **None** of these gate the logging path. There is **no** env var
> that turns host logging on/off — that is the `rw_impl->log_enabled()`
> callback (§2/§3). These are feature / firmware-image selectors.

`NEURON_UCODE_FLAVOR` (string `0x52e2`) selects the **firmware image** via a
`strcmp` chain in `get_memory_image`, indexing `image_list @0x9b8d20`:

| `getenv` result | Flavor | Image |
| --- | --- | --- |
| `NULL` (unset) | `1` | default (RELEASE / PERF — quieter) |
| `"debug"` / `"DEBUG"` (`0x4733` / `0x51b8`) | `2` | **DEBUG image** — keeps the `'S:'` formats, emits verbose firmware logs |
| `"test"` / `"TEST"` (`0x503e` / `0x4ddf`) | `3` | TEST image |

So the DEVICE log **verbosity** is image-level, not a host log gate. The
`NRTUCODE_MPLUS_ON_MARIANA` flag is **removed** — if set, `get_memory_image`
emits `"…flag has been removed … transition to … NEURON_RT_DBG_V4_PLUS=0/1 env
var"` (`0x4877`). `[HIGH for the strcmp→flavor→image_list mapping; MED that
flavor 1 is "RELEASE/PERF" by *name* — the `image_list[1]` entry name was not
dereferenced.]`

---

## 10. The prelink / external-lib log helpers

* **`prelink_error_log_callback` @ `0x9b24b0`** — a 4-instruction thunk,
  byte-exact:

  ```text
  9b24b0: mov rcx,rdx ; mov rdx,rsi ; mov esi,0x1 ; jmp 0x9b04b0 <context_vlog>
  ```

  i.e. `context_vlog(ctx, severity=1/ERROR, fmt, va)` with the args shuffled.
  This is the log callback the prelink/relocate loader (`prelink_log.c`)
  registers so its own errors funnel into the same context sink at **ERROR**
  level.

* **`log_error` @ `0x9b60a0`** — a self-contained variadic logger (same
  `va_list` assembly as `context_log`) that calls a callback at `obj[+0x30]`
  with handle `obj[+0x28]` (`mov rax,[rdi+0x30]; mov rdi,[rdi+0x28]; call rax`).
  Structurally identical to the context sink but with the callback stored
  *inline* in the object rather than in a vtable — the generic logger for the
  opset / `external_lib.c` layer. `[HIGH · OBSERVED for the shape; the owning
  object is INFERRED from the `opset_create` neighborhood.]`

---

## 11. `.rodata` string catalog (logging + leak)

All RVAs are `.rodata` file offsets (Δ = 0), byte-confirmed
(`strings -t x` / `dd`):

| RVA | String | Used by |
| --- | --- | --- |
| `0x5043` | `"%s: %.*s"` | `print_logs` (per line) |
| `0x5229` | `"%s logs enabled; bufsize=%u max level=%u"` | `enable_logs` |
| `0x5400` | `"%s: internal error: invalid log buffer tail index %u"` | `print_logs` guard |
| `0x4ec3` | `"nrtucode: invalid API usage in \`%s\`: %s: Logs must be enabled before printing them"` | `print_logs` no-ring |
| `0x47a7` | `"(core)->boot_state == BOOTED_LEGACY"` | all core log methods |
| `0x4e68` | `"nrtucode: invalid API usage: check \`%s\` in \`%s\` failed"` | boot_state check |
| `0x50ad` | `"nrtucode_core_t@%p"` | `core_create` default friendly_name |
| `0x52fa` | `"Initialized %s"` | `core_create` (sev 4) |
| `0x51be` | `"%s renamed %s"` | `set_friendly_name` (sev 4) |
| `0x47cb` | `"nrtucode: nonfatal internal error: %i object(s) leaked, … (did you forget to call nrt_close or nrtucode_context_destroy?)"` | `objcount_check>0` (**INTERNAL ONLY**) |
| `0x5271` | `"nrtucode: internal error: object(s) double-freed, improper teardown of library"` (len `0x4f`) | `objcount_check<0` (**both libs**; STRIPPED `0x36d7`) |
| `0x52e2` | `"NEURON_UCODE_FLAVOR"` | `get_memory_image` getenv |
| `0x4733`/`0x51b8` | `"debug"`/`"DEBUG"` | flavor strcmp |
| `0x503e`/`0x4ddf` | `"test"`/`"TEST"` | flavor strcmp |
| `0x4d15` | `"NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE"` | `ll_create` getenv |
| `0x4fae` | `"NRTUCODE_MPLUS_ON_MARIANA"` | removed-flag getenv |
| `0x4877` | `"…NRTUCODE_MPLUS_ON_MARIANA flag has been removed … transition to … NEURON_RT_DBG_V4_PLUS=0/1 env var"` | removed-flag error |

`__FILE__` tokens present in `.rodata`: `nrtucode_context.c`, `nrtucode_core.c`,
`nrtucode_objcount.c`, `prelink_log.c`, `external_lib.c`.

---

## 12. Confidence ledger

**HIGH · OBSERVED** (direct disassembly / byte read):

* The 7-entry log API census; 5 exported in stripped (`nm -D`).
* `context_vlog`/`context_log`: the `log_enabled` GATE (rw_impl `+0x18`) →
  `vsnprintf` into `ctx[+0x20]` (grow via `realloc`, cap `0x3fff`) → `log_emit`
  SINK (rw_impl `+0x10`, arg0 = ctx) — full register/`va_list` trace.
* `enable_logs` (default `0x20000`) / `with_size_hint`: `device_malloc` the
  ring via memhandle `+0x00` into `core[+0x38]`; round to `0x20`; publish
  descriptor (`+0x08` addr / `+0x04` size / `+0x10` head=0) via `rw_impl->write`;
  the `4→1` level probe → `set_max_loglevel`.
* `disable_logs`: clear descriptor + `device_free` + zero `core[+0x38]`.
* `set_max_loglevel`: `rw_impl->write(dram_base+0x14, level)`.
* `print_logs`: read head (`rw_impl->read dram_base+0x10`); tail = `core[+0x44]`;
  `malloc(head−tail)`; `memhandle->read` body; parse `[sev][\0str]` records;
  emit `"%s: %.*s"`; advance `core[+0x44]`; free.
* The device control-block offsets (`+0x04/+0x08/+0x10/+0x14`) and the record
  wire format from the write/read widths + loop bytes.
* Severity literals `{1=ERROR, 4=INFO/DEBUG}`; device byte passed through.
* `objcount`: ctor (`xchg 0` + `atexit`) as `.init_array[1]` (both libs);
  `lock inc`/`dec`; 3 balanced create/destroy classes; check leak(`>0`) /
  double-free(`<0`); single `.bss int` (whole `.bss` = 16 B).
* **Production split**: stripped check (`0x31` B) = double-free **only**;
  leaked-string **absent** from production `.rodata`; internal check (`0x60` B)
  = both. Byte sizes + string presence verified both ways.
* `NEURON_UCODE_FLAVOR` strcmp → `{unset:1, debug:2, test:3}` → `image_list`;
  5 getenv sites enumerated; **no** getenv on the log path.
* `prelink_error_log_callback` = `context_vlog(., sev=1, .)` thunk;
  `log_error` = generic `{handle@+0x28, callback@+0x30}` logger.

**MED · INFERRED**:

* That the `print_logs` ring is the **identical** ring the device `0x18b84`
  logger feeds (host consumer side is HIGH; the on-device producer head-advance
  is SEQ-firmware scope; the `[sev][cstr]` SPSC format matches).
* The device-log severity byte vs the SEQ error-record severity are **distinct
  axes** (honest reading; no blob spells out a correspondence).
* The full numeric level-name map beyond `{1=ERROR, 4=INFO/DEBUG}` — only the
  scale *direction* (ERROR-low … DEBUG-high) is observed via the `4→1` probe.
* Flavor `1` == "RELEASE/PERF" by **name** (the `image_list[1]` entry name was
  not dereferenced; flavors `2`/`3` demonstrably map to DEBUG/TEST firmware).

**LOW · UNRECOVERED**:

* The `image_list` per-flavor image member identities (image-survey scope).
* The exact owning object of `log_error @0x9b60a0` (opset / external_lib —
  inferred from the neighborhood).

---

## See also

* [Concurrency Primitives](concurrency-primitives.md) — the single `objcount`
  atomic (the `lock incl`/`decl` sites @ `0x9b17a0`/`0x9b17b0`, the `.bss int`
  @ `0x9bb564`); this page reuses those anchors for the leak/double-free
  *semantics*.
* [nrtucode_context](nrtucode-context.md) — the `nrtucode_context_t` that owns
  the `rw_impl` vtable and the log scratch buffer.
* [SEQ Error Handler](../firmware/seq/error-handler.md) — the device producer
  of the `'S:'` error log lines.
* [File-IO Manager](../firmware/kernels/file-io-manager.md) — the device half
  of the `'S:'`/`'P%i:'` `pool_stdio` ring + the separate Q7 kernel-stdio ring.
