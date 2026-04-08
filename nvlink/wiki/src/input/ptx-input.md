# PTX Input & JIT

When nvlink encounters PTX (Parallel Thread Execution) assembly source as input -- either as a standalone `.ptx` file on the command line or as a type-1 member extracted from a fatbin container -- it cannot merge the text directly into the output ELF. PTX is architecture-neutral assembly; it must be compiled to SASS machine code for a specific SM target before linking. nvlink handles this by invoking the embedded ptxas backend (the same compiler backend that the standalone `ptxas` tool uses) entirely in-process, through a library-style C API. The resulting cubin is then fed back into the normal merge pipeline as if it had been a cubin input from the start.

This page documents the complete path: PTX detection, file loading, the two compilation modes (relocatable and whole-program), the ptxas wrapper API, option forwarding, and result handling.

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_4CDF80` | `is_ptx` | 121 B | Detects PTX content by scanning for `.version` directive |
| `sub_476BF0` | `load_file` | ~350 B | Reads a file into arena memory with optional null-termination |
| `sub_4BD760` | `ptx_compile_relocatable` | ~700 B | Relocatable PTX compilation (per-module, produces `.o`-style cubin) |
| `sub_4BD4E0` | `ptx_compile_whole_program` | ~550 B | Whole-program PTX compilation (single-pass, produces complete cubin) |
| `sub_4BD240` | `ptx_compile_from_fatbin` | ~900 B | PTX compilation for content extracted from fatbin containers |
| `sub_4BDB90` | `ptxas_compile_engine` | 8,025 B | Core compilation engine -- builds arg vector and calls embedded ptxas |
| `sub_4BE350` | `ptxas_dispatch` | ~200 B | Dispatch layer: routes PTX content to `sub_4BDB90` or fatbin driver |
| `sub_4BE3D0` | `ptxas_get_stderr` | ~50 B | Retrieves stderr output from a failed ptxas compilation |
| `sub_4BE400` | `ptxas_cleanup_context` | ~250 B | Frees the compilation context and all associated allocations |
| `sub_4CDD60` | `container_create` | ~400 B | Allocates and initializes a 168-byte compilation context |
| `sub_4CE2F0` | `container_set_arch` | ~130 B | Sets the target SM architecture on the context |
| `sub_4CE3B0` | `container_set_version` | ~70 B | Sets the fatbin version field on the context |
| `sub_4CE380` | `container_set_64bit` | ~70 B | Enables 64-bit addressing mode on the context |
| `sub_4CE640` | `container_set_debug` | ~70 B | Enables debug mode on the context |
| `sub_4CE3E0` | `container_append_option` | ~500 B | Appends a command-line option string to the context's option accumulator |
| `sub_4CE070` | `container_set_content` | ~500 B | Loads content buffer into context, classifies type (fatbin/ELF/NVVM/PTX) |
| `sub_4CE8C0` | `container_find_arch_match` | 29,098 B | Architecture matching within fatbin containers |
| `sub_4CE670` | `container_extract_content` | ~350 B | Extracts matched content from the container |
| `sub_45CB90` | `skip_comment` | ~200 B | Skips `//` and `/* */` comments in PTX source |

## PTX Detection (sub\_4CDF80)

PTX detection is one of the content-sniffing predicates called from `main()` during the [file type detection](file-type-detection.md) phase. The function examines the raw text content for a `.version` directive -- the mandatory first semantic token in any PTX source file -- while tolerating leading whitespace and C-style comments.

### Algorithm

```c
// sub_4CDF80 -- is_ptx
// Returns true if the buffer contains PTX source (starts with ".version"
// after skipping whitespace and comments)
bool is_ptx(char *buf) {
    if (!buf || !*buf)
        return false;

    char *p = buf;
    const unsigned short *ctype = *__ctype_b_loc();

    do {
        // Skip whitespace characters (space, tab, newline, etc.)
        while (*p && (ctype[(unsigned char)*p] & 0x2000))  // _ISspace
            p++;

        // If we hit a comment, skip it and loop back for more whitespace
        if (memcmp(p, "//", 2) == 0 || memcmp(p, "/*", 2) == 0) {
            skip_comment(&p);  // sub_45CB90
            continue;
        }
        break;
    } while (*p);

    // The first non-comment, non-whitespace content must be ".version"
    return memcmp(p, ".version", 8) == 0;
}
```

The `0x2000` bitmask tests `_ISspace` in the glibc `__ctype_b_loc()` table, which matches space (0x20), tab (0x09), newline (0x0A), carriage return (0x0D), vertical tab (0x0B), and form feed (0x0C).

### Comment Skipping (sub\_45CB90)

The comment skipper handles both comment styles:

- **Line comments (`//`)**: Advances the pointer past all characters until `\n` or end-of-string, then past the newline itself.
- **Block comments (`/* ... */`)**: Scans forward character-by-character until `*/` is found, then advances past the closing `*/`.

```c
// sub_45CB90 -- skip_comment
// Advances *pp past one comment (// or /* */)
void skip_comment(char **pp) {
    char *p = *pp;
    if (starts_with("//", p)) {
        // Line comment: scan to newline
        while (*p && *p != '\n')
            p++;
        if (*p) p++;          // skip the newline
        *pp = p;
    } else if (starts_with("/*", p)) {
        // Block comment: scan to */
        while (*p) {
            if (starts_with("*/", p)) {
                *pp = p + 2;  // skip past */
                return;
            }
            p++;
        }
        *pp = p;              // unterminated block comment: point at \0
    }
}
```

This means the detector correctly identifies PTX files that start with copyright headers, NVCC-generated comment blocks, or any combination of whitespace and comments before the `.version` directive.

### Detection in main()

In `main()` at `0x409800`, after reading the 56-byte header probe, PTX detection is attempted after cubin (ELF magic), fatbin (`0xBA55ED50`), and NVVM IR checks have all failed:

```c
// In main(), simplified PTX dispatch:
if (sub_4CDF80(header_buf)) {
    // PTX source -- compile via embedded ptxas
    result = sub_4BD760(
        &cubin_out,        // output cubin buffer
        ptx_data,          // PTX source text
        target_arch,       // SM architecture number
        is_64bit,          // 64-bit addressing flag
        is_debug,          // debug mode flag
        has_extra_opts,    // whether -Xptxas options exist
        xptxas_options,    // forwarded ptxas options string
        fatbin_version     // fatbin version for compatibility
    );
}
```

## File Loading (sub\_476BF0)

When PTX arrives as a standalone file, `main()` reads it using `sub_476BF0`, the general-purpose file loader. For PTX input specifically, the second parameter `a2` is set to `1` (non-zero), which triggers null-termination of the buffer -- essential because PTX is processed as a C string.

```c
// sub_476BF0 -- load_file
// Reads an entire file into arena-allocated memory.
// If null_terminate is non-zero, appends a \0 byte after the file content.
void *load_file(const char *path, bool null_terminate) {
    FILE *f = fopen(path, "rb");
    if (!f) {
        fatal_error(ERR_FILE_OPEN, path);
        return NULL;
    }

    fseek(f, 0, SEEK_END);
    size_t size = ftell(f);
    fseek(f, 0, SEEK_SET);

    // Allocate: file_size + 1 if null-terminating, else file_size
    size_t alloc_size = size + (null_terminate ? 1 : 0);
    void *arena = get_arena_from_context();
    void *buf = arena_alloc(arena, alloc_size);
    if (!buf) {
        arena_oom(arena, alloc_size);
        fatal_error(ERR_FILE_OPEN, path);
    }

    if (fread(buf, 1, size, f) != size)
        fatal_error(ERR_FILE_READ, path);

    fclose(f);

    if (null_terminate)
        ((char *)buf)[size] = '\0';

    return buf;
}
```

For fatbin-extracted PTX, the content is already in memory from the fatbin extraction pipeline, and the container library handles any necessary null-termination.

## Compilation Context (Container Object)

Both compilation modes share a common "container" object -- a 168-byte structure allocated by `sub_4CDD60`. This is the same container type used by the fatbin extraction pipeline (validated by the magic `0x1464243BC`), repurposed here as a compilation context.

### Container Structure

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `magic` | `0x1464243BC` -- container magic (validates the object) |
| 8 | 4 | `arch` | Target SM architecture number (e.g., `90` for sm\_90) |
| 12 | 4 | `version` | Fatbin version for compatibility |
| 16 | 8 | `flags` | Mode flags (64-bit, debug, etc.) |
| 24 | 8 | `extra_options_1` | First accumulated option string (from `-Xptxas`) |
| 32 | 8 | `extra_options_2` | Second accumulated option string (additional options) |
| 40 | 8 | `extra_options_3` | Third option accumulator |
| 48 | 8 | `extra_options_4` | Fourth option accumulator |
| 72 | 8 | `content_ptr` | Pointer to input content (PTX text or fatbin data) |
| 80 | 4 | `content_type` | Content type: 1=NVVM IR, 2=fatbin, 3=cubin ELF, 4=PTX |
| 88 | 8 | `matched_data` | Extracted/matched content after arch matching |
| 96 | 4 | `matched_type` | Type of matched content |
| 104 | 8 | `matched_size` | Size of matched content in bytes |
| 120 | 8 | `cubin_output` | Output cubin pointer after compilation |
| 136 | 8 | `obfuscation_key` | PTX obfuscation key (if applicable) |
| 144 | 16 | `option_list` | Linked list of accumulated options |
| 152 | 8 | `stderr_output` | Captured stderr from ptxas compilation |
| 160 | 1 | `is_64bit` | 64-bit addressing mode flag |
| 161 | 1 | `is_debug` | Debug mode flag |

### Context Setup Sequence

Both `sub_4BD760` and `sub_4BD4E0` follow the same initialization sequence:

```
container_create()          --> allocate 168-byte context, set magic
container_set_version()     --> set fatbin version at offset +12
container_set_arch()        --> set target SM at offset +8, validate via arch DB
container_set_64bit()       --> set offset+160 = 1 (if 64-bit mode)
container_set_debug()       --> set offset+16 = 1 (if debug mode)
container_append_option()   --> append -Xptxas forwarded options
container_set_content()     --> load PTX text, classify as type 4
container_find_arch_match() --> architecture matching (for fatbin content)
                                or passthrough (for raw PTX)
```

## Relocatable Compilation (sub\_4BD760)

This is the primary compilation path for PTX input files encountered during normal device linking. It produces a relocatable cubin (device `.o`) that can participate in the merge phase.

### Call Sites

- **Direct PTX files**: Called from `main()` when a `.ptx` file is detected in the input loop.
- **Fatbin PTX members**: Called from `sub_42AF40` (fatbin extraction) when a type-1 (PTX) member is the best architecture match.
- **LTO result compilation**: Called after cicc LTO compilation produces PTX output that must be assembled to SASS.

### Signature

```c
// sub_4BD760 -- ptx_compile_relocatable
// Returns: 0=success, 1=warning (non-fatal), 5=error, 7=no-match, 8=ptxas failure
int ptx_compile_relocatable(
    void    **cubin_out,      // [out] pointer to compiled cubin buffer
    void     *ptx_data,       // PTX source text (null-terminated)
    uint32_t  target_arch,    // SM architecture number
    bool      is_64bit,       // 64-bit addressing mode
    bool      is_debug,       // generate debug info
    bool      has_extra_opts, // whether extra ptxas options exist
    char     *extra_options,  // forwarded -Xptxas option string
    uint32_t  fatbin_version  // fatbin compatibility version
);
```

### Flow

1. **Create context**: `sub_4CDD60` allocates and zeroes 168 bytes, writes magic `0x1464243BC`.
2. **Set version**: `sub_4CE3B0` writes `fatbin_version` at context+12.
3. **Set architecture**: `sub_4CE2F0` writes `target_arch` at context+8, validates via the architecture database (`sub_44E530` + `sub_486EA0`).
4. **Set 64-bit mode**: If `is_64bit`, `sub_4CE380` sets context+160 = 1.
5. **Set debug mode**: If `is_debug`, `sub_4CE640` sets context+16 = 1.
6. **Append extra options**: If `extra_options` is non-NULL, `sub_4CE3E0` appends the string to the option accumulator at context+32.
7. **Set content**: `sub_4CE070` stores the PTX pointer at context+72, classifies it as type 4 (PTX).
8. **Find architecture match**: `sub_4CE8C0` processes the context. For raw PTX (type 4), this parses the `.version` and `.target` directives to validate compatibility with the target SM.
9. **Handle match result**: If no match (return 3), return 7. If error, return 5.
10. **Extract content**: `sub_4CE670` retrieves the matched content pointer, type code, and size.
11. **Compile or passthrough**:
    - If `matched_type != 1` (not already compiled): the content pointer is used directly -- `sub_4CE3E0` appends the relocatable-mode flag (integer constant `30614221`, which is the address of the string `"-c"` in the `.rodata` section), then appends machine-width flag (`"-m64"` or `"-m32"`), and delegates to `sub_4BE350` for actual compilation.
    - If `matched_type == 1` (already matched as compiled content): append the `"-c"` flag, optionally the extra-options flag (constant `30616008`, the address of `"-ewp"` for "extensible whole program"), and the machine-width flag, then delegate to `sub_4BE350`.
12. **Copy result**: On success, the compiled cubin is copied from the compilation context into arena-allocated memory, and `*cubin_out` is set to the new buffer.
13. **Cleanup**: `sub_4BE400` frees the context.

### Error Recovery

The function uses `setjmp`/`longjmp` for error recovery. Before entering the compilation path, it saves a jump buffer via `_setjmp(env)`. If compilation triggers a fatal error in the embedded ptxas backend, `longjmp` returns control to the error handler, which sets the error flags and returns 5.

If compilation produces no output but also no error, the function retrieves stderr output via `sub_4BE3D0` and writes it to `stderr` with `fputs`. The return code in this case is 8 (ptxas failure without cubin output) or 5 (internal error).

## Whole-Program Compilation (sub\_4BD4E0)

This mode compiles a single PTX source as a complete program (not relocatable). It is used in the LTO pipeline after cicc produces a whole-program PTX output, and when nvlink operates in "whole program compile" mode as logged in the `main()` debug trace.

### Signature

```c
// sub_4BD4E0 -- ptx_compile_whole_program
// Same parameter set as relocatable compile.
// Returns: 0=success, 1=multiple-output warning, 5=error, 7=no-match
int ptx_compile_whole_program(
    void    **cubin_out,
    void     *ptx_data,
    uint32_t  target_arch,
    bool      is_64bit,
    bool      is_debug,
    bool      has_extra_opts,
    char     *extra_options,
    uint32_t  fatbin_version
);
```

### Differences from Relocatable Mode

The function is structurally nearly identical to `sub_4BD760`, with these key differences:

1. **No `-c` flag**: The whole-program path does not append the relocatable compilation flag (`"-c"`). This tells ptxas to produce a fully linked executable cubin rather than a relocatable object.
2. **Output type check**: After `sub_4CE670` extracts the matched content, if `matched_type != 1`, the function calls `sub_4BE400` (cleanup) and returns `1` -- signaling that the content requires further processing. In relocatable mode, this condition triggers a secondary compilation attempt.
3. **No setjmp wrapper around output copy**: The whole-program path performs a simpler linear flow -- allocate, memcpy, set `*cubin_out`, cleanup, return 0. There is no setjmp-based error recovery around the copy step.
4. **Extra options**: The `-ewp` ("extensible whole program") flag is appended when `has_extra_opts` is set, and the machine-width flag (`-m64`/`-m32`) follows.

### Return Codes

| Code | Meaning |
|---|---|
| `0` | Success -- cubin written to `*cubin_out` |
| `1` | Content requires further processing (matched\_type != 1) |
| `5` | Internal error or compilation failure |
| `7` | Architecture match returned no-match (code 3 from `sub_4CE8C0`) |
| `8` | PTX compilation failure (no stderr available) |

## The Embedded ptxas Pipeline

The actual PTX-to-SASS compilation is performed by `sub_4BDB90`, which is the core engine behind the `sub_4BE350` dispatch layer. This function constructs a command-line argument vector and invokes the embedded ptxas compiler backend through the function pointer at `qword_2A77DD0`.

### Compilation Engine (sub\_4BDB90)

```c
// sub_4BDB90 -- ptxas_compile_engine (simplified pseudocode)
int ptxas_compile_engine(compilation_context *ctx, void **output, size_t *output_size) {
    int err = validate_library_loaded();  // sub_4CE040
    if (err) return err;

    // Determine content type and source
    int content_type = ctx->matched_type;    // offset +96
    void *ptx_source;
    int ptx_length;

    if (content_type == 1) {
        // PTX text from matched content
        ptx_source = ctx->matched_data;      // offset +88
        ptx_length = ctx->matched_size;      // offset +104
    } else if (content_type == 8) {
        // NVVM IR -- not compilable as PTX
        error("NVVM");
        return 3;
    } else {
        // Raw PTX from content_ptr
        ptx_source = ctx->content_ptr;       // offset +72
        ptx_length = strlen(ptx_source);
    }

    // Check for PTX Obfuscation
    if (ctx->obfuscation_key)                // offset +136
        warning("PTX Obfuscation");

    // Build argument vector for embedded ptxas
    char *argv[32];
    int argc = 5;   // base arguments pre-filled from static table

    // Parse extra_options_1 (space-delimited tokens)
    if (ctx->extra_options_1) {              // offset +24
        char *copy = arena_strdup(ctx->extra_options_1);
        char *tok = strtok_r(copy, " \t", &save);
        while (tok) {
            argv[argc++] = tok;
            tok = strtok_r(NULL, " \t", &save);
        }
    }

    // Parse extra_options_2 (same tokenization)
    if (ctx->extra_options_2) {              // offset +32
        char *copy = arena_strdup(ctx->extra_options_2);
        char *tok = strtok_r(copy, " \t", &save);
        while (tok) {
            argv[argc++] = tok;
            tok = strtok_r(NULL, " \t", &save);
        }
    }

    // Append obfuscation key if present
    if (ctx->obfuscation_key) {
        sprintf(hex_buf, "0x%llx", ctx->obfuscation_key);
        argv[argc++] = "-ok";
        argv[argc++] = hex_buf;
        if (ptx_length) {
            argv[argc++] = "-ptxlen";
            sprintf(len_buf, "0x%x", ptx_length);
            argv[argc++] = len_buf;
        }
    }
    argv[argc] = NULL;

    // Invoke embedded ptxas via function pointer
    void *result = NULL;
    uint32_t rc = qword_2A77DD0(1, argc, argv, &result, 0, 0);

    // Translate ptxas return code to nvlink return code
    if (rc > 8)
        return 5;  // internal error
    return ptxas_rc_translate_table[rc];  // dword_1D48AC0
}
```

The function pointer `qword_2A77DD0` is the entry point into the embedded ptxas compiler. The first argument (`1`) selects the compilation mode. The argument vector `argv` contains ptxas options in the same format as the standalone `ptxas` command-line tool.

### Argument Construction

The base argument slots (indices 0-4) are populated from a static table at `off_1D48AE8` / `off_1D48AF0`. These contain the pre-configured options that are always passed:

| Slot | Typical Content | Description |
|---|---|---|
| 0 | Architecture string | `"--gpu-name=sm_XX"` |
| 1 | Addressing mode | `"-m64"` or `"-m32"` |
| 2 | Compilation flag | `"-c"` for relocatable, absent for whole-program |
| 3 | PTX source pointer | The in-memory PTX text |
| 4 | (reserved) | Additional fixed options |

Slots 5+ are filled by tokenizing the accumulated option strings. These come from:

- **`-Xptxas` CLI options** (global variable `qword_2A5F238`): User-specified options forwarded to the embedded ptxas.
- **Fatbin-embedded options**: Options stored in the fatbin container's member entry header.
- **LTO consensus options**: For LTO-produced PTX, the options agreed upon across all translation units (e.g., `-ftz=1`, `-prec_div=0`).

### Mercury Post-Compilation

When the target architecture is sm >= 100 (Mercury), the compilation engine performs an additional step after ptxas produces a cubin. `sub_4BDB90` detects mercury-class output (content\_type == 16 at offset+96) and invokes `sub_4748F0` -- the in-process Mercury finalizer -- to transform the initial cubin into the final Mercury/CapMerc binary format. This is the same finalizer invoked by `sub_4275C0` during the post-link phase.

## Fatbin PTX Compilation (sub\_4BD240)

When PTX content is extracted from a fatbin container, the compilation follows a slightly different path through `sub_4BD240`. This function receives an already-initialized container context (the same one that performed the fatbin extraction), and either compiles the PTX content or copies an already-compiled cubin.

```c
// sub_4BD240 -- ptx_compile_from_fatbin (simplified)
int ptx_compile_from_fatbin(
    void **cubin_out,           // [out] compiled cubin
    void **container,           // container context from fatbin extraction
    void  *matched_content,     // extracted content from container
    int    matched_type,        // content type (1=PTX, 2=cubin, etc.)
    size_t matched_size,        // content size
    bool   is_64bit,            // 64-bit mode
    bool   has_ewp,             // extensible whole-program flag
    char  *extra_options        // additional options
) {
    if (matched_type != 1) {
        // Not PTX -- treat as pre-compiled content, copy directly
        goto copy_to_output;
    }

    // Append relocatable flag "-c"
    if (container_append_option(container, "-c"))
        return 5;

    // Append -ewp if needed
    if (has_ewp && container_append_option(container, "-ewp"))
        return 5;

    // Append machine width
    if (is_64bit) {
        if (container_append_option(container, "-m64"))
            return 5;
    } else {
        if (container_append_option(container, "-m32"))
            return 5;
    }

    // Append any extra options from the fatbin member
    if (extra_options && container_append_option(container, extra_options))
        return 5;

    // Compile
    if (ptxas_dispatch(container, &compiled_data, &compiled_size))
        goto handle_error;

copy_to_output:
    void *buf = arena_alloc(arena, compiled_size);
    memcpy(buf, compiled_data, compiled_size);
    *cubin_out = buf;
    ptxas_cleanup_context(container);
    return 0;

handle_error:
    // Retrieve and print stderr
    char *errmsg = NULL;
    ptxas_get_stderr(container, &errmsg);
    if (errmsg) fputs(errmsg, stderr);
    ptxas_cleanup_context(container);
    return (no_output) ? 8 : 5;
}
```

## Option Forwarding

nvlink forwards several option categories to the embedded ptxas:

### Direct Forwarding via -Xptxas

The `-Xptxas` CLI option (stored in `qword_2A5F238`) passes arbitrary strings directly to ptxas. Multiple `-Xptxas` values are accumulated into a space-delimited string. The compilation engine tokenizes this string on whitespace boundaries and appends each token to the argv vector.

```
nvlink --arch sm_90 -Xptxas -O3 -Xptxas -maxrregcount=64 input.ptx
```

This results in `argv` containing `[..., "-O3", "-maxrregcount=64", NULL]`.

### Implicit Options

Several nvlink-level options are automatically translated to ptxas options:

| nvlink option | Global variable | ptxas equivalent |
|---|---|---|
| `--maxrregcount N` | `dword_2A5F22C` | `--maxrregcount N` |
| `--suppress-debug-info` | `byte_2A5F226` | Suppresses `-g` |
| `--Ofast-compile` | `qword_2A5F258` | Optimization level |

### Fatbin-Embedded Options

When PTX is extracted from a fatbin, the fatbin member entry may carry option strings at offsets +24 and +32 within the member header. These typically include the compilation options that were used when the fatbin was originally created (e.g., `-ftz=1`, `-prec_div=0`). The compilation engine parses both strings and appends their tokens to argv.

## Verbose Keep Mode

When `--verbose-keep` (or `-vkeep`) is active (global flag `byte_2A5F29B`), nvlink writes intermediate files to disk and logs the ptxas commands that would produce equivalent results. This is invaluable for debugging compilation issues:

```
# Example verbose-keep output for fatbin PTX extraction:
nvlink -extract kernel.ptx -m64 -arch=sm_90 -o kernel_extracted.ptx

# The embedded ptxas invocation (reconstructed from argv):
ptxas --gpu-name=sm_90 -m64 -c kernel.ptx -o kernel.cubin
```

In verbose-keep mode, `sub_42AF40` (the fatbin extraction dispatch) writes the extracted PTX to a file using the naming convention `<basename>_<index>.ptx`, then logs the equivalent command line.

## Return Code Translation

The embedded ptxas backend returns its own error codes, which are translated to nvlink return codes via a lookup table at `dword_1D48AC0`:

| ptxas rc | nvlink rc | Meaning |
|---|---|---|
| 0 | 0 | Success |
| 1 | 5 | Compilation error |
| 2 | 5 | Internal error |
| 3 | 3 | No match / incompatible |
| 4 | 4 | Resource exhaustion |
| 5 | 5 | Fatal error |
| 6 | 5 | Fatal error |
| 7 | 5 | Fatal error |
| 8 | 5 | Fatal error |
| >8 | 5 | Unknown error (clamped) |

## Result Handling

After successful compilation, the cubin buffer is copied from the compilation context's internal storage into the nvlink arena allocator, and the context is freed. The cubin pointer is returned to `main()`, which then:

1. Validates the cubin as a device ELF (`sub_43D970` -- ELF magic, `e_machine == 190`).
2. Validates architecture compatibility (`sub_426570`).
3. Registers the module for linking (`sub_42A680`).
4. Queues the cubin for the merge phase (`sub_45E7D0`).

The resulting cubin is indistinguishable from a cubin that was provided directly as input -- it participates in weak symbol resolution, dead code elimination, shared memory layout, and all other linker phases without any PTX-specific handling.

## Error Handling

### Compilation Failures

When the embedded ptxas reports an error:

1. `sub_4BE350` returns a non-zero status.
2. The calling function (`sub_4BD760` or `sub_4BD4E0`) retrieves stderr via `sub_4BE3D0`, which reads offset+152 of the context.
3. If stderr is non-NULL, it is written to the process stderr with `fputs`.
4. `sub_4BE400` frees the context, walking the linked list at offset+144 to free all accumulated option strings.
5. The function returns 5 (error) or 8 (compilation failure without diagnostic output).

### Architecture Mismatch

If the PTX `.target` directive specifies an architecture incompatible with the link target (e.g., PTX targeting sm\_90 when linking for sm\_80), `sub_4CE8C0` returns 3. The compilation functions translate this to return code 7, and `main()` reports the error.

### setjmp/longjmp Recovery

Both `sub_4BD760` and `sub_4BD240` use `setjmp` to establish an error recovery point before entering the compilation engine. If the ptxas backend encounters a fatal internal error (assertion failure, out-of-memory in the ptxas allocator, etc.), it triggers a `longjmp` back to the saved point. The recovery handler sets the error/warning flags and returns the appropriate error code without crashing the nvlink process.

## Diagnostic Strings

| String | Location | Context |
|---|---|---|
| `"relocatable compile"` | `main()` debug trace | Logged before `sub_4BD760` calls |
| `"whole program compile"` | `main()` debug trace | Logged before `sub_4BD4E0` calls |
| `"compile linked lto ir:"` | `main()` debug trace | Logged when compiling LTO-produced PTX |
| `"PTX Obfuscation"` | `sub_4BDB90` | Warning when obfuscated PTX is encountered |
| `"NVVM"` | `sub_4BDB90` | Error when NVVM IR is passed to PTX compiler |
| `"ptxas-lto"` | `main()` timing phase | Timing tag for LTO ptxas compilation |

## Cross-References

- **Inbound**: [Input File Loop](../pipeline/input-loop.md) dispatches PTX files to `sub_4BD760`. [Fatbin Extraction](fatbin-extraction.md) calls `sub_4BD240` for type-1 members.
- **Outbound**: Compiled cubins enter the [Merge Phase](../pipeline/merge.md). The [Embedded ptxas](../ptxas/overview.md) section documents the ptxas backend internals. [CLI Options](../pipeline/cli-options.md) covers `-Xptxas` and `--verbose-keep`.
- **Shared infrastructure**: The container object is shared with [Fatbin Extraction](fatbin-extraction.md). Architecture validation uses the same [Architecture Profiles](../targets/arch-profiles.md) database.
