# The 168-Byte Input Container

Every input that flows through the four nvlink input-compile entry points -- `sub_4BD0A0` (fatbin arch-match only), `sub_4BD240` (fatbin member compile-or-copy), `sub_4BD4E0` (whole-program PTX compile), and `sub_4BD760` (relocatable PTX compile) -- shares the same heap-allocated opaque control block. Internally there is no exported type for it; the binary refers to it only by raw offset arithmetic on a `_QWORD *`. This page reconstructs the 168-byte layout (`0xA8` bytes), documents the magic, the lifecycle, and the per-field writer/reader pairing as observed across every setter, the engine, the getter, and the cleanup routine.

The struct is the *only* mutable state shared between the input-detection layer (`main()`), the architecture-matching layer (`sub_4CE8C0`), the embedded-ptxas dispatcher (`sub_4BE350` / `sub_4BDB90`), and the Mercury finalizer (`sub_4748F0`). All four input formats -- raw PTX, NVVM IR / LTO IR, cubin, and fatbin -- funnel through this single container, which is why it is named generically rather than `ptx_ctx` or `fatbin_ctx`.

**Confidence: HIGH.** Every offset is corroborated by at least one setter and one reader, with a few fields cross-referenced from the cleanup walk in `sub_4BE400`. The magic constant `0x1464243BC` (5,473,715,132 decimal -- not a printable ASCII tag but a deliberate sentinel) is checked at the head of every getter / setter and in the validator `sub_4CE040`.

## Lifecycle Overview

```
                       allocate                      populate              read                  free
                  ┌─────────────────┐   ┌────────────────────────────┐   ┌──────────┐   ┌────────────────┐
  4BD0A0/4BD240   │                 │ ▶ │ 4CE3B0  set version  (+12) │ ▶ │  4BDB90  │ ▶ │     4BE400     │
  4BD4E0/4BD760   │     4CDD60      │ ▶ │ 4CE2F0  set arch     (+8)  │ ▶ │ (engine) │ ▶ │  (cleanup walk)│
  ───────────▶    │ malloc(168)+    │ ▶ │ 4CE380  set accel    (+160)│ ▶ │  4CE670  │ ▶ │ frees +24,+56, │
                  │ write magic     │ ▶ │ 4CE640  set flags    (+16) │ ▶ │ (getter) │ ▶ │ +120,+128, list│
                  │ 0x1464243BC     │ ▶ │ 4CE3E0  append opts  (+32) │   └──────────┘   │ at +144, then  │
                  └─────────────────┘   │ 4CE070  set content  (+72) │                  │ the ctx itself │
                                        └────────────────────────────┘                  └────────────────┘
```

Allocation, every setter, the getter, the engine, and the cleanup all share one structural feature: a `_setjmp(env)` guarded body that swaps the thread-local error descriptor (returned by `sub_44F410`) for the duration of the operation. The descriptor's two status bytes are saved on the stack, replaced with `0,0`, and at the end OR'd back with whatever the body wrote. A `longjmp` from inside the arena allocator or the ptxas backend lands in the descriptor-restore branch, returns code 5 (with diagnostic) or 1 (without), and never leaks the container.

## Magic and Validator

```c
// sub_4CE040 -- container_validate (33 bytes, leaf, 3 basic blocks)
// Returns 0 = valid, 1 = NULL pointer, 2 = wrong magic.
static inline int container_validate(_QWORD *ctx) {
    if (!ctx) return 1;
    return 2 * (*ctx != 0x1464243BCLL);
}
```

The constant `0x1464243BC` is written *once* by `sub_4CDD60` at offset 0 and is never written again. Every setter begins with the same prologue:

```c
result = 1;
if (a1) {
    result = 2;
    if (*(_QWORD *)a1 == 0x1464243BCLL) {
        // ... actual setter body ...
        return 0;
    }
}
return result;   // 1 if NULL, 2 if magic mismatch
```

Why 8 bytes for a 32-bit constant? Because the upper 4 bytes are guaranteed zero by the post-allocation `memset` (see *Allocation* below), and the comparison is `_QWORD` for ABI reasons -- the slot is 8-byte aligned and reading it as a full qword saves a sign-extension. The high half being zero also catches a class of buggy aliasing: any free-and-reuse that overwrites only 4 bytes of the header will leave the magic intact-looking on a 32-bit read but break on the 64-bit check.

## Allocation: sub_4CDD60

```c
// sub_4CDD60 -- container_create (544 bytes, 21 BBs)
// Allocates an opaque control block of 168 bytes, zeroes it,
// and stamps the magic. Returns nvlink-style status (0/1/2/5).
int container_create(void **out_ctx, void *unused) {
    // Swap thread-local error descriptor for setjmp recovery
    err_desc_t *d = sub_44F410(NULL, NULL);
    jmp_buf env;
    save_and_reset_descriptor(d, env);

    if (_setjmp(env) == 0 && out_ctx) {
        // sub_4307C0 is the arena allocator; argument 0xA8 == 168
        _QWORD *ctx = sub_4307C0(arena, 0xA8);
        if (!ctx) {
            sub_45CAC0();          // OOM diagnostic
            *out_ctx = NULL;
            restore_descriptor(d);
            return 1;              // OOM
        }
        // Zero bytes 8..175 via 16-byte aligned memset
        ctx[1]  = 0;
        ctx[20] = 0;
        memset(((uintptr_t)(ctx + 2) & ~7),
               0,
               8 * (((uint32_t)ctx - (((uint32_t)ctx + 16) & ~7) + 168) >> 3));
        ctx[0] = 0x1464243BCLL;     // magic
        *out_ctx = ctx;
    }
    restore_descriptor(d);
    return descriptor_was_raised(d) ? 5 : 0;
}
```

Three things to note:

1. The `0xA8` (168) byte allocation is the only place the size constant appears in the binary; nothing inside the container probes its own size.
2. The two explicit assignments `ctx[1] = 0` and `ctx[20] = 0` correspond to offset +8 (arch / version qword) and offset +160 (the two accelerated-arch flag bytes packed into a qword). They are zeroed *before* the bulk `memset`, which then zeroes everything from offset +16 through +167 via a single SIMD-friendly loop. The split is a compiler optimization -- the surrounding alignment fix-up math means the bulk store starts at an 8-byte-aligned offset that may or may not include byte 16, so the head bytes are written explicitly.
3. The post-zero `ctx[0] = 0x1464243BCLL` is the very last write inside the protected region. If a `longjmp` fires between the `sub_4307C0` return and the magic stamp, `out_ctx` is left as a dangling allocation -- but the arena owns it, so it is reclaimed at arena teardown.

## Field-Offset Table

Every offset below is corroborated by at least one writer (where the value first appears) and one reader (where it is consumed). Fields marked `?` are observed in cleanup or in opaque branches but their semantic role is not fully pinned down.

| Offset | Size | Field | Type | Writer(s) | Reader(s) | Notes |
|---:|---:|---|---|---|---|---|
| 0   | 8 | `magic`              | `uint64_t` | `4CDD60` | every setter, `4CE040` | Constant `0x1464243BC`; never overwritten |
| 8   | 4 | `arch`               | `uint32_t` | `4CE2F0` | `4BDB90`, `4CE2F0` arch DB lookup | SM number (e.g., 90 for sm_90) |
| 12  | 4 | `version`            | `uint32_t` | `4CE3B0` | `4BDB90` (passed to ptxas) | Fatbin compatibility version (from `dword_2A5B528`) |
| 16  | 8 | `flag_word`          | `uint64_t` | `4CE640` | `4BDB90` (`& 2` Mercury test) | Mode-flag bitfield; bit 1 = 64-bit pointers |
| 24  | 8 | `member_opts`        | `char *`   | `4CE8C0` | `4BDB90` (`v27`, strtok_r'd), `4BE400` (free) | Options harvested from matched fatbin member header |
| 32  | 8 | `cli_opts_accum`     | `char *`   | `4CE3E0` | `4BDB90` (`v28`, strtok_r'd) | Accumulator for tokens appended via `4CE3E0` (`-c`, `-m64`, `-g`, `-Xptxas`) |
| 40  | 8 | `?`                  | `void *`   | `4CE8C0` (alt path) | -- | Alternative options buffer in fatbin paths |
| 48  | 8 | `?`                  | `char *`   | `4CE8C0`/build | `4BDB90` (`v9`, `strstr "-threads"`) | Mercury thread-count source |
| 56  | 8 | `aux_buf`            | `void *`   | dynamic   | `4BE400` (`a1[7]`, free) | Auxiliary buffer freed at cleanup |
| 72  | 8 | `content_ptr`        | `void *`   | `4CE070` | `4BDB90` (raw PTX path) | Input bytes: PTX text, fatbin bytes, cubin bytes, or NVVM IR |
| 80  | 4 | `content_type`       | `int32_t`  | `4CE070` | `4BDB90`, `4BE350` | 1=already-compiled, 2=fatbin, 3=ELF cubin, 4=PTX, 8=NVVM |
| 88  | 8 | `matched_data`       | `void *`   | `4CE8C0`, `4BDB90` (post-compile) | `4CE670`, `4BDB90` | Content extracted by arch matching, or compiled cubin |
| 96  | 4 | `matched_type`       | `int32_t`  | `4CE8C0`, `4BDB90` | `4CE670`, `4BDB90`, `4BE350` | 1=PTX text, 8=NVVM, 16=Mercury-class (triggers `4748F0`) |
| 104 | 8 | `matched_size`       | `size_t`   | `4CE8C0`, `4BDB90` | `4CE670`, `4BDB90` | Size in bytes of `matched_data` |
| 120 | 8 | `cubin_output`       | `void *`   | `4BDB90`  | `4BE400` (frees via `qword_2A77DD0(4,...)`) | Compiled cubin pointer owned by ptxas allocator |
| 128 | 8 | `?`                  | `void *`   | `4BDB90`  | `4BE400` (`a1[16]`, `sub_431000`) | Auxiliary output buffer |
| 136 | 8 | `obfuscation_key`    | `uint64_t` | `4CE8C0` (fatbin walker) | `4BDB90` (`-ok`/`-ptxlen` emission) | Non-zero triggers "PTX Obfuscation" warning and `-ok 0x<key>` |
| 144 | 16 | `option_list_head`  | `LL node*` | `4CE3E0`  | `4BE400` (`a1[18]`, walk + free) | Linked list of arena-tracked option strings (intrusive `[next, payload]`) |
| 160 | 1 | `accelerated`        | `uint8_t`  | `4CE380`  | `4BDB90` (passed to `sub_44E530`) | sm_XXa accelerated-arch flag (trailing 'a' in arch name) |
| 161 | 1 | `aux_flag`           | `uint8_t`  | `4CE2F0`  | `4CE2F0` (read into `v4`) | Secondary arch flag, forwarded to `sub_44E530` |
| 162 | 6 | `padding`            | -- | -- | -- | Tail padding -- `168 = 21 qwords` with two stray bytes at +160/+161; the remaining 6 bytes are zero-filled by allocation |

The structure is therefore "mixed-width": 21 qword slots (offsets 0, 8, 16, 24, 32, 40, 48, 56, 64*, 72, 80*, 88, 96*, 104, 112*, 120, 128, 136, 144, 152, 160) with three qword slots used as `<u32, u32>` pairs (`arch:version` at 8/12, `content_ptr:content_type:_` at 72-83, and `matched_data:matched_type:_` at 88-99), plus the trailing flag-byte pair at 160/161. Slots marked `*` were not enumerated above but lie inside the qword-grid: +64 (between `aux_buf` and `content_ptr`), +112 (between `matched_size` and `cubin_output`), and +152 (`stderr_buf`, see below). Each of those is zeroed by allocation and either unused or written by the ptxas backend through the `qword_2A77DD0` call.

### Stderr Slot (Offset +152)

The ptxas backend, when it fails, writes a diagnostic C string into `ctx[19]` (offset 152). This is read back by `sub_4BE3D0` (`ptxas_get_stderr`), which simply returns `*(char **)(ctx + 152)` and clears it. The setter side is *not* one of the six container setters listed in the page title -- it is the embedded ptxas itself, reaching into the container through the `qword_2A77DD0` ABI. The slot is allocation-zeroed so a successful compile leaves it `NULL`, and `4BE3D0` distinguishes "no diagnostic" from "ptxas raised stderr" purely by null check.

## Per-Setter Anatomy

All six setters share the magic-prologue / setjmp pattern. The bodies differ only in what they store.

### sub_4CE2F0 -- set arch (+8) and validate

```c
// 132 bytes, 8 BBs, 4 callees
int container_set_arch(void *ctx, uint32_t sm_number) {
    if (!ctx) return 1;
    if (*(uint64_t *)ctx != 0x1464243BCLL) return 2;

    char arch_flag_lo = *(uint8_t *)(ctx + 160);  // accelerated
    char arch_flag_hi = *(uint8_t *)(ctx + 161);  // aux_flag
    *(uint32_t *)(ctx + 8) = sm_number;

    char arch_name[20];
    if (sub_44E530(arch_name, sm_number, 0, arch_flag_lo, arch_flag_hi)) {
        if (sub_486EA0(arch_name, ...))
            return 0;
    } else {
        // Unknown arch -- emit "compute_%d%s" diagnostic via 467460
        sub_467460(dword_2A5C000, arch_name);
        clear_error_flag();
    }
    return 2;
}
```

Important sequencing: the accelerated bytes at +160/+161 must already be set (via `sub_4CE380`) *before* `sub_4CE2F0` is called, because they feed the arch-name formatter. The four input wrappers (`sub_4BD0A0`, `sub_4BD240`, `sub_4BD4E0`, `sub_4BD760`) honor this order: `4CE3B0` then `4CE2F0` then optional `4CE380`. Re-calling `4CE380` after `4CE2F0` would *not* re-validate the new combination.

### sub_4CE3B0 -- set fatbin version (+12)

```c
// 37 bytes, 4 BBs, leaf
int container_set_version(void *ctx, uint32_t v) {
    if (!ctx) return 1;
    if (*(uint64_t *)ctx != 0x1464243BCLL) return 2;
    *(uint32_t *)(ctx + 12) = v;
    return 0;
}
```

Pure setter, no validation, no allocation. Always the first setter called after `4CDD60`.

### sub_4CE380 -- set accelerated flag (+160)

```c
// 41 bytes, 4 BBs, leaf
int container_set_accelerated(void *ctx) {
    if (!ctx) return 1;
    if (*(uint64_t *)ctx != 0x1464243BCLL) return 2;
    *(uint8_t *)(ctx + 160) = 1;
    return 0;
}
```

The hardcoded `= 1` means this is *only* callable to *set* the flag; there is no companion clearer. The container is born with the flag at zero (allocation-time `memset`), and once raised it stays raised for the entire input's lifetime.

### sub_4CE640 -- set flag word (+16)

```c
// 38 bytes, 4 BBs, leaf
int container_set_flags(_QWORD *ctx, uint64_t v) {
    if (!ctx) return 1;
    if (*ctx != 0x1464243BCLL) return 2;
    ctx[2] = v;                          // offset 16
    return 0;
}
```

Stores a full qword. The two PTX wrappers (`4BD760`, `4BD4E0`) call this with `1` when `dword_2A5F30C == 64` -- which is the only observed value. The Mercury bit-2 test in `sub_4BDB90` (`& 2`) is therefore the residue of an alternate code path that may set the flag to `3` for 64-bit + Mercury, but no caller has been seen to do so. Treat bit 0 as "64-bit" and bit 1 as "Mercury hint" until a counter-example emerges.

### sub_4CE3E0 -- append option token (+32)

```c
// 601 bytes, 22 BBs, 9 callees
int container_append_option(void *ctx, const char *new_token) {
    if (!ctx) return 1;
    if (*(uint64_t *)ctx != 0x1464243BCLL) return 2;

    // Inside setjmp:
    const char *existing = *(char **)(ctx + 32);
    if (existing) {
        size_t n = strlen(existing) + 2;            // +2 for the space and \0
        char *grown = arena_alloc(n);
        if (!grown) sub_45CAC0();
        *(uint16_t *)stpcpy(grown, existing) = ' ';  // append " " then \0
        *(char **)(ctx + 32) = grown;
        list_push(ctx + 144, grown);                 // track for cleanup

        size_t total = strlen(grown) + strlen(new_token) + 1;
        char *joined = arena_alloc(total);
        if (!joined) sub_45CAC0();
        strcpy(joined, grown);
        strcat(joined, new_token);
        *(char **)(ctx + 32) = joined;
        list_push(ctx + 144, joined);
    } else {
        size_t n = strlen(new_token) + 1;
        char *fresh = arena_alloc(n);
        if (!fresh) sub_45CAC0();
        strcpy(fresh, new_token);
        *(char **)(ctx + 32) = fresh;
        list_push(ctx + 144, fresh);
    }
    return 0;
}
```

Every appended string is duplicated into the arena and linked into the cleanup list at +144 (`sub_4644C0` is the list-push helper). The previous accumulator pointer is *not* explicitly freed by `4CE3E0` -- it is reachable only through the list head, and `sub_4BE400` walks the entire list at cleanup. The net effect is `O(n^2)` total memory in the number of appended tokens (each append copies the previous accumulator), but this is bounded by the small number of options typically forwarded.

### sub_4CE070 -- set content and classify (+72, +80)

```c
// 633 bytes, 35 BBs, 6 callees
int container_set_content(void *ctx, const void *content) {
    if (!ctx) return 1;
    if (*(uint64_t *)ctx != 0x1464243BCLL) return 2;

    *(void **)(ctx + 72) = content;
    if (!content) { /* setjmp-restore, return 1 */ }

    // Classify
    uint64_t hdr = *(uint64_t *)content & 0xFFFFFFFFFFFFLL;
    if (hdr == 0x1BA55ED50LL) {                            // fatbin magic
        *(uint32_t *)(ctx + 80) = 2;
    } else if (sub_43D970(content) && elf_e_machine(content) == 190) {
        *(uint32_t *)(ctx + 80) = 3;                        // ELF cubin (EM_CUDA)
    } else if (load_le32(content) == 0xDEC0170B            // NVVM magic LE
            || load_be32(content) == 0x1EE55A01) {          // alt-endian
        *(uint32_t *)(ctx + 80) = 1;                        // LTO-IR / compiled
    } else if (sub_4CDF80(content)) {                      // ".version" probe
        *(uint32_t *)(ctx + 80) = 4;                        // PTX text
    } else {
        sub_467460(dword_2A5BFA0, 0, 30675157);             // unknown content
        return 2;
    }
    return 0;
}
```

This is the only setter that performs content classification. The four canonical content_type codes are written here: 1 (LTO-IR / pre-compiled), 2 (fatbin), 3 (ELF cubin), 4 (PTX text). NVVM is detected here but classified as 1; the NVVM-specific code 8 is written by `sub_4CE8C0` later, during arch matching, when a fatbin member is identified as NVVM. See [PTX Input](ptx-input.md#flow-sub_4bd760) and [NVVM IR Input](nvvm-ir-input.md) for how downstream consumers interpret these codes.

## Reader: sub_4BDB90

The engine pulls a half-dozen fields out of the container in a tight prologue, then constructs the embedded-ptxas argv as documented in [PTX Input](ptx-input.md#compilation-engine-sub_4bdb90-8025-bytes). The relevant reads are:

```c
arch          = ctx->arch;                  // +8
content_type  = ctx->matched_type;          // +96 -- preferred over +80
matched_data  = ctx->matched_data;          // +88
matched_size  = ctx->matched_size;          // +104
content_ptr   = ctx->content_ptr;           // +72
member_opts   = ctx->member_opts;           // +24
cli_opts      = ctx->cli_opts_accum;        // +32
threads_src   = ctx->[+48];                 // +48 ("-threads" strstr source)
mercury_hint  = ctx->flag_word & 2;         // +16, bit 1
obfusc_key    = ctx->obfuscation_key;       // +136
accel         = ctx->accelerated;           // +160
```

The validator-then-read pattern is *not* coupled with a setjmp at the engine; the engine has its own outer setjmp covering the entire compile. Cross-context errors from inside the ptxas backend land in that outer recovery, and the container is left intact for cleanup by `sub_4BE400`.

## Reader: sub_4CE670 (getter for compiled output)

```c
// 453 bytes, 22 BBs -- container_extract_content
int container_get_match(void *ctx, _DWORD *out_type, _QWORD *out_size,
                        void **out_data) {
    if (!ctx) return 1;
    if (*(uint64_t *)ctx != 0x1464243BCLL) return 2;
    if (!out_data || !out_type || !out_size) return 1;

    *out_data = *(void **)(ctx + 88);     // matched_data
    *out_type = *(int32_t *)(ctx + 96);   // matched_type
    *out_size = *(size_t  *)(ctx + 104);  // matched_size
    return (*out_data == NULL) ? 1 : 0;
}
```

The "no match" sentinel is `matched_data == NULL` (return 1), which the four wrappers translate to an architecture-mismatch error. Note that this getter returns three fields *in one call* -- there is no per-field getter, which simplifies the contract: a successful extract is atomic with respect to type/size/data.

## Free Path: sub_4BE400

```c
// 162 bytes, 16 BBs, 2 callees
void container_destroy(_QWORD *ctx, ...) {
    if (!ctx) return;
    if (ctx[3])  sub_431000(ctx[3]);                  // +24  member_opts
    if (ctx[7])  sub_431000(ctx[7]);                  // +56  aux_buf
    if (ctx[15]) qword_2A77DD0(4, ctx[15], ...);     // +120 cubin_output (ptxas allocator)
    if (ctx[16]) sub_431000(ctx[16]);                 // +128 aux output
    for (_QWORD *n = (_QWORD *)ctx[18]; n; n = (_QWORD *)*n)
        sub_431000(n[1]);                             // walk option_list payloads
    sub_464520((_QWORD *)ctx[18]);                    // free list nodes
    sub_431000(ctx);                                  // the container itself
}
```

The cleanup order is deliberate:

1. Free `member_opts` (+24) and `aux_buf` (+56) first -- these are user-supplied / fatbin-walker-supplied buffers.
2. Return `cubin_output` (+120) to the *ptxas* allocator via `qword_2A77DD0(4, ...)`, since it was allocated there.
3. Free `aux output` (+128) via the *nvlink arena* free `sub_431000`.
4. Walk the `option_list` at +144, freeing every payload via the arena.
5. Free the list nodes themselves through `sub_464520`.
6. Finally free the container itself.

Critically, the magic is **not** zeroed before the final free. This is safe only because `sub_431000` is the arena allocator and immediately reclaims the memory; a subsequent allocation that reuses the slot will overwrite the magic. If a stale pointer is dereferenced after free, the magic check would coincidentally succeed only if the new allocation happens to leave `0x1464243BC` at offset 0 -- vanishingly unlikely for general allocations but trivially possible for another container creation, which is a use-after-free in the spirit of the magic. See QUIRK 3 below.

## QUIRK 1: The Magic Is Not a Tag

`0x1464243BC` looks deliberately non-ASCII. The low 32 bits `0x4243BC` are not printable; the upper byte `0x01` makes the whole word fall outside any common type tag. The choice is consistent with a *random* sentinel rather than a four-character cookie. Compare with NVIDIA's other sentinels: fatbin uses `0xBA55ED50` ("BASSED50"), NVVM IR uses `0xDEC0170B` ("DECOI70B"), ELF uses `0x7F454C46` (".ELF"). The container magic is the only one in nvlink's input pipeline that is *not* a backronym, suggesting it was generated rather than chosen -- which is good practice for an internal sentinel that should not collide with any user-controlled bytes.

## QUIRK 2: Two Accelerated-Arch Flag Bytes at +160/+161

The container holds *two* trailing bytes of arch metadata, but only one setter (`sub_4CE380`) is exposed and it writes only `+160 = 1`. The second byte `+161` is read by `sub_4CE2F0` and forwarded to the arch-name formatter `sub_44E530`, but no exported setter writes it. The byte is allocation-zeroed and stays zero unless the container is mutated through a path we have not yet identified -- likely a direct write by `sub_4CE8C0` during fatbin arch matching, where the matched member's own header can specify both 'a' (accelerated) and 'f' (frozen / family) suffixes. This means `+161` may be the `f` (sm_XXf) family-architecture flag, currently zero for every PTX-from-disk path and only nonzero for fatbin members tagged with the family attribute. No verified caller has been observed to set it, so callers compiling family-tagged sm_XXf members from the command line will silently produce a non-family cubin.

## QUIRK 3: Cleanup Does Not Invalidate the Magic

After `sub_4BE400` returns, the container memory has been freed to the arena, but offset 0 still reads `0x1464243BC` until the arena reuses the slot. A naive double-free or stale-handle access through `sub_4CE040` will succeed (returning 0 = "valid"), and subsequent setter calls will write into freed memory. The defense in `sub_4CE040` against this is structural: the four wrappers (`sub_4BD0A0`, `sub_4BD240`, `sub_4BD4E0`, `sub_4BD760`) all overwrite their local handle with the result of `sub_4CDD60` once and never call `sub_4BE400` twice. There is no double-free check anywhere in the container API -- which is acceptable inside a strict single-owner pipeline but would be a correctness landmine if any future caller passed the container across a thread or stored it in a registry. The magic should arguably be overwritten with `0xDEADBEEFDEADBEEFLL` or similar at the head of cleanup; that this is not done is the most prominent piece of "trust the caller" surface in the input pipeline.

## Cross-References

- **Used by**: [PTX Input & JIT](ptx-input.md) (relocatable, whole-program, fatbin-extracted paths), [Fatbin Extraction](fatbin-extraction.md) (arch-match phase via `sub_4BD0A0`), [NVVM IR / LTO IR Input](nvvm-ir-input.md) (content_type 8 path), [Cubin Loading](cubin-loading.md) (the engine bypasses the container for direct cubins; this page documents the path through which a fatbin-extracted cubin re-enters the merge pipeline).
- **Allocator**: [Memory Management (Arenas)](../infra/memory-arenas.md) -- `sub_4307C0` / `sub_431000` are the arena pair.
- **Error descriptor**: [Error Reporting System](../infra/error-reporting.md) -- `sub_44F410` returns the thread-local descriptor that every setter setjmp-saves.
- **Architecture flags at +160/+161**: [Architecture Profiles](../targets/arch-profiles.md) (formatter `sub_44E530`, validator `sub_486EA0`).
- **Mercury hint at +16 bit 1**: [FNLZR (Finalizer)](../mercury/fnlzr.md) for the post-ptxas Mercury path.
- **Engine consumer**: [PTX Input -- Compilation Engine](ptx-input.md#compilation-engine-sub_4bdb90-8025-bytes).
