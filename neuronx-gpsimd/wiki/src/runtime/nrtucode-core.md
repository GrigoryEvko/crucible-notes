# `nrtucode_core_t` Struct + Introspection / Boot

The `nrtucode_core_t` is the per-engine handle of the **nrtucode** ucode runtime:
the object that pins one booted GPSIMD/NeuronCore image to one host owner. It is a
**`0x70`-byte heap struct** whose first field is a back-pointer to the
[`nrtucode_context_t`](nrtucode-context.md) it threads every device transaction
through. This page reconstructs that struct field-exact, documents its four
introspection accessors (`get_context` / `get_coretype` / `set_friendly_name` /
`on_ucode_booted`), and resolves the **boot-claim magic seam** — the device DRAM
sentinel a freshly-booted image advertises and the claim word a live core stamps
back to take sole ownership.

A senior C++/LLVM reimplementer should be able to re-declare the struct, re-emit
`nrtucode_core_create`/`_destroy`, and reproduce the boot handshake byte-for-byte
from this page alone.

All addresses, offsets, magic constants and string offsets below were read from
the two shipped binaries via static analysis:

- **`libnrtucode_internal.so`** — `10,276,288` bytes, BuildID
  `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd`, **not stripped** (the symbol twin;
  every `symbol@addr` below is its `.symtab` address).
  Path: `neuronx-gpsimd/extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/libnrtucode_internal.so`.
- **`libnrtucode.so`** — the stripped sibling (BuildID `abf4e088…d50f38`); same
  source tree, identical codegen. Its `.dynsym` exports map to the internal-twin
  symbols 1:1 (the stripped `.text` is `0x308b20`-based; the internal twin is
  `0x9b0640`-based). Addresses in this page are the **internal-twin** addresses.
- The host driver **`libnrt.so`** carries the device-side reflection of the same
  routine as `nrtucode_core_on_ucode_booted @0x9b0ab0` (documented in
  [device bring-up](nrtucode-bringup.md) §7).

> **Section deltas (VMA − file offset), `libnrtucode_internal.so`** —
> `.rodata` Δ=0, `.text` Δ=`0x1000`, `.data.rel.ro` Δ=`0x2000`, `.data` Δ=`0x3000`
> (`readelf -SW`). `.rodata`/`.text` addresses listed in this page are VMA; the
> `.rodata` null-guard format string in §7 is cited at its file offset.

> **Address-space hazard (read this first).** The `0x70`-byte block is a **host
> `malloc`**. Almost none of the live device state — the boot magic, the log
> control-block, the DGE mailbox, the PC-bounds window — lives *in* this struct.
> They live in a **device DRAM control block** whose base address is stored at
> `core+0x20` (`dram_base`) and reached through the context's `rw_impl` vtable
> (`ctx+0x00`→vtable; slot `+0x00`=read, slot `+0x08`=write). A host field is a
> slot inside the `0x70` block; a device field is `rw(ctx, core[+0x20]+N, …)`.
> Conflating the two (treating `dram_base+0x28` mailbox as host offset `+0x28`)
> is the single biggest trap. The host struct is §2; the device CB map is §6.

---

## 1. Export table

| Internal `.symtab` | Stripped `.dynsym` | Symbol |
|---|---|---|
| `0x9b0640` | `0x308b20` | `nrtucode_core_create` |
| `0x9b0780` | `0x308c60` | `nrtucode_core_destroy` |
| `0x9b0950` | `0x308e30` | `nrtucode_core_get_context` |
| `0x9b0990` | `0x308e70` | `nrtucode_core_get_userdata` |
| `0x9b09d0` | `0x308eb0` | `nrtucode_core_set_userdata` |
| `0x9b0a10` | `0x308ef0` | `nrtucode_core_get_coretype` |
| `0x9b0a50` | `0x308f30` | `nrtucode_core_set_friendly_name` |
| `0x9b0aa0` | `0x308f80` | `nrtucode_core_get_friendly_name` |
| `0x9b0ab0` | `0x308f90` | `nrtucode_core_on_ucode_booted` |
| `0x9b0540` | `0x308a20` | `nrtucode_context_log` *(internal variadic logger; helper)* |

Addresses verified with `nm libnrtucode_internal.so | rg nrtucode_core_` this
analysis. `[HIGH·OBSERVED]`

---

## 2. The core struct — `0x70` bytes, every byte accounted

The struct has no IDA-typed definition (it is a `malloc(0x70)` blob). Its layout
is recovered from the writers in `nrtucode_core_create` and the readers in the
accessors. The allocation is byte-exact:

```
9b0675:  mov  $0x70,%edi              ; sizeof(nrtucode_core_t) = 0x70
9b067a:  call 9b7b90 <malloc@plt>
```

| off | sz | type | name | writer / proof (`@addr`) | conf |
|---|---|---|---|---|---|
| `0x00` | 8 | `nrtucode_context_t*` | `context` | create `mov %rbx,(%rax)` @`0x9b0684` (rbx = ctx arg). `get_context` returns it: `mov (%rdi),%rax` @`0x9b0955` | HIGH·OBS |
| `0x08` | 8 | `void*` | `userdata` | create init 0: `movq $0,0x8(%rax)` @`0x9b0687`. `set_userdata` `mov %rsi,0x8(%rdi)` @`0x9b09d5`; `get_userdata` `mov 0x8(%rdi),%rax` @`0x9b0995` | HIGH·OBS |
| `0x10` | 4 | `uint32_t` | `coretype` | create `mov %ebp,0x10(%rax)` @`0x9b068f` (create arg2). `get_coretype` `mov 0x10(%rdi),%eax` @`0x9b0a15`. Legality mask **`0x102020204`** = `{2,9,17,25,32}` (§3.5) | HIGH·OBS |
| `0x14` | 4 | — | *(pad)* | never written by create (next write jumps to `0x18`). Alignment pad before the qword at `0x18`. Contents UNKNOWN | HIGH·INF |
| `0x18` | 8 | `uint64_t`/`void*` | `opaque_handle_a` | create `mov %r13,0x18(%rax)` @`0x9b0692` (create arg3). **Write-only** in this library; never read by any host method | HIGH·OBS (write) / LOW·INF (purpose) |
| `0x20` | 8 | `uint64_t` | `dram_base` | create `mov %r12,0x20(%rax)` @`0x9b0696` (create arg4). Device-DRAM base of the per-core control block; the target of every log/dge/pc-bounds/boot rw transaction (§6) | HIGH·OBS |
| `0x28` | 8 | `uint64_t`/`void*` | `opaque_handle_b` | create `mov %r15,0x28(%rax)` @`0x9b069a` (create arg5). **Write-only** here. **NOTE:** *not* the DGE mailbox — the mailbox is `dram_base+0x28`, a different space | HIGH·OBS (write) / LOW·INF (purpose) |
| `0x30` | 4 | `uint32_t` | `boot_state` | create init 0: `movl $0,0x30(%rax)` @`0x9b069e`. `on_ucode_booted` sets `movl $0x1,0x30(%rbx)` @`0x9b0b52` on a successful claim. `0`=NOT_BOOTED, `1`=BOOTED. Every device-touching method gates on `cmpl $0x1,0x30(…)` first | HIGH·OBS |
| `0x34` | 4 | — | *(pad)* | create's next write after `0x30` (u32) jumps to `0x38`; bytes `0x34..0x37` untouched. Alignment pad. Contents UNKNOWN | HIGH·OBS |
| `0x38` | 8 | `void*` | `log_memhandle` | create init 0: `movq $0,0x38(%rax)` @`0x9b06a5`. `enable_logs` allocates via memhandle vtable slot0 and stores the handle here (`lea 0x38(%rbx),%rcx ; call *(%rax)` @`0x9b0c4a`/`0x9b0c53`). `destroy`/`disable_logs` free it and zero it. Non-null ⇒ "logs enabled" | HIGH·OBS |
| `0x40` | 4 | `uint32_t` | `log_buf_size` | `enable_logs` `mov %ebp,0x40(%rbx)` @`0x9b0c59`. Host bookkeeping of the allocated log-buffer size | HIGH·OBS |
| `0x44` | 4 | `uint32_t` | `log_read_cursor` | `enable_logs` `movl $0,0x44(%rbx)` @`0x9b0c5c` (reset). `print_logs` reads it `mov 0x44(%rbx),%r12d` @`0x9b0e4e`, bounds-checks vs `log_buf_size@+0x40` (`cmp 0x40(%rbx),%r14d` @`0x9b0e59`), then advances `add %r14d,0x44(%rbx)` @`0x9b0fa5`. = bytes already consumed | HIGH·OBS |
| `0x48` | `0x21` | `char[0x21]` | `friendly_name` | create `snprintf(&core[0x48], 0x21, "nrtucode_core_t@%p", core)` @`0x9b06cd`. `get_friendly_name` `lea 0x48(%rdi),%rax` @`0x9b0aa0`. `set_friendly_name` `strncpy(core+0x48, src, 0x20)` then forces NUL `movb $0,0x68(%r14)` @`0x9b0a8b`. 32 usable chars + guaranteed NUL at `+0x68`; span `0x48..0x68` incl. (`0x21` B) | HIGH·OBS |
| `0x69` | 7 | — | *(tail pad)* | never written by create/destroy/any method. Trailing padding to round `malloc(0x70)` — last live byte is the name NUL at `0x68`. Contents UNKNOWN | HIGH·INF |

**Byte-coverage checklist `0x00..0x70`:**
`0x00` context · `0x08` userdata · `0x10` coretype · `0x14` PAD · `0x18` opaque_a ·
`0x20` dram_base · `0x28` opaque_b · `0x30` boot_state · `0x34` PAD ·
`0x38` log_memhandle · `0x40` log_buf_size · `0x44` log_read_cursor ·
`0x48..0x68` friendly_name`[0x21]` (NUL @`0x68`) · `0x69..0x6F` tail PAD.
**Every one of the `0x70` bytes is accounted for.** `[HIGH·OBSERVED]`

> **Adversarial pad check.** A scan of the create body `0x9b0640..0x9b06f0`
> shows writes to exactly `(%rax)` `+0x08` `+0x10` `+0x18` `+0x20` `+0x28` `+0x30`
> `+0x38` and the `snprintf` into `+0x48` — and **nothing else**. `+0x14`,
> `+0x34`, `+0x40`, `+0x44`, `+0x69..` are never touched by create (the log
> fields are written later by `enable_logs`, the pads never). `[OBSERVED]`

### Reconstructed C

```c
typedef struct nrtucode_core_t {        /* sizeof = 0x70 */
    nrtucode_context_t *context;        /* +0x00  back-ptr; rw/log handle      */
    void               *userdata;       /* +0x08  caller pointer (init 0)      */
    uint32_t            coretype;       /* +0x10  NRTUCODE_CORE_* {2,9,17,25,32}*/
    /* uint32_t         _pad0;             +0x14  (never written)              */
    uint64_t            opaque_handle_a; /* +0x18  create arg3 (write-only)    */
    uint64_t            dram_base;      /* +0x20  device control-block base    */
    uint64_t            opaque_handle_b; /* +0x28  create arg5 (write-only)    */
    uint32_t            boot_state;     /* +0x30  0=NOT_BOOTED 1=BOOTED        */
    /* uint32_t         _pad1;             +0x34  (never written)              */
    void               *log_memhandle;  /* +0x38  0 when logs disabled         */
    uint32_t            log_buf_size;   /* +0x40                               */
    uint32_t            log_read_cursor;/* +0x44  bytes consumed by print_logs */
    char                friendly_name[0x21]; /* +0x48..+0x68 (forced NUL @0x68)*/
    /* char             _tailpad[7];       +0x69..+0x6F                        */
} nrtucode_core_t;
```

> **QUIRK — `friendly_name` is `0x21` (33), not `0x20`.** `set_friendly_name`
> `strncpy`s at most `0x20` (32) bytes, which does **not** NUL-terminate when the
> source is ≥ 32 chars; the explicit `movb $0,0x68(…)` guarantees termination.
> The default name from create is `snprintf` with size `0x21` — so the field is
> sized for 32 chars + the always-present terminator at `+0x68`.

---

## 3. Introspection accessors

All four are bodies of ~10 instructions. Three null-guard `core` and **abort**
on NULL (a fatal contract, not an error return); `set_friendly_name` does
**not** — see §3.3. The null-guard fault message is the shared format
`"nrtucode: invalid API usage in `\``%s`\``: `\``%s`\`` is null\n"` (`.rodata` file
offset `0x5469`), formatted via `fprintf(stderr, fmt, <fn-name>, "core")` then
`abort()`.

### 3.1 `nrtucode_core_get_context` @`0x9b0950`

```c
/* nrtucode_context_t* nrtucode_core_get_context(nrtucode_core_t *core); */
nrtucode_context_t *nrtucode_core_get_context(nrtucode_core_t *core) {
    if (!core) { fprintf(stderr, "...`core` is null", "nrtucode_core_get_context", "core"); abort(); }
    return *(nrtucode_context_t **)((char*)core + 0x00);  /* mov (%rdi),%rax @0x9b0955 */
}
```

Returns the `+0x00` back-pointer verbatim. No side effects. `[HIGH·OBSERVED]`

### 3.2 `nrtucode_core_get_coretype` @`0x9b0a10`

```c
/* uint32_t nrtucode_core_get_coretype(nrtucode_core_t *core); */
uint32_t nrtucode_core_get_coretype(nrtucode_core_t *core) {
    if (!core) { fprintf(stderr, "...`core` is null", "nrtucode_core_get_coretype", "core"); abort(); }
    return *(uint32_t *)((char*)core + 0x10);  /* mov 0x10(%rdi),%eax @0x9b0a15 (zero-ext) */
}
```

A pure 32-bit zero-extended load of the field create stamped from arg2. **No
validation here** — the `{2,9,17,25,32}` legality gate lives in the
`dge_*`/`mailbox` methods (§3.5), not in this getter. `[HIGH·OBSERVED]`

### 3.3 `nrtucode_core_set_friendly_name` @`0x9b0a50`

```c
/* void nrtucode_core_set_friendly_name(nrtucode_core_t *core, const char *name); */
void nrtucode_core_set_friendly_name(nrtucode_core_t *core, const char *name) {
    /* GOTCHA: NO null-guard on core. The very first act dereferences core+0x00. */
    nrtucode_context_t *ctx = *(void**)core;               /* mov (%rdi),%rdi @0x9b0a5b */
    char *cur = (char*)core + 0x48;                        /* lea 0x48(%r14),%r15      */
    nrtucode_context_log(ctx, /*sev=*/4, "%s renamed %s",  /* call @0x9b0a76, sev=INFO */
                         cur /*old name*/, name);
    strncpy(cur, name, 0x20);                              /* call strncpy @0x9b0a86   */
    *((char*)core + 0x68) = '\0';                          /* movb $0,0x68(%r14) @0x9b0a8b */
}
```

The INFO log is emitted **first**, reading the *old* name before `strncpy`
overwrites it — the line reads `"<oldname> renamed <newname>"`. No return value,
no bound on `name` beyond the 32-byte `strncpy` cap. `[HIGH·OBSERVED]`

> **GOTCHA — `set_friendly_name(NULL, …)` segfaults; it does not abort.** Unlike
> the other three accessors, it has no `test rdi,rdi`; `mov (%rdi),%rdi`
> @`0x9b0a5b` dereferences a NULL `core` directly. A documented contract
> deviation. `get_friendly_name` @`0x9b0aa0` (`lea 0x48(%rdi),%rax ; ret`) is a
> two-instruction leaf — it has **no** null-guard either, but it never
> dereferences, so it returns `core+0x48` past a NULL rather than faulting.

### 3.4 `nrtucode_context_log` — the variadic logger

`set_friendly_name` and both boot asserts call the internal logger
`nrtucode_context_log @0x9b0540` (signature `(nrtucode_context_t*, int sev,
const char *fmt, ...)`). It gates on the context's `rw_impl` log-enabled slot,
`vsnprintf`s into the context scratch (`ctx+0x18`/`ctx+0x20`), and emits via the
`rw_impl` log slot. Severities seen here: `4`=INFO (rename, init, destroy),
`1`=ERROR (the two boot asserts). Full reconstruction:
[`nrtucode_context_t`](nrtucode-context.md) §3.

### 3.5 The coretype legality gate — mask `0x102020204` = `{2,9,17,25,32}`

`get_coretype` returns the raw field; the **validity gate** is replicated in the
device-touching methods (`dge_set/get_priority_class_map`, `get_dge_mailbox_addr`,
`private_set/get_dge_mailbox`). Each reads `core+0x10`, bounds it, and tests a
64-bit bitmask:

```
9b1010:  mov    0x10(%rdi),%eax           ; coretype
9b1013:  cmp    $0x20,%rax                ; bound = 32; ja => reject
9b101c:  movabs $0x102020204,%rsi         ; 64-bit immediate 04 02 02 02 01 00 00 00
9b1026:  bt     %rax,%rsi                 ; bit set? => accepted coretype
```

The immediate `0x102020204` has bits at **2, 9, 17, 25, and 32** — the **NX_POOL**
coretype set: `2`=SUNDA, `9`=CAYMAN, `17`=MARIANA, `25`=MARIANA_PLUS,
`32`=MAVERICK. The same `movabs $0x102020204` + `cmp $0x20` appears at
`0x9b110c`, `0x9b120f`, `0x9b138e`, `0x9b140e` (all five gate sites), so the bound
and mask are uniform across every accessor that validates a coretype. `[HIGH·OBSERVED;`
`Maverick=32 / MARIANA_PLUS=25 labels INFERRED·MED — contiguity, not byte-grounded codenames]`

> **CORRECTION — the host coretype mask is `0x102020204` (not `0x2020204`), so
> the set is `{2, 9, 17, 25, 32}` (not `{2, 9, 17, 25}`).** Two earlier
> statements drop bit 32: an initial draft cited `cmp $0x19 ; bt $0x2020204` →
> `{2,9,17,25}`, and the [device bring-up](nrtucode-bringup.md) §4.3 GOTCHA cites
> mask `0x2020204` (bits 2/9/17/25). **Byte evidence:** every gate site uses a
> `48 be 04 02 02 02 01 00 00 00` `movabs` (REX.W `0x48` ⇒ 64-bit operand) with
> `cmp $0x20` (not `$0x19`); the high byte `0x01` sets bit 32. The 32-bit
> `mov $0x2020204,%ebp` @`0x9b1210` is a *different* `ebp` constant inside the
> same function — the gate `bt` there reads `%r13` = `0x102020204`, not `%ebp`.
> The [object-model graph](object-model-graph.md) already records the corrected
> mask/set; this page agrees with it.

> **GOTCHA — NX_POOL `{2,9,17,25,32}` (this struct) vs Q7-POOL `{6,13,21,29,37}`
> (not this struct).** A *separate* bitmask `0x2020202040` = `{6,13,21,29,37}`
> lives at `0x9b1a1f`, but it is inside `nrtucode_ll_get_libraries_from_opcodes`
> (an opcode→library mapper, bound `cmp $0x25`=37) — it tests an opcode/library
> classifier, **not** the core struct's `coretype@+0x10`. The Q7-POOL set is
> `6`=SUNDA, `13`=CAYMAN, `21`=MARIANA, `29`=MARIANA_PLUS, `37`=MAVERICK. The
> validity gate on *this* struct's coretype is the NX_POOL set only. Do not
> conflate the two pools.

---

## 4. `nrtucode_core_create` / `nrtucode_core_destroy`

### 4.1 `nrtucode_core_create` @`0x9b0640`

```c
/* SysV AMD64; signature recovered from prologue + the field writers.            */
int nrtucode_core_create(nrtucode_context_t *ctx,    /* rdi -> core+0x00         */
                         uint32_t            coretype,/* esi -> core+0x10         */
                         uint64_t            arg3,    /* rdx -> core+0x18 (opaque_a) */
                         uint64_t            dram_base,/* rcx -> core+0x20        */
                         uint64_t            arg5,    /* r8  -> core+0x28 (opaque_b) */
                         nrtucode_core_t   **out);    /* r9  -> *out = new core   */
```

Body (`0x9b0640`):

1. `*out = NULL` up front (`movq $0,(%r14)` @`0x9b066e`).
2. `malloc(0x70)` @`0x9b067a`. On NULL → log `"Failed to allocate nrtucode_core_t"`,
   return `5`.
3. Field init (all OBSERVED, §2): `context=ctx`, `userdata=0`, `coretype=esi`,
   `opaque_a=rdx`, `dram_base=rcx`, `opaque_b=r8`, `boot_state=0`,
   `log_memhandle=0`.
4. Default name: `snprintf(&core[0x48], 0x21, "nrtucode_core_t@%p", core)`
   @`0x9b06cd`.
5. Publish `*out = core` (`mov %rax,(%r14)` @`0x9b06ad`).
6. Log `"Initialized %s"` (sev 4) with the friendly name; `objcount_increment`;
   return `0`.

> **NOTE — create does no device I/O.** `dram_base` and the two opaque handles
> are stored as-is; create issues **no** rw-vtable calls and never touches the
> device control block. The first device transaction is `on_ucode_booted` (§5).

Return codes: `0`=OK, `5`=`malloc` failed. Aborts (`fprintf`+`abort`) if `ctx`
or `out` is NULL.

### 4.2 `nrtucode_core_destroy` @`0x9b0780`

Body:

1. Null-guard handle → abort on NULL.
2. `nrtucode_core_disable_logs(core)` @`0x9b078d` — tears down the device log CB.
3. Belt-and-suspenders: if `core[+0x38]!=0`, free it via memhandle slot1
   (`mov 0x38(%rbx),%rsi ; call *0x8(%rax)` @`0x9b0792`/`0x9b07a2`).
4. **Release the claim:** if `boot_state(core[+0x30])!=0`
   (`cmpl $0x0,0x30(%rbx)` @`0x9b07a5`), write the ready/legacy magic
   **`0x6099cb34`** back to `dram_base+0` (`movl $0x6099cb34,0xc(%rsp)`
   @`0x9b07ab`, then rw WRITE slot1 4B `call *0x8(%rax)` @`0x9b07c7`) — returning
   the device to the unclaimed state (single-owner release).
5. Log `"Destroyed %s"` (sev 4); `objcount_decrement` @`0x9b07ee`; `free(core)`
   @`0x9b07e7`; return `0`. `[HIGH·OBSERVED]`

---

## 5. The boot-claim seam — `nrtucode_core_on_ucode_booted` @`0x9b0ab0`

This is the headline of the page: the **single-owner lock** is **not** a host
field — it is the **device DRAM sentinel word at `dram_base+0x00`**, read and
test-then-set through the `rw_impl` vtable. By the time this runs the image is
already booted; `on_ucode_booted` only *claims* it.

```c
/* int nrtucode_core_on_ucode_booted(nrtucode_core_t *core); */
int nrtucode_core_on_ucode_booted(nrtucode_core_t *core) {
    if (!core) { fprintf(stderr, "...`core` is null",
                         "nrtucode_core_on_ucode_booted", "core"); abort(); }   /* @0x9b0ab5 */
    nrtucode_context_t *ctx = *(void**)core;            /* +0x00                          */
    uint64_t dram_base      = *(uint64_t*)((char*)core + 0x20);
    uint32_t W = 0;
    /* READ the booted-image sentinel: read(ctx, dram_base+0, 4, &W) via rw vt[0] */
    int rc = (*ctx->rw_impl->vt[0])(ctx, dram_base, 4, &W);   /* call *(%rax) @0x9b0add  */
    if (rc) return rc;                                        /* device-error passthrough */

    if (W == 0x6099cb34) {                  /* READY / LEGACY (unclaimed)  cmp @0x9b0aee */
        uint32_t claim = 0x502b2da1;        /* CLAIM word                  mov @0x9b0b2f */
        rc = (*ctx->rw_impl->vt[1])(ctx, dram_base, 4, &claim); /* WRITE   call *0x8 @0x9b0b4b */
        if (rc) return rc;                  /* write-error passthrough     jne @0x9b0b50 */
        *(uint32_t*)((char*)core + 0x30) = 1;  /* boot_state = BOOTED      movl @0x9b0b52 */
        return 0;
    }
    if (W == 0x502b2da1) {                  /* already CLAIMED by another  cmp @0x9b0af7 */
        nrtucode_context_log(ctx, 1 /*ERROR*/,
            "...%s: Core is claimed by another nrtucode_core_t instance",
            "claim_core", (char*)core + 0x48 /*friendly_name*/);             /* @0x9b0b1f */
        return 8;
    }
    /* any other W => incompatible image (W still in r9d as the %x arg)              */
    nrtucode_context_log(ctx, 1 /*ERROR*/,
        "...%s: Magic value `%x` mismatch (booted image is incompatible)",
        "claim_core", (char*)core + 0x48, W);                               /* @0x9b0b80 */
    return 8;
}
```

**Boot-magic resolution (byte-exact):**

| what | value | proof (`@addr`) |
|---|---|---|
| sentinel address | `dram_base + 0x00`, `dram_base = *(u64)(core+0x20)` | `mov 0x20(%rbx),%rsi` @`0x9b0acc`, **no** `add` after — offset is exactly `+0` |
| transport / width | `rw_impl` read slot `+0x00`, **4 bytes** | `mov $0x4,%edx ; call *(%rax)` @`0x9b0ad8`/`0x9b0add` |
| READY (accept) | `0x6099cb34` | `cmp $0x6099cb34,%r9d` @`0x9b0aee` |
| CLAIMED (busy) | `0x502b2da1` | `cmp $0x502b2da1,%r9d` @`0x9b0af7` |
| claim write | `0x502b2da1` → `dram_base+0` | `movl $0x502b2da1,0xc(%rsp)` @`0x9b0b2f` ; `call *0x8(%rax)` @`0x9b0b4b` |
| set host state | `boot_state = 1` | `movl $0x1,0x30(%rbx)` @`0x9b0b52` |
| release (destroy) | `0x6099cb34` → `dram_base+0` | `movl $0x6099cb34,…` @`0x9b07ab` ; `call *0x8(%rax)` @`0x9b07c7` |

> **CORRECTION — reconciliation with [device bring-up](nrtucode-bringup.md) §7
> (committed): magics match exactly, no divergence.** That page documents
> `nrtucode_core_on_ucode_booted @0x9b0ab0` as a 4-byte read of the **ready
> sentinel `0x6099cb34`** (`==` the device `.globstruct[0]`, bytes `34 cb 99 60`
> LE) followed by a write of the **claim magic `0x502b2da1`**. This struct-view
> disasm reproduces both constants byte-for-byte at the addresses in the table
> above. There is **no** magic discrepancy to correct — the two pages are
> consistent. The only correction on this page concerns the *coretype* mask
> (§3.5), which is orthogonal to the boot path: `on_ucode_booted` never reads
> `core+0x10` (verified — its only field accesses are `+0x00`, `+0x20`, `+0x30`,
> `+0x48`), so the boot magic is **coretype-independent** for every flavor.

> **NOTE — the host claim is read-then-write, not a CAS.** The read @`0x9b0add`
> and the write @`0x9b0b4b` are two distinct `rw_impl` transactions; mutual
> exclusion relies on the (caller-supplied) `rw_impl` backend serializing device
> accesses. The host issues no atomic compare-exchange. `[OBSERVED]`

### 5.1 State machine (single-owner lock)

```
W = read_u32(dram_base + 0)                       ; rw vt[0], 4B
if (read errored) return errcode                  ; device passthrough
switch (W) {
  case 0x6099cb34:                                ; READY / unclaimed
      if (write_u32(dram_base+0, 0x502b2da1)) return errcode
      core->boot_state = 1                        ; +0x30 = BOOTED
      return 0
  case 0x502b2da1:                                ; busy
      log(ERROR, "Core is claimed by another nrtucode_core_t instance"); return 8
  default:                                        ; incompatible image
      log(ERROR, "Magic value `%x` mismatch (booted image is incompatible)", W); return 8
}
release (destroy): write_u32(dram_base+0, 0x6099cb34) when boot_state != 0
```

Return: `0`=claimed OK · `8`=busy or incompatible · rw-errcode passthrough if the
device transaction itself failed · abort on NULL `core`.

---

## 6. The device control block (`dram_base`, reached via `rw_impl`)

Beyond the boot word at `+0x00`, the rest of the live engine state is
`dram_base`-relative (reached as `rw(ctx, core[+0x20]+N, …)`), **not** host
struct fields. Summarised for the address-space picture; full treatment in the
[object-model graph](object-model-graph.md) and the per-API runtime pages.

| dram off | sz | name | reached by |
|---|---|---|---|
| `+0x00` | 4 | boot magic / claim word | `on_ucode_booted` (§5), `destroy` release |
| `+0x04` | 4 | log buffer size (device) | `enable_logs` |
| `+0x08` | 8 | log buffer device addr | `enable_logs` (memhandle slot4 translate) / `disable_logs` writes 0 |
| `+0x10` | 4 | log cursor (device) | `enable_logs` writes 0 |
| `+0x14` | 4 | max loglevel (device) | `set_max_loglevel` / `disable_logs` |
| `+0x18` | var | priority_class_map | `dge_set/get_priority_class_map` (count·4, count≤4) |
| `+0x28` | 4·4 | dge_mailbox`[0..3]` | `get_dge_mailbox_addr` returns `dram_base+0x28`; `private_set/get_dge_mailbox` index slot n≤3 |
| `+0x38` | 8 | pc_bounds_soc_addr_lo | `enable/disable/get_pc_bounds_check` |
| `+0x40` | 8 | pc_bounds_soc_addr_hi | `enable/disable/get_pc_bounds_check` |

---

## 7. Strings / magic-site index `[OBSERVED]`

`.rodata` strings (reproduce identically in both binaries; `.rodata` Δ=0):

| string | used by |
|---|---|
| `nrtucode: invalid API usage in `\``%s`\``: `\``%s`\`` is null\n` (file off `0x5469`) | every accessor null-guard |
| `nrtucode_core_t@%p` | create default name (`snprintf`) |
| `%s renamed %s` | `set_friendly_name` INFO log |
| `Initialized %s` / `Destroyed %s` | create / destroy INFO log |
| `claim_core` | the `%s` fn-name in both boot asserts |
| `...%s: Core is claimed by another nrtucode_core_t instance` | already-claimed (busy) assert |
| `...%s: Magic value `\``%x`\`` mismatch (booted image is incompatible)` | incompatible-image assert (`%x` = the bad word) |

Magic-constant operand sites across `.text` (exactly four — no per-coretype
magic array): `0x9b07ab` `0x6099cb34` (destroy release) · `0x9b0aee`
`cmp 0x6099cb34` (READY test) · `0x9b0af7` `cmp 0x502b2da1` (CLAIMED test) ·
`0x9b0b2f` `0x502b2da1` (claim write).

---

## 8. Confidence summary

**HIGH · OBSERVED** (single-instruction-anchored):

- struct size `0x70` (`mov $0x70,%edi` @`0x9b0675`); the byte map (§2) with every
  field anchored to its writer.
- `context@+0x00` back-pointer (create `mov %rbx,(%rax)` @`0x9b0684`; `get_context`
  @`0x9b0955`).
- `coretype@+0x10` (create @`0x9b068f`; `get_coretype` @`0x9b0a15`) and its
  legality mask `0x102020204` = `{2,9,17,25,32}` with bound `cmp $0x20`.
- the boot magics: READY `0x6099cb34`, CLAIM `0x502b2da1`, at `dram_base+0` via rw
  vt[0]/vt[1], `boot_state=1` on claim, release on destroy.
- create/destroy flow; all four accessors; `set_friendly_name`'s missing
  null-guard.

**MED · INFERRED:** the `MARIANA_PLUS = 25` / `MAVERICK = 32` coretype labels in
§3.5 (v4+/v5-interior, by contiguity with the Q7-POOL pattern, not byte-grounded
codenames). The purpose of `opaque_handle_a@+0x18` / `opaque_handle_b@+0x28` (the
writes are HIGH·OBSERVED; the meaning is LOW — never read in this library,
consumed by lower NRT layers).

**UNKNOWN:** pad bytes `+0x14..0x17`, `+0x34..0x37`, `+0x69..0x6F` (alignment/tail
padding, never written, heap-garbage contents — accounted for as padding).

---

## 9. See also

- [`nrtucode_context_t` + Lifecycle](nrtucode-context.md) — the `0x28`-byte object
  this struct's `+0x00` points back to (the `rw_impl`/`memhandle_impl` vtables and
  the logger).
- [The nrtucode Object Model Graph](object-model-graph.md) — the full
  context→core→memhandle→ll→opset graph and the corrected coretype mask/set.
- [Device bring-up: `nrtucode_core_create` → claim](nrtucode-bringup.md) — the
  host-driver call sequence that ends in `on_ucode_booted` (the committed boot
  magics this page reconciles with).
- [Boot & Reset (microarchitecture)](../uarch/boot-reset.md) — the device side of
  the ready/claim sentinel.
- Device firmware globals (`.globstruct`) — the on-device structure whose first
  word is the `0x6099cb34` ready sentinel; see
  `neuronx-gpsimd/wiki/src/appendix/struct-device-firmware-globals.md` *(page
  pending; until it lands, the `.globstruct[0]` ready word is documented in
  device bring-up §7).*
