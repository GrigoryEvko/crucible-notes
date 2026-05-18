# PTX Input & JIT

When nvlink encounters PTX (Parallel Thread Execution) assembly source as input -- either as a standalone `.ptx` file on the command line, as a type-1 member extracted from a fatbin container, or as the text output of a cicc-LTO whole-program compilation -- it cannot merge the text directly into the output ELF. PTX is architecture-neutral assembly; it must be compiled to SASS machine code for a specific SM target before linking. nvlink handles this by invoking the embedded ptxas backend (the same compiler backend that the standalone `ptxas` tool uses) entirely in-process, through a single function-pointer entry point at `qword_2A77DD0` (bound to `sub_52E060` by `sub_4FFC30`). The resulting cubin is then fed back into the normal merge pipeline as if it had been a cubin input from the start.

This page documents the complete path: PTX detection, file loading, the three compilation entry points (relocatable, whole-program, and fatbin-embedded), the compilation context object, the argv construction, option forwarding, embedded ptxas invocation, return-code translation, Mercury post-processing, and error handling.

**Confidence: HIGH.** Every function referenced here has been decompiled and cross-checked against `main()` call sites, and the literal string operands (`"-c"`, `"-g"`, `"-m64"`, `"-m32"`, `"-arch"`, `"--input-as-string"`, `"-ok"`, `"-ptxlen"`) have been extracted from the `.rodata` section of the binary. The 168-byte container field offsets are verified against setter, getter, and cleanup paths.

## Key Functions

| Address | Name | Role |
|---|---|---|
| `sub_4CDF80` | `is_ptx` | Detects PTX content by scanning for `.version` directive after whitespace/comments |
| `sub_45CB90` | `skip_comment` | Skips `//` and `/* */` comments during PTX detection |
| `sub_476BF0` | `load_file` | Reads a file into arena memory with optional null-termination |
| `sub_4BD760` | `ptx_compile_relocatable` | Top-level relocatable PTX compile (produces relocatable cubin) |
| `sub_4BD4E0` | `ptx_compile_whole_program` | Top-level whole-program PTX compile (produces final cubin) |
| `sub_4BD240` | `fatbin_member_compile` | Compiles PTX (or copies cubin) extracted from a fatbin container |
| `sub_4BD0A0` | `fatbin_arch_match` | Creates context, loads fatbin, runs arch matching -- returns matched content |
| `sub_4BDB90` | `ptxas_compile_engine` | Core engine: builds argv, invokes embedded ptxas, runs Mercury finalizer |
| `sub_4BE350` | `ptxas_dispatch` | Thin dispatcher that validates context and calls `sub_4BDB90` |
| `sub_4BE3D0` | `ptxas_get_stderr` | Reads `context+152` -- ptxas diagnostic string |
| `sub_4BE400` | `ptxas_cleanup_context` | Frees cubin, option strings, linked list items, and context itself |
| `sub_4CDD60` | `container_create` | Allocates a 168-byte context, writes magic `0x1464243BC` |
| `sub_4CE040` | `container_validate` | Validates that an opaque pointer is a valid container |
| `sub_4CE2F0` | `container_set_arch` | Sets SM number at offset +8, validates via arch DB |
| `sub_4CE3B0` | `container_set_version` | Sets fatbin compatibility version at offset +12 |
| `sub_4CE380` | `container_set_accelerated` | Sets offset +160 = 1 (sm_XXa accelerated-arch flag) |
| `sub_4CE640` | `container_set_flag_bit0` | Writes `a2` to offset +16 (64-bit or mode flag) |
| `sub_4CE3E0` | `container_append_option` | Concatenates a new option token into the option accumulator at offset +32 |
| `sub_4CE070` | `container_set_content` | Stores content pointer at +72 and classifies type at +80 (1/2/3/4) |
| `sub_4CE8C0` | `container_find_arch_match` | Architecture-aware content extraction (fatbin member selection) |
| `sub_4CE670` | `container_extract_content` | Copies `(data,type,size)` from `(+88,+96,+104)` into caller buffers |
| `sub_429BA0` | `build_ptxas_options` | Constructs the forwarded option string from `-Xptxas`, `--maxrregcount`, etc. |
| `sub_4FFC30` | `bind_ptxas_entry` | Initializer that binds `qword_2A77DD0` to the real entry `sub_52E060` |
| `sub_52E060` | `embedded_ptxas_main` | The in-process ptxas library entry point (called via `qword_2A77DD0`) |
| `sub_4748F0` | `mercury_finalize` | Post-compile Mercury finalizer for sm >= 100 cubins |

## PTX Detection (sub\_4CDF80)

PTX detection is one of the content-sniffing predicates invoked from `main()` during the [file-type detection](file-type-detection.md) phase. The function examines raw text content for the `.version` directive -- the mandatory first semantic token in any PTX source file -- while tolerating leading whitespace, C-style line comments, and C-style block comments.

### Algorithm

```c
// sub_4CDF80 -- is_ptx (exact translation of decompilation)
// Returns true iff the buffer begins (modulo whitespace and comments)
// with the 8-byte literal ".version".
bool is_ptx(char *buf) {
    if (!buf || !*buf)
        return false;

    char *p = buf;
    const unsigned short *ctype = *__ctype_b_loc();

    do {
        // Skip whitespace characters: bit 0x2000 in the glibc ctype table is _ISspace,
        // matching space (0x20), tab (0x09), newline (0x0A), carriage return (0x0D),
        // vertical tab (0x0B), and form feed (0x0C).
        while (*p && (ctype[(unsigned char)*p] & 0x2000))
            p++;

        // If the next characters start a comment, skip it and resume whitespace-skip.
        if (memcmp(p, "//", 2) == 0 || memcmp(p, "/*", 2) == 0) {
            skip_comment(&p);   // sub_45CB90
            continue;
        }
        break;
    } while (*p);

    return memcmp(p, ".version", 8) == 0;
}
```

The `.version` directive is mandatory and must be the first semantic token in every PTX file (followed shortly by `.target` and `.address_size`). Even hand-written PTX sources typically begin with an NVCC-generated comment header, so comment skipping is essential.

### Comment Skipping (sub\_45CB90)

```c
// sub_45CB90 -- skip_comment
// Advances *pp past one comment (// or /* */) in the source buffer.
void skip_comment(char **pp) {
    char *p = *pp;
    if (p[0] == '/' && p[1] == '/') {
        while (*p && *p != '\n')
            p++;
        if (*p) p++;                  // skip the newline
        *pp = p;
    } else if (p[0] == '/' && p[1] == '*') {
        p += 2;
        while (*p) {
            if (p[0] == '*' && p[1] == '/') {
                *pp = p + 2;           // skip past */
                return;
            }
            p++;
        }
        *pp = p;                       // unterminated block comment: point at \0
    }
}
```

An unterminated block comment causes the outer loop to exit with `p` pointing at the null terminator, which makes the final `memcmp(p, ".version", 8)` return non-zero -- detection correctly fails on truncated files.

### Dispatch in main()

In `main()` at `0x409800`, after the 56-byte header probe has been read and the cubin (ELF magic `7F 45 4C 46`), fatbin (`0xBA55ED50`), and NVVM-IR (`DE C0 17 0B`) tests have all failed, `is_ptx(header)` is the next check. On success, `main()` calls `sub_476BF0(path, 1)` to load the full file with null-termination, then dispatches to `sub_4BD760` (relocatable) or `sub_4BD4E0` (whole-program) depending on the link mode.

## File Loading (sub\_476BF0)

When PTX arrives as a standalone file, `main()` reads it using `sub_476BF0` with `null_terminate = 1`. Null-termination is essential because all downstream PTX consumers (`sub_4CDF80`, `sub_4CE070`, `sub_4BDB90`) treat the buffer as a C string and may call `strlen` on it.

```c
// sub_476BF0 -- load_file
// Reads an entire file into arena-allocated memory.
// If null_terminate is non-zero, appends a \0 byte after the file content.
void *load_file(const char *path, bool null_terminate) {
    FILE *f = fopen(path, "rb");
    if (!f) {
        error_file_open(path);        // sub_467460 with error descriptor
        return NULL;
    }

    fseek(f, 0, SEEK_END);
    size_t size = ftell(f);
    fseek(f, 0, SEEK_SET);

    size_t alloc = size + (null_terminate ? 1 : 0);
    void *arena = current_arena();    // from sub_44F410 context
    void *buf = sub_4307C0(arena, alloc);
    if (!buf)
        sub_45CAC0(arena, alloc, ...);  // OOM diagnostic

    if (fread(buf, 1, size, f) != size)
        error_file_read(path);

    fclose(f);
    if (null_terminate)
        ((char *)buf)[size] = '\0';
    return buf;
}
```

For fatbin-extracted PTX, the content is already in memory as the output of the fatbin walker; `sub_4BD0A0` + `sub_4BD240` operate directly on the extracted pointer without re-reading from disk.

## The Compilation Context (168-byte "Container")

Both the relocatable and whole-program paths share a common opaque structure -- internally called a *container* -- allocated by `sub_4CDD60` and freed by `sub_4BE400`. The same structure is used by `sub_4BD0A0` for pure fatbin extraction (arch matching only, no compile) and by `sub_4BD240` for compiling content already extracted from a fatbin, which is why the object is named generically.

### Layout

Offsets verified by inspecting every setter (`sub_4CE2F0`, `sub_4CE3B0`, `sub_4CE380`, `sub_4CE640`, `sub_4CE070`), the getter (`sub_4CE670`), the engine (`sub_4BDB90`), and the cleanup (`sub_4BE400`). Fields whose exact semantics are not fully reverse-engineered are marked with `?`.

| Offset | Size | Field | Writer | Reader | Notes |
|---|---|---|---|---|---|
| 0 | 8 | `magic` | `sub_4CDD60` | every setter | Constant `0x1464243BC` -- validates the object |
| 8 | 4 | `arch` | `sub_4CE2F0` | `sub_4BDB90`, `sub_42AF40` | SM architecture number (e.g., 90 for sm_90) |
| 12 | 4 | `version` | `sub_4CE3B0` | `sub_4BDB90` line 247 | Fatbin compatibility version (from `dword_2A5B528`) |
| 16 | 8 | `flags` | `sub_4CE640` | `sub_4BDB90` line 265 (`& 2`) | Mode-flag word. Bit tests observed: `& 2` in Mercury path |
| 24 | 8 | `extra_options_1` | `sub_4CE8C0` line 446/946 | `sub_4BDB90` line 131 (`v27`) | Option string harvested from matched fatbin member header |
| 32 | 8 | `extra_options_2` | `sub_4CE3E0` | `sub_4BDB90` line 132 (`v28`) | Accumulator for options appended via `sub_4CE3E0` (`-c`, `-m64`, `-g`, -Xptxas tokens) |
| 40 | 8 | `?` | -- | -- | Used in alternative extra-options path |
| 48 | 8 | `?` | -- | `sub_4BDB90` line 264 (`v9`) | Checked for `-threads` substring during Mercury path |
| 56 | 8 | `?` | -- | `sub_4BE400` (`a1[7]`) | Freed on cleanup -- auxiliary option buffer |
| 72 | 8 | `content_ptr` | `sub_4CE070` | `sub_4BDB90` line 97 | Input content pointer (PTX text, fatbin bytes, cubin bytes, NVVM IR) |
| 80 | 4 | `content_type` | `sub_4CE070` | `sub_4BDB90` line 98 | 1=compiled (LTO-IR/cubin), 2=fatbin, 3=ELF cubin, 4=PTX, 8=NVVM |
| 88 | 8 | `matched_data` | `sub_4CE8C0` | `sub_4CE670`, `sub_4BDB90` line 91 | Content extracted by arch matching |
| 96 | 4 | `matched_type` | `sub_4CE8C0` | `sub_4CE670`, `sub_4BDB90` line 79 | 1, 8, or 16 observed. 16 = Mercury-class result (triggers finalizer) |
| 104 | 8 | `matched_size` | `sub_4CE8C0` | `sub_4CE670`, `sub_4BDB90` line 92 | Size in bytes of matched content |
| 120 | 8 | `cubin_output` | `sub_4BDB90` line 322 | `sub_4BE400` | Compiled cubin pointer (freed via `qword_2A77DD0(4, ...)`) |
| 128 | 8 | `?` | -- | `sub_4BE400` (`a1[16]`) | Auxiliary output buffer, freed with `sub_431000` |
| 136 | 8 | `obfuscation_key` | `sub_4CE8C0`/fatbin walker | `sub_4BDB90` line 100 | Non-zero when PTX was obfuscated; emits warning + `-ok`/`-ptxlen` |
| 144 | 16 | `option_list` | `sub_4CE3E0` line 84 | `sub_4BE400` (`a1[18]`) | Linked list head for arena-tracked option strings |
| 152 | 8 | `stderr_buf` | `sub_4BDB90` via ptxas | `sub_4BE3D0` | Last diagnostic string from ptxas |
| 160 | 1 | `accelerated` | `sub_4CE380` | `sub_4BDB90` line 130 | sm_XXa accelerated-arch flag (passed to `sub_44E530` arch DB lookup) |
| 161 | 1 | `?` | -- | `sub_4CE2F0` line 18 | Secondary arch flag (read into `v4` and passed to `sub_44E530`) |

## Relocatable Compilation (sub\_4BD760)

This is the primary compilation entry for PTX files encountered during normal device linking. It produces a relocatable cubin that participates in the merge phase alongside other cubin inputs.

### Signature

The eight parameters are presented in the order the decompiler produced them. The semantics of the three boolean parameters (`a4`, `a5`, `a6`) were determined by tracing the unique callsite in `main()` at lines 699-707.

```c
// sub_4BD760 -- ptx_compile_relocatable
// Returns: 0 = success, 1 = success-with-warning, 5 = error, 7 = arch mismatch,
//          8 = compilation failure with no diagnostic string
int ptx_compile_relocatable(
    void     **cubin_out,      // a1 -- [out] arena-allocated compiled cubin
    void      *ptx_data,       // a2 -- null-terminated PTX source
    uint32_t   target_arch,    // a3 -- dword_2A5F314 (SM number)
    bool       accelerated,    // a4 -- byte_2A5F2C0 (sm_XXa flag, from arch name ending in 'a')
    bool       is_64bit,       // a5 -- (dword_2A5F30C == 64)
    bool       debug_info,     // a6 -- byte_2A5F310 (nvlink -debug / -g flag)
    const char *extra_options, // a7 -- returned by sub_429BA0 (forwarded -Xptxas etc.)
    uint32_t   fatbin_version  // a8 -- dword_2A5B528
);
```

The wiki previously mis-labeled `a4` as `is_64bit` and `a5` as `debug`. The correct mapping is derived from:

1. **`byte_2A5F2C0`** is assigned in `sub_427AE0` line 1041 as `sub_44E4F0(arch_name)`, which returns `arch_name[strlen(arch_name)-1] == 'a'`. This is the sm_XXa / sm_XXf accelerated-arch discriminator, not the 64-bit mode.
2. **`dword_2A5F30C == 64`** is the 64-bit addressing check (`dword_2A5F30C` holds the `-m` argument, either 32 or 64).
3. **`byte_2A5F310`** is bound to the `"debug"` CLI option in `sub_427AE0` line 944 (`sub_42E390(v2, "debug", &byte_2A5F310, 1)`).
4. Inside `sub_4BD760`, `if (a6)` appends the option at `.rodata` address `0x1D329C8`, which dumps as `"-g"` (not `"-ewp"` as previously documented). Address `0x1D322CD` dumps as `"-c"`.

### Flow (sub\_4BD760)

```
 1. sub_4CDD60      -- allocate 168-byte container, write magic
 2. sub_4CE3B0      -- set +12 = fatbin_version
 3. sub_4CE2F0      -- set +8 = target_arch, validate via sub_44E530/sub_486EA0 arch DB
 4. if (accelerated) sub_4CE380           -- set +160 = 1 (sm_XXa mode)
 5. if (is_64bit)    sub_4CE640(ctx, 1)   -- set +16 = 1
 6. if (extra_options) sub_4CE3E0(ctx, extra_options) -- append forwarded opts to +32
 7. sub_4CE070      -- store PTX text at +72, classify content type at +80
                       For raw PTX, this calls sub_4CDF80 again and sets +80 = 4
 8. sub_4CE8C0      -- architecture matching (passthrough for raw PTX)
                       -- return 3 means "no matching architecture" -> ret 7
                       -- any other non-zero means error -> ret 5
 9. sub_4CE670      -- copy (matched_data, matched_type, matched_size) out
                       matched_type == 1 means "already compiled" (bypass path)

    If matched_type == 1 (direct copy path):
       Jump to step 15, copying matched_data as the output cubin

    Else (actual compilation path):
10. sub_4CE3E0(ctx, 30614221)            -- append "-c" (relocatable flag)
11. if (debug_info) sub_4CE3E0(ctx, 30616008)  -- append "-g" (debug info flag)
12. if (is_64bit)   sub_4CE3E0(ctx, "-m64") else sub_4CE3E0(ctx, "-m32")
13. sub_4BE350(ctx, &compiled, &size)   -- invoke embedded ptxas
    On failure: retrieve stderr via sub_4BE3D0, fputs to stderr,
                call sub_4BE400, return 8 (no diag) or 5 (with diag)
14. (fall through with matched_type == 1 after direct path join)
15. setjmp environment established for memcpy path
16. arena_alloc(matched_size) via sub_4307C0
17. memcpy(new_buf, compiled, matched_size)
18. *cubin_out = new_buf
19. sub_4BE400(ctx) -- cleanup
20. Return 0 (or 1 if a warning flag was raised)
```

### setjmp/longjmp error recovery

Before the memcpy step, `sub_4BD760` saves a jump buffer via `_setjmp(env)` and installs it in the current execution context (`sub_44F410` returns a pointer to a thread-local error descriptor). If the memcpy, allocation, or follow-up code triggers `longjmp`, control returns to the `if (_setjmp(...))` branch which restores the prior error descriptor and returns an error code. This protects against OOM conditions in the arena allocator mid-copy.

### Return Codes (sub\_4BD760)

| Code | Source | Meaning |
|---|---|---|
| 0 | Normal return | Success; `*cubin_out` is valid |
| 1 | Error descriptor raised during memcpy path | Soft warning; cubin still valid |
| 5 | Any setter/compile failure, or setjmp fall-through | Hard error; cubin invalid |
| 7 | `sub_4CE8C0` returned 3 | Arch mismatch (PTX `.target` directive incompatible with link target) |
| 8 | `sub_4BE350` failed, `(context+80) != 0` | ptxas failure but the fatbin arch match had not classified to cubin |

## Whole-Program Compilation (sub\_4BD4E0)

This entry is used when nvlink operates in "whole program compile" mode -- logged by `main()` as `"whole program compile\n"` to stderr when `dword_2A5F308 & 1` (verbose flag) is set. The function is structurally similar to `sub_4BD760` but has three key differences.

### Differences from Relocatable Mode

| Aspect | `sub_4BD760` (relocatable) | `sub_4BD4E0` (whole-program) |
|---|---|---|
| `-c` flag appended | Yes, unconditionally after arch match | No |
| `-g` flag appended | `if (debug_info)` | `if (debug_info)` (same) |
| `-m64`/`-m32` appended | Yes | Yes |
| Direct-copy path (matched_type == 1) | Rejoins main flow and does arena memcpy | Returns 1 immediately (signal: content needs further processing) |
| setjmp around memcpy | Yes | No -- simpler linear flow |
| Arch mismatch return | 7 | 5 + 2 = 7 (computed as `2 * (v17 == 3) + 5`) |

The whole-program mode is invoked from `main()` at line 1165 and is specifically for taking the PTX text produced by the cicc-LTO pipeline and turning it into a final, non-relocatable cubin. Because LTO has already done whole-program optimization, the result doesn't need `-c` (which would prepare the cubin for further linking); ptxas produces the final SASS directly.

### Return Codes (sub\_4BD4E0)

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Direct-copy early exit (`matched_type != 1`) -- caller should handle |
| 5 | Error |
| 7 | Arch mismatch |
| 8 | ptxas failure without diagnostic |

## Fatbin-Embedded PTX Compilation (sub\_4BD240)

When PTX is encountered inside a fatbin container, the two-phase split is different:

1. **Phase 1 -- Arch match**: `sub_42AF40` calls `sub_4BD0A0` to create a container, load the fatbin bytes, run `sub_4CE8C0` architecture matching, and extract `(matched_data, matched_type, matched_size)`. `sub_4BD0A0` does *not* compile -- it returns the container handle to the caller.
2. **Phase 2 -- Compile-or-copy**: `sub_42AF40` then calls `sub_4BD240` passing the container and the extracted content. Depending on `matched_type`, this either compiles (PTX path) or directly copies the already-compiled cubin.

### sub\_4BD240 Algorithm

```c
// sub_4BD240 -- fatbin_member_compile
int fatbin_member_compile(
    void **cubin_out,        // a1 -- [out] cubin buffer
    void **container,        // a2 -- container handle from sub_4BD0A0
    void  *matched_data,     // a3 -- extracted content pointer
    int    matched_type,     // a4 -- type code (1 = PTX, other = compiled)
    size_t matched_size,     // a5 -- content size
    bool   is_64bit,         // a6 -- dword_2A5F30C == 64
    bool   debug_info,       // a7 -- byte_2A5F310 (-debug/-g)
    char  *extra_options     // a8 -- from sub_429BA0 (fatbin entry's own options)
) {
    if (matched_type != 1)        // Not PTX -- already-compiled content
        goto direct_copy;

    if (sub_4CE3E0(container, "-c"))        return 5;
    if (debug_info && sub_4CE3E0(container, "-g"))       return 5;
    if (is_64bit ? sub_4CE3E0(container, "-m64")
                 : sub_4CE3E0(container, "-m32"))        return 5;
    if (extra_options && sub_4CE3E0(container, extra_options)) return 5;

    if (sub_4BE350(container, &compiled, &compiled_size)) {
        // Compilation failure
        char *s = NULL;
        sub_4BE3D0(container, &s);
        if (s) fputs(s, stderr);
        return (last_error_flag() == 0) ? 8 : 5;
    }

direct_copy:
    // setjmp for OOM recovery
    if (_setjmp(env)) { restore(); }
    else {
        void *buf = arena_alloc(compiled_size);
        memcpy(buf, compiled, compiled_size);
        *cubin_out = buf;
        sub_4BE400(container);
    }
    return (warning_flag ? 1 : 0);
}
```

Note that unlike `sub_4BD760`, this function does not call `sub_4CE380` (set accelerated), nor does it re-run `sub_4CE3B0`/`sub_4CE2F0`/`sub_4CE070` -- all of that was done by `sub_4BD0A0` during phase 1, including the arch DB validation and the `sub_4CE8C0` matching. `sub_4BD240` only appends compilation options and invokes ptxas.

## The Embedded ptxas Pipeline

The actual PTX-to-SASS compilation is performed by `sub_4BDB90`, the core engine behind the `sub_4BE350` dispatcher. This function constructs a command-line argument vector and invokes the embedded ptxas compiler backend through `qword_2A77DD0`. The function pointer is initialized by `sub_4FFC30` to `sub_52E060` -- the entry point of the in-process ptxas library.

### Dispatcher (sub\_4BE350)

```c
int ptxas_dispatch(ctx, out_buf, out_size) {
    int err = sub_4CE040(ctx);   // validate magic
    if (err) return err;

    // If the matched content or the raw content is "already compiled" or PTX,
    // bypass the sub_4FFC30 init call
    if ((ctx->matched_data && ctx->matched_type != 1) &&
        (ctx->content_ptr  && ctx->content_type != 4))
        return sub_4BDB90(ctx, out_buf, out_size);

    // Initialize the ptxas entry pointer if needed
    unsigned rc = sub_4FFC30(0);
    if (rc > 8) return 5;
    int translated = dword_1D48AC0[rc];
    if (translated) return translated;
    return sub_4BDB90(ctx, out_buf, out_size);
}
```

### Compilation Engine (sub\_4BDB90, 8025 bytes)

This is the heart of the in-process compilation. The simplified algorithm is:

```c
int ptxas_compile_engine(container *ctx, void **output, size_t *output_size) {
    int err = sub_4CE040(ctx);
    if (err) return err;

    int content_type = ctx->matched_type;    // offset +96
    if (content_type > 16 || (1 << content_type) & 0x10014 == 0) {
        // Valid types are the bits set in 0x10014 = bit 2, bit 4, bit 16
        // (i.e., matched_type in {2, 4, 16})
        *output = NULL;
    }

    if (ctx->matched_data == NULL)                 // offset +88
        goto no_match;

    if (content_type == 8) {                       // NVVM cannot be PTX-compiled
        error("NVVM");  // dword_2A5BF80 diagnostic descriptor
        return 3;
    }

    void *ptx_src;
    size_t ptx_len;
    if (content_type == 1) {
        // Pre-matched PTX text (from fatbin extraction)
        ptx_src = ctx->matched_data;               // +88
        ptx_len = ctx->matched_size;               // +104
    } else {
        // Raw PTX directly from content_ptr
        void *ctx_72 = ctx->content_ptr;           // +72
        if (!ctx_72 || ctx->content_type != 4)
            goto no_match;
        if (ctx->obfuscation_key)                  // +136
            warning("PTX Obfuscation");  // dword_2A5BEB0
        ptx_src = ctx_72;
        ptx_len = strlen(ptx_src);
    }

    // --- setjmp recovery point ---
    if (_setjmp(env)) goto recover;

    // --- Arch-name formatting ---
    char arch_buf[13];
    sub_44E530(arch_buf,
               ctx->arch,                          // +8 (e.g., 90)
               0,
               ctx->accelerated,                   // +160 (sm_XXa flag)
               0);
    // arch_buf now holds "sm_90" or "sm_90a" etc.

    // --- Build base argv from static tables ---
    // off_1D48AE8 contains "-arch"           at slot 0
    // followed by "--input-as-string"        at slot 1
    // and several function pointers          at slots 2..4 (argv hooks)
    // v71[0] = (_, ptr_to_-arch)
    // v72    = ptx_src                       (the PTX text itself)
    // v71[1] = (_, ptr_to_--input-as-string)
    void *argv[32] = { /* slots 0..4 from off_1D48AE8/off_1D48AF0 and ptx_src */ };
    int argc = 5;

    // --- Append tokens from ctx->extra_options_1 (harvested from fatbin member header) ---
    if (ctx->extra_options_1) {                    // +24
        char *copy = arena_strdup(ctx->extra_options_1);
        for (char *t = strtok_r(copy, " \t", &save);
             t;
             t = strtok_r(NULL, " \t", &save))
            argv[argc++] = t;
    }

    // --- Append tokens from ctx->extra_options_2 (CLI -Xptxas + -c/-g/-m* added by wrappers) ---
    if (ctx->extra_options_2) {                    // +32
        char *copy = arena_strdup(ctx->extra_options_2);
        for (char *t = strtok_r(copy, " \t", &save);
             t;
             t = strtok_r(NULL, " \t", &save))
            argv[argc++] = t;
    }

    // --- Obfuscation-key protocol ---
    if (ctx->obfuscation_key) {                    // +136
        char hex_key[32];
        sprintf(hex_key, "0x%llx", ctx->obfuscation_key);
        argv[argc++] = "-ok";
        argv[argc++] = hex_key;
        if (ptx_len) {
            char hex_len[32];
            sprintf(hex_len, "0x%zx", ptx_len);
            argv[argc++] = "-ptxlen";
            argv[argc++] = hex_len;
        }
    }
    argv[argc] = NULL;

    // --- Invoke embedded ptxas ---
    void *result = NULL;
    uint32_t ptxas_rc = qword_2A77DD0(
        1,          // operation: compile-to-buffer
        argc,       // argument count
        argv,       // argument vector
        &result,    // [out] output buffer pointer
        0, 0);

    ctx->matched_data = (ptxas_rc == 0) ? result : NULL;

    // Free the temporary option-copy buffers
    if (extra1_copy) arena_free(extra1_copy);
    if (extra2_copy) arena_free(extra2_copy);

    // --- Translate ptxas return code to nvlink return code ---
    if (ptxas_rc > 8) return 5;
    return dword_1D48AC0[ptxas_rc];

    // --- Mercury finalization (when matched_type becomes 16 after ptxas returns) ---
    // When ptxas produces a Mercury-class (sm >= 100) cubin, the intermediate
    // output is tagged with content_type == 16 and sub_4748F0 is invoked
    // in-process to finalize it into the CapMerc/Mercury binary format.
}
```

### The Static argv Base Tables

Extracted from the binary at `.rodata`:

- **`off_1D48AE8`** -- first pointer is `"-arch"` (address `0x1D326F4`)
- **`off_1D48AF0`** -- first pointer is `"--input-as-string"` (address `0x1D48A85`)

Both tables contain subsequent function-pointer slots into the `sub_4BE970`/`sub_4BE980`/`sub_4BE990`/`sub_4BE9A0`/`sub_4BE9B0`/`sub_4BE9B7` range -- these are argv-tracking helper callbacks (likely deallocation/trace hooks installed by the ptxas library via a registration protocol). The first PTX-related slot layout is therefore:

| Slot | Content | Source |
|---|---|---|
| 0 | `"-arch"` | `off_1D48AE8[0]` |
| 1 | `sm_XX[a]` string | `sub_44E530` output (arch-name formatting) |
| 2 | `"--input-as-string"` | `off_1D48AF0[0]` |
| 3 | PTX source pointer (literal buffer) | `v72 = v59 = ptx_src` |
| 4 | `(reserved callback)` | Static table function pointer |
| 5+ | Tokenized `extra_options_1` | `context+24` |
| ... | Tokenized `extra_options_2` | `context+32` |
| ... | `"-ok"`, `0x<hex_key>` | If obfuscation_key != 0 |
| ... | `"-ptxlen"`, `0x<hex_len>` | If obfuscation_key != 0 and ptx_len != 0 |

The `--input-as-string` flag tells the embedded ptxas to read the PTX source directly from the next argv pointer (treated as a char* buffer) rather than opening a file -- this is how the in-process compilation path avoids writing the PTX to a temporary file.

### Return-Code Translation (dword\_1D48AC0)

Extracted directly from the binary at `.rodata+0x1D48AC0`:

```
dword_1D48AC0 = { 0, 4, 4, 6, 6, 6, 5, 7, 8 }
```

| ptxas rc | nvlink rc | Meaning |
|---|---|---|
| 0 | 0 | Success |
| 1 | 4 | Compilation / syntax error |
| 2 | 4 | Compilation / syntax error |
| 3 | 6 | Arch or version mismatch |
| 4 | 6 | Arch or version mismatch |
| 5 | 6 | Arch or version mismatch |
| 6 | 5 | Internal error |
| 7 | 7 | Resource limit exceeded (e.g., register count) |
| 8 | 8 | Fatal abort |
| >8 | 5 | Unknown error (clamped) |

### Mercury Post-Compilation (sub\_4748F0)

When the target SM is >= 100 (Blackwell and beyond), the initial cubin emitted by the embedded ptxas requires additional post-processing to produce the final Mercury/CapMerc binary format. `sub_4BDB90` detects this by checking the content type after the compile: if `matched_type == 16`, it invokes `sub_4748F0` -- the same in-process Mercury finalizer used by the post-link phase via `sub_4275C0`.

The finalizer setup includes parsing a `-threads` substring from the accumulated options (via `strstr` + `strtol`) to configure parallel finalization threads, and populating a 152-byte configuration block with Mercury-specific parameters before calling `sub_4748F0`. Failure of the finalizer returns diagnostic code 9 via `sub_1CEF420` (error-message translator).

See [Mercury Finalizer](../mercury/fnlzr.md) for the 10-phase finalization pipeline.

## Option Forwarding

nvlink forwards several option categories to the embedded ptxas. The forwarding logic is centralized in `sub_429BA0`, which builds the space-delimited option string that `sub_4BDB90` later tokenizes.

### sub\_429BA0 -- Option String Construction

The function reads `qword_2A5F238` (the accumulated `-Xptxas` list) and walks its entries via the `sub_464740`/`sub_464A80`/`sub_464A90`/`sub_464AA0`/`sub_464AC0` iterator protocol (an internal container-walker pattern used across nvlink). Each entry is concatenated into a single `char *` with space separators. Then `dword_2A5F22C` (`--maxrregcount`) is appended if > 0, and a number of other global flags contribute additional tokens.

The resulting string is then passed as `a7` to `sub_4BD760`/`sub_4BD4E0`, which forwards it to `sub_4CE3E0`, which in turn concatenates it into `context+32`.

### Option Sources (in order of appearance in the final argv)

1. **Static base** (always): `-arch sm_XX[a] --input-as-string <ptx>`
2. **Fatbin-member options** (when compiling fatbin-extracted PTX): stored by `sub_4CE8C0` at `context+24` during arch matching, tokenized into argv slots 5+. These typically include the options used when the fatbin was originally built (e.g., `-ftz=1`, `-prec_div=0`, `-fmad=1`).
3. **CLI-forwarded options** (always): `-c` (relocatable only), `-g` (if `-debug`), `-m64`/`-m32`, plus the `-Xptxas` tokens built by `sub_429BA0`. These are appended one by one via `sub_4CE3E0` to `context+32`.
4. **Obfuscation-key protocol** (when PTX was obfuscated): `-ok 0x<key>` and `-ptxlen 0x<bytes>`.

### Implicit Option Translations

Several nvlink-level CLI flags are automatically translated to ptxas options by the pipeline:

| nvlink flag | Global variable | ptxas equivalent | Mechanism |
|---|---|---|---|
| `-arch sm_XX` | `dword_2A5F314` | `-arch sm_XX[a]` | Injected as argv slots 0-1 |
| `-arch sm_XXa` | `byte_2A5F2C0 = 1` | `-arch sm_XXa` | Formatted by `sub_44E530` |
| `-m64` / `-m32` | `dword_2A5F30C` | `-m64` / `-m32` | Appended by `sub_4BD760`/`sub_4BD4E0` |
| `-debug` / `-g` | `byte_2A5F310` | `-g` | Appended by `sub_4BD760` (relocatable only appends it) |
| `-suppress-debug-info` | `byte_2A5F226` | (sets `byte_2A5F310 = 0`) | Forces `-g` off in `sub_427AE0` line 1087 |
| `-maxrregcount N` | `dword_2A5F22C` | `-maxrregcount N` | Appended by `sub_429BA0` |
| `-Xptxas <str>` | `qword_2A5F238` (list) | `<str>` verbatim | Accumulated by `sub_429BA0`, tokenized by `sub_4BDB90` |
| `-Ofast-compile` | `qword_2A5F258` | `-Ofast-compile` or similar | Appended by `sub_429BA0` |
| `--verbose-keep` | `byte_2A5F29B` | (logs ptxas cmdline to stderr) | `main()` line 1471 (`nvlink -extract ...`) |

## Error Handling

### Compilation Failures

When the embedded ptxas reports an error:

1. `sub_4BE350` (or `sub_4BDB90` directly) returns a non-zero status.
2. The caller (`sub_4BD760`, `sub_4BD4E0`, or `sub_4BD240`) allocates a local `char *s = NULL` and calls `sub_4BE3D0(ctx, &s)`, which simply reads `*(ctx + 152)` -- the stderr string the ptxas backend populated during the failed compile.
3. If `s != NULL`, it is written to the process stderr with `fputs(s, stderr)`.
4. `sub_4BE400(ctx)` is invoked, which walks the cleanup chain:
   - Frees `context+24` (extra_options_1) via `sub_431000`
   - Frees `context+56` via `sub_431000`
   - Frees `context+120` (cubin_output) via `qword_2A77DD0(4, ...)` -- this returns the cubin buffer to the ptxas backend's allocator
   - Frees `context+128` via `sub_431000`
   - Walks the linked list at `context+144` and frees each entry's `[1]` field via `sub_431000`
   - Calls `sub_464520` on the list head, then `sub_431000` on the context itself
5. The caller returns 5 (generic error) or 8 (ptxas failure without stderr output) based on whether the error descriptor at `context+152` was populated.

### Architecture Mismatch

If `sub_4CE8C0` returns 3 (arch DB lookup rejected the PTX `.target` directive), the compilation wrappers translate this to return code 7 (`sub_4BD760`) or `2 * (v17 == 3) + 5 = 7` (`sub_4BD4E0`). `main()` reports the error and terminates the file processing loop for that input.

### setjmp/longjmp Recovery

`sub_4BD760`, `sub_4BD240`, `sub_4CDD60`, `sub_4CE3E0`, `sub_4CE070`, and `sub_4CE670` each save a `jmp_buf` via `_setjmp(env)` and install it in the error-descriptor slot (`sub_44F410` returns the descriptor at the current execution context). A fatal error deep inside the arena allocator or the ptxas backend (assertion, OOM) triggers `longjmp` back to the nearest saved point. The recovery branch clears the context, sets the error flag, and returns the appropriate error code without crashing the process. The surrounding state -- in particular the prior error descriptor and the two-byte status flags -- is saved in local variables (`v45`, `v46`, `v47`, `v48`, etc.) and restored before any early return.

### NVVM Rejection

If PTX dispatch is reached with a context whose `matched_type == 8` (NVVM IR), `sub_4BDB90` emits the diagnostic "NVVM" through `sub_467460(dword_2A5BF80, "NVVM")` and returns 3. This is a defensive check -- NVVM inputs should have been caught earlier in `main()` and routed to the cicc library via [LTO Integration](../lto/libnvvm-integration.md) -- but if a fatbin member is misclassified, this path prevents the embedded ptxas from being fed IR it cannot consume.

### PTX Obfuscation Warning

When `context+136` is non-zero (set by the fatbin walker when it encounters an obfuscated PTX member), `sub_4BDB90` emits the warning `"PTX Obfuscation"` via `sub_467460(dword_2A5BEB0, ...)` before proceeding with compilation. The obfuscation key is then passed to ptxas via the `-ok 0x<key>` argument and the original PTX length via `-ptxlen 0x<len>` -- this protocol allows the embedded ptxas to deobfuscate the PTX internally before compilation.

## Difference Between PTX Input and LTO IR Input

Both PTX and NVVM LTO IR ultimately produce device SASS, but they travel different paths through nvlink. The split happens at the content-type classification in `sub_4CE070`:

| Aspect | PTX input (type 4) | LTO IR input (type 8) |
|---|---|---|
| Detector | `sub_4CDF80` (string `.version`) | NVVM magic `DEC0170B` |
| Loader | `sub_476BF0(path, 1)` (null-terminated) | `sub_476BF0(path, 0)` (binary) |
| Content-type code | 4 | 8 |
| Fatbin-extracted content-type | 1 (after arch match) | 8 (stays NVVM) |
| Top-level compile function | `sub_4BD760` / `sub_4BD4E0` | `sub_4BD1F0` (cicc-LTO pipeline) |
| Compile backend | Embedded ptxas via `qword_2A77DD0` | libnvvm via dlsym'd entry points |
| Option source | `sub_429BA0` + fatbin member header | LTO consensus options (`sub_42AF40` strstr logic) |
| Intermediate representation | Text stays text | libnvvm lowers NVVM IR to PTX text |
| Post-processing | Optional Mercury finalizer | cicc-LTO emits PTX which is then compiled via `sub_4BD760`/`sub_4BD4E0` |
| Merged path | Direct to merge pipeline | Producer of PTX that re-enters the PTX compile path |

The second-to-last row is the critical one: LTO IR eventually *becomes* PTX as output of the cicc-LTO whole-program compile, and that PTX is then fed back through the embedded-ptxas path described on this page. The "compile linked lto ir:" stderr message in `main()` (line 941) is printed just before this re-entry. So LTO input and PTX input ultimately converge on `sub_4BDB90` -- the LTO path just has an extra libnvvm stage in front.

The `sub_4BDB90` engine actively rejects type-8 content (`return 3` with "NVVM" diagnostic), ensuring that NVVM IR can never reach the embedded ptxas directly -- it must first pass through the libnvvm lowering stage.

See [LTO Integration](../lto/libnvvm-integration.md) and [LTO Overview](../lto/overview.md) for the complete LTO pipeline.

## Verbose-Keep Mode (`--verbose-keep`)

When `byte_2A5F29B` is set by `-vkeep` / `--verbose-keep`, `main()` writes intermediate files to disk and logs the equivalent standalone commands to stderr before each internal step. For PTX extraction:

```
nvlink -extract kernel.ptx -m64 -arch=sm_90 -o kernel_extracted.ptx
```

The file is written via `fopen(path, "w")` when the extracted name contains `.ptx`, or `fopen(path, "wb")` otherwise. The `printf` format string appears at `main()` line 1471.

For NVVM IR, the log entry is `nvlink -lto-add-module <name>.nvvm` at line 279 of `sub_42AF40`. The verbose-keep path never logs the exact argv used for the embedded-ptxas invocation -- instead the reconstructed equivalent is what the user sees. To capture the actual embedded-ptxas arguments, one would need to hook `qword_2A77DD0` or place a breakpoint at `sub_4BDB90+0x...` where the `qword_2A77DD0` call is issued.

## Result Handling

After successful compilation, the cubin buffer produced by the embedded ptxas lives in the ptxas backend's own allocator. `sub_4BDB90` stores the pointer into `context+120` (`cubin_output`). The wrapper function (`sub_4BD760` / `sub_4BD4E0` / `sub_4BD240`) then allocates a fresh buffer from the nvlink arena (`sub_4307C0`) sized to `matched_size` and memcpy's the cubin into it. This copy step is important because:

1. The nvlink arena outlives individual compilation contexts and has well-defined cleanup semantics at program exit.
2. `sub_4BE400` (context cleanup) calls `qword_2A77DD0(4, ctx->cubin_output, ...)` which returns the original cubin buffer to the ptxas allocator -- the memcpy ensures the caller has an independent copy before cleanup.

The cubin pointer is then returned to `main()`, which passes it through:

1. `sub_43D970` -- validate ELF magic and `e_machine == 190` (EM_CUDA)
2. `sub_426570` -- validate architecture compatibility (`e_flags` vs link target)
3. `sub_4275C0` -- optional Mercury finalization for sm >= 100
4. `sub_42A680` -- register the module with the link table
5. The merge phase ([Merge](../pipeline/merge.md)) then treats it identically to any directly-provided cubin input.

## Diagnostic Strings

Strings located via `.rodata` cross-references:

| String | Location | Context |
|---|---|---|
| `"relocatable compile\n"` | `main()` line 1183-1184 | Logged before `sub_4BD760` calls (gated by `dword_2A5F308 & 1`) |
| `"whole program compile\n"` | `main()` line 1160-1161 | Logged before `sub_4BD4E0` calls |
| `"compile linked lto ir:\n"` | `main()` line 941 | Logged when re-entering the PTX path for cicc-LTO output |
| `"PTX Obfuscation"` | `sub_4BDB90` line 102-103 | Warning when `context+136` is non-zero |
| `"NVVM"` | `sub_4BDB90` line 86 | Error when `content_type == 8` reaches the PTX compile engine |
| `"ptxas-lto"` | `main()` timing phase | Timing tag for LTO ptxas compilation (via `sub_4279C0`) |
| `"nvlink -extract %s -m%d -arch=%s -o %s\n"` | `sub_42AF40` line 130 | Verbose-keep log for fatbin extraction |
| `"nvlink -lto-nvvm-compile -m%d"` | `main()` line 1110 | Verbose-keep log for LTO compilation |
| `"found IR for libcudadevrt\n"` | `sub_42AF40` line 252 | Info message for CUDA device runtime library |

## Cross-References

- **Detected from**: [File-Type Detection](file-type-detection.md) selects PTX input via `sub_4CDF80`
- **Called by**: [Input Loop](../pipeline/input-loop.md) for standalone PTX files; [Fatbin Extraction](fatbin-extraction.md) via `sub_42AF40` → `sub_4BD0A0` → `sub_4BD240` for fatbin-embedded type-1 members
- **Produces**: Cubin inputs to the [Merge Phase](../pipeline/merge.md)
- **Post-processing**: [Mercury Finalizer](../mercury/fnlzr.md) for sm >= 100 output
- **Backend**: [Embedded ptxas](../ptxas/overview.md) documents the in-process ptxas library internals
- **Related inputs**: [Fatbin Extraction](fatbin-extraction.md) (shares the 168-byte container object); [LTO Integration](../lto/libnvvm-integration.md) and [LTO Overview](../lto/overview.md) (alternative path for NVVM IR that eventually re-enters here)
- **Container struct**: [168-Byte Input Container](container-struct.md) -- dedicated page documenting the field-offset table, magic, and lifecycle of the opaque object shared by all four input entry points
- **Architecture validation**: [Architecture Profiles](../targets/arch-profiles.md) (the `sub_44E530`/`sub_486EA0` arch DB used by `sub_4CE2F0`)
- **Options surface**: [CLI Options](../pipeline/cli-options.md) covers `-Xptxas`, `-maxrregcount`, `-debug`, `-suppress-debug-info`, `--verbose-keep`
