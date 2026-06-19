# `nrtucode_ll_create` / `destroy` / `set_friendly_name` / `get_library_size`

> **Binary:** `libnrtucode_internal.so` (host x86-64 ELF, *not* stripped,
> BuildID `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd`) from the
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64` package. The shipped twin
> `libnrtucode.so` (BuildID `abf4e088…`, `.dynsym`-only) carries the same code
> with `.dynsym` exports at the stripped addresses listed below; it was used to
> cross-check codegen. Every offset, immediate, symbol and `.rodata` string on
> this page was re-verified by `nm`/`objdump`/`readelf`/`xxd` against the
> internal binary.
>
> **Section deltas** (`readelf -SW`): `.rodata` VMA==fileoffset (Δ=0), `.text`
> Δ=0x1000, `.data.rel.ro` Δ=0x2000, `.data` Δ=0x3000. All `.rodata` strings and
> the `cayman_memory_bounds` const below are at VMA==fileoffset and were read
> directly.
>
> **GOTCHA — no C++ vtables here.** `nm libnrtucode_internal.so | rg -c '_ZTV'`
> → **0**. This binary has *zero* `_ZTV` C++ vtables; the memhandle dispatch
> table reached through `ctx+0x08` is a plain **C function-pointer array**
> (slot N = `base + 8*N`, first fnptr at `+0x00`). The `_ZTV + 0x10` slot rule
> used for `libnrt.so` / the ncore2gp DLLs does **not** apply — every `call
> *N(%rax)` below is indexed straight from the table base.

This page documents the construction and lifecycle-tail of the **loadable
library** (`ll`) object — the 72-byte (`0x48`) host handle that owns one
prelinked GPSIMD ext-ISA kernel image plus its on-device staging buffer.
`nrtucode_ll_create` resolves a library selector, loads the embedded ext-ISA
blob, **prelinks** it (host relocation), **stages** the relocated image to
device memory, and records the result in the `ll` struct.
`nrtucode_ll_destroy` is the symmetric host destructor; `set_friendly_name` and
`get_library_size` are the two trivial accessors.

Cross-links:

- The object-model graph that lists `nrtucode_ll_t` alongside the core/opset
  handles: [`runtime/object-model-graph.md`](./object-model-graph.md) (`ll` =
  `0x48`).
- The host prelinker / R\_XTENSA relocator that `create` calls and whose `"UCPL "`
  output header `create` stages to device:
  [`runtime/ucode-relocation-consumer.md`](./ucode-relocation-consumer.md)
  (committed) and [`runtime/prelinker-ucpl.md`](./prelinker-ucpl.md) (stub).
- The load/unload **instruction-sequence** generators (distinct from `destroy`):
  [`runtime/nrtucode-ll-load-unload.md`](./nrtucode-ll-load-unload.md) (stub).
- The opcode→library resolver that maps opcodes to a `library_selector`:
  [`runtime/opcode-to-lib-resolver.md`](./opcode-to-lib-resolver.md) (stub).
- The device-side consumer of the staged image:
  [`../firmware/pool/external-lib-loader.md`](../firmware/pool/external-lib-loader.md)
  (committed).

---

## 0. Symbol table `[HIGH·OBSERVED]`

`nm` confirms the four exported entry points (all `T`, global) plus the
internal helpers `create` reaches:

| symbol | internal `@` | stripped `@` | size | notes |
| ------ | ------------ | ------------ | ---- | ----- |
| `nrtucode_ll_create` | `0x9b1a90` | `0x309f40` | — | this page |
| `nrtucode_ll_destroy` | `0x9b1da0` | `0x30a230` | `0x82` | host destructor |
| `nrtucode_ll_set_friendly_name` | `0x9b2450` | `0x30a8e0` | `0x46` | inline-buffer rename |
| `nrtucode_ll_get_library_size` | `0x9b24a0` | `0x30a930` | `0x05` | `mov 0x18(rdi),rax; ret` |
| `nrtucode_get_ext_isa_internal` | `0x9b2b30` | — | — | static; selects the ext-ISA blob |
| `prelink` | `0x9b5d60` | — | — | static; host relocator (orchestrator) |
| `prelink_load_lib` | `0x9b5e70` | — | — | static; segment copy |
| `prelink_relocate_lib` | `0x9b6160` | — | — | static; `Elf32_Rela` walk |
| `nrtucode_objcount_increment` | `0x9b17a0` | — | — | static; `lock inc` live-object counter |
| `nrtucode_objcount_decrement` | `0x9b17b0` | — | — | static; `lock dec` |
| `cayman_memory_bounds` | `0x9aaee0` | `0x3b98` | — | `.rodata` const; scratch sizes + masks |

The struct's binary name is `nrtucode_ll_t` — the default friendly-name format
string `"nrtucode_ll_t@%p"` lives at `.rodata 0x49ac`. The public header calls
the opaque type `nrtucode_loadable_library_t`; doc line: *"A loadable_library
object corresponds to exactly one library to be loaded into Q7 IRAM/DRAM."*

> **Verified `.rodata` strings** (file offset == VMA, `.rodata` base `0x46b0`):
> `0x49ac` `"nrtucode_ll_t@%p"`, `0x4d15`
> `"NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE"`, `0x4d41` `"ll"`, `0x4d44`
> `"Prelinked library would be larger than the available buffer on device"`,
> `0x4749` `"nrtucode_ll_create"`, `0x5071` `"out_ll"`, `0x52fa`
> `"Initialized %s"`, `0x5528` `"Failed prelink with status = %d"`, `0x50c0`
> `"Destroyed %s"`, `0x51be` `"%s renamed %s"`, `0x502a`
> `"nrtucode_ll_destroy"`, `0x5469` `"nrtucode: invalid API usage in %s: %s is
> null"`. All read byte-exact.

---

## 1. The `nrtucode_ll_t` struct — `malloc(0x48)`, 72 bytes `[HIGH·OBSERVED]`

Allocation: `mov $0x48,%edi`@`0x9b1afd` → `call malloc@plt`@`0x9b1b02`. Every
store into the block (`rax`/`rbx` = `ll`) is single-instruction-anchored:

| off | size | type | name | writer / proof | obs/inf · conf |
| --- | ---- | ---- | ---- | -------------- | -------------- |
| `0x00` | 8 | `nrtucode_context_t*` | `context` | `mov %r14,(%rax)`@`0x9b1b1f` (`r14`=arg1=ctx). `destroy` reads `mov (%rdi),%r14`@`0x9b1dac`. | OBSERVED·HIGH |
| `0x08` | 8 | `void*` (memhandle) | `device_memhandle` | written by the device allocator, not a direct store: `lea 0x8(%rbx),%rcx`@`0x9b1c3c` is the out-handle arg to memhandle `device_malloc` (`call *(%rax)`@`0x9b1c5d`). `destroy` frees it via slot `+0x08`. | OBSERVED·HIGH (write) / INFERRED·HIGH (role) |
| `0x10` | 8 | `uint64_t` | `library_selector` | `mov %r12,0x10(%rax)`@`0x9b1b13`; `r12` = the env/CPTC/caller-resolved selector (§3), same value fed to `get_ext_isa_internal`. | OBSERVED·HIGH (write) / INFERRED·MED (purpose) |
| `0x18` | 8 | `uint64_t` | `prelinked_size` | pre-set `movq $0,0x18(%rax)`@`0x9b1b17`; on the prelink path set `mov %rsi,0x18(%rbx)`@`0x9b1c48` where `rsi = OUT[0x10]+OUT[0x0c]` (= total staged image bytes, §8). | OBSERVED·HIGH |
| `0x20` | `0x21` | `char[0x21]` | `friendly_name` | `lea 0x20(%rbx),%r12`; `snprintf(size=0x21,"nrtucode_ll_t@%p")` — size imm `mov $0x21,%esi`@`0x9b1bf6`, `call snprintf`@`0x9b1c03`. Spans `0x20..0x40` (NUL @`0x40`). | OBSERVED·HIGH |
| `0x41` | 7 | — | tail pad | `malloc(0x48)`; last written byte is the name NUL at `0x40`. `0x41..0x47` never touched by create/destroy — heap garbage; alignment/tail pad. | INFERRED·HIGH |

Byte-coverage `0x00..0x48`: `0x00` context · `0x08` device\_memhandle · `0x10`
library\_selector · `0x18` prelinked\_size · `0x20..0x40` friendly\_name[`0x21`]
(NUL @`0x40`) · `0x41..0x47` tail pad — **every one of the `0x48` bytes is
accounted for.**

```c
struct nrtucode_ll_t {                  /* sizeof = 0x48 = 72  (malloc'd)        */
    nrtucode_context_t *context;        /* +0x00  arg1; never NULL on success    */
    void               *device_memhandle;/*+0x08  staged-image handle; valid only */
                                        /*        when prelinked_size != 0        */
    uint64_t            library_selector;/*+0x10  resolved lib index (env/CPTC/arg)*/
    uint64_t            prelinked_size; /* +0x18  total staged device bytes; 0 on  */
                                        /*        the coretype-6 (no-prelink) path */
    char                friendly_name[0x21]; /* +0x20..+0x40 (NUL); "nrtucode_ll_t@%p" */
    /* +0x41 .. +0x47 : tail pad (uninitialised) */
};
```

> **NOTE — `+0x08` is written by the callee, not a direct store.** The
> memhandle `device_malloc` slot receives `&ll->device_memhandle` as its
> out-parameter (`lea 0x8(%rbx),%rcx`) and writes the on-device buffer handle
> into it. So `+0x08` is meaningful **only** on the prelink path; on the
> coretype-6 path it is left whatever `malloc` returned (and `destroy`'s
> `prelinked_size!=0` gate keeps it from being touched). The committed
> object-model-graph page lists the same `0x48` layout.

---

## 2. Signature `[HIGH·OBSERVED prologue + writers]`

```c
nrtucode_result_t nrtucode_ll_create(
        nrtucode_context_t  *ctx,        /* rdi -> ll+0x00 ; NULL => FATAL abort   */
        uint32_t             coretype,    /* esi -> per-coretype switch (§5)        */
        uint32_t             flavor,       /* edx -> get_ext_isa_internal arg2       */
        uint64_t             library,       /* rcx -> caller library selector (§3)    */
        nrtucode_ll_t      **out_ll);        /* r8  -> *out=NULL up front; NULL=>abort */
```

Return (`eax`): `0` OK · `5` `malloc` OOM (ll, scratch1, or scratch2) · `7`
staged image exceeds the 64 KiB device cap (§7) · `8` bad coretype (switch
default), a propagated `get_ext_isa_internal` error, or a device
write/translate error from the memhandle vtable · `9` `prelink` failed
(`"Failed prelink with status = %d"`). A non-zero `get_ext_isa_internal` rc is
returned verbatim **before any allocation**.

The NULL-arg contract is **FATAL** (`fprintf(stderr, "nrtucode: invalid API
usage in %s: %s is null") + abort()`), not error-returned — the same idiom used
across the nrtucode object constructors. `ctx==NULL` and `out_ll==NULL`
(arg-name string `"out_ll"`@`0x5071`, fn-name `"nrtucode_ll_create"`@`0x4749`)
both abort.

---

## 3. Library-selector resolution — the CPTC env force `[HIGH·OBSERVED]`

The very first thing `create` does after the NULL guards is a single `getenv`
(the only one in the function) on the byte-exact name
`"NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE"` (`.rodata 0x4d15`). Internal
`@0x9b1ac4..0x9b1ae0`:

```c
uint64_t selector;                                   /* r12 */
char *env = getenv("NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE");
selector = (env != NULL) ? 3 : library;              /* cmove r12,r13 ; literal $0x3 */
if (library != 0) selector = library;                /* cmovne r12,r13 : caller wins */
```

Truth table (derived by executing both `cmov`s):

| caller `library` | env set? | resolved `selector` (`r12`) |
| ---------------- | -------- | --------------------------- |
| `!= 0` | any | `library` (caller **always** wins) |
| `== 0` | yes | `3` (CPTC force → index 3) |
| `== 0` | no | `0` (default index 0) |

> **NOTE.** The env var takes effect **only** when the caller passes
> `library==0`, forcing the literal index `3` (`mov $0x3,%r12d`). The resolved
> `selector` is then (a) passed as `get_ext_isa_internal`'s library arg and (b)
> stored at `ll+0x10`. The "CPTC decode == index 3" semantics are INFERRED from
> the env name; the integer index `3` (not a bit-set) is OBSERVED. The stripped
> twin encodes the identical logic with `mov $0x3,%r13d ; cmove %rbp,%r13 ;
> cmovne %rbp,%r13` — same literal, same branches.

---

## 4. `nrtucode_ll_create` — annotated reconstruction `[HIGH·OBSERVED]`

Symbol `@0x9b1a90` (internal) / `0x309f40` (stripped). The flow:

```c
nrtucode_result_t nrtucode_ll_create(nrtucode_context_t *ctx, uint32_t coretype,
                                     uint32_t flavor, uint64_t library,
                                     nrtucode_ll_t **out_ll)
{
    /* --- 0. NULL guards (FATAL, not error-returned) @0x9b1a9e / 0x9b1aaa --- */
    if (ctx == NULL)    abort_invalid_api("nrtucode_ll_create", "...");   /* fprintf+abort */
    if (out_ll == NULL) abort_invalid_api("nrtucode_ll_create", "out_ll");
    *out_ll = NULL;

    /* --- 1. resolve library selector (§3) @0x9b1ac4..e0 --- */
    uint64_t selector = resolve_cptc(library);             /* getenv + 2 cmov; r12 */

    /* --- 2. load the embedded ext-ISA prelinked-image blob.  pi_lib @rsp+0x38 --- */
    pi_library_t *pi_lib;                                   /* out param */
    int rc = nrtucode_get_ext_isa_internal(coretype, flavor, &pi_lib, selector); /* call 0x9b2b30 @0x9b1af0 */
    if (rc != 0) return rc;                                 /* propagated, NO alloc yet */

    /* --- 3. allocate the host handle @0x9b1afd --- */
    nrtucode_ll_t *ll = malloc(0x48);                       /* mov $0x48,%edi ; call malloc@plt */
    if (ll == NULL) return 5;
    ll->library_selector = selector;                       /* mov %r12,0x10(%rax) @0x9b1b13 */
    ll->prelinked_size   = 0;                               /* movq $0,0x18(%rax) @0x9b1b17 */
    ll->context          = ctx;                             /* mov %r14,(%rax)    @0x9b1b1f */

    /* --- 4. per-coretype switch (§5) @0x9b1b22 --- */
    if (coretype == 6)            goto name_and_succeed;    /* no prelink; size stays 0 */
    if (!is_prelink_coretype(coretype)) {                   /* not in {13,21,29[,37]} */
        free(ll); return 8;                                 /* @0x9b1ceb */
    }

    /* --- 5. host scratch buffers from cayman_memory_bounds (§6a) --- */
    void *scratch1 = malloc(cayman_memory_bounds[0x18]);   /* = 0x8000 (32 KiB) @0x9b1b4d */
    if (!scratch1) { free(ll); return 5; }
    void *scratch2 = malloc(cayman_memory_bounds[0x38]);   /* = 0x2000  (8 KiB) @0x9b1b5e */
    if (!scratch2) { free(scratch1); free(ll); return 5; }

    /* --- 6. host prelink (relocation) @0x9b1b96 — see #816 --- */
    struct prelink_out OUT;                                 /* rsp+0x18; 0x20-byte UCPL hdr + descriptors */
    rc = prelink(pi_lib, &cayman_memory_bounds, ctx, prelink_error_log_callback,
                 scratch1, scratch2, &OUT);                 /* prelink @0x9b5d60 */
    if (rc != 0) {
        nrtucode_context_log(ctx, 1, "Failed prelink with status = %d", rc);
        free(scratch2); free(scratch1); free(ll); return 9;
    }

    /* --- 7. record size; stage to device iff non-zero (§6b, §8) --- */
    ll->prelinked_size = OUT.seg3_off + OUT.seg3_len;       /* mov 0x28(rsp)+0x24(rsp) -> 0x18(rbx) @0x9b1c48 */
    if (ll->prelinked_size != 0) {
        void *mh = ctx->memhandle_impl;                    /* mov 0x8(%r14),%rax @0x9b1c4e */
        rc = ((device_malloc_fn)mh[0])(ctx, 0x1000000, &ll->device_memhandle); /* call *(%rax) @0x9b1c5d ; 16 MiB */
        if (rc) { free(scratch2); free(scratch1); free(ll); return rc; }

        /* 3 ordered device writes via memhandle slot +0x18 (write) (§8) */
        rc  = mh_write(ctx, ll->device_memhandle, /*off*/0,            /*len*/0x20,        &OUT);       /* @0x9b1c81 */
        rc |= mh_write(ctx, ll->device_memhandle, OUT.seg2_off, OUT.seg2_len, scratch1);               /* @0x9b1ca3 */
        rc |= mh_write(ctx, ll->device_memhandle, OUT.seg3_off, OUT.seg3_len, scratch2);               /* @0x9b1cc0 */
        if (rc) { device_free(ctx, ll->device_memhandle); free(scratch2); free(scratch1); free(ll); return rc; }

        /* device-buffer cap (§7) @0x9b1d0b */
        if (ll->prelinked_size >= 0x10001) {               /* cmpq $0x10001,0x18(%rbx) ; jb */
            nrtucode_context_log(ctx, 1,
                "Prelinked library would be larger than the available buffer on device");
            device_free(ctx, ll->device_memhandle);
            free(scratch2); free(scratch1); free(ll); return 7;
        }
    }
    free(scratch2); free(scratch1);                         /* @0x9b1d32 — transient buffers released */

name_and_succeed:
    /* --- 8. name, log, count, hand back @0x9b1be8.. --- */
    snprintf(ll->friendly_name, 0x21, "nrtucode_ll_t@%p", ll); /* call snprintf @0x9b1c03 */
    nrtucode_context_log(ctx, 4, "Initialized %s", ll->friendly_name);
    nrtucode_objcount_increment();                         /* lock inc @0x9b1c23 */
    *out_ll = ll;
    return 0;                                              /* NRTUCODE_SUCCESS */
}
```

> **NOTE — error-path freeing.** *Every* `create` error path frees what it has
> allocated so far in strict reverse order (device handle → scratch2 → scratch1
> → ll). There is no `goto cleanup` fall-through; the cleanup is open-coded per
> exit. This is why `destroy` never has to free the scratch buffers — they
> never outlive `create`.

---

## 5. The per-coretype switch — exact constants `[HIGH·OBSERVED]`

`create` validates `coretype` against the **opcode-style** numbering
`{6, 13, 21, 29}` — *not* the `core_create` numbering `{2,9,17,25}` (the two
differ by +4). The two binaries encode the test differently:

**Internal twin** (`@0x9b1b22`) — bitmask form:

```
cmp    $0x25, %ebp                ; coretype > 37 -> bad (ret 8)
movabs $0x2020202000, %rcx        ; the literal bitmask
bt     %rax, %rcx                 ; test bit `coretype`
jae    <special>                  ; bit clear -> coretype==6 check, else ret 8
```

Bits set in `0x2020202000` are positions `13, 21, 29, 37` (`0x0d,0x15,0x1d,0x25`),
spaced by +8. Plus the special-cased `6`. So the internal twin accepts
`{6, 13, 21, 29, 37}`.

**Stripped / shipped** (`@0x309fd9`) — jump-table form: `add $0xfffffffa,%r12d`
(`coretype-6`), `cmp $0x17` (range `[6..29]`), then `jt @0x3a24` (24 signed
rel32 entries). The table routes `6 → 0x30a183` (no-prelink success), `13/21/29
→ 0x309ff7` (prelink), everything else → `0x30a140` (free ll, return 8).

> **QUIRK — `37` is shipped-unreachable.** The internal bitmask lists bit `37`
> (`0x25`), but the **shipped jump table only spans `coretype-6` ∈ `[0,0x17]`**,
> i.e. `coretype ∈ [6,29]`, so `37` falls off the end and is rejected. Treat the
> shipped set **`{6, 13, 21, 29}`** as authoritative for runtime. `6` is the
> degenerate host-only family that needs no device prelink (`prelinked_size`
> stays `0`); `13/21/29` are the three device generations that do.

---

## 6. Two allocation tiers `[HIGH·OBSERVED]`

Do not conflate them.

**(a) Host scratch (transient, freed before return).** Sizes come from the
`cayman_memory_bounds` const (`.rodata 0x9aaee0`):

| `cayman_memory_bounds` qword (LE) | value |
| --------------------------------- | ----- |
| `+0x00` | `0x0` |
| `+0x08` | `0x20000` (128 KiB) |
| `+0x10` | `0x18000` (96 KiB) |
| `+0x18` | **`0x8000` (32 KiB)** — `scratch1` |
| `+0x20` | `0x80000` (512 KiB) |
| `+0x28` | `0x40000` (256 KiB) |
| `+0x30` | `0xbe000` (760 KiB) |
| `+0x38` | **`0x2000` (8 KiB)** — `scratch2` |
| `+0x40` | `0xffa0ffffdfffffff` (mask) |
| `+0x50` | `0x3ff9bfffdfffffff` (mask) |

Only `[0x18]` (`scratch1`, `malloc`@`0x9b1b4d`) and `[0x38]` (`scratch2`,
`malloc`@`0x9b1b5e`) are used by `create`. Both are handed to `prelink` (which
poison-fills them with `0xA5`) and freed on the success cap-pass and on every
error path.

**(b) Device staging buffer — 16 MiB.**

```
mov 0x8(%r14),%rax          ; rax = ctx->memhandle_impl (the C fnptr table)
mov $0x1000000,%edx         ; size = 16 MiB  (HARD-CODED) @0x9b1c52
lea 0x8(%rbx),%rcx          ; out-handle = &ll->device_memhandle
call *(%rax)                ; table slot +0x00 = device_malloc @0x9b1c5d
```

The 16 MiB on-device buffer is requested through the memhandle table's
`device_malloc` (slot `+0x00`); the handle lands in `ll+0x08`. This only runs
when `prelinked_size != 0`.

---

## 7. The 64 KiB device cap `[HIGH·OBSERVED]`

After the three writes succeed, `@0x9b1d0b`:

```
cmpq $0x10001, 0x18(%rbx)   ; compare prelinked_size with 0x10001
jb   <ok>                   ; size < 0x10001 (i.e. <= 0x10000) -> OK
```

The immediate is `0x10001` and the branch is `jb` (below), so the **enforced
maximum staged-image size is `0x10000` = 65 536 = 64 KiB inclusive**. On
overflow it logs `"Prelinked library would be larger than the available buffer
on device"` (`.rodata 0x4d44`), frees the device handle + both scratch buffers
+ `ll`, and returns `7`. Identical in the stripped twin (`cmpq $0x10001,0x18`
@`0x30a14c`).

> **NOTE — asymmetry.** The device buffer requested is 16 MiB (§6b) but the
> prelinked image is capped at 64 KiB. The 16 MiB is a generous fixed
> allocation; the 64 KiB is the real ceiling enforced on the actual library
> size.

---

## 8. Device write sequence + the UCPL header `[HIGH·OBSERVED]`

All three writes go through `call *0x18(%rax)` (memhandle table slot `+0x18` =
write), `rax = ctx->memhandle_impl`. Args: `write(ctx, handle, off, len, src)`.

| write | site | off | len | src |
| ----- | ---- | --- | --- | --- |
| header (UCPL) | `@0x9b1c81` | `0` (`xor %edx`) | `0x20` (`mov $0x20,%ecx`) | `&OUT` (`rsp+0x18`) |
| seg2 | `@0x9b1ca3` | `OUT[0x04]` (`0x1c(rsp)`) | `OUT[0x08]` (`0x20(rsp)`) | `scratch1` (`r12`) |
| seg3 | `@0x9b1cc0` | `OUT[0x0c]` (`0x24(rsp)`) | `OUT[0x10]` (`0x28(rsp)`) | `scratch2` (`r13`) |

`ll->prelinked_size = OUT[0x10] + OUT[0x0c]` (end-offset of seg3 = total image)
— `mov 0x28(%rsp),%esi ; add 0x24(%rsp),%esi ; mov %rsi,0x18(%rbx)`
@`0x9b1c40..48`. Any non-zero write rc jumps to the device-free + free path.

> **CORRECTION / RECONCILIATION with #816 (`ucode-relocation-consumer.md`).**
> The first device write is the **32-byte UCPL header** that host `prelink`
> emits into its `OUT` descriptor. #816 §8 decodes `prelink @0x9b5d60` storing
> `*(u64*)(out+0x00) = 0x204C504355` — little-endian ASCII `"UCPL "` — as a
> 32-byte (`0x20`) header. This page independently confirms the **`0x20`** write
> length here (`mov $0x20,%ecx`@`0x9b1c77`), so the two pages agree byte-for-byte:
> **seg1 = the `0x20`-byte `"UCPL "` header, staged first at device offset 0,
> followed by the two relocated body segments.** No divergence. (The `"UCPL "`
> magic itself is *not* a string in `libnrtucode_internal.so`'s `.rodata` — it
> is an immediate stored by `prelink`, and the device loader consumes it, so a
> `.rodata` grep for `"UCPL "` in this binary correctly returns nothing.) The
> `create` consumer here treats `OUT` as an opaque `{header, seg2 off/len, seg3
> off/len}` descriptor; the full UCPL layout and the `prelink_relocate_lib
> @0x9b6160` relocation walk live in #816 / the
> [prelinker-ucpl](./prelinker-ucpl.md) lane.

---

## 9. `nrtucode_ll_destroy` — the host destructor `[HIGH·OBSERVED]`

Symbol `@0x9b1da0` / `0x30a230`, **`0x82` bytes**, full disasm decoded
byte-exact. It frees exactly what `create` allocated and nothing it did not.

```c
nrtucode_result_t nrtucode_ll_destroy(nrtucode_ll_t *ll)
{
    if (ll == NULL)                                        /* test %rdi,%rdi ; je @0x9b1da7 */
        abort_invalid_api("nrtucode_ll_destroy", "ll");    /* fprintf(stderr,...)+abort */

    nrtucode_context_t *ctx = ll->context;                /* mov (%rdi),%r14 @0x9b1dac */

    if (ll->prelinked_size != 0) {                        /* cmpq $0,0x18(%rdi) ; je @0x9b1db4 */
        void *mh = ctx->memhandle_impl;                   /* mov 0x8(%r14),%rax @0x9b1db6 */
        ((device_free_fn)mh[1])(ctx, ll->device_memhandle);/* call *0x8(%rax) @0x9b1dc1 -- slot +0x08 */
    }

    nrtucode_context_log(ctx, 4, "Destroyed %s", ll->friendly_name); /* @0x9b1dc4..d9 */
    free(ll);                                             /* call free@plt @0x9b1de1 -- the 0x48 block */
    nrtucode_objcount_decrement();                        /* lock dec @0x9b1de8 */
    return 0;                                             /* xor eax,eax ; ret @0x9b1ded */
}
```

**Teardown order (exact):** read `ctx` → (gated) `device_free` → log
`"Destroyed %s"` reading the inline name → `free(ll)` → `objcount_decrement` →
return `0`.

> **NOTE — ordering is safe.** The log reads `ll->friendly_name` **before**
> `free(ll)` (no name UAF). `objcount_decrement` runs **after** `free(ll)` but
> only touches the global `.bss` counter (`lock dec`), never the freed block —
> safe.

**Free-list — what `destroy` releases:**

| resource | freed by | gate |
| -------- | -------- | ---- |
| device staging buffer (`ll+0x08`) | memhandle `device_free` (table slot `+0x08`) @`0x9b1dc1` | only when `prelinked_size(+0x18) != 0` |
| host `ll` struct (the `0x48` block) | `free@plt` @`0x9b1de1` | always (non-abort path) |
| `friendly_name[0x21]` | implicit — inline in the `0x48` block | freed by `free(ll)` |
| scratch1 / scratch2 | **not here** — freed inside `create` | n/a (transient) |

> **GOTCHA — the device-free gate is the same field `create` writes.** `create`
> sets `prelinked_size(+0x18)` non-zero **iff** it actually `device_malloc`'d,
> and `destroy` `device_free`s **iff** `+0x18 != 0`. The device handle is freed
> exactly when it was allocated — perfectly symmetric, no leak on the coretype-6
> path (no alloc, no free) and no spurious free either. The `+0x18` gate is a
> *"was a device buffer staged?"* test, **not** a double-free guard.

> **GOTCHA — no per-object double-free guard.** `destroy` does **not** null
> `ll->device_memhandle` or `ll->context` after freeing, and there is no in-struct
> "freed" sentinel. Calling `destroy` twice on the same pointer is undefined
> (glibc `free` double-free abort + a device double-free). The only defense is
> the **global** `objcount` detector: after the second `destroy` the counter
> goes negative and the `atexit` hook (`objcount_check @0x9b17c0`) prints a
> `"double-freed"` diagnostic — a post-mortem, not prevention.

> **NOTE — `destroy` ≠ unload.** `destroy` frees the **host** object + its
> staging buffer; it emits **no** device instruction stream. Tearing down the
> kernels already loaded into Q7 IRAM/DRAM is the job of the *unload-sequence*
> generator ([`nrtucode-ll-load-unload.md`](./nrtucode-ll-load-unload.md), the
> `0x1095`-magic record stream) and the device-side
> [`external-lib-loader`](../firmware/pool/external-lib-loader.md)
> (`xtensa_unload`). `destroy`'s complete call set (`device_free`, `context_log`,
> `free`, `objcount_decrement`, plus the abort-path `fprintf`/`abort`) contains
> no `0x1095` producer. Correct idiom: **emit-unload-sequence (device) then
> destroy (host).**

---

## 10. `nrtucode_ll_set_friendly_name` `[HIGH·OBSERVED]`

Symbol `@0x9b2450` / `0x30a8e0`, **`0x46` bytes**.

```c
void nrtucode_ll_set_friendly_name(nrtucode_ll_t *ll, const char *name)
{
    nrtucode_context_t *ctx = ll->context;                /* mov (%rdi),%rdi @0x9b245b -- NO null guard */
    nrtucode_context_log(ctx, 4, "%s renamed %s",
                         ll->friendly_name, name);         /* old=&name(+0x20), new=name @0x9b2476 */
    strncpy(ll->friendly_name, name, 0x20);               /* n=32 @0x9b247b ; call strncpy @0x9b2486 */
    ll->friendly_name[0x20] = '\0';                       /* movb $0,0x40(%r14) @0x9b248b -- forced NUL */
}
```

The name lives in the **fixed inline buffer** at `ll+0x20` (no `strdup`, no
separate heap allocation) — the same `0x21`-byte region `create` initialised
with `snprintf("nrtucode_ll_t@%p")`; `set_friendly_name` overwrites it in place.
`strncpy(dst, name, 32)` then an **unconditional** `movb $0,+0x40` forces the
terminator, so the copy is safe even when `name` is ≥ 32 chars with no embedded
NUL — names longer than 32 chars are silently **truncated to 32 + NUL**. Cap =
33 bytes including NUL, matching the public-header contract.

> **GOTCHA — zero validation.** `set_friendly_name` has **no** NULL guard on
> either argument: it immediately derefs `ll->context` (so `ll==NULL` faults)
> and passes `name` straight to `%s`/`strncpy` (so `name==NULL` faults). It
> returns `void`, so it cannot signal a bad arg — it trusts the caller. This
> differs from `create`/`destroy`, which abort-guard their pointers.

---

## 11. `nrtucode_ll_get_library_size` `[HIGH·OBSERVED]`

Symbol `@0x9b24a0` / `0x30a930`, **`0x05` bytes**, byte-for-byte identical in
both libs (`48 8b 47 18 c3`):

```c
size_t nrtucode_ll_get_library_size(nrtucode_ll_t *ll)
{
    return ll->prelinked_size;          /* mov 0x18(%rdi),%rax ; ret */
}
```

Returns the qword at `ll+0x18` = `prelinked_size` = `OUT[0x0c] + OUT[0x10]` =
the **total prelinked image size as staged to the device** (the bytes actually
written to the device buffer). **Not** the host object size (constant `0x48`),
**not** the 16 MiB device buffer allocation. For a successfully-created
prelink-path `ll` the value is guaranteed in `[0, 0x10000]` (the cap, §7); for a
coretype-6 `ll` it is exactly `0`. No NULL guard (bare deref), caller-trusted.

---

## 12. Alloc / free symmetry — no leak `[HIGH·OBSERVED]`

| # | resource (create) | create site | freed by | balanced? |
| - | ----------------- | ----------- | -------- | --------- |
| 1 | host `ll` `malloc(0x48)` | `@0x9b1afd` | `destroy` `free(ll)` @`0x9b1de1` **and** every create error path | YES (host) |
| 2 | device staging buf `device_malloc(16 MiB)` → `ll+0x08` | `@0x9b1c4e/c5d` (size!=0) | `destroy` `device_free` slot `+0x08` @`0x9b1dc1`, gated on `+0x18!=0` | YES (gated by same field) |
| 3 | host `scratch1` `malloc(0x8000)` | `@0x9b1b4d` | inside `create` before return (all paths) | YES (transient) |
| 4 | host `scratch2` `malloc(0x2000)` | `@0x9b1b5e` | inside `create` before return | YES (transient) |
| 5 | `friendly_name[0x21]` (inline) | `@0x9b1be8` snprintf | implicit via `free(ll)` (row 1) | YES (inline) |

`objcount`: `create` `lock inc`@`0x9b17a0`; `destroy` `lock dec`@`0x9b17b0`.
**Balanced.** Every allocation `create` makes is released exactly once on the
normal lifecycle (rows 1+2 by `destroy`; rows 3+4 transiently inside `create`;
row 5 inline). No leak; no normal-path double-free.

---

## 13. Per-build (Maverick) delta `[HIGH·OBSERVED]`

`create`'s coretype switch **does** diverge between the two libs (§5: internal
lists `37`, shipped jump-table omits it). The **lifecycle-tail trio**
(`destroy` / `set_friendly_name` / `get_library_size`) does **not**:
byte-comparing internal vs shipped (after the `.text` Δ=0x1000 fixup),
`get_library_size` is byte-for-byte identical; `set_friendly_name` differs in 7
bytes and `destroy` in 23, all inside RIP-relative `disp32` / `call rel32`
operand fields (string refs + call targets) — pure link relocations. Every
opcode, ModRM, immediate (`cmpq $0,0x18`, slot `*0x8(%rax)`, `$0x4` severity,
`$0x20`/`$0x40` in the setter) is identical. **No per-build behavioral delta in
the destructor or accessors.**

---

## 14. Confidence summary

**HIGH · OBSERVED** (single-instruction-anchored, both libs cross-checked where
they agree): the `0x48` struct + all writers; `malloc 0x48`; the CPTC env
resolution; the shipped coretype set `{6,13,21,29}`; scratch sizes
`0x8000`/`0x2000`; the 16 MiB device buffer + 3-segment write order + `0x20`-byte
UCPL header; the 64 KiB cap; return codes `0/5/7/8/9`; the full `destroy`
teardown + free-list + `+0x18` device-free gate; `set_friendly_name` inline-buffer
mechanics; `get_library_size = ll->prelinked_size`; objcount balance; no
per-build delta in the tail trio.

**MED · INFERRED:** the `+0x08` "device\_memhandle" *naming* (the write is HIGH;
the memhandle role is inferred from the slot used + `destroy`'s free); the
`+0x10` "library *index*" semantics (value + store HIGH; "index 3 == CPTC
library" inferred from the env name); the `OUT`/UCPL field *names*
(offsets/lengths OBSERVED as the write triples; header/seg2/seg3 naming
inferred — full layout in #816).

**OPEN (out of this binary's reach):** the `pi_library` blob encoding and the
`prelink_relocate_lib @0x9b6160` relocation algorithm (#816 / the
[prelinker-ucpl](./prelinker-ucpl.md) lane); the opaque
`library_selector → blob` mapping inside `nrtucode_get_ext_isa_internal
@0x9b2b30`; the platform memhandle implementation behind device\_malloc/free.
