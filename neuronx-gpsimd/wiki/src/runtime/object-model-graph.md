# The nrtucode Object Model Graph

> **Scope.** This page reconstructs the **codec-side runtime object model** that
> lives entirely inside one host ELF — `libnrtucode_internal.so`, the CODEC half
> of the GPSIMD custom-op host library (`aws-neuronx-gpsimd-customop-lib`,
> v0.21.2.0). It documents the complete
> **context → core → memhandle → ll (loadable-library) → opset** object graph:
> per-object heap layout (field/offset/size), the create/destroy lifecycle of
> each (annotated against the real ctor/dtor `symbol@addr`), the ownership edges,
> the two platform function-pointer tables (`rw_impl` / `memhandle_impl`), the
> device-image build/load layer (`image_list` / `*_libs` getters /
> `get_memory_image` / `get_ext_isa` / `prelink`→UCPL), and one consolidated
> object-graph diagram. A senior C++/LLVM reimplementer rebuilding the host codec
> should be able to re-declare all four structs and re-derive every lifecycle
> from this page alone.
>
> **Binary.** `libnrtucode_internal.so` —
> `neuronx-gpsimd/extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/libnrtucode_internal.so`.
> ELF64 x86-64 DYN, 10,276,288 B, BuildID `9cbf78c6…e585fd`, **not stripped**
> (full `.symtab`). Every offset, address, struct size, and slot below was
> re-read **this session** with stock `objdump -d` / `readelf -rSW` / `nm` /
> `strings` / `c++filt`; the IDA sidecars (`*_structures.json`, `*_functions.json`,
> `*_strings.json`) were used to *locate*, the binary to *verify*.
>
> **Confidence/evidence tags.** Every claim carries `HIGH/MED/LOW` ×
> `OBSERVED` (read from the binary this session) / `INFERRED` / `CARRIED` (cited
> from a sibling page, not re-derived). Callouts use literal `QUIRK` / `GOTCHA` /
> `NOTE` / `CORRECTION`.

## 0. Section-offset map (read before any raw dump)

This binary's load segments carry a **non-uniform VA−fileoffset delta** —
confirm before `xxd`/`objdump` on a `.data`/`.data.rel.ro`-resident table:

| section | VMA | file offset | Δ (VMA − fileoff) | VMA==fileoff? |
|---|---|---|---|---|
| `.rodata` | `0x46b0` | `0x46b0` | `0x0` | yes |
| `.text` | `0x9b01a0` | `0x9af1a0` | `0x1000` | no |
| `.data.rel.ro` | `0x9b8cf0` | `0x9b6cf0` | `0x2000` | no |
| `.data` | `0x9ba4a8` | `0x9b74a8` | `0x3000` | no |

`HIGH / OBSERVED` (`readelf -SW`). **All addresses on this page are VAs.** For
`objdump -d` and `readelf -r`, VAs are used directly (the tools relocate). Only a
raw `xxd <file-offset>` of a `.data`/`.data.rel.ro` struct (e.g.
`plat_memhandle_dummy@0x9b8cf0`, `image_list@0x9b8d20`) must subtract the section
Δ — the relocations below are read straight from `readelf -r` (VA space) and need
no adjustment.

> **GOTCHA.** This Δ is **specific to `libnrtucode_internal.so`**. It is *not* the
> `ncore2gp` config-DLL `0x200000` delta and *not* zero. Over-generalising
> "VMA==fileoff" from `.text`/`.rodata` to `.data` reads the wrong bytes.

## 1. What this object model is

`libnrtucode_internal.so` exports the **`nrtucode_*` C ABI** (52 exported
`nrtucode_*` symbols — `nm -D … | rg -c ' nrtucode_'` → **52**, `HIGH/OBSERVED`).
It defines **four heap objects**, each a flat `malloc` block of **plain C** — *no
object carries a C++ vtable of its own*. Each self-identifies by a friendly-name
format string written at creation (`snprintf(buf,0x21,"<tag>@%p",self)`):

| object | `malloc` size | tag string (`.rodata`) | export prefix | ctor | dtor |
|---|---|---|---|---|---|
| `nrtucode_context_t` | **`0x28`** | `nrtucode_context_t@%p` (`@0x4a9f`) | `nrtucode_context_*` | `0x9b0290` | `0x9b03d0` |
| `nrtucode_core_t` | **`0x70`** | `nrtucode_core_t@%p` (`@0x50ad`) | `nrtucode_core_*` | `0x9b0640` | `0x9b0780` |
| `nrtucode_ll_t` | **`0x48`** | `nrtucode_ll_t@%p` (`@0x49ac`) | `nrtucode_ll_*` | `0x9b1a90` | `0x9b1da0` |
| `nrtucode_opset_t` | **`0x830`** | `nrtucode_opset_t@%p` (`@0x5548`) | `nrtucode_opset_*` | `0x9b24c0` | `0x9b25c0` |

`HIGH/OBSERVED` — sizes byte-exact from the `mov $size,%edi ; call malloc@plt`
prologue of each ctor (§§2–5); tag strings from `strings -t x` (`.rodata` offsets
in the table). The four runtime structs are **absent** from IDA's struct DB —
`*_structures.json` contains only the five ELF loader types (`Elf64_Sym`,
`Elf64_Rela`, `Elf64_Dyn`, `Elf64_Verneed`, `Elf64_Vernaux`); the runtime objects
are reconstructed from ctor/dtor code, not lifted from a recovered type.

The objects **never touch device memory directly.** All device I/O is routed
through two function-pointer tables the embedder (the production runtime
`libnrt.so` ucode layer) installs **into the context**:

* `rw_impl` (`context+0x00`) — 5-slot register/DRAM read-write + log table.
* `memhandle_impl` (`context+0x08`) — 5-slot device-memory alloc/free/r/w/addr.

A context created with no `memhandle_impl` keeps the built-in
`plat_memhandle_dummy` (`@0x9b8cf0`), whose `read`/`write`/`malloc` all return
errcode 8 ("not attached") — §6.

Object lifetimes are leak-tracked by a single process-global atomic `objcount`
(`@0x9bb564`, `.bss`); every tracked create increments, every tracked destroy
decrements, and an `atexit` hook prints a leak/double-free diagnostic — §8.

> **NOTE — naming reconciliation.** Two sibling naming conventions for the core's
> three address fields are reconciled here: the per-function carves' generic
> `opaque_handle_a/b` are, by the embedder's call site, `iram_base` (ctor arg3)
> and `apb_base` (ctor arg5); `core+0x20` is the device control-block
> `dram_base` (ctor arg4). This page adopts **`iram_base` / `dram_base` /
> `apb_base`** (`CARRIED` reconciliation; the field stores are `OBSERVED`).

## 2. `nrtucode_context_t` — `malloc(0x28)`, 5 qwords

**Ctor** `nrtucode_context_create@0x9b0290`; **dtor**
`nrtucode_context_destroy@0x9b03d0`. The `0x28` allocation and all five field
stores are `OBSERVED` byte-exact:

```
9b02c1: mov  $0x28,%edi              ; sizeof = 0x28
9b02c6: call 9b7b90 <malloc@plt>
9b02d8: mov  %r14,(%rax)             ; +0x00 rw_impl     = arg2
9b02db: lea  0x8a0e(%rip),%rax       ; -> 9b8cf0 plat_memhandle_dummy
9b02e2: mov  %rax,0x8(%r15)          ; +0x08 memhandle_impl = &dummy (default)
9b02e6: movq $0x0,0x10(%r15)         ; +0x10 userdata    = 0
9b02ee: mov  $0x200,%edi             ; scratch buffer size
9b02f3: call 9b7b90 <malloc@plt>
9b02fd: mov  %rax,0x20(%r15)         ; +0x20 log_scratch_buf = malloc(0x200)
9b0301: movq $0x200,0x18(%r15)       ; +0x18 log_scratch_size = 0x200
```

| off | sz | type | field | init / role | conf |
|---|---|---|---|---|---|
| `0x00` | 8 | `const rw_table*` | `rw_impl` | ctor arg2; the §6.1 5-slot reg/DRAM/log table. **Never NULL** (ctor aborts if `arg2==0`). `mov %r14,(%rax)`. | HIGH OBS |
| `0x08` | 8 | `const memhandle_table*` | `memhandle_impl` | `&plat_memhandle_dummy@0x9b8cf0` default; overwritten by `nrtucode_context_set_memhandle_impl@0x9b0390` (`mov %rsi,0x8(%rdi)`). The §6.2 5-slot device-memory table. | HIGH OBS |
| `0x10` | 8 | `void*` | `userdata` | `0`; get/set via `context_get_userdata@0x9b0430` (returns `ctx[+0x10]`) / `context_set_userdata@0x9b0470`. Opaque embedder cookie. | HIGH OBS |
| `0x18` | 8 | `uint64_t` | `log_scratch_size` | `0x200`; capacity of the `vsnprintf` scratch; grown by `realloc` when a formatted line exceeds it. | HIGH OBS |
| `0x20` | 8 | `char*` | `log_scratch_buf` | `malloc(0x200)`; **freed FIRST** in destroy. | HIGH OBS |

`sizeof = 0x28`, every byte accounted for. `HIGH/OBSERVED`.

### Lifecycle (annotated)

```c
// nrtucode_context_create @0x9b0290
int nrtucode_context_create(uint32_t api_level /*a1*/,
                            const rw_table *rw_impl /*a2*/,
                            nrtucode_context_t **out /*a3*/) {
    if (api_level != 3) return 4;             // ONLY api_level 3 allocates
    if (!rw_impl || !out) { fprintf(stderr,…); abort(); }  // hard API-misuse abort
    nrtucode_context_t *ctx = malloc(0x28);   if (!ctx) return 5;
    ctx->rw_impl        = rw_impl;            // +0x00
    ctx->memhandle_impl = &plat_memhandle_dummy; // +0x08 default
    ctx->userdata       = 0;                  // +0x10
    ctx->log_scratch_buf = malloc(0x200);     if (!ctx->log_scratch_buf){free(ctx);return 5;}
    ctx->log_scratch_size = 0x200;            // +0x18
    *out = ctx;
    nrtucode_objcount_increment();            // @0x9b17a0
    return 0;
}

// nrtucode_context_destroy @0x9b03d0
void nrtucode_context_destroy(nrtucode_context_t *ctx) {
    if (!ctx) abort();
    free(ctx->log_scratch_buf);               // +0x20 freed first
    free(ctx);
    nrtucode_objcount_decrement();            // @0x9b17b0
}
```

> **GOTCHA.** `context_destroy` does **not** free `rw_impl`/`memhandle_impl`
> (caller-owned const tables) and does **not** walk or destroy child cores / ll's
> / opsets — there is **no children list**. A context destroyed with live
> children leaks them; `objcount` catches the imbalance at exit (§8). `HIGH/OBSERVED`.

## 3. `nrtucode_core_t` — `malloc(0x70)`, the booted-engine handle

**Ctor** `nrtucode_core_create@0x9b0640`; **dtor**
`nrtucode_core_destroy@0x9b0780`. The `0x70` allocation and the eight field
stores are `OBSERVED`:

```
9b0675: mov  $0x70,%edi              ; sizeof = 0x70
9b067a: call 9b7b90 <malloc@plt>
9b0684: mov  %rbx,(%rax)             ; +0x00 context  = arg1 (owning ctx)
9b0687: movq $0x0,0x8(%rax)          ; +0x08 userdata = 0
9b068f: mov  %ebp,0x10(%rax)         ; +0x10 coretype = arg2 (u32)
9b0692: mov  %r13,0x18(%rax)         ; +0x18 iram_base = arg3
9b0696: mov  %r12,0x20(%rax)         ; +0x20 dram_base = arg4 (device CB base)
9b069a: mov  %r15,0x28(%rax)         ; +0x28 apb_base  = arg5
9b069e: movl $0x0,0x30(%rax)         ; +0x30 boot_state = 0 (NOT BOOTED)
9b06a5: movq $0x0,0x38(%rax)         ; +0x38 log_memhandle = 0
```

Ctor signature (`IDA` + embedder caller naming):
`nrtucode_core_create(context, coretype, iram_base, dram_base, apb_base, core_out)`.

| off | sz | type | field | role | conf |
|---|---|---|---|---|---|
| `0x00` | 8 | `context*` | `context` | owning ctx; the **only stored back-edge**; `core_get_context@0x9b0950` returns `core[0]`. | HIGH OBS |
| `0x08` | 8 | `void*` | `userdata` | per-core cookie; get/set via `core_get/set_userdata`. Embedder leaves NULL. | HIGH OBS |
| `0x10` | 4 | `uint32_t` | `coretype` | the `NRTUCODE_CORE_*` enum (§9); `core_get_coretype` returns `*(u32)(core+0x10)`. Gated against the NX_POOL mask `0x102020204` in dge/mailbox. | HIGH OBS |
| `0x14` | 4 | — | *pad* | — | MED INF |
| `0x18` | 8 | `uint64_t` | `iram_base` | ctor arg3 (device IRAM SOC window). Host-side write-only; consumed by the boot path. | HIGH OBS |
| `0x20` | 8 | `uint64_t` | `dram_base` | ctor arg4 = device **control-block** base. Every per-core device field (§3.2) is reached as `rw(ctx, dram_base+N, len, buf)` — DRAM-relative, **not** host-relative. | HIGH OBS |
| `0x28` | 8 | `uint64_t` | `apb_base` | ctor arg5 (APB/CSR window). Host-side write-only. | HIGH OBS |
| `0x30` | 4 | `uint32_t` | `boot_state` | `0`=NOT BOOTED, `1`=BOOTED_LEGACY. Set to 1 by `on_ucode_booted` after the claim handshake. Embedded assert `(core)->boot_state == BOOTED_LEGACY` gates logs/dge/mailbox. | HIGH OBS |
| `0x34` | 4 | — | *pad* | — | MED INF |
| `0x38` | 8 | `void*` | `log_memhandle` | `0` when logs disabled; set by `enable_logs_with_size_hint` to a `device_malloc`'d log ring; freed via `device_free` in destroy if `!=0`. | HIGH OBS |
| `0x40` | 4 | `uint32_t` | `log_buf_size` | `enable_logs`: rounded buffer size. | HIGH OBS |
| `0x44` | 4 | `uint32_t` | `log_read_cursor` | `enable_logs`: `0`; `print_logs` uses it as the consumed-bytes cursor. | HIGH OBS |
| `0x48` | `0x21` | `char[0x21]` | `friendly_name` | `snprintf(core+0x48,0x21,"nrtucode_core_t@%p",core)`; `set` does `strncpy(core+0x48,src,0x20)+NUL@core[0x68]`. | HIGH OBS |
| `0x69` | `0x07` | — | tail pad | `malloc(0x70)` round-up (`0x48+0x21=0x69 … 0x70`). | HIGH |

`sizeof = 0x70`, every byte accounted for. `HIGH/OBSERVED`.

### 3.1 Lifecycle (annotated)

```c
// nrtucode_core_create @0x9b0640
int nrtucode_core_create(nrtucode_context_t *ctx, uint32_t coretype,
                         uint64_t iram_base, uint64_t dram_base,
                         uint64_t apb_base, nrtucode_core_t **out) {
    if (!ctx || !out) abort();
    nrtucode_core_t *core = malloc(0x70);
    if (!core) { context_log(ctx, 5, "Failed to allocate nrtucode_core_t"); return 5; }
    core->context = ctx; core->userdata = 0; core->coretype = coretype;
    core->iram_base = iram_base; core->dram_base = dram_base; core->apb_base = apb_base;
    core->boot_state = 0; core->log_memhandle = 0;
    snprintf(core->friendly_name, 0x21, "nrtucode_core_t@%p", core);
    context_log(ctx, 4, "Initialized %s", core->friendly_name);
    *out = core; nrtucode_objcount_increment(); return 0;
}

// nrtucode_core_destroy @0x9b0780
void nrtucode_core_destroy(nrtucode_core_t *core) {
    if (!core) abort();
    nrtucode_core_disable_logs(core);
    nrtucode_context_t *ctx = core->context;
    if (core->log_memhandle)                       // +0x38
        ctx->memhandle_impl->device_free(ctx, core->log_memhandle);   // slot +0x08
    if (core->boot_state) {                         // +0x30
        uint32_t unclaim = 0x6099CB34;              // release magic
        ctx->rw_impl->write(ctx, core->dram_base + 0, 4, &unclaim);   // slot +0x08
    }
    context_log(ctx, 4, "Destroyed %s", core->friendly_name);
    free(core); nrtucode_objcount_decrement();      // @0x9b17b0 — CONFIRMED present
}
```

`HIGH/OBSERVED` — `core_destroy` calls `nrtucode_objcount_decrement@0x9b17b0`
(verified `call 9b17b0` at `0x9b07ee`); it releases the device claim and frees its
log ring but **does not** free its context (context outlives cores).

### 3.2 The device control block at `dram_base` (`core+0x20`) — *not* a host struct

These offsets are reached through `rw_impl` (read = slot+0x00, write = slot+0x08)
as `rw(ctx, dram_base+N, len, buf)`. They are the per-core **device-DRAM** layout
the host pokes, recovered from the `rw` call sites:

| `dram_base+` | sz | field | accessor |
|---|---|---|---|
| `0x00` | 4 | claim magic | `on_ucode_booted` reads; `0x6099CB34`=UNCLAIMED → writes `0x502B2DA1`=CLAIMED. |
| `0x04` | 4 | log buffer size | `enable_logs` writes rounded size. |
| `0x08` | 8 | log buffer ptr | `enable_logs` writes the device log-ring SOC addr (memhandle slot+0x20 translate). |
| `0x10` | 4 | log write cursor | `print_logs` reads (producer-advanced). |
| `0x28` | 4×4 | DGE mailbox[0..3] | `dge_mailbox` set/get r/w 4 u32 at `dram_base+0x28+4*i`; `get_dge_mailbox_addr` returns `dram_base+0x28`. |
| `0x38` | 8 | pc_bounds_lo | `enable_pc_bounds_check` / `get_pc_bounds`. |
| `0x40` | 8 | pc_bounds_hi | r/w via rw slot+0x00/+0x08, len 8. |

`HIGH/OBSERVED` (rw call sites). This is the core object's **device-side
projection**, not a host allocation.

### 3.3 The boot/claim handshake (`on_ucode_booted@0x9b0ab0`)

```c
uint32_t m; ctx->rw_impl->read(ctx, dram_base + 0, 4, &m);            // slot +0x00
if (m == 0x6099CB34 /*unclaimed*/) {                                  // cmp $0x6099cb34,%r9d @9b0aee
    m = 0x502B2DA1; ctx->rw_impl->write(ctx, dram_base + 0, 4, &m);   // claim; call *0x8(%rax) @9b0b4b
    core->boot_state = 1; return 0;                                   // BOOTED_LEGACY
} else if (m == 0x502B2DA1)                                           // cmp $0x502b2da1,%r9d @9b0af7
    log "Core is claimed by another nrtucode_core_t instance";
else
    log "Magic value `%x` mismatch (booted image is incompatible)";
return 8;
```

`HIGH/OBSERVED` (magics `0x6099cb34` / `0x502b2da1`, `call *0x8(%rax)`, the two
diagnostic strings, `boot_state` compared to 1 at `cmpl $0x1,0x30(%rdi)`@`0x9b0be6`).

> **NOTE.** "boot" is a **cooperative claim** on a device-DRAM magic word, not a
> code load. The IRAM image landing is done by the embedder's HAL *before* this.

## 4. `nrtucode_ll_t` — `malloc(0x48)`, the loadable-library (DKL) handle

"ll" = **loadable library** = a Dynamic-Kernel-Load (DKL) ext-ISA library staged
into device memory. **Ctor** `nrtucode_ll_create@0x9b1a90` (`mov $0x48,%edi`@
`0x9b1afd`); **dtor** `nrtucode_ll_destroy@0x9b1da0`.

| off | sz | type | field | role | conf |
|---|---|---|---|---|---|
| `0x00` | 8 | `context*` | `context` | ctor arg1 (owning ctx). `ll_get_*_sequence` asserts `ll->context == nrtucode_core_get_context(core)` — the ll and the core it loads onto **must share a context**. | HIGH OBS |
| `0x08` | 8 | `void*` | `device_memhandle` | `memhandle_impl->device_malloc` result (the staged image's device buffer); allocated in ctor when prelinked size>0, freed in dtor. | HIGH OBS |
| `0x10` | 8 | `uint64_t` | `flags/flavor` | resolved ucode-flavor / CPTC-decode flag; default = ctor arg4, or the `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE` env override. | HIGH OBS |
| `0x18` | 8 | `uint64_t` | `device_size` | prelinked size (IRAM+DRAM region totals); `0` until set; returned by `ll_get_library_size@0x9b24a0` (`ll[+0x18]`). | HIGH OBS |
| `0x20` | `0x21` | `char[0x21]` | `friendly_name` | `snprintf(ll+0x20,0x21,"nrtucode_ll_t@%p",ll)`; `ll_set_friendly_name` updates. | HIGH OBS |
| `0x41` | `0x07` | — | tail pad | round-up to `0x48`. | MED |

`sizeof = 0x48`. `HIGH/OBSERVED`.

### 4.1 Create flow — the device-image **build** step

```c
// nrtucode_ll_create @0x9b1a90
int nrtucode_ll_create(nrtucode_context_t *ctx, uint32_t coretype,
                       uint32_t flavor, uint64_t flag, nrtucode_ll_t **out) {
    uint64_t v16 = flag;                       // or env NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE
    ext_isa_desc desc;
    int r = nrtucode_get_ext_isa_internal(coretype, flavor, &desc, v16);  // §6.2
    if (r) return r;                           // miss -> propagate
    nrtucode_ll_t *ll = malloc(0x48);
    ll->context = ctx; ll->flags = v16; ll->device_size = 0;             // ll[3]=0
    if (coretype > 0x25) return 8;             // arch gate
    if (coretype in {13,21,29,37}) {           // Cayman/Mariana/Mariana+/Maverick: PRELINK
        void *bufIram = malloc(cayman_memory_bounds.iram_scratch_size);  // @0x9aaee0 +0x18
        void *bufDram = malloc(cayman_memory_bounds.dram_scratch_size);  //          +0x38
        if (prelink(desc.blob, &cayman_memory_bounds, ctx,
                    prelink_error_log_callback, bufIram, bufDram))        // §6.3
            { log "Failed prelink with status = %d"; … return 9; }
        ll->device_size = iram_total + dram_total;
        if (ll->device_size) {
            ll->device_memhandle =
                ctx->memhandle_impl->device_malloc(ctx, ll->device_size, 0x1000000); // slot +0x00, align 16M
            ctx->memhandle_impl->write_memhandle(ctx, ll->device_memhandle, …);      // slot +0x18: header(32B)+IRAM+DRAM
        }
        if (ll->device_size >= 0x10000)        // per-device cap
            { log "Prelinked library would be larger than the available buffer on device";
              device_free(…); … return 7; }
        free(bufIram); free(bufDram);
    } else if (coretype == 6 /*Sunda*/) {
        /* SHORT path: Sunda DKL is NOT host-prelinked here — name+log+objcount only */
    } else return 8;
    snprintf(ll->friendly_name, 0x21, "nrtucode_ll_t@%p", ll);
    context_log(ctx, 4, "Initialized %s", ll->friendly_name);
    *out = ll; nrtucode_objcount_increment(); return 0;
}

// nrtucode_ll_destroy @0x9b1da0
void nrtucode_ll_destroy(nrtucode_ll_t *ll) {
    if (!ll) abort();
    if (ll->device_size)                        // +0x18 != 0
        ll->context->memhandle_impl->device_free(ll->context, ll->device_memhandle); // slot +0x08
    context_log(ll->context, 4, "Destroyed %s", ll->friendly_name);
    free(ll); nrtucode_objcount_decrement();
}
```

`HIGH/OBSERVED` for the field stores, the arch gates, the env string, the prelink
call, and the "Failed prelink" / "Prelinked library would be larger" diagnostics.

### 4.2 Consumers — the **load** step

`ll_get_load_sequence` / `ll_get_unload_sequence` (and the shared
`ll_get_sequence_common@0x9b1fc0`) all assert `ll->context == core->context`,
require an 8-byte-aligned `instr_buf`, and emit the DMA/descriptor instruction
stream that copies `ll[+0x08]` onto the core. `ll_get_libraries_from_opcodes@0x9b1880`
maps an opcode set + coretype to the set of libraries needed (a bitmap over the
`*_libs` tables, §6.3). The instruction-stream **format** is carved per-function
elsewhere — `CARRIED`; here only the ll-object's role is `OBSERVED`.

## 5. `nrtucode_opset_t` — `malloc(0x830)`, the opcode/specialization set

**Ctor** `nrtucode_opset_create@0x9b24c0` (`mov $0x830,%edi`@`0x9b24df`,
`memset(opset+8,0,0x800)`@`0x9b2504`); **dtor** `nrtucode_opset_destroy@0x9b25c0`.

| off | sz | type | field | role | conf |
|---|---|---|---|---|---|
| `0x000` | 8 | `context*` | `context` | ctor arg1. | HIGH OBS |
| `0x008` | 8×256 | `void*[256]` | `opcode_bucket[op]` | one slot per opcode `0..255` (`opset+8+8*op`). NULL = opcode absent; set to a `calloc(1,0x100)` bucket by `add_instruction`. | HIGH OBS |
| `0x808` | `0x28` | `char[0x28]` | `friendly_name` | 40-byte inline name buffer spanning `+0x808..+0x830`. `create` `snprintf(opset+0x808,0x21,"nrtucode_opset_t@%p",opset)` (`add $0x808,%rdi`@`0x9b250c`); `set_friendly_name` forces the terminating NUL at `+0x828` (`movb $0x0,0x828(%r14)`@`0x9b294e`). | HIGH OBS |

`sizeof = 0x830 = 8 + 256*8 + 0x28`. `HIGH/OBSERVED`.

> **CORRECTION — `friendly_name` is `char[0x28]` (40 B), not `char[0x21]` + a `0x07` tail pad.**
> An earlier draft of this row typed the field as `char[0x21]` with a separate `0x07` pad
> at `+0x829`. The byte evidence (cross-checked on [`nrtucode-opset`](nrtucode-opset.md) §2)
> makes the whole `+0x808..+0x830` range the name buffer with **no** separate pad:
> `set_friendly_name` writes the forced terminating NUL at **`+0x828`** (`movb $0x0,0x828(%r14)`
> @`0x9b294e`), which is only reachable if the field extends past `+0x828`. The `0x21` is the
> **default-name `snprintf` cap** (`mov $0x21,%esi`@`0x9b251a`), not the field size. So
> `0x830 = 8 + 0x800 + 0x28`, all of the trailing `0x28` being the name buffer. *[HIGH/OBSERVED]*

Each present bucket is a separate `calloc(1,0x100)` = a 256-entry `uint8`
**specialization-presence** array indexed by spec id `0..255`.

```c
// add_instruction: op = instr[0];
if (!opset->opcode_bucket[op]) {                 // opset[1+op]
    opset->opcode_bucket[op] = calloc(1, 0x100);
    log "%s added opcode 0x%02x";
}
if (op == 0xF0) {                                // extended-opcode form
    uint8_t spec = instr[12];
    opset->opcode_bucket[op][spec] = 1;
    log "%s added extended opcode 0xf0 with specialization 0x%02x";
}
// queries: has_opcode = opset[1+op] != 0; has_specialization = bucket[sp];
// get_num_opcodes / get_num_specializations = SSE popcount over the 256 buckets.

// nrtucode_opset_destroy @0x9b25c0
void nrtucode_opset_destroy(nrtucode_opset_t *opset) {
    if (!opset) abort();
    context_log(opset->context, 4, "Destroyed %s", opset->friendly_name);
    for (int op = 0; op < 256; ++op) free(opset->opcode_bucket[op]);
    free(opset);
    /* NO nrtucode_objcount_decrement() */
}
```

> **QUIRK — opset is NOT leak-tracked.** `opset_destroy@0x9b25c0` calls only
> `context_log` + `free`×N + (`fprintf`/`abort` on the null guard) — **no
> `objcount_decrement`** (verified: no `call 9b17b0` in the body), and
> `opset_create` likewise omits `objcount_increment`. The opset is a
> lighter-weight, **untracked** dictionary object; the other three are tracked.
> `HIGH/OBSERVED`.

## 6. The two platform tables + the image build/load layer

### 6.1 `rw_impl` (`context+0x00`) — 5-slot register/DRAM read-write + log

> **CORRECTION — these are C function-pointer tables, not C++ `_ZTV` vtables.**
> `nm libnrtucode_internal.so \| rg -c '_ZTV'` → **0**: the binary contains
> **zero** C++ virtual tables. The two "platform vtables" are plain
> `static const struct { fnptr … }` tables. **There is no
> offset-to-top/typeinfo header**, so the `_ZTV…+0x10` rule does **not** apply:
> the table pointer *is* the symbol address and slot N is at **`symbol+8*N`**
> (the first fnptr is `+0x00`, not `+0x10`). Only `plat_memhandle_dummy` is
> shipped in this lib (the real impls live in the embedder); slot offsets for
> both tables are recovered from the `call *0xN(%rax)` call sites.

`rw_impl` slots (recovered from the `call *0xN(%rax)` sites):

| slot | signature | proven by |
|---|---|---|
| `+0x00` | `read(rw, soc_addr, len, out_buf) -> err` | `on_ucode_booted` reads `dram_base+0`; pc_bounds / log-cursor reads. |
| `+0x08` | `write(rw, soc_addr, len, in_buf) -> err` | claim write `call *0x8(%rax)`@`0x9b0b4b`; dge mailbox / pc_bounds / log-buf writes. |
| `+0x10` | `log_emit(rw, severity, msg_ptr, msg_len)` | `context_log` tail call `call *0x10(%rax)`@`0x9b0522`. |
| `+0x18` | `log_enabled(rw) -> bool` | `context_log` gate `call *0x18(%rax)`@`0x9b04c6`. |
| `+0x20` | aux/size-config(rw, …) | `enable_logs_with_size_hint` slot+0x20 call. `LOW` on exact semantics, offset `OBSERVED`. |

`HIGH/OBSERVED` for `+0x00`/`+0x08`/`+0x10`/`+0x18`; `MED` for `+0x20`.

### 6.2 `memhandle_impl` (`context+0x08`) — 5-slot device-memory table

`plat_memhandle_dummy@0x9b8cf0` relocations (`readelf -rW`, `OBSERVED`):

| slot | reloc target | `nm` symbol | dummy behaviour |
|---|---|---|---|
| `+0x00` | `0x9b1820` | `dummy_device_malloc` | `device_malloc(ctx,?,size,out)`; `*out=0`; log "memhandle platform implementation is required but is not attached"; **return 8**. |
| `+0x08` | `0x9b1850` | `dummy_device_free` | `device_free(ctx, handle)`; no-op. |
| `+0x10` | `0x9b1860` | `dummy_read_memhandle` | `read(ctx,handle,off,len,dst)` → 8. |
| `+0x18` | `0x9b1870` | `dummy_write_memhandle` | `write(ctx,handle,off,len,src)` → 8. |
| `+0x20` | — (NULL) | — | `device_addr/translate(ctx,handle) -> SOC addr`; **absent in dummy**. A real impl **must** supply it (used by `enable_logs` slot+0x20 and transitively by ll staging). |

`HIGH/OBSERVED` — four relocated slots at `0x9b8cf0/+0x8/+0x10/+0x18`, the fifth
(`+0x20`) carries no relocation (NULL). The dummy proves the **5-slot layout**
(`0x28`-wide table); slot `+0x20` is what distinguishes a real platform impl.

> **GOTCHA — vtable slot measurement.** Because there is no `_ZTV` header here, do
> **not** subtract `0x10`. Slot N of *either* platform table = `symbol + 8*N`,
> read straight from the `call *0x{N*8}(%rax)` encoding. (Applying the C++
> `_ZTV…+0x10` convention to these C tables would mis-index every slot by two.)

### 6.3 Device-image build/load layer

**`image_list@0x9b8d20`** — the per-coretype memory-image table.
`get_memory_image@0x9b2960` indexes `&image_list + 16*coretype` (`lea …# 9b8d20
<image_list>`@`0x9b29b5`). So `image_list` is an array of **16-byte** entries
`{uint64 flavor_count; struct flavor *arr}` keyed by coretype (`readelf -r`
confirms ptr-slot relocs at `0x9b8d28→0x9ba4b0`, `0x9b8d38→0x9ba4d8`,
`0x9b8d48→0x9ba500`, … in `.data` — a clean 16-byte stride). Each flavor record
is **40 bytes** (10 dwords; the walk steps `i += 10`):
`{uint32 flavor_id; void* getter_iram; void* getter_dram; void* getter_sram;
void* getter_extram}`. `get_memory_image(coretype, region in {0=IRAM,1=DRAM,
2=SRAM,3=EXTRAM}, flavor, &ptr, &size)` walks to `flavor_id==flavor`, then calls
the region getter. Flavor 0 (DEFAULT) resolves the `NEURON_UCODE_FLAVOR` env var
("debug"=2, "test"=3, else 1=PERF). Errcodes: 1 bad coretype, 2 no entry /
unknown flavor, 3 region absent. `HIGH/OBSERVED`.

**The five `*_libs` tables** — the ext-ISA (DKL) library getters.
`get_ext_isa_internal@0x9b2b30` selects a per-arch table by Q7_POOL coretype
(`lea … # … <…>` at `0x9b2bfd`…`0x9b2c31`, `OBSERVED`):

| coretype | table | VA |
|---|---|---|
| 6 (Sunda) | `sunda_libs` | `0x9b8f80` |
| 13 (Cayman) | `cayman_libs` | `0x9b8f90` |
| 21 (Mariana) | `mariana_libs` | `0x9b8fd0` |
| 29 (Mariana+) | `mariana_plus_libs` | `0x9b9010` |
| 37 (Maverick) | `maverick_libs` | `0x9b9050` |

Each table holds `{getter_a, getter_b}` fnptr **pairs** (stride `2*lib_index`);
the two getters fill the two halves of an ext-ISA library descriptor.
`get_num_ext_isa_libs@0x9b2c90` returns **4** for the Cayman+ set (mask
`0x2020202000`, `mov $0x4,%ecx`@`0x9b2cb3`) and **1** for Sunda (`mov $0x1,%eax`@
`0x9b2c97`). The `*_libs` tables carry `R_X86_64_64` relocs to the
`SUNDA_Q7_POOL_…EXTISA…_SO_get` symbols and RELATIVE relocs to the `*_get`
accessors. `HIGH/OBSERVED`.

**`prelink@0x9b5d60` → UCPL** — the ll device-image builder, called **only** from
`ll_create`. It takes the ext-ISA blob, the per-arch `*_memory_bounds`
(`sunda_memory_bounds@0x9aaea0` / `cayman_memory_bounds@0x9aaee0`, a 0x40-byte
`{iram_base, iram_size, iram_typeid, iram_scratch_size, dram_base, dram_size,
dram_typeid, dram_scratch_size}` record), two host scratch buffers, and the
context (for logging via `prelink_error_log_callback`), and emits a 32-byte header
beginning with magic **`0x204C504355` = ASCII `"UCPL "`** (`movabs
$0x204c504355,%rcx`@`0x9b5e1e`; the literal `UCPL ` string at `.rodata
0x9b4e20`), followed by aligned text/data sizes. Internals
(`prelink_load_lib@0x9b5e70` + `prelink_relocate_lib@0x9b6160` →
`relocate_op@0x9b6660`) consume the UCPL/rXtensa relocation table — that **format**
is documented in the dedicated relocation page (cross-ref
[The Ucode Relocation Consumer](./ucode-relocation-consumer.md)); here `prelink`
is only the ll object's build mechanism. `HIGH/OBSERVED` for the magic + call
graph; relocation table format `CARRIED`.

## 7. Ownership graph — who owns / refs / frees whom

Edge legend: `--owns-->` creator frees the target on its own destroy;
`--refs-->` non-owning back-pointer, never freed; `--uses-->` calls through, does
not store; `==binds==` cross-object invariant enforced by an assert.

```
embedder (libnrt ucode layer)
   | creates+owns  (1 nrtucode_context_t per device NeuronCore)
   v
nrtucode_context_t (0x28)
   |  +0x00 rw_impl        --refs--> [rw table; embedder-owned; NOT freed]
   |  +0x08 memhandle_impl --refs--> [mem table; default plat_memhandle_dummy; NOT freed]
   |  +0x10 userdata       --refs--> {embedder cookie; NOT freed}
   |  +0x20 log_scratch_buf --owns--> malloc(0x200)  (freed in context_destroy)
   |
   | (N children; context stores NO children list, does NOT cascade-destroy)
   +============> nrtucode_core_t (0x70)   core[+0x00] --refs--> context (back-edge)
   |                core[+0x38] --owns--> log ring (device_malloc; device_free in dtor)
   |                core --uses--> rw_impl (device CB I/O) + memhandle_impl (log ring)
   |                core holds the device CLAIM on dram_base+0 (released in dtor)
   |
   +============> nrtucode_ll_t (0x48)     ll[+0x00] --refs--> context
   |                ll[+0x08] --owns--> device image (device_malloc; device_free in dtor)
   |                ll --uses--> §6 image layer (get_ext_isa -> prelink/UCPL -> write_memhandle)
   |                ll ==binds== core:  ll->context == core->context  (asserted)
   |
   +============> nrtucode_opset_t (0x830) opset[+0x00] --refs--> context
                    opset[+8..] --owns--> 0..256 calloc(0x100) buckets (all freed in dtor)
                    NOT objcount-tracked
```

Invariants (all `OBSERVED`):

* **I1.** Every object stores `context` at `+0x00` as a non-owning back-pointer —
  the **only** inter-object pointer the four objects hold. There is no
  `context→children` list; the context cannot enumerate or cascade-destroy.
* **I2.** The embedder owns the destroy **order**: cores + ll's + opsets must be
  destroyed before their context. `objcount` catches violations at exit, but
  device buffers leak if `memhandle_impl` is torn down first.
* **I3.** `rw_impl` and `memhandle_impl` are const, embedder-owned, shared by
  every child of a context (reached via `core[0]`/`ll[0]`/`opset[0]`→ctx).
* **I4.** An ll and the core it loads onto **must share a context**
  (`ll->context == nrtucode_core_get_context(core)`, asserted).
* **I5.** `objcount@0x9bb564` tracks **context + core + ll only**; **opset is
  untracked** (§5 quirk).

## 8. Canonical bring-up / teardown + the leak tracker

```
BRING-UP                                            TEARDOWN (reverse, embedder-ordered)
 1 context_create(3,&rw_impl,&ctx)                  11 ll_get_unload_sequence(ll,core); ll_destroy(ll)  -> device_free image
 2 context_set_memhandle_impl(ctx,&mem)             12 core_print_logs(core); core_destroy(core)        -> device_free ring + unclaim
 3 context_set_userdata(ctx,{…})                    13 opset_destroy(opset)   (untracked)
 4 core_create(ctx,Q7_POOL,iram,dram,apb,&core)     14 context_destroy(ctx)   -> free log scratch + ctx
 5 core_set_friendly_name(core,"ND…NC….Q7_POOL…")   15 atexit -> nrtucode_objcount_check  asserts 0 outstanding
 6 core_on_ucode_booted(core)   claim @dram_base+0; boot_state=1
 7 core_enable_logs(core)       device_malloc ring -> core+0x38
 8 ll_create(ctx,coretype,flavor,flag,&ll)   get_ext_isa -> prelink(UCPL) -> device_malloc+write
 9 ll_get_load_sequence(ll,core,buf,…)       emit DMA load stream onto core
10 (run kernels; core_print_logs drains ring)
```

**Leak tracker / globals** (`HIGH/OBSERVED`, `nm` + `objdump`):

| symbol | VA | role |
|---|---|---|
| `objcount` | `0x9bb564` (`.bss`) | int32, interlocked inc/dec, `atexit`-checked. |
| `nrtucode_objcount_setup` | `0x9b1780` | zero `objcount`; `atexit(nrtucode_objcount_check)`; from the lib `.init`. |
| `nrtucode_objcount_increment` | `0x9b17a0` | interlocked++. |
| `nrtucode_objcount_decrement` | `0x9b17b0` | interlocked--. |
| `nrtucode_objcount_check` | `0x9b17c0` | `objcount>0` → "%i object(s) leaked, improper teardown … forget to call nrt_close or nrtucode_context_destroy?"; `<0` → "object(s) double-freed". |
| `plat_memhandle_dummy` | `0x9b8cf0` | default memhandle table (4 slots + NULL). |
| `image_list` | `0x9b8d20` | per-coretype `{count, flavor*}` table. |
| `sunda/cayman/mariana/mariana_plus/maverick_libs` | `0x9b8f80…0x9b9050` | ext-ISA getter-pair tables. |
| `sunda_memory_bounds` / `cayman_memory_bounds` | `0x9aaea0` / `0x9aaee0` | 0x40-byte prelink geometry. |
| `nrtucode_get_api_level` | `0x9b0260` | returns **3** (`mov $0x3,%eax ; ret`) — the ABI compat id the embedder asserts. |

## 9. The `NRTUCODE_CORE_*` coretype enum

The coretype (`core+0x10`) encodes (arch, engine). Two families are visible in
this lib's bitmasks (`HIGH/OBSERVED` for the values; `MED` for the contiguity
labels):

* **Q7_POOL** (mask `0x2020202040` in `opset_get_library_index`; `0x2020202000`
  in `get_num_ext_isa_libs` / `ll_create`) = `{6, 13, 21, 29, 37}`:
  6=SUNDA (NC-v2), 13=CAYMAN (NC-v3), 21=MARIANA (NC-v4), 29=MARIANA_PLUS
  (NC-v4+), 37=MAVERICK (NC-v5). Stride 8; `ll_create` prelinks 13/21/29/37,
  Sunda(6) short-path.
* **NX_POOL** (mask `0x102020204` in the dge/mailbox kind assert) =
  `{2, 9, 17, 25, 32}`: 2=SUNDA, 9=CAYMAN, 17=MARIANA, 25=MARIANA_PLUS, 32=MAVERICK.

> **QUIRK.** The Maverick NX_POOL bit is **32**, breaking the `+8` stride at the
> top of the mask `0x102020204` (the lower four are `+8`). `OBSERVED` from the
> bitmask; the Maverick label is `MED` (by contiguity with Q7_POOL).

This lib therefore supports **five arches** (Sunda/Cayman/Mariana/Mariana+/Maverick
= NC-v2/v3/v4/v4+/v5). The full per-engine enum table is carved on the dedicated
context/core detail pages and is not re-tabulated here.

## 10. Adversarial self-verify

The five strongest claims, re-challenged against the binary this session:

1. **`context = 0x28`** — `mov $0x28,%edi ; call malloc@plt`@`0x9b02c1`; five
   field stores `0x9b02d8…0x9b0301` exhaust the block. **Confirmed.**
2. **`core = 0x70`** — `mov $0x70,%edi`@`0x9b0675`; stores `0x9b0684…0x9b06a5`
   plus the `0x48`-offset `snprintf` name + tail pad → `0x70`. **Confirmed.**
3. **`ll = 0x48`** — `mov $0x48,%edi`@`0x9b1afd`; name `snprintf` at `ll+0x20`,
   `device_size` at `+0x18` → `0x48`. **Confirmed.**
4. **`opset = 0x830`** — `mov $0x830,%edi`@`0x9b24df`, `memset(opset+8,0,0x800)`@
   `0x9b2504`, name `snprintf` at `opset+0x808` (`add $0x808,%rdi`@`0x9b250c`) →
   `8 + 0x800 + 0x28 = 0x830`. **Confirmed.**
5. **Ownership tree + the two table identities** — `image_list` 16-byte stride
   (relocs `0x9b8d28/0x9b8d38/0x9b8d48`); `plat_memhandle_dummy` four relocated
   slots `0x9b8cf0/+8/+0x10/+0x18` → `dummy_device_malloc/free/read/write`, fifth
   slot NULL; `rw_impl` `+0x10`/`+0x18` proven by `context_log` calls; the
   `ll==core` context bind by the asserted string. **Confirmed.** Challenge that
   surfaced a real fix: the task framed both tables as C++ `_ZTV…+0x10` vtables —
   `nm \| rg -c '_ZTV'` returns **0**, so the `+0x10` header subtraction is wrong
   here; slots are at `symbol+8*N`. Captured as the **CORRECTION** in §6.

## 11. Cross-references

* Detail pages (per-object): *The nrtucode Context*
  [nrtucode-context.md](./nrtucode-context.md), *The nrtucode Core*
  [nrtucode-core.md](./nrtucode-core.md), *The nrtucode ll Create*
  [nrtucode-ll-create.md](./nrtucode-ll-create.md), *The nrtucode Opset*
  [nrtucode-opset.md](./nrtucode-opset.md).
* Prelink / relocation: [The Ucode Relocation Consumer](./ucode-relocation-consumer.md)
  (the UCPL/rXtensa relocation table format `prelink` consumes).
* Host struct census: the host-runtime struct census appendix (*Struct: Host
  Runtime Layouts*) collates these four sizes; the wider host execution-state
  struct census is in
  [Host Execution-State Struct Census](../appendix/struct-exec-state-census.md).

---

*Every offset, size, address, vtable slot, magic word, and string on this page
was re-verified against `libnrtucode_internal.so` this session
(`objdump -d` / `readelf -rSW` / `nm` / `strings` / `c++filt`). Confidence/evidence
tags are inline; `CORRECTION` callouts mark where the standard `_ZTV…+0x10`
methodology did not apply to this binary's C function-pointer tables.*
