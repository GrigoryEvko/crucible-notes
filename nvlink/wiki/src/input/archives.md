# Archive Processing

nvlink processes Unix `ar(1)` archives -- static libraries containing multiple object files bundled under a single `.a` path. When the input loop's 56-byte header probe matches the 8-byte magic `"!<arch>\n"` (regular archive) or `"!<thin>\n"` (thin archive), execution enters the archive subsystem. The subsystem allocates an 80-byte iterator context, then loops over every member entry in the archive. For each member it parses the standard 60-byte `ar` header, resolves the member name (with GNU long-name table support), builds a composite `"archive:member"` path string, extracts or opens the member's content, and re-enters the input loop's type dispatch so the member is classified and processed exactly as if it had been a standalone file on the command line.

The archive layer is split into two tiers: five low-level functions in the `0x487000` range that implement `ar` format parsing, and four thin wrapper functions in the `0x4BDA00` range that translate return codes through a dispatch table (`dword_1D48A50`) and provide the API surface called from `main()`.

| | |
|---|---|
| **Magic detection** | `sub_487A90` at `0x487A90` (51 bytes) -- tests `"!<arch>\n"` and `"!<thin>\n"` |
| **Archive open** | `sub_487C20` at `0x487C20` (2,549 bytes) -- allocates 80-byte context |
| **Member iteration** | `sub_487E10` at `0x487E10` (5,592 bytes) -- parses `ar` headers, advances cursor |
| **Name resolution** | `sub_487AD0` at `0x487AD0` (356 bytes) -- builds `"archive:member"` path |
| **Member path accessor** | `sub_488290` at `0x488290` (8 bytes) -- returns context+40 (current path) |
| **Context destroy** | `sub_488200` at `0x488200` (144 bytes) -- frees context, member lists, path strings |
| **API wrappers** | `sub_4BDAC0` (open), `sub_4BDAF0` (next), `sub_4BDB30` (close), `sub_4BDB60` (get path) |
| **Caller** | `main()` at `0x409800`, archive dispatch branch |
| **Trigger** | First 8 bytes of file match `"!<arch>\n"` or `"!<thin>\n"` |

## Unix ar Format Overview

nvlink implements a parser for the standard Unix `ar` archive format (System V / GNU variant). The format has three structural layers:

### Global Header

Every archive begins with an 8-byte magic string. nvlink recognizes two variants:

| Magic (ASCII) | Hex bytes | Meaning |
|---|---|---|
| `!<arch>\n` | `21 3C 61 72 63 68 3E 0A` | Regular archive -- member data is embedded inline |
| `!<thin>\n` | `21 3C 74 68 69 6E 3E 0A` | Thin archive -- member data lives in external files |

The first member data begins immediately after this 8-byte header, at file offset 8.

### Member Header (60 bytes)

Each member is preceded by a fixed 60-byte ASCII header with the following layout:

| Offset | Size | Field | Format | Description |
|---|---|---|---|---|
| 0 | 16 | `ar_name` | ASCII, space-padded | Member name or `/nnn` long-name reference |
| 16 | 12 | `ar_date` | Decimal ASCII | Modification timestamp (seconds since epoch) |
| 28 | 6 | `ar_uid` | Decimal ASCII | Owner user ID |
| 34 | 6 | `ar_gid` | Decimal ASCII | Owner group ID |
| 40 | 8 | `ar_mode` | Octal ASCII | File permissions |
| 48 | 10 | `ar_size` | Decimal ASCII | Member data size in bytes |
| 58 | 2 | `ar_fmag` | `\x60\x0A` | End-of-header magic (backtick + newline) |

nvlink reads the size field at offset +48 by copying 10 bytes into a local buffer, NUL-terminating it, and calling `strtol(buf, NULL, 10)`. This matches the decompiled code in `sub_487E10`:

```c
// sub_487E10: size extraction from ar header
// v10 points to start of the 60-byte member header
strncpy(dest, (const char *)(v10 + 48), 10);   // ar_size field
dest[10] = '\0';
v6 = strtol(dest, NULL, 10);                    // parse decimal
```

Member data immediately follows the 60-byte header. If the size is odd, a single padding byte (`\n`) is appended to maintain 2-byte alignment for the next header. The iteration logic implements this alignment:

```c
// sub_487E10: alignment to next member
// v6 = parsed member size, v7 = current data offset
if (v6 % 2)
    next_offset = v7 + v6 - (v6 % 2) + 2;   // round up to even
else
    next_offset = v7 + v6;
```

### Special Members

Several member names have special meaning in GNU `ar` archives. nvlink handles three:

| Name pattern | Identity | nvlink behavior |
|---|---|---|
| `/` | Symbol table (armap) | First character is `/`, second is not a digit. Skipped (advances cursor past it) |
| `//` | GNU long-name string table | Detected by the `v34 == 2` check (two consecutive `/` characters). Its offset is stored in the iterator at context+48 for later long-name lookups |
| `__.LIBDEP` | Library dependency metadata | Compared with a 9-byte string match. Always skipped -- nvlink does not use LIBDEP data |

## Magic Detection (sub\_487A90)

The archive detection function is one of the simplest predicates in the binary:

```c
// sub_487A90 at 0x487A90 -- 51 bytes
bool is_archive(const void *buf, size_t size) {
    if (size <= 7)
        return false;
    if (memcmp(buf, "!<arch>\n", 8) == 0)
        return true;
    return memcmp(buf, "!<thin>\n", 8) == 0;
}
```

The function requires at least 8 bytes (`size > 7`). It checks for the regular archive magic first, then falls through to the thin archive magic. This means both archive types enter the same processing pipeline, with thin-vs-regular distinction deferred to the open function.

## Archive Open (sub\_487C20)

Opening an archive allocates an 80-byte iterator context and initializes it from the raw file buffer.

### Signature

```c
// sub_487C20 at 0x487C20 -- 2,549 bytes
// Returns 0 on success, 1 on error (via setjmp/longjmp)
int archive_open(void **ctx_out,     // a1: receives the allocated context
                 void  *buffer,      // a2: raw file data (mmap or fread result)
                 size_t size,        // a3: buffer size in bytes
                 const char *path);  // a4: archive file path (for diagnostics)
```

### Context Layout (80 bytes)

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `buffer` | Pointer to the raw archive data |
| 8 | 8 | `size` | Total buffer size in bytes |
| 16 | 8 | `cursor` | Current read position (offset of next member header) |
| 24 | 8 | `member_size` | Size of the current member's data |
| 32 | 8 | `path` | Arena-allocated copy of the archive file path |
| 40 | 8 | `member_path` | Constructed `"archive:member"` path for the current member |
| 48 | 8 | `longnames_ptr` | Pointer to the `//` (GNU long-name table) member, or NULL |
| 56 | 8 | `member_list` | Linked list of extracted member records |
| 64 | 8 | `path_list` | Linked list of allocated path strings |
| 72 | 1 | `is_thin` | Boolean: 1 if `"!<thin>\n"`, 0 if `"!<arch>\n"` |
| 73 | 7 | (padding) | Unused |

The initialization sequence allocates the 80 bytes via `sub_4307C0` (arena allocator), zeros the middle 64 bytes (offsets 8-71) with four 128-bit zero stores, copies the buffer pointer and size into slots 0 and 1, then checks for thin archive format:

```c
// sub_487C20: thin archive detection
ctx->is_thin = false;
if (ctx->size > 7)
    ctx->is_thin = (memcmp(ctx->buffer, "!<thin>\n", 8) == 0);
```

The archive path (`a4`) is duplicated into arena memory via `strlen` + `sub_4307C0` + `strcpy` and stored at offset +32.

Error handling uses `setjmp`/`longjmp`. If any allocation fails (arena returns NULL), the OOM handler `sub_45CAC0` is invoked, which may `longjmp` back to the error recovery point. On error, any partially-allocated context is freed and the function returns 1.

## Member Iteration (sub\_487E10)

This is the core of the archive subsystem. Each call advances to the next member, parses its header, resolves its name, and returns a pointer to its content.

### Signature

```c
// sub_487E10 at 0x487E10 -- 5,592 bytes
// Returns: 0 = no more members, 1 = error, 2 = thin resolve failed
int archive_next_member(const char **content_out,   // a1: receives member content pointer
                        void       *size_out,        // a2: receives member size (via indirect)
                        void       *ctx);            // a3: the 80-byte iterator context
```

### Iteration Algorithm

The function implements a loop that may skip multiple internal members (symbol tables, long-name tables, `__.LIBDEP` entries) before yielding a real member to the caller:

```
archive_next_member(content_out, size_out, ctx):
    |
    +-- Validate: return 3 if ctx==NULL, size_out==NULL, or content_out==NULL
    |
    +-- LOOP:
    |   |
    |   +-- Compute next header position:
    |   |     if cursor != 0:
    |   |         pos = cursor + member_size (aligned to 2-byte boundary)
    |   |     else:
    |   |         pos = buffer + 8  (skip the 8-byte global magic)
    |   |
    |   +-- Check bounds: if pos >= buffer + size --> no more members
    |   |     set content_out = NULL, return 0
    |   |
    |   +-- Parse size: strncpy 10 bytes from pos+48, strtol base 10
    |   |
    |   +-- Classify member name at pos:
    |   |     |
    |   |     +-- Name starts with "/" and next char is a digit?
    |   |     |     --> GNU long name reference: strtol(pos+1) gives offset
    |   |     |         into the long-name table at ctx->longnames_ptr
    |   |     |
    |   |     +-- Name starts with "/" and next char is "/"?
    |   |     |     --> This IS the long-name table ("//")
    |   |     |         Store pos in ctx->longnames_ptr (offset +48)
    |   |     |         Advance cursor, continue loop (skip this member)
    |   |     |
    |   |     +-- Name starts with "/" and next char is not a digit?
    |   |     |     --> Symbol table ("/")
    |   |     |         Skip: advance cursor, continue loop
    |   |     |
    |   |     +-- Name matches "__.LIBDEP"?
    |   |     |     --> Library dependency metadata
    |   |     |         Skip: advance cursor, continue loop
    |   |     |
    |   |     +-- Otherwise: regular member name
    |   |           Name extends from pos to the first "/" character
    |   |
    |   +-- Build composite path via sub_487AD0:
    |   |     result = archive_path + ":" + member_name
    |   |
    |   +-- Free previous member_path if any (ctx+40)
    |   +-- Store new path in ctx+40
    |   |
    |   +-- Extract content:
    |   |     if ctx->is_thin:
    |   |         Parse member path after the ":"
    |   |         Open external file via sub_476BF0 (fopen/fread/fclose)
    |   |         Validate: sub_476E90 checks for ELF magic (0x7F454C46)
    |   |         If open fails: return 2
    |   |     else (regular archive):
    |   |         Content pointer = pos + 60 (immediately after the header)
    |   |         sub_476E90 validates ELF magic at that pointer
    |   |
    |   +-- Set *size_out = member_size
    |   +-- Set *content_out = validated content pointer
    |   +-- Append to member tracking list via sub_4644C0 (ctx+56)
    |   +-- Update cursor: ctx+16 = pos + 60, ctx+24 = member_size
    |   +-- Return (success, value depends on error state)
```

### GNU Long Name Resolution

Standard `ar` headers only provide 16 bytes for the member name. When a member's name exceeds this limit, GNU `ar` uses a two-part encoding:

1. The archive contains a special member named `//` (two forward slashes). Its data is a concatenated string table where each name is terminated by `/\n`.

2. Members with long names have their `ar_name` field set to `/offset` where `offset` is a decimal byte offset into the `//` string table.

nvlink detects this pattern in `sub_487E10` by checking whether the name field starts with `/` followed by a digit character (tested via `__ctype_b_loc()` with the `isdigit` bitmask `0x800`). When found, it calls `strtol(name + 1, NULL, 10)` to extract the offset, then indexes into the long-name table stored at `ctx + 48`:

```c
// sub_487AD0: long-name resolution path
if (*name == '/' && isdigit(name[1])) {
    int offset = strtol(name + 1, NULL, 10);
    if (!ctx->longnames_ptr)
        fatal_error("longnames header not found");
    // longnames_ptr points to the "//" member's data (at header + 60)
    const char *resolved = ctx->longnames_ptr + offset + 60;
    int len = strchr(resolved, '/') - resolved;
    // ... build path with resolved name
}
```

If a long-name reference appears before the `//` member has been seen, the function calls `sub_467460` with the diagnostic string `"longnames header not found"` -- a fatal error.

## Name Resolution and Path Construction (sub\_487AD0)

Every archive member receives a composite path string in the format `"archive_path:member_name"`. This path serves as the member's identity throughout the linker pipeline -- it appears in diagnostics, symbol records, and debug information.

```c
// sub_487AD0 at 0x487AD0 -- 356 bytes
// Returns: arena-allocated string "archive_path:member_name"
char *build_member_path(const char *archive_path,    // src: the .a file path
                        const char *header_ptr,      // a2: points to ar_name field
                        void       *longnames_ptr);  // a3: the "//" string table, or NULL
```

The function handles two name formats:

**Direct name** (no long-name reference): The member name starts at `header_ptr` and extends to the first `/` character (which terminates `ar` names in GNU format). The `/` is not included in the output.

**Long-name reference** (`/offset`): When the name starts with `/` followed by a digit, the function looks up the offset in the long-name string table. The resolved name starts at `longnames_ptr + offset + 60` (60 bytes past the `//` member header) and extends to the next `/`.

In both cases, the function allocates `strlen(archive_path) + 1 + name_length + 1` bytes, then constructs:

```c
memcpy(result, archive_path, archive_len);      // "libfoo.a"
result[archive_len] = ':';                       // ":"
memcpy(result + archive_len + 1, name, name_len); // "bar.o"
result[archive_len + 1 + name_len] = '\0';      // NUL terminator
```

An `ar` archive `libfoo.a` containing member `bar.o` produces the path `"libfoo.a:bar.o"`. This colon-separated format is consistent with how other linkers (GNU ld, lld) identify archive members in diagnostics.

If the name field contains no `/` terminator at all, the function calls `sub_467460` with `"unexpected archive format"` -- a fatal error indicating a malformed `ar` header.

## Thin Archive Support

Thin archives (`"!<thin>\n"` magic) differ from regular archives in one critical way: member data is not embedded in the archive file. Instead, each member header's data region is empty (or contains only the member's path), and the actual content lives in a separate file on disk.

When `sub_487E10` encounters a member in a thin archive (`ctx->is_thin == true` at offset +72), it takes a different extraction path:

1. Builds the composite path via `sub_487AD0` as usual
2. Extracts the member file path from the composite path (everything after the `:` separator, via `strchr(path, ':') + 1`)
3. Opens and reads the external file via `sub_476BF0` -- which calls `fopen(path, "rb")`, `fseek/ftell` to determine size, `sub_4307C0` to allocate a buffer, and `fread` to load the content
4. Validates the loaded content via `sub_476E90`, which checks for ELF magic (`0x7F454C46` = `1179403647` as a signed int32)
5. If the file cannot be opened or read, returns 2 (thin archive resolve failure)

For regular archives, the content pointer is computed as `header_ptr + 60` (immediately after the 60-byte `ar` header), pointing directly into the memory-mapped archive buffer. No additional I/O is required.

The thin archive flag is set once during `sub_487C20` (archive open) and never changes:

```c
// sub_487C20: thin detection, stored at ctx+72
ctx->is_thin = (size > 7 && memcmp(buffer, "!<thin>\n", 8) == 0);
```

## \_\_.LIBDEP Skipping

The `__.LIBDEP` pseudo-member is a GNU extension that records library dependency information. nvlink explicitly skips this member. The detection in `sub_487E10` uses a 9-byte comparison against the string `"__.LIBDEP"` at the start of the member name:

```c
// sub_487E10: LIBDEP detection (simplified from the decompiled byte-comparison loop)
if (strncmp(header_name, "__.LIBDEP", 9) == 0) {
    // Skip: advance cursor past this member, continue to next
    ctx->cursor = header_ptr + 60;
    ctx->member_size = parsed_size;
    continue;
}
```

This ensures that dependency metadata members never reach the type dispatch system.

## API Wrapper Layer

The functions called from `main()` are not the low-level parsers directly. Instead, four thin wrappers at `0x4BDAC0`-`0x4BDB60` provide a normalized API with return code translation through `dword_1D48A50`:

| Wrapper | Address | Delegates to | Description |
|---|---|---|---|
| `sub_4BDAC0` | `0x4BDAC0` | `sub_487C20` | Open: allocate context from buffer |
| `sub_4BDAF0` | `0x4BDAF0` | `sub_487E10` | Next: advance to next member |
| `sub_4BDB30` | `0x4BDB30` | `sub_488200` | Close: destroy context, free resources |
| `sub_4BDB60` | `0x4BDB60` | `sub_488290` | Get path: return current member's composite path |

Each wrapper translates the internal return code through a lookup table:

```c
// sub_4BDAC0 (archive_open wrapper)
int archive_open_wrapper(void **ctx_out, void *buf, size_t size, const char *path) {
    unsigned int rc = sub_487C20(ctx_out, buf, size, path);
    if (rc <= 2)
        return dword_1D48A50[rc];   // translate through dispatch table
    return 1;                       // fallback: error
}
```

The `dword_1D48A50` table maps internal codes to the API-level codes that `main()` expects. This indirection isolates the archive parser's internal error semantics from the linker's top-level error handling.

## Member Re-Entry into Type Dispatch

Each extracted archive member re-enters the input loop's type classification system. The member's content pointer (an in-memory buffer for regular archives, or a freshly-loaded buffer for thin archives) is classified by the same magic-number checks used for top-level input files:

- ELF magic (`0x7F454C46`) with `e_machine == 190` --> cubin handler
- Fatbin magic (`0xBA55ED50`) --> fatbin extraction (recursion into sub\_42AF40)
- NVVM IR magic (`0x1EE55A01`) --> IR module registration
- PTX `.version` header --> ptxas JIT compilation
- Nested archive --> recursive archive iteration

This means an archive can contain cubins, fatbins, NVVM IR modules, or even other archives, and each will be handled correctly. In practice, CUDA static libraries (`.a` files) produced by `nvcc` contain cubin members (device ELF with `e_machine == 190`).

The member's composite path (`"libfoo.a:bar.o"`) is carried through the entire pipeline, appearing in error messages, symbol table entries, and debug information. This provides clear provenance when a linker error traces back to a specific member within an archive.

## Context Destruction (sub\_488200)

When iteration is complete (no more members or an error), `sub_4BDB30` delegates to `sub_488200` to tear down the iterator context:

```c
// sub_488200 at 0x488200 -- 144 bytes
int archive_close(void *ctx) {
    ctx->buffer = NULL;
    ctx->size = 0;

    // Free the archive path and member path strings
    arena_free(ctx->path);        // offset +32
    arena_free(ctx->member_path); // offset +40

    // Walk and free the member tracking list (offset +56)
    node = ctx->member_list;
    while (node) {
        // nullsub_4: no-op destructor for member records
        node = node->next;
    }
    list_destroy(ctx->member_list);

    // Walk and free the path string list (offset +64)
    node = ctx->path_list;
    while (node) {
        arena_free(node->data);   // free each allocated path string
        node = node->next;
    }
    list_destroy(ctx->path_list);

    // Free the context itself
    arena_free(ctx);
    return 0;
}
```

The member tracking list at offset +56 uses a no-op destructor (`nullsub_4`), meaning the member data pointers are not freed -- they point into the archive buffer (for regular archives) or into arena-allocated read buffers (for thin archives) that are cleaned up when the arena is destroyed.

## Integration with Library Resolution

Archives reach the input loop through two paths:

1. **Direct input**: The user passes `libfoo.a` on the command line. The path enters `qword_2A5F330` directly.

2. **Library search**: The user passes `-lfoo`. Library resolution (documented in [Library Resolution](../pipeline/library-resolution.md)) transforms this to `libfoo.a`, searches `-L` paths, and appends the resolved path to `qword_2A5F330`.

In the library search path, the two-pass search strategy first checks for file existence via `stat()`, then opens the archive via `sub_42A2D0` (archive search callback) which validates that at least one member matches the target CPU architecture (checking `e_machine` against the host machine type mapping).

## Function Map

| Address | Size | Identity | Description |
|---|---|---|---|
| `0x487A90` | 51 B | `is_archive` | Tests buffer for `"!<arch>\n"` or `"!<thin>\n"` magic |
| `0x487C20` | 2,549 B | `archive_open` | Allocates 80-byte context, sets thin flag, copies path |
| `0x487E10` | 5,592 B | `archive_next_member` | Parses ar header, resolves name, extracts/opens content |
| `0x487AD0` | 356 B | `build_member_path` | Constructs `"archive:member"` composite path string |
| `0x488290` | 8 B | `get_member_path` | Returns `ctx + 40` (current member path pointer) |
| `0x488200` | 144 B | `archive_close` | Destroys context, frees path strings and member lists |
| `0x4BDAC0` | 48 B | `archive_open_wrapper` | API wrapper: delegates to `sub_487C20`, translates return code |
| `0x4BDAF0` | 48 B | `archive_next_wrapper` | API wrapper: delegates to `sub_487E10`, translates return code |
| `0x4BDB30` | 48 B | `archive_close_wrapper` | API wrapper: delegates to `sub_488200`, translates return code |
| `0x4BDB60` | 8 B | `archive_path_wrapper` | API thunk: delegates to `sub_488290` |
| `0x476BF0` | 384 B | `read_file` | Opens file, reads entire content into arena buffer (thin archive member loading) |
| `0x476E90` | 32 B | `validate_elf_magic` | Returns non-NULL if first 4 bytes are `0x7F454C46` (ELF magic) |
| `0x4644C0` | 112 B | `list_prepend` | Prepends a node to a singly-linked list (member/path tracking) |
| `0x467460` | -- | `fatal_error` | Diagnostic reporter, called on format errors |

## Diagnostic Strings

| String | Source function | Context |
|---|---|---|
| `"!<arch>\n"` | `sub_487A90` | Regular archive magic comparison |
| `"!<thin>\n"` | `sub_487A90`, `sub_487C20`, `sub_487E10` | Thin archive magic comparison |
| `"__.LIBDEP"` | `sub_487E10` | Library dependency metadata member (skipped) |
| `"longnames header not found"` | `sub_487AD0` | Fatal: `/offset` reference before `//` member seen |
| `"unexpected archive format"` | `sub_487AD0` | Fatal: member name has no `/` terminator |

## Cross-References

- [File Type Detection](file-type-detection.md) -- The `sub_487A90` predicate in the detection function table
- [Input File Loop](../pipeline/input-loop.md) -- The dispatch branch that enters archive iteration
- [Library Resolution](../pipeline/library-resolution.md) -- How `-lfoo` resolves to `libfoo.a` and the two-pass search strategy
- [Cubin Loading](cubin-loading.md) -- Processing of ELF members extracted from archives
- [Fatbin Extraction](fatbin-extraction.md) -- Fatbin members found inside archives
- [NVVM IR / LTO IR Input](nvvm-ir-input.md) -- IR members in archives (when `-lto` is active)
