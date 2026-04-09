# Library Search

nvlink's library search infrastructure is a self-contained subsystem that resolves `-l<name>` flags to filesystem paths. It combines a linked-list-based directory search context, a two-pass probing algorithm (existence check, then archive validation), a general-purpose string tokenizer for environment variable parsing, and a set of path manipulation utilities. The subsystem is reused for both `-L` command-line directories and `LIBRARY_PATH` environment variable entries, and its architecture-validation callback integrates with the archive subsystem to reject incompatible host-architecture members.

This page documents the infrastructure components at reimplementation depth. For the pipeline-level view of when and how library resolution runs within `main`, see [Library Resolution](../pipeline/library-resolution.md).

## Function Map

| Address | Name (recovered) | Size | Role |
|---|---|---|---|
| `sub_4622D0` | `search_context_create` | 80 B | Allocates 16-byte head/tail linked-list context |
| `sub_462500` | `search_context_append` | 48 B | Appends a directory to the search context (direct call) |
| `sub_462520` | `search_context_append_cb` | 48 B | Appends a directory to the search context (tokenizer callback) |
| `sub_462320` | `search_context_destroy` | 112 B | Frees search context and directory node list |
| `sub_462870` | `path_search` | 4,905 B | Core search: iterates directories, probes filesystem, calls acceptance callback |
| `sub_44EC40` | `split_and_callback` | 576 B | Tokenizes a string on a delimiter, invokes a callback per token |
| `sub_44E8B0` | `tokenize` | 4,780 B | Token extractor with quoting, escaping, bracket nesting support |
| `sub_462620` | `path_split` | 3,579 B | Splits a path into directory, basename, and extension components |
| `sub_462C10` | `path_split_dir_file` | 512 B | Splits a path into directory and filename (no extension separation) |
| `sub_462550` | `path_join` | 288 B | Joins directory + basename + optional extension into a normalized path |
| `sub_429AA0` | `make_library_filename` | 304 B | Transforms `-l` name into `lib<name>.so` or `lib<name>.a` |
| `sub_42A2D0` | `archive_validate_callback` | 5,008 B | Opens archive, iterates members, validates CPU architecture |
| `sub_464460` | `list_node_create` | 96 B | Allocates a 16-byte linked-list node (next + data) |

## Search Context Data Structure

### 16-Byte Context Header (sub\_4622D0)

`search_context_create` allocates a 16-byte structure that serves as the anchor for a singly-linked list of search directories. The structure uses a tail-pointer idiom for O(1) append.

```
search_context_t (16 bytes)
=======================================
Offset  Size  Field   Description
---------------------------------------
  0      8    head    Pointer to first search_dir_node, or NULL if empty
  8      8    tail    Pointer to the `next` field of the last node
                      (initialized to &self.head for empty lists)
```

The key initialization detail: `tail` is set to point at the context itself (i.e., `&ctx->head`), not to NULL. This means the first append writes directly into the `head` field via `*ctx->tail = new_node`, eliminating a special case for empty-list insertion.

From the decompiled code:

```c
// sub_4622D0 -- search_context_create
search_context_t* search_context_create(arena_t* arena) {
    search_context_t* ctx = arena_alloc(arena, 16);  // sub_4307C0
    if (!ctx)
        arena_alloc_fail(arena, 16);                  // sub_45CAC0 (noreturn)
    ctx->head = NULL;          // *result = 0
    ctx->tail = (void*)ctx;   // result[1] = result  -- self-referencing tail
    return ctx;
}
```

### Directory Node (sub\_464460)

Each directory in the search path is represented by a 16-byte linked-list node:

```
search_dir_node_t (16 bytes)
=======================================
Offset  Size  Field   Description
---------------------------------------
  0      8    next    Pointer to next node, or NULL
  8      8    path    Pointer to directory path string (arena-allocated)
```

`list_node_create` (`sub_464460`) allocates the node and stores both the `next` pointer and the data pointer. In the search context's usage, `next` is always NULL at creation (nodes are appended at the tail).

```c
// sub_464460 -- list_node_create
search_dir_node_t* list_node_create(char* path, search_dir_node_t* next) {
    search_dir_node_t* node = arena_alloc(arena, 16);
    if (!node)
        arena_alloc_fail(arena, 16);
    node->next = next;     // *result = a2   (NULL for append)
    node->path = path;     // result[1] = a1
    return node;
}
```

### Append Operations (sub\_462500, sub\_462520)

Two append functions exist with identical logic but different calling conventions:

```c
// sub_462500 -- search_context_append (direct call from main)
void search_context_append(search_context_t* ctx, char* dir_path) {
    search_dir_node_t* n = list_node_create(dir_path, NULL);
    *ctx->tail = n;     // link new node at the tail
    ctx->tail = n;      // advance tail pointer to new node's `next` field
}

// sub_462520 -- search_context_append_cb (callback for split_and_callback)
void search_context_append_cb(char* dir_path, search_context_t* ctx) {
    search_dir_node_t* n = list_node_create(dir_path, NULL);
    *ctx->tail = n;
    ctx->tail = n;
}
```

The only difference is argument order: `sub_462500` takes `(ctx, path)` while `sub_462520` takes `(path, ctx)`. The latter matches the `(token, user_data)` callback signature expected by the string tokenizer `sub_44EC40`.

Both functions update two pointers:
1. `*ctx->tail = n` -- writes the new node's address into the `next` field of the previous tail node (or into `head` if the list was empty)
2. `ctx->tail = n` -- advances the tail pointer to the new node, so the next append will write into the new node's `next` field

## Search Path Construction

The search path is built in two sequential phases. `-L` paths always precede `LIBRARY_PATH` paths, matching GNU `ld` convention.

### Phase 1: -L Command-Line Directories

`main` iterates the multi-value option list stored at `qword_2A5F300` (populated by the option parser for every `-L` argument):

```c
search_context_t* ctx = search_context_create(arena);

search_dir_node_t* lpath = qword_2A5F300;  // -L path list head
while (lpath) {
    search_context_append(ctx, lpath->path);    // sub_462500
    lpath = lpath->next;
}
```

### Phase 2: LIBRARY_PATH Environment Variable

After `-L` paths, nvlink reads the `LIBRARY_PATH` environment variable, tokenizes it on `:` delimiters, and appends each token:

```c
char* env = getenv("LIBRARY_PATH");
split_and_callback(env, ":",             // sub_44EC40
    /*include_empty=*/0, /*skip_empty=*/1,
    search_context_append_cb, (uintptr_t)ctx,
    /*handle_escapes=*/1, /*handle_brackets=*/1);
```

## String Tokenizer (sub\_44EC40)

`split_and_callback` is a general-purpose tokenizer used by multiple subsystems (library path parsing, nvinfo parsing, option response files). It copies the input string into arena memory, then repeatedly calls `sub_44E8B0` (the token extractor) to split on a delimiter, invoking a callback for each token.

```c
// sub_44EC40 -- split_and_callback
void split_and_callback(
    char*    input,           // string to tokenize
    char*    delimiters,      // delimiter characters (e.g. ":")
    bool     include_empty,   // if true, invoke callback even for empty tokens
    bool     keep_delimiters, // passed through to tokenize()
    void   (*callback)(char* token, uintptr_t arg),
    uintptr_t callback_arg,
    bool     handle_escapes,  // tokenizer handles backslash escapes
    bool     handle_brackets  // tokenizer handles [...] bracket nesting
)
{
    if (!input) return;

    // Make arena-owned working copy (sub_44E8B0 modifies the string in-place)
    char* work = arena_strdup(input);
    char* cursor = work;

    char* token = tokenize(&cursor, delimiters,
                           handle_escapes, handle_brackets, keep_delimiters);
    while (token) {
        if (include_empty || *token != '\0')
            callback(token, callback_arg);
        token = tokenize(&cursor, delimiters,
                         handle_escapes, handle_brackets, keep_delimiters);
    }
}
```

The `include_empty` flag controls whether zero-length tokens (from consecutive delimiters like `::`) invoke the callback. For `LIBRARY_PATH` parsing, `include_empty` is 0, so empty path components are silently skipped. The `handle_escapes` and `handle_brackets` flags are both 1, enabling backslash-escaped characters and `[...]`-delimited literal blocks within path components (though these features are primarily used for nvinfo parsing, not paths).

### Token Extractor (sub\_44E8B0)

The token extractor `sub_44E8B0` is a 4,780-byte function that implements stateful string splitting. It modifies the cursor pointer in-place (passed by reference) and returns a pointer to the next token, or NULL when exhausted. Key behaviors:

- Scans forward from `*cursor` until it hits a delimiter character or NUL
- When a delimiter is found, writes NUL to terminate the token and advances `*cursor` past it
- Backslash escaping: if `handle_escapes` is set, `\x` is treated as a literal `x` (the backslash is consumed)
- Bracket nesting: if `handle_brackets` is set, `[` begins a nested scope where delimiters are ignored until the matching `]`
- Quoted strings: `"..."` blocks are passed through without delimiter splitting

## Path Manipulation Utilities

### path\_split (sub\_462620)

Decomposes a filesystem path into three arena-allocated components: directory, basename, and extension. Uses `strrchr` for both `.` (extension separator) and `/` (directory separator), handling the edge case where a dot appears only in the directory portion (e.g., `/usr/lib.d/foo` -- the dot is part of the directory, not an extension).

```c
// sub_462620 -- path_split
void path_split(char* path,
                char** dir_out,      // "/usr/lib" (or NULL if no directory component)
                char** basename_out, // "libfoo" (no extension)
                char** ext_out)      // "a" (no leading dot, or NULL if no extension)
{
    char* work = arena_strdup(path);
    char* dot  = strrchr(work, '.');
    char* slash = strrchr(work, '/');

    // Dot that precedes the last slash is part of the directory, not an extension
    if (dot < slash)
        dot = NULL;

    if (dir_out) {
        if (slash) {
            *slash = '\0';
            *dir_out = arena_strdup(work);
            *slash = '/';
        } else {
            *dir_out = NULL;
        }
    }

    if (basename_out) {
        if (dot) *dot = '\0';              // temporarily terminate at the dot
        char* base_start = slash ? slash + 1 : work;
        *basename_out = arena_strdup(base_start);
        if (dot) *dot = '.';              // restore
    }

    if (ext_out) {
        if (dot) {
            *ext_out = arena_strdup(dot + 1);  // extension without leading dot
        } else {
            *ext_out = NULL;
        }
    }

    arena_free(work);
}
```

### path\_split\_dir\_file (sub\_462C10)

A simpler variant that splits a path into just directory and filename components (no extension separation):

```c
// sub_462C10 -- path_split_dir_file
void path_split_dir_file(char* path,
                         char** dir_out,   // "/usr/lib" or NULL
                         char** file_out)  // "libfoo.a" (preserves extension)
{
    char* work = arena_strdup(path);
    char* slash = strrchr(work, '/');

    if (dir_out) {
        if (slash) {
            *slash = '\0';
            *dir_out = arena_strdup(work);
            *slash = '/';
        } else {
            *dir_out = NULL;
        }
    }

    if (file_out) {
        char* name_start = slash ? slash + 1 : work;
        *file_out = arena_strdup(name_start);
    }

    arena_free(work);
}
```

### path\_join (sub\_462550)

Constructs a normalized path from directory, basename, and optional extension components. Strips trailing slashes from the directory before appending:

```c
// sub_462550 -- path_join
char* path_join(char* dir, char* basename, char* ext) {
    string_builder_t* sb = string_builder_create(128);  // sub_44FB20

    if (dir && *dir) {
        // Append directory, stripping trailing slashes
        size_t len = strlen(dir);
        char* end = dir + len;
        while (end > dir && *(end - 1) == '/')
            end--;
        // Append characters from dir[0..end)
        for (char* p = dir; p < end; p++)
            string_builder_append_char(sb, *p);      // sub_44FF90
        string_builder_append_char(sb, '/');
    }

    string_builder_append_str(sb, basename);           // sub_44FE60

    if (ext && *ext) {
        string_builder_append_char(sb, '.');
        string_builder_append_str(sb, ext);
    }

    return string_builder_finalize(sb);                // sub_44FDC0
}
```

The trailing-slash stripping ensures that directories like `/usr/lib/` produce `/usr/lib/libfoo.a` rather than `/usr/lib//libfoo.a`.

## Library Name Transformation (sub\_429AA0)

Converts a bare `-l<name>` argument into a filesystem filename by prepending `lib` and appending `.so` (for shared objects) or `.a` (for static archives).

```c
// sub_429AA0 -- make_library_filename
char* make_library_filename(char* name, bool shared) {
    // Step 1: Prepend "lib" using a DWORD write
    char* tmp = arena_alloc(arena, strlen(name) + 4);
    *(uint32_t*)tmp = 0x0062696C;    // little-endian: 6C='l', 69='i', 62='b', 00=NUL
    strcat(tmp, name);               // tmp = "lib<name>"

    // Step 2: Append extension
    char* result;
    if (shared) {
        result = arena_alloc(arena, strlen(tmp) + 4);
        char* end = stpcpy(result, tmp);
        *(uint32_t*)end = 0x006F732E; // little-endian: 2E='.', 73='s', 6F='o', 00=NUL
    } else {
        result = arena_alloc(arena, strlen(tmp) + 3);
        strcpy(stpcpy(result, tmp), ".a");
    }

    arena_free(tmp);
    return result;
}
```

### DWORD-Write Trick

Both the `lib` prefix and `.so` suffix are written as 32-bit integer stores rather than string copies. This is a micro-optimization pattern seen throughout nvlink. The constants decode as:

| Constant | Hex bytes (little-endian) | ASCII |
|---|---|---|
| `0x0062696C` (decimal 6,449,516) | `6C 69 62 00` | `"lib\0"` |
| `0x006F732E` (decimal 7,303,982) | `2E 73 6F 00` | `".so\0"` |

The DWORD write simultaneously writes all characters including the NUL terminator in a single aligned store, which is faster than `strcpy("lib")` for such short strings. The `.a` suffix uses `strcpy` because it is only two characters and the trick would not save anything.

## Core Search Algorithm (sub\_462870)

`path_search` is the central function of the subsystem. It takes a search context, a candidate filename, behavioral flags, and an optional acceptance callback. It returns the full path to the first matching file, or NULL.

### Signature

```c
// sub_462870 -- path_search
char* path_search(
    search_context_t*  ctx,           // search directory list (may be NULL)
    char*              filename,      // file to find (may contain '/' directory prefix)
    bool               search_dirs,   // if true, iterate ctx directories
    bool               try_split,     // if true, attempt path_split fallback
    accept_fn          callback,      // optional: returns 0=accept, nonzero=reject
    uintptr_t          callback_arg   // passed through to callback
)
```

### Algorithm

The function implements a multi-stage search with fallback:

**Stage 1 -- Direct path detection.** The filename is copied into arena memory. If `strrchr(copy, '/')` finds a directory separator, the path has an explicit directory component. The code splits the filename at the last `/` into a directory part and a base part.

**Stage 2 -- Absolute path or no search context.** If the directory component starts with `/` (absolute path), or the directory is empty, or `ctx` is NULL, or `search_dirs` is false, the code checks the original filename directly via `stat()`. If the file exists, it is accepted immediately (a deep copy of the filename is returned).

**Stage 3 -- Directory list iteration.** For relative paths with a search context, the code iterates every node in the search context linked list:

```c
search_dir_node_t* node = ctx->head;
while (node) {
    // Build candidate: strip trailing slashes from node->path,
    // append '/', append filename
    char* candidate = path_join_inline(node->path, filename);

    if (stat(candidate) == 0) {
        // File exists on disk
        if (!callback)
            return candidate;             // no validation needed
        if (callback(candidate, callback_arg) == 0)
            return candidate;             // callback accepted
    }

    arena_free(candidate);
    node = node->next;
}
```

The path construction is performed inline using the string builder (`sub_44FB20`), appending directory characters while stripping trailing slashes, adding a `/` separator, then appending the filename via `sub_44FE60`.

**Stage 4 -- Path decomposition fallback.** If `try_split` is true and no match was found in stages 1--3, the function decomposes the filename using `path_split` into directory, basename, and extension. It then reconstructs the filename from the components and recursively calls itself with `try_split=0` and the reconstructed path. This handles cases where the path structure encodes search semantics (not used for standard `-l` resolution).

### Two-Pass Search Strategy

For each `-l` library, `main` invokes `path_search` twice:

1. **Pass 1 (stat-only):** `callback=NULL`. The function returns the first candidate path where `stat()` succeeds. No archive validation occurs. This quickly resolves libraries that exist as plain files.

2. **Pass 2 (archive validation):** `callback=archive_validate_callback` (`sub_42A2D0`). The function finds the file via `stat()`, then invokes the callback to open it as an archive and verify that at least one member has the correct CPU architecture. The callback returns 0 to accept, or non-zero to continue searching the next directory.

The two-pass design optimizes the common case: most libraries are found in the first directory with the correct architecture, so the expensive archive-open-and-iterate path is only taken when the stat-only pass fails.

## Archive Validation Callback (sub\_42A2D0)

When the stat-only pass fails (Pass 1 finds no matching file) or the library needs architecture validation, `main` invokes Pass 2 with `sub_42A2D0` as the acceptance callback.

### Algorithm

```c
// sub_42A2D0 -- archive_validate_callback
int archive_validate_callback(char* archive_path, int flags) {
    // 1. Open archive
    archive_handle_t handle;
    int status = archive_open(&handle, archive_path);           // sub_4BDAC0

    // 2. Check initial status
    if (status == 7 && !suppress_arch_warn
        && !strstr(archive_path, "cudadevrt"))
        warning("architecture mismatch in %s", archive_path);  // sub_467460
    else if (status == 4)
        error("unsupported code in " + archive_path);
    else if (status != 0)
        error(archive_status_string(status));

    // 3. Iterate archive members
    member_t member;
    while (1) {
        status = archive_next_member(&member, handle);          // sub_4BDAF0
        if (status == 0) break;  // end of archive
        if (status == 7) {       // arch mismatch for this member
            if (!suppress_arch_warn && !strstr(archive_path, "cudadevrt"))
                warning("...");
            goto next_member;
        }
        if (status == 4) {
            error("unsupported code in " + archive_path);
        }

next_member:
        // 4. Validate CPU architecture via e_machine
        uint16_t elf_machine = get_elf_header(member)->e_machine;
        int expected = cpu_arch_to_elf_machine(cpu_arch_string);

        if (elf_machine == expected) {
            // 5. Match found -- extract and accept
            archive_close(handle);                              // sub_4BDB30
            process_member(archive_path);                       // sub_4297B0
            return 0;  // accept
        }
    }

    // 6. No compatible member found
    archive_close(handle);
    error("cannot find compatible member in %s", archive_path);
    return 1;  // reject
}
```

### CPU Architecture Mapping

The callback maps `--cpu-arch` string values to ELF `e_machine` constants. The mapping is implemented as a chain of `strcmp` calls with a default fallback:

| `--cpu-arch` value | ELF `e_machine` | Constant |
|---|---|---|
| `unknown` | 62 | `EM_X86_64` |
| `X86_64` | 62 | `EM_X86_64` |
| `X86` | 3 | `EM_386` |
| `ARMv7` | 40 | `EM_ARM` |
| `PPC64LE` | 21 | `EM_PPC64` |
| `AARCH64` | 183 | `EM_AARCH64` |

The `unknown` and `X86_64` cases are checked first (both map to `EM_X86_64`), which is the fast path for the overwhelmingly common x86-64 host environment. If none of the known strings match, the callback emits an error diagnostic `"unexpected cpuArch"` and sets `e_machine` to 0, which will never match any valid ELF member.

### Archive Status Codes

The archive API returns status codes that the callback must interpret:

| Code | Meaning | Callback behavior |
|---|---|---|
| 0 | Success / end of iteration | Break iteration loop |
| 4 | Format error (not a valid archive / member) | Emit error, continue |
| 7 | Architecture mismatch | Emit warning (unless suppressed), continue |
| Other | General error | Convert to error string via `sub_4BC270` |

### cudadevrt Suppression

Architecture mismatch warnings (status code 7) are silently suppressed for archives whose path contains the substring `"cudadevrt"`. The check `strstr(archive_path, "cudadevrt")` appears at two points in the callback: once for the initial archive open status, and once for per-member iteration status. This prevents spurious warnings when `libcudadevrt.a` is built for a different host architecture than specified by `--cpu-arch`, which is a common configuration in cross-compilation scenarios.

## Global Variables

| Address | Type | Name (recovered) | Usage |
|---|---|---|---|
| `qword_2A5F300` | `node*` | `library_path_list` | Linked list from `-L` flags |
| `qword_2A5F2F8` | `node*` | `library_name_list` | Linked list from `-l` flags |
| `qword_2A5F330` | `node*` | `input_file_list` | Master input list; resolved libraries appended here |
| `qword_2A5F318` | `char*` | `arch_string` | Target GPU architecture (e.g., `"sm_90a"`) |
| `qword_2A5F2A0` | `char*` | `cpu_arch_string` | Host CPU architecture (e.g., `"X86_64"`) |
| `byte_2A5F298` | `bool` | `suppress_arch_warn` | Suppresses architecture mismatch warnings globally |
| `dword_2A77DC0` | `int` | `linker_mode` | Controls whether library resolution runs (skipped for modes 1, 2) |

## Implementation Notes

### Tail-Pointer Linked List

The search context's tail-pointer design eliminates branching in the append path. Traditional singly-linked lists require a conditional to handle the empty-list case (`if (head == NULL) head = new; else tail->next = new`). By initializing `tail` to `&head`, the generic `*tail = new` write handles both empty and non-empty lists identically. This is the same idiom used by the Linux kernel's `list_head` and Linus Torvalds' "good taste" linked list example.

### String Builder for Path Construction

Path construction uses the shared string builder infrastructure (`sub_44FB20` create, `sub_44FF90` append char, `sub_44FE60` append string, `sub_44FDC0` finalize). The builder is initialized with a 128-byte buffer and grows as needed. `path_search` uses it inline rather than calling `path_join`, directly appending directory characters (with trailing-slash stripping) and the filename. The finalized string is arena-allocated and returned to the caller.

### Arena Memory Management

Every string allocation in the search subsystem goes through the arena allocator (`sub_4307C0`). Temporary copies (e.g., the working copy in `path_split`) are freed via `sub_431000`. The search context itself, its directory nodes, and the path strings all live in arena memory, ensuring cleanup is handled when the arena is destroyed rather than requiring individual `free` calls.

## Cross-References

- [Library Resolution (pipeline)](../pipeline/library-resolution.md) -- pipeline-level view of when and how library search runs
- [CLI Option Parsing](../pipeline/cli-options.md) -- `-L`, `-l`, `--cpu-arch`, `--keep-system-libraries` registration
- [Archive Processing](../input/archives.md) -- `sub_4BDAC0`, `sub_4BDAF0`, `sub_4BDB30` archive member API
- [Input File Loop](../pipeline/input-loop.md) -- consumes the resolved input file list
- [Memory Arenas](memory-arenas.md) -- `sub_4307C0` / `sub_431000` arena allocator used throughout
- [Error Reporting](error-reporting.md) -- `sub_467460` diagnostic emission

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| Search context is 16 bytes with self-referencing tail pointer | HIGH | `sub_4622D0` decompiled: `sub_4307C0(v2, 16)`, then `*result = 0; result[1] = result` -- self-reference confirmed |
| `search_context_append` at `sub_462500` takes `(ctx, path)` | HIGH | Decompiled: `sub_464460(a2, 0)` then `**(_QWORD **)(a1 + 8) = result; *(_QWORD *)(a1 + 8) = result` |
| `search_context_append_cb` at `sub_462520` swaps argument order `(path, ctx)` | HIGH | Decompiled: `sub_464460(a1, 0)` then operates on `a2` -- argument swap confirmed |
| `make_library_filename` uses DWORD trick `0x0062696C` for "lib" | HIGH | `sub_429AA0` decompiled: `*(_DWORD *)v4 = 6449516` which is `0x62696C` = `"lib\0"` little-endian |
| `.so` suffix via DWORD `0x006F732E` | HIGH | `sub_429AA0` decompiled: `*(_DWORD *)stpcpy(v8, v5) = (_DWORD)&loc_6F732E` -- address encodes `.so\0` |
| `.a` suffix via `strcpy` | HIGH | `sub_429AA0` decompiled: `strcpy(stpcpy(v8, v5), ".a")` |
| `path_search` at `sub_462870` (4,905 B) iterates directories with `stat()` | HIGH | Decompiled function exists (large, matches described size); uses `stat` for existence check |
| CPU architecture mapping: `X86_64`=62, `X86`=3, `ARMv7`=40, `AARCH64`=183, `PPC64LE`=21 | HIGH | String `"unknown,X86,X86_64,ARMv7,AARCH64,PPC64LE"` at `0x1d332f0`; individual arch strings at `0x1d33fe5`--`0x1d33ffa` |
| `"unexpected cpuArch"` error message | HIGH | String at `0x1d34002` in strings JSON |
| `cudadevrt` suppression for arch mismatch warnings | HIGH | String `"found IR for libcudadevrt"` at `0x1d340a8`; `strstr` check visible in decompiled `sub_42A2D0` |
| `LIBRARY_PATH` environment variable used for search path | MEDIUM | Wiki says `LIBRARY_PATH` but strings JSON only shows `LD_LIBRARY_PATH` at `0x225fcda`; the actual `getenv` call target needs verification against decompiled main |
| String tokenizer at `sub_44EC40` (576 B) with `sub_44E8B0` (4,780 B) | HIGH | Both decompiled files exist with matching sizes; `sub_44EC40` calls `sub_44E8B0` in a loop |
| Tail-pointer linked list idiom | HIGH | Confirmed by `sub_4622D0` decompiled code: `result[1] = result` is the self-referencing tail initialization |
| Two-pass search strategy (stat-only then archive validation) | MEDIUM | Inferred from main() call pattern; `sub_462870` signature supports optional callback parameter |
| `--cpu-arch` option string | HIGH | String `"cpu-arch"` at `0x1d326cd` in strings JSON |
